"""How individual fields behaved through the 2024 drought, in the worst-hit crop basins.

The basin layer says a drought happened. This asks what the fields inside it did about it,
using the one thing observable everywhere: greenness.

Method, and its limits, carried over from the Oregon validation of this same approach
(E:\\Water\\Oregan\\drought-ndvi-validation):

- The index is a field's NDVI minus the median NDVI of every field in the same window on
  the same date, averaged over the drought event. Comparing against neighbours on the same
  date cancels the season, the drought itself, atmospheric correction and sun angle. What
  survives is this field against its neighbours under identical rainfall.
- Fields that hold greenness while the neighbourhood browns are drawing water from
  somewhere other than that month's rain. That is *consistent with* irrigation, and also
  with deep roots, a later planting, or wetter soil. It ranks candidates; it does not
  prove abstraction.
- Against Oregon's measured consumptive use this index tracked water use per acre well
  (rho 0.5-0.9) but did NOT rank total volume better than field area alone. So the output
  here is deliberately about intensity and timing, never volume.
- The signal only exists where the drought overlaps the growing season. A drought that
  falls between harvest and planting shows nothing, because there is nothing green to
  lose. `season_overlap` records this per basin so a null result can be read correctly.

Outputs (data/response/)
    response_<HYBAS>.json   per-field response metrics + the basin's drought window
    response_index.json     which basins have a response layer, and their headline numbers

Run in the `ftw` env (GDAL network reads crash in `crop`):
    <ftw-python> field_response.py [--basins 6060492990,6060718180]
"""
import argparse
import datetime as dt
import json
import math
import os
import threading
import time

import geopandas as gpd
import numpy as np
import odc.stac
import pandas as pd
import planetary_computer as pc
import pystac_client
from odc.geo.geobox import GeoBox
from odc.geo.geom import BoundingBox
from rasterio.features import rasterize
from shapely.geometry import box

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
FIELDS = os.path.join(APP, "data", "fields")
OUT = os.path.join(APP, "data", "response")
os.makedirs(OUT, exist_ok=True)

DROUGHT_CANDIDATES = [r"E:\Water\water intelligence\drought",
                      r"H:\water intelligence\drought"]
DROUGHT = next((d for d in DROUGHT_CANDIDATES
                if os.path.exists(os.path.join(d, "data", "basins_lev06_br.gpkg"))),
               DROUGHT_CANDIDATES[0])

YEAR = 2024
RES = 20.0                     # overridden by --res; 30 m for the large example windows
COMPOSITE_DAYS = 10
CLOUD_MAX = 70
BANDS = ["B04", "B08", "SCL"]
SCL_BAD = {0, 1, 3, 8, 9, 10, 11}
STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
MIN_VALID = 0.30
PAD_DAYS = 45
AOI_DEG = 0.20                 # ~22 km window; a whole basin is far too much at 20 m
AOI_MAX = 0.70                 # ~78 km; past this a single composite stops fitting in RAM
AOI_STEPS = [0.20, 0.28, 0.36, 0.45, 0.55, 0.70]
GRID_MAX = 2000                # px per side; the cap that keeps every basin the same cost
MAX_TOTAL_FIELDS = 26000       # every field in the window ships, so this bounds the payload
MIN_SCOREABLE = 2500           # below this there is not enough measured to be worth showing
RES_STEPS = [20.0, 30.0, 40.0, 60.0]
MIN_FIELD_HA = 2.0
MIN_OBS = 4
# Matches ACCUM in the drought product's config: the rain series shown under a field must
# be the same 30-day accumulation that decided the basin was in drought.
ACCUM = 30

MIN_SCENES = 2                 # never fewer: a median of one scene is not a median
MAX_SCENES = 8                 # never more: past this the download dominates
CLEAR_TARGET = 0.97            # stop adding scenes once a clear look is this likely
CROP_CLASSES = [18, 19, 20, 35, 36, 39, 40, 41, 46, 47, 48, 62]

# Default targets: the top drought-on-cropland basins from field_drought.py, chosen to
# span both a wet-season failure and a dry-season one.
DEFAULT = ["6060492990", "6060718180", "6060739950"]


