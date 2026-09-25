"""
Idempotency for mutating API requests.

A client sends `Idempotency-Key: <unique string>` on POST / PUT / PATCH / DELETE.
The HTTP middleware (ops/http.py):
  * first request: claims (key, caller) with status IN_PROGRESS, runs it; a 2xx
    response (status + body <= 256 KB) is stored as COMPLETED, any other response
    releases the key (the action did not complete, so a retry runs again)
  * repeat with the same key, same caller, same method + path + body hash:
    the stored response is returned (header Idempotent-Replay: true) -- the order,
    webhook or billing action is NOT executed again
  * same key with a different request: 409 CONFLICT
  * same key while the first is still running: 409 CONFLICT (retry later)
The caller identity is a digest of the credentials used (session cookie /
Authorization / X-ATIP-Token), so keys are scoped per caller. Entries expire after
ops.idempotency_ttl_hours (24).

ops.require_idempotency_for_orders (default false): when true, order-creating /
submitting / cancelling routes (/api/oms/*, /api/orders*) reject a request without
the header (400). The /trading page always sends one.

Structural protection exists regardless: W4 allows one order per intent and per
risk decision (UNIQUE), webhooks dedupe on event id (ops/webhooks.py).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

ORDER_ROUTES = r"^/api/(oms/orders|orders)"


def request_hash(method, path, body: bytes) -> str:
    return hashlib.sha256(method.encode() + b"|" + path.encode() + b"|" + (body or b"")).hexdigest()


def claim(conn, key, caller, rhash, ttl_hours) -> dict | None:
    """None if claimed now; otherwise the existing row."""
    conn.execute("DELETE FROM ops_idempotency WHERE expires_at < ?", (datetime.now(),))
    r = conn.execute("SELECT status, request_hash, response_status, response_body, content_type FROM ops_idempotency "
                     "WHERE idem_key=? AND caller=?", (key, caller)).fetchone()
    if r:
        conn.commit()
        return dict(r)
    conn.execute("INSERT INTO ops_idempotency (idem_key,caller,request_hash,status,created_at,expires_at) VALUES "
                 "(?,?,?,?,?,?)", (key, caller, rhash, "IN_PROGRESS", datetime.now(),
                                   datetime.now() + timedelta(hours=ttl_hours)))
    conn.commit()
    return None


def complete(conn, key, caller, status, body: bytes, content_type):
    conn.execute("UPDATE ops_idempotency SET status='COMPLETED', response_status=?, response_body=?, content_type=? "
                 "WHERE idem_key=? AND caller=?", (status, body[:262144], content_type, key, caller))
    conn.commit()


def release(conn, key, caller):
    conn.execute("DELETE FROM ops_idempotency WHERE idem_key=? AND caller=? AND status='IN_PROGRESS'", (key, caller))
    conn.commit()
