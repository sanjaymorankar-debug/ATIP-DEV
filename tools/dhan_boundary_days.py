"""
Find (and with --apply fill) the daily bars the 7-year history backfill skipped at its window edges (16 Nov 2026).

Dhan's daily-history API treats toDate as EXCLUSIVE. data/history_backfill.py asked for each 365-day window as
(start, end), so every window that ended on a session came back without that session's bar -- and the next (older)
window ends the day before this one starts, so nothing ever asked for it again. On the 15 Nov 2026 19:15 IST backup:
1,499 symbol-days in 331 symbols, about 300 symbols each on 3 Mar 2020, 2021, 2022, 2023 and the Saturday session of
2 Mar 2024. The backfill now asks for end + 1 (data.dhan.fetch_daily_through); this repairs what is already stored.

Which days: Dhan's cache records every window asked for (atip_data/raw/dhan/{SYMBOL}_{from}_{to}_daily.json; `to`
is the day never fetched). A `to` day is listed when it lies inside the symbol's stored history, has no bar, and was
a session -- for a year utils/trading_calendar.py lists, is_trading_day(); before that, some stored symbol has a bar
that day (2 Mar 2024 was a Saturday session). A weekday before the calendar that no stored symbol has a bar for is
listed as unconfirmed: --apply asks Dhan for it too, and an empty answer -- or DH-907 "no data present", which is how
Dhan answered for Diwali Balipratipada 2023-11-14 on 16 Nov 2026 -- means there was no session; it stays listed.

    python -m tools.dhan_boundary_days                               # dry run on atip_data/atip.db, read-only
    python -m tools.dhan_boundary_days --db atip_data/backups/X.db   # dry run on a copy (opened immutable)
    python -m tools.dhan_boundary_days --list                        # every symbol-day, not just the dates
    python -m tools.dhan_boundary_days --apply [--symbols TATASTEEL] # after `python -m ops backup --kind pre-release`
    python -m tools.dhan_boundary_days --apply --db scratch.db       # fill a copy instead

--apply asks Dhan for each day on its own (day, day + 1 -- read-only market data, never an order call) and stores it
through data.dhan.store_daily_bars on today's corporate-action basis, as the backfill does (source 'dhan'). Safe to
run twice: a filled day has a bar and is no longer listed.
"""

import argparse
import re
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

NO_DATA = "DH-907"              # Dhan: "incorrect parameters or no data present"
CACHE_NAME = re.compile(r"^(?P<sym>.+)_(?P<frm>\d{4}-\d{2}-\d{2})_(?P<to>\d{4}-\d{2}-\d{2})_daily\.json$")


def window_ends(cache_dir) -> dict:
    """symbol -> the `to` dates (YYYY-MM-DD) of its cached daily windows."""
    out = defaultdict(set)
    for f in Path(cache_dir).glob("*_daily.json"):
        m = CACHE_NAME.match(f.name)
        if m:
            out[m["sym"]].add(m["to"])
    return out


def _session(day: date, traded: set):
    """True: a session. None: a weekday before the holiday calendar that no stored symbol traded. False: not one."""
    from utils.trading_calendar import NSE_HOLIDAYS, is_trading_day
    if day.year >= min(d.year for d in NSE_HOLIDAYS):
        return is_trading_day(day)
    if str(day) in traded:
        return True
    return None if day.weekday() < 5 else False


def plan(conn, cache_dir, symbols=None) -> list:
    """[{symbol, date, confirmed}] oldest first within each symbol: window ends with no bar inside the history."""
    ends = window_ends(cache_dir)
    if symbols:
        wanted = {s.upper() for s in symbols}
        ends = {s: v for s, v in ends.items() if s.upper() in wanted}
    traded = {str(r[0])[:10] for r in conn.execute("SELECT DISTINCT date FROM prices_daily")}
    out = []
    for sym in sorted(ends):
        lo, hi = conn.execute("SELECT MIN(date), MAX(date) FROM prices_daily WHERE symbol=?", (sym,)).fetchone()
        if lo is None:
            continue
        inside = sorted(d for d in ends[sym] if str(lo)[:10] <= d <= str(hi)[:10])
        if not inside:
            continue
        have = {str(r[0])[:10] for r in conn.execute(
            f"SELECT date FROM prices_daily WHERE symbol=? AND date IN ({','.join('?' * len(inside))})",
            (sym, *inside))}
        for d in inside:
            if d in have:
                continue
            ok = _session(date.fromisoformat(d), traded)
            if ok is not False:
                out.append({"symbol": sym, "date": d, "confirmed": bool(ok)})
    return out


