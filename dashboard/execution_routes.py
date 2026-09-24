"""
W4 API: decisions, intents, risk, orders, executions, positions, audit -- and
the /trading page. Registered from dashboard/server.py with the same
conventions as the strategy routes (JSON, 400 invalid, 404 unknown, X-ATIP-Token
on anything that changes state).

The existing /api/orders* routes belong to the W1-era order rules and are
unchanged; W4 orders live under /api/oms/*.

    GET  /api/strategy-decisions            all strategies' decisions (filters)
    GET  /api/position-intents              intents with authorization status
    GET  /api/risk/limits                   effective limits + source
    PUT  /api/risk/limits                   {key: value | null | "default"}
    GET  /api/risk/decisions                risk decisions (filters)
    GET  /api/risk/decisions/{id}
    POST /api/risk/decisions/{id}/approve   REVIEW_REQUIRED -> APPROVED
    POST /api/risk/evaluate                 {intent_ids?} evaluate without executing
    GET  /api/risk/exposure                 equity, positions, sector, strategy exposure
    GET  /api/oms/orders   GET /api/oms/orders/{id}
    POST /api/oms/orders                    {risk_decision_id} create + submit (PAPER)
    POST /api/oms/orders/{id}/submit | cancel | refresh
    GET  /api/oms/executions   GET /api/oms/fills   GET /api/oms/positions
    GET  /api/execution/status              mode, live gate, auto-execute, adapter
    POST /api/execution/run                 {execute?, as_of?} one cycle
    GET  /api/audit/intent/{id}   GET /api/audit/order/{id}
    GET  /trading                           page
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
import json

from fastapi.responses import HTMLResponse, JSONResponse


def register(app, guard, Req, get_connection, json_safe):
    from execution import audit as AU, config as CF, order_manager as OM, pipeline as PL, positions as PO
    from execution import risk_engine as RE
    from execution.errors import ExecutionError
    from execution.models import ORDER_STATES, ORDER_TRANSITIONS, RISK_STATUSES

    def err(e, code=400):
        return JSONResponse({"error": str(e)}, status_code=code)

    def err_for(e):
        return err(e, 404 if str(e).startswith("no ") else 400)

    async def body(request):
        try:
            b = await request.json()
        except Exception:
            return {}
        return b if isinstance(b, dict) else {}

    def rows(conn, q, args):
        return [dict(r) for r in conn.execute(q, args)]

    def where(filters):
        parts, args = [], []
        for col, val in filters:
            if val not in (None, ""):
                parts.append(f"{col}=?"); args.append(val)
        return (" WHERE " + " AND ".join(parts)) if parts else "", args

    # -- decisions & intents --------------------------------------------------------
    @app.get("/api/strategy-decisions")
    async def api_all_decisions(as_of: str = None, strategy_id: str = None, decision: str = None,
                                limit: int = 200, include_wait: bool = False):
        conn = get_connection()
        try:
            w, a = where([("as_of", as_of), ("strategy_id", strategy_id), ("decision", decision)])
            if not include_wait and not decision:
                w += (" AND " if w else " WHERE ") + "decision<>'WAIT'"
            out = []
            for d in rows(conn, f"SELECT * FROM strategy_decision{w} ORDER BY as_of DESC, strategy_id, symbol "
                                f"LIMIT ?", a + [int(limit)]):
                for k in ("reasons_json", "parameters_json", "features_json", "reason_codes_json"):
                    d[k.replace("_json", "")] = json.loads(d.pop(k) or "null")
                out.append(d)
            return JSONResponse(json_safe(out))
        finally:
            conn.close()

    @app.get("/api/position-intents")
    async def api_intents(status: str = None, as_of: str = None, strategy_id: str = None, limit: int = 200):
        conn = get_connection()
        try:
            w, a = where([("authorization_status", status), ("as_of", as_of), ("strategy_id", strategy_id)])
            return JSONResponse(json_safe(rows(conn, f"SELECT * FROM strategy_position_intent{w} "
                                                     f"ORDER BY as_of DESC, created_at DESC LIMIT ?", a + [int(limit)])))
        finally:
            conn.close()

    # -- risk ---------------------------------------------------------------------
    @app.get("/api/risk/limits")
    async def api_risk_limits():
        conn = get_connection()
        try:
            return JSONResponse(json_safe({"limits": CF.risk_limits(conn),
                                           "history": rows(conn, "SELECT * FROM risk_limit_history ORDER BY id DESC "
                                                                 "LIMIT 50", [])}))
        finally:
            conn.close()

    @app.put("/api/risk/limits", dependencies=guard)
    async def api_risk_limits_put(request: Req):
        b = await body(request)
        note = b.pop("_note", "")
        conn = get_connection()
        try:
            return JSONResponse(json_safe(CF.set_risk_limits(conn, b, note=note)))
        except ExecutionError as e:
            return err(e)
        finally:
            conn.close()

    @app.get("/api/risk/decisions")
    async def api_risk_decisions(status: str = None, strategy_id: str = None, symbol: str = None, limit: int = 200):
        conn = get_connection()
        try:
            w, a = where([("risk_status", status), ("strategy_id", strategy_id), ("symbol", symbol)])
            out = []
            for d in rows(conn, f"SELECT * FROM risk_decision{w} ORDER BY created_at DESC LIMIT ?", a + [int(limit)]):
                d["risk_checks"] = json.loads(d.pop("risk_checks_json") or "[]")
                d.pop("limits_json", None)
                out.append(d)
            return JSONResponse(json_safe(out))
        finally:
            conn.close()

    @app.get("/api/risk/decisions/{rid}")
    async def api_risk_decision(rid: str):
        conn = get_connection()
        try:
            d = RE.get_decision(conn, rid)
            return JSONResponse(json_safe(d)) if d else err(f"no risk decision {rid}", 404)
        finally:
            conn.close()

    @app.post("/api/risk/decisions/{rid}/approve", dependencies=guard)
    async def api_risk_approve(rid: str):
        conn = get_connection()
        try:
            return JSONResponse(json_safe(RE.approve_review(conn, rid)))
        except ExecutionError as e:
            return err_for(e)
        finally:
            conn.close()

    @app.post("/api/risk/evaluate", dependencies=guard)
    async def api_risk_evaluate(request: Req):
        """{intent_ids?: [...], as_of?} -- risk only, never orders."""
        b = await body(request)
        conn = get_connection()
        try:
            ids = b.get("intent_ids") or PL.pending_intents(conn, b.get("as_of"))
            out = []
            for iid in ids:
                try:
                    out.append(RE.evaluate(conn, iid).as_dict())
                except ExecutionError as e:
                    out.append({"intent_id": iid, "error": str(e)})
            return JSONResponse(json_safe(out))
        finally:
            conn.close()

    @app.get("/api/risk/exposure")
    async def api_risk_exposure():
        conn = get_connection()
        try:
            return JSONResponse(json_safe({**PO.exposure(conn), "limits": CF.limit_values(conn)}))
        except Exception as e:
            return err(e)
        finally:
            conn.close()

    # -- orders -------------------------------------------------------------------
    @app.get("/api/oms/orders")
    async def api_oms_orders(status: str = None, strategy_id: str = None, symbol: str = None, limit: int = 200):
        conn = get_connection()
        try:
            w, a = where([("status", status), ("strategy_id", strategy_id), ("symbol", symbol)])
            return JSONResponse(json_safe(rows(conn, f"SELECT * FROM oms_order{w} ORDER BY created_at DESC LIMIT ?",
                                               a + [int(limit)])))
        finally:
            conn.close()

    @app.get("/api/oms/orders/{oid}")
    async def api_oms_order(oid: str):
        conn = get_connection()
        try:
            return JSONResponse(json_safe(OM.order_detail(conn, oid)))
        except ExecutionError as e:
            return err_for(e)
        finally:
            conn.close()

    @app.post("/api/oms/orders", dependencies=guard)
    async def api_oms_create(request: Req):
        """{risk_decision_id, submit?: true}. PAPER only in W4."""
        b = await body(request)
        conn = get_connection()
        try:
            o = OM.create_order(conn, b.get("risk_decision_id"))
            if b.get("submit", True):
                o = OM.submit_order(conn, o["order_id"])
            return JSONResponse(json_safe(o))
        except ExecutionError as e:
            return err_for(e)
        finally:
            conn.close()

    def _order_action(fn):
        async def handler(oid: str):
            conn = get_connection()
            try:
                return JSONResponse(json_safe(fn(conn, oid)))
            except ExecutionError as e:
                return err_for(e)
            finally:
                conn.close()
        handler.__name__ = f"api_oms_{fn.__name__}"        # unique operation ids
        return handler

    app.post("/api/oms/orders/{oid}/submit", dependencies=guard)(_order_action(OM.submit_order))
    app.post("/api/oms/orders/{oid}/cancel", dependencies=guard)(_order_action(OM.cancel_order))
    app.post("/api/oms/orders/{oid}/refresh", dependencies=guard)(_order_action(OM.refresh_order))

    @app.get("/api/oms/executions")
    async def api_oms_executions(order_id: str = None, limit: int = 200):
        conn = get_connection()
        try:
            w, a = where([("order_id", order_id)])
            return JSONResponse(json_safe(rows(conn, f"SELECT * FROM oms_execution{w} ORDER BY at DESC LIMIT ?",
                                               a + [int(limit)])))
        finally:
            conn.close()

    @app.get("/api/oms/fills")
    async def api_oms_fills(strategy_id: str = None, symbol: str = None, limit: int = 200):
        conn = get_connection()
        try:
            w, a = where([("strategy_id", strategy_id), ("symbol", symbol)])
            return JSONResponse(json_safe(rows(conn, f"SELECT * FROM oms_fill{w} ORDER BY filled_at DESC LIMIT ?",
                                               a + [int(limit)])))
        finally:
            conn.close()

    @app.get("/api/oms/positions")
    async def api_oms_positions():
        conn = get_connection()
        try:
            b = PO.book(conn)
            return JSONResponse(json_safe({"book": "PAPER", "equity": b["equity"], "cash": b["cash"],
                                           "positions": b["positions"],
                                           "by_strategy": PO.strategy_positions(conn)}))
        except Exception as e:
            return err(e)
        finally:
            conn.close()

    # -- execution control & audit ------------------------------------------------------
    @app.get("/api/execution/status")
    async def api_execution_status():
        ok, why = CF.live_gate()
        conn = get_connection()
        try:
            pending = conn.execute("SELECT COUNT(*) FROM strategy_position_intent "
                                   "WHERE authorization_status='NOT_AUTHORIZED'").fetchone()[0]
        finally:
            conn.close()
        try:
            from orders.risk import halted
            h, hwhy = halted()
        except Exception as e:
            h, hwhy = True, str(e)
        return JSONResponse(json_safe({"settings": CF.execution_settings(), "live_allowed": ok, "live_gate": why,
                                       "live_adapter": "refuses every call in W4", "kill_switch": h,
                                       "kill_switch_reason": hwhy, "pending_intents": pending,
                                       "risk_statuses": RISK_STATUSES, "order_states": ORDER_STATES,
                                       "order_transitions": {k: sorted(v) for k, v in ORDER_TRANSITIONS.items()}}))

    @app.post("/api/execution/run", dependencies=guard)
    async def api_execution_run(request: Req):
        """{execute?: bool, as_of?}. execute false = risk only; true = also send
        APPROVED orders to the PAPER adapter."""
        b = await body(request)
        return JSONResponse(json_safe(PL.run_execution_cycle(execute=b.get("execute"), as_of=b.get("as_of"))))

    @app.get("/api/audit/intent/{iid}")
    async def api_audit_intent(iid: str):
        conn = get_connection()
        try:
            return JSONResponse(json_safe(AU.trail(conn, intent_id=iid)))
        except ExecutionError as e:
            return err_for(e)
        finally:
            conn.close()

    @app.get("/api/audit/order/{oid}")
    async def api_audit_order(oid: str):
        conn = get_connection()
        try:
            return JSONResponse(json_safe(AU.trail(conn, order_id=oid)))
        except ExecutionError as e:
            return err_for(e)
        finally:
            conn.close()

    @app.get("/trading", response_class=HTMLResponse)
    async def trading_page():
        from dashboard.execution_page import render
        from dashboard.security import token as _tok
        return HTMLResponse(render(_tok()))
