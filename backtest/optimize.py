"""
Parameter optimisation (W23, BT-04).

    optimize(request, space, method="grid"|"random"|"adaptive"|"bayes", select_by="sharpe",
             max_trials=60, seed=42, min_trades=10) -> parent run_id + trials

SPACE: {param: spec} with spec one of
    {"values": [..]}                      explicit candidates
    {"min": a, "max": b, "step": s}       a numeric grid (inclusive)
    {"min": a, "max": b}                  continuous (random / adaptive / bayes only); integers
                                          when both bounds are integers
Parameters not in the space keep the request's params (or the strategy defaults).

METHODS
    grid       every combination (refused above max_trials -- no silent truncation)
    random     max_trials draws, seeded (the same seed gives the same trials)
    adaptive   half the budget random, the rest local search: each step perturbs the best
               set so far by one grid step (or 10% of the range) in one parameter; a
               cheap, transparent coarse-to-fine search -- not Bayesian optimisation
    bayes      Bayesian optimisation by a Tree-structured Parzen Estimator (Bergstra,
               Bardenet, Bengio & Kegl 2011, "Algorithms for hyper-parameter optimization"),
               numpy only:
                 start    the first max(5, max_trials // 5) trials (at most max_trials) are the
                          random method's draws for the same seed
                 split    then, before each trial, the trials so far are split: GOOD = the best
                          ceil(25%) of them by the select_by score (at most 25; ties -> the
                          earlier trial), BAD = the rest -- a trial with no score (failed, or
                          under min_trades) is always BAD
                 model    per parameter, independently, a density of GOOD, l(x), and of BAD,
                          g(x): numeric parameters a Parzen mixture -- one Gaussian per trial
                          plus a prior N(mid, range), equal weights, each bandwidth the larger
                          gap to its sorted neighbours clipped to [range / min(100, n + 1),
                          range], every component truncated to the bounds; categorical ones
                          (non-numeric values) (count + 1/k) / (n + 1)
                 choose   24 candidates drawn from l (rejection-sampled inside the bounds) and
                          the one maximising l(x) / g(x) -- the expected-improvement criterion
                          of the paper -- that has not run yet is the next trial
               Parameter kinds: a {"values"} list of numbers or a {min, max, step} grid is
               ordinal (the index of the sorted values, -0.5 .. k - 0.5, rounded; each value's
               probability is its unit bin's mass), other values are categorical, integer
               bounds give integers (bins [v - 0.5, v + 0.5]), other bounds a continuous
               value (rounded to 6 decimals like random). The same seed gives the same
               trials. A space whose every combination has run ends the search early.
Every trial is an ordinary backtest run (kind opt_trial) under one parent run (kind
optimization), so each can be inspected, and the whole search is reproducible from
the parent's snapshot. Whatever the method, every trial that ran counts in the deflated
Sharpe's number of trials.

OUT-OF-SAMPLE PROTECTION: optimisation is refused on the test window (period_label
"test" / allow_test). Optimise on research (or validation), then evaluate the chosen
set once on test -- see backtest/study.py.

OVERFITTING
    min_trades     a trial with fewer closed trades scores None (a Sharpe from 3 trades
                   is noise)
    deflated_sharpe  Bailey & Lopez de Prado's Deflated Sharpe Ratio of the best trial:
                   the probability its Sharpe exceeds what the best of N trials would
                   show by luck, given the trials' Sharpe dispersion and the best
                   trial's return skew / kurtosis. Below 0.95 -> overfit_warning.
    also reported: best vs median trial metric, share of trials with a positive metric.
"""

from __future__ import annotations

import itertools
import json
import math
import random
from statistics import NormalDist

import numpy as np

from backtest import service, store
from db.schema import get_connection

METHODS = ("grid", "random", "adaptive", "bayes")
MAX_TRIALS = 400
_N = NormalDist()
TPE_GAMMA = 0.25            # the best quarter of the trials so far model l(x) ...
TPE_GOOD_MAX = 25           # ... at most 25 of them
TPE_CANDIDATES = 24         # draws from l(x) per trial


