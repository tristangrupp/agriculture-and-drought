/**
 * Drought & Fields — a map where the basin-scale drought product and the field-scale
 * crop data meet.
 *
 * Terminology: trazo4 delineates FIELDS (cropped plots). A parcel is a land holding,
 * which is a different unit and is not what this data contains.
 *
 * The two halves are loaded very differently, deliberately. The drought layer is 1,072
 * basins and about 2 MB, so it ships whole and every filter is instant and local. The
 * field layer is 36.5 million polygons that must never be loaded whole; it is queried
 * from repacked GeoParquet by DuckDB-WASM over HTTP range requests, viewport by viewport.
 *
 * Pattern credit: the command palette, the file-extent pruning and the lazy DuckDB boot
 * follow WRI's ai-web-map-exploration experiments.
 */

import { CONFIG } from '../config.js?v=546c63c8ff';
import { fieldCharts } from './charts.js?v=772c603ccc';
import * as F from './fields.js?v=08ec1bba5d';
import { createRegistry, coreCommands } from './commands.js?v=984a0b3f46';
import { createTour } from './tour.js?v=7a1f62d3b2';

const $ = (id) => document.getElementById(id);
const RAMP = ['--d1', '--d2', '--d3', '--d4', '--d5', '--d6', '--d7'];
const css = (v) => getComputedStyle(document.body).getPropertyValue(v).trim();

const state = {
	metric: 'total_drought_days',
	basins: null,
	events: {},
	meta: null,
	selected: null,
	isolated: null,
	satellite: false,
	legend: true,
	fields: null,
	lastWhere: null,
	breaks: [],
	mapReady: false,
	view: 'overview',
	basinFields: null,     // per-basin field counts and hectares
	stressed: null,        // ranked shortlist of drought-on-cropland basins
	responseIndex: null,   // basins that have a Sentinel-2 response layer
	response: null,        // the loaded layer: per-field NDVI behaviour
	mode: null,            // 'basin' or 'field' - decides what the panel is for
	examples: null,        // the shipped example windows
	example: null,         // which one is open
	series: null,          // per-field NDVI + basin rainfall for the open example
	picked: null           // the field whose trend is on screen
};

/* Two modes, each with a single objective. The copy is written as what the viewer
   should be able to do in this mode - the panel below it keeps only what serves that. */
const MODES = {
	basin: {
		label: 'Basin view',
		crumb: 'BASINS',
		q: 'Where did the rain fail, and did it hit farmland?',
		text:
			'Compare basins. Darker means more days in 2024 when rain fell short of normal for ' +
			'that time of year. Switch to cropland exposure to see where the drought met farms.'
	},
	field: {
		label: 'Field view',
		crumb: 'FIELDS',
		q: 'Under the same rain, which fields stayed green?',
		text:
			'Every field here got the same rainfall. Darker green fields held their greenness ' +
			'better than their neighbours; pale ones were not measured. Click a field for its ' +
			'trend against the rain.'
	}
};

/** Fields are legible from about here; below it the map is a basin map again. */
const FIELD_MODE_ZOOM = 8.5;

/** True on the GitHub Pages build, where there is no parquet to query. */
const STATIC = !!CONFIG.STATIC;

/* The views in the left nav. Each one decides how basins are coloured and what the
   right-hand card says; the map itself is shared. */
const VIEWS = {
	overview: {
		title: 'Drought overview',
		lead: 'CHIRPS rainfall against a 1990–2010 baseline, per HydroBASINS level-6 basin. ' +
			'A day counts as drought only when the previous 30 days fall below the driest ' +
			'fifth for that date, so an ordinary dry season is not counted.',
		metric: 'total_drought_days'
	},
	stress: {
		title: 'Water-stressed basins',
		lead: 'A composite of duration, longest unbroken event and accumulated deficit, ' +
			'each scaled to its own 95th percentile so no unit dominates.',
		metric: 'stress'
	},
	exposure: {
		title: 'Cropland exposure',
		lead: 'Stress multiplied by the cropland actually inside the basin. A very dry ' +
			'basin with no farmland scores low here, which is the point.',
		metric: 'exposure'
	},
	fields: {
		title: 'Fields in view',
		lead: '29.2 million Brazilian field polygons, read per viewport straight from ' +
			'GeoParquet. Zoom past z10.5 and load them.',
		metric: 'total_drought_days'
	},
	response: {
		title: 'Field drought response',
		lead: 'Each field\u2019s NDVI minus the median of every field around it on the same ' +
			'date, averaged across the drought. Comparing neighbours on one date cancels ' +
			'the season, the drought and the atmosphere; what is left is this field against ' +
			'its neighbours under the same rain.',
		metric: 'stress'
	},
	method: {
		title: 'How this works',
		lead: 'Provenance, pruning, and the limits of what greenness can tell you about water.',
		metric: 'total_drought_days'
	}
};

/* ------------------------------------------------------------------ map */

const EMPTY_STYLE = {
	version: 8,
	sources: {},
	layers: [{ id: 'bg', type: 'background', paint: { 'background-color': '#faf8f6' } }]
};

const map = new maplibregl.Map({
	container: 'map',
	style: EMPTY_STYLE,
	center: [-51, -13],
	zoom: 3.7,
	maxZoom: 17,
	attributionControl: { compact: true }
});
map.addControl(new maplibregl.NavigationControl({ visualizePitch: false }), 'bottom-right');
map.addControl(
	new maplibregl.ScaleControl({ maxWidth: 110, unit: 'metric' }),
	'bottom-left'
);

/* ------------------------------------------------------------------ log */

function log(title, meta, cls) {
	const el = document.createElement('div');
	el.className = 'row' + (cls ? ' ' + cls : '');
	el.innerHTML = `<b>${title}</b>${meta ? `<div class="meta">${meta}</div>` : ''}`;
	$('log').prepend(el);
	while ($('log').children.length > 4) $('log').lastElementChild.remove();
	setTimeout(() => el.remove(), 9000);
}

/* ------------------------------------------------------------------ drought layer */

/** Zoom-dependent basin opacity, shared by every place that resets it. */
function baseOpacity(sat = state.satellite) {
	// Over imagery the choropleth has to become a tint rather than a fill, or the two
	// fight and neither is legible. Same zoom shape, roughly half the weight.
	const k = sat ? 0.45 : 1;
	return [
		'interpolate', ['linear'], ['zoom'],
		7, 0.62 * k,
		CONFIG.FIELD_MIN_ZOOM, 0.30 * k,
		CONFIG.FIELD_MIN_ZOOM + 1.5, 0.10 * k
	];
}

function quantileBreaks(values, n) {
	const s = values.filter((v) => v > 0).sort((a, b) => a - b);
	if (!s.length) return new Array(n - 1).fill(0);
	const out = [];
	for (let i = 1; i < n; i++) out.push(s[Math.floor((s.length * i) / n)]);
	return out;
}

/** Metric definitions, including the two that only exist after the field join. */
function metricDefs() {
	const extra = state.basinFields
		? [
				{ key: 'stress', label: 'Water stress (composite)', unit: '' },
				{ key: 'exposure', label: 'Cropland exposure', unit: 'ha·stress' },
				{ key: 'ha_crop', label: 'Cropland in basin', unit: 'ha' }
			]
		: [];
	return CONFIG.DROUGHT_METRICS.concat(extra);
}

function paintExpression() {
	const key = state.metric;
	const vals = state.basins.features.map((f) => f.properties[key] ?? 0);
	state.breaks = quantileBreaks(vals, RAMP.length);
	const stops = [];
	state.breaks.forEach((b, i) => stops.push(b, css(RAMP[i + 1])));
	return ['step', ['coalesce', ['get', key], 0], css(RAMP[0]), ...stops];
}

