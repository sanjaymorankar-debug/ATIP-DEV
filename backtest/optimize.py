"""
Parameter optimisation (W23, BT-04).

    optimize(request, space, method="grid"|"random"|"adaptive", select_by="sharpe",
             max_trials=60, seed=42, min_trades=10) -> parent run_id + trials

SPACE: {param: spec} with spec one of
    {"values": [..]}                      explicit candidates
    {"min": a, "max": b, "step": s}       a numeric grid (inclusive)
    {"min": a, "max": b}                  continuous (random / adaptive only); integers when
                                          both bounds are integers
Parameters not in the space keep the request's params (or the strategy defaults).

METHODS
    grid       every combination (refused above max_trials -- no silent truncation)
    random     max_trials draws, seeded (the same seed gives the same trials)
    adaptive   half the budget random, the rest local search: each step perturbs the best
               set so far by one grid step (or 10% of the range) in one parameter; a
               cheap, transparent coarse-to-fine search -- not Bayesian optimisation
Every trial is an ordinary backtest run (kind opt_trial) under one parent run (kind
optimization), so each can be inspected, and the whole search is reproducible from
the parent's snapshot.

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

from backtest import service, store
from db.schema import get_connection

METHODS = ("grid", "random", "adaptive")
MAX_TRIALS = 400
_N = NormalDist()


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
                             f"max_trials, or use random / adaptive")
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
            n_random = budget if method == "random" else max(1, budget // 2)
            attempts = 0
            while len(trials) < n_random and attempts < n_random * 20:
                attempts += 1
                run({**base_params, **{k: _sample(s, rng) for k, s in space.items()}})
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
