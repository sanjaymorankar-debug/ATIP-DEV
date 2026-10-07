"""
W25 portfolio-risk API. JSON; 400 invalid; 404 unknown; token on state changes.
Authz: under /api/risk (risk:read / risk:configure); the emergency exit needs risk:approve.
Nothing here places a LIVE order.

    GET  /api/risk/portfolio                  ?book=PAPER|LIVE&lookback=250&days=60  full analysis
    GET  /api/risk/portfolio/snapshots        ?book&limit  stored post-market headlines
    POST /api/risk/portfolio/what-if          (token) {weights: {sym: w}, value?}
    POST /api/risk/optimize                   (token) {symbols? | book?, objective, max_weight?, sector_cap?,
                                              min_weight?, risk_aversion?, expected?, lookback?, shrinkage?,
                                              turnover_penalty?, current_weights?, neutral_to?, neutral_targets?}
                                              W39: turnover_penalty without current_weights uses the book's
                                              position weights (book_snapshot); neutral_to {name: {sym: x}}
    GET  /api/risk/optimize/{opt_id}
    POST /api/risk/frontier                   (token) {symbols? | book?, points?, ...}
    POST /api/risk/rebalance-plan             (token) {opt_id | weights, book?, band_pct?, min_trade_value?,
                                              cash_buffer_pct?, equity?}
    GET  /api/risk/emergency-exit             ?book  the plan + confirm code
    POST /api/risk/emergency-exit             (token, risk:approve) {book, confirm, reason?, last_close?}
    GET  /api/risk/emergency-exit/history
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
import json

from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool


def register(app, guard, Req, get_connection, json_safe):
    from portfolio import emergency as EM
    from portfolio import optimize as OPT
    from portfolio import risk as RK

    BAD = (ValueError, KeyError, TypeError)

    def err(e, code=400):
        return JSONResponse({"error": str(e)}, status_code=code)

    async def body(request):
        try:
            b = await request.json()
        except Exception:
            return {}
        return b if isinstance(b, dict) else {}

    def _call(fn):
        conn = get_connection()
        try:
            return JSONResponse(json_safe(fn(conn)))
        except LookupError as e:
            return err(e, 404)
        except BAD as e:
            return err(e)
        finally:
            conn.close()

    async def run(fn):
        return await run_in_threadpool(_call, fn)

    def _book(b):
        book = str(b or "PAPER").upper()
        if book not in RK.BOOKS:
            raise ValueError(f"book must be one of {RK.BOOKS}")
        return book

    def _symbols(conn, b):
        syms = b.get("symbols")
        if syms:
            if not isinstance(syms, list) or not all(isinstance(s, str) for s in syms) or len(syms) > 100:
                raise ValueError("symbols must be a list of up to 100 symbols")
            return [s.strip().upper() for s in syms if s.strip()]
        snap = RK.book_snapshot(conn, _book(b.get("book")))
        if len(snap["positions"]) < 2:
            raise ValueError(f"the {snap['book']} book holds {len(snap['positions'])} position(s); pass symbols")
        return [p["symbol"] for p in snap["positions"]]

    def _opt_kw(b):
        kw = {}
        for k, cast in (("max_weight", float), ("min_weight", float), ("sector_cap", float),
                        ("risk_aversion", float), ("lookback", int), ("shrinkage", float), ("risk_free", float)):
            if b.get(k) is not None:
                kw[k] = cast(b[k])
        if "lookback" in kw and not 60 <= kw["lookback"] <= 1000:
            raise ValueError("lookback must be 60..1000")
        if "shrinkage" in kw and not 0 <= kw["shrinkage"] <= 1:
            raise ValueError("shrinkage must be 0..1")
        if b.get("expected") is not None:
            kw["expected"] = b["expected"]
        return kw

    def _pf_kw(conn, b):
        """W39 (PF-14 / PF-15) optimise() options; validated again by optimise()."""
        kw = {}
        if b.get("turnover_penalty") is not None:
            kw["turnover_penalty"] = float(b["turnover_penalty"])
        cw = b.get("current_weights")
        if cw is not None:
            if not isinstance(cw, dict) or len(cw) > 500:
                raise ValueError("current_weights must be {symbol: weight} with up to 500 names")
            kw["current_weights"] = {str(k).strip().upper(): float(v) for k, v in cw.items()}
        elif kw.get("turnover_penalty"):
            snap = RK.book_snapshot(conn, _book(b.get("book")))
            kw["current_weights"] = {p["symbol"]: float(p["weight"]) for p in snap["positions"]}
        nt = b.get("neutral_to")
        if nt is not None:
            if not isinstance(nt, dict) or not all(isinstance(v, dict) and len(v) <= 500 for v in nt.values()):
                raise ValueError("neutral_to must be {name: {symbol: exposure}}")
            kw["neutral_to"] = {str(k): {str(s): None if x is None else float(x) for s, x in v.items()}
                                for k, v in nt.items()}
            if b.get("neutral_targets") is not None:
                if not isinstance(b["neutral_targets"], dict):
                    raise ValueError("neutral_targets must be {name: target}")
                kw["neutral_targets"] = {str(k): float(v) for k, v in b["neutral_targets"].items()}
        return kw

    @app.get("/api/risk/portfolio")
    async def api_risk_portfolio(book: str = "PAPER", lookback: int = 250, days: int = 60):
        return await run(lambda c: RK.analyse(c, _book(book), max(60, min(lookback, 1000)), max(5, min(days, 500))))

    @app.get("/api/risk/portfolio/snapshots")
    async def api_risk_portfolio_snapshots(book: str = None, limit: int = 60):
        def f(c):
            q, a = "SELECT as_of, book, headline_json FROM portfolio_risk_snapshot", []
            if book:
                q += " WHERE book=?"; a.append(_book(book))
            return [{"as_of": r[0], "book": r[1], **json.loads(r[2] or "{}")} for r in
                    c.execute(q + " ORDER BY as_of DESC LIMIT ?", a + [max(1, min(int(limit), 500))])]
        return await run(f)

    @app.post("/api/risk/portfolio/what-if", dependencies=guard)
    async def api_risk_what_if(request: Req):
        b = await body(request)
        w = b.get("weights")
        if not isinstance(w, dict) or not w or len(w) > 100:
            return err("weights must be {symbol: weight} with 1..100 names")
        try:
            w = {str(k).upper(): float(v) for k, v in w.items()}
        except BAD as e:
            return err(e)
        return await run(lambda c: RK.analyse(c, "PAPER", weights=w, value=b.get("value")))

    @app.post("/api/risk/optimize", dependencies=guard)
    async def api_risk_optimize(request: Req):
        b = await body(request)
        return await run(lambda c: OPT.optimise(c, _symbols(c, b), b.get("objective", "min_variance"), **_opt_kw(b),
                                                **_pf_kw(c, b)))

    @app.get("/api/risk/optimize/{opt_id}")
    async def api_risk_optimize_get(opt_id: str):
        def f(c):
            r = OPT.get_optimization(c, opt_id)
            if not r:
                raise LookupError(f"no optimisation {opt_id}")
            return r
        return await run(f)

    @app.post("/api/risk/frontier", dependencies=guard)
    async def api_risk_frontier(request: Req):
        b = await body(request)
        return await run(lambda c: OPT.frontier(c, _symbols(c, b), int(max(3, min(int(b.get("points", 12)), 40))),
                                                **_opt_kw(b)))

    @app.post("/api/risk/rebalance-plan", dependencies=guard)
    async def api_risk_rebalance_plan(request: Req):
        b = await body(request)

        def f(c):
            w = b.get("weights")
            if b.get("opt_id"):
                o = OPT.get_optimization(c, b["opt_id"])
                if not o:
                    raise LookupError(f"no optimisation {b['opt_id']}")
                w = o["weights"]
            if not isinstance(w, dict) or not w:
                raise ValueError("send opt_id or weights {symbol: weight}")
            kw = {k: float(b[k]) for k in ("band_pct", "min_trade_value", "cash_buffer_pct", "equity")
                  if b.get(k) is not None}
            return OPT.rebalance_plan(c, {str(k).upper(): float(v) for k, v in w.items()}, _book(b.get("book")), **kw)
        return await run(f)

    @app.get("/api/risk/emergency-exit")
    async def api_risk_emergency_plan(book: str = "PAPER"):
        return await run(lambda c: EM.plan(c, _book(book)))

    @app.get("/api/risk/emergency-exit/history")
    async def api_risk_emergency_history(limit: int = 20):
        return await run(lambda c: EM.history(c, max(1, min(int(limit), 200))))

    @app.post("/api/risk/emergency-exit", dependencies=guard)
    async def api_risk_emergency_exit(request: Req):
        b = await body(request)
        if not b.get("confirm"):
            return err("confirm (the code from GET /api/risk/emergency-exit) is required")
        actor = getattr(getattr(request, "state", None), "user", None) or "dashboard"
        return await run(lambda c: EM.execute(c, _book(b.get("book")), str(b["confirm"]), str(actor),
                                              str(b.get("reason") or "")[:500],
                                              "last_close" if b.get("last_close") else "live"))
