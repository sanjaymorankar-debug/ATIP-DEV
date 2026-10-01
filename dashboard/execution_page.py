"""
The /trading page (W4): the minimum UI over the W4 API.

    Status     execution mode, live gate (closed by default), kill switch,
               pending intents; "Evaluate risk" and "Run paper cycle" buttons
    Decisions  latest strategy decisions (strategy, symbol, decision, confidence,
               regime, reason codes)
    Risk       limits (with source), risk decisions (approved / rejected /
               blocked / review, with the failing check), exposure by sector
               and strategy
    Execution  orders (state, fills), fills, positions (book + per strategy)
    Portfolio risk (W25, DB-17)  PAPER / LIVE book: VaR / ES (historical, parametric,
               Monte Carlo; 1 and 10 days), concentration, risk contribution,
               correlation heatmap, performance attribution, emergency exit

Everything is read from /api/*; nothing here computes a result.
"""

import json

PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ATIP Trading</title>
<style>
:root{--bg:#0f172a;--panel:#1e293b;--line:#334155;--text:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8;--good:#059669;--bad:#dc2626;--warn:#f59e0b}
*{box-sizing:border-box;margin:0;padding:0}body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:13px}
.top{background:var(--panel);padding:10px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px}
.top a{color:var(--accent);text-decoration:none;margin-left:12px}.wrap{padding:14px 16px;max-width:1400px}
h2{font-size:13px;color:var(--accent);margin:16px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
table{width:100%;border-collapse:collapse;background:var(--panel);font-size:11.5px}
th,td{padding:5px 7px;border-bottom:1px solid #1e293b55;text-align:left;vertical-align:top}th{color:var(--muted);background:var(--bg);white-space:nowrap}
.muted{color:var(--muted)}.pos{color:var(--good)}.neg{color:var(--bad)}.note{color:var(--warn)}
.pill{display:inline-block;padding:1px 7px;border-radius:9px;font-size:10.5px;background:#334155}
.APPROVED,.FILLED,.AUTHORIZED{background:#065f46}.REJECTED,.FAILED,.BLOCKED{background:#7f1d1d}.REVIEW_REQUIRED,.PARTIALLY_FILLED,.CANCEL_PENDING{background:#78350f}
button{background:#2563eb;color:#fff;border:none;border-radius:6px;padding:5px 10px;cursor:pointer;font-size:12px;margin:2px}
button.ghost{background:transparent;border:1px solid var(--line);color:var(--text)}
.scroll{max-height:360px;overflow:auto}.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:6px 0}
.nav a{color:var(--accent);margin-right:14px;text-decoration:none}
h3{font-size:12px;color:var(--muted);margin:12px 0 6px}.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}
.kpis{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0}.kpi{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 10px;min-width:120px}
.kpi b{display:block;font-size:15px}.kpi span{color:var(--muted);font-size:10.5px}
select{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:3px 6px}
button.danger{background:var(--bad)}
</style></head><body>
<div class="top"><div><b style="color:var(--accent)">📊 ATIP</b> <span class="muted">Trading — risk &amp; execution (W4)</span></div>
<div><a href="/strategies">Strategies</a><a href="/backtests">Backtests</a><a href="/execution-lab">Execution lab</a><a href="/">← Dashboard</a></div></div>
<div class="wrap">
<div class="nav"><a href="#status">Status</a><a href="#decisions">Decisions</a><a href="#risk">Risk</a><a href="#prisk">Portfolio risk</a><a href="#execution">Execution</a><a href="#livepnl">Live P&amp;L</a><a href="#ops">Execution ops</a></div>
<h2 id="status">Status</h2><div id="st"></div>
<div class="row"><button class="ghost" onclick="run(false)">Evaluate risk on pending intents</button>
<button onclick="run(true)">Run paper cycle (risk + paper orders)</button><span id="msg" class="note"></span></div>
<h2 id="decisions">Latest decisions</h2><div id="dec" class="scroll"></div>
<h2>Position intents</h2><div id="int" class="scroll"></div>
<h2 id="risk">Risk limits</h2><div id="lim" class="scroll"></div>
<h2>Risk decisions</h2><div id="rd" class="scroll"></div>
<h2>Exposure (PAPER book)</h2><div id="exp"></div>
<h2 id="prisk">Portfolio risk (W25)</h2>
<div class="row">Book <select id="pbook" onchange="loadRisk()"><option>PAPER</option><option>LIVE</option></select>
<button class="ghost" onclick="loadRisk()">Refresh</button><span id="pmsg" class="muted"></span></div>
<div id="phead"></div>
<div class="grid2"><div><h3>Value at risk / expected shortfall</h3><div id="pvar"></div></div>
<div><h3>Concentration</h3><div id="pconc"></div></div></div>
<h3>Risk contribution by position</h3><div id="prc" class="scroll"></div>
<h3>Correlation</h3><div id="pcorr" class="scroll"></div>
<h3>Performance attribution</h3><div id="pperf" class="scroll"></div>
<h3>Emergency exit</h3><div id="pemx"></div>
<h2 id="livepnl">Live P&amp;L <span class="muted">(W29 MON-04: newest live quote per symbol; refreshes every 60 s)</span></h2>
<div id="lp"></div><div id="lps"></div>
<h2 id="ops">Execution operations (W29)</h2>
<div class="row"><button class="ghost" onclick="w29Run('/api/execution/broker-health/check',{})">Check broker health</button>
<button class="ghost" onclick="w29Run('/api/execution/paper-match',{})">Match resting paper orders</button>
<button class="ghost" onclick="w29Run('/api/execution/reconciliation/run',{})">Reconcile now</button>
<select id="andays" onchange="loadOps()"><option>7</option><option selected>30</option><option>90</option></select><span id="w29msg" class="muted"></span></div>
<div id="bh"></div><div id="rec" style="margin-top:8px"></div><div id="rest" style="margin-top:8px"></div>
<div id="an" style="margin-top:8px"></div><div id="kz" style="margin-top:8px"></div>
<h2 id="execution">Orders</h2><div id="ord" class="scroll"></div>
<h2>Fills</h2><div id="fil" class="scroll"></div>
<h2>Positions</h2><div id="pos" class="scroll"></div>
</div>
<script>
const TOKEN=__TOKEN__;
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const num=(v,d=2)=>v==null?'—':Number(v).toLocaleString(undefined,{maximumFractionDigits:d});
const pill=s=>`<span class="pill ${s}">${s}</span>`;
async function j(u,o){const r=await fetch(u,o);const t=await r.json();if(!r.ok)throw new Error((t.error&&t.error.message)||t.error||r.status);return t}
// W8: every mutating call carries an Idempotency-Key; order create / cancel use a key tied to
// the decision / order, so a double click or a network retry cannot act twice
const idem=k=>k||((crypto.randomUUID&&crypto.randomUUID())||(Date.now()+'-'+Math.random().toString(36).slice(2)));
const send=(m,u,b,k)=>j(u,{method:m,headers:{'Content-Type':'application/json','X-ATIP-Token':TOKEN,'Idempotency-Key':idem(k)},body:JSON.stringify(b||{})});
const table=(h,rows)=>`<table><thead><tr>${h.map(x=>`<th>${x}</th>`).join('')}</tr></thead><tbody>${rows.join('')||`<tr><td colspan=${h.length} class="muted">none</td></tr>`}</tbody></table>`;
async function load(){
  const s=await j('/api/execution/status');
  document.getElementById('st').innerHTML=`Mode <b>${s.settings.mode}</b> · live trading <b class="${s.live_allowed?'neg':'pos'}">${s.live_allowed?'ALLOWED':'DISABLED'}</b> <span class="muted">(${esc(s.live_gate)}; live adapter ${esc(s.live_adapter)})</span> · kill switch ${s.kill_switch?'<b class="neg">HALTED</b> '+esc(s.kill_switch_reason):'<span class="pos">off</span>'} · auto-execute paper <b>${s.settings.auto_execute_paper}</b> · manual review <b>${s.settings.require_manual_review}</b> · pending intents <b>${s.pending_intents}</b>`;
  const d=await j('/api/strategy-decisions?limit=100');
  document.getElementById('dec').innerHTML=table(['Date','Strategy','Symbol','Decision','Confidence','Score','Regime','Reason codes','Reason'],d.map(x=>`<tr><td>${x.as_of}</td><td>${esc(x.strategy_id)} <span class="muted">${x.version}</span></td><td>${esc(x.symbol)}</td><td><b>${x.decision}</b> <span class="muted">${x.action}</span></td><td>${num(x.confidence)}</td><td>${num(x.score,1)}</td><td>${x.regime||''}</td><td class="muted">${esc((x.reason_codes||[]).join(', '))}</td><td class="muted" style="max-width:380px">${esc((x.reasons||[]).join('; '))}</td></tr>`));
  const it=await j('/api/position-intents?limit=100');
  document.getElementById('int').innerHTML=table(['Date','Strategy','Symbol','Side','Action','Qty','Entry ref','Stop','Target','Book','Authorization','Audit'],it.map(x=>`<tr><td>${x.as_of}</td><td>${esc(x.strategy_id)}</td><td>${esc(x.symbol)}</td><td>${x.side}</td><td>${x.action}</td><td>${x.quantity??'—'}</td><td>${num(x.entry_reference)}</td><td>${num(x.stop_price)}</td><td>${num(x.target_price)}</td><td>${x.book||''}</td><td>${pill(x.authorization_status)}</td><td><a style="color:var(--accent)" href="/api/audit/intent/${x.intent_id}" target="_blank">trail</a></td></tr>`));
  const L=await j('/api/risk/limits');
  document.getElementById('lim').innerHTML=table(['Limit','Value','Source','Default','Meaning'],Object.entries(L.limits).map(([k,v])=>`<tr><td>${k}</td><td><b>${v.value??'off'}</b></td><td class="muted">${v.source}</td><td class="muted">${v.default}</td><td class="muted">${esc(v.description)}</td></tr>`))+'<p class="muted">Change with PUT /api/risk/limits {"key": value | null | "default"}. W1 limits in config.json risk_limits also apply.</p>';
  const R=await j('/api/risk/decisions?limit=100');
  document.getElementById('rd').innerHTML=table(['When','Strategy','Symbol','Side','Requested','Approved','Status','Reason / binding checks',''],R.map(x=>{const f=(x.risk_checks||[]).filter(c=>c.status==='FAIL'||c.status==='WARN').map(c=>c.check+': '+c.message);return `<tr><td>${esc(x.created_at)}</td><td>${esc(x.strategy_id)}</td><td>${esc(x.symbol)}</td><td>${x.side}</td><td>${x.requested_quantity??'—'}</td><td>${x.approved_quantity}</td><td>${pill(x.risk_status)}</td><td class="muted" style="max-width:420px">${esc(x.rejection_reason||'')}${f.length?'<br>'+esc(f.join(' | ')):''}</td><td>${x.risk_status==='REVIEW_REQUIRED'?`<button onclick="approve('${x.risk_decision_id}')">Approve</button>`:x.risk_status==='APPROVED'?`<button class="ghost" onclick="order('${x.risk_decision_id}')">Paper order</button>`:''}</td></tr>`}));
  try{const E=await j('/api/risk/exposure');const eq=E.equity||0;
   document.getElementById('exp').innerHTML=`Equity <b>${num(E.equity)}</b> · cash ${num(E.cash)} · positions ${num(E.positions_value)} (${E.n_positions})`+
   table(['Sector','Value','% equity'],Object.entries(E.by_sector).map(([k,v])=>`<tr><td>${esc(k)}</td><td>${num(v)}</td><td>${eq?num(v/eq*100):'—'}%</td></tr>`))+
   table(['Strategy','Value','% equity','P&L','Positions'],Object.entries(E.by_strategy).map(([k,v])=>`<tr><td>${esc(k)}</td><td>${num(v.value)}</td><td>${eq?num(v.value/eq*100):'—'}%</td><td class="${v.pnl>=0?'pos':'neg'}">${num(v.pnl)}</td><td>${v.positions}</td></tr>`));}catch(e){document.getElementById('exp').textContent=e.message}
  const O=await j('/api/oms/orders?limit=100');
  document.getElementById('ord').innerHTML=table(['Created','Order','Strategy','Symbol','Side','Qty','Filled','Avg','Status','Mode','Reason',''],O.map(x=>`<tr><td>${esc(x.created_at)}</td><td><a style="color:var(--accent)" href="/api/oms/orders/${x.order_id}" target="_blank">${x.order_id}</a></td><td>${esc(x.strategy_id)}</td><td>${esc(x.symbol)}</td><td>${x.side}</td><td>${x.quantity}</td><td>${x.filled_quantity}</td><td>${num(x.avg_fill_price)}</td><td>${pill(x.status)}</td><td>${x.mode}</td><td class="muted">${esc(x.reason||'')}</td><td>${['CREATED','VALIDATED','ACKNOWLEDGED','PARTIALLY_FILLED'].includes(x.status)?`<button class="ghost" onclick="cancel('${x.order_id}')">Cancel</button>`:''}</td></tr>`));
  const F=await j('/api/oms/fills?limit=100');
  document.getElementById('fil').innerHTML=table(['When','Order','Strategy','Symbol','Side','Qty','Price','Fees','Price source'],F.map(x=>`<tr><td>${esc(x.filled_at)}</td><td>${x.order_id}</td><td>${esc(x.strategy_id)}</td><td>${esc(x.symbol)}</td><td>${x.side}</td><td>${x.quantity}</td><td>${num(x.price)}</td><td>${num(x.fees)}</td><td class="muted">${esc(x.price_source)}</td></tr>`));
  try{const P=await j('/api/oms/positions');
   document.getElementById('pos').innerHTML=table(['Symbol','Qty','Avg','Mark','Value','Unrealised'],P.positions.map(p=>`<tr><td>${esc(p.symbol)}</td><td>${p.qty}</td><td>${num(p.avg_price)}</td><td>${num(p.mark)}</td><td>${num(p.value)}</td><td class="${(p.unrealised||0)>=0?'pos':'neg'}">${num(p.unrealised)}</td></tr>`))+
   '<p class="muted" style="margin-top:6px">By strategy (from W4 fills)</p>'+table(['Strategy','Symbol','Qty','Avg','Mark','Realised','Unrealised','P&L'],P.by_strategy.map(p=>`<tr><td>${esc(p.strategy_id)}</td><td>${esc(p.symbol)}</td><td>${p.quantity}</td><td>${num(p.avg_price)}</td><td>${num(p.mark)}</td><td>${num(p.realised)}</td><td>${num(p.unrealised)}</td><td class="${p.pnl>=0?'pos':'neg'}">${num(p.pnl)}</td></tr>`));}catch(e){document.getElementById('pos').textContent=e.message}
}
async function run(ex){const m=document.getElementById('msg');m.textContent='running…';try{const r=await send('POST','/api/execution/run',{execute:ex});m.textContent=`${r.status}: ${r.rows} intents; risk ${JSON.stringify(r.risk)}; ${r.orders.length} orders`+(r.error?' — '+r.error:'');load()}catch(e){m.textContent=e.message}}
async function approve(id){try{await send('POST',`/api/risk/decisions/${id}/approve`);load()}catch(e){alert(e.message)}}
async function order(id){if(!confirm('Create and submit a PAPER order for this approved decision?'))return;try{await send('POST','/api/oms/orders',{risk_decision_id:id},`order-${id}`);load()}catch(e){alert(e.message)}}
async function cancel(id){try{await send('POST',`/api/oms/orders/${id}/cancel`,{},`cancel-${id}`);load()}catch(e){alert(e.message)}}
async function loadRisk(){
  const b=document.getElementById('pbook').value, m=document.getElementById('pmsg');m.textContent=' computing…';
  let A;try{A=await j('/api/risk/portfolio?book='+b)}catch(e){m.textContent=' '+e.message;return}
  m.textContent=' as of '+A.as_of+(A.coverage?` · risk covers ${num(A.coverage.value_share*100,1)}% of the book (${A.coverage.sessions} sessions)`:'');
  const X=A.exposure||{}, V=A.var||{}, C=A.concentration||{}, RA=A.risk_attribution||{}, CO=A.correlation||{};
  const l95=((V.levels||[]).find(l=>l.confidence==0.95)||{}).historical||{};
  const k=(t,v)=>`<div class="kpi"><span>${t}</span><b>${v}</b></div>`;
  document.getElementById('phead').innerHTML=A.note&&!X.positions?.length?`<p class="muted">${esc(A.note)}</p>`:
   `<div class="kpis">${k('Positions',X.n_positions??'—')}${k('Invested',num(X.positions_value,0))}${k('Equity',num(X.equity,0))}${k('1-day VaR 95%',num(l95.var_value,0)+' <span>'+num(l95.var_pct)+'%</span>')}${k('ES 95%',num(l95.es_value,0))}${k('Volatility (ann.)',num(V.annual_vol_pct)+'%')}${k('Beta',num(RA.portfolio_beta))}${k('Effective N',num(C.effective_n))}${k('Avg correlation',num(CO.avg_pairwise_weighted))}</div>`+
   table(['Sector','Value','Weight','% equity'],Object.entries(X.by_sector||{}).map(([s,v])=>`<tr><td>${esc(s)}</td><td>${num(v.value,0)}</td><td>${num(v.weight*100,1)}%</td><td>${v.pct_equity==null?'—':num(v.pct_equity,1)+'%'}</td></tr>`));
  const meth=['historical','parametric','monte_carlo','historical_10d','parametric_10d'];
  document.getElementById('pvar').innerHTML=table(['Method','Conf.','VaR %','VaR Rs','ES %','ES Rs','VaR % equity'],(V.levels||[]).flatMap(l=>meth.filter(x=>l[x]).map(x=>`<tr><td>${x.replace('_',' ')}</td><td>${l.confidence*100}%</td><td>${num(l[x].var_pct)}</td><td>${num(l[x].var_value,0)}</td><td>${num(l[x].es_pct)}</td><td>${num(l[x].es_value,0)}</td><td>${l[x].var_pct_equity==null?'—':num(l[x].var_pct_equity)}</td></tr>`)))+(V.worst_day_pct!=null?`<p class="muted">Worst day in window ${num(V.worst_day_pct)}% · losses shown positive</p>`:'');
  document.getElementById('pconc').innerHTML=C.n?table(['Measure','Value'],[['Largest weight',num(C.max_weight*100,1)+'%'],['Top 5 weight',num(C.top5_weight*100,1)+'%'],['HHI',num(C.hhi,3)],['Effective positions',num(C.effective_n)],['Sectors',C.n_sectors],['Effective sectors',num(C.effective_sectors)],['Diversification ratio',num(CO.diversification_ratio)],['Systematic share of variance',RA.systematic_share==null?'—':num(RA.systematic_share*100,1)+'%']].map(r=>`<tr><td>${r[0]}</td><td><b>${r[1]}</b></td></tr>`)):'<p class="muted">—</p>';
  document.getElementById('prc').innerHTML=table(['Symbol','Sector','Weight','Risk share','Risk / weight','Vol (ann.)','Beta','Component VaR 95% Rs'],(RA.positions||[]).map(p=>`<tr><td>${esc(p.symbol)}</td><td class="muted">${esc(p.sector)}</td><td>${num(p.weight*100,1)}%</td><td><b>${num(p.risk_share*100,1)}%</b></td><td class="${p.risk_to_weight>1.2?'neg':''}">${num(p.risk_to_weight)}</td><td>${num(p.vol_annual_pct,1)}%</td><td>${num(p.beta)}</td><td>${num(p.component_var95_value,0)}</td></tr>`));
  const M=CO.matrix;
  document.getElementById('pcorr').innerHTML=M?`<table><thead><tr><th></th>${M.symbols.map(s=>`<th>${esc(s)}</th>`).join('')}</tr></thead><tbody>${M.values.map((r,i)=>`<tr><th>${esc(M.symbols[i])}</th>${r.map((v,jx)=>`<td style="background:${i==jx?'transparent':v>=0?`rgba(220,38,38,${Math.min(Math.abs(v),1)*.7})`:`rgba(5,150,105,${Math.min(Math.abs(v),1)*.7})`}">${num(v)}</td>`).join('')}</tr>`).join('')}</tbody></table><p class="muted">Highly correlated pairs (≥0.7): ${(CO.high_pairs||[]).map(p=>esc(p.a+'/'+p.b+' '+p.corr)).join(', ')||'none'}</p>`:`<p class="muted">${esc(CO.note||'—')}</p>`;
  const P=A.performance_attribution||{}, Bn=P.brinson;
  document.getElementById('pperf').innerHTML=P.positions?`<p>${P.from} → ${P.to}: book <b class="${P.portfolio_return_pct>=0?'pos':'neg'}">${num(P.portfolio_return_pct)}%</b> vs ${P.benchmark} ${num(P.benchmark_return_pct)}% · beta ${num(P.beta)} → market part ${num(P.beta_return_pct)}%, alpha <b>${num(P.alpha_return_pct)}%</b>${Bn?` · Brinson vs ${esc(Bn.benchmark)}: allocation ${num(Bn.allocation_pct)}%, selection ${num(Bn.selection_pct)}%`:''}</p>`+
   table(['Symbol','Start weight','Return','Contribution'],P.positions.map(p=>`<tr><td>${esc(p.symbol)}</td><td>${num(p.start_weight*100,1)}%</td><td class="${p.return_pct>=0?'pos':'neg'}">${num(p.return_pct)}%</td><td>${num(p.contribution_pct,3)}%</td></tr>`))+`<p class="muted">${esc(P.method)}</p>`:`<p class="muted">${esc(P.note||P.error||'—')}</p>`;
  try{const E=await j('/api/risk/emergency-exit?book='+b);
   document.getElementById('pemx').innerHTML=`<p>${E.positions.length} position(s), ${num(E.value,0)} · ${E.open_orders.length} open order(s) · kill switch ${E.kill_switch_on?'<b class="neg">ON</b>':'off'}</p><p class="muted">Will: ${E.will.map(esc).join('; ')}.</p>`+(E.confirm?`<button class="danger" onclick="flatten('${b}','${E.confirm}')">Emergency exit ${b}…</button>`:'<p class="muted">nothing to flatten</p>');}catch(e){document.getElementById('pemx').textContent=e.message}
}
async function flatten(b,code){const t=prompt(`EMERGENCY EXIT (${b}): halts all trading, cancels open orders`+(b==='PAPER'?' and sells every paper position.':'. LIVE: no order is sent; you get the sell list.')+`\nType ${code} to confirm:`);if(t!==code)return;
 const reason=prompt('Reason (recorded):')||'';try{const r=await send('POST','/api/risk/emergency-exit',{book:b,confirm:code,reason},`emx-${code}`);alert(`${r.status}: sold ${r.sold.length}, failed ${r.failed.length}, cancelled ${r.cancelled_orders.length}`+(r.manual_sells.length?`\nSell at broker: `+r.manual_sells.map(x=>x.symbol+' '+x.qty).join(', '):''));load();loadRisk()}catch(e){alert(e.message)}}
async function w29Run(u,b){const m=document.getElementById('w29msg');m.textContent=' running…';
  try{const r=await send('POST',u,b||{},'w29-'+Date.now());m.textContent=' '+(r.overall||r.result||r.status||'done');loadOps();load()}catch(e){m.textContent=' '+e.message}}
async function loadPnl(){try{const P=await j('/api/pnl/live');const card=(b,t)=>`<div class="kpi" style="display:inline-block;min-width:200px;margin:0 10px 8px 0;padding:8px 12px;border:1px solid var(--line);border-radius:8px"><div class="muted">${t}</div><div style="font-size:18px;font-weight:700" class="${(b.day_pnl||0)>=0?'pos':'neg'}">₹${num(b.day_pnl,0)} today</div><div class="muted">value ₹${num(b.value,0)} · unrealised ₹${num(b.unrealized,0)} · ${b.positions} pos</div></div>`;
  const rows=[...P.LIVE.rows.map(r=>({...r,book:'LIVE'})),...P.PAPER.rows.map(r=>({...r,book:'PAPER'}))];
  document.getElementById('lp').innerHTML=card(P.LIVE,'LIVE holdings')+card(P.PAPER,'PAPER book')+`<span class="muted">${P.PAPER.cash!=null?'paper cash ₹'+num(P.PAPER.cash,0)+' · realised ₹'+num(P.PAPER.realized_to_date,0):''} ${P.stale_prices.length?'· stale prices: '+esc(P.stale_prices.slice(0,8).join(', ')):''}</span>`
   +table(['Book','Symbol','Qty','Avg','Price','Day %','Day P&L','Unrealised','Price source'],rows.map(r=>`<tr><td>${r.book}</td><td><b>${esc(r.symbol)}</b></td><td>${r.qty}</td><td>${num(r.avg)}</td><td>${num(r.price)}</td><td class="${(r.day_pct||0)>=0?'pos':'neg'}">${num(r.day_pct)}%</td><td class="${(r.day_pnl||0)>=0?'pos':'neg'}">${num(r.day_pnl,0)}</td><td class="${(r.unrealized||0)>=0?'pos':'neg'}">${num(r.unrealized,0)}</td><td class="muted">${esc(r.price_source||'')}${r.quote_age_min!=null?' · '+r.quote_age_min+'m':''}</td></tr>`))
   +Object.entries(P.strategies).map(([s,b])=>`<div class="muted">strategy ${esc(s)}: day ₹${num(b.day_pnl,0)} · unrealised ₹${num(b.unrealized,0)} · ${b.positions} open</div>`).join('');
  const S=await j('/api/pnl/live/series');const pts=(S.series.PAPER||[]).concat([]);const l=(S.series.LIVE||[]);
  const spark=(v,lbl)=>{if(v.length<2)return '';const ys=v.map(p=>p.day_pnl||0),mn=Math.min(...ys,0),mx=Math.max(...ys,0),rg=mx-mn||1,w=420,h=70;
   return `<div class="muted">${lbl} day P&L ${v[0].ts}→${v[v.length-1].ts}</div><svg viewBox="0 0 ${w} ${h}" width="100%" style="max-width:${w}px"><line x1="0" x2="${w}" y1="${h-4-(0-mn)/rg*(h-8)}" y2="${h-4-(0-mn)/rg*(h-8)}" stroke="#475569" stroke-dasharray="3 3"/><path d="${v.map((p,i)=>`${i?'L':'M'}${(i/(v.length-1)*(w-4)+2).toFixed(1)},${(h-4-((p.day_pnl||0)-mn)/rg*(h-8)).toFixed(1)}`).join('')}" fill="none" stroke="#38bdf8" stroke-width="1.5"/></svg>`};
  document.getElementById('lps').innerHTML=spark(l,'LIVE')+spark(pts,'PAPER')}catch(e){document.getElementById('lp').textContent=e.message}}
async function loadOps(){
  try{const B=await j('/api/execution/broker-health');const L=B.latest;document.getElementById('bh').innerHTML=L?`<b>Broker health ${pill(L.overall)}</b> <span class="muted">${esc(String(L.checked_at).slice(0,16))}${L.in_session?'':' (outside session)'}</span>`+table(['Check','Status','Detail'],L.checks.map(c=>`<tr><td>${esc(c.check)}</td><td>${pill(c.status)}</td><td class="muted">${esc(c.detail)}${c.latency_ms!=null?' · '+c.latency_ms+' ms':''}</td></tr>`)):'<span class="muted">no broker health check yet</span>'}catch(e){document.getElementById('bh').textContent=e.message}
  try{const R=await j('/api/execution/reconciliation?limit=1');const r=R[0];document.getElementById('rec').innerHTML=r?`<b>Reconciliation ${pill(r.status)}</b> <span class="muted">${esc(r.trade_date)} · ${r.breaks} breaks · ${r.explained} explained</span>`+table(['Book','Kind','Symbol / order','Detail'],Object.entries(r.details).flatMap(([bk,d])=>[...d.breaks.map(x=>[bk,'BREAK',x]),...d.explained.map(x=>[bk,'explained',x])]).map(([bk,k,x])=>`<tr><td>${bk}</td><td>${k}</td><td>${esc(x.symbol||x.order_id)}</td><td class="muted">${esc(JSON.stringify(x))}</td></tr>`)):'<span class="muted">no reconciliation yet</span>'}catch(e){document.getElementById('rec').textContent=e.message}
  try{const O=await j('/api/oms/orders?limit=200');const rest=(Array.isArray(O)?O:O.orders||[]).filter(o=>['ACKNOWLEDGED','CREATED','VALIDATED'].includes(o.status));
   document.getElementById('rest').innerHTML=`<b>Resting / open orders</b>`+table(['Order','Symbol','Side','Qty','Type','Limit','Trigger','Status','Actions'],rest.map(o=>`<tr><td>${esc(o.order_id)}</td><td>${esc(o.symbol)}</td><td>${o.side}</td><td>${o.quantity}</td><td>${esc(o.order_type)}</td><td>${num(o.limit_price)}</td><td>${num(o.trigger_price)}</td><td>${pill(o.status)}</td><td><button class="ghost" onclick="w29Modify('${o.order_id}')">Modify</button></td></tr>`))}catch(e){document.getElementById('rest').textContent=e.message}
  try{const d=document.getElementById('andays').value;const A=await j('/api/execution/analytics?days='+d);const o=A.overall;
   document.getElementById('an').innerHTML=`<b>Execution analytics</b> <span class="muted">${esc(A.period)}${A.note?' · '+esc(A.note):''}</span>`+table(['Orders','Fill rate (qty)','Fill rate (orders)','Rejected','Cancelled','Failed','Time to fill p50 / p90','Slippage bps mean / p90 / worst','Slippage cost','Fees'],[`<tr><td>${o.orders}</td><td>${o.fill_rate_qty==null?'—':num(o.fill_rate_qty*100,1)+'%'}</td><td>${o.fill_rate_orders==null?'—':num(o.fill_rate_orders*100,1)+'%'}</td><td>${o.rejected}</td><td>${o.cancelled}</td><td>${o.failed}</td><td>${num(o.time_to_fill_s.median,1)}s / ${num(o.time_to_fill_s.p90,1)}s</td><td>${num(o.slippage_bps.mean)} / ${num(o.slippage_bps.p90)} / ${num(o.slippage_bps.worst)}</td><td>₹${num(o.slippage_cost_rs,0)}</td><td>₹${num(o.fees_rs,0)}</td></tr>`])
    +(o.rejection_reasons.length?`<div class="muted">rejections: ${o.rejection_reasons.map(r=>esc(r.reason)+' ×'+r.count).join(' · ')}</div>`:'')}catch(e){document.getElementById('an').textContent=e.message}
  try{const K=await j('/api/zerodha/status');document.getElementById('kz').innerHTML=`<b>Zerodha session</b> ${pill(K.state)} <span class="muted">${esc(K.detail)}</span> ${K.state!=='VALID'&&K.configured?'<button class="ghost" onclick="w29Kite()">Log in to Zerodha</button>':''}`}catch(e){}
}
async function w29Modify(oid){const q=prompt('New quantity (blank = keep):','');if(q===null)return;const l=prompt('New limit price (blank = keep):','');if(l===null)return;const t=prompt('New trigger price (blank = keep):','');if(t===null)return;
  try{await send('POST',`/api/oms/orders/${oid}/modify`,{quantity:q||null,limit_price:l||null,trigger_price:t||null},'mod-'+oid+'-'+Date.now());loadOps();load()}catch(e){alert(e.message)}}
async function w29Kite(){try{const r=await j('/api/zerodha/login-url');window.open(r.login_url,'_blank')}catch(e){alert(e.message)}}
load();loadRisk();loadPnl();loadOps();setInterval(loadPnl,60000);
</script></body></html>"""


def render(token: str) -> str:
    return PAGE.replace("__TOKEN__", json.dumps(token))
