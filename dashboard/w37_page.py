"""
The /brokers page (W37): consolidated holdings across brokers (BR-07, ENT-06 credentials), broker file / API
import into the wealth ledger (PF-12), the paper options book with an order ticket (ENT-15), and the asset-class
catalogue. Reads and writes only through /api/brokers, /api/portfolio/imports, /api/execution/options|assets.
"""

import json

PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ATIP Brokers</title>
<style>
:root{--bg:#0f172a;--panel:#1e293b;--line:#334155;--text:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8;--good:#34d399;--bad:#f87171;--warn:#f59e0b}
*{box-sizing:border-box;margin:0;padding:0}body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:13px}
.top{background:var(--panel);padding:10px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px}
.top a{color:var(--accent);text-decoration:none;margin-left:12px}.wrap{padding:14px 16px;max-width:1300px}
.tabs{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:10px}.tab{padding:6px 12px;border-radius:6px;background:var(--panel);cursor:pointer;color:var(--muted)}
.tab.on{background:#2563eb;color:#fff}.pane{display:none}.pane.on{display:block}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin-bottom:10px}
h3{font-size:13px;color:var(--accent);margin-bottom:8px}.muted{color:var(--muted)}.note{color:var(--warn)}.ok{color:var(--good)}.bad{color:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:5px 7px;border-bottom:1px solid #33415555;text-align:left;vertical-align:top}th{color:var(--muted)}
input,select,textarea{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:5px 7px;font-size:12.5px}
button{background:#2563eb;color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer;font-size:12.5px;margin:2px}
label{color:var(--muted);font-size:12px;margin-right:8px}.scroll{max-height:420px;overflow:auto}
</style></head><body>
<div class="top"><div><b style="color:var(--accent)">📊 ATIP</b> <span class="muted">Brokers, imports &amp; multi-asset (W37)</span></div>
<div><a href="/account">Account (credentials)</a><a href="/wealth">Wealth</a><a href="/trading">Trading</a><a href="/">← Dashboard</a></div></div>
<div class="wrap"><div class="tabs" id="tabs"></div>

<div class="pane" id="p-acc"><div class="card"><h3>Holdings across brokers</h3><div class="muted">Read-only: credentials come from your encrypted vault (Account page) or ATIP's existing Dhan / Zerodha setup. Nothing here can place an order.</div><div id="cons" style="margin-top:8px"></div></div>
<div class="card"><h3>Supported brokers</h3><div id="brk"></div></div></div>

<div class="pane" id="p-imp"><div class="card"><h3>Import a broker file</h3>
<div class="muted">Tradebook / order history / holdings CSV from Zerodha Console, Upstox, Groww, ICICI Direct, Angel One or any CSV with recognisable columns. Preview first; importing the same file twice adds nothing.</div>
<div style="margin:8px 0"><input type="file" id="file" accept=".csv,.txt"> <label>broker <input id="ib" size="10" placeholder="auto"></label><label>holdings as of <input id="iasof" size="10" placeholder="YYYY-MM-DD"></label>
<button onclick="imp('preview')">Preview</button><button onclick="imp('commit')">Import</button></div><div id="impout"></div></div>
<div class="card"><h3>Import from a connected broker</h3><select id="apib"><option>zerodha</option><option>upstox</option><option>angel</option></select> <button onclick="apiImp()">Import today's trades</button> <span class="muted">Dhan is already synced daily (broker_sync).</span><div id="apiout"></div></div>
<div class="card"><h3>Import history</h3><div id="runs"></div></div></div>

<div class="pane" id="p-opt"><div class="card"><h3>Paper options book</h3><div id="ob"></div></div>
<div class="card"><h3>Order ticket (paper)</h3><div>
<label>underlying <input id="ou" size="10" value="NIFTY"></label><label>expiry <input id="oe" size="10" placeholder="YYYY-MM-DD"></label>
<label>strike <input id="ok" size="8"></label><label>type <select id="ot"><option>CE</option><option>PE</option></select></label>
<label>side <select id="os"><option>BUY</option><option>SELL</option></select></label><label>lots <input id="ol" size="4" value="1"></label>
<button onclick="opt('check')">Check</button><button onclick="opt('order')">Place paper order</button></div><div id="oout" style="margin-top:8px"></div>
<div class="muted" style="margin-top:6px">Long premium only (SELL closes what you hold). Whole lots, premium caps, no new positions on expiry day. Turn on with options.enabled in config.json.</div></div></div>

<div class="pane" id="p-ast"><div class="card"><h3>Asset classes</h3><div id="ast"></div></div></div>
</div>
<script>
const TOKEN=__TOKEN__;
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const num=(v,d=2)=>v==null?'—':Number(v).toLocaleString('en-IN',{maximumFractionDigits:d});
async function j(u,o){const r=await fetch(u,o);const t=await r.json();if(!r.ok)throw new Error(t.error||t.detail||r.status);return t}
const post=(u,b)=>j(u,{method:'POST',headers:{'Content-Type':'application/json','X-ATIP-Token':TOKEN},body:JSON.stringify(b||{})});
const table=(h,rows)=>`<table><thead><tr>${h.map(x=>`<th>${x}</th>`).join('')}</tr></thead><tbody>${rows.join('')||`<tr><td colspan=${h.length} class="muted">none</td></tr>`}</tbody></table>`;
const TABS=[['acc','Accounts'],['imp','Import'],['opt','Options (paper)'],['ast','Asset classes']];const loaded={};
function show(id){document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.dataset.id===id));document.querySelectorAll('.pane').forEach(p=>p.classList.toggle('on',p.id==='p-'+id));
  if(!loaded[id]){loaded[id]=1;({acc,imp:runs,opt:book,ast})[id]()}}
document.getElementById('tabs').innerHTML=TABS.map(([id,l])=>`<div class="tab" data-id="${id}" onclick="show('${id}')">${l}</div>`).join('');

async function acc(){j('/api/brokers').then(B=>document.getElementById('brk').innerHTML=table(['Broker','Credential fields','Live orders'],B.map(b=>`<tr><td><b>${esc(b.broker)}</b></td><td class="muted">${esc(b.credential_fields.join(', '))}</td><td class="muted">${esc(b.live_orders)}</td></tr>`)));
  const el=document.getElementById('cons');el.innerHTML='<span class="muted">reading brokers…</span>';
  try{const C=await j('/api/brokers/consolidated');el.innerHTML=`<div class="muted">read: ${esc(C.brokers_read.join(', ')||'none')}${Object.keys(C.errors).length?` · <span class="note">errors: ${esc(Object.entries(C.errors).map(([k,v])=>k+': '+v).join('; '))}</span>`:''}</div>`+
    table(['Symbol','Quantity','Avg price','By broker'],C.holdings.map(h=>`<tr><td><b>${esc(h.symbol)}</b></td><td>${num(h.quantity,0)}</td><td>${num(h.avg_price)}</td><td class="muted">${esc(Object.entries(h.brokers).map(([b,x])=>`${b} ${num(x.quantity,0)} @ ${num(x.avg_price)}`).join(' · '))}</td></tr>`))}
  catch(e){el.innerHTML=`<span class="bad">${esc(e.message)}</span>`}}

async function readFile(){const f=document.getElementById('file').files[0];if(!f)throw new Error('choose a CSV file');if(f.size>5e6)throw new Error('file too large');return {name:f.name,text:await f.text()}}
async function imp(kind){const o=document.getElementById('impout');o.innerHTML='<span class="muted">working…</span>';
  try{const f=await readFile();const b={filename:f.name,content:f.text,broker:document.getElementById('ib').value.trim()||null,as_of:document.getElementById('iasof').value.trim()||null};
    const r=await post('/api/portfolio/imports/'+kind,b);
    if(kind==='preview'){o.innerHTML=`<div>${esc(r.kind)} · detected <b>${esc(r.broker_detected)}</b> · ${r.summary.rows} rows, ${r.summary.symbols} symbols${r.summary.date_range?` · ${r.summary.date_range.join(' → ')}`:''} · <span class="${r.summary.errors?'note':'ok'}">${r.summary.errors} rows not usable</span></div>`+
      table(Object.keys(r.rows_preview[0]||{line:1}),r.rows_preview.slice(0,20).map(x=>`<tr>${Object.values(x).map(v=>`<td>${esc(v)}</td>`).join('')}</tr>`))+(r.errors.length?`<div class="note" style="margin-top:6px">${r.errors.slice(0,10).map(e=>`line ${e.line}: ${esc(e.problem)}`).join('<br>')}</div>`:'')}
    else{o.innerHTML=`<span class="ok">✔ ${r.added} added, ${r.already_imported} already in the ledger (${esc(r.broker)} ${esc(r.kind)})</span>`;runs()}}
  catch(e){o.innerHTML=`<span class="bad">✖ ${esc(e.message)}</span>`}}
async function apiImp(){const o=document.getElementById('apiout');o.textContent='working…';try{const r=await post('/api/portfolio/imports/from-broker',{broker:document.getElementById('apib').value});o.innerHTML=`<span class="ok">✔ trades: ${r.trades.added} added${r.holdings?` · holdings: ${r.holdings.added} opening positions`:''} (credential: ${esc(r.credential_source)})</span>`;runs()}catch(e){o.innerHTML=`<span class="bad">✖ ${esc(e.message)}</span>`}}
async function runs(){const R=await j('/api/portfolio/imports/runs');document.getElementById('runs').innerHTML=table(['When','Broker','Kind','File','Rows','Added','Already','Errors'],R.map(r=>`<tr><td class="muted">${esc(String(r.created_at).slice(0,16))}</td><td>${esc(r.broker)}</td><td>${esc(r.kind)}</td><td>${esc(r.filename)}</td><td>${r.rows_in}</td><td>${r.added}</td><td>${r.skipped}</td><td>${r.errors}</td></tr>`))}

async function book(){const B=await j('/api/execution/options/book');document.getElementById('ob').innerHTML=`<div class="muted">${B.enabled?'ON':'OFF (options.enabled)'} · paper cash ₹${num(B.cash,0)} · premium held ₹${num(B.premium_held,0)} · realised ₹${num(B.realized_total,0)}</div>`+
  table(['Contract','Qty','Avg','Mark','Unrealised','Mark source'],B.positions.map(p=>`<tr><td><b>${esc(p.underlying)}</b> ${esc(p.expiry)} ${num(p.strike,0)} ${esc(p.option_type)}</td><td>${num(p.qty,0)}</td><td>${num(p.avg_price)}</td><td>${num(p.mark)}</td><td class="${(p.unrealized||0)>=0?'ok':'bad'}">${num(p.unrealized,0)}</td><td class="muted">${esc(p.mark_source)}</td></tr>`))+
  `<div class="muted" style="margin:8px 0 4px">Recent fills</div>`+table(['When','Side','Contract','Lots','Price','Premium','Fees','Realised','Reason'],B.recent_trades.map(t=>`<tr><td class="muted">${esc(String(t.at).slice(0,16))}</td><td>${esc(t.side)}</td><td>${esc(t.underlying)} ${esc(t.expiry)} ${num(t.strike,0)} ${esc(t.option_type)}</td><td>${t.lots}</td><td>${num(t.price)}</td><td>${num(t.premium,0)}</td><td>${num(t.fees)}</td><td>${num(t.realized,0)}</td><td class="muted">${esc(t.reason)}</td></tr>`))}
async function opt(kind){const g=id=>document.getElementById(id).value.trim();const o=document.getElementById('oout');
  try{const r=await post('/api/execution/options/'+kind,{underlying:g('ou'),expiry:g('oe'),strike:+g('ok'),option_type:g('ot'),side:g('os'),lots:+g('ol')});
    o.innerHTML=kind==='check'?`<span class="ok">✔ allowed: ${r.lots} lot(s) × ${r.lot_size} = ${num(r.qty,0)} @ ₹${num(r.price)} (${esc(r.price_source)}) — premium ₹${num(r.qty*r.price,0)}</span>`:
      `<span class="ok">✔ filled ${esc(r.trade_id)}: ${esc(r.side)} ${num(r.qty,0)} @ ₹${num(r.price)}, cash ${num(r.cash_change,0)}</span>`;if(kind==='order')book()}
  catch(e){o.innerHTML=`<span class="bad">✖ ${esc(e.message)}</span>`}}
async function ast(){const A=await j('/api/execution/assets');document.getElementById('ast').innerHTML=`<div class="muted">${esc(A.live)}</div>`+table(['Class','Tradable','Path','Rules'],Object.entries(A.asset_classes).map(([k,v])=>`<tr><td><b>${esc(k)}</b></td><td>${esc(v.tradable)}</td><td class="muted">${esc(v.path)}</td><td class="muted">${esc(v.rules)}</td></tr>`))}
show('acc');
</script></body></html>"""


def render(token: str) -> str:
    return PAGE.replace("__TOKEN__", json.dumps(token))
