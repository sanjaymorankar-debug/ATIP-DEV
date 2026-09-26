"""
Wealth-track API (W11-W17) and the /wealth page. Conventions as elsewhere: JSON,
400 invalid, 404 unknown, X-ATIP-Token on anything that changes state. Every
route acts on the CALLER's own data (wealth/common.owner_from_principal); no
route takes an owner from the request. Nothing here creates an order, an
intent, a risk decision or an order rule.

    W11 Investor DNA
    GET  /api/wealth/dna/questionnaire      question bank (version Q1)
    GET  /api/wealth/dna                    current profile (404 if none) + status CURRENT / STALE
    POST /api/wealth/dna                    (token) {answers} -> new immutable version
    POST /api/wealth/dna/preview            (token) {answers} -> computed, not stored
    GET  /api/wealth/dna/history            versions
    GET  /api/wealth/dna/{profile_id}       one stored version (own only)
    GET  /wealth                            page
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
from fastapi.responses import HTMLResponse, JSONResponse


def register(app, guard, Req, get_connection, json_safe):
    from wealth import common as C
    from wealth import dna as DNA

    BAD = (ValueError, KeyError, TypeError)

    def err(e, code=400):
        return JSONResponse({"error": str(e)}, status_code=code)

    async def body(request):
        try:
            b = await request.json()
        except Exception:
            return {}
        return b if isinstance(b, dict) else {}

    def owner(request):
        return C.owner_from_principal(getattr(request.state, "principal", None))

    def actor(request):
        p = getattr(request.state, "principal", None) or {}
        return p.get("username") or "owner"

    def run(fn, code=400):
        conn = get_connection()
        try:
            return JSONResponse(json_safe(fn(conn)))
        except LookupError as e:
            return err(e, 404)
        except BAD as e:
            return err(e, code)
        finally:
            conn.close()

    # ── W11 Investor DNA ────────────────────────────────────────────────────
    @app.get("/api/wealth/dna/questionnaire")
    async def api_wealth_dna_questionnaire():
        return JSONResponse(DNA.questionnaire())

    @app.get("/api/wealth/dna")
    async def api_wealth_dna(request: Req):
        def f(conn):
            p = DNA.current(conn, owner(request))
            if not p:
                raise LookupError("no investor profile yet: answer the questionnaire (POST /api/wealth/dna)")
            return p
        return run(f)

    @app.post("/api/wealth/dna", dependencies=guard)
    async def api_wealth_dna_save(request: Req):
        b = await body(request)
        return run(lambda conn: DNA.save(conn, owner(request), b.get("answers") or {}, actor(request)))

    @app.post("/api/wealth/dna/preview", dependencies=guard)
    async def api_wealth_dna_preview(request: Req):
        b = await body(request)
        return run(lambda conn: DNA.compute(b.get("answers") or {},
                                            DNA.observed_behaviour(conn, owner(request))))

    @app.get("/api/wealth/dna/history")
    async def api_wealth_dna_history(request: Req):
        return run(lambda conn: DNA.history(conn, owner(request)))

    @app.get("/api/wealth/dna/{profile_id}")
    async def api_wealth_dna_version(profile_id: str, request: Req):
        def f(conn):
            o = owner(request)
            r = conn.execute("SELECT * FROM investor_profile_version WHERE profile_id=? AND tenant_id=? AND "
                             "owner_id=?", (profile_id, o["tenant_id"], o["owner_id"])).fetchone()
            if not r:
                raise LookupError("not found")
            return DNA._row_to_profile(r)
        return run(f)

    # ── Page ────────────────────────────────────────────────────────────────
    @app.get("/wealth", response_class=HTMLResponse)
    async def wealth_page():
        from dashboard.security import token as _tok
        from dashboard.wealth_page import render
        return HTMLResponse(render(_tok()))
