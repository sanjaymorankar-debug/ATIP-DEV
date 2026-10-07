"""
Tables and additive columns added in W39 (tracker reconciliation 2026-10-07: the owner's
Wave 1-20 deployment tracker, the PERF-001 detail sheet and the owner notes), applied by
db/schema.py.

    perf_ledger (+ columns)   PERF-001-01  entry_seq (portable entry order: MySQL has no rowid),
                                           order_ref / signal_ref (exact order and signal links),
                                           fee_breakdown (JSON: brokerage / stt / exchange / sebi /
                                           stamp / gst / dp / other, when the source gives it)
    history_backfill_run      DP-23  every long-history backfill run (data/history_backfill.py)
    history_backfill_symbol   DP-23  the date before which the broker has no bar for a symbol
    ml_model_version (+ columns)  ML-18  code_version (git commit, +dirty) and lineage_json (dataset /
                                         feature set / config / backtest links) of every trained version
"""

W39_TABLES = {
    "history_backfill_run": (
        """CREATE TABLE IF NOT EXISTS history_backfill_run (
            run_id TEXT PRIMARY KEY, started_at TIMESTAMP, finished_at TIMESTAMP, years INTEGER, target_start DATE,
            symbols INTEGER, windows INTEGER, rows_stored INTEGER, status TEXT, detail_json TEXT)""",
    ),
    "history_backfill_symbol": (
        """CREATE TABLE IF NOT EXISTS history_backfill_symbol (
            symbol TEXT PRIMARY KEY, exhausted_before DATE, checked_at TIMESTAMP, note TEXT)""",
    ),
}

W39_COLUMNS = {
    "perf_ledger": {"entry_seq": "INTEGER", "order_ref": "TEXT", "signal_ref": "TEXT", "fee_breakdown": "TEXT"},
    # ML-18 research-to-production lineage: the code a model version was trained with
    "ml_model_version": {"code_version": "TEXT", "lineage_json": "TEXT"},
}
