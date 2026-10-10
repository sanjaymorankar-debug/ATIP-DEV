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
         "sessions + PME of the investor's flows; W39: days invested shown and exported. W40: benchmark "
         "\"nifty50tr\" -- the Nifty 50 with dividends reinvested (data/total_return.py: NSE's TRI method, the price "
         "index exact, its dividend points estimated from the top 50 stored stocks by cap, picked monthly and weighted "
         "daily; nightly 21:10; GET /api/data/total-return), part of the report's audited inputs",
         "Independent QA; the dividend part is an estimate (no free-float or constituent data) -- compare once with "
         "NSE's published TRI", "wealth/perf/data.py; wealth/perf/report.py; data/total_return.py",
         "test_benchmark_return_is_the_price_return_over_the_sessions; tests/test_w40_total_return.py"),
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
         "tools/dhan_token_refresh.py (RenewToken with the documented dhanClientId header, else TOTP + PIN "
         "generateAccessToken; writes config or vault), run daily at 06:30 and at login by the macOS LaunchAgent "
         "com.atip.dhan-token-refresh (W39, PR #4) and at 08:00 by the Windows task. W39b's own scheduler job was "
         "dropped on merging #4 (two renewals a day)",
         "Owner: add dhan_pin + dhan_totp_secret (or vault them) once; run deploy/launchd/install.sh token",
         "tools/dhan_token_refresh.py; deploy/launchd/com.atip.dhan-token-refresh.plist",
         "tests/test_dhan_token_refresh.py", notes="Delivered by W39 (PR #4, merged to master 2026-10-08); "
                                                   "independent validation PENDING"),
    _row("DP-23", "Data Platform", "Long price history (7 years)", "Last 7 years of daily data pulled and stored",
         SRC_NOTES, "P1", IMPL, 90,
         "W39 (PR #4, its DP-11): data/history_backfill.py walks each tracked symbol back to 7 years in 365-day "
         "windows, resumable (prices_daily_backfill), listing-aware, nightly 22:20 (40 symbols); db/purge.py "
         "HISTORY tier keeps prices_daily / ai_scores / predictions 7 years. W39b fix on merge: old windows are "
         "stored with today's corporate-action basis. W39b's own backfill was dropped",
         "Owner: Dhan Data API subscription; the Nifty 500 completes in ~2 weeks of nightly runs",
         "data/history_backfill.py; db/purge.py; data/dhan.py",
         "tests/test_w39_history.py; tests/test_w39b_merge.py",
         notes="Delivered by W39 (PR #4, merged to master 2026-10-08); independent validation PENDING"),
    _row("DB-20", "Dashboard", "Stock history like a trading platform", "History of a stock shown like any "
         "trading software", SRC_NOTES, "P1", IMPL, 90,
         "W26 stock panel + W39: 3Y / 5Y / All, daily / weekly / monthly bars, SMA 20/50/200, EMA 21, Bollinger, "
         "log scale, multi-year returns; checked in headless Chromium. W39b: signal / pattern marks. W40: RSI 14 and "
         "MACD 12-26-9 panels (the server's definitions), an intraday chart (1D / 5D on 15-minute bars, session VWAP, "
         "previous close, intraday scan marks; GET /api/stock/{symbol}/intraday) and the on-demand 75-minute rating",
         "Owner UAT", "dashboard/stock_view.py",
         "test_stock_history_reports_multi_year_returns; tests/test_w40_stock_chart.py"),
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

