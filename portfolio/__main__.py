"""
W25 portfolio risk CLI.

    python -m portfolio risk      [--book PAPER|LIVE] [--lookback 250] [--days 60] [--store]
    python -m portfolio optimize  --symbols A,B,C | --book LIVE  [--objective min_variance|mean_variance|
                                  max_sharpe|risk_parity] [--max-weight 0.2] [--sector-cap 0.35]
    python -m portfolio rebalance --opt OPT... [--book PAPER] [--band 1.0] [--equity N]
    python -m portfolio snapshot  (the post-market job, now; respects portfolio_risk.snapshot_enabled)

The emergency exit is python -m portfolio.emergency (plan / flatten).
"""

import argparse
import json


def main():
    ap = argparse.ArgumentParser(prog="python -m portfolio")
    ap.add_argument("cmd", choices=["risk", "optimize", "rebalance", "snapshot"])
    ap.add_argument("--book", default="PAPER")
    ap.add_argument("--lookback", type=int, default=250)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--store", action="store_true")
    ap.add_argument("--symbols")
    ap.add_argument("--objective", default="min_variance")
    ap.add_argument("--max-weight", type=float, default=0.2)
    ap.add_argument("--sector-cap", type=float, default=0.35)
    ap.add_argument("--opt")
    ap.add_argument("--band", type=float, default=1.0)
    ap.add_argument("--equity", type=float)
    a = ap.parse_args()
    from db.schema import get_connection
    from portfolio import optimize as OPT, risk as RK
    if a.cmd == "snapshot":
        print(json.dumps(RK.run_scheduled(), indent=2, default=str))
        return
    conn = get_connection()
    try:
        if a.cmd == "risk":
            r = RK.analyse(conn, a.book.upper(), a.lookback, a.days)
            if a.store and r.get("coverage"):
                RK.store_snapshot(conn, r)
            print(json.dumps({"headline": RK.headline(r), **r}, indent=2, default=str))
        elif a.cmd == "optimize":
            syms = [s.strip().upper() for s in a.symbols.split(",")] if a.symbols else \
                [p["symbol"] for p in RK.book_snapshot(conn, a.book.upper())["positions"]]
            print(json.dumps(OPT.optimise(conn, syms, a.objective, max_weight=a.max_weight,
                                          sector_cap=a.sector_cap, lookback=a.lookback), indent=2, default=str))
        else:
            o = OPT.get_optimization(conn, a.opt) if a.opt else None
            if not o:
                raise SystemExit("--opt <opt_id> from an optimize run is required")
            print(json.dumps(OPT.rebalance_plan(conn, o["weights"], a.book.upper(), band_pct=a.band,
                                                equity=a.equity), indent=2, default=str))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
