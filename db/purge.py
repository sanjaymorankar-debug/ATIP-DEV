"""ATIP — Database Retention / Purge Utility

Deletes rows older than a configurable number of calendar days, per table.
Two retention tiers by default:

  LONG  (default 600 days) — symbol/date-keyed tables that feed technical
        indicators, scoring, and backtesting: prices_daily, technical_
        indicators, ai_scores, predictions, accuracy_tracker, etc. These
        need real depth — SMA-200, golden/death-cross, and the 52-week
        fib levels all read a trailing ~252-trading-day window, and
        backtesting/accuracy trend reports want history beyond that.

  SHORT (default 90 days)  — high-frequency / operational tables that are
        only ever read for "recent" context: intraday index snapshots
        (written every ~15s by the WS feed during market hours), live
        quote/tick caches, and the pipeline run log. Keeping these at
        600 days would balloon the database for no analytical benefit —
        nothing reads a live tick from 18 months ago.

Run standalone:
    python -m db.purge                  # dry-run — reports what WOULD be removed
    python -m db.purge --apply           # actually delete + VACUUM
    python -m db.purge --apply --days 600 --short-days 90
"""
import argparse, logging
from datetime import date, timedelta
from db.schema import get_connection, log_job

log = logging.getLogger(__name__)

# table -> its date/timestamp column
LONG_RETENTION_TABLES = {
    "prices_daily":         "date",
    "technical_indicators": "date",
    "ai_scores":            "date",
    "institutional_data":   "date",
    "fii_dii_market":       "date",
    "market_health":        "date",
    "predictions":          "pred_date",
    "accuracy_tracker":     "pred_date",
    "portfolio_holdings":   "date",
    "bulk_deals":           "date",
}

SHORT_RETENTION_TABLES = {
    "index_levels":   "date",
    "global_markets": "date",
    "news_articles":  "fetched_at",
    "pipeline_log":   "run_date",
    "live_quotes":    "timestamp",
    "live_ticks":     "received_at",
}

DEFAULT_LONG_DAYS  = 600
DEFAULT_SHORT_DAYS = 90


def _purge_table(conn, table, date_col, cutoff, dry_run):
    try:
        count = conn.execute(f"SELECT COUNT(*) c FROM {table} WHERE {date_col} < ?", (cutoff,)).fetchone()["c"]
    except Exception as e:
        log.warning(f"  purge {table}: {e}")
        return f"error: {e}"
    if count and not dry_run:
        conn.execute(f"DELETE FROM {table} WHERE {date_col} < ?", (cutoff,))
    return count


def purge_old_data(long_days: int = DEFAULT_LONG_DAYS, short_days: int = DEFAULT_SHORT_DAYS,
                    dry_run: bool = True) -> dict:
    """
    Delete rows older than the retention window for each table.
    Returns {table: rows_removed_or_error}.
    dry_run=True (default) deletes nothing — just reports what would go,
    so this is safe to call from a schedule for visibility before you
    ever pass --apply / dry_run=False.
    """
    long_cutoff  = str(date.today() - timedelta(days=long_days))
    short_cutoff = str(date.today() - timedelta(days=short_days))

    conn = get_connection()
    results = {}
    try:
        for table, col in LONG_RETENTION_TABLES.items():
            results[table] = _purge_table(conn, table, col, long_cutoff, dry_run)
        for table, col in SHORT_RETENTION_TABLES.items():
            results[table] = _purge_table(conn, table, col, short_cutoff, dry_run)

        total = sum(v for v in results.values() if isinstance(v, int))
        if not dry_run:
            conn.commit()
            conn.execute("VACUUM")  # reclaim disk space after a real delete
        log.info(f"  {'Would remove' if dry_run else 'Removed'} {total} rows total "
                 f"(long-retention cutoff {long_cutoff} / {long_days}d, "
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
    ap.add_argument("--days", type=int, default=DEFAULT_LONG_DAYS,
                     help=f"Retention days for core historical tables (default {DEFAULT_LONG_DAYS})")
    ap.add_argument("--short-days", type=int, default=DEFAULT_SHORT_DAYS,
                     help=f"Retention days for high-frequency/operational tables (default {DEFAULT_SHORT_DAYS})")
    args = ap.parse_args()

    res = purge_old_data(long_days=args.days, short_days=args.short_days, dry_run=not args.apply)
    print(f"\n{'DRY RUN — nothing deleted, add --apply to actually purge' if not args.apply else 'PURGE COMPLETE'}")
    for table, n in res.items():
        print(f"  {table:<22} {n}")
