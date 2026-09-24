"""
The order manager (W4): APPROVED risk decision -> order -> execution -> fills.

    create_order(risk_decision_id)   CREATED. Only from an APPROVED risk decision;
                                     one order per intent (DuplicateOrderError)
    validate_order(order_id)         VALIDATED, or REJECTED (kill switch on,
                                     live gate closed, bad quantity)
    submit_order(order_id)           SUBMITTED -> adapter -> ACKNOWLEDGED /
                                     PARTIALLY_FILLED / FILLED / REJECTED / FAILED
    cancel_order(order_id)           CANCELLED (not yet at the broker) or
                                     CANCEL_PENDING -> CANCELLED (resting paper LIMIT)
    refresh_order(order_id)          poll the adapter for a resting order

Every state change goes through transition(), which enforces
models.ORDER_TRANSITIONS and writes an oms_order_event row. Every adapter call
is an oms_execution row (request, response, status); every fill an oms_fill
row with the strategy it belongs to. A broker exception or error response is
recorded (FAILED / REJECTED) and logged with the order's context; it is never
retried silently.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime

from execution.adapters import get_adapter
from execution.config import LIVE, execution_settings, live_gate
from execution.errors import BrokerError, DuplicateOrderError, ExecutionError, InvalidIntentError, LiveTradingDisabled
from execution.models import (ACKNOWLEDGED, APPROVED, CANCEL_PENDING, CANCELLED, CREATED, FAILED, FILLED,
                              O_REJECTED, PARTIALLY_FILLED, SUBMITTED, TERMINAL, VALIDATED, check_transition)

log = logging.getLogger("atip.execution")


def _now():
    return datetime.now()


def get_order(conn, order_id: str) -> dict:
    r = conn.execute("SELECT * FROM oms_order WHERE order_id=?", (order_id,)).fetchone()
    if not r:
        raise InvalidIntentError(f"no order {order_id}")
    return dict(r)


def transition(conn, order_id: str, to: str, message: str = "", details: dict | None = None, actor="oms",
               **fields) -> dict:
    o = get_order(conn, order_id)
    check_transition(o["status"], to)
    sets = ["status=?", "updated_at=?"] + [f"{k}=?" for k in fields]
    conn.execute(f"UPDATE oms_order SET {', '.join(sets)} WHERE order_id=?",
                 [to, _now()] + list(fields.values()) + [order_id])
    conn.execute("INSERT INTO oms_order_event (order_id,from_status,to_status,message,details_json,actor,at) "
                 "VALUES (?,?,?,?,?,?,?)", (order_id, o["status"], to, (message or "")[:2000],
                                            json.dumps(details or {}, default=str), actor, _now()))
    conn.commit()
    (log.warning if to in (O_REJECTED, FAILED) else log.info)(
        f"  order {order_id} {o['side']} {o['quantity']} {o['symbol']}: {o['status']} -> {to} {message}")
    return get_order(conn, order_id)


def create_order(conn, risk_decision_id: str, actor: str = "oms") -> dict:
    rd = conn.execute("SELECT * FROM risk_decision WHERE risk_decision_id=?", (risk_decision_id,)).fetchone()
    if not rd:
        raise InvalidIntentError(f"no risk decision {risk_decision_id}")
    rd = dict(rd)
    if rd["risk_status"] != APPROVED:
        raise InvalidIntentError(f"risk decision {risk_decision_id} is {rd['risk_status']}; "
                                 f"only APPROVED decisions become orders")
    ex = conn.execute("SELECT order_id FROM oms_order WHERE intent_id=? OR risk_decision_id=?",
                      (rd["intent_id"], risk_decision_id)).fetchone()
    if ex:
        raise DuplicateOrderError(f"intent {rd['intent_id']} already has order {ex[0]}")
    s = execution_settings()
    oid = "OMS" + uuid.uuid4().hex[:14].upper()
    now = _now()
    conn.execute("INSERT INTO oms_order (order_id,intent_id,risk_decision_id,decision_id,strategy_id,strategy_version,"
                 "symbol,side,quantity,order_type,limit_price,product_type,mode,adapter,status,reference_price,"
                 "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (oid, rd["intent_id"], risk_decision_id, rd["decision_id"], rd["strategy_id"], rd["strategy_version"],
                  rd["symbol"], rd["side"], int(rd["approved_quantity"]), s["order_type"],
                  rd["reference_price"] if s["order_type"] == "LIMIT" else None, s["product_type"], s["mode"],
                  "paper" if s["mode"] != LIVE else "dhan", CREATED, rd["reference_price"], now, now))
    conn.execute("INSERT INTO oms_order_event (order_id,from_status,to_status,message,details_json,actor,at) "
                 "VALUES (?,?,?,?,?,?,?)", (oid, None, CREATED, f"from risk decision {risk_decision_id}",
                                            json.dumps({"risk_decision_id": risk_decision_id}), actor, now))
    conn.commit()
    return get_order(conn, oid)


def validate_order(conn, order_id: str) -> dict:
    o = get_order(conn, order_id)
    if o["status"] != CREATED:
        raise ExecutionError(f"order {order_id} is {o['status']}; only CREATED orders are validated")
    problems = []
    try:
        from orders.risk import halted
        h, why = halted()
        if h:
            problems.append(f"trading halted: {why}")
    except Exception as e:
        problems.append(f"kill switch unreadable: {e}")
    if o["mode"] == LIVE:
        ok, why = live_gate()
        if not ok:
            problems.append(f"live trading disabled ({why})")
    if int(o["quantity"]) <= 0:
        problems.append("quantity must be > 0")
    if o["side"] not in ("BUY", "SELL"):
        problems.append(f"bad side {o['side']}")
    if problems:
        return transition(conn, order_id, O_REJECTED, "; ".join(problems), reason="; ".join(problems))
    return transition(conn, order_id, VALIDATED, "pre-submit checks passed")


def _record_execution(conn, o, action, request, result=None, error=None):
    eid = "EX" + uuid.uuid4().hex[:16].upper()
    conn.execute("INSERT INTO oms_execution (execution_id,order_id,adapter,action,request_json,response_json,status,"
                 "broker_order_id,error,at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (eid, o["order_id"], o["adapter"], action, json.dumps(request, default=str),
                  json.dumps(result.raw if result else None, default=str), result.status if result else "ERROR",
                  result.broker_order_id if result else None, error, _now()))
    conn.commit()
    return eid


def _record_fill(conn, o, eid, qty, price, fees, source):
    if not qty or price is None:
        return
    conn.execute("INSERT INTO oms_fill (fill_id,order_id,execution_id,strategy_id,strategy_version,symbol,side,"
                 "quantity,price,fees,price_source,mode,filled_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("FL" + uuid.uuid4().hex[:16].upper(), o["order_id"], eid, o["strategy_id"], o["strategy_version"],
                  o["symbol"], o["side"], int(qty), float(price), float(fees or 0), source, o["mode"], _now()))
    conn.commit()


def _apply(conn, o, eid, r):
    """Move the order to the state the broker reported and record any fill."""
    new_fill = max(0, int(r.filled_quantity or 0) - int(o["filled_quantity"] or 0))
    if new_fill:
        _record_fill(conn, o, eid, new_fill, r.avg_price, r.fees, r.price_source)
    fields = {"broker_order_id": r.broker_order_id or o["broker_order_id"]}
    if r.filled_quantity:
        fields.update(filled_quantity=int(r.filled_quantity), avg_fill_price=r.avg_price, fees=r.fees or 0)
    target = {"FILLED": FILLED, "PARTIALLY_FILLED": PARTIALLY_FILLED, "ACCEPTED": ACKNOWLEDGED,
              "REJECTED": O_REJECTED, "CANCELLED": CANCELLED}.get(r.status, FAILED)
    cur = get_order(conn, o["order_id"])["status"]
    if cur == SUBMITTED and target in (FILLED, PARTIALLY_FILLED):
        transition(conn, o["order_id"], ACKNOWLEDGED, "broker accepted", **fields)
        cur = ACKNOWLEDGED
    if target == cur and target != PARTIALLY_FILLED:
        return get_order(conn, o["order_id"])
    if target in (O_REJECTED, FAILED):
        fields["reason"] = r.message
    return transition(conn, o["order_id"], target, r.message or r.status, {"execution_id": eid}, **fields)


def submit_order(conn, order_id: str) -> dict:
    o = get_order(conn, order_id)
    if o["status"] == CREATED:
        o = validate_order(conn, order_id)
        if o["status"] != VALIDATED:
            return o
    if o["status"] != VALIDATED:
        raise ExecutionError(f"order {order_id} is {o['status']}; only CREATED / VALIDATED orders are submitted")
    o = transition(conn, order_id, SUBMITTED, f"to {o['adapter']} adapter ({o['mode']})")
    request = {k: o[k] for k in ("order_id", "symbol", "side", "quantity", "order_type", "limit_price",
                                 "product_type", "reference_price")}
    try:
        adapter = get_adapter(conn, o["mode"])
        r = adapter.submit(o)
    except (LiveTradingDisabled, BrokerError) as e:
        eid = _record_execution(conn, o, "submit", request, error=str(e))
        log.error(f"  order {order_id}: submit failed: {e}")
        return transition(conn, order_id, FAILED, str(e), {"execution_id": eid}, reason=str(e))
    except Exception as e:
        eid = _record_execution(conn, o, "submit", request, error=f"{type(e).__name__}: {e}")
        log.exception(f"  order {order_id}: unexpected execution failure")
        return transition(conn, order_id, FAILED, f"{type(e).__name__}: {e}", {"execution_id": eid}, reason=str(e))
    eid = _record_execution(conn, o, "submit", request, r)
    return _apply(conn, get_order(conn, order_id), eid, r)


def cancel_order(conn, order_id: str, reason: str = "cancelled by owner") -> dict:
    o = get_order(conn, order_id)
    if o["status"] in TERMINAL:
        raise ExecutionError(f"order {order_id} is {o['status']} (terminal)")
    if o["status"] in (CREATED, VALIDATED):
        return transition(conn, order_id, CANCELLED, reason, reason=reason)
    if o["status"] == SUBMITTED:
        raise ExecutionError(f"order {order_id} is SUBMITTED with no broker acknowledgement yet; refresh it first")
    o = transition(conn, order_id, CANCEL_PENDING, reason)
    try:
        r = get_adapter(conn, o["mode"]).cancel(o)
    except Exception as e:
        eid = _record_execution(conn, o, "cancel", {"order_id": order_id}, error=str(e))
        return transition(conn, order_id, FAILED, f"cancel failed: {e}", {"execution_id": eid}, reason=str(e))
    eid = _record_execution(conn, o, "cancel", {"order_id": order_id}, r)
    if r.status == "CANCELLED":
        return transition(conn, order_id, CANCELLED, reason, {"execution_id": eid}, reason=reason)
    return refresh_order(conn, order_id)


def refresh_order(conn, order_id: str) -> dict:
    o = get_order(conn, order_id)
    if o["status"] in TERMINAL or not o["broker_order_id"]:
        return o
    try:
        r = get_adapter(conn, o["mode"]).status(o)
    except Exception as e:
        eid = _record_execution(conn, o, "status", {"order_id": order_id}, error=str(e))
        log.warning(f"  order {order_id}: status poll failed: {e}")
        return get_order(conn, order_id)
    eid = _record_execution(conn, o, "status", {"order_id": order_id}, r)
    return _apply(conn, o, eid, r)


def order_detail(conn, order_id: str) -> dict:
    o = get_order(conn, order_id)
    o["events"] = [dict(r) for r in conn.execute("SELECT * FROM oms_order_event WHERE order_id=? ORDER BY id",
                                                 (order_id,))]
    o["executions"] = [dict(r) for r in conn.execute("SELECT * FROM oms_execution WHERE order_id=? ORDER BY at",
                                                     (order_id,))]
    o["fills"] = [dict(r) for r in conn.execute("SELECT * FROM oms_fill WHERE order_id=? ORDER BY filled_at",
                                                (order_id,))]
    return o
