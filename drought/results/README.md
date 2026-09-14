# Result tables

Small enough to ship with the repo. Everything else — the 224 MB GeoPackage, the 33 MB
weekly anomaly table, the GeoParquet and the STAC catalog — is reproducible by running the
pipeline as described in the root README.

| file | contents |
|---|---|
| `drought_events.csv` | one row per pooled drought event, 12,445 events over 2019-2025 |
| `drought_summary_by_basin_year.csv` | basin x year: events, longest event, drought days, deficit |
| `monthly_climatology.csv` | 1990-2010 baseline per basin per calendar month |
| `weekly_climatology.csv` | 1990-2010 baseline per basin per 7-day bin |

Join key is `bidx`, or `HYBAS_ID` for the HydroBASINS level-6 identifier.
