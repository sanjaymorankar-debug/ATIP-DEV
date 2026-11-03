"""
Research pages: /research (equity research reports with the fundamental scorecard, the DVM view and the
earnings surprise card, ratings, hit rate and the scorecard's record, 7-year history coverage),
/screener (fundamental + technical stock screener with presets and saved screens), /signals
(end-of-day technical signals and their track record), /market-pulse (global cues, GIFT Nifty, the
event calendar, FII flows and positioning, order-book pressure, your pending orders) and /options-builder (multi-leg
strategy builder with a live payoff chart, net Greeks and a suitability tier). All read the API in
dashboard/w39_routes.py; none of them places an order.

Theming (UX audit H3 / H6, 3 Nov 2026): every colour is a CSS variable that points at the shell's tokens
(--ax-bg, --ax-surface, --ax-text, --ax-muted, --ax-brand, --ax-pos / --ax-neg / --ax-warn and their -bg,
--ax-series-1 / -2, --ax-grid), with the old dark hex as the fallback, so the pages follow the shell's light and
dark themes and still render on their own (?shell=0). Charts draw with classes (.sv-*), never hex attributes.
Payoff / gate charts: series 1 (expiry, Nifty) solid, series 2 (target date, 200-DMA) dashed, so identity never
rests on colour; DVM meters: series 1 on the brand-soft track, the low / high thresholds marked in muted ink.

Accessibility (M1 / M6 / M7): every field has a visible <label for>; every table sits in its own focusable,
labelled scroll region (role=region, tabindex=0) so a phone never scrolls sideways; loading (.ax-skel shimmer),
empty (why, and what to do) and error states are distinct.
"""

import html
import json

_STYLE = """
:root{--bg:var(--ax-bg,#0f172a);--panel:var(--ax-surface,#1e293b);--panel2:var(--ax-surface-2,#0f172a);--line:var(--ax-border,#334155);
--line2:var(--ax-border-soft,#33415599);--text:var(--ax-text,#e2e8f0);--muted:var(--ax-muted,#94a3b8);--accent:var(--ax-brand,#38bdf8);
--good:var(--ax-pos,#34d399);--bad:var(--ax-neg,#f87171);--warn:var(--ax-warn,#f59e0b);--grid:var(--ax-grid,#334155)}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:var(--ax-font,system-ui,sans-serif);background:var(--bg);color:var(--text);font-size:13px;line-height:1.45}
.top{background:var(--panel);padding:10px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;
 align-items:center;flex-wrap:wrap;gap:6px}
.top .brand{color:var(--accent)}.top h1{display:inline;font-size:15px;font-weight:650;color:var(--text);margin-left:6px}
.top a{color:var(--accent);text-decoration:none;margin-left:12px;display:inline-block;padding:4px 0;min-height:24px}.top a:hover{text-decoration:underline}
.wrap{padding:14px 16px;max-width:1300px;min-width:0}
.wrap h2{font-size:14px;font-weight:650;color:var(--accent);margin-bottom:8px}
.wrap h3{font-size:13px;font-weight:650;color:var(--accent);margin:10px 0 6px}
.wrap a{color:var(--accent)}
.tabs{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:10px}
.wrap .tab{padding:6px 12px;border-radius:6px;background:var(--panel);border:1px solid var(--line);cursor:pointer;color:var(--muted);
 font-weight:600;margin:0}
.wrap .tab.on{background:var(--ax-brand,#2563eb);border-color:var(--ax-brand,#2563eb);color:var(--ax-brand-ink,#fff)}
.pane{display:none}.pane.on{display:block}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin-bottom:10px;min-width:0;
 overflow-wrap:break-word}
.muted{color:var(--muted)}.ok{color:var(--good)}.bad{color:var(--bad)}.note{color:var(--warn)}
.wrap table{width:100%;border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}
.wrap th,.wrap td{padding:6px 8px;border-bottom:1px solid var(--line2);text-align:left;vertical-align:top;overflow-wrap:normal}
.wrap th{color:var(--muted);font-weight:600}
:is(.wrap,.top) :is(input,select,textarea){background:var(--ax-surface,#0f172a);color:var(--text);border:1px solid var(--line);
 border-radius:6px;padding:5px 8px;font:inherit;font-size:13px;min-height:32px;max-width:100%}
:is(.wrap,.top) input:disabled{opacity:.6;cursor:not-allowed}
:is(.wrap,.top) button{background:var(--ax-brand,#2563eb);color:var(--ax-brand-ink,#fff);border:1px solid var(--ax-brand,#2563eb);
 border-radius:6px;padding:5px 12px;cursor:pointer;font:inherit;font-size:13px;font-weight:600;margin:2px;min-height:32px}
.wrap button.ghost,.wrap button.chip{background:var(--panel);color:var(--text);border-color:var(--line);font-weight:500}
.wrap button.chip[aria-pressed="true"]{border-color:var(--ax-brand,#2563eb);box-shadow:inset 0 0 0 1px var(--ax-brand,#2563eb);font-weight:650}
.wrap button.thb{background:none;border:0;color:inherit;font:inherit;font-weight:600;padding:0;margin:0;min-height:0;text-align:left}
:is(.wrap,.top) :focus-visible{outline:2px solid var(--ax-focus,#93c5fd);outline-offset:2px}
.wrap label{font-size:12px;color:var(--muted);font-weight:600}.wrap label.chk{display:inline-flex;gap:6px;align-items:center}
.wrap label.chk input{min-height:0}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600;white-space:nowrap}
.BUY,.PASS{background:var(--ax-pos-bg,#064e3b);color:var(--ax-pos,#a7f3d0)}.ADD{background:var(--ax-info-bg,#134e4a);color:var(--ax-info,#99f6e4)}
.REDUCE,.WARN{background:var(--ax-warn-bg,#78350f);color:var(--ax-warn,#fde68a)}.SELL,.FAIL{background:var(--ax-neg-bg,#7f1d1d);color:var(--ax-neg,#fecaca)}
.NOT_RATED,.NA{background:var(--ax-surface-3,#334155);color:var(--ax-text-2,#cbd5e1)}
.scroll{max-height:460px;overflow:auto}
.tw{overflow-x:auto;max-width:100%}
.tw th:first-child,.tw td:first-child{position:sticky;left:0;background:var(--panel);z-index:1}
.empty,.emptyst{color:var(--muted)}.emptyst ol{margin:6px 0 0 20px}.emptyst li{margin:4px 0}
.errst{background:var(--ax-neg-bg,#7f1d1d);color:var(--ax-neg,#fecaca);border-radius:6px;padding:8px 10px;margin:4px 0}
.errst button{margin-left:6px}
.skw{padding:4px 0}.skw .ax-skel{height:14px;margin:8px 0}
.sr{position:absolute!important;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.frm{display:flex;flex-wrap:wrap;gap:8px 10px;align-items:flex-end;margin-top:8px}
.fld{display:flex;flex-direction:column;gap:3px;min-width:0;max-width:100%}.fld.grow{flex:1 1 220px}.fld.grow input{width:100%}
.w-s{width:84px}.w-m{width:150px}.w-l{width:240px}
.hint{font-size:12.5px;margin-top:4px}
"""

_EXTRA = """
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px}
.stat{background:var(--ax-surface-2,#0f172a);border:1px solid var(--line2);border-radius:6px;padding:8px 10px;min-width:0}
.stat .k{color:var(--muted);font-size:12px}.stat .v{font-size:17px;font-weight:600;margin-top:2px;font-variant-numeric:tabular-nums}
.stat .v.sm{font-size:14px}
ul.b{margin:4px 0 0 18px}ul.b li{margin:3px 0}.two{display:grid;grid-template-columns:1fr 1fr;gap:10px}
@media(max-width:800px){.two{grid-template-columns:1fr}}
.two>*{min-width:0}.sx{min-width:0}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;vertical-align:middle;margin-right:5px}
.sw.g-OPEN{background:var(--ax-pos,#0ca30c)}.sw.g-CAUTION{background:var(--ax-warn,#fab219)}.sw.g-CLOSED{background:var(--ax-neg,#d03b3b)}
.sw.pa{background:var(--ax-pos-bg,#064e3b);border:1px solid var(--good)}.sw.la{background:var(--ax-neg-bg,#7f1d1d);border:1px solid var(--bad)}
#chart,#gchart{overflow:hidden}.legend{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12px;color:var(--muted);margin:4px 0}
.legend i{display:inline-block;width:18px;height:0;border-top:2px solid;vertical-align:middle;margin-right:5px}
.legend i.s1{border-color:var(--ax-series-1,#3987e5)}.legend i.s2{border-color:var(--ax-series-2,#d95926);border-top-style:dashed}
.legend i.sp{border-color:var(--muted);border-top-style:dotted}
.axes{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:8px}
@media(max-width:1000px){.axes{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:640px){.axes{grid-template-columns:1fr}}
.ax{background:var(--ax-surface-2,#0f172a);border:1px solid var(--line2);border-radius:6px;padding:8px 10px;min-width:0}.ax b{font-size:13px}
.ax.fl{display:flex;align-items:center;justify-content:center}
.ck{margin-top:5px;font-size:12.5px;line-height:1.4}.ck .d{color:var(--muted);font-size:12px;margin-left:16px}
.ck i{font-style:normal;display:inline-block;width:14px;font-weight:700}.sub{font-size:12px}
.dvm .v{font-size:20px;font-weight:600}.meter{position:relative;height:8px;background:var(--ax-brand-soft,#184f95);border-radius:4px;margin:6px 0 4px}
.meter>span{display:block;height:100%;background:var(--ax-series-1,#3987e5);border-radius:4px}
.meter>i{position:absolute;top:-3px;width:2px;height:14px;background:var(--muted)}.ck .s{display:inline-block;min-width:28px;font-weight:600}
.track{width:120px;height:8px;background:var(--ax-surface-3,#0f172a);border-radius:4px;position:relative}
.track>span{position:absolute;height:8px;border-radius:4px}.track .up{left:50%;background:var(--ax-series-1,#3987e5)}
.track .dn{right:50%;background:var(--ax-series-2,#d95926)}
#tip{position:fixed;pointer-events:none;background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:6px;
 padding:6px 8px;font-size:12.5px;display:none;z-index:9;box-shadow:0 6px 18px rgba(0,0,0,.18);font-variant-numeric:tabular-nums}
.c-s1{color:var(--ax-series-1,#3987e5)}.c-s2{color:var(--ax-series-2,#d95926)}
.sv-grid{stroke:var(--grid);stroke-width:1}.sv-t{fill:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}
.sv-ink{fill:var(--text);font-size:12px;font-variant-numeric:tabular-nums}.sv-ink .sv-t{font-size:12px}
.sv-zero{stroke:var(--muted);stroke-width:1.25}.sv-spot{stroke:var(--muted);stroke-width:1;stroke-dasharray:2 3}
.sv-s1{fill:none;stroke:var(--ax-series-1,#3987e5);stroke-width:2;stroke-linejoin:round}
.sv-s2{fill:none;stroke:var(--ax-series-2,#d95926);stroke-width:2;stroke-dasharray:6 4}
.sv-pa{fill:var(--ax-pos-bg,#064e3b);opacity:.85}.sv-la{fill:var(--ax-neg-bg,#7f1d1d);opacity:.85}
.sv-be{fill:var(--panel);stroke:var(--text);stroke-width:2}.sv-xh{stroke:var(--text);stroke-width:1}
.sv-d1{fill:var(--ax-series-1,#3987e5);stroke:var(--panel);stroke-width:2}.sv-d2{fill:var(--ax-series-2,#d95926);stroke:var(--panel);stroke-width:2}
.sv-mark{fill:var(--text);stroke:var(--panel);stroke-width:2;paint-order:stroke}
.sv-area{fill:var(--ax-series-1,#3987e5);fill-opacity:.3;stroke:var(--ax-series-1,#3987e5);stroke-width:2}
rect.g-OPEN{fill:var(--ax-pos,#0ca30c)}rect.g-CAUTION{fill:var(--ax-warn,#fab219)}rect.g-CLOSED{fill:var(--ax-neg,#d03b3b)}
.presets{display:flex;flex-wrap:wrap;gap:4px;margin:2px 0 4px}.pgl{width:100%;margin-top:4px;font-size:12px}
.leg{display:grid;grid-template-columns:52px repeat(2,minmax(86px,1fr)) minmax(90px,1fr) minmax(120px,1.2fr) minmax(64px,.6fr)
 minmax(96px,1fr) minmax(120px,1.4fr) auto;gap:6px 8px;align-items:end;padding:8px 0;border-bottom:1px solid var(--line2)}
.leg .fld input,.leg .fld select{width:100%}.legn{font-weight:700;color:var(--muted);align-self:center}
.legi{font-size:12px;color:var(--muted);align-self:center}.legi .src{display:block}.leg .rm{align-self:end;justify-self:end;min-width:44px}
@media(max-width:980px){.leg{grid-template-columns:repeat(3,minmax(0,1fr))}.legn{grid-column:1/-1}}
@media(max-width:560px){.leg{grid-template-columns:repeat(2,minmax(0,1fr))}}
.tierrow{display:flex;gap:6px 10px;align-items:baseline;flex-wrap:wrap;margin:0 0 10px}
.tierb{display:inline-block;padding:2px 10px;border-radius:12px;font-weight:700;font-size:13px}
.t1{background:var(--ax-pos-bg,#064e3b);color:var(--ax-pos,#a7f3d0)}.t2{background:var(--ax-info-bg,#134e4a);color:var(--ax-info,#99f6e4)}
.t3{background:var(--ax-neg-bg,#7f1d1d);color:var(--ax-neg,#fecaca)}
.pay{display:grid;grid-template-columns:minmax(0,2.2fr) minmax(220px,1fr);gap:14px;align-items:start}
@media(max-width:860px){.pay{grid-template-columns:1fr}}.pay>*{min-width:0}
.kvs dt{color:var(--muted);font-size:12px;font-weight:600;margin-top:6px}.kvs dd{margin:1px 0 0;font-variant-numeric:tabular-nums}
#ost{min-height:18px;margin:0 0 6px}
"""

_HEAD = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>__TITLE__</title><style>__STYLE__</style></head><body>
<div class="top"><div><b class="brand">📊 ATIP</b> <h1>__SUB__</h1></div>
<div><a href="/market-pulse">Market pulse</a><a href="/screener">Screener</a><a href="/signals">Signals</a><a href="/research">Research</a><a href="/options-builder">Options builder</a><a href="/">← Dashboard</a></div></div>"""

# what a panel shows while its data loads (the shell's .ax-skel shimmer; the words say it too)
_SK = ('<div class="skw" role="status"><span class="muted">Loading…</span><div class="ax-skel"></div>'
       '<div class="ax-skel" style="width:88%"></div><div class="ax-skel" style="width:94%"></div></div>')

_JS_COMMON = r"""
const TOKEN=__TOKEN__;
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function j(u,o){const r=await fetch(u,o);let t=null;try{t=await r.json()}catch(e){t=null}
 if(!r.ok||t===null){const er=new Error((t&&(t.error||(typeof t.detail==='string'?t.detail:'')))||`${r.status} ${r.statusText||'error'}`);er.status=r.status;throw er}return t}
const post=(u,b)=>j(u,{method:'POST',headers:{'Content-Type':'application/json','X-ATIP-Token':TOKEN},body:JSON.stringify(b||{})});
const EMPTY='Nothing stored for this yet: it fills in once ATIP has collected the data.';
function table(h,rows,o){o=typeof o==='string'?{label:o}:(o||{});
 const lab=o.label||'Table: '+h.map(x=>String(x).replace(/<[^>]*>/g,'').trim()).filter(Boolean).slice(0,3).join(', ');
 const head=o.nohead?'':`<thead><tr>${h.map(x=>`<th scope="col">${x}</th>`).join('')}</tr></thead>`;
 const t=`<table>${head}<tbody>${rows.join('')||`<tr><td colspan="${h.length}" class="empty">${o.empty||EMPTY}</td></tr>`}</tbody></table>`;
 return o.plain?t:`<div class="tw ax-scrollx${o.cls?' '+o.cls:''}" tabindex="0" role="region" aria-label="${esc(lab)}">${t}</div>`}
const skel=(what,k)=>`<div class="skw" role="status"><span class="muted">Loading ${esc(what||'')}…</span>${Array.from({length:k||3},(_,i)=>`<div class="ax-skel" style="width:${[100,88,94,76,82][i%5]}%"></div>`).join('')}</div>`;
const errbox=(e,what,retry)=>`<div class="errst" role="alert"><b>Could not load ${esc(what)}.</b> ${esc(e&&e.message||e)}${retry?` <button type="button" class="ghost" onclick="${retry}">Try again</button>`:''}</div>`;
const SR=t=>`<span class="sr">${t}</span>`;
const pill=s=>`<span class="pill ${esc(s)}">${esc(String(s||'').replace('_',' '))}</span>`;
const n=(v,d=2)=>v==null||isNaN(v)?'—':Number(v).toLocaleString('en-IN',{maximumFractionDigits:d,minimumFractionDigits:0});
const pct=v=>v==null?'—':(v>0?'+':'')+n(v,1)+'%';
const rs=v=>v==null||isNaN(v)?'—':(v<0?'−':'')+'₹'+Math.abs(Number(v)).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});
const gpill=g=>g?`<span class="pill ${g==='OPEN'?'BUY':g==='CAUTION'?'REDUCE':'SELL'}">${g==='OPEN'?'● open':g==='CAUTION'?'◐ caution':'○ closed'}</span>`:'<span class="pill NOT_RATED">unknown</span>';
const apill=a=>a?`<span class="pill ${a==='WITH'?'BUY':a==='MIXED'?'REDUCE':'SELL'}">${a==='WITH'?'with market':a==='MIXED'?'mixed':'against market'}</span>`:'<span class="muted">—</span>';
const stl=s=>String(s||'').replaceAll('_',' ').toLowerCase();
function tabs(list,el){document.getElementById(el).innerHTML=list.map(([id,l])=>`<button type="button" class="tab" role="tab" id="tab-${id}" aria-controls="p-${id}" aria-selected="false" data-id="${id}" onclick="show('${id}')">${l}</button>`).join('')}
function showTab(id){document.querySelectorAll('.tab').forEach(t=>{const on=t.dataset.id===id;t.classList.toggle('on',on);t.setAttribute('aria-selected',on?'true':'false')});
 document.querySelectorAll('.pane').forEach(p=>p.classList.toggle('on',p.id==='p-'+id))}
