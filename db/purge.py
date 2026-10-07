"""ATIP — Database Retention / Purge Utility

Deletes rows older than a configurable number of calendar days, per table.
Three retention tiers by default:

  HISTORY (default 7 years, W39) — the record ATIP cannot rebuild: daily
        prices (the 7-year history data/history_backfill.py downloads), the
        signals and predictions whose hit rate is tracked, ownership and deal
        data. These used to sit in LONG and were deleted after 600 days with
        no archive, which made a multi-year history impossible to keep.

  LONG  (default 600 days) — symbol/date-keyed tables that feed technical
        indicators, scoring, and backtesting: prices_daily, technical_
        indicators, ai_scores, predictions, accuracy_tracker, etc. These
        need real depth — SMA-200, golden/death-cross, and the 52-week
        fib levels all read a trailing ~252-trading-day window. Everything
        here can be recomputed from the HISTORY tier.

  SHORT (default 90 days)  — high-frequency / operational tables that are
        only ever read for "recent" context: intraday index snapshots
        (written every ~15s by the WS feed during market hours), live
        quote/tick caches, and the pipeline run log. Keeping these at
        600 days would balloon the database for no analytical benefit —
        nothing reads a live tick from 18 months ago.

Overrides, atip_data/config.json:
    "retention": {"history_days": 2557, "long_days": 600, "short_days": 90}
history_days 0 (or null) keeps the HISTORY tier forever.

Run standalone:
    python -m db.purge                  # dry-run — reports what WOULD be removed
    python -m db.purge --apply           # actually delete + VACUUM
    python -m db.purge --apply --days 600 --short-days 90 --history-days 2557
"""
import argparse, logging
from datetime import date, timedelta
from db.schema import get_connection, log_job

log = logging.getLogger(__name__)

# table -> its date/timestamp column
HISTORY_RETENTION_TABLES = {             # W39: moved out of LONG
    "prices_daily":         "date",
    "ai_scores":            "date",
    "predictions":          "pred_date",
    "accuracy_tracker":     "pred_date",
    "institutional_data":   "date",
    "fii_dii_market":       "date",
    "bulk_deals":           "date",
    "portfolio_holdings":   "date",
    "fo_underlying_daily":  "date",      # W27
    "corporate_announcement": "broadcast_at",   # W28b (NS-06)
}

LONG_RETENTION_TABLES = {
    "technical_indicators": "date",
    "market_health":        "date",
    "technical_ext":        "date",      # W21
    "sector_breadth":       "date",      # W21
    "score_components":     "date",      # W22
    "ml_anomaly":           "as_of",     # W24
    "intraday_scan_hit":    "session",   # W28
    "news_symbol_score":    "date",      # W28b (NS-05)
}

SHORT_RETENTION_TABLES = {
    "index_levels":   "date",
    "global_markets": "date",
    "news_articles":  "fetched_at",
    "pipeline_log":   "run_date",
    "live_quotes":    "timestamp",
    "live_ticks":     "received_at",
    "intraday_bars":  "ts",              # W21 (DP-03)
    "broker_health_check": "checked_at",  # W29
    "live_pnl_snapshot": "ts",            # W29
    "latency_rollup": "minute",          # W34 (EX-15)
    "oms_event_delivery": "at",          # W34 (EX-16); the outbox itself is kept (audit)
    "order_book_snapshot": "ts",         # W35 (DP-05); history stays in lake "depth"
    "option_chain_snapshot": "ts",       # W35 (DP-08); history stays in lake "option_chain"
    "fo_contract_daily": "date",         # W35 (DP-08); every contract stays in lake "fo_bhavcopy"
}

HISTORY_YEARS = 7
DEFAULT_HISTORY_DAYS = 2557              # 7 x 365.25: the window history_backfill fills
DEFAULT_LONG_DAYS  = 600
DEFAULT_SHORT_DAYS = 90


