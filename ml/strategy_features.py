"""
ML predictions as W3 strategy features (the ML -> strategy link).

MLPredictionHistory(conn, start, end, model_id=None).on(as_of, symbol) ->
    {"ml_score", "ml_prediction", "ml_confidence", "ml_prob_up"} or {}

Which predictions a strategy may see:
  * model: config ml.default_model (strategies name the feature, not the model);
    with no default model every ml_* feature is None and conditions on it are
    simply not met
  * only predictions stored for EXACTLY as_of (point in time: a prediction made
    at the close of t, from data up to t)
  * only from a version that was ACTIVE when it predicted (shadow predictions of
    VALIDATION / APPROVED versions are excluded)
  * only out of sample: as_of must be after the end of the version's training
    dataset (ml_dataset.end_date), so a backtest over the training period cannot
    use a model that has already seen those labels

A strategy reading ml_score still produces a StrategyDecision -> PositionIntent
-> W4 risk engine -> order manager. ML never reaches execution directly.
"""

from __future__ import annotations

from datetime import date


def _d(x):
    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


class MLPredictionHistory:
    def __init__(self, conn, start=None, end=None, model_id=None):
        from ml.config import settings
        self.model_id = model_id or settings().get("default_model")
        self._rows = {}
        if not self.model_id:
            return
        ends = {v: _d(e) for v, e in conn.execute(
            "SELECT v.version, d.end_date FROM ml_model_version v JOIN ml_dataset d ON d.dataset_id=v.dataset_id "
            "WHERE v.model_id=?", (self.model_id,)) if e}
        q = ("SELECT symbol, as_of, model_version, ml_score, prediction, confidence, prob_up FROM ml_prediction "
             "WHERE model_id=? AND version_status='ACTIVE'")
        args = [self.model_id]
        if start:
            q += " AND as_of>=?"; args.append(str(start))
        if end:
            q += " AND as_of<=?"; args.append(str(end))
        q += " ORDER BY as_of, created_at"
        for sym, a, ver, score, pred, conf, pu in conn.execute(q, args):
            a = _d(a)
            if ver in ends and a <= ends[ver]:
                continue                                  # in-sample for that version: never served
            self._rows[(a, sym)] = {"ml_score": score, "ml_prediction": pred, "ml_confidence": conf, "ml_prob_up": pu}

    def on(self, as_of, symbol) -> dict:
        return self._rows.get((as_of, symbol), {})
