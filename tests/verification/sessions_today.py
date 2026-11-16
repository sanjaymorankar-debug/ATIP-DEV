"""Per-session capital and P&L for a day, through the app's own ledger code, on a DB snapshot (read-only use)."""
import json, sqlite3, sys
from datetime import date
DB, DAY = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else str(date.today())
c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
from orders import sessions as S
out = {"day": DAY, "sessions": []}
for s in S.list_sessions(c)["sessions"]:
    rules = S._linked_rules(c, s["id"])
    tr = S._trades(rules, S._charges(c, rules), since=s.get("pool_started_at"))
    L = s["pool"] or {}
    today_in = [t for t in tr if t["date"] == DAY]
    today_out = [t for t in tr if t["closed"] and t["exit_date"] == DAY]
    realised_today = round(sum(t["realised_by_day"].get(DAY, 0.0) for t in tr), 2)
    closed = [t for t in tr if t["closed"]]
    out["sessions"].append({
        "id": s["id"], "name": s["name"], "status": s["status"], "mode": s["mode"], "auto_pick": s.get("auto_pick"),
        "allocated": L.get("start_capital"), "cash": L.get("cash"), "deployed_cost": round(sum(h["cost"] for h in L.get("open", [])), 2),
        "open_value": L.get("open_value"), "unrealised": L.get("unrealised"), "booked_all": L.get("booked"),
        "equity": L.get("equity"), "open": [(h["symbol"], h["qty"], h["entry"], h["mark"], h["since"]) for h in L.get("open", [])],
        "entries_today": [(t["symbol"], t["qty"], t["entry"], t["at"]) for t in today_in],
        "exits_today": [(t["symbol"], t["exit_price"], t["pnl"]) for t in today_out],
        "realised_today": realised_today, "closed_all": len(closed), "wins_all": sum(1 for t in closed if t["pnl"] > 0),
        "closed_today": len(today_out), "wins_today": sum(1 for t in today_out if t["pnl"] > 0),
        "waiting": L.get("waiting"), "floor": L.get("floor")})
out["spare"] = S.spare_paper_cash(c)
json.dump(out, sys.stdout, indent=1, default=str)
