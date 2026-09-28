# W23: Research and backtesting depth handoff

**Status:** IMPLEMENTED BUT NOT VERIFIED. Build check plus a scratch-copy smoke run on a 12-stock universe. ChatGPT testing is pending.
**Branch:** `w23-research-backtest` (on top of W22). Not merged, not deployed.

## Delivered

| ID | Feature | Where |
|---|---|---|
| BT-04 | Parameter optimisation | `backtest/optimize.py`. Grid, random (seeded) or adaptive (random then local refinement; stated as not Bayesian) search over a space `{param: {values} \| {min,max,step} \| {min,max}}`. Every trial is an ordinary run (kind `opt_trial`) under a parent (kind `optimization`). A grid larger than `max_trials` is refused (no silent truncation). Trials under `min_trades` score None. **Refused on the test window.** Overfitting diagnostics: Deflated Sharpe Ratio (Bailey & López de Prado), best vs median, share of positive trials, `overfit_warning` |
| QR-11 | Parameter sensitivity | `backtest/sensitivity.py`. One-at-a-time ±steps around the chosen set: a curve per parameter, a stability ratio, and a `knife_edge` flag (a ±1 step loses more than half the metric or flips its sign), plus an optional 2-D heat map. `robust_share` |
| BT-14 | Robustness | `backtest/robustness.py`. Baseline, all percentage costs doubled, slippage tripled, seeded universe halves, first / second half of the period, per-regime returns (market_health), trade-shuffle Monte Carlo P(loss). Six PASS / FAIL checks → score, verdict ROBUST / FRAGILE / NOT_ROBUST |
| BT-12 | VaR / CVaR in backtests | `backtest/metrics.py tail_risk()`, now in every run's metrics: `var_95`, `cvar_95`, `var_99`, `cvar_99` (historical), `var_95_param`, `skew`, `excess_kurtosis`, `worst_day`, `best_day` |
| QR-07 | Momentum research | Strategy `momentum`: breakout to a new N-session high with a momentum and 200-SMA filter, ranked by momentum |
| QR-08 | Mean-reversion research | Strategy `mean_reversion_rsi`: RSI(2) washout above the 200-SMA; exit on RSI recovery, stop or time |
| — | End-to-end study | `backtest/study.py`. Split 60/20/20, then: optimise on research; pick among the top-k on validation; sensitivity; robustness; **one** test run. The verdict (SUPPORTED / REJECTED / INCONCLUSIVE) is recorded as a W22 research study with every run linked, then frozen. Presets for both strategies |

## Interfaces

- **CLI:**
  - `python -m backtest optimize | sensitivity | robustness --strategy ... --start --end ...`
  - `python -m backtest study momentum|mean_reversion_rsi --start --end`
- **API** (token, `research:run`; they validate, then run in a thread):
  - `POST /api/backtests/optimize`
  - `POST /api/backtests/sensitivity`
  - `POST /api/backtests/robustness`
  - `POST /api/backtests/study`
- **Results:** in `/api/backtests` (parent kinds `optimization` / `sensitivity` / `robustness`); studies in `/api/research/studies`.
- **Schema:** no change. It reuses `backtest_run` parent / child and the W22 study tables.

## Smoke run (scratch copy, 12 large caps)

- **Momentum (2025-06 → 2026-09):** 15 trades, −6.5%, Sharpe −1.76. VaR95 0.29% / CVaR95 0.57% daily, skew −1.58.
- **Mean reversion:** 120 trades, −10.5%.
- **Optimisation:** a 2×2 grid ran 4 trials in 0.8 s. DSR 0.008 gave `overfit_warning`.
- **Refusals:** a test-window optimisation was refused, and so was an 820-combination grid.
- **Sensitivity:** 6 trials, no knife edges.
- **Robustness:** NOT_ROBUST (0/6), MC P(loss) 1.0. By regime: BULL +3.5%, NEUTRAL −3.4%, BEAR −6.1%.
- **Study (mean_reversion_rsi):** REJECTED (test −2.1%, Sharpe −3.15, overfit warning), study CONCLUDED with 7 links.
- **DSR check:** on a synthetic strong series it gives 0.998.
- **Takeaway:** on this sample neither simple strategy has an edge after costs. The tooling reports that plainly, which is the point.

## Notes for QA

- A full-universe study (~500 symbols) is much slower than the 12-stock smoke run. Run studies off-hours.
- The adaptive search is transparent local refinement, not Bayesian optimisation.
