"""
Per-user broker credential vault (W9 foundation, completed in W37: ENT-06).

    store(conn, tenant, user, broker, fields: dict, label="default")
        encrypts the whole field set with AES-256-GCM (ops/crypto.py; AAD binds the
        ciphertext to tenant / user / broker / label) and keeps ONLY the ciphertext and
        the field NAMES. Replacing an existing credential counts as a rotation.
    list_for(conn, tenant, user)       metadata only: broker, label, field names, status,
                                       created / rotated / last accessed, access count
    revoke(conn, ...)                  status REVOKED + ciphertext wiped
    _reveal(conn, credential_id, purpose)
        server-side use only (future broker connectors); every call is audited and
        counted. NOTHING in W9 calls it: no broker connection is made with vault
        credentials, and no API route returns a value (write-only by design).

Requires the encryption key (python -m ops keygen); without it storing is refused --
nothing is ever kept in plaintext. Development never enters a credential: the owner
and each user enter their own.

W37 completion:
    expires_at        store(..., expires_at=) or the broker's rule (EXPIRY_HOURS: Dhan, Zerodha and
                      Upstox access tokens are daily); expired credentials are reported, never used
    credentials_for(conn, tenant, user, broker, purpose)
                      the ONE way a broker connector (brokers/, BR-07) or importer (PF-12) obtains a
                      credential: the newest ACTIVE, unexpired entry, decrypted server-side, audited
                      ("vault.accessed" with the purpose). For the owner's default tenant with no vault
                      entry, falls back to the owner's existing configuration (config.json / secrets)
                      for brokers ATIP already connected to (dhan, zerodha) -- labelled source "config".
    verify(conn, credential_id)
                      a READ-ONLY call (profile / funds) through brokers/ with the credential; result in
                      last_verified_at / verify_status / verify_detail. Never places, modifies or cancels.
    expiring(conn, hours)  credentials expiring soon (the scheduler alerts the owner once a day)
    Key rotation (ops/crypto.rotate_key) now re-encrypts these entries too.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta

from enterprise import audit

BROKERS = {"dhan": ("client_id", "access_token"), "zerodha": ("api_key", "api_secret", "access_token"),
           "upstox": ("api_key", "api_secret", "access_token"), "angel": ("client_id", "api_key", "pin", "totp_secret"),
           "other": ()}
# access tokens of these brokers are valid for one trading day (re-login daily)
EXPIRY_HOURS = {"dhan": 24, "zerodha": 24, "upstox": 24}


def _ctx(tenant, user, broker, label):
    return f"vault:{tenant}:{user}:{broker}:{label}"


def store(conn, tenant, user, broker, fields: dict, label="default", actor=None, expires_at=None) -> dict:
    from ops.crypto import available, encrypt, key_id
    broker = (broker or "").lower()
    if broker not in BROKERS:
        raise ValueError(f"unknown broker {broker}; known {sorted(BROKERS)}")
    if not re.match(r"^[A-Za-z0-9 _.-]{1,40}$", label or ""):
        raise ValueError("label: 1-40 chars [A-Za-z0-9 _.-]")
    if not isinstance(fields, dict) or not fields or not all(isinstance(v, str) and v for v in fields.values()):
        raise ValueError("fields must be a non-empty object of non-empty strings")
    need = set(BROKERS[broker]) - set(fields)
    if need:
        raise ValueError(f"missing fields for {broker}: {sorted(need)}")
    if not available():
        raise ValueError("the vault needs an encryption key: python -m ops keygen (owner action), then restart")
    enc = encrypt(json.dumps(fields), _ctx(tenant, user, broker, label))
    now = datetime.now()
    if expires_at is None and broker in EXPIRY_HOURS:
        expires_at = now + timedelta(hours=EXPIRY_HOURS[broker])
    elif expires_at is not None and not isinstance(expires_at, datetime):
        expires_at = datetime.fromisoformat(str(expires_at)[:19].replace(" ", "T"))
    prev = conn.execute("SELECT credential_id FROM enterprise_vault_credential WHERE tenant_id=? AND user_id=? AND "
                        "broker=? AND label=?", (tenant, user, broker, label)).fetchone()
    if prev:
        cid = prev[0]
        conn.execute("UPDATE enterprise_vault_credential SET secret_enc=?, field_names_json=?, key_id=?, status='ACTIVE', "
                     "updated_at=?, rotated_at=?, expires_at=?, verify_status=NULL, verify_detail=NULL WHERE "
                     "credential_id=?", (enc, json.dumps(sorted(fields)), key_id(), now, now, expires_at, cid))
        action = "vault.rotated"
    else:
        cid = "vc_" + uuid.uuid4().hex[:16]
        conn.execute("INSERT INTO enterprise_vault_credential (credential_id,tenant_id,user_id,broker,label,secret_enc,"
                     "field_names_json,key_id,status,created_at,updated_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (cid, tenant, user, broker, label, enc, json.dumps(sorted(fields)), key_id(), "ACTIVE", now, now,
                      expires_at))
        action = "vault.stored"
    audit.record(conn, action, tenant_id=tenant, user_id=user, actor=actor or user, resource=cid,
                 details={"broker": broker, "label": label, "fields": sorted(fields)}, commit=False)
    conn.commit()
    return {"credential_id": cid, "broker": broker, "label": label, "fields": sorted(fields), "status": "ACTIVE",
            "expires_at": str(expires_at)[:19] if expires_at else None}


def list_for(conn, tenant, user=None) -> list:
    q = ("SELECT credential_id, tenant_id, user_id, broker, label, field_names_json, key_id, status, created_at, "
         "updated_at, rotated_at, last_accessed_at, access_count, expires_at, last_verified_at, verify_status, "
         "verify_detail FROM enterprise_vault_credential WHERE tenant_id=?")
    args = [tenant]
    if user:
        q += " AND user_id=?"
        args.append(user)
    out = []
    for r in conn.execute(q + " ORDER BY broker, label", args):
        d = dict(r)
        d["fields"] = json.loads(d.pop("field_names_json") or "[]")
        d["expired"] = _expired(d.get("expires_at"))
        out.append(d)
    return out


def revoke(conn, tenant, user, credential_id, actor=None) -> dict:
    cur = conn.execute("UPDATE enterprise_vault_credential SET status='REVOKED', secret_enc='', updated_at=? WHERE "
                       "credential_id=? AND tenant_id=? AND user_id=?", (datetime.now(), credential_id, tenant, user))
    if not cur.rowcount:
        raise ValueError(f"no credential {credential_id}")
    audit.record(conn, "vault.revoked", tenant_id=tenant, user_id=user, actor=actor or user, resource=credential_id,
                 commit=False)
    conn.commit()
    return {"credential_id": credential_id, "status": "REVOKED"}


def _reveal(conn, credential_id, purpose, actor="system") -> dict:
    """Server-side only; audited. Not called anywhere in W9 (no broker connector uses the vault yet)."""
    from ops.crypto import decrypt
    r = conn.execute("SELECT tenant_id, user_id, broker, label, secret_enc, status FROM enterprise_vault_credential "
                     "WHERE credential_id=?", (credential_id,)).fetchone()
    if not r or r[5] != "ACTIVE":
        raise ValueError("credential not active")
    fields = json.loads(decrypt(r[4], _ctx(r[0], r[1], r[2], r[3])))
    conn.execute("UPDATE enterprise_vault_credential SET last_accessed_at=?, access_count=access_count+1 WHERE "
                 "credential_id=?", (datetime.now(), credential_id))
    audit.record(conn, "vault.accessed", tenant_id=r[0], user_id=r[1], actor=actor, resource=credential_id,
                 details={"purpose": purpose}, commit=False)
    conn.commit()
    return fields


# ── W37 (ENT-06) completion ────────────────────────────────────────────
def _expired(v) -> bool:
    if not v:
        return False
    t = v if isinstance(v, datetime) else datetime.fromisoformat(str(v)[:19].replace(" ", "T"))
    return t <= datetime.now()


def _owner_config(broker: str) -> dict | None:
    """The owner's existing broker configuration (default tenant only)."""
    from ops.secrets import get
    if broker == "dhan":
        cid, tok = get("DHAN_CLIENT_ID", log_access=True), get("DHAN_ACCESS_TOKEN", log_access=True)
        return {"client_id": cid, "access_token": tok} if cid and tok else None
    if broker == "zerodha":
        try:
            from portfolio.zerodha import TOKEN_PATH, load_cfg
            key = (load_cfg() or {}).get("kite_api_key")
            tok = json.loads(TOKEN_PATH.read_text()).get("access_token") if TOKEN_PATH.exists() else None
        except Exception:
            key = tok = None
        return {"api_key": key, "access_token": tok} if key and tok else None
    return None


