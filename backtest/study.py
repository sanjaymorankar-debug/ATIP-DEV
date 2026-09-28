"""
End-to-end strategy research study (W23; QR-07 momentum, QR-08 mean reversion):
one command from a hypothesis to an out-of-sample verdict, recorded as a W22
research study with every run linked.

    run_study(strategy_id, space, start, end, hypothesis=None, method="adaptive",
              max_trials=30, select_by="sharpe", top_k=3, split=(0.6, 0.2, 0.2), seed=42,
              universe=None, sensitivity_steps=1)

  1 split     the sessions of [start, end] into research / validation / test (60/20/20)
  2 optimise  on RESEARCH only (backtest/optimize.py)
  3 validate  the top_k research sets on VALIDATION; the best validation metric wins
  4 sensitivity around the winner on research (knife edges)
  5 robustness on research + validation (costs, slippage, subsamples, halves, regimes, MC)
  6 test      the winner ONCE on the untouched TEST window (allow_test)
  7 verdict   SUPPORTED when test return > 0, test Sharpe > 0 and robustness is not
              NOT_ROBUST; REJECTED when the test return <= 0; else INCONCLUSIVE --
              recorded as the study's conclusion (frozen)

PRESETS give a hypothesis and a search space for the two research strategies:
`python -m backtest study momentum --start ... --end ...`.
"""

from __future__ import annotations

from backtest import optimize as OPT
from backtest import service
from backtest.robustness import robustness
from backtest.sensitivity import sensitivity
from backtest.walkforward import trading_sessions

PRESETS = {
    "momentum": {
        "hypothesis": "Stocks breaking out to a new 55-session high with strong 60-session momentum, above "
                      "their 200-session average, continue to outperform over the following weeks.",
        "space": {"breakout_lookback": {"values": [20, 55, 100]}, "min_momentum_pct": {"values": [5, 10, 20]},
                  "stop_pct": {"values": [5, 7, 10]}, "target_pct": {"values": [12, 20, 30]}},
    },
    "mean_reversion_rsi": {
        "hypothesis": "Short-term washouts (RSI(2) < 10) in stocks above their 200-session average revert "
                      "within a few sessions.",
        "space": {"entry_rsi": {"values": [5, 10, 15]}, "exit_rsi": {"values": [60, 70, 80]},
                  "max_hold_sessions": {"values": [3, 5, 7, 10]}, "stop_pct": {"values": [4, 6, 8]}},
    },
}


def split_periods(start, end, split=(0.6, 0.2, 0.2)) -> dict:
    if abs(sum(split) - 1) > 1e-6 or min(split) <= 0:
        raise ValueError("split must be three positive shares summing to 1")
    s = trading_sessions(start, end)
    if len(s) < 120:
        raise ValueError(f"{len(s)} sessions between {start} and {end}: a study needs at least 120")
    a = int(len(s) * split[0])
    b = a + int(len(s) * split[1])
    return {"research": [str(s[0]), str(s[a - 1])], "validation": [str(s[a]), str(s[b - 1])],
            "test": [str(s[b]), str(s[-1])]}


def _m(res, k):
    return (res.get("metrics") or {}).get(k)


