"""Choose one demo basin per drought metric.

Constraints, in order of priority:

1. The crop labels must come from the SAME year as the drought being shown. The labels
   turned out to exist in exactly one year per basin (rice 2019/2020, sugarcane 2024), so
   without this constraint the demo would show a 2022 drought over fields labelled in
   2024 and rely on crop type persisting - an assumption worth avoiding when it can be
   designed out.
2. Field boundaries must exist on F: for that state and year.
3. At least MIN_LABELS labelled fields, so the AOI has enough tagged fields to be worth
   animating. This is a demo-density floor, not a scientific one: it means each metric
   gets the highest-ranked basin ABOVE the floor, not the highest-ranked basin outright.
4. Four distinct basins, two rice and two sugarcane.

    <crop-python> pick_demo_basins.py
"""
import os
import unicodedata

import geopandas as gpd
import numpy as np
import pandas as pd

import config as cfg

METRICS = ["total_drought_days", "longest_event_days", "n_events", "total_deficit_mm"]
CROPS = ["rice", "sugarcane"]
FIELD_DIR = r"F:\Trazo Fields v2\field boundaries"
MIN_LABELS = 100


def norm(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return s.replace(" ", "_")


def boundary_index():
    out = {}
    for fn in os.listdir(FIELD_DIR):
        if fn.startswith("Brazil_") and fn.endswith(".gpkg"):
            st, yr = fn[len("Brazil_"):-len(".gpkg")].rsplit("_", 1)
            out.setdefault(st, set()).add(int(yr))
    return out


def candidates():
    lab = gpd.read_parquet(
        os.path.join(cfg.OUT_DIR, "crop_exposure", "labels_drought_exposure.parquet"))
    lab = pd.DataFrame(lab.drop(columns="geometry"))
    lab = lab[lab["sub"].isin(CROPS) & lab["bidx"].notna()]
    lab["bidx"] = lab["bidx"].astype(int)

    per = lab.groupby(["bidx", "sub", "crop_year", "state"]).size().reset_index(name="n_labels")
    per["year"] = per["crop_year"].astype(int)

    summ = pd.read_csv(os.path.join(cfg.OUT_DIR, "drought_summary_by_basin_year.csv"))
    basins = gpd.read_file(cfg.BASINS_GPKG, layer="basins")
    c = per.merge(summ, on=["bidx", "year"]).merge(
        basins[["bidx", "HYBAS_ID", "SUB_AREA"]], on="bidx")

    fs = boundary_index()
    c["state_file"] = c["state"].map(norm)
    c = c[[(s in fs) and (y in fs[s]) for s, y in zip(c.state_file, c.year)]]
    return c


def main():
    c = candidates()
    print("basin-years with label-year == drought-year and boundaries on disk: %d" % len(c))
    ok = c[c.n_labels >= MIN_LABELS].copy()
    print("of those, with >= %d labels: %d" % (MIN_LABELS, len(ok)))
    print("\nlabel years present: %s"
          % c.groupby("sub")["year"].apply(lambda s: sorted(s.unique())).to_dict())

    # two metrics to rice, two to sugarcane; greedy on the metric, basins must be distinct
    # Rice takes the two duration/frequency metrics because the rice basins carry both
    # the longest single event and the most separate events among eligible candidates;
    # sugarcane takes the two volume metrics. Assigning it the other way round left the
    # "number of events" button showing a basin with only 2 events.
    plan = {"rice": ["longest_event_days", "n_events"],
            "sugarcane": ["total_drought_days", "total_deficit_mm"]}
    used, chosen = set(), {}
    for crop, metrics in plan.items():
        for m in metrics:
            pool = ok[(ok["sub"] == crop) & (~ok.HYBAS_ID.isin(used))]
            if pool.empty:
                print("no basin left for %s / %s" % (crop, m))
                continue
            row = pool.nlargest(1, m).iloc[0]
            used.add(row.HYBAS_ID)
            chosen[m] = row

    print("\n%-20s %-11s %-18s %4s %6s %6s %s" %
          ("metric", "HYBAS", "state", "year", "labels", "value", "crop"))
    for m in METRICS:
        if m not in chosen:
            continue
        r = chosen[m]
        print("%-20s %-11d %-18s %4d %6d %6.0f %s"
              % (m, r.HYBAS_ID, r.state, r.year, r.n_labels, r[m], r["sub"]))
        print("     n_events %d | longest %d d | drought days %d | deficit %.0f mm"
              % (r.n_events, r.longest_event_days, r.total_drought_days, r.total_deficit_mm))

    print("\nRUNNER-UP POOL (top 5 per metric among eligible)")
    for m in METRICS:
        print("\n-- %s" % m)
        print(ok.nlargest(5, m)[["HYBAS_ID", "state", "year", "sub", "n_labels", m]]
              .to_string(index=False, float_format=lambda v: "%.0f" % v))


if __name__ == "__main__":
    main()
