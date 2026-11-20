"""
/ab: the session arms' PAPER A/B test, the regime gate and the DUMMY- resting orders (KGF-09/11/13/16, 16 Nov 2026).
Every time on it is IST.

    GET  /ab                               the page: gate banner, the ten arms, dummy GTT / forever records, weekly A/B
    GET  /api/arms                         every session's arm, its words and its gate state; breadth; pre-open gate
    GET  /api/breadth[?days=60]            breadth20 / breadth50 history (scores/breadth.py)
    GET  /api/algo-orders[?active=1]       the DUMMY- records (orders/algo_gtt.py), newest first      read-only
    POST /api/algo-orders/gtt-state        {"symbol", "quantity"?, "buy_price"?, "stoploss"?}        read-only
    POST /api/algo-orders/live-gtt         {"symbol", "quantity", "buy_price", "stoploss"?, "confirm_live": true}
                                           a DUMMY- record only; NOTHING is sent to any broker        trading Mac only
    GET  /api/ab-report[?week=YYYY-MM-DD]  the stored weekly A/B report (portfolio/ab_report.py)

The POSTs need the X-ATIP-Token header like every other; the view-only server refuses live-gtt (ops/role.py).
"""

import json

from dashboard import ux_kit as K

e = K.e


def gate_banner(conn) -> str:
    """The regime gate in one line for the Sessions page, Home and the apps: open / closed per metric, with the value."""
    try:
        from orders import arms
        from scores.breadth import latest
        b = latest(conn)
        al = arms.all_arms(conn)
    except Exception as ex:
        return f'<div class="ax-banner warn ax-small" role="status">Regime gate unavailable: {e(ex)}</div>'
    if not al:
        return ""
    if not b:
        return ('<div class="ax-banner warn ax-small" role="status" data-ax-gate="none"><span>Regime gate: no breadth stored '
                'yet, so every gated arm waits (python -m scores.breadth).</span></div>')
    gated = {}
    for sid, a in al.items():
        g = a.get("regime_gate")
        if g:
            gated.setdefault((g["metric"], float(g.get("min", 50))), []).append(sid)
    parts, closed = [], False
    for (m, lo), ids in sorted(gated.items()):
        v = float(b.get(m) or 0)
        ok = v >= lo
        closed = closed or not ok
        parts.append(f'{m} {v:.1f}% {"≥" if ok else "<"} {lo:g}% → <b>{"OPEN" if ok else "CLOSED"}</b> for '
                     f'{", ".join(sorted(ids, key=lambda i: int(i.lstrip("S"))))}')
    ungated = sorted((sid for sid, a in al.items() if not a.get("regime_gate")), key=lambda i: int(i.lstrip("S")))
    if ungated:
        parts.append(f'no gate: {", ".join(ungated)}')
    return (f'<div class="ax-banner {"warn" if closed else "info"} ax-small" role="status" data-ax-gate="{"closed" if closed else "open"}">'
            f'<span>Regime gate (breadth on {e(b["date"])}, read at the open): ' + " · ".join(parts) +
            ' · exits always run. Strategy today: rank-fill on the ATIP score and the Kite retest, not AI (the news AI '
            'is off until it shows a lift). <a href="/ab">Arms →</a></span></div>')


def _arms_table(conn) -> str:
    from orders import arms
    rows = []
    for a in arms.board(conn):
        g = a["gate"] or {}
        rows.append({"id": a["id"], "name": a["name"], "entry": (a["arm"] or {}).get("entry") or "—",
                     "rule": a["describe"] or "(no arm: the session's own columns)",
                     "gate": ("—" if not g.get("metric") else f'{"OPEN" if g["open"] else "CLOSED"} '
                              f'({g["metric"]} {g["value"] if g.get("value") is not None else "?"} vs {g["min"]:g})')})
    return K.table("ab-arms", [("id", "Session", "text"), ("name", "Arm", "text"), ("entry", "Entry", "text"),
                               ("rule", "Rules (new entries; open trades keep their legs)", "text"),
                               ("gate", "Gate today", "text")], rows, name="arms", empty="No session has an arm.")


