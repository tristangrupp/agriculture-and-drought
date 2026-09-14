/**
 * The field data layer: DuckDB-WASM reading repacked GeoParquet over HTTP range requests.
 *
 * Three levels of pruning, cheapest first, which is what makes 36.5 million polygons
 * queryable from a browser at all:
 *
 *   1. file    - the inventory carries each file's extent, so files that cannot intersect
 *                the viewport are never named in the query
 *   2. row group - the parquet was Hilbert-sorted and written in 15k-row groups, so the
 *                bbox statistics per group are tight and DuckDB skips almost all of them
 *   3. row     - the bbox predicate itself
 *
 * Geometry comes back as WKB and is decoded here rather than by the spatial extension.
 * That is deliberate: loading `spatial` into the wasm build is an extra ~10 MB and an
 * extra failure mode, and a polygon decoder is forty lines.
 */

import { CONFIG } from '../config.js';

const DUCKDB_CDN = 'https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@1.32.0/+esm';

let dbPromise = null;
let inventory = null;

/** Lazy boot. The wasm bundle is ~35 MB, so nothing loads until a field query happens. */
export function getDB() {
	dbPromise ??= init();
	return dbPromise;
}

/** Drop a poisoned instance: once the wasm heap overflows, every later query fails. */
export function resetDB() {
	dbPromise?.then((d) => d.db.terminate()).catch(() => {});
	dbPromise = null;
}

async function init() {
	const duckdb = await import(/* @vite-ignore */ DUCKDB_CDN);
	const bundle = await duckdb.selectBundle(duckdb.getJsDelivrBundles());
	// Same-origin worker shim around the CDN worker script; a bare cross-origin Worker
	// URL is blocked by the browser.
	const workerUrl = URL.createObjectURL(
		new Blob([`importScripts("${bundle.mainWorker}");`], { type: 'text/javascript' })
	);
	try {
		const worker = new Worker(workerUrl);
		const db = new duckdb.AsyncDuckDB(
			new duckdb.ConsoleLogger(duckdb.LogLevel.WARNING),
			worker
		);
		await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
		const conn = await db.connect();
		return { duckdb, db, conn };
	} finally {
		URL.revokeObjectURL(workerUrl);
	}
}

export async function getInventory() {
	if (inventory) return inventory;
	const r = await fetch(CONFIG.INVENTORY_URL);
	if (!r.ok) throw new Error(`inventory ${r.status} — has the repack run?`);
	inventory = (await r.json()).filter((e) => e.year === CONFIG.YEAR);
	return inventory;
}

/** Files whose extent intersects the bbox. Pruning step one. */
export function filesFor(inv, b) {
	return inv.filter(
		(e) => e.xmin <= b.e && e.xmax >= b.w && e.ymin <= b.n && e.ymax >= b.s
	);
}

const abs = (u) =>
	u.startsWith('http') ? u : new URL(u, window.location.href).href;

export function urlsFor(files) {
	return files.map((f) => `${abs(CONFIG.FIELDS_BASE)}/${f.file}`);
}

/** Clamp a viewport to MAX_QUERY_DEG per edge, centred, so a zoomed-out query cannot
 *  ask for a continent's worth of row groups. */
export function clampBbox(b) {
	const cx = (b.w + b.e) / 2;
	const cy = (b.s + b.n) / 2;
	const hw = Math.min((b.e - b.w) / 2, CONFIG.MAX_QUERY_DEG / 2);
	const hh = Math.min((b.n - b.s) / 2, CONFIG.MAX_QUERY_DEG / 2);
	const clamped = hw < (b.e - b.w) / 2 - 1e-9 || hh < (b.n - b.s) / 2 - 1e-9;
	return {
		bbox: { w: cx - hw, e: cx + hw, s: cy - hh, n: cy + hh },
		clamped
	};
}

/* ------------------------------------------------------------------ WKB decode */

/**
 * Minimal WKB reader for Polygon and MultiPolygon, which is all the field data contains.
 * Returns GeoJSON coordinates. Rings are emitted as-is; MapLibre does not care about
 * winding order for fills.
 */
