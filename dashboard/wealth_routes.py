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

    W12 Multi-asset wealth
    GET  /api/wealth/assets                 asset-class registry (supported / EXCLUDED classes)
    GET  /api/wealth/summary                net worth, by class / source / sector, concentration,
                                            liquidity, ATIP risk overlay, health, reconciliation
    GET  /api/wealth/positions              normalized positions (broker + manual + paper)
    GET  /api/wealth/holdings               manual holdings ; POST (token) create
    GET  /api/wealth/holdings/{id}          ; PUT (token) update ; DELETE (token) close (kept, status CLOSED)
    GET  /api/wealth/liabilities            ; POST (token) ; PUT / DELETE /{id} (token)
    GET  /api/wealth/classifications        symbol overrides ; PUT (token) {symbol, asset_class, instrument}
    DELETE /api/wealth/classifications/{symbol}  (token)
    GET  /api/wealth/snapshots?days=        net-worth history ; POST (token) record today's

    W13 Goal planning
    GET  /api/wealth/goals                  active goals with evaluation (?include_inactive=true)
    POST /api/wealth/goals                  (token) create
    GET  /api/wealth/goals/overview         totals, status counts, required return, link warnings
    GET  /api/wealth/goals/{id}             goal + evaluation (projection, gap, required SIP / CAGR,
                                            Monte Carlo, scenarios, glide path)
    PUT  /api/wealth/goals/{id}             (token) update ; DELETE (token) -> ABANDONED
    POST /api/wealth/goals/{id}/status      (token) {status}
    POST /api/wealth/goals/{id}/simulate    (token) {overrides} what-if, not saved
    POST /api/wealth/goals/{id}/projection  (token) store today's evaluation
    GET  /api/wealth/goals/{id}/projections stored evaluations ; GET .../events  change history
    POST /api/wealth/dna/refresh            (token) re-score the current answers (risk requirement
                                            from goals) as a new version

    W14 Asset allocation
    GET  /api/wealth/allocation             latest stored run (404 if none)
    POST /api/wealth/allocation/run         (token) compute + store a run
    POST /api/wealth/allocation/preview     (token) compute, not stored
    GET  /api/wealth/allocation/runs        ; GET /api/wealth/allocation/runs/{id}
    GET  /api/wealth/allocation/signals     tactical signals now (ATIP market intelligence)
    GET  /api/wealth/allocation/cma         capital market assumptions + model portfolios + bounds
    GET  /api/wealth/allocation/policy      ; PUT (token) {bounds, excluded_classes, tactical_enabled, max_tilt_pct}

    GET  /wealth                            page
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
from fastapi.responses import HTMLResponse, JSONResponse


