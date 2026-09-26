"""W13 tab: goal planning (targets, projections, required SIP / CAGR, Monte Carlo, scenarios)."""

HTML = r"""
<div id="gol_msg"></div><div id="gol_cards" class="grid"></div>
<div id="gol_warn"></div>
<h2>Goals</h2><div id="gol_list"></div>
<div id="gol_detail"></div>
<h2>Add a goal</h2>
<div class="row">
<input id="g_name" placeholder="name" style="width:170px"><select id="g_type"></select><select id="g_pri"><option>ESSENTIAL</option><option selected>IMPORTANT</option><option>ASPIRATIONAL</option></select>
<input id="g_amt" type="number" placeholder="target (today's Rs)" style="width:150px"><label class="muted">by <input id="g_date" type="date"></label>
<input id="g_cur" type="number" placeholder="already saved (Rs)" style="width:140px"><input id="g_sip" type="number" placeholder="monthly SIP (Rs)" style="width:130px">
<input id="g_step" type="number" step="any" placeholder="step-up %/yr" style="width:100px"><input id="g_inf" type="number" step="any" placeholder="inflation % (default by type)" style="width:170px">
<input id="g_ret" type="number" step="any" placeholder="return % (default: allocation / DNA)" style="width:210px">
</div>
<div class="row" style="margin-top:6px" id="g_ret_row"><span class="muted">Retirement (instead of a target):</span><input id="g_rexp" type="number" placeholder="monthly expense (today's Rs)" style="width:190px"><input id="g_ryrs" type="number" placeholder="years in retirement" style="width:140px"><input id="g_rret" type="number" step="any" placeholder="post-retirement return %" style="width:170px">
<span class="muted">Emergency fund:</span><input id="g_em" type="number" placeholder="months of expenses" style="width:140px"></div>
<div class="row" style="margin-top:6px"><button class="primary" onclick="golAdd()">Add goal</button></div>
"""

