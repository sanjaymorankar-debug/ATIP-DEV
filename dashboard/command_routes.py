"""
UXR-01 Command Center: one screen that answers "is it safe, what needs me, what is running".

    GET  /command                         the page (dashboard/command_page.py)
    GET  /api/command/summary             market / feed / broker / halt state, risk budget,
                                          top EOD signals with their live price, pending
                                          confirmations, sessions, recent order activity
    POST /api/risk/halt      {reason}     kill switch ON  (orders.risk.halt) + every RUNNING
                                          session shown as PAUSED
    POST /api/risk/resume    {confirm:"RESUME"}   kill switch OFF (orders.risk.resume);
                                          sessions are NOT restarted -- the owner resumes each
    GET  /api/sessions                    the ten sessions with per-session counts
    POST /api/command/auto-send  {on, ack}   the owner's switch: AUTO sessions send without the
                                          confirmation tap (orders/sessions.py set_auto_send)
    GET  /api/sessions/events?sid=       append-only change log
    POST /api/sessions/{sid}/mode   {mode, ack}
    POST /api/sessions/{sid}/status {status, reason}
    POST /api/sessions/{sid}        {name?, strategy?, universe?, max_trades?, max_loss_value?, capital_cap?,
                                     target_pct?, stop_pct?, trail_pct?, hold_days?, min_buy_value?}
    POST /api/sessions/{sid}/link   {rule_id}
    POST /api/sessions/pools        {capital, total?, target_pct?, stop_pct?, trail_pct?, hold_days?, min_buy_value?,
                                     max_occurrences?, auto_pick?, ids?}
                                    a cash pool per session, books from now (orders/sessions.py setup_pools):
                                    total // capital sessions when total is given; "sid" with the same
                                    fields restarts one session's pool (start_pool)
    POST /api/sessions/autopick     {hold_min_score: 60 | null}   auto-pick's HOLD fallback (orders/autopick.py):
                                    when no BUY signal is free, HOLD stocks scoring at least this; null = off

Every POST needs the X-ATIP-Token header (dashboard/security.py). Nothing here places, changes
or cancels an order: halt/resume call the existing kill switch, which every order path already
checks. The trigger engine holds a session's entry rules while it is paused, MANUAL or over a
limit (orders/sessions.py gate); "sessions.auto_bypass" says whether the owner's auto send switch
lets AUTO sessions skip the tap.
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
import math
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

FEED_STALE_SECONDS = 60
IST = ZoneInfo("Asia/Kolkata")


def _finite(obj):
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite(v) for v in obj]
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes)):
        try:
            obj = obj.item()
        except (TypeError, ValueError):
            return obj
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def _rows(conn, sql, params=()):
    try:
        return [dict(r) if hasattr(r, "keys") else r for r in conn.execute(sql, params).fetchall()]
    except Exception:
        return []


def _market_open() -> bool:
    try:
        from pipeline.scheduler import is_market_hours
        return bool(is_market_hours())
    except Exception:
        return False


def _feed(conn, market_open: bool) -> dict:
    row = _rows(conn, "SELECT MAX(timestamp) AS ts FROM live_quotes")
    ts = row[0]["ts"] if row else None
    age = None
    if ts:
        try:
            age = round((datetime.now() - datetime.fromisoformat(str(ts).replace("T", " "))).total_seconds())
        except ValueError:
            age = None
    stale = bool(market_open and (age is None or age > FEED_STALE_SECONDS))
    return {"last_tick": ts, "age_seconds": age, "stale": stale, "cutoff_seconds": FEED_STALE_SECONDS}


def _broker() -> dict:
    try:
        from data.dhan import load_dhan_config
        cfg = load_dhan_config()
        configured = bool(cfg.get("dhan_client_id") and cfg.get("dhan_access_token"))
    except Exception:
        configured = False
    try:
        from orders.environment import broker_env
        env = broker_env()
    except Exception:
        env = "PAPER"
    return {"credentials_configured": configured, "broker_env": env, "is_live": env == "LIVE"}


def _signals(conn, limit=8) -> dict:
    d = _rows(conn, "SELECT MAX(date) AS d FROM ai_scores")
    sig_date = str(d[0]["d"]) if d and d[0]["d"] else None
    if not sig_date:
        return {"date": None, "rows": []}
    rows = _rows(conn, "SELECT symbol, `signal`, atip_score, acs, cri, zpi, mri, vpi, top_factor_1 "
                       "FROM ai_scores WHERE date = ? AND `signal` IN ('BUY','SELL') "
                       "ORDER BY atip_score DESC LIMIT ?", (sig_date, limit))
    syms = [r["symbol"] for r in rows]
    closes, live = {}, {}
    if syms:
        marks = ",".join("?" * len(syms))
        for r in _rows(conn, f"SELECT symbol, close FROM prices_daily WHERE date = ? AND symbol IN ({marks})",
                       (sig_date, *syms)):
            closes[r["symbol"]] = r["close"]
        for r in _rows(conn, f"SELECT symbol, ltp, timestamp FROM live_quotes WHERE symbol IN ({marks}) "
                             f"ORDER BY timestamp", tuple(syms)):
            live[r["symbol"]] = r["ltp"]                # last one per symbol wins
    for r in rows:
        r["signal_price"] = closes.get(r["symbol"])
        r["live_price"] = live.get(r["symbol"])
        sp, lp = r["signal_price"], r["live_price"]
        r["gap_pct"] = round((lp - sp) / sp * 100, 2) if sp and lp else None
    return {"date": sig_date, "rows": rows}


def _market_health(conn) -> dict:
    r = _rows(conn, "SELECT date, mh_score, regime FROM market_health ORDER BY date DESC LIMIT 1")
    return r[0] if r else {}


def _risk(conn, env) -> dict:
    from orders import risk
    lim = risk.limits()
    try:
        orders_today = risk._orders_today(conn)
    except Exception:
        orders_today = None
    try:
        from portfolio.pnl import risk_state
        st = risk_state(conn, "LIVE" if env == "LIVE" else "PAPER")
    except Exception as e:
        st = {"day_pnl": None, "drawdown_pct": None, "error": str(e)}
    h, why = risk.halted()
    return {"halted": h, "halt_reason": why or None, "limits": lim, "orders_today": orders_today,
            "day_pnl": st.get("day_pnl"), "drawdown_pct": st.get("drawdown_pct")}


def summary(conn) -> dict:
    from orders import rules, sessions
    mo = _market_open()
    broker = _broker()
    try:
        pending = rules.list_rules(status=rules.PENDING_CONFIRMATION)
    except Exception:
        pending = []
    recent = _rows(conn, "SELECT id, symbol, side, status, role, quantity_value, resolved_trigger_price, "
                         "execution_price, dhan_order_id, execution_error, updated_at "
                         "FROM order_rules ORDER BY updated_at DESC LIMIT 8")
    return {"now": datetime.now().isoformat(timespec="seconds"), "market_open": mo,
            "feed": _feed(conn, mo), "broker": broker, "risk": _risk(conn, broker["broker_env"]),
            "market_health": _market_health(conn), "signals": _signals(conn),
            "pending": pending, "recent": recent, "sessions": sessions.list_sessions(conn)}


def register(app, guard, Req, get_connection, json_safe):
    def call(fn):
        conn = get_connection()
        try:
            return JSONResponse(_finite(json_safe(fn(conn))))
        except LookupError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except (ValueError, TypeError, KeyError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        finally:
            conn.close()

    async def body(request) -> dict:
        try:
            b = await request.json()
        except Exception:
            return {}
        return b if isinstance(b, dict) else {}

    @app.get("/command", response_class=HTMLResponse)
    async def command_page():
        from dashboard.command_page import render
        return HTMLResponse(render())

    @app.get("/api/command/summary")
    async def api_command_summary():
        return await run_in_threadpool(call, summary)

    @app.post("/api/command/auto-send", dependencies=guard)
    async def api_command_auto_send(request: Req):
        b = await body(request)
        from orders.sessions import set_auto_send
        return await run_in_threadpool(call, lambda c: set_auto_send(c, bool(b.get("on")), bool(b.get("ack")),
                                                                      "from Command Center"))

    @app.post("/api/risk/halt", dependencies=guard)
    async def api_risk_halt(request: Req):
        b = await body(request)
        reason = str(b.get("reason") or "").strip()[:200] or f"halted from /command {datetime.now(IST):%Y-%m-%d %H:%M} IST"

        def go(conn):
            from orders import risk, sessions
            risk.halt(reason)
            paused = sessions.pause_all(conn, reason)
            h, why = risk.halted()
            return {"halted": h, "reason": why, "sessions_paused": paused}
        return await run_in_threadpool(call, go)

    @app.post("/api/risk/resume", dependencies=guard)
    async def api_risk_resume(request: Req):
        b = await body(request)
        if str(b.get("confirm") or "") != "RESUME":
            return JSONResponse({"error": 'send {"confirm": "RESUME"} to lift the halt'}, status_code=400)

        def go(conn):
            from orders import risk
            was = risk.resume()
            h, why = risk.halted()
            # config.json "trading_halted": true still halts after the flag file is gone
            return {"was_halted": was, "halted": h, "reason": why or None,
                    "note": "sessions stay paused -- resume each one you want running"}
        return await run_in_threadpool(call, go)

    @app.get("/api/sessions")
    async def api_sessions():
        from orders.sessions import list_sessions
        return await run_in_threadpool(call, list_sessions)

    @app.get("/api/sessions/events")
    async def api_session_events(sid: str = None, limit: int = 50):
        from orders.sessions import events
        return await run_in_threadpool(call, lambda c: events(c, sid, limit))

    @app.post("/api/sessions/{sid}/mode", dependencies=guard)
    async def api_session_mode(sid: str, request: Req):
        b = await body(request)
        from orders.sessions import set_mode
        return await run_in_threadpool(call, lambda c: set_mode(c, sid, b.get("mode"), bool(b.get("ack"))))

    @app.post("/api/sessions/{sid}/status", dependencies=guard)
    async def api_session_status(sid: str, request: Req):
        b = await body(request)
        from orders.sessions import set_status
        return await run_in_threadpool(call, lambda c: set_status(c, sid, b.get("status"), str(b.get("reason") or "")))

    @app.post("/api/sessions/{sid}/link", dependencies=guard)
    async def api_session_link(sid: str, request: Req):
        b = await body(request)
        from orders.sessions import link_rule
        return await run_in_threadpool(call, lambda c: link_rule(c, sid, str(b.get("rule_id") or "")))

    @app.post("/api/sessions/autopick", dependencies=guard)
    async def api_session_autopick(request: Req):
        b = await body(request)
        from orders.autopick import set_hold_min_score
        return await run_in_threadpool(call, lambda c: set_hold_min_score(c, b.get("hold_min_score"),
                                                                          str(b.get("reason") or "")))

    @app.post("/api/sessions/pools", dependencies=guard)
    async def api_session_pools(request: Req):
        b = await body(request)
        from orders.sessions import setup_pools, start_pool
        rules = {k: b.get(k) for k in ("target_pct", "stop_pct", "min_buy_value", "max_occurrences",
                                       "trail_pct", "hold_days")}
        if b.get("sid"):
            return await run_in_threadpool(call, lambda c: start_pool(c, str(b["sid"]).upper(), b.get("capital"), **rules))
        ids = [str(i).upper() for i in b.get("ids") or []] or None
        return await run_in_threadpool(call, lambda c: setup_pools(c, b.get("capital"), ids, total=b.get("total"),
                                                                   auto_pick=b.get("auto_pick"), **rules))

    @app.post("/api/sessions/{sid}", dependencies=guard)
    async def api_session_update(sid: str, request: Req):
        b = await body(request)
        from orders.sessions import update_session
        return await run_in_threadpool(call, lambda c: update_session(c, sid, b))
