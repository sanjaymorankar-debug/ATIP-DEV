"""
Parameter sensitivity (W23, QR-11): is the chosen parameter set a plateau or a
knife edge?

    sensitivity(request, space, select_by="sharpe", steps=2, min_trades=10,
                pairwise=None) -> parent run_id + per-parameter curves

For each parameter in `space` (same spec as backtest/optimize.py), the request's
params are held fixed and that one parameter is moved +-1..steps grid steps (or
+-10% / 20% of its range for continuous specs), one backtest each (kind sens_trial
under a parent of kind sensitivity).

Per parameter:
    curve          [(value, metric)]
    stability      min(neighbour metric at +-1 step) / base metric (base > 0)
    knife_edge     a +-1 step neighbour loses more than half the base metric, or flips
                   its sign
    thin_neighbours  the +-1 step values with no metric (fewer than min_trades trades, or
                   the run did not complete): "stops trading one step away". W39b decision:
                   reported as a separate fragility flag, not as a knife edge, because there
                   is no metric to compare -- robust_share keeps its meaning.
Overall: robust_share = parameters that are not knife edges / all parameters;
thin_edges = parameters with a thin neighbour (review these before trusting the plateau).
pairwise=["a","b"] adds a 2-D grid (base +-steps on both) as a heat map.
"""

from __future__ import annotations

from backtest.optimize import _finish, _grid_values, _parent, _trial, metric_of, validate_space


def _around(spec, base, steps):
    if "values" in spec or spec.get("step"):
        vals = _grid_values(spec)
        if base in vals:
            i = vals.index(base)
        else:
            i = min(range(len(vals)), key=lambda j: abs(vals[j] - base) if isinstance(base, (int, float)) else 0)
        return [vals[j] for j in range(max(0, i - steps), min(len(vals), i + steps + 1))]
    lo, hi = spec["min"], spec["max"]
    ints = isinstance(lo, int) and isinstance(hi, int)
    out = []
    for k in range(-steps, steps + 1):
        v = min(hi, max(lo, base * (1 + 0.1 * k))) if base else lo + (hi - lo) * (0.5 + 0.1 * k)
        v = int(round(v)) if ints else round(v, 6)
        if v not in out:
            out.append(v)
    return out


def sensitivity(request: dict, space: dict, select_by: str = "sharpe", steps: int = 2, min_trades: int = 10,
                pairwise: list | None = None) -> dict:
    from backtest import service
    if request.get("period_label") == "test" or request.get("allow_test"):
        raise ValueError("sensitivity analysis on the test window is refused")
    validate_space(space)
    if not 1 <= int(steps) <= 5:
        raise ValueError("steps must be 1..5")
    snap = service.resolve_config(request)
    base_params = dict(snap["params"])
    for k in space:
        if k not in base_params:
            raise ValueError(f"{k} is not a parameter of {snap['strategy_id']}")
    base = {k: v for k, v in request.items() if k != "params"}
    pid, _ = _parent(request, "sensitivity", {"space": space, "select_by": select_by, "steps": steps,
                                              "pairwise": pairwise})
    idx = [0]
    cache = {}

    def score(params):
        key = tuple(sorted(params.items()))
        if key not in cache:
            rid, res = _trial(base, params, "sens_trial", pid, idx[0])
            idx[0] += 1
            cache[key] = (rid, metric_of(res, select_by, min_trades))
        return cache[key]
    try:
        base_rid, base_score = score(base_params)
        out = {}
        for k, spec in space.items():
            vals = _around(spec, base_params[k], int(steps))
            curve = [{"value": v, "run_id": score({**base_params, k: v})[0],
                      select_by: score({**base_params, k: v})[1]} for v in vals]
            i = vals.index(base_params[k]) if base_params[k] in vals else None
            near = [curve[j] for j in (i - 1, i + 1) if i is not None and 0 <= j < len(curve) and j != i]
            nb = [pt[select_by] for pt in near if pt[select_by] is not None]
            thin = [pt["value"] for pt in near if pt[select_by] is None]
            stab = (min(nb) / base_score) if (nb and base_score and base_score > 0) else None
            knife = bool(nb) and base_score is not None and (
                any((x > 0) != (base_score > 0) for x in nb) or (base_score > 0 and min(nb) < 0.5 * base_score))
            out[k] = {"curve": curve, "base_value": base_params[k], "stability": round(stab, 3) if stab is not None
                      else None, "knife_edge": knife, "thin_neighbours": thin}
        heat = None
        if pairwise and len(pairwise) == 2 and all(p in space for p in pairwise):
            a, b = pairwise
            va, vb = _around(space[a], base_params[a], int(steps)), _around(space[b], base_params[b], int(steps))
            heat = {"x": a, "y": b, "x_values": va, "y_values": vb,
                    "grid": [[score({**base_params, a: x, b: y})[1] for x in va] for y in vb]}
        n = len(out)
        summary = {"base_params": base_params, "base_run_id": base_rid, "base_" + select_by: base_score,
                   "parameters": out, "heatmap": heat,
                   "robust_share": round(sum(not v["knife_edge"] for v in out.values()) / n, 3) if n else None,
                   "knife_edges": [k for k, v in out.items() if v["knife_edge"]],
                   "thin_edges": [k for k, v in out.items() if v["thin_neighbours"]], "trials": idx[0]}
        _finish(pid, "COMPLETED", summary)
        return {"run_id": pid, "status": "COMPLETED", **summary}
    except Exception as e:
        from backtest import store
        from db.schema import get_connection
        conn = get_connection()
        try:
            store.mark_failed(conn, pid, f"{type(e).__name__}: {e}")
        finally:
            conn.close()
        raise

