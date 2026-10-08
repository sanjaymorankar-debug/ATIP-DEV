"""
W32 pages for the W9 SaaS routes (parked without pages until now):

    /app             a tenant's workspace: onboarding checklist, the tenant's own paper book,
                     e-mail verification (also completes ?verify=<token> links), notification
                     preferences + delivery log, devices / sessions (ENT-18: revoke one or all
                     others), broker credentials in the vault (ENT-06), consents, privacy requests
                     (export / delete), billing (plan, invoices, pay -- SANDBOX unless the owner
                     configured Razorpay: then a pay link, and autopay start / cancel), reports (run,
                     outputs, schedule)
    /admin/console   platform view: tenants with plan / subscription / dunning / usage /
                     onboarding / admin MFA, isolation check, privacy decisions, onboarding,
                     payments, dunning and billing-cycle runs, API-key limits

Same conventions as dashboard/enterprise_page.py: the session is an HttpOnly cookie, nothing
secret is stored in the page; every call goes through the authz middleware.
"""

from dashboard.enterprise_page import _page


def render_app():
    return _page("Workspace", """
<div id="vmsg" class="note"></div>
<h2>Getting started</h2><div id="ob"></div>
<h2>Paper book</h2><div id="bk"></div>
<h2>E-mail</h2><div id="em"></div><button onclick="sendVerify()">Send verification link</button> <span id="emm" class="muted"></span>
<h2>Notification preferences</h2><div id="np"></div>
<div><select id="pc"><option>risk</option><option>execution</option><option>alert</option><option>report</option><option>billing</option><option>security</option></select>
<label><input type="checkbox" id="ch_in_app" checked disabled> in-app</label><label><input type="checkbox" id="ch_email"> e-mail</label><label><input type="checkbox" id="ch_telegram"> Telegram</label>
<select id="pm"><option>immediate</option><option>digest</option></select> quiet <input id="qs" placeholder="22:00" size="5">-<input id="qe" placeholder="07:00" size="5"><button onclick="savePref()">Save</button></div>
<h2>Recent deliveries</h2><div id="dl" class="scroll"></div>
<h2>Devices &amp; sessions</h2><div id="ss"></div><button onclick="revokeOthers()">Sign out all other devices</button>
<h2>Broker credentials (encrypted vault)</h2><div id="vt"></div>
<div><select id="vb"></select><input id="vl" placeholder="label" size="10"><span id="vf"></span><button onclick="saveCred()">Store</button> <span class="muted">values are encrypted at rest and never shown again</span></div>
<h2>Billing</h2><div id="bl"></div>
<h2>Reports</h2><div id="rp"></div>
<h2>Privacy</h2><div id="pv"></div><button onclick="priv('export')">Request my data (export)</button><button style="background:#7f1d1d" onclick="priv('delete')">Request account deletion</button>
<h2>Consents</h2><div id="cs"></div>
""" + r"""<script>
let BROKERS={};
async function verifyFromLink(){const t=new URLSearchParams(location.search).get('verify');if(!t)return;
  try{await send('POST','/api/auth/verify-email',{token:t});vmsg.textContent='E-mail address verified.';history.replaceState(null,'','/app')}catch(e){vmsg.textContent='Verification failed: '+e.message}}
async function load(){
 const O=await j('/api/onboarding');const st=O.steps||{};
 ob.innerHTML=table(['Step','Done'],Object.entries(st).map(([k,v])=>`<tr><td>${esc(k.replace(/_/g,' '))}</td><td>${v?'✓':'—'}</td></tr>`))+(st.tenant_setup?'':`<div style="margin-top:6px">Starting paper cash <input id="sc" value="1000000" size="10"><button onclick="setup()">Open my paper book</button></div>`);
 try{const B=await j('/api/tenant/book');bk.innerHTML=`equity ₹${(B.equity||0).toLocaleString('en-IN')} · cash ₹${(B.cash||0).toLocaleString('en-IN')} · ${B.n_positions||0} positions · unrealised ₹${(B.unrealised||0).toLocaleString('en-IN')}`+table(['Symbol','Qty','Avg','Mark','Value','Unrealised'],(B.positions||[]).map(p=>`<tr><td>${esc(p.symbol)}</td><td>${p.qty}</td><td>${p.avg_price}</td><td>${p.mark??'—'}</td><td>${p.value??'—'}</td><td>${p.unrealised??'—'}</td></tr>`))}catch(e){bk.textContent=e.message}
 const m=await j('/api/auth/me');const u=m.user||{};em.innerHTML=`${esc(u.email||'no address on the profile')} · ${u.email_verified_at?'<b>verified</b>':'<span class="note">not verified</span>'}`;
 const P=await j('/api/account/notification-preferences');const pr=Array.isArray(P)?P:(P.preferences||[]);
 np.innerHTML=table(['Category','Channels','Mode','Quiet','Unsubscribed'],pr.map(x=>`<tr><td>${esc(x.category)}</td><td>${esc((x.channels||[]).join(', '))}</td><td>${esc(x.mode)}</td><td>${esc(x.quiet_start||'')}${x.quiet_end?'–'+esc(x.quiet_end):''}</td><td>${x.unsubscribed?'yes':''}</td></tr>`));
 const D=await j('/api/account/deliveries');const dd=Array.isArray(D)?D:(D.deliveries||[]);
 dl.innerHTML=table(['When','Channel','To','Subject','Status'],dd.slice(0,50).map(x=>`<tr><td>${esc(String(x.created_at).slice(0,16))}</td><td>${esc(x.channel)}</td><td class="muted">${esc(x.destination_masked||'')}</td><td>${esc(x.subject||'')}</td><td>${esc(x.status)}</td></tr>`));
 const S=await j('/api/account/sessions');const cur=(S.find?S.find(s=>s.current):null);
 ss.innerHTML=table(['Device','IP','Signed in','Last seen','Expires',''],S.map(s=>`<tr><td>${esc((s.user_agent||'').slice(0,60))}${s.current?' <b>(this device)</b>':''}</td><td>${esc(s.ip||'')}</td><td>${esc(String(s.created_at).slice(0,16))}</td><td>${esc(String(s.last_seen_at||'').slice(0,16))}</td><td>${esc(String(s.expires_at).slice(0,16))}</td><td>${s.current?'':`<button onclick="revoke('${s.session_id}')">Sign out</button>`}</td></tr>`));
 const V=await j('/api/account/vault');BROKERS=V.brokers||{};
 vt.innerHTML=table(['Broker','Label','Fields','Status','Updated','Used',''],(V.credentials||[]).map(c=>`<tr><td>${esc(c.broker)}</td><td>${esc(c.label||'')}</td><td class="muted">${esc((c.field_names||[]).join(', '))}</td><td>${esc(c.status)}</td><td>${esc(String(c.updated_at||'').slice(0,16))}</td><td>${c.access_count||0}×</td><td><button onclick="delCred('${c.credential_id}')">Delete</button></td></tr>`));
 if(!vb.options.length){vb.innerHTML=Object.keys(BROKERS).map(b=>`<option>${b}</option>`).join('');vb.onchange=fields;fields()}
 try{const Bl=await j('/api/billing');const s=Bl.subscription||{};
   bl.innerHTML=`plan <b>${esc(s.plan_id)}</b> · ${esc(s.status)}${s.dunning_state?' · dunning '+esc(s.dunning_state):''} · period ends ${esc(String(s.current_period_end||'').slice(0,10))}`+
   (Bl.provider==='razorpay'?`<div class="muted">Razorpay ${esc(Bl.provider_state||'')}${Bl.autopay?` · autopay ${esc(Bl.autopay.status)}${Bl.autopay.short_url?` · <a href="${esc(Bl.autopay.short_url)}" target="_blank" rel="noopener">authorise the mandate</a>`:''}`:''} <button onclick="autopay()">Start autopay</button><button onclick="autopayCancel()">Cancel autopay</button></div>`:'')+
   table(['Invoice','Period','Amount','Status','Due',''],(Bl.invoices||[]).map(i=>`<tr><td>${esc(i.invoice_id)}</td><td>${esc(i.period_start||'')}–${esc(i.period_end||'')}</td><td>${esc(i.amount)} ${esc(i.currency||'')}</td><td>${esc(i.status)}</td><td>${esc(i.due_date||'')}</td><td>${i.status==='OPEN'?`<button onclick="pay('${i.invoice_id}')">Pay (${esc(Bl.provider||'')})</button>`:''}</td></tr>`))+
   ((Bl.plans||[]).length?`<div>Change plan <select id="np2">${Bl.plans.map(p=>`<option value="${esc(p.plan_id)}">${esc(p.plan_id)} – ${esc(p.name||'')}</option>`).join('')}</select><button onclick="plan()">Change</button></div>`:'')}catch(e){bl.textContent=e.message}
 try{const R=await j('/api/account/reports');rp.innerHTML=table(['Report','Kind','Schedule','Last run',''],R.map(r=>`<tr><td>${esc(r.name)}</td><td>${esc(r.kind)}</td><td>${esc(r.schedule||'—')}</td><td>${esc(String(r.last_run_at||'—').slice(0,16))}</td><td><button onclick="runRep('${r.report_id}','html')">HTML</button><button onclick="runRep('${r.report_id}','csv')">CSV</button></td></tr>`))}catch(e){rp.textContent=e.message}
 const Pv=await j('/api/account/privacy');const pq=Array.isArray(Pv)?Pv:(Pv.requests||[]);pv.innerHTML=table(['Request','Kind','Status','When'],pq.map(q=>`<tr><td>${esc(q.request_id)}</td><td>${esc(q.kind)}</td><td>${esc(q.status)}</td><td>${esc(String(q.requested_at).slice(0,16))}</td></tr>`));
 const C=await j('/api/account/consents');const cc=Array.isArray(C)?C:(C.consents||[]);cs.innerHTML=table(['Document','Version','Accepted'],cc.map(x=>`<tr><td>${esc(x.document)}</td><td>${esc(x.version)}</td><td>${esc(String(x.accepted_at).slice(0,16))}</td></tr>`));
}
function fields(){vf.innerHTML=(BROKERS[vb.value]||[]).map(f=>`<input type="password" data-f="${f}" placeholder="${f}" size="12" autocomplete="off">`).join('')}
async function saveCred(){const f={};vf.querySelectorAll('input').forEach(i=>f[i.dataset.f]=i.value);try{await send('POST','/api/account/vault',{broker:vb.value,label:vl.value||'main',fields:f});vf.querySelectorAll('input').forEach(i=>i.value='');load()}catch(e){alert(e.message)}}
async function delCred(id){if(!confirm('Delete this credential?'))return;await j('/api/account/vault/'+id,{method:'DELETE'});load()}
async function setup(){try{await send('POST','/api/onboarding/setup',{starting_cash:Number(sc.value)});load()}catch(e){alert(e.message)}}
async function sendVerify(){try{const r=await send('POST','/api/account/email/verify',{});emm.textContent=r.status==='SANDBOX'?'link written to the local outbox (sandbox mail)':r.status}catch(e){emm.textContent=e.message}}
async function savePref(){const ch=['in_app'];if(ch_email.checked)ch.push('email');if(ch_telegram.checked)ch.push('telegram');
  try{await send('PUT','/api/account/notification-preferences',{category:pc.value,channels:ch,mode:pm.value,quiet_start:qs.value||null,quiet_end:qe.value||null});load()}catch(e){alert(e.message)}}
async function revoke(id){await j('/api/account/sessions/'+id,{method:'DELETE'});load()}
async function revokeOthers(){const S=await j('/api/account/sessions');for(const s of S){if(!s.current)await j('/api/account/sessions/'+s.session_id,{method:'DELETE'})}load()}
async function pay(id){try{const r=await send('POST',`/api/billing/invoices/${id}/pay`,{});if(r.pay_url)window.open(r.pay_url,'_blank','noopener');alert(r.status||'done');load()}catch(e){alert(e.message)}}
async function autopay(){try{const r=await send('POST','/api/billing/autopay',{});if(r.short_url)window.open(r.short_url,'_blank','noopener');load()}catch(e){alert(e.message)}}
async function autopayCancel(){if(!confirm('Cancel autopay at the end of the current billing cycle?'))return;try{await send('POST','/api/billing/autopay/cancel',{at_cycle_end:true});load()}catch(e){alert(e.message)}}
async function plan(){try{await send('POST','/api/billing/plan',{plan_id:np2.value});load()}catch(e){alert(e.message)}}
async function runRep(id,f){try{const r=await send('POST',`/api/reports/${id}/run`,{format:f});window.open(`/api/reports/${id}/outputs/${r.output_id}`,'_blank')}catch(e){alert(e.message)}}
async function priv(k){const reason=prompt(`Reason for the ${k} request (recorded):`);if(reason===null)return;if(k==='delete'&&!confirm('Deletion anonymises your account once an administrator approves it. Continue?'))return;try{await send('POST','/api/account/privacy',{kind:k,reason});load()}catch(e){alert(e.message)}}
verifyFromLink().then(load).catch(e=>document.body.insertAdjacentHTML('beforeend',`<p class="note" style="padding:16px">${esc(e.message)}</p>`));
</script>""")


