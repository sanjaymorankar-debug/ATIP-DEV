"""
W38 pages: /compliance (SEC-05 checks, ENT-17 inventory + DSR SLA, ENT-14 register, DBS-05 readiness),
/privacy (the generated privacy notice) and /m, the mobile web app (ENT-09) with its PWA shell
(manifest, service worker, icon).

The mobile app is READ-ONLY and carries no token: the service worker caches the page for offline use,
and a cached page must not hold a credential. It can only be reached from a phone when the dashboard is
reachable from it -- on the owner's laptop that means a private tunnel (e.g. Tailscale) or the gated
ENT-07 exposure; nothing here opens a port.
"""

import html
import json

_STYLE = """
:root{--bg:#0f172a;--panel:#1e293b;--line:#334155;--text:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8;--good:#34d399;--bad:#f87171;--warn:#f59e0b}
*{box-sizing:border-box;margin:0;padding:0}body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:13px}
.top{background:var(--panel);padding:10px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px}
.top a{color:var(--accent);text-decoration:none;margin-left:12px}.wrap{padding:14px 16px;max-width:1300px}
.tabs{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:10px}.tab{padding:6px 12px;border-radius:6px;background:var(--panel);cursor:pointer;color:var(--muted)}
.tab.on{background:#2563eb;color:#fff}.pane{display:none}.pane.on{display:block}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin-bottom:10px}
h3{font-size:13px;color:var(--accent);margin-bottom:8px}.muted{color:var(--muted)}.ok{color:var(--good)}.bad{color:var(--bad)}.note{color:var(--warn)}
table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:5px 7px;border-bottom:1px solid #33415555;text-align:left;vertical-align:top}th{color:var(--muted)}
input,select{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:5px 7px;font-size:12.5px}
button{background:#2563eb;color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer;font-size:12.5px;margin:2px}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600}
.PASS{background:#064e3b;color:#a7f3d0}.WARN{background:#78350f;color:#fde68a}.FAIL{background:#7f1d1d;color:#fecaca}.NA{background:#334155;color:#cbd5e1}
.scroll{max-height:460px;overflow:auto}
"""

COMPLIANCE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ATIP Compliance</title><style>__STYLE__</style></head><body>
<div class="top"><div><b style="color:var(--accent)">📊 ATIP</b> <span class="muted">Compliance &amp; platform (W38)</span></div>
<div><a href="/privacy">Privacy notice</a><a href="/ops">Ops</a><a href="/">← Dashboard</a></div></div>
<div class="wrap"><div class="tabs" id="tabs"></div>
<div class="pane" id="p-chk"><div class="card"><h3>Compliance checks <button onclick="runNow()">Run now</button></h3>
<div class="muted">Technical controls, checked daily at 07:50 and on demand; a check that gets worse raises an alert. Not a legal opinion — see the Regulatory tab.</div>
<div id="chk" style="margin-top:8px"></div></div><div class="card"><h3>History</h3><div id="hist"></div></div></div>
<div class="pane" id="p-inv"><div class="card"><h3>Data inventory</h3><div class="muted">Every table, its class, whether it holds personal data and how long it is kept. Unclassified tables are flagged by the data-inventory check.</div><div id="inv" class="scroll" style="margin-top:8px"></div></div></div>
<div class="pane" id="p-dsr"><div class="card"><h3>Data-subject requests</h3><div id="dsr"></div></div></div>
<div class="pane" id="p-reg"><div class="card"><h3>Regulatory sign-off register (ENT-14)</h3>
<div class="note">Engineering's list of what a qualified professional must confirm. SIGNED_OFF / NOT_APPLICABLE need the reviewer and a reference. Turning on other users or live trading with open items in that gate fails the compliance check.</div>
<div id="reg" style="margin-top:8px"></div></div></div>
<div class="pane" id="p-pg"><div class="card"><h3>PostgreSQL readiness (DBS-05)</h3><div id="pg"></div></div></div>
</div>
<script>
const TOKEN=__TOKEN__;
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function j(u,o){const r=await fetch(u,o);const t=await r.json();if(!r.ok)throw new Error(t.error||t.detail||r.status);return t}
const post=(u,b)=>j(u,{method:'POST',headers:{'Content-Type':'application/json','X-ATIP-Token':TOKEN},body:JSON.stringify(b||{})});
const table=(h,rows)=>`<table><thead><tr>${h.map(x=>`<th>${x}</th>`).join('')}</tr></thead><tbody>${rows.join('')||`<tr><td colspan=${h.length} class="muted">none</td></tr>`}</tbody></table>`;
const pill=s=>`<span class="pill ${esc(s)}">${esc(s)}</span>`;
const TABS=[['chk','Checks'],['inv','Data inventory'],['dsr','Privacy requests'],['reg','Regulatory'],['pg','PostgreSQL']];const loaded={};
function show(id){document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.dataset.id===id));document.querySelectorAll('.pane').forEach(p=>p.classList.toggle('on',p.id==='p-'+id));
  if(!loaded[id]){loaded[id]=1;({chk,inv,dsr,reg,pg})[id]()}}
