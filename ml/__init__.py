"""
ATIP W5 -- the AI/ML subsystem.

    market data (prices_daily, ai_scores, market_health, fii_dii_market)
      -> feature registry        feature_registry.py  (reuses the W3 feature catalogue)
      -> feature set (versioned) feature_registry.FeatureSet, content-hashed
      -> dataset (versioned)     dataset.py   point in time, time-ordered, embargoed
      -> labels                  labels.py    computed ONLY from bars after the row date
      -> model                   models.py    numpy logistic / ridge; optional sklearn /
                                              xgboost / lightgbm adapters
      -> training run            training.py  time-aware split, artifact, metrics
      -> model registry          registry.py  versions + lifecycle (DRAFT .. ACTIVE .. ARCHIVED)
      -> prediction              predict.py   ml_prediction rows, explanations
      -> ML score                strategy_features.py  W3 features ml_score / ml_prediction /
                                              ml_confidence / ml_prob_up
      -> strategy engine (W3) -> PositionIntent -> risk engine (W4) -> order manager -> paper

A model never places an order and nothing in ml/ imports execution/: ML output
reaches trading only as a feature a strategy reads, and the strategy's
decisions still pass through the W4 risk engine.

W40: meta_label.py -- a secondary model over the technical signals (triple-barrier labels,
uniqueness weights, purged k-fold, bet sizing, an ADOPTABLE / NO_EDGE gate); research only.

Also: regime.py (deterministic / ML / hybrid regime providers), monitoring.py
(prediction and feature drift, realised performance), explain.py (linear
contributions, global importances), assistant.py (read-only "why" answers).
"""
