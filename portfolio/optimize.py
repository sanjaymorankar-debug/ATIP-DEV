"""
Portfolio optimisation (W25, PF-10) and rebalance planning (PF-06). numpy only.

optimise(conn, symbols, objective, ...)
    objective   min_variance | mean_variance (max mu'w - lambda/2 w'Sigma w) |
                max_sharpe (the frontier point with the best (mu - rf) / sigma) |
                risk_parity (equal risk contribution, exact -- not the diagonal
                approximation quant/portfolio.py "risk" uses)
    long only; constraints: max_weight (per name), min_weight (per held name, optional),
    sector_cap (per NSE industry). Solved by projected gradient: every step is projected
    onto {sum w = 1, 0 <= w <= max_weight} (exact, by bisection) and then sector caps
    (scale the over-cap sector down, hand the excess to the rest), alternating until
    both hold. An infeasible set (e.g. max_weight x n < 1) is reported, not forced.

    Inputs: covariance from `lookback` sessions of daily returns, shrunk toward the
    constant-correlation target (`shrinkage`, default 0.3 -- sample covariances of a
    few hundred sessions over-fit). Expected returns (`expected`):
        "historical"  annualised mean return, shrunk halfway to the cross-sectional mean
        "equal"       the same for every name (-> max_sharpe == min_variance)
        {symbol: annual return}  your own views
    Stored in portfolio_optimization with the inputs, so a result can be reproduced.

frontier(...) -> points (vol, return, weights) for a grid of risk aversions.

rebalance_plan(conn, target_weights, book, ...)
    The trades that move a book to target weights (of equity x (1 - cash_buffer_pct)):
    NEW / ADD / REDUCE / EXIT / HOLD per name, whole shares at the latest price. A name
    whose drift is inside `band_pct` (percentage points of equity) or whose trade is below
    `min_trade_value` is HOLD. Estimated costs (paper brokerage + slippage) and turnover.
    A PLAN only: nothing is ordered. Trades go through a strategy + the W4 risk engine
    (the portfolio strategy kind's reweight_band_pct emits the same ADD / REDUCE).
"""

from __future__ import annotations

import json
import math
import uuid
from collections import defaultdict
from datetime import date, datetime

import numpy as np

OBJECTIVES = ("min_variance", "mean_variance", "max_sharpe", "risk_parity")


def shrink_cov(R: np.ndarray, shrinkage: float = 0.3) -> np.ndarray:
    S = np.cov(R.T)
    if S.ndim == 0:
        return np.array([[float(S)]])
    sd = np.sqrt(np.diag(S))
    C = S / np.outer(sd, sd)
    n = len(sd)
    rbar = (C.sum() - n) / (n * (n - 1)) if n > 1 else 0.0
    F = rbar * np.outer(sd, sd)
    np.fill_diagonal(F, sd ** 2)
    return (1 - shrinkage) * S + shrinkage * F


def _proj_box_simplex(v: np.ndarray, ub: np.ndarray, lb: np.ndarray) -> np.ndarray:
    """Exact Euclidean projection onto {sum w = 1, lb <= w <= ub}: find the shift t with
    sum clip(v - t, lb, ub) = 1. That sum is piecewise linear and decreasing in t with
    breakpoints v - ub and v - lb, so evaluate it at every breakpoint (vectorised) and
    interpolate inside the bracketing segment."""
    bp = np.unique(np.concatenate([v - ub, v - lb]))
    f = np.clip(v[None, :] - bp[:, None], lb, ub).sum(1)          # decreasing in bp
    k = int(np.searchsorted(-f, -1.0))                              # first bp with f <= 1
    if k == 0:
        t = bp[0]
    elif k >= len(bp):
        t = bp[-1]
    else:
        f0, f1 = f[k - 1], f[k]
        t = bp[k - 1] + (bp[k] - bp[k - 1]) * ((f0 - 1.0) / (f0 - f1) if f0 != f1 else 0.0)
    return np.clip(v - t, lb, ub)


