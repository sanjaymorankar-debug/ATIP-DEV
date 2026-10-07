"""
W39 pages: /research (equity research reports, ratings, hit rate, 7-year history coverage),
/screener (fundamental screener with presets and saved screens) and /options-builder
(multi-leg strategy builder with a payoff chart). Both read the W39 API
(dashboard/w39_routes.py); the options page only analyses -- it has no order button.

Payoff chart colours: categorical slots 1 and 2 of the dataviz reference palette, dark steps
(#3987e5 expiry, #d95926 target date), validated on the panel surface #1e293b (contrast and
CVD separation pass); the target-date line is also dashed, so identity never rests on colour.
"""

import json

from dashboard.w38_page import _STYLE

_EXTRA = """
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}
.stat{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px 10px}
.stat .k{color:var(--muted);font-size:11px}.stat .v{font-size:17px;font-weight:600;margin-top:2px}
.BUY{background:#064e3b;color:#a7f3d0}.ADD{background:#134e4a;color:#99f6e4}.REDUCE{background:#78350f;color:#fde68a}
.SELL{background:#7f1d1d;color:#fecaca}.NOT_RATED{background:#334155;color:#cbd5e1}
ul.b{margin:4px 0 0 18px}ul.b li{margin:3px 0}.two{display:grid;grid-template-columns:1fr 1fr;gap:10px}
@media(max-width:800px){.two{grid-template-columns:1fr}}
#chart{overflow:hidden}.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:var(--muted);margin:4px 0}
.legend i{display:inline-block;width:18px;height:0;border-top:2px solid;vertical-align:middle;margin-right:5px}
#tip{position:fixed;pointer-events:none;background:#0b1220;border:1px solid var(--line);border-radius:6px;padding:6px 8px;
 font-size:12px;display:none;z-index:9}
"""

_HEAD = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>__TITLE__</title><style>__STYLE__</style></head><body>
<div class="top"><div><b style="color:var(--accent)">📊 ATIP</b> <span class="muted">__SUB__</span></div>
<div><a href="/screener">Screener</a><a href="/research">Research</a><a href="/options-builder">Options builder</a><a href="/">← Dashboard</a></div></div>"""

_JS_COMMON = r"""
const TOKEN=__TOKEN__;
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function j(u,o){const r=await fetch(u,o);const t=await r.json();if(!r.ok)throw new Error(t.error||t.detail||r.status);return t}
const post=(u,b)=>j(u,{method:'POST',headers:{'Content-Type':'application/json','X-ATIP-Token':TOKEN},body:JSON.stringify(b||{})});
const table=(h,rows)=>`<table><thead><tr>${h.map(x=>`<th>${x}</th>`).join('')}</tr></thead><tbody>${rows.join('')||`<tr><td colspan=${h.length} class="muted">none</td></tr>`}</tbody></table>`;
const pill=s=>`<span class="pill ${esc(s)}">${esc(String(s||'').replace('_',' '))}</span>`;
const n=(v,d=2)=>v==null||isNaN(v)?'—':Number(v).toLocaleString('en-IN',{maximumFractionDigits:d,minimumFractionDigits:0});
const pct=v=>v==null?'—':(v>0?'+':'')+n(v,1)+'%';
const rs=v=>v==null||isNaN(v)?'—':(v<0?'−':'')+'₹'+Math.abs(Number(v)).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});
"""

RESEARCH = _HEAD + r"""
<div class="wrap"><div class="tabs" id="tabs"></div>
<div class="pane" id="p-rep"><div class="card"><h3>Equity research report</h3>
<div class="muted">Institutional-style report built from ATIP's stored data: DCF with bull / base / bear cases, peer and own-history multiples, a 12-month target and a rating. A model output for your own research, not advice.</div>
<div style="margin-top:8px"><input id="sym" placeholder="Symbol e.g. RELIANCE" style="width:200px" onkeydown="if(event.key==='Enter')load()">
<button onclick="load()">Open</button><button onclick="load(true)">Rebuild &amp; save</button></div></div>
<div id="rep"></div></div>
<div class="pane" id="p-rat"><div class="card"><h3>Latest ratings <select id="rf" onchange="rat()"><option value="">All</option><option>BUY</option><option>ADD</option><option>REDUCE</option><option>SELL</option><option>NOT_RATED</option></select>
<button onclick="runAll()">Rebuild all</button></h3><div id="rat" class="scroll"></div></div>
<div class="card"><h3>Hit rate of past calls</h3><div class="muted">A call is a new rating or a target move of more than 5%. HIT = the target was reached within 12 months.</div><div id="hit" style="margin-top:6px"></div></div></div>
<div class="pane" id="p-his"><div class="card"><h3>7-year price history</h3>
<div class="muted">The nightly backfill (22:20) walks each tracked stock back 7 years from Dhan, 40 stocks a night. Daily prices are now kept for 7 years.</div>
<div id="cov" style="margin-top:8px"></div><button onclick="backfill()">Run a pass now (10 stocks)</button><span id="bfo" class="muted"></span></div></div>
</div><div id="tip"></div>
<script>""" + _JS_COMMON + r"""
const TABS=[['rep','Report'],['rat','Ratings & hit rate'],['his','History coverage']];const loaded={};
function show(id){document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.dataset.id===id));document.querySelectorAll('.pane').forEach(p=>p.classList.toggle('on',p.id==='p-'+id));
 if(!loaded[id]){loaded[id]=1;({rep:()=>{},rat,his})[id]()}}
