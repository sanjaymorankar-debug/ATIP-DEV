"""
Strategy Engine API (W3) and the /strategies page. Registered from
dashboard/server.py; same conventions as the backtest routes: JSON in/out,
400 for an invalid request, 404 for an unknown strategy, and every route that
changes something needs the X-ATIP-Token header.

    GET  /api/strategies                       list (+ latest backtest, health)
    POST /api/strategies                       create from a definition (DRAFT)
    GET  /api/strategies/catalog               kinds, features, operators, states
    GET  /api/strategies/regime-mapping        regime -> strategies mapping
    PUT  /api/strategies/regime-mapping/{regime}   replace one regime's mapping
    GET  /api/strategies/combined              combined decision across strategies
    GET  /api/strategies/{id}                  detail (definition, versions, history)
    PUT  /api/strategies/{id}                  metadata; a behaviour change is a new version
    POST /api/strategies/{id}/versions         add a version
    POST /api/strategies/{id}/activate | pause | disable | retire | archive
    POST /api/strategies/{id}/lifecycle        any allowed transition {to_state}
    GET  /api/strategies/{id}/health
    GET  /api/strategies/{id}/options          (W40) the paper option positions of an option overlay
    GET  /api/strategies/{id}/options/dry-run  (W40) today's legs + risk check; nothing stored or placed
    GET  /api/strategies/{id}/decisions        stored decisions + intents + runs
    POST /api/strategies/{id}/decisions        generate decisions now
    POST /api/strategies/{id}/backtest         W2 backtest of a stored version

No route here places or authorises an order: position intents are stored
with authorization_status NOT_AUTHORIZED and nothing reads them in W3.
"""

# No `from __future__ import annotations`: FastAPI must see the real Request
# class in handler signatures, and it is only a local name inside register().
import json
import threading

from fastapi.responses import HTMLResponse, JSONResponse


