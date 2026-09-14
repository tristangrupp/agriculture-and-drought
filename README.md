# Drought & Fields

A map where the basin-scale drought product meets the field-scale crop data: 1,072
HydroBASINS level-6 basins carrying 2024 CHIRPS drought statistics, over 29.2 million
Brazilian trazo4 field polygons queried live from the browser.

Terminology: trazo4 delineates **fields** (cropped plots). A **parcel** is a land
holding — a different unit, and not what this data contains.

> **The trazo4 field corpus is unpublished** — no peer-reviewed publication yet — and must
> not be pushed to Source Cooperative or any other public host. `data/fields/` and
> `data/inventory.json` are gitignored for that reason.
>
> **Five example windows are an authorised exception.** They ship publicly from `docs/`:
> roughly 25,000 field polygons out of 29.2 million, already scored, carrying no attribute
> beyond crop class, area and the NDVI result. A deliberate subset, released on the data
> owner's instruction. It is not the corpus and must not be described as one.

Built on the patterns in WRI's
[ai-web-map-exploration](https://wri.github.io/applied-ai-experiments/ai-web-map-exploration/command-palette)
experiments — the command palette, lazy DuckDB-WASM boot, file-extent pruning and
constrained-SQL guards are all lifted from there.

## Running it

```powershell
python serve.py            # http://127.0.0.1:8800  — range requests enabled
```

Then open the page and press <kbd>⌘K</kbd> / <kbd>Ctrl K</kbd>.

`serve.py` exists because Python's stock static handler **ignores `Range` headers**. Without
206 support every "read one row group" turns into a full-file download, which quietly
destroys the entire point of the architecture.

## The two halves, loaded very differently

| | Drought basins | Fields |
|---|---|---|
| size | 1,072 features, 2.1 MB | 29.2 M polygons, 10.3 GB |
| transport | ships whole as GeoJSON | queried per viewport by DuckDB-WASM |
| filtering | instant, client-side | SQL over HTTP range requests |

The basin layer is small enough that every recolour and filter is local and immediate.
The field layer must never be loaded whole, so it is read straight out of GeoParquet by
DuckDB compiled to WebAssembly, one viewport at a time.

## Making 29 M polygons queryable from a browser

Three levels of pruning, cheapest first:

1. **File** — `data/inventory.json` carries each file's extent, so files that cannot
   intersect the viewport are never named in the query.
2. **Row group** — the parquet is Hilbert-sorted and written in 15,000-row groups, so
   each group's bbox statistics are tight and DuckDB skips nearly all of them.
3. **Row** — the `geom_bbox` predicate itself.

Step 2 is why `pipeline/repack_fields.py` exists. The raw trazo4 files store rows in
arbitrary order, so a single row group spans nearly a whole state and a viewport predicate
prunes *nothing* — one map pan would pull the entire file. Measured on Brazil_Acre: mean
row-group x-span falls from **5.33° to 1.96°** over a 7.15° extent after sorting, which is
the expected optimum for twelve groups in two dimensions. Large states get hundreds of
groups and correspondingly tighter boxes.

Geometry is **93% of the bytes**. Attributes plus bbox plus centroid for all 236 M fields
across all seven years and every country come to roughly 6.6 GB; with polygons, about
80 GB. Worth knowing if the scope ever widens.

## Pipeline

```powershell
$crop = "$env:USERPROFILE\AppData\Local\ESRI\conda\envs\crop\python.exe"

& $crop pipeline\repack_fields.py --year 2024      # E:\trazo4_fields_parquet -> data\fields
& $crop pipeline\build_drought_layer.py --year 2024 # drought product -> data\basins_2024.geojson
& $crop pipeline\field_drought.py                   # fields x basins -> basin_fields_2024.json
& $ftw  pipeline\field_response.py                  # Sentinel-2 per field -> data\response\
```

`field_response.py` must run in the `ftw` env, not `crop`: it reads COGs over the network
through GDAL, which crashes in `crop`.

`repack_fields.py` is resumable — it skips files already written — and updates
`data/inventory.json` after each file, so an interrupted run leaves a usable catalogue.

### Source-schema traps, found the hard way

The trazo4 schema is **not uniform across files**, and a first run failed on all 34:

- the MapBiomas class column is year-suffixed: `mbmode24` for 2024, not `mbmode23`
- `country`, `state` and `year` are present in only **9 of 34** files
- `area_ha` is present in 23 of 34

Region therefore comes from the *filename*, which is reliable everywhere, and every
optional column is probed with `DESCRIBE` before it is selected.

## From basin drought to field drought

The basin layer says a drought happened. Two pipeline steps ask what the fields inside it
did about it.

**`field_drought.py` — where the drought actually lands on farmland.** A point-in-polygon
join of every field centroid against the basin layer: **29,223,909 of 29,229,588 fields
placed, 100.0%**, in 534 s. The 5,679 misses are coastal fields outside the basin coverage.
That gives **38.9 M ha** of cropland attributed to basins, and three new map metrics —
water stress, cropland exposure, cropland area.

Ranking by drought-on-cropland rather than by drought alone changes the answer completely.
The Amazon basins that top every raw drought metric drop out, because almost nothing is
farmed in them. What rises instead:

| HYBAS | drought days | longest event | cropland ha | crop fields | stress |
|---|---|---|---|---|---|
| 6060492990 | 183 | 57 | 1,632,292 | 16,425 | 0.53 |
| 6060718180 | 181 | 94 | 902,856 | 81,973 | 0.60 |
| 6060501000 | 109 | 43 | 1,296,546 | 17,353 | 0.35 |
| 6060739950 | 221 | 120 | 347,048 | 38,136 | 0.77 |

Stress is a composite of duration, longest unbroken event and accumulated deficit, each
scaled to its own 95th percentile so no unit dominates. Exposure is stress times the
cropland actually inside the basin.

**`field_response.py` — what individual fields did.** For the top basins it pulls
Sentinel-2 L2A from Planetary Computer across the drought window ±45 days and builds
~10-day SCL-masked composites at 20 m. Each field then scores as **its NDVI minus the
median NDVI of every field in the same window on the same date**, averaged over the event.

Comparing a field against its neighbours *on one date* cancels the season, the drought
itself, atmospheric correction and sun angle. What survives is this field against its
neighbours under identical rainfall.

### Every field is drawn; not every field is scored

An absent polygon and an unmeasured one look identical on a map, and a reader will assume
the first. The examples originally shipped only *scored* fields - cropland over 2 ha with
enough clear satellite looks - so pasture, woodland and small plots were simply not in the
file. In the Bahia window that meant 3,254 polygons drawn out of 59,834 that trazo4
actually delineated. It read as a field model that had missed almost everything, which is
the opposite of what happened.

Display and measurement are now separate:

| | what it covers |
|---|---|
| drawn | every field trazo4 delineated inside the window |
| scored | cropland over 2 ha with at least 4 clear composites |

Unscored fields ship with a null score and a pale neutral fill, the panel states both
counts, and clicking one names the reason: not cropland, under 2 ha, or too few clear
satellite looks. Windows are sized by *total* field count rather than scoreable count, so
shipping everything stays affordable - the dense northeastern windows would otherwise
carry three times the geometry budgeted for them.

Unscored geometry is also simplified harder than scored geometry (28 m against 11 m). It
exists to show that coverage is complete, nobody reads its exact edge, and in most windows
it is the majority of the payload.

### Cloud, and the cost of a flat scene budget

A first attempt capped every 10-day composite at its two least-cloudy scenes, which cut
the download roughly threefold. It also cut coverage exactly where cloud is worst: Parana
scored 638 of 8,585 candidate fields, Pernambuco 3,960 of 10,197. Two half-clouded looks
leave most fields under the 30% valid-pixel floor.

The budget now keeps adding scenes until a pixel is 97% likely to be seen clear at least
once, to a hard cap of eight. Clear regions still take two and stay cheap; cloudy ones
take what they need. The fix for "not enough clear looks" is more looks, not a lower bar
for what counts as one.

### Coverage, and what the grey fields are

Scoring runs on a 0.20 degree window (~22 km) placed on the densest cluster of crop fields
in the basin, not on the whole basin. At 20 m that window is already a 1,174 x 1,140 grid
per band per composite; a whole basin would be hundreds of times that.

The map therefore draws many more fields than it scores, and the unscored ones are grey.
**None of them are grey because of cloud.** Every cropland field over 2 ha inside the window
scored in all three basins - 719/719, 2,564/2,564, 3,274/3,274. The 10-day median composites
give enough clear looks that no field runs short. Taking basin 6060718180 as the example,
of 7,177 fields drawn in that view:

| | fields |
|---|---|
| scored | 2,471 |
| outside the scoring window | 2,674 |
| not cropland | 1,446 |
| under the 2 ha floor | 586 |

The tooltip names which of those applies to each grey field, and the window is drawn as a
dashed rectangle, because the first version said "too cloudy to score" and that was simply
wrong.

The real cost is basin coverage: 6060718180 holds 68,152 crop fields and the window scores
2,564 of them. The window is representative of its own neighbourhood, not of the basin.
Widening it is a compute decision, not a method change.

### What this can and cannot say

The method and its limits come from the Oregon validation of the same index against
measured consumptive use (`E:\Water\Oregan\drought-ndvi-validation`):

- Holding greenness while the neighbourhood browns means water is coming from somewhere
  other than that month's rain. That is **consistent with** irrigation — and also with
  deeper roots, a later planting, or wetter soil. It ranks candidates; it does not prove
  abstraction.
- Against Oregon's measured use the index tracked water use **per acre** well (rho
  0.5–0.9) but did **not** rank total volume better than field area alone. So the output
  here is deliberately about intensity and timing, never volume.
