"""
    python -m execution status                     # mode, live gate, kill switch, pending intents
    python -m execution limits [--set key=value ...]
    python -m execution evaluate [--as-of 2026-09-24]   # risk only
    python -m execution run [--execute]            # one cycle; --execute sends APPROVED orders (PAPER)
    python -m execution order RISK_DECISION_ID     # create + submit one PAPER order
    python -m execution cancel ORDER_ID
    python -m execution audit --intent ID | --order ID
    python -m execution exposure
"""

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m execution")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status"); sub.add_parser("exposure")
    l = sub.add_parser("limits"); l.add_argument("--set", nargs="*", default=[])
    e = sub.add_parser("evaluate"); e.add_argument("--as-of")
    r = sub.add_parser("run"); r.add_argument("--execute", action="store_true"); r.add_argument("--as-of")
    o = sub.add_parser("order"); o.add_argument("risk_decision_id")
    c = sub.add_parser("cancel"); c.add_argument("order_id")
    a = sub.add_parser("audit"); a.add_argument("--intent"); a.add_argument("--order")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from db.schema import get_connection
    from execution import audit, config, order_manager, pipeline, positions, risk_engine
    if args.cmd == "run":
        out = pipeline.run_execution_cycle(execute=args.execute, as_of=args.as_of)
    else:
        conn = get_connection()
        try:
            if args.cmd == "status":
                ok, why = config.live_gate()
                out = {"settings": config.execution_settings(), "live_allowed": ok, "live_gate": why,
                       "pending_intents": len(pipeline.pending_intents(conn))}
            elif args.cmd == "limits":
                if args.set:
                    ch = {}
                    for kv in args.set:
                        k, v = kv.split("=", 1)
                        ch[k] = None if v.lower() == "null" else ("default" if v == "default" else float(v))
                    config.set_risk_limits(conn, ch, actor="cli")
                out = config.risk_limits(conn)
            elif args.cmd == "evaluate":
                out = []
                for iid in pipeline.pending_intents(conn, args.as_of):
                    out.append(risk_engine.evaluate(conn, iid).as_dict())
            elif args.cmd == "order":
                oo = order_manager.create_order(conn, args.risk_decision_id)
                out = order_manager.submit_order(conn, oo["order_id"])
            elif args.cmd == "cancel":
                out = order_manager.cancel_order(conn, args.order_id)
            elif args.cmd == "audit":
                out = audit.trail(conn, intent_id=args.intent, order_id=args.order)
            elif args.cmd == "exposure":
                out = positions.exposure(conn)
        finally:
            conn.close()
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
