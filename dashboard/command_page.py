"""
The /command page (UXR-01): the Command Center from the UX redesign.

Reads GET /api/command/summary every 15 seconds; writes only through the guarded
/api/risk/halt, /api/risk/resume, /api/orders/{id}/confirm|reject and /api/sessions/* routes,
with the dashboard token injected the same way as the other pages (dashboard/security.py).

Design rules carried from the redesign:
  * the kill switch is on screen at all times and asks once, with the consequences spelled out
  * EOD signals and live prices are labelled as such; a stale live feed puts up a banner
  * gains/losses carry an arrow and a sign, never colour alone
  * AUTO needs a second, explicit step, and the page says plainly that session settings are
    not yet read by the trigger engine (orders/sessions.py)
  * Indian number formatting (en-IN), dark by default, light when the OS asks for it
"""

import json

PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ATIP Command Center</title>
<style>
:root{--bg:#0B1220;--panel:#111A2C;--panel2:#17243B;--line:#22304A;--text:#E6EBF5;--muted:#9AA8C1;--soft:#C9D3E6;
 --accent:#38BDF8;--good:#6EE7B7;--goodbg:#0E2A22;--bad:#FCA5A5;--badbg:#3B1114;--warn:#FDE68A;--warnbg:#2A2410;
 --auto:#FDBA74;--autobg:#3A1F0A;--btn:#2563EB;--kill:#DC2626}
@media (prefers-color-scheme: light){:root{--bg:#F5F7FB;--panel:#FFFFFF;--panel2:#EAF1FB;--line:#D5DDEA;--text:#0F172A;--muted:#4B5873;--soft:#334155;
 --accent:#0369A1;--good:#047857;--goodbg:#E3F6EE;--bad:#B91C1C;--badbg:#FDE8E8;--warn:#854D0E;--warnbg:#FEF6DA;--auto:#9A3412;--autobg:#FFEEDD}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.num{font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace;font-variant-numeric:tabular-nums}
header{display:flex;flex-wrap:wrap;align-items:center;gap:10px;padding:10px 16px;background:var(--panel);border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:10px;margin-right:auto;font-weight:700}
.logo{width:30px;height:30px;border-radius:8px;background:var(--accent);color:var(--bg);display:grid;place-items:center;font-size:13px}
.pill{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;font-size:12px;border:1px solid var(--line);color:var(--soft)}
.pill.ok{background:var(--goodbg);color:var(--good);border-color:transparent;font-weight:600}
.pill.warn{background:var(--warnbg);color:var(--warn);border-color:transparent;font-weight:600}
.pill.bad{background:var(--badbg);color:var(--bad);border-color:transparent;font-weight:600}
button{font:inherit;cursor:pointer;min-height:44px;padding:0 14px;border-radius:8px;border:1px solid var(--line);background:transparent;color:var(--text)}
button:focus-visible,a:focus-visible,input:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
button.primary{background:var(--btn);border-color:var(--btn);color:#fff;font-weight:600}
button.kill{background:var(--kill);border-color:var(--kill);color:#fff;font-weight:700}
button.halted{background:var(--badbg);border-color:var(--bad);color:var(--bad);font-weight:700}
button:disabled{opacity:.5;cursor:not-allowed}
.banner{padding:9px 16px;font-size:13px;font-weight:600}
.banner.bad{background:#7F1D1D;color:#FEE2E2}.banner.warn{background:#78350F;color:#FEF3C7}.banner.info{background:var(--panel2);color:var(--soft);font-weight:400}
.wrap{display:flex;flex-wrap:wrap}
nav{flex:1 1 200px;max-width:220px;padding:14px 10px;border-right:1px solid var(--line);display:flex;flex-direction:column;gap:2px}
nav a{padding:9px 12px;border-radius:8px;color:var(--soft)}nav a.on{background:var(--panel2);color:var(--text);font-weight:600}
nav .more{margin-top:12px;font-size:12px;color:var(--muted);padding:0 12px}
main{flex:999 1 560px;min-width:0;padding:18px;display:flex;flex-direction:column;gap:14px}
h1{margin:0;font-size:21px}h2{margin:0 0 8px;font-size:15px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card.attn{border-color:#FBBF24}
.grid3{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
.lbl{font-size:12px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.big{font-size:20px;font-weight:600;margin-top:4px}
.meter{height:8px;border-radius:4px;background:var(--line);margin-top:10px;overflow:hidden}.meter>div{height:8px}
.sub{font-size:12px;color:var(--muted);margin-top:6px}
.row2{display:flex;flex-wrap:wrap;gap:14px}.row2>.card{flex:1 1 380px;min-width:0}
table{width:100%;border-collapse:collapse;font-size:13px}th{color:var(--muted);font-weight:500;text-align:left;padding:6px 4px}
td{padding:8px 4px;border-top:1px solid var(--line)}.scroll{overflow-x:auto}
.sig{padding:2px 8px;border-radius:999px;font-size:12px;font-weight:600}.sig.BUY{background:var(--goodbg);color:var(--good)}.sig.SELL{background:var(--badbg);color:var(--bad)}
.up{color:var(--good)}.down{color:var(--bad)}.muted{color:var(--muted)}
.item{display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:10px 0;border-top:1px solid var(--line)}.item:first-of-type{border-top:none}
.item .grow{flex:1 1 220px;min-width:0}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px}
.tile{text-align:left;display:flex;flex-direction:column;gap:3px;padding:10px 12px;min-height:84px;background:var(--bg);border:1px solid var(--line);border-radius:8px;color:var(--text)}
.tile[aria-pressed=true]{border:2px solid var(--accent)}
.mode{font-size:12px;padding:1px 8px;border-radius:999px;background:var(--line);color:var(--soft)}
.mode.CONFIRM{background:#0C2D48;color:#7DD3FC}.mode.AUTO{background:var(--autobg);color:var(--auto);font-weight:700}
.panel{margin-top:12px;padding:14px;border:1px solid var(--line);border-radius:10px;background:var(--bg);display:none}.panel.open{display:block}
.seg{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:8px 0}
.seg button{display:flex;flex-direction:column;align-items:flex-start;padding:8px 10px;min-height:60px;text-align:left}
.seg button[aria-checked=true]{border:2px solid var(--accent);background:var(--panel2)}.seg button.AUTO[aria-checked=true]{border-color:#FB923C}
.autobox{border:1px solid #FB923C;background:var(--autobg);border-radius:10px;padding:12px;margin:8px 0;display:none;color:var(--auto)}.autobox.open{display:block}
.autobox label{display:flex;gap:10px;align-items:flex-start;margin:8px 0;cursor:pointer}.autobox input{width:20px;height:20px;margin:0}
.modal{position:fixed;inset:0;background:rgba(3,6,14,.78);display:none;align-items:center;justify-content:center;padding:16px;z-index:50}.modal.open{display:flex}
.dialog{width:460px;max-width:100%;background:var(--panel);border:1px solid var(--bad);border-radius:12px;padding:20px;display:flex;flex-direction:column;gap:12px}
.dialog input{min-height:42px;padding:0 10px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--text);font:inherit;width:100%}
.actions{display:flex;gap:10px;justify-content:flex-end;flex-wrap:wrap}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
.disc{font-size:12px;color:var(--muted);padding:4px 2px}
@media (max-width:720px){nav{max-width:none;flex:1 1 100%;flex-direction:row;overflow-x:auto;border-right:none;border-bottom:1px solid var(--line);padding:6px 8px}nav a{white-space:nowrap}nav .more{display:none}main{padding:14px}}
</style></head><body>
<header>
 <div class="brand"><div class="logo" aria-hidden="true">AT</div><div>ATIP <span class="muted" style="font-weight:400">Command Center</span></div></div>
 <span id="pMarket" class="pill">Market …</span>
 <span id="pFeed" class="pill">Feed …</span>
 <span id="pSig" class="pill">Signals …</span>
 <span id="pEnv" class="pill">…</span>
 <button id="killBtn" class="kill" type="button">Halt all trading</button>
</header>
<div id="haltBanner" class="banner bad" hidden></div>
<div id="feedBanner" class="banner warn" role="alert" hidden></div>
<div class="wrap">
<nav aria-label="Main">
 <a class="on" href="/command" aria-current="page">Command Center</a>
 <a href="/">Signals &amp; scores</a>
 <a href="/signals">Technical signals</a>
 <a href="/trading">Trading</a>
 <a href="/execution-lab">Execution</a>
 <a href="/options-builder">Options builder</a>
 <a href="/backtests">Backtests &amp; journal</a>
 <a href="/brokers">Brokers</a>
 <div class="more">More: <a href="/quant">Quant</a> · <a href="/ml">ML</a> · <a href="/wealth">Wealth</a> · <a href="/compliance">Compliance</a> · <a href="/m">Mobile</a></div>
</nav>
<main>
 <div><h1>Command Center</h1><div id="headline" class="muted" aria-live="polite">Loading…</div></div>

 <section class="grid3" aria-label="Risk budget">
  <div class="card"><div class="lbl">Daily loss used</div><div id="lossV" class="big num">—</div><div class="meter"><div id="lossM" style="width:0;background:#FBBF24"></div></div><div id="lossS" class="sub"></div></div>
  <div class="card"><div class="lbl">Orders placed today</div><div id="ordV" class="big num">—</div><div class="meter"><div id="ordM" style="width:0;background:var(--accent)"></div></div><div id="ordS" class="sub"></div></div>
  <div class="card"><div class="lbl">Day P&amp;L</div><div id="pnlV" class="big num">—</div><div id="pnlS" class="sub"></div></div>
 </section>

 <div class="row2">
  <section id="pendCard" class="card" aria-label="Waiting for you"><h2 id="pendH">Waiting for you</h2><div id="pend"></div></section>
  <section class="card" aria-label="Top signals">
   <div style="display:flex;justify-content:space-between;gap:8px;align-items:baseline"><h2>Top signals</h2><a href="/">All scores →</a></div>
   <div id="sigNote" class="sub" style="margin:0 0 6px"></div>
   <div class="scroll"><table><thead><tr><th>Stock</th><th>Signal</th><th>ATIP</th><th>Signal ₹</th><th>Live ₹</th><th>Gap</th></tr></thead><tbody id="sigs"></tbody></table></div>
  </section>
 </div>

 <section class="card" aria-label="Sessions">
  <div style="display:flex;justify-content:space-between;gap:8px;align-items:baseline"><h2>Sessions</h2><span id="sesSum" class="muted" style="font-size:13px"></span></div>
  <div class="banner info" style="border-radius:8px;margin-bottom:10px">Session mode and pause are recorded and logged, but the trigger engine does not read them yet: rules still follow their own confirmation setting. <b>Halt all trading</b> is the control that stops orders.</div>
  <div id="tiles" class="tiles"></div>
  <div id="sesPanel" class="panel" aria-live="polite">
   <h2 id="spName"></h2><div id="spMeta" class="sub" style="margin:0 0 6px"></div>
   <div class="lbl" style="margin-top:8px">How orders go out</div>
   <div class="seg" role="radiogroup" aria-label="Execution mode" id="spModes"></div>
   <div id="autoBox" class="autobox">
    <b>Turn on AUTO for <span id="abName"></span>?</b>
    <div style="font-size:13px;margin-top:6px">ATIP would send orders for this session without asking you, within its limits. Every order still passes the halt, funds, circuit and loss checks.</div>
    <label><input type="checkbox" id="abAck"> I understand orders can go out without asking me, and I can halt everything from any screen.</label>
    <div class="actions"><button type="button" id="abCancel">Keep current mode</button><button type="button" id="abGo" class="primary" disabled>Turn on AUTO</button></div>
   </div>
   <div class="actions" style="justify-content:flex-start"><button type="button" id="spPause"></button></div>
   <div id="spMsg" class="sub" role="status"></div>
  </div>
 </section>

 <section class="card" aria-label="Recent order activity"><h2>Recent order activity</h2><div id="recent"></div></section>
 <p class="disc">Personal use only. Model outputs, not advice. Signals are computed at the close; live prices are shown for context.</p>
</main></div>

<div id="haltModal" class="modal" aria-hidden="true">
 <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="hmT">
  <h2 id="hmT" style="font-size:18px">Halt all trading?</h2>
  <p style="margin:0;color:var(--soft)">No order will be sent — PAPER or LIVE — until you resume. Running sessions are marked paused. Open positions stay open; exit them from Trading if you want out.</p>
  <label class="muted" style="font-size:13px">Reason (saved with the halt)<input id="hmReason" value="Manual halt from Command Center"></label>
  <div class="actions"><button type="button" id="hmCancel">Keep trading</button><button type="button" id="hmGo" class="kill">Halt now</button></div>
 </div>
</div>
<div id="live" class="sr" aria-live="assertive"></div>

<script>
const TOKEN=__TOKEN__;
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const inr=(v,d=0)=>v==null||isNaN(v)?'—':'₹'+Number(v).toLocaleString('en-IN',{maximumFractionDigits:d,minimumFractionDigits:d});
const signed=v=>v==null?'—':(v>0?'▲ +':v<0?'▼ −':'')+inr(Math.abs(v));
async function j(u,o){const r=await fetch(u,o);let t={};try{t=await r.json()}catch(e){}if(!r.ok)throw new Error(t.error||t.detail||('HTTP '+r.status));return t}
const post=(u,b)=>j(u,{method:'POST',headers:{'Content-Type':'application/json','X-ATIP-Token':TOKEN},body:JSON.stringify(b||{})});
const say=m=>{$('live').textContent=m};
let S=null, sel=null, lastFocus=null;

function render(d){
 S=d; const r=d.risk||{}, f=d.feed||{}, b=d.broker||{};
 // header pills
 $('pMarket').className='pill '+(d.market_open?'ok':''); $('pMarket').textContent=d.market_open?'Market open':'Market closed';
 $('pFeed').className='pill '+(f.stale?'warn':''); $('pFeed').textContent=f.age_seconds==null?'Live feed: no ticks':('Live feed · '+(f.age_seconds<120?f.age_seconds+' s':Math.round(f.age_seconds/60)+' min')+' ago');
 $('pSig').textContent=d.signals&&d.signals.date?('Signals: EOD '+d.signals.date):'Signals: none yet';
 $('pEnv').className='pill '+(b.is_live?'bad':'warn'); $('pEnv').textContent=b.is_live?'LIVE · real money':(b.broker_env||'PAPER');
 // halt
 const kb=$('killBtn');
 if(r.halted){kb.className='halted';kb.textContent='Halted · Resume…';$('haltBanner').hidden=false;$('haltBanner').textContent='Trading halted — '+(r.halt_reason||'')+'. Nothing is sent until you resume. Previews and dry runs still work.';}
 else{kb.className='kill';kb.textContent='Halt all trading';$('haltBanner').hidden=true;}
 $('feedBanner').hidden=!f.stale; if(f.stale)$('feedBanner').textContent='Live prices are '+(f.age_seconds==null?'missing':f.age_seconds+' seconds old')+' while the market is open. Treat triggers and the prices below as unreliable until the feed recovers.';
 // risk budget
 const cap=(r.limits||{}).max_daily_loss_value, lost=r.day_pnl!=null?Math.max(0,-r.day_pnl):null;
 $('lossV').innerHTML=cap?(inr(lost)+' <span class="muted" style="font-size:13px">of '+inr(cap)+'</span>'):(lost==null?'—':inr(lost));
 const lp=cap&&lost!=null?Math.min(100,lost/cap*100):0; $('lossM').style.width=lp+'%'; $('lossM').style.background=lp>=80?'#F87171':'#FBBF24';
 $('lossS').textContent=cap?'Limit from config.json risk_limits · new BUYs refused at 100%':'No daily loss limit set — add max_daily_loss_value in risk_limits';
 const oc=(r.limits||{}).max_orders_per_day; $('ordV').innerHTML=(r.orders_today??'—')+(oc?' <span class="muted" style="font-size:13px">of '+oc+'</span>':'');
 $('ordM').style.width=(oc&&r.orders_today!=null?Math.min(100,r.orders_today/oc*100):0)+'%'; $('ordS').textContent=oc?'':'No order-count limit set';
 $('pnlV').className='big num '+(r.day_pnl>0?'up':r.day_pnl<0?'down':''); $('pnlV').textContent=signed(r.day_pnl);
 $('pnlS').textContent=(r.drawdown_pct!=null?'Drawdown '+r.drawdown_pct+'% from peak · ':'')+(b.broker_env||'PAPER')+' book';
 // headline
 const ses=d.sessions||{sessions:[]}; const pend=d.pending||[];
 $('headline').textContent=(r.halted?'Trading is halted. ':'')+ses.running+' of '+ses.sessions.length+' sessions running · '+pend.length+' order'+(pend.length===1?'':'s')+' waiting for you'+(f.stale?' · live feed is stale':'');
 // pending
 $('pendCard').className='card'+(pend.length?' attn':''); $('pendH').textContent='Waiting for you · '+pend.length;
 $('pend').innerHTML=pend.length?pend.map(p=>`<div class="item"><div class="grow"><b>${esc(p.side)} ${esc(p.quantity_value)} ${esc(p.symbol)}</b> <span class="num">@ ${inr(p.trigger_hit_price||p.resolved_trigger_price,2)}</span><div class="sub" style="margin:2px 0 0">${esc(p.role||'ENTRY')} rule · stop ${inr(p.resolved_stoploss_price,2)} · expires ${esc(String(p.confirmation_expires_at||'').slice(11,16))}</div></div><button type="button" data-rej="${esc(p.id)}">Reject</button><button type="button" class="primary" data-conf="${esc(p.id)}">Confirm &amp; send</button></div>`).join(''):'<div class="muted">Nothing needs you right now.</div>';
 // signals
 const sg=d.signals||{rows:[]}; $('sigNote').textContent=sg.date?('Computed at the close of '+sg.date+'. The live price moves; the signal does not.'):'No scores yet — run the post-market pipeline.';
 $('sigs').innerHTML=(sg.rows||[]).map(s=>{const g=s.gap_pct;const gs=g==null?'—':(g>0?'▲ +':g<0?'▼ ':'')+g+'%';const big=g!=null&&Math.abs(g)>=3;
  return `<tr><td><b>${esc(s.symbol)}</b></td><td><span class="sig ${esc(s.signal)}">${esc(s.signal)}</span></td><td class="num">${s.atip_score==null?'—':Math.round(s.atip_score)}</td><td class="num muted">${inr(s.signal_price,2)}</td><td class="num">${inr(s.live_price,2)}</td><td class="num" style="${big?'color:#FBBF24;font-weight:600':''}" title="${big?'Price has moved 3% or more since the signal':''}">${gs}</td></tr>`}).join('')||'<tr><td colspan="6" class="muted">No BUY/SELL signals.</td></tr>';
 // sessions
 $('sesSum').textContent=ses.running+' running · '+ses.auto+' on AUTO';
 $('tiles').innerHTML=ses.sessions.map(t=>`<button type="button" class="tile" data-sid="${esc(t.id)}" aria-pressed="${sel===t.id}"><span style="display:flex;justify-content:space-between;width:100%"><b>${esc(t.id)} ${esc(t.name)}</b><span class="mode ${esc(t.mode)}">${esc(t.mode)}</span></span><span class="sub" style="margin:0">${esc(t.status)}${t.stats.awaiting?' · '+t.stats.awaiting+' waiting':''}</span><span class="sub num" style="margin:0">${t.stats.today} today · ${t.stats.watching} watching · ${t.stats.executed} done</span></button>`).join('');
 if(sel)openSession(sel,false);
 // recent
 const rc=d.recent||[]; $('recent').innerHTML=rc.length?rc.map(o=>`<div class="item"><span class="num muted" style="width:52px">${esc(String(o.updated_at||'').slice(11,16))}</span><span class="grow">${esc(o.role||'')} ${esc(o.side)} ${esc(o.quantity_value)} <b>${esc(o.symbol)}</b> · trigger ${inr(o.resolved_trigger_price,2)}${o.execution_price?' · filled '+inr(o.execution_price,2):''}${o.execution_error?' · <span class="down">'+esc(o.execution_error)+'</span>':''}</span><span class="num muted" style="font-size:12px">${esc(o.status)}${o.dhan_order_id?' · '+esc(o.dhan_order_id):''}</span></div>`).join(''):'<div class="muted">No order rules yet.</div>';
}

function openSession(sid,focus=true){
 sel=sid; const t=(S.sessions.sessions||[]).find(x=>x.id===sid); if(!t)return;
 document.querySelectorAll('.tile').forEach(b=>b.setAttribute('aria-pressed',b.dataset.sid===sid));
 $('sesPanel').classList.add('open'); $('spName').textContent=t.id+' · '+t.name;
 $('spMeta').textContent=(t.strategy||'')+' · max '+t.max_trades+' trades/day · max loss '+inr(t.max_loss_value)+' · capital '+inr(t.capital_cap);
 const desc={MANUAL:'You place every order',CONFIRM:'ATIP asks before sending',AUTO:'Sends within limits'};
 $('spModes').innerHTML=['MANUAL','CONFIRM','AUTO'].map(m=>`<button type="button" role="radio" class="${m}" aria-checked="${t.mode===m}" data-mode="${m}"><b>${m[0]+m.slice(1).toLowerCase()}</b><span class="sub" style="margin:0">${desc[m]}</span></button>`).join('');
 $('abName').textContent=t.id; const paused=t.status!=='RUNNING';
 $('spPause').textContent=paused?'Mark session running':'Pause session'; $('spPause').dataset.to=paused?'RUNNING':'PAUSED';
 if(focus)$('spName').focus?.();
}

async function load(){try{render(await j('/api/command/summary'))}catch(e){$('headline').textContent='Could not load: '+e.message}}

// kill switch
$('killBtn').onclick=async()=>{
 if(S&&S.risk&&S.risk.halted){
  if(!confirm('Lift the halt? Orders can be sent again. Sessions stay paused until you resume each one.'))return;
  try{await post('/api/risk/resume',{confirm:'RESUME'});say('Trading resumed');}catch(e){alert(e.message)} load(); return;}
 lastFocus=document.activeElement; $('haltModal').classList.add('open'); $('haltModal').setAttribute('aria-hidden','false'); $('hmGo').focus();
};
function closeModal(){$('haltModal').classList.remove('open');$('haltModal').setAttribute('aria-hidden','true');(lastFocus||$('killBtn')).focus()}
$('hmCancel').onclick=closeModal;
$('haltModal').addEventListener('keydown',e=>{if(e.key==='Escape')closeModal()});
$('hmGo').onclick=async()=>{try{const r=await post('/api/risk/halt',{reason:$('hmReason').value});say('Trading halted. '+r.sessions_paused+' sessions paused.');}catch(e){alert(e.message)} closeModal(); load();};

// pending confirm / reject (existing guarded routes; confirm re-validates the price)
$('pend').addEventListener('click',async e=>{
 const c=e.target.closest('[data-conf]'), r=e.target.closest('[data-rej]');
 try{
  if(c){const res=await fetch('/api/orders/'+encodeURIComponent(c.dataset.conf)+'/confirm',{method:'POST',headers:{'X-ATIP-Token':TOKEN}});const t=await res.json();
   if(res.status===409&&t.slippage){if(confirm(t.error+'\n\nPlace anyway at the current price?')){await post('/api/orders/'+encodeURIComponent(c.dataset.conf)+'/confirm?force=true')}}
   else if(!res.ok)throw new Error(t.error||res.status); say('Order confirmed');}
  if(r){await post('/api/orders/'+encodeURIComponent(r.dataset.rej)+'/reject');say('Order rejected');}
 }catch(err){alert(err.message)} load();
});

// sessions
$('tiles').addEventListener('click',e=>{const t=e.target.closest('[data-sid]');if(t){$('autoBox').classList.remove('open');openSession(t.dataset.sid)}});
$('spModes').addEventListener('click',async e=>{
 const b=e.target.closest('[data-mode]'); if(!b||!sel)return; const m=b.dataset.mode;
 const t=S.sessions.sessions.find(x=>x.id===sel); if(t.mode===m)return;
 if(m==='AUTO'){$('abAck').checked=false;$('abGo').disabled=true;$('autoBox').classList.add('open');$('abAck').focus();return}
 try{await post('/api/sessions/'+sel+'/mode',{mode:m});$('spMsg').textContent=sel+' set to '+m+'.';}catch(err){$('spMsg').textContent=err.message} load();
});
$('abAck').onchange=()=>{$('abGo').disabled=!$('abAck').checked};
$('abCancel').onclick=()=>$('autoBox').classList.remove('open');
$('abGo').onclick=async()=>{try{await post('/api/sessions/'+sel+'/mode',{mode:'AUTO',ack:true});$('spMsg').textContent=sel+' set to AUTO (recorded; engine wiring pending).';$('autoBox').classList.remove('open')}catch(err){$('spMsg').textContent=err.message} load();};
$('spPause').onclick=async()=>{try{await post('/api/sessions/'+sel+'/status',{status:$('spPause').dataset.to,reason:'from Command Center'});$('spMsg').textContent=sel+' marked '+$('spPause').dataset.to.toLowerCase()+'.'}catch(err){$('spMsg').textContent=err.message} load();};

load(); setInterval(load,15000);
</script></body></html>"""


def render() -> str:
    from dashboard.security import page_token      # the credential this request may carry (per-session in enterprise mode)
    return PAGE.replace("__TOKEN__", json.dumps(page_token()))
