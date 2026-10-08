"""
W39 pages: /research (equity research reports with the fundamental scorecard, the DVM view and the
earnings surprise card, ratings, hit rate and the scorecard's record, 7-year history coverage),
/screener (fundamental + technical stock screener with presets and saved screens), /signals
(end-of-day technical signals and their track record), /market-pulse (global cues, GIFT Nifty, the
event calendar, FII flows and positioning, order-book pressure, your pending orders) and /options-builder (multi-leg
strategy builder with a payoff chart). All read the W39 API (dashboard/w39_routes.py); none of them
places an order.

Payoff chart colours: categorical slots 1 and 2 of the dataviz reference palette, dark steps
(#3987e5 expiry, #d95926 target date), validated on the panel surface #1e293b (contrast and
CVD separation pass); the target-date line is also dashed, so identity never rests on colour.
DVM meters (/research): one series, slot 1 #3987e5 on a track of the same ramp's darker step (#184f95), the
low / high thresholds marked in muted ink, and the score always printed beside the bar.
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
.two>*{min-width:0}.pill{white-space:nowrap}.sx{overflow-x:auto}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;vertical-align:middle;margin-right:5px}
#chart{overflow:hidden}.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:var(--muted);margin:4px 0}
.legend i{display:inline-block;width:18px;height:0;border-top:2px solid;vertical-align:middle;margin-right:5px}
.axes{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:8px}
@media(max-width:1000px){.axes{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:640px){.axes{grid-template-columns:1fr}}
.ax{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px 10px}.ax b{font-size:12.5px}
.ax.fl{display:flex;align-items:center;justify-content:center}
.ck{margin-top:5px;font-size:12px;line-height:1.35}.ck .d{color:var(--muted);font-size:11.5px;margin-left:16px}
.ck i{font-style:normal;display:inline-block;width:14px;font-weight:700}
.dvm .v{font-size:20px;font-weight:600}.meter{position:relative;height:8px;background:#184f95;border-radius:4px;margin:6px 0 4px}
.meter>span{display:block;height:100%;background:#3987e5;border-radius:4px}
.meter>i{position:absolute;top:-3px;width:2px;height:14px;background:var(--muted)}.ck .s{display:inline-block;min-width:28px;font-weight:600}
#tip{position:fixed;pointer-events:none;background:#0b1220;border:1px solid var(--line);border-radius:6px;padding:6px 8px;
 font-size:12px;display:none;z-index:9}
"""

