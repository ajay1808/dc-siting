// Suitability map.
//
// Design notes:
//  * Per-factor subscores are baked into the tiles as integers, so changing
//    weights is a pure style recompute -- no refetch, no re-tiling.
//  * Nothing is asserted without provenance: every factor carries its source,
//    what it measures, and its known caveat.
//  * AI features are BRING-YOUR-OWN-KEY. The key lives in this browser's
//    localStorage and is sent only to api.anthropic.com. This site has no
//    backend and no shared key to abuse.

const RAMP = [[0,'#2b0f3a'],[20,'#5a1d55'],[40,'#a83355'],
              [60,'#e2683c'],[80,'#f5b93b'],[100,'#f7f14e']];
const LS_W = 'dcsiting.customWeights';
const LS_K = 'dcsiting.anthropicKey';
const LS_SEEN = 'dcsiting.seenAbout';
const CELL_LAYERS = ['cells_r4','cells_r6','cells_r7'];

const cfg = await fetch('./config.json').then(r => r.json());
const live = cfg.live_factors;
const store = {
  get(k){ try { return localStorage.getItem(k); } catch(_) { return null; } },
  set(k,v){ try { localStorage.setItem(k,v); } catch(_){} },
};

const proto = new pmtiles.Protocol();
maplibregl.addProtocol('pmtiles', proto.tile);

const map = new maplibregl.Map({
  container:'map', boxZoom:false,     // shift-drag is our region select
  style:{ version:8,
    sources:{
      score_r4:{type:'vector',url:'pmtiles://./tiles/score_r4.pmtiles'},
      score_r6:{type:'vector',url:'pmtiles://./tiles/score_r6.pmtiles'},
      score_r7:{type:'vector',url:'pmtiles://./tiles/score_r7.pmtiles'},
      states:{type:'geojson',data:'./states.geojson'},
      sel:{type:'geojson',data:{type:'FeatureCollection',features:[]}},
      sites:{type:'geojson',data:{type:'FeatureCollection',features:[]}},
    },
    layers:[
      {id:'bg',type:'background',paint:{'background-color':'#0b0e13'}},
      {id:'cells_r4',type:'fill',source:'score_r4','source-layer':'score',
        maxzoom:6,paint:{'fill-color':'#333','fill-opacity':0.9}},
      {id:'cells_r6',type:'fill',source:'score_r6','source-layer':'score',
        minzoom:6,maxzoom:9,paint:{'fill-color':'#333','fill-opacity':0.9}},
      {id:'cells_r7',type:'fill',source:'score_r7','source-layer':'score',
        minzoom:9,paint:{'fill-color':'#333','fill-opacity':0.9}},
      {id:'state-line',type:'line',source:'states',
        paint:{'line-color':'#3a4658','line-width':0.7}},
      {id:'sel-fill',type:'fill',source:'sel',
        paint:{'fill-color':'#4da3ff','fill-opacity':0.18}},
      {id:'sel-line',type:'line',source:'sel',
        paint:{'line-color':'#4da3ff','line-width':1.4}},
      {id:'site-poly',type:'line',source:'sites',filter:['!=',['geometry-type'],'Point'],
        paint:{'line-color':'#f7f14e','line-width':1.6}},
      {id:'site-pt',type:'circle',source:'sites',filter:['==',['geometry-type'],'Point'],
        paint:{'circle-radius':5,'circle-color':'#f7f14e',
               'circle-stroke-color':'#000','circle-stroke-width':1}},
      {id:'hi',type:'line',source:'score_r7','source-layer':'score',
        minzoom:9,filter:['==',['get','h3'],''],
        paint:{'line-color':'#fff','line-width':2}},
    ]},
  center:[-98.5,39.5], zoom:3.9, maxZoom:13, minZoom:3,
});
window.__map = map; window.__errs = [];
map.on('error', e => window.__errs.push(String(e && e.error || e)));
map.addControl(new maplibregl.NavigationControl(),'top-right');

// Constructed before layout settles, so MapLibre can latch its 400x300
// fallback and never fire 'load'. Re-measure at settling points. NOT a
// ResizeObserver: resize() mutates the observed element, feeding back in.
function fit(){
  const el=document.getElementById('map');
  if(el.clientWidth && map.getCanvas().clientWidth!==el.clientWidth) map.resize();
}
requestAnimationFrame(fit); setTimeout(fit,300);
addEventListener('resize',fit); addEventListener('pageshow',fit);
document.addEventListener('visibilitychange',()=>{ if(!document.hidden) fit(); });

// ── weights ────────────────────────────────────────────────────────────────
let weights = {...cfg.profiles.default.weights};
try { const s=JSON.parse(store.get(LS_W)||'null'); if(s) weights=s; } catch(_){}

// `xm` is the exclusion multiplier x100 baked per cell. It MUST be applied
// here too: without it a reweighted map shows protected land and wetland as
// ordinary developable ground.
function scoreExpr(w){
  const used = live.filter(f => (w[f]??0) > 0);
  if(!used.length) return ['get','score'];
  const num=['+',...used.map(f=>['case',['has',f],['*',w[f],['get',f]],0])];
  const den=['+',...used.map(f=>['case',['has',f],w[f],0])];
  const base=['case',['>',den,0],['/',num,den],0];
  const mult=['/',['coalesce',['get','xm'],100],100];
  return ['*',base,mult];
}
function scoreOf(props,w=weights){
  let n=0,d=0;
  for(const f of live){ if(props[f]===undefined) continue;
    const x=w[f]??0; n+=x*props[f]; d+=x; }
  if(d<=0) return null;
  const mult=(props.xm===undefined?100:props.xm)/100;
  return (n/d)*mult;
}
function apply(){
  const e=scoreExpr(weights), m=+document.getElementById('minscore').value;
  for(const id of CELL_LAYERS){
    map.setPaintProperty(id,'fill-color',['interpolate',['linear'],e,...RAMP.flat()]);
    map.setFilter(id, m>0?['>=',e,m]:null);
  }
}
const persist=()=>store.set(LS_W,JSON.stringify(weights));

