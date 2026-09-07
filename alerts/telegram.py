"""ATIP — Telegram Alert Engine (10 alert types)"""
import os, json, logging, requests
from datetime import date, datetime, timedelta
from pathlib import Path
from db.schema import get_connection

log = logging.getLogger(__name__)
CONFIG_PATH = Path("atip_data/config.json")

def load_config():
    cfg={}
    if CONFIG_PATH.exists():
        try: cfg=json.loads(CONFIG_PATH.read_text())
        except: pass
    cfg.setdefault("telegram_token",  os.getenv("ATIP_TELEGRAM_TOKEN",""))
    cfg.setdefault("telegram_chat_id",os.getenv("ATIP_TELEGRAM_CHAT_ID",""))
    return cfg

def _is_placeholder(v):
    """
    config_template.json ships values like YOUR_TELEGRAM_BOT_TOKEN. Those are
    non-empty, so the old `if not token` check let them through to the API,
    which 404s — an unconfigured install therefore looked like a network fault
    in the log. Every alert this system has attempted has failed that way.
    """
    s = str(v or "").strip()
    return (not s) or s.upper().startswith("YOUR") or s.upper() in ("XXX", "TODO", "CHANGEME")

def send_telegram(message, parse_mode="HTML"):
    cfg=load_config(); token=cfg.get("telegram_token"); chat=cfg.get("telegram_chat_id")
    if _is_placeholder(token) or _is_placeholder(chat):
        log.warning("[TELEGRAM NOT CONFIGURED — set telegram_token + telegram_chat_id in "
                    f"atip_data/config.json] {message[:80]}")
        return False
    try:
        r=requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id":chat,"text":message,"parse_mode":parse_mode},timeout=10)
        r.raise_for_status(); log.info("  ✓ Telegram sent"); return True
    except Exception as e:
        log.error(f"  Telegram failed: {e}"); return False

def fmt(emoji, title, body, footer=""):
    ts=datetime.now().strftime("%d %b %Y %H:%M IST")
    msg=f"{emoji} <b>ATIP — {title}</b>\n🕐 {ts}\n\n{body}"
    if footer: msg+=f"\n\n<i>{footer}</i>"
    return msg+"\n\n⚠️ <i>Not financial advice. Personal use only.</i>"

def check_gap_alert(trade_date=None):
    if trade_date is None: trade_date=date.today()
    conn=get_connection()
    try:
        r=conn.execute("SELECT gift_nifty FROM index_levels WHERE date=? ORDER BY time DESC LIMIT 1",(str(trade_date),)).fetchone()
        p=conn.execute("SELECT nifty50 FROM index_levels WHERE date<? ORDER BY date DESC,time DESC LIMIT 1",(str(trade_date),)).fetchone()
    finally: conn.close()
    if not r or not p or not r["gift_nifty"] or not p["nifty50"]: return False
    gap=(r["gift_nifty"]-p["nifty50"])/p["nifty50"]*100
    if abs(gap)>=0.8:
        d="🟢 GAP UP" if gap>0 else "🔴 GAP DOWN"
        send_telegram(fmt("📊","Market Open Gap",f"<b>GIFT Nifty:</b> {r['gift_nifty']:,.0f}\n<b>Prev Close:</b> {p['nifty50']:,.0f}\n<b>Gap:</b> {gap:+.2f}% ({d})"))
        return True
    return False

def check_vix_alert(trade_date=None):
    if trade_date is None: trade_date=date.today()
    conn=get_connection()
    try: rows=conn.execute("SELECT india_vix FROM index_levels WHERE date=? ORDER BY time",(str(trade_date),)).fetchall()
    finally: conn.close()
    if len(rows)<2: return False
    v1=rows[0]["india_vix"]; v2=rows[-1]["india_vix"]
    if v1 and v2 and (v2-v1)>=3.0:
        send_telegram(fmt("🚨","VIX Spike",f"<b>VIX:</b> {v2:.1f} (+{v2-v1:.1f})\n{'🚨 PANIC — avoid new positions' if v2>25 else '⚠️ High volatility — reduce sizing'}"))
        return True
    return False

