"""
W36 API: deep learning gate (ML-02), RL research (ML-04), AI assistant (ML-16), no-code strategy builder
(SE-10), widget schemas (API-06). JSON; 400 invalid; 404 unknown; token on every POST.
Authz (enterprise/authz.py): /api/ml -> ml:read / ml:write (existing); /api/assistant POST -> research:run;
/api/strategy-builder/save -> strategy:write, other builder POSTs -> research:run; /api/schemas -> dashboard:read.

    GET  /assistant                                 chat page
    POST /api/assistant/ask                         {question, conversation_id?}
    GET  /api/assistant/conversations               ?limit
    GET  /api/assistant/conversations/{cid}
    GET  /api/assistant/status                      settings (no secrets) + today's spend
    GET  /strategy-builder                          builder page
    GET  /api/strategy-builder/options              operators + feature catalogue
    POST /api/strategy-builder/compile              {spec} -> W3 definition (validated)
    POST /api/strategy-builder/preview              {spec, symbols?} -> today's entry matches (read-only)
    POST /api/strategy-builder/save                 {spec} -> DRAFT strategy
    POST /api/ml/deep/benefit-check                 {dataset_spec, params?, n_windows?}
    GET  /api/ml/deep/benefit-checks
    POST /api/ml/rl/runs                            {symbols, start, end, split, params?}
    GET  /api/ml/rl/runs ; GET /api/ml/rl/runs/{run_id}
    GET  /api/schemas ; GET /api/schemas/check-all ; GET /api/schemas/{widget} ; GET /api/schemas/{widget}/check
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

    def page(module):
        from dashboard.security import token
        import importlib
        return HTMLResponse(importlib.import_module(module).render(token()))

    # ── ML-16 assistant ──
    @app.get("/assistant", response_class=HTMLResponse)
    async def assistant_page():
        return page("dashboard.assistant_page")

    @app.post("/api/assistant/ask", dependencies=guard)
    async def assistant_ask(request: Req):
        b = await body(request)
        from ml.chat import ask
        return await run(lambda c: ask(c, str(b.get("question") or ""), b.get("conversation_id") or None))

    @app.get("/api/assistant/conversations")
    async def assistant_conversations(limit: int = 30):
        from ml.chat import conversations
        return await run(lambda c: conversations(c, max(1, min(int(limit), 200))))

    @app.get("/api/assistant/conversations/{cid}")
    async def assistant_conversation(cid: str):
        from ml.chat import conversation
        return await run(lambda c: conversation(c, cid))

    @app.get("/api/assistant/status")
    async def assistant_status():
        from ml.chat import _spent_today, settings
        return await run(lambda c: {**{k: v for k, v in settings().items()}, "spent_today_usd": round(_spent_today(c), 4)})

    # ── SE-10 strategy builder ──
    @app.get("/strategy-builder", response_class=HTMLResponse)
    async def builder_page():
        return page("dashboard.strategy_builder_page")

    @app.get("/api/strategy-builder/options")
    async def builder_options():
        from strategy_engine.builder import options
        return JSONResponse(json_safe(options()))

    @app.post("/api/strategy-builder/compile", dependencies=guard)
    async def builder_compile(request: Req):
        b = await body(request)
        from strategy_engine.builder import compile_spec
        return await run(lambda c: compile_spec(b.get("spec") or {}))

    @app.post("/api/strategy-builder/preview", dependencies=guard)
    async def builder_preview(request: Req):
        b = await body(request)
        from strategy_engine.builder import compile_spec, preview
        return await run(lambda c: preview(c, compile_spec(b.get("spec") or {}), symbols=b.get("symbols") or None))

    @app.post("/api/strategy-builder/save", dependencies=guard)
    async def builder_save(request: Req):
        b = await body(request)
        from strategy_engine.builder import save
        return await run(lambda c: save(c, b.get("spec") or {}))

    # ── ML-02 / ML-04 ──
    @app.post("/api/ml/deep/benefit-check", dependencies=guard)
    async def deep_benefit(request: Req):
        b = await body(request)
        from ml.deep import benefit_check
        if not b.get("dataset_spec"):
            return JSONResponse({"error": "dataset_spec is required"}, status_code=400)
        return await run(lambda c: benefit_check(c, b["dataset_spec"], b.get("params"),
                                                 n_windows=int(b.get("n_windows") or 5)))

    @app.get("/api/ml/deep/benefit-checks")
    async def deep_checks(limit: int = 50):
        from ml.deep import checks
        return await run(lambda c: checks(c, max(1, min(int(limit), 500))))

    @app.post("/api/ml/rl/runs", dependencies=guard)
    async def rl_run(request: Req):
        b = await body(request)
        from ml.rl import train_and_evaluate
        syms = b.get("symbols") or []
        if not syms or not b.get("start") or not b.get("end") or not b.get("split"):
            return JSONResponse({"error": "symbols, start, split and end are required"}, status_code=400)
        return await run(lambda c: train_and_evaluate(c, syms, b["start"], b["end"], b["split"], b.get("params")))

    @app.get("/api/ml/rl/runs")
    async def rl_runs(limit: int = 30):
        from ml.rl import runs
        return await run(lambda c: runs(c, max(1, min(int(limit), 200))))

    @app.get("/api/ml/rl/runs/{run_id}")
    async def rl_get(run_id: str):
        from ml.rl import get_run
        return await run(lambda c: get_run(c, run_id))

    # ── API-06 widget schemas ──
    def _check(request, widget):
        import requests
        from dashboard import widget_schemas as W
        sch = W.get(widget)
        port = request.url.port or 8000
        r = requests.get(f"http://127.0.0.1:{port}{W.WIDGETS[widget][1]}", timeout=60)
        if r.status_code != 200:
            return {"widget": widget, "ok": False, "errors": [f"HTTP {r.status_code}"]}
        errs = W.validate(r.json(), sch)
        return {"widget": widget, "endpoint": W.WIDGETS[widget][1], "ok": not errs, "errors": errs[:25]}

    @app.get("/api/schemas")
    async def schemas():
        from dashboard.widget_schemas import catalogue
        return JSONResponse(catalogue())

    @app.get("/api/schemas/check-all")
    async def schemas_check_all(request: Req):
        from dashboard.widget_schemas import WIDGETS
        out = [await run_in_threadpool(_check, request, w) for w in WIDGETS]
        return JSONResponse({"ok": all(x["ok"] for x in out), "widgets": out})

    @app.get("/api/schemas/{widget}")
    async def schema(widget: str):
        from dashboard.widget_schemas import get
        try:
            return JSONResponse(get(widget))
        except LookupError as e:
            return JSONResponse({"error": str(e)}, status_code=404)

    @app.get("/api/schemas/{widget}/check")
    async def schema_check(widget: str, request: Req):
        from dashboard.widget_schemas import WIDGETS
        if widget not in WIDGETS:
            return JSONResponse({"error": f"unknown widget {widget}"}, status_code=404)
        return JSONResponse(await run_in_threadpool(_check, request, widget))
