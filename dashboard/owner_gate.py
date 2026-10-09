"""
Owner-only gate for a hosted ATIP, driven by the central bkesari.com login.

ATIP can place real orders with the owner's Dhan account, so once it is
reachable from the internet nobody but the owner may learn it is there. With
this gate on, every request -- page, API, static file -- must carry a
bkesari.com session that the central auth service says is the owner's
(introspection answers ``"atip": true``: the SUPER_ADMIN account whose email
is AUTH_OWNER_EMAIL, with TOTP passed on that session). Anything else gets
the same bare 404 FastAPI gives a path that does not exist: not 401, not 403,
nothing that says "there is something here".

Off unless BKESARI_SSO_INTROSPECT_URL is set, so a local run on the Mac is
unchanged. It is installed as the OUTERMOST middleware, ahead of the token
guard, enterprise auth and the ops layer, so a non-owner request touches
nothing else.

  BKESARI_SSO_INTROSPECT_URL   https://dev.bkesari.com/auth/api/introspect
  BKESARI_SSO_CLIENT_ID        an `internal` client registered on the hub (atip-dev)
  BKESARI_SSO_CLIENT_SECRET
  BKESARI_SSO_COOKIE           default "__Host-bk_sso,bk_sso"
  ATIP_OWNER_GATE_EXEMPT       default "/health/live" (container probe; says nothing)

The Dhan credentials are untouched by this: they stay in atip_data/config.json
or the DHAN_* environment on the ATIP host, and never reach a browser.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from typing import Callable, Optional

log = logging.getLogger("atip.owner_gate")

NOT_FOUND = b'{"detail":"Not Found"}'
CACHE_SECONDS = 30


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def enabled() -> bool:
    return bool(_env("BKESARI_SSO_INTROSPECT_URL"))


def _cookie_names() -> list[str]:
    return [n.strip() for n in _env("BKESARI_SSO_COOKIE", "__Host-bk_sso,bk_sso").split(",") if n.strip()]


def read_token(cookie_header: str) -> Optional[str]:
    jar: dict[str, str] = {}
    for part in (cookie_header or "").split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        jar.setdefault(k.strip(), v.strip())
    for name in _cookie_names():
        if jar.get(name):
            return urllib.parse.unquote(jar[name])
    return None


def _introspect_http(token: str) -> dict:
    """POST {token} to the central service with client credentials."""
    creds = f"{urllib.parse.quote(_env('BKESARI_SSO_CLIENT_ID'))}:{urllib.parse.quote(_env('BKESARI_SSO_CLIENT_SECRET'))}"
    req = urllib.request.Request(
        _env("BKESARI_SSO_INTROSPECT_URL"),
        data=json.dumps({"token": token}).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Basic " + base64.b64encode(creds.encode()).decode(),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 -- URL is operator config
        return json.loads(resp.read().decode() or "{}")


class OwnerCheck:
    """Is the session behind this token the owner's? Cached briefly; fails closed."""

    def __init__(self, introspect: Callable[[str], dict] = _introspect_http, ttl: float = CACHE_SECONDS):
        self._introspect = introspect
        self._ttl = ttl
        self._cache: dict[str, tuple[float, bool]] = {}
        self._lock = threading.Lock()

    def is_owner(self, token: Optional[str]) -> bool:
        if not token or len(token) > 200:
            return False
        key = hashlib.sha256(token.encode()).hexdigest()
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < self._ttl:
                return hit[1]
        try:
            answer = self._introspect(token)
            owner = bool(answer.get("active") is True and answer.get("atip") is True)
        except Exception as e:  # unreachable, refused, malformed: nobody gets in
            log.warning("owner gate: introspection failed: %s", e)
            owner = False
        with self._lock:
            if len(self._cache) > 1000:
                self._cache.clear()
            self._cache[key] = (now, owner)
        return owner


class OwnerGateMiddleware:
    """Pure ASGI, so it can sit outside everything and cost nothing extra."""

    def __init__(self, app, check: Optional[OwnerCheck] = None, exempt: Optional[list[str]] = None):
        self.app = app
        self.check = check or OwnerCheck()
        raw = _env("ATIP_OWNER_GATE_EXEMPT", "/health/live")
        self.exempt = set(exempt if exempt is not None else [p.strip() for p in raw.split(",") if p.strip()])

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        if scope["type"] == "http" and scope.get("path") in self.exempt and scope.get("method") in ("GET", "HEAD"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        token = read_token(headers.get("cookie", ""))
        import anyio
        owner = await anyio.to_thread.run_sync(self.check.is_owner, token)
        if owner:
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send({
            "type": "http.response.start",
            "status": 404,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(NOT_FOUND)).encode()),
                        (b"cache-control", b"no-store")],
        })
        await send({"type": "http.response.body", "body": NOT_FOUND})


def install(app, check: Optional[OwnerCheck] = None):
    """Adds the gate as the outermost middleware when central login is configured."""
    if not enabled():
        return False
    app.add_middleware(OwnerGateMiddleware, check=check)
    log.info("owner gate on: ATIP answers 404 to everyone but the owner")
    return True
