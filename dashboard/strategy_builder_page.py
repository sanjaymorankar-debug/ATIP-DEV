"""
The /strategy-builder page (W36: SE-10): build a rule strategy from dropdowns, preview today's matches,
save it as a DRAFT. Everything goes through /api/strategy-builder/*; the definition it produces is the
same W3 JSON a coded library strategy uses, validated the same way.
"""

import json

PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ATIP Strategy Builder</title>
<style>
:root{--bg:#0f172a;--panel:#1e293b;--line:#334155;--text:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8;--good:#34d399;--bad:#f87171;--warn:#f59e0b}
*{box-sizing:border-box;margin:0;padding:0}body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:13px}
.top{background:var(--panel);padding:10px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px}
.top a{color:var(--accent);text-decoration:none;margin-left:12px}.wrap{padding:14px 16px;max-width:1200px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin-bottom:12px}
h3{font-size:13px;color:var(--accent);margin-bottom:8px}.muted{color:var(--muted)}.note{color:var(--warn)}
input,select,textarea{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:5px 7px;font-size:12.5px}
button{background:#2563eb;color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer;font-size:12.5px;margin:2px}
button.ghost{background:transparent;border:1px solid var(--line);color:var(--muted)}
.row{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:4px 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px}
label{color:var(--muted);font-size:12px;display:block}label input,label select{width:100%;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:4px 6px;border-bottom:1px solid #33415555;text-align:left}th{color:var(--muted)}
pre{white-space:pre-wrap;font-size:11.5px;color:var(--muted);max-height:360px;overflow:auto;background:var(--bg);padding:8px;border-radius:6px}
.ok{color:var(--good)}.bad{color:var(--bad)}
@media(max-width:600px){.row>*{flex:1 1 100%}}
</style></head><body>
<div class="top"><div><b style="color:var(--accent)">📊 ATIP</b> <span class="muted">No-code strategy builder (SE-10)</span></div>
<div><a href="/strategies">Strategies</a><a href="/backtests">Backtests</a><a href="/">← Dashboard</a></div></div>
<div class="wrap">
<div class="card"><h3>1. Name</h3><div class="grid"><label>Name<input id="name" placeholder="e.g. Oversold quality bounce"></label>
<label style="grid-column:span 2">Description<input id="desc" placeholder="what it is meant to catch"></label></div></div>

<div class="card"><h3>2. Enter when</h3><div class="row"><select id="emode" onchange="kvis()"><option value="all">ALL of these hold</option><option value="any">ANY of these holds</option><option value="min">AT LEAST k of these hold</option></select>
<span id="kbox" style="display:none">k = <input id="ek" type="number" min="1" value="2" style="width:60px"></span></div>
<div id="entry"></div><button class="ghost" onclick="addCond('entry')">+ condition</button></div>

<div class="card"><h3>3. Exit when</h3><div class="row"><select id="xmode"><option value="any">ANY of these holds</option><option value="all">ALL of these hold</option></select></div>
<div id="exit"></div><button class="ghost" onclick="addCond('exit')">+ condition</button>
<div class="muted" style="margin-top:6px">Stops, targets and the holding limit below also close positions.</div></div>

<div class="card"><h3>4. Sizing, ranking and risk</h3><div class="grid">
<label>Position % of equity<input id="position_pct" type="number" value="8" step="0.5"></label>
<label>Max positions<input id="max_positions" type="number" value="10"></label>
<label>New per day<input id="max_new_per_day" type="number" value="3"></label>
<label>Stop % below entry<input id="stop_pct" type="number" value="5" step="0.5"></label>
<label>Target % above entry<input id="target_pct" type="number" value="10" step="0.5"></label>
<label>Max holding sessions<input id="max_hold" type="number" value="20"></label>
<label>Rank candidates by<input id="rank_by" list="feats" value="atip_score"></label>
<label>Skip when CRI above<input id="max_cri" type="number" value="75"></label>
<label>Blocked regimes<input id="blocked" value="HIGH_RISK"></label>
<label>Only these symbols (optional)<input id="symbols" placeholder="TCS, INFY"></label></div></div>

<div class="card"><button onclick="act('compile')">Check</button><button onclick="act('preview')">Preview today's matches</button><button onclick="act('save')">Save as DRAFT</button>
<span id="msg" class="muted" style="margin-left:8px"></span><div id="out" style="margin-top:8px"></div></div>
</div>
<datalist id="feats"></datalist>
<script>
const TOKEN=__TOKEN__;
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let OPS=['<','<=','>','>=','==','!=','between','in','not_in','crosses_above','crosses_below'];
const EXAMPLES=['rsi_14','sma_20','sma_50','sma_200','ema_20','ret_5','ret_20','ret_60','atr_pct_14','vol_ratio_20','volatility_20','bb_pctb_20','range_pos_20','rel_strength_60','adx_14','zscore_20'];
async function j(u,o){const r=await fetch(u,o);const t=await r.json();if(!r.ok)throw new Error(t.error||t.detail||r.status);return t}
const post=(u,b)=>j(u,{method:'POST',headers:{'Content-Type':'application/json','X-ATIP-Token':TOKEN},body:JSON.stringify(b||{})});
function kvis(){document.getElementById('kbox').style.display=document.getElementById('emode').value==='min'?'':'none'}
function addCond(where,c){c=c||{};const d=document.createElement('div');d.className='row cond';
  d.innerHTML=`<input class="f" list="feats" placeholder="feature e.g. rsi_14" value="${esc(c.feature||'')}" style="width:170px">
  <select class="op">${OPS.map(o=>`<option ${o===(c.op||'>')?'selected':''}>${o}</option>`).join('')}</select>
  <select class="vt"><option value="number" ${c.value_type==='number'||!c.value_type?'selected':''}>number</option><option value="feature" ${c.value_type==='feature'?'selected':''}>feature</option><option value="list" ${c.value_type==='list'?'selected':''}>list</option></select>
  <input class="v" placeholder="value (between: 40, 70)" value="${esc(c.value??'')}" style="width:170px">
  <button class="ghost" onclick="this.parentNode.remove()">✕</button>`;
  document.getElementById(where).appendChild(d)}
function conds(where){return [...document.querySelectorAll('#'+where+' .cond')].map(r=>({feature:r.querySelector('.f').value.trim(),op:r.querySelector('.op').value,value_type:r.querySelector('.vt').value,value:r.querySelector('.v').value.trim()}))}
const g=id=>document.getElementById(id).value.trim();
function spec(){const syms=g('symbols');return {name:g('name'),description:g('desc'),
  entry:{mode:g('emode'),k:+g('ek'),conditions:conds('entry')},exit:{mode:g('xmode'),conditions:conds('exit')},
  position:{position_pct:+g('position_pct'),max_positions:+g('max_positions'),max_new_per_day:+g('max_new_per_day'),stop_pct:+g('stop_pct'),target_pct:+g('target_pct'),max_hold:+g('max_hold')},
  rank_by:g('rank_by'),max_cri:+g('max_cri'),blocked_regimes:g('blocked').split(',').map(x=>x.trim()).filter(Boolean),
  symbols:syms?syms.split(',').map(x=>x.trim().toUpperCase()).filter(Boolean):null}}
async function act(kind){const m=document.getElementById('msg'),o=document.getElementById('out');m.className='muted';m.textContent='working…';
  try{const r=await post('/api/strategy-builder/'+kind,{spec:spec()});
    if(kind==='compile'){m.className='ok';m.textContent=`✔ valid: ${r.strategy_id} (${r.parameters.length} tunable parameters)`;o.innerHTML='<pre>'+esc(JSON.stringify(r,null,2))+'</pre>'}
    else if(kind==='preview'){m.className='ok';m.textContent=`${r.match_count} of ${r.evaluated} stocks qualify on ${r.as_of}`;
      o.innerHTML=`<div class="muted">${esc(r.note)}</div><table><thead><tr><th>Symbol</th><th>Conditions met</th><th>Rank value</th></tr></thead><tbody>${r.matches.map(x=>`<tr><td><b>${esc(x.symbol)}</b></td><td>${x.conditions_met}/${x.conditions}</td><td>${esc(x.rank_value)}</td></tr>`).join('')||'<tr><td colspan=3 class="muted">no matches</td></tr>'}</tbody></table>`+
      (r.one_condition_short.length?`<div class="muted" style="margin-top:6px">One condition short: ${r.one_condition_short.map(x=>esc(x.symbol)).join(', ')}</div>`:'')}
    else{m.className='ok';m.innerHTML=`✔ saved as DRAFT: <a style="color:var(--accent)" href="/strategies">${esc(r.strategy_id||'')}</a> — backtest it from Strategies before moving it on.`}
  }catch(e){m.className='bad';m.textContent='✖ '+e.message}}
(async()=>{try{const o=await j('/api/strategy-builder/options');OPS=o.operators;
  const all=[...new Set([].concat(...Object.values(o.catalogue)).filter(x=>!x.includes('<')&&!x.endsWith('_N')).concat(EXAMPLES))].sort();
  document.getElementById('feats').innerHTML=all.map(x=>`<option value="${esc(x)}">`).join('')}catch(e){}
  addCond('entry',{feature:'atip_score',op:'>=',value:60});addCond('entry',{feature:'close',op:'>',value_type:'feature',value:'sma_50'});
  addCond('entry',{feature:'regime',op:'not_in',value_type:'list',value:'BEAR, HIGH_RISK'});addCond('exit',{feature:'close',op:'<',value_type:'feature',value:'sma_20'});
  addCond('exit',{feature:'cri',op:'>',value:65})})();
</script></body></html>"""


def render(token: str) -> str:
    return PAGE.replace("__TOKEN__", json.dumps(token))
