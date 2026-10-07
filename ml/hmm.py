"""
Market states from an unsupervised Gaussian hidden Markov model (W39, ML-17). numpy only.

Market Health (scores/engine.py compute_mh) labels each session from a rule-based score and
ml/regime.py MLRegime is a supervised model of that label; ml/unsupervised.py clusters STOCKS.
Nothing learned the market's own states. This does:

GaussianHMM(n_states=3, max_iter=200, tol=1e-4, return_col=0)
    2-4 hidden states, diagonal-covariance Gaussian emissions, fitted by Baum-Welch EM with a
    log-space forward-backward (no underflow on long series). Deterministic: the columns are
    standardised, the observations sorted on their first principal component and cut into
    n_states quantile groups whose means seed a k-means (ml.unsupervised._kmeans); there is no
    random number anywhere, so the same input gives the same fit. Each iteration's
    log-likelihood is kept in history_; EM never lowers it (the variance floor is a constrained
    M-step, so that holds with it too). After fitting, the states are ordered best first by
    mean / stdev of the return column (decimal daily returns) and labelled
        {CALM|VOLATILE}_{BULL|NEUTRAL|BEAR}
    CALM / VOLATILE  the state's return stdev at or below / above the geometric middle of the
                     lowest and highest state stdevs
    BULL / BEAR      annualised mean return above +NEUTRAL_BAND / below -NEUTRAL_BAND, else NEUTRAL
    (a repeated label gets _2, _3 in that order).
        predict(X)   Viterbi path              filtered(X)  P(state_t | x_1..x_t): forward only
        smoothed(X)  P(state_t | x_1..x_T)     score(X)     log-likelihood
    filtered() is what a decision at t may use; smoothed() looks ahead within X.

market_features(conn, end=None, vix=True, breadth=False)
    the daily market observations, each known at that session's close (sessions = NIFTY50 rows):
        ret      NIFTY50 log return (prices_daily)
        rv20     realised volatility of the last 20 returns, annualised %
        vix      log India VIX close (prices_daily INDIAVIX, else market_health.vix_level)
        breadth  market_health.pct_advancing (opt-in)
    vix / breadth are forward-filled (past values only) and used in a fit only when stored on
    >= COVERAGE of that fit's sessions.

MarketStates(conn, end=None, n_states=3, lookback=750, refit_every=21, min_obs=250, ...)
    walk-forward and point in time: .at(as_of) uses the model fitted on the `lookback` sessions
    up to the latest refit anchor <= as_of (every `refit_every` sessions), and filters the state
    with observations <= as_of only. Fits are cached; nothing after as_of is ever read for it.
market_regime(conn, as_of=None, ...)   one fit ending at as_of, with the states described.

HMM_TO_REGIME maps a state label onto the Market Health taxonomy (STRONG_BULL .. HIGH_RISK) for
strategies: ml/regime.py HMMRegime, config ml.regime_source = "hmm" (opt-in).
"""

from __future__ import annotations

import math
from bisect import bisect_right
from datetime import date

import numpy as np

STICKY = 0.9                    # initial probability of staying in a state
NEUTRAL_BAND = 0.05             # |annualised mean return| below this: NEUTRAL
COVERAGE = 0.95                 # an optional feature needs this share of a fit's sessions
RV_WINDOW = 20
HMM_TO_REGIME = {"CALM_BULL": "BULL", "CALM_NEUTRAL": "NEUTRAL", "CALM_BEAR": "BEAR",
                 "VOLATILE_BULL": "NEUTRAL", "VOLATILE_NEUTRAL": "BEAR", "VOLATILE_BEAR": "HIGH_RISK"}


def _lse(a):
    m = np.max(a)
    return float(m + np.log(np.sum(np.exp(a - m)))) if np.isfinite(m) else -math.inf


