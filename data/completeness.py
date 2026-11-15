"""
Data completeness of the stored 15-minute bars (owner item 18, 16 Nov 2026).

intraday_bars (interval_min 15, written every 30 minutes from 10:00 by pipeline/scheduler.run_intraday_30min from
Dhan's intraday API) has no rows at all for the 13 sessions from 6 to 23 Oct 2026 -- ATIP was off for the Windows
to Mac move -- and nothing noticed: the intraday scans, the 75-minute ratings and the microstructure features read
around the hole. The backup of 15 Nov 19:15 has 499 symbols x ~24.8 bars on every other session since 28 Sep.

    check(conn, session, ...)    the share of the tracked universe with at least one bar that session
    run(conn=None, today=None)   today's session plus the last LOOKBACK_SESSIONS (from the first stored session on):
                                 each stored in data_completeness; a session below min_share (95%) is alerted once
                                 (Telegram + the dashboard's alert list), with the backfill command for the gap
    run_scheduled()              the 16:15 IST job on market days (pipeline/w39_jobs.py), after the 15:30 bar run

The universe is today's tracked one (data/dhan.get_tracked_symbols: the Nifty 500 list without REITs, plus holdings),
so an old session is measured against today's list -- a constituent added since counts as missing there, which is
why only a session below 95% is flagged, not every symbol. tools/backfill_intraday_bars.py fills a gap.

Config (atip_data/config.json): "intraday_completeness": {"enabled": true, "time": "16:15", "min_share": 0.95}
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger("atip.completeness")

CONFIG_PATH = Path("atip_data") / "config.json"
DATASET = "intraday_bars_15m"
MIN_SHARE = 0.95
LOOKBACK_SESSIONS = 30
BACKFILL_CMD = ".venv/bin/python tools/backfill_intraday_bars.py --from {first} --to {last} --apply"


def settings() -> dict:
    try:
        c = (json.loads(CONFIG_PATH.read_text(encoding="utf-8")) or {}).get("intraday_completeness") or {}
    except Exception:
        c = {}
    try:
        share = float(c.get("min_share", MIN_SHARE))
    except (TypeError, ValueError):
        share = MIN_SHARE
    return {"enabled": c.get("enabled", True) is not False, "time": str(c.get("time") or "16:15"),
            "min_share": share}


def ensure_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS data_completeness (
        session_date TEXT NOT NULL, dataset TEXT NOT NULL, universe INTEGER, have INTEGER, bars INTEGER,
        share REAL, min_share REAL, ok INTEGER, missing_sample TEXT, alerted INTEGER DEFAULT 0, checked_at TEXT,
        PRIMARY KEY (session_date, dataset))""")


def _universe(conn) -> list:
    from data.dhan import get_tracked_symbols
    return list(get_tracked_symbols(conn))


def check(conn, session, universe=None, interval_min=15, min_share=MIN_SHARE) -> dict:
    """{session, universe, have, bars, share, ok, missing (sample)} for one session."""
    universe = sorted(set(universe if universe is not None else _universe(conn)))
    day = str(session)
    got = {r[0]: r[1] for r in conn.execute(
        "SELECT symbol, COUNT(*) FROM intraday_bars WHERE interval_min=? AND ts>=? AND ts<? GROUP BY symbol",
        (int(interval_min), day, str(date.fromisoformat(day) + timedelta(days=1))))}
    have = [s for s in universe if got.get(s)]
    share = len(have) / len(universe) if universe else 1.0
    missing = [s for s in universe if not got.get(s)]
    return {"session": day, "universe": len(universe), "have": len(have), "bars": sum(got.get(s, 0) for s in have),
            "share": round(share, 4), "ok": share >= min_share, "missing": missing[:20], "n_missing": len(missing)}