_HEAD = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>__TITLE__</title><style>__STYLE__</style></head><body>
<div class="top"><div><b style="color:var(--accent)">📊 ATIP</b> <span class="muted">__SUB__</span></div>
<div><a href="/market-pulse">Market pulse</a><a href="/screener">Screener</a><a href="/signals">Signals</a><a href="/research">Research</a><a href="/options-builder">Options builder</a><a href="/">← Dashboard</a></div></div>"""

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
const gpill=g=>g?`<span class="pill ${g==='OPEN'?'BUY':g==='CAUTION'?'REDUCE':'SELL'}">${g==='OPEN'?'● open':g==='CAUTION'?'◐ caution':'○ closed'}</span>`:'<span class="pill NOT_RATED">unknown</span>';
const apill=a=>a?`<span class="pill ${a==='WITH'?'BUY':a==='MIXED'?'REDUCE':'SELL'}">${a==='WITH'?'with market':a==='MIXED'?'mixed':'against market'}</span>`:'<span class="muted">—</span>';
const stl=s=>String(s||'').replaceAll('_',' ').toLowerCase();
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
<div class="card"><h3>Hit rate of past calls</h3><div class="muted">A call is a new rating or a target move of more than 5%. HIT = the target was reached within 12 months.</div><div id="hit" style="margin-top:6px"></div></div>
<div class="card"><h3>Does the scorecard pay? <select id="sch" onchange="screc()"><option value="20">20 sessions</option><option value="60" selected>60 sessions</option><option value="120">120 sessions</option><option value="250">250 sessions</option></select></h3>
<div class="muted">Return minus the Nifty's after each stored scorecard, by checks passed. One sample per stock per month. A band needs 30 samples before it means anything; the record starts the day the 20:50 job first stores scorecards.</div><div id="scr" style="margin-top:6px"></div></div></div>
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
 try{const r=rebuild?await post(`/api/research/equity/${encodeURIComponent(s)}/refresh`):await j(`/api/research/equity/${encodeURIComponent(s)}`);el.innerHTML=render(r);scard(s);esurp(s)}catch(e){el.innerHTML=`<div class="card bad">${esc(e.message)}</div>`}}
async function scard(s){const el=document.getElementById('scd');if(!el)return;
 try{el.innerHTML=scorecard(await j(`/api/research/scorecard/${encodeURIComponent(s)}`))}catch(e){el.innerHTML=`<div class="card muted">No fundamental scorecard: ${esc(e.message)}</div>`}
 const dv=document.getElementById('dvd');if(!dv)return;
 try{dv.innerHTML=dvmcard(await j(`/api/research/dvm/${encodeURIComponent(s)}`))}catch(e){dv.innerHTML=`<div class="card muted">No DVM view: ${esc(e.message)}</div>`}}
const ZCLS={STRONG_PERFORMER:'BUY',VALUE_UNDER_RADAR:'ADD',EXPENSIVE_PERFORMER:'ADD',MID_RANGE:'NOT_RATED',WEAK:'REDUCE',VALUE_TRAP:'SELL',MOMENTUM_TRAP:'SELL'};
function dvmcard(d){const lv=d.levels||{high:55,low:35};
 const ax=a=>`<div class="ax dvm"><b>${esc(a.label)}</b> <span class="v">${a.score==null?'—':n(a.score,0)}</span><span class="muted"> / 100${a.level?' · '+stl(a.level):''}</span>
 <div class="meter" role="img" aria-label="${esc(a.label)} ${a.score==null?'unknown':n(a.score,0)+' of 100'}" title="${esc(a.label)}: ${a.score==null?'unknown':n(a.score,0)} (low below ${lv.low}, high ${lv.high}+)"><span style="width:${a.score==null?0:Math.max(0,Math.min(100,a.score))}%"></span><i style="left:${lv.low}%"></i><i style="left:${lv.high}%"></i></div>
 <div class="muted" style="font-size:11.5px">${esc(a.basis)}</div>
 ${a.components.map(c=>`<div class="ck"><span class="s">${c.score==null?'·':n(c.score,0)}</span>${esc(c.label)} <span class="muted">${c.score==null?'(no data)':'× '+n(c.weight*100,0)+'%'}</span><div class="d">${esc(c.detail)}</div></div>`).join('')}</div>`;
 const z=d.zone;
 return `<div class="card"><h3>DVM view: ${z?`<span class="pill ${ZCLS[z.key]||'NOT_RATED'}">${esc(z.label)}</span> <span class="muted">${esc(z.reading)}</span>`:`<span class="muted">${esc(d.note||'no zone')}</span>`}</h3>
 <div class="muted">Durability, valuation and momentum, 0–100 each (Trendlyne-style): durability from the scorecard's financial-health and past-performance checks, valuation from the research model's fair value and the P/E against the industry (P/B for financials; high = cheap), momentum from the daily technical rating and the RS rating. High is ${lv.high}+, low below ${lv.low} (the two marks on each bar).${z?` Zone rule: ${esc(z.rule)}.`:''} A description, not advice: the zones have no track record yet.</div>
 <div class="axes">${d.axes.map(ax).join('')}</div></div>`}
async function esurp(s){const el=document.getElementById('esd');if(!el)return;
 try{el.innerHTML=ecard(await j(`/api/research/earnings-surprise/${encodeURIComponent(s)}`))}catch(e){el.innerHTML=`<div class="card muted">Earnings surprise: ${esc(e.message)}</div>`}}
function ecard(o){const x=o.latest||{},sg=v=>v==null?'—':(v>0?'+':'')+n(v,2);
 const why=(v,r)=>v==null&&r?`<div class="muted" style="font-size:11.5px;margin-top:3px">${esc(r)}</div>`:'';
 const tr=x.eps_trend?`${esc(stl(x.eps_trend))} <span class="muted" style="font-size:12px">${x.eps_trend_pts>0?'+':''}${n(x.eps_trend_pts,1)} pts</span>`:'—';
 const sig=(o.signals||[]).slice(0,5).map(s=>`<tr><td>${esc(String(s.date).slice(0,10))}</td><td>${s.direction==='BULL'?'▲ positive':'▼ negative'}</td><td>${rs(s.entry)}</td><td>${rs(s.stop)}</td><td>${rs(s.target)}</td><td>${s.confluence}/6</td><td>${esc(s.status)}</td><td>${pct(s.excess_60d)}</td></tr>`);
 const rec=(o.record||[]).map(r=>`${esc(r.name)}: ${r.closed} closed, avg R ${r.avg_r==null?'—':n(r.avg_r,2)}, alerts ${r.alerts==='on'?'on':`held (${Math.min(r.closed,30)}/30)`}`).join(' · ');
 return `<div class="card"><h3>Earnings surprise <span class="muted">${esc(x.quarter||'')} · filed ${esc(String(x.available_from||x.known_on||'').slice(0,16))}${x.exact_date?'':' (estimated)'} · ${x.days_since_result} days ago</span></h3>
 <div class="muted">${esc(o.method)}</div>
 <div class="grid" style="margin-top:8px"><div class="stat"><div class="k">EPS surprise (SUE)</div><div class="v">${sg(x.sue)}</div>${why(x.sue,x.sue_reason)}</div>
 <div class="stat"><div class="k">Revenue surprise (SUE)</div><div class="v">${sg(x.sue_revenue)}</div>${why(x.sue_revenue,x.sue_revenue_reason)}</div>
 <div class="stat"><div class="k">EPS trend (revisions proxy)</div><div class="v">${tr}</div>${why(x.eps_trend,x.eps_trend_reason)}</div>
 <div class="stat"><div class="k">EPS vs a year earlier</div><div class="v">${n(x.eps_q)} <span class="muted" style="font-size:12px">vs ${n(x.eps_year_ago)}</span></div></div></div>
 ${sig.length?`<div style="margin-top:8px">${table(['Drift signal','Surprise','Entry','Stop','Target','Confluence','Status','vs Nifty 60d'],sig)}</div>`:`<div class="muted" style="margin-top:6px">No post-earnings-drift signal (it needs |SUE| of ${n(o.trigger,0)}+ on the first session after a filing).</div>`}
 ${rec?`<div class="muted" style="margin-top:6px">${rec}</div>`:''}</div>`}
const AXN={value:'Value',growth:'Growth',past:'Past',health:'Health',dividend:'Dividend'};
function flake(axes){const cx=160,cy=118,R=80,ang=i=>(-90+72*i)*Math.PI/180,pt=(i,r)=>[cx+r*Math.cos(ang(i)),cy+r*Math.sin(ang(i))];
 const ring=k=>axes.map((_,i)=>pt(i,R*k/6).map(v=>v.toFixed(1)).join(',')).join(' ');
 const shape=axes.map((a,i)=>pt(i,R*Math.max(a.passed,0.15)/6).map(v=>v.toFixed(1)).join(',')).join(' ');
 const lab=axes.map((a,i)=>{const[x,y]=pt(i,R+16),an=Math.abs(x-cx)<5?'middle':x>cx?'start':'end';return `<text x="${x.toFixed(1)}" y="${(y+(y<cy-40?-2:y>cy+20?10:4)).toFixed(1)}" text-anchor="${an}" font-size="11" fill="#e2e8f0">${AXN[a.key]||esc(a.label)} <tspan fill="#94a3b8">${a.passed}/6</tspan></text>`}).join('');
 return `<svg viewBox="0 0 320 236" width="100%" style="max-width:300px" role="img" aria-label="${esc(axes.map(a=>`${a.label} ${a.passed} of 6`).join(', '))}">
 ${[2,4,6].map(k=>`<polygon points="${ring(k)}" fill="none" stroke="#334155"/>`).join('')}${axes.map((_,i)=>{const[x,y]=pt(i,R);return `<line x1="${cx}" y1="${cy}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}" stroke="#334155"/>`}).join('')}
 <polygon points="${shape}" fill="#3987e5" fill-opacity="0.35" stroke="#3987e5" stroke-width="2"/>${lab}</svg>`}
function scorecard(sc){const mark=p=>p===true?'<i class="ok">✓</i>':p===false?'<i class="bad">✗</i>':'<i class="muted">·</i>';
 return `<div class="card"><h3>Fundamental scorecard: ${sc.checks_passed} of 30 checks passed <span class="muted">(${sc.checks_known} could be checked)</span></h3>
 <div class="muted">Five axes of six pass / fail checks over the stored fundamentals, each with the numbers it used: <span class="ok">✓</span> pass, <span class="bad">✗</span> fail, <b>·</b> no data or not meaningful${sc.financial?' (debt checks are not scored for a bank or NBFC: compare it with other financials)':''}. Growth uses the latest reported figures: ATIP holds no analyst forecasts.</div>
 <div class="axes"><div class="ax fl">${flake(sc.axes)}</div>${sc.axes.map(a=>`<div class="ax"><b>${esc(a.label)}</b> <span class="muted">${a.passed}/6</span>${a.checks.map(c=>`<div class="ck">${mark(c.pass)}${esc(c.label)}<div class="d">${esc(c.detail)}</div></div>`).join('')}</div>`).join('')}</div></div>`}
async function screc(){const h=document.getElementById('sch').value;const o=await j(`/api/research/scorecard-record?horizon=${h}`);
 document.getElementById('scr').innerHTML=table(['Checks passed','Samples','Beat the Nifty','Median excess','Mean excess'],o.bands.map(b=>`<tr><td>${esc(b.band)}</td><td>${b.n}${b.enough?'':' <span class="muted">(too few)</span>'}</td><td>${b.beat_nifty_pct==null?'—':b.beat_nifty_pct+'%'}</td><td>${pct(b.median_excess_pct)}</td><td>${pct(b.mean_excess_pct)}</td></tr>`))+
 `<div class="muted" style="margin-top:6px">${o.top_minus_bottom_pct==null?`Top band minus bottom band: not yet (${o.stored_days} days stored).`:`Top band minus bottom band: <b>${pct(o.top_minus_bottom_pct)}</b> over ${o.horizon} sessions.`}</div>`}
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
 <div id="scd"><div class="card muted">Loading the scorecard…</div></div><div id="dvd"></div>
 <div id="esd"></div>
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
async function rat(){screc();const r=document.getElementById('rf').value;const d=await j('/api/research/equity'+(r?`?rating=${r}`:''));
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
<div class="wrap"><div class="card"><h3>Stock screener</h3>
<div class="muted">Filter every stock ATIP tracks on fundamentals, technicals or both, Screener.in / Chartink style: <code>roce_pct &gt; 20 AND debt_equity &lt; 0.5 AND (pe &lt; 25 OR peg &lt; 1)</code>, <code>scan_golden_cross = 1 AND rs_rating &gt;= 80</code>, <code>patterns CONTAINS "engulfing" AND rsi_14 &lt; 40</code>, <code>industry IN ("Capital Goods")</code>. Percentages are in %, money in ₹ crore where the field ends in _cr. A stock missing a field never matches a condition on it.</div>
<div id="presets" style="margin:8px 0;display:flex;flex-wrap:wrap;gap:4px"></div>
<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:6px"><input id="nl" style="flex:1;min-width:220px" placeholder="Ask in English: debt-free capital goods companies with ROCE above 20% near their 52-week high" onkeydown="if(event.key==='Enter')ask()"><button onclick="ask()">Write the query</button></div>
<div id="nlo" class="muted" style="margin-bottom:6px"></div>
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
 if(k==='research_rating')return pill(v);if(k==='tech_rating_label')return `<span style="white-space:nowrap">${esc(String(v).replace('_',' '))}</span>`;if(f.kind==='text')return esc(v);
 if(f.unit==='%')return n(v,1)+'%';if(f.unit==='pts')return (v>0?'+':'')+n(v,2);if(f.unit==='₹ cr')return n(v,0);if(f.unit==='₹')return n(v,2);if(f.unit==='x')return n(v,2);return n(v,2)};
(async()=>{const c=await j('/api/screener/fields');CAT={...c,fields:{}};
 Object.values(c.groups).flat().forEach(f=>CAT.fields[f.key]=f);
 const opts=Object.entries(c.groups).map(([g,fs])=>`<optgroup label="${esc(g)}">${fs.map(f=>`<option value="${f.key}">${esc(f.label)}${f.unit?' ('+esc(f.unit)+')':''}</option>`).join('')}</optgroup>`).join('');
 document.getElementById('bf').innerHTML=opts;document.getElementById('bf').value='roce_pct';document.getElementById('sort').innerHTML='<option value="">first field</option>'+opts;
 const G={fundamental:'Fundamental',technical:'Technical',combined:'Fundamental + technical'};
 document.getElementById('presets').innerHTML=Object.keys(G).map(g=>`<div style="width:100%;margin-top:4px" class="muted">${G[g]}</div><div style="display:flex;flex-wrap:wrap;gap:4px">${c.presets.map((p,i)=>p.group===g?`<button title="${esc(p.description)}" onclick="preset(${i})" style="background:var(--panel);border:1px solid var(--line)">${esc(p.name)}</button>`:'').join('')}</div>`).join('');
 document.getElementById('help').innerHTML=Object.entries(c.groups).map(([g,fs])=>`<h3 style="margin-top:6px">${esc(g)}</h3>`+table(['Field','Name','Unit','Notes'],fs.map(f=>`<tr><td><code>${f.key}</code></td><td>${esc(f.label)}</td><td>${esc(f.unit||f.kind)}</td><td class="muted">${esc([f.description,f.aliases.length?'also: '+f.aliases.join(', '):''].filter(Boolean).join(' · '))}</td></tr>`))).join('');
 saved();const q=new URLSearchParams(location.search).get('q');if(q){document.getElementById('q').value=q;go()}})();
function preset(i){const p=CAT.presets[i];document.getElementById('q').value=p.query;document.getElementById('sort').value=p.sort||'';document.getElementById('dir').value=p.desc===false?'0':'1';go()}
function addCond(join){const f=val('bf'),o=val('bo'),v=val('bv').trim();if(!v)return;const isText=CAT.fields[f].kind==='text';
 const lit=isText&&!/^".*"$/.test(v)?`"${v.replace(/"/g,'')}"`:v;const q=document.getElementById('q');q.value=(q.value.trim()?q.value.trim()+` ${join} `:'')+`${f} ${o} ${lit}`;document.getElementById('bv').value=''}
const val=id=>document.getElementById(id).value;
function params(extra){const p=new URLSearchParams({query:val('q').trim(),desc:val('dir'),limit:'500',...(extra||{})});if(val('sort'))p.set('sort',val('sort'));return p}
async function ask(){const t=val('nl').trim();if(!t)return;const o=document.getElementById('nlo');o.textContent='Translating…';
 try{const r=await post('/api/screener/ask',{text:t});document.getElementById('q').value=r.query||'';
  if(r.sort){const s=document.getElementById('sort');if(![...s.options].some(x=>x.value===r.sort))s.add(new Option(r.sort,r.sort));s.value=r.sort;document.getElementById('dir').value=r.desc?'1':'0'}
  o.innerHTML=`${r.mode==='claude'?'Written by Claude':'Written by the rule translator'+(r.fallback_reason?` <span title="${esc(r.fallback_reason)}">(Claude not used: ${esc(r.fallback_reason)})</span>`:'')}. ${r.valid?'Check the query, then <b>Run screen</b>.':`<span class="bad">${esc(r.error||'not a valid query')}</span>`}${r.explanation?' · '+esc(r.explanation):''}${(r.unmatched||[]).length?` · <span class="note">not understood: ${esc(r.unmatched.join(', '))}</span>`:''}`}
 catch(e){o.innerHTML=`<span class="bad">${esc(e.message)}</span>`}}
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


SIGNALS = _HEAD + r"""
<div class="wrap"><div class="tabs" id="tabs"></div>
<div class="pane" id="p-today"><div class="card"><h3>Technical signals <span class="muted" id="asof"></span></h3>
<div class="muted">End-of-day scans for the next session (research/technicals.py): crossovers, breakouts on volume, oscillator turns, Supertrend, squeezes, trend templates. Each signal has an entry (the close), a stop 2×ATR away and a target 4×ATR away (2R), and a <b>confluence</b> count of independent agreeing evidence out of 6: technical rating, volume, relative strength vs Nifty, market regime, a candle pattern, the research rating. Each also carries the <b>market gate</b> it was born under: a long signal while the gate is closed is <i>against the market</i>. The <b>Weekly</b> column is the same rating on completed weekly bars: ✓ when it is on the signal's side. In brackets after each scan: its record so far in today's market, how often it beat the Nifty over 20 sessions · its median excess (grey: fewer than 10 signals; * across all markets). Not advice.</div>
<div id="gate" style="margin-top:8px"></div>
<div style="margin-top:8px;display:flex;gap:6px;align-items:center;flex-wrap:wrap"><select id="dirf" onchange="today()"><option value="">Bullish and bearish</option><option value="BULL">Bullish</option><option value="BEAR">Bearish</option></select>
<select id="alf" onchange="draw()"><option value="not_against" selected>Hide signals against the market</option><option value="">All signals</option><option value="WITH">Only with the market</option></select>
<span class="muted">min confluence</span><select id="mc" onchange="today()"><option>0</option><option>1</option><option selected>2</option><option>3</option><option>4</option><option>5</option><option>6</option></select>
<button onclick="runNow()">Recompute now</button><span id="ro" class="muted"></span></div></div>
<div class="card"><div id="sig" style="overflow-x:auto"></div></div></div>
<div class="pane" id="p-rec"><div class="card"><h3>Track record per scan</h3>
<div class="muted">Every signal is followed until it reaches its target (win), its stop, or 20 sessions (expired at the close). Average R: +2 is a target, −1 a stop. Expectancy above 0 means the scan has paid on ATIP's own stocks; a few dozen closed signals are needed before trusting it. Chart-pattern scans (Darvas box, VCP, double bottom, ascending and descending triangles, head and shoulders and its inverse, rising and falling channels and wedges) are tracked from day one but only alert once 30 of their signals have closed with a positive average R.</div>
<div style="margin-top:6px;display:flex;gap:6px;align-items:center;flex-wrap:wrap"><span class="muted">only signals with confluence ≥</span> <select id="rc" onchange="rec()"><option>0</option><option>1</option><option>2</option><option>3</option><option>4</option></select>
<select id="ra" onchange="rec()"><option value="">born in any market</option><option value="WITH">born with the market</option><option value="MIXED">born in a mixed market</option><option value="AGAINST">born against the market</option></select></div>
<h3 style="margin-top:10px">Does the market gate help?</h3><div id="geff" class="sx"></div>
<h3 style="margin-top:10px">Per scan</h3>
<div id="stats" style="margin-top:4px;overflow-x:auto"></div></div>
<div class="card"><h3>Against the Nifty, by market and holding period</h3>
<div class="muted">Separately from the stop and target, every signal's return is recorded 5, 20 and 60 sessions later and compared with the Nifty over the same days (signed for its direction). Each cell: how often the signal beat the Nifty · its median excess return (number of signals). Split by the market gate the signal was born under. Grey cells have fewer than 10 signals: too few to judge.</div>
<div style="margin-top:6px;display:flex;gap:6px;align-items:center;flex-wrap:wrap"><span class="muted">holding period</span><select id="rh" onchange="fwd()"><option value="5">5 sessions</option><option value="20" selected>20 sessions</option><option value="60">60 sessions</option></select><span class="muted">(confluence filter above applies)</span></div>
<div id="fwall" style="margin-top:8px" class="sx"></div>
<h3 style="margin-top:10px">Does confluence add?</h3><div id="fwconf" class="sx"></div>
<h3 style="margin-top:10px">Does the weekly trend add?</h3><div id="fwweek" class="sx"></div>
<h3 style="margin-top:10px">Per scan</h3><div id="fwscan" class="sx"></div><div id="fwnote" class="muted" style="margin-top:4px"></div></div></div>
<div class="pane" id="p-intra"><div class="card"><h3>Intraday signals <input id="idate" type="date" onchange="intra()"> <button onclick="post('/api/signals/intraday/run').then(intra)">Scan now</button></h3>
<div class="muted">On the 15-minute bars ATIP stores every 30 minutes (Dhan intraday data): opening-range breakout / breakdown (the first close beyond the 09:15–09:30 range, by 11:30, on 1.5× the slot's usual volume), open = low / open = high (judged on the first hour: no trade more than 0.1 % beyond the open and 0.5 %+ away from it by 10:15), and the TTM squeeze on 15-minute bars (on 1.2× volume). Each is measured from the price when ATIP saw it, to the session's close, and against the Nifty over the same minutes. RVol: the trigger bar's volume over the same slot in the last 5 sessions. A scan alerts only after 30 closed hits with a positive record. Not advice.</div>
<div id="isig" class="sx" style="margin-top:6px"></div></div>
<div class="card"><h3>Intraday record per scan</h3><div id="istat" class="sx"></div></div></div>
</div><div id="tip"></div>
<script>""" + _JS_COMMON + r"""
const TABS=[['today','Today'],['rec','Track record'],['intra','Intraday']];const loaded={};
function show(id){document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.dataset.id===id));document.querySelectorAll('.pane').forEach(p=>p.classList.toggle('on',p.id==='p-'+id));
 if(!loaded[id]){loaded[id]=1;({today,rec,intra})[id]()}}