// ── tabs ───────────────────────────────────────────────────────────────────
function showTab(name){
  document.querySelectorAll('.tabs button').forEach(b=>b.classList.toggle('on',b.dataset.tab===name));
  document.querySelectorAll('[data-panel]').forEach(p=>{p.hidden=p.dataset.panel!==name;});
}
document.querySelectorAll('.tabs button').forEach(b=>{b.onclick=()=>showTab(b.dataset.tab);});

// ── profile + ramp ─────────────────────────────────────────────────────────
const sel=document.getElementById('profile');
for(const [id,p] of Object.entries(cfg.profiles)) sel.add(new Option(p.label,id));
sel.add(new Option('Custom','custom'));
document.getElementById('ramp').style.background =
  `linear-gradient(90deg,${RAMP.map(([s,c])=>`${c} ${s}%`).join(',')})`;
sel.onchange=()=>{
  if(sel.value!=='custom'){ weights={...cfg.profiles[sel.value].weights}; persist(); renderFactors(); }
  document.getElementById('profile-desc').textContent =
    sel.value==='custom'?'Your own weighting.':(cfg.profiles[sel.value].description||'');
  apply(); rerankSites();
};
const minEl=document.getElementById('minscore');
minEl.oninput=()=>{document.getElementById('minval').textContent=minEl.value;apply();};
document.getElementById('reset').onclick=()=>{
  const b=sel.value==='custom'?'default':sel.value;
  weights={...cfg.profiles[b].weights}; sel.value=b; persist(); renderFactors(); apply(); rerankSites();
};

// ── factor list + provenance ───────────────────────────────────────────────
function renderFactors(){
  const host=document.getElementById('factors');
  const total=Object.entries(weights).filter(([f])=>cfg.factors[f]?.live)
    .reduce((a,[,v])=>a+(v||0),0);
  host.innerHTML='';
  for(const [id,f] of Object.entries(cfg.factors)){
    const w=weights[id]??0, pct=total>0&&f.live?(w/total*100):0;
    const d=document.createElement('div');
    d.className='frow'+(f.live?'':' dead');
    d.innerHTML=`<div class="fhead">
        <button class="info" data-f="${id}">i</button>
        <span class="fname">${f.label}${f.live?'':' <span class="tag">no data</span>'}</span>
        <span class="fnum">${pct.toFixed(1)}%</span></div>
      <input type="range" min="0" max="0.4" step="0.005" value="${w}" data-w="${id}">
      <div class="bar"><i style="width:${pct}%"></i></div>`;
    host.appendChild(d);
  }
  document.getElementById('wsum').textContent =
    `${live.length}/${Object.keys(cfg.factors).length} factors carry data`;
  host.querySelectorAll('input[data-w]').forEach(r=>{r.oninput=()=>{
    weights[r.dataset.w]=+r.value; sel.value='custom';
    document.getElementById('profile-desc').textContent='Your own weighting.';
    persist(); renderFactors(); apply(); rerankSites();
  };});
  host.querySelectorAll('button.info').forEach(b=>{b.onclick=()=>showInfo(b.dataset.f);});
}
function showInfo(id){
  const f=cfg.factors[id];
  document.getElementById('info-card').innerHTML=`
    <h3>${f.label}</h3>
    <div class="kv"><span>Source</span><span>${f.source}</span></div>
    ${f.detail?`<div class="kv"><span>Coverage</span><span>${f.detail}</span></div>`:''}
    ${f.source_url?`<div class="kv"><span>Link</span><span><a href="${f.source_url}" target="_blank" rel="noopener">${f.source_url}</a></span></div>`:''}
    <div class="kv"><span>Weight</span><span>${((weights[id]??0)*100).toFixed(1)} (relative)</span></div>
    <p style="margin:12px 0 0">${f.means}</p>
    ${f.caveat?`<p class="cav"><b>Caveat:</b> ${f.caveat}</p>`:''}
    ${f.live?'':'<p class="cav"><b>Not live:</b> no data yet; excluded from the score.</p>'}
    <button id="info-close">Close</button>`;
  document.getElementById('info').hidden=false;
  document.getElementById('info-close').onclick=()=>{document.getElementById('info').hidden=true;};
}
document.getElementById('info').onclick=e=>{ if(e.target.id==='info') e.currentTarget.hidden=true; };

// Always-visible source list, so provenance is not hidden behind a click.
function renderSources(){
  document.getElementById('srclist').innerHTML=Object.entries(cfg.factors).map(([id,f])=>
    `<div><span>${f.label}</span>${f.source_url
      ? `<a href="${f.source_url}" target="_blank" rel="noopener">${f.source}</a>`
      : `<a>${f.source}</a>`}</div>`).join('');
}

