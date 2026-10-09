"""
Native (numpy-only) tree models and the ensemble (W24: ML-01, ML-10, ML-11).

ATIP ships no scikit-learn / xgboost / lightgbm, so the W5 adapters for those raise
ModelDependencyError. These are self-contained implementations:

    native_gbm             gradient-boosted trees. Classification: multi-class softmax
                           boosting with one multi-output tree per round (gradient P - Y,
                           hessian P(1-P)); regression: squared loss. Second-order leaf
                           values -G/(H + l2), shrinkage `learning_rate`, row / column
                           subsampling. Deterministic for a given seed.
    native_random_forest   bagged trees with a random feature subset per tree; leaves
                           hold class frequencies (classification) or means (regression).
    ensemble               several member families fitted on the same rows and blended:
                           weights "equal", or "validation" -- members fitted on the first
                           80% of rows (rows are date-ordered), weighted by 1 / held-out
                           loss, then all refitted on every row.

TREES are histogram-based: each feature is cut into at most `max_bins` quantile bins
fitted on the training rows (missing values take the training median first), and
splits are searched on bin boundaries -- the LightGBM idea, in plain numpy.
Split gain = GL^2/(HL+l2) + GR^2/(HR+l2) - G^2/(H+l2), summed over outputs.

EXPLAINABILITY (ML-10): contributions(X) are path contributions (the "Saabas" /
tree-interpreter method): walking each row's decision path, the change in node value
at every split is credited to that split's feature, so bias + contributions = the
raw prediction (logit of the predicted class for classification). importances() are
total split gains, normalised.
"""

from __future__ import annotations

import math

import numpy as np

from ml.models import BaseModel, Preprocessor, _weights


# ── the multi-output histogram tree ────────────────────────────────────────
class _Binner:
    def fit(self, Z, max_bins):
        self.edges = []
        for j in range(Z.shape[1]):
            q = np.unique(np.quantile(Z[:, j], np.linspace(0, 1, max_bins + 1)[1:-1]))
            self.edges.append(q)
        return self

    def transform(self, Z):
        B = np.empty(Z.shape, dtype=np.int16)
        for j, e in enumerate(self.edges):
            B[:, j] = np.searchsorted(e, Z[:, j], side="right")
        return B


class _Tree:
    """Arrays: feat, thr (bin: go left when bin <= thr), left, right, value (k), gain."""

    def fit(self, B, G, H, cols, max_depth, min_leaf, l2, nbins, min_gain=1e-9):
        self.feat, self.thr, self.left, self.right, self.value, self.gain = [], [], [], [], [], []
        k = G.shape[1]

        def leaf_value(g, h):
            return -g.sum(axis=0) / (h.sum(axis=0) + l2)

        def build(idx, depth):
            node = len(self.feat)
            g, h = G[idx], H[idx]
            self.feat.append(-1); self.thr.append(0); self.left.append(-1); self.right.append(-1)
            self.value.append(leaf_value(g, h)); self.gain.append(0.0)
            if depth >= max_depth or len(idx) < 2 * min_leaf:
                return node
            Gt, Ht = g.sum(axis=0), h.sum(axis=0)
            parent = float(np.sum(Gt * Gt / (Ht + l2)))
            best = (min_gain, None, None)
            for j in cols:
                b = B[idx, j]
                cnt = np.bincount(b, minlength=nbins)
                if (cnt > 0).sum() < 2:
                    continue
                gh = np.stack([np.bincount(b, weights=g[:, c], minlength=nbins) for c in range(k)], axis=1)
                hh = np.stack([np.bincount(b, weights=h[:, c], minlength=nbins) for c in range(k)], axis=1)
                cg, chh, cc = np.cumsum(gh, axis=0)[:-1], np.cumsum(hh, axis=0)[:-1], np.cumsum(cnt)[:-1]
                ok = (cc >= min_leaf) & (len(idx) - cc >= min_leaf)
                if not ok.any():
                    continue
                gl = np.sum(cg * cg / (chh + l2), axis=1)
                gr_ = Gt - cg
                hr_ = Ht - chh
                gr = np.sum(gr_ * gr_ / (hr_ + l2), axis=1)
                gain = np.where(ok, gl + gr - parent, -np.inf)
                t = int(np.argmax(gain))
                if gain[t] > best[0]:
                    best = (float(gain[t]), j, t)
            if best[1] is None:
                return node
            j, t = best[1], best[2]
            m = B[idx, j] <= t
            self.feat[node], self.thr[node], self.gain[node] = j, t, best[0]
            self.left[node] = build(idx[m], depth + 1)
            self.right[node] = build(idx[~m], depth + 1)
            return node
        build(np.arange(B.shape[0]), 0)
        self._arrays()
        return self

    def _arrays(self):
        self.feat_a, self.thr_a = np.array(self.feat), np.array(self.thr)
        self.left_a, self.right_a = np.array(self.left), np.array(self.right)
        self.value_a = np.array(self.value, dtype=float)

    def predict(self, B):
        node = np.zeros(B.shape[0], dtype=int)
        active = self.feat_a[node] >= 0
        while active.any():
            ids = np.where(active)[0]
            f = self.feat_a[node[ids]]
            go_left = B[ids, f] <= self.thr_a[node[ids]]
            node[ids] = np.where(go_left, self.left_a[node[ids]], self.right_a[node[ids]])
            active = self.feat_a[node] >= 0
        return self.value_a[node]

    def contributions(self, B, out_idx, n_feat):
        """(bias vector per row, contribution matrix) for output column(s) out_idx per row."""
        n = B.shape[0]
        C = np.zeros((n, n_feat))
        bias = self.value_a[0][out_idx]
        node = np.zeros(n, dtype=int)
        rows = np.arange(n)
        while True:
            f = self.feat_a[node]
            act = f >= 0
            if not act.any():
                break
            r = rows[act]
            nd = node[act]
            nxt = np.where(B[r, f[act]] <= self.thr_a[nd], self.left_a[nd], self.right_a[nd])
            dv = self.value_a[nxt, out_idx[act]] - self.value_a[nd, out_idx[act]]
            np.add.at(C, (r, f[act]), dv)
            node[act] = nxt
        return bias, C

    def to_state(self):
        return {"feat": self.feat, "thr": self.thr, "left": self.left, "right": self.right,
                "value": [list(map(float, v)) for v in self.value], "gain": self.gain}

    @classmethod
    def from_state(cls, s):
        t = cls()
        t.feat, t.thr, t.left, t.right, t.value, t.gain = s["feat"], s["thr"], s["left"], s["right"], s["value"], s["gain"]
        t._arrays()
        return t


