"""
The /insights page (W28): AI news summary (DB-06), classified news and LLM spend
(NS-02/03/05), corporate announcements (NS-06), crash-risk and top-25 SPI lists with the
MSI gauge (DB-11), strategy performance (DB-16), model / factor monitoring and AI-strategy
readiness (DB-18, SE-05), and intraday scan hits (SG-08). Read from /api/insights/*;
nothing is computed in the browser beyond drawing.

Charts are single-series lines (one hue, #3987e5 -- validated for this dark surface),
2px, with a crosshair tooltip; every charted value is also in the table beside it.
"""

import json

PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ATIP Insights</title>
<style>
:root{--bg:#0f172a;--panel:#1e293b;--line:#334155;--text:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8;--series:#3987e5;
--good:#059669;--bad:#dc2626;--warn:#f59e0b;--grid:#334155}
*{box-sizing:border-box;margin:0;padding:0}body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:13px}
.top{background:var(--panel);padding:10px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px}
.top a{color:var(--accent);text-decoration:none;margin-left:12px}.wrap{padding:14px 16px;max-width:1400px}
.tabs{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:10px}.tab{padding:6px 12px;border-radius:6px;background:var(--panel);cursor:pointer;color:var(--muted)}
.tab.on{background:#2563eb;color:#fff}.pane{display:none}.pane.on{display:block}
h2{font-size:13px;color:var(--accent);margin:16px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
table{width:100%;border-collapse:collapse;background:var(--panel);font-size:11.5px}
th,td{padding:5px 7px;border-bottom:1px solid #1e293b55;text-align:left;vertical-align:top}th{color:var(--muted);background:var(--bg);white-space:nowrap}
.muted{color:var(--muted)}.note{color:var(--warn)}.scroll{max-height:420px;overflow:auto}
.pill{display:inline-block;padding:1px 7px;border-radius:9px;font-size:10.5px;background:#334155}
.pos{color:#34d399}.neg{color:#f87171}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin-bottom:10px}
.big{font-size:28px;font-weight:600}.row{display:flex;gap:12px;flex-wrap:wrap}.row>.card{flex:1;min-width:260px}
button,select,input{background:#2563eb;color:#fff;border:none;border-radius:6px;padding:4px 9px;cursor:pointer;font-size:11.5px;margin:1px}
select,input{background:var(--panel);color:var(--text);border:1px solid var(--line)}
.chart{position:relative;width:100%;height:180px}.chart svg{width:100%;height:100%;display:block}
.tip{position:absolute;pointer-events:none;background:#0b1220;border:1px solid var(--line);border-radius:6px;padding:4px 7px;font-size:11px;display:none;white-space:nowrap}
.tip b{display:block;font-size:12px}
ul.b{margin:6px 0 0 18px}ul.b li{margin:3px 0}
</style></head><body>
<div class="top"><div><b style="color:var(--accent)">📊 ATIP</b> <span class="muted">Insights (W28)</span></div>
<div><a href="/strategies">Strategies</a><a href="/ml">ML</a><a href="/trading">Trading</a><a href="/">← Dashboard</a></div></div>
<div class="wrap">
<div class="tabs" id="tabs"></div>

<div class="pane" id="p-news">
  <div class="card" id="digest"></div>
  <div class="row"><div class="card"><b>LLM usage</b> <span class="muted">(NS-02 cost cap)</span><div id="usage"></div></div>
  <div class="card"><b>Source weights</b> <span class="muted">(NS-05, measured)</span><div id="sources" class="scroll" style="max-height:220px"></div></div></div>
  <h2>Classified news <span class="muted">— event type, sentiment (model vs lexicon), confidence, novelty, source weight</span></h2>
  <div style="margin-bottom:6px"><input id="nsym" placeholder="symbol" size="10"> <select id="nev"><option value="">all events</option></select> <button onclick="loadNews()">Filter</button></div>
  <div id="news" class="scroll"></div>
  <h2>Weighted news score by stock <span class="muted">— 50 neutral; decayed, novelty- and source-weighted (feeds the NS input of the scores)</span></h2>
  <div id="nscores" class="scroll" style="max-height:280px"></div>
</div>

<div class="pane" id="p-ann">
  <h2>NSE corporate announcements <span class="muted">— classified; transcripts / results of tracked stocks read by Claude when enabled</span></h2>
  <div style="margin-bottom:6px"><input id="asym" placeholder="symbol" size="10"> <select id="aev"><option value="">all types</option></select> <button onclick="loadAnn()">Filter</button></div>
  <div id="ann" class="scroll"></div>
</div>

<div class="pane" id="p-lists">
  <div class="row"><div class="card"><b>Market Sentiment Index (MSI)</b> <span class="muted" id="msid"></span><div class="big" id="msiv">—</div><div id="msic" class="muted"></div></div>
  <div class="card" style="flex:2"><b>MSI, last 120 days</b><div class="chart" id="msichart"></div></div></div>
  <h2>Crash risk — top 25 by CRI <span class="muted">— with the three largest CRI contributions and the change since the previous session</span></h2>
  <div id="crash" class="scroll"></div>
  <h2>Top 25 SPI <span class="muted">— Stability Profit Index</span></h2><div id="spi" class="scroll"></div>
</div>

<div class="pane" id="p-strat">
  <div style="margin-bottom:6px">Window <select id="win" onchange="loadStrat()"><option>20</option><option selected>60</option><option>250</option></select> days
  <button onclick="refreshStrat()">Recompute now</button> <span class="muted">Decision outcomes: forward return of each BUY/SELL decision vs Nifty 50. Paper: FIFO-matched paper fills. Descriptive only — read n beside every rate.</span></div>
  <div id="strat" class="scroll"></div>
  <div class="card" style="margin-top:10px"><b id="eqt">Paper realised P&amp;L — pick a strategy</b><div class="chart" id="eqchart"></div></div>
</div>

<div class="pane" id="p-models">
  <div class="card" id="ai"></div>
  <h2>Model monitoring <span class="muted">— input drift (columns with PSI &gt; 0.25) and daily prediction volume per model version</span></h2>
  <div id="models"></div>
  <h2>Factor decay <span class="muted">— recent vs earlier mean IC (ml/decay.py)</span></h2><div id="factors" class="scroll"></div>
</div>

<div class="pane" id="p-scans">
  <div style="margin-bottom:6px"><select id="scan" onchange="loadScans()"><option value="">all scans</option>
  <option>ORB_UP</option><option>ORB_DOWN</option><option>VWAP_RECLAIM</option><option>VWAP_LOSS</option><option>VOLUME_SURGE</option>
  <option>POWER_HOUR_UP</option><option>POWER_HOUR_DOWN</option><option>GAP_HOLD_UP</option><option>GAP_HOLD_DOWN</option></select>
  <button onclick="runScan()">Scan today's bars now</button> <span class="muted">From stored 15-min bars. Observations, not signals: nothing here creates a decision or an order.</span></div>
  <div id="scans" class="scroll"></div>
</div>
</div>
<script>
const TOKEN=__TOKEN__;
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const num=(v,d=2)=>v==null?'—':Number(v).toFixed(d);
const sg=v=>v==null?'—':`<span class="${v>0?'pos':v<0?'neg':''}">${v>0?'+':''}${Number(v).toFixed(2)}</span>`;
const pct=v=>v==null?'—':(v*100).toFixed(1)+'%';
async function j(u,o){const r=await fetch(u,o);const t=await r.json();if(!r.ok)throw new Error(t.error||t.detail||r.status);return t}
const post=(u,b)=>j(u,{method:'POST',headers:{'Content-Type':'application/json','X-ATIP-Token':TOKEN},body:JSON.stringify(b||{})});
const table=(h,rows)=>`<table><thead><tr>${h.map(x=>`<th>${x}</th>`).join('')}</tr></thead><tbody>${rows.join('')||`<tr><td colspan=${h.length} class="muted">none</td></tr>`}</tbody></table>`;

/* single-series line chart: 2px line, recessive grid, crosshair + tooltip (textContent only) */
function lineChart(el,pts,{fmt=v=>num(v),label='value'}={}){
  el.innerHTML='';const tip=document.createElement('div');tip.className='tip';
  if(!pts.length){el.textContent='no data yet';el.className+=' muted';return}
  const W=el.clientWidth||600,H=el.clientHeight||180,L=44,R=10,T=10,B=22;
  const ys=pts.map(p=>p.y),lo=Math.min(...ys),hi=Math.max(...ys),pad=(hi-lo)*0.08||1,y0=lo-pad,y1=hi+pad;
  const X=i=>L+(pts.length===1?0:(W-L-R)*i/(pts.length-1)),Y=v=>T+(H-T-B)*(1-(v-y0)/(y1-y0));
  const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox',`0 0 ${W} ${H}`);
  const add=(tag,a)=>{const e=document.createElementNS(ns,tag);for(const k in a)e.setAttribute(k,a[k]);svg.appendChild(e);return e};
  for(let k=0;k<=3;k++){const v=y0+(y1-y0)*k/3,y=Y(v);add('line',{x1:L,x2:W-R,y1:y,y2:y,stroke:'var(--grid)','stroke-width':1});
    const t=add('text',{x:L-6,y:y+3,'text-anchor':'end',fill:'var(--muted)','font-size':10});t.textContent=fmt(v)}
  [0,pts.length-1].forEach(i=>{const t=add('text',{x:X(i),y:H-6,'text-anchor':i?'end':'start',fill:'var(--muted)','font-size':10});t.textContent=pts[i].x});
  add('path',{d:pts.map((p,i)=>`${i?'L':'M'}${X(i).toFixed(1)},${Y(p.y).toFixed(1)}`).join(''),fill:'none',stroke:'var(--series)','stroke-width':2,'stroke-linejoin':'round','stroke-linecap':'round'});
  const last=pts.length-1;add('circle',{cx:X(last),cy:Y(pts[last].y),r:4,fill:'var(--series)',stroke:'var(--panel)','stroke-width':2});
  const hair=add('line',{y1:T,y2:H-B,stroke:'var(--muted)','stroke-width':1,visibility:'hidden'});
  const dot=add('circle',{r:4,fill:'var(--series)',stroke:'var(--panel)','stroke-width':2,visibility:'hidden'});
  const hit=add('rect',{x:L,y:T,width:W-L-R,height:H-T-B,fill:'transparent'});
  hit.addEventListener('pointermove',ev=>{const r=svg.getBoundingClientRect(),px=(ev.clientX-r.left)*W/r.width;
    const i=Math.max(0,Math.min(last,Math.round((px-L)/((W-L-R)/Math.max(1,last)))));
    hair.setAttribute('x1',X(i));hair.setAttribute('x2',X(i));hair.setAttribute('visibility','visible');
    dot.setAttribute('cx',X(i));dot.setAttribute('cy',Y(pts[i].y));dot.setAttribute('visibility','visible');
    tip.textContent='';const b=document.createElement('b');b.textContent=fmt(pts[i].y);tip.appendChild(b);
    tip.appendChild(document.createTextNode(`${label} · ${pts[i].x}`));tip.style.display='block';
    const tx=X(i)*r.width/W;tip.style.left=Math.min(tx+10,r.width-140)+'px';tip.style.top='4px'});
  hit.addEventListener('pointerleave',()=>{hair.setAttribute('visibility','hidden');dot.setAttribute('visibility','hidden');tip.style.display='none'});
  el.appendChild(svg);el.appendChild(tip)}

const TABS=[['news','News & AI summary'],['ann','Announcements'],['lists','Crash risk / SPI / MSI'],['strat','Strategy performance'],['models','Models & AI strategies'],['scans','Intraday scans']];
const loaded={};
function show(id){document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.dataset.id===id));
  document.querySelectorAll('.pane').forEach(p=>p.classList.toggle('on',p.id==='p-'+id));
  if(!loaded[id]){loaded[id]=1;({news:loadNewsTab,ann:loadAnn,lists:loadLists,strat:loadStrat,models:loadModels,scans:loadScans})[id]()}
  try{localStorage.setItem('atip_insights_tab',id)}catch(e){}}
document.getElementById('tabs').innerHTML=TABS.map(([id,l])=>`<div class="tab" data-id="${id}" onclick="show('${id}')">${l}</div>`).join('');

const EVENTS=['EARNINGS_BEAT','EARNINGS_MISS','EARNINGS_INLINE','GUIDANCE_UP','GUIDANCE_DOWN','ORDER_WIN','CAPEX_EXPANSION','M_AND_A','FUNDRAISE','BUYBACK','DIVIDEND','MGMT_CHANGE','PROMOTER_ACTIVITY','BLOCK_DEAL','RATING_UPGRADE','RATING_DOWNGRADE','BROKER_UPGRADE','BROKER_DOWNGRADE','REGULATORY_ACTION','LEGAL','RBI_POLICY','GOVT_POLICY','GLOBAL_MACRO','GEOPOLITICS','COMMODITY','SECTOR_TREND','IPO','MARKET_WRAP','OTHER'];
document.getElementById('nev').innerHTML+=EVENTS.map(e=>`<option>${e}</option>`).join('');
const AEV=['EARNINGS_CALL','RESULTS','BOARD_OUTCOME','INVESTOR_MEET','BOARD_MEETING','DIVIDEND','BUYBACK','BONUS_SPLIT','M_AND_A','CREDIT_RATING','ORDER_WIN','MGMT_CHANGE','PROMOTER_ACTIVITY','LEGAL','REGULATORY_ACTION','FUNDRAISE','AGM','FILING','OTHER'];
document.getElementById('aev').innerHTML+=AEV.map(e=>`<option>${e}</option>`).join('');

async function loadDigest(){const d=await j('/api/insights/news-digest');const el=document.getElementById('digest');
  const btns=`<div style="margin-top:8px">${['premarket','midday','close'].map(s=>`<button onclick="buildDigest('${s}')">Summarise ${s}</button>`).join('')}</div>`;
  if(!d.digest_id){el.innerHTML=`<b>AI news summary</b><p class="muted">No summary yet — the scheduler writes one pre-market, at midday and after the close.</p>${btns}`;return}
  el.innerHTML=`<div class="muted">${esc(d.session)} · ${esc(d.generated_at)} · ${d.method==='claude'?'written by '+esc(d.model):'extractive (no LLM)'} · ${d.articles_used} stories · tone <b>${esc(d.market_tone)}</b>${d.error?` · <span class="note">${esc(d.error)}</span>`:''}</div>
  <div style="font-size:16px;font-weight:600;margin:6px 0">${esc(d.headline)}</div><div>${esc(d.summary)}</div>
  <ul class="b">${(d.bullets||[]).map(b=>`<li>${esc(b)}</li>`).join('')}</ul>
  <div class="muted" style="margin-top:6px">Themes: ${(d.themes||[]).map(esc).join(', ')||'—'} · Stocks: ${(d.symbols||[]).map(esc).join(', ')||'—'}</div>
  <div class="muted">A restatement of the news, not advice; not an input to any score.</div>${btns}`}
async function buildDigest(s){try{await post('/api/insights/news-digest',{session:s});loadDigest()}catch(e){alert(e.message)}}
async function loadNews(){const s=document.getElementById('nsym').value.trim(),e=document.getElementById('nev').value;
  const N=await j(`/api/insights/news?hours=72&limit=200${s?'&symbol='+encodeURIComponent(s):''}${e?'&event_type='+e:''}`);
  document.getElementById('news').innerHTML=table(['Time','Headline','Source','Event','Sentiment','Lexicon','Conf','Imp','Novelty','Src w','By','Stocks'],
   N.map(a=>`<tr><td class="muted">${esc(String(a.fetched_at).slice(5,16))}</td><td>${a.url?`<a style="color:var(--text)" href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.headline)}</a>`:esc(a.headline)}${a.dup_of?' <span class="pill">dup</span>':''}</td><td class="muted">${esc(a.source)}</td><td>${esc(a.event_type||a.category)}</td><td>${sg(a.sentiment)}</td><td>${sg(a.sentiment_lex)}</td><td>${num(a.confidence)}</td><td>${esc(a.importance)}</td><td>${num(a.novelty)}</td><td>${num(a.source_weight)}</td><td class="muted">${esc(a.classifier)}</td><td>${(a.symbols||[]).map(esc).join(' ')}</td></tr>`))}
async function loadNewsTab(){loadDigest();loadNews();
  const U=await j('/api/insights/news/usage');const st=U.settings;
  document.getElementById('usage').innerHTML=`<div class="muted" style="margin:4px 0">LLM ${st.enabled?'<b>ON</b>':'<b>OFF</b> (lexicon only)'} · ${esc(st.model)} · effort ${esc(st.effort)} · budget $${num(st.daily_budget_usd)}/day · batch ${st.batch_size}</div>`+
   table(['Day','Purpose','Calls','Items','In tok','Out tok','Cache read','Est. $','Refusals','Errors'],U.rows.map(r=>`<tr><td>${r.day}</td><td>${esc(r.purpose)}</td><td>${r.calls}</td><td>${r.items}</td><td>${r.input_tokens}</td><td>${r.output_tokens}</td><td>${r.cache_read_tokens}</td><td>${num(r.cost_usd,4)}</td><td>${r.refusals}</td><td>${r.errors}</td></tr>`));
  const S=await j('/api/insights/news/sources');
  document.getElementById('sources').innerHTML=table(['Source','Articles','Dup share','Stock share','Reaction hit','n','Configured','Weight'],S.quality.map(r=>`<tr><td>${esc(r.source)}</td><td>${r.articles}</td><td>${pct(r.duplicate_share)}</td><td>${pct(r.symbol_share)}</td><td>${pct(r.reaction_hit_rate)}</td><td>${r.reaction_n}</td><td>${num(r.configured_weight)}</td><td><b>${num(r.weight)}</b></td></tr>`));
  const NS=await j('/api/insights/news/scores');
  document.getElementById('nscores').innerHTML=`<div class="muted">${esc(NS.date||'no scores yet')}</div>`+table(['Stock','Score','Articles','Effective weight','Weighted sentiment'],NS.rows.map(r=>`<tr><td>${esc(r.symbol)}</td><td><b>${num(r.score,1)}</b></td><td>${r.n_articles}</td><td>${num(r.effective_weight,3)}</td><td>${sg(r.mean_sentiment)}</td></tr>`))}

async function loadAnn(){const s=document.getElementById('asym').value.trim(),e=document.getElementById('aev').value;
  const A=await j(`/api/insights/announcements?days=14${s?'&symbol='+encodeURIComponent(s):''}${e?'&event_type='+e:''}`);
  document.getElementById('ann').innerHTML=table(['Time','Stock','Type','Subject','Tone','Conf','Imp','Summary / analysis','By'],A.map(a=>{
   const n=a.nlp?`<div class="muted" style="margin-top:3px">Guidance <b>${esc(a.nlp.guidance)}</b>${(a.nlp.key_points||[]).length?'<br>• '+a.nlp.key_points.map(esc).join('<br>• '):''}${(a.nlp.risks||[]).length?'<br><span class="note">Risks:</span> '+a.nlp.risks.map(esc).join('; '):''}</div>`:'';
   return `<tr><td class="muted">${esc(String(a.broadcast_at||'').slice(5,16))}</td><td><b>${esc(a.symbol)}</b></td><td>${esc(a.event_type)}</td><td>${a.attachment_url?`<a style="color:var(--text)" href="${esc(a.attachment_url)}" target="_blank" rel="noopener">${esc(a.subject)}</a>`:esc(a.subject)}</td><td>${sg(a.tone)}</td><td>${num(a.confidence)}</td><td>${esc(a.importance)}</td><td>${esc(a.summary)}${n}</td><td class="muted">${esc(a.classifier)}</td></tr>`}))}

async function loadLists(){const L=await j('/api/insights/lists');
  document.getElementById('msid').textContent=L.date?`· ${L.date}`:'';document.getElementById('msiv').textContent=num(L.msi,1);
  document.getElementById('msic').innerHTML=(L.msi_components||[]).map(c=>`${esc(c.component)} ${num(c.value,0)}`).join(' · ')||'components not recorded for this session';
  lineChart(document.getElementById('msichart'),(L.msi_history||[]).map(r=>({x:r.date,y:r.msi})),{fmt:v=>num(v,1),label:'MSI'});
  document.getElementById('crash').innerHTML=table(['Stock','CRI','Band','Δ vs prev','Top drivers','ATIP','Signal','Beta','Close'],L.crash_risk.map(r=>`<tr><td><b>${esc(r.symbol)}</b></td><td><b>${num(r.cri,1)}</b></td><td><span class="pill">${r.band}</span></td><td>${sg(r.cri_change)}</td><td class="muted">${r.drivers.map(d=>`${esc(d.component)} ${num(d.contribution,1)}`).join(' · ')||'—'}</td><td>${num(r.atip_score,1)}</td><td>${esc(r.signal)}</td><td>${num(r.beta_1y)}</td><td>${num(r.close)}</td></tr>`));
  document.getElementById('spi').innerHTML=(L.spi_note?`<p class="note">${esc(L.spi_note)}</p>`:'')+table(['Stock','SPI','FS','ATIP','Signal','Close'],L.top_spi.map(r=>`<tr><td><b>${esc(r.symbol)}</b></td><td><b>${num(r.spi,1)}</b></td><td>${num(r.fund_score,1)}</td><td>${num(r.atip_score,1)}</td><td>${esc(r.signal)}</td><td>${num(r.close)}</td></tr>`))}

async function loadStrat(){const w=document.getElementById('win').value;const S=await j('/api/insights/strategies?window='+w);
  const h=(m,k)=>{const x=(m.decisions.horizons||{})[k]||{};return x.n?`${pct(x.hit_rate)} <span class="muted">n=${x.n}</span><br>${sg(x.mean_return_pct)}% / ex ${sg(x.mean_excess_pct)}%`:`<span class="muted">n=0${x.pending?` (${x.pending} pending)`:''}</span>`};
  document.getElementById('strat').innerHTML=(S.length?`<div class="muted">as of ${esc(S[0].as_of)}</div>`:'<p class="muted">No performance computed yet — the post-market job writes it, or press "Recompute now".</p>')+
   table(['Strategy','Status','Decisions (BUY/SELL)','5d hit · ret / excess','10d','20d','Paper realised','Trades · win','Open · unrealised','Fees'],S.map(m=>`<tr><td><a href="#" style="color:var(--accent)" onclick="eq('${esc(m.strategy_id)}','${esc(m.version)}');return false"><b>${esc(m.name||m.strategy_id)}</b></a><br><span class="muted">${esc(m.strategy_id)} ${esc(m.version)}</span></td><td><span class="pill">${esc(m.status)}</span></td><td>${m.decisions.decisions} <span class="muted">(${m.decisions.buy}/${m.decisions.sell})</span></td><td>${h(m,'5')}</td><td>${h(m,'10')}</td><td>${h(m,'20')}</td><td>${sg(m.paper.realised_pnl)}</td><td>${m.paper.closed_trades} · ${pct(m.paper.win_rate)}</td><td>${m.paper.open_positions} · ${sg(m.paper.unrealised_pnl)}</td><td>${num(m.paper.fees)}</td></tr>`))}
async function eq(sid,ver){const E=await j(`/api/insights/strategies/${encodeURIComponent(sid)}/equity?version=${encodeURIComponent(ver)}`);
  document.getElementById('eqt').textContent=`Paper realised P&L (cumulative ₹) — ${sid} ${ver}`;
  lineChart(document.getElementById('eqchart'),E.map(r=>({x:r.date,y:r.cum_realised})),{fmt:v=>'₹'+Math.round(v).toLocaleString('en-IN'),label:'cumulative realised'})}
async function refreshStrat(){try{await post('/api/insights/strategies/refresh');loadStrat()}catch(e){alert(e.message)}}

async function loadModels(){const A=await j('/api/insights/ai-strategies');
  document.getElementById('ai').innerHTML=`<b>AI-driven strategies (SE-05)</b> — <b class="${A.ready?'pos':'note'}">${A.ready?'READY':'NOT READY'}</b>
   ${table(['Check','','Detail'],A.checks.map(c=>`<tr><td>${esc(c.name)}</td><td>${c.ok?'<span class="pos">✔ pass</span>':'<span class="neg">✖ fail</span>'}</td><td class="muted">${esc(c.detail)}</td></tr>`))}
   <div style="margin-top:6px">${A.strategies.map(s=>`${esc(s.strategy_id)} <span class="pill">${esc(s.status)}</span>${s.deciding?' deciding':''}`).join(' · ')}</div>
   <div style="margin-top:6px"><input id="amodel" placeholder="model_id with an ACTIVE version" size="30"> <button onclick="activate()">Use this model for the AI strategies (→ PAPER)</button></div>
   <div class="muted">${esc(A.note)}</div>`;
  const M=await j('/api/insights/models');const box=document.getElementById('models');box.innerHTML='';
  if(!M.models.length)box.innerHTML='<p class="muted">No model has monitoring or predictions yet.</p>';
  M.models.forEach((m,i)=>{const d=document.createElement('div');d.className='row';
    d.innerHTML=`<div class="card"><b>${esc(m.model_id)} ${esc(m.version)}</b> <span class="pill">${esc(m.status)}</span><div class="muted">Columns drifting (PSI &gt; 0.25)</div><div class="chart" id="md${i}"></div></div>
     <div class="card"><b>Predictions per day</b><div class="muted">mean ML score in the table</div><div class="chart" id="mp${i}"></div></div>`;box.appendChild(d);
    lineChart(document.getElementById('md'+i),m.monitoring.map(r=>({x:r.as_of,y:r.n_shifted||0})),{fmt:v=>num(v,0),label:'columns shifted'});
    lineChart(document.getElementById('mp'+i),m.predictions.map(r=>({x:r.as_of,y:r.n})),{fmt:v=>num(v,0),label:'predictions'});
    const t=document.createElement('div');t.className='scroll';t.style.maxHeight='200px';
    t.innerHTML=table(['Date','Predictions','Mean ML score','Mean confidence'],m.predictions.slice(-30).reverse().map(r=>`<tr><td>${r.as_of}</td><td>${r.n}</td><td>${num(r.mean_score,1)}</td><td>${num(r.mean_conf,3)}</td></tr>`));box.appendChild(t)});
  const F=(M.health||{}).factors||[];
  document.getElementById('factors').innerHTML=(M.health?`<div class="muted">checked ${esc(M.health.checked_at)} · ${esc(M.health.status)}</div>`:'<p class="muted">No model / factor health check stored yet.</p>')+
   table(['Factor','Earlier IC','Recent IC','Status','Reason'],F.map(f=>`<tr><td>${esc(f.factor_key)}</td><td>${num(f.earlier_mean_ic,4)}</td><td>${num(f.recent_mean_ic,4)}</td><td><span class="pill">${esc(f.status)}</span></td><td class="muted">${esc(f.reason||'')}</td></tr>`))}
async function activate(){const m=document.getElementById('amodel').value.trim();if(!m)return;const reason=prompt(`Reason for using ${m} in the AI strategies?`);if(reason===null)return;
  try{const r=await post('/api/insights/ai-strategies/activate',{model_id:m,reason});alert(r.strategies.map(s=>`${s.strategy_id}: ${s.result}${s.detail?' — '+s.detail:''}`).join('\n'));loaded.models=0;loadModels()}catch(e){alert(e.message)}}

async function loadScans(){const s=document.getElementById('scan').value;const S=await j('/api/insights/scans'+(s?'?scan='+s:''));
  document.getElementById('scans').innerHTML=`<div class="muted">${esc(S.date||'no scan hits stored yet')}</div>`+table(['Stock','Scan','Dir','Price','Strength','Last bar','First seen','ATIP','Signal','Detail'],S.hits.map(h=>`<tr><td><b>${esc(h.symbol)}</b></td><td>${esc(h.scan)}</td><td class="${h.direction==='UP'?'pos':'neg'}">${esc(h.direction)}</td><td>${num(h.price)}</td><td>${num(h.strength)}</td><td class="muted">${esc(String(h.bar_ts).slice(11,16))}</td><td class="muted">${esc(String(h.first_seen_at).slice(11,16))}</td><td>${num(h.atip_score,1)}</td><td>${esc(h.signal)}</td><td class="muted">${esc(Object.entries(h.detail||{}).map(([k,v])=>k+' '+v).join(' · '))}</td></tr>`))}
async function runScan(){try{const r=await post('/api/insights/scans/run');alert(`${r.status}: ${r.hits??0} hits${r.reason?' — '+r.reason:''}`);loadScans()}catch(e){alert(e.message)}}

let first='news';try{first=localStorage.getItem('atip_insights_tab')||'news'}catch(e){}
if(!TABS.some(t=>t[0]===first))first='news';show(first);
</script></body></html>"""


def render(token: str) -> str:
    return PAGE.replace("__TOKEN__", json.dumps(token))
