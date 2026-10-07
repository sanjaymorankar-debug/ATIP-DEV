"""ATIP — Database Schema

Time convention (DBS-06): every time ATIP writes itself is IST local, naive
(the machine runs in IST and datetime.now() is what is written):
  market data  prices_daily.date, index_levels (date, time), live_quotes.timestamp
  signals      ai_scores.date, signal_log.signal_date / logged_at, predictions.pred_date
  orders       order_log.timestamp, order_rules.created_at/updated_at,
               paper_order.created_at            -- ISO text, 'YYYY-MM-DDTHH:MM:SS.ffffff'
  positions    paper_position.updated_at, strategy_position / strategy_event.created_at (ISO text)
  portfolio    portfolio_holdings.date, portfolio_sync.synced_at, pnl_daily.date / recorded_at
  backtests    bar dates from prices_daily
  logs, jobs   pipeline_log start_time/end_time, alert_log.created_at, data_quality.run_at,
               news_articles.fetched_at (publication time, converted from the feed's UTC)
The only exception is the `created_at ... DEFAULT CURRENT_TIMESTAMP` audit
columns: SQLite's CURRENT_TIMESTAMP is UTC. Compare one with an IST date only
after DATE(created_at,'+5 hours','+30 minutes'). Text timestamps in ISO form
('T' separator) and SQLite form (space) both sort correctly within one column;
take [:19] and replace 'T' with ' ' before comparing across the two.
"""
import os, sqlite3, logging, threading
from pathlib import Path
from datetime import datetime, date

# W8: ATIP_DB_PATH points a process (tests, a restore drill, staging) at another file.
DB_PATH = Path(os.environ.get("ATIP_DB_PATH", "atip_data/atip.db"))
log = logging.getLogger(__name__)


# ── sqlite3 DATE/TIMESTAMP converters ──────────────────────────────────────
# get_connection() uses detect_types=PARSE_DECLTYPES, so sqlite3 converts any
# column declared DATE or TIMESTAMP on the way out. Python 3.12 deprecated the
# *default* converters for those two types, which made every such query emit
#   DeprecationWarning: The default date/timestamp converter is deprecated
# — thousands of lines per scoring run, drowning the actual output.
#
# Registering our own converters is the documented replacement. These
# deliberately reproduce the old behaviour (DATE -> datetime.date,
# TIMESTAMP -> datetime.datetime) so nothing downstream changes, and they fall
# back to the raw string rather than raising if a stored value isn't ISO —
# a malformed timestamp should not take down a query.
def _conv_date(raw):
    s = raw.decode() if isinstance(raw, bytes) else raw
    try:
        return datetime.fromisoformat(s).date() if len(s) > 10 else date.fromisoformat(s)
    except (ValueError, TypeError):
        return s

def _conv_timestamp(raw):
    s = raw.decode() if isinstance(raw, bytes) else raw
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return s

for _decl in ("date", "DATE"):
    sqlite3.register_converter(_decl, _conv_date)
for _decl in ("timestamp", "TIMESTAMP", "datetime", "DATETIME"):
    sqlite3.register_converter(_decl, _conv_timestamp)

# How long a writer waits for another writer before giving up (milliseconds).
BUSY_TIMEOUT_MS = 60_000


_PG_URL: list = []
_MYSQL_URL: list = []


def _gated_runtime_url(schemes, backend_name):
    """The configured URL when the runtime is switched to `backend_name`, else None.

    All three are required -- ATIP_DATABASE_URL (not the generic DATABASE_URL other
    projects set) with a matching scheme, config database.backend = backend_name, and
    database.allow_experimental = true -- because the statements db/dialect_scan.py
    lists (PRAGMA, sqlite_master, rowid) still fail off SQLite."""
    url = os.environ.get("ATIP_DATABASE_URL", "")
    if not url.lower().startswith(schemes):
        return None
    try:
        import json
        cfg = json.loads((Path("atip_data") / "config.json").read_text(encoding="utf-8"))
        d = cfg.get("database") or {}
        if d.get("backend") == backend_name and d.get("allow_experimental") is True:
            return url
    except Exception:
        pass
    return None


def mysql_runtime_url():
    """The MySQL URL when the runtime is switched over, else None.

    Gated exactly like pg_runtime_url(): config.json "database": {"backend": "mysql",
    "allow_experimental": true} plus ATIP_DATABASE_URL=mysql://user:pass@host/db.
    db/mysql.py translates the whole schema (verified against a real server by
    tests/test_mysql_backend.py), but hand-written queries in the wider code base
    still contain SQLite-only constructs and reserved-word column references
    (db.mysql.reserved_columns reports those), so the backend stays opt-in."""
    if not _MYSQL_URL:
        _MYSQL_URL.append(_gated_runtime_url(("mysql://", "mysql+pymysql://", "mariadb://"), "mysql"))
    return _MYSQL_URL[0]


def pg_runtime_url():
    """W38 (DBS-05): the PostgreSQL URL when the runtime is switched over, else None. All three are
    required -- ATIP_DATABASE_URL (not the generic DATABASE_URL other projects set) is postgresql://,
    config database.backend = "postgresql" and database.allow_experimental = true -- because the
    statements db/dialect_scan.py lists (PRAGMA, sqlite_master, rowid) still fail on PostgreSQL."""
    if not _PG_URL:
        _PG_URL.append(_gated_runtime_url(
            ("postgres://", "postgresql://", "postgresql+psycopg://"), "postgresql"))
    return _PG_URL[0]


def describe_target() -> str:
    """Which database init_db() actually initialises, for logging and for --init.

    Derived from the same gated URLs get_connection() dispatches on, NOT from
    db.backend.database_url(): that reads ATIP_DATABASE_URL whether or not the
    config gates let it through, so it would name an engine the runtime is not
    using -- the opposite of the problem this function exists to fix.

    Credentials are masked. A DSN carries a password and this string is both
    logged and printed to the terminal.
    """
    from db.backend import masked_url
    pg = pg_runtime_url()
    if pg:
        return f"PostgreSQL — {masked_url(pg)}"
    my = mysql_runtime_url()
    if my:
        return f"MySQL — {masked_url(my)}"
    return f"SQLite — {DB_PATH.resolve()}"


def get_connection():
    pg = pg_runtime_url()
    if pg:
        from db.backend import PgConnection
        return PgConnection(pg)
    my = mysql_runtime_url()
    if my:
        from db.backend import MySQLConnection
        return MySQLConnection(my)
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # WAL lets readers and one writer coexist, but a second WRITER still has to
    # wait -- and sqlite3's default patience is 5 seconds, which is shorter than
    # this system's own write transactions: run_scoring_pipeline holds one across
    # its whole 502-symbol loop (~50s). That is why the index feed logged "Index
    # feed DB flush failed: database is locked" and why a post-market catch-up
    # died with the same error on 2026-09-20. Wait instead of failing.
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_migrated(conn)
    return conn


# W8 (W7-R8): the additive migrations below used to run on EVERY connection --
# dozens of sqlite_master / PRAGMA table_info probes per get_connection(). They
# are idempotent, so running them once per process per database file is enough.
# ATIP_MIGRATE_EVERY_CONNECTION=1 (or ops.migrate_every_connection) restores the
# old behaviour. Versioned migrations on top of these live in ops/migrations.py.
_MIGRATED: set = set()
_MIGRATE_LOCK = threading.Lock()


def _every_connection() -> bool:
    if os.environ.get("ATIP_MIGRATE_EVERY_CONNECTION", "").lower() in ("1", "true", "yes"):
        return True
    try:
        import json
        cfg = json.loads((Path("atip_data") / "config.json").read_text(encoding="utf-8"))
        return bool((cfg.get("ops") or {}).get("migrate_every_connection", False))
    except Exception:
        return False


def _ensure_migrated(conn, force=False):
    key = str(Path(DB_PATH).resolve())
    if key in _MIGRATED and not force:
        return
    with _MIGRATE_LOCK:
        if key in _MIGRATED and not force:
            return
        _run_additive_migrations(conn)
        if not _every_connection():
            _MIGRATED.add(key)


def _run_additive_migrations(conn):
    _migrate_index_levels_chg_columns(conn)
    _migrate_index_levels_unique(conn)
    _migrate_corporate_actions_table(conn)
    _migrate_ai_scores_beta_column(conn)
    _migrate_technical_macd_pct_column(conn)
    _migrate_market_health_breadth_columns(conn)
    _migrate_news_classifier_column(conn)
    _migrate_pipeline_log_columns(conn)
    _migrate_alert_log_table(conn)
    for name, ddls in {**W1_TABLES, **W2_TABLES, **W3_TABLES, **W4_TABLES, **W5_TABLES,
                       **W6_TABLES, **W7_TABLES, **W8_TABLES, **WEALTH_TABLES,
                       **W21_TABLES, **W22_TABLES, **W24_TABLES, **W25_TABLES, **W27_TABLES, **W28_TABLES, **W29_TABLES, **W30_TABLES, **W9_TABLES, **W32_TABLES}.items():
        _create_table_if_missing(conn, name, ddls)
    for table, cols in W3_W4_COLUMNS.items():       # additive columns on tables created earlier
        _add_missing_columns(conn, table, cols)
    from db.schema_w36 import W36_TABLES                    # W36: ML / strategy tooling
    for name, ddls in W36_TABLES.items():
        _create_table_if_missing(conn, name, ddls)
    from db.schema_w38 import W38_TABLES                    # W38: platform & compliance
    for name, ddls in W38_TABLES.items():
        _create_table_if_missing(conn, name, ddls)
    from db.schema_w37 import W37_TABLES, W37_COLUMNS       # W37: brokers & multi-asset
    for name, ddls in W37_TABLES.items():
        _create_table_if_missing(conn, name, ddls)
    for table, cols in W37_COLUMNS.items():
        _add_missing_columns(conn, table, cols)
    from db.schema_w28b import W28B_TABLES, W28B_COLUMNS   # W28b: NS-05 / NS-06
    for name, ddls in W28B_TABLES.items():
        _create_table_if_missing(conn, name, ddls)
    for table, cols in W28B_COLUMNS.items():
        _add_missing_columns(conn, table, cols)
    from db.schema_w34 import W34_TABLES, W34_COLUMNS     # W34: execution microstructure
    for name, ddls in W34_TABLES.items():
        _create_table_if_missing(conn, name, ddls)
    for table, cols in W34_COLUMNS.items():
        _add_missing_columns(conn, table, cols)
    from db.schema_w35 import W35_TABLES                    # W35: data platform
    for name, ddls in W35_TABLES.items():
        _create_table_if_missing(conn, name, ddls)

def _create_table_if_missing(conn, name, ddls):
    """Additive migration: create a table (and its indexes) an existing
    database does not have yet. Never alters or drops anything."""
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone():
            for ddl in ddls:
                conn.execute(ddl)
            conn.commit()
    except sqlite3.OperationalError as e:
        log.warning(f"  {name} migration skipped: {e}")

