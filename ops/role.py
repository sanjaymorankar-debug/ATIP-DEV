"""
Which copy of ATIP this is: the TRADER (the Mac) or a VIEWER (a web server).

The same code runs in both places. The Mac runs everything -- the schedule, the Dhan feeds, the order-rules
monitor, AUTO sessions, Telegram. A web server shows what the Mac has, and nothing on it can trade:

    ATIP_ROLE unset, "" or "trader"   TRADER   (the default: nothing changes on the Mac)
    ATIP_ROLE "viewer"                VIEWER
    anything else                     VIEWER, with a warning

config.json "role" is read the same way, and VIEWER wins if either one says so. Like broker_env falling back
to PAPER, the failure mode of a typo is "this copy cannot trade", never the reverse.

A VIEWER (docs/VIEWER_SERVER.md):
  * starts the dashboard only -- no scheduler, no Dhan feeds, no order-rules monitor (main.py,
    dashboard/server.py), and refuses the commands that fetch, alert or trade (blocked_commands)
  * refuses every POST / PUT / PATCH / DELETE except sign-in, with 403 and the reason (install_guard)
  * never builds a Dhan client (data/dhan.get_dhan_client) or an execution client
    (orders/environment.get_execution_client), and its paper broker places nothing (orders/paper.py)
  * sends no Telegram alert (alerts/telegram.notify): the Mac sends them
  * takes its database from the Mac (ops/viewer_sync.py); nothing travels back
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("atip.ops")

CONFIG_PATH = Path("atip_data") / "config.json"

TRADER = "TRADER"
VIEWER = "VIEWER"

MESSAGE = "This copy of ATIP is view only: orders, trading and changes run on the Mac."
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
# Sign-in stays open so a session can be started and ended; nothing else writes.
SIGN_IN = ("/api/auth/login", "/api/auth/refresh", "/api/auth/logout")

_warned: set = set()


class ViewerRefused(RuntimeError):
    """Something a VIEWER must not do. A RuntimeError, so callers that already handle a missing Dhan
    credential (RuntimeError) handle this the same way."""


def _declared() -> list:
    try:
        from_config = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("role")
    except Exception:
        from_config = None
    return [str(v).strip().lower() for v in (os.environ.get("ATIP_ROLE"), from_config)
            if v is not None and str(v).strip()]


def role() -> str:
    for value in _declared():
        if value == "trader":
            continue
        if value != "viewer" and value not in _warned:
            _warned.add(value)
            log.warning(f"  ATIP_ROLE / config role {value!r} is neither trader nor viewer — running as VIEWER")
        return VIEWER
    return TRADER


def is_viewer() -> bool:
    return role() == VIEWER


def refuse(what: str) -> None:
    """Raise ViewerRefused on a VIEWER; nothing on the TRADER."""
    if is_viewer():
        raise ViewerRefused(f"{what}: {MESSAGE}")


# main.py flags that fetch data, schedule jobs, alert or reach the broker. A VIEWER runs none of them.
_MAC_ONLY = (("scheduler", "--scheduler"), ("run", "--run"), ("live_feed", "--live-feed"),
             ("login_zerodha", "--login-zerodha"), ("test_telegram", "--test-telegram"),
             ("morning_brief", "--morning-brief"), ("dhan_securities", "--dhan-securities"),
             ("dhan_history", "--dhan-history"), ("dhan_quote", "--dhan-quote"), ("purge_db", "--purge-db"))


def blocked_commands(args) -> list:
    """The main.py flags in `args` that a VIEWER refuses; [] on the TRADER."""
    if not is_viewer():
        return []
    return [flag for attr, flag in _MAC_ONLY if getattr(args, attr, None)]


def install_guard(app):
    """VIEWER: every write is refused with 403 and the reason, except sign-in. GET pages and APIs keep working.
    On the TRADER the middleware passes every request through, so the Mac's dashboard is unchanged."""
    from starlette.responses import JSONResponse

    @app.middleware("http")
    async def _viewer_read_only(request, call_next):
        if request.method in WRITE_METHODS and is_viewer():
            path = request.scope.get("path") or ""
            root = request.scope.get("root_path") or ""
            if root and path.startswith(root):
                path = path[len(root):] or "/"
            if not path.startswith(SIGN_IN):
                return JSONResponse({"error": MESSAGE, "read_only": True, "role": VIEWER}, status_code=403)
        return await call_next(request)
