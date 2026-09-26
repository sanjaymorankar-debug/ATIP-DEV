"""W14 tab: strategic + tactical asset allocation with signals, constraints and risk guard."""

HTML = r"""
<div id="aal_msg"></div>
<div class="row"><button class="primary" onclick="aalRun()">Compute &amp; save allocation</button><button onclick="aalPreview()">Preview</button><span class="muted" id="aal_src"></span></div>
<div id="aal_cards" class="grid" style="margin-top:8px"></div>
<h2>Target allocation</h2><div id="aal_target"></div>
<h2>Market intelligence signals (tactical)</h2><div id="aal_sig"></div>
<h2>How the target was built</h2><div id="aal_steps"></div>
<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(320px,1fr))"><div><h3>Equity split</h3><div id="aal_eq"></div></div><div><h3>Sector views (informational)</h3><div id="aal_sec"></div></div></div>
<h2>Your allocation policy</h2>
<p class="muted">Tighten the bounds for any class, exclude classes, or switch tactical tilts off. Bounds apply inside the band's own limits.</p>
<div class="row"><select id="p_cls"></select><input id="p_min" type="number" placeholder="min %" style="width:80px"><input id="p_max" type="number" placeholder="max %" style="width:80px"><button onclick="aalBound()">Set bound</button>
<label><input type="checkbox" id="p_tac" checked onchange="aalPol()"> tactical tilts</label><input id="p_tilt" type="number" step="any" placeholder="max tilt pts" style="width:100px" onchange="aalPol()"></div>
<div id="aal_pol" class="muted" style="margin-top:6px"></div>
<h2>History</h2><div id="aal_hist"></div>
"""

