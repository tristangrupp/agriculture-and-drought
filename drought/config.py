"""Shared paths, grid constants and analysis periods for the Brazil CHIRPS drought pipeline."""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
OUT_DIR = os.path.join(HERE, "output")
DAILY_DIR = os.path.join(DATA_DIR, "daily")
for _d in (DATA_DIR, OUT_DIR, DAILY_DIR):
    os.makedirs(_d, exist_ok=True)

# --- CHIRPS v2.0 global 0.05 deg grid ----------------------------------------
CHIRPS_RES = 0.05
CHIRPS_X0 = -180.0          # west edge of the global grid
CHIRPS_Y0 = 50.0            # north edge of the global grid
CHIRPS_NODATA = -9999.0
CHIRPS_DAILY_URL = ("https://data.chc.ucsb.edu/products/CHIRPS-2.0/global_daily/"
                    "tifs/p05/%d/chirps-v2.0.%d.%02d.%02d.tif.gz")

# --- analysis periods ---------------------------------------------------------
BASELINE_YEARS = range(1990, 2011)    # 1990-2010 inclusive, climatology
EVENT_YEARS = range(2019, 2026)       # 2019-2025 inclusive, drought detection
SPINUP_DAYS = 30                      # days pulled before each period for P30

# --- drought definition -------------------------------------------------------
ACCUM = 30              # days in the running accumulation P30
THR_PCTL = 20.0         # moderate-drought threshold percentile of baseline P30
THR_PCTL_SEVERE = 10.0
DOY_WINDOW = 15         # +/- days pooled when fitting the day-of-year threshold
THR_SMOOTH = 31         # circular moving-average window applied to the threshold
MIN_THR_MM = 5.0        # below this the basin-day is "not assessable" (normal dry season)
POOL_GAP_DAYS = 30      # merge two events separated by <= this many non-drought days
POOL_RATIO = 0.20       # ...only if the gap surplus is < this fraction of the deficit
POOL_LOOKBACK = 90      # only this much of the earlier event's deficit counts in that test
MIN_DURATION = 15       # discard pooled events shorter than this

# --- outputs ------------------------------------------------------------------
BASINS_GPKG = os.path.join(DATA_DIR, "basins_lev06_br.gpkg")
BASIN_INDEX = os.path.join(DATA_DIR, "basin_index.npz")
THRESHOLDS = os.path.join(DATA_DIR, "thresholds.npz")
PRODUCT_GPKG = os.path.join(OUT_DIR, "brazil_drought_hydrobasins_lev06_2019_2025.gpkg")


def daily_npz(year):
    return os.path.join(DAILY_DIR, "chirps_basin_daily_%d.npz" % year)
