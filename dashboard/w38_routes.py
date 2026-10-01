"""
W38 API and pages: compliance monitoring (SEC-05), data inventory / DSR SLA / privacy notice (ENT-17),
regulatory sign-off register (ENT-14), PostgreSQL readiness (DBS-05), mobile web app (ENT-09).
JSON; 400 invalid; 404 unknown; token on every POST.
Authz (enterprise/authz.py): /api/compliance GET -> audit:read, POST -> system:operate; /api/platform ->
system:operate; /api/mobile -> dashboard:read; /privacy, /api/privacy/policy and the PWA shell are public.

    GET  /compliance                              the page (dashboard/w38_page.py)
    GET  /api/compliance                          latest run (checks + evidence) and run history
    POST /api/compliance/run                      run every check now
    GET  /api/compliance/inventory                every table: class / personal / retention (gaps = null)
    GET  /api/compliance/dsr                      open data-subject requests against the SLA
    GET  /api/compliance/regulatory               the ENT-14 register
    POST /api/compliance/regulatory/{item_id}     {status, reviewer, reference, note}
    GET  /api/platform/postgres                   dialect scan: how far the code is from PostgreSQL
    GET  /privacy  /api/privacy/policy            the privacy notice (DRAFT until legally approved)
    GET  /m  /api/mobile/summary                  mobile web app (installable PWA)
    GET  /manifest.webmanifest  /sw.js  /icon.svg PWA shell
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
import time

from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

_SCAN_CACHE: dict = {}


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

    def actor(request):
        p = getattr(request.state, "principal", None) or {}
        return p.get("username") or "owner"

    # ── pages ──
    @app.get("/compliance", response_class=HTMLResponse)
    async def page_compliance():
        from dashboard.security import token
        from dashboard.w38_page import render_compliance
        return HTMLResponse(render_compliance(token()))

    @app.get("/privacy", response_class=HTMLResponse)
    async def page_privacy():
        from dashboard.w38_page import render_privacy

        def f():
            from enterprise.privacy import policy
            conn = get_connection()
            try:
                return render_privacy(policy(conn))
            finally:
                conn.close()
        return HTMLResponse(await run_in_threadpool(f))

    @app.get("/m", response_class=HTMLResponse)
    async def page_mobile():
        from dashboard.security import token
        from dashboard.w38_page import render_mobile
        return HTMLResponse(render_mobile(token()))

    # ── SEC-05 ──
    @app.get("/api/compliance")
    async def api_compliance():
        from ops.compliance import latest
        return await run(lambda c: latest(c))

    @app.post("/api/compliance/run", dependencies=guard)
    async def api_compliance_run():
        from ops.compliance import run as run_checks
        return await run(lambda c: run_checks(c, trigger="manual"))

    # ── ENT-17 ──
    @app.get("/api/compliance/inventory")
    async def api_inventory():
        from enterprise.privacy import full_inventory
        return await run(lambda c: {"tables": full_inventory(c)})

    @app.get("/api/compliance/dsr")
    async def api_dsr():
        from enterprise.privacy import sla_status
        return await run(lambda c: sla_status(c))

    @app.get("/api/privacy/policy")
    async def api_policy():
        from enterprise.privacy import policy
        return await run(lambda c: policy(c))

    # ── ENT-14 ──
    @app.get("/api/compliance/regulatory")
    async def api_reg():
        from ops import regulatory as REG
        return await run(lambda c: {"items": REG.items(c), "statuses": REG.STATUSES, "gates": REG.GATES})

    @app.post("/api/compliance/regulatory/{item_id}", dependencies=guard)
    async def api_reg_update(item_id: str, request: Req):
        from ops import regulatory as REG
        b, who = await body(request), actor(request)
        return await run(lambda c: REG.update(c, item_id, b.get("status"), who, b.get("reviewer"), b.get("reference"),
                                              b.get("note")))

    # ── DBS-05 ──
    @app.get("/api/platform/postgres")
    async def api_pg():
        from db.dialect_scan import scan
        from db.schema import pg_runtime_url
        if not _SCAN_CACHE or time.time() - _SCAN_CACHE["at"] > 3600:     # an AST walk of the code: cache 1 h
            r = await run_in_threadpool(scan, ".")
            _SCAN_CACHE.update(at=time.time(), r=r)
        r = dict(_SCAN_CACHE["r"])
        r["runtime"] = "postgresql (experimental)" if pg_runtime_url() else "sqlite"
        return JSONResponse(json_safe(r))

    # ── ENT-09: mobile web app ──
    @app.get("/api/mobile/summary")
    async def api_mobile():
        from dashboard.w38_page import mobile_summary
        return await run(mobile_summary)

    @app.get("/manifest.webmanifest")
    async def pwa_manifest():
        from dashboard.w38_page import MANIFEST
        return Response(MANIFEST, media_type="application/manifest+json")

    @app.get("/sw.js")
    async def pwa_sw():
        from dashboard.w38_page import SERVICE_WORKER
        # served from / so its scope covers /m; never cached by the browser HTTP cache
        return Response(SERVICE_WORKER, media_type="application/javascript", headers={"Cache-Control": "no-cache"})

    @app.get("/icon.svg")
    async def pwa_icon():
        from dashboard.w38_page import ICON
        return Response(ICON, media_type="image/svg+xml")
