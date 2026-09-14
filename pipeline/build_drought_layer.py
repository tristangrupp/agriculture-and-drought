"""Export the 2024 drought product as compact GeoJSON for the map.

Reuses the finished CHIRPS x HydroBASINS analysis in ..\..\drought - nothing is
recomputed here. The basins are simplified and the attributes cut to what the map and the
command palette actually read, so the whole drought layer stays small enough to ship
inline and to filter client-side without a query round trip.

Output (data/)
    basins_2024.geojson   1072 HydroBASINS level-6 basins with 2024 drought attributes
    events_2024.json      drought events that were running during 2024, per basin
    drought_meta.json     value ranges and the field list, for legend and palette

    <crop-python> build_drought_layer.py [--year 2024]
"""
import argparse
import json
import os
import sys

import geopandas as gpd
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
# The CHIRPS x HydroBASINS analysis lives on E: - it was moved off H: between sessions,
# so the location is resolved rather than assumed, and a missing copy fails loudly.
DROUGHT_CANDIDATES = [
    r"E:\Water\water intelligence\drought",
    r"H:\water intelligence\drought",
]
DROUGHT = next((d for d in DROUGHT_CANDIDATES
                if os.path.exists(os.path.join(d, "data", "basins_lev06_br.gpkg"))),
               DROUGHT_CANDIDATES[0])
OUT_DIR = os.path.join(os.path.dirname(HERE), "data")
SIMPLIFY_DEG = 0.01          # ~1 km; basins are 3,000-30,000 km2 so shape survives

METRICS = [
    ("total_drought_days", "Days in drought"),
    ("longest_event_days", "Longest event (days)"),
    ("n_events", "Number of events"),
    ("total_deficit_mm", "Rainfall deficit (mm)"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2024)
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)

    gpkg = os.path.join(DROUGHT, "data", "basins_lev06_br.gpkg")
    summ_csv = os.path.join(DROUGHT, "output", "drought_summary_by_basin_year.csv")
    ev_csv = os.path.join(DROUGHT, "output", "drought_events.csv")
    for p in (gpkg, summ_csv, ev_csv):
        if not os.path.exists(p):
            sys.exit("missing %s - run the drought pipeline first" % p)

    basins = gpd.read_file(gpkg, layer="basins")
    summ = pd.read_csv(summ_csv)
    events = pd.read_csv(ev_csv, parse_dates=["start", "end", "peak_date"])

    yr = summ[summ.year == args.year].set_index("bidx")
    g = basins.merge(yr.reset_index(), on="bidx", how="left")
    for m, _ in METRICS:
        g[m] = pd.to_numeric(g[m], errors="coerce").fillna(0)

    # events overlapping the year, so the map can show what was running and when
    ev = events[(events.end >= "%d-01-01" % args.year)
                & (events.start <= "%d-12-31" % args.year)].copy()
    ev_by_basin = {}
    for b, grp in ev.groupby("bidx"):
        ev_by_basin[int(b)] = [
            {"start": r.start.strftime("%Y-%m-%d"), "end": r.end.strftime("%Y-%m-%d"),
             "days": int(r.duration_days), "peak": r.peak_date.strftime("%Y-%m-%d"),
             "deficit_mm": round(float(r.deficit_mm), 1),
             "severity": str(getattr(r, "severity", ""))}
            for r in grp.sort_values("start").itertuples()]

    keep = ["bidx", "HYBAS_ID", "SUB_AREA", "frac_br"] + [m for m, _ in METRICS]
    g = g[[c for c in keep if c in g.columns] + ["geometry"]].copy()
    g["HYBAS_ID"] = g["HYBAS_ID"].astype("int64").astype(str)
    g["bidx"] = g["bidx"].astype(int)
    g["SUB_AREA"] = g["SUB_AREA"].round(0)
    g["frac_br"] = g["frac_br"].round(3)
    for m, _ in METRICS:
        g[m] = g[m].round(1)
    g["n_events_2024"] = g["bidx"].map(lambda b: len(ev_by_basin.get(int(b), [])))

    g["geometry"] = g.geometry.simplify(SIMPLIFY_DEG, preserve_topology=True)
    g = g[~g.geometry.is_empty]

    gj_path = os.path.join(OUT_DIR, "basins_%d.geojson" % args.year)
    g.to_file(gj_path, driver="GeoJSON")
    # GeoJSON from GDAL carries full float precision; trimming it roughly halves the file
    gj = json.load(open(gj_path))
    def trim(coords):
        if isinstance(coords[0], (int, float)):
            return [round(coords[0], 4), round(coords[1], 4)]
        return [trim(c) for c in coords]
    for f in gj["features"]:
        f["geometry"]["coordinates"] = trim(f["geometry"]["coordinates"])
        f.pop("id", None)
    json.dump(gj, open(gj_path, "w"), separators=(",", ":"))

    json.dump(ev_by_basin, open(os.path.join(OUT_DIR, "events_%d.json" % args.year), "w"),
              separators=(",", ":"))

    meta = {
        "year": args.year,
        "metrics": [{"key": k, "label": lab,
                     "min": float(g[k].min()), "max": float(g[k].max()),
                     "p95": float(g[k].quantile(0.95))} for k, lab in METRICS],
        "n_basins": int(len(g)),
        "n_basins_in_drought": int((g.n_events > 0).sum()) if "n_events" in g else 0,
        "total_events": int(len(ev)),
    }
    json.dump(meta, open(os.path.join(OUT_DIR, "drought_meta.json"), "w"), indent=1)

    print("basins %d -> %s (%.1f MB)"
          % (len(g), os.path.basename(gj_path), os.path.getsize(gj_path) / 1e6))
    print("events overlapping %d: %d across %d basins"
          % (args.year, len(ev), len(ev_by_basin)))
    for m, lab in METRICS:
        print("  %-22s min %8.1f  median %8.1f  max %8.1f"
              % (lab, g[m].min(), g[m].median(), g[m].max()))


if __name__ == "__main__":
    main()
