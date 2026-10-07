"""
W39 scheduled jobs (owner notes, 2026-10-07).

    dhan_token_refresh   OPS-12  the Dhan access token is renewed every day before the
                         pre-market jobs (config "dhan_token_refresh_time", default 06:45
                         IST) and once at start-up when it no longer works -- RenewToken
                         while it is valid, else TOTP + PIN (tools/dhan_token_refresh.py).
                         Until W39 only a Windows scheduled task did this, so on the macOS
                         machine the token simply expired every 24 hours.
    sip_run              EX-20   due stock-SIP plans at config "sip_time" (default 09:30) on
                         market days, PAPER only (orders/sip.py); a no-op without plans.
    history_backfill     DP-23   weekly top-up of the long daily history (Sunday,
                         config "history_backfill_time", default 06:00): only what is
                         missing, so after the first run it is a few calls for new symbols.

Switches (atip_data/config.json):
    "dhan_token_auto_refresh": true      (default true; needs dhan_pin + dhan_totp_secret
                                          for an expired token)
    "dhan_token_refresh_time": "06:45"
    "history_backfill_weekly": true      (default true)
    "history_backfill_time": "06:00"
    "history_years": 7
    "sip_enabled": true, "sip_time": "09:30"
"""

from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path

log = logging.getLogger("atip.scheduler")

ROOT = Path(__file__).resolve().parent.parent


def config() -> dict:
    try:
        return json.loads((ROOT / "atip_data" / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _hhmm(v, default):
    s = str(v or default)
    try:
        h, m = s.split(":")
        if 0 <= int(h) < 24 and 0 <= int(m) < 60:
            return f"{int(h):02d}:{int(m):02d}"
    except ValueError:
        pass
    log.warning(f"  invalid time {s!r} in config.json; using {default}")
    return default


def _refresher():
    spec = importlib.util.spec_from_file_location("atip_dhan_token_refresh", ROOT / "tools" / "dhan_token_refresh.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def dhan_token_refresh(only_if_invalid: bool = False) -> dict:
    cfg = config()
    if not cfg.get("dhan_client_id") and not _vaulted_client_id():
        return {"status": "SKIPPED", "reason": "no Dhan client id configured"}
    code = _refresher().run(only_if_invalid=only_if_invalid)
    return {"status": "SUCCESS" if code == 0 else "FAILED",
            "error": None if code == 0 else "token could not be renewed: see atip_data/dhan_token_refresh.log "
                                            "(an expired token needs dhan_pin + dhan_totp_secret)"}


def _vaulted_client_id() -> bool:
    try:
        from ops.secrets import get
        return bool(get("DHAN_CLIENT_ID", log_access=False))
    except Exception:
        return False


def history_backfill() -> dict:
    from data.history_backfill import backfill
    r = backfill()
    return {"status": r.get("status"), "rows": r.get("rows"),
            "error": None if r.get("status") in ("SUCCESS", "NO_NEW") else
            f"{sum(1 for x in r.get('results', []) if x.get('status') == 'FAILED')} window(s) failed"}


def sip_run() -> dict:
    """EX-20: due stock-SIP plans, on market days only (orders/sip.py; PAPER only)."""
    from datetime import date
    from utils.trading_calendar import is_trading_day
    if not is_trading_day(date.today()):
        return {"status": "SKIPPED", "reason": "not a market day"}
    from orders.sip import run_due
    return run_due()


def schedule_jobs(schedule, run_job) -> list:
    """Register the W39 jobs; returns what was registered (for the start-up log)."""
    cfg = config()
    out = []
    if cfg.get("dhan_token_auto_refresh", True):
        t = _hhmm(cfg.get("dhan_token_refresh_time"), "06:45")
        schedule.every().day.at(t).do(run_job, "dhan_token_refresh", dhan_token_refresh)
        out.append(f"dhan_token_refresh daily {t}")
    if cfg.get("sip_enabled", True):
        t = _hhmm(cfg.get("sip_time"), "09:30")
        schedule.every().day.at(t).do(run_job, "sip_run", sip_run)
        out.append(f"sip_run daily {t} (market days, PAPER)")
    if cfg.get("history_backfill_weekly", True):
        t = _hhmm(cfg.get("history_backfill_time"), "06:00")
        schedule.every().sunday.at(t).do(run_job, "history_backfill", history_backfill)
        out.append(f"history_backfill Sunday {t}")
    return out


def startup(run_job) -> None:
    """At scheduler start: a token that stopped working (the machine was off at 06:45) is
    renewed before the catch-up jobs fetch anything."""
    if config().get("dhan_token_auto_refresh", True):
        try:
            run_job("dhan_token_refresh", dhan_token_refresh, only_if_invalid=True)
        except Exception as e:
            log.warning(f"  Dhan token start-up check: {e}")
