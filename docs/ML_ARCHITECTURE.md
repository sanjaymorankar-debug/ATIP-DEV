# ATIP ML architecture (W5)

```
MARKET DATA  prices_daily · ai_scores · market_health · fii_dii_market
    ↓  (W1 data quality: data/quality.py; W2 validated bars: backtest/data.py PriceHistory)
FEATURE REGISTRY        ml/feature_registry.py  (metadata over the W3 feature catalogue)
    ↓
FEATURE PIPELINE        strategy_engine/features.py via EvalEnv (point in time)
    ↓
DATASET / LABELS        ml/dataset.py + ml/labels.py  (versioned, hashed, chronological)
    ↓
ML MODEL                ml/models.py  (numpy logistic / ridge; optional sklearn/xgboost/lightgbm)
    ↓
MODEL REGISTRY          ml/registry.py + ml/artifacts.py  (versions, lifecycle, immutable artifacts)
    ↓
PREDICTION              ml/predict.py → ml_prediction  (+ explain.py, monitoring.py)
    ↓
AI/ML SCORE             ml/strategy_features.py → W3 features ml_score, ml_prediction,
                        ml_confidence, ml_prob_up
    ↓
STRATEGY ENGINE (W3) → StrategyDecision → PositionIntent
    ↓
RISK ENGINE (W4) → RiskDecision → ORDER MANAGER → PAPER EXECUTION
```

**Risk integration (W4).** The risk engine's `ml_model_active` check applies to any intent whose decision used an `ml_*` feature. The prediction behind it must be from `ml.default_model` and ACTIVE, and that model version must still be ACTIVE when risk is evaluated; otherwise the intent is BLOCKED. The model, version, score and confidence are stored in the risk decision (ML provenance). `execution/` may read `ml/`; `ml/` never imports `execution/`.

**Separation from execution.**
- `ml/` imports nothing from `execution/`, `orders/` or the Dhan client. This was checked in the W5 build.
- A model's output reaches trading only as a feature that a strategy definition reads. That strategy's decisions then pass the W4 risk engine like any other.
- The regime can be ML-assisted only in `hybrid` mode, and hybrid only ever makes the regime **more defensive**.

## 1. Features

- **One implementation.**
  - Every ML feature is a W3 strategy feature. W5 added `bb_pctb_N`, `bb_width_N`, `nifty_ret_N`, `breadth_pct`, `adv_decline`, `fii_net_cr` and `dii_net_cr`.
  - W3-completion had already added `macd*`, `adx_N` and `vwap_N`.
  - Models, strategies and backtests therefore compute identical values.
- **Metadata** (`ml_feature`): feature_id (`name@vN`), name, description, category, data_type, calculation_method, version, dependencies, availability, lookback, created_at.
- **Feature sets** (`ml_feature_set`):
  - Each is (name, version, features, per-feature versions) with a sha256 content hash.
  - Reusing an existing name@version for different content is refused.
  - Built-in sets:
    - `atip_core@1`: 32 technical, market and ATIP-score features.
    - `atip_technical@1`: the same without the ATIP scores, usable over the full price history.
- **Categoricals:** regime, vol_regime, market_trend and score_signal are one-hot encoded with fixed categories.
- **`ml_*` features are never model inputs.** `describe()` refuses them, which prevents feedback loops.
- **ML-only context features** (`ml/context_features.py`, completion pass), computed per date and merged by the same `enrich()` for datasets and predictions:
  - **Index returns:** `banknifty_ret_5/20`, `midcap_ret_20`, `smallcap_ret_20`.
  - **Global inputs:** `gm_*` from `global_markets`, the latest row known before 15:30 IST (10:00 UTC, because `created_at` is UTC).
  - **Sector:** `sector_ret_20`, `stock_vs_sector_20`, `sector_breadth`, cross-sectional over the same date's rows by NSE industry.
  - **Feature set:** `atip_extended@1` (54 features).
- **Availability:**
  - ATIP scores start 2026-07-27.
  - FII/DII flows start 2026-07-28.
  - Market Health starts 2025-01-27.
  - A dataset reports per-feature coverage.

## 2. Leakage prevention

| Risk | Control |
|---|---|
| Future prices in features | Features come from W2 `PointInTimeView` (raises `LookAheadError` for bars after t) and `ScoresHistory` (raises for later score dates) |
| Labels as inputs | Labels are computed in `labels.py` from the raw bar list, separately; they are never in X |
| Labels beyond the dataset end | Rows whose label date (t+h) is after `end` are dropped |
| Train/validation overlap | `time_split` is chronological: the last dates are validation, train rows whose label date reaches validation are purged, plus an embargo (default = horizon). There is no random split |
| Imputation leakage | Medians, means and stds are fitted on TRAIN rows only and stored in the artifact |
| In-sample predictions reaching strategies | `MLPredictionHistory` serves a prediction only for dates after the version's dataset end, and only from versions ACTIVE when they predicted |
| Regime feedback | Datasets and predictions use the deterministic Market Health regime as input, never the ML regime |

