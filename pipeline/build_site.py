"""Assemble the public GitHub Pages build in docs/.

The local app reads 29.2 M field polygons out of GeoParquet with DuckDB-WASM. None of that
can ship: the trazo4 corpus is unpublished, it is 10.3 GB, and GitHub Pages is a static
host. So the public build is a different thing wearing the same interface - five fixed
example windows, each shipped as a plain GeoJSON of already-scored fields.

That trade is worth stating plainly. The public site cannot answer "show me the fields
here"; it can only answer "show me these five places". In exchange it needs no server, no
WebAssembly, no range requests, and it loads in about a second.

What ships:
    basin drought layer      1,072 basins, whole-country, ~2 MB
    five example windows     scored fields with their NDVI response
    the drought events       per-basin event lists for the basin cards

What does not ship: data/fields/*.parquet, data/inventory.json, and the DuckDB path that
reads them.

    <ftw-python> build_site.py [--out docs]
"""
import argparse
import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
DATA = os.path.join(APP, "data")
RESP = os.path.join(DATA, "response")

# MapBiomas classes, for naming each example by what actually grows there rather than by a
# municipality name I would be guessing at.
# Every MapBiomas class that appears in these windows, so a label never falls back to a
# bare class number. Crop classes are the ones that get scored; the rest are drawn only.
CROP_NAME = {
    3: "forest", 4: "savanna", 5: "mangrove", 6: "flooded forest",
    9: "planted forest", 11: "wetland", 12: "grassland", 15: "pasture",
    18: "cropland", 19: "temporary crop", 20: "sugarcane",
    21: "mosaic of agriculture and pasture", 23: "beach", 24: "urban",
    25: "other non-vegetated", 26: "water", 29: "rocky outcrop", 30: "mining",
    31: "aquaculture", 32: "salt flat", 33: "river or lake", 35: "oil palm",
    36: "perennial crop", 39: "soybean", 40: "rice", 41: "other temporary crop",
    46: "coffee", 47: "citrus", 48: "other perennial", 49: "wooded sandbank",
    50: "sandbank", 62: "cotton",
}

# The five, in the order they appear in the nav. Region and system are editorial; every
# number attached to them is computed.
EXAMPLES = [
    ("6060718180", "Sao Paulo", "Sugarcane belt"),
    ("6060723000", "Minas Gerais", "Coffee country"),
    ("6060492990", "Mato Grosso", "Soy frontier, Cerrado"),
    ("6060762870", "Parana", "Double-cropped south"),
    ("6060011380", "Pernambuco", "Semi-arid northeast"),
    ("6060739950", "Sao Paulo", "Highest composite stress"),
    ("6060501000", "Mato Grosso", "Western Mato Grosso"),
    ("6060008110", "Para", "Amazon frontier"),
    ("6060013430", "Espirito Santo", "Coastal southeast"),
    ("6060011900", "Bahia", "Northeast Cerrado"),
]

COPY_SRC = [
    "index.html",
    ("src", ["app.js", "app.css", "charts.js", "commands.js", "fields.js", "tour.js"]),
    ("vendor", None),
]

# Douglas-Peucker tolerance for the published geometry, in degrees (~11 m). Finer than
# the 30 m pixels the scoring measured on, so it cannot lose anything the NDVI saw.
SIMPLIFY_DEG = 0.0001
# Unscored fields are context - they exist to show that coverage is complete, and nobody
# reads their exact edge. Coarser simplification there costs nothing legible and, since
# they are the majority in most windows, saves most of the payload.
SIMPLIFY_DEG_UNSCORED = 0.00025

COPY_DATA = [
    "basins_2024.geojson",
    "events_2024.json",
    "drought_meta.json",
    "basin_fields_2024.json",
    "stressed_basins.json",
]


