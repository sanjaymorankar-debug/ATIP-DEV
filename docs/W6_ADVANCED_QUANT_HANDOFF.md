# ATIP W6 Advanced Quant: development handoff

- **Independent functional testing:** PENDING — ChatGPT.
- **Testing performed by Claude:** none. Only build/integration checks (section 9) and the deployment startup check.
- **Live trading:** DISABLED.

**Recovery point:** MAIN_BEFORE = PROD_BEFORE = `123b340` (tag `w5-final-prod-before-w6`). Before promotion there is also a DB backup `atip_data/atip.db.bak-before-w6-<timestamp>` and branch `backup/pre-w6-master`.

## 0. Consolidation and production findings

- **Consolidation:** every local branch (w1–w5, backups) had 0 commits outside `master`. The worktree `D:\Projects\ATIP-dev` (w5-ai-ml) equalled `master`, and there was no uncommitted work apart from the owner's untracked spreadsheets, which were left untouched. W6 was built on branch `w6-advanced-quant` in the worktree and fast-forwarded into `master`.
- **Production** is `D:\Projects\ATIP` on `master`. The repository's own files say so: `docs/ATIP_PHASE_STATUS.md` ("deployed to D:\Projects\ATIP"), `start_atip.bat` (`python main.py` from its own folder), `atip_autostart.vbs` / `ATIP_TaskScheduler.xml` (launch that .bat).
  - There is no separate production directory, release branch or deploy script.
  - Promotion = fast-forwarding `master` in that folder; additive migrations applied by `get_connection()` at start-up; restarting `python main.py` (scheduler + dashboard in one process, port 8000).
  - The database is `atip_data/atip.db`. Config and secrets live in `atip_data/config.json` and `.env` (git-ignored) and were not touched.
  - The actual Windows scheduled task was not inspected, per the owner's earlier instruction.

## 1. Implementation status

| Area | Coding status | Notes |
|---|---|---|
| Factor Framework | COMPLETED | 40 factors across 7 categories: 16 compute from price data today; 18 need fundamental_data (empty); 6 need data ATIP does not collect (shares, bid/ask) |
| Factor Registry | COMPLETED | quant_factor (versioned, hashed), quant_factor_set |
| Normalization | COMPLETED | z-score, percentile, rank, winsorize, min-max, sector-/market-relative, neutralize |
| Ranking | COMPLETED | Stock, sector-internal, sector, factor, composite |
| Composite Factors | COMPLETED | Weights, direction, include/exclude, version, coverage rule; 3 built-ins |
| Multi-Factor Strategies | COMPLETED | W3 multi_factor / quant_rank on qf_* / qc_*; library vqm_multi_factor, mom_lowvol_liq |
| Statistical Arbitrage | COMPLETED | Hedge ratio, log/ratio spread, z-score, correlation, ADF, half-life |
| Pairs Trading | IN PROGRESS | Registry + W3 `pairs` kind built; short legs not executable (no shortable instrument) |
| Cointegration | COMPLETED | Engle-Granger (ADF on the OLS residual, MacKinnon critical values) |
| Cross-Sectional Strategies | COMPLETED | quant_rank / portfolio on stored cross-sectional scores |
| Market-Neutral Strategies | IN PROGRESS | Dollar / beta / sector neutral construction built; not executable without shorts |
| Portfolio Construction | COMPLETED | equal / score / inverse_vol / risk / factor, max_weight, sector_cap, gross, exposures |
| Factor Neutralization | COMPLETED | normalize.neutralize (OLS residual) + sector-relative normalization |
| Volatility Framework | IN PROGRESS | Realised / historical / Parkinson / GK / downside / regime done; implied needs options data |
| Derivatives Framework | BLOCKED (data) | Schema + BS / greeks / IV / basis / roll maths done; no futures or options data source |
| Options Analytics | BLOCKED (data) | Calculators done (`/api/quant/options/price`); options_analytics empty |
| Event Framework | IN PROGRESS | Corporate actions + bulk deals live; earnings / dividends / macro / index changes pending a source |
| Microstructure Framework | IN PROGRESS | Intraday features from live_quotes; tick / depth / spread pending data |
| Quant Experiments | COMPLETED | quant_experiment linked to W2 backtest runs |
| W5 AI/ML Integration | COMPLETED | qf_* / qc_* / ev_* usable as ML features (dataset + predict supply QuantHistory); ml_score usable as a factor |
| W3 Strategy Integration | COMPLETED | New kinds pairs / portfolio; qf_* / qc_* / ev_* features; backtest adapter supplies quant history |
| W4 Risk Integration | COMPLETED | Every W6 long leg is a PositionIntent → W4 risk engine; short legs produce no intent |

