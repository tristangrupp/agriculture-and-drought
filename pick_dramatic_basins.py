"""Pick the most extreme basin-year for each drought metric, ignoring crop labels.

Earlier selection required rice or sugarcane training labels in the same year, which
capped how extreme the cases could be - the labels only exist in a handful of basins.
Dropping that constraint, the only hard requirements are:

1. Field boundaries exist on F: for the state the basin sits in, for that year.
2. The basin holds IRRIGABLE CROPLAND, not merely farmland. Screening on farmland in
   general returns pasture, and pasture is rainfed - a pasture parcel holding its
   greenness through a drought has deep roots or better soil, not a pump, so it cannot
   demonstrate anything about water abstraction. The screen therefore counts only
   temporary and permanent CROP classes, excluding pasture, planted forest, mosaic and
   grassland, and separately counts centre pivots.
3. Four distinct basins, spread across regions so the four buttons are not one place.

Ranking is the metric itself. Divergence between fields cannot be known before the
imagery is pulled, so this stage maximises drama and reports the runner-ups; the
divergence check happens in demo_index.py once NDVI exists.

    <crop-python> pick_dramatic_basins.py
"""
import os
import unicodedata

import geopandas as gpd
import numpy as np
from shapely import minimum_bounding_circle
import pandas as pd

import config as cfg

METRICS = ["total_drought_days", "longest_event_days", "n_events", "total_deficit_mm"]
FIELD_DIR = r"F:\Trazo Fields v2\field boundaries"
NE_STATES = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
             "geojson/ne_10m_admin_1_states_provinces.geojson")
STATES_GEOJSON = os.path.join(cfg.DATA_DIR, "br_states.geojson")
MIN_REGION_SEP_DEG = 3.0     # keep the four cases apart on the map
SCREEN_DEG = 0.30            # window sampled at the basin centroid for the farm screen
FARM_HA = 2.0
# Irrigable crops only. Deliberately EXCLUDES pasture (15), planted forest (9),
# mosaic ag/pasture (21) and grassland (12): those are rainfed, so greenness held
# through a drought there says nothing about irrigation.
CROP_CLASSES = {18, 19, 20, 35, 36, 39, 40, 41, 46, 47, 48, 62}
MIN_CROP_FIELDS = 200        # crop parcels >= FARM_HA needed in the screening window
MIN_PIVOTS = 15              # and this many real centre pivots, so irrigation is present

# Centre pivots are the unambiguous irrigation signal: a circle in a field-boundary
# layer is a pump.
# Centre pivots: a true circle fills 1.0 of its minimum bounding CIRCLE, a square only
# 0.637. An earlier version tested the bounding RECTANGLE instead - a circle fills pi/4 of
# one - but so do many irregular blobs, so it returned hundreds of false positives whose
# median circle-fill was 0.50, i.e. less circular than a square. Verified against Oeste da
# Bahia, where real pivots reach 0.98.
PIVOT_CIRC_MIN = 0.85
PIVOT_HA_LO, PIVOT_HA_HI = 20.0, 200.0


