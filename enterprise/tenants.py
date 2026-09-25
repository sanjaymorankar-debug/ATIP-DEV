"""
Tenants: the isolation boundary.

enterprise_tenant: tenant_id, name, status, settings_json, limits_json, created /
updated. Lifecycle:

    ACTIVE <-> SUSPENDED      suspended tenants are read-only (GET only)
    ACTIVE / SUSPENDED -> DISABLED -> ACTIVE     disabled: no access at all
    any -> ARCHIVED           terminal; data kept, no access
The owner's tenant (enterprise.default_tenant, "default") cannot be
suspended, disabled or archived -- the platform must keep an administrator.

Limits (limits_json, merged with the subscription plan's -- the tighter wins):
    max_users, max_strategies, max_models, max_backtests_per_day, max_api_keys

Tenant-owned rows carry tenant_id (strategies, models, pairs, experiments,
portfolios, intents, risk decisions, orders, and all enterprise tables);
existing rows belong to "default".
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from enterprise import audit
from enterprise.config import settings

STATUSES = ("ACTIVE", "SUSPENDED", "DISABLED", "ARCHIVED")
TRANSITIONS = {"ACTIVE": {"SUSPENDED", "DISABLED", "ARCHIVED"}, "SUSPENDED": {"ACTIVE", "DISABLED", "ARCHIVED"},
               "DISABLED": {"ACTIVE", "ARCHIVED"}, "ARCHIVED": set()}
LIMIT_KEYS = ("max_users", "max_strategies", "max_models", "max_backtests_per_day", "max_api_keys")
DEFAULT_LIMITS = {"max_users": 25, "max_strategies": 50, "max_models": 20, "max_backtests_per_day": 50,
                  "max_api_keys": 5}


def _limits(v):
    if set(v) - set(LIMIT_KEYS):
        raise ValueError(f"unknown limits {sorted(set(v) - set(LIMIT_KEYS))}; known {LIMIT_KEYS}")
    for k, x in v.items():
        if x is not None and (isinstance(x, bool) or not isinstance(x, int) or x < 0):
            raise ValueError(f"{k} must be a non-negative integer or null")
    return v


def create(conn, tenant_id, name, settings_=None, limits=None, actor="system") -> dict:
    if not re.match(r"^[a-z][a-z0-9_-]{1,39}$", tenant_id or ""):
        raise ValueError("tenant_id: lowercase letters, digits, - or _ (2-40)")
    if conn.execute("SELECT 1 FROM enterprise_tenant WHERE tenant_id=?", (tenant_id,)).fetchone():
        raise ValueError(f"tenant {tenant_id} already exists")
    now = datetime.now()
    conn.execute("INSERT INTO enterprise_tenant (tenant_id,name,status,settings_json,limits_json,created_at,updated_at) "
                 "VALUES (?,?,?,?,?,?,?)", (tenant_id, name or tenant_id, "ACTIVE", json.dumps(settings_ or {}),
                                            json.dumps(_limits({**DEFAULT_LIMITS, **(limits or {})})), now, now))
    audit.record(conn, "tenant.create", tenant_id=tenant_id, actor=actor, resource=tenant_id, commit=False)
    conn.commit()
    return get(conn, tenant_id)


def ensure_default(conn) -> dict:
    tid = settings()["default_tenant"]
    if not get(conn, tid):
        create(conn, tid, "Owner", limits={k: None for k in LIMIT_KEYS}, actor="bootstrap")
    return get(conn, tid)


def get(conn, tenant_id) -> dict | None:
    r = conn.execute("SELECT * FROM enterprise_tenant WHERE tenant_id=?", (tenant_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["settings"] = json.loads(d.pop("settings_json") or "{}")
    d["limits"] = json.loads(d.pop("limits_json") or "{}")
    return d


def list_all(conn) -> list:
    return [get(conn, r[0]) for r in conn.execute("SELECT tenant_id FROM enterprise_tenant ORDER BY tenant_id")]


def set_status(conn, tenant_id, status, reason="", actor="system") -> dict:
    t = get(conn, tenant_id)
    if not t:
        raise ValueError(f"no tenant {tenant_id}")
    status = (status or "").upper()
    if status not in TRANSITIONS[t["status"]]:
        raise ValueError(f"{t['status']} -> {status} not allowed; allowed {sorted(TRANSITIONS[t['status']])}")
    if tenant_id == settings()["default_tenant"] and status != "ACTIVE":
        raise ValueError("the owner's default tenant cannot be suspended, disabled or archived")
    conn.execute("UPDATE enterprise_tenant SET status=?, updated_at=? WHERE tenant_id=?",
                 (status, datetime.now(), tenant_id))
    audit.record(conn, "tenant.status", tenant_id=tenant_id, actor=actor, resource=tenant_id,
                 details={"from": t["status"], "to": status, "reason": reason}, commit=False)
    conn.commit()
    return get(conn, tenant_id)


def update(conn, tenant_id, name=None, settings_=None, limits=None, actor="system") -> dict:
    t = get(conn, tenant_id)
    if not t:
        raise ValueError(f"no tenant {tenant_id}")
    lim = _limits({**t["limits"], **(limits or {})})
    conn.execute("UPDATE enterprise_tenant SET name=?, settings_json=?, limits_json=?, updated_at=? WHERE tenant_id=?",
                 (name or t["name"], json.dumps({**t["settings"], **(settings_ or {})}), json.dumps(lim),
                  datetime.now(), tenant_id))
    audit.record(conn, "tenant.update", tenant_id=tenant_id, actor=actor, resource=tenant_id,
                 details={"name": name, "settings": settings_, "limits": limits}, commit=False)
    conn.commit()
    return get(conn, tenant_id)


def effective_limits(conn, tenant_id) -> dict:
    """Tenant limits merged with the plan's: the tighter non-null value wins."""
    t = get(conn, tenant_id) or {"limits": {}}
    out = dict(t["limits"])
    try:
        from enterprise.billing import plan_limits
        for k, v in plan_limits(conn, tenant_id).items():
            if v is not None and (out.get(k) is None or v < out[k]):
                out[k] = v
    except Exception:
        pass
    return out


def usage(conn, tenant_id) -> dict:
    def n(q, *a):
        try:
            return conn.execute(q, a).fetchone()[0]
        except Exception:
            return None
    return {"users": n("SELECT COUNT(DISTINCT user_id) FROM enterprise_user_role WHERE tenant_id=?", tenant_id),
            "strategies": n("SELECT COUNT(*) FROM strategy WHERE COALESCE(tenant_id,'default')=?", tenant_id),
            "models": n("SELECT COUNT(*) FROM ml_model WHERE COALESCE(tenant_id,'default')=?", tenant_id),
            "api_keys": n("SELECT COUNT(*) FROM enterprise_api_key WHERE tenant_id=? AND revoked_at IS NULL", tenant_id)}


def check_limit(conn, tenant_id, key, adding=1):
    """Raise ValueError when adding `adding` would exceed the tenant's limit."""
    lim = effective_limits(conn, tenant_id).get(f"max_{key}")
    cur = usage(conn, tenant_id).get(key)
    if lim is not None and cur is not None and cur + adding > lim:
        raise ValueError(f"tenant {tenant_id} limit max_{key} = {lim} reached ({cur} in use)")
