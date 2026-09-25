"""
The prediction engine.

    predict(conn, model_id, as_of=None, version=None, symbols=None, store=True)

1. the model version: the ACTIVE one (default), or a named version. Predictions
   from a version that is not ACTIVE are stored with its status (e.g.
   VALIDATION -- "shadow" predictions) and are never served to strategies.
2. the artifact is loaded from the registry path, hash-checked, cached (no
   retraining, no re-reading).
3. features at the close of `as_of` for the universe -- the same point-in-time
   pipeline as training (W3 EvalEnv + the deterministic Market Health regime),
   encoded to the artifact's columns, missing values imputed with the training
   medians inside the model.
4. outputs per symbol, persisted in ml_prediction:
     classification  prediction (class), probabilities, confidence = highest
                     class probability, prob_up (P(UP) or P(1) when the label has one)
     regression      prediction (value), interval (+/-1.96 residual sd, when the
                     family provides one); confidence left NULL -- not invented
     ml_score        0..100 standardised score for strategies:
                     direction      50 x (1 + P(UP) - P(DOWN))
                     binary_return  100 x P(1)
                     forward_return / return_rank  percentile rank of the prediction
                                    among the day's predictions x 100
                     signal_outcome 100 x P(WIN)
                     volatility / market regime labels: NULL (not directional)
     explanation     explain.explain_row (top features, contributions, reason codes)
     features        the input values used (traceability)

Predictions are information. Nothing here creates an intent or an order.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timedelta

import numpy as np

from ml import artifacts, registry as REG
from ml.dataset import encode_row
from ml.explain import explain_row

log = logging.getLogger("atip.ml")


def _d(x):
    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


def _latest_session(conn):
    r = conn.execute("SELECT MAX(date) FROM prices_daily WHERE source IN ('dhan','bhavcopy')").fetchone()[0]
    return _d(r) if r else None


def feature_rows(conn, feature_names, as_of, symbols):
    """{symbol: {feature: value}} at the close of as_of (point in time)."""
    from backtest.data import Bar, PriceHistory, ScoresHistory
    from strategy_engine.kinds import EvalEnv
    from strategy_engine.regime import MarketHealthRegime
    if symbols == ["NIFTY50"]:
        nb = []
        for d, o, h, l, c, v in conn.execute("SELECT date, open, high, low, close, volume FROM prices_daily WHERE "
                                             "symbol='NIFTY50' AND date>=? AND date<=? ORDER BY date",
                                             (str(as_of - timedelta(days=420)), str(as_of))):
            if c and (not nb or _d(d) > nb[-1].date):
                nb.append(Bar(_d(d), float(o or c), float(h or c), float(l or c), float(c), float(v or 0)))
        hist = PriceHistory({"NIFTY50": nb}, [], (str(as_of), str(as_of), 420))
    else:
        hist = PriceHistory.load(conn, symbols, as_of, as_of, warmup_days=420)
    bench = {}
    for d, c in conn.execute("SELECT date, close FROM prices_daily WHERE symbol='NIFTY50' AND date>=? AND date<=?",
                             (str(as_of - timedelta(days=420)), str(as_of))):
        bench[_d(d)] = c
    env = EvalEnv(hist, symbols, ScoresHistory(conn, as_of - timedelta(days=10), as_of),
                  MarketHealthRegime(conn, as_of - timedelta(days=10), as_of), bench)
    from ml import context_features as CF
    from ml.dataset import w3_inputs
    w3 = w3_inputs(feature_names)
    out = {}
    for s in symbols:
        ctx = env.context(s, as_of)
        if ctx is not None:
            out[s] = {f: ctx.get(f) for f in w3}
    if any(f in CF.ALL for f in feature_names):     # same context code as the dataset builder
        CF.enrich(out, feature_names, as_of, CF.MarketContext(conn, as_of, as_of),
                  CF.sector_map() if any(f in CF.CROSS_SECTIONAL for f in feature_names) else {})
    return out


def _score(label_kind, classes, proba_row, value=None):
    if proba_row is None:
        return None
    p = dict(zip([str(c) for c in classes], proba_row))
    if label_kind == "direction":
        return round(50 * (1 + p.get("UP", 0) - p.get("DOWN", 0)), 2)
    if label_kind == "binary_return":
        return round(100 * p.get("1", 0), 2)
    if label_kind == "signal_outcome":
        return round(100 * p.get("WIN", 0), 2)
    return None


def predict(conn, model_id: str, as_of=None, version: str | None = None, symbols=None, store: bool = True) -> dict:
    m = REG.get_model(conn, model_id)
    if not m:
        raise REG.ModelRegistryError(f"no model {model_id}")
    mv = REG.get_version(conn, model_id, version)
    if not mv:
        raise REG.ModelRegistryError(f"model {model_id} has no {'version ' + version if version else 'ACTIVE version'}")
    if not mv.get("artifact_path"):
        raise REG.ModelRegistryError(f"{model_id} {mv['version']} has no trained artifact")
    model, doc = artifacts.load(mv["artifact_path"], mv["artifact_hash"])
    as_of = _d(as_of) if as_of else _latest_session(conn)
    feats, all_cols, keep_cols = doc["feature_names"], doc["all_columns"], doc["selected_columns"]
    keep = [all_cols.index(c) for c in keep_cols]
    if symbols is None:
        if m["purpose"] == "regime":
            symbols = ["NIFTY50"]
        else:
            from backtest.data import tracked_universe
            symbols = list(tracked_universe(conn).symbols)
    rows = feature_rows(conn, feats, as_of, list(symbols))
    if not rows:
        raise ValueError(f"no feature rows for {as_of} (no bars on that date?)")
    syms = sorted(rows)
    X = np.array([encode_row(rows[s], feats)[1] for s in syms], dtype=float)[:, keep]
    label = doc["label"]
    preds = model.predict(X)
    proba = model.predict_proba(X) if model.task == "classification" else None
    interval = model.interval(X) if model.task == "regression" else None
    ranks = None
    if model.task == "regression" and label["kind"] in ("forward_return", "return_rank") and len(preds) > 1:
        order = np.argsort(np.argsort(preds))
        ranks = (order / (len(preds) - 1) * 100).tolist()
    now = datetime.now()
    out = []
    for i, s in enumerate(syms):
        pr = proba[i].tolist() if proba is not None else None
        probs = dict(zip([str(c) for c in model.classes], [round(float(v), 6) for v in pr])) if pr else None
        conf = round(max(pr), 6) if pr else None
        prob_up = next((probs[k] for k in ("UP", "1", "WIN") if probs and k in probs), None)
        score = _score(label["kind"], model.classes, pr) if pr else (round(ranks[i], 2) if ranks else None)
        ex = explain_row(model, X[i])
        rec = {"prediction_id": "PR" + uuid.uuid4().hex[:16].upper(), "model_id": model_id,
               "model_version": mv["version"], "version_status": mv["status"], "symbol": s, "as_of": str(as_of),
               "prediction": str(preds[i]), "prediction_value": (float(preds[i]) if model.task == "regression" else
                                                                  prob_up),
               "probabilities": probs, "confidence": conf, "prob_up": prob_up, "ml_score": score,
               "interval_low": round(interval[0][i], 6) if interval else None,
               "interval_high": round(interval[1][i], 6) if interval else None,
               "feature_set": mv["feature_set"], "feature_set_hash": mv["feature_set_hash"],
               "artifact_hash": mv["artifact_hash"], "explanation": ex,
               "features": {k: (v if not isinstance(v, float) else round(v, 6)) for k, v in rows[s].items()},
               "created_at": now.isoformat()}
        out.append(rec)
    if store:
        conn.executemany(
            "INSERT INTO ml_prediction (prediction_id,model_id,model_version,version_status,symbol,as_of,prediction,"
            "prediction_value,probabilities_json,confidence,prob_up,ml_score,interval_low,interval_high,feature_set,"
            "feature_set_hash,artifact_hash,explanation_json,features_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(model_id,model_version,symbol,as_of) DO UPDATE SET "
            "prediction=excluded.prediction,prediction_value=excluded.prediction_value,"
            "probabilities_json=excluded.probabilities_json,confidence=excluded.confidence,prob_up=excluded.prob_up,"
            "ml_score=excluded.ml_score,interval_low=excluded.interval_low,interval_high=excluded.interval_high,"
            "version_status=excluded.version_status,explanation_json=excluded.explanation_json,"
            "features_json=excluded.features_json,created_at=excluded.created_at",
            [(r["prediction_id"], model_id, r["model_version"], r["version_status"], r["symbol"], r["as_of"],
              r["prediction"], r["prediction_value"], json.dumps(r["probabilities"]), r["confidence"], r["prob_up"],
              r["ml_score"], r["interval_low"], r["interval_high"], r["feature_set"], r["feature_set_hash"],
              r["artifact_hash"], json.dumps(r["explanation"]), json.dumps(r["features"], default=str), now)
             for r in out])
        conn.commit()
        try:
            from ml.monitoring import record_batch
            record_batch(conn, model_id, mv["version"], as_of, X, keep_cols, out, doc.get("reference_stats"))
        except Exception as e:
            log.warning(f"  ml monitoring for {model_id}: {e}")
    return {"model_id": model_id, "version": mv["version"], "version_status": mv["status"], "as_of": str(as_of),
            "rows": len(out), "stored": store, "predictions": out}


def run_scheduled_predictions(trade_date=None) -> dict:
    """ml.enabled: predict with every ACTIVE model version (run_job compatible)."""
    from db.schema import get_connection
    from ml.config import settings
    if not settings()["enabled"]:
        return {"status": "SKIPPED", "rows": 0, "reason": "ml.enabled is false"}
    conn = get_connection()
    try:
        done, failed = {}, {}
        for (mid,) in conn.execute("SELECT DISTINCT model_id FROM ml_model_version WHERE status='ACTIVE'").fetchall():
            try:
                done[mid] = predict(conn, mid, trade_date)["rows"]
            except Exception as e:
                failed[mid] = str(e); log.warning(f"  ml predictions {mid}: {e}")
        try:
            from ml.monitoring import evaluate_matured
            for mid in done:
                evaluate_matured(conn, mid)
        except Exception as e:
            log.warning(f"  ml realised evaluation: {e}")
    finally:
        conn.close()
    if not done and not failed:
        return {"status": "SKIPPED", "rows": 0, "reason": "no ACTIVE model version"}
    return {"status": "FAILED" if failed and not done else "SUCCESS", "rows": sum(done.values()),
            "models": done, "failed": failed, "error": "; ".join(f"{k}: {v}" for k, v in failed.items()) or None}