# ── W39 (PR #4, merged to master 2026-10-08): research, screener, signals, market pulse, options, depth ──
SRC4 = "W39 (PR #4): docs/W39_RESEARCH_HISTORY_OPTIONS_HANDOFF.md"
NEW_ROWS += [
    _row('W39-RS', 'Research', 'Equity research reports (RS-01..10)', 'Institutional-style valuation, targets, ratings, reports with disclosures and tracked calls', SRC4, "P1", IMPL, 85,
         'research/valuation.py (DCF + scenarios + sensitivity, justified P/B, peer / own-history multiples); research/report.py (12-month targets, ratings, SEBI-style disclosures, calls and hit rate); nightly 20:40',
         'No consensus estimates feed; XBRL key lines only (no 3-statement model); hit rates start from the first report and need PaRRVA review before being shown to others', 'research/valuation.py; research/report.py', 'tests/test_w39_research.py',
         owner="Claude (development), PR #4",
         notes="W39 developed on branch ccr-643d84fc-yig8ts (PR #4), merged to master 2026-10-08; NOT "
               "DEPLOYED; independent validation PENDING"),
    _row('W39-SCREENER', 'Research', 'Stock screener: fundamental + technical (SC-20)', 'Query screener with presets, saved screens and alerts', SRC4, "P1", IMPL, 85,
         'research/screener.py: query language (precedence, NOT, IN, aliases), presets, saved screens with new-match alerts, CSV; /screener',
         'Independent QA', 'research/screener.py', 'tests/test_w39_screener.py',
         owner="Claude (development), PR #4",
         notes="W39 developed on branch ccr-643d84fc-yig8ts (PR #4), merged to master 2026-10-08; NOT "
               "DEPLOYED; independent validation PENDING"),
    _row('W39-OPTIONS', 'Derivatives', 'Options strategy builder (OP-01..05)', 'Payoff, breakevens, POP and Greeks for multi-leg strategies', SRC4, "P1", IMPL, 85,
         'quant/options_strategy.py: templates, payoff and breakevens, POP, Greeks, implied IV, chain pricing; POST /api/options/(build|analyse)',
         "Analysis only: one-click execution needs the live OMS and the owner's explicit go-ahead", 'quant/options_strategy.py', 'tests/test_w39_options.py',
         owner="Claude (development), PR #4",
         notes="W39 developed on branch ccr-643d84fc-yig8ts (PR #4), merged to master 2026-10-08; NOT "
               "DEPLOYED; independent validation PENDING"),
    _row('W39-TECH', 'Technical Analysis', 'Technical scans, ratings and signals (TA-01..04)', '30 scans, 19 candle patterns, technical / RS ratings, signals with ATR levels and confluence', SRC4, "P1", IMPL, 85,
         'research/technicals.py + research/tech_signals.py: scans, patterns, ratings, signals with entry / stop / target, confluence, outcomes (TARGET / STOPPED / EXPIRED); nightly 20:30',
         'Trust a scan only after ~30 closed signals (records start on the first run)', 'research/technicals.py; research/tech_signals.py', 'tests/test_w39_technicals.py',
         owner="Claude (development), PR #4",
         notes="W39 developed on branch ccr-643d84fc-yig8ts (PR #4), merged to master 2026-10-08; NOT "
               "DEPLOYED; independent validation PENDING"),
    _row('W39-PULSE', 'Market Intelligence', 'Market pulse (MP-01..05)', 'Global-cue model, GIFT gap, FII flow pressure, participant OI, OI walls', SRC4, "P1", IMPL, 85,
         'research/market_pulse.py + data/participant_oi.py: global-cue model, basis-free GIFT gap, FII streak / absorption, NSE participant OI positioning, OI walls; /market-pulse',
         'Needs Nifty + global history loaded, Dhan index quotes for GIFT, NSE reachable for participant OI', 'research/market_pulse.py; data/participant_oi.py', 'tests/test_w39_market_pulse.py',
         owner="Claude (development), PR #4",
         notes="W39 developed on branch ccr-643d84fc-yig8ts (PR #4), merged to master 2026-10-08; NOT "
               "DEPLOYED; independent validation PENDING"),
    _row('W39-REGIME', 'Market Intelligence', 'Market regime gate (RG-01..04)', 'Distribution / follow-through days, 200-DMA, signals tagged with or against the market', SRC4, "P1", IMPL, 85,
         "research/regime_gate.py: OPEN / CAUTION / CLOSED gate, signals tagged and never alerted against it, 'does the gate help' record",
         "Thresholds are ATIP's choices (IBD tradition); evidence needs 30+ closed signals per side", 'research/regime_gate.py', 'tests/test_w39_regime_gate.py',
         owner="Claude (development), PR #4",
         notes="W39 developed on branch ccr-643d84fc-yig8ts (PR #4), merged to master 2026-10-08; NOT "
               "DEPLOYED; independent validation PENDING"),
    _row('W39-TRACK', 'Research', 'Signal track record by regime and horizon (TR-01..03)', '5/20/60-session returns vs the Nifty per signal, by scan x gate and confluence', SRC4, "P1", IMPL, 85,
         "research/tech_signals.py forward returns and stats, shown next to today's signals",
         'Useful after a few months of signals', 'research/tech_signals.py', 'tests/test_w39_track_record.py',
         owner="Claude (development), PR #4",
         notes="W39 developed on branch ccr-643d84fc-yig8ts (PR #4), merged to master 2026-10-08; NOT "
               "DEPLOYED; independent validation PENDING"),
    _row('W39-WEEKLY', 'Technical Analysis', 'Weekly technical rating (WK-01..02)', 'Weekly rating on completed weeks; daily / weekly agreement', SRC4, "P1", IMPL, 85,
         'research/technicals.py weekly bars, rating, agreement as screener field and signal attribute',
         'Independent QA', 'research/technicals.py', 'tests/test_w39_weekly.py',
         owner="Claude (development), PR #4",
         notes="W39 developed on branch ccr-643d84fc-yig8ts (PR #4), merged to master 2026-10-08; NOT "
               "DEPLOYED; independent validation PENDING"),
    _row('W39-PATTERNS', 'Technical Analysis', 'Chart patterns from swing pivots (CP-01..04) and marks on the stock chart', 'Darvas box, VCP, double bottom, ascending triangle, head and shoulders; W39b: inverse H&S, descending triangle, channels, wedges, chart marks', SRC4, "P1", IMPL, 85,
         'research/patterns.py: patterns as scans with levels, screener setups, alerts held until a positive 30-signal record. W39b (CP-04): inverse head and shoulders, descending triangle, rising / falling channels (breakout and breakdown each), rising / falling wedges -- 14 pattern scans, each calibrated at 0.02-0.1 % of stock-days on random walks; the stock chart (/api/stock/{symbol}/history chart_marks) marks signals, pattern breakouts, candle patterns and the lines of the patterns in place, on daily / weekly / monthly bars, with a toggle',
         'Rule-based approximations; independent QA; each new scan needs 30 closed signals before its alerts start', 'research/patterns.py; dashboard/stock_view.py', 'tests/test_w39_patterns.py; tests/test_w39b_patterns_more.py',
         owner="Claude (development), PR #4",
         notes="W39 developed on branch ccr-643d84fc-yig8ts (PR #4), merged to master 2026-10-08; NOT "
               "DEPLOYED; independent validation PENDING"),
    _row('W39-RSLINE', 'Technical Analysis', 'RS-line new highs and size-group RS ranks (RL-01..02)', 'RS line new highs (also before price); RS ranks within AMFI size groups', SRC4, "P1", IMPL, 85,
         'research/technicals.py RS line; AMFI size groups from shares x price',
         'Independent QA', 'research/technicals.py', 'tests/test_w39_rs_line.py',
         owner="Claude (development), PR #4",
         notes="W39 developed on branch ccr-643d84fc-yig8ts (PR #4), merged to master 2026-10-08; NOT "
               "DEPLOYED; independent validation PENDING"),
    _row('W39-DELIVERY', 'Technical Analysis', 'Delivery-% spike scan (DL-01)', 'NSE delivery percentage spike as a scan and screener field', SRC4, "P1", IMPL, 85,
         'research/technicals.py delivery spike scan, snapshot fields, preset',
         'Independent QA', 'research/technicals.py', 'tests/test_w39_delivery.py',
         owner="Claude (development), PR #4",
         notes="W39 developed on branch ccr-643d84fc-yig8ts (PR #4), merged to master 2026-10-08; NOT "
               "DEPLOYED; independent validation PENDING"),
    _row('W39-SCORECARD', 'Research', 'Explainable fundamental scorecard (FS-01..03)', '5 axes x 6 pass / fail checks with their numbers, stored nightly with a record vs the Nifty', SRC4, "P1", IMPL, 85,
         'research/scorecard.py: value, earnings, growth, dividend, balance-sheet checks; screener fields and presets; nightly 20:50',
         'Reported (not forecast) growth; banks / NBFCs miss six checks by design; record needs months', 'research/scorecard.py', 'tests/test_w39_scorecard.py',
         owner="Claude (development), PR #4",
         notes="W39 developed on branch ccr-643d84fc-yig8ts (PR #4), merged to master 2026-10-08; NOT "
               "DEPLOYED; independent validation PENDING"),
    _row('W39-EVENTS', 'Market Intelligence', 'Macro event calendar (EV-01..03)', 'FOMC, US CPI, payrolls, RBI dates; the NSE session each hits; gap band widening', SRC4, "P1", IMPL, 85,
         'research/event_calendar.py: seeded dates, config / API additions, band widened after US releases',
         "Seeded dates reach end-2026 (RBI Feb-2027): add next year's dates each December", 'research/event_calendar.py', 'tests/test_w39_event_calendar.py',
         owner="Claude (development), PR #4",
         notes="W39 developed on branch ccr-643d84fc-yig8ts (PR #4), merged to master 2026-10-08; NOT "
               "DEPLOYED; independent validation PENDING"),
    _row('W39-INTRADAY', 'Technical Analysis', 'Intraday scans on 15-minute bars (IN-01..03)', 'Opening-range breakout, open = low / high, intraday squeeze with a record to the close', SRC4, "P1", IMPL, 85,
         'research/intraday_signals.py; every 15 minutes 09:45-15:50',
         'Needs the Dhan Data API (15-minute bars) and 3 sessions of stored bars', 'research/intraday_signals.py', 'tests/test_w39_intraday.py',
         owner="Claude (development), PR #4",
         notes="W39 developed on branch ccr-643d84fc-yig8ts (PR #4), merged to master 2026-10-08; NOT "
               "DEPLOYED; independent validation PENDING"),
    _row('W39-DEPTH20', 'Data Platform', '20-level depth, depth-weighted imbalance and order-flow imbalance (DP-01..03, OF-01..03)', 'Dhan 20-level depth WebSocket, DWI, OFI from quote changes (best level and multi-level), persistent flags, validation', SRC4, "P1", IMPL, 85,
         'data/depth20.py: depth feed (off by default), DWI, OFI summed over every book update, logistic validation vs the next 1 / 5 minutes and OFI vs its own interval',
         'Owner: depth20.enabled and the Dhan Data API', 'data/depth20.py', 'tests/test_w39_depth20.py; tests/test_w39b_ofi.py',
         owner="Claude (development), PR #4",
         notes="W39 developed on branch ccr-643d84fc-yig8ts (PR #4), merged to master 2026-10-08; NOT "
               "DEPLOYED; independent validation PENDING"),
    _row('W39-GLOBALSYNC', 'Market Intelligence', 'Synchronised global-cue model (GS-01..03)', '15:30 / 08:45 snapshots of global futures, Asia and FX; the opening gap modelled on them', SRC4, "P1", IMPL, 85,
         'research/global_sync.py: snapshots, walk-forward record vs GIFT',
         'Needs 40 mornings of snapshots (~2 months)', 'research/global_sync.py', 'tests/test_w39_global_sync.py',
         owner="Claude (development), PR #4",
         notes="W39 developed on branch ccr-643d84fc-yig8ts (PR #4), merged to master 2026-10-08; NOT "
               "DEPLOYED; independent validation PENDING"),
    _row('W39-NLSCREEN', 'Research', 'English to screener query (NL-01..03)', 'Plain-English screener queries, validated and never run unseen', SRC4, "P1", IMPL, 85,
         'research/screener_nl.py: Claude path when enabled, rule translator otherwise; POST /api/screener/ask',
         'Claude path needs a working Anthropic key (KD-001)', 'research/screener_nl.py', 'tests/test_w39_screener_nl.py',
         owner="Claude (development), PR #4",
         notes="W39 developed on branch ccr-643d84fc-yig8ts (PR #4), merged to master 2026-10-08; NOT "
               "DEPLOYED; independent validation PENDING"),
    _row('W39-MCP', 'APIs', 'Read-only ATIP MCP server (MC-01..03)', '12 read-only tools over stdio on a query-only database connection', SRC4, "P1", IMPL, 85,
         'tools/atip_mcp.py; W40: 20 tools -- adds dvm, earnings_surprise, mf_search / mf_analytics / mf_sip / '
         'mf_compare, backtest_validation (stored CPCV / PBO) and chart_marks, each checked under query_only',
         'Local stdio only (no hosted transport)', 'tools/atip_mcp.py', 'tests/test_w39_mcp.py',
         owner="Claude (development), PR #4",
         notes="W39 developed on branch ccr-643d84fc-yig8ts (PR #4), merged to master 2026-10-08; NOT "
               "DEPLOYED; independent validation PENDING"),
    _row('W39-ORDERBOOK', 'Market Intelligence', 'Order-book pressure and open orders (OB-01..03)', 'Market-wide pending buy / sell pressure; a read-only view of your open orders', SRC4, "P1", IMPL, 85,
         'data/order_pressure.py (every 15 minutes, market hours); portfolio/open_orders.py',
         'Needs the Dhan Data API; context, not a signal', 'data/order_pressure.py; portfolio/open_orders.py', 'tests/test_w39_market_pulse.py',
         owner="Claude (development), PR #4",
         notes="W39 developed on branch ccr-643d84fc-yig8ts (PR #4), merged to master 2026-10-08; NOT "
               "DEPLOYED; independent validation PENDING"),
]

