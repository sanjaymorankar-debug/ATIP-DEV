"""
The trading workspace (UI fixes v2, W1-W5, 3 Nov 2026; benchmark Kite / Dhan / TradingView).

    W1  watchlist rail     a collapsible rail beside every page (desktop), the /watchlist page (phones and the apps):
                           several lists, live LTP with ▲/▼ %, the ATIP score and signal, hover B / S / chart / depth
    W2  order ticket       one drawer for every Buy / Sell button (bottom sheet on phones): qty <-> ₹, product, price
                           type, stop / target; before saving it shows ₹ risk, reward:risk, charges, margin, the daily
                           loss budget left and the EOD-signal vs LTP gap (GET /api/command/preview), and it saves
                           through POST /api/orders -- the existing order-rule API, unchanged, always asking you to
                           confirm before the entry is placed
    W3  pro chart          /stock/{symbol}: TradingView Lightweight Charts 5.0.9 (Apache-2.0, served from
                           /static/vendor): candles, volume, RSI / MACD panes, MA / BB overlays, crosshair with the OHLC
                           legend, ATIP BUY / SELL markers, stop / target price lines, timeframes
    W4  market depth       beside the chart: 5 or 20 levels from the 20-level depth feed's own book in memory; when the
                           feed is OFF the panel says so and why
    W5  P&L strip          on the Trade screens: today's P&L, open positions, orders waiting for your tap, loss budget used

Routes (register):
    GET  /stock/{symbol}                 the full-page stock view (the modal on / stays as it was)
    GET  /watchlist                      the watchlist as a page
    GET  /api/v2/watchlists              {"lists": [{"id", "name", "symbols"}]}  (a first list is made from the top
                                         BUY signals when there is none)
    POST /api/v2/watchlists              {id?, name, symbols}  save one list (token)
    POST /api/v2/watchlists/{id}/delete  (token)
    GET  /api/v2/quotes?symbols=A,B      LTP, change, live or close, ATIP score and signal per symbol
    GET  /api/v2/depth/{symbol}?levels=  the book (5 or 20 levels) or {"feed": "OFF", "message"}
    GET  /api/v2/pnl-strip               the four W5 figures
    GET  /api/v2/freshness               each EOD dataset's date against the date it should have reached

Nothing here places, confirms or cancels an order: the ticket's only write is the order-rule API the old modal used.
Watchlists live in enterprise_watchlist (tenant "default", user "owner" on the single-user Mac), in the order given.
"""

# No `from __future__ import annotations`: FastAPI must see the real Request class (like dashboard/ux_routes.py).

import json
import logging
import re
import uuid
from datetime import date, datetime

log = logging.getLogger("atip.workspace")

SYMBOL_RE = re.compile(r"^[A-Z0-9&\-_.]{1,20}$")
TENANT, OWNER = "default", "owner"
MAX_LISTS, MAX_SYMBOLS = 10, 100


def _rows(conn, sql, a=()):
    try:
        return [dict(r) if hasattr(r, "keys") else r for r in conn.execute(sql, a).fetchall()]
    except Exception as e:
        log.debug(f"  workspace read: {e}")
        return []


def _sym(s) -> str:
    s = str(s or "").strip().upper()
    if not SYMBOL_RE.match(s):
        raise ValueError(f"not a symbol: {s[:24]!r}")
    return s


# ── W1 watchlists ───────────────────────────────────────────────────────────────────────────────

def _default_symbols(conn) -> list:
    rows = _rows(conn, "SELECT symbol FROM ai_scores WHERE date = (SELECT MAX(date) FROM ai_scores) AND `signal` = 'BUY' "
                       "ORDER BY atip_score DESC LIMIT 8")
    tod = _rows(conn, "SELECT symbol FROM predictions WHERE is_tod = 1 ORDER BY pred_date DESC LIMIT 1")
    out = [r["symbol"] for r in tod] + [r["symbol"] for r in rows]
    return list(dict.fromkeys(out))