document.getElementById('tabs').innerHTML=TABS.map(([id,l])=>`<div class="tab" data-id="${id}" onclick="show('${id}')">${l}</div>`).join('');
async function chk(){const d=await j('/api/compliance');const el=document.getElementById('chk');
  if(!d.run){el.innerHTML='<span class="muted">No run yet.</span> <button onclick="runNow()">Run the checks</button>';return}
  el.innerHTML=`<div class="muted">Run ${esc(d.run.run_id)} at ${esc(String(d.run.at).slice(0,16))} (${esc(d.run.trigger)}): <b>${esc(d.run.summary)}</b></div>`+
   table(['Status','Check','Detail'],d.results.map(r=>`<tr><td>${pill(r.status)}</td><td><b>${esc(r.title)}</b></td><td>${esc(r.detail)}</td></tr>`));
  document.getElementById('hist').innerHTML=table(['At','Trigger','Pass','Warn','Fail'],d.history.map(h=>`<tr><td>${esc(String(h.at).slice(0,16))}</td><td>${esc(h.trigger)}</td><td class="ok">${h.passed}</td><td class="note">${h.warned}</td><td class="bad">${h.failed}</td></tr>`))}
async function runNow(){try{await post('/api/compliance/run');loaded.chk=0;chk()}catch(e){alert(e.message)}}
async function inv(){const d=await j('/api/compliance/inventory');document.getElementById('inv').innerHTML=table(['Table','Class','Personal','Retention'],
  d.tables.map(t=>`<tr><td>${esc(t.table)}</td><td>${t.class?esc(t.class):'<span class="bad">unclassified</span>'}</td><td>${t.personal==null?'—':t.personal?'<b>yes</b>':'no'}</td><td class="muted">${esc(t.retention)}</td></tr>`))}
async function dsr(){const d=await j('/api/compliance/dsr');const row=r=>`<tr><td>${esc(r.request_id)}</td><td>${esc(r.kind)}</td><td>${esc(r.requested_at.slice(0,10))}</td><td>${esc(r.due_at.slice(0,10))}</td><td>${r.days_left<0?`<span class="bad">${-r.days_left} day(s) overdue</span>`:r.days_left+' day(s)'}</td></tr>`;
  document.getElementById('dsr').innerHTML=`<div class="muted">Response SLA: ${d.sla_days} days · ${d.open} open</div>`+table(['Request','Kind','Requested','Due','Left'],[...d.overdue,...d.due_soon].map(row))}
async function reg(){const d=await j('/api/compliance/regulatory');document.getElementById('reg').innerHTML=table(['Gate','Item','Confirm','Status','Reviewer / reference','Update'],
  d.items.map(i=>`<tr><td class="muted">${esc(i.gate)}</td><td><b>${esc(i.item_id)}</b><br>${esc(i.title)}</td><td class="muted" style="max-width:420px">${esc(i.confirm)}</td><td>${esc(i.status)}${i.signed_at?`<br><span class="muted">${esc(String(i.signed_at).slice(0,10))}</span>`:''}</td><td>${esc(i.reviewer||'')}<br><span class="muted">${esc(i.reference||'')}</span></td>
   <td><select id="s-${esc(i.item_id)}">${d.statuses.map(s=>`<option ${s===i.status?'selected':''}>${s}</option>`).join('')}</select><br><input id="r-${esc(i.item_id)}" placeholder="reviewer" size="12"><input id="f-${esc(i.item_id)}" placeholder="reference" size="12"><br><button onclick="regSave('${esc(i.item_id)}')">Save</button></td></tr>`))}
