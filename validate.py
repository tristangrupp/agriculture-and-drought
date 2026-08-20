"""Check the detected droughts against events that are independently documented.

The method is only trustworthy if it reproduces droughts nobody disputes.

The pass/fail test is on **timing**: during the documented window, at least MIN_SHARE of
the region's basins must be in drought, and the window must be worse than that region's
median month. Testing timing rather than annual totals matters, because a region can have
a documented drought in one year and a still worse one in another - three regions here do
- and an annual ranking would call that a failure when the documented event is in fact
detected, correctly timed. For the same reason the test does not require the documented
window to be the region's worst: being on the record is not a claim to being the maximum.

The annual table is printed alongside as information, not as a test.

    Amazon (Negro/Solimoes)  Aug-Nov 2023  record low water at Manaus, Oct 2023
    SE Brazil (Parana hdw)   Apr-Jun 2021  worst hydrological drought in ~90 years
    Pantanal                 Jun-Sep 2024  record fire season after a long dry spell
    Rio Grande do Sul        Jan-Mar 2022  third consecutive La Nina summer drought
    Northeast (Caatinga)     -             no documented standout; printed only

Run in the ESRI `crop` env:
    <crop-python> validate.py
"""
import os

import geopandas as gpd
import numpy as np
import pandas as pd

import config as cfg

MIN_SHARE = 0.50        # of the region's basins in drought during the window

REGIONS = {
    # name: (minlon, minlat, maxlon, maxlat, (window start, window end) or None)
    "Amazon (Negro/Solimoes)": (-70, -6, -58, 2, ("2023-08", "2023-11")),
    "SE Brazil (Parana headwaters)": (-52, -24, -44, -16, ("2021-04", "2021-06")),
    "Pantanal": (-58, -20, -55, -16, ("2024-06", "2024-09")),
    "Rio Grande do Sul": (-57, -33, -50, -27, ("2022-01", "2022-03")),
    "Northeast (Caatinga)": (-42, -12, -36, -5, None),
}


def monthly_share(events, nb):
    """Share of the region's basins with an active drought event in each month."""
    months = pd.date_range("2019-01-01", "2025-12-01", freq="MS")
    vals = []
    for m in months:
        me = m + pd.offsets.MonthEnd(0)
        vals.append(events[(events.start <= me) & (events.end >= m)].bidx.nunique() / nb)
    return pd.Series(vals, index=months)


def main():
    basins = gpd.read_file(cfg.BASINS_GPKG, layer="basins")
    cent = basins.geometry.representative_point()
    basins["lon"], basins["lat"] = cent.x, cent.y

    events = pd.read_csv(os.path.join(cfg.OUT_DIR, "drought_events.csv"),
                         parse_dates=["start", "end"])
    events = events.merge(basins[["bidx", "lon", "lat"]], on="bidx")
    summ = pd.read_csv(os.path.join(cfg.OUT_DIR, "drought_summary_by_basin_year.csv"))
    summ = summ.merge(basins[["bidx", "lon", "lat", "SUB_AREA"]], on="bidx")

    fails = []
    for name, (x0, y0, x1, y1, win) in REGIONS.items():
        inbox = lambda d: d[(d.lon >= x0) & (d.lon <= x1) & (d.lat >= y0) & (d.lat <= y1)]
        se, ss = inbox(events), inbox(summ)
        if ss.empty:
            print("%s: no basins in box" % name)
            continue
        nb = ss.bidx.nunique()
        share = monthly_share(se, nb)

        by_year = ss.groupby("year").apply(
            lambda d: (d.total_drought_days * d.SUB_AREA).sum() / d.SUB_AREA.sum(),
            include_groups=False)
        print("\n%s  (%d basins)" % (name, nb))
        print("  drought days per basin-year: %s"
              % "  ".join("%d:%3.0f" % (y, v) for y, v in by_year.items()))
        print("  driest months: %s"
              % ", ".join("%s %.0f%%" % (i.strftime("%Y-%m"), v * 100)
                          for i, v in share.nlargest(4).items()))
        if win is None:
            continue

        w = share[(share.index >= win[0]) & (share.index <= win[1] + "-28")]
        med = share.median()
        ok = w.mean() >= MIN_SHARE and w.mean() >= med
        print("  documented window %s..%s: %.0f%% of basins in drought "
              "(need >=%.0f%% and above the median month, %.0f%%) -> %s"
              % (win[0], win[1], 100 * w.mean(), 100 * MIN_SHARE, 100 * med,
                 "PASS" if ok else "FAIL"))
        worst = int(by_year.idxmax())
        if worst != int(win[0][:4]):
            print("  note: %d is detected as the region's worst year, above the "
                  "documented %s" % (worst, win[0][:4]))
        if not ok:
            fails.append(name)

    print()
    if fails:
        print("VALIDATION FAILED for: %s" % ", ".join(fails))
        return 1
    print("all documented droughts reproduced at the right time")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
