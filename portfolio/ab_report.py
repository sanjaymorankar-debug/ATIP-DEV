"""
The weekly A/B report of the session arms (KGF-13, 16 Nov 2026): every Sunday at 18:00 IST, per arm, from the day its
arm was set (the ARM session event) -- trades closed, win %, average net in rupees and in %, profit factor, maximum
drawdown of its closed-trade equity, the days its regime gate held it out, the picks it skipped (falling, no fresh
LTP, sector cap, re-entry cooldown) and its re-entries within an hour of a stop. Stored in ab_weekly_report (week_end PRIMARY KEY, built_at, payload JSON) and shown on /ab and in the apps.

The owner's rule (prompt 15 Nov 2026): run on paper for at least MIN_TRADES closed trades per arm or MIN_WEEKS weeks,
whichever is later, before promoting or retiring any arm. Each row says how far the arm is from that.

    python -m portfolio.ab_report               # build and store this week's report now, print it
    python -m portfolio.ab_report --week 2026-11-22
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

log = logging.getLogger("atip.ab_report")

MIN_TRADES = 30
MIN_WEEKS = 6
CONTROL = "S5"
REENTRY_WINDOW_MIN = 60          # a buy this soon after a stop on the same symbol counts as a re-entry (KGF-19)


def _row(r):
    return dict(r) if hasattr(r, "keys") else r


def ensure_table(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS ab_weekly_report (week_end TEXT PRIMARY KEY, built_at TEXT NOT NULL, "
                 "payload TEXT NOT NULL)")
    conn.commit()


def _arm_since(conn, sid: str) -> str | None:
    r = conn.execute("SELECT MIN(at) AS at FROM session_events WHERE session_id = ? AND event = 'ARM'", (sid,)).fetchone()
    return _row(r)["at"] if r and _row(r)["at"] else None


def _events(conn, sid: str, event: str, since: str, until: str) -> list:
    return [_row(r) for r in conn.execute(
        "SELECT at, detail FROM session_events WHERE session_id = ? AND event = ? AND at >= ? AND at <= ?",
        (sid, event, since, until)).fetchall()]


def arm_stats(conn, sid: str, since: str, until: str) -> dict:
    """One arm's figures over closed trades entered since `since` and closed by `until` (ISO times)."""
    from orders import sessions
    rules = sessions._linked_rules(conn, sid)
    tr = [t for t in sessions._trades(rules, sessions._charges(conn, rules), since=since)
          if t["closed"] and (t.get("exit_date") or "") <= until[:10]]
    tr.sort(key=lambda t: (t.get("exit_date") or "", t["at"]))
    wins = [t for t in tr if t["pnl"] > 0]
    gross_w, gross_l = sum(t["pnl"] for t in wins), -sum(t["pnl"] for t in tr if t["pnl"] <= 0)
    eq, peak, dd = 0.0, 0.0, 0.0
    for t in tr:
        eq += t["pnl"]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    gate_days = {e["at"][:10] for e in _events(conn, sid, "AUTO_PICK_GATE", since, until)}
    skipped = _events(conn, sid, "AUTO_PICK_SKIP", since, until) + _events(conn, sid, "AUTO_PICK_WAIT_LTP", since, until)
    n = len(tr)
    return {"session": sid, "trades": n, "wins": len(wins), "win_pct": round(len(wins) / n * 100, 1) if n else None,
            "net": round(sum(t["pnl"] for t in tr), 2), "avg_net": round(sum(t["pnl"] for t in tr) / n, 2) if n else None,
            "avg_pct": round(sum(t["pnl"] / t["value"] * 100 for t in tr if t["value"]) / n, 3) if n else None,
            "pf": (round(gross_w / gross_l, 2) if gross_l else ("∞" if gross_w else None)),
            "max_dd": round(dd, 2), "gate_days": len(gate_days), "skipped": len(skipped),
            **{f"skipped_{k}": sum(1 for e in skipped if f"skipped_{k}" in (e.get("detail") or ""))
               for k in ("falling", "sector", "cooldown")},
            **_reentries(conn, tr),
            "open": sum(1 for t in sessions._trades(rules, since=since) if not t["closed"])}


def _reentries(conn, trades: list) -> dict:
    """KGF-19, measured: the closed trades bought within REENTRY_WINDOW_MIN of a stop on the same symbol (in any
    session), and their net -- what a re-entry cooldown would have kept out. The cooldown itself is off by default."""
    hits = []
    for t in trades:
        try:
            at = datetime.fromisoformat(t["at"])
        except (TypeError, ValueError):
            continue
        r = conn.execute("SELECT 1 FROM order_rules WHERE symbol = ? AND role = 'STOP' AND status = 'EXECUTED' "
                         "AND updated_at >= ? AND updated_at < ? LIMIT 1",
                         (t["symbol"], (at - timedelta(minutes=REENTRY_WINDOW_MIN)).isoformat(), t["at"])).fetchone()
        if r:
            hits.append(t)
    return {"reentries": len(hits), "reentry_net": round(sum(t["pnl"] for t in hits), 2)}