async function regSave(id){const g=k=>document.getElementById(k+'-'+id).value.trim();try{await post('/api/compliance/regulatory/'+encodeURIComponent(id),{status:g('s'),reviewer:g('r'),reference:g('f')});reg()}catch(e){alert(e.message)}}
async function pg(){const d=await j('/api/platform/postgres');document.getElementById('pg').innerHTML=
  `<div>Runtime: <b>${esc(d.runtime)}</b></div><div style="margin:6px 0"><b>${d.translatable}</b> of ${d.statements} SQL statements translate automatically (<b>${d.pct_ready}%</b>); ${d.needs_change} need a hand change.</div>`+
  table(['File','Line','Construct','SQL'],d.items.map(i=>`<tr><td>${esc(i.file)}</td><td>${i.line}</td><td>${esc(i.kind)}</td><td class="muted">${esc(i.sql)}</td></tr>`))+
  '<div class="muted" style="margin-top:8px">Migrate a copy: <code>python tools/sqlite_to_postgres.py --source &lt;copy.db&gt; --target postgresql://... --execute</code> (docs/POSTGRESQL_MIGRATION.md)</div>'}
show('chk');
</script></body></html>"""


def render_compliance(token: str) -> str:
    return COMPLIANCE.replace("__STYLE__", _STYLE).replace("__TOKEN__", json.dumps(token))


def render_privacy(p: dict) -> str:
    e = html.escape
    draft = "" if p["approved"] else ('<div class="card" style="border-color:var(--warn)"><b class="note">DRAFT</b> '
                                      '<span class="muted">generated from the system\'s data inventory; it becomes the '
                                      'published notice once legally reviewed (saas.privacy_policy_approved).</span></div>')
    groups = "".join(
        f"<h3 style='margin-top:10px'>{e(k.title())} data</h3><table><tbody>"
        + "".join(f"<tr><td>{e(i['table'].replace('_', ' '))}</td><td class='muted'>{e(i['retention'])}</td></tr>"
                  for i in v) + "</tbody></table>" for k, v in sorted(p["personal_data"].items()))
    li = lambda xs: "<ul style='margin:6px 0 0 18px'>" + "".join(f"<li>{e(x)}</li>" for x in xs) + "</ul>"
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ATIP Privacy Notice</title><style>{_STYLE}
li{{margin:3px 0}}</style></head><body>
<div class="top"><div><b style="color:var(--accent)">📊 ATIP</b> <span class="muted">Privacy notice v{e(p['version'])}</span></div><div><a href="/">Home</a></div></div>
<div class="wrap" style="max-width:860px">{draft}
<div class="card"><h3>Who is responsible</h3><div>{e(p['controller'])}. Privacy questions: {e(p['contact'])}. Grievance officer: {e(p['grievance_officer'])}.</div></div>
<div class="card"><h3>What we use your data for</h3>{li(p['purposes'])}</div>
<div class="card"><h3>Personal data we hold, and for how long</h3>{groups or '<span class="muted">none recorded</span>'}
<div class="muted" style="margin-top:8px">Other data ({e(', '.join(p['not_personal']))}) is not about you personally.</div></div>
<div class="card"><h3>What we never do</h3>{li(p['never'])}</div>
<div class="card"><h3>Your rights</h3>{li(p['rights'])}</div>
</div></body></html>"""