document.getElementById('tabs').innerHTML=TABS.map(([id,l])=>`<div class="tab" data-id="${id}" onclick="show('${id}')">${l}</div>`).join('');
const li=xs=>xs&&xs.length?`<ul class="b">${xs.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:'<span class="muted">none found</span>';
async function load(rebuild){const s=document.getElementById('sym').value.trim().toUpperCase();if(!s)return;const el=document.getElementById('rep');el.innerHTML='<div class="card muted">Loading…</div>';
 try{const r=rebuild?await post(`/api/research/equity/${encodeURIComponent(s)}/refresh`):await j(`/api/research/equity/${encodeURIComponent(s)}`);el.innerHTML=render(r)}catch(e){el.innerHTML=`<div class="card bad">${esc(e.message)}</div>`}}
function render(r){const v=r.valuation,ps=r.price_stats||{},q=r.quality||{},f=r.key_financials||{};
 const meth=Object.entries(v.methods||{}).map(([k,m])=>`<tr><td>${esc(k.replace('_',' '))}</td><td>${rs(m.value)}</td><td>${n(m.weight*100,0)}%</td><td class="muted">${esc(m.multiple?`${m.multiple.toUpperCase()} ${m.peer_median??m.own_median}x (${m.peers??m.quarters} ${m.peers?'peers':'quarters'})`:`ke ${m.ke_pct}%`)}</td></tr>`);
 const sc=v.scenarios?['bear','base','bull'].map(k=>`<tr><td>${k}</td><td>${rs(v.scenarios[k].value)}</td><td>${v.scenarios[k].g1_pct}%</td><td>${v.scenarios[k].ke_pct}%</td><td>${v.scenarios[k].terminal_pct}%</td><td>${n(v.scenarios[k].weight*100,0)}%</td></tr>`):[];
 const sens=v.sensitivity?`<table><thead><tr><th>ke \\ g∞</th>${v.sensitivity.terminal_pct.map(x=>`<th>${x}%</th>`).join('')}</tr></thead><tbody>${v.sensitivity.values.map((row,i)=>`<tr><th>${v.sensitivity.ke_pct[i]}%</th>${row.map(x=>`<td>${rs(x)}</td>`).join('')}</tr>`).join('')}</tbody></table>`:'<span class="muted">DCF not applicable</span>';
 const peers=(r.peers||[]).map(p=>`<tr><td>${esc(p.symbol)}</td><td>${rs(p.price)}</td><td>${n(p.pe,1)}</td><td>${n(p.pb,1)}</td><td>${n(p.roe_pct,1)}</td><td>${pct(p.revenue_growth_pct)}</td><td>${n(p.mcap_cr,0)}</td></tr>`);
 const shp=(r.shareholding||[]).map(x=>`<tr><td>${esc(String(x.as_of).slice(0,10))}</td><td>${n(x.promoter_pct,2)}</td><td>${n(x.fpi_pct,2)}</td><td>${n(x.mf_pct,2)}</td><td>${n(x.retail_pct,2)}</td><td>${n(x.pledged_pct,2)}</td></tr>`);
 return `<div class="card"><h3>${esc(r.symbol)} <span class="muted">${esc(r.industry||'')}</span> · ${pill(r.rating)} <span class="muted">as of ${esc(r.as_of)}</span></h3>
 <div class="grid"><div class="stat"><div class="k">Price</div><div class="v">${rs(r.price)}</div></div><div class="stat"><div class="k">12-month target</div><div class="v">${rs(r.target_price)}</div></div>
 <div class="stat"><div class="k">Upside</div><div class="v">${pct(r.upside_pct)}</div></div><div class="stat"><div class="k">Fair value</div><div class="v">${rs(r.fair_value)}</div></div>
 <div class="stat"><div class="k">Uncertainty</div><div class="v">${esc((r.uncertainty||'—').replace('_',' '))}</div></div><div class="stat"><div class="k">Moat proxy / quality</div><div class="v">${esc(q.moat_proxy||'—')} / ${n(q.quality_score,0)}</div></div></div></div>
 <div class="two"><div class="card"><h3>Investment thesis</h3>${li(r.thesis)}</div><div class="card"><h3>Risks</h3>${li(r.risks)}</div></div>
 <div class="card"><h3>Catalysts</h3>${li(r.catalysts)}</div>
 <div class="two"><div class="card"><h3>Valuation methods (cost of equity ${v.cost_of_equity_pct}%)</h3>${table(['Method','Value','Weight','Basis'],meth)}</div>
 <div class="card"><h3>DCF scenarios</h3>${table(['Case','Value','Growth','ke','Terminal','Weight'],sc)}</div></div>
 <div class="card"><h3>Sensitivity: base-case value by cost of equity × terminal growth</h3>${sens}</div>
 <div class="two"><div class="card"><h3>Key financials (${esc(f.quarter||'')})</h3>${table(['Metric','Value'],[['EPS (TTM)',n(f.eps_ttm)],['Book value / share',n(f.book_value_ps)],['Revenue (₹ cr, quarter)',n(f.revenue_cr,0)],['Revenue growth YoY',pct(f.revenue_growth_yoy)],['EPS growth YoY',pct(f.eps_growth_yoy)],['ROE',f.roe==null?'—':n(f.roe*100,1)+'%'],['ROCE',f.roce==null?'—':n(f.roce*100,1)+'%'],['Debt / equity',n(f.debt_equity)],['Interest cover',n(f.interest_coverage,1)]].map(([a,b])=>`<tr><td>${a}</td><td>${b}</td></tr>`))}</div>
 <div class="card"><h3>Price history (${n(ps.history_years,1)} years stored)</h3>${table(['Statistic','Value'],[['52-week range',`${rs(ps.low_52w)} – ${rs(ps.high_52w)}`],['1-year return',pct(ps.return_1y_pct)],['3-year CAGR',pct(ps.cagr_3y_pct)],['5-year CAGR',pct(ps.cagr_5y_pct)],['7-year CAGR',pct(ps.cagr_7y_pct)],['Volatility (1y)',ps.volatility_1y_pct==null?'—':ps.volatility_1y_pct+'%'],['Max drawdown (1y)',pct(ps.max_drawdown_1y_pct)]].map(([a,b])=>`<tr><td>${a}</td><td>${b}</td></tr>`))}</div></div>
 <div class="card"><h3>Peers (${esc(r.industry||'industry unknown')})</h3>${table(['Symbol','Price','P/E','P/B','ROE %','Revenue growth','M-cap ₹ cr'],peers)}</div>
 <div class="card"><h3>Shareholding (%)</h3>${table(['Quarter','Promoter','FPI','MF','Retail','Pledged'],shp)}</div>
 <div class="card"><h3>Disclosures</h3>${li(r.disclosures)}</div>`}
async function rat(){const r=document.getElementById('rf').value;const d=await j('/api/research/equity'+(r?`?rating=${r}`:''));
 document.getElementById('rat').innerHTML=table(['Symbol','As of','Price','Target','Upside','Rating','Uncertainty','Moat','ATIP signal'],d.map(x=>`<tr><td><a href="#" style="color:var(--accent)" onclick="document.getElementById('sym').value='${esc(x.symbol)}';show('rep');load();return false">${esc(x.symbol)}</a></td><td>${esc(String(x.as_of).slice(0,10))}</td><td>${rs(x.price)}</td><td>${rs(x.target_price)}</td><td>${pct(x.upside_pct)}</td><td>${pill(x.rating)}</td><td>${esc(x.uncertainty||'')}</td><td>${esc(x.moat_proxy||'')}</td><td>${esc(x.atip_signal||'')}</td></tr>`));
 const h=await j('/api/research/hit-rate');document.getElementById('hit').innerHTML=table(['Rating','Calls','Open','Hit','Missed','Success rate','Avg return'],Object.entries(h).map(([k,o])=>`<tr><td>${pill(k)}</td><td>${o.calls}</td><td>${o.open}</td><td>${o.hit}</td><td>${o.missed}</td><td>${o.success_rate_pct==null?'—':o.success_rate_pct+'%'}</td><td>${pct(o.avg_return_pct)}</td></tr>`))}
async function runAll(){if(!confirm('Rebuild today\'s report for every tracked stock?'))return;const o=await post('/api/research/equity/run');alert(`${o.rows} reports, ${o.calls} calls`);rat()}
async function his(){const c=await j('/api/data/history/coverage');document.getElementById('cov').innerHTML=`<div class="grid"><div class="stat"><div class="k">Target</div><div class="v">${esc(c.target)}</div></div><div class="stat"><div class="k">Complete</div><div class="v">${c.complete} / ${c.symbols}</div></div><div class="stat"><div class="k">Listed later</div><div class="v">${c.listed_later}</div></div><div class="stat"><div class="k">Pending</div><div class="v">${c.pending}</div></div><div class="stat"><div class="k">Parked (errors)</div><div class="v">${c.parked}</div></div><div class="stat"><div class="k">Oldest bar</div><div class="v">${esc(c.oldest_bar||'—')}</div></div></div>`}
async function backfill(){document.getElementById('bfo').textContent=' running…';try{const o=await post('/api/data/history/backfill',{max:10});document.getElementById('bfo').textContent=` ${o.status}: ${o.symbols??0} stocks, ${o.rows??0} bars, ${o.remaining??0} to go`;his()}catch(e){document.getElementById('bfo').textContent=' '+e.message}}
show('rep');
{const q=new URLSearchParams(location.search).get('symbol');if(q){document.getElementById('sym').value=q;load()}}
</script></body></html>"""

OPTIONS = _HEAD + r"""
<div class="wrap"><div class="card"><h3>Options strategy builder</h3>
<div class="muted">Analysis only: nothing is ordered, and margin is not computed (check your broker's calculator). Premiums come from ATIP's stored option chain / F&amp;O bhavcopy; legs with no stored quote are priced with Black-Scholes at 18% IV and say so.</div>
<div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:6px;align-items:center">
<input id="sym" value="NIFTY" style="width:120px" onchange="chain()"><span class="muted">spot</span><input id="spot" type="number" step="any" style="width:100px">
<span class="muted">lot</span><input id="lot" type="number" min="1" value="75" style="width:70px">
<span class="muted">expiry</span><input id="exp" list="exps" style="width:120px" placeholder="YYYY-MM-DD"><datalist id="exps"></datalist>
<select id="tpl"></select><span class="muted">wings (strikes)</span><input id="w" type="number" min="1" value="2" style="width:55px">
<button onclick="build()">Build</button><span id="ci" class="muted"></span></div></div>
<div class="card"><h3>Legs <button onclick="addLeg()">+ leg</button><button onclick="analyse()">Analyse</button>
<span class="muted">value on</span> <input id="td" type="date" onchange="analyse()"></h3><div id="legs" style="overflow-x:auto"></div></div>
<div id="out"></div></div><div id="tip"></div>
<script>""" + _JS_COMMON + r"""
let LEGS=[],LAST=null,RT=null;
window.addEventListener('resize',()=>{clearTimeout(RT);RT=setTimeout(()=>LAST&&chart(LAST),150)});
(async()=>{const t=await j('/api/options/templates');document.getElementById('tpl').innerHTML=t.map(x=>`<option value="${x.key}" title="${esc(x.description)}">${esc(x.name)} (${esc(x.view)})</option>`).join('');
 const d=new Date();d.setDate(d.getDate()+((2-d.getDay()+7)%7||7));document.getElementById('exp').value=d.toISOString().slice(0,10);chain()})();  // NSE weekly expiry: Tuesday
async function chain(){const s=document.getElementById('sym').value.trim().toUpperCase();try{const c=await j(`/api/options/chain/${encodeURIComponent(s)}`);
 if(c.spot)document.getElementById('spot').value=c.spot;if(c.lot_size)document.getElementById('lot').value=c.lot_size;
 document.getElementById('exps').innerHTML=c.expiries.map(e=>`<option value="${e}">`).join('');if(c.expiries.length)document.getElementById('exp').value=c.expiries[0];
 document.getElementById('ci').textContent=c.quotes?` ${c.quotes} stored quotes, ${c.expiries.length} expiries`:' no stored chain: premiums will be modelled'}catch(e){document.getElementById('ci').textContent=' '+e.message}}
const val=id=>document.getElementById(id).value;
function body(){return{symbol:val('sym').trim().toUpperCase()||null,spot:Number(val('spot'))||null,lot_size:Number(val('lot'))||1,target_date:val('td')||null}}
async function build(){try{const r=await post('/api/options/build',{...body(),template:val('tpl'),expiry:val('exp'),width_steps:Number(val('w'))||2});LEGS=r.legs;drawLegs();show(r)}catch(e){fail(e)}}
async function analyse(){if(!LEGS.length)return;try{show(await post('/api/options/analyse',{...body(),legs:LEGS}))}catch(e){fail(e)}}
function fail(e){document.getElementById('out').innerHTML=`<div class="card bad">${esc(e.message)}</div>`}
function addLeg(){LEGS.push({kind:'CE',side:'BUY',strike:Number(val('spot'))||null,expiry:val('exp'),lots:1,premium:null});drawLegs()}
function upd(i,k,v){LEGS[i][k]=(k==='kind'||k==='side'||k==='expiry')?v:(v===''?null:Number(v));if(k==='strike'||k==='expiry'||k==='kind')LEGS[i].premium=null}
function drawLegs(){const sel=(i,k,opts)=>`<select onchange="upd(${i},'${k}',this.value)">${opts.map(o=>`<option ${LEGS[i][k]===o?'selected':''}>${o}</option>`).join('')}</select>`;
 const inp=(i,k,w)=>`<input type="number" step="any" style="width:${w}px" value="${LEGS[i][k]??''}" onchange="upd(${i},'${k}',this.value)">`;
 document.getElementById('legs').innerHTML=table(['Side','Type','Strike','Expiry','Lots','Premium','IV','Source',''],LEGS.map((l,i)=>`<tr><td>${sel(i,'side',['BUY','SELL'])}</td><td>${sel(i,'kind',['CE','PE','FUT'])}</td><td>${inp(i,'strike',90)}</td><td><input style="width:105px" value="${esc(l.expiry)}" onchange="upd(${i},'expiry',this.value)"></td><td>${inp(i,'lots',50)}</td><td>${inp(i,'premium',80)}</td><td>${l.iv==null?'—':n(l.iv*100,1)+'%'}</td><td class="muted">${esc(l.price_source||l.iv_source||'')}</td><td><button onclick="LEGS.splice(${i},1);drawLegs()">✕</button></td></tr>`))}
function show(r){LEGS=r.legs.map(l=>({...l}));drawLegs();const g=r.greeks;
 const be=r.breakevens.length?r.breakevens.map(x=>n(x,2)).join(', '):'none';
 document.getElementById('out').innerHTML=`<div class="card"><div class="grid">
 <div class="stat"><div class="k">Net premium (${r.premium_type})</div><div class="v">${rs(Math.abs(r.net_premium))}</div></div>
 <div class="stat"><div class="k">Max profit</div><div class="v">${r.unlimited_profit?'Unlimited':rs(r.max_profit)}</div></div>
 <div class="stat"><div class="k">Max loss</div><div class="v">${r.unlimited_loss?'Unlimited':rs(r.max_loss)}</div></div>
 <div class="stat"><div class="k">Breakevens</div><div class="v" style="font-size:14px">${be}</div></div>
 <div class="stat"><div class="k">Probability of profit</div><div class="v">${r.probability_of_profit_pct}%</div></div>
 <div class="stat"><div class="k">Reward : risk</div><div class="v">${r.reward_to_risk==null?'—':r.reward_to_risk+' : 1'}</div></div></div>
 <div class="grid" style="margin-top:8px"><div class="stat"><div class="k">Δ delta (units)</div><div class="v">${n(g.delta,2)}</div></div><div class="stat"><div class="k">Γ gamma</div><div class="v">${n(g.gamma,4)}</div></div>
 <div class="stat"><div class="k">Θ theta (₹/day)</div><div class="v">${n(g.theta,0)}</div></div><div class="stat"><div class="k">ν vega (₹/vol pt)</div><div class="v">${n(g.vega,0)}</div></div></div></div>
 <div class="card"><h3>Payoff (₹, ${r.legs.length} legs × lot ${r.lot_size})</h3><div class="legend"><span><i style="border-color:#3987e5"></i>At expiry ${esc(r.first_expiry)}</span><span><i style="border-color:#d95926;border-top-style:dashed"></i>On ${esc(r.target_date)}</span><span class="muted">spot ${n(r.spot,2)}</span></div>
 <div id="chart"></div><details style="margin-top:6px"><summary class="muted">Table view</summary><div class="scroll">${table(['Spot','At expiry','On '+r.target_date],r.payoff.spot.map((x,i)=>`<tr><td>${n(x,2)}</td><td>${n(r.payoff.expiry[i],0)}</td><td>${n(r.payoff.target[i],0)}</td></tr>`))}</div></details>
 <div class="muted" style="margin-top:6px">${r.notes.map(esc).join(' ')}</div></div>`;chart(r)}
function chart(r){LAST=r;const W=Math.max(280,document.getElementById('chart').clientWidth),H=320,m={l:72,r:14,t:10,b:30};const xs=r.payoff.spot,e=r.payoff.expiry,t=r.payoff.target;
 const x0=xs[0],x1=xs[xs.length-1];let y0=Math.min(0,...e,...t),y1=Math.max(0,...e,...t);const pad=(y1-y0)*0.08||1;y0-=pad;y1+=pad;
 const X=v=>m.l+(v-x0)/(x1-x0)*(W-m.l-m.r),Y=v=>m.t+(y1-v)/(y1-y0)*(H-m.t-m.b);
 const path=a=>a.map((v,i)=>(i?'L':'M')+X(xs[i]).toFixed(1)+','+Y(v).toFixed(1)).join('');
 const ticks=k=>Array.from({length:k+1},(_,i)=>y0+(y1-y0)*i/k);
 let s=`<svg width="${W}" height="${H}" role="img" aria-label="Payoff chart" style="display:block">`;
 ticks(5).forEach(v=>{s+=`<line x1="${m.l}" x2="${W-m.r}" y1="${Y(v)}" y2="${Y(v)}" stroke="#334155" stroke-width="1" opacity=".5"/><text x="${m.l-6}" y="${Y(v)+4}" fill="#94a3b8" font-size="11" text-anchor="end">${n(v,0)}</text>`});
 (W<520?[0,.5,1]:[0,.25,.5,.75,1]).forEach(f=>{const v=x0+(x1-x0)*f;s+=`<text x="${X(v)}" y="${H-10}" fill="#94a3b8" font-size="11" text-anchor="${f===0?'start':f===1?'end':'middle'}">${n(v,0)}</text>`});
 s+=`<line x1="${m.l}" x2="${W-m.r}" y1="${Y(0)}" y2="${Y(0)}" stroke="#94a3b8" stroke-width="1"/>`;
 s+=`<line x1="${X(r.spot)}" x2="${X(r.spot)}" y1="${m.t}" y2="${H-m.b}" stroke="#94a3b8" stroke-dasharray="2 3" stroke-width="1"/>`;
 s+=`<path d="${path(t)}" fill="none" stroke="#d95926" stroke-width="2" stroke-dasharray="6 4"/><path d="${path(e)}" fill="none" stroke="#3987e5" stroke-width="2"/>`;
 r.breakevens.forEach((b,k)=>{const dy=k%2?18:-8;s+=`<circle cx="${X(b)}" cy="${Y(0)}" r="4" fill="#1e293b" stroke="#e2e8f0" stroke-width="2"/><text x="${X(b)}" y="${Y(0)+dy}" fill="#e2e8f0" font-size="11" text-anchor="middle">${n(b,0)}</text>`});
 s+=`<line id="xh" y1="${m.t}" y2="${H-m.b}" stroke="#e2e8f0" stroke-width="1" opacity="0"/><circle id="d1" r="4" fill="#3987e5" stroke="#1e293b" stroke-width="2" opacity="0"/><circle id="d2" r="4" fill="#d95926" stroke="#1e293b" stroke-width="2" opacity="0"/>`;
 s+=`<rect x="${m.l}" y="${m.t}" width="${W-m.l-m.r}" height="${H-m.t-m.b}" fill="transparent" id="hit"/></svg>`;
 const el=document.getElementById('chart');el.innerHTML=s;const tip=document.getElementById('tip'),hit=document.getElementById('hit');
 hit.onmousemove=ev=>{const b=hit.getBoundingClientRect(),px=ev.clientX-b.left;const sv=x0+px/b.width*(x1-x0);let i=0,best=1e18;xs.forEach((v,k)=>{const d=Math.abs(v-sv);if(d<best){best=d;i=k}});
  const xh=document.getElementById('xh');xh.setAttribute('x1',X(xs[i]));xh.setAttribute('x2',X(xs[i]));xh.setAttribute('opacity',.6);
  [['d1',e],['d2',t]].forEach(([id,a])=>{const c=document.getElementById(id);c.setAttribute('cx',X(xs[i]));c.setAttribute('cy',Y(a[i]));c.setAttribute('opacity',1)});
  tip.style.display='block';tip.style.left=(ev.clientX+14)+'px';tip.style.top=(ev.clientY+10)+'px';
  tip.innerHTML=`<b>Spot ${n(xs[i],2)}</b> <span class="muted">(${pct((xs[i]/r.spot-1)*100)})</span><br><span style="color:#3987e5">━</span> At expiry: ${rs(e[i])}<br><span style="color:#d95926">┅</span> On ${esc(r.target_date)}: ${rs(t[i])}`};
 hit.onmouseleave=()=>{tip.style.display='none';['xh','d1','d2'].forEach(id=>document.getElementById(id).setAttribute('opacity',0))}}
</script></body></html>"""


SCREENER = _HEAD + r"""
<div class="wrap"><div class="card"><h3>Fundamental screener</h3>
<div class="muted">Filter every stock ATIP has fundamentals for, Screener.in style: <code>roce_pct &gt; 20 AND debt_equity &lt; 0.5 AND (pe &lt; 25 OR peg &lt; 1)</code>, <code>industry IN ("Capital Goods")</code>, <code>research_rating = "BUY"</code>. Percentages are in %, money in ₹ crore where the field ends in _cr. A stock missing a field never matches a condition on it.</div>
<div id="presets" style="margin:8px 0;display:flex;flex-wrap:wrap;gap:4px"></div>
<textarea id="q" rows="3" style="width:100%;font-family:ui-monospace,monospace;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:8px" placeholder="roce_pct > 20 AND debt_equity < 0.5"></textarea>
<div style="margin-top:6px;display:flex;flex-wrap:wrap;gap:6px;align-items:center">
<select id="bf"></select><select id="bo"><option>&gt;</option><option>&gt;=</option><option>&lt;</option><option>&lt;=</option><option>=</option><option>!=</option></select>
<input id="bv" style="width:110px" placeholder="value"><button onclick="addCond('AND')">+ AND</button><button onclick="addCond('OR')">+ OR</button>
<span class="muted" style="margin-left:10px">sort</span><select id="sort"><option value="">first field</option></select>
<select id="dir"><option value="1">high → low</option><option value="0">low → high</option></select>
<button onclick="go()">Run screen</button><a id="csv" href="#" style="color:var(--accent);margin-left:6px">CSV</a></div>
<details style="margin-top:8px"><summary class="muted">All fields</summary><div id="help" class="scroll" style="margin-top:6px"></div></details></div>
<div class="card"><h3 id="rh">Results</h3><div id="res" style="overflow-x:auto"><span class="muted">Pick a preset or write a query.</span></div></div>
<div class="card"><h3>Saved screens</h3>
<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center"><input id="sn" placeholder="Name this screen" style="width:220px">
<label class="muted"><input type="checkbox" id="snt"> alert me on new matches (daily 20:50)</label><button onclick="save()">Save current query</button><span id="so" class="muted"></span></div>
<div id="saved" style="margin-top:8px"></div></div></div><div id="tip"></div>
<script>""" + _JS_COMMON + r"""
let CAT=null,LAST=null,SORT=null,DESC=1;
const fmt=(k,v)=>{if(v==null)return '<span class="muted">—</span>';const f=CAT.fields[k]||{};
 if(k==='symbol')return `<a href="/research?symbol=${encodeURIComponent(v)}" style="color:var(--accent)">${esc(v)}</a>`;
 if(k==='research_rating')return pill(v);if(f.kind==='text')return esc(v);
 if(f.unit==='%')return n(v,1)+'%';if(f.unit==='pts')return (v>0?'+':'')+n(v,2);if(f.unit==='₹ cr')return n(v,0);if(f.unit==='₹')return n(v,2);if(f.unit==='x')return n(v,2);return n(v,2)};
(async()=>{const c=await j('/api/screener/fields');CAT={...c,fields:{}};
 Object.values(c.groups).flat().forEach(f=>CAT.fields[f.key]=f);
 const opts=Object.entries(c.groups).map(([g,fs])=>`<optgroup label="${esc(g)}">${fs.map(f=>`<option value="${f.key}">${esc(f.label)}${f.unit?' ('+esc(f.unit)+')':''}</option>`).join('')}</optgroup>`).join('');
 document.getElementById('bf').innerHTML=opts;document.getElementById('bf').value='roce_pct';document.getElementById('sort').innerHTML='<option value="">first field</option>'+opts;
 document.getElementById('presets').innerHTML=c.presets.map((p,i)=>`<button title="${esc(p.description)}" onclick="preset(${i})" style="background:var(--panel);border:1px solid var(--line)">${esc(p.name)}</button>`).join('');
 document.getElementById('help').innerHTML=Object.entries(c.groups).map(([g,fs])=>`<h3 style="margin-top:6px">${esc(g)}</h3>`+table(['Field','Name','Unit','Notes'],fs.map(f=>`<tr><td><code>${f.key}</code></td><td>${esc(f.label)}</td><td>${esc(f.unit||f.kind)}</td><td class="muted">${esc([f.description,f.aliases.length?'also: '+f.aliases.join(', '):''].filter(Boolean).join(' · '))}</td></tr>`))).join('');
 saved();const q=new URLSearchParams(location.search).get('q');if(q){document.getElementById('q').value=q;go()}})();
function preset(i){const p=CAT.presets[i];document.getElementById('q').value=p.query;document.getElementById('sort').value=p.sort||'';document.getElementById('dir').value=p.desc===false?'0':'1';go()}
function addCond(join){const f=val('bf'),o=val('bo'),v=val('bv').trim();if(!v)return;const isText=CAT.fields[f].kind==='text';
 const lit=isText&&!/^".*"$/.test(v)?`"${v.replace(/"/g,'')}"`:v;const q=document.getElementById('q');q.value=(q.value.trim()?q.value.trim()+` ${join} `:'')+`${f} ${o} ${lit}`;document.getElementById('bv').value=''}