// ── AI: bring your own key ─────────────────────────────────────────────────
// No backend, no shared key. The key is stored in this browser only and sent
// only to api.anthropic.com. A shared proxy key on a public site gets drained.
const getKey = () => store.get(LS_K) || '';
function showKeyModal(reason){
  document.getElementById('key-card').innerHTML=`
    <h2>AI key</h2>
    <p>The AI features (site recommendations, policy news) call Anthropic's API
       directly from your browser using <b>your own key</b>.</p>
    <p class="cav">This site has no backend and ships no key of its own. Yours is
       stored in this browser's localStorage, is sent only to api.anthropic.com,
       and never reaches this server or anyone else. Usage bills to your account.</p>
    ${reason?`<p class="cav">${reason}</p>`:''}
    <input id="key-input" type="password" placeholder="sk-ant-..." value="${getKey()}">
    <p style="font-size:11px;color:var(--muted);margin-top:8px">
      Get one at <a href="https://console.anthropic.com/settings/keys" target="_blank"
      rel="noopener">console.anthropic.com</a>. Everything else on this map works
      without a key.</p>
    <div class="wbar" style="margin-top:14px">
      <button id="key-save">Save</button>
      <button id="key-clear">Remove key</button>
      <button id="key-close">Close</button></div>`;
  document.getElementById('keymodal').hidden=false;
  document.getElementById('key-save').onclick=()=>{
    store.set(LS_K,document.getElementById('key-input').value.trim());
    document.getElementById('keymodal').hidden=true;
  };
  document.getElementById('key-clear').onclick=()=>{
    store.set(LS_K,''); document.getElementById('key-input').value='';
  };
  document.getElementById('key-close').onclick=()=>{document.getElementById('keymodal').hidden=true;};
}
document.getElementById('open-key').onclick=()=>showKeyModal();
document.getElementById('keymodal').onclick=e=>{ if(e.target.id==='keymodal') e.currentTarget.hidden=true; };

async function askClaude(prompt,{search=false,maxTokens=1400}={}){
  const key=getKey();
  if(!key){ showKeyModal('Add a key to use this feature.'); throw new Error('no key'); }
  const body={model:'claude-sonnet-5',max_tokens:maxTokens,
    messages:[{role:'user',content:prompt}]};
  if(search) body.tools=[{type:'web_search_20250305',name:'web_search',max_uses:4}];
  const r=await fetch('https://api.anthropic.com/v1/messages',{
    method:'POST',
    headers:{'content-type':'application/json','x-api-key':key,
      'anthropic-version':'2023-06-01',
      'anthropic-dangerous-direct-browser-access':'true'},
    body:JSON.stringify(body)});
  if(!r.ok){
    const t=await r.text().catch(()=>'');
    let msg=`HTTP ${r.status}`;
    try{ msg=JSON.parse(t).error.message||msg; }catch(_){}
    if(r.status===401) showKeyModal('That key was rejected. Check it and save again.');
    throw new Error(msg);
  }
  const d=await r.json();
  const text=(d.content||[]).filter(b=>b.type==='text').map(b=>b.text).join('');
  // Citations come back attached to text blocks when web search ran.
  const urls=new Set();
  for(const b of (d.content||[])){
    for(const c of (b.citations||[])) if(c.url) urls.add(c.url);
    if(b.type==='web_search_tool_result')
      for(const it of (b.content||[])) if(it.url) urls.add(it.url);
  }
  return {text,urls:[...urls]};
}
function renderAI(host,{text,urls}){
  host.innerHTML=`<div class="aiout">${text.replace(/</g,'&lt;')}</div>`+
    (urls.length?`<div style="margin-top:8px">${urls.map(u=>
      `<a class="srclink" href="${u}" target="_blank" rel="noopener">${u}</a>`).join('')}</div>`:'');
}

// ── cell inspection ────────────────────────────────────────────────────────
let current=null;
const STATE={'01':'AL','04':'AZ','05':'AR','06':'CA','08':'CO','09':'CT','10':'DE',
 '11':'DC','12':'FL','13':'GA','16':'ID','17':'IL','18':'IN','19':'IA','20':'KS',
 '21':'KY','22':'LA','23':'ME','24':'MD','25':'MA','26':'MI','27':'MN','28':'MS',
 '29':'MO','30':'MT','31':'NE','32':'NV','33':'NH','34':'NJ','35':'NM','36':'NY',
 '37':'NC','38':'ND','39':'OH','40':'OK','41':'OR','42':'PA','44':'RI','45':'SC',
 '46':'SD','47':'TN','48':'TX','49':'UT','50':'VT','51':'VA','53':'WA','54':'WV',
 '55':'WI','56':'WY'};

map.on('click',CELL_LAYERS,e=>{
  if(e.originalEvent && e.originalEvent.shiftKey) return;   // region select
  const f=e.features[0], p=f.properties;
  current={props:p,geometry:f.geometry,lngLat:e.lngLat};
  const s=scoreOf(p);
  document.getElementById('cell-score').textContent=s==null?'–':Math.round(s);
  const counties=(p.cnames||'').split('|').filter(Boolean);
  const st=STATE[p.st]||p.st;
  document.getElementById('cell-geo').innerHTML=
    `<div class="kv"><span>H3 index</span><span>${p.h3??'—'}</span></div>
     <div class="kv"><span>Resolution</span><span>r${cfg.resolution} · ~5.16 km²</span></div>
     <div class="kv"><span>State</span><span>${st??'—'}</span></div>
     <div class="kv"><span>${counties.length>1?'Counties':'County'}</span><span>${counties.join(', ')||'—'}</span></div>
     <div class="kv"><span>Centre</span><span>${e.lngLat.lat.toFixed(4)}, ${e.lngLat.lng.toFixed(4)}</span></div>
     <div class="kv"><span>Factors used</span><span>${p.nf}</span></div>`;
  const notes=[];
  for(const fl of cfg.flags) if(p['fl_'+fl.id]) notes.push(`<span class="flag">⚑ ${fl.label}</span>`);
  for(const [k,v] of Object.entries(cfg.exclusions||{}))
    if(p['x_'+k]) notes.push(`<span class="exc">✖ ${v.reason} (score ×${v.multiplier})</span>`);
  document.getElementById('cell-flags').innerHTML=notes.join('');
  const rows=live.filter(f=>p[f]!==undefined&&(weights[f]??0)>0)
    .map(f=>[cfg.factors[f].label,p[f],weights[f],cfg.factors[f].source]);
  document.getElementById('cell-factors').innerHTML=rows.sort((a,b)=>b[2]-a[2])
    .map(([l,v,,src])=>`<div class="brow" title="Source: ${src}"><span class="bn">${l}</span>
      <span class="bb"><i style="width:${v}%"></i></span><span class="bv">${v}</span></div>`).join('')
    ||'<p class="desc">No factors carry data here.</p>';
  document.getElementById('h3note').textContent =
    counties.length>1?'This hexagon straddles a county boundary; all counties it touches are listed.':'';
  document.getElementById('news-out').innerHTML='';
  document.getElementById('cell-empty').hidden=true;
  document.getElementById('cell-body').hidden=false;
  showTab('cell');
  map.setFilter('hi',['==',['get','h3'],p.h3??'']);
});
map.on('mouseenter',CELL_LAYERS,()=>map.getCanvas().style.cursor='pointer');
map.on('mouseleave',CELL_LAYERS,()=>map.getCanvas().style.cursor='');

