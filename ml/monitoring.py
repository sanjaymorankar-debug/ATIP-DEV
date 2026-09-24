"""
Model monitoring foundation (persistence + first metrics; alerting is W6/W8).

record_batch()   after every stored prediction batch -> ml_model_monitoring:
    prediction distribution  class counts + mean probabilities, or mean/std of values
    feature drift            PSI of each input column vs the training reference
                             quantiles stored in the artifact (reference_stats);
                             PSI > 0.25 is flagged "shift" (a common rule of thumb,
                             recorded as such, not a verdict)
    data quality             missing-value rate per column vs training
    activation period        model version + status on that date

evaluate_matured()  for predictions whose horizon has elapsed, compute the
    realised label from prices (labels.label_for) and store metrics of the
    matured predictions in ml_model_metrics (kind "realised") -- the basis for
    decay monitoring. Nothing is assumed where the data is missing.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import numpy as np

PSI_SHIFT = 0.25


def _psi(values, quantiles):
    v = values[~np.isnan(values)]
    if len(v) < 10 or quantiles is None:
        return None
    edges = [-np.inf] + sorted(set(quantiles)) + [np.inf]
    act = np.histogram(v, bins=edges)[0] / len(v)
    exp = np.diff([0.0] + [p / 100 for p in (10, 25, 50, 75, 90)][:len(edges) - 2] + [1.0])
    if len(exp) != len(act):
        return None
    act, exp = np.clip(act, 1e-4, None), np.clip(exp, 1e-4, None)
    return float(np.sum((act - exp) * np.log(act / exp)))


def record_batch(conn, model_id, version, as_of, X, columns, preds, reference):
    X = np.asarray(X, dtype=float)
    drift, quality = {}, {}
    q = np.array(reference["quantiles"]) if reference and reference.get("quantiles") else None
    for j, c in enumerate(columns):
        col = X[:, j]
        quality[c] = {"missing": round(float(np.isnan(col).mean()), 4),
                      "train_missing": round(float(reference["missing"][j]), 4) if reference else None}
        psi = _psi(col, q[:, j].tolist() if q is not None and len(set(q[:, j])) == q.shape[0] else None)
        if psi is not None:
            drift[c] = {"psi": round(psi, 4), "shift": psi > PSI_SHIFT}
    classes = [p["prediction"] for p in preds]
    dist = {"rows": len(preds)}
    if preds and preds[0].get("probabilities"):
        vals, cnt = np.unique(classes, return_counts=True)
        dist["class_counts"] = {str(v): int(c) for v, c in zip(vals, cnt)}
        keys = list(preds[0]["probabilities"])
        dist["mean_probabilities"] = {k: round(float(np.mean([p["probabilities"][k] for p in preds])), 4) for k in keys}
    else:
        vv = [p["prediction_value"] for p in preds if p.get("prediction_value") is not None]
        if vv:
            dist.update(mean=round(float(np.mean(vv)), 6), std=round(float(np.std(vv)), 6))
    status = conn.execute("SELECT status FROM ml_model_version WHERE model_id=? AND version=?",
                          (model_id, version)).fetchone()
    conn.execute("INSERT INTO ml_model_monitoring (model_id,version,as_of,version_status,prediction_dist_json,"
                 "feature_drift_json,data_quality_json,n_shifted,created_at) VALUES (?,?,?,?,?,?,?,?,?) "
                 "ON CONFLICT(model_id,version,as_of) DO UPDATE SET prediction_dist_json=excluded.prediction_dist_json,"
                 "feature_drift_json=excluded.feature_drift_json,data_quality_json=excluded.data_quality_json,"
                 "n_shifted=excluded.n_shifted,created_at=excluded.created_at",
                 (model_id, version, str(as_of), status[0] if status else None, json.dumps(dist),
                  json.dumps(drift), json.dumps(quality), sum(1 for d in drift.values() if d["shift"]),
                  datetime.now()))
    conn.commit()


def evaluate_matured(conn, model_id, version=None) -> dict | None:
    """Realised performance of stored predictions whose label horizon has elapsed."""
    from backtest.data import PriceHistory
    from ml import artifacts, registry as REG
    from ml.labels import LabelSpec, label_for
    from ml.models import metrics
    mv = REG.get_version(conn, model_id, version)
    if not mv or not mv.get("artifact_path"):
        return None
    _, doc = artifacts.load(mv["artifact_path"], mv["artifact_hash"])
    lab = LabelSpec.from_dict(doc["label"])
    if lab.kind == "market_regime":
        return None                                     # evaluated against market_health later (W6)
    rows = conn.execute("SELECT symbol, as_of, prediction FROM ml_prediction WHERE model_id=? AND model_version=?",
                        (model_id, mv["version"])).fetchall()
    if not rows:
        return None
    syms = sorted({r[0] for r in rows})
    d0 = min(date.fromisoformat(str(r[1])[:10]) for r in rows)
    hist = PriceHistory.load(conn, syms, d0, date.today(), warmup_days=5)
    yt, yp = [], []
    for sym, a, p in rows:
        a = date.fromisoformat(str(a)[:10])
        i = hist._index.get(sym, {}).get(a)
        if i is None:
            continue
        v, _ = label_for(hist.bars[sym], i, lab)
        if v is None:
            continue
        yt.append(v); yp.append(p if lab.task == "classification" else float(p))
    if not yt:
        return None
    m = metrics(lab.task, yt, yp)
    m["note"] = "realised on matured predictions; descriptive"
    conn.execute("INSERT INTO ml_model_metrics (model_id,version,kind,period_start,period_end,metrics_json,created_at) "
                 "VALUES (?,?,?,?,?,?,?)", (model_id, mv["version"], "realised", str(d0), str(date.today()),
                                            json.dumps(m), datetime.now()))
    conn.commit()
    return m
