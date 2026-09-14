"""Place the crop-classification training labels inside the drought basins.

Answers: does the v9 crop-classification training data sit in basins that saw drought
2019-2025, and how exposed is it? Time is deliberately ignored here - every field is
scored against its basin's whole-period drought statistics, not against the drought that
was running in its own crop year. (Matching label year to drought year is the obvious
next step; this is the spatial question only.)

"Drought zone" needs care: 1066 of 1072 basins had at least one event in seven years, so
"was there ever a drought here" is not a discriminating question. Exposure is therefore
graded by how much drought a basin carried, using quartiles of mean drought days per year
across all 1072 basins, plus a separate flag for basins that saw an extreme event.

Fields are joined on their representative point, so a field that straddles a basin
boundary is counted once, in the basin holding most of it.

Outputs (output/crop_exposure/)
    labels_drought_exposure.parquet   every label with its basin and drought stats
    exposure_by_class.csv             field counts by coarse class x exposure tier
    exposure_by_state.csv             field counts by state x exposure tier
    exposure_by_basin.csv             per basin: labels held, drought stats

Run in the ESRI `crop` env:
    <crop-python> crop_exposure.py
"""
import os

import geopandas as gpd
import numpy as np
import pandas as pd

import config as cfg

LABELS = (r"C:\Users\grupp\Desktop\crop classification v9 handoff"
          r"\data\labels\ALL_labels.parquet")
OUT_DIR = os.path.join(cfg.OUT_DIR, "crop_exposure")
EXTREME_DAYS = 120          # an event this long is the "extreme" duration class

