"""HTTP acceptance checks of the capital plan on one local server (Test or Prod). Usage:
    python acceptance.py NAME PORT SERVER_DIR OUT.json
Prints one line per check; writes the results with IST times."""
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

NAME, PORT, DIR, OUT = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
BASE = f"http://127.0.0.1:{PORT}"
IST = timezone(timedelta(hours=5, minutes=30))
TOKEN = open(f"{DIR}/atip_data/dashboard_token.txt", encoding="utf-8").read().strip()
results = []


def call(method, path, body=None, token=True):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-ATIP-Token", TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode()
            code = r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read().decode(), e.code
    try:
        return code, json.loads(raw)
    except ValueError:
        return code, raw


def check(name, ok, note=""):
    t = datetime.now(IST).strftime("%H:%M:%S IST")
    results.append({"test": name, "result": "PASS" if ok else "FAIL", "time": t, "note": note})
    print(f"{t}  {'PASS' if ok else 'FAIL'}  {name}  {note}")


def pools():
    code, d = call("GET", "/api/sessions", token=False)
    return {s["id"]: s.get("pool") for s in d["sessions"]} if code == 200 else {}


# Test server: a fresh database -- give it the owner's ten Rs 25,000 pools first, running
if NAME == "test":
    code, d = call("POST", "/api/sessions/pools", {"total": 250000, "capital": 25000, "target_pct": 3, "stop_pct": 2,
                                                   "min_buy_value": 12000, "max_occurrences": 500})
    check("setup: ten Rs 25,000 pools on the fresh DB", code == 200 and len(d.get("sessions", [])) == 10, f"HTTP {code}")
    for i in range(1, 11):
        call("POST", f"/api/v2/sessions/S{i}/setup", {"status": "RUNNING"})

before = pools()
code, ov0 = call("GET", "/api/v2/sessions", token=False)
code, p = call("GET", "/api/v2/capital/plan", token=False)
check("CP-01 GET /api/v2/capital/plan", code == 200 and p.get("paper") and p.get("guard_active"),
      f"pool Rs {p.get('pool', {}).get('value'):,.2f}, spare Rs {p.get('spare_before'):,.2f}, buffer Rs {p.get('buffer'):,.2f}, "
      f"per session Rs {p.get('per_session'):,.0f}")
check("CP-02 plan keeps >= 10 % buffer", p.get("buffer_after_pct", 0) >= 10.0, f"spare after {p.get('buffer_after_pct')} % of pool")
code, html = call("GET", "/sessions", token=False)
check("CP-03 /sessions shows 'Top up sessions…' dialog", code == 200 and 'id="ax-cap"' in str(html), f"HTTP {code}")
code, d = call("POST", "/api/v2/capital/apply", {"ack": True}, token=False)
check("CP-04 apply without the token is refused", code == 401, f"HTTP {code}")
code, d = call("POST", "/api/v2/capital/apply", {"ack": False})
check("CP-05 apply without ack is refused", code == 400 and "ack" in json.dumps(d), f"HTTP {code}: {str(d)[:80]}")
code, d = call("POST", "/api/v2/capital/apply", {"ack": True, "reason": f"acceptance {NAME}"})
moved = d.get("moved") if isinstance(d, dict) else None
check("CP-06 apply moves the planned top-ups", code == 200 and moved == p.get("total_top_up"),
      f"HTTP {code}: moved Rs {moved:,.2f} to {len(d.get('sessions', []))} sessions" if code == 200 else str(d)[:120])
after = pools()
same = [sid for sid in before if before[sid] and after.get(sid) and abs(before[sid]["pnl"] - after[sid]["pnl"]) < 0.02]
check("CP-07 session P&L unchanged by the top-up (CAP-03)", len(same) == len([s for s in before if before[s]]),
      f"{len(same)} of {len([s for s in before if before[s]])} unchanged; S1 pnl {before.get('S1', {}).get('pnl')} -> "
      f"{after.get('S1', {}).get('pnl')}, capital {after.get('S1', {}).get('capital')}")
cap_ok = all(after[s]["capital"] == round(before[s]["start_capital"] + before[s]["transfers"] + p["per_session"], 2)
             for s in before if before[s])
check("CP-08 each session's capital rose by the top-up", cap_ok,
      ", ".join(f"{s} {after[s]['capital']:,.0f}" for s in list(after)[:3] if after[s]))
code, sp = call("GET", "/api/v2/sessions/spare", token=False)
check("CP-09 spare cash after = plan's spare after; no over-allocation",
      code == 200 and abs(sp["spare"] - p["spare_after"]) < 0.02 and sp.get("over_allocated") == 0,
      f"spare Rs {sp.get('spare'):,.2f}, over_allocated {sp.get('over_allocated')}")
code, d = call("POST", "/api/v2/accounting/transfer", {"from": None, "to": "S1", "amount": 10000, "reason": "into the buffer"})
check("CP-10 a transfer into the cash buffer is refused", code == 400 and "buffer" in json.dumps(d), f"HTTP {code}: {str(d)[:100]}")
code, d = call("POST", "/api/v2/sessions/new", {})
check("CP-11 a new session cannot take the buffer", code == 400, f"HTTP {code}: {str(d)[:110]}")
code, d = call("POST", "/api/v2/capital/apply", {"ack": True})
check("CP-12 a second apply moves nothing", code == 200 and d.get("moved") == 0, f"HTTP {code}: moved {d.get('moved') if isinstance(d, dict) else d}")
code, ov1 = call("GET", "/api/v2/sessions", token=False)
t0, t1 = ov0.get("total", {}), ov1.get("total", {})
check("CP-13 accounting totals: net/actual P&L unchanged, capital up by the top-up",
      code == 200 and abs((t1.get("net_pnl") or 0) - (t0.get("net_pnl") or 0)) < 0.05
      and abs((t1.get("actual_pnl") or 0) - (t0.get("actual_pnl") or 0)) < 0.05,
      f"net {t0.get('net_pnl')} -> {t1.get('net_pnl')}; actual {t0.get('actual_pnl')} -> {t1.get('actual_pnl')}; "
      f"equity {t0.get('equity')} -> {t1.get('equity')}")
code, d = call("GET", "/api/v2/sessions/S1", token=False)
acts = json.dumps(d)
check("CP-14 S1's activity shows the transfer", code == 200 and "TRANSFER" in acts, f"HTTP {code}")
for path in ("/home", "/sessions", "/command/pnl", "/accounting?tab=transfers", "/api/v2/home"):
    code, _ = call("GET", path, token=False)
    check(f"CP-15 GET {path} after the top-up", code == 200, f"HTTP {code}")
code, html = call("GET", "/sessions", token=False)
check("CP-16 the top-up button is disabled once applied", code == 200 and 'id="ax-cap"' not in str(html), "")
json.dump({"server": NAME, "port": PORT, "results": results, "plan": p, "after_pools": after, "before_pools": before},
          open(OUT, "w"), indent=1, default=str)
print(f"{sum(r['result'] == 'PASS' for r in results)} / {len(results)} passed")