"""

RESEARCH = _HEAD + r"""
<div class="wrap"><div class="tabs" id="tabs" role="tablist" aria-label="Research views"></div>
<div class="pane" id="p-rep" role="tabpanel" aria-labelledby="tab-rep"><div class="card"><h2>Equity research report</h2>
<div class="muted">Institutional-style report built from ATIP's stored data: DCF with bull / base / bear cases, peer and own-history multiples, a 12-month target and a rating. A model output for your own research, not advice.</div>
<div class="frm"><div class="fld"><label for="sym">Symbol</label><input id="sym" class="w-l" placeholder="e.g. RELIANCE" autocomplete="off" spellcheck="false" onkeydown="if(event.key==='Enter')load()"></div>
<button type="button" onclick="load()">Open</button><button type="button" class="ghost" onclick="load(true)">Rebuild &amp; save</button></div></div>
<div id="rep" aria-live="polite"><div class="card emptyst">Type a symbol such as RELIANCE and press <b>Open</b> to read its stored report; <b>Rebuild &amp; save</b> makes a fresh one from today's data.</div></div></div>
<div class="pane" id="p-rat" role="tabpanel" aria-labelledby="tab-rat"><div class="card"><h2>Latest ratings</h2>
<div class="frm"><div class="fld"><label for="rf">Rating</label><select id="rf" onchange="rat()"><option value="">All ratings</option><option value="BUY">Buy</option><option value="ADD">Add</option><option value="REDUCE">Reduce</option><option value="SELL">Sell</option><option value="NOT_RATED">Not rated</option></select></div>
<button type="button" onclick="runAll()">Rebuild all</button><span id="rao" class="muted" role="status"></span></div>
<div id="rat" class="scroll" tabindex="0" role="region" aria-label="Latest ratings" style="margin-top:8px">__SK__</div></div>
<div class="card"><h2>Hit rate of past calls</h2><div class="muted">A call is a new rating or a target move of more than 5%. HIT = the target was reached within 12 months.</div><div id="hit" style="margin-top:6px">__SK__</div></div>
<div class="card"><h2>Does the scorecard pay?</h2>
<div class="frm"><div class="fld"><label for="sch">Holding period</label><select id="sch" onchange="screc()"><option value="20">20 sessions</option><option value="60" selected>60 sessions</option><option value="120">120 sessions</option><option value="250">250 sessions</option></select></div></div>
<div class="muted" style="margin-top:6px">Return minus the Nifty's after each stored scorecard, by checks passed. One sample per stock per month. A band needs 30 samples before it means anything; the record starts the day the 20:50 IST job first stores scorecards.</div><div id="scr" style="margin-top:6px">__SK__</div></div></div>
<div class="pane" id="p-his" role="tabpanel" aria-labelledby="tab-his"><div class="card"><h2>7-year price history</h2>
<div class="muted">The nightly backfill (22:20 IST) walks each tracked stock back 7 years from Dhan, 40 stocks a night. Daily prices are now kept for 7 years.</div>
<div id="cov" style="margin-top:8px">__SK__</div><button type="button" onclick="backfill()">Run a pass now (10 stocks)</button><span id="bfo" class="muted" role="status"></span></div></div>
</div><div id="tip"></div>
<script>""" + _JS_COMMON + r"""
const TABS=[['rep','Report'],['rat','Ratings &amp; hit rate'],['his','History coverage']];const loaded={};
function show(id){showTab(id);if(!loaded[id]){loaded[id]=1;({rep:()=>{},rat,his})[id]()}}
tabs(TABS,'tabs');
const li=xs=>xs&&xs.length?`<ul class="b">${xs.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:'<span class="muted">Nothing listed in the stored data for this stock.</span>';
async function load(rebuild){const s=document.getElementById('sym').value.trim().toUpperCase();const el=document.getElementById('rep');
 if(!s){el.innerHTML='<div class="card emptyst">Type a symbol first, for example RELIANCE.</div>';document.getElementById('sym').focus();return}
 el.innerHTML=`<div class="card">${skel((rebuild?'a fresh report for ':'the report for ')+s,5)}</div>`;
 try{const r=rebuild?await post(`/api/research/equity/${encodeURIComponent(s)}/refresh`):await j(`/api/research/equity/${encodeURIComponent(s)}`);el.innerHTML=render(r);scard(s);esurp(s)}
 catch(e){el.innerHTML=`<div class="card">${e.status===404&&!rebuild?`<div class="emptyst">No stored report for ${esc(s)} yet (${esc(e.message)}). Press <b>Rebuild &amp; save</b> to make one now, or check the symbol.</div>`:errbox(e,'the report for '+s,rebuild?'load(true)':'load()')}</div>`}}
async function scard(s){const el=document.getElementById('scd');if(!el)return;
 try{el.innerHTML=scorecard(await j(`/api/research/scorecard/${encodeURIComponent(s)}`))}catch(e){el.innerHTML=`<div class="card emptyst">No fundamental scorecard for ${esc(s)}: ${esc(e.message)}. It appears once ATIP has stored this stock's fundamentals.</div>`}
 const dv=document.getElementById('dvd');if(!dv)return;
 try{dv.innerHTML=dvmcard(await j(`/api/research/dvm/${encodeURIComponent(s)}`))}catch(e){dv.innerHTML=`<div class="card emptyst">No DVM view for ${esc(s)}: ${esc(e.message)}.</div>`}}
const ZCLS={STRONG_PERFORMER:'BUY',VALUE_UNDER_RADAR:'ADD',EXPENSIVE_PERFORMER:'ADD',MID_RANGE:'NOT_RATED',WEAK:'REDUCE',VALUE_TRAP:'SELL',MOMENTUM_TRAP:'SELL'};
function dvmcard(d){const lv=d.levels||{high:55,low:35};
 const ax=a=>`<div class="ax dvm"><b>${esc(a.label)}</b> <span class="v">${a.score==null?'—':n(a.score,0)}</span><span class="muted"> / 100${a.level?' · '+stl(a.level):''}</span>
 <div class="meter" role="img" aria-label="${esc(a.label)} ${a.score==null?'unknown':n(a.score,0)+' of 100'}" title="${esc(a.label)}: ${a.score==null?'unknown':n(a.score,0)} (low below ${lv.low}, high ${lv.high}+)"><span style="width:${a.score==null?0:Math.max(0,Math.min(100,a.score))}%"></span><i style="left:${lv.low}%"></i><i style="left:${lv.high}%"></i></div>
 <div class="muted sub">${esc(a.basis)}</div>
 ${a.components.map(c=>`<div class="ck"><span class="s">${c.score==null?'·':n(c.score,0)}</span>${esc(c.label)} <span class="muted">${c.score==null?'(no data)':'× '+n(c.weight*100,0)+'%'}</span><div class="d">${esc(c.detail)}</div></div>`).join('')}</div>`;
 const z=d.zone;
 return `<div class="card"><h2>DVM view: ${z?`<span class="pill ${ZCLS[z.key]||'NOT_RATED'}">${esc(z.label)}</span> <span class="muted">${esc(z.reading)}</span>`:`<span class="muted">${esc(d.note||'no zone')}</span>`}</h2>
 <div class="muted">Durability, valuation and momentum, 0–100 each (Trendlyne-style): durability from the scorecard's financial-health and past-performance checks, valuation from the research model's fair value and the P/E against the industry (P/B for financials; high = cheap), momentum from the daily technical rating and the RS rating. High is ${lv.high}+, low below ${lv.low} (the two marks on each bar).${z?` Zone rule: ${esc(z.rule)}.`:''} A description, not advice: the zones have no track record yet.</div>
 <div class="axes">${d.axes.map(ax).join('')}</div></div>`}
async function esurp(s){const el=document.getElementById('esd');if(!el)return;
 try{el.innerHTML=ecard(await j(`/api/research/earnings-surprise/${encodeURIComponent(s)}`))}catch(e){el.innerHTML=`<div class="card emptyst">Earnings surprise for ${esc(s)}: ${esc(e.message)}.</div>`}}
function ecard(o){const x=o.latest||{},sg=v=>v==null?'—':(v>0?'+':'')+n(v,2);
 const why=(v,r)=>v==null&&r?`<div class="muted sub" style="margin-top:3px">${esc(r)}</div>`:'';
 const tr=x.eps_trend?`${esc(stl(x.eps_trend))} <span class="muted sub">${x.eps_trend_pts>0?'+':''}${n(x.eps_trend_pts,1)} pts</span>`:'—';
 const sig=(o.signals||[]).slice(0,5).map(s=>`<tr><td>${esc(String(s.date).slice(0,10))}</td><td>${s.direction==='BULL'?'▲ positive':'▼ negative'}</td><td>${rs(s.entry)}</td><td>${rs(s.stop)}</td><td>${rs(s.target)}</td><td>${s.confluence}/6</td><td>${esc(s.status)}</td><td>${pct(s.excess_60d)}</td></tr>`);
 const rec=(o.record||[]).map(r=>`${esc(r.name)}: ${r.closed} closed, avg R ${r.avg_r==null?'—':n(r.avg_r,2)}, alerts ${r.alerts==='on'?'on':`held (${Math.min(r.closed,30)}/30)`}`).join(' · ');
 return `<div class="card"><h2>Earnings surprise <span class="muted">${esc(x.quarter||'')} · filed ${esc(String(x.available_from||x.known_on||'').slice(0,16))}${x.exact_date?'':' (estimated)'} · ${x.days_since_result} days ago</span></h2>
 <div class="muted">${esc(o.method)}</div>
 <div class="grid" style="margin-top:8px"><div class="stat"><div class="k">EPS surprise (SUE)</div><div class="v">${sg(x.sue)}</div>${why(x.sue,x.sue_reason)}</div>
 <div class="stat"><div class="k">Revenue surprise (SUE)</div><div class="v">${sg(x.sue_revenue)}</div>${why(x.sue_revenue,x.sue_revenue_reason)}</div>
 <div class="stat"><div class="k">EPS trend (revisions proxy)</div><div class="v">${tr}</div>${why(x.eps_trend,x.eps_trend_reason)}</div>
 <div class="stat"><div class="k">EPS vs a year earlier</div><div class="v">${n(x.eps_q)} <span class="muted sub">vs ${n(x.eps_year_ago)}</span></div></div></div>
 ${sig.length?`<div style="margin-top:8px">${table(['Drift signal','Surprise','Entry','Stop','Target','Confluence','Status','vs Nifty 60d'],sig,'Post-earnings drift signals')}</div>`:`<div class="muted" style="margin-top:6px">No post-earnings-drift signal (it needs |SUE| of ${n(o.trigger,0)}+ on the first session after a filing).</div>`}
 ${rec?`<div class="muted" style="margin-top:6px">${rec}</div>`:''}</div>`}
const AXN={value:'Value',growth:'Growth',past:'Past',health:'Health',dividend:'Dividend'};
function flake(axes){const cx=160,cy=118,R=80,ang=i=>(-90+72*i)*Math.PI/180,pt=(i,r)=>[cx+r*Math.cos(ang(i)),cy+r*Math.sin(ang(i))];
 const ring=k=>axes.map((_,i)=>pt(i,R*k/6).map(v=>v.toFixed(1)).join(',')).join(' ');
 const shape=axes.map((a,i)=>pt(i,R*Math.max(a.passed,0.15)/6).map(v=>v.toFixed(1)).join(',')).join(' ');
 const lab=axes.map((a,i)=>{const[x,y]=pt(i,R+16),an=Math.abs(x-cx)<5?'middle':x>cx?'start':'end';return `<text class="sv-ink" x="${x.toFixed(1)}" y="${(y+(y<cy-40?-2:y>cy+20?10:4)).toFixed(1)}" text-anchor="${an}">${AXN[a.key]||esc(a.label)} <tspan class="sv-t">${a.passed}/6</tspan></text>`}).join('');
 return `<svg viewBox="0 0 320 236" width="100%" style="max-width:300px" role="img" aria-label="${esc(axes.map(a=>`${a.label} ${a.passed} of 6`).join(', '))}">
 ${[2,4,6].map(k=>`<polygon class="sv-grid" points="${ring(k)}" fill="none"/>`).join('')}${axes.map((_,i)=>{const[x,y]=pt(i,R);return `<line class="sv-grid" x1="${cx}" y1="${cy}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}"/>`}).join('')}
 <polygon class="sv-area" points="${shape}"/>${lab}</svg>`}
function scorecard(sc){const mark=p=>p===true?'<i class="ok">✓</i>':p===false?'<i class="bad">✗</i>':'<i class="muted">·</i>';
 return `<div class="card"><h2>Fundamental scorecard: ${sc.checks_passed} of 30 checks passed <span class="muted">(${sc.checks_known} could be checked)</span></h2>
 <div class="muted">Five axes of six pass / fail checks over the stored fundamentals, each with the numbers it used: <span class="ok">✓</span> pass, <span class="bad">✗</span> fail, <b>·</b> no data or not meaningful${sc.financial?' (debt checks are not scored for a bank or NBFC: compare it with other financials)':''}. Growth uses the latest reported figures: ATIP holds no analyst forecasts.</div>
 <div class="axes"><div class="ax fl">${flake(sc.axes)}</div>${sc.axes.map(a=>`<div class="ax"><b>${esc(a.label)}</b> <span class="muted">${a.passed}/6</span>${a.checks.map(c=>`<div class="ck">${mark(c.pass)}${esc(c.label)}<div class="d">${esc(c.detail)}</div></div>`).join('')}</div>`).join('')}</div></div>`}
async function screc(){const h=document.getElementById('sch').value,el=document.getElementById('scr');el.innerHTML=skel('the scorecard record',3);
 try{const o=await j(`/api/research/scorecard-record?horizon=${h}`);
 el.innerHTML=table(['Checks passed','Samples','Beat the Nifty','Median excess','Mean excess'],o.bands.map(b=>`<tr><td>${esc(b.band)}</td><td>${b.n}${b.enough?'':' <span class="muted">(too few)</span>'}</td><td>${b.beat_nifty_pct==null?'—':b.beat_nifty_pct+'%'}</td><td>${pct(b.median_excess_pct)}</td><td>${pct(b.mean_excess_pct)}</td></tr>`),{label:'Scorecard record by checks passed',empty:`No scorecard is ${esc(h)} sessions old yet, so there is nothing to measure: the record grows as stored scorecards age.`})+
 `<div class="muted" style="margin-top:6px">${o.top_minus_bottom_pct==null?`Top band minus bottom band: not yet (${o.stored_days} days stored).`:`Top band minus bottom band: <b>${pct(o.top_minus_bottom_pct)}</b> over ${o.horizon} sessions.`}</div>`}
 catch(e){el.innerHTML=errbox(e,'the scorecard record','screc()')}}
