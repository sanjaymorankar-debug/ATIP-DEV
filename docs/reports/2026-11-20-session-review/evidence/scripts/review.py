"""20 Nov 2026 session review (owner request ~22:50 IST): today's P&L per session, last week's profit, failing
strategies and the one-month loss analysis. Runs ATIP's own session code (orders/sessions.py) against a LOCAL restore of
the VPS's 19:15 IST pg_dump (never the VPS). Writes JSON + CSV to OUT.

    ATIP_DATABASE_URL=postgresql://... PYTHONPATH=<worktree>:<pylib> python review.py OUT
(cwd must hold atip_data/config.json with "database": {"backend": "postgresql", "allow_experimental": true})
"""
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
FROM, TODAY = "2026-10-20", "2026-11-20"

from db.schema import get_connection   # noqa: E402
from orders import sessions as S        # noqa: E402

c = get_connection()


def rows(sql, *a):
    cur = c.execute(sql, a)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


R = {"run": {"db_max_prices_daily": rows("SELECT MAX(date) AS d FROM prices_daily")[0]["d"]}}

# ── sessions, arms, ledgers ─────────────────────────────────────────────────────────────────────────────────────────
sess = S.list_sessions(c)["sessions"]
R["sessions"] = []
trades = []
for s in sess:
    sid = s["id"]
    L = S.ledger(c, sid, s) or {}
    arm = rows("SELECT value FROM session_settings WHERE name = ?", "arm:" + sid)
    R["sessions"].append({k: s.get(k) for k in ("id", "name", "strategy", "status", "auto_pick", "start_capital",
                                                "pool_started_at", "stop_pct", "target_pct", "trail_pct")}
                         | {"arm": json.loads(arm[0]["value"]) if arm else None}
                         | {k: L.get(k) for k in ("capital", "cash", "equity", "pnl", "return_pct", "booked", "unrealised",
                                                  "charges", "trades", "closed", "wins", "win_rate_pct", "transfers")}
                         | {"open": [{k: h[k] for k in ("symbol", "qty", "entry", "mark", "unrealised", "since", "stop")}
                                     for h in L.get("open", [])]})
    rules = S._linked_rules(c, sid)
    by_id = {r["id"]: r for r in rules}
    legs = defaultdict(list)
    for r in rules:
        if r.get("parent_rule_id"):
            legs[r["parent_rule_id"]].append(r)
    for t in S._trades(rules, S._charges(c, rules), since=s.get("pool_started_at")):
        e = by_id[t["rule_id"]]
        done = [l for l in legs[e["id"]] if l.get("status") == "EXECUTED" and l.get("execution_price")]
        x = done[-1] if done else {}
        stop_leg = next((l for l in legs[e["id"]] if l.get("role") == "STOP"), {})
        if x.get("role") == "STOP":
            act, hw = x.get("trail_activate_price"), x.get("trail_high_water")
            why = "trail stop" if act and hw and float(hw) >= float(act) else "stop-loss"
        elif x:
            why = {"TARGET": "target", "TIME": "time exit"}.get(x.get("role"), (x.get("role") or "manual").lower())
        else:
            why = None
        trades.append(t | {"session": sid, "notes": e.get("notes"), "created_at": str(e.get("created_at")),
                           "stop_pct_entry": e.get("bracket_stop_pct"), "target_pct_entry": e.get("bracket_target_pct"),
                           "stop_leg_trigger": stop_leg.get("resolved_trigger_price"),
                           "exit_role": x.get("role"), "exit_reason": why, "exit_at": str(x.get("updated_at") or "")})

# ── daily closes ───────────────────────────────────────────────────────────────────────────────────────────────────
syms = sorted({t["symbol"] for t in trades})
px = pd.DataFrame(rows("SELECT symbol, date, close FROM prices_daily WHERE date >= ? AND (series = 'EQ' OR series IS NULL) "
                       "AND symbol IN (" + ",".join("?" * len(syms)) + ")", "2026-09-01", *syms)) if syms else pd.DataFrame()
px["date"] = px["date"].astype(str).str[:10]
px = px.drop_duplicates(["symbol", "date"], keep="last")
close = {(r.symbol, r.date): float(r.close) for r in px.itertuples()}
days = sorted({d for d in px["date"] if FROM <= d <= TODAY})


