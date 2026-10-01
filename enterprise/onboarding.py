"""
Self-service onboarding (W9, ENT-02).

Flow (each step is derived from real data, so the checklist cannot drift):
    1 registered       POST /api/auth/register (self-registration must be enabled) creates a
                       PENDING user + tenant + FREE trial (W7)
    2 activated        a platform administrator activates the user (PUT /api/admin/users/{id})
    3 terms_accepted   the user accepts the current terms (enterprise/privacy.py consent)
    4 email_verified   verification link (enterprise/email_flows.py; SANDBOX outbox by default)
    5 tenant_setup     POST /api/onboarding/setup: the tenant's paper book is opened
                       (execution/tenant_books.py, starting cash from the tenant setting
                       paper_starting_cash, default Rs 10,00,000) and defaults recorded
    6 first_watchlist  the tenant has at least one watchlist
    7 mfa_enabled      optional, recommended for administrators
status(conn, tenant) returns the checklist; complete when steps 1-6 hold.
"""

from __future__ import annotations

import json
from datetime import datetime

from enterprise import audit

STEPS = ("registered", "activated", "terms_accepted", "email_verified", "tenant_setup", "first_watchlist",
         "mfa_enabled")
REQUIRED = STEPS[:6]


def status(conn, tenant_id, user_id=None) -> dict:
    def one(q, *a):
        try:
            r = conn.execute(q, a).fetchone()
            return r[0] if r else None
        except Exception:
            return None
    if user_id is None:
        user_id = one("SELECT user_id FROM enterprise_user_role WHERE tenant_id=? AND role='SUPER_ADMIN' ORDER BY "
                      "granted_at LIMIT 1", tenant_id)
    from enterprise.privacy import terms_version
    steps = {
        "registered": bool(user_id),
        "activated": one("SELECT status FROM enterprise_user WHERE user_id=?", user_id) == "ACTIVE",
        "terms_accepted": bool(one("SELECT 1 FROM enterprise_consent WHERE user_id=? AND document='terms' AND "
                                   "version=?", user_id, terms_version())),
        "email_verified": bool(one("SELECT email_verified_at FROM enterprise_user WHERE user_id=?", user_id)),
        "tenant_setup": bool(one("SELECT 1 FROM tenant_paper_account WHERE tenant_id=?", tenant_id)) or
        tenant_id == _default(),
        "first_watchlist": bool(one("SELECT 1 FROM enterprise_watchlist WHERE tenant_id=? LIMIT 1", tenant_id)),
        "mfa_enabled": bool(one("SELECT mfa_enabled FROM enterprise_user WHERE user_id=?", user_id)),
    }
    done = all(steps[s] for s in REQUIRED)
    now = datetime.now()
    conn.execute("INSERT INTO enterprise_onboarding (tenant_id,steps_json,completed_at,updated_at) VALUES (?,?,?,?) "
                 "ON CONFLICT(tenant_id) DO UPDATE SET steps_json=excluded.steps_json, completed_at=COALESCE("
                 "enterprise_onboarding.completed_at, excluded.completed_at), updated_at=excluded.updated_at",
                 (tenant_id, json.dumps(steps), now if done else None, now))
    conn.commit()
    nxt = next((s for s in STEPS if not steps[s]), None)
    return {"tenant_id": tenant_id, "user_id": user_id, "steps": steps, "complete": done, "next": nxt}


def _default():
    from enterprise.config import settings
    return settings()["default_tenant"]


def setup_tenant(conn, tenant_id, actor, starting_cash=None) -> dict:
    """Open the tenant's paper book (idempotent) and return the checklist."""
    from execution.tenant_books import ensure_account
    if tenant_id == _default():
        raise ValueError("the owner's tenant uses the existing W1 paper book")
    if starting_cash is not None and (not isinstance(starting_cash, (int, float)) or not 10_000 <= starting_cash
                                      <= 100_000_000):
        raise ValueError("starting_cash must be between 10,000 and 100,000,000")
    acct = ensure_account(conn, tenant_id, starting_cash)
    audit.record(conn, "onboarding.tenant_setup", tenant_id=tenant_id, actor=actor,
                 details={"starting_cash": acct["starting_cash"]}, commit=False)
    conn.commit()
    return {"paper_account": acct, **status(conn, tenant_id)}
