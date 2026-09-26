"""W15 tab: drift vs target, triggers, cost-aware rebalance plans (advisory only)."""

HTML = r"""
<div id="rbl_msg"></div>
<div id="rbl_cards" class="grid"></div>
<h2>Drift vs target</h2><div id="rbl_drift"></div>
<h2>Triggers</h2><div id="rbl_trig"></div>
<h2>Build a plan</h2>
<div class="row"><select id="r_mode"><option value="to_band">To band edges (least turnover)</option><option value="to_target">All the way to target</option><option value="cash_flow">Invest new cash only (no selling)</option></select>
<input id="r_cash" type="number" placeholder="new cash to invest (Rs)" style="width:180px"><button class="primary" onclick="rblPlan()">Build plan</button></div>
<div id="rbl_plan"></div>
<h2>Plans</h2><div id="rbl_hist"></div>
"""

JS = r"""
function rblPlanView(p){const S=p.summary,A=p.after;
 return `<h3>${p.plan_id?'Plan '+esc(p.plan_id)+' '+pill(p.status):'Plan'} · ${esc(p.mode)}${p.new_cash?' · new cash '+inr(p.new_cash):''}</h3>
 <div class="grid">${card('Legs',S.legs)}${card('Turnover',inr(S.turnover),pct(S.turnover_pct)+' of investable')}${card('Estimated costs',inr(S.estimated_costs),num(S.cost_pct_of_turnover,3)+'% of turnover')}${card('Max drift after',num(A.max_drift_pp,2)+' pts')}${card('Expected return after',pct(A.stats.expected_return_pct,2),'volatility '+pct(A.stats.volatility_pct,2))}${S.residual_cash?card('Left in cash',inr(S.residual_cash)):''}</div>
 ${table(['Side','Class','Instrument','Symbol / name','#Qty','#Price','#Value','#Costs','Why'],p.trades.map(t=>`<tr><td class="${t.side==='SELL'||t.side==='REDUCE'?'bad':'good'}"><b>${t.side}</b></td><td>${t.class}</td><td>${esc(t.instrument)}</td><td>${esc(t.symbol||'')} <span class="muted">${esc(t.name&&t.name!==t.symbol?t.name:'')}</span>${t.source==='SUGGESTED'?' <span class="pill info">suggested</span>':''}</td><td class="n">${t.quantity??'—'}</td><td class="n">${t.price==null?'—':num(t.price)}</td><td class="n">${inr(t.value)}</td><td class="n">${inr(t.costs.total)}</td><td class="muted">${esc(t.reason)}</td></tr>`))}
 ${p.skipped.length?'<div class="muted">Skipped: '+p.skipped.map(s=>esc(s.class)+' '+inr(s.amount)+' ('+esc(s.reason)+')').join('; ')+'</div>':''}
 <div class="warn" style="margin-top:6px">${esc(p.tax_note)}</div><div class="muted">${esc(p.execution_note)}</div>
 ${p.plan_id&&p.status==='PROPOSED'?`<div class="row" style="margin-top:6px"><button onclick="rblDecide('${p.plan_id}','ACCEPTED')">Accept</button><button onclick="rblDecide('${p.plan_id}','EXECUTED_MANUALLY')">I executed it</button><button onclick="rblDecide('${p.plan_id}','DISMISSED')">Dismiss</button></div>`:''}
 ${p.plan_id&&p.status==='ACCEPTED'?`<div class="row" style="margin-top:6px"><button onclick="rblDecide('${p.plan_id}','EXECUTED_MANUALLY')">I executed it</button></div>`:''}`}
async function rblPlan(){const b={mode:document.getElementById('r_mode').value};const c=document.getElementById('r_cash').value;if(c!=='')b.new_cash=Number(c);
 try{const p=await post('/api/wealth/rebalance/plan',b);document.getElementById('rbl_plan').innerHTML=rblPlanView(p);rblHist()}catch(e){msg('rbl_msg',e.message,1)}}
async function rblDecide(id,d){const note=prompt('Note (optional):')||null;try{const p=await post('/api/wealth/rebalance/plans/'+id+'/decision',{decision:d,note});document.getElementById('rbl_plan').innerHTML=rblPlanView(p);rblHist()}catch(e){msg('rbl_msg',e.message,1)}}
async function rblOpen(id){const p=await j('/api/wealth/rebalance/plans/'+id);document.getElementById('rbl_plan').innerHTML=rblPlanView(p)}
async function rblHist(){const H=await j('/api/wealth/rebalance/plans?limit=20');document.getElementById('rbl_hist').innerHTML=table(['Plan','Created','Mode','Verdict','Status','Decided'],H.map(h=>`<tr><td><a href="#" onclick="rblOpen('${h.plan_id}');return false" style="color:var(--accent)">${esc(h.plan_id)}</a></td><td>${esc(String(h.created_at).slice(0,16))}</td><td>${esc(h.mode)}</td><td>${esc(h.verdict)}</td><td>${pill(h.status)}</td><td class="muted">${esc(h.decided_at?String(h.decided_at).slice(0,16)+' '+(h.decision_note||''):'')}</td></tr>`))}
LOADERS.rbl=async()=>{
 let C;try{C=await j('/api/wealth/rebalance/check')}catch(e){msg('rbl_msg',e.message,1);return}
 const vc={REBALANCE:'OFF_TRACK',REVIEW:'AT_RISK',NO_ACTION:'ON_TRACK'}[C.verdict];
 document.getElementById('rbl_cards').innerHTML=card('Verdict',pill(C.verdict,vc))+card('Investable value',inr(C.investable_value),'net-worth assets '+inr(C.net_worth_assets))+card('Current volatility',pct(C.stats.current.volatility_pct,2),'target '+pct(C.stats.target.volatility_pct,2))+card('Current bad year',pct(C.stats.current.bad_year_pct,1),'target '+pct(C.stats.target.bad_year_pct,1))+card('Target from',esc(C.target_source));
 document.getElementById('rbl_drift').innerHTML=table(['Class','#Current','#Target','#Drift (pts)','#Relative','Band','#Current value','#Target value'],C.drift.map(d=>`<tr${d.out_of_band?' style="background:#7f1d1d33"':''}><td>${d.class}</td><td class="n">${pct(d.current_pct)}</td><td class="n">${pct(d.target_pct)}</td><td class="n ${d.out_of_band?'bad':''}">${d.drift_pp>0?'+':''}${num(d.drift_pp,2)}</td><td class="n">${d.relative_pct==null?'—':num(d.relative_pct,1)+'%'}</td><td class="muted">${num(d.band[0],1)}–${num(d.band[1],1)}%</td><td class="n">${inr(d.current_value)}</td><td class="n">${inr(d.target_value)}</td></tr>`))+`<div class="muted">Bands: ±${C.bands.abs_pct} pts or ±${C.bands.rel_pct}% of target, whichever is tighter. ${esc(C.tax_note)}</div>`;
 document.getElementById('rbl_trig').innerHTML=C.triggers.length?C.triggers.map(t=>`<div>${pill(t.trigger,t.severity==='action'?'OFF_TRACK':'AT_RISK')} ${esc(t.detail)}</div>`).join(''):'<div class="good">No trigger: holdings are within their bands.</div>';
 rblHist();
};
"""

TAB = ("rbl", "Rebalance", HTML, JS)
