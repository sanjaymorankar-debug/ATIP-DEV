"""
    python -m backtest run --strategy dip --start 2025-06-01 --end 2026-09-23
    python -m backtest run --strategy dip --params '{"stop_pct": 4}' --capital 500000
    python -m backtest run --strategy dip --periods '{"research":["2025-02-01","2025-12-31"],
                           "validation":["2026-01-01","2026-04-30"],"test":["2026-05-01","2026-09-23"]}'
                           --period validation
    python -m backtest walkforward --strategy dip --start 2025-03-01 --end 2026-09-23
                           --train 120 --validation 40 --test 40 --step 40
                           --candidates '[{"stop_pct":3},{"stop_pct":5}]'
    python -m backtest montecarlo RUN_ID --method trade_shuffle --sims 1000 --seed 42
    python -m backtest list
    python -m backtest show RUN_ID
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

    m = sub.add_parser("montecarlo"); m.add_argument("run_id")
    m.add_argument("--method", default="trade_shuffle"); m.add_argument("--sims", type=int, default=1000)
    m.add_argument("--seed", type=int, default=42); m.add_argument("--block", type=int, default=1)

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
        out = run_walk_forward(req, a.train, a.validation, a.test, a.step, a.candidates, a.select_by)
        print(json.dumps({k: out[k] for k in ("run_id", "status", "oos_metrics")}, indent=2, default=str))
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