const cellp=c=>c?`${c.positive_pct}% · ${pct(c.median_pct)} <span class="muted">(${c.n})</span>`:'<span class="muted">—</span>';
async function intra(){const d=document.getElementById('idate').value;const rows=await j('/api/signals/intraday'+(d?`?date=${d}`:''));
 document.getElementById('isig').innerHTML=table(['Symbol','','Scan','Bar','Seen at','Seen price','Level','RVol','Why','To the close','vs Nifty','Alerts'],rows.map(x=>`<tr><td><a href="/research?symbol=${encodeURIComponent(x.symbol)}" style="color:var(--accent)">${esc(x.symbol)}</a></td><td>${dpill(x.direction)}</td><td>${esc(x.name)}</td><td>${esc(String(x.bar_ts||'').slice(11,16))}</td><td>${esc(String(x.seen_at||'').slice(11,16))}</td><td>${n(x.seen_price,2)}</td><td>${n(x.level,2)}</td><td>${x.rvol_slot==null?'—':n(x.rvol_slot,2)+'×'}</td><td class="muted">${esc(x.reason)}</td><td>${pct(x.ret_close_pct)}</td><td>${pct(x.excess_close_pct)}</td><td>${x.alerts==='on'?'<span class="ok">on</span>':'<span class="muted">held</span>'}</td></tr>`));
 const st=await j('/api/signals/intraday/stats');
 document.getElementById('istat').innerHTML=table(['Scan','','Hits','Closed','Made money · median (n)','Beat the Nifty · median excess (n)','Avg best / worst after seen','Alerts'],st.map(s=>`<tr><td>${esc(s.name)}</td><td>${dpill(s.direction)}</td><td>${s.hits}</td><td>${s.closed}${s.enough?'':' <span class="muted">(too few)</span>'}</td><td>${cellp(s.to_close)}</td><td>${cellp(s.vs_nifty)}</td><td>${s.avg_best_pct==null?'—':pct(s.avg_best_pct)+' / '+pct(s.avg_worst_pct)}</td><td>${s.alerts==='on'?'<span class="ok">on</span>':`<span class="muted">held · ${s.closed}/30</span>`}</td></tr>`))}
