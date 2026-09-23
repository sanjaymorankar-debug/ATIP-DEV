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
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
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


def dashboard_host() -> str:
    """127.0.0.1 unless config.json deliberately says otherwise."""
    host = str(_config().get("dashboard_host", LOCAL_HOST)).strip() or LOCAL_HOST
    if host not in (LOCAL_HOST, "localhost", "::1"):
        log.warning(f"  Dashboard bound to {host} by config.json — the API is reachable "
                    f"from other machines; mutating routes still require the token in "
                    f"{TOKEN_PATH}")
    return host
