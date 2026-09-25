"""
User notifications: an in-app inbox per user, with preferences.

notify(conn, tenant_id, category, title, body, permission=None, user_ids=None,
       severity="info")
    -> one enterprise_notification row per recipient: the given users, or every
    ACTIVE member of the tenant holding `permission`, filtered by each user's
    preferences (preferences.notifications.categories.<category> = false opts out).

Channels:
    in_app     always (the inbox: GET /api/account/notifications)
    telegram   only the platform owner's existing bot (alerts/telegram.py), and
               only for users of the default tenant who opt in -- ATIP has one
               bot token, it is not a per-user channel
    email      not implemented (no mail sender is configured; pending)

Categories used today: risk (W4 rejections / blocks / reviews), account, tenant,
alert (workspace alert rules), billing.
"""

from __future__ import annotations

from datetime import datetime

CATEGORIES = ("risk", "account", "tenant", "alert", "billing", "system")


def _recipients(conn, tenant_id, permission):
    from enterprise import rbac
    out = []
    for uid, role in conn.execute("SELECT r.user_id, r.role FROM enterprise_user_role r JOIN enterprise_user u ON "
                                  "u.user_id=r.user_id WHERE r.tenant_id=? AND u.status='ACTIVE'", (tenant_id,)):
        if permission is None or permission in rbac.role_permissions(conn, role):
            out.append(uid)
    return sorted(set(out))


def notify(conn, tenant_id, category, title, body="", permission=None, user_ids=None, severity="info") -> int:
    import json
    from enterprise.config import settings
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {CATEGORIES}")
    users = user_ids or _recipients(conn, tenant_id, permission)
    n = 0
    tg = False
    for uid in users:
        r = conn.execute("SELECT preferences_json FROM enterprise_user WHERE user_id=?", (uid,)).fetchone()
        prefs = (json.loads(r[0] or "{}") if r else {}).get("notifications", {})
        if prefs.get("categories", {}).get(category) is False:
            continue
        conn.execute("INSERT INTO enterprise_notification (tenant_id,user_id,category,severity,title,body,created_at) "
                     "VALUES (?,?,?,?,?,?,?)", (tenant_id, uid, category, severity, title[:200], (body or "")[:4000],
                                                datetime.now()))
        n += 1
        tg = tg or (prefs.get("channels", {}).get("telegram") is True and tenant_id == settings()["default_tenant"])
    conn.commit()
    if tg:
        try:
            from alerts.telegram import notify as tg_notify
            tg_notify(f"[ATIP {category}] {title}", body)
        except Exception:
            pass
    return n


def inbox(conn, user_id, unread_only=False, limit=100) -> list:
    q = "SELECT * FROM enterprise_notification WHERE user_id=?" + (" AND read_at IS NULL" if unread_only else "")
    return [dict(r) for r in conn.execute(q + " ORDER BY id DESC LIMIT ?", (user_id, int(limit)))]


def mark_read(conn, user_id, ids=None) -> int:
    q = "UPDATE enterprise_notification SET read_at=? WHERE user_id=? AND read_at IS NULL"
    args = [datetime.now(), user_id]
    if ids:
        q += f" AND id IN ({','.join('?' * len(ids))})"; args += list(ids)
    n = conn.execute(q, args).rowcount
    conn.commit()
    return n


def on_risk_decision(conn, rd) -> None:
    """Hook from the W4 execution cycle: tell the strategy's tenant about rejections,
    blocks and reviews (users holding risk:read)."""
    if rd.risk_status == "APPROVED":
        return
    r = conn.execute("SELECT COALESCE(tenant_id,'default') FROM strategy WHERE strategy_id=?",
                     (rd.strategy_id,)).fetchone()
    notify(conn, r[0] if r else "default", "risk", f"{rd.risk_status}: {rd.side} {rd.symbol} ({rd.strategy_id})",
           rd.rejection_reason or "", permission="risk:read",
           severity="warning" if rd.risk_status != "REVIEW_REQUIRED" else "info")
