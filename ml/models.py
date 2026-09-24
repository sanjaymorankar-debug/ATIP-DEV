"""
The model interface and model families.

    BaseModel
      fit(X, y)                 X: float matrix with NaN for missing; y: labels / targets
      predict(X)                classes (classification) or values (regression)
      predict_proba(X)          class probabilities, classification only
      interval(X)               (low, high) prediction interval, regression when supported
      contributions(X)          per-row feature contributions (linear models)
      importances()             global feature importance where the family has one
      to_state() / from_state() serialisable state -> artifacts.py writes it
      metadata()                type, task, params, classes, columns

Families (MODEL_TYPES):
    logistic_regression   numpy; multinomial softmax, L2, full-batch gradient descent
                          from zeros (deterministic -- no random init)
    linear_regression     numpy; ridge closed form; residual stdev on the training
                          rows gives a +/-1.96 sd interval (stated as such, not a
                          guarantee)
    random_forest, gradient_boosting   scikit-learn adapters
    xgboost, lightgbm                  adapters for those packages
    neural_network                     placeholder for a future wave
The adapters import their library lazily: without it, training raises
ModelDependencyError naming the missing package. ATIP ships none of these
libraries (numpy-only by design); nothing is installed automatically.

Preprocessing (Preprocessor): median imputation and standardisation fitted on
the TRAINING rows only and stored with the model, so prediction applies the
same transform.
"""

from __future__ import annotations

import base64
import math
import pickle

import numpy as np


class ModelDependencyError(RuntimeError):
    pass


class Preprocessor:
    def __init__(self, medians=None, means=None, stds=None):
        self.medians, self.means, self.stds = medians, means, stds

    def fit(self, X):
        X = np.asarray(X, dtype=float)
        with np.errstate(all="ignore"):
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)   # all-NaN column -> 0 below
                med = np.nanmedian(X, axis=0) if X.size else np.zeros(X.shape[1])
        med = np.where(np.isnan(med), 0.0, med)
        Xi = np.where(np.isnan(X), med, X)
        self.medians, self.means = med, Xi.mean(axis=0)
        sd = Xi.std(axis=0)
        self.stds = np.where(sd > 1e-12, sd, 1.0)
        return self

    def transform(self, X):
        X = np.asarray(X, dtype=float)
        Xi = np.where(np.isnan(X), self.medians, X)
        return (Xi - self.means) / self.stds

    def to_state(self):
        return {"medians": self.medians.tolist(), "means": self.means.tolist(), "stds": self.stds.tolist()}

    @classmethod
    def from_state(cls, s):
        return cls(np.array(s["medians"]), np.array(s["means"]), np.array(s["stds"]))


class BaseModel:
    model_type = "base"
    tasks = ("classification", "regression")
    default_params: dict = {}

    def __init__(self, task: str, params: dict | None = None, columns=None, classes=None):
        if task not in self.tasks:
            raise ValueError(f"{self.model_type} supports {self.tasks}, not {task}")
        unknown = set(params or {}) - set(self.default_params)
        if unknown:
            raise ValueError(f"unknown parameters for {self.model_type}: {sorted(unknown)}")
        self.task, self.params = task, {**self.default_params, **(params or {})}
        self.columns, self.classes = list(columns or []), classes
        self.pre = None

    # -- interface ------------------------------------------------------------------
    def fit(self, X, y):
        raise NotImplementedError

    def predict(self, X):
        raise NotImplementedError

    def predict_proba(self, X):
        raise NotImplementedError(f"{self.model_type} has no probabilities")

    def interval(self, X):
        return None

    def contributions(self, X):
        return None

    def importances(self):
        return None

    def metadata(self) -> dict:
        return {"model_type": self.model_type, "task": self.task, "params": self.params, "classes": self.classes,
                "columns": self.columns}

    def to_state(self) -> dict:
        raise NotImplementedError

    def load_state(self, state: dict):
        raise NotImplementedError


