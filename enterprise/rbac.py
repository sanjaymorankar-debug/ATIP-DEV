"""
Role-based access control: roles, permissions, role -> permission mapping.

Permissions are "<module>:<action>". Every API route maps to exactly one of
them in enterprise/authz.py (ROUTE_RULES); business logic never checks roles.
The mapping is seeded into enterprise_role_permission and can be edited by a
SUPER_ADMIN (PUT /api/admin/roles/{role}); SUPER_ADMIN always has everything.

Roles are assigned per tenant (enterprise_user_role: tenant_id, user_id, role).
"""

from __future__ import annotations

from datetime import datetime

PERMISSIONS = {
    "dashboard:read": "view dashboards and market data",
    "research:read": "view backtests, experiments, research results",
    "research:run": "run backtests, experiments, factor research",
    "strategy:read": "view strategies, decisions, health",
    "strategy:write": "create strategies and versions, edit metadata, generate decisions",
    "strategy:lifecycle": "activate / pause / disable / retire strategies, regime mapping",
    "quant:read": "view factors, scores, rankings, pairs, portfolios",
    "quant:write": "compute factors, define composites, pairs, portfolios",
    "ml:read": "view ML features, datasets, models, predictions",
    "ml:write": "define feature sets, datasets, models; train; predict",
    "ml:lifecycle": "validate / approve / activate / pause models",
    "risk:read": "view risk limits, decisions, exposure",
    "risk:configure": "change risk limits and risk profiles",
    "risk:approve": "approve REVIEW_REQUIRED risk decisions",
    "execution:read": "view orders, fills, positions, execution status",
    "execution:trade": "create / submit / cancel orders, run the execution cycle",
    "orders:manage": "manage W1 order rules (create / confirm / reject / delete)",
    "portfolio:read": "view portfolio, holdings, P&L",
    "portfolio:manage": "manage portfolio settings, rebalancing inputs",
    "notifications:read": "read own notifications",
    "workspace:write": "own watchlists, alert rules, saved reports, API keys, preferences",
    "system:operate": "operate the platform: pipeline refresh, kill switch, data jobs",
    "audit:read": "read the audit log",
    "admin:users": "manage users, roles, password resets",
    "admin:tenants": "manage tenants, tenant settings and limits",
    "admin:roles": "edit role permissions",
    "admin:billing": "manage plans, subscriptions, usage, invoices",
}

_READ = ["dashboard:read", "notifications:read", "workspace:write"]
ROLES = {
    "SUPER_ADMIN": ("platform / tenant administrator: every permission", list(PERMISSIONS)),
    "RESEARCHER": ("research, factors, strategies (read), backtests",
                   _READ + ["research:read", "research:run", "strategy:read", "quant:read", "ml:read",
                            "portfolio:read"]),
    "QUANT": ("researcher + builds strategies, factors and models",
              _READ + ["research:read", "research:run", "strategy:read", "strategy:write", "quant:read",
                       "quant:write", "ml:read", "ml:write", "portfolio:read", "risk:read"]),
    "STRATEGY_MANAGER": ("owns strategy lifecycle and model activation",
                         _READ + ["research:read", "research:run", "strategy:read", "strategy:write",
                                  "strategy:lifecycle", "quant:read", "ml:read", "ml:lifecycle", "risk:read",
                                  "execution:read", "portfolio:read"]),
    "RISK_MANAGER": ("risk configuration, approvals, exposure",
                     _READ + ["risk:read", "risk:configure", "risk:approve", "execution:read", "strategy:read",
                              "portfolio:read", "quant:read", "audit:read", "system:operate"]),
    "TRADER": ("execution, orders, positions",
               _READ + ["execution:read", "execution:trade", "orders:manage", "portfolio:read", "risk:read",
                        "strategy:read"]),
    "PORTFOLIO_MANAGER": ("portfolio construction and oversight",
                          _READ + ["portfolio:read", "portfolio:manage", "quant:read", "quant:write",
                                   "strategy:read", "risk:read", "execution:read", "research:read"]),
    "VIEWER": ("read-only dashboards",
               ["dashboard:read", "notifications:read", "research:read", "strategy:read", "quant:read",
                "ml:read", "risk:read", "execution:read", "portfolio:read"]),
}


def seed(conn) -> dict:
    """Idempotent: inserts missing roles / permissions / mappings; never removes an
    owner's edits (except that SUPER_ADMIN always regains every permission)."""
    now = datetime.now()
    for p, d in PERMISSIONS.items():
        conn.execute("INSERT OR IGNORE INTO enterprise_permission (permission,description) VALUES (?,?)", (p, d))
    for r, (d, perms) in ROLES.items():
        new = conn.execute("INSERT OR IGNORE INTO enterprise_role (role,description,builtin,created_at) "
                           "VALUES (?,?,1,?)", (r, d, now)).rowcount
        if new or r == "SUPER_ADMIN":
            conn.executemany("INSERT OR IGNORE INTO enterprise_role_permission (role,permission) VALUES (?,?)",
                             [(r, p) for p in perms])
    conn.commit()
    return {"roles": len(ROLES), "permissions": len(PERMISSIONS)}


def role_permissions(conn, role: str) -> set:
    if role == "SUPER_ADMIN":
        return set(PERMISSIONS)
    return {r[0] for r in conn.execute("SELECT permission FROM enterprise_role_permission WHERE role=?", (role,))}


def permissions_for(conn, roles) -> set:
    out = set()
    for r in roles or ():
        out |= role_permissions(conn, r)
    return out


def set_role_permissions(conn, role: str, permissions: list) -> set:
    if role not in {r[0] for r in conn.execute("SELECT role FROM enterprise_role")}:
        raise ValueError(f"unknown role {role}")
    if role == "SUPER_ADMIN":
        raise ValueError("SUPER_ADMIN always has every permission")
    unknown = set(permissions) - set(PERMISSIONS)
    if unknown:
        raise ValueError(f"unknown permissions {sorted(unknown)}")
    conn.execute("DELETE FROM enterprise_role_permission WHERE role=?", (role,))
    conn.executemany("INSERT INTO enterprise_role_permission (role,permission) VALUES (?,?)",
                     [(role, p) for p in sorted(set(permissions))])
    conn.commit()
    return role_permissions(conn, role)
