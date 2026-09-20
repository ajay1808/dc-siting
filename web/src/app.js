// Suitability map.
//
// Two ideas carry this UI:
//   1. Per-factor subscores are baked into the tiles as integers, so changing
//      weights is a pure style recompute -- no refetch, no re-tiling.
//   2. Nothing is asserted without provenance: every factor carries its
//      source, what it measures, and its known caveat.

const RAMP = [[0,'#2b0f3a'],[20,'#5a1d55'],[40,'#a83355'],
              [60,'#e2683c'],[80,'#f5b93b'],[100,'#f7f14e']];
const LS_KEY = 'dcsiting.customWeights';

const cfg = await fetch('./config.json').then(r => r.json());
const live = cfg.live_factors;
const CELL_LAYERS = ['cells_r4', 'cells_r6', 'cells_r7'];

const proto = new pmtiles.Protocol();
maplibregl.addProtocol('pmtiles', proto.tile);

const map = new maplibregl.Map({
  container: 'map',
  style: {
    version: 8,
    sources: {
      score_r4: { type:'vector', url:'pmtiles://./tiles/score_r4.pmtiles' },
      score_r6: { type:'vector', url:'pmtiles://./tiles/score_r6.pmtiles' },
      score_r7: { type:'vector', url:'pmtiles://./tiles/score_r7.pmtiles' },
      states:   { type:'geojson', data:'./states.geojson' },
    },
    layers: [
      { id:'bg', type:'background', paint:{'background-color':'#0b0e13'} },
      { id:'cells_r4', type:'fill', source:'score_r4', 'source-layer':'score',
        maxzoom:6, paint:{'fill-color':'#333','fill-opacity':0.9} },
      { id:'cells_r6', type:'fill', source:'score_r6', 'source-layer':'score',
        minzoom:6, maxzoom:9, paint:{'fill-color':'#333','fill-opacity':0.9} },
      { id:'cells_r7', type:'fill', source:'score_r7', 'source-layer':'score',
        minzoom:9, paint:{'fill-color':'#333','fill-opacity':0.9} },
      { id:'state-line', type:'line', source:'states',
        paint:{'line-color':'#3a4658','line-width':0.7} },
      { id:'sel', type:'line', source:'score_r7', 'source-layer':'score',
        minzoom:9, filter:['==',['get','h3'],''],
        paint:{'line-color':'#fff','line-width':2} },
    ],
  },
  center:[-98.5,39.5], zoom:3.9, maxZoom:13, minZoom:3,
});
window.__map = map; window.__errs = [];
map.on('error', e => window.__errs.push(String(e && e.error || e)));
map.addControl(new maplibregl.NavigationControl(), 'top-right');

// Constructed before layout settles (module top-level await), so MapLibre can
// latch its 400x300 fallback and never fire 'load'. Re-measure at a few
// settling points. NOT a ResizeObserver: resize() mutates the observed
// element, which feeds back into the observer.
function fit(){
  const el = document.getElementById('map');
  if (el.clientWidth && map.getCanvas().clientWidth !== el.clientWidth) map.resize();
}
requestAnimationFrame(fit); setTimeout(fit, 300);
addEventListener('resize', fit); addEventListener('pageshow', fit);
document.addEventListener('visibilitychange', () => { if(!document.hidden) fit(); });

// ── weights ────────────────────────────────────────────────────────────────
let weights = { ...cfg.profiles.default.weights };
try {
  const saved = JSON.parse(localStorage.getItem(LS_KEY) || 'null');
  if (saved) weights = saved;
} catch (_) { /* private mode / blocked storage: fall back to the profile */ }

// Factors absent from a cell drop out of BOTH numerator and denominator, so
// missing data never reads as "unsuitable".
function scoreExpr(w){
  const used = live.filter(f => (w[f] ?? 0) > 0);
  if (!used.length) return ['get','score'];
  const num = ['+', ...used.map(f => ['case',['has',f],['*', w[f], ['get',f]],0])];
  const den = ['+', ...used.map(f => ['case',['has',f], w[f], 0])];
  return ['case',['>',den,0],['/',num,den],0];
}
function apply(){
  const expr = scoreExpr(weights);
  const min = +document.getElementById('minscore').value;
  for (const id of CELL_LAYERS){
    map.setPaintProperty(id,'fill-color',
      ['interpolate',['linear'],expr,...RAMP.flat()]);
    map.setFilter(id, min > 0 ? ['>=',expr,min] : null);
  }
}
function persist(){
  try { localStorage.setItem(LS_KEY, JSON.stringify(weights)); } catch(_){}
}

// ── UI: profile + ramp ─────────────────────────────────────────────────────
const sel = document.getElementById('profile');
for (const [id,p] of Object.entries(cfg.profiles)) sel.add(new Option(p.label,id));
sel.add(new Option('Custom','custom'));
document.getElementById('ramp').style.background =
  `linear-gradient(90deg,${RAMP.map(([s,c])=>`${c} ${s}%`).join(',')})`;

