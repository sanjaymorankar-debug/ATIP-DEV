"""
    python -m quant sync                                  # factor registry, built-in factor set, composites
    python -m quant compute [--as-of 2026-09-24] [--factors mom_12_1,vol_60]
    python -m quant rank KEY [--as-of D] [--top 20] [--sector]   # KEY e.g. composite:mom_lowvol_liq@1, mom_12_1@1
    python -m quant pair-add '{"asset_a": "HDFCBANK", "asset_b": "ICICIBANK", "entry_z": 2}'
    python -m quant pair PAIR_ID [--as-of D] [--store]
    python -m quant screen SYM1,SYM2,... [--lookback 250]
    python -m quant portfolio '{"key": "composite:mom_lowvol_liq@1", "top_n": 20, "method": "inverse_vol"}'
    python -m quant research KEY --start D --end D [--kind ic|ic_decay|quantiles] [--horizon 5]
    python -m quant events                               # sync market_event from corporate actions + bulk deals
    python -m quant micro [--date D]                     # intraday features from live_quotes
"""

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m quant")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync"); sub.add_parser("events")
    c = sub.add_parser("compute"); c.add_argument("--as-of"); c.add_argument("--factors")
    r = sub.add_parser("rank"); r.add_argument("key"); r.add_argument("--as-of"); r.add_argument("--top", type=int, default=20)
    r.add_argument("--sector", action="store_true")
    pa = sub.add_parser("pair-add"); pa.add_argument("spec")
    p = sub.add_parser("pair"); p.add_argument("pair_id"); p.add_argument("--as-of"); p.add_argument("--store", action="store_true")
    s = sub.add_parser("screen"); s.add_argument("symbols"); s.add_argument("--lookback", type=int, default=250)
    pf = sub.add_parser("portfolio"); pf.add_argument("spec"); pf.add_argument("--as-of")
    rs = sub.add_parser("research"); rs.add_argument("key"); rs.add_argument("--start", required=True)
    rs.add_argument("--end", required=True); rs.add_argument("--kind", default="ic"); rs.add_argument("--horizon", type=int, default=5)
    m = sub.add_parser("micro"); m.add_argument("--date")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from db.schema import get_connection
    from quant import composite, engine, events, factors, microstructure, pairs, portfolio, research
    from quant.config import settings
    conn = get_connection()
    try:
        if a.cmd == "sync":
            out = {"factors": factors.sync(conn), "factor_set": factors.ensure_builtin_set(conn),
                   "composites": [c["name"] + "@" + c["version"] for c in composite.ensure_builtin(conn)]}
        elif a.cmd == "compute":
            factors.sync(conn); composite.ensure_builtin(conn)
            out = engine.compute(conn, a.as_of, a.factors.split(",") if a.factors else None, settings()["universe"],
                                 True, settings()["composites"])
        elif a.cmd == "rank":
            asof = a.as_of or conn.execute("SELECT MAX(as_of) FROM quant_factor_score").fetchone()[0]
            out = engine.sector_rankings(conn, asof, a.key) if a.sector else engine.rankings(conn, asof, a.key, a.top)
        elif a.cmd == "pair-add":
            out = pairs.save_pair(conn, json.loads(a.spec))
        elif a.cmd == "pair":
            out = pairs.analyze_pair(conn, a.pair_id, a.as_of, store=a.store)
        elif a.cmd == "screen":
            out = pairs.screen(conn, a.symbols.split(","), lookback=a.lookback)
        elif a.cmd == "portfolio":
            asof = a.as_of or conn.execute("SELECT MAX(as_of) FROM quant_factor_score").fetchone()[0]
            out = portfolio.build(conn, asof, json.loads(a.spec))
        elif a.cmd == "research":
            out = {"ic": lambda: research.factor_ic(conn, a.key, a.start, a.end, a.horizon),
                   "ic_decay": lambda: research.ic_decay(conn, a.key, a.start, a.end),
                   "quantiles": lambda: research.quantile_returns(conn, a.key, a.start, a.end, a.horizon)}[a.kind]()
        elif a.cmd == "events":
            out = events.sync_events(conn)
        elif a.cmd == "micro":
            out = microstructure.compute_day(conn, a.date)
    finally:
        conn.close()
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