document.getElementById('copy-h3').onclick=async()=>{
  if(!current) return;
  try{ await navigator.clipboard.writeText(current.props.h3??''); }catch(_){}
  const b=document.getElementById('copy-h3');
  b.textContent='Copied'; setTimeout(()=>b.textContent='Copy H3 index',1200);
};
function download(name,text,mime){
  const u=URL.createObjectURL(new Blob([text],{type:mime}));
  const a=document.createElement('a'); a.href=u; a.download=name; a.click();
  URL.revokeObjectURL(u);
}
document.getElementById('dl-geojson').onclick=()=>{
  if(!current) return;
  download(`h3_${current.props.h3||'cell'}.geojson`, JSON.stringify(
    {type:'Feature',geometry:current.geometry,
     properties:{...current.props,h3_resolution:cfg.resolution,
       generated:new Date().toISOString()}},null,1),'application/geo+json');
};

function placeName(p){
  const c=(p.cnames||'').split('|').filter(Boolean).join(', ');
  return `${c||'unknown county'}, ${STATE[p.st]||p.st}`;
}
document.getElementById('news-cell').onclick=async()=>{
  if(!current) return;
  const host=document.getElementById('news-out');
  host.innerHTML='<p class="spin">searching…</p>';
  try{
    const where=placeName(current.props);
    const out=await askClaude(
`Search for recent news and policy developments about data center development in ${where} (United States).

Cover, where sources exist: proposed or operating data centers; local moratoria, zoning fights or referendums; utility interconnection and ratepayer decisions; water use restrictions; tax abatements or incentives; organised community opposition.

Write at most 180 words, in plain prose, and state clearly if you find little or nothing. Do not speculate beyond what the sources say. Do not invent URLs.`,
      {search:true});
    renderAI(host,out);
  }catch(err){ host.innerHTML=`<p class="warn">${err.message}</p>`; }
};

// ── site upload & ranking ──────────────────────────────────────────────────
let sites=[];

function parseCSV(text){
  const lines=text.trim().split(/\r?\n/);
  const head=lines[0].split(',').map(s=>s.trim().toLowerCase().replace(/^"|"$/g,''));
  const iLat=head.findIndex(h=>/^(lat|latitude|y)$/.test(h));
  const iLng=head.findIndex(h=>/^(lon|lng|long|longitude|x)$/.test(h));
  if(iLat<0||iLng<0) throw new Error('CSV needs lat/latitude and lon/lng/longitude columns');
  const iName=head.findIndex(h=>/^(name|site|id|label)$/.test(h));
  const out=[];
  for(let i=1;i<lines.length;i++){
    const c=lines[i].split(',').map(s=>s.trim().replace(/^"|"$/g,''));
    const lat=+c[iLat], lng=+c[iLng];
    if(!isFinite(lat)||!isFinite(lng)) continue;
    out.push({name:iName>=0?c[iName]:`Site ${i}`,lat,lng});
  }
  return out;
}
// Parcel-aware. Polygons are treated as parcels: real acreage, every H3 cell
// the parcel covers, and its attributes (owner, APN, land use, value) carried
// through to ranking and export. Points behave as before.
const OWNER=['owner','OWNER','ownername','OWNNAME','owner_name','OWNER1','mail_name'];
const APN=['parcelnumb','APN','apn','PARCELID','PARCEL_ID','PIN','pin','parcel_id','PARCELNO'];
const ACRE=['ll_gisacre','gisacre','GISACRE','ACRES','acres','deeded_acres','ACREAGE','CALC_ACRE'];
const USE=['usedesc','LANDUSE','landuse','USE_DESC','zoning','ZONING','lbcs_activity_desc','PROP_CLASS'];
const VAL=['parval','TOTVAL','total_value','TOTAL_VAL','landval','LANDVAL','APPRAISED','ASSESSED'];
const pick=(p,keys)=>{ for(const k of keys) if(p[k]!==undefined&&p[k]!==null&&p[k]!=='') return p[k]; return null; };

