"""
Walk-forward model validation (W24, ML-08) and permutation importance (ML-10) --
the evidence a model needs before anyone relies on it.

    walk_forward(conn, model_type, dataset_spec, params=None, n_windows=5,
                 min_train_dates=40, embargo=None, perm_importance=True, store=True)

The dataset (point in time, ml/dataset.py) is built once. Its dates are cut into
`n_windows` consecutive test blocks after an initial training span; each window
trains on EVERY earlier row whose label date is before the test block's first
date minus `embargo` sessions (purged, expanding window), and predicts the block.
Nothing from a test block, or whose label overlaps it, is ever trained on.

Per window and pooled over all out-of-sample rows:
    classification  accuracy, log loss, vs the baselines "majority class" and
                    "training class frequencies" (log loss); rank IC between the model
                    score (P(up) - P(down), or P(positive class)) and the realised label
                    order; top-minus-bottom quintile of the realised label order;
                    calibration (predicted top-class probability deciles vs hit rate)
    regression      RMSE vs the training-mean baseline, rank IC, hit rate of the sign
    permutation importance: the drop in the window's primary metric when one column is
                    shuffled (seeded) -- model-agnostic, on out-of-sample rows only
VERDICT: EDGE when the pooled out-of-sample metric beats the baseline AND the mean
window IC is positive with t >= 2; WEAK when it beats the baseline but IC is not
significant; NO_EDGE otherwise. Stored in ml_validation_report.

ML-07 (feature selection) reads the permutation importances: see selection.py.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime

import numpy as np

ORDER = {"UP": 1, "FLAT": 0, "DOWN": -1, "1": 1, "0": 0, "POS": 1, "NEG": 0, "True": 1, "False": 0}


def _rank(a):
    a = np.asarray(a, dtype=float)
    r = np.empty(len(a))
    r[np.argsort(a, kind="mergesort")] = np.arange(len(a))
    return r


def _spearman(x, y):
    if len(x) < 10 or np.std(x) == 0 or np.std(y) == 0:
        return None
    return float(np.corrcoef(_rank(x), _rank(y))[0, 1])


def _score(proba, classes):
    idx = {c: i for i, c in enumerate(classes)}
    if "UP" in idx and "DOWN" in idx:
        return proba[:, idx["UP"]] - proba[:, idx["DOWN"]]
    pos = next((c for c in classes if c in ("1", "POS", "True", "UP")), classes[-1])
    return proba[:, idx[pos]]


def _order(y, classes):
    return np.array([ORDER.get(str(v), classes.index(str(v)) if str(v) in classes else 0) for v in y], dtype=float)


def _logloss(P, classes, y):
    idx = {c: i for i, c in enumerate(classes)}
    return float(-np.mean([math.log(max(1e-12, P[i][idx[str(v)]])) if str(v) in idx else math.log(1e-12)
                           for i, v in enumerate(y)]))


def _classification_metrics(model, X, y, prior):
    P = model.predict_proba(X)
    pred = [model.classes[i] for i in P.argmax(axis=1)]
    ys = [str(v) for v in y]
    acc = float(np.mean([a == b for a, b in zip(ys, pred)]))
    maj = max(set(prior), key=prior.get)
    base_P = np.tile([prior.get(c, 1e-6) for c in model.classes], (len(ys), 1))
    s, o = _score(P, model.classes), _order(ys, model.classes)
    q = np.quantile(s, [0.2, 0.8]) if len(s) >= 25 else None
    tb = (float(o[s >= q[1]].mean() - o[s <= q[0]].mean()) if q is not None and (s >= q[1]).any() and
          (s <= q[0]).any() else None)
    top = P.max(axis=1)
    hit = np.array([a == b for a, b in zip(ys, pred)], dtype=float)
    cal = []
    for lo in np.arange(0, 1, 0.1):
        m = (top >= lo) & (top < lo + 0.1)
        if m.sum() >= 10:
            cal.append({"p_range": [round(lo, 1), round(lo + 0.1, 1)], "mean_p": round(float(top[m].mean()), 3),
                        "hit_rate": round(float(hit[m].mean()), 3), "rows": int(m.sum())})
    return {"rows": len(ys), "accuracy": round(acc, 4),
            "baseline_accuracy": round(float(np.mean([v == maj for v in ys])), 4),
            "log_loss": round(_logloss(P, model.classes, ys), 4),
            "baseline_log_loss": round(_logloss(base_P, model.classes, ys), 4),
            "ic": None if _spearman(s, o) is None else round(_spearman(s, o), 4),
            "top_minus_bottom_quintile": None if tb is None else round(tb, 4), "calibration": cal}


def _regression_metrics(model, X, y, train_mean):
    yp, yt = np.asarray(model.predict(X), float), np.asarray(y, float)
    ic = _spearman(yp, yt)
    return {"rows": len(yt), "rmse": round(float(np.sqrt(np.mean((yt - yp) ** 2))), 6),
            "baseline_rmse": round(float(np.sqrt(np.mean((yt - train_mean) ** 2))), 6),
            "ic": None if ic is None else round(ic, 4),
            "hit_rate": round(float(np.mean(np.sign(yp) == np.sign(yt))), 4)}


def _primary(task, m):
    """Higher is better: -log loss (classification) / -RMSE (regression)."""
    return -m["log_loss"] if task == "classification" else -m["rmse"]


def _perm_importance(model, task, X, y, cols, prior, train_mean, seed=11, repeats=2):
    rng = np.random.default_rng(seed)
    f = (lambda Xp: _classification_metrics(model, Xp, y, prior)) if task == "classification" else \
        (lambda Xp: _regression_metrics(model, Xp, y, train_mean))
    base = _primary(task, f(X))
    out = {}
    for j, c in enumerate(cols):
        drops = []
        for _ in range(repeats):
            Xp = X.copy()
            Xp[:, j] = X[rng.permutation(len(X)), j]
            drops.append(base - _primary(task, f(Xp)))
        out[c] = round(float(np.mean(drops)), 6)
    return out


def walk_forward(conn, model_type, dataset_spec: dict, params: dict | None = None, n_windows: int = 5,
                 min_train_dates: int = 40, embargo: int | None = None, perm_importance: bool = True,
                 store: bool = True, ds=None) -> dict:
    from ml import dataset as DS
    from ml.labels import LabelSpec
    from ml.models import make_model
    spec = DS.DatasetSpec.from_dict(dataset_spec)
    lab = LabelSpec.from_dict(spec.label)
    ds = ds or DS.build(conn, spec)
    if len(ds.y) < 200:
        raise ValueError(f"dataset has {len(ds.y)} rows: walk-forward validation needs at least 200")
    emb = lab.horizon if embargo is None else int(embargo)
    udates = sorted(set(ds.dates))
    if len(udates) < min_train_dates + n_windows * 2:
        raise ValueError(f"{len(udates)} dates: too few for {min_train_dates} training dates + {n_windows} windows")
    block = (len(udates) - min_train_dates) // int(n_windows)
    X = ds.X
    miss = np.isnan(X).mean(axis=0)
    keep = [i for i in range(X.shape[1]) if miss[i] <= 0.5]
    cols = [ds.columns[i] for i in keep]
    X = X[:, keep]
    y = np.array(ds.y, dtype=object)
    dates, ldates = np.array(ds.dates), np.array(ds.label_dates)
    wins, oos_idx, oos_models, perms = [], [], [], []
    for w in range(int(n_windows)):
        t0 = udates[min_train_dates + w * block]
        t1 = udates[min(len(udates) - 1, min_train_dates + (w + 1) * block - 1)]
        cut = udates[max(0, udates.index(t0) - emb)]
        tr = np.where((dates < cut) & (ldates < t0))[0]
        te = np.where((dates >= t0) & (dates <= t1))[0]
        if len(tr) < 100 or len(te) < 20:
            continue
        m = make_model(model_type, lab.task, params, cols, lab.classes)
        if lab.task == "classification" and len({str(v) for v in y[tr]}) < 2:
            continue
        m.fit(X[tr], list(y[tr]))
        if lab.task == "classification":
            ys = [str(v) for v in y[tr]]
            prior = {c: ys.count(c) / len(ys) for c in set(ys)}
            met = _classification_metrics(m, X[te], list(y[te]), prior)
            tm = None
        else:
            prior, tm = None, float(np.mean(np.asarray(y[tr], float)))
            met = _regression_metrics(m, X[te], list(y[te]), tm)
        pi = _perm_importance(m, lab.task, X[te], list(y[te]), cols, prior, tm) if perm_importance else None
        wins.append({"window": w, "train_rows": int(len(tr)), "train_end": str(dates[tr].max()),
                     "test_start": str(t0), "test_end": str(t1), "metrics": met, "perm_importance": pi})
        oos_idx.append(te)
        oos_models.append((m, prior, tm))
        if pi:
            perms.append(pi)
    if not wins:
        raise ValueError("no window had enough rows")
    # pooled out-of-sample metrics: each row scored by the model of its own window
    if lab.task == "classification":
        classes = list(lab.classes or sorted({str(v) for v in y}))
        Ps, ys, base = [], [], []
        for te, (m, prior, _) in zip(oos_idx, oos_models):
            P = m.predict_proba(X[te])
            full = np.zeros((len(te), len(classes)))
            for j, c in enumerate(m.classes):
                if c in classes:
                    full[:, classes.index(c)] = P[:, j]
            Ps.append(full)
            ys += [str(v) for v in y[te]]
            base.append(np.tile([prior.get(c, 1e-6) for c in classes], (len(te), 1)))
        P, B = np.vstack(Ps), np.vstack(base)
        s, o = _score(P, classes), _order(ys, classes)
        pooled = {"rows": len(ys), "accuracy": round(float(np.mean([classes[i] == v for i, v in
                                                                    zip(P.argmax(1), ys)])), 4),
                  "log_loss": round(_logloss(P, classes, ys), 4), "baseline_log_loss": round(_logloss(B, classes, ys), 4),
                  "ic": _spearman(s, o)}
        beats = pooled["log_loss"] < pooled["baseline_log_loss"]
    else:
        yp, yt, bl = [], [], []
        for te, (m, _, tm) in zip(oos_idx, oos_models):
            yp += list(m.predict(X[te]))
            yt += [float(v) for v in y[te]]
            bl += [tm] * len(te)
        yp, yt, bl = np.array(yp), np.array(yt), np.array(bl)
        pooled = {"rows": len(yt), "rmse": round(float(np.sqrt(np.mean((yt - yp) ** 2))), 6),
                  "baseline_rmse": round(float(np.sqrt(np.mean((yt - bl) ** 2))), 6), "ic": _spearman(yp, yt),
                  "hit_rate": round(float(np.mean(np.sign(yp) == np.sign(yt))), 4)}
        beats = pooled["rmse"] < pooled["baseline_rmse"]
    ics = [w["metrics"]["ic"] for w in wins if w["metrics"].get("ic") is not None]
    t = (np.mean(ics) / (np.std(ics, ddof=1) / math.sqrt(len(ics)))) if len(ics) >= 3 and np.std(ics, ddof=1) > 0 \
        else None
    pooled["ic"] = None if pooled["ic"] is None else round(pooled["ic"], 4)
    pooled["mean_window_ic"] = round(float(np.mean(ics)), 4) if ics else None
    pooled["ic_t_stat"] = None if t is None else round(float(t), 3)
    verdict = "EDGE" if beats and t is not None and t >= 2 and np.mean(ics) > 0 else "WEAK" if beats else "NO_EDGE"
    rep = {"model_type": model_type, "dataset_id": spec.dataset_id, "task": lab.task, "label": lab.as_dict(),
           "params": params or {}, "embargo_sessions": emb, "windows": wins, "pooled": pooled, "verdict": verdict,
           "columns": cols, "method": "expanding window, purged by label date + embargo; one model per window"}
    if store:
        rid = "VR" + uuid.uuid4().hex[:14].upper()
        conn.execute("INSERT INTO ml_validation_report (report_id,model_type,dataset_id,label_json,params_json,"
                     "report_json,verdict,created_at) VALUES (?,?,?,?,?,?,?,?)",
                     (rid, model_type, spec.dataset_id, json.dumps(lab.as_dict()), json.dumps(params or {}),
                      json.dumps(rep, default=str), verdict, datetime.now()))
        conn.commit()
        rep["report_id"] = rid
    return rep


def get_report(conn, rid) -> dict:
    r = conn.execute("SELECT report_json, created_at FROM ml_validation_report WHERE report_id=?", (rid,)).fetchone()
    if not r:
        raise LookupError("validation report not found")
    d = json.loads(r[0])
    d.update({"report_id": rid, "created_at": str(r[1])[:19]})
    return d


def list_reports(conn, limit=50) -> list:
    return [dict(zip(("report_id", "model_type", "dataset_id", "verdict", "created_at"), r)) for r in conn.execute(
        "SELECT report_id, model_type, dataset_id, verdict, created_at FROM ml_validation_report ORDER BY created_at "
        "DESC LIMIT ?", (int(limit),))]
