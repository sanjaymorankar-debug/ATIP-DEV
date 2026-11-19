"""
Market breadth and the regime gate (KGF-09, 16 Nov 2026).

    breadth20 / breadth50   % of ATIP's scored universe whose close is above its own 20- / 50-day simple average
                            (EQ series, one bar per symbol and day; a stock with fewer than N closes counts as not above,
                            as the 15 Nov 2026 study counted it)

Why a gate. ATIP's market-health regime flipped BEAR / NEUTRAL / BULL from day to day and read BULL on 13, 15 and 29 Oct
2026 while only 19-21 % of the universe was above its 50-day average; rank-fill took any HOLD stock whatever the regime.
On ATIP's own data 2019-2026 (`.ux_build/kite-gap-2026-11-15`, `research/kite_retest_study.py`) the Kite breakout-retest
K1 averaged +0.97 % a trade with breadth50 >= 50 on the setup day and -0.11 % below it (last 12 months +1.14 % vs
-1.11 %). Wider stops lost more than the 2 % stop on the Sep-Oct signals and won only in the November rebound, so they
go only behind this gate.

A session's gate is session_settings `arm:<id>` -> {"regime_gate": {"metric": "breadth50", "min": 50}} (orders/arms.py).
No new entry while the metric is under min; exits keep running. The gate reads the latest stored day at or before
today -- yesterday's close during the session -- so it is fixed for the whole trading day.

Stored in `market_breadth` (date PRIMARY KEY, breadth20, breadth50, n, above20, above50, computed_at) by the evening
job after scoring (pipeline/w39_jobs.py breadth_run) and on demand:

    python -m scores.breadth                       # compute the missing days, print the last ten
    python -m scores.breadth --from 2025-01-01     # recompute a range
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

log = logging.getLogger("atip.breadth")

METRICS = ("breadth20", "breadth50")
WINDOWS = {"breadth20": 20, "breadth50": 50}
LOOKBACK_DAYS = 110                 # calendar days of closes read for one day's 50-day average


def _row(r):
    return dict(r) if hasattr(r, "keys") else r


def ensure_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_breadth (
            date        TEXT PRIMARY KEY,
            breadth20   REAL,
            breadth50   REAL,
            n           INTEGER,
            above20     INTEGER,
            above50     INTEGER,
            computed_at TEXT NOT NULL
        )""")
    conn.commit()


def _universe(conn) -> list:
    """Every symbol ATIP has scored (the study's universe: 529 on 15 Nov 2026)."""
    try:
        return [_row(r)["symbol"] for r in conn.execute("SELECT DISTINCT symbol FROM ai_scores").fetchall()]
    except Exception:
        return []


