"""
W39b scheduled jobs (tracker reconciliation, 2026-10-07).

    sip_run              EX-20   due stock-SIP plans at config "sip_time" (default 09:30) on
                         market days, PAPER only (orders/sip.py); a no-op without plans.
    total_return         W40 PERF-001-05  rebuild the estimated Nifty total-return index
                         (data/total_return.py) at 21:10 on market days; reads only stored data.

The Dhan token renewal and the 7-year history backfill are W39's (PR #4): the LaunchAgent /
Windows task running tools/dhan_token_refresh.py, and data/history_backfill.py scheduled by
pipeline/scheduler.py _schedule_w39_jobs. (W39b had built both too; on merging #4 its own
versions were dropped so there is one of each.)

Switches (atip_data/config.json):
    "sip_enabled": true, "sip_time": "09:30"
"""

from __future__ import annotations

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






def sip_run() -> dict:
    """EX-20: due stock-SIP plans, on market days only (orders/sip.py; PAPER only)."""
    from datetime import date
    from utils.trading_calendar import is_trading_day
    if not is_trading_day(date.today()):
        return {"status": "SKIPPED", "reason": "not a market day"}
    from orders.sip import run_due
    return run_due()


def total_return_run() -> dict:
    """PERF-001-05: rebuild the estimated total-return index after the day's prices and corporate actions."""
    from datetime import date
    from utils.trading_calendar import is_trading_day
    if not is_trading_day(date.today()):
        return {"status": "SKIPPED", "reason": "not a market day"}
    from data.total_return import run
    return run()


def schedule_jobs(schedule, run_job) -> list:
    """Register the W39b jobs; returns what was registered (for the start-up log)."""
    cfg = config()
    out = []
    if cfg.get("sip_enabled", True):
        t = _hhmm(cfg.get("sip_time"), "09:30")
        schedule.every().day.at(t).do(run_job, "sip_run", sip_run)
        out.append(f"sip_run daily {t} (market days, PAPER)")
    schedule.every().day.at("21:10").do(run_job, "total_return", total_return_run)
    out.append("total_return daily 21:10 (market days)")
    return out