TIERS = ["low", "moderate", "high", "severe"]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    basins = gpd.read_file(cfg.BASINS_GPKG, layer="basins")
    summ = pd.read_csv(os.path.join(cfg.OUT_DIR, "drought_summary_by_basin_year.csv"))
    events = pd.read_csv(os.path.join(cfg.OUT_DIR, "drought_events.csv"))

    g = summ.groupby("bidx")
    stats = pd.DataFrame({
        "drought_days_total": g["total_drought_days"].sum(),
        "n_events_total": g["n_events"].sum(),
        "max_event_days": g["longest_event_days"].max(),
        "deficit_mm_total": g["total_deficit_mm"].sum(),
        "years_with_drought": g["n_events"].apply(lambda s: int((s > 0).sum())),
    })
    stats["mean_drought_days_per_year"] = (
        stats["drought_days_total"] / len(list(cfg.EVENT_YEARS)))
    worst = summ.loc[summ.groupby("bidx")["total_drought_days"].idxmax(),
                     ["bidx", "year"]].set_index("bidx")["year"]
    stats["worst_year"] = worst
    stats["has_extreme_event"] = (
        events[events.duration_days >= EXTREME_DAYS].groupby("bidx").size()
        .reindex(stats.index).fillna(0) > 0)

    # Exposure tier = quartile of mean drought days per year across all basins.
    q = stats["mean_drought_days_per_year"].quantile([.25, .5, .75]).to_list()
    stats["exposure"] = pd.cut(stats["mean_drought_days_per_year"],
                               [-np.inf] + q + [np.inf], labels=TIERS)
    print("exposure tier cut points (mean drought days/yr): %s"
          % ", ".join("%.0f" % v for v in q))

    bas = basins[["bidx", "HYBAS_ID", "SUB_AREA", "frac_br", "geometry"]].merge(
        stats.reset_index(), on="bidx")

    labels = gpd.read_parquet(LABELS)
    print("labels: %d fields, crs %s" % (len(labels), labels.crs.to_string()))
    if labels.crs != bas.crs:
        labels = labels.to_crs(bas.crs)

    pts = labels.copy()
    pts["geometry"] = labels.geometry.representative_point()
    joined = gpd.sjoin(pts, bas, how="left", predicate="within")
    joined = joined.drop(columns=["index_right"])

    miss = joined["bidx"].isna()
    print("fields matched to a basin: %d / %d  (%d outside the Brazil basin set)"
          % ((~miss).sum(), len(joined), miss.sum()))
    if miss.any():
        print("  unmatched by state: %s"
              % joined.loc[miss, "state"].value_counts().head(6).to_dict())

    out = joined.drop(columns=["geometry"]).copy()
    out["geometry"] = labels.geometry.values
    out = gpd.GeoDataFrame(out, geometry="geometry", crs=labels.crs)
    out.to_parquet(os.path.join(OUT_DIR, "labels_drought_exposure.parquet"), index=False)

    m = joined[~miss].copy()
    m["exposure"] = m["exposure"].astype(str)

    print("\nFIELDS BY EXPOSURE TIER")
    tot = len(joined)
    for t in TIERS:
        n = int((m.exposure == t).sum())
        print("  %-9s %6d fields  (%4.1f%%)" % (t, n, 100 * n / tot))
    n_ext = int(m.has_extreme_event.sum())
    print("  in a basin that saw an event of %d+ days: %d fields (%.1f%%)"
          % (EXTREME_DAYS, n_ext, 100 * n_ext / tot))
    n_any = int((m.n_events_total > 0).sum())
    print("  in a basin with any drought event at all: %d fields (%.1f%%)"
          % (n_any, 100 * n_any / tot))

    print("\nDROUGHT DAYS 2019-2025 OF THE BASINS HOLDING LABELS")
    d = m["drought_days_total"]
    print("  min %d  p25 %.0f  median %.0f  p75 %.0f  max %d  (national median %.0f)"
          % (d.min(), d.quantile(.25), d.median(), d.quantile(.75), d.max(),
             stats["drought_days_total"].median()))

    ct = pd.crosstab(m["coarse"], m["exposure"]).reindex(columns=TIERS, fill_value=0)
    ct["total"] = ct.sum(axis=1)
    ct["pct_high_or_severe"] = (100 * (ct["high"] + ct["severe"]) / ct["total"]).round(1)
    ct.to_csv(os.path.join(OUT_DIR, "exposure_by_class.csv"))
    print("\nBY COARSE CLASS")
    print(ct.to_string())

    if "sub" in m.columns:
        cs = pd.crosstab(m["sub"], m["exposure"]).reindex(columns=TIERS, fill_value=0)
        cs["total"] = cs.sum(axis=1)
        cs["pct_high_or_severe"] = (100 * (cs["high"] + cs["severe"]) / cs["total"]).round(1)
        cs.sort_values("total", ascending=False).to_csv(
            os.path.join(OUT_DIR, "exposure_by_subclass.csv"))
        print("\nBY SUBCLASS (top 12 by count)")
        print(cs.sort_values("total", ascending=False).head(12).to_string())

    st = pd.crosstab(m["state"], m["exposure"]).reindex(columns=TIERS, fill_value=0)
    st["total"] = st.sum(axis=1)
    st["pct_high_or_severe"] = (100 * (st["high"] + st["severe"]) / st["total"]).round(1)
    st = st.sort_values("total", ascending=False)
    st.to_csv(os.path.join(OUT_DIR, "exposure_by_state.csv"))
    print("\nBY STATE (top 15 by count)")
    print(st.head(15).to_string())

    per_basin = m.groupby(["bidx", "HYBAS_ID"]).agg(
        n_labels=("field_uid", "count"),
        drought_days_total=("drought_days_total", "first"),
        n_events_total=("n_events_total", "first"),
        max_event_days=("max_event_days", "first"),
        exposure=("exposure", "first"),
        worst_year=("worst_year", "first")).reset_index()
    per_basin.sort_values("n_labels", ascending=False).to_csv(
        os.path.join(OUT_DIR, "exposure_by_basin.csv"), index=False)
    print("\nlabels span %d basins of %d" % (len(per_basin), len(bas)))
    print("\nTOP 10 BASINS BY LABEL COUNT")
    print(per_basin.nlargest(10, "n_labels")[
        ["HYBAS_ID", "n_labels", "drought_days_total", "max_event_days",
         "exposure", "worst_year"]].to_string(index=False))
    print("\nMOST DROUGHT-EXPOSED BASINS THAT HOLD LABELS (top 10 by drought days)")
    print(per_basin.nlargest(10, "drought_days_total")[
        ["HYBAS_ID", "n_labels", "drought_days_total", "max_event_days",
         "exposure", "worst_year"]].to_string(index=False))
    print("\nwrote %s" % OUT_DIR)


if __name__ == "__main__":
    main()