class _TreeModelBase(BaseModel):
    def _prep(self, X, fit=False):
        if fit:
            self.pre = Preprocessor().fit(X)
        X = np.asarray(X, dtype=float)
        Z = np.where(np.isnan(X), self.pre.medians, X)
        if fit:
            self.binner = _Binner().fit(Z, int(self.params["max_bins"]))
        return self.binner.transform(Z)

    def _targets(self, y):
        if self.task == "classification":
            y = [str(v) for v in y]
            wanted = [str(c) for c in (self.classes or [])]
            present = set(y)
            self.classes = [c for c in wanted if c in present] or sorted(present)
            idx = {c: i for i, c in enumerate(self.classes)}
            Y = np.zeros((len(y), len(self.classes)))
            Y[np.arange(len(y)), [idx[v] for v in y]] = 1
            return Y
        return np.asarray(y, dtype=float).reshape(-1, 1)

    def importances(self):
        imp = np.zeros(len(self.columns))
        for t in self.trees:
            for f, g in zip(t.feat, t.gain):
                if f >= 0:
                    imp[f] += g
        return dict(zip(self.columns, (imp / imp.sum()).tolist())) if imp.sum() > 0 else None

    def _base_state(self):
        return {"pre": self.pre.to_state(), "edges": [e.tolist() for e in self.binner.edges], "classes": self.classes,
                "trees": [t.to_state() for t in self.trees]}

    def _load_base(self, s):
        self.pre = Preprocessor.from_state(s["pre"])
        self.binner = _Binner()
        self.binner.edges = [np.array(e) for e in s["edges"]]
        self.classes = s["classes"]
        self.trees = [_Tree.from_state(t) for t in s["trees"]]


def _softmax(A):
    A = A - A.max(axis=1, keepdims=True)
    E = np.exp(A)
    return E / E.sum(axis=1, keepdims=True)


