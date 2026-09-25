"""
Enterprise audit trail (enterprise_audit): who did what, where, with what result.

Written by the authz middleware for every mutating request and every denial,
and by the enterprise services for account / tenant / role / key / billing
events. Rows are append-only; nothing in ATIP updates or deletes them.
Secrets (passwords, tokens, keys) are never written into details.

W8 tamper evidence: triggers (ops/migrations.py 0004) reject UPDATE / DELETE, and
each row carries row_hash = sha256(prev_hash | the row's fields), prev_hash being
the previous row's row_hash -- a hash chain. verify_chain() recomputes it; an edited,
deleted or inserted-out-of-band row breaks the chain from that point. Rows written
before W8 have no hash (the chain starts at the first W8 row).
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime

_CHAIN_LOCK = threading.Lock()
_FIELDS = ("at", "tenant_id", "user_id", "actor", "action", "resource", "method", "path", "status_code", "ip",
           "details_json")

_SECRET_KEYS = {"password", "new_password", "old_password", "token", "api_key", "secret", "reset_token",
                "refresh_token", "otp", "code"}


def _clean(d):
    if isinstance(d, dict):
        return {k: ("***" if k in _SECRET_KEYS else _clean(v)) for k, v in d.items()}
    return d


def record(conn, action, *, tenant_id=None, user_id=None, actor=None, resource=None, method=None, path=None,
           status_code=None, ip=None, details=None, commit=True):
    vals = (datetime.now(), tenant_id, user_id, actor, action, resource, method, path, status_code, ip,
            json.dumps(_clean(details or {}), default=str)[:4000])
    with _CHAIN_LOCK:
        try:
            r = conn.execute("SELECT row_hash FROM enterprise_audit WHERE row_hash IS NOT NULL ORDER BY id DESC "
                             "LIMIT 1").fetchone()
            prev = r[0] if r else "GENESIS"
            conn.execute("INSERT INTO enterprise_audit (at,tenant_id,user_id,actor,action,resource,method,path,"
                         "status_code,ip,details_json,prev_hash,row_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         vals + (prev, row_hash(prev, vals)))
        except Exception as e:
            if "prev_hash" not in str(e) and "row_hash" not in str(e):
                raise
            conn.execute("INSERT INTO enterprise_audit (at,tenant_id,user_id,actor,action,resource,method,path,"
                         "status_code,ip,details_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)", vals)
        if commit:
            conn.commit()


def row_hash(prev, vals) -> str:
    return hashlib.sha256((str(prev) + "|" + "|".join("" if v is None else str(v) for v in vals)).encode()
                          ).hexdigest()


def verify_chain(conn, limit=None) -> dict:
    """Recompute the hash chain. {"ok", "checked", "first_break": id | None, "unhashed": n}."""
    q = "SELECT id, " + ",".join(_FIELDS) + ", prev_hash, row_hash FROM enterprise_audit ORDER BY id"
    prev, checked, unhashed = None, 0, 0
    conn2 = conn
    old = conn2.row_factory
    conn2.row_factory = None                  # raw values, exactly as stored
    try:
        for row in conn2.execute(q + (f" LIMIT {int(limit)}" if limit else "")):
            rid, vals, ph, rh = row[0], row[1:12], row[12], row[13]
            if rh is None:
                unhashed += 1
                continue
            if prev is not None and ph != prev:
                return {"ok": False, "checked": checked, "first_break": rid, "unhashed": unhashed,
                        "reason": "prev_hash does not match the previous row"}
            if rh != row_hash(ph, vals):
                return {"ok": False, "checked": checked, "first_break": rid, "unhashed": unhashed,
                        "reason": "row content changed"}
            prev, checked = rh, checked + 1
    finally:
        conn2.row_factory = old
    return {"ok": True, "checked": checked, "first_break": None, "unhashed": unhashed}


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