def watchlists(conn) -> dict:
    rows = _rows(conn, "SELECT watchlist_id, name, symbols_json, updated_at FROM enterprise_watchlist "
                       "WHERE tenant_id = ? AND user_id = ? ORDER BY created_at, watchlist_id", (TENANT, OWNER))
    lists = []
    for r in rows:
        try:
            syms = [s for s in json.loads(r["symbols_json"] or "[]") if isinstance(s, str)]
        except ValueError:
            syms = []
        lists.append({"id": r["watchlist_id"], "name": r["name"] or "Watchlist", "symbols": syms})
    if not lists:
        lists = [{"id": "", "name": "My watchlist", "symbols": _default_symbols(conn), "draft": True}]
    return {"lists": lists, "max_lists": MAX_LISTS, "max_symbols": MAX_SYMBOLS}


def save_watchlist(conn, b: dict) -> dict:
    name = re.sub(r"\s+", " ", str(b.get("name") or "")).strip()[:40]
    if not name:
        raise ValueError("Give the watchlist a name.")
    syms = []
    for s in b.get("symbols") or []:
        s = _sym(s)
        if s not in syms:
            syms.append(s)
    if len(syms) > MAX_SYMBOLS:
        raise ValueError(f"A watchlist holds up to {MAX_SYMBOLS} stocks.")
    wid = str(b.get("id") or "").strip()
    now = datetime.now().isoformat(timespec="seconds")
    if wid:
        if not _rows(conn, "SELECT 1 FROM enterprise_watchlist WHERE watchlist_id = ? AND tenant_id = ? AND user_id = ?",
                     (wid, TENANT, OWNER)):
            raise LookupError("That watchlist does not exist any more.")
        conn.execute("UPDATE enterprise_watchlist SET name = ?, symbols_json = ?, updated_at = ? WHERE watchlist_id = ?",
                     (name, json.dumps(syms), now, wid))
    else:
        n = _rows(conn, "SELECT COUNT(*) AS n FROM enterprise_watchlist WHERE tenant_id = ? AND user_id = ?", (TENANT, OWNER))
        if n and n[0]["n"] >= MAX_LISTS:
            raise ValueError(f"Up to {MAX_LISTS} watchlists: delete one first.")
        wid = "WL" + uuid.uuid4().hex[:10]
        conn.execute("INSERT INTO enterprise_watchlist (watchlist_id, tenant_id, user_id, name, symbols_json, shared, "
                     "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?)", (wid, TENANT, OWNER, name, json.dumps(syms), now, now))
    conn.commit()
    return {"id": wid, "name": name, "symbols": syms}


def delete_watchlist(conn, wid: str) -> dict:
    cur = conn.execute("DELETE FROM enterprise_watchlist WHERE watchlist_id = ? AND tenant_id = ? AND user_id = ?",
                       (str(wid), TENANT, OWNER))
    conn.commit()
    if not cur.rowcount:
        raise LookupError("That watchlist does not exist.")
    return {"deleted": wid}


def quotes(conn, symbols: list) -> dict:
    """LTP from the live quotes (today, during the session), else the last close; with the ATIP score and signal."""
    syms = []
    for s in symbols or []:
        try:
            s = _sym(s)
        except ValueError:
            continue
        if s not in syms:
            syms.append(s)
    syms = syms[:MAX_SYMBOLS]
    if not syms:
        return {"quotes": {}, "as_of": datetime.now().isoformat(timespec="seconds")}
    mk = ",".join("?" * len(syms))
    today = date.today().isoformat()
    live = {r["symbol"]: r for r in _rows(
        conn, f"SELECT q.symbol, q.ltp, q.prev_close, q.timestamp FROM live_quotes q JOIN (SELECT symbol, MAX(id) AS m "
              f"FROM live_quotes WHERE symbol IN ({mk}) GROUP BY symbol) x ON q.id = x.m", tuple(syms))}
    closes = {}
    for r in _rows(conn, f"SELECT symbol, date, close FROM prices_daily WHERE symbol IN ({mk}) AND date >= "
                         f"(SELECT date(MAX(date), '-10 days') FROM prices_daily) ORDER BY symbol, date", tuple(syms)):
        closes.setdefault(r["symbol"], []).append(r)
    scores = {r["symbol"]: r for r in _rows(
        conn, f"SELECT symbol, atip_score, `signal` FROM ai_scores WHERE date = (SELECT MAX(date) FROM ai_scores) "
              f"AND symbol IN ({mk})", tuple(syms))}
    names = {}
    try:
        from data.companies import load_company_names
        cn = load_company_names()
        names = {s: cn.get(s) for s in syms}
    except Exception:
        pass
    out = {}
    for s in syms:
        cl = closes.get(s) or []
        last = cl[-1] if cl else None
        prev = cl[-2] if len(cl) > 1 else None
        lq = live.get(s)
        is_live = bool(lq and lq.get("ltp") and str(lq.get("timestamp") or "")[:10] == today)
        ltp = float(lq["ltp"]) if is_live else (float(last["close"]) if last else None)
        pc = (float(lq["prev_close"]) if is_live and lq.get("prev_close") else
              (float(last["close"]) if is_live and last else (float(prev["close"]) if prev else None)))
        chg = round((ltp - pc) / pc * 100, 2) if ltp and pc else None
        sc = scores.get(s) or {}
        out[s] = {"symbol": s, "name": names.get(s), "ltp": ltp, "prev_close": pc, "change_pct": chg,
                  "live": is_live, "at": (lq or {}).get("timestamp") if is_live else (last or {}).get("date"),
                  "atip_score": sc.get("atip_score"), "signal": sc.get("signal")}
    return {"quotes": out, "as_of": datetime.now().isoformat(timespec="seconds")}


