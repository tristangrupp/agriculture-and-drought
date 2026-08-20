# Anomalous drought in Brazil, 2019–2025 — CHIRPS × HydroBASINS level 6

A basin-level drought data product for Brazil. It answers three questions with one
consistent baseline:

1. **What is normal?** Average rainfall for every week and every month of the year,
   per basin, from CHIRPS 1990–2010.
2. **Which week-long periods of 2019–2025 were anomalously dry?** Every basin-week
   ranked against the 21 baseline years of that same week.
3. **How many droughts did each basin have in each year, and how long did each last?**
   Day-by-day run lengths, with dry spells that are interrupted by a few wet days kept
   together as one event.

Anomaly is defined against the 1990–2010 baseline *for that time of year*, so a normal
dry season is never reported as a drought.

## Data

| Input | Source | Notes |
|---|---|---|
| CHIRPS v2.0 daily rainfall, 0.05° | `data.chc.ucsb.edu` global daily p05 GeoTIFFs | open, no auth |
| HydroBASINS standard, South America, level 6 | `data.hydrosheds.org` | open, no auth |
| Brazil boundary | Natural Earth 10m admin-0 | used only to select basins |

**Analysis unit.** HydroBASINS level 6, every basin with ≥5% of its area in Brazil:
**1072 basins**, median **151 CHIRPS pixels** each. Statistics are computed over the
*full* basin polygon, not the Brazilian part — a catchment is a hydrological unit, and
clipping it at a political border would bias its mean rainfall. `frac_br` records how
much of each basin lies in Brazil.

**Periods.** Baseline 1990–2010 (21 years). Event period 2019–2025 (7 years). December
of 1989 and of 2018 is also pulled, to seed the 30-day accumulation on 1 January.

## Method

### 1. Rasters never touch the disk

Each global daily GeoTIFF is downloaded, gunzipped, sliced to the Brazil window and
collapsed to one cos(lat)-weighted mean per basin with a single `bincount`, then
discarded. The full 28-year record lands as ~30 MB of (day × basin) matrices instead of
~26 GB of rasters.

### 2. Weekly and monthly climatology

The year is cut into 52 seven-day bins on a fixed 365-day calendar (29 February shares
the 28 February slot, so leap and common years stay aligned; the last bin carries 8
days). For each basin and bin, the mean, sd and 10th/20th/50th percentile of the
rainfall total across the 21 baseline years.

### 3. Variable day-of-year drought threshold

For each of the 365 day-of-year slots, the 20th percentile (moderate) and 10th
percentile (severe) of the 30-day accumulated rainfall **P30**, pooled over ±15 days and
21 years (~630 samples per slot), then smoothed with a 31-day circular moving average.

Because the threshold follows the seasonal cycle, a normal dry season sits *at* its own
threshold and is not flagged. Only rainfall that is low **for that time of year** is.

### 4. Day-by-day detection

A basin-day is in drought when `P30(t) < thr20(doy(t))`. A 30-day accumulation is used
rather than raw daily rainfall because daily CHIRPS is intermittent — a bare "day below
normal" test on daily values measures rain-day frequency, not drought.

Days whose threshold is below `MIN_THR_MM` (5 mm/30 d) are climatologically rainless.
They are marked **not assessable** rather than in drought, so the Caatinga and Cerrado
dry seasons cannot generate false events.

### 5. Pooling — interrupted dry spells stay one drought

Runs of drought days are merged when

* the gap between them is ≤ `POOL_GAP_DAYS` (30 days), **and**
* the surplus rainfall in the gap is < `POOL_RATIO` (20%) of the deficit accumulated in
  the preceding `POOL_LOOKBACK` (90) days of the earlier run.

The gap window equals the accumulation length, because any burst of rain big enough to
lift P30 over the threshold keeps it there for about 30 days by construction; the
**surplus-ratio test**, not the gap length, is what separates a genuine drought-breaking
rain from a brief interruption. Pooled events shorter than `MIN_DURATION` (15 days) are
dropped as noise.

Each event carries `wet_days_absorbed` — how many non-drought days the pooling swallowed
— so the merging is auditable rather than hidden.

## Outputs

`output/`

| File | Contents |
|---|---|
| `weekly_climatology.csv` | basin × 52 weeks: baseline mean, sd, p10/p20/p50 |
| `monthly_climatology.csv` | basin × 12 months: same |
| `weekly_anomalies.csv` | basin × week × year 2019–2025: total, anomaly, percentile, z, `dry_week` flag |
| `drought_events.csv` | one row per event: start, end, duration, deficit, intensity, severe days, wet days absorbed |
| `drought_summary_by_basin_year.csv` | basin × year: n_events, longest_event_days, total_drought_days, deficit |
| `brazil_drought_hydrobasins_lev06_2019_2025.gpkg` | layers `basins`, `basin_year`, `events` — polygons joined to the tables |
| `parquet/*.parquet` | GeoParquet copies of the three layers |
| `brazil_drought_dashboard.html` | self-contained map + per-basin daily trace, no server needed |

