"""Score each field on how much greenness it held while the rain failed.

A field can draw water during a drought in two ways that look OPPOSITE in NDVI, so the
index has to catch both.

    green_index(f)  NDVI(f, t) minus the median NDVI of every AOI field at t, averaged
                    over the drought window. Catches a field irrigated to keep growing:
                    it holds greenness while its neighbours brown.

    flood_index(f)  share of the field seen as open water (MNDWI > 0), averaged over the
                    same window. Catches flood irrigation, which reads as LOW NDVI - a
                    flooded, freshly transplanted rice paddy is water plus seedlings.

    water_use(f) = max(green_index, flood_index)

Using only greenness gets rice exactly backwards. In the Santa Catarina cases the labelled
paddies sit 0.05-0.20 NDVI BELOW their neighbours during the drought while carrying three
times as much open water (19% of the field against 6%), because they are flooded in the
September-November transplanting window. They are drawing the most water in the scene and
a greenness-only index scores them lowest.

Comparing against the AOI median on the same date cancels everything that hits the whole
scene at once - the season, the drought itself, atmospheric correction, sun angle. What
survives is the difference between this field and its neighbours under identical rainfall.

What this is NOT: proof of irrigation. A deep-rooted perennial, a late-planted crop or a
field on a wetter soil can hold greenness too. The index ranks candidates for a human to
look at; the tagged rice and sugarcane fields are there to show whether the crops we know
about behave differently from the rest.

Also computed
    ndvi_drop      NDVI before the event minus NDVI at its worst, per field
    green_at_peak  NDVI at the composite nearest the drought peak
    water_frac_*   share of the field seen as open water (flooded paddies show up here)

Output per demo: output/demo/<key>/field_index.parquet + a summary to stdout.

Run in the ESRI `crop` env:
    <crop-python> demo_index.py
"""
import json
import os

import geopandas as gpd
import numpy as np
import pandas as pd

import config as cfg
import demo_config as dc

MIN_OBS = 4          # composites a field needs inside the scoring window to be scored
MIN_WINDOW_DAYS = 70  # shortest scoring window; short events yield too few clear composites


def to_date(v):
    s = str(int(v))
    return pd.Timestamp("%s-%s-%s" % (s[:4], s[4:6], s[6:]))