document.getElementById('tabs').innerHTML=TABS.map(([id,l])=>`<div class="tab" data-id="${id}" onclick="show('${id}')">${l}</div>`).join('');
const dpill=d=>`<span class="pill ${d==='BULL'?'BUY':d==='BEAR'?'SELL':'NOT_RATED'}">${d==='BULL'?'▲ bull':d==='BEAR'?'▼ bear':esc(d)}</span>`;
let SIG=[];
const wkc=x=>!x.tech_rating_w_label?'<span class="muted" title="needs 35+ completed weeks of history">—</span>':`<span class="${x.weekly_agrees===1?'ok':x.weekly_agrees===0?'bad':''}" title="${x.weekly_agrees===1?'the weekly rating is on this signal\'s side':'the weekly rating is not on this signal\'s side'}">${x.weekly_agrees===1?'✓ ':'✗ '}${esc(x.tech_rating_w_label.replace('_',' '))}</span>`;
const recb=s=>{const r=s.record;if(!r)return '';const m=(r.median_excess_pct>0?'+':'')+r.median_excess_pct;
 const t=`Over ${s.record_horizon} sessions ${s.record_scope==='gate'?'in a market with the gate '+(s.market_gate||'').toLowerCase():'in all markets (none yet in this one)'}: beat the Nifty ${r.beat_nifty_pct}% of ${r.n} times, median excess ${m}%${r.enough?'':'. Too few to judge.'}`;
 return ` <span class="${r.enough?(r.median_excess_pct>0?'ok':'bad'):'muted'}" style="font-size:11px;white-space:nowrap" title="${esc(t)}">[${r.beat_nifty_pct}% · ${m}%${s.record_scope==='gate'?'':'*'}]</span>`};
async function gate(){try{const g=await j('/api/market-regime');const el=document.getElementById('gate');
 if(!g.gate){el.innerHTML=`<span class="muted">Market gate: ${esc(g.reason||'not computed yet')}</span>`;return}
 const long=g.gate==='OPEN'?'long signals are <b>with</b> the market':g.gate==='CAUTION'?'mixed market: long signals are <b>mixed</b>, size down':'long signals are <b>against</b> the market; short signals are with it';
 el.innerHTML=`Market gate ${gpill(g.gate)} <b>${esc(stl(g.status))}</b> since ${esc(g.status_since||'—')} · ${g.dd_count} distribution day${g.dd_count===1?'':'s'} in ${g.rules.dd_window} sessions · Nifty ${g.above_200dma==null?'(200-DMA n/a)':g.above_200dma?'above':'below'} its 200-DMA · ${long}. <a href="/market-pulse" style="color:var(--accent)">Details</a>`}catch(e){}}
async function today(){const p=new URLSearchParams({min_confluence:document.getElementById('mc').value});const d=document.getElementById('dirf').value;if(d)p.set('direction',d);
 SIG=await j('/api/signals/technical?'+p);draw()}
function draw(){const af=document.getElementById('alf').value,r=SIG.filter(x=>!af||(af==='not_against'?x.alignment!=='AGAINST':x.alignment===af));
 const hidden=SIG.length-r.length;const g={};r.forEach(x=>{const k=x.symbol+'|'+x.direction;(g[k]=g[k]||{...x,list:[]}).list.push(x)});
 const rows=Object.values(g).sort((a,b)=>b.confluence-a.confluence||b.list.length-a.list.length||a.symbol.localeCompare(b.symbol));
 document.getElementById('asof').textContent=SIG.length?`· ${String(SIG[0].date).slice(0,10)} · ${rows.length} stocks, ${r.length} signals${hidden?` (${hidden} hidden by the market filter)`:''}`:'· none yet';
 document.getElementById('sig').innerHTML=table(['Symbol','','Market','Signals','Entry','Stop','Target','Confluence','Evidence','Rating','Weekly','Patterns'],rows.map(x=>`<tr><td><a href="/research?symbol=${encodeURIComponent(x.symbol)}" style="color:var(--accent)">${esc(x.symbol)}</a></td><td>${dpill(x.direction)}</td><td>${apill(x.alignment)}</td><td>${x.list.map(s=>`<span title="${esc(s.reason||'')}">${esc(s.name)}</span>${recb(s)}`).join(' · ')}</td><td>${n(x.entry,2)}</td><td>${n(x.stop,2)}</td><td>${n(x.target,2)}</td><td><b>${x.confluence}</b>/6</td><td class="muted">${Object.entries(x.evidence||{}).filter(([k,v])=>v).map(([k])=>esc(k)).join(', ')}</td><td style="white-space:nowrap">${esc((x.tech_rating_label||'').replace('_',' '))}</td><td style="white-space:nowrap">${wkc(x)}</td><td class="muted">${esc(x.patterns||'')}</td></tr>`))}
const fcell=c=>!c?'<span class="muted">—</span>':`<span class="${c.enough?(c.median_excess_pct>0?'ok':'bad'):'muted'}" style="white-space:nowrap">${c.beat_nifty_pct}% · ${c.median_excess_pct>0?'+':''}${c.median_excess_pct}%</span> <span class="muted">(${c.n})</span>`;
const GCOLS=[['ALL','All markets'],['OPEN','Gate open'],['CAUTION','Caution'],['CLOSED','Gate closed']];
async function fwd(){const h=document.getElementById('rh').value,mc=document.getElementById('rc').value;
 const f=await j(`/api/signals/technical/forward?horizon=${h}&min_confluence=${mc}`);
 document.getElementById('fwall').innerHTML=table(['All signals',...GCOLS.map(g=>g[1])],[`<tr><td class="muted">beat the Nifty · median excess (n)</td>${GCOLS.map(([k])=>`<td>${fcell(f.overall[k])}</td>`).join('')}</tr>`]);
 document.getElementById('fwconf').innerHTML=table(['Confluence',...GCOLS.map(g=>g[1])],f.by_confluence.map(b=>`<tr><td>${esc(b.band)} of 6</td>${GCOLS.map(([k])=>`<td>${fcell(b.cells[k])}</td>`).join('')}</tr>`));
 const WK={AGREES:'Weekly rating on the signal\'s side',DISAGREES:'Weekly rating not on its side',NO_WEEKLY:'No weekly rating yet'};
 document.getElementById('fwweek').innerHTML=table(['Weekly trend',...GCOLS.map(g=>g[1])],f.by_weekly.map(b=>`<tr><td>${esc(WK[b.weekly])}</td>${GCOLS.map(([k])=>`<td>${fcell(b.cells[k])}</td>`).join('')}</tr>`));
 document.getElementById('fwscan').innerHTML=table(['Scan','',...GCOLS.map(g=>g[1])],f.by_scan.map(o=>`<tr><td>${esc(o.name)}</td><td>${dpill(o.direction)}</td>${GCOLS.map(([k])=>`<td>${fcell(o.cells[k])}</td>`).join('')}</tr>`));
 document.getElementById('fwnote').textContent=f.note}
