"""ATIP — FastAPI Dashboard Server (http://localhost:8000)"""
import json, logging, threading, time
import html as _html
from dashboard import fmt as _F
from datetime import date, datetime, timedelta
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

def json_safe(obj):
    """
    Make DB rows serialisable by JSONResponse.

    get_connection() uses detect_types=PARSE_DECLTYPES, so every DATE column
    comes back as datetime.date and every TIMESTAMP as datetime.datetime.
    json.dumps cannot encode either, so any /api/* route returning raw rows
    answered 500 — /api/scores, /api/mh and /api/news all did. The rest only
    looked healthy because they happened to be empty; they would have failed
    the moment they had rows.

    The HTML page was unaffected because build_html() stringifies everything
    and generate_state() writes its JSON with default=str, which is why the
    dashboard looked fine while the API was broken.
    """
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    return obj

def q(conn,sql,*params):
    try: rows=conn.execute(sql,params).fetchall(); return [dict(r) for r in rows]
    except: return []

def q1(conn,sql,*params):
    try: r=conn.execute(sql,params).fetchone(); return dict(r) if r else {}
    except: return {}

# The close of the session BEFORE the one on screen. Every percentage change on
# this dashboard is measured against it, because that is what a change figure
# means everywhere else in finance: today's move, not the move since some other
# session. Comparing a live price against the DISPLAYED session's own close was
# the bug — with scores a day behind it reported NIACL at -11.2%, which was the
# real 2026-09-07 to 2026-09-08 move, not today's change; with scores current it
# reported 0.0%, because it was comparing today's close against itself.
PREV_CLOSE_SQL = ("(SELECT p2.close FROM prices_daily p2 WHERE p2.symbol=%(sym)s "
                  "AND p2.date < %(date)s ORDER BY p2.date DESC LIMIT 1)")


def pct_change(last, prev):
    """Change from prev close to last, as a percentage. None when unknowable."""
    try:
        last, prev = float(last), float(prev)
    except (TypeError, ValueError):
        return None
    if prev <= 0:
        return None
    return round((last - prev) / prev * 100, 2)


def chg_span(last, prev, size=12):
    """
    CMP change rendered the way a quote screen renders it: absolute move and
    percentage, coloured, both against the previous close.
    """
    p = pct_change(last, prev)
    if p is None:
        return ""
    diff = float(last) - float(prev)
    if abs(p) < 0.005:                                   # UI fixes v2 (G1): no move is grey, never red or green
        return f'<span class="chg" style="font-size:{size}px;color:#94a3b8"> • 0.00 (0.00%)</span>'
    col = "#34d399" if p >= 0 else "#f87171"           # 4.5:1+ on the dark page; the shell maps both to its tokens
    sign = "+" if p >= 0 else ""
    arrow = "▲" if p > 0 else "▼" if p < 0 else "•"
    return (f'<span class="chg" style="font-size:{size}px;color:{col}"> {arrow} {sign}{diff:.2f} '
            f'({sign}{p:.2f}%)</span>')


def get_scores(td,limit=50):
    conn=get_connection()
    try: return q(conn,"SELECT s.*,p.close as cmp,"
                  "(SELECT p2.close FROM prices_daily p2 WHERE p2.symbol=s.symbol AND p2.date<? ORDER BY p2.date DESC LIMIT 1) AS prev_close,"
                  "t.rsi_14,t.adx_14,t.atr_pct,t.volume_ratio FROM ai_scores s "
                  "LEFT JOIN prices_daily p ON s.symbol=p.symbol AND p.date=? "
                  "LEFT JOIN technical_indicators t ON s.symbol=t.symbol AND t.date=? "
                  "WHERE s.date=? ORDER BY s.atip_score DESC LIMIT ?",
                  str(td),str(td),str(td),str(td),limit)
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


def expected_trade_date():
    """
    The trade date the system SHOULD have scores for right now.

    The convention itself is correct and unchanged: scores describe the last
    COMPLETED trading session — today once the market has closed and the
    post-market run has finished, otherwise the previous trading day.
    postmarket_target_date() already encodes exactly that and the pipeline
    targets it, so the dashboard follows the same rule instead of inventing
    its own.
    """
    try:
        from utils.trading_calendar import postmarket_target_date
        return str(postmarket_target_date())
    except Exception as e:
        log.warning(f"  expected trade date unavailable: {e}")
        return None


def sessions_between(d1, d2):
    """Trading sessions from d1 (exclusive) to d2 (inclusive); bounded so a
    wildly wrong date can't spin."""
    try:
        from utils.trading_calendar import is_trading_day
        from datetime import timedelta
        a = date.fromisoformat(str(d1)); b = date.fromisoformat(str(d2))
        if b <= a:
            return 0
        n, cur = 0, a
        while cur < b and n < 500:
            cur += timedelta(days=1)
            if is_trading_day(cur):
                n += 1
        return n
    except Exception:
        return 0


def data_freshness():
    """
    (shown_date, expected_date, stale_sessions).

    Deliberately does NOT hide or substitute anything — the newest scores
    available are still what gets shown. It reports the gap so the UI can say
    so out loud, because silently presenting week-old scores as today's is the
    failure this is meant to prevent (spec DATA-005 / DASH-007: "stale/invalid
    data cannot silently appear as current").
    """
    shown = latest_scored_date()
    expected = expected_trade_date()
    stale = sessions_between(shown, expected) if (shown and expected) else 0
    return shown, expected, stale


def get_mh(td):
    conn=get_connection()
    try: return q1(conn,"SELECT * FROM market_health WHERE date=?",str(td))
    finally: conn.close()

def get_indexes(td=None):
    """
    The most recent NSE index snapshot there is — deliberately NOT tied to the
    scored date.

    It used to match the scored date EXACTLY, so the whole index panel rendered
    blank outside market hours. That was fixed with a `date<=td` fallback, which
    fixed the blank but introduced the opposite fault: the reading is also
    CAPPED at the scored date, so whenever scoring falls behind, the panel keeps
    showing an old snapshot while fresher rows sit unused in the table.

    Seen on 2026-09-08 at 19:56: scores were a session behind at 2026-09-07, so
    the panel showed a pre-open snapshot from 08:43 with every change at +0.00%,
    while 2,743 rows from that day — including the actual close, Nifty 23,635.10
    at -0.61% — were ignored.

    These are two different things. Scores describe a session; the index panel
    describes the market right now, and the freshness banner already says how
    old the scores are. `td` is accepted and ignored, for callers that still
    pass it.
    """
    conn=get_connection()
    try: return q1(conn,"SELECT * FROM index_levels ORDER BY date DESC,time DESC LIMIT 1")
    finally: conn.close()

def get_global(td):
    # global_markets holds up to three rows per date (time = 'premarket' at
    # 01:30, 'intraday', 'overnight' at 17:30) and time is that LABEL, not a
    # clock. With no tie-break SQLite satisfied `ORDER BY date DESC` from the
    # UNIQUE(date,time) index, whose order within a date is alphabetical --
    # 'premarket' first -- so the panel always showed the OLDEST snapshot of the
    # day: on 2026-09-18 S&P +1.14% (01:30) while the 17:30 row said -0.17%,
    # the opposite sign, under a label that only showed the date. Order by when
    # the row was actually written.
    conn=get_connection()
    # Review 3 Nov 2026 (dashboard M2): not capped at the SCORED date -- until the 16:45 scores land that showed the
    # day before's markets under a current-looking label. Newest written row, the same as get_indexes.
    try: return q1(conn,"SELECT * FROM global_markets "
                        "ORDER BY date DESC, created_at DESC, id DESC LIMIT 1")
    finally: conn.close()

def get_tod(td):
    conn=get_connection()
    try:
        d=q1(conn,"SELECT s.*,p.close as cmp,"
             "(SELECT p2.close FROM prices_daily p2 WHERE p2.symbol=s.symbol AND p2.date<? ORDER BY p2.date DESC LIMIT 1) AS prev_close,"
             "t.atr_14 FROM ai_scores s LEFT JOIN prices_daily p ON s.symbol=p.symbol AND p.date=? "
             "LEFT JOIN technical_indicators t ON s.symbol=t.symbol AND t.date=? "
             "WHERE s.date=? AND s.is_tod=1 LIMIT 1",str(td),str(td),str(td),str(td))
        # One trade plan, one formula (3 Nov 2026). This used to compute its own
        # levels (stop = close - 1.5xATR, targets = close + 2x / 3.5xATR) for ANY
        # signal, while /home read the stored plan from scores/predictions.py
        # (ATR stop clamped to 2-6 %, targets +3 % / +6 %, and NO levels for
        # HOLD/WAIT). The same Trade of the Day showed a stop and targets here and
        # "—" on Today, and this page invented a plan for a stock the engine said
        # to HOLD. Both pages now use compute_trade_plan(), so a HOLD shows no plan.
        if d:
            from scores.predictions import compute_trade_plan
            plan=compute_trade_plan(d.get("signal") or "", d.get("cmp") or 0, d.get("atr_14"))
            d["sl"]=plan["stop_loss"]; d["t1"]=plan["target_1"]; d["t2"]=plan["target_2"]
        return d
    finally: conn.close()

def get_news(limit=20):
    conn=get_connection()
    try: return q(conn,"SELECT headline,source,category,sentiment,importance,news_score,ai_summary,fetched_at FROM news_articles ORDER BY fetched_at DESC LIMIT ?",limit)
    finally: conn.close()

def get_portfolio(td):
    conn=get_connection()
    try: return q(conn,"SELECT h.*,"
                  "(SELECT p2.close FROM prices_daily p2 WHERE p2.symbol=h.symbol AND p2.date<? ORDER BY p2.date DESC LIMIT 1) AS prev_close "
                  # Review 3 Nov 2026 (dashboard M3): the latest holdings snapshot at or before the scored date, not
                  # only that exact date (29 Oct had scores and no holdings row: the tab said "No portfolio data")
                  "FROM portfolio_holdings h WHERE h.date=(SELECT MAX(date) FROM portfolio_holdings WHERE date<=?) "
                  "ORDER BY h.weight_pct DESC",
                  str(td),str(td))
    finally: conn.close()

def get_fii_dii(td,days=10):
    """Recent FII/DII net flows for the dashboard panel the architecture doc
    specifies. The data was already being fetched and fed into MH/MSI; it just
    had nowhere to show."""
    conn=get_connection()
    try: return q(conn,"SELECT * FROM fii_dii_market WHERE date<=? ORDER BY date DESC LIMIT ?",str(td),days)
    finally: conn.close()

# NSE sector indexes, as (label, index_levels column) — the doc's "Sector
# Rotation" view. Ranked by the day's % change rather than listed in a fixed
# order, which is what makes it a rotation view instead of a plain list.
SECTOR_COLS=[("IT","nifty_it_chg"),("Auto","nifty_auto_chg"),("FMCG","nifty_fmcg_chg"),
             ("Metal","nifty_metal_chg"),("Realty","nifty_realty_chg"),("PSU Bank","nifty_psubank_chg"),
             ("Energy","nifty_energy_chg"),("Pharma","nifty_pharma_chg"),("Bank","banknifty_chg"),
             ("Midcap150","midcap150_chg"),("SmallCap250","smallcap250_chg"),("Nifty50","nifty50_chg")]

def _stale_days(d):
    """Calendar days between a YYYY-MM-DD string and today. None if unparseable."""
    try:
        return (date.today() - datetime.strptime(str(d)[:10], "%Y-%m-%d").date()).days
    except Exception:
        return None


def get_signal_history(limit=150):
    """Signal history + momentum success rates for the History tab."""
    try:
        from scores.signal_log import success_report, recent_signals, momentum_thresholds
        # "How often has it hit target in the last year" is a different question
        # from the all-time rate, and the more useful one — an all-time figure
        # keeps counting signals from a model version that no longer exists.
        year_ago = date.today() - timedelta(days=365)
        return {"report": success_report(), "report_1y": success_report(since=year_ago),
                "since_1y": str(year_ago),
                "recent": recent_signals(limit=limit),
                "thresholds": list(momentum_thresholds())}
    except Exception as e:
        log.warning(f"  signal history unavailable: {e}")
        return {"report": {"buckets": [], "total_signals": 0},
                "report_1y": {"buckets": [], "total_signals": 0}, "since_1y": "",
                "recent": [], "thresholds": []}


def get_order_book(limit=300):
    """
    Every order rule ATIP is tracking, in every state -- the 'where are my
    orders' view. Deliberately not limited to Dhan-visible orders: a rule
    still watching a trigger price, or one ATIP rejected or let expire, never
    reaches Dhan at all, and that absence is exactly what a user needs to see
    to understand why nothing shows up in Dhan's own order book.
    """
    try:
        from orders import rules as oe
        rule_rows = oe.list_rules()
    except Exception as e:
        log.warning(f"  order rules unavailable: {e}")
        rule_rows = []

    # Orders placed OUTSIDE the rule system -- direct/immediate orders via the
    # CLI, strategy.live.py's enter()/exits, or any future "place now" button
    # -- have no order_rules row at all. order_log is where every one of those
    # actually lands, so it is the other half of a complete book.
    already_shown = {r.get("dhan_order_id") for r in rule_rows if r.get("dhan_order_id")}
    direct_rows = []
    try:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT timestamp,symbol,transaction_type,quantity,mode,status,"
                "dhan_order_id,error,price,order_type FROM order_log "
                "WHERE status != 'DRY_RUN_OK' ORDER BY id DESC LIMIT ?", (limit,))
            for row in cur.fetchall():
                d = dict(row)
                if d.get("dhan_order_id") and d["dhan_order_id"] in already_shown:
                    continue   # already visible as its order_rules row
                direct_rows.append({
                    "id": f"log-{d.get('dhan_order_id') or d['timestamp']}",
                    "symbol": d.get("symbol"), "side": d.get("transaction_type"),
                    "created_at": d.get("timestamp"), "status": d.get("status"),
                    "dhan_order_id": d.get("dhan_order_id"),
                    "execution_error": d.get("error"),
                    "quantity_value": d.get("quantity"), "quantity_type": "SHARES",
                    "order_type": d.get("order_type"), "limit_price": d.get("price"),
                    "_direct": True,
                })
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"  direct order log unavailable: {e}")

    merged = rule_rows + direct_rows
    merged.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return merged[:limit]


def get_phs(td):
    """Portfolio Health Score — the doc's section 11. Computed on demand rather
    than read from a column so the panel is never stale relative to holdings."""
    try:
        from scores.portfolio_health import compute_phs
        return compute_phs(td) or {}
    except Exception as e:
        log.warning(f"  portfolio health unavailable: {e}")
        return {}

def get_sector_rotation(idx):
    """[(label, pct_change)] sorted strongest-first, skipping any sector with
    no reading for the day."""
    rows=[(lbl,idx.get(col)) for lbl,col in SECTOR_COLS]
    rows=[(l,v) for l,v in rows if v is not None]
    return sorted(rows,key=lambda kv:kv[1],reverse=True)

def get_top25(td):
    conn=get_connection()
    try:
        def t25(col,where=""):
            return q(conn,f"SELECT s.symbol,s.atip_score,s.{col},s.`signal`,s.cri,s.acs,s.beta_1y,p.close as cmp,"
                          f"(SELECT p2.close FROM prices_daily p2 WHERE p2.symbol=s.symbol AND p2.date<s.date ORDER BY p2.date DESC LIMIT 1) AS prev_close "
                          f"FROM ai_scores s LEFT JOIN prices_daily p ON s.symbol=p.symbol AND p.date=s.date "
                          f"WHERE s.date=? {where} ORDER BY s.{col} DESC LIMIT 25",str(td))
        return {"vpi":t25("vpi"),"rri":t25("rri"),"mri":t25("mri"),"zpi":t25("zpi","AND s.signal='BUY'"),"cri":t25("cri","AND s.cri>60")}
    finally: conn.close()

def get_alerts(hours=24, limit=12):
    """Alerts raised in the last `hours` (alert_log), newest first."""
    conn=get_connection()
    try:
        since=datetime.now()-timedelta(hours=hours)
        return q(conn,"SELECT created_at,category,severity,title,telegram_sent,telegram_error FROM alert_log "
                      "WHERE created_at>=? ORDER BY id DESC LIMIT ?",since,limit)
    except Exception:
        return []
    finally:
        conn.close()

def get_job_health():
    """Current job-health problems (pipeline/health.py) as dicts."""
    try:
        from pipeline.health import check_job_health
        conn=get_connection()
        try:
            return [{"label":p.label,"kind":p.kind,"detail":p.detail} for p in check_job_health(conn)]
        finally:
            conn.close()
    except Exception as e:
        return [{"label":"Job health check","kind":"ERROR","detail":str(e)}]

def alerts_panel_html():
    """
    The pipeline's state and the last day's alerts, at the top of the page.
    Every alert is recorded here whether or not Telegram delivered it; with no
    bot configured this is the only place most of them can be read.
    """
    import html as _h
    problems=get_job_health(); alerts=get_alerts()
    try:
        from alerts.telegram import telegram_configured
        tg=telegram_configured()
    except Exception:
        tg=False
    try:
        from pipeline.recover import today as _recovered_today
        reran=_recovered_today()
    except Exception:
        reran=[]
    rerun_html=("".join(f'<div>🔁 {_h.escape(r["what"])} re-run at {_h.escape(r["last_at"][11:16])} '
                        f'({r["attempts"]}x) for {_h.escape(r["problems"] or "")} — '
                        f'{"done" if r["result"]=="RAN" else _h.escape(str(r["result"]))}</div>' for r in reran))
    if problems:
        rows="".join(f'<div>• <b>{_h.escape(p["kind"])}</b> {_h.escape(p["label"])} — {_h.escape(p["detail"])}</div>'
                     for p in problems)
        btn=('<button style="margin-left:10px;font-size:11px" onclick="this.disabled=true;this.textContent=\'Re-running…\';'
             'afetch(\'/api/health/recover\',{method:\'POST\'}).then(r=>r.json()).then(()=>location.reload())">'
             'Re-run missed jobs now</button>')
        head=(f'<div style="background:#7f1d1d;color:#fee2e2;padding:6px 18px;font-size:12px;line-height:1.6">'
              f'<b>🩺 Pipeline problems ({len(problems)})</b>{btn}{rows}'
              f'<div style="color:#fecaca;font-size:11px">Missed / failed jobs are re-run automatically every 30 min '
              f'(up to 3 tries a day each).</div>{rerun_html}</div>')
    else:
        head=('<div style="background:#0f2e24;color:#a7f3d0;padding:4px 18px;font-size:11.5px">'
              '🩺 Pipeline: every monitored job has run' + (f'{rerun_html}' if rerun_html else '') + '</div>')
    sev_col={"error":"#f87171","warning":"#fbbf24"}
    items="".join(
        f'<div style="display:flex;gap:10px;padding:2px 0"><span style="color:#94a3b8;width:44px">{str(a["created_at"])[11:16]}</span>'
        f'<span style="color:{sev_col.get(a["severity"],"#94a3b8")};width:72px">{_h.escape(a["category"])}</span>'
        f'<span style="flex:1">{_h.escape(a["title"] or "")}</span>'
        f'<span style="color:#94a3b8">{"📨 sent" if a["telegram_sent"] else "dashboard only"}</span></div>'
        for a in alerts)
    tg_note=("" if tg else ' · <span style="color:#fbbf24">Telegram not configured — alerts appear only here '
             '(<code>python -m alerts.telegram --test</code> explains the setup)</span>')
    body=(f'<details style="background:#1e293b;padding:5px 18px;font-size:11.5px;border-bottom:1px solid #334155">'
          f'<summary style="cursor:pointer;color:#94a3b8">🔔 Alerts, last 24h: {len(alerts)}{tg_note}</summary>'
          f'<div style="margin-top:4px">{items or "<i>none</i>"}</div></details>')
    return head+body

def generate_state(td=None):
    if td is None: td=latest_scored_date()
    state={"generated_at":str(datetime.now()),"trade_date":str(td),"scores":get_scores(td,limit=2000),"mh":get_mh(td),"indexes":get_indexes(td),"global":get_global(td),"tod":get_tod(td),"news":get_news(),"portfolio":get_portfolio(td),"top25":get_top25(td),"fii_dii":get_fii_dii(td),"phs":get_phs(td),"sighist":get_signal_history(),"orders_book":get_order_book()}
    STATE_PATH.parent.mkdir(exist_ok=True); STATE_PATH.write_text(json.dumps(state,default=str))
    return state

def pill(val,inv=False):
    """A 0-100 score in its band (dashboard/score_glossary: 70+ strong, 45-69 neutral, below 45 weak; CRI the other
    way round). The band is in the tooltip and the accessible name, so colour is never the only cue."""
    if val is None: return "—"
    from dashboard.score_glossary import band
    b=band("CRI" if inv else "",val)
    return f'<span class="sp sp-{b}" title="{b}">{float(val):.0f}</span>'

