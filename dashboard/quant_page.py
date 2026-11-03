"""
The /quant page (W6): factors (category, coverage, top ranks), composites,
quant strategies, pairs (spread, z-score, hedge ratio), experiments, portfolio
exposure, data dependencies. Read from /api/quant/*; nothing is computed here.
W40: the factor risk model's factors (latest return, t statistic, 20-session return, annualised
volatility), the regression R^2 and the bias test (/api/quant/risk-model/*); the book view is on
/trading (Portfolio risk).

UX audit (3 Nov 2026): plain labels instead of config keys (the key stays in a tooltip), every table in a
keyboard-reachable scroll region with loading / empty / error states, each section loading on its own.
"""

import json

PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ATIP Quant</title>
<style>
:root{--bg:#0f172a;--panel:#1e293b;--panel2:#334155;--line:#334155;--text:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8;--good:#6ee7b7;--bad:#fca5a5;--warn:#fde68a;--goodbg:#0e2a22;--badbg:#3b1114;--warnbg:#2a2410}
*{box-sizing:border-box;margin:0;padding:0}body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:13px}
.top{background:var(--panel);padding:10px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px}
.top>div:last-child{display:flex;flex-wrap:wrap;gap:4px 12px}.top a{color:var(--accent);text-decoration:none;display:inline-block;padding:4px 0}.wrap{padding:14px 16px;max-width:1400px}
h2{font-size:13px;color:var(--accent);margin:16px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
table{width:100%;border-collapse:collapse;overflow-wrap:normal;background:var(--panel);font-size:12.5px}
th,td{padding:5px 7px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;font-variant-numeric:tabular-nums}th{color:var(--muted);background:var(--bg);white-space:nowrap;position:sticky;top:0;z-index:1}
.muted{color:var(--muted)}.note{color:var(--warn)}
.pill{display:inline-block;padding:1px 8px;border-radius:9px;font-size:12px;background:var(--panel2);color:var(--text)}
.pill.ACTIVE,.pill.OK,.pill.COMPLETED{background:var(--goodbg);color:var(--good)}.pill.DATA_PENDING,.pill.DRAFT{background:var(--warnbg);color:var(--warn)}.pill.FAILED{background:var(--badbg);color:var(--bad)}
select,button{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:3px 7px;font-size:12.5px;max-width:100%}
label{color:var(--muted);font-size:12px;font-weight:400}
p,.wrap>div{overflow-wrap:anywhere}
.deps{display:grid;grid-template-columns:max-content 1fr;gap:3px 12px;margin:6px 0}.deps dt{color:var(--muted)}.deps dd{color:var(--warn)}
.tw{overflow:auto;max-width:100%;margin:2px 0}.scroll .tw{max-height:360px}
.tw:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.empty{color:var(--muted);padding:8px 2px;margin:2px 0}
.err{background:var(--badbg);color:var(--bad);border:1px solid var(--bad);border-radius:6px;padding:8px 10px;margin:4px 0}
.skel{display:grid;gap:8px;padding:6px 0}.skel .ax-skel{height:14px}.ax-skel{background:var(--panel2);border-radius:6px;min-height:14px}
@media(max-width:760px){.wrap{padding:12px}.deps{grid-template-columns:1fr}.tw td:first-child,.tw th:first-child{position:sticky;left:0;z-index:1;background:var(--panel)}.tw thead th:first-child{z-index:2;background:var(--bg)}}
</style></head><body>
<div class="top"><div><b style="color:var(--accent)">📊 ATIP</b> <span class="muted">Advanced quant</span></div>
<div><a href="/strategies">Strategies</a><a href="/ml">ML</a><a href="/trading">Trading</a><a href="/">← Dashboard</a></div></div>
<div class="wrap">
<p class="muted">Factor scores, pairs and portfolios are research outputs. They reach trading only through strategies and the risk engine. Nothing here is validated.</p>
<h2>Status &amp; data dependencies</h2><div id="st" data-load></div>
<h2>Factors</h2><div id="fac" class="scroll" data-load></div>
<h2>Top ranks <label for="key">Rank by</label> <select id="key" onchange="ranks()"></select></h2><div id="rk" class="scroll" data-load></div>
<h2>Quant strategies</h2><div id="qs" class="scroll" data-load></div>
<h2>Pairs</h2><div id="pr" class="scroll" data-load></div>
<h2>Experiments</h2><div id="ex" class="scroll" data-load></div>
<h2>Portfolio exposure (latest)</h2><div id="pf" data-load></div>
<h2 id="rmh">Factor risk model</h2><div id="rm" data-load></div>
</div>
<script>
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const num=(v,d=2)=>v==null?'—':Number(v).toFixed(d);const pill=s=>`<span class="pill ${esc(s)}">${esc(s)}</span>`;
async function j(u){const r=await fetch(u);const t=await r.json();if(!r.ok)throw new Error(t.error||r.status);return t}
// UX audit M7: shimmer while loading, why + what to do when empty, the error and what to try when a read fails
const SKEL='<div class="skel" role="status" aria-busy="true" aria-label="Loading"><div class="ax-skel"></div><div class="ax-skel"></div><div class="ax-skel"></div></div>';
const TRY='Check that the ATIP server is running, then reload this page.';
function skel(){document.querySelectorAll('[data-load]').forEach(el=>{if(!el.innerHTML.trim())el.innerHTML=SKEL})}
function fail(el,what,e,tip){el=typeof el==='string'?document.getElementById(el):el;if(el)el.innerHTML=`<div class="err" role="alert"><b>Could not load ${esc(what)}:</b> ${esc((e&&e.message)||e)}. ${tip||TRY}</div>`}
async function sec(id,what,fn,tip){const el=document.getElementById(id);try{await fn(el)}catch(e){fail(el,what,e,tip)}}
const strip=x=>String(x).replace(/<[^>]*>/g,'');
const table=(h,rows,o)=>{o=o||{};const lbl=o.label||('Table: '+h.filter(Boolean).slice(0,4).map(strip).join(', '));
  if(!rows.length)return `<p class="empty" role="status">${o.empty||'Nothing to show yet.'}</p>`;
  return `<div class="tw" tabindex="0" role="region" aria-label="${esc(lbl)}"><table><thead><tr>${h.map(x=>`<th scope="col">${x}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`};
// UX audit M3: plain names for the data-dependency keys; the key stays in the tooltip
const DEP={fundamentals:'Fundamentals',shares:'Shares outstanding / market cap',spread:'Bid-ask spread',derivatives:'Derivatives (F&O)',
  microstructure:'Market microstructure',events_pending:'Event types not collected yet',depth:'Order-book depth',order_imbalance:'Order imbalance',tick_count:'Tick count'};
const lab=k=>`<span title="Config key: ${esc(k)}">${esc(DEP[k]||(k.charAt(0).toUpperCase()+k.slice(1)).replace(/_/g,' '))}</span>`;
const depVal=v=>typeof v==='string'?esc(v):Array.isArray(v)?esc(v.map(x=>String(x).replace(/_/g,' ').toLowerCase()).join(', ')):
  `<dl class="deps">${Object.entries(v||{}).map(([k,x])=>`<dt>${lab(k)}</dt><dd>${depVal(x)}</dd>`).join('')}</dl>`;
const normTxt=n=>!n?'—':typeof n!=='object'?esc(n):esc([n.method,n.winsorize!=null?'winsorized at '+n.winsorize+'%':null,n.neutralize?'neutralised by '+n.neutralize:null].filter(Boolean).join(', ')||JSON.stringify(n));
let QS=null;
const notRun=()=>QS&&QS.settings&&!QS.settings.enabled?'Scheduled factor scoring is off (quant.enabled in config.json); turn it on and the post-market run (16:45 IST) computes them.':'They are computed in the post-market run (16:45 IST on trading days).';
async function load(){
  let F=[];const cov={};
  await sec('st','the quant status',async el=>{const s=await j('/api/quant/status');QS=s;(s.coverage||[]).forEach(c=>cov[c.factor_key]=c.n);
    el.innerHTML=`Scheduled factor computation <b>${s.settings.enabled?'ON':'OFF'}</b> · latest scores <b>${esc(s.latest_scores||'not computed yet')}</b>`+
     `<dl class="deps">${Object.entries(s.data_dependencies||{}).map(([k,v])=>`<dt>${lab(k)}</dt><dd>${depVal(v)}</dd>`).join('')}</dl><div class="muted">${esc(s.execution_link)}</div>`});
  await sec('fac','the factors',async el=>{F=await j('/api/quant/factors');
    el.innerHTML=table(['Factor','Category','Direction','Normalization','Status','Coverage (latest)','Formula'],F.map(f=>`<tr><td><b>${esc(f.name)}</b><br><span class="muted">${esc(f.factor_id)} v${esc(f.version)}</span></td><td>${esc(f.category)}</td><td>${f.direction>0?'higher better':'lower better'}</td><td class="muted">${normTxt(f.normalization)}</td><td>${pill(f.status)}</td><td>${cov[f.factor_id+'@'+f.version]??'—'}</td><td class="muted">${esc(f.formula)}</td></tr>`),
      {label:'Factors',empty:'No factors are registered. The factor library loads with quant/factors.py; restart ATIP if this stays empty.'})});
  await sec('rk','the composites',async el=>{const C=await j('/api/quant/composites');const sel=document.getElementById('key');
    sel.innerHTML=C.map(c=>`<option value="composite:${esc(c.name)}@${esc(c.version)}">composite ${esc(c.name)}@${esc(c.version)}</option>`).join('')+F.map(f=>`<option value="${esc(f.factor_id)}@${esc(f.version)}">${esc(f.name||f.factor_id)}</option>`).join('');
    if(!sel.options.length)el.innerHTML='<p class="muted">No factors or composites to rank by yet.</p>';else await ranks()});
  await Promise.all([
  sec('qs','the quant strategies',async el=>{const Q=await j('/api/quant/strategies');
    el.innerHTML=table(['Strategy','Kind','Status','Version','Features'],Q.map(q=>`<tr><td>${esc(q.strategy_id)}<br><span class="muted">${esc(q.name)}</span></td><td>${esc(q.kind)}</td><td>${pill(q.status)}</td><td>${esc(q.current_version)}</td><td class="muted">${esc((q.features_used||[]).join(', '))}</td></tr>`),
      {label:'Quant strategies',empty:'No strategy reads quant factors yet. Build one on Strategy builder with a qf_* or qc_* feature.'})}),
  sec('pr','the pairs',async el=>{const P=await j('/api/quant/pairs');const S=await j('/api/quant/spreads?limit=200');const last={};S.forEach(x=>{if(!last[x.pair_id])last[x.pair_id]=x});
    el.innerHTML=table(['Pair','A / B','Hedge','Entry/Exit/Stop z','Status','Last z','Corr','ADF t','Half-life'],P.map(p=>{const l=last[p.pair_id]||{};return `<tr><td>${esc(p.pair_id)}@${esc(p.version)}</td><td>${esc(p.asset_a)} / ${esc(p.asset_b)}</td><td>${esc(typeof p.hedge_ratio==='object'?Object.entries(p.hedge_ratio||{}).map(([k,v])=>k.replace(/_/g,' ')+' '+v).join(', '):p.hedge_ratio)}</td><td>${p.entry_z} / ${p.exit_z} / ${p.stop_z}</td><td>${pill(p.status)}</td><td>${num(l.zscore)}</td><td>${num(l.correlation)}</td><td>${num(l.adf_t)}</td><td>${num(l.half_life,1)}</td></tr>`}),
      {label:'Pairs',empty:'No pairs defined yet. Add one with POST /api/quant/pairs; spreads are then computed in the post-market run.'})}),
  sec('ex','the experiments',async el=>{const E=await j('/api/quant/experiments');
    el.innerHTML=table(['Experiment','Hypothesis','Strategy','Period','Status','Backtests'],E.map(e=>`<tr><td>${esc(e.name)}@${esc(e.version)}</td><td class="muted" style="min-width:200px">${esc(e.hypothesis)}</td><td>${esc(e.config.strategy_id||'')}</td><td>${esc(e.config.start)} → ${esc(e.config.end)}</td><td>${pill(e.status)}</td><td class="muted">${esc(e.backtest_run_ids.join(', '))}</td></tr>`),
      {label:'Experiments',empty:'No experiments yet. Record one with POST /api/quant/experiments to keep a hypothesis next to its backtests.'})}),
  sec('pf','the portfolio exposure',async el=>{const PF=await j('/api/quant/portfolios?limit=1');
    if(PF.length){const p=PF[0],x=p.exposures||{};el.innerHTML=`<b>${esc(p.name)}</b> ${esc(p.as_of)} · ${esc(p.method)} · long ${num(x.long,3)} short ${num(x.short,3)} gross ${num(x.gross,3)} net ${num(x.net,3)} beta ${num(x.beta,3)}`+table(['Sector','Net weight'],Object.entries(x.sector_net||{}).map(([k,v])=>`<tr><td>${esc(k)}</td><td>${num(v,4)}</td></tr>`),{label:'Portfolio net weight by sector',empty:'This portfolio has no sector weights.'})}
    else el.innerHTML='<p class="muted">No portfolio built yet. Build one with POST /api/quant/portfolios from a composite score.</p>'})]);
}
async function riskModel(){const el=document.getElementById('rm');try{
  const S=await j('/api/quant/risk-model/status');
  if(S.status==='NOT_BUILT'){el.innerHTML=`<p class="muted">${esc(S.note)}</p>`;return}
  const F=await j('/api/quant/risk-model/factors?days=20');const B=(S.bias||{}).summary||{},X=S.exposures||{},R=S.factor_returns||{};
  el.innerHTML=`<div>${pill(S.status)} model ${esc((S.model||{}).model_hash)} · exposures ${esc(X.first)} → ${esc(X.last)} (${X.latest_estimation_universe??'—'} of ${X.latest_universe??'—'} stocks in the estimation universe) · R² latest ${num(R.r2_latest,3)}, 20-session mean ${num(R.r2_mean_20,3)}`+(B.portfolios?` · bias test: ${B.in_band} of ${B.portfolios} test portfolios inside the 95% band, median ${num(B.median_bias,3)}`:'')+`</div>`+
   (F.status==='OK'?table(['Factor','Kind','Return '+esc(F.factor_return_date||''),'t','20 sessions','Vol (ann.)'],F.factors.map(f=>`<tr><td>${esc(f.factor)}</td><td class="muted">${esc(f.kind)}</td><td>${num(f.return_pct,3)}%</td><td>${num(f.t_stat,2)}</td><td>${num(f.cum_return_pct_20d,2)}%</td><td>${num(f.vol_pct_annual,1)}%</td></tr>`),{label:'Risk model factor returns',empty:'No factor returns for the last 20 sessions.'}):`<p class="muted">${esc(F.note||'No factor returns yet.')}</p>`)}catch(e){fail(el,'the factor risk model',e)}}
async function ranks(){const k=document.getElementById('key').value;if(!k)return;const el=document.getElementById('rk');try{const R=await j('/api/quant/rankings?top=25&key='+encodeURIComponent(k));
  el.innerHTML=table(['Rank','Symbol','Score','Raw','Sector','Sector rank'],R.map(r=>`<tr><td>${r.rank}</td><td>${esc(r.symbol)}</td><td>${num(r.score,1)}</td><td>${num(r.raw,4)}</td><td>${esc(r.sector)}</td><td>${r.sector_rank??''}</td></tr>`),
    {label:'Top ranks',empty:'No scores for this factor yet. '+notRun()})}catch(e){fail(el,'the rankings',e,'Pick another factor, or reload this page.')}}
skel();load();riskModel();
</script></body></html>"""


def render(token: str) -> str:
    return PAGE.replace("__TOKEN__", json.dumps(token))
