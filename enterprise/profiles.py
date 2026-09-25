"""
Tenant- and user-level trading / risk profiles.

enterprise_risk_profile (scope TENANT or USER):
    trading_enabled        false blocks trading for that scope
    allowed_modes          ["PAPER"]  (LIVE is listed only if the owner adds it --
                           and W4's live gate is still closed; W4 stays authoritative)
    allowed_brokers        ["PAPER"]
    asset_classes          ["EQUITY"]
    allowed_strategies     null = all, else a list of strategy_ids
    max_capital            rupees the scope may deploy (informational for W4 today)
    max_order_value        rupees per order
    max_daily_orders
    notification defaults  (users' own preferences override)

effective(tenant, user) = the TIGHTER of the two (AND for booleans, intersection
for lists, min for numbers). Profiles can only restrict: they never loosen a W4
limit, never open the live gate.

Where they apply:
  * authz middleware: execution:trade requests need trading_enabled and the
    execution mode in allowed_modes for the caller's effective profile
  * W4 risk engine (execution/risk_engine.py, check "tenant_profile"): intents of
    a strategy owned by a tenant with trading disabled or the strategy not allowed
    are BLOCKED; max_order_value caps the order quantity
"""

from __future__ import annotations

import json
from datetime import datetime

from enterprise import audit

FIELDS = ("trading_enabled", "allowed_modes", "allowed_brokers", "asset_classes", "allowed_strategies",
          "max_capital", "max_order_value", "max_daily_orders")
DEFAULT = {"trading_enabled": True, "allowed_modes": ["PAPER"], "allowed_brokers": ["PAPER"],
           "asset_classes": ["EQUITY"], "allowed_strategies": None, "max_capital": None, "max_order_value": None,
           "max_daily_orders": None}


def get(conn, scope, scope_id) -> dict:
    r = conn.execute("SELECT profile_json, updated_at, updated_by FROM enterprise_risk_profile WHERE scope=? AND "
                     "scope_id=?", (scope, scope_id)).fetchone()
    out = dict(DEFAULT)
    if r:
        out.update(json.loads(r[0] or "{}"))
    out["_stored"] = bool(r)
    return out


def set_profile(conn, scope, scope_id, changes: dict, actor="system") -> dict:
    if scope not in ("TENANT", "USER"):
        raise ValueError("scope must be TENANT or USER")
    bad = set(changes) - set(FIELDS)
    if bad:
        raise ValueError(f"unknown profile fields {sorted(bad)}; known {FIELDS}")
    cur = get(conn, scope, scope_id)
    cur.pop("_stored")
    for k in ("allowed_modes", "allowed_brokers", "asset_classes"):
        if k in changes and not isinstance(changes[k], list):
            raise ValueError(f"{k} must be a list")
    for k in ("max_capital", "max_order_value", "max_daily_orders"):
        v = changes.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
            raise ValueError(f"{k} must be a non-negative number or null")
    cur.update(changes)
    conn.execute("INSERT INTO enterprise_risk_profile (scope,scope_id,profile_json,updated_at,updated_by) VALUES "
                 "(?,?,?,?,?) ON CONFLICT(scope,scope_id) DO UPDATE SET profile_json=excluded.profile_json,"
                 "updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                 (scope, scope_id, json.dumps(cur), datetime.now(), actor))
    audit.record(conn, "profile.update", tenant_id=scope_id if scope == "TENANT" else None,
                 user_id=scope_id if scope == "USER" else None, actor=actor, details=changes, commit=False)
    conn.commit()
    return get(conn, scope, scope_id)


def _tight(a, b, k):
    if k == "trading_enabled":
        return bool(a) and bool(b)
    if isinstance(a, list) or isinstance(b, list):
        if a is None:
            return b
        if b is None:
            return a
        return [x for x in a if x in b]
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def effective(conn, tenant_id, user_id=None) -> dict:
    t = get(conn, "TENANT", tenant_id)
    u = get(conn, "USER", user_id) if user_id else dict(DEFAULT)
    return {k: _tight(t.get(k), u.get(k), k) for k in FIELDS}


def tenant_constraints(conn, tenant_id) -> dict:
    """What the W4 risk engine applies for intents of a tenant's strategies."""
    return {k: v for k, v in get(conn, "TENANT", tenant_id).items() if k in FIELDS}