# ── W39b round 3 (2026-10-08): analysis tools from docs/ANALYSIS_TOOLS_AND_SIGNALS_PLAN_2026-10.md and the
#    gap analysis (section 4), built on branch claude/wizardly-curie-fbjeoa (PR #5) ─────────────────────────
SRC5 = "W39b (PR #5): docs/ATIP_GAP_ANALYSIS_2026-10.md section 4; docs/ANALYSIS_TOOLS_AND_SIGNALS_PLAN_2026-10.md"
NOTE5 = "W39b developed 2026-10-08 on branch claude/wizardly-curie-fbjeoa (PR #5); independent validation PENDING"
NEW_ROWS += [
    _row("W39B-TF75", "Technical Analysis", "75-minute technical rating and its agreement with the daily one",
         "Trendlyne / TradingView-style intraday timeframe rating", SRC5, "P2", IMPL, 85,
         "research/technicals.py bars_75 / rating_75: the daily rating's votes on 75-minute bars built only from "
         "stored 15-minute bars (09:15 / 10:30 / 11:45 / 13:00 / 14:15), completed bars only, 35 bars (7 sessions) "
         "needed, never a stale session; stored in technical_snapshot (tech_rating_75*, rsi_14_75, "
         "supertrend_dir_75, bar_75_end, mtf_alignment_75) by the 20:30 run; screener fields and a preset",
         "Independent QA. W40: a 75m column on /signals and GET /api/stock/{symbol}/rating-75 (on demand from the "
         "stored 15-minute bars, completed bars only); the /signals column is still the 20:30 snapshot",
         "research/technicals.py; research/tech_signals.py; research/screener.py",
         "tests/test_w39b_tf75.py; tests/test_w39b_tf75_signals.py",
         notes=NOTE5),
    _row("W39B-DVM", "Research", "DVM view: durability, valuation, momentum 0-100 and zones",
         "Trendlyne-style DVM scores with explanations", SRC5, "P2", IMPL, 85,
         "research/dvm.py: durability from the scorecard's 12 health / past-performance checks, valuation from "
         "the research fair value (60 %) and P/E (P/B for financials) vs the industry median (40 %), momentum "
         "from the technical and RS ratings; every score explains itself with its numbers; seven zones; stored "
         "nightly on fundamental_scorecard (dvm_*); GET /api/research/dvm/{symbol}; DVM card on /research; 3 "
         "screener presets",
         "Independent QA; weights are ATIP's definitions, not backtested -- record whether the zones pay before "
         "using them", "research/dvm.py; research/scorecard.py; dashboard/w39_routes.py", "tests/test_w39b_dvm.py",
         notes=NOTE5),
    _row("W39B-PEAD", "Research", "Earnings surprise (SUE), EPS-trend proxy and a post-earnings-drift scan",
         "Earnings surprise and PEAD (gap analysis 4.7)", SRC5, "P2", IMPL, 85,
         "research/earnings_surprise.py: SUE and revenue SUE vs the same quarter a year earlier (8-quarter "
         "standard deviation, 4 needed), point in time from the NSE broadcast date; EPS-trend revisions proxy; "
         "pead_bull / pead_bear scans (|SUE| >= 2, first session after the filing, 60-session horizon) stored "
         "and tracked like every scan, alerts held until 30 closed signals with positive expectancy; 20:35 job; "
         "screener fields and a preset; GET /api/research/earnings-surprise/{symbol}",
         "Independent QA; needs 9+ quarters of NSE XBRL history (run the history backfill); no consensus "
         "estimates (ATIP's own history only); filings arrive with the Saturday fundamentals job",
         "research/earnings_surprise.py; research/tech_signals.py", "tests/test_w39b_earnings_surprise.py",
         notes=NOTE5, blocker="For a consensus-based surprise: a consensus estimates feed (not available today)"),
    _row("W39B-MFA", "Wealth / Mutual funds", "Mutual fund analytics on stored AMFI NAVs (read-only)",
         "Value Research / Kuvera-style scheme analytics (gap analysis 4.3)", SRC5, "P2", IMPL, 85,
         "data/mf_analytics.py: point-to-point and CAGR returns, 1Y / 3Y rolling returns with hurdle stats, "
         "volatility / Sharpe / Sortino, drawdown with dates, beta / alpha / tracking error vs a price index, SIP "
         "and lump-sum XIRR, category rank among stored schemes, side-by-side compare; 3 read-only GET routes; "
         "scheme detail card on /data-platform. W40: step-up SIP, stamp duty (0.005 %) and units rounded down to 3 "
         "decimals by default, optional flat exit load per instalment; volatility annualised by the NAVs actually "
         "observed a year (not a fixed 252)",
         "Independent QA. Holding MFs in the wealth ledger stays out of scope (owner Scope Exclusions); "
         "benchmark is a price index (the W40 Nifty 50 TR is not wired here yet); peers limited to stored schemes; "
         "tiered exit loads not modelled",
         "data/mf_analytics.py; dashboard/w35_routes.py; dashboard/w35_page.py",
         "tests/test_w39b_mf_analytics.py", notes=NOTE5),
]

