"""
Per-user API keys for programmatic access (header "Authorization: ApiKey <key>").

Key format: atk_<8-char id>_<secret>. Only the SHA-256 digest is stored; the key
is shown once at creation. A key has scopes (a subset of the permissions the
user holds in the key's tenant -- effective permissions are the intersection,
re-evaluated on every request), an optional expiry, last-used time, and can be
revoked. Counted against the tenant limit max_api_keys.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta

from enterprise import audit
from enterprise import security as S


def create(conn, user_id, tenant_id, name, scopes, days=90) -> dict:
    from enterprise import rbac, tenants
    from enterprise.users import memberships
    roles = memberships(conn, user_id).get(tenant_id)
    if not roles:
        raise ValueError(f"user is not a member of {tenant_id}")
    held = rbac.permissions_for(conn, roles)
    bad = set(scopes or []) - held
    if not scopes or bad:
        raise ValueError(f"scopes must be a non-empty subset of the user's permissions; not held: {sorted(bad)}")
    tenants.check_limit(conn, tenant_id, "api_keys")
    kid = secrets.token_hex(4)
    key = f"atk_{kid}_{secrets.token_urlsafe(32)}"
    exp = datetime.now() + timedelta(days=int(days)) if days else None
    conn.execute("INSERT INTO enterprise_api_key (key_id,user_id,tenant_id,name,key_hash,scopes_json,created_at,"
                 "expires_at) VALUES (?,?,?,?,?,?,?,?)", (kid, user_id, tenant_id, name, S.digest(key),
                                                          json.dumps(sorted(scopes)), datetime.now(), exp))
    audit.record(conn, "apikey.create", tenant_id=tenant_id, user_id=user_id, actor=user_id, resource=kid,
                 details={"scopes": scopes, "expires_at": exp}, commit=False)
    conn.commit()
    return {"key_id": kid, "api_key": key, "expires_at": exp.isoformat() if exp else None, "scopes": sorted(scopes),
            "note": "store it now; ATIP keeps only a digest"}


def list_keys(conn, user_id) -> list:
    out = []
    for r in conn.execute("SELECT key_id, tenant_id, name, scopes_json, created_at, expires_at, last_used_at, revoked_at "
                          "FROM enterprise_api_key WHERE user_id=? ORDER BY created_at DESC", (user_id,)):
        d = dict(r)
        d["scopes"] = json.loads(d.pop("scopes_json") or "[]")
        out.append(d)
    return out


def revoke(conn, user_id, key_id, actor) -> None:
    n = conn.execute("UPDATE enterprise_api_key SET revoked_at=? WHERE key_id=? AND user_id=? AND revoked_at IS NULL",
                     (datetime.now(), key_id, user_id)).rowcount
    if not n:
        raise ValueError(f"no active key {key_id}")
    audit.record(conn, "apikey.revoke", user_id=user_id, actor=actor, resource=key_id, commit=False)
    conn.commit()


def principal(conn, key) -> dict | None:
    if not key or not key.startswith("atk_"):
        return None
    r = conn.execute("SELECT key_id, user_id, tenant_id, scopes_json, expires_at FROM enterprise_api_key "
                     "WHERE key_hash=? AND revoked_at IS NULL", (S.digest(key),)).fetchone()
    if not r or (r[4] and str(r[4]) < str(datetime.now())):
        return None
    conn.execute("UPDATE enterprise_api_key SET last_used_at=? WHERE key_id=?", (datetime.now(), r[0]))
    conn.commit()
    return {"user_id": r[1], "tenant_id": r[2], "via": "api_key", "key_id": r[0], "scopes": set(json.loads(r[3]))}