class LogisticModel(BaseModel):
    model_type = "logistic_regression"
    tasks = ("classification",)
    default_params = {"l2": 1.0, "learning_rate": 0.1, "iterations": 500}

    def fit(self, X, y):
        y = [str(v) for v in y]
        present = set(y)
        wanted = [str(c) for c in (self.classes or [])]
        self.classes = [c for c in wanted if c in present] or sorted(present)
        self.pre = Preprocessor().fit(X)
        Z = self.pre.transform(X)
        n, p = Z.shape
        k = len(self.classes)
        idx = {c: i for i, c in enumerate(self.classes)}
        Y = np.zeros((n, k)); Y[np.arange(n), [idx[v] for v in y]] = 1
        W, b = np.zeros((p, k)), np.zeros(k)
        lr, l2 = float(self.params["learning_rate"]), float(self.params["l2"])
        for _ in range(int(self.params["iterations"])):
            P = self._softmax(Z @ W + b)
            G = P - Y
            W -= lr * (Z.T @ G / n + l2 * W / n)
            b -= lr * G.mean(axis=0)
        self.W, self.b = W, b
        return self

    @staticmethod
    def _softmax(A):
        A = A - A.max(axis=1, keepdims=True)
        E = np.exp(A)
        return E / E.sum(axis=1, keepdims=True)

    def predict_proba(self, X):
        return self._softmax(self.pre.transform(X) @ self.W + self.b)

    def predict(self, X):
        return [self.classes[i] for i in self.predict_proba(X).argmax(axis=1)]

    def contributions(self, X):
        """Contribution of each column to the predicted class's logit: coef x standardised value."""
        Z = self.pre.transform(X)
        cls = self.predict_proba(X).argmax(axis=1)
        return np.stack([Z[i] * self.W[:, c] for i, c in enumerate(cls)]) if len(Z) else np.zeros((0, len(self.columns)))

    def importances(self):
        imp = np.abs(self.W).mean(axis=1)
        return dict(zip(self.columns, (imp / imp.sum()).tolist())) if imp.sum() else None

    def to_state(self):
        return {"W": self.W.tolist(), "b": self.b.tolist(), "pre": self.pre.to_state(), "classes": self.classes}

    def load_state(self, s):
        self.W, self.b = np.array(s["W"]), np.array(s["b"])
        self.pre, self.classes = Preprocessor.from_state(s["pre"]), s["classes"]
        return self


class RidgeModel(BaseModel):
    model_type = "linear_regression"
    tasks = ("regression",)
    default_params = {"l2": 1.0}

    def fit(self, X, y):
        y = np.asarray(y, dtype=float)
        self.pre = Preprocessor().fit(X)
        Z = self.pre.transform(X)
        p = Z.shape[1]
        self.b = float(y.mean())
        self.w = np.linalg.solve(Z.T @ Z + float(self.params["l2"]) * np.eye(p), Z.T @ (y - self.b))
        resid = y - (Z @ self.w + self.b)
        self.resid_sd = float(np.std(resid, ddof=1)) if len(y) > 1 else None
        return self

    def predict(self, X):
        return (self.pre.transform(X) @ self.w + self.b).tolist()

    def interval(self, X):
        if self.resid_sd is None:
            return None
        p = np.array(self.predict(X))
        return (p - 1.96 * self.resid_sd).tolist(), (p + 1.96 * self.resid_sd).tolist()

    def contributions(self, X):
        return self.pre.transform(X) * self.w

    def importances(self):
        imp = np.abs(self.w)
        return dict(zip(self.columns, (imp / imp.sum()).tolist())) if imp.sum() else None

    def to_state(self):
        return {"w": self.w.tolist(), "b": self.b, "resid_sd": self.resid_sd, "pre": self.pre.to_state()}

    def load_state(self, s):
        self.w, self.b, self.resid_sd = np.array(s["w"]), s["b"], s["resid_sd"]
        self.pre = Preprocessor.from_state(s["pre"])
        return self


class _PickledAdapter(BaseModel):
    """Wraps a third-party estimator; state is its pickle (base64) + the preprocessor."""
    package = None

    def _make(self):
        raise NotImplementedError

    def _require(self):
        try:
            return __import__(self.package)
        except ImportError as e:
            raise ModelDependencyError(f"{self.model_type} needs the '{self.package}' package, which is not "
                                       f"installed (ATIP does not install it automatically)") from e

    def fit(self, X, y):
        self._require()
        self.pre = Preprocessor().fit(X)
        if self.task == "classification":
            y = [str(v) for v in y]
            self.classes = sorted(set(y))
        self.est = self._make()
        self.est.fit(self.pre.transform(X), y)
        return self

    def predict(self, X):
        out = self.est.predict(self.pre.transform(X))
        return [str(v) for v in out] if self.task == "classification" else [float(v) for v in out]

    def predict_proba(self, X):
        if self.task != "classification":
            return super().predict_proba(X)
        P = self.est.predict_proba(self.pre.transform(X))
        order = [list(map(str, self.est.classes_)).index(c) for c in self.classes]
        return P[:, order]

    def importances(self):
        imp = getattr(self.est, "feature_importances_", None)
        return dict(zip(self.columns, [float(v) for v in imp])) if imp is not None else None

    def to_state(self):
        return {"pickle": base64.b64encode(pickle.dumps(self.est)).decode(), "pre": self.pre.to_state(),
                "classes": self.classes}

    def load_state(self, s):
        self._require()
        self.est = pickle.loads(base64.b64decode(s["pickle"]))     # only ATIP's own hash-checked artifacts
        self.pre, self.classes = Preprocessor.from_state(s["pre"]), s["classes"]
        return self