def fill(conn, todo, dhan, events=None) -> dict:
    """Ask Dhan for each listed day on its own and store what it returns. Commits per symbol."""
    from data.dhan import fetch_daily_through, store_daily_bars
    events = events or {}
    res = {"filled": 0, "held": 0, "no_bar": [], "refused": defaultdict(int)}
    last = None
    for t in todo:
        if last and t["symbol"] != last:
            conn.commit()
        last = t["symbol"]
        day = date.fromisoformat(t["date"])
        df = fetch_daily_through(t["symbol"], day, day, dhan)
        if df.empty:
            err = df.attrs.get("dhan_error")
            if err and not (err == NO_DATA and not t["confirmed"]):
                res["refused"][str(err)] += 1
            else:
                res["no_bar"].append((t["symbol"], t["date"]))
            continue
        n, held, _ = store_daily_bars(conn, t["symbol"], df, day, day, events.get(t["symbol"]),
                                      basis_date=date.today())
        res["filled"] += n
        res["held"] += held
    conn.commit()
    res["refused"] = dict(res["refused"])
    return res


def _open(db, write: bool):
    """The named copy (immutable for a dry run), or the live database (read-only for a dry run)."""
    from db.schema import DB_PATH, get_connection
    if db and write:
        conn = sqlite3.connect(str(db))
    elif db:
        conn = sqlite3.connect(f"file:{Path(db).resolve()}?mode=ro&immutable=1", uri=True)
    elif write:
        return get_connection()
    else:
        conn = sqlite3.connect(f"file:{Path(DB_PATH).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv=None) -> int:
    from data import dhan as D
    ap = argparse.ArgumentParser(description="List (and with --apply fill) the bars the history backfill skipped "
                                             "at its window edges: Dhan's toDate is exclusive")
    ap.add_argument("--db", help="a copy of the database (default: the live one, atip_data/atip.db)")
    ap.add_argument("--cache-dir", default=str(D.CACHE_DIR), help="Dhan's window cache (default: %(default)s)")
    ap.add_argument("--symbols", nargs="+")
    ap.add_argument("--list", action="store_true", help="print every symbol-day")
    ap.add_argument("--apply", action="store_true", help="ask Dhan for each day and store it (default: dry run)")
    a = ap.parse_args(argv)
    conn = _open(a.db, a.apply)
    try:
        todo = plan(conn, a.cache_dir, a.symbols)
        unconfirmed = [t for t in todo if not t["confirmed"]]
        print(f"{len(todo)} symbol-day(s) missing at a window edge in {len({t['symbol'] for t in todo})} symbol(s)"
              f" ({len(unconfirmed)} unconfirmed weekday(s))" + ("" if a.apply else " -- dry run, nothing written"))
        by_day, confirmed = defaultdict(list), {}
        for t in todo:
            by_day[t["date"]].append(t["symbol"])
            confirmed[t["date"]] = t["confirmed"]
        for d in sorted(by_day):
            syms = by_day[d]
            print(f"  {d} {date.fromisoformat(d):%a} {len(syms):>4} {'session' if confirmed[d] else 'unconfirmed'}  "
                  f"{', '.join(syms[:6])}{' ...' if len(syms) > 6 else ''}")
        if a.list:
            for t in todo:
                print(f"    {t['symbol']} {t['date']}")
        if a.apply and todo:
            if not D.HAS_DHAN:
                print("  dhanhq is not installed")
                return 1
            client, _ = D.get_dhan_client()
            try:
                from data.corporate_actions import basis_events
                events = basis_events(conn)
            except Exception as e:
                print(f"  corporate-action basis unavailable, storing Dhan's bars as-is: {e}")
                events = {}
            t0 = time.monotonic()
            res = fill(conn, todo, client, events)
            print(f"  filled {res['filled']}, held for corporate actions {res['held']}, no bar from Dhan "
                  f"{len(res['no_bar'])}, refused {res['refused'] or 0} ({time.monotonic() - t0:.0f} s)")
            for sym, d in res["no_bar"][:20]:
                print(f"    no bar: {sym} {d}")
            return 1 if res["refused"] else 0
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