function refreshBasinPaint() {
	map.setPaintProperty('basins-fill', 'fill-color', paintExpression());
	const hi = Math.max(...state.basins.features.map((f) => f.properties[state.metric] ?? 0));
	const m = metricDefs().find((d) => d.key === state.metric) || { unit: '' };
	$('rampHi').textContent = `${Math.round(hi)}${m.unit ? ' ' + m.unit : ''}`;
	$('ramp').innerHTML = RAMP.map((r) => `<i style="background:${css(r)}"></i>`).join('');
}

async function loadDrought() {
	const [b, ev, meta] = await Promise.all([
		fetch(CONFIG.BASINS_URL).then((r) => r.json()),
		fetch(CONFIG.EVENTS_URL).then((r) => r.json()),
		fetch(CONFIG.META_URL).then((r) => r.json())
	]);
	state.basins = b;
	state.events = ev;
	state.meta = meta;

	// The field join is optional: without it the app still works, it just loses the
	// stress and exposure views rather than failing to start.
	try {
		const [bf, sb] = await Promise.all([
			fetch(CONFIG.BASIN_FIELDS_URL).then((r) => (r.ok ? r.json() : null)),
			fetch(CONFIG.STRESSED_URL).then((r) => (r.ok ? r.json() : null))
		]);
		if (bf) {
			state.basinFields = bf;
			state.stressed = sb || [];
			for (const f of b.features) {
				const s = bf[String(f.properties.bidx)];
				if (s) Object.assign(f.properties, {
					stress: s.stress, exposure: s.exposure, ha_crop: s.ha_crop,
					n_crop: s.n_crop, n_fields: s.n_fields, ha_total: s.ha_total,
					crop_share: s.crop_share
				});
			}
		}
	} catch (e) {
		console.warn('field join not available', e);
	}

	map.addSource('basins', { type: 'geojson', data: b });
	map.addLayer({
		id: 'basins-fill',
		type: 'fill',
		source: 'basins',
		// The basin choropleth is the overview; once fields are in play it must get out of
		// their way, so opacity falls off as the field threshold is crossed.
		paint: { 'fill-color': paintExpression(), 'fill-opacity': baseOpacity() }
	});
	map.addLayer({
		id: 'basins-line',
		type: 'line',
		source: 'basins',
		paint: { 'line-color': '#ffffff', 'line-width': 0.6, 'line-opacity': 0.85 }
	});
	map.addLayer({
		id: 'basins-hi',
		type: 'line',
		source: 'basins',
		filter: ['==', ['get', 'bidx'], -1],
		paint: { 'line-color': '#2c2a28', 'line-width': 2 }
	});

	const inDrought = b.features.filter((f) => (f.properties.n_events ?? 0) > 0).length;
	state.inDrought = inDrought;

	const sel = $('metricSel');
	sel.innerHTML = metricDefs()
		.map((m) => `<option value="${m.key}">${m.label}</option>`)
		.join('');
	sel.value = state.metric;
	sel.onchange = () => setMetric(sel.value);
	refreshBasinPaint();
	log('Drought layer ready', `${b.features.length} basins · ${meta.total_events} events in ${CONFIG.YEAR}`);
}

function setMetric(key) {
	state.metric = key;
	$('metricSel').value = key;
	refreshBasinPaint();
	const m = metricDefs().find((d) => d.key === key);
	log('Recoloured basins', m ? m.label : key);
}

/* ------------------------------------------------------------------ basin selection */

function showBasin(props) {
	state.selected = props;
	const ev = state.events[String(props.bidx)] || [];
	$('basinTitle').textContent = `HYBAS ${props.HYBAS_ID}`;
	const fs = state.basinFields ? state.basinFields[String(props.bidx)] : null;
	$('basinStats').innerHTML = CONFIG.DROUGHT_METRICS.map((m) => {
		const v = props[m.key] ?? 0;
		return `<div class="stat"><span class="v">${Math.round(v).toLocaleString()}</span>
      <span class="k">${m.label.toLowerCase()}${m.unit ? ' (' + m.unit + ')' : ''}</span></div>`;
	}).join('') + (fs
		? `<div class="stat"><span class="v">${Math.round(fs.ha_crop).toLocaleString()}</span>
         <span class="k">cropland (ha)</span></div>
       <div class="stat"><span class="v">${fs.n_fields.toLocaleString()}</span>
         <span class="k">fields in basin</span></div>`
		: '');
	$('basinEvents').innerHTML = ev.length
		? `<div class="lbl">Events running in ${CONFIG.YEAR}</div>` +
			ev
				.map(
					(e) =>
						`<div class="evt"><span>${e.start} → ${e.end}</span>
             <span class="d">${e.days} d</span></div>`
				)
				.join('')
		: `<p class="muted small" style="margin:8px 0 0">No drought event in ${CONFIG.YEAR}.</p>`;
	$('basinBox').hidden = false;
	map.setFilter('basins-hi', ['==', ['get', 'bidx'], props.bidx]);
}

/* The example list is data, not cartography. Loading it here rather than inside the map's
   load handler means the reader still gets a usable page - and a visible reason - if
   MapLibre never reports ready. */
loadExamples();

/* MapLibre only listens for window resizes. The map's own box also changes when the panel
   switches mode or grows a scrollbar, and without this the canvas kept its old size and
   left a blank strip down the right-hand side and along the bottom. */
if (typeof ResizeObserver !== 'undefined') {
	new ResizeObserver(() => map.resize()).observe(document.getElementById('map'));
}

/* Static-build cleanup is page structure, not cartography, so it runs immediately.
   Inside the map load handler it waited on WebGL: when the map stalled, the public
   site still offered a live-query view and notes for a path that does not ship. */
if (STATIC) {
	// No parquet ships, so the live-query view would only ever fail, and the notes
	// must not tell the reader to use a path that is not there.
	document.querySelectorAll('[data-view="fields"]').forEach((a) => a.remove());
	const zf = $('zoomFields');
	if (zf) zf.hidden = true;
	const a = $('noteA');
	const b = $('noteB');
	if (a) {
		a.innerHTML =
			'Pick an example on the left. Each is a window of a few thousand fields, ' +
			'scored against Sentinel-2 through that basin’s 2024 drought.';
	}
	if (b) {
		b.innerHTML =
			'Click any field for its NDVI trend against the rainfall that drove it. ' +
			'Holding greenness is consistent with irrigation — and with deeper ' +
			'roots, a later planting, or wetter soil.';
	}
}


setTimeout(() => {
	if (state.mapReady) return;
	const lead = $('ctxLead');
	if (lead && /Loading basins/.test(lead.textContent)) {
		lead.textContent =
			'The map did not finish loading. This usually means WebGL is unavailable or ' +
			'blocked in this browser. The example list on the left still works, and ' +
			'reloading the page normally fixes it.';
	}
	log('Map did not report ready', 'WebGL may be unavailable or blocked', 'err');
}, 15000);

