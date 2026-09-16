/**
 * A guided first look, for someone who arrives with no idea what this is.
 *
 * The page opens on a map of 1,072 river basins, ten example buttons, a colour ramp and a
 * command palette. To anyone outside the work that is a wall. The tour walks the one path
 * that explains it - where the rain failed, one place up close, one field's story, and what
 * the method cannot claim - and then gets out of the way.
 *
 * It opens once, on a first visit, and remembers being dismissed. The Tour button in the
 * header (or ?tour=1 in the URL) brings it back. Every step can be skipped, Esc closes it,
 * and the page underneath stays usable throughout.
 *
 * The module owns only the overlay. Everything it does to the map goes through the hooks
 * app.js passes in, so the tour cannot drift out of step with how the app really works.
 */

const SEEN_KEY = 'drought-fields:tour-seen:v1';

function seen() {
	try {
		return localStorage.getItem(SEEN_KEY) === '1';
	} catch {
		return false;
	}
}

function markSeen() {
	try {
		localStorage.setItem(SEEN_KEY, '1');
	} catch {
		/* private mode or blocked storage: the tour just offers itself again next time */
	}
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const esc = (s) =>
	String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);

/* The steps. Written for a reader who has never heard of NDVI, HydroBASINS or CHIRPS:
   each one says what is on screen, in the words someone would use to describe it. */
function buildSteps(h) {
	const haveExamples = () => h.hasExamples();
	const canZoomIn = () => h.hasExamples() && h.mapReady();

	return [
		{
			id: 'welcome',
			title: 'What’s this?',
			body: `<p>In 2024, much of Brazil had far less rain than usual.</p>
        <p>This map shows <em>where</em>. Then it zooms to individual farm fields, to see
        which ones stayed green anyway.</p>
        <p>One minute. Leave at any point.</p>`,
			primary: 'Show me around',
			skip: 'Skip, I’ll explore'
		},
		{
			id: 'basins',
			title: 'Where the rain failed',
			target: '#map',
			before: async () => {
				h.setView('overview');
				h.flyToBrazil();
				await sleep(1000);
			},
			body: `<p>Each shape is a river basin. Darker orange means more days in 2024 when
        rain fell short of normal <em>for that time of year</em>.</p>
        <p>An ordinary dry season never counts. Hover a basin for its numbers.</p>`
		},
		{
			id: 'views',
			title: 'Other basin colours',
			target: [
				'.view-link[data-view="overview"]',
				'.view-link[data-view="stress"]',
				'.view-link[data-view="exposure"]'
			],
			body: `<p>These recolour the same basins: drought length, severity, and the cropland
        inside.</p>
        <p>A dry basin with no farms matters less than a moderately dry one full of
        crops.</p>`
		},
		{
			id: 'examples',
			title: 'Ten places, up close',
			target: '#exampleNav',
			requires: haveExamples,
			body: `<p>Ten places, each a different kind of farming: sugarcane, coffee, soy,
        and more.</p>
        <p>Each opens thousands of real field boundaries.</p>`
		},
		{
			id: 'fields',
			title: 'One place, field by field',
			target: '#map',
			requires: canZoomIn,
			before: async () => {
				const first = h.firstExample();
				if (first && h.currentExample() !== first) {
					await h.loadExample(first);
					await sleep(1400);
				}
			},
			body: () => `<p>This is <em>${esc(h.currentRegion() || 'one example')}</em>. Every
        outline is one farm field.</p>
        <p><em>Darker green</em> means a field stayed greener than its neighbours while the
        rain failed.</p>
        <p>The panel has switched to <em>Field view</em>, with its own question. Zoom out,
        or use <em>All basins</em>, to return.</p>`
		},
		{
			id: 'pale',
			title: 'Pale means not measured',
			target: '#map',
			requires: canZoomIn,
			body: `<p>Pale fields are real fields that weren’t scored: pasture, forest, small
        plots, or places too cloudy to read.</p>
        <p>They stay on the map so that nothing looks missing.</p>`
		},
		{
			id: 'story',
			title: 'One field’s story',
			target: '#pickBox',
			requires: canZoomIn,
			before: async () => {
				const f = h.standoutField();
				if (!f) return;
				h.flyTo(f.center, 14);
				await h.showField(f.props);
				await sleep(1500);
			},
			body: `<p>One of the fields that held up well.</p>
        <p>Top: this field in <em>green</em>, its neighbours dashed.<br>
        Bottom: rain in <em>blue</em>, the drought line dashed.<br>
        Shaded band: the drought.</p>
        <p>Click any field for its own charts.</p>`
		},
		{
			id: 'limits',
			title: 'What this can’t tell you',
			body: `<p>A field that stays green in a drought <em>might</em> draw irrigation
        water. It might also root deeper, plant later, or sit on wetter soil.</p>
        <p>This points to fields worth a look. It doesn’t prove water use.</p>`
		},
		{
			id: 'done',
			title: 'You’re set',
			target: '#tourBtn',
			body: `<p>The satellite toggle is in the panel. <kbd>Ctrl K</kbd> searches
        commands.</p>
        <p>The <em>Tour</em> button reopens this.</p>`,
			primary: 'Start exploring'
		}
	];
}

