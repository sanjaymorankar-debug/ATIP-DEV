# W25: Portfolio risk handoff

**Status:** IMPLEMENTED BUT NOT VERIFIED. Build check, a real-data smoke run on a scratch copy of the production database, an API run through FastAPI TestClient, and a browser render of the new /trading panels with mocked API responses. ChatGPT testing is pending.
**Branch:** `w25-portfolio-risk`, from master `0e648af` (ATIP-W24-RC1). Not merged, not deployed.
**Defaults:** nothing changes behaviour until you switch it on. The four new risk limits are `null` (off), and the snapshot job is off. The emergency exit never sends a LIVE order.

## Delivered

| ID | Feature | Where |
|---|---|---|
| PF-03 | Exposure, LIVE book included | `portfolio/risk.py book_snapshot`: PAPER or LIVE book (LIVE = the latest successful holdings sync; no broker call). Positions, weights, % of equity, sector exposure |
| PF-04 | Concentration | `concentration`: max and top-5 weight, HHI, effective N (1/HHI), sector HHI and effective sectors |
| PF-09 / RK-11 | Correlation, analytics | `correlation`: pairwise matrix; plain and value-weighted average pairwise correlation; most-correlated and most-diversifying pairs; diversification ratio |
| RK-11 | Correlation limit | W4 limit `max_avg_correlation` (0..1): value-weighted mean correlation of the new symbol with the held book. Above the limit, no BUY |
| RK-12 | Portfolio VaR / ES | `value_at_risk`: see "VaR / ES methods" below. W4 limit `max_portfolio_var_pct`: post-trade 1-day 95% historical VaR, % of equity |
| RK-09 | Volatility limit | W4 limit `max_symbol_volatility_pct`: annualised 60-session volatility |
| RK-10 | Liquidity (ADV) limit | W4 limit `max_adv_participation_pct` caps the order quantity at a share of 20-session average daily volume. The W1 order path (`config.json risk_limits.max_adv_participation_pct`) checks BUY and SELL |
| PF-07 | Risk attribution | `risk_attribution`: per position and per sector share of variance (sums to 1); risk-to-weight ratio; component VaR (sums to parametric VaR); beta to NIFTY50; systematic vs idiosyncratic share |
| PF-08 | Performance attribution | `performance_attribution`: see "Performance attribution" below |
| PF-11 | CAGR / XIRR | `returns_analytics`: CAGR from stored daily equity (pnl_daily, after ≥ 3 months); PAPER book and per-position XIRR from paper fills + today's value. LIVE has no trade ledger in ATIP; ledger XIRR is in /wealth (W15.5) |
| PF-10 | Portfolio optimiser | `portfolio/optimize.py`: see "Optimiser" below |
| PF-06 | Add / reduce toward target | `rebalance_plan` and a strategy-kind option; see "Rebalancing" below |
| RK-16 | Emergency exit | `portfolio/emergency.py`: see "Emergency exit" below |
| DB-17 | Risk dashboard | /trading "Portfolio risk (W25)": book selector; KPI tiles (VaR, ES, volatility, beta, effective N, correlation); sector exposure; VaR / ES table (every method); concentration; risk contribution; correlation heatmap; performance attribution; emergency exit button |
| (job) | Post-market snapshot | `portfolio_risk` job after daily P&L: headline + full analysis per non-empty book in `portfolio_risk_snapshot`; alert when VaR > `portfolio_risk.var95_alert_pct`. Off unless `portfolio_risk.snapshot_enabled` |

### VaR / ES methods (RK-12)

- Three methods: historical, parametric (normal) and Monte Carlo (20,000 seeded multivariate-normal draws).
- Confidence levels 95% and 99%; horizons of 1 day and 10 days (historical: overlapping 10-session sums; parametric: scaled by √10).
- Each figure is given as %, in rupees and as % of equity.

### Performance attribution (PF-08)

