"""Repack the trazo4 2024 field parquets for browser querying.

The raw files cannot be queried from a browser. Rows sit in arbitrary order, so every row
group spans nearly the whole state and a viewport predicate prunes nothing - a single
map pan would pull the entire file. The fix is the standard cloud-native GeoParquet
recipe, and it is the reason this stage exists at all:

1. sort rows on a Hilbert curve over the file's own extent, so fields that are near each
   other on the ground are near each other in the file
2. write small row groups, so the per-group bbox statistics are tight enough that DuckDB
   can skip nearly all of them
3. keep the existing `geom_bbox` struct, which is what the viewport predicate tests
4. emit an inventory of per-file extents, so whole files can be excluded before DuckDB
   is even asked - the same file-level pruning the WRI cloud-native-query demo does

Measured on Brazil_Acre_2023: mean row-group x-span falls from 5.33 deg to 1.96 deg over
a 7.15 deg extent, which is the expected optimum for 12 groups in two dimensions. Large
states get hundreds of groups and correspondingly tighter boxes.

Columns are cut to what the application actually reads. Geometry is ~93% of the bytes, so
everything else is nearly free to carry.

The source schema is NOT uniform, which the first run discovered the hard way. The
MapBiomas class column carries the year in its name (`mbmode24` for 2024), and
`country`/`state`/`year` appear in only 9 of the 34 files. Region is therefore taken from
the filename, which is reliable for every file, and every optional column is probed before
it is selected.

    <crop-python> repack_fields.py [--year 2024] [--only Brazil] [--rg 15000]

Scope note: the trazo4 field data is UNPUBLISHED and must not be pushed to any public
host. See the README - the output of this stage stays local.
"""
import argparse
import glob
import json
import os
import time

import duckdb

SRC_DIR = r"E:\trazo4_fields_parquet"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(os.path.dirname(HERE), "data", "fields")
INVENTORY = os.path.join(os.path.dirname(HERE), "data", "inventory.json")

# Row-group target. Small enough that a city-scale viewport touches one or two groups,
# large enough that the per-group footer overhead stays negligible.
ROW_GROUP = 15000


def region_of(path):
    stem = os.path.basename(path)[: -len(".parquet")]
    region, year = stem.rsplit("_", 1)
    return region, int(year)


def columns_of(con, src):
    """Column names actually present in one source file."""
    rows = con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [src]).fetchall()
    return {r[0] for r in rows}