def run_study(strategy_id, space=None, start=None, end=None, hypothesis=None, method="adaptive", max_trials=30,
              select_by="sharpe", top_k=3, split=(0.6, 0.2, 0.2), seed=42, universe=None, sensitivity_steps=1,
              min_trades=5, actor="owner") -> dict:
    from db.schema import get_connection
    from quant import studies as ST
    preset = PRESETS.get(strategy_id, {})
    space = space or preset.get("space")
    if not space:
        raise ValueError(f"no search space given and no preset for {strategy_id}")
    per = split_periods(start, end, split)
    base = {"strategy_id": strategy_id}
    if universe:
        base["universe"] = universe
    conn = get_connection()
    try:
        study = ST.create(conn, {"title": f"{strategy_id} research study {start}..{end}",
                                 "hypothesis": hypothesis or preset.get("hypothesis") or f"{strategy_id} has an edge",
                                 "method": f"optimise ({method}, {max_trials} trials, {select_by}) on research "
                                           f"{per['research']}, pick among top {top_k} on validation "
                                           f"{per['validation']}, sensitivity + robustness, one test run on "
                                           f"{per['test']}", "tags": ["W23", strategy_id]}, actor)
    finally:
        conn.close()
    sid = study["study_id"]

    def link(kind, ref, note):
        c = get_connection()
        try:
            ST.link(c, sid, kind, ref, note, actor)
        finally:
            c.close()

    out = {"study_id": sid, "periods": per}
    research = {**base, "periods": per, "period_label": "research"}
    opt = OPT.optimize(research, space, method, select_by, max_trials, seed, min_trades)
    link("backtest", opt["run_id"], "optimisation on research")
    out["optimization"] = {"run_id": opt["run_id"], "best": opt.get("best"), "diagnostics": opt.get("diagnostics")}
    top = [t for t in opt["trials"] if t["score"] is not None][:int(top_k)]
    if not top:
        return _conclude(sid, out, "INCONCLUSIVE", "no parameter set produced enough trades on the research window",
                         actor)
    val = []
    for t in top:
        res = service.create_and_run({**base, "params": t["params"], "periods": per, "period_label": "validation"})
        link("backtest", res["run_id"], f"validation of research trial {t['index']}")
        val.append({"params": t["params"], "run_id": res["run_id"], select_by: OPT.metric_of(res, select_by, 1)})
    scored = [v for v in val if v[select_by] is not None]
    winner = max(scored, key=lambda v: v[select_by]) if scored else val[0]
    out["validation"] = val
    out["chosen_params"] = winner["params"]
    sens = sensitivity({**research, "params": winner["params"]},
                       {k: space[k] for k in space if k in winner["params"]}, select_by, int(sensitivity_steps),
                       min_trades)
    link("backtest", sens["run_id"], "sensitivity around the chosen set (research)")
    out["sensitivity"] = {"run_id": sens["run_id"], "knife_edges": sens["knife_edges"],
                          "robust_share": sens["robust_share"]}
    rob = robustness({**base, "params": winner["params"], "start": per["research"][0], "end": per["validation"][1]},
                     n_subsamples=2, seed=seed)
    link("backtest", rob["run_id"], "robustness on research + validation")
    out["robustness"] = {"run_id": rob["run_id"], "verdict": rob["verdict"], "score": rob["score"],
                         "checks": rob["checks"]}
    test = service.create_and_run({**base, "params": winner["params"], "periods": per, "period_label": "test",
                                   "allow_test": True})
    link("backtest", test["run_id"], "the single out-of-sample test run")
    out["test"] = {"run_id": test["run_id"], "status": test["status"], "metrics": test.get("metrics")}
    tr, ts = _m(test, "total_return"), _m(test, "sharpe")
    if test["status"] != "COMPLETED" or tr is None:
        outcome, why = "INCONCLUSIVE", "the test run did not complete"
    elif tr <= 0:
        outcome, why = "REJECTED", f"out-of-sample return {tr:.2%} <= 0"
    elif (ts or 0) > 0 and rob["verdict"] != "NOT_ROBUST":
        outcome, why = "SUPPORTED", f"out-of-sample return {tr:.2%}, Sharpe {ts:.2f}, robustness {rob['verdict']}"
    else:
        outcome, why = "INCONCLUSIVE", f"out-of-sample return {tr:.2%} but Sharpe {ts} / robustness {rob['verdict']}"
    return _conclude(sid, out, outcome, why + ("; overfitting warning on the research optimisation"
                                               if (opt.get("diagnostics") or {}).get("overfit_warning") else ""),
                     actor)


def _conclude(sid, out, outcome, why, actor):
    from db.schema import get_connection
    from quant import studies as ST
    conn = get_connection()
    try:
        s = ST.get(conn, sid)
        if not s["links"]:
            ST.abandon(conn, sid, why, actor)
        else:
            ST.conclude(conn, sid, outcome, why, actor)
    finally:
        conn.close()
    return {**out, "outcome": outcome, "conclusion": why}