map.on('load', async () => {
	try {
		await loadDrought();
	} catch (e) {
		log('Could not load the drought layer', String(e).slice(0, 120), 'err');
	}
	map.on('click', 'basins-fill', (e) => showBasin(e.features[0].properties));
	map.on('mouseenter', 'basins-fill', () => (map.getCanvas().style.cursor = 'pointer'));
	map.on('mouseleave', 'basins-fill', () => {
		map.getCanvas().style.cursor = '';
		hideTip();
	});
	map.on('mousemove', 'basins-fill', (e) => {
		const p = e.features[0].properties;
		showTip(
			`<b>HYBAS ${p.HYBAS_ID}</b>` +
				CONFIG.DROUGHT_METRICS.map(
					(m) =>
						`<div class="r"><span>${m.label}</span><span>${Math.round(
							p[m.key] ?? 0
						).toLocaleString()}</span></div>`
				).join(''),
			e.originalEvent
		);
	});
	map.on('mousemove', 'fields-fill', (e) => {
		const p = e.features[0].properties;
		const resp =
			state.view !== 'response' || !state.response
				? ''
				: p.held != null
					? `<div class="r"><span>held greenness</span><span>${Number(p.held).toFixed(2)}</span></div>` +
						`<div class="r"><span>vs neighbours</span><span>${
							Number(p.anom) >= 0 ? '+' : ''
						}${Number(p.anom).toFixed(3)} NDVI</span></div>`
					: `<div class="r"><span>not scored</span><span>${unscoredReason(p)}</span></div>`;
		showTip(
			`<b>${p.mb_name}</b>` +
				resp +
				`<div class="r"><span>area</span><span>${
					p.area_ha == null ? 'n/a' : Number(p.area_ha).toFixed(1) + ' ha'
				}</span></div>` +
				`<div class="r"><span>state</span><span>${p.state || '—'}</span></div>` +
				(p.hansen_loss_year
					? `<div class="r"><span>forest loss</span><span>20${p.hansen_loss_year}</span></div>`
					: ''),
			e.originalEvent
		);
	});
	map.on('mouseleave', 'fields-fill', hideTip);
	map.on('click', 'fields-fill', (e) => showField(e.features[0].properties));
	map.on('zoomend', updateFieldButton);
	map.on('zoomend', updateSatHint);
	map.on('moveend', () => applyMode());
	updateFieldButton();
	const satBox = $('satToggle');
	if (satBox) satBox.onchange = () => setBasemap(satBox.checked);
	// On by default: every example is somewhere real, and the imagery is what shows that
	// the polygons land on actual parcels.
	setBasemap(true);
	state.mapReady = true;
	const initial = location.hash.replace('#', '');
	setView(VIEWS[initial] ? initial : 'overview');
});

/* ------------------------------------------------------------------ tooltip */

const tip = $('tip');
function showTip(html, ev) {
	tip.innerHTML = html;
	const pad = 14;
	let x = ev.clientX + pad;
	let y = ev.clientY + pad;
	const r = tip.getBoundingClientRect();
	if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - pad;
	if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - pad;
	tip.style.left = x + 'px';
	tip.style.top = y + 'px';
	tip.style.opacity = 1;
}
function hideTip() {
	tip.style.opacity = 0;
}

/* ------------------------------------------------------------------ fields */

const CLASS_COLOR = {
	15: '#c08a3e', 18: '#3fa96b', 19: '#4bb87c', 20: '#7bc46b', 21: '#9a8f4a',
	39: '#5ec27a', 40: '#49b5a4', 46: '#8e6bd0', 41: '#6fbf8a', 62: '#d9a05b',
	3: '#1f6b45', 4: '#557a3a', 12: '#8d9a5b', 24: '#8a8f99', 26: '#2f6f9e',
	33: '#2f6f9e', 11: '#3c7f86', 9: '#2f8f6a', 36: '#a07bd0', 47: '#c9973f'
};
const classColorExpr = () => {
	const pairs = [];
	for (const [k, v] of Object.entries(CLASS_COLOR)) pairs.push(Number(k), v);
	return ['match', ['coalesce', ['get', 'mb_class'], -1], ...pairs, '#7a828d'];
};

/* Held-greenness ramp. Anchored at zero on purpose: a field that behaved exactly like
   its neighbours must read as no signal, not as the middle of a colour scale. */
const HELD_COLORS = ['#e9e4d8', '#cddca8', '#a8cd7c', '#79b855', '#489a3c', '#1f7a34'];

function heldColorExpr() {
	return [
		'case',
		// Unscored is a real state, not an absence: a pale neutral that reads as "this
		// field exists and was not measured", never as empty ground.
		['==', ['get', 'held'], null], '#ded8cd',
		['interpolate', ['linear'], ['get', 'held'],
			0, HELD_COLORS[0], 0.2, HELD_COLORS[1], 0.4, HELD_COLORS[2],
			0.6, HELD_COLORS[3], 0.8, HELD_COLORS[4], 1, HELD_COLORS[5]]
	];
}

/** Repaint the field layer for whichever mode is active. */
function applyFieldPaint() {
	if (!map.getLayer('fields-fill')) return;
	const resp = state.view === 'response' && state.response;
	map.setPaintProperty('fields-fill', 'fill-color',
		resp ? heldColorExpr() : classColorExpr());
	map.setPaintProperty('fields-fill', 'fill-opacity', resp ? 0.85 : 0.75);
	map.setPaintProperty('fields-line', 'line-color',
		state.satellite ? '#f4f1ea' : resp ? '#5c5a58' : '#ffffff');
	map.setPaintProperty('fields-line', 'line-width', resp ? 0.25 : 0.4);
}

/* Why a field has no score. Never cloud: with 10-day median composites every field
   inside the window scored in all three basins. These are scope decisions, so the tooltip
   should name the decision rather than imply the data failed. */
function unscoredReason(p) {
	const r = state.response;
	if (!r) return 'no layer loaded';
	if (p.lon != null) {
		const [w, s, e, n] = r.aoi;
		if (!(p.lon >= w && p.lon <= e && p.lat >= s && p.lat <= n))
			return 'outside the scored window';
	}
	if (!CONFIG.CROP_CLASSES.includes(Number(p.mb_class))) return 'not cropland';
	if (!(Number(p.area_ha) >= 2)) return 'under 2 ha';
	return 'too few clear satellite looks';
}

/** Join the loaded response layer onto field features by fid. */
function joinResponse(features) {
	const r = state.response;
	if (!r) return features;
	let hit = 0;
	for (const f of features) {
		const v = r.fields[String(f.properties.fid)];
		if (v) {
			f.properties.anom = v[0];
			f.properties.held = v[1];
			f.properties.drop = v[2];
			hit += 1;
		} else {
			f.properties.anom = null;
			f.properties.held = null;
			f.properties.drop = null;
		}
	}
	state.responseHits = hit;
	return features;
}

function ensureFieldLayers() {
	if (map.getSource('fields')) return;
	map.addSource('fields', {
		type: 'geojson',
		data: { type: 'FeatureCollection', features: [] }
	});
	map.addLayer({
		id: 'fields-fill',
		type: 'fill',
		source: 'fields',
		paint: { 'fill-color': classColorExpr(), 'fill-opacity': 0.75 }
	});
	map.addLayer({
		id: 'fields-line',
		type: 'line',
		source: 'fields',
		paint: { 'line-color': '#ffffff', 'line-width': 0.4, 'line-opacity': 0.9 }
	});
	// Outlines whichever field the right-hand panel is describing. Without it, "this
	// field" in the panel has no visible referent among twenty thousand polygons.
	map.addLayer({
		id: 'fields-pick',
		type: 'line',
		source: 'fields',
		filter: ['==', ['get', 'fid'], -1],
		paint: { 'line-color': '#2c2a28', 'line-width': 2.6 }
	});
}

function viewBbox() {
	const b = map.getBounds();
	return { w: b.getWest(), s: b.getSouth(), e: b.getEast(), n: b.getNorth() };
}