def render_console():
    return _page("Admin console", """
<h2>Tenants</h2><div id="tn" class="scroll"></div>
<div><button onclick="runJob('/api/admin/dunning/run')">Run dunning now</button><button onclick="runJob('/api/admin/billing-cycle/run')">Run billing cycle now</button> <span id="jm" class="muted"></span></div>
<h2>Privacy requests</h2><div id="pq"></div>
<h2>Onboarding</h2><div id="on"></div>
<h2>Payments (SANDBOX unless the owner enabled a provider)</h2><div id="ps" class="muted"></div><div><button onclick="runJob('/api/admin/payments/reconcile')">Reconcile with the provider now</button></div><div id="py" class="scroll"></div>
<h2>Tenant isolation</h2><div id="is"></div>
""" + r"""<script>
async function load(){
 const C=await j('/api/admin/console');
 const PS=C.payments_status||{};ps.textContent=`provider ${PS.provider||C.payments_provider||''}: ${PS.state||''}${PS.reason?' — '+PS.reason:''}${PS.provider==='razorpay'?(PS.webhook_configured?' · webhook secret set':' · no webhook secret (settles by reconcile)'):''}`;
 tn.innerHTML=table(['Tenant','Status','Plan','Subscription','Dunning','Usage','Onboarded','Admins (MFA)'],(C.tenants||[]).map(t=>`<tr><td><b>${esc(t.tenant_id)}</b><br><span class="muted">${esc(t.name||'')}</span></td><td>${esc(t.status)}</td><td>${esc(t.plan||'')}</td><td>${esc(t.subscription||'')}</td><td>${esc(t.dunning||'')}</td><td class="muted">${esc(Object.entries(t.usage||{}).map(([k,v])=>k+' '+v).join(' · '))}</td><td>${t.onboarding_complete?'✓':'—'}</td><td>${t.admins??''} (${t.admins_with_mfa??0})</td></tr>`));
 try{const P=await j('/api/admin/privacy');pq.innerHTML=table(['Request','Tenant','User','Kind','Reason','Status',''],(P.requests||[]).map(r=>`<tr><td>${esc(r.request_id)}</td><td>${esc(r.tenant_id)}</td><td>${esc(r.user_id)}</td><td>${esc(r.kind)}</td><td>${esc(r.reason||'')}</td><td>${esc(r.status)}</td><td>${r.status==='PENDING'?`<button onclick="decide('${r.request_id}',true)">Approve</button><button style="background:#7f1d1d" onclick="decide('${r.request_id}',false)">Reject</button>`:''}</td></tr>`))}catch(e){pq.textContent=e.message}
 try{const O=await j('/api/admin/onboarding');const oo=Array.isArray(O)?O:(O.tenants||O.onboarding||[]);on.innerHTML=table(['Tenant','Complete','Steps'],oo.map(o=>`<tr><td>${esc(o.tenant_id)}</td><td>${o.complete?'✓':'—'}</td><td class="muted">${esc(Object.entries(o.steps||{}).filter(([k,v])=>!v).map(([k])=>k).join(', ')||'all done')}</td></tr>`))}catch(e){on.textContent=e.message}
 try{const Y=await j('/api/admin/payments');const yy=Array.isArray(Y)?Y:(Y.payments||[]);py.innerHTML=table(['Payment','Tenant','Invoice','Provider','Amount','Status','When'],yy.map(p=>`<tr><td>${esc(p.payment_id)}</td><td>${esc(p.tenant_id)}</td><td>${esc(p.invoice_id||'')}</td><td>${esc(p.provider)}</td><td>${esc(p.amount)} ${esc(p.currency||'')}</td><td>${esc(p.status)}</td><td>${esc(String(p.created_at).slice(0,16))}</td></tr>`))}catch(e){py.textContent=e.message}
 try{const I=await j('/api/admin/isolation');is.innerHTML=`${I.classified} of ${I.tables} tables classified`+(I.unclassified&&I.unclassified.length?`<div class="note">unclassified (fail closed for non-owner tenants): ${esc(I.unclassified.join(', '))}</div>`:'')}catch(e){is.textContent=e.message}
}
async function decide(id,ok){const reason=ok?'':prompt('Reason for rejecting:');if(!ok&&reason===null)return;try{await send('POST',`/api/admin/privacy/${id}/decision`,{approve:ok,reason});load()}catch(e){alert(e.message)}}
async function runJob(u){try{const r=await send('POST',u,{});jm.textContent=JSON.stringify(r).slice(0,200);load()}catch(e){jm.textContent=e.message}}
load().catch(e=>document.body.insertAdjacentHTML('beforeend',`<p class="note" style="padding:16px">${esc(e.message)}</p>`));
</script>""")