def _dummy_table(conn) -> str:
    from orders import algo_gtt
    rows = []
    for d in algo_gtt.listing(conn, 60):
        rows.append({"id": d["id"], "kind": "Resting buy (GTT single / Forever SINGLE)" if d["kind"] == "BUY"
                     else "Stop + target (GTT two-leg / Forever OCO)", "symbol": d["symbol"], "session": d.get("session_id") or "—",
                     "qty": d.get("quantity"), "trigger": d.get("trigger_price"), "stop": d.get("stop_trigger"),
                     "target": d.get("target_trigger"), "status": d["status"], "valid": d.get("valid_until") or "—",
                     "fill": d.get("fill_price"), "msg": d.get("message") or ""})
    return K.table("ab-dummy", [("id", "Record", "text"), ("kind", "Simulates", "text"), ("symbol", "Symbol", "text"),
                                ("session", "Session", "text"), ("qty", "Qty", "int"), ("trigger", "Buy trigger", "money"),
                                ("stop", "Stop", "money"), ("target", "Target", "money"), ("status", "Status", "text"),
                                ("valid", "Valid until", "text"), ("fill", "Filled at", "money"), ("msg", "Note", "text")],
                   rows, name="dummy-gtt", empty="No DUMMY- record yet: the retest arms write one when a setup is free.")


def _report_html(conn) -> str:
    try:
        from portfolio.ab_report import latest
        rep = latest(conn)
    except Exception as ex:
        return f'<div class="ax-empty">Weekly A/B report unavailable: {e(ex)}</div>'
    if not rep:
        return ('<div class="ax-empty">No weekly A/B report yet: the first is built on Sunday 22 Nov 2026 at 18:00 IST '
                '(python -m portfolio.ab_report builds one now).</div>')
    rows = rep["arms"]
    cols = [("session", "Session", "text"), ("arm", "Arm", "text"), ("trades", "Closed", "int"), ("win_pct", "Win %", "pct"),
            ("avg_net", "Avg net ₹", "pnl"), ("avg_pct", "Avg %", "pct"), ("pf", "PF", "text"),
            ("max_dd", "Max DD ₹", "pnl"), ("gate_days", "Gate-blocked days", "int"), ("skipped", "Skipped picks", "int"),
            ("skipped_sector", "Sector-cap skips", "int"), ("skipped_cooldown", "Cooldown skips", "int"),
            ("reentries", "Re-entries ≤60 min after a stop", "int"),
            ("held_from_before", "Held from before the arm", "text"), ("verdict", "Verdict", "text")]
    return (f'<p class="ax-small ax-muted">Week to {e(rep["week_end"])} (built {e(rep["built_at"])} IST). Since the arms '
            f'started {e(rep["since"])}. Promote or retire an arm only after {rep["min_trades"]} closed trades and '
            f'{rep["min_weeks"]} weeks, whichever is later (next review {e(rep["next_review"])}).</p>' +
            K.table("ab-week", cols, rows, name="ab-week"))


def page_html(conn, root: str = "", app: bool = False) -> str:
    from dashboard.shell import page
    from orders import arms
    try:
        from scores.breadth import history
        hist = history(conn, 20)
    except Exception:
        hist = []
    bh = ('<p class="ax-small ax-muted">Breadth (scored universe above its 20- / 50-day average), latest first: ' +
          " · ".join(f'{e(h["date"][5:])} {h["breadth20"]:.0f}/{h["breadth50"]:.0f}' for h in hist[:10]) + "</p>") if hist else ""
    note = ('<div class="ax-card"><h2>What this page is</h2><p>Ten PAPER sessions, ten arms. S5 is the control: exactly the '
            'rules of 15 Nov (rank-fill on the ATIP score, stop 2 %, trail 1 % from +3 %, no gate). Every other arm '
            'changes one thing the 15 Nov study found: a breadth gate, a wider or ATR stop with the rupee risk held at '
            'about ₹625, or the Kite breakout-retest entry (a resting buy at the breakout candle\'s low). '
            '<b>Rank-fill is the strategy today, not AI</b>: ATIP produced 0 BUY signals on most days, so the picks are '
            'the best-ranked HOLD stocks; the news AI stays off until news_impact_eval shows a lift.</p>'
            '<p class="ax-small ax-muted">DUMMY- records simulate the broker-side orders a LIVE switch would use (Dhan '
            'Forever SINGLE / OCO, Kite GTT single / two-leg). <b>No order is sent to any broker.</b></p></div>')
    body = (gate_banner(conn) + note + bh + '<h2 class="ax-h2">Arms</h2>' + _arms_table(conn) +
            '<h2 class="ax-h2">Weekly A/B report</h2>' + _report_html(conn) +
            '<h2 class="ax-h2">DUMMY broker-side orders</h2>' + _dummy_table(conn))
    return page("Session arms (A/B)", body, "/ab", root=root, app=app,
                job="Which entry and exit rules work: ten PAPER arms against the S5 control, behind the breadth gate (IST)")


