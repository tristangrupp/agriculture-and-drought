"""Prepare the field set for each demo: clip to basin, place an AOI on the farmland.

For each demo case:
1. read the state-year field boundaries, clipped to the basin's bounding box
2. keep parcels of at least MIN_FIELD_HA that are actually inside the basin
3. place a square AOI on the densest cluster of IRRIGABLE CROP parcels, preferring
   centre pivots where they exist. A level-6 basin holds tens of thousands of parcels -
   too many for 20 m imagery and too many to animate - and the choice of window decides
   what the demo can show. Pasture is excluded from that choice: it is rainfed, so a
   pasture parcel staying green through a drought says something about its roots, not
   about water being applied to it.
4. record each parcel's MapBiomas class so the animation can be read by land use

Crop labels are no longer required. Where rice or sugarcane training labels happen to
overlap the AOI they are still recorded in `crop_tag`, but they no longer drive selection
and are not colour-coded on the map.

Output per demo (output/demo/<key>/)
    fields.parquet   AOI parcels with area_ha, mb_class, farm flag, crop_tag
    aoi.json         AOI bounds, basin id, counts

Run in the ESRI `crop` env:
    <crop-python> demo_fields.py
"""
import json
import os

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely import minimum_bounding_circle
from shapely.geometry import box

import config as cfg
import demo_config as dc

EQUAL_AREA = "ESRI:102033"

MB_NAMES = {3: "forest", 4: "savanna", 5: "mangrove", 6: "flooded forest", 9: "planted forest",
            11: "wetland", 12: "grassland", 15: "pasture", 18: "agriculture",
            19: "temporary crop", 20: "sugarcane", 21: "mosaic ag/pasture", 23: "beach",
            24: "urban", 25: "other non-vegetated", 26: "water", 27: "not observed",
            29: "rocky outcrop", 30: "mining", 31: "aquaculture", 32: "salt flat",
            33: "river/lake", 35: "oil palm", 36: "perennial crop", 39: "soybean",
            40: "rice", 41: "other temporary crop", 46: "coffee", 47: "citrus",
            48: "other perennial", 49: "wooded sandbank", 50: "sandbank", 62: "cotton"}


def pick_aoi(pts, fallback_geom):
    """Square AOI centred on the densest cluster of the supplied points."""
    if pts.empty:
        c = fallback_geom.centroid
        return (c.x - dc.AOI_DEG / 2, c.y - dc.AOI_DEG / 2,
                c.x + dc.AOI_DEG / 2, c.y + dc.AOI_DEG / 2)
    xs, ys = pts.x.to_numpy(), pts.y.to_numpy()
    step = dc.AOI_DEG / 5
    best, best_n = None, -1
    for cx in np.arange(xs.min(), xs.max() + step, step):
        for cy in np.arange(ys.min(), ys.max() + step, step):
            b = (cx - dc.AOI_DEG / 2, cy - dc.AOI_DEG / 2,
                 cx + dc.AOI_DEG / 2, cy + dc.AOI_DEG / 2)
            n = int(((xs >= b[0]) & (xs <= b[2]) & (ys >= b[1]) & (ys <= b[3])).sum())
            if n > best_n:
                best, best_n = b, n
    return best


