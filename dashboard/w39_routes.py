"""
W39 API and pages: equity research reports (RS), the 7-year price history (DP-11) and the
options strategy builder (OP). JSON; 400 invalid; 404 unknown; token on every POST.
Authz (enterprise/authz.py): GET /api/research/ -> research:read, POST -> research:run;
GET /api/data/ -> dashboard:read, POST -> research:run; POST /api/options/(build|analyse) ->
research:run (analysis only: nothing is ordered); other GETs -> dashboard:read.

    GET  /research                                   the research page (dashboard/w39_page.py)
    GET  /api/research/equity?rating=BUY             latest rating per symbol
    GET  /api/research/equity/{symbol}               today's stored report, else built now (?fresh=1 rebuilds)
    POST /api/research/equity/{symbol}/refresh       build and store today's report
    POST /api/research/equity/run                    {symbols?} build and store reports for the universe
    GET  /api/research/equity/{symbol}/history       rating / target calls and their outcomes
    GET  /api/research/hit-rate                      closed calls by rating
    GET  /api/data/history/coverage                  how much of the universe reaches back 7 years
    POST /api/data/history/backfill                  {symbols?, max?, years?} one budgeted backfill pass
    GET  /options-builder                            the strategy builder page
    GET  /api/options/templates
    GET  /api/options/chain/{symbol}                 spot, lot size, expiries and strikes ATIP has stored
    POST /api/options/build                          {template, symbol, expiry, spot?, width_steps?, lot_size?, target_date?}
    POST /api/options/analyse                        {symbol?, spot, legs, lot_size?, target_date?}
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
from datetime import date

from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool


def register(app, guard, Req, get_connection, json_safe):
    from quant.options_strategy import StrategyError

    def call(fn):
        conn = get_connection()
        try:
            return JSONResponse(json_safe(fn(conn)))
        except LookupError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except (StrategyError, ValueError, TypeError, KeyError) as e:
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

    def _sym(symbol):
        s = str(symbol or "").strip().upper()
        if not s or len(s) > 32 or not all(c.isalnum() or c in "-&_" for c in s):
            raise ValueError("invalid symbol")
        return s

    # ── pages ──
    @app.get("/research", response_class=HTMLResponse)
    async def page_research():
        from dashboard.security import token
        from dashboard.w39_page import render_research
        return HTMLResponse(render_research(token()))

    @app.get("/options-builder", response_class=HTMLResponse)
    async def page_options_builder():
        from dashboard.security import token
        from dashboard.w39_page import render_options
        return HTMLResponse(render_options(token()))

    # ── equity research ──
    @app.get("/api/research/equity")
    async def api_research_ratings(rating: str = None, limit: int = 500):
        from research.report import latest_ratings
        return await run(lambda c: latest_ratings(c, rating, max(1, min(int(limit), 2000))))

    @app.get("/api/research/hit-rate")
    async def api_research_hit_rate():
        from research.report import hit_rate
        return await run(hit_rate)

    @app.post("/api/research/equity/run", dependencies=guard)
    async def api_research_run(request: Req):
        b = await body(request)
        syms = [_sym(s) for s in (b.get("symbols") or [])] or None

        def f(_conn):
            from research.report import run_reports
            return run_reports(syms)
        return await run(f)

    @app.get("/api/research/equity/{symbol}")
    async def api_research_report(symbol: str, fresh: int = 0):
        def f(conn):
            from research.report import build_report, stored_report
            s = _sym(symbol)
            if not fresh:
                rep = stored_report(conn, s)
                if rep and rep.get("as_of") == str(date.today()):
                    return rep
            rep = build_report(conn, s)
            if rep.get("price") is None:
                raise LookupError(f"no stored prices for {s}")
            return rep
        return await run(f)

    @app.post("/api/research/equity/{symbol}/refresh", dependencies=guard)
    async def api_research_refresh(symbol: str):
        def f(conn):
            from research.report import build_report, save_report
            rep = build_report(conn, _sym(symbol))
            if rep.get("price") is None:
                raise LookupError(f"no stored prices for {symbol.upper()}")
            rep["is_call"] = save_report(conn, rep)
            conn.commit()
            return rep
        return await run(f)

    @app.get("/api/research/equity/{symbol}/history")
    async def api_research_history(symbol: str):
        from research.report import history
        return await run(lambda c: history(c, _sym(symbol)))

    # ── 7-year price history ──
    @app.get("/api/data/history/coverage")
    async def api_history_coverage():
        from data.history_backfill import coverage
        return await run(coverage)

    @app.post("/api/data/history/backfill", dependencies=guard)
    async def api_history_backfill(request: Req):
        b = await body(request)
        syms = [_sym(s) for s in (b.get("symbols") or [])] or None
        mx = int(b["max"]) if b.get("max") else None
        yrs = int(b["years"]) if b.get("years") else None
        if mx is not None and not 1 <= mx <= 500:
            return JSONResponse({"error": "max must be 1..500"}, status_code=400)
        if yrs is not None and not 1 <= yrs <= 20:
            return JSONResponse({"error": "years must be 1..20"}, status_code=400)

        def f(_conn):
            from data.history_backfill import run_backfill
            out = run_backfill(syms, yrs, mx)
            if len(out.get("results") or []) > 50:
                out.pop("results")
            return out
        return await run(f)

    # ── options strategy builder ──
    @app.get("/api/options/templates")
    async def api_options_templates():
        from quant.options_strategy import templates
        return JSONResponse(templates())

    @app.get("/api/options/chain/{symbol}")
    async def api_options_chain(symbol: str):
        def f(conn):
            from quant.options_strategy import chain_quotes, strike_step
            s = _sym(symbol)
            q = chain_quotes(conn, s)
            keys = q["quotes"].keys()
            strikes = sorted({k[1] for k in keys if k[2] in ("CE", "PE")})
            spot = q["spot"]
            if spot is None:
                r = conn.execute("SELECT close FROM prices_daily WHERE symbol=? ORDER BY date DESC LIMIT 1",
                                 (s,)).fetchone()
                spot = float(r[0]) if r else None
            return {"symbol": s, "spot": spot, "lot_size": q["lot_size"],
                    "expiries": sorted({k[0] for k in keys if k[2] in ("CE", "PE")}), "strikes": strikes,
                    "strike_step": strike_step(s, spot, strikes) if spot else None, "quotes": len(keys)}
        return await run(f)

    def _analyse(conn, b, legs):
        from quant.options_strategy import analyse, chain_quotes, price_legs
        sym = _sym(b["symbol"]) if b.get("symbol") else None
        q = chain_quotes(conn, sym) if sym else {"quotes": {}, "lot_size": None, "spot": None}
        spot = b.get("spot") or q["spot"]
        if spot is None and sym:
            r = conn.execute("SELECT close FROM prices_daily WHERE symbol=? ORDER BY date DESC LIMIT 1",
                             (sym,)).fetchone()
            spot = float(r[0]) if r else None
        if spot is None:
            raise ValueError("spot is required (no stored price for this symbol)")
        legs = price_legs(legs, q["quotes"])
        lot = int(b.get("lot_size") or q["lot_size"] or 1)
        out = analyse(legs, float(spot), lot_size=lot, target_date=b.get("target_date"))
        out["symbol"] = sym
        return out

    @app.post("/api/options/build", dependencies=guard)
    async def api_options_build(request: Req):
        b = await body(request)

        def f(conn):
            from quant.options_strategy import build, chain_quotes
            if not b.get("template") or not b.get("expiry"):
                raise ValueError("template and expiry are required")
            sym = _sym(b["symbol"]) if b.get("symbol") else ""
            q = chain_quotes(conn, sym) if sym else {"quotes": {}, "spot": None}
            spot = b.get("spot") or q["spot"]
            if spot is None and sym:
                r = conn.execute("SELECT close FROM prices_daily WHERE symbol=? ORDER BY date DESC LIMIT 1",
                                 (sym,)).fetchone()
                spot = float(r[0]) if r else None
            if spot is None:
                raise ValueError("spot is required (no stored price for this symbol)")
            strikes = sorted({k[1] for k in q["quotes"] if k[2] in ("CE", "PE")})
            legs = build(b["template"], float(spot), b["expiry"], symbol=sym,
                         width_steps=int(b.get("width_steps") or 2), strikes=strikes or None)
            missing = [l for l in legs if l["kind"] != "FUT" and (str(l["expiry"]), float(l["strike"]), l["kind"])
                       not in q["quotes"]]
            if missing:                       # no stored premium: price at a Black-Scholes default so it still draws
                from quant.derivatives import bs_price
                from quant.options_strategy import DEFAULT_IV, RISK_FREE, _years
                for l in missing:
                    T = _years(l["expiry"], date.today())
                    l["premium"] = round(bs_price(float(spot), l["strike"], T, RISK_FREE, DEFAULT_IV,
                                                  "call" if l["kind"] == "CE" else "put"), 2)
                    l["price_source"] = f"model (Black-Scholes, IV {DEFAULT_IV:.0%}): no stored quote"
            return _analyse(conn, {**b, "spot": spot}, legs)
        return await run(f)

    @app.post("/api/options/analyse", dependencies=guard)
    async def api_options_analyse(request: Req):
        b = await body(request)
        legs = b.get("legs")
        if not isinstance(legs, list):
            return JSONResponse({"error": "legs must be a list"}, status_code=400)
        return await run(lambda c: _analyse(c, b, legs))
