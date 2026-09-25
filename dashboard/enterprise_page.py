"""
W7 pages: /login, /account (profile, password, notifications, API keys,
watchlists, alert rules, trading profile) and /admin (users, tenants, roles,
subscriptions, audit). Minimal; everything goes through /api/auth, /api/account
and /api/admin, authorized by the middleware. The session is an HttpOnly cookie,
so no token is stored in the page.
"""

_STYLE = """<style>
:root{--bg:#0f172a;--panel:#1e293b;--line:#334155;--text:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8;--bad:#dc2626;--warn:#f59e0b}
*{box-sizing:border-box;margin:0;padding:0}body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:13px}
.top{background:var(--panel);padding:10px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px}
.top a{color:var(--accent);text-decoration:none;margin-left:12px}.wrap{padding:14px 16px;max-width:1300px}
h2{font-size:13px;color:var(--accent);margin:16px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
table{width:100%;border-collapse:collapse;background:var(--panel);font-size:11.5px}th,td{padding:5px 7px;border-bottom:1px solid #1e293b55;text-align:left;vertical-align:top}th{color:var(--muted);background:var(--bg)}
input,select{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;margin:2px}
button{background:#2563eb;color:#fff;border:none;border-radius:6px;padding:5px 10px;cursor:pointer;font-size:12px;margin:2px}
.muted{color:var(--muted)}.note{color:var(--warn)}.scroll{max-height:340px;overflow:auto}.box{background:var(--panel);padding:16px;border-radius:8px;max-width:360px;margin:60px auto}
</style>"""
_JS = r"""<script>
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function j(u,o){const r=await fetch(u,Object.assign({credentials:'same-origin'},o||{}));const t=await r.json().catch(()=>({}));if(!r.ok)throw new Error(t.error||r.status);return t}
const send=(m,u,b)=>j(u,{method:m,headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});
const table=(h,rows)=>`<table><thead><tr>${h.map(x=>`<th>${x}</th>`).join('')}</tr></thead><tbody>${rows.join('')||`<tr><td colspan=${h.length} class="muted">none</td></tr>`}</tbody></table>`;
async function logout(){await send('POST','/api/auth/logout');location='/login'}
</script>"""
_TOP = """<div class="top"><div><b style="color:var(--accent)">📊 ATIP</b> <span class="muted">{title}</span></div>
<div><a href="/">Dashboard</a><a href="/account">Account</a><a href="/admin">Admin</a><a href="#" onclick="logout()">Sign out</a></div></div>"""


def _page(title, body):
    return (f"<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><title>ATIP {title}</title>{_STYLE}</head><body>"
            f"{_TOP.format(title=title)}<div class='wrap'>{body}</div>{_JS}</body></html>")


def render_login():
    return (f"<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><title>ATIP Sign in</title>{_STYLE}</head><body>"
            """<div class="box"><h2>Sign in to ATIP</h2><div id="st" class="muted"></div>
<input id="u" placeholder="username" autocomplete="username" style="width:100%"><br>
<input id="p" type="password" placeholder="password" autocomplete="current-password" style="width:100%"><br>
<input id="t" placeholder="tenant (optional)" style="width:100%"><br>
<input id="o" placeholder="MFA code (if enabled)" autocomplete="one-time-code" inputmode="numeric" style="width:100%"><br>
<button onclick="go()">Sign in</button> <span id="msg" class="note"></span></div>"""
            + _JS + r"""<script>
j('/api/enterprise/status').then(s=>{if(!s.enabled)document.getElementById('st').textContent='Enterprise sign-in is disabled; ATIP is in single-user mode.'});
async function go(){const m=document.getElementById('msg');m.textContent='';
 try{const r=await send('POST','/api/auth/login',{username:u.value,password:p.value,tenant_id:t.value||null,otp:o.value||null});
  const next=new URLSearchParams(location.search).get('next')||'/';location=r.must_change_password?'/account':(next.startsWith('/')?next:'/')}catch(e){m.textContent=e.message}}
</script></body></html>""")