`catalog/` — a Portolan/STAC catalog (`brazil-drought-hydrobasins`) wrapping the same
GeoParquet as a cloud-native collection with providers, licence, temporal extent and
per-column descriptions. Geometry is carried once, on the `basins` asset; `basin_year`
is a plain attribute table that joins on `bidx` (repeating the polygon on every
basin-year row cost 53 MB for 7,504 rows of numbers, against 143 KB without it).
PMTiles are not generated - `tippecanoe` has no Windows build - so `portolan check`
reports `pmtiles_recommended`; the catalog is valid without them and the dashboard
covers interactive viewing.

## Crop-label overlay

`crop_exposure.py` places the v9 crop-classification training labels
(`ALL_labels.parquet`, 53,588 fields) inside the basins and grades each field by its
basin's whole-period drought load. **Time is ignored** - a field is scored against
2019-2025 totals, not against the drought running in its own crop year. Matching label
year to drought year is the next step, not this one.

"Was there ever a drought here" does not discriminate: 1066 of 1072 basins had at least
one event, so every matched field would answer yes. Exposure is graded instead by
quartiles of mean drought days per year across all 1072 basins (cut points 56 / 72 / 95
days), with a separate flag for basins that saw an event of 120+ days. Fields are joined
on their representative point, so a field straddling a boundary is counted once.

Outputs land in `output/crop_exposure/`.

## Field-level demo: which parcels kept drinking

Four worked examples, one per drought metric, as four buttons on the dashboard.

| button | basin | where | value | pivots in view |
|---|---|---|---|---|
| Drought days | 6060742400 | São Paulo 2020 | 219 days | 42 |
| Longest event | 6060562010 | Bahia 2020 | 122-day event | 20 |
| Number of events | 6060013190 | Espírito Santo 2023 | 3 events | 2 |
| Rainfall deficit | 6060628070 | Goiás 2023 | 149 mm | 20 |

**Selection** (`pick_dramatic_basins.py`). Rank on the metric; require irrigable cropland
**and at least 15 genuine centre pivots**. Three earlier versions of this screen were wrong
and each failure is worth recording:

1. Requiring rice/sugarcane labels from the same year capped how extreme the cases could be.
2. Screening on *farmland* returned pasture — which is rainfed. A pasture parcel that holds
   its greenness through a drought has deep roots or better soil, not a pump. Three of the
   four basins that version chose held effectively no cropland (one had 0 crop parcels of
   2,192), so the apparent "eucalyptus vs pasture" divergence was a rooting-depth story
   with nothing to say about water abstraction.
3. Detecting pivots by **bounding-rectangle** fill (a circle fills π/4 of one) matched any
   irregular blob and produced hundreds of false positives whose median bounding-**circle**
   fill was 0.50 — less circular than a square. The test is now bounding-circle fill > 0.85
   at 20–200 ha, verified against Oeste da Bahia where real pivots reach 0.98.

Requiring pivots costs extremity, and that trade is itself a finding: Brazil's worst
droughts fall on the Amazon, which has no irrigation, while the irrigated Cerrado and
Sudeste get milder ones. The longest event here is 122 days against 335 in Amazonas.

**Colour.** Hue is what the weather was doing on that date — purple while the basin is in
drought, green while it is not — and depth is the parcel's own NDVI (0.15–0.75). Deep purple
is a full canopy while the rain has failed. Centre pivots carry a blue outline; that is
irrigation infrastructure, not crop type. Cloud gaps are filled by linear interpolation
along time per parcel between the real observations either side, so the animation does not
flicker to grey; the raw observation mask is kept separately and the statistics use only
measured values.

**Result — pivots vs everything else in the same window, on identical rainfall:**

| case | pivots | pivot NDVI anomaly | rest | gap |
|---|---|---|---|---|
| Goiás 2023 | 20 | **+0.107** | +0.005 | **+0.102** |
| Bahia 2020 | 20 | −0.083 | −0.038 | −0.045 |
| São Paulo 2020 | 42 | −0.254 | −0.071 | −0.183 |

**The signal is conditional on phenology.** Goiás works: its drought runs Oct–Dec, inside
the irrigated cropping window, and the pivots hold NDVI a tenth above their neighbours with
a water-use index of 0.525 against 0.183. São Paulo inverts because its drought runs
Mar–May, when pivots sit bare between harvest and winter planting — bare soil reads as the
driest thing in the scene. A pivot only reveals itself when the drought overlaps the season
it is being irrigated for.