- The signal only exists where the drought overlaps the growing season. Each layer records
  `season_overlap`, the median scene NDVI during the event, so a flat result reads as
  "nothing was growing" rather than "nothing drew water".

### The three scored basins

| basin | drought window | days | fields | scene NDVI | p10 | p90 |
|---|---|---|---|---|---|---|
| 6060492990 Mato Grosso | 2023-10-18 to 2024-03-26 | 161 | 719 | 0.45 | −0.128 | +0.065 |
| 6060718180 São Paulo | 2024-05-13 to 2024-08-14 | 94 | 2,564 | 0.26 | −0.108 | +0.106 |
| 6060739950 São Paulo | 2024-04-26 to 2024-08-23 | 120 | 3,274 | 0.35 | −0.169 | +0.099 |

6060492990 is the wet-season failure: the drought runs straight through the soy season with
scene NDVI at 0.45 the whole way, so there was a full crop to lose. Its fields span roughly
0.19 NDVI under identical rain.

The two São Paulo basins are dry-season events, and they corrected an error in how this
page first read them. Scoring `season_overlap` alone, a low median NDVI meant "nothing was
growing" and the result was null. But 6060718180 browns off to 0.26 while carrying the
**widest positive tail of the three** at +0.106. Fields staying green while everything
around them burns off is the clearest signature this method produces, not the weakest.