def res_for(aoi, lat):
    """Coarsest-necessary resolution: the finest step that still fits GRID_MAX.

    Windows are sized by field count, so a sparse region gets a wide window - and a wide
    window at 20 m is thousands of pixels of pure download cost for fields that are
    already hundreds of pixels across."""
    km = (aoi[2] - aoi[0]) * 111.32 * math.cos(math.radians(lat))
    for r in RES_STEPS:
        if km * 1000 / r <= GRID_MAX:
            return r
    return RES_STEPS[-1]


def utm_crs(lon, lat):
    zone = int((lon + 180) // 6) + 1
    return "EPSG:%d" % ((32700 if lat < 0 else 32600) + zone)


def basin_rain(bidx, first, last):
    """30-day rainfall accumulation for a basin, against its drought threshold.

    Both come from the drought product rather than being recomputed, so the rain shown
    under a field is the same series that decided the basin was in drought: P30 below the
    day-of-year 20th percentile. Anything else would invite the reader to check one
    number against a different definition of the same number.
    """
    daily = os.path.join(DROUGHT, "data", "daily")
    years = sorted({first.year, last.year, first.year - 1})
    dates, precip = [], []
    for y in years:
        p = os.path.join(daily, "chirps_basin_daily_%d.npz" % y)
        if not os.path.exists(p):
            continue
        z = np.load(p)
        dates.append(z["dates"])
        precip.append(z["precip"][:, bidx])
    if not dates:
        return None
    dates = np.concatenate(dates)
    precip = np.concatenate(precip)
    order = np.argsort(dates)
    dates, precip = dates[order], precip[order]

    # P30 is a trailing 30-day sum, so the window needs 29 days of run-up before `first`.
    acc = np.convolve(precip, np.ones(ACCUM, "float64"), mode="full")[:len(precip)]
    acc[:ACCUM - 1] = np.nan

    thr = np.load(os.path.join(DROUGHT, "data", "thresholds.npz"))["thr20"][:, bidx]
    ts = pd.to_datetime([str(x) for x in dates], format="%Y%m%d")
    # thr20 is indexed by day-of-year over 365 days; 31 Dec in a leap year reuses day 365.
    doy = np.minimum(ts.dayofyear.values, 365) - 1

    keep = (ts >= pd.Timestamp(first)) & (ts <= pd.Timestamp(last))
    return {
        "dates": [int(x) for x in dates[keep]],
        "p30": [None if not np.isfinite(v) else round(float(v), 1) for v in acc[keep]],
        "thr": [None if not np.isfinite(v) else round(float(v), 1)
                for v in thr[doy][keep]],
        "accum_days": ACCUM,
    }


def basin_event(bidx):
    ev = pd.read_csv(os.path.join(DROUGHT, "output", "drought_events.csv"),
                     parse_dates=["start", "end", "peak_date"])
    ev = ev[(ev.bidx == bidx) & (ev.end >= "%d-01-01" % YEAR)
            & (ev.start <= "%d-12-31" % YEAR)]
    if ev.empty:
        return None
    return ev.nlargest(1, "duration_days").iloc[0]


def densest_box(pts, size):
    """The size-degree box holding the most fields."""
    xs, ys = pts[:, 0], pts[:, 1]
    step = size / 5
    best, best_n = None, -1
    for cx in np.arange(xs.min(), xs.max() + step, step):
        for cy in np.arange(ys.min(), ys.max() + step, step):
            b = (cx - size / 2, cy - size / 2, cx + size / 2, cy + size / 2)
            n = int(((xs >= b[0]) & (xs <= b[2]) & (ys >= b[1]) & (ys <= b[3])).sum())
            if n > best_n:
                best, best_n = b, n
    return best, best_n


def count_in(pts, b):
    xs, ys = pts[:, 0], pts[:, 1]
    return int(((xs >= b[0]) & (xs <= b[2]) & (ys >= b[1]) & (ys <= b[3])).sum())


def pick_aoi(pts_score, pts_all, target=None, size=AOI_DEG):
    """The window: placed on scoreable cropland, sized against both constraints.

    Field size varies by an order of magnitude between regions, so a fixed window gives
    719 scoreable fields in one basin and 3,274 in another. Growing to a count makes the
    examples comparable - but the count that matters is scoreable cropland, since a window
    packed with unscoreable smallholdings has nothing to show.

    Every field in the window ships, so growth also stops once the total would be too
    expensive to deliver, even if the scoreable target has not been met.
    """
    if not target:
        b, n = densest_box(pts_score, size)
        return b, n, count_in(pts_all, b)

    chosen = None
    for s in AOI_STEPS:
        b, n = densest_box(pts_score, s)
        total = count_in(pts_all, b)
        if chosen and total > MAX_TOTAL_FIELDS and chosen[1] >= MIN_SCOREABLE:
            break                      # already enough to measure; do not pay for more
        chosen = (b, n, total)
        if n >= target or total > MAX_TOTAL_FIELDS:
            break
    return chosen


def load_fields(basin_geom, inv, crops_only=True):
    """Fields inside the basin, read from the repacked parquet.

    `crops_only` splits the two jobs this serves: scoring wants cropland big enough to
    measure, drawing wants every field there is, so the map does not imply the delineation
    missed the ground it simply did not score.
    """
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    minx, miny, maxx, maxy = basin_geom.bounds
    hit = [e for e in inv if e["xmin"] <= maxx and e["xmax"] >= minx
           and e["ymin"] <= maxy and e["ymax"] >= miny]
    if not hit:
        return None
    urls = ",".join("'%s'" % os.path.join(FIELDS, e["file"]).replace(os.sep, "/")
                    for e in hit)
    crops = ",".join(str(c) for c in CROP_CLASSES)
    filt = ("AND mb_class IN (%s) AND COALESCE(area_ha, 0) >= %s" % (crops, MIN_FIELD_HA)
            if crops_only else "")
    df = con.execute(f"""
        -- geom is typed GEOMETRY, so a bare SELECT returns DuckDB's internal
        -- serialisation. ST_AsWKB forces the standard encoding shapely reads.
        SELECT fid, mb_class, area_ha, lon, lat, ST_AsWKB(geom) AS geom
        FROM read_parquet([{urls}])
        WHERE lon BETWEEN {minx} AND {maxx} AND lat BETWEEN {miny} AND {maxy}
          {filt}
    """).fetchdf()
    if df.empty:
        return None
    # DuckDB hands BLOB back as bytearray; shapely's WKB reader only takes bytes.
    wkb = df.geom.map(lambda b: bytes(b) if b is not None else None)
    g = gpd.GeoDataFrame(df.drop(columns="geom"),
                         geometry=gpd.GeoSeries.from_wkb(wkb), crs="EPSG:4326")
    return g[g.geometry.representative_point().within(basin_geom)].reset_index(drop=True)


def run_basin(hybas, basins, inv, target=None, geojson=False, auto_res=True):
    row = basins[basins.HYBAS_ID.astype("int64").astype(str) == hybas]
    if row.empty:
        print("  %s: not a Brazilian level-6 basin" % hybas)
        return None
    row = row.iloc[0]
    ev = basin_event(int(row.bidx))
    if ev is None:
        print("  %s: no 2024 drought event" % hybas)
        return None

    print("\n%s  event %s -> %s (%d d), peak %s"
          % (hybas, ev.start.date(), ev.end.date(), ev.duration_days, ev.peak_date.date()))
    g_all = load_fields(row.geometry, inv, crops_only=False)
    if g_all is None or len(g_all) < 200:
        print("  too few fields in this basin")
        return None
    scoreable = (g_all.mb_class.isin(CROP_CLASSES)
                 & (g_all.area_ha.fillna(0) >= MIN_FIELD_HA))
    print("  fields in basin: %s (%s scoreable cropland)"
          % ("{:,}".format(len(g_all)), "{:,}".format(int(scoreable.sum()))))

    # Placed on scoreable cropland - that is the subject - but bounded by the total,
    # because every field inside the window ships.
    rp = g_all.geometry.representative_point()
    pts_all = np.c_[rp.x, rp.y]
    pts_score = pts_all[scoreable.values]
    aoi, n_score, n_total = pick_aoi(pts_score, pts_all, target=target)
    inbox = g_all.geometry.intersects(box(*aoi))
    sel_all = g_all[inbox].reset_index(drop=True)
    sel = g_all[inbox & scoreable].reset_index(drop=True)
    if len(sel) < 100:
        print("  too little scoreable cropland in the window")
        return None
    print("  AOI %.2f deg  %.3f %.3f %.3f %.3f -> %s fields, %s scoreable"
          % (aoi[2] - aoi[0], aoi[0], aoi[1], aoi[2], aoi[3],
             "{:,}".format(len(sel_all)), "{:,}".format(len(sel))))

    lon, lat = (aoi[0] + aoi[2]) / 2, (aoi[1] + aoi[3]) / 2
    crs = utm_crs(lon, lat)
    res = res_for(aoi, lat) if auto_res else RES
    gm = sel.to_crs(crs)
    minx, miny, maxx, maxy = gm.total_bounds
    minx, miny = np.floor(minx / res) * res, np.floor(miny / res) * res
    maxx, maxy = np.ceil(maxx / res) * res, np.ceil(maxy / res) * res
    gbox = GeoBox.from_bbox(BoundingBox(minx, miny, maxx, maxy, crs=crs), resolution=res)
    H, W = gbox.shape
    fid = rasterize(((geom, i + 1) for i, geom in enumerate(gm.geometry)),
                    out_shape=(H, W), transform=gbox.transform, fill=0, dtype="int32")
    nf = len(sel)
    npix = np.bincount(fid.ravel(), minlength=nf + 1)[1:]
    flat = fid.ravel()
    inb = flat > 0
    idx = flat[inb] - 1
    print("  grid %d x %d at %d m, median %d px/field"
          % (H, W, res, int(np.median(npix[npix > 0]))))

    start = (ev.start - pd.Timedelta(days=PAD_DAYS)).date()
    end = (ev.end + pd.Timedelta(days=PAD_DAYS)).date()
    cat = pystac_client.Client.open(STAC, modifier=pc.sign_inplace)
    items = list(cat.search(collections=["sentinel-2-l2a"], bbox=list(aoi),
                            datetime="%s/%s" % (start, end),
                            query={"eo:cloud_cover": {"lt": CLOUD_MAX}}).items())
    groups = {}
    for it in items:
        d = pd.Timestamp(it.datetime).date()
        if start <= d <= end:
            groups.setdefault((d - start).days // COMPOSITE_DAYS, []).append(it)
    # Every scene in a window is fetched in full for three bands, so the tail of cloudy
    # scenes costs as much as the clear ones and contributes almost nothing after masking.
    kept = 0
    for k in groups:
        groups[k].sort(key=lambda it: it.properties.get("eo:cloud_cover", 100))
        # Keep adding scenes until a pixel is CLEAR_TARGET likely to be seen at least
        # once. Two clear scenes is plenty; two 70%-cloud scenes is not, and a flat cap
        # cannot tell those apart.
        chosen, miss = [], 1.0
        for it in groups[k]:
            chosen.append(it)
            miss *= min(max(it.properties.get("eo:cloud_cover", 100) / 100.0, 0.02), 0.98)
            if len(chosen) >= MIN_SCENES and 1 - miss >= CLEAR_TARGET:
                break
            if len(chosen) >= MAX_SCENES:
                break
        groups[k] = chosen
        kept += len(chosen)
    print("  imagery %s .. %s: %d scenes -> %d composites (%d scenes kept)"
          % (start, end, len(items), len(groups), kept))

    lock = threading.Lock()

    def one_composite(k):
        """Load one date window and reduce it to a per-field mean NDVI."""
        mid = start + dt.timedelta(days=k * COMPOSITE_DAYS + COMPOSITE_DAYS // 2)
        ds, err = None, "?"
        for attempt in range(4):
            try:
                # The SAS tokens in a search result expire; re-sign at fetch time or a
                # long pull starts 403-ing halfway through.
                ds = odc.stac.load(groups[k], bands=BANDS, geobox=gbox, chunks=None,
                                   dtype="uint16", nodata=0, groupby="solar_day",
                                   patch_url=pc.sign)
                break
            except Exception as e:                       # noqa: BLE001
                err = str(e)[:70]
                time.sleep(3 * (attempt + 1))
        if ds is None:
            with lock:
                print("    %s load failed (%s)" % (mid, err), flush=True)
            return None
        clear = ~np.isin(ds["SCL"].values, list(SCL_BAD))
        b4 = ds["B04"].values.astype("float32")
        b8 = ds["B08"].values.astype("float32")
        b4[~clear] = np.nan
        b8[~clear] = np.nan
        with np.errstate(invalid="ignore", divide="ignore"):
            nd = (b8 - b4) / (b8 + b4)
        if nd.ndim == 3:
            nd = np.nanmedian(nd, axis=0)
        if nd.shape != (H, W):
            return None
        good = np.isfinite(nd.ravel()[inb])
        cnt = np.bincount(idx[good], minlength=nf).astype("float64")
        ssum = np.bincount(idx[good], weights=nd.ravel()[inb][good], minlength=nf)
        with np.errstate(invalid="ignore", divide="ignore"):
            mean_nd = np.where(cnt > 0, ssum / np.maximum(cnt, 1), np.nan)
        mean_nd[cnt / np.maximum(npix, 1) < MIN_VALID] = np.nan
        with lock:
            print("    %s  clear %4.1f%%  NDVI median %.3f  fields %d"
                  % (mid, 100 * clear.mean(), np.nanmedian(mean_nd),
                     int(np.isfinite(mean_nd).sum())), flush=True)
        return int(mid.strftime("%Y%m%d")), mean_nd.astype("float32")

    # Serial on purpose: a thread pool over these loads moved 0.9 MB in 20 s, because
    # GDAL's /vsicurl reads serialise anyway and the pool only added contention.
    done = [r for r in (one_composite(k) for k in sorted(groups)) if r is not None]
    done.sort(key=lambda r: r[0])
    dates = [r[0] for r in done]
    series = [r[1] for r in done]

    if len(dates) < MIN_OBS:
        print("  not enough clear composites")
        return None

    ndvi = np.stack(series)
    d = pd.to_datetime([str(x) for x in dates], format="%Y%m%d")
    in_ev = (d >= ev.start) & (d <= ev.end)
    pre = d < ev.start
    if in_ev.sum() < MIN_OBS:
        in_ev = np.ones(len(d), bool)      # short event: score the whole window

    scene_med = np.nanmedian(ndvi, axis=1)
    anom = ndvi - scene_med[:, None]
    with np.errstate(invalid="ignore"):
        n_obs = np.isfinite(anom[in_ev]).sum(axis=0)
        mean_anom = np.nanmean(anom[in_ev], axis=0)
        ndvi_pre = np.nanmean(ndvi[pre], axis=0) if pre.any() else np.full(nf, np.nan)
        ndvi_ev = np.nanmean(ndvi[in_ev], axis=0)
        ndvi_min = np.nanmin(ndvi[in_ev], axis=0)
    mean_anom[n_obs < MIN_OBS] = np.nan
    drop = ndvi_pre - ndvi_min

    # Scale to 0-1 anchored at zero: zero anomaly means "behaved exactly like its
    # neighbours" and must read as no signal. Only fields ABOVE their neighbours score.
    hi = np.nanpercentile(np.where(mean_anom > 0, mean_anom, np.nan), 95)
    held = np.clip(mean_anom / max(hi if np.isfinite(hi) else 1e-6, 1e-6), 0, 1)

    scored = np.isfinite(mean_anom)
    # Does the drought overlap anything green? If the scene is bare throughout, a null
    # result means "nothing was growing", not "nothing drew water".
    overlap = float(np.nanmedian(scene_med[in_ev]))

    rec = {
        "hybas": hybas,
        "bidx": int(row.bidx),
        "aoi": [round(v, 5) for v in aoi],
        "event": {"start": str(ev.start.date()), "end": str(ev.end.date()),
                  "days": int(ev.duration_days), "peak": str(ev.peak_date.date())},
        "dates": dates,
        "scene_ndvi": [None if not np.isfinite(v) else round(float(v), 3)
                       for v in scene_med],
        "season_overlap": round(overlap, 3),
        "res_m": int(res),
        "n_drawn": int(len(sel_all)),
        "n_scoreable": int(nf),
        "n_fields": int(nf),
        "n_scored": int(scored.sum()),
        "fields": {
            int(sel.fid.iloc[i]): [
                round(float(mean_anom[i]), 3),
                round(float(held[i]), 3),
                round(float(drop[i]), 3) if np.isfinite(drop[i]) else None,
                int(sel.mb_class.iloc[i]) if pd.notna(sel.mb_class.iloc[i]) else None
            ]
            for i in range(nf) if scored[i]
        },
    }
    q = np.nanpercentile(mean_anom[scored], [10, 50, 90])
    print("  scored %s of %s scoreable (%s drawn) · anomaly p10 %+.3f  median %+.3f  p90 %+.3f"
          % ("{:,}".format(int(scored.sum())), "{:,}".format(nf),
             "{:,}".format(len(sel_all)), *q))
    print("  scene NDVI during the event: median %.3f (%s)"
          % (overlap, "crops growing" if overlap > 0.35 else "little green cover"))
    json.dump(rec, open(os.path.join(OUT, "response_%s.json" % hybas), "w"),
              separators=(",", ":"), allow_nan=False)

    # Per-field trends, plus the rainfall that drove them. Kept in its own file: it is
    # the largest thing here and only needed once someone clicks a field.
    ser = {
        "hybas": hybas,
        "dates": dates,
        "scene": rec["scene_ndvi"],
        "event": rec["event"],
        "rain": basin_rain(int(row.bidx), d[0].date(), d[-1].date()),
        "fields": {
            int(sel.fid.iloc[i]): [
                None if not np.isfinite(v) else round(float(v), 3) for v in ndvi[:, i]
            ]
            for i in range(nf) if scored[i]
        },
    }
    json.dump(ser, open(os.path.join(OUT, "series_%s.json" % hybas), "w"),
              separators=(",", ":"), allow_nan=False)
    print("  series -> series_%s.json (%.1f MB)"
          % (hybas, os.path.getsize(os.path.join(OUT, "series_%s.json" % hybas)) / 1e6))

    if geojson:
        # Geometry for the static GitHub Pages build, which has no DuckDB and no parquet.
        # 5 decimal places is ~1 m at this latitude - far finer than the field boundaries.
        # Every field in the window, with a score where there is one. An unscored field
        # is not a missing field, and the map must not imply that it is.
        by_fid = {int(sel.fid.iloc[i]): (round(float(held[i]), 3),
                                         round(float(mean_anom[i]), 3))
                  for i in range(nf) if scored[i]}
        gj = gpd.GeoDataFrame(
            {
                "fid": sel_all.fid.values,
                "mb_class": sel_all.mb_class.values,
                "area_ha": np.round(sel_all.area_ha.fillna(0).values.astype("float64"), 1),
                "held": [by_fid.get(int(f), (None, None))[0] for f in sel_all.fid],
                "anom": [by_fid.get(int(f), (None, None))[1] for f in sel_all.fid],
            },
            geometry=sel_all.geometry.values,
            crs="EPSG:4326",
        )
        path = os.path.join(OUT, "fields_%s.geojson" % hybas)
        gj.to_file(path, driver="GeoJSON", coordinate_precision=5)
        print("  geometry -> %s (%.1f MB)"
              % (os.path.basename(path), os.path.getsize(path) / 1e6))
    return {"hybas": hybas, "bidx": int(row.bidx), "aoi": rec["aoi"],
            "event": rec["event"], "n_scored": rec["n_scored"],
            "season_overlap": overlap,
            "anom_p10": round(float(q[0]), 3), "anom_p90": round(float(q[2]), 3)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--basins", default=",".join(DEFAULT))
    ap.add_argument("--target", type=int, default=0,
                    help="grow the window until it holds this many scoreable fields")
    ap.add_argument("--res", type=float, default=0,
                    help="fixed metres per pixel; default scales with window size")
    ap.add_argument("--geojson", action="store_true",
                    help="also write field geometry for the static build")
    args = ap.parse_args()
    if args.res:
        globals()["RES"] = args.res

    basins = gpd.read_file(os.path.join(DROUGHT, "data", "basins_lev06_br.gpkg"),
                           layer="basins")
    inv = [e for e in json.load(open(os.path.join(APP, "data", "inventory.json")))
           if e["year"] == YEAR]

    index_path = os.path.join(OUT, "response_index.json")
    index = json.load(open(index_path)) if os.path.exists(index_path) else []
    for hybas in [h.strip() for h in args.basins.split(",") if h.strip()]:
        try:
            r = run_basin(hybas, basins, inv, target=args.target or None,
                          geojson=args.geojson, auto_res=not args.res)
        except Exception as e:                           # noqa: BLE001
            print("  FAILED %s: %s" % (hybas, str(e)[:140]))
            continue
        if r:
            index = [e for e in index if e["hybas"] != hybas] + [r]
            json.dump(index, open(index_path, "w"), separators=(",", ":"), indent=1)
    print("\nresponse layers: %d" % len(index))


if __name__ == "__main__":
    main()
