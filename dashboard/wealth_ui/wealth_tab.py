"""W12 tab: multi-asset wealth (net worth, allocation, concentration, positions, holdings, liabilities)."""

HTML = r"""
<div id="wlt_msg"></div><div id="wlt_cards" class="grid"></div>
<div class="row" style="margin-top:8px"><button onclick="wltSnap()">Record today's snapshot</button><span class="muted" id="wlt_asof"></span></div>
<h2>Net worth history</h2><div id="wlt_hist"></div>
<h2>Allocation by asset class</h2><div id="wlt_class"></div>
<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(320px,1fr));margin-top:8px">
<div><h3>By source</h3><div id="wlt_src"></div></div><div><h3>Equity by sector</h3><div id="wlt_sec" class="scroll" style="max-height:260px"></div></div></div>
<h2>Concentration &amp; liquidity</h2><div id="wlt_conc"></div>
<h2>Holdings flagged by ATIP scores</h2><div id="wlt_risk"></div>
<h2>Positions</h2><div id="wlt_pos" class="scroll"></div>
<h2>Add a holding</h2>
<div class="row" id="wlt_form">
<select id="h_class" onchange="wltInst()"></select><select id="h_inst" onchange="wltVal()"></select><select id="h_val"></select>
<input id="h_name" placeholder="name" style="width:160px"><input id="h_sym" placeholder="symbol (listed)" style="width:120px">
<input id="h_qty" type="number" step="any" placeholder="quantity / amount / grams" style="width:170px">
<input id="h_cost" type="number" step="any" placeholder="avg cost per unit (Rs)" style="width:160px">
<input id="h_px" type="number" step="any" placeholder="manual price per unit" style="width:150px">
<input id="h_cur" placeholder="currency" value="INR" style="width:70px"><input id="h_fx" type="number" step="any" placeholder="FX (INR per unit)" style="width:120px">
<button class="primary" onclick="wltAdd()">Add</button></div>
<p class="muted" id="wlt_excl"></p>
<h2>Liabilities</h2><div id="wlt_liab"></div>
<div class="row" style="margin-top:6px"><select id="l_kind"></select><input id="l_name" placeholder="name"><input id="l_out" type="number" placeholder="outstanding (Rs)"><input id="l_rate" type="number" step="any" placeholder="interest %"><input id="l_emi" type="number" placeholder="EMI (Rs)"><button onclick="wltLiab()">Add liability</button></div>
<h2>Reconciliation &amp; data quality</h2><div id="wlt_rec"></div>
<h2>Classification overrides</h2><p class="muted">If ATIP classes a listed symbol wrongly (for example a gold ETF read as a share), override it here.</p>
<div class="row"><input id="c_sym" placeholder="symbol"><select id="c_class"></select><select id="c_inst"><option>ETF</option><option>STOCK</option></select><button onclick="wltCls()">Save override</button></div><div id="wlt_cls"></div>
"""

