"""ATIP — FastAPI Dashboard Server (http://localhost:8000)"""
import json, logging
from datetime import date, datetime
from pathlib import Path
from db.schema import get_connection
from data.companies import load_company_names

log=logging.getLogger(__name__)
STATE_PATH=Path("atip_data/dashboard_state.json")

try:
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse
    import uvicorn; HAS_FASTAPI=True
except ImportError:
    HAS_FASTAPI=False; log.warning("pip install fastapi uvicorn")

def q(conn,sql,*params):
    try: rows=conn.execute(sql,params).fetchall(); return [dict(r) for r in rows]
    except: return []

def q1(conn,sql,*params):
    try: r=conn.execute(sql,params).fetchone(); return dict(r) if r else {}
    except: return {}

def get_scores(td,limit=50):
    conn=get_connection()
    try: return q(conn,"SELECT s.*,p.close as cmp,t.rsi_14,t.adx_14,t.atr_pct,t.volume_ratio FROM ai_scores s LEFT JOIN prices_daily p ON s.symbol=p.symbol AND p.date=? LEFT JOIN technical_indicators t ON s.symbol=t.symbol AND t.date=? WHERE s.date=? ORDER BY s.atip_score DESC LIMIT ?",str(td),str(td),str(td),limit)
    finally: conn.close()

def latest_scored_date():
    """
    The most recent trading day that actually has AI scores, rather than
    assuming date.today(). The post-market pipeline resolves its own target
    trading day (previous trading day if run before 4 PM IST, or on a
    weekend/holiday) — so "today" and "the date the data lives under" are
    often different. Live dashboard routes must follow the data, not the
    calendar, or they show whatever stale/partial row happens to exist for
    today's literal date while the real, complete data sits under yesterday.
    """
    conn = get_connection()
    try:
        row = conn.execute("SELECT MAX(date) d FROM ai_scores").fetchone()
        return row["d"] if row and row["d"] else str(date.today())
    finally:
        conn.close()

def get_mh(td):
    conn=get_connection()
    try: return q1(conn,"SELECT * FROM market_health WHERE date=?",str(td))
    finally: conn.close()

def get_indexes(td):
    conn=get_connection()
    try: return q1(conn,"SELECT * FROM index_levels WHERE date=? ORDER BY time DESC LIMIT 1",str(td))
    finally: conn.close()

def get_global(td):
    conn=get_connection()
    try: return q1(conn,"SELECT * FROM global_markets WHERE date<=? ORDER BY date DESC LIMIT 1",str(td))
    finally: conn.close()

def get_tod(td):
    conn=get_connection()
    try:
        d=q1(conn,"SELECT s.*,p.close as cmp,t.atr_14 FROM ai_scores s LEFT JOIN prices_daily p ON s.symbol=p.symbol AND p.date=? LEFT JOIN technical_indicators t ON s.symbol=t.symbol AND t.date=? WHERE s.date=? AND s.is_tod=1 LIMIT 1",str(td),str(td),str(td))
        if d.get("cmp") and d.get("atr_14"):
            d["sl"]=round(d["cmp"]-1.5*d["atr_14"],2); d["t1"]=round(d["cmp"]+2.0*d["atr_14"],2); d["t2"]=round(d["cmp"]+3.5*d["atr_14"],2)
        return d
    finally: conn.close()

def get_news(limit=20):
    conn=get_connection()
    try: return q(conn,"SELECT headline,source,category,sentiment,importance,news_score,ai_summary,fetched_at FROM news_articles ORDER BY fetched_at DESC LIMIT ?",limit)
    finally: conn.close()

def get_portfolio(td):
    conn=get_connection()
    try: return q(conn,"SELECT * FROM portfolio_holdings WHERE date=? ORDER BY weight_pct DESC",str(td))
    finally: conn.close()

def get_top25(td):
    conn=get_connection()
    try:
        def t25(col,where=""):
            return q(conn,f"SELECT s.symbol,s.atip_score,s.{col},s.signal,s.cri,s.acs,s.beta_1y,p.close as cmp FROM ai_scores s LEFT JOIN prices_daily p ON s.symbol=p.symbol AND p.date=s.date WHERE s.date=? {where} ORDER BY s.{col} DESC LIMIT 25",str(td))
        return {"vpi":t25("vpi"),"rri":t25("rri"),"mri":t25("mri"),"zpi":t25("zpi","AND s.signal='BUY'"),"cri":t25("cri","AND s.cri>60")}
    finally: conn.close()

def generate_state(td=None):
    if td is None: td=latest_scored_date()
    state={"generated_at":str(datetime.now()),"trade_date":str(td),"scores":get_scores(td),"mh":get_mh(td),"indexes":get_indexes(td),"global":get_global(td),"tod":get_tod(td),"news":get_news(),"portfolio":get_portfolio(td),"top25":get_top25(td)}
    STATE_PATH.parent.mkdir(exist_ok=True); STATE_PATH.write_text(json.dumps(state,default=str))
    return state

def pill(val,inv=False):
    if val is None: return "—"
    v=float(val)
    if inv: v=100-v
    col="#059669" if v>=70 else "#f59e0b" if v>=45 else "#dc2626"
    return f'<span style="background:{col}20;color:{col};padding:2px 7px;border-radius:4px;font-weight:600;font-size:11px">{float(val):.0f}</span>'