**What this is not.** Holding greenness through a drought is consistent with irrigation but
also with deep roots, late planting or wetter soil. The pivot outline is the one piece of
direct evidence here, because it comes from infrastructure shape rather than behaviour.

## Validation

`validate.py` checks the output against droughts that are independently documented:
Amazon Aug-Nov 2023, SE Brazil Apr-Jun 2021, Pantanal Jun-Sep 2024, Rio Grande do Sul
Jan-Mar 2022. The method is not trustworthy if it cannot reproduce droughts nobody
disputes. All four pass.

The test is on **timing**, not on annual totals: during the documented window, at least
half the region's basins must be in drought and the window must be worse than that
region's median month. An earlier version of this test required the documented year to
be the region's *worst* year, and three regions failed it - not because the drought was
missed, but because a later year was worse. The Amazon's documented Aug-Nov 2023 window
has 85% of basins in drought and is plainly detected; 2024 simply carried more drought
days. Being on the historical record is not a claim to being the maximum, so the test
does not treat it as one. The years the model actually ranks worst are reported as a
finding, not suppressed.

`test_engine.py` covers the calendar, accumulation and pooling logic on synthetic
arrays — including the two cases that matter most: a dry spell interrupted by a short
wet week stays one event, and a genuine drought-breaking rain splits it into two.

## Run

```powershell
$crop = "$env:USERPROFILE\AppData\Local\ESRI\conda\envs\crop\python.exe"
$ftw  = "$env:USERPROFILE\AppData\Local\ESRI\conda\envs\ftw\python.exe"

& $crop test_engine.py            # logic checks, no data needed
& $crop hydrobasins.py            # -> data/basins_lev06_br.gpkg, data/basin_index.npz
& $crop download_chirps_daily.py  # -> data/daily/*.npz   (~45-90 min, resumable)
& $crop climatology.py            # -> output/*_climatology.csv, data/thresholds.npz
& $crop detect.py                 # -> output/weekly_anomalies.csv, drought_events.csv, ...
& $crop product.py                # -> output/*.gpkg, output/parquet/
& $crop validate.py               # documented-drought check
& $ftw  dashboard.py              # -> output/brazil_drought_dashboard.html
& $crop summary.py                # headline numbers to stdout

# cloud-native catalogue (portolan CLI, pipx)
cd catalog; portolan init --auto; portolan add brazil_drought_lev06/
portolan check --metadata --fix; cd ..; & $crop stac_meta.py
```

Two conda environments are used because the ESRI `crop` env segfaults on matplotlib and
on network GDAL reads; `ftw` handles rendering. In `crop`, windowed GDAL reads also
crash, which is why each CHIRPS tif is written to a temp file and read whole.

## Parameters

All in `config.py`.

| Name | Value | Meaning |
|---|---|---|
| `ACCUM` | 30 | days in the running accumulation |
| `THR_PCTL` / `THR_PCTL_SEVERE` | 20 / 10 | percentile defining moderate / severe drought |
| `DOY_WINDOW` | ±15 d | days pooled when fitting each day-of-year threshold |
| `THR_SMOOTH` | 31 d | circular smoothing of the threshold curve |
| `MIN_THR_MM` | 5 mm/30 d | below this a basin-day is not assessable |
| `POOL_GAP_DAYS` | 30 | longest gap that can be pooled |
| `POOL_RATIO` | 0.20 | gap surplus allowed, as a fraction of prior deficit |
| `POOL_LOOKBACK` | 90 d | how much of the earlier run counts toward that deficit |
| `MIN_DURATION` | 15 d | shortest event kept |

## Known limits

* CHIRPS blends satellite estimates with gauges; gauge density in the Amazon interior is
  low, so basin means there rest more on the satellite component.
* One coastal basin has no valid CHIRPS pixels (its cells are all ocean-masked) and is
  reported as NaN throughout.
* Events are attributed to the calendar year they start in. Events crossing New Year are
  kept whole and flagged `crosses_new_year`, so a year's `n_events` counts droughts that
  *began* that year while `total_drought_days` counts days that *fell* in it.
* Severity is the share of an event's days below the 10th percentile
  (`severe` >= 0.50, `moderate` >= 0.20, else `mild`) - one axis, not the *relative*
  shortfall. Relative shortfall labelled a year-long Amazon drought "mild" purely because
  a wet basin's threshold is a large number, which is the wrong reading.
* 2019-2025 is drier than the 1990-2010 baseline overall: 22.5% of basin-weeks fall at or
  below the baseline 20th percentile, against the 20% the construction would give a
  stationary climate. Read year-to-year comparisons in that light.
