"""
The /quant page (W6): factors (category, coverage, top ranks), composites,
quant strategies, pairs (spread, z-score, hedge ratio), experiments, portfolio
exposure, data dependencies. Read from /api/quant/*; nothing is computed here.
"""

import json

PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ATIP Quant</title>
<style>
:root{--bg:#0f172a;--panel:#1e293b;--line:#334155;--text:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8;--good:#059669;--bad:#dc2626;--warn:#f59e0b}
*{box-sizing:border-box;margin:0;padding:0}body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:13px}
.top{background:var(--panel);padding:10px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px}
.top a{color:var(--accent);text-decoration:none;margin-left:12px}.wrap{padding:14px 16px;max-width:1400px}
h2{font-size:13px;color:var(--accent);margin:16px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
table{width:100%;border-collapse:collapse;background:var(--panel);font-size:11.5px}
th,td{padding:5px 7px;border-bottom:1px solid #1e293b55;text-align:left;vertical-align:top}th{color:var(--muted);background:var(--bg);white-space:nowrap}
.muted{color:var(--muted)}.note{color:var(--warn)}.scroll{max-height:360px;overflow:auto}
.pill{display:inline-block;padding:1px 7px;border-radius:9px;font-size:10.5px;background:#334155}.ACTIVE,.OK,.COMPLETED{background:#065f46}.DATA_PENDING,.DRAFT{background:#78350f}.FAILED{background:#7f1d1d}
select,button{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:3px 7px;font-size:12px}
</style></head><body>
<div class="top"><div><b style="color:var(--accent)">📊 ATIP</b> <span class="muted">Advanced quant (W6)</span></div>
<div><a href="/strategies">Strategies</a><a href="/ml">ML</a><a href="/trading">Trading</a><a href="/">← Dashboard</a></div></div>
<div class="wrap">
<p class="muted">Factor scores, pairs and portfolios are research outputs. They reach trading only through W3 strategies and the W4 risk engine. Nothing here is validated.</p>
<h2>Status &amp; data dependencies</h2><div id="st"></div>
<h2>Factors</h2><div id="fac" class="scroll"></div>
<h2>Top ranks <select id="key" onchange="ranks()"></select></h2><div id="rk" class="scroll"></div>
<h2>Quant strategies</h2><div id="qs" class="scroll"></div>
<h2>Pairs</h2><div id="pr" class="scroll"></div>
<h2>Experiments</h2><div id="ex" class="scroll"></div>
<h2>Portfolio exposure (latest)</h2><div id="pf"></div>
</div>
<script>
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const num=(v,d=2)=>v==null?'—':Number(v).toFixed(d);const pill=s=>`<span class="pill ${s}">${s}</span>`;
async function j(u){const r=await fetch(u);const t=await r.json();if(!r.ok)throw new Error(t.error||r.status);return t}
const table=(h,rows)=>`<table><thead><tr>${h.map(x=>`<th>${x}</th>`).join('')}</tr></thead><tbody>${rows.join('')||`<tr><td colspan=${h.length} class="muted">none</td></tr>`}</tbody></table>`;
async function load(){
  const s=await j('/api/quant/status');const cov={};(s.coverage||[]).forEach(c=>cov[c.factor_key]=c.n);
  document.getElementById('st').innerHTML=`Scheduled factor computation <b>${s.settings.enabled?'ON':'OFF'}</b> · latest scores <b>${s.latest_scores||'none'}</b><br>`+Object.entries(s.data_dependencies).map(([k,v])=>`<div class="note">${k}: ${esc(typeof v==='string'?v:JSON.stringify(v))}</div>`).join('')+`<div class="muted">${esc(s.execution_link)}</div>`;
  const F=await j('/api/quant/factors');
  document.getElementById('fac').innerHTML=table(['Factor','Category','Direction','Normalization','Status','Coverage (latest)','Formula'],F.map(f=>`<tr><td><b>${f.factor_id}</b> <span class="muted">v${f.version}</span><br><span class="muted">${esc(f.name)}</span></td><td>${f.category}</td><td>${f.direction>0?'higher better':'lower better'}</td><td class="muted">${esc(JSON.stringify(f.normalization))}</td><td>${pill(f.status)}</td><td>${cov[f.factor_id+'@'+f.version]??'—'}</td><td class="muted">${esc(f.formula)}</td></tr>`));
  const C=await j('/api/quant/composites');const sel=document.getElementById('key');
  sel.innerHTML=C.map(c=>`<option value="composite:${c.name}@${c.version}">composite ${c.name}@${c.version}</option>`).join('')+F.map(f=>`<option value="${f.factor_id}@${f.version}">${f.factor_id}</option>`).join('');
  ranks();
  const Q=await j('/api/quant/strategies');
  document.getElementById('qs').innerHTML=table(['Strategy','Kind','Status','Version','Features'],Q.map(q=>`<tr><td>${esc(q.strategy_id)}<br><span class="muted">${esc(q.name)}</span></td><td>${q.kind}</td><td>${pill(q.status)}</td><td>${q.current_version}</td><td class="muted">${esc((q.features_used||[]).join(', '))}</td></tr>`));
  const P=await j('/api/quant/pairs');const S=await j('/api/quant/spreads?limit=200');const last={};S.forEach(x=>{if(!last[x.pair_id])last[x.pair_id]=x});
  document.getElementById('pr').innerHTML=table(['Pair','A / B','Hedge','Entry/Exit/Stop z','Status','Last z','Corr','ADF t','Half-life'],P.map(p=>{const l=last[p.pair_id]||{};return `<tr><td>${p.pair_id}@${p.version}</td><td>${p.asset_a} / ${p.asset_b}</td><td>${esc(JSON.stringify(p.hedge_ratio))}</td><td>${p.entry_z} / ${p.exit_z} / ${p.stop_z}</td><td>${pill(p.status)}</td><td>${num(l.zscore)}</td><td>${num(l.correlation)}</td><td>${num(l.adf_t)}</td><td>${num(l.half_life,1)}</td></tr>`}));
  const E=await j('/api/quant/experiments');
  document.getElementById('ex').innerHTML=table(['Experiment','Hypothesis','Strategy','Period','Status','Backtests'],E.map(e=>`<tr><td>${esc(e.name)}@${e.version}</td><td class="muted">${esc(e.hypothesis)}</td><td>${esc(e.config.strategy_id||'')}</td><td>${e.config.start} → ${e.config.end}</td><td>${pill(e.status)}</td><td class="muted">${e.backtest_run_ids.join(', ')}</td></tr>`));
  const PF=await j('/api/quant/portfolios?limit=1');
  if(PF.length){const p=PF[0],x=p.exposures||{};document.getElementById('pf').innerHTML=`<b>${esc(p.name)}</b> ${p.as_of} · ${p.method} · long ${num(x.long,3)} short ${num(x.short,3)} gross ${num(x.gross,3)} net ${num(x.net,3)} beta ${num(x.beta,3)}`+table(['Sector','Net weight'],Object.entries(x.sector_net||{}).map(([k,v])=>`<tr><td>${esc(k)}</td><td>${num(v,4)}</td></tr>`))}else document.getElementById('pf').innerHTML='<span class="muted">no portfolio built yet (POST /api/quant/portfolios)</span>';
}
async function ranks(){const k=document.getElementById('key').value;if(!k)return;try{const R=await j('/api/quant/rankings?top=25&key='+encodeURIComponent(k));
  document.getElementById('rk').innerHTML=table(['Rank','Symbol','Score','Raw','Sector','Sector rank'],R.map(r=>`<tr><td>${r.rank}</td><td>${esc(r.symbol)}</td><td>${num(r.score,1)}</td><td>${num(r.raw,4)}</td><td>${esc(r.sector)}</td><td>${r.sector_rank??''}</td></tr>`))}catch(e){document.getElementById('rk').textContent=e.message}}
load();
</script></body></html>"""


def render(token: str) -> str:
    return PAGE.replace("__TOKEN__", json.dumps(token))
