"""
Data privacy and compliance foundation (W9, ENT-17 / SEC-05; ENT-14 is legal work).

Data inventory (INVENTORY): every table's data class, whether it holds personal data,
and its retention rule. Classes: personal, credential, financial, research, market,
operational, audit.

Consent: record_consent / has_consent for versioned documents (terms, privacy). The
current versions come from config.json "saas.terms_version" / "saas.privacy_version".
Sign-in reports consent_required when the current terms were not accepted.

Data-subject requests (enterprise_privacy_request):
    request(conn, tenant, user, "export" | "delete", reason)       -> PENDING
    decide(conn, request_id, approve, actor)                         admin (admin:users);
        approve -> execute:
          export  a JSON file under atip_data/privacy_exports/<request_id>.json with the
                  user's personal data (profile, memberships, sessions metadata, API-key
                  metadata, notifications, preferences, workspace items, consents, vault
                  METADATA, their audit rows) -- never secrets or ciphertext
          delete  anonymise: username -> deleted-<id>, e-mail / display name / preferences
                  cleared, MFA cleared, status DISABLED, deleted_at set; sessions, refresh
                  tokens and API keys revoked; watchlists, alert rules, reports, notification
                  preferences / deliveries and vault credentials removed. Audit rows are
                  KEPT (append-only, tamper-evident): they reference the user id only.
        reject -> REJECTED with the reason
Retention (purge_expired, daily): operational rows only -- read notifications, deliveries,
report outputs, expired sessions / refresh / e-mail tokens, API usage, idempotency --
per the tenant setting retention_days (defaults below). Market, research, financial and
audit data are never purged by this job.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from enterprise import audit

INVENTORY = {
    "enterprise_user": ("personal", True, "account lifetime; anonymised on an approved delete request"),
    "enterprise_session": ("personal", True, "expired sessions purged after 30 days"),
    "enterprise_refresh_token": ("credential", True, "expired tokens purged after 30 days"),
    "enterprise_api_key": ("credential", True, "digest only; revoked keys kept for audit"),
    "enterprise_email_token": ("credential", True, "purged 7 days after expiry"),
    "enterprise_password_reset": ("credential", True, "digest only"),
    "enterprise_vault_credential": ("credential", True, "ciphertext only; wiped on revoke / delete request"),
    "enterprise_notification": ("personal", True, "read notifications purged after retention_days (180)"),
    "enterprise_notification_delivery": ("personal", True, "purged after retention_days (90)"),
    "enterprise_notification_pref": ("personal", True, "account lifetime"),
    "enterprise_watchlist": ("personal", True, "account lifetime"),
    "enterprise_alert_rule": ("personal", True, "account lifetime"),
    "enterprise_report": ("personal", True, "account lifetime"),
    "enterprise_report_output": ("financial", True, "purged after retention_days (90)"),
    "enterprise_consent": ("personal", True, "kept as proof of consent"),
    "enterprise_privacy_request": ("personal", True, "kept as proof of handling"),
    "enterprise_audit": ("audit", True, "append-only; never purged by ATIP"),
    "enterprise_api_usage": ("operational", False, "purged after 400 days"),
    "enterprise_usage": ("operational", False, "billing evidence; kept"),
    "enterprise_invoice": ("financial", False, "kept (accounting)"),
    "enterprise_payment": ("financial", False, "kept (accounting); no card data is ever stored"),
    "tenant_paper_account": ("financial", False, "tenant lifetime"),
    "tenant_paper_position": ("financial", False, "tenant lifetime"),
    "tenant_paper_fill": ("financial", False, "tenant lifetime"),
    "oms_order": ("financial", False, "kept (trade audit)"),
    "strategy": ("research", False, "tenant lifetime"),
    "prices_daily": ("market", False, "600 days (W1 purge)"),
    "ops_idempotency": ("operational", False, "expires after 24 h"),
    "ops_secret_access": ("operational", False, "names only; kept"),
}
DEFAULT_RETENTION = {"notifications_read": 180, "deliveries": 90, "report_outputs": 90, "api_usage": 400}
EXPORT_DIR = Path("atip_data") / "privacy_exports"


def _cfg() -> dict:
    try:
        return json.loads((Path("atip_data") / "config.json").read_text(encoding="utf-8")).get("saas") or {}
    except Exception:
        return {}


def terms_version() -> str:
    return str(_cfg().get("terms_version") or "1.0")


def privacy_version() -> str:
    return str(_cfg().get("privacy_version") or "1.0")


def record_consent(conn, user_id, tenant_id, document, version=None, ip=None) -> dict:
    if document not in ("terms", "privacy", "risk_disclosure"):
        raise ValueError("document must be terms, privacy or risk_disclosure")
    version = version or (terms_version() if document == "terms" else privacy_version())
    conn.execute("INSERT OR IGNORE INTO enterprise_consent (tenant_id,user_id,document,version,accepted_at,ip) VALUES "
                 "(?,?,?,?,?,?)", (tenant_id, user_id, document, version, datetime.now(), ip))
    audit.record(conn, "privacy.consent", tenant_id=tenant_id, user_id=user_id, actor=user_id,
                 details={"document": document, "version": version}, commit=False)
    conn.commit()
    return {"document": document, "version": version, "accepted": True}


def has_consent(conn, user_id, document="terms") -> bool:
    v = terms_version() if document == "terms" else privacy_version()
    try:
        return conn.execute("SELECT 1 FROM enterprise_consent WHERE user_id=? AND document=? AND version=?",
                            (user_id, document, v)).fetchone() is not None
    except Exception:
        return True


def consents(conn, user_id) -> list:
    return [dict(r) for r in conn.execute("SELECT document, version, accepted_at FROM enterprise_consent WHERE "
                                          "user_id=? ORDER BY accepted_at", (user_id,))]


def request(conn, tenant_id, user_id, kind, reason="") -> dict:
    if kind not in ("export", "delete"):
        raise ValueError("kind must be export or delete")
    if conn.execute("SELECT 1 FROM enterprise_privacy_request WHERE user_id=? AND kind=? AND status='PENDING'",
                    (user_id, kind)).fetchone():
        raise ValueError(f"a {kind} request is already pending")
    rid = "prq_" + uuid.uuid4().hex[:12]
    conn.execute("INSERT INTO enterprise_privacy_request (request_id,tenant_id,user_id,kind,status,reason,requested_at) "
                 "VALUES (?,?,?,?,?,?,?)", (rid, tenant_id, user_id, kind, "PENDING", (reason or "")[:500],
                                            datetime.now()))
    audit.record(conn, f"privacy.{kind}_requested", tenant_id=tenant_id, user_id=user_id, actor=user_id, resource=rid,
                 commit=False)
    conn.commit()
    try:
        from enterprise.notifications import notify
        notify(conn, tenant_id, "privacy", f"Privacy request: {kind}", f"request {rid} awaits a decision",
               permission="admin:users")
    except Exception:
        pass
    return get_request(conn, rid)


def get_request(conn, rid) -> dict:
    r = conn.execute("SELECT * FROM enterprise_privacy_request WHERE request_id=?", (rid,)).fetchone()
    if not r:
        raise ValueError(f"no request {rid}")
    d = dict(r)
    d["result"] = json.loads(d.pop("result_json") or "null")
    return d


def list_requests(conn, tenant_id=None, user_id=None) -> list:
    q, a = "SELECT request_id FROM enterprise_privacy_request WHERE 1=1", []
    if tenant_id:
        q += " AND tenant_id=?"; a.append(tenant_id)
    if user_id:
        q += " AND user_id=?"; a.append(user_id)
    return [get_request(conn, r[0]) for r in conn.execute(q + " ORDER BY requested_at DESC", a)]


def decide(conn, rid, approve, actor, note="") -> dict:
    req = get_request(conn, rid)
    if req["status"] != "PENDING":
        raise ValueError(f"request {rid} is {req['status']}")
    now = datetime.now()
    if not approve:
        conn.execute("UPDATE enterprise_privacy_request SET status='REJECTED', decided_by=?, decided_at=?, result_json=? "
                     "WHERE request_id=?", (actor, now, json.dumps({"note": note}), rid))
        audit.record(conn, "privacy.rejected", tenant_id=req["tenant_id"], user_id=req["user_id"], actor=actor,
                     resource=rid, commit=False)
        conn.commit()
        return get_request(conn, rid)
    result = export_user(conn, req["user_id"], rid) if req["kind"] == "export" else delete_user(conn, req["user_id"],
                                                                                                 actor)
    conn.execute("UPDATE enterprise_privacy_request SET status='COMPLETED', decided_by=?, decided_at=?, completed_at=?, "
                 "result_json=? WHERE request_id=?", (actor, now, datetime.now(), json.dumps(result, default=str), rid))
    audit.record(conn, f"privacy.{req['kind']}_completed", tenant_id=req["tenant_id"], user_id=req["user_id"],
                 actor=actor, resource=rid, commit=False)
    conn.commit()
    return get_request(conn, rid)


def _rows(conn, q, *a):
    try:
        return [dict(r) for r in conn.execute(q, a)]
    except Exception:
        return []


def export_user(conn, user_id, rid) -> dict:
    from enterprise.users import get
    data = {
        "generated_at": datetime.now().isoformat(timespec="seconds"), "user": get(conn, user_id),
        "sessions": _rows(conn, "SELECT created_at, expires_at, last_seen_at, ip, user_agent, revoked_at FROM "
                                "enterprise_session WHERE user_id=?", user_id),
        "api_keys": _rows(conn, "SELECT key_id, name, scopes_json, created_at, expires_at, last_used_at, revoked_at "
                                "FROM enterprise_api_key WHERE user_id=?", user_id),
        "notifications": _rows(conn, "SELECT category, severity, title, body, created_at, read_at FROM "
                                     "enterprise_notification WHERE user_id=?", user_id),
        "notification_preferences": _rows(conn, "SELECT * FROM enterprise_notification_pref WHERE user_id=?", user_id),
        "watchlists": _rows(conn, "SELECT * FROM enterprise_watchlist WHERE user_id=?", user_id),
        "alert_rules": _rows(conn, "SELECT * FROM enterprise_alert_rule WHERE user_id=?", user_id),
        "reports": _rows(conn, "SELECT * FROM enterprise_report WHERE user_id=?", user_id),
        "consents": _rows(conn, "SELECT document, version, accepted_at, ip FROM enterprise_consent WHERE user_id=?",
                          user_id),
        "vault_credentials_metadata": _rows(conn, "SELECT broker, label, field_names_json, status, created_at, "
                                                  "rotated_at FROM enterprise_vault_credential WHERE user_id=?",
                                            user_id),
        "audit": _rows(conn, "SELECT at, action, resource, method, path, status_code, ip FROM enterprise_audit WHERE "
                             "user_id=? ORDER BY id DESC LIMIT 5000", user_id),
    }
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    p = EXPORT_DIR / f"{rid}.json"
    p.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    return {"export_file": str(p), "sections": {k: (len(v) if isinstance(v, list) else 1) for k, v in data.items()}}


def delete_user(conn, user_id, actor) -> dict:
    from enterprise.users import revoke_sessions
    from enterprise.config import settings
    r = conn.execute("SELECT username FROM enterprise_user WHERE user_id=?", (user_id,)).fetchone()
    if not r:
        raise ValueError(f"no user {user_id}")
    owner_admin = conn.execute("SELECT 1 FROM enterprise_user_role WHERE user_id=? AND tenant_id=? AND "
                               "role='SUPER_ADMIN'", (user_id, settings()["default_tenant"])).fetchone()
    if owner_admin and conn.execute("SELECT COUNT(*) FROM enterprise_user_role WHERE tenant_id=? AND role='SUPER_ADMIN'",
                                    (settings()["default_tenant"],)).fetchone()[0] <= 1:
        raise ValueError("refused: this is the platform's last administrator")
    now = datetime.now()
    conn.execute("UPDATE enterprise_user SET username=?, email=NULL, display_name=NULL, preferences_json='{}', "
                 "mfa_enabled=0, mfa_secret_enc=NULL, mfa_pending_enc=NULL, email_verified_at=NULL, status='DISABLED', "
                 "deleted_at=?, updated_at=? WHERE user_id=?", (f"deleted-{user_id[-8:]}", now, now, user_id))
    revoke_sessions(conn, user_id)
    conn.execute("UPDATE enterprise_api_key SET revoked_at=COALESCE(revoked_at, ?) WHERE user_id=?", (now, user_id))
    removed = {}
    for t in ("enterprise_watchlist", "enterprise_alert_rule", "enterprise_report", "enterprise_notification_pref",
              "enterprise_notification_delivery", "enterprise_notification", "enterprise_vault_credential",
              "enterprise_email_token"):
        try:
            removed[t] = conn.execute(f"DELETE FROM {t} WHERE user_id=?", (user_id,)).rowcount
        except Exception:
            removed[t] = 0
    conn.execute("DELETE FROM enterprise_user_role WHERE user_id=?", (user_id,))
    conn.commit()
    return {"anonymised": True, "removed": removed, "audit_rows": "kept (append-only)"}


def purge_expired(conn) -> dict:
    """Operational retention only. Per-tenant overrides: tenant settings retention_days {...}."""
    from enterprise.tenants import list_all
    now = datetime.now()
    out = {}
    for t in list_all(conn):
        days = {**DEFAULT_RETENTION, **((t.get("settings") or {}).get("retention_days") or {})}
        tid = t["tenant_id"]
        n = conn.execute("DELETE FROM enterprise_notification WHERE tenant_id=? AND read_at IS NOT NULL AND "
                         "created_at<?", (tid, now - timedelta(days=int(days["notifications_read"])))).rowcount
        n += conn.execute("DELETE FROM enterprise_notification_delivery WHERE tenant_id=? AND created_at<?",
                          (tid, now - timedelta(days=int(days["deliveries"])))).rowcount
        n += conn.execute("DELETE FROM enterprise_report_output WHERE tenant_id=? AND created_at<?",
                          (tid, now - timedelta(days=int(days["report_outputs"])))).rowcount
        n += conn.execute("DELETE FROM enterprise_api_usage WHERE tenant_id=? AND date<?",
                          (tid, str((now - timedelta(days=int(days["api_usage"]))).date()))).rowcount
        out[tid] = n
    out["_sessions"] = conn.execute("DELETE FROM enterprise_session WHERE expires_at<?",
                                    (now - timedelta(days=30),)).rowcount
    out["_refresh"] = conn.execute("DELETE FROM enterprise_refresh_token WHERE expires_at<?",
                                   (now - timedelta(days=30),)).rowcount
    out["_email_tokens"] = conn.execute("DELETE FROM enterprise_email_token WHERE expires_at<?",
                                        (now - timedelta(days=7),)).rowcount
    conn.commit()
    return out


def inventory() -> list:
    return [{"table": t, "class": c, "personal": p, "retention": r} for t, (c, p, r) in sorted(INVENTORY.items())]
