"""
W40 factor risk model API (quant/risk_model.py; the book view is portfolio/risk.py factor_risk).
JSON; 400 invalid; X-ATIP-Token on the one state change. Authorisation is the existing
enterprise/authz.py ROUTE_RULES, unchanged: GET /api/quant/* quant:read, POST /api/quant/*
quant:write, GET /api/portfolio/* portfolio:read (the owner's books: default tenant only).
Nothing here creates an intent, a risk decision or an order.

    GET  /api/quant/risk-model/status     model definition (factors, merged industries, hash), coverage
                                          and filled descriptors, latest dates, R^2, the bias test of the
                                          standard test portfolios, the last nightly run
    GET  /api/quant/risk-model/factors    ?as_of&days  latest factor returns (t statistics), returns over
                                          `days` sessions, annualised volatilities, the correlation matrix
    POST /api/quant/risk-model/run        (token) {rebuild?: bool} update the stored model now (background;
                                          the same incremental, idempotent update as the nightly job)
    GET  /api/portfolio/risk-model        ?portfolio=PAPER|LIVE  total = factor + specific risk, per-factor
                                          contributions, marginal contribution per stock, beta and active
                                          risk vs the Nifty 50 proxy (or the index), the book's bias statistic
                                          (book= is accepted too, as /api/risk/portfolio; empty = PAPER)
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
import logging
import threading
from datetime import date, datetime

from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

log = logging.getLogger("atip.dashboard")
_RUNNING = threading.Lock()


def register(app, guard, Req, get_connection, json_safe):
    from quant import risk_model as RMOD

    BAD = (ValueError, KeyError, TypeError)

    def err(e, code=400):
        return JSONResponse({"error": str(e)}, status_code=code)

    def _call(fn):
        conn = get_connection()
        try:
            return JSONResponse(json_safe(fn(conn)))
        except BAD as e:
            return err(e)
        finally:
            conn.close()

    async def run(fn):
        return await run_in_threadpool(_call, fn)

    def _date(v):
        if v in (None, ""):
            return None
        try:
            return date.fromisoformat(str(v)[:10])
        except ValueError:
            raise ValueError("as_of must be YYYY-MM-DD")

    @app.get("/api/quant/risk-model/status")
    async def api_risk_model_status():
        return await run(RMOD.status)

    @app.get("/api/quant/risk-model/factors")
    async def api_risk_model_factors(as_of: str = None, days: int = 20):
        def f(c):
            if not 1 <= int(days) <= 250:
                raise ValueError("days must be 1..250")
            return RMOD.factors_summary(c, _date(as_of), int(days))
        return await run(f)

    @app.get("/api/portfolio/risk-model")
    async def api_portfolio_risk_model(portfolio: str = None, book: str = None):
        from portfolio import risk as RK

        def f(c):
            if portfolio and book and portfolio.upper() != book.upper():
                raise ValueError("portfolio and book disagree; send one of them")
            book_ = str(portfolio or book or "PAPER").upper()
            if book_ not in RK.BOOKS:
                raise ValueError(f"portfolio must be one of {RK.BOOKS}")
            return RK.factor_risk(c, book_)
        return await run(f)

    @app.post("/api/quant/risk-model/run", dependencies=guard)
    async def api_risk_model_run(request: Req):
        try:
            b = await request.json()
        except Exception:
            b = {}
        rebuild = bool((b if isinstance(b, dict) else {}).get("rebuild"))
        if not _RUNNING.acquire(blocking=False):
            return JSONResponse({"started": False, "reason": "a risk-model update is already running"})

        def work():
            from db.schema import get_connection as gc, log_job
            from ops.jobs import job_lock
            start = datetime.now()
            try:
                with job_lock("risk_model") as held:
                    if not held:
                        return
                    conn = gc()
                    try:
                        res = RMOD.update(conn, rebuild=rebuild)
                    finally:
                        conn.close()
                    log_job("risk_model", res.get("status", "SUCCESS"), res.get("rows"), error=res.get("reason"),
                            start_time=start, kind="run")
            except Exception as e:                      # recorded, never raised into the server
                log.warning(f"  risk model update failed: {e}")
                log_job("risk_model", "FAILED", None, error=f"{type(e).__name__}: {e}", start_time=start, kind="run")
            finally:
                _RUNNING.release()
        threading.Thread(target=work, daemon=True).start()
        return JSONResponse({"started": True, "rebuild": rebuild,
                             "note": "follow GET /api/quant/risk-model/status (last_job)"})
