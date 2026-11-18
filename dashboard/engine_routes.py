"""
Session engine routes (owner prompt 18 Nov 2026, 03:05 PM IST): orders/session_engine.py on the dashboard and in the apps.

    GET  /api/v2/engine                          every Rs 25K session's card data + the summary row (sessions, combined
                                                 balance, combined P&L, reserve); what the Sessions tab and the apps read
    GET  /api/v2/engine/plan                     what a split of the reserve into Rs 25K sessions would make (nothing saved)
    POST /api/v2/engine/create   {n?, ack, run?} PAPER only: n new Rs 25K sessions (default: as many as fit), CREATED
                                                 unless run (ACTIVE in AUTO; needs ack)
    POST /api/v2/engine/{sid}/activate {ack}    CREATED / PAUSED -> ACTIVE (refused under the floor or when COMPLETED)
    POST /api/v2/engine/sync                     refresh session_trades / session_ledger now
    GET  /api/v2/engine/{sid}                    one session: wallet, counts, trades and ledger
    GET  /api/v2/engine/{sid}/trades.csv         its trade list as CSV (times IST)
    GET  /api/v2/engine/{sid}/ledger.csv         its ledger history as CSV (times IST)

Every POST needs the X-ATIP-Token header (`guard`), and the viewer role refuses it (orders/sessions.py add_session calls
ops.role.refuse). Nothing here places or confirms an order.
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool


def register(app, guard, Req, get_connection, json_safe):
    from orders import session_engine as E

    def with_conn(fn):
        conn = get_connection()
        try:
            return fn(conn)
        finally:
            conn.close()

    async def run(fn):
        try:
            return JSONResponse(json_safe(await run_in_threadpool(with_conn, fn)))
        except (ValueError, LookupError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)

    async def body(request):
        try:
            b = await request.json()
        except Exception:
            b = {}
        return b if isinstance(b, dict) else {}

    @app.get("/api/v2/engine")
    async def engine_overview():
        return await run(E.overview)

    @app.get("/api/v2/engine/plan")
    async def engine_plan():
        return await run(E.plan)

    @app.post("/api/v2/engine/create", dependencies=guard)
    async def engine_create(request: Req):
        b = await body(request)
        n = b.get("n")
        return await run(lambda c: E.create(c, None if n in (None, "") else int(n), b.get("ack") is True,
                                            b.get("run") is True))

    @app.post("/api/v2/engine/sync", dependencies=guard)
    async def engine_sync():
        return await run(E.sync)

    @app.post("/api/v2/engine/{sid}/activate", dependencies=guard)
    async def engine_activate(sid: str, request: Req):
        b = await body(request)
        return await run(lambda c: E.activate(c, sid.upper(), b.get("ack") is True))

    @app.get("/api/v2/engine/{sid}")
    async def engine_session(sid: str):
        def go(c):
            st = E.stats(c, sid.upper())
            if st is None:
                raise LookupError(f"{sid.upper()} has no seed capital")
            E.sync(c, sid.upper())
            return {"session": st, "trades": E.trades(c, sid), "ledger": E.ledger_rows(c, sid)}
        return await run(go)

    def _csv(sid: str, kind: str):
        def go(c):
            E.sync(c, sid.upper())
            if kind == "trades":
                return E.to_csv(E.trades(c, sid), E.TRADE_COLUMNS)
            return E.to_csv(E.ledger_rows(c, sid), E.LEDGER_COLUMNS)
        return go

    @app.get("/api/v2/engine/{sid}/trades.csv")
    async def engine_trades_csv(sid: str):
        text = await run_in_threadpool(with_conn, _csv(sid, "trades"))
        return Response(text, media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{sid.upper()}_trades_IST.csv"'})

    @app.get("/api/v2/engine/{sid}/ledger.csv")
    async def engine_ledger_csv(sid: str):
        text = await run_in_threadpool(with_conn, _csv(sid, "ledger"))
        return Response(text, media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{sid.upper()}_ledger_IST.csv"'})