class NativeGBM(_TreeModelBase):
    model_type = "native_gbm"
    default_params = {"n_estimators": 150, "learning_rate": 0.05, "max_depth": 3, "min_samples_leaf": 40,
                      "l2": 1.0, "subsample": 0.8, "colsample": 0.8, "max_bins": 32, "seed": 7}

    def fit(self, X, y, sample_weight=None):
        """sample_weight (W40): per-row weights, rescaled to mean 1, multiplying each row's gradient and hessian
        (and the weighted prior); None is the unweighted fit, unchanged."""
        p = self.params
        rng = np.random.default_rng(int(p["seed"]))
        B = self._prep(X, fit=True)
        Y = self._targets(y)
        n, k = Y.shape
        sw = _weights(sample_weight, n)
        if self.task == "classification":
            prior = np.clip(Y.mean(axis=0) if sw is None else (Y * sw[:, None]).mean(axis=0), 1e-6, 1)
            self.init = np.log(prior) - np.log(prior).mean()
        else:
            self.init = np.array([float(Y.mean() if sw is None else (Y[:, 0] * sw).mean())])
        F = np.tile(self.init, (n, 1))
        self.trees = []
        m = B.shape[1]
        ncol = max(1, int(round(m * float(p["colsample"]))))
        for _ in range(int(p["n_estimators"])):
            if self.task == "classification":
                P = _softmax(F)
                G, H = P - Y, np.maximum(P * (1 - P), 1e-6)
            else:
                G, H = F - Y, np.ones_like(F)
            if sw is not None:
                G, H = G * sw[:, None], H * sw[:, None]
            rows = rng.random(n) < float(p["subsample"]) if float(p["subsample"]) < 1 else np.ones(n, bool)
            cols = sorted(rng.choice(m, ncol, replace=False).tolist())
            t = _Tree().fit(B[rows], G[rows], H[rows], cols, int(p["max_depth"]), int(p["min_samples_leaf"]),
                            float(p["l2"]), int(p["max_bins"]) + 1)
            F += float(p["learning_rate"]) * t.predict(B)
            self.trees.append(t)
        return self

    def _raw(self, X):
        B = self._prep(X)
        F = np.tile(self.init, (B.shape[0], 1))
        for t in self.trees:
            F += float(self.params["learning_rate"]) * t.predict(B)
        return F, B

    def predict_proba(self, X):
        if self.task != "classification":
            return super().predict_proba(X)
        return _softmax(self._raw(X)[0])

    def predict(self, X):
        F, _ = self._raw(X)
        if self.task == "classification":
            return [self.classes[i] for i in F.argmax(axis=1)]
        return F[:, 0].tolist()

    def contributions(self, X):
        F, B = self._raw(X)
        out = F.argmax(axis=1) if self.task == "classification" else np.zeros(len(F), dtype=int)
        C = np.zeros((len(F), B.shape[1]))
        lr = float(self.params["learning_rate"])
        for t in self.trees:
            _, c = t.contributions(B, out, B.shape[1])
            C += lr * c
        return C

    def to_state(self):
        return {**self._base_state(), "init": self.init.tolist()}

    def load_state(self, s):
        self._load_base(s)
        self.init = np.array(s["init"])
        return self


class NativeRandomForest(_TreeModelBase):
    model_type = "native_random_forest"
    default_params = {"n_estimators": 100, "max_depth": 8, "min_samples_leaf": 30, "max_features": "sqrt",
                      "bootstrap": True, "max_bins": 32, "seed": 7}

    def fit(self, X, y, sample_weight=None):
        """sample_weight (W40): each tree's bootstrap draws row i with probability w_i / sum(w) (AFML 4.5:
        weighted bagging); without bootstrap the weights are ignored. None is the unweighted fit, unchanged."""
        p = self.params
        rng = np.random.default_rng(int(p["seed"]))
        B = self._prep(X, fit=True)
        Y = self._targets(y)
        n, m = B.shape
        sw = _weights(sample_weight, n)
        mf = p["max_features"]
        ncol = max(1, int(math.sqrt(m))) if mf == "sqrt" else max(1, int(round(m * float(mf)))) if mf else m
        self.trees = []
        for _ in range(int(p["n_estimators"])):
            if not p["bootstrap"]:
                rows = np.arange(n)
            else:
                rows = rng.integers(0, n, n) if sw is None else rng.choice(n, n, replace=True, p=sw / sw.sum())
            cols = sorted(rng.choice(m, ncol, replace=False).tolist())
            # leaf value = -sum(g)/(sum(h)+l2) with g = -Y, h = 1 and l2 ~ 0 -> the mean target
            self.trees.append(_Tree().fit(B[rows], -Y[rows], np.ones_like(Y[rows]), cols, int(p["max_depth"]),
                                          int(p["min_samples_leaf"]), 1e-9, int(p["max_bins"]) + 1))
        return self

    def _avg(self, X):
        B = self._prep(X)
        return sum(t.predict(B) for t in self.trees) / len(self.trees), B

    def predict_proba(self, X):
        if self.task != "classification":
            return super().predict_proba(X)
        P, _ = self._avg(X)
        P = np.clip(P, 0, None)
        return P / np.maximum(P.sum(axis=1, keepdims=True), 1e-12)

    def predict(self, X):
        P, _ = self._avg(X)
        return [self.classes[i] for i in P.argmax(axis=1)] if self.task == "classification" else P[:, 0].tolist()

    def contributions(self, X):
        P, B = self._avg(X)
        out = P.argmax(axis=1) if self.task == "classification" else np.zeros(len(P), dtype=int)
        C = np.zeros((len(P), B.shape[1]))
        for t in self.trees:
            C += t.contributions(B, out, B.shape[1])[1]
        return C / len(self.trees)

    def to_state(self):
        return self._base_state()

    def load_state(self, s):
        self._load_base(s)
        return self


