"""
W39 (OB-04) — your own pending orders at the broker, read-only.

Dhan: GET /v2/orders (dhanhq get_order_list) and GET /v2/forever/orders (forever / GTT orders).
Open statuses: TRANSIT, PENDING, PART_TRADED (and TRIGGER_PENDING / CONFIRM for forever orders).
Reading needs no static IP (only placing / modifying / cancelling does). Nothing here can change
an order. Also lists ATIP's own resting paper orders and its order rules (ACTIVE target / stop triggers
and PENDING_CONFIRMATION), so one screen answers "what is waiting to execute".
"""

from __future__ import annotations

OPEN = {"TRANSIT", "PENDING", "PART_TRADED", "TRIGGER_PENDING", "CONFIRM", "OPEN"}


def _rows(resp):
    if isinstance(resp, dict):
        data = resp.get("data")
        if resp.get("status") == "failure":
            raise RuntimeError(str(resp.get("remarks") or resp)[:200])
        return data if isinstance(data, list) else []
    return resp if isinstance(resp, list) else []


def _norm(o: dict, kind: str) -> dict:
    g = lambda *ks: next((o.get(k) for k in ks if o.get(k) not in (None, "")), None)
    return {"kind": kind, "order_id": g("orderId", "order_id"), "symbol": g("tradingSymbol", "trading_symbol"),
            "side": g("transactionType", "transaction_type"), "order_type": g("orderType", "order_type"),
            "product": g("productType", "product_type"), "quantity": g("quantity"),
            "filled": g("filledQty", "filled_qty", "tradedQuantity"), "price": g("price"),
            "trigger_price": g("triggerPrice", "trigger_price"), "status": g("orderStatus", "order_status"),
            "leg": g("legName", "leg_name"), "created": g("createTime", "create_time", "updateTime")}


def dhan_open_orders(dhan=None) -> dict:
    from data import dhan as D
    if not D.HAS_DHAN:
        return {"broker": "dhan", "status": "UNAVAILABLE", "reason": "dhanhq not installed", "orders": []}
    try:
        dhan = dhan or D.get_dhan_client()[0]
    except RuntimeError as e:
        return {"broker": "dhan", "status": "UNAVAILABLE", "reason": str(e), "orders": []}
    out, errors = [], []
    try:
        out += [_norm(o, "order") for o in _rows(dhan.get_order_list())
                if str(o.get("orderStatus") or "").upper() in OPEN]
    except Exception as e:
        errors.append(f"orders: {e}")
    fn = getattr(dhan, "get_forever", None) or getattr(dhan, "get_all_forever_orders", None)
    if fn:
        try:
            out += [_norm(o, "forever") for o in _rows(fn())
                    if str(o.get("orderStatus") or "PENDING").upper() in OPEN]
        except Exception as e:
            errors.append(f"forever orders: {e}")
    return {"broker": "dhan", "status": "OK" if not errors else ("PARTIAL" if out else "FAILED"),
            "orders": out, "errors": errors}


def atip_waiting(conn) -> dict:
    """ATIP's own: paper orders still resting and order rules waiting for confirmation."""
    def rows(sql):
        try:
            cur = conn.execute(sql)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        except Exception:
            return []
    return {"paper_orders": rows("SELECT * FROM paper_order WHERE status IN ('PENDING','PARTIALLY_FILLED') "
                                 "ORDER BY created_at DESC LIMIT 200"),
            # ACTIVE: ATIP-held target / stop / trailing triggers waiting for their price;
            # PENDING_CONFIRMATION: triggered, waiting for you to confirm (orders/rules.py)
            "order_rules": rows("SELECT * FROM order_rules WHERE status IN ('ACTIVE','PENDING_CONFIRMATION') "
                                "ORDER BY created_at DESC LIMIT 200")}


def waiting(conn, dhan=None) -> dict:
    return {"broker": dhan_open_orders(dhan), "atip": atip_waiting(conn)}
