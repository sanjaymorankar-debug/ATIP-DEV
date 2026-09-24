"""
The /backtests page (W2): the minimum UI over the backtest API -- start a run,
see run status, open a run's metrics, bias report, equity and drawdown curves,
trades and Monte Carlo results, and compare completed runs. Everything is read
from /api/backtests*; nothing here computes a result.
"""

import json

PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ATIP Backtests</title>
<style>
:root{--bg:#0f172a;--panel:#1e293b;--line:#334155;--text:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8;--good:#059669;--bad:#dc2626;--warn:#f59e0b}
*{box-sizing:border-box;margin:0;padding:0}body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:13px}
.top{background:var(--panel);padding:10px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center}
.top a{color:var(--accent);text-decoration:none}.wrap{padding:14px 16px;max-width:1300px}
h2{font-size:13px;color:var(--accent);margin:14px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
.form{display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end;background:var(--panel);padding:10px;border-radius:8px}
.form label{display:flex;flex-direction:column;gap:3px;font-size:11px;color:var(--muted)}
input,select,textarea{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px}
button{background:#2563eb;color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer;font-size:12px}
table{width:100%;border-collapse:collapse;background:var(--panel);font-size:11.5px}
th,td{padding:5px 7px;border-bottom:1px solid #1e293b55;text-align:left;white-space:nowrap}th{color:var(--muted);background:var(--bg)}
tr.sel td{background:#1e3a5f}tbody tr{cursor:pointer}.muted{color:var(--muted)}.pos{color:var(--good)}.neg{color:var(--bad)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:6px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:7px;padding:7px 9px}.kpi b{display:block;font-size:15px}
.kpi span{font-size:10px;color:var(--muted);text-transform:uppercase}
.warn{background:#78350f55;border:1px solid var(--warn);color:#fde68a;border-radius:6px;padding:6px 9px;margin:6px 0;font-size:11.5px}
svg{background:var(--panel);border-radius:8px;width:100%;height:auto}#msg{margin-left:8px;color:var(--warn)}
.scroll{max-height:320px;overflow:auto}
@media (max-width:700px){.form{flex-direction:column;align-items:stretch}}
</style></head><body>
<div class="top"><div><b style="color:var(--accent)">📊 ATIP</b> <span class="muted">Backtests</span></div><a href="/">← Dashboard</a></div>
<div class="wrap">
<h2>New backtest</h2>
<div class="form">
 <label>Strategy<select id="fStrat"></select></label>
 <label>Start<input id="fStart" type="date"></label>
 <label>End<input id="fEnd" type="date"></label>
 <label>Capital ₹<input id="fCap" type="number" step="1000"></label>
 <label>Parameters (JSON)<input id="fParams" size="34" placeholder='{"stop_pct": 3}'></label>
 <label>Cost model<select id="fCost"><option>nse_delivery</option><option>nse_intraday</option><option>flat</option><option>zero</option></select></label>
 <button onclick="startRun()">Run</button><span id="msg"></span>
</div>
<p class="muted" style="margin-top:6px">A backtest describes what the rules would have done on past data under the stated costs and assumptions. It is not a forecast. Research / validation / test periods and walk-forward are available through the API and <code>python -m backtest</code>.</p>
<h2>Runs <button style="float:right;padding:3px 9px" onclick="loadRuns()">↻</button></h2>
<div class="scroll"><table><thead><tr><th>Run</th><th>Kind</th><th>Strategy</th><th>Period</th><th>Window</th><th>Status</th><th>Return</th><th>CAGR</th><th>Sharpe</th><th>Max DD</th><th>Trades</th><th>Compare</th></tr></thead><tbody id="runs"></tbody></table></div>
<div id="detail"></div>
</div>
<script>
const TOKEN=__TOKEN__;
const pct=v=>v==null?'—':(v*100).toFixed(2)+'%', num=(v,d=2)=>v==null?'—':Number(v).toFixed(d);
const cls=v=>v==null?'':(v>=0?'pos':'neg');
let compare=new Set();
async function j(u,o){const r=await fetch(u,o);const t=await r.json();if(!r.ok)throw new Error(t.error||r.status);return t}
async function init(){
  const s=await j('/api/backtests/strategies');
  document.getElementById('fStrat').innerHTML=Object.keys(s.strategies).map(k=>`<option>${k}</option>`).join('');
  document.getElementById('fCap').value=s.defaults.initial_capital;
  const e=new Date();e.setDate(e.getDate()-1);document.getElementById('fEnd').value=e.toISOString().slice(0,10);
  const b=new Date();b.setFullYear(b.getFullYear()-1);document.getElementById('fStart').value=b.toISOString().slice(0,10);
  loadRuns();
}
async function startRun(){
  const m=document.getElementById('msg');m.textContent='';
  let params=null;try{const p=document.getElementById('fParams').value.trim();params=p?JSON.parse(p):null}catch(e){m.textContent='parameters are not valid JSON';return}
  const body={strategy_id:fStrat.value,start:fStart.value,end:fEnd.value,initial_capital:Number(fCap.value),cost_model:fCost.value};
  if(params)body.params=params;
  try{const r=await j('/api/backtests',{method:'POST',headers:{'Content-Type':'application/json','X-ATIP-Token':TOKEN},body:JSON.stringify(body)});
      m.textContent='started '+r.run_id;setTimeout(loadRuns,1500)}catch(e){m.textContent=e.message}
}
async function loadRuns(){
  const runs=await j('/api/backtests?limit=100');
  document.getElementById('runs').innerHTML=runs.map(r=>{const x=r.metrics||{};return `<tr onclick="show('${r.run_id}')">
   <td>${r.run_id}</td><td>${r.kind}</td><td>${r.strategy_id}</td><td>${r.period_label||''}</td><td>${r.start_date||''} → ${r.end_date||''}</td>
   <td>${r.status}</td><td class="${cls(x.total_return)}">${pct(x.total_return)}</td><td>${pct(x.cagr)}</td><td>${num(x.sharpe)}</td>
   <td class="neg">${pct(x.max_drawdown)}</td><td>${x.trades??'—'}</td>
   <td onclick="event.stopPropagation()"><input type="checkbox" ${compare.has(r.run_id)?'checked':''} onchange="toggleCmp('${r.run_id}',this.checked)"></td></tr>`}).join('');
  if(runs.some(r=>r.status==='RUNNING'||r.status==='CREATED'))setTimeout(loadRuns,4000);
}
function toggleCmp(id,on){on?compare.add(id):compare.delete(id);if(compare.size>1)showCompare()}
async function showCompare(){
  const rs=await Promise.all([...compare].map(id=>j('/api/backtests/'+id)));
  const keys=['total_return','cagr','volatility','sharpe','sortino','calmar','max_drawdown','win_rate','profit_factor','expectancy','trades','costs_paid'];
  const f=(k,v)=>['total_return','cagr','volatility','max_drawdown','win_rate'].includes(k)?pct(v):num(v);
  document.getElementById('detail').innerHTML=`<h2>Comparison</h2><table><thead><tr><th>Metric</th>${rs.map(r=>`<th>${r.run_id}<br><span class="muted">${r.strategy_id} ${r.period_label}</span></th>`).join('')}</tr></thead>
   <tbody>${keys.map(k=>`<tr><td>${k}</td>${rs.map(r=>`<td>${f(k,(r.metrics||{})[k])}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}
function chart(points,key,color,fmt,h){
  if(points.length<2)return '<p class="muted">not enough points</p>';
  const W=1200,H=h||220,P=36,vals=points.map(p=>p[key]),mn=Math.min(...vals),mx=Math.max(...vals),rg=(mx-mn)||1;
  const x=i=>P+i*(W-2*P)/(points.length-1),y=v=>H-P+(-(v-mn)/rg)*(H-2*P);
  const d=vals.map((v,i)=>(i?'L':'M')+x(i).toFixed(1)+','+y(v).toFixed(1)).join('');
  return `<svg viewBox="0 0 ${W} ${H}"><path d="${d}" fill="none" stroke="${color}" stroke-width="1.6"/>
   <text x="4" y="${P}" fill="#94a3b8" font-size="11">${fmt(mx)}</text><text x="4" y="${H-P+4}" fill="#94a3b8" font-size="11">${fmt(mn)}</text>
   <text x="${P}" y="${H-8}" fill="#94a3b8" font-size="11">${points[0].date}</text><text x="${W-P}" y="${H-8}" fill="#94a3b8" font-size="11" text-anchor="end">${points[points.length-1].date}</text></svg>`;
}
async function show(id){
  const r=await j('/api/backtests/'+id), m=r.metrics||{}, b=r.bias_report||{};
  let h=`<h2>Run ${id} — ${r.strategy_id} v${r.strategy_version} · ${r.period_label} · ${r.start_date} → ${r.end_date} · ${r.status}</h2>`;
  if(r.error)h+=`<div class="warn">${r.error}</div>`;
  (b.warnings||[]).concat(b.test_window_warnings||[]).forEach(w=>h+=`<div class="warn">⚠ ${w}</div>`);
  const k=[['Total return',pct(m.total_return)],['CAGR',pct(m.cagr)],['Volatility',pct(m.volatility)],['Sharpe',num(m.sharpe)],['Sortino',num(m.sortino)],
   ['Calmar',num(m.calmar)],['Max drawdown',pct(m.max_drawdown)],['Trades',m.trades??'—'],['Win rate',pct(m.win_rate)],['Avg win ₹',num(m.avg_win,0)],
   ['Avg loss ₹',num(m.avg_loss,0)],['Profit factor',num(m.profit_factor)],['Expectancy ₹',num(m.expectancy,0)],['Costs ₹',num(m.costs_paid,0)],['Slippage ₹',num(m.slippage_paid,0)],['Avg exposure',num(m.avg_exposure)+'%']];
  h+=`<div class="grid">${k.map(([a,v])=>`<div class="kpi"><span>${a}</span><b>${v}</b></div>`).join('')}</div>`;
  if(r.kind==='walk_forward'){
    h+=`<h2>Windows</h2><table><thead><tr><th>#</th><th>Kind</th><th>Window</th><th>Status</th><th>Params</th><th>Return</th><th>Sharpe</th></tr></thead><tbody>${(r.children||[]).map(c=>`<tr onclick="show('${c.run_id}')"><td>${c.window_index}</td><td>${c.kind}</td><td>${c.start_date} → ${c.end_date}</td><td>${c.status}</td><td>${JSON.stringify(c.params)}</td><td>${pct((c.metrics||{}).total_return)}</td><td>${num((c.metrics||{}).sharpe)}</td></tr>`).join('')}</tbody></table>`;
    document.getElementById('detail').innerHTML=h;return;
  }
  const [eq,dd,tr,mc]=await Promise.all([j(`/api/backtests/${id}/equity`),j(`/api/backtests/${id}/drawdowns`),j(`/api/backtests/${id}/trades`),j(`/api/backtests/${id}/montecarlo`)]);
  h+=`<h2>Equity</h2>${chart(eq,'equity','#38bdf8',v=>'₹'+Math.round(v).toLocaleString('en-IN'))}`;
  h+=`<h2>Drawdown</h2>${chart(eq,'drawdown_pct','#dc2626',v=>v.toFixed(1)+'%',160)}`;
  h+=`<h2>Drawdown episodes</h2><table><thead><tr><th>Peak</th><th>Trough</th><th>Recovered</th><th>Depth</th><th>Duration (sessions)</th><th>Recovery (sessions)</th></tr></thead><tbody>${dd.slice(0,10).map(e=>`<tr><td>${e.peak_date}</td><td>${e.trough_date}</td><td>${e.recovery_date||'not yet'}</td><td class="neg">${num(e.depth_pct)}%</td><td>${e.duration_sessions}</td><td>${e.recovery_sessions??'—'}</td></tr>`).join('')}</tbody></table>`;
  h+=`<h2>Monte Carlo <button style="float:right;padding:3px 9px" onclick="runMC('${id}','trade_shuffle')">Trade shuffle</button><button style="float:right;padding:3px 9px;margin-right:6px" onclick="runMC('${id}','return_bootstrap')">Return bootstrap</button></h2>`;
  h+=mc.length?mc.map(x=>{const R=x.results||{};const row=(n,d)=>d&&d.n?`<tr><td>${n}</td>${['p5','p25','p50','p75','p95'].map(p=>`<td>${pct(d[p])}</td>`).join('')}</tr>`:'';
     return `<p class="muted">${R.method} · ${R.n_sims} sims · seed ${R.seed} · P(loss) ${pct(R.prob_loss)} — ${R.disclaimer}</p><table><thead><tr><th></th><th>p5</th><th>p25</th><th>p50</th><th>p75</th><th>p95</th></tr></thead><tbody>${row('Final return',R.final_return)}${row('Max drawdown',R.max_drawdown)}</tbody></table>`}).join(''):'<p class="muted">none yet</p>';
  h+=`<h2>Trades (${tr.length})</h2><div class="scroll"><table><thead><tr><th>#</th><th>Symbol</th><th>Entry</th><th>Price</th><th>Qty</th><th>Exit</th><th>Price</th><th>Reason</th><th>Net ₹</th><th>Return</th><th>Costs ₹</th><th>Sessions</th></tr></thead><tbody>${tr.map(t=>`<tr><td>${t.seq}</td><td>${t.symbol}</td><td>${t.entry_date}</td><td>${num(t.entry_price)}</td><td>${t.qty}</td><td>${t.exit_date}</td><td>${num(t.exit_price)}</td><td>${t.exit_reason}</td><td class="${cls(t.net_pnl)}">${num(t.net_pnl,0)}</td><td class="${cls(t.return_pct)}">${num(t.return_pct)}%</td><td>${num(t.costs,0)}</td><td>${t.holding_sessions}</td></tr>`).join('')}</tbody></table></div>`;
  h+=`<h2>Configuration snapshot</h2><pre class="muted" style="white-space:pre-wrap;font-size:11px">${JSON.stringify({config:r.config,code_version:r.code_version,config_hash:r.config_hash,data:r.data_fingerprint,bias:b},null,1).replace(/</g,'&lt;')}</pre>`;
  document.getElementById('detail').innerHTML=h;
}
async function runMC(id,method){
  try{await j(`/api/backtests/${id}/montecarlo`,{method:'POST',headers:{'Content-Type':'application/json','X-ATIP-Token':TOKEN},body:JSON.stringify({method,n_sims:1000,seed:42})});show(id)}
  catch(e){alert(e.message)}
}
init();
</script></body></html>"""


def render(token: str) -> str:
    return PAGE.replace("__TOKEN__", json.dumps(token))