JS = r"""
const ACLS=["EQUITY","INTL_EQUITY","BONDS","GOLD","SILVER","CASH"];let APOL=null;
function aalRender(r,src){
 document.getElementById('aal_src').textContent=src||'';
 const I=r.investor,S=r.stats.target,G=r.risk_guard;
 document.getElementById('aal_cards').innerHTML=card('Band used',esc(I.band.replace(/_/g,' ')),'risk score '+num(I.effective_risk_score,1)+(I.effective_risk_score<I.risk_score?' (DNA '+num(I.risk_score,1)+', horizon cap)':''))+card('Horizon',num(I.horizon_years,1)+' yrs',I.horizon_source)+card('Expected return',pct(S.expected_return_pct,2),'strategic '+pct(r.stats.strategic.expected_return_pct,2))+card('Volatility',pct(S.volatility_pct,2))+card('1-in-20 bad year',`<span class="${G.within_limit?'':'bad'}">${pct(S.bad_year_pct,1)}</span>`,'your limit −'+G.max_annual_loss_pct+'%')+card('Sharpe (CMA)',num(S.sharpe,2));
 document.getElementById('aal_target').innerHTML=table(['Class','#Strategic','#Tactical tilt','#Target','#Bounds',''],ACLS.map(c=>`<tr><td>${c}</td><td class="n">${pct(r.strategic[c])}</td><td class="n ${r.tactical_tilts_pp[c]<0?'bad':r.tactical_tilts_pp[c]>0?'good':''}">${r.tactical_tilts_pp[c]>0?'+':''}${num(r.tactical_tilts_pp[c],2)}</td><td class="n"><b>${pct(r.target[c])}</b></td><td class="n muted">${r.bounds[c][0]}–${r.bounds[c][1]}%</td><td style="width:30%"><div class="bar"><i style="width:${r.target[c]}%"></i></div></td></tr>`))+`<div class="muted">Not allocated (held as they are): ${r.not_allocated.join(', ')} · CMA: ${esc(r.cma.source)}</div>`;
 document.getElementById('aal_sig').innerHTML=table(['Signal','#Score','Value','As of','Source','Meaning'],r.signals.map(s=>`<tr><td>${esc(s.signal)}</td><td class="n ${s.score<0?'bad':s.score>0?'good':''}">${s.score==null?'<span class="muted">no data</span>':num(s.score,2)}</td><td class="muted" style="font-size:11px">${esc(JSON.stringify(s.value))}</td><td>${esc(s.as_of||'')}</td><td class="muted">${esc(s.source)}</td><td class="muted">${esc(s.note)}</td></tr>`));
 document.getElementById('aal_steps').innerHTML=r.steps.map(s=>`<div style="margin:4px 0"><b>${esc(s.step)}</b> <span class="muted">${esc(s.detail)}</span><div class="muted" style="font-size:11px">${ACLS.map(c=>c+' '+num((s.weights||{})[c],1)).join(' · ')}</div></div>`).join('')+
  '<h3>Tilt contributions</h3>'+table(['Signal','Class','#Score','#Weight','#Contribution'],r.tilt_explanation.map(t=>`<tr><td>${esc(t.signal)}</td><td>${t.class}</td><td class="n">${num(t.score,2)}</td><td class="n">${num(t.weight,2)}</td><td class="n">${num(t.contribution,3)}</td></tr>`));
 const E=r.equity_split;document.getElementById('aal_eq').innerHTML=`<div>Large ${pct(E.LARGE)} · Mid ${pct(E.MID)} · Small ${pct(E.SMALL)}</div>`+E.notes.map(n=>`<div class="muted">${esc(n)}</div>`).join('');
 const V=r.sector_views||{};document.getElementById('aal_sec').innerHTML=V.strongest?`<div class="good">Strongest: ${V.strongest.map(x=>esc(x.sector)+' ('+num(x.median_atip_score,0)+')').join(', ')}</div><div class="bad">Weakest: ${V.weakest.map(x=>esc(x.sector)+' ('+num(x.median_atip_score,0)+')').join(', ')}</div><div class="muted">${esc(V.note)} · ${esc(V.as_of)}</div>`:'<span class="muted">'+esc(V.status||'not available')+'</span>';
}
async function aalRun(){try{const r=await post('/api/wealth/allocation/run');aalRender(r,'saved as '+r.run_id);msg('aal_msg','Allocation saved. The Rebalance tab compares your holdings with it.');aalHist()}catch(e){msg('aal_msg',e.message,1)}}
async function aalPreview(){try{aalRender(await post('/api/wealth/allocation/preview'),'preview (not saved)')}catch(e){msg('aal_msg',e.message,1)}}
async function aalHist(){const H=await j('/api/wealth/allocation/runs?limit=20');document.getElementById('aal_hist').innerHTML=table(['Run','Date','Band'].concat(ACLS.map(c=>'#'+c)),H.map(h=>`<tr><td class="muted">${esc(h.run_id)}</td><td>${esc(String(h.created_at).slice(0,16))}</td><td>${esc(h.band)}</td>${ACLS.map(c=>`<td class="n">${num(h.target[c],1)}</td>`).join('')}</tr>`))}
function aalPolShow(){document.getElementById('aal_pol').innerHTML='Bounds: '+(Object.entries(APOL.bounds).map(([k,v])=>`${k} ${v[0]}–${v[1]}% <a href="#" onclick="aalUnbound('${k}');return false">✕</a>`).join(' · ')||'band defaults')+' · excluded: '+(APOL.excluded_classes.join(', ')||'none')+' · tactical '+(APOL.tactical_enabled?'on':'off')+(APOL.max_tilt_pct!=null?' (max '+APOL.max_tilt_pct+' pts)':'');
 document.getElementById('p_tac').checked=APOL.tactical_enabled}
async function aalSavePol(p){try{APOL=await put('/api/wealth/allocation/policy',p);aalPolShow()}catch(e){msg('aal_msg',e.message,1)}}
function aalBase(){return {bounds:{...APOL.bounds},excluded_classes:[...APOL.excluded_classes],tactical_enabled:APOL.tactical_enabled,max_tilt_pct:APOL.max_tilt_pct}}
function aalBound(){const b=aalBase();b.bounds[document.getElementById('p_cls').value]=[Number(document.getElementById('p_min').value||0),Number(document.getElementById('p_max').value||100)];aalSavePol(b)}
function aalUnbound(k){const b=aalBase();delete b.bounds[k];aalSavePol(b)}
function aalPol(){const b=aalBase();b.tactical_enabled=document.getElementById('p_tac').checked;const t=document.getElementById('p_tilt').value;b.max_tilt_pct=t===''?null:Number(t);aalSavePol(b)}
LOADERS.aal=async()=>{
 const sel=document.getElementById('p_cls');if(!sel.options.length)sel.innerHTML=ACLS.map(c=>`<option>${c}</option>`).join('');
 APOL=await j('/api/wealth/allocation/policy');aalPolShow();
 try{const r=await j('/api/wealth/allocation');aalRender(r,'latest saved run '+r.run_id+' · '+String(r.created_at).slice(0,16))}catch(e){if(e.status===404){try{aalRender(await post('/api/wealth/allocation/preview'),'preview (nothing saved yet)')}catch(x){msg('aal_msg',x.message,1)}}else msg('aal_msg',e.message,1)}
 aalHist();
};
"""

TAB = ("aal", "Allocation", HTML, JS)