def money(v):
    """₹ with Indian grouping, always 2 decimals (UX audit M4): dashboard/ux_kit.inr, as every page uses."""
    from dashboard.ux_kit import inr
    try:
        return inr(v) if v not in (None, "") and float(v) != 0 else "—"
    except (TypeError, ValueError):
        return "—"

def bval(v):
    """Render a beta figure: >1 (more volatile than Nifty) in amber, <1 in slate, missing as a dash."""
    if v is None: return "—"
    v=float(v)
    col="#f59e0b" if v>1 else "#94a3b8"
    return f'<span style="color:{col};font-weight:600">{v:.2f}</span>'

def order_btns(sym,cmp,primary="BUY",signal=None):
    """
    Buy + Sell buttons for any table row or card.

    Both sides are always rendered on every screen — `primary` only controls
    which one is visually emphasised (a stock on the CRI risk list is
    usually a Sell candidate, one in a Buy Zone a Buy candidate), because
    the action you want isn't always the one the screen is about: you may
    well want to sell something sitting in Top VPI.

    cmp is passed through as the modal's reference_price. It can legitimately
    be 0 here (no prices_daily row joined for this date) — openOrderModal()
    handles that by falling back to the live-quote cell and then forcing an
    absolute-price target, since a % move off a 0 reference is meaningless.

    BUY ONLY ON A BUY SIGNAL (3 Nov 2026). `signal` is the stock's current EOD
    signal. Buy is rendered only when it is BUY; on HOLD / WAIT / SELL / unknown
    the slot reads "No buy" with the reason in its tooltip, and the order modal
    is opened with that signal so its Buy side is locked too. Sell stays
    available everywhere (you may want to exit a holding whatever the signal).
    """
    if not sym: return "—"
    c=cmp or 0
    sig=(signal or "").upper()
    b_cls="obuy" if primary=="BUY" else "obuy ghost"
    s_cls="osell" if primary=="SELL" else "osell ghost"
    buy=(f'<button class="{b_cls}" onclick="openOrderModal(\'{sym}\',{c},\'BUY\',\'{sig}\')">Buy</button>' if sig=="BUY" else
         f'<span class="nobuy" title="Buy appears only on a BUY signal. Signal now: {sig or "none"}">No buy</span>')
    return (buy + f'<button class="{s_cls}" onclick="openOrderModal(\'{sym}\',{c},\'SELL\',\'{sig}\')">Sell</button>')

def _check_js(html):
    """
    Fail loudly if the generated <script> block has an unterminated string
    literal.

    The whole UI is one inline script, so a single broken literal is a
    SyntaxError that prevents showTab/srt/ft from ever being defined — every
    tab silently stops working while the page still renders and the server
    still returns 200. That happened: a backslash-n written in the Python
    f-string became a REAL newline in the emitted JS and split two message
    strings across lines.

    Cheap heuristic, run on every render: inside the script, ignore // comments
    and count unescaped quotes per line. Logs a warning rather than raising, so
    a false positive can never take the dashboard down.
    """
    import re as _re
    m = _re.search(r"<script>(.*?)</script>", html, _re.S)
    if not m:
        return
    bad = []
    for i, line in enumerate(m.group(1).splitlines(), 1):
        code = line.split("//", 1)[0] if "//" in line and not ("://" in line) else line
        for ch in ("'", '"'):
            n = len([1 for j, c in enumerate(code) if c == ch and (j == 0 or code[j-1] != "\\")])
            if n % 2:
                bad.append(f"line {i}: unbalanced {ch} -> {code.strip()[:70]}")
                break
    if bad:
        log.error("  ⚠ GENERATED JS LOOKS BROKEN - dashboard tabs will not work:")
        for b in bad[:6]:
            log.error(f"      {b}")


