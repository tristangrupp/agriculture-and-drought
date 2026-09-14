"""Per-field NDVI through the drought year, plus water that was actually there.

For each demo AOI this pulls Sentinel-2 L2A from the Planetary Computer across the whole
drought year, builds ~10-day cloud-masked composites, and reduces each composite to one
number per field with a single bincount over a rasterised field-id grid - the same trick
the CHIRPS stage uses on basins.

Two indices come off the same scenes, which is the point:

    NDVI  = (B08 - B04) / (B08 + B04)    how green the field is
    MNDWI = (B03 - B11) / (B03 + B11)    open water, positive where water is present

Using MNDWI from the same imagery is what makes the water layer honest. A distance to a
mapped river line answers "where does the hydrography say water should be"; the drought
question is "where was there still water while the rain had stopped", and only an
observation on the date can answer that. The masks kept here are water seen at the
drought peak, water seen in every composite (the part that did not dry up) and water seen
at any point. No distance-to-water metric is derived - that was excluded on request.

Resolution is 20 m, not 10 m: fields are >= 1 ha so the smallest still carries ~25 pixels,
and it cuts the pixel count per composite by four.

Output per demo (output/demo/<key>/)
    ndvi.npz    dates, ndvi (ntime, nfield), water_frac, valid_frac, cloud_frac
    water.npz   water masks and the per-field distances, on the AOI grid
    Run in the `ftw` env - GDAL network COG reads crash in `crop`:
    <ftw-python> demo_ndvi.py [--demo KEY]
"""
import argparse
import datetime as dt
import json
import os

import geopandas as gpd
import numpy as np
import odc.stac
import pandas as pd
import planetary_computer as pc
import pystac_client
from odc.geo.geobox import GeoBox
from odc.geo.geom import BoundingBox
from rasterio.features import rasterize

import config as cfg
import demo_config as dc

RES = 20.0                    # metres
COMPOSITE_DAYS = 10
PAD_DAYS = 60                 # imagery either side of the drought event
CLOUD_MAX = 70                # scene-level filter; SCL does the per-pixel work
BANDS = ["B03", "B04", "B08", "B11", "SCL"]
SCL_BAD = {0, 1, 3, 8, 9, 10, 11}   # nodata, saturated, shadow, cloud med/high, cirrus, snow
STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
MIN_VALID = 0.30              # a field-date needs this share of clear pixels to count