def repack(con, src, dst, rg, region, year):
    ext = con.execute(
        """SELECT min(geom_bbox.xmin), max(geom_bbox.xmax),
                  min(geom_bbox.ymin), max(geom_bbox.ymax), count(*)
           FROM read_parquet(?)""", [src]).fetchone()
    xmin, xmax, ymin, ymax, n = ext
    if not n:
        return None
    # A degenerate extent (a single field, or all fields on one line) would make the
    # Hilbert box zero-width and the sort meaningless; pad it rather than fail.
    if xmax - xmin < 1e-6:
        xmax = xmin + 1e-6
    if ymax - ymin < 1e-6:
        ymax = ymin + 1e-6

    have = columns_of(con, src)
    yy = year % 100
    # MapBiomas class is year-suffixed; area_ha and the country/state text columns are
    # present in only some files, so each is probed and given a constant fallback.
    mb = "mbmode%02d" % yy
    country = "country" if "country" in have else "'%s'" % region.split("_")[0]
    state = "state" if "state" in have else (
        "'%s'" % region.split("_", 1)[1].replace("_", " ") if "_" in region else "NULL")
    area = "area_ha" if "area_ha" in have else "CAST(NULL AS DOUBLE)"
    mb_expr = ("CAST(%s AS SMALLINT)" % mb) if mb in have else "CAST(NULL AS SMALLINT)"
    hly = ("CAST(hansen_loss_year AS SMALLINT)" if "hansen_loss_year" in have
           else "CAST(NULL AS SMALLINT)")
    hlf = "hansen_loss_frac" if "hansen_loss_frac" in have else "CAST(NULL AS DOUBLE)"

    con.execute(f"""
        COPY (
          SELECT
            fid,
            {country}                          AS country,
            {state}                            AS state,
            CAST({year} AS SMALLINT)           AS year,
            {mb_expr}                          AS mb_class,
            {area}                             AS area_ha,
            {hly}                              AS hansen_loss_year,
            {hlf}                              AS hansen_loss_frac,
            geom_bbox,
            (geom_bbox.xmin + geom_bbox.xmax) / 2 AS lon,
            (geom_bbox.ymin + geom_bbox.ymax) / 2 AS lat,
            geom
          FROM read_parquet('{src}')
          ORDER BY ST_Hilbert(
            ST_Point((geom_bbox.xmin + geom_bbox.xmax) / 2,
                     (geom_bbox.ymin + geom_bbox.ymax) / 2),
            {{min_x: {xmin}, min_y: {ymin}, max_x: {xmax}, max_y: {ymax}}}::BOX_2D)
        ) TO '{dst}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE {rg})
    """)
    return dict(rows=int(n), xmin=float(xmin), xmax=float(xmax),
                ymin=float(ymin), ymax=float(ymax))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2024)
    # Brazil by default: the drought layer is Brazil-only, so fields outside it would sit
    # on the map with no basin context behind them.
    ap.add_argument("--only", default="Brazil",
                    help="substring filter on the region name; pass '' for every region")
    ap.add_argument("--rg", type=int, default=ROW_GROUP)
    ap.add_argument("--memory", default="8GB")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    files = sorted(glob.glob(os.path.join(SRC_DIR, "*_%d.parquet" % args.year)))
    if args.only:
        files = [f for f in files if args.only.lower() in os.path.basename(f).lower()]
    if not files:
        raise SystemExit("no source files matched")

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("SET preserve_insertion_order=false;")
    con.execute(f"SET memory_limit='{args.memory}';")
    con.execute(f"SET temp_directory='{os.path.join(OUT_DIR, '_tmp')}';")

    prior = json.load(open(INVENTORY)) if os.path.exists(INVENTORY) else []
    # Keep this year's entries so the resume check can see them; other years pass through
    # untouched so a 2024 run never discards a 2023 catalogue.
    inv = list(prior)

    t_all = time.time()
    for i, src in enumerate(files, 1):
        region, year = region_of(src)
        name = "%s_%d.parquet" % (region, year)
        dst = os.path.join(OUT_DIR, name)
        meta = next((e for e in inv if e["file"] == name), None)
        if meta and os.path.exists(dst) and os.path.getsize(dst) > 0:
            print("  [%2d/%d] %-30s cached" % (i, len(files), region), flush=True)
            continue
        t0 = time.time()
        try:
            st = repack(con, src, dst, args.rg, region, year)
        except Exception as e:                       # noqa: BLE001
            print("  [%2d/%d] %-30s FAILED %s" % (i, len(files), region, str(e)[:70]))
            continue
        if st is None:
            print("  [%2d/%d] %-30s empty, skipped" % (i, len(files), region))
            continue
        mb = os.path.getsize(dst) / 1e6
        st.update(file=name, region=region, year=year, mb=round(mb, 1))
        inv = [e for e in inv if e["file"] != name] + [st]
        print("  [%2d/%d] %-30s %9s rows  %7.0f MB  %5.0f s"
              % (i, len(files), region, "{:,}".format(st["rows"]), mb, time.time() - t0),
              flush=True)
        json.dump(sorted(inv, key=lambda e: (e["year"], e["region"])),
                  open(INVENTORY, "w"), indent=1)

    total_mb = sum(e["mb"] for e in inv if e["year"] == args.year)
    total_rows = sum(e["rows"] for e in inv if e["year"] == args.year)
    print("\n%d files, %s fields, %.1f GB, %.0f min total"
          % (len([e for e in inv if e["year"] == args.year]),
             "{:,}".format(total_rows), total_mb / 1000, (time.time() - t_all) / 60))
    print("inventory -> %s" % INVENTORY)


if __name__ == "__main__":
    main()