function ringArea(ring){                       // spherical excess, m²
  const R=6378137, rad=Math.PI/180; let a=0;
  for(let i=0;i<ring.length-1;i++){
    const [x1,y1]=ring[i],[x2,y2]=ring[i+1];
    a+=(x2-x1)*rad*(2+Math.sin(y1*rad)+Math.sin(y2*rad));
  }
  return Math.abs(a*R*R/2);
}
function geomAcres(g){
  const polys=g.type==='Polygon'?[g.coordinates]:g.type==='MultiPolygon'?g.coordinates:[];
  let m2=0;
  for(const poly of polys){ m2+=ringArea(poly[0]); for(const h of poly.slice(1)) m2-=ringArea(h); }
  return m2/4046.8564224;
}
function geomCentroid(g){
  const cs=JSON.stringify(g.coordinates).match(/-?\d+\.?\d*(?:e-?\d+)?/g).map(Number);
  const xs=[],ys=[]; for(let k=0;k<cs.length-1;k+=2){ xs.push(cs[k]); ys.push(cs[k+1]); }
  return [xs.reduce((a,b)=>a+b,0)/xs.length, ys.reduce((a,b)=>a+b,0)/ys.length];
}
function fromGeoJSON(gj){
  const out=[];
  const feats=gj.type==='FeatureCollection'?gj.features:[gj];
  for(const [i,f] of feats.entries()){
    const g=f.geometry; if(!g) continue;
    const p=f.properties||{};
    let lat,lng,acres=null,poly=null;
    if(g.type==='Point'){ [lng,lat]=g.coordinates; }
    else if(g.type==='Polygon'||g.type==='MultiPolygon'){
      [lng,lat]=geomCentroid(g); poly=g;
      const stated=+pick(p,ACRE);
      acres=isFinite(stated)&&stated>0?stated:geomAcres(g);
    } else continue;
    if(!isFinite(lat)||!isFinite(lng)) continue;
    out.push({name:pick(p,APN)||p.name||p.Name||p.NAME||p.site||p.id||`Site ${i+1}`,
      lat,lng,acres,poly,owner:pick(p,OWNER),use:pick(p,USE),value:pick(p,VAL),attrs:p});
  }
  return out;
}

async function settle(){
  await new Promise(res=>{
    let done=false;
    const t=setTimeout(()=>{if(!done){done=true;map.off('idle',h);res();}},4000);
    const h=()=>{if(!done){done=true;clearTimeout(t);map.off('idle',h);res();}};
    map.on('idle',h);
  });
}
// queryRenderedFeatures only sees loaded tiles, so each site needs its tile
// on screen; r7 starts at zoom 9.
async function lookupCell(lat,lng){
  map.jumpTo({center:[lng,lat],zoom:9.6}); await settle();
  const f=map.queryRenderedFeatures(map.project([lng,lat]),{layers:['cells_r7']});
  return f[0]?f[0].properties:null;
}
// A parcel larger than one hex (~1,275 acres) is scored as the AREA-WEIGHTED
// mean of every cell whose centre falls inside it, not its centroid cell.
async function lookupParcel(site){
  map.jumpTo({center:[site.lng,site.lat],zoom:9.6}); await settle();
  let cells=[];
  try{ cells=h3.polygonToCells(site.poly.type==='Polygon'?site.poly.coordinates:
         site.poly.coordinates[0],7,true); }catch(_){}
  if(cells.length<=1){ const p=await lookupCell(site.lat,site.lng); return {props:p,n:1}; }
  const found=[];
  for(const c of cells.slice(0,40)){
    const [la,lo]=h3.cellToLatLng(c);
    const f=map.queryRenderedFeatures(map.project([lo,la]),{layers:['cells_r7']});
    if(f[0]) found.push(f[0].properties);
  }
  if(!found.length) return {props:await lookupCell(site.lat,site.lng),n:1};
  const agg={...found[0]};
  for(const k of [...live,'xm']){
    const v=found.map(p=>p[k]).filter(x=>x!==undefined);
    if(v.length) agg[k]=v.reduce((a,b)=>a+b,0)/v.length;
  }
  return {props:agg,n:found.length};
}

document.getElementById('site-file').onchange=async ev=>{
  const file=ev.target.files[0]; if(!file) return;
  const st=document.getElementById('site-status');
  const view={center:map.getCenter(),zoom:map.getZoom()};
  st.textContent='reading…';
  try{
    let raw=[];
    if(/\.zip$/i.test(file.name)){
      const gj=await shp(await file.arrayBuffer());
      raw=fromGeoJSON(Array.isArray(gj)?{type:'FeatureCollection',
        features:gj.flatMap(g=>g.features)}:gj);
    } else if(/\.csv|\.txt$/i.test(file.name)){
      raw=parseCSV(await file.text());
    } else {
      raw=fromGeoJSON(JSON.parse(await file.text()));
    }
    const read=raw.length;
    raw=raw.filter(s=>s.lat>=24&&s.lat<=50&&s.lng>=-125&&s.lng<=-66);
    const minAc=+document.getElementById('min-acres').value||0;
    const parcels=raw.some(s=>s.poly);
    if(parcels&&minAc>0) raw=raw.filter(s=>(s.acres??0)>=minAc);
    if(!raw.length) throw new Error(parcels&&minAc>0
      ?`no parcels of ${minAc}+ acres inside CONUS`:'no usable features inside CONUS');
    // Largest first, so a cap keeps the parcels most likely to matter.
    if(parcels) raw.sort((a,b)=>(b.acres??0)-(a.acres??0));
    const CAP=parcels?150:60, eligible=raw.length, capped=eligible>CAP;
    raw=raw.slice(0,CAP);
    sites=[];
    for(const [i,s] of raw.entries()){
      st.textContent=`scoring ${i+1}/${raw.length}…`;
      if(s.poly){ const r=await lookupParcel(s); sites.push({...s,props:r.props,nCells:r.n}); }
      else sites.push({...s,props:await lookupCell(s.lat,s.lng),nCells:1});
    }
    map.jumpTo(view);
    map.getSource('sites').setData({type:'FeatureCollection',
      features:sites.map(s=>({type:'Feature',properties:{name:s.name},
        geometry:s.poly||{type:'Point',coordinates:[s.lng,s.lat]}}))});
    st.textContent=`${sites.length} ${parcels?'parcel':'site'}${sites.length>1?'s':''} scored`
      +(parcels&&minAc>0?` (${minAc}+ acres)`:'')
      +(capped?` — largest ${CAP} of ${eligible} eligible`:'')
      +(read>eligible&&!(parcels&&minAc>0)?` · ${read-eligible} outside CONUS dropped`:'');
    rerankSites();
  }catch(err){ st.innerHTML=`<span class="warn">${err.message}</span>`; }
};

