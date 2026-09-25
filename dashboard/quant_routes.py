"""
W6 advanced-quant API and the /quant page. Conventions as elsewhere: JSON,
400 invalid, 404 unknown, X-ATIP-Token on anything that changes state. Nothing
here creates an intent, a risk decision or an order.

    GET  /api/quant/status                 config, factor coverage, data dependencies
    GET  /api/quant/factors                registry;  GET /api/quant/factors/{id}
    GET  /api/quant/factor-sets            POST (token) {name, version, factors}
    GET  /api/quant/composites             POST (token) {name, version, components, min_coverage}
    POST /api/quant/compute                (token) {as_of?, factors?, composites?} -- background
    GET  /api/quant/scores                 ?as_of&key&symbol&limit
    GET  /api/quant/rankings               ?as_of&key&top&bottom&sector ; ?by=sector for sector ranks
    GET  /api/quant/strategies             W3 strategies of kinds pairs / portfolio / multi_factor /
                                           quant_rank that read quant features
    GET  /api/quant/experiments            POST (token) create; POST /{id}/run (token, background)
    GET  /api/quant/pairs                  POST (token) create; POST /{id}/status (token);
    GET  /api/quant/pairs/{id}/analyze     spread / z / hedge ratio / cointegration now
    GET  /api/quant/spreads                stored spread snapshots
    POST /api/quant/pairs/screen           (token) {symbols, lookback, min_corr}
    POST /api/quant/portfolios             (token) {name, key, top_n, bottom_n, method, constraints, long_short}
    GET  /api/quant/portfolios             with positions and exposures
    GET  /api/quant/research               POST (token) {key, kind: ic|ic_decay|quantiles|correlation, ...}
    GET  /api/quant/events                 ?symbol ; POST /api/quant/events/sync (token)
    GET  /api/quant/volatility/{symbol}    close-to-close / Parkinson / GK / downside / regime
    GET  /api/quant/derivatives            instruments + data status (PENDING)
    GET  /api/quant/options                analytics (PENDING) ; GET /api/quant/options/price  BS calculator
    GET  /api/quant/microstructure         ?symbol&date
    GET  /quant                            page
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
import json
import threading

from fastapi.responses import HTMLResponse, JSONResponse


def register(app, guard, Req, get_connection, json_safe):
    from quant import composite as CP, derivatives as DV, engine as EN, events as EVT, experiments as EXP
    from quant import factors as FX, microstructure as MS, pairs as PR, portfolio as PF, research as RS
    from quant import volatility as VL
    from quant.config import settings

    BAD = (ValueError, KeyError, TypeError)
    _ready = {"done": False}

    def conn_ready():
        conn = get_connection()
        if not _ready["done"]:
            FX.sync(conn); FX.ensure_builtin_set(conn); CP.ensure_builtin(conn)
            _ready["done"] = True
        return conn

    def err(e, code=400):
        return JSONResponse({"error": str(e)}, status_code=code)

    async def body(request):
        try:
            b = await request.json()
        except Exception:
            return {}
        return b if isinstance(b, dict) else {}

    def rows(conn, q, args=()):
        return [dict(r) for r in conn.execute(q, args)]

    def latest(conn):
        r = conn.execute("SELECT MAX(as_of) FROM quant_factor_score").fetchone()[0]
        return str(r)[:10] if r else None

    def bg(fn):
        def run():
            c = get_connection()
            try:
                fn(c)
            except Exception:
                pass
            finally:
                c.close()
        threading.Thread(target=run, daemon=True).start()

    @app.get("/api/quant/status")
    async def api_quant_status():
        conn = conn_ready()
        try:
            a = latest(conn)
            cov = rows(conn, "SELECT factor_key, kind, COUNT(*) n FROM quant_factor_score WHERE as_of=? GROUP BY 1,2",
                       (a,)) if a else []
            return JSONResponse(json_safe({
                "settings": settings(), "latest_scores": a, "coverage": cov,
                "data_dependencies": {"fundamentals": FX.FUND, "shares": FX.SHARES, "spread": FX.SPREAD,
                                      "derivatives": DV.DATA_STATUS, "microstructure": MS.PENDING,
                                      "events_pending": EVT.PENDING_TYPES},
                "execution_link": "none: quant output reaches trading only through W3 strategies -> W4 risk"}))
        finally:
            conn.close()

    @app.get("/api/quant/factors")
    async def api_quant_factors(category: str = None):
        return JSONResponse(json_safe([f.meta() for f in FX.FACTORS if not category or f.category == category]))

    @app.get("/api/quant/factors/{fid}")
    async def api_quant_factor(fid: str):
        try:
            f = FX.get(fid)
        except ValueError as e:
            return err(e, 404)
        conn = conn_ready()
        try:
            hist = rows(conn, "SELECT as_of, COUNT(*) n, AVG(raw) mean_raw FROM quant_factor_score WHERE factor_key=? "
                              "GROUP BY as_of ORDER BY as_of DESC LIMIT 30", (f.key,))
            versions = rows(conn, "SELECT factor_id, version, status, content_hash, created_at FROM quant_factor "
                                  "WHERE factor_id=?", (fid,))
            return JSONResponse(json_safe({**f.meta(), "versions": versions, "recent_coverage": hist}))
        finally:
            conn.close()

    @app.get("/api/quant/factor-sets")
    async def api_quant_factor_sets():
        conn = conn_ready()
        try:
            out = rows(conn, "SELECT * FROM quant_factor_set ORDER BY name, version")
            for d in out:
                d["factors"] = json.loads(d.pop("factors_json"))
            return JSONResponse(json_safe(out))
        finally:
            conn.close()

    @app.post("/api/quant/factor-sets", dependencies=guard)
    async def api_quant_factor_set_create(request: Req):
        b = await body(request)
        conn = conn_ready()
        try:
            return JSONResponse(json_safe(FX.save_factor_set(conn, b.get("name"), str(b.get("version") or "1"),
                                                             b.get("factors") or [], b.get("description", ""))))
        except BAD as e:
            return err(e)
        finally:
            conn.close()

    @app.get("/api/quant/composites")
    async def api_quant_composites():
        conn = conn_ready()
        try:
            return JSONResponse(json_safe([CP.get(conn, r[0], r[1]) for r in conn.execute(
                "SELECT name, version FROM quant_composite ORDER BY name, version")]))
        finally:
            conn.close()

    @app.post("/api/quant/composites", dependencies=guard)
    async def api_quant_composite_create(request: Req):
        b = await body(request)
        conn = conn_ready()
        try:
            cd = CP.CompositeDef(b.get("name"), str(b.get("version") or "1"), b.get("components") or [],
                                 float(b.get("min_coverage", 0.6)), b.get("normalization") or
                                 {"method": "percentile", "winsorize": 0}, b.get("description", ""))
            return JSONResponse(json_safe(CP.save(conn, cd)))
        except BAD as e:
            return err(e)
        finally:
            conn.close()

    @app.post("/api/quant/compute", dependencies=guard)
    async def api_quant_compute(request: Req):
        """Factor scores + composites for a date (background; ~all tracked symbols)."""
        b = await body(request)
        conn_ready().close()
        comps = b.get("composites") or settings()["composites"]
        bg(lambda c: EN.compute(c, b.get("as_of"), b.get("factors"), settings()["universe"], True, comps))
        return JSONResponse({"status": "STARTED", "as_of": b.get("as_of") or "latest session"})

    @app.get("/api/quant/scores")
    async def api_quant_scores(as_of: str = None, key: str = None, symbol: str = None, limit: int = 200):
        conn = conn_ready()
        try:
            a = as_of or latest(conn)
            q, args = "SELECT * FROM quant_factor_score WHERE as_of=?", [a]
            if key:
                q += " AND factor_key=?"; args.append(key)
            if symbol:
                q += " AND symbol=?"; args.append(symbol.upper())
            return JSONResponse(json_safe(rows(conn, q + " ORDER BY factor_key, score DESC LIMIT ?", args + [int(limit)])))
        finally:
            conn.close()

    @app.get("/api/quant/rankings")
    async def api_quant_rankings(key: str, as_of: str = None, top: int = None, bottom: int = None,
                                 sector: str = None, by: str = None):
        conn = conn_ready()
        try:
            a = as_of or latest(conn)
            if by == "sector":
                return JSONResponse(json_safe(EN.sector_rankings(conn, a, key)))
            return JSONResponse(json_safe(EN.rankings(conn, a, key, top, bottom, sector)))
        finally:
            conn.close()

    @app.get("/api/quant/strategies")
    async def api_quant_strategies():
        conn = conn_ready()
        try:
            out = []
            for s in rows(conn, "SELECT strategy_id, name, kind, status, current_version FROM strategy"):
                v = conn.execute("SELECT definition_json FROM strategy_version WHERE strategy_id=? AND version=?",
                                 (s["strategy_id"], s["current_version"])).fetchone()
                d = json.loads(v[0]) if v else {}
                if s["kind"] in ("pairs", "portfolio") or "quant" in (d.get("inputs") or []):
                    s["parameters"] = d.get("parameters")
                    s["features_used"] = d.get("features_used")
                    out.append(s)
            return JSONResponse(json_safe(out))
        finally:
            conn.close()

    @app.get("/api/quant/experiments")
    async def api_quant_experiments():
        conn = conn_ready()
        try:
            return JSONResponse(json_safe([EXP.get(conn, r[0]) for r in conn.execute(
                "SELECT experiment_id FROM quant_experiment ORDER BY created_at DESC")]))
        finally:
            conn.close()

    @app.post("/api/quant/experiments", dependencies=guard)
    async def api_quant_experiment_create(request: Req):
        b = await body(request)
        conn = conn_ready()
        try:
            return JSONResponse(json_safe(EXP.create(conn, b)))
        except BAD as e:
            return err(e)
        finally:
            conn.close()

    @app.post("/api/quant/experiments/{eid}/run", dependencies=guard)
    async def api_quant_experiment_run(eid: str):
        conn = conn_ready()
        try:
            if not EXP.get(conn, eid):
                return err(f"no experiment {eid}", 404)
        finally:
            conn.close()
        bg(lambda c: EXP.run(c, eid))
        return JSONResponse({"experiment_id": eid, "status": "STARTED"})

    @app.get("/api/quant/pairs")
    async def api_quant_pairs():
        conn = conn_ready()
        try:
            return JSONResponse(json_safe([PR.get_pair(conn, r[0], r[1]) for r in conn.execute(
                "SELECT pair_id, version FROM quant_pair ORDER BY pair_id, version")]))
        finally:
            conn.close()

    @app.post("/api/quant/pairs", dependencies=guard)
    async def api_quant_pair_create(request: Req):
        b = await body(request)
        conn = conn_ready()
        try:
            return JSONResponse(json_safe(PR.save_pair(conn, b)))
        except BAD as e:
            return err(e)
        finally:
            conn.close()

    @app.post("/api/quant/pairs/screen", dependencies=guard)
    async def api_quant_pairs_screen(request: Req):
        b = await body(request)
        conn = conn_ready()
        try:
            return JSONResponse(json_safe(PR.screen(conn, b.get("symbols") or [], b.get("as_of"),
                                                    int(b.get("lookback", 250)), float(b.get("min_corr", 0.7)))))
        except BAD as e:
            return err(e)
        finally:
            conn.close()

    @app.post("/api/quant/pairs/{pid}/status", dependencies=guard)
    async def api_quant_pair_status(pid: str, request: Req):
        b = await body(request)
        conn = conn_ready()
        try:
            return JSONResponse(json_safe(PR.set_status(conn, pid, str(b.get("version") or "1"), b.get("status"))))
        except BAD as e:
            return err(e)
        finally:
            conn.close()

    @app.get("/api/quant/pairs/{pid}/analyze")
    async def api_quant_pair_analyze(pid: str, as_of: str = None, store: bool = False):
        conn = conn_ready()
        try:
            return JSONResponse(json_safe(PR.analyze_pair(conn, pid, as_of, store=store)))
        except BAD as e:
            return err(e, 404 if str(e).startswith("no ") else 400)
        finally:
            conn.close()

    @app.get("/api/quant/spreads")
    async def api_quant_spreads(pair_id: str = None, limit: int = 100):
        conn = conn_ready()
        try:
            q = "SELECT * FROM quant_spread" + (" WHERE pair_id=?" if pair_id else "") + " ORDER BY as_of DESC LIMIT ?"
            return JSONResponse(json_safe(rows(conn, q, ([pair_id] if pair_id else []) + [int(limit)])))
        finally:
            conn.close()

    @app.get("/api/quant/portfolios")
    async def api_quant_portfolios(limit: int = 20):
        conn = conn_ready()
        try:
            out = rows(conn, "SELECT * FROM quant_portfolio ORDER BY created_at DESC LIMIT ?", (int(limit),))
            for p in out:
                p["spec"] = json.loads(p.pop("spec_json") or "{}")
                p["positions"] = rows(conn, "SELECT symbol, weight, side, sector FROM quant_portfolio_position WHERE "
                                            "portfolio_id=? ORDER BY ABS(weight) DESC", (p["portfolio_id"],))
                e = conn.execute("SELECT exposure_json FROM quant_exposure WHERE portfolio_id=?",
                                 (p["portfolio_id"],)).fetchone()
                p["exposures"] = json.loads(e[0]) if e else None
            return JSONResponse(json_safe(out))
        finally:
            conn.close()

    @app.post("/api/quant/portfolios", dependencies=guard)
    async def api_quant_portfolio_build(request: Req):
        b = await body(request)
        conn = conn_ready()
        try:
            return JSONResponse(json_safe(PF.build(conn, b.pop("as_of", None) or latest(conn), b)))
        except BAD as e:
            return err(e)
        finally:
            conn.close()

    @app.get("/api/quant/research")
    async def api_quant_research(key: str = None, limit: int = 50):
        conn = conn_ready()
        try:
            q = "SELECT * FROM quant_factor_research" + (" WHERE factor_key=?" if key else "") + \
                " ORDER BY created_at DESC LIMIT ?"
            out = rows(conn, q, ([key] if key else []) + [int(limit)])
            for d in out:
                d["result"] = json.loads(d.pop("result_json") or "null")
            return JSONResponse(json_safe(out))
        finally:
            conn.close()

    @app.post("/api/quant/research", dependencies=guard)
    async def api_quant_research_run(request: Req):
        """{key, kind: ic|ic_decay|quantiles|correlation, start, end, horizon?, keys?, as_of?} -- background."""
        b = await body(request)
        kind = b.get("kind", "ic")
        fn = {"ic": lambda c: RS.factor_ic(c, b["key"], b["start"], b["end"], int(b.get("horizon", 5))),
              "ic_decay": lambda c: RS.ic_decay(c, b["key"], b["start"], b["end"]),
              "quantiles": lambda c: RS.quantile_returns(c, b["key"], b["start"], b["end"], int(b.get("horizon", 5))),
              "correlation": lambda c: RS._store(c, "|".join(b["keys"]), "correlation", b["as_of"], b["as_of"],
                                                 RS.factor_correlation(c, b["keys"], b["as_of"]))}.get(kind)
        if fn is None:
            return err("kind must be ic, ic_decay, quantiles or correlation")
        bg(fn)
        return JSONResponse({"status": "STARTED", "kind": kind})

    @app.get("/api/quant/events")
    async def api_quant_events(symbol: str = None, event_type: str = None, limit: int = 200):
        conn = conn_ready()
        try:
            q, args = "SELECT * FROM market_event WHERE 1=1", []
            if symbol:
                q += " AND symbol=?"; args.append(symbol.upper())
            if event_type:
                q += " AND event_type=?"; args.append(event_type.upper())
            return JSONResponse(json_safe({"pending_types": EVT.PENDING_TYPES,
                                           "events": rows(conn, q + " ORDER BY known_at DESC LIMIT ?",
                                                          args + [int(limit)])}))
        finally:
            conn.close()

    @app.post("/api/quant/events/sync", dependencies=guard)
    async def api_quant_events_sync():
        conn = conn_ready()
        try:
            return JSONResponse(json_safe(EVT.sync_events(conn)))
        finally:
            conn.close()

    @app.get("/api/quant/volatility/{symbol}")
    async def api_quant_vol(symbol: str, as_of: str = None):
        from datetime import date
        from backtest.data import PriceHistory
        conn = conn_ready()
        try:
            d = date.fromisoformat(as_of) if as_of else date.today()
            h = PriceHistory.load(conn, [symbol.upper()], d, d, warmup_days=420)
            bars = h.view(d).history(symbol.upper())
            if not bars:
                return err(f"no bars for {symbol}", 404)
            return JSONResponse(json_safe({
                "symbol": symbol.upper(), "as_of": str(bars[-1].date),
                "close_to_close_20": VL.close_to_close(bars, 20), "close_to_close_60": VL.close_to_close(bars, 60),
                "parkinson_20": VL.parkinson(bars, 20), "garman_klass_20": VL.garman_klass(bars, 20),
                "downside_60": VL.downside(bars, 60), "change_20_60": VL.change(bars, 20, 60),
                "regime": VL.regime(bars), "implied": None, "iv_rv_spread": None,
                "note": "implied volatility needs options data (pending)"}))
        finally:
            conn.close()

    @app.get("/api/quant/derivatives")
    async def api_quant_derivatives():
        conn = conn_ready()
        try:
            return JSONResponse(json_safe({"data_status": DV.DATA_STATUS,
                                           "instruments": rows(conn, "SELECT * FROM derivatives_instrument LIMIT 200"),
                                           "quotes": conn.execute("SELECT COUNT(*) FROM derivatives_quote").fetchone()[0]}))
        finally:
            conn.close()

    @app.get("/api/quant/options")
    async def api_quant_options(limit: int = 200):
        conn = conn_ready()
        try:
            return JSONResponse(json_safe({"data_status": DV.DATA_STATUS,
                                           "analytics": rows(conn, "SELECT * FROM options_analytics ORDER BY date DESC "
                                                                   "LIMIT ?", (int(limit),))}))
        finally:
            conn.close()

    @app.get("/api/quant/options/price")
    async def api_quant_option_price(S: float, K: float, days: float, sigma: float = None, r: float = 0.065,
                                     kind: str = "call", price: float = None, q: float = 0.0):
        """Black-Scholes calculator: give sigma for price + greeks, or price for implied vol."""
        if kind not in ("call", "put"):
            return err("kind must be call or put")
        T = days / 365
        out = {"S": S, "K": K, "T_years": T, "r": r, "q": q, "kind": kind}
        if price is not None:
            sigma = DV.implied_vol(price, S, K, T, r, kind, q)
            out["implied_vol"] = sigma
        if sigma:
            out["price"] = DV.bs_price(S, K, T, r, sigma, kind, q)
            out["greeks"] = DV.greeks(S, K, T, r, sigma, kind, q)
        return JSONResponse(json_safe(out))

    @app.get("/api/quant/microstructure")
    async def api_quant_micro(symbol: str = None, date: str = None, limit: int = 200):
        conn = conn_ready()
        try:
            q, args = "SELECT * FROM microstructure_feature WHERE 1=1", []
            if symbol:
                q += " AND symbol=?"; args.append(symbol.upper())
            if date:
                q += " AND date=?"; args.append(date)
            return JSONResponse(json_safe({"pending": MS.PENDING, "features": rows(
                conn, q + " ORDER BY date DESC, symbol LIMIT ?", args + [int(limit)])}))
        finally:
            conn.close()

    @app.get("/quant", response_class=HTMLResponse)
    async def quant_page():
        from dashboard.quant_page import render
        from dashboard.security import token as _tok
        return HTMLResponse(render(_tok()))
