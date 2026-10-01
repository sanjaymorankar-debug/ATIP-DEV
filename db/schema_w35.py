"""
Tables added in W35 (data platform), applied by db/schema.py _run_additive_migrations in a loop of its own.

    lake_partition          DP-22  manifest of every lake file (version, knowledge time, hash)
    tick_capture_status     DP-04  per day: ticks captured / flushed, symbols, gaps
    order_book_snapshot     DP-05  top-5 depth per snapshot: bids/asks JSON + spread, mid, imbalance
    fo_contract_daily       DP-08  F&O contracts of the nearest expiries (all strikes), per session
    option_chain_snapshot   DP-08  intraday NSE option-chain snapshots (configured index underlyings)
    macro_series            DP-14  macro series catalogue (source, frequency, release lag)
    macro_observation       DP-14  observations with period, value and available_from (point in time)
    macro_calendar          DP-14  owner-maintained / seeded release + policy dates (RBI MPC, CPI, GDP ...)
    asset_price_daily       DP-21  daily prices of non-equity assets (commodities, currencies, bonds, indices)
    mf_nav                  DP-21  AMFI daily mutual fund NAVs
    alt_dataset             AD-01  registered alternative-data sources + health
    alt_observation         AD-01  per source x entity x date value with available_from
"""

W35_TABLES = {
    "lake_partition": (
        """CREATE TABLE IF NOT EXISTS lake_partition (
            id INTEGER PRIMARY KEY AUTOINCREMENT, dataset TEXT NOT NULL, partition_date DATE NOT NULL,
            version INTEGER NOT NULL DEFAULT 1, path TEXT NOT NULL, format TEXT, rows INTEGER, columns_json TEXT,
            sha256 TEXT, bytes INTEGER, source TEXT, knowledge_time TIMESTAMP, written_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_lake_ds_date ON lake_partition(dataset, partition_date, version)",
    ),
    "tick_capture_status": (
        """CREATE TABLE IF NOT EXISTS tick_capture_status (
            day DATE PRIMARY KEY, ticks INTEGER, flushed INTEGER, symbols INTEGER, dropped INTEGER,
            first_tick TIMESTAMP, last_tick TIMESTAMP, minute_bars INTEGER, updated_at TIMESTAMP)""",
    ),
    "order_book_snapshot": (
        """CREATE TABLE IF NOT EXISTS order_book_snapshot (
            symbol TEXT NOT NULL, ts TIMESTAMP NOT NULL, ltp REAL, best_bid REAL, best_ask REAL, mid REAL,
            spread_bps REAL, bid_qty_5 REAL, ask_qty_5 REAL, imbalance REAL, bids_json TEXT, asks_json TEXT,
            source TEXT, PRIMARY KEY (symbol, ts))""",
        "CREATE INDEX IF NOT EXISTS idx_obs_ts ON order_book_snapshot(ts)",
    ),
    "fo_contract_daily": (
        """CREATE TABLE IF NOT EXISTS fo_contract_daily (
            date DATE NOT NULL, symbol TEXT NOT NULL, instrument TEXT NOT NULL, expiry DATE NOT NULL,
            strike REAL NOT NULL DEFAULT 0, option_type TEXT NOT NULL DEFAULT 'XX', open REAL, high REAL, low REAL,
            close REAL, settle REAL, prev_close REAL, oi REAL, oi_chg REAL, volume REAL, value REAL,
            underlying REAL, lot_size INTEGER, iv REAL,
            PRIMARY KEY (date, symbol, instrument, expiry, strike, option_type))""",
        "CREATE INDEX IF NOT EXISTS idx_focd_sym ON fo_contract_daily(symbol, date)",
    ),
    "option_chain_snapshot": (
        """CREATE TABLE IF NOT EXISTS option_chain_snapshot (
            ts TIMESTAMP NOT NULL, symbol TEXT NOT NULL, expiry DATE NOT NULL, strike REAL NOT NULL,
            option_type TEXT NOT NULL, ltp REAL, change REAL, iv REAL, oi REAL, oi_chg REAL, volume REAL,
            bid REAL, ask REAL, bid_qty REAL, ask_qty REAL, underlying REAL,
            PRIMARY KEY (ts, symbol, expiry, strike, option_type))""",
        "CREATE INDEX IF NOT EXISTS idx_ocs_sym ON option_chain_snapshot(symbol, ts)",
    ),
    "macro_series": (
        """CREATE TABLE IF NOT EXISTS macro_series (
            series_id TEXT PRIMARY KEY, name TEXT, country TEXT, source TEXT, source_code TEXT, frequency TEXT,
            unit TEXT, release_lag_days INTEGER, transform TEXT, last_fetch TIMESTAMP, last_period DATE,
            status TEXT, error TEXT)""",
    ),
    "macro_observation": (
        """CREATE TABLE IF NOT EXISTS macro_observation (
            series_id TEXT NOT NULL, period DATE NOT NULL, value REAL, available_from DATE NOT NULL,
            first_seen TIMESTAMP, revised INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (series_id, period))""",
    ),
    "macro_calendar": (
        """CREATE TABLE IF NOT EXISTS macro_calendar (
            event_date DATE NOT NULL, event TEXT NOT NULL, country TEXT DEFAULT 'IN', importance TEXT,
            series_id TEXT, source TEXT, note TEXT, PRIMARY KEY (event_date, event))""",
    ),
    "asset_price_daily": (
        """CREATE TABLE IF NOT EXISTS asset_price_daily (
            asset_class TEXT NOT NULL, symbol TEXT NOT NULL, date DATE NOT NULL, open REAL, high REAL, low REAL,
            close REAL, volume REAL, currency TEXT, source TEXT, PRIMARY KEY (asset_class, symbol, date))""",
    ),
    "mf_nav": (
        """CREATE TABLE IF NOT EXISTS mf_nav (
            scheme_code TEXT NOT NULL, date DATE NOT NULL, nav REAL, scheme_name TEXT, isin_growth TEXT,
            isin_reinvest TEXT, amc TEXT, category TEXT, PRIMARY KEY (scheme_code, date))""",
        "CREATE INDEX IF NOT EXISTS idx_mfnav_date ON mf_nav(date)",
    ),
    "alt_dataset": (
        """CREATE TABLE IF NOT EXISTS alt_dataset (
            source_id TEXT PRIMARY KEY, name TEXT, description TEXT, entity TEXT, frequency TEXT, enabled INTEGER,
            last_run TIMESTAMP, last_status TEXT, last_rows INTEGER, coverage REAL, stale_days INTEGER, error TEXT,
            meta_json TEXT)""",
    ),
    "alt_observation": (
        """CREATE TABLE IF NOT EXISTS alt_observation (
            source_id TEXT NOT NULL, entity TEXT NOT NULL, date DATE NOT NULL, metric TEXT NOT NULL, value REAL,
            available_from TIMESTAMP, meta_json TEXT, PRIMARY KEY (source_id, entity, date, metric))""",
        "CREATE INDEX IF NOT EXISTS idx_alt_obs ON alt_observation(source_id, metric, date)",
    ),
}
