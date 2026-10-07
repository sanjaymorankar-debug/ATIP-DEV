"""
W39 API and pages: equity research reports (RS), the 7-year price history (DP-11) and the
options strategy builder (OP). JSON; 400 invalid; 404 unknown; token on every POST.
Authz (enterprise/authz.py): GET /api/research/ -> research:read, POST -> research:run;
GET /api/data/ -> dashboard:read, POST -> research:run; POST /api/options/(build|analyse) ->
research:run (analysis only: nothing is ordered); POST /api/screener/ -> workspace:write (saved
screens); POST /api/signals/, /api/market-pulse/, /api/orderbook/, /api/market-regime/ -> research:run;
GET /api/brokers/open-orders -> portfolio:read; other GETs -> dashboard:read.

    GET  /research                                   the research page (dashboard/w39_page.py)
    GET  /api/research/equity?rating=BUY             latest rating per symbol
    GET  /api/research/equity/{symbol}               today's stored report, else built now (?fresh=1 rebuilds)
    POST /api/research/equity/{symbol}/refresh       build and store today's report
    POST /api/research/equity/run                    {symbols?} build and store reports for the universe
    GET  /api/research/equity/{symbol}/history       rating / target calls and their outcomes
    GET  /api/research/hit-rate                      closed calls by rating
    GET  /api/data/history/coverage                  how much of the universe reaches back 7 years
    POST /api/data/history/backfill                  {symbols?, max?, years?} one budgeted backfill pass
    GET  /screener                                   the stock screener page (fundamental + technical)
    GET  /api/screener/fields                        field catalogue (groups, units, aliases), presets, operators
    GET  /api/screener/run?query&sort&desc&limit&columns     run a screen (read-only)
    GET  /api/screener/run.csv?...                   the same, as CSV
    GET  /api/screener/saved                         saved screens; POST {name, query, sort?, desc?, columns?, notify?, screen_id?}
    GET  /api/screener/saved/{id}/run                run a saved screen; reports new / dropped matches since its last run
    POST /api/screener/saved/{id}/delete
    GET  /signals                                    technical signals page (today, track record)
    GET  /api/signals/technical?date&direction&min_confluence&alignment    signals with levels, confluence, market gate
    GET  /api/signals/technical/stats?min_confluence&alignment  track record per scan: win rate, average R
    GET  /api/signals/technical/gate-effect?min_confluence     closed signals WITH / MIXED / AGAINST the market gate
    GET  /api/signals/technical/symbol/{symbol}      latest technical snapshot + recent signals for one stock
    POST /api/signals/technical/run                  {symbols?} compute today's snapshot and signals now
    GET  /api/market-regime                          the market gate today: status, distribution days, 200-DMA, changes
    GET  /api/market-regime/history?days=250         one row per session (Nifty, DMAs, distribution days, gate)
    POST /api/market-regime/run                      recompute the gate now
    GET  /market-pulse                               global cues, FII flows, positioning, order book, your orders
    GET  /api/market-pulse                           everything + the overall context and its reasons
    GET  /api/market-pulse/global | /fii | /positioning   the parts
    POST /api/market-pulse/gift                      capture the GIFT Nifty / global-model gap estimate now
    POST /api/market-pulse/refresh                   fetch NSE participant OI (7 days) and the Nifty history now
    GET  /api/orderbook/pressure?side=buy|sell&limit  latest pending buy / sell totals per stock today
    GET  /api/orderbook/pressure/{symbol}            today's polls for one stock
    POST /api/orderbook/snapshot                     poll the whole universe now
    GET  /api/brokers/open-orders                    your pending orders at Dhan (read-only) + ATIP's resting ones
    GET  /options-builder                            the strategy builder page
    GET  /api/options/templates
    GET  /api/options/chain/{symbol}                 spot, lot size, expiries and strikes ATIP has stored
    POST /api/options/build                          {template, symbol, expiry, spot?, width_steps?, lot_size?, target_date?}
    POST /api/options/analyse                        {symbol?, spot, legs, lot_size?, target_date?}
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
import math
from datetime import date

from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool


def register(app, guard, Req, get_connection, json_safe):
    from quant.options_strategy import StrategyError

    def finite(obj):
        """NaN / inf -> None, so one bad number cannot turn a whole response into a 400."""
        if isinstance(obj, dict):
            return {k: finite(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [finite(v) for v in obj]
        if isinstance(obj, float) and not math.isfinite(obj):
            return None
        return obj

    def call(fn):
        conn = get_connection()
        try:
            return JSONResponse(finite(json_safe(fn(conn))))
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

    # ── market pulse: global cues, FII, positioning, order book ──
    @app.get("/market-pulse", response_class=HTMLResponse)
    async def page_market_pulse():
        from dashboard.security import token
        from dashboard.w39_page import render_pulse
        return HTMLResponse(render_pulse(token()))

    @app.get("/api/market-pulse")
    async def api_pulse():
        from research.market_pulse import pulse
        return await run(pulse)

    @app.get("/api/market-pulse/global")
    async def api_pulse_global():
        from research.market_pulse import global_cue_model
        return await run(global_cue_model)

    @app.get("/api/market-pulse/fii")
    async def api_pulse_fii():
        from research.market_pulse import fii_pressure
        return await run(fii_pressure)

    @app.get("/api/market-pulse/positioning")
    async def api_pulse_positioning():
        from research.market_pulse import oi_walls, positioning
        return await run(lambda c: {**positioning(c), "oi_walls": oi_walls(c)})

    @app.post("/api/market-pulse/gift", dependencies=guard)
    async def api_pulse_gift():
        def f(conn):
            from research.market_pulse import capture_gift
            return capture_gift(conn)
        return await run(f)

    @app.post("/api/market-pulse/refresh", dependencies=guard)
    async def api_pulse_refresh():
        def f(_conn):
            from data.participant_oi import run as poi_run
            from research.market_pulse import nifty_history
            return {"participant_oi": poi_run(7), "nifty_history": nifty_history("5y")}
        return await run(f)

    @app.get("/api/orderbook/pressure")
    async def api_book_pressure(side: str = None, limit: int = 100):
        from data.order_pressure import latest
        if side and side not in ("buy", "sell"):
            return JSONResponse({"error": "side must be buy or sell"}, status_code=400)
        return await run(lambda c: latest(c, side=side, limit=max(1, min(int(limit), 2000))))

    @app.get("/api/orderbook/pressure/{symbol}")
    async def api_book_symbol(symbol: str):
        from data.order_pressure import intraday
        return await run(lambda c: intraday(c, _sym(symbol)))

    @app.post("/api/orderbook/snapshot", dependencies=guard)
    async def api_book_snapshot():
        def f(conn):
            from data.order_pressure import snapshot
            return snapshot(conn=conn)
        return await run(f)

    @app.get("/api/brokers/open-orders")
    async def api_open_orders():
        from portfolio.open_orders import waiting
        return await run(waiting)

    # ── technical signals ──
    @app.get("/signals", response_class=HTMLResponse)
    async def page_signals():
        from dashboard.security import token
        from dashboard.w39_page import render_signals
        return HTMLResponse(render_signals(token()))

    _ALIGN = ("WITH", "MIXED", "AGAINST")

    @app.get("/api/signals/technical")
    async def api_tech_signals(date: str = None, direction: str = None, min_confluence: int = 0, limit: int = 300,
                               alignment: str = None):
        from research.tech_signals import todays_signals
        if direction and direction.upper() not in ("BULL", "BEAR"):
            return JSONResponse({"error": "direction must be BULL or BEAR"}, status_code=400)
        if alignment and alignment != "not_against" and alignment.upper() not in _ALIGN:
            return JSONResponse({"error": "alignment must be WITH, MIXED, AGAINST or not_against"}, status_code=400)
        return await run(lambda c: todays_signals(c, date, direction, max(0, int(min_confluence)),
                                                   max(1, min(int(limit), 2000)), alignment))

    @app.get("/api/signals/technical/stats")
    async def api_tech_signal_stats(min_confluence: int = 0, alignment: str = None):
        from research.tech_signals import scan_stats
        if alignment and alignment.upper() not in _ALIGN:
            return JSONResponse({"error": "alignment must be WITH, MIXED or AGAINST"}, status_code=400)
        return await run(lambda c: scan_stats(c, max(0, int(min_confluence)), alignment))

    @app.get("/api/signals/technical/gate-effect")
    async def api_tech_gate_effect(min_confluence: int = 0):
        from research.tech_signals import gate_effect
        return await run(lambda c: gate_effect(c, max(0, int(min_confluence))))

    @app.get("/api/signals/technical/symbol/{symbol}")
    async def api_tech_symbol(symbol: str):
        def f(conn):
            from research.tech_signals import ensure_tables, latest_snapshot
            s = _sym(symbol)
            snap = latest_snapshot(conn, [s]).get(s)
            if not snap:
                raise LookupError(f"no technical snapshot for {s}")
            ensure_tables(conn)
            cur = conn.execute("SELECT date, scan, name, direction, entry, stop, target, confluence, status, "
                               "return_pct, r_multiple FROM technical_signal WHERE symbol=? ORDER BY date DESC "
                               "LIMIT 50", (s,))
            cols = [c[0] for c in cur.description]
            return {"snapshot": snap, "signals": [dict(zip(cols, r)) for r in cur.fetchall()]}
        return await run(f)

    @app.post("/api/signals/technical/run", dependencies=guard)
    async def api_tech_run(request: Req):
        b = await body(request)
        syms = [_sym(x) for x in (b.get("symbols") or [])] or None

        def f(_conn):
            from research.tech_signals import run_technical
            return run_technical(syms)
        return await run(f)

    # ── market regime gate ──
    @app.get("/api/market-regime")
    async def api_regime():
        from research.regime_gate import current
        return await run(current)

    @app.get("/api/market-regime/history")
    async def api_regime_history(days: int = 250):
        from research.regime_gate import history
        return await run(lambda c: history(c, max(1, min(int(days), 2000))))

    @app.post("/api/market-regime/run", dependencies=guard)
    async def api_regime_run():
        def f(conn):
            from research.regime_gate import update
            from research.tech_signals import backfill_gate
            out = update(conn)
            out["signals_tagged"] = backfill_gate(conn)
            return out
        return await run(f)

    # ── stock screener (fundamental + technical) ──
    @app.get("/screener", response_class=HTMLResponse)
    async def page_screener():
        from dashboard.security import token
        from dashboard.w39_page import render_screener
        return HTMLResponse(render_screener(token()))

    @app.get("/api/screener/fields")
    async def api_screener_fields():
        from research.screener import catalog
        return JSONResponse(catalog())

    def _screen(query, sort, desc, limit, columns):
        from research.screener import ScreenError, run_screen
        if not query or len(query) > 2000:
            raise ScreenError("query is required (up to 2000 characters)")
        cols = [c.strip() for c in columns.split(",") if c.strip()] if columns else None
        return lambda c: run_screen(c, query, sort or None, bool(desc), max(1, min(int(limit), 2000)), cols)

    @app.get("/api/screener/run")
    async def api_screener_run(query: str = "", sort: str = None, desc: int = 1, limit: int = 200,
                               columns: str = None):
        from research.screener import ScreenError
        try:
            fn = _screen(query, sort, desc, limit, columns)
        except ScreenError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return await run(fn)

    @app.get("/api/screener/run.csv")
    async def api_screener_csv(query: str = "", sort: str = None, desc: int = 1, limit: int = 2000,
                               columns: str = None):
        from fastapi.responses import Response
        from research.screener import ScreenError, to_csv
        try:
            fn = _screen(query, sort, desc, limit, columns)
        except ScreenError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        def f():
            conn = get_connection()
            try:
                return to_csv(fn(conn))
            finally:
                conn.close()
        try:
            text = await run_in_threadpool(f)
        except ScreenError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return Response(text, media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="atip_screen.csv"'})

    @app.get("/api/screener/saved")
    async def api_screener_saved():
        from research.screener import list_screens
        return await run(list_screens)

    @app.post("/api/screener/saved", dependencies=guard)
    async def api_screener_save(request: Req):
        b = await body(request)

        def f(conn):
            from research.screener import save_screen
            return save_screen(conn, b.get("name"), b.get("query") or "", b.get("sort"), b.get("desc", True),
                               b.get("columns"), bool(b.get("notify")), b.get("screen_id"))
        return await run(f)

    @app.get("/api/screener/saved/{screen_id}/run")
    async def api_screener_run_saved(screen_id: str):
        from research.screener import run_saved
        return await run(lambda c: run_saved(c, screen_id))

    @app.post("/api/screener/saved/{screen_id}/delete", dependencies=guard)
    async def api_screener_delete(screen_id: str):
        from research.screener import delete_screen
        return await run(lambda c: delete_screen(c, screen_id))

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