def register(app, guard, Req, get_connection, json_safe):
    from wealth import allocation as AL
    from wealth import assets as AS
    from wealth import common as C
    from wealth import dna as DNA
    from wealth import goals as G
    from wealth import holdings as H

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

    @app.post("/api/wealth/dna/refresh", dependencies=guard)
    async def api_wealth_dna_refresh(request: Req):
        def f(conn):
            p = DNA.refresh_requirement(conn, owner(request), actor(request))
            if not p:
                raise LookupError("no investor profile yet")
            return p
        return run(f)

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

    # ── W12 Multi-asset wealth ──────────────────────────────────────────────
    @app.get("/api/wealth/assets")
    async def api_wealth_assets():
        return JSONResponse(AS.registry())

    @app.get("/api/wealth/summary")
    async def api_wealth_summary(request: Req, positions: bool = False):
        def f(conn):
            s = H.summary(conn, owner(request))
            if not positions:
                s.pop("positions")
            return s
        return run(f)

    @app.get("/api/wealth/positions")
    async def api_wealth_positions(request: Req):
        def f(conn):
            pos, recon = H.positions(conn, owner(request))
            return {"positions": pos, "reconciliation": recon}
        return run(f)

    @app.get("/api/wealth/holdings")
    async def api_wealth_holdings(request: Req, include_closed: bool = False):
        return run(lambda conn: H.list_holdings(conn, owner(request), include_closed))

    @app.post("/api/wealth/holdings", dependencies=guard)
    async def api_wealth_holding_add(request: Req):
        b = await body(request)
        return run(lambda conn: H.add_holding(conn, owner(request), b, actor(request)))

    @app.get("/api/wealth/holdings/{hid}")
    async def api_wealth_holding(hid: str, request: Req):
        return run(lambda conn: H.get_holding(conn, owner(request), hid))

    @app.put("/api/wealth/holdings/{hid}", dependencies=guard)
    async def api_wealth_holding_update(hid: str, request: Req):
        b = await body(request)
        return run(lambda conn: H.update_holding(conn, owner(request), hid, b, actor(request)))

    @app.delete("/api/wealth/holdings/{hid}", dependencies=guard)
    async def api_wealth_holding_close(hid: str, request: Req):
        return run(lambda conn: H.close_holding(conn, owner(request), hid, actor(request)))

    @app.get("/api/wealth/liabilities")
    async def api_wealth_liabilities(request: Req):
        return run(lambda conn: H.list_liabilities(conn, owner(request)))

    @app.post("/api/wealth/liabilities", dependencies=guard)
    async def api_wealth_liability_add(request: Req):
        b = await body(request)
        return run(lambda conn: H.add_liability(conn, owner(request), b, actor(request)))

    @app.put("/api/wealth/liabilities/{lid}", dependencies=guard)
    async def api_wealth_liability_update(lid: str, request: Req):
        b = await body(request)
        return run(lambda conn: H.update_liability(conn, owner(request), lid, b, actor(request)))

    @app.delete("/api/wealth/liabilities/{lid}", dependencies=guard)
    async def api_wealth_liability_close(lid: str, request: Req):
        return run(lambda conn: H.close_liability(conn, owner(request), lid, actor(request)))

    @app.get("/api/wealth/classifications")
    async def api_wealth_classifications(request: Req):
        return run(lambda conn: H.overrides(conn, owner(request)))

    @app.put("/api/wealth/classifications", dependencies=guard)
    async def api_wealth_classification_set(request: Req):
        b = await body(request)
        return run(lambda conn: H.set_classification(conn, owner(request), b.get("symbol"), b.get("asset_class"),
                                                     b.get("instrument") or "ETF"))

    @app.delete("/api/wealth/classifications/{symbol}", dependencies=guard)
    async def api_wealth_classification_clear(symbol: str, request: Req):
        return run(lambda conn: H.clear_classification(conn, owner(request), symbol))

    @app.get("/api/wealth/snapshots")
    async def api_wealth_snapshots(request: Req, days: int = 730):
        return run(lambda conn: H.snapshots(conn, owner(request), max(1, min(days, 3650))))

    @app.post("/api/wealth/snapshots", dependencies=guard)
    async def api_wealth_snapshot_record(request: Req):
        return run(lambda conn: H.record_snapshot(conn, owner(request)))

    # ── W13 Goal planning ───────────────────────────────────────────────────
    @app.get("/api/wealth/goals")
    async def api_wealth_goals(request: Req, include_inactive: bool = False):
        return run(lambda conn: G.list_goals(conn, owner(request), include_inactive))

    @app.post("/api/wealth/goals", dependencies=guard)
    async def api_wealth_goal_create(request: Req):
        b = await body(request)
        return run(lambda conn: G.create(conn, owner(request), b, actor(request)))

    @app.get("/api/wealth/goals/overview")
    async def api_wealth_goals_overview(request: Req):
        return run(lambda conn: G.overview(conn, owner(request)))

    @app.get("/api/wealth/goals/{gid}")
    async def api_wealth_goal(gid: str, request: Req):
        return run(lambda conn: G.get(conn, owner(request), gid))

    @app.put("/api/wealth/goals/{gid}", dependencies=guard)
    async def api_wealth_goal_update(gid: str, request: Req):
        b = await body(request)
        return run(lambda conn: G.update(conn, owner(request), gid, b, actor(request)))

    @app.delete("/api/wealth/goals/{gid}", dependencies=guard)
    async def api_wealth_goal_abandon(gid: str, request: Req):
        return run(lambda conn: G.set_status(conn, owner(request), gid, "ABANDONED", actor(request)))

    @app.post("/api/wealth/goals/{gid}/status", dependencies=guard)
    async def api_wealth_goal_status(gid: str, request: Req):
        b = await body(request)
        return run(lambda conn: G.set_status(conn, owner(request), gid, b.get("status"), actor(request)))

    @app.post("/api/wealth/goals/{gid}/simulate", dependencies=guard)
    async def api_wealth_goal_simulate(gid: str, request: Req):
        b = await body(request)
        return run(lambda conn: G.simulate(conn, owner(request), gid, b.get("overrides") or {}))

    @app.post("/api/wealth/goals/{gid}/projection", dependencies=guard)
    async def api_wealth_goal_projection(gid: str, request: Req):
        return run(lambda conn: G.record_projection(conn, owner(request), gid))

    @app.get("/api/wealth/goals/{gid}/projections")
    async def api_wealth_goal_projections(gid: str, request: Req):
        return run(lambda conn: G.projections(conn, owner(request), gid))

    @app.get("/api/wealth/goals/{gid}/events")
    async def api_wealth_goal_events(gid: str, request: Req):
        return run(lambda conn: G.events(conn, owner(request), gid))

    # ── W14 Asset allocation ────────────────────────────────────────────────
    @app.get("/api/wealth/allocation")
    async def api_wealth_allocation(request: Req):
        def f(conn):
            r = AL.latest(conn, owner(request))
            if not r:
                raise LookupError("no allocation run yet (POST /api/wealth/allocation/run)")
            return r
        return run(f)

    @app.post("/api/wealth/allocation/run", dependencies=guard)
    async def api_wealth_allocation_run(request: Req):
        return run(lambda conn: AL.run(conn, owner(request), actor(request)))

    @app.post("/api/wealth/allocation/preview", dependencies=guard)
    async def api_wealth_allocation_preview(request: Req):
        return run(lambda conn: AL.compute(conn, owner(request)))

    @app.get("/api/wealth/allocation/runs")
    async def api_wealth_allocation_runs(request: Req, limit: int = 50):
        return run(lambda conn: AL.runs(conn, owner(request), max(1, min(limit, 500))))

    @app.get("/api/wealth/allocation/runs/{rid}")
    async def api_wealth_allocation_get(rid: str, request: Req):
        return run(lambda conn: AL.get_run(conn, owner(request), rid))

    @app.get("/api/wealth/allocation/signals")
    async def api_wealth_allocation_signals():
        return run(lambda conn: AL.signals(conn))

    @app.get("/api/wealth/allocation/cma")
    async def api_wealth_allocation_cma():
        c = AL.cma()
        return JSONResponse({"returns": c["returns"], "volatility": c["volatility"], "source": c["source"],
                             "correlation": {f"{a}/{b}": v for (a, b), v in c["correlation"].items()},
                             "models": AL.MODELS, "bounds": AL.BOUNDS, "signal_weights": AL.SIGNAL_WEIGHTS,
                             "methodology_version": AL.METHODOLOGY_VERSION})

    @app.get("/api/wealth/allocation/policy")
    async def api_wealth_allocation_policy(request: Req):
        return run(lambda conn: AL.policy(conn, owner(request)))

    @app.put("/api/wealth/allocation/policy", dependencies=guard)
    async def api_wealth_allocation_policy_set(request: Req):
        b = await body(request)
        return run(lambda conn: AL.set_policy(conn, owner(request), b))

    # ── Page ────────────────────────────────────────────────────────────────
    @app.get("/wealth", response_class=HTMLResponse)
    async def wealth_page():
        from dashboard.security import token as _tok
        from dashboard.wealth_page import render
        return HTMLResponse(render(_tok()))