# ── W4 depth ────────────────────────────────────────────────────────────────────────────────────

def depth(conn, symbol: str, levels: int = 5) -> dict:
    """The live book from the 20-level depth feed (read under its lock, never changed), else why there is none."""
    sym = _sym(symbol)
    levels = 20 if int(levels or 5) > 5 else 5
    try:
        from data import depth20 as D
        st = D.feed_status()
        feed = D.get_depth20()
    except Exception as e:
        return {"symbol": sym, "feed": "OFF", "message": f"Market depth is unavailable here ({type(e).__name__}).", "levels": levels}
    last = (_rows(conn, "SELECT ts, best_bid, best_bid_qty, best_ask, best_ask_qty, spread_bp, dwi FROM depth20_snapshot "
                        "WHERE symbol = ? ORDER BY ts DESC LIMIT 1", (sym,)) or [None])[0]
    if feed is None:
        why = ("The 20-level depth feed is switched off. Turn it on in Settings › Live feeds (it needs the Dhan Data API)."
               if not st.get("enabled") else "The 20-level depth feed is not running right now.")
        return {"symbol": sym, "feed": "OFF", "message": why, "levels": levels, "last_snapshot": last}
    book = None
    try:
        with feed._lock:
            sid = next((k for k, v in feed._sid_to_sym.items() if v == sym), None)
            b = feed._book.get(sid) if sid is not None else None
            if b:
                book = {"bid": [list(x) for x in (b.get("bid") or [])[:levels]],
                        "ask": [list(x) for x in (b.get("ask") or [])[:levels]]}
    except Exception as e:
        log.debug(f"  depth read {sym}: {e}")
    if not book:
        return {"symbol": sym, "feed": "ON", "message": f"{sym} is not on the depth feed's list, or no book has arrived yet.",
                "levels": levels, "last_snapshot": last}
    side = lambda xs: [{"price": p, "qty": q, "orders": o} for p, q, o in xs]
    bq = sum(q for _, q, _ in book["bid"])
    aq = sum(q for _, q, _ in book["ask"])
    return {"symbol": sym, "feed": "ON", "levels": levels, "bids": side(book["bid"]), "asks": side(book["ask"]),
            "total_bid_qty": bq, "total_ask_qty": aq,
            "imbalance": round((bq - aq) / (bq + aq), 3) if bq + aq else None,
            "at": feed.last_at.isoformat(timespec="seconds") if getattr(feed, "last_at", None) else None}


# ── W5 the P&L strip ────────────────────────────────────────────────────────────────────────────

def pnl_strip(conn) -> dict:
    from accounting.periods import periods
    p = periods(conn)
    try:
        from orders.environment import broker_env
        env = broker_env()
    except Exception:
        env = "PAPER"
    try:
        from portfolio.pnl import positions
        opens = [x for x in positions(conn, "LIVE" if env == "LIVE" else "PAPER") if x.get("qty")]
    except Exception:
        opens = []
    waiting = (_rows(conn, "SELECT COUNT(*) AS n FROM order_rules WHERE status = 'PENDING_CONFIRMATION'") or [{"n": 0}])[0]["n"]
    cap = used = None
    try:
        from orders import risk
        cap = risk.limits().get("max_daily_loss_value")
        from portfolio.pnl import risk_state
        st = risk_state(conn, "LIVE" if env == "LIVE" else "PAPER")
        used = max(0.0, -(st.get("day_pnl") or 0.0)) if st.get("day_pnl") is not None else None
    except Exception:
        pass
    return {"env": env, "today": p["today"], "open_positions": len(opens), "waiting": int(waiting or 0),
            "loss_budget": {"cap": cap, "used": used,
                            "used_pct": round(used / cap * 100, 1) if cap and used is not None else None},
            "as_of": p["as_of"]}