class GaussianHMM:
    def __init__(self, n_states: int = 3, max_iter: int = 200, tol: float = 1e-4, return_col: int = 0,
                 min_var: float = 1e-3):
        if not 2 <= int(n_states) <= 4:
            raise ValueError("n_states must be 2..4")
        self.n_states, self.max_iter, self.tol = int(n_states), int(max_iter), float(tol)
        self.return_col, self.min_var = int(return_col), float(min_var)

    # ── data ──
    @staticmethod
    def _2d(X):
        X = np.asarray(X, dtype=float)
        X = X[:, None] if X.ndim == 1 else X
        if X.ndim != 2 or not np.all(np.isfinite(X)):
            raise ValueError("X must be a finite (sessions x features) array")
        return X

    def _z(self, X):
        return (self._2d(X) - self.mu_) / self.sd_

    def _logB(self, Z):
        d = Z.shape[1]
        q = (((Z[:, None, :] - self._m[None]) ** 2) / self._v[None]).sum(2)
        return -0.5 * (d * math.log(2 * math.pi) + np.log(self._v).sum(1)[None, :] + q)

    # ── forward-backward in log space ──
    def _forward(self, logB):
        la = np.empty_like(logB)
        with np.errstate(divide="ignore"):
            la[0] = np.log(self._pi) + logB[0]
            for t in range(1, len(logB)):
                m = la[t - 1].max()
                la[t] = logB[t] + m + np.log(np.exp(la[t - 1] - m) @ self._A)
        return la

    def _backward(self, logB):
        lb = np.zeros_like(logB)
        with np.errstate(divide="ignore"):
            for t in range(len(logB) - 2, -1, -1):
                v = logB[t + 1] + lb[t + 1]
                m = v.max()
                lb[t] = m + np.log(self._A @ np.exp(v - m))
        return lb

    def _estep(self, Z):
        logB = self._logB(Z)
        la, lb = self._forward(logB), self._backward(logB)
        ll = _lse(la[-1])
        g = np.exp(la + lb - ll)
        g /= g.sum(1, keepdims=True)
        with np.errstate(divide="ignore"):
            logA = np.log(self._A)
        xi = np.exp(la[:-1, :, None] + logA[None] + (logB[1:] + lb[1:])[:, None, :] - ll).sum(0)
        return ll, g, xi

    def _mstep(self, Z, g, xi):
        self._pi = g[0] / g[0].sum()
        rows = xi.sum(1, keepdims=True)
        self._A = np.where(rows > 0, xi / np.where(rows > 0, rows, 1.0), self._A)
        w = g.sum(0)
        for k in range(self.n_states):
            if w[k] < 1e-10:                      # an empty state keeps its parameters
                continue
            m = g[:, k] @ Z / w[k]
            self._m[k] = m
            self._v[k] = np.maximum(g[:, k] @ (Z - m) ** 2 / w[k], self.min_var)

    def _init(self, Z):
        from ml.unsupervised import _kmeans
        K = self.n_states
        if Z.shape[1] == 1:
            s = Z[:, 0]
        else:
            v = np.linalg.svd(Z - Z.mean(0), full_matrices=False)[2][0]
            s = Z @ (v * np.sign(v[np.argmax(np.abs(v))]))
        C0 = np.array([Z[g].mean(0) for g in np.array_split(np.argsort(s, kind="stable"), K)])
        lab, C, _ = _kmeans(Z, K, 0, init=C0)
        self._m, self._v = C0.copy(), np.ones_like(C0)
        for k in range(K):
            idx = lab == k
            if idx.sum() >= 2:
                self._m[k], self._v[k] = Z[idx].mean(0), np.maximum(Z[idx].var(0), self.min_var)
        self._pi = np.full(K, 1.0 / K)
        self._A = np.full((K, K), (1 - STICKY) / (K - 1))
        np.fill_diagonal(self._A, STICKY)

    # ── fit ──
    def fit(self, X):
        X = self._2d(X)
        if len(X) < 10 * self.n_states:
            raise ValueError(f"{len(X)} observations: too few for {self.n_states} states")
        self.mu_, sd = X.mean(0), X.std(0)
        self.sd_ = np.where(sd > 0, sd, 1.0)
        Z = (X - self.mu_) / self.sd_
        self._init(Z)
        jac = len(X) * float(np.log(self.sd_).sum())          # z-space -> original-units likelihood
        ll, g, xi = self._estep(Z)
        hist, self.converged_ = [ll], False
        for _ in range(self.max_iter):
            self._mstep(Z, g, xi)
            ll, g, xi = self._estep(Z)
            hist.append(ll)
            if hist[-1] - hist[-2] < self.tol:
                self.converged_ = True
                break
        self.history_ = [h - jac for h in hist]
        self.n_iter_ = len(hist) - 1
        self._order(g)
        return self

    def _order(self, g):
        rc = self.return_col
        mean = self._m[:, rc] * self.sd_[rc] + self.mu_[rc]
        vol = np.sqrt(self._v[:, rc]) * self.sd_[rc]
        p = np.argsort(-(mean / vol), kind="stable")       # best mean / stdev first
        self._pi, self._A = self._pi[p], self._A[np.ix_(p, p)]
        self._m, self._v = self._m[p], self._v[p]
        self.occupancy_ = g.mean(0)[p]
        mean, vol = mean[p], vol[p]
        mid = math.sqrt(vol.min() * vol.max())
        labels = []
        for m, s in zip(mean, vol):
            ann = m * 252
            base = ("CALM" if s <= mid * (1 + 1e-12) else "VOLATILE") + "_" + (
                "BULL" if ann > NEUTRAL_BAND else "BEAR" if ann < -NEUTRAL_BAND else "NEUTRAL")
            n = sum(1 for x in labels if x.split("_")[:2] == base.split("_"))
            labels.append(base if not n else f"{base}_{n + 1}")
        self.labels_ = labels
        self.state_return_, self.state_vol_ = mean, vol

    # ── inference (original units) ──
    def score(self, X) -> float:
        Z = self._z(X)
        return _lse(self._forward(self._logB(Z))[-1]) - len(Z) * float(np.log(self.sd_).sum())

    def filtered(self, X):
        la = self._forward(self._logB(self._z(X)))
        p = np.exp(la - la.max(1, keepdims=True))
        return p / p.sum(1, keepdims=True)

    def smoothed(self, X):
        logB = self._logB(self._z(X))
        la, lb = self._forward(logB), self._backward(logB)
        p = np.exp(la + lb - (la + lb).max(1, keepdims=True))
        return p / p.sum(1, keepdims=True)

    def predict(self, X):
        """The Viterbi (most likely) state path."""
        logB = self._logB(self._z(X))
        with np.errstate(divide="ignore"):
            logA, delta = np.log(self._A), np.log(self._pi) + logB[0]
        psi = np.zeros(logB.shape, dtype=int)
        for t in range(1, len(logB)):
            s = delta[:, None] + logA
            psi[t] = s.argmax(0)
            delta = s.max(0) + logB[t]
        path = np.empty(len(logB), dtype=int)
        path[-1] = int(delta.argmax())
        for t in range(len(logB) - 1, 0, -1):
            path[t - 1] = psi[t, path[t]]
        return path

    @property
    def means(self):
        return self._m * self.sd_ + self.mu_

    @property
    def variances(self):
        return self._v * self.sd_ ** 2

    @property
    def transmat(self):
        return self._A.copy()

    def summary(self) -> list:
        out = []
        for k, lab in enumerate(self.labels_):
            stay = float(self._A[k, k])
            out.append({"state": k, "label": lab, "regime": HMM_TO_REGIME.get("_".join(lab.split("_")[:2])),
                        "mean_return_ann_pct": round(float(self.state_return_[k]) * 252 * 100, 2),
                        "vol_ann_pct": round(float(self.state_vol_[k]) * math.sqrt(252) * 100, 2),
                        "share": round(float(self.occupancy_[k]), 4),
                        "expected_duration": round(1 / (1 - stay), 1) if stay < 1 else None,
                        "transitions": [round(float(x), 4) for x in self._A[k]]})
        return out


