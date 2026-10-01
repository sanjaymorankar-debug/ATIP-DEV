"""
Per-user broker credential vault -- FOUNDATION ONLY (W9, ENT-06).

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
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime

from enterprise import audit

BROKERS = {"dhan": ("client_id", "access_token"), "zerodha": ("api_key", "api_secret", "access_token"),
           "upstox": ("api_key", "api_secret", "access_token"), "angel": ("client_id", "api_key", "pin", "totp_secret"),
           "other": ()}


def _ctx(tenant, user, broker, label):
    return f"vault:{tenant}:{user}:{broker}:{label}"


def store(conn, tenant, user, broker, fields: dict, label="default", actor=None) -> dict:
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
    prev = conn.execute("SELECT credential_id FROM enterprise_vault_credential WHERE tenant_id=? AND user_id=? AND "
                        "broker=? AND label=?", (tenant, user, broker, label)).fetchone()
    if prev:
        cid = prev[0]
        conn.execute("UPDATE enterprise_vault_credential SET secret_enc=?, field_names_json=?, key_id=?, status='ACTIVE', "
                     "updated_at=?, rotated_at=? WHERE credential_id=?",
                     (enc, json.dumps(sorted(fields)), key_id(), now, now, cid))
        action = "vault.rotated"
    else:
        cid = "vc_" + uuid.uuid4().hex[:16]
        conn.execute("INSERT INTO enterprise_vault_credential (credential_id,tenant_id,user_id,broker,label,secret_enc,"
                     "field_names_json,key_id,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (cid, tenant, user, broker, label, enc, json.dumps(sorted(fields)), key_id(), "ACTIVE", now, now))
        action = "vault.stored"
    audit.record(conn, action, tenant_id=tenant, user_id=user, actor=actor or user, resource=cid,
                 details={"broker": broker, "label": label, "fields": sorted(fields)}, commit=False)
    conn.commit()
    return {"credential_id": cid, "broker": broker, "label": label, "fields": sorted(fields), "status": "ACTIVE"}


def list_for(conn, tenant, user=None) -> list:
    q = ("SELECT credential_id, tenant_id, user_id, broker, label, field_names_json, key_id, status, created_at, "
         "updated_at, rotated_at, last_accessed_at, access_count FROM enterprise_vault_credential WHERE tenant_id=?")
    args = [tenant]
    if user:
        q += " AND user_id=?"
        args.append(user)
    out = []
    for r in conn.execute(q + " ORDER BY broker, label", args):
        d = dict(r)
        d["fields"] = json.loads(d.pop("field_names_json") or "[]")
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