def _proj_sectors(u, groups, caps):
    """Exact projection onto {sum of each capped group <= cap} (disjoint half-spaces)."""
    u = u.copy()
    for g, idx in groups.items():
        cap = caps.get(g)
        if cap is not None:
            s = u[idx].sum()
            if s > cap:
                u[idx] -= (s - cap) / len(idx)
    return u


def _project(v, ub, lb, groups, caps, iters=500):
    """Exact Euclidean projection onto box-simplex AND sector caps (Dykstra)."""
    if not caps:
        return _proj_box_simplex(v, ub, lb)
    x, p, q = v.copy(), np.zeros_like(v), np.zeros_like(v)
    y = x
    for _ in range(iters):
        y = _proj_box_simplex(x + p, ub, lb)
        p = x + p - y
        z = _proj_sectors(y + q, groups, caps)
        q = y + q - z
        if np.abs(z - x).max() < 1e-9:
            x = z
            break
        x = z
    return y


def _solve(mu, S, lam, ub, lb, groups, caps, iters=3000, obj="mean_variance", w0=None):
    """Accelerated projected gradient (FISTA) on the convex quadratic."""
    n = len(mu)
    lam = 1.0 if obj == "min_variance" else max(float(lam), 1e-9)
    lin = np.zeros(n) if obj == "min_variance" else mu
    L = float(np.linalg.eigvalsh(S).max()) * lam
    step = 1 / (L if L > 0 else 1.0)
    w = _project(np.full(n, 1 / n) if w0 is None else w0, ub, lb, groups, caps)
    yk, t = w.copy(), 1.0
    for _ in range(iters):
        nw = _project(yk - step * (lam * (S @ yk) - lin), ub, lb, groups, caps)
        if np.abs(nw - w).max() < 1e-8:
            w = nw
            break
        nt = (1 + math.sqrt(1 + 4 * t * t)) / 2
        yk = nw + (t - 1) / nt * (nw - w)
        w, t = nw, nt
    return w


def _risk_parity(S, iters=2000):
    n = len(S)
    w = np.full(n, 1 / n)
    for _ in range(iters):
        m = S @ w
        rc = w * m
        nw = w * (rc.mean() / np.where(rc > 0, rc, 1e-12)) ** 0.5
        nw /= nw.sum()
        if np.abs(nw - w).max() < 1e-12:
            break
        w = nw
    return w


def inputs(conn, symbols, as_of=None, lookback: int = 250, shrinkage: float = 0.3, expected="historical") -> dict:
    from portfolio.risk import returns_matrix
    rm = returns_matrix(conn, list(symbols), as_of, lookback)
    syms = rm["symbols"]
    if len(syms) < 2:
        raise ValueError(f"need >= 2 names with price history; have {len(syms)} ({rm['excluded']})")
    S = shrink_cov(rm["R"], shrinkage) * 252
    hist = rm["R"].mean(0) * 252
    if isinstance(expected, dict):
        missing = [s for s in syms if s not in expected]
        if missing:
            raise ValueError(f"no expected return for {missing}")
        mu = np.array([float(expected[s]) for s in syms])
        src = "user views"
    elif expected == "equal":
        mu = np.full(len(syms), float(hist.mean()))
        src = "equal"
    else:
        mu = 0.5 * hist + 0.5 * hist.mean()
        src = "historical, shrunk 50% to the cross-sectional mean"
    return {"symbols": syms, "mu": mu, "S": S, "excluded": rm["excluded"], "sessions": len(rm["dates"]),
            "expected_source": src, "as_of": rm["dates"][-1]}


