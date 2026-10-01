"""
W34 execution-microstructure API. JSON; 400 invalid; 404 unknown; token on state changes.
Authz: /api/execution GET -> execution:read, POST -> execution:trade (existing rules);
/api/backtests/event-driven POST -> research:run. Everything here is PAPER: nothing can place a LIVE order.

    GET  /execution-lab                          the page (dashboard/w34_page.py)
    GET  /api/execution/algos                    ?status=&limit=        algo parents            EX-11
    GET  /api/execution/algos/{pid}              parent, children, execution quality
    POST /api/execution/algos                    {risk_decision_id, algo, params?}  start one by hand
    POST /api/execution/algos/{pid}/cancel       {reason?}
    POST /api/execution/algos/{pid}/resume       {reason?}
    POST /api/execution/algos/tick               work every parent now (in session)
    GET  /api/execution/impact                   ?symbol&quantity&side=BUY&price=             EX-12
    POST /api/execution/impact/calibrate         refit Y from LIVE fills
    GET  /api/execution/latency                  ?minutes=1440  per-stage summary               EX-15
    GET  /api/execution/latency/{stage}          ?minutes=1440  per-minute series
    POST /api/execution/latency/flush
    GET  /api/execution/events                   ?topic&key&limit   recent OMS events           EX-16
    GET  /api/execution/events/stats             ?hours=24  by topic, dead letters, handlers
    POST /api/execution/events/dispatch          deliver pending events now
    POST /api/backtests/event-driven             a backtest request + {"event_driven": {...}}    BT-17
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool


def register(app, guard, Req, get_connection, json_safe):
    def call(fn):
        conn = get_connection()
        try:
            return JSONResponse(json_safe(fn(conn)))
        except LookupError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except (ValueError, TypeError, KeyError) as e:
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

    def _n(v, lo, hi, name):
        v = int(v)
        if not lo <= v <= hi:
            raise ValueError(f"{name} must be {lo}..{hi}")
        return v

    @app.get("/execution-lab", response_class=HTMLResponse)
    async def execution_lab():
        from dashboard.w34_page import render
        from dashboard.security import token
        return HTMLResponse(render(token()))

    # ── EX-11 algos ──
    @app.get("/api/execution/algos")
    async def algos_list(status: str = None, limit: int = 100):
        from execution.algos import list_parents
        return await run(lambda c: list_parents(c, status, _n(limit, 1, 1000, "limit")))

    @app.get("/api/execution/algos/{pid}")
    async def algos_get(pid: str):
        from execution.algos import report
        return await run(lambda c: report(c, pid))

    @app.post("/api/execution/algos", dependencies=guard)
    async def algos_start(request: Req):
        b = await body(request)
        from execution.algos import start_algo
        if not b.get("risk_decision_id") or not b.get("algo"):
            return JSONResponse({"error": "risk_decision_id and algo are required"}, status_code=400)
        return await run(lambda c: start_algo(c, str(b["risk_decision_id"]), str(b["algo"]), b.get("params") or {}))

    @app.post("/api/execution/algos/{pid}/cancel", dependencies=guard)
    async def algos_cancel(pid: str, request: Req):
        b = await body(request)
        from execution.algos import cancel
        return await run(lambda c: cancel(c, pid, str(b.get("reason") or "cancelled by owner")))

    @app.post("/api/execution/algos/{pid}/resume", dependencies=guard)
    async def algos_resume(pid: str, request: Req):
        b = await body(request)
        from execution.algos import resume
        return await run(lambda c: resume(c, pid, str(b.get("reason") or "resumed by owner")))

    @app.post("/api/execution/algos/tick", dependencies=guard)
    async def algos_tick():
        from execution.algos import tick
        from execution.events import dispatch
        return await run(lambda c: {"events": dispatch(c), "tick": tick(c)})

    # ── EX-12 impact ──
    @app.get("/api/execution/impact")
    async def impact(symbol: str, quantity: int, side: str = "BUY", price: float = None):
        from execution.impact import estimate
        if side.upper() not in ("BUY", "SELL"):
            return JSONResponse({"error": "side must be BUY or SELL"}, status_code=400)
        return await run(lambda c: estimate(c, symbol.upper(), _n(quantity, 1, 10 ** 8, "quantity"), side, price))

    @app.post("/api/execution/impact/calibrate", dependencies=guard)
    async def impact_calibrate():
        from execution.impact import calibrate
        return await run(calibrate)

    # ── EX-15 latency ──
    @app.get("/api/execution/latency")
    async def latency(minutes: int = 1440):
        from ops.latency import summary
        return await run(lambda c: summary(c, _n(minutes, 1, 60 * 24 * 30, "minutes")))

    @app.get("/api/execution/latency/{stage}")
    async def latency_series(stage: str, minutes: int = 1440):
        from ops.latency import series
        return await run(lambda c: series(c, stage, _n(minutes, 1, 60 * 24 * 30, "minutes")))

    @app.post("/api/execution/latency/flush", dependencies=guard)
    async def latency_flush():
        from ops.latency import flush
        return await run(flush)

    # ── EX-16 events ──
    @app.get("/api/execution/events")
    async def events_recent(topic: str = None, key: str = None, limit: int = 200):
        from execution.events import recent
        return await run(lambda c: recent(c, topic, key, _n(limit, 1, 2000, "limit")))

    @app.get("/api/execution/events/stats")
    async def events_stats(hours: int = 24):
        from execution.events import stats
        return await run(lambda c: stats(c, _n(hours, 1, 24 * 30, "hours")))

    @app.post("/api/execution/events/dispatch", dependencies=guard)
    async def events_dispatch():
        from execution.events import dispatch
        return await run(dispatch)

    # ── BT-17 event-driven backtest ──
    @app.post("/api/backtests/event-driven", dependencies=guard)
    async def backtest_event_driven(request: Req):
        b = await body(request)
        from backtest.event_driven import run_and_store
        try:
            return JSONResponse(json_safe(await run_in_threadpool(run_and_store, b)))
        except (ValueError, TypeError, KeyError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=422)
