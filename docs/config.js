/**
 * Public build - see pipeline/build_site.py.
 *
 * This is NOT the local config. The field parquet and the DuckDB reader that queries it
 * do not ship: trazo4 is unpublished and 10.3 GB. What ships is five pre-scored example
 * windows as plain GeoJSON, so the site is fully static.
 */
export const CONFIG = {
	STATIC: true,

	BASINS_URL: './data/basins_2024.geojson',
	EVENTS_URL: './data/events_2024.json',
	META_URL: './data/drought_meta.json',
	BASIN_FIELDS_URL: './data/basin_fields_2024.json',
	STRESSED_URL: './data/stressed_basins.json',

	/** The five example windows, written by pipeline/build_site.py. */
	EXAMPLES_URL: './data/examples/index.json',
	EXAMPLES_BASE: './data/examples',

	YEAR: 2024,

	/** Unused in the static build - kept so the shared app.js needs no branching. */
	FIELD_MIN_ZOOM: 10.5,
	MAX_FIELDS: 12000,
	MAX_QUERY_DEG: 1.2,

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

	CROP_CLASSES: [18, 19, 20, 35, 36, 39, 40, 41, 46, 47, 48, 62],

	DROUGHT_METRICS: [
		{ key: 'total_drought_days', label: 'Days in drought', unit: 'days' },
		{ key: 'longest_event_days', label: 'Longest event', unit: 'days' },
		{ key: 'n_events', label: 'Number of events', unit: '' },
		{ key: 'total_deficit_mm', label: 'Rainfall deficit', unit: 'mm' }
	]
};
