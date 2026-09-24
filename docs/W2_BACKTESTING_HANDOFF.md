# ATIP W2 Research & Backtesting: development handoff for independent validation

Developed 2026-09-24 in the worktree `D:\Projects\ATIP-dev`, branch `w2-backtesting`, on top of `w1-foundation` (`3c13f63`).
**Nothing in W2 has been run, tested, committed, pushed or deployed.** The only check was syntax compilation (`py_compile`).
The live ATIP (`D:\Projects\ATIP`, branch `master`) is untouched. It had been reading W1 files from disk mid-run, so it was switched back to `master` to match the running process.

W2 scope: BT-16, BT-08, BT-09, BT-11, BT-05, BT-06, BT-07, BT-03, BT-02, BT-13, BT-15.
The W2 brief as received was cut off at "STEP 1 — INSPECT … portfo". The rest of the process (handoff format, stop condition) was taken to match W1.

## Gap analysis (before W2)

| ID | Feature | Current before W2 | Required | Class |
|---|---|---|---|---|
| BT-16 | Run records / dated equity | Results printed only; no dates on trades | Run table, config snapshot + hash, code version, data fingerprint, dated equity | MISSING |
| BT-08 | Portfolio-level backtest | Each trade on its own notional | Cash, positions, sizing, exposure, portfolio returns | MISSING |
| BT-09 | Drawdown | Max DD over trade order (wrong ordering) | Series, episodes, duration, recovery on dated equity | PARTIAL |
| BT-11 | Metrics | Win rate, profit factor, expectancy | Plus return, CAGR, volatility, Sharpe, Sortino, Calmar | PARTIAL |
| BT-05 | Costs | Blended 0.25% constant, per engine | Central itemised model | PARTIAL |
| BT-06 | Slippage | Paper broker only (5 bps) | Fixed / % models, shared default | PARTIAL |
| BT-07 | Liquidity | Turnover floor in one engine | Volume participation + turnover | PARTIAL |
| BT-03 | In-sample / out-of-sample | none | Research / validation / test with guards | MISSING |
| BT-02 | Walk-forward | none | Rolling windows, candidate selection, stitched OOS | MISSING |
| BT-13 | Monte Carlo | none | Trade shuffle, return bootstrap, seeded | MISSING |
| BT-15 | Bias controls | Next-open entry; pre-entry ATR | PIT view, look-ahead guard, data/timestamp validation, survivorship flag | PARTIAL |

## 1. Changed functionality