- Buy-and-hold of today's holdings over N sessions. Start weights are backed out from today's weights, so contributions sum to the book return.
- The return is split into a beta part and an alpha part vs NIFTY50.
- Brinson-Fachler allocation / selection by sector, against the equal-weighted ATIP universe.

### Optimiser (PF-10)

- **Objectives:** min_variance, mean_variance, max_sharpe (frontier search) and risk_parity (exact equal risk contribution).
- **Constraints:** long only, with max_weight, min_weight and sector_cap.
- **Solver:** FISTA (accelerated projected gradient) with an exact Euclidean projection onto box, simplex and sector caps (Dykstra).
- **Inputs:** covariance shrunk toward constant correlation (0.3). Expected returns can be historical (shrunk 50% to the cross-sectional mean), equal, or your own views.
- **Output:** efficient frontier points; infeasible constraints are reported, not forced.
- Every run is stored in `portfolio_optimization`.

### Rebalancing (PF-06)

- **Plan:** `rebalance_plan` gives NEW / ADD / REDUCE / EXIT / HOLD per name toward target weights: whole shares, a drift band, a minimum trade value, a cash buffer, estimated costs and turnover. It is a plan only and is stored in `portfolio_rebalance_plan`.
- **Strategy option:** the `portfolio` strategy kind has a new opt-in `reweight_band_pct`. When it is set, it emits ADD / REDUCE with an exact share quantity (instead of HOLD) when a held name drifts from its target share. The intent carries that quantity (`strategy_engine/engine.py`), and the W4 risk engine then checks it as usual.

### Emergency exit (RK-16)

- **Plan:** `plan` lists the positions and open orders, and gives a confirm code. The code is a hash of the book and its positions, and is valid for 10 minutes.
- **Execute:** turns the kill switch ON, then cancels open W4 orders.
- **PAPER book:** sells every position through the paper broker (live quote, or the last close).
- **LIVE book:** no order is sent; the result is the sell list to execute at the broker.
- Every run is recorded (`risk_emergency_exit`) and raises a risk alert. The dashboard asks you to type the code; the API needs `risk:approve`.

## Interfaces

- **CLI:**
  - `python -m portfolio risk | optimize | rebalance | snapshot`
  - `python -m portfolio.emergency plan | flatten | history`
- **API** (`dashboard/portfolio_risk_routes.py`, under `/api/risk`; authz `risk:read` / `risk:configure`; the emergency exit needs `risk:approve`):
  - `GET /api/risk/portfolio`
  - `GET /api/risk/portfolio/snapshots`
  - `POST /api/risk/portfolio/what-if`
  - `POST /api/risk/optimize`, `GET /api/risk/optimize/{id}`
  - `POST /api/risk/frontier`
  - `POST /api/risk/rebalance-plan`
  - `GET|POST /api/risk/emergency-exit`, `GET /api/risk/emergency-exit/history`
- **Tables:** `portfolio_risk_snapshot`, `portfolio_optimization`, `portfolio_rebalance_plan`, `risk_emergency_exit` (additive, `W25_TABLES`).
- **Config:**
  - W4 `w4_risk_limits` / `PUT /api/risk/limits`: `max_symbol_volatility_pct`, `max_adv_participation_pct`, `max_avg_correlation`, `max_portfolio_var_pct` (all default null).
  - W1 `risk_limits.max_adv_participation_pct`.
  - `portfolio_risk.{snapshot_enabled, var95_alert_pct}`.
- **Changed files:**
  - `execution/config.py`, `execution/risk_engine.py`, `orders/risk.py`
  - `strategy_engine/kinds.py`, `strategy_engine/engine.py`
  - `db/schema.py`, `pipeline/scheduler.py`, `enterprise/authz.py`
  - `dashboard/execution_page.py`, `dashboard/server.py`
  - Every change is additive. Behaviour with the new settings unset is unchanged.

## Evidence (scratch copy of production, 2026-09-28)