def build(conn, week_end: str | None = None) -> dict:
    """The report for the week ending on `week_end` (a Sunday; default the latest Sunday at or before today)."""
    from orders import arms, sessions
    ensure_table(conn)
    d = date.fromisoformat(week_end) if week_end else date.today()
    d = d - timedelta(days=(d.weekday() + 1) % 7)                 # back to Sunday
    until = f"{d.isoformat()}T23:59:59"
    rows, firsts = [], []
    for s in sessions.list_sessions(conn)["sessions"]:
        a = arms.get(conn, s["id"])
        since = _arm_since(conn, s["id"])
        if not a or not since:
            continue
        firsts.append(since)
        st = arm_stats(conn, s["id"], since, until)
        weeks = (d - date.fromisoformat(since[:10])).days / 7
        ready = st["trades"] >= MIN_TRADES and weeks >= MIN_WEEKS
        st.update(arm=a.get("label") or s["name"], rule=arms.describe(a), since=since[:10], weeks=round(weeks, 1),
                  verdict=("ready to judge" if ready else
                           f"collecting: {st['trades']}/{MIN_TRADES} trades, {weeks:.1f}/{MIN_WEEKS} weeks"))
        rows.append(st)
    ctrl = next((r for r in rows if r["session"] == CONTROL), None)
    for r in rows:
        if ctrl and r is not ctrl and r["avg_pct"] is not None and ctrl.get("avg_pct") is not None:
            r["vs_control_pct"] = round(r["avg_pct"] - ctrl["avg_pct"], 3)
    start = min(firsts)[:10] if firsts else None
    review = None
    if start:
        review = (date.fromisoformat(start) + timedelta(weeks=MIN_WEEKS))
        review = (review + timedelta(days=(6 - review.weekday()) % 7)).isoformat()     # that Sunday
    rep = {"week_end": d.isoformat(), "built_at": datetime.now().isoformat(timespec="seconds"), "since": start,
           "min_trades": MIN_TRADES, "min_weeks": MIN_WEEKS, "control": CONTROL, "next_review": review, "arms": rows,
           "note": "PAPER only. Each arm's figures cover trades entered after its arm was set; open trades are not counted."}
    conn.execute("INSERT OR REPLACE INTO ab_weekly_report (week_end, built_at, payload) VALUES (?, ?, ?)",
                 (rep["week_end"], rep["built_at"], json.dumps(rep, default=str)))
    conn.commit()
    return rep


def latest(conn, week_end: str | None = None) -> dict | None:
    ensure_table(conn)
    r = conn.execute("SELECT payload FROM ab_weekly_report WHERE week_end <= ? ORDER BY week_end DESC LIMIT 1",
                     (week_end or "9999-12-31",)).fetchone()
    return json.loads(_row(r)["payload"]) if r else None


def run_scheduled() -> dict:
    """Sunday 18:00 IST (pipeline/w39_jobs.py)."""
    from db.schema import get_connection
    if date.today().weekday() != 6:
        return {"skipped": "not Sunday"}
    conn = get_connection()
    try:
        rep = build(conn)
        try:
            from alerts.telegram import send_telegram
            lines = [f"{r['session']} {r['arm']}: {r['trades']} closed, win {r['win_pct'] if r['win_pct'] is not None else '-'}%, "
                     f"avg {r['avg_pct'] if r['avg_pct'] is not None else '-'}%, gate days {r['gate_days']}" for r in rep["arms"]]
            send_telegram(f"<b>ATIP weekly A/B (paper), week to {rep['week_end']}</b>\n" + "\n".join(lines))
        except Exception as e:
            log.warning(f"  A/B report alert not sent: {e}")
        return {"week_end": rep["week_end"], "arms": len(rep["arms"])}
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse
    from db.schema import get_connection
    ap = argparse.ArgumentParser(prog="python -m portfolio.ab_report", description="Weekly A/B report of the session arms")
    ap.add_argument("--week", help="the Sunday the week ends on, YYYY-MM-DD")
    a = ap.parse_args()
    c = get_connection()
    try:
        rep = build(c, a.week)
        print(f"week to {rep['week_end']} (arms since {rep['since']}, next review {rep['next_review']})")
        for r in rep["arms"]:
            print(f"  {r['session']:<4} {r['arm']:<22} closed {r['trades']:>3}  win {r['win_pct']}%  avg {r['avg_pct']}%  "
                  f"PF {r['pf']}  max DD {r['max_dd']}  gate days {r['gate_days']}  skipped {r['skipped']}  "
                  f"re-entries {r['reentries']}  {r['verdict']}")
    finally:
        c.close()