| ID | Feature | What was implemented |
|---|---|---|
| BT-16 | Run records | `backtest_run`: run_id, kind, parent, strategy id/version, period label, window, data source, timeframe, capital, params, **full config snapshot + SHA-256 hash**, code version (git commit, `+dirty`), data fingerprint (bars/symbols/dates + SHA-256 of OHLCV), bias report, metrics, status CREATED→RUNNING→COMPLETED/FAILED, timestamps. Trades, dated equity curve and drawdown episodes in child tables. Runs execute only from the stored snapshot. |
| BT-08 | Portfolio backtest | `backtest/engine.py`: shared cash, multiple positions, `max_positions`, sizing via W1 `orders.risk.size_position` on current equity, fills at next open, protective stop/target, max-hold exits, realised/unrealised P&L, equity, exposure %, daily returns, close-out at the end of the window. |
| BT-09 | Drawdown | `metrics.drawdown_series`, `max_drawdown`, `drawdown_episodes` (peak, trough, recovery date, depth, duration, recovery sessions). Stored per run. |
| BT-11 | Metrics | `metrics.py`: total return, CAGR, volatility, Sharpe, Sortino, Calmar, max DD, win rate, avg win/loss, profit factor, expectancy (₹ and %), avg exposure. Assumptions in the module docstring. |
| BT-05 | Costs | `backtest/costs.py`: itemised brokerage (pct/min/max), STT buy/sell, exchange, SEBI fee, stamp duty, GST, DP charge. Presets `nse_delivery` (default), `nse_intraday`, `flat`, `zero`. Configured centrally (`backtest.cost_model`, `cost_overrides`). |
| BT-06 | Slippage | `backtest/slippage.py`: `none`, `fixed` (₹/share), `pct` (bps, default 5 = paper broker). Always adverse. |
| BT-07 | Liquidity | `backtest/liquidity.py`: max % of the bar's volume (cap or reject), minimum average turnover over prior bars (never the fill bar), zero-volume bars can't fill. |
| BT-03 | IS / OOS | `backtest/periods.py`: ordered, non-overlapping research/validation/test windows. A run evaluates one labelled window. Data is loaded only to the window end. Test needs `allow_test`. A re-test with different params is flagged. |
| BT-02 | Walk-forward | `backtest/walkforward.py`: windows in sessions (train/validation/test/step, step ≥ test). Candidates are compared on validation, the chosen one runs once on test, and OOS is stitched from the test windows. Child runs sit under a parent. Selection only, no optimiser. |
| BT-13 | Monte Carlo | `backtest/montecarlo.py`: trade shuffle (path/drawdown risk) and daily-return bootstrap (block size). Seeded percentiles, P(loss), a disclaimer on every result. Stored in `backtest_montecarlo`. |
| BT-15 | Bias controls | `backtest/data.py`: strategies only get `PointInTimeView` (raises `LookAheadError` for later dates, and the run fails). Signals at close, fills at next open. Bars validated (invalid OHLC, non-trading day, future date, non-increasing dates dropped and listed). Universe declares survivorship bias. ai_scores caveat. Price-return basis stated. All in each run's bias report. |
| Interface | Strategy API | `backtest/strategy.py` (`Strategy`, `Signal`, `StrategyContext`). Reference strategies `dip`, `atip_signal`, `buy_and_hold`. The engine holds no strategy logic. |
| Service/CLI/API/UI | | `backtest/service.py`, `python -m backtest …`, `/api/backtests*`, page `/backtests` (form, runs, metrics, equity and drawdown charts, trades, Monte Carlo, comparison). |

## 2. Files

| File | Change |
|---|---|
| backtest/__init__.py, config.py, costs.py, slippage.py, liquidity.py, data.py, strategy.py, strategies.py, engine.py, metrics.py, periods.py, walkforward.py, montecarlo.py, store.py, service.py, __main__.py | **new** |
| dashboard/backtest_page.py | **new**, the /backtests page |
| dashboard/server.py | backtest API routes, /backtests route, "Backtests" link in the top bar |
| db/schema.py | W2_TABLES (additive), created by get_connection |
| config_template.json | `backtest` section |
| docs/W2_BACKTESTING_HANDOFF.md | **new**, this file |

Existing engines `scores/backtest.py`, `strategy/backtest_aggressive.py` and `strategy/backtest_history.py` are unchanged.

## 3. Database (additive)

`backtest_run`, `backtest_trade` (PK run_id+seq), `backtest_equity` (PK run_id+date), `backtest_drawdown` (PK run_id+seq), `backtest_montecarlo`. Columns are listed in `db/schema.py` under W2_TABLES. Execution assumptions live in `backtest_run.config_json`, not a separate table.

## 4. API (new; W1 routes unchanged)

| Method | Route | Notes |
|---|---|---|
| GET | /api/backtests?limit&strategy_id | Run list with metrics |
| GET | /api/backtests/strategies | Registered strategies (id, version, default params) + config defaults |
| POST | /api/backtests | Body = request (below). 400 on invalid; else `{run_id, status: RUNNING}`, executes in background. Needs X-ATIP-Token |
| POST | /api/backtests/walkforward | `{request:{…start,end}, train, validation, test, step, candidates?, select_by?}`. Token |
| GET | /api/backtests/{id} | Run incl. config snapshot, hashes, fingerprint, bias report, metrics; `children` for walk_forward |
| GET | /api/backtests/{id}/trades, /equity, /drawdowns, /metrics, /montecarlo | 404 for unknown id |
| POST | /api/backtests/{id}/montecarlo | `{method, n_sims, seed, block_size}`. Token |
| GET | /backtests | HTML page |

