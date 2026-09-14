"""Build the 1990-2010 CHIRPS baseline: weekly, monthly and day-of-year thresholds.

Three products come out of the baseline period, all per HydroBASINS level-6 basin:

1. `output/weekly_climatology.csv`  - the 52 seven-day bins of the year (the last bin
   carries 8 days), with the mean, sd and 10th/20th/50th percentile of the weekly
   rainfall total across the 21 baseline years.
2. `output/monthly_climatology.csv` - the same for calendar months.
3. `data/thresholds.npz`            - the variable drought threshold used day by day:
   for each of the 365 day-of-year slots, the 20th (moderate) and 10th (severe)
   percentile of the 30-day accumulated rainfall P30, pooled over +/- 15 days and the
   21 baseline years (~630 samples per slot), then smoothed with a 31-day circular
   moving average.

Because the threshold varies with day of year, a normal dry season sits at its own
threshold and is not flagged; only rainfall that is low for that time of year is.

Run in the ESRI `crop` env:
    <crop-python> climatology.py
"""
import os

import numpy as np
import pandas as pd

import config as cfg
import series as S


def bin_climatology(precip, labels, year, nbin, name, extra):
    """Per-basin distribution of the rainfall total in each calendar bin."""
    nb = precip.shape[1]
    yrs = list(cfg.BASELINE_YEARS)
    totals = np.full((len(yrs), nbin, nb), np.nan, "float32")
    for iy, y in enumerate(yrs):
        for b in range(1, nbin + 1):
            m = (year == y) & (labels == b)
            if m.any():
                totals[iy, b - 1] = np.nansum(precip[m], axis=0)
    rows = []
    for b in range(nbin):
        v = totals[:, b, :]
        cols = {
            "bidx": np.arange(nb),
            name: b + 1,
            "mean_mm": np.nanmean(v, axis=0),
            "sd_mm": np.nanstd(v, axis=0, ddof=1),
            "p10_mm": np.nanpercentile(v, 10, axis=0),
            "p20_mm": np.nanpercentile(v, 20, axis=0),
            "p50_mm": np.nanpercentile(v, 50, axis=0),
            "n_years": np.isfinite(v).sum(axis=0),
        }
        for k, fn in extra.items():
            cols[k] = fn(b)
        rows.append(pd.DataFrame(cols))
    df = pd.concat(rows, ignore_index=True).sort_values(["bidx", name])
    return totals, df


def main():
    years = [1989] + list(cfg.BASELINE_YEARS)
    dates, precip = S.load_daily(years)
    year = np.array([d.year for d in dates])
    month = np.array([d.month for d in dates])
    pdoy = S.pseudo_doy(dates)
    week = S.week_of_year(pdoy)
    nb = precip.shape[1]
    print("baseline series: %d days x %d basins (%s .. %s)"
          % (len(dates), nb, dates[0], dates[-1]))

    inb = (year >= 1990) & (year <= 2010)

    wk_tot, wk_df = bin_climatology(
        precip[inb], week[inb], year[inb], 52, "week",
        {"doy_start": lambda b: b * 7 + 1,
         "doy_end": lambda b: 365 if b == 51 else b * 7 + 7})
    wk_df.to_csv(os.path.join(cfg.OUT_DIR, "weekly_climatology.csv"), index=False)
    print("wrote weekly_climatology.csv (%d rows)" % len(wk_df))

    mo_tot, mo_df = bin_climatology(precip[inb], month[inb], year[inb], 12, "month", {})
    mo_df.to_csv(os.path.join(cfg.OUT_DIR, "monthly_climatology.csv"), index=False)
    print("wrote monthly_climatology.csv (%d rows)" % len(mo_df))

    # ---- day-of-year P30 thresholds -----------------------------------------
    p30 = S.rolling_sum(precip, cfg.ACCUM)
    use = inb & np.isfinite(p30).any(axis=1)
    p30b, doyb = p30[use], pdoy[use]
    print("P30 samples in baseline: %d days" % use.sum())

    thr20 = np.full((365, nb), np.nan, "float32")
    thr10 = np.full((365, nb), np.nan, "float32")
    p50 = np.full((365, nb), np.nan, "float32")
    for d in range(1, 366):
        off = (doyb - d + 182) % 365 - 182            # circular distance in days
        m = np.abs(off) <= cfg.DOY_WINDOW
        v = p30b[m]
        thr20[d - 1] = np.nanpercentile(v, cfg.THR_PCTL, axis=0)
        thr10[d - 1] = np.nanpercentile(v, cfg.THR_PCTL_SEVERE, axis=0)
        p50[d - 1] = np.nanpercentile(v, 50, axis=0)

    thr20 = S.circular_smooth(thr20, cfg.THR_SMOOTH).astype("float32")
    thr10 = S.circular_smooth(thr10, cfg.THR_SMOOTH).astype("float32")
    p50 = S.circular_smooth(p50, cfg.THR_SMOOTH).astype("float32")

    assessable = thr20 >= cfg.MIN_THR_MM
    print("threshold mm/30d: median %.1f  range %.1f - %.1f"
          % (np.nanmedian(thr20), np.nanmin(thr20), np.nanmax(thr20)))
    print("assessable basin-days: %.1f%% (rest are normal dry season, thr < %.0f mm/30d)"
          % (100 * assessable.mean(), cfg.MIN_THR_MM))
    nass = assessable.sum(axis=0)
    print("basins assessable all year: %d / %d   never: %d"
          % (int((nass == 365).sum()), nb, int((nass == 0).sum())))

    np.savez_compressed(cfg.THRESHOLDS, thr20=thr20, thr10=thr10, p50=p50,
                        assessable=assessable, weekly_totals=wk_tot,
                        monthly_totals=mo_tot,
                        baseline_years=np.array(list(cfg.BASELINE_YEARS)))
    print("wrote %s" % cfg.THRESHOLDS)


if __name__ == "__main__":
    main()
