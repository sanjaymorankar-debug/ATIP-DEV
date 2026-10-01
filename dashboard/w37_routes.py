"""
W37 API: brokers (BR-07), multi-broker import (PF-12), multi-asset / options (ENT-15), vault completion (ENT-06).
JSON; 400 invalid; 404 unknown; token on every POST. Nothing here can place a LIVE order.
Authz (enterprise/authz.py): /api/brokers GET -> portfolio:read, POST -> portfolio:manage; /api/portfolio/imports ->
portfolio:manage (existing /api/portfolio rule); /api/execution/options POST -> execution:trade (existing rule);
/api/account/vault -> workspace:write (existing rule).

    GET  /brokers                                    the page (dashboard/w37_page.py)
    GET  /api/brokers                                supported brokers, credential fields, live status
    GET  /api/brokers/consolidated                   holdings across the caller's brokers
    GET  /api/brokers/{broker}/snapshot              holdings + positions at one broker (read-only)
    POST /api/brokers/payload-preview                {order_id, broker}
    POST /api/account/vault/{credential_id}/verify   read-only connection check
    GET  /api/account/vault/expiring                 ?hours=12
    POST /api/portfolio/imports/preview              {filename, content}   (CSV text)
    POST /api/portfolio/imports/commit               {filename, content, broker?, as_of?, force?}
    POST /api/portfolio/imports/from-broker          {broker}
    GET  /api/portfolio/imports/runs
    GET  /api/execution/assets                       asset classes and their execution / risk paths
    GET  /api/execution/options/book                 positions (marked), cash, premium held, recent trades
    POST /api/execution/options/check                {underlying, expiry, strike, option_type, side, lots} (no fill)
    POST /api/execution/options/order                same body: risk-checked paper fill
    POST /api/execution/options/settle               expiry settlement now
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

MAX_FILE_CHARS = 5_000_000


def register(app, guard, Req, get_connection, json_safe):
    def call(fn):
        conn = get_connection()
        try:
            return JSONResponse(json_safe(fn(conn)))
        except LookupError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except (ValueError, TypeError, KeyError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except RuntimeError as e:
            return JSONResponse({"error": str(e)}, status_code=502)
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

    def who(request):
        """(tenant, user) for vault / broker access; the owner in a single-user install."""
        p = getattr(request.state, "principal", None) or {}
        return p.get("tenant_id") or "default", p.get("user_id") or "owner"

    def ledger_owner(request):
        from wealth import common as C
        return C.owner_from_principal(getattr(request.state, "principal", None))

    def content(b):
        c = b.get("content")
        if not isinstance(c, str) or not c.strip():
            raise ValueError("content (the CSV text) is required")
        if len(c) > MAX_FILE_CHARS:
            raise ValueError("file too large (5 MB of text max)")
        return c

    @app.get("/brokers", response_class=HTMLResponse)
    async def brokers_page():
        from dashboard.w37_page import render
        from dashboard.security import token
        return HTMLResponse(render(token()))

    # ── BR-07 ──
    @app.get("/api/brokers")
    async def brokers():
        from brokers.registry import brokers as B
        return JSONResponse(B())

    @app.get("/api/brokers/consolidated")
    async def brokers_consolidated(request: Req):
        t, u = who(request)
        from brokers.registry import consolidated
        return await run(lambda c: consolidated(c, t, u))

    @app.get("/api/brokers/{broker}/snapshot")
    async def broker_snapshot(broker: str, request: Req):
        t, u = who(request)
        from brokers.registry import snapshot
        return await run(lambda c: snapshot(c, t, u, broker.lower()))

    @app.post("/api/brokers/payload-preview", dependencies=guard)
    async def payload_preview(request: Req):
        b = await body(request)
        from brokers.registry import payload_preview as P
        return await run(lambda c: P(c, str(b.get("order_id") or ""), str(b.get("broker") or "")))

    # ── ENT-06 ──
    @app.post("/api/account/vault/{credential_id}/verify", dependencies=guard)
    async def vault_verify(credential_id: str, request: Req):
        t, u = who(request)
        from enterprise.vault import verify

        def f(c):
            own = c.execute("SELECT 1 FROM enterprise_vault_credential WHERE credential_id=? AND tenant_id=? AND "
                            "user_id=?", (credential_id, t, u)).fetchone()
            if not own:
                raise LookupError(f"no credential {credential_id}")
            return verify(c, credential_id, actor=u)
        return await run(f)

    @app.get("/api/account/vault/expiring")
    async def vault_expiring(request: Req, hours: int = 12):
        t, u = who(request)
        from enterprise.vault import expiring
        return await run(lambda c: [x for x in expiring(c, max(1, min(int(hours), 168)))
                                    if x["tenant_id"] == t and x["user_id"] == u])

    # ── PF-12 ──
    @app.post("/api/portfolio/imports/preview", dependencies=guard)
    async def import_preview(request: Req):
        b = await body(request)
        from portfolio.imports import parse

        def f(c):
            r = parse(content(b), str(b.get("filename") or ""))
            r["rows_preview"] = r["rows"][:50]
            r.pop("rows")
            return r
        return await run(f)

    @app.post("/api/portfolio/imports/commit", dependencies=guard)
    async def import_commit(request: Req):
        b = await body(request)
        owner = ledger_owner(request)
        from portfolio.imports import commit, parse
        return await run(lambda c: commit(c, owner, parse(content(b), str(b.get("filename") or "")), b.get("broker"),
                                          b.get("as_of"), bool(b.get("force")), str(b.get("filename") or "")))

    @app.post("/api/portfolio/imports/from-broker", dependencies=guard)
    async def import_from_broker(request: Req):
        b = await body(request)
        owner, (t, u) = ledger_owner(request), who(request)
        from portfolio.imports import import_from_broker as I
        return await run(lambda c: I(c, owner, t, u, str(b.get("broker") or "").lower()))

    @app.get("/api/portfolio/imports/runs")
    async def import_runs(request: Req):
        owner = ledger_owner(request)
        from portfolio.imports import runs
        return await run(lambda c: runs(c, owner))

    # ── ENT-15 ──
    @app.get("/api/execution/assets")
    async def assets():
        from execution.assets import catalogue
        return JSONResponse(json_safe(catalogue()))

    @app.get("/api/execution/options/book")
    async def options_book():
        from execution.options_paper import book
        return await run(book)

    @app.post("/api/execution/options/check", dependencies=guard)
    async def options_check(request: Req):
        b = await body(request)
        from execution.options_paper import check, ensure_tables

        def f(c):
            ensure_tables(c)
            return check(c, b)
        return await run(f)

    @app.post("/api/execution/options/order", dependencies=guard)
    async def options_order(request: Req):
        b = await body(request)
        from execution.options_paper import place
        _, u = who(request)
        return await run(lambda c: place(c, b, actor=u))

    @app.post("/api/execution/options/settle", dependencies=guard)
    async def options_settle():
        from execution.options_paper import settle_expired
        return await run(lambda c: settle_expired(None, c))