async function rec(){fwd();const mc=document.getElementById('rc').value,al=document.getElementById('ra').value;
 const [r,ge]=await Promise.all([j('/api/signals/technical/stats?min_confluence='+mc+(al?'&alignment='+al:'')),j('/api/signals/technical/gate-effect?min_confluence='+mc)]);
 document.getElementById('geff').innerHTML=table(['Signals born','Open','Closed','Win rate','Avg R','Avg return'],ge.groups.map(o=>`<tr><td>${o.alignment==='UNKNOWN'?'<span class="muted">before the gate</span>':apill(o.alignment)}</td><td>${o.open}</td><td>${o.closed}</td><td>${o.win_rate_pct==null?'—':o.win_rate_pct+'%'}</td><td>${o.avg_r==null?'—':(o.avg_r>0?'+':'')+o.avg_r}</td><td>${pct(o.avg_return_pct)}</td></tr>`))+`<div class="muted" style="margin-top:4px">${esc(ge.verdict||ge.note)}</div>`;
 document.getElementById('stats').innerHTML=table(['Scan','','Open','Closed','Target','Stopped','Expired','Win rate','Avg R','Avg return','Alerts'],r.map(o=>`<tr><td>${esc(o.name)}</td><td>${dpill(o.direction)}</td><td>${o.open}</td><td>${o.closed}</td><td>${o.target}</td><td>${o.stopped}</td><td>${o.expired}</td><td>${o.win_rate_pct==null?'—':o.win_rate_pct+'%'}</td><td>${o.avg_r==null?'—':(o.avg_r>0?'+':'')+o.avg_r}</td><td>${pct(o.avg_return_pct)}</td><td>${o.alerts==='held'?`<span class="muted" style="white-space:nowrap" title="a chart-pattern or post-earnings-drift scan alerts once 30 of its signals have closed with a positive average R">held · ${Math.min(o.closed,30)}/30</span>`:'on'}</td></tr>`))}
