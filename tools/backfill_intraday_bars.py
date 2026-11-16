"""
Backfill intraday_bars (15-minute bars) from Dhan's intraday API (owner item 18, 16 Nov 2026).

ATIP has no 15-minute bars at all for the 13 sessions from 6 to 23 Oct 2026 (ATIP was off for the Windows to Mac
move): the backup of 15 Nov 19:15 holds 499 symbols x ~24.8 bars on every session from 28 Sep except those. The daily
check (data/completeness.py) alerts on such a gap; this fills it.

Dry run by default: measures the gap -- sessions x symbols with no bar -- in the database and says what --apply would
fetch. Nothing is written and Dhan is not called. --apply fetches each symbol's bars over the range from Dhan's
intraday API (read-only market data, paced and circuit-broken like every Dhan call in data/dhan.py: ~0.3 s a symbol)
and inserts only bars that are missing; a stored bar is never changed. It needs the Mac's Dhan token, so run it in the
ATIP folder on the Mac. intraday_bars keeps 90 days (db/purge.py), so a session older than that is not worth filling.

Usage (in the ATIP folder):
    .venv/bin/python tools/backfill_intraday_bars.py                         # dry run, 6-23 Oct 2026
    .venv/bin/python tools/backfill_intraday_bars.py --from 2026-10-06 --to 2026-10-23 --apply
Options:
    --db PATH        another database (a copy or a backup); a dry run opens it read-only
    --symbols A,B    only these symbols. Default: the symbols that have bars on the stored sessions nearest the
                     range (before and after it), i.e. the universe the 30-minute bar job was fetching then
    --interval 15    bar size in minutes (Dhan: 1, 5, 15, 25, 60)
    --min-bars N     a symbol-session with fewer than N bars counts as missing (default 1: only a session with no
                     bar). For a session cut short, e.g. 16 Nov 2026 (bars stop at 10:15 IST): --min-bars 20
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import statistics
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_FROM, DEFAULT_TO = "2026-10-06", "2026-10-23"
MAX_DAYS_PER_CALL = 85            # Dhan's intraday API serves at most 90 days a request
NEIGHBOUR_SESSIONS = 3


def _connect(db: Path, write: bool):
    if write:
        c = sqlite3.connect(str(db), timeout=60)
        c.execute("PRAGMA busy_timeout=60000")
        return c
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def sessions_between(start: date, end: date) -> list:
    from utils.trading_calendar import is_trading_day
    out, d = [], start
    while d <= end:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def _by_session(conn, start: date, end: date, interval: int) -> dict:
    """session -> {symbol: bars}"""
    out = {}
    for day, sym, n in conn.execute(
            "SELECT substr(ts,1,10), symbol, COUNT(*) FROM intraday_bars WHERE interval_min=? AND ts>=? AND ts<? "
            "GROUP BY 1, 2", (interval, str(start), str(end + timedelta(days=1)))):
        out.setdefault(day, {})[sym] = n
    return out


def neighbour_universe(conn, start: date, end: date, interval: int):
    """(symbols, median bars per symbol-session) from the stored sessions nearest the range."""
    before = [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(ts,1,10) d FROM intraday_bars WHERE interval_min=? AND ts<? ORDER BY d DESC LIMIT ?",
        (interval, str(start), NEIGHBOUR_SESSIONS))]
    after = [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(ts,1,10) d FROM intraday_bars WHERE interval_min=? AND ts>=? ORDER BY d LIMIT ?",
        (interval, str(end + timedelta(days=1)), NEIGHBOUR_SESSIONS))]
    days = before + after
    if not days:
        return [], None
    syms, counts = set(), []
    for d in days:
        rows = conn.execute("SELECT symbol, COUNT(*) FROM intraday_bars WHERE interval_min=? AND ts>=? AND ts<? "
                            "GROUP BY symbol", (interval, d, str(date.fromisoformat(d) + timedelta(days=1)))).fetchall()
        syms.update(r[0] for r in rows)
        counts += [r[1] for r in rows]
    return sorted(syms), (statistics.median(counts) if counts else None)


def measure(conn, start: date, end: date, interval: int, symbols=None, min_bars: int = 1) -> dict:
    sessions = sessions_between(start, end)
    have = _by_session(conn, start, end, interval)
    universe, per = (sorted(set(symbols)), None) if symbols else neighbour_universe(conn, start, end, interval)
    if per is None:
        _, per = neighbour_universe(conn, start, end, interval)
    rows, need, short = [], {}, 0.0
    for s in sessions:
        got = have.get(str(s), {})
        missing = [x for x in universe if (got.get(x) or 0) < max(1, min_bars)]
        rows.append({"session": str(s), "have": len(universe) - len(missing), "missing": len(missing),
                     "bars": sum(got.values())})
        for x in missing:
            need.setdefault(x, []).append(str(s))
            short += max(0.0, (per or 0) - (got.get(x) or 0))
    pairs = sum(r["missing"] for r in rows)
    return {"sessions": rows, "universe": len(universe), "symbols_to_fetch": sorted(need), "need": need,
            "missing_pairs": pairs, "bars_per_symbol_session": per,
            "est_missing_bars": int(round(short)) if per else None}


def apply(db: Path, need: dict, start: date, end: date, interval: int) -> dict:
    """Fetch and insert the missing bars. Dhan calls are read-only (data/dhan.fetch_historical_intraday)."""
    os.chdir(ROOT)                       # atip_data/config.json and the security list are relative to the repository
    from data.dhan import fetch_historical_intraday, get_dhan_client
    client, _ = get_dhan_client()        # refuses on the view-only server (ops/role.py)
    conn = _connect(db, write=True)
    inserted, empty, now = 0, [], datetime.now()
    try:
        for i, sym in enumerate(sorted(need)):
            wanted = set(need[sym])
            got = 0
            a = start
            while a <= end:
                b = min(end, a + timedelta(days=MAX_DAYS_PER_CALL - 1))
                df = fetch_historical_intraday(sym, a, b + timedelta(days=1), interval, client)
                for r in ([] if df is None or df.empty else df.to_dict("records")):
                    ts = r.get("datetime")
                    if ts is None:
                        continue
                    ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
                    if ts.strftime("%Y-%m-%d") not in wanted:
                        continue
                    cur = conn.execute(
                        "INSERT INTO intraday_bars (symbol, ts, interval_min, open, high, low, close, volume, source, "
                        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol, interval_min, ts) DO NOTHING",
                        (sym, ts.strftime("%Y-%m-%d %H:%M:%S"), int(interval), r.get("open"), r.get("high"),
                         r.get("low"), r.get("close"), int(r.get("volume") or 0), "dhan", now))
                    got += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                a = b + timedelta(days=1)
            inserted += got
            if not got:
                empty.append(sym)
            if (i + 1) % 20 == 0:
                conn.commit()
                print(f"  {i + 1}/{len(need)} symbols, {inserted} bars inserted", flush=True)
        conn.commit()
    finally:
        conn.close()
    return {"inserted": inserted, "symbols": len(need), "no_bars_from_dhan": empty}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from", dest="start", default=DEFAULT_FROM)
    ap.add_argument("--to", dest="end", default=DEFAULT_TO)
    ap.add_argument("--db", default=None)
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--interval", type=int, default=15)
    ap.add_argument("--min-bars", type=int, default=1,
                    help="a symbol-session with fewer bars than this counts as missing (default 1)")
    ap.add_argument("--apply", action="store_true", help="fetch from Dhan and insert (default: dry run)")
    a = ap.parse_args(argv)
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    if end < start:
        ap.error("--to is before --from")
    db = Path(a.db).resolve() if a.db else (ROOT / "atip_data" / "atip.db")
    if not db.exists():
        ap.error(f"no database at {db}")
    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()] if a.symbols else None
    conn = _connect(db, write=False)
    try:
        m = measure(conn, start, end, a.interval, symbols, a.min_bars)
    finally:
        conn.close()
    if not m["universe"]:
        print("No stored sessions near the range to take a universe from: pass --symbols.")
        return 2
    print(f"intraday_bars ({a.interval}-minute) in {db}")
    print(f"Range {start} .. {end}: {len(m['sessions'])} sessions, universe {m['universe']} symbols"
          + (f"; fewer than {a.min_bars} bars counts as missing" if a.min_bars > 1 else ""))
    print(f"{'session':<12}{'have':>6}{'missing':>9}{'bars':>8}")
    for r in m["sessions"]:
        print(f"{r['session']:<12}{r['have']:>6}{r['missing']:>9}{r['bars']:>8}")
    per = m["bars_per_symbol_session"]
    print(f"Gap: {m['missing_pairs']} symbol-sessions"
          + (f", about {m['est_missing_bars']:,} bars at {per:g} bars a symbol-session" if per else ""))
    print(f"--apply would make ~{len(m['symbols_to_fetch'])} Dhan intraday calls "
          f"(~{len(m['symbols_to_fetch']) * 0.3 / 60:.1f} min at Dhan's pacing)")
    if not a.apply:
        print("Dry run: nothing written, Dhan not called. Add --apply to fetch and insert.")
        return 0
    if not m["need"]:
        print("Nothing missing.")
        return 0
    t0 = time.monotonic()
    out = apply(db, m["need"], start, end, a.interval)
    print(f"Inserted {out['inserted']:,} bars for {out['symbols']} symbols in {time.monotonic() - t0:.0f}s"
          + (f"; Dhan returned nothing for {len(out['no_bars_from_dhan'])}: {', '.join(out['no_bars_from_dhan'][:10])}"
             if out["no_bars_from_dhan"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