# ── W40 (2026-10-09): what remained buildable after W39b -- gap analysis section 4 items 5 and 9 ──────
SRC6 = "W40 (PR #9): docs/ATIP_GAP_ANALYSIS_2026-10.md section 4 items 5 and 9; tracker next actions"
W40N = "W40 developed 2026-10-09 on branch claude/wizardly-curie-fbjeoa (PR #9); independent validation PENDING"
NEW_ROWS += [
    _row("W40-RISKMODEL", "Risk & Portfolio", "Fundamental factor risk model (Barra-style)",
         "Factor exposures, factor returns, covariance, specific risk and portfolio risk decomposition", SRC6, "P1",
         IMPL, 85,
         "quant/risk_model.py: 8 styles (size, beta, momentum, residual volatility, book-to-price, earnings yield, "
         "ROE, liquidity) point in time, winsorised and cap-standardised, NSE industries (small ones pooled); "
         "daily sqrt(cap)-weighted regression with the industry constraint and robust t statistics; EWMA / "
         "Newey-West factor covariance; shrunk EWMA specific risk; decomposition of the PAPER / LIVE book (factor vs "
         "specific, per factor, marginal per stock, beta, active risk vs a cap-weighted Nifty 50 proxy); bias "
         "statistic on standard portfolios; nightly 21:45, incremental; /trading and /quant cards",
         "Independent QA on the real database; needs filed share counts for 30+ stocks; universe and industries are "
         "today's (survivorship); no free-float data", "quant/risk_model.py; portfolio/risk.py; "
         "dashboard/risk_model_routes.py", "tests/test_w40_risk_model.py", notes=W40N),
    _row("W40-META", "AI/ML", "Meta-labelling of the technical signals (AFML ch. 3-4, 7, 10)",
         "A second model deciding which primary signals to take, with bet sizes", SRC6, "P2", IMPL, 85,
         "ml/meta_label.py: triple-barrier labels from the signal engine's own outcome rule; 37 features known at "
         "the signal's close; sample weights by average uniqueness; purged k-fold with embargo; GBM / logistic / RF; "
         "out-of-fold precision / recall / AUC and the kept signals' expectancy vs all; AFML bet sizing (reported "
         "only); ADOPTABLE only with 200+ signals and t >= 2, and the registry refuses activating anything else; "
         "Saturday retrain and 20:45 scoring, both off unless meta_label.enabled; /signals column when adopted",
         "Owner: switch on after a few hundred closed signals exist and a version is ADOPTABLE; independent QA",
         "ml/meta_label.py; ml/validation.py; dashboard/w40_routes.py", "tests/test_w40_meta_label.py",
         notes=W40N),
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
         "Wave 9", "P1", IMPL, 85,
         "strategy/aggressive.py: ATR trail, T1 partial, T2 checkpoint. W39b (decision taken under the owner's "
         "'take the decisions' instruction): aggressive_enabled=false refuses NEW entries in strategy.live, while "
         "open positions keep being managed (stops / targets / trails), so switching it off never strands a "
         "position; new cri_exit_threshold (default off) closes the remaining quantity with MARKET_RISK_EXIT when "
         "the stock's crash-risk index reaches it, on the existing broker path (pretrade checks incl. price band "
         "and circuit limits unchanged); threshold validated to (0, 100]",
         "Independent QA; the owner picks a CRI threshold (e.g. 80) after reviewing CRI history before turning it "
         "on; the aggressive strategy stays unscheduled and PAPER",
         "strategy/aggressive.py; strategy/live.py",
         "tests/test_strategy_live.py (test_disabled_flag_refuses_new_entries_but_keeps_managing_open_positions, "
         "test_cri_spike_exits_the_whole_position_only_when_configured, test_cri_below_threshold_or_unknown_does_"
         "not_exit, test_cri_threshold_is_validated); tests/test_aggressive_*.py",
         notes="W39b developed 2026-10-08 on branch claude/wizardly-curie-fbjeoa (PR #5); was BLOCKED on an owner "
               "decision, decided as described (reversible: one config key each); independent validation PENDING"),
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
         "LIVE SIP stays behind the live-trading master switch", "orders/sip.py",
         "tests/test_w39_retail.py; tests/test_w39_notify_transactions.py",
         notes="W39 developed 2026-10-07 (PR #5); fix: with two or more plans due, the second order waited on "
               "the first plan's uncommitted rows and failed -- the loop now commits before each placement"),
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
    "ENT-04": {"Status": "BLOCKED", "Completion %": "90",
               "Current Implementation": "Plans, subscriptions, invoices DRAFT/OPEN/PAID, provider interface "
               "(sandbox/noop), dunning + grace, payment webhook consumer. W39b (gateway decision delegated: "
               "Razorpay -- UPI / cards / netbanking, native Subscriptions): enterprise/razorpay.py on the existing "
               "payments interface -- REST v1 invoices with idempotent receipts, customers, plans, autopay "
               "subscriptions, cancel; signed webhook mapping onto invoices, payments, refunds, dunning and grace "
               "(out-of-order safe, money once per Razorpay id); reconcile() polling so payments settle without a "
               "public webhook; keys only from the vault; live keys refused without billing.razorpay.allow_live "
               "and the production environment",
               "Next Action / Missing Work": "Owner: run the flows in Razorpay test mode (docs/BILLING_RAZORPAY.md); "
               "GST tax lines are not built (ENT-14 PAYMENTS-TAX)",
               "Key Files": "enterprise/payments.py; enterprise/razorpay.py; enterprise/billing.py; enterprise/w32.py; "
                            "db/schema_billing.py",
               "Evidence": "tests/test_w39b_razorpay.py (26 tests on recorded Razorpay v1 response shapes)",
               "Blocker / Input Required": "Owner: a KYC-activated Razorpay business account with Invoices and "
               "Subscriptions enabled; store RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET / WEBHOOK_SECRET_RAZORPAY with "
               "python -m ops vault-set; registering the webhook URL needs ENT-07 (public HTTPS)"},
    "API-04": {"Current Implementation": "Signed inbound/outbound webhooks + W32 business consumers (payments, "
               "broker order.update), event status PROCESSED/IGNORED/FAILED. W39b: the Razorpay provider payloads "
               "are mapped (POST /api/webhooks/razorpay: X-Razorpay-Signature HMAC-SHA256 of the raw body, event-id "
               "and body-digest dedupe, 72 h replay bound, REJECTED rows for bad signatures)",
               "Next Action / Missing Work": "Independent QA; verify against real Razorpay test-mode deliveries "
               "once ENT-07 gives ATIP a public URL; a broker postback mapping when a broker offers one",
               "Key Files": "enterprise/w32.py; enterprise/razorpay.py; ops/webhooks.py; dashboard/ops_routes.py",
               "Evidence": "tests/test_w39b_razorpay.py"},
    "ENT-14": {"Status": "BLOCKED", "Blocker / Input Required": "Legal: a qualified professional must review and "
               "sign off each register item (SEBI RA / IA applicability)"},
    # 2026-10-09: the database plan changed -- PostgreSQL is the final server database, MySQL support removed
    "DBS-05": {"Next Action / Missing Work": "Run the scheduler's write jobs for a few days on a PostgreSQL copy; "
               "connection pool; then decide the runtime switch. PostgreSQL is the final server database (VPS or "
               "managed PostgreSQL 16: Hostinger shared hosting offers none)",
               "Key Files": "db/postgres.py; db/dialect_scan.py; db/backend.py; tools/sqlite_to_postgres.py; "
                            "tools/schema_sql.py; docs/POSTGRESQL_MIGRATION.md",
               "Notes+": ("2026-10-09 plan change: PostgreSQL is the final server database; the MySQL backend "
                          "(PR #8: db/mysql.py, tools/sqlite_to_mysql.py, tools/export_mysql.py, "
                          "atip_schema.mysql.sql, compose profile) was removed and mysql:// URLs are refused; "
                          "db/sql/atip_schema.postgresql.sql is now generated by tools/schema_sql.py and tested "
                          "(tests/test_schema_sql.py)",
                          "2026-10-10: PgConnection runtime fixes (from PR #12): BEGIN IMMEDIATE as an advisory "
                          "lock (ops.jobs locks and every migration now apply on PostgreSQL, so a natively built "
                          "database gets the append-only guards), per-statement savepoints in write "
                          "transactions, lastrowid, PRAGMA query_only as READ ONLY, DROP TRIGGER / CREATE OR "
                          "REPLACE TRIGGER, UTC session time zone, identity seq on perf_ledger / ml_dl_benefit "
                          "for rowid; 14 live tests; CI runs them against a PostgreSQL 16 service"),
               "Last Updated": "2026-10-10"},
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
    "EX-01": {"Next Action / Missing Work": "Independent QA", "Key Files": "orders/rules.py",
              "Evidence": "tests/test_w39_qa_suite.py",
              "Notes+": "W39: tests/test_w39_qa_suite.py -- validation, triggers, OCO / bracket lifecycle, trailing "
              "stops through the paper broker; fix: bracket legs now anchor on the real fill price"},
    "OPS-02": {"Next Action / Missing Work": "Independent QA; network download steps are exercised only live",
               "Key Files": "pipeline/scheduler.py; .github/workflows/tests.yml",
               "Evidence": "test_postmarket_scores_a_seeded_session_end_to_end; "
                           "test_postmarket_refuses_a_session_that_is_a_copy_of_the_previous_one",
               "Notes+": "W39: end-to-end test of run_postmarket on 260 seeded sessions (offline steps run for real) "
               "in tests/test_w39_qa_suite.py"},
    "OPS-04": {"Completion %": "75",
               "Current Implementation": "W38: Dockerfile (IST, non-root, volume, healthcheck), docker-compose "
               "(127.0.0.1-only ports, optional postgres profile), deploy/atip.env.example, cloud-init VM. W39b: the "
               "image is BUILT and smoke-tested (dev container + .github/workflows/docker.yml on every PR): --init "
               "on an empty volume, /health/live and /health/ready 200, uid 10001, IST, no state / .git / .claude "
               "in the image, live_gate() closed; BASE_IMAGE build arg for registry mirrors; .dockerignore fix "
               "(local agent worktrees had added 75 MB)",
               "Next Action / Missing Work": "Owner: choose a host (ENT-16) and exposure (ENT-07); Kubernetes "
               "manifests and a registry push only after that",
               "Key Files": "Dockerfile; docker-compose.yml; .dockerignore; .github/workflows/docker.yml; "
                            "docs/CONTAINER_DEPLOYMENT.md",
               "Evidence": ".github/workflows/docker.yml; docs/CONTAINER_DEPLOYMENT.md (Status)"},
    "QR-11": {"Current Implementation": "W23: one-at-a-time sensitivity curves, stability, knife-edge flags, 2-D "
              "heat map. W39b: a +-1 step neighbour with fewer than min_trades trades (or a failed run) is listed in "
              "thin_neighbours / thin_edges (study summary and CLI too) instead of vanishing; knife_edge and "
              "robust_share still compare only neighbours that have a metric",
              "Next Action / Missing Work": "Independent QA",
              "Evidence": "test_a_neighbour_that_stops_trading_is_a_thin_edge_not_a_knife_edge; "
                          "test_stability_and_knife_edge_by_hand (tests/test_w39_qa_suite.py)",
              "Notes+": "W39 QA finding (methodology call): 'stops trading one step away' was not flagged. W39b "
              "decision: report it as a separate thin-edge fragility flag rather than a knife edge, so robust_share "
              "keeps its meaning"},
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
              "Next Action / Missing Work": "Independent QA. W39b: the event-driven engine (BT-17) now trades "
              "ADD / REDUCE too, through its own order model", "Key Files": "portfolio/optimize.py; "
              "strategy_engine/kinds.py; backtest/engine.py; backtest/event_driven.py; strategy_engine/adapter.py",
              "Evidence": "tests/test_w39_backtest_partial.py"},
    "BT-14": {"Completion %": "90", "Current Implementation": "W23: robustness battery (costs x2, slippage x3, "
              "universe halves, period halves, regimes, Monte Carlo) with verdict. W39b: backtest/cpcv.py -- "
              "combinatorial purged cross-validation (N groups, k test groups, purge + embargo per AFML 7.4, "
              "C(N-1,k-1) out-of-sample paths with their distribution) and PBO by CSCV (Bailey et al.: logits, "
              "degradation regression, probability of loss; from a matrix or an optimisation's trials); "
              "python -m backtest cpcv | pbo; POST /api/backtests/cpcv and /api/backtests/{id}/pbo",
              "Next Action / Missing Work": "Independent QA. W40: first / second-order stochastic dominance of the "
              "selected trial's out-of-sample results over a random selection (in pbo() and CPCV); CPCV on the "
              "event-driven engine (engine=\"event_driven\")",
              "Key Files": "backtest/robustness.py; backtest/cpcv.py",
              "Evidence": "tests/test_w39b_cpcv_pbo.py; tests/test_w39b_cpcv_dominance.py"},
    "BT-17": {"Current Implementation": "W34: backtest/event_driven.py -- event queue over daily or intraday bars; "
              "latency, partial fills vs bar volume, MARKET/LIMIT/STOP, impact-priced fills, TWAP slices, intrabar "
              "protective exits; same Strategy interface; stored as kind event_driven. W39b: ADD / REDUCE through "
              "the order model (matches the W2 engine leg for leg on a hand-computed case; BUY / SELL-only runs "
              "byte-identical); fix: a SELL with nothing left to sell (a max-hold exit queued on the bar a stop "
              "fired) was charged costs and a fill, an over-sized SELL was charged on its full size, and a slow "
              "max-hold exit was queued again every session",
              "Next Action / Missing Work": "Independent QA. W40: reports the adapter's skipped decisions like W2; "
              "fix: the end-of-window close-out is now in the last day's return (chained returns overstated final "
              "equity by the close-out costs). It still does not trade SHORT (W2 does, as futures)",
              "Evidence": "tests/test_w39_backtest_partial.py (test_event_driven_*); "
                          "tests/test_w39b_event_driven_skips.py"},
    "BT-04": {"Completion %": "90", "Current Implementation": "W23: grid / random / adaptive optimiser with trial runs, "
              "test-window refusal, Deflated Sharpe + overfit warning. W40: method=\"bayes\" -- a Tree-structured "
              "Parzen Estimator (Bergstra et al. 2011) in numpy after a seeded random start, same trial storage and "
              "deflated-Sharpe accounting",
              "Next Action / Missing Work": "Independent QA",
              "Key Files": "backtest/optimize.py", "Evidence": "tests/test_w39b_bayes_optimize.py"},
    "QR-05": {"Completion %": "90", "Next Action / Missing Work": "Independent QA. W40: the W2 backtest simulates "
              "SHORT / COVER as near-month stock futures (lots, margin, daily variation margin, NSE F&O costs, a "
              "roll N sessions before expiry; refused, never priced off spot, without futures history); the paper "
              "book can auto-roll (futures.auto_roll, off by default). Not modelled: physical settlement, SPAN margin",
              "Key Files": "execution/futures_paper.py; backtest/futures.py; backtest/engine.py",
              "Evidence": "tests/test_w40_futures_backtest.py"},
    "QR-06": {"Next Action / Missing Work": "Independent QA. W40: long/short portfolios backtest with their futures "
              "legs (see QR-05); lot granularity still makes small books non-neutral -- check exposure after sizing",
              "Evidence": "tests/test_w40_futures_backtest.py"},
    "ENT-15": {"Completion %": "85", "Next Action / Missing Work": "Owner: options.enabled; QA. W40: option-overlay "
               "strategies (strategy_engine/option_overlay.py) emit multi-leg OPTION intents -- covered call, "
               "protective put, spreads, iron condor and more, strikes by delta or % OTM, monthly expiry -- sized by "
               "the risk engine (max loss, margin approximation, naked short calls refused unless allowed), filled "
               "all-or-nothing in the paper options book, marked daily, exited by rule, settled at expiry; PAPER "
               "only, LIVE refused; a replay on stored daily option prices",
               "Key Files": "execution/options_paper.py; execution/option_intents.py; strategy_engine/option_overlay.py",
               "Evidence": "tests/test_w40_option_intents.py; docs/W40_OPTION_INTENTS_HANDOFF.md"},
    "API-05": {"Completion %": "100", "Current Implementation": "W31: python -m ops api-docs (openapi.json + "
               "API_REFERENCE.md, 362 routes with permissions). W39: every public v1 resource has a typed response "
               "schema (openapi-v1.json) checked against the real handlers by a contract test; error bodies "
               "(incl. 422) use the envelope",
               "Next Action / Missing Work": "Independent QA. Per-route schemas for the internal dashboard routes "
               "are not planned: /api/v1 is the published contract", "Evidence": "tests/test_w39_api_contract.py"},
    "SE-01": {"Notes+": "W39: KNOWN_DEFECTS W3-L4 resolved -- ADD / REDUCE decisions are simulated in backtests"},
    "MON-03": {"Notes+": "W39 audit of every notify path: an alert sent while the caller held an uncommitted write "
               "stalled 60 s and was lost -- fixed in SIP run_due, the wealth investor cycle, the Dhan live-quote "
               "refresh and portfolio sync; log_job and the Dhan tick / index flushes now close on failure "
               "(tests/test_w39_notify_transactions.py)"},
    "DP-21": {"Notes+": "W39 check: AMFI mutual-fund NAV ingestion (owner note 'amfi for MF data') is this row -- "
              "mf_nav daily at 23:30; MF remains outside the wealth-track mitigation scope (owner Scope Exclusions)"},
    "ENT-11": {"Notes+": "W39 check: ad-hoc intraday price alerts (Zerodha / Dhan parity) are covered here "
               "(W32 evaluate_alerts_intraday every 15 min)"},
    "ENT-19": {"Notes+": "W39 check: watchlists exist (Zerodha / Dhan parity); live quotes beside them are not shown"},
}