def ensure_geojson(hybas, rec, dst):
    """Geometry for an example, read from the parquet by fid.

    `rec["fields"]` maps fid -> [anom, held, drop, mb_class], so the scoring already names
    every field it kept. This just attaches shapes to those names.
    """
    import duckdb
    import geopandas as gpd
    import numpy as np

    fields = rec["fields"]
    fids = [int(k) for k in fields]
    w, s, e, n = rec["aoi"]
    inv = [x for x in json.load(open(os.path.join(DATA, "inventory.json"), encoding="utf-8"))
           if x["year"] == 2024]
    hit = [x for x in inv
           if x["xmin"] <= e and x["xmax"] >= w and x["ymin"] <= n and x["ymax"] >= s]
    if not hit:
        raise RuntimeError("no parquet covers %s" % hybas)
    urls = ",".join("'%s'" % os.path.join(DATA, "fields", x["file"]).replace(os.sep, "/")
                    for x in hit)

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("CREATE TEMP TABLE keep (fid BIGINT)")
    con.executemany("INSERT INTO keep VALUES (?)", [(f,) for f in fids])
    # geom is typed GEOMETRY, so a bare SELECT hands back DuckDB's internal serialisation
    # rather than WKB. ST_AsWKB forces the encoding shapely reads.
    df = con.execute(f"""
        SELECT p.fid, p.mb_class, p.area_ha, ST_AsWKB(p.geom) AS geom
        FROM read_parquet([{urls}]) p
        JOIN keep k ON k.fid = p.fid
        WHERE p.lon BETWEEN {w} AND {e} AND p.lat BETWEEN {s} AND {n}
    """).fetchdf()
    if df.empty:
        raise RuntimeError("no geometry matched for %s" % hybas)

    wkb = df.geom.map(lambda b: bytes(b) if b is not None else None)
    g = gpd.GeoDataFrame(df.drop(columns="geom"),
                         geometry=gpd.GeoSeries.from_wkb(wkb), crs="EPSG:4326")
    g["anom"] = [fields[str(int(f))][0] for f in g.fid]
    g["held"] = [fields[str(int(f))][1] for f in g.fid]
    g["area_ha"] = np.round(g.area_ha.astype("float64"), 1)
    g = g[["fid", "mb_class", "area_ha", "held", "anom", "geometry"]]
    g.to_file(dst, driver="GeoJSON", coordinate_precision=5)
    return len(g)


def publish_geometry(src, dst):
    """Write the delivery copy of an example: simplified, rounded, no index column."""
    import geopandas as gpd

    g = gpd.read_file(src)
    before = os.path.getsize(src)
    scored = g["held"].notna() if "held" in g.columns else None
    if scored is None or scored.all():
        g["geometry"] = g.geometry.simplify(SIMPLIFY_DEG)
    else:
        geom = g.geometry.copy()
        geom.loc[scored] = g.geometry[scored].simplify(SIMPLIFY_DEG)
        geom.loc[~scored] = g.geometry[~scored].simplify(SIMPLIFY_DEG_UNSCORED)
        g["geometry"] = geom
    g = g[g.geometry.notna() & ~g.geometry.is_empty]
    if os.path.exists(dst):
        os.remove(dst)
    g.to_file(dst, driver="GeoJSON", coordinate_precision=5)
    return len(g), before, os.path.getsize(dst)


def dominant_crop(path):
    """The two commonest classes among SCORED fields, and the counts drawn vs scored.

    Scored fields only: the label answers "what is this example of", and every window
    also contains pasture and woodland that are drawn for coverage but never measured.
    Counting those would describe the landscape instead of the analysis.
    """
    with open(path, encoding="utf-8") as fh:
        gj = json.load(fh)
    counts = {}
    n_scored = 0
    for f in gj["features"]:
        p = f["properties"]
        if p.get("held") is None:
            continue
        n_scored += 1
        c = p.get("mb_class")
        if c is not None:
            counts[int(c)] = counts.get(int(c), 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:2]
    return ([CROP_NAME.get(c, "class %d" % c) for c, _ in top],
            len(gj["features"]), n_scored)


