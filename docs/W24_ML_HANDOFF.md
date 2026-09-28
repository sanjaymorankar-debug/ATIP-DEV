# W24: Machine learning handoff

**Status:** IMPLEMENTED BUT NOT VERIFIED. Build check, a synthetic correctness check, and a real-data smoke run on a scratch copy. ChatGPT testing is pending.
**Branch:** `w24-ml` (on top of W23). Not merged, not deployed. **No model was activated.**

## Delivered

| ID | Feature | Where |
|---|---|---|
| ML-01 | Train and validate first models | `ml/trees.py`: native numpy `native_gbm` (softmax multi-class / squared-loss gradient-boosted histogram trees, second-order leaves, row / column subsampling, seeded) and `native_random_forest`. ATIP has no scikit-learn, so the W5 tree adapters could never run. `ml/presets.py bootstrap` creates the models, validates them walk-forward on one shared point-in-time dataset, and trains versions (TRAINED, never ACTIVE) |
| ML-11 | Ensembles | Model type `ensemble`: members fitted on the same rows, blended by 1 / held-out loss (or equal weights), then refitted on all rows. Contributions and importances are weight-averaged |
| ML-10 | Explainability for trees | Path (Saabas / tree-interpreter) contributions: bias + contributions equals the raw logit exactly (checked). Gain importances, plus out-of-sample permutation importance in every validation window |
| ML-08 | Model validation | `ml/validation.py walk_forward`: expanding windows, training rows purged by label date + embargo, one model per window. Per window and pooled: accuracy and log loss vs majority / class-frequency baselines, rank IC, top-minus-bottom quintile, calibration table; regression RMSE vs mean baseline, IC and hit rate. Verdict EDGE / WEAK / NO_EDGE. Stored in `ml_validation_report` |
| ML-07 | Feature selection | `ml/selection.py`: keep columns with positive mean out-of-sample permutation importance and stability ≥ 60% of windows; save the result as a new, content-hashed feature-set version |
| ML-05 | Regime model | `presets.regime`: market-level rows with a `market_regime` label, validated and trained (`regime_gbm`). The W5 config still decides whether it is ever used; the deterministic provider stays the default |
| ML-03 | Unsupervised | `ml/unsupervised.py`: k-means++ clusters of stocks on factor scores, with profiles (`ml_cluster*`); return PCA with explained variance and loadings |
| ML-06 | Anomaly detection | Robust (median / MAD) z-scores of return, volume, range and gap over 60 sessions, plus a multivariate score. Corporate-action days are marked as explained (`ml_anomaly`). Post-market job, off unless `ml.anomalies_enabled` |
| ML-09 / MON-05 | Drift alerts, model and factor decay | `ml/decay.py`: PSI-shift share per live model (WARNING / ALERT), realised-vs-validation decay, factor IC decay (recent vs earlier, sign flip / fade). Stored in `ml_health_check`, alerted via the alert log. Post-market job (on when ml or quant is enabled), W8 monitor rule `ml_model_health` |

## Interfaces

- **CLI:** `python -m ml bootstrap | regime-model | validate | select | clusters | pca | anomalies | health`.
- **API:** `dashboard/ml_w24_routes.py`:
  - `/api/ml/validation[/{id}[/selection]]`
  - `/api/ml/bootstrap`
  - `/api/ml/clusters`
  - `/api/ml/pca`
  - `/api/ml/anomalies`
  - `/api/ml/health`
  - Authz falls under the existing `/api/ml` rules (`ml:read` / `ml:write`).
- **Tables:** `ml_validation_report`, `ml_cluster_run`, `ml_cluster`, `ml_anomaly`, `ml_health_check`.
- **Config:** `ml.anomalies_enabled` (default false).

## Evidence

**Synthetic check (known non-linear signal, 3,000 rows):**

| Model | Accuracy | Log loss |
|---|---|---|
| Logistic | 0.75 | 0.63 |
| native_gbm | 0.955 | 0.157 |
| Random forest | 0.82 | 0.76 |
| Ensemble | 0.958 | 0.29 |

- All four round-trip exactly through save / load.
- Importances rank the three true features first.
- Regression correlation: GBM 0.95 vs ridge 0.59.

**Real data (scratch copy): first models.**
- Setup: 60 stocks, 2025-06 → 2026-08, every 3rd session, label direction over 5 sessions (±1%); 6,000 rows, 38 columns.
- Validation: 5 windows, 3,540 out-of-sample rows.
- Result: **NO_EDGE for logistic, native_gbm and ensemble.**
  - Pooled log loss 1.09–1.11 vs the class-frequency baseline 1.06.
  - Mean window IC around −0.06, t around −0.5.
  - The calibration table shows over-confidence: predicted 0.55, hit rate 0.29.
- Bootstrap time: 141 s, including the dataset build.

**Other real-data results:**
- **Feature selection:** stable columns include `atr_pct_14` and `macd_hist` (stability 1.0), then `mh_score` and `rsi_14`.
- **Regime model:** NO_EDGE (accuracy 0.59, log loss above baseline).
- **Clusters:** 5 clusters over 500 stocks, e.g. a mean-reversion-heavy cluster of 155 and a trend cluster of 106.
- **PCA:** the first component explains 22.6% of return variance.
- **Anomalies (2026-09-25):** 22 of 500 flagged (WHIRLPOOL range, +7.7%).
- **Health:** OK. Factor decay is INSUFFICIENT_HISTORY until backfills run.

**Conclusion:** with ATIP's current features and history, the out-of-the-box classifiers do not beat a naive baseline on 5-day direction. This is the result the validation layer exists to show before anything is activated.

## Not done (by decision)

- **ML-02 deep learning, ML-04 reinforcement learning:** the tracker says "only if validated benefit" / "low priority". There is no validated benefit yet (see above).
- **SE-05 AI-driven strategies:** they activate only once a model is ACTIVE, which is an owner decision after evidence.
