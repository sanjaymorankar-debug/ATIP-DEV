# ATIP W5 AI/ML: development handoff

- **Independent testing:** PENDING — ChatGPT.
- **Testing performed by Claude:** none. No model was trained, evaluated or activated; only build/integration checks were run (section 8).
- **Live trading:** DISABLED. **Deployment:** NOT PERFORMED.
- **Where it was built:** 2026-09-25 in `D:\Projects\ATIP-dev`, branch `w5-ai-ml`, on top of W1–W4 (`d3b8715`).
- **Merge:** fast-forwarded into `master` in `D:\Projects\ATIP`.

Architecture: `docs/ML_ARCHITECTURE.md`. Lifecycle: `docs/ML_MODEL_LIFECYCLE.md`.

## 0. W5 completion pass (2026-09-25, second brief)

**Consolidation.** The inspection found all ten local branches (w1 → w5 and the backups) with 0 commits outside `master`, and both working trees clean apart from the owner's untracked spreadsheets. `D:\Projects\ATIP` (`master`) was already the single authoritative codebase. The completion pass was built in the worktree and fast-forwarded into `master`.

**Pending W5 items found against the brief, now implemented:**

| Item | Implementation |
|---|---|
| Sector performance | `ml/context_features.py`: `sector_ret_20`, `stock_vs_sector_20`, `sector_breadth`. Computed cross-sectionally per date from the NSE industry map (the cached Nifty 500 list, no network) |
| Index returns | `banknifty_ret_5/20`, `midcap_ret_20`, `smallcap_ret_20` (NIFTYBANK, NIFTYMIDCAP150, NIFTYSMLCAP250 series) |
| Global market inputs | `gm_sp500_chg`, `gm_nasdaq_chg`, `gm_nikkei_chg`, `gm_brent_chg`, `gm_gold_chg`, `gm_usdinr_chg`, `gm_us10y_chg`, `gm_global_score` from `global_markets`. The latest row dated ≤ t and created before 15:30 IST is used; `created_at` is UTC, so the cutoff is 10:00 UTC |
| Feature set `atip_extended@1` | 54 features: core + flows + context + global + sector. Existing sets are unchanged (immutable) |
| Label `signal_outcome` | WIN / LOSS / TIMEOUT by +target / −stop within h sessions. A bar touching both counts as a LOSS (pessimistic) |
| Label `return_rank` | Cross-sectional percentile of the forward return per date (ranking task) |
| `ml_label` table | Reusable, versioned label definitions, plus 6 built-ins (`direction_5d`, `fwd_return_10d`, `return_rank_20d`, `vol_regime_20d`, `market_regime_5d`, `signal_outcome_10d`). `GET/POST /api/ml/labels` |
| **W4 risk integration** | Risk check `ml_model_active` (execution/risk_engine.py). If a decision used any `ml_*` feature, the ACTIVE prediction behind it must exist, come from `ml.default_model`, and that model version must **still be ACTIVE** when risk is evaluated. Otherwise the intent is BLOCKED. Model, version, score and confidence are recorded as ML provenance in the risk decision |

Datasets and predictions compute the context features through the same `context_features.enrich()`. Cross-sectional features use only the same date's rows.


## 1. Implementation status

| Capability | Status | Where |
|---|---|---|
| Feature Registry | IMPLEMENTED | ml/feature_registry.py, ml_feature, ml_feature_set |
| Dataset Framework | IMPLEMENTED | ml/dataset.py, ml_dataset, .npz snapshots |
| Label Framework | IMPLEMENTED | ml/labels.py (7 kinds incl. signal_outcome, return_rank); ml_label definitions |
| ML Feature Pipeline | IMPLEMENTED | W3 feature catalogue via EvalEnv + new features (Bollinger, index returns, breadth, FII/DII) |
| Model Abstraction | IMPLEMENTED | ml/models.py. The numpy families are available; the sklearn / xgboost / lightgbm adapters need packages that are not installed; the neural network is a placeholder |
| Model Registry | IMPLEMENTED | ml/registry.py, ml_model, ml_model_version, ml_model_event |
| Model Versioning | IMPLEMENTED | Artifact, feature-set, dataset-spec, snapshot and training-config hashes; immutable artifacts |
| Training Framework | IMPLEMENTED | ml/training.py, ml_training_run. Not exercised |
| Prediction Engine | IMPLEMENTED | ml/predict.py, ml_prediction. Not exercised |
| AI/ML Score Integration | IMPLEMENTED | `ml_score` / `ml_prediction` / `ml_confidence` / `ml_prob_up` strategy features; `atip_ml_blend` multi-factor (ML as one factor) |
| Regime ML Framework | PARTIAL | Deterministic / ML / hybrid providers built; no regime model trained |
| Explainability Framework | PARTIAL | Linear contributions + reason codes; tree models get global importances only (no SHAP) |
| Model Monitoring Framework | PARTIAL | Persistence, PSI drift, prediction distribution, realised metrics; no alerting or dashboards |
| Strategy Integration | IMPLEMENTED | W3 EvalEnv / FeatureContext / engine / backtest adapter read stored predictions point in time; 2 DRAFT library strategies |
| Risk Integration | IMPLEMENTED | W4 check `ml_model_active`: ML-driven intents are BLOCKED when the model version is no longer ACTIVE or the provenance is missing. ML never bypasses the risk engine |
| Context features (sector / index / global) | IMPLEMENTED | ml/context_features.py; feature set atip_extended@1 |
| AI assistant (read-only) | PARTIAL | ml/assistant.py, GET /api/ml/explain/{symbol}. It assembles facts and templated answers, with no LLM (the Anthropic key returns 401, KD-001) |