sel.onchange = () => {
  if (sel.value !== 'custom'){
    weights = { ...cfg.profiles[sel.value].weights };
    persist(); renderFactors();
  }
  document.getElementById('profile-desc').textContent =
    sel.value === 'custom' ? 'Your own weighting.'
                           : (cfg.profiles[sel.value].description || '');
  apply();
};
const minEl = document.getElementById('minscore');
minEl.oninput = () => { document.getElementById('minval').textContent = minEl.value; apply(); };
document.getElementById('reset').onclick = () => {
  const base = sel.value === 'custom' ? 'default' : sel.value;
  weights = { ...cfg.profiles[base].weights };
  sel.value = base; persist(); renderFactors(); apply();
};

// ── UI: tabs ───────────────────────────────────────────────────────────────
for (const b of document.querySelectorAll('.tabs button')){
  b.onclick = () => {
    document.querySelectorAll('.tabs button').forEach(x=>x.classList.toggle('on',x===b));
    document.querySelectorAll('[data-panel]').forEach(p=>{
      p.hidden = p.dataset.panel !== b.dataset.tab;
    });
  };
}

// ── UI: factor list with weights + provenance ──────────────────────────────
function renderFactors(){
  const host = document.getElementById('factors');
  const total = Object.entries(weights)
    .filter(([f]) => cfg.factors[f]?.live)
    .reduce((a,[,v]) => a + (v||0), 0);
  host.innerHTML = '';
  for (const [id,f] of Object.entries(cfg.factors)){
    const w = weights[id] ?? 0;
    const pct = total > 0 && f.live ? (w/total*100) : 0;
    const row = document.createElement('div');
    row.className = 'frow' + (f.live ? '' : ' dead');
    row.innerHTML = `
      <div class="fhead">
        <button class="info" data-f="${id}" title="source & meaning">i</button>
        <span class="fname">${f.label}${f.live?'':' <span class="tag">no data</span>'}</span>
        <span class="fnum">${pct.toFixed(1)}%</span>
      </div>
      <input type="range" min="0" max="0.4" step="0.005" value="${w}" data-w="${id}">
      <div class="bar"><i style="width:${pct}%"></i></div>`;
    host.appendChild(row);
  }
  document.getElementById('wsum').textContent =
    `${live.length}/${Object.keys(cfg.factors).length} factors carry data`;
  host.querySelectorAll('input[data-w]').forEach(r => {
    r.oninput = () => {
      weights[r.dataset.w] = +r.value;
      sel.value = 'custom';
      document.getElementById('profile-desc').textContent = 'Your own weighting.';
      persist(); renderFactors(); apply();
    };
  });
  host.querySelectorAll('button.info').forEach(b => { b.onclick = () => showInfo(b.dataset.f); });
}

function showInfo(id){
  const f = cfg.factors[id];
  document.getElementById('info-card').innerHTML = `
    <h3>${f.label}</h3>
    <div class="kv"><span>Source</span><span>${f.source}</span></div>
    ${f.detail?`<div class="kv"><span>Coverage</span><span>${f.detail}</span></div>`:''}
    ${f.source_url?`<div class="kv"><span>Link</span><span><a href="${f.source_url}" target="_blank" rel="noopener">${f.source_url}</a></span></div>`:''}
    <div class="kv"><span>Weight</span><span>${((weights[id]??0)*100).toFixed(1)} (relative)</span></div>
    <p style="margin:12px 0 0">${f.means}</p>
    ${f.caveat?`<p class="cav"><b>Caveat:</b> ${f.caveat}</p>`:''}
    ${f.live?'':'<p class="cav"><b>Not live:</b> this factor has no data yet and is excluded from the score.</p>'}
    <button id="info-close">Close</button>`;
  document.getElementById('info').hidden = false;
  document.getElementById('info-close').onclick = () => { document.getElementById('info').hidden = true; };
}
document.getElementById('info').onclick = e => {
  if (e.target.id === 'info') e.currentTarget.hidden = true;
};

// ── cell inspection ────────────────────────────────────────────────────────
let current = null;
const STATE = {'01':'AL','04':'AZ','05':'AR','06':'CA','08':'CO','09':'CT','10':'DE',
 '11':'DC','12':'FL','13':'GA','16':'ID','17':'IL','18':'IN','19':'IA','20':'KS',
 '21':'KY','22':'LA','23':'ME','24':'MD','25':'MA','26':'MI','27':'MN','28':'MS',
 '29':'MO','30':'MT','31':'NE','32':'NV','33':'NH','34':'NJ','35':'NM','36':'NY',
 '37':'NC','38':'ND','39':'OH','40':'OK','41':'OR','42':'PA','44':'RI','45':'SC',
 '46':'SD','47':'TN','48':'TX','49':'UT','50':'VT','51':'VA','53':'WA','54':'WV',
 '55':'WI','56':'WY'};

