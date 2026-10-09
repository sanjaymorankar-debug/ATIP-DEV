"""
The order manager (W4): APPROVED risk decision -> order -> execution -> fills.

    create_order(risk_decision_id)   CREATED. Only from an APPROVED risk decision;
                                     one order per intent (DuplicateOrderError). W40: an OPTION_OPEN /
                                     OPTION_CLOSE decision becomes ONE order, instrument OPT, adapter
                                     paper_opt, quantity = structure lots, its legs in legs_json; filled
                                     all-or-nothing by the paper options book (LIVE refused). Its legs
                                     are the fills (paper_option_strategy_trade), so no oms_fill row
    validate_order(order_id)         VALIDATED, or REJECTED (kill switch on,
                                     live gate closed, bad quantity)
    submit_order(order_id)           SUBMITTED -> adapter -> ACKNOWLEDGED /
                                     PARTIALLY_FILLED / FILLED / REJECTED / FAILED
    cancel_order(order_id)           CANCELLED (not yet at the broker) or
                                     CANCEL_PENDING -> CANCELLED (resting paper LIMIT)
    refresh_order(order_id)          poll the adapter for a resting order
    modify_order(order_id, ...)      (W29, EX-08) quantity / limit / trigger / type of a
                                     resting (ACKNOWLEDGED) order; an oms_order_event row
                                     records old -> new; CREATED / VALIDATED orders are
                                     edited in place before they reach the broker
    place_protective_stop(order_id)  (W29, EX-02) after an entry fills: a child SL-M (or SL)
                                     SELL for the filled quantity at the intent's stop_price,
                                     linked by parent_order_id. Risk-reducing, so it needs no
                                     risk decision; PAPER only (LIVE is refused upstream)

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
                              MODIFIABLE, O_REJECTED, PARTIALLY_FILLED, SUBMITTED, TERMINAL, VALIDATED,
                              check_order_type, check_transition)

from ops.resilience import non_idempotent

log = logging.getLogger("atip.execution")


def _in_market_session(now=None) -> bool:
    """NSE cash session: a trading day, 09:15-15:30 IST (the machine runs in IST)."""
    from utils.trading_calendar import is_trading_day
    now = now or datetime.now()
    return is_trading_day(now.date()) and (9 * 60 + 15) <= now.hour * 60 + now.minute <= 15 * 60 + 30


def _now():
    return datetime.now()


def _adapter(conn, o):
    """The adapter for an order: tenant (W9 per-tenant paper books) and instrument (W30 FUT)."""
    from execution.tenant_books import tenant_of_strategy
    return get_adapter(conn, o["mode"], instrument=o.get("instrument"),
                       tenant_id=tenant_of_strategy(conn, o["strategy_id"]))


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
    from execution.events import publish                     # W34 (EX-16): same transaction as the change
    publish(conn, "order.state", order_id, {"order_id": order_id, "from": o["status"], "to": to,
                                            "symbol": o["symbol"], "side": o["side"], "quantity": o["quantity"],
                                            "strategy_id": o["strategy_id"], "mode": o["mode"],
                                            "algo_parent_id": o.get("algo_parent_id"), "message": (message or "")[:300]})
    conn.commit()
    (log.warning if to in (O_REJECTED, FAILED) else log.info)(
        f"  order {order_id} {o['side']} {o['quantity']} {o['symbol']}: {o['status']} -> {to} {message}")
    return get_order(conn, order_id)


def create_order(conn, risk_decision_id: str, actor: str = "oms", order_type: str | None = None,
                 limit_price: float | None = None, trigger_price: float | None = None) -> dict:
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
    instrument = "FUT" if rd.get("action") in ("SHORT", "COVER") else "CASH"     # W30: futures short legs
    legs_json = None
    if rd.get("action") in ("OPTION_OPEN", "OPTION_CLOSE"):                    # W40: multi-leg option orders
        if s["mode"] == LIVE or rd.get("book") == LIVE:
            raise InvalidIntentError("LIVE options are not built")
        from execution.option_intents import order_plan
        instrument, legs_json = "OPT", json.dumps(order_plan(conn, rd), default=str)
    if instrument in ("FUT", "OPT"):
        if s["mode"] == LIVE:
            raise InvalidIntentError("LIVE futures are not built")
        # FUT: filled at the EOD futures close; OPT: every leg at the chain mid -/+ slippage, all-or-nothing
        order_type, limit_price, trigger_price = "MARKET", None, None
    otype = (order_type or s["order_type"]).upper()
    if otype == "LIMIT" and limit_price is None:
        limit_price = rd["reference_price"]
    otype = check_order_type(otype, rd["side"], limit_price, trigger_price)
    oid = "OMS" + uuid.uuid4().hex[:14].upper()
    now = _now()
    adapter = {"FUT": "paper_fut", "OPT": "paper_opt"}.get(instrument, "paper") if s["mode"] != LIVE else "dhan"
    conn.execute("INSERT INTO oms_order (order_id,intent_id,risk_decision_id,decision_id,strategy_id,strategy_version,"
                 "symbol,side,quantity,order_type,limit_price,trigger_price,product_type,mode,adapter,status,"
                 "reference_price,instrument,created_at,updated_at" + (",legs_json" if legs_json else "") + ") VALUES "
                 "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?" + (",?" if legs_json else "") + ")",
                 (oid, rd["intent_id"], risk_decision_id, rd["decision_id"], rd["strategy_id"], rd["strategy_version"],
                  rd["symbol"], rd["side"], int(rd["approved_quantity"]), otype,
                  limit_price if otype in ("LIMIT", "SL") else None,
                  trigger_price if otype in ("SL", "SL-M") else None,
                  "NRML" if instrument in ("FUT", "OPT") else s["product_type"], s["mode"],
                  adapter, CREATED, rd["reference_price"], instrument, now, now) + ((legs_json,) if legs_json else ()))
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
        elif not _in_market_session():
            problems.append("outside the NSE market session (09:15-15:30 IST on a trading day)")
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
    if o.get("instrument") == "OPT":
        # W40: a multi-leg option order's fills are its legs, recorded by the paper options book
        # (paper_option_strategy_trade). One oms_fill row (one symbol, one price) would be read as a
        # cash position in the underlying by positions.py, reconcile.py and the wealth ledger.
        return
    conn.execute("INSERT INTO oms_fill (fill_id,order_id,execution_id,strategy_id,strategy_version,symbol,side,"
                 "quantity,price,fees,price_source,mode,filled_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("FL" + uuid.uuid4().hex[:16].upper(), o["order_id"], eid, o["strategy_id"], o["strategy_version"],
                  o["symbol"], o["side"], int(qty), float(price), float(fees or 0), source, o["mode"], _now()))
    from execution.events import publish                     # W34 (EX-16)
    publish(conn, "order.fill", o["order_id"], {"order_id": o["order_id"], "fill_qty": int(qty), "price": float(price),
                                                "fees": float(fees or 0), "symbol": o["symbol"], "side": o["side"],
                                                "strategy_id": o["strategy_id"], "mode": o["mode"],
                                                "algo_parent_id": o.get("algo_parent_id")})
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


@non_idempotent
def submit_order(conn, order_id: str) -> dict:
    o = get_order(conn, order_id)
    if o["status"] == CREATED:
        o = validate_order(conn, order_id)
        if o["status"] != VALIDATED:
            return o
    if o["status"] != VALIDATED:
        raise ExecutionError(f"order {order_id} is {o['status']}; only CREATED / VALIDATED orders are submitted")
    o = transition(conn, order_id, SUBMITTED, f"to {o['adapter']} adapter ({o['mode']})")
    request = {k: o.get(k) for k in ("order_id", "symbol", "side", "quantity", "order_type", "limit_price",
                                     "trigger_price", "product_type", "reference_price")}
    try:
        from ops.latency import record, since_ms, timed       # W34 (EX-15)
        it = conn.execute("SELECT created_at FROM strategy_position_intent WHERE intent_id=?",
                          (o["intent_id"],)).fetchone()
        record("order.decision_to_submit", since_ms(it[0]) if it else None)
        adapter = _adapter(conn, o)
        with timed("order.submit_adapter"):
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


@non_idempotent
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
        r = _adapter(conn, o).cancel(o)
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
        r = _adapter(conn, o).status(o)
    except Exception as e:
        eid = _record_execution(conn, o, "status", {"order_id": order_id}, error=str(e))
        log.warning(f"  order {order_id}: status poll failed: {e}")
        return get_order(conn, order_id)
    eid = _record_execution(conn, o, "status", {"order_id": order_id}, r)
    return _apply(conn, o, eid, r)


def _event(conn, o, message, details, actor):
    """An audit row with no state change (oms_order_event is append-only)."""
    conn.execute("INSERT INTO oms_order_event (order_id,from_status,to_status,message,details_json,actor,at) "
                 "VALUES (?,?,?,?,?,?,?)", (o["order_id"], o["status"], o["status"], message[:2000],
                                            json.dumps(details, default=str), actor, _now()))


@non_idempotent
def modify_order(conn, order_id: str, quantity: int | None = None, limit_price: float | None = None,
                 trigger_price: float | None = None, order_type: str | None = None, actor: str = "owner") -> dict:
    """EX-08. Quantity may only go DOWN from the approved quantity -- raising it would
    bypass the risk decision that sized the order."""
    o = get_order(conn, order_id)
    if o["status"] in TERMINAL:
        raise ExecutionError(f"order {order_id} is {o['status']} (terminal)")
    if o["status"] not in MODIFIABLE | {CREATED, VALIDATED}:
        raise ExecutionError(f"order {order_id} is {o['status']}; only CREATED / VALIDATED / ACKNOWLEDGED "
                             f"orders can be modified")
    new = {"quantity": int(quantity) if quantity is not None else int(o["quantity"]),
           "limit_price": float(limit_price) if limit_price is not None else o["limit_price"],
           "trigger_price": float(trigger_price) if trigger_price is not None else o.get("trigger_price"),
           "order_type": (order_type or o["order_type"]).upper()}
    if new["quantity"] <= 0:
        raise ExecutionError("quantity must be > 0")
    if new["quantity"] > int(o["quantity"]) and o.get("risk_decision_id"):
        raise ExecutionError(f"quantity can only be reduced (approved {o['quantity']}); a larger order needs a "
                             f"new risk decision")
    try:
        new["order_type"] = check_order_type(new["order_type"], o["side"], new["limit_price"], new["trigger_price"])
    except ValueError as e:
        raise ExecutionError(str(e)) from e
    old = {k: o.get(k) for k in new}
    if new == old:
        raise ExecutionError("nothing to change")
    if o["status"] in (CREATED, VALIDATED):
        conn.execute("UPDATE oms_order SET quantity=?, limit_price=?, trigger_price=?, order_type=?, "
                     "modified_count=COALESCE(modified_count,0)+1, updated_at=? WHERE order_id=?",
                     (new["quantity"], new["limit_price"], new["trigger_price"], new["order_type"], _now(), order_id))
        _event(conn, o, "modified before submission", {"from": old, "to": new}, actor)
        conn.commit()
        return get_order(conn, order_id)
    try:
        if o.get("instrument") in ("FUT", "OPT"):
            raise ExecutionError("futures / option paper orders fill at once; nothing to modify")
        r = _adapter(conn, o).modify(o, new)
    except Exception as e:
        eid = _record_execution(conn, o, "modify", {"order_id": order_id, **new}, error=str(e))
        _event(conn, o, f"modify failed: {e}", {"execution_id": eid, "to": new}, actor)
        conn.commit()
        raise ExecutionError(f"modify failed: {e}") from e
    eid = _record_execution(conn, o, "modify", {"order_id": order_id, **new}, r)
    if r.status == "REJECTED":
        _event(conn, o, f"modify rejected: {r.message}", {"execution_id": eid, "to": new}, actor)
        conn.commit()
        raise ExecutionError(f"modify rejected by the broker: {r.message}")
    conn.execute("UPDATE oms_order SET quantity=?, limit_price=?, trigger_price=?, order_type=?, "
                 "modified_count=COALESCE(modified_count,0)+1, updated_at=? WHERE order_id=?",
                 (new["quantity"], new["limit_price"], new["trigger_price"], new["order_type"], _now(), order_id))
    _event(conn, o, "modified at the broker", {"execution_id": eid, "from": old, "to": new}, actor)
    conn.commit()
    return _apply(conn, get_order(conn, order_id), eid, r)


def place_protective_stop(conn, order_id: str, stop_price: float | None = None, limit_offset_pct: float | None = None,
                          actor: str = "oms") -> dict:
    """EX-02: a protective SELL stop for a filled BUY entry, as a child order. SL-M by
    default; with limit_offset_pct an SL whose limit sits that far below the trigger."""
    o = get_order(conn, order_id)
    if (o.get("instrument") or "CASH") != "CASH":
        # a futures COVER or an option order is not a cash entry: an SL SELL of the underlying would be wrong
        raise ExecutionError(f"protective stops are for cash entries, not {o.get('instrument')} orders")
    if o["side"] != "BUY" or o["status"] not in (FILLED, PARTIALLY_FILLED):
        raise ExecutionError("a protective stop needs a filled (or partly filled) BUY entry")
    if o["mode"] == LIVE:
        raise LiveTradingDisabled("protective stops are paper-only (live execution is not built)")
    if conn.execute("SELECT 1 FROM oms_order WHERE parent_order_id=? AND status NOT IN ('CANCELLED','REJECTED',"
                    "'FAILED')", (order_id,)).fetchone():
        raise DuplicateOrderError(f"order {order_id} already has a live protective stop")
    if stop_price is None:
        r = conn.execute("SELECT stop_price FROM strategy_position_intent WHERE intent_id=?",
                         (o["intent_id"],)).fetchone()
        stop_price = r[0] if r else None
    if not stop_price or float(stop_price) <= 0:
        raise ExecutionError("no stop price (the intent carries none; pass stop_price)")
    trig = float(stop_price)
    otype, lim = "SL-M", None
    if limit_offset_pct:
        otype, lim = "SL", round(trig * (1 - float(limit_offset_pct) / 100), 2)
    qty = int(o["filled_quantity"] or 0)
    oid = "OMS" + uuid.uuid4().hex[:14].upper()
    now = _now()
    conn.execute("INSERT INTO oms_order (order_id,intent_id,risk_decision_id,decision_id,strategy_id,strategy_version,"
                 "symbol,side,quantity,order_type,limit_price,trigger_price,product_type,mode,adapter,status,"
                 "reference_price,parent_order_id,tenant_id,created_at,updated_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 # oms_order.intent_id / risk_decision_id are NOT NULL UNIQUE: a child stop has
                 # neither, so it carries a synthetic id that names its parent
                 (oid, f"PSTOP-{order_id}", f"PSTOP-{order_id}", o["decision_id"], o["strategy_id"],
                  o["strategy_version"], o["symbol"], "SELL",
                  # reference = the entry's fill price (the market when the stop is placed): the
                  # paper adapter's "reference" pricing must not see the trigger as the market,
                  # or the stop would trigger on placement. Stop slippage is measured vs trigger.
                  qty, otype, lim, trig, o["product_type"], o["mode"], o["adapter"], CREATED,
                  o["avg_fill_price"] or o["reference_price"], order_id,
                  o.get("tenant_id") or "default", now, now))
    conn.execute("INSERT INTO oms_order_event (order_id,from_status,to_status,message,details_json,actor,at) "
                 "VALUES (?,?,?,?,?,?,?)", (oid, None, CREATED, f"protective stop for {order_id}",
                                            json.dumps({"parent_order_id": order_id, "trigger": trig}), actor, now))
    conn.commit()
    return submit_order(conn, oid)


def order_detail(conn, order_id: str) -> dict:
    o = get_order(conn, order_id)
    o["events"] = [dict(r) for r in conn.execute("SELECT * FROM oms_order_event WHERE order_id=? ORDER BY id",
                                                 (order_id,))]
    o["executions"] = [dict(r) for r in conn.execute("SELECT * FROM oms_execution WHERE order_id=? ORDER BY at",
                                                     (order_id,))]
    o["fills"] = [dict(r) for r in conn.execute("SELECT * FROM oms_fill WHERE order_id=? ORDER BY filled_at",
                                                (order_id,))]
    return o