class EnsembleModel(BaseModel):
    model_type = "ensemble"
    default_params = {"members": ["logistic_regression", "native_gbm", "native_random_forest"],
                      "member_params": {}, "weights": "validation", "holdout": 0.2}

    def _member_types(self):
        from ml.models import MODEL_TYPES
        mem = list(self.params["members"])
        if len(mem) < 2 or any(m not in MODEL_TYPES or m == "ensemble" for m in mem):
            raise ValueError(f"ensemble members must be 2+ of {sorted(t for t in MODEL_TYPES if t != 'ensemble')}")
        if self.task == "regression":
            mem = ["linear_regression" if m == "logistic_regression" else m for m in mem]
        return mem

    def _make(self, t):
        from ml.models import make_model
        return make_model(t, self.task, (self.params.get("member_params") or {}).get(t), self.columns, self.classes)

    def _loss(self, m, X, y):
        if self.task == "classification":
            P = m.predict_proba(X)
            idx = {c: i for i, c in enumerate(m.classes)}
            return float(-np.mean([math.log(max(1e-12, P[i][idx[str(v)]])) if str(v) in idx else math.log(1e-12)
                                   for i, v in enumerate(y)]))
        return float(np.sqrt(np.mean((np.asarray(m.predict(X)) - np.asarray(y, float)) ** 2)))

    def fit(self, X, y):
        X = np.asarray(X, dtype=float)
        types = self._member_types()
        n = len(y)
        if self.task == "classification":
            present = sorted({str(v) for v in y})
            self.classes = [c for c in [str(c) for c in (self.classes or [])] if c in present] or present
        w = np.ones(len(types))
        self.holdout_losses = None
        if self.params["weights"] == "validation" and n >= 50:
            cut = int(n * (1 - float(self.params["holdout"])))
            losses = []
            for t in types:
                m = self._make(t).fit(X[:cut], list(y[:cut]))
                losses.append(self._loss(m, X[cut:], list(y[cut:])))
            self.holdout_losses = dict(zip(types, losses))
            w = np.array([1 / max(l_, 1e-9) for l_ in losses])
        self.weights = (w / w.sum()).tolist()
        self.types = types
        self.members = [self._make(t).fit(X, list(y)) for t in types]
        return self

    def predict_proba(self, X):
        if self.task != "classification":
            return super().predict_proba(X)
        out = np.zeros((np.asarray(X).shape[0], len(self.classes)))
        for w, m in zip(self.weights, self.members):
            P = m.predict_proba(X)
            for j, c in enumerate(m.classes):
                if c in self.classes:
                    out[:, self.classes.index(c)] += w * P[:, j]
        return out / np.maximum(out.sum(axis=1, keepdims=True), 1e-12)

    def predict(self, X):
        if self.task == "classification":
            return [self.classes[i] for i in self.predict_proba(X).argmax(axis=1)]
        return (sum(w * np.asarray(m.predict(X)) for w, m in zip(self.weights, self.members))).tolist()

    def contributions(self, X):
        cs = [(w, m.contributions(X)) for w, m in zip(self.weights, self.members)]
        cs = [(w, c) for w, c in cs if c is not None]
        return sum(w * c for w, c in cs) / sum(w for w, _ in cs) if cs else None

    def importances(self):
        acc = {}
        for w, m in zip(self.weights, self.members):
            for k, v in (m.importances() or {}).items():
                acc[k] = acc.get(k, 0.0) + w * v
        s = sum(acc.values())
        return {k: v / s for k, v in acc.items()} if s else None

    def metadata(self):
        return {**super().metadata(), "members": self.types, "weights": self.weights,
                "holdout_losses": self.holdout_losses}

    def to_state(self):
        return {"types": self.types, "weights": self.weights, "classes": self.classes,
                "holdout_losses": self.holdout_losses, "members": [m.to_state() for m in self.members]}

    def load_state(self, s):
        from ml.models import make_model
        self.types, self.weights, self.classes = s["types"], s["weights"], s["classes"]
        self.holdout_losses = s.get("holdout_losses")
        self.members = [make_model(t, self.task, (self.params.get("member_params") or {}).get(t), self.columns,
                                   self.classes).load_state(st) for t, st in zip(self.types, s["members"])]
        return self
