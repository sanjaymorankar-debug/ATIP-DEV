"""
Strategy performance (W28: DB-16) -- what each strategy's decisions and paper fills
actually did, per strategy version, over trailing windows.

Two independent measures, never blended:

  Decision outcomes  every BUY / SELL decision in the window is marked against what the
                     stock did over the next HORIZONS sessions (prices_daily closes, from
                     the decision's as_of close), and against the Nifty 50 over the same
                     sessions. A SELL counts as a hit when the stock fell. Decisions whose
                     horizon has not elapsed are counted as pending, not scored.
                       n, hit_rate, mean_return_pct, mean_excess_pct per horizon
  Paper book         oms_fill rows of the strategy (paper mode), FIFO-matched per symbol:
                       realised_pnl, closed_trades, win_rate, open_qty and mark-to-market
                       unrealised_pnl at the latest close, fees, turnover

compute(conn, strategy_id, version, as_of, window_days) -> metrics dict
run_all(as_of) stores every strategy x WINDOWS in strategy_performance (post-market).
These are descriptive: a short window or a handful of decisions is noise, and n is
always reported beside every rate.
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from datetime import date, datetime, timedelta

HORIZONS = (5, 10, 20)
WINDOWS = (20, 60, 250)


def _d(v):
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


class _Closes:
    """Cached close series per symbol, for forward returns."""

    def __init__(self, conn):
        self.conn = conn
        self.cache = {}

    def series(self, sym):
        if sym not in self.cache:
            rows = self.conn.execute("SELECT date, close FROM prices_daily WHERE symbol=? AND close>0 ORDER BY date",
                                     (sym,)).fetchall()
            self.cache[sym] = ([str(r[0])[:10] for r in rows], [float(r[1]) for r in rows])
        return self.cache[sym]

    def forward(self, sym, as_of, h):
        import bisect
        dates, closes = self.series(sym)
        i = bisect.bisect_right(dates, str(as_of)[:10]) - 1
        if i < 0 or i + h >= len(dates):
            return None
        return (closes[i + h] / closes[i] - 1) * 100

    def last(self, sym):
        _, closes = self.series(sym)
        return closes[-1] if closes else None


def decision_outcomes(conn, strategy_id, version, start, end, closes=None, bench=None) -> dict:
    closes = closes or _Closes(conn)
    if bench is None:
        try:
            from data.dhan import BETA_BENCHMARK_SYMBOL as bench
        except Exception:
            bench = "NIFTY50"
    rows = conn.execute("SELECT as_of, symbol, decision FROM strategy_decision WHERE strategy_id=? AND version=? "
                        "AND as_of>=? AND as_of<=? AND decision IN ('BUY','SELL')",
                        (strategy_id, version, str(start), str(end))).fetchall()
    out = {"decisions": len(rows), "buy": sum(1 for r in rows if r[2] == "BUY"),
           "sell": sum(1 for r in rows if r[2] == "SELL"), "horizons": {}}
    for h in HORIZONS:
        rets, exc, hits, pending = [], [], 0, 0
        for as_of, sym, dec in rows:
            r = closes.forward(sym, as_of, h)
            if r is None:
                pending += 1
                continue
            sign = 1 if dec == "BUY" else -1
            b = closes.forward(bench, as_of, h)
            rets.append(sign * r)
            if b is not None:
                exc.append(sign * (r - b))
            hits += (sign * r) > 0
        n = len(rets)
        out["horizons"][str(h)] = {
            "n": n, "pending": pending,
            "hit_rate": round(hits / n, 4) if n else None,
            "mean_return_pct": round(sum(rets) / n, 3) if n else None,
            "mean_excess_pct": round(sum(exc) / len(exc), 3) if exc else None,
        }
    return out


def paper_book(conn, strategy_id, version=None, start=None, end=None, closes=None) -> dict:
    closes = closes or _Closes(conn)
    sql = "SELECT symbol, side, quantity, price, fees, filled_at FROM oms_fill WHERE strategy_id=? AND (mode IS NULL OR mode<>'LIVE')"
    args = [strategy_id]
    if version:
        sql += " AND strategy_version=?"
        args.append(version)
    if end:
        sql += " AND filled_at<?"
        args.append(str(_d(end) + timedelta(days=1)))
    sql += " ORDER BY filled_at"
    lots = defaultdict(deque)          # symbol -> deque[[qty, price]] (long lots; negative qty = short)
    realised = fees = turnover = 0.0
    closed = wins = 0
    for sym, side, qty, px, fee, at in conn.execute(sql, args):
        fees += fee or 0.0
        turnover += qty * px
        in_window = start is None or str(at)[:10] >= str(start)
        signed = qty if side == "BUY" else -qty
        q = lots[sym]
        while signed and q and (q[0][0] > 0) != (signed > 0):
            lot = q[0]
            m = min(abs(lot[0]), abs(signed))
            pnl = (px - lot[1]) * m if lot[0] > 0 else (lot[1] - px) * m
            if in_window:
                realised += pnl
                closed += 1
                wins += pnl > 0
            lot[0] += m if lot[0] < 0 else -m
            signed += -m if signed > 0 else m
            if lot[0] == 0:
                q.popleft()
        if signed:
            q.append([signed, px])
    open_qty = unreal = 0.0
    positions = []
    for sym, q in lots.items():
        qty = sum(lot[0] for lot in q)
        if not qty:
            continue
        last = closes.last(sym)
        u = sum((last - lot[1]) * lot[0] for lot in q) if last else None
        positions.append({"symbol": sym, "qty": qty, "last": last, "unrealised": None if u is None else round(u, 2)})
        open_qty += abs(qty)
        unreal += u or 0.0
    return {"realised_pnl": round(realised, 2), "closed_trades": closed,
            "win_rate": round(wins / closed, 4) if closed else None, "fees": round(fees, 2),
            "turnover": round(turnover, 2), "open_positions": len(positions), "unrealised_pnl": round(unreal, 2),
            "positions": sorted(positions, key=lambda p: -abs(p["unrealised"] or 0))[:20]}


def compute(conn, strategy_id, version, as_of=None, window_days=60, closes=None) -> dict:
    end = _d(as_of or date.today())
    start = end - timedelta(days=int(window_days))
    closes = closes or _Closes(conn)
    return {"strategy_id": strategy_id, "version": version, "as_of": str(end), "window_days": int(window_days),
            "start": str(start), "decisions": decision_outcomes(conn, strategy_id, version, start, end, closes),
            "paper": paper_book(conn, strategy_id, version, start, end, closes)}


def _versions(conn):
    return conn.execute("SELECT DISTINCT strategy_id, version FROM strategy_decision ORDER BY strategy_id, version").fetchall()


def run_all(as_of=None, conn=None) -> dict:
    from db.schema import get_connection
    own = conn is None
    conn = conn or get_connection()
    try:
        end = _d(as_of or date.today())
        closes = _Closes(conn)
        n = 0
        for sid, ver in _versions(conn):
            for w in WINDOWS:
                m = compute(conn, sid, ver, end, w, closes)
                conn.execute("INSERT OR REPLACE INTO strategy_performance (strategy_id,version,as_of,window_days,"
                             "metrics_json,created_at) VALUES (?,?,?,?,?,?)",
                             (sid, ver, str(end), w, json.dumps(m), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                n += 1
        conn.commit()
        return {"status": "SUCCESS" if n else "EMPTY", "rows": n}
    finally:
        if own:
            conn.close()


def latest(conn, window_days=60) -> list:
    rows = conn.execute(
        "SELECT p.strategy_id, p.version, p.as_of, p.metrics_json, s.name, s.status FROM strategy_performance p "
        "LEFT JOIN strategy s ON s.strategy_id=p.strategy_id WHERE p.window_days=? AND p.as_of=(SELECT MAX(as_of) "
        "FROM strategy_performance WHERE window_days=?) ORDER BY p.strategy_id", (int(window_days), int(window_days)))
    out = []
    for sid, ver, a, mj, name, status in rows:
        m = json.loads(mj or "{}")
        m.update({"name": name, "status": status})
        out.append(m)
    return out


def equity_curve(conn, strategy_id, version=None, days=250) -> list:
    """Daily cumulative realised P&L from the strategy's paper fills (FIFO), for the panel chart."""
    start = date.today() - timedelta(days=days)
    sql = ("SELECT symbol, side, quantity, price, filled_at FROM oms_fill WHERE strategy_id=? AND "
           "(mode IS NULL OR mode<>'LIVE')")
    args = [strategy_id]
    if version:
        sql += " AND strategy_version=?"
        args.append(version)
    lots = defaultdict(deque)
    by_day = defaultdict(float)
    for sym, side, qty, px, at in conn.execute(sql + " ORDER BY filled_at", args):
        signed = qty if side == "BUY" else -qty
        q = lots[sym]
        while signed and q and (q[0][0] > 0) != (signed > 0):
            lot = q[0]
            m = min(abs(lot[0]), abs(signed))
            by_day[str(at)[:10]] += (px - lot[1]) * m if lot[0] > 0 else (lot[1] - px) * m
            lot[0] += m if lot[0] < 0 else -m
            signed += -m if signed > 0 else m
            if lot[0] == 0:
                q.popleft()
        if signed:
            q.append([signed, px])
    cum, out = 0.0, []
    for d in sorted(by_day):
        cum += by_day[d]
        if d >= str(start):
            out.append({"date": d, "cum_realised": round(cum, 2)})
    return out
