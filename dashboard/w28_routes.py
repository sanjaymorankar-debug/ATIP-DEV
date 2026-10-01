"""
W28 API: news AI (NS-02..04, DB-06), score lists (DB-11), intraday scans (SG-08),
strategy performance (DB-16), ML monitoring series (DB-18), AI-strategy readiness (SE-05).

    GET  /api/news/summary                 latest market brief (Claude or rule-based, labelled)
    POST /api/news/summary                 (token) regenerate now; body {hours?}
    GET  /api/news/ai-usage                ?days=7  AI requests, tokens, cost, the daily cap
    GET  /api/lists/crash-risk             ?n=25&date=
    GET  /api/lists/spi                    ?n=25&date=
    GET  /api/lists/msi                    ?sessions=60
    GET  /api/scans/intraday               ?session=&scan=   latest run of the session
    POST /api/scans/intraday/run           (token) run the scans now
    GET  /api/strategy-performance         ?days=30
    GET  /api/ml/monitoring-series         ?model_id=&version=   drift / realised metrics over time
    GET  /api/ml/ai-strategy-readiness     gate chain per ML strategy
    POST /api/strategies/{sid}/ml-activate (token, strategy:lifecycle) {to_state: PAPER|ACTIVE, reason?}
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
import json

from fastapi.responses import JSONResponse
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

    # ── news AI ──
    @app.get("/api/news/summary")
    async def news_summary():
        from data.news_ai import latest_summary
        return await run(lambda c: latest_summary(c) or {"note": "no summary yet (POST /api/news/summary)"})

    @app.post("/api/news/summary", dependencies=guard)
    async def news_summary_now(request: Req):
        b = await body(request)
        from data.news_ai import market_summary
        return await run(lambda c: market_summary(c, _n(b.get("hours") or 24, 1, 168, "hours")))

    @app.get("/api/news/ai-usage")
    async def news_ai_usage(days: int = 7):
        from data.news_ai import usage_summary
        return await run(lambda c: usage_summary(c, _n(days, 1, 90, "days")))

    # ── lists ──
    @app.get("/api/lists/crash-risk")
    async def list_crash(n: int = 25, date: str = None):
        from scores.lists import crash_risk
        return await run(lambda c: crash_risk(c, date, _n(n, 1, 200, "n")))

    @app.get("/api/lists/spi")
    async def list_spi(n: int = 25, date: str = None):
        from scores.lists import top_spi
        return await run(lambda c: top_spi(c, date, _n(n, 1, 200, "n")))

    @app.get("/api/lists/msi")
    async def list_msi(sessions: int = 60):
        from scores.lists import msi_panel
        return await run(lambda c: msi_panel(c, _n(sessions, 5, 500, "sessions")))

    # ── intraday scans ──
    @app.get("/api/scans/intraday")
    async def scans(session: str = None, scan: str = None):
        from strategy.intraday_scan import latest_hits
        return await run(lambda c: latest_hits(c, session, scan))

    @app.post("/api/scans/intraday/run", dependencies=guard)
    async def scans_run():
        from strategy.intraday_scan import run_intraday_scans
        return JSONResponse(json_safe(await run_in_threadpool(run_intraday_scans)))

    # ── strategy performance ──
    @app.get("/api/strategy-performance")
    async def strat_perf(days: int = 30):
        from strategy_engine.performance import performance
        return await run(lambda c: performance(c, _n(days, 1, 365, "days")))

    # ── ML monitoring series / AI strategies ──
    @app.get("/api/ml/monitoring-series")
    async def ml_series(model_id: str = None, version: str = None):
        def f(c):
            models = [r[0] for r in c.execute("SELECT DISTINCT model_id FROM ml_model_monitoring ORDER BY 1")]
            mid = model_id or (models[0] if models else None)
            if not mid:
                return {"models": [], "drift": [], "metrics": [], "health": [],
                        "note": "no model monitoring yet (needs a model with predictions and ml.enabled)"}
            sql, args = "SELECT as_of, version, n_shifted, feature_drift_json, prediction_dist_json FROM " \
                        "ml_model_monitoring WHERE model_id=?", [mid]
            if version:
                sql += " AND version=?"
                args.append(version)
            drift = []
            for as_of, ver, n_shift, fd, pdist in c.execute(sql + " ORDER BY as_of", args):
                fd = json.loads(fd or "{}")
                psis = [v.get("psi") if isinstance(v, dict) else v for v in fd.values()] if isinstance(fd, dict) else []
                psis = [p for p in psis if isinstance(p, (int, float))]
                drift.append({"as_of": str(as_of)[:10], "version": ver, "n_shifted": n_shift,
                              "max_psi": max(psis) if psis else None,
                              "mean_psi": sum(psis) / len(psis) if psis else None})
            metrics = [{"period_end": str(pe)[:10], "version": v, "kind": k, **json.loads(m or "{}")}
                       for v, k, pe, m in c.execute("SELECT version, kind, period_end, metrics_json FROM "
                                                    "ml_model_metrics WHERE model_id=? ORDER BY period_end", (mid,))]
            health = [{"checked_at": str(t)[:16], "status": s} for t, s in c.execute(
                "SELECT checked_at, status FROM ml_health_check ORDER BY id DESC LIMIT 60")][::-1]
            return {"model_id": mid, "models": models, "drift": drift, "metrics": metrics, "health": health}
        return await run(f)

    @app.get("/api/ml/ai-strategy-readiness")
    async def ai_ready():
        from ml.ai_strategy import readiness
        return await run(readiness)

    @app.post("/api/strategies/{sid}/ml-activate", dependencies=guard)
    async def ai_activate(sid: str, request: Req):
        b = await body(request)
        from ml.ai_strategy import activate
        from strategy_engine.lifecycle import LifecycleError

        def f(c):
            try:
                return activate(c, sid, b.get("to_state") or "PAPER", "dashboard", b.get("reason") or "")
            except LifecycleError as e:
                raise ValueError(str(e))
        return await run(f)