def _sessions(today, n) -> list:
    from utils.trading_calendar import is_trading_day
    out, d = [], today
    while len(out) < n:
        if is_trading_day(d):
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def run(conn=None, today=None, alert=True) -> dict:
    """Check today's session (once its bars are in) and the recent ones; store, alert newly-found gaps once."""
    from db.schema import get_connection
    from utils.trading_calendar import is_trading_day
    own = conn is None
    conn = conn or get_connection()
    st = settings()
    today = today or date.today()
    try:
        ensure_table(conn)
        first = conn.execute("SELECT MIN(ts) FROM intraday_bars WHERE interval_min=15").fetchone()[0]
        if not first:
            return {"status": "SKIPPED", "reason": "no 15-minute bars stored yet", "rows": 0}
        first = str(first)[:10]
        sessions = [d for d in _sessions(today, LOOKBACK_SESSIONS) if str(d) >= first]
        if not is_trading_day(today):
            sessions = [d for d in sessions if d != today]
        universe = _universe(conn)
        now = datetime.now().isoformat(timespec="seconds")
        results, new_gaps = [], []
        for d in sessions:
            r = check(conn, d, universe, min_share=st["min_share"])
            prev = conn.execute("SELECT ok, alerted FROM data_completeness WHERE session_date=? AND dataset=?",
                                (str(d), DATASET)).fetchone()
            alerted = int(prev[1] or 0) if prev else 0
            if not r["ok"] and not alerted:
                new_gaps.append(r)
            conn.execute("INSERT INTO data_completeness (session_date, dataset, universe, have, bars, share, min_share,"
                         " ok, missing_sample, alerted, checked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                         "ON CONFLICT(session_date, dataset) DO UPDATE SET universe=excluded.universe, "
                         "have=excluded.have, bars=excluded.bars, share=excluded.share, min_share=excluded.min_share, "
                         "ok=excluded.ok, missing_sample=excluded.missing_sample, checked_at=excluded.checked_at",
                         (str(d), DATASET, r["universe"], r["have"], r["bars"], r["share"], st["min_share"],
                          int(r["ok"]), ",".join(r["missing"]), alerted, now))
            results.append(r)
        conn.commit()
        sent = False
        if new_gaps and alert:
            sent = _alert(new_gaps, st["min_share"])
            if sent is not None:
                conn.executemany("UPDATE data_completeness SET alerted=1 WHERE session_date=? AND dataset=?",
                                 [(g["session"], DATASET) for g in new_gaps])
                conn.commit()
        bad = [r for r in results if not r["ok"]]
        today_r = next((r for r in results if r["session"] == str(today)), None)
        return {"status": "SUCCESS" if not bad else "PARTIAL", "rows": len(results),
                "today": today_r, "below": [(r["session"], r["share"]) for r in bad],
                "alerted": [g["session"] for g in new_gaps] if new_gaps and alert else [],
                "error": (f"{len(bad)} session(s) below {st['min_share']:.0%}: "
                          f"{', '.join(r['session'] for r in bad[:5])}{' …' if len(bad) > 5 else ''}") if bad else None}
    finally:
        if own:
            conn.close()


def _alert(gaps, min_share):
    """One message for the sessions newly found below min_share. Returns notify's 'recorded' flag (None on error)."""
    try:
        from alerts.telegram import fmt, notify
        lines = "\n".join(f"  {g['session']}: {g['have']}/{g['universe']} symbols ({g['share']:.0%})"
                          for g in gaps[:12])
        more = f"\n  … and {len(gaps) - 12} more" if len(gaps) > 12 else ""
        cmd = BACKFILL_CMD.format(first=gaps[0]["session"], last=gaps[-1]["session"])
        body = (f"Fewer than {min_share:.0%} of the tracked universe have 15-minute bars for:\n{lines}{more}\n\n"
                f"Intraday scans and the 75-minute ratings read around these sessions. Fill them from Dhan "
                f"(dry run first, without --apply):\n<code>{cmd}</code>")
        return notify(fmt("⚠️", "Intraday bars incomplete", body), category="data_quality", severity="warning",
                      key=f"intraday_completeness:{gaps[0]['session']}:{gaps[-1]['session']}")["recorded"]
    except Exception as e:
        log.warning(f"  completeness alert: {e}")
        return None


def run_scheduled() -> dict:
    """16:15 IST on market days. Reads the database only (and alerts); a no-op on the view-only server."""
    from ops.role import is_viewer
    from utils.trading_calendar import is_trading_day
    if is_viewer():
        return {"status": "SKIPPED", "reason": "view-only copy"}
    if not settings()["enabled"]:
        return {"status": "SKIPPED", "reason": "intraday_completeness.enabled is false"}
    if not is_trading_day(date.today()):
        return {"status": "SKIPPED", "reason": "not a market day"}
    return run()
