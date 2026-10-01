"""
Broker registry (W37: BR-07): which brokers ATIP can read and how to get a connector for a user.

    connector(broker, fields)                   a read-only connector for explicit credential fields
    for_user(conn, tenant, user, broker, purpose)
                                                the user's connector, credentials from the vault (ENT-06)
    consolidated(conn, tenant, user)            holdings across every broker the user has a usable
                                                credential for, merged by symbol (broker breakdown kept)
    payload_preview(conn, order_id, broker)     what an OMS order would look like at that broker -- the
                                                order adapters refuse to send it (LIVE is not built)
"""

from __future__ import annotations

from brokers.base import as_dicts
from brokers.connectors import (AngelConnector, AngelOrderAdapter, DhanConnector, DhanOrderAdapter, UpstoxConnector,
                                UpstoxOrderAdapter, ZerodhaConnector, ZerodhaOrderAdapter)

CONNECTORS = {"dhan": DhanConnector, "zerodha": ZerodhaConnector, "upstox": UpstoxConnector, "angel": AngelConnector}
ORDER_ADAPTERS = {"dhan": DhanOrderAdapter, "zerodha": ZerodhaOrderAdapter, "upstox": UpstoxOrderAdapter,
                  "angel": AngelOrderAdapter}


def brokers() -> list:
    from enterprise.vault import BROKERS
    return [{"broker": b, "credential_fields": list(BROKERS.get(b, ())), "read_only_connector": True,
             "live_orders": "not built (payload preview only)"} for b in CONNECTORS]


def connector(broker: str, fields: dict):
    b = (broker or "").lower()
    if b not in CONNECTORS:
        raise ValueError(f"no connector for broker {broker}; available {sorted(CONNECTORS)}")
    return CONNECTORS[b](fields)


def for_user(conn, tenant, user, broker, purpose: str):
    from enterprise.vault import credentials_for
    cred = credentials_for(conn, tenant, user, broker, purpose)
    return connector(broker, cred["fields"]), cred["source"]


def snapshot(conn, tenant, user, broker) -> dict:
    c, source = for_user(conn, tenant, user, broker, "account snapshot (read-only)")
    return {"broker": broker, "credential_source": source, "holdings": as_dicts(c.holdings()),
            "positions": as_dicts(c.positions())}


def consolidated(conn, tenant, user) -> dict:
    merged, errors, used = {}, {}, []
    for b in CONNECTORS:
        try:
            c, _ = for_user(conn, tenant, user, b, "consolidated holdings (read-only)")
        except LookupError:
            continue
        try:
            hs = c.holdings()
        except Exception as e:
            errors[b] = f"{type(e).__name__}: {str(e)[:160]}"
            continue
        used.append(b)
        for h in hs:
            m = merged.setdefault(h.symbol, {"symbol": h.symbol, "quantity": 0.0, "cost": 0.0, "brokers": {}})
            m["quantity"] += h.quantity
            m["cost"] += h.quantity * (h.avg_price or 0)
            m["brokers"][b] = {"quantity": h.quantity, "avg_price": h.avg_price, "ltp": h.ltp}
    rows = []
    for m in merged.values():
        m["avg_price"] = round(m["cost"] / m["quantity"], 4) if m["quantity"] else None
        m.pop("cost")
        rows.append(m)
    return {"brokers_read": used, "errors": errors, "holdings": sorted(rows, key=lambda r: r["symbol"])}


def payload_preview(conn, order_id: str, broker: str) -> dict:
    b = (broker or "").lower()
    if b not in ORDER_ADAPTERS:
        raise ValueError(f"no order adapter for {broker}")
    r = conn.execute("SELECT * FROM oms_order WHERE order_id=?", (order_id,)).fetchone()
    if not r:
        raise LookupError(f"no order {order_id}")
    o = dict(r)
    return {"order_id": order_id, "broker": b, "payload": ORDER_ADAPTERS[b]().build_payload(o),
            "sendable": False, "why": "LIVE execution is not built in ATIP; this is the request ATIP would send"}
