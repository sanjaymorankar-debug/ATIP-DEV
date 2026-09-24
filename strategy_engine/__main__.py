"""
    python -m strategy_engine sync                         # register library definitions
    python -m strategy_engine list
    python -m strategy_engine show atip_zpi_momentum [--version 1.0.0]
    python -m strategy_engine validate path/to/definition.json
    python -m strategy_engine create path/to/definition.json
    python -m strategy_engine add-version path/to/definition.json
    python -m strategy_engine transition atip_zpi_momentum RESEARCH --reason "..."
    python -m strategy_engine decide atip_zpi_momentum [--as-of 2026-09-23] [--book PAPER] [--no-store]
    python -m strategy_engine health atip_zpi_momentum
    python -m strategy_engine regime-mapping [--set BULL '[{"strategy_id":"atip_zpi_momentum"}]' | --set BEAR NO_TRADE]
    python -m strategy_engine combined [--as-of 2026-09-23] [--mode vote|priority|weighted]
    python -m strategy_engine backtest atip_zpi_momentum --start 2025-06-01 --end 2026-09-23 [--version 1.0.0]
"""

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m strategy_engine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync"); sub.add_parser("list")
    s = sub.add_parser("show"); s.add_argument("strategy_id"); s.add_argument("--version")
    for name in ("validate", "create", "add-version"):
        p = sub.add_parser(name); p.add_argument("path")
    t = sub.add_parser("transition"); t.add_argument("strategy_id"); t.add_argument("to_state")
    t.add_argument("--reason", default="")
    d = sub.add_parser("decide"); d.add_argument("strategy_id"); d.add_argument("--version")
    d.add_argument("--as-of"); d.add_argument("--book", default="PAPER"); d.add_argument("--no-store", action="store_true")
    h = sub.add_parser("health"); h.add_argument("strategy_id")
    r = sub.add_parser("regime-mapping"); r.add_argument("--set", nargs=2, metavar=("REGIME", "ENTRIES"))
    c = sub.add_parser("combined"); c.add_argument("--as-of"); c.add_argument("--mode", default="vote")
    b = sub.add_parser("backtest"); b.add_argument("strategy_id"); b.add_argument("--version")
    b.add_argument("--start", required=True); b.add_argument("--end", required=True)
    b.add_argument("--params", type=json.loads, default=None); b.add_argument("--capital", type=float)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from db.schema import get_connection
    from strategy_engine import health, lifecycle, registry
    from strategy_engine.definition import validate
    out = None
    if a.cmd == "validate":
        out = validate(json.loads(Path(a.path).read_text(encoding="utf-8")))
    elif a.cmd == "decide":
        from strategy_engine.engine import generate_decisions
        out = generate_decisions(a.strategy_id, a.version, a.as_of, a.book, store=not a.no_store)
        out["decisions"] = [d for d in out["decisions"] if d["decision"] != "WAIT"][:50]
    elif a.cmd == "backtest":
        from backtest import service
        req = {"strategy_id": a.strategy_id, "start": a.start, "end": a.end}
        req["strategy_version"] = a.version or _current(a.strategy_id)
        if a.params: req["params"] = a.params
        if a.capital: req["initial_capital"] = a.capital
        out = service.create_and_run(req)
    else:
        conn = get_connection()
        try:
            if a.cmd == "sync":
                out = registry.sync_library(conn)
            elif a.cmd == "list":
                out = registry.list_strategies(conn)
            elif a.cmd == "show":
                out = {"strategy": registry.get_strategy(conn, a.strategy_id),
                       "version": registry.get_version(conn, a.strategy_id, a.version),
                       "versions": registry.list_versions(conn, a.strategy_id),
                       "lifecycle": lifecycle.history(conn, a.strategy_id)}
            elif a.cmd == "create":
                out = registry.create_strategy(conn, json.loads(Path(a.path).read_text(encoding="utf-8")))
            elif a.cmd == "add-version":
                out = registry.add_version(conn, json.loads(Path(a.path).read_text(encoding="utf-8")))
            elif a.cmd == "transition":
                out = lifecycle.transition(conn, a.strategy_id, a.to_state, a.reason)
            elif a.cmd == "health":
                out = health.compute_health(conn, a.strategy_id)
            elif a.cmd == "regime-mapping":
                from strategy_engine import selection
                if a.set:
                    entries = a.set[1] if a.set[1].upper() == "NO_TRADE" else json.loads(a.set[1])
                    selection.set_mapping(conn, a.set[0], "NO_TRADE" if entries == a.set[1] else entries)
                out = selection.get_mapping(conn)
            elif a.cmd == "combined":
                from strategy_engine import selection
                out = selection.combined_decisions(conn, a.as_of, a.mode)
        finally:
            conn.close()
    print(json.dumps(out, indent=2, default=str))


def _current(sid):
    from db.schema import get_connection
    from strategy_engine import registry
    conn = get_connection()
    try:
        s = registry.get_strategy(conn, sid)
        if not s:
            raise SystemExit(f"no strategy {sid} (python -m strategy_engine sync registers the library)")
        return s["current_version"]
    finally:
        conn.close()


if __name__ == "__main__":
    main()