def build_html(state):
    from dashboard.stock_view import ASSETS as _stock_assets      # W26 stock panel + table filters
    from dashboard.w28_assets import ASSETS as _w28_assets         # W28 news brief, lists & scans tab
    from dashboard.w28b_assets import ASSETS as _w28b_assets       # W28b news weights, announcements
    mh=state.get("mh",{}); tod=state.get("tod",{}); idx=state.get("indexes",{}); glb=state.get("global",{})
    scores=state.get("scores",[]); news=state.get("news",[]); port=state.get("portfolio",[]); top25=state.get("top25",{})
    sig_of={r.get("symbol"):r.get("signal") for r in scores}  # holdings carry no signal of their own
    # A session whose inputs covered under half the MH weight has no score
    # (scores/engine.py MH_MIN_COVERAGE): show a dash, not a red 0.
    mh_known=mh.get("mh_score") is not None
    mh_s=mh.get("mh_score") or 0; regime=mh.get("regime") or "—"
    mh_txt=f"{mh_s:.0f}" if mh_known else "—"
    mh_col=("#059669" if mh_s>=60 else "#f59e0b" if mh_s>=40 else "#dc2626") if mh_known else "#64748b"
    gen=state.get("generated_at","")[:16]
    def chg(v, inverse=False, why="No value yet"):
        # UI fixes v2 (G1 / H9): a missing change is a grey dash with the reason, never a red 0.00%; a change of
        # zero is grey; a rise / fall carries its arrow and sign; inverse metrics (VIX) rise in amber
        from dashboard import fmt as F
        if F.num(v) is None:
            return f'<span style="color:#94a3b8" title="{_html.escape(why)}">—</span>'
        t = F.tone(v)
        c = "#94a3b8" if t == "flat" else ("#fbbf24" if t == "up" else "#34d399") if inverse else ("#34d399" if t == "up" else "#f87171")
        a = "▲ " if t == "up" else "▼ " if t == "down" else ""
        return f'<span style="color:{c};font-weight:600">{a}{F.fmt_pct(v)}</span>'
    names=load_company_names()
    def cname(sym):
        n=names.get((sym or "").upper())
        return f'<div style="font-size:9.5px;color:#94a3b8;font-weight:400;white-space:normal">{n}</div>' if n else ""
    import html as _html
    from dashboard import score_glossary as G
    score_legend=G.legend_html("scoreLegend")
    def _srch(sym):
        return _html.escape(f"{sym} {names.get((sym or '').upper(), '')}".lower(), quote=True)
    score_rows="".join(f"""<tr data-sym="{r.get('symbol')}" data-signal="{r.get('signal') or ''}" data-search="{_srch(r.get('symbol'))}"><td>{r.get('atip_rank') if r.get('atip_rank') is not None else '—'}</td><td><b>{r.get('symbol')}</b>{'⭐' if r.get('is_tod') and r.get('signal')=='BUY' else ''}{cname(r.get('symbol'))}</td><td>{pill(r.get('atip_score'))}</td><td>{pill(r.get('vpi'))}</td><td>{pill(r.get('mri'))}</td><td>{pill(r.get('rri'))}</td><td>{pill(r.get('zpi'))}</td><td>{pill(r.get('cri'),inv=True)}</td><td>{pill(r.get('acs'))}</td><td class="cmpcell" data-prev="{r.get('prev_close') or ''}" data-cmp="{r.get('cmp') or ''}">{money(r.get('cmp'))}{chg_span(r.get('cmp'), r.get('prev_close'))}</td><td data-v="{r.get('beta_1y') if r.get('beta_1y') is not None else ''}">{bval(r.get('beta_1y'))}</td><td style="color:{'#059669' if r.get('signal')=='BUY' else '#dc2626' if r.get('signal')=='SELL' else '#2563eb'};font-weight:600">{r.get('signal','—')}</td><td style="font-size:10px;color:#64748b">{r.get('top_factor_1') or '—'}</td><td class="acts">{order_btns(r.get('symbol'),r.get('cmp'),primary=('SELL' if r.get('signal')=='SELL' else 'BUY'),signal=r.get('signal'))}</td></tr>""" for r in scores)
    # Review 3 Nov 2026 (dashboard M1): headlines are outside text on a page that carries the order token -- escaped
    # (after unescaping the feed's own &amp;, so it still reads right)
    _nx=lambda x: _html.escape(_html.unescape(str(x or '')), quote=True)
    news_rows="".join(f"""<tr><td style="font-size:12px;max-width:300px">{_nx(n.get('headline'))}</td><td style="font-size:11px">{_nx(n.get('source'))}</td><td style="color:{'#dc2626' if n.get('importance')=='HIGH' else '#f59e0b'};font-size:11px;font-weight:600">{n.get('importance','')}</td><td style="color:{'#059669' if (n.get('sentiment') or 0)>0.1 else '#dc2626' if (n.get('sentiment') or 0)<-0.1 else '#64748b'};font-weight:600">{(n.get('sentiment') or 0):+.2f}</td></tr>""" for n in news[:15])
    port_rows="".join(f"""<tr data-sym="{p.get('symbol')}"><td><b>{p.get('symbol')}</b>{cname(p.get('symbol'))}</td><td>{p.get('qty')}</td><td>{money(p.get('avg_price'))}</td><td class="cmpcell" data-prev="{p.get('prev_close') or ''}" data-cmp="{p.get('cmp') or ''}">{money(p.get('cmp'))}{chg_span(p.get('cmp'), p.get('prev_close'))}</td><td style="color:{'#059669' if (p.get('pnl_pct') or 0)>=0 else '#dc2626'};font-weight:600">{(p.get('pnl_pct') or 0):+.1f}%</td><td>{pill(p.get('atip_score'))}</td><td>{pill(p.get('cri'),inv=True)}</td><td style="font-size:11px">{p.get('signal','—')}</td><td class="acts">{order_btns(p.get('symbol'),p.get('cmp'),primary='SELL',signal=sig_of.get(p.get('symbol')))}</td></tr>""" for p in port)
    # Show the LEVEL beside the change. A column of "+0.00%" tells you nothing
    # about where the market closed, which is the first thing you look for
    # outside market hours -- and pre-open every change legitimately reads 0.00%
    # because Dhan's prev_close equals the last traded price until the bell.
    def _lvl(v):
        x = idx.get(v)
        return (f'<span style="color:#94a3b8;font-size:11px;margin-right:6px">{x:,.0f}</span>'
                if isinstance(x, (int, float)) else '')
    idx_rows="".join(f'<div style="display:flex;justify-content:space-between;margin:4px 0;font-size:12px"><span style="color:#94a3b8">{k}</span><span>{_lvl(v[:-4])}{chg(idx.get(v))}</span></div>' for k,v in [("Nifty50","nifty50_chg"),("BankNifty","banknifty_chg"),("Midcap150","midcap150_chg"),("SmallCap250","smallcap250_chg"),("IT","nifty_it_chg"),("Auto","nifty_auto_chg"),("FMCG","nifty_fmcg_chg"),("Metal","nifty_metal_chg"),("Realty","nifty_realty_chg"),("PSUBank","nifty_psubank_chg"),("Energy","nifty_energy_chg"),("Pharma","nifty_pharma_chg")])
    glb_rows="".join(f'<div style="display:flex;justify-content:space-between;margin:4px 0;font-size:12px"><span style="color:#94a3b8">{k}</span>{chg(glb.get(v))}</div>' for k,v in [("S&P500","sp500_chg"),("Dow","dow_chg"),("Nasdaq","nasdaq_chg"),("Nikkei","nikkei_chg"),("Crude","crude_wti_chg"),("Gold","gold_chg"),("USD/INR","usd_inr_chg")])
    # One row shape for all five Top-25 lists. Previously the three that had a
    # tab were three near-identical 300-char f-strings; adding RRI and MRI as a
    # fourth and fifth copy wasn't worth it.
    def t25_row(r,mid,primary="BUY",sig_style=""):
        return (f'<tr data-sym="{r.get("symbol")}">'
                f'<td><b>{r.get("symbol")}</b>{cname(r.get("symbol"))}</td>{mid}'
                f'<td class="cmpcell" data-prev="{r.get("prev_close") or ""}" data-cmp="{r.get("cmp") or ""}">{money(r.get("cmp"))}{chg_span(r.get("cmp"), r.get("prev_close"))}</td>'
                f'<td data-v="{r.get("beta_1y") if r.get("beta_1y") is not None else ""}">{bval(r.get("beta_1y"))}</td>'
                f'<td{sig_style}>{r.get("signal") or "—"}</td>'
                f'<td class="acts">{order_btns(r.get("symbol"),r.get("cmp"),primary=primary,signal=r.get("signal"))}</td></tr>')

    def by_signal(r):
        return "SELL" if r.get("signal")=="SELL" else "BUY"

    top25_rows={
      "vpi":"".join(t25_row(r,f'<td>{pill(r.get("atip_score"))}</td><td>{pill(r.get("vpi"))}</td>',
                            by_signal(r)) for r in top25.get("vpi",[])),
      "zpi":"".join(t25_row(r,f'<td>{pill(r.get("zpi"))}</td><td>{pill(r.get("atip_score"))}</td>',
                            "BUY"," style='color:#059669;font-weight:600'") for r in top25.get("zpi",[])),
      "rri":"".join(t25_row(r,f'<td>{pill(r.get("rri"))}</td><td>{pill(r.get("atip_score"))}</td>',
                            by_signal(r)) for r in top25.get("rri",[])),
      "mri":"".join(t25_row(r,f'<td>{pill(r.get("mri"))}</td><td>{pill(r.get("atip_score"))}</td>',
                            by_signal(r)) for r in top25.get("mri",[])),
      "cri":"".join(t25_row(r,f'<td style="color:#dc2626;font-weight:700">{(r.get("cri") or 0):.0f}</td>'
                              f'<td>{pill(r.get("atip_score"))}</td>',
                            "SELL"," style='color:#dc2626'") for r in top25.get("cri",[]))}

    # ── FII/DII panel + Sector Rotation heatmap ──────────────────────────────
    fii_dii=state.get("fii_dii",[]) or []
    def cr(v):
        """₹Cr flow, green for net buying, red for net selling."""
        if v is None: return '<span style="color:#94a3b8">—</span>'
        c="#059669" if v>0 else "#dc2626" if v<0 else "#94a3b8"
        return f'<span style="color:{c};font-weight:600">{v:+,.0f}</span>'
    fii_rows="".join(
        f'<tr><td>{r.get("date")}</td><td>{cr(r.get("fii_net_cr"))}</td><td>{cr(r.get("dii_net_cr"))}</td>'
        f'<td>{cr(r.get("fii_5d_avg"))}</td><td>{cr(r.get("dii_5d_avg"))}</td></tr>' for r in fii_dii)
    # ── Portfolio Health panel ───────────────────────────────────────────────
    phs=state.get("phs") or {}
    phs_score=phs.get("phs")
    phs_col=("#059669" if (phs_score or 0)>=60 else "#f59e0b" if (phs_score or 0)>=40 else "#dc2626")
    phs_comp="".join(
        f'<div style="display:flex;justify-content:space-between;padding:4px 0;'
        f'border-bottom:1px solid #33415533;font-size:12px">'
        f'<span style="color:#94a3b8">{k}</span>{pill(v)}</div>'
        for k,v in (phs.get("components") or {}).items())
    # Index readings can legitimately predate the scored date (the feed only
    # runs during market hours), so label them rather than let an old close
    # read as today's.
    idx_date=str(idx.get("date") or "")
    idx_time=str(idx.get("time") or "")
    idx_note=(f'<span style="color:#f59e0b">as of {idx_date} {idx_time}</span>'
              if idx_date and idx_date != str(state.get("trade_date") or "")
              else f'<span style="color:#94a3b8">{idx_time}</span>')
    # The Global panel carried no date at all, so a stale reading was
    # indistinguishable from a live one. global_markets had not been written
    # since 2026-08-03 — over a month — and the sidebar showed those figures
    # as though they were today's, because get_global() falls back to the most
    # recent row on or before the view date. The NSE panel beside it has said
    # "as of ..." all along; this makes the two consistent.
    glb_date=str(glb.get("date") or "")
    glb_stale=_stale_days(glb_date)
    glb_note=(f'<span style="color:{"#dc2626" if glb_stale and glb_stale>3 else "#f59e0b"}">'
              f'as of {glb_date}</span>'
              if glb_date and glb_date != str(state.get("trade_date") or "")
              else f'<span style="color:#94a3b8">{glb_date}</span>')
    sectors=get_sector_rotation(idx)
    def heat(v):
        """Background intensity scaled to ±2%, which covers a normal NSE day."""
        cap=min(abs(v)/2.0,1.0)
        base="5,150,105" if v>=0 else "220,38,38"
        return f"rgba({base},{0.15+0.55*cap:.2f})"
    sector_line=(f"strongest {sectors[0][0]} {sectors[0][1]:+.2f}% · weakest {sectors[-1][0]} {sectors[-1][1]:+.2f}%"
                 if sectors else "no sector data for this date")
    sector_tiles="".join(
        f'<div style="background:{heat(v)};border:1px solid #33415577;border-radius:6px;'
        f'padding:7px 9px;min-width:96px;text-align:center">'
        f'<div class="sec-l">{lbl}</div>'
        f'<div class="sec-v">{"▲" if v > 0 else "▼" if v < 0 else "•"} {v:+.2f}%</div></div>'
        for lbl,v in sectors) or '<div style="color:#64748b;font-size:12px">No sector data for this date.</div>'
    # ── Signal history + momentum success rates ──────────────────────────────
    ob = state.get("orders_book") or []

    def _order_row(r):
        status = r.get("status") or ""
        is_direct = bool(r.get("_direct"))
        scol = {"ACTIVE": "#2563eb", "PENDING_CONFIRMATION": "#f59e0b",
                "EXECUTED": "#059669", "PLACED": "#059669", "FAILED": "#dc2626",
                "BLOCKED_INSUFFICIENT_FUNDS": "#dc2626",
                "CANCELLED": "#64748b", "EXPIRED": "#64748b"}.get(status, "#94a3b8")
        oid = r.get("dhan_order_id") or ""
        # The order id prefix IS the ground truth for "was this ever real":
        # execute_rule() and orders.broker both store whatever
        # orders.broker.place_*_order() returned, and the paper broker's ids
        # are always "PAPERxxxxxxxx". A rule with no id yet has simply never
        # fired -- it is still watching its trigger.
        if oid.startswith("PAPER"):
            mode = ('<span style="color:#94a3b8;font-size:10px;border:1px solid #94a3b8;'
                    'border-radius:3px;padding:0 4px" title="Filled by the paper broker '
                    'at a live price -- never reached Dhan">PAPER</span>')
        elif oid:
            mode = ('<span style="color:#dc2626;font-size:10px;font-weight:700;border:1px '
                    'solid #dc2626;border-radius:3px;padding:0 4px" title="Placed with '
                    'Dhan for real">LIVE</span>')
        else:
            mode = ""
        if is_direct:
            # Placed immediately -- CLI, strategy.live.py, or a future "place
            # now" UI -- not a conditional watch, so there is no trigger price
            # and no ENTRY/TARGET/STOP role; show what kind of order it was.
            role_badge = (' <b style="color:#94a3b8">[DIRECT]</b>')
            ot = r.get("order_type") or "MARKET"
            lp = r.get("limit_price")
            trig_s = f'LIMIT @ {money(lp)}' if (ot == "LIMIT" and lp) else ot
            trig = None
        else:
            role = r.get("role") or "ENTRY"
            role_badge = (f' <b style="color:{"#dc2626" if role=="STOP" else "#059669" if role=="TARGET" else "#38bdf8"}">'
                         f'[{role}]</b>') if role != "ENTRY" else ""
            dirsym = "≤" if r.get("trigger_direction") == "BELOW" else "≥"
            trig = r.get("resolved_trigger_price")
            trig_s = f'{dirsym} {money(trig)}' if trig is not None else "—"
        qty = r.get("quantity_value")
        qt_ = r.get("quantity_type")
        qty_s = (f'{qty:g} sh' if qt_ == "SHARES" else f'{qty:g}% risk' if qt_ == "RISK"
                 else money(qty)) if qty is not None else "—"
        exec_price = r.get("execution_price")
        # order_log only records the ESTIMATED price at order time, not the
        # actual average fill -- that lives in paper_order for PAPER mode, and
        # would need a Dhan order-status lookup for LIVE (not built here).
        # Left as "placed" rather than a fabricated fill price.
        exec_s = (f'{money(exec_price)} × {r.get("execution_quantity") or "?"}'
                  if exec_price else ("placed" if (is_direct and status == "PLACED") else "—"))
        err = r.get("execution_error")
        err_html = (f'<div style="color:#dc2626;font-size:10px;max-width:180px;'
                    f'white-space:normal;margin-top:2px">{_html.escape(str(err))}</div>') if err else ""
        actions = ""
        if status == "PENDING_CONFIRMATION":
            actions = (f'<button onclick="confirmRule(\'{r["id"]}\')" '
                      f'style="background:#059669;color:#fff;border:none;border-radius:4px;'
                      f'padding:3px 10px;font-size:11px;cursor:pointer;margin-right:4px">Confirm</button>'
                      f'<button onclick="rejectRule(\'{r["id"]}\')" '
                      f'style="background:#dc2626;color:#fff;border:none;border-radius:4px;'
                      f'padding:3px 10px;font-size:11px;cursor:pointer">Reject</button>')
        elif status == "ACTIVE":
            actions = (f'<button onclick="deleteRule(\'{r["id"]}\')" '
                      f'style="background:none;border:1px solid #dc2626;color:#dc2626;'
                      f'border-radius:4px;cursor:pointer;padding:2px 8px;font-size:11px">Cancel</button>')
        created = str(r.get("created_at") or "")[:19].replace("T", " ")
        return (f'<tr data-sym="{r.get("symbol")}">'
                f'<td style="font-size:11px;color:#94a3b8">{created}</td>'
                f'<td><b>{r.get("symbol")}</b>{cname(r.get("symbol"))}</td>'
                f'<td style="color:{"#059669" if r.get("side")=="BUY" else "#dc2626"};'
                f'font-weight:600">{r.get("side")}{role_badge}</td>'
                f'<td data-v="{trig if trig is not None else ""}">{trig_s}</td>'
                f'<td data-v="{qty if qty is not None else ""}">{qty_s}</td>'
                f'<td><span style="background:{scol}20;color:{scol};padding:2px 7px;'
                f'border-radius:4px;font-weight:600;font-size:11px">{status}</span></td>'
                f'<td>{mode}</td>'
                f'<td>{exec_s}{err_html}</td>'
                f'<td style="font-size:10px;color:#94a3b8">{oid or "—"}</td>'
                f'<td class="acts">{actions}</td></tr>')

    order_rows = "".join(_order_row(r) for r in ob)
    order_rows_or_empty = order_rows or (
        '<tr><td colspan="10" style="text-align:center;color:#94a3b8;padding:20px">'
        'No orders yet. Use the Buy/Sell buttons on any stock to create one.</td></tr>')

    sh = state.get("sighist") or {}
    sh_rep = sh.get("report") or {}
    ths = sh.get("thresholds") or []

    def _pct(v, good=55.0):
        if v is None:
            return '<span style="color:#94a3b8">-</span>'
        c = "#059669" if v >= good else "#f59e0b" if v >= 35 else "#dc2626"
        return f'<span style="color:{c};font-weight:700">{v:.0f}%</span>'

    def _num(v, suffix="d"):
        return f"{v}{suffix}" if v else '<span style="color:#94a3b8">n/a</span>'

    def _signed(v):
        if v is None:
            return "-"
        c = "#059669" if v >= 0 else "#dc2626"
        return f'<span style="color:{c}">{v:+.1f}%</span>'

    # "How often in the last year" asked separately from the all-time rate.
    # An all-time figure keeps counting signals produced by a model version that
    # no longer exists, so the two can disagree — and when they do, the recent
    # one is the one that describes the system you are running now.
    sh_1y = (sh.get("report_1y") or {})
    by_1y = {(b["signal"], b["threshold_pct"]): b for b in sh_1y.get("buckets", [])}

    def _cell_1y(sig, th):
        b = by_1y.get((sig, th))
        if not b or not b.get("watched"):
            return ('<span style="color:#94a3b8" title="no signal from the last '
                    '12 months has been watched for a session yet">-</span>')
        return (f'{_pct(b["hit_rate"])}'
                f'<span style="color:#94a3b8;font-size:10px"> ({b["hits"]}/{b["watched"]})</span>')

    succ_rows = ""
    for b in sh_rep.get("buckets", []):
        sig_c = "#059669" if b["signal"] == "BUY" else "#dc2626"
        succ_rows += (
            f'<tr><td style="font-weight:600;color:{sig_c}">{b["signal"]}</td>'
            f'<td><b>{b["threshold_pct"]:g}%</b></td>'
            f'<td>{b.get("watched", 0)}</td><td>{b["hits"]}</td>'
            f'<td>{_pct(b["hit_rate"])}</td>'
            f'<td>{_num(b["median_sessions"])}</td>'
            f'<td>{_num(b["fastest_sessions"])}</td>'
            f'<td>{_num(b["slowest_sessions"])}</td>'
            f'<td>{_signed(b.get("avg_mfe"))}</td>'
            f'<td>{_signed(b.get("avg_mae"))}</td>'
            f'<td style="color:#94a3b8">{b.get("not_yet_watched", 0)}</td>'
            f'<td>{_cell_1y(b["signal"], b["threshold_pct"])}</td></tr>')

    def _oc(o):
        """One threshold cell: hit / miss / still open, with sessions taken."""
        if not o:
            return '<span style="color:#94a3b8">-</span>'
        if o.get("still_open"):
            return '<span style="color:#94a3b8">open</span>'
        if o.get("hit"):
            d = o.get("sessions_to_hit")
            gap = o.get("data_gap_sessions") or 0
            ok = '<span style="color:#059669;font-weight:700">OK</span>'
            if d and not gap:
                return ok + f'<span style="color:#94a3b8;font-size:10px"> {d}d</span>'
            # Hit across a hole in the price history. It DID reach the target, but
            # the elapsed time is only an upper bound - "27d" here would claim a
            # precision the data cannot support, so show it as "<=27d".
            if d:
                return ok + (f'<span style="color:#f59e0b;font-size:10px" '
                             f'title="{gap} session(s) missing from price history - '
                             f'target was reached somewhere inside that window"> &le;{d}d</span>')
            return ok + ('<span style="color:#f59e0b;font-size:10px" '
                         'title="price history gap - timing unknown"> ?</span>')
        return '<span style="color:#dc2626">X</span>'

    def _first_hit(outs):
        """
        Earliest date any target was reached, and which one.

        The table showed how MANY sessions a hit took but never the date it
        happened on, so a hit could not be lined up against anything else -- a
        news event, a results day, or your own order log.
        """
        hits = [o for o in outs if o.get("hit") and o.get("hit_date")]
        if not hits:
            return '<span style="color:#94a3b8">-</span>'
        first = min(hits, key=lambda o: str(o["hit_date"]))
        return (f'<span style="color:#059669">{str(first["hit_date"])[:10]}</span>'
                f'<span style="color:#94a3b8;font-size:10px"> @{first["threshold_pct"]:g}%</span>')

    hist_rows = ""
    for r in sh.get("recent", []):
        outs = r.get("outcomes", [])
        omap = {o["threshold_pct"]: o for o in outs}
        cells = "".join(f'<td style="text-align:center">{_oc(omap.get(t))}</td>' for t in ths)
        mfe = next((o.get("max_favourable_pct") for o in outs if o.get("max_favourable_pct") is not None), None)
        mae = next((o.get("max_adverse_pct") for o in outs if o.get("max_adverse_pct") is not None), None)
        sig_c = "#059669" if r.get("signal") == "BUY" else "#dc2626"
        hist_rows += (
            f'<tr data-sym="{r.get("symbol")}" data-signal="{r.get("signal") or ""}"><td>{r.get("signal_date")}</td>'
            f'<td><b>{r.get("symbol")}</b>{"*" if r.get("is_tod") else ""}</td>'
            f'<td style="color:{sig_c};font-weight:600">{r.get("signal")}</td>'
            f'<td>{money(r.get("entry_price"))}</td>'
            f'<td>{pill(r.get("atip_score"))}</td><td>{pill(r.get("zpi"))}</td>'
            f'<td>{pill(r.get("cri"),inv=True)}</td>{cells}'
            f'<td>{_first_hit(outs)}</td>'
            f'<td>{_signed(mfe)}</td><td>{_signed(mae)}</td>'
            f'<td style="font-size:10px;color:#94a3b8">{r.get("model_version") or "-"}</td></tr>')

    th_heads = "".join(f'<th style="text-align:center">{t:g}%</th>' for t in ths)
    hist_rows_or_empty = hist_rows or (
        '<tr><td colspan="15" style="text-align:center;color:#94a3b8;padding:20px">'
        'No signals logged yet. The post-market run appends them; backfill past '
        'dates with: python -m scores.signal_log --backfill</td></tr>')
    sh_total = sh_rep.get("total_signals", 0)
    sh_span = (f'{sh_rep.get("first","")} to {sh_rep.get("last","")}'
               if sh_rep.get("first") else "no signals yet")
    sh_since_1y = sh.get("since_1y") or "-"
    sh_thin = any(b["resolved"] and b["resolved"] < 20 for b in sh_rep.get("buckets", []))
    _gh = sum(b.get("hits_gapped") or 0 for b in sh_rep.get("buckets", []))
    _ht = sum(b.get("hits") or 0 for b in sh_rep.get("buckets", []))
    _msgs = []
    if sh_thin:
        _msgs.append("Small sample - treat these percentages as indicative, not a "
                     "track record.")
    if _gh:
        # The single most misleading thing this table can do is show a high hit
        # rate built out of gaps, so say it in the open rather than in a tooltip.
        _msgs.append(f"<b>{_gh} of {_ht} hits were established across a gap in price "
                     f"history</b> - price was already past the target when data "
                     f"resumed, so those say nothing about how fast, or whether the "
                     f"move would have been catchable. Marked &le; in the table. "
                     f"Fix the daily price feed before reading these rates as "
                     f"performance.")
    sh_note = ('<div style="color:#f59e0b;font-size:11.5px;margin-top:6px;line-height:1.5">'
               + " ".join(_msgs) + '</div>') if _msgs else ''

    # ── Data freshness banner ────────────────────────────────────────────────
    shown_d, expected_d, stale_n = data_freshness()
    if stale_n >= 1:
        _sev = "#dc2626" if stale_n >= 3 else "#f59e0b"
        stale_banner = (
            f'<div style="background:{_sev};color:#fff;padding:7px 18px;font-size:12.5px;font-weight:600">'
            f'⚠ STALE DATA — showing scores for {shown_d}, but the last completed trading session is '
            f'{expected_d} ({stale_n} session{"s" if stale_n != 1 else ""} behind). '
            f'Run <code style="background:#00000030;padding:1px 5px;border-radius:3px">python main.py '
            f'--run postmarket</code> to refresh. Do not trade off these numbers.</div>')
    else:
        stale_banner = (f'<div style="background:#065f46;color:#d1fae5;padding:5px 18px;font-size:11.5px">'
                        f'✓ Scores current for the last completed session ({shown_d})</div>')
    health_panel = alerts_panel_html()
    health_bad = "Pipeline problems" in health_panel
    tod_sym=tod.get('symbol') or '—'; tod_sig=tod.get('signal') or '—'; tod_cmp=tod.get('cmp') or '—'
    # Levels only when the engine recommended a trade (BUY/SELL); a HOLD/WAIT
    # Trade of the Day says so instead of printing "₹None" or invented levels.
    if tod.get('sl') is not None:
        tod_plan=(f'<div><div class="tod-l">Stop Loss</div><div class="tod-v" style="color:#dc2626">{money(tod["sl"])}</div></div>'
                  f'<div><div class="tod-l">Target 1</div><div class="tod-v" style="color:#059669">{money(tod["t1"])}</div></div>'
                  f'<div><div class="tod-l">Target 2</div><div class="tod-v" style="color:#059669">{money(tod["t2"])}</div></div>')
    else:
        tod_plan=(f'<div style="grid-column:span 3"><div class="tod-l">Trade plan</div>'
                  f'<div class="tod-v" style="color:#94a3b8;font-size:14px">No trade plan — signal is {tod_sig}. '
                  f'Stop and targets appear only on a BUY or SELL signal.</div></div>')
    # Served with the page so the dashboard keeps working; see dashboard/security.py.
    from dashboard.security import page_token as _dash_token
    token_js = json.dumps(_dash_token())
    tod_cname=names.get((tod.get('symbol') or "").upper(),"")
    # Owner decision 3 Nov 2026: a Trade of the Day only on a BUY signal. On a day with no BUY pick the card says so
    # (and how the day's scores split) instead of showing a HOLD / WAIT stock.
    tod_buy=(tod_sig=='BUY')
    _n={k:sum(1 for r in scores if (r.get('signal') or '')==k) for k in ('BUY','HOLD','WAIT','SELL')}
    tod_none=('<div class="tod-card tod-none" role="status"><div class="tod-sym">No BUY signal for today</div>'
              '<div style="grid-column:1/-1" class="tod-v">ATIP names a Trade of the Day only from a BUY signal. '
              f'Scores of {state.get("trade_date") or "—"}: {_n["BUY"]} BUY · {_n["HOLD"]} HOLD · {_n["WAIT"]} WAIT · {_n["SELL"]} SELL. '
              + (f'{_n["BUY"]} stock{"s have" if _n["BUY"]!=1 else " has"} a BUY signal: pick BUY in the signal filter below.'
                 if _n["BUY"] else 'New signals come with the post-market scores at 16:45 IST.') + '</div></div>')
    # Task 2/3: the operating-status banner and the Global & Asia tab (dashboard/global_asia.py)
    from dashboard.global_asia import status_banner_html as _op_banner, asia_tab_html as _asia_tab
    op_banner=_op_banner(); asia_tab=_asia_tab()
    from dashboard.market_page import live_tab_html as _live_tab                       # 2026-10-31 (DP-01, MON-07)
    live_tab=_live_tab()
    from dashboard.news_impact_view import panel_html as _ni_panel                     # NI-05
    ni_news_panel=_ni_panel("niNewsBox")
    # Where an order from the modal would go (3 Nov 2026): the modal used to say
    # nothing about PAPER vs LIVE. Unreadable config resolves to PAPER, matching
    # orders.environment's own rule, and the label says so.
    try:
        from orders.environment import broker_env as _benv, LIVE as _LIVE, describe as _bdesc
        env_now=_benv(); env_live=(env_now==_LIVE); env_line=_bdesc()
    except Exception:
        env_now, env_live = "PAPER", False
        env_line="PAPER — simulated fills at live prices, no order reaches Dhan"
    env_cls="live" if env_live else "paper"
    # The order ticket's charges estimate (UX audit M5): backtest/costs.py's delivery and intraday models
    try:
        from backtest.costs import cost_model as _cm
        costs_attr=_html.escape(json.dumps({"CNC":_cm("nse_delivery").as_dict(),"INTRADAY":_cm("nse_intraday").as_dict()}),quote=True)
    except Exception:
        costs_attr="{}"
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ATIP Scores · ATIP</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}body{{font-family:system-ui,sans-serif;background:var(--ax-bg,#0f172a);color:var(--ax-text,#e2e8f0);font-size:13.5px}}.topbar{{background:var(--ax-surface,#1e293b);padding:10px 18px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--ax-border,#334155)}}.logo{{font-size:17px;font-weight:700;color:var(--ax-brand,#38bdf8)}}.kpi-row{{display:flex;gap:8px;padding:10px 18px;flex-wrap:wrap;background:var(--ax-surface,#1e293b);border-bottom:1px solid var(--ax-border,#334155)}}.kpi{{background:var(--ax-surface-2,#0f172a);border:1px solid var(--ax-border,#334155);border-radius:8px;padding:8px 14px;min-width:100px}}.kpi-l{{font-size:12px;color:var(--ax-muted,#64748b);text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px}}.kpi-v{{font-size:20px;font-weight:700}}.body{{display:flex}}.sidebar{{width:200px;background:var(--ax-surface,#1e293b);border-right:1px solid var(--ax-border,#334155);padding:12px;overflow-y:auto;min-height:100vh}}.sidebar h3{{font-size:12px;color:var(--ax-muted,#64748b);text-transform:uppercase;margin-bottom:6px;margin-top:14px}}.sidebar h3:first-child{{margin-top:0}}.main{{flex:1;padding:14px;overflow-x:auto}}.section{{margin-bottom:20px}}.st{{font-size:13px;font-weight:600;color:var(--ax-brand,#38bdf8);margin-bottom:8px;padding-bottom:5px;border-bottom:1px solid var(--ax-border,#334155)}}.tod-card[hidden]{{display:none}}.tod-card{{background:var(--ax-surface,#1e293b);border:1px solid var(--ax-border,#334155);border-radius:10px;padding:14px;display:grid;grid-template-columns:1fr 1fr;gap:10px}}.tod-sym{{font-size:24px;font-weight:700;grid-column:1/-1}}.tod-l{{font-size:12px;color:var(--ax-muted,#94a3b8)}}.tod-v{{font-size:13px;font-weight:600}}.tabs{{display:flex;gap:4px;margin-bottom:10px}}.tab{{padding:5px 12px;border-radius:6px;font-size:12px;cursor:pointer;border:1px solid var(--ax-border,#334155);background:var(--ax-surface,#1e293b);color:var(--ax-muted,#94a3b8)}}.tab.active{{background:var(--ax-brand,#2563eb);color:var(--ax-brand-ink,#fff);border-color:var(--ax-brand,#2563eb)}}.tc{{display:none}}.tc.active{{display:block}}table{{width:100%;border-collapse:collapse;background:var(--ax-surface,#1e293b);border-radius:8px;overflow:hidden;font-size:12.5px}}th{{background:var(--ax-surface-2,#0f172a);color:var(--ax-muted,#94a3b8);padding:7px 7px;text-align:left;border-bottom:1px solid var(--ax-border,#334155);font-size:12px;cursor:pointer;white-space:nowrap}}th:hover{{color:var(--ax-text,#e2e8f0)}}td{{padding:6px 7px;border-bottom:1px solid var(--ax-border-soft,#1e293b22);white-space:nowrap}}tr:hover td{{background:var(--ax-surface-2,#0f172a)}}.disc{{font-size:12px;color:var(--ax-muted,#475569);margin-top:16px;padding-top:10px;border-top:1px solid var(--ax-border,#334155);line-height:1.6}}input,select{{padding:5px 10px;border-radius:6px;border:1px solid var(--ax-border,#334155);background:var(--ax-surface-2,#0f172a);color:var(--ax-text,#e2e8f0);font-size:12px}}.rf{{background:var(--ax-brand,#2563eb);color:var(--ax-brand-ink,#fff);border:none;padding:5px 12px;border-radius:6px;cursor:pointer;font-size:12px}}
.ob{{background:var(--ax-surface,#1e293b);border:1px solid var(--ax-brand,#38bdf8);color:var(--ax-brand,#38bdf8);padding:3px 10px;border-radius:5px;font-size:12px;cursor:pointer}}.ob:hover{{background:var(--ax-brand,#38bdf8);color:var(--ax-brand-ink,#0f172a)}}
.acts{{white-space:nowrap}}.acts button{{padding:3px 11px;border-radius:5px;font-size:12px;cursor:pointer;font-weight:600;margin-right:4px}}
.obuy{{background:#047857;border:1px solid #059669;color:#fff}}.obuy:hover{{background:#047857}}
.osell{{background:#dc2626;border:1px solid #dc2626;color:#fff}}.osell:hover{{background:#b91c1c}}
.acts button.ghost{{background:transparent}}.obuy.ghost{{color:var(--ax-pos,#059669)}}.osell.ghost{{color:var(--ax-neg,#dc2626)}}
.acts button.ghost:hover{{color:#fff}}.obuy.ghost:hover{{background:#047857}}.osell.ghost:hover{{background:#dc2626}}
.acts .nobuy{{display:inline-block;min-width:44px;padding:3px 6px;margin-right:4px;font-size:12px;color:var(--ax-muted,#94a3b8);border:1px dashed var(--ax-border,#475569);border-radius:5px;text-align:center;cursor:help}}
.modal .envbadge{{display:inline-block;margin-left:8px;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:700;vertical-align:middle}}
.modal .envbadge.paper{{background:#0e7490;color:#fff}}.modal .envbadge.live{{background:#dc2626;color:#fff}}
.modal .envline{{margin:6px 0 10px;padding:7px 10px;border-radius:6px;font-size:12px}}.modal .envline.paper{{background:var(--ax-info-bg,#083344);color:var(--ax-info,#a5f3fc)}}.modal .envline.live{{background:var(--ax-neg-bg,#450a0a);color:var(--ax-neg,#fecaca);border:1px solid #dc2626}}
.modal .nobuynote{{display:none;margin:-4px 0 8px;font-size:12px;color:var(--ax-warn,#fbbf24)}}.modal .nobuynote.show{{display:block}}
#mSide button:disabled{{opacity:.35;cursor:not-allowed}}
.tod-card .acts button{{padding:6px 20px;font-size:12.5px;margin-top:4px}}
.cmpwarn{{color:var(--ax-warn,#f59e0b);font-size:12px;margin-bottom:8px;display:none}}.cmpwarn.show{{display:block}}
.brk{{background:var(--ax-surface-2,#0f172a);border:1px solid var(--ax-border,#334155);border-radius:7px;padding:10px;margin:10px 0}}
.brk .mrow{{margin-bottom:6px}}.brk input[type=number]{{max-width:80px;flex:0 0 auto}}
.u{{font-size:12px;color:var(--ax-muted,#64748b)}}
.modal-bg{{display:none;position:fixed;inset:0;background:#00000090;z-index:990;align-items:center;justify-content:center}}.modal-bg.open{{display:flex}}
.modal{{background:var(--ax-surface,#1e293b);border:1px solid var(--ax-border,#334155);border-radius:10px;padding:18px;width:420px;max-width:92vw;max-height:88vh;overflow-y:auto}}
.modal h3{{font-size:15px;margin-bottom:4px}}.modal .sub{{font-size:12px;color:var(--ax-muted,#64748b);margin-bottom:12px}}
.mrow{{display:flex;align-items:center;gap:8px;margin-bottom:9px;flex-wrap:wrap}}.mrow label{{width:64px;font-size:12px;color:var(--ax-muted,#94a3b8);flex-shrink:0}}
.mrow input,.mrow select{{flex:1;min-width:90px}}
.seg{{display:inline-flex;border:1px solid var(--ax-border,#334155);border-radius:6px;overflow:hidden}}.seg button{{border:none;background:var(--ax-surface-2,#0f172a);color:var(--ax-muted,#94a3b8);padding:5px 16px;font-size:12px;cursor:pointer}}.seg button.on[data-v="BUY"]{{background:#047857;color:#fff}}.seg button.on[data-v="SELL"]{{background:#dc2626;color:#fff}}
.preview{{background:var(--ax-surface-2,#0f172a);border:1px dashed var(--ax-border,#334155);border-radius:6px;padding:9px;font-size:12px;color:var(--ax-text-2,#cbd5e1);margin:10px 0;line-height:1.5}}
.mactions{{display:flex;gap:8px;margin-top:6px}}.mactions button{{flex:1;padding:8px;border:none;border-radius:6px;font-size:12.5px;cursor:pointer}}.msave{{background:var(--ax-brand,#2563eb);color:var(--ax-brand-ink,#fff)}}.mcancel{{background:var(--ax-surface-3,#334155);color:var(--ax-text,#e2e8f0)}}
.banner{{font-size:12px;padding:5px 12px}}.banner.dry{{background:var(--ax-warn-bg,#fef9c3);color:var(--ax-warn,#78350f)}}.banner.live{{background:var(--ax-neg-bg,#fecaca);color:var(--ax-neg,#7f1d1d);font-weight:700}}
.pend{{background:var(--ax-surface,#1e293b);border:1px solid #dc2626;border-radius:8px;padding:8px 12px;margin-bottom:10px;display:none}}.pend.show{{display:block}}
.pend-item{{display:flex;justify-content:space-between;align-items:center;padding:5px 0;font-size:12px;border-top:1px solid var(--ax-border-soft,#33415555)}}.pend-item:first-of-type{{border-top:none}}
.pend-item button{{border:none;border-radius:4px;padding:3px 10px;font-size:12px;cursor:pointer;margin-left:5px}}.pconf{{background:#047857;color:#fff}}.prej{{background:#dc2626;color:#fff}}
.rules-mini{{margin-top:12px;font-size:12px}}.rules-mini li{{display:flex;justify-content:space-between;background:var(--ax-surface-2,#0f172a);border-radius:5px;padding:5px 8px;margin-bottom:4px;list-style:none}}
.hide{{display:none!important}}

/* 3 Nov 2026 (UX audit H4, H5, M1, M5): status strip, five KPIs, grouped tabs, legend, phone layout, order ticket */
.nowrap{{white-space:nowrap}}
.sstrip{{margin:10px 18px 0;border:1px solid var(--ax-border,#334155);border-radius:10px;background:var(--ax-surface,#1e293b);overflow:hidden}}
.ss-head{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:6px 12px;font-size:12.5px;color:var(--ax-muted,#94a3b8)}}
.ss-meta{{margin-right:auto}}
.ss-more{{background:none;border:1px solid var(--ax-border,#334155);color:var(--ax-brand,#38bdf8);border-radius:6px;padding:3px 10px;font-size:12.5px;cursor:pointer}}
.ss-items > .ss-item[data-hide="1"]{{display:none}}
body.ax-shelled .ss-item #atipOpBanner{{display:none!important}}
.ss-items .banner, .ss-items details, .ss-items > .ss-item > div{{font-size:12.5px!important}}
.kpi-row{{align-items:stretch}}
.kpi-more{{flex:1 1 100%}}
.kpi-more > summary{{cursor:pointer;font-size:12.5px;color:var(--ax-brand,#38bdf8);font-weight:600;padding:4px 0}}
.kpi-more-body{{display:flex;flex-wrap:wrap;gap:8px;margin-top:6px}}
.kpi-more .sidebar{{width:auto;min-height:0;flex:1 1 240px;border:1px solid var(--ax-border,#334155);border-radius:8px}}
details.sector > summary{{cursor:pointer;list-style:revert}}
details.sector .ss-sum{{font-weight:400;color:var(--ax-text,#e2e8f0);font-size:13px}}
.sec-l{{font-size:12px;color:var(--ax-text,#e2e8f0)}}.sec-v{{font-size:14px;font-weight:700;color:var(--ax-text,#fff)}}
.tgroups{{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px}}
.tgroups button{{border:1px solid var(--ax-border,#334155);background:var(--ax-surface,#1e293b);color:var(--ax-text,#e2e8f0);border-radius:999px;padding:5px 14px;font-size:13px;font-weight:600;cursor:pointer}}
.tgroups button[aria-pressed="true"]{{background:var(--ax-text,#e2e8f0);color:var(--ax-surface,#0f172a);border-color:var(--ax-text,#e2e8f0)}}
.tabs{{flex-wrap:wrap}}.tab[data-hide="1"]{{display:none}}
.tabs:not([data-ready]) > .tab:nth-child(n+3){{display:none}}
.tab:focus-visible,.tgroups button:focus-visible,.tc:focus-visible{{outline:3px solid var(--ax-focus,#93c5fd);outline-offset:2px}}
.tc{{overflow-x:auto;max-width:100%}}
.sp{{display:inline-block;min-width:30px;padding:1px 7px;border-radius:4px;font-weight:600;font-size:12.5px;text-align:center}}
.sp-strong{{background:var(--ax-pos-bg,#05966920);color:var(--ax-pos,#059669)}}.sp-neutral{{background:var(--ax-warn-bg,#f59e0b20);color:var(--ax-warn,#f59e0b)}}.sp-weak{{background:var(--ax-neg-bg,#dc262620);color:var(--ax-neg,#dc2626)}}
.mlab{{width:64px;font-size:13px;color:var(--ax-muted,#94a3b8);flex-shrink:0}}
.modal{{width:min(480px,94vw)}}
.chk{{display:inline-flex!important;align-items:center;gap:6px;width:auto!important;color:var(--ax-text,#e2e8f0)!important;white-space:nowrap}}
.chk input{{flex:none;min-width:0;width:16px;height:16px}}
.sw{{display:grid;grid-template-columns:auto 1fr;gap:2px 10px;align-items:center;margin:10px 0}}
.sw label{{font-size:13px;font-weight:600;color:var(--ax-text,#e2e8f0)}}
.sw .swhelp{{grid-column:2;font-size:12px;color:var(--ax-muted,#94a3b8);line-height:1.45}}
.sw input[role=switch]{{appearance:none;-webkit-appearance:none;width:40px;height:22px;border-radius:11px;background:var(--ax-surface-3,#334155);border:1px solid var(--ax-border,#475569);position:relative;cursor:pointer;margin:0;padding:0;flex:none;min-width:0}}
.sw input[role=switch]::before{{content:"";position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;background:var(--ax-surface,#fff);box-shadow:0 1px 2px rgba(0,0,0,.4);transition:left .15s}}
.sw input[role=switch]:checked{{background:var(--ax-brand,#2563eb);border-color:var(--ax-brand,#2563eb)}}
.sw input[role=switch]:checked::before{{left:20px}}
.sw input[role=switch]:focus-visible{{outline:3px solid var(--ax-focus,#93c5fd);outline-offset:2px}}
.mwarn{{background:var(--ax-warn-bg,#2a2410);color:var(--ax-warn,#fde68a);border-radius:6px;padding:7px 10px;font-size:12.5px;margin:-4px 0 8px}}
.merr{{background:var(--ax-neg-bg,#3b1114);color:var(--ax-neg,#fca5a5);border-radius:6px;padding:7px 10px;font-size:12.5px;margin:6px 0}}
.mnote{{font-size:12px;color:var(--ax-muted,#94a3b8);line-height:1.5;margin:4px 0 6px}}
.mnote button{{background:none;border:1px solid var(--ax-brand,#38bdf8);color:var(--ax-brand,#38bdf8);border-radius:6px;padding:2px 9px;font-size:12px;cursor:pointer;margin-left:4px}}
.mrisk{{background:var(--ax-surface-2,#0f172a);border:1px solid var(--ax-border,#334155);border-radius:6px;padding:9px;margin:0 0 10px;font-size:12.5px;line-height:1.55}}
.mrisk dl{{display:grid;grid-template-columns:auto 1fr;gap:2px 12px;margin:0}}.mrisk dt{{color:var(--ax-muted,#94a3b8)}}.mrisk dd{{margin:0;font-weight:600;font-variant-numeric:tabular-nums;text-align:right}}
.rmeter{{height:8px;border-radius:4px;background:var(--ax-surface-3,#334155);overflow:hidden;margin:6px 0 2px}}.rmeter span{{display:block;height:100%;border-radius:4px}}
.rm-ok{{background:var(--ax-pos,#059669)}}.rm-warn{{background:var(--ax-warn,#f59e0b)}}.rm-bad{{background:var(--ax-neg,#dc2626)}}
.mrow input,.mrow select{{min-width:0}}
td,th{{font-variant-numeric:tabular-nums}}
#st thead th{{position:sticky;top:0;z-index:2;background:var(--ax-surface-2)}}
@media (max-width:767px){{
 .sstrip{{margin:8px 10px 0}}.kpi-row{{padding:8px 10px}}.kpi{{flex:1 1 140px;min-width:0}}
 .main{{padding:10px;min-width:0}}.body{{display:block}}
 #st th:first-child,#st td:first-child{{display:none}}
 .tc table td:first-child,.tc table th:first-child,#st td:nth-child(2),#st th:nth-child(2){{position:sticky;left:0;z-index:1;background:var(--ax-surface,#1e293b)}}
 .tfilter{{flex-wrap:wrap}}.tfilter input{{flex:1 1 100%}}
 .tod-card{{grid-template-columns:1fr}}
 .mrow label,.mlab{{width:100%}}
}}
</style></head>
<body>
<div class="topbar legacy-only"><div><span class="logo">📊 ATIP</span> <span style="color:#64748b">AI Trading Intelligence Platform</span></div><div style="display:flex;gap:10px;align-items:center"><a href="/command" style="font-size:12px;color:#fbbf24;text-decoration:none;font-weight:600">Command Center</a><a href="/global-asia" style="font-size:12px;color:#38bdf8;text-decoration:none">Global &amp; Asia</a><a href="/wealth" style="font-size:12px;color:#38bdf8;text-decoration:none">Wealth</a><a href="/strategies" style="font-size:12px;color:#38bdf8;text-decoration:none">Strategies</a><a href="/trading" style="font-size:12px;color:#38bdf8;text-decoration:none">Trading</a><a href="/ml" style="font-size:12px;color:#38bdf8;text-decoration:none">ML</a><a href="/assistant" style="font-size:12px;color:#38bdf8;text-decoration:none">Assistant</a><a href="/brokers" style="font-size:12px;color:#38bdf8;text-decoration:none">Brokers</a><a href="/compliance" style="font-size:12px;color:#38bdf8;text-decoration:none">Compliance</a><a href="/m" style="font-size:12px;color:#38bdf8;text-decoration:none">Mobile</a><a href="/quant" style="font-size:12px;color:#38bdf8;text-decoration:none">Quant</a><a href="/market" style="font-size:12px;color:#38bdf8;text-decoration:none">Market</a><a href="/account" style="font-size:12px;color:#38bdf8;text-decoration:none">Account</a><a href="/admin" style="font-size:12px;color:#38bdf8;text-decoration:none">Admin</a><a href="/backtests" style="font-size:12px;color:#38bdf8;text-decoration:none">Backtests</a></div></div>
<div id="sstrip" class="sstrip" role="region" aria-label="Status">
 <div class="ss-head"><span class="ss-meta">Data as of {gen} IST · <span class="ax-updated" data-ax-updated title="When the last live quote arrived">Updated —</span> · <span id="clk" class="nowrap">Refresh in 5:00</span></span>
  <button type="button" class="rf" onclick="location.reload()">↻ Refresh now</button>
  <button type="button" class="ss-more" id="ssMore" aria-expanded="false" aria-controls="ssItems" hidden></button></div>
 <div class="ss-items" id="ssItems">
 <div class="ss-item" data-sev="op">{op_banner}</div>
 <div class="ss-item" data-sev="{3 if stale_n>=3 else 2 if stale_n>=1 else 0}">{stale_banner}</div>
 <div class="ss-item" data-sev="{3 if health_bad else 0}">{health_panel}</div>
 <div class="ss-item" data-sev="0" id="brokerItem"><div id="brokerBanner" class="banner dry">Checking broker status…</div></div>
 </div>
</div>
<script>
// ── one status strip (UX audit H4): the most severe item shows, the rest behind "+N more" ──
var ssOpen=false;
function ssSev(it){{
  var sv=it.getAttribute('data-sev');
  if(sv!=='op') return [true,parseInt(sv,10)||0];
  var tb=document.getElementById('atipTokenBanner'), ob=document.getElementById('atipOpBanner'), rb=document.getElementById('atipRoleBanner');
  var on=function(x){{return x&&getComputedStyle(x).display!=='none'&&(x.textContent||'').trim();}};
  if(on(tb)) return [true,3];
  if(on(ob)) return [true,/⏸|STOPPED|stopped/.test(ob.textContent)?2:0];
  return [!!on(rb),1];
}}
function ssLayout(){{
  var box=document.getElementById('ssItems'), btn=document.getElementById('ssMore'); if(!box||!btn) return;
  var its=[].slice.call(box.children).map(function(it,i){{var v=ssSev(it);return {{el:it,show:v[0],sev:v[1],i:i}};}});
  its.sort(function(a,b){{return (b.sev-a.sev)||(a.i-b.i);}});
  its.forEach(function(x){{box.appendChild(x.el);}});
  var vis=its.filter(function(x){{return x.show;}}), n=0;
  its.forEach(function(x){{var h=!x.show||(!ssOpen&&x!==vis[0]); x.el.setAttribute('data-hide',h?'1':'0'); if(x.show&&x!==vis[0]) n++;}});
  btn.hidden=!n; btn.textContent=ssOpen?'Show less':('+'+n+' more'); btn.setAttribute('aria-expanded',ssOpen?'true':'false');
}}
ssLayout(); document.getElementById('ssMore').addEventListener('click',function(){{ssOpen=!ssOpen;ssLayout();}});
</script>
<div id="pendBox" class="pend" style="margin:10px 18px 0"><b style="color:#dc2626">⚠️ Awaiting confirmation</b><div id="pendList"></div></div>
<div class="kpi-row" role="region" aria-label="Market at a glance">
  <div class="kpi"><div class="kpi-l">Market Health</div><div class="kpi-v" style="color:{mh_col}">{mh_txt}</div><div style="font-size:11px;color:#64748b">{regime}</div></div>
  <div class="kpi"><div class="kpi-l">Nifty 50</div><div class="kpi-v">{chg(idx.get('nifty50_chg'))}</div><div style="font-size:11px;color:#64748b">{(f"{idx.get('nifty50'):,.2f}" if idx.get('nifty50') else '—')}</div></div>
  <div class="kpi"><div class="kpi-l">India VIX</div><div class="kpi-v" style="color:{'#dc2626' if (idx.get('india_vix') or 0)>20 else '#94a3b8'}">{idx.get('india_vix','—')}</div></div>
  <div class="kpi"><div class="kpi-l">GIFT Nifty</div><div class="kpi-v">{chg(idx.get('gift_nifty_chg') if idx.get('gift_nifty') else None, why="No GIFT Nifty quote in the latest index read" + (f" ({idx.get('time')} IST)" if idx.get('time') else ""))}</div><div style="font-size:11px;color:#94a3b8">{(f"{idx.get('gift_nifty'):,.0f}" if idx.get('gift_nifty') else '—')}</div></div>
  <div class="kpi"><div class="kpi-l">Sentiment</div><div class="kpi-v" style="color:{'#059669' if idx.get('overall_sentiment')=='BULLISH' else '#dc2626' if idx.get('overall_sentiment')=='BEARISH' else '#94a3b8'}">{idx.get('overall_sentiment') or '—'}</div></div>
  <details class="kpi-more"><summary>Global and more</summary><div class="kpi-more-body">
  <div class="kpi"><div class="kpi-l">Bank Nifty</div><div class="kpi-v">{chg(idx.get('banknifty_chg'))}</div><div style="font-size:11px;color:#64748b">{(f"{idx.get('banknifty'):,.2f}" if idx.get('banknifty') else '—')}</div></div>
  <div class="kpi"><div class="kpi-l">S&P 500</div><div class="kpi-v">{chg(glb.get('sp500_chg'))}</div></div>
  <div class="kpi"><div class="kpi-l">Gold</div><div class="kpi-v">{chg(glb.get('gold_chg'))}</div></div>
  <div class="kpi"><div class="kpi-l">USD/INR</div><div class="kpi-v">{chg(glb.get('usd_inr_chg'))}</div></div>
  <div class="sidebar"><h3>NSE Indexes &nbsp;{idx_note}</h3>{idx_rows}</div><div class="sidebar"><h3>Global &nbsp;{glb_note}</h3>{glb_rows}</div></div></details>
</div>
<div class="body">
<div class="main">
  <div class="section"><div class="st">🎯 Trade of the Day</div>
    {tod_none if not tod_buy else ''}<div class="tod-card" data-sym="{tod_sym}"{'' if tod_buy else ' hidden'}>
      <div class="tod-sym">{tod_sym} <span style="font-size:13px;color:#059669">{tod_sig}</span>{f'<div style="font-size:12px;color:#94a3b8;font-weight:400">{tod_cname}</div>' if tod_cname else ''}</div>
      <div><div class="tod-l">CMP <span class="ax-feedmini" data-ax-feed title="The live price feed"><span class="ax-feedtext" hidden></span><span class="ax-feedshort"></span></span></div><div class="tod-v cmpcell" data-prev="{tod.get('prev_close') or ''}" data-cmp="{tod.get('cmp') or ''}">{money(tod.get('cmp'))}{chg_span(tod.get('cmp'), tod.get('prev_close'), size=12)}</div></div>
      {tod_plan}
      <div><div class="tod-l">ACS Confidence</div><div class="tod-v">{(tod.get('acs') or 0):.0f}/100</div></div>
      <div style="grid-column:1/-1;font-size:11px;color:#94a3b8">✦ {tod.get('top_factor_1') or '—'} &nbsp; ✦ {tod.get('top_factor_2') or '—'}</div>
      <div style="grid-column:1/-1" class="acts">{order_btns(tod.get('symbol'),tod.get('cmp'),primary=('SELL' if tod_sig=='SELL' else 'BUY'),signal=tod_sig)}</div>
    </div>
  </div>
  <details class="section sector"><summary class="st">🔄 Sector Rotation <span class="ss-sum">{sector_line}</span> <span class="u">{idx_note} · show all</span></summary>
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">{sector_tiles}</div>
  </details>
  <div class="tgroups" role="group" aria-label="Views"><button type="button" data-g="scores" aria-pressed="true">Scores</button><button type="button" data-g="lists" aria-pressed="false">Lists</button><button type="button" data-g="flows" aria-pressed="false">Flows &amp; News</button><button type="button" data-g="activity" aria-pressed="false">Activity</button></div>
  <div class="tabs" role="tablist" aria-label="Tables"><div class="tab active" onclick="showTab('scores',this)">ATIP Scores</div><div class="tab" onclick="showTab('port',this)">Portfolio</div><div class="tab" onclick="showTab('vpi',this)">Top VPI</div><div class="tab" onclick="showTab('zpi',this)">Buy Zones</div><div class="tab" onclick="showTab('rri',this)">Recovery (RRI)</div><div class="tab" onclick="showTab('mri',this)">Momentum (MRI)</div><div class="tab" onclick="showTab('cri',this)">CRI Risk</div><div class="tab" onclick="showTab('fiidii',this)">FII / DII</div><div class="tab" onclick="showTab('news',this);atipLoadNewsImpact('niNewsBox')">News</div><div class="tab" onclick="showTab('hist',this)">Signal History</div><div class="tab" onclick="showTab('orders',this)">Orders</div><div class="tab" onclick="showTab('livefeeds',this);atipLoadFeeds('lfBox')">Live</div><div class="tab" onclick="showTab('asia',this);atipLoadAsia('gaBox');atipLoadNewsImpact('niAsiaBox')">Global &amp; Asia</div></div>
  <div id="scores" class="tc active section">
    {score_legend}
    <div class="tfilter"><input id="srch" placeholder="Search symbol or company…" aria-label="Search symbol or company" oninput="ft()"><select id="sf" aria-label="Signal" onchange="ft()"><option value="">All signals</option><option>BUY</option><option>SELL</option><option>HOLD</option><option>WAIT</option></select><select id="slim" aria-label="Rows to show" onchange="ft()" title="Rows to show, in the current sort order"><option value="50">Show 50</option><option value="100">Show 100</option><option value="250">Show 250</option><option value="0">Show all</option></select><span class="cnt" id="scnt"></span><span class="cnt">· click a column to sort · click a stock for its history · hover a column name for its meaning</span></div>
    <table id="st"><thead><tr><th>#</th><th>Symbol</th><th>{G.abbr("ATIP")}</th><th>{G.abbr("VPI")}</th><th>{G.abbr("MRI")}</th><th>{G.abbr("RRI")}</th><th>{G.abbr("ZPI")}</th><th>{G.abbr("CRI", "CRI↓")}</th><th>{G.abbr("ACS")}</th><th>CMP <span class="ax-feedmini" data-ax-feed title="The live price feed"><span class="ax-feedtext" hidden></span><span class="ax-feedshort"></span></span></th><th>{G.abbr("BETA", "Beta")}</th><th>Signal</th><th>Factor</th><th>Action</th></tr></thead><tbody>{score_rows or '<tr><td colspan="14" style="text-align:center;color:#64748b;padding:20px">No scores yet. The post-market run computes them at 16:45 IST on trading days.</td></tr>'}</tbody></table>
  </div>
  <div id="port" class="tc section">
    {f'''<div style="display:flex;gap:14px;align-items:flex-start;margin-bottom:12px;flex-wrap:wrap">
      <div style="background:#1e293b;border:1px solid #334155;border-radius:10px;padding:14px 18px;min-width:190px">
        <div class="kpi-l">Portfolio Health</div>
        <div style="font-size:32px;font-weight:700;color:{phs_col}">{phs_score:.0f}</div>
        <div style="font-size:12px;color:#94a3b8">{phs.get("band","—")} · {phs.get("holdings",0)} holdings</div>
        <div style="font-size:11px;color:#94a3b8;margin-top:4px">Portfolio beta {phs.get("portfolio_beta") or "—"} · regime {phs.get("regime","—")}</div>
        {f'<div style="font-size:11px;color:#f59e0b;margin-top:4px">⚠ holdings as of {phs.get("date")} — portfolio sync has not run since</div>' if phs.get("stale") else ''}
      </div>
      <div style="background:#1e293b;border:1px solid #334155;border-radius:10px;padding:12px 16px;min-width:270px">
        <div class="kpi-l" style="margin-bottom:5px">Components</div>{phs_comp}
      </div>
    </div>''' if phs_score is not None else ''}
    <table><thead><tr><th>Symbol</th><th>Qty</th><th>Avg</th><th>CMP <span class="ax-feedmini" data-ax-feed title="The live price feed"><span class="ax-feedtext" hidden></span><span class="ax-feedshort"></span></span></th><th>P&L%</th><th>ATIP</th><th>CRI↓</th><th>Signal</th><th>Action</th></tr></thead><tbody>{port_rows if port_rows else '<tr><td colspan="9" style="text-align:center;color:#64748b;padding:20px">No portfolio data. Configure Dhan (or Zerodha Kite) API and run a portfolio sync.</td></tr>'}</tbody></table></div>
  <div id="vpi" class="tc section"><table><thead><tr><th>Symbol</th><th>{G.abbr("ATIP")}</th><th>{G.abbr("VPI")}</th><th>CMP <span class="ax-feedmini" data-ax-feed title="The live price feed"><span class="ax-feedtext" hidden></span><span class="ax-feedshort"></span></span></th><th>{G.abbr("BETA", "Beta")}</th><th>Signal</th><th>Action</th></tr></thead><tbody>{top25_rows.get('vpi','') or '<tr><td colspan="7" style="text-align:center;color:#64748b;padding:20px">No VPI list for this date yet. It comes with the post-market scores.</td></tr>'}</tbody></table></div>
  <div id="zpi" class="tc section"><table><thead><tr><th>Symbol</th><th>{G.abbr("ZPI")}</th><th>{G.abbr("ATIP")}</th><th>CMP <span class="ax-feedmini" data-ax-feed title="The live price feed"><span class="ax-feedtext" hidden></span><span class="ax-feedshort"></span></span></th><th>{G.abbr("BETA", "Beta")}</th><th>Signal</th><th>Action</th></tr></thead><tbody>{top25_rows.get('zpi','') or '<tr><td colspan="7" style="text-align:center;color:#64748b;padding:20px">No buy-zone list for this date yet. It comes with the post-market scores.</td></tr>'}</tbody></table></div>
  <div id="rri" class="tc section"><table><thead><tr><th>Symbol</th><th>{G.abbr("RRI")}</th><th>{G.abbr("ATIP")}</th><th>CMP <span class="ax-feedmini" data-ax-feed title="The live price feed"><span class="ax-feedtext" hidden></span><span class="ax-feedshort"></span></span></th><th>{G.abbr("BETA", "Beta")}</th><th>Signal</th><th>Action</th></tr></thead><tbody>{top25_rows.get('rri','') or '<tr><td colspan="7" style="text-align:center;color:#64748b;padding:20px">No RRI data for this date.</td></tr>'}</tbody></table></div>
  <div id="mri" class="tc section"><table><thead><tr><th>Symbol</th><th>{G.abbr("MRI")}</th><th>{G.abbr("ATIP")}</th><th>CMP <span class="ax-feedmini" data-ax-feed title="The live price feed"><span class="ax-feedtext" hidden></span><span class="ax-feedshort"></span></span></th><th>{G.abbr("BETA", "Beta")}</th><th>Signal</th><th>Action</th></tr></thead><tbody>{top25_rows.get('mri','') or '<tr><td colspan="7" style="text-align:center;color:#64748b;padding:20px">No MRI data for this date.</td></tr>'}</tbody></table></div>
  <div id="cri" class="tc section"><table><thead><tr><th>Symbol</th><th>{G.abbr("CRI", "CRI 🔴")}</th><th>{G.abbr("ATIP")}</th><th>CMP <span class="ax-feedmini" data-ax-feed title="The live price feed"><span class="ax-feedtext" hidden></span><span class="ax-feedshort"></span></span></th><th>{G.abbr("BETA", "Beta")}</th><th>Signal</th><th>Action</th></tr></thead><tbody>{top25_rows.get('cri','') or '<tr><td colspan="7" style="text-align:center;color:#64748b;padding:20px">No crash-risk list for this date yet. It comes with the post-market scores.</td></tr>'}</tbody></table></div>
  <div id="fiidii" class="tc section"><table><thead><tr><th>Date</th><th>FII net ₹Cr</th><th>DII net ₹Cr</th><th>FII 5-day avg</th><th>DII 5-day avg</th></tr></thead><tbody>{fii_rows if fii_rows else '<tr><td colspan="5" style="text-align:center;color:#64748b;padding:20px">No FII/DII data yet — it is fetched by the post-market Bhavcopy job.</td></tr>'}</tbody></table></div>
  <div id="news" class="tc section">{ni_news_panel}<div class="st" style="margin-top:14px">Headlines</div><table><thead><tr><th style="width:320px">Headline</th><th>Source</th><th>Importance</th><th>Sentiment</th></tr></thead><tbody>{news_rows}</tbody></table></div>
  <div id="hist" class="tc section">
    <div class="st">Momentum success rate &mdash; did price move the way the signal said?</div>
    <table><thead><tr><th>Signal</th>
      <th>Target</th>
      <th title="Signals watched for at least one session since they were issued — the denominator of the hit rate">Watched</th>
      <th>Hits</th>
      <th title="Hits divided by signals watched for at least one session, whether or not they have hit">Hit rate</th>
      <th>Median</th><th>Fastest</th><th>Slowest</th><th>Avg best</th><th>Avg worst</th>
      <th title="Issued too recently to have a forward session yet — in neither the hits nor the denominator">Too new</th>
      <th title="Hit rate over signals issued in the last 12 months">Last 1 year</th></tr></thead>
      <tbody>{succ_rows}</tbody></table>
    {sh_note}
    <div class="st" style="margin-top:16px">Signal log &mdash; {sh_total} signals, {sh_span} (append-only)
      <span style="font-size:11px;color:#94a3b8;font-weight:400">&nbsp; "Last 1 year" counts signals issued since {sh_since_1y}</span></div>
    <div style="display:flex;gap:6px;margin-bottom:8px">
      <input id="hsrch" placeholder="Filter by symbol..." aria-label="Filter the signal log by symbol" oninput="hft()">
      <select id="hsf" aria-label="Signal" onchange="hft()"><option value="">All signals</option><option>BUY</option><option>SELL</option></select>
      <span id="hcnt" style="font-size:11px;color:#64748b;align-self:center"></span>
    </div>
    <table id="ht"><thead><tr><th title="Date the signal was issued">Issued</th><th>Symbol</th><th>Signal</th><th>Entry</th><th>ATIP</th><th>ZPI</th><th title="Crash-Risk Index: lower is better">CRI↓</th>
      {th_heads}<th title="Date the first target was reached">Target hit</th><th>Best</th><th>Worst</th><th>Model</th></tr></thead>
      <tbody>{hist_rows_or_empty}</tbody></table>
  </div>
  <div id="orders" class="tc section">
    <div id="brokerBanner2" class="banner dry" style="margin-bottom:12px">Checking broker status…</div>
    <div class="st">Order book — every order and rule ATIP is tracking, in every state</div>
    <div style="font-size:11.5px;color:#94a3b8;margin-bottom:10px;line-height:1.5">
      Two kinds of row here. <b style="color:#38bdf8">[ENTRY]/[TARGET]/[STOP]</b> rules are
      conditional watches: a rule with no order id has never been sent anywhere — it is still
      watching its trigger price, checked by this dashboard every 30 seconds, and Dhan has no
      idea it exists yet. <b style="color:#94a3b8">[DIRECT]</b> rows are orders placed
      immediately — via the CLI or the aggressive strategy — with no trigger to wait for.
      For either kind: PAPER means it filled in the simulator at a live price and never
      reached Dhan; LIVE means it reached Dhan for real. This is different from a
      broker-side GTT order: ATIP's rules are watched HERE, not on Dhan's servers.
    </div>
    <table id="obt"><thead><tr><th>Created</th><th>Symbol</th><th>Side</th><th>Trigger</th>
      <th>Qty</th><th>Status</th><th>Mode</th><th>Fill</th><th>Order ID</th><th>Action</th></tr></thead>
      <tbody>{order_rows_or_empty}</tbody></table>
  </div>
  {asia_tab}
  {live_tab}
  <p class="disc">⚠️ ATIP is for personal informational use only. Not financial advice. All AI scores are model outputs — verify independently. Not SEBI registered. Consult a registered advisor before investing.</p>
</div></div>

<div class="modal-bg" id="modalBg"><div class="modal" role="dialog" aria-modal="true" aria-labelledby="mTitle" id="mBox" data-costs="{costs_attr}">
  <h3 id="mTitle">Order Rule — <span id="mSym"></span><span class="envbadge {env_cls}" id="mEnv">{env_now}</span></h3>
  <div class="sub" id="mCmp"></div>
  <div class="envline {env_cls}" id="mEnvLine" role="status">{env_line}</div>
  <div class="nobuynote" id="mNoBuy">Buy is locked: this stock's signal is <b id="mSig"></b>. Buy is available only on a BUY signal.</div>
  <div class="cmpwarn" id="mCmpWarn">⚠ No reference price available for this stock — enter an absolute target price (% move needs a reference).</div>
  <div class="mrow"><span class="mlab" id="mSideL">Side</span><div class="seg" id="mSide" role="group" aria-labelledby="mSideL"><button type="button" class="on" data-v="BUY" onclick="setSide('BUY')">Buy</button><button type="button" data-v="SELL" onclick="setSide('SELL')">Sell</button></div></div>
  <div class="mrow"><label for="mTV">Target</label><select id="mTT" aria-label="Target type" onchange="mPrev()"><option value="PRICE">At price ₹</option><option value="PERCENT">% move</option></select><input type="number" step="0.01" id="mTV" placeholder="e.g. 2500" oninput="mPrev()"></div>
  <div class="mrow"><label for="mQV">Qty</label><select id="mQT" aria-label="Quantity type" onchange="mPrev()"><option value="SHARES">Shares</option><option value="AMOUNT">₹ Amount</option><option value="RISK">% capital at risk (BUY + stoploss)</option></select><input type="number" step="0.01" id="mQV" placeholder="e.g. 10" oninput="mPrev()"></div>
  <div class="mrow"><label for="mProd">Product</label><select id="mProd" onchange="mPrev()"><option value="CNC">Delivery</option><option value="INTRADAY">Intraday</option></select><select id="mOT" aria-label="Order type" onchange="mPrev()"><option value="MARKET">Market</option><option value="LIMIT">Limit</option></select><input type="number" step="0.01" id="mLimit" class="hide" aria-label="Limit price, ₹" placeholder="Limit ₹" oninput="mPrev()"></div>
  <div class="brk">
    <div style="font-size:12.5px;color:#38bdf8;font-weight:600;margin-bottom:7px">🎯 Bracket — auto-exit after this order fills</div>
    <div class="mrow"><label class="chk"><input type="checkbox" id="mBrk" checked onchange="mPrev()"> Use a bracket</label>
      <span class="u">exits are measured from the actual fill price</span></div>
    <div class="mrow"><label for="mBT1">Target 1</label><input type="number" step="0.1" id="mBT1" value="3" oninput="mPrev()"><span class="u">% profit, exits</span><input type="number" step="5" id="mBSplit" value="50" aria-label="Share of the quantity Target 1 exits, %" oninput="mPrev()" style="max-width:60px"><span class="u">% of qty</span></div>
    <div class="mrow"><label for="mBT2">Target 2</label><input type="number" step="0.1" id="mBT2" value="6" oninput="mPrev()"><span class="u">% — exits the rest (blank = one target)</span></div>
    <div class="mrow"><label for="mBSL">Stoploss</label><input type="number" step="0.1" id="mBSL" value="2" oninput="mPrev()"><span class="u">% loss</span></div>
    <div class="mrow"><label class="chk"><input type="checkbox" id="mTrail" checked onchange="mPrev()"> Trail the stop</label>
      <input type="number" step="0.1" id="mTrailV" value="2" aria-label="Trail distance, % below the high" oninput="mPrev()"><span class="u">% below the high</span>
      <input type="number" step="0.5" id="mTrailJ" value="0" aria-label="Trail step, ₹ (0 = smooth)" oninput="mPrev()" style="max-width:60px"><span class="u">₹ step (0 = smooth)</span></div>
    <div class="mnote">Trailing is worked out by this dashboard, not by Dhan, so it pauses whenever the dashboard is stopped. <button type="button" onclick="checkBroker()">Check broker</button> to see whether your Dhan connection can hold the stop on Dhan's side instead.<div id="mBrkCap" role="status"></div></div>
    <div class="sw"><input type="checkbox" role="switch" id="mBAuto" checked onchange="mPrev()"><label for="mBAuto">Exit legs fire without asking me</label>
      <div class="swhelp">On by default: a stop that waits for your tap protects nothing while you are away. It still refuses to act on a quote older than 3 minutes.</div></div>
  </div>
  <div class="sw"><input type="checkbox" role="switch" id="mConfirm" checked onchange="mPrev()"><label for="mConfirm">Require confirmation before the ENTRY order</label>
    <div class="swhelp">On: when the trigger is hit, ATIP asks you before it places the entry.</div></div>
  <div class="mwarn" id="mConfWarn" role="alert" hidden>⚠ Entry confirmation is off: when the trigger is hit, ATIP places this entry at once, without asking you.</div>
  <div class="preview" id="mPreviewTxt">—</div>
  <div class="mrisk" id="mRisk" aria-live="polite"></div>
  <div class="merr" id="mErr" role="alert" hidden></div>
  <div class="rules-mini"><b style="font-size:12px;color:#64748b">Active rules for this stock</b><ul id="mRulesList"></ul></div>
  <div class="mactions"><button class="msave" onclick="saveOrderRule()">Save rule · {env_now}</button><button class="mcancel" onclick="closeOrderModal()">Close</button></div>
</div></div>
<script>
// Every route that changes anything needs this token (dashboard/security.py).
// It is served with the page, so the dashboard works untouched, while a script
// on another site cannot set the header without a preflight this server refuses.
const ATIP_TOKEN={token_js};
function afetch(u,o){{o=o||{{}};o.headers=Object.assign({{}},o.headers||{{}},{{'X-ATIP-Token':ATIP_TOKEN}});return fetch(u,o);}}
function showTab(id,el){{document.querySelectorAll('.tc').forEach(t=>t.classList.remove('active'));document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));document.getElementById(id).classList.add('active');el.classList.add('active');}}
let ss={{}};
// Sort any table column. Three things the previous version got wrong:
//   * it parsed the RENDERED text, so a CMP cell reading "Rs3176.70 +2.45 (0.08%)"
//     collapsed to a meaningless number once the change was added beside it;
//   * a missing value (an em dash) became NaN and silently fell back to a STRING
//     compare for the whole pair, so one blank beta scrambled the column;
//   * nothing on screen said the headers were clickable at all.
// Cells that carry a `data-v` attribute are sorted on that; everything else
// falls back to text. Blanks always sort last, in both directions, because a
// missing value is not "the smallest value".
function cellVal(td){{
  if(!td) return null;
  if(td.dataset && td.dataset.v!==undefined) {{
    if(td.dataset.v==='') return null;
    var d=parseFloat(td.dataset.v); return isNaN(d)?null:d;
  }}
  var t=(td.innerText||'').trim();
  if(!t||t==='—'||t==='-') return null;
  var m=t.replace(/[,₹%]/g,'').match(/-?\\d+(\\.\\d+)?/);
  return m?parseFloat(m[0]):t.toLowerCase();
}}
function srt(tid,col){{
  const tb=document.getElementById(tid); if(!tb) return;
  const rows=[...tb.querySelectorAll('tbody tr')];
  const asc=ss[tid+col]!==true; ss[tid+col]=asc;
  rows.sort((a,b)=>{{
    const av=cellVal(a.cells[col]), bv=cellVal(b.cells[col]);
    if(av===null&&bv===null) return 0;
    if(av===null) return 1;          // blanks last, whichever direction
    if(bv===null) return -1;
    if(typeof av==='number'&&typeof bv==='number') return asc?av-bv:bv-av;
    return asc?String(av).localeCompare(String(bv)):String(bv).localeCompare(String(av));
  }});
  const tbody=tb.querySelector('tbody'); rows.forEach(r=>tbody.appendChild(r));
  tb.querySelectorAll('thead th').forEach((th,i)=>{{
    th.dataset.sorted = (i===col) ? (asc?'asc':'desc') : '';
    if(th.dataset.sortable) th.setAttribute('aria-sort', (i===col) ? (asc?'ascending':'descending') : 'none');
  }});
}}
// Make every table sortable, and make it look sortable. Sorting already existed
// on the ATIP Scores table but nothing indicated it, and the Top-25 tables --
// which also show Beta -- had none at all.
function makeSortable(){{
  document.querySelectorAll('.tc table, #st').forEach(tb=>{{
    if(!tb.id) tb.id='t'+Math.random().toString(36).slice(2,9);
    tb.querySelectorAll('thead th').forEach((th,i)=>{{
      if(th.dataset.sortable) return;
      var label=(th.innerText||'').trim();
      if(label==='Action'||label==='') return;
      th.dataset.sortable='1';
      th.style.cursor='pointer';
      th.title='Sort by '+label;
      th.addEventListener('click',function(){{srt(tb.id,i);}});
      /* Verification 4 Nov 2026 (VF-27): sortable from the keyboard too, and the sort is announced */
      th.tabIndex=0; th.setAttribute('aria-sort','none');
      th.addEventListener('keydown',function(e){{if(e.key==='Enter'||e.key===' '){{e.preventDefault();srt(tb.id,i);}}}});
    }});
  }});
}}
document.addEventListener('DOMContentLoaded',makeSortable);
makeSortable();
// ft() / hft() (ATIP Scores / Signal History filters) live in dashboard/stock_view.py:
// they match the signal exactly -- matching the row text hit every row through its
// Buy / Sell buttons -- and search symbol + company only.
function ft(){{}} function hft(){{}}

// ── Live clock + auto page refresh (topbar, right side) ────────────────
var AUTO_REFRESH_SECONDS=300, _secsLeft=AUTO_REFRESH_SECONDS;
function tickClock(){{
  // nothing you are typing into is thrown away: no reload while the order ticket or a dialog is open
  var mb=document.getElementById('modalBg');
  if((mb&&mb.classList.contains('open'))||document.querySelector('dialog[open]')) _secsLeft=Math.max(_secsLeft,30);
  var el=document.getElementById('clk'), t=Math.max(0,_secsLeft);
  if(el) el.textContent='Refresh in '+Math.floor(t/60)+':'+(t%60<10?'0':'')+(t%60);
  _secsLeft--;
  if(_secsLeft<0) location.reload();
}}
setInterval(tickClock,1000); tickClock();

// ── Order rule modal ─────────────────────────────────────────────────
var mSymbol='', mCmpVal=0, mSideVal='BUY', mSig=null;
function setSide(v){{if(v==='BUY'&&mSig!==null&&mSig!=='BUY')return;mSideVal=v;document.querySelectorAll('#mSide button').forEach(b=>b.classList.toggle('on',b.dataset.v===v));mPrev();}}
function liveCmpFor(sym){{
  // The live-quote poller rewrites .cmpcell every 15s, so it's a fresher
  // reference price than the EOD close baked into the page at render time —
  // and it's the only source when the EOD join came back empty (cmp=0).
  var el=document.querySelector('[data-sym="'+sym+'"] .cmpcell')||document.querySelector('[data-sym="'+sym+'"].cmpcell');
  if(!el)return 0;
  // Read the numeric attribute rather than parsing the cell text: the cell now
  // also carries the absolute and percentage change, and a text parse would
  // happily read part of that as the price.
  var dv=parseFloat(el.dataset.cmp||'');
  if(!isNaN(dv)&&dv>0)return dv;
  var v=parseFloat((el.textContent||'').replace(/[^0-9.]/g,''));
  return (!isNaN(v)&&v>0)?v:0;
}}
function openOrderModal(sym,cmp,side,signal){{
  mSymbol=sym;
  mCmpVal=(cmp&&cmp>0)?cmp:liveCmpFor(sym);
  // Buy only on a BUY signal: a known non-BUY signal locks the Buy side.
  // An unknown signal (undefined, e.g. from a page that doesn't pass one) leaves it open.
  mSig=(signal===undefined||signal===null)?null:String(signal).toUpperCase();
  var buyLocked=(mSig!==null&&mSig!=='BUY');
  mSideVal=(buyLocked?'SELL':(side||'BUY'));
  var bb=document.querySelector('#mSide button[data-v="BUY"]'); if(bb){{bb.disabled=buyLocked;bb.title=buyLocked?'Buy is available only on a BUY signal':'';}}
  document.getElementById('mNoBuy').classList.toggle('show',buyLocked);
  document.getElementById('mSig').textContent=mSig||'';
  document.getElementById('mSym').textContent=sym;
  document.getElementById('mCmp').textContent=mCmpVal>0?('CMP ₹'+mCmpVal):'CMP unavailable';
  // Without a reference price a PERCENT trigger resolves against 0, so lock
  // the form to an absolute price instead of silently storing a bad rule.
  var noCmp=!(mCmpVal>0);
  document.getElementById('mCmpWarn').classList.toggle('show',noCmp);
  var tt=document.getElementById('mTT');
  tt.value='PRICE'; tt.disabled=noCmp;
  document.querySelectorAll('#mSide button').forEach(b=>b.classList.toggle('on',b.dataset.v===mSideVal));
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
function brkVals(){{
  if(!document.getElementById('mBrk').checked) return null;
  var t1=parseFloat(document.getElementById('mBT1').value);
  var t2=parseFloat(document.getElementById('mBT2').value);
  var sl=parseFloat(document.getElementById('mBSL').value);
  var sp=parseFloat(document.getElementById('mBSplit').value);
  var tv=parseFloat(document.getElementById('mTrailV').value);
  var tj=parseFloat(document.getElementById('mTrailJ').value);
  return {{t1:isNaN(t1)?null:t1, t2:isNaN(t2)?null:t2, sl:isNaN(sl)?null:sl,
           auto:document.getElementById('mBAuto').checked,
           split:isNaN(sp)?0.5:Math.min(0.95,Math.max(0.05,sp/100)),
           trail:document.getElementById('mTrail').checked,
           trailV:isNaN(tv)?null:tv, trailJ:isNaN(tj)?0:tj}};
}}
function mPrev(){{
  var qv=document.getElementById('mQV').value||'?', qtv=document.getElementById('mQT').value, qt=qtv==='SHARES'?'shares':qtv==='RISK'?'% of capital at risk, sized at execution from the stoploss':'₹ worth';
  var tp=mResolvedTrigger();
  var entryTxt=(mSideVal==='BUY'?'Buy ':'Sell ')+qv+' '+qt+' of '+mSymbol+' at/'+(mSideVal==='BUY'?'below':'above')+' ₹'+(tp!==null?tp.toFixed(2):'—');
  var confTxt=document.getElementById('mConfirm').checked?'asks you to confirm':'⚡ places automatically';
  var b=brkVals(), brkTxt='no bracket — nothing will exit this position for you';
  if(b){{
    var parts=[];
    // Shown against the trigger as an estimate; the real legs are built from
    // the actual fill price once the entry executes.
    var pc1=Math.round(b.split*100), pc2=100-pc1;
    if(b.t1) parts.push('T1 +'+b.t1+'% exits '+pc1+'%'+(tp?' (~₹'+(tp*(1+b.t1/100)).toFixed(2)+')':''));
    if(b.t2) parts.push('T2 +'+b.t2+'% exits '+pc2+'%'+(tp?' (~₹'+(tp*(1+b.t2/100)).toFixed(2)+')':''));
    if(b.sl) parts.push('SL -'+b.sl+'%'+(tp?' (~₹'+(tp*(1-b.sl/100)).toFixed(2)+')':'')+(b.trail&&b.trailV?' trailing '+b.trailV+'%'+(b.trailJ?' in ₹'+b.trailJ+' steps':''):''));
    brkTxt=parts.length?('then auto-exit: '+parts.join(' · ')+(b.auto?' [fires automatically]':' [asks first]')):'bracket on but no levels set';
  }}
  document.getElementById('mPreviewTxt').textContent=entryTxt+' — '+confTxt+'.  '+brkTxt+'.';
  document.getElementById('mConfWarn').hidden=document.getElementById('mConfirm').checked;
  document.getElementById('mLimit').classList.toggle('hide',document.getElementById('mOT').value!=='LIMIT');
  mRiskBox(tp);
}}
// ── one money format (UX audit M4): Indian grouping and 2 decimals, as dashboard/ux_kit.inr ──
function inrf(v){{if(v===null||v===undefined||v===''||isNaN(v))return '—';var n=Number(v),neg=n<-0.004,p=Math.abs(n).toFixed(2).split('.'),w=p[0];if(w.length>3){{w=w.slice(0,-3).replace(/\\B(?=(\\d{{2}})+(?!\\d))/g,',')+','+w.slice(-3);}}return (neg?'−':'')+'₹'+w+'.'+p[1];}}
// ── money at risk and charges (UX audit M5): backtest/costs.py's models, sent with the page ──
var MC=null;
function mCosts(){{if(MC===null){{try{{MC=JSON.parse(document.getElementById('mBox').dataset.costs||'{{}}');}}catch(e){{MC={{}};}}}}return MC;}}
function mCharge(side,value,prod){{
  var m=mCosts()[prod]||null; if(!m||!(value>0)) return null;
  if(m.name==='flat') return value*m.flat_pct/100;
  var br=Math.max(value*m.brokerage_pct/100,m.brokerage_min||0); if(m.brokerage_max!=null) br=Math.min(br,m.brokerage_max);
  var stt=value*(side==='BUY'?m.stt_buy_pct:m.stt_sell_pct)/100, ex=value*m.exchange_txn_pct/100, sebi=value*m.sebi_fee_pct/100;
  var stamp=side==='BUY'?value*m.stamp_buy_pct/100:0, gst=(br+ex+sebi)*m.gst_pct/100, dp=side==='SELL'?(m.dp_charge_per_sell||0):0;
  return br+stt+ex+sebi+stamp+gst+dp;
}}
function mRiskBox(tp){{
  var box=document.getElementById('mRisk'); if(!box) return;
  var ot=document.getElementById('mOT').value, lim=parseFloat(document.getElementById('mLimit').value);
  var e=(ot==='LIMIT'&&!isNaN(lim))?lim:tp, prod=document.getElementById('mProd').value;
  var qt=document.getElementById('mQT').value, qv=parseFloat(document.getElementById('mQV').value);
  var env=(document.getElementById('mEnv')||{{}}).textContent||'PAPER', buy=mSideVal==='BUY';
  if(!(e>0)||isNaN(qv)||qv<=0){{box.innerHTML='<span class="u">Type a target and a quantity to see the money at risk and the charges.</span> Goes to <b>'+env+'</b>.';return;}}
  if(qt==='RISK'){{box.innerHTML='<dl><dt>Money at risk</dt><dd>'+qv+'% of capital</dd><dt>Shares</dt><dd>sized when it fills, from the stoploss</dd><dt>Goes to</dt><dd>'+env+'</dd></dl>';return;}}
  var q=qt==='SHARES'?Math.floor(qv):Math.floor(qv/e);
  if(q<1){{box.innerHTML='<span class="u">'+(qt==='SHARES'?'Enter at least 1 share.':inrf(qv)+' buys less than one share at '+inrf(e)+'.')+'</span>';return;}}
  var val=q*e, b=brkVals(), stop=(b&&b.sl)?(buy?e*(1-b.sl/100):e*(1+b.sl/100)):null;
  // a buy pays its own charges and the sale's; a sell (an exit) pays only its own
  var c1=mCharge(mSideVal,val,prod), c2=buy?mCharge('SELL',stop?q*stop:val,prod):0, ch=(c1||0)+(c2||0);
  var risk=stop!==null?q*Math.abs(e-stop)+ch:val, pc=val>0?risk/val*100:0;
  var tone=stop===null?'bad':(pc<=2.5?'ok':(pc<=5?'warn':'bad')), word={{ok:'small',warn:'medium',bad:'large'}}[tone];
  box.innerHTML='<dl><dt>Order value</dt><dd>'+q+' × '+inrf(e)+' = '+inrf(val)+'</dd>'+
    '<dt>'+(buy?'Money at risk':'Exit value')+'</dt><dd>'+(buy?(stop!==null?inrf(risk)+' if the stop at '+inrf(stop)+' is hit':'all '+inrf(val)+': no stoploss set'):inrf(val))+'</dd>'+
    '<dt>Estimated charges</dt><dd>'+(c1===null?'—':inrf(ch)+' ('+(buy?'buy '+inrf(c1)+' + sell '+inrf(c2):'this sale')+', '+(prod==='INTRADAY'?'intraday':'delivery')+')')+'</dd>'+
    '<dt>Goes to</dt><dd>'+env+'</dd></dl>'+
    (buy?'<div class="rmeter" role="meter" aria-valuemin="0" aria-valuemax="10" aria-valuenow="'+Math.min(10,pc).toFixed(1)+'" aria-label="Risk meter: '+pc.toFixed(1)+' per cent of the order, '+word+'"><span class="rm-'+tone+'" style="width:'+Math.min(100,pc*10).toFixed(0)+'%"></span></div><span class="u">Risk meter: '+pc.toFixed(1)+'% of the order value, '+word+' (stop loss and charges)</span>':'');
}}
function mErr(m){{var el=document.getElementById('mErr'); if(!el) return; el.hidden=!m; el.textContent=m||''; if(m&&window.AX&&AX.toast) AX.toast(m,true);}}
// "Check broker" (UX audit M5): what the installed Dhan SDK offers, in words, from /api/orders/broker-capability
async function checkBroker(){{
  var el=document.getElementById('mBrkCap'); el.textContent='Checking…';
  try{{
    var r=await fetch('/api/orders/broker-capability'); var c=await r.json();
    if(!r.ok) throw new Error(c.error||r.statusText);
    el.textContent=!c.sdk_version?'The Dhan SDK is not installed here, so the trailing stop stays on this dashboard.':
      ((c.supports_gtt||c.supports_super_order)?('Your Dhan SDK ('+c.sdk_version+') has a broker-side order type (GTT or super order) that could hold the stop. ATIP still trails it here today.'):
      ('Your Dhan SDK ('+c.sdk_version+') has no broker-side stop for delivery trades, so the trailing stop stays on this dashboard.'));
  }}catch(e){{el.textContent='Could not check the broker: '+e.message;}}
}}
// The shell's dialog and toast (UX audit H7); on the bare page (?shell=0) nothing is placed without it
function uiSay(m,bad){{if(window.AX&&AX.toast){{AX.toast(m,bad);}}else{{var b=document.getElementById('brokerBanner'); if(b) b.textContent=m;}}}}
function uiPriceMoved(e,rule){{
  if(window.AX&&AX.priceMoved) return AX.priceMoved(e,rule);
  uiSay('The price moved past the slippage limit. Open this page with the menu to place it anyway.',true);
  return Promise.resolve(false);
}}
async function saveOrderRule(){{
  if(mSideVal==='BUY'&&mSig!==null&&mSig!=='BUY'){{mErr('Buy is available only on a BUY signal (signal now: '+mSig+').');return;}}
  var tt=document.getElementById('mTT').value;
  // Validate before POSTing — an empty target or qty used to be sent as
  // null/NaN, which the API rejected with a bare 400 and no visible reason.
  if(mResolvedTrigger()===null){{mErr('Enter a target '+(tt==='PRICE'?'price':'% move')+' first.');document.getElementById('mTV').focus();return;}}
  var qv=parseFloat(document.getElementById('mQV').value);
  if(isNaN(qv)||qv<=0){{mErr('Enter a quantity (shares or ₹ amount) greater than 0.');document.getElementById('mQV').focus();return;}}
  if(document.getElementById('mOT').value==='LIMIT'&&isNaN(parseFloat(document.getElementById('mLimit').value))){{mErr('Limit orders need a limit price.');document.getElementById('mLimit').focus();return;}}
  mErr('');
  var payload={{
    symbol:mSymbol, side:mSideVal, trigger_type:tt,
    trigger_value: tt==='PRICE'?parseFloat(document.getElementById('mTV').value):null,
    trigger_percent: tt==='PERCENT'?parseFloat(document.getElementById('mTV').value):null,
    reference_price:mCmpVal,
    quantity_type:document.getElementById('mQT').value, quantity_value:parseFloat(document.getElementById('mQV').value),
    product_type:document.getElementById('mProd').value, order_type:document.getElementById('mOT').value,
    limit_price: document.getElementById('mOT').value==='LIMIT'?parseFloat(document.getElementById('mLimit').value):null,
    require_confirmation: document.getElementById('mConfirm').checked,
  }};
  var b=brkVals();
  if(b){{
    payload.bracket_target_pct=b.t1; payload.bracket_target2_pct=b.t2;
    payload.bracket_stop_pct=b.sl;   payload.bracket_auto_exit=b.auto;
    payload.bracket_target_split=b.split;
    if(b.trail&&b.trailV){{
      payload.trail_enabled=true; payload.trail_type='PERCENT';
      payload.trail_value=b.trailV; payload.trail_jump=b.trailJ;
    }}
  }}
  var res=await afetch('/api/orders',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}});
  if(!res.ok){{var e=await res.json().catch(()=>({{}}));mErr('Not saved: '+(e.error||res.statusText));return;}}
  uiSay('Rule saved for '+mSymbol+' ('+((document.getElementById('mEnv')||{{}}).textContent||'PAPER')+').');
  document.getElementById('mPreviewTxt').textContent='✓ Rule saved — it will be monitored while the dashboard is running, and flagged here for your confirmation when the target is hit.';
  document.getElementById('mTV').value=''; document.getElementById('mQV').value='';
  loadMiniRules();
}}
async function loadMiniRules(){{
  var res=await fetch('/api/orders?symbol='+encodeURIComponent(mSymbol)+'&status=ACTIVE');
  if(!res.ok)return;
  var rules=await res.json();
  var ul=document.getElementById('mRulesList'); ul.innerHTML='';
  if(!rules.length){{ul.innerHTML='<li style="justify-content:center;color:#94a3b8">No active rules</li>';return;}}
  rules.forEach(r=>{{
    var li=document.createElement('li');
    var role=r.role&&r.role!=='ENTRY'?' <b style="color:'+(r.role==='STOP'?'#dc2626':'#059669')+'">['+r.role+']</b>':'';
    var dir=r.trigger_direction==='BELOW'?'≤':'≥';
    li.innerHTML='<span>'+r.side+role+' '+r.quantity_value+(r.quantity_type==='SHARES'?' sh':r.quantity_type==='RISK'?'% risk':' ₹')+' '+dir+' ₹'+r.resolved_trigger_price.toFixed(2)+(r.require_confirmation?'':' ⚡')+'</span><button onclick="deleteRule(\\''+r.id+'\\')" style="background:none;border:none;color:#dc2626;cursor:pointer">✕</button>';
    ul.appendChild(li);
  }});
}}
async function deleteRule(id){{await afetch('/api/orders/'+id,{{method:'DELETE'}});loadMiniRules();}}

// ── Pending confirmations + broker status (polled every 5s) ────────────
function esc(x){{return String(x==null?'':x).replace(/[&<>"']/g,function(c){{return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c];}});}}
async function pollPending(){{
  try{{
    var res=await fetch('/api/orders/pending');
    if(!res.ok)return;
    var rules=await res.json();
    var box=document.getElementById('pendBox'), list=document.getElementById('pendList');
    if(!rules.length){{box.classList.remove('show');return;}}
    box.classList.add('show'); list.innerHTML='';
    window._pendRules={{}}; rules.forEach(r=>{{window._pendRules[r.id]=r;}});
    rules.forEach(r=>{{
      var d=document.createElement('div'); d.className='pend-item';
      var _q=r.quantity_type==='SHARES'?(r.quantity_value+' sh'):(r.est_qty?('~'+r.est_qty+' sh'):(r.quantity_type==='SESSION'?'session cash':r.quantity_value));
      d.innerHTML='<span>'+esc(r.symbol)+' '+esc(r.side)+' '+esc(_q)+' hit '+inrf(r.trigger_hit_price||r.resolved_trigger_price)+(r.resolved_stoploss_price?' · stop '+inrf(r.resolved_stoploss_price):'')+'</span><span><button class="pconf" onclick="confirmRule(\\''+r.id+'\\')">Confirm</button><button class="prej" onclick="rejectRule(\\''+r.id+'\\')">Reject</button></span>';
      list.appendChild(d);
    }});
  }}catch(e){{}}
}}
async function confirmRule(id,force){{
  // Safety UX (3 Nov 2026): the order, its value and its stop-loss are shown and confirmed before anything is sent
  var pr=(window._pendRules||{{}})[id];
  if(!pr){{ try{{ var _r=await fetch('/api/orders/pending'); var _l=_r.ok?await _r.json():[]; pr=_l.find(x=>x.id===id); }}catch(e){{}} }}
  pr=pr||{{id:id}};
  if(!force){{ if(!(window.AX&&AX.confirmOrder)) {{ uiSay('Not sent: the confirmation dialog did not load. Reload the page.',true); return; }}
    if(!(await AX.confirmOrder(pr,{{action:'send'}}))) return; }}
  var res=await afetch('/api/orders/'+id+'/confirm'+(force?'?force=true':''),{{method:'POST'}});
  if(res.status===409){{
    // Price ran away between the trigger and your tap: the shell's dialog shows the trigger, the price now and the
    // move in ₹ and %, and placing it anyway is an explicit second choice (UX audit H7), never a silent fill.
    var e=await res.json().catch(()=>({{}}));
    if(await uiPriceMoved(e,(window._pendRules||{{}})[id]||{{}})){{
      var r2=await afetch('/api/orders/'+id+'/confirm?force=true',{{method:'POST'}});
      if(r2.ok) uiSay('Order placed at the market price.'); else {{var e3=await r2.json().catch(()=>({{}}));uiSay('Not placed: '+(e3.error||r2.statusText),true);}}
    }}
  }} else if(!res.ok){{
    var e2=await res.json().catch(()=>({{}}));
    uiSay('Confirm failed: '+(e2.error||res.statusText),true);
  }} else {{
    var out=await res.json().catch(()=>({{}}));
    var legs=(out.result&&out.result.bracket_legs)||[];
    uiSay('Order placed.'+(legs.length?' Protective legs: '+legs.map(l=>l.role+' at '+inrf(l.trigger)+' × '+l.qty).join(', ')+'.':''));
  }}
  pollPending(); loadMiniRules();
}}
async function rejectRule(id){{await afetch('/api/orders/'+id+'/reject',{{method:'POST'}});pollPending();}}
setInterval(pollPending,5000); pollPending();
(async function(){{
  try{{
    var res=await fetch('/api/orders/broker-status'); var s=await res.json();
    var msg, cls;
    if(!s.credentials_configured){{
      msg='🟡 Dhan credentials are not set up (Settings › Credentials), so Confirm will fail until they are.'; cls='banner dry';
    }} else if(s.is_live){{
      msg='🔴 LIVE — tapping Confirm on a pending rule places a REAL order with Dhan. Real money.'; cls='banner live';
    }} else {{
      // The bug this replaces: this used to say "REAL order" here too, purely
      // because Dhan credentials existed -- regardless of broker_env. Every
      // order actually fills in the paper simulator at a live price and NEVER
      // reaches Dhan while broker_env is not LIVE.
      msg='🟢 '+s.broker_env+' — orders are simulated at live prices and tracked here; none reaches Dhan. Real orders need the LIVE switch.'; cls='banner dry';
    }}
    var b=document.getElementById('brokerBanner'); if(b){{b.textContent=msg;b.className=cls;}}
    var b2=document.getElementById('brokerBanner2'); if(b2){{b2.textContent=msg;b2.className=cls;}}
    var bi=document.getElementById('brokerItem'); if(bi){{bi.setAttribute('data-sev',s.is_live?'3':(!s.credentials_configured?'1':'0')); ssLayout();}}
  }}catch(e){{}}
}})();

// ── Live CMP feed (updates CMP cells across all tabs, no full reload) ──
async function pollLiveQuotes(){{
  try{{
    var res=await fetch('/api/live-quotes');
    if(!res.ok)return;
    var at=res.headers.get('X-ATIP-Quotes-At')||'';
    var quotes=await res.json();
    document.querySelectorAll('[data-sym]').forEach(el=>{{
      var sym=el.dataset.sym, qd=quotes[sym];
      if(!qd || !qd.ltp) return;
      var cell = el.classList.contains('cmpcell') ? el : el.querySelector('.cmpcell');
      if(!cell) return;
      // Against the PREVIOUS session's close, which is what a change figure
      // means on any quote screen. Measuring against the displayed session's own
      // close reported the move since THAT session: -11.2% for NIACL while
      // scores were a day behind, and 0.00% once they were current.
      //
      // Prefer the broker's own prev_close over data-prev. data-prev is the
      // close before the SCORED date, and scores only land at 16:45 -- so all
      // through a session it is one close too old: on Monday it would have
      // shown ATGL's Friday move (+12.56%) as today's change.
      var prev=parseFloat(cell.dataset.prev);
      if(qd.prev_close!=null && qd.prev_close>0) prev=qd.prev_close;
      var pct = (!isNaN(prev) && prev>0) ? ((qd.ltp-prev)/prev*100) : null;
      var flat = pct!==null && Math.abs(pct)<0.005;   // UI fixes v2 (G1): unchanged is grey, never green
      var col = pct===null ? '#e2e8f0' : flat ? '#94a3b8' : (pct>0 ? '#34d399' : '#f87171');
      var sign = (pct!==null && pct>0) ? '+' : '';
      var arrow = pct===null ? '' : flat ? '• ' : (pct>0 ? '▲ ' : '▼ ');
      var was=parseFloat(cell.dataset.cmp);   // live-data UX (3 Nov 2026): a changed price flashes once, ▲/▼ in the text
      cell.innerHTML = inrf(qd.ltp) + (pct!==null
        ? ' <span class="chg" style="font-size:12px;color:'+col+'">'+arrow+sign+(qd.ltp-prev).toFixed(2)+' ('+sign+pct.toFixed(2)+'%)</span>'
        : '');
      cell.dataset.cmp = qd.ltp;
      if(!isNaN(was) && was!==qd.ltp && window.AX && AX.flash) AX.flash(cell, qd.ltp>was?1:-1);
      if(at) cell.title='price at '+at+' IST';
    }});
    if(Object.keys(quotes).length && window.AX && AX.stamp) AX.stamp(at ? String(at).slice(-8) : null);
  }}catch(e){{}}
}}
setInterval(pollLiveQuotes,15000); pollLiveQuotes();
// ── the 13 tables in four groups (UX audit H4); tab ids, labels and order are unchanged ──
var TG={{scores:['scores','port'],lists:['vpi','zpi','rri','mri','cri'],flows:['fiidii','news','asia'],activity:['hist','orders','livefeeds']}};
function tabId(t){{var m=/showTab\\('(\\w+)'/.exec(t.getAttribute('onclick')||'');return m?m[1]:'';}}
function groupOf(id){{for(var g in TG){{if(TG[g].indexOf(id)>=0)return g;}}return 'lists';}}
function showGroup(g,pick){{
  document.querySelectorAll('.tgroups button').forEach(function(b){{b.setAttribute('aria-pressed',b.dataset.g===g?'true':'false');}});
  var first=null, act=null;
  document.querySelectorAll('.tabs .tab').forEach(function(t){{var on=groupOf(tabId(t))===g; t.setAttribute('data-hide',on?'0':'1'); if(on&&!first)first=t; if(on&&t.classList.contains('active'))act=t;}});
  if(pick&&!act&&first) first.click();
}}
function tabsA11y(){{
  document.querySelectorAll('.tabs .tab').forEach(function(t){{
    if(t.dataset.ax) return; t.dataset.ax='1';
    t.setAttribute('role','tab'); t.tabIndex=0; t.setAttribute('aria-selected',t.classList.contains('active')?'true':'false');
    t.addEventListener('keydown',function(e){{if(e.key==='Enter'||e.key===' '){{e.preventDefault();t.click();}}}});
    var id=tabId(t), pane=document.getElementById(id);
    if(pane){{pane.setAttribute('role','tabpanel'); pane.tabIndex=0; pane.setAttribute('aria-label',(t.textContent||id).trim());}}
  }});
}}
var _showTab0=showTab;
showTab=function(id,el){{_showTab0(id,el); showGroup(groupOf(id),false);
  document.querySelectorAll('.tabs .tab').forEach(function(t){{t.setAttribute('aria-selected',t===el?'true':'false');}});}};
document.querySelectorAll('.tgroups button').forEach(function(b){{b.addEventListener('click',function(){{showGroup(b.dataset.g,true);}});}});
function uiInit(){{tabsA11y(); var a=document.querySelector('.tabs .tab.active'); showGroup(groupOf(a?tabId(a):'scores'),false);
  var tb=document.querySelector('.tabs'); if(tb) tb.setAttribute('data-ready','1'); ssLayout();}}
uiInit(); window.addEventListener('load',uiInit); setInterval(ssLayout,3000);
</script>{_stock_assets}{_w28_assets}{_w28b_assets}</body></html>"""
    # Catch a broken inline script before it silently disables every tab.
    _check_js(html)
    return html

if HAS_FASTAPI:
    from dashboard.security import dashboard_root_path as _root_path
    # root_path lets a reverse proxy serve the dashboard at a subpath such as
    # https://dev.bkesari.com/ATIP/ (ATIP_ROOT_PATH=/ATIP). Empty by default, so a
    # plain local run at http://localhost:8000/ is unchanged.
    app=FastAPI(title="ATIP Dashboard",version="0.2",root_path=_root_path())
    # W7: the enterprise authorization middleware (a pass-through unless
    # config.json enterprise.enabled is true -- see enterprise/authz.py)
    from enterprise.authz import install as _install_enterprise_authz
    _install_enterprise_authz(app)
    # Task 2: read-only while ATIP is STOPPED (21:00-06:00 IST or a manual stop) -- writes get 423
    from pipeline.operating_schedule import install_readonly_guard as _install_readonly_guard
    _install_readonly_guard(app)
    # A VIEWER (a web server showing the Mac's data, ops/role.py) refuses every write -- 403, outermost
    from ops.role import install_guard as _install_viewer_guard
    _install_viewer_guard(app)
    from fastapi import Depends, HTTPException, Request as _Req
    from dashboard.security import TOKEN_HEADER, guard_ok

    async def _authorised(request: _Req):
        """Guards every route that can change something. See dashboard/security.py."""
        if not guard_ok(request.headers.get(TOKEN_HEADER)):
            raise HTTPException(status_code=401,
                                detail=f"{TOKEN_HEADER} missing or wrong — this route can "
                                       f"place, confirm or cancel orders")
    _guard = [Depends(_authorised)]
    @app.get("/",response_class=HTMLResponse)
    async def dashboard():
        return HTMLResponse(content=build_html(generate_state(latest_scored_date())))
    @app.get("/api/scores")
    async def api_scores(): return JSONResponse(json_safe(get_scores(latest_scored_date())))
    @app.get("/api/mh")
    async def api_mh(): return JSONResponse(json_safe(get_mh(latest_scored_date())))
    @app.get("/api/tod")
    async def api_tod(): return JSONResponse(json_safe(get_tod(latest_scored_date())))
    @app.get("/api/alerts")
    async def api_alerts():
        try:
            from pipeline.recover import today as _recovered_today
            reran=_recovered_today()
        except Exception:
            reran=[]
        return JSONResponse(json_safe({"job_health":get_job_health(),"recovered_today":reran,
                                       "alerts":get_alerts(hours=72,limit=100)}))
    @app.post("/api/health/recover", dependencies=_guard)
    async def api_health_recover():
        """Re-run every missed / failed job now (pipeline/recover.py) and report what is still open."""
        from starlette.concurrency import run_in_threadpool
        from pipeline.recover import check_and_recover
        out=await run_in_threadpool(check_and_recover)
        return JSONResponse(json_safe({"ran":[{"step":r["step"],"for":r["labels"],"result":r["result"]} for r in out["ran"]],
                                       "skipped":[{"step":s["step"],"reason":s["reason"]} for s in out["skipped"]],
                                       "recovered":out["recovered"],
                                       "still_open":[{"label":p.label,"kind":p.kind,"detail":p.detail}
                                                     for p in out["problems"]]}))
    @app.get("/api/pnl")
    async def api_pnl():
        """PF-02 / PF-13: both books valued now (no broker call), today's risk
        state, and the last 60 pnl_daily rows."""
        from portfolio.pnl import portfolio_summary, risk_state, ENVS
        conn=get_connection()
        try:
            out={"books":{e:portfolio_summary(conn,e) for e in ENVS},
                 "risk":{e:risk_state(conn,e) for e in ENVS},
                 "daily":q(conn,"SELECT * FROM pnl_daily ORDER BY date DESC, env LIMIT 60"),
                 "last_sync":q(conn,"SELECT date,source,status,n_holdings,error,synced_at FROM portfolio_sync "
                                    "ORDER BY id DESC LIMIT 5")}
        finally:
            conn.close()
        return JSONResponse(json_safe(out))
    @app.get("/api/data-quality")
    async def api_data_quality():
        """DP-19: the latest session's checks and DQS, plus the DQS history."""
        conn=get_connection()
        try:
            last=q1(conn,"SELECT MAX(date) d FROM data_quality") or {}
            out={"date":last.get("d"),
                 "checks":q(conn,"SELECT check_name,severity,failed,checked,score,detail FROM data_quality "
                                 "WHERE date=? ORDER BY check_name",last.get("d")) if last.get("d") else [],
                 "dqs_history":q(conn,"SELECT date,severity,score FROM data_quality WHERE check_name='DQS' "
                                      "ORDER BY date DESC LIMIT 30")}
        finally:
            conn.close()
        return JSONResponse(json_safe(out))
    @app.get("/api/news")
    async def api_news(): return JSONResponse(json_safe(get_news()))
    @app.get("/api/portfolio")
    async def api_portfolio(): return JSONResponse(json_safe(get_portfolio(latest_scored_date())))
    @app.post("/api/refresh", dependencies=_guard)
    async def api_refresh():
        import threading
        from pipeline.scheduler import run_postmarket
        threading.Thread(target=run_postmarket,daemon=True).start()
        return JSONResponse({"status":"pipeline started"})

    # ── Backtests (W2) ──────────────────────────────────────────────────
    # Create/run validate the request synchronously (a bad request is a 400,
    # not a FAILED run), then execute in a background thread; poll the run.
    from backtest import service as bt_service, store as bt_store

    def _bt_rows(kind, run_id):
        conn=get_connection()
        try:
            if not bt_store.get_run(conn, run_id):
                return None
            return bt_store.get_rows(conn, kind, run_id)
        finally:
            conn.close()

    @app.get("/api/backtests")
    async def api_backtests(limit: int = 50, strategy_id: str = None):
        conn=get_connection()
        try: return JSONResponse(json_safe(bt_store.list_runs(conn, limit, strategy_id)))
        finally: conn.close()

    @app.get("/api/backtests/strategies")
    async def api_backtest_strategies():
        from backtest.strategies import REGISTRY
        from backtest.config import backtest_config
        return JSONResponse(json_safe({"strategies":{k:c().describe() for k,c in REGISTRY.items()},
                                       "defaults":backtest_config()}))

    @app.post("/api/backtests", dependencies=_guard)
    async def api_backtest_create(request: _Req):
        """Body: a backtest request (backtest/service.py). Returns run_id; runs in the background."""
        import threading
        payload = await request.json()
        try:
            run_id = bt_service.create(payload)
        except (ValueError, KeyError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        threading.Thread(target=bt_service.execute, args=(run_id,), daemon=True).start()
        return JSONResponse({"run_id": run_id, "status": "RUNNING"})

    @app.post("/api/backtests/walkforward", dependencies=_guard)
    async def api_backtest_walkforward(request: _Req):
        """Body: {request:{...start,end...}, train, validation, test, step, candidates?, select_by?,
        purge?, embargo?} -- purge / embargo (BT-18) are sessions, default 0 (no gap between windows)."""
        import threading
        from backtest.walkforward import run_walk_forward, build_windows, trading_sessions
        b = await request.json()
        try:
            req = b["request"]
            purge, embargo = int(b.get("purge") or 0), int(b.get("embargo") or 0)
            bt_service.resolve_config(req)
            build_windows(trading_sessions(req["start"], req["end"]), int(b["train"]), int(b["validation"]),
                          int(b["test"]), int(b["step"]), purge, embargo)
        except (ValueError, KeyError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        threading.Thread(target=run_walk_forward, daemon=True,
                         args=(req, int(b["train"]), int(b["validation"]), int(b["test"]), int(b["step"]),
                               b.get("candidates"), b.get("select_by", "sharpe"), purge, embargo)).start()
        return JSONResponse({"status": "RUNNING", "note": "the parent run appears in /api/backtests (kind walk_forward)"})

    # ── W23: optimisation, sensitivity, robustness, research study ─────────
    # Each validates synchronously (400 on a bad request), then runs in a thread;
    # the parent run appears in /api/backtests (kind optimization / sensitivity /
    # robustness) and its summary holds the result. A study is found under
    # /api/research/studies.
    def _bg(fn, *args):
        import threading
        threading.Thread(target=fn, args=args, daemon=True).start()

    @app.post("/api/backtests/optimize", dependencies=_guard)
    async def api_backtest_optimize(request: _Req):
        """Body: {request, space, method? grid|random|adaptive|bayes, select_by?, max_trials?, seed?, min_trades?}."""
        from backtest.optimize import optimize, prepare
        b = await request.json()
        try:
            prepare(b["request"], b["space"], b.get("method", "grid"), int(b.get("max_trials", 60)))
        except (ValueError, KeyError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        _bg(optimize, b["request"], b["space"], b.get("method", "grid"), b.get("select_by", "sharpe"),
            int(b.get("max_trials", 60)), int(b.get("seed", 42)), int(b.get("min_trades", 10)))
        return JSONResponse({"status": "RUNNING", "note": "the parent run appears in /api/backtests (kind optimization)"})

    @app.post("/api/backtests/sensitivity", dependencies=_guard)
    async def api_backtest_sensitivity(request: _Req):
        """Body: {request, space, select_by?, steps?, pairwise?}."""
        from backtest.optimize import validate_space
        from backtest.sensitivity import sensitivity
        b = await request.json()
        try:
            validate_space(b["space"]); bt_service.resolve_config(b["request"])
            if b["request"].get("period_label") == "test" or b["request"].get("allow_test"):
                raise ValueError("sensitivity analysis on the test window is refused")
        except (ValueError, KeyError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        _bg(sensitivity, b["request"], b["space"], b.get("select_by", "sharpe"), int(b.get("steps", 2)), 10,
            b.get("pairwise"))
        return JSONResponse({"status": "RUNNING", "note": "the parent run appears in /api/backtests (kind sensitivity)"})

    @app.post("/api/backtests/robustness", dependencies=_guard)
    async def api_backtest_robustness(request: _Req):
        """Body: {request (start/end), n_subsamples?, seed?}."""
        from backtest.robustness import robustness
        b = await request.json()
        try:
            snap = bt_service.resolve_config(b["request"])
            if snap.get("periods") or snap["period_label"] == "test":
                raise ValueError("give start / end (not periods); the test window is refused")
        except (ValueError, KeyError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        _bg(robustness, b["request"], int(b.get("n_subsamples", 3)), int(b.get("seed", 42)))
        return JSONResponse({"status": "RUNNING", "note": "the parent run appears in /api/backtests (kind robustness)"})

    @app.post("/api/backtests/costsweep", dependencies=_guard)
    async def api_backtest_costsweep(request: _Req):
        """Body: {request, multipliers? (default [0,0.5,1,1.5,2,3,5]), scale? costs|slippage|both} (BT-19)."""
        from backtest.robustness import cost_sweep, prepare_cost_sweep
        b = await request.json()
        try:
            mults = prepare_cost_sweep(b["request"], b.get("multipliers"), b.get("scale", "costs"))
        except (ValueError, KeyError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        _bg(cost_sweep, b["request"], mults, b.get("scale", "costs"))
        return JSONResponse({"status": "RUNNING", "note": "the parent run appears in /api/backtests (kind cost_sweep)"})

    @app.post("/api/backtests/cpcv", dependencies=_guard)
    async def api_backtest_cpcv(request: _Req):
        """Body: {request (start/end; + event_driven settings), groups? (6), test_groups? (2), candidates?, select_by?,
        purge?, embargo?, engine? w2|event_driven} -- combinatorially purged CV (backtest/cpcv.py): the distribution of
        k/N x C(N, k) out-of-sample paths."""
        from backtest.cpcv import prepare_cpcv, run_cpcv
        b = await request.json()
        try:
            args = (b["request"], int(b.get("groups", 6)), int(b.get("test_groups", 2)), b.get("candidates"),
                    b.get("select_by", "sharpe"), int(b.get("purge") or 0), int(b.get("embargo") or 0),
                    b.get("engine") or "w2")
            prepare_cpcv(*args)
        except (ValueError, KeyError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        _bg(run_cpcv, *args)
        return JSONResponse({"status": "RUNNING", "note": "the parent run appears in /api/backtests (kind cpcv)"})

    @app.post("/api/backtests/{run_id}/pbo", dependencies=_guard)
    async def api_backtest_pbo(run_id: str, request: _Req):
        """Body (optional): {partitions? (16), metric? sharpe|mean|total_return} -- the probability of backtest
        overfitting (CSCV) of the run's trials (an optimisation), computed now and stored as a run of kind pbo."""
        from backtest.cpcv import pbo_for_run
        try:
            b = await request.json()
        except Exception:
            b = {}
        try:
            out = pbo_for_run(run_id, int((b or {}).get("partitions", 16)), (b or {}).get("metric", "sharpe"))
        except (ValueError, TypeError, AttributeError) as e:
            return JSONResponse({"error": str(e)}, status_code=404 if str(e).startswith("no backtest run") else 400)
        return JSONResponse(json_safe(out))

    @app.post("/api/backtests/study", dependencies=_guard)
    async def api_backtest_study(request: _Req):
        """Body: {strategy_id, start, end, space?, hypothesis?, method?, max_trials?, universe?}."""
        from backtest.study import PRESETS, run_study, split_periods
        b = await request.json()
        try:
            if not b.get("space") and b.get("strategy_id") not in PRESETS:
                raise ValueError(f"space is required (presets exist for {sorted(PRESETS)})")
            split_periods(b["start"], b["end"])
            bt_service.resolve_config({"strategy_id": b["strategy_id"], "start": b["start"], "end": b["end"]})
        except (ValueError, KeyError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        _bg(lambda: run_study(b["strategy_id"], b.get("space"), b["start"], b["end"], b.get("hypothesis"),
                              b.get("method", "adaptive"), int(b.get("max_trials", 30)), universe=b.get("universe")))
        return JSONResponse({"status": "RUNNING", "note": "the study appears in /api/research/studies"})

    @app.get("/api/backtests/{run_id}")
    async def api_backtest_get(run_id: str):
        conn=get_connection()
        try:
            run = bt_store.get_run(conn, run_id)
            if not run:
                return JSONResponse({"error": "not found"}, status_code=404)
            if run["kind"] in ("walk_forward", "cpcv"):
                run["children"] = bt_store.child_runs(conn, run_id)
            return JSONResponse(json_safe(run))
        finally: conn.close()

    @app.get("/api/backtests/{run_id}/trades")
    async def api_backtest_trades(run_id: str):
        rows = _bt_rows("backtest_trade", run_id)
        return JSONResponse(json_safe(rows)) if rows is not None else JSONResponse({"error": "not found"}, status_code=404)

    @app.get("/api/backtests/{run_id}/equity")
    async def api_backtest_equity(run_id: str):
        rows = _bt_rows("backtest_equity", run_id)
        return JSONResponse(json_safe(rows)) if rows is not None else JSONResponse({"error": "not found"}, status_code=404)

    @app.get("/api/backtests/{run_id}/drawdowns")
    async def api_backtest_drawdowns(run_id: str):
        rows = _bt_rows("backtest_drawdown", run_id)
        return JSONResponse(json_safe(rows)) if rows is not None else JSONResponse({"error": "not found"}, status_code=404)

    @app.get("/api/backtests/{run_id}/metrics")
    async def api_backtest_metrics(run_id: str):
        conn=get_connection()
        try:
            run = bt_store.get_run(conn, run_id)
            if not run:
                return JSONResponse({"error": "not found"}, status_code=404)
            return JSONResponse(json_safe({"run_id": run_id, "status": run["status"], "metrics": run.get("metrics"),
                                           "bias_report": run.get("bias_report")}))
        finally: conn.close()

    @app.get("/api/backtests/{run_id}/montecarlo")
    async def api_backtest_mc_list(run_id: str):
        conn=get_connection()
        try: return JSONResponse(json_safe(bt_store.get_montecarlo(conn, run_id)))
        finally: conn.close()

    @app.post("/api/backtests/{run_id}/montecarlo", dependencies=_guard)
    async def api_backtest_mc_run(run_id: str, request: _Req):
        """Body: {method: trade_shuffle|return_bootstrap, n_sims, seed, block_size}."""
        b = await request.json()
        try:
            return JSONResponse(json_safe(bt_service.run_montecarlo(
                run_id, b.get("method", "trade_shuffle"), int(b.get("n_sims", 1000)), int(b.get("seed", 42)),
                int(b.get("block_size", 1)))))
        except (ValueError, KeyError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.get("/backtests", response_class=HTMLResponse)
    async def backtests_page():
        from dashboard.backtest_page import render
        from dashboard.security import page_token as _tok
        return HTMLResponse(render(_tok()))

    # ── Strategy Engine (W3) ─────────────────────────────────────────────
    from dashboard.strategy_routes import register as _register_strategy_routes
    _register_strategy_routes(app, _guard, _Req, get_connection, json_safe)

    # ── Risk engine & execution (W4) ─────────────────────────────────────
    from dashboard.execution_routes import register as _register_execution_routes
    _register_execution_routes(app, _guard, _Req, get_connection, json_safe)
    from dashboard.portfolio_risk_routes import register as _register_portfolio_risk     # W25
    _register_portfolio_risk(app, _guard, _Req, get_connection, json_safe)
    from dashboard.risk_model_routes import register as _register_risk_model            # W40 factor risk model
    _register_risk_model(app, _guard, _Req, get_connection, json_safe)
    from dashboard.stock_view import register as _register_stock_view                   # W26
    _register_stock_view(app, get_connection, json_safe)
    from dashboard.market_routes import register as _register_market_routes             # W27
    _register_market_routes(app, _guard, _Req, get_connection, json_safe)
    from dashboard.w28_routes import register as _register_w28_routes                   # W28
    _register_w28_routes(app, _guard, _Req, get_connection, json_safe)
    from dashboard.w28b_routes import register as _register_w28b_routes                 # W28b
    _register_w28b_routes(app, _guard, _Req, get_connection, json_safe)
    from dashboard.w29_routes import register as _register_w29_routes                   # W29
    _register_w29_routes(app, _guard, _Req, get_connection, json_safe)
    from dashboard.w34_routes import register as _register_w34_routes                   # W34
    _register_w34_routes(app, _guard, _Req, get_connection, json_safe)
    from dashboard.w35_routes import register as _register_w35_routes                   # W35
    _register_w35_routes(app, _guard, _Req, get_connection, json_safe)
    from dashboard.w36_routes import register as _register_w36_routes                   # W36
    _register_w36_routes(app, _guard, _Req, get_connection, json_safe)
    from dashboard.w37_routes import register as _register_w37_routes                   # W37
    _register_w37_routes(app, _guard, _Req, get_connection, json_safe)
    from dashboard.w38_routes import register as _register_w38_routes                   # W38
    _register_w38_routes(app, _guard, _Req, get_connection, json_safe)
    from dashboard.w39_routes import register as _register_w39_routes                   # W39
    _register_w39_routes(app, _guard, _Req, get_connection, json_safe)
    from dashboard.w39_retail_routes import register as _register_w39b_routes           # W39b: baskets, SIP
    _register_w39b_routes(app, _guard, _Req, get_connection, json_safe)
    from dashboard.w40_routes import register as _register_w40_routes                   # W40: meta-labelling
    _register_w40_routes(app, _guard, _Req, get_connection, json_safe)
    from dashboard.command_routes import register as _register_command_routes          # UXR-01: Command Center
    _register_command_routes(app, _guard, _Req, get_connection, json_safe)
    from dashboard.command_more import register as _register_command_more             # UXR-06..09: console screens
    _register_command_more(app, _guard, _Req, get_connection, json_safe)
    from dashboard.ux_routes import register as _register_ux_routes                    # UXD/SES/ACC/RPT (2 Nov 2026): the shell
    _register_ux_routes(app, _guard, _Req, get_connection, json_safe)                  # on every page, new pages, /api/v2
    from dashboard.global_asia import register as _register_global_asia                # Task 2/3: status + Asia
    _register_global_asia(app, _guard, _Req, get_connection, json_safe)
    from dashboard.news_impact_view import register as _register_news_impact            # NI-05: news impact
    _register_news_impact(app, _guard, get_connection, json_safe)

    # ── AI / ML (W5) ─────────────────────────────────────────────────────
    from dashboard.ml_routes import register as _register_ml_routes
    _register_ml_routes(app, _guard, _Req, get_connection, json_safe)
    from dashboard.ml_w24_routes import register as _register_ml_w24_routes     # W24
    _register_ml_w24_routes(app, _guard, _Req, get_connection, json_safe)

    # ── Advanced quant (W6) ──────────────────────────────────────────────
    from dashboard.quant_routes import register as _register_quant_routes
    _register_quant_routes(app, _guard, _Req, get_connection, json_safe)

    # ── Research platform: factor approval, studies, formulas (W22) ───────
    from dashboard.research_routes import register as _register_research_routes
    _register_research_routes(app, _guard, _Req, get_connection, json_safe)

    # ── Wealth track: investor DNA, wealth, goals, allocation ... (W11-W17)
    from dashboard.wealth_routes import register as _register_wealth_routes
    _register_wealth_routes(app, _guard, _Req, get_connection, json_safe)

    # ── Enterprise: users, tenants, RBAC, account, admin (W7) ─────────────
    from dashboard.enterprise_routes import register as _register_enterprise_routes
    _register_enterprise_routes(app, _guard, _Req, get_connection, json_safe)
    from dashboard.saas_routes import register as _register_saas_routes                 # W9 SaaS, wired in W32
    _register_saas_routes(app, _guard, _Req, get_connection, json_safe)

    # ── Operations (W8): health, metrics, backups, monitoring, webhooks ─
    from dashboard.ops_routes import register as _register_ops_routes
    _register_ops_routes(app, _guard, _Req, get_connection, json_safe)
    # outermost middleware (request ids, /api/v1, size limits, JSON validation, rate
    # limit, idempotency, security headers, metrics) + the standard error envelope
    from ops.http import install as _install_ops_http
    _install_ops_http(app)

    # ── Buy/Sell target + stoploss rules ────────────────────────────────
    # See orders/rules.py docstring for why triggering (automatic)
    # and execution (confirmation-gated by default) are kept separate.
    from orders import rules as oe
    from fastapi import Request

    oe.init_orders_table()

    @app.post("/api/orders", dependencies=_guard)
    async def api_create_order(request: Request):
        payload = await request.json()
        role = (payload.get("role") or oe.ROLE_ENTRY) if isinstance(payload, dict) else oe.ROLE_ENTRY
        if role != oe.ROLE_ENTRY:
            # Review 3 Nov 2026 (orders M5): exit legs skip the LIVE confirmation tap by design, so a rule sent from
            # outside may only be an ENTRY. STOP / TARGET legs are built by ATIP itself from a filled entry
            # (orders/rules._create_bracket_legs), never posted.
            return JSONResponse({"error": f"role {role} is a bracket leg ATIP builds from a filled entry; "
                                          f"post an ENTRY with bracket_stop_pct / bracket_target_pct instead"},
                                status_code=400)
        try:
            return JSONResponse(json_safe(oe.create_rule(payload)))
        except (ValueError, KeyError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.get("/api/orders")
    async def api_list_orders(symbol: str = None, status: str = None):
        return JSONResponse(json_safe(oe.list_rules(symbol=symbol, status=status)))

    @app.get("/api/orders/pending")
    async def api_pending_orders():
        # Review 3 Nov 2026 (dashboard H1): the confirm dialog shows shares and value, not a raw quantity_value (a pool
        # buy's "100" is 100 % of the session's cash). Sized read-only, the way execute_rule will size it.
        return JSONResponse(json_safe(oe.with_estimates(oe.list_rules(status=oe.PENDING_CONFIRMATION))))

    @app.post("/api/orders/{rule_id}/confirm", dependencies=_guard)
    async def api_confirm_order(rule_id: str, force: bool = False):
        # Routed through confirm_rule() so the price is re-validated: the
        # confirmation window is 30 minutes now, so a tap can land well after
        # the trigger and a market order would fill wherever price has got to.
        # A refusal comes back as 409 with both prices; the UI then offers an
        # explicit "place anyway".
        result = oe.confirm_rule(rule_id, force=force)
        if result.get("status") == "REFUSED_SLIPPAGE":
            return JSONResponse({"error": result["error"], "slippage": True,
                                 "trigger_price": result.get("trigger_price"),
                                 "current_price": result.get("current_price"),
                                 "drift_pct": result.get("drift_pct")}, status_code=409)
        if result.get("status") == "FAILED" and "not found" in str(result.get("error", "")):
            return JSONResponse({"error": result["error"]}, status_code=404)
        return JSONResponse(json_safe({"rule": oe.get_rule(rule_id), "result": result}))

    @app.post("/api/orders/{rule_id}/reject", dependencies=_guard)
    async def api_reject_order(rule_id: str):
        rule = oe.reject_rule(rule_id)
        if not rule:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(json_safe(rule))

    @app.delete("/api/orders/{rule_id}", dependencies=_guard)
    async def api_delete_order(rule_id: str):
        ok = oe.delete_rule(rule_id)
        if not ok:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"deleted": True})

    @app.get("/api/orders/broker-capability")
    async def api_broker_capability():
        """What the installed Dhan SDK offers for broker-side stops (orders.rules.broker_trailing_capability): the
        order ticket's "Check broker" button (UX audit M5). Reads the local SDK only; nothing reaches Dhan."""
        from orders.rules import broker_trailing_capability
        try:
            c = broker_trailing_capability()
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse(json_safe({k: c.get(k) for k in ("sdk_version", "supports_bracket", "supports_super_order",
                                                             "supports_gtt", "notes")}))

    @app.get("/api/orders/broker-status")
    async def api_broker_status():
        from data.dhan import load_dhan_config
        cfg = load_dhan_config()
        configured = bool(cfg.get("dhan_client_id") and cfg.get("dhan_access_token"))
        # credentials_configured alone is NOT "will this place a real order" --
        # it only means Dhan is reachable for market data. Whether a confirmed
        # rule reaches Dhan for real is broker_env, and the banner that reads
        # this was claiming "REAL order" regardless of it, even while every
        # order was actually filling in the paper simulator.
        try:
            from orders.environment import broker_env, LIVE
            env = broker_env()
        except Exception:
            env = "PAPER"
        return JSONResponse({"credentials_configured": configured,
                             "broker_env": env, "is_live": env == "LIVE"})

    # Last successful quote fetch, served while the market is shut. The page
    # polls /api/live-quotes every 15s for as long as a tab is open, and each
    # call asks Dhan for ~500 quotes: a tab left open on Friday evening kept
    # fetching 501 quotes a minute all night (atip.log, 2026-09-18 20:00 ->
    # 2026-09-19 09:15, where the quote thread finally died), on top of the
    # index WebSocket's own reconnect storm. Outside market hours there is no
    # new price to fetch -- the last one IS the close.
    # In the session too, every tab and request shares one fetch per
    # LIVE_QUOTES_TTL seconds. Each tab used to ask Dhan itself, and Chrome
    # wakes background tabs together on the minute: two or three ~500-quote
    # calls landed in the same second and Dhan (1 quote call a second) refused
    # all but one -- 639 refusals on 2026-10-30.
    _live_quotes_cache = {"date": None, "data": {}, "at": 0.0}
    _live_quotes_lock = threading.Lock()
    LIVE_QUOTES_TTL = 15

    @app.get("/api/live-quotes")
    async def api_live_quotes():
        """Live LTP for every symbol currently shown on the dashboard (scores + portfolio)."""
        from pipeline.scheduler import is_market_hours
        from ops.role import is_viewer
        from starlette.concurrency import run_in_threadpool
        if is_viewer():                  # no Dhan here: the prices are the Mac's, in the copied database
            return JSONResponse({})
        # X-ATIP-Quotes-At: when these prices were fetched (IST), so each CMP cell can say how old it is (UX audit H1)
        if not is_market_hours():
            cached = (_live_quotes_cache["data"]
                      if _live_quotes_cache["date"] == str(date.today()) else {})
            return JSONResponse(cached, headers={"X-ATIP-Quotes-At": _live_quotes_cache.get("ts") or ""} if cached else None)
        # A thread, not the event loop: a request may wait on another's fetch or its turn at Dhan
        data = await run_in_threadpool(_session_live_quotes)
        return JSONResponse(data, headers={"X-ATIP-Quotes-At": _live_quotes_cache.get("ts") or ""} if data else None)

    def _session_live_quotes() -> dict:
        """The session's quotes, fetched from Dhan at most once per LIVE_QUOTES_TTL. A request that
        arrives while another is fetching waits for that fetch and gets its result."""
        from data.dhan import fetch_live_quotes
        today = str(date.today())
        with _live_quotes_lock:
            if time.monotonic() - _live_quotes_cache["at"] < LIVE_QUOTES_TTL:
                return _live_quotes_cache["data"] if _live_quotes_cache["date"] == today else {}
            _live_quotes_cache["at"] = time.monotonic()      # a failed fetch waits out the window too
            td = latest_scored_date()
            conn = get_connection()
            try:
                syms = {r["symbol"] for r in q(conn, "SELECT DISTINCT symbol FROM ai_scores WHERE date=?", str(td))}
                syms |= {r["symbol"] for r in q(conn, "SELECT DISTINCT symbol FROM portfolio_holdings WHERE date=?", str(td))}
            finally:
                conn.close()
            if not syms:
                return {}
            try:
                df = fetch_live_quotes(list(syms))
            except Exception as e:
                log.warning(f"  live quotes fetch failed: {e}")
                return {}
            if df.empty:
                return {}
            out = _quotes_out(df)
            _live_quotes_cache.update(date=today, data=out, ts=datetime.now().strftime("%H:%M:%S"))
            try:                          # the feed label counts this fetch as a live price (UX audit H1)
                from dashboard.ux_kit import note_quotes_fetched
                note_quotes_fetched()
            except Exception:
                pass
            return out

    # 2026-10-31 (DP-01 / MON-07): every Dhan feed's state for the Live tab and the phone's Live screen, served
    # the way the quotes above are -- one read per LIVE_QUOTES_TTL, shared by every tab and request. It reads
    # live_feed_status only (ops/data_health.feeds), never Dhan, so a VIEWER shows the Mac's feed states from the
    # copied database. A feed switched off reads OFF.
    _live_feeds_cache = {"data": None, "at": 0.0}
    _live_feeds_lock = threading.Lock()

    @app.get("/api/live-feeds")
    async def api_live_feeds():
        """State of the index / stock WebSocket, tick capture, 1-minute bars, 20-level depth and option chain."""
        from starlette.concurrency import run_in_threadpool
        return JSONResponse(await run_in_threadpool(_live_feeds))

    def _live_feeds() -> dict:
        from data.dhan_ws import feed_window_open
        from ops.data_health import feeds
        with _live_feeds_lock:
            c = _live_feeds_cache
            if c["data"] is not None and time.monotonic() - c["at"] < LIVE_QUOTES_TTL:
                return c["data"]
            conn = get_connection()
            try:
                rows = feeds(conn)
            finally:
                conn.close()
            out = {"checked_at": datetime.now().isoformat(timespec="seconds"), "window_open": feed_window_open(),
                   "feeds": rows}
            c.update(data=out, at=time.monotonic())
            return out

    def _quotes_out(df) -> dict:
        import math
        def _fin(v):
            # Review 3 Nov 2026 (dashboard M4): pandas turns a missing price into NaN, NaN is truthy, and Starlette
            # refuses NaN in JSON -- one bad row made /api/live-quotes a 500 and froze every CMP cell.
            try:
                v = float(v)
            except (TypeError, ValueError):
                return None
            return v if math.isfinite(v) and v > 0 else None
        out = {}
        for _, row in df.iterrows():
            ltp = _fin(row.get("ltp"))
            prev = _fin(row.get("prev_close"))
            if ltp is None:
                continue
            chg = round((ltp - prev) / prev * 100, 2) if ltp and prev else None
            # prev_close comes from Dhan and is the LAST COMPLETED session's
            # close, which is the only correct baseline for a live price. The
            # page's own data-prev is the close before the SCORED date, so
            # between 09:15 and the 16:45 post-market run it is one session too
            # old and renders a two-session move as today's change.
            out[row["symbol"]] = {"ltp": ltp, "chg_pct": chg, "prev_close": prev}
        return out

    # RCA-13 (6 Nov 2026): a pass that lost to a database lock runs again after MONITOR_LOCK_RETRY_S, not after the
    # full MONITOR_EVERY_S. Every connection already waits up to 60 s for a writer (db.schema.BUSY_TIMEOUT_MS), so a
    # pass that still fails has lost that minute; another 30 s on top leaves exit legs unwatched for 90 s.
    MONITOR_EVERY_S = 30
    MONITOR_LOCK_RETRY_S = 5

    def _is_db_lock(e) -> bool:
        return "database is locked" in str(e) or "database table is locked" in str(e)

    def _order_monitor_pass() -> bool:
        """One pass: auto-pick, then check_triggers. True when either lost to a database lock."""
        locked = False
        try:                    # free cash-pool sessions queue their next occurrence first
            from orders.autopick import run as autopick
            autopick()
        except Exception as e:
            log.error(f"  auto-pick error: {e}")
            locked = _is_db_lock(e)
        try:
            events = oe.check_triggers()
            if events:
                log.info(f"  order_rules: {events}")
        except Exception as e:
            log.error(f"  order_rules monitor error: {e}")
            locked = locked or _is_db_lock(e)
        return locked

    def _order_monitor_loop():
        """
        Runs only while the dashboard process is up — deliberately NOT part
        of pipeline/scheduler.py (see orders/rules.py docstring for
        why). Read-only: flags PENDING_CONFIRMATION, executes only rules
        explicitly marked require_confirmation=False.
        """
        import time
        from pipeline.scheduler import is_market_hours
        from pipeline.operating_schedule import is_stopped
        while True:
            wait = MONITOR_EVERY_S
            try:
                # Task 2: nothing is triggered while ATIP is stopped (21:00-06:00 IST / manual stop)
                if is_market_hours() and not is_stopped():
                    if _order_monitor_pass():
                        wait = MONITOR_LOCK_RETRY_S
                        log.warning(f"  order monitor: pass lost to a database lock, running again in {wait}s")
            except Exception as e:
                log.error(f"  order_rules monitor error: {e}")
            time.sleep(wait)

    @app.on_event("startup")
    async def _start_order_monitor():
        import threading
        from ops.role import is_viewer
        if is_viewer():
            log.info("  Order-rules monitor not started: this copy is a VIEWER, orders run on the Mac")
            return
        threading.Thread(target=_order_monitor_loop, name="atip-order-monitor", daemon=True).start()
        log.info("  ✓ Order-rules monitor started (flags triggers every 30s during market hours)")

if __name__=="__main__":
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(message)s")
    if HAS_FASTAPI:
        from dashboard.security import dashboard_host
        uvicorn.run("dashboard.server:app",host=dashboard_host(),port=8000,reload=False)
    else:
        print("pip install fastapi uvicorn")