# ── market data ──
def _series(conn, sql, args):
    out = {}
    for d, v in conn.execute(sql, args):
        if v is not None:
            out[str(d)[:10]] = float(v)
    return out


def market_features(conn, end=None, vix: bool = True, breadth: bool = False) -> dict:
    """{"dates": [...], "columns": {name: array}, "present": {name: bool array}} -- see the module doc."""
    e = [str(end)[:10]] if end else []
    w = " AND date<=?" if end else ""
    rows = conn.execute("SELECT date, close FROM prices_daily WHERE symbol='NIFTY50' AND close>0" + w +
                        " ORDER BY date", e).fetchall()
    if len(rows) < RV_WINDOW + 2:
        return {"dates": [], "columns": {}, "present": {}}
    c = np.array([float(r[1]) for r in rows])
    ret = np.diff(np.log(c))
    rv = np.lib.stride_tricks.sliding_window_view(ret, RV_WINDOW).std(1, ddof=1) * math.sqrt(252) * 100
    dates = [str(r[0])[:10] for r in rows][RV_WINDOW:]
    cols = {"ret": ret[RV_WINDOW - 1:], "rv20": rv}
    present = {"ret": np.ones(len(dates), bool), "rv20": np.ones(len(dates), bool)}
    extra = {}
    if vix:
        v = _series(conn, "SELECT date, vix_level FROM market_health WHERE vix_level>0" + w, e)
        v.update(_series(conn, "SELECT date, close FROM prices_daily WHERE symbol='INDIAVIX' AND close>0" + w, e))
        extra["vix"] = {d: math.log(x) for d, x in v.items()}
    if breadth:
        extra["breadth"] = _series(conn, "SELECT date, pct_advancing FROM market_health WHERE pct_advancing IS NOT "
                                         "NULL" + w, e)
    for name, s in extra.items():
        have = np.array([d in s for d in dates])
        if not have.any():
            continue
        vals, last = np.empty(len(dates)), np.nan
        for i, d in enumerate(dates):                        # forward fill: past values only
            last = s.get(d, last)
            vals[i] = last
        cols[name], present[name] = vals, have
    return {"dates": dates, "columns": cols, "present": present}