def build_config(static=True):
    """The public config. Deliberately a separate file from the local one: the local app
    keeps its DuckDB path, and nothing here should be able to switch it back on."""
    return """/**
 * Public build - see pipeline/build_site.py.
 *
 * This is NOT the local config. The field parquet and the DuckDB reader that queries it
 * do not ship: trazo4 is unpublished and 10.3 GB. What ships is five pre-scored example
 * windows as plain GeoJSON, so the site is fully static.
 */
export const CONFIG = {
\tSTATIC: true,

\tBASINS_URL: './data/basins_2024.geojson',
\tEVENTS_URL: './data/events_2024.json',
\tMETA_URL: './data/drought_meta.json',
\tBASIN_FIELDS_URL: './data/basin_fields_2024.json',
\tSTRESSED_URL: './data/stressed_basins.json',

\t/** The five example windows, written by pipeline/build_site.py. */
\tEXAMPLES_URL: './data/examples/index.json',
\tEXAMPLES_BASE: './data/examples',

\tYEAR: %(year)d,

\t/** Unused in the static build - kept so the shared app.js needs no branching. */
\tFIELD_MIN_ZOOM: 10.5,
\tMAX_FIELDS: 12000,
\tMAX_QUERY_DEG: 1.2,

\tMB_CLASSES: %(mb)s,

\tCROP_CLASSES: %(crops)s,

\tDROUGHT_METRICS: %(metrics)s
};
"""