def utm_crs(lon, lat):
    zone = int((lon + 180) // 6) + 1
    return "EPSG:%d" % ((32700 if lat < 0 else 32600) + zone)


def peak_window(demo):
    """The drought event of the demo year, and its peak date."""
    basins = gpd.read_file(cfg.BASINS_GPKG, layer="basins")
    bidx = int(basins.loc[basins.HYBAS_ID == demo["hybas_id"], "bidx"].iloc[0])
    ev = pd.read_csv(os.path.join(cfg.OUT_DIR, "drought_events.csv"),
                     parse_dates=["start", "end", "peak_date"])
    ev = ev[(ev.bidx == bidx) & (ev.start_year == demo["year"])]
    if ev.empty:
        raise SystemExit("no drought event for %s" % demo["key"])
    main = ev.nlargest(1, "duration_days").iloc[0]
    return bidx, ev, main


def build_grid(fields, aoi):
    """One GeoBox for both the field raster and every Sentinel-2 load.

    Deriving the grid twice - once from the field bounds, once from the AOI bbox - gives
    two grids that differ by a few pixels and silently misaligns every zonal statistic,
    so the GeoBox is built once here and handed to odc.stac.load as well.
    """
    lon = (aoi[0] + aoi[2]) / 2
    lat = (aoi[1] + aoi[3]) / 2
    crs = utm_crs(lon, lat)
    f = fields.to_crs(crs)
    minx, miny, maxx, maxy = f.total_bounds
    minx, miny = np.floor(minx / RES) * RES, np.floor(miny / RES) * RES
    maxx, maxy = np.ceil(maxx / RES) * RES, np.ceil(maxy / RES) * RES
    gbox = GeoBox.from_bbox(BoundingBox(minx, miny, maxx, maxy, crs=crs), resolution=RES)
    H, W = gbox.shape
    fid = rasterize(((g, int(i) + 1) for g, i in zip(f.geometry, f.fid)),
                    out_shape=(H, W), transform=gbox.transform, fill=0, dtype="int32")
    return crs, gbox, fid, (minx, miny, maxx, maxy), (H, W)


def series_window(demo, main):
    """Imagery window: the demo year, widened to cover the whole drought event.

    A drought that starts in October and runs into the following May peaks OUTSIDE its
    start year, so pulling only the calendar year would miss the part the demo is about.
    The window is the union of the calendar year and the event plus PAD_DAYS either side,
    which keeps a full annual cycle for the NDVI curve and still reaches the peak.
    """
    y0 = dt.date(demo["year"], 1, 1)
    y1 = dt.date(demo["year"], 12, 31)
    e0 = main.start.date() - dt.timedelta(days=PAD_DAYS)
    e1 = main.end.date() + dt.timedelta(days=PAD_DAYS)
    return min(y0, e0), max(y1, e1)


def composites(items, start, end):
    """Group scene items into fixed COMPOSITE_DAYS windows across the series window."""
    groups = {}
    for it in items:
        d = pd.Timestamp(it.datetime).date()
        if d < start or d > end:
            continue
        k = (d - start).days // COMPOSITE_DAYS
        groups.setdefault(k, []).append(it)
    return dict(sorted(groups.items()))


def run_demo(demo):
    meta = json.load(open(dc.demo_path(demo, "aoi.json")))
    aoi = meta["aoi"]
    fields = gpd.read_parquet(dc.demo_path(demo, "fields.parquet"))
    nf = len(fields)
    bidx, events, main = peak_window(demo)
    print("")
    print("%s | %s %d | HYBAS %d | %d parcels"
          % (demo["button"], demo["state"], demo["year"], demo["hybas_id"], nf))
    print("  main event %s -> %s (%d d), peak %s"
          % (main.start.date(), main.end.date(), main.duration_days, main.peak_date.date()))

    crs, gbox, fid, bounds, (H, W) = build_grid(fields, aoi)
    npix = np.bincount(fid.ravel(), minlength=nf + 1)[1:]
    print("  grid %d x %d at %.0f m; pixels per field: median %d, min %d"
          % (H, W, RES, int(np.median(npix)), int(npix.min())))

    cat = pystac_client.Client.open(STAC, modifier=pc.sign_inplace)
    win0, win1 = series_window(demo, main)
    search = cat.search(collections=["sentinel-2-l2a"], bbox=aoi,
                        datetime="%s/%s" % (win0.isoformat(), win1.isoformat()),
                        query={"eo:cloud_cover": {"lt": CLOUD_MAX}})
    items = list(search.items())
    groups = composites(items, win0, win1)
    print("  imagery window %s .. %s" % (win0, win1))
    print("  %d scenes -> %d composite windows" % (len(items), len(groups)))

    flat = fid.ravel()
    inb = flat > 0
    idx = flat[inb] - 1

    dates, ndvi_t, water_t, valid_t = [], [], [], []
    water_stack = []
    for k, its in groups.items():
        mid = win0 + dt.timedelta(days=k * COMPOSITE_DAYS + COMPOSITE_DAYS // 2)
        try:
            # patch_url re-signs every asset URL at read time. Signing only at search
            # time leaves ~1 h SAS tokens that expire part-way through a long pull, and
            # the reads then fail with HTTP 403 - which is what silently emptied the
            # Rondonia series on the first attempt.
            ds = odc.stac.load(its, bands=BANDS, geobox=gbox, chunks=None,
                               dtype="uint16", nodata=0, groupby="solar_day",
                               patch_url=pc.sign)
        except Exception as e:                       # noqa: BLE001
            print("    %s: load failed (%s)" % (mid, str(e)[:60]))
            continue
        scl = ds["SCL"].values
        clear = ~np.isin(scl, list(SCL_BAD))
        b3 = ds["B03"].values.astype("float32")
        b4 = ds["B04"].values.astype("float32")
        b8 = ds["B08"].values.astype("float32")
        b11 = ds["B11"].values.astype("float32")
        for a in (b3, b4, b8, b11):
            a[~clear] = np.nan
        with np.errstate(invalid="ignore", divide="ignore"):
            nd = (b8 - b4) / (b8 + b4)
            mw = (b3 - b11) / (b3 + b11)
        nd = np.nanmedian(nd, axis=0) if nd.ndim == 3 else nd
        mw = np.nanmedian(mw, axis=0) if mw.ndim == 3 else mw
        if nd.shape != (H, W):
            print("    %s: grid mismatch %s vs %s" % (mid, nd.shape, (H, W)))
            continue

        good = np.isfinite(nd.ravel()[inb])
        cnt = np.bincount(idx[good], minlength=nf).astype("float64")
        ssum = np.bincount(idx[good], weights=nd.ravel()[inb][good], minlength=nf)
        with np.errstate(invalid="ignore", divide="ignore"):
            mean_nd = np.where(cnt > 0, ssum / np.maximum(cnt, 1), np.nan)
        valid = cnt / np.maximum(npix, 1)
        mean_nd[valid < MIN_VALID] = np.nan

        wet = np.isfinite(mw) & (mw > 0)
        wcnt = np.bincount(idx[wet.ravel()[inb]], minlength=nf).astype("float64")
        wfrac = wcnt / np.maximum(npix, 1)

        dates.append(int(mid.strftime("%Y%m%d")))
        ndvi_t.append(mean_nd.astype("float32"))
        water_t.append(wfrac.astype("float32"))
        valid_t.append(valid.astype("float32"))
        water_stack.append(wet)
        print("    %s  clear %4.1f%%  NDVI median %.3f  fields valid %d"
              % (mid, 100 * clear.mean(), np.nanmedian(mean_nd),
                 int(np.isfinite(mean_nd).sum())), flush=True)

    if not dates:
        print("  no usable composites")
        return

    dates = np.array(dates, "int32")
    ndvi = np.stack(ndvi_t)
    np.savez_compressed(dc.demo_path(demo, "ndvi.npz"),
                        dates=dates, ndvi=ndvi, water_frac=np.stack(water_t),
                        valid_frac=np.stack(valid_t), npix=npix,
                        event_start=int(main.start.strftime("%Y%m%d")),
                        event_end=int(main.end.strftime("%Y%m%d")),
                        peak_date=int(main.peak_date.strftime("%Y%m%d")))

    # ---- water that was actually present ------------------------------------
    ws = np.stack(water_stack)
    peak = int(main.peak_date.strftime("%Y%m%d"))
    ipeak = int(np.argmin(np.abs(dates - peak)))
    water_peak = ws[ipeak]
    water_perm = ws.mean(axis=0) >= 0.9        # wet in ~every composite of the year
    water_ever = ws.any(axis=0)

    # Distance to surface water is deliberately NOT computed - excluded at the user's
    # request. The masks are kept because open water inside a rice field is direct
    # evidence of flooding, which is a use signal rather than a proximity proxy.
    np.savez_compressed(dc.demo_path(demo, "water.npz"),
                        water_peak=water_peak, water_perm=water_perm,
                        water_ever=water_ever, peak_composite=int(dates[ipeak]),
                        bounds=np.array(bounds), crs=str(crs), shape=np.array([H, W]))
    print("  open water: %.2f%% of AOI at the peak | %.2f%% in every composite | %.2f%% ever"
          % (100 * water_peak.mean(), 100 * water_perm.mean(), 100 * water_ever.mean()))
    print("  wrote ndvi.npz + water.npz")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", default=None)
    args = ap.parse_args()
    for demo in dc.DEMOS:
        if args.demo and demo["key"] != args.demo:
            continue
        try:
            run_demo(demo)
        except Exception as e:                       # noqa: BLE001
            print("FAILED %s: %s" % (demo["key"], e))


if __name__ == "__main__":
    main()