export function wkbToGeoJSON(bytes) {
	const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
	let p = 0;
	const u8 = () => dv.getUint8(p++);
	let le = true;
	const u32 = () => {
		const v = dv.getUint32(p, le);
		p += 4;
		return v;
	};
	const f64 = () => {
		const v = dv.getFloat64(p, le);
		p += 8;
		return v;
	};
	const ring = () => {
		const n = u32();
		const out = new Array(n);
		for (let i = 0; i < n; i++) out[i] = [f64(), f64()];
		return out;
	};
	const polygon = () => {
		const n = u32();
		const out = new Array(n);
		for (let i = 0; i < n; i++) out[i] = ring();
		return out;
	};

	le = u8() === 1;
	const type = u32() & 0xff;
	if (type === 3) return { type: 'Polygon', coordinates: polygon() };
	if (type === 6) {
		const n = u32();
		const polys = new Array(n);
		for (let i = 0; i < n; i++) {
			le = u8() === 1;
			u32(); // per-part type, always polygon here
			polys[i] = polygon();
		}
		return { type: 'MultiPolygon', coordinates: polys };
	}
	return null;
}

/* ------------------------------------------------------------------ queries */

const SELECT_COLS = `fid, state, mb_class, area_ha, hansen_loss_year, hansen_loss_frac,
   lon, lat, geom`;

/**
 * Fields intersecting a viewport. `where` is an optional extra predicate, already
 * validated by the caller.
 */
export async function queryFields(bbox, { where = null, limit = CONFIG.MAX_FIELDS } = {}) {
	const inv = await getInventory();
	const files = filesFor(inv, bbox);
	if (!files.length) return { features: [], files: 0, rows: 0, ms: 0, truncated: false };

	const { conn } = await getDB();
	const urls = urlsFor(files);
	const extra = where ? ` AND (${where})` : '';
	const sql = `
    SELECT ${SELECT_COLS}
    FROM read_parquet([${urls.map((u) => `'${u}'`).join(', ')}])
    WHERE geom_bbox.xmin <= ${bbox.e} AND geom_bbox.xmax >= ${bbox.w}
      AND geom_bbox.ymin <= ${bbox.n} AND geom_bbox.ymax >= ${bbox.s}${extra}
    LIMIT ${limit + 1}`;

	const t0 = performance.now();
	const res = await conn.query(sql);
	const ms = performance.now() - t0;

	const rows = res.toArray();
	const truncated = rows.length > limit;
	const use = truncated ? rows.slice(0, limit) : rows;

	const features = [];
	for (const r of use) {
		const o = r.toJSON ? r.toJSON() : r;
		let geometry = null;
		try {
			const g = o.geom;
			if (g) geometry = wkbToGeoJSON(g instanceof Uint8Array ? g : new Uint8Array(g));
		} catch {
			geometry = null;
		}
		if (!geometry) continue;
		features.push({
			type: 'Feature',
			geometry,
			properties: {
				fid: Number(o.fid),
				state: o.state ?? '',
				mb_class: o.mb_class == null ? null : Number(o.mb_class),
				mb_name: CONFIG.MB_CLASSES[Number(o.mb_class)] ?? 'unclassified',
				area_ha: o.area_ha == null ? null : Number(o.area_ha),
				hansen_loss_year: o.hansen_loss_year == null ? null : Number(o.hansen_loss_year),
				hansen_loss_frac: o.hansen_loss_frac == null ? null : Number(o.hansen_loss_frac),
				lon: Number(o.lon),
				lat: Number(o.lat)
			}
		});
	}
	return { features, files: files.length, rows: use.length, ms, truncated, sql };
}

/** Class composition for a viewport, without shipping any geometry. */
export async function summarise(bbox, { where = null } = {}) {
	const inv = await getInventory();
	const files = filesFor(inv, bbox);
	if (!files.length) return { total: 0, classes: [], ms: 0 };
	const { conn } = await getDB();
	const urls = urlsFor(files);
	const extra = where ? ` AND (${where})` : '';
	const t0 = performance.now();
	const res = await conn.query(`
    SELECT mb_class, count(*) AS n, sum(COALESCE(area_ha, 0)) AS ha
    FROM read_parquet([${urls.map((u) => `'${u}'`).join(', ')}])
    WHERE geom_bbox.xmin <= ${bbox.e} AND geom_bbox.xmax >= ${bbox.w}
      AND geom_bbox.ymin <= ${bbox.n} AND geom_bbox.ymax >= ${bbox.s}${extra}
    GROUP BY 1 ORDER BY n DESC`);
	const ms = performance.now() - t0;
	const classes = res.toArray().map((r) => {
		const o = r.toJSON ? r.toJSON() : r;
		return {
			cls: o.mb_class == null ? null : Number(o.mb_class),
			name: CONFIG.MB_CLASSES[Number(o.mb_class)] ?? 'unclassified',
			n: Number(o.n),
			ha: Number(o.ha)
		};
	});
	return { total: classes.reduce((a, c) => a + c.n, 0), classes, ms, files: files.length };
}