function updateFieldButton() {
	const ok = map.getZoom() >= CONFIG.FIELD_MIN_ZOOM;
	const btn = $('zoomFields');
	if (btn) {
		btn.disabled = false;
		btn.textContent = ok ? 'Load fields here' : 'Zoom in, then load fields';
	}
}

async function loadFields({ where = null } = {}) {
	if (map.getZoom() < CONFIG.FIELD_MIN_ZOOM) {
		log('Zoom in first', `Fields load from z${CONFIG.FIELD_MIN_ZOOM}; you are at z${map.getZoom().toFixed(1)}`, 'err');
		return;
	}
	const { bbox, clamped } = F.clampBbox(viewBbox());
	state.lastWhere = where;
	log('Querying fields…', 'booting DuckDB and reading parquet row groups');
	try {
		ensureFieldLayers();
		const res = await F.queryFields(bbox, { where });
		state.fields = res;
		joinResponse(res.features);
		map.getSource('fields').setData({
			type: 'FeatureCollection',
			features: res.features
		});
		applyFieldPaint();
		$('fieldBox').hidden = false;
		$('fieldStats').innerHTML =
			`<div class="stat"><span class="v">${res.features.length.toLocaleString()}</span>
        <span class="k">fields drawn</span></div>
       <div class="stat"><span class="v">${res.ms.toFixed(0)} ms</span>
        <span class="k">query time</span></div>` +
			(state.view === 'response' && state.response
				? `<div class="stat"><span class="v">${(state.responseHits || 0).toLocaleString()}</span>
           <span class="k">with NDVI score</span></div>`
				: '');
		log(
			`${res.features.length.toLocaleString()} fields`,
			`${res.files} file${res.files === 1 ? '' : 's'} touched · ${res.ms.toFixed(0)} ms` +
				(res.truncated ? ` · capped at ${CONFIG.MAX_FIELDS.toLocaleString()}` : '') +
				(clamped ? ' · viewport clamped' : '')
		);
		summariseView({ silent: true });
	} catch (e) {
		F.resetDB();
		log('Field query failed', String(e).slice(0, 160), 'err');
	}
}

async function summariseView({ silent = false } = {}) {
	const { bbox } = F.clampBbox(viewBbox());
	try {
		const s = await F.summarise(bbox, { where: state.lastWhere });
		const top = s.classes.slice(0, 8);
		$('fieldClasses').innerHTML =
			`<div class="lbl">Land cover in view</div>` +
			top
				.map((c) => {
					const col = CLASS_COLOR[c.cls] || '#7a828d';
					const pct = s.total ? (100 * c.n) / s.total : 0;
					return `<div class="clsRow">
              <span><i class="swatch" style="background:${col}"></i>${c.name}</span>
              <span class="n">${c.n.toLocaleString()}</span></div>
            <div class="bar"><i style="width:${pct.toFixed(1)}%;background:${col}"></i></div>`;
				})
				.join('');
		$('fieldBox').hidden = false;
		if (!silent)
			log(`${s.total.toLocaleString()} fields in view`, `${s.classes.length} classes · ${s.ms.toFixed(0)} ms`);
	} catch (e) {
		if (!silent) log('Summary failed', String(e).slice(0, 140), 'err');
	}
}

/* ------------------------------------------------------------------ actions */

async function flyToPlace(q) {
	try {
		const r = await fetch(
			`https://nominatim.openstreetmap.org/search?format=json&limit=1&q=${encodeURIComponent(q)}`,
			{ headers: { Accept: 'application/json' } }
		);
		const j = await r.json();
		if (!j.length) return log('No such place', q, 'err');
		const { lat, lon, display_name } = j[0];
		map.flyTo({ center: [Number(lon), Number(lat)], zoom: 11.5, duration: 1400 });
		log('Flying to', display_name.slice(0, 80));
	} catch (e) {
		log('Geocoding failed', String(e).slice(0, 120), 'err');
	}
}

function flyToWorst() {
	const key = state.metric;
	let best = null;
	for (const f of state.basins.features)
		if (!best || (f.properties[key] ?? 0) > (best.properties[key] ?? 0)) best = f;
	const c = centroid(best);
	map.flyTo({ center: c, zoom: 7.5, duration: 1400 });
	showBasin(best.properties);
	const m = metricDefs().find((d) => d.key === key) || { unit: '' };
	log('Worst basin on this metric', `HYBAS ${best.properties.HYBAS_ID} · ${Math.round(best.properties[key])} ${m.unit || ''}`);
}

function centroid(f) {
	let x = 0, y = 0, n = 0;
	const walk = (c) => {
		if (typeof c[0] === 'number') { x += c[0]; y += c[1]; n++; }
		else c.forEach(walk);
	};
	walk(f.geometry.coordinates);
	return [x / n, y / n];
}

function highlightThreshold(key, n) {
	state.metric = key;
	$('metricSel').value = key;
	refreshBasinPaint();
	map.setPaintProperty('basins-fill', 'fill-opacity', [
		'case', ['>=', ['coalesce', ['get', key], 0], n], 0.85, 0.08
	]);
	const hit = state.basins.features.filter((f) => (f.properties[key] ?? 0) >= n).length;
	log(`${hit} basins over ${n}`, key.replace(/_/g, ' '));
}

function isolateSelected() {
	if (!state.selected) return;
	state.isolated = state.selected.bidx;
	map.setFilter('basins-fill', ['==', ['get', 'bidx'], state.isolated]);
	map.setFilter('basins-line', ['==', ['get', 'bidx'], state.isolated]);
	log('Isolated basin', `HYBAS ${state.selected.HYBAS_ID}`);
}
function clearIsolate() {
	state.isolated = null;
	map.setFilter('basins-fill', null);
	map.setFilter('basins-line', null);
	map.setPaintProperty('basins-fill', 'fill-opacity', baseOpacity());
	log('Showing all basins', '');
}

/* Washed right out on purpose. Imagery is the most detailed thing that can be on this
   map and none of it is the finding, so it is desaturated, lifted out of true black and
   held at half opacity - context for where a field sits, never the subject. */
const SAT_PAINT = {
	'raster-opacity': 0.38,
	'raster-saturation': -0.72,
	// Lifting the black point alone turns imagery muddy brown against a cream ground.
	// Raising the whole range instead bleaches it toward the page colour, which is what
	// "washed out" should mean here.
	'raster-brightness-min': 0.55,
	'raster-brightness-max': 1,
	'raster-contrast': -0.3
};

/** Below zoom 5.5 the tiles are a slow, meaningless texture, so the layer starts there. */
const SAT_MIN_ZOOM = 5.5;

function ensureSatLayer() {
	if (!map.getSource('sat')) {
		map.addSource('sat', {
			type: 'raster',
			tiles: [
				'https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'
			],
			tileSize: 256,
			maxzoom: 18,
			attribution:
				'Imagery \u00a9 Esri, Maxar, Earthstar Geographics, and the GIS User Community'
		});
	}
	if (!map.getLayer('sat')) {
		// Before basins-fill puts it at the bottom of the stack, so it is under the
		// choropleth and under the field polygons, which are added later still.
		map.addLayer(
			{
				id: 'sat',
				type: 'raster',
				source: 'sat',
				minzoom: SAT_MIN_ZOOM,
				paint: SAT_PAINT
			},
			'basins-fill'
		);
	}
}

