"""
Strategy performance (DB-16), W28 -- one row per strategy, from what ATIP records:

    lifecycle      status, current version, latest health status / reason (strategy_health)
    backtest       the current version's latest COMPLETED backtest: total return, CAGR, Sharpe,
                   max drawdown, win rate, profit factor, trades, period
    decisions      the last `days` of strategy_decision by action (ENTER / EXIT / HOLD / ...)
                   and how many were blocked
    book (PAPER / LIVE separately)  from W4 fills (oms_fill), average-cost per symbol:
                   realised P&L, open quantity, cost basis, unrealised P&L at the latest close,
                   number of fills, fees
    FUTURES / OPTIONS  the paper futures short legs (W30) and the option overlays' multi-leg
                   positions (W40): realised, unrealised (the last daily mark), open positions, fills

Only fills the W4 OMS made carry a strategy; positions opened outside it (order rules,
manual paper orders) belong to no strategy (KNOWN_ISSUES W4-R7).
"""

from __future__ import annotations

import json
from datetime import date, timedelta


def _close(conn, sym):
    r = conn.execute("SELECT close FROM prices_daily WHERE symbol=? AND close>0 ORDER BY date DESC LIMIT 1",
                     (sym,)).fetchone()
    return float(r[0]) if r else None


def book_pnl(conn, strategy_id) -> dict:
    out = {}
    pos = {}
    for mode, sym, side, qty, px, fees in conn.execute(
            "SELECT mode, symbol, side, quantity, price, COALESCE(fees,0) FROM oms_fill WHERE strategy_id=? AND "
            "order_id NOT IN (SELECT order_id FROM oms_order WHERE COALESCE(instrument,'CASH') IN ('FUT','OPT')) "
            "ORDER BY filled_at", (strategy_id,)):
        b = out.setdefault(mode or "PAPER", {"realized": 0.0, "fees": 0.0, "fills": 0, "unrealized": 0.0,
                                             "open_positions": 0, "cost_basis": 0.0})
        b["fills"] += 1
        b["fees"] += fees
        q, avg = pos.get((mode, sym), (0.0, 0.0))
        qty = float(qty)
        signed = qty if side == "BUY" else -qty
        if q == 0 or (q > 0) == (signed > 0):           # opening / adding
            avg = (avg * abs(q) + px * qty) / (abs(q) + qty)
            q += signed
        else:                                           # reducing, possibly crossing through zero
            closing = min(qty, abs(q))
            b["realized"] += (px - avg) * closing * (1 if q > 0 else -1)
            was_long = q > 0
            q += signed
            if q == 0:
                avg = 0.0
            elif (q > 0) != was_long:                   # crossed: the remainder opens at this price
                avg = px
        pos[(mode, sym)] = (q, avg)
    for (mode, sym), (q, avg) in pos.items():
        if q:
            b = out[mode or "PAPER"]
            px = _close(conn, sym)
            b["open_positions"] += 1
            b["cost_basis"] += abs(q) * avg
            if px:
                b["unrealized"] += (px - avg) * q
    for b in out.values():
        b["realized"] = round(b["realized"] - b["fees"], 2)
        b["unrealized"] = round(b["unrealized"], 2)
        b["cost_basis"] = round(b["cost_basis"], 2)
        b["fees"] = round(b["fees"], 2)
    return out


def _futures(conn, sid) -> dict:
    """W30: the strategy's paper stock-futures short legs, as a FUTURES book."""
    try:
        rows = conn.execute("SELECT COUNT(*), COALESCE(SUM(realized_pnl),0), SUM(CASE WHEN lots<>0 THEN 1 ELSE 0 END) "
                            "FROM paper_futures_position WHERE strategy_id=?", (sid,)).fetchone()
    except Exception:
        return {}
    if not rows or not rows[0]:
        return {}
    from execution.futures_paper import book
    unreal = sum(p["unrealized"] or 0 for p in book(conn)["positions"] if p["strategy_id"] == sid)
    return {"FUTURES": {"realized": round(rows[1], 2), "unrealized": round(unreal, 2), "open_positions": rows[2] or 0,
                        "fills": conn.execute("SELECT COUNT(*) FROM paper_futures_trade WHERE strategy_id=?",
                                              (sid,)).fetchone()[0], "fees": None, "cost_basis": None}}


def _options(conn, sid) -> dict:
    """W40: the strategy's multi-leg paper option positions (option overlays), as an OPTIONS book."""
    try:
        from execution.options_paper import strategy_book
        b = strategy_book(conn, sid)
    except Exception:
        return {}
    return {"OPTIONS": b} if b else {}


def performance(conn, days: int = 30) -> list:
    since = str(date.today() - timedelta(days=int(days)))
    rows = []
    for sid, name, kind, status, ver in conn.execute(
            "SELECT strategy_id, name, kind, status, current_version FROM strategy ORDER BY strategy_id"):
        h = conn.execute("SELECT status, metrics_json, issues_json, as_of FROM strategy_health WHERE strategy_id=? "
                         "ORDER BY as_of DESC, created_at DESC LIMIT 1", (sid,)).fetchone()
        bt = conn.execute("SELECT run_id, start_date, end_date, metrics_json FROM backtest_run WHERE strategy_id=? AND "
                          "strategy_version=? AND status='COMPLETED' AND COALESCE(kind,'single') NOT IN ('wf_test') "
                          "ORDER BY finished_at DESC LIMIT 1", (sid, ver)).fetchone()
        m = json.loads(bt[3] or "{}") if bt else {}
        dec = {a: n for a, n in conn.execute("SELECT COALESCE(action, decision), COUNT(*) FROM strategy_decision WHERE "
                                             "strategy_id=? AND as_of>=? GROUP BY 1", (sid, since))}
        blocked = conn.execute("SELECT COUNT(*) FROM strategy_decision WHERE strategy_id=? AND as_of>=? AND "
                               "blocked_reason IS NOT NULL", (sid, since)).fetchone()[0]
        issues = json.loads(h[2] or "[]") if h else []
        rows.append({
            "strategy_id": sid, "name": name, "kind": kind, "status": status, "version": ver,
            "health": {"status": h[0], "as_of": str(h[3])[:10],
                       "issues": [i.get("code") for i in issues if isinstance(i, dict)]} if h else None,
            "backtest": {"run_id": bt[0], "period": f"{str(bt[1])[:10]}..{str(bt[2])[:10]}",
                         **{k: m.get(k) for k in ("total_return", "cagr", "sharpe", "max_drawdown", "win_rate",
                                                  "profit_factor", "trades")}} if bt else None,
            "decisions": {"days": int(days), "by_action": dec, "blocked": blocked},
            "books": {**book_pnl(conn, sid), **_futures(conn, sid), **_options(conn, sid)}})
    return rows