def credentials_for(conn, tenant, user, broker, purpose: str, label: str | None = None) -> dict:
    """{"source": "vault" | "config", "credential_id", "fields"} for a broker connector; raises LookupError
    when there is no usable credential (none, revoked or expired)."""
    from execution.tenant_books import is_default
    broker = (broker or "").lower()
    q = ("SELECT credential_id, expires_at FROM enterprise_vault_credential WHERE tenant_id=? AND user_id=? AND broker=? "
         "AND status='ACTIVE'")
    args = [tenant, user, broker]
    if label:
        q += " AND label=?"
        args.append(label)
    rows = conn.execute(q + " ORDER BY updated_at DESC", args).fetchall()
    live = [r for r in rows if not _expired(r[1])]
    if live:
        return {"source": "vault", "credential_id": live[0][0], "fields": _reveal(conn, live[0][0], purpose)}
    if rows:
        raise LookupError(f"the {broker} credential expired at {str(rows[0][1])[:16]}: store a fresh token")
    if is_default(tenant):
        f = _owner_config(broker)
        if f:
            return {"source": "config", "credential_id": None, "fields": f}
    raise LookupError(f"no {broker} credential for this user")


def verify(conn, credential_id, actor="owner") -> dict:
    """Read-only check that a stored credential works (broker profile / funds)."""
    from brokers.registry import connector
    r = conn.execute("SELECT tenant_id, user_id, broker, expires_at FROM enterprise_vault_credential WHERE "
                     "credential_id=?", (credential_id,)).fetchone()
    if not r:
        raise LookupError(f"no credential {credential_id}")
    if _expired(r[3]):
        status, detail = "EXPIRED", f"expired at {str(r[3])[:16]}"
    else:
        try:
            fields = _reveal(conn, credential_id, "verify (read-only profile call)", actor)
            prof = connector(r[2], fields).profile()
            status, detail = "OK", f"connected as {prof.get('name') or prof.get('client_id') or 'account'}"
        except Exception as e:
            status, detail = "FAILED", f"{type(e).__name__}: {str(e)[:200]}"
    conn.execute("UPDATE enterprise_vault_credential SET last_verified_at=?, verify_status=?, verify_detail=? WHERE "
                 "credential_id=?", (datetime.now(), status, detail, credential_id))
    audit.record(conn, "vault.verified", tenant_id=r[0], user_id=r[1], actor=actor, resource=credential_id,
                 details={"status": status}, commit=False)
    conn.commit()
    return {"credential_id": credential_id, "verify_status": status, "detail": detail}


def expiring(conn, hours: int = 12) -> list:
    soon = datetime.now() + timedelta(hours=int(hours))
    out = []
    for r in conn.execute("SELECT credential_id, tenant_id, user_id, broker, label, expires_at FROM "
                          "enterprise_vault_credential WHERE status='ACTIVE' AND expires_at IS NOT NULL"):
        t = r[5] if isinstance(r[5], datetime) else datetime.fromisoformat(str(r[5])[:19].replace(" ", "T"))
        if t <= soon:
            out.append({"credential_id": r[0], "tenant_id": r[1], "user_id": r[2], "broker": r[3], "label": r[4],
                        "expires_at": str(t)[:16], "expired": t <= datetime.now()})
    return out