function setBasemap(on) {
	state.satellite = !!on;
	if (state.satellite) ensureSatLayer();
	if (map.getLayer('sat')) {
		map.setLayoutProperty('sat', 'visibility', state.satellite ? 'visible' : 'none');
	}
	map.setPaintProperty('basins-fill', 'fill-opacity', baseOpacity());
	// Dark hairlines vanish into imagery; light ones read on both.
	if (map.getLayer('fields-line')) {
		map.setPaintProperty('fields-line', 'line-color',
			state.satellite ? '#f4f1ea' : (state.view === 'response' ? '#5c5a58' : '#ffffff'));
	}
	const box = $('satToggle');
	if (box) box.checked = state.satellite;
	updateSatHint();
}

/** Say when the layer is present but out of range, rather than looking broken. */
function updateSatHint() {
	const el = $('satHint');
	if (!el) return;
	el.textContent = !state.satellite
		? 'Esri World Imagery, held well back'
		: map.getZoom() < SAT_MIN_ZOOM
			? 'Zoom in to an example to see it'
			: 'Esri World Imagery, held well back';
}

function toggleBasemap() {
	setBasemap(!state.satellite);
	log(state.satellite ? 'Satellite basemap on' : 'Satellite basemap off',
		state.satellite && map.getZoom() < SAT_MIN_ZOOM
			? `Appears from z${SAT_MIN_ZOOM}; you are at z${map.getZoom().toFixed(1)}`
			: '');
}

function toggleLegend() {
	state.legend = !state.legend;
	$('context').style.display = state.legend ? '' : 'none';
}

function flyToBrazil() {
	map.flyTo({ center: [-51, -13], zoom: 3.7, duration: 1200 });
	log('Back to Brazil', '');
}

function renderRanking() {
	const host = $('ctxExtra');
	if (!host) return;
	if (!state.stressed || !state.stressed.length) {
		host.innerHTML = state.basinFields
			? ''
			: '<p class="ctxLead" style="margin-top:10px">Run <code>pipeline/field_drought.py</code>' +
				' to add the field join, and this becomes a ranked list.</p>';
		return;
	}
	const key = state.view === 'exposure' ? 'exposure' : 'stress';
	const rows = state.stressed.slice().sort((a, b) => b[key] - a[key]).slice(0, 12);
	host.innerHTML =
		`<p class="group" style="margin-top:14px">Top basins by ${
			key === 'exposure' ? 'cropland exposure' : 'water stress'
		}</p><div class="rank">` +
		rows
			.map(
				(r, i) => `<div class="rankRow" data-bidx="${r.bidx}">
        <span class="i">${String(i + 1).padStart(2, '0')}</span>
        <span class="rankName">${r.HYBAS_ID}</span>
        <span class="rankVal">${
					key === 'exposure'
						? Math.round(r.ha_crop).toLocaleString() + ' ha'
						: r.stress.toFixed(2)
				}</span></div>`
			)
			.join('') +
		'</div>';
	host.querySelectorAll('.rankRow').forEach((el) => {
		el.onclick = () => {
			const b = state.basins.features.find(
				(f) => f.properties.bidx === Number(el.dataset.bidx)
			);
			if (!b) return;
			map.flyTo({ center: centroid(b), zoom: 7.5, duration: 1200 });
			showBasin(b.properties);
		};
	});
}

/** Load the per-field series for the open example, once. */
async function ensureSeries(hybas) {
	if (state.series && state.series.hybas === hybas) return state.series;
	state.series = null;
	const r = await fetch(`${CONFIG.EXAMPLES_BASE}/series_${hybas}.json`);
	if (!r.ok) throw new Error(`HTTP ${r.status}`);
	state.series = await r.json();
	return state.series;
}

async function showField(props) {
	state.picked = props;
	if (map.getLayer('fields-pick')) {
		map.setFilter('fields-pick', ['==', ['get', 'fid'], props.fid]);
	}
	const box = $('pickBox');
	if (!box) return;
	box.hidden = state.mode !== 'field' && currentMode() !== 'field';
	$('pickTitle').textContent = props.mb_name || 'Field';
	$('pickStats').innerHTML =
		`<div class="stat"><span class="v">${
			props.held == null ? '\u2014' : Number(props.held).toFixed(2)
		}</span><span class="k">held greenness</span></div>
     <div class="stat"><span class="v">${
				props.anom == null ? '\u2014' : (Number(props.anom) >= 0 ? '+' : '') + Number(props.anom).toFixed(3)
			}</span><span class="k">vs neighbours</span></div>
     <div class="stat"><span class="v">${
				props.area_ha == null ? '\u2014' : Number(props.area_ha).toFixed(1)
			}</span><span class="k">hectares</span></div>`;
	if (props.held == null) {
		$('pickCharts').innerHTML =
			`<p class="ctxLead">This field is mapped but not scored: ${unscoredReason(props)}.
       It is drawn so the coverage you see is the coverage there is.</p>`;
		return;
	}
	$('pickCharts').innerHTML = '<p class="ctxLead">Loading trend\u2026</p>';
	try {
		await ensureSeries(state.example);
		$('pickCharts').innerHTML = fieldCharts(state.series, props.fid);
	} catch (e) {
		$('pickCharts').innerHTML =
			`<p class="ctxLead">Trend unavailable: ${String(e).slice(0, 80)}</p>`;
	}
}

/** Little NDVI trace for the AOI, with the drought window shaded. */
function sparkline(rec) {
	const v = rec.scene_ndvi.map((x) => (x == null ? NaN : x));
	const ok = v.filter(Number.isFinite);
	if (ok.length < 2) return '';
	const lo = Math.min(...ok);
	const hi = Math.max(...ok);
	const W = 288;
	const Hh = 46;
	const d = rec.dates.map((x) => String(x));
	const xf = (i) => (i / (v.length - 1)) * W;
	const yf = (y) => Hh - ((y - lo) / Math.max(hi - lo, 1e-6)) * (Hh - 6) - 3;
	const path = v
		.map((y, i) => {
			if (!Number.isFinite(y)) return '';
			const cmd = i && Number.isFinite(v[i - 1]) ? 'L' : 'M';
			return `${cmd}${xf(i).toFixed(1)} ${yf(y).toFixed(1)}`;
		})
		.join('');
	const s = rec.event.start.replace(/-/g, '');
	const e = rec.event.end.replace(/-/g, '');
	const i0 = d.findIndex((x) => x >= s);
	let i1 = -1;
	for (let i = 0; i < d.length; i += 1) if (d[i] <= e) i1 = i;
	const band =
		i0 >= 0 && i1 > i0
			? `<rect x="${xf(i0).toFixed(1)}" y="0" width="${(xf(i1) - xf(i0)).toFixed(1)}" height="${Hh}" fill="#e9ac63" opacity=".22"/>`
			: '';
	return (
		`<svg class="spark" viewBox="0 0 ${W} ${Hh}" width="100%" height="${Hh}" role="img" aria-label="Median NDVI across the window, drought shaded">` +
		`${band}<path d="${path}" fill="none" stroke="#2c2a28" stroke-width="1.4"/></svg>` +
		`<div class="rampLbl"><span>${rec.dates[0]}</span>` +
		`<span>median NDVI ${lo.toFixed(2)}\u2013${hi.toFixed(2)}</span>` +
		`<span>${rec.dates[rec.dates.length - 1]}</span></div>`
	);
}

/** Outline the Sentinel-2 scoring window. Without it the grey fields beyond the edge read
    as missing data rather than as ground that was never analysed. */
