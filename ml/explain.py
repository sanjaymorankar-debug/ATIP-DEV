"""
Explainability foundation.

    explain_row(model, x_row)  -> {"top_features", "feature_contributions",
                                   "model_reason_codes", "explanation_version", "method"}

  linear models (logistic / ridge): contribution = coefficient x standardised
      value (for logistic, the coefficient of the predicted class). Exact for
      the model's linear score; this is not SHAP, but it is what SHAP returns
      for a linear model with independent features.
  other families: no per-row attribution in W5 -- the global importances
      stored with the model version (ml_model_explanation) are returned
      instead, flagged method="global_importance". A SHAP-style explainer can
      be added behind the same function later.

Reason codes: ML_POS_<feature> / ML_NEG_<feature> for the three largest
contributions (the direction each pushes the prediction).
"""

from __future__ import annotations

import numpy as np

EXPLANATION_VERSION = "linear-contrib-1"


def explain_row(model, x_row, top_n: int = 5) -> dict:
    c = model.contributions(np.asarray([x_row], dtype=float))
    if c is None:
        imp = model.importances() or {}
        top = sorted(imp.items(), key=lambda kv: -kv[1])[:top_n]
        return {"method": "global_importance", "explanation_version": "global-1",
                "top_features": [k for k, _ in top], "feature_contributions": dict(top), "model_reason_codes": []}
    row = c[0]
    order = np.argsort(-np.abs(row))[:top_n]
    contrib = {model.columns[i]: round(float(row[i]), 6) for i in order}
    codes = [f"ML_{'POS' if row[i] > 0 else 'NEG'}_{model.columns[i]}" for i in order[:3]]
    return {"method": "linear_contribution", "explanation_version": EXPLANATION_VERSION,
            "top_features": list(contrib), "feature_contributions": contrib, "model_reason_codes": codes}
