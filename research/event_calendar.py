"""
W39 Phase 2 (item 8, EV-01..03) — the macro event calendar: US Fed decisions (FOMC), US CPI, the US
jobs report (payrolls) and RBI policy decisions, so the market pulse can say when a morning's gap
estimate is less reliable than usual.

    KINDS          each kind's release time in its own time zone, and whether it lands before India's
                   open (and so widens the gap estimate) or during the session
    SEED           the published dates (checked 2026-10-07 against two sources each, see below); more
                   come from config.json "event_calendar": {"events": [{"date", "kind", "title"?}]}, or
                   POST /api/market-pulse/events
    exposure()     which NSE session an event first hits, and how: US releases come after India's close
                   (CPI and payrolls at 08:30 New York = 18:00 / 19:00 IST; the Fed at 14:00 New York =
                   23:30 / 00:30 IST), so they hit the NEXT session's open; RBI decides at 10:00 IST, during
                   that day's session
    for_session()  the events that hit a session -- what capture_gift() stores with the morning estimate
    gap_band()     the gap estimate's typical miss (mean absolute error of the stored GIFT estimates),
                   widened on a morning after a US release. The widening is the measured ratio of the miss
                   on event mornings to the miss on other mornings once 10 and 30 of them exist; until
                   then config "default_widen" (1.5, an assumption, stated as such on the page)

New York time follows US daylight saving (second Sunday of March to first Sunday of November), worked out
here so nothing depends on the machine's time-zone database.

Sources for SEED: the Fed's 2026 calendar and its 2027 tentative calendar (press release, 5 Sep 2025); the
BLS 2026 CPI and Employment Situation schedules (January payrolls moved to 11 Feb by the 2026 funding
lapse); RBI's 2026-27 MPC schedule (press release, 23 Mar 2026). The policy decision is the last day of
each meeting. Refresh SEED each year, as utils/trading_calendar.py's holidays are.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

log = logging.getLogger(__name__)

KINDS = {
    "FOMC": {"label": "US Fed decision (FOMC)", "region": "US", "local": time(14, 0), "before_open": True},
    "US_CPI": {"label": "US CPI inflation", "region": "US", "local": time(8, 30), "before_open": True},
    "US_NFP": {"label": "US jobs report (payrolls)", "region": "US", "local": time(8, 30), "before_open": True},
    "RBI_POLICY": {"label": "RBI policy decision", "region": "IN", "local": time(10, 0), "before_open": False},
}

SEED = {
    "FOMC": ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28",
             "2026-12-09",
             # 2027: the Fed's tentative calendar
             "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09", "2027-07-28", "2027-09-15", "2027-10-27",
             "2027-12-08"],
    "US_CPI": ["2026-01-13", "2026-02-13", "2026-03-11", "2026-04-10", "2026-05-12", "2026-06-10", "2026-07-14",
               "2026-08-12", "2026-09-11", "2026-10-14", "2026-11-10", "2026-12-10"],
    "US_NFP": ["2026-01-09", "2026-02-11", "2026-03-06", "2026-04-03", "2026-05-08", "2026-06-05", "2026-07-02",
               "2026-08-07", "2026-09-04", "2026-10-02", "2026-11-06", "2026-12-04"],
    "RBI_POLICY": ["2026-04-08", "2026-06-05", "2026-08-05", "2026-10-07", "2026-12-04", "2027-02-05"],
}

DEFAULTS = {"default_widen": 1.5, "min_event_mornings": 10, "min_normal_mornings": 30, "min_mornings": 10}
NSE_OPEN, NSE_CLOSE = time(9, 15), time(15, 30)

DDL = (
    """CREATE TABLE IF NOT EXISTS macro_event (
        event_date DATE NOT NULL, kind TEXT NOT NULL, title TEXT, source TEXT, created_at TIMESTAMP,
        PRIMARY KEY (event_date, kind))""",
)


def settings() -> dict:
    try:
        from ops.config import load
        raw = load().get("event_calendar") or {}
    except Exception:
        raw = {}
    out = {k: type(v)(raw[k]) if k in raw else v for k, v in DEFAULTS.items()}
    out["events"] = [e for e in (raw.get("events") or []) if isinstance(e, dict)]
    return out


def _d(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


_SEEDED: set = set()


def ensure_tables(conn, seed: bool = True):
    """Create the table and, once per database per process, add SEED and the config's events (existing rows,
    a deleted one included, are left alone)."""
    for d in DDL:
        conn.execute(d)
    import db.schema as S
    key = str(getattr(S, "DB_PATH", ""))
    if seed and key not in _SEEDED:
        _SEEDED.add(key)
        n = 0
        rows = [(d, k, None, "seed") for k, ds in SEED.items() for d in ds]
        rows += [(str(e.get("date"))[:10], str(e.get("kind") or "").upper(), e.get("title"), "config")
                 for e in settings()["events"]]
        for d, k, title, src in rows:
            try:
                _d(d)
            except ValueError:
                log.warning(f"  event calendar: bad date {d!r} in config")
                continue
            if k not in KINDS:
                log.warning(f"  event calendar: unknown kind {k!r} in config")
                continue
            n += conn.execute("INSERT OR IGNORE INTO macro_event (event_date, kind, title, source, created_at) "
                              "VALUES (?,?,?,?,?)", (d, k, title, src, datetime.now())).rowcount or 0
        if n:
            conn.commit()


# ── time zones and sessions ──────────────────────────────────────────────────

def _nth_sunday(year, month, n):
    d = date(year, month, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def us_dst(d: date) -> bool:
    """US daylight saving: from the second Sunday of March to the first Sunday of November."""
    return _nth_sunday(d.year, 3, 2) <= d < _nth_sunday(d.year, 11, 1)


def ist(event_date, kind) -> datetime:
    """The release time in India (naive IST datetime)."""
    d, k = _d(event_date), KINDS[kind]
    local = datetime.combine(d, k["local"])
    if k["region"] == "IN":
        return local
    return local + (timedelta(hours=9, minutes=30) if us_dst(d) else timedelta(hours=10, minutes=30))


def exposure(event_date, kind) -> tuple:
    """(the NSE session the event first hits, "before_open" or "intraday")."""
    from utils.trading_calendar import is_trading_day
    at = ist(event_date, kind)
    d = at.date()
    if is_trading_day(d) and at.time() < NSE_OPEN:
        return d, "before_open"
    if is_trading_day(d) and at.time() < NSE_CLOSE:
        return d, "intraday"
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d, "before_open"


def _event(row) -> dict:
    d, k = _d(row[0]), row[1]
    sess, timing = exposure(d, k)
    at = ist(d, k)
    return {"event_date": str(d), "kind": k, "label": row[2] or KINDS[k]["label"], "source": row[3],
            "time_ist": at.strftime("%H:%M") + ("" if at.date() == d else " (next day)"),
            "session": str(sess), "timing": timing, "widens_gap": timing == "before_open"}


def events(conn, start, end) -> list:
    """Every stored event from start to end (event dates), with its IST time and the session it hits."""
    ensure_tables(conn)
    rows = conn.execute("SELECT event_date, kind, title, source FROM macro_event WHERE event_date>=? AND "
                        "event_date<=? AND (source IS NULL OR source<>'deleted') ORDER BY event_date, kind",
                        (str(_d(start)), str(_d(end)))).fetchall()
    return [_event(r) for r in rows if r[1] in KINDS]


def for_session(conn, session) -> list:
    """The events that hit this NSE session (a US release up to a few days before, or an RBI decision that day)."""
    s = _d(session)
    return [e for e in events(conn, s - timedelta(days=6), s) if e["session"] == str(s)]


def upcoming(conn, today=None, days: int = 14) -> list:
    t = _d(today or date.today())
    return [e for e in events(conn, t - timedelta(days=4), t + timedelta(days=days)) if e["session"] >= str(t)]


def add_event(conn, event_date, kind, title=None) -> dict:
    kind = str(kind or "").upper()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}")
    d = _d(event_date)
    ensure_tables(conn)
    conn.execute("INSERT INTO macro_event (event_date, kind, title, source, created_at) VALUES (?,?,?,?,?) "
                 "ON CONFLICT(event_date, kind) DO UPDATE SET title=excluded.title, source=excluded.source",
                 (str(d), kind, (str(title)[:120] if title else None), "manual", datetime.now()))
    conn.commit()
    return _event((str(d), kind, title, "manual"))


def delete_event(conn, event_date, kind) -> dict:
    """Mark an event deleted (kept as a row, so a seeded date does not come back on the next start)."""
    ensure_tables(conn)
    n = conn.execute("UPDATE macro_event SET source='deleted' WHERE event_date=? AND kind=? AND "
                     "(source IS NULL OR source<>'deleted')", (str(_d(event_date)), str(kind or "").upper())).rowcount
    conn.commit()
    if not n:
        raise LookupError("no such event")
    return {"deleted": n}


# ── the gap estimate's band ──────────────────────────────────────────────────

def _misses(conn) -> tuple:
    """(absolute misses of the GIFT gap estimate on mornings after a US release, on other mornings)."""
    try:
        rows = conn.execute("SELECT date, expected_gap_pct, actual_gap_pct, events FROM market_cue WHERE "
                            "actual_gap_pct IS NOT NULL AND expected_gap_pct IS NOT NULL").fetchall()
    except Exception:
        return [], []
    by_day = {}
    for d, exp, act, ev in rows:                           # one miss per morning: the latest capture wins
        by_day[str(d)[:10]] = (abs(exp - act), ev)
    ev_m, no_m = [], []
    for d, (miss, ev) in by_day.items():
        kinds = [k for k in (ev or "").split(",") if k]
        if ev is None:                                     # stored before the calendar existed: look it up
            kinds = [e["kind"] for e in for_session(conn, d) if e["widens_gap"]]
        (ev_m if any(KINDS.get(k, {}).get("before_open") for k in kinds) else no_m).append(miss)
    return ev_m, no_m


def widen_factor(conn, cfg=None) -> dict:
    cfg = cfg or settings()
    ev_m, no_m = _misses(conn)
    if len(ev_m) >= cfg["min_event_mornings"] and len(no_m) >= cfg["min_normal_mornings"] and sum(no_m) > 0:
        r = (sum(ev_m) / len(ev_m)) / (sum(no_m) / len(no_m))
        return {"factor": round(min(max(r, 1.0), 3.0), 2), "basis": "measured",
                "event_mornings": len(ev_m), "other_mornings": len(no_m)}
    return {"factor": cfg["default_widen"], "basis": "assumed until 10 event and 30 other mornings are stored",
            "event_mornings": len(ev_m), "other_mornings": len(no_m)}


def gap_band(conn, session=None, cfg=None) -> dict:
    """The typical miss of the morning gap estimate, widened when a US release landed overnight."""
    cfg = cfg or settings()
    evs = for_session(conn, session or date.today())
    widen = [e for e in evs if e["widens_gap"]]
    ev_m, no_m = _misses(conn)
    base = no_m if len(no_m) >= cfg["min_mornings"] else ev_m + no_m
    typical = round(sum(base) / len(base), 3) if len(base) >= cfg["min_mornings"] else None
    w = widen_factor(conn, cfg) if widen else {"factor": 1.0, "basis": "no US release overnight"}
    return {"events": evs, "typical_miss_pct": typical, "widen": w["factor"], "widen_basis": w["basis"],
            "band_pct": round(typical * w["factor"], 3) if typical is not None else None,
            "mornings": len(ev_m) + len(no_m),
            "note": None if typical is not None else f"needs {cfg['min_mornings']} evaluated mornings for a band"}


def main(argv=None):
    import argparse
    import json
    from db.schema import get_connection
    p = argparse.ArgumentParser(description="Macro event calendar (FOMC, US CPI, payrolls, RBI)")
    p.add_argument("cmd", choices=("upcoming", "today", "add"), nargs="?", default="upcoming")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--date")
    p.add_argument("--kind")
    a = p.parse_args(argv)
    conn = get_connection()
    try:
        if a.cmd == "add":
            out = add_event(conn, a.date, a.kind)
        elif a.cmd == "today":
            out = gap_band(conn)
        else:
            out = upcoming(conn, days=a.days)
        print(json.dumps(out, indent=2, default=str))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