def main():
    basins = gpd.read_file(cfg.BASINS_GPKG, layer="basins")
    labels = gpd.read_parquet(
        os.path.join(cfg.OUT_DIR, "crop_exposure", "labels_drought_exposure.parquet"))

    for demo in dc.DEMOS:
        print("\n" + "=" * 74)
        print("%s  |  %s %d  |  HYBAS %d"
              % (demo["button"], demo["state"], demo["year"], demo["hybas_id"]))
        print("=" * 74)

        basin = basins[basins.HYBAS_ID == demo["hybas_id"]]
        bgeom = basin.geometry.iloc[0]

        path = dc.boundary_path(demo)
        flds = gpd.read_file(path, bbox=tuple(basin.total_bounds))
        print("parcels in the basin bbox: %d" % len(flds))
        if "area_ha" not in flds.columns:
            flds["area_ha"] = flds.to_crs(EQUAL_AREA).area / 1e4
        flds = flds[flds.area_ha >= dc.MIN_FIELD_HA]
        flds = flds[flds.intersects(bgeom)].reset_index(drop=True)

        mbcol = next((c for c in flds.columns if c.startswith("mbmode")), None)
        flds["mb_class"] = (flds[mbcol].fillna(-1).astype(int) if mbcol else -1)
        flds["mb_name"] = flds["mb_class"].map(MB_NAMES).fillna("unknown")
        flds["is_crop"] = flds["mb_class"].isin(dc.CROP_CLASSES)

        # Centre pivots by shape - the unambiguous irrigation marker. The test is how
        # much of its minimum bounding CIRCLE the parcel fills: 1.0 for a true circle,
        # 0.637 for a square. Testing the bounding rectangle instead (an earlier version)
        # matches any blob that happens to fill pi/4 of one and is worthless here.
        u = flds.to_crs(EQUAL_AREA)
        circ = np.array([minimum_bounding_circle(g).area for g in u.geometry])
        with np.errstate(invalid="ignore", divide="ignore"):
            fill = np.where(circ > 0, u.geometry.area.to_numpy() / circ, 0.0)
        flds["circ_fill"] = fill.round(3)
        flds["is_pivot"] = ((fill > dc.PIVOT_CIRC_MIN)
                            & (flds.area_ha.to_numpy() >= dc.PIVOT_HA_LO)
                            & (flds.area_ha.to_numpy() <= dc.PIVOT_HA_HI))
        flds["is_farm"] = flds["is_crop"] | flds["is_pivot"]
        print("parcels >=%.0f ha in the basin: %d  (%d crop, %d centre pivots)"
              % (dc.MIN_FIELD_HA, len(flds), int(flds.is_crop.sum()),
                 int(flds.is_pivot.sum())))

        # pivots anchor the window when there are enough of them; otherwise crops do
        anchor = flds.loc[flds.is_pivot] if flds.is_pivot.sum() >= 20 else flds.loc[flds.is_crop]
        print("  AOI anchored on %s" % ("centre pivots" if flds.is_pivot.sum() >= 20
                                        else "crop parcels"))
        farm_pts = anchor.geometry.representative_point()
        aoi = pick_aoi(farm_pts, bgeom)
        aoi_box = box(*aoi)
        sel = flds[flds.intersects(aoi_box)].reset_index(drop=True)
        print("AOI %.3f %.3f %.3f %.3f -> %d parcels (%d crop, %d pivots)"
              % (aoi[0], aoi[1], aoi[2], aoi[3], len(sel),
                 int(sel.is_crop.sum()), int(sel.is_pivot.sum())))
        top = sel.mb_name.value_counts().head(6)
        print("  land use: %s" % ", ".join("%s %d" % (k, v) for k, v in top.items()))

        # crop labels are optional now - recorded where they exist, never required
        sel["crop_tag"] = "untagged"
        sel["label_overlap"] = 0.0
        lab = labels[labels["sub"].isin(["rice", "sugarcane"])
                     & labels.geometry.intersects(aoi_box)]
        if not lab.empty:
            j = gpd.overlay(sel[["geometry"]].reset_index().rename(columns={"index": "fid"}),
                            lab[["geometry", "sub"]].reset_index(drop=True),
                            how="intersection", keep_geom_type=True)
            if not j.empty:
                inter = j.to_crs(EQUAL_AREA).area.groupby(j["fid"]).sum()
                fa = sel.to_crs(EQUAL_AREA).area
                frac = (inter / fa.reindex(inter.index)).clip(0, 1)
                hit = frac[frac >= dc.LABEL_OVERLAP].index
                sel.loc[hit, "crop_tag"] = "labelled crop"
                sel.loc[frac.index, "label_overlap"] = frac.values
        n_tag = int((sel.crop_tag != "untagged").sum())
        print("  overlapping crop labels: %d parcels" % n_tag)

        sel["fid"] = np.arange(len(sel))
        keep = ["fid", "area_ha", "mb_class", "mb_name", "is_farm", "is_crop",
                "is_pivot", "circ_fill", "crop_tag", "label_overlap", "geometry"]
        sel[keep].to_parquet(dc.demo_path(demo, "fields.parquet"), index=False)

        json.dump({
            "key": demo["key"], "hybas_id": demo["hybas_id"], "state": demo["state"],
            "year": demo["year"], "metric": demo["metric"],
            "aoi": list(aoi), "basin_bounds": list(basin.total_bounds),
            "n_fields": int(len(sel)), "n_farm": int(sel.is_farm.sum()),
            "n_crop": int(sel.is_crop.sum()), "n_pivot": int(sel.is_pivot.sum()),
            "n_tagged": n_tag,
            "land_use": {k: int(v) for k, v in top.items()},
        }, open(dc.demo_path(demo, "aoi.json"), "w"), indent=2)
        print("wrote %s" % dc.demo_path(demo, "fields.parquet"))


if __name__ == "__main__":
    main()