def _constraints(syms, max_weight, min_weight, sector_cap, sectors):
    n = len(syms)
    ub = np.full(n, float(max_weight) if max_weight else 1.0)
    lb = np.full(n, float(min_weight) if min_weight else 0.0)
    if ub.sum() < 1 - 1e-9:
        raise ValueError(f"infeasible: max_weight {max_weight} x {n} names < 100%")
    if lb.sum() > 1 + 1e-9:
        raise ValueError(f"infeasible: min_weight {min_weight} x {n} names > 100%")
    groups = defaultdict(list)
    for i, s in enumerate(syms):
        groups[sectors.get(s, "UNKNOWN")].append(i)
    groups = {g: np.array(v) for g, v in groups.items()}
    caps = {}
    if sector_cap:
        caps = {g: float(sector_cap) for g in groups}
        cap_room = sum(min(float(sector_cap), ub[idx].sum()) for idx in groups.values())
        if cap_room < 1 - 1e-9:
            raise ValueError(f"infeasible: sector_cap {sector_cap} over {len(groups)} sectors < 100%")
    return ub, lb, groups, caps


def _stats(w, mu, S, rf):
    ret = float(w @ mu)
    vol = float(math.sqrt(max(w @ S @ w, 0)))
    rc = w * (S @ w) / (vol ** 2) if vol > 0 else np.zeros(len(w))
    return {"expected_return_pct": round(ret * 100, 3), "volatility_pct": round(vol * 100, 3),
            "sharpe": round((ret - rf) / vol, 3) if vol > 0 else None,
            "effective_n": round(1 / float((w ** 2).sum()), 2), "max_risk_share": round(float(rc.max()), 4)}


def optimise(conn, symbols, objective: str = "min_variance", *, risk_aversion: float = 4.0, max_weight=0.2,
             min_weight=None, sector_cap=0.35, lookback: int = 250, shrinkage: float = 0.3, expected="historical",
             risk_free: float = 0.065, as_of=None, store: bool = True) -> dict:
    if objective not in OBJECTIVES:
        raise ValueError(f"objective must be one of {OBJECTIVES}")
    from portfolio.risk import _sectors
    inp = inputs(conn, symbols, as_of, lookback, shrinkage, expected)
    syms, mu, S = inp["symbols"], inp["mu"], inp["S"]
    sec = _sectors()
    ub, lb, groups, caps = _constraints(syms, max_weight, min_weight, sector_cap, sec)
    if objective == "min_variance":
        w = _solve(mu, S, 1.0, ub, lb, groups, caps, obj="min_variance")
    elif objective == "mean_variance":
        w = _solve(mu, S, float(risk_aversion), ub, lb, groups, caps)
    elif objective == "max_sharpe":
        # coarse grid over log(lambda), warm-started along the frontier, then refine
        # around the best point (Sharpe along the frontier is close to unimodal)
        best, prev, tried = None, None, {}

        def at(lam):
            nonlocal prev
            if lam not in tried:
                prev = _solve(mu, S, lam, ub, lb, groups, caps, iters=1500, w0=prev)
                tried[lam] = (prev, _stats(prev, mu, S, risk_free))
            return tried[lam]
        grid = list(np.geomspace(64, 0.25, 9))
        for lam in grid:
            cw, st = at(lam)
            if st["sharpe"] is not None and (best is None or st["sharpe"] > best[1]):
                best = (lam, st["sharpe"])
        lo, hi = best[0] / 2, best[0] * 2
        for _ in range(6):
            m1, m2 = lo * (hi / lo) ** (1 / 3), lo * (hi / lo) ** (2 / 3)
            if (at(m1)[1]["sharpe"] or -1e9) >= (at(m2)[1]["sharpe"] or -1e9):
                hi = m2
            else:
                lo = m1
        lam_best = max(tried, key=lambda k: tried[k][1]["sharpe"] if tried[k][1]["sharpe"] is not None else -1e9)
        w = tried[lam_best][0]
        risk_aversion = round(float(lam_best), 4)
    else:
        w = _risk_parity(S)
        if (w > ub + 1e-9).any() or (w < lb - 1e-9).any() or                 any(w[idx].sum() > caps[g] + 1e-9 for g, idx in groups.items() if g in caps):
            w = _project(w, ub, lb, groups, caps)     # nearest feasible to the unconstrained ERC
    w = np.where(w < 1e-5, 0.0, w)
    w = w / w.sum()
    weights = {s: round(float(x), 6) for s, x in sorted(zip(syms, w), key=lambda kv: -kv[1]) if x > 0}
    sw = defaultdict(float)
    for s, x in weights.items():
        sw[sec.get(s, "UNKNOWN")] += x
    eq = np.full(len(syms), 1 / len(syms))
    out = {"objective": objective, "as_of": inp["as_of"], "weights": weights,
           "sector_weights": {k: round(v, 4) for k, v in sorted(sw.items(), key=lambda kv: -kv[1])},
           "stats": _stats(w, mu, S, risk_free), "equal_weight_stats": _stats(eq, mu, S, risk_free),
           "inputs": {"symbols": syms, "sessions": inp["sessions"], "lookback": lookback, "shrinkage": shrinkage,
                      "expected": inp["expected_source"], "risk_free": risk_free,
                      "risk_aversion": risk_aversion if objective in ("mean_variance", "max_sharpe") else None,
                      "max_weight": max_weight, "min_weight": min_weight, "sector_cap": sector_cap,
                      "expected_returns_pct": {s: round(float(m) * 100, 2) for s, m in zip(syms, mu)}},
           "excluded": inp["excluded"],
           "binding": {"max_weight": [s for s, x in weights.items() if max_weight and x >= max_weight - 1e-4],
                       "sector_cap": [g for g, x in sw.items() if sector_cap and x >= sector_cap - 1e-4]},
           "note": "expected returns are estimates: the weights are only as good as mu; min_variance and "
                   "risk_parity do not use mu"}
    if store:
        oid = "OPT" + datetime.now().strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:4]
        conn.execute("INSERT INTO portfolio_optimization (opt_id, as_of, objective, result_json, created_at) "
                     "VALUES (?,?,?,?,?)", (oid, out["as_of"], objective, json.dumps(out), datetime.now()))
        conn.commit()
        out["opt_id"] = oid
    return out