## 2. W1–W4 changes made for W5 (additive)

- `strategy_engine/features.py`:
  - new parametric features: `bb_pctb_N`, `bb_width_N`, `nifty_ret_N`;
  - new market features: `breadth_pct`, `adv_decline`, `fii_net_cr`, `dii_net_cr`;
  - ML features `ml_*` (category `ml`);
  - `FeatureContext.ml`.
- `strategy_engine/regime.py`: market rows now include breadth and flows. `get_provider()` returns the configured provider and defaults to deterministic, so behaviour is unchanged.
- `strategy_engine/kinds.py`: `EvalEnv(ml=…)`.
- `strategy_engine/engine.py`: `build_env` uses `get_provider` and the ML prediction history.
- `strategy_engine/adapter.py`: backtests can read stored ML predictions, out of sample only.
- `db/schema.py`: `W5_TABLES`.
- `pipeline/scheduler.py`: `ml_predictions` job before `strategy_decisions`; SKIPPED unless enabled.
- `dashboard/server.py`: registers the ML routes; "ML" link.
- `config_template.json`: section 12 "ml".

- `execution/risk_engine.py` (completion pass): one added gate, `ml_model_active`. It applies only to intents from decisions that used `ml_*` features.

No W1–W4 table was altered. The OMS, paper broker, aggressive exit and order rules are untouched.

## 3. Files

- **New:**
  - `ml/`: `__init__`, config, feature_registry, context_features (completion pass), labels, dataset, models, artifacts, registry, training, predict, explain, monitoring, regime, strategy_features, assistant, `__main__`.
  - `dashboard/ml_routes.py`, `dashboard/ml_page.py`.
  - `strategy_engine/library/50_ml_direction.json`, `51_atip_ml_blend.json`.
  - `docs/W5_AI_ML_HANDOFF.md`, `docs/ML_ARCHITECTURE.md`, `docs/ML_MODEL_LIFECYCLE.md`.
- **Modified:** as in section 2, plus `docs/KNOWN_ISSUES.md` and `docs/ATIP_MASTER_TRACKER.csv`.

## 4. Database

**New tables:**
- `ml_label` (completion pass), `ml_feature`, `ml_feature_set`, `ml_dataset`
- `ml_model`, `ml_model_version`, `ml_model_event`
- `ml_training_run`, `ml_prediction`
- `ml_model_metrics`, `ml_model_explanation`, `ml_model_monitoring`

**Modified tables:** none. Additive `CREATE TABLE IF NOT EXISTS`, applied by `get_connection()`.

