"""
Enterprise audit trail (enterprise_audit): who did what, where, with what result.

Written by the authz middleware for every mutating request and every denial,
and by the enterprise services for account / tenant / role / key / billing
events. Rows are append-only; nothing in ATIP updates or deletes them.
Secrets (passwords, tokens, keys) are never written into details.
"""

from __future__ import annotations

import json
from datetime import datetime

_SECRET_KEYS = {"password", "new_password", "old_password", "token", "api_key", "secret", "reset_token"}


def _clean(d):
    if isinstance(d, dict):
        return {k: ("***" if k in _SECRET_KEYS else _clean(v)) for k, v in d.items()}
    return d


def record(conn, action, *, tenant_id=None, user_id=None, actor=None, resource=None, method=None, path=None,
           status_code=None, ip=None, details=None, commit=True):
    conn.execute("INSERT INTO enterprise_audit (at,tenant_id,user_id,actor,action,resource,method,path,status_code,ip,"
                 "details_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (datetime.now(), tenant_id, user_id, actor, action, resource, method, path, status_code, ip,
                  json.dumps(_clean(details or {}), default=str)[:4000]))
    if commit:
        conn.commit()


def query(conn, tenant_id=None, user_id=None, action=None, limit=200) -> list:
    q, args = "SELECT * FROM enterprise_audit WHERE 1=1", []
    for col, v in (("tenant_id", tenant_id), ("user_id", user_id), ("action", action)):
        if v:
            q += f" AND {col}=?"; args.append(v)
    out = []
    for r in conn.execute(q + " ORDER BY id DESC LIMIT ?", args + [int(limit)]):
        d = dict(r)
        d["details"] = json.loads(d.pop("details_json") or "{}")
        out.append(d)
    return out