function rankedSites(){
  return sites.map(s=>({...s,score:s.props?scoreOf(s.props):null}))
    .sort((a,b)=>(b.score??-1)-(a.score??-1));
}
const fmtAc=a=>a==null?'':(a>=100?Math.round(a):a.toFixed(1))+' ac';
function rerankSites(){
  if(!sites.length) return;
  const r=rankedSites();
  document.getElementById('site-list').innerHTML=r.map((s,i)=>
    `<div class="srow" data-lat="${s.lat}" data-lng="${s.lng}" title="${[
       s.owner&&('Owner: '+s.owner), s.use&&('Use: '+s.use), s.value&&('Value: '+s.value),
       s.nCells>1&&(`Averaged over ${s.nCells} cells`)].filter(Boolean).join('\n')}">
       <span class="rank">${i+1}</span>
       <span class="nm">${s.name}${s.acres!=null?` <span style="color:var(--muted)">· ${fmtAc(s.acres)}</span>`:''}
         ${s.owner?`<br><span style="color:var(--muted);font-size:10px">${String(s.owner).slice(0,40)}</span>`:''}</span>
       <span class="sc">${s.score==null?'—':Math.round(s.score)}</span></div>`).join('');
  document.querySelectorAll('#site-list .srow').forEach(el=>{el.onclick=()=>{
    map.jumpTo({center:[+el.dataset.lng,+el.dataset.lat],zoom:11});};});
}
const csv=v=>{ const t=v==null?'':String(v); return /[",\n]/.test(t)?`"${t.replace(/"/g,'""')}"`:t; };
document.getElementById('dl-sites').onclick=()=>{
  if(!sites.length) return;
  const r=rankedSites();
  // Pass through every attribute the upload carried, after the computed ones.
  const extra=[...new Set(r.flatMap(s=>Object.keys(s.attrs||{})))].slice(0,40);
  const cols=['rank','name','score','acres','cells_averaged','owner','land_use','value',
              'lat','lng','h3','county','state',...live,...extra.map(k=>'src_'+k)];
  const rows=r.map((s,i)=>[i+1,s.name,s.score==null?'':Math.round(s.score),
    s.acres==null?'':s.acres.toFixed(2),s.nCells,s.owner,s.use,s.value,
    s.lat.toFixed(6),s.lng.toFixed(6),s.props?.h3??'',
    (s.props?.cnames||'').split('|').join('; '),STATE[s.props?.st]||'',
    ...live.map(f=>s.props?.[f]==null?'':Math.round(s.props[f])),
    ...extra.map(k=>s.attrs?.[k])].map(csv).join(','));
  download('ranked_sites.csv',[cols.join(','),...rows].join('\n'),'text/csv');
};
document.getElementById('rank-ai').onclick=async()=>{
  const host=document.getElementById('site-ai');
  if(!sites.length){ host.innerHTML='<p class="desc">Upload sites first.</p>'; return; }
  host.innerHTML='<p class="spin">thinking…</p>';
  const r=rankedSites().slice(0,25);
  const table=r.map((s,i)=>{
    const p=s.props||{};
    const fac=live.filter(f=>p[f]!==undefined)
      .map(f=>`${cfg.factors[f].label}=${Math.round(p[f])}`).join('; ');
    const meta=[s.acres!=null&&`${fmtAc(s.acres)}`, s.use&&`use: ${s.use}`,
                s.owner&&`owner: ${s.owner}`].filter(Boolean).join(', ');
    return `${i+1}. ${s.name} (${(p.cnames||'').split('|').join(', ')}, ${STATE[p.st]||'?'})`
         + `${meta?' ['+meta+']':''} composite=${s.score==null?'n/a':Math.round(s.score)} :: ${fac}`;
  }).join('\n');
  try{
    const out=await askClaude(
`You are advising on data center site selection. Below are candidate sites already
scored 0-100 on each factor by an open-data model (higher is better on every factor).
Where acreage, land use or owner is given, it came from the user's parcel file.

${table}

Recommend the best site and say why, in at most 220 words. Name the specific
factors that decide it and the main risk of your pick. If acreage is given, say
whether it is plausible for a campus (large hyperscale sites typically need
hundreds of acres). If two are close, say so. Judge only from these numbers
and attributes; do not invent facts about the locations.`,
      {maxTokens:900});
    renderAI(host,out);
  }catch(err){ host.innerHTML=`<p class="warn">${err.message}</p>`; }
};

// ── region select & administrative aggregation ─────────────────────────────
let selCells=[], selAgg=[], counties=null;
async function loadCounties(){
  if(!counties) counties=await fetch('./counties.geojson').then(r=>r.json());
  return counties;
}

// Shift-drag box select. boxZoom is disabled so this gesture is ours.
let selectMode=false;
(function boxSelect(){
  const cv=map.getCanvasContainer(); let start=null, box=null;
  cv.addEventListener('mousedown',e=>{
    // Shift is the power-user gesture; the toolbar toggle is the discoverable
    // one. Requiring a modifier key nobody is told about hides the feature.
    if(!e.shiftKey && !selectMode) return;
    e.preventDefault(); map.dragPan.disable();
    start=[e.clientX,e.clientY];
    box=document.createElement('div'); box.className='selbox';
    document.body.appendChild(box);
  },true);
  addEventListener('mousemove',e=>{
    if(!start) return;
    const x=Math.min(e.clientX,start[0]), y=Math.min(e.clientY,start[1]);
    Object.assign(box.style,{left:x+'px',top:y+'px',
      width:Math.abs(e.clientX-start[0])+'px',height:Math.abs(e.clientY-start[1])+'px'});
  });
  addEventListener('mouseup',e=>{
    if(!start) return;
    const r=map.getCanvas().getBoundingClientRect();
    const p1=[start[0]-r.left,start[1]-r.top], p2=[e.clientX-r.left,e.clientY-r.top];
    box.remove(); box=null; start=null; map.dragPan.enable();
    if(selectMode) setSelectMode(false);
    if(Math.abs(p2[0]-p1[0])<4&&Math.abs(p2[1]-p1[1])<4) return;
    const layer=map.getZoom()>=9?'cells_r7':(map.getZoom()>=6?'cells_r6':'cells_r4');
    const feats=map.queryRenderedFeatures([p1,p2],{layers:[layer]});
    const seen=new Set();
    selCells=[];
    for(const f of feats){
      const k=f.properties.h3||JSON.stringify(f.geometry.coordinates[0][0]);
      if(seen.has(k)) continue; seen.add(k);
      selCells.push({props:f.properties,geometry:f.geometry});
    }
    showTab('region'); aggregate();
  });
})();
function setSelectMode(on){
  selectMode=on;
  const b=document.getElementById('sel-mode');
  b.textContent=on?'Click-drag on map…':'Select area';
  b.style.borderColor=on?'var(--accent)':'';
  map.getCanvas().style.cursor=on?'crosshair':'';
}
document.getElementById('sel-mode').onclick=()=>setSelectMode(!selectMode);
document.getElementById('clear-sel').onclick=()=>{
  if(selectMode) setSelectMode(false);
  selCells=[]; selAgg=[];
  map.getSource('sel').setData({type:'FeatureCollection',features:[]});
  document.getElementById('region-status').textContent='';
  document.getElementById('region-list').innerHTML='';
  document.getElementById('news-region-out').innerHTML='';
};
document.getElementById('agg-level').onchange=aggregate;

function meanBy(items,key){
  const s=items.reduce((a,b)=>a+(b??0),0); return items.length?s/items.length:null;
}
async function aggregate(){
  const st=document.getElementById('region-status');
  if(!selCells.length){ st.textContent='Nothing selected.'; return; }
  const level=document.getElementById('agg-level').value;
  const scored=selCells.map(c=>({...c,score:scoreOf(c.props)}));
  selAgg=[];

  if(level==='cells'){
    selAgg=scored.map(c=>({name:c.props.h3,geometry:c.geometry,
      score:c.score,n:1,props:c.props}));
  } else if(level.startsWith('h3_')){
    const res=+level.split('_')[1];
    const groups={};
    for(const c of scored){
      if(!c.props.h3) continue;
      const parent=h3.cellToParent(c.props.h3,res);
      (groups[parent] ||= []).push(c);
    }
    selAgg=Object.entries(groups).map(([p,items])=>({
      name:p, n:items.length, score:meanBy(items.map(i=>i.score)),
      geometry:{type:'Polygon',coordinates:[
        h3.cellToBoundary(p,true).concat([h3.cellToBoundary(p,true)[0]])]}}));
  } else {
    const gj=await loadCounties();
    const groups={};
    for(const c of scored){
      const keys = level==='county'
        ? (c.props.cnames||'').split('|').filter(Boolean).map(n=>`${n}|${c.props.st}`)
        : [c.props.st];
      for(const k of (keys.length?keys:['?'])) (groups[k] ||= []).push(c);
    }
    for(const [k,items] of Object.entries(groups)){
      let geom=null, label=k;
      if(level==='county'){
        const [nm,stf]=k.split('|');
        const f=gj.features.find(x=>x.properties.name===nm&&x.properties.fips.startsWith(stf));
        geom=f?f.geometry:null; label=`${nm} County, ${STATE[stf]||stf}`;
      } else {
        label=STATE[k]||k;
      }
      selAgg.push({name:label,n:items.length,score:meanBy(items.map(i=>i.score)),geometry:geom});
    }
  }

  selAgg.sort((a,b)=>(b.score??-1)-(a.score??-1));
  const feats=selAgg.filter(a=>a.geometry).map(a=>({type:'Feature',
    properties:{name:a.name,score:a.score==null?null:Math.round(a.score),cells:a.n},
    geometry:a.geometry}));
  map.getSource('sel').setData({type:'FeatureCollection',features:feats});
  const noun={county:'counties',state:'states',cells:'cells'}[level]
    || 'H3 parents';
  st.textContent=`${selCells.length} cells → ${selAgg.length} ${noun}`;
  document.getElementById('region-list').innerHTML=selAgg.slice(0,40).map((a,i)=>
    `<div class="srow"><span class="rank">${i+1}</span><span class="nm">${a.name}</span>
     <span class="sc">${a.score==null?'—':Math.round(a.score)}</span></div>`).join('');
}
document.getElementById('dl-region').onclick=()=>{
  if(!selAgg.length) return;
  download('region.geojson',JSON.stringify({type:'FeatureCollection',
    features:selAgg.filter(a=>a.geometry).map(a=>({type:'Feature',
      properties:{name:a.name,mean_score:a.score==null?null:+a.score.toFixed(1),
        cells:a.n,aggregation:document.getElementById('agg-level').value,
        generated:new Date().toISOString()},geometry:a.geometry}))},null,1),
    'application/geo+json');
};
document.getElementById('dl-region-csv').onclick=()=>{
  if(!selAgg.length) return;
  download('region.csv',['name,mean_score,cells',
    ...selAgg.map(a=>`"${String(a.name).replace(/"/g,'""')}",${
      a.score==null?'':a.score.toFixed(1)},${a.n}`)].join('\n'),'text/csv');
};
document.getElementById('news-region').onclick=async()=>{
  const host=document.getElementById('news-region-out');
  if(!selAgg.length){ host.innerHTML='<p class="desc">Select a region first.</p>'; return; }
  host.innerHTML='<p class="spin">searching…</p>';
  const places=selAgg.slice(0,6).map(a=>a.name).join('; ');
  try{
    const out=await askClaude(
`Search for recent news and policy developments about data center development in these US areas: ${places}.

Cover, where sources exist: proposed or operating data centers; local moratoria, zoning fights or referendums; utility interconnection and ratepayer decisions; water use restrictions; tax abatements; organised community opposition.

At most 220 words of plain prose. Say clearly where you find little or nothing. Do not speculate beyond the sources and do not invent URLs.`,
      {search:true});
    renderAI(host,out);
  }catch(err){ host.innerHTML=`<p class="warn">${err.message}</p>`; }
};

// ── about ──────────────────────────────────────────────────────────────────
function showAbout(){
  const miss=Object.keys(cfg.factors).filter(f=>!live.includes(f));
  const c=cfg.calibration||{};
  document.getElementById('about-card').innerHTML=`
    <h2>Siting Command</h2>
    <p style="color:var(--muted)">An open-data screening map for where large
       data centers can plausibly go in the continental US.</p>

    <h3>What it is</h3>
    <p>Every ~5 km² hexagon in the lower 48 (1,467,441 of them) is scored 0–100
       on ${Object.keys(cfg.factors).length} factors — grid access, interconnection headroom, power
       cost, cooling climate, water, terrain, land cover, policy, hazard and
       more — built entirely from public federal and NGO data.</p>

    <h3>Who it's for</h3>
    <p>Anyone doing early site screening or diligence: developers narrowing a
       search, utilities and planners anticipating load, researchers and
       journalists tracking the buildout, communities checking what the numbers
       say about their own county.</p>

    <h3>How to use it</h3>
    <ul>
      <li><b>Score</b> — pick a weighting profile and see every data source.</li>
      <li><b>Weights</b> — drag any factor to build your own methodology. Click
          <b>ⓘ</b> for the source, coverage and caveats behind it.</li>
      <li><b>Cell</b> — click a hexagon for its H3 index, counties, full
          breakdown, GeoJSON export and a policy-news search.</li>
      <li><b>Sites</b> — upload a shapefile, GeoJSON or CSV to rank candidate
          sites against each other.</li>
      <li><b>Region</b> — hold <b>Shift</b> and drag to select an area, then roll
          it up to counties, states or coarser H3 cells and export.</li>
    </ul>

    <h3>What it is not</h3>
    <p>A screening tool, not a site survey. It says where to look, never where
       to build. Scores are relative, not absolute: a 70 means "better than most
       of the country on these weights", not "viable".</p>
    <p class="cav">Known limits: cooling uses degree-days, not ASHRAE design
       wet-bulb (so humidity is invisible); interconnection uses planned
       generation, which understates queue contention; power price joins at
       state level; broadband measures adoption, not availability; slope is
       sampled at ~1.4 km so it screens out mountains but says nothing about a
       specific parcel. ${miss.length?`Not yet live: ${miss.join(', ')}.`:''}</p>
    ${c.n_positive?`<p style="font-size:11px;color:var(--muted)">Weights are fitted
       against ${c.n_positive} operating campuses and ${c.n_negative} known-bad
       sites (mean ${c.positives_mean} vs ${c.negatives_mean}).</p>`:''}
    <p style="font-size:11px;color:var(--muted)">AI features are bring-your-own-key
       and call Anthropic directly from your browser. Everything else works
       without a key.</p>
    <button id="about-close">Start exploring</button>`;
  document.getElementById('about').hidden=false;
  document.getElementById('about-close').onclick=()=>{
    document.getElementById('about').hidden=true; store.set(LS_SEEN,'1');
  };
}
document.getElementById('open-about').onclick=showAbout;
document.getElementById('about').onclick=e=>{ if(e.target.id==='about') e.currentTarget.hidden=true; };

// ── boot ───────────────────────────────────────────────────────────────────
map.on('style.load',()=>{
  fit();
  sel.value='default';
  document.getElementById('profile-desc').textContent=cfg.profiles.default.description||'';
  renderFactors(); renderSources(); apply();
  const c=cfg.calibration||{};
  if(c.n_positive) document.getElementById('calib').textContent=
    `Weights fitted against ${c.n_positive} operating campuses and ${c.n_negative} known-bad sites (mean ${c.positives_mean} vs ${c.negatives_mean}).`;
  document.getElementById('excl').innerHTML=Object.entries(cfg.exclusions||{})
    .map(([,v])=>`<span class="exc">✖ ${v.reason}</span>`).join('');
  const missing=Object.keys(cfg.factors).filter(f=>!live.includes(f));
  document.getElementById('status').textContent=
    `${live.length}/${Object.keys(cfg.factors).length} factors live`;
  if(missing.length) document.getElementById('provisional').textContent=
    `No data yet: ${missing.join(', ')}`;
  if(!store.get(LS_SEEN)) showAbout();
});
