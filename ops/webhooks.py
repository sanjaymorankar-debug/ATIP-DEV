"""
Webhook security: inbound verification and outbound signed delivery.

INBOUND  POST /api/webhooks/{source}
  Headers  X-ATIP-Timestamp: <unix seconds>
           X-ATIP-Event-Id:  <unique id from the sender>
           X-ATIP-Signature: sha256=<hex HMAC-SHA256(secret, f"{timestamp}.{raw body}")>
  Secret   WEBHOOK_SECRET_<SOURCE> (ops/secrets.py: env / .env / atip_data/secrets), falling
           back to WEBHOOK_SECRET_DEFAULT. No secret configured -> 503 (the endpoint is off).
  Checks   signature (constant-time), timestamp within 300 s (replay window), event id not
           seen before for this source (ops_webhook_event PRIMARY KEY -> duplicate = 200
           {"duplicate": true}, processed once), body <= 256 KB, JSON.
  Effect   the event is recorded (payload sha256, type) as RECEIVED. ATIP has no inbound
           integration that ACTS on a webhook yet (billing / broker postbacks are future
           work): the endpoint is the verified, idempotent intake those will use.

OUTBOUND endpoints (ops_webhook_endpoint, registered via /api/ops/webhooks) receive
  ATIP events (emit(event_type, payload)). Each delivery (ops_webhook_delivery) is signed
  the same way with the endpoint's secret, retried with exponential backoff (1, 2, 4, 8,
  16 min ... up to 8 attempts), then DEAD (dead-letter; visible in /api/ops/webhooks and
  re-queueable). A circuit breaker per host stops hammering a failing receiver. Delivery
  runs in the scheduler job ops_webhook_dispatch (every 5 min). UNIQUE(endpoint, event id)
  makes emit() idempotent. Only https:// URLs are accepted outside development.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlparse

TOLERANCE_S = 300
MAX_BODY = 256 * 1024
MAX_ATTEMPTS = 8


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def _secret_for(source: str):
    from ops.secrets import CATALOG, get
    name = f"WEBHOOK_SECRET_{source.upper()}"
    if name in CATALOG:
        v = get(name)
    else:
        import os
        v = os.environ.get(name)
        if not v:
            from ops.secrets import SECRETS_DIR
            p = SECRETS_DIR / name
            v = p.read_text(encoding="utf-8").strip() if p.exists() else None
    return v or get("WEBHOOK_SECRET_DEFAULT")


def verify_inbound(conn, source: str, headers: dict, body: bytes) -> tuple:
    """(http_status, payload). Records the event when accepted."""
    import re
    if not re.match(r"^[a-z0-9_]{1,32}$", source or ""):
        return 404, {"error": {"code": "NOT_FOUND", "message": "unknown webhook source"}}
    secret = _secret_for(source)
    if not secret:
        return 503, {"error": {"code": "DEPENDENCY_UNAVAILABLE", "message": f"webhook source {source} not configured"}}
    if len(body) > MAX_BODY:
        return 413, {"error": {"code": "PAYLOAD_TOO_LARGE", "message": "webhook body too large"}}
    ts, eid, sig = headers.get("x-atip-timestamp"), headers.get("x-atip-event-id"), headers.get("x-atip-signature")
    if not ts or not eid or not sig or len(eid) > 128:
        return 400, {"error": {"code": "VALIDATION_FAILED", "message": "missing webhook headers"}}
    if not hmac.compare_digest(sign(secret, ts, body), sig):
        _record(conn, source, eid, False, "REJECTED", body, None, "bad signature")
        return 401, {"error": {"code": "UNAUTHENTICATED", "message": "invalid signature"}}
    try:
        age = abs(time.time() - int(ts))
    except ValueError:
        return 400, {"error": {"code": "VALIDATION_FAILED", "message": "bad timestamp"}}
    if age > TOLERANCE_S:
        return 401, {"error": {"code": "UNAUTHENTICATED", "message": "timestamp outside the replay window"}}
    try:
        payload = json.loads(body or b"{}")
    except Exception:
        return 400, {"error": {"code": "VALIDATION_FAILED", "message": "body is not JSON"}}
    if conn.execute("SELECT 1 FROM ops_webhook_event WHERE source=? AND event_id=?", (source, eid)).fetchone():
        return 200, {"received": True, "duplicate": True, "event_id": eid}
    etype = payload.get("type") if isinstance(payload, dict) else None
    _record(conn, source, eid, True, "RECEIVED", body, etype, None)
    return 200, {"received": True, "duplicate": False, "event_id": eid}


def _record(conn, source, eid, sig_ok, status, body, etype, error):
    try:
        conn.execute("INSERT INTO ops_webhook_event (source,event_id,received_at,signature_ok,status,payload_sha256,"
                     "event_type,error) VALUES (?,?,?,?,?,?,?,?)",
                     (source, eid if sig_ok else f"rejected:{uuid.uuid4().hex[:12]}", datetime.now(), int(sig_ok),
                      status, hashlib.sha256(body).hexdigest(), etype, error))
        conn.commit()
    except Exception:
        conn.rollback()


# -- outbound ---------------------------------------------------------------------------

def add_endpoint(conn, url, events, secret_name, tenant_id="default", actor="owner") -> dict:
    from ops.config import environment
    u = urlparse(url)
    if u.scheme not in ("https", "http") or not u.netloc:
        raise ValueError("url must be http(s)://host/...")
    if u.scheme != "https" and environment() != "development":
        raise ValueError("outbound webhooks require https outside development")
    if not str(secret_name).startswith("WEBHOOK_SECRET_"):
        raise ValueError("secret_name must name a WEBHOOK_SECRET_* secret (the value is never stored here)")
    eid = "whe_" + uuid.uuid4().hex[:12]
    conn.execute("INSERT INTO ops_webhook_endpoint (endpoint_id,tenant_id,url,events_json,secret_name,status,"
                 "created_at,created_by) VALUES (?,?,?,?,?,?,?,?)",
                 (eid, tenant_id, url, json.dumps(list(events or ["*"])), secret_name, "ACTIVE", datetime.now(), actor))
    conn.commit()
    return {"endpoint_id": eid}


def emit(conn, event_type, payload, event_id=None) -> int:
    """Queue an event for every ACTIVE endpoint subscribed to it. Idempotent per event id."""
    event_id = event_id or uuid.uuid4().hex
    n = 0
    for ep in conn.execute("SELECT endpoint_id, events_json FROM ops_webhook_endpoint WHERE status='ACTIVE'"):
        evs = json.loads(ep[1] or '["*"]')
        if "*" in evs or event_type in evs:
            cur = conn.execute("INSERT OR IGNORE INTO ops_webhook_delivery (delivery_id,endpoint_id,event_type,event_id,"
                               "payload_json,status,attempts,next_attempt_at,created_at) VALUES (?,?,?,?,?,?,0,?,?)",
                               ("whd_" + uuid.uuid4().hex[:16], ep[0], event_type, event_id,
                                json.dumps(payload, default=str), "PENDING", datetime.now(), datetime.now()))
            n += cur.rowcount
    conn.commit()
    return n


def _post(url, body, headers, timeout=10):
    import urllib.request
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:        # noqa: S310 (scheme validated on registration)
        return r.status


def due_count() -> int:
    """Cheap check the scheduler makes before running the dispatch job."""
    from db.schema import get_connection
    c = get_connection()
    try:
        return c.execute("SELECT COUNT(*) FROM ops_webhook_delivery WHERE status IN ('PENDING','RETRY') AND "
                         "next_attempt_at <= ?", (datetime.now(),)).fetchone()[0]
    except Exception:
        return 0
    finally:
        c.close()


def dispatch_due(limit=50) -> dict:
    """Scheduler job: deliver PENDING / RETRY deliveries that are due."""
    from db.schema import get_connection
    from ops import metrics
    from ops.errors import DependencyUnavailable
    from ops.resilience import breaker
    from ops.secrets import get
    c = get_connection()
    sent = failed = 0
    try:
        rows = c.execute("SELECT d.delivery_id, d.event_type, d.event_id, d.payload_json, d.attempts, e.url, "
                         "e.secret_name FROM ops_webhook_delivery d JOIN ops_webhook_endpoint e USING (endpoint_id) "
                         "WHERE d.status IN ('PENDING','RETRY') AND d.next_attempt_at <= ? AND e.status='ACTIVE' "
                         "ORDER BY d.next_attempt_at LIMIT ?", (datetime.now(), int(limit))).fetchall()
        for did, etype, evid, pj, attempts, url, sname in rows:
            secret = get(sname) if sname else None
            if not secret:
                c.execute("UPDATE ops_webhook_delivery SET status='DEAD', last_error=? WHERE delivery_id=?",
                          (f"secret {sname} not configured", did))
                failed += 1
                continue
            body = json.dumps({"id": evid, "type": etype, "data": json.loads(pj or "{}")}).encode()
            ts = str(int(time.time()))
            headers = {"Content-Type": "application/json", "User-Agent": "ATIP-Webhooks/1",
                       "X-ATIP-Timestamp": ts, "X-ATIP-Event-Id": evid, "X-ATIP-Signature": sign(secret, ts, body)}
            try:
                code = breaker(f"webhook:{urlparse(url).netloc}").call(_post, url, body, headers)
                ok, err = 200 <= code < 300, None if 200 <= code < 300 else f"HTTP {code}"
            except DependencyUnavailable as e:
                code, ok, err = None, False, str(e)
            except Exception as e:
                code, ok, err = getattr(e, "code", None), False, f"{type(e).__name__}: {str(e)[:200]}"
            attempts += 1
            if ok:
                c.execute("UPDATE ops_webhook_delivery SET status='DELIVERED', attempts=?, last_status_code=?, "
                          "delivered_at=?, last_error=NULL WHERE delivery_id=?", (attempts, code, datetime.now(), did))
                sent += 1
                metrics.inc("atip_webhook_deliveries_total", {"status": "DELIVERED"})
            else:
                dead = attempts >= MAX_ATTEMPTS or (isinstance(code, int) and 400 <= code < 500 and code != 429)
                c.execute("UPDATE ops_webhook_delivery SET status=?, attempts=?, last_status_code=?, last_error=?, "
                          "next_attempt_at=? WHERE delivery_id=?",
                          ("DEAD" if dead else "RETRY", attempts, code, err,
                           datetime.now() + timedelta(minutes=2 ** (attempts - 1)), did))
                failed += 1
                metrics.inc("atip_webhook_deliveries_total", {"status": "DEAD" if dead else "RETRY"})
            c.commit()
    finally:
        c.close()
    return {"status": "SUCCESS", "rows": sent, "failed": failed, "due": sent + failed}


def requeue(conn, delivery_id) -> bool:
    cur = conn.execute("UPDATE ops_webhook_delivery SET status='PENDING', attempts=0, next_attempt_at=? WHERE "
                       "delivery_id=? AND status='DEAD'", (datetime.now(), delivery_id))
    conn.commit()
    return cur.rowcount > 0


def overview(conn) -> dict:
    return {
        "endpoints": [dict(r) for r in conn.execute("SELECT endpoint_id, tenant_id, url, events_json, secret_name, "
                                                   "status, created_at FROM ops_webhook_endpoint")],
        "deliveries": [dict(r) for r in conn.execute("SELECT status, COUNT(*) AS n FROM ops_webhook_delivery GROUP BY 1")],
        "dead": [dict(r) for r in conn.execute("SELECT delivery_id, endpoint_id, event_type, attempts, last_error "
                                              "FROM ops_webhook_delivery WHERE status='DEAD' ORDER BY created_at DESC "
                                              "LIMIT 50")],
        "inbound": [dict(r) for r in conn.execute("SELECT source, status, COUNT(*) AS n, MAX(received_at) AS last FROM "
                                                 "ops_webhook_event GROUP BY 1, 2")],
    }
