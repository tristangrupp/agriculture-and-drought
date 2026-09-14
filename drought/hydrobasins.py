"""Build the HydroBASINS level-6 analysis units for Brazil and the CHIRPS pixel index.

Downloads HydroSHEDS HydroBASINS standard South America levels 1-12 (open, no auth),
keeps level 6, selects every basin with at least MIN_FRAC_BR of its area inside Brazil,
and rasterizes those basins onto a window of the *global* CHIRPS 0.05 deg grid so that
every later daily raster can be collapsed to basin means with a single bincount.

Basin statistics are computed over the FULL basin polygon, not the Brazilian part:
a catchment is a hydrological unit and clipping it at a political border would bias
the basin-mean rainfall. `frac_br` records how much of each basin is in Brazil.

Outputs
    data/basins_lev06_br.gpkg   selected basins (EPSG:4326), attributes below
    data/basin_index.npz        pixel -> basin lookup on the CHIRPS grid
        idx        (H, W) int32   basin row number in the gpkg, -1 = no basin
        row0, col0 int            offset of this window in the global CHIRPS grid
        hybas_id   (n,) int64
        pfaf_id    (n,) int64
        area_km2   (n,) float64   HydroBASINS SUB_AREA
        frac_br    (n,) float64
        npix       (n,) int32     CHIRPS pixels per basin
        wsum       (n,) float64   sum of cos(lat) weights per basin

Run in the ESRI `crop` env:
    <crop-python> hydrobasins.py
"""
import io
import os
import zipfile

import geopandas as gpd
import numpy as np
import rasterio
import requests
from rasterio.features import rasterize
from rasterio.transform import from_origin

import config as cfg

HYBAS_URL = "https://data.hydrosheds.org/file/hydrobasins/standard/hybas_sa_lev01-12_v1c.zip"
HYBAS_ZIP = os.path.join(cfg.DATA_DIR, "hybas_sa_lev01-12_v1c.zip")
HYBAS_DIR = os.path.join(cfg.DATA_DIR, "hybas")
LEVEL = 6

NE_URL = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
          "geojson/ne_10m_admin_0_countries.geojson")
BR_GEOJSON = os.path.join(cfg.DATA_DIR, "brazil.geojson")

MIN_FRAC_BR = 0.05          # keep a basin if >= 5% of its area is in Brazil
EQUAL_AREA = "ESRI:102033"  # South America Albers Equal Area, for area fractions


