"""Loading and calendar helpers shared by the climatology and detection stages."""
import datetime as dt
import os

import numpy as np

import config as cfg


def to_date(yyyymmdd):
    s = int(yyyymmdd)
    return dt.date(s // 10000, (s // 100) % 100, s % 100)


def load_daily(years):
    """Concatenate the per-year basin matrices into one chronological series.

    Returns (dates, precip) where dates is a list of datetime.date and precip is
    (nday, nbasin) float32 in mm/day.
    """
    parts = []
    for y in years:
        path = cfg.daily_npz(y)
        if not os.path.exists(path):
            raise SystemExit("missing %s - run download_chirps_daily.py first" % path)
        z = np.load(path)
        parts.append((z["dates"], z["precip"]))
    dates = np.concatenate([p[0] for p in parts])
    precip = np.concatenate([p[1] for p in parts], axis=0)
    order = np.argsort(dates)
    dates, precip = dates[order], precip[order]
    return [to_date(d) for d in dates], precip


def pseudo_doy(dates):
    """Day of year on a fixed 365-day calendar; 29 Feb shares the 28 Feb slot (59).

    Keeps a day-of-year climatology aligned across leap and non-leap years, which a raw
    doy does not: without the shift, 1 March is doy 60 in common years and 61 in leap
    years, so the two would land in different day-of-year bins.
    """
    out = np.empty(len(dates), "int16")
    for i, d in enumerate(dates):
        doy = d.timetuple().tm_yday
        leap = (d.year % 4 == 0 and d.year % 100 != 0) or d.year % 400 == 0
        if leap and doy >= 60:
            doy -= 1
        out[i] = doy
    return out


def week_of_year(pdoy):
    """52 seven-day bins on the 365-day calendar; the last bin carries 8 days."""
    return np.minimum((pdoy - 1) // 7 + 1, 52).astype("int16")


def rolling_sum(precip, window):
    """Running `window`-day total, NaN for the first window-1 rows.

    A day whose window contains any missing input stays NaN rather than silently
    under-counting the accumulation.
    """
    filled = np.nan_to_num(precip, nan=0.0)
    bad = ~np.isfinite(precip)
    csum = np.cumsum(filled.astype("float64"), axis=0)
    cbad = np.cumsum(bad.astype("int32"), axis=0)
    out = np.full(precip.shape, np.nan, "float32")
    tot = csum[window - 1:].copy()
    tot[1:] -= csum[:-window]
    nbad = cbad[window - 1:].copy()
    nbad[1:] -= cbad[:-window]
    tot[nbad > 0] = np.nan
    out[window - 1:] = tot.astype("float32")
    return out


def circular_smooth(arr, window):
    """Moving average down axis 0 with wrap-around (axis 0 is day-of-year)."""
    n = arr.shape[0]
    half = window // 2
    padded = np.concatenate([arr[n - half:], arr, arr[:half]], axis=0)
    kern = np.ones(window) / window
    out = np.empty_like(arr)
    for j in range(arr.shape[1]):
        out[:, j] = np.convolve(padded[:, j], kern, mode="valid")[:n]
    return out
