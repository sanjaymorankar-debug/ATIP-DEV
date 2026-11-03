"""
Null-EMA gap fill (Task 2 step 4, 2026-10-26).

On 2026-10-26 technical_indicators had ema_50 and sma_200 NULL for 2,772 of 3,279 symbols, 26 of them in the
scored Nifty-500 universe (AETHER, KSB, KIRLOSBROS, SANSERA, CUPID, ...). The indicators are right to be
NULL: those small/mid-caps' prices_daily history starts at the CM Bhavcopy archive ~33 sessions back, and
data/technical.py needs 50 bars for EMA-50 and 200 for SMA-200 (it never fakes one from less). Dhan's
history backfill (data/history_backfill.py) cannot fill them -- all 40 attempts failed with DH-902, the
inactive Data API plan.

The fix fills the history instead: NSE's CM Bhavcopy for a past session carries every listed symbol, so one
download per session repairs every short symbol at once. For the symbols short of MIN_BARS, newest missing
session first, a budget of sessions_per_run downloads per run:

    * only rows that do not exist are inserted (ON CONFLICT DO NOTHING) -- a Dhan or Bhavcopy row already
      stored is never overwritten, so no symbol's basis changes under it
    * rows are stored as source 'bhavcopy_archive', unadjusted, exactly like the daily Bhavcopy rows those
      symbols already have (a corporate action inside the filled window shows as the same step it would
      in the daily feed)
    * each session's outcome is kept in ema_gap_sessions, so a 404 (holiday, archive gap) is not retried
    * then technical_indicators is recomputed for the repaired symbols on the latest scored date

At 25 sessions a run the 26 scored names reach 50 bars (EMA-50) on the first run and 210 (SMA-200, crosses)
in about a week of 06:00 runs.

Config, atip_data/config.json:  "ema_gap": {"enabled": true, "sessions_per_run": 25, "min_bars": 210,
                                             "universe": "tracked"}     ("all" = every prices_daily symbol)
CLI:  python -m data.ema_gap [--sessions 25] [--all]
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULTS = {"enabled": True, "sessions_per_run": 25, "min_bars": 210, "universe": "tracked", "lookback_days": 420}
EMA50_BARS = 50

DDL = """CREATE TABLE IF NOT EXISTS ema_gap_sessions (
    date TEXT PRIMARY KEY, status TEXT, rows_added INTEGER, error TEXT, attempted_at TIMESTAMP)"""


def settings() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(Path("atip_data/config.json").read_text(encoding="utf-8")).get("ema_gap") or {})
    except Exception:
        pass
    return cfg


def short_symbols(conn, universe: list, min_bars: int, upto: str) -> dict:
    """{symbol: bars} for the universe symbols with fewer than min_bars daily bars up to `upto`."""
    if not universe:
        return {}
    out = {}
    for i in range(0, len(universe), 400):
        chunk = universe[i:i + 400]
        marks = ",".join("?" * len(chunk))
        have = {r[0]: int(r[1]) for r in conn.execute(
            f"SELECT symbol, COUNT(*) FROM prices_daily WHERE date<=? AND symbol IN ({marks}) GROUP BY symbol",
            (upto, *chunk)).fetchall()}
        out.update({s: have.get(s, 0) for s in chunk if have.get(s, 0) < min_bars})
    return out


def missing_sessions(conn, symbols: list, upto: date, lookback_days: int) -> list:
    """Past NSE sessions on which at least one of `symbols` has no bar, skipping sessions already attempted --
    the ones that repair the most symbols first, newest first among equals. (Newest-first alone spent the whole
    budget on recent sessions that one recently listed symbol lacked -- HEGAM, listed 2026-09-22 -- and never
    reached the 2026-07-30..09-04 hole every short symbol shares: defect found in the 2026-10-26 run.)"""
    from utils.trading_calendar import is_trading_day
    tried = {r[0] for r in conn.execute("SELECT date FROM ema_gap_sessions").fetchall()}
    out = []
    d = upto
    stop = upto - timedelta(days=lookback_days)
    marks = ",".join("?" * len(symbols))
    while d > stop:
        d -= timedelta(days=1)
        if not is_trading_day(d) or str(d) in tried:
            continue
        n = conn.execute(f"SELECT COUNT(*) FROM prices_daily WHERE date=? AND symbol IN ({marks})",
                         (str(d), *symbols)).fetchone()[0]
        if n < len(symbols):
            out.append((len(symbols) - n, d))
    return [d for _, d in sorted(out, key=lambda x: (-x[0], -x[1].toordinal()))]


ADJ_SOURCE = "bhavcopy_archive_adj"     # an archive row put on the post-event basis (at insert, or by the repair below)


def fill_session(conn, d: date, symbols: set, session, download=None) -> int:
    """Insert d's Bhavcopy rows for `symbols` that are not stored yet. Returns rows inserted."""
    from data.bhavcopy import download_bhavcopy_cm, parse_bhavcopy_cm
    raw = (download or download_bhavcopy_cm)(d, session)
    df = parse_bhavcopy_cm(raw, d)
    df = df[df["symbol"].isin(symbols)]
    from data.corporate_actions import _later
    n = 0
    for r in df.itertuples(index=False):
        # Review 3 Nov 2026 (ingestion H1): the archive file is NSE's RAW price of that day. The stored history is on the
        # post-event basis of every split / bonus already reconciled after d (data/corporate_actions.py), so the row is
        # put on that basis before it goes in -- TDPOWERSYS got 165 pre-split rows at twice the price (2026-10-27..11-02).
        pf, vf = _later(conn, r.symbol, d)
        px = lambda v: None if _f(v) is None else round(_f(v) * pf, 2)
        vol = _f(r.volume)
        cur = conn.execute("INSERT INTO prices_daily(symbol,date,open,high,low,close,volume,series,source) "
                           "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol,date) DO NOTHING",
                           (r.symbol, str(d), px(r.open), px(r.high), px(r.low), px(r.close),
                            None if vol is None else round(vol / vf), r.series,
                            "bhavcopy_archive" if pf == 1.0 and vf == 1.0 else ADJ_SOURCE))
        n += max(0, cur.rowcount or 0)
    return n