def fetch_hybas():
    if not os.path.exists(HYBAS_ZIP):
        print("downloading HydroBASINS SA (334 MB) ...")
        with requests.get(HYBAS_URL, stream=True, timeout=600) as r:
            r.raise_for_status()
            with open(HYBAS_ZIP, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
    stem = "hybas_sa_lev%02d_v1c" % LEVEL
    shp = os.path.join(HYBAS_DIR, stem + ".shp")
    if not os.path.exists(shp):
        os.makedirs(HYBAS_DIR, exist_ok=True)
        with zipfile.ZipFile(HYBAS_ZIP) as z:
            members = [m for m in z.namelist() if os.path.basename(m).startswith(stem)]
            if not members:
                raise SystemExit("level %d not in archive: %s" % (LEVEL, z.namelist()[:5]))
            for m in members:
                with z.open(m) as src, open(os.path.join(HYBAS_DIR, os.path.basename(m)), "wb") as dst:
                    dst.write(src.read())
    return shp


def fetch_brazil():
    if not os.path.exists(BR_GEOJSON):
        gdf = gpd.read_file(NE_URL)
        br = gdf[gdf["ADM0_A3"] == "BRA"][["ADMIN", "ADM0_A3", "geometry"]].to_crs("EPSG:4326")
        if br.empty:
            raise SystemExit("Brazil not found in Natural Earth admin-0")
        br.to_file(BR_GEOJSON, driver="GeoJSON")
    return gpd.read_file(BR_GEOJSON)


def main():
    shp = fetch_hybas()
    br = fetch_brazil()
    br_geom = br.to_crs(EQUAL_AREA).geometry.union_all()

    bas = gpd.read_file(shp)
    print("level %d basins in South America: %d" % (LEVEL, len(bas)))

    # coarse filter on the Brazil bbox first, then exact area fraction
    cand = bas[bas.intersects(br.to_crs(bas.crs).geometry.union_all())].copy()
    print("intersecting Brazil: %d" % len(cand))

    ca = cand.to_crs(EQUAL_AREA)
    full = ca.geometry.area
    inter = ca.geometry.intersection(br_geom).area
    cand["frac_br"] = (inter / full).values
    cand["area_km2_calc"] = (full / 1e6).values
    sel = cand[cand["frac_br"] >= MIN_FRAC_BR].copy()
    sel = sel.sort_values("HYBAS_ID").reset_index(drop=True)
    sel["bidx"] = np.arange(len(sel), dtype="int32")
    print("kept (frac_br >= %.2f): %d basins" % (MIN_FRAC_BR, len(sel)))

    # CHIRPS-aligned window covering the selected basins
    minx, miny, maxx, maxy = sel.total_bounds
    col0 = int(np.floor((minx - cfg.CHIRPS_X0) / cfg.CHIRPS_RES))
    col1 = int(np.ceil((maxx - cfg.CHIRPS_X0) / cfg.CHIRPS_RES))
    row0 = int(np.floor((cfg.CHIRPS_Y0 - maxy) / cfg.CHIRPS_RES))
    row1 = int(np.ceil((cfg.CHIRPS_Y0 - miny) / cfg.CHIRPS_RES))
    H, W = row1 - row0, col1 - col0
    transform = from_origin(cfg.CHIRPS_X0 + col0 * cfg.CHIRPS_RES,
                            cfg.CHIRPS_Y0 - row0 * cfg.CHIRPS_RES,
                            cfg.CHIRPS_RES, cfg.CHIRPS_RES)
    print("CHIRPS window rows %d:%d cols %d:%d  -> %d x %d px" % (row0, row1, col0, col1, H, W))

    idx = rasterize(
        ((geom, int(b)) for geom, b in zip(sel.geometry, sel["bidx"])),
        out_shape=(H, W), transform=transform, fill=-1, dtype="int32", all_touched=False,
    )

    lat = cfg.CHIRPS_Y0 - (row0 + np.arange(H) + 0.5) * cfg.CHIRPS_RES
    w2d = np.repeat(np.cos(np.radians(lat))[:, None], W, axis=1)

    flat = idx.ravel()
    inb = flat >= 0
    npix = np.bincount(flat[inb], minlength=len(sel)).astype("int32")
    wsum = np.bincount(flat[inb], weights=w2d.ravel()[inb], minlength=len(sel))

    empty = int((npix == 0).sum())
    print("pixels assigned: %d / %d  (%.1f%% of window)" % (inb.sum(), H * W, 100 * inb.mean()))
    print("basins with 0 pixels: %d   min/median pixels: %d / %d"
          % (empty, npix.min(), int(np.median(npix))))
    if empty:
        print("  dropping %d basins too small for the 0.05 deg grid" % empty)

    keep = npix > 0
    sel = sel[keep].reset_index(drop=True)
    remap = np.full(len(keep), -1, "int32")
    remap[np.where(keep)[0]] = np.arange(keep.sum(), dtype="int32")
    idx = np.where(idx >= 0, remap[np.clip(idx, 0, None)], -1).astype("int32")
    sel["bidx"] = np.arange(len(sel), dtype="int32")
    npix, wsum = npix[keep], wsum[keep]

    out_cols = ["bidx", "HYBAS_ID", "NEXT_DOWN", "MAIN_BAS", "PFAF_ID", "SUB_AREA",
                "area_km2_calc", "frac_br", "geometry"]
    out_cols = [c for c in out_cols if c in sel.columns]
    sel[out_cols].to_file(cfg.BASINS_GPKG, layer="basins", driver="GPKG")

    np.savez_compressed(
        cfg.BASIN_INDEX, idx=idx, row0=row0, col0=col0, H=H, W=W,
        hybas_id=sel["HYBAS_ID"].to_numpy("int64"),
        pfaf_id=sel["PFAF_ID"].to_numpy("int64"),
        area_km2=sel["SUB_AREA"].to_numpy("float64"),
        frac_br=sel["frac_br"].to_numpy("float64"),
        npix=npix, wsum=wsum,
    )
    print("wrote %s (%d basins)" % (cfg.BASINS_GPKG, len(sel)))
    print("wrote %s" % cfg.BASIN_INDEX)


if __name__ == "__main__":
    main()
