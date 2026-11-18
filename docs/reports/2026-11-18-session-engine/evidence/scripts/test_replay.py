"""Test-server replay (18 Nov 2026, after the close): drive the Rs 25K sessions made on the Test server through the REAL
order path -- orders.rules -> orders.broker -> orders.paper -- with a scripted quote feed, then run the session engine's
check as the dashboard's order monitor does. Run from srv/test (its own config.json and atip.db; no credentials):

    ATIP_DB_PATH=$PWD/atip.db PYTHONPATH=<worktree> python ../../test_replay.py

    S11  ACTIVE: two winning round trips (+3 %), so its cash compounds
    S12  ACTIVE -> PAUSED - BELOW FLOOR: bought, then a gap through its 2 % stop to -25 %
    S13  ACTIVE -> COMPLETED: max_occurrences 2 for the replay, two round trips, then frozen
Every order is PAPER. Prints JSON evidence."""
import json
from datetime import datetime, timedelta

import pandas as pd

import data.dhan as D
import orders.broker as B
import orders.paper as P
from db.schema import get_connection
from orders import rules, session_engine as E, sessions as S

KNOWN = {"RPLA": "910001", "RPLB": "910002", "RPLC": "910003", "RPLD": "910004", "RPLE": "910005"}
prices = {}


def quotes(symbols, *a, **k):
    ts = datetime.now()
    return pd.DataFrame([{"symbol": s, "ltp": prices[s], "prev_close": None, "timestamp": ts} for s in symbols if s in prices],
                        columns=["symbol", "ltp", "prev_close", "timestamp"])


def sec(symbol):
    s = str(symbol).upper()
    return {"security_id": KNOWN[s], "exchange": "NSE_EQ"} if s in KNOWN else None


def no_dhan(*a, **k):
    raise RuntimeError("no Dhan client on the Test server")


rules.get_security_id = sec
B.get_security_id = sec
B.get_dhan_client = no_dhan
for mod in (rules, B, D):
    mod.fetch_live_quotes = quotes
P.PaperBroker._quote_cache = {}
P.PaperBroker.QUOTE_TTL_SECONDS = 0.0
rules.init_orders_table()


def conn_do(fn, *a, **k):
    c = get_connection()
    try:
        return fn(c, *a, **k)
    finally:
        c.close()


def entry(sid, symbol, trigger):
    r = rules.create_rule({"symbol": symbol, "side": "BUY", "trigger_type": "PRICE", "trigger_value": trigger,
                           "reference_price": trigger * 1.02, "quantity_type": "SHARES", "quantity_value": 1,
                           "require_confirmation": False, "notes": "Test-server replay"})
    conn_do(S.link_rule, sid, r["id"])
    return r["id"]


def tick(**p):
    prices.update(p)
    return rules.check_triggers()


out = {"started_ist": E.ist_now()}
# run 2: the first run's entries were refused by the daily-loss limit (no P&L history on a fresh database); retire them
out["cancelled_from_run1"] = conn_do(lambda c: (c.execute("UPDATE order_rules SET status = 'CANCELLED' WHERE notes = "
                                                          "'Test-server replay' AND status <> 'EXECUTED'"), c.commit(),
                                                c.execute("SELECT changes()").fetchone()[0])[2])
for sid in ("S11", "S12", "S13"):
    if conn_do(S.get_session, sid)["status"] != "RUNNING":
        conn_do(E.activate, sid, True)
conn_do(S.update_session, "S13", {"max_occurrences": 2})
for sid in ("S11", "S12", "S13"):                     # the replay's own entries only: auto-pick has no scores here
    conn_do(S.update_session, sid, {"auto_pick": False})

entry("S11", "RPLA", 100.0); tick(RPLA=100.0); tick(RPLA=103.0)
entry("S11", "RPLB", 50.0); tick(RPLB=50.0); tick(RPLB=51.5)
entry("S12", "RPLC", 200.0); tick(RPLC=200.0); tick(RPLC=150.0)
entry("S13", "RPLD", 80.0); tick(RPLD=80.0); tick(RPLD=82.4)
entry("S13", "RPLE", 40.0); tick(RPLE=40.0); tick(RPLE=41.2)
out["engine_check"] = conn_do(E.check)
out["after"] = {sid: {k: v for k, v in conn_do(E.stats, sid).items() if k != "open"} for sid in ("S11", "S12", "S13")}
out["order_log"] = [dict(r) for r in conn_do(lambda c: c.execute(
    "SELECT timestamp, symbol, transaction_type, quantity, status, session_id FROM order_log ORDER BY id").fetchall())]
out["alerts"] = conn_do(E.alerts)
out["state_events"] = [dict(r) for r in conn_do(lambda c: c.execute(
    "SELECT session_id, at, detail FROM session_events WHERE event = 'STATE' ORDER BY id").fetchall())]
out["finished_ist"] = E.ist_now()
print(json.dumps(out, indent=1, default=str))
