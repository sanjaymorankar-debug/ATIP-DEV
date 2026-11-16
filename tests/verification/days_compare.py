"""Today vs the previous 5 trading sessions from a snapshot: realised P&L (net of charges), trades closed, win rate,
entries, signals, utilisation (paper positions at cost / paper equity, from pnl_daily PAPER). Uses the app's _trades."""
import json, sqlite3, sys
from orders import sessions as S
DB = sys.argv[1]
c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
days = [r[0] for r in c.execute("SELECT DISTINCT date FROM ai_scores WHERE date <= '2026-11-16' ORDER BY date DESC LIMIT 6")][::-1]
tr = []
for s in S.list_sessions(c)["sessions"]:
    rules = S._linked_rules(c, s["id"])
    for t in S._trades(rules, S._charges(c, rules), since=s.get("pool_started_at")):
        tr.append({**t, "sid": s["id"]})
pnl = {r["date"]: dict(r) for r in c.execute("SELECT * FROM pnl_daily WHERE env='PAPER'")}
out = []
for d in days:
    closed = [t for t in tr if t["closed"] and t["exit_date"] == d]
    gross = sum((t["exit_price"] - t["entry"]) * t["qty"] for t in closed)
    net = sum(t["realised_by_day"].get(d, 0.0) for t in tr)
    wins = sum(1 for t in closed if t["pnl"] > 0)
    prev = c.execute("SELECT MAX(date) FROM ai_scores WHERE date < ?", (d,)).fetchone()[0]
    sig = {r[0]: r[1] for r in c.execute("SELECT `signal`, COUNT(*) FROM ai_scores WHERE date=? GROUP BY 1", (prev,))}
    p = pnl.get(d) or {}
    eq = p.get("equity"); cost = p.get("cost")
    out.append({"date": d, "entries": sum(1 for t in tr if t["date"] == d), "closed": len(closed), "wins": wins,
                "win_rate": round(wins / len(closed) * 100, 1) if closed else None, "gross": round(gross, 2),
                "net": round(net, 2), "charges": round(gross - sum(t["pnl"] for t in closed), 2),
                "signals_used_from": prev, "buy_signals": sig.get("BUY", 0), "hold": sig.get("HOLD", 0),
                "utilisation_pct": round(cost / eq * 100, 1) if eq and cost is not None else None,
                "positions": p.get("n_positions"), "equity": eq, "day_pnl": p.get("day_pnl")})
# occupancy: share of (session, trading day) pairs that held a position at the close, since 30 Oct
alld = [r[0] for r in c.execute("SELECT DISTINCT date FROM ai_scores WHERE date >= '2026-10-30' AND date <= '2026-11-16' ORDER BY date")]
occ = {}
for s in sorted({t["sid"] for t in tr}, key=lambda x: int(x[1:])):
    held = 0
    for d in alld:
        if any(t["sid"] == s and t["date"] <= d and (not t["closed"] or (t["exit_date"] or "") > d) for t in tr):
            held += 1
    occ[s] = round(held / len(alld), 3)
print(json.dumps({"days": out, "occupancy": occ, "occupancy_days": len(alld)}, indent=1, default=str))
