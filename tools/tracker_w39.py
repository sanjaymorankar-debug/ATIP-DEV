"""
W39 tracker reconciliation (2026-10-07): the owner's deployment workbook + master tracker
compared with the code, the new items added, statuses and test evidence refreshed.

    python tools/tracker_w39.py --owner-csv <uploaded ATIP_MASTER_TRACKER.csv> \
        --owner-xlsx <uploaded ATIP_Master_Deployment_Tracker_Wave_1_to_20_UPDATED.xlsx> \
        --coverage <coverage.json from a full pytest run with per-test contexts>

INPUTS
  docs/ATIP_MASTER_TRACKER.csv   the canonical feature tracker kept in this repository
  --owner-csv     the copy the owner uploaded; it predates W28b-W38, so the rows where it
                  still says YET TO START / IN PROGRESS for work already merged are listed
                  (sheet "Owner CSV vs repo") rather than taken over
  --owner-xlsx    the owner's Wave 1-20 deployment workbook (Master Tracker, Gap Analysis,
                  Scope Exclusions, Roadmap, PERF-001 Detail, Implementation Sequence, Master
                  Dev Prompt) -- updated in place and extended
  --coverage      `coverage json --show-contexts` of `pytest tests/` run with
                  dynamic_context = test_function: which test modules execute each
                  feature's key files, and their line coverage

OUTPUTS
  docs/ATIP_MASTER_TRACKER.csv   new W39 rows; refreshed statuses / notes; three new columns:
                  "Deployment Wave" (the owner's Wave 1-20 numbering, plus the repo waves
                  W21-W39 for the platform extensions), "Test Evidence", "Blocker / Input
                  Required"
  docs/ATIP_Master_Deployment_Tracker_Wave_1_to_20_UPDATED.xlsx   the owner's workbook with
                  every sheet's statuses updated and new sheets: Summary, All Features, New
                  Items (W39), Blockers, Owner CSV vs repo, Status Legend

TESTING STATUS (development-side automated tests; independent QA and owner UAT are tracked
by QA-001 / UAT-001 and in Notes):
  COMPLETED     the feature's key code is exercised by passing automated tests (>= 60% of
                its lines) or by tests written for it in W39
  IN PROGRESS   partly exercised (1-59% of its lines)
  YET TO START  no automated test reaches its key code
  BLOCKED       the feature itself is blocked
  N/A           no code (documentation / legal / process item)
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "docs" / "ATIP_MASTER_TRACKER.csv"
XLSX_OUT = ROOT / "docs" / "ATIP_Master_Deployment_Tracker_Wave_1_to_20_UPDATED.xlsx"
TODAY = "2026-10-07"
NEW_COLUMNS = ["Deployment Wave", "Test Evidence", "Blocker / Input Required"]
PHASE_W39 = ("11", "W39 reconciliation (owner tracker 2026-10-07)")

W39_TESTS = "tests/test_w39_performance_detail.py"
NOTES_TESTS = "tests/test_w39_owner_notes.py"


def _row(fid, module, feature, target, source, prio, status, pct, impl, nxt, files, tests, blocker="",
         depends="", owner="Claude (development)", notes=""):
    return {"ID": fid, "Module": module, "Feature": feature, "Target Requirement": target, "Scope Source": source,
            "Priority": prio, "Status": status, "Completion %": str(pct), "Depends On": depends,
            "Current Implementation": impl, "Next Action / Missing Work": nxt, "Key Files": files,
            "Evidence": tests, "Owner": owner, "Started On": TODAY,
            "Completed On": TODAY if status == "COMPLETED" else "", "Last Updated": TODAY,
            "Notes": notes or "W39 developed 2026-10-07 on branch claude/wizardly-curie-fbjeoa (PR #5); "
                              "independent validation PENDING",
            "Blocker / Input Required": blocker}


IMPL = "IMPLEMENTED BUT NOT VERIFIED"
SRC_PERF = "Owner deployment tracker: PERF-001 Detail"
PERF = "wealth/perf/"

# ── the 14 PERF-001 requirements of the owner's "PERF-001 Detail" sheet ─────────────────
NEW_ROWS = [
    _row("PERF-001-01", "Wealth / Performance", "Performance data model",
         "Signals, orders, fills, positions, cash, costs, benchmarks, timestamps; immutable history", SRC_PERF, "P0",
         IMPL, 90, "perf_ledger append-only (migration 0005 triggers; UPDATE/DELETE refused); source refs per import; "
                   "W39: order_ref, signal_ref, fee_breakdown, entry_seq (portable entry order, migration 0006), "
                   "DEPOSIT / WITHDRAWAL cash rows, paper PARTIALLY_FILLED orders imported (were lost)",
         "Positions are derived from the ledger by design (no second source of truth); independent QA",
         "wealth/perf/ledger.py; db/schema_w39.py; ops/migrations.py",
         "test_paper_partial_fills_reach_the_ledger; test_entry_order_is_portable_and_numbered; "
         "test_migration_0006_numbers_old_rows_and_the_ledger_stays_append_only; "
         "test_cash_rows_and_fee_breakdown_are_validated; test_voided_transactions_are_ignored_but_kept"),
    _row("PERF-001-02", "Wealth / Performance", "Model Return", "Model signal execution at model price/rules; "
         "reproducible from signal log", SRC_PERF, "P0", IMPL, 90,
         "model.build: BUY signals (duplicate_of IS NULL) at the signal close, exit at horizon / SELL signal, equal "
         "weight, gross; pure function of signal_log + prices_daily", "Independent QA",
         "wealth/perf/model.py", "test_model_return_is_reproducible_and_matches_a_hand_calculation; "
                                 "test_model_and_executable_are_reported_separately"),
    _row("PERF-001-03", "Wealth / Performance", "Executable Return", "Realistic execution incl. slippage and "
         "liquidity", SRC_PERF, "P0", IMPL, 85,
         "next-session open x (1 + slippage bps), NSE cost model both sides, ADV cap (NOT_EXECUTABLE); W39: "
         "liquidity UNKNOWN flag when volume is missing, exit liquidity check, optional square-root market-impact "
         "slippage (slippage_model=impact: half spread + Y x sigma x sqrt(Q/ADV), inputs up to the signal date)",
         "Parity with the OMS paper fill model is not automated (different fill rule by design)",
         "wealth/perf/model.py; execution/impact.py",
         "test_executable_applies_slippage_costs_and_the_liquidity_cap; "
         "test_impact_slippage_uses_the_square_root_estimate_known_at_the_signal"),
    _row("PERF-001-04", "Wealth / Performance", "Actual Investor Return", "Actual quantities, prices, cash flows, "
         "exits; ledger-derived P&L / XIRR", SRC_PERF, "P0", IMPL, 90,
         "engine.run_actual: average cost, TWR, XIRR, PME; W39: realized P&L / fees / dividends for the period and "
         "lifetime-to-end, labelled; whole-account return with cash", "Independent QA",
         "wealth/perf/engine.py", "test_period_scope_is_separate_from_lifetime; "
                                  "test_xirr_and_pme_match_hand_computed_flows; test_average_cost_realized_pnl_and_fees"),
    _row("PERF-001-05", "Wealth / Performance", "Benchmark Return", "NIFTY / index / sector benchmark; period and "
         "methodology displayed", SRC_PERF, "P1", IMPL, 85,
         "any prices_daily symbol (NIFTY50, NIFTYBANK, midcap, smallcap, sector indices); price return over the same "
         "sessions + PME of the investor's flows; W39: days invested shown and exported",
         "Total-return index (dividends) not available from the data source", "wealth/perf/data.py; "
                                                                                 "wealth/perf/report.py",
         "test_benchmark_return_is_the_price_return_over_the_sessions"),
    _row("PERF-001-06", "Wealth / Performance", "Cost Attribution", "Brokerage, exchange, slippage, other costs; "
         "reconcile exactly to the ledger", SRC_PERF, "P0", IMPL, 90,
         "W39: fees per kind for the period (dividend TDS and capped-sell fees counted), independent SUM over "
         "perf_ledger reconciliation, recorded components (fee_breakdown), itemised NSE statutory estimate for "
         "trades without a breakdown, slippage vs reference price; broker imports carry the contract-note split "
         "(brokerage / STT / exchange / SEBI / stamp / GST / DP) per trade, and a contract-note charges file "
         "becomes FEE rows (no double count)", "Independent QA with real broker contract notes",
         "wealth/perf/engine.py; portfolio/imports.py",
         "test_costs_reconcile_with_dividend_tax_fee_rows_and_a_capped_sell; "
         "test_statutory_estimate_itemises_paper_brokerage_only_trades; tests/test_w39_contract_note_fees.py"),
    _row("PERF-001-07", "Wealth / Performance", "Position Attribution", "Entry / exit timing, sizing, averaging; "
         "variance from model decomposed", SRC_PERF, "P0", IMPL, 90,
         "entry timing + averaging + exit timing + costs = actual - model P&L on the same quantity; W39: + sizing "
         "effect = actual - model P&L on the model notional", "Independent QA", "wealth/perf/model.py",
         "test_sizing_effect_completes_the_decomposition_and_exact_links_win; test_position_attribution_identity"),
    _row("PERF-001-08", "Wealth / Performance", "Return Metrics", "CAGR, XIRR, Alpha, Sharpe, Sortino, drawdown, "
         "win rate, profit factor; validated independently", SRC_PERF, "P1", IMPL, 90,
         "metrics.py + backtest/metrics.py; W39 tests recompute Sharpe, Sortino, max drawdown, beta, alpha, "
         "volatility, CAGR with the statistics module", "Independent QA", "wealth/perf/metrics.py; backtest/metrics.py",
         "test_risk_metrics_match_independent_formulas; test_cagr_annualises_a_full_year; "
         "test_xirr_one_year_ten_percent; test_trade_stats"),
    _row("PERF-001-09", "Wealth / Performance", "Signal Attribution", "Each signal to entry, add-on, exit, outcome",
         SRC_PERF, "P0", IMPL, 90,
         "link_signals: EXACT (the buy carries signal_ref, W39) or WINDOW (first buy within 5 sessions); add-ons, "
         "exits, open quantity, momentum outcomes; signal-driven vs discretionary P&L on the period gain (W39)",
         "Independent QA", "wealth/perf/model.py; wealth/perf/report.py",
         "test_sizing_effect_completes_the_decomposition_and_exact_links_win"),
    _row("PERF-001-10", "Wealth / Performance", "Portfolio Attribution", "Asset / sector / stock contribution to "
         "return and risk; reconciles", SRC_PERF, "P1", IMPL, 90,
         "gain by symbol / sector / asset class on average capital (sums to total); W39: Euler risk contribution "
         "(shares sum to 100%, volatility contributions to portfolio volatility), cash account",
         "Independent QA", "wealth/perf/engine.py",
         "test_risk_contribution_reconciles_and_groups_sum_to_the_total; test_contributions_reconcile_to_the_total"),
    _row("PERF-001-11", "Wealth / Performance", "Performance Dashboard", "Model / executable / actual / benchmark "
         "side by side; no single ambiguous figure", SRC_PERF, "P0", IMPL, 90,
         "/wealth Performance tab: four returns with definitions and days invested, gaps, actual, costs, "
         "contribution, risk contribution, cash account, signal attribution, trades, audit + verify, ledger; "
         "overview card shows all four (W39 added EXECUTABLE)", "Owner UAT",
         "dashboard/wealth_ui/performance_tab.py; dashboard/wealth_ui/overview_tab.py",
         "test_routes_export_a_preview_and_verify_a_saved_report; test_happy_path_through_the_api"),
    _row("PERF-001-12", "Wealth / Performance", "Audit & Explainability", "Methodology, assumptions, calculation "
         "version, source data; every number traceable", SRC_PERF, "P0", IMPL, 90,
         "audit block: methodology + CALCULATION_VERSION (+git), assumptions, ledger / signal / price content "
         "sha256, last price date per series, basis fallbacks; W39 verify() rebuilds a saved report and diffs it "
         "(GET .../reports/{id}/verify)", "Independent QA", "wealth/perf/report.py",
         "test_verify_reproduces_and_detects_changed_inputs"),
    _row("PERF-001-13", "Wealth / Performance", "Report Export", "Downloadable report for period / portfolio / "
         "strategy; matches dashboard", SRC_PERF, "P2", IMPL, 90,
         "CSV (every section, copied not recomputed) and JSON of a saved report; W39: preview export route, "
         "strategy filter (ACTUAL limited to one strategy's trades), printable HTML report (A4, Print / Save as PDF) "
         "rendered from the same sections as the CSV", "Owner UAT of the printed layout",
         "wealth/perf/report.py; dashboard/wealth_routes.py; dashboard/wealth_ui/performance_tab.py",
         "test_export_carries_every_section_with_the_reports_numbers; test_strategy_filter_limits_the_actual_portfolio; "
         "test_printable_html_has_the_csv_sections_and_numbers_and_escapes_text"),
    _row("PERF-001-14", "Wealth / Performance", "Regression & Edge Cases", "Corporate actions, partial fills, "
         "averaging, cash flows, missing data, holidays", SRC_PERF, "P0", IMPL, 90,
         "edge-case suite: split, bonus, partial fill, averaging, intraday round trip, OPENING, cash, holiday, "
         "missing bars, oversell, future date, void", "Independent QA", PERF,
         "test_a_trade_on_a_market_holiday_counts_on_the_next_session; test_missing_bars_are_reported_not_hidden; "
         "test_a_bonus_issue_keeps_value_and_cost; test_an_opening_position_is_measured_from_that_days_close; "
         "test_split_between_buy_and_today_moves_neither_value_nor_return"),
]

# ── the owner's notes (ATIP-.txt, FEatures-.txt) ───────────────────────────────────────
SRC_NOTES = "Owner notes (ATIP-.txt / FEatures-.txt)"
NEW_ROWS += [
    _row("OPS-12", "Deployment/Ops", "Dhan token auto-renewal (TOTP)", "dhan_totp_secret: the 24-hour token "
         "renewed without manual login", SRC_NOTES, "P0", IMPL, 90,
         "tools/dhan_token_refresh.py (RenewToken, else TOTP + PIN generateAccessToken, writes config or vault); "
         "W39: scheduler job daily at dhan_token_refresh_time (06:45) + start-up renewal when the token is dead "
         "(it ran only from a Windows task, so on macOS the token expired daily); config template keys",
         "Owner: add dhan_pin + dhan_totp_secret (or vault them) once", "pipeline/w39_jobs.py; "
                                                                        "tools/dhan_token_refresh.py",
         "test_token_renewal_is_scheduled_and_runs_at_start_only_when_needed; "
         "test_refresher_only_if_invalid_keeps_a_working_token",
         blocker="Owner input (not a code blocker): dhan_pin + base32 dhan_totp_secret from web.dhan.co"),
    _row("DP-23", "Data Platform", "Long price history (7 years)", "Last 7 years of daily data pulled and stored",
         SRC_NOTES, "P1", IMPL, 85,
         "W39: data/history_backfill.py year-window backfill of the tracked universe + indices (resumable, "
         "remembers listing dates, retries refused windows); weekly purge now keeps prices_daily for history_years "
         "(it deleted everything older than 600 days); main.py --backfill-history; weekly Sunday top-up; "
         "/api/data/history/status", "Owner: run python main.py --backfill-history once (Dhan API, ~30-40 min "
                                      "for 500 symbols)", "data/history_backfill.py; db/purge.py",
         "test_plan_asks_only_for_the_missing_years_in_shared_windows; "
         "test_backfill_stores_through_the_fetcher_and_remembers_listing_dates; "
         "test_the_purge_keeps_price_history_for_history_years"),
    _row("DB-20", "Dashboard", "Stock history like a trading platform", "History of a stock shown like any "
         "trading software", SRC_NOTES, "P1", IMPL, 90,
         "W26 stock panel + W39: 3Y / 5Y / All, daily / weekly / monthly bars, SMA 20/50/200, EMA 21, Bollinger, "
         "log scale, multi-year returns; checked in headless Chromium", "Owner UAT", "dashboard/stock_view.py",
         "test_stock_history_reports_multi_year_returns"),
]

# ── the global algo-trading feature map (ATIP_Global_Algo_Trading_Features_and_Formulas (1).xlsx) ──
SRC_MAP = "Feature map (ATIP_Global_Algo_Trading_Features_and_Formulas)"
NEW_ROWS += [
    _row("RK-21", "Risk Management", "Pre-trade price band + gross leverage", "Position, notional, leverage, "
         "price-band, liquidity constraints (central risk gateway)", SRC_MAP, "P0", IMPL, 85,
         "W39: price band vs LTP / last close in the W4 risk engine and the manual / rule path; NSE circuit limits "
         "(Dhan quote upper / lower circuit stored in live_quotes) refuse a price outside today's circuit; paper "
         "futures notional in gross exposure; gross-exposure limit on the manual path",
         "Independent QA; circuit fields confirmed against a live Dhan quote",
         "execution/risk_engine.py; orders/risk.py; data/dhan.py",
         "tests/test_w39_risk_gateway.py"),
    _row("AF-09", "Alpha/Factor Engine", "Time-series momentum + vol-adjusted 12-1", "sign(return_n) x vol "
         "scaling; rank(12-1M) volatility-adjusted", SRC_MAP, "P1", IMPL, 85,
         "W39: tsmom_12m and mom_12_1_vol_adj registered point-in-time factors", "Factor IC research before any "
                                                                                  "score use", "quant/factors.py",
         "tests/test_w39_quant_portfolio.py"),
    _row("PF-14", "Portfolio", "Mean-variance with turnover penalty", "max mu'w - lambda w'Sw - kappa turnover",
         SRC_MAP, "P1", IMPL, 85, "W39: turnover_penalty + current_weights in portfolio/optimize (exact proximal step inside the "
                                  "budget / sector-cap shifts); kappa = 0 reproduces the old solver; route falls back "
                                  "to the book's weights", "Independent QA", "portfolio/optimize.py",
         "tests/test_w39_quant_portfolio.py"),
    _row("PF-15", "Portfolio", "Factor neutrality", "Minimise unwanted factor exposure subject to alpha", SRC_MAP,
         "P2", IMPL, 85, "W39: normalize.apply neutralize spec (OLS residual, sector dummies); optimiser neutral_to "
                         "equality constraints", "Independent QA", "quant/normalize.py; portfolio/optimize.py",
         "tests/test_w39_quant_portfolio.py"),
    _row("AF-10", "Alpha/Factor Engine", "Volatility surface + term structure", "IV by strike / maturity, skew, "
         "term structure, Greeks", SRC_MAP, "P2", IMPL, 80,
         "W39: quant.derivatives.vol_surface from fo_contract_daily (moneyness x expiry grid, ATM term structure, "
         "skew, per-strike Greeks); GET /api/data/fo/surface", "Needs F&O contract history (DP-08 store, daily)",
         "quant/derivatives.py", "tests/test_w39_ml_regime_lineage_surface.py"),
    _row("ML-17", "AI/ML", "Unsupervised market regime (HMM)", "HMM / clustering / volatility-breadth state model",
         SRC_MAP, "P1", IMPL, 80, "W39: numpy Gaussian HMM on market features, point-in-time, as regime provider "
                                  "mode 'hmm' (opt-in)", "Owner: choose the regime provider; research validation",
         "ml/hmm.py; ml/regime.py", "tests/test_w39_ml_regime_lineage_surface.py"),
    _row("ML-18", "AI/ML", "Research-to-production lineage", "Version data + features + model + params + code hash",
         SRC_MAP, "P1", IMPL, 85, "W39: ml_model_version.code_version + lineage_json; lineage() manifest + route",
         "Independent QA", "ml/registry.py", "tests/test_w39_ml_regime_lineage_surface.py"),
    _row("BT-18", "Backtesting", "Purged walk-forward for strategies", "Train / test with purge + embargo",
         SRC_MAP, "P1", IMPL, 85, "W39: purge_sessions / embargo_sessions in backtest/walkforward (default 0 = old "
                                  "behaviour)", "Independent QA", "backtest/walkforward.py",
         "tests/test_w39_backtest.py"),
    _row("BT-19", "Backtesting", "Transaction-cost stress sweep", "Net = gross - commissions - slippage - impact - "
         "taxes; multiplier sweep + break-even", SRC_MAP, "P1", IMPL, 85,
         "W39: cost_sweep over cost / slippage multipliers with interpolated break-even multiplier",
         "Independent QA", "backtest/robustness.py", "tests/test_w39_backtest.py"),
]

# rows still in development when the files are generated: shown IN PROGRESS until merged
IN_DEVELOPMENT: set = set()
for _r in NEW_ROWS:
    if _r["ID"] in IN_DEVELOPMENT:
        _r.update({"Status": "IN PROGRESS", "Completion %": "60", "Completed On": "",
                   "Notes": "W39 in development on branch claude/wizardly-curie-fbjeoa (PR #5); not merged yet"})

# ── owner workbook items needing a decision (marked BLOCKED, work moved on) ───────────
NEW_ROWS += [
    _row("EX-17", "Execution", "Aggressive exit: enable flag + CRI-spike / momentum-decay exit",
         "Aggressive exit default-disabled until validated (owner workbook Wave 9)", "Owner deployment tracker: "
         "Wave 9", "P1", "BLOCKED", 60,
         "strategy/aggressive.py: ATR trail, T1 partial, T2 checkpoint; DEFAULTS aggressive_enabled=False but no "
         "code reads the flag (off only because nothing schedules strategy.live); bypasses the W4 risk engine",
         "Decide whether the flag gates strategy.live enter/manage, and whether to add a CRI-spike exit",
         "strategy/aggressive.py; strategy/live.py", "tests/test_aggressive_*.py (74 tests)",
         blocker="Owner decision: (1) should aggressive_enabled=false stop `python -m strategy.live --manage` "
                 "(today it runs regardless)? (2) add a CRI-spike exit (threshold?) and route orders through the "
                 "W4 risk engine? Changing either alters live exit behaviour"),
    _row("EX-18", "Execution", "Basket orders (paper)", "Zerodha / Dhan parity: place a list of orders together",
         SRC_NOTES, "P2", IMPL, 80, "W39: orders/basket.py -- named baskets, margin / risk pre-check of the whole "
                                   "basket, all-or-nothing PAPER placement through the existing order path",
         "LIVE placement stays behind the live-trading master switch", "orders/basket.py",
         "tests/test_w39_retail.py"),
    _row("EX-19", "Execution", "Broker-side GTT / forever orders", "Zerodha GTT / Dhan forever orders",
         SRC_NOTES, "P2", "BLOCKED", 20,
         "ATIP-side trigger rules (orders/rules.py) fire while ATIP runs; nothing is placed at the broker",
         "Place / modify / cancel broker GTT (Dhan forever-order API, Kite GTT) for LIVE positions",
         "orders/rules.py", "",
         blocker="Owner authorization for LIVE broker order placement (live trading is off by design) and the "
                 "Dhan forever-order API enabled on the account"),
    _row("EX-20", "Execution", "Stock SIP (recurring buys, paper)", "Zerodha / Dhan parity: systematic stock SIP",
         SRC_NOTES, "P2", IMPL, 80, "W39: orders/sip.py -- monthly / weekly plans by amount or quantity, executed "
                                   "in PAPER by the scheduler, skipped on holidays, history kept",
         "LIVE SIP stays behind the live-trading master switch", "orders/sip.py", "tests/test_w39_retail.py"),
    _row("UX-01", "Dashboard", "Investor / Trader mode + progressive disclosure", "Consumer UX gap (owner Gap "
         "Analysis: 50% -> 90%)", "Owner deployment tracker: Gap Analysis", "P1", IMPL, 80,
         "Investor / Trader mode preference (W17 INT-001); W39: Simple / Detailed view on /wealth -- Simple hides "
         "the audit, ledger and trade-level tables", "Owner UAT of which panels belong in Simple",
         "dashboard/wealth_page.py", "tests/test_w39_retail.py"),
]

# ── refreshed statuses of existing rows (only where the code or the blocker changed) ─────
UPDATES = {
    "PERF-001": {"Completion %": "90", "Current Implementation": "W15.5 + W39 (PERF-001-01..14 detail gaps "
                 "closed: see the PERF-001-xx rows)", "Next Action / Missing Work": "Independent QA + owner UAT"},
    "SE-05": {"Status": "BLOCKED", "Blocker / Input Required": "Evidence, not a decision: no ML model version has "
              "passed validation (W24: NO_EDGE). Re-train when more history exists (DP-23 backfill) and activate "
              "only an ADOPTABLE model"},
    "BR-08": {"Blocker / Input Required": "Owner: a Dhan-issued sandbox token, then run python -m orders.sandbox_check "
              "--place-test-order"},
    "ENT-07": {"Blocker / Input Required": "Owner decision: how ATIP is exposed to the internet (Cloudflare Tunnel / "
               "Tailscale / VPS reverse proxy), the domain and TLS"},
    "ENT-08": {"Blocker / Input Required": "Depends on ENT-07 (no exposure decided)"},
    "ENT-04": {"Status": "BLOCKED", "Blocker / Input Required": "Owner decision: payment gateway (Razorpay / Stripe / "
               "Cashfree) and its merchant credentials"},
    "ENT-14": {"Status": "BLOCKED", "Blocker / Input Required": "Legal: a qualified professional must review and "
               "sign off each register item (SEBI RA / IA applicability)"},
    "ENT-16": {"Status": "BLOCKED", "Next Action / Missing Work": "DBS-05 (PostgreSQL path) and OPS-04 (container "
               "packaging) are done (W38); multi-instance HA needs the runtime moved to PostgreSQL and a host",
               "Blocker / Input Required": "Owner decision: switch the production runtime to PostgreSQL and choose a "
               "cloud host (OPS-04 packaging is ready)"},
    "ENT-09": {"Blocker / Input Required": "Native apps: Apple / Google developer accounts and an exposure decision "
               "(ENT-07); the PWA (/m) works today on a private network"},
    "UAT-001": {"Status": "BLOCKED", "Blocker / Input Required": "Owner: execute the UAT journeys (docs/W19_UAT_PLAN.md, "
                "W33 section 5) and record acceptance"},
    "QA-001": {"Completion %": "85", "Current Implementation": "W18: 70-test wealth suite, security review, "
               "performance. W33: static security review. W39: load / latency tool (tools/load_test.py, in-process "
               "or against a running dashboard, targets from W18 / W33), Playwright browser smoke tests of every "
               "page and tab, QA suite for untested modules; measured p95 145-184 ms over HTTP at 4-8 threads, "
               "0 errors (docs/W39_QA_PERFORMANCE.md)",
               "Next Action / Missing Work": "Independent testing (ChatGPT) per W33 handoff; the development suite "
               "now runs 820+ tests green (W39); run tools/load_test.py against the production machine"},
    "EX-01": {"Next Action / Missing Work": "Independent QA",
              "Notes+": "W39: tests/test_w39_qa_suite.py -- validation, triggers, OCO / bracket lifecycle, trailing "
              "stops through the paper broker; fix: bracket legs now anchor on the real fill price"},
    "OPS-02": {"Next Action / Missing Work": "Independent QA; network download steps are exercised only live",
               "Notes+": "W39: end-to-end test of run_postmarket on 260 seeded sessions (offline steps run for real) "
               "in tests/test_w39_qa_suite.py"},
    "QR-11": {"Notes+": "W39 QA finding (owner methodology call): a neighbouring parameter value with too few trades "
              "is ignored, so 'stops trading one step away' is not flagged as a knife edge "
              "(backtest/sensitivity.py)"},
    "TA-05": {"Current Implementation": "52-week 23.6/38.2/50/61.8 levels; W39: swing-anchored Fibonacci (last two "
              "5-bar pivots in 120 sessions, 38.2 / 50 / 61.8 levels, nearest level and distance) in technical_ext, "
              "shown in the stock panel", "Next Action / Missing Work": "Independent QA",
              "Key Files": "data/technical.py; data/technical_ext.py", "Evidence": "tests/test_w39_technical_rs_fib.py"},
    "TA-08": {"Current Implementation": "20-day RS with date-matched benchmark; W39 (TA-08b): 63 / 126-session RS vs "
              "the stock's sector index (NSE industry -> NIFTYIT / PHARMA / AUTO / FMCG / METAL / REALTY / ENERGY / "
              "BANK / PSUBANK, NIFTY50 fallback flagged) and percentile within the industry; display only",
              "Next Action / Missing Work": "Independent QA; IC research before any score use",
              "Key Files": "scores/engine.py; data/technical_ext.py", "Evidence": "tests/test_w39_technical_rs_fib.py"},
    "PF-10": {"Notes+": "W39 fix: the sector-cap projection (Dykstra, stopped early) could return weights over the "
              "sector cap (29 of 294 random cases, up to 48 points); replaced by the exact KKT projection "
              "(test_projection_never_breaks_a_sector_cap_and_is_the_nearest_feasible_point)"},
    "PF-12": {"Notes+": "W39 (PERF-001-06): contract-note charge columns stored per trade as fee_breakdown; a "
              "charges-only file becomes FEE rows (tests/test_w39_contract_note_fees.py)"},
    "API-03": {"Completion %": "90", "Current Implementation": "API keys with scopes + per-key limits; /api/v1 alias; "
               "v1 OpenAPI contract written by python -m ops api-docs; W39: deprecation policy "
               "(docs/API_VERSIONING_POLICY.md) enforced by a registry -- Deprecation / Sunset / Link headers, "
               "deprecated in OpenAPI, 410 GONE after sunset, >= 180 days notice validated",
               "Next Action / Missing Work": "Independent QA",
               "Key Files": "enterprise/public_api.py; ops/http.py; ops/__main__.py; docs/API_VERSIONING_POLICY.md",
               "Evidence": "test_a_deprecated_v1_resource_warns_then_answers_410_after_sunset"},
    "ENT-12": {"Next Action / Missing Work": "Independent QA. Tax report content is NOT built: Tax is excluded by the "
               "owner (deployment tracker, Scope Exclusions sheet); revisit only if the owner re-scopes it",
               "Notes+": "W39 check: the remaining gap (tax report) is owner-excluded scope, not open work; W39 adds the "
               "printable performance report (PERF-001-13)"},
    "PF-08": {"Next Action / Missing Work": "Independent QA. Trade-based attribution of the LIVE book now exists in the "
              "wealth performance report (PERF-001-07 / -09 / -10 on portfolio LIVE: broker_sync + PF-12 imports)"},
    "PF-11": {"Next Action / Missing Work": "Independent QA. LIVE XIRR now comes from the wealth performance ledger "
              "(PERF-001-04, portfolio LIVE: Dhan broker_sync + PF-12 broker imports)"},
    "PF-06": {"Completion %": "90", "Current Implementation": "W25: rebalance_plan (NEW/ADD/REDUCE/EXIT/HOLD, band, "
              "min trade, costs, turnover); portfolio kind reweight_band_pct emits ADD/REDUCE with exact quantity. W39: "
              "the W2 backtest simulates ADD / REDUCE (partial rows, averaged entries, max_position_pct cap, P&L "
              "conserved; BUY/SELL-only runs byte-identical); reweight_band_pct accepted by definition validation "
              "(was refused); saved runs, walk-forward and Monte Carlo count positions",
              "Next Action / Missing Work": "Independent QA; the event-driven engine (BT-17) still trades whole "
              "positions (it says so)", "Key Files": "portfolio/optimize.py; strategy_engine/kinds.py; "
              "backtest/engine.py; strategy_engine/adapter.py", "Evidence": "tests/test_w39_backtest_partial.py"},
    "API-05": {"Completion %": "100", "Current Implementation": "W31: python -m ops api-docs (openapi.json + "
               "API_REFERENCE.md, 362 routes with permissions). W39: every public v1 resource has a typed response "
               "schema (openapi-v1.json) checked against the real handlers by a contract test; error bodies "
               "(incl. 422) use the envelope",
               "Next Action / Missing Work": "Independent QA. Per-route schemas for the internal dashboard routes "
               "are not planned: /api/v1 is the published contract", "Evidence": "tests/test_w39_api_contract.py"},
    "SE-01": {"Notes+": "W39: KNOWN_DEFECTS W3-L4 resolved -- ADD / REDUCE decisions are simulated in backtests"},
    "DP-21": {"Notes+": "W39 check: AMFI mutual-fund NAV ingestion (owner note 'amfi for MF data') is this row -- "
              "mf_nav daily at 23:30; MF remains outside the wealth-track mitigation scope (owner Scope Exclusions)"},
    "ENT-11": {"Notes+": "W39 check: ad-hoc intraday price alerts (Zerodha / Dhan parity) are covered here "
               "(W32 evaluate_alerts_intraday every 15 min)"},
    "ENT-19": {"Notes+": "W39 check: watchlists exist (Zerodha / Dhan parity); live quotes beside them are not shown"},
}

# Deployment-tracker wave of every module (owner waves 1-9 are module groups; 11-20 = repo W11-W20)
WAVE_BY_PREFIX = [
    (r"^(INV|WLT|GOL|AAL|RBL|PERF|AIA|INT|QA|UAT|REL)-", None),     # by ID below
    (r"^(DBS|OPS|MON|SEC|API-0[12])", "1"), (r"^(DP|NS-01|AD)", "2"), (r"^TA", "3"),
    (r"^(SC|AF|ML|NS)", "4"), (r"^(SG|SE)", "5"), (r"^(BT|QR)", "6"), (r"^(EX|RK|BR)", "7"),
    (r"^(PF|DB|UX)", "8"), (r"^(ENT|API)", "9+ (Enterprise SaaS)"),
]
WEALTH_WAVE = {"INV-001": "11", "WLT-001": "12", "GOL-001": "13", "AAL-001": "14", "RBL-001": "15",
               "PERF-001": "15.5", "AIA-001": "16", "INT-001": "17", "QA-001": "18", "UAT-001": "19", "REL-001": "20"}


def deployment_wave(fid: str) -> str:
    if fid.startswith("PERF-001"):
        return "15.5"
    if fid in WEALTH_WAVE:
        return WEALTH_WAVE[fid]
    for rx, w in WAVE_BY_PREFIX:
        if w and re.match(rx, fid):
            return w
    return ""


# ── test evidence ──────────────────────────────────────────────────────────────────
def _py_files(key_files: str) -> list:
    out = []
    for part in re.split(r"[;,]", key_files or ""):
        p = part.strip().split(" ")[0]
        if not p or p in ("—", "-"):
            continue
        path = ROOT / p
        if p.endswith("/") or path.is_dir():
            out += sorted(str(x.relative_to(ROOT)) for x in path.glob("*.py") if x.name != "__init__.py")
        elif p.endswith(".py") and path.exists():
            out.append(p)
    return out


def evidence(row: dict, cov: dict) -> tuple[str, str]:
    """(Testing Status, Test Evidence) from line coverage of the row's key files."""
    if row["Status"] == "BLOCKED":
        return "BLOCKED", ""
    files = _py_files(row.get("Key Files", ""))
    named = row.get("Evidence", "")
    w39 = bool(re.search(r"test_w39_|tests/test_\w+\.py|^test_", named or ""))
    if not files:
        return ("COMPLETED" if w39 else "N/A"), (named if w39 else "no code file listed")
    stmts = covered = 0
    mods = set()
    for f in files:
        c = cov.get(f)
        if not c:
            continue
        stmts += c["stmts"]
        covered += c["stmts"] * c["pct"] / 100
        mods.update(t.split(".")[1] if t.startswith("tests.") else t.split(".")[0] for t in c["tests"])
    pct = covered / stmts * 100 if stmts else 0.0
    mods = sorted(m for m in mods if m.startswith("test_"))
    ev = f"{pct:.0f}% of {stmts} lines in {len(files)} key file(s) run by: " + (", ".join(mods[:8]) or "no test")
    if len(mods) > 8:
        ev += f" (+{len(mods) - 8} more)"
    if w39 and named:
        ev = f"{named} | {ev}"
    if pct >= 60 or (w39 and pct > 0):
        return "COMPLETED", ev
    if pct > 0:
        return "IN PROGRESS", ev
    return "YET TO START", ev


