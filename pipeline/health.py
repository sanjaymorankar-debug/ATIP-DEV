"""
Job health: is every scheduled job running, and is it doing anything? (MON-02)

pipeline_log records every run (pipeline.scheduler.run_job, kind='run'). This
module holds what each important job is EXPECTED to do and compares the two:

  MISSED   a daily job has no run by its deadline on a trading day
  STALLED  an intraday job's last run is too old during market hours
  FAILING  a job's last FAILING_STREAK runs all failed
  EMPTY    a job that should produce rows ran and produced none

Nothing used to make this comparison. The news job logged nothing from
2026-09-10 for eleven days and no surface reported it; portfolio sync failed
on every run for a week, each failure logged as a green tick.

    python -m pipeline.health            # print current problems
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from utils.trading_calendar import is_trading_day

log = logging.getLogger(__name__)

FAILING_STREAK = 3
BAD = ("FAILED",)


@dataclass(frozen=True)
class Expect:
    """What one job (or any of a group of alternative jobs) should do.

    daily_by   on each trading day, a run must have started by this time
    every_min  during market hours, a run at least this often
    weekly_by  (weekday, time): a run in the 8 days before that moment
    need_rows  a run that processed nothing does not count as the job working
    """
    label: str
    jobs: tuple
    daily_by: time | None = None
    every_min: int | None = None
    weekly_by: tuple | None = None
    need_rows: bool = False
    need_success: bool = False   # only SUCCESS / EMPTY count (SKIPPED does not)
    from_time: time | None = None  # a run on the due day counts only from this time (midday news)


# Deadlines are the scheduled time plus a margin for the job to finish; for the
# post-market chain, after the 18:30 catch-up. The *_catchup / *_recover names are
# the start-up catch-up and pipeline/recover.py re-runs of the same work.
EXPECTATIONS = (
    Expect("Global markets (pre-market)", ("global_premarket", "global_catchup", "global_recover"),
           daily_by=time(7, 30), need_rows=True),
    Expect("News (morning)", ("news_premarket", "news_catchup", "news_recover"), daily_by=time(8, 30),
           need_rows=True),
    # Zerodha reports SKIPPED when it is not connected; that must not make a
    # failed Dhan sync look like a working portfolio sync.
    Expect("Portfolio sync", ("dhan_portfolio", "zerodha_portfolio"), daily_by=time(8, 30), need_success=True),
    Expect("Morning brief", ("morning_digest",), daily_by=time(8, 45)),
    Expect("Index levels (intraday)", ("intraday_indexes",), every_min=45, need_rows=True),
    Expect("News (midday)", ("news_midday", "news_catchup", "news_recover"), daily_by=time(12, 30),
           from_time=time(11, 30)),
    Expect("Bhavcopy (EOD prices)", ("bhavcopy_eod",), daily_by=time(18, 45), need_rows=True),
    Expect("Technical indicators", ("technical_indicators",), daily_by=time(18, 45), need_rows=True),
    Expect("AI scoring", ("ai_scoring_engine",), daily_by=time(18, 45), need_rows=True),
    Expect("NSE index closes", ("nse_index_closes",), daily_by=time(21, 45)),
    Expect("Signal log", ("signal_log",), daily_by=time(21, 45)),
    Expect("Delivery", ("delivery",), daily_by=time(20, 0)),
    Expect("Data quality", ("data_quality",), daily_by=time(18, 45), need_rows=True),
    Expect("Daily P&L", ("daily_pnl",), daily_by=time(18, 45)),
    Expect("Accuracy audit (weekly)", ("accuracy_audit",), weekly_by=(5, time(9, 30))),
)

MARKET_OPEN, MARKET_CLOSE = time(9, 15), time(15, 30)


@dataclass
class Problem:
    label: str
    kind: str            # MISSED / STALLED / FAILING / EMPTY
    detail: str
    jobs: tuple

    @property
    def key(self) -> str:
        """Identifies the problem for de-duplicating alerts within a day."""
        return f"{self.kind}:{'|'.join(self.jobs)}"


def _runs(conn, jobs, since=None, limit=None):
    q = (f"SELECT job_name, status, rows_processed, error_msg, start_time FROM pipeline_log "
         f"WHERE kind='run' AND job_name IN ({','.join('?' * len(jobs))})")
    args = list(jobs)
    if since is not None:
        q += " AND start_time >= ?"; args.append(since)
    q += " ORDER BY start_time DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(q, args)]


def _ok(run, need_rows, need_success=False):
    if run["status"] in BAD:
        return False
    if need_success and run["status"] not in ("SUCCESS", "EMPTY"):
        return False
    if need_rows and run["status"] in ("EMPTY",):
        return False
    return True


def _last_due_day(deadline: time, now: datetime) -> date | None:
    """The most recent trading day whose deadline has passed, within a week."""
    d = now.date()
    for _ in range(8):
        if is_trading_day(d) and datetime.combine(d, deadline) <= now:
            return d
        d -= timedelta(days=1)
    return None


def _monitoring_since(conn) -> datetime | None:
    """When run_job first recorded a run. Rows written before that are the
    jobs' own step rows, under other names, so a period before it cannot be
    judged -- without this, the first day would report every job as missed."""
    r = conn.execute("SELECT MIN(start_time) FROM pipeline_log WHERE kind='run'").fetchone()[0]
    if r is None:
        return None
    return r if isinstance(r, datetime) else datetime.fromisoformat(str(r))


def check_job_health(conn, now: datetime | None = None) -> list[Problem]:
    now = now or datetime.now()
    problems: list[Problem] = []
    since = _monitoring_since(conn)
    if since is None:
        return problems
    for e in EXPECTATIONS:
        recent = _runs(conn, e.jobs, limit=FAILING_STREAK)
        if len(recent) == FAILING_STREAK and all(r["status"] in BAD for r in recent):
            problems.append(Problem(e.label, "FAILING",
                                    f"last {FAILING_STREAK} runs failed — {recent[0]['error_msg'] or 'no error text'}",
                                    e.jobs))
            continue
        if e.daily_by:
            day = _last_due_day(e.daily_by, now)
            if day is None or since > datetime.combine(day, time(0, 0)):
                continue
            # Runs on the due day (from from_time), or any later run: a slot missed on the
            # day but re-run since (start-up catch-up, pipeline/recover.py) has current data.
            runs = _runs(conn, e.jobs, since=datetime.combine(day, e.from_time or time(0, 0)))
            if not runs:
                problems.append(Problem(e.label, "MISSED",
                                        f"no run on {day} by {e.daily_by:%H:%M}", e.jobs))
            elif not any(_ok(r, e.need_rows, e.need_success) for r in runs):
                last = runs[0]
                kind = "EMPTY" if last["status"] == "EMPTY" else "FAILING"
                problems.append(Problem(e.label, kind,
                                        f"{len(runs)} run(s) on {day}, none worked — last {last['status']}"
                                        + (f": {last['error_msg']}" if last["error_msg"] else ""), e.jobs))
        elif e.every_min:
            if not is_trading_day(now.date()):
                continue
            window_start = datetime.combine(now.date(), MARKET_OPEN) + timedelta(minutes=e.every_min)
            if not (window_start <= now <= datetime.combine(now.date(), MARKET_CLOSE)):
                continue
            if since > now - timedelta(minutes=e.every_min):
                continue
            last_ok = next((r for r in _runs(conn, e.jobs, since=now - timedelta(minutes=e.every_min))
                            if _ok(r, e.need_rows, e.need_success)), None)
            if not last_ok:
                prev = _runs(conn, e.jobs, limit=1)
                problems.append(Problem(e.label, "STALLED",
                                        f"no working run in the last {e.every_min} min"
                                        + (f" — last {prev[0]['status']} at {str(prev[0]['start_time'])[11:16]}"
                                           if prev else " — never run"), e.jobs))
        elif e.weekly_by:
            wd, t = e.weekly_by
            due = now.date() - timedelta(days=(now.date().weekday() - wd) % 7)
            if datetime.combine(due, t) > now:
                due -= timedelta(days=7)
            window = datetime.combine(due, t) - timedelta(days=8)
            if since > window:
                continue
            if not _runs(conn, e.jobs, since=window):
                problems.append(Problem(e.label, "MISSED", f"no run in the week to {due}", e.jobs))
    return problems


def format_problems(problems: list[Problem]) -> str:
    if not problems:
        return "All monitored jobs are running."
    return "\n".join(f"{p.kind:<8} {p.label}: {p.detail}" for p in problems)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    argparse.ArgumentParser(description=__doc__).parse_args()
    from db.schema import get_connection
    conn = get_connection()
    try:
        print(format_problems(check_job_health(conn)))
    finally:
        conn.close()
