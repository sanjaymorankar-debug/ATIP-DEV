"""
The /ml page (W5): models (type, version, status, feature set, dataset,
activation), predictions (symbol, prediction, confidence, ML score, model,
date), feature sets, datasets, training runs, and lifecycle buttons. Read
from /api/ml/*; nothing is computed here.
"""

import json

PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ATIP ML</title>
<style>
:root{--bg:#0f172a;--panel:#1e293b;--line:#334155;--text:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8;--good:#059669;--bad:#dc2626;--warn:#f59e0b}
*{box-sizing:border-box;margin:0;padding:0}body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:13px}
.top{background:var(--panel);padding:10px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px}
.top a{color:var(--accent);text-decoration:none;margin-left:12px}.wrap{padding:14px 16px;max-width:1400px}
h2{font-size:13px;color:var(--accent);margin:16px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
table{width:100%;border-collapse:collapse;background:var(--panel);font-size:11.5px}
th,td{padding:5px 7px;border-bottom:1px solid #1e293b55;text-align:left;vertical-align:top}th{color:var(--muted);background:var(--bg);white-space:nowrap}
.muted{color:var(--muted)}.note{color:var(--warn)}.scroll{max-height:360px;overflow:auto}
.pill{display:inline-block;padding:1px 7px;border-radius:9px;font-size:10.5px;background:#334155}
.ACTIVE,.COMPLETED,.TRAINED,.BUILT{background:#065f46}.FAILED{background:#7f1d1d}.VALIDATION,.APPROVED,.PAUSED,.RUNNING{background:#78350f}
button{background:#2563eb;color:#fff;border:none;border-radius:6px;padding:3px 8px;cursor:pointer;font-size:11px;margin:1px}
</style></head><body>
<div class="top"><div><b style="color:var(--accent)">📊 ATIP</b> <span class="muted">AI / ML (W5)</span></div>
<div><a href="/strategies">Strategies</a><a href="/trading">Trading</a><a href="/">← Dashboard</a></div></div>
<div class="wrap">
<p class="muted">Models produce predictions only. ML reaches trading solely as a strategy feature (ml_score…), and every strategy decision still passes the W4 risk engine. Nothing here is validated: metrics are descriptive.</p>
<h2>Status</h2><div id="st"></div>
<h2>Models</h2><div id="mod" class="scroll"></div>
<h2>Latest predictions</h2><div id="pred" class="scroll"></div>
<h2>Feature sets</h2><div id="fs" class="scroll"></div>
<h2>Datasets</h2><div id="ds" class="scroll"></div>
<h2>Training runs</h2><div id="tr" class="scroll"></div>
</div>
<script>
const TOKEN=__TOKEN__;
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const num=(v,d=3)=>v==null?'—':Number(v).toFixed(d);
const pill=s=>`<span class="pill ${s}">${s}</span>`;
async function j(u,o){const r=await fetch(u,o);const t=await r.json();if(!r.ok)throw new Error(t.error||r.status);return t}
const post=(u,b)=>j(u,{method:'POST',headers:{'Content-Type':'application/json','X-ATIP-Token':TOKEN},body:JSON.stringify(b||{})});
const table=(h,rows)=>`<table><thead><tr>${h.map(x=>`<th>${x}</th>`).join('')}</tr></thead><tbody>${rows.join('')||`<tr><td colspan=${h.length} class="muted">none</td></tr>`}</tbody></table>`;
const NEXT={TRAINED:['VALIDATION'],VALIDATION:['APPROVED'],APPROVED:['ACTIVE'],ACTIVE:['PAUSED'],PAUSED:['ACTIVE']};
async function load(){
  const s=await j('/api/ml/status');
  document.getElementById('st').innerHTML=`ML scheduled predictions <b>${s.settings.enabled?'ON':'OFF'}</b> · default model <b>${esc(s.settings.default_model)||'none'}</b> · regime source <b>${s.settings.regime_source}</b> · families: ${Object.entries(s.model_families).map(([k,v])=>`${k} <span class="muted">(${esc(v)})</span>`).join(', ')}<br><span class="muted">${esc(s.execution_link)}</span>`;
  const M=await j('/api/ml/models');let rows=[];
  for(const m of M){const d=await j('/api/ml/models/'+m.model_id);
    for(const v of d.versions){rows.push(`<tr><td><b>${esc(m.model_id)}</b><br><span class="muted">${esc(m.name)}</span></td><td>${m.model_type}<br><span class="muted">${m.task} · ${m.label_kind} · ${m.purpose}</span></td><td>${v.version}</td><td>${pill(v.status)}</td><td>${esc(v.feature_set)}<br><span class="muted">${(v.feature_set_hash||'').slice(0,10)}</span></td><td>${esc(v.dataset_id)}</td><td>${esc(v.train_start||'')} → ${esc(v.train_end||'')}</td><td class="muted">${esc(JSON.stringify((v.metrics||{}).validation||{}))}</td><td>${(NEXT[v.status]||[]).map(t=>`<button onclick="move('${m.model_id}','${v.version}','${t}')">→ ${t}</button>`).join('')}</td></tr>`)}
    if(!d.versions.length)rows.push(`<tr><td><b>${esc(m.model_id)}</b></td><td>${m.model_type}</td><td colspan=7 class="muted">no versions yet (train via POST /api/ml/models/${m.model_id}/train)</td></tr>`)}
  document.getElementById('mod').innerHTML=table(['Model','Type','Version','Status','Feature set','Dataset','Training period','Validation (descriptive)',''],rows);
  const P=await j('/api/ml/predictions?limit=100');
  document.getElementById('pred').innerHTML=table(['Date','Symbol','Prediction','Confidence','ML score','Model','Status','Top features'],P.map(p=>`<tr><td>${p.as_of}</td><td>${esc(p.symbol)}</td><td><b>${esc(p.prediction)}</b></td><td>${num(p.confidence)}</td><td>${num(p.ml_score,1)}</td><td>${esc(p.model_id)} ${p.model_version}</td><td>${pill(p.version_status)}</td><td class="muted">${esc(((p.explanation||{}).top_features||[]).join(', '))}</td></tr>`));
  const F=await j('/api/ml/feature-sets');
  document.getElementById('fs').innerHTML=table(['Name','Version','Features','Hash','Created'],F.map(f=>`<tr><td>${esc(f.name)}</td><td>${f.version}</td><td>${f.features.length} <span class="muted">${esc(f.features.join(', '))}</span></td><td class="muted">${f.content_hash.slice(0,12)}</td><td>${esc(f.created_at)}</td></tr>`));
  const D=await j('/api/ml/datasets');
  document.getElementById('ds').innerHTML=table(['Dataset','Feature set','Label','Period','Status','Rows'],D.map(d=>`<tr><td>${esc(d.dataset_id)}</td><td>${esc(d.feature_set)}</td><td>${esc(JSON.stringify(d.label))}</td><td>${d.start_date} → ${d.end_date}</td><td>${pill(d.status)}</td><td>${(d.summary||{}).rows??'—'}</td></tr>`));
  const T=await j('/api/ml/training-runs');
  document.getElementById('tr').innerHTML=table(['Run','Model','Version','Dataset','Status','Started','Finished','Error'],T.map(t=>`<tr><td>${t.run_id}</td><td>${esc(t.model_id)}</td><td>${t.version||''}</td><td>${esc(t.dataset_id)}</td><td>${pill(t.status)}</td><td>${esc(t.started_at)}</td><td>${esc(t.finished_at||'')}</td><td class="note">${esc(t.error||'')}</td></tr>`));
}
async function move(m,v,to){const reason=prompt(`Reason for ${m} ${v} → ${to}?`);if(reason===null)return;try{await post(`/api/ml/models/${m}/lifecycle`,{version:v,to_state:to,reason});load()}catch(e){alert(e.message)}}
load();
</script></body></html>"""


def render(token: str) -> str:
    return PAGE.replace("__TOKEN__", json.dumps(token))
