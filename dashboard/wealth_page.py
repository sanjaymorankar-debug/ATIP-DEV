"""
The /wealth page (W11-W17): the Investor-mode home. One tab per wave, all read
from /api/wealth/*; nothing is computed in the page. Tabs are registered in
TABS (id, title, html, js) so each wave adds its own without touching the rest.
"""

import json

HEAD = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ATIP Wealth</title>
<style>
:root{--bg:#0f172a;--panel:#1e293b;--line:#334155;--text:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8;--good:#10b981;--bad:#ef4444;--warn:#f59e0b}
*{box-sizing:border-box;margin:0;padding:0}body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:13px}
.top{background:var(--panel);padding:10px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px}
.top a{color:var(--accent);text-decoration:none;margin-left:12px}.wrap{padding:14px 16px;max-width:1400px}
.tabs{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:12px;border-bottom:1px solid var(--line)}
.tabs button{background:none;border:none;color:var(--muted);padding:8px 12px;cursor:pointer;font-size:13px;border-bottom:2px solid transparent}
.tabs button.on{color:var(--accent);border-bottom-color:var(--accent)}
.tab{display:none}.tab.on{display:block}
h2{font-size:13px;color:var(--accent);margin:16px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
h3{font-size:12px;color:var(--muted);margin:10px 0 6px;text-transform:uppercase;letter-spacing:.04em}
table{width:100%;border-collapse:collapse;background:var(--panel);font-size:12px}
th,td{padding:5px 7px;border-bottom:1px solid #33415566;text-align:left;vertical-align:top}th{color:var(--muted);background:var(--bg);white-space:nowrap}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.muted{color:var(--muted)}.warn{color:var(--warn)}.bad{color:var(--bad)}.good{color:var(--good)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px}
.card .k{font-size:11px;color:var(--muted)}.card .v{font-size:20px;font-weight:600;margin-top:2px;font-variant-numeric:tabular-nums}
.bar{height:6px;background:#334155;border-radius:3px;margin-top:6px;overflow:hidden}.bar i{display:block;height:100%;background:var(--accent)}
.pill{display:inline-block;padding:1px 8px;border-radius:9px;font-size:11px;background:#334155}
.pill.warning{background:#78350f}.pill.info{background:#1e3a5f}.pill.HIGH{background:#7f1d1d}.pill.MODERATE{background:#78350f}.pill.LOW{background:#065f46}
.pill.CURRENT,.pill.OK,.pill.ON_TRACK{background:#065f46}.pill.STALE,.pill.AT_RISK{background:#78350f}.pill.OFF_TRACK{background:#7f1d1d}
form .q{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:8px 10px;margin-bottom:6px}
form .q label{display:block;margin-bottom:4px}form .q .help{font-size:11px;color:var(--muted)}
input,select,button,textarea{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12.5px}
button.primary{background:#0369a1;border-color:#0369a1;cursor:pointer}button{cursor:pointer}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.tw{overflow-x:auto;max-width:100%}.scroll{max-height:420px;overflow:auto}
.disc{font-size:11px;color:var(--muted);margin-top:18px;border-top:1px solid var(--line);padding-top:8px}
.msg{padding:6px 10px;border-radius:6px;margin:6px 0;background:#1e3a5f}.msg.e{background:#7f1d1d}
@media(max-width:640px){.wrap{padding:10px}.top a{margin-left:8px}}
</style></head><body>
<div class="top"><div><b style="color:var(--accent)">📊 ATIP</b> <span class="muted">Investor mode</span> <span id="ld" class="muted" style="font-size:11px"></span></div>
<div><a href="/">Trader dashboard</a><a href="/strategies">Strategies</a><a href="/trading">Trading</a><a href="/backtests">Backtests</a></div></div>
<div class="wrap"><div id="uat_banner"></div><div class="tabs" id="tabs"></div>
"""

COMMON_JS = r"""
const TOKEN=__TOKEN__;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=(v,d=2)=>v==null||isNaN(v)?'—':Number(v).toLocaleString('en-IN',{minimumFractionDigits:d,maximumFractionDigits:d});
const inr=v=>v==null?'—':'₹'+num(v,0);const pct=(v,d=1)=>v==null?'—':num(v,d)+'%';
const pill=(s,c)=>`<span class="pill ${esc(c||s)}">${esc(s)}</span>`;
async function j(u,o){const r=await fetch(u,o);let t={};try{t=await r.json()}catch(e){}if(!r.ok){const e=new Error(t.error||t.detail||('HTTP '+r.status));e.status=r.status;throw e}return t}
const post=(u,b)=>j(u,{method:'POST',headers:{'Content-Type':'application/json','X-ATIP-Token':TOKEN},body:JSON.stringify(b||{})});
const put=(u,b)=>j(u,{method:'PUT',headers:{'Content-Type':'application/json','X-ATIP-Token':TOKEN},body:JSON.stringify(b||{})});
const del=u=>j(u,{method:'DELETE',headers:{'X-ATIP-Token':TOKEN}});
const table=(h,rows)=>`<div class="tw"><table><thead><tr>${h.map(x=>`<th${x.startsWith('#')?' class="n"':''}>${x.replace(/^#/,'')}</th>`).join('')}</tr></thead><tbody>${rows.join('')||`<tr><td colspan=${h.length} class="muted">none</td></tr>`}</tbody></table></div>`;
const card=(k,v,sub,bar)=>`<div class="card"><div class="k">${k}</div><div class="v">${v}</div>${sub?`<div class="muted" style="font-size:11px">${sub}</div>`:''}${bar!=null?`<div class="bar"><i style="width:${Math.max(0,Math.min(100,bar))}%"></i></div>`:''}</div>`;
const msg=(el,t,e)=>{document.getElementById(el).innerHTML=`<div class="msg${e?' e':''}">${esc(t)}</div>`};
const LOADERS={};let CUR=null;
function show(id){CUR=id;document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.id==='t_'+id));
 document.querySelectorAll('#tabs button').forEach(b=>b.classList.toggle('on',b.dataset.t===id));
 try{localStorage.setItem('atip_wealth_tab',id)}catch(e){}
 const ld=document.getElementById('ld');if(LOADERS[id]){ld.textContent='loading…';LOADERS[id]().catch(e=>{console.error(e);ld.textContent='could not load: '+e.message}).then(()=>{if(ld.textContent==='loading…')ld.textContent=''})}}
"""

# ── W11 Investor DNA ──────────────────────────────────────────────────────
DNA_HTML = r"""
<div id="dna_msg"></div>
<div id="dna_view"></div>
<h2>Questionnaire <span class="muted" id="dna_qv"></span></h2>
<p class="muted">Answer once; re-take it whenever your situation changes. Every answer and score is kept as a version.</p>
<form id="dna_form" onsubmit="return false"></form>
<div class="row" style="margin:8px 0"><button onclick="dnaPreview()">Preview</button><button class="primary" onclick="dnaSave()">Save profile</button><button onclick="dnaRefresh()" title="re-score your saved answers; risk requirement then comes from your goals">Refresh from goals</button></div>
<h2>History</h2><div id="dna_hist"></div>
"""

DNA_JS = r"""
let DNAQ=null;
function dnaRender(p){
  if(!p){document.getElementById('dna_view').innerHTML='<div class="msg">No investor profile yet. Answer the questionnaire below.</div>';return}
  const s=p.scores;const flags=(p.flags||[]).map(f=>`<div>${pill(f.code,f.severity)} ${esc(f.message)}</div>`).join('')||'<span class="muted">none</span>';
  const bias=Object.entries(p.biases||{}).map(([k,b])=>`<tr><td>${esc(k.replace(/_/g,' '))}</td><td class="n">${num(b.score,0)}</td><td>${pill(b.level)}</td><td class="muted">${esc(b.note)}</td></tr>`);
  const ex=(name,rows)=>`<h3>${name}</h3>`+table(['Factor','Input','#Score','#Weight','#Contribution'],(rows||[]).map(r=>`<tr><td>${esc(r.factor)}</td><td>${esc(r.input)}</td><td class="n">${num(r.score,1)}</td><td class="n">${num(r.weight,3)}</td><td class="n">${num(r.contribution,2)}</td></tr>`));
  const ob=p.observed_behaviour||{};
  document.getElementById('dna_view').innerHTML=`
  <h2>Your Investor DNA ${p.status?pill(p.status):''} <span class="muted">v${p.version??'preview'} · ${esc(p.methodology_version)}${p.created_at?' · '+esc(String(p.created_at).slice(0,16)):''}</span></h2>
  <div class="grid">${card('Risk profile',esc(p.band.replace(/_/g,' ')),'score '+num(s.risk_score,0)+' / 100',s.risk_score)}
  ${card('Risk capacity',num(s.risk_capacity,0),'what your finances can absorb',s.risk_capacity)}
  ${card('Risk tolerance',num(s.risk_tolerance,0),'what you are willing to take',s.risk_tolerance)}
  ${card('Risk requirement',num(s.risk_requirement,0),p.requirement_source==='GOALS'?'from your goals':'from your target return',s.risk_requirement)}
  ${card('Experience',num(s.experience,0),'',s.experience)}
  ${card('Horizon',num(p.horizon.years,0)+' yrs',p.horizon.bucket)}
  ${card('Personality',esc(p.personality.archetype.replace(/_/g,' ')),p.personality.style+' · '+p.personality.mode+' mode')}
  ${card('Monthly surplus',inr(p.finances.monthly_surplus),'savings rate '+pct(p.finances.savings_rate_pct))}</div>
  <h3>Flags</h3>${flags}
  <h3>Behavioural profile</h3>${table(['Bias','#Score','Level','What it means'],bias)}
  <h3>Observed behaviour (paper book)</h3><div class="muted">${esc(Object.entries(ob).map(([k,v])=>k+': '+v).join(' · '))}</div>
  <h2>How the scores were built</h2><div class="muted">Risk score = ${esc(p.explanation.risk_score)}</div>
  ${ex('Risk capacity',p.explanation.risk_capacity)}${ex('Risk tolerance',p.explanation.risk_tolerance)}${ex('Risk requirement',p.explanation.risk_requirement)}${ex('Experience',p.explanation.experience)}`;
}
function dnaForm(answers){
  const f=document.getElementById('dna_form');let sec='';let h='';
  for(const q of DNAQ.questions){
    if(q.section!==sec){sec=q.section;h+=`<h3>${esc(sec)}</h3>`}
    const v=answers?answers[q.id]:undefined;
    let inp;if(q.type==='number')inp=`<input type="number" name="${q.id}" min="${q.min??''}" max="${q.max??''}" step="any" value="${v??''}" style="width:180px">`;
    else inp=`<select name="${q.id}"><option value="">— choose —</option>${q.options.map(o=>`<option value="${esc(o.value)}"${String(v)===o.value?' selected':''}>${esc(o.label)}</option>`).join('')}</select>`;
    h+=`<div class="q"><label>${esc(q.text)}</label>${inp}${q.help?`<div class="help">${esc(q.help)}</div>`:''}</div>`;
  }
  f.innerHTML=h;
}
function dnaAnswers(){const a={};for(const q of DNAQ.questions){const el=document.querySelector(`#dna_form [name="${q.id}"]`);if(el&&el.value!=='')a[q.id]=q.type==='number'?Number(el.value):el.value}return a}
async function dnaPreview(){try{const p=await post('/api/wealth/dna/preview',{answers:dnaAnswers()});dnaRender(p);msg('dna_msg','Preview only — not saved.')}catch(e){msg('dna_msg',e.message,1)}}
async function dnaSave(){try{const p=await post('/api/wealth/dna',{answers:dnaAnswers()});dnaRender(p);msg('dna_msg','Saved as version '+p.version+'.');dnaHist()}catch(e){msg('dna_msg',e.message,1)}}
async function dnaRefresh(){try{const p=await post('/api/wealth/dna/refresh');dnaRender(p);msg('dna_msg','Re-scored as version '+p.version+' (risk requirement from '+p.requirement_source+').');dnaHist()}catch(e){msg('dna_msg',e.message,1)}}
async function dnaHist(){const H=await j('/api/wealth/dna/history');document.getElementById('dna_hist').innerHTML=table(['Version','Band','#Risk score','Questionnaire','Methodology','Saved'],H.map(h=>`<tr><td>${h.version}</td><td>${esc(h.band)}</td><td class="n">${num(h.risk_score,1)}</td><td>${esc(h.questionnaire_version)}</td><td>${esc(h.methodology_version)}</td><td>${esc(String(h.created_at).slice(0,16))}</td></tr>`))}
LOADERS.dna=async()=>{
  if(!DNAQ){DNAQ=await j('/api/wealth/dna/questionnaire');document.getElementById('dna_qv').textContent=DNAQ.version+' · '+DNAQ.methodology}
  let p=null;try{p=await j('/api/wealth/dna')}catch(e){if(e.status!==404)msg('dna_msg',e.message,1)}
  dnaRender(p);dnaForm(p?p.answers:null);dnaHist();
};
"""

FEEDBACK_JS = r"""
function fbOpen(on){document.getElementById('fb_box').style.display=on===false?'none':'block';document.getElementById('fb_res').textContent=''}
async function fbSend(){const b={page:CUR,category:document.getElementById('fb_cat').value,severity:document.getElementById('fb_sev').value,message:document.getElementById('fb_msg').value,context:{url:location.pathname,tab:CUR,width:window.innerWidth}};
 try{const r=await post('/api/wealth/feedback',b);document.getElementById('fb_res').textContent='Thanks, recorded ('+r.feedback_id+').';document.getElementById('fb_msg').value=''}catch(e){document.getElementById('fb_res').textContent=e.message}}
j('/api/wealth/uat/context').then(u=>{if(u.active)document.getElementById('uat_banner').innerHTML=`<div class="msg" style="background:#78350f">UAT persona: <b>${esc(u.owner.owner_id)}</b> (tenant ${esc(u.owner.tenant_id)}). This is test data, not your own. Remove wealth.uat_owner from config.json to return to your own data.</div>`}).catch(()=>{});
"""


def _tabs():
    """(id, title, html, js); later waves add a module under dashboard/wealth_ui/."""
    from dashboard.wealth_ui import (advisor_tab, allocation_tab, goals_tab, overview_tab, performance_tab,
                                     rebalance_tab, wealth_tab)
    return [overview_tab.TAB, wealth_tab.TAB, goals_tab.TAB, allocation_tab.TAB, rebalance_tab.TAB, performance_tab.TAB, advisor_tab.TAB,
            ("dna", "Investor DNA", DNA_HTML, DNA_JS)]

FEEDBACK = r"""
<div style="position:fixed;right:14px;bottom:14px;z-index:5"><button class="primary" onclick="fbOpen()">Feedback</button></div>
<div id="fb_box" style="display:none;position:fixed;right:14px;bottom:56px;width:min(360px,calc(100vw - 28px));z-index:6" class="card">
<div class="k">Report a problem or idea about this tab</div>
<div class="row" style="margin:6px 0"><select id="fb_cat"><option>BUG</option><option>UX</option><option>DATA</option><option>METHODOLOGY</option><option>IDEA</option></select>
<select id="fb_sev"><option value="P3">P3 minor</option><option value="P2">P2 annoying</option><option value="P1">P1 wrong result</option><option value="P0">P0 blocker / unsafe</option></select></div>
<textarea id="fb_msg" rows="4" style="width:100%" placeholder="What happened, and what did you expect?"></textarea>
<div class="row" style="margin-top:6px"><button class="primary" onclick="fbSend()">Send</button><button onclick="fbOpen(false)">Close</button><span id="fb_res" class="muted"></span></div></div>
"""

FOOT = r"""
<p class="disc">⚠️ ATIP is for personal informational use only and is not SEBI-registered investment advice. Every figure is a model output from the data and assumptions shown. Nothing on this page places an order. NPS, FDs, tax, insurance and mutual funds are outside ATIP's scope.</p>
</div>
"""


def render(token: str) -> str:
    TABS = _tabs()
    tabs ="".join(f'<button data-t="{t[0]}" onclick="show(\'{t[0]}\')">{t[1]}</button>' for t in TABS)
    bodies = "".join(f'<div class="tab" id="t_{t[0]}">{t[2]}</div>' for t in TABS)
    js = COMMON_JS + "".join(t[3] for t in TABS)
    first = TABS[0][0]
    boot = (f"document.getElementById('tabs').innerHTML={json.dumps(tabs)};"
            f"let _t='{first}';try{{_t=localStorage.getItem('atip_wealth_tab')||_t}}catch(e){{}}"
            f"if(!document.getElementById('t_'+_t))_t='{first}';show(_t);")
    html = HEAD + bodies + FEEDBACK + FOOT + "<script>" + js + FEEDBACK_JS + boot + "</script></body></html>"
    return html.replace("__TOKEN__", json.dumps(token))