def bust_caches(out):
    """Give every script and stylesheet a content-hashed URL.

    GitHub Pages serves with a ten-minute cache, and index.html loading ./src/app.js by a
    fixed URL meant a returning visitor kept running the previous deploy's code - the
    example buttons "missing" after one push, and the field-view fixes invisible after
    another. ES module imports are cached by URL too, so the imports inside the scripts are
    rewritten as well, each file always carrying its own hash so a module shared by two
    importers is still loaded once.
    """
    import hashlib
    import re

    files = {
        "config.js": os.path.join(out, "config.js"),
    }
    src_dir = os.path.join(out, "src")
    for f in os.listdir(src_dir):
        if f.endswith((".js", ".css")):
            files["src/" + f] = os.path.join(src_dir, f)
    # Hash the content before any rewriting, so the hash tracks the source that changed.
    tag = {k: hashlib.sha1(open(v, "rb").read()).hexdigest()[:10] for k, v in files.items()}

    def swap_imports(path, base):
        s = open(path, encoding="utf-8").read()

        def repl(m):
            spec = m.group(2)
            target = os.path.normpath(os.path.join(base, spec)).replace(os.sep, "/")
            key = next((k for k in tag if target.endswith(k)), None)
            if not key:
                return m.group(0)
            return "%s%s?v=%s%s" % (m.group(1), spec, tag[key], m.group(3))

        s = re.sub(r"""(from\s+['"])(\.{1,2}/[^'"?]+\.js)(['"])""", repl, s)
        s = re.sub(r"""(import\(\s*['"])(\.{1,2}/[^'"?]+\.js)(['"])""", repl, s)
        open(path, "w", encoding="utf-8").write(s)

    for k, v in files.items():
        if k.endswith(".js"):
            swap_imports(v, os.path.dirname(k) or ".")

    idx = os.path.join(out, "index.html")
    h = open(idx, encoding="utf-8").read()
    for k, v in tag.items():
        h = h.replace('"./%s"' % k, '"./%s?v=%s"' % (k, v))
    open(idx, "w", encoding="utf-8").write(h)
    return tag

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs")
    args = ap.parse_args()
    out = os.path.join(APP, args.out)

    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "data", "examples"))

    # --- code ---------------------------------------------------------------
    shutil.copy2(os.path.join(APP, "index.html"), out)
    for item in COPY_SRC[1:]:
        name, files = item
        src = os.path.join(APP, name)
        dst = os.path.join(out, name)
        os.makedirs(dst, exist_ok=True)
        for f in (files or os.listdir(src)):
            shutil.copy2(os.path.join(src, f), dst)

    # --- config, with the DuckDB path removed --------------------------------
    local = open(os.path.join(APP, "config.js"), encoding="utf-8").read()

    def grab(key):
        i = local.index(key + ":")
        depth, j = 0, local.index(("[" if local[i + len(key) + 2] == "[" else "{"), i)
        k = j
        while True:
            if local[k] in "[{":
                depth += 1
            elif local[k] in "]}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        return local[j:k + 1]

    cfg = build_config() % {
        "year": 2024,
        "mb": grab("MB_CLASSES"),
        "crops": grab("CROP_CLASSES"),
        "metrics": grab("DROUGHT_METRICS"),
    }
    open(os.path.join(out, "config.js"), "w", encoding="utf-8").write(cfg)

    # --- data ---------------------------------------------------------------
    for f in COPY_DATA:
        shutil.copy2(os.path.join(DATA, f), os.path.join(out, "data", f))

    index = []
    for hybas, region, system in EXAMPLES:
        rp = os.path.join(RESP, "response_%s.json" % hybas)
        gp = os.path.join(RESP, "fields_%s.geojson" % hybas)
        if not os.path.exists(rp):
            print("  skipping %s - not scored yet" % hybas)
            continue
        rec = json.load(open(rp, encoding="utf-8"))
        if not os.path.exists(gp):
            if "fields" not in rec:
                print("  skipping %s - no geometry and no field list" % hybas)
                continue
            n_geom = ensure_geojson(hybas, rec, gp)
            print("  %s: derived geometry for %s fields from parquet"
                  % (hybas, "{:,}".format(n_geom)))

        # Published as .json, not .geojson: GitHub Pages compresses application/json and
        # may serve an unknown extension uncompressed. It is 7.6 MB against 1.0 MB.
        pub = os.path.join(out, "data", "examples", "fields_%s.json" % hybas)
        n_pub, raw_b, pub_b = publish_geometry(gp, pub)
        # The per-field table in response_*.json duplicates what the GeoJSON already
        # carries, so the public copy drops it and keeps only the basin-level series.
        slim = {k: v for k, v in rec.items() if k != "fields"}
        # Per-field trends ship separately: they are the biggest file here and the page
        # only needs them once someone clicks a field.
        sp = os.path.join(RESP, "series_%s.json" % hybas)
        has_series = os.path.exists(sp)
        if has_series:
            shutil.copy2(sp, os.path.join(out, "data", "examples",
                                          "series_%s.json" % hybas))
        json.dump(slim, open(os.path.join(out, "data", "examples",
                                          "response_%s.json" % hybas), "w"),
                  separators=(",", ":"))

        crops, n, n_scored = dominant_crop(pub)
        w, s, e, n_ = rec["aoi"]
        index.append({
            "hybas": hybas,
            "region": region,
            "system": system,
            "crops": crops,
            "aoi": rec["aoi"],
            "center": [round((w + e) / 2, 5), round((s + n_) / 2, 5)],
            "n_fields": n,
            "n_scored": n_scored,
            "res_m": rec.get("res_m"),
            "event": rec["event"],
            "season_overlap": rec["season_overlap"],
            "size_mb": round(pub_b / 1e6, 1),
            "series": has_series,
        })
        print("  %s  %-14s %7s drawn %7s scored  %5.1f -> %4.1f MB  %s"
              % (hybas, region, "{:,}".format(n), "{:,}".format(n_scored),
                 raw_b / 1e6, pub_b / 1e6, ", ".join(crops)))

    json.dump(index, open(os.path.join(out, "data", "examples", "index.json"), "w"),
              separators=(",", ":"), indent=1)

    # GitHub Pages runs Jekyll by default, which silently drops files and folders whose
    # names begin with an underscore. Nothing here does today, but the cost of being wrong
    # is a 404 on a data file with no error anywhere.
    open(os.path.join(out, ".nojekyll"), "w").close()

    # The earlier 2019-2025 drought dashboard served at the site root before this app
    # replaced it, and it stays reachable at /overview.html. It has to be copied here, by
    # the build, from its source in drought/: this function deletes docs/ before writing,
    # so a copy placed by hand is silently wiped by the next rebuild - which is exactly how
    # it was lost once.
    tags = bust_caches(out)
    print("  cache-busted %d files (app.js ?v=%s)" % (len(tags), tags.get("src/app.js")))

    legacy = os.path.join(APP, "drought", "index.html")
    if os.path.exists(legacy):
        shutil.copy2(legacy, os.path.join(out, "overview.html"))
    else:
        print("  WARNING: drought/index.html missing - /overview.html will 404")

    total = sum(os.path.getsize(os.path.join(dp, f))
                for dp, _, fs in os.walk(out) for f in fs)
    print("\n%s: %d examples, %.1f MB total" % (args.out, len(index), total / 1e6))


if __name__ == "__main__":
    main()