map.on('click', CELL_LAYERS, e => {
  const feat = e.features[0];
  const p = feat.properties;
  current = { props:p, geometry:feat.geometry };

  let num=0, den=0; const rows=[];
  for (const f of live){
    if (p[f] === undefined) continue;
    const w = weights[f] ?? 0;
    num += w*p[f]; den += w;
    if (w > 0) rows.push([cfg.factors[f].label, p[f], w]);
  }
  document.getElementById('cell-score').textContent = den>0 ? Math.round(num/den) : '–';

  const counties = (p.cnames || '').split('|').filter(Boolean);
  const st = STATE[p.st] || p.st;
  document.getElementById('cell-geo').innerHTML =
    `<div class="kv"><span>H3 index</span><span>${p.h3 ?? '—'}</span></div>
     <div class="kv"><span>Resolution</span><span>r${cfg.resolution} · ~5.16 km²</span></div>
     <div class="kv"><span>State</span><span>${st ?? '—'}</span></div>
     <div class="kv"><span>${counties.length>1?'Counties':'County'}</span><span>${
       counties.length ? counties.join(', ') : '—'}</span></div>
     <div class="kv"><span>Centre</span><span>${e.lngLat.lat.toFixed(4)}, ${e.lngLat.lng.toFixed(4)}</span></div>
     <div class="kv"><span>Factors used</span><span>${p.nf}</span></div>`;

  const notes=[];
  for (const fl of cfg.flags) if (p['fl_'+fl.id]) notes.push(`<span class="flag">⚑ ${fl.label}</span>`);
  for (const [k,v] of Object.entries(cfg.exclusions||{}))
    if (p['x_'+k]) notes.push(`<span class="exc">✖ ${v.reason} (score ×${v.multiplier})</span>`);
  document.getElementById('cell-flags').innerHTML = notes.join('');

  document.getElementById('cell-factors').innerHTML = rows
    .sort((a,b)=>b[2]-a[2])
    .map(([l,v])=>`<div class="brow"><span class="bn">${l}</span>
      <span class="bb"><i style="width:${v}%"></i></span><span class="bv">${v}</span></div>`).join('')
    || '<p class="desc">No factors carry data here.</p>';

  document.getElementById('h3note').textContent =
    counties.length>1 ? 'This hexagon straddles a county boundary; all counties it touches are listed.' : '';
  document.getElementById('cell-empty').hidden = true;
  document.getElementById('cell-body').hidden = false;
  document.querySelector('.tabs button[data-tab="cell"]').click();
  map.setFilter('sel', ['==',['get','h3'], p.h3 ?? '']);
});
map.on('mouseenter', CELL_LAYERS, ()=>map.getCanvas().style.cursor='pointer');
map.on('mouseleave', CELL_LAYERS, ()=>map.getCanvas().style.cursor='');

// H3 index is the portable identifier; GeoJSON carries the geometry itself.
document.getElementById('copy-h3').onclick = async () => {
  if (!current) return;
  try { await navigator.clipboard.writeText(current.props.h3 ?? ''); }
  catch(_) { /* clipboard blocked; index is visible above regardless */ }
  const b = document.getElementById('copy-h3');
  b.textContent = 'Copied'; setTimeout(()=>b.textContent='Copy H3 index', 1200);
};
document.getElementById('dl-geojson').onclick = () => {
  if (!current) return;
  const fc = { type:'Feature', geometry: current.geometry,
    properties: { ...current.props, h3_resolution: cfg.resolution,
                  generated: new Date().toISOString() } };
  const url = URL.createObjectURL(new Blob([JSON.stringify(fc,null,1)],
    { type:'application/geo+json' }));
  const a = document.createElement('a');
  a.href = url; a.download = `h3_${current.props.h3 || 'cell'}.geojson`; a.click();
  URL.revokeObjectURL(url);
};

// ── boot ───────────────────────────────────────────────────────────────────
map.on('style.load', () => {
  fit();
  sel.value = 'default';
  document.getElementById('profile-desc').textContent = cfg.profiles.default.description || '';
  renderFactors(); apply();

  const c = cfg.calibration || {};
  if (c.n_positive) document.getElementById('calib').textContent =
    `Weights fitted against ${c.n_positive} operating campuses and ` +
    `${c.n_negative} known-bad sites (mean ${c.positives_mean} vs ${c.negatives_mean}).`;
  document.getElementById('excl').innerHTML = Object.entries(cfg.exclusions||{})
    .map(([,v])=>`<span class="exc">✖ ${v.reason}</span>`).join('');

  const missing = Object.keys(cfg.factors).filter(f=>!live.includes(f));
  document.getElementById('status').textContent =
    `${live.length}/${Object.keys(cfg.factors).length} factors live`;
  if (missing.length) document.getElementById('provisional').textContent =
    `No data yet: ${missing.join(', ')}`;
});