def compute(conn, start: str | None = None, end: str | None = None) -> list[dict]:
    """breadth20/50 for every trading day from start to end (default: the last stored day + 1 to the newest bar)."""
    import pandas as pd
    ensure_table(conn)
    uni = _universe(conn)
    if not uni:
        return []
    if not end:
        r = conn.execute("SELECT MAX(date) AS d FROM prices_daily WHERE series = 'EQ'").fetchone()
        end = str(_row(r)["d"] or "")[:10]
        if not end:
            return []
    if not start:
        r = _row(conn.execute("SELECT MAX(date) AS d FROM market_breadth").fetchone())
        start = (date.fromisoformat(r["d"][:10]) + timedelta(days=1)).isoformat() if r and r["d"] else \
            (date.fromisoformat(end) - timedelta(days=30)).isoformat()
    if start > end:
        return []
    lo = (date.fromisoformat(start) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    q = ("SELECT symbol, date, close, id AS rid FROM prices_daily WHERE series = 'EQ' AND date >= ? AND date <= ? "
         f"AND symbol IN ({','.join('?' * len(uni))})")
    d = pd.DataFrame([_row(r) for r in conn.execute(q, (lo, end + "T99", *uni)).fetchall()],
                     columns=["symbol", "date", "close", "rid"])
    if d.empty:
        return []
    d["date"] = d["date"].astype(str).str[:10]
    d = d[pd.to_datetime(d["date"]).dt.day_of_week < 5]
    d = d.sort_values(["symbol", "date", "rid"]).drop_duplicates(["symbol", "date"], keep="last")
    d["close"] = d["close"].astype(float)
    g = d.groupby("symbol")["close"]
    d["sma20"] = g.transform(lambda s: s.rolling(20).mean())
    d["sma50"] = g.transform(lambda s: s.rolling(50).mean())
    d = d[(d["date"] >= start) & (d["date"] <= end)]
    out, now = [], datetime.now().isoformat(timespec="seconds")
    for day, x in d.groupby("date"):
        n = len(x)
        a20, a50 = int((x["close"] > x["sma20"]).sum()), int((x["close"] > x["sma50"]).sum())
        row = {"date": day, "breadth20": round(a20 / n * 100, 2), "breadth50": round(a50 / n * 100, 2), "n": n,
               "above20": a20, "above50": a50, "computed_at": now}
        conn.execute("INSERT OR REPLACE INTO market_breadth (date, breadth20, breadth50, n, above20, above50, computed_at) "
                     "VALUES (:date, :breadth20, :breadth50, :n, :above20, :above50, :computed_at)", row)
        out.append(row)
    conn.commit()
    return out


def latest(conn, on: str | None = None) -> dict | None:
    """The newest stored breadth at or before `on` (default today): what the gate reads during a session."""
    ensure_table(conn)
    on = on or date.today().isoformat()
    r = conn.execute("SELECT * FROM market_breadth WHERE date <= ? ORDER BY date DESC LIMIT 1", (on,)).fetchone()
    return _row(r) if r else None


def gate_state(conn, gate: dict | None, on: str | None = None) -> dict:
    """{"open": bool, "metric", "min", "value", "date", "reason"} for a session's regime gate. No gate -> open.
    A gate with no stored breadth is CLOSED (fail closed: the gate exists to stop entries in a weak market)."""
    if not gate:
        return {"open": True, "metric": None, "min": None, "value": None, "date": None, "reason": None}
    metric = str(gate.get("metric") or "breadth50")
    lo = float(gate.get("min") if gate.get("min") is not None else 50)
    if metric not in METRICS:
        return {"open": False, "metric": metric, "min": lo, "value": None, "date": None,
                "reason": f"regime gate: unknown metric {metric!r}"}
    b = latest(conn, on)
    if not b or b.get(metric) is None:
        return {"open": False, "metric": metric, "min": lo, "value": None, "date": None,
                "reason": f"regime gate: no {metric} stored yet (python -m scores.breadth)"}
    # LRCA-06 (19 Nov 2026, strategy v2 freshness_gate, OFF unless config.json turns it on): the newest stored breadth
    # was used at any age, so a stopped 21:20 / 07:30 job froze the gate open (or shut) on an old day.
    from orders import strategy_v2 as SV2
    if SV2.on("freshness_gate"):
        why = SV2.stale(b["date"], SV2.value("breadth_max_age_sessions") or 1,
                        date.fromisoformat(str(on)[:10]) if on else None)
        if why:
            return {"open": False, "metric": metric, "min": lo, "value": float(b[metric]), "date": b["date"],
                    "reason": f"regime gate closed: stored breadth is stale ({why})"}
    v = float(b[metric])
    ok = v >= lo
    return {"open": ok, "metric": metric, "min": lo, "value": v, "date": b["date"],
            "reason": None if ok else f"regime gate closed: {metric} {v:.1f}% on {b['date']} is under {lo:g}%"}


def history(conn, days: int = 60) -> list:
    ensure_table(conn)
    return [_row(r) for r in conn.execute("SELECT * FROM market_breadth ORDER BY date DESC LIMIT ?", (int(days),))]


def run_scheduled() -> dict:
    """The evening job: the days since the last stored one."""
    from db.schema import get_connection
    conn = get_connection()
    try:
        rows = compute(conn)
        last = latest(conn)
        return {"days": len(rows), "latest": last}
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse
    from db.schema import get_connection
    ap = argparse.ArgumentParser(prog="python -m scores.breadth", description="breadth20 / breadth50 of the scored universe")
    ap.add_argument("--from", dest="start", help="first day to (re)compute, YYYY-MM-DD")
    ap.add_argument("--to", dest="end", help="last day, YYYY-MM-DD")
    a = ap.parse_args()
    c = get_connection()
    try:
        rows = compute(c, a.start, a.end)
        print(f"computed {len(rows)} day(s)")
        for r in reversed(history(c, 10)):
            print(f"  {r['date']}  breadth20 {r['breadth20']:5.1f}%  breadth50 {r['breadth50']:5.1f}%  n={r['n']}")
    finally:
        c.close()
