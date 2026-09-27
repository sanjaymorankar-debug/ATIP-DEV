"""
W22 research platform API: factor approval (AF-08), research studies (DBS-07 /
QR-12), the formula registry (SC-18) and stored score components (AF-03). JSON;
400 invalid, 404 unknown; X-ATIP-Token on anything that changes state.

    GET  /api/quant/approvals                   every factor's approval state
    GET  /api/quant/approvals/{key}             evidence evaluation + history (key e.g. mom_12_1@1)
    POST /api/quant/approvals/{key}             (token) {decision, reason}
    POST /api/quant/research-report             (token) {start, end} IC / decay / redundancy for all factors
    GET  /api/research/studies                  ?status ; POST (token) {title, hypothesis, method, tags, supersedes}
    GET  /api/research/studies/{id}
    POST /api/research/studies/{id}/links       (token) {kind, ref, note}
    POST /api/research/studies/{id}/conclude    (token) {outcome, conclusion}
    POST /api/research/studies/{id}/abandon     (token) {reason}
    GET  /api/formulas                          ; GET /api/formulas/{weights_hash}
    GET  /api/scores/components/{symbol}?date=  the sub-factors behind each ATIP index
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
from fastapi.responses import JSONResponse


def register(app, guard, Req, get_connection, json_safe):
    from quant import approval as AP
    from quant import studies as ST
    from scores import formulas as FM

    BAD = (ValueError, KeyError, TypeError)

    def err(e, code=400):
        return JSONResponse({"error": str(e)}, status_code=code)

    async def body(request):
        try:
            b = await request.json()
        except Exception:
            return {}
        return b if isinstance(b, dict) else {}

    def actor(request):
        p = getattr(request.state, "principal", None) or {}
        return p.get("username") or "owner"

    def run(fn):
        conn = get_connection()
        try:
            return JSONResponse(json_safe(fn(conn)))
        except LookupError as e:
            return err(e, 404)
        except BAD as e:
            return err(e)
        finally:
            conn.close()

    @app.get("/api/quant/approvals")
    async def api_quant_approvals():
        def f(conn):
            from quant.factors import FACTORS
            return [{**AP.state(conn, fd.key), "category": fd.category, "name": fd.name} for fd in FACTORS]
        return run(f)

    @app.get("/api/quant/approvals/{key}")
    async def api_quant_approval(key: str):
        return run(lambda conn: {**AP.evaluate(conn, key), "history": AP.history(conn, key)})

    @app.post("/api/quant/approvals/{key}", dependencies=guard)
    async def api_quant_approval_decide(key: str, request: Req):
        b = await body(request)
        return run(lambda conn: AP.decide(conn, key, b.get("decision"), b.get("reason") or "", actor(request)))

    @app.post("/api/quant/research-report", dependencies=guard)
    async def api_quant_research_report(request: Req):
        b = await body(request)
        from quant.research import research_report
        if not b.get("start") or not b.get("end"):
            return err("start and end are required")
        return run(lambda conn: research_report(conn, b["start"], b["end"]))

    @app.get("/api/research/studies")
    async def api_research_studies(status: str = None):
        return run(lambda conn: ST.list_studies(conn, status))

    @app.post("/api/research/studies", dependencies=guard)
    async def api_research_study_create(request: Req):
        b = await body(request)
        return run(lambda conn: ST.create(conn, b, actor(request)))

    @app.get("/api/research/studies/{sid}")
    async def api_research_study(sid: str):
        return run(lambda conn: ST.get(conn, sid))

    @app.post("/api/research/studies/{sid}/links", dependencies=guard)
    async def api_research_study_link(sid: str, request: Req):
        b = await body(request)
        return run(lambda conn: ST.link(conn, sid, b.get("kind"), b.get("ref"), b.get("note"), actor(request)))

    @app.post("/api/research/studies/{sid}/conclude", dependencies=guard)
    async def api_research_study_conclude(sid: str, request: Req):
        b = await body(request)
        return run(lambda conn: ST.conclude(conn, sid, b.get("outcome"), b.get("conclusion"), actor(request)))

    @app.post("/api/research/studies/{sid}/abandon", dependencies=guard)
    async def api_research_study_abandon(sid: str, request: Req):
        b = await body(request)
        return run(lambda conn: ST.abandon(conn, sid, b.get("reason"), actor(request)))

    @app.get("/api/formulas")
    async def api_formulas():
        return run(lambda conn: FM.list_formulas(conn))

    @app.get("/api/formulas/{weights_hash}")
    async def api_formula(weights_hash: str):
        return run(lambda conn: FM.get(conn, weights_hash))

    @app.get("/api/scores/components/{symbol}")
    async def api_score_components(symbol: str, date: str = None):
        def f(conn):
            d = date or conn.execute("SELECT MAX(date) FROM score_components WHERE symbol=?",
                                     (symbol.upper(),)).fetchone()[0]
            if not d:
                raise LookupError("no stored components for that symbol")
            return {"symbol": symbol.upper(), "date": str(d)[:10], "indices": FM.components(conn, symbol, d)}
        return run(f)