**LIVE book (8 holdings, ₹78,493; ANMOL excluded for 6% price history, so the risk numbers cover 98.3% of value):**

| Measure | Result |
|---|---|
| 1-day 95% VaR | historical 1.68% (₹1,299), parametric 1.67%, Monte Carlo 1.68% |
| 1-day 95% ES | 2.14% |
| 99% VaR | 2.34% |
| 10-day historical 95% VaR | 4.2% |
| Volatility | 16.0% annualised |
| Beta | 0.73 |
| Effective N | 4.7 |
| Mean correlation (value-weighted) | 0.30 |
| Diversification ratio | 1.52 |

- **Risk attribution:** NTPC 30% of the weight carries 27% of the risk; BPCL 20% of the weight carries 24%.
  - Component VaR sums to ₹1,278 vs parametric ₹1,288; the gap is the mean term.
  - Systematic share of variance: 37%.
- **Performance attribution, 2026-07-03 → 2026-09-25:**
  - Book −5.63% vs NIFTY50 −4.28%; beta 0.47; alpha −3.63%.
  - Brinson vs a 500-stock equal-weighted universe: allocation −1.64%, selection −1.77%.
  - Position contributions sum exactly to the book return.
- **Optimiser (15 names, max weight 15%, sector cap 30%):** every run respects the caps; the frontier is monotone.
  - min variance: 11.4% volatility vs 13.6% for equal weight.
  - risk parity: effective N 14.2, max risk share 6.7%.
  - Check: none of 3,000 random feasible portfolios had lower variance than min_variance.
  - Timings: min_variance 0.03 s; max_sharpe 1.3 s for 15 names and 30 s for 60 names (runs off the event loop).
- **Pre-trade limits (RELIANCE against the LIVE book):**
  - Volatility 19.1%: FAIL at 10, PASS at 40.
  - Correlation 0.19: FAIL at 0.1, PASS at 0.6.
  - Post-trade VaR 0.37%: FAIL at 0.2, PASS at 1.0.
  - Unknown symbol: rejected (fail closed).
  - W1 ADV: 2% of ADV refused at a 1% cap; an unmeasurable ADV is refused.
  - Bad limit values are rejected (correlation 1.5, ADV 150%, negative).
- **Emergency exit:**
  - PAPER: bought ITC and INFY, planned, a wrong code was refused, and the flatten sold both (DONE). The kill switch was ON afterwards.
  - LIVE: MANUAL_ACTION_REQUIRED with 8 sells listed and no order sent.
- **PF-06 reweight:** A overweight by 40.9 points gives REDUCE 45 shares; the matching ADD for B; `_quantity` passes the 45 through.
- **API:** every route answered as expected, including 400 for a bad book / objective / infeasible caps and 404 for an unknown optimisation. The authz rules resolve to `risk:approve` / `risk:read` / `risk:configure`.
- **Page:** the JavaScript passes a syntax check, and every panel rendered in the browser with real API output.

## Notes and limits

- Risk numbers are only as good as the history. Names with less than 80% of the window are excluded and shown. Weights are not silently rescaled: `coverage.value_share` says what the numbers describe.
- The optimiser's expected returns are estimates. The last 250 sessions were a falling market for most of the test names, so the example `mean_variance` / `max_sharpe` returns are negative. min_variance and risk_parity do not use expected returns.
- Performance attribution replays today's holdings, not trades inside the window. The LIVE book has no trade ledger in ATIP.
- The LIVE equity uses the last recorded LIVE cash in pnl_daily, never a broker call. In the smoke data that cash equals the paper balance, because `broker_env` is PAPER.

## Not done

- **Intraday VaR:** needs the Dhan intraday bars, and dhan_15min_bars currently fails.
- **Stress scenarios by factor:** the W6 quant factor exposures exist, but there is no scenario engine yet.
- **Short / derivatives risk:** there is no instrument data.