def _grid_values(spec):
    if "values" in spec:
        v = list(spec["values"])
        if not v:
            raise ValueError("values must not be empty")
        return v
    lo, hi, st = spec.get("min"), spec.get("max"), spec.get("step")
    if lo is None or hi is None or st is None:
        raise ValueError("a grid parameter needs values, or min / max / step")
    if st <= 0 or hi < lo:
        raise ValueError("step must be > 0 and max >= min")
    out, x, n = [], lo, 0
    while x <= hi + 1e-12 and n < 10000:
        out.append(round(x, 10) if isinstance(st, float) or isinstance(lo, float) else int(x))
        x += st
        n += 1
    return out


def validate_space(space: dict) -> dict:
    if not isinstance(space, dict) or not space:
        raise ValueError("space must be a non-empty object {param: spec}")
    for k, spec in space.items():
        if not isinstance(spec, dict):
            raise ValueError(f"space.{k} must be an object")
        if "values" in spec:
            _grid_values(spec)
        elif spec.get("min") is None or spec.get("max") is None or spec["max"] < spec["min"]:
            raise ValueError(f"space.{k}: need values, or min <= max")
    return space


def _sample(spec, rng):
    if "values" in spec:
        return rng.choice(list(spec["values"]))
    if spec.get("step"):
        return rng.choice(_grid_values(spec))
    lo, hi = spec["min"], spec["max"]
    if isinstance(lo, int) and isinstance(hi, int):
        return rng.randint(lo, hi)
    return round(rng.uniform(lo, hi), 6)


def _neighbour(spec, value, rng):
    if "values" in spec or spec.get("step"):
        vals = _grid_values(spec)
        try:
            i = vals.index(value)
        except ValueError:
            return rng.choice(vals)
        return vals[max(0, min(len(vals) - 1, i + rng.choice((-1, 1))))]
    lo, hi = spec["min"], spec["max"]
    d = (hi - lo) * 0.1 * rng.choice((-1, 1))
    v = min(hi, max(lo, value + d))
    return int(round(v)) if isinstance(lo, int) and isinstance(hi, int) else round(v, 6)


# ── bayes: Tree-structured Parzen Estimator ────────────────────────────────