# Deployment-tracker wave of every module (owner waves 1-9 are module groups; 11-20 = repo W11-W20)
WAVE_BY_PREFIX = [
    (r"^W40-", "W40 (PR #9)"),
    (r"^W39B-", "W39b (PR #5)"),
    (r"^W39-", "W39 (PR #4)"),
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
            if k == "Notes+":                # one note, or a tuple of notes appended in turn
                for note in (v if isinstance(v, tuple) else (v,)):
                    if note not in r["Notes"]:
                        r["Notes"] = (r["Notes"] + " | " if r["Notes"] else "") + note
            else:
                r[k] = v
        r["Last Updated"] = upd.get("Last Updated", TODAY)
    for r in rows:                      # stale "not in master" notes: every wave through W38 is merged
        if "not in master" in r.get("Notes", "") or re.search(r"developed .* on branch w\d", r.get("Notes", "")):
            r["Notes"] = r["Notes"].replace("not in master", "merged to master") + \
                (" | W39 check 2026-10-07: commit is an ancestor of master" if "W39 check" not in r["Notes"] else "")
    seq = max(int(r["Seq"]) for r in rows)
    for n in NEW_ROWS:
        if n["ID"] in by_id:
            # the row's own blocker is authoritative even when empty: a decided item (EX-17) must lose its old one
            by_id[n["ID"]].update({k: v for k, v in n.items() if v or k == "Blocker / Input Required"})
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
