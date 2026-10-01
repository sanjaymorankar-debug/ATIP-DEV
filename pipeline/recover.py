"""
Automatic recovery of missed / failed jobs (MON-02 follow-up, 2026-10-02).

pipeline/health.py only REPORTED a problem ("MISSED Global markets (pre-market) -- no run on
2026-10-01 by 07:30") and the data stayed stale until the next day's slot. This module acts on
the report: for each MISSED / FAILING / EMPTY problem that has a remedy it re-runs the job NOW,
fetching current data (today's global markets, the latest news, a fresh portfolio sync, any
unscored session, a morning brief built from what is current), then the caller re-checks.

A re-run is recorded in pipeline_log under the job names health.py counts for that problem
(global_recover, news_recover, morning_digest, ...), so a successful recovery clears it.

Limits, so a broken dependency (expired token, site down) cannot loop:
  MAX_ATTEMPTS a step is tried at most this many times per calendar day
  MIN_GAP      and not again within this many minutes
Every attempt is stored in job_recovery and shown on the dashboard / in the morning brief.

    python -m pipeline.recover            # check, recover, print what happened
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
MIN_GAP = timedelta(minutes=25)
RECOVERABLE = ("MISSED", "FAILING", "EMPTY", "STALLED")

# health.py label -> recovery step
REMEDY = {
    "Global markets (pre-market)": "global",
    "Index levels (intraday)": "intraday",
    "News (morning)": "news",
    "News (midday)": "news",
    "Portfolio sync": "portfolio",
    "Bhavcopy (EOD prices)": "postmarket",
    "Technical indicators": "postmarket",
    "AI scoring": "postmarket",
    "Data quality": "postmarket",
    "Daily P&L": "postmarket",
    "Signal log": "postmarket",
    "Delivery": "eod_late",
    "Accuracy audit (weekly)": "weekly",
    "Morning brief": "brief",
}
# Data first, the brief last: it summarises what the other steps fetched.
ORDER = ("global", "intraday", "news", "portfolio", "postmarket", "eod_late", "weekly", "brief")
STEP_LABEL = {"global": "global markets", "intraday": "index levels", "news": "news",
              "portfolio": "portfolio sync", "postmarket": "post-market scoring", "eod_late": "delivery",
              "weekly": "weekly audit", "brief": "morning brief"}

DDL = """CREATE TABLE IF NOT EXISTS job_recovery (
    day TEXT NOT NULL, step TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_at TIMESTAMP,
    last_result TEXT, problems TEXT, PRIMARY KEY (day, step))"""


def ensure_table(conn):
    conn.execute(DDL)
    conn.commit()


def plan(conn, problems, now=None) -> list[dict]:
    """[{step, labels, allowed, reason}] for the recoverable problems, in run order (pure)."""
    now = now or datetime.now()
    ensure_table(conn)
    steps: dict[str, list] = {}
    for p in problems:
        s = REMEDY.get(p.label)
        if s and p.kind in RECOVERABLE:
            steps.setdefault(s, []).append(f"{p.kind} {p.label}")
    out = []
    for s in ORDER:
        if s not in steps:
            continue
        r = conn.execute("SELECT attempts, last_at FROM job_recovery WHERE day=? AND step=?",
                         (str(now.date()), s)).fetchone()
        attempts, last_at = (r[0], r[1]) if r else (0, None)
        if isinstance(last_at, str):
            last_at = datetime.fromisoformat(last_at[:26].replace(" ", "T"))
        allowed, reason = True, ""
        if attempts >= MAX_ATTEMPTS:
            allowed, reason = False, f"tried {attempts}x today — needs attention"
        elif last_at and now - last_at < MIN_GAP:
            allowed, reason = False, f"retried at {last_at:%H:%M}; next try after {(last_at + MIN_GAP):%H:%M}"
        out.append({"step": s, "labels": steps[s], "allowed": allowed, "reason": reason, "attempts": attempts})
    return out


def _fetch_global(td, mode):
    from data.markets import fetch_global_markets
    rec = fetch_global_markets(td, mode) or {}
    n = len([k for k in rec if k.endswith("_chg")])
    if not n:
        return {"status": "FAILED", "error": "no global quotes returned"}
    return {"status": "SUCCESS", "rows": n}


def _brief():
    from alerts.telegram import send_morning_digest
    return {"status": "SUCCESS", "rows": int(bool(send_morning_digest()))}


def _run_step(step, now):
    from pipeline import scheduler as S
    td = now.date()
    if step == "global":
        mode = "premarket" if now.time() < datetime.strptime("09:15", "%H:%M").time() else "intraday"
        r = S.run_job("global_recover", _fetch_global, td, mode)
        S.run_job("dashboard_refresh", S._rebuild_dashboard, td)
        return r
    if step == "intraday":
        return S.run_intraday_15min(force=True)
    if step == "news":
        from data.news import run_news_pipeline
        r = S.run_job("news_recover", run_news_pipeline, 24)
        try:
            S._news_summary()
        except Exception as e:
            log.debug(f"  news summary after recovery: {e}")
        return r
    if step == "portfolio":
        return S._run_portfolio_sync(td)
    if step == "postmarket":
        return S.run_postmarket_if_missing()
    if step == "eod_late":
        return S.run_eod_late()
    if step == "weekly":
        return S.run_weekly()
    if step == "brief":
        return S.run_job("morning_digest", _brief)
    raise ValueError(step)


def recover(problems, now=None) -> dict:
    """Re-run what `problems` call for. Returns {ran: [...], skipped: [...]} (each with labels)."""
    from db.schema import get_connection
    now = now or datetime.now()
    conn = get_connection()
    try:
        steps = plan(conn, problems, now)
    finally:
        conn.close()
    ran, skipped = [], []
    for st in steps:
        if not st["allowed"]:
            skipped.append(st)
            continue
        log.warning(f"  ↻ Auto-recovery: re-running {STEP_LABEL[st['step']]} for {'; '.join(st['labels'])}")
        result = "RAN"
        try:
            r = _run_step(st["step"], datetime.now())
            if isinstance(r, dict) and r.get("status") == "FAILED":
                result = f"FAILED: {r.get('error') or ''}"[:200]
        except Exception as e:
            log.warning(f"  Auto-recovery {st['step']}: {e}")
            result = f"FAILED: {type(e).__name__}: {e}"[:200]
        conn = get_connection()
        try:
            ensure_table(conn)
            conn.execute(
                "INSERT INTO job_recovery (day, step, attempts, last_at, last_result, problems) VALUES (?,?,1,?,?,?) "
                "ON CONFLICT(day, step) DO UPDATE SET attempts=attempts+1, last_at=excluded.last_at, "
                "last_result=excluded.last_result, problems=excluded.problems",
                (str(now.date()), st["step"], datetime.now(), result, "; ".join(st["labels"])))
            conn.commit()
        finally:
            conn.close()
        ran.append({**st, "result": result})
    return {"ran": ran, "skipped": skipped}


def today(conn=None, day=None) -> list[dict]:
    """Recovery attempts made on `day` (default today), for the dashboard and the brief."""
    from db.schema import get_connection
    own = conn is None
    conn = conn or get_connection()
    try:
        ensure_table(conn)
        rows = conn.execute("SELECT step, attempts, last_at, last_result, problems FROM job_recovery WHERE day=? "
                            "ORDER BY last_at", (str(day or date.today()),)).fetchall()
        return [{"step": r[0], "what": STEP_LABEL.get(r[0], r[0]), "attempts": r[1], "last_at": str(r[2])[:16],
                 "result": r[3], "problems": r[4]} for r in rows]
    finally:
        if own:
            conn.close()


def check_and_recover(now=None) -> dict:
    """check -> recover -> re-check. {problems (still open), recovered (labels now clear), ran, skipped}."""
    from db.schema import get_connection
    from pipeline.health import check_job_health
    conn = get_connection()
    try:
        before = check_job_health(conn, now)
    finally:
        conn.close()
    if not before:
        return {"problems": [], "recovered": [], "ran": [], "skipped": []}
    rep = recover(before)
    after = before
    if rep["ran"]:
        conn = get_connection()
        try:
            after = check_job_health(conn)
        finally:
            conn.close()
    still = {p.label for p in after}
    recovered = [p.label for p in before if p.label not in still]
    return {"problems": after, "recovered": recovered, "ran": rep["ran"], "skipped": rep["skipped"]}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from pipeline.health import format_problems
    out = check_and_recover()
    for r in out["ran"]:
        print(f"re-ran {STEP_LABEL[r['step']]:<20} {r['result']:<10} for {'; '.join(r['labels'])}")
    for s in out["skipped"]:
        print(f"not re-run {STEP_LABEL[s['step']]:<16} {s['reason']}")
    if out["recovered"]:
        print("recovered: " + ", ".join(out["recovered"]))
    print(format_problems(out["problems"]))
