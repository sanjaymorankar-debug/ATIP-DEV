# ATIP quant research toolkit (W6)

All of these describe samples. None of them was run to validate a strategy in W6; ATIP's price history starts on 2025-01-27.

## Factor research (`quant/research.py`, stored in `quant_factor_research`)

| Tool | What |
|---|---|
| `factor_ic(key, start, end, horizon)` | Per date: Spearman correlation of the stored score with the forward h-session return (bars after t only; only fully elapsed windows). Returns mean IC, IC stdev, t-stat, hit rate |
| `ic_decay(key, start, end, horizons)` | Mean IC at 1 / 5 / 10 / 20 sessions |
| `quantile_returns(key, start, end, horizon, q=5)` | Mean forward return per score quintile per date, averaged; spread = top − bottom |
| `factor_correlation(keys, as_of)` | Cross-sectional Spearman matrix (redundancy) |

**Prerequisite:** factor scores must be stored for the dates (`quant.enabled`, or `POST /api/quant/compute {as_of}` per date).

## Statistical arbitrage (`quant/statarb.py`, `quant/pairs.py`)

| Tool | Detail |
|---|---|
| Hedge ratio | OLS of ln A on ln B (with intercept), or fixed |
| Spread | log: ln A − β ln B; ratio: A / B |
| Z-score | (spread − mean) / stdev over the lookback |
| Correlation | Of daily log returns |
| Cointegration | Engle-Granger: ADF (constant, 1 lag) on the OLS residual. 5% critical value −3.34 (MacKinnon, 2 variables); 1% −3.90; 10% −3.04 |
| Half-life | −ln 2 / ln(1 + φ), from Δs = a + φ·s(−1); None when not mean-reverting in the sample |

**Pair registry** (`quant_pair`):
- fields: pair_id@version, asset_a, asset_b, hedge_ratio ("ols" or a number), spread_kind, lookback, entry_z, exit_z, stop_z (0 ≤ exit < entry < stop), capital_allocation_pct, status DRAFT/ACTIVE/PAUSED/RETIRED;
- `analyze_pair` stores snapshots in `quant_spread`;
- `screen(symbols)` ranks candidate pairs by ADF t.

**Trading a pair** uses the W3 `pairs` kind (library `pairs_bank`):
- **Entry:** when flat and |z| ≥ entry, short the rich leg and go long the cheap leg.
- **Exit:** when held and |z| ≤ exit (PAIR_EXIT) or |z| ≥ stop (PAIR_STOP).
- **Short leg:** a SELL decision with no intent; the cash book cannot short.
- **Long leg:** NO_ACTION (SHORT_LEG_UNAVAILABLE) unless `allow_single_leg`.

## Portfolio construction & market-neutral (`quant/portfolio.py`)

- **Methods:**
  - equal;
  - score;
  - inverse_vol;
  - risk (equal-risk, diagonal approximation);
  - factor.
- **Constraints:** max_weight, sector_cap, gross. Caps are iterative, with the excess redistributed; an infeasible cap leaves cash.
- **Neutrality:**
  - dollar: long sum = short sum;
  - beta: the short book is scaled to the long book's beta exposure;
  - sector: long = short within every industry present on both sides.
- **Exposures:** long, short, gross, net, beta, sector net, factor exposure (weighted mean score).
- **Persisted** via `POST /api/quant/portfolios` into `quant_portfolio`, `quant_portfolio_position` and `quant_exposure`.
- **Strategy kind:** the W3 `portfolio` kind applies the same construction on every rebalance session (library `market_neutral_mlq`).

## Volatility (`quant/volatility.py`)

- **Estimators:** close-to-close (realised / historical), Parkinson, Garman-Klass, downside semi-deviation, rolling series, short/long change.
- **Regime:** LOW / NORMAL / HIGH against the 20th / 80th percentile of the stock's own history.
- **Implied volatility:** IV and IV−RV need options data (pending).
- **API:** `/api/quant/volatility/{symbol}`.

## Derivatives (`quant/derivatives.py`)

- **DATA PENDING:** no futures / options source is integrated, and the `derivatives_instrument`, `derivatives_quote` and `options_analytics` tables are empty.
- **Math ready:**
  - Black-Scholes (with dividend yield q) and greeks (theta per day, vega and rho per point);
  - implied volatility (bisection within no-arbitrage bounds);
  - IV rank and percentile;
  - futures basis, annualised basis, roll yield, rollover %.
- **Calculator:** `/api/quant/options/price?S=&K=&days=&sigma=|price=`.

## Events (`quant/events.py`)

- **Sources:** `market_event` is built from `corporate_actions` (SPLIT / BONUS / RIGHTS / DEMERGER / CONSOLIDATION / SCHEME) and from `bulk_deals` (BULK_DEAL, with direction).
- **Pending types:** EARNINGS, DIVIDEND, ANNOUNCEMENT, MACRO and INDEX_CHANGE have no source; `add_event()` is the interface for them.
- **Features:** `ev_days_since_<type>`, `ev_bulk_net_5d`.

## Microstructure (`quant/microstructure.py`)

- **Computed** per day from the `live_quotes` intraday snapshots: n_snapshots, intraday_rv, intraday_range_pct, trade_intensity, last_hour_move_pct.
- **Pending:** spread, depth, order imbalance and tick counts (`live_ticks` is empty, and there are no bid/ask quotes).
- **Market-impact input:** `participation(order_qty, adv)`.

## Experiments (`quant/experiments.py`)

- **Record** (`quant_experiment`): name@version, hypothesis, universe, factor_set / composite, strategy_id@version, parameters, start–end, frequency, benchmark, config hash, status.
- **Run:** `run()` submits a **W2** backtest of the strategy version and links the run id. The run's metrics are recorded but not judged.
