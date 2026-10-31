"""ATIP — Telegram Alert Engine (10 alert types)"""
import os, json, logging, requests
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from db.schema import get_connection

log = logging.getLogger(__name__)
CONFIG_PATH = Path("atip_data/config.json")
# P1-3: when an automatic morning brief may go out -- from its 08:30 slot to the 09:15 open
MORNING_BRIEF_WINDOW = (dtime(8, 30), dtime(9, 15))

def load_config():
    cfg={}
    if CONFIG_PATH.exists():
        try: cfg=json.loads(CONFIG_PATH.read_text())
        except: pass
    cfg.setdefault("telegram_token",  os.getenv("ATIP_TELEGRAM_TOKEN",""))
    cfg.setdefault("telegram_chat_id",os.getenv("ATIP_TELEGRAM_CHAT_ID",""))
    for key,name in (("telegram_token","TELEGRAM_TOKEN"),("telegram_chat_id","TELEGRAM_CHAT_ID")):   # W31: vault
        if not cfg.get(key):
            try:
                from ops.secrets import get as _sget
                cfg[key]=_sget(name,log_access=False) or ""
            except Exception: pass
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

def telegram_configured():
    cfg=load_config()
    return not (_is_placeholder(cfg.get("telegram_token")) or _is_placeholder(cfg.get("telegram_chat_id")))

def _title_of(message):
    """'ATIP — <title>' from fmt()'s first line, for the dashboard list."""
    import re
    first=re.sub(r"<[^>]+>","",message.split("\n",1)[0])
    return first.split("ATIP — ",1)[-1].strip()[:120]

def notify(message, category="alert", severity="info", key=None, parse_mode="HTML"):
    """
    Record an alert in alert_log (the dashboard's alert panel) and deliver it
    to Telegram when a bot is configured. Returns {"recorded", "sent",
    "duplicate"}.

    key: an alert with the same key already recorded today is not repeated --
    a job that stays broken is reported once a day, not every half hour.

    Before this, an alert existed only as a Telegram message; with no bot
    configured every one of them -- failures, CRI danger, the morning brief --
    went nowhere but a WARNING line in atip.log.

    Never call this while the calling thread holds an uncommitted write on
    another connection to the same database: commit first, or notify after the
    commit. The alert is written on a connection of its own, so it would wait
    out the whole busy timeout (db.schema.BUSY_TIMEOUT_MS) for a lock this
    thread cannot release until notify returns, and then be lost. Python's
    sqlite3 opens that transaction implicitly on the first INSERT / UPDATE /
    DELETE -- even one that failed -- and a connection dropped without close()
    keeps it until the garbage collector reaches it. Nothing here can see
    another connection's transaction, so the rule is the caller's to keep.
    """
    out={"recorded":False,"sent":False,"duplicate":False}
    now=datetime.now()
    conn=None
    try:
        conn=get_connection()
        if key and conn.execute("SELECT 1 FROM alert_log WHERE dedupe_key=? AND created_at>=?",
                                (key,datetime.combine(now.date(),datetime.min.time()))).fetchone():
            out["duplicate"]=True
            return out
        cur=conn.execute("INSERT INTO alert_log (created_at,category,severity,title,message,dedupe_key) "
                         "VALUES (?,?,?,?,?,?)",(now,category,severity,_title_of(message),message,key))
        conn.commit(); out["recorded"]=True; alert_id=cur.lastrowid
    except Exception as e:
        log.warning(f"  alert_log: could not record alert: {e}"); alert_id=None
    try:
        from ops.role import is_viewer
        # A VIEWER (ops/role.py) records but never delivers: the Mac sends every alert once
        sent,err=(False,"viewer: alerts are sent by the Mac") if is_viewer() else _deliver_telegram(message,parse_mode)
        out["sent"]=sent
        if conn is not None and alert_id is not None:
            conn.execute("UPDATE alert_log SET telegram_sent=?, telegram_error=? WHERE id=?",
                         (int(sent),err,alert_id)); conn.commit()
    finally:
        if conn is not None: conn.close()
    return out

def _deliver_telegram(message, parse_mode="HTML"):
    """(sent, error). The error text never carries the token: requests puts the
    full URL -- /bot<token>/sendMessage -- into its exception messages, which
    were logged as they were."""
    cfg=load_config(); token=str(cfg.get("telegram_token") or ""); chat=cfg.get("telegram_chat_id")
    if _is_placeholder(token) or _is_placeholder(chat):
        log.warning("[TELEGRAM NOT CONFIGURED — set telegram_token + telegram_chat_id in "
                    f"atip_data/config.json; shown on the dashboard instead] {_title_of(message)[:80]}")
        return False,"not configured"
    try:
        r=requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id":chat,"text":message,"parse_mode":parse_mode},timeout=10)
        r.raise_for_status(); log.info("  ✓ Telegram sent"); return True,None
    except Exception as e:
        err=str(e).replace(token,"<token>") if token else str(e)
        log.error(f"  Telegram failed: {err}"); return False,err[:300]

