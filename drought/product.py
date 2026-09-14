"""Assemble the basin-level drought data product.

Joins the detection tables onto the HydroBASINS level-6 polygons and writes one
GeoPackage with everything a GIS user needs, plus GeoParquet copies for the
cloud-native catalogue.

Layers in `output/brazil_drought_hydrobasins_lev06_2019_2025.gpkg`
    basins      one row per basin, 2019-2025 totals (events, drought days, worst year)
    basin_year  one row per basin-year, geometry repeated
    events      one row per drought event, geometry repeated

Run in the ESRI `crop` env:
    <crop-python> product.py
"""
import os

import geopandas as gpd
import numpy as np
import pandas as pd

import config as cfg

DUR_CLASS = [(15, 29, "short 15-29 d"), (30, 59, "moderate 30-59 d"),
             (60, 119, "long 60-119 d"), (120, 10 ** 6, "extreme 120+ d")]


def duration_class(days):
    for lo, hi, name in DUR_CLASS:
        if lo <= days <= hi:
            return name
    return "sub-threshold"


def main():
    basins = gpd.read_file(cfg.BASINS_GPKG, layer="basins")
    events = pd.read_csv(os.path.join(cfg.OUT_DIR, "drought_events.csv"),
                         parse_dates=["start", "end", "peak_date"])
    summ = pd.read_csv(os.path.join(cfg.OUT_DIR, "drought_summary_by_basin_year.csv"))

    events["duration_class"] = events["duration_days"].map(duration_class)
    # Severity on one axis: the share of the event's days that were below the 10th
    # percentile of the baseline, not the relative shortfall. Relative shortfall makes a
    # year-long Amazon drought look "mild" purely because a wet basin's threshold is
    # large, which is the wrong reading.
    sev_share = events["severe_days"] / events["duration_days"]
    events["severe_day_share"] = sev_share.round(3)
    events["severity"] = np.where(sev_share >= 0.50, "severe",
                                  np.where(sev_share >= 0.20, "moderate", "mild"))

    # ---- per basin, whole period -------------------------------------------
    g = summ.groupby("bidx")
    per_basin = pd.DataFrame({
        "n_events_total": g["n_events"].sum(),
        "drought_days_total": g["total_drought_days"].sum(),
        "deficit_mm_total": g["total_deficit_mm"].sum(),
        "years_with_drought": g["n_events"].apply(lambda s: int((s > 0).sum())),
        "max_event_days": g["longest_event_days"].max(),
    })
    worst = summ.loc[summ.groupby("bidx")["total_drought_days"].idxmax(),
                     ["bidx", "year", "total_drought_days"]]
    per_basin = per_basin.join(worst.set_index("bidx").rename(
        columns={"year": "worst_year", "total_drought_days": "worst_year_days"}))
    per_basin["mean_drought_days_per_year"] = (
        per_basin["drought_days_total"] / len(list(cfg.EVENT_YEARS)))

    basins_out = basins.merge(per_basin.reset_index(), on="bidx", how="left").fillna(
        {"n_events_total": 0, "drought_days_total": 0, "deficit_mm_total": 0.0,
         "years_with_drought": 0, "max_event_days": 0})

    geom = basins[["bidx", "geometry"]]
    basin_year = geom.merge(summ, on="bidx", how="right")
    events_geo = geom.merge(events, on="bidx", how="right")

    if os.path.exists(cfg.PRODUCT_GPKG):
        os.remove(cfg.PRODUCT_GPKG)
    basins_out.to_file(cfg.PRODUCT_GPKG, layer="basins", driver="GPKG")
    basin_year.to_file(cfg.PRODUCT_GPKG, layer="basin_year", driver="GPKG")
    events_geo.to_file(cfg.PRODUCT_GPKG, layer="events", driver="GPKG")
    print("wrote %s" % cfg.PRODUCT_GPKG)

    # GeoParquet carries geometry once, on the basins layer. Repeating a 7 kB polygon on
    # every basin-year row costs 53 MB for 7504 rows of numbers; the year table is a plain
    # Parquet attribute table that joins back on `bidx` (or HYBAS_ID).
    pq = os.path.join(cfg.OUT_DIR, "parquet")
    os.makedirs(pq, exist_ok=True)
    basins_out.to_parquet(os.path.join(pq, "basins.parquet"), index=False)
    events_geo.to_parquet(os.path.join(pq, "events.parquet"), index=False)
    summ_flat = summ.merge(basins[["bidx", "HYBAS_ID"]], on="bidx")
    summ_flat.to_parquet(os.path.join(pq, "basin_year.parquet"), index=False)
    events.to_csv(os.path.join(cfg.OUT_DIR, "drought_events.csv"), index=False)
    print("wrote GeoParquet to %s" % pq)

    # ---- headline numbers ---------------------------------------------------
    print("\nevents by year and duration class")
    print(pd.crosstab(events["start_year"], events["duration_class"]).to_string())
    print("\nbasins in drought at some point each year")
    print(summ[summ.n_events > 0].groupby("year")["bidx"].nunique().to_string())
    print("\nlongest 10 events")
    top = events.nlargest(10, "duration_days").merge(
        basins[["bidx", "HYBAS_ID"]], on="bidx")
    print(top[["HYBAS_ID", "start", "end", "duration_days", "deficit_mm",
               "severity"]].to_string(index=False))


if __name__ == "__main__":
    main()