async function runNow(){document.getElementById('ro').textContent=' computing…';try{const o=await post('/api/signals/technical/run');document.getElementById('ro').textContent=` ${o.rows} stocks, ${o.signals} signals (${o.as_of})`;gate();today()}catch(e){document.getElementById('ro').textContent=' '+e.message}}
gate();show('today');
</script></body></html>"""


PULSE = _HEAD + r"""
<div class="wrap">
<div class="card"><h3>Market context <span id="ctx"></span> <span class="muted" id="asof"></span></h3>
<div id="reasons"></div>
<div style="margin-top:6px"><button onclick="act('/api/market-pulse/gift','captured')">Capture GIFT Nifty now</button><button onclick="act('/api/market-regime/run','gate recomputed').then(gateCard)">Recompute market gate</button><button onclick="act('/api/market-pulse/refresh','refreshed')">Fetch NSE positioning + Nifty history</button><button onclick="act('/api/orderbook/snapshot','polled')">Poll order book now</button><span id="ao" class="muted"></span></div>
<div class="muted" style="margin-top:4px">Built from ATIP's own models and stored data; each part reports its own track record. Not advice.</div></div>
<div class="card"><h3>Market gate <span id="gpill"></span> <span class="muted" id="gsince"></span></h3>
<div class="muted">Should new long signals be taken? An IBD-style read of the Nifty from ATIP's data: distribution days (down sessions on higher market volume), corrections, rally attempts and follow-through days, plus the 200-DMA. <a href="/signals" style="color:var(--accent)">Signals</a> are tagged with, mixed or against it, and the track record shows whether that helps.</div>
<div class="two" style="margin-top:8px"><div id="gsum" class="sx"></div><div id="gchanges" class="sx"></div></div>
<div id="gchart" style="margin-top:6px;position:relative"></div>
<div class="legend"><span><i style="border-color:#3987e5"></i>Nifty 50</span><span><i style="border-color:#d95926;border-top-style:dashed"></i>200-DMA</span><span style="color:var(--text)">▼</span><span>distribution day</span><span style="color:var(--text)">▲</span><span>follow-through day</span><span><b class="sw" style="background:#0ca30c"></b>gate open</span><span><b class="sw" style="background:#fab219"></b>caution</span><span><b class="sw" style="background:#d03b3b"></b>closed</span></div>
<details><summary class="muted">Table view (last 30 sessions)</summary><div id="gtable" class="sx"></div></details></div>
<div class="card"><h3>Event calendar</h3>
<div class="muted">US Fed decisions, US CPI and the US jobs report come out after India's close, so they hit the next session's open; the RBI decides at 10:00 IST, during the session. On a morning after a US release the gap estimate is given a wider band.</div>
<div id="evs" class="sx" style="margin-top:6px"></div>
<details style="margin-top:6px"><summary class="muted">Add an event (an unscheduled meeting, next year's dates)</summary><div style="margin-top:6px;display:flex;flex-wrap:wrap;gap:6px;align-items:center"><input id="evd" type="date"><select id="evk"></select><input id="evt" placeholder="title (optional)" style="width:220px"><button onclick="addEv()">Add</button><span id="evo" class="muted"></span></div></details></div>
<div class="two"><div class="card"><h3>Global cues → Nifty</h3><div id="glob" class="sx"></div></div>
<div class="card"><h3>FII / DII money</h3><div id="fii" class="sx"></div></div></div>
<div class="two"><div class="card"><h3>Derivatives positioning</h3><div id="pos" class="sx"></div></div>
<div class="card"><h3>Pending orders across the market</h3><div id="book" class="sx"></div></div></div>
<div class="card"><h3>20-level depth (watchlist)</h3>
<div class="muted">Dhan's 20-level order book for up to 50 watchlist stocks, sampled every 15 seconds. DWI weighs the levels within 0.5 % of the mid, nearest first (e<sup>−0.5(k−1)</sup>): +1 all bids, −1 all asks. A stock is flagged when |DWI| &gt; 0.3 in its last 3 snapshots. OFI (order-flow imbalance, Cont, Kukanov &amp; Stoikov) adds up every quote change at the best bid and ask since the stock's previous snapshot, from every update the feed receives: bids added or the bid raised count as buying, asks added or the ask lowered as selling, in units of the average best-level depth. OFI 20 does the same at each of the 20 levels (Xu, Gould &amp; Howison), divided by the book's average depth per level, so stocks compare. Research on NSE stocks finds order-book imbalance predictive for minutes at most; the check below says whether it is on ATIP's own data. Context, not a signal.</div>
<div id="d20" class="sx" style="margin-top:6px"></div></div>
<div class="card"><h3>Your pending orders</h3><div id="mine" style="overflow-x:auto"></div></div>
</div><div id="tip"></div>
<script>""" + _JS_COMMON + r"""
const lab=s=>s?`<span class="pill ${/POSITIVE|INFLOW|RISK_ON|COVERING/.test(s)?'BUY':/NEGATIVE|OUTFLOW|RISK_OFF|CROWDED/.test(s)?'SELL':'NOT_RATED'}">${esc(s.replaceAll('_',' '))}</span>`:'';
const kv=rows=>table(['',''],rows.filter(r=>r[1]!==undefined).map(([a,b])=>`<tr><td class="muted">${a}</td><td>${b}</td></tr>`)).replace('<thead><tr><th></th><th></th></tr></thead>','');
const cr=v=>v==null||isNaN(v)?'—':(v<0?'−':'')+'₹'+n(Math.abs(v),0)+' cr';
const bar=(v,max)=>{const w=Math.min(100,Math.abs(v)/max*100);return `<div style="display:flex;align-items:center;gap:4px"><div style="width:120px;height:8px;background:#0f172a;border-radius:4px;position:relative"><div style="position:absolute;${v>=0?'left:50%':'right:50%'};width:${w/2}%;height:8px;border-radius:4px;background:${v>=0?'#3987e5':'#d95926'}"></div></div><span>${v>=0?'+':'−'}${n(Math.abs(v),3)}%</span></div>`};
const TIM={before_open:'before the open',intraday:'during the session'};
const OVN={FOMC:'the Fed decision',US_CPI:'US CPI',US_NFP:'the US jobs report'};
const ovn=ev=>String(ev||'').split(',').filter(k=>OVN[k]).map(k=>OVN[k]).join(' and ');
function evCard(p){const ev=p.events||{},b=ev.band||{},gr=(p.gap_record||{}).by_events;let h='';
 if((ev.today||[]).length)h+=`<div class="note" style="margin-bottom:6px">Today: ${ev.today.map(e=>`${esc(e.label)} (${esc(e.event_date)}, ${esc(e.time_ist)} IST, ${TIM[e.timing]})`).join('; ')}${b.band_pct!=null?`. Gap band ± ${n(b.band_pct,2)}%: the usual miss of ${n(b.typical_miss_pct,2)}% × ${b.widen} (${esc(b.widen_basis)}).`:''}</div>`;
 h+=table(['Released','Event','Time (IST)','Hits the session of','When'],(ev.upcoming||[]).map(e=>`<tr><td>${esc(e.event_date)}</td><td>${esc(e.label)}</td><td>${esc(e.time_ist)}</td><td>${esc(e.session)}</td><td>${TIM[e.timing]}</td></tr>`));
 if(gr)h+=`<div class="muted" style="margin-top:6px">Gap-estimate miss after a US release: ${gr.after_us_release.mean_abs_error_pct==null?'—':gr.after_us_release.mean_abs_error_pct+'%'} over ${gr.after_us_release.mornings} mornings, other mornings ${gr.other.mean_abs_error_pct==null?'—':gr.other.mean_abs_error_pct+'%'} over ${gr.other.mornings}. Widening ×${gr.widen.factor} (${esc(gr.widen.basis)}).</div>`;
 document.getElementById('evs').innerHTML=h}
async function addEv(){try{const o=await post('/api/market-pulse/events',{date:document.getElementById('evd').value,kind:document.getElementById('evk').value,title:document.getElementById('evt').value||null});document.getElementById('evo').textContent=` added: ${o.label} hits ${o.session}`;load()}catch(e){document.getElementById('evo').textContent=' '+e.message}}
j('/api/market-pulse/events').then(o=>{document.getElementById('evk').innerHTML=Object.entries(o.kinds).map(([k,l])=>`<option value="${k}">${esc(l)}</option>`).join('')}).catch(()=>{});
async function load(){const p=await j('/api/market-pulse');evCard(p);document.getElementById('asof').textContent='· '+p.as_of.replace('T',' ');
 document.getElementById('ctx').innerHTML=lab(p.context)+(p.context_score!=null?` <span class="muted">score ${p.context_score}</span>`:'');
 document.getElementById('reasons').innerHTML=p.reasons.length?`<ul class="b">${p.reasons.map(r=>`<li>${esc(r)}</li>`).join('')}</ul>`:'<span class="muted">Not enough stored data yet.</span>';
 const g=p.global_model,t=p.gift_today,gr=p.gap_record||{};let h='';
 if(t)h+=kv([['GIFT Nifty move since yesterday 15:30',`${pct(t.gift_move_pct)} <span class="muted">(${esc(t.gift_ref_source||'')})</span>`],['Expected open',t.expected_gap_pct==null?'—':`${pct(t.expected_gap_pct)} · ${n(t.expected_gap_pts,0)} pts`+(t.band_pct!=null?` <span class="muted">± ${n(t.band_pct,2)}% (typical miss${ovn(t.events)?', widened after '+ovn(t.events)+' overnight':''})</span>`:'')],['Captured',esc(String(t.captured_at).slice(11,16))]]);
 if(g.status==='OK'){h+=`<div style="margin:6px 0">Last night's global moves imply a Nifty move of <b>${pct(g.expected_move_pct)}</b> ${lab(g.cue_label)} <span class="muted">(daily σ ${g.nifty_sigma_pct}%, R² ${g.r2})</span></div>`;
  const mx=Math.max(0.01,...Object.values(g.contributions_pct).map(Math.abs));
  h+=table(['Factor','Last move','Contribution','Sensitivity','Corr 60d'],Object.entries(g.contributions_pct).map(([k,v])=>`<tr><td>${esc(k)}</td><td>${g.inputs[k]?(g.inputs[k].move>0?'+':'')+n(g.inputs[k].move,2)+' '+g.inputs[k].unit:'—'}</td><td>${bar(v,mx)}</td><td class="muted">${g.sensitivity[k]?n(g.sensitivity[k].value,3)+' '+esc(g.sensitivity[k].unit):''}</td><td>${g.corr_60d[k]!=null?n(g.corr_60d[k],2):'—'}</td></tr>`));
  const w=g.walk_forward;h+=`<div class="muted" style="margin-top:6px">Walk-forward, last ${w.sessions} sessions: direction right ${w.direction_hit_rate_pct??'—'}% of days that moved > 0.2%; RMSE ${w.rmse_pct}% vs ${w.rmse_zero_forecast_pct}% for "no change" (${w.beats_zero?'beats':'does not beat'} it).</div>`}
 else h+=`<div class="muted">${esc(g.reason||g.status)}</div>`;
 if((g.stale_factors||[]).length)h+=`<div class="note" style="margin-top:4px">Left out because their data stopped updating over a week ago: ${esc(g.stale_factors.join(', '))}</div>`;
 const gs=p.global_sync||{};
 if(gs.status==='OK'){const w=gs.walk_forward;h+=`<div style="margin-top:6px">Synchronised model (15:30 → 08:45 moves of ${esc(gs.inputs.join(', '))}): ${gs.today?`expected open <b>${pct(gs.today.expected_gap_pct)}</b>`:'no morning snapshot yet'} <span class="muted">· walk-forward over ${w.mornings} mornings: RMSE ${w.rmse_pct}% vs ${w.rmse_zero_pct}% for "no change"${w.rmse_gift_pct!=null?`, ${w.rmse_same_mornings_pct}% vs GIFT's ${w.rmse_gift_pct}%`:''}</span></div>`}
 else if(gs.status)h+=`<div class="muted" style="margin-top:6px">Synchronised model (15:30 → 08:45 moves): ${esc(gs.reason||gs.status)}</div>`;
 if(gr.gift||gr.global_model||gr.synchronised)h+=`<div class="muted">Open-gap record: GIFT ${gr.gift?gr.gift.direction_hit_rate_pct+'% right over '+gr.gift.mornings+' mornings':'—'} · daily model ${gr.global_model?gr.global_model.direction_hit_rate_pct+'% over '+gr.global_model.mornings:'—'} · synchronised ${gr.synchronised?gr.synchronised.direction_hit_rate_pct+'% over '+gr.synchronised.mornings:'—'}</div>`;
 document.getElementById('glob').innerHTML=h;
 const f=p.fii;document.getElementById('fii').innerHTML=f.status!=='OK'?`<span class="muted">${esc(f.reason||f.status)}</span>`:
  `<div style="margin-bottom:6px">Flow pressure ${lab(f.pressure_label)} <span class="muted">score ${f.pressure_score??'—'}</span></div>`+
  kv([['FII / DII net ('+esc(f.as_of)+')',`${cr(f.fii_net_cr)} / ${cr(f.dii_net_cr)}`],['FII 5-day',`${cr(f.fii_5d_cr)} <span class="muted">z ${f.z_fii_5d??'—'}</span>`],['Flow surprise (vs what returns explain)',f.z_surprise==null?'—':`z ${f.z_surprise}`],['FII selling streak',f.selling_streak_days+' days'],['DII absorption (20d)',f.dii_absorption_20d==null?'—':f.dii_absorption_20d+'× FII selling'],['Month to date FII / DII',`${cr(f.fii_mtd_cr)} / ${cr(f.dii_mtd_cr)}`],['Year to date FII / DII',`${cr(f.fii_ytd_cr)} / ${cr(f.dii_ytd_cr)}`],['USD/INR',f.usd_inr?`${f.usd_inr} <span class="muted">(${pct(f.usd_inr_20d_pct)} in 20 days)</span>`:'—']])+
  `<details><summary class="muted">Last 10 days</summary>${table(['Date','FII (₹ cr)','DII (₹ cr)'],f.last_10.map(x=>`<tr><td>${esc(x.date)}</td><td>${n(x.fii_net_cr,0)}</td><td>${n(x.dii_net_cr,0)}</td></tr>`))}</details>`;
 const o=p.positioning,fi=o.fii,nf=o.nifty_futures,pc=o.pcr;let ph='',pr=[];
 if(fi){ph+=`<div style="margin-bottom:6px">${lab(o.read)} <span class="muted">${esc(o.read_note||'')}</span></div>`;pr.push(['FII index futures long %',`${fi.index_futures_long_pct}% <span class="muted">(${fi.change_5d_pp==null?'':(fi.change_5d_pp>0?'+':'')+fi.change_5d_pp+' pp in 5 days, '}percentile ${fi.percentile??'—'} of ${fi.history_days} days)</span>`],['FII net index futures',n(fi.net_index_futures,0)+' contracts'],['FII net index calls / puts',`${n(fi.net_index_calls,0)} / ${n(fi.net_index_puts,0)}`],['Client index futures long %',o.client_index_futures_long_pct==null?'—':o.client_index_futures_long_pct+'%'])}
 else ph+='<div class="muted">No participant OI stored yet: "Fetch NSE positioning" above (NSE serves it to Indian connections).</div>';
 if(nf)pr.push(['Nifty futures ('+esc(nf.as_of)+')',`${esc(nf.buildup.replaceAll('_',' ').toLowerCase())} <span class="muted">price ${pct(nf.price_chg_pct)}, OI ${pct(nf.oi_chg_pct)}</span>`]);
 if(pc)pr.push(['Nifty PCR (OI)',`${pc.value} <span class="muted">z ${pc.z??'—'}, max pain ${n(pc.max_pain,0)}</span>`]);
 if(pr.length)ph+=kv(pr);
 const W=Object.entries(p.oi_walls||{});if(W.length)ph+=table(['Index','Expiry','Spot','Put wall (support)','Call wall (resistance)'],W.map(([k,x])=>`<tr><td>${k}</td><td>${esc(x.expiry)}</td><td>${n(x.spot,0)}</td><td>${x.support?n(x.support.strike,0)+' <span class="muted">('+pct(x.support.distance_pct)+')</span>':'—'}</td><td>${x.resistance?n(x.resistance.strike,0)+' <span class="muted">('+pct(x.resistance.distance_pct)+')</span>':'—'}</td></tr>`));
 document.getElementById('pos').innerHTML=ph;
 const b=p.order_book;let bh='';
 if(b&&b.stocks){bh+=kv([['As of',esc(b.as_of)],['Market-wide pending buy / sell',b.market_buy_sell_ratio==null?'—':b.market_buy_sell_ratio+'×'],['Stocks with buyers / sellers dominant',`${b.buyers_dominant} / ${b.sellers_dominant} of ${b.stocks}`],['Persistent buyers',esc(b.persistent_buyers.join(', ')||'none')],['Persistent sellers',esc(b.persistent_sellers.join(', ')||'none')]]);
  const [bu,se]=await Promise.all([j('/api/orderbook/pressure?side=buy&limit=8'),j('/api/orderbook/pressure?side=sell&limit=8')]);
  const q=v=>v==null?'—':v>=1e5?n(v/1e5,1)+' L':v>=1e3?n(v/1e3,0)+'k':n(v,0);
  const row=x=>`<tr><td><a href="/research?symbol=${encodeURIComponent(x.symbol)}" style="color:var(--accent)">${esc(x.symbol)}</a></td><td>${q(x.total_buy_qty)}</td><td>${q(x.total_sell_qty)}</td><td>${n(x.total_imbalance,2)}</td><td>${pct(x.chg_pct)}</td></tr>`;
  bh+=`<div class="two" style="margin-top:6px"><div class="sx">${table(['Most bid','Buy','Sell','Imb.','Chg'],bu.map(row))}</div><div class="sx">${table(['Most offered','Buy','Sell','Imb.','Chg'],se.map(row))}</div></div><div class="muted">Buy / Sell: total quantity waiting in the book (L = lakh shares). Imb. = (buy − sell) / (buy + sell).</div><div class="note" style="margin-top:4px">${esc(b.caveat)}</div>`}
 else bh='<span class="muted">No order-book polls today (market hours, needs the Dhan Data API).</span>';
 document.getElementById('book').innerHTML=bh;
 mine();depth20().catch(()=>{})}
async function depth20(){const o=await j('/api/orderbook/depth20');let h='';
 if(!o.rows.length)h=`<span class="muted">No depth snapshots today. Feed: ${esc(o.feed.detail||o.feed.mode||'')}</span>`;
 else{h=table(['Symbol','Mid','Spread','DWI','OFI','OFI 20','Best level','All 20','Persistent'],o.rows.slice(0,30).map(x=>`<tr><td><a href="/research?symbol=${encodeURIComponent(x.symbol)}" style="color:var(--accent)">${esc(x.symbol)}</a></td><td>${n(x.mid,2)}</td><td>${n(x.spread_bp,1)} bp</td><td>${n(x.dwi,2)}</td><td>${n(x.ofi_l1_norm,2)}</td><td>${n(x.ofi_ml,2)}</td><td>${n(x.imb_l1,2)}</td><td>${n(x.imb_20,2)}</td><td>${x.persistent?`<span class="${x.persistent==='BUYERS'?'ok':'bad'}">${esc(x.persistent.toLowerCase())}</span>`:''}</td></tr>`));
  const v=await j('/api/orderbook/depth20/validation?horizon=1'),N={dwi:'DWI',imb_l1:'best level',imb_20:'all 20 levels',ofi_l1:'OFI',ofi_ml:'OFI 20 levels'};
  h+=`<div class="muted" style="margin-top:6px">Does it predict the next minute? ${Object.entries(v.measures).map(([k,m])=>`${N[k]}: ${m.z==null?'too few':`z ${m.z}, sign right ${m.sign_right_pct??'—'}%`}${m.n<v.n?` (n ${m.n})`:''}${m.predictive?' <b>(predictive)</b>':''}`).join(' · ')} <span>(${v.n} snapshot pairs a minute apart; ${v.min_n} and |z| ≥ 2 needed)</span></div>`;
  const c=v.contemporaneous;if(c)h+=`<div class="muted" style="margin-top:4px">OFI against the mid's move over the same interval (a sanity check: Cont, Kukanov &amp; Stoikov find a strong relation): ${Object.entries(c.measures).map(([k,m])=>`${N[k]}: ${m.z==null?'too few':`z ${m.z}, R² ${m.r2==null?'—':n(m.r2*100,0)+'%'}`}${m.related?' <b>(related)</b>':''}`).join(' · ')} <span>(${c.pairs} pairs of consecutive snapshots; R² is the median of each stock's line. OFI 20 counts a quote that moves a tick at every level, so it follows the move closely by construction)</span></div>`}
 document.getElementById('d20').innerHTML=h}
async function mine(){const w=await j('/api/brokers/open-orders');const br=w.broker;let h=`<div class="muted">Dhan: ${esc(br.status)}${br.reason?' · '+esc(br.reason):''}${(br.errors||[]).length?' · '+esc(br.errors.join('; ')):''}</div>`;
 h+=table(['Kind','Symbol','Side','Type','Product','Qty','Filled','Price','Trigger','Status','Created'],br.orders.map(o=>`<tr><td>${esc(o.kind)}</td><td>${esc(o.symbol)}</td><td>${esc(o.side)}</td><td>${esc(o.order_type)}</td><td>${esc(o.product)}</td><td>${n(o.quantity,0)}</td><td>${n(o.filled,0)}</td><td>${n(o.price,2)}</td><td>${n(o.trigger_price,2)}</td><td>${esc(o.status)}</td><td class="muted">${esc(o.created||'')}</td></tr>`));
 const a=w.atip;h+=`<div class="muted" style="margin-top:8px">ATIP paper orders resting: ${a.paper_orders.length} · ATIP target / stop rules waiting: ${a.order_rules.length}</div>`;
 if(a.order_rules.length)h+=table(['Symbol','Side','Role','Trigger price','Status'],a.order_rules.slice(0,30).map(r=>`<tr><td>${esc(r.symbol)}</td><td>${esc(r.side)}</td><td>${esc(r.role||'')}</td><td>${n(r.resolved_trigger_price,2)}</td><td>${esc(r.status)}</td></tr>`));
 document.getElementById('mine').innerHTML=h}
let GH=null;
async function gateCard(){try{const [g,h]=await Promise.all([j('/api/market-regime'),j('/api/market-regime/history?days=250')]);
 const el=document.getElementById('gsum');
 if(!g.gate){el.innerHTML=`<span class="muted">${esc(g.reason||'Not computed yet: it runs nightly with the technical signals.')}</span>`;return}
 document.getElementById('gpill').innerHTML=gpill(g.gate);
 document.getElementById('gsince').textContent=`· ${stl(g.status)} since ${g.status_since||'—'}`;
 el.innerHTML=kv([['Why',esc(g.reason||'')],[`Distribution days (last ${g.rules.dd_window} sessions)`,`${g.dd_count}${g.dd_dates.length?' <span class="muted">('+g.dd_dates.map(esc).join(', ')+')</span>':''}`],['Nifty vs 200-DMA',g.sma200==null?'—':`${n(g.nifty_close,0)} vs ${n(g.sma200,0)} <span class="muted">(${pct((g.nifty_close/g.sma200-1)*100)})</span>`],['Last follow-through day',esc(g.ftd_date||'—')],['Rules',`<span class="muted">distribution day: Nifty down ${g.rules.dd_drop_pct}% or more on higher market volume; under pressure at ${g.rules.pressure_dd}, correction at ${g.rules.correction_dd} or ${g.rules.correction_drawdown_pct}% off the high; follow-through: day ${g.rules.ftd_min_day}+ of a rally attempt, up ${g.rules.ftd_pct}% on higher volume</span>`]]);
 document.getElementById('gchanges').innerHTML=table(['Changed','Status','Gate','Why'],g.changes.map(c=>`<tr><td style="white-space:nowrap">${esc(c.date)}</td><td>${esc(stl(c.status))}</td><td>${gpill(c.gate)}</td><td class="muted">${esc(c.reason||'')}</td></tr>`));
 GH=h;drawGate();
 document.getElementById('gtable').innerHTML=table(['Date','Nifty','Change','Volume vs prev.','Distribution day','Count','Status','Gate'],h.slice(-30).reverse().map(r=>`<tr><td>${esc(r.date)}</td><td>${n(r.nifty_close,2)}</td><td>${pct(r.change_pct)}</td><td>${r.volume_ratio==null?'—':n(r.volume_ratio,2)+'×'}</td><td>${r.distribution_day?'yes':''}</td><td>${r.dd_count}</td><td>${esc(stl(r.status))}</td><td>${r.gate?gpill(r.gate):''}</td></tr>`))
}catch(e){document.getElementById('gsum').innerHTML=`<span class="bad">${esc(e.message)}</span>`}}
const GC={OPEN:'#0ca30c',CAUTION:'#fab219',CLOSED:'#d03b3b'};
function drawGate(){const h=GH,el=document.getElementById('gchart');if(!h||h.length<2){el.innerHTML='';return}
 const W=Math.max(300,el.clientWidth),H=250,ST=8,ml=48,mr=70,mt=16,mb=20,ph=H-mt-mb-ST-8,pw=W-ml-mr,N=h.length;
 const vals=h.flatMap(r=>[r.nifty_close,r.sma200]).filter(v=>v!=null);let lo=Math.min(...vals),hi=Math.max(...vals);const pad=(hi-lo)*.06||1;lo-=pad;hi+=pad;
 const X=i=>ml+i*pw/(N-1),Y=v=>mt+(hi-v)/(hi-lo)*ph;
 const raw=(hi-lo)/4,mag=Math.pow(10,Math.floor(Math.log10(raw))),stp=[1,2,2.5,5,10].map(m=>m*mag).find(m=>m>=raw);
 let svg=`<svg width="${W}" height="${H}" role="img" aria-label="Nifty 50 with its 200-day average, distribution days and the market gate">`;
 for(let t=Math.ceil(lo/stp)*stp;t<=hi;t+=stp)svg+=`<line x1="${ml}" x2="${ml+pw}" y1="${Y(t)}" y2="${Y(t)}" stroke="rgba(255,255,255,.07)"/><text x="${ml-6}" y="${Y(t)+3.5}" text-anchor="end" font-size="10.5" fill="#94a3b8">${n(t,0)}</text>`;
 const path=k=>{let d='',pen=false;h.forEach((r,i)=>{const v=r[k];if(v==null){pen=false;return}d+=(pen?'L':'M')+X(i).toFixed(1)+' '+Y(v).toFixed(1);pen=true});return d};
 svg+=`<path d="${path('sma200')}" fill="none" stroke="#d95926" stroke-width="2" stroke-dasharray="6 4"/><path d="${path('nifty_close')}" fill="none" stroke="#3987e5" stroke-width="2" stroke-linejoin="round"/>`;
 h.forEach((r,i)=>{const x=X(i),y=Y(r.nifty_close);
  if(r.distribution_day)svg+=`<polygon points="${x-4.5},${y-15} ${x+4.5},${y-15} ${x},${y-6}" fill="#e2e8f0" stroke="#1e293b" stroke-width="2" paint-order="stroke"/>`;
  if(r.follow_through)svg+=`<polygon points="${x-4.5},${y+15} ${x+4.5},${y+15} ${x},${y+6}" fill="#e2e8f0" stroke="#1e293b" stroke-width="2" paint-order="stroke"/>`});
 const sy=mt+ph+8,half=pw/(N-1)/2;let a=0;
 for(let i=1;i<=N;i++){if(i===N||h[i].gate!==h[a].gate){const g=h[a].gate;if(g){const x0=Math.max(ml,X(a)-half)+(a?1:0),x1=Math.min(ml+pw,X(i-1)+half)-(i<N?1:0);
   svg+=`<rect x="${x0}" y="${sy}" width="${Math.max(1,x1-x0)}" height="${ST}" rx="2" fill="${GC[g]}"/>`}a=i}}
 const L=h[N-1];let yn=Y(L.nifty_close),ys=L.sma200==null?null:Y(L.sma200);if(ys!=null&&Math.abs(yn-ys)<13){const m=(yn+ys)/2;if(yn<ys){yn=m-7;ys=m+7}else{yn=m+7;ys=m-7}}
 svg+=`<text x="${ml+pw+6}" y="${yn+4}" font-size="11" fill="#e2e8f0">Nifty 50</text>`+(ys==null?'':`<text x="${ml+pw+6}" y="${ys+4}" font-size="11" fill="#e2e8f0">200-DMA</text>`);
 [0,.25,.5,.75,1].forEach(f=>{const i=Math.round(f*(N-1));svg+=`<text x="${X(i)}" y="${H-4}" text-anchor="${f===0?'start':f===1?'end':'middle'}" font-size="10.5" fill="#94a3b8">${esc(h[i].date)}</text>`});
 svg+=`<line id="gxh" y1="${mt}" y2="${sy+ST}" stroke="#94a3b8" stroke-width="1" opacity="0"/><circle id="gd1" r="4" fill="#3987e5" stroke="#1e293b" stroke-width="2" opacity="0"/><circle id="gd2" r="4" fill="#d95926" stroke="#1e293b" stroke-width="2" opacity="0"/>`;
 svg+=`<rect id="ghit" x="${ml}" y="${mt}" width="${pw}" height="${sy+ST-mt}" fill="transparent"/></svg>`;
 el.innerHTML=svg;const hit=document.getElementById('ghit'),tip=document.getElementById('tip');
 hit.onmousemove=ev=>{const b=hit.getBoundingClientRect(),i=Math.max(0,Math.min(N-1,Math.round((ev.clientX-b.left)/b.width*(N-1)))),r=h[i],x=X(i);
  const xh=document.getElementById('gxh');xh.setAttribute('x1',x);xh.setAttribute('x2',x);xh.setAttribute('opacity',.6);
  const d1=document.getElementById('gd1');d1.setAttribute('cx',x);d1.setAttribute('cy',Y(r.nifty_close));d1.setAttribute('opacity',1);
  const d2=document.getElementById('gd2');if(r.sma200!=null){d2.setAttribute('cx',x);d2.setAttribute('cy',Y(r.sma200));d2.setAttribute('opacity',1)}else d2.setAttribute('opacity',0);
  tip.style.display='block';tip.style.left=Math.min(ev.clientX+14,window.innerWidth-230)+'px';tip.style.top=(ev.clientY+10)+'px';
  tip.innerHTML=`<b>${esc(r.date)}</b><br><span style="color:#3987e5">━</span> Nifty ${n(r.nifty_close,2)} <span class="muted">(${pct(r.change_pct)})</span><br><span style="color:#d95926">┅</span> 200-DMA ${n(r.sma200,0)}<br>Volume vs previous ${r.volume_ratio==null?'—':n(r.volume_ratio,2)+'×'}${r.distribution_day?' · <b>distribution day</b>':''}${r.follow_through?' · <b>follow-through day</b>':''}<br>${r.dd_count} distribution days · ${esc(stl(r.status))} · gate ${esc(r.gate||'—')}`};
 hit.onmouseleave=()=>{tip.style.display='none';['gxh','gd1','gd2'].forEach(id=>document.getElementById(id).setAttribute('opacity',0))}}
window.addEventListener('resize',()=>{clearTimeout(window._gr);window._gr=setTimeout(drawGate,150)});
async function act(u,word){document.getElementById('ao').textContent=' working…';try{const r=await post(u);document.getElementById('ao').textContent=' '+word+': '+esc(JSON.stringify(r).slice(0,140));load()}catch(e){document.getElementById('ao').textContent=' '+e.message}}
load();gateCard();
</script></body></html>"""


def render_pulse(token: str) -> str:
    return (PULSE.replace("__STYLE__", _STYLE + _EXTRA).replace("__TOKEN__", json.dumps(token))
            .replace("__TITLE__", "ATIP Market pulse").replace("__SUB__", "Market pulse (W39)"))


def render_signals(token: str) -> str:
    return (SIGNALS.replace("__STYLE__", _STYLE + _EXTRA).replace("__TOKEN__", json.dumps(token))
            .replace("__TITLE__", "ATIP Signals").replace("__SUB__", "Technical signals (W39)"))


def render_screener(token: str) -> str:
    return (SCREENER.replace("__STYLE__", _STYLE + _EXTRA).replace("__TOKEN__", json.dumps(token))
            .replace("__TITLE__", "ATIP Screener").replace("__SUB__", "Stock screener (W39)"))


def render_research(token: str) -> str:
    return (RESEARCH.replace("__STYLE__", _STYLE + _EXTRA).replace("__TOKEN__", json.dumps(token))
            .replace("__TITLE__", "ATIP Research").replace("__SUB__", "Equity research (W39)"))


def render_options(token: str) -> str:
    return (OPTIONS.replace("__STYLE__", _STYLE + _EXTRA).replace("__TOKEN__", json.dumps(token))
            .replace("__TITLE__", "ATIP Options Builder").replace("__SUB__", "Options strategy builder (W39)"))
