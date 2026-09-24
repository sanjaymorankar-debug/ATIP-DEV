"""
The /strategies page (W3): the minimum UI over the Strategy Engine API --
strategy list (version, status, latest backtest, health), regime mapping,
strategy detail (definition, parameters, lifecycle, versions, backtest
history, decisions, position intents, health), lifecycle transitions,
"generate decisions" and "submit backtest".
Everything is read from /api/strategies*; nothing here computes a result.
"""

import json

PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ATIP Strategies</title>
<style>
:root{--bg:#0f172a;--panel:#1e293b;--line:#334155;--text:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8;--good:#059669;--bad:#dc2626;--warn:#f59e0b}
*{box-sizing:border-box;margin:0;padding:0}body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:13px}
.top{background:var(--panel);padding:10px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center}
.top a{color:var(--accent);text-decoration:none;margin-left:12px}.wrap{padding:14px 16px;max-width:1300px}
h2{font-size:13px;color:var(--accent);margin:14px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
table{width:100%;border-collapse:collapse;background:var(--panel);font-size:11.5px}
th,td{padding:5px 7px;border-bottom:1px solid #1e293b55;text-align:left;vertical-align:top}th{color:var(--muted);background:var(--bg);white-space:nowrap}
tbody tr.click{cursor:pointer}.muted{color:var(--muted)}.pos{color:var(--good)}.neg{color:var(--bad)}
.pill{display:inline-block;padding:1px 7px;border-radius:9px;font-size:10.5px;background:#334155}
.HEALTHY{background:#065f46}.WARNING{background:#78350f}.ERROR{background:#7f1d1d}.STALE{background:#6b21a8}.DISABLED{background:#334155}
button{background:#2563eb;color:#fff;border:none;border-radius:6px;padding:5px 10px;cursor:pointer;font-size:12px;margin:2px}
button.ghost{background:transparent;border:1px solid var(--line);color:var(--text)}
input,select{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:4px 7px;font-size:12px}
pre{white-space:pre-wrap;font-size:11px;background:var(--panel);padding:8px;border-radius:6px;max-height:380px;overflow:auto}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:6px 0}.note{color:var(--warn);font-size:11.5px}
.scroll{max-height:340px;overflow:auto}
@media (max-width:700px){.row{flex-direction:column;align-items:stretch}}
</style></head><body>
<div class="top"><div><b style="color:var(--accent)">📊 ATIP</b> <span class="muted">Strategies</span></div>
<div><a href="/api/strategies/combined">Combined (JSON)</a><a href="/backtests">Backtests</a><a href="/">← Dashboard</a></div></div>
<div class="wrap">
<p class="muted">Strategies produce decisions and position intents, not orders: every intent here is <b>NOT_AUTHORIZED</b> (the risk and execution path is W4).</p>
<h2>Strategies</h2>
<div class="scroll"><table><thead><tr><th>Strategy</th><th>Kind</th><th>Version</th><th>Status</th><th>Last backtest</th><th>Return</th><th>Sharpe</th><th>Max DD</th><th>Latest signals (score)</th><th>Health</th></tr></thead><tbody id="list"></tbody></table></div>
<h2>Regime mapping <span class="muted">(Market Health regime → strategies in play; edit via PUT /api/strategies/regime-mapping/{regime})</span></h2>
<div id="regimes"></div>
<div id="detail"></div>
</div>
<script>
const TOKEN=__TOKEN__;
const pct=v=>v==null?'—':(v*100).toFixed(2)+'%', num=(v,d=2)=>v==null?'—':Number(v).toFixed(d);
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function j(u,o){const r=await fetch(u,o);const t=await r.json();if(!r.ok)throw new Error(t.error||r.status);return t}
const post=(u,b)=>j(u,{method:'POST',headers:{'Content-Type':'application/json','X-ATIP-Token':TOKEN},body:JSON.stringify(b||{})});
async function load(){
  const s=await j('/api/strategies');
  document.getElementById('list').innerHTML=s.map(x=>{const b=x.latest_backtest||{},m=b.metrics||{},h=x.health;
   return `<tr class="click" onclick="show('${x.strategy_id}')"><td><b>${esc(x.strategy_id)}</b><br><span class="muted">${esc(x.name)}</span></td><td>${x.kind}</td><td>${x.current_version}</td><td>${x.status}</td>
   <td>${b.run_id?esc(b.run_id)+' <span class="muted">'+esc(b.period_label||'')+' '+b.status+'</span>':'—'}</td><td class="${(m.total_return||0)>=0?'pos':'neg'}">${pct(m.total_return)}</td><td>${num(m.sharpe)}</td><td class="neg">${pct(m.max_drawdown)}</td>
   <td>${sig(x.latest_run)}</td><td>${h?`<span class="pill ${h.status}">${h.status}</span>`:'—'}</td></tr>`}).join('');
}
function sig(r){if(!r)return '<span class="muted">no run</span>';if(r.status==='FAILED')return `<span class="neg">${r.as_of} FAILED</span>`;
  const c=Object.entries(r.counts||{}).map(([k,v])=>k+' '+v).join(', ');
  return `<span class="muted">${r.as_of}</span> ${esc(c)}`+(r.top||[]).map(t=>`<br>${t.decision} ${esc(t.symbol)} <span class="muted">(${num(t.score,1)})</span>`).join('')}
async function loadRegimes(){
  const r=await j('/api/strategies/regime-mapping'), m=r.mapping;
  document.getElementById('regimes').innerHTML=`<table><thead><tr><th>Regime</th><th>Strategies (priority · weight)</th></tr></thead><tbody>${Object.keys(m).map(k=>`<tr><td>${k}</td><td>${m[k].no_trade?'<b class="neg">NO_TRADE</b>':(m[k].strategies.map(x=>`${esc(x.strategy_id)} <span class="muted">(${x.priority} · ${x.weight}${x.enabled?'':' · disabled'})</span>`).join(', ')||'<span class="muted">—</span>')}</td></tr>`).join('')}</tbody></table>`;
}
async function show(id){
  const s=await j('/api/strategies/'+id), d=s.definition||{}, P=await j(`/api/strategies/${id}/parameters`);
  let h=`<h2>${esc(s.strategy_id)} — ${esc(s.name)} · v${s.current_version} · ${s.status}</h2><p>${esc(s.description)}</p>`;
  h+=`<div class="row"><span class="muted">Lifecycle:</span>${s.allowed_transitions.map(t=>`<button class="ghost" onclick="move('${id}','${t}')">→ ${t}</button>`).join('')||'<span class="muted">terminal</span>'}</div>`;
  h+=`<div class="row"><button onclick="decide('${id}')">Generate decisions (PAPER book)</button><button class="ghost" onclick="health('${id}')">Recompute health</button>
      <span class="muted">Backtest v${s.current_version}:</span><input type="date" id="bs"><input type="date" id="be"><input type="number" id="bc" placeholder="capital ₹" step="1000"><button onclick="bt('${id}')">Submit backtest</button><span id="msg" class="note"></span></div>`;
  h+=`<h2>Parameters</h2><table><thead><tr><th>Name</th><th>Type</th><th>Default</th><th>Min</th><th>Max</th><th>Allowed</th><th>Description</th></tr></thead><tbody>${P.parameters.map(p=>`<tr><td>${p.name}</td><td>${p.type}</td><td>${esc(JSON.stringify(p.default))}</td><td>${p.min??''}</td><td>${p.max??''}</td><td>${p.allowed?esc(p.allowed.join(', ')):''}</td><td>${esc(p.description)}</td></tr>`).join('')||'<tr><td colspan=7 class="muted">none</td></tr>'}</tbody></table>`;
  h+=`<h2>Definition v${s.current_version} <span class="muted">inputs: ${(d.inputs||[]).join(', ')} · features: ${(d.features_used||[]).join(', ')}</span></h2><pre>${esc(JSON.stringify(d,null,1))}</pre>`;
  h+=`<h2>Health</h2><div id="health">${s.health?healthHtml(s.health):'<span class="muted">not computed yet</span>'}</div>`;
  h+=`<h2>Decisions</h2><div id="dec" class="scroll"></div><h2>Position intents <span class="muted">(NOT_AUTHORIZED — not orders)</span></h2><div id="intents" class="scroll"></div>`;
  h+=`<h2>Backtests</h2><table><thead><tr><th>Run</th><th>Version</th><th>Kind</th><th>Period</th><th>Window</th><th>Status</th><th>Return</th><th>Sharpe</th><th>Max DD</th></tr></thead><tbody>${s.backtests.map(b=>{const m=b.metrics_json?JSON.parse(b.metrics_json):{};return `<tr><td><a style="color:var(--accent)" href="/backtests">${b.run_id}</a></td><td>${b.strategy_version}</td><td>${b.kind}</td><td>${b.period_label||''}</td><td>${b.start_date} → ${b.end_date}</td><td>${b.status}</td><td>${pct(m.total_return)}</td><td>${num(m.sharpe)}</td><td class="neg">${pct(m.max_drawdown)}</td></tr>`}).join('')||'<tr><td colspan=9 class="muted">none</td></tr>'}</tbody></table>`;
  h+=`<h2>Versions</h2><table><thead><tr><th>Version</th><th>Hash</th><th>Created</th><th>Notes</th></tr></thead><tbody>${s.versions.map(v=>`<tr><td>${v.version}</td><td class="muted">${v.definition_hash.slice(0,16)}…</td><td>${v.created_at}</td><td>${esc(v.notes)}</td></tr>`).join('')}</tbody></table>`;
  h+=`<h2>Lifecycle history</h2><table><thead><tr><th>When</th><th>Version</th><th>From</th><th>To</th><th>Reason</th><th>Evidence</th><th>By</th></tr></thead><tbody>${s.lifecycle.map(e=>`<tr><td>${e.at}</td><td>${e.version||''}</td><td>${e.from_state||''}</td><td>${e.to_state}</td><td>${esc(e.message)}</td><td class="muted">${esc(JSON.stringify(e.details))}</td><td>${esc(e.actor)}</td></tr>`).join('')}</tbody></table>`;
  document.getElementById('detail').innerHTML=h; loadDecisions(id);
}
function healthHtml(x){const m=x.metrics;let h=`<span class="pill ${x.status}">${x.status}</span> <span class="muted">${x.as_of||''} · ${esc(x.status_reason||'')}</span>`;
  if(m){const le=m.last_execution,ls=m.last_signal,da=m.data_availability,pr=m.performance_ref;
   h+=`<table style="margin-top:6px"><tbody><tr><th>Last execution</th><td>${le?le.as_of+' '+le.status:'never'}</td><th>Last signal</th><td>${ls?ls.as_of+' '+ls.decision+' '+esc(ls.symbol):'none'}</td></tr>
   <tr><th>Signals / run</th><td>${m.signal_frequency??'—'} <span class="muted">(${m.decision_runs} runs)</span></td><th>Errors</th><td>${m.error_count} failed runs · ${m.error_events} events</td></tr>
   <tr><th>Data availability</th><td>${da?da.evaluated+' / '+da.universe:'—'}</td><th>Parameters</th><td>${m.parameter_validity.valid?'valid':'<span class="neg">'+esc(m.parameter_validity.error)+'</span>'}</td></tr>
   <tr><th>Performance ref</th><td colspan=3>${pr?esc(pr.run_id)+' '+esc(pr.period_label||pr.kind||'')+' · return '+pct(pr.total_return)+' · Sharpe '+num(pr.sharpe)+' · max DD '+pct(pr.max_drawdown):'no completed backtest'}</td></tr></tbody></table>`}
  return h+((x.issues||[]).map(i=>`<div class="note">${i.level}: ${esc(i.message)}</div>`).join(''))}
async function loadDecisions(id){
  const r=await j(`/api/strategies/${id}/decisions?limit=200`);
  document.getElementById('dec').innerHTML=`<table><thead><tr><th>Date</th><th>Symbol</th><th>Decision</th><th>Target %</th><th>Confidence</th><th>Score</th><th>Regime</th><th>Stop</th><th>Target</th><th>Reasons</th></tr></thead><tbody>${r.decisions.map(x=>`<tr><td>${x.as_of}</td><td>${x.symbol}</td><td><b>${x.decision}</b> <span class="muted">${x.action}</span>${x.blocked_reason?'<br><span class="note">'+esc(x.blocked_reason)+'</span>':''}</td><td>${x.target_position_pct??''}</td><td>${num(x.confidence)}</td><td>${num(x.score,1)}</td><td>${x.regime||''}</td><td>${x.stop_price??''}</td><td>${x.target_price??''}</td><td class="muted" style="max-width:420px">${esc((x.reasons||[]).join('; '))}</td></tr>`).join('')||'<tr><td colspan=10 class="muted">no decisions yet</td></tr>'}</tbody></table>`;
  document.getElementById('intents').innerHTML=`<table><thead><tr><th>Date</th><th>Symbol</th><th>Side</th><th>Action</th><th>Target %</th><th>Qty (indicative)</th><th>Confidence</th><th>Authorization</th><th>Reason</th></tr></thead><tbody>${r.intents.map(x=>`<tr><td>${x.as_of}</td><td>${x.symbol}</td><td>${x.side}</td><td>${x.action}</td><td>${x.target_position_pct??''}</td><td>${x.quantity??'—'}</td><td>${num(x.confidence)}</td><td class="note">${x.authorization_status}</td><td class="muted" style="max-width:420px">${esc(x.reason)}</td></tr>`).join('')||'<tr><td colspan=9 class="muted">no intents</td></tr>'}</tbody></table>`;
}
async function move(id,to){const reason=prompt(`Reason for ${to}?`)||'';try{await post(`/api/strategies/${id}/lifecycle`,{to_state:to,reason});load();show(id)}catch(e){alert(e.message)}}
async function decide(id){try{const r=await post(`/api/strategies/${id}/decisions`,{book:'PAPER'});alert('Decisions for '+r.as_of+': '+JSON.stringify(r.counts));loadDecisions(id)}catch(e){alert(e.message)}}
async function health(id){try{const r=await j(`/api/strategies/${id}/health`);document.getElementById('health').innerHTML=healthHtml(r);load()}catch(e){alert(e.message)}}
async function bt(id){const m=document.getElementById('msg');m.textContent='';
  const b={start:document.getElementById('bs').value,end:document.getElementById('be').value};const c=document.getElementById('bc').value;if(c)b.initial_capital=Number(c);
  try{const r=await post(`/api/strategies/${id}/backtest`,b);m.textContent='started '+r.run_id+' (see Backtests)'}catch(e){m.textContent=e.message}}
load();loadRegimes();
</script></body></html>"""


def render(token: str) -> str:
    return PAGE.replace("__TOKEN__", json.dumps(token))