function showAoi(aoi) {
	const [w, s, e, n] = aoi;
	const data = {
		type: 'Feature',
		properties: {},
		geometry: {
			type: 'Polygon',
			coordinates: [[[w, s], [e, s], [e, n], [w, n], [w, s]]]
		}
	};
	if (map.getSource('aoi')) {
		map.getSource('aoi').setData(data);
	} else {
		map.addSource('aoi', { type: 'geojson', data });
		map.addLayer({
			id: 'aoi-line',
			type: 'line',
			source: 'aoi',
			paint: {
				'line-color': '#2c2a28',
				'line-width': 1.2,
				'line-dasharray': [3, 2],
				'line-opacity': 0.7
			}
		});
	}
	map.setLayoutProperty('aoi-line', 'visibility', 'visible');
}

function hideAoi() {
	if (map.getLayer('aoi-line')) map.setLayoutProperty('aoi-line', 'visibility', 'none');
}

/** Open one of the five shipped examples: fly to it, draw its fields, describe it.

    The GeoJSON already carries `held` and `anom` per feature, so unlike the local path
    there is nothing to join - the file is the join, done at build time. */
async function loadExample(hybas) {
	const ex = (state.examples || []).find((e) => e.hybas === hybas);
	if (!ex) return;
	state.example = hybas;
	markActiveExample(hybas);
	if (!state.mapReady) {
		log('Map is not ready yet', 'The example cannot be drawn until it is', 'err');
		return;
	}
	log('Loading example\u2026',
		`${ex.region} \u00b7 ${ex.n_fields.toLocaleString()} fields \u00b7 ${ex.size_mb} MB`);
	try {
		const base = CONFIG.EXAMPLES_BASE;
		const [rec, gj] = await Promise.all([
			fetch(`${base}/response_${hybas}.json`).then((r) => r.json()),
			fetch(`${base}/fields_${hybas}.json`).then((r) => r.json())
		]);
		state.response = rec;
		state.series = null;
		state.picked = null;
		state.exampleGeo = gj;
		$('pickBox').hidden = true;
		if (map.getLayer('fields-pick')) {
			map.setFilter('fields-pick', ['==', ['get', 'fid'], -1]);
		}
		for (const f of gj.features) {
			f.properties.mb_name =
				CONFIG.MB_CLASSES[Number(f.properties.mb_class)] || 'unclassified';
		}
		ensureFieldLayers();
		map.getSource('fields').setData(gj);
		applyFieldPaint();
		showAoi(rec.aoi);
		const [w, s, e, n] = rec.aoi;
		map.fitBounds([[w, s], [e, n]], { padding: 20, duration: 1100 });
		const nScored = gj.features.filter((f) => f.properties.held != null).length;
		$('fieldStats').innerHTML =
			`<div class="stat"><span class="v">${gj.features.length.toLocaleString()}</span>
        <span class="k">fields mapped</span></div>
       <div class="stat"><span class="v">${nScored.toLocaleString()}</span>
        <span class="k">of those scored</span></div>
       <div class="stat"><span class="v">${ex.crops[0]}</span>
        <span class="k">commonest crop</span></div>`;
		$('fieldClasses').innerHTML =
			`<p class="ctxLead" style="margin-top:10px">Every field trazo4 delineated here is
       drawn. The pale ones were not scored \u2014 not cropland, under 2 ha, or too few
       clear satellite looks. Click one to see which.</p>`;
		if (state.view !== 'response') setView('response');
		else renderResponsePanel();
		map.once('moveend', () => applyMode(true));
		log(`${ex.region}: ${gj.features.length.toLocaleString()} fields`,
			`${ex.system} \u00b7 ${rec.event.days}-day drought`);
	} catch (err) {
		log('Example failed to load', String(err).slice(0, 120), 'err');
	}
}

function markActiveExample(hybas) {
	document.querySelectorAll('.ex-link').forEach((a) =>
		a.classList.toggle('active', a.dataset.hybas === hybas)
	);
}

/** Build the example buttons in the sidebar from the shipped index. */
function renderExampleNav() {
	const host = $('exampleNav');
	if (!host || !state.examples) return;
	host.innerHTML =
		'<p class="group">Examples</p>' +
		state.examples
			.map(
				(e) => `<a class="view-link ex-link" data-hybas="${e.hybas}" href="#response">
        <span class="vt">${e.region}</span>
        <span class="vd">${e.system} \u00b7 ${e.n_fields.toLocaleString()} fields${
					e.n_scored ? `, ${e.n_scored.toLocaleString()} scored` : ''
				}</span></a>`
			)
			.join('');
	host.querySelectorAll('.ex-link').forEach((a) => {
		a.onclick = (evt) => {
			evt.preventDefault();
			loadExample(a.dataset.hybas);
		};
	});
}

async function loadExamples() {
	if (state.examples) return state.examples;
	try {
		const j = await fetch(CONFIG.EXAMPLES_URL).then((r) => (r.ok ? r.json() : []));
		state.examples = j;
		renderExampleNav();
		return j;
	} catch (e) {
		state.examples = [];
		return [];
	}
}

async function loadResponse(hybas) {
	const meta = (state.responseIndex || []).find((r) => r.hybas === hybas);
	if (!meta) return;
	log('Loading field response\u2026', `basin ${hybas}`);
	try {
		const rec = await fetch(`${CONFIG.RESPONSE_BASE}/response_${hybas}.json`).then((r) => {
			if (!r.ok) throw new Error(`HTTP ${r.status}`);
			return r.json();
		});
		state.response = rec;
		const [w, s, e, n] = rec.aoi;
		showAoi(rec.aoi);
		map.fitBounds([[w, s], [e, n]], { padding: 24, duration: 1100 });
		renderResponsePanel();
		map.once('moveend', () => loadFields());
		log(
			`${rec.n_scored.toLocaleString()} fields scored`,
			`${rec.dates.length} clear composites \u00b7 ${rec.event.days}-day drought`
		);
	} catch (err) {
		log('Response layer failed', String(err).slice(0, 120), 'err');
	}
}

function renderExamplePanel() {
	const host = $('ctxExtra');
	const rec = state.response;
	const ex = (state.examples || []).find((e) => e.hybas === state.example);
	if (!rec || !ex) {
		host.innerHTML =
			'<p class="ctxLead" style="margin-top:10px">Pick an example from the left. Each one' +
			' is a window of several thousand fields, already scored against Sentinel-2' +
			' through that basin\u2019s 2024 drought.</p>';
		return;
	}
	const green = rec.season_overlap > 0.35;
	const note = green
		? `Median NDVI held at ${rec.season_overlap.toFixed(2)} through the drought, so there was a crop to lose. Greener-than-neighbours is consistent with irrigation, and also with deeper roots, a later planting or wetter soil.`
		: `The neighbourhood browned off to ${rec.season_overlap.toFixed(2)} median NDVI. Fields still running well above their neighbours here are the clearest signal this method produces \u2014 though a later planting would look the same.`;
	host.innerHTML =
		`<p class="group" style="margin-top:14px">${ex.region} \u2014 ${ex.system}</p>
     ${sparkline(rec)}
     <div class="stats" style="margin-top:10px">
       <div class="stat"><span class="v">${rec.event.days}</span>
         <span class="k">drought days</span></div>
       <div class="stat"><span class="v">${ex.n_fields.toLocaleString()}</span>
         <span class="k">fields mapped</span></div>
       <div class="stat"><span class="v">${(ex.n_scored || 0).toLocaleString()}</span>
         <span class="k">of those scored</span></div>
       <div class="stat"><span class="v">${rec.dates.length}</span>
         <span class="k">clear composites</span></div>
       <div class="stat"><span class="v">${rec.season_overlap.toFixed(2)}</span>
         <span class="k">scene NDVI in drought</span></div>
     </div>
     <div class="ramp" style="margin-top:12px">${HELD_COLORS.map(
				(c) => `<i style="background:${c}"></i>`
			).join('')}</div>
     <div class="rampLbl"><span>like its neighbours</span><span>held greenness</span></div>
     <p class="ctxLead" style="margin-top:10px">${note}</p>
     <p class="ctxLead" style="margin-top:8px">Mostly ${ex.crops.join(' and ')}.
       Drought ran ${rec.event.start} to ${rec.event.end}.</p>
     <p class="ctxLead" style="margin-top:8px">Every field trazo4 delineated here is drawn.
       Click a pale one to see why it was not scored.</p>`;
}

