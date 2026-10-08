"""
W39 — seven years of daily price history (DP-11).

ATIP's prices_daily began on 2025-01-27 and run_historical_pipeline only ever asks
Dhan for a recent window, so there was no multi-year history for the stock view,
backtests, valuation bands or factor research. This fills it in, backwards from each
symbol's earliest stored bar to "today minus N years", one window at a time:

    * resumable    each run picks up where the last stopped (the earliest stored bar is
                   the cursor), so a budget of N symbols a night finishes the Nifty 500
                   in a couple of weeks without a long burst against Dhan
    * listing-aware  a window that comes back empty, or starts well inside itself, is
                   where the stock's history begins: the symbol is marked exhausted
                   and never asked again for that window
    * same basis   bars go through data.dhan.store_daily_bars, the corporate-action
                   basis the daily pipeline uses
    * kept         db/purge.py keeps prices_daily for 7 years (HISTORY tier), the
                   window this fills

Table: prices_daily_backfill (one row per symbol: target, earliest bar, exhausted,
consecutive failures, last error).

Config, atip_data/config.json:
    "history": {"backfill_enabled": true, "years": 7, "symbols_per_run": 40,
                "chunk_days": 365, "pause_seconds": 0.4}

CLI:
    python -m data.history_backfill status
    python -m data.history_backfill run [--symbols RELIANCE TCS] [--max 40] [--years 7]
Scheduled nightly at 22:20 by pipeline/scheduler.py (_schedule_w39_jobs).
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

DEFAULTS = {"backfill_enabled": True, "years": 7, "symbols_per_run": 40, "chunk_days": 365,
            "pause_seconds": 0.4}
LISTING_TOLERANCE_DAYS = 10     # longer than any NSE closure: a gap this big at a window's start is the listing
MAX_FAILURES = 3                # consecutive refusals for one symbol before it is parked

DDL = """CREATE TABLE IF NOT EXISTS prices_daily_backfill (
    symbol       TEXT PRIMARY KEY,
    target_start TEXT NOT NULL,
    earliest     TEXT,
    exhausted    INTEGER NOT NULL DEFAULT 0,
    failures     INTEGER NOT NULL DEFAULT 0,
    rows_added   INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    updated_at   TIMESTAMP
)"""


def settings() -> dict:
    try:
        from ops.config import load
        raw = load().get("history") or {}
    except Exception:
        raw = {}
    out = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}
    out["backfill_enabled"] = out.get("backfill_enabled") is not False
    for k in ("years", "symbols_per_run", "chunk_days"):
        try:
            out[k] = max(1, int(out[k]))
        except (TypeError, ValueError):
            out[k] = DEFAULTS[k]
    try:
        out["pause_seconds"] = max(0.0, float(out["pause_seconds"]))
    except (TypeError, ValueError):
        out["pause_seconds"] = DEFAULTS["pause_seconds"]
    return out


def ensure_tables(conn):
    conn.execute(DDL)


def target_start(years: int, today: date | None = None) -> date:
    return (today or date.today()) - timedelta(days=round(years * 365.25))


def _d(v) -> date | None:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def _earliest(conn, symbol) -> date | None:
    row = conn.execute("SELECT MIN(date) FROM prices_daily WHERE symbol=?", (symbol,)).fetchone()
    return _d(row[0]) if row else None


def _state(conn) -> dict:
    out = {}
    for r in conn.execute("SELECT symbol, target_start, earliest, exhausted, failures, rows_added, last_error "
                          "FROM prices_daily_backfill"):
        out[r[0]] = {"target_start": _d(r[1]), "earliest": _d(r[2]), "exhausted": bool(r[3]),
                     "failures": int(r[4] or 0), "rows_added": int(r[5] or 0), "last_error": r[6]}
    return out


def needs_backfill(earliest: date | None, state: dict | None, target: date) -> bool:
    """True while the symbol's stored history does not reach `target` and nothing says it cannot."""
    if earliest is not None and earliest <= target + timedelta(days=LISTING_TOLERANCE_DAYS):
        return False
    if state:
        if state["exhausted"] and state["target_start"] and state["target_start"] <= target:
            return False                      # already walked back to an older target: listed later
        if state["failures"] >= MAX_FAILURES:
            return False                      # parked; `status` shows the error, `run --symbols` retries
    return True


def _save(conn, symbol, target, earliest, exhausted, failures, added, error):
    conn.execute("""INSERT INTO prices_daily_backfill
                        (symbol, target_start, earliest, exhausted, failures, rows_added, last_error, updated_at)
                    VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(symbol) DO UPDATE SET target_start=excluded.target_start,
                        earliest=excluded.earliest, exhausted=excluded.exhausted, failures=excluded.failures,
                        rows_added=prices_daily_backfill.rows_added + excluded.rows_added,
                        last_error=excluded.last_error, updated_at=excluded.updated_at""",
                 (symbol, str(target), str(earliest) if earliest else None, int(exhausted), failures, added,
                  error, datetime.now()))