def render_account():
    return _page("Account", """
<h2>Profile</h2><div id="me"></div>
<h2>Change password</h2><input id="op" type="password" placeholder="current"><input id="np" type="password" placeholder="new (12+ chars, letter + digit)"><button onclick="pw()">Change</button> <span id="pm" class="note"></span>
<h2>Notifications</h2><div id="nt" class="scroll"></div><button onclick="readAll()">Mark all read</button>
<h2>Trading profile (effective)</h2><div id="tp"></div>
<h2>API keys</h2><div id="ak"></div><input id="kn" placeholder="name"><input id="ks" placeholder="scopes, comma-separated" size="50"><button onclick="newKey()">Create</button> <span id="km" class="note"></span>
<h2>Watchlists</h2><div id="wl"></div><input id="wn" placeholder="name"><input id="wsy" placeholder="SYMBOLS, comma-separated" size="50"><button onclick="newWl()">Save</button>
<h2>Alert rules</h2><div id="al"></div><input id="as" placeholder="SYMBOL"><input id="af" placeholder="feature e.g. rsi_14"><select id="ao"><option>&lt;</option><option>&lt;=</option><option>&gt;</option><option>&gt;=</option></select><input id="av" placeholder="value" size="8"><button onclick="newAl()">Add</button>
""" + r"""<script>
async function load(){
 const m=await j('/api/auth/me');const u=m.user||{};
 document.getElementById('me').innerHTML=`<b>${esc(u.username)}</b> (${esc(u.display_name)}) · tenant <b>${esc(m.principal.tenant_id)}</b> · roles ${esc((m.principal.roles||[]).join(', '))}<br><span class="muted">${esc((m.permissions||[]).join(' · '))}</span>`+(u.must_change_password?'<div class="note">You must change your password.</div>':'');
 const tp=m.trading_profile||{};document.getElementById('tp').innerHTML=`trading ${tp.trading_enabled?'enabled':'<b>disabled</b>'} · modes ${esc((tp.allowed_modes||[]).join(','))} · brokers ${esc((tp.allowed_brokers||[]).join(','))} · max order ${tp.max_order_value??'—'} <span class="muted">(W4 risk limits still apply)</span>`;
 const N=await j('/api/account/notifications');document.getElementById('nt').innerHTML=table(['When','Category','Title','Body'],N.map(n=>`<tr style="${n.read_at?'':'font-weight:600'}"><td>${esc(n.created_at)}</td><td>${n.category}</td><td>${esc(n.title)}</td><td class="muted">${esc(n.body)}</td></tr>`));
 const K=await j('/api/account/api-keys');document.getElementById('ak').innerHTML=table(['Key','Name','Scopes','Expires','Last used','Status',''],K.map(k=>`<tr><td>${k.key_id}</td><td>${esc(k.name)}</td><td class="muted">${esc(k.scopes.join(', '))}</td><td>${esc(k.expires_at)}</td><td>${esc(k.last_used_at||'')}</td><td>${k.revoked_at?'revoked':'active'}</td><td>${k.revoked_at?'':`<button onclick="rk('${k.key_id}')">Revoke</button>`}</td></tr>`));
 const W=await j('/api/account/watchlists');document.getElementById('wl').innerHTML=table(['Name','Symbols',''],W.map(w=>`<tr><td>${esc(w.name)}</td><td>${esc((w.symbols||[]).join(', '))}</td><td><button onclick="del('watchlists','${w.watchlist_id}')">Delete</button></td></tr>`));
 const A=await j('/api/account/alerts');document.getElementById('al').innerHTML=table(['Rule','Condition','Last value','Last triggered',''],A.map(a=>`<tr><td>${esc(a.name)}</td><td>${esc(a.symbol)} ${esc(a.feature)} ${esc(a.op)} ${a.value}</td><td>${a.last_value??'—'}</td><td>${esc(a.last_triggered_at||'')}</td><td><button onclick="del('alerts','${a.rule_id}')">Delete</button></td></tr>`));
}
async function pw(){try{await send('POST','/api/auth/password',{old_password:op.value,new_password:np.value});location='/login'}catch(e){pm.textContent=e.message}}
async function readAll(){await send('POST','/api/account/notifications/read',{});load()}
async function newKey(){try{const r=await send('POST','/api/account/api-keys',{name:kn.value,scopes:ks.value.split(',').map(s=>s.trim()).filter(Boolean)});km.textContent='Copy now (shown once): '+r.api_key;load()}catch(e){km.textContent=e.message}}
async function rk(id){await j('/api/account/api-keys/'+id,{method:'DELETE'});load()}
async function newWl(){try{await send('POST','/api/account/watchlists',{name:wn.value,symbols:wsy.value.split(',')});load()}catch(e){alert(e.message)}}
async function newAl(){try{await send('POST','/api/account/alerts',{symbol:as.value,feature:af.value,op:ao.value,value:Number(av.value)});load()}catch(e){alert(e.message)}}
async function del(k,id){await j(`/api/account/${k}/${id}`,{method:'DELETE'});load()}
load().catch(e=>document.body.insertAdjacentHTML('beforeend',`<p class="note" style="padding:16px">${esc(e.message)}</p>`));
</script>""")