const val=id=>document.getElementById(id).value;
function params(extra){const p=new URLSearchParams({query:val('q').trim(),desc:val('dir'),limit:'500',...(extra||{})});if(val('sort'))p.set('sort',val('sort'));return p}
async function go(){const q=val('q').trim();if(!q)return;const el=document.getElementById('res');el.innerHTML='<span class="muted">Running…</span>';
 document.getElementById('csv').href='/api/screener/run.csv?'+params({limit:'2000'});
 try{draw(await j('/api/screener/run?'+params()))}catch(e){el.innerHTML=`<span class="bad">${esc(e.message)}</span>`;document.getElementById('rh').textContent='Results'}}
function draw(r,note){LAST=r;document.getElementById('rh').innerHTML=`Results: <b>${r.count}</b> of ${r.universe} stocks${r.count>r.rows.length?` (showing ${r.rows.length})`:''} <span class="muted">sorted by ${esc((CAT.fields[r.sort]||{}).label||r.sort)} ${r.desc?'↓':'↑'}</span>${note||''}`;
 const head=r.columns.map(c=>`<th style="cursor:pointer" onclick="resort('${c}')" title="${esc((CAT.fields[c]||{}).description||'')}">${esc((CAT.fields[c]||{}).label||c)}${r.sort===c?(r.desc?' ↓':' ↑'):''}</th>`).join('');
 document.getElementById('res').innerHTML=`<table><thead><tr>${head}</tr></thead><tbody>${r.rows.map(x=>`<tr>${r.columns.map(c=>`<td>${fmt(c,x[c])}</td>`).join('')}</tr>`).join('')||`<tr><td colspan="${r.columns.length}" class="muted">no stock matches</td></tr>`}</tbody></table>`}