def repair_unadjusted(conn, apply: bool = False) -> list:
    """One-off repair (review 3 Nov 2026): archive rows stored RAW before an ex-date whose event had already been
    reconciled -- inserted after reconciled_at (prices_daily.created_at is UTC, reconciled_at IST) with source
    'bhavcopy_archive', which fill_session now only writes for a row that needed no adjustment. A repaired row becomes
    ADJ_SOURCE, so running this again finds nothing and never scales a row twice. (A row-by-row "which basis is it
    nearer" test was tried and rejected: TDPOWERSYS's own price doubled between Nov 2025 and Jul 2026, so the
    nearest trusted row months away misjudged about one row in six.)
    Returns [{symbol, ex_date, factor, rows}]; apply=True scales them onto the post-event basis and commits.

        python -m data.ema_gap --repair            # what it would change
        python -m data.ema_gap --repair --apply    # change it (take a backup first)
    """
    where = ("symbol=? AND date<? AND source='bhavcopy_archive' AND datetime(created_at, '+330 minutes') > ?")
    out = []
    for ev in conn.execute(
            "SELECT symbol, ex_date, MAX(price_factor) AS pf, MIN(kind IN ('SPLIT','BONUS','CONSOLIDATION')) AS share, "
            "MAX(reconciled_at) AS rec FROM corporate_actions WHERE status IN ('adjusted', 'already_adjusted') "
            "AND price_factor IS NOT NULL AND price_factor != 1 AND reconciled_at IS NOT NULL "
            "GROUP BY symbol, ex_date ORDER BY symbol, ex_date DESC").fetchall():
        sym, ex, pf, share, rec = ev[0], str(ev[1]), float(ev[2]), ev[3], ev[4]
        n = conn.execute(f"SELECT COUNT(*) FROM prices_daily WHERE {where}", (sym, ex, rec)).fetchone()[0]
        if not n:
            continue
        out.append({"symbol": sym, "ex_date": ex, "factor": pf, "rows": n})
        if apply:
            # every later event's factor applies to a raw row before it (_later); the newest ex-date first, then
            # the row is ADJ_SOURCE and an older event of the same symbol does not pick it up again
            ids = [r[0] for r in conn.execute(f"SELECT id, date FROM prices_daily WHERE {where}", (sym, ex, rec)).fetchall()]
            for rid in ids:
                d = conn.execute("SELECT date FROM prices_daily WHERE id=?", (rid,)).fetchone()[0]
                from data.corporate_actions import _later
                p, v = _later(conn, sym, d)
                conn.execute("UPDATE prices_daily SET open=ROUND(open*?,2), high=ROUND(high*?,2), low=ROUND(low*?,2), "
                             "close=ROUND(close*?,2), volume=CAST(ROUND(volume/?) AS INTEGER), source=? WHERE id=?",
                             (p, p, p, p, v, ADJ_SOURCE, rid))
    if apply and out:
        conn.commit()
    return out