def tpe_startup(max_trials: int) -> int:
    """Random trials before the model takes over."""
    return min(int(max_trials), max(5, int(max_trials) // 5))


def _sf(x):
    """P(N(0, 1) > x), numpy, relative error < 1.2e-7 in either tail (Numerical Recipes erfc)."""
    z = np.abs(x) / math.sqrt(2)
    t = 1.0 / (1.0 + 0.5 * z)
    r = t * np.exp(-z * z - 1.26551223 + t * (1.00002368 + t * (0.37409196 + t * (0.09678418 + t * (
        -0.18628806 + t * (0.27886807 + t * (-1.13520398 + t * (1.48851587 + t * (-0.82215223 + t * 0.17087277)))))))))
    return np.where(x >= 0, 0.5 * r, 1.0 - 0.5 * r)


def _tails(x):
    """(P(N(0, 1) <= x), P(N(0, 1) > x)): each point's two tails, the small one computed directly."""
    s = _sf(np.abs(x))
    return np.where(x < 0, s, 1.0 - s), np.where(x < 0, 1.0 - s, s)


def _mass(a, b):
    """P(a < N(0, 1) <= b) for a <= b, as a difference of upper tails when a >= 0, else of lower
    tails (no cancellation; adjacent bins telescope to the whole interval's mass); a bin far
    narrower than the unit takes its width x the density at its middle."""
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    (lo_a, up_a), (lo_b, up_b) = _tails(a), _tails(b)
    cdf = np.where(a >= 0, up_a - up_b, lo_b - lo_a)
    mid = (a + b) / 2
    narrow = (b - a) * np.exp(-mid * mid / 2) / math.sqrt(2 * math.pi)
    return np.maximum(np.where(b - a < 1e-3, narrow, cdf), 0.0)


class _Dim:
    """One parameter of the space as the estimator sees it (optimize's module docstring):
    cat (a category index), ord (an index into the sorted numeric values), int, float."""

    def __init__(self, spec: dict):
        if "values" in spec or spec.get("step"):
            vals = _grid_values(spec)
            if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
                self.kind, self.values = "ord", sorted(set(vals))
                self.lo, self.hi = -0.5, len(self.values) - 0.5
            else:
                self.kind, self.values = "cat", []
                for v in vals:
                    if v not in self.values:
                        self.values.append(v)
            return
        lo, hi = spec["min"], spec["max"]
        if isinstance(lo, int) and isinstance(hi, int) and not isinstance(lo, bool) and not isinstance(hi, bool):
            self.kind, self.vmin, self.vmax = "int", lo, hi
            self.lo, self.hi = lo - 0.5, hi + 0.5
        elif float(lo) == float(hi):
            self.kind, self.value = "const", lo                     # min == max: nothing to model
        else:
            self.kind, self.lo, self.hi = "float", float(lo), float(hi)

    def size(self):
        """How many distinct values (None: continuous)."""
        if self.kind in ("cat", "ord"):
            return len(self.values)
        if self.kind == "int":
            return self.vmax - self.vmin + 1
        return 1 if self.kind == "const" else None

    def encode(self, value) -> float:
        if self.kind == "const":
            return 0.0
        if self.kind == "cat":
            return float(self.values.index(value))
        if self.kind == "ord":
            if value in self.values:
                return float(self.values.index(value))
            return float(min(range(len(self.values)), key=lambda i: abs(self.values[i] - value)))
        return float(value)

    def decode(self, x: float):
        if self.kind == "const":
            return self.value
        if self.kind == "cat":
            return self.values[int(x)]
        if self.kind == "ord":
            return self.values[int(min(len(self.values) - 1, max(0, math.floor(x + 0.5))))]
        if self.kind == "int":
            return int(min(self.vmax, max(self.vmin, math.floor(x + 0.5))))
        return round(float(min(self.hi, max(self.lo, x))), 6)

    # numeric: an adaptive Parzen estimator; categorical: smoothed frequencies
    def model(self, xs: np.ndarray):
        if self.kind == "const":
            return None
        if self.kind == "cat":
            k = len(self.values)
            counts = np.bincount(xs.astype(int), minlength=k) if len(xs) else np.zeros(k)
            return (counts + 1.0 / k) / (len(xs) + 1.0)
        width = self.hi - self.lo
        mus = np.append(xs, (self.lo + self.hi) / 2)               # the trials, then the prior
        order = np.argsort(mus, kind="stable")
        srt = mus[order]
        gaps = np.maximum(np.diff(np.concatenate([[self.lo], srt])), np.diff(np.concatenate([srt, [self.hi]])))
        sig = np.empty_like(mus)
        sig[order] = gaps
        sig = np.clip(sig, width / min(100.0, len(mus)), width)
        sig[-1] = width
        z = _mass((self.lo - mus) / sig, (self.hi - mus) / sig)     # truncation to the bounds
        return mus, sig, np.full(len(mus), 1.0 / len(mus)) / np.maximum(z, 1e-300)

    def draw(self, model, rng, n: int) -> np.ndarray:
        if self.kind == "const":
            return np.zeros(n)
        if self.kind == "cat":
            return rng.choice(len(self.values), size=n, p=model).astype(float)
        mus, sig, _w = model
        comp = rng.integers(0, len(mus), size=n)                    # equal weights
        x = rng.normal(mus[comp], sig[comp])
        for _ in range(25):                                         # truncated normals, by rejection
            out = (x < self.lo) | (x > self.hi)
            if not out.any():
                break
            x[out] = rng.normal(mus[comp[out]], sig[comp[out]])
        x = np.clip(x, self.lo, self.hi)
        if self.kind in ("ord", "int"):
            x = np.floor(x + 0.5)                                    # the value whose bin it fell in
            x = np.clip(x, self.lo + 0.5, self.hi - 0.5)
        return x

    def log_density(self, model, x: np.ndarray) -> np.ndarray:
        if self.kind == "const":
            return np.zeros(len(x))
        if self.kind == "cat":
            return np.log(model[x.astype(int)])
        mus, sig, wz = model
        if self.kind == "float":
            u = (x[:, None] - mus[None, :]) / sig[None, :]
            p = (np.exp(-u * u / 2) / (math.sqrt(2 * math.pi) * sig[None, :]) * wz[None, :]).sum(axis=1)
        else:                                                       # the mass of the value's unit bin
            a = (np.maximum(x - 0.5, self.lo)[:, None] - mus[None, :]) / sig[None, :]
            b = (np.minimum(x + 0.5, self.hi)[:, None] - mus[None, :]) / sig[None, :]
            p = (_mass(a, b) * wz[None, :]).sum(axis=1)
        return np.log(np.maximum(p, 1e-300))


class TPE:
    """Suggests the next parameter set from the trials so far (optimize's module docstring)."""

    def __init__(self, space: dict, seed: int):
        self.keys = list(space)
        self.dims = [_Dim(space[k]) for k in self.keys]
        self.rng = np.random.default_rng(seed)
        sizes = [d.size() for d in self.dims]
        self.size = None if any(s is None for s in sizes) else math.prod(sizes)

    def split(self, trials: list) -> tuple:
        """(GOOD, BAD) trials: the best ceil(gamma x n) scored (at most TPE_GOOD_MAX), the rest."""
        scored = sorted((t for t in trials if t["score"] is not None), key=lambda t: (-t["score"], t["index"]))
        n_good = min(len(scored), TPE_GOOD_MAX, max(1, math.ceil(TPE_GAMMA * len(trials))))
        good = scored[:n_good]
        ids = {t["index"] for t in good}
        return good, [t for t in trials if t["index"] not in ids]

    def suggest(self, trials: list, is_new) -> dict | None:
        """The candidate with the largest l(x) / g(x) among TPE_CANDIDATES draws from l that
        is_new(params) accepts; up to 4 rounds of draws, then None."""
        good, bad = self.split(trials)
        if not good:
            return None
        models = []
        for k, d in zip(self.keys, self.dims):
            xg = np.array([d.encode(t["params"][k]) for t in good])
            xb = np.array([d.encode(t["params"][k]) for t in bad])
            models.append((d.model(xg), d.model(xb)))
        for _ in range(4):
            xs = [d.draw(lm, self.rng, TPE_CANDIDATES) for d, (lm, _gm) in zip(self.dims, models)]
            score = sum(d.log_density(lm, x) - d.log_density(gm, x) for d, (lm, gm), x in zip(self.dims, models, xs))
            for i in np.argsort(-score, kind="stable"):
                params = {k: d.decode(x[i]) for k, d, x in zip(self.keys, self.dims, xs)}
                if is_new(params):
                    return params
        return None


def metric_of(res: dict, key: str, min_trades: int):
    m = res.get("metrics") or {}
    if res.get("status") != "COMPLETED" or (m.get("trades") or 0) < min_trades:
        return None
    v = m.get(key)
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


def _moments(xs):
    n = len(xs)
    if n < 3:
        return 0.0, 3.0
    mu = sum(xs) / n
    sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / n)
    if not sd:
        return 0.0, 3.0
    skew = sum(((x - mu) / sd) ** 3 for x in xs) / n
    kurt = sum(((x - mu) / sd) ** 4 for x in xs) / n
    return skew, kurt


def deflated_sharpe(best_daily_returns: list, trial_sharpes_annual: list) -> dict:
    """DSR (Bailey & Lopez de Prado 2014) on per-session Sharpe ratios."""
    r = [x for x in best_daily_returns if x is not None]
    n_tr = len([s for s in trial_sharpes_annual if s is not None])
    if len(r) < 30 or n_tr < 2:
        return {"dsr": None, "note": "needs >= 30 sessions and >= 2 scored trials"}
    mu = sum(r) / len(r)
    sd = math.sqrt(sum((x - mu) ** 2 for x in r) / (len(r) - 1))
    if not sd:
        return {"dsr": None, "note": "no variance"}
    sr = mu / sd                                                    # per session
    srs = [s / math.sqrt(252) for s in trial_sharpes_annual if s is not None]
    m = sum(srs) / len(srs)
    var = sum((s - m) ** 2 for s in srs) / max(1, len(srs) - 1)
    g = 0.5772156649
    sr0 = math.sqrt(var) * ((1 - g) * _N.inv_cdf(1 - 1 / n_tr) + g * _N.inv_cdf(1 - 1 / (n_tr * math.e)))
    skew, kurt = _moments(r)
    den = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if den <= 0:
        return {"dsr": None, "note": "non-positive variance term"}
    z = (sr - sr0) * math.sqrt(len(r) - 1) / math.sqrt(den)
    return {"dsr": round(_N.cdf(z), 4), "sharpe_per_session": round(sr, 5), "expected_max_sharpe_per_session":
            round(sr0, 5), "trials": n_tr, "skew": round(skew, 3), "kurtosis": round(kurt, 3)}


def _parent(request, kind, meta):
    snap = service.resolve_config(request)
    snap[kind] = meta
    conn = get_connection()
    try:
        pid = store.create_run(conn, snap, kind=kind)
        store.mark_running(conn, pid)
    finally:
        conn.close()
    return pid, snap


def _trial(base, params, kind, parent, idx):
    rid = service.create({**base, "params": params}, kind=kind, parent_run_id=parent, window_index=idx)
    res = service.execute(rid)
    return rid, res


def _finish(pid, status, summary, metrics=None, bias=None):
    conn = get_connection()
    try:
        store.save_summary(conn, pid, status, summary, metrics=metrics, bias=bias)
    finally:
        conn.close()


def _returns(run_id):
    conn = get_connection()
    try:
        return [r["daily_return"] for r in store.get_rows(conn, "backtest_equity", run_id)][1:]
    finally:
        conn.close()


def prepare(request: dict, space: dict, method: str, max_trials: int):
    if method not in METHODS:
        raise ValueError(f"method must be one of {METHODS}")
    if request.get("period_label") == "test" or request.get("allow_test"):
        raise ValueError("optimisation on the test window is refused: optimise on research / validation, then "
                         "evaluate the chosen parameters once on test")
    if not 1 <= int(max_trials) <= MAX_TRIALS:
        raise ValueError(f"max_trials must be 1..{MAX_TRIALS}")
    validate_space(space)
    if method == "grid":
        for k, s in space.items():
            if "values" not in s and not s.get("step"):
                raise ValueError(f"grid needs values or a step for {k}")
        n = 1
        for s in space.values():
            n *= len(_grid_values(s))
        if n > max_trials:
            raise ValueError(f"grid has {n} combinations > max_trials {max_trials}: narrow the space, raise "
                             f"max_trials, or use random / adaptive / bayes")
    service.resolve_config(request)                 # validates the base request


def optimize(request: dict, space: dict, method: str = "grid", select_by: str = "sharpe", max_trials: int = 60,
             seed: int = 42, min_trades: int = 10) -> dict:
    from backtest.walkforward import SELECT_BY
    if select_by not in SELECT_BY:
        raise ValueError(f"select_by must be one of {SELECT_BY}")
    prepare(request, space, method, max_trials)
    base_params = dict(request.get("params") or {})
    base = {k: v for k, v in request.items() if k != "params"}
    pid, _ = _parent(request, "optimization", {"space": space, "method": method, "select_by": select_by,
                                               "max_trials": max_trials, "seed": seed, "min_trades": min_trades})
    rng = random.Random(seed)
    trials, seen = [], set()

    def run(params):
        key = json.dumps(params, sort_keys=True)
        if key in seen:
            return None
        seen.add(key)
        rid, res = _trial(base, params, "opt_trial", pid, len(trials))
        t = {"index": len(trials), "params": params, "run_id": rid, "status": res["status"],
             "score": metric_of(res, select_by, min_trades), "metrics": {k: (res.get("metrics") or {}).get(k) for k in
                                                                         ("sharpe", "total_return", "max_drawdown",
                                                                          "trades", "profit_factor", "cagr")}}
        trials.append(t)
        return t
    try:
        if method == "grid":
            keys = list(space)
            for combo in itertools.product(*[_grid_values(space[k]) for k in keys]):
                run({**base_params, **dict(zip(keys, combo))})
        else:
            budget = int(max_trials)
            n_random = {"random": budget, "bayes": tpe_startup(budget)}.get(method, max(1, budget // 2))
            attempts = 0
            while len(trials) < n_random and attempts < n_random * 20:
                attempts += 1
                run({**base_params, **{k: _sample(s, rng) for k, s in space.items()}})
            n_start = len(trials)
            attempts = 0
            while method == "adaptive" and len(trials) < budget and attempts < budget * 20:
                attempts += 1
                scored = [t for t in trials if t["score"] is not None]
                if not scored:
                    run({**base_params, **{k: _sample(s, rng) for k, s in space.items()}})
                    continue
                best = max(scored, key=lambda t: t["score"])
                k = rng.choice(list(space))
                run({**best["params"], k: _neighbour(space[k], best["params"].get(k), rng)})
            if method == "bayes":
                tpe = TPE(space, seed)
                attempts = 0
                while len(trials) < budget and attempts < budget * 20 and (tpe.size is None or len(trials) < tpe.size):
                    attempts += 1
                    params = tpe.suggest(trials, lambda p: json.dumps({**base_params, **p}, sort_keys=True) not in seen)
                    run({**base_params, **(params or {k: _sample(s, rng) for k, s in space.items()})})
        scored = sorted([t for t in trials if t["score"] is not None], key=lambda t: -t["score"])
        best = scored[0] if scored else None
        vals = [t["score"] for t in scored]
        diag = {"trials": len(trials), "scored": len(scored), "positive_share": round(sum(v > 0 for v in vals) /
                                                                                       len(vals), 3) if vals else None,
                "best": best["score"] if best else None,
                "median": sorted(vals)[len(vals) // 2] if vals else None}
        if best:
            sharpes = [t["metrics"]["sharpe"] for t in trials if t["metrics"].get("sharpe") is not None]
            diag["deflated_sharpe"] = deflated_sharpe(_returns(best["run_id"]), sharpes)
            d = diag["deflated_sharpe"].get("dsr")
            diag["overfit_warning"] = (d is not None and d < 0.95) or len(scored) < 3
        summary = {"method": method, "select_by": select_by, "space": space, "best": best,
                   "trials": sorted(trials, key=lambda t: (t["score"] is None, -(t["score"] or 0))),
                   "diagnostics": diag}
        if method == "bayes":
            summary["search"] = {"estimator": "TPE (Bergstra et al. 2011)", "startup_trials": n_start,
                                 "gamma": TPE_GAMMA, "good_max": TPE_GOOD_MAX, "candidates": TPE_CANDIDATES,
                                 "seed": seed}
        _finish(pid, "COMPLETED" if best else "FAILED", summary, metrics=best["metrics"] if best else None,
                bias={"selection": f"best {select_by} over {len(trials)} trials on {request.get('period_label') or 'full'}"
                                   f" window; in-sample -- confirm out of sample (walk-forward / test)"})
        return {"run_id": pid, "status": "COMPLETED" if best else "FAILED", **summary}
    except Exception as e:
        conn = get_connection()
        try:
            store.mark_failed(conn, pid, f"{type(e).__name__}: {e}")
        finally:
            conn.close()
        raise
