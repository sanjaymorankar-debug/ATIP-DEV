"""
Main-dashboard additions (W28b), injected before </body> after the W28 assets:

  NS-05  News tab: "News weight by stock" -- the stocks whose weighted news score is furthest
         from neutral 50, with article count and effective weight; plus the measured source
         weights (collapsed).
  NS-06  News tab: "Corporate announcements" -- the last 3 days of NSE announcements with
         type, tone and, where the model read the document, guidance / key points / risks.

Loaded from /api/news/symbol-scores, /api/news/sources and /api/news/announcements when the
page opens. Rows with data-sym open the W26 stock panel.
"""

ASSETS = r"""
<script>
(function(){
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const num=(v,d=1)=>v==null||isNaN(v)?'—':Number(v).toFixed(d);
const sg=v=>v==null?'—':`<span style="color:${v>0?'#34d399':v<0?'#f87171':'#94a3b8'}">${v>0?'+':''}${Number(v).toFixed(2)}</span>`;
async function j(u){const r=await fetch(u);const t=await r.json();if(!r.ok)throw new Error(t.error||r.status);return t}
const tbl=(h,rows)=>`<table><thead><tr>${h.map(x=>`<th>${x}</th>`).join('')}</tr></thead><tbody>${rows.join('')||`<tr><td colspan=${h.length} style="color:#64748b;text-align:center;padding:12px">none</td></tr>`}</tbody></table>`;
function init(){
  const box=document.getElementById('news'); if(!box||document.getElementById('w28bgrid')) return;
  const grid=document.createElement('div'); grid.className='w28grid'; grid.id='w28bgrid';
  grid.innerHTML='<div class="w28card"><h4>News weight by stock <span class="w28m" id="w28bns"></span></h4><div class="w28m" style="margin-bottom:4px">50 = neutral; decayed by age, discounted for repeats, weighted by source and confidence (NS-05)</div><div id="w28bn" style="max-height:320px;overflow:auto"></div><details style="margin-top:6px"><summary class="w28m" style="cursor:pointer">Source weights</summary><div id="w28bsrc"></div></details></div>'+
    '<div class="w28card"><h4>Corporate announcements <span class="w28m">last 3 days (NS-06)</span></h4><div id="w28ba" style="max-height:360px;overflow:auto"></div></div>';
  const brief=document.getElementById('w28brief');
  if(brief&&brief.nextSibling) box.insertBefore(grid,brief.nextSibling); else box.prepend(grid);
  j('/api/news/symbol-scores?n=40').then(r=>{document.getElementById('w28bns').textContent=r.date||'';
    document.getElementById('w28bn').innerHTML=r.note?`<span class="w28m">${esc(r.note)}</span>`:tbl(['Symbol','Score','Articles','Weight','Sentiment'],r.rows.map(x=>`<tr data-sym="${esc(x.symbol)}" style="cursor:pointer"><td><b>${esc(x.symbol)}</b></td><td style="color:${x.score>55?'#34d399':x.score<45?'#f87171':'#e2e8f0'}">${num(x.score)}</td><td>${x.n_articles}</td><td>${num(x.effective_weight,2)}</td><td>${sg(x.mean_sentiment)}</td></tr>`))}).catch(e=>document.getElementById('w28bn').textContent=e.message);
  j('/api/news/sources').then(r=>{document.getElementById('w28bsrc').innerHTML=tbl(['Source','Articles','Dup %','Reaction hit','n','Weight'],r.quality.map(x=>`<tr><td>${esc(x.source)}</td><td>${x.articles}</td><td>${num((x.duplicate_share||0)*100)}</td><td>${x.reaction_hit_rate==null?'—':num(x.reaction_hit_rate*100)+'%'}</td><td>${x.reaction_n}</td><td><b>${num(x.weight,2)}</b></td></tr>`))}).catch(()=>{});
  j('/api/news/announcements?days=3&n=80').then(r=>{document.getElementById('w28ba').innerHTML=tbl(['Time','Symbol','Type','Subject','Tone'],r.map(a=>{
    const n=a.nlp?`<div class="w28m">Guidance <b>${esc(a.nlp.guidance)}</b>${(a.nlp.key_points||[]).length?' · '+a.nlp.key_points.slice(0,3).map(esc).join(' · '):''}${(a.nlp.risks||[]).length?'<br>Risks: '+a.nlp.risks.slice(0,3).map(esc).join('; '):''}</div>`:'';
    return `<tr data-sym="${esc(a.symbol)}" style="cursor:pointer"><td class="w28m">${esc(String(a.broadcast_at||'').slice(5,16))}</td><td><b>${esc(a.symbol)}</b></td><td>${esc(a.event_type)}</td><td>${a.attachment_url?`<a href="${esc(a.attachment_url)}" target="_blank" rel="noopener" style="color:#e2e8f0">${esc(a.subject)}</a>`:esc(a.subject)}${n}</td><td>${sg(a.tone)} <span class="w28m">${a.classifier==='claude'?'AI':'rule'}</span></td></tr>`}))}).catch(e=>document.getElementById('w28ba').textContent=e.message);
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',()=>setTimeout(init,0));else setTimeout(init,0);
})();
</script>
"""
