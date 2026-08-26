// Suitability map. The important idea here: per-factor subscores are baked
// into the tiles as integer properties, so switching weighting profiles is a
// pure style change -- MapLibre recomputes the weighted score in the GPU
// expression and nothing has to be re-tiled or re-fetched.

const RAMP = [
  [0,   '#2b0f3a'], [20,  '#5a1d55'], [40,  '#a83355'],
  [60,  '#e2683c'], [80,  '#f5b93b'], [100, '#f7f14e'],
];

const cfg = await fetch('./config.json').then(r => r.json());
const live = cfg.live_factors;

const proto = new pmtiles.Protocol();
maplibregl.addProtocol('pmtiles', proto.tile);

const map = new maplibregl.Map({
  container: 'map',
  style: {
    version: 8,
    sources: {
      score_r4: { type: 'vector', url: 'pmtiles://./tiles/score_r4.pmtiles' },
      score_r6: { type: 'vector', url: 'pmtiles://./tiles/score_r6.pmtiles' },
      score_r7: { type: 'vector', url: 'pmtiles://./tiles/score_r7.pmtiles' },
      states: { type: 'geojson', data: './states.geojson' },
    },
    layers: [
      { id: 'bg', type: 'background', paint: { 'background-color': '#0b0e13' } },
      // One visual surface, three H3 resolutions. Coarse cells carry the low
      // zooms so the map reads as a continuous choropleth instead of the
      // speckled scatter you get from thinning 1.5M hexes.
      { id: 'cells_r4', type: 'fill', source: 'score_r4', 'source-layer': 'score',
        maxzoom: 6, paint: { 'fill-color': '#333', 'fill-opacity': 0.9 } },
      { id: 'cells_r6', type: 'fill', source: 'score_r6', 'source-layer': 'score',
        minzoom: 6, maxzoom: 9, paint: { 'fill-color': '#333', 'fill-opacity': 0.9 } },
      { id: 'cells_r7', type: 'fill', source: 'score_r7', 'source-layer': 'score',
        minzoom: 9, paint: { 'fill-color': '#333', 'fill-opacity': 0.9 } },
      { id: 'state-line', type: 'line', source: 'states',
        paint: { 'line-color': '#3a4658', 'line-width': 0.7 } },
      { id: 'sel', type: 'line', source: 'score_r7', 'source-layer': 'score',
        minzoom: 9, filter: ['==', ['get', 'h3'], ''],
        paint: { 'line-color': '#fff', 'line-width': 2 } },
    ],
  },
  center: [-98.5, 39.5], zoom: 3.9, maxZoom: 13, minZoom: 3,
});
window.__map = map;
window.__errs = [];
map.on('error', (e) => { window.__errs.push(String(e && e.error || e)); console.error('MAP ERROR', e && e.error); });
map.addControl(new maplibregl.NavigationControl(), 'top-right');

// If the map is constructed while its container is hidden or unmeasured (a
// background tab, an embedded preview pane), MapLibre latches its 400x300
// fallback size and never fires 'load' because it never renders a frame.
// Re-measure at a few settling points. Each call is idempotent and cheap.
// Deliberately NOT a ResizeObserver: map.resize() mutates the canvas inside
// the observed element, which re-triggers the observer in a feedback loop.
function fit() {
  const el = document.getElementById('map');
  const c = map.getCanvas();
  if (el.clientWidth && c.clientWidth !== el.clientWidth) map.resize();
}
requestAnimationFrame(fit);
setTimeout(fit, 300);
addEventListener('resize', fit);
addEventListener('pageshow', fit);
document.addEventListener('visibilitychange', () => { if (!document.hidden) fit(); });

// Weighted score, computed in-expression over whichever factors a cell has.
// Factors absent from a cell drop out of BOTH numerator and denominator, so
// missing data never reads as "unsuitable".
function scoreExpr(weights) {
  const used = live.filter(f => (weights[f] ?? 0) > 0);
  if (!used.length) return ['get', 'score'];
  const num = ['+', ...used.map(f =>
    ['case', ['has', f], ['*', weights[f], ['get', f]], 0])];
  const den = ['+', ...used.map(f => ['case', ['has', f], weights[f], 0])];
  return ['case', ['>', den, 0], ['/', num, den], 0];
}

const CELL_LAYERS = ['cells_r4', 'cells_r6', 'cells_r7'];

function apply(profileId, minScore) {
  const w = cfg.profiles[profileId].weights;
  const expr = scoreExpr(w);
  for (const id of CELL_LAYERS) {
    map.setPaintProperty(id, 'fill-color',
      ['interpolate', ['linear'], expr, ...RAMP.flat()]);
    map.setFilter(id, minScore > 0 ? ['>=', expr, minScore] : null);
  }
  window.__expr = expr; window.__w = w;
}

// --- UI ---------------------------------------------------------------------
const sel = document.getElementById('profile');
for (const [id, p] of Object.entries(cfg.profiles)) {
  sel.add(new Option(p.label, id));
}
document.getElementById('ramp').style.background =
  `linear-gradient(90deg, ${RAMP.map(([s, c]) => `${c} ${s}%`).join(',')})`;

const minEl = document.getElementById('minscore');
const refresh = () => {
  document.getElementById('minval').textContent = minEl.value;
  document.getElementById('profile-desc').textContent =
    cfg.profiles[sel.value].description || '';
  apply(sel.value, +minEl.value);
};
sel.onchange = refresh;
minEl.oninput = refresh;

map.on('style.load', () => {
  fit();
  refresh();
  const missing = Object.keys(cfg.factors).filter(f => !live.includes(f));
  document.getElementById('status').textContent =
    `${live.length}/${Object.keys(cfg.factors).length} factors live`;
  if (missing.length) {
    document.getElementById('provisional').textContent =
      `PROVISIONAL — awaiting: ${missing.join(', ')}`;
  }
});

map.on('click', CELL_LAYERS, (e) => {
  const p = e.features[0].properties;
  const w = cfg.profiles[sel.value].weights;
  let num = 0, den = 0;
  const rows = [];
  for (const f of live) {
    if (p[f] === undefined) continue;
    const wt = w[f] ?? 0;
    num += wt * p[f]; den += wt;
    rows.push([cfg.factors[f].label, p[f], wt]);
  }
  document.getElementById('cell-score').textContent =
    den > 0 ? Math.round(num / den) : '–';
  document.getElementById('cell-factors').innerHTML = rows
    .sort((a, b) => b[2] - a[2])
    .map(([label, v, wt]) => `
      <div class="frow">
        <span class="fname">${label}</span>
        <span class="fbar"><i style="width:${v}%"></i></span>
        <span class="fval">${v}</span>
      </div>`).join('') +
    `<div class="frow" style="margin-top:8px;opacity:.6">
       <span class="fname">factors present</span>
       <span class="fval">${p.nf}</span></div>`;
  document.getElementById('readout').classList.remove('hidden');
  map.setFilter('sel', ['==', ['get', 'h3'], p.h3 ?? '']);
});
map.on('mouseenter', CELL_LAYERS, () => map.getCanvas().style.cursor = 'pointer');
map.on('mouseleave', CELL_LAYERS, () => map.getCanvas().style.cursor = '');