# ── Tables added in W1 (foundation) ───────────────────────────────────────
W1_TABLES = {
    # One row per (date, env) -- PAPER or LIVE -- written by portfolio/pnl.py
    # at the end of each session (PF-13). The daily-loss and drawdown limits in
    # orders/risk.py read it: day P&L against the previous row, drawdown
    # against the highest peak_equity. equity is NULL when cash was unknown
    # (LIVE with the broker unreachable).
    "pnl_daily": (
        """CREATE TABLE IF NOT EXISTS pnl_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date DATE NOT NULL, env TEXT NOT NULL,
            n_positions INTEGER, positions_value REAL, cost REAL, unrealised REAL,
            realised_cum REAL, cash REAL, equity REAL, day_pnl REAL,
            peak_equity REAL, drawdown_pct REAL, recorded_at TIMESTAMP,
            UNIQUE(date, env))""",
    ),
    # One row per (date, check) from data/quality.py (DP-19): what was checked,
    # how many rows failed, a sample, and the session's DataQualityScore row
    # (check='DQS').
    "data_quality": (
        """CREATE TABLE IF NOT EXISTS data_quality (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date DATE NOT NULL, check_name TEXT NOT NULL,
            severity TEXT NOT NULL, failed INTEGER, checked INTEGER, score REAL,
            detail TEXT, run_at TIMESTAMP, UNIQUE(date, check_name))""",
    ),
    # One row per portfolio sync attempt (PF-01/BR-04). The LIVE book is the
    # holdings of the latest SUCCESS here -- so an account that sold
    # everything reads as empty, not as its last day with holdings -- and a
    # FAILED row says why (DH-901 = the Dhan token expired).
    "portfolio_sync": (
        """CREATE TABLE IF NOT EXISTS portfolio_sync (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date DATE NOT NULL, source TEXT NOT NULL,
            status TEXT NOT NULL, n_holdings INTEGER, error TEXT, synced_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_portfolio_sync_date ON portfolio_sync(date, status)",
    ),
    # Per-feed outcome of the last news fetch (NS-01): a feed that stops
    # answering shows up here instead of silently contributing nothing.
    "news_source_status": (
        """CREATE TABLE IF NOT EXISTS news_source_status (
            source TEXT PRIMARY KEY, url TEXT, last_attempt TIMESTAMP, last_ok TIMESTAMP,
            last_items INTEGER, consecutive_failures INTEGER DEFAULT 0, last_error TEXT)""",
    ),
}

# ── Tables added in W2 (research & backtesting) ───────────────────────────
# backtest/store.py. One backtest_run row per run -- its full configuration
# snapshot (strategy, version, parameters, period, capital, costs, slippage,
# liquidity, sizing) and hash, the code version and data fingerprint it ran
# on, its bias report and metrics -- with the trades, dated equity curve and
# drawdown episodes in child tables. Execution assumptions live in the
# snapshot rather than in a table of their own: they are part of what makes a
# run reproducible, and stored together they cannot drift apart.
W2_TABLES = {
    "backtest_run": (
        """CREATE TABLE IF NOT EXISTS backtest_run (
            run_id TEXT PRIMARY KEY, parent_run_id TEXT, kind TEXT NOT NULL DEFAULT 'single',
            window_index INTEGER, strategy_id TEXT NOT NULL, strategy_version TEXT,
            period_label TEXT, start_date DATE, end_date DATE, data_source TEXT, timeframe TEXT,
            initial_capital REAL, params_json TEXT, config_json TEXT NOT NULL, config_hash TEXT,
            code_version TEXT, data_fingerprint_json TEXT, bias_report_json TEXT, metrics_json TEXT,
            summary_json TEXT, status TEXT NOT NULL DEFAULT 'CREATED', error TEXT,
            created_at TIMESTAMP, started_at TIMESTAMP, finished_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_backtest_run_strategy ON backtest_run(strategy_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_backtest_run_parent ON backtest_run(parent_run_id)",
    ),
    "backtest_trade": (
        """CREATE TABLE IF NOT EXISTS backtest_trade (
            run_id TEXT NOT NULL, seq INTEGER NOT NULL, symbol TEXT NOT NULL,
            entry_date DATE, entry_price REAL, entry_ref_price REAL, qty INTEGER,
            exit_date DATE, exit_price REAL, exit_ref_price REAL, exit_reason TEXT,
            gross_pnl REAL, costs REAL, net_pnl REAL, return_pct REAL, holding_sessions INTEGER,
            entry_reason TEXT, PRIMARY KEY (run_id, seq))""",
    ),
    "backtest_equity": (
        """CREATE TABLE IF NOT EXISTS backtest_equity (
            run_id TEXT NOT NULL, date DATE NOT NULL, cash REAL, positions_value REAL, equity REAL,
            exposure_pct REAL, n_positions INTEGER, realized_cum REAL, unrealized REAL,
            daily_return REAL, peak_equity REAL, drawdown_pct REAL, PRIMARY KEY (run_id, date))""",
    ),
    "backtest_drawdown": (
        """CREATE TABLE IF NOT EXISTS backtest_drawdown (
            run_id TEXT NOT NULL, seq INTEGER NOT NULL, peak_date DATE, trough_date DATE,
            recovery_date DATE, peak_equity REAL, trough_equity REAL, depth_pct REAL,
            duration_sessions INTEGER, recovery_sessions INTEGER, PRIMARY KEY (run_id, seq))""",
    ),
    "backtest_montecarlo": (
        """CREATE TABLE IF NOT EXISTS backtest_montecarlo (
            mc_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, method TEXT NOT NULL, n_sims INTEGER,
            seed INTEGER, params_json TEXT, results_json TEXT, created_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_backtest_mc_run ON backtest_montecarlo(run_id)",
    ),
}

# ── Tables added in W3 (strategy engine) ──────────────────────────────────
# strategy_engine/. The definition JSON in strategy_version is the source of
# truth for a version (rules, factors, weights, parameters, position and risk
# rules); strategy_parameter indexes its parameters so they can be queried per
# version. The audit table is strategy_engine_event, NOT strategy_event: that
# name already belongs to the aggressive-exit module's live position ledger
# (strategy/positions.py), which W3 leaves untouched.
W3_TABLES = {
    "strategy": (
        """CREATE TABLE IF NOT EXISTS strategy (
            strategy_id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT, kind TEXT NOT NULL,
            category TEXT, status TEXT NOT NULL DEFAULT 'DRAFT', current_version TEXT,
            source TEXT DEFAULT 'user', owner TEXT DEFAULT 'owner', priority INTEGER DEFAULT 100,
            weight REAL DEFAULT 1.0, created_at TIMESTAMP, updated_at TIMESTAMP, activated_at TIMESTAMP)""",
    ),
    "strategy_version": (
        """CREATE TABLE IF NOT EXISTS strategy_version (
            strategy_id TEXT NOT NULL, version TEXT NOT NULL, definition_json TEXT NOT NULL,
            definition_hash TEXT NOT NULL, notes TEXT, created_at TIMESTAMP, first_activated_at TIMESTAMP,
            PRIMARY KEY (strategy_id, version))""",
    ),
    "strategy_parameter": (
        """CREATE TABLE IF NOT EXISTS strategy_parameter (
            strategy_id TEXT NOT NULL, version TEXT NOT NULL, name TEXT NOT NULL, type TEXT NOT NULL,
            default_json TEXT, min REAL, max REAL, allowed_json TEXT, required INTEGER NOT NULL DEFAULT 0,
            description TEXT, PRIMARY KEY (strategy_id, version, name))""",
    ),
    "strategy_regime_mapping": (
        """CREATE TABLE IF NOT EXISTS strategy_regime_mapping (
            id INTEGER PRIMARY KEY AUTOINCREMENT, regime TEXT NOT NULL, strategy_id TEXT,
            no_trade INTEGER NOT NULL DEFAULT 0, priority INTEGER NOT NULL DEFAULT 100,
            weight REAL NOT NULL DEFAULT 1.0, enabled INTEGER NOT NULL DEFAULT 1, notes TEXT,
            updated_at TIMESTAMP, UNIQUE (regime, strategy_id))""",
    ),
    "strategy_engine_event": (
        """CREATE TABLE IF NOT EXISTS strategy_engine_event (
            id INTEGER PRIMARY KEY AUTOINCREMENT, strategy_id TEXT, version TEXT, event_type TEXT NOT NULL,
            from_state TEXT, to_state TEXT, message TEXT, details_json TEXT, actor TEXT, at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_strategy_engine_event ON strategy_engine_event(strategy_id, at)",
    ),
    "strategy_decision_run": (
        """CREATE TABLE IF NOT EXISTS strategy_decision_run (
            run_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, version TEXT, as_of DATE, book TEXT,
            status TEXT NOT NULL DEFAULT 'SUCCESS', error TEXT, params_json TEXT, n_universe INTEGER,
            n_evaluated INTEGER, counts_json TEXT, created_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_strategy_decision_run ON strategy_decision_run(strategy_id, as_of)",
    ),
    "strategy_decision": (
        """CREATE TABLE IF NOT EXISTS strategy_decision (
            decision_id TEXT PRIMARY KEY, run_id TEXT, strategy_id TEXT NOT NULL, version TEXT NOT NULL,
            as_of DATE NOT NULL, timestamp TIMESTAMP, symbol TEXT NOT NULL, decision TEXT NOT NULL,
            action TEXT NOT NULL, confidence REAL, score REAL, regime TEXT, reasons_json TEXT,
            parameters_json TEXT, risk_requirement TEXT, target_position_pct REAL, stop_price REAL,
            target_price REAL, max_hold_sessions INTEGER, blocked_reason TEXT, features_json TEXT,
            reason_codes_json TEXT, signal_source TEXT,
            UNIQUE (strategy_id, version, as_of, symbol))""",
        "CREATE INDEX IF NOT EXISTS idx_strategy_decision_date ON strategy_decision(as_of, decision)",
    ),
    "strategy_position_intent": (
        """CREATE TABLE IF NOT EXISTS strategy_position_intent (
            intent_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL UNIQUE, strategy_id TEXT NOT NULL,
            version TEXT NOT NULL, as_of DATE NOT NULL, timestamp TIMESTAMP, symbol TEXT NOT NULL,
            side TEXT NOT NULL, action TEXT, target_position_pct REAL, quantity INTEGER, stop_price REAL,
            target_price REAL, max_hold_sessions INTEGER, confidence REAL, reason TEXT,
            risk_requirement TEXT, authorization_status TEXT NOT NULL DEFAULT 'NOT_AUTHORIZED',
            created_at TIMESTAMP, entry_reference REAL, book TEXT, risk_decision_id TEXT,
            authorized_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_strategy_intent ON strategy_position_intent(as_of, strategy_id)",
    ),
    # the features each version needs (derived from its definition), so
    # "which strategies use rsi_14?" is a query, not a JSON scan
    "strategy_feature": (
        """CREATE TABLE IF NOT EXISTS strategy_feature (
            strategy_id TEXT NOT NULL, version TEXT NOT NULL, feature TEXT NOT NULL, inputs TEXT,
            PRIMARY KEY (strategy_id, version, feature))""",
    ),
    "strategy_health": (
        """CREATE TABLE IF NOT EXISTS strategy_health (
            strategy_id TEXT NOT NULL, version TEXT NOT NULL, as_of DATE NOT NULL, status TEXT NOT NULL,
            metrics_json TEXT, issues_json TEXT, created_at TIMESTAMP,
            PRIMARY KEY (strategy_id, version, as_of))""",
    ),
}

# ── Tables added in W4 (risk engine and execution) ──────────────────────────
# execution/. The chain an audit walks, each row naming the one before it:
#   strategy_decision -> strategy_position_intent -> risk_decision -> oms_order
#   -> oms_order_event / oms_execution -> oms_fill -> paper_position
# The table names are prefixed oms_ because order_rules / order_log / paper_order
# already exist (the W1-era order rules, order log and paper broker) and stay
# as they are: the paper broker still keeps its own paper_order / paper_position.
W4_TABLES = {
    # risk limit overrides set through the API; code defaults and config.json
    # "w4_risk_limits" sit underneath (execution/config.py)
    "risk_limit": (
        """CREATE TABLE IF NOT EXISTS risk_limit (
            key TEXT PRIMARY KEY, value_json TEXT, note TEXT, updated_by TEXT, updated_at TIMESTAMP)""",
    ),
    "risk_limit_history": (
        """CREATE TABLE IF NOT EXISTS risk_limit_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, old_json TEXT, new_json TEXT,
            actor TEXT, at TIMESTAMP)""",
    ),
    "risk_decision": (
        """CREATE TABLE IF NOT EXISTS risk_decision (
            risk_decision_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL, decision_id TEXT,
            strategy_id TEXT, strategy_version TEXT, symbol TEXT NOT NULL, side TEXT, action TEXT,
            book TEXT, mode TEXT, requested_quantity INTEGER, approved_quantity INTEGER,
            reference_price REAL, est_value REAL, equity REAL, risk_status TEXT NOT NULL,
            rejection_reason TEXT, risk_checks_json TEXT, limits_json TEXT, engine_version TEXT,
            reviewed_by TEXT, reviewed_at TIMESTAMP, created_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_risk_decision_intent ON risk_decision(intent_id)",
        "CREATE INDEX IF NOT EXISTS idx_risk_decision_created ON risk_decision(created_at, risk_status)",
    ),
    "oms_order": (
        """CREATE TABLE IF NOT EXISTS oms_order (
            order_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL UNIQUE, risk_decision_id TEXT NOT NULL UNIQUE,
            decision_id TEXT, strategy_id TEXT, strategy_version TEXT, symbol TEXT NOT NULL,
            side TEXT NOT NULL, quantity INTEGER NOT NULL, order_type TEXT NOT NULL, limit_price REAL,
            product_type TEXT, mode TEXT NOT NULL, adapter TEXT, status TEXT NOT NULL,
            broker_order_id TEXT, filled_quantity INTEGER NOT NULL DEFAULT 0, avg_fill_price REAL,
            fees REAL NOT NULL DEFAULT 0, reference_price REAL, reason TEXT,
            created_at TIMESTAMP, updated_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_oms_order_status ON oms_order(status, created_at)",
    ),
    "oms_order_event": (
        """CREATE TABLE IF NOT EXISTS oms_order_event (
            id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL, from_status TEXT,
            to_status TEXT NOT NULL, message TEXT, details_json TEXT, actor TEXT, at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_oms_order_event ON oms_order_event(order_id, id)",
    ),
    "oms_execution": (
        """CREATE TABLE IF NOT EXISTS oms_execution (
            execution_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, adapter TEXT, action TEXT,
            request_json TEXT, response_json TEXT, status TEXT, broker_order_id TEXT, error TEXT,
            at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_oms_execution_order ON oms_execution(order_id)",
    ),
    "oms_fill": (
        """CREATE TABLE IF NOT EXISTS oms_fill (
            fill_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, execution_id TEXT, strategy_id TEXT,
            strategy_version TEXT, symbol TEXT NOT NULL, side TEXT NOT NULL, quantity INTEGER NOT NULL,
            price REAL NOT NULL, fees REAL NOT NULL DEFAULT 0, price_source TEXT, mode TEXT,
            filled_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_oms_fill_strategy ON oms_fill(strategy_id, symbol)",
    ),
}

# ── Tables added in W5 (AI/ML) ──────────────────────────────────────────────
# ml/. Traceability chain: ml_prediction -> (model_id, model_version) ->
# ml_model_version (artifact hash, feature set hash, dataset spec + snapshot
# hash, training config hash) -> ml_dataset -> ml_feature_set -> ml_feature.
# The older "predictions" table (scores/predictions.py, the signal engine's
# own forecasts) is unrelated and unchanged.
W5_TABLES = {
    "ml_feature": (
        """CREATE TABLE IF NOT EXISTS ml_feature (
            feature_id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT, category TEXT, data_type TEXT,
            calculation_method TEXT, version TEXT NOT NULL, dependencies_json TEXT, availability TEXT,
            lookback INTEGER, created_at TIMESTAMP)""",
    ),
    "ml_feature_set": (
        """CREATE TABLE IF NOT EXISTS ml_feature_set (
            name TEXT NOT NULL, version TEXT NOT NULL, features_json TEXT NOT NULL,
            feature_versions_json TEXT NOT NULL, description TEXT, content_hash TEXT NOT NULL,
            created_at TIMESTAMP, PRIMARY KEY (name, version))""",
    ),
    # reusable, versioned label definitions (a dataset also embeds its own label spec)
    "ml_label": (
        """CREATE TABLE IF NOT EXISTS ml_label (
            label_id TEXT PRIMARY KEY, name TEXT NOT NULL, version TEXT NOT NULL, kind TEXT NOT NULL,
            task TEXT NOT NULL, spec_json TEXT NOT NULL, spec_hash TEXT NOT NULL, description TEXT,
            created_at TIMESTAMP)""",
    ),
    "ml_dataset": (
        """CREATE TABLE IF NOT EXISTS ml_dataset (
            dataset_id TEXT PRIMARY KEY, name TEXT NOT NULL, version TEXT NOT NULL, spec_json TEXT NOT NULL,
            spec_hash TEXT NOT NULL, feature_set TEXT NOT NULL, label_json TEXT NOT NULL, start_date DATE,
            end_date DATE, universe_json TEXT, frequency TEXT, sampling_json TEXT, source TEXT, status TEXT,
            summary_json TEXT, snapshot_path TEXT, snapshot_hash TEXT, created_at TIMESTAMP, built_at TIMESTAMP)""",
    ),
    "ml_model": (
        """CREATE TABLE IF NOT EXISTS ml_model (
            model_id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT, model_type TEXT NOT NULL,
            task TEXT NOT NULL, label_kind TEXT NOT NULL, feature_set TEXT NOT NULL, purpose TEXT NOT NULL,
            status TEXT NOT NULL, active_version TEXT, owner TEXT, created_at TIMESTAMP, updated_at TIMESTAMP)""",
    ),
    "ml_model_version": (
        """CREATE TABLE IF NOT EXISTS ml_model_version (
            model_id TEXT NOT NULL, version TEXT NOT NULL, status TEXT NOT NULL, feature_set TEXT,
            feature_set_hash TEXT, dataset_id TEXT, dataset_spec_hash TEXT, dataset_snapshot_hash TEXT,
            training_config_json TEXT, training_config_hash TEXT, train_start DATE, train_end DATE,
            artifact_path TEXT, artifact_hash TEXT, metrics_json TEXT, error TEXT, created_at TIMESTAMP,
            trained_at TIMESTAMP, activated_at TIMESTAMP, PRIMARY KEY (model_id, version))""",
    ),
    "ml_model_event": (
        """CREATE TABLE IF NOT EXISTS ml_model_event (
            id INTEGER PRIMARY KEY AUTOINCREMENT, model_id TEXT NOT NULL, version TEXT, event_type TEXT NOT NULL,
            from_state TEXT, to_state TEXT, message TEXT, details_json TEXT, actor TEXT, at TIMESTAMP)""",
    ),
    "ml_training_run": (
        """CREATE TABLE IF NOT EXISTS ml_training_run (
            run_id TEXT PRIMARY KEY, model_id TEXT NOT NULL, version TEXT, dataset_id TEXT, status TEXT NOT NULL,
            config_json TEXT, metrics_json TEXT, rows INTEGER, error TEXT, traceback TEXT, actor TEXT,
            started_at TIMESTAMP, finished_at TIMESTAMP)""",
    ),
    "ml_prediction": (
        """CREATE TABLE IF NOT EXISTS ml_prediction (
            prediction_id TEXT PRIMARY KEY, model_id TEXT NOT NULL, model_version TEXT NOT NULL,
            version_status TEXT, symbol TEXT NOT NULL, as_of DATE NOT NULL, prediction TEXT,
            prediction_value REAL, probabilities_json TEXT, confidence REAL, prob_up REAL, ml_score REAL,
            interval_low REAL, interval_high REAL, feature_set TEXT, feature_set_hash TEXT, artifact_hash TEXT,
            explanation_json TEXT, features_json TEXT, created_at TIMESTAMP,
            UNIQUE (model_id, model_version, symbol, as_of))""",
        "CREATE INDEX IF NOT EXISTS idx_ml_prediction_date ON ml_prediction(as_of, model_id)",
    ),
    "ml_model_metrics": (
        """CREATE TABLE IF NOT EXISTS ml_model_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT, model_id TEXT NOT NULL, version TEXT NOT NULL, kind TEXT NOT NULL,
            period_start DATE, period_end DATE, metrics_json TEXT, created_at TIMESTAMP)""",
    ),
    "ml_model_explanation": (
        """CREATE TABLE IF NOT EXISTS ml_model_explanation (
            id INTEGER PRIMARY KEY AUTOINCREMENT, model_id TEXT NOT NULL, version TEXT NOT NULL, kind TEXT NOT NULL,
            explanation_version TEXT, payload_json TEXT, created_at TIMESTAMP)""",
    ),
    "ml_model_monitoring": (
        """CREATE TABLE IF NOT EXISTS ml_model_monitoring (
            model_id TEXT NOT NULL, version TEXT NOT NULL, as_of DATE NOT NULL, version_status TEXT,
            prediction_dist_json TEXT, feature_drift_json TEXT, data_quality_json TEXT, n_shifted INTEGER,
            created_at TIMESTAMP, PRIMARY KEY (model_id, version, as_of))""",
    ),
}

# ── Tables added in W6 (advanced quant) ─────────────────────────────────────
# quant/. Factor metadata is versioned (quant_factor PK factor_id+version);
# per-date scores for factors AND composites share quant_factor_score (kind
# factor | composite) -- rankings are its rank / sector_rank columns, so there is
# no separate rankings table. market_event normalises corporate_actions and
# bulk_deals (it references them by source + source_id; they stay unchanged).
# The derivatives tables are schema only: no futures / options source exists yet.
W6_TABLES = {
    "quant_factor": (
        """CREATE TABLE IF NOT EXISTS quant_factor (
            factor_id TEXT NOT NULL, version TEXT NOT NULL, name TEXT, category TEXT, description TEXT,
            inputs_json TEXT, formula TEXT, lookback INTEGER, frequency TEXT, normalization_json TEXT,
            direction INTEGER, data_dependency TEXT, status TEXT, content_hash TEXT, created_at TIMESTAMP,
            PRIMARY KEY (factor_id, version))""",
    ),
    "quant_factor_set": (
        """CREATE TABLE IF NOT EXISTS quant_factor_set (
            name TEXT NOT NULL, version TEXT NOT NULL, factors_json TEXT NOT NULL, content_hash TEXT NOT NULL,
            description TEXT, created_at TIMESTAMP, PRIMARY KEY (name, version))""",
    ),
    "quant_composite": (
        """CREATE TABLE IF NOT EXISTS quant_composite (
            name TEXT NOT NULL, version TEXT NOT NULL, components_json TEXT NOT NULL, min_coverage REAL,
            normalization_json TEXT, description TEXT, content_hash TEXT NOT NULL, status TEXT,
            created_at TIMESTAMP, PRIMARY KEY (name, version))""",
    ),
    "quant_factor_score": (
        """CREATE TABLE IF NOT EXISTS quant_factor_score (
            as_of DATE NOT NULL, symbol TEXT NOT NULL, factor_key TEXT NOT NULL, kind TEXT NOT NULL,
            raw REAL, norm REAL, pct REAL, score REAL, rank INTEGER, sector TEXT, sector_rank INTEGER,
            universe_size INTEGER, created_at TIMESTAMP, PRIMARY KEY (as_of, symbol, factor_key))""",
        "CREATE INDEX IF NOT EXISTS idx_qfs_key_date ON quant_factor_score(factor_key, as_of)",
    ),
    "quant_factor_research": (
        """CREATE TABLE IF NOT EXISTS quant_factor_research (
            id INTEGER PRIMARY KEY AUTOINCREMENT, factor_key TEXT NOT NULL, kind TEXT NOT NULL,
            start_date DATE, end_date DATE, result_json TEXT, created_at TIMESTAMP)""",
    ),
    "quant_experiment": (
        """CREATE TABLE IF NOT EXISTS quant_experiment (
            experiment_id TEXT PRIMARY KEY, name TEXT NOT NULL, version TEXT, hypothesis TEXT, config_json TEXT,
            config_hash TEXT, status TEXT NOT NULL, backtest_run_ids_json TEXT, error TEXT,
            created_at TIMESTAMP, updated_at TIMESTAMP)""",
    ),
    "quant_pair": (
        """CREATE TABLE IF NOT EXISTS quant_pair (
            pair_id TEXT NOT NULL, version TEXT NOT NULL, asset_a TEXT NOT NULL, asset_b TEXT NOT NULL,
            hedge_ratio TEXT, spread_kind TEXT, lookback INTEGER, entry_z REAL, exit_z REAL, stop_z REAL,
            capital_allocation_pct REAL, status TEXT, notes TEXT, created_at TIMESTAMP,
            PRIMARY KEY (pair_id, version))""",
    ),
    "quant_spread": (
        """CREATE TABLE IF NOT EXISTS quant_spread (
            pair_id TEXT NOT NULL, version TEXT NOT NULL, as_of DATE NOT NULL, hedge_ratio REAL, spread REAL,
            zscore REAL, correlation REAL, adf_t REAL, cointegrated_5pct INTEGER, half_life REAL,
            sessions INTEGER, created_at TIMESTAMP, PRIMARY KEY (pair_id, version, as_of))""",
    ),
    "quant_portfolio": (
        """CREATE TABLE IF NOT EXISTS quant_portfolio (
            portfolio_id TEXT PRIMARY KEY, name TEXT, as_of DATE, spec_json TEXT, method TEXT, long_short TEXT,
            cash REAL, created_at TIMESTAMP)""",
    ),
    "quant_portfolio_position": (
        """CREATE TABLE IF NOT EXISTS quant_portfolio_position (
            portfolio_id TEXT NOT NULL, symbol TEXT NOT NULL, weight REAL, side TEXT, sector TEXT,
            PRIMARY KEY (portfolio_id, symbol))""",
    ),
    "quant_exposure": (
        """CREATE TABLE IF NOT EXISTS quant_exposure (
            id INTEGER PRIMARY KEY AUTOINCREMENT, portfolio_id TEXT, as_of DATE, exposure_json TEXT,
            created_at TIMESTAMP)""",
    ),
    "market_event": (
        """CREATE TABLE IF NOT EXISTS market_event (
            event_id TEXT PRIMARY KEY, source TEXT NOT NULL, source_id TEXT, symbol TEXT, event_type TEXT NOT NULL,
            category TEXT, event_date DATE, known_at DATE NOT NULL, direction TEXT, value REAL, payload_json TEXT,
            created_at TIMESTAMP, UNIQUE (source, source_id))""",
        "CREATE INDEX IF NOT EXISTS idx_market_event_sym ON market_event(symbol, known_at)",
    ),
    "microstructure_feature": (
        """CREATE TABLE IF NOT EXISTS microstructure_feature (
            symbol TEXT NOT NULL, date DATE NOT NULL, feature TEXT NOT NULL, value REAL, source TEXT,
            created_at TIMESTAMP, PRIMARY KEY (symbol, date, feature))""",
    ),
    "derivatives_instrument": (
        """CREATE TABLE IF NOT EXISTS derivatives_instrument (
            instrument_id TEXT PRIMARY KEY, underlying TEXT NOT NULL, instrument_type TEXT NOT NULL,
            expiry DATE, strike REAL, option_type TEXT, lot_size INTEGER, exchange TEXT, source TEXT,
            created_at TIMESTAMP)""",
    ),
    "derivatives_quote": (
        """CREATE TABLE IF NOT EXISTS derivatives_quote (
            instrument_id TEXT NOT NULL, date DATE NOT NULL, open REAL, high REAL, low REAL, close REAL,
            settle REAL, volume INTEGER, open_interest INTEGER, oi_change INTEGER, underlying_close REAL,
            source TEXT, created_at TIMESTAMP, PRIMARY KEY (instrument_id, date))""",
    ),
    "options_analytics": (
        """CREATE TABLE IF NOT EXISTS options_analytics (
            instrument_id TEXT NOT NULL, date DATE NOT NULL, iv REAL, delta REAL, gamma REAL, theta REAL,
            vega REAL, rho REAL, iv_rank REAL, iv_rv_spread REAL, model TEXT, created_at TIMESTAMP,
            PRIMARY KEY (instrument_id, date))""",
    ),
}

# ── Tables added in W7 (enterprise layer) ──────────────────────────────────
# enterprise/. Only digests of session tokens, API keys and reset tokens are
# stored; passwords are PBKDF2 hashes. Tenant-owned W3-W6 tables gain a
# tenant_id column (W3_W4_COLUMNS below) defaulting to 'default', so every
# existing row belongs to the owner's tenant.
W7_TABLES = {
    "enterprise_tenant": (
        """CREATE TABLE IF NOT EXISTS enterprise_tenant (
            tenant_id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL, settings_json TEXT,
            limits_json TEXT, created_at TIMESTAMP, updated_at TIMESTAMP)""",
    ),
    "enterprise_user": (
        """CREATE TABLE IF NOT EXISTS enterprise_user (
            user_id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, email TEXT, display_name TEXT,
            password_hash TEXT NOT NULL, status TEXT NOT NULL, failed_logins INTEGER NOT NULL DEFAULT 0,
            locked_until TIMESTAMP, must_change_password INTEGER NOT NULL DEFAULT 0, preferences_json TEXT,
            created_at TIMESTAMP, updated_at TIMESTAMP, last_login_at TIMESTAMP, created_by TEXT)""",
    ),
    "enterprise_role": (
        """CREATE TABLE IF NOT EXISTS enterprise_role (
            role TEXT PRIMARY KEY, description TEXT, builtin INTEGER NOT NULL DEFAULT 1, created_at TIMESTAMP)""",
    ),
    "enterprise_permission": (
        """CREATE TABLE IF NOT EXISTS enterprise_permission (permission TEXT PRIMARY KEY, description TEXT)""",
    ),
    "enterprise_role_permission": (
        """CREATE TABLE IF NOT EXISTS enterprise_role_permission (
            role TEXT NOT NULL, permission TEXT NOT NULL, PRIMARY KEY (role, permission))""",
    ),
    "enterprise_user_role": (
        """CREATE TABLE IF NOT EXISTS enterprise_user_role (
            tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, role TEXT NOT NULL, granted_by TEXT,
            granted_at TIMESTAMP, PRIMARY KEY (tenant_id, user_id, role))""",
    ),
    "enterprise_session": (
        """CREATE TABLE IF NOT EXISTS enterprise_session (
            token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, tenant_id TEXT NOT NULL, created_at TIMESTAMP,
            expires_at TIMESTAMP, last_seen_at TIMESTAMP, ip TEXT, user_agent TEXT, revoked_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_ent_session_user ON enterprise_session(user_id)",
    ),
    "enterprise_password_reset": (
        """CREATE TABLE IF NOT EXISTS enterprise_password_reset (
            token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at TIMESTAMP, created_by TEXT,
            created_at TIMESTAMP, used_at TIMESTAMP)""",
    ),
    "enterprise_api_key": (
        """CREATE TABLE IF NOT EXISTS enterprise_api_key (
            key_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, tenant_id TEXT NOT NULL, name TEXT,
            key_hash TEXT NOT NULL UNIQUE, scopes_json TEXT, created_at TIMESTAMP, expires_at TIMESTAMP,
            last_used_at TIMESTAMP, revoked_at TIMESTAMP)""",
    ),
    "enterprise_risk_profile": (
        """CREATE TABLE IF NOT EXISTS enterprise_risk_profile (
            scope TEXT NOT NULL, scope_id TEXT NOT NULL, profile_json TEXT, updated_at TIMESTAMP,
            updated_by TEXT, PRIMARY KEY (scope, scope_id))""",
    ),
    "enterprise_notification": (
        """CREATE TABLE IF NOT EXISTS enterprise_notification (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT, user_id TEXT NOT NULL, category TEXT,
            severity TEXT, title TEXT, body TEXT, created_at TIMESTAMP, read_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_ent_notif_user ON enterprise_notification(user_id, read_at)",
    ),
    "enterprise_audit": (
        """CREATE TABLE IF NOT EXISTS enterprise_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, at TIMESTAMP, tenant_id TEXT, user_id TEXT, actor TEXT,
            action TEXT NOT NULL, resource TEXT, method TEXT, path TEXT, status_code INTEGER, ip TEXT,
            details_json TEXT)""",
        "CREATE INDEX IF NOT EXISTS idx_ent_audit_tenant ON enterprise_audit(tenant_id, id)",
    ),
    "enterprise_plan": (
        """CREATE TABLE IF NOT EXISTS enterprise_plan (
            plan_id TEXT PRIMARY KEY, name TEXT, price_month REAL, currency TEXT, limits_json TEXT,
            features_json TEXT, status TEXT)""",
    ),
    "enterprise_subscription": (
        """CREATE TABLE IF NOT EXISTS enterprise_subscription (
            tenant_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, status TEXT NOT NULL, started_at TIMESTAMP,
            current_period_end TIMESTAMP, cancel_at TIMESTAMP, updated_at TIMESTAMP)""",
    ),
    "enterprise_usage": (
        """CREATE TABLE IF NOT EXISTS enterprise_usage (
            tenant_id TEXT NOT NULL, date DATE NOT NULL, metric TEXT NOT NULL, value REAL,
            PRIMARY KEY (tenant_id, date, metric))""",
    ),
    "enterprise_invoice": (
        """CREATE TABLE IF NOT EXISTS enterprise_invoice (
            invoice_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, period_start DATE, period_end DATE,
            plan_id TEXT, amount REAL, currency TEXT, status TEXT NOT NULL, lines_json TEXT, created_at TIMESTAMP)""",
    ),
    "enterprise_watchlist": (
        """CREATE TABLE IF NOT EXISTS enterprise_watchlist (
            watchlist_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, name TEXT,
            symbols_json TEXT, shared INTEGER NOT NULL DEFAULT 0, created_at TIMESTAMP, updated_at TIMESTAMP)""",
    ),
    "enterprise_alert_rule": (
        """CREATE TABLE IF NOT EXISTS enterprise_alert_rule (
            rule_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, name TEXT, symbol TEXT,
            feature TEXT, op TEXT, value REAL, status TEXT, last_value REAL, last_triggered_at TIMESTAMP,
            created_at TIMESTAMP)""",
    ),
    "enterprise_report": (
        """CREATE TABLE IF NOT EXISTS enterprise_report (
            report_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, name TEXT, kind TEXT,
            params_json TEXT, shared INTEGER NOT NULL DEFAULT 0, created_at TIMESTAMP)""",
    ),
}

# ── Tables added in W8 (production hardening, ops/) ────────────────────────
# Secret VALUES are never stored: ops_secret_access holds the name, the source
# it resolved from and the caller; ops_secret_meta the rotation dates.
W8_TABLES = {
    "schema_migrations": (
        """CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TIMESTAMP,
            duration_ms REAL, status TEXT NOT NULL, rollback_note TEXT, error TEXT)""",
    ),
    "ops_config_version": (
        """CREATE TABLE IF NOT EXISTS ops_config_version (
            id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL, environment TEXT,
            config_json TEXT, changes_json TEXT, recorded_at TIMESTAMP)""",
    ),
    "ops_secret_access": (
        """CREATE TABLE IF NOT EXISTS ops_secret_access (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, source TEXT, found INTEGER,
            caller TEXT, at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_ops_secret_access ON ops_secret_access(name, at)",
    ),
    "ops_secret_meta": (
        """CREATE TABLE IF NOT EXISTS ops_secret_meta (
            name TEXT PRIMARY KEY, rotated_at TIMESTAMP, rotated_by TEXT, note TEXT)""",
    ),
    "ops_backup": (
        """CREATE TABLE IF NOT EXISTS ops_backup (
            backup_id TEXT PRIMARY KEY, kind TEXT NOT NULL, path TEXT, started_at TIMESTAMP,
            finished_at TIMESTAMP, size_bytes INTEGER, sha256 TEXT, integrity TEXT, tables_json TEXT,
            status TEXT NOT NULL, error TEXT, pruned_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_ops_backup_status ON ops_backup(status, finished_at)",
    ),
    "ops_job_lock": (
        """CREATE TABLE IF NOT EXISTS ops_job_lock (
            job TEXT PRIMARY KEY, owner TEXT NOT NULL, pid INTEGER, acquired_at TIMESTAMP,
            heartbeat_at TIMESTAMP, expires_at TIMESTAMP)""",
    ),
    "ops_heartbeat": (
        """CREATE TABLE IF NOT EXISTS ops_heartbeat (
            component TEXT PRIMARY KEY, beat_at TIMESTAMP, pid INTEGER, detail TEXT)""",
    ),
    "ops_alert": (
        """CREATE TABLE IF NOT EXISTS ops_alert (
            rule TEXT PRIMARY KEY, status TEXT NOT NULL, severity TEXT, message TEXT, first_at TIMESTAMP,
            last_at TIMESTAMP, resolved_at TIMESTAMP, notified_at TIMESTAMP, count INTEGER NOT NULL DEFAULT 0)""",
    ),
    "ops_idempotency": (
        """CREATE TABLE IF NOT EXISTS ops_idempotency (
            idem_key TEXT NOT NULL, caller TEXT NOT NULL, request_hash TEXT NOT NULL, status TEXT NOT NULL,
            response_status INTEGER, response_body BLOB, content_type TEXT, created_at TIMESTAMP,
            expires_at TIMESTAMP, PRIMARY KEY (idem_key, caller))""",
        "CREATE INDEX IF NOT EXISTS idx_ops_idem_expiry ON ops_idempotency(expires_at)",
    ),
    "ops_webhook_event": (
        """CREATE TABLE IF NOT EXISTS ops_webhook_event (
            source TEXT NOT NULL, event_id TEXT NOT NULL, received_at TIMESTAMP, signature_ok INTEGER,
            status TEXT, payload_sha256 TEXT, event_type TEXT, error TEXT, PRIMARY KEY (source, event_id))""",
    ),
    "ops_webhook_endpoint": (
        """CREATE TABLE IF NOT EXISTS ops_webhook_endpoint (
            endpoint_id TEXT PRIMARY KEY, tenant_id TEXT, url TEXT NOT NULL, events_json TEXT,
            secret_name TEXT, status TEXT NOT NULL, created_at TIMESTAMP, created_by TEXT)""",
    ),
    "ops_webhook_delivery": (
        """CREATE TABLE IF NOT EXISTS ops_webhook_delivery (
            delivery_id TEXT PRIMARY KEY, endpoint_id TEXT NOT NULL, event_type TEXT, event_id TEXT,
            payload_json TEXT, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMP, last_status_code INTEGER, last_error TEXT, created_at TIMESTAMP,
            delivered_at TIMESTAMP, UNIQUE(endpoint_id, event_id))""",
        "CREATE INDEX IF NOT EXISTS idx_ops_wh_delivery ON ops_webhook_delivery(status, next_attempt_at)",
    ),
    "enterprise_refresh_token": (
        """CREATE TABLE IF NOT EXISTS enterprise_refresh_token (
            token_hash TEXT PRIMARY KEY, family_id TEXT NOT NULL, user_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
            created_at TIMESTAMP, expires_at TIMESTAMP, used_at TIMESTAMP, revoked_at TIMESTAMP,
            replaced_by TEXT, ip TEXT)""",
        "CREATE INDEX IF NOT EXISTS idx_ent_refresh_family ON enterprise_refresh_token(family_id)",
    ),
}

# ── Tables added in W11-W20 (wealth track, wealth/) ────────────────────────
# Every row is owned by (tenant_id, owner_id); see wealth/common.py. *_version
# and ledger tables are append-only (migration 0005 adds the triggers).
WEALTH_TABLES = {
    # W11 Investor DNA
    "investor_profile_version": (
        """CREATE TABLE IF NOT EXISTS investor_profile_version (
            profile_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, version INTEGER NOT NULL,
            questionnaire_version TEXT, methodology_version TEXT, answers_json TEXT, answers_hash TEXT,
            result_json TEXT, band TEXT, risk_score REAL, created_at TIMESTAMP, created_by TEXT,
            UNIQUE(tenant_id, owner_id, version))""",
    ),
    "investor_profile": (
        """CREATE TABLE IF NOT EXISTS investor_profile (
            tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, profile_id TEXT NOT NULL, version INTEGER,
            band TEXT, risk_score REAL, mode TEXT, updated_at TIMESTAMP, PRIMARY KEY (tenant_id, owner_id))""",
    ),
    # W12 Multi-asset wealth
    "wealth_holding": (
        """CREATE TABLE IF NOT EXISTS wealth_holding (
            holding_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, asset_class TEXT NOT NULL,
            instrument TEXT NOT NULL, valuation TEXT NOT NULL, name TEXT NOT NULL, symbol TEXT, quantity REAL NOT NULL,
            unit TEXT, avg_cost REAL, currency TEXT DEFAULT 'INR', fx_rate REAL, manual_price REAL,
            manual_price_as_of DATE, maturity_date DATE, coupon_pct REAL, notes TEXT, status TEXT NOT NULL,
            created_at TIMESTAMP, updated_at TIMESTAMP, updated_by TEXT)""",
        "CREATE INDEX IF NOT EXISTS idx_wealth_holding_owner ON wealth_holding(tenant_id, owner_id, status)",
    ),
    "wealth_liability": (
        """CREATE TABLE IF NOT EXISTS wealth_liability (
            liability_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, kind TEXT NOT NULL,
            name TEXT, outstanding REAL NOT NULL, interest_pct REAL, emi REAL, end_date DATE, notes TEXT,
            status TEXT NOT NULL, updated_at TIMESTAMP, updated_by TEXT)""",
        "CREATE INDEX IF NOT EXISTS idx_wealth_liability_owner ON wealth_liability(tenant_id, owner_id, status)",
    ),
    "wealth_classification": (
        """CREATE TABLE IF NOT EXISTS wealth_classification (
            tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, symbol TEXT NOT NULL, asset_class TEXT NOT NULL,
            instrument TEXT, updated_at TIMESTAMP, PRIMARY KEY (tenant_id, owner_id, symbol))""",
    ),
    "wealth_snapshot": (
        """CREATE TABLE IF NOT EXISTS wealth_snapshot (
            tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, date DATE NOT NULL, net_worth REAL, gross_assets REAL,
            liabilities REAL, invested_cost REAL, by_class_json TEXT, positions INTEGER, created_at TIMESTAMP,
            PRIMARY KEY (tenant_id, owner_id, date))""",
    ),
    # W13 Goal planning
    "wealth_goal": (
        """CREATE TABLE IF NOT EXISTS wealth_goal (
            goal_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, name TEXT NOT NULL,
            goal_type TEXT NOT NULL, priority TEXT NOT NULL, target_amount REAL, target_date DATE NOT NULL,
            inflation_pct REAL, current_amount REAL, linked_json TEXT, monthly_contribution REAL, step_up_pct REAL,
            expected_return_pct REAL, volatility_pct REAL, retirement_monthly_expense REAL, years_in_retirement REAL,
            post_retirement_return_pct REAL, emergency_months REAL, notes TEXT, status TEXT NOT NULL,
            created_at TIMESTAMP, updated_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_wealth_goal_owner ON wealth_goal(tenant_id, owner_id, status)",
    ),
    "wealth_goal_event": (
        """CREATE TABLE IF NOT EXISTS wealth_goal_event (
            id INTEGER PRIMARY KEY AUTOINCREMENT, goal_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
            owner_id TEXT NOT NULL, at TIMESTAMP, kind TEXT NOT NULL, details_json TEXT, actor TEXT)""",
        "CREATE INDEX IF NOT EXISTS idx_wealth_goal_event ON wealth_goal_event(goal_id, id)",
    ),
    "wealth_goal_projection": (
        """CREATE TABLE IF NOT EXISTS wealth_goal_projection (
            goal_id TEXT NOT NULL, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, as_of DATE NOT NULL,
            status TEXT, success_probability REAL, projected REAL, future_target REAL, gap REAL, result_json TEXT,
            methodology_version TEXT, created_at TIMESTAMP, PRIMARY KEY (goal_id, as_of))""",
    ),
    # W14 Asset allocation
    "wealth_allocation_policy": (
        """CREATE TABLE IF NOT EXISTS wealth_allocation_policy (
            tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, policy_json TEXT, updated_at TIMESTAMP,
            PRIMARY KEY (tenant_id, owner_id))""",
    ),
    "wealth_allocation_run": (
        """CREATE TABLE IF NOT EXISTS wealth_allocation_run (
            run_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, as_of DATE,
            methodology_version TEXT, profile_id TEXT, band TEXT, target_json TEXT, result_json TEXT,
            inputs_hash TEXT, created_at TIMESTAMP, created_by TEXT)""",
        "CREATE INDEX IF NOT EXISTS idx_wealth_alloc_owner ON wealth_allocation_run(tenant_id, owner_id, created_at)",
    ),
    # W15 Rebalancing
    "wealth_rebalance_plan": (
        """CREATE TABLE IF NOT EXISTS wealth_rebalance_plan (
            plan_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, created_at TIMESTAMP,
            created_by TEXT, mode TEXT, new_cash REAL, target_run_id TEXT, verdict TEXT, status TEXT NOT NULL,
            result_json TEXT, methodology_version TEXT, decided_at TIMESTAMP, decided_by TEXT, decision_note TEXT)""",
        "CREATE INDEX IF NOT EXISTS idx_wealth_rbl_owner ON wealth_rebalance_plan(tenant_id, owner_id, created_at)",
    ),
    # W15.5 Performance attribution
    "perf_ledger": (
        """CREATE TABLE IF NOT EXISTS perf_ledger (
            txn_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, portfolio TEXT NOT NULL,
            source TEXT NOT NULL, source_ref TEXT NOT NULL, trade_date DATE NOT NULL, ts TEXT, kind TEXT NOT NULL,
            symbol TEXT, quantity REAL, price REAL, gross_value REAL, fees REAL, reference_price REAL,
            price_quality TEXT, strategy_id TEXT, tag TEXT, note TEXT, created_at TIMESTAMP, import_run TEXT,
            UNIQUE(tenant_id, owner_id, source, source_ref))""",
        "CREATE INDEX IF NOT EXISTS idx_perf_ledger_pf ON perf_ledger(tenant_id, owner_id, portfolio, trade_date)",
    ),
    "perf_ledger_void": (
        """CREATE TABLE IF NOT EXISTS perf_ledger_void (
            txn_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, voided_at TIMESTAMP,
            voided_by TEXT, reason TEXT)""",
    ),
    "perf_report_run": (
        """CREATE TABLE IF NOT EXISTS perf_report_run (
            report_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, portfolio TEXT,
            period_start DATE, period_end DATE, benchmark TEXT, methodology_version TEXT, calculation_version TEXT,
            inputs_hash TEXT, result_json TEXT, created_at TIMESTAMP, created_by TEXT)""",
        "CREATE INDEX IF NOT EXISTS idx_perf_report_owner ON perf_report_run(tenant_id, owner_id, created_at)",
    ),
    # W16 Advisor
    "wealth_advice_log": (
        """CREATE TABLE IF NOT EXISTS wealth_advice_log (
            advice_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, asked_at TIMESTAMP,
            asked_by TEXT, question TEXT, topic TEXT, response_json TEXT, narration_status TEXT,
            narration_model TEXT, feedback TEXT, feedback_note TEXT)""",
        "CREATE INDEX IF NOT EXISTS idx_wealth_advice_owner ON wealth_advice_log(tenant_id, owner_id, asked_at)",
    ),
    # W17 Integrated intelligence
    "wealth_cycle_run": (
        """CREATE TABLE IF NOT EXISTS wealth_cycle_run (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, run_date DATE,
            status TEXT, result_json TEXT, created_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_wealth_cycle_owner ON wealth_cycle_run(tenant_id, owner_id, created_at)",
    ),
    # W19 Beta / UAT feedback
    "wealth_feedback": (
        """CREATE TABLE IF NOT EXISTS wealth_feedback (
            feedback_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, created_at TIMESTAMP,
            created_by TEXT, page TEXT, category TEXT, severity TEXT, message TEXT, context_json TEXT,
            status TEXT NOT NULL, triage_note TEXT, triaged_at TIMESTAMP, triaged_by TEXT)""",
        "CREATE INDEX IF NOT EXISTS idx_wealth_feedback_status ON wealth_feedback(status, severity)",
    ),
    "wealth_preference": (
        """CREATE TABLE IF NOT EXISTS wealth_preference (
            tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT, updated_at TIMESTAMP,
            PRIMARY KEY (tenant_id, owner_id, key))""",
    ),
}

# ── Tables added in W21 (data & technical analysis) ─────────────────────────
W21_TABLES = {
    # DP-03: intraday bars the 30-minute job fetched and used to discard
    "intraday_bars": (
        """CREATE TABLE IF NOT EXISTS intraday_bars (
            symbol TEXT NOT NULL, ts TIMESTAMP NOT NULL, interval_min INTEGER NOT NULL, open REAL, high REAL,
            low REAL, close REAL, volume INTEGER, source TEXT DEFAULT 'dhan', created_at TIMESTAMP,
            PRIMARY KEY (symbol, interval_min, ts))""",
        "CREATE INDEX IF NOT EXISTS idx_intraday_bars_ts ON intraday_bars(ts)",
    ),
    # TA-03/04/06/09/10/11, SC-16: data/technical_ext.py
    "technical_ext": (
        """CREATE TABLE IF NOT EXISTS technical_ext (
            symbol TEXT NOT NULL, date DATE NOT NULL,
            vwap_20d REAL, vwap_session REAL, vwap_dev_pct REAL, vp_poc REAL, vp_vah REAL, vp_val REAL, vp_basis TEXT,
            sr_support REAL, sr_support_touches INTEGER, sr_support_dist_pct REAL, sr_resistance REAL,
            sr_resistance_touches INTEGER, sr_resistance_dist_pct REAL, beta_60 REAL, beta_downside REAL,
            beta_long REAL, beta_long_sessions INTEGER, bri REAL, weekly_rsi REAL, weekly_trend TEXT,
            monthly_trend TEXT, mtf_alignment INTEGER, supertrend REAL, supertrend_dir INTEGER,
            ichimoku_tenkan REAL, ichimoku_kijun REAL, ichimoku_span_a REAL, ichimoku_span_b REAL,
            keltner_upper REAL, keltner_lower REAL, donchian_upper REAL, donchian_lower REAL, mfi_14 REAL,
            cmf_20 REAL, roc_10 REAL, aroon_up REAL, aroon_down REAL, psar REAL, created_at TIMESTAMP,
            PRIMARY KEY (symbol, date))""",
        "CREATE INDEX IF NOT EXISTS idx_technical_ext_date ON technical_ext(date)",
    ),
    # DP-13: data/market_series.py
    "sector_breadth": (
        """CREATE TABLE IF NOT EXISTS sector_breadth (
            date DATE NOT NULL, sector TEXT NOT NULL, stocks INTEGER, pct_advancing REAL, avg_return_pct REAL,
            pct_above_50dma REAL, pct_above_200dma REAL, created_at TIMESTAMP, PRIMARY KEY (date, sector))""",
    ),
}

# ── Tables added in W22 (factor research platform) ─────────────────────────
W22_TABLES = {
    "score_components": (                    # AF-03: the sub-factors of each ATIP index
        """CREATE TABLE IF NOT EXISTS score_components (
            symbol TEXT NOT NULL, date DATE NOT NULL, index_name TEXT NOT NULL, component TEXT NOT NULL,
            value REAL, weight REAL, PRIMARY KEY (symbol, date, index_name, component))""",
        "CREATE INDEX IF NOT EXISTS idx_score_components_date ON score_components(date, index_name)",
    ),
    "formula_registry": (                    # SC-18: weights_hash -> the weights it stood for
        """CREATE TABLE IF NOT EXISTS formula_registry (
            weights_hash TEXT PRIMARY KEY, weights_json TEXT NOT NULL, notes_json TEXT, code_version TEXT,
            first_seen_at TIMESTAMP)""",
    ),
    "quant_factor_approval": (               # AF-08: append-only decision history
        """CREATE TABLE IF NOT EXISTS quant_factor_approval (
            id INTEGER PRIMARY KEY AUTOINCREMENT, factor_key TEXT NOT NULL, decision TEXT NOT NULL, verdict TEXT,
            evidence_json TEXT, reason TEXT, decided_at TIMESTAMP, decided_by TEXT)""",
        "CREATE INDEX IF NOT EXISTS idx_qfa_key ON quant_factor_approval(factor_key, id)",
    ),
    "research_study": (                      # DBS-07
        """CREATE TABLE IF NOT EXISTS research_study (
            study_id TEXT PRIMARY KEY, title TEXT NOT NULL, hypothesis TEXT NOT NULL, method TEXT, status TEXT NOT NULL,
            outcome TEXT, conclusion TEXT, tags_json TEXT, supersedes TEXT, created_at TIMESTAMP,
            updated_at TIMESTAMP, created_by TEXT, concluded_by TEXT)""",
    ),
    "research_link": (
        """CREATE TABLE IF NOT EXISTS research_link (
            study_id TEXT NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL, note TEXT, added_at TIMESTAMP,
            added_by TEXT, PRIMARY KEY (study_id, kind, ref))""",
    ),
}

# ── Tables added in W24 (machine learning) ─────────────────────────────────
W24_TABLES = {
    "ml_validation_report": (          # ML-08 walk-forward reports
        """CREATE TABLE IF NOT EXISTS ml_validation_report (
            report_id TEXT PRIMARY KEY, model_type TEXT, dataset_id TEXT, label_json TEXT, params_json TEXT,
            report_json TEXT, verdict TEXT, created_at TIMESTAMP)""",
    ),
    "ml_cluster_run": (                # ML-03
        """CREATE TABLE IF NOT EXISTS ml_cluster_run (
            run_id TEXT PRIMARY KEY, as_of DATE, method TEXT, k INTEGER, summary_json TEXT, created_at TIMESTAMP)""",
    ),
    "ml_cluster": (
        """CREATE TABLE IF NOT EXISTS ml_cluster (
            run_id TEXT NOT NULL, as_of DATE, symbol TEXT NOT NULL, cluster INTEGER,
            PRIMARY KEY (run_id, symbol))""",
    ),
    "ml_anomaly": (                    # ML-06
        """CREATE TABLE IF NOT EXISTS ml_anomaly (
            as_of DATE NOT NULL, symbol TEXT NOT NULL, kind TEXT, score REAL, detail_json TEXT, created_at TIMESTAMP,
            PRIMARY KEY (as_of, symbol))""",
    ),
    "ml_health_check": (               # ML-09 / MON-05
        """CREATE TABLE IF NOT EXISTS ml_health_check (
            id INTEGER PRIMARY KEY AUTOINCREMENT, checked_at TIMESTAMP, status TEXT, result_json TEXT)""",
    ),
}

# ── Tables added in W25 (portfolio risk) ──────────────────────────────────
W25_TABLES = {
    "portfolio_risk_snapshot": (       # PF-03/04/07/09, RK-11/12: post-market risk of each book
        """CREATE TABLE IF NOT EXISTS portfolio_risk_snapshot (
            as_of DATE NOT NULL, book TEXT NOT NULL, headline_json TEXT, analysis_json TEXT, created_at TIMESTAMP,
            PRIMARY KEY (as_of, book))""",
    ),
    "portfolio_optimization": (        # PF-10
        """CREATE TABLE IF NOT EXISTS portfolio_optimization (
            opt_id TEXT PRIMARY KEY, as_of DATE, objective TEXT, result_json TEXT, created_at TIMESTAMP)""",
    ),
    "portfolio_rebalance_plan": (      # PF-06
        """CREATE TABLE IF NOT EXISTS portfolio_rebalance_plan (
            plan_id TEXT PRIMARY KEY, book TEXT, as_of DATE, plan_json TEXT, created_at TIMESTAMP)""",
    ),
    "risk_emergency_exit": (           # RK-16
        """CREATE TABLE IF NOT EXISTS risk_emergency_exit (
            run_id TEXT PRIMARY KEY, book TEXT, actor TEXT, reason TEXT, status TEXT, result_json TEXT,
            created_at TIMESTAMP)""",
    ),
}

# ── Tables added in W27 (data & scores) ───────────────────────────────────
W27_TABLES = {
    "fundamental_filing": (            # DP-15: one NSE integrated-filing XBRL, parsed (point in time)
        """CREATE TABLE IF NOT EXISTS fundamental_filing (
            xbrl_url TEXT PRIMARY KEY, symbol TEXT NOT NULL, period_end DATE NOT NULL, nature TEXT NOT NULL,
            audited TEXT, broadcast_at TIMESTAMP, facts_json TEXT, status TEXT NOT NULL DEFAULT 'PARSED',
            error TEXT, fetched_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_ff_symbol_period ON fundamental_filing(symbol, period_end)",
    ),
    "shareholding_pattern": (          # DP-16: quarterly shareholding pattern (NSE SHP XBRL)
        """CREATE TABLE IF NOT EXISTS shareholding_pattern (
            symbol TEXT NOT NULL, as_of DATE NOT NULL, promoter_pct REAL, public_pct REAL, mf_pct REAL,
            fpi_pct REAL, insurance_pct REAL, dii_pct REAL, retail_pct REAL, pledged_pct REAL,
            submitted_at TIMESTAMP, xbrl_url TEXT, source TEXT DEFAULT 'nse_shp', fetched_at TIMESTAMP,
            PRIMARY KEY (symbol, as_of))""",
    ),
    "insider_trade": (                 # DP-16: SEBI PIT disclosures (NSE corporates-pit)
        """CREATE TABLE IF NOT EXISTS insider_trade (
            disclosure_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, person TEXT, person_category TEXT,
            txn_type TEXT, security_type TEXT, qty REAL, value_rs REAL, mode TEXT, txn_from DATE, txn_to DATE,
            disclosed_at TIMESTAMP, post_pct REAL, fetched_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_insider_symbol ON insider_trade(symbol, disclosed_at)",
    ),
    "sast_disclosure": (               # DP-16: SAST Reg 29 acquisitions / disposals
        """CREATE TABLE IF NOT EXISTS sast_disclosure (
            disclosure_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, acquirer TEXT, is_promoter INTEGER,
            txn_type TEXT, shares_acq REAL, shares_sold REAL, post_pct REAL, disclosed_at TIMESTAMP,
            fetched_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_sast_symbol ON sast_disclosure(symbol, disclosed_at)",
    ),
    "fo_underlying_daily": (           # DP-08 (partial) / SC-06: per-underlying F&O summary from NSE F&O bhavcopy
        """CREATE TABLE IF NOT EXISTS fo_underlying_daily (
            date DATE NOT NULL, symbol TEXT NOT NULL, kind TEXT NOT NULL, underlying_price REAL,
            fut_close REAL, fut_oi REAL, fut_oi_chg REAL, fut_volume REAL,
            call_oi REAL, put_oi REAL, call_oi_chg REAL, put_oi_chg REAL, call_volume REAL, put_volume REAL,
            pcr_oi REAL, pcr_volume REAL, max_pain REAL, near_expiry DATE, created_at TIMESTAMP,
            PRIMARY KEY (date, symbol))""",
    ),
    "global_market_history": (         # DP-12: daily close history of every global series (incl. yields)
        """CREATE TABLE IF NOT EXISTS global_market_history (
            series TEXT NOT NULL, date DATE NOT NULL, close REAL, source TEXT DEFAULT 'yfinance',
            PRIMARY KEY (series, date))""",
    ),
    "live_feed_status": (              # DP-01: stock feed health (one row per feed, overwritten)
        """CREATE TABLE IF NOT EXISTS live_feed_status (
            feed TEXT PRIMARY KEY, mode TEXT, subscribed INTEGER, ticks INTEGER, last_tick_at TIMESTAMP,
            last_flush_at TIMESTAMP, detail TEXT, updated_at TIMESTAMP)""",
    ),
}

# ── Tables added in W28 (strategy & AI) ───────────────────────────────────
W28_TABLES = {
    "ai_usage_log": (                  # NS-02..04: every Anthropic request, tokens + cost (daily cap)
        """CREATE TABLE IF NOT EXISTS ai_usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, day DATE NOT NULL, created_at TIMESTAMP, purpose TEXT, model TEXT,
            input_tokens INTEGER, output_tokens INTEGER, cache_write_tokens INTEGER, cache_read_tokens INTEGER,
            cost_usd REAL, ok INTEGER, error TEXT)""",
        "CREATE INDEX IF NOT EXISTS idx_ai_usage_day ON ai_usage_log(day)",
    ),
    "news_summary": (                  # NS-04 / DB-06
        """CREATE TABLE IF NOT EXISTS news_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TIMESTAMP, window_hours INTEGER, article_count INTEGER,
            classifier TEXT, model TEXT, summary_json TEXT, fallback_reason TEXT)""",
    ),
    "intraday_scan_hit": (             # SG-08
        """CREATE TABLE IF NOT EXISTS intraday_scan_hit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, run_at TIMESTAMP, session DATE, scan TEXT,
            symbol TEXT, price REAL, score REAL, details_json TEXT)""",
        "CREATE INDEX IF NOT EXISTS idx_scan_hit_session ON intraday_scan_hit(session, scan)",
    ),
}

# ── Tables added in W29 (execution) ───────────────────────────────────────
W29_TABLES = {
    "reconciliation_run": (            # BR-05
        """CREATE TABLE IF NOT EXISTS reconciliation_run (
            run_id TEXT PRIMARY KEY, trade_date DATE, run_at TIMESTAMP, status TEXT, breaks INTEGER,
            explained INTEGER, details_json TEXT)""",
    ),
    "broker_health_check": (           # BR-06 / RK-17
        """CREATE TABLE IF NOT EXISTS broker_health_check (
            id INTEGER PRIMARY KEY AUTOINCREMENT, checked_at TIMESTAMP NOT NULL, overall TEXT NOT NULL,
            in_session INTEGER, checks_json TEXT)""",
        "CREATE INDEX IF NOT EXISTS idx_broker_health_at ON broker_health_check(checked_at)",
    ),
    "live_pnl_snapshot": (             # MON-04: intraday book P&L every 15 min
        """CREATE TABLE IF NOT EXISTS live_pnl_snapshot (
            ts TIMESTAMP NOT NULL, book TEXT NOT NULL, value REAL, day_pnl REAL, unrealized REAL, positions INTEGER,
            PRIMARY KEY (ts, book))""",
    ),
    "audit_export": (                  # SEC-03: one row per exported audit segment
        """CREATE TABLE IF NOT EXISTS audit_export (
            export_id TEXT PRIMARY KEY, source TEXT NOT NULL, first_id INTEGER, last_id INTEGER, rows INTEGER,
            path TEXT, offbox_path TEXT, sha256 TEXT, chain_ok INTEGER, created_at TIMESTAMP)""",
    ),
}

# ── Tables added in W30 (advanced quant) ──────────────────────────────────
W30_TABLES = {
    "paper_futures_position": (        # QR-05 / QR-06: short legs via stock futures (paper)
        """CREATE TABLE IF NOT EXISTS paper_futures_position (
            id INTEGER PRIMARY KEY AUTOINCREMENT, strategy_id TEXT NOT NULL DEFAULT '', underlying TEXT NOT NULL,
            expiry DATE NOT NULL, lots INTEGER NOT NULL, lot_size INTEGER NOT NULL, avg_price REAL NOT NULL,
            realized_pnl REAL NOT NULL DEFAULT 0, margin_blocked REAL NOT NULL DEFAULT 0, opened_at TIMESTAMP,
            updated_at TIMESTAMP, UNIQUE(strategy_id, underlying, expiry))""",
    ),
    "paper_futures_trade": (
        """CREATE TABLE IF NOT EXISTS paper_futures_trade (
            trade_id TEXT PRIMARY KEY, order_id TEXT, strategy_id TEXT, underlying TEXT, expiry DATE, side TEXT,
            lots INTEGER, lot_size INTEGER, price REAL, fees REAL, reason TEXT, at TIMESTAMP)""",
    ),
    "ops_restore_drill": (             # W31 (OPS-06): scheduled restore drills
        """CREATE TABLE IF NOT EXISTS ops_restore_drill (
            drill_id TEXT PRIMARY KEY, backup_id TEXT, source TEXT, started_at TIMESTAMP, finished_at TIMESTAMP,
            seconds REAL, status TEXT, details_json TEXT)""",
    ),
    "ops_rollback_drill": (            # W31 (OPS-11): executed rollback drills (scratch clone)
        """CREATE TABLE IF NOT EXISTS ops_rollback_drill (
            drill_id TEXT PRIMARY KEY, from_ref TEXT, to_ref TEXT, started_at TIMESTAMP, finished_at TIMESTAMP,
            rto_seconds REAL, status TEXT, details_json TEXT)""",
    ),
    "event_study": (                   # QR-09: stored event-study results
        """CREATE TABLE IF NOT EXISTS event_study (
            study_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, period TEXT, params_json TEXT, result_json TEXT,
            created_at TIMESTAMP)""",
    ),
}

# ── Tables added in W9 (enterprise SaaS) ───────────────────────────────────
# Per-tenant paper books, credential vault (ciphertext only), onboarding,
# payments (SANDBOX by default), notification preferences / deliveries,
# report outputs, consent and privacy requests, per-API-key usage.
W32_TABLES = {
    # SEC-02 MFA recovery codes: sha256 digests only, one-time (enterprise/w32.py)
    "enterprise_mfa_recovery": (
        """CREATE TABLE IF NOT EXISTS enterprise_mfa_recovery (
            code_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at TIMESTAMP, used_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_mfa_recovery_user ON enterprise_mfa_recovery(user_id)",
    ),
}

W9_TABLES = {
    "tenant_paper_account": (
        """CREATE TABLE IF NOT EXISTS tenant_paper_account (
            tenant_id TEXT PRIMARY KEY, starting_cash REAL NOT NULL, cash REAL NOT NULL, realized_pnl REAL DEFAULT 0,
            peak_equity REAL, created_at TIMESTAMP, updated_at TIMESTAMP)""",
    ),
    "tenant_paper_position": (
        """CREATE TABLE IF NOT EXISTS tenant_paper_position (
            tenant_id TEXT NOT NULL, symbol TEXT NOT NULL, quantity INTEGER NOT NULL, avg_price REAL NOT NULL,
            realized_pnl REAL DEFAULT 0, updated_at TIMESTAMP, PRIMARY KEY (tenant_id, symbol))""",
    ),
    "tenant_paper_fill": (
        """CREATE TABLE IF NOT EXISTS tenant_paper_fill (
            fill_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, order_id TEXT, symbol TEXT NOT NULL, side TEXT NOT NULL,
            quantity INTEGER NOT NULL, price REAL NOT NULL, fees REAL, filled_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_tpf_tenant ON tenant_paper_fill(tenant_id, filled_at)",
    ),
    "tenant_pnl_daily": (
        """CREATE TABLE IF NOT EXISTS tenant_pnl_daily (
            tenant_id TEXT NOT NULL, date DATE NOT NULL, equity REAL, cash REAL, positions_value REAL, day_pnl REAL,
            peak_equity REAL, drawdown_pct REAL, recorded_at TIMESTAMP, PRIMARY KEY (tenant_id, date))""",
    ),
    "enterprise_vault_credential": (
        """CREATE TABLE IF NOT EXISTS enterprise_vault_credential (
            credential_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, broker TEXT NOT NULL,
            label TEXT, secret_enc TEXT NOT NULL, field_names_json TEXT, key_id TEXT, status TEXT NOT NULL,
            created_at TIMESTAMP, updated_at TIMESTAMP, rotated_at TIMESTAMP, last_accessed_at TIMESTAMP,
            access_count INTEGER NOT NULL DEFAULT 0, UNIQUE(tenant_id, user_id, broker, label))""",
    ),
    "enterprise_onboarding": (
        """CREATE TABLE IF NOT EXISTS enterprise_onboarding (
            tenant_id TEXT PRIMARY KEY, steps_json TEXT, completed_at TIMESTAMP, updated_at TIMESTAMP)""",
    ),
    "enterprise_payment": (
        """CREATE TABLE IF NOT EXISTS enterprise_payment (
            payment_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, invoice_id TEXT, provider TEXT NOT NULL,
            amount REAL, currency TEXT, status TEXT NOT NULL, provider_ref TEXT, idempotency_key TEXT UNIQUE,
            error TEXT, created_at TIMESTAMP, updated_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_ent_payment_tenant ON enterprise_payment(tenant_id, created_at)",
    ),
    "enterprise_notification_pref": (
        """CREATE TABLE IF NOT EXISTS enterprise_notification_pref (
            tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, category TEXT NOT NULL, channels_json TEXT,
            mode TEXT NOT NULL DEFAULT 'immediate', quiet_start TEXT, quiet_end TEXT,
            unsubscribed INTEGER NOT NULL DEFAULT 0, updated_at TIMESTAMP, PRIMARY KEY (tenant_id, user_id, category))""",
    ),
    "enterprise_notification_delivery": (
        """CREATE TABLE IF NOT EXISTS enterprise_notification_delivery (
            delivery_id TEXT PRIMARY KEY, tenant_id TEXT, notification_id INTEGER, user_id TEXT NOT NULL,
            channel TEXT NOT NULL, destination_masked TEXT, subject TEXT, status TEXT NOT NULL, mode TEXT,
            error TEXT, created_at TIMESTAMP, sent_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_ent_ndel_status ON enterprise_notification_delivery(status, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_ent_ndel_user ON enterprise_notification_delivery(user_id, created_at)",
    ),
    "enterprise_email_token": (
        """CREATE TABLE IF NOT EXISTS enterprise_email_token (
            token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, purpose TEXT NOT NULL, email TEXT,
            expires_at TIMESTAMP, used_at TIMESTAMP, created_at TIMESTAMP)""",
    ),
    "enterprise_report_output": (
        """CREATE TABLE IF NOT EXISTS enterprise_report_output (
            output_id TEXT PRIMARY KEY, report_id TEXT NOT NULL, tenant_id TEXT NOT NULL, user_id TEXT,
            format TEXT NOT NULL, content TEXT, rows INTEGER, created_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_ent_rout_report ON enterprise_report_output(report_id, created_at)",
    ),
    "enterprise_consent": (
        """CREATE TABLE IF NOT EXISTS enterprise_consent (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT, user_id TEXT NOT NULL, document TEXT NOT NULL,
            version TEXT NOT NULL, accepted_at TIMESTAMP, ip TEXT, UNIQUE(user_id, document, version))""",
    ),
    "enterprise_privacy_request": (
        """CREATE TABLE IF NOT EXISTS enterprise_privacy_request (
            request_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, kind TEXT NOT NULL,
            status TEXT NOT NULL, reason TEXT, requested_at TIMESTAMP, decided_by TEXT, decided_at TIMESTAMP,
            completed_at TIMESTAMP, result_json TEXT)""",
    ),
    "enterprise_api_usage": (
        """CREATE TABLE IF NOT EXISTS enterprise_api_usage (
            key_id TEXT NOT NULL, tenant_id TEXT NOT NULL, date DATE NOT NULL, calls INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (key_id, date))""",
    ),
}

# Columns added after a table first shipped (applied by get_connection with
# _add_missing_columns; fresh installs get them from the CREATE above).
W3_W4_COLUMNS = {
    "strategy_decision": {"reason_codes_json": "TEXT", "signal_source": "TEXT"},
    "strategy_position_intent": {"entry_reference": "REAL", "book": "TEXT", "risk_decision_id": "TEXT",
                                 "authorized_at": "TIMESTAMP", "tenant_id": "TEXT DEFAULT 'default'"},
    # W7: tenant ownership of tenant-owned W3-W6 rows (existing rows -> 'default')
    "strategy": {"tenant_id": "TEXT DEFAULT 'default'"},
    "risk_decision": {"tenant_id": "TEXT DEFAULT 'default'"},
    "oms_order": {"tenant_id": "TEXT DEFAULT 'default'",
                  # W29: per-order type (EX-02), modify (EX-08), protective child stops; W30: instrument
                  "trigger_price": "REAL", "parent_order_id": "TEXT", "modified_count": "INTEGER DEFAULT 0",
                  "instrument": "TEXT DEFAULT 'CASH'"},
    "ml_model": {"tenant_id": "TEXT DEFAULT 'default'"},
    "quant_pair": {"tenant_id": "TEXT DEFAULT 'default'"},
    "quant_experiment": {"tenant_id": "TEXT DEFAULT 'default'"},
    "quant_portfolio": {"tenant_id": "TEXT DEFAULT 'default'"},
    # W8: MFA (TOTP secret encrypted with ops/crypto.py) and the audit hash chain
    "enterprise_user": {"mfa_enabled": "INTEGER NOT NULL DEFAULT 0", "mfa_secret_enc": "TEXT",
                        "mfa_pending_enc": "TEXT", "email_verified_at": "TIMESTAMP", "deleted_at": "TIMESTAMP"},
    "enterprise_audit": {"prev_hash": "TEXT", "row_hash": "TEXT"},
    # W27: fundamentals from NSE filings -- point-in-time availability, SPI stored beside FS,
    # and which inputs each score actually used
    "fundamental_data": {"period_end": "DATE", "available_from": "TIMESTAMP", "nature": "TEXT",
                         "eps_q": "REAL", "spi_score": "REAL", "score_inputs": "TEXT", "mf_hold": "REAL",
                         "fpi_hold": "REAL", "shares_out": "REAL", "equity_cr": "REAL", "debt_cr": "REAL",
                         "profit_fy_cr": "REAL"},
    "institutional_data": {"ins_components": "TEXT"},
    # W29 / W30 columns of oms_order live in the single "oms_order" entry above (a second
    # key here silently replaced W7's tenant_id -- fixed in W32)
    # W30 (QR-10 / QR-05): implied volatility from option settle prices; lot size for futures shorts
    "live_quotes": {"buy_qty": "REAL", "sell_qty": "REAL"},        # W30 (AF-07): stock-feed order imbalance
    # W31 (OPS-06): encrypted off-site copy of each verified backup
    "ops_backup": {"offsite_path": "TEXT", "offsite_status": "TEXT", "encrypted_sha256": "TEXT"},
    "fo_underlying_daily": {"atm_iv": "REAL", "iv_call_atm": "REAL", "iv_put_atm": "REAL", "iv_skew": "REAL",
                            "iv_expiry": "DATE", "iv_dte": "INTEGER", "lot_size": "INTEGER"},
    "global_markets": {"us_3m": "REAL", "us_3m_chg": "REAL", "us_5y": "REAL", "us_5y_chg": "REAL",
                       "us_30y": "REAL", "us_30y_chg": "REAL"},
    # W9: tenant ownership of the remaining tenant-owned tables, e-mail verification,
    # API-key limits, billing dunning, report schedules
    "backtest_run": {"tenant_id": "TEXT DEFAULT 'default'"},
    "ml_dataset": {"tenant_id": "TEXT DEFAULT 'default'"},
    "quant_factor_research": {"tenant_id": "TEXT DEFAULT 'default'"},
    "enterprise_api_key": {"rate_limit_per_minute": "INTEGER", "daily_quota": "INTEGER"},
    "enterprise_subscription": {"dunning_state": "TEXT", "grace_until": "TIMESTAMP", "payment_provider": "TEXT"},
    "enterprise_invoice": {"due_date": "DATE", "paid_at": "TIMESTAMP", "payment_id": "TEXT", "finalized_at": "TIMESTAMP"},
    "enterprise_report": {"schedule": "TEXT", "formats_json": "TEXT", "last_run_at": "TIMESTAMP"},
}

# Every alert ATIP raises, whether or not Telegram delivered it: the dashboard
# shows this table, so an unconfigured bot no longer means an alert went nowhere
# (every alert before 2026-09-24 did). key de-duplicates within a day.
ALERT_LOG_DDL = (
    """CREATE TABLE IF NOT EXISTS alert_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TIMESTAMP NOT NULL,
        category TEXT NOT NULL, severity TEXT NOT NULL DEFAULT 'info',
        title TEXT, message TEXT NOT NULL, dedupe_key TEXT,
        telegram_sent INTEGER NOT NULL DEFAULT 0, telegram_error TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_alert_log_created ON alert_log(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_alert_log_key ON alert_log(dedupe_key, created_at)",
)

def _migrate_alert_log_table(conn):
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='alert_log'").fetchone():
            for ddl in ALERT_LOG_DDL:
                conn.execute(ddl)
            conn.commit()
    except sqlite3.OperationalError as e:
        log.warning(f"  alert_log migration skipped: {e}")

def _migrate_pipeline_log_columns(conn):
    """pipeline_log.kind ('run' / 'step') and duration_s, plus an index for the
    per-job lookups job-health monitoring makes (MON-01/02)."""
    if _add_missing_columns(conn, "pipeline_log", {"kind": "TEXT", "duration_s": "REAL"}):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pipeline_log_job ON pipeline_log(job_name, start_time)")
        conn.commit()

# market_health columns added for market breadth (DP-17) and for recording
# which Market Health inputs were actually present (SC-09). breadth holds the %
# of the tracked universe above its 200-DMA, adv_decline the advance/decline
# ratio -- both declared from the start and never written until now.
MARKET_HEALTH_ADDED_COLUMNS = {
    "advances": "INTEGER", "declines": "INTEGER", "pct_advancing": "REAL",
    "new_highs": "INTEGER", "new_lows": "INTEGER", "breadth_universe": "INTEGER",
    "mh_coverage": "REAL", "mh_inputs": "TEXT", "backfilled": "INTEGER DEFAULT 0",
    # written by scores/portfolio_health.py, read by the morning brief; it
    # was added only by that module's own migration, so a fresh install's
    # brief failed on it
    "portfolio_health": "REAL",
}

def _add_missing_columns(conn, table, columns) -> list:
    """ALTER TABLE ADD COLUMN for each of `columns` the table lacks; never
    drops or rewrites rows. [] when the table does not exist yet (init_db()
    creates it with the columns) or nothing was missing."""
    try:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.OperationalError:
        return []
    added = []
    for col, typ in columns.items():
        if existing and col not in existing:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                added.append(col)
            except sqlite3.OperationalError as e:
                log.warning(f"  {table} migration skipped {col}: {e}")
    if added:
        conn.commit()
        log.info(f"  ✓ {table} migrated — added columns: {', '.join(added)}")
    return added

def _migrate_market_health_breadth_columns(conn):
    _add_missing_columns(conn, "market_health", MARKET_HEALTH_ADDED_COLUMNS)

def _migrate_news_classifier_column(conn):
    """news_articles.classifier: 'claude' or 'rule'. The rule-based fallback
    writes a fixed confidence of 0.5 (data/news.py classify_rule_based), which
    ACS's NewsConfidence read as if it were a measurement. Rows stored before
    the column existed are marked 'rule' when they carry exactly what that
    fallback writes -- category GENERAL, confidence 0.5; every row in the
    2026-09 database does, the Anthropic key never having been set."""
    if _add_missing_columns(conn, "news_articles", {"classifier": "TEXT"}):
        conn.execute("UPDATE news_articles SET classifier='rule' WHERE classifier IS NULL "
                     "AND category='GENERAL' AND confidence=0.5")
        conn.commit()

# NSE's corporate-action calendar and how each event was reconciled with
# prices_daily (data/corporate_actions.py). factor: from NSE's terms;
# price_factor: the one the stored history carries; status: pending,
# adjusted, already_adjusted, unadjusted, unverified or no_data.
CORPORATE_ACTIONS_DDL = (
    """CREATE TABLE IF NOT EXISTS corporate_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, ex_date DATE NOT NULL,
        subject TEXT NOT NULL, kind TEXT, factor REAL,
        status TEXT NOT NULL DEFAULT 'pending', price_factor REAL,
        price_rows INTEGER, volume_rows INTEGER, note TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, reconciled_at TIMESTAMP,
        UNIQUE(symbol, ex_date, subject))""",
    "CREATE INDEX IF NOT EXISTS idx_ca_symbol ON corporate_actions(symbol, ex_date)",
)

def _migrate_corporate_actions_table(conn):
    """Self-healing, non-destructive migration -- init_db() runs only with
    --init, so an existing database gets the corporate_actions table here."""
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('prices_daily','corporate_actions')")}
        if "prices_daily" in names and "corporate_actions" not in names:
            for ddl in CORPORATE_ACTIONS_DDL:
                conn.execute(ddl)
            conn.commit()
            log.info("  ✓ corporate_actions table created")
    except sqlite3.OperationalError as e:
        log.warning(f"  corporate_actions migration skipped: {e}")

INDEX_LEVELS_UNIQUE = "uq_index_levels_date_time"
_index_levels_unique_blocked = False

def _migrate_index_levels_unique(conn):
    """Non-destructive migration -- one index_levels row per (date, time).

    The table only had a plain index, so when two flush threads ran at once
    (2026-09-09 and 09-10) every snapshot was stored twice: 2,191 byte-identical
    pairs. Both writers now insert with OR IGNORE / OR REPLACE, which is valid
    with or without this index, so a database that still holds duplicates keeps
    working: the index is simply not created, and that is reported once per
    process instead of retried (and the table re-scanned) on every connection.
    Existing rows are never deleted here.
    """
    global _index_levels_unique_blocked
    if _index_levels_unique_blocked:
        return
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='index_levels'").fetchone():
            return  # fresh install -- init_db() creates the table, then calls this
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
                        (INDEX_LEVELS_UNIQUE,)).fetchone():
            return
        conn.execute(f"CREATE UNIQUE INDEX {INDEX_LEVELS_UNIQUE} ON index_levels(date,time)")
        conn.commit()
        log.info("  ✓ index_levels migrated — (date, time) is now unique")
    except sqlite3.IntegrityError:
        _index_levels_unique_blocked = True
        log.warning("  index_levels still holds duplicate (date, time) rows — unique index "
                    "not created; remove the duplicates to enable it")
    except sqlite3.OperationalError as e:
        log.warning(f"  index_levels unique-index migration skipped: {e}")

def _migrate_technical_macd_pct_column(conn):
    """Self-healing, non-destructive migration — adds
    technical_indicators.macd_hist_pct, the MACD histogram as a percentage of
    price, which is what the scores read: the raw rupee histogram is not
    comparable across a universe priced from Rs 7 to Rs 134,860. Never drops or
    rewrites existing rows."""
    try:
        existing = {row[1] for row in conn.execute(
            "PRAGMA table_info(technical_indicators)").fetchall()}
    except sqlite3.OperationalError:
        return  # table doesn't exist yet — init_db() creates it with the column
    if existing and "macd_hist_pct" not in existing:
        try:
            conn.execute("ALTER TABLE technical_indicators ADD COLUMN macd_hist_pct REAL")
            conn.commit()
            log.info("  ✓ technical_indicators migrated — added column: macd_hist_pct")
        except sqlite3.OperationalError as e:
            log.warning(f"  technical_indicators migration skipped macd_hist_pct: {e}")


def _migrate_ai_scores_beta_column(conn):
    """Self-healing, non-destructive migration — adds ai_scores.beta_1y
    (1-year beta vs Nifty 50) for databases created before this column
    existed. Never drops or rewrites existing rows."""
    try:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(ai_scores)").fetchall()}
    except sqlite3.OperationalError:
        return  # table doesn't exist yet — init_db() will create it with the column
    if "beta_1y" not in existing:
        try:
            conn.execute("ALTER TABLE ai_scores ADD COLUMN beta_1y REAL")
            conn.commit()
            log.info("  ✓ ai_scores migrated — added column: beta_1y")
        except sqlite3.OperationalError as e:
            log.warning(f"  ai_scores migration skipped beta_1y: {e}")

def _migrate_index_levels_chg_columns(conn):
    """Self-healing, non-destructive migration.

    markets.py computes a `<col>_chg` value for every entry in NSE_INDEXES
    (14 indexes), but index_levels originally only declared _chg columns
    for 4 of them. That mismatch is what threw:
        DB: table index_levels has no column named nifty_it_chg
    This adds the missing columns via ALTER TABLE ADD COLUMN only — it
    never drops or rewrites existing rows, and is a cheap no-op once the
    columns already exist.
    """
    try:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(index_levels)").fetchall()}
    except sqlite3.OperationalError:
        return  # table doesn't exist yet (fresh install) — init_db() will create it with all columns
    needed = ["nifty_it_chg", "nifty_auto_chg", "nifty_fmcg_chg", "nifty_metal_chg",
              "nifty_realty_chg", "nifty_psubank_chg", "nifty_energy_chg", "nifty_pharma_chg",
              "india_vix_chg", "gift_nifty_chg"]
    added = []
    for col in needed:
        if col not in existing:
            try:
                conn.execute(f"ALTER TABLE index_levels ADD COLUMN {col} REAL")
                added.append(col)
            except sqlite3.OperationalError as e:
                log.warning(f"  index_levels migration skipped {col}: {e}")
    if added:
        conn.commit()
        log.info(f"  ✓ index_levels migrated — added columns: {', '.join(added)}")

def init_db():
    conn = get_connection()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS prices_daily (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, date DATE NOT NULL,
        open REAL, high REAL, low REAL, close REAL, adj_close REAL,
        volume INTEGER, delivery_qty INTEGER, delivery_pct REAL,
        series TEXT DEFAULT 'EQ', source TEXT DEFAULT 'bhavcopy',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(symbol,date))""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_prices_symbol_date ON prices_daily(symbol,date)")
    c.execute("""CREATE TABLE IF NOT EXISTS technical_indicators (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, date DATE NOT NULL,
        rsi_14 REAL, stoch_k REAL, stoch_d REAL, williams_r REAL, cci_20 REAL,
        macd_line REAL, macd_signal REAL, macd_hist REAL, macd_hist_pct REAL, adx_14 REAL,
        ema_9 REAL, ema_21 REAL, ema_50 REAL, sma_200 REAL,
        atr_14 REAL, atr_pct REAL, bb_upper REAL, bb_lower REAL, bb_mid REAL, bb_width REAL,
        obv REAL, volume_sma20 REAL, volume_ratio REAL, rel_volume REAL,
        pivot REAL, r1 REAL, r2 REAL, s1 REAL, s2 REAL,
        fib_236 REAL, fib_382 REAL, fib_500 REAL, fib_618 REAL,
        golden_cross INTEGER DEFAULT 0, death_cross INTEGER DEFAULT 0,
        above_200dma INTEGER DEFAULT 0, gap_pct REAL, tech_score REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(symbol,date))""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_tech_symbol_date ON technical_indicators(symbol,date)")
    c.execute("""CREATE TABLE IF NOT EXISTS fundamental_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, quarter TEXT NOT NULL,
        report_date DATE, roe REAL, roce REAL, net_margin REAL, operating_margin REAL, roa REAL,
        eps_ttm REAL, eps_growth_yoy REAL, revenue_cr REAL, revenue_growth_yoy REAL,
        profit_cr REAL, profit_growth_yoy REAL, qoq_revenue_chg REAL, qoq_profit_chg REAL,
        debt_equity REAL, current_ratio REAL, interest_coverage REAL, fcf_cr REAL, cash_cr REAL,
        pe_ratio REAL, pb_ratio REAL, ev_ebitda REAL, peg_ratio REAL, dividend_yield REAL,
        book_value_ps REAL, promoter_hold REAL, promoter_pledge REAL, inst_hold REAL,
        fundamental_score REAL, source TEXT DEFAULT 'alpha_vantage',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(symbol,quarter))""")
    c.execute("""CREATE TABLE IF NOT EXISTS institutional_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, date DATE NOT NULL,
        fii_net_cr REAL, dii_net_cr REAL, mf_net_cr REAL,
        promoter_buy INTEGER DEFAULT 0, promoter_sell INTEGER DEFAULT 0,
        delivery_pct REAL, inst_score REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(symbol,date))""")
    c.execute("""CREATE TABLE IF NOT EXISTS fii_dii_market (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date DATE NOT NULL UNIQUE,
        fii_buy_cr REAL, fii_sell_cr REAL, fii_net_cr REAL,
        dii_buy_cr REAL, dii_sell_cr REAL, dii_net_cr REAL,
        fii_5d_avg REAL, dii_5d_avg REAL, pcr REAL, mwpl_pct REAL, adv_decline REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS news_articles (
        id INTEGER PRIMARY KEY AUTOINCREMENT, fetched_at TIMESTAMP NOT NULL,
        headline TEXT NOT NULL, source TEXT, url TEXT, category TEXT,
        symbols_mentioned TEXT, sentiment REAL, importance TEXT,
        confidence REAL, news_score REAL, ai_summary TEXT, processed INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, classifier TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_news_date ON news_articles(fetched_at)")
    c.execute("""CREATE TABLE IF NOT EXISTS ai_scores (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, date DATE NOT NULL,
        vpi REAL, spi REAL, rri REAL, mri REAL, cri REAL, msi REAL, zpi REAL, acs REAL,
        tech_score REAL, fund_score REAL, inst_score REAL, news_score REAL,
        atip_score REAL, atip_rank INTEGER, signal TEXT, confidence REAL,
        beta_1y REAL,
        tod_score REAL, is_tod INTEGER DEFAULT 0,
        mh_score REAL, regime TEXT,
        top_factor_1 TEXT, top_factor_2 TEXT, top_factor_3 TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(symbol,date))""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_scores_date_atip ON ai_scores(date,atip_score DESC)")
    c.execute("""CREATE TABLE IF NOT EXISTS market_health (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date DATE NOT NULL UNIQUE,
        mh_score REAL, regime TEXT, nifty_trend REAL, banknifty REAL, breadth REAL,
        vix_score REAL, fii_score REAL, dii_score REAL, global_score REAL,
        sector_score REAL, adv_decline REAL, nifty_close REAL, vix_level REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        advances INTEGER, declines INTEGER, pct_advancing REAL, new_highs INTEGER,
        new_lows INTEGER, breadth_universe INTEGER, mh_coverage REAL, mh_inputs TEXT,
        backfilled INTEGER DEFAULT 0, portfolio_health REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS index_levels (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date DATE NOT NULL, time TEXT NOT NULL,
        nifty50 REAL, nifty50_chg REAL, banknifty REAL, banknifty_chg REAL,
        midcap150 REAL, midcap150_chg REAL, smallcap250 REAL, smallcap250_chg REAL,
        nifty_it REAL, nifty_it_chg REAL, nifty_auto REAL, nifty_auto_chg REAL,
        nifty_fmcg REAL, nifty_fmcg_chg REAL, nifty_metal REAL, nifty_metal_chg REAL,
        nifty_realty REAL, nifty_realty_chg REAL, nifty_psubank REAL, nifty_psubank_chg REAL,
        nifty_energy REAL, nifty_energy_chg REAL, nifty_pharma REAL, nifty_pharma_chg REAL,
        india_vix REAL, india_vix_chg REAL, gift_nifty REAL, gift_nifty_chg REAL, overall_sentiment TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_index_datetime ON index_levels(date,time)")
    c.execute("""CREATE TABLE IF NOT EXISTS global_markets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date DATE NOT NULL, time TEXT DEFAULT 'overnight',
        sp500 REAL, sp500_chg REAL, dow REAL, dow_chg REAL, nasdaq REAL, nasdaq_chg REAL,
        nikkei REAL, nikkei_chg REAL, hangseng REAL, hangseng_chg REAL,
        ftse100 REAL, ftse100_chg REAL, dax REAL, dax_chg REAL,
        crude_wti REAL, crude_wti_chg REAL, crude_brent REAL, crude_brent_chg REAL,
        gold REAL, gold_chg REAL, silver REAL, silver_chg REAL,
        usd_inr REAL, usd_inr_chg REAL, usd_index REAL, usd_index_chg REAL,
        us_10y REAL, us_10y_chg REAL,
        global_score REAL, us_score REAL, asia_score REAL, commodity_score REAL,
        global_sentiment TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(date,time))""")
    c.execute("""CREATE TABLE IF NOT EXISTS portfolio_holdings (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date DATE NOT NULL, symbol TEXT NOT NULL,
        qty INTEGER, avg_price REAL, cmp REAL, current_val REAL, pnl REAL, pnl_pct REAL,
        atip_score REAL, vpi REAL, cri REAL, zpi REAL, signal TEXT, weight_pct REAL,
        sector TEXT, beta_1y REAL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(symbol,date))""")
    c.execute("""CREATE TABLE IF NOT EXISTS predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, pred_date DATE NOT NULL, symbol TEXT NOT NULL,
        signal TEXT, atip_score REAL, vpi REAL, zpi REAL, mri REAL, cri REAL, acs REAL,
        entry_price REAL, stop_loss REAL, target_1 REAL, target_2 REAL,
        risk_reward REAL, position_size_pct REAL, confidence REAL,
        reasoning TEXT, regime TEXT, is_tod INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(pred_date,symbol))""")
    c.execute("""CREATE TABLE IF NOT EXISTS accuracy_tracker (
        id INTEGER PRIMARY KEY AUTOINCREMENT, pred_date DATE NOT NULL, symbol TEXT NOT NULL,
        signal TEXT, entry_price REAL,
        price_5d REAL, return_5d REAL, correct_5d INTEGER,
        price_10d REAL, return_10d REAL, correct_10d INTEGER,
        price_20d REAL, return_20d REAL, correct_20d INTEGER,
        hit_target_1 INTEGER DEFAULT 0, hit_stop_loss INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(pred_date,symbol))""")
    c.execute("""CREATE TABLE IF NOT EXISTS pipeline_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_date DATE, job_name TEXT NOT NULL,
        start_time TIMESTAMP, end_time TIMESTAMP, status TEXT,
        rows_processed INTEGER DEFAULT 0, error_msg TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, kind TEXT, duration_s REAL)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pipeline_log_job ON pipeline_log(job_name, start_time)")
    c.execute("""CREATE TABLE IF NOT EXISTS weight_config (
        id INTEGER PRIMARY KEY AUTOINCREMENT, index_name TEXT NOT NULL,
        variable TEXT NOT NULL, weight REAL NOT NULL, description TEXT,
        regime TEXT DEFAULT 'ALL', active INTEGER DEFAULT 1,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(index_name,variable,regime))""")
    c.execute("""CREATE TABLE IF NOT EXISTS bulk_deals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, date DATE NOT NULL,
        net_value_cr REAL, deal_count INTEGER DEFAULT 0, source TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(symbol,date))""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_bulk_symbol_date ON bulk_deals(symbol,date)")
    c.execute("""CREATE TABLE IF NOT EXISTS live_quotes (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol     TEXT    NOT NULL,
        ltp        REAL,
        open       REAL,
        high       REAL,
        low        REAL,
        prev_close REAL,
        volume     INTEGER,
        chg_pct    REAL,
        timestamp  TEXT,
        source     TEXT DEFAULT 'dhan',
        UNIQUE(symbol, timestamp))""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_lq_symbol ON live_quotes(symbol, timestamp)")
    c.execute("""CREATE TABLE IF NOT EXISTS live_ticks (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol      TEXT,
        security_id TEXT,
        ltp         REAL,
        open        REAL,
        high        REAL,
        low         REAL,
        close       REAL,
        volume      INTEGER,
        timestamp   TEXT,
        received_at TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_lt_symbol ON live_ticks(symbol, received_at)")
    for ddl in CORPORATE_ACTIONS_DDL:
        c.execute(ddl)
    conn.commit()
    _ensure_migrated(conn, force=True)   # W8: base tables now exist -- re-run the additive layer
    _migrate_index_levels_unique(conn)
    conn.close()
    # describe_target(), not DB_PATH: on MySQL or PostgreSQL this function has
    # just built the schema on a server and written no SQLite file at all, and
    # naming one told an operator they were on an engine they were not.
    target = describe_target()
    log.info(f"✅ Database ready: {target}")
    return target

def seed_weights():
    weights = [
        ("VPI","V",0.18,"Volatility (ATR%)","ALL"),("VPI","TS",0.15,"Trend Strength (ADX)","ALL"),
        ("VPI","RS",0.12,"Relative Strength vs Nifty","ALL"),("VPI","LQ",0.10,"Liquidity","ALL"),
        ("VPI","VOL",0.10,"Volume Expansion","ALL"),("VPI","MR",0.10,"Mean Reversion","ALL"),
        ("VPI","FG",0.10,"Fundamental Growth","ALL"),("VPI","IS",0.08,"Institutional Strength","ALL"),
        ("VPI","NS",0.07,"News Sentiment","ALL"),
        ("SPI","ROE",0.20,"Return on Equity","ALL"),("SPI","ROCE",0.15,"Return on Capital","ALL"),
        ("SPI","EPS",0.15,"EPS Growth YoY","ALL"),("SPI","Revenue",0.10,"Revenue Growth","ALL"),
        ("SPI","FCF",0.10,"Free Cash Flow","ALL"),("SPI","Debt",0.10,"Debt/Equity inv","ALL"),
        ("SPI","PEG",0.10,"PEG Ratio inv","ALL"),("SPI","Quality",0.10,"Quality Composite","ALL"),
        ("RRI","Recovery",0.25,"Rebound from 52W low","ALL"),("RRI","Support",0.20,"Near support","ALL"),
        ("RRI","Volume",0.15,"Vol on up-days","ALL"),("RRI","RSIRecovery",0.15,"RSI crossed 30","ALL"),
        ("RRI","Institutional",0.15,"DII/MF buying","ALL"),("RRI","News",0.10,"Positive news","ALL"),
        ("MRI","MACD",0.25,"MACD cross zero","ALL"),("MRI","RSI",0.20,"RSI cross 40","ALL"),
        ("MRI","ADX",0.15,"ADX falling inv","ALL"),("MRI","Volume",0.15,"Vol surge","ALL"),
        ("MRI","EMA",0.15,"9-EMA cross 21","ALL"),("MRI","News",0.10,"Catalyst news","ALL"),
        ("CRI","Volatility",0.20,"High ATR near 52W high","ALL"),("CRI","Debt",0.20,"High D/E","ALL"),
        ("CRI","Distribution",0.15,"Vol on down-days","ALL"),("CRI","WeakTrend",0.15,"Below DMAs","ALL"),
        ("CRI","NegativeNews",0.15,"Negative news","ALL"),("CRI","MarketWeakness",0.15,"Sector+VIX","ALL"),
        ("MSI","News",0.30,"Aggregate news","ALL"),("MSI","FII",0.20,"FII 5-day trend","ALL"),
        ("MSI","DII",0.15,"DII activity","ALL"),("MSI","Sector",0.10,"% sectors green","ALL"),
        ("MSI","Options",0.10,"PCR inverted","ALL"),("MSI","Global",0.10,"Global sentiment","ALL"),
        ("MSI","VIX",0.05,"VIX inverted","ALL"),
        ("ZPI","Support",0.15,"Near key support","ALL"),("ZPI","Resistance",0.15,"Room to run","ALL"),
        ("ZPI","RSI",0.10,"RSI 30-50 zone","ALL"),("ZPI","ATR",0.10,"R:R >= 1:3","ALL"),
        ("ZPI","Volume",0.10,"Low vol pullback","ALL"),("ZPI","Trend",0.10,"Above 200-DMA","ALL"),
        ("ZPI","Institutional",0.10,"Accumulation 10d","ALL"),("ZPI","News",0.10,"No negative news","ALL"),
        ("ZPI","Sector",0.10,"Top-3 sector","ALL"),
        ("ACS","HistoricalAccuracy",0.25,"Past accuracy 90d","ALL"),
        ("ACS","Agreement",0.20,"% indexes agree","ALL"),("ACS","MarketRegime",0.20,"MH>60","ALL"),
        ("ACS","DataQuality",0.15,"Input completeness","ALL"),
        ("ACS","NewsConfidence",0.10,"AI news conf","ALL"),("ACS","Liquidity",0.10,"Turnover","ALL"),
        ("MH","NiftyTrend",0.20,"Nifty trend","ALL"),("MH","BankNifty",0.15,"BankNifty","ALL"),
        ("MH","Breadth",0.10,"% above 200DMA","ALL"),("MH","VIX",0.10,"VIX inv","ALL"),
        ("MH","FII",0.10,"FII net 5d","ALL"),("MH","DII",0.10,"DII net","ALL"),
        ("MH","Global",0.10,"Global score","ALL"),("MH","Sector",0.10,"Sector breadth","ALL"),
        ("MH","AdvanceDecline",0.05,"A/D ratio","ALL"),
        ("ATIP","VPI",0.20,"VPI","ALL"),("ATIP","SPI",0.15,"SPI","ALL"),
        ("ATIP","RRI",0.10,"RRI","ALL"),("ATIP","MRI",0.10,"MRI","ALL"),
        ("ATIP","MSI",0.10,"MSI","ALL"),("ATIP","ZPI",0.10,"ZPI","ALL"),
        ("ATIP","TS",0.10,"Tech Score","ALL"),("ATIP","FS",0.10,"Fund Score","ALL"),
        ("ATIP","INS",0.05,"Inst Score","ALL"),
        ("TOD","VPI",0.20,"VPI","ALL"),("TOD","ZPI",0.15,"ZPI","ALL"),
        ("TOD","MRI",0.15,"MRI","ALL"),("TOD","MSI",0.10,"MSI","ALL"),
        ("TOD","Volume",0.10,"Volume breakout","ALL"),("TOD","Breakout",0.10,"Price breakout","ALL"),
        ("TOD","Sector",0.10,"Sector strength","ALL"),("TOD","ACS",0.10,"ACS","ALL"),
        # INS -- Institutional Score. MutualFund/Insider from the original
        # doc formula (E17) are intentionally omitted: NSE does not publish
        # free per-stock mutual-fund-flow or insider-trade data, so there is
        # no real source to wire in for them (see compute_ins() docstring).
        # Weight is redistributed across the three sources that ARE real:
        # market-wide FII/DII 5-day net flow + promoter holding + bulk/block
        # deal net value for the stock.
        ("INS","FII",0.40,"Market FII 5-day net flow","ALL"),
        ("INS","DII",0.30,"Market DII 5-day net flow","ALL"),
        ("INS","Promoter",0.20,"Promoter shareholding %","ALL"),
        ("INS","BulkDeals",0.10,"Net bulk/block deal value (10d)","ALL"),
        # TS — Technical Score, exactly the architecture doc's formula. These
        # were hardcoded inside compute_tech_score() with only 8 of the 12
        # components and Trend at 0.15 instead of 0.08. VWAP is seeded for
        # completeness but never contributes yet (it needs intraday bars);
        # the score renormalises over whichever components are present.
        ("TS","RSI",0.10,"RSI zone","ALL"),("TS","MACD",0.10,"MACD histogram","ALL"),
        ("TS","ADX",0.10,"Trend strength","ALL"),("TS","ATR",0.08,"Volatility","ALL"),
        ("TS","EMA",0.08,"EMA alignment","ALL"),("TS","VWAP",0.08,"VWAP (needs intraday)","ALL"),
        ("TS","Bollinger",0.08,"Position in band","ALL"),("TS","Volume",0.08,"Volume ratio","ALL"),
        ("TS","Trend",0.08,"Above 200-DMA","ALL"),("TS","SR",0.08,"Support/Resistance crosses","ALL"),
        ("TS","Gap",0.07,"Gap analysis","ALL"),("TS","RelativeVolume",0.07,"Vs same-weekday avg","ALL"),
        # PHS — Portfolio Health Score, the doc's formula (section 11).
        ("PHS","Diversification",0.25,"Concentration across holdings","ALL"),
        ("PHS","Risk",0.20,"Portfolio beta + CRI exposure","ALL"),
        ("PHS","Drawdown",0.15,"Drawdown from peak","ALL"),
        ("PHS","Quality",0.15,"Mean ATIP score of holdings","ALL"),
        ("PHS","Allocation",0.15,"Position sizing vs regime","ALL"),
        ("PHS","Performance",0.10,"Unrealised P&L","ALL"),
    ]
    conn = get_connection()
    conn.executemany(
        "INSERT OR IGNORE INTO weight_config(index_name,variable,weight,description,regime) VALUES(?,?,?,?,?)",
        weights)
    conn.commit(); conn.close()
    log.info(f"✅ Seeded {len(weights)} weight configurations")

def log_job(job_name, status, rows=0, error=None, run_date=None, start_time=None,
            end_time=None, kind="step"):
    """
    One pipeline_log row. kind='run' is a whole scheduled job, written by
    pipeline.scheduler.run_job with its real start and end; kind='step' is a
    job function reporting its own outcome from inside (start_time is then
    when it reported, the only moment it knows).

    A failure to write is logged, not swallowed: this used to `except: pass`,
    so a locked or broken database made the pipeline's own record go silent.
    """
    end_time = end_time or datetime.now()
    start_time = start_time or end_time
    try:
        conn = get_connection()
        conn.execute(
            "INSERT INTO pipeline_log(run_date,job_name,start_time,end_time,status,rows_processed,"
            "error_msg,kind,duration_s) VALUES(?,?,?,?,?,?,?,?,?)",
            (str(run_date or __import__('datetime').date.today()), job_name, start_time, end_time,
             status, rows, str(error)[:2000] if error else None, kind,
             round((end_time - start_time).total_seconds(), 1)))
        conn.commit(); conn.close()
    except Exception as e:
        log.warning(f"  pipeline_log: could not record {job_name} {status}: {e}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    print(init_db()); seed_weights()