function renderResponsePanel() {
	const host = $('ctxExtra');
	if (!host) return;
	if (state.examples && state.examples.length) return renderExamplePanel();
	const idx = state.responseIndex;
	if (!idx || !idx.length) {
		host.innerHTML =
			'<p class="ctxLead" style="margin-top:10px">No response layers yet. Run ' +
			'<code>pipeline/field_response.py</code> to score a basin\u2019s fields against ' +
			'Sentinel-2 through its drought.</p>';
		return;
	}
	const rec = state.response;
	const list =
		'<p class="group" style="margin-top:14px">Scored basins</p><div class="rank">' +
		idx
			.map(
				(r, i) => `<div class="rankRow" data-hybas="${r.hybas}">
        <span class="i">${String(i + 1).padStart(2, '0')}</span>
        <span class="rankName">${r.hybas}</span>
        <span class="rankVal">${r.n_scored.toLocaleString()} fields</span></div>`
			)
			.join('') +
		'</div>';

	/* Reading a result needs two numbers, not one. season_overlap says whether the
	   neighbourhood had anything green to lose; the positive tail says whether some fields
	   kept it anyway. Low cover with a wide tail is the strongest irrigation signature
	   there is - everything burnt off except the fields drawing water from elsewhere. Low
	   cover with no tail is the genuine null: nothing was growing. */
	const meta = rec ? idx.find((r) => r.hybas === rec.hybas) : null;
	const p90 = meta ? meta.anom_p90 : 0;
	const green = rec && rec.season_overlap > 0.35;
	const standout = p90 >= 0.06;
	const note = !rec
		? ''
		: green
			? `Median NDVI held at ${rec.season_overlap.toFixed(2)} through the drought, so there was a crop to lose. Greener-than-neighbours is consistent with irrigation, and also with deeper roots, a later planting or wetter soil.`
			: standout
				? `The neighbourhood browned off to ${rec.season_overlap.toFixed(2)} median NDVI, yet the top fields still ran ${p90.toFixed(2)} above their neighbours. Holding green while everything around you burns off is the clearest signature this method produces \u2014 though a later planting would look the same.`
				: `Median NDVI was only ${rec.season_overlap.toFixed(2)} during the drought, and no field stood out against its neighbours. Nothing was growing here; that is not the same as nothing drawing water.`;
	const detail = rec
		? `<p class="group" style="margin-top:14px">Basin ${rec.hybas}</p>
       ${sparkline(rec)}
       <div class="stats" style="margin-top:10px">
         <div class="stat"><span class="v">${rec.event.days}</span>
           <span class="k">drought days</span></div>
         <div class="stat"><span class="v">${rec.n_scored.toLocaleString()}</span>
           <span class="k">fields scored</span></div>
         <div class="stat"><span class="v">${p90 >= 0 ? '+' : ''}${p90.toFixed(2)}</span>
           <span class="k">top decile vs neighbours</span></div>
         <div class="stat"><span class="v">${rec.season_overlap.toFixed(2)}</span>
           <span class="k">scene NDVI in drought</span></div>
       </div>
       <p class="ctxLead" style="margin-top:10px">${
					rec.n_scored === rec.n_scoreable
						? `All ${rec.n_scored.toLocaleString()} cropland fields over 2 ha in this window scored`
						: `${rec.n_scored.toLocaleString()} of ${(rec.n_scoreable || rec.n_scored).toLocaleString()} cropland fields over 2 ha scored`
				}, across ${rec.dates.length} ten-day composites. Every other field is drawn
         too, in pale neutral \u2014 not cropland, under 2 ha, or too few clear looks.</p>
       <div class="ramp" style="margin-top:12px">${HELD_COLORS.map(
					(c) => `<i style="background:${c}"></i>`
				).join('')}</div>
       <div class="rampLbl"><span>like its neighbours</span><span>held greenness</span></div>
       <p class="ctxLead" style="margin-top:10px">${note}</p>`
		: '<p class="ctxLead" style="margin-top:10px">Pick a basin to load its scored fields.</p>';

	host.innerHTML = list + detail;
	host.querySelectorAll('.rankRow').forEach((el) => {
		el.onclick = () => loadResponse(el.dataset.hybas);
	});
}

/** Which mode the map is actually in: fields are loaded and legible, or they are not. */
function currentMode() {
	// Leaving for the basin view is the reader's decision the moment they click, not when a
	// multi-second fly-out finally drops the zoom below the threshold.
	if (state.leavingFields) return 'basin';
	if (map.getZoom() < FIELD_MODE_ZOOM) return 'basin';
	// Loaded fields only count if they are on screen. Zoomed in on some other basin with an
	// example still loaded far away, the map is showing basins, so the panel should too.
	if (state.response && state.response.aoi) {
		const [w, s, e, n] = state.response.aoi;
		const b = map.getBounds();
		if (b.getWest() <= e && b.getEast() >= w && b.getSouth() <= n && b.getNorth() >= s) {
			return 'field';
		}
	}
	const live = state.fields && state.fields.features && state.fields.features.length;
	return live ? 'field' : 'basin';
}

/**
 * Re-point the panel at one objective. Basin mode keeps the basin colour controls, ranking
 * and basin card; field mode keeps the example summary, field legend and picked field.
 * Neither mode shows the other's legend, which is what made the two hard to tell apart.
 */
function applyMode(force) {
	const mode = currentMode();
	if (!force && mode === state.mode) return;
	state.mode = mode;
	const m = MODES[mode];
	const box = $('objBox');
	if (box) box.dataset.mode = mode;
	if ($('objMode')) $('objMode').textContent = m.label;
	if ($('objQ')) $('objQ').textContent = m.q;
	if ($('objText')) $('objText').textContent = m.text;
	if ($('crumbMode')) $('crumbMode').textContent = m.crumb;
	if ($('objBack')) $('objBack').hidden = mode !== 'field';
	document.body.dataset.mode = mode;

	const field = mode === 'field';
	if ($('basinControls')) $('basinControls').hidden = field;
	if ($('basinBox')) $('basinBox').hidden = field || !state.selected;
	// "Fields in view" belongs to the live-query path. For a shipped example the response
	// card already states fields mapped and scored, and a second card repeating them only
	// pushes the picked field's chart further down.
	if ($('fieldBox')) $('fieldBox').hidden = !field || !!state.response;
	if ($('pickBox')) $('pickBox').hidden = !field || !state.picked;
	// The example summary lives in ctxExtra; in basin mode that space is the basin ranking.
	if (field && state.view === 'response') renderResponsePanel();
	else if (!field) renderRanking();
	if ($('ctxTitle')) {
		$('ctxTitle').textContent = field
			? (VIEWS.response && VIEWS.response.title) || 'Field drought response'
			: (VIEWS[state.view === 'response' ? 'overview' : state.view] || VIEWS.overview).title;
	}
	if ($('ctxLead')) {
		$('ctxLead').hidden = field;
	}
}