def _f(v):
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def run(sessions: int | None = None, universe: str | None = None, today: date | None = None,
        download=None, session=None, recompute=True) -> dict:
    """One budgeted pass. `download(d, session) -> DataFrame` replaces the NSE download (tests)."""
    from db.schema import get_connection
    cfg = settings()
    if not cfg.get("enabled", True):
        return {"status": "SKIPPED", "rows": 0, "reason": "ema_gap.enabled is false"}
    budget = int(sessions or cfg["sessions_per_run"])
    conn = get_connection()
    try:
        conn.execute(DDL)
        upto = conn.execute("SELECT MAX(date) FROM ai_scores").fetchone()[0] or str(today or date.today())
        upto = str(upto)[:10]
        if (universe or cfg["universe"]) == "all":
            uni = [r[0] for r in conn.execute("SELECT DISTINCT symbol FROM prices_daily WHERE date=?", (upto,)).fetchall()]
        else:
            uni = [r[0] for r in conn.execute("SELECT DISTINCT symbol FROM ai_scores WHERE date=?", (upto,)).fetchall()]
            if not uni:
                from data.dhan import get_tracked_symbols
                uni = list(get_tracked_symbols(conn))
        short = short_symbols(conn, sorted(set(uni)), int(cfg["min_bars"]), upto)
        if not short:
            return {"status": "SKIPPED", "rows": 0, "reason": f"every symbol has {cfg['min_bars']}+ bars"}
        before_ema50 = sum(1 for n in short.values() if n < EMA50_BARS)
        todo = missing_sessions(conn, sorted(short), datetime.strptime(upto, "%Y-%m-%d").date(),
                                int(cfg["lookback_days"]))[:budget]
        log.info(f"📜 EMA gap: {len(short)} symbols short of {cfg['min_bars']} bars ({before_ema50} below EMA-50's "
                 f"{EMA50_BARS}); filling {len(todo)} session(s) from the Bhavcopy archive")
        if todo and session is None and download is None:
            from data.bhavcopy import get_nse_session
            session = get_nse_session()
        added, failed = 0, 0
        for d in todo:
            try:
                n = fill_session(conn, d, set(short), session, download)
                conn.execute("INSERT OR REPLACE INTO ema_gap_sessions (date,status,rows_added,error,attempted_at) "
                             "VALUES (?,?,?,?,?)", (str(d), "OK", n, None, datetime.now()))
                added += n
            except Exception as e:
                failed += 1
                conn.execute("INSERT OR REPLACE INTO ema_gap_sessions (date,status,rows_added,error,attempted_at) "
                             "VALUES (?,?,?,?,?)", (str(d), "FAILED", 0, str(e)[:300], datetime.now()))
                log.warning(f"  EMA gap {d}: {e}")
            conn.commit()
        after = short_symbols(conn, sorted(short), EMA50_BARS, upto)
    finally:
        conn.close()
    recomputed = 0
    if recompute and added:
        from data.technical import run_technical_pipeline
        r = run_technical_pipeline(trade_date=datetime.strptime(upto, "%Y-%m-%d").date(), symbols=sorted(short))
        recomputed = int((r or {}).get("rows") or 0)
    status = "FAILED" if todo and failed == len(todo) else "PARTIAL" if failed else "SUCCESS"
    return {"status": status, "rows": added, "short_symbols": len(short), "below_ema50_before": before_ema50,
            "below_ema50_after": len(after), "sessions_filled": len(todo) - failed, "sessions_failed": failed,
            "indicators_recomputed": recomputed, "as_of": upto,
            "error": f"{failed}/{len(todo)} sessions could not be downloaded" if failed else None}


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(description="Fill short price histories from the NSE Bhavcopy archive")
    ap.add_argument("--sessions", type=int)
    ap.add_argument("--all", action="store_true", help="every prices_daily symbol, not just the scored universe")
    ap.add_argument("--repair", action="store_true", help="list archive rows stored raw after a split / bonus was reconciled")
    ap.add_argument("--apply", action="store_true", help="with --repair: put them on the adjusted basis")
    a = ap.parse_args(argv)
    if a.repair:
        from db.schema import get_connection
        c = get_connection()
        try:
            print(json.dumps(repair_unadjusted(c, apply=a.apply), indent=2, default=str))
        finally:
            c.close()
        return
    print(json.dumps(run(a.sessions, "all" if a.all else None), indent=2, default=str))


if __name__ == "__main__":
    main()
