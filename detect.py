"""Detect drought events per HydroBASINS level-6 basin, 2019-2025.

Two passes, matching the two questions asked of the data.

Weekly screen - which week-long periods of the year were anomalously dry?
    Each basin-week total of 2019-2025 is ranked against the 21 totals the same week of
    the year produced in 1990-2010. Output carries the empirical percentile, the z-score
    and a flag for weeks at or below the baseline 20th percentile.

Daily engine - how long did each drought actually last?
    A basin-day is in drought when its 30-day accumulated rainfall P30 falls below the
    day-of-year 20th-percentile threshold from climatology.py. Because the threshold
    tracks the seasonal cycle, a normal dry season is not a drought; a wet season that
    fails is. Days whose threshold is below MIN_THR_MM are climatologically rainless and
    are marked not assessable rather than in drought.

    Runs of drought days are then pooled: two runs separated by a gap of POOL_GAP_DAYS
    or fewer are merged when the surplus rainfall in that gap is smaller than POOL_RATIO
    of the deficit accumulated over the preceding POOL_LOOKBACK days of the earlier run.
    The gap window is set to the length of the accumulation itself, because any burst of
    rain big enough to lift P30 over the threshold keeps it there for about 30 days by
    construction; the surplus-ratio test, not the gap length, is what separates a real
    drought-breaking rain from a brief interruption. That is what makes a dry spell punctuated by a
    few wet days count as one long drought instead of several short ones. Pooled events
    shorter than MIN_DURATION days are dropped as noise.

Outputs
    output/weekly_anomalies.csv            basin x week x year, 2019-2025
    output/drought_events.csv              one row per pooled drought event
    output/drought_summary_by_basin_year.csv   one row per basin-year

Run in the ESRI `crop` env:
    <crop-python> detect.py
"""
import datetime as dt
import os

import numpy as np
import pandas as pd

import config as cfg
import series as S


def weekly_screen(dates, precip, weekly_totals):
    """Rank every event-period basin-week against the baseline distribution."""
    year = np.array([d.year for d in dates])
    pdoy = S.pseudo_doy(dates)
    week = S.week_of_year(pdoy)
    nb = precip.shape[1]

    base_mean = np.nanmean(weekly_totals, axis=0)             # (52, nb)
    base_sd = np.nanstd(weekly_totals, axis=0, ddof=1)
    base_p20 = np.nanpercentile(weekly_totals, 20, axis=0)
    nbase = weekly_totals.shape[0]

    rows = []
    for y in cfg.EVENT_YEARS:
        for w in range(1, 53):
            m = (year == y) & (week == w)
            if not m.any():
                continue
            tot = np.nansum(precip[m], axis=0)
            base = weekly_totals[:, w - 1, :]                 # (nbase, nb)
            pctl = 100.0 * (base < tot[None, :]).sum(axis=0) / nbase
            with np.errstate(invalid="ignore", divide="ignore"):
                z = np.where(base_sd[w - 1] > 0,
                             (tot - base_mean[w - 1]) / base_sd[w - 1], np.nan)
            rows.append(pd.DataFrame({
                "bidx": np.arange(nb),
                "year": y,
                "week": w,
                "doy_start": (w - 1) * 7 + 1,
                "n_days": int(m.sum()),
                "precip_mm": tot,
                "baseline_mean_mm": base_mean[w - 1],
                "baseline_p20_mm": base_p20[w - 1],
                "anom_mm": tot - base_mean[w - 1],
                "pctile": pctl,
                "z": z,
                "dry_week": (tot <= base_p20[w - 1]) & (base_mean[w - 1] >= 1.0),
            }))
    return pd.concat(rows, ignore_index=True)


def runs_of_true(flag):
    """Start and end indices (inclusive) of each True run in a 1-D boolean array."""
    if not flag.any():
        return []
    d = np.diff(flag.astype("int8"))
    starts = list(np.where(d == 1)[0] + 1)
    ends = list(np.where(d == -1)[0])
    if flag[0]:
        starts.insert(0, 0)
    if flag[-1]:
        ends.append(len(flag) - 1)
    return list(zip(starts, ends))


def pool_runs(runs, deficit, surplus):
    """Merge runs split by short, weakly-wet gaps.

    deficit[t] and surplus[t] are the daily-equivalent mm by which P30 sits below or
    above the threshold. Two runs merge when the gap is short and the rain that fell in
    it recovered less than POOL_RATIO of the deficit built up over the preceding
    POOL_LOOKBACK days, so one long event cannot swallow arbitrarily wet gaps.
    """
    if not runs:
        return []
    merged = [list(runs[0])]
    for s, e in runs[1:]:
        ps, pe = merged[-1]
        gap = s - pe - 1
        if gap <= cfg.POOL_GAP_DAYS:
            prior = deficit[max(ps, pe - cfg.POOL_LOOKBACK + 1):pe + 1].sum()
            gap_surplus = surplus[pe + 1:s].sum()
            if prior > 0 and gap_surplus < cfg.POOL_RATIO * prior:
                merged[-1][1] = e
                continue
        merged.append([s, e])
    return merged


