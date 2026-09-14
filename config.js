/**
 * Everything environment-specific lives here, so moving the field data to a real host is
 * a one-line change. `FIELDS_BASE` is the only thing that must point somewhere serving
 * HTTP range requests - DuckDB reads parquet footers and individual row groups by range,
 * and a server without range support will silently pull whole files instead.
 */
export const CONFIG = {
	/** Base URL for the repacked field parquet. Local static server for now. */
	FIELDS_BASE: './data/fields',

	/** Per-file extents, written by pipeline/repack_fields.py. Used to skip whole files
	 *  before DuckDB is asked anything - the cheapest pruning available. */
	INVENTORY_URL: './data/inventory.json',

	BASINS_URL: './data/basins_2024.geojson',
	EVENTS_URL: './data/events_2024.json',
	META_URL: './data/drought_meta.json',

	/** Written by pipeline/field_drought.py — per-basin field counts and the ranked
	 *  drought-on-cropland shortlist. Optional: the app degrades to the drought-only
	 *  views if they are absent. */
	BASIN_FIELDS_URL: './data/basin_fields_2024.json',
	STRESSED_URL: './data/stressed_basins.json',

	/** Written by pipeline/field_response.py - per-field NDVI behaviour through each
	 *  basin's 2024 drought. Also optional. */
	RESPONSE_INDEX_URL: './data/response/response_index.json',
	RESPONSE_BASE: './data/response',

	/** The five shipped example windows. Locally these sit alongside the response
	 *  layers; the public build copies them into data/examples/. */
	EXAMPLES_URL: './data/examples/index.json',
	EXAMPLES_BASE: './data/examples',

	YEAR: 2024,

	/** Below this zoom the map shows basins only. Field queries at continental zoom would
	 *  return millions of rows and prune nothing useful. */
	FIELD_MIN_ZOOM: 10.5,

	/** Hard ceiling on rows returned to the browser for drawing. */
	MAX_FIELDS: 12000,

	/** Largest viewport edge, in degrees, a field query may span. Keeps the row-group
	 *  reads bounded no matter how far the user zooms out mid-query. */
	MAX_QUERY_DEG: 1.2,

	/** MapBiomas classes, for styling and for the palette's plain-language filters. */
	MB_CLASSES: {
		3: 'forest', 4: 'savanna', 5: 'mangrove', 6: 'flooded forest',
		9: 'planted forest', 11: 'wetland', 12: 'grassland', 15: 'pasture',
		18: 'agriculture', 19: 'temporary crop', 20: 'sugarcane',
		21: 'mosaic ag/pasture', 23: 'beach', 24: 'urban', 25: 'other non-vegetated',
		26: 'water', 29: 'rocky outcrop', 30: 'mining', 31: 'aquaculture',
		32: 'salt flat', 33: 'river/lake', 35: 'oil palm', 36: 'perennial crop',
		39: 'soybean', 40: 'rice', 41: 'other temporary crop', 46: 'coffee',
		47: 'citrus', 48: 'other perennial', 49: 'wooded sandbank', 50: 'sandbank',
		62: 'cotton'
	},

	/** Classes that count as cropland when the palette says "cropland". */
	CROP_CLASSES: [18, 19, 20, 35, 36, 39, 40, 41, 46, 47, 48, 62],

	DROUGHT_METRICS: [
		{ key: 'total_drought_days', label: 'Days in drought', unit: 'days' },
		{ key: 'longest_event_days', label: 'Longest event', unit: 'days' },
		{ key: 'n_events', label: 'Number of events', unit: '' },
		{ key: 'total_deficit_mm', label: 'Rainfall deficit', unit: 'mm' }
	]
};
