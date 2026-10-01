"""
Tables and additive columns added in W37 (brokers & multi-asset), applied by db/schema.py.

    broker_import_run         PF-12  every file / API import into the wealth ledger
    paper_options_position    ENT-15 paper options book (also created by execution/options_paper.ensure_tables)
    paper_options_trade       ENT-15 fills and expiry settlements
    enterprise_vault_credential (+ columns)  ENT-06 expiry and verification of stored broker credentials
"""

W37_TABLES = {
    "broker_import_run": (
        """CREATE TABLE IF NOT EXISTS broker_import_run (
            run_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, broker TEXT, kind TEXT,
            filename TEXT, rows_in INTEGER, added INTEGER, skipped INTEGER, errors INTEGER, created_at TIMESTAMP,
            detail_json TEXT)""",
    ),
    "paper_options_position": (
        """CREATE TABLE IF NOT EXISTS paper_options_position (
            underlying TEXT NOT NULL, expiry DATE NOT NULL, strike REAL NOT NULL, option_type TEXT NOT NULL,
            qty REAL NOT NULL DEFAULT 0, lot_size INTEGER, avg_price REAL, realized REAL NOT NULL DEFAULT 0,
            opened_at TIMESTAMP, updated_at TIMESTAMP, PRIMARY KEY (underlying, expiry, strike, option_type))""",
    ),
    "paper_options_trade": (
        """CREATE TABLE IF NOT EXISTS paper_options_trade (
            trade_id TEXT PRIMARY KEY, underlying TEXT, expiry DATE, strike REAL, option_type TEXT, side TEXT,
            lots INTEGER, lot_size INTEGER, qty REAL, price REAL, premium REAL, fees REAL, price_source TEXT,
            reason TEXT, realized REAL, at TIMESTAMP)""",
    ),
}

W37_COLUMNS = {
    "enterprise_vault_credential": {"expires_at": "TIMESTAMP", "last_verified_at": "TIMESTAMP", "verify_status": "TEXT",
                                    "verify_detail": "TEXT"},
}