class RandomForestAdapter(_PickledAdapter):
    model_type, package = "random_forest", "sklearn"
    default_params = {"n_estimators": 200, "max_depth": 6, "min_samples_leaf": 20, "random_state": 7}

    def _make(self):
        from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
        return (RandomForestClassifier if self.task == "classification" else RandomForestRegressor)(**self.params)


class GradientBoostingAdapter(_PickledAdapter):
    model_type, package = "gradient_boosting", "sklearn"
    default_params = {"n_estimators": 200, "max_depth": 3, "learning_rate": 0.05, "random_state": 7}

    def _make(self):
        from sklearn.ensemble import GradientBoostingClassifier, GradientBoostingRegressor
        return (GradientBoostingClassifier if self.task == "classification" else GradientBoostingRegressor)(**self.params)


class XGBoostAdapter(_PickledAdapter):
    model_type, package = "xgboost", "xgboost"
    default_params = {"n_estimators": 300, "max_depth": 4, "learning_rate": 0.05, "random_state": 7}

    def fit(self, X, y):
        if self.task == "classification":          # xgboost wants integer class labels
            self._require()
            self.pre = Preprocessor().fit(X)
            ys = [str(v) for v in y]
            self.classes = sorted(set(ys))
            import xgboost
            self.est = xgboost.XGBClassifier(**self.params)
            self.est.fit(self.pre.transform(X), [self.classes.index(v) for v in ys])
            self.est.classes_ = np.array(self.classes)
            return self
        return super().fit(X, y)

    def _make(self):
        import xgboost
        return xgboost.XGBRegressor(**self.params)


class LightGBMAdapter(_PickledAdapter):
    model_type, package = "lightgbm", "lightgbm"
    default_params = {"n_estimators": 300, "num_leaves": 31, "learning_rate": 0.05, "random_state": 7}

    def _make(self):
        import lightgbm
        return (lightgbm.LGBMClassifier if self.task == "classification" else lightgbm.LGBMRegressor)(**self.params)


class NeuralNetworkAdapter(BaseModel):
    model_type = "neural_network"

    def fit(self, X, y):
        raise ModelDependencyError("neural_network is a placeholder for a future wave; no implementation in W5")


MODEL_TYPES = {c.model_type: c for c in (LogisticModel, RidgeModel, RandomForestAdapter, GradientBoostingAdapter,
                                         XGBoostAdapter, LightGBMAdapter, NeuralNetworkAdapter)}


def make_model(model_type: str, task: str, params=None, columns=None, classes=None) -> BaseModel:
    if model_type not in MODEL_TYPES:
        raise ValueError(f"model_type must be one of {sorted(MODEL_TYPES)}")
    return MODEL_TYPES[model_type](task, params, columns, classes)


def availability() -> dict:
    out = {}
    for t, c in MODEL_TYPES.items():
        pkg = getattr(c, "package", None)
        if t == "neural_network":
            out[t] = "placeholder (future wave)"
        elif not pkg:
            out[t] = "available (numpy)"
        else:
            try:
                __import__(pkg); out[t] = f"available ({pkg})"
            except ImportError:
                out[t] = f"needs '{pkg}' (not installed)"
    return out


def metrics(task, y_true, y_pred, proba=None, classes=None) -> dict:
    """Descriptive metrics recorded with a training run. Not a validation claim."""
    if not len(y_true):
        return {"rows": 0}
    if task == "classification":
        yt, yp = [str(v) for v in y_true], [str(v) for v in y_pred]
        acc = sum(a == b for a, b in zip(yt, yp)) / len(yt)
        out = {"rows": len(yt), "accuracy": round(acc, 4)}
        base = max(yt.count(c) for c in set(yt)) / len(yt)
        out["majority_class_rate"] = round(base, 4)
        if proba is not None and classes:
            idx = {c: i for i, c in enumerate(classes)}
            ll = -np.mean([math.log(max(1e-12, proba[i][idx[v]])) if v in idx else math.log(1e-12)
                           for i, v in enumerate(yt)])
            out["log_loss"] = round(float(ll), 4)
        return out
    yt, yp = np.asarray(y_true, float), np.asarray(y_pred, float)
    rmse = float(np.sqrt(np.mean((yt - yp) ** 2)))
    ic = float(np.corrcoef(yt, yp)[0, 1]) if len(yt) > 2 and np.std(yp) > 0 and np.std(yt) > 0 else None
    return {"rows": len(yt), "rmse": round(rmse, 6), "mae": round(float(np.mean(np.abs(yt - yp))), 6),
            "correlation": None if ic is None else round(ic, 4)}
