"""
Subscription & billing foundation. NO PAYMENT PROCESSING: ATIP has no payment
gateway, and nothing here charges anyone. Invoices are DRAFT records only.

enterprise_plan          plan_id, name, price_month (NULL until the owner sets a
                         price -- no price is invented), currency, limits_json,
                         features_json, status
                         seeded: FREE, PRO, ENTERPRISE (limits only)
enterprise_subscription  tenant_id (one per tenant), plan_id, status TRIAL / ACTIVE /
                         PAST_DUE / CANCELLED / EXPIRED, started_at, current_period_end,
                         cancel_at
enterprise_usage         (tenant_id, date, metric) -> value: users, strategies, models,
                         api_keys, backtests, api_requests (metered daily, and
                         api_requests per request by the middleware)
enterprise_invoice       invoice_id, tenant_id, period, plan, amount (NULL if the plan
                         has no price), currency, status DRAFT, lines_json

The plan's limits merge with the tenant's (tenants.effective_limits: tighter wins).
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta

from enterprise import audit

SUB_STATUSES = ("TRIAL", "ACTIVE", "PAST_DUE", "CANCELLED", "EXPIRED")
PLANS = {
    "FREE": {"max_users": 2, "max_strategies": 5, "max_models": 2, "max_backtests_per_day": 5, "max_api_keys": 1},
    "PRO": {"max_users": 10, "max_strategies": 50, "max_models": 20, "max_backtests_per_day": 50, "max_api_keys": 5},
    "ENTERPRISE": {"max_users": None, "max_strategies": None, "max_models": None, "max_backtests_per_day": None,
                   "max_api_keys": None},
}
FEATURES = {"FREE": ["research", "strategies", "backtests"],
            "PRO": ["research", "strategies", "backtests", "ml", "quant", "paper_execution", "api_keys"],
            "ENTERPRISE": ["research", "strategies", "backtests", "ml", "quant", "paper_execution", "api_keys",
                           "multi_tenant_admin", "audit_export"]}


def seed(conn):
    for pid, lim in PLANS.items():
        conn.execute("INSERT OR IGNORE INTO enterprise_plan (plan_id,name,price_month,currency,limits_json,features_json,"
                     "status) VALUES (?,?,?,?,?,?,?)", (pid, pid.title(), None, "INR", json.dumps(lim),
                                                        json.dumps(FEATURES[pid]), "ACTIVE"))
    conn.commit()


def set_price(conn, plan_id, price_month, actor) -> dict:
    if price_month is not None and (not isinstance(price_month, (int, float)) or price_month < 0):
        raise ValueError("price_month must be a non-negative number or null")
    conn.execute("UPDATE enterprise_plan SET price_month=? WHERE plan_id=?", (price_month, plan_id))
    audit.record(conn, "billing.price", actor=actor, resource=plan_id, details={"price_month": price_month},
                 commit=False)
    conn.commit()
    return plans(conn, plan_id)[0]


def plans(conn, plan_id=None) -> list:
    q = "SELECT * FROM enterprise_plan" + (" WHERE plan_id=?" if plan_id else "")
    out = []
    for r in conn.execute(q, (plan_id,) if plan_id else ()):
        d = dict(r)
        d["limits"] = json.loads(d.pop("limits_json") or "{}")
        d["features"] = json.loads(d.pop("features_json") or "[]")
        out.append(d)
    return out


def subscribe(conn, tenant_id, plan_id, status="ACTIVE", days=30, actor="system") -> dict:
    if not plans(conn, plan_id):
        raise ValueError(f"no plan {plan_id}")
    if status not in SUB_STATUSES:
        raise ValueError(f"status must be one of {SUB_STATUSES}")
    now = datetime.now()
    conn.execute("INSERT INTO enterprise_subscription (tenant_id,plan_id,status,started_at,current_period_end,updated_at)"
                 " VALUES (?,?,?,?,?,?) ON CONFLICT(tenant_id) DO UPDATE SET plan_id=excluded.plan_id,"
                 "status=excluded.status,current_period_end=excluded.current_period_end,updated_at=excluded.updated_at",
                 (tenant_id, plan_id, status, now, now + timedelta(days=int(days)), now))
    audit.record(conn, "billing.subscription", tenant_id=tenant_id, actor=actor, details={"plan": plan_id,
                                                                                          "status": status},
                 commit=False)
    conn.commit()
    return subscription(conn, tenant_id)


def subscription(conn, tenant_id) -> dict | None:
    r = conn.execute("SELECT * FROM enterprise_subscription WHERE tenant_id=?", (tenant_id,)).fetchone()
    return dict(r) if r else None


def plan_limits(conn, tenant_id) -> dict:
    s = subscription(conn, tenant_id)
    if not s or s["status"] not in ("TRIAL", "ACTIVE", "PAST_DUE"):
        return {}
    p = plans(conn, s["plan_id"])
    return p[0]["limits"] if p else {}


def meter(conn, tenant_id, metric, value=None, increment=None, day=None) -> None:
    day = str(day or date.today())
    if increment is not None:
        conn.execute("INSERT INTO enterprise_usage (tenant_id,date,metric,value) VALUES (?,?,?,?) ON CONFLICT"
                     "(tenant_id,date,metric) DO UPDATE SET value=value+excluded.value",
                     (tenant_id, day, metric, increment))
    else:
        conn.execute("INSERT INTO enterprise_usage (tenant_id,date,metric,value) VALUES (?,?,?,?) ON CONFLICT"
                     "(tenant_id,date,metric) DO UPDATE SET value=excluded.value", (tenant_id, day, metric, value))


def meter_all(conn, day=None) -> int:
    from enterprise.tenants import list_all, usage
    n = 0
    for t in list_all(conn):
        for k, v in usage(conn, t["tenant_id"]).items():
            if v is not None:
                meter(conn, t["tenant_id"], k, value=v, day=day); n += 1
    conn.commit()
    return n


def draft_invoice(conn, tenant_id, period_start, period_end, actor) -> dict:
    s = subscription(conn, tenant_id)
    if not s:
        raise ValueError(f"tenant {tenant_id} has no subscription")
    p = plans(conn, s["plan_id"])[0]
    usage_rows = [dict(r) for r in conn.execute("SELECT metric, MAX(value) peak FROM enterprise_usage WHERE tenant_id=? "
                                                "AND date>=? AND date<=? GROUP BY metric",
                                                (tenant_id, str(period_start), str(period_end)))]
    iid = "INV" + uuid.uuid4().hex[:10].upper()
    conn.execute("INSERT INTO enterprise_invoice (invoice_id,tenant_id,period_start,period_end,plan_id,amount,currency,"
                 "status,lines_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (iid, tenant_id, str(period_start), str(period_end), p["plan_id"], p["price_month"], p["currency"],
                  "DRAFT", json.dumps({"plan": p["plan_id"], "usage_peaks": usage_rows}), datetime.now()))
    audit.record(conn, "billing.invoice_draft", tenant_id=tenant_id, actor=actor, resource=iid, commit=False)
    conn.commit()
    return {"invoice_id": iid, "amount": p["price_month"], "status": "DRAFT",
            "note": "no payment gateway: this is a record, nothing is charged"}