def register(app, guard, Req, get_connection, json_safe):
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import HTMLResponse, JSONResponse

    def ctx(request):
        ua = request.headers.get("user-agent", "")
        return {"root": request.scope.get("root_path") or "",
                "app": "ATIPApp/" in ua or request.query_params.get("app") == "1"}

    def with_conn(fn):
        conn = get_connection()
        try:
            return fn(conn)
        finally:
            conn.close()

    @app.get("/ab", response_class=HTMLResponse)
    async def ab_page(request: Req):
        c = ctx(request)
        return HTMLResponse(await run_in_threadpool(with_conn, lambda conn: page_html(conn, **c)))

    @app.get("/api/arms")
    async def arms_api():
        def go(conn):
            from orders import arms
            from scores.breadth import latest
            from orders.autopick import _health
            return {"arms": arms.board(conn), "breadth": latest(conn), "preopen": _health(conn),
                    "banner": gate_banner(conn)}
        return JSONResponse(json_safe(await run_in_threadpool(with_conn, go)))

    @app.get("/api/breadth")
    async def breadth_api(days: int = 60):
        from scores.breadth import history
        return JSONResponse(json_safe({"breadth": await run_in_threadpool(with_conn, lambda conn: history(conn, days))}))

    @app.get("/api/algo-orders")
    async def algo_orders(active: int = 0, limit: int = 100):
        from orders.algo_gtt import listing
        return JSONResponse(json_safe({"records": await run_in_threadpool(
            with_conn, lambda conn: listing(conn, limit, bool(active))), "note": "DUMMY- records only; no broker order"}))

    async def _body(request):
        try:
            b = await request.json()
            return b if isinstance(b, dict) else {}
        except Exception:
            return {}

    @app.post("/api/algo-orders/gtt-state", dependencies=guard)
    async def gtt_state(request: Req):
        from orders.algo_gtt import gtt_state as st
        b = await _body(request)
        if not b.get("symbol"):
            return JSONResponse({"error": "symbol is required"}, status_code=400)
        out = await run_in_threadpool(with_conn, lambda conn: st(conn, b["symbol"], b.get("quantity"),
                                                                 b.get("buy_price"), b.get("stoploss")))
        return JSONResponse(json_safe(out))

    @app.post("/api/algo-orders/live-gtt", dependencies=guard)
    async def live_gtt(request: Req):
        from orders.algo_gtt import place_dummy_buy, setup_for
        b = await _body(request)
        if not b.get("symbol"):
            return JSONResponse({"error": "symbol is required"}, status_code=400)

        def go(conn):
            buy, stop = b.get("buy_price"), b.get("stoploss")
            setup = setup_for(conn, str(b["symbol"]).upper())
            if not buy and setup:
                buy, stop = setup.get("buy_price"), setup.get("stop_price")
            # confirm_live must be the JSON boolean true: a string "true", 1 or a missing key is refused
            return place_dummy_buy(conn, b["symbol"], b.get("quantity"), buy or 0, stop,
                                   confirm_live=b.get("confirm_live") is True, setup=setup)
        try:
            out = await run_in_threadpool(with_conn, go)
        except RuntimeError as ex:                # ops/role.py ViewerRefused
            return JSONResponse({"error": str(ex)}, status_code=403)
        code = 200 if out["buy_status"] in ("success", "already_active") else (
            409 if out["buy_status"] in ("blocked", "waiting_for_ltp") else 400)
        return JSONResponse(json_safe(out), status_code=code)

    @app.get("/api/ab-report")
    async def ab_report(week: str | None = None):
        from portfolio.ab_report import latest
        rep = await run_in_threadpool(with_conn, lambda conn: latest(conn, week))
        return JSONResponse(json_safe(rep) if rep else {"error": "no weekly A/B report stored yet"},
                            status_code=200 if rep else 404)