def close_on(sym, d):
    return close.get((sym, d))


def prev_close(sym, d):
    ds = [x for x in days if x < d]
    for x in reversed(ds):
        v = close_on(sym, x)
        if v is not None:
            return v
    return None


# ── each trade's P&L by day: realised on its exit day, mark-to-market (close to close) while held ───────────────────
daily = defaultdict(float)          # (session, day) -> rupees
missing = []
for t in trades:
    if t["side"] != "BUY":
        continue
    d0 = t["date"]
    for d in days:
        if d < d0:
            continue
        if t["closed"] and d > t["exit_date"]:
            break
        ref = t["entry"] if d == d0 else (prev_close(t["symbol"], d) or t["entry"])
        if t["closed"] and d == t["exit_date"]:
            end = t["exit_price"]
        else:
            end = close_on(t["symbol"], d)
            if end is None:
                missing.append((t["symbol"], d)); end = ref
        v = (end - ref) * t["qty"]
        if t["closed"] and d == t["exit_date"]:
            v -= t["charges"]                       # entry + exit charges, booked when the trade closes
        elif not t["closed"] and d == d0:
            v -= t["charges"]                       # an open trade has paid its buy charges
        daily[(t["session"], d)] += v
R["missing_closes"] = sorted(set(missing))[:50]

# check: a closed trade's days add up to its booked P&L (exact up to partial exits)
chk = defaultdict(float)
for t in trades:
    if t["closed"] and t["side"] == "BUY":
        tot = 0.0
        for d in days:
            if t["date"] <= d <= t["exit_date"]:
                ref = t["entry"] if d == t["date"] else (prev_close(t["symbol"], d) or t["entry"])
                end = t["exit_price"] if d == t["exit_date"] else (close_on(t["symbol"], d) or ref)
                tot += (end - ref) * t["qty"]
        chk[t["rule_id"]] = round(tot - t["charges"] - t["pnl"], 2)
R["check_closed_trades_off_by"] = {k: v for k, v in chk.items() if abs(v) > 1}

sids = [s["id"] for s in sess]
dd = pd.DataFrame([{"day": d, **{sid: round(daily.get((sid, d), 0.0), 2) for sid in sids}} for d in days]).set_index("day")
dd["TOTAL"] = dd[sids].sum(axis=1)
dd.to_csv(OUT / "session_pnl_by_day.csv")
R["by_day"] = dd.reset_index().to_dict("records")

# ── today (the app's own 16:00 IST RCA too) ───────────────────────────────────────────────────────────────────────
R["today_mtm"] = dd.loc[TODAY].to_dict() if TODAY in dd.index else None
pm = rows("SELECT day, built_at, payload FROM post_market_rca WHERE day >= '2026-11-16' ORDER BY day")
R["post_market_rca"] = []
for r in pm:
    p = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"] or "{}")
    R["post_market_rca"].append({"day": str(r["day"]), "built_at": str(r["built_at"]), "headline": p.get("headline"),
                                 "totals": p.get("totals"), "sessions": p.get("sessions")})
R["today_closed"] = [t for t in trades if t["closed"] and t["exit_date"] == TODAY]
R["today_bought"] = [t for t in trades if t["date"] == TODAY]

# ── weeks ─────────────────────────────────────────────────────────────────────────────────────────────────────────
wk = dd.copy(); wk.index = pd.to_datetime(wk.index)
weekly = wk.groupby(wk.index.to_period("W-SUN")).sum().round(2)
weekly.index = [f"{p.start_time.date()}..{(p.end_time.date() - timedelta(days=2))}" for p in weekly.index]
weekly.to_csv(OUT / "session_pnl_by_week.csv")
R["by_week"] = weekly.reset_index().rename(columns={"index": "week"}).to_dict("records")

# ── trades: CSV + groupings ───────────────────────────────────────────────────────────────────────────────────────
T = pd.DataFrame(trades)
T.to_csv(OUT / "trades.csv", index=False)
json.dump(R, open(OUT / "review.json", "w"), indent=1, default=str)
print(json.dumps({"days": len(days), "trades": len(trades), "closed": int(T["closed"].sum()) if len(T) else 0,
                  "today": R["today_mtm"], "missing_closes": len(R["missing_closes"]),
                  "check_off": len(R["check_closed_trades_off_by"])}, default=str))
