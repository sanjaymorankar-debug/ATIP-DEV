"""
Idempotent platform SUPER_ADMIN seed.

    SUPER_ADMIN_EMAILS="a@example.com,b@example.com" python -m enterprise.super_admin_seed

* Emails come only from the environment; no email or password lives in code or Git.
* A new account gets a random, unusable password, status ACTIVE, e-mail pre-verified and
  must_change_password set, plus SUPER_ADMIN in the default tenant (= platform administrator).
  The owner sets a real password with "Forgot password" (one-time code by e-mail), or an
  administrator issues a reset token.
* An existing user keeps their password and data: they only gain SUPER_ADMIN and, if PENDING,
  become ACTIVE. SUSPENDED-type states (DISABLED / LOCKED / REJECTED) are left alone.
* Safe to run repeatedly: no duplicate users or roles.
"""

from __future__ import annotations

import os
import re
import secrets
import sys
from datetime import datetime

from enterprise import audit, rbac
from enterprise import security as S
from enterprise.config import settings

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def parse_emails(raw) -> list:
    seen, out = set(), []
    for e in (raw or "").split(","):
        e = e.strip().lower()
        if EMAIL_RE.match(e) and e not in seen:
            seen.add(e)
            out.append(e)
    return out


def _username(conn, email):
    base = re.sub(r"[^a-z0-9._-]", "", email.split("@")[0].lower())
    base = (base if base[:1].isalpha() else "u" + base)[:36].ljust(3, "0")
    name, i = base, 1
    while conn.execute("SELECT 1 FROM enterprise_user WHERE username=?", (name,)).fetchone():
        i += 1
        name = f"{base[:34]}{i}"
    return name


def seed(conn, emails, tenant_id=None) -> list:
    from enterprise import tenants
    rbac.seed(conn)
    tid = tenant_id or settings()["default_tenant"]
    if not tenants.get(conn, tid):
        tenants.create(conn, tid, "Platform", actor="super-admin-seed")
    now, out = datetime.now(), []
    for email in emails:
        row = conn.execute("SELECT user_id, status FROM enterprise_user WHERE LOWER(email)=?", (email,)).fetchone()
        outcome = "existing"
        if not row:
            uid = "U" + secrets.token_hex(6).upper()
            conn.execute("INSERT INTO enterprise_user (user_id,username,email,display_name,password_hash,status,"
                         "failed_logins,must_change_password,preferences_json,created_at,updated_at,created_by,"
                         "email_verified_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (uid, _username(conn, email), email, email.split("@")[0],
                          S.hash_password(secrets.token_urlsafe(32) + "aA1!"), "ACTIVE", 0, 1, "{}", now, now,
                          "super-admin-seed", now))
            outcome, status = "created", "ACTIVE"
        else:
            uid, status = row[0], row[1]
            if status == "PENDING":
                conn.execute("UPDATE enterprise_user SET status='ACTIVE', email_verified_at=COALESCE(email_verified_at,?),"
                             " updated_at=? WHERE user_id=?", (now, now, uid))
        if not conn.execute("SELECT 1 FROM enterprise_user_role WHERE tenant_id=? AND user_id=? AND role='SUPER_ADMIN'",
                            (tid, uid)).fetchone():
            conn.execute("INSERT INTO enterprise_user_role (tenant_id,user_id,role,granted_by,granted_at) "
                         "VALUES (?,?,?,?,?)", (tid, uid, "SUPER_ADMIN", "super-admin-seed", now))
            audit.record(conn, "user.roles", tenant_id=tid, user_id=uid, actor="super-admin-seed",
                         details={"roles": ["SUPER_ADMIN"]}, commit=False)
        out.append({"email": email, "outcome": outcome})
    conn.commit()
    return out


def main() -> int:
    emails = parse_emails(os.environ.get("SUPER_ADMIN_EMAILS"))
    if not emails:
        print("SUPER_ADMIN_EMAILS is empty or has no valid e-mail addresses", file=sys.stderr)
        return 1
    from db.schema import get_connection
    conn = get_connection()
    try:
        for r in seed(conn, emails):
            print(f"SUPER_ADMIN {r['outcome']}: {r['email']}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