Request keys: `strategy_id` (required), `params`, `start`/`end` **or** `periods` + `period_label`, `allow_test`, `initial_capital`, `universe` ("tracked_current" or a list), `universe_survivorship_bias`, `cost_model`, `cost_overrides`, `slippage`, `liquidity`, `sizing`, `risk_free_rate_pct`, `close_out_at_end`, `notes`. Unknown keys → 400.

## 5. Configuration

`atip_data/config.json` → `"backtest"` (see config_template.json): initial_capital, cost_model, cost_overrides, slippage, liquidity, sizing (risk_per_trade_pct, max_position_pct, max_positions, default_stop_pct), risk_free_rate_pct. All optional. A run stores the resolved values, so changing config later doesn't change an existing run.

## 6. Expected behaviour (worked values)

| Feature | Input | Expected output |
|---|---|---|
| BT-05 | `cost_model("nse_delivery").charges("BUY", 100000)` | stt 100, exchange 2.97, sebi 0.10, stamp 15, gst 0.5526, brokerage 0 → total **118.6226** |
| BT-05 | same, SELL 100000 | stt 100, exchange 2.97, sebi 0.10, stamp 0, gst 0.5526 → **103.6226** |
| BT-05 | `nse_intraday` BUY 100000 | brokerage 20 (0.03% = 30, capped), stt 0, exchange 2.97, sebi 0.10, stamp 3, gst 4.1526 → **30.2226** |
| BT-05 | `cost_model("nse_delivery", {"bogus": 1})` | ValueError |
| BT-06 | pct 5 bps, BUY at 100 / SELL at 100 | 100.05 / 99.95 |
| BT-06 | fixed 0.10, BUY at 100 | 100.10; kind "x" → ValueError |
| BT-07 | cap rule 10%, qty 5000, bar volume 20000 | (2000, "capped …"); with on_breach "reject" → (0, reason); volume 0 → (0, …) |
| BT-11 | equity [100, 110, 99, 120] | total_return 0.20; max_drawdown −0.10; returns [0.1, −0.1, 0.212121…] |
| BT-09 | same curve, dates d0..d3 | one episode: peak d1 (110), trough d2 (99), recovery d3, depth −0.10, duration 2, recovery 1 |
| BT-11 | returns all equal | volatility 0 → sharpe/sortino None (not ∞) |
| BT-11 | net P&L [100, −50, 30, −20] | win_rate 0.5, avg_win 65, avg_loss −35, profit_factor 130/70 = 1.857, expectancy 15 |
| BT-08 | `size_position(100, 95, capital=1_000_000)` (defaults 1% / 10%) | by risk 2000, by cap 1000 → **1000**, capped_by max_position_pct |
| BT-03 | periods with validation starting on/before research end | ValueError (overlap) |
| BT-03 | period_label "test" without allow_test | ValueError / 400 |
| BT-02 | 300 sessions, train 120, validation 40, test 40, step 40 | 3 windows; step 30 < test 40 → ValueError |
| BT-13 | trade_shuffle, any seed | final_return identical across all sims (only the path varies); same seed → identical output |
| BT-15 | a strategy calling `ctx.data.bar(sym, as_of + 1 day)` | run FAILED, error contains "look-ahead blocked" |
| BT-15 | tracked universe | bias_report.universe.survivorship_bias = true and a warning |
| BT-16 | same request run twice on unchanged code and data | identical config_hash, data sha256, metrics, trades |

## 7. Test scenarios for ChatGPT

