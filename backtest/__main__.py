"""
    python -m backtest run --strategy dip --start 2025-06-01 --end 2026-09-23
    python -m backtest run --strategy dip --params '{"stop_pct": 4}' --capital 500000
    python -m backtest run --strategy dip --periods '{"research":["2025-02-01","2025-12-31"],
                           "validation":["2026-01-01","2026-04-30"],"test":["2026-05-01","2026-09-23"]}'
                           --period validation
    python -m backtest walkforward --strategy dip --start 2025-03-01 --end 2026-09-23
                           --train 120 --validation 40 --test 40 --step 40
                           --candidates '[{"stop_pct":3},{"stop_pct":5}]'
                           [--purge 10 --embargo 1]   (BT-18: gaps between windows; default 0 / 0)
    python -m backtest montecarlo RUN_ID --method trade_shuffle --sims 1000 --seed 42
    python -m backtest list
    python -m backtest show RUN_ID
    python -m backtest optimize --strategy dip --start D --end D --space '{"stop_pct":{"values":[3,5]}}'
                           [--method grid|random|adaptive] [--trials 60] [--select-by sharpe]
    python -m backtest sensitivity --strategy dip --start D --end D --space '{...}' [--steps 2] [--pairwise a,b]
    python -m backtest robustness --strategy dip --start D --end D [--subsamples 3]
    python -m backtest costsweep --strategy dip --start D --end D [--multipliers 0,0.5,1,1.5,2,3,5]
                           [--scale costs|slippage|both]      (BT-19: cost stress + break-even)
    python -m backtest study momentum|mean_reversion_rsi|<strategy> --start D --end D [--space '{...}']
                           [--trials 30]      (W23: optimise -> validate -> sensitivity -> robustness -> test)
    python -m backtest cpcv --strategy dip --start D --end D --groups 6 --test-groups 2
                           [--candidates '[{"stop_pct":3},{"stop_pct":5}]'] [--select-by sharpe]
                           [--purge 10 --embargo 1]   (CPCV: distribution of phi out-of-sample paths)
    python -m backtest pbo OPTIMIZATION_RUN_ID [--partitions 16] [--metric sharpe|mean|total_return]
    python -m backtest pbo --matrix returns.csv|returns.json [--partitions 16]
                           (PBO by CSCV on trial returns -- backtest/cpcv.py)
"""

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m backtest", description="ATIP backtests")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--strategy", required=True)
        p.add_argument("--params", type=json.loads, default=None, help="JSON object")
        p.add_argument("--capital", type=float, default=None)
        p.add_argument("--universe", type=lambda s: s.split(","), default=None, help="comma-separated symbols")
        p.add_argument("--cost-model", default=None)

    r = sub.add_parser("run"); common(r)
    r.add_argument("--start"); r.add_argument("--end")
    r.add_argument("--periods", type=json.loads, default=None)
    r.add_argument("--period", default=None, help="research|validation|test|full")
    r.add_argument("--allow-test", action="store_true")

    w = sub.add_parser("walkforward"); common(w)
    w.add_argument("--start", required=True); w.add_argument("--end", required=True)
    for k in ("train", "validation", "test", "step"):
        w.add_argument(f"--{k}", type=int, required=True, help="sessions")
    w.add_argument("--candidates", type=json.loads, default=None, help="JSON list of param objects")
    w.add_argument("--select-by", default="sharpe")
    w.add_argument("--purge", type=int, default=0, help="sessions dropped from the end of each in-sample window")
    w.add_argument("--embargo", type=int, default=0, help="sessions skipped at the start of each out-of-sample window")

    m = sub.add_parser("montecarlo"); m.add_argument("run_id")
    m.add_argument("--method", default="trade_shuffle"); m.add_argument("--sims", type=int, default=1000)
    m.add_argument("--seed", type=int, default=42); m.add_argument("--block", type=int, default=1)

    o = sub.add_parser("optimize"); common(o)
    o.add_argument("--start", required=True); o.add_argument("--end", required=True)
    o.add_argument("--space", type=json.loads, required=True); o.add_argument("--method", default="grid")
    o.add_argument("--trials", type=int, default=60); o.add_argument("--select-by", default="sharpe")
    o.add_argument("--seed", type=int, default=42); o.add_argument("--min-trades", type=int, default=10)
    se = sub.add_parser("sensitivity"); common(se)
    se.add_argument("--start", required=True); se.add_argument("--end", required=True)
    se.add_argument("--space", type=json.loads, required=True); se.add_argument("--steps", type=int, default=2)
    se.add_argument("--select-by", default="sharpe"); se.add_argument("--pairwise", default=None)
    rb = sub.add_parser("robustness"); common(rb)
    rb.add_argument("--start", required=True); rb.add_argument("--end", required=True)
    rb.add_argument("--subsamples", type=int, default=3)
    cs = sub.add_parser("costsweep"); common(cs)
    cs.add_argument("--start", required=True); cs.add_argument("--end", required=True)
    cs.add_argument("--multipliers", type=lambda s: [float(x) for x in s.split(",")], default=None,
                    help="comma-separated, default 0,0.5,1,1.5,2,3,5")
    cs.add_argument("--scale", default="costs", choices=("costs", "slippage", "both"))
    st = sub.add_parser("study"); st.add_argument("strategy")
    st.add_argument("--start", required=True); st.add_argument("--end", required=True)
    st.add_argument("--space", type=json.loads, default=None); st.add_argument("--trials", type=int, default=30)
    st.add_argument("--method", default="adaptive"); st.add_argument("--universe", type=lambda s: s.split(","),
                                                                        default=None)
    cv = sub.add_parser("cpcv"); common(cv)
    cv.add_argument("--start", required=True); cv.add_argument("--end", required=True)
    cv.add_argument("--groups", type=int, default=6, help="N groups of sessions")
    cv.add_argument("--test-groups", type=int, default=2, help="k groups tested per split: C(N, k) splits")
    cv.add_argument("--candidates", type=json.loads, default=None, help="JSON list of param objects")
    cv.add_argument("--select-by", default="sharpe", choices=("sharpe", "sortino", "total_return"))
    cv.add_argument("--purge", type=int, default=0, help="sessions dropped from the end of a training group "
                                                         "before a test group")
    cv.add_argument("--embargo", type=int, default=0, help="sessions dropped from the start of a training group "
                                                           "after a test group")
    pb = sub.add_parser("pbo"); pb.add_argument("run_id", nargs="?", default=None,
                                                help="a parent run whose trials share its window (an optimisation)")
    pb.add_argument("--matrix", default=None, help="CSV / JSON of returns: rows = sessions, columns = trials")
    pb.add_argument("--partitions", type=int, default=16); pb.add_argument("--metric", default="sharpe",
                                                                           choices=("sharpe", "mean", "total_return"))

    sub.add_parser("list")
    s = sub.add_parser("show"); s.add_argument("run_id")

    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from backtest import service, store

    def request():
        req = {"strategy_id": a.strategy, "params": a.params}
        for k, v in (("initial_capital", a.capital), ("universe", a.universe), ("cost_model", a.cost_model)):
            if v is not None:
                req[k] = v
        return req

    if a.cmd == "run":
        req = request()
        if a.periods:
            req.update({"periods": a.periods, "period_label": a.period or "full", "allow_test": a.allow_test})
        else:
            req.update({"start": a.start, "end": a.end})
        print(json.dumps(service.create_and_run(req), indent=2, default=str))
    elif a.cmd == "walkforward":
        from backtest.walkforward import run_walk_forward
        req = request(); req.update({"start": a.start, "end": a.end})
        out = run_walk_forward(req, a.train, a.validation, a.test, a.step, a.candidates, a.select_by,
                               a.purge, a.embargo)
        print(json.dumps({k: out[k] for k in ("run_id", "status", "oos_metrics", "purge_sessions", "embargo_sessions",
                                              "sessions_purged", "sessions_embargoed", "recommended_purge_sessions")},
                         indent=2, default=str))
    elif a.cmd == "optimize":
        from backtest.optimize import optimize
        req = request(); req.update({"start": a.start, "end": a.end})
        out = optimize(req, a.space, a.method, a.select_by, a.trials, a.seed, a.min_trades)
        print(json.dumps({k: out.get(k) for k in ("run_id", "status", "best", "diagnostics")}, indent=2, default=str))
    elif a.cmd == "sensitivity":
        from backtest.sensitivity import sensitivity
        req = request(); req.update({"start": a.start, "end": a.end})
        out = sensitivity(req, a.space, a.select_by, a.steps, pairwise=a.pairwise.split(",") if a.pairwise else None)
        print(json.dumps({k: out.get(k) for k in ("run_id", "robust_share", "knife_edges", "thin_edges", "parameters")}, indent=2,
                         default=str))
    elif a.cmd == "robustness":
        from backtest.robustness import robustness
        req = request(); req.update({"start": a.start, "end": a.end})
        out = robustness(req, a.subsamples)
        print(json.dumps({k: out.get(k) for k in ("run_id", "verdict", "score", "checks", "regimes", "monte_carlo")},
                         indent=2, default=str))
    elif a.cmd == "costsweep":
        from backtest.robustness import cost_sweep
        req = request(); req.update({"start": a.start, "end": a.end})
        out = cost_sweep(req, a.multipliers, a.scale)
        print(json.dumps({k: out.get(k) for k in ("run_id", "status", "scale", "break_even", "break_even_note",
                                                  "monotone", "points", "notes")}, indent=2, default=str))
    elif a.cmd == "study":
        from backtest.study import run_study
        out = run_study(a.strategy, a.space, a.start, a.end, method=a.method, max_trials=a.trials,
                        universe=a.universe)
        print(json.dumps(out, indent=2, default=str))
    elif a.cmd == "cpcv":
        from backtest.cpcv import run_cpcv
        req = request(); req.update({"start": a.start, "end": a.end})
        out = run_cpcv(req, a.groups, a.test_groups, a.candidates, a.select_by, a.purge, a.embargo)
        print(json.dumps({k: out.get(k) for k in ("run_id", "status", "n_groups", "k_test", "n_splits", "n_paths",
                                                  "distribution", "selection_frequency", "pbo", "notes")},
                         indent=2, default=str))
    elif a.cmd == "pbo":
        from backtest import cpcv as CV
        if bool(a.run_id) == bool(a.matrix):
            ap.error("pbo needs a run_id or --matrix (one of them)")
        if a.matrix:
            mat, labels = CV.load_matrix(a.matrix)
            out = CV.pbo(mat, a.partitions, a.metric, labels=labels)
        else:
            out = CV.pbo_for_run(a.run_id, a.partitions, a.metric)
        print(json.dumps({k: v for k, v in out.items() if k != "logit_values"}, indent=2, default=str))
    elif a.cmd == "montecarlo":
        print(json.dumps(service.run_montecarlo(a.run_id, a.method, a.sims, a.seed, a.block), indent=2, default=str))
    else:
        from db.schema import get_connection
        conn = get_connection()
        try:
            if a.cmd == "list":
                for r_ in store.list_runs(conn):
                    m_ = r_.get("metrics") or {}
                    print(f"{r_['run_id']}  {r_['kind']:<13} {r_['strategy_id']:<12} {r_['period_label'] or '':<10} "
                          f"{r_['start_date']}..{r_['end_date']}  {r_['status']:<9} "
                          f"ret {m_.get('total_return')}  mdd {m_.get('max_drawdown')}")
            else:
                print(json.dumps(store.get_run(conn, a.run_id), indent=2, default=str))
        finally:
            conn.close()


if __name__ == "__main__":
    main()
