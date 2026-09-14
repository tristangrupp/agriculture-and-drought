"""The four field-level demo cases, one per drought metric.

Each case is the most extreme basin-year on its metric that sits on IRRIGABLE CROPLAND and
has field boundaries on F: for that state and year. See pick_dramatic_basins.py.

Two earlier versions of this selection were wrong. The first required rice or sugarcane
training labels in the same year, which capped how extreme the cases could be. The second
screened on farmland in general - and returned pasture, which is rainfed: a pasture parcel
that keeps its greenness through a drought has deep roots or better soil, not a pump, so it
cannot demonstrate anything about water abstraction. Of the four basins that version chose,
three held effectively no cropland at all (one had 0 crop parcels out of 2,192).

The screen now counts only temporary and permanent crop classes and requires at least 15
genuine centre pivots - a circle in a field-boundary layer is a pump, and it is the one
land use that proves irrigation from shape alone.

Requiring pivots costs extremity, and that trade is the honest finding: Brazil's most
extreme droughts fall on the Amazon, which has no irrigation, while the irrigated Cerrado
and Sudeste see milder ones. The longest event here is 122 days against 335 in Amazonas.
These are the basins where "who kept pumping" is a real question.
"""
import os

import config as cfg

DEMO_DIR = os.path.join(cfg.OUT_DIR, "demo")
FIELD_DIR = r"F:\Trazo Fields v2\field boundaries"
os.makedirs(DEMO_DIR, exist_ok=True)

# AOI is a square window in degrees, placed on the densest cluster of AGRICULTURAL fields.
AOI_DEG = 0.22          # ~24 km at these latitudes
MIN_FIELD_HA = 1.0      # ignore slivers; keeps the animation and the S2 zonal stats sane
LABEL_OVERLAP = 0.40    # share of a boundary field covered by a label polygon to tag it
# Irrigable crop classes only when placing the AOI. Pasture (15), planted forest (9),
# mosaic (21) and grassland (12) are excluded: they are rainfed, so greenness held through
# a drought there reflects rooting depth or soil, not water being applied.
CROP_CLASSES = {18, 19, 20, 35, 36, 39, 40, 41, 46, 47, 48, 62}
# Centre pivots: a true circle fills 1.0 of its minimum bounding CIRCLE, a square only
# 0.637. An earlier version tested the bounding RECTANGLE instead - a circle fills pi/4 of
# one - but so do many irregular blobs, so it returned hundreds of false positives whose
# median circle-fill was 0.50, i.e. less circular than a square. Verified against Oeste da
# Bahia, where real pivots reach 0.98.
PIVOT_CIRC_MIN = 0.85
PIVOT_HA_LO, PIVOT_HA_HI = 20.0, 200.0

DEMOS = [
    {
        "key": "drought_days",
        "button": "Drought days example",
        "metric": "total_drought_days",
        "metric_label": "Drought days",
        "hybas_id": 6060742400,
        "state": "São Paulo",
        "state_file": "Sao_Paulo",
        "year": 2020,
        "note": "219 days in drought across 3 events, over 3887 crop parcels and 49 centre pivots",
    },
    {
        "key": "longest_event",
        "button": "Longest event example",
        "metric": "longest_event_days",
        "metric_label": "Longest event (days)",
        "hybas_id": 6060562010,
        "state": "Bahia",
        "state_file": "Bahia",
        "year": 2020,
        "note": "one unbroken 122-day drought over irrigated western Bahia cropland",
    },
    {
        "key": "n_events",
        "button": "Number of events example",
        "metric": "n_events",
        "metric_label": "Number of events",
        "hybas_id": 6060013190,
        "state": "Espírito Santo",
        "state_file": "Espirito_Santo",
        "year": 2023,
        "note": "3 separate droughts in one year over irrigated coffee and sugarcane country",
    },
    {
        "key": "deficit",
        "button": "Rainfall deficit example",
        "metric": "total_deficit_mm",
        "metric_label": "Rainfall deficit (mm)",
        "hybas_id": 6060628070,
        "state": "Goiás",
        "state_file": "Goias",
        "year": 2023,
        "note": "149 mm of accumulated deficit over Cerrado pivot irrigation",
    },
]

BY_KEY = {d["key"]: d for d in DEMOS}


def boundary_path(demo):
    return os.path.join(FIELD_DIR, "Brazil_%s_%d.gpkg" % (demo["state_file"], demo["year"]))


def demo_path(demo, name):
    d = os.path.join(DEMO_DIR, demo["key"])
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)