# ── ENT-09: mobile web app ──────────────────────────────────────────────────
def mobile_summary(conn) -> dict:
    """Everything the phone screen shows, in one read-only request."""
    def one(q, *a):
        try:
            r = conn.execute(q, a).fetchone()
            return dict(r) if r else None
        except Exception:
            return None

    def many(q, *a):
        try:
            return [dict(r) for r in conn.execute(q, a).fetchall()]
        except Exception:
            return []
    d = (one("SELECT MAX(date) d FROM ai_scores") or {}).get("d")
    out = {"scores_date": str(d) if d else None,
           "market": one("SELECT date, mh_score, regime FROM market_health ORDER BY date DESC LIMIT 1"),
           "indexes": one("SELECT date, time, nifty50, nifty50_chg, india_vix, gift_nifty_chg FROM index_levels "
                          "ORDER BY date DESC, time DESC LIMIT 1"),
           "global": one("SELECT date, global_score, global_sentiment, sp500_chg FROM global_markets "
                         "ORDER BY date DESC LIMIT 1"),
           "buys": many("SELECT symbol, atip_score, cri FROM ai_scores WHERE date=? AND signal='BUY' "
                        "ORDER BY atip_score DESC LIMIT 5", str(d)) if d else [],
           "sells": many("SELECT symbol, atip_score, cri FROM ai_scores WHERE date=? AND signal='SELL' "
                         "ORDER BY cri DESC LIMIT 5", str(d)) if d else [],
           "tod": one("SELECT symbol, acs FROM ai_scores WHERE date=? AND is_tod=1 LIMIT 1", str(d)) if d else None,
           "pnl": many("SELECT date, env, equity, day_pnl, drawdown_pct FROM pnl_daily ORDER BY date DESC, env LIMIT 2"),
           "alerts": many("SELECT created_at, category, severity, title FROM alert_log ORDER BY id DESC LIMIT 6")}
    try:
        from pipeline.health import check_job_health
        out["pipeline_problems"] = [{"label": p.label, "kind": p.kind} for p in check_job_health(conn)]
    except Exception:
        out["pipeline_problems"] = []
    return out


