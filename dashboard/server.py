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


def chg_span(last, prev, size=10):
    """
    CMP change rendered the way a quote screen renders it: absolute move and
    percentage, coloured, both against the previous close.
    """
    p = pct_change(last, prev)
    if p is None:
        return ""
    diff = float(last) - float(prev)
    col = "#059669" if p >= 0 else "#dc2626"
    sign = "+" if p >= 0 else ""
    return (f'<span style="font-size:{size}px;color:{col}"> {sign}{diff:.2f} '
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
    conn=get_connection()
    try: return q1(conn,"SELECT * FROM global_markets WHERE date<=? ORDER BY date DESC LIMIT 1",str(td))
    finally: conn.close()

def get_tod(td):
    conn=get_connection()
    try:
        d=q1(conn,"SELECT s.*,p.close as cmp,"
             "(SELECT p2.close FROM prices_daily p2 WHERE p2.symbol=s.symbol AND p2.date<? ORDER BY p2.date DESC LIMIT 1) AS prev_close,"
             "t.atr_14 FROM ai_scores s LEFT JOIN prices_daily p ON s.symbol=p.symbol AND p.date=? "
             "LEFT JOIN technical_indicators t ON s.symbol=t.symbol AND t.date=? "
             "WHERE s.date=? AND s.is_tod=1 LIMIT 1",str(td),str(td),str(td),str(td))
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
    try: return q(conn,"SELECT h.*,"
                  "(SELECT p2.close FROM prices_daily p2 WHERE p2.symbol=h.symbol AND p2.date<? ORDER BY p2.date DESC LIMIT 1) AS prev_close "
                  "FROM portfolio_holdings h WHERE h.date=? ORDER BY h.weight_pct DESC",
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

def get_signal_history(limit=150):
    """Signal history + momentum success rates for the History tab."""
    try:
        from scores.signal_log import success_report, recent_signals, momentum_thresholds
        return {"report": success_report(), "recent": recent_signals(limit=limit),
                "thresholds": list(momentum_thresholds())}
    except Exception as e:
        log.warning(f"  signal history unavailable: {e}")
        return {"report": {"buckets": [], "total_signals": 0}, "recent": [], "thresholds": []}


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
            return q(conn,f"SELECT s.symbol,s.atip_score,s.{col},s.signal,s.cri,s.acs,s.beta_1y,p.close as cmp,"
                          f"(SELECT p2.close FROM prices_daily p2 WHERE p2.symbol=s.symbol AND p2.date<s.date ORDER BY p2.date DESC LIMIT 1) AS prev_close "
                          f"FROM ai_scores s LEFT JOIN prices_daily p ON s.symbol=p.symbol AND p.date=s.date "
                          f"WHERE s.date=? {where} ORDER BY s.{col} DESC LIMIT 25",str(td))
        return {"vpi":t25("vpi"),"rri":t25("rri"),"mri":t25("mri"),"zpi":t25("zpi","AND s.signal='BUY'"),"cri":t25("cri","AND s.cri>60")}
    finally: conn.close()

def generate_state(td=None):
    if td is None: td=latest_scored_date()
    state={"generated_at":str(datetime.now()),"trade_date":str(td),"scores":get_scores(td),"mh":get_mh(td),"indexes":get_indexes(td),"global":get_global(td),"tod":get_tod(td),"news":get_news(),"portfolio":get_portfolio(td),"top25":get_top25(td),"fii_dii":get_fii_dii(td),"phs":get_phs(td),"sighist":get_signal_history()}
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

def order_btns(sym,cmp,primary="BUY"):
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
    """
    if not sym: return "—"
    c=cmp or 0
    b_cls="obuy" if primary=="BUY" else "obuy ghost"
    s_cls="osell" if primary=="SELL" else "osell ghost"
    return (f'<button class="{b_cls}" onclick="openOrderModal(\'{sym}\',{c},\'BUY\')">Buy</button>'
            f'<button class="{s_cls}" onclick="openOrderModal(\'{sym}\',{c},\'SELL\')">Sell</button>')

def _check_js(html):
    """
    Fail loudly if the generated <script> block has an unterminated string
    literal.

    The whole UI is one inline script, so a single broken literal is a
    SyntaxError that prevents showTab/srt/ft from ever being defined — every
    tab silently stops working while the page still renders and the server
    still returns 200. That happened: a backslash-n written in the Python
    f-string became a REAL newline in the emitted JS and split two alert()
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
    score_rows="".join(f"""<tr data-sym="{r.get('symbol')}"><td>{r.get('atip_rank','')}</td><td><b>{r.get('symbol')}</b>{'⭐' if r.get('is_tod') else ''}{cname(r.get('symbol'))}</td><td>{pill(r.get('atip_score'))}</td><td>{pill(r.get('vpi'))}</td><td>{pill(r.get('mri'))}</td><td>{pill(r.get('rri'))}</td><td>{pill(r.get('zpi'))}</td><td>{pill(r.get('cri'),inv=True)}</td><td>{pill(r.get('acs'))}</td><td class="cmpcell" data-prev="{r.get('prev_close') or ''}" data-cmp="{r.get('cmp') or ''}">₹{r.get('cmp') or '—'}{chg_span(r.get('cmp'), r.get('prev_close'))}</td><td data-v="{r.get('beta_1y') if r.get('beta_1y') is not None else ''}">{bval(r.get('beta_1y'))}</td><td style="color:{'#059669' if r.get('signal')=='BUY' else '#dc2626' if r.get('signal')=='SELL' else '#2563eb'};font-weight:600">{r.get('signal','—')}</td><td style="font-size:10px;color:#64748b">{r.get('top_factor_1','')}</td><td class="acts">{order_btns(r.get('symbol'),r.get('cmp'),primary=('SELL' if r.get('signal')=='SELL' else 'BUY'))}</td></tr>""" for r in scores[:50])
    news_rows="".join(f"""<tr><td style="font-size:12px;max-width:300px">{n.get('headline','')}</td><td style="font-size:11px">{n.get('source','')}</td><td style="color:{'#dc2626' if n.get('importance')=='HIGH' else '#f59e0b'};font-size:11px;font-weight:600">{n.get('importance','')}</td><td style="color:{'#059669' if (n.get('sentiment') or 0)>0.1 else '#dc2626' if (n.get('sentiment') or 0)<-0.1 else '#64748b'};font-weight:600">{(n.get('sentiment') or 0):+.2f}</td></tr>""" for n in news[:15])
    port_rows="".join(f"""<tr data-sym="{p.get('symbol')}"><td><b>{p.get('symbol')}</b>{cname(p.get('symbol'))}</td><td>{p.get('qty')}</td><td>₹{p.get('avg_price') or '—'}</td><td class="cmpcell" data-prev="{p.get('prev_close') or ''}" data-cmp="{p.get('cmp') or ''}">₹{p.get('cmp') or '—'}{chg_span(p.get('cmp'), p.get('prev_close'))}</td><td style="color:{'#059669' if (p.get('pnl_pct') or 0)>=0 else '#dc2626'};font-weight:600">{(p.get('pnl_pct') or 0):+.1f}%</td><td>{pill(p.get('atip_score'))}</td><td>{pill(p.get('cri'),inv=True)}</td><td style="font-size:11px">{p.get('signal','—')}</td><td class="acts">{order_btns(p.get('symbol'),p.get('cmp'),primary='SELL')}</td></tr>""" for p in port)
    # Show the LEVEL beside the change. A column of "+0.00%" tells you nothing
    # about where the market closed, which is the first thing you look for
    # outside market hours -- and pre-open every change legitimately reads 0.00%
    # because Dhan's prev_close equals the last traded price until the bell.
    def _lvl(v):
        x = idx.get(v)
        return (f'<span style="color:#64748b;font-size:11px;margin-right:6px">{x:,.0f}</span>'
                if isinstance(x, (int, float)) else '')
    idx_rows="".join(f'<div style="display:flex;justify-content:space-between;margin:4px 0;font-size:12px"><span style="color:#94a3b8">{k}</span><span>{_lvl(v[:-4])}{chg(idx.get(v))}</span></div>' for k,v in [("Nifty50","nifty50_chg"),("BankNifty","banknifty_chg"),("Midcap150","midcap150_chg"),("SmallCap250","smallcap250_chg"),("IT","nifty_it_chg"),("Auto","nifty_auto_chg"),("FMCG","nifty_fmcg_chg"),("Metal","nifty_metal_chg"),("Realty","nifty_realty_chg"),("PSUBank","nifty_psubank_chg"),("Energy","nifty_energy_chg"),("Pharma","nifty_pharma_chg")])
    glb_rows="".join(f'<div style="display:flex;justify-content:space-between;margin:4px 0;font-size:12px"><span style="color:#94a3b8">{k}</span>{chg(glb.get(v))}</div>' for k,v in [("S&P500","sp500_chg"),("Dow","dow_chg"),("Nasdaq","nasdaq_chg"),("Nikkei","nikkei_chg"),("Crude","crude_wti_chg"),("Gold","gold_chg"),("USD/INR","usd_inr_chg")])
    # One row shape for all five Top-25 lists. Previously the three that had a
    # tab were three near-identical 300-char f-strings; adding RRI and MRI as a
    # fourth and fifth copy wasn't worth it.
    def t25_row(r,mid,primary="BUY",sig_style=""):
        return (f'<tr data-sym="{r.get("symbol")}">'
                f'<td><b>{r.get("symbol")}</b>{cname(r.get("symbol"))}</td>{mid}'
                f'<td class="cmpcell" data-prev="{r.get("prev_close") or ""}" data-cmp="{r.get("cmp") or ""}">₹{r.get("cmp") or "—"}{chg_span(r.get("cmp"), r.get("prev_close"))}</td>'
                f'<td data-v="{r.get("beta_1y") if r.get("beta_1y") is not None else ""}">{bval(r.get("beta_1y"))}</td>'
                f'<td{sig_style}>{r.get("signal","—")}</td>'
                f'<td class="acts">{order_btns(r.get("symbol"),r.get("cmp"),primary=primary)}</td></tr>')

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
        if v is None: return '<span style="color:#64748b">—</span>'
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
              else f'<span style="color:#64748b">{idx_time}</span>')
    sectors=get_sector_rotation(idx)
    def heat(v):
        """Background intensity scaled to ±2%, which covers a normal NSE day."""
        cap=min(abs(v)/2.0,1.0)
        base="5,150,105" if v>=0 else "220,38,38"
        return f"rgba({base},{0.15+0.55*cap:.2f})"
    sector_tiles="".join(
        f'<div style="background:{heat(v)};border:1px solid #33415577;border-radius:6px;'
        f'padding:7px 9px;min-width:96px;text-align:center">'
        f'<div style="font-size:11px;color:#e2e8f0">{lbl}</div>'
        f'<div style="font-size:14px;font-weight:700;color:#fff">{v:+.2f}%</div></div>'
        for lbl,v in sectors) or '<div style="color:#64748b;font-size:12px">No sector data for this date.</div>'
    # ── Signal history + momentum success rates ──────────────────────────────
    sh = state.get("sighist") or {}
    sh_rep = sh.get("report") or {}
    ths = sh.get("thresholds") or []

    def _pct(v, good=55.0):
        if v is None:
            return '<span style="color:#64748b">-</span>'
        c = "#059669" if v >= good else "#f59e0b" if v >= 35 else "#dc2626"
        return f'<span style="color:{c};font-weight:700">{v:.0f}%</span>'

    def _num(v, suffix="d"):
        return f"{v}{suffix}" if v else '<span style="color:#64748b">n/a</span>'

    def _signed(v):
        if v is None:
            return "-"
        c = "#059669" if v >= 0 else "#dc2626"
        return f'<span style="color:{c}">{v:+.1f}%</span>'

    succ_rows = ""
    for b in sh_rep.get("buckets", []):
        sig_c = "#059669" if b["signal"] == "BUY" else "#dc2626"
        succ_rows += (
            f'<tr><td style="font-weight:600;color:{sig_c}">{b["signal"]}</td>'
            f'<td><b>{b["threshold_pct"]:g}%</b></td>'
            f'<td>{b["resolved"]}</td><td>{b["hits"]}</td>'
            f'<td>{_pct(b["hit_rate"])}</td>'
            f'<td>{_num(b["median_sessions"])}</td>'
            f'<td>{_num(b["fastest_sessions"])}</td>'
            f'<td>{_num(b["slowest_sessions"])}</td>'
            f'<td>{_signed(b.get("avg_mfe"))}</td>'
            f'<td>{_signed(b.get("avg_mae"))}</td>'
            f'<td style="color:#64748b">{b["open"]}</td></tr>')

    def _oc(o):
        """One threshold cell: hit / miss / still open, with sessions taken."""
        if not o:
            return '<span style="color:#64748b">-</span>'
        if o.get("still_open"):
            return '<span style="color:#64748b">open</span>'
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

    hist_rows = ""
    for r in sh.get("recent", []):
        outs = r.get("outcomes", [])
        omap = {o["threshold_pct"]: o for o in outs}
        cells = "".join(f'<td style="text-align:center">{_oc(omap.get(t))}</td>' for t in ths)
        mfe = next((o.get("max_favourable_pct") for o in outs if o.get("max_favourable_pct") is not None), None)
        mae = next((o.get("max_adverse_pct") for o in outs if o.get("max_adverse_pct") is not None), None)
        sig_c = "#059669" if r.get("signal") == "BUY" else "#dc2626"
        hist_rows += (
            f'<tr data-sym="{r.get("symbol")}"><td>{r.get("signal_date")}</td>'
            f'<td><b>{r.get("symbol")}</b>{"*" if r.get("is_tod") else ""}</td>'
            f'<td style="color:{sig_c};font-weight:600">{r.get("signal")}</td>'
            f'<td>Rs{r.get("entry_price") or "-"}</td>'
            f'<td>{pill(r.get("atip_score"))}</td><td>{pill(r.get("zpi"))}</td>'
            f'<td>{pill(r.get("cri"),inv=True)}</td>{cells}'
            f'<td>{_signed(mfe)}</td><td>{_signed(mae)}</td>'
            f'<td style="font-size:10px;color:#64748b">{r.get("model_version") or "-"}</td></tr>')

    th_heads = "".join(f'<th style="text-align:center">{t:g}%</th>' for t in ths)
    hist_rows_or_empty = hist_rows or (
        '<tr><td colspan="14" style="text-align:center;color:#64748b;padding:20px">'
        'No signals logged yet. The post-market run appends them; backfill past '
        'dates with: python -m scores.signal_log --backfill</td></tr>')
    sh_total = sh_rep.get("total_signals", 0)
    sh_span = (f'{sh_rep.get("first","")} to {sh_rep.get("last","")}'
               if sh_rep.get("first") else "no signals yet")
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
    tod_sym=tod.get('symbol','—'); tod_sig=tod.get('signal','—'); tod_cmp=tod.get('cmp','—')
    tod_cname=names.get((tod.get('symbol') or "").upper(),"")
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ATIP Dashboard</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}body{{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;font-size:13px}}.topbar{{background:#1e293b;padding:10px 18px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #334155}}.logo{{font-size:17px;font-weight:700;color:#38bdf8}}.kpi-row{{display:flex;gap:8px;padding:10px 18px;flex-wrap:wrap;background:#1e293b;border-bottom:1px solid #334155}}.kpi{{background:#0f172a;border:1px solid #334155;border-radius:8px;padding:8px 14px;min-width:100px}}.kpi-l{{font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px}}.kpi-v{{font-size:20px;font-weight:700}}.body{{display:flex}}.sidebar{{width:200px;background:#1e293b;border-right:1px solid #334155;padding:12px;overflow-y:auto;min-height:100vh}}.sidebar h3{{font-size:10px;color:#64748b;text-transform:uppercase;margin-bottom:6px;margin-top:14px}}.sidebar h3:first-child{{margin-top:0}}.main{{flex:1;padding:14px;overflow-x:auto}}.section{{margin-bottom:20px}}.st{{font-size:13px;font-weight:600;color:#38bdf8;margin-bottom:8px;padding-bottom:5px;border-bottom:1px solid #334155}}.tod-card{{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:14px;display:grid;grid-template-columns:1fr 1fr;gap:10px}}.tod-sym{{font-size:24px;font-weight:700;grid-column:1/-1}}.tod-l{{font-size:11px;color:#94a3b8}}.tod-v{{font-size:13px;font-weight:600}}.tabs{{display:flex;gap:4px;margin-bottom:10px}}.tab{{padding:5px 12px;border-radius:6px;font-size:12px;cursor:pointer;border:1px solid #334155;background:#1e293b;color:#94a3b8}}.tab.active{{background:#2563eb;color:#fff;border-color:#2563eb}}.tc{{display:none}}.tc.active{{display:block}}table{{width:100%;border-collapse:collapse;background:#1e293b;border-radius:8px;overflow:hidden;font-size:11.5px}}th{{background:#0f172a;color:#94a3b8;padding:7px 7px;text-align:left;border-bottom:1px solid #334155;font-size:11px;cursor:pointer;white-space:nowrap}}th:hover{{color:#e2e8f0}}td{{padding:6px 7px;border-bottom:1px solid #1e293b22;white-space:nowrap}}tr:hover td{{background:#0f172a}}.disc{{font-size:10px;color:#475569;margin-top:16px;padding-top:10px;border-top:1px solid #334155;line-height:1.6}}input,select{{padding:5px 10px;border-radius:6px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;font-size:12px}}.rf{{background:#2563eb;color:#fff;border:none;padding:5px 12px;border-radius:6px;cursor:pointer;font-size:12px}}
.ob{{background:#1e293b;border:1px solid #38bdf8;color:#38bdf8;padding:3px 10px;border-radius:5px;font-size:11px;cursor:pointer}}.ob:hover{{background:#38bdf8;color:#0f172a}}
.acts{{white-space:nowrap}}.acts button{{padding:3px 11px;border-radius:5px;font-size:11px;cursor:pointer;font-weight:600;margin-right:4px}}
.obuy{{background:#059669;border:1px solid #059669;color:#fff}}.obuy:hover{{background:#047857}}
.osell{{background:#dc2626;border:1px solid #dc2626;color:#fff}}.osell:hover{{background:#b91c1c}}
.acts button.ghost{{background:transparent}}.obuy.ghost{{color:#059669}}.osell.ghost{{color:#dc2626}}
.acts button.ghost:hover{{color:#fff}}.obuy.ghost:hover{{background:#059669}}.osell.ghost:hover{{background:#dc2626}}
.tod-card .acts button{{padding:6px 20px;font-size:12.5px;margin-top:4px}}
.cmpwarn{{color:#f59e0b;font-size:11px;margin-bottom:8px;display:none}}.cmpwarn.show{{display:block}}
.brk{{background:#0f172a;border:1px solid #334155;border-radius:7px;padding:10px;margin:10px 0}}
.brk .mrow{{margin-bottom:6px}}.brk input[type=number]{{max-width:80px;flex:0 0 auto}}
.u{{font-size:11px;color:#64748b}}
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
{stale_banner}
<div id="brokerBanner" class="banner dry">Checking broker status…</div>
<div id="pendBox" class="pend" style="margin:10px 18px 0"><b style="color:#dc2626">⚠️ Awaiting confirmation</b><div id="pendList"></div></div>
<div class="kpi-row">
  <div class="kpi"><div class="kpi-l">Market Health</div><div class="kpi-v" style="color:{mh_col}">{mh_s:.0f}</div><div style="font-size:11px;color:#64748b">{regime}</div></div>
  <div class="kpi"><div class="kpi-l">Nifty 50</div><div class="kpi-v">{chg(idx.get('nifty50_chg'))}</div><div style="font-size:11px;color:#64748b">{(f"{idx.get('nifty50'):,.2f}" if idx.get('nifty50') else '—')}</div></div>
  <div class="kpi"><div class="kpi-l">Bank Nifty</div><div class="kpi-v">{chg(idx.get('banknifty_chg'))}</div><div style="font-size:11px;color:#64748b">{(f"{idx.get('banknifty'):,.2f}" if idx.get('banknifty') else '—')}</div></div>
  <div class="kpi"><div class="kpi-l">India VIX</div><div class="kpi-v" style="color:{'#dc2626' if (idx.get('india_vix') or 0)>20 else '#94a3b8'}">{idx.get('india_vix','—')}</div></div>
  <div class="kpi"><div class="kpi-l">GIFT Nifty</div><div class="kpi-v">{chg(idx.get('gift_nifty_chg'))}</div><div style="font-size:11px;color:#64748b">{(f"{idx.get('gift_nifty'):,.0f}" if idx.get('gift_nifty') else '—')}</div></div>
  <div class="kpi"><div class="kpi-l">Sentiment</div><div class="kpi-v" style="color:{'#059669' if idx.get('overall_sentiment')=='BULLISH' else '#dc2626' if idx.get('overall_sentiment')=='BEARISH' else '#94a3b8'}">{idx.get('overall_sentiment') or '—'}</div></div>
  <div class="kpi"><div class="kpi-l">S&P 500</div><div class="kpi-v">{chg(glb.get('sp500_chg'))}</div></div>
  <div class="kpi"><div class="kpi-l">Gold</div><div class="kpi-v">{chg(glb.get('gold_chg'))}</div></div>
  <div class="kpi"><div class="kpi-l">USD/INR</div><div class="kpi-v">{chg(glb.get('usd_inr_chg'))}</div></div>
</div>
<div class="body">
<div class="sidebar"><h3>NSE Indexes &nbsp;{idx_note}</h3>{idx_rows}<h3>Global</h3>{glb_rows}</div>
<div class="main">
  <div class="section"><div class="st">🎯 Trade of the Day</div>
    <div class="tod-card" data-sym="{tod_sym}">
      <div class="tod-sym">{tod_sym} <span style="font-size:13px;color:#059669">{tod_sig}</span>{f'<div style="font-size:12px;color:#94a3b8;font-weight:400">{tod_cname}</div>' if tod_cname else ''}</div>
      <div><div class="tod-l">CMP <span style="color:#38bdf8">●live</span></div><div class="tod-v cmpcell" data-prev="{tod.get('prev_close') or ''}" data-cmp="{tod.get('cmp') or ''}">₹{tod_cmp}{chg_span(tod.get('cmp'), tod.get('prev_close'), size=12)}</div></div>
      <div><div class="tod-l">Stop Loss</div><div class="tod-v" style="color:#dc2626">₹{tod.get('sl','—')}</div></div>
      <div><div class="tod-l">Target 1</div><div class="tod-v" style="color:#059669">₹{tod.get('t1','—')}</div></div>
      <div><div class="tod-l">Target 2</div><div class="tod-v" style="color:#059669">₹{tod.get('t2','—')}</div></div>
      <div><div class="tod-l">ACS Confidence</div><div class="tod-v">{(tod.get('acs') or 0):.0f}/100</div></div>
      <div style="grid-column:1/-1;font-size:11px;color:#94a3b8">✦ {tod.get('top_factor_1','—')} &nbsp; ✦ {tod.get('top_factor_2','—')}</div>
      <div style="grid-column:1/-1" class="acts">{order_btns(tod.get('symbol'),tod.get('cmp'),primary=('SELL' if tod_sig=='SELL' else 'BUY'))}</div>
    </div>
  </div>
  <div class="section"><div class="st">🔄 Sector Rotation <span style="font-size:11px;color:#64748b;font-weight:400">— strongest first &nbsp;{idx_note}</span></div>
    <div style="display:flex;gap:6px;flex-wrap:wrap">{sector_tiles}</div>
  </div>
  <div class="tabs"><div class="tab active" onclick="showTab('scores',this)">ATIP Scores</div><div class="tab" onclick="showTab('port',this)">Portfolio</div><div class="tab" onclick="showTab('vpi',this)">Top VPI</div><div class="tab" onclick="showTab('zpi',this)">Buy Zones</div><div class="tab" onclick="showTab('rri',this)">Recovery (RRI)</div><div class="tab" onclick="showTab('mri',this)">Momentum (MRI)</div><div class="tab" onclick="showTab('cri',this)">CRI Risk</div><div class="tab" onclick="showTab('fiidii',this)">FII / DII</div><div class="tab" onclick="showTab('news',this)">News</div><div class="tab" onclick="showTab('hist',this)">Signal History</div></div>
  <div id="scores" class="tc active section">
    <div style="display:flex;gap:6px;margin-bottom:8px"><input id="srch" placeholder="Search…" oninput="ft()"><select id="sf" onchange="ft()"><option value="">All signals</option><option>BUY</option><option>SELL</option><option>HOLD</option><option>WAIT</option></select></div>
    <table id="st"><thead><tr><th onclick="srt('st',0)">#</th><th onclick="srt('st',1)">Symbol</th><th onclick="srt('st',2)">ATIP</th><th onclick="srt('st',3)">VPI</th><th onclick="srt('st',4)">MRI</th><th onclick="srt('st',5)">RRI</th><th onclick="srt('st',6)">ZPI</th><th onclick="srt('st',7)">CRI↓</th><th onclick="srt('st',8)">ACS</th><th onclick="srt('st',9)">CMP <span style="color:#38bdf8">●live</span></th><th onclick="srt('st',10)">Beta</th><th onclick="srt('st',11)">Signal</th><th>Factor</th><th>Action</th></tr></thead><tbody>{score_rows}</tbody></table>
  </div>
  <div id="port" class="tc section">
    {f'''<div style="display:flex;gap:14px;align-items:flex-start;margin-bottom:12px;flex-wrap:wrap">
      <div style="background:#1e293b;border:1px solid #334155;border-radius:10px;padding:14px 18px;min-width:190px">
        <div class="kpi-l">Portfolio Health</div>
        <div style="font-size:32px;font-weight:700;color:{phs_col}">{phs_score:.0f}</div>
        <div style="font-size:12px;color:#94a3b8">{phs.get("band","—")} · {phs.get("holdings",0)} holdings</div>
        <div style="font-size:11px;color:#64748b;margin-top:4px">Portfolio beta {phs.get("portfolio_beta") or "—"} · regime {phs.get("regime","—")}</div>
        {f'<div style="font-size:11px;color:#f59e0b;margin-top:4px">⚠ holdings as of {phs.get("date")} — portfolio sync has not run since</div>' if phs.get("stale") else ''}
      </div>
      <div style="background:#1e293b;border:1px solid #334155;border-radius:10px;padding:12px 16px;min-width:270px">
        <div class="kpi-l" style="margin-bottom:5px">Components</div>{phs_comp}
      </div>
    </div>''' if phs_score is not None else ''}
    <table><thead><tr><th>Symbol</th><th>Qty</th><th>Avg</th><th>CMP <span style="color:#38bdf8">●live</span></th><th>P&L%</th><th>ATIP</th><th>CRI↓</th><th>Signal</th><th>Action</th></tr></thead><tbody>{port_rows if port_rows else '<tr><td colspan="9" style="text-align:center;color:#64748b;padding:20px">No portfolio data. Configure Dhan (or Zerodha Kite) API and run a portfolio sync.</td></tr>'}</tbody></table></div>
  <div id="vpi" class="tc section"><table><thead><tr><th>Symbol</th><th>ATIP</th><th>VPI</th><th>CMP <span style="color:#38bdf8">●live</span></th><th>Beta</th><th>Signal</th><th>Action</th></tr></thead><tbody>{top25_rows.get('vpi','')}</tbody></table></div>
  <div id="zpi" class="tc section"><table><thead><tr><th>Symbol</th><th>ZPI</th><th>ATIP</th><th>CMP <span style="color:#38bdf8">●live</span></th><th>Beta</th><th>Signal</th><th>Action</th></tr></thead><tbody>{top25_rows.get('zpi','')}</tbody></table></div>
  <div id="rri" class="tc section"><table><thead><tr><th>Symbol</th><th>RRI</th><th>ATIP</th><th>CMP <span style="color:#38bdf8">●live</span></th><th>Beta</th><th>Signal</th><th>Action</th></tr></thead><tbody>{top25_rows.get('rri','') or '<tr><td colspan="7" style="text-align:center;color:#64748b;padding:20px">No RRI data for this date.</td></tr>'}</tbody></table></div>
  <div id="mri" class="tc section"><table><thead><tr><th>Symbol</th><th>MRI</th><th>ATIP</th><th>CMP <span style="color:#38bdf8">●live</span></th><th>Beta</th><th>Signal</th><th>Action</th></tr></thead><tbody>{top25_rows.get('mri','') or '<tr><td colspan="7" style="text-align:center;color:#64748b;padding:20px">No MRI data for this date.</td></tr>'}</tbody></table></div>
  <div id="cri" class="tc section"><table><thead><tr><th>Symbol</th><th>CRI 🔴</th><th>ATIP</th><th>CMP <span style="color:#38bdf8">●live</span></th><th>Beta</th><th>Signal</th><th>Action</th></tr></thead><tbody>{top25_rows.get('cri','')}</tbody></table></div>
  <div id="fiidii" class="tc section"><table><thead><tr><th>Date</th><th>FII net ₹Cr</th><th>DII net ₹Cr</th><th>FII 5-day avg</th><th>DII 5-day avg</th></tr></thead><tbody>{fii_rows if fii_rows else '<tr><td colspan="5" style="text-align:center;color:#64748b;padding:20px">No FII/DII data yet — it is fetched by the post-market Bhavcopy job.</td></tr>'}</tbody></table></div>
  <div id="news" class="tc section"><table><thead><tr><th style="width:320px">Headline</th><th>Source</th><th>Importance</th><th>Sentiment</th></tr></thead><tbody>{news_rows}</tbody></table></div>
  <div id="hist" class="tc section">
    <div class="st">Momentum success rate &mdash; did price move the way the signal said?</div>
    <table><thead><tr><th>Signal</th><th>Target</th><th>Resolved</th><th>Hits</th><th>Hit rate</th>
      <th>Median</th><th>Fastest</th><th>Slowest</th><th>Avg best</th><th>Avg worst</th><th>Open</th></tr></thead>
      <tbody>{succ_rows}</tbody></table>
    {sh_note}
    <div class="st" style="margin-top:16px">Signal log &mdash; {sh_total} signals, {sh_span} (append-only)</div>
    <div style="display:flex;gap:6px;margin-bottom:8px">
      <input id="hsrch" placeholder="Filter by symbol..." oninput="hft()">
      <select id="hsf" onchange="hft()"><option value="">All signals</option><option>BUY</option><option>SELL</option></select>
    </div>
    <table id="ht"><thead><tr><th>Date</th><th>Symbol</th><th>Signal</th><th>Entry</th><th>ATIP</th><th>ZPI</th><th>CRI</th>
      {th_heads}<th>Best</th><th>Worst</th><th>Model</th></tr></thead>
      <tbody>{hist_rows_or_empty}</tbody></table>
  </div>
  <p class="disc">⚠️ ATIP is for personal informational use only. Not financial advice. All AI scores are model outputs — verify independently. Not SEBI registered. Consult a registered advisor before investing.</p>
</div></div>

<div class="modal-bg" id="modalBg"><div class="modal">
  <h3>Order Rule — <span id="mSym"></span></h3>
  <div class="sub" id="mCmp"></div>
  <div class="cmpwarn" id="mCmpWarn">⚠ No reference price available for this stock — enter an absolute target price (% move needs a reference).</div>
  <div class="mrow"><label>Side</label><div class="seg" id="mSide"><button type="button" class="on" data-v="BUY" onclick="setSide('BUY')">Buy</button><button type="button" data-v="SELL" onclick="setSide('SELL')">Sell</button></div></div>
  <div class="mrow"><label>Target</label><select id="mTT" onchange="mPrev()"><option value="PRICE">At price ₹</option><option value="PERCENT">% move</option></select><input type="number" step="0.01" id="mTV" placeholder="e.g. 2500" oninput="mPrev()"></div>
  <div class="mrow"><label>Qty</label><select id="mQT" onchange="mPrev()"><option value="SHARES">Shares</option><option value="AMOUNT">₹ Amount</option></select><input type="number" step="0.01" id="mQV" placeholder="e.g. 10" oninput="mPrev()"></div>
  <div class="mrow"><label>Product</label><select id="mProd"><option value="CNC">Delivery</option><option value="INTRADAY">Intraday</option></select><select id="mOT" onchange="mPrev()"><option value="MARKET">Market</option><option value="LIMIT">Limit</option></select><input type="number" step="0.01" id="mLimit" class="hide" placeholder="Limit ₹"></div>
  <div class="brk">
    <div style="font-size:11.5px;color:#38bdf8;font-weight:600;margin-bottom:7px">🎯 Bracket — auto-exit after this order fills</div>
    <div class="mrow"><label><input type="checkbox" id="mBrk" checked onchange="mPrev()"> Enable</label>
      <span style="font-size:11px;color:#64748b">exits are measured from the actual fill price</span></div>
    <div class="mrow"><label>Target 1</label><input type="number" step="0.1" id="mBT1" value="3" oninput="mPrev()"><span class="u">% profit — exits</span><input type="number" step="5" id="mBSplit" value="50" oninput="mPrev()" style="max-width:60px"><span class="u">% of qty</span></div>
    <div class="mrow"><label>Target 2</label><input type="number" step="0.1" id="mBT2" value="6" oninput="mPrev()"><span class="u">% — exits the remainder (blank = single target)</span></div>
    <div class="mrow"><label>Stoploss</label><input type="number" step="0.1" id="mBSL" value="2" oninput="mPrev()"><span class="u">% loss</span></div>
    <div class="mrow"><label><input type="checkbox" id="mTrail" checked onchange="mPrev()"> Trail it</label>
      <input type="number" step="0.1" id="mTrailV" value="2" oninput="mPrev()"><span class="u">% below the high</span>
      <input type="number" step="0.5" id="mTrailJ" value="0" oninput="mPrev()" style="max-width:60px"><span class="u">₹ step (0 = smooth)</span></div>
    <div style="font-size:10.5px;color:#f59e0b;line-height:1.5">⚠ Trailing is computed by THIS dashboard, not by Dhan — it freezes if the dashboard stops. Run <code>python -m orders.rules --probe-broker</code> to see if your Dhan SDK can hold the stop broker-side instead.</div>
    <div class="mrow"><label style="width:auto"><input type="checkbox" id="mBAuto" checked onchange="mPrev()"> Exit legs fire without asking me</label></div>
    <div style="font-size:10.5px;color:#94a3b8;line-height:1.5">A stop that waits for a tap protects nothing while you're away, so this is on by default. It still refuses to act on a quote older than 3 minutes.</div>
  </div>
  <div class="mrow"><label style="width:auto"><input type="checkbox" id="mConfirm" checked> Require confirmation before the ENTRY order is placed</label></div>
  <div class="preview" id="mPreviewTxt">—</div>
  <div class="rules-mini"><b style="font-size:11px;color:#64748b">Active rules for this stock</b><ul id="mRulesList"></ul></div>
  <div class="mactions"><button class="msave" onclick="saveOrderRule()">Save Rule</button><button class="mcancel" onclick="closeOrderModal()">Close</button></div>
</div></div>
<script>
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
    }});
  }});
}}
document.addEventListener('DOMContentLoaded',makeSortable);
makeSortable();
function ft(){{
  var q=(document.getElementById('srch')||{{value:''}}).value.toLowerCase();
  var s=(document.getElementById('sf')||{{value:''}}).value.toLowerCase();
  var rows=document.querySelectorAll('#st tbody tr');
  for(var i=0;i<rows.length;i++){{
    var txt=rows[i].innerText.toLowerCase();
    rows[i].style.display=(txt.indexOf(q)>=0&&(s===''||txt.indexOf(s)>=0))?'':'none';
  }}
}}

function hft(){{
  var q=(document.getElementById('hsrch')||{{value:''}}).value.toLowerCase();
  var s=(document.getElementById('hsf')||{{value:''}}).value.toLowerCase();
  var rows=document.querySelectorAll('#ht tbody tr');
  for(var i=0;i<rows.length;i++){{
    var t=rows[i].innerText.toLowerCase();
    rows[i].style.display=(t.indexOf(q)>=0&&(s===''||t.indexOf(s)>=0))?'':'none';
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
function openOrderModal(sym,cmp,side){{
  mSymbol=sym;
  mCmpVal=(cmp&&cmp>0)?cmp:liveCmpFor(sym);
  mSideVal=side||'BUY';
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
  var qv=document.getElementById('mQV').value||'?', qt=document.getElementById('mQT').value==='SHARES'?'shares':'₹ worth';
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
}}
async function saveOrderRule(){{
  var tt=document.getElementById('mTT').value;
  // Validate before POSTing — an empty target or qty used to be sent as
  // null/NaN, which the API rejected with a bare 400 and no visible reason.
  if(mResolvedTrigger()===null){{alert('Enter a target '+(tt==='PRICE'?'price':'% move')+' first.');return;}}
  var qv=parseFloat(document.getElementById('mQV').value);
  if(isNaN(qv)||qv<=0){{alert('Enter a quantity (shares or ₹ amount) greater than 0.');return;}}
  if(document.getElementById('mOT').value==='LIMIT'&&isNaN(parseFloat(document.getElementById('mLimit').value))){{alert('Limit orders need a limit price.');return;}}
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
  var res=await fetch('/api/orders',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}});
  if(!res.ok){{var e=await res.json().catch(()=>({{}}));alert('Failed: '+(e.error||res.statusText));return;}}
  document.getElementById('mPreviewTxt').textContent='✓ Rule saved — it will be monitored while the dashboard is running, and flagged here for your confirmation when the target is hit.';
  document.getElementById('mTV').value=''; document.getElementById('mQV').value='';
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
    var role=r.role&&r.role!=='ENTRY'?' <b style="color:'+(r.role==='STOP'?'#dc2626':'#059669')+'">['+r.role+']</b>':'';
    var dir=r.trigger_direction==='BELOW'?'≤':'≥';
    li.innerHTML='<span>'+r.side+role+' '+r.quantity_value+(r.quantity_type==='SHARES'?' sh':' ₹')+' '+dir+' ₹'+r.resolved_trigger_price.toFixed(2)+(r.require_confirmation?'':' ⚡')+'</span><button onclick="deleteRule(\\''+r.id+'\\')" style="background:none;border:none;color:#dc2626;cursor:pointer">✕</button>';
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
async function confirmRule(id,force){{
  var res=await fetch('/api/orders/'+id+'/confirm'+(force?'?force=true':''),{{method:'POST'}});
  if(res.status===409){{
    // Price ran away between the trigger and your tap — say by how much and
    // let it be an explicit decision rather than a silent fill.
    var e=await res.json().catch(()=>({{}}));
    if(confirm((e.error||'Price moved since the trigger.')+'\\n\\nPlace the order anyway at the current price?')){{
      await fetch('/api/orders/'+id+'/confirm?force=true',{{method:'POST'}});
    }}
  }} else if(!res.ok){{
    var e2=await res.json().catch(()=>({{}}));
    alert('Confirm failed: '+(e2.error||res.statusText));
  }} else {{
    var out=await res.json().catch(()=>({{}}));
    var legs=(out.result&&out.result.bracket_legs)||[];
    if(legs.length) alert('Order placed. Protective legs created:\\n'+legs.map(l=>l.role+' @ ₹'+l.trigger+' x'+l.qty).join('\\n'));
  }}
  pollPending(); loadMiniRules();
}}
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
      // Against the PREVIOUS session's close, which is what a change figure
      // means on any quote screen. Measuring against the displayed session's own
      // close reported the move since THAT session: -11.2% for NIACL while
      // scores were a day behind, and 0.00% once they were current.
      var prev=parseFloat(cell.dataset.prev);
      var pct = (!isNaN(prev) && prev>0) ? ((qd.ltp-prev)/prev*100) : null;
      var col = pct===null ? '#e2e8f0' : (pct>=0 ? '#059669' : '#dc2626');
      var sign = (pct!==null && pct>=0) ? '+' : '';
      cell.innerHTML = '₹'+qd.ltp.toFixed(2) + (pct!==null
        ? ' <span style="font-size:10px;color:'+col+'">'+sign+(qd.ltp-prev).toFixed(2)+' ('+sign+pct.toFixed(2)+'%)</span>'
        : '');
      cell.dataset.cmp = qd.ltp;
    }});
  }}catch(e){{}}
}}
setInterval(pollLiveQuotes,15000); pollLiveQuotes();
</script></body></html>"""
    # Catch a broken inline script before it silently disables every tab.
    _check_js(html)
    return html

if HAS_FASTAPI:
    app=FastAPI(title="ATIP Dashboard",version="0.2")
    @app.get("/",response_class=HTMLResponse)
    async def dashboard():
        return HTMLResponse(content=build_html(generate_state(latest_scored_date())))
    @app.get("/api/scores")
    async def api_scores(): return JSONResponse(json_safe(get_scores(latest_scored_date())))
    @app.get("/api/mh")
    async def api_mh(): return JSONResponse(json_safe(get_mh(latest_scored_date())))
    @app.get("/api/tod")
    async def api_tod(): return JSONResponse(json_safe(get_tod(latest_scored_date())))
    @app.get("/api/news")
    async def api_news(): return JSONResponse(json_safe(get_news()))
    @app.get("/api/portfolio")
    async def api_portfolio(): return JSONResponse(json_safe(get_portfolio(latest_scored_date())))
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
            return JSONResponse(json_safe(oe.create_rule(payload)))
        except (ValueError, KeyError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.get("/api/orders")
    async def api_list_orders(symbol: str = None, status: str = None):
        return JSONResponse(json_safe(oe.list_rules(symbol=symbol, status=status)))

    @app.get("/api/orders/pending")
    async def api_pending_orders():
        return JSONResponse(json_safe(oe.list_rules(status=oe.PENDING_CONFIRMATION)))

    @app.post("/api/orders/{rule_id}/confirm")
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

    @app.post("/api/orders/{rule_id}/reject")
    async def api_reject_order(rule_id: str):
        rule = oe.reject_rule(rule_id)
        if not rule:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(json_safe(rule))

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