def render_admin():
    return _page("Admin", """
<h2>Users</h2><div id="us" class="scroll"></div>
<div><input id="nu" placeholder="username"><input id="npw" type="password" placeholder="initial password"><input id="nr" placeholder="roles e.g. TRADER,VIEWER"><input id="nt2" placeholder="tenant (default: yours)"><button onclick="addUser()">Create user</button> <span id="um" class="note"></span></div>
<h2>Tenants</h2><div id="tn" class="scroll"></div>
<h2>Roles &amp; permissions</h2><div id="rl" class="scroll"></div>
<h2>Plans</h2><div id="pl"></div>
<h2>Audit (latest)</h2><div id="au" class="scroll"></div>
""" + r"""<script>
async function load(){
 const U=await j('/api/admin/users');document.getElementById('us').innerHTML=table(['User','Status','Memberships','Last login',''],U.map(u=>`<tr><td><b>${esc(u.username)}</b><br><span class="muted">${u.user_id}</span></td><td>${u.status}</td><td>${esc(Object.entries(u.memberships||{}).map(([t,r])=>t+': '+r.join(',')).join(' | '))}</td><td>${esc(u.last_login_at||'')}</td><td>${u.status!=='ACTIVE'?`<button onclick="st('${u.user_id}','ACTIVE')">Activate</button>`:`<button onclick="st('${u.user_id}','DISABLED')">Disable</button>`}<button onclick="rs('${u.user_id}')">Reset password</button></td></tr>`));
 const T=await j('/api/admin/tenants');document.getElementById('tn').innerHTML=table(['Tenant','Status','Plan','Usage','Limits'],T.map(t=>`<tr><td><b>${esc(t.tenant_id)}</b> ${esc(t.name)}</td><td>${t.status}</td><td>${esc((t.subscription||{}).plan_id||'—')} ${esc((t.subscription||{}).status||'')}</td><td class="muted">${esc(JSON.stringify(t.usage))}</td><td class="muted">${esc(JSON.stringify(t.effective_limits))}</td></tr>`));
 const R=await j('/api/admin/roles');document.getElementById('rl').innerHTML=table(['Role','Description','Permissions'],Object.entries(R).map(([r,v])=>`<tr><td><b>${r}</b></td><td>${esc(v.description)}</td><td class="muted">${esc(v.permissions.join(' · '))}</td></tr>`));
 const P=await j('/api/admin/plans');document.getElementById('pl').innerHTML=table(['Plan','Price / month','Limits','Features'],P.map(p=>`<tr><td>${p.plan_id}</td><td>${p.price_month??'<span class="muted">not set</span>'}</td><td class="muted">${esc(JSON.stringify(p.limits))}</td><td class="muted">${esc(p.features.join(', '))}</td></tr>`))+'<p class="muted">No payment gateway: invoices are DRAFT records only.</p>';
 const A=await j('/api/admin/audit?limit=100');document.getElementById('au').innerHTML=table(['When','Tenant','Actor','Action','Path','Status'],A.map(a=>`<tr><td>${esc(a.at)}</td><td>${esc(a.tenant_id)}</td><td>${esc(a.actor)}</td><td>${esc(a.action)}</td><td class="muted">${esc((a.method||'')+' '+(a.path||a.resource||''))}</td><td>${a.status_code??''}</td></tr>`));
}
async function addUser(){try{await send('POST','/api/admin/users',{username:nu.value,password:npw.value,roles:nr.value.split(',').map(s=>s.trim()).filter(Boolean),tenant_id:nt2.value||null});um.textContent='created (must change password at first sign-in)';load()}catch(e){um.textContent=e.message}}
async function st(id,s){try{await send('PUT','/api/admin/users/'+id,{status:s});load()}catch(e){alert(e.message)}}
async function rs(id){try{const r=await send('POST',`/api/admin/users/${id}/password-reset`);prompt('One-time reset token (30 min) — give it to the user:',r.reset_token)}catch(e){alert(e.message)}}
load().catch(e=>document.body.insertAdjacentHTML('beforeend',`<p class="note" style="padding:16px">${esc(e.message)}</p>`));
</script>""")