function resort(c){const s=document.getElementById('sort');document.getElementById('dir').value=(s.value===c&&val('dir')==='1')?'0':'1';s.value=c;go()}
async function saved(){const d=await j('/api/screener/saved');document.getElementById('saved').innerHTML=table(['Name','Query','Last run','Matches','Alerts',''],d.map(x=>`<tr><td>${esc(x.name)}</td><td><code>${esc(x.query)}</code></td><td>${esc(String(x.last_run_at||'—').slice(0,16))}</td><td>${x.last_count??'—'}</td><td>${x.notify?'on':'off'}</td><td><button onclick="runSaved('${x.screen_id}')">Run</button><button onclick="loadSaved('${x.screen_id}')">Edit</button><button onclick="del('${x.screen_id}')">✕</button></td></tr>`));window.SAVED=d}
function loadSaved(id){const x=window.SAVED.find(s=>s.screen_id===id);document.getElementById('q').value=x.query;document.getElementById('sort').value=x.sort||'';document.getElementById('dir').value=x.descending?'1':'0';document.getElementById('sn').value=x.name;document.getElementById('snt').checked=!!x.notify;window.EDIT=id}
async function runSaved(id){try{const r=await j(`/api/screener/saved/${id}/run`);loadSaved(id);draw(r,r.new.length||r.dropped.length?` · <span class="ok">new: ${esc(r.new.join(', ')||'none')}</span> · <span class="note">dropped: ${esc(r.dropped.join(', ')||'none')}</span>`:'');saved()}catch(e){alert(e.message)}}
async function save(){try{const x=await post('/api/screener/saved',{name:val('sn').trim(),query:val('q').trim(),sort:val('sort')||null,desc:val('dir')==='1',notify:document.getElementById('snt').checked,screen_id:window.EDIT||null});window.EDIT=x.screen_id;document.getElementById('so').textContent=' saved';saved()}catch(e){document.getElementById('so').textContent=' '+e.message}}
async function del(id){if(!confirm('Delete this saved screen?'))return;await post(`/api/screener/saved/${id}/delete`);if(window.EDIT===id)window.EDIT=null;saved()}
</script></body></html>"""


def render_screener(token: str) -> str:
    return (SCREENER.replace("__STYLE__", _STYLE + _EXTRA).replace("__TOKEN__", json.dumps(token))
            .replace("__TITLE__", "ATIP Screener").replace("__SUB__", "Fundamental screener (W39)"))


def render_research(token: str) -> str:
    return (RESEARCH.replace("__STYLE__", _STYLE + _EXTRA).replace("__TOKEN__", json.dumps(token))
            .replace("__TITLE__", "ATIP Research").replace("__SUB__", "Equity research (W39)"))


def render_options(token: str) -> str:
    return (OPTIONS.replace("__STYLE__", _STYLE + _EXTRA).replace("__TOKEN__", json.dumps(token))
            .replace("__TITLE__", "ATIP Options Builder").replace("__SUB__", "Options strategy builder (W39)"))
