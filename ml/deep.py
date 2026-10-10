"""
Deep learning (W36: ML-02) -- a native NumPy multi-layer perceptron and the benefit gate
that decides whether it may be used at all.

NeuralNetwork (model_type "neural_network", replacing the W5 placeholder)
    Fully connected ReLU network on the standard Preprocessor (median impute, z-score):
        hidden        layer sizes, default [32, 16]
        dropout       on hidden activations during training
        l2            weight decay
        optimiser     Adam (learning_rate, betas 0.9 / 0.999), mini-batches of batch_size
        early stop    the LAST val_fraction of the training rows (the rows arrive in date
                      order from ml.dataset) are held out; training stops after `patience`
                      epochs without a validation improvement and the best weights are kept --
                      the held-out block is the most recent, so it never peeks backwards
        output        softmax (classification) or linear (regression)
    Deterministic for a given seed. No GPU, no torch / tensorflow dependency: the features are
    tabular cross-sections where small networks are the only sensible size anyway.
    importances(): mean absolute input-gradient of the prediction over the training rows
    (normalised), so the W24 explanation views have something honest to show.

benefit_check(conn, dataset_spec)  -- "only if validated benefit" (the tracker's condition)
    Runs ml.validation.walk_forward for the network and for each baseline (logistic / linear
    regression and native_gbm) on the SAME dataset, windows and embargo, then:
        ADOPTABLE      the network's pooled out-of-sample score beats the best baseline by at
                       least MARGIN (relative), it wins at least MIN_WINDOW_SHARE of the windows,
                       and its own verdict is EDGE (IC t-stat >= 2)
        NOT_ADOPTABLE  otherwise -- with the reason
    The result is stored in ml_dl_benefit. ml/registry.transition refuses to move a
    neural_network version to APPROVED / ACTIVE unless its dataset has an ADOPTABLE check
    (see allowed_to_activate()).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

import numpy as np

from ml.models import BaseModel, Preprocessor

MARGIN = 0.01            # relative improvement over the best baseline's pooled score
MIN_WINDOW_SHARE = 0.6


class NeuralNetwork(BaseModel):
    model_type = "neural_network"
    default_params = {"hidden": [32, 16], "learning_rate": 0.003, "epochs": 200, "batch_size": 256, "l2": 1e-4,
                      "dropout": 0.1, "patience": 15, "val_fraction": 0.15, "seed": 7}

    # ── network ──
    def _init(self, p_in, p_out, rng):
        sizes = [p_in] + [int(h) for h in self.params["hidden"]] + [p_out]
        self.W = [rng.normal(0, np.sqrt(2.0 / a), (a, b)) for a, b in zip(sizes, sizes[1:])]
        self.B = [np.zeros(b) for b in sizes[1:]]

    def _forward(self, Z, train=False, rng=None):
        acts, masks = [Z], []
        h = Z
        for i, (W, b) in enumerate(zip(self.W, self.B)):
            h = h @ W + b
            if i < len(self.W) - 1:
                h = np.maximum(h, 0.0)
                if train and self.params["dropout"] > 0:
                    keep = 1.0 - float(self.params["dropout"])
                    m = (rng.random(h.shape) < keep) / keep
                    h = h * m
                    masks.append(m)
                else:
                    masks.append(None)
            acts.append(h)
        return acts, masks

    @staticmethod
    def _softmax(A):
        A = A - A.max(axis=1, keepdims=True)
        E = np.exp(A)
        return E / E.sum(axis=1, keepdims=True)

    def _loss_grad(self, out, Y):
        if self.task == "classification":
            P = self._softmax(out)
            loss = -np.mean(np.log(np.clip((P * Y).sum(axis=1), 1e-12, None)))
            return loss, (P - Y) / len(Y)
        d = out[:, 0] - Y
        return float(np.mean(d ** 2)), (2 * d / len(Y))[:, None]

    def _backward(self, acts, masks, G):
        gW, gB = [None] * len(self.W), [None] * len(self.W)
        for i in range(len(self.W) - 1, -1, -1):
            gW[i] = acts[i].T @ G + float(self.params["l2"]) * self.W[i]
            gB[i] = G.sum(axis=0)
            if i > 0:
                G = G @ self.W[i].T
                G = G * (acts[i] > 0)
                if masks[i - 1] is not None:
                    G = G * masks[i - 1]
        return gW, gB

    def _targets(self, y):
        if self.task == "classification":
            idx = {c: i for i, c in enumerate(self.classes)}
            Y = np.zeros((len(y), len(self.classes)))
            Y[np.arange(len(y)), [idx[str(v)] for v in y]] = 1
            return Y
        return np.asarray(y, dtype=float)

    # ── interface ──
    def fit(self, X, y):
        rng = np.random.default_rng(int(self.params["seed"]))
        if self.task == "classification":
            y = [str(v) for v in y]
            present = set(y)
            wanted = [str(c) for c in (self.classes or [])]
            self.classes = [c for c in wanted if c in present] or sorted(present)
        else:
            y = [float(v) for v in y]
            self.y_mean, self.y_sd = float(np.mean(y)), float(np.std(y) or 1.0)
            y = [(v - self.y_mean) / self.y_sd for v in y]
        self.pre = Preprocessor().fit(X)
        Z = self.pre.transform(X)
        Y = self._targets(y)
        n = len(Z)
        n_val = int(n * float(self.params["val_fraction"])) if n >= 200 else 0
        Ztr, Ytr = (Z[:-n_val], Y[:-n_val]) if n_val else (Z, Y)
        Zva, Yva = (Z[-n_val:], Y[-n_val:]) if n_val else (None, None)
        self._init(Z.shape[1], len(self.classes) if self.task == "classification" else 1, rng)
        mW = [np.zeros_like(w) for w in self.W]; vW = [np.zeros_like(w) for w in self.W]
        mB = [np.zeros_like(b) for b in self.B]; vB = [np.zeros_like(b) for b in self.B]
        lr, b1, b2, t = float(self.params["learning_rate"]), 0.9, 0.999, 0
        bs = max(16, int(self.params["batch_size"]))
        best, best_state, stale = np.inf, None, 0
        self.history = []
        for epoch in range(int(self.params["epochs"])):
            order = rng.permutation(len(Ztr))
            for s in range(0, len(order), bs):
                ix = order[s:s + bs]
                acts, masks = self._forward(Ztr[ix], train=True, rng=rng)
                _, G = self._loss_grad(acts[-1], Ytr[ix])
                gW, gB = self._backward(acts, masks, G)
                t += 1
                for i in range(len(self.W)):
                    for P_, g, m, v in ((self.W, gW, mW, vW), (self.B, gB, mB, vB)):
                        m[i] = b1 * m[i] + (1 - b1) * g[i]
                        v[i] = b2 * v[i] + (1 - b2) * g[i] ** 2
                        P_[i] -= lr * (m[i] / (1 - b1 ** t)) / (np.sqrt(v[i] / (1 - b2 ** t)) + 1e-8)
            if Zva is not None:
                vl, _ = self._loss_grad(self._forward(Zva)[0][-1], Yva)
                self.history.append(round(float(vl), 6))
                if vl < best - 1e-6:
                    best, stale = vl, 0
                    best_state = ([w.copy() for w in self.W], [b.copy() for b in self.B])
                else:
                    stale += 1
                    if stale >= int(self.params["patience"]):
                        break
        if best_state:
            self.W, self.B = best_state
        self.epochs_run = epoch + 1
        self._imp = self._grad_importance(Ztr[:2000])
        return self

    def _raw(self, X):
        return self._forward(self.pre.transform(X))[0][-1]

    def predict_proba(self, X):
        if self.task != "classification":
            raise NotImplementedError("regression has no probabilities")
        return self._softmax(self._raw(X))

    def predict(self, X):
        out = self._raw(X)
        if self.task == "classification":
            return [self.classes[i] for i in out.argmax(axis=1)]
        return (out[:, 0] * self.y_sd + self.y_mean).tolist()

    def _grad_importance(self, Z):
        if not len(Z):
            return None
        acts, masks = self._forward(Z)
        out = acts[-1]
        G = (self._softmax(out) if self.task == "classification" else np.ones_like(out)) / len(Z)
        g = G
        for i in range(len(self.W) - 1, -1, -1):
            g = g @ self.W[i].T
            if i > 0:
                g = g * (acts[i] > 0)
        imp = np.abs(g).mean(axis=0) * len(Z)
        return imp / imp.sum() if imp.sum() else None

    def importances(self):
        imp = getattr(self, "_imp", None)
        return dict(zip(self.columns, imp.tolist())) if imp is not None and self.columns else None

    def metadata(self) -> dict:
        m = super().metadata()
        m.update({"epochs_run": getattr(self, "epochs_run", None),
                  "best_validation_loss": min(self.history) if getattr(self, "history", None) else None})
        return m

    def to_state(self):
        return {"W": [w.tolist() for w in self.W], "B": [b.tolist() for b in self.B], "pre": self.pre.to_state(),
                "classes": self.classes, "y_mean": getattr(self, "y_mean", None), "y_sd": getattr(self, "y_sd", None),
                "imp": None if getattr(self, "_imp", None) is None else self._imp.tolist(),
                "epochs_run": getattr(self, "epochs_run", None)}

    def load_state(self, s):
        self.W = [np.array(w) for w in s["W"]]
        self.B = [np.array(b) for b in s["B"]]
        self.pre = Preprocessor.from_state(s["pre"])
        self.classes, self.y_mean, self.y_sd = s.get("classes"), s.get("y_mean"), s.get("y_sd")
        self._imp = None if s.get("imp") is None else np.array(s["imp"])
        self.epochs_run = s.get("epochs_run")
        return self


# ── the benefit gate ─────────────────────────────────────────────────────
def _score(task, pooled):
    return -pooled["log_loss"] if task == "classification" else -pooled["rmse"]


def benefit_check(conn, dataset_spec: dict, params: dict | None = None, baselines=None, n_windows: int = 5) -> dict:
    from ml import dataset as DS
    from ml.labels import LabelSpec
    from ml.validation import walk_forward
    spec = DS.DatasetSpec.from_dict(dataset_spec)
    task = LabelSpec.from_dict(spec.label).task
    base_types = baselines or (["logistic_regression"] if task == "classification" else ["linear_regression"]) \
        + ["native_gbm"]
    ds = DS.build(conn, spec)
    nn = walk_forward(conn, "neural_network", dataset_spec, params, n_windows=n_windows, ds=ds)
    bases = {}
    for bt in base_types:
        try:
            bases[bt] = walk_forward(conn, bt, dataset_spec, None, n_windows=n_windows, ds=ds, perm_importance=False)
        except Exception as e:
            bases[bt] = {"error": str(e)}
    ok = {k: v for k, v in bases.items() if "pooled" in v}
    if not ok:
        verdict, reason, best = "NOT_ADOPTABLE", "no baseline could be validated on this dataset", None
    else:
        best = max(ok, key=lambda k: _score(task, ok[k]["pooled"]))
        s_nn, s_b = _score(task, nn["pooled"]), _score(task, ok[best]["pooled"])
        rel = (s_nn - s_b) / abs(s_b) if s_b else 0.0
        bw = {w["test_start"]: _score(task, w["metrics"]) for w in ok[best]["windows"]}
        wins = [w for w in nn["windows"] if w["test_start"] in bw]
        share = (sum(1 for w in wins if _score(task, w["metrics"]) > bw[w["test_start"]]) / len(wins)) if wins else 0.0
        if rel < MARGIN:
            verdict, reason = "NOT_ADOPTABLE", f"pooled score {rel:+.2%} vs {best} (needs >= {MARGIN:.0%})"
        elif share < MIN_WINDOW_SHARE:
            verdict, reason = "NOT_ADOPTABLE", f"wins {share:.0%} of windows vs {best} (needs {MIN_WINDOW_SHARE:.0%})"
        elif nn["verdict"] != "EDGE":
            verdict, reason = "NOT_ADOPTABLE", f"the network's own verdict is {nn['verdict']} (needs EDGE)"
        else:
            verdict, reason = "ADOPTABLE", f"beats {best} by {rel:+.2%} pooled and in {share:.0%} of windows, EDGE"
    out = {"check_id": "DL" + uuid.uuid4().hex[:14].upper(), "dataset_id": spec.dataset_id, "task": task,
           "params": params or {}, "verdict": verdict, "reason": reason, "best_baseline": best,
           "network": {"report_id": nn.get("report_id"), "pooled": nn["pooled"], "verdict": nn["verdict"]},
           "baselines": {k: ({"report_id": v.get("report_id"), "pooled": v["pooled"], "verdict": v["verdict"]}
                             if "pooled" in v else v) for k, v in bases.items()},
           "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    conn.execute("INSERT INTO ml_dl_benefit (check_id,dataset_id,verdict,reason,result_json,created_at) VALUES "
                 "(?,?,?,?,?,?)", (out["check_id"], spec.dataset_id, verdict, reason, json.dumps(out, default=str),
                                   datetime.now()))
    conn.commit()
    return out


def allowed_to_activate(conn, dataset_id: str) -> tuple:
    """(ok, why) for a neural_network version trained on dataset_id."""
    # rowid (on PostgreSQL the identity `seq` column db.postgres gives the entry-order
    # tables, db.backend.ENTRY_ORDER_TABLES): picks the LATEST check when two
    # share a created_at, which TIMESTAMP's one-second resolution makes likely for checks
    # queued together.
    r = conn.execute("SELECT verdict, reason FROM ml_dl_benefit WHERE dataset_id=? "
                     "ORDER BY created_at DESC, rowid DESC LIMIT 1", (dataset_id,)).fetchone()
    if not r:
        return False, f"no deep-learning benefit check for dataset {dataset_id} (POST /api/ml/deep/benefit-check)"
    return r[0] == "ADOPTABLE", f"latest benefit check: {r[0]} -- {r[1]}"


def checks(conn, limit=50) -> list:
    return [dict(zip(("check_id", "dataset_id", "verdict", "reason", "created_at"), r)) for r in conn.execute(
        "SELECT check_id, dataset_id, verdict, reason, created_at FROM ml_dl_benefit ORDER BY created_at DESC LIMIT ?",
        (int(limit),))]
