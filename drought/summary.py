"""Print the headline answers the analysis was built to give.

    <crop-python> summary.py
"""
import os

import geopandas as gpd
import numpy as np
import pandas as pd

import config as cfg

REGIONS = [
    ("Amazon NW (Negro/Solimoes)", -74, -6, -58, 3),
    ("Amazon E (Tapajos/Xingu/Para)", -58, -10, -46, 0),
    ("Northeast (Caatinga)", -45, -16, -34, -2),
    ("Cerrado / Sao Francisco", -50, -20, -40, -8),
    ("Pantanal / upper Paraguay", -60, -22, -54, -14),
    ("SE / Parana headwaters", -54, -25, -42, -16),
    ("South (RS/SC/PR)", -58, -34, -48, -25),
]


def region_of(lon, lat):
    for name, x0, y0, x1, y1 in REGIONS:
        if x0 <= lon <= x1 and y0 <= lat <= y1:
            return name
    return "other"


def main():
    bas = gpd.read_file(cfg.BASINS_GPKG, layer="basins")
    c = bas.geometry.representative_point()
    bas["lon"], bas["lat"] = c.x, c.y
    bas["region"] = [region_of(x, y) for x, y in zip(bas.lon, bas.lat)]

    ev = pd.read_csv(os.path.join(cfg.OUT_DIR, "drought_events.csv"),
                     parse_dates=["start", "end"]).merge(
        bas[["bidx", "region", "SUB_AREA", "HYBAS_ID"]], on="bidx")
    summ = pd.read_csv(os.path.join(cfg.OUT_DIR, "drought_summary_by_basin_year.csv")).merge(
        bas[["bidx", "region", "SUB_AREA"]], on="bidx")
    wk = pd.read_csv(os.path.join(cfg.OUT_DIR, "weekly_anomalies.csv"))

    nb = len(bas)
    print("=" * 78)
    print("BRAZIL DROUGHT 2019-2025 : %d HydroBASINS level-6 basins" % nb)
    print("=" * 78)

    print("\n1. NATIONAL, PER YEAR")
    print("   year  basins_hit  events  mean_events  mean_drought_days  extreme(120d+)")
    for y in cfg.EVENT_YEARS:
        s = summ[summ.year == y]
        e = ev[ev.start_year == y]
        w = (s.total_drought_days * s.SUB_AREA).sum() / s.SUB_AREA.sum()
        print("   %d     %4d      %5d      %4.2f            %5.1f            %4d"
              % (y, int((s.n_events > 0).sum()), len(e),
                 s.n_events.mean(), w, int((e.duration_days >= 120).sum())))

    print("\n2. AREA-WEIGHTED DROUGHT DAYS PER YEAR, BY REGION")
    piv = summ.groupby(["region", "year"]).apply(
        lambda d: (d.total_drought_days * d.SUB_AREA).sum() / d.SUB_AREA.sum(),
        include_groups=False).unstack()
    print(piv.round(0).astype(int).to_string())

    print("\n3. HOW MANY DROUGHTS DID A BASIN HAVE IN A YEAR?")
    dist = summ.n_events.value_counts().sort_index()
    tot = dist.sum()
    for k, v in dist.items():
        print("   %d event(s): %5d basin-years  (%4.1f%%)" % (k, v, 100 * v / tot))
    print("   basin-years with 2 or more separate droughts: %.1f%%"
          % (100 * summ[summ.n_events >= 2].shape[0] / tot))

    print("\n4. EVENT DURATION")
    q = ev.duration_days.quantile([.25, .5, .75, .9, .99])
    print("   p25 %.0f  median %.0f  p75 %.0f  p90 %.0f  p99 %.0f  max %d days"
          % (q[.25], q[.5], q[.75], q[.9], q[.99], ev.duration_days.max()))
    print("   events pooled across a wet interruption: %.1f%% (median 6 wet days absorbed)"
          % (100 * (ev.wet_days_absorbed > 0).mean()))

    print("\n5. LONGEST 12 EVENTS")
    top = ev.nlargest(12, "duration_days")
    print("   %-30s %-11s %-11s %5s %8s %9s" %
          ("region", "start", "end", "days", "deficit", "severity"))
    for _, r in top.iterrows():
        print("   %-30s %s %s %5d %7.0fmm %9s"
              % (r.region[:30], r.start.date(), r.end.date(), r.duration_days,
                 r.deficit_mm, r.severity))

    print("\n6. WHICH WEEKS OF THE YEAR WERE ANOMALOUSLY DRY MOST OFTEN")
    print("   (share of basin-weeks at or below the baseline 20th percentile;")
    print("    20% is the by-construction expectation, so higher = drier than baseline)")
    byw = wk.groupby("week").dry_week.mean() * 100
    for y in cfg.EVENT_YEARS:
        s = wk[wk.year == y].groupby("week").dry_week.mean() * 100
        peak = s.nlargest(3)
        print("   %d  overall %4.1f%%   driest weeks: %s"
              % (y, wk[wk.year == y].dry_week.mean() * 100,
                 ", ".join("w%02d (%.0f%%)" % (i, v) for i, v in peak.items())))
    print("   across all years, driest weeks of the calendar: %s"
          % ", ".join("w%02d %.0f%%" % (i, v) for i, v in byw.nlargest(5).items()))

    print("\n7. WORST SINGLE BASIN-YEARS (drought days)")
    w = summ.nlargest(8, "total_drought_days").merge(bas[["bidx", "HYBAS_ID"]], on="bidx")
    for _, r in w.iterrows():
        print("   HYBAS %d  %d  %d days in %d event(s)  [%s]"
              % (r.HYBAS_ID, r.year, r.total_drought_days, r.n_events, r.region))


if __name__ == "__main__":
    main()