def check_broad_selloff(trade_date=None):
    if trade_date is None: trade_date=date.today()
    conn=get_connection()
    try: row=conn.execute("SELECT * FROM index_levels WHERE date=? ORDER BY time DESC LIMIT 1",(str(trade_date),)).fetchone()
    finally: conn.close()
    if not row: return False
    bearish=[(c.replace("_chg","").upper(),row[c]) for c in ["nifty50_chg","banknifty_chg","midcap150_chg","smallcap250_chg"] if row[c] and row[c]<-1.0]
    if len(bearish)>=3:
        lines="\n".join(f"  🔴 {n}: {v:+.2f}%" for n,v in bearish)
        send_telegram(fmt("📉","Broad Selloff",f"<b>{len(bearish)} indexes down >1%:</b>\n{lines}\n\n🚨 Capital preservation mode"))
        return True
    return False

def check_zpi_alerts(trade_date=None):
    if trade_date is None: trade_date=date.today()
    conn=get_connection()
    try: rows=conn.execute("SELECT symbol,zpi,vpi,acs FROM ai_scores WHERE date=? AND zpi>=75 AND signal='BUY' AND cri<40 ORDER BY zpi DESC LIMIT 5",(str(trade_date),)).fetchall()
    finally: conn.close()
    if not rows: return 0
    lines="".join(f"\n🎯 <b>{r['symbol']}</b>  ZPI:{r['zpi']:.0f}  VPI:{r['vpi']:.0f}" for r in rows)
    send_telegram(fmt("🎯","ZPI Buy Zone",f"<b>Buy zone candidates:</b>{lines}"))
    return len(rows)

def check_cri_alerts(trade_date=None):
    if trade_date is None: trade_date=date.today()
    conn=get_connection()
    try:
        rows=conn.execute("""SELECT p.symbol,p.pnl_pct,s.cri,s.signal FROM portfolio_holdings p
            JOIN ai_scores s ON p.symbol=s.symbol AND s.date=?
            WHERE p.date=? AND s.cri>80 ORDER BY s.cri DESC""",(str(trade_date),str(trade_date))).fetchall()
    finally: conn.close()
    if not rows: return 0
    lines="".join(f"\n🚨 <b>{r['symbol']}</b>  CRI:{r['cri']:.0f}  P&L:{r['pnl_pct']:+.1f}%" for r in rows)
    send_telegram(fmt("🚨","CRI Danger",f"<b>High risk holdings:</b>{lines}\n\n⚠️ Review stop-loss"))
    return len(rows)

def send_tod_alert(trade_date=None):
    if trade_date is None: trade_date=date.today()
    conn=get_connection()
    try:
        tod=conn.execute("""SELECT s.*,p.close as cmp,t.atr_14 FROM ai_scores s
            LEFT JOIN prices_daily p ON s.symbol=p.symbol AND p.date=?
            LEFT JOIN technical_indicators t ON s.symbol=t.symbol AND t.date=?
            WHERE s.date=? AND s.is_tod=1 LIMIT 1""",(str(trade_date),str(trade_date),str(trade_date))).fetchone()
        mh=conn.execute("SELECT mh_score,regime FROM market_health WHERE date=?",(str(trade_date),)).fetchone()
    finally: conn.close()
    if not tod: return False
    cmp=tod["cmp"] or 0; atr=tod["atr_14"] or 0
    sl=round(cmp-1.5*atr,2) if atr else "—"; t1=round(cmp+2.0*atr,2) if atr else "—"; t2=round(cmp+3.5*atr,2) if atr else "—"
    body=(f"<b>Stock:</b> {tod['symbol']}  <b>Signal:</b> {tod['signal']}\n"
          f"<b>TOD Score:</b> {tod['tod_score']:.1f}  <b>ATIP:</b> {tod['atip_score']:.1f}\n\n"
          f"<b>CMP:</b> ₹{cmp:,.2f}\n<b>Stop Loss:</b> ₹{sl}\n<b>Target 1:</b> ₹{t1}\n<b>Target 2:</b> ₹{t2}\n\n"
          f"<b>Factors:</b>\n  ✦ {tod['top_factor_1'] or '—'}\n  ✦ {tod['top_factor_2'] or '—'}\n"
          f"\n<b>Market:</b> {(mh['regime'] if mh else '—')}  MH:{(mh['mh_score'] if mh else '—'):.0f}")
    send_telegram(fmt("🎯","Trade of the Day",body))
    return True

