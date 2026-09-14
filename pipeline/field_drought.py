"""Join every Brazilian field to its basin, and rank where drought meets farmland.

A basin can be the driest in the country and still not matter agriculturally, and a basin
full of cropland can have had an easy year. What the app needs is the intersection: where
severe 2024 drought sits on top of a lot of farmed land.

This stage does the spatial join once - 29.2 million field centroids against 1,072
HydroBASINS polygons - and writes per-basin field statistics that the map can read
instantly. No imagery is involved; that comes later, for the basins this stage selects.

Method note: the join is done on the field CENTROID, so a field straddling a basin
boundary is counted once, in the basin holding its middle. Fields are 1-100 ha against
basins of 3,000-30,000 km2, so the edge effect is negligible.

Outputs (data/)
    basin_fields_2024.json   per basin: field counts and hectares by land-use group
    stressed_basins.json     the ranked shortlist, with the composite stress score

    <crop-python> field_drought.py
"""
import json
import os
import time

import duckdb
import geopandas as gpd
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
FIELDS = os.path.join(APP, "data", "fields")
INVENTORY = os.path.join(APP, "data", "inventory.json")
OUT = os.path.join(APP, "data")

DROUGHT_CANDIDATES = [
    r"E:\Water\water intelligence\drought",
    r"H:\water intelligence\drought",
]
DROUGHT = next((d for d in DROUGHT_CANDIDATES
                if os.path.exists(os.path.join(d, "data", "basins_lev06_br.gpkg"))),
               DROUGHT_CANDIDATES[0])

YEAR = 2024
CROP_CLASSES = [18, 19, 20, 35, 36, 39, 40, 41, 46, 47, 48, 62]
PASTURE_CLASSES = [15, 21]

# Composite water stress. Duration and intensity are different failures - a basin can be
# dry for most of the year in small doses, or have one long unbroken event - so the score
# takes both, each scaled to its own 95th percentile so neither unit dominates, plus the
# accumulated deficit which is the volume of missing rain.
STRESS_WEIGHTS = {"total_drought_days": 0.4, "longest_event_days": 0.35,
                  "total_deficit_mm": 0.25}