def load_coverage(path) -> dict:
    """{file: {pct, stmts, tests[]}} from `coverage json --show-contexts`."""
    d = json.loads(Path(path).read_text())
    out = {}
    for f, v in d["files"].items():
        tests = set()
        for ctxs in (v.get("contexts") or {}).values():
            tests.update(c.rsplit(".", 1)[0] for c in ctxs if c)
        out[f] = {"pct": v["summary"]["percent_covered"], "stmts": v["summary"]["num_statements"],
                  "tests": sorted(tests)}
    return out


# ── CSV ────────────────────────────────────────────────────────────────────────────
def read_csv(path) -> tuple[list, list]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        return list(r.fieldnames), [dict(x) for x in r]


def reconcile(rows: list, fields: list, cov: dict) -> tuple[list, list]:
    for c in NEW_COLUMNS:
        if c not in fields:
            fields.append(c)
    by_id = {r["ID"]: r for r in rows}
    for fid, upd in UPDATES.items():
        r = by_id.get(fid)
        if not r:
            continue
        for k, v in upd.items():
            if k == "Notes+":
                if v not in r["Notes"]:
                    r["Notes"] = (r["Notes"] + " | " if r["Notes"] else "") + v
            else:
                r[k] = v
        r["Last Updated"] = TODAY
    for r in rows:                      # stale "not in master" notes: every wave through W38 is merged
        if "not in master" in r.get("Notes", "") or re.search(r"developed .* on branch w\d", r.get("Notes", "")):
            r["Notes"] = r["Notes"].replace("not in master", "merged to master") + \
                (" | W39 check 2026-10-07: commit is an ancestor of master" if "W39 check" not in r["Notes"] else "")
    seq = max(int(r["Seq"]) for r in rows)
    for n in NEW_ROWS:
        if n["ID"] in by_id:
            by_id[n["ID"]].update({k: v for k, v in n.items() if v})
            continue
        seq += 1
        row = {f: "" for f in fields}
        row.update(n)
        row.update({"Seq": str(seq), "Phase": PHASE_W39[0], "Phase Name": PHASE_W39[1]})
        rows.append(row)
        by_id[n["ID"]] = row
    for r in rows:
        r.setdefault("Blocker / Input Required", "")
        r["Deployment Wave"] = deployment_wave(r["ID"]) or r.get("Deployment Wave", "")
        if cov:
            r["Testing Status"], r["Test Evidence"] = evidence(r, cov)
    return fields, rows


