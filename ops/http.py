"""
The operations HTTP layer: a pure ASGI middleware, OUTERMOST in the stack (it
runs before the W7 enterprise authorization middleware and the routes).

Per request:
  1. request id: X-Request-ID (validated, <= 64 chars [A-Za-z0-9._-]) or a new one;
     X-Correlation-ID propagated (default = request id); both returned as headers and
     carried into logs (ops/context.py)
  2. API versioning: /api/v1/<x> is served by /api/<x> (the current contract = v1);
     responses carry API-Version: v1. Unversioned /api stays for the existing pages.
     W39 (API-03): a v1 resource listed in enterprise/public_api.DEPRECATIONS gets
     Deprecation / Sunset / Link headers, and 410 GONE once past its sunset date.
  3. request size: bodies over ops.max_request_bytes (2 MB) -> 413 (also chunked bodies)
  4. JSON validation: a non-empty body on POST/PUT/PATCH/DELETE under /api must be
     application/json and parse -> else 415 / 400
  5. rate limiting (ops.rate_limit_enabled, default off): per client (IP + credential
     digest) token bucket of ops.rate_limit_per_minute -> 429 + Retry-After
  6. idempotency (ops/idempotency.py): Idempotency-Key replay / conflict; optionally
     required on order routes
  7. security headers on every response: X-Content-Type-Options nosniff,
     X-Frame-Options DENY, Referrer-Policy no-referrer, Cross-Origin-Opener-Policy
     same-origin, Permissions-Policy (camera, microphone, geolocation off);
     Cache-Control no-store on /api/auth, /api/account, /api/admin; HSTS only when
     ops.tls_enabled; Content-Security-Policy only when ops.csp is set (the existing
     pages use inline scripts, so no default CSP is imposed)
  8. v1 list responses: ?page=&page_size= (default 50, max 500), ?sort=field|-field,
     ?filter[field]=value -> X-Total-Count / X-Page / X-Page-Size
  9. metrics: request count + latency by route template and status
 10. audit: while the enterprise layer is OFF, every mutating /api request is written
     to enterprise_audit (actor "local-token" / "anonymous", path, status) -- when it
     is ON, the enterprise middleware audits instead
CORS: a CORSMiddleware is added only when ops.cors_origins lists origins (none by
default = same-origin only).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from urllib.parse import parse_qs

from ops import context, metrics
from ops.errors import envelope

_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MUTATING = ("POST", "PUT", "PATCH", "DELETE")
_CFG = {"at": 0.0, "v": None}


def _cfg():
    if time.monotonic() - _CFG["at"] > 5 or _CFG["v"] is None:
        from ops.config import ops
        try:
            _CFG["v"] = ops()
        except Exception:
            from ops.config import OPS_DEFAULTS
            _CFG["v"] = dict(OPS_DEFAULTS)
        _CFG["at"] = time.monotonic()
    return _CFG["v"]


def _security_headers(path, cfg):
    h = [(b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"),
         (b"referrer-policy", b"no-referrer"), (b"cross-origin-opener-policy", b"same-origin"),
         (b"permissions-policy", b"camera=(), microphone=(), geolocation=()")]
    if re.match(r"^/api/(auth|account|admin)", path):
        h.append((b"cache-control", b"no-store"))
    if cfg.get("tls_enabled"):
        h.append((b"strict-transport-security", f"max-age={int(cfg.get('hsts_seconds', 31536000))}".encode()))
    if cfg.get("csp"):
        h.append((b"content-security-policy", str(cfg["csp"]).encode()))
    return h


def _caller(headers) -> str:
    raw = (headers.get("authorization") or "") + "|" + (headers.get("x-atip-token") or "") + "|" + \
          (re.search(r"atip_session=([^;]+)", headers.get("cookie") or "") or [None, ""])[1]
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _rate_ok(key, per_minute) -> tuple:
    """W9: the bucket lives in the shared-state store (in-process by default, Redis-ready)."""
    from ops.shared_state import store
    return store().bucket(f"http:{key}", per_minute)


def _paginate(data, qs):
    if not isinstance(data, list):
        return data, None
    items = data
    for k, v in qs.items():
        m = re.match(r"^filter\[(\w+)\]$", k)
        if m:
            items = [x for x in items if isinstance(x, dict) and str(x.get(m.group(1))) == v[0]]
    if qs.get("sort"):
        f = qs["sort"][0]
        rev = f.startswith("-")
        f = f.lstrip("-")
        items = sorted(items, key=lambda x: (x.get(f) is None, str(x.get(f)) if not isinstance(x.get(f), (int, float))
                                             else x.get(f)) if isinstance(x, dict) else (True, ""), reverse=rev)
    total = len(items)
    try:
        size = max(1, min(500, int(qs.get("page_size", ["50"])[0])))
        page = max(1, int(qs.get("page", ["1"])[0]))
    except ValueError:
        size, page = 50, 1
    if "page" in qs or "page_size" in qs:
        items = items[(page - 1) * size: page * size]
    return items, {"x-total-count": total, "x-page": page, "x-page-size": size}


class OpsMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        cfg = _cfg()
        headers = {k.decode().lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        rid = headers.get("x-request-id") if _ID.match(headers.get("x-request-id") or "") else context.new_id()
        corr = headers.get("x-correlation-id") if _ID.match(headers.get("x-correlation-id") or "") else rid
        context.request_id.set(rid)
        context.correlation_id.set(corr)
        path, method = scope["path"], scope["method"].upper()
        version = None
        if path.startswith("/api/v1/"):
            scope = dict(scope)
            scope["path"] = "/api/" + path[len("/api/v1/"):]
            scope["raw_path"] = scope["path"].encode()
            path, version = scope["path"], "v1"
        extra = [(b"x-request-id", rid.encode()), (b"x-correlation-id", corr.encode())] + \
            _security_headers(path, cfg) + ([(b"api-version", b"v1")] if path.startswith("/api/") else [])
        dep = None
        if version == "v1":                    # W39 (API-03): a deprecated v1 resource (enterprise/public_api.py)
            try:
                from enterprise.public_api import deprecation_for
                dep = deprecation_for(method, path)
            except Exception:
                dep = None
            if dep:
                extra += [(k.encode(), v.encode("latin-1")) for k, v in dep["headers"]]
        if version is None and path.startswith("/api/") and \
                (headers.get("authorization") or "").lower().startswith("apikey "):
            # W9: API-key clients should call the frozen /api/v1 contract
            extra += [(b"deprecation", b"true"),
                      (b"link", f'</api/v1/{path[len("/api/"):]}>; rel="successor-version"'.encode())]
        t0 = time.perf_counter()
        state = {"status": 500}

        async def reply(status, payload, more=()):
            body = json.dumps(payload).encode()
            await send({"type": "http.response.start", "status": status,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
                        + extra + list(more)})
            await send({"type": "http.response.body", "body": body})
            state["status"] = status

        if dep and dep["gone"]:
            metrics.inc("atip_http_gone_total")
            return await reply(410, envelope("GONE", f"{dep['resource']} was retired on {dep['sunset']}"
                                             + (f"; use {dep['successor']}" if dep.get("successor") else ""), rid))

        # -- request body (mutating API requests are buffered: size, JSON, idempotency) -------------
        body, buffered = b"", False
        limit = int(cfg.get("max_request_bytes") or 2_000_000)
        cl = headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > limit:
            return await reply(413, envelope("PAYLOAD_TOO_LARGE", f"request body over {limit} bytes", rid))
        if method in MUTATING and path.startswith("/api/"):
            chunks, size = [], 0
            while True:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    return
                chunks.append(msg.get("body", b""))
                size += len(chunks[-1])
                if size > limit:
                    return await reply(413, envelope("PAYLOAD_TOO_LARGE", f"request body over {limit} bytes", rid))
                if not msg.get("more_body"):
                    break
            body, buffered = b"".join(chunks), True
            if body.strip():
                if "application/json" not in (headers.get("content-type") or ""):
                    return await reply(415, envelope("VALIDATION_FAILED", "request body must be application/json", rid))
                try:
                    json.loads(body)
                except Exception:
                    return await reply(400, envelope("VALIDATION_FAILED", "request body is not valid JSON", rid))

        caller = _caller(headers)
        if cfg.get("rate_limit_enabled") and path.startswith("/api/"):
            ip = (scope.get("client") or ("?",))[0]
            ok, wait = _rate_ok(f"{ip}|{caller}", int(cfg.get("rate_limit_per_minute") or 300))
            if not ok:
                metrics.inc("atip_http_rate_limited_total")
                return await reply(429, envelope("RATE_LIMITED", "too many requests", rid, True),
                                   [(b"retry-after", str(wait).encode())])

        idem_key = headers.get("idempotency-key") if method in MUTATING and path.startswith("/api/") else None
        from ops.idempotency import ORDER_ROUTES
        if method in MUTATING and cfg.get("require_idempotency_for_orders") and re.match(ORDER_ROUTES, path) \
                and not idem_key:
            return await reply(400, envelope("VALIDATION_FAILED", "Idempotency-Key header required for order routes",
                                             rid))
        if idem_key:
            if not _ID.match(idem_key[:64]) or len(idem_key) > 64:
                return await reply(400, envelope("VALIDATION_FAILED", "Idempotency-Key: 1-64 chars [A-Za-z0-9._-]",
                                                 rid))
            from db.schema import get_connection
            from ops import idempotency as IDM
            rh = IDM.request_hash(method, path, body)
            c = get_connection()
            try:
                prev = IDM.claim(c, idem_key, caller, rh, int(cfg.get("idempotency_ttl_hours") or 24))
            finally:
                c.close()
            if prev:
                if prev["request_hash"] != rh:
                    return await reply(409, envelope("CONFLICT", "Idempotency-Key reused for a different request",
                                                     rid))
                if prev["status"] != "COMPLETED":
                    return await reply(409, envelope("CONFLICT", "a request with this Idempotency-Key is in progress",
                                                     rid, True))
                b = prev["response_body"] or b""
                await send({"type": "http.response.start", "status": prev["response_status"],
                            "headers": [(b"content-type", (prev["content_type"] or "application/json").encode()),
                                        (b"content-length", str(len(b)).encode()), (b"idempotent-replay", b"true")]
                            + extra})
                await send({"type": "http.response.body", "body": b})
                return

        if buffered:
            sent = {"done": False}

            async def receive_replay():
                if not sent["done"]:
                    sent["done"] = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()
            downstream_receive = receive_replay
        else:
            downstream_receive = receive

        qs = parse_qs(scope.get("query_string", b"").decode())
        paginate = version == "v1" and method == "GET" and any(
            k in qs for k in ("page", "page_size", "sort")) or (version == "v1" and method == "GET" and any(
                k.startswith("filter[") for k in qs))
        capture = paginate or bool(idem_key)
        cap = {"start": None, "chunks": []}

        async def send_wrapper(msg):
            if msg["type"] == "http.response.start":
                state["status"] = msg["status"]
                if capture:
                    cap["start"] = msg
                    return
                msg = dict(msg)
                msg["headers"] = list(msg.get("headers", [])) + extra
            elif msg["type"] == "http.response.body" and capture:
                cap["chunks"].append(msg.get("body", b""))
                if msg.get("more_body"):
                    return
                await self._flush(cap, send, extra, paginate, qs, idem_key, caller)
                return
            await send(msg)

        try:
            await self.app(scope, downstream_receive, send_wrapper)
        except Exception:
            state["status"] = 500
            if idem_key:
                self._release(idem_key, caller)
            raise
        finally:
            route = scope.get("route")
            tmpl = getattr(route, "path", None) or ("static" if not path.startswith("/api/") else "unmatched")
            metrics.inc("atip_http_requests_total", {"method": method, "route": tmpl, "status": state["status"]})
            metrics.observe("atip_http_request_duration_seconds", time.perf_counter() - t0,
                            {"method": method, "route": tmpl})
            if method in MUTATING and path.startswith("/api/"):
                self._audit(headers, method, path, state["status"], scope)

    async def _flush(self, cap, send, extra, paginate, qs, idem_key, caller):
        start = cap["start"]
        body = b"".join(cap["chunks"])
        hdrs = [(k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"]
        ctype = next((v.decode() for k, v in hdrs if k.lower() == b"content-type"), "")
        if paginate and "application/json" in ctype and start["status"] < 300:
            try:
                data, meta = _paginate(json.loads(body), qs)
                if meta:
                    body = json.dumps(data).encode()
                    hdrs += [(k.encode(), str(v).encode()) for k, v in meta.items()]
            except Exception:
                pass
        if idem_key:
            from db.schema import get_connection
            from ops import idempotency as IDM
            c = get_connection()
            try:
                if start["status"] < 300:          # only a success is replayed; a failure may be retried
                    IDM.complete(c, idem_key, caller, start["status"], body, ctype)
                else:
                    IDM.release(c, idem_key, caller)
            finally:
                c.close()
        await send({"type": "http.response.start", "status": start["status"],
                    "headers": hdrs + [(b"content-length", str(len(body)).encode())] + extra})
        await send({"type": "http.response.body", "body": body})

    def _release(self, key, caller):
        try:
            from db.schema import get_connection
            from ops.idempotency import release
            c = get_connection()
            try:
                release(c, key, caller)
            finally:
                c.close()
        except Exception:
            pass

    def _audit(self, headers, method, path, status, scope):
        try:
            from enterprise.config import enabled
            if enabled():
                return                        # the enterprise middleware audits
            from dashboard.security import token_ok
            from db.schema import get_connection
            from enterprise.audit import record
            actor = "local-token" if token_ok(headers.get("x-atip-token")) else "anonymous"
            c = get_connection()
            try:
                record(c, "api." + method.lower(), actor=actor, method=method, path=path, status_code=status,
                       ip=(scope.get("client") or (None,))[0], details={"request_id": context.request_id.get()})
            finally:
                c.close()
        except Exception:
            pass


def install(app):
    """Outermost middleware + optional CORS + error envelope handlers."""
    from ops.config import ops
    from ops.errors import install_handlers
    cfg = ops()
    if cfg.get("cors_origins"):
        from fastapi.middleware.cors import CORSMiddleware
        app.add_middleware(CORSMiddleware, allow_origins=list(cfg["cors_origins"]), allow_credentials=True,
                           allow_methods=["GET", "POST", "PUT", "DELETE"],
                           allow_headers=["Content-Type", "Authorization", "X-ATIP-Token", "Idempotency-Key",
                                          "X-Request-ID", "X-Correlation-ID"])
    app.add_middleware(OpsMiddleware)
    install_handlers(app)
    return app