export function createTour(h) {
	const all = buildSteps(h);
	// Evaluated live, not once: whether the map is ready or the examples have arrived can
	// change while the tour is open, and a step that cannot work should drop out quietly.
	const live = () => all.filter((s) => !s.requires || s.requires());

	let root = null;
	let cur = null;
	let busy = false;
	let token = 0;
	let ro = null;
	let els = {};

	function rectFor(step) {
		if (!step || !step.target) return null;
		const sels = Array.isArray(step.target) ? step.target : [step.target];
		let r = null;
		for (const sel of sels) {
			const el = document.querySelector(sel);
			if (!el) continue;
			const b = el.getBoundingClientRect();
			// A hidden panel (the context column collapses on narrow screens) has no box;
			// the card then centres itself rather than pointing at nothing.
			if (b.width < 2 || b.height < 2) continue;
			r = r
				? {
						left: Math.min(r.left, b.left),
						top: Math.min(r.top, b.top),
						right: Math.max(r.right, b.right),
						bottom: Math.max(r.bottom, b.bottom)
					}
				: { left: b.left, top: b.top, right: b.right, bottom: b.bottom };
		}
		if (!r) return null;
		r.left = Math.max(r.left, 0);
		r.top = Math.max(r.top, 0);
		r.right = Math.min(r.right, innerWidth);
		r.bottom = Math.min(r.bottom, innerHeight);
		r.width = r.right - r.left;
		r.height = r.bottom - r.top;
		return r.width > 1 && r.height > 1 ? r : null;
	}

	function place() {
		if (!root || !cur) return;
		const { spot, card, scrim } = els;
		const r = rectFor(cur);
		const vw = innerWidth;
		const vh = innerHeight;
		const pad = 12;
		const m = 16;
		const cw = card.offsetWidth;
		const ch = card.offsetHeight;
		let x;
		let y;
		if (!r) {
			spot.hidden = true;
			scrim.hidden = false;
			x = (vw - cw) / 2;
			y = (vh - ch) / 2;
		} else {
			scrim.hidden = true;
			spot.hidden = false;
			const g = 4;
			spot.style.left = `${r.left - g}px`;
			spot.style.top = `${r.top - g}px`;
			spot.style.width = `${r.width + 2 * g}px`;
			spot.style.height = `${r.height + 2 * g}px`;
			if (r.width > vw * 0.4 && r.height > vh * 0.4) {
				// The map fills most of the screen; beside it there is no room, so the card
				// sits inside its lower-left corner, where the least of the data is.
				x = r.left + m;
				y = r.bottom - ch - m;
			} else if (r.right + pad + cw <= vw - m) {
				x = r.right + pad;
				y = r.top;
			} else if (r.left - pad - cw >= m) {
				x = r.left - pad - cw;
				y = r.top;
			} else if (r.bottom + pad + ch <= vh - m) {
				x = r.left;
				y = r.bottom + pad;
			} else {
				x = r.left;
				y = r.top - pad - ch;
			}
		}
		card.style.left = `${Math.round(Math.min(Math.max(x, m), vw - cw - m))}px`;
		card.style.top = `${Math.round(Math.min(Math.max(y, m), vh - ch - m))}px`;
	}

	function render(loading) {
		const steps = live();
		const n = Math.max(steps.indexOf(cur), 0);
		const last = n === steps.length - 1;
		els.count.textContent = loading ? 'Loading…' : `${n + 1} of ${steps.length}`;
		els.title.textContent = cur.title;
		els.body.innerHTML = typeof cur.body === 'function' ? cur.body() : cur.body;
		els.back.hidden = n === 0;
		els.skip.textContent = cur.skip || 'Skip tour';
		els.skip.hidden = last;
		els.next.textContent = cur.primary || (last ? 'Done' : 'Next');
		els.next.disabled = loading;
		els.back.disabled = loading;
		// Placed now AND on the next frame. Now, because browsers pause animation frames in a
		// tab that is not in front, and a tour left in a background tab came back with the
		// card still pointing at the previous step. Next frame, because fonts and the chart
		// panel can still change the card's height after this line runs.
		place();
		requestAnimationFrame(place);
		if (!loading) els.next.focus({ preventScroll: true });
	}

	async function show(step) {
		if (!step) return end();
		const my = ++token;
		cur = step;
		busy = true;
		render(true);
		try {
			if (step.before) await step.before();
		} catch {
			/* a step that fails to set the stage still explains itself */
		}
		if (my !== token || !root) return;
		busy = false;
		const first = Array.isArray(step.target) ? step.target[0] : step.target;
		if (first) document.querySelector(first)?.scrollIntoView?.({ block: 'nearest' });
		render(false);
		observe();
	}

	function observe() {
		if (ro) ro.disconnect();
		if (typeof ResizeObserver === 'undefined') return;
		ro = new ResizeObserver(() => requestAnimationFrame(place));
		ro.observe(els.card);
		const sels = cur && cur.target ? [].concat(cur.target) : [];
		for (const s of sels) {
			const el = document.querySelector(s);
			if (el) ro.observe(el);
		}
	}

	function next() {
		if (busy) return;
		const s = live();
		const i = s.indexOf(cur);
		if (i >= s.length - 1) return end();
		show(s[i + 1]);
	}

	function back() {
		if (busy) return;
		const s = live();
		const i = s.indexOf(cur);
		if (i > 0) show(s[i - 1]);
	}

	function onKey(e) {
		if (!root) return;
		// The command palette has its own keys; leave them alone while it is open.
		const palette = document.getElementById('paletteWrap');
		if (palette && !palette.hidden) return;
		if (e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
		if (e.key === 'Escape') {
			e.preventDefault();
			end();
		} else if (e.key === 'ArrowRight') {
			e.preventDefault();
			next();
		} else if (e.key === 'ArrowLeft') {
			e.preventDefault();
			back();
		}
	}

	function mount() {
		root = document.createElement('div');
		root.className = 'tour';
		root.innerHTML = `
      <div class="tourScrim" hidden></div>
      <div class="tourSpot" hidden></div>
      <section class="tourCard" role="dialog" aria-modal="false" aria-labelledby="tourTitle">
        <p class="tourStep" aria-live="polite"></p>
        <h2 id="tourTitle"></h2>
        <div class="tourBody"></div>
        <div class="tourNav">
          <button type="button" class="tourLink tourSkip"></button>
          <span class="grow"></span>
          <button type="button" class="ghost tourBack">Back</button>
          <button type="button" class="primary tourNext">Next</button>
        </div>
      </section>`;
		document.body.appendChild(root);
		els = {
			scrim: root.querySelector('.tourScrim'),
			spot: root.querySelector('.tourSpot'),
			card: root.querySelector('.tourCard'),
			count: root.querySelector('.tourStep'),
			title: root.querySelector('#tourTitle'),
			body: root.querySelector('.tourBody'),
			skip: root.querySelector('.tourSkip'),
			back: root.querySelector('.tourBack'),
			next: root.querySelector('.tourNext')
		};
		els.next.onclick = next;
		els.back.onclick = back;
		els.skip.onclick = end;
		addEventListener('keydown', onKey);
		addEventListener('resize', place);
	}

	function end() {
		token++;
		markSeen();
		if (ro) ro.disconnect();
		ro = null;
		removeEventListener('keydown', onKey);
		removeEventListener('resize', place);
		if (root) root.remove();
		root = null;
		cur = null;
		busy = false;
		document.getElementById('tourBtn')?.focus({ preventScroll: true });
	}

	function start() {
		if (root) return;
		mount();
		show(live()[0]);
	}

	/** Open on a first visit, or whenever the URL asks for it with ?tour=1.

	    Waits up to six seconds for the map first. Steps that need the map drop out while it
	    is loading, so starting too early reads "1 of 6" and then jumps to "2 of 9" - a
	    counter that changes its mind is the opposite of reassuring. */
	async function maybeAutostart() {
		const forced = /[?&]tour=1(&|$)/.test(location.search);
		if (!forced && seen()) return;
		const t0 = Date.now();
		while (!h.mapReady() && Date.now() - t0 < 6000) await sleep(200);
		await sleep(400);
		start();
	}

	return { start, end, maybeAutostart, isOpen: () => !!root };
}