## 2. Files

- **New:**
  - `quant/` (`__init__`, config, factors, normalize, engine, composite, strategy_features, statarb, pairs, portfolio, volatility, derivatives, events, microstructure, research, experiments, `__main__`);
  - `dashboard/quant_routes.py`, `dashboard/quant_page.py`;
  - `strategy_engine/library/60_vqm_multi_factor.json`, `61_mom_lowvol_liq.json`, `62_pairs_bank.json`, `63_market_neutral_portfolio.json`;
  - docs `W6_ADVANCED_QUANT_HANDOFF.md`, `QUANT_ARCHITECTURE.md`, `FACTOR_FRAMEWORK.md`, `QUANT_RESEARCH.md`.
- **Modified:**
  - `strategy_engine/features.py`: qf_ / qc_ / ev_ features, `FeatureContext.quant`;
  - `strategy_engine/kinds.py`: `EvalEnv(quant=…)`, PairsEvaluator, PortfolioEvaluator;
  - `strategy_engine/definition.py`: kinds pairs / portfolio + validation;
  - `strategy_engine/engine.py`, `adapter.py`: quant history;
  - `ml/dataset.py`, `ml/predict.py`, `ml/feature_registry.py`: quant features as ML inputs;
  - `db/schema.py`: W6_TABLES;
  - `pipeline/scheduler.py`: `quant_factors` job;
  - `dashboard/server.py`: routes + "Quant" link;
  - `config_template.json`: section 13;
  - `docs/KNOWN_ISSUES.md`, `docs/ATIP_MASTER_TRACKER.csv`.
- **Untouched:** W4 risk engine, OMS, paper broker, aggressive exit and order rules.

## 3. Database

- **New tables:**
  - `quant_factor`, `quant_factor_set`, `quant_composite`, `quant_factor_score`, `quant_factor_research`;
  - `quant_experiment`, `quant_pair`, `quant_spread`;
  - `quant_portfolio`, `quant_portfolio_position`, `quant_exposure`;
  - `market_event`, `microstructure_feature`;
  - `derivatives_instrument`, `derivatives_quote`, `options_analytics`.
- **Modified tables:** none.
- **Migrations:** additive (`CREATE TABLE IF NOT EXISTS`), applied by `get_connection()`.
- **Existing tables reused, not duplicated:**

| Instead of a new table | Uses |
|---|---|
| Rankings | `quant_factor_score.rank` / `sector_rank` |
| Composites' scores | The same score table |
| Events | Reference `corporate_actions` / `bulk_deals` |
| Tick data | Existing (empty) `live_ticks` |

## 4. APIs (new; none modified)

The `/api/quant/*` routes, 32 in total including the `/quant` page:

| Area | Routes |
|---|---|
| Status | status |
| Factors & sets | factors, factors/{id}, factor-sets (GET/POST) |
| Composites | composites (GET/POST) |
| Scores & ranks | compute (POST), scores, rankings |
| Strategies & experiments | strategies, experiments (GET/POST), experiments/{id}/run |
| Pairs | pairs (GET/POST), pairs/screen, pairs/{id}/status, pairs/{id}/analyze, spreads |
| Portfolios & research | portfolios (GET/POST), research (GET/POST) |
| Events | events, events/sync |
| Volatility & derivatives | volatility/{symbol}, derivatives, options, options/price |
| Microstructure | microstructure |

## 5. Configuration

`config.json` "quant", defaults off:

