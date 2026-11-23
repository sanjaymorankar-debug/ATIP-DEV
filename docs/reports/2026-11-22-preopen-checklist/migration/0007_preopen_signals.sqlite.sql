-- ATIP migration 0007 preopen_checklist_signals (22 Nov 2026, PO-CL-02)
-- Applied automatically by ops/migrations.py at start-up (schema_migrations version 0007). This file is for review
-- or a manual run. Rollback: DROP INDEX idx_preopen_signals_eval; DROP TABLE preopen_signals; (only the checklist reads it)
CREATE TABLE IF NOT EXISTS preopen_signals (
        date DATE NOT NULL, run_label TEXT NOT NULL,
        nasdaq_close REAL, nasdaq_chg_pct REAL, dow_close REAL, dow_chg_pct REAL, us_close_date TEXT,
        nq_price REAL, nq_chg_pct REAL, ym_price REAL, ym_chg_pct REAL, us_futures_as_of TEXT,
        us_green INTEGER, us_red INTEGER, us_vote INTEGER, us_status TEXT, us_source TEXT,
        vix REAL, vix_regime TEXT, vix_threshold REAL, vix_status TEXT, vix_source TEXT, vix_as_of TEXT,
        size_multiplier REAL, stop_multiplier REAL, options_note TEXT,
        advances INTEGER, declines INTEGER, unchanged INTEGER, ad_ratio REAL, ad_basis TEXT, ad_vote INTEGER,
        ad_status TEXT, ad_source TEXT, ad_as_of TEXT,
        gift_price REAL, gift_expiry TEXT, gift_ref REAL, gift_ref_source TEXT, nifty_prev_close REAL,
        gift_vs_close_pct REAL, gift_move_pct REAL, gift_move_basis TEXT, gift_vote INTEGER, expected_open TEXT,
        gift_status TEXT, gift_source TEXT, gift_as_of TEXT,
        vote_sum INTEGER, votes_counted INTEGER, bias TEXT, config_json TEXT, caveats_json TEXT, fetched_at_ist TEXT,
        actual_open REAL, actual_gap_pct REAL, actual_open_class TEXT, nifty_close REAL, day_chg_pct REAL,
        day_direction TEXT, evaluated_at_ist TEXT, eval_note TEXT,
        PRIMARY KEY (date, run_label));
CREATE INDEX IF NOT EXISTS idx_preopen_signals_eval ON preopen_signals(evaluated_at_ist);
