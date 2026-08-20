"""Stream CHIRPS v2.0 daily rainfall and collapse it to HydroBASINS level-6 means.

Each global 0.05 deg daily GeoTIFF (~3 MB gzipped) is downloaded, gunzipped, read,
sliced to the Brazil window built by hydrobasins.py, and reduced to one cos(lat)-weighted
mean per basin with a single bincount. The raster is then discarded, so the whole 28-year
record lands on disk as a few tens of MB of (day x basin) matrices instead of ~26 GB of
rasters.

Windowed GDAL reads and MemoryFile reads segfault in the ESRI `crop` env, so each tif is
written to a temp file and read in full before slicing (same workaround as the monthly
CHIRPS downloader in ../soilmoisture).

Years fetched: 1989 (December only, seeds the 30-day accumulation), 1990-2010 baseline,
2018 (December only) and 2019-2025 event period.

    output: data/daily/chirps_basin_daily_<year>.npz   (resumable, one file per year)
        dates  (nday,)          int32   yyyymmdd
        precip (nday, nbasin)   float32 basin-mean rainfall, mm/day, NaN if no valid pixel
        vfrac  (nday, nbasin)   float32 fraction of the basin's pixels that were valid

Run in the ESRI `crop` env:
    <crop-python> download_chirps_daily.py [--years 1990-2010] [--workers 8]
"""
import argparse
import datetime as dt
import gzip
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import rasterio
import requests

import config as cfg

RETRIES = 4
TIMEOUT = 180


def load_index():
    z = np.load(cfg.BASIN_INDEX)
    idx = z["idx"]
    H, W = int(z["H"]), int(z["W"])
    row0, col0 = int(z["row0"]), int(z["col0"])
    nb = len(z["npix"])
    lat = cfg.CHIRPS_Y0 - (row0 + np.arange(H) + 0.5) * cfg.CHIRPS_RES
    w2d = np.repeat(np.cos(np.radians(lat))[:, None], W, axis=1).astype("float64")
    flat_idx = idx.ravel()
    inb = flat_idx >= 0
    return dict(flat_idx=flat_idx[inb], w=w2d.ravel()[inb], inb=inb,
                nb=nb, H=H, W=W, row0=row0, col0=col0,
                wsum_all=np.bincount(flat_idx[inb], weights=w2d.ravel()[inb], minlength=nb))


def date_list(year, december_only=False):
    d = dt.date(year, 12, 1) if december_only else dt.date(year, 1, 1)
    end = dt.date(year, 12, 31)
    out = []
    while d <= end:
        out.append(d)
        d += dt.timedelta(days=1)
    return out


def fetch_day(d, ix, session):
    url = cfg.CHIRPS_DAILY_URL % (d.year, d.year, d.month, d.day)
    last = None
    for attempt in range(RETRIES):
        try:
            r = session.get(url, timeout=TIMEOUT)
            if r.status_code == 404:
                return d, None, None            # day genuinely not published
            r.raise_for_status()
            raw = gzip.decompress(r.content)
            break
        except Exception as e:                   # noqa: BLE001 - retry any transport error
            last = e
            time.sleep(2 * (attempt + 1))
    else:
        raise RuntimeError("%s: %s" % (d, last))

    fd, tmp = tempfile.mkstemp(suffix=".tif", prefix="chirps_%s_" % d.isoformat())
    os.close(fd)
    try:
        with open(tmp, "wb") as fh:
            fh.write(raw)
        with rasterio.open(tmp) as src:
            full = src.read(1)
    finally:
        os.remove(tmp)

    sub = full[ix["row0"]:ix["row0"] + ix["H"], ix["col0"]:ix["col0"] + ix["W"]]
    if sub.shape != (ix["H"], ix["W"]):
        raise RuntimeError("%s: unexpected grid %s" % (d, full.shape))
    vals = sub.ravel()[ix["inb"]].astype("float64")
    good = np.isfinite(vals) & (vals != cfg.CHIRPS_NODATA) & (vals > -100)
    w = np.where(good, ix["w"], 0.0)
    wsum = np.bincount(ix["flat_idx"], weights=w, minlength=ix["nb"])
    psum = np.bincount(ix["flat_idx"], weights=w * np.where(good, vals, 0.0), minlength=ix["nb"])
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(wsum > 0, psum / np.maximum(wsum, 1e-12), np.nan)
    vfrac = wsum / ix["wsum_all"]
    return d, mean.astype("float32"), vfrac.astype("float32")


def run_year(year, ix, workers, december_only=False):
    path = cfg.daily_npz(year)
    days = date_list(year, december_only)
    if os.path.exists(path):
        z = np.load(path)
        if len(z["dates"]) == len(days) and np.isfinite(z["precip"]).any():
            print("  %d: cached (%d days)" % (year, len(z["dates"])))
            return
    precip = np.full((len(days), ix["nb"]), np.nan, "float32")
    vfrac = np.zeros((len(days), ix["nb"]), "float32")
    pos = {d: i for i, d in enumerate(days)}
    missing = []
    t0 = time.time()
    with requests.Session() as session:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(fetch_day, d, ix, session): d for d in days}
            done = 0
            for fut in as_completed(futs):
                d = futs[fut]
                try:
                    dd, mean, vf = fut.result()
                    if mean is None:
                        missing.append(d)
                    else:
                        precip[pos[dd]] = mean
                        vfrac[pos[dd]] = vf
                except Exception as e:           # noqa: BLE001
                    missing.append(d)
                    print("    FAIL %s: %s" % (d, e))
                done += 1
                if done % 100 == 0:
                    print("    %d/%d" % (done, len(days)), flush=True)

    filled = np.isfinite(precip).any(axis=1)
    if not filled.all():
        gaps = [days[i] for i in np.where(~filled)[0]]
        print("    retrying %d gap days" % len(gaps))
        with requests.Session() as session:
            for d in gaps:
                try:
                    dd, mean, vf = fetch_day(d, ix, session)
                    if mean is not None:
                        precip[pos[dd]] = mean
                        vfrac[pos[dd]] = vf
                except Exception as e:           # noqa: BLE001
                    print("    STILL MISSING %s: %s" % (d, e))

    filled = np.isfinite(precip).any(axis=1)
    dates = np.array([int(d.strftime("%Y%m%d")) for d in days], "int32")
    np.savez_compressed(path, dates=dates, precip=precip, vfrac=vfrac)
    print("  %d: %d/%d days, %d missing, %.0f s -> %s"
          % (year, int(filled.sum()), len(days), int((~filled).sum()),
             time.time() - t0, os.path.basename(path)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="all",
                    help="'all', a single year, or 'YYYY-YYYY'")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    plan = [(1989, True)] + [(y, False) for y in cfg.BASELINE_YEARS] \
        + [(2018, True)] + [(y, False) for y in cfg.EVENT_YEARS]
    if args.years != "all":
        if "-" in args.years:
            a, b = (int(x) for x in args.years.split("-"))
            want = set(range(a, b + 1))
        else:
            want = {int(args.years)}
        plan = [p for p in plan if p[0] in want]

    ix = load_index()
    print("basins: %d   window: %d x %d px   years: %d"
          % (ix["nb"], ix["H"], ix["W"], len(plan)))
    for year, dec_only in plan:
        run_year(year, ix, args.workers, dec_only)
    print("done")


if __name__ == "__main__":
    sys.exit(main())
