"""
Who can reach the dashboard, and who can change anything through it.

The API served 15 routes on 0.0.0.0 with no authentication at all, five of them
mutating: POST /api/refresh (starts the whole post-market pipeline), POST
/api/orders (creates an order rule), POST /api/orders/{id}/confirm (places the
order), /reject, and DELETE /api/orders/{id}. Anything that could reach the port
could trade.

Two changes, both of which the owner should not notice:

  HOST. Bound to 127.0.0.1 unless config.json says otherwise
  ("dashboard_host"). ATIP is a single-user system on one machine and is
  published elsewhere only as a read-only snapshot (publish_snapshot.py renders
  from the database, it does not call this API), so nothing legitimate needed
  the open bind. Setting dashboard_host back to 0.0.0.0 is honoured and logged
  as the deliberate choice it is.

  TOKEN. Every mutating route requires the X-ATIP-Token header. The token is
  generated once into atip_data/dashboard_token.txt and injected into the page
  that this same server serves, so the dashboard keeps working untouched. This
  is what stops a web page open in the same browser from POSTing an order to
  localhost: a cross-site script cannot set a custom header without a CORS
  preflight this server never grants. GET routes stay open locally -- they read
  nothing secret that a local user cannot already read from the database.

  Enterprise mode serves other tenants from the same app. The install's token
  stays the owner's: no other tenant's page carries it and its write guard
  refuses it. A signed-in tenant gets a token of its own, derived from the
  install's and tied to its session (session_token): its pages carry that, and
  the guard accepts it for that session alone.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
from contextvars import ContextVar
from pathlib import Path

log = logging.getLogger("atip.dashboard")

CONFIG_PATH = Path("atip_data") / "config.json"
TOKEN_PATH = Path("atip_data") / "dashboard_token.txt"
TOKEN_HEADER = "X-ATIP-Token"
LOCAL_HOST = "127.0.0.1"


def _config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def token() -> str:
    """The install's dashboard token, created on first use."""
    try:
        existing = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning(f"  dashboard token unreadable ({e}) — issuing a new one")
    fresh = secrets.token_urlsafe(32)
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(fresh, encoding="utf-8")
    log.info(f"  ✓ Dashboard token issued → {TOKEN_PATH}")
    return fresh


def token_ok(supplied: str | None) -> bool:
    return bool(supplied) and hmac.compare_digest(str(supplied), token())


# The dashboard credential the request being served may carry, set by enterprise/authz.py. None while enterprise
# mode is off, where the install's token is the one credential. In enterprise mode it is the install's token for the
# owner, the signed-in session's own token (session_token) for any other tenant, and "" for a stranger.
credential: ContextVar[str | None] = ContextVar("atip_dashboard_credential", default=None)


def session_token(session_secret: str) -> str:
    """One signed-in session's dashboard token. It is derived from the install's token, so it is not that token and
    no other session can derive it: the session's own pages carry it, and the write guard accepts it for that session."""
    return hmac.new(token().encode(), f"session:{session_secret}".encode(), "sha256").hexdigest()


def page_token() -> str:
    """The token a page may carry for the request being served (see credential)."""
    current = credential.get()
    return token() if current is None else current


def guard_ok(supplied: str | None) -> bool:
    """Whether the write guard accepts the supplied token for the request being served."""
    current = credential.get()
    if current is None:
        return token_ok(supplied)
    return bool(current) and bool(supplied) and hmac.compare_digest(str(supplied), current)


def dashboard_host() -> str:
    """127.0.0.1 unless config.json (or, W38 OPS-04, ATIP_DASHBOARD_HOST -- the container image sets
    0.0.0.0 INSIDE the container; docker-compose publishes it on the host's 127.0.0.1 only)
    deliberately says otherwise."""
    host = (os.environ.get("ATIP_DASHBOARD_HOST") or str(_config().get("dashboard_host", LOCAL_HOST))).strip() \
        or LOCAL_HOST
    if host not in (LOCAL_HOST, "localhost", "::1"):
        log.warning(f"  Dashboard bound to {host} by config.json — the API is reachable "
                    f"from other machines; mutating routes still require the token in "
                    f"{TOKEN_PATH}")
    return host


def dashboard_root_path() -> str:
    """The URL prefix the dashboard is served under, "" when served at the root.

    Set ATIP_ROOT_PATH=/ATIP (or config.json dashboard_root_path) when a reverse
    proxy serves ATIP at https://<host>/ATIP/ and strips that prefix before
    forwarding. FastAPI needs to know the prefix to build correct links and to
    serve /ATIP/docs and /ATIP/openapi.json; without it every generated URL points
    at the proxy's root and the dashboard's own fetch() calls 404.

    Normalised to a leading slash and no trailing slash, which is the form
    Starlette expects.
    """
    raw = (os.environ.get("ATIP_ROOT_PATH")
           or str(_config().get("dashboard_root_path", ""))).strip()
    if not raw or raw == "/":
        return ""
    return "/" + raw.strip("/")
