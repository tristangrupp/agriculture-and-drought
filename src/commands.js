/**
 * The command registry behind ⌘K.
 *
 * Structure borrowed from the WRI command-palette experiment: registered commands are
 * matched first and run deterministically; anything unmatched falls through to a single
 * always-last row that turns the typed text into a question rather than a dead end.
 *
 * Commands are plain objects so a demo can register and unregister its own without the
 * palette knowing anything about it.
 */

import { CONFIG } from '../config.js';

/** Very small subsequence scorer — good enough to rank a few dozen commands. */
export function score(query, text) {
	const q = query.toLowerCase().trim();
	const t = text.toLowerCase();
	if (!q) return 0.001;
	if (t.startsWith(q)) return 1000 - t.length;
	const idx = t.indexOf(q);
	if (idx >= 0) return 500 - idx - t.length * 0.01;
	// subsequence: every query char in order
	let i = 0;
	let gaps = 0;
	for (const c of t) {
		if (c === q[i]) i++;
		else if (i > 0) gaps++;
		if (i === q.length) return 100 - gaps * 0.1;
	}
	return -1;
}

export function createRegistry() {
	const commands = new Map();

	function register(cmd) {
		commands.set(cmd.id, cmd);
		return () => commands.delete(cmd.id);
	}

	function registerAll(list) {
		const offs = list.map(register);
		return () => offs.forEach((f) => f());
	}

	/**
	 * Rank commands for a query. A command may expose `match(query)` to claim free text
	 * (used by the parameterised ones like "fly to <place>"), which wins over name scoring
	 * so that typing a place name does not first have to beat every other command.
	 */
	function search(query) {
		const out = [];
		for (const c of commands.values()) {
			if (c.when && !c.when()) continue;
			let s = -1;
			let arg;
			if (c.match) {
				const m = c.match(query);
				if (m) {
					s = 900;
					arg = m;
				}
			}
			if (s < 0) {
				s = Math.max(
					score(query, c.title),
					c.keywords ? Math.max(...c.keywords.map((k) => score(query, k))) - 1 : -1
				);
			}
			if (s > 0) out.push({ cmd: c, score: s, arg });
		}
		out.sort((a, b) => b.score - a.score);
		return out.slice(0, 8);
	}

	return { register, registerAll, search, commands };
}

/**
 * The core command set. `ctx` is the application surface the commands act on, passed in
 * rather than imported so this module stays testable and the map is never a global.
 */
