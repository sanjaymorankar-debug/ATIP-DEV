"""W17 tab: the Investor-mode home -- one view of the whole chain, mode switch, today's signals with suitability."""

HTML = r"""
<div id="ovw_msg"></div>
<div class="row"><span class="muted">View:</span><button id="m_inv" onclick="ovwMode('INVESTOR')">Investor mode</button><button id="m_trd" onclick="ovwMode('TRADER')">Trader mode</button>
<button onclick="ovwCycle()">Run investor cycle now</button><span class="muted" id="ovw_cycle"></span></div>
<div id="ovw_trader" style="display:none" class="msg">Trader mode: your trading pages are <a style="color:var(--accent)" href="/">Dashboard</a> · <a style="color:var(--accent)" href="/strategies">Strategies</a> · <a style="color:var(--accent)" href="/trading">Trading</a> · <a style="color:var(--accent)" href="/backtests">Backtests</a>. Investor views stay available here.</div>
<div id="ovw_next"></div>
<div id="ovw_cards" class="grid" style="margin-top:8px"></div>
<h2>Briefing</h2><div id="ovw_brief"></div>
<h2>Today's ATIP signals, checked against your profile and plan</h2><div id="ovw_sig"></div>
<h2>How the pieces connect</h2><div id="ovw_status"></div>
"""

JS = r"""
async function ovwMode(m){try{await put('/api/wealth/mode',{mode:m});LOADERS.ovw()}catch(e){msg('ovw_msg',e.message,1)}}
async function ovwCycle(){msg('ovw_msg','Running the investor cycle…');try{const r=await post('/api/wealth/cycle');msg('ovw_msg','Cycle '+r.status+': '+Object.entries(r.steps).map(([k,v])=>k+' '+v.status).join(' · ')+(r.alerts.length?' · '+r.alerts.length+' alert(s)':''));LOADERS.ovw()}catch(e){msg('ovw_msg',e.message,1)}}
LOADERS.ovw=async()=>{
 const O=await j('/api/wealth/overview');const M=O.mode.mode;
 document.getElementById('m_inv').className=M==='INVESTOR'?'primary':'';document.getElementById('m_trd').className=M==='TRADER'?'primary':'';
 document.getElementById('ovw_trader').style.display=M==='TRADER'?'block':'none';
 const C=O.last_cycle;document.getElementById('ovw_cycle').textContent=C?'last cycle '+String(C.created_at).slice(0,16)+' '+C.status:'no cycle run yet';
 const D=O.dna,W=O.wealth.totals,G=O.goals,A=O.allocation,P=O.performance;
 document.getElementById('ovw_cards').innerHTML=card('Net worth',inr(W.net_worth),W.positions+' positions')+
  card('Risk profile',D.band?esc(D.band.replace(/_/g,' ')):'<a href="#" onclick="show(\'dna\');return false" style="color:var(--accent)">not set</a>',D.status!=='MISSING'?'score '+num(D.risk_score,0)+(D.status==='STALE'?' · STALE':''):'',D.risk_score)+
  card('Goals',G.goals,Object.entries(G.status_counts||{}).map(([k,v])=>k.replace('_',' ').toLowerCase()+' '+v).join(' · ')||'none yet')+
  card('Allocation',pill(A.verdict,{REBALANCE:'OFF_TRACK',REVIEW:'AT_RISK',NO_ACTION:'ON_TRACK'}[A.verdict]||'info'),A.target_source||A.detail||'')+
  (P?card('Performance ('+esc(P.portfolio)+')',pct(P.returns.ACTUAL,2),'model '+pct(P.returns.MODEL,2)+' · executable '+pct(P.returns.EXECUTABLE,2)+' · benchmark '+pct(P.returns.BENCHMARK,2)):card('Performance','—','no report yet'));
 const B=O.briefing;document.getElementById('ovw_brief').innerHTML=B.claims.map(c=>`<div style="margin:4px 0">${esc(c.text)} <span class="muted" style="font-size:11px">[${c.evidence.map(e=>esc(e.source)).join(', ')}]</span></div>`).join('')+
  B.recommendations.map(r=>`<div style="margin:4px 0">${pill(r.suitability,r.suitability==='OK'?'OK':'AT_RISK')} <b>${esc(r.action)}</b> <span class="muted">${esc(r.rationale)}</span></div>`).join('')+B.caveats.map(c=>`<div class="warn">${esc(c)}</div>`).join('');
 document.getElementById('ovw_sig').innerHTML=table(['Symbol','Signal','#ATIP score','Held','Fit','Checks'],O.signals.map(s=>`<tr><td>${esc(s.symbol)}</td><td>${esc(s.signal)}</td><td class="n">${num(s.atip_score,1)}</td><td>${s.held?'yes':''}</td><td>${pill(s.verdict,{SUITABLE:'OK',CAUTION:'AT_RISK',NOT_SUITABLE:'OFF_TRACK'}[s.verdict])}</td><td class="muted" style="font-size:11px">${s.checks.map(c=>c.check+': '+c.result+' ('+esc(c.detail)+')').join('<br>')}</td></tr>`))+'<div class="muted">Fit with your profile and plan only; ATIP places nothing from this page.</div>';
 const S=await j('/api/wealth/status');
 document.getElementById('ovw_next').innerHTML=S.next_step?`<div class="msg">Next step: <b>${esc(S.next_step)}</b></div>`:'';
 document.getElementById('ovw_status').innerHTML=`<div>State ${pill(S.state,{READY:'OK',DEGRADED:'AT_RISK',INCOMPLETE:'OFF_TRACK'}[S.state])} · scheduled daily cycle ${S.scheduled_cycle?'ON':'OFF (config wealth.enabled)'}</div>`+
  table(['Investor chain','Last updated'],Object.entries(S.components).map(([k,v])=>`<tr><td>${esc(k.replace(/_/g,' '))}</td><td>${v?esc(v):'<span class="warn">missing</span>'}</td></tr>`))+
  table(['Market data','Latest'],Object.entries(S.market_data).map(([k,v])=>`<tr><td>${esc(k)}</td><td class="${S.stale_market_data.includes(k)?'warn':''}">${esc(v||'none')}</td></tr>`));
};
"""

TAB = ("ovw", "Overview", HTML, JS)
