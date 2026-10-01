"""
Reinforcement learning (W36: ML-04) -- research only.

A tabular Q-learning agent that learns WHEN to be long or flat in a stock, trained and judged
on ATIP's own daily data with costs, and compared out of sample against the two obvious
baselines. It is a research tool: a learned policy is stored with its evaluation, it cannot
be activated, and nothing here creates a decision, intent or order. (The tracker rates RL
"low priority"; this gives it an honest test rather than a production path.)

Environment (one trajectory per symbol, daily bars from prices_daily, point in time)
    state     4 discrete features known at the close of day t
                mom    20-session return tercile (computed on the TRAINING window only)
                trend  close above / below its 50-session average
                vol    20-session volatility tercile (training-window cut points)
                pos    currently FLAT / LONG
              -> 3 x 2 x 3 x 2 = 36 states
    actions   FLAT, LONG (exposure 0 / 1 from the next session)
    reward    exposure x next-session return - cost_bps x |change in exposure| - risk_penalty x
              exposure x (20-session volatility)^2 -- costs are charged, so churning is learned against

Training    epsilon-greedy Q-learning over the training window (start .. split), `episodes` passes
            over every symbol's trajectory, epsilon decaying linearly to epsilon_min, learning rate
            alpha, discount gamma.
Evaluation  the greedy policy on split .. end (never seen in training), equal-weight across symbols,
            against BUY_AND_HOLD (always long) and TREND (long while close > 50-session average):
            total return, annualised Sharpe, max drawdown, exposure, turnover.
Verdict     BEATS_BASELINES only when the out-of-sample Sharpe beats both baselines by >= 0.2;
            otherwise NO_ADVANTAGE (the expected result for a 36-state agent -- it is recorded).

    train_and_evaluate(conn, symbols, start, end, split, params) -> result (stored in ml_rl_run)
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import date, datetime

import numpy as np

DEFAULTS = {"episodes": 30, "alpha": 0.1, "gamma": 0.95, "epsilon": 0.3, "epsilon_min": 0.02, "cost_bps": 15.0,
            "risk_penalty": 0.0, "seed": 3}
FLAT, LONG = 0, 1
SHARPE_MARGIN = 0.2


def _d(v):
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def _load(conn, symbols, start, end):
    out = {}
    for s in symbols:
        rows = conn.execute("SELECT date, close FROM prices_daily WHERE symbol=? AND date<=? AND close>0 ORDER BY date",
                            (s, str(end))).fetchall()
        if len(rows) < 120:
            continue
        d = [_d(r[0]) for r in rows]
        c = np.array([float(r[1]) for r in rows])
        ret = np.r_[np.nan, c[1:] / c[:-1] - 1]
        mom = np.full(len(c), np.nan)
        vol = np.full(len(c), np.nan)
        sma = np.full(len(c), np.nan)
        for i in range(50, len(c)):
            mom[i] = c[i] / c[i - 20] - 1
            vol[i] = np.std(ret[i - 19:i + 1])
            sma[i] = c[i - 49:i + 1].mean()
        keep = [i for i in range(len(c)) if d[i] >= start and not math.isnan(sma[i])]
        if len(keep) < 60:
            continue
        out[s] = {"date": [d[i] for i in keep], "close": c[keep], "ret": ret[keep], "mom": mom[keep],
                  "vol": vol[keep], "trend": (c[keep] > sma[keep]).astype(int)}
    return out


def _cuts(data, split):
    m = np.concatenate([v["mom"][[i for i, d in enumerate(v["date"]) if d < split]] for v in data.values()])
    s = np.concatenate([v["vol"][[i for i, d in enumerate(v["date"]) if d < split]] for v in data.values()])
    return np.nanquantile(m, [1 / 3, 2 / 3]), np.nanquantile(s, [1 / 3, 2 / 3])


def _state(v, i, pos, cuts):
    mc, vc = cuts
    m = int(np.searchsorted(mc, v["mom"][i]))
    s = int(np.searchsorted(vc, v["vol"][i]))
    return ((m * 2 + int(v["trend"][i])) * 3 + s) * 2 + pos


def _reward(v, i, prev, act, p):
    r = act * v["ret"][i + 1]
    r -= p["cost_bps"] / 1e4 * abs(act - prev)
    r -= p["risk_penalty"] * act * (v["vol"][i] ** 2)
    return float(r)


def _train(data, split, cuts, p):
    rng = np.random.default_rng(int(p["seed"]))
    Q = np.zeros((36, 2))
    visits = np.zeros((36, 2), dtype=int)
    eps0, eps1, n_ep = float(p["epsilon"]), float(p["epsilon_min"]), int(p["episodes"])
    for ep in range(n_ep):
        eps = eps0 - (eps0 - eps1) * ep / max(1, n_ep - 1)
        for v in data.values():
            idx = [i for i, d in enumerate(v["date"]) if d < split]
            pos = FLAT
            for k in idx[:-1]:
                s = _state(v, k, pos, cuts)
                a = int(rng.integers(2)) if rng.random() < eps else int(Q[s].argmax())
                rwd = _reward(v, k, pos, a, p)
                s2 = _state(v, k + 1, a, cuts)
                Q[s, a] += float(p["alpha"]) * (rwd + float(p["gamma"]) * Q[s2].max() - Q[s, a])
                visits[s, a] += 1
                pos = a
    return Q, visits


def _run_policy(data, split, cuts, policy, p):
    """Daily equal-weight portfolio returns over split.. for a policy(v, i, pos) -> 0/1."""
    by_date, expo, turns = {}, [], 0
    for v in data.values():
        idx = [i for i, d in enumerate(v["date"]) if d >= split]
        pos = FLAT
        for k in idx[:-1]:
            a = policy(v, k, pos)
            r = _reward(v, k, pos, a, {**p, "risk_penalty": 0.0})
            turns += abs(a - pos)
            by_date.setdefault(v["date"][k + 1], []).append(r)
            expo.append(a)
            pos = a
    days = sorted(by_date)
    rets = np.array([np.mean(by_date[d]) for d in days])
    if not len(rets):
        return None
    eq = np.cumprod(1 + rets)
    sd = rets.std(ddof=1) if len(rets) > 1 else 0.0
    peak = np.maximum.accumulate(eq)
    return {"days": len(rets), "total_return": round(float(eq[-1] - 1), 4),
            "sharpe": round(float(rets.mean() / sd * math.sqrt(252)), 3) if sd > 0 else None,
            "max_drawdown": round(float((eq / peak - 1).min()), 4),
            "exposure": round(float(np.mean(expo)), 3) if expo else 0.0, "turnover": turns,
            "start": str(days[0]), "end": str(days[-1])}


def train_and_evaluate(conn, symbols, start, end, split, params: dict | None = None) -> dict:
    p = {**DEFAULTS, **{k: v for k, v in (params or {}).items() if k in DEFAULTS}}
    start, end, split = _d(start), _d(end), _d(split)
    if not (start < split < end):
        raise ValueError("need start < split < end")
    data = _load(conn, [s.upper() for s in symbols], start, end)
    if not data:
        raise ValueError("no symbol has enough daily bars in the window (>= 120 sessions incl. warm-up)")
    cuts = _cuts(data, split)
    Q, visits = _train(data, split, cuts, p)
    agent = _run_policy(data, split, cuts, lambda v, i, pos: int(Q[_state(v, i, pos, cuts)].argmax()), p)
    bh = _run_policy(data, split, cuts, lambda v, i, pos: LONG, p)
    tr = _run_policy(data, split, cuts, lambda v, i, pos: int(v["trend"][i]), p)
    if not agent:
        raise ValueError("no out-of-sample days after the split")
    sa = agent["sharpe"] if agent["sharpe"] is not None else -9
    best_base = max((x["sharpe"] for x in (bh, tr) if x and x["sharpe"] is not None), default=None)
    verdict = "BEATS_BASELINES" if best_base is not None and sa >= best_base + SHARPE_MARGIN else "NO_ADVANTAGE"
    policy = {f"mom{m}_trend{t}_vol{s}": ("LONG" if Q[((m * 2 + t) * 3 + s) * 2 + 1].argmax() else "FLAT")
              for m in range(3) for t in range(2) for s in range(3)}
    rid = "RL" + uuid.uuid4().hex[:14].upper()
    res = {"run_id": rid, "symbols": sorted(data), "start": str(start), "split": str(split), "end": str(end),
           "params": p, "verdict": verdict, "out_of_sample": {"agent": agent, "buy_and_hold": bh, "trend": tr},
           "policy_when_long": policy, "unvisited_state_actions": int((visits == 0).sum()),
           "cut_points": {"momentum": [round(float(x), 5) for x in cuts[0]],
                          "volatility": [round(float(x), 5) for x in cuts[1]]},
           "note": "Research only: the policy cannot be activated and creates no decisions or orders."}
    conn.execute("INSERT INTO ml_rl_run (run_id,verdict,result_json,q_table_json,created_at) VALUES (?,?,?,?,?)",
                 (rid, verdict, json.dumps(res, default=str), json.dumps(Q.round(6).tolist()), datetime.now()))
    conn.commit()
    return res


def runs(conn, limit=30) -> list:
    out = []
    for rid, verdict, rj, at in conn.execute("SELECT run_id, verdict, result_json, created_at FROM ml_rl_run "
                                             "ORDER BY created_at DESC LIMIT ?", (int(limit),)):
        r = json.loads(rj)
        out.append({"run_id": rid, "verdict": verdict, "created_at": str(at)[:19], "symbols": len(r["symbols"]),
                    "split": r["split"], "agent": r["out_of_sample"]["agent"],
                    "buy_and_hold": r["out_of_sample"]["buy_and_hold"], "trend": r["out_of_sample"]["trend"]})
    return out


def get_run(conn, run_id) -> dict:
    r = conn.execute("SELECT result_json FROM ml_rl_run WHERE run_id=?", (run_id,)).fetchone()
    if not r:
        raise LookupError(f"no RL run {run_id}")
    return json.loads(r[0])
