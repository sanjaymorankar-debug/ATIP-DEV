"""
The /baskets page (W39): basket orders (EX-18) and stock SIP plans (EX-20). Reads and writes only through
/api/orders/baskets and /api/orders/sip; where an order goes is decided by broker_env (PAPER by default) and the
live-trading master switch, exactly as for a single order.
"""

import json

PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ATIP Baskets &amp; SIP</title>
<style>
:root{--bg:#0f172a;--panel:#1e293b;--line:#334155;--text:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8;--good:#34d399;--bad:#f87171;--warn:#f59e0b}
*{box-sizing:border-box;margin:0;padding:0}body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:13px}
.top{background:var(--panel);padding:10px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px}
.top a{color:var(--accent);text-decoration:none;margin-left:12px}.wrap{padding:14px 16px;max-width:1200px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin-bottom:10px}
h3{font-size:13px;color:var(--accent);margin-bottom:8px}.muted{color:var(--muted)}.ok{color:var(--good)}.bad{color:var(--bad)}.note{color:var(--warn)}
table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:5px 7px;border-bottom:1px solid #33415555;text-align:left;vertical-align:top}th{color:var(--muted)}
input,select{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:5px 7px;font-size:12.5px}
button{background:#2563eb;color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer;font-size:12.5px;margin:2px}
button.ghost{background:#334155}button.red{background:#b91c1c}label{color:var(--muted);font-size:12px;margin-right:6px}
.row{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:4px 0}
</style></head><body>
<div class="top"><div><b style="color:var(--accent)">📊 ATIP</b> <span class="muted">Basket orders &amp; stock SIP</span> <span id="env" class="note"></span></div>
<div><a href="/trading">Trading</a><a href="/wealth">Wealth</a><a href="/">← Dashboard</a></div></div>
<div class="wrap">
<div class="card"><h3>Baskets</h3><div class="muted">A basket is previewed as dry runs first (halt, live switch, risk limits and the funds for the whole basket). Placing it sends nothing unless every leg is clean; SELL legs go first. Where orders go is decided by broker_env (PAPER by default).</div>
<div id="blist" style="margin-top:8px"></div></div>
<div class="card"><h3 id="bedit_t">New basket</h3>
<div class="row"><label>name <input id="bname" size="24"></label><input type="hidden" id="bid"></div>
<div id="legs"></div>
<div class="row"><button class="ghost" onclick="addLeg()">+ leg</button><button onclick="saveBasket()">Save</button><button class="ghost" onclick="newBasket()">Clear</button></div>
<div id="bmsg"></div></div>
<div class="card"><h3>Preview / place</h3><div id="bpv" class="muted">Choose a basket above.</div></div>
<div class="card"><h3>Stock SIP plans</h3><div class="muted">Recurring market buys, executed by the scheduler at 09:30 on market days -- in PAPER only (a real-money SIP needs your explicit authorization). One execution per due date, retried up to 3 times.</div>
<div class="row" style="margin-top:8px"><label>symbol <input id="ssym" size="10"></label><label>amount ₹ <input id="samt" size="8"></label><label>or qty <input id="sqty" size="5"></label>
<label>frequency <select id="sfreq"><option>MONTHLY</option><option>WEEKLY</option></select></label><label>day <input id="sday" size="3" value="1" title="1-28 for monthly, 0=Mon..4=Fri for weekly"></label>
<label>end <input id="send" size="10" placeholder="YYYY-MM-DD"></label><button onclick="addSip()">Add plan</button><button class="ghost" onclick="runSip()">Run due now</button></div>
<div id="smsg"></div><div id="slist"></div></div>
</div>
<script>
const TOKEN=__TOKEN__;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=(v,d=2)=>v==null?'—':Number(v).toLocaleString('en-IN',{maximumFractionDigits:d});
async function j(u,o){const r=await fetch(u,o);let t={};try{t=await r.json()}catch(e){}if(!r.ok)throw new Error(t.error||t.detail||r.status);return t}
const post=(u,b)=>j(u,{method:'POST',headers:{'Content-Type':'application/json','X-ATIP-Token':TOKEN},body:JSON.stringify(b||{})});
const table=(h,rows)=>`<table><thead><tr>${h.map(x=>`<th>${x}</th>`).join('')}</tr></thead><tbody>${rows.join('')||`<tr><td colspan=${h.length} class="muted">none</td></tr>`}</tbody></table>`;
const say=(id,t,bad)=>{document.getElementById(id).innerHTML=`<div class="${bad?'bad':'ok'}" style="margin-top:6px">${esc(t)}</div>`};
function legRow(l){l=l||{side:'BUY',order_type:'MARKET',product_type:'CNC'};return `<div class="row leg"><input class="ls" size="10" placeholder="SYMBOL" value="${esc(l.symbol||'')}">
<select class="lside"><option${l.side==='BUY'?' selected':''}>BUY</option><option${l.side==='SELL'?' selected':''}>SELL</option></select>
<input class="lq" size="5" placeholder="qty" value="${esc(l.quantity||'')}"><select class="lot"><option${l.order_type==='MARKET'?' selected':''}>MARKET</option><option${l.order_type==='LIMIT'?' selected':''}>LIMIT</option></select>
<input class="lp" size="7" placeholder="price" value="${l.price?esc(l.price):''}"><select class="lprod"><option${l.product_type==='CNC'?' selected':''}>CNC</option><option${l.product_type==='INTRADAY'?' selected':''}>INTRADAY</option></select>
<button class="ghost" onclick="this.parentNode.remove()">✕</button></div>`}
function addLeg(l){document.getElementById('legs').insertAdjacentHTML('beforeend',legRow(l))}
function newBasket(){document.getElementById('bid').value='';document.getElementById('bname').value='';document.getElementById('legs').innerHTML='';document.getElementById('bedit_t').textContent='New basket';addLeg()}
function legs(){return [...document.querySelectorAll('.leg')].map(r=>({symbol:r.querySelector('.ls').value,side:r.querySelector('.lside').value,quantity:Number(r.querySelector('.lq').value),order_type:r.querySelector('.lot').value,price:Number(r.querySelector('.lp').value||0),product_type:r.querySelector('.lprod').value}))}
async function saveBasket(){const id=document.getElementById('bid').value;try{const b=await post('/api/orders/baskets'+(id?'/'+id:''),{name:document.getElementById('bname').value,legs:legs()});say('bmsg','Saved '+b.basket_id);document.getElementById('bid').value=b.basket_id;loadBaskets()}catch(e){say('bmsg',e.message,1)}}
async function editBasket(id){const b=await j('/api/orders/baskets/'+id);document.getElementById('bid').value=id;document.getElementById('bname').value=b.name;document.getElementById('bedit_t').textContent='Edit '+b.name;document.getElementById('legs').innerHTML='';b.legs.forEach(addLeg)}
async function archiveBasket(id){if(!confirm('Archive this basket?'))return;await post('/api/orders/baskets/'+id+'/archive');loadBaskets()}
function pvTable(p){return `<div>${esc(p.name)} · env <b>${esc(p.env)}</b> · buys ₹${num(p.buy_value)} · sells ₹${num(p.sell_value)} · needs ₹${num(p.net_needed)} of ₹${num(p.available)} available · ${p.ready?'<span class="ok">ready</span>':'<span class="bad">blocked</span>'}</div>`+
 table(['Side','Symbol','Qty','Type','Price','Est. value','Dry run','Problem'],p.legs.map(l=>`<tr><td>${l.side}</td><td>${esc(l.symbol)}</td><td>${l.quantity}</td><td>${l.order_type}</td><td>${l.price?num(l.price):'—'}</td><td>₹${num(l.estimated_value)}</td><td>${esc(l.status)}</td><td class="bad">${esc(l.problem||'')}</td></tr>`))+(p.problems.filter(x=>x.startsWith('funds')).length?'<div class="bad">'+p.problems.filter(x=>x.startsWith('funds')).map(esc).join('<br>')+'</div>':'')}
async function previewBasket(id){const el=document.getElementById('bpv');el.textContent='Dry-running every leg…';try{const r=await post('/api/orders/baskets/'+id+'/preview');el.innerHTML=pvTable(r)+(r.ready?`<button class="red" onclick="placeBasket('${id}')">Place basket (${esc(r.env)})</button>`:'')}catch(e){el.innerHTML='<span class="bad">'+esc(e.message)+'</span>'}}
async function placeBasket(id){if(!confirm('Place every leg of this basket now?'))return;const el=document.getElementById('bpv');try{const r=await post('/api/orders/baskets/'+id+'/execute',{confirm:true});el.innerHTML=`<div>Run ${esc(r.run_id)}: <b>${esc(r.status)}</b></div>`+table(['Side','Symbol','Qty','Status','Order','Reason'],r.placed.map(x=>`<tr><td>${x.side}</td><td>${esc(x.symbol)}</td><td>${x.quantity}</td><td>${esc(x.status)}</td><td>${esc(x.order_id||'')}</td><td>${esc(x.reason||'')}</td></tr>`))+(r.status==='BLOCKED'?pvTable(r.preview):'');loadBaskets()}catch(e){el.innerHTML='<span class="bad">'+esc(e.message)+'</span>'}}
async function loadBaskets(){const B=await j('/api/orders/baskets');document.getElementById('blist').innerHTML=table(['Basket','Legs','Symbols','Updated',''],B.map(b=>`<tr><td>${esc(b.name)}</td><td>${b.legs}</td><td>${esc(b.symbols.join(', '))}</td><td>${esc(String(b.updated_at).slice(0,16))}</td><td><button onclick="previewBasket('${b.basket_id}')">Preview</button><button class="ghost" onclick="editBasket('${b.basket_id}')">Edit</button><button class="ghost" onclick="archiveBasket('${b.basket_id}')">Archive</button></td></tr>`))}
async function addSip(){const g=x=>document.getElementById(x).value;const b={symbol:g('ssym'),frequency:g('sfreq'),day:Number(g('sday'))};if(g('samt'))b.amount=Number(g('samt'));if(g('sqty'))b.quantity=Number(g('sqty'));if(g('send'))b.end_date=g('send');
 try{const p=await post('/api/orders/sip',b);say('smsg','Plan '+p.plan_id+' -- first due '+p.next_due);loadSips()}catch(e){say('smsg',e.message,1)}}
async function sipStatus(id,st){try{await post('/api/orders/sip/'+id+'/status',{status:st});loadSips()}catch(e){say('smsg',e.message,1)}}
async function runSip(){try{const r=await post('/api/orders/sip/run');say('smsg',`${r.status}: ${r.due||0} due, ${r.rows||0} placed`+(r.error?' -- '+r.error:''),r.status==='FAILED');loadSips()}catch(e){say('smsg',e.message,1)}}
async function loadSips(){const S=await j('/api/orders/sip');document.getElementById('slist').innerHTML=table(['Symbol','Amount / qty','Frequency','Next due','Last run','Status',''],S.map(p=>`<tr><td>${esc(p.symbol)}</td><td>${p.amount?'₹'+num(p.amount,0):p.quantity+' sh'}</td><td>${p.frequency} (${p.frequency==='MONTHLY'?'day '+p.day:['Mon','Tue','Wed','Thu','Fri'][p.day]})</td><td>${esc(p.next_due||'')}</td><td>${esc(p.last_run_date||'—')}</td><td>${esc(p.status)}</td><td>${p.status==='ACTIVE'?`<button class="ghost" onclick="sipStatus('${p.plan_id}','PAUSED')">Pause</button>`:p.status==='PAUSED'?`<button class="ghost" onclick="sipStatus('${p.plan_id}','ACTIVE')">Resume</button>`:''}${p.status!=='ENDED'?`<button class="ghost" onclick="sipStatus('${p.plan_id}','ENDED')">End</button>`:''}</td></tr>`))}
j('/api/orders/broker-status').then(s=>{document.getElementById('env').textContent='broker_env: '+(s.broker_env||'?')+(s.is_live?' (LIVE: real orders)':'')}).catch(()=>{});
newBasket();loadBaskets();loadSips();
</script></body></html>"""


def render(token: str) -> str:
    return PAGE.replace("__TOKEN__", json.dumps(token))
