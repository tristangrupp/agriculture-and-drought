/**
 * The field trend charts: one field's NDVI above the rainfall that drove it.
 *
 * Pure functions - data in, SVG string out. No map, no DOM, no app state, so they can be
 * exercised directly in node against a real series file.
 *
 * Two measures on different scales means two stacked panels sharing one x-axis, never a
 * second y-axis: with two baselines to choose, a dual axis can be made to show any
 * correlation you like. Each panel is one coloured series read against a neutral dashed
 * reference, the same grammar twice.
 *
 * Palette checked with the dataviz validator against the card surface #f2efe9. Green and
 * grey failed the chroma floor; green and orange failed protan separation at deltaE 1.8.
 * Green and blue pass at 19.5 (deutan), and the references carry a dash plus a direct
 * label so identity never rests on colour alone.
 */

/* Validated against the card surface with the dataviz checker: CVD separation deltaE
   19.5 (deutan), chroma floor and contrast all pass. References stay neutral ink. */
const C_NDVI = '#1f7a34';
const C_RAIN = '#3072b8';

/** Parse a yyyymmdd int into a day number for positioning. */
function dnum(v) {
	const s = String(v);
	return Date.UTC(+s.slice(0, 4), +s.slice(4, 6) - 1, +s.slice(6, 8)) / 86400000;
}

/** Both ticks in a panel take their precision from the panel's range, so an axis never
    reads "172" at one end and "0.00" at the other. */
function tick(v, range) {
	return v.toFixed(range >= 10 ? 0 : 2);
}

function fmtDate(v) {
	const s = String(v);
	const m = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
		'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][+s.slice(4, 6) - 1];
	return `${m} ${+s.slice(6, 8)}`;
}

/**
 * One panel of the stacked pair. `main` is the coloured series, `ref` the neutral dashed
 * reference it should be read against. Both share the x-domain passed in, so the two
 * panels line up vertically and a date in one is the same date in the other.
 */
function panel(opts) {
	const { main, ref, dates, x0, x1, color, label, refLabel, unit, ev, height, zero } = opts;
	const W = 292;
	const H = height;
	const padL = 34;
	const padB = 14;
	const padT = 8;
	const vals = main.concat(ref || []).filter((v) => v != null && isFinite(v));
	if (vals.length < 2) return '';
	let lo = Math.min(...vals);
	let hi = Math.max(...vals);
	if (hi - lo < 1e-6) hi = lo + 1;
	const pad = (hi - lo) * 0.08;
	// Rainfall is a magnitude: it cannot be negative, and how far the line sits above the
	// axis is the quantity itself, so the axis starts at zero. NDVI is an index, not a
	// count, so it keeps a fitted range - but never a negative floor either.
	lo = zero ? 0 : Math.max(lo - pad, 0);
	hi += pad;
	const xf = (d) => padL + ((dnum(d) - x0) / Math.max(x1 - x0, 1)) * (W - padL - 4);
	const yf = (v) => padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB);

	const line = (arr, cls, stroke) => {
		let d = '';
		let open = false;
		arr.forEach((v, i) => {
			if (v == null || !isFinite(v)) { open = false; return; }
			d += `${open ? 'L' : 'M'}${xf(dates[i]).toFixed(1)} ${yf(v).toFixed(1)}`;
			open = true;
		});
		return d
			? `<path d="${d}" class="${cls}"${stroke ? ` stroke="${stroke}"` : ''}/>`
			: '';
	};

	// The drought window, shaded identically in both panels so the eye can carry it down.
	const evBand = ev
		? `<rect x="${xf(ev.a).toFixed(1)}" y="${padT}"
       width="${Math.max(xf(ev.b) - xf(ev.a), 1).toFixed(1)}"
       height="${H - padT - padB}" class="cband"/>`
		: '';

	return `<svg class="chart" viewBox="0 0 ${W} ${H}" width="100%" height="${H}"
    role="img" aria-label="${label} against ${refLabel}">
    ${evBand}
    <line x1="${padL}" y1="${(H - padB).toFixed(1)}" x2="${W - 4}" y2="${(H - padB).toFixed(1)}" class="axis"/>
    <text x="0" y="${(padT + 7).toFixed(1)}" class="tick">${tick(hi, hi)}</text>
    <text x="0" y="${(H - padB - 1).toFixed(1)}" class="tick">${tick(lo, hi)}</text>
    ${ref ? line(ref, 'ref') : ''}
    ${line(main, 'main', color)}
  </svg>
  <div class="legend">
    <span><i style="background:${color}"></i>${label}${unit ? ` (${unit})` : ''}</span>
    <span><i class="dash"></i>${refLabel}</span>
  </div>`;
}

/** The two stacked panels for one field. */
export function fieldCharts(s, fid) {
	if (!s) return '<p class="ctxLead">Trend data is still loading\u2026</p>';
	const nd = s.fields[String(fid)];
	if (!nd) return '<p class="ctxLead">No trend recorded for this field.</p>';
	const rain = s.rain;

	// One x-domain for both panels, spanning whichever series starts first and ends last.
	const allX = s.dates.concat(rain ? rain.dates : []);
	const x0 = Math.min(...allX.map(dnum));
	const x1 = Math.max(...allX.map(dnum));
	const ev = { a: Number(s.event.start.replace(/-/g, '')), b: Number(s.event.end.replace(/-/g, '')) };

	const top = panel({
		main: nd, ref: s.scene, dates: s.dates, x0, x1, color: C_NDVI,
		label: 'This field', refLabel: 'Neighbourhood median', unit: 'NDVI',
		ev, height: 86
	});
	const bottom = rain
		? panel({
				main: rain.p30, ref: rain.thr, dates: rain.dates, x0, x1, color: C_RAIN,
				label: `Basin rainfall, ${rain.accum_days}-day`, refLabel: 'Drought threshold',
				unit: 'mm', ev, height: 76, zero: true
			})
		: '';
	// CHIRPS is ~5 km, so rainfall is the basin's, not this field's. Saying so matters:
	// the whole point of the NDVI panel is that neighbouring fields diverge under rain
	// that is, as far as anything here can tell, identical.
	return top + bottom +
		`<div class="rampLbl"><span>${fmtDate(Math.min(...allX))}</span>
       <span>shaded: drought</span><span>${fmtDate(Math.max(...allX))}</span></div>`;
}