def run_demo(demo):
    z = np.load(dc.demo_path(demo, "ndvi.npz"))
    fields = gpd.read_parquet(dc.demo_path(demo, "fields.parquet"))
    meta = json.load(open(dc.demo_path(demo, "aoi.json")))

    dates = pd.DatetimeIndex([to_date(d) for d in z["dates"]])
    ndvi = z["ndvi"]                       # (ntime, nfield)
    wfrac = z["water_frac"]
    ev0, ev1 = to_date(z["event_start"]), to_date(z["event_end"])
    peak = to_date(z["peak_date"])

    # A 38-day event leaves ~4 composite slots, and cloud takes most of them, so the
    # scoring window is the event widened symmetrically to at least MIN_WINDOW_DAYS.
    # Vegetation responds to a rainfall deficit with a lag anyway, so the days just after
    # an event carry as much of the signal as the days inside it.
    pad = max(0, (MIN_WINDOW_DAYS - (ev1 - ev0).days) // 2)
    w0, w1 = ev0 - pd.Timedelta(days=pad), ev1 + pd.Timedelta(days=pad)
    in_ev = (dates >= w0) & (dates <= w1)
    pre = (dates < w0) & (dates >= w0 - pd.Timedelta(days=90))
    pad_msg = ("  scoring window widened by +/-%d d -> %s .. %s"
               % (pad, w0.date(), w1.date())) if pad else None

    # scene-median NDVI per date: what the neighbourhood did
    scene_med = np.nanmedian(ndvi, axis=1)
    anom = ndvi - scene_med[:, None]

    with np.errstate(invalid="ignore"):
        n_obs = np.isfinite(anom[in_ev]).sum(axis=0)
        mean_anom = np.nanmean(anom[in_ev], axis=0)
        pre_ndvi = np.nanmean(ndvi[pre], axis=0) if pre.any() else np.full(ndvi.shape[1], np.nan)
        min_ndvi = np.nanmin(ndvi[in_ev], axis=0) if in_ev.any() else np.full(ndvi.shape[1], np.nan)
    mean_anom[n_obs < MIN_OBS] = np.nan

    ipeak = int(np.argmin(np.abs((dates - peak).days.to_numpy())))
    green_at_peak = ndvi[ipeak]

    # Anchor the scale at zero, not at the AOI minimum. Rescaling between the 2nd and
    # 98th percentile put the MEDIAN field halfway up the ramp, so a whole neighbourhood
    # of ordinary rainfed fields came out orange. Zero anomaly means "behaved exactly like
    # its neighbours" and must read as no signal; only fields ABOVE their neighbours score.
    hi = np.nanpercentile(np.where(mean_anom > 0, mean_anom, np.nan), 95)
    green_index = np.clip(mean_anom / max(hi, 1e-6), 0, 1)

    with np.errstate(invalid="ignore"):
        mean_wfrac = np.nanmean(wfrac[in_ev], axis=0)
    whi = np.nanpercentile(np.where(mean_wfrac > 0.02, mean_wfrac, np.nan), 95)
    flood_index = np.clip(mean_wfrac / max(whi if np.isfinite(whi) else 1.0, 0.02), 0, 1)

    water_use = np.fmax(green_index, flood_index)
    signal = np.where(np.isnan(water_use), "none",
                      np.where(flood_index > green_index, "flooding",
                               np.where(green_index > 0.05, "greenness", "none")))

    out = pd.DataFrame({
        "fid": fields["fid"].to_numpy(),
        "area_ha": fields["area_ha"].to_numpy(),
        "crop_tag": fields["crop_tag"].to_numpy(),
        "mb_name": fields["mb_name"].to_numpy(),
        "is_farm": fields["is_farm"].to_numpy(),
        "is_pivot": fields["is_pivot"].to_numpy() if "is_pivot" in fields else False,
        "is_crop": fields["is_crop"].to_numpy() if "is_crop" in fields else False,
        "n_obs_in_event": n_obs,
        "mean_ndvi_anom": mean_anom.astype("float32"),
        "green_index": green_index.astype("float32"),
        "flood_index": flood_index.astype("float32"),
        "mean_water_frac": mean_wfrac.astype("float32"),
        "water_signal": signal,
        "water_use": water_use.astype("float32"),
        "ndvi_pre_event": pre_ndvi.astype("float32"),
        "ndvi_min_in_event": min_ndvi.astype("float32"),
        "ndvi_drop": (pre_ndvi - min_ndvi).astype("float32"),
        "green_at_peak": green_at_peak.astype("float32"),
        "water_frac_peak": wfrac[ipeak].astype("float32"),
        "water_frac_max": np.nanmax(wfrac, axis=0).astype("float32"),
    })
    out.to_parquet(dc.demo_path(demo, "field_index.parquet"), index=False)

    print("\n%s | %s %d | HYBAS %d"
          % (demo["button"], demo["state"], demo["year"], demo["hybas_id"]))
    print("  event %s -> %s, peak %s | %d composites, %d in the scoring window"
          % (ev0.date(), ev1.date(), peak.date(), len(dates), int(in_ev.sum())))
    if pad_msg:
        print(pad_msg)
    print("  scene NDVI: before window median %.3f -> at peak %.3f"
          % (np.nanmedian(pre_ndvi), np.nanmedian(green_at_peak)))
    if (ev1 - ev0).days > 200:
        print("  note: this event spans seasons, so ndvi_drop is not meaningful here -"
              " the anomaly-vs-neighbours index is what carries the signal")
    scored = out[out.n_obs_in_event >= MIN_OBS]
    print("  fields scored: %d of %d" % (len(scored), len(out)))

    # Divergence is what makes a case worth showing: if every parcel does the same thing
    # there is nothing to look at. Measured as the spread of the NDVI anomaly across
    # parcels during the drought window.
    sp = scored["mean_ndvi_anom"]
    print("  divergence: sd %.3f | p10 %+.3f p90 %+.3f | spread %.3f"
          % (sp.std(), sp.quantile(.1), sp.quantile(.9),
             sp.quantile(.9) - sp.quantile(.1)))
    g = scored.groupby("mb_name")
    summ = pd.DataFrame({
        "n": g.size(),
        "ndvi_anom": g["mean_ndvi_anom"].mean().round(4),
        "green_idx": g["green_index"].mean().round(3),
        "water_frac": g["mean_water_frac"].mean().round(3),
        "flood_idx": g["flood_index"].mean().round(3),
        "water_use": g["water_use"].mean().round(3),
    })
    summ = summ[summ["n"] >= 10].sort_values("n", ascending=False)
    print(summ.to_string())
    print("  dominant signal: %s" % scored.water_signal.value_counts().to_dict())

    # the headline comparison: centre pivots against everything else in the same window
    if "is_pivot" in scored and scored.is_pivot.any():
        piv, rest = scored[scored.is_pivot], scored[~scored.is_pivot]
        print("  CENTRE PIVOTS (%d) vs the rest (%d): NDVI anomaly %+.3f vs %+.3f  ->  gap %+.3f"
              % (len(piv), len(rest), piv.mean_ndvi_anom.mean(),
                 rest.mean_ndvi_anom.mean(),
                 piv.mean_ndvi_anom.mean() - rest.mean_ndvi_anom.mean()))
        print("    water_use index: pivots %.3f vs rest %.3f"
              % (piv.water_use.mean(), rest.water_use.mean()))
    if len(summ) >= 2:
        hi, lo = summ.ndvi_anom.idxmax(), summ.ndvi_anom.idxmin()
        print("  most divergent land uses: %s %+.3f vs %s %+.3f"
              % (hi, summ.ndvi_anom.max(), lo, summ.ndvi_anom.min()))
    top = scored.nlargest(5, "water_use")[
        ["fid", "area_ha", "mb_name", "green_index", "flood_index", "water_use",
         "water_signal"]]
    print("  highest-scoring parcels:")
    print(top.to_string(index=False))
    return out


def main():
    for demo in dc.DEMOS:
        p = dc.demo_path(demo, "ndvi.npz")
        if not os.path.exists(p):
            print("skip %s - no ndvi.npz" % demo["key"])
            continue
        run_demo(demo)


if __name__ == "__main__":
    main()