function backToBasins() {
	state.leavingFields = true;
	setView('overview');
	flyToBrazil();
	map.once('moveend', () => {
		state.leavingFields = false;
		applyMode(true);
	});
}

function setView(name) {
	const v = VIEWS[name];
	if (!v) return;
	state.view = name;
	// Only the view links carry data-view. The example buttons share the .view-link class
	// for styling, and toggling them here cleared the chosen example's highlight the moment
	// loadExample switched to the response view.
	document.querySelectorAll('.view-link[data-view]').forEach((a) =>
		a.classList.toggle('active', a.dataset.view === name)
	);
	$('ctxTitle').textContent = v.title;
	$('ctxLead').textContent =
		name === 'overview' && state.basins
			? `${state.basins.features.length.toLocaleString()} basins · ${(
					state.inDrought || 0
				).toLocaleString()} in drought during ${CONFIG.YEAR}. ${v.lead}`
			: v.lead;
	const avail = metricDefs().some((m) => m.key === v.metric);
	if (avail && state.basins) setMetric(v.metric);
	if (name === 'response') loadExamples();
	if (name !== 'response') hideAoi();
	else if (state.response) showAoi(state.response.aoi);
	if (name === 'response') {
		if (state.responseIndex === null) {
			state.responseIndex = [];
			fetch(CONFIG.RESPONSE_INDEX_URL)
				.then((r) => (r.ok ? r.json() : []))
				.then((j) => {
					state.responseIndex = j;
					if (state.view === 'response') renderResponsePanel();
				})
				.catch(() => {});
		}
		renderResponsePanel();
	} else {
		renderRanking();
	}
	applyFieldPaint();
	applyMode(true);
}

const ctx = {
	setView,
	setMetric,
	loadFields,
	clearFields: () => {
		if (map.getSource('fields'))
			map.getSource('fields').setData({ type: 'FeatureCollection', features: [] });
		state.fields = null;
		$('fieldBox').hidden = true;
		log('Cleared fields', '');
	},
	hasFields: () => !!state.fields?.features?.length,
	summariseView,
	flyToWorst,
	flyToPlace,
	flyToBrazil,
	toggleBasemap,
	toggleLegend,
	highlightThreshold,
	isolateSelected,
	clearIsolate,
	hasSelection: () => !!state.selected,
	isIsolated: () => state.isolated != null
};

$('zoomFields').onclick = () => loadFields();

document.querySelectorAll('.view-link').forEach((a) => {
	a.onclick = (e) => {
		e.preventDefault();
		setView(a.dataset.view);
	};
});
addEventListener('hashchange', () => {
	const n = location.hash.replace('#', '');
	if (VIEWS[n]) setView(n);
});

/* ------------------------------------------------------------------ palette */

const registry = createRegistry();
registry.registerAll(coreCommands(ctx));

let paletteResults = [];
let paletteIndex = 0;

function openPalette() {
	$('paletteWrap').hidden = false;
	$('paletteInput').value = '';
	$('paletteInput').focus();
	renderPalette('');
}
function closePalette() {
	$('paletteWrap').hidden = true;
}
function renderPalette(q) {
	paletteResults = registry.search(q);
	paletteIndex = 0;
	const list = $('paletteList');
	if (!paletteResults.length) {
		list.innerHTML = `<li class="ai" aria-selected="true"><div class="t">
      <div>Ask about “${escapeHtml(q)}”</div>
      <div class="h">No command matches. An AI fallback would answer here — not wired up in this build.</div>
    </div></li>`;
		return;
	}
	list.innerHTML = paletteResults
		.map(
			(r, i) => `<li role="option" aria-selected="${i === 0}" data-i="${i}">
        <div class="t"><div>${escapeHtml(r.cmd.title)}</div>
        ${r.cmd.hint && r.arg ? `<div class="h">${escapeHtml(r.cmd.hint(r.arg))}</div>` : ''}</div>
        <span class="g">${r.cmd.group}</span></li>`
		)
		.join('');
	[...list.children].forEach((li) => {
		li.onclick = () => runIndex(Number(li.dataset.i));
		li.onmouseenter = () => setIndex(Number(li.dataset.i));
	});
}
function escapeHtml(s) {
	return String(s).replace(/[&<>"']/g, (c) =>
		({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]
	);
}
function setIndex(i) {
	paletteIndex = i;
	[...$('paletteList').children].forEach((li, k) =>
		li.setAttribute('aria-selected', String(k === i))
	);
}
function runIndex(i) {
	const r = paletteResults[i];
	closePalette();
	if (!r) return;
	try {
		r.cmd.run(r.arg);
	} catch (e) {
		log('Command failed', String(e).slice(0, 140), 'err');
	}
}

$('paletteInput').addEventListener('input', (e) => renderPalette(e.target.value));
$('paletteScrim').onclick = closePalette;
$('paletteBtn').onclick = openPalette;
if ($('objBack')) $('objBack').onclick = backToBasins;
ctx.backToBasins = backToBasins;
applyMode(true);

/** A clearly-held field for the tour to show: the 97th percentile of the anomaly among
    scored fields big enough to see, not the maximum - the single most extreme field in a
    window is as likely to be a misclassified patch of water or woodland as a real result. */
function standoutField() {
	const gj = state.exampleGeo;
	if (!gj) return null;
	const pool = gj.features.filter(
		(f) => f.properties.held != null && f.properties.anom != null &&
			Number(f.properties.area_ha) >= 10
	);
	if (!pool.length) return null;
	pool.sort((a, b) => a.properties.anom - b.properties.anom);
	const f = pool[Math.min(pool.length - 1, Math.floor(pool.length * 0.97))];
	let w = Infinity;
	let s = Infinity;
	let e = -Infinity;
	let n = -Infinity;
	const walk = (c) => {
		if (typeof c[0] === 'number') {
			w = Math.min(w, c[0]); e = Math.max(e, c[0]);
			s = Math.min(s, c[1]); n = Math.max(n, c[1]);
		} else c.forEach(walk);
	};
	walk(f.geometry.coordinates);
	return { props: f.properties, center: [(w + e) / 2, (s + n) / 2] };
}

const tour = createTour({
	setView,
	flyToBrazil,
	flyTo: (center, zoom) => map.flyTo({ center, zoom, duration: 1300 }),
	mapReady: () => !!state.mapReady,
	hasExamples: () => !!(state.examples && state.examples.length),
	firstExample: () => (state.examples && state.examples[0] ? state.examples[0].hybas : null),
	currentExample: () => (state.response ? state.example : null),
	currentRegion: () => {
		const ex = (state.examples || []).find((e) => e.hybas === state.example);
		return ex ? `${ex.region} \u2014 ${ex.system.toLowerCase()}` : null;
	},
	loadExample,
	standoutField,
	showField
});
ctx.startTour = () => tour.start();
$('tourBtn').onclick = () => tour.start();
tour.maybeAutostart();
addEventListener('keydown', (e) => {
	const open = !$('paletteWrap').hidden;
	if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
		e.preventDefault();
		open ? closePalette() : openPalette();
		return;
	}
	if (!open) return;
	if (e.key === 'Escape') closePalette();
	else if (e.key === 'ArrowDown') {
		e.preventDefault();
		setIndex(Math.min(paletteIndex + 1, paletteResults.length - 1));
	} else if (e.key === 'ArrowUp') {
		e.preventDefault();
		setIndex(Math.max(paletteIndex - 1, 0));
	} else if (e.key === 'Enter') {
		e.preventDefault();
		runIndex(paletteIndex);
	}
});

if (!/Mac|iPhone|iPad/.test(navigator.platform)) $('kbdHint').textContent = 'Ctrl K';