def frontier(conn, symbols, points: int = 12, **kw) -> dict:
    from portfolio.risk import _sectors
    inp = inputs(conn, symbols, kw.get("as_of"), kw.get("lookback", 250), kw.get("shrinkage", 0.3),
                 kw.get("expected", "historical"))
    ub, lb, groups, caps = _constraints(inp["symbols"], kw.get("max_weight", 0.2), kw.get("min_weight"),
                                        kw.get("sector_cap", 0.35), _sectors())
    rf = kw.get("risk_free", 0.065)
    out, w = [], None
    for lam in np.geomspace(128, 0.25, int(points)):          # warm-start along the frontier
        w = _solve(inp["mu"], inp["S"], lam, ub, lb, groups, caps, iters=1500, w0=w)
        out.append({"risk_aversion": round(float(lam), 3), **_stats(w, inp["mu"], inp["S"], rf),
                    "weights": {s: round(float(x), 4) for s, x in zip(inp["symbols"], w) if x > 1e-4}})
    out.sort(key=lambda p: p["volatility_pct"])
    return {"as_of": inp["as_of"], "points": out, "expected": inp["expected_source"]}


def get_optimization(conn, opt_id: str) -> dict | None:
    r = conn.execute("SELECT result_json FROM portfolio_optimization WHERE opt_id=?", (opt_id,)).fetchone()
    return {**json.loads(r[0]), "opt_id": opt_id} if r else None


# -- PF-06 -------------------------------------------------------------------------------