| Key | Default |
|---|---|
| enabled | false |
| universe | tracked_current |
| factor_set | atip_factors |
| composites | [vqm, mom_lowvol_liq] |
| winsorize_pct | 1.0 |

## 6. Data dependencies (not fabricated)

| Dependency | Needed for | Status |
|---|---|---|
| fundamental_data | Value / quality / growth factors, vqm composites | Table empty; filled by the weekly fundamentals job |
| Shares outstanding | Market cap, size, turnover, FCF yield, P/S | Not collected |
| Bid / ask, depth, ticks | Spread, imbalance, depth, tick features | Not collected; live_ticks is empty |
| Futures / options | Derivatives, IV, greeks history, IV rank | No source integrated |
| Earnings / dividend / macro / index-change calendars | Event types | No source |
| Shortable instrument | Pairs short leg, market-neutral books | Not in the cash / paper book |

## 7. Deferred work

- Data ingestion for the dependencies above.
- A shortable-instrument path (futures / SLB) through W4.
- Factor-score back-fill for research dates.
- Event features beyond "days since".
- The market-impact model (EX-12) and execution algos (EX-11).
- Performance: factor computation is pure Python per symbol (~all tracked symbols per date).

## 8. Known issues

See `docs/KNOWN_ISSUES.md` (W6-R1..R8 plus earlier items).

## 9. Build/integration checks performed (not testing)

| Check | Result |
|---|---|
| `py_compile` of quant, ml, strategy_engine, execution, dashboard, backtest, db, pipeline | 94 files, 0 errors |
| pyflakes on `quant/` and the quant dashboard files | Clean |
| Static boundary: `quant/` imports of execution / orders | None |
| All 15 library definitions validate | 0 invalid |
| Migrations on a **copy** of the production DB | 16 W6 tables created; existing rows kept (corporate_actions 277, bulk_deals 651, ai_scores 9,032, …) |
| Registry sync on the copy | 40 factors, factor set atip_factors@1, composites vqm@1, mom_lowvol_liq@1, vqmg_lowvol@1 |
| App startup + route registration | 32 W6 routes, 137 in total, no duplicate method+path |
| Production restart | Recorded in the final report (process started / running / no immediate startup error) |

## 10. Deferred testing items (for ChatGPT)

| ID | Scenario |
|---|---|
| W6-T01 | Compute factors for a date: coverage > 0 for price factors; DATA_PENDING factors have no rows |
| W6-T02 | Point in time: a factor for t uses no bar after t; fundamentals only after report_date + 45 days |
| W6-T03 | Normalization: percentile ties, winsorize tails, sector fallback (< 5 members), neutralize residual orthogonal to exposures |
| W6-T04 | Composite coverage rule: vqm produces no rows with empty fundamentals |
| W6-T05 | Rankings: rank 1 = highest score; sector ranks per industry |
| W6-T06 | Factor or composite version bump: a changed definition under the same version is refused |
| W6-T07 | statarb: hedge ratio, z-score, ADF and half-life on synthetic cointegrated / random-walk series |
| W6-T08 | Pairs strategy: z ≥ entry → SELL (SHORT_LEG) + NO_ACTION (SHORT_LEG_UNAVAILABLE); no intents |
| W6-T09 | Pairs held: z inside exit → EXIT; beyond stop → EXIT PAIR_STOP |
| W6-T10 | Portfolio: max_weight / sector_cap respected; dollar / beta / sector neutrality sums |
| W6-T11 | Portfolio strategy with long_short: new longs NO_ACTION (NEUTRALITY_UNAVAILABLE) |
| W6-T12 | W4: W6 intents go through the risk engine; no path from quant to orders |
| W6-T13 | Black-Scholes put-call parity; IV round-trip; greeks signs |
| W6-T14 | Events: sync is idempotent; ev_days_since_split uses only known_at ≤ as_of |
| W6-T15 | Microstructure from live_quotes for a day with snapshots |
| W6-T16 | Experiment run links a W2 backtest run id |
| W6-T17 | ML: a feature set containing qf_* builds a dataset (values from stored scores) |
| W6-T18 | Regression: W1–W5 flows unchanged; quant.enabled false → job SKIPPED |
