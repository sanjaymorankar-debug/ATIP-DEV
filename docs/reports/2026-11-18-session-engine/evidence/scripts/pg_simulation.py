"""The 10 x 500 simulation of tests/test_session_engine_2026_11_18.py on PostgreSQL (the VPS backend). Run from srv/pgsim."""
import json, sys, time
sys.path.insert(0, sys.argv[1])
from db.schema import init_db, get_connection
from orders import session_engine as E, sessions as S
import tests.test_session_engine_2026_11_18 as T

t0 = time.time()
init_db()
c = get_connection()
out = {"backend": type(c).__module__, "started_ist": E.ist_now()}
expected = T._simulate(c, 10, 500)
out["check"] = E.check(c)
rows = {}
for k in range(1, 11):
    sid = f"S{k}"
    s = S.get_session(c, sid)
    final = json.loads(s["final_ledger"])
    led = E.ledger_rows(c, sid)
    trs = E.trades(c, sid)
    want = round(expected[sid], 2)
    rows[sid] = {"expected": want, "final_cash": final["cash_available"], "ledger_last": led[-1]["balance_after"],
                 "ledger_sum": round(sum(r["amount"] for r in led if r["type"] != "PNL"), 2),
                 "trades": len(trs), "legs": final["legs"], "completed": final["completed_trades"], "status": s["status"],
                 "to_the_paisa": final["cash_available"] == want == led[-1]["balance_after"]}
out["sessions"] = rows
out["all_to_the_paisa"] = all(r["to_the_paisa"] for r in rows.values())
out["seconds"] = round(time.time() - t0, 1)
out["finished_ist"] = E.ist_now()
c.close()
print(json.dumps(out, indent=1))
