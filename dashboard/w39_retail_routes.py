"""
W39 API and page: basket orders (EX-18) and stock SIP plans (EX-20). JSON; 400 invalid; 404 unknown; token
on every POST. Authz (enterprise/authz.py): GET /api/orders -> execution:read, POST -> orders:manage.

    GET  /baskets                                  the page (dashboard/w39_retail_page.py)
    GET  /api/orders/baskets                       saved baskets
    POST /api/orders/baskets                       {name, note?, legs: [...]}  create
    GET  /api/orders/baskets/{basket_id}           one basket + its last runs
    POST /api/orders/baskets/{basket_id}           update (same body)
    POST /api/orders/baskets/{basket_id}/archive
    POST /api/orders/baskets/{basket_id}/preview   every leg as a dry run + the basket's funds check
    POST /api/orders/baskets/{basket_id}/execute   {confirm: true} places it (all-or-nothing gate)
    GET  /api/orders/sip                           SIP plans
    POST /api/orders/sip                           {symbol, amount | quantity, frequency, day, start_date?, end_date?}
    GET  /api/orders/sip/{plan_id}                 one plan + its executions
    POST /api/orders/sip/{plan_id}/status          {status: ACTIVE | PAUSED | ENDED}
    POST /api/orders/sip/run                       execute what is due now (PAPER only)
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

    @app.get("/baskets", response_class=HTMLResponse)
    async def page_baskets():
        from dashboard.security import token
        from dashboard.w39_retail_page import render
        return HTMLResponse(render(token()))

    # ── EX-18 baskets ──
    @app.get("/api/orders/baskets")
    async def api_baskets():
        from orders import basket as BK
        return await run(lambda conn: BK.list_baskets(conn))

    @app.post("/api/orders/baskets", dependencies=guard)
    async def api_basket_create(request: Req):
        from orders import basket as BK
        b = await body(request)
        return await run(lambda conn: BK.save(conn, b))

    @app.get("/api/orders/baskets/{basket_id}")
    async def api_basket_get(basket_id: str):
        from orders import basket as BK
        return await run(lambda conn: BK.get(conn, basket_id))

    @app.post("/api/orders/baskets/{basket_id}", dependencies=guard)
    async def api_basket_update(basket_id: str, request: Req):
        from orders import basket as BK
        b = await body(request)
        return await run(lambda conn: BK.save(conn, b, basket_id))

    @app.post("/api/orders/baskets/{basket_id}/archive", dependencies=guard)
    async def api_basket_archive(basket_id: str):
        from orders import basket as BK
        return await run(lambda conn: BK.archive(conn, basket_id))

    @app.post("/api/orders/baskets/{basket_id}/preview", dependencies=guard)
    async def api_basket_preview(basket_id: str):
        from orders import basket as BK
        return await run(lambda conn: BK.preview(conn, basket_id))

    @app.post("/api/orders/baskets/{basket_id}/execute", dependencies=guard)
    async def api_basket_execute(basket_id: str, request: Req):
        from orders import basket as BK
        b = await body(request)
        return await run(lambda conn: BK.execute(conn, basket_id, confirm=b.get("confirm") is True))

    # ── EX-20 SIP ──
    @app.get("/api/orders/sip")
    async def api_sip_plans():
        from orders import sip as SIP
        return await run(lambda conn: SIP.plans(conn))

    @app.post("/api/orders/sip/run", dependencies=guard)
    async def api_sip_run():
        from orders import sip as SIP
        return JSONResponse(json_safe(await run_in_threadpool(SIP.run_due)))

    @app.post("/api/orders/sip", dependencies=guard)
    async def api_sip_create(request: Req):
        from orders import sip as SIP
        b = await body(request)
        return await run(lambda conn: SIP.create(conn, b))

    @app.get("/api/orders/sip/{plan_id}")
    async def api_sip_get(plan_id: str):
        from orders import sip as SIP
        return await run(lambda conn: SIP.get(conn, plan_id))

    @app.post("/api/orders/sip/{plan_id}/status", dependencies=guard)
    async def api_sip_status(plan_id: str, request: Req):
        from orders import sip as SIP
        b = await body(request)
        return await run(lambda conn: SIP.set_status(conn, plan_id, b.get("status")))
