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
    order_basket / order_basket_run  EX-18  named multi-leg baskets and every preview / placement
    sip_plan / sip_execution         EX-20  stock SIP plans and one row per due date (idempotent)
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
    "order_basket": (
        """CREATE TABLE IF NOT EXISTS order_basket (
            basket_id TEXT PRIMARY KEY, name TEXT NOT NULL, legs_json TEXT NOT NULL, note TEXT,
            archived INTEGER NOT NULL DEFAULT 0, created_at TIMESTAMP, updated_at TIMESTAMP)""",
    ),
    "order_basket_run": (
        """CREATE TABLE IF NOT EXISTS order_basket_run (
            run_id TEXT PRIMARY KEY, basket_id TEXT NOT NULL, at TIMESTAMP, env TEXT, confirm INTEGER,
            status TEXT, buy_value REAL, sell_value REAL, available REAL, results_json TEXT)""",
        "CREATE INDEX IF NOT EXISTS idx_order_basket_run ON order_basket_run(basket_id, at)",
    ),
    "sip_plan": (
        """CREATE TABLE IF NOT EXISTS sip_plan (
            plan_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, amount REAL, quantity INTEGER, frequency TEXT NOT NULL,
            day INTEGER NOT NULL, start_date DATE NOT NULL, end_date DATE, status TEXT NOT NULL, next_due DATE,
            last_run_date DATE, note TEXT, created_at TIMESTAMP, updated_at TIMESTAMP)""",
    ),
    "sip_execution": (
        """CREATE TABLE IF NOT EXISTS sip_execution (
            plan_id TEXT NOT NULL, due_date DATE NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_attempt_at
            TIMESTAMP, symbol TEXT, quantity INTEGER, price_ref REAL, status TEXT, order_status TEXT, order_id TEXT,
            detail TEXT, PRIMARY KEY (plan_id, due_date))""",
    ),
}

W39_COLUMNS = {
    "perf_ledger": {"entry_seq": "INTEGER", "order_ref": "TEXT", "signal_ref": "TEXT", "fee_breakdown": "TEXT"},
    # ML-18 research-to-production lineage: the code a model version was trained with
    "ml_model_version": {"code_version": "TEXT", "lineage_json": "TEXT"},
}