So the panel reads two numbers together: cover says whether the neighbourhood had anything
to lose, the tail says whether some fields kept it anyway. Low cover with a wide tail is a
strong result; low cover with a flat distribution is the genuine null. A later planting
date would still look identical to irrigation in both cases.

## Layout

```
drought-app/
  index.html          shell
  config.js           every environment-specific value, incl. FIELDS_BASE
  src/app.js          map, layers, panel, palette wiring
  src/fields.js       DuckDB-WASM, pruning, WKB decode
  src/commands.js     command registry and the core command set
  src/app.css         light theme matched to the WRI reference
  vendor/             MapLibre, served same-origin (see below)
  pipeline/           repack, drought layer, field join, Sentinel-2 response
  data/               basins, events, inventory, fields/*.parquet, response/
  serve.py            static server with Range support
```

## Two things that will bite anyone editing this

**MapLibre is vendored, not CDN-loaded.** MapLibre tiles GeoJSON in a web worker, and a
cross-origin script cannot spawn that worker. Loaded from unpkg the map paints its
background, reports no error, and then silently never renders a single data layer. It cost
an afternoon; keep `vendor/maplibre-gl.js` local.

**Geometry is decoded from WKB in JavaScript**, not by DuckDB's spatial extension. Loading
`spatial` into the wasm build is another ~10 MB and another failure mode, and a polygon
decoder is forty lines (`src/fields.js`).

## The public build

`docs/` is a self-contained static site, published with GitHub Pages. It is the same
interface backed by different data, because none of the local architecture survives the
trip: the corpus is unpublished, it is 10.3 GB, and Pages is a static host with no DuckDB
and no parquet.

```powershell
& $ftw pipelineuild_site.py          # -> docs/
```

| | local | published |
|---|---|---|
| fields | 29.2 M, queried per viewport | ~25,000 in five fixed windows |
| transport | DuckDB-WASM over range requests | plain GeoJSON |
| answers | "show me the fields here" | "show me these five places" |
| needs | a server with 206 support | nothing |

The five windows span five farming systems — the Mato Grosso soy frontier, the São Paulo
sugarcane belt, the double-cropped Paraná south, Minas Gerais coffee, and the semi-arid
northeast — so they differ in field size, crop calendar and drought season rather than only
in location.

Window size follows field count, not degrees: a fixed window gave 719 fields in Mato Grosso
and 3,274 in São Paulo, because median field size differs by an order of magnitude between
them. Resolution then follows window size, holding every grid near 2,000 px a side. Fine
resolution buys nothing once a field is hundreds of pixels across; it only buys download
time.

### Publishing it

GitHub Pages, `main` branch, `/docs` folder — no Actions, no build step on their side.
`docs/.nojekyll` is written by the build because Jekyll silently drops paths beginning with
an underscore, and a silently missing data file is the worst kind of 404.

### Hosting the full corpus, later

`config.js → FIELDS_BASE` is the single line to edit. The only hard requirement of a host
is HTTP range-request support; without it every "read one row group" becomes a full-file
download. Nothing else in the app knows where the data lives.

## Status

- **Brazil, 2024.** 27 state files, 29,229,588 fields, 10.3 GB. The pipeline takes
  `--year` and `--only`, so other years are one command each.
- The seven non-Brazilian regions were repacked and then removed: the drought layer is
  Brazil-only, so fields outside it would sit on the map with no basin behind them.
- The palette's AI fallback row is drawn but not wired to a model — the reference uses
  `byo-keys` for that, and no key is configured here.