The existing `predictions` table (the signal engine's forecasts) is unrelated and unchanged.

## 5. APIs (new; none modified)

| Method | Path |
|---|---|
| GET | /api/ml/status · /api/ml/features · /api/ml/feature-sets · /api/ml/labels (now with stored definitions) |
| POST (token) | /api/ml/labels (completion pass) |
| POST (token) | /api/ml/feature-sets |
| GET / POST (token) | /api/ml/datasets (POST `build: true` builds in the background) · GET /api/ml/datasets/{id} |
| GET / POST (token) | /api/ml/models · GET /api/ml/models/{id} |
| POST (token) | /api/ml/models/{id}/train · /lifecycle · /activate · /pause |
| GET | /api/ml/training-runs · /api/ml/predictions · /api/ml/predictions/{id} · /api/ml/monitoring · /api/ml/regime |
| POST (token) | /api/ml/predict |
| GET | /api/ml/explain/{symbol}?q= (read-only) |
| GET | /ml (page) |

## 6. Configuration

`config.json` "ml" defaults:

| Key | Default |
|---|---|
| enabled | false |
| default_model | null |
| feature_set | atip_core |
| prediction_horizon | 5 |
| model_path | atip_data/ml |
| regime_source | deterministic |
| regime_model | null |

With these defaults:
- nothing is predicted on a schedule;
- strategies see no ML values;
- the regime is unchanged.

**Security:** no credentials are used by `ml/`. Artifacts are written under `atip_data/` (git-ignored runtime data).

## 7. Deferred work

- Train, validate and activate first models. This is owner work, followed by ChatGPT validation.
- Install and evaluate scikit-learn / XGBoost / LightGBM, if wanted. Nothing was installed.
- SHAP-style explanations for tree models.
- Drift and decay alerting and dashboards (W6/W8).
- Asynchronous inference.
- Walk-forward model validation reports.
- An LLM assistant on top of `assistant.explain_symbol`, which needs a working API key (KD-001).
- Unsupervised learning, anomaly detection, deep learning and RL (ML-03/06/02/04).

## 8. Build/integration checks performed (not testing)

| Check | Result |
|---|---|
| `py_compile` of `ml/`, the ML dashboard files, `strategy_engine/`, `db/schema.py`, `pipeline/scheduler.py`, `dashboard/server.py` | 0 errors |
| Import of all 16 `ml` modules + 2 dashboard modules | 0 errors |
| pyflakes on `ml/`, `strategy_engine/`, the ML dashboard files | Only the pre-existing unused import in `strategy_engine/definition.py` |
| Built-in feature sets constructed (every feature known) | atip_core@1, atip_technical@1 |
| Library definitions 50/51 validated | Valid; inputs include `ml` |
| Migrations on a **copy** of the live DB | 11 ML tables created; existing rows kept (ai_scores 9,032, predictions 9,030, order_rules 10, …) |
| Library sync on the copy | 2 added (ml_direction, atip_ml_blend), 9 unchanged; 45 ML features synced |
| App startup + route registration | 23 W5 routes, no duplicate method+path, 104 routes in total |
| Static safety check | No `execution` / `orders` / Dhan imports and no order calls in `ml/` or `ml_routes.py` |
| Completion pass: compile + pyflakes of `ml/`, `execution/`, `ml_routes.py` | 0 errors, 0 warnings |
| Completion pass: migrations on a fresh DB copy | `ml_label` created; existing rows kept; 63 ML features, 3 built-in feature sets (atip_extended@1 = 54 features), 6 labels |
| Completion pass: route registration | 24 W5 routes, 105 in total, no duplicate method+path |

Not performed: model training, prediction runs, accuracy or profitability evaluation, API/UI testing.

## 9. Deferred testing items (for ChatGPT)

| ID | Scenario | Expected |
|---|---|---|
| W5-T01 | FeatureSet with an `ml_*` feature | ValueError |
| W5-T02 | Same feature-set name@version, different features | Refused |
| W5-T03 | Label for the last h sessions | None (dropped) |
| W5-T04 | Dataset: every label_date ≤ end | Yes |
| W5-T05 | time_split: max train label_date < validation start; no shuffling | Yes |
| W5-T06 | Point-in-time: feature row for t uses no bar after t | LookAheadError is never raised; values match a manual calculation |
| W5-T07 | Train logistic on a small dataset | Version TRAINED, artifact + hashes stored, run COMPLETED |
| W5-T08 | Training with too few rows / a single class | Run FAILED with the reason; version FAILED |
| W5-T09 | TRAINED → ACTIVE directly | Refused |
| W5-T10 | Activate v2 while v1 is ACTIVE | v1 PAUSED, v2 ACTIVE |
| W5-T11 | Tamper with an artifact file | Load refused (hash mismatch) |
| W5-T12 | Save over an existing artifact | FileExistsError |
| W5-T13 | Predict: probabilities sum to 1; confidence = max; regression confidence NULL | Yes |
| W5-T14 | Prediction from a VALIDATION version | Stored with version_status VALIDATION; not served to strategies |
| W5-T15 | Strategy reads ml_score for a date ≤ the dataset end | None (in-sample blocked) |
| W5-T16 | ml_direction in PAPER with an ACTIVE model | Decisions → intents → W4 risk; no direct orders |
| W5-T17 | regime_source hybrid, ML says BULL, deterministic HIGH_RISK | HIGH_RISK |
| W5-T18 | Monitoring row after predictions | PSI / missing / distribution stored |
| W5-T19 | /api/ml/explain/{symbol}?q=why | Facts only; no write |
| W5-T20 | ml.enabled false | ml_predictions job SKIPPED |
| W5-T21 | Regression | W1–W4 flows unchanged; W3 strategies without ml_* unaffected |
| W5-T22 | Context: global row created after 10:00 UTC on t | Not used for t |
| W5-T23 | sector_ret_20 for an industry with < 3 members that day | None |
| W5-T24 | signal_outcome: a bar touching both barriers | LOSS |
| W5-T25 | return_rank labels on one date | Span 0..100 |
| W5-T26 | Risk: ML-driven intent after its model version is PAUSED | BLOCKED, ml_model_active FAIL |
| W5-T27 | Risk: intent from a strategy not using ml_* | ml_model_active SKIP; unchanged W4 behaviour |