class MarketStates:
    """Walk-forward market states over market_features (loaded once, so the connection may close)."""

    def __init__(self, conn, end=None, n_states: int = 3, lookback: int = 750, refit_every: int = 21,
                 min_obs: int = 250, vix: bool = True, breadth: bool = False, max_iter: int = 200):
        if not 2 <= int(n_states) <= 4:
            raise ValueError("n_states must be 2..4")
        self.f = market_features(conn, end, vix, breadth)
        self.n_states, self.lookback, self.refit_every = int(n_states), int(lookback), max(1, int(refit_every))
        self.min_obs, self.max_iter = max(int(min_obs), 10 * int(n_states)), int(max_iter)
        self._fits = {}

    def _cols(self, lo, hi):
        out = ["ret", "rv20"]
        for name in ("vix", "breadth"):
            if name in self.f["columns"]:
                seg = self.f["columns"][name][lo:hi + 1]
                if self.f["present"][name][lo:hi + 1].mean() >= COVERAGE and np.all(np.isfinite(seg)):
                    out.append(name)
        return out

    def _X(self, lo, hi, cols):
        return np.column_stack([self.f["columns"][c][lo:hi + 1] for c in cols])

    def _fit(self, lo, hi):
        if (lo, hi) not in self._fits:
            cols = self._cols(lo, hi)
            m = GaussianHMM(self.n_states, self.max_iter).fit(self._X(lo, hi, cols))
            self._fits[(lo, hi)] = (m, cols)
        return self._fits[(lo, hi)]

    def at(self, as_of, refit_every=None) -> dict:
        dates = self.f["dates"]
        d = str(as_of)[:10]
        pos = bisect_right(dates, d) - 1
        if pos < 0:
            return {"status": "NO_DATA", "as_of": d, "reason": "no NIFTY50 history (prices_daily) on or before as_of"}
        every = max(1, int(refit_every or self.refit_every))
        anchor = pos - pos % every
        lo = max(0, anchor - self.lookback + 1)
        if anchor - lo + 1 < self.min_obs:
            return {"status": "INSUFFICIENT_HISTORY", "as_of": d, "date": dates[pos],
                    "reason": f"{anchor - lo + 1} sessions to fit on < min_obs {self.min_obs}"}
        model, cols = self._fit(lo, anchor)
        X = self._X(lo, pos, cols)
        p = model.filtered(X)[-1]
        k = int(p.argmax())
        lab = model.labels_[k]
        return {"status": "OK", "as_of": d, "date": dates[pos], "state": lab,
                "regime": HMM_TO_REGIME.get("_".join(lab.split("_")[:2])), "confidence": round(float(p[k]), 4),
                "probabilities": {model.labels_[i]: round(float(x), 4) for i, x in enumerate(p)},
                "fit_start": dates[lo], "fit_end": dates[anchor], "fit_sessions": anchor - lo + 1, "features": cols,
                "model": model}


def market_regime(conn, as_of=None, n_states: int = 3, lookback: int = 750, min_obs: int = 250, vix: bool = True,
                  breadth: bool = False) -> dict:
    """One fit on the `lookback` sessions ending at as_of (default: the latest NIFTY50 session)."""
    as_of = as_of or conn.execute("SELECT MAX(date) FROM prices_daily WHERE symbol='NIFTY50'").fetchone()[0]
    if not as_of:
        return {"status": "NO_DATA", "as_of": None, "reason": "no NIFTY50 history (prices_daily)"}
    as_of = as_of if isinstance(as_of, date) else str(as_of)[:10]
    ms = MarketStates(conn, as_of, n_states, lookback, 1, min_obs, vix, breadth)
    r = ms.at(as_of)
    m = r.pop("model", None)
    if m is not None:
        lo = ms.f["dates"].index(r["fit_start"])
        X = ms._X(lo, lo + r["fit_sessions"] - 1, r["features"])
        r.update(states=m.summary(), viterbi_state=m.labels_[int(m.predict(X)[-1])],
                 log_likelihood=round(m.history_[-1], 3), iterations=m.n_iter_, converged=m.converged_,
                 method=f"Gaussian HMM, {m.n_states} states, diagonal covariance, Baum-Welch EM")
    return r