JS = r"""
let ASSETS=null;
const LIAB=["HOME_LOAN","VEHICLE_LOAN","PERSONAL_LOAN","EDUCATION_LOAN","CREDIT_CARD","LOAN_AGAINST_SHARES","OTHER"];
function spark(rows,key){key=key||'net_worth';if(!rows.length)return '<span class="muted">no history yet</span>';const W=640,H=120,P=6;const v=rows.map(r=>r[key]||0);const mn=Math.min(...v),mx=Math.max(...v),rg=(mx-mn)||1;
 const pts=v.map((x,i)=>`${P+i*(W-2*P)/Math.max(1,v.length-1)},${H-P-(x-mn)/rg*(H-2*P)}`).join(' ');
 return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;max-width:${W}px;height:${H}px;background:var(--panel);border-radius:6px"><polyline fill="none" stroke="#38bdf8" stroke-width="2" points="${pts}"/></svg><div class="muted">${esc(rows[0].date)} ${inr(v[0])} → ${esc(rows[rows.length-1].date)} ${inr(v[v.length-1])}</div>`}
const grp=(g,label)=>table([label,'#Value','#Weight','#Cost','#Unrealized','#Count'],Object.entries(g||{}).map(([k,x])=>`<tr><td>${esc(k)}</td><td class="n">${inr(x.value)}</td><td class="n">${pct(x.weight_pct)}<div class="bar"><i style="width:${x.weight_pct}%"></i></div></td><td class="n">${inr(x.cost)}</td><td class="n ${x.unrealized<0?'bad':'good'}">${x.unrealized==null?'—':inr(x.unrealized)}</td><td class="n">${x.count}</td></tr>`));
function wltInst(){const c=document.getElementById('h_class').value;const I=Object.entries(ASSETS.instruments).filter(([k,v])=>v.classes.includes(c));document.getElementById('h_inst').innerHTML=I.map(([k])=>`<option>${k}</option>`).join('');wltVal()}
function wltVal(){const i=document.getElementById('h_inst').value;const v=ASSETS.instruments[i];document.getElementById('h_val').innerHTML=v.valuations.map(x=>`<option${x===v.default_valuation?' selected':''}>${x}</option>`).join('')}
async function wltAdd(){const g=id=>document.getElementById(id).value;const b={asset_class:g('h_class'),instrument:g('h_inst'),valuation:g('h_val'),name:g('h_name'),quantity:g('h_qty')===''?null:Number(g('h_qty'))};
 if(g('h_sym'))b.symbol=g('h_sym');if(g('h_cost')!=='')b.avg_cost=Number(g('h_cost'));if(g('h_px')!=='')b.manual_price=Number(g('h_px'));if(g('h_cur'))b.currency=g('h_cur');if(g('h_fx')!=='')b.fx_rate=Number(g('h_fx'));
 try{await post('/api/wealth/holdings',b);msg('wlt_msg','Holding added.');LOADERS.wlt()}catch(e){msg('wlt_msg',e.message,1)}}
async function wltClose(id){if(!confirm('Close this holding? It is kept in history with status CLOSED.'))return;try{await del('/api/wealth/holdings/'+id);LOADERS.wlt()}catch(e){msg('wlt_msg',e.message,1)}}
async function wltEditPx(id){const v=prompt('New manual price per unit (Rs, or foreign currency for FX_MANUAL):');if(v==null||v==='')return;try{await put('/api/wealth/holdings/'+id,{manual_price:Number(v)});LOADERS.wlt()}catch(e){msg('wlt_msg',e.message,1)}}
async function wltLiab(){const g=id=>document.getElementById(id).value;const b={kind:g('l_kind'),name:g('l_name'),outstanding:g('l_out')===''?null:Number(g('l_out'))};if(g('l_rate')!=='')b.interest_pct=Number(g('l_rate'));if(g('l_emi')!=='')b.emi=Number(g('l_emi'));
 try{await post('/api/wealth/liabilities',b);LOADERS.wlt()}catch(e){msg('wlt_msg',e.message,1)}}
async function wltLiabClose(id){if(!confirm('Mark this liability closed?'))return;try{await del('/api/wealth/liabilities/'+id);LOADERS.wlt()}catch(e){msg('wlt_msg',e.message,1)}}
async function wltCls(){try{await put('/api/wealth/classifications',{symbol:document.getElementById('c_sym').value,asset_class:document.getElementById('c_class').value,instrument:document.getElementById('c_inst').value});LOADERS.wlt()}catch(e){msg('wlt_msg',e.message,1)}}
async function wltClsDel(s){try{await del('/api/wealth/classifications/'+encodeURIComponent(s));LOADERS.wlt()}catch(e){msg('wlt_msg',e.message,1)}}
async function wltSnap(){try{const r=await post('/api/wealth/snapshots');msg('wlt_msg','Snapshot recorded for '+r.date+': net worth '+inr(r.net_worth));LOADERS.wlt()}catch(e){msg('wlt_msg',e.message,1)}}
LOADERS.wlt=async()=>{
 if(!ASSETS){ASSETS=await j('/api/wealth/assets');const cls=Object.keys(ASSETS.allocation_classes).map(c=>`<option>${c}</option>`).join('');
  document.getElementById('h_class').innerHTML=cls;document.getElementById('c_class').innerHTML=cls;wltInst();
  document.getElementById('l_kind').innerHTML=LIAB.map(k=>`<option>${k}</option>`).join('');
  document.getElementById('wlt_excl').textContent='Not supported (out of scope): '+Object.keys(ASSETS.excluded).join(', ')+'.'}
 const S=await j('/api/wealth/summary?positions=true');const T=S.totals;
 document.getElementById('wlt_asof').textContent=' as of '+String(S.as_of).slice(0,16);
 document.getElementById('wlt_cards').innerHTML=card('Net worth',inr(T.net_worth))+card('Gross assets',inr(T.gross_assets))+card('Liabilities',inr(T.liabilities))+card('Invested (ex-cash)',inr(T.invested_value),'cost '+inr(T.invested_cost))+card('Unrealized P&L',`<span class="${T.unrealized<0?'bad':'good'}">${inr(T.unrealized)}</span>`,pct(T.unrealized_pct))+
  (S.portfolio_health&&S.portfolio_health.phs!=null?card('Portfolio health',num(S.portfolio_health.phs,0),S.portfolio_health.band,S.portfolio_health.phs):'');
 document.getElementById('wlt_class').innerHTML=grp(S.by_asset_class,'Asset class');
 document.getElementById('wlt_src').innerHTML=grp(S.by_source,'Source');document.getElementById('wlt_sec').innerHTML=grp(S.equity_by_sector,'Sector');
 const c=S.concentration;document.getElementById('wlt_conc').innerHTML=`<div class="grid">${card('Largest holding',pct(c.largest_pct))}${card('Top 5',pct(c.top5_pct))}${card('HHI',num(c.hhi,0),'effective holdings '+num(c.effective_holdings,1))}${Object.entries(S.liquidity).map(([k,v])=>card('Liquidity '+k,pct(v.pct),inr(v.value))).join('')}</div>`+
  (c.single_stock_breaches.length?`<div class="warn" style="margin-top:6px">Above the ${c.single_stock_cap_pct}% single-stock cap: ${c.single_stock_breaches.map(b=>esc(b.symbol)+' '+pct(b.weight_pct)).join(', ')}</div>`:'');
 document.getElementById('wlt_risk').innerHTML=table(['Symbol','#Value','#CRI','Signal','#ATIP score'],S.holdings_at_risk.map(r=>`<tr><td>${esc(r.symbol)}</td><td class="n">${inr(r.value)}</td><td class="n bad">${num(r.cri,0)}</td><td>${esc(r.signal)}</td><td class="n">${num(r.atip_score,1)}</td></tr>`));
 document.getElementById('wlt_pos').innerHTML=table(['Source','Class','Instrument','Name / symbol','#Qty','#Avg cost','#Price','Price source','#Value','#Unrealized','ATIP',''],S.positions.map(p=>`<tr${p.include_in_net_worth?'':' class="muted"'}><td>${p.source}</td><td>${p.asset_class}</td><td>${p.instrument}</td><td>${esc(p.name)}${p.symbol&&p.symbol!==p.name?' <span class="muted">'+esc(p.symbol)+'</span>':''}${p.stale?' <span class="warn">stale</span>':''}</td><td class="n">${num(p.quantity,2)} <span class="muted">${esc(p.unit)}</span></td><td class="n">${p.avg_cost==null?'—':num(p.avg_cost)}</td><td class="n">${p.price==null?'—':num(p.price)}</td><td class="muted" style="font-size:11px">${esc(p.price_source)}${p.price_as_of?' · '+esc(p.price_as_of):''}</td><td class="n">${inr(p.value)}</td><td class="n ${p.unrealized<0?'bad':'good'}">${p.unrealized==null?'—':inr(p.unrealized)+' ('+pct(p.unrealized_pct)+')'}</td><td>${p.atip?'score '+num(p.atip.atip_score,0)+' · CRI '+num(p.atip.cri,0)+' · '+esc(p.atip.signal):''}</td><td>${p.holding_id?`<button onclick="wltEditPx('${p.holding_id}')" title="update manual price">₹</button> <button onclick="wltClose('${p.holding_id}')">✕</button>`:''}</td></tr>`));
 document.getElementById('wlt_liab').innerHTML=table(['Kind','Name','#Outstanding','#Rate','#EMI',''],S.liabilities.map(l=>`<tr><td>${l.kind}</td><td>${esc(l.name)}</td><td class="n">${inr(l.outstanding)}</td><td class="n">${pct(l.interest_pct,2)}</td><td class="n">${inr(l.emi)}</td><td><button onclick="wltLiabClose('${l.liability_id}')">✕</button></td></tr>`));
 const R=S.reconciliation;document.getElementById('wlt_rec').innerHTML=`<div>Broker: ${esc(R.broker.status)}${R.broker.book_date?' · synced '+esc(R.broker.book_date)+' · broker '+inr(R.broker.broker_reported_value)+' vs ATIP-marked '+inr(R.broker.atip_marked_value)+' (difference '+inr(R.broker.difference)+')':''}</div><div class="muted">${esc(R.broker.note||'')}</div><div>Paper book: ${R.paper_book.positions} positions, ${inr(R.paper_book.value)} (${esc(R.paper_book.note)})</div>`+
  (S.data_quality.length?'<h3>Data quality</h3>'+S.data_quality.map(d=>`<div class="warn">${esc(d.name)}: ${esc(d.issue)}</div>`).join(''):'<div class="good">No stale or missing prices.</div>')+
  `<div class="muted" style="margin-top:6px">Gold ${inr(S.spot.gold_inr_g.price)}/g · silver ${inr(S.spot.silver_inr_g.price)}/g · USD/INR ${num(S.spot.usd_inr,2)} (${esc(S.spot.gold_inr_g.source)})</div>`;
 const O=await j('/api/wealth/classifications');document.getElementById('wlt_cls').innerHTML=table(['Symbol','Class','Instrument',''],Object.entries(O).map(([k,v])=>`<tr><td>${esc(k)}</td><td>${v.asset_class}</td><td>${v.instrument}</td><td><button onclick="wltClsDel('${esc(k)}')">✕</button></td></tr>`));
 document.getElementById('wlt_hist').innerHTML=spark(await j('/api/wealth/snapshots'));
};
"""

TAB = ("wlt", "Wealth", HTML, JS)
