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
</style></head><body>
<div class="top"><div><b style="color:var(--accent)">📊 ATIP</b> <span class="muted">Trading — risk &amp; execution (W4)</span></div>
<div><a href="/strategies">Strategies</a><a href="/backtests">Backtests</a><a href="/">← Dashboard</a></div></div>
<div class="wrap">
<div class="nav"><a href="#status">Status</a><a href="#decisions">Decisions</a><a href="#risk">Risk</a><a href="#execution">Execution</a></div>
<h2 id="status">Status</h2><div id="st"></div>
<div class="row"><button class="ghost" onclick="run(false)">Evaluate risk on pending intents</button>
<button onclick="run(true)">Run paper cycle (risk + paper orders)</button><span id="msg" class="note"></span></div>
<h2 id="decisions">Latest decisions</h2><div id="dec" class="scroll"></div>
<h2>Position intents</h2><div id="int" class="scroll"></div>
<h2 id="risk">Risk limits</h2><div id="lim" class="scroll"></div>
<h2>Risk decisions</h2><div id="rd" class="scroll"></div>
<h2>Exposure (PAPER book)</h2><div id="exp"></div>
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
load();
</script></body></html>"""


def render(token: str) -> str:
    return PAGE.replace("__TOKEN__", json.dumps(token))
