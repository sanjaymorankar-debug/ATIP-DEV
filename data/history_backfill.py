"""
Long price history (W39, DP-23): the owner's note "last 7 years of data to be pulled and
stored".

Daily history used to stop at what the first `--dhan-history` run fetched (600 days) and,
worse, the weekly retention purge (db/purge.py) deleted every prices_daily row older than
600 days, so a longer history could not have been kept even if fetched. This module
fetches the missing years and db/purge.py now keeps prices_daily for history_years.

    history_years(cfg=None)        config "history_years" (default 7, bounded 1..25)
    coverage(conn, symbols, end)   earliest stored bar per symbol and what is missing
    plan(conn, symbols, years, end)   the yearly windows still to fetch, walking back from
                                   each symbol's earliest stored bar to end - years
    backfill(years, symbols, end_date, include_indices, dry_run)   fetch + store, resumable
    status(conn)                   last runs and the coverage summary

HOW. One Dhan daily-history call per symbol per window of at most 365 days (Dhan serves
long ranges, but a year at a time keeps every call small, cacheable and restartable),
stored by data.dhan.run_historical_pipeline -- the same code path, corporate-action basis
and upsert as the nightly sync, with basis_date = today because Dhan returns its whole
history adjusted as of the day of the fetch. Benchmark / sector indices go through
data.dhan.sync_index_benchmark_history window by window.

RESUMABLE. A run only asks for what is missing. A symbol whose window came back empty
(listed later than the window) is remembered in history_backfill_symbol and not asked
again for that window; a window the broker REFUSED (e.g. DH-902) is not remembered, so
the next run retries it.

    python -m data.history_backfill --years 7            # or: python main.py --backfill-history
    python -m data.history_backfill --years 7 --dry-run  # the plan only, no API call
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timedelta

log = logging.getLogger("atip.history")

DEFAULT_YEARS = 7
WINDOW_DAYS = 365
TOLERANCE_DAYS = 7          # a first bar within a week of the target start counts as covered
MIN_STUB_DAYS = 30          # a final window shorter than this is merged into the one before


def history_years(cfg: dict | None = None) -> int:
    if cfg is None:
        try:
            from pathlib import Path
            cfg = json.loads((Path("atip_data") / "config.json").read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    try:
        y = int(cfg.get("history_years") or DEFAULT_YEARS)
    except (TypeError, ValueError):
        y = DEFAULT_YEARS
    return max(1, min(25, y))


def target_start(years: int, end: date) -> date:
    return end - timedelta(days=int(round(years * 365.25)))


def ensure_tables(conn):
    from db.schema_w39 import W39_TABLES
    for name in ("history_backfill_run", "history_backfill_symbol"):
        for ddl in W39_TABLES[name]:
            conn.execute(ddl)


def coverage(conn, symbols: list, end: date, years: int) -> list:
    t0 = target_start(years, end)
    first = {}
    ph = ",".join("?" * len(symbols)) if symbols else "''"
    for s, d, n in conn.execute(f"SELECT symbol, MIN(date), COUNT(*) FROM prices_daily WHERE symbol IN ({ph}) "
                                f"GROUP BY symbol", list(symbols)):
        first[s] = (str(d)[:10], n)
    ex = _exhausted(conn)
    out = []
    for s in symbols:
        d, n = first.get(s, (None, 0))
        fd = date.fromisoformat(d) if d else None
        covered = fd is not None and (fd - t0).days <= TOLERANCE_DAYS
        no_older = s in ex and fd is not None and ex[s] >= fd - timedelta(days=TOLERANCE_DAYS)
        out.append({"symbol": s, "first_date": d, "rows": n, "target_start": str(t0),
                    "covered": covered or no_older,
                    "missing_days": 0 if (covered or no_older or fd is None) else (fd - t0).days,
                    "listed_later": bool(no_older and not covered)})
    return out


def _exhausted(conn) -> dict:
    try:
        return {r[0]: date.fromisoformat(str(r[1])[:10]) for r in conn.execute(
            "SELECT symbol, exhausted_before FROM history_backfill_symbol WHERE exhausted_before IS NOT NULL")}
    except Exception:
        return {}


def plan(conn, symbols: list, years: int, end: date) -> list:
    """[(window_start, window_end, [symbols])], newest window first. Windows are fixed year
    blocks counted back from `end` (so symbols share calls); a symbol joins every block that
    overlaps what it is missing: from the day before its first stored bar (or `end` when it
    has none) back to end - years, or to the date before which it is known to have none."""
    t0 = target_start(years, end)
    ex = _exhausted(conn)
    blocks = []
    k = 0
    while True:
        be = end - timedelta(days=k * WINDOW_DAYS)
        if be < t0:
            break
        bs = max(t0, be - timedelta(days=WINDOW_DAYS - 1))
        if (bs - t0).days < MIN_STUB_DAYS:       # a few days left over: fold them into this window
            bs = t0
        blocks.append((bs, be))
        if bs == t0:
            break
        k += 1
    need = {}
    for c in coverage(conn, symbols, end, years):
        if c["covered"]:
            continue
        top = date.fromisoformat(c["first_date"]) - timedelta(days=1) if c["first_date"] else end
        floor = max(t0, ex[c["symbol"]]) if c["symbol"] in ex else t0
        if top < floor:
            continue
        for bs, be in blocks:
            if bs <= top and be >= floor:
                need.setdefault((bs, be), []).append(c["symbol"])
    return [(bs, be, sorted(need[(bs, be)])) for bs, be in blocks if (bs, be) in need]


def backfill(years: int | None = None, symbols: list | None = None, end_date: date | None = None,
             include_indices: bool = True, dry_run: bool = False, fetch=None, index_fetch=None) -> dict:
    """Fetch every missing window. fetch / index_fetch default to data.dhan's
    run_historical_pipeline / sync_index_benchmark_history (injectable for tests)."""
    from db.schema import get_connection, log_job
    years = int(years or history_years())
    end = end_date or date.today()
    conn = get_connection()
    try:
        ensure_tables(conn)
        if symbols is None:
            from data.dhan import get_tracked_symbols
            symbols = get_tracked_symbols(conn)
        symbols = sorted(set(symbols))
        windows = plan(conn, symbols, years, end)
        idx = _index_plan(conn, years, end) if include_indices else []
        out = {"years": years, "target_start": str(target_start(years, end)), "symbols": len(symbols),
               "windows": [{"start": str(a), "end": str(b), "symbols": len(s)} for a, b, s in windows],
               "index_windows": [{"index": k, "start": str(a), "end": str(b)} for k, a, b in idx],
               "calls_planned": sum(len(s) for _, _, s in windows) + len(idx)}
        if dry_run:
            out["status"] = "DRY_RUN"
            return out
        run_id = f"hist_{uuid.uuid4().hex[:12]}"
        conn.execute("INSERT INTO history_backfill_run (run_id,started_at,years,target_start,symbols,windows,status) "
                     "VALUES (?,?,?,?,?,?,?)", (run_id, datetime.now(), years, out["target_start"], len(symbols),
                                                len(windows), "RUNNING"))
        conn.commit()
    finally:
        conn.close()

    if fetch is None or index_fetch is None:
        from data import dhan as DH
        fetch = fetch or DH.run_historical_pipeline
        index_fetch = index_fetch or DH.sync_index_benchmark_history
    rows, refused, results = 0, 0, []
    today = date.today()
    for ws, we, syms in windows:
        before = _first_dates(syms)
        r = fetch(symbols=syms, interval_min=0, start_date=ws, end_date=we, basis_date=today) or {}
        rows += int(r.get("rows") or 0)
        refused += int(r.get("refused") or 0)
        after = _first_dates(syms)
        # nothing older arrived and the broker refused nothing: the symbol has no history in
        # this window (listed later) -- remember it so later runs do not ask again
        if not int(r.get("refused") or 0) and r.get("status") != "FAILED":
            _mark_exhausted({s: (before.get(s) or str(we)) for s in syms if after.get(s) == before.get(s)})
        results.append({"start": str(ws), "end": str(we), "symbols": len(syms), "status": r.get("status"),
                        "rows": r.get("rows"), "refused": r.get("refused")})
    for key, ws, we in idx:
        r = index_fetch(days=(we - ws).days, end_date=we, index_key=key) or {}
        rows += int(r.get("rows") or 0)
        results.append({"index": key, "start": str(ws), "end": str(we), "status": r.get("status"),
                        "rows": r.get("rows")})
    bad = [x for x in results if x.get("status") == "FAILED"]
    status = "SUCCESS" if not bad else ("FAILED" if len(bad) == len(results) else "PARTIAL")
    if not results:
        status = "NO_NEW"
    conn = get_connection()
    try:
        conn.execute("UPDATE history_backfill_run SET finished_at=?, rows_stored=?, status=?, detail_json=? WHERE run_id=?",
                     (datetime.now(), rows, status, json.dumps({"windows": results, "refused": refused}), run_id))
        conn.commit()
    finally:
        conn.close()
    log_job("history_backfill", status, rows, error=f"{len(bad)} window(s) failed" if bad else None)
    out.update({"run_id": run_id, "status": status, "rows": rows, "refused": refused, "results": results})
    return out


def _index_plan(conn, years, end) -> list:
    from data.dhan import INDEX_SERIES_SYMBOLS
    t0 = target_start(years, end)
    out = []
    for key, sym in INDEX_SERIES_SYMBOLS.items():
        r = conn.execute("SELECT MIN(date) FROM prices_daily WHERE symbol=?", (sym,)).fetchone()
        first = date.fromisoformat(str(r[0])[:10]) if r and r[0] else end
        we = first - timedelta(days=1)
        while (we - t0).days > TOLERANCE_DAYS:
            ws = max(t0, we - timedelta(days=WINDOW_DAYS - 1))
            if (ws - t0).days < MIN_STUB_DAYS:
                ws = t0
            out.append((key, ws, we))
            we = ws - timedelta(days=1)
    return out


def _first_dates(symbols) -> dict:
    from db.schema import get_connection
    conn = get_connection()
    try:
        ph = ",".join("?" * len(symbols))
        return {s: str(d)[:10] for s, d in conn.execute(
            f"SELECT symbol, MIN(date) FROM prices_daily WHERE symbol IN ({ph}) GROUP BY symbol", list(symbols))}
    finally:
        conn.close()


def _mark_exhausted(first_by_symbol: dict):
    """{symbol: date}: the broker has no bar for the symbol before that date (it returned
    nothing older for a window that ended there)."""
    if not first_by_symbol:
        return
    from db.schema import get_connection
    conn = get_connection()
    try:
        for s, d in first_by_symbol.items():
            conn.execute("INSERT INTO history_backfill_symbol (symbol, exhausted_before, checked_at, note) VALUES "
                         "(?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET exhausted_before=excluded.exhausted_before, "
                         "checked_at=excluded.checked_at, note=excluded.note",
                         (s, str(d)[:10], datetime.now(), "the broker returned no bar before this date"))
        conn.commit()
    finally:
        conn.close()


def status(conn=None) -> dict:
    from db.schema import get_connection
    own = conn is None
    conn = conn or get_connection()
    try:
        ensure_tables(conn)
        years = history_years()
        from data.dhan import get_tracked_symbols
        try:
            syms = get_tracked_symbols(conn)
        except Exception:
            syms = []
        cov = coverage(conn, syms, date.today(), years) if syms else []
        runs = [dict(r) for r in conn.execute(
            "SELECT run_id, started_at, finished_at, years, target_start, symbols, windows, rows_stored, status "
            "FROM history_backfill_run ORDER BY started_at DESC LIMIT 10")]
        return {"history_years": years, "target_start": str(target_start(years, date.today())),
                "symbols": len(cov), "covered": sum(1 for c in cov if c["covered"]),
                "listed_later": sum(1 for c in cov if c["listed_later"]),
                "missing": [c for c in cov if not c["covered"]][:50], "runs": runs}
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
    ap = argparse.ArgumentParser(description="ATIP long price-history backfill (DP-23)")
    ap.add_argument("--years", type=int, default=None, help=f"years of daily history (default: config history_years "
                                                            f"or {DEFAULT_YEARS})")
    ap.add_argument("--symbols", nargs="+", help="only these symbols (default: the tracked universe)")
    ap.add_argument("--no-indices", action="store_true", help="skip the benchmark / sector indices")
    ap.add_argument("--dry-run", action="store_true", help="print the plan; no API call")
    a = ap.parse_args()
    print(json.dumps(backfill(a.years, a.symbols, include_indices=not a.no_indices, dry_run=a.dry_run),
                     indent=2, default=str))