JS = r"""
const GTYPES=["RETIREMENT","CHILD_EDUCATION","HOUSE","VEHICLE","WEDDING","TRAVEL","EMERGENCY_FUND","WEALTH_CREATION","CUSTOM"];
function golRow(g){const e=g.evaluation||{};const mc=e.monte_carlo||{};
 if(e.error)return `<tr><td>${esc(g.name)}</td><td colspan=9 class="bad">${esc(e.error)}</td></tr>`;
 return `<tr><td><a href="#" onclick="golShow('${g.goal_id}');return false" style="color:var(--accent)">${esc(g.name)}</a><div class="muted">${esc(g.goal_type)} · ${esc(g.priority)}</div></td><td>${esc(g.target_date)}</td><td class="n">${inr(e.future_target)}<div class="muted">${inr(e.target_today)} today</div></td><td class="n">${inr(e.funding&&e.funding.total)}<div class="muted">${pct(e.funded_pct_today)}</div></td><td class="n">${inr(g.monthly_contribution)}</td><td class="n">${inr(e.projected)}</td><td class="n ${e.gap<0?'bad':'good'}">${inr(e.gap)}</td><td class="n">${inr(e.required_monthly)}</td><td class="n">${e.required_return_pct==null?'<span class="bad">unreachable</span>':pct(e.required_return_pct,2)}</td><td>${pill(e.status)}<div class="muted">${mc.success_probability!=null?pct(mc.success_probability)+' success':''}</div></td></tr>`}
async function golShow(id){const g=await j('/api/wealth/goals/'+id);const e=g.evaluation;if(!e||e.error){document.getElementById('gol_detail').innerHTML='';return}
 const a=e.assumptions,mc=e.monte_carlo||{};
 document.getElementById('gol_detail').innerHTML=`<h2>${esc(g.name)} <span class="muted">${esc(e.methodology_version)} · as of ${esc(e.as_of)}</span></h2>
 <div class="grid">${card('Target (future Rs)',inr(e.future_target),esc(e.target_source))}${card('Projected',inr(e.projected),pct(e.projected_funded_pct)+' of target')}${card('Success probability',pct(mc.success_probability),mc.paths+' Monte Carlo paths',mc.success_probability)}${card('P10 / P50 / P90',inr(mc.p50),inr(mc.p10)+' … '+inr(mc.p90))}${card('Extra monthly needed',inr(e.additional_monthly),'or '+inr(e.lumpsum_today)+' today')}${card('Years left',num(e.years_left,1))}</div>
 ${e.note?`<div class="warn" style="margin-top:6px">${esc(e.note)}</div>`:''}<h3>Assumptions</h3><div class="muted">Return ${pct(a.expected_return_pct,2)} (vol ${pct(a.volatility_pct,1)}) from ${esc(a.return_source)} · inflation ${pct(a.inflation_pct,1)} (${esc(a.inflation_source)})</div>
 <h3>Scenarios</h3>${table(['Scenario','#Return','#Inflation','#Monthly','#Target','#Projected','#Gap','#Funded'],e.scenarios.map(s=>`<tr><td>${esc(s.scenario)}</td><td class="n">${pct(s.return_pct,2)}</td><td class="n">${pct(s.inflation_pct,1)}</td><td class="n">${inr(s.monthly_contribution)}</td><td class="n">${inr(s.future_target)}</td><td class="n">${inr(s.projected)}</td><td class="n ${s.gap<0?'bad':'good'}">${inr(s.gap)}</td><td class="n">${pct(s.funded_pct)}</td></tr>`))}
 <h3>Suggested glide path (growth assets)</h3><div class="muted">${e.glide_path.map(x=>x.years_left+'y: '+x.growth_assets_pct+'%').join(' · ')}</div>
 <h3>What if</h3><div class="row"><input id="w_sip" type="number" placeholder="monthly SIP" style="width:120px"><input id="w_ret" type="number" step="any" placeholder="return %" style="width:90px"><input id="w_date" type="date"><input id="w_amt" type="number" placeholder="target (today's Rs)" style="width:150px"><button onclick="golWhatIf('${id}')">Simulate</button><button onclick="golProj('${id}')">Store today's projection</button><button onclick="golStatus('${id}','ACHIEVED')">Mark achieved</button><button onclick="golStatus('${id}','ABANDONED')">Abandon</button></div><div id="w_out"></div>
 <h3>Stored projections</h3><div id="w_hist"></div>`;
 const P=await j('/api/wealth/goals/'+id+'/projections');document.getElementById('w_hist').innerHTML=P.length?spark(P,'success_probability').replace(/₹/g,'')+'<div class="muted">success probability (%) over time</div>':'<span class="muted">none yet</span>';}
async function golWhatIf(id){const o={};const g=x=>document.getElementById(x).value;if(g('w_sip')!=='')o.monthly_contribution=Number(g('w_sip'));if(g('w_ret')!=='')o.expected_return_pct=Number(g('w_ret'));if(g('w_date'))o.target_date=g('w_date');if(g('w_amt')!=='')o.target_amount=Number(g('w_amt'));
 try{const r=await post('/api/wealth/goals/'+id+'/simulate',{overrides:o});const e=r.evaluation;document.getElementById('w_out').innerHTML=`<div class="msg">What-if: projected ${inr(e.projected)} vs target ${inr(e.future_target)} · gap ${inr(e.gap)} · success ${pct((e.monte_carlo||{}).success_probability)} · ${e.status} (not saved)</div>`}catch(x){document.getElementById('w_out').innerHTML=`<div class="msg e">${esc(x.message)}</div>`}}
async function golProj(id){try{await post('/api/wealth/goals/'+id+'/projection');golShow(id)}catch(e){msg('gol_msg',e.message,1)}}
async function golStatus(id,st){if(!confirm('Set goal status to '+st+'?'))return;try{await post('/api/wealth/goals/'+id+'/status',{status:st});document.getElementById('gol_detail').innerHTML='';LOADERS.gol()}catch(e){msg('gol_msg',e.message,1)}}
async function golAdd(){const g=x=>document.getElementById(x).value;const b={name:g('g_name'),goal_type:g('g_type'),priority:g('g_pri'),target_date:g('g_date')};
 const nums={target_amount:'g_amt',current_amount:'g_cur',monthly_contribution:'g_sip',step_up_pct:'g_step',inflation_pct:'g_inf',expected_return_pct:'g_ret',retirement_monthly_expense:'g_rexp',years_in_retirement:'g_ryrs',post_retirement_return_pct:'g_rret',emergency_months:'g_em'};
 for(const[k,id]of Object.entries(nums))if(g(id)!=='')b[k]=Number(g(id));
 try{await post('/api/wealth/goals',b);msg('gol_msg','Goal added. Your Investor DNA risk requirement can now use your goals: re-score it on the Investor DNA tab (Refresh from goals).');LOADERS.gol()}catch(e){msg('gol_msg',e.message,1)}}
LOADERS.gol=async()=>{
 const sel=document.getElementById('g_type');if(!sel.options.length)sel.innerHTML=GTYPES.map(t=>`<option>${t}</option>`).join('');
 const O=await j('/api/wealth/goals/overview');const sc=O.status_counts||{};
 document.getElementById('gol_cards').innerHTML=card('Goals',O.goals,Object.entries(sc).map(([k,v])=>k+' '+v).join(' · '))+card('Target (future Rs)',inr(O.total_future_target))+card('Projected',inr(O.total_projected))+card('Monthly now',inr(O.monthly_contribution),'needed '+inr(O.required_monthly))+card('Extra monthly needed',inr(O.additional_monthly))+card('Required return',O.required_return?pct(O.required_return.required_return_pct,2):'—',O.required_return?'essential + important goals':'');
 document.getElementById('gol_warn').innerHTML=(O.link_warnings||[]).map(w=>`<div class="warn">${esc(w.key)} is linked to goals for ${num(w.total_pct,0)}% in total (over 100%).</div>`).join('')+(O.errors||[]).map(x=>`<div class="bad">${esc(x.error)}</div>`).join('');
 const L=await j('/api/wealth/goals');
 document.getElementById('gol_list').innerHTML=table(['Goal','Date','#Target','#Funded now','#Monthly','#Projected','#Gap','#Required monthly','#Required return','Status'],L.map(golRow));
};
"""

TAB = ("gol", "Goals", HTML, JS)
