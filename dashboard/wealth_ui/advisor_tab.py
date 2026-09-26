"""W16 tab: the explainable advisor (claims with evidence, suitability-checked suggestions)."""

HTML = r"""
<div id="adv_msg"></div>
<div class="row"><input id="a_q" placeholder="Ask about your wealth, goals, risk, allocation, performance, a stock (e.g. RELIANCE) or a scenario" style="flex:1;min-width:260px" onkeydown="if(event.key==='Enter')advAsk()">
<select id="a_topic"><option value="">auto topic</option></select><label class="muted"><input type="checkbox" id="a_nar"> AI narration</label><button class="primary" onclick="advAsk()">Ask</button></div>
<div id="a_sugg" class="row" style="margin-top:6px"></div>
<p class="muted" id="a_note"></p>
<div id="adv_ans"></div>
<h2>Previous questions</h2><div id="adv_hist"></div>
"""

JS = r"""
function advView(r){
 const ev=e=>`<span class="pill info" title="${esc(JSON.stringify(e.value))}">${esc(e.source)}${e.field?' · '+esc(e.field):''}${e.as_of?' · '+esc(e.as_of):''}</span>`;
 const n=r.narration||{};
 return `<h2>${esc(r.question||r.topic)} <span class="muted">topic ${esc(r.topic)}${r.symbol?' · '+esc(r.symbol):''} · confidence ${esc(r.confidence)} · ${esc(r.methodology_version)}</span></h2>
 ${n.status==='OK'?`<div class="card" style="margin-bottom:8px"><div class="k">AI narration (${esc(n.model)})</div><div style="white-space:pre-wrap;margin-top:4px">${esc(n.text)}</div><div class="muted" style="font-size:11px;margin-top:4px">${esc(n.note)}</div></div>`:(n.status&&n.status!=='OFF'?`<div class="muted">Narration ${esc(n.status)}: ${esc(n.detail||'')} — deterministic answer below.</div>`:'')}
 <h3>What the data says</h3>${(r.claims||[]).map(c=>`<div style="margin:5px 0">${esc(c.text)}<div>${c.evidence.map(ev).join(' ')}</div></div>`).join('')||'<span class="muted">no data</span>'}
 ${(r.recommendations||[]).length?'<h3>Suggestions (suitability-checked)</h3>'+r.recommendations.map(x=>`<div style="margin:5px 0">${pill(x.suitability||'OK',x.suitability==='OK'?'OK':'AT_RISK')} <b>${esc(x.action)}</b> <span class="muted">${esc(x.rationale)}</span><div>${(x.evidence||[]).map(ev).join(' ')}</div></div>`).join(''):''}
 ${(r.caveats||[]).length?'<h3>Caveats</h3>'+r.caveats.map(c=>`<div class="warn">${esc(c)}</div>`).join(''):''}
 <div class="muted" style="margin-top:8px">${esc(r.trading_note)} ${esc(r.disclaimer)}</div>
 ${r.advice_id?`<div class="row" style="margin-top:6px"><span class="muted">Was this helpful?</span><button onclick="advFb('${r.advice_id}',true)">Yes</button><button onclick="advFb('${r.advice_id}',false)">No</button></div>`:''}`}
async function advAsk(q){const b={question:q||document.getElementById('a_q').value};const t=document.getElementById('a_topic').value;if(t)b.topic=t;b.narrate=document.getElementById('a_nar').checked;
 if(q)document.getElementById('a_q').value=q;msg('adv_msg','Working…');
 try{const r=await post('/api/wealth/advisor/ask',b);document.getElementById('adv_msg').innerHTML='';document.getElementById('adv_ans').innerHTML=advView(r);advHist()}catch(e){msg('adv_msg',e.message,1)}}
async function advFb(id,h){const note=h?null:prompt('What was missing or wrong? (optional)');try{await post('/api/wealth/advisor/'+id+'/feedback',{helpful:h,note});msg('adv_msg','Thanks, feedback recorded.')}catch(e){msg('adv_msg',e.message,1)}}
async function advOpen(id){document.getElementById('adv_ans').innerHTML=advView(await j('/api/wealth/advisor/'+id))}
async function advHist(){const H=await j('/api/wealth/advisor/history?limit=20');document.getElementById('adv_hist').innerHTML=table(['Asked','Question','Topic','Narration','Feedback'],H.map(h=>`<tr><td>${esc(String(h.asked_at).slice(0,16))}</td><td><a href="#" onclick="advOpen('${h.advice_id}');return false" style="color:var(--accent)">${esc(h.question||'('+h.topic+')')}</a></td><td>${esc(h.topic)}</td><td class="muted">${esc(h.narration_status)}</td><td class="muted">${esc(h.feedback||'')}</td></tr>`))}
LOADERS.adv=async()=>{const T=await j('/api/wealth/advisor/topics');const sel=document.getElementById('a_topic');
 if(sel.options.length<2){sel.innerHTML+=T.topics.map(t=>`<option>${t}</option>`).join('');
  document.getElementById('a_sugg').innerHTML=T.suggested.map(q=>`<button onclick="advAsk(${esc(JSON.stringify(q))})">${esc(q)}</button>`).join('');
  document.getElementById('a_nar').checked=T.narration_enabled;document.getElementById('a_nar').disabled=!T.narration_enabled;
  document.getElementById('a_note').textContent='Every statement shows its evidence (hover a tag for the value). '+(T.narration_enabled?'AI narration uses '+T.narration_model+' and only rephrases the evidence.':'AI narration is off (config wealth.advisor_llm_enabled); answers are fully deterministic.')}
 advHist()};
"""

TAB = ("adv", "Advisor", HTML, JS)