def check_fii_alert(trade_date=None):
    if trade_date is None: trade_date=date.today()
    conn=get_connection()
    try: row=conn.execute("SELECT fii_net_cr,dii_net_cr FROM fii_dii_market WHERE date=?",(str(trade_date),)).fetchone()
    finally: conn.close()
    if not row: return False
    fii=row["fii_net_cr"] or 0; dii=row["dii_net_cr"] or 0
    if fii<-2000:
        send_telegram(fmt("📉","FII Big Sell",f"<b>FII Net:</b> ₹{fii:,.0f} Cr\n<b>DII Net:</b> ₹{dii:+,.0f} Cr\n{'🛡️ DII cushioning' if dii>500 else '⚠️ No DII support'}"))
        return True
    return False

def send_failure_alert(job_name, error):
    send_telegram(fmt("❌","Pipeline Failure",f"<b>Job:</b> {job_name}\n<b>Error:</b> {str(error)[:300]}"))

def run_all_alert_checks(trade_date=None):
    if trade_date is None: trade_date=date.today()
    results={}
    for name,fn in [("gap",check_gap_alert),("vix",check_vix_alert),("selloff",check_broad_selloff),
                    ("zpi_buy",check_zpi_alerts),("cri_danger",check_cri_alerts),("fii_sell",check_fii_alert)]:
        try: results[name]=fn(trade_date)
        except Exception as e: log.warning(f"  Alert {name}: {e}"); results[name]=False
    return results