def send_telegram(message, parse_mode="HTML", category="alert", severity="info", key=None):
    """Record + deliver (see notify); True when Telegram delivered it."""
    return notify(message,category=category,severity=severity,key=key,parse_mode=parse_mode)["sent"]

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
    try: rows=conn.execute("SELECT symbol,zpi,vpi,acs FROM ai_scores WHERE date=? AND zpi>=75 AND `signal`='BUY' AND cri<40 ORDER BY zpi DESC LIMIT 5",(str(trade_date),)).fetchall()
    finally: conn.close()
    if not rows: return 0
    lines="".join(f"\n🎯 <b>{r['symbol']}</b>  ZPI:{r['zpi']:.0f}  VPI:{r['vpi']:.0f}" for r in rows)
    send_telegram(fmt("🎯","ZPI Buy Zone",f"<b>Buy zone candidates:</b>{lines}"))
    return len(rows)

def check_cri_alerts(trade_date=None):
    if trade_date is None: trade_date=date.today()
    conn=get_connection()
    try:
        rows=conn.execute("""SELECT p.symbol,p.pnl_pct,s.cri,s.`signal` FROM portfolio_holdings p
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
    send_telegram(fmt("❌","Pipeline Failure",f"<b>Job:</b> {job_name}\n<b>Error:</b> {_html(str(error)[:300])}"),
                  category="job",severity="error",key=f"job_failed:{job_name}")

def _html(s):
    return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def send_job_health_alerts(problems):
    """One alert per job-health problem (pipeline/health.py), once a day each."""
    sent=0
    for p in problems:
        sev="error" if p.kind in ("FAILING","MISSED") else "warning"
        r=notify(fmt("🩺",f"Job {p.kind.title()}: {p.label}",_html(p.detail)),
                 category="job_health",severity=sev,key=f"health:{p.key}")
        sent+=int(r["recorded"])
    return sent

def send_recovery_notice(out):
    """What pipeline/recover.py re-ran, and whether that cleared the problem (one alert per run)."""
    from pipeline.recover import STEP_LABEL
    fixed=set(out.get("recovered") or [])
    lines=[]
    for r in out["ran"]:
        labels=[l.split(" ",1)[1] for l in r["labels"]]
        ok=all(l in fixed for l in labels) and not str(r["result"]).startswith("FAILED")
        lines.append(f"{'✅' if ok else '⚠️'} <b>{_html(STEP_LABEL.get(r['step'],r['step']))}</b> re-run for "
                     f"{_html(', '.join(labels))}" + ("" if ok else f" — still open ({_html(r['result'])})"))
    for s in out.get("skipped") or []:
        lines.append(f"⏸ {_html(STEP_LABEL.get(s['step'],s['step']))}: {_html(s['reason'])}")
    return notify(fmt("🔁","Missed jobs re-run","\n".join(lines)),category="job_health",severity="warning",
                  key=f"recover:{datetime.now():%Y-%m-%d %H:%M}")

def run_all_alert_checks(trade_date=None):
    if trade_date is None: trade_date=date.today()
    results={}
    for name,fn in [("gap",check_gap_alert),("vix",check_vix_alert),("selloff",check_broad_selloff),
                    ("zpi_buy",check_zpi_alerts),("cri_danger",check_cri_alerts),("fii_sell",check_fii_alert)]:
        try: results[name]=fn(trade_date)
        except Exception as e: log.warning(f"  Alert {name}: {e}"); results[name]=False
    return results


def morning_brief_window(now=None):
    """(True, "") when an automatic morning brief may go out at `now`: a trading day, from the 08:30
    slot to the 09:15 open. After the open a brief reads as today's plan while the market is already
    moving -- the auto-recovery sent one at 18:49 on 2026-10-26 -- and before 08:30 the scheduled run
    is still to come. A brief asked for by hand (main.py --morning-brief) is not held to this."""
    from utils.trading_calendar import is_trading_day
    now = now or datetime.now()
    start, end = MORNING_BRIEF_WINDOW
    if not is_trading_day(now.date()):
        return False, f"{now:%d %b} is not a trading day"
    if now.time() < start:
        return False, f"the {start:%H:%M} run sends it (now {now:%H:%M})"
    if now.time() >= end:
        return False, f"the market opened at {end:%H:%M} (now {now:%H:%M}): it would read as today's plan"
    return True, ""

def _fmt_chg(v):
    return f" ({v:+.2f}%)" if v is not None else ""

def _market_lines(conn, td, now=None):
    """P1-3: crude, USD-INR, FII/DII and the news tone for the brief -- each says when it has nothing."""
    now = now or datetime.now()
    today = str(now.date())
    lines = []
    def asof(d):
        return "" if str(d)[:10] == today else f" (as of {datetime.strptime(str(d)[:10], '%Y-%m-%d'):%d %b})"
    cr = conn.execute("SELECT date,crude_brent,crude_brent_chg,crude_wti,crude_wti_chg FROM global_markets "
                      "WHERE date<=? AND (crude_brent IS NOT NULL OR crude_wti IS NOT NULL) "
                      "ORDER BY date DESC,id DESC LIMIT 1",(today,)).fetchone()
    if cr:
        bits = [f"{n} ${cr[k]:,.2f}{_fmt_chg(cr[k+'_chg'])}" for n,k in (("Brent","crude_brent"),("WTI","crude_wti"))
                if cr[k] is not None]
        lines.append("<b>Crude:</b> "+" · ".join(bits)+asof(cr["date"]))
    else:
        lines.append("<b>Crude:</b> — (no global markets fetch stored)")
    fx = conn.execute("SELECT date,usd_inr,usd_inr_chg FROM global_markets WHERE date<=? AND usd_inr IS NOT NULL "
                      "ORDER BY date DESC,id DESC LIMIT 1",(today,)).fetchone()
    lines.append(f"<b>USD-INR:</b> {fx['usd_inr']:.2f}{_fmt_chg(fx['usd_inr_chg'])}{asof(fx['date'])}" if fx
                 else "<b>USD-INR:</b> — (no global markets fetch stored)")
    fd = conn.execute("SELECT date,fii_net_cr,dii_net_cr FROM fii_dii_market WHERE date<=? AND "
                      "(fii_net_cr IS NOT NULL OR dii_net_cr IS NOT NULL) ORDER BY date DESC LIMIT 1",(td,)).fetchone()
    if fd:
        nets = " · ".join(f"{n} ₹{v:+,.0f} Cr" for n,v in (("FII",fd["fii_net_cr"]),("DII",fd["dii_net_cr"]))
                          if v is not None)
        lines.append(f"<b>FII/DII ({datetime.strptime(str(fd['date'])[:10], '%Y-%m-%d'):%d %b}):</b> {nets}"
                     + ("" if str(fd["date"])[:10] == str(td)[:10] else f" — not yet published for {td}"))
    else:
        lines.append(f"<b>FII/DII:</b> — (none stored up to {td})")
    try:
        ns = conn.execute("SELECT created_at,article_count,classifier,summary_json FROM news_summary "
                          "ORDER BY id DESC LIMIT 1").fetchone()
    except Exception:
        ns = None
    made = None
    if ns:
        try: made = datetime.fromisoformat(str(ns["created_at"])[:19])
        except ValueError: made = None
    if ns and made and now - made <= timedelta(hours=24):
        tone = (json.loads(ns["summary_json"] or "{}").get("tone") or "—").replace("_", "-")
        how = "AI" if ns["classifier"] == "claude" else "ATIP keyword rules"
        lines.append(f"<b>News tone:</b> {_html(tone)} · {ns['article_count'] or 0} headlines ({how}, {made:%d %b %H:%M})")
    else:
        lines.append("<b>News tone:</b> — (no news summary in the last 24 h)")
    try:                                         # Task 3: the Asia Sentiment Score + each market, informational
        from data.asia_markets import snapshot
        a = snapshot(conn)
        s = a.get("sentiment") or {}
        if s.get("score") is not None:
            mk = " · ".join(f"{m['name']} {m['chg_pct']:+.2f}%" for m in a["markets"] if m.get("chg_pct") is not None)
            lines.append(f"<b>Asia:</b> {s['score']:.0f} ({_html(s.get('signal') or '—')}){asof(s['date'])}"
                         + (f" · {_html(mk)}" if mk else "")
                         + (f" · Pakistan risk {_html(s['pk_risk_level'])}" if s.get("pk_risk_level") not in (None, "NONE") else ""))
        else:
            lines.append("<b>Asia:</b> — (no Asia markets run stored)")
    except Exception as e:
        log.debug(f"  Asia line: {e}")
    try:                                         # NI-04: the news impact estimate, informational
        from data.news_impact import latest as ni_latest
        ni = ni_latest(conn)
        if ni and ni.get("score") is not None:
            top = (ni.get("drivers") or [{}])[0].get("headline")
            how = "ATIP keyword rules"
            lines.append(f"<b>News impact:</b> {ni['score']:.0f} ({_html(ni['bias'])}, {_html(str(ni['strength']).lower())})"
                         f" for {_html(ni['for_session'])} · {ni['n_items']} headlines ({how})"
                         + (f" · top: {_html(top[:90])}" if top else ""))
        else:
            lines.append("<b>News impact:</b> — (no estimate stored)")
    except Exception as e:
        log.debug(f"  News impact line: {e}")
    return lines

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
                          "WHERE date=? AND `signal`='BUY' ORDER BY atip_score DESC LIMIT 5",(td,)).fetchall()
        sells=conn.execute("SELECT symbol,atip_score,cri FROM ai_scores "
                           "WHERE date=? AND `signal`='SELL' ORDER BY cri DESC LIMIT 5",(td,)).fetchall()
        tod=conn.execute("""SELECT s.symbol,s.acs,p.close cmp,t.atr_14 FROM ai_scores s
                            LEFT JOIN prices_daily p ON p.symbol=s.symbol AND p.date=s.date
                            LEFT JOIN technical_indicators t ON t.symbol=s.symbol AND t.date=s.date
                            WHERE s.date=? AND s.is_tod=1 LIMIT 1""",(td,)).fetchone()
        risky=conn.execute("SELECT COUNT(*) n FROM ai_scores WHERE date=? AND cri>75",(td,)).fetchone()
        try: market=_market_lines(conn,td)
        except Exception as e:                      # a bad row in these lines must not cost the whole brief
            log.warning(f"  Morning digest: market lines unavailable: {e}"); market=[]
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
    body.extend(market)
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

    # Whether the pipeline behind these numbers is healthy (pipeline/health.py)
    try:
        from pipeline.health import check_job_health
        c2=get_connection()
        try:
            # this brief is being sent now: it is not missing from itself
            problems=[p for p in check_job_health(c2) if p.label!="Morning brief"]
            from pipeline.recover import today as _recovered_today
            reran=_recovered_today(c2)
        finally: c2.close()
        if reran:
            body.append("\n🔁 <b>Re-run today</b>: "+_html(", ".join(
                f"{r['what']} ({'ok' if str(r['result'])=='RAN' else r['result']})" for r in reran)))
        if problems:
            body.append(f"\n🩺 <b>Pipeline problems ({len(problems)})</b>")
            body.extend(f"  {p.kind}: {_html(p.label)} — {_html(p.detail)}" for p in problems[:8])
        else:
            body.append("\n🩺 Pipeline: all monitored jobs ran")
    except Exception as e:
        log.debug(f"  job health unavailable for the brief: {e}")

    # Recorded for the dashboard even when no bot is configured: returns True
    # once the brief exists somewhere the owner can read it.
    out=notify(fmt("☀️","Morning Brief — What should I do today?","\n".join(body),
                   f"Scores from {td}"
                   + (f" — {stale_n} session(s) STALE" if stale_n else " (current)")
                   + ". Verify against live prices before acting — model output, not advice."),
               category="digest",key=f"digest:{date.today()}")
    return out["recorded"] or out["sent"]

if __name__=="__main__":
    import argparse; logging.basicConfig(level=logging.INFO,format="%(asctime)s %(message)s")
    ap=argparse.ArgumentParser()
    ap.add_argument("--test",action="store_true"); ap.add_argument("--check-all",action="store_true"); ap.add_argument("--tod",action="store_true")
    args=ap.parse_args()
    if args.test:
        # Setup check: says what is missing instead of failing quietly
        if not telegram_configured():
            print("Telegram is NOT configured. To set it up:\n"
                  "  1. In Telegram, message @BotFather, send /newbot, copy the token it gives you.\n"
                  "  2. Send any message to your new bot, then open\n"
                  "     https://api.telegram.org/bot<token>/getUpdates and copy chat.id.\n"
                  "  3. Put both in atip_data/config.json:\n"
                  '       "telegram_token": "<token>", "telegram_chat_id": "<chat id>"\n'
                  "  4. Run  python -m alerts.telegram --test  again.\n"
                  "Until then every alert is still shown on the dashboard's Alerts panel.")
        else:
            ok,err=_deliver_telegram(fmt("✅","ATIP Test","Telegram alerts working! ATIP is live."))
            print("✅ Test message delivered." if ok else f"❌ Telegram refused it: {err}")
    elif getattr(args,"check_all",False): run_all_alert_checks()
    elif args.tod: send_tod_alert()
