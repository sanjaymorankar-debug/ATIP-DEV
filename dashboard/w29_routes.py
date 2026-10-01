"""
W29 execution API. JSON; 400 invalid; 404 unknown; token on state changes.
Authz (enterprise/authz.py): /api/oms and /api/execution POST -> execution:trade, GET ->
execution:read; /api/pnl -> portfolio:read; /api/zerodha, /api/audit/export -> system:operate
(POST) / dashboard:read (GET). Nothing here can place a LIVE order.

    POST /api/oms/orders/{oid}/modify           {quantity?, limit_price?, trigger_price?, order_type?}   EX-08
    POST /api/oms/orders/{oid}/protective-stop  {stop_price?, limit_offset_pct?}                         EX-02
    POST /api/execution/paper-match             fill resting paper orders that have crossed              EX-02
    GET  /api/execution/reconciliation          ?limit=5                                                 BR-05
    POST /api/execution/reconciliation/run
    GET  /api/execution/broker-health           latest + 24 h history                                    BR-06
    POST /api/execution/broker-health/check     {network?: true}
    GET  /api/execution/analytics               ?days=30 | start&end                                     EX-10
    GET  /api/execution/slippage                ?days=30&strategy_id=                                    EX-09
    GET  /api/execution/sandbox-check           read-only steps                                          BR-08
    GET  /api/zerodha/status | /api/zerodha/login-url ; GET /zerodha/callback?request_token=           BR-02
    GET  /api/pnl/live ; GET /api/pnl/live/series ?day=                                                  MON-04
    GET  /api/audit/export-status ; POST /api/audit/export {source?}                                     SEC-03
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool


def register(app, guard, Req, get_connection, json_safe):
    from execution.errors import ExecutionError

    def call(fn):
        conn = get_connection()
        try:
            return JSONResponse(json_safe(fn(conn)))
        except LookupError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except (ExecutionError, ValueError, TypeError, KeyError, RuntimeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        finally:
            conn.close()

    async def run(fn):
        return await run_in_threadpool(call, fn)

    async def body(request):
        try:
            b = await request.json()
        except Exception:
            return {}
        return b if isinstance(b, dict) else {}

    def _f(v):
        return None if v in (None, "") else float(v)

    # ── orders ──
    @app.post("/api/oms/orders/{oid}/modify", dependencies=guard)
    async def oms_modify(oid: str, request: Req):
        b = await body(request)
        from execution.order_manager import modify_order
        return await run(lambda c: modify_order(
            c, oid, int(b["quantity"]) if b.get("quantity") not in (None, "") else None, _f(b.get("limit_price")),
            _f(b.get("trigger_price")), b.get("order_type"), actor="dashboard"))

    @app.post("/api/oms/orders/{oid}/protective-stop", dependencies=guard)
    async def oms_stop(oid: str, request: Req):
        b = await body(request)
        from execution.order_manager import place_protective_stop
        return await run(lambda c: place_protective_stop(c, oid, _f(b.get("stop_price")),
                                                         _f(b.get("limit_offset_pct")), actor="dashboard"))

    @app.post("/api/execution/paper-match", dependencies=guard)
    async def paper_match():
        from execution.paper_matching import run_matching
        return JSONResponse(json_safe(await run_in_threadpool(run_matching)))

    # ── reconciliation / health ──
    @app.get("/api/execution/reconciliation")
    async def recon(limit: int = 5):
        from execution.reconcile import latest
        return await run(lambda c: latest(c, max(1, min(int(limit), 50))))

    @app.post("/api/execution/reconciliation/run", dependencies=guard)
    async def recon_run():
        from execution.reconcile import run_reconciliation
        return JSONResponse(json_safe(await run_in_threadpool(run_reconciliation)))

    @app.get("/api/execution/broker-health")
    async def bh():
        from execution.broker_health import history, latest
        return await run(lambda c: {"latest": latest(c), "history": history(c, 24)})

    @app.post("/api/execution/broker-health/check", dependencies=guard)
    async def bh_check(request: Req):
        b = await body(request)
        from execution.broker_health import check
        return JSONResponse(json_safe(await run_in_threadpool(lambda: check(network=b.get("network", True) is not False))))

    # ── analytics ──
    @app.get("/api/execution/analytics")
    async def ex_analytics(days: int = 30, start: str = None, end: str = None):
        from execution.analytics import analytics
        return await run(lambda c: analytics(c, start, end, max(1, min(int(days), 3650))))

    @app.get("/api/execution/slippage")
    async def ex_slippage(days: int = 30, strategy_id: str = None):
        from execution.analytics import slippage
        return await run(lambda c: slippage(c, None, None, strategy_id, max(1, min(int(days), 3650))))

    @app.get("/api/execution/sandbox-check")
    async def sandbox_check():
        from orders.sandbox_check import run as _run
        return JSONResponse(json_safe(await run_in_threadpool(lambda: _run(False))))

    # ── Zerodha session ──
    @app.get("/api/zerodha/status")
    async def kite_status():
        from portfolio.zerodha import token_status
        return JSONResponse(json_safe(token_status()))

    @app.get("/api/zerodha/login-url")
    async def kite_login_url():
        from portfolio.zerodha import login_url
        try:
            return JSONResponse({"login_url": login_url(),
                                 "redirect_url_to_register": "http://127.0.0.1:8000/zerodha/callback"})
        except RuntimeError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.get("/zerodha/callback", response_class=HTMLResponse)
    async def kite_callback(request_token: str = None, status: str = None):
        from portfolio.zerodha import complete_login
        import html
        if status and status != "success":
            return HTMLResponse(f"<p>Zerodha login not completed (status {html.escape(status)}).</p>", status_code=400)
        try:
            st = await run_in_threadpool(lambda: complete_login(request_token or ""))
            return HTMLResponse(f"<p>✅ Zerodha session stored — {html.escape(st['detail'])}.</p>"
                                f"<p><a href='/'>Back to the dashboard</a></p>")
        except Exception as e:
            return HTMLResponse(f"<p>Zerodha login failed: {html.escape(str(e))}</p>", status_code=400)

    # ── live P&L ──
    @app.get("/api/pnl/live")
    async def pnl_live():
        from portfolio.live_pnl import live_pnl
        return await run(live_pnl)

    @app.get("/api/pnl/live/series")
    async def pnl_series(day: str = None):
        from portfolio.live_pnl import day_series
        return await run(lambda c: day_series(c, day))

    # ── audit export ──
    @app.get("/api/audit/export-status")
    async def audit_status():
        from enterprise.audit_export import status
        return await run(status)

    @app.post("/api/audit/export", dependencies=guard)
    async def audit_export(request: Req):
        b = await body(request)
        from enterprise.audit_export import export, SOURCES

        def f(c):
            srcs = [b["source"]] if b.get("source") else list(SOURCES)
            return [export(c, s) for s in srcs]
        return await run(f)
