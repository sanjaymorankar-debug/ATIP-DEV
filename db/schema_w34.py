"""
Tables and additive columns added in W34 (execution microstructure), applied by
db/schema.py _run_additive_migrations in a loop of its own.

    exec_algo_parent              EX-11  one parent per algo-executed risk decision (TWAP/VWAP/POV/ICEBERG)
    execution_impact_calibration  EX-12  fitted square-root impact coefficient history (LIVE fills only)
    latency_rollup                EX-15  per stage per minute: n, p50, p95, p99, max (ms)
    oms_event_outbox              EX-16  transactional outbox of OMS events (order.state, order.fill, algo.*)
    oms_event_delivery            EX-16  per event x handler delivery record (at-least-once, idempotent)
"""

W34_TABLES = {
    "exec_algo_parent": (
        """CREATE TABLE IF NOT EXISTS exec_algo_parent (
            parent_id TEXT PRIMARY KEY, risk_decision_id TEXT NOT NULL UNIQUE, intent_id TEXT, decision_id TEXT,
            strategy_id TEXT, strategy_version TEXT, symbol TEXT NOT NULL, side TEXT NOT NULL,
            total_qty INTEGER NOT NULL, algo TEXT NOT NULL, params_json TEXT, start_at TIMESTAMP, end_at TIMESTAMP,
            status TEXT NOT NULL, filled_qty INTEGER NOT NULL DEFAULT 0, avg_price REAL, child_count INTEGER NOT NULL
            DEFAULT 0, reference_price REAL, impact_estimate_json TEXT, mode TEXT, reason TEXT,
            created_at TIMESTAMP, updated_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_algo_parent_status ON exec_algo_parent(status, start_at)",
    ),
    "execution_impact_calibration": (
        """CREATE TABLE IF NOT EXISTS execution_impact_calibration (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TIMESTAMP, n_fills INTEGER, y REAL,
            residual_bps REAL, adopted INTEGER, detail_json TEXT)""",
    ),
    "latency_rollup": (
        """CREATE TABLE IF NOT EXISTS latency_rollup (
            stage TEXT NOT NULL, minute TIMESTAMP NOT NULL, n INTEGER, p50_ms REAL, p95_ms REAL, p99_ms REAL,
            max_ms REAL, mean_ms REAL, PRIMARY KEY (stage, minute))""",
        "CREATE INDEX IF NOT EXISTS idx_latency_minute ON latency_rollup(minute)",
    ),
    "oms_event_outbox": (
        """CREATE TABLE IF NOT EXISTS oms_event_outbox (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, topic TEXT NOT NULL, key TEXT,
            payload_json TEXT, created_at TIMESTAMP, dispatched_at TIMESTAMP, attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT)""",
        "CREATE INDEX IF NOT EXISTS idx_outbox_pending ON oms_event_outbox(dispatched_at, seq)",
        "CREATE INDEX IF NOT EXISTS idx_outbox_topic ON oms_event_outbox(topic, created_at)",
    ),
    "oms_event_delivery": (
        """CREATE TABLE IF NOT EXISTS oms_event_delivery (
            event_id TEXT NOT NULL, handler TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 1,
            error TEXT, at TIMESTAMP, PRIMARY KEY (event_id, handler))""",
    ),
}

W34_COLUMNS = {
    "oms_order": {"algo_parent_id": "TEXT", "algo_slice": "INTEGER"},
}
