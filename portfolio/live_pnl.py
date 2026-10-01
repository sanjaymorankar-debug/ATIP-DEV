"""
Live strategy / P&L monitoring (MON-04), W29.

    live_pnl(conn)   right now, at the newest live quote per symbol (live_quotes, today):
        LIVE   latest synced holdings: value, day P&L vs previous close, unrealised vs cost
        PAPER  paper_position: value, day P&L, unrealised, realised (book to date), cash
        strategies  open W4 positions per strategy (oms_fill net quantity, average cost)
               valued live: unrealised and day P&L
        Each row says where its price came from (live quote and its age, or the last close
        when there is no quote today) -- a stale price is shown, never hidden.
    snapshot()       stores the book totals in live_pnl_snapshot (scheduler: every 15 min in
                     the session) so /trading can chart the day's P&L
    day_series(conn, day=None)
"""

from __future__ import annotations

from datetime import date, datetime


def _prices(conn, symbols):
    syms = sorted(set(symbols))
    if not syms:
        return {}
    q = ",".join("?" * len(syms))
    out = {}
    today = str(date.today())
    for sym, ltp, ts in conn.execute(
            f"SELECT q.symbol, q.ltp, q.timestamp FROM live_quotes q JOIN (SELECT symbol, MAX(timestamp) m FROM "
            f"live_quotes WHERE symbol IN ({q}) AND timestamp>=? GROUP BY symbol) x ON x.symbol=q.symbol AND "
            f"x.m=q.timestamp", (*syms, today)):
        if ltp:
            out[sym] = {"price": float(ltp), "source": "live", "ts": str(ts)[:19]}
    for sym in syms:
        prev = conn.execute("SELECT close, date FROM prices_daily WHERE symbol=? AND date<? AND close>0 ORDER BY date "
                            "DESC LIMIT 1", (sym, today)).fetchone()
        d = out.setdefault(sym, {})
        d["prev_close"] = float(prev[0]) if prev else None
        if "price" not in d:
            last = conn.execute("SELECT close, date FROM prices_daily WHERE symbol=? AND close>0 ORDER BY date DESC "
                                "LIMIT 1", (sym,)).fetchone()
            if last:
                d.update(price=float(last[0]), source=f"close {str(last[1])[:10]}", ts=None)
    now = datetime.now()
    for d in out.values():
        if d.get("ts"):
            try:
                d["age_min"] = round((now - datetime.fromisoformat(d["ts"].replace(" ", "T"))).total_seconds() / 60, 1)
            except ValueError:
                d["age_min"] = None
    return out


def _row(sym, qty, avg, px):
    p = px.get(sym, {})
    price, prev = p.get("price"), p.get("prev_close")
    return {"symbol": sym, "qty": qty, "avg": avg, "price": price, "price_source": p.get("source"),
            "quote_age_min": p.get("age_min"), "value": round(qty * price, 2) if price else None,
            "unrealized": round(qty * (price - avg), 2) if (price and avg) else None,
            "day_pnl": round(qty * (price - prev), 2) if (price and prev) else None,
            "day_pct": round((price / prev - 1) * 100, 2) if (price and prev) else None}


def _tot(rows):
    s = lambda k: round(sum(r[k] for r in rows if r.get(k) is not None), 2)
    return {"positions": len(rows), "value": s("value"), "unrealized": s("unrealized"), "day_pnl": s("day_pnl")}


def live_pnl(conn) -> dict:
    hold = conn.execute("SELECT symbol, qty, avg_price FROM portfolio_holdings WHERE date=(SELECT MAX(date) FROM "
                        "portfolio_holdings) AND qty>0").fetchall()
    try:
        paper = conn.execute("SELECT symbol, quantity, avg_price, realized_pnl FROM paper_position").fetchall()
        cash = conn.execute("SELECT balance FROM paper_account WHERE id=1").fetchone()
    except Exception:
        paper, cash = [], None
    strat = {}
    for sid, sym, side, qty, px in conn.execute("SELECT strategy_id, symbol, side, quantity, price FROM oms_fill "
                                                "WHERE order_id NOT IN (SELECT order_id FROM oms_order WHERE "
                                                "COALESCE(instrument,'CASH')='FUT') ORDER BY filled_at"):
        q, avg = strat.get((sid, sym), (0, 0.0))
        if side == "BUY":
            avg = (avg * q + px * qty) / (q + qty) if q + qty else 0.0
            q += qty
        else:
            q -= qty
            avg = avg if q > 0 else 0.0
        strat[(sid, sym)] = (q, avg)
    syms = [h[0] for h in hold] + [p[0] for p in paper if p[1]] + [k[1] for k, v in strat.items() if v[0]]
    px = _prices(conn, syms)
    live_rows = [_row(s, int(q), float(a or 0), px) for s, q, a in hold]
    paper_rows = [_row(s, int(q), float(a or 0), px) for s, q, a, _ in paper if q]
    by_strat = {}
    for (sid, sym), (q, a) in strat.items():
        if q:
            by_strat.setdefault(sid or "(none)", []).append(_row(sym, int(q), a, px))
    stale = [s for s, p in px.items() if p.get("source") != "live" or (p.get("age_min") or 0) > 20]
    try:                                               # W30: paper futures short legs, marked at the EOD close
        from execution.futures_paper import book as _fbook
        fut = _fbook(conn)
        fut = {"positions": len(fut["positions"]), "unrealized": fut["unrealized"], "realized": fut["realized"],
               "margin_blocked": fut["margin_blocked"], "rows": fut["positions"], "price_source": "EOD futures close"}
    except Exception:
        fut = None
    return {"as_of": datetime.now().isoformat(timespec="seconds"),
            "LIVE": {**_tot(live_rows), "rows": live_rows},
            "PAPER": {**_tot(paper_rows), "rows": paper_rows,
                      "realized_to_date": round(sum(float(p[3] or 0) for p in paper), 2),
                      "cash": round(float(cash[0]), 2) if cash else None},
            "strategies": {sid: {**_tot(rows), "rows": rows} for sid, rows in sorted(by_strat.items())},
            "FUTURES": fut,
            "stale_prices": sorted(stale)}


def snapshot() -> dict:
    from db.schema import get_connection
    conn = get_connection()
    try:
        p = live_pnl(conn)
        ts = datetime.now()
        for book in ("LIVE", "PAPER"):
            b = p[book]
            conn.execute("INSERT INTO live_pnl_snapshot (ts,book,value,day_pnl,unrealized,positions) VALUES "
                         "(?,?,?,?,?,?)", (ts, book, b["value"], b["day_pnl"], b["unrealized"], b["positions"]))
        conn.commit()
        return {"status": "SUCCESS", "rows": 2}
    finally:
        conn.close()


def day_series(conn, day=None) -> dict:
    day = str(day or date.today())[:10]
    out = {}
    for ts, book, v, d, u in conn.execute("SELECT ts, book, value, day_pnl, unrealized FROM live_pnl_snapshot WHERE "
                                          "DATE(ts)=? ORDER BY ts", (day,)):
        out.setdefault(book, []).append({"ts": str(ts)[11:16], "value": v, "day_pnl": d, "unrealized": u})
    return {"day": day, "series": out}
