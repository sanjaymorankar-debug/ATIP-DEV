"""
Pending-registration approvals.

Self-registered users start PENDING (users.create_user). An administrator approves them
(-> ACTIVE) or rejects them (-> REJECTED, with an optional reason the user sees at sign-in).
Every decision is written to enterprise_approval_log (who decided what about whom, in UTC and
IST) and to the hash-chained audit log, and the applicant is e-mailed (best-effort: a mail
failure never undoes the decision).

Scope: the platform administrator (SUPER_ADMIN in the default tenant) decides for everyone;
any other administrator only for users who belong to their own tenant.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from enterprise import audit

IST = timezone(timedelta(hours=5, minutes=30))


def ist_now() -> str:
    return datetime.now(IST).strftime("%d %b %Y %H:%M:%S IST")


def _scope_sql(tenant_id):
    if tenant_id is None:
        return "", ()
    return " AND u.user_id IN (SELECT user_id FROM enterprise_user_role WHERE tenant_id=?)", (tenant_id,)


def pending(conn, tenant_id=None) -> list:
    sql, args = _scope_sql(tenant_id)
    rows = conn.execute("SELECT u.user_id, u.username, u.email, u.display_name, u.created_at FROM enterprise_user u "
                        "WHERE u.status='PENDING' AND u.deleted_at IS NULL" + sql + " ORDER BY u.created_at", args)
    return [{"user_id": r[0], "username": r[1], "email": r[2], "name": r[3], "requested_at": str(r[4])} for r in rows]


def count(conn, tenant_id=None) -> int:
    sql, args = _scope_sql(tenant_id)
    return conn.execute("SELECT COUNT(*) FROM enterprise_user u WHERE u.status='PENDING' AND u.deleted_at IS NULL" + sql,
                        args).fetchone()[0]


def history(conn, limit=200) -> list:
    rows = conn.execute("SELECT id, at_utc, at_ist, user_id, username, action, actor, reason FROM "
                        "enterprise_approval_log ORDER BY id DESC LIMIT ?", (int(limit),))
    return [dict(zip(("id", "at_utc", "at_ist", "user_id", "username", "action", "actor", "reason"), r)) for r in rows]


def decide(conn, user_id, action, actor, reason=None, tenant_id=None) -> dict:
    """action: APPROVE | REJECT. `tenant_id` limits the decision to that tenant's users."""
    action = (action or "").upper()
    if action not in ("APPROVE", "REJECT"):
        raise ValueError("action must be APPROVE or REJECT")
    reason = (reason or "").strip()[:500] or None
    if tenant_id is not None and user_id not in {r["user_id"] for r in pending(conn, tenant_id)}:
        raise ValueError(f"no pending user {user_id}")
    r = conn.execute("SELECT username, email, status FROM enterprise_user WHERE user_id=? AND deleted_at IS NULL",
                     (user_id,)).fetchone()
    if not r:
        raise ValueError(f"no user {user_id}")
    username, email, status = r
    if status != "PENDING":
        raise ValueError(f"request already {status.lower()}")
    new = "ACTIVE" if action == "APPROVE" else "REJECTED"
    now = datetime.now()
    # conditional update: two administrators acting at once cannot both win
    n = conn.execute("UPDATE enterprise_user SET status=?, rejection_reason=?, updated_at=? "
                     "WHERE user_id=? AND status='PENDING'",
                     (new, reason if action == "REJECT" else None, now, user_id)).rowcount
    if not n:
        raise ValueError("request already handled")
    conn.execute("INSERT INTO enterprise_approval_log (at_utc,at_ist,user_id,username,action,actor,reason) "
                 "VALUES (?,?,?,?,?,?,?)",
                 (datetime.now(timezone.utc).replace(tzinfo=None), ist_now(), user_id, username, action, actor,
                  reason if action == "REJECT" else None))
    audit.record(conn, "user.approve" if action == "APPROVE" else "user.reject", user_id=user_id, actor=actor,
                 resource=username, details={"status": new, "reason": reason, "at_ist": ist_now()}, commit=False)
    conn.commit()
    _notify(conn, user_id, email, action, reason)
    return {"user_id": user_id, "username": username, "status": new}


def _notify(conn, user_id, email, action, reason):
    if not email:
        return
    try:
        from enterprise.email_flows import _mail
        if action == "APPROVE":
            _mail(conn, user_id, email, "Your ATIP account was approved", "Your account is active. You can sign in now.")
        else:
            _mail(conn, user_id, email, "Your ATIP registration was not approved",
                  "Your registration request was not approved." + (f"\nReason: {reason}" if reason else ""))
        conn.commit()
    except Exception:
        pass
