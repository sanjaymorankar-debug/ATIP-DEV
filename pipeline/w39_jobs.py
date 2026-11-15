"""
W39b scheduled jobs (tracker reconciliation, 2026-10-07).

    sip_run              EX-20   due stock-SIP plans at config "sip_time" (default 09:30) on
                         market days, PAPER only (orders/sip.py); a no-op without plans.
    total_return         W40 PERF-001-05  rebuild the estimated Nifty total-return index
                         (data/total_return.py) at 21:10 on market days; reads only stored data.
    risk_model           W40     the factor risk model (quant/risk_model.py) nightly at config
                         "risk_model.time" (default 21:45, after the post-market pipeline, its
                         18:30 catch-up and the 19:30 EOD-late run): exposures for new sessions,
                         regressions whose next session arrived, covariance and specific risk.
                         Incremental and idempotent -- a night it missed is caught up by the next
                         run, a second run the same night finds nothing (NO_NEW). Every day, not
                         only market days: on a holiday it simply has nothing new.

The Dhan token renewal and the 7-year history backfill are W39's (PR #4): the LaunchAgent /
Windows task running tools/dhan_token_refresh.py, and data/history_backfill.py scheduled by
pipeline/scheduler.py _schedule_w39_jobs. (W39b had built both too; on merging #4 its own
versions were dropped so there is one of each.)

    session_reports      RPT-05  every 15 minutes, 06:00-20:45: closed session cycles are stored and posted to the ledger,
                         then the report schedules that are due run (enterprise/session_reports.py).
                         Off with config "reports": {"enabled": false}. A no-op on the view-only server.

    post_market_rca      6 Nov 2026  at 16:00 IST on market days: the day's post-market RCA of the sessions' trades
                         (portfolio/post_market_rca.py; stored in post_market_rca, shown at /rca). Read-only; a no-op
                         on the view-only server. Off with config "post_market_rca": {"enabled": false}.

    instrument_master    16 Nov 2026 (owner item 5)  08:00 IST daily: Dhan's scrip master into instrument_master
                         (renames, REITs, tick and lot sizes; data/instruments.py)
    preopen_health       16 Nov 2026 (owner item 6)  08:45 IST on market days, retried 08:50 / 09:00 / 09:10 while
                         failing: Dhan token (DH-901, TOTP renewal), quote feed, today's scrip master, the previous
                         session's scores. Auto-pick waits on gate_status(); the reason is a banner on every page.
    intraday_completeness 16 Nov 2026 (owner item 18)  16:15 IST on market days: alert when fewer than 95% of the
                         universe have 15-minute bars for a session (data/completeness.py)

Switches (atip_data/config.json):
    "sip_enabled": true, "sip_time": "09:30"
    "post_market_rca": {"enabled": true, "time": "16:00"}
    "risk_model": {"enabled": true, "time": "21:45", ...}   (quant/risk_model.py DEFAULTS)
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


def risk_model_run() -> dict:
    """W40: bring the factor risk model up to the latest session (quant/risk_model.py)."""
    from quant.risk_model import run_scheduled
    return run_scheduled()


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
    from quant.risk_model import settings as rm_settings
    rm = rm_settings()
    if rm["enabled"]:
        t = _hhmm(rm["time"], "21:45")
        schedule.every().day.at(t).do(run_job, "risk_model", risk_model_run)
        out.append(f"risk_model daily {t} (factor risk model, incremental)")
    if (cfg.get("reports") or {}).get("enabled", True):
        # RPT-05 (2 Nov 2026): newly closed session cycles into session_cycles and the ledger, then the due
        # report schedules (enterprise/session_reports.py; their times are IST, a missed one catches up)
        for hh in range(6, 21):                  # every 15 min inside ATIP's run window (06:00-21:00 IST)
            for mm in (0, 15, 30, 45):
                schedule.every().day.at(f"{hh:02d}:{mm:02d}").do(run_job, "session_reports", session_reports_tick)
        out.append("session_reports every 15 min 06:00-20:45 (cycle book + due report schedules)")
    rca = cfg.get("post_market_rca") or {}
    if rca.get("enabled", True):
        # 6 Nov 2026 (owner): the daily post-market RCA of the sessions' trades, after the close (portfolio/post_market_rca.py)
        t = _hhmm(rca.get("time"), "16:00")
        schedule.every().day.at(t).do(run_job, "post_market_rca", post_market_rca_run)
        out.append(f"post_market_rca daily {t} IST (market days; session trades' RCA, what-if rules)")
    # KGF-09/10/13 (16 Nov 2026): the regime gate's breadth and the breakout-retest scan after the day's scores (the
    # evening re-score is done by ~21:00 IST; the 07:30 run catches a late one before the next open), and the weekly
    # A/B report of the session arms on Sunday 18:00 IST.
    for t in ("21:20", "07:30"):
        schedule.every().day.at(t).do(run_job, "breadth_and_scan", breadth_and_scan_run)
    out.append("breadth_and_scan daily 21:20 + 07:30 IST (breadth20/50, breakout-retest scan)")
    schedule.every().day.at("18:00").do(run_job, "ab_weekly_report", ab_report_run)      # runs on Sundays only
    out.append("ab_weekly_report Sunday 18:00 IST (session arms, PAPER A/B)")
    # 16 Nov 2026 (owner items 5, 6, 18): Dhan's scrip master at 08:00 IST (data/instruments.py), the pre-open health
    # gate at 08:45 with retries at 08:50 / 09:00 / 09:10 (ops/preopen_health.py; auto-pick waits on it), and the
    # 15-minute-bar completeness check at 16:15 on market days (data/completeness.py). Each is a no-op on the viewer.
    try:
        from data.instruments import settings as im_settings
        im = im_settings()
        if im["enabled"]:
            t = _hhmm(im["time"], "08:00")
            schedule.every().day.at(t).do(run_job, "instrument_master", instrument_master_run)
            out.append(f"instrument_master daily {t} IST (Dhan scrip master, renames, tick sizes)")
        from ops.preopen_health import schedule_jobs as preopen_jobs
        out += preopen_jobs(schedule, run_job)
        from data.completeness import settings as dc_settings
        dc = dc_settings()
        if dc["enabled"]:
            t = _hhmm(dc["time"], "16:15")
            schedule.every().day.at(t).do(run_job, "intraday_completeness", intraday_completeness_run)
            out.append(f"intraday_completeness daily {t} IST (market days; alert under {dc['min_share']:.0%})")
    except Exception as e:
        log.warning(f"  instrument master / pre-open gate / completeness jobs not scheduled: {e}")
    return out


def instrument_master_run() -> dict:
    from data.instruments import run_scheduled
    return run_scheduled()


def intraday_completeness_run() -> dict:
    from data.completeness import run_scheduled
    return run_scheduled()


def breadth_and_scan_run() -> dict:
    from scores.breadth import run_scheduled
    out = {"breadth": run_scheduled()}
    try:
        from db.schema import get_connection
        from strategy.breakout_retest import run_scan
        conn = get_connection()
        try:
            r = run_scan(conn, source="schedule")
            out["scan"] = {"as_of": r.get("as_of"), "setups": len(r.get("rows") or []), "scanned": r.get("scanned")}
        finally:
            conn.close()
    except Exception as e:
        out["scan_error"] = str(e)
    return out


def ab_report_run() -> dict:
    from portfolio.ab_report import run_scheduled
    return run_scheduled()


def post_market_rca_run() -> dict:
    from portfolio.post_market_rca import run_scheduled
    return run_scheduled()


def session_reports_tick() -> dict:
    from enterprise.session_reports import tick
    return tick()