def norm(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return s.replace(" ", "_")


def boundary_index():
    out = {}
    for fn in os.listdir(FIELD_DIR):
        if fn.startswith("Brazil_") and fn.endswith(".gpkg"):
            st, yr = fn[len("Brazil_"):-len(".gpkg")].rsplit("_", 1)
            out.setdefault(st, set()).add(int(yr))
    return out


def state_lookup():
    """Boundary-file state name for each basin, from the basin's representative point."""
    if not os.path.exists(STATES_GEOJSON):
        g = gpd.read_file(NE_STATES)
        g = g[g["admin"] == "Brazil"][["name", "geometry"]].to_crs("EPSG:4326")
        g.to_file(STATES_GEOJSON, driver="GeoJSON")
    states = gpd.read_file(STATES_GEOJSON)

    basins = gpd.read_file(cfg.BASINS_GPKG, layer="basins")
    pts = basins.copy()
    pts["geometry"] = basins.geometry.representative_point()
    j = gpd.sjoin(pts, states, how="left", predicate="within")
    j = j[~j.index.duplicated(keep="first")]
    basins["state"] = j["name"].to_numpy()
    cent = basins.geometry.representative_point()
    basins["lon"], basins["lat"] = cent.x, cent.y
    return basins


def count_pivots(gdf_utm, area_ha):
    """Parcels whose shape says centre pivot: near-circular and pivot-sized."""
    sized = (area_ha >= PIVOT_HA_LO) & (area_ha <= PIVOT_HA_HI)
    if not sized.any():
        return 0
    geoms = gdf_utm.geometry.to_numpy()[sized]
    circ = np.array([minimum_bounding_circle(g).area for g in geoms])
    with np.errstate(invalid="ignore", divide="ignore"):
        fill = np.where(circ > 0, np.array([g.area for g in geoms]) / circ, 0.0)
    return int((fill > PIVOT_CIRC_MIN).sum())


def farm_score(state_file, year, lon, lat):
    """Crop parcels and centre pivots in a small window at the basin centroid."""
    path = os.path.join(FIELD_DIR, "Brazil_%s_%d.gpkg" % (state_file, year))
    if not os.path.exists(path):
        return -1, -1, -1
    box = (lon - SCREEN_DEG / 2, lat - SCREEN_DEG / 2,
           lon + SCREEN_DEG / 2, lat + SCREEN_DEG / 2)
    try:
        f = gpd.read_file(path, bbox=box)
    except Exception:                                # noqa: BLE001
        return -1, -1, -1
    if f.empty:
        return 0, 0, 0
    # a few state-year GPKGs omit area_ha; fall back to the projected polygon area
    if "area_ha" in f.columns:
        ha = f["area_ha"]
    else:
        ha = f.to_crs(f.estimate_utm_crs()).area / 1e4
    f = f[ha.to_numpy() >= FARM_HA]
    if f.empty:
        return 0, 0, 0
    col = [c for c in f.columns if c.startswith("mbmode")]
    if not col:
        return len(f), -1, -1
    crops = f[f[col[0]].isin(CROP_CLASSES)]
    if crops.empty:
        return len(f), 0, 0
    u = crops.to_crs(crops.estimate_utm_crs())
    ha_c = (crops["area_ha"].to_numpy() if "area_ha" in crops.columns
            else (u.geometry.area / 1e4).to_numpy())
    return len(f), len(crops), count_pivots(u, ha_c)


def main():
    fs = boundary_index()
    basins = state_lookup()
    print("basins with a Brazilian state assigned: %d of %d"
          % (basins.state.notna().sum(), len(basins)))

    summ = pd.read_csv(os.path.join(cfg.OUT_DIR, "drought_summary_by_basin_year.csv"))
    c = summ.merge(basins[["bidx", "HYBAS_ID", "SUB_AREA", "state", "lon", "lat"]], on="bidx")
    c = c[c.state.notna()].copy()
    c["state_file"] = c["state"].map(norm)
    c["has_bounds"] = [(s in fs) and (int(y) in fs[s])
                       for s, y in zip(c.state_file, c.year)]
    ok = c[c.has_bounds & (c.n_events > 0)].copy()
    # Absolute deficit is largest where rainfall is largest, which is why every top
    # candidate is Amazonian. Cropland sits in drier country, so its deficits are smaller
    # in millimetres while being just as severe relative to normal.
    print("basin-years with boundaries on disk: %d" % len(ok))

    chosen, used_pts, cache = {}, [], {}
    for m in METRICS:
        pool = ok.sort_values(m, ascending=False)
        print("\nscreening for %s" % m)
        tried = 0
        for _, r in pool.iterrows():
            if any(abs(r.lon - x) < MIN_REGION_SEP_DEG and abs(r.lat - y) < MIN_REGION_SEP_DEG
                   for x, y in used_pts):
                continue
            if r.HYBAS_ID in [v.HYBAS_ID for v in chosen.values()]:
                continue
            k = (r.state_file, int(r.year), round(r.lon, 3), round(r.lat, 3))
            if k not in cache:
                cache[k] = farm_score(r.state_file, int(r.year), r.lon, r.lat)
            n_all, n_crop, n_piv = cache[k]
            print("  %-11d %-18s %d  %6.0f  parcels %5d, crops %5d, pivots %4d %s"
                  % (r.HYBAS_ID, r.state, r.year, r[m], n_all, n_crop, n_piv,
                     "OK" if (n_crop >= MIN_CROP_FIELDS and n_piv >= MIN_PIVOTS)
                     else ("too few pivots" if n_crop >= MIN_CROP_FIELDS
                           else "too little cropland")))
            tried += 1
            if n_crop >= MIN_CROP_FIELDS and n_piv >= MIN_PIVOTS:
                chosen[m] = r
                used_pts.append((r.lon, r.lat))
                break
            if tried >= 400:
                print("  gave up after 400 candidates")
                break

    print("\n%-20s %-11s %-20s %4s %8s  %s" %
          ("metric", "HYBAS", "state", "year", "value", "lon/lat"))
    for m in METRICS:
        if m not in chosen:
            print("%-20s no basin with enough cropland found" % m)
            continue
        r = chosen[m]
        print("%-20s %-11d %-20s %4d %8.0f  %.2f, %.2f"
              % (m, r.HYBAS_ID, r.state, r.year, r[m], r.lon, r.lat))
        k = (r.state_file, int(r.year), round(r.lon, 3), round(r.lat, 3))
        n_all, n_crop, n_piv = cache.get(k, (-1, -1, -1))
        print("     %d events | longest %d d | %d drought days | %.0f mm deficit"
              % (r.n_events, r.longest_event_days, r.total_drought_days, r.total_deficit_mm))
        print("     screening window: %d crop parcels, %d centre pivots" % (n_crop, n_piv))

    print("\nRUNNER-UPS (top 6 per metric, before the regional-spread filter)")
    for m in METRICS:
        print("\n-- %s" % m)
        print(ok.nlargest(6, m)[["HYBAS_ID", "state", "year", m, "n_events",
                                 "longest_event_days", "total_drought_days"]]
              .to_string(index=False, float_format=lambda v: "%.0f" % v))


if __name__ == "__main__":
    main()
