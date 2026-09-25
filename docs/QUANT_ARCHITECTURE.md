# ATIP quant architecture (W6)

```
MARKET DATA   prices_daily (bars, delivery %) · fundamental_data · corporate_actions · bulk_deals
              · live_quotes · NIFTY / sector index series
    ↓
FEATURE ENGINEERING   strategy_engine/features.py (W3; W5 additions) — reused, never re-implemented
    ↓
QUANT FACTORS         quant/factors.py      40 factors, versioned registry (quant_factor)
    ↓
NORMALIZATION         quant/normalize.py    winsorize · z-score · percentile · rank · min-max
                                            · sector-relative · market-relative · neutralize
    ↓
CROSS-SECTION         quant/engine.py       per-date scores, ranks, sector ranks → quant_factor_score
                      quant/composite.py    weighted composites (quant_composite)
    ↓
FACTOR RESEARCH       quant/research.py     IC, IC decay, quantile returns, factor correlation
                      quant/experiments.py  hypothesis records that run W2 backtests
    ↓
ADVANCED STRATEGIES   W3 kinds: multi_factor / quant_rank on qf_* / qc_* features,
                      pairs (quant/statarb.py), portfolio (quant/portfolio.py)
    ↓
BACKTESTING (W2)      the same StrategyDefinition runs through backtest/ (adapter supplies quant history)
    ↓
STRATEGY ENGINE (W3) → StrategyDecision → PositionIntent
    ↓
RISK ENGINE (W4) → RiskDecision → ORDER MANAGER → PAPER EXECUTION
```

## Boundaries

- **No imports of execution.** `quant/` imports nothing from `execution/` or `orders/`. This was checked in the build.
- **Quant output is data.** It is a score stored per (date, symbol) or a statistic. It becomes a trade only when a W3 strategy definition reads it and the W4 risk engine approves the resulting intent.
- **No second strategy engine or backtester.**
  - Pairs and portfolio strategies are W3 kinds.
  - Their decisions are ordinary `StrategyDecision`s.
  - Their intents are ordinary `PositionIntent`s.
  - Their backtests use W2.
- **ML reuse.**
  - `qf_*`, `qc_*` and `ev_*` are W3 features, so W5 feature sets can include them. The ML dataset builder and the prediction engine supply the same `QuantHistory`.
  - An ML score can be a factor in a W6 multi-factor strategy (`ml_score`), exactly as in W5.
- **Shorting.**
  - The PAPER cash book cannot sell what it does not hold.
  - Short legs (pairs, long/short portfolios) are recorded as SELL decisions with reason code `SHORT_LEG`, and no intent is made for them.
  - Strategies that need the short side to stay neutral hold back their longs (`SHORT_LEG_UNAVAILABLE`, `NEUTRALITY_UNAVAILABLE`) unless explicitly allowed to go single-leg or long-only.
  - The construction maths already support shorts, for when a shortable instrument (futures / SLB) is integrated.

## Point in time

| Input | Rule |
|---|---|
| Bars | W2 `PriceHistory` / `PointInTimeView`; bars dated ≤ as_of |
| Fundamentals | Known at `report_date + 45 days` (publication lag), else `created_at` |
| Corporate actions | Known at `ex_date` (the announcement date is not stored — conservative) |
| Bulk deals | Known on the deal date |
| Global markets (W5 context) | Row created before 15:30 IST (10:00 UTC) on t |
| Scores served to strategies | Only rows stored for exactly as_of |
| Cross-sectional factors / ranks | Only the same date's cross-section |

## Scheduling

Post-market job `quant_factors` runs after `ml_predictions` and before `strategy_decisions`. It is SKIPPED unless `quant.enabled` is true (default false). When it runs, it:
- syncs the registry;
- computes the factor set and composites for the session;
- syncs events;
- computes intraday microstructure features.

## Modules

| Module | Role |
|---|---|
| quant/factors.py | Registry + calculators + fundamentals point-in-time loader |
| quant/normalize.py | Normalization methods; `apply(spec)` |
| quant/engine.py | Cross-section compute, store, rankings, sector rankings, scheduled job |
| quant/composite.py | Composite definitions (versioned) and computation |
| quant/strategy_features.py | `QuantHistory` → W3 features `qf_*`, `qc_*`, `ev_*` |
| quant/statarb.py | Hedge ratio, spreads, z-score, correlation, ADF, Engle-Granger, half-life |
| quant/pairs.py | Pair registry (`quant_pair`), spread snapshots (`quant_spread`), pair screening |
| quant/portfolio.py | Weighting, constraints, neutrality, exposures, persisted portfolios |
| quant/volatility.py | Realised / historical / Parkinson / Garman-Klass / downside / regime / IV-RV |
| quant/derivatives.py | Black-Scholes, greeks, IV, IV rank, basis, roll (data pending) |
| quant/events.py | `market_event` sync + typed interface + `EventHistory` |
| quant/microstructure.py | Intraday features from `live_quotes`; tick/depth interface (data pending) |
| quant/research.py | IC, IC decay, quantile returns, factor correlation |
| quant/experiments.py | Experiment records linked to W2 backtest runs |
| strategy_engine/kinds.py | New `PairsEvaluator`, `PortfolioEvaluator` |
| dashboard/quant_routes.py, quant_page.py | `/api/quant/*`, `/quant` |