export function coreCommands(ctx) {
	const M = CONFIG.DROUGHT_METRICS;

	const cmds = [
		...M.map((m) => ({
			id: `metric.${m.key}`,
			title: `Colour basins by ${m.label.toLowerCase()}`,
			group: 'Drought',
			keywords: [m.key, m.label, 'metric', 'colour', 'color'],
			run: () => ctx.setMetric(m.key)
		})),
		{
			id: 'fields.load',
			title: 'Load fields in view',
			group: 'Fields',
			keywords: ['plots', 'query', 'duckdb', 'show fields'],
			run: () => ctx.loadFields()
		},
		{
			id: 'fields.clear',
			title: 'Clear fields',
			group: 'Fields',
			keywords: ['hide fields', 'remove'],
			when: () => ctx.hasFields(),
			run: () => ctx.clearFields()
		},
		{
			id: 'fields.cropland',
			title: 'Show cropland fields only',
			group: 'Fields',
			keywords: ['crops', 'agriculture', 'soy', 'filter'],
			run: () => ctx.loadFields({ where: `mb_class IN (${CONFIG.CROP_CLASSES.join(',')})` })
		},
		{
			id: 'fields.pasture',
			title: 'Show pasture fields only',
			group: 'Fields',
			keywords: ['grazing', 'cattle', 'filter'],
			run: () => ctx.loadFields({ where: 'mb_class = 15' })
		},
		{
			id: 'fields.large',
			title: 'Show fields larger than 100 ha',
			group: 'Fields',
			keywords: ['big', 'large', 'size', 'filter'],
			run: () => ctx.loadFields({ where: 'area_ha >= 100' })
		},
		{
			id: 'fields.cleared',
			title: 'Show fields with recent forest loss',
			group: 'Fields',
			keywords: ['hansen', 'deforestation', 'cleared', 'loss'],
			run: () =>
				ctx.loadFields({ where: 'hansen_loss_year >= 19 AND hansen_loss_frac > 0.05' })
		},
		{
			id: 'fields.summary',
			title: 'Summarise land cover in view',
			group: 'Fields',
			keywords: ['composition', 'classes', 'breakdown', 'what is here'],
			run: () => ctx.summariseView()
		},
		{
			id: 'basins.worst',
			title: 'Go to the worst-hit basin',
			group: 'Drought',
			keywords: ['driest', 'longest', 'extreme', 'worst'],
			run: () => ctx.flyToWorst()
		},
		{
			id: 'basins.isolate',
			title: 'Isolate the selected basin',
			group: 'Drought',
			keywords: ['focus', 'only', 'filter basin'],
			when: () => ctx.hasSelection(),
			run: () => ctx.isolateSelected()
		},
		{
			id: 'basins.clearIsolate',
			title: 'Show all basins again',
			group: 'Drought',
			keywords: ['reset', 'unfilter'],
			when: () => ctx.isIsolated(),
			run: () => ctx.clearIsolate()
		},
		{
			id: 'view.brazil',
			title: 'Zoom out to Brazil',
			group: 'View',
			keywords: ['reset view', 'home', 'overview'],
			run: () => ctx.flyToBrazil()
		},
		{
			id: 'view.backToBasins',
			title: 'Back to all basins',
			group: 'View',
			keywords: ['basins', 'zoom out', 'overview', 'brazil', 'back', 'exit fields'],
			run: () => ctx.backToBasins()
		},
		{
			id: 'help.tour',
			title: 'Take the guided tour',
			group: 'Help',
			keywords: ['tour', 'help', 'guide', 'intro', 'start', 'what is this', 'new'],
			run: () => ctx.startTour()
		},
		{
			id: 'view.basemap',
			title: 'Toggle satellite basemap',
			group: 'View',
			keywords: ['imagery', 'aerial', 'satellite', 'background'],
			run: () => ctx.toggleBasemap()
		},
		{
			id: 'view.legend',
			title: 'Toggle the drought legend',
			group: 'View',
			keywords: ['key', 'scale'],
			run: () => ctx.toggleLegend()
		},
		/* Parameterised: claims any text of the form "fly to X" / "go to X". */
		{
			id: 'view.flyTo',
			title: 'Fly to a place…',
			group: 'View',
			keywords: ['goto', 'navigate', 'search place'],
			match: (q) => {
				const m = /^(?:fly|go|zoom|jump)\s+to\s+(.{2,})$/i.exec(q.trim());
				return m ? m[1].trim() : null;
			},
			hint: (arg) => `Geocode “${arg}” and fly there`,
			run: (arg) => ctx.flyToPlace(arg)
		},
		/* Parameterised: "basins over 150 days" and similar. */
		{
			id: 'basins.threshold',
			title: 'Highlight basins over a threshold…',
			group: 'Drought',
			keywords: ['over', 'above', 'more than', 'threshold'],
			match: (q) => {
				const m = /(?:over|above|more than|>)\s*(\d+)\s*(days?|events?|mm)?/i.exec(q.trim());
				if (!m) return null;
				const unit = (m[2] || 'days').toLowerCase();
				const key = unit.startsWith('event')
					? 'n_events'
					: unit === 'mm'
						? 'total_deficit_mm'
						: 'total_drought_days';
				return { n: Number(m[1]), key };
			},
			hint: (a) =>
				`Highlight basins with ${a.key.replace(/_/g, ' ')} over ${a.n}`,
			run: (a) => ctx.highlightThreshold(a.key, a.n)
		}
	];

	return cmds;
}