def rebalance_plan(conn, target_weights: dict, book: str = "PAPER", *, band_pct: float = 1.0,
                   min_trade_value: float = 1000.0, cash_buffer_pct: float = 2.0, equity: float | None = None,
                   store: bool = True) -> dict:
    from portfolio.risk import book_snapshot
    from portfolio.pnl import mark_price
    snap = book_snapshot(conn, book)
    eq = float(equity or snap.get("equity") or 0)
    if eq <= 0:
        raise ValueError(f"{book} equity unknown -- pass equity")
    tot = sum(v for v in target_weights.values() if v > 0)
    if tot <= 0:
        raise ValueError("target weights sum to 0")
    investable = eq * (1 - cash_buffer_pct / 100)
    tgt = {s: v / tot for s, v in target_weights.items() if v > 0}
    held = {p["symbol"]: p for p in snap["positions"]}
    try:
        from orders.paper import settings as paper_settings
        ps = paper_settings()
        cost_rate = float(ps["paper_brokerage_pct"]) / 100 + float(ps["paper_slippage_bps"]) / 10000
    except Exception:
        cost_rate = 0.0013
    trades, buy_v, sell_v = [], 0.0, 0.0
    for s in sorted(set(tgt) | set(held)):
        px = (held.get(s) or {}).get("mark") or mark_price(conn, s)
        cur_v = (held.get(s) or {}).get("value") or 0.0
        cur_q = (held.get(s) or {}).get("qty") or 0
        tv = investable * tgt.get(s, 0.0)
        row = {"symbol": s, "price": px, "current_qty": cur_q, "current_value": round(cur_v, 2),
               "current_pct": round(cur_v / eq * 100, 3), "target_pct": round(tv / eq * 100, 3)}
        if not px:
            trades.append({**row, "action": "SKIP", "quantity": 0, "reason": "no price"})
            continue
        drift = (tv - cur_v) / eq * 100
        q = int(math.floor(abs(tv - cur_v) / px))
        if s not in tgt:
            action, q = "EXIT", cur_q
        elif abs(drift) < band_pct and cur_q:
            action, q = "HOLD", 0
        elif q * px < min_trade_value:
            action, q = "HOLD", 0
        elif tv > cur_v:
            action = "ADD" if cur_q else "NEW"
        else:
            action, q = "REDUCE", min(q, cur_q)
        val = q * px
        if action in ("NEW", "ADD"):
            buy_v += val
        elif action in ("REDUCE", "EXIT"):
            sell_v += val
        trades.append({**row, "action": action, "quantity": q, "trade_value": round(val, 2),
                       "drift_pct": round(drift, 3), "est_cost": round(val * cost_rate, 2)})
    order = {"EXIT": 0, "REDUCE": 1, "NEW": 2, "ADD": 3, "HOLD": 4, "SKIP": 5}
    trades.sort(key=lambda t: (order[t["action"]], -t.get("trade_value", 0)))
    cash_after = (snap.get("cash") or 0) + sell_v - buy_v - (buy_v + sell_v) * cost_rate
    out = {"book": book, "as_of": snap["as_of"], "equity": eq, "trades": trades,
           "summary": {"buys": round(buy_v, 2), "sells": round(sell_v, 2),
                       "turnover_pct": round((buy_v + sell_v) / eq * 100, 2),
                       "est_costs": round((buy_v + sell_v) * cost_rate, 2),
                       "cash_after": round(cash_after, 2),
                       "n_trades": sum(1 for t in trades if t["action"] in ("NEW", "ADD", "REDUCE", "EXIT"))},
           "params": {"band_pct": band_pct, "min_trade_value": min_trade_value, "cash_buffer_pct": cash_buffer_pct},
           "note": "a plan only; nothing is ordered. Sells are listed first so their cash funds the buys"}
    if cash_after < 0:
        out["warning"] = "buys exceed cash after sells; lower the targets or raise cash_buffer_pct"
    if store:
        pid = "RB" + datetime.now().strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:4]
        conn.execute("INSERT INTO portfolio_rebalance_plan (plan_id, book, as_of, plan_json, created_at) "
                     "VALUES (?,?,?,?,?)", (pid, book, out["as_of"], json.dumps(out, default=str), datetime.now()))
        conn.commit()
        out["plan_id"] = pid
    return out