def register(app, guard, Req, get_connection, json_safe):
    from strategy_engine import health as H, lifecycle as L, registry as REG, selection as SEL
    from strategy_engine.decisions import ACTIONS, DECISIONS, RISK_REQUIREMENTS, SIGNAL_ENGINE_EQUIVALENT
    from strategy_engine.definition import COMPOSITE_MODES, KINDS, DefinitionError, specs
    from strategy_engine.features import catalogue
    from strategy_engine.lifecycle import LifecycleError
    from strategy_engine.params import ParamError
    from strategy_engine.registry import RegistryError
    from strategy_engine.rules import OPS
    from strategy_engine.selection import SelectionError

    BAD = (ValueError, KeyError, TypeError, DefinitionError, RegistryError, LifecycleError, ParamError,
           SelectionError)
    _synced = {"done": False}

    def conn_synced():
        conn = get_connection()
        if not _synced["done"]:
            REG.sync_library(conn)
            _synced["done"] = True
        return conn

    def err(e, code=400):
        return JSONResponse({"error": str(e)}, status_code=code)

    def err_for(e):
        return err(e, 404 if "no strategy" in str(e) else 400)

    def not_found(sid):
        return JSONResponse({"error": f"no strategy {sid}"}, status_code=404)

    async def body(request):
        try:
            b = await request.json()
        except Exception:
            return {}
        return b if isinstance(b, dict) else {}

    def _latest_bt(conn, sid, ver):
        r = conn.execute("SELECT run_id, period_label, status, metrics_json, finished_at FROM backtest_run "
                         "WHERE strategy_id=? AND strategy_version=? ORDER BY created_at DESC LIMIT 1", (sid, ver)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["metrics"] = json.loads(d.pop("metrics_json")) if d.get("metrics_json") else None
        return d

    def _latest_health(conn, sid):
        r = conn.execute("SELECT version, as_of, status, metrics_json, issues_json FROM strategy_health "
                         "WHERE strategy_id=? ORDER BY as_of DESC, created_at DESC LIMIT 1", (sid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["issues"] = json.loads(d.pop("issues_json") or "[]")
        d["status_reason"] = json.loads(d.pop("metrics_json") or "{}").get("status_reason")
        return d

    def _latest_run(conn, sid, ver):
        """The latest decision run of the current version: date, status, counts by
        action, and the top-scoring BUY decisions of that date."""
        r = conn.execute("SELECT as_of, status, error, counts_json FROM strategy_decision_run WHERE strategy_id=? "
                         "AND version=? ORDER BY as_of DESC, created_at DESC LIMIT 1", (sid, ver)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["counts"] = json.loads(d.pop("counts_json") or "{}")
        d["top"] = [dict(x) for x in conn.execute(
            "SELECT symbol, decision, score FROM strategy_decision WHERE strategy_id=? AND version=? AND as_of=? "
            "AND decision IN ('BUY','SELL','EXIT') ORDER BY score DESC, symbol LIMIT 5", (sid, ver, str(d["as_of"])))]
        return d

    # -- collection routes (registered before /{sid}) ------------------------

    @app.get("/api/strategies")
    async def api_strategies():
        conn = conn_synced()
        try:
            out = []
            for s in REG.list_strategies(conn):
                s["latest_backtest"] = _latest_bt(conn, s["strategy_id"], s["current_version"])
                s["health"] = _latest_health(conn, s["strategy_id"])
                s["latest_run"] = _latest_run(conn, s["strategy_id"], s["current_version"])
                out.append(s)
            return JSONResponse(json_safe(out))
        finally:
            conn.close()

    @app.post("/api/strategies", dependencies=guard)
    async def api_strategy_create(request: Req):
        defn = await body(request)
        conn = conn_synced()
        try:
            p = getattr(request.state, "principal", None) or {}
            actor = p.get("user_id") if p.get("user_id") and p.get("user_id") != "legacy" else "owner"
            return JSONResponse(json_safe(REG.create_strategy(conn, defn, actor=actor)))
        except BAD as e:
            return err(e)
        finally:
            conn.close()

    @app.get("/api/strategies/catalog")
    async def api_strategy_catalog():
        return JSONResponse(json_safe({
            "kinds": KINDS, "composite_modes": COMPOSITE_MODES, "features": catalogue(), "operators": OPS,
            "decisions": DECISIONS, "actions": ACTIONS, "signal_engine_equivalent": SIGNAL_ENGINE_EQUIVALENT,
            "risk_requirements": RISK_REQUIREMENTS, "lifecycle_states": L.STATES,
            "lifecycle_transitions": {k: sorted(v) for k, v in L.TRANSITIONS.items()},
            "decision_states": L.DECISION_STATES, "health_statuses": H.STATUSES,
            "regimes": SEL.MAP_KEYS, "combination_modes": SEL.MODES}))

    @app.get("/api/strategies/regime-mapping")
    async def api_regime_mapping(regime: str = None):
        conn = conn_synced()
        try:
            out = {"mapping": SEL.get_mapping(conn)}
            if regime:
                out["selected"] = SEL.selected_strategies(conn, regime.upper())
            return JSONResponse(json_safe(out))
        finally:
            conn.close()

    @app.put("/api/strategies/regime-mapping/{regime}", dependencies=guard)
    async def api_regime_mapping_set(regime: str, request: Req):
        """Body: {"entries": "NO_TRADE"} or {"entries": [{strategy_id, priority, weight, enabled}]}."""
        b = await body(request)
        conn = conn_synced()
        try:
            return JSONResponse(json_safe(SEL.set_mapping(conn, regime, b.get("entries"))))
        except BAD as e:
            return err(e)
        finally:
            conn.close()

    @app.get("/api/strategies/combined")
    async def api_strategies_combined(as_of: str = None, mode: str = "vote", threshold: float = 0.3,
                                      min_votes: int = 1):
        conn = conn_synced()
        try:
            return JSONResponse(json_safe(SEL.combined_decisions(conn, as_of, mode, threshold, min_votes)))
        except BAD as e:
            return err(e)
        finally:
            conn.close()

    # -- one strategy ------------------------------------------------------------

    @app.get("/api/strategies/{sid}")
    async def api_strategy_get(sid: str):
        conn = conn_synced()
        try:
            s = REG.get_strategy(conn, sid)
            if not s:
                return not_found(sid)
            s["definition"] = (REG.get_version(conn, sid) or {}).get("definition")
            s["versions"] = REG.list_versions(conn, sid)
            s["lifecycle"] = L.history(conn, sid)
            s["events"] = L.history(conn, sid, event_type=None, limit=50)
            s["allowed_transitions"] = sorted(L.TRANSITIONS.get(s["status"], set()))
            s["health"] = _latest_health(conn, sid)
            s["backtests"] = [dict(r) for r in conn.execute(
                "SELECT run_id, strategy_version, kind, period_label, start_date, end_date, status, metrics_json, "
                "created_at FROM backtest_run WHERE strategy_id=? ORDER BY created_at DESC LIMIT 25", (sid,))]
            for b in s["backtests"]:          # W39: decoded beside the text the v1 contract published (additive)
                try:
                    b["metrics"] = json.loads(b["metrics_json"]) if b.get("metrics_json") else None
                except ValueError:
                    b["metrics"] = None
            return JSONResponse(json_safe(s))
        finally:
            conn.close()

    async def _update(sid, request):
        b = await body(request)
        conn = conn_synced()
        try:
            return JSONResponse(json_safe(REG.update_metadata(conn, sid, b)))
        except BAD as e:
            return err_for(e)
        finally:
            conn.close()

    @app.put("/api/strategies/{sid}", dependencies=guard)
    async def api_strategy_put(sid: str, request: Req):
        """Metadata only: name, description, category, owner, priority, weight.
        Behaviour (rules, parameters, kind) changes are new versions."""
        return await _update(sid, request)

    @app.post("/api/strategies/{sid}", dependencies=guard)
    async def api_strategy_update(sid: str, request: Req):
        """Same as PUT (kept for clients that cannot send PUT)."""
        return await _update(sid, request)

    @app.post("/api/strategies/{sid}/versions", dependencies=guard)
    async def api_strategy_add_version(sid: str, request: Req):
        defn = await body(request)
        if defn.get("strategy_id") != sid:
            return err("definition strategy_id must match the URL")
        notes = defn.pop("notes", "")
        conn = conn_synced()
        try:
            return JSONResponse(json_safe(REG.add_version(conn, defn, notes=notes)))
        except BAD as e:
            return err(e)
        finally:
            conn.close()

    @app.get("/api/strategies/{sid}/versions/{version}")
    async def api_strategy_version(sid: str, version: str):
        conn = conn_synced()
        try:
            v = REG.get_version(conn, sid, version)
            return JSONResponse(json_safe(v)) if v else err(f"no version {version} of {sid}", 404)
        finally:
            conn.close()

    @app.post("/api/strategies/{sid}/current-version", dependencies=guard)
    async def api_strategy_set_current(sid: str, request: Req):
        b = await body(request)
        conn = conn_synced()
        try:
            return JSONResponse(json_safe(REG.set_current_version(conn, sid, b.get("version"))))
        except BAD as e:
            return err(e)
        finally:
            conn.close()

    @app.get("/api/strategies/{sid}/parameters")
    async def api_strategy_params(sid: str, version: str = None):
        conn = conn_synced()
        try:
            v = REG.get_version(conn, sid, version)
            if not v:
                return not_found(sid)
            return JSONResponse(json_safe({"strategy_id": sid, "version": v["version"],
                                           "parameters": [p.as_dict() for p in specs(v["definition"])]}))
        finally:
            conn.close()

    async def _move(sid, to_state, request):
        b = await body(request)
        conn = conn_synced()
        try:
            return JSONResponse(json_safe(L.transition(conn, sid, to_state or b.get("to_state"),
                                                       b.get("reason", ""), b.get("evidence"))))
        except BAD as e:
            return err_for(e)
        finally:
            conn.close()

    @app.post("/api/strategies/{sid}/lifecycle", dependencies=guard)
    async def api_strategy_transition(sid: str, request: Req):
        """{to_state, reason, evidence}: any transition lifecycle.TRANSITIONS allows."""
        return await _move(sid, None, request)

    @app.post("/api/strategies/{sid}/activate", dependencies=guard)
    async def api_strategy_activate(sid: str, request: Req):
        """-> ACTIVE (needs a completed backtest of the current version). ACTIVE
        means daily decisions against the LIVE book -- intents only, never orders."""
        return await _move(sid, "ACTIVE", request)

    @app.post("/api/strategies/{sid}/pause", dependencies=guard)
    async def api_strategy_pause(sid: str, request: Req):
        return await _move(sid, "PAUSED", request)

    @app.post("/api/strategies/{sid}/disable", dependencies=guard)
    async def api_strategy_disable(sid: str, request: Req):
        return await _move(sid, "DISABLED", request)

    @app.post("/api/strategies/{sid}/retire", dependencies=guard)
    async def api_strategy_retire(sid: str, request: Req):
        return await _move(sid, "RETIRED", request)

    @app.post("/api/strategies/{sid}/archive", dependencies=guard)
    async def api_strategy_archive(sid: str, request: Req):
        return await _move(sid, "ARCHIVED", request)

    @app.post("/api/strategies/{sid}/decisions", dependencies=guard)
    async def api_strategy_decide(sid: str, request: Req):
        """{version?, as_of?, book: PAPER|LIVE, store: true}. Decisions and
        NOT_AUTHORIZED intents only -- never orders."""
        from strategy_engine.engine import generate_decisions
        b = await body(request)
        try:
            conn_synced().close()
            out = generate_decisions(sid, b.get("version"), b.get("as_of"), b.get("book", "PAPER"),
                                     bool(b.get("store", True)))
        except BAD as e:
            return err_for(e)
        return JSONResponse(json_safe(out))

    @app.get("/api/strategies/{sid}/decisions")
    async def api_strategy_decisions(sid: str, as_of: str = None, limit: int = 200, include_wait: bool = False):
        conn = conn_synced()
        try:
            if not REG.get_strategy(conn, sid):
                return not_found(sid)
            q = "SELECT * FROM strategy_decision WHERE strategy_id=?"
            args = [sid]
            if as_of:
                q += " AND as_of=?"; args.append(as_of)
            if not include_wait:
                q += " AND decision<>'WAIT'"
            q += " ORDER BY as_of DESC, decision, symbol LIMIT ?"; args.append(int(limit))
            rows = []
            for r in conn.execute(q, args):
                d = dict(r)
                d["reasons"] = json.loads(d.pop("reasons_json") or "[]")
                d["parameters"] = json.loads(d.pop("parameters_json") or "{}")
                d["features"] = json.loads(d.pop("features_json") or "{}")
                rows.append(d)
            qi = "SELECT * FROM strategy_position_intent WHERE strategy_id=?"
            ai = [sid]
            if as_of:
                qi += " AND as_of=?"; ai.append(as_of)
            qi += " ORDER BY as_of DESC, symbol LIMIT ?"; ai.append(int(limit))
            intents = [dict(r) for r in conn.execute(qi, ai)]
            runs = [dict(r) for r in conn.execute("SELECT * FROM strategy_decision_run WHERE strategy_id=? "
                                                  "ORDER BY as_of DESC, created_at DESC LIMIT 20", (sid,))]
            return JSONResponse(json_safe({"decisions": rows, "intents": intents, "runs": runs}))
        finally:
            conn.close()

    @app.get("/api/strategies/{sid}/options")
    async def api_strategy_options(sid: str, status: str = None, limit: int = 50):
        """W40 (ENT-15): the strategy's paper option positions (option overlays) with legs and daily marks."""
        from execution.options_paper import strategy_book, strategy_positions
        conn = conn_synced()
        try:
            if not REG.get_strategy(conn, sid):
                return not_found(sid)
            st = (status or "").upper() or None
            if st and st not in ("OPEN", "CLOSED", "SETTLED"):
                return err("status must be OPEN, CLOSED or SETTLED")
            return JSONResponse(json_safe({"strategy_id": sid, "book": strategy_book(conn, sid),
                                           "positions": strategy_positions(conn, sid, st, max(1, min(int(limit), 500)))}))
        finally:
            conn.close()

    @app.get("/api/strategies/{sid}/options/dry-run")
    async def api_strategy_options_dry_run(sid: str, as_of: str = None, version: str = None):
        """W40 (ENT-15): an option overlay's legs and risk check for today (or as_of) -- nothing stored,
        nothing placed (strategy_engine/option_overlay.dry_run)."""
        from strategy_engine.option_overlay import dry_run
        conn = conn_synced()
        try:
            if not REG.get_strategy(conn, sid):
                return not_found(sid)
            return JSONResponse(json_safe(dry_run(conn, sid, as_of, version)))
        except BAD as e:
            return err_for(e)
        finally:
            conn.close()

    @app.get("/api/strategies/{sid}/health")
    async def api_strategy_health(sid: str, version: str = None):
        conn = conn_synced()
        try:
            return JSONResponse(json_safe(H.compute_health(conn, sid, version)))
        except BAD as e:
            return err_for(e)
        finally:
            conn.close()

    @app.post("/api/strategies/{sid}/backtest", dependencies=guard)
    async def api_strategy_backtest(sid: str, request: Req):
        """A W2 backtest of a stored version. Body = a W2 backtest request without
        strategy_id; version defaults to the current one. Runs in the background."""
        from backtest import service as bts
        b = await body(request)
        conn = conn_synced()
        try:
            s = REG.get_strategy(conn, sid)
        finally:
            conn.close()
        if not s:
            return not_found(sid)
        req = {k: v for k, v in b.items() if k != "version"}
        req.update({"strategy_id": sid, "strategy_version": b.get("version") or s["current_version"]})
        try:
            run_id = bts.create(req)
        except BAD as e:
            return err(e)
        threading.Thread(target=bts.execute, args=(run_id,), daemon=True).start()
        return JSONResponse({"run_id": run_id, "status": "RUNNING", "strategy_version": req["strategy_version"]})

    @app.get("/strategies", response_class=HTMLResponse)
    async def strategies_page():
        from dashboard.security import token as _tok
        from dashboard.strategy_page import render
        return HTMLResponse(render(_tok()))