Known residual risks:
- The tracked universe is today's constituents (survivorship bias, as in W2).
- ATIP score history is only today's formulas applied to recent dates.

## 3. Datasets & labels

- **DatasetSpec:** name, version, feature_set, label, start, end, universe (`tracked_current`, a symbol list, or `market`), frequency `daily`, sampling (`every_n_sessions`, `max_symbols`), source.
  - `dataset_id` = name@version.
  - `spec_hash` = sha256 of the spec.
  - Built matrices are saved as `.npz` snapshots with a sha256.
- **Labels:**

| Kind | Task | Definition |
|---|---|---|
| direction | classification | UP / DOWN / NEUTRAL: forward h-session return vs ±threshold_pct |
| binary_return | classification | 1 if the forward return > threshold_pct |
| forward_return | regression | Forward return % |
| volatility_regime | classification | Future annualised volatility vs vol_low / vol_high |
| market_regime | classification | Market Health regime h sessions later (market rows) |
| signal_outcome | classification | WIN / LOSS / TIMEOUT: +target_pct vs −stop_pct within h sessions; a bar touching both is a LOSS |
| return_rank | regression (ranking) | Percentile 0–100 of the forward return among the same date's rows |

Named, versioned label definitions are stored in `ml_label` (6 built-ins). A dataset also embeds its own spec.

## 4. Models

**Interface:** `fit`, `predict`, `predict_proba`, `interval`, `contributions`, `importances`, `metadata`, `to_state` / `load_state`.

| Family | Status |
|---|---|
| logistic_regression | numpy, deterministic |
| linear_regression (ridge) | numpy; interval ±1.96 × residual sd |
| random_forest, gradient_boosting | Adapter; needs scikit-learn (not installed) |
| xgboost, lightgbm | Adapter; needs the package (not installed) |
| neural_network | Placeholder |

No dependency was added to `requirements.txt`; the adapters raise `ModelDependencyError` until the package is installed.

## 5. Training → registry

The pipeline runs in this order:
1. Build the dataset and snapshot it.
2. Select features: drop columns that are too often missing or constant on TRAIN rows.
3. Validate: minimum rows, at least 2 classes, finite targets.
4. Time split.
5. Fit on TRAIN rows only.
6. Record descriptive metrics.
7. Write the artifact (immutable, hashed).
8. Register the version as TRAINED.

Every run is an `ml_training_run` row, including failures and their tracebacks.

**Reproducibility key:**
- artifact hash
- feature-set content hash
- dataset spec hash and snapshot hash
- training-config hash

## 6. Prediction

- **Input:** the ACTIVE version (or a named version, whose predictions are stored as shadow rows with its status) and the features at the close of as_of.
- **Output per symbol:**
  - class or value;
  - probabilities, and confidence (the highest probability);
  - prob_up;
  - `ml_score` 0–100;
  - interval (ridge);
  - the explanation;
  - the input values.
- **Not invented:** confidence is NULL for regression, and `ml_score` is NULL for non-directional labels.
- **Caching:** artifacts are cached by (path, hash). Prediction never retrains.
- **Scheduling:** the post-market `ml_predictions` job runs **before** `strategy_decisions`, but only when `ml.enabled` is true and a version is ACTIVE.

## 7. Explainability & monitoring

- **Explanations:** linear contributions (coefficient × standardised value), the top 5 features, and reason codes `ML_POS_<f>` / `ML_NEG_<f>`. Explanation version `linear-contrib-1`.
- **Other families:** global importances (`ml_model_explanation`).
- **Monitoring** (`ml_model_monitoring`), per batch:
  - prediction distribution;
  - PSI drift per feature against the training quantiles (PSI > 0.25 is flagged);
  - missing-value rates;
  - the version's status that day.
- **Realised metrics:** `evaluate_matured()` stores metrics for matured predictions in `ml_model_metrics` (kind `realised`).

## 8. Configuration (`config.json` "ml"; all off by default)

| Key | Default |
|---|---|
| enabled | false |
| default_model | null |
| feature_set | atip_core |
| prediction_horizon | 5 |
| model_path | atip_data/ml |
| regime_source | deterministic |
| regime_model | null |
| training | {validation_fraction 0.2, embargo_sessions null, min_rows 200} |

## 9. Performance design

- Feature code is shared and cached per (symbol, date) context.
- Artifacts are cached.
- Training (API: background thread) is separate from inference.
- Predictions are batch-per-date and stored, so strategies read them instead of recomputing.

Dataset building is pure Python over EvalEnv and slow at full scale; use `sampling` (`every_n_sessions`, `max_symbols`).
