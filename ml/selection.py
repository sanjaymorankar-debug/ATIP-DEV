"""
Importance- and stability-based feature selection (W24, ML-07).

    recommend(conn, report_id, min_stability=0.6) -> which columns to keep, and why
    apply(conn, report_id, name, version, ...)   -> a new, versioned feature set

From a walk-forward validation report (validation.py), each column's out-of-sample
permutation importance in every window:
    mean_importance   average drop in the window metric when the column is shuffled
    stability         share of windows in which that drop was positive
A column is KEPT when mean_importance > 0 and stability >= min_stability; otherwise
DROPPED with the reason. One-hot columns (feature=category) are folded back into their
feature: a feature is kept when any of its columns is kept. The result can be saved as
a new feature-set version -- never an overwrite (feature sets are content-hashed).
"""

from __future__ import annotations


def recommend(conn, report_id: str, min_stability: float = 0.6) -> dict:
    from ml.validation import get_report
    rep = get_report(conn, report_id)
    wins = [w["perm_importance"] for w in rep["windows"] if w.get("perm_importance")]
    if not wins:
        raise ValueError("the report has no permutation importances (run it with perm_importance)")
    cols = rep["columns"]
    rows = []
    for c in cols:
        v = [w.get(c, 0.0) for w in wins]
        mean = sum(v) / len(v)
        stab = sum(1 for x in v if x > 0) / len(v)
        keep = mean > 0 and stab >= min_stability
        rows.append({"column": c, "feature": c.split("=", 1)[0], "mean_importance": round(mean, 6),
                     "stability": round(stab, 3), "keep": keep,
                     "reason": "stable positive importance" if keep else
                     ("importance <= 0 out of sample" if mean <= 0 else f"stability {stab:.2f} < {min_stability}")})
    rows.sort(key=lambda r: -r["mean_importance"])
    feats = {}
    for r in rows:
        feats[r["feature"]] = feats.get(r["feature"], False) or r["keep"]
    return {"report_id": report_id, "windows": len(wins), "min_stability": min_stability, "columns": rows,
            "keep_features": [f for f, k in feats.items() if k], "drop_features": [f for f, k in feats.items() if not k]}


def apply(conn, report_id: str, name: str, version: str, min_stability: float = 0.6) -> dict:
    from ml.feature_registry import FeatureSet, save_feature_set
    rec = recommend(conn, report_id, min_stability)
    if len(rec["keep_features"]) < 2:
        raise ValueError(f"only {len(rec['keep_features'])} feature(s) pass: not enough for a feature set")
    fs = FeatureSet(name, str(version), rec["keep_features"],
                    f"selected from validation report {report_id} (stability >= {min_stability})")
    return {"feature_set": save_feature_set(conn, fs), "recommendation": rec}
