-- ATIP migration 0007 preopen_checklist_signals (22 Nov 2026, PO-CL-02)
-- Applied automatically by ops/migrations.py at start-up (schema_migrations version 0007). This file is for review
-- or a manual run. Rollback: DROP INDEX idx_preopen_signals_eval; DROP TABLE preopen_signals; (only the checklist reads it)
CREATE TABLE IF NOT EXISTS preopen_signals (
        date DATE NOT NULL, run_label TEXT NOT NULL,
        nasdaq_close DOUBLE PRECISION, nasdaq_chg_pct DOUBLE PRECISION, dow_close DOUBLE PRECISION, dow_chg_pct DOUBLE PRECISION, us_close_date TEXT,
        nq_price DOUBLE PRECISION, nq_chg_pct DOUBLE PRECISION, ym_price DOUBLE PRECISION, ym_chg_pct DOUBLE PRECISION, us_futures_as_of TEXT,
        us_green BIGINT, us_red BIGINT, us_vote BIGINT, us_status TEXT, us_source TEXT,
        vix DOUBLE PRECISION, vix_regime TEXT, vix_threshold DOUBLE PRECISION, vix_status TEXT, vix_source TEXT, vix_as_of TEXT,
        size_multiplier DOUBLE PRECISION, stop_multiplier DOUBLE PRECISION, options_note TEXT,
        advances BIGINT, declines BIGINT, unchanged BIGINT, ad_ratio DOUBLE PRECISION, ad_basis TEXT, ad_vote BIGINT,
        ad_status TEXT, ad_source TEXT, ad_as_of TEXT,
        gift_price DOUBLE PRECISION, gift_expiry TEXT, gift_ref DOUBLE PRECISION, gift_ref_source TEXT, nifty_prev_close DOUBLE PRECISION,
        gift_vs_close_pct DOUBLE PRECISION, gift_move_pct DOUBLE PRECISION, gift_move_basis TEXT, gift_vote BIGINT, expected_open TEXT,
        gift_status TEXT, gift_source TEXT, gift_as_of TEXT,
        vote_sum BIGINT, votes_counted BIGINT, bias TEXT, config_json TEXT, caveats_json TEXT, fetched_at_ist TEXT,
        actual_open DOUBLE PRECISION, actual_gap_pct DOUBLE PRECISION, actual_open_class TEXT, nifty_close DOUBLE PRECISION, day_chg_pct DOUBLE PRECISION,
        day_direction TEXT, evaluated_at_ist TEXT, eval_note TEXT,
        PRIMARY KEY (date, run_label));
CREATE INDEX IF NOT EXISTS idx_preopen_signals_eval ON preopen_signals(evaluated_at_ist);
