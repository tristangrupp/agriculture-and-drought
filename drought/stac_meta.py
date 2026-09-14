"""Fill in the STAC metadata that `portolan init` leaves generic.

`portolan init` writes a placeholder catalog id and description and cannot know the
temporal extent or what the columns mean. This script sets them from the pipeline's own
config and tables, so the catalog describes the data rather than the tool that made it.

Run after `portolan add`, in any python with the standard library:
    python stac_meta.py
"""
import datetime as dt
import json
import os

import config as cfg

CATALOG = os.path.join(cfg.HERE, "catalog")
COLLECTION = os.path.join(CATALOG, "brazil_drought_lev06")

CATALOG_ID = "brazil-drought-hydrobasins"
CATALOG_DESC = (
    "Anomalous drought in Brazil, 2019-2025, at HydroBASINS level 6. Drought is defined "
    "against a CHIRPS v2.0 1990-2010 baseline that varies with day of year, so normal dry "
    "seasons are excluded and only rainfall that is low for its time of year is reported."
)

COLUMN_DOCS = {
    "basins": {
        "bidx": "row index used to join the other assets",
        "HYBAS_ID": "HydroBASINS level-6 identifier",
        "SUB_AREA": "basin area, km2 (HydroBASINS)",
        "frac_br": "fraction of the basin's area inside Brazil",
        "n_events_total": "drought events 2019-2025",
        "drought_days_total": "days in drought 2019-2025",
        "deficit_mm_total": "accumulated rainfall deficit, mm",
        "years_with_drought": "how many of the 7 years had at least one event",
        "max_event_days": "longest single event, days",
        "worst_year": "year with the most drought days",
        "worst_year_days": "drought days in that year",
        "mean_drought_days_per_year": "drought_days_total / 7",
    },
    "basin_year": {
        "bidx": "join key to the basins asset",
        "HYBAS_ID": "HydroBASINS level-6 identifier",
        "year": "calendar year, 2019-2025",
        "n_events": "events that STARTED in this year",
        "longest_event_days": "longest of those events",
        "total_drought_days": "days in this year that fell inside any event",
        "total_deficit_mm": "rainfall deficit of the events starting this year, mm",
        "max_intensity": "largest relative shortfall (threshold - P30) / threshold",
        "assessable_days": "days this year whose threshold exceeded 5 mm/30 d",
    },
    "events": {
        "bidx": "join key to the basins asset",
        "start": "first day in drought",
        "end": "last day in drought",
        "duration_days": "length of the pooled event",
        "start_year": "calendar year the event began in",
        "crosses_new_year": "true if the event spans 31 December",
        "deficit_mm": "sum of the daily shortfall over the event, mm",
        "mean_intensity": "mean relative shortfall (threshold - P30) / threshold",
        "max_intensity": "peak relative shortfall",
        "peak_date": "day of the peak shortfall",
        "severe_days": "days below the 10th-percentile threshold",
        "severe_day_share": "severe_days / duration_days",
        "severity": "severe (>=0.50 share), moderate (>=0.20), mild",
        "duration_class": "short 15-29 d / moderate 30-59 d / long 60-119 d / extreme 120+ d",
        "wet_days_absorbed": "non-drought days the pooling rule merged into this event",
        "min_p30_mm": "lowest 30-day rainfall total during the event, mm",
    },
}

PROVIDERS = [
    {"name": "Climate Hazards Center, UC Santa Barbara",
     "roles": ["producer", "licensor"],
     "url": "https://www.chc.ucsb.edu/data/chirps",
     "description": "CHIRPS v2.0 daily precipitation, 0.05 degrees"},
    {"name": "WWF / HydroSHEDS",
     "roles": ["producer", "licensor"],
     "url": "https://www.hydrosheds.org/products/hydrobasins",
     "description": "HydroBASINS standard, South America, level 6"},
]


def main():
    cat_path = os.path.join(CATALOG, "catalog.json")
    cat = json.load(open(cat_path, encoding="utf-8"))
    cat["id"] = CATALOG_ID
    cat["description"] = CATALOG_DESC
    json.dump(cat, open(cat_path, "w", encoding="utf-8"), indent=2)

    col_path = os.path.join(COLLECTION, "collection.json")
    col = json.load(open(col_path, encoding="utf-8"))
    y0, y1 = min(cfg.EVENT_YEARS), max(cfg.EVENT_YEARS)
    col["title"] = "Brazil drought events, HydroBASINS level 6, %d-%d" % (y0, y1)
    col["description"] = CATALOG_DESC
    col["license"] = "CC-BY-4.0"
    col["providers"] = PROVIDERS
    col["extent"]["temporal"]["interval"] = [[
        dt.datetime(y0, 1, 1).isoformat() + "Z",
        dt.datetime(y1, 12, 31, 23, 59, 59).isoformat() + "Z",
    ]]
    col["summaries"] = {
        "baseline_period": "1990-2010",
        "event_period": "%d-%d" % (y0, y1),
        "accumulation_days": cfg.ACCUM,
        "threshold_percentile": cfg.THR_PCTL,
        "severe_threshold_percentile": cfg.THR_PCTL_SEVERE,
        "min_event_duration_days": cfg.MIN_DURATION,
        "pooling": "gap <= %d d and gap surplus < %.0f%% of the deficit in the preceding %d d"
                   % (cfg.POOL_GAP_DAYS, 100 * cfg.POOL_RATIO, cfg.POOL_LOOKBACK),
        "not_assessable_below_mm_per_30d": cfg.MIN_THR_MM,
    }
    for key, docs in COLUMN_DOCS.items():
        if key in col.get("assets", {}):
            col["assets"][key].setdefault("description", "")
            col["assets"][key]["description"] = " | ".join(
                "%s: %s" % (k, v) for k, v in docs.items())
    json.dump(col, open(col_path, "w", encoding="utf-8"), indent=2)

    print("catalog id   : %s" % cat["id"])
    print("collection   : %s" % col["title"])
    print("temporal     : %s" % col["extent"]["temporal"]["interval"][0])
    print("assets       : %s" % ", ".join(col.get("assets", {})))


if __name__ == "__main__":
    main()
