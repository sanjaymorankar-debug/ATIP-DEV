# ATIP baseline — Phase 1

Extracted from `D:\Projects\ATIP` at `cb84a6adbbabf3d64a97ed076388b47e8080d97e Merge remote-tracking branch 'origin/master'` (branch `master`).
Every figure below is read out of the code or the database, so later phases can be diffed against it rather than described.

| Python files | Python lines | DB tables | DB rows | API routes | Scheduled jobs | Tests |
|---|---|---|---|---|---|---|
| 57 | 15,754 | 28 | 299,105 | 15 | 11 | 191 |

## Modules

| File | Lines | Purpose |
|---|---|---|
| `dashboard/server.py` | 1413 | ATIP — FastAPI Dashboard Server (http://localhost:8000) |
| `data/dhan.py` | 1275 | ATIP — Dhan API Integration (Real-Time + Historical Data) |
| `orders/rules.py` | 945 | ATIP — Buy/Sell Target & Stoploss Rules Engine |
| `pipeline/scheduler.py` | 873 | ATIP — Master Scheduler |
| `scores/signal_log.py` | 689 | ATIP — Append-only signal log and momentum outcome tracking |
| `tests/test_signal_pipeline.py` | 560 | Regression tests for the signal pipeline breakage found on 2026-09-18. |
| `scores/engine.py` | 543 | ATIP — AI Scoring Engine: VPI, MRI, RRI, CRI, ZPI, MSI, MH, ACS, ATIP Master |
| `orders/paper.py` | 476 | A simulated Dhan broker for real-time testing. |
| `strategy/positions.py` | 418 | Persistent state for aggressively-managed positions. |
| `data/technical.py` | 397 | ATIP — Technical Indicators Engine (35 indicators via pandas-ta) |
| `main.py` | 393 | ATIP — AI Trading Intelligence Platform  v0.2 |
| `data/dhan_ws.py` | 392 | ATIP — Real-time NSE Index Feed via Dhan WebSocket (Live Market Feed) |
| `strategy/live.py` | 392 | Runs the aggressive strategy against a broker, in real time. |
| `strategy/aggressive.py` | 364 | Aggressive exit management: book half at +3%, let the rest run. |
| `db/schema.py` | 354 | ATIP — Database Schema |
| `tests/test_index_feed.py` | 339 | Regression tests for the Dhan index WebSocket feed, after the 2026-09-19/20 |
| `strategy/backtest_aggressive.py` | 333 | Deterministic replay of the aggressive exit strategy over a price path. |
| `scores/backtest.py` | 332 | ATIP — Backtest Harness |
| `tests/test_regressions.py` | 327 | Regression tests for defects that actually occurred. |
| `tests/test_strategy_live.py` | 314 | The wiring between strategy, state and broker. |
| `orders/broker.py` | 310 | ATIP — Manual Order Execution (Buy / Sell via Dhan) |
| `data/bhavcopy.py` | 292 | ATIP — NSE Bhavcopy Downloader (Phase 2A) |
| `tests/test_aggressive_lifecycle.py` | 283 | Position lifecycle against a real (temporary) database: state transitions, |
| `tests/test_aggressive_decisions.py` | 275 | Pure decision logic: configuration, sizing, trailing arithmetic, the +6% |
| `dhan_test.py` | 270 | ATIP — Dhan Connection Tester + Data Loader |
| `alerts/telegram.py` | 266 | ATIP — Telegram Alert Engine (10 alert types) |
| `tests/test_paper_broker.py` | 261 | Paper broker and environment routing. |
| `scores/portfolio_health.py` | 255 | ATIP — Portfolio Health Score |
| `diagnose.py` | 252 | ATIP Diagnostic Tool |
| `scores/accuracy.py` | 230 | ATIP — Accuracy Tracker: prediction vs actual 5/10/20-day outcomes |
| `scores/predictions.py` | 212 | ATIP — Prediction Writer |
| `strategy/backtest_history.py` | 196 | Measure the aggressive exit policy against real bars. |
| `data/news.py` | 179 | ATIP — News Fetcher + Claude API Sentiment Classifier |
| `data/fundamentals.py` | 146 | ATIP — Fundamental Data Fetcher (Alpha Vantage + Screener.in fallback) |
| `data/markets.py` | 139 | ATIP — Global Markets & Intraday Index Fetcher |
| `data/index_constituents.py` | 134 | ATIP — NSE Index Constituent Lists |
| `data/companies.py` | 126 | ATIP — Symbol → Company Name Lookup |
| `db/purge.py` | 122 | ATIP — Database Retention / Purge Utility |
| `tests/test_aggressive_backtest.py` | 117 | Scenario replay tests. |
| `orders/environment.py` | 109 | Which broker does an order actually reach? |
| `publish_snapshot.py` | 98 | ATIP — publish a read-only dashboard snapshot to bkesari.com |
| `utils/trading_calendar.py` | 90 | ATIP — NSE Trading Calendar |
| `portfolio/zerodha.py` | 87 | ATIP — Zerodha Kite API Portfolio Sync + Portfolio Health Score |
| `tests/test_fundamentals_screener.py` | 57 | Screener.in is the only fundamentals source ATIP has without an Alpha Vantage |
| `find_index_tickers.py` | 44 | Run this on your own machine (needs real internet access to Yahoo Finance). |
| `tests/conftest.py` | 42 | Shared test fixtures. |
| `setup.py` | 14 |  |
| `alerts/__init__.py` | 2 |  |
| `dashboard/__init__.py` | 2 |  |
| `data/__init__.py` | 2 |  |
| `db/__init__.py` | 2 |  |
| `orders/__init__.py` | 2 | ATIP - order rules engine and broker execution. |
| `pipeline/__init__.py` | 2 |  |
| `portfolio/__init__.py` | 2 |  |
| `scores/__init__.py` | 2 |  |
| `strategy/__init__.py` | 2 | Trade-management strategies layered on top of ATIP's existing signals. |
| `utils/__init__.py` | 1 |  |

## Database

| Table | Rows | Cols | Span |
|---|---|---|---|
| `accuracy_tracker` | 2,004 | 17 | 2026-09-08 03:08:18 → 2026-09-20 02:46:39 |
| `ai_scores` | 6,637 | 28 | 2026-07-25 → 2026-09-18 |
| `bulk_deals` | 421 | 7 | 2026-09-07 → 2026-09-18 |
| `fii_dii_market` | 6 | 14 | 2026-07-28 → 2026-09-18 |
| `fundamental_data` | 0 | 34 |  |
| `global_markets` | 40 | 37 | 2026-07-25 → 2026-09-19 |
| `index_levels` | 41,624 | 33 | 2026-07-27 → 2026-09-18 |
| `institutional_data` | 0 | 11 |  |
| `live_quotes` | 5,506 | 11 | 2026-07-27 12:56:06 → 2026-09-20 22:01:42 |
| `live_ticks` | 0 | 11 |  |
| `market_health` | 15 | 17 | 2026-07-25 → 2026-09-18 |
| `news_articles` | 1,249 | 14 | 2026-07-25 06:22:44 → 2026-09-10 05:00:32 |
| `order_log` | 15 | 14 | 2026-09-08 12:05:34 → 2026-09-17 09:17:30 |
| `order_rules` | 10 | 45 | 2026-09-10T11:45:01 → 2026-09-10T13:10:32 |
| `paper_account` | 1 | 3 |  |
| `paper_order` | 7 | 16 | 2026-09-08T14:21:32 → 2026-09-17T09:17:30 |
| `paper_position` | 2 | 5 |  |
| `pipeline_log` | 928 | 9 | 2026-07-25 → 2026-09-20 |
| `portfolio_holdings` | 86 | 18 | 2026-07-25 → 2026-09-18 |
| `predictions` | 2,506 | 21 | 2026-09-05 14:23:40 → 2026-09-20 02:46:37 |
| `prices_daily` | 231,544 | 14 | 2025-01-27 → 2026-09-18 |
| `signal_log` | 62 | 23 | 2026-07-30 → 2026-09-18 |
| `signal_outcome` | 159 | 12 |  |
| `sqlite_sequence` | 16 | 2 |  |
| `strategy_event` | 3 | 8 | 2026-09-08T14:21:33 → 2026-09-08T14:21:35 |
| `strategy_position` | 1 | 28 | 2026-09-08T14:21:33 → 2026-09-08T14:21:33 |
| `technical_indicators` | 6,158 | 41 | 2026-07-25 → 2026-09-18 |
| `weight_config` | 105 | 8 |  |

## API routes

- `/` — `dashboard/server.py:1241`
- `/api/scores` — `dashboard/server.py:1244`
- `/api/mh` — `dashboard/server.py:1246`
- `/api/tod` — `dashboard/server.py:1248`
- `/api/news` — `dashboard/server.py:1250`
- `/api/portfolio` — `dashboard/server.py:1252`
- `/api/refresh` — `dashboard/server.py:1254`
- `/api/orders` — `dashboard/server.py:1269`
- `/api/orders` — `dashboard/server.py:1277`
- `/api/orders/pending` — `dashboard/server.py:1281`
- `/api/orders/{rule_id}/confirm` — `dashboard/server.py:1285`
- `/api/orders/{rule_id}/reject` — `dashboard/server.py:1302`
- `/api/orders/{rule_id}` — `dashboard/server.py:1309`
- `/api/orders/broker-status` — `dashboard/server.py:1316`
- `/api/live-quotes` — `dashboard/server.py:1343`

## Scheduled jobs

- `schedule.every().day.at("07:00").do(run_premarket)` — `pipeline/scheduler.py:793`
- `schedule.every().day.at("08:30").do(run_morning_digest)` — `pipeline/scheduler.py:800`
- `schedule.every().day.at(f"{hh:02d}:{mm:02d}").do(run_intraday_15min)` — `pipeline/scheduler.py:807`
- `schedule.every().day.at(f"{hh:02d}:{mm:02d}").do(run_intraday_30min)` — `pipeline/scheduler.py:812`
- `schedule.every().day.at("12:00").do(run_midday_news)` — `pipeline/scheduler.py:815`
- `schedule.every().day.at("12:30").do(run_midday_zpi_scan)` — `pipeline/scheduler.py:816`
- `schedule.every().day.at("14:45").do(run_preclose_scan)` — `pipeline/scheduler.py:817`
- `schedule.every().day.at(POSTMARKET_RUN_TIME).do(run_postmarket)` — `pipeline/scheduler.py:822`
- `schedule.every().day.at(POSTMARKET_CATCHUP_TIME).do(run_postmarket_if_missing)` — `pipeline/scheduler.py:823`
- `schedule.every().day.at("23:00").do(run_overnight)` — `pipeline/scheduler.py:826`
- `schedule.every().saturday.at("08:00").do(run_weekly)` — `pipeline/scheduler.py:829`

## Scoring engine

Functions: `load_weights`, `minmax`, `weighted_score`, `get_tech`, `get_prices`, `get_fund`, `get_inst`, `get_ns`, `get_fii`, `get_idx`, `get_global`, `get_bulk`, `get_news_confidence`, `compute_liquidity`, `compute_relative_strength`, `compute_breakout`, `compute_sector_ranks`, `sector_score`, `compute_ins`, `compute_vpi`, `compute_mri`, `compute_rri`, `compute_cri`, `compute_zpi`, `compute_mh`, `compute_msi`, `compute_acs`, `determine_signal`, `run_scoring_pipeline`

Constants:

- `BUY_STRICT` = `{"atip": 75, "vpi": 70, "zpi": 65, "acs": 60, "mh": 50}`
- `BUY_LOOSE` = `{"atip": 65, "vpi": 60, "zpi": 55, "mh": 40}`
- `SELL_CRI_DANGER` = `75      # above this, exit/avoid regardless of other scores`
- `SELL_ATIP_FLOOR` = `30      # below this, treat as a sell candidate`
- `HOLD_ATIP_FLOOR` = `50      # below this (and not a sell), sit out as WAIT`

## technical_indicators columns written

`symbol`, `date`, `{'`, `'.join(fields`


## Tests

| File | Tests | Parametrized blocks |
|---|---|---|
| `tests/test_aggressive_backtest.py` | 13 | 0 |
| `tests/test_aggressive_decisions.py` | 33 | 3 |
| `tests/test_aggressive_lifecycle.py` | 28 | 0 |
| `tests/test_fundamentals_screener.py` | 2 | 0 |
| `tests/test_index_feed.py` | 20 | 3 |
| `tests/test_paper_broker.py` | 22 | 2 |
| `tests/test_regressions.py` | 20 | 2 |
| `tests/test_signal_pipeline.py` | 31 | 3 |
| `tests/test_strategy_live.py` | 22 | 0 |

**Total: 191 test functions.**