def main():
    t0 = time.time()
    basins = gpd.read_file(os.path.join(DROUGHT, "data", "basins_lev06_br.gpkg"),
                           layer="basins")
    summ = pd.read_csv(os.path.join(DROUGHT, "output",
                                    "drought_summary_by_basin_year.csv"))
    summ = summ[summ.year == YEAR]
    print("basins %d, drought rows %d" % (len(basins), len(summ)))

    inv = [e for e in json.load(open(INVENTORY)) if e["year"] == YEAR]
    files = [os.path.join(FIELDS, e["file"]).replace(os.sep, "/") for e in inv]
    print("field files: %d" % len(files))

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("SET preserve_insertion_order=false;")
    con.execute("SET memory_limit='8GB';")
    con.execute("SET temp_directory=?", [os.path.join(FIELDS, "_tmp")])

    # Basins go in as WKB so DuckDB can do the containment test itself.
    b = basins[["bidx", "HYBAS_ID", "SUB_AREA", "geometry"]].copy()
    b["HYBAS_ID"] = b["HYBAS_ID"].astype("int64")
    b["wkb"] = b.geometry.to_wkb()
    con.register("basins_df", pd.DataFrame(b.drop(columns="geometry")))
    con.execute("""
        CREATE TABLE basins AS
        SELECT bidx, HYBAS_ID, SUB_AREA, ST_GeomFromWKB(wkb) AS geom,
               -- plain doubles rather than ST_Extent: BOX_2D is not a struct and cannot
               -- be dereferenced, and the join only needs four numbers anyway
               ST_XMin(ST_GeomFromWKB(wkb)) AS xmin,
               ST_XMax(ST_GeomFromWKB(wkb)) AS xmax,
               ST_YMin(ST_GeomFromWKB(wkb)) AS ymin,
               ST_YMax(ST_GeomFromWKB(wkb)) AS ymax
        FROM basins_df""")
    print("basins loaded  (%.0f s)" % (time.time() - t0))

    urls = ",".join("'%s'" % f for f in files)
    t1 = time.time()
    # The bbox test runs first and is cheap; ST_Within only sees the survivors.
    con.execute(f"""
        CREATE TABLE joined AS
        SELECT b.bidx,
               f.mb_class,
               f.area_ha
        FROM read_parquet([{urls}]) f
        JOIN basins b
          ON f.lon BETWEEN b.xmin AND b.xmax
         AND f.lat BETWEEN b.ymin AND b.ymax
         AND ST_Within(ST_Point(f.lon, f.lat), b.geom)
    """)
    n = con.execute("SELECT count(*) FROM joined").fetchone()[0]
    print("joined %s fields to basins  (%.0f s)" % ("{:,}".format(n), time.time() - t1))

    crops = ",".join(str(c) for c in CROP_CLASSES)
    past = ",".join(str(c) for c in PASTURE_CLASSES)
    agg = con.execute(f"""
        SELECT bidx,
               count(*)                                          AS n_fields,
               sum(COALESCE(area_ha, 0))                         AS ha_total,
               count(*) FILTER (mb_class IN ({crops}))            AS n_crop,
               sum(COALESCE(area_ha, 0)) FILTER (mb_class IN ({crops})) AS ha_crop,
               count(*) FILTER (mb_class IN ({past}))             AS n_pasture,
               sum(COALESCE(area_ha, 0)) FILTER (mb_class IN ({past})) AS ha_pasture,
               median(COALESCE(area_ha, 0))                       AS ha_median
        FROM joined GROUP BY bidx""").fetchdf()
    print("aggregated %d basins with fields" % len(agg))

    df = basins[["bidx", "HYBAS_ID", "SUB_AREA"]].merge(agg, on="bidx", how="left")
    for c in ["n_fields", "ha_total", "n_crop", "ha_crop", "n_pasture", "ha_pasture"]:
        df[c] = df[c].fillna(0)
    df = df.merge(summ[["bidx", "total_drought_days", "longest_event_days",
                        "n_events", "total_deficit_mm"]], on="bidx", how="left")
    for c in STRESS_WEIGHTS:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    # scale each component to its own p95 so no unit dominates the composite
    stress = 0
    for k, w in STRESS_WEIGHTS.items():
        p95 = df[k].quantile(0.95) or 1
        stress = stress + w * (df[k] / p95).clip(upper=1.5)
    df["stress"] = (stress / sum(STRESS_WEIGHTS.values())).round(3)
    df["crop_share"] = (df.ha_crop / df.ha_total.replace(0, pd.NA)).fillna(0).round(3)
    # exposure = how much farmland actually sits under that stress
    df["exposure"] = (df.stress * df.ha_crop).round(0)

    df["HYBAS_ID"] = df["HYBAS_ID"].astype("int64").astype(str)
    keep = ["bidx", "HYBAS_ID", "SUB_AREA", "n_fields", "ha_total", "n_crop", "ha_crop",
            "n_pasture", "ha_pasture", "ha_median", "total_drought_days",
            "longest_event_days", "n_events", "total_deficit_mm", "stress",
            "crop_share", "exposure"]
    out = df[keep].copy()
    for c in ["ha_total", "ha_crop", "ha_pasture", "ha_median", "total_deficit_mm"]:
        out[c] = out[c].round(1)
    for c in ["n_fields", "n_crop", "n_pasture", "total_drought_days",
              "longest_event_days", "n_events"]:
        out[c] = out[c].astype(int)

    # JSON has no NaN literal, and basins with no fields produce one for the median.
    # json.dump writes bare NaN by default, which every browser rejects.
    out = out.fillna(0)
    recs = out.to_dict("records")
    json.dump({int(r["bidx"]): r for r in recs},
              open(os.path.join(OUT, "basin_fields_%d.json" % YEAR), "w"),
              separators=(",", ":"), allow_nan=False)

    # the shortlist: enough farmland to matter, ranked by how hard it was hit
    shortlist = out[(out.ha_crop >= 20000) & (out.n_crop >= 2000)] \
        .sort_values("exposure", ascending=False).head(30)
    json.dump(shortlist.fillna(0).to_dict("records"),
              open(os.path.join(OUT, "stressed_basins.json"), "w"),
              separators=(",", ":"), indent=1, allow_nan=False)

    print("\ntotal fields joined: %s of %s (%.1f%%)"
          % ("{:,}".format(int(out.n_fields.sum())),
             "{:,}".format(sum(e["rows"] for e in inv)),
             100 * out.n_fields.sum() / sum(e["rows"] for e in inv)))
    print("cropland in basins: %.2f Mha" % (out.ha_crop.sum() / 1e6))
    print("\nTop 12 basins by drought-on-cropland exposure:")
    print("  %-12s %6s %7s %9s %8s %7s %6s"
          % ("HYBAS", "days", "longest", "crop ha", "crop n", "stress", "expos"))
    for r in shortlist.head(12).itertuples():
        print("  %-12s %6d %7d %9s %8s %7.2f %6.0fk"
              % (r.HYBAS_ID, r.total_drought_days, r.longest_event_days,
                 "{:,.0f}".format(r.ha_crop), "{:,}".format(r.n_crop),
                 r.stress, r.exposure / 1000))
    print("\ndone in %.0f s" % (time.time() - t0))


if __name__ == "__main__":
    main()
