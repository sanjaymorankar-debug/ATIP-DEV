"""
Audit trail: reconstruct one trade from market data to position.

    trail(intent_id=...) or trail(order_id=...) returns, in order:
      decision_run   the run: date, book, resolved parameters, universe size
      decision       features snapshot (the input data), regime, reasons,
                     reason codes, signal source, confidence, score
      intent         the PositionIntent and its authorization status
      risk_decision  every risk check with value, limit and result
      order          the order, its state events and adapter calls
      fills          oms_fill rows
      position       the paper position now, and this strategy's attributed position
"""

from __future__ import annotations

import json

from execution import order_manager as OM
from execution import positions as P
from execution import risk_engine as RE
from execution.errors import InvalidIntentError


def _loads(d, *keys):
    for k in keys:
        if k in d:
            try:
                d[k.replace("_json", "")] = json.loads(d.pop(k) or "null")
            except Exception:
                pass
    return d


def trail(conn, intent_id: str | None = None, order_id: str | None = None) -> dict:
    if order_id and not intent_id:
        intent_id = OM.get_order(conn, order_id)["intent_id"]
    if not intent_id:
        raise InvalidIntentError("give intent_id or order_id")
    it = RE.load_intent(conn, intent_id)
    dec = conn.execute("SELECT * FROM strategy_decision WHERE decision_id=?", (it["decision_id"],)).fetchone()
    dec = _loads(dict(dec), "reasons_json", "parameters_json", "features_json", "reason_codes_json") if dec else None
    run = None
    if dec and dec.get("run_id"):
        r = conn.execute("SELECT * FROM strategy_decision_run WHERE run_id=?", (dec["run_id"],)).fetchone()
        run = _loads(dict(r), "params_json", "counts_json") if r else None
    rds = [RE.get_decision(conn, r[0]) for r in conn.execute(
        "SELECT risk_decision_id FROM risk_decision WHERE intent_id=? ORDER BY created_at", (intent_id,))]
    o = conn.execute("SELECT order_id FROM oms_order WHERE intent_id=?", (intent_id,)).fetchone()
    order = OM.order_detail(conn, o[0]) if o else None
    try:
        pos = conn.execute("SELECT * FROM paper_position WHERE symbol=?", (it["symbol"],)).fetchone()
    except Exception:                        # the paper broker creates its tables on first use
        pos = None
    strat = [p for p in P.strategy_positions(conn, it["strategy_id"]) if p["symbol"] == it["symbol"]]
    return {"decision_run": run, "decision": dec, "intent": it, "risk_decisions": rds, "order": order,
            "fills": order["fills"] if order else [], "paper_position": dict(pos) if pos else None,
            "strategy_position": strat[0] if strat else None}