| Test ID | Feature | Scenario | Expected |
|---|---|---|---|
| W2-T01 | BT-05 | Cost presets per §6, incl. brokerage cap/min, sell-only DP charge | Exact totals |
| W2-T02 | BT-06 | Slippage kinds and sides; negative value | Adverse prices; ValueError |
| W2-T03 | BT-07 | Participation cap/reject, min turnover with too few prior bars | Per docstring |
| W2-T04 | BT-11/09 | Metrics on hand-built curves (flat, monotonic up, one drawdown, unrecovered drawdown) | Values per module conventions; None where undefined |
| W2-T05 | BT-15 | PriceHistory.load on seeded bars with invalid OHLC, weekend date, future date | Dropped and listed in `issues` |
| W2-T06 | BT-15 | PointInTimeView.history / bar beyond as_of | Only ≤ as_of; LookAheadError |
| W2-T07 | BT-08 | Seed 3 symbols, run `buy_and_hold` over a short window | Positions ≤ max_positions; cash + positions = equity each day; costs charged on both legs |
| W2-T08 | BT-08 | Stop/target on a bar hitting both | Exit STOP at stop price (pessimistic); gap below stop → STOP_GAP at open |
| W2-T09 | BT-08 | Signal on day d | Fill on d+1 open, never d |
| W2-T10 | BT-08 | Insufficient cash for a sized buy | Quantity cut to cash; zero → event "buy rejected" |
| W2-T11 | BT-16 | Create + execute; inspect backtest_run | Snapshot, hash, code_version, fingerprint, metrics, status COMPLETED |
| W2-T12 | BT-16 | Execute a COMPLETED run again | ValueError "already COMPLETED" |
| W2-T13 | BT-16 | Change strategy version after creating a run, then execute | FAILED "would not reproduce" |
| W2-T14 | BT-03 | Research/validation/test runs; data after window end never used | Trades and equity inside the window only |
| W2-T15 | BT-03 | Two test runs, different params, same window | Second run's bias report has test_window_warnings |
| W2-T16 | BT-02 | Walk-forward with 2 candidates | Validation child runs per candidate, one test run per window, chosen = best select_by, stitched OOS metrics on parent |
| W2-T17 | BT-13 | Both methods, same seed twice | Identical results; percentiles ordered p5 ≤ … ≤ p95; disclaimer present |
| W2-T18 | API | POST /api/backtests without token | 401/403; with token → run_id; bad request → 400 |
| W2-T19 | API | GET trades/equity/drawdowns/metrics/montecarlo for an unknown id | 404 |
| W2-T20 | UI | /backtests: start a run, list refreshes, open detail, charts, Monte Carlo button, compare two runs | Renders from API data |
| W2-T21 | Regression | Full `python -m pytest` | W1 and earlier tests still pass |
| W2-T22 | Real data | `python -m backtest run --strategy dip --start 2025-06-01 --end 2026-09-23` on a copy of atip.db | Completes; survivorship warning; plausible trade count |

## 8. Known limitations

- **Survivorship bias is unavoidable on current data.** ATIP only has deep history for today's constituents. Every run over the tracked universe is flagged; there's no point-in-time constituent history to remove it.
- **Price returns only.** Dividends aren't included, so long-hold results are understated. Splits and bonuses are adjusted.
- **Daily bars only.** Intraday stop/target order within a bar is unknowable, so the pessimistic reading is used.
- **`atip_signal` isn't a clean historical signal.** ai_scores history is today's formulas re-applied to history (flagged). `signal_log` is the contemporaneous record, and the rows are too few for research.
- **Walk-forward selects, it doesn't fit.** It chooses among candidates you supply; there's no optimiser (by design for W2). The stitched OOS curve starts at the first test session's close, not at initial capital.
- **Cost rates need checking.** They're the published NSE/SEBI/stamp/GST figures as understood in 2026-09. DP charge defaults to 0. Verify against a contract note.
- **Runs execute in-process.** API runs use a background thread with no queue or cancel; a long run occupies the dashboard process.
- **Some items are out of W2 scope.** VaR/CVaR (BT-12), robustness tests (BT-14), an event-driven engine (BT-17) and an experiment tracker (QR-12) aren't built.
- **No W2 unit tests were written.** Test scenarios are above for ChatGPT.
- **The old engines stay separate.** `scores/backtest.py` and `strategy/backtest_*` aren't migrated onto the framework.
