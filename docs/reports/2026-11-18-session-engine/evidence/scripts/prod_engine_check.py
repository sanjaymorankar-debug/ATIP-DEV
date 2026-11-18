"""Prod rehearsal (18 Nov 2026): the session engine on a PostgreSQL copy of the last live data (Mac, 17 Nov 11:58 IST).
Run from srv/prod with ATIP_DATABASE_URL set. Prints JSON evidence; writes only to that copy."""
import json, sys
from db.schema import get_connection
import orders.broker as B
from orders import session_engine as E, sessions as S

out = {"as_of_ist": E.ist_now()}
c = get_connection()
out["backend"] = type(c).__module__
cols = [r[1] for r in c.execute("PRAGMA table_info(order_log)").fetchall()]
out["order_log_had_session_id"] = "session_id" in cols
B._ensure_order_log_table(c)
out["order_log_by_session"] = {r[0]: r[1] for r in c.execute(
    "SELECT session_id, COUNT(*) FROM order_log GROUP BY session_id ORDER BY session_id").fetchall()}
E.ensure_tables(c)
out["sync"] = E.sync(c)
out["sessions"] = {}
for s in S.list_sessions(c)["sessions"]:
    st = E.stats(c, s["id"])
    if st:
        out["sessions"][s["id"]] = {k: st[k] for k in ("state", "seed_capital", "transfers", "capital", "cash_available",
                                                        "invested_value", "realised_pnl", "unrealised_pnl", "current_balance",
                                                        "growth_pct", "legs", "completed_trades", "open_trades", "wins",
                                                        "losses", "win_rate_pct", "avg_pnl_per_trade", "floor")}
        out["sessions"][s["id"]]["ledger_rows"] = len(E.ledger_rows(c, s["id"]))
        out["sessions"][s["id"]]["trade_rows"] = len(E.trades(c, s["id"]))
out["check_would_change"] = E.check(c, do_sync=False)
out["plan"] = E.plan(c)
out["rebase_dry_run"] = E.rebase(c, ack=False)
try:
    E.create(c, None, True)
    out["create"] = "created"
except ValueError as e:
    out["create_refused"] = str(e)
out["alerts"] = E.alerts(c)
c.close()
print(json.dumps(out, indent=1, default=str))