MOBILE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0f172a"><meta name="apple-mobile-web-app-capable" content="yes">
<link rel="manifest" href="/manifest.webmanifest"><link rel="icon" href="/icon.svg"><link rel="apple-touch-icon" href="/icon.svg">
<title>ATIP</title><style>__STYLE__
body{font-size:15px;padding-bottom:24px}.wrap{padding:12px}.big{font-size:26px;font-weight:700}.row{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid #33415555}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.card{margin-bottom:8px}#off{display:none;background:#78350f;color:#fde68a;padding:6px 12px;font-size:12px}</style></head><body>
<div class="top"><b style="color:var(--accent)">📊 ATIP</b><span class="muted" id="upd">loading…</span></div>
<div id="off">Offline — showing the last data received.</div>
<div class="wrap" id="app"></div>
<script>
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const n=(v,d=2)=>v==null?'—':Number(v).toLocaleString('en-IN',{maximumFractionDigits:d});
const sg=v=>v==null?'':`<span class="${v>=0?'ok':'bad'}">${v>=0?'+':''}${n(v)}%</span>`;
async function load(){let d,cached=false;
  try{const r=await fetch('/api/mobile/summary',{cache:'no-store'});if(!r.ok)throw 0;d=await r.json();try{localStorage.setItem('atip_m',JSON.stringify({at:Date.now(),d}))}catch(e){}}
  catch(e){try{const c=JSON.parse(localStorage.getItem('atip_m'));d=c.d;cached=c.at}catch(_){}}
  document.getElementById('off').style.display=cached?'block':'none';
  if(!d){document.getElementById('app').innerHTML='<div class="card">ATIP is not reachable from this device.</div>';return}
  document.getElementById('upd').textContent=cached?('cached '+new Date(cached).toLocaleTimeString()):('scores '+(d.scores_date||'—'));
  const m=d.market||{},ix=d.indexes||{},g=d.global||{};
  const list=(xs,f)=>xs.length?xs.map(f).join(''):'<div class="muted">none</div>';
  document.getElementById('app').innerHTML=
   `<div class="grid"><div class="card"><div class="muted">Market health</div><div class="big">${n(m.mh_score,0)}</div><div>${esc(m.regime||'—')}</div></div>
    <div class="card"><div class="muted">Nifty 50</div><div class="big">${n(ix.nifty50,0)}</div><div>${sg(ix.nifty50_chg)} · VIX ${n(ix.india_vix,1)}</div></div></div>
    <div class="card"><div class="row"><span>Global</span><span>${n(g.global_score,0)} ${esc(g.global_sentiment||'')} · S&amp;P ${sg(g.sp500_chg)}</span></div>
    <div class="row"><span>GIFT Nifty</span><span>${sg(ix.gift_nifty_chg)}</span></div>${d.tod?`<div class="row"><span>🎯 Trade of the day</span><b>${esc(d.tod.symbol)}</b></div>`:''}</div>
    <div class="card"><h3>🟢 Buy candidates</h3>${list(d.buys,b=>`<div class="row"><b>${esc(b.symbol)}</b><span>ATIP ${n(b.atip_score,0)} · CRI ${n(b.cri,0)}</span></div>`)}</div>
    <div class="card"><h3>🔴 Exit / avoid</h3>${list(d.sells,s=>`<div class="row"><b>${esc(s.symbol)}</b><span>CRI ${n(s.cri,0)}</span></div>`)}</div>
    <div class="card"><h3>Portfolio</h3>${list(d.pnl,p=>`<div class="row"><span>${esc(p.env)} ${esc(String(p.date).slice(0,10))}</span><span>₹${n(p.equity,0)} · day ${p.day_pnl>=0?'<span class=ok>':'<span class=bad>'}₹${n(p.day_pnl,0)}</span></span></div>`)}</div>
    <div class="card"><h3>🩺 Pipeline</h3>${d.pipeline_problems.length?list(d.pipeline_problems,p=>`<div class="row bad"><span>${esc(p.kind)}</span><span>${esc(p.label)}</span></div>`):'<div class="ok">all monitored jobs ran</div>'}</div>
    <div class="card"><h3>🔔 Alerts</h3>${list(d.alerts,a=>`<div class="row"><span class="muted">${esc(String(a.created_at).slice(5,16))}</span><span>${esc(a.title)}</span></div>`)}</div>
    <div class="muted" style="text-align:center">Read-only · model output, not advice · <a href="/" style="color:var(--accent)">full dashboard</a></div>`}
load();setInterval(load,5*60*1000);
if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js').catch(()=>{})}
</script></body></html>"""


def render_mobile(token: str = "") -> str:
    return MOBILE.replace("__STYLE__", _STYLE)                 # token deliberately unused: see the module note


MANIFEST = json.dumps({
    "name": "ATIP — AI Trading Intelligence", "short_name": "ATIP", "start_url": "/m", "scope": "/",
    "display": "standalone", "background_color": "#0f172a", "theme_color": "#0f172a",
    "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}]})

# Shell: cache-first. Data: network-only (the page keeps its own last-good copy); nothing else is cached,
# so no API response with personal data lands in the cache storage.
SERVICE_WORKER = r"""const C='atip-shell-v1';const SHELL=['/m','/manifest.webmanifest','/icon.svg'];
self.addEventListener('install',e=>{e.waitUntil(caches.open(C).then(c=>c.addAll(SHELL)));self.skipWaiting()});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==C).map(k=>caches.delete(k)))));self.clients.claim()});
self.addEventListener('fetch',e=>{const u=new URL(e.request.url);if(e.request.method!=='GET'||u.origin!==location.origin)return;
  if(SHELL.includes(u.pathname)){e.respondWith(fetch(e.request).then(r=>{const k=r.clone();caches.open(C).then(c=>c.put(e.request,k));return r}).catch(()=>caches.match(e.request)))}});
"""

ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="96" fill="#0f172a"/>
<polyline points="88,360 200,250 280,300 424,150" fill="none" stroke="#38bdf8" stroke-width="40" stroke-linecap="round" stroke-linejoin="round"/>
<circle cx="424" cy="150" r="28" fill="#34d399"/></svg>"""