# ── pages ───────────────────────────────────────────────────────────────────────────────────────

LWC = "/static/vendor/lightweight-charts-5.0.9/lightweight-charts.standalone.production.js"


def stock_page(conn, symbol: str, root: str = "", app: bool = False) -> str:
    from dashboard import fmt as F
    from dashboard.shell import page, _e
    sym = _sym(symbol)
    px = _rows(conn, "SELECT date, close FROM prices_daily WHERE symbol = ? ORDER BY date DESC LIMIT 1", (sym,))
    if not px:
        raise LookupError(f"No price history for {sym}. Check the symbol, or wait for the evening prices job.")
    sc = (_rows(conn, "SELECT date, atip_score, `signal` FROM ai_scores WHERE symbol = ? ORDER BY date DESC LIMIT 1", (sym,))
          or [{}])[0]
    name = None
    try:
        from data.companies import load_company_names
        name = load_company_names().get(sym)
    except Exception:
        pass
    sig = sc.get("signal") or ""
    tone = "good" if sig == "BUY" else "bad" if sig == "SELL" else ""
    head = (f'<div class="ax-sv-head" data-sym="{_e(sym)}"><div><div class="ax-sv-sym">'
            f'<span class="ax-st {tone}">{_e(sig or "no signal")}</span> <span class="ax-chip" title="ATIP score, {_e(sc.get("date") or "")} close">'
            f'ATIP {_e(F.fmt_score(sc.get("atip_score")))}</span></div></div>'
            f'<div class="ax-sv-px"><span class="ax-num ax-sv-ltp" data-ax-ltp>{_e(F.fmt_price(px[0]["close"]))}</span> '
            f'<span data-ax-chg></span><div class="ax-small ax-muted" data-ax-src>close {_e(px[0]["date"])}</div></div>'
            f'<div class="ax-sv-act"><button class="ax-btn primary" type="button" data-ax-trade="BUY" data-sym="{_e(sym)}" aria-keyshortcuts="B">Buy <kbd>B</kbd></button>'
            f'<button class="ax-btn danger" type="button" data-ax-trade="SELL" data-sym="{_e(sym)}" aria-keyshortcuts="S">Sell <kbd>S</kbd></button>'
            f'<button class="ax-btn" type="button" data-ax-wl-add="{_e(sym)}">☆ Watchlist</button></div></div>')
    body = (head + '<div class="ax-sv-stats" id="ax-sv-stats" aria-live="polite"></div>'
            '<div class="ax-sv-grid"><section class="ax-card ax-sv-chartcard" aria-label="Price chart">'
            '<div class="ax-sv-bar" role="toolbar" aria-label="Chart controls">'
            '<div class="ax-seg" role="group" aria-label="Timeframe">' + "".join(
                f'<button type="button" data-tf="{k}" aria-pressed="{"true" if k == "6M" else "false"}">{k}</button>'
                for k in ("1D", "5D", "1M", "3M", "6M", "1Y", "3Y", "All")) + '</div>'
            '<div class="ax-seg" role="group" aria-label="Chart type"><button type="button" data-kind="candle" aria-pressed="true">Candles</button>'
            '<button type="button" data-kind="line" aria-pressed="false">Line</button></div>'
            '<div class="ax-seg" role="group" aria-label="Overlays and panes">' + "".join(
                f'<button type="button" data-ind="{k}" aria-pressed="{"true" if k in ("ma", "vol") else "false"}">{l}</button>'
                for k, l in (("ma", "MA 20/50/200"), ("bb", "BB 20,2"), ("vol", "Volume"), ("rsi", "RSI 14"), ("macd", "MACD"),
                             ("marks", "ATIP signals"))) + '</div></div>'
            '<div class="ax-sv-legend ax-num" id="ax-sv-legend" aria-live="off"></div>'
            '<div id="ax-sv-chart" class="ax-sv-chart" aria-label="Price chart"></div>'
            '<p class="ax-small ax-muted" id="ax-sv-note"></p></section>'
            '<aside class="ax-card ax-sv-depth" aria-label="Market depth"><div class="ax-cardhead"><h2>Market depth</h2>'
            '<div class="ax-seg" role="group" aria-label="Levels"><button type="button" data-lv="5" aria-pressed="true">5</button>'
            '<button type="button" data-lv="20" aria-pressed="false">20</button></div></div><div id="ax-depth" aria-live="polite"></div></aside></div>'
            '<section class="ax-card" style="margin-top:14px"><h2>ATIP signals for this stock</h2><div id="ax-sv-signals"></div></section>'
            f'<script src="{_e(root)}{LWC}" defer></script>')
    return page(f"{sym}", body, f"/stock/{sym}", job=name or "", root=root, app=app,
                actions=f'<a class="ax-btn" href="/?stock={_e(sym)}">Classic view</a>',
                extra_head=f'<meta name="atip-stock" content="{_e(sym)}">')