def write_csv(path, fields, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def owner_csv_diff(owner_rows: list, rows: list) -> list:
    mine = {r["ID"]: r for r in rows}
    out = []
    for o in owner_rows:
        m = mine.get(o["ID"])
        if m and (o["Status"] != m["Status"] or o["Completion %"] != m["Completion %"]):
            out.append({"ID": o["ID"], "Feature": o["Feature"], "Owner CSV status": o["Status"],
                        "Owner CSV %": o["Completion %"], "Repo status (W39)": m["Status"],
                        "Repo %": m["Completion %"], "Why": (m.get("Notes") or m.get("Current Implementation"))[:160]})
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--owner-csv")
    ap.add_argument("--owner-xlsx")
    ap.add_argument("--coverage")
    ap.add_argument("--out", default=str(XLSX_OUT))
    a = ap.parse_args()
    fields, rows = read_csv(CSV_PATH)
    cov = load_coverage(a.coverage) if a.coverage else {}
    fields, rows = reconcile(rows, fields, cov)
    write_csv(CSV_PATH, fields, rows)
    print(f"{CSV_PATH}: {len(rows)} rows; status {dict(Counter(r['Status'] for r in rows))}; "
          f"testing {dict(Counter(r['Testing Status'] for r in rows))}")
    if a.owner_xlsx:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from tracker_w39_xlsx import build
        owner_rows = read_csv(a.owner_csv)[1] if a.owner_csv else []
        build(a.owner_xlsx, a.out, fields, rows, owner_csv_diff(owner_rows, rows))
        print(f"wrote {a.out}")
