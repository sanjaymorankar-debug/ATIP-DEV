"""
Notification channels, preferences and delivery (W9, ENT-10 / ENT-11).

Every enterprise notification is first an in-app inbox row (enterprise/notifications.py).
dispatch() then fans it out to the user's other channels for that category:

    channel    adapter            default mode   what "live" would do
    in_app     (the inbox row)    always         --
    email      EmailAdapter       SANDBOX        SMTP via the owner's server (mode "smtp")
    telegram   TelegramAdapter    SANDBOX        a per-user chat through the owner's bot (mode "live")
    webhook    WebhookAdapter     queue          signed POST to the tenant's registered endpoints
                                                 (ops/webhooks.py; only endpoints the tenant added)

SANDBOX = nothing leaves the machine: the message is written to
atip_data/outbox/<delivery_id>.eml (a local mail catcher for testing) and the
delivery row is status SANDBOX. Live modes are an OWNER configuration step
(config.json "saas.notifications"), and ops/config validation refuses them outside
the production environment.

Preferences (enterprise_notification_pref, per tenant / user / category):
    channels      ["in_app", "email", ...]   (default: in_app only)
    mode          immediate | digest          digest = one message per channel per day
    quiet hours   quiet_start / quiet_end "HH:MM" (IST) -> external delivery waits
    unsubscribed  1 = no external channel for this category (one-click unsubscribe link)
Deliveries: enterprise_notification_delivery (status QUEUED / DIGEST / SANDBOX / SENT /
FAILED / SKIPPED; destination masked).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, time as dtime, timedelta
from email.message import EmailMessage
from pathlib import Path

CHANNELS = ("in_app", "email", "telegram", "webhook")
OUTBOX = Path("atip_data") / "outbox"


def settings() -> dict:
    try:
        cfg = json.loads((Path("atip_data") / "config.json").read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    n = ((cfg.get("saas") or {}).get("notifications") or {})
    return {"email_mode": str((n.get("email") or {}).get("mode") or "sandbox").lower(),
            "email_from": (n.get("email") or {}).get("from") or "atip@localhost",
            "smtp_host": (n.get("email") or {}).get("smtp_host"),
            "smtp_port": int((n.get("email") or {}).get("smtp_port") or 587),
            "telegram_mode": str((n.get("telegram") or {}).get("mode") or "sandbox").lower(),
            "base_url": (cfg.get("saas") or {}).get("base_url") or "http://127.0.0.1:8000"}


def mask(dest: str | None) -> str | None:
    if not dest:
        return None
    if "@" in dest:
        u, d = dest.split("@", 1)
        return (u[:1] + "***@" + d) if u else "***@" + d
    return dest[:2] + "***" + dest[-2:] if len(dest) > 4 else "***"


# -- adapters ------------------------------------------------------------------------------

class EmailAdapter:
    name = "email"

    def send(self, to, subject, body, delivery_id) -> tuple:
        s = settings()
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = s["email_from"], to, subject
        msg["X-ATIP-Delivery"] = delivery_id
        msg.set_content(body)
        if s["email_mode"] != "smtp":
            OUTBOX.mkdir(parents=True, exist_ok=True)
            (OUTBOX / f"{delivery_id}.eml").write_bytes(bytes(msg))
            return "SANDBOX", None
        import smtplib
        from ops.secrets import get
        user, pw = get("SMTP_USERNAME"), get("SMTP_PASSWORD")
        if not s["smtp_host"] or not user or not pw:
            return "FAILED", "smtp mode without smtp_host / SMTP_USERNAME / SMTP_PASSWORD"
        with smtplib.SMTP(s["smtp_host"], s["smtp_port"], timeout=20) as smtp:
            smtp.starttls()
            smtp.login(user, pw)
            smtp.send_message(msg)
        return "SENT", None


class TelegramAdapter:
    name = "telegram"

    def send(self, chat_id, subject, body, delivery_id) -> tuple:
        if settings()["telegram_mode"] != "live":
            OUTBOX.mkdir(parents=True, exist_ok=True)
            (OUTBOX / f"{delivery_id}.telegram.txt").write_text(f"to chat {chat_id}\n{subject}\n\n{body}",
                                                                 encoding="utf-8")
            return "SANDBOX", None
        import urllib.parse
        import urllib.request
        from ops.secrets import get
        tok = get("TELEGRAM_TOKEN")
        if not tok:
            return "FAILED", "no TELEGRAM_TOKEN"
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": f"{subject}\n\n{body}"[:4000]}).encode()
        with urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=15) as r:
            return ("SENT", None) if r.status == 200 else ("FAILED", f"HTTP {r.status}")


class WebhookAdapter:
    name = "webhook"

    def send(self, tenant_id, subject, body, delivery_id, conn=None, category=None) -> tuple:
        from ops.webhooks import emit
        n = emit(conn, f"notification.{category or 'generic'}",
                 {"tenant_id": tenant_id, "subject": subject, "body": body}, event_id=delivery_id)
        return ("SENT", None) if n else ("SKIPPED", "no webhook endpoint subscribed")


ADAPTERS = {"email": EmailAdapter(), "telegram": TelegramAdapter(), "webhook": WebhookAdapter()}


# -- preferences ---------------------------------------------------------------------------

def get_prefs(conn, tenant_id, user_id, category) -> dict:
    r = conn.execute("SELECT channels_json, mode, quiet_start, quiet_end, unsubscribed FROM enterprise_notification_pref "
                     "WHERE tenant_id=? AND user_id=? AND category IN (?, '*') ORDER BY CASE category WHEN '*' THEN 1 "
                     "ELSE 0 END LIMIT 1", (tenant_id, user_id, category)).fetchone()
    if not r:
        return {"channels": ["in_app"], "mode": "immediate", "quiet_start": None, "quiet_end": None,
                "unsubscribed": False}
    return {"channels": json.loads(r[0] or '["in_app"]'), "mode": r[1] or "immediate", "quiet_start": r[2],
            "quiet_end": r[3], "unsubscribed": bool(r[4])}


def set_prefs(conn, tenant_id, user_id, category, channels=None, mode=None, quiet_start=None, quiet_end=None,
              unsubscribed=None) -> dict:
    from enterprise.notifications import CATEGORIES
    if category != "*" and category not in CATEGORIES:
        raise ValueError(f"category must be '*' or one of {CATEGORIES}")
    cur = get_prefs(conn, tenant_id, user_id, category)
    if channels is not None:
        bad = set(channels) - set(CHANNELS)
        if bad:
            raise ValueError(f"unknown channels {sorted(bad)}; known {CHANNELS}")
        cur["channels"] = sorted(set(channels) | {"in_app"})
    if mode is not None:
        if mode not in ("immediate", "digest"):
            raise ValueError("mode must be immediate or digest")
        cur["mode"] = mode
    for k, v in (("quiet_start", quiet_start), ("quiet_end", quiet_end)):
        if v is not None:
            if v and not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", v):
                raise ValueError(f"{k} must be HH:MM")
            cur[k] = v or None
    if unsubscribed is not None:
        cur["unsubscribed"] = bool(unsubscribed)
    conn.execute("INSERT INTO enterprise_notification_pref (tenant_id,user_id,category,channels_json,mode,quiet_start,"
                 "quiet_end,unsubscribed,updated_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(tenant_id,user_id,category) "
                 "DO UPDATE SET channels_json=excluded.channels_json, mode=excluded.mode, quiet_start=excluded.quiet_start,"
                 " quiet_end=excluded.quiet_end, unsubscribed=excluded.unsubscribed, updated_at=excluded.updated_at",
                 (tenant_id, user_id, category, json.dumps(cur["channels"]), cur["mode"], cur["quiet_start"],
                  cur["quiet_end"], int(cur["unsubscribed"]), datetime.now()))
    conn.commit()
    return cur


def list_prefs(conn, tenant_id, user_id) -> list:
    return [dict(r) for r in conn.execute("SELECT category, channels_json, mode, quiet_start, quiet_end, unsubscribed "
                                          "FROM enterprise_notification_pref WHERE tenant_id=? AND user_id=?",
                                          (tenant_id, user_id))]


def _in_quiet(p, now=None) -> datetime | None:
    """End of the current quiet window, or None when not quiet."""
    if not p.get("quiet_start") or not p.get("quiet_end"):
        return None
    now = now or datetime.now()
    s = dtime.fromisoformat(p["quiet_start"])
    e = dtime.fromisoformat(p["quiet_end"])
    t = now.time()
    inside = (s <= t < e) if s < e else (t >= s or t < e)
    if not inside:
        return None
    end = datetime.combine(now.date(), e)
    return end if end > now else end + timedelta(days=1)


def _destination(conn, user_id, channel):
    r = conn.execute("SELECT email, preferences_json, email_verified_at FROM enterprise_user WHERE user_id=?",
                     (user_id,)).fetchone()
    if not r:
        return None
    if channel == "email":
        return r[0] if r[0] and r[2] else None                  # verified addresses only
    if channel == "telegram":
        return (json.loads(r[1] or "{}").get("notifications") or {}).get("telegram_chat_id")
    return None


# -- dispatch ------------------------------------------------------------------------------

def dispatch(conn, notification_id, tenant_id, user_id, category, title, body) -> list:
    p = get_prefs(conn, tenant_id, user_id, category)
    out = []
    if p["unsubscribed"]:
        return out
    quiet_until = _in_quiet(p)
    for ch in p["channels"]:
        if ch == "in_app":
            continue
        dest = None if ch == "webhook" else _destination(conn, user_id, ch)
        did = "nd_" + uuid.uuid4().hex[:16]
        if ch != "webhook" and not dest:
            status, err = "SKIPPED", f"no verified {ch} destination"
        elif p["mode"] == "digest":
            status, err = "DIGEST", None
        elif quiet_until:
            status, err = "QUEUED", f"quiet hours until {quiet_until:%H:%M}"
        else:
            status, err = "PENDING", None
        conn.execute("INSERT INTO enterprise_notification_delivery (delivery_id,tenant_id,notification_id,user_id,"
                     "channel,destination_masked,subject,status,mode,error,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (did, tenant_id, notification_id, user_id, ch, mask(dest), title[:200], status, p["mode"], err,
                      datetime.now()))
        if status == "PENDING":
            _deliver(conn, did, ch, tenant_id, user_id, dest, title, body, category)
        out.append({"delivery_id": did, "channel": ch, "status": status})
    conn.commit()
    return out


def _deliver(conn, did, ch, tenant_id, user_id, dest, title, body, category=None):
    try:
        if ch == "webhook":
            st, err = ADAPTERS[ch].send(tenant_id, title, body + _footer(conn, user_id, tenant_id, category), did,
                                        conn=conn, category=category)
        else:
            st, err = ADAPTERS[ch].send(dest, title, body + _footer(conn, user_id, tenant_id, category), did)
    except Exception as e:
        st, err = "FAILED", f"{type(e).__name__}: {str(e)[:200]}"
    conn.execute("UPDATE enterprise_notification_delivery SET status=?, error=?, sent_at=? WHERE delivery_id=?",
                 (st, err, datetime.now() if st in ("SENT", "SANDBOX") else None, did))


def _footer(conn, user_id, tenant_id, category) -> str:
    if not category:
        return ""
    tok = unsubscribe_token(conn, user_id, tenant_id, category)
    return (f"\n\n--\nNot financial advice. ATIP is not SEBI registered.\n"
            f"Unsubscribe from '{category}': {settings()['base_url']}/api/notifications/unsubscribe?token={tok}")


def unsubscribe_token(conn, user_id, tenant_id, category) -> str:
    """Deterministic per (user, tenant, category), keyed by a server secret: a stolen link
    can only unsubscribe that one category."""
    from ops.secrets import get
    key = (get("ATIP_ENCRYPTION_KEY", log_access=False) or get("WEBHOOK_SECRET_DEFAULT", log_access=False) or
           "atip-unsubscribe-dev")
    raw = f"{user_id}|{tenant_id}|{category}"
    sig = hashlib.sha256((key + "|" + raw).encode()).hexdigest()[:32]
    import base64
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=") + "." + sig


def unsubscribe(conn, token) -> dict:
    import base64
    import hmac
    try:
        raw_b64, sig = token.split(".", 1)
        raw = base64.urlsafe_b64decode(raw_b64 + "=" * (-len(raw_b64) % 4)).decode()
        user_id, tenant_id, category = raw.split("|")
    except Exception:
        raise ValueError("invalid unsubscribe link")
    if not hmac.compare_digest(unsubscribe_token(conn, user_id, tenant_id, category).split(".", 1)[1], sig):
        raise ValueError("invalid unsubscribe link")
    set_prefs(conn, tenant_id, user_id, category, unsubscribed=True)
    return {"unsubscribed": category}


def run_pending(conn, digest=False) -> dict:
    """Scheduler tick: QUEUED deliveries whose quiet hours ended; with digest=True, one
    message per user / channel / tenant from the DIGEST rows."""
    sent = 0
    for did, ch, tid, uid, subj, nid in conn.execute(
            "SELECT delivery_id, channel, tenant_id, user_id, subject, notification_id FROM "
            "enterprise_notification_delivery WHERE status='QUEUED'").fetchall():
        p = get_prefs(conn, tid, uid, "*")
        if _in_quiet(p):
            continue
        body = conn.execute("SELECT body FROM enterprise_notification WHERE id=?", (nid,)).fetchone()
        _deliver(conn, did, ch, tid, uid, _destination(conn, uid, ch), subj, body[0] if body else "")
        sent += 1
    if digest:
        groups = {}
        for did, ch, tid, uid, subj in conn.execute("SELECT delivery_id, channel, tenant_id, user_id, subject FROM "
                                                     "enterprise_notification_delivery WHERE status='DIGEST'"):
            groups.setdefault((ch, tid, uid), []).append((did, subj))
        for (ch, tid, uid), items in groups.items():
            did = "nd_" + uuid.uuid4().hex[:16]
            dest = None if ch == "webhook" else _destination(conn, uid, ch)
            title = f"ATIP daily digest: {len(items)} notification(s)"
            conn.execute("INSERT INTO enterprise_notification_delivery (delivery_id,tenant_id,user_id,channel,"
                         "destination_masked,subject,status,mode,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (did, tid, uid, ch, mask(dest), title, "PENDING", "digest", datetime.now()))
            _deliver(conn, did, ch, tid, uid, dest, title, "\n".join(f"- {s}" for _, s in items))
            conn.executemany("UPDATE enterprise_notification_delivery SET status='DIGESTED', sent_at=? WHERE "
                             "delivery_id=?", [(datetime.now(), d) for d, _ in items])
            sent += 1
    conn.commit()
    return {"delivered": sent}


def deliveries(conn, tenant_id=None, user_id=None, limit=200) -> list:
    q, a = "SELECT * FROM enterprise_notification_delivery WHERE 1=1", []
    if tenant_id:
        q += " AND tenant_id=?"; a.append(tenant_id)
    if user_id:
        q += " AND user_id=?"; a.append(user_id)
    return [dict(r) for r in conn.execute(q + " ORDER BY created_at DESC LIMIT ?", a + [int(limit)])]