def watchlist_page(conn, root: str = "", app: bool = False) -> str:
    from dashboard.shell import page
    body = ('<div id="ax-wl-page" class="ax-wl ax-wl-pagewrap" data-ax-wl-page></div>'
            '<p class="ax-small ax-muted">Prices are the live price in market hours and the last close otherwise; '
            'the ATIP score and signal are from the last evening\'s scoring. Tap a stock for its chart, or B / S to trade.</p>')
    return page("Watchlist", body, "/watchlist", job="Your stocks at a glance: price, change and ATIP's view.", root=root, app=app)


def register(app, guard, call, html_page, ctx, Req) -> None:
    """Called by dashboard/ux_routes.py with its helpers (call: a function of conn -> JSON response)."""
    from fastapi.responses import HTMLResponse
    from starlette.concurrency import run_in_threadpool

    async def body(request) -> dict:
        try:
            b = await request.json()
        except Exception:
            return {}
        return b if isinstance(b, dict) else {}

    @app.get("/stock/{symbol}", response_class=HTMLResponse)
    async def ux_stock(symbol: str, request: Req):
        c = ctx(request)
        try:
            _sym(symbol)
        except ValueError as e:
            from dashboard import shell
            return HTMLResponse(shell.page("Not found", f'<div class="ax-empty">{shell._e(e)}</div>', "/"), status_code=404)
        return await run_in_threadpool(html_page, lambda conn: stock_page(conn, symbol, **c))

    @app.get("/watchlist", response_class=HTMLResponse)
    async def ux_watchlist(request: Req):
        c = ctx(request)
        return await run_in_threadpool(html_page, lambda conn: watchlist_page(conn, **c))

    @app.get("/api/v2/watchlists")
    async def api_v2_watchlists():
        return await run_in_threadpool(call, watchlists)

    @app.post("/api/v2/watchlists", dependencies=guard)
    async def api_v2_watchlist_save(request: Req):
        b = await body(request)
        return await run_in_threadpool(call, lambda c: save_watchlist(c, b))

    @app.post("/api/v2/watchlists/{wid}/delete", dependencies=guard)
    async def api_v2_watchlist_delete(wid: str):
        return await run_in_threadpool(call, lambda c: delete_watchlist(c, wid))

    @app.get("/api/v2/quotes")
    async def api_v2_quotes(symbols: str = ""):
        return await run_in_threadpool(call, lambda c: quotes(c, symbols.split(",")))

    @app.get("/api/v2/depth/{symbol}")
    async def api_v2_depth(symbol: str, levels: int = 5):
        return await run_in_threadpool(call, lambda c: depth(c, symbol, levels))

    @app.get("/api/v2/pnl-strip")
    async def api_v2_pnl_strip():
        return await run_in_threadpool(call, pnl_strip)

    @app.get("/api/v2/freshness")
    async def api_v2_freshness():
        from dashboard.ux_pages import freshness
        return await run_in_threadpool(call, lambda c: {"datasets": freshness(c)})

    @app.get("/api/v2/periods")
    async def api_v2_periods():
        from accounting.periods import reconcile
        return await run_in_threadpool(call, reconcile)