def retention_settings() -> dict:
    """The three windows, with config.json "retention" overrides; history_days None = keep forever."""
    out = {"history_days": DEFAULT_HISTORY_DAYS, "long_days": DEFAULT_LONG_DAYS,
           "short_days": DEFAULT_SHORT_DAYS}
    try:
        from ops.config import load
        raw = load().get("retention") or {}
    except Exception:
        raw = {}
    for key in out:
        if key in raw:
            try:
                out[key] = int(raw[key]) if raw[key] else (None if key == "history_days" else out[key])
            except (TypeError, ValueError):
                pass
    if out["history_days"] is not None:
        # never below LONG: the derived tables would outlive the prices they were computed from
        out["history_days"] = max(out["history_days"], out["long_days"])
    return out


def _purge_table(conn, table, date_col, cutoff, dry_run):
    try:
        count = conn.execute(f"SELECT COUNT(*) c FROM {table} WHERE {date_col} < ?", (cutoff,)).fetchone()["c"]
    except Exception as e:
        log.warning(f"  purge {table}: {e}")
        return f"error: {e}"
    if count and not dry_run:
        conn.execute(f"DELETE FROM {table} WHERE {date_col} < ?", (cutoff,))
    return count


def purge_old_data(long_days: int = None, short_days: int = None,
                    dry_run: bool = True, history_days: int = None) -> dict:
    """
    Delete rows older than the retention window for each table.
    Returns {table: rows_removed_or_error}.
    dry_run=True (default) deletes nothing — just reports what would go,
    so this is safe to call from a schedule for visibility before you
    ever pass --apply / dry_run=False.
    Windows left as None come from retention_settings() (config.json "retention").
    """
    cfg = retention_settings()
    long_days = long_days or cfg["long_days"]
    short_days = short_days or cfg["short_days"]
    history_days = history_days or cfg["history_days"]
    long_cutoff  = str(date.today() - timedelta(days=long_days))
    short_cutoff = str(date.today() - timedelta(days=short_days))

    conn = get_connection()
    results = {}
    try:
        if history_days:
            history_cutoff = str(date.today() - timedelta(days=max(history_days, long_days)))
            for table, col in HISTORY_RETENTION_TABLES.items():
                results[table] = _purge_table(conn, table, col, history_cutoff, dry_run)
        for table, col in LONG_RETENTION_TABLES.items():
            results[table] = _purge_table(conn, table, col, long_cutoff, dry_run)
        for table, col in SHORT_RETENTION_TABLES.items():
            results[table] = _purge_table(conn, table, col, short_cutoff, dry_run)

        total = sum(v for v in results.values() if isinstance(v, int))
        if not dry_run:
            conn.commit()
            conn.execute("VACUUM")  # reclaim disk space after a real delete
        log.info(f"  {'Would remove' if dry_run else 'Removed'} {total} rows total "
                 f"(history {f'{history_days}d' if history_days else 'kept forever'}, "
                 f"long-retention cutoff {long_cutoff} / {long_days}d, "
                 f"short-retention cutoff {short_cutoff} / {short_days}d)")
        log_job("db_purge", "DRY_RUN" if dry_run else "SUCCESS", total)
    except Exception as e:
        conn.rollback()
        log.error(f"  ✗ Purge failed: {e}")
        log_job("db_purge", "FAILED", 0, error=e)
        raise
    finally:
        conn.close()
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(description="Purge old ATIP data by retention window")
    ap.add_argument("--apply", action="store_true",
                     help="Actually delete rows (default is a dry-run report only)")
    ap.add_argument("--history-days", type=int, default=None,
                     help=f"Retention days for prices/signals history (default: config, else {DEFAULT_HISTORY_DAYS})")
    ap.add_argument("--days", type=int, default=None,
                     help=f"Retention days for derived daily tables (default: config, else {DEFAULT_LONG_DAYS})")
    ap.add_argument("--short-days", type=int, default=None,
                     help=f"Retention days for high-frequency/operational tables (default: config, else {DEFAULT_SHORT_DAYS})")
    args = ap.parse_args()

    res = purge_old_data(long_days=args.days, short_days=args.short_days, dry_run=not args.apply,
                         history_days=args.history_days)
    print(f"\n{'DRY RUN — nothing deleted, add --apply to actually purge' if not args.apply else 'PURGE COMPLETE'}")
    for table, n in res.items():
        print(f"  {table:<22} {n}")
