"""
Main-dashboard additions (W28), injected before </body> by dashboard/server.py --
kept out of the server.py f-string (no doubled braces):

  DB-06  News tab: the latest market brief at the top -- AI (Claude) or rule-based, always
         labelled, with tone, bullets, risks and stocks in focus, plus today's AI spend
         against the cap.
  DB-11  New tab "Lists & scans": Crash-risk 25 (CRI, 5-session change, main driver, held
         flag), Top SPI 25 (with its source), MSI series + components.
  SG-08  Same tab: the latest intraday scan run (hits by scan).

Everything loads from /api/news/*, /api/lists/*, /api/scans/intraday when the tab or the
page opens; nothing is computed in the browser beyond formatting. Rows with data-sym open
the W26 stock panel.
"""

ASSETS = r"""
<style>
.w28card{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:12px 14px;margin:0 0 12px}
.w28card h4{margin:0 0 6px;font-size:13px;color:#38bdf8}.w28m{color:#94a3b8;font-size:11px}
.w28tone{display:inline-block;padding:1px 8px;border-radius:9px;font-size:11px;margin-left:6px;background:#334155}
.w28tone.RISK_ON{background:#065f46}.w28tone.RISK_OFF{background:#7f1d1d}.w28tone.MIXED{background:#78350f}
.w28grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:12px}
.w28grid table{width:100%}
@media(max-width:600px){.w28grid{grid-template-columns:1fr}}
</style>
<script>
(function(){
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const num=(v,d=1)=>v==null||isNaN(v)?'—':Number(v).toFixed(d);
async function j(u){const r=await fetch(u);const t=await r.json();if(!r.ok)throw new Error(t.error||r.status);return t}
const tbl=(h,rows)=>`<table><thead><tr>${h.map(x=>`<th>${x}</th>`).join('')}</tr></thead><tbody>${rows.join('')||`<tr><td colspan=${h.length} style="color:#64748b;text-align:center;padding:12px">none</td></tr>`}</tbody></table>`;

async function brief(){
  const box=document.getElementById('news'); if(!box) return;
  const card=document.createElement('div'); card.className='w28card'; card.id='w28brief';
  card.innerHTML='<span class="w28m">Loading market brief…</span>'; box.prepend(card);
  try{
    const [s,u]=await Promise.all([j('/api/news/summary'),j('/api/news/ai-usage?days=1').catch(()=>null)]);
    if(s.note){card.innerHTML=`<h4>Market brief</h4><span class="w28m">${esc(s.note)}</span>`;return}
    const ai=s.classifier==='claude';
    card.innerHTML=`<h4>Market brief <span class="w28tone ${esc(s.tone)}">${esc(s.tone)}</span>
      <span class="w28m" style="margin-left:8px">${ai?'AI ('+esc(s.model)+')':'rule-based — not AI'} · ${s.article_count} headlines / ${s.window_hours}h · ${esc(String(s.created_at||'').slice(0,16))}</span></h4>
      <div style="font-weight:600;margin-bottom:6px">${esc(s.headline)}</div>
      <ul style="margin:0 0 6px 18px;padding:0">${(s.bullets||[]).map(b=>`<li>${esc(b.text)}</li>`).join('')}</ul>
      ${(s.key_risks||[]).length?`<div class="w28m">Risks: ${s.key_risks.map(esc).join(' · ')}</div>`:''}
      ${(s.stocks_in_focus||[]).length?`<div style="margin-top:6px">${s.stocks_in_focus.map(x=>`<span data-sym="${esc(x.symbol)}" style="cursor:pointer;display:inline-block;margin:2px 6px 2px 0;padding:2px 8px;border:1px solid #334155;border-radius:9px" title="${esc(x.why)}"><b>${esc(x.symbol)}</b> <span class="w28m">${esc(x.why).slice(0,60)}</span></span>`).join('')}</div>`:''}
      ${s.fallback_reason?`<div class="w28m" style="margin-top:6px">AI not used: ${esc(s.fallback_reason)}</div>`:''}
      ${u?`<div class="w28m" style="margin-top:4px">News AI ${u.enabled?'on':'off'} · today $${num(u.spent_today_usd,3)} of $${num(u.daily_cap_usd,2)} cap</div>`:''}`;
  }catch(e){card.innerHTML=`<h4>Market brief</h4><span class="w28m">${esc(e.message)}</span>`}
}

function addTab(){
  const tabs=document.querySelector('.tabs'); if(!tabs||document.getElementById('w28lists')) return;
  const t=document.createElement('div'); t.className='tab'; t.textContent='Lists & scans';
  t.onclick=function(){ if(window.showTab) showTab('w28lists',t); lists(); };
  tabs.appendChild(t);
  const last=[...document.querySelectorAll('.tc')].pop();
  const sec=document.createElement('div'); sec.id='w28lists'; sec.className='tc section';
  sec.innerHTML='<div class="w28grid"><div class="w28card"><h4>Crash risk 25 <span class="w28m" id="w28crs"></span></h4><div id="w28cr"></div></div><div class="w28card"><h4>Top SPI 25 <span class="w28m" id="w28spis"></span></h4><div id="w28spi"></div></div><div class="w28card"><h4>Market Sentiment Index (MSI)</h4><div id="w28msi"></div></div><div class="w28card"><h4>Intraday scans <span class="w28m" id="w28scs"></span></h4><div id="w28sc"></div></div></div>';
  (last&&last.parentNode?last.parentNode:document.body).insertBefore(sec,last?last.nextSibling:null);
}
let loaded=false;
async function lists(){
  if(loaded) return; loaded=true;
  j('/api/lists/crash-risk').then(r=>{document.getElementById('w28crs').textContent=r.session||'';
    document.getElementById('w28cr').innerHTML=tbl(['Symbol','CRI','Δ5s','Driver','ATIP','Signal'],r.rows.map(x=>`<tr data-sym="${esc(x.symbol)}" style="cursor:pointer"><td><b>${esc(x.symbol)}</b>${x.held?' <span title="LIVE holding">💼</span>':''}</td><td style="color:#f87171">${num(x.cri)}</td><td>${x.cri_change==null?'—':(x.cri_change>0?'+':'')+num(x.cri_change)}</td><td>${esc(x.driver||'—')}</td><td>${num(x.atip)}</td><td>${esc(x.signal)}</td></tr>`))}).catch(e=>document.getElementById('w28cr').textContent=e.message);
  j('/api/lists/spi').then(r=>{document.getElementById('w28spis').textContent=r.source||'';
    document.getElementById('w28spi').innerHTML=r.note?`<span class="w28m">${esc(r.note)}</span>`:tbl(['Symbol','SPI','FS / ATIP','Quarter','ROE','EPS YoY'],r.rows.map(x=>`<tr data-sym="${esc(x.symbol)}" style="cursor:pointer"><td><b>${esc(x.symbol)}</b></td><td>${num(x.spi)}</td><td>${num(x.fs??x.atip)}</td><td>${esc(x.quarter||'')}</td><td>${x.roe==null?'—':num(x.roe*100)+'%'}</td><td>${x.eps_growth_yoy==null?'—':num(x.eps_growth_yoy)+'%'}</td></tr>`))}).catch(e=>document.getElementById('w28spi').textContent=e.message);
  j('/api/lists/msi').then(r=>{const s=r.series||[];const v=s.filter(p=>p.msi!=null);let svg='';
    if(v.length>1){const ys=v.map(p=>p.msi),mn=Math.min(...ys),mx=Math.max(...ys),rg=mx-mn||1,w=380,h=90;
      svg=`<svg viewBox="0 0 ${w} ${h}" width="100%"><path d="${v.map((p,i)=>`${i?'L':'M'}${(i/(v.length-1)*(w-10)+5).toFixed(1)},${(h-8-(p.msi-mn)/rg*(h-16)).toFixed(1)}`).join('')}" fill="none" stroke="#38bdf8" stroke-width="1.6"/><text x="4" y="10" fill="#94a3b8" font-size="9">${num(mx)}</text><text x="4" y="${h-2}" fill="#94a3b8" font-size="9">${num(mn)}</text></svg>`}
    document.getElementById('w28msi').innerHTML=`<div class="w28m">latest ${esc(r.session||'')}: <b style="color:#e2e8f0">${num(v.length?v[v.length-1].msi:null)}</b> · ${v.length} sessions</div>${svg}`+
     (r.components.length?tbl(['Component','Value','Weight'],r.components.map(c=>`<tr><td>${esc(c.component)}</td><td>${num(c.value)}</td><td>${num(c.weight,2)}</td></tr>`)):`<div class="w28m">${esc(r.note||'')}</div>`)}).catch(e=>document.getElementById('w28msi').textContent=e.message);
  j('/api/scans/intraday').then(r=>{document.getElementById('w28scs').textContent=r.session?`${r.session} · run ${String(r.run_at||'').slice(11,16)}`:'';
    document.getElementById('w28sc').innerHTML=tbl(['Scan','Symbol','Price','Score','Rel vol','Detail'],(r.hits||[]).map(x=>`<tr data-sym="${esc(x.symbol)}" style="cursor:pointer"><td>${esc(x.scan)}</td><td><b>${esc(x.symbol)}</b></td><td>${num(x.price,2)}</td><td>${num(x.score)}</td><td>${num(x.details.rel_volume,2)}</td><td class="w28m">${esc(Object.entries(x.details).filter(([k])=>!['rel_volume','prev_atip','chg_pct'].includes(k)).map(([k,v])=>k+' '+v).join(' · '))}</td></tr>`))}).catch(e=>document.getElementById('w28sc').textContent=e.message);
}
function init(){brief();addTab()}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})();
</script>
"""