def backfill_symbol(conn, dhan, symbol, target, chunk_days, events=None, pause=0.0, prior_failures=0) -> dict:
    """Walk one symbol back to `target`, a window at a time. Commits after each window."""
    from data.dhan import fetch_historical_daily, store_daily_bars
    earliest = _earliest(conn, symbol)
    end = (earliest - timedelta(days=1)) if earliest else date.today()
    added = windows = 0
    exhausted = False
    error = None
    while end >= target:
        start = max(target, end - timedelta(days=chunk_days - 1))
        df = fetch_historical_daily(symbol, start, end, dhan)
        windows += 1
        if df.empty:
            if df.attrs.get("dhan_error"):
                error = str(df.attrs["dhan_error"])
            else:
                exhausted = True              # nothing at all in the window: before the listing
            break
        # basis as of today, the day of the fetch -- not the window's end, years ago (W39b merge fix)
        n, _held, _shifted = store_daily_bars(conn, symbol, df, start, end, events, basis_date=date.today())
        conn.commit()
        added += n
        first = min(_d(x) for x in df["date"] if x is not None)
        if first > start + timedelta(days=LISTING_TOLERANCE_DAYS):
            exhausted = True                  # the history starts inside this window
            break
        end = start - timedelta(days=1)
        if pause:
            time.sleep(pause)
    earliest = _earliest(conn, symbol)
    failures = prior_failures + 1 if error else 0
    _save(conn, symbol, target, earliest, exhausted, failures, added, error)
    conn.commit()
    return {"symbol": symbol, "rows": added, "windows": windows, "earliest": str(earliest) if earliest else None,
            "exhausted": exhausted, "error": error}


def run_backfill(symbols: list | None = None, years: int | None = None, max_symbols: int | None = None) -> dict:
    """One budgeted pass. Explicit `symbols` are retried even when parked after failures."""
    from db.schema import get_connection
    cfg = settings()
    years = years or cfg["years"]
    target = target_start(years)
    try:
        from data import dhan as D
        if not D.HAS_DHAN:
            return {"status": "FAILED", "error": "pip install dhanhq"}
        client, _ = D.get_dhan_client()
    except RuntimeError as e:
        return {"status": "FAILED", "error": str(e)}

    conn = get_connection()
    results, errors, todo = [], 0, []
    try:
        ensure_tables(conn)
        forced = bool(symbols)
        universe = sorted(set(symbols or D.get_tracked_symbols(conn)))
        state = _state(conn)
        if forced:
            todo = [s for s in universe if needs_backfill(_earliest(conn, s), None, target)]
        else:
            todo = [s for s in universe if needs_backfill(_earliest(conn, s), state.get(s), target)]
        budget = max_symbols or cfg["symbols_per_run"]
        basis = {}
        try:
            from data.corporate_actions import basis_events
            basis = basis_events(conn)
        except Exception as e:
            log.warning(f"  Corporate-action basis unavailable, storing Dhan's bars as-is: {e}")
        log.info(f"📜 History backfill to {target} ({years}y): {len(todo)} of {len(universe)} symbols short, "
                 f"doing {min(budget, len(todo))}")
        for sym in todo[:budget]:
            prior = (state.get(sym) or {}).get("failures", 0) if not forced else 0
            r = backfill_symbol(conn, client, sym, target, cfg["chunk_days"], basis.get(sym),
                                cfg["pause_seconds"], prior)
            results.append(r)
            errors += bool(r["error"])
    finally:
        conn.close()
    rows = sum(r["rows"] for r in results)
    if not todo:
        return {"status": "SKIPPED", "reason": f"history complete back to {target}", "rows": 0,
                "target": str(target), "symbols": 0, "errors": 0, "remaining": 0, "results": []}
    status = "FAILED" if results and errors == len(results) else "PARTIAL" if errors else "SUCCESS"
    return {"status": status, "target": str(target), "symbols": len(results), "rows": rows, "errors": errors,
            "remaining": max(0, len(todo) - len(results)), "results": results,
            "error": f"{errors}/{len(results)} symbols refused" if errors else None}


def coverage(conn, years: int | None = None, symbols: list | None = None) -> dict:
    """How much of the tracked universe already reaches back `years`."""
    years = years or settings()["years"]
    target = target_start(years)
    ensure_tables(conn)
    if symbols is None:
        try:
            from data.dhan import get_tracked_symbols
            symbols = get_tracked_symbols(conn)
        except Exception:
            symbols = [r[0] for r in conn.execute("SELECT DISTINCT symbol FROM prices_daily_backfill")]
    state = _state(conn)
    complete = listed_later = parked = pending = 0
    oldest = None
    for s in symbols:
        e = _earliest(conn, s)
        st = state.get(s)
        if e and (oldest is None or e < oldest):
            oldest = e
        if e is not None and e <= target + timedelta(days=LISTING_TOLERANCE_DAYS):
            complete += 1
        elif st and st["exhausted"] and st["target_start"] and st["target_start"] <= target:
            listed_later += 1
        elif st and st["failures"] >= MAX_FAILURES:
            parked += 1
        else:
            pending += 1
    return {"years": years, "target": str(target), "symbols": len(symbols), "complete": complete,
            "listed_later": listed_later, "parked": parked, "pending": pending,
            "oldest_bar": str(oldest) if oldest else None,
            "parked_errors": {s: st["last_error"] for s, st in state.items() if st["failures"] >= MAX_FAILURES}}


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(description="Seven-year daily price history backfill (W39)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    r = sub.add_parser("run")
    r.add_argument("--symbols", nargs="+")
    r.add_argument("--max", type=int)
    r.add_argument("--years", type=int)
    a = ap.parse_args(argv)
    if a.cmd == "status":
        from db.schema import get_connection
        conn = get_connection()
        try:
            print(coverage(conn))
        finally:
            conn.close()
    else:
        out = run_backfill(a.symbols, a.years, a.max)
        print({k: v for k, v in out.items() if k != "results"})


if __name__ == "__main__":
    main()
