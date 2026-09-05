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

if __name__=="__main__":
    import argparse; logging.basicConfig(level=logging.INFO,format="%(asctime)s %(message)s")
    ap=argparse.ArgumentParser()
    ap.add_argument("--test",action="store_true"); ap.add_argument("--check-all",action="store_true"); ap.add_argument("--tod",action="store_true")
    args=ap.parse_args()
    if args.test: send_telegram(fmt("✅","ATIP Test","Telegram alerts working! ATIP is live."))
    elif getattr(args,"check_all",False): run_all_alert_checks()
    elif args.tod: send_tod_alert()