const METH={dcf:'Discounted cash flow (DCF)',peer:'Peer multiples',history:'Own-history multiples',justified_pb:'Justified P/B'};
function render(r){const v=r.valuation,ps=r.price_stats||{},q=r.quality||{},f=r.key_financials||{};
 const meth=Object.entries(v.methods||{}).map(([k,m])=>`<tr><td title="${esc(k)}">${esc(METH[k]||k.replaceAll('_',' '))}</td><td>${rs(m.value)}</td><td>${n(m.weight*100,0)}%</td><td class="muted">${esc(m.multiple?`${m.multiple.toUpperCase()} ${m.peer_median??m.own_median}x (${m.peers??m.quarters} ${m.peers?'peers':'quarters'})`:`ke ${m.ke_pct}%`)}</td></tr>`);
 const sc=v.scenarios?['bear','base','bull'].map(k=>`<tr><td>${k}</td><td>${rs(v.scenarios[k].value)}</td><td>${v.scenarios[k].g1_pct}%</td><td>${v.scenarios[k].ke_pct}%</td><td>${v.scenarios[k].terminal_pct}%</td><td>${n(v.scenarios[k].weight*100,0)}%</td></tr>`):[];
 const sens=v.sensitivity?`<div class="tw ax-scrollx" tabindex="0" role="region" aria-label="Sensitivity of the base-case value"><table><thead><tr><th scope="col">ke \\ g∞</th>${v.sensitivity.terminal_pct.map(x=>`<th scope="col">${x}%</th>`).join('')}</tr></thead><tbody>${v.sensitivity.values.map((row,i)=>`<tr><th scope="row">${v.sensitivity.ke_pct[i]}%</th>${row.map(x=>`<td>${rs(x)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`:'<span class="muted">DCF not applicable to this stock (no positive cash-flow base to discount).</span>';
 const peers=(r.peers||[]).map(p=>`<tr><td>${esc(p.symbol)}</td><td>${rs(p.price)}</td><td>${n(p.pe,1)}</td><td>${n(p.pb,1)}</td><td>${n(p.roe_pct,1)}</td><td>${pct(p.revenue_growth_pct)}</td><td>${n(p.mcap_cr,0)}</td></tr>`);
 const shp=(r.shareholding||[]).map(x=>`<tr><td>${esc(String(x.as_of).slice(0,10))}</td><td>${n(x.promoter_pct,2)}</td><td>${n(x.fpi_pct,2)}</td><td>${n(x.mf_pct,2)}</td><td>${n(x.retail_pct,2)}</td><td>${n(x.pledged_pct,2)}</td></tr>`);
 return `<div class="card"><h2>${esc(r.symbol)} <span class="muted">${esc(r.industry||'')}</span> · ${pill(r.rating)} <span class="muted">as of ${esc(r.as_of)}</span></h2>
 <div class="grid"><div class="stat"><div class="k">Price</div><div class="v">${rs(r.price)}</div></div><div class="stat"><div class="k">12-month target</div><div class="v">${rs(r.target_price)}</div></div>
 <div class="stat"><div class="k">Upside</div><div class="v">${pct(r.upside_pct)}</div></div><div class="stat"><div class="k">Fair value</div><div class="v">${rs(r.fair_value)}</div></div>
 <div class="stat"><div class="k">Uncertainty</div><div class="v">${esc((r.uncertainty||'—').replace('_',' '))}</div></div><div class="stat"><div class="k">Moat proxy / quality</div><div class="v">${esc(q.moat_proxy||'—')} / ${n(q.quality_score,0)}</div></div></div></div>
 <div id="scd"><div class="card">${skel('the scorecard',3)}</div></div><div id="dvd"></div>
 <div id="esd"></div>
 <div class="two"><div class="card"><h2>Investment thesis</h2>${li(r.thesis)}</div><div class="card"><h2>Risks</h2>${li(r.risks)}</div></div>
 <div class="card"><h2>Catalysts</h2>${li(r.catalysts)}</div>
 <div class="two"><div class="card"><h2>Valuation methods (cost of equity ${v.cost_of_equity_pct}%)</h2>${table(['Method','Value','Weight','Basis'],meth,{label:'Valuation methods',empty:'No valuation method could be applied: the stored fundamentals are too thin.'})}</div>
 <div class="card"><h2>DCF scenarios</h2>${table(['Case','Value','Growth','ke','Terminal','Weight'],sc,{label:'DCF scenarios',empty:'No DCF for this stock: it has no positive cash-flow base to discount.'})}</div></div>
 <div class="card"><h2>Sensitivity: base-case value by cost of equity × terminal growth</h2>${sens}</div>
 <div class="two"><div class="card"><h2>Key financials (${esc(f.quarter||'')})</h2>${table(['Metric','Value'],[['EPS (TTM)',n(f.eps_ttm)],['Book value / share',n(f.book_value_ps)],['Revenue (₹ cr, quarter)',n(f.revenue_cr,0)],['Revenue growth YoY',pct(f.revenue_growth_yoy)],['EPS growth YoY',pct(f.eps_growth_yoy)],['ROE',f.roe==null?'—':n(f.roe*100,1)+'%'],['ROCE',f.roce==null?'—':n(f.roce*100,1)+'%'],['Debt / equity',n(f.debt_equity)],['Interest cover',n(f.interest_coverage,1)]].map(([a,b])=>`<tr><td>${a}</td><td>${b}</td></tr>`),'Key financials')}</div>
 <div class="card"><h2>Price history (${n(ps.history_years,1)} years stored)</h2>${table(['Statistic','Value'],[['52-week range',`${rs(ps.low_52w)} – ${rs(ps.high_52w)}`],['1-year return',pct(ps.return_1y_pct)],['3-year CAGR',pct(ps.cagr_3y_pct)],['5-year CAGR',pct(ps.cagr_5y_pct)],['7-year CAGR',pct(ps.cagr_7y_pct)],['Volatility (1y)',ps.volatility_1y_pct==null?'—':ps.volatility_1y_pct+'%'],['Max drawdown (1y)',pct(ps.max_drawdown_1y_pct)]].map(([a,b])=>`<tr><td>${a}</td><td>${b}</td></tr>`),'Price history statistics')}</div></div>
 <div class="card"><h2>Peers (${esc(r.industry||'industry unknown')})</h2>${table(['Symbol','Price','P/E','P/B','ROE %','Revenue growth','M-cap ₹ cr'],peers,{label:'Peers',empty:'No peers stored: ATIP tracks no other stock in this industry yet.'})}</div>
 <div class="card"><h2>Shareholding (%)</h2>${table(['Quarter','Promoter','FPI','MF','Retail','Pledged'],shp,{label:'Shareholding',empty:'No shareholding pattern stored for this stock yet: it arrives with the quarterly filings.'})}</div>
 <div class="card"><h2>Disclosures</h2>${li(r.disclosures)}</div>`}
async function rat(){screc();const r=document.getElementById('rf').value,el=document.getElementById('rat');el.innerHTML=skel('the ratings',4);
 try{const d=await j('/api/research/equity'+(r?`?rating=${r}`:''));
 el.innerHTML=table(['Symbol','As of','Price','Target','Upside','Rating','Uncertainty','Moat','ATIP signal'],d.map(x=>`<tr><td><a href="#" onclick="document.getElementById('sym').value='${esc(x.symbol)}';show('rep');load();return false">${esc(x.symbol)}</a></td><td>${esc(String(x.as_of).slice(0,10))}</td><td>${rs(x.price)}</td><td>${rs(x.target_price)}</td><td>${pct(x.upside_pct)}</td><td>${pill(x.rating)}</td><td>${esc(x.uncertainty||'')}</td><td>${esc(x.moat_proxy||'')}</td><td>${esc(x.atip_signal||'')}</td></tr>`),
  {plain:1,empty:r?`No stock is rated ${esc(stl(r))} right now: pick another rating or All ratings.`:'No research reports are stored yet: press Rebuild all, or open a symbol on the Report tab and press Rebuild &amp; save.'})}
 catch(e){el.innerHTML=errbox(e,'the ratings','rat()')}
 const hEl=document.getElementById('hit');hEl.innerHTML=skel('the hit rate',2);
 try{const h=await j('/api/research/hit-rate');hEl.innerHTML=table(['Rating','Calls','Open','Hit','Missed','Success rate','Avg return'],Object.entries(h).map(([k,o])=>`<tr><td>${pill(k)}</td><td>${o.calls}</td><td>${o.open}</td><td>${o.hit}</td><td>${o.missed}</td><td>${o.success_rate_pct==null?'—':o.success_rate_pct+'%'}</td><td>${pct(o.avg_return_pct)}</td></tr>`),
  {label:'Hit rate by rating',empty:'No calls recorded yet: a call is logged when a stock is first rated or its target moves by more than 5%.'})}
 catch(e){hEl.innerHTML=errbox(e,'the hit rate','rat()')}}
async function runAll(){if(!confirm('Rebuild today\'s report for every tracked stock?'))return;const o=document.getElementById('rao');o.textContent=' rebuilding…';
 try{const x=await post('/api/research/equity/run');o.textContent=` ${x.rows} reports, ${x.calls} calls`;rat()}catch(e){o.textContent=' '+e.message}}
async function his(){const el=document.getElementById('cov');el.innerHTML=skel('the history coverage',2);
 try{const c=await j('/api/data/history/coverage');el.innerHTML=`<div class="grid"><div class="stat"><div class="k">Target</div><div class="v">${esc(c.target)}</div></div><div class="stat"><div class="k">Complete</div><div class="v">${c.complete} / ${c.symbols}</div></div><div class="stat"><div class="k">Listed later</div><div class="v">${c.listed_later}</div></div><div class="stat"><div class="k">Pending</div><div class="v">${c.pending}</div></div><div class="stat"><div class="k">Parked (errors)</div><div class="v">${c.parked}</div></div><div class="stat"><div class="k">Oldest bar</div><div class="v">${esc(c.oldest_bar||'—')}</div></div></div>`}
 catch(e){el.innerHTML=errbox(e,'the history coverage','his()')}}
async function backfill(){document.getElementById('bfo').textContent=' running…';try{const o=await post('/api/data/history/backfill',{max:10});document.getElementById('bfo').textContent=` ${o.status}: ${o.symbols??0} stocks, ${o.rows??0} bars, ${o.remaining??0} to go`;his()}catch(e){document.getElementById('bfo').textContent=' '+e.message}}
show('rep');
{const q=new URLSearchParams(location.search).get('symbol');if(q){document.getElementById('sym').value=q;load()}}
</script></body></html>"""

OPTIONS = _HEAD + r"""
<div class="wrap"><div class="card"><h2>Build a strategy</h2>
<div class="muted">Analysis only: nothing is ordered, and margin is not computed (check your broker's calculator). Premiums come from ATIP's stored option chain / F&amp;O bhavcopy; legs with no stored quote are priced with Black-Scholes at 18% IV and say so.</div>
<div class="frm">
<div class="fld"><label for="sym">Underlying</label><input id="sym" class="w-m" list="symlist" value="NIFTY" autocomplete="off" spellcheck="false" aria-describedby="symn" onchange="chain()"><datalist id="symlist"></datalist></div>
<div class="fld"><label for="spot">Spot price</label><input id="spot" class="w-m" type="number" step="any" min="0" inputmode="decimal" oninput="schedule()"></div>
<div class="fld"><label for="lot">Lot size</label><input id="lot" class="w-s" type="number" min="1" step="1" value="75" inputmode="numeric" oninput="schedule()"></div>
<div class="fld"><label for="exp">Expiry for new legs</label><input id="exp" class="w-m" list="exps" placeholder="YYYY-MM-DD" autocomplete="off" onchange="rewing()"><datalist id="exps"></datalist></div>
<div class="fld"><label for="w">Wing width (strikes)</label><input id="w" class="w-s" type="number" min="1" step="1" value="2" inputmode="numeric" onchange="rewing()"></div>
<div class="fld"><label for="td">Value the P&amp;L on</label><input id="td" class="w-m" type="date" onchange="schedule()"></div></div>
<div id="symn" class="muted hint"></div><div id="ci" class="muted hint" role="status"></div>
<h3>Start from a preset</h3>
<div class="presets" role="group" aria-label="Strategy presets">
<button type="button" class="chip" data-tpl="long_call" aria-pressed="false" onclick="build('long_call')">Long call</button>
<button type="button" class="chip" data-tpl="long_put" aria-pressed="false" onclick="build('long_put')">Long put</button>
<button type="button" class="chip" data-tpl="bull_call_spread" aria-pressed="false" onclick="build('bull_call_spread')">Bull call spread</button>
<button type="button" class="chip" data-tpl="bear_put_spread" aria-pressed="false" onclick="build('bear_put_spread')">Bear put spread</button>
<button type="button" class="chip" data-tpl="iron_condor" aria-pressed="false" onclick="build('iron_condor')">Iron condor</button></div>
<div class="frm"><div class="fld"><label for="tpl">Other strategies</label><select id="tpl" class="w-l"><option value="">Loading…</option></select></div><button type="button" onclick="build()">Build</button></div></div>
<div class="card"><h2>Legs <span id="legn" class="muted"></span></h2><div id="legs"></div>
<div class="frm"><button type="button" id="addleg" class="ghost" onclick="addLeg()">+ Add a leg</button><button type="button" class="ghost" onclick="clearLegs()">Clear legs</button><button type="button" onclick="analyse()">Analyse</button><span class="muted">Results update by themselves as you edit.</span></div></div>
<div id="ost" class="muted" role="status" aria-live="polite"></div>
<div id="out"></div></div><div id="tip"></div>
<script>""" + _JS_COMMON + r"""
let LEGS=[],LAST=null,RT=null,SEQ=0,TM=null,TPL=null,EDITED=false,STEP=null;const TPLS={};
const $=id=>document.getElementById(id);
const val=id=>$(id).value;
const todayIST=()=>new Date(Date.now()+19800000).toISOString().slice(0,10);
window.addEventListener('resize',()=>{clearTimeout(RT);RT=setTimeout(()=>LAST&&chart(LAST),150)});
(async()=>{empty();drawLegs();
 const d=new Date(Date.now()+19800000);d.setUTCDate(d.getUTCDate()+((2-d.getUTCDay()+7)%7||7));$('exp').value=d.toISOString().slice(0,10);  // NSE weekly expiry: Tuesday (IST)
 j('/api/options/symbols').then(o=>{$('symlist').innerHTML=o.symbols.map(s=>`<option value="${esc(s.symbol)}">${esc(s.source)} · ${esc(s.as_of)}</option>`).join('');
  $('symn').textContent=o.symbols.length?`${o.symbols.length} underlyings have a stored chain: type to search them.`:'No stored option chain yet: type any symbol and enter its spot price.'})
  .catch(e=>{$('symn').textContent='The list of stored chains is unavailable ('+e.message+'): type a symbol.'});
 try{const t=await j('/api/options/templates');t.forEach(x=>TPLS[x.key]=x);
  $('tpl').innerHTML=t.map(x=>`<option value="${esc(x.key)}">${esc(x.name)} (${esc(x.view)})</option>`).join('');
  document.querySelectorAll('[data-tpl]').forEach(b=>{const x=TPLS[b.dataset.tpl];if(x)b.title=x.description})}
 catch(e){$('tpl').innerHTML='<option value="">Strategy list unavailable</option>'}
 const q=new URLSearchParams(location.search).get('symbol');if(q)$('sym').value=q.trim().toUpperCase();
 await chain()})();
async function chain(){const s=val('sym').trim().toUpperCase(),ci=$('ci');$('sym').value=s;
 if(!s){ci.textContent='Type an underlying such as NIFTY.';return}
 ci.textContent=`Loading the stored chain for ${s}…`;
 try{const c=await j(`/api/options/chain/${encodeURIComponent(s)}`);STEP=c.strike_step||null;
  if(c.spot)$('spot').value=c.spot;if(c.lot_size)$('lot').value=c.lot_size;
  const t=todayIST(),ex=(c.expiries||[]).filter(e=>e>=t);
  $('exps').innerHTML=ex.map(e=>`<option value="${esc(e)}"></option>`).join('');
  if(ex.length)$('exp').value=ex.find(e=>e>t)||ex[0];
  ci.textContent=c.quotes?`${s}: ${n(c.quotes,0)} stored quotes, ${ex.length} expir${ex.length===1?'y':'ies'} still open${ex.length?' (next '+ex[0]+')':''}. Spot ${c.spot?n(c.spot,2):'unknown: enter it'}${c.lot_size?', lot '+c.lot_size:''}.`
   :`No stored option chain for ${s}: premiums will be modelled with Black-Scholes at 18% IV. ${c.spot?'Spot is the last stored close.':'Enter the spot price.'}`;
  if(TPL&&!EDITED)build(TPL);else if(LEGS.length)schedule()}
 catch(e){ci.textContent=`Could not load ${s}: ${e.message}. Enter the spot price yourself.`}}
function body(){return{symbol:val('sym').trim().toUpperCase()||null,spot:Number(val('spot'))||null,lot_size:Number(val('lot'))||1,target_date:val('td')||null}}
function mark(t){document.querySelectorAll('[data-tpl]').forEach(b=>b.setAttribute('aria-pressed',b.dataset.tpl===t?'true':'false'))}
function rewing(){if(TPL&&!EDITED)build(TPL)}
async function build(tpl){tpl=tpl||val('tpl');if(!tpl)return;TPL=tpl;EDITED=false;mark(tpl);const my=++SEQ;clearTimeout(TM);
 busy(`Building the ${TPLS[tpl]?TPLS[tpl].name.toLowerCase():'strategy'}…`);
 try{const r=await post('/api/options/build',{...body(),template:tpl,expiry:val('exp'),width_steps:Number(val('w'))||2});if(my!==SEQ)return;
  LEGS=r.legs.map(l=>({...l}));drawLegs();show(r)}catch(e){if(my===SEQ)fail(e)}}
function busy(m){$('ost').textContent=m;if(!LAST)$('out').innerHTML=`<div class="card">${skel('the payoff',4)}</div>`}
function fail(e){const box=`<div class="errst" role="alert"><b>Can't analyse these legs yet.</b> ${esc(e.message)}${LAST?' The result below is for the last legs that worked.':''}</div>`;
 if(LAST)$('ost').innerHTML=box;else{$('ost').textContent='';$('out').innerHTML=`<div class="card">${box}</div>`}}
function empty(){LAST=null;$('ost').textContent='';$('out').innerHTML=`<div class="card emptyst"><h2>Three steps to a payoff</h2><ol>
<li><b>Pick the underlying.</b> Choose NIFTY, BANKNIFTY or a stock from the list: ATIP fills in its spot price, lot size and the expiries it has stored.</li>
<li><b>Add legs.</b> Press a preset such as <i>Iron condor</i>, or <i>Add a leg</i> and set buy or sell, call or put, strike and lots.</li>
<li><b>Read the result.</b> The payoff chart, max profit and loss, breakevens, net Greeks and who the strategy suits appear here and update as you edit. Nothing is ever ordered.</li></ol></div>`}
function schedule(){clearTimeout(TM);if(!LEGS.length){SEQ++;empty();return}$('ost').textContent='Updating…';TM=setTimeout(analyse,300)}
async function analyse(){clearTimeout(TM);if(!LEGS.length){empty();return}const my=++SEQ;if(!LAST)busy('Working out the payoff…');
 try{const r=await post('/api/options/analyse',{...body(),legs:LEGS});if(my!==SEQ)return;merge(r);show(r)}catch(e){if(my===SEQ)fail(e)}}
function atmStrike(){const s=Number(val('spot'));if(!s)return null;const st=STEP||(s>=10000?50:s>=1000?10:5);return Math.round(s/st)*st}
function addLeg(){LEGS.push({kind:'CE',side:'BUY',strike:atmStrike(),expiry:val('exp'),lots:1,premium:null});EDITED=true;TPL=null;mark(null);drawLegs();
 const f=$(`l${LEGS.length-1}-side`);if(f)f.focus();schedule()}
function rmLeg(i){LEGS.splice(i,1);EDITED=true;drawLegs();schedule();$('addleg').focus()}
function clearLegs(){LEGS=[];TPL=null;mark(null);drawLegs();schedule()}
function upd(i,k,v){const l=LEGS[i];if(!l)return;
 if(k==='kind'||k==='side'||k==='expiry')l[k]=v;else l[k]=v===''?null:Number(v);
 if(k==='strike'||k==='expiry'||k==='kind'){l.premium=null;l.iv=null;delete l.price_source;delete l.iv_source;const p=$(`l${i}-premium`);if(p)p.value=''}
 if(k==='premium'){l.iv=null;delete l.iv_source;if(v==='')delete l.price_source;else l.price_source='entered by you'}
 if(k==='kind'){const s=$(`l${i}-strike`);if(s){s.disabled=v==='FUT';if(v==='FUT'){l.strike=null;s.value=''}else if(l.strike==null){l.strike=atmStrike();s.value=l.strike??''}}}
 EDITED=true;schedule()}
const ivt=l=>l.kind==='FUT'?'—':l.iv==null?'auto':n(l.iv*100,1)+'%';
function drawLegs(){const el=$('legs');$('legn').textContent=LEGS.length?`(${LEGS.length})`:'';
 if(!LEGS.length){el.innerHTML='<p class="emptyst">No legs yet. Pick a preset above, or press <b>+ Add a leg</b>.</p>';return}
 const opt=(i,k,os)=>os.map(([v,t])=>`<option value="${v}"${LEGS[i][k]===v?' selected':''}>${t}</option>`).join('');
 el.innerHTML=LEGS.map((l,i)=>{const id=k=>`l${i}-${k}`,N=i+1,fut=l.kind==='FUT';
  return `<div class="leg" role="group" aria-labelledby="${id('h')}"><div class="legn" id="${id('h')}">Leg ${N}</div>
<div class="fld"><label for="${id('side')}">Side</label><select id="${id('side')}" onchange="upd(${i},'side',this.value)">${opt(i,'side',[['BUY','Buy'],['SELL','Sell']])}</select></div>
<div class="fld"><label for="${id('kind')}">Type</label><select id="${id('kind')}" onchange="upd(${i},'kind',this.value)">${opt(i,'kind',[['CE','Call (CE)'],['PE','Put (PE)'],['FUT','Future']])}</select></div>
<div class="fld"><label for="${id('strike')}">Strike</label><input id="${id('strike')}" type="number" step="any" min="0" inputmode="decimal" value="${fut?'':esc(l.strike??'')}"${fut?' disabled':''} oninput="upd(${i},'strike',this.value)"></div>
<div class="fld"><label for="${id('expiry')}">Expiry</label><input id="${id('expiry')}" list="exps" autocomplete="off" placeholder="YYYY-MM-DD" value="${esc(l.expiry||'')}" onchange="upd(${i},'expiry',this.value)"></div>
<div class="fld"><label for="${id('lots')}">Lots</label><input id="${id('lots')}" type="number" min="1" step="1" inputmode="numeric" value="${esc(l.lots??1)}" oninput="upd(${i},'lots',this.value)"></div>
<div class="fld"><label for="${id('premium')}">Premium ₹/unit</label><input id="${id('premium')}" type="number" step="any" min="0" inputmode="decimal" placeholder="auto" value="${esc(l.premium??'')}" oninput="upd(${i},'premium',this.value)"></div>
<div class="legi">IV <b id="${id('iv')}">${ivt(l)}</b><span class="src" id="${id('src')}">${esc(l.price_source||l.iv_source||'')}</span></div>
<button type="button" class="ghost rm" title="Remove leg ${N}" aria-label="Remove leg ${N}" onclick="rmLeg(${i})">✕</button></div>`}).join('')}
function merge(r){(r.legs||[]).forEach((x,i)=>{const l=LEGS[i];if(!l)return;
 if(l.premium==null&&x.premium!=null){l.premium=x.premium;const p=$(`l${i}-premium`);if(p&&document.activeElement!==p)p.value=x.premium}
 if(x.price_source&&!l.price_source)l.price_source=x.price_source;l.iv=x.iv;l.iv_source=x.iv_source;
 const v=$(`l${i}-iv`);if(v)v.textContent=ivt(l);const s=$(`l${i}-src`);if(s)s.textContent=l.price_source||l.iv_source||''})}
const nearest=(xs,x)=>{let i=0,b=Infinity;for(let k=0;k<xs.length;k++){const d=Math.abs(xs[k]-x);if(d<b){b=d;i=k}}return i};
function zones(r){const xs=r.payoff.spot,e=r.payoff.expiry,cuts=[xs[0],...(r.breakevens||[]),xs[xs.length-1]],win=[],lose=[];
 for(let k=0;k<cuts.length-1;k++){const a=cuts[k],b=cuts[k+1],v=e[nearest(xs,(a+b)/2)];
  const t=cuts.length===2?'anywhere in the range shown':k===0?`below ${n(b,0)}`:k===cuts.length-2?`above ${n(a,0)}`:`between ${n(a,0)} and ${n(b,0)}`;(v>0?win:lose).push(t)}
 return {win,lose}}
function summary(r){const S=r.symbol||'the underlying',z=zones(r),xs=r.payoff.spot,i=nearest(xs,r.spot);
 const w=z.win.length?`makes money if ${S} ends ${z.win.join(' or ')}`:'makes no money anywhere in the range shown',l=z.lose.length?`loses ${z.lose.join(' or ')}`:'never loses in the range shown';
 const mp=r.unlimited_profit?'unlimited':rs(r.max_profit),ml=r.unlimited_loss?'unlimited':rs(r.max_loss);
 const aria=`Payoff at expiry ${r.first_expiry}: ${w}; ${l}. Max profit ${mp}, max loss ${ml}. Breakevens ${(r.breakevens||[]).map(x=>n(x,0)).join(', ')||'none'}.`;
 const html=`<dl class="kvs"><dt>At expiry (${esc(r.first_expiry)})</dt><dd>It ${esc(w)}; it ${esc(l)}.</dd>
 <dt>Best case</dt><dd>${r.unlimited_profit?'<span class="ok">Unlimited</span>: the profit keeps growing with a big move':rs(r.max_profit)}</dd>
 <dt>Worst case</dt><dd>${r.unlimited_loss?'<span class="bad">Unlimited</span>: a big move keeps adding to the loss':rs(r.max_loss)}</dd>
 <dt>If ${esc(S)} stays at ${n(r.spot,0)}</dt><dd>${rs(r.payoff.expiry[i])} at expiry</dd>
 <dt>On ${esc(r.target_date)} at today's spot</dt><dd>${rs(r.payoff.target[i])} <span class="muted">(Black-Scholes, before expiry)</span></dd></dl>`;
 return {aria,html}}
const TIERC={Beginner:'t1',Intermediate:'t2',Advanced:'t3'};
function show(r){LAST=r;$('ost').textContent='';const g=r.greeks||{},su=r.suitability||{},S=r.symbol||'the underlying',sm=summary(r);
 const bes=(r.breakevens||[]).length?r.breakevens.map(x=>n(x,2)).join(' · '):'None in the range shown';
 const mp=r.unlimited_profit?'<span class="ok">Unlimited</span>':rs(r.max_profit),ml=r.unlimited_loss?'<span class="bad">Unlimited</span>':rs(r.max_loss);
 $('out').innerHTML=`<div class="card"><h2>Result <span class="muted">· ${esc(S)} at ${n(r.spot,2)} · ${r.legs.length} leg${r.legs.length===1?'':'s'} × lot ${r.lot_size} · first expiry ${esc(r.first_expiry)}</span></h2>
 ${su.tier?`<div class="tierrow"><span class="muted">Suits</span> <span class="tierb ${TIERC[su.tier]||''}">${esc(su.tier)}</span> <span>${esc(su.reason)}</span></div>`:''}
 <div class="grid"><div class="stat"><div class="k">Net premium</div><div class="v">${rs(Math.abs(r.net_premium))}</div><div class="k">${r.premium_type==='credit'?'received (credit)':'paid (debit)'}</div></div>
 <div class="stat"><div class="k">Max profit</div><div class="v">${mp}</div></div>
 <div class="stat"><div class="k">Max loss</div><div class="v">${ml}</div></div>
 <div class="stat"><div class="k">Breakevens at expiry</div><div class="v sm">${bes}</div></div>
 <div class="stat"><div class="k">Probability of profit</div><div class="v">${r.probability_of_profit_pct==null?'—':n(r.probability_of_profit_pct,1)+'%'}</div></div>
 <div class="stat"><div class="k">Reward : risk</div><div class="v">${r.reward_to_risk==null?'—':n(r.reward_to_risk,2)+' : 1'}</div></div></div>
 <h3>Net Greeks <span class="muted">(the whole position, today)</span></h3>
 <div class="grid"><div class="stat"><div class="k">Δ Delta (₹ per point)</div><div class="v">${n(g.delta,2)}</div></div><div class="stat"><div class="k">Γ Gamma (Δ per point)</div><div class="v">${n(g.gamma,4)}</div></div>
 <div class="stat"><div class="k">Θ Theta (₹ per day)</div><div class="v">${n(g.theta,0)}</div></div><div class="stat"><div class="k">ν Vega (₹ per vol point)</div><div class="v">${n(g.vega,0)}</div></div></div>
 <div class="muted sub" style="margin-top:6px">Δ: rupees made for a 1-point rise in ${esc(S)} · Γ: how fast Δ changes · Θ: rupees made or lost a day from time decay · ν: rupees made for a 1-point rise in implied volatility.</div></div>
 <div class="card"><h2>Payoff (₹)</h2><div class="pay"><figure style="margin:0"><div class="legend"><span><i class="s1"></i>At expiry ${esc(r.first_expiry)}</span><span><i class="s2"></i>On ${esc(r.target_date)}</span><span><i class="sp"></i>Spot ${n(r.spot,2)}</span><span><b class="sw pa"></b>profit</span><span><b class="sw la"></b>loss</span></div>
 <div id="chart"></div></figure><div>${sm.html}</div></div>
 <details style="margin-top:8px"><summary class="muted">Table view of the payoff</summary>${table(['Spot','At expiry (₹)','On '+esc(r.target_date)+' (₹)'],r.payoff.spot.map((x,i)=>`<tr><td>${n(x,2)}</td><td>${n(r.payoff.expiry[i],0)}</td><td>${n(r.payoff.target[i],0)}</td></tr>`),{label:'Payoff table',cls:'scroll'})}</details>
 <div class="muted" style="margin-top:6px">${(r.notes||[]).map(esc).join(' ')}</div></div>`;chart(r)}
const kf=v=>{const a=Math.abs(v),s=v<0?'−':'';return a>=1e7?s+n(a/1e7,1)+' Cr':a>=1e5?s+n(a/1e5,1)+' L':a>=1e3?s+n(a/1e3,a>=1e4?0:1)+'k':s+n(a,0)};
function chart(r){LAST=r;const el=$('chart');if(!el)return;
 const W=Math.max(240,Math.floor(el.clientWidth||600)),H=W<480?250:310,m={l:W<480?48:58,r:12,t:14,b:28},pw=W-m.l-m.r;
 const xs=r.payoff.spot,e=r.payoff.expiry,t=r.payoff.target,x0=xs[0],x1=xs[xs.length-1],f=v=>v.toFixed(1);
 let y0=Math.min(0,...e,...t),y1=Math.max(0,...e,...t);const pad=(y1-y0)*0.08||1;y0-=pad;y1+=pad;
 const X=v=>m.l+(v-x0)/(x1-x0)*pw,Y=v=>m.t+(y1-v)/(y1-y0)*(H-m.t-m.b),z=Y(0);
 const path=a=>a.map((v,i)=>(i?'L':'M')+f(X(xs[i]))+','+f(Y(v))).join('');
 const area=`M${f(X(x0))},${f(z)}`+e.map((v,i)=>'L'+f(X(xs[i]))+','+f(Y(v))).join('')+`L${f(X(x1))},${f(z)}Z`;
 const lab=summary(r).aria;
 let s=`<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="${esc(lab)}" style="display:block"><title>${esc(lab)}</title>`;
 s+=`<defs><clipPath id="cpU"><rect x="${m.l}" y="${m.t}" width="${pw}" height="${f(Math.max(0,z-m.t))}"/></clipPath><clipPath id="cpD"><rect x="${m.l}" y="${f(z)}" width="${pw}" height="${f(Math.max(0,H-m.b-z))}"/></clipPath></defs>`;
 const raw=(y1-y0)/5,mag=Math.pow(10,Math.floor(Math.log10(raw))),stp=[1,2,2.5,5,10].map(k=>k*mag).find(k=>k>=raw);    // round ticks, 0 among them
 for(let v=Math.ceil(y0/stp)*stp;v<=y1;v+=stp){s+=`<line class="sv-grid" x1="${m.l}" x2="${W-m.r}" y1="${f(Y(v))}" y2="${f(Y(v))}"/><text class="sv-t" x="${m.l-6}" y="${f(Y(v)+4)}" text-anchor="end">${kf(Math.abs(v)<stp/1e6?0:v)}</text>`}
 (W<520?[0,.5,1]:[0,.25,.5,.75,1]).forEach(q=>{const v=x0+(x1-x0)*q;s+=`<text class="sv-t" x="${f(X(v))}" y="${H-8}" text-anchor="${q===0?'start':q===1?'end':'middle'}">${n(v,0)}</text>`});
 s+=`<path class="sv-pa" d="${area}" clip-path="url(#cpU)"/><path class="sv-la" d="${area}" clip-path="url(#cpD)"/>`;
 s+=`<line class="sv-zero" x1="${m.l}" x2="${W-m.r}" y1="${f(z)}" y2="${f(z)}"/><line class="sv-spot" x1="${f(X(r.spot))}" x2="${f(X(r.spot))}" y1="${m.t}" y2="${H-m.b}"/>`;
 s+=`<path class="sv-s2" d="${path(t)}"/><path class="sv-s1" d="${path(e)}"/>`;
 (r.breakevens||[]).forEach((b,k)=>{const bx=X(b),dy=k%2?18:-9,an=bx<m.l+24?'start':bx>W-m.r-24?'end':'middle';s+=`<circle class="sv-be" cx="${f(bx)}" cy="${f(z)}" r="4"/><text class="sv-ink" x="${f(bx)}" y="${f(z+dy)}" text-anchor="${an}">${n(b,0)}</text>`});
 s+=`<line id="xh" class="sv-xh" y1="${m.t}" y2="${H-m.b}" opacity="0"/><circle id="d1" class="sv-d1" r="4" opacity="0"/><circle id="d2" class="sv-d2" r="4" opacity="0"/>`;
 s+=`<rect x="${m.l}" y="${m.t}" width="${pw}" height="${H-m.t-m.b}" fill="transparent" id="hit"/></svg>`;
 el.innerHTML=s;const tip=$('tip'),hit=$('hit');
 hit.onmousemove=ev=>{const b=hit.getBoundingClientRect(),sv=x0+(ev.clientX-b.left)/b.width*(x1-x0),i=nearest(xs,sv);
  const xh=$('xh');xh.setAttribute('x1',X(xs[i]));xh.setAttribute('x2',X(xs[i]));xh.setAttribute('opacity',.6);
  [['d1',e],['d2',t]].forEach(([id,a])=>{const c=$(id);c.setAttribute('cx',X(xs[i]));c.setAttribute('cy',Y(a[i]));c.setAttribute('opacity',1)});
  tip.style.display='block';tip.style.left=Math.min(ev.clientX+14,window.innerWidth-230)+'px';tip.style.top=(ev.clientY+10)+'px';
  tip.innerHTML=`<b>Spot ${n(xs[i],2)}</b> <span class="muted">(${pct((xs[i]/r.spot-1)*100)})</span><br><span class="c-s1">━</span> At expiry: ${rs(e[i])}<br><span class="c-s2">┅</span> On ${esc(r.target_date)}: ${rs(t[i])}`};
 hit.onmouseleave=()=>{tip.style.display='none';['xh','d1','d2'].forEach(id=>$(id).setAttribute('opacity',0))}}
</script></body></html>"""


SCREENER = _HEAD + r"""
<div class="wrap"><div class="card"><h2>Stock screener</h2>
<div class="muted">Filter every stock ATIP tracks on fundamentals, technicals or both, Screener.in / Chartink style: <code>roce_pct &gt; 20 AND debt_equity &lt; 0.5 AND (pe &lt; 25 OR peg &lt; 1)</code>, <code>scan_golden_cross = 1 AND rs_rating &gt;= 80</code>, <code>patterns CONTAINS "engulfing" AND rsi_14 &lt; 40</code>, <code>industry IN ("Capital Goods")</code>. Percentages are in %, money in ₹ crore where the field ends in _cr. A stock missing a field never matches a condition on it.</div>
<div id="presets" class="presets" role="group" aria-label="Preset screens" style="margin:8px 0">__SK__</div>
<div class="frm"><div class="fld grow"><label for="nl">Ask in English</label><input id="nl" placeholder="debt-free capital goods companies with ROCE above 20% near their 52-week high" onkeydown="if(event.key==='Enter')ask()"></div><button type="button" onclick="ask()">Write the query</button></div>
<div id="nlo" class="muted" role="status" style="margin:4px 0 6px"></div>
<div class="fld"><label for="q">Screen query</label><textarea id="q" rows="3" style="width:100%;font-family:var(--ax-mono,ui-monospace,monospace)" placeholder="roce_pct > 20 AND debt_equity < 0.5"></textarea></div>
<div class="frm">
<div class="fld"><label for="bf">Field</label><select id="bf" class="w-l"></select></div>
<div class="fld"><label for="bo">Condition</label><select id="bo"><option>&gt;</option><option>&gt;=</option><option>&lt;</option><option>&lt;=</option><option>=</option><option>!=</option></select></div>
<div class="fld"><label for="bv">Value</label><input id="bv" class="w-m" onkeydown="if(event.key==='Enter')addCond('AND')"></div>
<button type="button" class="ghost" onclick="addCond('AND')">+ AND</button><button type="button" class="ghost" onclick="addCond('OR')">+ OR</button></div>
<div class="frm">
<div class="fld"><label for="sort">Sort by</label><select id="sort" class="w-l"><option value="">The first field in the query</option></select></div>
<div class="fld"><label for="dir">Order</label><select id="dir"><option value="1">High → low</option><option value="0">Low → high</option></select></div>
<button type="button" onclick="go()">Run screen</button><a id="csv" href="#" style="margin-left:6px">Download CSV</a></div>
<details style="margin-top:8px"><summary class="muted">All fields</summary><div id="help" class="scroll" tabindex="0" role="region" aria-label="All screener fields" style="margin-top:6px"></div></details></div>
<div class="card"><h2 id="rh">Results</h2>
__LEGEND__
<div id="res" aria-live="polite"><div class="emptyst">Pick a preset or write a query, then press <b>Run screen</b>.</div></div></div>
<div class="card"><h2>Saved screens</h2>
<div class="frm"><div class="fld"><label for="sn">Screen name</label><input id="sn" class="w-l"></div>
<label class="chk"><input type="checkbox" id="snt"> Alert me on new matches (daily, 20:50 IST)</label><button type="button" onclick="save()">Save current query</button><span id="so" class="muted" role="status"></span></div>
<div id="saved" style="margin-top:8px">__SK__</div></div>
<div id="sgl" hidden data-json="__GLOSS__"></div></div><div id="tip"></div>
<script>""" + _JS_COMMON + r"""
let CAT=null,LAST=null,SORT=null,DESC=1;
const GL=JSON.parse(document.getElementById('sgl').dataset.json||'{}');
const fmt=(k,v)=>{if(v==null)return '<span class="muted">—</span>';const f=CAT.fields[k]||{};
 if(k==='symbol')return `<a href="/research?symbol=${encodeURIComponent(v)}">${esc(v)}</a>`;
 if(k==='research_rating')return pill(v);if(k==='tech_rating_label')return `<span style="white-space:nowrap">${esc(String(v).replace('_',' '))}</span>`;if(f.kind==='text')return esc(v);
 if(f.unit==='%')return n(v,1)+'%';if(f.unit==='pts')return (v>0?'+':'')+n(v,2);if(f.unit==='₹ cr')return n(v,0);if(f.unit==='₹')return n(v,2);if(f.unit==='x')return n(v,2);return n(v,2)};
(async()=>{let c;try{c=await j('/api/screener/fields')}catch(e){document.getElementById('presets').innerHTML=errbox(e,'the screener fields','location.reload()');return}
 CAT={...c,fields:{}};
 Object.values(c.groups).flat().forEach(f=>CAT.fields[f.key]=f);
 const opts=Object.entries(c.groups).map(([g,fs])=>`<optgroup label="${esc(g)}">${fs.map(f=>`<option value="${f.key}">${esc(f.label)}${f.unit?' ('+esc(f.unit)+')':''}</option>`).join('')}</optgroup>`).join('');
 document.getElementById('bf').innerHTML=opts;document.getElementById('bf').value='roce_pct';document.getElementById('sort').innerHTML='<option value="">The first field in the query</option>'+opts;
 const G={fundamental:'Fundamental',technical:'Technical',combined:'Fundamental + technical'};
 document.getElementById('presets').innerHTML=Object.keys(G).map(g=>`<div class="pgl muted">${G[g]}</div><div class="presets">${c.presets.map((p,i)=>p.group===g?`<button type="button" class="chip" title="${esc(p.description)}" onclick="preset(${i})">${esc(p.name)}</button>`:'').join('')}</div>`).join('');
 document.getElementById('help').innerHTML=Object.entries(c.groups).map(([g,fs])=>`<h3>${esc(g)}</h3>`+table(['Field','Name','Unit','Notes'],fs.map(f=>`<tr><td><code>${f.key}</code></td><td>${esc(f.label)}</td><td>${esc(f.unit||f.kind)}</td><td class="muted">${esc([GL[f.key],f.description,f.aliases.length?'also: '+f.aliases.join(', '):''].filter(Boolean).join(' · '))}</td></tr>`),{plain:1})).join('');
 saved();const q=new URLSearchParams(location.search).get('q');if(q){document.getElementById('q').value=q;go()}})();
function preset(i){const p=CAT.presets[i];document.getElementById('q').value=p.query;document.getElementById('sort').value=p.sort||'';document.getElementById('dir').value=p.desc===false?'0':'1';go()}
function addCond(join){const f=val('bf'),o=val('bo'),v=val('bv').trim();if(!v){document.getElementById('bv').focus();return}const isText=CAT&&CAT.fields[f]&&CAT.fields[f].kind==='text';
 const lit=isText&&!/^".*"$/.test(v)?`"${v.replace(/"/g,'')}"`:v;const q=document.getElementById('q');q.value=(q.value.trim()?q.value.trim()+` ${join} `:'')+`${f} ${o} ${lit}`;document.getElementById('bv').value=''}
const val=id=>document.getElementById(id).value;
function params(extra){const p=new URLSearchParams({query:val('q').trim(),desc:val('dir'),limit:'500',...(extra||{})});if(val('sort'))p.set('sort',val('sort'));return p}
async function ask(){const t=val('nl').trim();if(!t)return;const o=document.getElementById('nlo');o.textContent='Translating…';
 try{const r=await post('/api/screener/ask',{text:t});document.getElementById('q').value=r.query||'';
  if(r.sort){const s=document.getElementById('sort');if(![...s.options].some(x=>x.value===r.sort))s.add(new Option(r.sort,r.sort));s.value=r.sort;document.getElementById('dir').value=r.desc?'1':'0'}
  o.innerHTML=`${r.mode==='claude'?'Written by Claude':'Written by the rule translator'+(r.fallback_reason?` <span title="${esc(r.fallback_reason)}">(Claude not used: ${esc(r.fallback_reason)})</span>`:'')}. ${r.valid?'Check the query, then <b>Run screen</b>.':`<span class="bad">${esc(r.error||'not a valid query')}</span>`}${r.explanation?' · '+esc(r.explanation):''}${(r.unmatched||[]).length?` · <span class="note">not understood: ${esc(r.unmatched.join(', '))}</span>`:''}`}
 catch(e){o.innerHTML=`<span class="bad">${esc(e.message)}</span>`}}
async function go(){const q=val('q').trim(),el=document.getElementById('res');if(!q){el.innerHTML='<div class="emptyst">Write a query first, or pick a preset above.</div>';return}
 el.innerHTML=skel('the screen',5);
 document.getElementById('csv').href='/api/screener/run.csv?'+params({limit:'2000'});
 try{draw(await j('/api/screener/run?'+params()))}catch(e){el.innerHTML=`<div class="errst" role="alert"><b>The screen did not run.</b> ${esc(e.message)}. Check the field names under <i>All fields</i>.</div>`;document.getElementById('rh').textContent='Results'}}
function draw(r,note){LAST=r;document.getElementById('rh').innerHTML=`Results: <b>${r.count}</b> of ${r.universe} stocks${r.count>r.rows.length?` (showing ${r.rows.length})`:''} <span class="muted">sorted by ${esc((CAT.fields[r.sort]||{}).label||r.sort)} ${r.desc?'↓':'↑'}</span>${note||''}`;
 const head=r.columns.map(c=>{const f=CAT.fields[c]||{},lab=esc(f.label||c),on=r.sort===c,t=GL[c]?`<abbr title="${esc(GL[c])}">${lab}</abbr>`:lab;
  return `<th scope="col"${on?` aria-sort="${r.desc?'descending':'ascending'}"`:''}><button type="button" class="thb" title="${esc(GL[c]||f.description||('Sort by '+(f.label||c)))}" onclick="resort('${esc(c)}')">${t}${on?(r.desc?' ↓':' ↑'):''}</button></th>`}).join('');
 const rows=r.rows.map(x=>`<tr>${r.columns.map(c=>`<td>${fmt(c,x[c])}</td>`).join('')}</tr>`).join('');
 document.getElementById('res').innerHTML=`<div class="tw ax-scrollx" tabindex="0" role="region" aria-label="Screen results"><table><thead><tr>${head}</tr></thead><tbody>${rows||`<tr><td colspan="${r.columns.length}" class="empty">No stock matches every condition. Loosen one (a lower ROCE, say), or check that the fields you used have data: a stock missing a field never matches.</td></tr>`}</tbody></table></div>`}
function resort(c){const s=document.getElementById('sort');document.getElementById('dir').value=(s.value===c&&val('dir')==='1')?'0':'1';s.value=c;go()}
async function saved(){const el=document.getElementById('saved');
 try{const d=await j('/api/screener/saved');window.SAVED=d;
  el.innerHTML=table(['Name','Query','Last run','Matches','Alerts',SR('Actions')],d.map(x=>`<tr><td>${esc(x.name)}</td><td><code>${esc(x.query)}</code></td><td>${esc(String(x.last_run_at||'—').slice(0,16))}</td><td>${x.last_count??'—'}</td><td>${x.notify?'on':'off'}</td><td style="white-space:nowrap"><button type="button" class="ghost" aria-label="Run ${esc(x.name)}" onclick="runSaved('${x.screen_id}')">Run</button><button type="button" class="ghost" aria-label="Edit ${esc(x.name)}" onclick="loadSaved('${x.screen_id}')">Edit</button><button type="button" class="ghost" title="Delete" aria-label="Delete ${esc(x.name)}" onclick="del('${x.screen_id}')">✕</button></td></tr>`),
   {label:'Saved screens',empty:'No saved screens yet: write a query above, give it a name and press <b>Save current query</b>.'})}
 catch(e){el.innerHTML=errbox(e,'the saved screens','saved()')}}
function loadSaved(id){const x=window.SAVED.find(s=>s.screen_id===id);document.getElementById('q').value=x.query;document.getElementById('sort').value=x.sort||'';document.getElementById('dir').value=x.descending?'1':'0';document.getElementById('sn').value=x.name;document.getElementById('snt').checked=!!x.notify;window.EDIT=id}
async function runSaved(id){document.getElementById('res').innerHTML=skel('the saved screen',5);
 try{const r=await j(`/api/screener/saved/${id}/run`);loadSaved(id);draw(r,r.new.length||r.dropped.length?` · <span class="ok">new: ${esc(r.new.join(', ')||'none')}</span> · <span class="note">dropped: ${esc(r.dropped.join(', ')||'none')}</span>`:'');saved()}
 catch(e){document.getElementById('res').innerHTML=errbox(e,'the saved screen',`runSaved('${id}')`)}}
async function save(){try{const x=await post('/api/screener/saved',{name:val('sn').trim(),query:val('q').trim(),sort:val('sort')||null,desc:val('dir')==='1',notify:document.getElementById('snt').checked,screen_id:window.EDIT||null});window.EDIT=x.screen_id;document.getElementById('so').textContent=' saved';saved()}catch(e){document.getElementById('so').textContent=' '+e.message}}
async function del(id){if(!confirm('Delete this saved screen?'))return;try{await post(`/api/screener/saved/${id}/delete`);if(window.EDIT===id)window.EDIT=null;saved()}catch(e){document.getElementById('so').textContent=' '+e.message}}
</script></body></html>"""


SIGNALS = _HEAD + r"""
<div class="wrap"><div class="tabs" id="tabs" role="tablist" aria-label="Signal views"></div>
<div class="pane" id="p-today" role="tabpanel" aria-labelledby="tab-today"><div class="card"><h2>Technical signals <span class="muted" id="asof"></span></h2>
<div class="muted">End-of-day scans for the next session (research/technicals.py): crossovers, breakouts on volume, oscillator turns, Supertrend, squeezes, trend templates. Each signal has an entry (the close), a stop 2×ATR away and a target 4×ATR away (2R), and a <b>confluence</b> count of independent agreeing evidence out of 6: technical rating, volume, relative strength vs Nifty, market regime, a candle pattern, the research rating. Each also carries the <b>market gate</b> it was born under: a long signal while the gate is closed is <i>against the market</i>. The <b>Weekly</b> column is the same rating on completed weekly bars: ✓ when it is on the signal's side. The <b>75m</b> column is the same rating on the session's completed 75-minute bars (built from the stored 15-minute bars): ✓ when it agrees with the daily rating, ✗ when it is on the other side, ~ when one of the two is neutral. In brackets after each scan: its record so far in today's market, how often it beat the Nifty over 20 sessions · its median excess (grey: fewer than 10 signals; * across all markets). Not advice.</div>
<div id="metanote" class="muted" style="display:none;margin-top:4px"><b>Meta-label P · size</b> (meta-labelling is switched on and an adopted model scored today's signals): the model's probability that the signal reaches its target before its stop or horizon, judged from what was known at the close (the best of the stock's scans; hover for each), and the suggested size 0–1 from that probability (AFML bet sizing). Green: at or above the model's threshold. Tested out of sample before adoption (<a href="/api/research/meta-label/report">report</a>); information only, never used to size an order.</div>
<div id="gate" style="margin-top:8px" aria-live="polite"></div>
<div class="frm"><div class="fld"><label for="dirf">Direction</label><select id="dirf" onchange="today()"><option value="">Bullish and bearish</option><option value="BULL">Bullish</option><option value="BEAR">Bearish</option></select></div>
<div class="fld"><label for="alf">Market filter</label><select id="alf" onchange="draw()"><option value="not_against" selected>Hide signals against the market</option><option value="">All signals</option><option value="WITH">Only with the market</option></select></div>
<div class="fld"><label for="mc">Minimum confluence</label><select id="mc" onchange="today()"><option>0</option><option>1</option><option selected>2</option><option>3</option><option>4</option><option>5</option><option>6</option></select></div>
<button type="button" onclick="runNow()">Recompute now</button><span id="ro" class="muted" role="status"></span></div></div>
<div class="card"><div id="sig">__SK__</div></div></div>
<div class="pane" id="p-rec" role="tabpanel" aria-labelledby="tab-rec"><div class="card"><h2>Track record per scan</h2>
<div class="muted">Every signal is followed until it reaches its target (win), its stop, or 20 sessions (expired at the close). Average R: +2 is a target, −1 a stop. Expectancy above 0 means the scan has paid on ATIP's own stocks; a few dozen closed signals are needed before trusting it. Chart-pattern scans (Darvas box, VCP, double bottom, ascending and descending triangles, head and shoulders and its inverse, rising and falling channels and wedges) are tracked from day one but only alert once 30 of their signals have closed with a positive average R.</div>
<div class="frm"><div class="fld"><label for="rc">Only signals with confluence of at least</label><select id="rc" onchange="rec()"><option>0</option><option>1</option><option>2</option><option>3</option><option>4</option></select></div>
<div class="fld"><label for="ra">Born in</label><select id="ra" onchange="rec()"><option value="">born in any market</option><option value="WITH">born with the market</option><option value="MIXED">born in a mixed market</option><option value="AGAINST">born against the market</option></select></div></div>
<h3>Does the market gate help?</h3><div id="geff">__SK__</div>
<h3>Per scan</h3>
<div id="stats" style="margin-top:4px">__SK__</div></div>
<div class="card"><h2>Against the Nifty, by market and holding period</h2>
<div class="muted">Separately from the stop and target, every signal's return is recorded 5, 20 and 60 sessions later and compared with the Nifty over the same days (signed for its direction). Each cell: how often the signal beat the Nifty · its median excess return (number of signals). Split by the market gate the signal was born under. Grey cells have fewer than 10 signals: too few to judge.</div>
<div class="frm"><div class="fld"><label for="rh">Holding period</label><select id="rh" onchange="fwd()"><option value="5">5 sessions</option><option value="20" selected>20 sessions</option><option value="60">60 sessions</option></select></div><span class="muted">(the confluence filter above applies)</span></div>
<div id="fwall" style="margin-top:8px">__SK__</div>
<h3>Does confluence add?</h3><div id="fwconf"></div>
<h3>Does the weekly trend add?</h3><div id="fwweek"></div>
<h3>Per scan</h3><div id="fwscan"></div><div id="fwnote" class="muted" style="margin-top:4px"></div></div></div>
<div class="pane" id="p-intra" role="tabpanel" aria-labelledby="tab-intra"><div class="card"><h2>Intraday signals</h2>
<div class="frm"><div class="fld"><label for="idate">Session date</label><input id="idate" type="date" onchange="intra()"></div><button type="button" onclick="iscan()">Scan now</button><span id="io" class="muted" role="status"></span></div>
<div class="muted" style="margin-top:6px">On the 15-minute bars ATIP stores every 30 minutes (Dhan intraday data): opening-range breakout / breakdown (the first close beyond the 09:15–09:30 range, by 11:30, on 1.5× the slot's usual volume), open = low / open = high (judged on the first hour: no trade more than 0.1 % beyond the open and 0.5 %+ away from it by 10:15), and the TTM squeeze on 15-minute bars (on 1.2× volume). Each is measured from the price when ATIP saw it, to the session's close, and against the Nifty over the same minutes. RVol: the trigger bar's volume over the same slot in the last 5 sessions. A scan alerts only after 30 closed hits with a positive record. Times are IST. Not advice.</div>
<div id="isig" style="margin-top:6px">__SK__</div></div>
<div class="card"><h2>Intraday record per scan</h2><div id="istat">__SK__</div></div></div>
</div><div id="tip"></div>
<script>""" + _JS_COMMON + r"""
const TABS=[['today','Today'],['rec','Track record'],['intra','Intraday']];const loaded={};
function show(id){showTab(id);if(!loaded[id]){loaded[id]=1;({today,rec,intra})[id]()}}
const cellp=c=>c?`${c.positive_pct}% · ${pct(c.median_pct)} <span class="muted">(${c.n})</span>`:'<span class="muted">—</span>';
async function intra(){const d=document.getElementById('idate').value,el=document.getElementById('isig');el.innerHTML=skel('the intraday signals',3);
 try{const rows=await j('/api/signals/intraday'+(d?`?date=${d}`:''));
 el.innerHTML=table(['Symbol',SR('Direction'),'Scan','Bar','Seen at','Seen price','Level','RVol','Why','To the close','vs Nifty','Alerts'],rows.map(x=>`<tr><td><a href="/research?symbol=${encodeURIComponent(x.symbol)}">${esc(x.symbol)}</a></td><td>${dpill(x.direction)}</td><td>${esc(x.name)}</td><td>${esc(String(x.bar_ts||'').slice(11,16))}</td><td>${esc(String(x.seen_at||'').slice(11,16))}</td><td>${n(x.seen_price,2)}</td><td>${n(x.level,2)}</td><td>${x.rvol_slot==null?'—':n(x.rvol_slot,2)+'×'}</td><td class="muted">${esc(x.reason)}</td><td>${pct(x.ret_close_pct)}</td><td>${pct(x.excess_close_pct)}</td><td>${x.alerts==='on'?'<span class="ok">on</span>':'<span class="muted">held</span>'}</td></tr>`),
  {label:'Intraday signals',empty:`No intraday scan fired ${d?'on '+esc(d):'today'}: the scans run on the 15-minute bars ATIP stores during market hours (09:15–15:30 IST). Press <b>Scan now</b> to run them on what is stored.`})}
 catch(e){el.innerHTML=errbox(e,'the intraday signals','intra()')}
 const se=document.getElementById('istat');se.innerHTML=skel('the intraday record',2);
 try{const st=await j('/api/signals/intraday/stats');
 se.innerHTML=table(['Scan',SR('Direction'),'Hits','Closed','Made money · median (n)','Beat the Nifty · median excess (n)','Avg best / worst after seen','Alerts'],st.map(s=>`<tr><td>${esc(s.name)}</td><td>${dpill(s.direction)}</td><td>${s.hits}</td><td>${s.closed}${s.enough?'':' <span class="muted">(too few)</span>'}</td><td>${cellp(s.to_close)}</td><td>${cellp(s.vs_nifty)}</td><td>${s.avg_best_pct==null?'—':pct(s.avg_best_pct)+' / '+pct(s.avg_worst_pct)}</td><td>${s.alerts==='on'?'<span class="ok">on</span>':`<span class="muted">held · ${s.closed}/30</span>`}</td></tr>`),
  {label:'Intraday record per scan',empty:'No intraday scan has fired yet, so there is no record: it builds from the first hits.'})}
 catch(e){se.innerHTML=errbox(e,'the intraday record','intra()')}}
async function iscan(){const o=document.getElementById('io');o.textContent=' scanning…';try{await post('/api/signals/intraday/run');o.textContent=' done';intra()}catch(e){o.textContent=' '+e.message}}
tabs(TABS,'tabs');
const dpill=d=>`<span class="pill ${d==='BULL'?'BUY':d==='BEAR'?'SELL':'NOT_RATED'}">${d==='BULL'?'▲ bull':d==='BEAR'?'▼ bear':esc(d)}</span>`;
let SIG=[];
const wkc=x=>!x.tech_rating_w_label?'<span class="muted" title="needs 35+ completed weeks of history">—</span>':`<span class="${x.weekly_agrees===1?'ok':x.weekly_agrees===0?'bad':''}" title="${x.weekly_agrees===1?'the weekly rating is on this signal\'s side':'the weekly rating is not on this signal\'s side'}">${x.weekly_agrees===1?'✓ ':'✗ '}${esc(x.tech_rating_w_label.replace('_',' '))}</span>`;
const c75=x=>{const l=x.tech_rating_75_label,d=x.tech_rating_label||'';if(!l)return '<span class="muted" title="no 75-minute rating for this session: it needs 35+ completed 75-minute bars (7 sessions of 15-minute bars)">—</span>';
 const sd=v=>/BUY/.test(v)?1:/SELL/.test(v)?-1:0,a=sd(l),b=sd(d),ok=x.mtf_alignment_75==='BULL'||x.mtf_alignment_75==='BEAR',opp=a!==0&&b!==0&&a!==b;
 const t=`75-minute rating ${l.replace('_',' ')} on the bar to ${x.bar_75_end||'?'}; daily ${d?d.replace('_',' '):'—'}: `+(ok?'they agree':opp?'they disagree':'one of the two is neutral');
 return `<span class="${ok?'ok':opp?'bad':'muted'}" title="${esc(t)}">${ok?'✓ ':opp?'✗ ':'~ '}${esc(l.replace('_',' '))}</span>`};
const recb=s=>{const r=s.record;if(!r)return '';const m=(r.median_excess_pct>0?'+':'')+r.median_excess_pct;
 const t=`Over ${s.record_horizon} sessions ${s.record_scope==='gate'?'in a market with the gate '+(s.market_gate||'').toLowerCase():'in all markets (none yet in this one)'}: beat the Nifty ${r.beat_nifty_pct}% of ${r.n} times, median excess ${m}%${r.enough?'':'. Too few to judge.'}`;
 return ` <span class="${r.enough?(r.median_excess_pct>0?'ok':'bad'):'muted'}" style="font-size:12px;white-space:nowrap" title="${esc(t)}">[${r.beat_nifty_pct}% · ${m}%${s.record_scope==='gate'?'':'*'}]</span>`};
async function gate(){const el=document.getElementById('gate');try{const g=await j('/api/market-regime');
 if(!g.gate){el.innerHTML=`<span class="muted">Market gate: ${esc(g.reason||'not computed yet: it runs nightly with the technical signals')}</span>`;return}
 const long=g.gate==='OPEN'?'long signals are <b>with</b> the market':g.gate==='CAUTION'?'mixed market: long signals are <b>mixed</b>, size down':'long signals are <b>against</b> the market; short signals are with it';
 el.innerHTML=`Market gate ${gpill(g.gate)} <b>${esc(stl(g.status))}</b> since ${esc(g.status_since||'—')} · ${g.dd_count} distribution day${g.dd_count===1?'':'s'} in ${g.rules.dd_window} sessions · Nifty ${g.above_200dma==null?'(200-DMA n/a)':g.above_200dma?'above':'below'} its 200-DMA · ${long}. <a href="/market-pulse">Details</a>`}
 catch(e){el.innerHTML=`<span class="muted">The market gate could not be loaded (${esc(e.message)}); the signals below are unaffected.</span>`}}
async function today(){const el=document.getElementById('sig');el.innerHTML=skel('the signals',5);const p=new URLSearchParams({min_confluence:document.getElementById('mc').value});const d=document.getElementById('dirf').value;if(d)p.set('direction',d);
 try{SIG=await j('/api/signals/technical?'+p);draw()}catch(e){el.innerHTML=errbox(e,'the signals','today()');document.getElementById('asof').textContent=''}}
function draw(){const af=document.getElementById('alf').value,r=SIG.filter(x=>!af||(af==='not_against'?x.alignment!=='AGAINST':x.alignment===af));
 const hidden=SIG.length-r.length;const g={};r.forEach(x=>{const k=x.symbol+'|'+x.direction;(g[k]=g[k]||{...x,list:[]}).list.push(x)});
 const rows=Object.values(g).sort((a,b)=>b.confluence-a.confluence||b.list.length-a.list.length||a.symbol.localeCompare(b.symbol));
 document.getElementById('asof').textContent=SIG.length?`· ${String(SIG[0].date).slice(0,10)} · ${rows.length} stocks, ${r.length} signals${hidden?` (${hidden} hidden by the market filter)`:''}`:'· no signals yet';
 const META=SIG.some(s=>s.meta_prob!=null);document.getElementById('metanote').style.display=META?'':'none';
 const why=SIG.length?'Every signal is hidden by the market filter: choose <b>All signals</b> to see them.':`No signal has a confluence of ${esc(document.getElementById('mc').value)} or more for the latest session: lower the minimum confluence, or press <b>Recompute now</b>.`;
 document.getElementById('sig').innerHTML=table(['Symbol',SR('Direction'),'Market','Signals','Entry','Stop','Target','Confluence','Evidence','Rating','Weekly','75m','Patterns',...(META?['Meta-label P · size']:[])],rows.map(x=>`<tr><td><a href="/research?symbol=${encodeURIComponent(x.symbol)}">${esc(x.symbol)}</a></td><td>${dpill(x.direction)}</td><td>${apill(x.alignment)}</td><td>${x.list.map(s=>`<span title="${esc(s.reason||'')}">${esc(s.name)}</span>${recb(s)}`).join(' · ')}</td><td>${n(x.entry,2)}</td><td>${n(x.stop,2)}</td><td>${n(x.target,2)}</td><td><b>${x.confluence}</b>/6</td><td class="muted">${Object.entries(x.evidence||{}).filter(([k,v])=>v).map(([k])=>esc(k)).join(', ')}</td><td style="white-space:nowrap">${esc((x.tech_rating_label||'').replace('_',' '))}</td><td style="white-space:nowrap">${wkc(x)}</td><td style="white-space:nowrap">${c75(x)}</td><td class="muted">${esc(x.patterns||'')}</td>${META?`<td>${metac(x)}</td>`:''}</tr>`),
  {label:"Today's technical signals",empty:why})}
// meta-labelling: the best P(target first) among the stock's scans and its suggested size (information only)
const metac=x=>{const m=x.list.filter(s=>s.meta_prob!=null);if(!m.length)return '<span class="muted">—</span>';const b=m.reduce((a,s)=>s.meta_prob>a.meta_prob?s:a);
 const t=m.map(s=>`${s.name}: P(target first) ${Math.round(100*s.meta_prob)}%, suggested size ${(+s.meta_size).toFixed(1)}${s.meta_kept?'':' (below the threshold)'}`).join('; ')+` · ${b.meta_model||''}`;
 return `<span class="${b.meta_kept?'ok':'muted'}" style="white-space:nowrap" title="${esc(t)}">${Math.round(100*b.meta_prob)}% · ${(+b.meta_size).toFixed(1)}</span>`};
const fcell=c=>!c?'<span class="muted">—</span>':`<span class="${c.enough?(c.median_excess_pct>0?'ok':'bad'):'muted'}" style="white-space:nowrap">${c.beat_nifty_pct}% · ${c.median_excess_pct>0?'+':''}${c.median_excess_pct}%</span> <span class="muted">(${c.n})</span>`;
const GCOLS=[['ALL','All markets'],['OPEN','Gate open'],['CAUTION','Caution'],['CLOSED','Gate closed']];
const NOFWD='No signal is old enough to measure yet: each needs the full holding period behind it.';
async function fwd(){const h=document.getElementById('rh').value,mc=document.getElementById('rc').value;document.getElementById('fwall').innerHTML=skel('the record against the Nifty',2);
 try{const f=await j(`/api/signals/technical/forward?horizon=${h}&min_confluence=${mc}`);
 document.getElementById('fwall').innerHTML=table(['All signals',...GCOLS.map(g=>g[1])],[`<tr><td class="muted">beat the Nifty · median excess (n)</td>${GCOLS.map(([k])=>`<td>${fcell(f.overall[k])}</td>`).join('')}</tr>`],'All signals against the Nifty');
 document.getElementById('fwconf').innerHTML=table(['Confluence',...GCOLS.map(g=>g[1])],f.by_confluence.map(b=>`<tr><td>${esc(b.band)} of 6</td>${GCOLS.map(([k])=>`<td>${fcell(b.cells[k])}</td>`).join('')}</tr>`),{label:'Against the Nifty by confluence',empty:NOFWD});
 const WK={AGREES:'Weekly rating on the signal\'s side',DISAGREES:'Weekly rating not on its side',NO_WEEKLY:'No weekly rating yet'};
 document.getElementById('fwweek').innerHTML=table(['Weekly trend',...GCOLS.map(g=>g[1])],f.by_weekly.map(b=>`<tr><td>${esc(WK[b.weekly])}</td>${GCOLS.map(([k])=>`<td>${fcell(b.cells[k])}</td>`).join('')}</tr>`),{label:'Against the Nifty by weekly trend',empty:NOFWD});
 document.getElementById('fwscan').innerHTML=table(['Scan',SR('Direction'),...GCOLS.map(g=>g[1])],f.by_scan.map(o=>`<tr><td>${esc(o.name)}</td><td>${dpill(o.direction)}</td>${GCOLS.map(([k])=>`<td>${fcell(o.cells[k])}</td>`).join('')}</tr>`),{label:'Against the Nifty per scan',empty:NOFWD});
 document.getElementById('fwnote').textContent=f.note}
 catch(e){document.getElementById('fwall').innerHTML=errbox(e,'the record against the Nifty','fwd()')}}
async function rec(){fwd();const mc=document.getElementById('rc').value,al=document.getElementById('ra').value;
 document.getElementById('geff').innerHTML=skel('the gate effect',2);document.getElementById('stats').innerHTML=skel('the track record',4);
 try{const [r,ge]=await Promise.all([j('/api/signals/technical/stats?min_confluence='+mc+(al?'&alignment='+al:'')),j('/api/signals/technical/gate-effect?min_confluence='+mc)]);
 document.getElementById('geff').innerHTML=table(['Signals born','Open','Closed','Win rate','Avg R','Avg return'],ge.groups.map(o=>`<tr><td>${o.alignment==='UNKNOWN'?'<span class="muted">before the gate</span>':apill(o.alignment)}</td><td>${o.open}</td><td>${o.closed}</td><td>${o.win_rate_pct==null?'—':o.win_rate_pct+'%'}</td><td>${o.avg_r==null?'—':(o.avg_r>0?'+':'')+o.avg_r}</td><td>${pct(o.avg_return_pct)}</td></tr>`),{label:'Does the market gate help',empty:'No signal has closed yet, so the gate cannot be judged.'})+`<div class="muted" style="margin-top:4px">${esc(ge.verdict||ge.note)}</div>`;
 document.getElementById('stats').innerHTML=table(['Scan',SR('Direction'),'Open','Closed','Target','Stopped','Expired','Win rate','Avg R','Avg return','Alerts'],r.map(o=>`<tr><td>${esc(o.name)}</td><td>${dpill(o.direction)}</td><td>${o.open}</td><td>${o.closed}</td><td>${o.target}</td><td>${o.stopped}</td><td>${o.expired}</td><td>${o.win_rate_pct==null?'—':o.win_rate_pct+'%'}</td><td>${o.avg_r==null?'—':(o.avg_r>0?'+':'')+o.avg_r}</td><td>${pct(o.avg_return_pct)}</td><td>${o.alerts==='held'?`<span class="muted" style="white-space:nowrap" title="a chart-pattern or post-earnings-drift scan alerts once 30 of its signals have closed with a positive average R">held · ${Math.min(o.closed,30)}/30</span>`:'on'}</td></tr>`),
  {label:'Track record per scan',empty:al?'No signal was born in that market with this confluence: choose <i>born in any market</i> or a lower confluence.':'No signal has been recorded with this confluence yet: lower the minimum, or wait for the first signals to be scanned.'})}
 catch(e){document.getElementById('stats').innerHTML=errbox(e,'the track record','rec()');document.getElementById('geff').innerHTML=''}}
async function runNow(){document.getElementById('ro').textContent=' computing…';try{const o=await post('/api/signals/technical/run');document.getElementById('ro').textContent=` ${o.rows} stocks, ${o.signals} signals (${o.as_of})`;gate();today()}catch(e){document.getElementById('ro').textContent=' '+e.message}}
gate();show('today');
</script></body></html>"""


PULSE = _HEAD + r"""
<div class="wrap">
<div class="card"><h2>Market context <span id="ctx"></span> <span class="muted" id="asof"></span></h2>
<div id="reasons">__SK__</div>
<div class="frm"><button type="button" onclick="act('/api/market-pulse/gift','GIFT Nifty captured')">Capture GIFT Nifty now</button><button type="button" onclick="act('/api/market-regime/run','Market gate recomputed').then(gateCard)">Recompute market gate</button><button type="button" onclick="act('/api/market-pulse/refresh','Positioning and Nifty history fetched')">Fetch NSE positioning + Nifty history</button><button type="button" onclick="act('/api/orderbook/snapshot','Order book polled')">Poll order book now</button><span id="ao" class="muted" role="status"></span></div>
<div class="muted" style="margin-top:4px">Built from ATIP's own models and stored data; each part reports its own track record. Times are IST. Not advice.</div></div>
<div class="card"><h2>Market gate <span id="gpill"></span> <span class="muted" id="gsince"></span></h2>
<div class="muted">Should new long signals be taken? An IBD-style read of the Nifty from ATIP's data: distribution days (down sessions on higher market volume), corrections, rally attempts and follow-through days, plus the 200-DMA. <a href="/signals">Signals</a> are tagged with, mixed or against it, and the track record shows whether that helps.</div>
<div class="two" style="margin-top:8px"><div id="gsum" class="sx">__SK__</div><div id="gchanges" class="sx"></div></div>
<div id="gchart" style="margin-top:6px;position:relative"></div>
<div class="legend"><span><i class="s1"></i>Nifty 50</span><span><i class="s2"></i>200-DMA</span><span>▼ distribution day</span><span>▲ follow-through day</span><span><b class="sw g-OPEN"></b>gate open</span><span><b class="sw g-CAUTION"></b>caution</span><span><b class="sw g-CLOSED"></b>closed</span></div>
<details><summary class="muted">Table view (last 30 sessions)</summary><div id="gtable" class="sx"></div></details></div>
<div class="card"><h2>Event calendar</h2>
<div class="muted">US Fed decisions, US CPI and the US jobs report come out after India's close, so they hit the next session's open; the RBI decides at 10:00 IST, during the session. On a morning after a US release the gap estimate is given a wider band.</div>
<div id="evs" class="sx" style="margin-top:6px">__SK__</div>
<details style="margin-top:6px"><summary class="muted">Add an event (an unscheduled meeting, next year's dates)</summary><div class="frm"><div class="fld"><label for="evd">Date</label><input id="evd" type="date"></div><div class="fld"><label for="evk">Event</label><select id="evk"><option value="">Loading…</option></select></div><div class="fld"><label for="evt">Title (optional)</label><input id="evt" class="w-l"></div><button type="button" onclick="addEv()">Add</button><span id="evo" class="muted" role="status"></span></div></details></div>
<div class="two"><div class="card"><h2>Global cues → Nifty</h2><div id="glob" class="sx">__SK__</div></div>
<div class="card"><h2>FII / DII money</h2><div id="fii" class="sx">__SK__</div></div></div>
<div class="two"><div class="card"><h2>Derivatives positioning</h2><div id="pos" class="sx">__SK__</div></div>
<div class="card"><h2>Pending orders across the market</h2><div id="book" class="sx">__SK__</div></div></div>
<div class="card"><h2>20-level depth (watchlist)</h2>
<div class="muted">Dhan's 20-level order book for up to 50 watchlist stocks, sampled every 15 seconds. DWI weighs the levels within 0.5 % of the mid, nearest first (e<sup>−0.5(k−1)</sup>): +1 all bids, −1 all asks. A stock is flagged when |DWI| &gt; 0.3 in its last 3 snapshots. OFI (order-flow imbalance, Cont, Kukanov &amp; Stoikov) adds up every quote change at the best bid and ask since the stock's previous snapshot, from every update the feed receives: bids added or the bid raised count as buying, asks added or the ask lowered as selling, in units of the average best-level depth. OFI 20 does the same at each of the 20 levels (Xu, Gould &amp; Howison), divided by the book's average depth per level, so stocks compare. Research on NSE stocks finds order-book imbalance predictive for minutes at most; the check below says whether it is on ATIP's own data. Context, not a signal.</div>
<div id="d20" class="sx" style="margin-top:6px">__SK__</div></div>
<div class="card"><h2>Your pending orders</h2><div id="mine">__SK__</div></div>
</div><div id="tip"></div>
<script>""" + _JS_COMMON + r"""
const lab=s=>s?`<span class="pill ${/POSITIVE|INFLOW|RISK_ON|COVERING/.test(s)?'BUY':/NEGATIVE|OUTFLOW|RISK_OFF|CROWDED/.test(s)?'SELL':'NOT_RATED'}">${esc(s.replaceAll('_',' '))}</span>`:'';
const kv=(rows,label)=>table(['',''],rows.filter(r=>r[1]!==undefined).map(([a,b])=>`<tr><th scope="row" class="muted">${a}</th><td>${b}</td></tr>`),{nohead:1,label:label||'Details'});
const cr=v=>v==null||isNaN(v)?'—':(v<0?'−':'')+'₹'+n(Math.abs(v),0)+' cr';
const bar=(v,max)=>{const w=Math.min(100,Math.abs(v)/max*100);return `<div style="display:flex;align-items:center;gap:4px"><div class="track"><span class="${v>=0?'up':'dn'}" style="width:${w/2}%"></span></div><span>${v>=0?'+':'−'}${n(Math.abs(v),3)}%</span></div>`};
const TIM={before_open:'before the open',intraday:'during the session'};
const OVN={FOMC:'the Fed decision',US_CPI:'US CPI',US_NFP:'the US jobs report'};
const ovn=ev=>String(ev||'').split(',').filter(k=>OVN[k]).map(k=>OVN[k]).join(' and ');
function evCard(p){const ev=p.events||{},b=ev.band||{},gr=(p.gap_record||{}).by_events;let h='';
 if((ev.today||[]).length)h+=`<div class="note" style="margin-bottom:6px">Today: ${ev.today.map(e=>`${esc(e.label)} (${esc(e.event_date)}, ${esc(e.time_ist)} IST, ${TIM[e.timing]})`).join('; ')}${b.band_pct!=null?`. Gap band ± ${n(b.band_pct,2)}%: the usual miss of ${n(b.typical_miss_pct,2)}% × ${b.widen} (${esc(b.widen_basis)}).`:''}</div>`;
 h+=table(['Released','Event','Time (IST)','Hits the session of','When'],(ev.upcoming||[]).map(e=>`<tr><td>${esc(e.event_date)}</td><td>${esc(e.label)}</td><td>${esc(e.time_ist)}</td><td>${esc(e.session)}</td><td>${TIM[e.timing]}</td></tr>`),{label:'Upcoming events',empty:'No scheduled event is stored for the coming weeks: add one below when a meeting is announced.'});
 if(gr)h+=`<div class="muted" style="margin-top:6px">Gap-estimate miss after a US release: ${gr.after_us_release.mean_abs_error_pct==null?'—':gr.after_us_release.mean_abs_error_pct+'%'} over ${gr.after_us_release.mornings} mornings, other mornings ${gr.other.mean_abs_error_pct==null?'—':gr.other.mean_abs_error_pct+'%'} over ${gr.other.mornings}. Widening ×${gr.widen.factor} (${esc(gr.widen.basis)}).</div>`;
 document.getElementById('evs').innerHTML=h}
async function addEv(){try{const o=await post('/api/market-pulse/events',{date:document.getElementById('evd').value,kind:document.getElementById('evk').value,title:document.getElementById('evt').value||null});document.getElementById('evo').textContent=` added: ${o.label} hits ${o.session}`;load()}catch(e){document.getElementById('evo').textContent=' '+e.message}}
j('/api/market-pulse/events').then(o=>{document.getElementById('evk').innerHTML=Object.entries(o.kinds).map(([k,l])=>`<option value="${k}">${esc(l)}</option>`).join('')}).catch(e=>{document.getElementById('evk').innerHTML='<option value="">Event kinds unavailable</option>'});
async function load(){let p;try{p=await j('/api/market-pulse')}catch(e){document.getElementById('reasons').innerHTML=errbox(e,'the market pulse','load()');
  ['evs','glob','fii','pos','book'].forEach(id=>{document.getElementById(id).innerHTML='<span class="muted">Not loaded: see the error above.</span>'});mine();depth20().catch(er=>{document.getElementById('d20').innerHTML=errbox(er,'the depth snapshots','depth20()')});return}
 try{evCard(p)}catch(e){document.getElementById('evs').innerHTML=errbox(e,'the event calendar','load()')}
 document.getElementById('asof').textContent='· '+String(p.as_of||'').replace('T',' ');
 document.getElementById('ctx').innerHTML=lab(p.context)+(p.context_score!=null?` <span class="muted">score ${p.context_score}</span>`:'');
 document.getElementById('reasons').innerHTML=(p.reasons||[]).length?`<ul class="b">${p.reasons.map(r=>`<li>${esc(r)}</li>`).join('')}</ul>`:'<span class="muted">Not enough stored data yet: the context fills in once GIFT Nifty, the global markets and the FII flows have been collected (press the buttons below to fetch them now).</span>';
 try{
 const g=p.global_model||{},t=p.gift_today,gr=p.gap_record||{};let h='';
 if(t)h+=kv([['GIFT Nifty move since yesterday 15:30',`${pct(t.gift_move_pct)} <span class="muted">(${esc(t.gift_ref_source||'')})</span>`],['Expected open',t.expected_gap_pct==null?'—':`${pct(t.expected_gap_pct)} · ${n(t.expected_gap_pts,0)} pts`+(t.band_pct!=null?` <span class="muted">± ${n(t.band_pct,2)}% (typical miss${ovn(t.events)?', widened after '+ovn(t.events)+' overnight':''})</span>`:'')],['Captured (IST)',esc(String(t.captured_at).slice(11,16))]],'GIFT Nifty this morning');
 if(g.status==='OK'){h+=`<div style="margin:6px 0">Last night's global moves imply a Nifty move of <b>${pct(g.expected_move_pct)}</b> ${lab(g.cue_label)} <span class="muted">(daily σ ${g.nifty_sigma_pct}%, R² ${g.r2})</span></div>`;
  const mx=Math.max(0.01,...Object.values(g.contributions_pct).map(Math.abs));
  h+=table(['Factor','Last move','Contribution','Sensitivity','Corr 60d'],Object.entries(g.contributions_pct).map(([k,v])=>`<tr><td>${esc(k)}</td><td>${g.inputs[k]?(g.inputs[k].move>0?'+':'')+n(g.inputs[k].move,2)+' '+g.inputs[k].unit:'—'}</td><td>${bar(v,mx)}</td><td class="muted">${g.sensitivity[k]?n(g.sensitivity[k].value,3)+' '+esc(g.sensitivity[k].unit):''}</td><td>${g.corr_60d[k]!=null?n(g.corr_60d[k],2):'—'}</td></tr>`),'Global factors and their contribution');
  const w=g.walk_forward;h+=`<div class="muted" style="margin-top:6px">Walk-forward, last ${w.sessions} sessions: direction right ${w.direction_hit_rate_pct??'—'}% of days that moved > 0.2%; RMSE ${w.rmse_pct}% vs ${w.rmse_zero_forecast_pct}% for "no change" (${w.beats_zero?'beats':'does not beat'} it).</div>`}
 else h+=`<div class="muted">${esc(g.reason||g.status||'The global model has not run yet.')}</div>`;
 if((g.stale_factors||[]).length)h+=`<div class="note" style="margin-top:4px">Left out because their data stopped updating over a week ago: ${esc(g.stale_factors.join(', '))}</div>`;
 const gs=p.global_sync||{};
 if(gs.status==='OK'){const w=gs.walk_forward;h+=`<div style="margin-top:6px">Synchronised model (15:30 → 08:45 IST moves of ${esc(gs.inputs.join(', '))}): ${gs.today?`expected open <b>${pct(gs.today.expected_gap_pct)}</b>`:'no morning snapshot yet'} <span class="muted">· walk-forward over ${w.mornings} mornings: RMSE ${w.rmse_pct}% vs ${w.rmse_zero_pct}% for "no change"${w.rmse_gift_pct!=null?`, ${w.rmse_same_mornings_pct}% vs GIFT's ${w.rmse_gift_pct}%`:''}</span></div>`}
 else if(gs.status)h+=`<div class="muted" style="margin-top:6px">Synchronised model (15:30 → 08:45 IST moves): ${esc(gs.reason||gs.status)}</div>`;
 if(gr.gift||gr.global_model||gr.synchronised)h+=`<div class="muted">Open-gap record: GIFT ${gr.gift?gr.gift.direction_hit_rate_pct+'% right over '+gr.gift.mornings+' mornings':'—'} · daily model ${gr.global_model?gr.global_model.direction_hit_rate_pct+'% over '+gr.global_model.mornings:'—'} · synchronised ${gr.synchronised?gr.synchronised.direction_hit_rate_pct+'% over '+gr.synchronised.mornings:'—'}</div>`;
 document.getElementById('glob').innerHTML=h}catch(e){document.getElementById('glob').innerHTML=errbox(e,'the global cues','load()')}
 try{
 const f=p.fii||{};document.getElementById('fii').innerHTML=f.status!=='OK'?`<span class="muted">${esc(f.reason||f.status||'No FII / DII flows stored yet.')}</span>`:
  `<div style="margin-bottom:6px">Flow pressure ${lab(f.pressure_label)} <span class="muted">score ${f.pressure_score??'—'}</span></div>`+
  kv([['FII / DII net ('+esc(f.as_of)+')',`${cr(f.fii_net_cr)} / ${cr(f.dii_net_cr)}`],['FII 5-day',`${cr(f.fii_5d_cr)} <span class="muted">z ${f.z_fii_5d??'—'}</span>`],['Flow surprise (vs what returns explain)',f.z_surprise==null?'—':`z ${f.z_surprise}`],['FII selling streak',f.selling_streak_days+' days'],['DII absorption (20d)',f.dii_absorption_20d==null?'—':f.dii_absorption_20d+'× FII selling'],['Month to date FII / DII',`${cr(f.fii_mtd_cr)} / ${cr(f.dii_mtd_cr)}`],['Year to date FII / DII',`${cr(f.fii_ytd_cr)} / ${cr(f.dii_ytd_cr)}`],['USD/INR',f.usd_inr?`${f.usd_inr} <span class="muted">(${pct(f.usd_inr_20d_pct)} in 20 days)</span>`:'—']],'FII and DII flows')+
  `<details><summary class="muted">Last 10 days</summary>${table(['Date','FII (₹ cr)','DII (₹ cr)'],(f.last_10||[]).map(x=>`<tr><td>${esc(x.date)}</td><td>${n(x.fii_net_cr,0)}</td><td>${n(x.dii_net_cr,0)}</td></tr>`),'FII and DII, last 10 days')}</details>`}
 catch(e){document.getElementById('fii').innerHTML=errbox(e,'the FII / DII flows','load()')}
 try{
 const o=p.positioning||{},fi=o.fii,nf=o.nifty_futures,pc=o.pcr;let ph='',pr=[];
 if(fi){ph+=`<div style="margin-bottom:6px">${lab(o.read)} <span class="muted">${esc(o.read_note||'')}</span></div>`;pr.push(['FII index futures long %',`${fi.index_futures_long_pct}% <span class="muted">(${fi.change_5d_pp==null?'':(fi.change_5d_pp>0?'+':'')+fi.change_5d_pp+' pp in 5 days, '}percentile ${fi.percentile??'—'} of ${fi.history_days} days)</span>`],['FII net index futures',n(fi.net_index_futures,0)+' contracts'],['FII net index calls / puts',`${n(fi.net_index_calls,0)} / ${n(fi.net_index_puts,0)}`],['Client index futures long %',o.client_index_futures_long_pct==null?'—':o.client_index_futures_long_pct+'%'])}
 else ph+='<div class="muted">No participant OI stored yet: "Fetch NSE positioning" above (NSE serves it to Indian connections).</div>';
 if(nf)pr.push(['Nifty futures ('+esc(nf.as_of)+')',`${esc(nf.buildup.replaceAll('_',' ').toLowerCase())} <span class="muted">price ${pct(nf.price_chg_pct)}, OI ${pct(nf.oi_chg_pct)}</span>`]);
 if(pc)pr.push(['Nifty PCR (OI)',`${pc.value} <span class="muted">z ${pc.z??'—'}, max pain ${n(pc.max_pain,0)}</span>`]);
 if(pr.length)ph+=kv(pr,'Derivatives positioning');
 const W=Object.entries(p.oi_walls||{});if(W.length)ph+=table(['Index','Expiry','Spot','Put wall (support)','Call wall (resistance)'],W.map(([k,x])=>`<tr><td>${esc(k)}</td><td>${esc(x.expiry)}</td><td>${n(x.spot,0)}</td><td>${x.support?n(x.support.strike,0)+' <span class="muted">('+pct(x.support.distance_pct)+')</span>':'—'}</td><td>${x.resistance?n(x.resistance.strike,0)+' <span class="muted">('+pct(x.resistance.distance_pct)+')</span>':'—'}</td></tr>`),'Option open-interest walls');
 document.getElementById('pos').innerHTML=ph}catch(e){document.getElementById('pos').innerHTML=errbox(e,'the positioning','load()')}
 try{
 const b=p.order_book;let bh='';
 if(b&&b.stocks){bh+=kv([['As of',esc(b.as_of)],['Market-wide pending buy / sell',b.market_buy_sell_ratio==null?'—':b.market_buy_sell_ratio+'×'],['Stocks with buyers / sellers dominant',`${b.buyers_dominant} / ${b.sellers_dominant} of ${b.stocks}`],['Persistent buyers',esc(b.persistent_buyers.join(', ')||'none today')],['Persistent sellers',esc(b.persistent_sellers.join(', ')||'none today')]],'Market-wide order book');
  const [bu,se]=await Promise.all([j('/api/orderbook/pressure?side=buy&limit=8'),j('/api/orderbook/pressure?side=sell&limit=8')]);
  const q=v=>v==null?'—':v>=1e5?n(v/1e5,1)+' L':v>=1e3?n(v/1e3,0)+'k':n(v,0);
  const row=x=>`<tr><td><a href="/research?symbol=${encodeURIComponent(x.symbol)}">${esc(x.symbol)}</a></td><td>${q(x.total_buy_qty)}</td><td>${q(x.total_sell_qty)}</td><td>${n(x.total_imbalance,2)}</td><td>${pct(x.chg_pct)}</td></tr>`;
  bh+=`<div class="two" style="margin-top:6px"><div class="sx">${table(['Most bid','Buy','Sell','Imb.','Chg'],bu.map(row),{label:'Most bid stocks',empty:'No stock is bid heavily right now.'})}</div><div class="sx">${table(['Most offered','Buy','Sell','Imb.','Chg'],se.map(row),{label:'Most offered stocks',empty:'No stock is offered heavily right now.'})}</div></div><div class="muted">Buy / Sell: total quantity waiting in the book (L = lakh shares). Imb. = (buy − sell) / (buy + sell).</div><div class="note" style="margin-top:4px">${esc(b.caveat)}</div>`}
 else bh='<span class="muted">No order-book polls today: they run in market hours (09:15–15:30 IST) and need the Dhan Data API.</span>';
 document.getElementById('book').innerHTML=bh}catch(e){document.getElementById('book').innerHTML=errbox(e,'the order book','load()')}
 mine();depth20().catch(e=>{document.getElementById('d20').innerHTML=errbox(e,'the depth snapshots','depth20()')})}
async function depth20(){const o=await j('/api/orderbook/depth20');let h='';
 if(!o.rows.length)h=`<span class="muted">No depth snapshots today: they are taken in market hours (09:15–15:30 IST) while the depth feed runs. Feed: ${esc(o.feed.detail||o.feed.mode||'off')}</span>`;
 else{h=table(['Symbol','Mid','Spread','DWI','OFI','OFI 20','Best level','All 20','Persistent'],o.rows.slice(0,30).map(x=>`<tr><td><a href="/research?symbol=${encodeURIComponent(x.symbol)}">${esc(x.symbol)}</a></td><td>${n(x.mid,2)}</td><td>${n(x.spread_bp,1)} bp</td><td>${n(x.dwi,2)}</td><td>${n(x.ofi_l1_norm,2)}</td><td>${n(x.ofi_ml,2)}</td><td>${n(x.imb_l1,2)}</td><td>${n(x.imb_20,2)}</td><td>${x.persistent?`<span class="${x.persistent==='BUYERS'?'ok':'bad'}">${esc(x.persistent.toLowerCase())}</span>`:''}</td></tr>`),'20-level depth by stock');
  const v=await j('/api/orderbook/depth20/validation?horizon=1'),N={dwi:'DWI',imb_l1:'best level',imb_20:'all 20 levels',ofi_l1:'OFI',ofi_ml:'OFI 20 levels'};
  h+=`<div class="muted" style="margin-top:6px">Does it predict the next minute? ${Object.entries(v.measures).map(([k,m])=>`${N[k]||esc(k)}: ${m.z==null?'too few':`z ${m.z}, sign right ${m.sign_right_pct??'—'}%`}${m.n<v.n?` (n ${m.n})`:''}${m.predictive?' <b>(predictive)</b>':''}`).join(' · ')} <span>(${v.n} snapshot pairs a minute apart; ${v.min_n} and |z| ≥ 2 needed)</span></div>`;
  const c=v.contemporaneous;if(c)h+=`<div class="muted" style="margin-top:4px">OFI against the mid's move over the same interval (a sanity check: Cont, Kukanov &amp; Stoikov find a strong relation): ${Object.entries(c.measures).map(([k,m])=>`${N[k]||esc(k)}: ${m.z==null?'too few':`z ${m.z}, R² ${m.r2==null?'—':n(m.r2*100,0)+'%'}`}${m.related?' <b>(related)</b>':''}`).join(' · ')} <span>(${c.pairs} pairs of consecutive snapshots; R² is the median of each stock's line. OFI 20 counts a quote that moves a tick at every level, so it follows the move closely by construction)</span></div>`}
 document.getElementById('d20').innerHTML=h}
async function mine(){const el=document.getElementById('mine');
 try{const w=await j('/api/brokers/open-orders');const br=w.broker||{},a=w.atip||{};let h=`<div class="muted">Dhan: ${esc(br.status||'unknown')}${br.reason?' · '+esc(br.reason):''}${(br.errors||[]).length?' · '+esc(br.errors.join('; ')):''}</div>`;
 h+=table(['Kind','Symbol','Side','Type','Product','Qty','Filled','Price','Trigger','Status','Created'],(br.orders||[]).map(o=>`<tr><td>${esc(o.kind)}</td><td>${esc(o.symbol)}</td><td>${esc(o.side)}</td><td>${esc(o.order_type)}</td><td>${esc(o.product)}</td><td>${n(o.quantity,0)}</td><td>${n(o.filled,0)}</td><td>${n(o.price,2)}</td><td>${n(o.trigger_price,2)}</td><td>${esc(o.status)}</td><td class="muted">${esc(o.created||'')}</td></tr>`),
  {label:'Your pending orders at Dhan',empty:'No pending order at Dhan right now.'});
 const rules=a.order_rules||[];h+=`<div class="muted" style="margin-top:8px">ATIP paper orders resting: ${(a.paper_orders||[]).length} · ATIP target / stop rules waiting: ${rules.length}</div>`;
 if(rules.length)h+=table(['Symbol','Side','Role','Trigger price','Status'],rules.slice(0,30).map(r=>`<tr><td>${esc(r.symbol)}</td><td>${esc(r.side)}</td><td>${esc(r.role||'')}</td><td>${n(r.resolved_trigger_price,2)}</td><td>${esc(r.status)}</td></tr>`),'ATIP target and stop rules waiting');
 el.innerHTML=h}catch(e){el.innerHTML=errbox(e,'your pending orders','mine()')}}
let GH=null;
async function gateCard(){const el=document.getElementById('gsum');try{const [g,h]=await Promise.all([j('/api/market-regime'),j('/api/market-regime/history?days=250')]);
 if(!g.gate){el.innerHTML=`<span class="muted">${esc(g.reason||'Not computed yet: it runs nightly with the technical signals.')}</span>`;return}
 document.getElementById('gpill').innerHTML=gpill(g.gate);
 document.getElementById('gsince').textContent=`· ${stl(g.status)} since ${g.status_since||'—'}`;
 el.innerHTML=kv([['Why',esc(g.reason||'')],[`Distribution days (last ${g.rules.dd_window} sessions)`,`${g.dd_count}${g.dd_dates.length?' <span class="muted">('+g.dd_dates.map(esc).join(', ')+')</span>':''}`],['Nifty vs 200-DMA',g.sma200==null?'—':`${n(g.nifty_close,0)} vs ${n(g.sma200,0)} <span class="muted">(${pct((g.nifty_close/g.sma200-1)*100)})</span>`],['Last follow-through day',esc(g.ftd_date||'—')],['Rules',`<span class="muted">distribution day: Nifty down ${g.rules.dd_drop_pct}% or more on higher market volume; under pressure at ${g.rules.pressure_dd}, correction at ${g.rules.correction_dd} or ${g.rules.correction_drawdown_pct}% off the high; follow-through: day ${g.rules.ftd_min_day}+ of a rally attempt, up ${g.rules.ftd_pct}% on higher volume</span>`]],'Market gate summary');
 document.getElementById('gchanges').innerHTML=table(['Changed','Status','Gate','Why'],g.changes.map(c=>`<tr><td style="white-space:nowrap">${esc(c.date)}</td><td>${esc(stl(c.status))}</td><td>${gpill(c.gate)}</td><td class="muted">${esc(c.reason||'')}</td></tr>`),{label:'Market gate changes',empty:'The gate has not changed in the stored history.'});
 GH=h;drawGate();
 document.getElementById('gtable').innerHTML=table(['Date','Nifty','Change','Volume vs prev.','Distribution day','Count','Status','Gate'],h.slice(-30).reverse().map(r=>`<tr><td>${esc(r.date)}</td><td>${n(r.nifty_close,2)}</td><td>${pct(r.change_pct)}</td><td>${r.volume_ratio==null?'—':n(r.volume_ratio,2)+'×'}</td><td>${r.distribution_day?'yes':''}</td><td>${r.dd_count}</td><td>${esc(stl(r.status))}</td><td>${r.gate?gpill(r.gate):''}</td></tr>`),{label:'Market gate, last 30 sessions',empty:'No gate history stored yet.'})
}catch(e){el.innerHTML=errbox(e,'the market gate','gateCard()')}}
function drawGate(){const h=GH,el=document.getElementById('gchart');if(!h||h.length<2){el.innerHTML='';return}
 const W=Math.max(240,Math.floor(el.clientWidth||600)),H=250,ST=8,ml=48,mr=W<420?8:70,mt=16,mb=20,ph=H-mt-mb-ST-8,pw=W-ml-mr,N=h.length;
 const vals=h.flatMap(r=>[r.nifty_close,r.sma200]).filter(v=>v!=null);let lo=Math.min(...vals),hi=Math.max(...vals);const pad=(hi-lo)*.06||1;lo-=pad;hi+=pad;
 const X=i=>ml+i*pw/(N-1),Y=v=>mt+(hi-v)/(hi-lo)*ph;
 const raw=(hi-lo)/4,mag=Math.pow(10,Math.floor(Math.log10(raw))),stp=[1,2,2.5,5,10].map(m=>m*mag).find(m=>m>=raw);
 let svg=`<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="Nifty 50 with its 200-day average, distribution days and the market gate over the last ${N} sessions">`;
 for(let t=Math.ceil(lo/stp)*stp;t<=hi;t+=stp)svg+=`<line class="sv-grid" x1="${ml}" x2="${ml+pw}" y1="${Y(t)}" y2="${Y(t)}"/><text class="sv-t" x="${ml-6}" y="${Y(t)+3.5}" text-anchor="end">${n(t,0)}</text>`;
 const path=k=>{let d='',pen=false;h.forEach((r,i)=>{const v=r[k];if(v==null){pen=false;return}d+=(pen?'L':'M')+X(i).toFixed(1)+' '+Y(v).toFixed(1);pen=true});return d};
 svg+=`<path class="sv-s2" d="${path('sma200')}"/><path class="sv-s1" d="${path('nifty_close')}"/>`;
 h.forEach((r,i)=>{const x=X(i),y=Y(r.nifty_close);
  if(r.distribution_day)svg+=`<polygon class="sv-mark" points="${x-4.5},${y-15} ${x+4.5},${y-15} ${x},${y-6}"/>`;
  if(r.follow_through)svg+=`<polygon class="sv-mark" points="${x-4.5},${y+15} ${x+4.5},${y+15} ${x},${y+6}"/>`});
 const sy=mt+ph+8,half=pw/(N-1)/2;let a=0;
 for(let i=1;i<=N;i++){if(i===N||h[i].gate!==h[a].gate){const g=h[a].gate;if(g){const x0=Math.max(ml,X(a)-half)+(a?1:0),x1=Math.min(ml+pw,X(i-1)+half)-(i<N?1:0);
   svg+=`<rect class="g-${esc(g)}" x="${x0}" y="${sy}" width="${Math.max(1,x1-x0)}" height="${ST}" rx="2"/>`}a=i}}
 if(mr>20){const L=h[N-1];let yn=Y(L.nifty_close),ys=L.sma200==null?null:Y(L.sma200);if(ys!=null&&Math.abs(yn-ys)<13){const m=(yn+ys)/2;if(yn<ys){yn=m-7;ys=m+7}else{yn=m+7;ys=m-7}}
 svg+=`<text class="sv-ink" x="${ml+pw+6}" y="${yn+4}">Nifty 50</text>`+(ys==null?'':`<text class="sv-ink" x="${ml+pw+6}" y="${ys+4}">200-DMA</text>`)}
 (W<520?[0,.5,1]:[0,.25,.5,.75,1]).forEach(f=>{const i=Math.round(f*(N-1));svg+=`<text class="sv-t" x="${X(i)}" y="${H-4}" text-anchor="${f===0?'start':f===1?'end':'middle'}">${esc(h[i].date)}</text>`});
 svg+=`<line id="gxh" class="sv-xh" y1="${mt}" y2="${sy+ST}" opacity="0"/><circle id="gd1" class="sv-d1" r="4" opacity="0"/><circle id="gd2" class="sv-d2" r="4" opacity="0"/>`;
 svg+=`<rect id="ghit" x="${ml}" y="${mt}" width="${pw}" height="${sy+ST-mt}" fill="transparent"/></svg>`;
 el.innerHTML=svg;const hit=document.getElementById('ghit'),tip=document.getElementById('tip');
 hit.onmousemove=ev=>{const b=hit.getBoundingClientRect(),i=Math.max(0,Math.min(N-1,Math.round((ev.clientX-b.left)/b.width*(N-1)))),r=h[i],x=X(i);
  const xh=document.getElementById('gxh');xh.setAttribute('x1',x);xh.setAttribute('x2',x);xh.setAttribute('opacity',.6);
  const d1=document.getElementById('gd1');d1.setAttribute('cx',x);d1.setAttribute('cy',Y(r.nifty_close));d1.setAttribute('opacity',1);
  const d2=document.getElementById('gd2');if(r.sma200!=null){d2.setAttribute('cx',x);d2.setAttribute('cy',Y(r.sma200));d2.setAttribute('opacity',1)}else d2.setAttribute('opacity',0);
  tip.style.display='block';tip.style.left=Math.min(ev.clientX+14,window.innerWidth-230)+'px';tip.style.top=(ev.clientY+10)+'px';
  tip.innerHTML=`<b>${esc(r.date)}</b><br><span class="c-s1">━</span> Nifty ${n(r.nifty_close,2)} <span class="muted">(${pct(r.change_pct)})</span><br><span class="c-s2">┅</span> 200-DMA ${n(r.sma200,0)}<br>Volume vs previous ${r.volume_ratio==null?'—':n(r.volume_ratio,2)+'×'}${r.distribution_day?' · <b>distribution day</b>':''}${r.follow_through?' · <b>follow-through day</b>':''}<br>${r.dd_count} distribution days · ${esc(stl(r.status))} · gate ${esc(r.gate||'—')}`};
 hit.onmouseleave=()=>{tip.style.display='none';['gxh','gd1','gd2'].forEach(id=>document.getElementById(id).setAttribute('opacity',0))}}
window.addEventListener('resize',()=>{clearTimeout(window._gr);window._gr=setTimeout(drawGate,150)});
async function act(u,word){const o=document.getElementById('ao');o.textContent=' working…';o.removeAttribute('title');
 try{const r=await post(u);const st=r&&(r.status||r.result);o.textContent=' '+word+(st?` (${stl(st)})`:'')+'.';o.title=JSON.stringify(r).slice(0,400);load()}catch(e){o.textContent=' '+e.message}}
load();gateCard();
</script></body></html>"""


# score columns the screener can show -> the glossary's key (dashboard/score_glossary.py)
_SCORE_FIELDS = {"atip_score": "ATIP", "vpi": "VPI", "vpi_score": "VPI", "mri": "MRI", "mri_score": "MRI", "rri": "RRI",
                 "rri_score": "RRI", "zpi": "ZPI", "zpi_score": "ZPI", "cri": "CRI", "cri_score": "CRI", "acs": "ACS",
                 "acs_score": "ACS", "beta": "BETA", "beta_1y": "BETA"}


def _page(tpl: str, token: str, title: str, sub: str) -> str:
    return (tpl.replace("__STYLE__", _STYLE + _EXTRA).replace("__TOKEN__", json.dumps(token))
            .replace("__TITLE__", title).replace("__SUB__", sub).replace("__SK__", _SK))


def render_pulse(token: str) -> str:
    return _page(PULSE, token, "ATIP Market pulse", "Market pulse")


def render_signals(token: str) -> str:
    return _page(SIGNALS, token, "ATIP Signals", "Technical signals")


def render_screener(token: str) -> str:
    from dashboard.score_glossary import describe, legend_html
    gloss = {k: describe(v) for k, v in _SCORE_FIELDS.items()}
    return (_page(SCREENER, token, "ATIP Screener", "Stock screener")
            .replace("__LEGEND__", legend_html("screenerLegend"))
            .replace("__GLOSS__", html.escape(json.dumps(gloss, ensure_ascii=False), quote=True)))


def render_research(token: str) -> str:
    return _page(RESEARCH, token, "ATIP Research", "Equity research")


def render_options(token: str) -> str:
    return _page(OPTIONS, token, "ATIP Options Builder", "Options strategy builder")
