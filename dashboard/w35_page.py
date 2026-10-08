"""
The /data-platform page (W35): data lake (DP-22), ticks (DP-04), order-book depth (DP-05), F&O contracts and
option chains (DP-08), macro series and calendar (DP-14), multi-asset prices and MF NAVs (DP-21), alternative
data sources (AD-01/02). Read from /api/data/*.

W39b: a scheme picked from the MF NAV search opens a detail card on the Multi-asset tab -- returns, rolling
returns (chart + min / median / max / % above 0 and the hurdle), risk and benchmark figures, the AMFI-category
rank with its coverage, and a SIP / lump-sum calculator (/api/data/mf/{scheme}/analytics and /sip).

Charts are single-series lines (one hue, #3987e5 -- validated for this dark surface), 2px, crosshair tooltip;
reference levels (0, the hurdle) are dashed muted lines. Every charted value is also listed in a table, except
the rolling-return series (one point per NAV date), which is summarised by its statistics table.
"""

import json

PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ATIP Data Platform</title>
<style>
:root{--bg:#0f172a;--panel:#1e293b;--line:#334155;--text:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8;--series:#3987e5;--grid:#334155;--warn:#f59e0b}
*{box-sizing:border-box;margin:0;padding:0}body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:13px}
.top{background:var(--panel);padding:10px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px}
.top a{color:var(--accent);text-decoration:none;margin-left:12px}.wrap{padding:14px 16px;max-width:1400px}
.tabs{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:10px}.tab{padding:6px 12px;border-radius:6px;background:var(--panel);cursor:pointer;color:var(--muted)}
.tab.on{background:#2563eb;color:#fff}.pane{display:none}.pane.on{display:block}
h2{font-size:13px;color:var(--accent);margin:14px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
table{width:100%;border-collapse:collapse;background:var(--panel);font-size:11.5px}
th,td{padding:5px 7px;border-bottom:1px solid #1e293b55;text-align:left;vertical-align:top}th{color:var(--muted);background:var(--bg);white-space:nowrap}
.muted{color:var(--muted)}.note{color:var(--warn)}.scroll{max-height:420px;overflow:auto}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin-bottom:10px}
.row{display:flex;gap:12px;flex-wrap:wrap}.row>.card{flex:1;min-width:300px}
button,select,input{background:#2563eb;color:#fff;border:none;border-radius:6px;padding:4px 9px;cursor:pointer;font-size:11.5px;margin:1px}
select,input{background:var(--bg);color:var(--text);border:1px solid var(--line)}label{color:var(--muted);margin-right:8px}
.chart{position:relative;width:100%;height:180px}.chart svg{width:100%;height:100%;display:block}
.tip{position:absolute;pointer-events:none;background:#0b1220;border:1px solid var(--line);border-radius:6px;padding:4px 7px;font-size:11px;display:none;white-space:nowrap}.tip b{display:block}
</style></head><body>
<div class="top"><div><b style="color:var(--accent)">📊 ATIP</b> <span class="muted">Data platform (W35)</span></div>
<div><a href="/market">Market</a><a href="/quant">Quant</a><a href="/">← Dashboard</a></div></div>
<div class="wrap"><div class="tabs" id="tabs"></div>

<div class="pane" id="p-lake"><div class="card"><div id="lake"></div><button onclick="verify()">Verify hashes</button>
 <label>archive table <input id="atab" size="14" placeholder="prices_daily"></label><label>date col <input id="acol" size="8" value="date"></label><label>start <input id="ast" size="10"></label><button onclick="archive()">Archive to lake</button></div></div>

<div class="pane" id="p-ticks"><div class="row"><div class="card"><b>Tick capture</b> <span class="muted">live_feed.capture_ticks</span><div id="ticks"></div><button onclick="minuteBars()">Build today's 1-min bars</button></div>
<div class="card" style="flex:2"><b>Order-book depth — latest per symbol</b> <span class="muted">depth.enabled</span> <button onclick="depthNow()">Snapshot now</button><div id="depth" class="scroll"></div></div></div></div>

<div class="pane" id="p-fo"><div class="card"><label>underlying <input id="fsym" size="12" value="NIFTY"></label><label>expiry <select id="fexp"><option value="">all</option></select></label>
<button onclick="chain()">Latest option chain</button> <button onclick="chainNow()">Snapshot now</button> <button onclick="contracts()">EOD contracts</button><div id="fo" class="scroll" style="margin-top:8px"></div></div></div>

<div class="pane" id="p-macro"><div class="row"><div class="card" style="flex:2"><b>Macro series (point in time)</b> <button onclick="macroRefresh()">Refresh now</button><div id="macro" class="scroll"></div></div>
<div class="card"><b id="mt">Series — pick one</b><div class="chart" id="mchart"></div><b>Calendar</b><div id="cal"></div>
<label>date <input id="cd" size="10"></label><label>event <input id="ce" size="18"></label><button onclick="addCal()">Add</button></div></div></div>

<div class="pane" id="p-assets"><div class="row"><div class="card"><b>Assets</b> <button onclick="assetsRefresh()">Refresh now</button><div id="assets" class="scroll"></div></div>
<div class="card" style="flex:2"><b id="at">Price — pick an asset</b><div class="chart" id="achart"></div>
<b>Mutual fund NAVs</b> <input id="mfq" size="20" placeholder="search scheme"><button onclick="mfSearch()">Search</button> <span class="muted">pick a scheme for its analytics</span><div id="mf" class="scroll" style="max-height:240px"></div></div></div>
<div class="card" id="mfd" style="display:none"><div id="mfh"></div>
<div class="row" style="margin-top:4px"><div style="flex:1;min-width:280px"><h2>Returns</h2><div id="mfret"></div><h2>Risk</h2><div id="mfrisk"></div></div>
<div style="flex:1;min-width:280px"><h2 id="mfrt">Rolling returns</h2><label>window <select id="mfwin" onchange="mfRolling()"><option>1Y</option><option>3Y</option></select></label><label>hurdle % a year <input id="mfhurdle" size="4" value="7" onchange="if(MF)mfOpen(MF.scheme.scheme_code)"></label>
<div class="chart" id="mfrchart"></div><div id="mfrs"></div></div></div>
<h2>Category rank</h2><div id="mfcat"></div>
<h2>SIP calculator <span class="muted">(on the stored NAVs; a holiday buys at the next NAV)</span></h2>
<label>₹ a month <input id="sipa" size="7" value="5000"></label><label>day <input id="sipd" size="2" value="5"></label><label>start <input id="sips" size="10" placeholder="3Y back"></label><label>end <input id="sipe" size="10" placeholder="last NAV"></label><label>lump sum ₹ <input id="sipl" size="9" placeholder="same total"></label><button onclick="mfSip()">Calculate</button>
<div id="sipout" style="margin-top:6px"></div><p class="muted" id="mfnote" style="margin-top:8px"></p></div></div>

<div class="pane" id="p-alt"><div class="card"><div id="alt"></div></div><div class="card"><b id="altt">Observations — pick a source metric</b><div id="altobs" class="scroll"></div></div></div>
</div>
<script>
const TOKEN=__TOKEN__;
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const num=(v,d=2)=>v==null?'—':Number(v).toFixed(d);
async function j(u,o){const r=await fetch(u,o);const t=await r.json();if(!r.ok)throw new Error(t.error||t.detail||r.status);return t}
const post=(u,b)=>j(u,{method:'POST',headers:{'Content-Type':'application/json','X-ATIP-Token':TOKEN},body:JSON.stringify(b||{})});
const table=(h,rows)=>`<table><thead><tr>${h.map(x=>`<th>${x}</th>`).join('')}</tr></thead><tbody>${rows.join('')||`<tr><td colspan=${h.length} class="muted">none</td></tr>`}</tbody></table>`;
function lineChart(el,pts,{fmt=v=>num(v),label='value',refs=[]}={}){
  el.innerHTML='';const tip=document.createElement('div');tip.className='tip';if(!pts.length){el.textContent='no data';return}
  const W=el.clientWidth||600,H=el.clientHeight||180,L=52,R=10,T=10,B=22,ys=pts.map(p=>p.y).concat(refs.map(r=>r.y)),lo=Math.min(...ys),hi=Math.max(...ys),pad=(hi-lo)*0.08||1,y0=lo-pad,y1=hi+pad;
  const X=i=>L+(pts.length===1?0:(W-L-R)*i/(pts.length-1)),Y=v=>T+(H-T-B)*(1-(v-y0)/(y1-y0));
  const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox',`0 0 ${W} ${H}`);
  const add=(t,a)=>{const e=document.createElementNS(ns,t);for(const k in a)e.setAttribute(k,a[k]);svg.appendChild(e);return e};
  for(let k=0;k<=3;k++){const v=y0+(y1-y0)*k/3,y=Y(v);add('line',{x1:L,x2:W-R,y1:y,y2:y,stroke:'var(--grid)','stroke-width':1});const t=add('text',{x:L-6,y:y+3,'text-anchor':'end',fill:'var(--muted)','font-size':10});t.textContent=fmt(v)}
  [0,pts.length-1].forEach(i=>{const t=add('text',{x:X(i),y:H-6,'text-anchor':i?'end':'start',fill:'var(--muted)','font-size':10});t.textContent=pts[i].x});
  refs.forEach(r=>{const y=Y(r.y);add('line',{x1:L,x2:W-R,y1:y,y2:y,stroke:'var(--muted)','stroke-width':1,'stroke-dasharray':'4 3'});const t=add('text',{x:W-R-2,y:y-3,'text-anchor':'end',fill:'var(--muted)','font-size':10});t.textContent=r.label});
  add('path',{d:pts.map((p,i)=>`${i?'L':'M'}${X(i).toFixed(1)},${Y(p.y).toFixed(1)}`).join(''),fill:'none',stroke:'var(--series)','stroke-width':2,'stroke-linejoin':'round','stroke-linecap':'round'});
  const last=pts.length-1;add('circle',{cx:X(last),cy:Y(pts[last].y),r:4,fill:'var(--series)',stroke:'var(--panel)','stroke-width':2});
  const hair=add('line',{y1:T,y2:H-B,stroke:'var(--muted)','stroke-width':1,visibility:'hidden'}),dot=add('circle',{r:4,fill:'var(--series)',stroke:'var(--panel)','stroke-width':2,visibility:'hidden'});
  const hit=add('rect',{x:L,y:T,width:W-L-R,height:H-T-B,fill:'transparent'});
  hit.addEventListener('pointermove',ev=>{const r=svg.getBoundingClientRect(),px=(ev.clientX-r.left)*W/r.width,i=Math.max(0,Math.min(last,Math.round((px-L)/((W-L-R)/Math.max(1,last)))));
    hair.setAttribute('x1',X(i));hair.setAttribute('x2',X(i));hair.setAttribute('visibility','visible');dot.setAttribute('cx',X(i));dot.setAttribute('cy',Y(pts[i].y));dot.setAttribute('visibility','visible');
    tip.textContent='';const b=document.createElement('b');b.textContent=fmt(pts[i].y);tip.appendChild(b);tip.appendChild(document.createTextNode(`${label} · ${pts[i].x}`));tip.style.display='block';tip.style.left=Math.min(X(i)*r.width/W+10,r.width-160)+'px';tip.style.top='4px'});
  hit.addEventListener('pointerleave',()=>{hair.setAttribute('visibility','hidden');dot.setAttribute('visibility','hidden');tip.style.display='none'});
  el.appendChild(svg);el.appendChild(tip)}
const TABS=[['lake','Data lake'],['ticks','Ticks & depth'],['fo','Derivatives'],['macro','Macro'],['assets','Multi-asset'],['alt','Alt data']];const loaded={};
function show(id){document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.dataset.id===id));document.querySelectorAll('.pane').forEach(p=>p.classList.toggle('on',p.id==='p-'+id));
  if(!loaded[id]){loaded[id]=1;({lake,ticks:ticksTab,fo:chain,macro,assets,alt})[id]()}try{localStorage.setItem('atip_dp_tab',id)}catch(e){}}
document.getElementById('tabs').innerHTML=TABS.map(([id,l])=>`<div class="tab" data-id="${id}" onclick="show('${id}')">${l}</div>`).join('');

async function lake(){const L=await j('/api/data/lake');document.getElementById('lake').innerHTML=`<div class="muted">root ${esc(L.root)} · new files written as <b>${esc(L.write_format)}</b>${L.write_format!=='parquet'?' (pip install pyarrow duckdb for Parquet + SQL)':''}</div>`+
  table(['Dataset','Partitions','Files','Rows','MB','First','Last','Formats'],L.datasets.map(d=>`<tr><td><b>${esc(d.dataset)}</b></td><td>${d.partitions}</td><td>${d.files}</td><td>${Number(d.rows||0).toLocaleString('en-IN')}</td><td>${num((d.bytes||0)/1048576,1)}</td><td>${esc(d.first)}</td><td>${esc(d.last)}</td><td>${esc(d.formats)}</td></tr>`))}
async function verify(){try{const r=await post('/api/data/lake/verify');alert(`${r.ok} files OK, ${r.problems.length} problems`+(r.problems.length?'\n'+r.problems.slice(0,10).map(p=>p.problem+': '+p.path).join('\n'):''))}catch(e){alert(e.message)}}
async function archive(){try{const r=await post('/api/data/lake/archive',{table:document.getElementById('atab').value.trim(),date_col:document.getElementById('acol').value.trim(),start:document.getElementById('ast').value.trim()||null});alert(`${r.partitions} partitions, ${r.rows} rows`);lake()}catch(e){alert(e.message)}}

async function ticksTab(){const T=await j('/api/data/ticks/status');document.getElementById('ticks').innerHTML=table(['Day','Ticks','Flushes','Symbols','Dropped','First','Last','1-min bars'],T.map(t=>`<tr><td>${t.day}</td><td>${t.ticks}</td><td>${t.flushed}</td><td>${t.symbols}</td><td class="${t.dropped?'note':''}">${t.dropped}</td><td>${esc(String(t.first_tick).slice(11))}</td><td>${esc(String(t.last_tick).slice(11))}</td><td>${t.minute_bars??'—'}</td></tr>`));
  const D=await j('/api/data/depth?limit=100');document.getElementById('depth').innerHTML=table(['Symbol','Time','Bid','Ask','Spread bps','Bid qty (5)','Ask qty (5)','Imbalance'],D.map(d=>`<tr><td><b>${esc(d.symbol)}</b></td><td class="muted">${esc(String(d.ts).slice(11))}</td><td>${num(d.best_bid)}</td><td>${num(d.best_ask)}</td><td>${num(d.spread_bps,1)}</td><td>${Number(d.bid_qty_5||0).toLocaleString('en-IN')}</td><td>${Number(d.ask_qty_5||0).toLocaleString('en-IN')}</td><td>${num(d.imbalance,3)}</td></tr>`))}
async function minuteBars(){try{const r=await post('/api/data/ticks/minute-bars');alert(`${r.status}: ${r.rows} bars`);ticksTab()}catch(e){alert(e.message)}}
async function depthNow(){try{const r=await post('/api/data/depth/snapshot');alert(`${r.status}: ${r.rows} symbols${r.reason?' — '+r.reason:''}`);ticksTab()}catch(e){alert(e.message)}}

async function chain(){const s=document.getElementById('fsym').value.trim(),e=document.getElementById('fexp').value;const C=await j(`/api/data/fo/chain?symbol=${encodeURIComponent(s)}${e?'&expiry='+e:''}`);
  const sel=document.getElementById('fexp');if(C.expiries&&!e)sel.innerHTML='<option value="">all</option>'+C.expiries.map(x=>`<option>${x}</option>`).join('');
  if(!C.ts){document.getElementById('fo').innerHTML='<p class="muted">No option-chain snapshot stored (derivatives.option_chain_enabled, or "Snapshot now").</p>';return}
  const by={};C.rows.forEach(r=>{const k=r.expiry+'|'+r.strike;(by[k]=by[k]||{expiry:r.expiry,strike:r.strike})[r.option_type]=r});
  document.getElementById('fo').innerHTML=`<div class="muted">${esc(C.symbol)} · ${esc(C.ts)} · underlying ${num((C.rows[0]||{}).underlying)}</div>`+table(['Expiry','CE OI','CE ΔOI','CE IV','CE LTP','Strike','PE LTP','PE IV','PE ΔOI','PE OI'],Object.values(by).map(r=>{const c=r.CE||{},p=r.PE||{};
    return `<tr><td class="muted">${r.expiry}</td><td>${Number(c.oi||0).toLocaleString('en-IN')}</td><td>${Number(c.oi_chg||0).toLocaleString('en-IN')}</td><td>${num(c.iv,1)}</td><td>${num(c.ltp)}</td><td><b>${num(r.strike,0)}</b></td><td>${num(p.ltp)}</td><td>${num(p.iv,1)}</td><td>${Number(p.oi_chg||0).toLocaleString('en-IN')}</td><td>${Number(p.oi||0).toLocaleString('en-IN')}</td></tr>`}))}
async function chainNow(){try{const r=await post('/api/data/fo/chain/snapshot',{symbol:document.getElementById('fsym').value.trim()});alert(`${r.status}: ${r.rows} rows`);chain()}catch(e){alert(e.message)}}
async function contracts(){const s=document.getElementById('fsym').value.trim(),e=document.getElementById('fexp').value;const R=await j(`/api/data/fo/contracts?symbol=${encodeURIComponent(s)}${e?'&expiry='+e:''}`);
  document.getElementById('fo').innerHTML=table(['Date','Type','Expiry','Strike','Opt','Close','Settle','OI','ΔOI','Volume','IV %','Underlying'],R.map(r=>`<tr><td>${r.date}</td><td>${esc(r.instrument)}</td><td>${r.expiry}</td><td>${num(r.strike,0)}</td><td>${esc(r.option_type)}</td><td>${num(r.close)}</td><td>${num(r.settle)}</td><td>${Number(r.oi||0).toLocaleString('en-IN')}</td><td>${Number(r.oi_chg||0).toLocaleString('en-IN')}</td><td>${Number(r.volume||0).toLocaleString('en-IN')}</td><td>${num(r.iv,1)}</td><td>${num(r.underlying)}</td></tr>`))}

async function macro(){const M=await j('/api/data/macro');document.getElementById('macro').innerHTML=table(['Series','Period','Value','Previous','Available from','Status'],M.series.map(s=>`<tr><td><a href="#" style="color:var(--accent)" onclick="mseries('${esc(s.series_id)}','${esc(s.name)}');return false">${esc(s.name)}</a><br><span class="muted">${esc(s.series_id)} · ${esc(s.unit)} · ${esc(s.frequency)}</span></td><td>${esc(s.period)}</td><td><b>${num(s.value,3)}</b></td><td>${num(s.previous,3)}</td><td>${esc(s.available_from)}</td><td class="${s.status==='ERROR'?'note':''}">${esc(s.status)}${s.error?'<br>'+esc(s.error):''}</td></tr>`));
  document.getElementById('cal').innerHTML=table(['Date','Event','Source'],M.calendar.map(c=>`<tr><td>${c.event_date}</td><td>${esc(c.event)}</td><td class="muted">${esc(c.source)}</td></tr>`))}
async function mseries(id,name){const S=await j('/api/data/macro/'+encodeURIComponent(id));document.getElementById('mt').textContent=name;lineChart(document.getElementById('mchart'),S.slice(-120).map(r=>({x:r.period,y:r.value})),{fmt:v=>num(v,2),label:id})}
async function macroRefresh(){try{const r=await post('/api/data/macro/refresh');alert(`${r.status}: ${r.rows} new observations, ${r.events} events${r.failed.length?' — failed: '+r.failed.join(', '):''}`);macro()}catch(e){alert(e.message)}}
async function addCal(){try{await post('/api/data/macro/calendar',{event_date:document.getElementById('cd').value.trim(),event:document.getElementById('ce').value.trim()});macro()}catch(e){alert(e.message)}}

async function assets(){const A=await j('/api/data/assets');document.getElementById('assets').innerHTML=`<div class="muted">MF: ${A.mutual_funds.schemes||0} schemes, NAVs to ${esc(A.mutual_funds.latest||'—')}</div>`+
  table(['Class','Asset','From','To','Rows'],A.assets.map(a=>`<tr><td>${esc(a.asset_class)}</td><td><a href="#" style="color:var(--accent)" onclick="aseries('${esc(a.asset_class)}','${esc(a.symbol)}');return false">${esc(a.symbol)}</a></td><td>${a.first}</td><td>${a.last}</td><td>${a.rows}</td></tr>`))}
async function aseries(c,s){const S=await j(`/api/data/assets/${encodeURIComponent(c)}/${encodeURIComponent(s)}`);document.getElementById('at').textContent=`${s} (${c})`;lineChart(document.getElementById('achart'),S.slice(-260).map(r=>({x:r.date,y:r.close})),{fmt:v=>num(v,2),label:s})}
async function assetsRefresh(){try{const r=await post('/api/data/assets/refresh');alert(`${r.status}: ${r.rows} rows${r.errors.length?' — '+r.errors.join('; '):''}`);assets()}catch(e){alert(e.message)}}
async function mfSearch(){const R=await j('/api/data/mf?q='+encodeURIComponent(document.getElementById('mfq').value.trim()));document.getElementById('mf').innerHTML=table(['Scheme','NAV','Date','AMC'],R.map(r=>`<tr><td><a href="#" style="color:var(--accent)" onclick="mfOpen('${esc(r.scheme_code)}');return false">${esc(r.scheme_name)}</a><br><span class="muted">${esc(r.scheme_code)} · ${esc(r.category||'')}</span></td><td>${num(r.nav,4)}</td><td>${r.date}</td><td class="muted">${esc(r.amc||'')}</td></tr>`))}

// W39b scheme detail: analytics, rolling-return chart, category rank, SIP calculator
let MF=null;
const pct=v=>v==null?'—':num(v,2)+'%',val=id=>document.getElementById(id).value.trim(),why=r=>`<span class="muted">${esc(r||'n/a')}</span>`;
const inr=v=>v==null?'—':'₹'+Number(v).toLocaleString('en-IN',{maximumFractionDigits:2});
async function mfOpen(code){document.getElementById('mfd').style.display='block';document.getElementById('mfh').textContent=`Loading ${code} …`;
  const h=val('mfhurdle');try{MF=await j(`/api/data/mf/${encodeURIComponent(code)}/analytics${h?'?hurdle='+encodeURIComponent(h):''}`)}catch(e){MF=null;document.getElementById('mfh').innerHTML=`<p class="note">${esc(e.message)}</p>`;return}
  const A=MF,S=A.scheme,cp=S.category_parsed||{},F=A.since_first_nav;document.getElementById('sipout').innerHTML='';
  document.getElementById('mfh').innerHTML=`<b>${esc(S.scheme_name||S.scheme_code)}</b> <span class="muted">${esc(S.scheme_code)} · ${esc(cp.label||'no AMFI category stored')} · ${esc(S.plan||'plan ?')} / ${esc(S.option||'option ?')}${S.amc?' · '+esc(S.amc):''}</span><br>NAV <b>${num(A.latest_nav,4)}</b> on ${esc(A.as_of)} <span class="muted">· ${A.history.navs} NAVs stored since ${esc(A.history.first)}${A.history.gap_count?` · ${A.history.gap_count} gaps`:''}</span>`+(A.warnings.length?`<div class="note">${A.warnings.map(esc).join('<br>')}</div>`:'');
  document.getElementById('mfret').innerHTML=table(['Period','From','NAV then','Absolute','CAGR'],Object.entries(A.returns).map(([k,r])=>`<tr><td><b>${k}</b></td><td>${esc(r.start_date||r.start_target)}</td><td>${num(r.start_nav,4)}</td><td>${r.reason?why(r.reason):pct(r.absolute_pct)}</td><td>${r.annualised&&!r.reason?pct(r.cagr_pct):'<span class="muted">—</span>'}</td></tr>`).concat([`<tr><td><b>Since first stored NAV</b></td><td>${esc(F.first_date)}</td><td>${num(F.first_nav,4)}</td><td>${pct(F.absolute_pct)}</td><td>${F.reason?why(F.reason):pct(F.cagr_pct)}</td></tr>`]));
  mfRolling();mfRisk();mfCat();document.getElementById('mfnote').textContent=A.note}
function mfRolling(){if(!MF)return;const w=val('mfwin'),R=MF.rolling[w],st=R.stats,el=document.getElementById('mfrchart');
  document.getElementById('mfrt').textContent=`Rolling ${w} returns (annualised) · ${R.windows} windows`+(R.series_step>1?` · chart: every ${R.series_step}th`:'');
  if(!st){el.innerHTML=why(R.reason);document.getElementById('mfrs').innerHTML='';return}
  lineChart(el,R.series.map(p=>({x:p.date,y:p.return_pct})),{fmt:v=>num(v,1)+'%',label:`${w} window ending`,refs:[{y:0,label:'0%'},{y:R.hurdle_pct,label:`hurdle ${num(R.hurdle_pct,1)}%`}]});
  document.getElementById('mfrs').innerHTML=table(['Min','Median','Max','Mean','Windows > 0',`> ${num(R.hurdle_pct,1)}% hurdle`,'Latest'],[`<tr><td>${pct(st.min_pct)}<br><span class="muted">${esc(st.min_end_date)}</span></td><td>${pct(st.median_pct)}</td><td>${pct(st.max_pct)}<br><span class="muted">${esc(st.max_end_date)}</span></td><td>${pct(st.mean_pct)}</td><td>${num(st.pct_positive,1)}%</td><td>${num(st.pct_above_hurdle,1)}%</td><td>${pct(st.latest_pct)}</td></tr>`])}
function mfRisk(){const K=MF.risk,B=K.benchmark||{},w=K.window;
  const dd=x=>x.max_drawdown_pct==null?why(x.reason):x.peak_date?`${pct(x.max_drawdown_pct)} <span class="muted">peak ${esc(x.peak_date)} → trough ${esc(x.trough_date)} → ${x.recovery_date?'recovered '+esc(x.recovery_date):'not recovered'}</span>`:pct(x.max_drawdown_pct);
  const vs=B.status==='OK'?`beta ${num(B.beta,2)} · alpha ${pct(B.alpha_annual_pct)} a year · tracking error ${pct(B.tracking_error_pct)}<br><span class="muted">${B.matched_returns} matched returns; ${esc(B.basis)}</span>`:why(B.reason);
  document.getElementById('mfrisk').innerHTML=`<div class="muted">${esc(w.start)} → ${esc(w.end)} · ${w.returns} daily returns${w.excluded_gap_returns?` (${w.excluded_gap_returns} across gaps left out)`:''} · risk-free ${num(K.risk_free_pct,2)}%${w.note?' · '+esc(w.note):''}</div>`+
    table(['Measure','Value'],[['Volatility (annualised)',K.reason?why(K.reason):pct(K.volatility_pct)],['Sharpe',num(K.sharpe,2)],['Sortino',num(K.sortino,2)],['Max drawdown (window)',dd(K.max_drawdown)],['Max drawdown (all stored)',dd(K.max_drawdown_full_history)],[`vs ${esc(B.symbol||'benchmark')}`,vs]].map(([a,b])=>`<tr><td>${a}</td><td>${b}</td></tr>`))}
function mfCat(){const C=MF.category||{},el=document.getElementById('mfcat');if(C.status!=='OK'){el.innerHTML=why(C.reason);return}
  const rk=r=>r.rank?`<b>${r.rank}</b> of ${r.of} <span class="muted">(${esc(r.order)}; median ${pct(r.category_median)})</span>`:`${why(r.reason)} <span class="muted">(${r.of} ranked)</span>`,cv=C.coverage;
  el.innerHTML=`<div class="muted">${esc((C.category_parsed||{}).label||C.category)} · ${esc(C.peer_filter)} · ${cv.compared} compared of ${cv.in_category_stored} stored in the category (${cv.with_1y} with 1Y, ${cv.with_3y} with 3Y)${cv.truncated_to?' · first '+cv.truncated_to+' only':''}. ${esc(cv.note)}</div>`+
    table(['1Y return','3Y CAGR','1Y volatility'],[`<tr><td>${rk(C.rank.return_1y)}</td><td>${rk(C.rank.cagr_3y)}</td><td>${rk(C.rank.volatility_1y)}</td></tr>`])+
    `<div class="scroll" style="max-height:220px;margin-top:6px">`+table(['Scheme','1Y','3Y CAGR','1Y volatility'],C.peers.map(p=>`<tr${p.is_target?' style="outline:1px solid var(--accent)"':''}><td><a href="#" style="color:var(--accent)" onclick="mfOpen('${esc(p.scheme_code)}');return false">${esc(p.scheme_name||p.scheme_code)}</a></td><td>${pct(p.return_1y_pct)}</td><td>${pct(p.cagr_3y_pct)}</td><td>${pct(p.volatility_1y_pct)}</td></tr>`))+'</div>'}
async function mfSip(){if(!MF)return;const q=new URLSearchParams({amount:val('sipa'),day:val('sipd')});[['start','sips'],['end','sipe'],['lump_sum','sipl']].forEach(([k,id])=>{if(val(id))q.set(k,val(id))});
  const el=document.getElementById('sipout');el.textContent='Calculating …';
  try{const R=await j(`/api/data/mf/${encodeURIComponent(MF.scheme.scheme_code)}/sip?${q}`);if(R.status!=='OK'){el.innerHTML=`<p class="note">${esc(R.reason)}</p>`;return}
    const S=R.sip,L=R.lump_sum,I=R.inputs;
    el.innerHTML=`<div class="muted">${esc(I.start)} → ${esc(I.end)} · valued at NAV ${num(R.valuation.nav,4)} on ${esc(R.valuation.date)}</div>`+(R.warnings.length?`<div class="note">${R.warnings.map(esc).join('<br>')}</div>`:'')+
      table(['','Invested','Units','Value','Gain','Absolute','XIRR'],[`<tr><td><b>SIP</b> ${S.instalments} × ${inr(I.amount)} on day ${I.day}</td><td>${inr(S.invested)}</td><td>${num(S.units,3)}</td><td>${inr(S.value)}</td><td>${inr(S.gain)}</td><td>${pct(S.absolute_return_pct)}</td><td><b>${pct(S.xirr_pct)}</b></td></tr>`,
        `<tr><td><b>Lump sum</b> on ${esc(L.date)} <span class="muted">(${esc(L.basis)})</span></td><td>${inr(L.invested)}</td><td>${num(L.units,3)}</td><td>${inr(L.value)}</td><td>${inr(L.gain)}</td><td>${pct(L.absolute_return_pct)}</td><td><b>${pct(L.xirr_pct)}</b></td></tr>`])+
      `<div class="scroll" style="max-height:200px;margin-top:6px">`+table(['Scheduled','Allotted','NAV','Units','Total units'],R.schedule.map(x=>`<tr><td>${esc(x.scheduled)}</td><td>${esc(x.nav_date)}${x.days_late?` <span class="muted">+${x.days_late}d</span>`:''}</td><td>${num(x.nav,4)}</td><td>${num(x.units,3)}</td><td>${num(x.cumulative_units,3)}</td></tr>`))+'</div>'+
      (R.skipped.length?`<div class="muted">${R.skipped.length} instalments skipped: ${esc(R.skipped[0].reason)}</div>`:'')}
  catch(e){el.innerHTML=`<p class="note">${esc(e.message)}</p>`}}

async function alt(){const A=await j('/api/data/alt');document.getElementById('alt').innerHTML=table(['Source','Entity','Metrics','External','Enabled','Last run','Status','Rows','Coverage','Stale days',''],A.sources.map(s=>{const h=s.health||{};
  return `<tr><td><b>${esc(s.name)}</b><br><span class="muted">${esc(s.source_id)} — ${esc(s.description)}</span></td><td>${esc(s.entity)}</td><td>${s.metrics.map(m=>`<a href="#" style="color:var(--accent)" onclick="altobs('${esc(s.source_id)}','${esc(m)}');return false">${esc(m)}</a>`).join(' · ')}</td><td>${s.external?'yes':'no'}</td><td>${s.enabled?'✔':'—'}</td><td class="muted">${esc(String(h.last_run||'').slice(0,16))}</td><td class="${h.last_status==='FAILED'?'note':''}">${esc(h.last_status||'')}${h.error?'<br>'+esc(h.error):''}</td><td>${h.last_rows??'—'}</td><td>${h.coverage!=null?num(h.coverage*100,0)+'%':'—'}</td><td>${h.stale_days??'—'}</td><td><button onclick="altRun('${esc(s.source_id)}')">Run now</button></td></tr>`}))}
async function altobs(s,m){const R=await j(`/api/data/alt/${encodeURIComponent(s)}/${encodeURIComponent(m)}`);document.getElementById('altt').textContent=`${s} · ${m} — ${R.length} rows`;
  document.getElementById('altobs').innerHTML=table(['Entity','Date','Value','Available from'],R.slice(-400).reverse().map(r=>`<tr><td>${esc(r.entity)}</td><td>${r.date}</td><td>${num(r.value,3)}</td><td class="muted">${esc(r.available_from)}</td></tr>`))}
async function altRun(s){try{const r=await post(`/api/data/alt/${encodeURIComponent(s)}/run`);alert(`${r.rows} rows, coverage ${num(r.coverage*100,0)}%`);alt()}catch(e){alert(e.message)}}
let first='lake';try{first=localStorage.getItem('atip_dp_tab')||'lake'}catch(e){}if(!TABS.some(t=>t[0]===first))first='lake';show(first);
</script></body></html>"""


def render(token: str) -> str:
    return PAGE.replace("__TOKEN__", json.dumps(token))
