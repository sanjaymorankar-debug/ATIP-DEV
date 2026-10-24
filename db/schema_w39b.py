"""
Tables and additive columns added in W39b (tracker reconciliation 2026-10-07: the owner's
Wave 1-20 deployment tracker, the PERF-001 detail sheet and the owner notes), applied by
db/schema.py beside db/schema_w39.py (the W39 research / history / options tables, PR #4).
The 7-year history backfill is #4's data/history_backfill.py (table prices_daily_backfill).

    perf_ledger (+ columns)   PERF-001-01  entry_seq (portable entry order: rowid is SQLite's),
                                           order_ref / signal_ref (exact order and signal links),
                                           fee_breakdown (JSON: brokerage / stt / exchange / sebi /
                                           stamp / gst / dp / other, when the source gives it)
    order_basket / order_basket_run  EX-18  named multi-leg baskets and every preview / placement
    sip_plan / sip_execution         EX-20  stock SIP plans and one row per due date (idempotent)
    live_quotes (+ columns)          RK-21  upper_circuit / lower_circuit from the quote (price band)
    technical_ext (+ columns)        TA-08b / TA-05  sector-relative strength, swing-anchored Fibonacci
    ml_model_version (+ columns)  ML-18  code_version (git commit, +dirty) and lineage_json (dataset /
                                         feature set / config / backtest links) of every trained version
    index_total_return            PERF-001-05  the estimated Nifty total-return index (data/total_return.py)
    ml_meta_label / ml_meta_label_run / ml_meta_label_score   W40  meta-labelling of the technical signals
                                         (DDL in ml/meta_label.py, appended to W39B_TABLES below)
    backtest_trade / backtest_equity (+ columns)  QR-05 / QR-06 (W40)  futures short legs of the W2
                                         backtest: instrument / direction / expiry / lots / lot_size /
                                         leg / rolled / calendar_spread / roll_cost per trade row,
                                         futures_margin / futures_notional per equity point (DDL in
                                         backtest/futures.py)
    quant_risk_exposure       W40  the factor risk model (quant/risk_model.py): per stock per session the
                                   style exposures, model industry, market cap, filled-descriptor flags,
                                   the next session's return and specific return, the specific-risk forecast
    quant_risk_factor_return  W40  daily factor returns (constrained WLS) with t statistics
    quant_risk_regression     W40  one row per regression: stocks, excluded, R^2
    quant_risk_covariance     W40  the EWMA / Newey-West factor covariance as of each session
    quant_risk_state          W40  the model definition (factors, merged industries, hash) and the bias test
    paper_option_strategy_*  W40 / ENT-15  option-overlay strategies' multi-leg paper positions, legs,
                                           fills and daily marks (DDL: execution/options_paper.py)
    oms_order (+ legs_json)  W40 / ENT-15  the legs of a multi-leg OPT order
"""

from data.total_return import DDL as _TRI
from quant.risk_model import TABLES as _RISK_MODEL

W39B_TABLES = {
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
    "index_total_return": _TRI,                                     # W40 PERF-001-05
    # W40: the fundamental factor risk model (quant/risk_model.py owns the DDL)
    "quant_risk_exposure": _RISK_MODEL["quant_risk_exposure"],
    "quant_risk_factor_return": _RISK_MODEL["quant_risk_factor_return"],
    "quant_risk_regression": _RISK_MODEL["quant_risk_regression"],
    "quant_risk_covariance": _RISK_MODEL["quant_risk_covariance"],
    "quant_risk_state": _RISK_MODEL["quant_risk_state"],
}

W39B_COLUMNS = {
    "perf_ledger": {"entry_seq": "INTEGER", "order_ref": "TEXT", "signal_ref": "TEXT", "fee_breakdown": "TEXT"},
    # ML-18 research-to-production lineage: the code a model version was trained with
    "ml_model_version": {"code_version": "TEXT", "lineage_json": "TEXT"},
    # RK-21 the exchange price band (NSE circuit limits) when the quote carries it
    "live_quotes": {"upper_circuit": "REAL", "lower_circuit": "REAL"},
    # TA-08b sector-relative strength, TA-05 swing-anchored Fibonacci (stored, not scored)
    "technical_ext": {"rs_sector_index": "TEXT", "rs_sector_63": "REAL", "rs_sector_126": "REAL",
                      "rs_sector_pctile": "REAL", "fib_swing_high": "REAL", "fib_swing_low": "REAL",
                      "fib_swing_dir": "TEXT", "fib_382": "REAL", "fib_500": "REAL", "fib_618": "REAL",
                      "fib_nearest": "TEXT", "fib_nearest_dist_pct": "REAL"},
    # PF-06 partial position changes: a REDUCE row (1), the ADD fills into its position
    "backtest_trade": {"partial": "INTEGER", "adds": "INTEGER"},
}

# W40 (gap analysis §4 item 9): meta-labelling of the technical signals -- the DDL lives with its module
#     ml_meta_label         one triple-barrier label per closed technical signal (barrier, t1, R, gap)
#     ml_meta_label_run     every training run: the out-of-fold report and its ADOPTABLE / NO_EDGE verdict
#     ml_meta_label_score   the nightly scores of the day's signals (probability, bet size), when enabled
from ml.meta_label import DDL as _META_LABEL_DDL  # noqa: E402  (stdlib + numpy only at import)

W39B_TABLES.update(_META_LABEL_DDL)

# QR-05 / QR-06 (W40): futures short legs in the W2 backtest -- the DDL lives in backtest/futures.py
from backtest.futures import EQUITY_COLUMNS as _FUT_EQUITY, TRADE_COLUMNS as _FUT_TRADE  # noqa: E402

W39B_COLUMNS["backtest_trade"] = {**W39B_COLUMNS["backtest_trade"], **_FUT_TRADE}
W39B_COLUMNS["backtest_equity"] = {**W39B_COLUMNS.get("backtest_equity", {}), **_FUT_EQUITY}

# W40 (ENT-15): option-overlay strategies' multi-leg paper positions (paper_option_strategy_position /
# _leg / _trade / _mark) -- the DDL lives with the book, execution/options_paper.py STRATEGY_TABLES --
# and oms_order.legs_json, the legs of a multi-leg OPT order (execution/option_intents.order_plan)
from execution.options_paper import STRATEGY_TABLES as _W40_OPTION_DDL  # noqa: E402

W39B_TABLES.update(_W40_OPTION_DDL)
W39B_COLUMNS.setdefault("oms_order", {})["legs_json"] = "TEXT"
