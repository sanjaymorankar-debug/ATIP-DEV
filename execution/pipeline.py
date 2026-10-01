"""
The execution cycle: pending intents -> risk decisions -> (optionally) paper orders.

    run_execution_cycle(execute=None)
      1. every intent still NOT_AUTHORIZED from the books in execution.execute_books,
         newest decision date first, SELLs before BUYs (exits free cash first)
      2. risk_engine.evaluate() each -> risk_decision + intent status
      3. when execute is true -- or execute is None and execution.auto_execute_paper
         is true -- every APPROVED decision becomes an order that is validated and
         submitted to the PAPER adapter. LIVE never executes in W4.

The post-market pipeline calls it after strategy_decisions with execute=None,
so with the shipped defaults it evaluates risk and places nothing.
"""

from __future__ import annotations

import logging

from execution import order_manager as OM
from execution import risk_engine as RE
from execution.config import PAPER, execution_settings
from execution.errors import ExecutionError
from execution.models import APPROVED

log = logging.getLogger("atip.execution")


def pending_intents(conn, as_of=None, strategy_id=None) -> list:
    q = "SELECT intent_id FROM strategy_position_intent WHERE authorization_status='NOT_AUTHORIZED'"
    args = []
    if as_of:
        q += " AND as_of=?"; args.append(str(as_of))
    if strategy_id:
        q += " AND strategy_id=?"; args.append(strategy_id)
    q += " ORDER BY as_of DESC, CASE side WHEN 'SELL' THEN 0 ELSE 1 END, confidence DESC, symbol"
    return [r[0] for r in conn.execute(q, args)]


def execute_approved(conn, risk_decision_id: str) -> dict:
    o = OM.create_order(conn, risk_decision_id)
    return OM.submit_order(conn, o["order_id"])


def run_execution_cycle(trade_date=None, execute: bool | None = None, as_of=None, conn=None) -> dict:
    from db.schema import get_connection
    own = conn is None
    conn = conn or get_connection()
    s = execution_settings()
    do_exec = s["auto_execute_paper"] if execute is None else bool(execute)
    counts, orders, errors = {}, [], []
    try:
        ids = pending_intents(conn, as_of)
        for iid in ids:
            try:
                rd = RE.evaluate(conn, iid)
            except ExecutionError as e:
                errors.append(f"{iid}: {e}"); log.warning(f"  risk evaluation {iid}: {e}")
                continue
            except Exception as e:
                errors.append(f"{iid}: {type(e).__name__}: {e}"); log.exception(f"  risk evaluation {iid} failed")
                continue
            counts[rd.risk_status] = counts.get(rd.risk_status, 0) + 1
            try:                                    # W7: tell the strategy's tenant (enterprise enabled only)
                from enterprise.config import enabled
                if enabled():
                    from enterprise.notifications import on_risk_decision
                    on_risk_decision(conn, rd)
            except Exception as e:
                log.warning(f"  risk notification for {rd.risk_decision_id}: {e}")
            if do_exec and rd.risk_status == APPROVED and s["mode"] == PAPER:
                try:
                    o = execute_approved(conn, rd.risk_decision_id)
                    orders.append({"order_id": o["order_id"], "symbol": o["symbol"], "side": o["side"],
                                   "status": o["status"], "filled": o["filled_quantity"]})
                    if s.get("protective_stops") and o["side"] == "BUY" and o["filled_quantity"]:
                        try:                        # W29 (EX-02)
                            st = OM.place_protective_stop(conn, o["order_id"],
                                                          limit_offset_pct=s.get("protective_stop_limit_offset_pct"))
                            orders.append({"order_id": st["order_id"], "symbol": st["symbol"], "side": "SELL",
                                           "status": st["status"], "protective_stop_for": o["order_id"]})
                        except Exception as e:
                            errors.append(f"protective stop for {o['order_id']}: {e}")
                except Exception as e:
                    errors.append(f"order for {rd.risk_decision_id}: {e}")
                    log.warning(f"  order for {rd.risk_decision_id}: {e}")
    finally:
        if own:
            conn.close()
    status = "SKIPPED" if not ids else ("FAILED" if errors and not counts else "SUCCESS")
    return {"status": status, "rows": len(ids), "risk": counts, "orders": orders, "executed": do_exec,
            "mode": s["mode"], "errors": errors[:50], "error": "; ".join(errors[:5]) or None,
            "reason": "no pending intents" if not ids else None}
