"""python -m research report SYMBOL | run [--symbols ...] | evaluate | hit-rate | ratings [--rating BUY]"""

import argparse
import json
import logging

from research import report as R


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(prog="python -m research", description="W39 equity research reports")
    sub = ap.add_subparsers(dest="cmd", required=True)
    one = sub.add_parser("report")
    one.add_argument("symbol")
    run = sub.add_parser("run")
    run.add_argument("--symbols", nargs="+")
    sub.add_parser("evaluate")
    sub.add_parser("hit-rate")
    rt = sub.add_parser("ratings")
    rt.add_argument("--rating")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        out = R.run_reports(a.symbols)
    else:
        from db.schema import get_connection
        conn = get_connection()
        try:
            out = {"report": lambda: R.build_report(conn, a.symbol),
                   "evaluate": lambda: R.evaluate_targets(conn),
                   "hit-rate": lambda: R.hit_rate(conn),
                   "ratings": lambda: R.latest_ratings(conn, a.rating)}[a.cmd]()
        finally:
            conn.close()
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
