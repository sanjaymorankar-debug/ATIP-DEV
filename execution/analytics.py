"""
Measured slippage (EX-09) and execution analytics (EX-10), W29 -- from the W4 audit tables
(oms_order, oms_order_event, oms_fill); nothing is estimated.

SLIPPAGE (per fill)
    benchmark   the order's reference_price -- the price the decision acted on (the decision
                close); for a stop order (SL / SL-M) its trigger_price
    slippage_bps = side * (fill - reference) / reference * 10,000, side +1 BUY / -1 SELL,
                so POSITIVE = adverse (paid more on a buy, received less on a sell)
    cost_rs     = side * (fill - reference) * quantity
    The paper broker always slips adversely by paper_slippage_bps, and with
    paper_fill_price "live" the gap between the decision close and the next live price is
    the real-world timing cost -- which is what this measures.

ANALYTICS (per period, overall and by strategy / by order type / by day)
    orders, fill rate (filled qty / ordered qty, and filled orders / orders that reached the
    broker), rejection rate with the reasons ranked, cancel rate, failed count,
    time to fill (SUBMITTED -> FILLED, from oms_order_event), slippage mean / median / p90 /
    worst bps and total cost, fees.

    slippage(conn, start, end, strategy_id=None)   fill rows with slippage
    analytics(conn, start=None, end=None, days=30) the summary
"""

from __future__ import annotations

from datetime import date, datetime, timedelta


def _pct(vals, q):
    if not vals:
        return None
    v = sorted(vals)
    k = (len(v) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 2)


def _range(start, end, days):
    end = str(end or date.today())[:10]
    start = str(start or (date.fromisoformat(end) - timedelta(days=int(days))))[:10]
    return start, end


def slippage(conn, start=None, end=None, strategy_id=None, days=30) -> list:
    start, end = _range(start, end, days)
    sql = ("SELECT f.fill_id, f.order_id, f.strategy_id, f.symbol, f.side, f.quantity, f.price, f.fees, f.mode, "
           "f.filled_at, o.reference_price, o.order_type, o.limit_price, o.trigger_price FROM oms_fill f JOIN oms_order o "
           "ON o.order_id=f.order_id WHERE DATE(f.filled_at) BETWEEN ? AND ?")
    args = [start, end]
    if strategy_id:
        sql += " AND f.strategy_id=?"
        args.append(strategy_id)
    out = []
    for r in conn.execute(sql + " ORDER BY f.filled_at", args):
        d = dict(zip(("fill_id", "order_id", "strategy_id", "symbol", "side", "quantity", "price", "fees", "mode",
                      "filled_at", "reference_price", "order_type", "limit_price", "trigger_price"), r))
        # a stop's benchmark is its trigger (the price the stop was meant to exit at)
        ref = d["trigger_price"] if d["order_type"] in ("SL", "SL-M") and d["trigger_price"] else d["reference_price"]
        d["benchmark"] = "trigger" if ref == d["trigger_price"] and d["order_type"] in ("SL", "SL-M") else "reference"
        sign = 1 if d["side"] == "BUY" else -1
        if ref:
            d["slippage_bps"] = round(sign * (d["price"] - ref) / ref * 1e4, 2)
            d["slippage_cost_rs"] = round(sign * (d["price"] - ref) * d["quantity"], 2)
        else:
            d["slippage_bps"] = d["slippage_cost_rs"] = None
        out.append(d)
    return out


def _time_to_fill(conn, order_ids) -> list:
    out = []
    for oid in order_ids:
        ev = conn.execute("SELECT to_status, at FROM oms_order_event WHERE order_id=? ORDER BY id", (oid,)).fetchall()
        sub = next((e[1] for e in ev if e[0] == "SUBMITTED"), None)
        fil = next((e[1] for e in ev if e[0] == "FILLED"), None)
        if sub and fil:
            a = sub if isinstance(sub, datetime) else datetime.fromisoformat(str(sub))
            b = fil if isinstance(fil, datetime) else datetime.fromisoformat(str(fil))
            out.append((b - a).total_seconds())
    return out


def _summary(conn, orders, fills) -> dict:
    n = len(orders)
    reached = [o for o in orders if o["status"] not in ("CREATED", "VALIDATED") and
               not (o["status"] in ("REJECTED", "FAILED") and not o["broker_order_id"])]
    filled = [o for o in orders if o["status"] in ("FILLED", "PARTIALLY_FILLED")]
    rej = [o for o in orders if o["status"] == "REJECTED"]
    reasons = {}
    for o in rej + [o for o in orders if o["status"] == "FAILED"]:
        k = (o["reason"] or "unknown")[:80]
        reasons[k] = reasons.get(k, 0) + 1
    oq = sum(int(o["quantity"] or 0) for o in orders)
    fq = sum(int(o["filled_quantity"] or 0) for o in orders)
    sl = [f["slippage_bps"] for f in fills if f["slippage_bps"] is not None]
    ttf = _time_to_fill(conn, [o["order_id"] for o in filled])
    return {
        "orders": n, "reached_broker": len(reached), "filled_orders": len(filled),
        "fill_rate_qty": round(fq / oq, 4) if oq else None,
        "fill_rate_orders": round(len(filled) / len(reached), 4) if reached else None,
        "rejected": len(rej), "rejection_rate": round(len(rej) / n, 4) if n else None,
        "cancelled": sum(1 for o in orders if o["status"] == "CANCELLED"),
        "failed": sum(1 for o in orders if o["status"] == "FAILED"),
        "rejection_reasons": sorted(({"reason": k, "count": v} for k, v in reasons.items()),
                                    key=lambda x: -x["count"])[:10],
        "time_to_fill_s": {"median": _pct(ttf, 0.5), "p90": _pct(ttf, 0.9), "n": len(ttf)},
        "slippage_bps": {"mean": round(sum(sl) / len(sl), 2) if sl else None, "median": _pct(sl, 0.5),
                         "p90": _pct(sl, 0.9), "worst": max(sl) if sl else None, "n": len(sl)},
        "slippage_cost_rs": round(sum(f["slippage_cost_rs"] or 0 for f in fills), 2),
        "fees_rs": round(sum(f["fees"] or 0 for f in fills), 2),
    }


def analytics(conn, start=None, end=None, days=30) -> dict:
    start, end = _range(start, end, days)
    orders = [dict(r) for r in conn.execute("SELECT * FROM oms_order WHERE DATE(created_at) BETWEEN ? AND ?",
                                            (start, end))]
    fills = slippage(conn, start, end)

    def group(key):
        out = {}
        for k in sorted({str(o.get(key)) for o in orders}):
            os_ = [o for o in orders if str(o.get(key)) == k]
            ids = {o["order_id"] for o in os_}
            out[k] = _summary(conn, os_, [f for f in fills if f["order_id"] in ids])
        return out
    by_day = {}
    for o in orders:
        d = str(o["created_at"])[:10]
        by_day.setdefault(d, []).append(o)
    return {"period": f"{start}..{end}", "overall": _summary(conn, orders, fills),
            "by_strategy": group("strategy_id"), "by_order_type": group("order_type"), "by_mode": group("mode"),
            "by_day": {d: {"orders": len(v), "filled": sum(1 for o in v if o["status"] == "FILLED"),
                           "rejected": sum(1 for o in v if o["status"] == "REJECTED")} for d, v in sorted(by_day.items())},
            "note": None if orders else "no W4 orders in the period (strategies are DRAFT; auto_execute_paper off)"}
