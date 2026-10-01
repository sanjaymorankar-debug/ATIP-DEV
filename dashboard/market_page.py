"""
The /market page (W27): pre-open view (DB-04), global markets and US yields with
history (DP-12), F&O OI / PCR / max pain (DP-08 partial, SC-06 input), per-symbol
fundamentals (DP-15, SC-13 / SC-02) and ownership (DP-16), the bulk / block deal
study (AD-03) and the live-feed status (DP-01). Reads /api/market/*; computes nothing.
"""

import json

PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ATIP Market</title>
<style>
:root{--bg:#0f172a;--panel:#1e293b;--line:#334155;--text:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8;--good:#10b981;--bad:#f87171;--warn:#f59e0b}
*{box-sizing:border-box;margin:0;padding:0}body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:13px}
.top{background:var(--panel);padding:10px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px}
.top a{color:var(--accent);text-decoration:none;margin-left:12px}.wrap{padding:14px 16px;max-width:1400px}
h2{font-size:13px;color:var(--accent);margin:18px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px}.kl{color:var(--muted);font-size:11px}.kv{font-size:20px;font-weight:700;margin-top:3px;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;background:var(--panel);font-size:11.5px}
th,td{padding:5px 7px;border-bottom:1px solid #33415555;text-align:left;font-variant-numeric:tabular-nums}th{color:var(--muted);background:var(--bg);white-space:nowrap}
.muted{color:var(--muted)}.note{color:var(--warn)}.scroll{max-height:380px;overflow:auto}.up{color:var(--good)}.dn{color:var(--bad)}
input,select,button{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px}
button{cursor:pointer}svg{display:block}
@media(max-width:600px){.wrap{padding:12px}}
</style></head><body>
<div class="top"><div><b style="color:var(--accent)">📈 ATIP</b> <span class="muted">Market data (W27)</span></div>
<div><a href="/quant">Quant</a><a href="/trading">Trading</a><a href="/data-platform">Data platform</a><a href="/">← Dashboard</a></div></div>
<div class="wrap">
<h2>Pre-open · GIFT Nifty</h2><div id="pre" class="grid"></div><div id="preNote" class="muted" style="margin-top:6px"></div>
<div id="giftChart" style="margin-top:8px"></div>
<h2>Global markets &amp; US yields</h2><div id="glob" class="grid"></div>
<div style="margin:8px 0"><select id="ser" onchange="hist()"></select> <select id="days" onchange="hist()"><option>90</option><option selected>365</option><option>1825</option></select>
<button onclick="refresh('global_history',{period:'5y'})">Backfill 5y history</button></div><div id="hist"></div>
<h2>Derivatives · OI, PCR, max pain <span id="fodate" class="muted"></span></h2>
<div style="margin-bottom:6px"><select id="kind" onchange="fo()"><option value="">all</option><option>INDEX</option><option>STOCK</option></select>
<button onclick="refresh('fo',{sessions:5})">Fetch last 5 sessions</button></div><div id="fo" class="scroll"></div>
<h2>Fundamentals &amp; ownership</h2>
<div style="margin-bottom:6px"><input id="sym" placeholder="Symbol e.g. INFY" style="width:160px" onkeydown="if(event.key==='Enter')look()"> <button onclick="look()">Look up</button>
<button onclick="refresh('fundamentals',{symbols:[cur()]})">Refresh from NSE</button> <button onclick="refresh('institutional',{symbols:[cur()]})">Refresh ownership</button></div>
<div id="fund" class="scroll"></div><div id="own" style="margin-top:8px"></div>
<h2>Bulk / block deal signal (AD-03)</h2><div id="deal"></div><button style="margin-top:6px" onclick="refresh('deal_signal',{})">Re-run study</button>
<h2>Live feeds</h2><div id="feed"></div>
<div id="msg" class="note" style="margin-top:12px"></div>
</div>
<script>
const TOKEN=__TOKEN__;
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const num=(v,d=2)=>v==null||isNaN(v)?'—':Number(v).toLocaleString('en-IN',{minimumFractionDigits:d,maximumFractionDigits:d});
const pct=(v,d=2)=>v==null?'—':`<span class="${v>=0?'up':'dn'}">${v>=0?'+':''}${Number(v).toFixed(d)}%</span>`;
async function j(u,o){const r=await fetch(u,o);const t=await r.json();if(!r.ok)throw new Error(t.error||r.status);return t}
const table=(h,rows)=>`<table><thead><tr>${h.map(x=>`<th>${x}</th>`).join('')}</tr></thead><tbody>${rows.join('')||`<tr><td colspan=${h.length} class="muted">none</td></tr>`}</tbody></table>`;
const kpi=(l,v,s='')=>`<div class="kpi"><div class="kl">${l}</div><div class="kv">${v}</div><div class="muted" style="font-size:11px">${s}</div></div>`;
function spark(pts,w=620,h=110){if(!pts||pts.length<2)return '<span class="muted">not enough history</span>';
  const v=pts.map(p=>p.close),mn=Math.min(...v),mx=Math.max(...v),r=mx-mn||1;
  const d=pts.map((p,i)=>`${i?'L':'M'}${(i/(pts.length-1)*(w-10)+5).toFixed(1)},${(h-5-(p.close-mn)/r*(h-10)).toFixed(1)}`).join('');
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" style="max-width:${w}px;background:var(--panel);border-radius:6px" role="img" aria-label="price history"><path d="${d}" fill="none" stroke="#38bdf8" stroke-width="1.6"/><text x="6" y="12" fill="#94a3b8" font-size="10">${num(mx)}</text><text x="6" y="${h-4}" fill="#94a3b8" font-size="10">${num(mn)}</text><text x="${w-6}" y="12" fill="#94a3b8" font-size="10" text-anchor="end">${esc(pts[pts.length-1].date)}</text></svg>`}
const cur=()=>document.getElementById('sym').value.trim().toUpperCase();
async function refresh(job,body){try{const r=await j('/api/market/refresh/'+job,{method:'POST',headers:{'Content-Type':'application/json','X-ATIP-Token':TOKEN},body:JSON.stringify(body)});document.getElementById('msg').textContent=`${job}: started in the background — reload in a minute.`}catch(e){document.getElementById('msg').textContent=job+': '+e.message}}
async function pre(){const p=await j('/api/market/preopen');const g=p.gift||{},n=p.nifty_last_close||{};
  const d=Object.fromEntries((p.derivatives||[]).map(x=>[x.symbol,x]));
  document.getElementById('pre').innerHTML=kpi('GIFT Nifty',num(g.gift_nifty,1),`${pct(g.gift_nifty_chg)} · ${esc(g.date||'')} ${esc(g.time||'')}`)
   +kpi('Nifty last close',num(n.close,1),esc(n.date||''))+kpi('Implied gap',p.implied_gap_pct==null?'—':pct(p.implied_gap_pct),'GIFT vs Nifty close')
   +kpi('NIFTY PCR (OI)',num(d.NIFTY?.pcr_oi,3),`max pain ${num(d.NIFTY?.max_pain,0)} · exp ${esc(d.NIFTY?.near_expiry||'')}`)
   +kpi('BANKNIFTY PCR (OI)',num(d.BANKNIFTY?.pcr_oi,3),`max pain ${num(d.BANKNIFTY?.max_pain,0)}`)
   +kpi('FII / DII net (Cr)',`${num(p.fii_dii?.fii_net_cr,0)} / ${num(p.fii_dii?.dii_net_cr,0)}`,esc(p.fii_dii?.date||''));
  document.getElementById('preNote').textContent=p.gap_note||'';
  document.getElementById('giftChart').innerHTML=spark(p.gift_series);
  const G=p.global||{};const cols=[['sp500','S&P 500'],['nasdaq','Nasdaq'],['dow','Dow'],['nikkei','Nikkei'],['hangseng','Hang Seng'],['ftse100','FTSE'],['dax','DAX'],['crude_brent','Brent'],['gold','Gold'],['usd_inr','USD/INR'],['usd_index','DXY'],['us_3m','US 3M yield'],['us_5y','US 5Y yield'],['us_10y','US 10Y yield'],['us_30y','US 30Y yield']];
  document.getElementById('glob').innerHTML=cols.map(([k,l])=>kpi(l,num(G[k],2),pct(G[k+'_chg']))).join('')+kpi('Global score',num(G.global_score,1),esc(G.global_sentiment||'')+' · '+esc(G.date||''));
  document.getElementById('ser').innerHTML=cols.map(([k,l])=>`<option value="${k}"${k==='us_10y'?' selected':''}>${l}</option>`).join('');hist()}
async function hist(){const s=document.getElementById('ser').value,d=document.getElementById('days').value;
  try{const h=await j(`/api/market/global-history?series=${s}&days=${d}`);document.getElementById('hist').innerHTML=spark(h.points)}catch(e){document.getElementById('hist').textContent=e.message}}
async function fo(){const k=document.getElementById('kind').value;const r=await j('/api/market/derivatives?limit=60'+(k?'&kind='+k:''));
  document.getElementById('fodate').textContent=r.date?`· ${r.date}`:'';
  document.getElementById('fo').innerHTML=r.note?`<span class="muted">${esc(r.note)}</span>`:table(['Underlying','Kind','Spot','Fut','Fut OI chg','Call OI','Put OI','PCR OI','PCR vol','Max pain','Near expiry'],
   r.rows.map(x=>`<tr><td><b>${esc(x.symbol)}</b></td><td>${esc(x.kind)}</td><td>${num(x.underlying_price)}</td><td>${num(x.fut_close)}</td><td>${num(x.fut_oi_chg,0)}</td><td>${num(x.call_oi,0)}</td><td>${num(x.put_oi,0)}</td><td>${num(x.pcr_oi,3)}</td><td>${num(x.pcr_volume,3)}</td><td>${num(x.max_pain,0)}</td><td>${esc(x.near_expiry)}</td></tr>`))}
async function look(){const s=cur();if(!s)return;try{
  const f=await j('/api/market/fundamentals/'+encodeURIComponent(s));
  document.getElementById('fund').innerHTML=`<div class="muted" style="margin-bottom:4px">filings stored: ${esc(JSON.stringify(f.filings))} · values in ₹ crore; ratios as fractions; FS / SPI valued at the latest close</div>`+table(['Quarter','Basis','Revenue','Profit','Rev YoY','EPS YoY','Profit QoQ','Op margin','ROE','ROCE','D/E','EPS TTM','P/E','P/B','FS','SPI','Filed'],
   f.quarters.map(q=>`<tr><td><b>${esc(q.quarter)}</b></td><td class="muted">${esc(q.nature||q.source)}</td><td>${num(q.revenue_cr,0)}</td><td>${num(q.profit_cr,0)}</td><td>${pct(q.revenue_growth_yoy,1)}</td><td>${pct(q.eps_growth_yoy,1)}</td><td>${pct(q.qoq_profit_chg,1)}</td><td>${num(q.operating_margin,3)}</td><td>${num(q.roe,3)}</td><td>${num(q.roce,3)}</td><td>${num(q.debt_equity,2)}</td><td>${num(q.eps_ttm)}</td><td>${num(q.pe_ratio,1)}</td><td>${num(q.pb_ratio,1)}</td><td>${num(q.fundamental_score,1)}</td><td>${num(q.spi_score,1)}</td><td class="muted">${esc(String(q.available_from||'').slice(0,10))}</td></tr>`));
  const o=await j('/api/market/ownership/'+encodeURIComponent(s));const F=o.features||{};
  document.getElementById('own').innerHTML=`<div class="grid">${kpi('Promoter %',num(F.promoter_pct),F.promoter_chg==null?'':'QoQ '+num(F.promoter_chg)+' pp')}${kpi('Mutual funds %',num(F.mf_pct),F.mf_chg==null?'':'QoQ '+num(F.mf_chg)+' pp')}${kpi('FPI %',num(F.fpi_pct),F.fpi_chg==null?'':'QoQ '+num(F.fpi_chg)+' pp')}${kpi('Pledged % of promoter',num(F.pledged_pct))}${kpi('Insider net 90d (Cr)',num(F.insider_net_cr_90d,2),`${F.insider_buys_90d??0} buys · ${F.insider_sells_90d??0} sells`)}</div>`
   +'<div style="margin-top:8px">'+table(['As of','Promoter','MF','FPI','Insurance','DII','Retail','Filed'],o.shareholding.map(r=>`<tr><td>${esc(r.as_of)}</td><td>${num(r.promoter_pct)}</td><td>${num(r.mf_pct)}</td><td>${num(r.fpi_pct)}</td><td>${num(r.insurance_pct)}</td><td>${num(r.dii_pct)}</td><td>${num(r.retail_pct)}</td><td class="muted">${esc(String(r.submitted_at||'').slice(0,10))}</td></tr>`))+'</div>'
   +'<div class="scroll" style="margin-top:8px">'+table(['Disclosed','Person','Category','Type','Qty','Value (₹)','Mode'],o.insider.slice(0,20).map(r=>`<tr><td>${esc(String(r.disclosed_at||'').slice(0,10))}</td><td>${esc(r.person)}</td><td class="muted">${esc(r.person_category)}</td><td class="${r.txn_type==='BUY'?'up':r.txn_type==='SELL'?'dn':''}">${esc(r.txn_type)}</td><td>${num(r.qty,0)}</td><td>${num(r.value_rs,0)}</td><td class="muted">${esc(r.mode)}</td></tr>`))+'</div>';
 }catch(e){document.getElementById('fund').textContent=e.message;document.getElementById('own').textContent=''}}
async function deal(){const d=await j('/api/market/deal-signal');if(d.note){document.getElementById('deal').innerHTML=`<span class="muted">${esc(d.note)}</span>`;return}
  const rows=Object.entries(d.horizons||{}).map(([h,v])=>`<tr><td>${h}d</td><td>${v.BUY.n}</td><td>${pct(v.BUY.mean_abnormal_pct)}</td><td>${num(v.BUY.t_stat)}</td><td>${v.SELL.n}</td><td>${pct(v.SELL.mean_abnormal_pct)}</td><td>${num(v.SELL.t_stat)}</td><td>${num(v.pooled_ic,3)}</td></tr>`);
  document.getElementById('deal').innerHTML=`<div style="margin-bottom:6px">Verdict <b>${esc(d.verdict)}</b> · ${d.events} events · ${esc(d.period)} · computed ${esc(String(d.computed_at||'').slice(0,16))}</div><div class="muted" style="margin-bottom:6px">Abnormal = stock − NIFTY50 forward return after the deal session. Small, recent sample: descriptive only.</div>`+table(['Horizon','BUY n','BUY mean','BUY t','SELL n','SELL mean','SELL t','Pooled IC'],rows)}
async function feed(){const f=await j('/api/market/feed-status');const s=f.stocks||{};
  document.getElementById('feed').innerHTML=`Stocks: <b>${s.running?esc(s.mode):(s.enabled?'enabled, not started':'off')}</b> ${s.running?`· ${s.subscribed} symbols · ${s.ticks} ticks · last tick ${esc(String(s.last_tick_at||'—').slice(11,19))} · ${esc(s.detail)}`:'<span class="muted">(config.json live_feed.stocks_enabled)</span>'}<br>Indexes: ${f.indexes?.running?`running · ${f.indexes.indexes_ticking} ticking`:'not running in this process'}`}
pre().catch(e=>document.getElementById('pre').textContent=e.message);fo().catch(e=>document.getElementById('fo').textContent=e.message);
deal().catch(()=>{});feed().catch(()=>{});
</script></body></html>"""


def render(token: str) -> str:
    return PAGE.replace("__TOKEN__", json.dumps(token))