def detect_events(dates, p30, thr20, thr10, assessable):
    """Pooled drought events for every basin over the whole event series."""
    pdoy = S.pseudo_doy(dates)
    di = pdoy - 1
    thr_d = thr20[di]                       # (nday, nbasin) threshold for that calendar day
    thr_s = thr10[di]
    ass_d = assessable[di]

    gap = thr_d - p30                       # mm per 30 days, positive = short of normal
    daily = gap / float(cfg.ACCUM)          # daily-equivalent mm
    in_dr = np.isfinite(p30) & ass_d & (gap > 0)
    severe = in_dr & (p30 < thr_s)

    deficit = np.where(in_dr, np.maximum(daily, 0.0), 0.0)
    surplus = np.where(np.isfinite(daily), np.maximum(-daily, 0.0), 0.0)

    with np.errstate(invalid="ignore", divide="ignore"):
        rel = np.where(thr_d > 0, gap / thr_d, np.nan)

    recs = []
    nb = p30.shape[1]
    for b in range(nb):
        runs = pool_runs(runs_of_true(in_dr[:, b]), deficit[:, b], surplus[:, b])
        for s, e in runs:
            dur = e - s + 1
            if dur < cfg.MIN_DURATION:
                continue
            sl = slice(s, e + 1)
            defc = deficit[sl, b]
            intens = rel[sl, b]
            peak = s + int(np.nanargmax(np.where(np.isfinite(intens), intens, -np.inf)))
            recs.append({
                "bidx": b,
                "start": dates[s],
                "end": dates[e],
                "duration_days": dur,
                "start_year": dates[s].year,
                "end_year": dates[e].year,
                "crosses_new_year": dates[s].year != dates[e].year,
                "deficit_mm": float(defc.sum()),
                "mean_intensity": float(np.nanmean(intens)),
                "max_intensity": float(np.nanmax(intens)),
                "peak_date": dates[peak],
                "severe_days": int(severe[sl, b].sum()),
                "wet_days_absorbed": int((~in_dr[sl, b]).sum()),
                "min_p30_mm": float(np.nanmin(p30[sl, b])),
            })
    return pd.DataFrame(recs), in_dr, ass_d


def summarize(events, in_dr, ass_d, dates):
    """Per basin-year totals. Days are attributed to the calendar year they fall in."""
    year = np.array([d.year for d in dates])
    nb = in_dr.shape[1]
    rows = []
    for y in cfg.EVENT_YEARS:
        m = year == y
        ev = events[events["start_year"] == y]
        n_ev = ev.groupby("bidx").size().reindex(range(nb), fill_value=0).to_numpy()
        longest = ev.groupby("bidx")["duration_days"].max().reindex(range(nb)).fillna(0).to_numpy()
        deficit = ev.groupby("bidx")["deficit_mm"].sum().reindex(range(nb)).fillna(0).to_numpy()
        maxint = ev.groupby("bidx")["max_intensity"].max().reindex(range(nb)).to_numpy()
        rows.append(pd.DataFrame({
            "bidx": np.arange(nb),
            "year": y,
            "n_events": n_ev,
            "longest_event_days": longest.astype("int32"),
            "total_drought_days": in_dr[m].sum(axis=0).astype("int32"),
            "total_deficit_mm": deficit,
            "max_intensity": maxint,
            "assessable_days": ass_d[m].sum(axis=0).astype("int32"),
        }))
    return pd.concat(rows, ignore_index=True)


def main():
    years = [2018] + list(cfg.EVENT_YEARS)
    dates, precip = S.load_daily(years)
    z = np.load(cfg.THRESHOLDS)
    thr20, thr10, assessable = z["thr20"], z["thr10"], z["assessable"]

    p30 = S.rolling_sum(precip, cfg.ACCUM)

    keep = np.array([d >= dt.date(2019, 1, 1) for d in dates])
    dates_e = [d for d, k in zip(dates, keep) if k]
    p30_e, precip_e = p30[keep], precip[keep]
    print("event series: %d days x %d basins (%s .. %s)"
          % (len(dates_e), precip.shape[1], dates_e[0], dates_e[-1]))

    wk = weekly_screen(dates_e, precip_e, z["weekly_totals"])
    wk.to_csv(os.path.join(cfg.OUT_DIR, "weekly_anomalies.csv"), index=False)
    print("wrote weekly_anomalies.csv (%d rows, %.1f%% flagged dry)"
          % (len(wk), 100 * wk["dry_week"].mean()))

    events, in_dr, ass_d = detect_events(dates_e, p30_e, thr20, thr10, assessable)
    events = events.sort_values(["bidx", "start"]).reset_index(drop=True)
    events.to_csv(os.path.join(cfg.OUT_DIR, "drought_events.csv"), index=False)
    print("wrote drought_events.csv (%d events, %d basins affected)"
          % (len(events), events["bidx"].nunique()))
    print("  duration days: median %.0f  p90 %.0f  max %.0f"
          % (events["duration_days"].median(),
             events["duration_days"].quantile(0.9),
             events["duration_days"].max()))
    print("  events per year:")
    print(events.groupby("start_year").size().to_string())

    summ = summarize(events, in_dr, ass_d, dates_e)
    summ.to_csv(os.path.join(cfg.OUT_DIR, "drought_summary_by_basin_year.csv"), index=False)
    print("wrote drought_summary_by_basin_year.csv (%d rows)" % len(summ))

    np.savez_compressed(os.path.join(cfg.DATA_DIR, "detection_state.npz"),
                        dates=np.array([int(d.strftime("%Y%m%d")) for d in dates_e], "int32"),
                        p30=p30_e.astype("float32"), in_drought=in_dr,
                        assessable=ass_d, precip=precip_e.astype("float32"))
    print("wrote data/detection_state.npz (daily traces for the dashboard)")


if __name__ == "__main__":
    main()