def bval(v):
    """Render a beta figure: >1 (more volatile than Nifty) in amber, <1 in slate, missing as a dash."""
    if v is None: return "—"
    v=float(v)
    col="#f59e0b" if v>1 else "#94a3b8"
    return f'<span style="color:{col};font-weight:600">{v:.2f}</span>'

def build_html(state):
    mh=state.get("mh",{}); tod=state.get("tod",{}); idx=state.get("indexes",{}); glb=state.get("global",{})
    scores=state.get("scores",[]); news=state.get("news",[]); port=state.get("portfolio",[]); top25=state.get("top25",{})
    mh_s=mh.get("mh_score",0) or 0; regime=mh.get("regime","—")
    mh_col="#059669" if mh_s>=60 else "#f59e0b" if mh_s>=40 else "#dc2626"
    gen=state.get("generated_at","")[:16]
    def chg(v): c="#059669" if (v or 0)>0 else "#dc2626"; return f'<span style="color:{c};font-weight:600">{(v or 0):+.2f}%</span>'
    names=load_company_names()
    def cname(sym):
        n=names.get((sym or "").upper())
        return f'<div style="font-size:9.5px;color:#64748b;font-weight:400;white-space:normal">{n}</div>' if n else ""
    score_rows="".join(f"""<tr data-sym="{r.get('symbol')}"><td>{r.get('atip_rank','')}</td><td><b>{r.get('symbol')}</b>{'⭐' if r.get('is_tod') else ''}{cname(r.get('symbol'))}</td><td>{pill(r.get('atip_score'))}</td><td>{pill(r.get('vpi'))}</td><td>{pill(r.get('mri'))}</td><td>{pill(r.get('rri'))}</td><td>{pill(r.get('zpi'))}</td><td>{pill(r.get('cri'),inv=True)}</td><td>{pill(r.get('acs'))}</td><td class="cmpcell" data-eod="{r.get('cmp') or ''}">₹{r.get('cmp') or '—'}</td><td>{bval(r.get('beta_1y'))}</td><td style="color:{'#059669' if r.get('signal')=='BUY' else '#dc2626' if r.get('signal')=='SELL' else '#2563eb'};font-weight:600">{r.get('signal','—')}</td><td style="font-size:10px;color:#64748b">{r.get('top_factor_1','')}</td><td><button class="ob" onclick="openOrderModal('{r.get('symbol')}',{r.get('cmp') or 0})">Order</button></td></tr>""" for r in scores[:50])
    news_rows="".join(f"""<tr><td style="font-size:12px;max-width:300px">{n.get('headline','')}</td><td style="font-size:11px">{n.get('source','')}</td><td style="color:{'#dc2626' if n.get('importance')=='HIGH' else '#f59e0b'};font-size:11px;font-weight:600">{n.get('importance','')}</td><td style="color:{'#059669' if (n.get('sentiment') or 0)>0.1 else '#dc2626' if (n.get('sentiment') or 0)<-0.1 else '#64748b'};font-weight:600">{(n.get('sentiment') or 0):+.2f}</td></tr>""" for n in news[:15])
    port_rows="".join(f"""<tr data-sym="{p.get('symbol')}"><td><b>{p.get('symbol')}</b>{cname(p.get('symbol'))}</td><td>{p.get('qty')}</td><td>₹{p.get('avg_price') or '—'}</td><td class="cmpcell" data-eod="{p.get('cmp') or ''}">₹{p.get('cmp') or '—'}</td><td style="color:{'#059669' if (p.get('pnl_pct') or 0)>=0 else '#dc2626'};font-weight:600">{(p.get('pnl_pct') or 0):+.1f}%</td><td>{pill(p.get('atip_score'))}</td><td>{pill(p.get('cri'),inv=True)}</td><td style="font-size:11px">{p.get('signal','—')}</td></tr>""" for p in port)
    idx_rows="".join(f'<div style="display:flex;justify-content:space-between;margin:4px 0;font-size:12px"><span style="color:#94a3b8">{k}</span>{chg(idx.get(v))}</div>' for k,v in [("Nifty50","nifty50_chg"),("BankNifty","banknifty_chg"),("Midcap150","midcap150_chg"),("SmallCap250","smallcap250_chg"),("IT","nifty_it_chg"),("Auto","nifty_auto_chg"),("FMCG","nifty_fmcg_chg"),("Metal","nifty_metal_chg"),("Realty","nifty_realty_chg"),("PSUBank","nifty_psubank_chg"),("Energy","nifty_energy_chg"),("Pharma","nifty_pharma_chg")])
    glb_rows="".join(f'<div style="display:flex;justify-content:space-between;margin:4px 0;font-size:12px"><span style="color:#94a3b8">{k}</span>{chg(glb.get(v))}</div>' for k,v in [("S&P500","sp500_chg"),("Dow","dow_chg"),("Nasdaq","nasdaq_chg"),("Nikkei","nikkei_chg"),("Crude","crude_wti_chg"),("Gold","gold_chg"),("USD/INR","usd_inr_chg")])
    top25_rows={"vpi":"".join(f"<tr data-sym=\"{r.get('symbol')}\"><td><b>{r.get('symbol')}</b>{cname(r.get('symbol'))}</td><td>{pill(r.get('atip_score'))}</td><td>{pill(r.get('vpi'))}</td><td class=\"cmpcell\" data-eod=\"{r.get('cmp') or ''}\">₹{r.get('cmp') or '—'}</td><td>{bval(r.get('beta_1y'))}</td><td>{r.get('signal','—')}</td></tr>" for r in top25.get("vpi",[])),
                "zpi":"".join(f"<tr data-sym=\"{r.get('symbol')}\"><td><b>{r.get('symbol')}</b>{cname(r.get('symbol'))}</td><td>{pill(r.get('zpi'))}</td><td>{pill(r.get('atip_score'))}</td><td class=\"cmpcell\" data-eod=\"{r.get('cmp') or ''}\">₹{r.get('cmp') or '—'}</td><td>{bval(r.get('beta_1y'))}</td><td style='color:#059669;font-weight:600'>{r.get('signal','—')}</td></tr>" for r in top25.get("zpi",[])),
                "cri":"".join(f"<tr data-sym=\"{r.get('symbol')}\"><td><b>{r.get('symbol')}</b>{cname(r.get('symbol'))}</td><td style='color:#dc2626;font-weight:700'>{r.get('cri',0):.0f}</td><td>{pill(r.get('atip_score'))}</td><td class=\"cmpcell\" data-eod=\"{r.get('cmp') or ''}\">₹{r.get('cmp') or '—'}</td><td>{bval(r.get('beta_1y'))}</td><td style='color:#dc2626'>{r.get('signal','—')}</td></tr>" for r in top25.get("cri",[]))}
    tod_sym=tod.get('symbol','—'); tod_sig=tod.get('signal','—'); tod_cmp=tod.get('cmp','—')
    tod_cname=names.get((tod.get('symbol') or "").upper(),"")
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ATIP Dashboard</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}body{{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;font-size:13px}}.topbar{{background:#1e293b;padding:10px 18px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #334155}}.logo{{font-size:17px;font-weight:700;color:#38bdf8}}.kpi-row{{display:flex;gap:8px;padding:10px 18px;flex-wrap:wrap;background:#1e293b;border-bottom:1px solid #334155}}.kpi{{background:#0f172a;border:1px solid #334155;border-radius:8px;padding:8px 14px;min-width:100px}}.kpi-l{{font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px}}.kpi-v{{font-size:20px;font-weight:700}}.body{{display:flex}}.sidebar{{width:200px;background:#1e293b;border-right:1px solid #334155;padding:12px;overflow-y:auto;min-height:100vh}}.sidebar h3{{font-size:10px;color:#64748b;text-transform:uppercase;margin-bottom:6px;margin-top:14px}}.sidebar h3:first-child{{margin-top:0}}.main{{flex:1;padding:14px;overflow-x:auto}}.section{{margin-bottom:20px}}.st{{font-size:13px;font-weight:600;color:#38bdf8;margin-bottom:8px;padding-bottom:5px;border-bottom:1px solid #334155}}.tod-card{{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:14px;display:grid;grid-template-columns:1fr 1fr;gap:10px}}.tod-sym{{font-size:24px;font-weight:700;grid-column:1/-1}}.tod-l{{font-size:11px;color:#94a3b8}}.tod-v{{font-size:13px;font-weight:600}}.tabs{{display:flex;gap:4px;margin-bottom:10px}}.tab{{padding:5px 12px;border-radius:6px;font-size:12px;cursor:pointer;border:1px solid #334155;background:#1e293b;color:#94a3b8}}.tab.active{{background:#2563eb;color:#fff;border-color:#2563eb}}.tc{{display:none}}.tc.active{{display:block}}table{{width:100%;border-collapse:collapse;background:#1e293b;border-radius:8px;overflow:hidden;font-size:11.5px}}th{{background:#0f172a;color:#94a3b8;padding:7px 7px;text-align:left;border-bottom:1px solid #334155;font-size:11px;cursor:pointer;white-space:nowrap}}th:hover{{color:#e2e8f0}}td{{padding:6px 7px;border-bottom:1px solid #1e293b22;white-space:nowrap}}tr:hover td{{background:#0f172a}}.disc{{font-size:10px;color:#475569;margin-top:16px;padding-top:10px;border-top:1px solid #334155;line-height:1.6}}input,select{{padding:5px 10px;border-radius:6px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;font-size:12px}}.rf{{background:#2563eb;color:#fff;border:none;padding:5px 12px;border-radius:6px;cursor:pointer;font-size:12px}}
.ob{{background:#1e293b;border:1px solid #38bdf8;color:#38bdf8;padding:3px 10px;border-radius:5px;font-size:11px;cursor:pointer}}.ob:hover{{background:#38bdf8;color:#0f172a}}
.modal-bg{{display:none;position:fixed;inset:0;background:#00000090;z-index:100;align-items:center;justify-content:center}}.modal-bg.open{{display:flex}}
.modal{{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:18px;width:420px;max-width:92vw;max-height:88vh;overflow-y:auto}}
.modal h3{{font-size:15px;margin-bottom:4px}}.modal .sub{{font-size:11px;color:#64748b;margin-bottom:12px}}
.mrow{{display:flex;align-items:center;gap:8px;margin-bottom:9px;flex-wrap:wrap}}.mrow label{{width:64px;font-size:12px;color:#94a3b8;flex-shrink:0}}
.mrow input,.mrow select{{flex:1;min-width:90px}}
.seg{{display:inline-flex;border:1px solid #334155;border-radius:6px;overflow:hidden}}.seg button{{border:none;background:#0f172a;color:#94a3b8;padding:5px 16px;font-size:12px;cursor:pointer}}.seg button.on[data-v="BUY"]{{background:#059669;color:#fff}}.seg button.on[data-v="SELL"]{{background:#dc2626;color:#fff}}
.preview{{background:#0f172a;border:1px dashed #334155;border-radius:6px;padding:9px;font-size:12px;color:#cbd5e1;margin:10px 0;line-height:1.5}}
.mactions{{display:flex;gap:8px;margin-top:6px}}.mactions button{{flex:1;padding:8px;border:none;border-radius:6px;font-size:12.5px;cursor:pointer}}.msave{{background:#2563eb;color:#fff}}.mcancel{{background:#334155;color:#e2e8f0}}
.banner{{font-size:11.5px;padding:5px 12px}}.banner.dry{{background:#fef9c3;color:#78350f}}.banner.live{{background:#fecaca;color:#7f1d1d;font-weight:700}}
.pend{{background:#1e293b;border:1px solid #dc2626;border-radius:8px;padding:8px 12px;margin-bottom:10px;display:none}}.pend.show{{display:block}}
.pend-item{{display:flex;justify-content:space-between;align-items:center;padding:5px 0;font-size:12px;border-top:1px solid #33415555}}.pend-item:first-of-type{{border-top:none}}
.pend-item button{{border:none;border-radius:4px;padding:3px 10px;font-size:11px;cursor:pointer;margin-left:5px}}.pconf{{background:#059669;color:#fff}}.prej{{background:#dc2626;color:#fff}}
.rules-mini{{margin-top:12px;font-size:11.5px}}.rules-mini li{{display:flex;justify-content:space-between;background:#0f172a;border-radius:5px;padding:5px 8px;margin-bottom:4px;list-style:none}}
.hide{{display:none!important}}
</style></head>
<body>
<div class="topbar"><div><span class="logo">📊 ATIP</span> <span style="color:#64748b">AI Trading Intelligence Platform</span></div><div style="display:flex;gap:10px;align-items:center"><span id="clk" style="font-size:11px;color:#94a3b8"></span><span style="font-size:11px;color:#64748b">Data as of: {gen}</span><button class="rf" onclick="location.reload()">↻ Refresh</button></div></div>
<div id="brokerBanner" class="banner dry">Checking broker status…</div>
<div id="pendBox" class="pend" style="margin:10px 18px 0"><b style="color:#dc2626">⚠️ Awaiting confirmation</b><div id="pendList"></div></div>
<div class="kpi-row">
  <div class="kpi"><div class="kpi-l">Market Health</div><div class="kpi-v" style="color:{mh_col}">{mh_s:.0f}</div><div style="font-size:11px;color:#64748b">{regime}</div></div>
  <div class="kpi"><div class="kpi-l">Nifty 50</div><div class="kpi-v">{chg(idx.get('nifty50_chg'))}</div></div>
  <div class="kpi"><div class="kpi-l">Bank Nifty</div><div class="kpi-v">{chg(idx.get('banknifty_chg'))}</div></div>
  <div class="kpi"><div class="kpi-l">India VIX</div><div class="kpi-v" style="color:{'#dc2626' if (idx.get('india_vix') or 0)>20 else '#94a3b8'}">{idx.get('india_vix','—')}</div></div>
  <div class="kpi"><div class="kpi-l">Sentiment</div><div class="kpi-v" style="color:{'#059669' if idx.get('overall_sentiment')=='BULLISH' else '#dc2626' if idx.get('overall_sentiment')=='BEARISH' else '#94a3b8'}">{idx.get('overall_sentiment') or '—'}</div></div>
  <div class="kpi"><div class="kpi-l">S&P 500</div><div class="kpi-v">{chg(glb.get('sp500_chg'))}</div></div>
  <div class="kpi"><div class="kpi-l">Gold</div><div class="kpi-v">{chg(glb.get('gold_chg'))}</div></div>
  <div class="kpi"><div class="kpi-l">USD/INR</div><div class="kpi-v">{chg(glb.get('usd_inr_chg'))}</div></div>
</div>
<div class="body">
<div class="sidebar"><h3>NSE Indexes</h3>{idx_rows}<h3>Global</h3>{glb_rows}</div>
<div class="main">
  <div class="section"><div class="st">🎯 Trade of the Day</div>
    <div class="tod-card" data-sym="{tod_sym}">
      <div class="tod-sym">{tod_sym} <span style="font-size:13px;color:#059669">{tod_sig}</span>{f'<div style="font-size:12px;color:#94a3b8;font-weight:400">{tod_cname}</div>' if tod_cname else ''}</div>
      <div><div class="tod-l">CMP <span style="color:#38bdf8">●live</span></div><div class="tod-v cmpcell" data-eod="{tod.get('cmp') or ''}">₹{tod_cmp}</div></div>
      <div><div class="tod-l">Stop Loss</div><div class="tod-v" style="color:#dc2626">₹{tod.get('sl','—')}</div></div>
      <div><div class="tod-l">Target 1</div><div class="tod-v" style="color:#059669">₹{tod.get('t1','—')}</div></div>
      <div><div class="tod-l">Target 2</div><div class="tod-v" style="color:#059669">₹{tod.get('t2','—')}</div></div>
      <div><div class="tod-l">ACS Confidence</div><div class="tod-v">{(tod.get('acs') or 0):.0f}/100</div></div>
      <div style="grid-column:1/-1;font-size:11px;color:#94a3b8">✦ {tod.get('top_factor_1','—')} &nbsp; ✦ {tod.get('top_factor_2','—')}</div>
    </div>
  </div>
  <div class="tabs"><div class="tab active" onclick="showTab('scores',this)">ATIP Scores</div><div class="tab" onclick="showTab('port',this)">Portfolio</div><div class="tab" onclick="showTab('vpi',this)">Top VPI</div><div class="tab" onclick="showTab('zpi',this)">Buy Zones</div><div class="tab" onclick="showTab('cri',this)">CRI Risk</div><div class="tab" onclick="showTab('news',this)">News</div></div>
  <div id="scores" class="tc active section">
    <div style="display:flex;gap:6px;margin-bottom:8px"><input id="srch" placeholder="Search…" oninput="ft()"><select id="sf" onchange="ft()"><option value="">All signals</option><option>BUY</option><option>SELL</option><option>HOLD</option><option>WAIT</option></select></div>
    <table id="st"><thead><tr><th onclick="srt('st',0)">#</th><th onclick="srt('st',1)">Symbol</th><th onclick="srt('st',2)">ATIP</th><th onclick="srt('st',3)">VPI</th><th onclick="srt('st',4)">MRI</th><th onclick="srt('st',5)">RRI</th><th onclick="srt('st',6)">ZPI</th><th onclick="srt('st',7)">CRI↓</th><th onclick="srt('st',8)">ACS</th><th onclick="srt('st',9)">CMP <span style="color:#38bdf8">●live</span></th><th onclick="srt('st',10)">Beta</th><th onclick="srt('st',11)">Signal</th><th>Factor</th><th>Order</th></tr></thead><tbody>{score_rows}</tbody></table>
  </div>
  <div id="port" class="tc section"><table><thead><tr><th>Symbol</th><th>Qty</th><th>Avg</th><th>CMP</th><th>P&L%</th><th>ATIP</th><th>CRI↓</th><th>Action</th></tr></thead><tbody>{port_rows if port_rows else '<tr><td colspan="8" style="text-align:center;color:#64748b;padding:20px">No portfolio data. Configure Zerodha Kite API and run --login-zerodha</td></tr>'}</tbody></table></div>
  <div id="vpi" class="tc section"><table><thead><tr><th>Symbol</th><th>ATIP</th><th>VPI</th><th>CMP <span style="color:#38bdf8">●live</span></th><th>Beta</th><th>Signal</th></tr></thead><tbody>{top25_rows.get('vpi','')}</tbody></table></div>
  <div id="zpi" class="tc section"><table><thead><tr><th>Symbol</th><th>ZPI</th><th>ATIP</th><th>CMP <span style="color:#38bdf8">●live</span></th><th>Beta</th><th>Signal</th></tr></thead><tbody>{top25_rows.get('zpi','')}</tbody></table></div>
  <div id="cri" class="tc section"><table><thead><tr><th>Symbol</th><th>CRI 🔴</th><th>ATIP</th><th>CMP <span style="color:#38bdf8">●live</span></th><th>Beta</th><th>Signal</th></tr></thead><tbody>{top25_rows.get('cri','')}</tbody></table></div>
  <div id="news" class="tc section"><table><thead><tr><th style="width:320px">Headline</th><th>Source</th><th>Importance</th><th>Sentiment</th></tr></thead><tbody>{news_rows}</tbody></table></div>
  <p class="disc">⚠️ ATIP is for personal informational use only. Not financial advice. All AI scores are model outputs — verify independently. Not SEBI registered. Consult a registered advisor before investing.</p>
</div></div>

<div class="modal-bg" id="modalBg"><div class="modal">
  <h3>Order Rule — <span id="mSym"></span></h3>
  <div class="sub" id="mCmp"></div>
  <div class="mrow"><label>Side</label><div class="seg" id="mSide"><button type="button" class="on" data-v="BUY" onclick="setSide('BUY')">Buy</button><button type="button" data-v="SELL" onclick="setSide('SELL')">Sell</button></div></div>
  <div class="mrow"><label>Target</label><select id="mTT" onchange="mPrev()"><option value="PRICE">At price ₹</option><option value="PERCENT">% move</option></select><input type="number" step="0.01" id="mTV" placeholder="e.g. 2500" oninput="mPrev()"></div>
  <div class="mrow"><label>Qty</label><select id="mQT" onchange="mPrev()"><option value="SHARES">Shares</option><option value="AMOUNT">₹ Amount</option></select><input type="number" step="0.01" id="mQV" placeholder="e.g. 10" oninput="mPrev()"></div>
  <div class="mrow"><label>Product</label><select id="mProd"><option value="CNC">Delivery</option><option value="INTRADAY">Intraday</option></select><select id="mOT" onchange="mPrev()"><option value="MARKET">Market</option><option value="LIMIT">Limit</option></select><input type="number" step="0.01" id="mLimit" class="hide" placeholder="Limit ₹"></div>
  <div class="mrow"><label><input type="checkbox" id="mSL" checked onchange="mPrev()"> Stoploss</label><select id="mSLT" onchange="mPrev()"><option value="PERCENT">%</option><option value="AMOUNT">₹ move</option></select><input type="number" step="0.01" id="mSLV" placeholder="e.g. -2" value="-2" oninput="mPrev()"></div>
  <div class="mrow"><label style="width:auto"><input type="checkbox" id="mConfirm" checked> Require confirmation before order is placed</label></div>
  <div class="preview" id="mPreviewTxt">—</div>
  <div class="rules-mini"><b style="font-size:11px;color:#64748b">Active rules for this stock</b><ul id="mRulesList"></ul></div>
  <div class="mactions"><button class="msave" onclick="saveOrderRule()">Save Rule</button><button class="mcancel" onclick="closeOrderModal()">Close</button></div>
</div></div>
<script>
function showTab(id,el){{document.querySelectorAll('.tc').forEach(t=>t.classList.remove('active'));document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));document.getElementById(id).classList.add('active');el.classList.add('active');}}
let ss={{}};function srt(tid,col){{const tb=document.getElementById(tid);const rows=[...tb.querySelectorAll('tbody tr')];const asc=ss[tid+col]!==true;ss[tid+col]=asc;rows.sort((a,b)=>{{const av=a.cells[col]?.innerText.replace(/[^0-9.\\-]/g,'');const bv=b.cells[col]?.innerText.replace(/[^0-9.\\-]/g,'');const an=parseFloat(av),bn=parseFloat(bv);if(!isNaN(an)&&!isNaN(bn))return asc?an-bn:bn-an;return asc?(av||'').localeCompare(bv||''):(bv||'').localeCompare(av||'');}});const tbody=tb.querySelector('tbody');rows.forEach(r=>tbody.appendChild(r));}}
function ft(){{
  var q=(document.getElementById('srch')||{{value:''}}).value.toLowerCase();
  var s=(document.getElementById('sf')||{{value:''}}).value.toLowerCase();
  var rows=document.querySelectorAll('#st tbody tr');
  for(var i=0;i<rows.length;i++){{
    var txt=rows[i].innerText.toLowerCase();
    rows[i].style.display=(txt.indexOf(q)>=0&&(s===''||txt.indexOf(s)>=0))?'':'none';
  }}
}}

// ── Live clock + auto page refresh (topbar, right side) ────────────────
var AUTO_REFRESH_SECONDS=300, _secsLeft=AUTO_REFRESH_SECONDS;
function tickClock(){{
  var now=new Date();
  document.getElementById('clk').textContent='🕒 '+now.toLocaleTimeString()+'  ·  refresh in '+_secsLeft+'s';
  _secsLeft--;
  if(_secsLeft<0) location.reload();
}}
setInterval(tickClock,1000); tickClock();

// ── Order rule modal ─────────────────────────────────────────────────
var mSymbol='', mCmpVal=0, mSideVal='BUY';
function setSide(v){{mSideVal=v;document.querySelectorAll('#mSide button').forEach(b=>b.classList.toggle('on',b.dataset.v===v));mPrev();}}
function openOrderModal(sym,cmp){{
  mSymbol=sym; mCmpVal=cmp; mSideVal='BUY';
  document.getElementById('mSym').textContent=sym;
  document.getElementById('mCmp').textContent='CMP ₹'+cmp;
  document.querySelectorAll('#mSide button').forEach(b=>b.classList.toggle('on',b.dataset.v==='BUY'));
  document.getElementById('modalBg').classList.add('open');
  mPrev(); loadMiniRules();
}}
function closeOrderModal(){{document.getElementById('modalBg').classList.remove('open');}}
document.getElementById('mOT').addEventListener('change',function(){{document.getElementById('mLimit').classList.toggle('hide',this.value!=='LIMIT');}});
function mResolvedTrigger(){{
  var tt=document.getElementById('mTT').value, tv=parseFloat(document.getElementById('mTV').value);
  if(isNaN(tv))return null;
  return tt==='PRICE'?tv:+(mCmpVal*(1+tv/100)).toFixed(2);
}}
function mPrev(){{
  var qv=document.getElementById('mQV').value||'?', qt=document.getElementById('mQT').value==='SHARES'?'shares':'₹ worth';
  var tp=mResolvedTrigger();
  var slTxt='none';
  if(document.getElementById('mSL').checked && tp){{
    var slv=parseFloat(document.getElementById('mSLV').value);
    if(!isNaN(slv)){{
      var slt=document.getElementById('mSLT').value;
      var slp=slt==='PERCENT'?+(tp*(1+slv/100)).toFixed(2):+(tp+slv).toFixed(2);
      slTxt='₹'+slp.toFixed(2);
    }}
  }}
  var confTxt=document.getElementById('mConfirm').checked?'will ask you to confirm':'⚡ will execute automatically';
  document.getElementById('mPreviewTxt').textContent=(mSideVal==='BUY'?'Buy ':'Sell ')+qv+' '+qt+' of '+mSymbol+' at/'+(mSideVal==='BUY'?'below':'above')+' ₹'+(tp!==null?tp.toFixed(2):'—')+' | Stoploss: '+slTxt+' | '+confTxt;
}}
async function saveOrderRule(){{
  var tt=document.getElementById('mTT').value;
  var payload={{
    symbol:mSymbol, side:mSideVal, trigger_type:tt,
    trigger_value: tt==='PRICE'?parseFloat(document.getElementById('mTV').value):null,
    trigger_percent: tt==='PERCENT'?parseFloat(document.getElementById('mTV').value):null,
    reference_price:mCmpVal,
    quantity_type:document.getElementById('mQT').value, quantity_value:parseFloat(document.getElementById('mQV').value),
    product_type:document.getElementById('mProd').value, order_type:document.getElementById('mOT').value,
    limit_price: document.getElementById('mOT').value==='LIMIT'?parseFloat(document.getElementById('mLimit').value):null,
    require_confirmation: document.getElementById('mConfirm').checked,
    stoploss: document.getElementById('mSL').checked && document.getElementById('mSLV').value ? {{type:document.getElementById('mSLT').value, value:parseFloat(document.getElementById('mSLV').value)}} : null,
  }};
  var res=await fetch('/api/orders',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}});
  if(!res.ok){{var e=await res.json().catch(()=>({{}}));alert('Failed: '+(e.error||res.statusText));return;}}
  loadMiniRules();
}}
async function loadMiniRules(){{
  var res=await fetch('/api/orders?symbol='+encodeURIComponent(mSymbol)+'&status=ACTIVE');
  if(!res.ok)return;
  var rules=await res.json();
  var ul=document.getElementById('mRulesList'); ul.innerHTML='';
  if(!rules.length){{ul.innerHTML='<li style="justify-content:center;color:#64748b">No active rules</li>';return;}}
  rules.forEach(r=>{{
    var li=document.createElement('li');
    li.innerHTML='<span>'+r.side+' '+r.quantity_value+(r.quantity_type==='SHARES'?' sh':' ₹')+' @ ₹'+r.resolved_trigger_price.toFixed(2)+(r.require_confirmation?'':' ⚡')+'</span><button onclick="deleteRule(\\''+r.id+'\\')" style="background:none;border:none;color:#dc2626;cursor:pointer">✕</button>';
    ul.appendChild(li);
  }});
}}
async function deleteRule(id){{await fetch('/api/orders/'+id,{{method:'DELETE'}});loadMiniRules();}}

// ── Pending confirmations + broker status (polled every 5s) ────────────
async function pollPending(){{
  try{{
    var res=await fetch('/api/orders/pending');
    if(!res.ok)return;
    var rules=await res.json();
    var box=document.getElementById('pendBox'), list=document.getElementById('pendList');
    if(!rules.length){{box.classList.remove('show');return;}}
    box.classList.add('show'); list.innerHTML='';
    rules.forEach(r=>{{
      var d=document.createElement('div'); d.className='pend-item';
      d.innerHTML='<span>'+r.symbol+' '+r.side+' '+r.quantity_value+' hit ₹'+(r.trigger_hit_price||r.resolved_trigger_price).toFixed(2)+'</span><span><button class="pconf" onclick="confirmRule(\\''+r.id+'\\')">Confirm</button><button class="prej" onclick="rejectRule(\\''+r.id+'\\')">Reject</button></span>';
      list.appendChild(d);
    }});
  }}catch(e){{}}
}}
async function confirmRule(id){{await fetch('/api/orders/'+id+'/confirm',{{method:'POST'}});pollPending();}}
async function rejectRule(id){{await fetch('/api/orders/'+id+'/reject',{{method:'POST'}});pollPending();}}
setInterval(pollPending,5000); pollPending();
(async function(){{
  try{{
    var res=await fetch('/api/orders/broker-status'); var s=await res.json();
    var b=document.getElementById('brokerBanner');
    if(s.credentials_configured){{b.textContent='🔴 Dhan connected — tapping Confirm on a pending rule places a REAL order.';b.className='banner live';}}
    else{{b.textContent='🟡 Dhan credentials not configured (atip_data/config.json) — Confirm will fail until set.';b.className='banner dry';}}
  }}catch(e){{}}
}})();

// ── Live CMP feed (updates CMP cells across all tabs, no full reload) ──
async function pollLiveQuotes(){{
  try{{
    var res=await fetch('/api/live-quotes');
    if(!res.ok)return;
    var quotes=await res.json();
    document.querySelectorAll('[data-sym]').forEach(el=>{{
      var sym=el.dataset.sym, qd=quotes[sym];
      if(!qd || !qd.ltp) return;
      var cell = el.classList.contains('cmpcell') ? el : el.querySelector('.cmpcell');
      if(!cell) return;
      var eod=parseFloat(cell.dataset.eod);
      var chgFromEod = (!isNaN(eod) && eod>0) ? ((qd.ltp-eod)/eod*100) : null;
      var col = chgFromEod===null ? '#e2e8f0' : (chgFromEod>=0 ? '#059669' : '#dc2626');
      cell.innerHTML = '₹'+qd.ltp.toFixed(2) + (chgFromEod!==null ? ' <span style="font-size:10px;color:'+col+'">'+(chgFromEod>=0?'+':'')+chgFromEod.toFixed(1)+'%</span>' : '');
    }});
  }}catch(e){{}}
}}
setInterval(pollLiveQuotes,15000); pollLiveQuotes();
</script></body></html>"""

if HAS_FASTAPI:
    app=FastAPI(title="ATIP Dashboard",version="0.2")
    @app.get("/",response_class=HTMLResponse)
    async def dashboard():
        return HTMLResponse(content=build_html(generate_state(latest_scored_date())))
    @app.get("/api/scores")
    async def api_scores(): return JSONResponse(get_scores(latest_scored_date()))
    @app.get("/api/mh")
    async def api_mh(): return JSONResponse(get_mh(latest_scored_date()))
    @app.get("/api/tod")
    async def api_tod(): return JSONResponse(get_tod(latest_scored_date()))
    @app.get("/api/news")
    async def api_news(): return JSONResponse(get_news())
    @app.get("/api/portfolio")
    async def api_portfolio(): return JSONResponse(get_portfolio(latest_scored_date()))
    @app.post("/api/refresh")
    async def api_refresh():
        import threading
        from pipeline.scheduler import run_postmarket
        threading.Thread(target=run_postmarket,daemon=True).start()
        return JSONResponse({"status":"pipeline started"})

    # ── Buy/Sell target + stoploss rules ────────────────────────────────
    # See orders/rules.py docstring for why triggering (automatic)
    # and execution (confirmation-gated by default) are kept separate.
    from orders import rules as oe
    from fastapi import Request

    oe.init_orders_table()

    @app.post("/api/orders")
    async def api_create_order(request: Request):
        payload = await request.json()
        try:
            return JSONResponse(oe.create_rule(payload))
        except (ValueError, KeyError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.get("/api/orders")
    async def api_list_orders(symbol: str = None, status: str = None):
        return JSONResponse(oe.list_rules(symbol=symbol, status=status))

    @app.get("/api/orders/pending")
    async def api_pending_orders():
        return JSONResponse(oe.list_rules(status=oe.PENDING_CONFIRMATION))

    @app.post("/api/orders/{rule_id}/confirm")
    async def api_confirm_order(rule_id: str):
        rule = oe.get_rule(rule_id)
        if not rule:
            return JSONResponse({"error": "not found"}, status_code=404)
        if rule["status"] != oe.PENDING_CONFIRMATION:
            return JSONResponse({"error": f"rule is not pending confirmation (status: {rule['status']})"}, status_code=400)
        result = oe.execute_rule(rule_id, rule.get("trigger_hit_price"), confirm=True)
        return JSONResponse({"rule": oe.get_rule(rule_id), "result": result})

    @app.post("/api/orders/{rule_id}/reject")
    async def api_reject_order(rule_id: str):
        rule = oe.reject_rule(rule_id)
        if not rule:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(rule)

    @app.delete("/api/orders/{rule_id}")
    async def api_delete_order(rule_id: str):
        ok = oe.delete_rule(rule_id)
        if not ok:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"deleted": True})

    @app.get("/api/orders/broker-status")
    async def api_broker_status():
        from data.dhan import load_dhan_config
        cfg = load_dhan_config()
        configured = bool(cfg.get("dhan_client_id") and cfg.get("dhan_access_token"))
        return JSONResponse({"credentials_configured": configured})

    @app.get("/api/live-quotes")
    async def api_live_quotes():
        """Live LTP for every symbol currently shown on the dashboard (scores + portfolio)."""
        from data.dhan import fetch_live_quotes
        td = latest_scored_date()
        conn = get_connection()
        try:
            syms = {r["symbol"] for r in q(conn, "SELECT DISTINCT symbol FROM ai_scores WHERE date=?", str(td))}
            syms |= {r["symbol"] for r in q(conn, "SELECT DISTINCT symbol FROM portfolio_holdings WHERE date=?", str(td))}
        finally:
            conn.close()
        if not syms:
            return JSONResponse({})
        try:
            df = fetch_live_quotes(list(syms))
        except Exception as e:
            log.warning(f"  live quotes fetch failed: {e}")
            return JSONResponse({})
        if df.empty:
            return JSONResponse({})
        out = {}
        for _, row in df.iterrows():
            ltp = row.get("ltp")
            prev = row.get("prev_close")
            chg = round((ltp - prev) / prev * 100, 2) if ltp and prev else None
            out[row["symbol"]] = {"ltp": ltp, "chg_pct": chg}
        return JSONResponse(out)

    def _order_monitor_loop():
        """
        Runs only while the dashboard process is up — deliberately NOT part
        of pipeline/scheduler.py (see orders/rules.py docstring for
        why). Read-only: flags PENDING_CONFIRMATION, executes only rules
        explicitly marked require_confirmation=False.
        """
        import time
        from pipeline.scheduler import is_market_hours
        while True:
            try:
                if is_market_hours():
                    events = oe.check_triggers()
                    if events:
                        log.info(f"  order_rules: {events}")
            except Exception as e:
                log.error(f"  order_rules monitor error: {e}")
            time.sleep(30)

    @app.on_event("startup")
    async def _start_order_monitor():
        import threading
        threading.Thread(target=_order_monitor_loop, daemon=True).start()
        log.info("  ✓ Order-rules monitor started (flags triggers every 30s during market hours)")

if __name__=="__main__":
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(message)s")
    if HAS_FASTAPI:
        uvicorn.run("dashboard.server:app",host="0.0.0.0",port=8000,reload=False)
    else:
        print("pip install fastapi uvicorn")
