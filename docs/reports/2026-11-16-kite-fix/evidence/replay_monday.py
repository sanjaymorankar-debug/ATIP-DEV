"""
Replay of Monday 16 Nov 2026's first passes through the REAL KGF code on a copy of the live database (the "Prod" run).
The market is closed when this runs, so quotes come from a fake feed (13 Nov closes, then scripted ticks) and every
broker client is replaced by a recorder that fails the run if any order-changing method is touched. PAPER only.

    cd .ux_build/kite-fix-2026-11-16/replay
    ATIP_DB_PATH=$PWD/replay.db PYTHONPATH=<worktree> python replay_monday.py <snapshot.db>
"""
import json
import shutil
import sys
from datetime import datetime, date

import pandas as pd

SNAP = sys.argv[1]
shutil.copy(SNAP, "replay.db")

import data.dhan as D
import orders.broker as B
import orders.rules as RL
from orders import algo_gtt, arms, autopick, rules, sessions
from db.schema import get_connection

CALLS = []


class Recorder:
    def __getattr__(self, item):
        CALLS.append(item)
        if item.startswith(("place", "modify", "cancel", "exit_all", "kill", "convert")):
            raise AssertionError(f"ORDER-CHANGING CALL {item}")
        return lambda *a, **k: {"status": "success", "data": []}


PRICES = {}


def quotes(symbols, *a, **k):
    ts = datetime.now()
    return pd.DataFrame([{"symbol": s, "ltp": PRICES[s], "prev_close": None, "timestamp": ts} for s in symbols if s in PRICES],
                        columns=["symbol", "ltp", "prev_close", "timestamp"])


for m in (RL, B, D):
    m.fetch_live_quotes = quotes
B.get_dhan_client = lambda *a, **k: (Recorder(), None)
D.get_dhan_client = lambda *a, **k: (Recorder(), None)
algo_gtt.default_broker = lambda: algo_gtt.ReadOnlyBroker(Recorder())

conn = get_connection()
closes = {r[0]: r[1] for r in conn.execute(
    "SELECT symbol, close FROM prices_daily WHERE date = (SELECT MAX(date) FROM prices_daily WHERE series='EQ') AND series='EQ'")}
PRICES.update(closes)
out = {"started": datetime.now().isoformat(timespec="seconds"), "steps": []}


def step(name, data):
    out["steps"].append({"step": name, "data": data})
    print(f"\n== {name}")
    print(json.dumps(data, indent=1, default=str)[:3000])


step("arms", [{"id": a["id"], "name": a["name"], "gate": a["gate"] and {k: a["gate"][k] for k in ("open", "metric", "value")}}
              for a in arms.board(conn)])
held = {s["id"]: [h["symbol"] for h in (s.get("pool") or {}).get("open") or []] for s in sessions.list_sessions(conn)["sessions"]}
step("held at the start", held)

# 09:16 -- three open trades stop out (S3 retest arm, S5 control, S6 ATR arm), through their own STOP legs
for sid in ("S3", "S5", "S6"):
    for sym in held.get(sid) or []:
        stop = next((r for r in sessions._linked_rules(conn, sid) if r.get("role") == "STOP" and r["status"] == "ACTIVE"
                     and r["symbol"] == sym), None)
        if stop:
            PRICES[sym] = round(float(stop["resolved_trigger_price"]) * 0.999, 2)
ev = rules.check_triggers()
step("09:16 check_triggers (stops)", [{k: e.get(k) for k in ("type", "symbol", "status")} for e in ev])

# 09:20 -- auto-pick: the freed sessions pick under their arms
picks = autopick.run(conn, now=datetime.combine(date.today(), datetime.min.time()).replace(hour=9, minute=20))
step("09:20 auto-pick", [{k: e.get(k) for k in ("session", "type", "status", "symbol", "source", "trigger", "stoploss",
                                                 "price", "signal_close", "ltp_fresh", "quantity_est", "stop_pct", "reason",
                                                 "dummy_id")} for e in picks])
step("DUMMY records", [{k: d[k] for k in ("id", "kind", "symbol", "trigger_price", "stop_trigger", "status", "valid_until",
                                           "session_id")} for d in algo_gtt.listing(conn)])

# 10:05 -- every picked stock trades down to its trigger: the LIMIT entries fill in PAPER at min(LTP, trigger)
for e in picks:
    if e["type"] == "PICKED":
        PRICES[e["symbol"]] = round(float(e["trigger"]) * 0.998, 2)
ev = rules.check_triggers()
step("10:05 check_triggers (entries)", [{k: e.get(k) for k in ("type", "symbol", "status")} for e in ev])
fills = []
for e in picks:
    if e.get("rule_id"):
        r = rules.get_rule(e["rule_id"])
        legs = [l for l in rules.list_rules() if l.get("parent_rule_id") == r["id"]]
        fills.append({"session": e["session"], "symbol": r["symbol"], "status": r["status"], "limit": r["limit_price"],
                      "fill": r["execution_price"], "qty": r["execution_quantity"],
                      "value": round((r["execution_price"] or 0) * (r["execution_quantity"] or 0), 2),
                      "legs": [(l["role"], l["resolved_trigger_price"], l.get("trail_value"), l.get("trail_activate_price"),
                                l.get("not_before")) for l in legs], "max_hold_days": r.get("max_hold_days")})
step("fills and legs", fills)
step("algo sync", algo_gtt.sync(conn))
step("DUMMY records after the fills", [{k: d[k] for k in ("id", "kind", "symbol", "trigger_price", "stop_trigger",
                                                          "target_trigger", "status", "fill_price")} for d in algo_gtt.listing(conn)])
from decimal import Decimal
from orders.ticks import band_tick
bad = [(r["symbol"], r["role"], r["resolved_trigger_price"]) for r in rules.list_rules()
       if r["status"] in ("ACTIVE", "EXECUTED") and str(r.get("created_at") or "") >= out["started"]
       and Decimal(repr(float(r["resolved_trigger_price"]))) % band_tick(r["resolved_trigger_price"]) != 0]
step("off-tick prices on new rules", bad)
step("broker calls", {"calls": sorted(set(CALLS)), "order_changing": [c for c in CALLS if c.startswith(
    ("place", "modify", "cancel", "exit_all", "kill", "convert"))]})
out["finished"] = datetime.now().isoformat(timespec="seconds")
json.dump(out, open("replay_result.json", "w"), indent=1, default=str)
