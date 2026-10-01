"""
The /execution-lab page (W34): execution algos (EX-11), pre-trade impact (EX-12), latency
telemetry (EX-15), the OMS event bus (EX-16) and the event-driven backtester (BT-17).
Read from /api/execution/* and /api/backtests/event-driven; PAPER only.

The latency chart is a single-series line (one hue, #3987e5 -- validated for this dark
surface), 2px, crosshair tooltip; every charted value is also in the table beside it.
"""

import json

PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ATIP Execution Lab</title>
<style>
:root{--bg:#0f172a;--panel:#1e293b;--line:#334155;--text:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8;--series:#3987e5;--grid:#334155;--warn:#f59e0b}
*{box-sizing:border-box;margin:0;padding:0}body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:13px}
.top{background:var(--panel);padding:10px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px}
.top a{color:var(--accent);text-decoration:none;margin-left:12px}.wrap{padding:14px 16px;max-width:1400px}
h2{font-size:13px;color:var(--accent);margin:18px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
table{width:100%;border-collapse:collapse;background:var(--panel);font-size:11.5px}
th,td{padding:5px 7px;border-bottom:1px solid #1e293b55;text-align:left;vertical-align:top}th{color:var(--muted);background:var(--bg);white-space:nowrap}
.muted{color:var(--muted)}.note{color:var(--warn)}.scroll{max-height:380px;overflow:auto}
.pill{display:inline-block;padding:1px 7px;border-radius:9px;font-size:10.5px;background:#334155}
.WORKING,.COMPLETED{background:#065f46}.PAUSED,.WAITING{background:#78350f}.CANCELLED,.EXPIRED{background:#7f1d1d}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin-bottom:10px}
.row{display:flex;gap:12px;flex-wrap:wrap}.row>.card{flex:1;min-width:300px}
button,select,input{background:#2563eb;color:#fff;border:none;border-radius:6px;padding:4px 9px;cursor:pointer;font-size:11.5px;margin:1px}
select,input{background:var(--bg);color:var(--text);border:1px solid var(--line)}
label{display:inline-block;margin:3px 8px 3px 0;color:var(--muted)}
.chart{position:relative;width:100%;height:170px}.chart svg{width:100%;height:100%;display:block}
.tip{position:absolute;pointer-events:none;background:#0b1220;border:1px solid var(--line);border-radius:6px;padding:4px 7px;font-size:11px;display:none;white-space:nowrap}.tip b{display:block}
pre{white-space:pre-wrap;font-size:11px;color:var(--muted)}
</style></head><body>
<div class="top"><div><b style="color:var(--accent)">📊 ATIP</b> <span class="muted">Execution lab (W34) — PAPER only</span></div>
<div><a href="/trading">Trading</a><a href="/backtests">Backtests</a><a href="/">← Dashboard</a></div></div>
<div class="wrap">

<h2>Execution algos (EX-11) <span class="muted">— large approved orders worked in slices; off unless execution.algo.enabled</span></h2>
<div class="row"><div class="card" style="flex:3"><div id="algos" class="scroll"></div><div id="algodetail" style="margin-top:8px"></div></div>
<div class="card"><b>Start an algo by hand</b><div class="muted">from an APPROVED risk decision with no order yet</div>
<label>risk decision <input id="ard" size="26"></label><label>algo <select id="aalgo"><option>TWAP</option><option>VWAP</option><option>POV</option><option>ICEBERG</option></select></label>
<label>minutes <input id="adur" value="60" size="4"></label><label>POV rate <input id="arate" value="0.1" size="4"></label>
<div><button onclick="startAlgo()">Start</button> <button onclick="tick()">Work all now</button></div></div></div>

<h2>Pre-trade market impact (EX-12) <span class="muted">— square-root model; Y calibrated from LIVE fills only</span></h2>
<div class="card"><label>symbol <input id="isym" size="12" value="RELIANCE"></label><label>quantity <input id="iqty" size="8" value="1000"></label>
<label>side <select id="iside"><option>BUY</option><option>SELL</option></select></label><button onclick="impact()">Estimate</button> <button onclick="calibrate()">Re-calibrate Y</button>
<div id="imp" style="margin-top:8px"></div></div>

<h2>Latency (EX-15) <span class="muted">— data and order paths; worst minute in the window</span></h2>
<div class="row"><div class="card"><label>window <select id="lwin" onchange="latency()"><option value="60">1 h</option><option value="1440" selected>24 h</option><option value="10080">7 d</option></select></label> <button onclick="flushLat()">Flush now</button><div id="lat" class="scroll"></div></div>
<div class="card"><b id="lst">p95 per minute — pick a stage</b><div class="chart" id="lchart"></div></div></div>

<h2>OMS events (EX-16) <span class="muted">— transactional outbox, at-least-once delivery</span></h2>
<div class="row"><div class="card"><div id="evstats"></div><button onclick="dispatchEv()">Deliver pending now</button></div>
<div class="card" style="flex:2"><div id="events" class="scroll"></div></div></div>

<h2>Event-driven backtest (BT-17) <span class="muted">— partial fills vs bar volume, latency, impact, TWAP slices</span></h2>
<div class="card"><label>strategy_id <input id="bsid" size="22"></label><label>start <input id="bstart" size="10" placeholder="YYYY-MM-DD"></label><label>end <input id="bend" size="10" placeholder="YYYY-MM-DD"></label>
<label>timeframe <select id="btf"><option>1d</option><option>15m</option></select></label><label>latency bars <input id="blat" value="1" size="2"></label>
<label>participation <input id="bpart" value="0.1" size="4"></label><label>slices <input id="bsl" value="1" size="2"></label>
<label>impact <select id="bimp"><option>sqrt</option><option>none</option></select></label><label>capital <input id="bcap" value="1000000" size="9"></label>
<button onclick="edbt()">Run</button><div id="bt" style="margin-top:8px"></div></div>
</div>
<script>
const TOKEN=__TOKEN__;
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const num=(v,d=2)=>v==null?'—':Number(v).toFixed(d);
async function j(u,o){const r=await fetch(u,o);const t=await r.json();if(!r.ok)throw new Error(t.error||t.detail||r.status);return t}
const post=(u,b)=>j(u,{method:'POST',headers:{'Content-Type':'application/json','X-ATIP-Token':TOKEN},body:JSON.stringify(b||{})});
const table=(h,rows)=>`<table><thead><tr>${h.map(x=>`<th>${x}</th>`).join('')}</tr></thead><tbody>${rows.join('')||`<tr><td colspan=${h.length} class="muted">none</td></tr>`}</tbody></table>`;
function lineChart(el,pts,{fmt=v=>num(v),label='value'}={}){
  el.innerHTML='';const tip=document.createElement('div');tip.className='tip';
  if(!pts.length){el.textContent='no data in this window';return}
  const W=el.clientWidth||600,H=el.clientHeight||170,L=48,R=10,T=10,B=22;
  const ys=pts.map(p=>p.y),lo=Math.min(0,...ys),hi=Math.max(...ys)||1,y0=lo,y1=hi*1.08;
  const X=i=>L+(pts.length===1?0:(W-L-R)*i/(pts.length-1)),Y=v=>T+(H-T-B)*(1-(v-y0)/(y1-y0));
  const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox',`0 0 ${W} ${H}`);
  const add=(t,a)=>{const e=document.createElementNS(ns,t);for(const k in a)e.setAttribute(k,a[k]);svg.appendChild(e);return e};
  for(let k=0;k<=3;k++){const v=y0+(y1-y0)*k/3,y=Y(v);add('line',{x1:L,x2:W-R,y1:y,y2:y,stroke:'var(--grid)','stroke-width':1});const t=add('text',{x:L-6,y:y+3,'text-anchor':'end',fill:'var(--muted)','font-size':10});t.textContent=fmt(v)}
  [0,pts.length-1].forEach(i=>{const t=add('text',{x:X(i),y:H-6,'text-anchor':i?'end':'start',fill:'var(--muted)','font-size':10});t.textContent=pts[i].x});
  add('path',{d:pts.map((p,i)=>`${i?'L':'M'}${X(i).toFixed(1)},${Y(p.y).toFixed(1)}`).join(''),fill:'none',stroke:'var(--series)','stroke-width':2,'stroke-linejoin':'round','stroke-linecap':'round'});
  const last=pts.length-1;add('circle',{cx:X(last),cy:Y(pts[last].y),r:4,fill:'var(--series)',stroke:'var(--panel)','stroke-width':2});
  const hair=add('line',{y1:T,y2:H-B,stroke:'var(--muted)','stroke-width':1,visibility:'hidden'}),dot=add('circle',{r:4,fill:'var(--series)',stroke:'var(--panel)','stroke-width':2,visibility:'hidden'});
  const hit=add('rect',{x:L,y:T,width:W-L-R,height:H-T-B,fill:'transparent'});
  hit.addEventListener('pointermove',ev=>{const r=svg.getBoundingClientRect(),px=(ev.clientX-r.left)*W/r.width;const i=Math.max(0,Math.min(last,Math.round((px-L)/((W-L-R)/Math.max(1,last)))));
    hair.setAttribute('x1',X(i));hair.setAttribute('x2',X(i));hair.setAttribute('visibility','visible');dot.setAttribute('cx',X(i));dot.setAttribute('cy',Y(pts[i].y));dot.setAttribute('visibility','visible');
    tip.textContent='';const b=document.createElement('b');b.textContent=fmt(pts[i].y);tip.appendChild(b);tip.appendChild(document.createTextNode(`${label} · ${pts[i].x}`));tip.style.display='block';tip.style.left=Math.min(X(i)*r.width/W+10,r.width-160)+'px';tip.style.top='4px'});
  hit.addEventListener('pointerleave',()=>{hair.setAttribute('visibility','hidden');dot.setAttribute('visibility','hidden');tip.style.display='none'});
  el.appendChild(svg);el.appendChild(tip)}

async function algos(){const A=await j('/api/execution/algos?limit=100');
  document.getElementById('algos').innerHTML=table(['Parent','Algo','Side','Symbol','Filled / total','Avg','Window','Status','Est. bps',''],A.map(p=>{
    const est=JSON.parse(p.impact_estimate_json||'{}');const open=['WAITING','WORKING','PAUSED'].includes(p.status);
    return `<tr><td><a href="#" style="color:var(--accent)" onclick="detail('${esc(p.parent_id)}');return false">${esc(p.parent_id)}</a><br><span class="muted">${esc(p.strategy_id||'')}</span></td><td>${esc(p.algo)}</td><td>${esc(p.side)}</td><td><b>${esc(p.symbol)}</b></td><td>${p.filled_qty} / ${p.total_qty}</td><td>${num(p.avg_price)}</td><td class="muted">${esc(String(p.start_at).slice(5,16))} → ${esc(String(p.end_at).slice(11,16))}</td><td><span class="pill ${p.status}">${p.status}</span>${p.reason?`<div class="muted">${esc(p.reason)}</div>`:''}</td><td>${num(est.total_bps)}</td><td>${open?`<button onclick="act('${esc(p.parent_id)}','cancel')">Cancel</button>`:''}${p.status==='PAUSED'?`<button onclick="act('${esc(p.parent_id)}','resume')">Resume</button>`:''}</td></tr>`}))}
async function detail(pid){const R=await j('/api/execution/algos/'+encodeURIComponent(pid));const q=R.quality;
  document.getElementById('algodetail').innerHTML=`<div class="muted">${esc(pid)} · arrival ${num(q.arrival_price)} · avg ${num(q.avg_price)} · interval VWAP ${num(q.interval_vwap)} · vs arrival <b>${num(q.vs_arrival_bps)}</b> bps · vs VWAP <b>${num(q.vs_vwap_bps)}</b> bps · estimated ${num(q.estimated_bps)} bps</div>`+
   table(['Slice','Order','Type','Qty','Filled','Avg','Status'],R.children.map(c=>`<tr><td>${c.algo_slice}</td><td>${esc(c.order_id)}</td><td>${esc(c.order_type)}</td><td>${c.quantity}</td><td>${c.filled_quantity}</td><td>${num(c.avg_fill_price)}</td><td>${esc(c.status)}</td></tr>`))}
async function act(pid,a){const reason=prompt(`Reason to ${a} ${pid}?`);if(reason===null)return;try{await post(`/api/execution/algos/${encodeURIComponent(pid)}/${a}`,{reason});algos()}catch(e){alert(e.message)}}
async function startAlgo(){const algo=document.getElementById('aalgo').value;const params={duration_min:+document.getElementById('adur').value};if(algo==='POV')params.rate=+document.getElementById('arate').value;
  try{const p=await post('/api/execution/algos',{risk_decision_id:document.getElementById('ard').value.trim(),algo,params});alert(`${p.parent_id}: ${p.status}, ${p.start_at} → ${p.end_at}`);algos()}catch(e){alert(e.message)}}
async function tick(){try{const r=await post('/api/execution/algos/tick');alert(`${r.tick.status}: ${r.tick.rows||0} parents${r.tick.reason?' — '+r.tick.reason:''}`);algos()}catch(e){alert(e.message)}}

async function impact(){try{const e=await j(`/api/execution/impact?symbol=${encodeURIComponent(document.getElementById('isym').value.trim())}&quantity=${+document.getElementById('iqty').value}&side=${document.getElementById('iside').value}`);
  document.getElementById('imp').innerHTML=e.ok?`<b>${num(e.total_bps)} bps</b> ≈ ₹${Number(e.cost_rs).toLocaleString('en-IN')} · half-spread ${num(e.half_spread_bps)} + impact ${num(e.impact_bps)} bps · participation ${num((e.participation||0)*100,2)}% of ADV ${e.out_of_model?'<span class="note">(above 25%: outside the model)</span>':''}<div class="muted">σ ${num(e.inputs.sigma_daily_bps)} bps/day · ADV ${Number(e.inputs.adv_shares||0).toLocaleString('en-IN')} · spread ${esc(e.inputs.spread_source)} · Y ${e.y} (${esc(e.y_source)}) · data to ${esc(e.inputs.as_of)}</div>`:`<span class="note">${esc(e.reason)}</span>`}catch(err){alert(err.message)}}
async function calibrate(){try{const r=await post('/api/execution/impact/calibrate');alert(r.adopted?`Y = ${num(r.y,3)} from ${r.n_fills} live fills`:r.note)}catch(e){alert(e.message)}}

async function latency(){const L=await j('/api/execution/latency?minutes='+document.getElementById('lwin').value);const S=Object.entries(L.stages).sort();
  document.getElementById('lat').innerHTML=table(['Stage','n','mean ms','worst p95','worst p99','max','unflushed p95'],S.map(([k,v])=>`<tr><td><a href="#" style="color:var(--accent)" onclick="lseries('${esc(k)}');return false">${esc(k)}</a></td><td>${v.n??'—'}</td><td>${num(v.mean_ms,1)}</td><td>${num(v.worst_minute_p95_ms,1)}</td><td>${num(v.worst_minute_p99_ms,1)}</td><td>${num(v.max_ms,1)}</td><td>${v.unflushed?num(v.unflushed.p95_ms,1)+' (n='+v.unflushed.n+')':'—'}</td></tr>`))}
async function lseries(st){const s=await j(`/api/execution/latency/${encodeURIComponent(st)}?minutes=${document.getElementById('lwin').value}`);document.getElementById('lst').textContent=`p95 per minute — ${st}`;
  lineChart(document.getElementById('lchart'),s.map(r=>({x:String(r.minute).slice(5,16),y:r.p95_ms})),{fmt:v=>num(v,0)+' ms',label:'p95'})}
async function flushLat(){try{await post('/api/execution/latency/flush');latency()}catch(e){alert(e.message)}}

async function evs(){const S=await j('/api/execution/events/stats');
  document.getElementById('evstats').innerHTML=table(['Topic','Events 24h','Pending','With errors'],S.by_topic.map(t=>`<tr><td>${esc(t.topic)}</td><td>${t.events}</td><td>${t.pending}</td><td>${t.failed_attempts}</td></tr>`))+
   `<div class="muted" style="margin:6px 0">Handlers: ${Object.entries(S.handlers).map(([t,h])=>`${esc(t)} → ${h.map(esc).join(', ')}`).join(' · ')}</div>`+
   (S.dead_letters.length?`<div class="note">Dead letters: ${S.dead_letters.length}</div>`+table(['Event','Topic','Key','Attempts','Error'],S.dead_letters.map(d=>`<tr><td>${esc(d.event_id)}</td><td>${esc(d.topic)}</td><td>${esc(d.key)}</td><td>${d.attempts}</td><td class="note">${esc(d.last_error)}</td></tr>`)):'');
  const E=await j('/api/execution/events?limit=100');
  document.getElementById('events').innerHTML=table(['#','Time','Topic','Key','Payload','Delivered'],E.map(e=>`<tr><td>${e.seq}</td><td class="muted">${esc(String(e.created_at).slice(11,19))}</td><td>${esc(e.topic)}</td><td>${esc(e.key)}</td><td class="muted">${esc(Object.entries(e.payload).filter(([k,v])=>v!=null&&k!=='message').map(([k,v])=>k+' '+v).join(' · '))}</td><td>${e.dispatched_at?'✔':e.attempts?'retrying':'pending'}</td></tr>`))}
async function dispatchEv(){try{const r=await post('/api/execution/events/dispatch');alert(`${r.rows} events, ${r.delivered} deliveries, ${r.failed} failed`);evs()}catch(e){alert(e.message)}}

async function edbt(){const g=id=>document.getElementById(id).value.trim();const out=document.getElementById('bt');out.innerHTML='<span class="muted">running…</span>';
  try{const r=await post('/api/backtests/event-driven',{strategy_id:g('bsid'),start:g('bstart'),end:g('bend'),initial_capital:+g('bcap'),
    event_driven:{timeframe:g('btf'),latency_bars:+g('blat'),participation_cap:+g('bpart'),slices:+g('bsl'),impact:g('bimp')}});
    const m=r.metrics;out.innerHTML=`Run <b>${esc(r.run_id)}</b> · ${r.bars} bars · ${r.trades} trades · return ${num((m.total_return||0)*100)}% · Sharpe ${num(m.sharpe)} · max DD ${num((m.max_drawdown||0)*100)}% · fills ${m.fills} (${m.partial_fills} partial) · slippage ₹${num(m.slippage_paid,0)} · costs ₹${num(m.costs_paid,0)} — <a style="color:var(--accent)" href="/backtests">open in Backtests</a>`}
  catch(e){out.innerHTML=`<span class="note">${esc(e.message)}</span>`}}
algos();latency();evs();
</script></body></html>"""


def render(token: str) -> str:
    return PAGE.replace("__TOKEN__", json.dumps(token))