def send_morning_digest(trade_date=None):
    """
    The 8:30 AM answer to "What should I do today?" — the question the
    architecture doc opens with.

    Everything needed was already computed: the post-market run scores and
    writes predictions the previous evening, and the 07:00 pre-market job
    refreshes global markets, GIFT Nifty and news. Nothing ever delivered it, so
    the answer sat in a dashboard nobody had open before the market opened.

    Uses the most recent SCORED date, not today's calendar date — on a Monday
    the actionable scores are Friday's, and reporting "no data" because today
    has no rows yet would be wrong.
    """
    conn=get_connection()
    try:
        row=conn.execute("SELECT MAX(date) d FROM ai_scores").fetchone()
        td=row["d"] if row and row["d"] else None
        if not td:
            log.info("  Morning digest: no scores yet — nothing to send"); return False
        td=str(td)
        mh=conn.execute("SELECT mh_score,regime,portfolio_health FROM market_health "
                        "WHERE date<=? ORDER BY date DESC LIMIT 1",(td,)).fetchone()
        idx=conn.execute("SELECT nifty50_chg,india_vix,gift_nifty_chg FROM index_levels "
                         "WHERE date<=? ORDER BY date DESC,time DESC LIMIT 1",(td,)).fetchone()
        glb=conn.execute("SELECT sp500_chg,global_score FROM global_markets "
                         "WHERE date<=? ORDER BY date DESC LIMIT 1",(td,)).fetchone()
        buys=conn.execute("SELECT symbol,atip_score,zpi,cri,acs FROM ai_scores "
                          "WHERE date=? AND signal='BUY' ORDER BY atip_score DESC LIMIT 5",(td,)).fetchall()
        sells=conn.execute("SELECT symbol,atip_score,cri FROM ai_scores "
                           "WHERE date=? AND signal='SELL' ORDER BY cri DESC LIMIT 5",(td,)).fetchall()
        tod=conn.execute("""SELECT s.symbol,s.acs,p.close cmp,t.atr_14 FROM ai_scores s
                            LEFT JOIN prices_daily p ON p.symbol=s.symbol AND p.date=s.date
                            LEFT JOIN technical_indicators t ON t.symbol=s.symbol AND t.date=s.date
                            WHERE s.date=? AND s.is_tod=1 LIMIT 1""",(td,)).fetchone()
        risky=conn.execute("SELECT COUNT(*) n FROM ai_scores WHERE date=? AND cri>75",(td,)).fetchone()
    finally:
        conn.close()

    regime=(mh["regime"] if mh else None) or "—"
    mh_s=mh["mh_score"] if mh else None
    # The doc's own per-band action guidance (section 10).
    action={"STRONG_BULL":"Full deployment — max position sizing",
            "BULL":"Normal sizing — favour VPI/ZPI names",
            "NEUTRAL":"Reduced sizing — only ACS &gt; 70 setups",
            "BEAR":"Minimal exposure — defensives only",
            "HIGH_RISK":"Cash / hedges only"}.get(regime,"No regime read")

    body=[f"<b>Market Health:</b> {mh_s:.0f} ({regime})" if mh_s is not None else "<b>Market Health:</b> —",
          f"<b>Stance:</b> {action}"]
    if idx:
        bits=[]
        if idx["nifty50_chg"] is not None: bits.append(f"Nifty {idx['nifty50_chg']:+.2f}%")
        if idx["india_vix"]: bits.append(f"VIX {idx['india_vix']:.1f}")
        if idx["gift_nifty_chg"] is not None: bits.append(f"GIFT {idx['gift_nifty_chg']:+.2f}%")
        if bits: body.append("<b>Open:</b> "+" · ".join(bits))
    if glb and glb["global_score"] is not None:
        body.append(f"<b>Global:</b> score {glb['global_score']:.0f}"
                    +(f", S&amp;P {glb['sp500_chg']:+.2f}%" if glb["sp500_chg"] is not None else ""))
    if mh and mh["portfolio_health"] is not None:
        body.append(f"<b>Portfolio Health:</b> {mh['portfolio_health']:.0f}")

    if tod:
        lv=""
        if tod["cmp"] and tod["atr_14"]:
            lv=f", SL ₹{tod['cmp']-1.5*tod['atr_14']:.1f}, T1 ₹{tod['cmp']+2.0*tod['atr_14']:.1f}"
        body.append(f"\n🎯 <b>Trade of the Day:</b> {tod['symbol']} (ACS {(tod['acs'] or 0):.0f}{lv})")

    if buys:
        body.append("\n🟢 <b>BUY candidates</b>")
        for b in buys:
            body.append(f"  {b['symbol']} — ATIP {b['atip_score']:.0f}, ZPI {(b['zpi'] or 0):.0f}, "
                        f"CRI {(b['cri'] or 0):.0f}, ACS {(b['acs'] or 0):.0f}")
    else:
        body.append("\n🟢 <b>BUY candidates:</b> none clear the gates today")
    if sells:
        body.append("\n🔴 <b>Exit / avoid</b>")
        for s in sells:
            body.append(f"  {s['symbol']} — CRI {(s['cri'] or 0):.0f}, ATIP {s['atip_score']:.0f}")
    if risky and risky["n"]:
        body.append(f"\n⚠️ {risky['n']} stock(s) with CRI &gt; 75 — suppressed from Buy regardless of ATIP")

    # A brief built on week-old scores is worse than no brief — it reads as
    # today's answer. Say so at the top, not in the footnote.
    stale_n = 0
    try:
        from dashboard.server import expected_trade_date, sessions_between
        exp = expected_trade_date()
        stale_n = sessions_between(td, exp) if exp else 0
        if stale_n >= 1:
            body.insert(0, f"⚠️ <b>STALE — these are {td} scores, {stale_n} session"
                           f"{'s' if stale_n != 1 else ''} behind the last completed session "
                           f"({exp}). The pipeline has not run since. Do not trade off them.</b>\n")
    except Exception as e:
        log.debug(f"  freshness check unavailable: {e}")

    return send_telegram(fmt("☀️","Morning Brief — What should I do today?","\n".join(body),
                             f"Scores from {td}"
                             + (f" — {stale_n} session(s) STALE" if stale_n else " (current)")
                             + ". Verify against live prices before acting — model output, not advice."))

if __name__=="__main__":
    import argparse; logging.basicConfig(level=logging.INFO,format="%(asctime)s %(message)s")
    ap=argparse.ArgumentParser()
    ap.add_argument("--test",action="store_true"); ap.add_argument("--check-all",action="store_true"); ap.add_argument("--tod",action="store_true")
    args=ap.parse_args()
    if args.test: send_telegram(fmt("✅","ATIP Test","Telegram alerts working! ATIP is live."))
    elif getattr(args,"check_all",False): run_all_alert_checks()
    elif args.tod: send_tod_alert()
