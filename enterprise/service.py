"""
Enterprise bootstrap and scheduled jobs.

bootstrap(conn, username, password)  seeds roles / permissions / plans, creates the
    default tenant (ENTERPRISE plan, unlimited) and its first SUPER_ADMIN. Run it
    before switching enterprise.enabled on -- otherwise nobody could sign in. The
    password is typed by the owner (python -m enterprise bootstrap prompts for it);
    Claude never sets one.

ensure_seeded(conn)  idempotent seeding (roles, permissions, plans, default tenant),
    called by the API on first use.

run_scheduled(trade_date)  post-market (enterprise.enabled only): evaluate workspace
    alert rules, meter tenant usage.
"""

from __future__ import annotations

from enterprise import billing, rbac, tenants, users


def ensure_seeded(conn) -> dict:
    rbac.seed(conn)
    billing.seed(conn)
    t = tenants.ensure_default(conn)
    if not billing.subscription(conn, t["tenant_id"]):
        billing.subscribe(conn, t["tenant_id"], "ENTERPRISE", "ACTIVE", days=3650, actor="bootstrap")
    return {"default_tenant": t["tenant_id"]}


def bootstrap(conn, username, password, email=None) -> dict:
    ensure_seeded(conn)
    tid = tenants.ensure_default(conn)["tenant_id"]
    if conn.execute("SELECT 1 FROM enterprise_user_role WHERE tenant_id=? AND role='SUPER_ADMIN'", (tid,)).fetchone():
        raise ValueError("a SUPER_ADMIN already exists for the default tenant; bootstrap is one-time")
    u = users.create_user(conn, username, password, tid, ["SUPER_ADMIN"], email=email, actor="bootstrap")
    return {"user_id": u["user_id"], "username": u["username"], "tenant_id": tid,
            "next": 'set "enterprise": {"enabled": true} in atip_data/config.json and restart ATIP'}


def run_scheduled(trade_date=None) -> dict:
    from db.schema import get_connection
    from enterprise.config import enabled
    if not enabled():
        return {"status": "SKIPPED", "rows": 0, "reason": "enterprise.enabled is false"}
    conn = get_connection()
    try:
        ensure_seeded(conn)
        from enterprise.workspace import evaluate_alerts
        a = evaluate_alerts(conn, trade_date)
        n = billing.meter_all(conn, trade_date)
    finally:
        conn.close()
    return {"status": "SUCCESS", "rows": a["rules"], "alerts": a, "usage_rows": n}
