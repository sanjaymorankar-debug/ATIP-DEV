"""
Paper P&L report: every paper fill as round-trip trades, and the book's P&L by day, week and month.

    python -m portfolio.paper_report                     # the whole paper account, by month
    python -m portfolio.paper_report --scope S1 --by day # one session's cash pool

Built from paper_order alone (orders/paper.py), so it covers every paper trade -- a session, Trade
Setup, the CLI or a strategy -- back to the account's first fill. pnl_daily (portfolio/pnl.py) has
only the days the post-market job ran and nothing before 25 Sep 2026; this re-values each day:

    cash    the scope's opening money + every fill's cash (sales in, buys and charges out) to that day
    equity  cash + shares held, at that day's close (prices_daily; today: the latest live quote)
    P&L     equity - the previous day's equity; a week or a month is the sum of its days

Booked P&L is what a sale realised against the average cost of the shares sold, the charges of
both sides included, so a closed trade's net is exactly what it added to cash. Shares still held
count in equity at their mark, not in booked.

Scopes
    all     the whole paper account; its opening balance is today's paper cash less every fill
    S1..    one session's cash pool (orders/sessions.py start_pool): its fills (paper_order.tag
            ATIP-S1) since the pool started, from its start_capital
    pools   every session with a pool together, from the sum of their capitals
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, timedelta

from db.schema import get_connection
from portfolio.pnl import _table_exists, mark_price

log = logging.getLogger(__name__)

PERIODS = ("day", "week", "month")


def _fills(conn) -> list[dict]:
    """Every paper fill in time order: an end-of-day order at its session's open, others when placed."""
    if not _table_exists(conn, "paper_order"):
        return []
    cols = {r[1] for r in conn.execute("PRAGMA table_info(paper_order)").fetchall()}
    fs = "fill_session" if "fill_session" in cols else "NULL"
    out = []
    for r in conn.execute(f"SELECT order_id, symbol, transaction_type, filled_qty, fill_price, brokerage, tag, "
                          f"created_at, {fs} AS fill_session FROM paper_order "
                          f"WHERE filled_qty > 0 AND fill_price IS NOT NULL").fetchall():
        r = dict(r)
        at = f"{r['fill_session']}T09:15:00" if r.get("fill_session") else str(r["created_at"] or "")
        tag = str(r.get("tag") or "")
        out.append({"order_id": r["order_id"], "symbol": r["symbol"], "side": r["transaction_type"],
                    "qty": int(r["filled_qty"]), "price": float(r["fill_price"]), "fees": float(r["brokerage"] or 0),
                    "at": at, "session": tag[len("ATIP-"):] if tag.startswith("ATIP-") else None})
    out.sort(key=lambda f: f["at"])
    return out


def _cash(f: dict) -> float:
    v = f["qty"] * f["price"]
    return (v - f["fees"]) if f["side"] == "SELL" else -(v + f["fees"])


def _replay(fills: list) -> tuple[list, list]:
    """(round trips, booked events). A trip runs from a buy into a flat symbol until it is flat
    again; a sale books against the average cost of what is held, buy charges included."""
    book, trips, events = {}, [], []
    for f in fills:
        b = book.setdefault(f["symbol"], {"qty": 0, "cost": 0.0, "trip": None})
        q, px, fee = f["qty"], f["price"], f["fees"]
        if f["side"] == "BUY":
            if b["qty"] == 0 or b["trip"] is None:
                b["trip"] = {"symbol": f["symbol"], "session": f["session"], "opened": f["at"], "closed": None,
                             "bought_qty": 0, "buy_value": 0.0, "sold_qty": 0, "sell_value": 0.0,
                             "charges": 0.0, "net": 0.0, "fills": 0}
                trips.append(b["trip"])
            t = b["trip"]
            t["bought_qty"] += q; t["buy_value"] += q * px; t["charges"] += fee; t["fills"] += 1
            b["qty"] += q; b["cost"] += q * px + fee
            continue
        sold = min(q, b["qty"])
        if sold <= 0:                       # nothing held to sell against (a reset in between): no cost basis
            continue
        avg = b["cost"] / b["qty"]
        fee_part = fee * sold / q
        pnl = sold * px - fee_part - avg * sold
        b["cost"] -= avg * sold; b["qty"] -= sold
        t = b["trip"]
        t["sold_qty"] += sold; t["sell_value"] += sold * px; t["charges"] += fee_part; t["net"] += pnl; t["fills"] += 1
        events.append({"date": f["at"][:10], "symbol": f["symbol"], "pnl": pnl, "trip": t})
        if b["qty"] == 0:
            t["closed"] = f["at"]; b["cost"] = 0.0; b["trip"] = None
    for t in trips:
        t["held_qty"] = t["bought_qty"] - t["sold_qty"]
        t["avg_buy"] = round(t["buy_value"] / t["bought_qty"], 2) if t["bought_qty"] else None
        t["avg_sell"] = round(t["sell_value"] / t["sold_qty"], 2) if t["sold_qty"] else None
    return trips, events


def _trading_days(start: date, end: date, extra=()) -> list[date]:
    from utils.trading_calendar import is_trading_day
    extra = set(extra)
    out, x = [], start
    while x <= end:
        if is_trading_day(x) or str(x) in extra:
            out.append(x)
        x += timedelta(days=1)
    return out


def daily(conn, fills: list, opening: float, start: date, end: date | None = None) -> list[dict]:
    """One row per trading day from `start` to `end` (default today): cash, the value of what is
    held at that day's mark, equity, the day's P&L, what was booked and the charges paid."""
    end = end or date.today()
    _, events = _replay(fills)
    by_day, booked = {}, {}
    for f in fills:
        by_day.setdefault(f["at"][:10], []).append(f)
    for e in events:
        booked[e["date"]] = booked.get(e["date"], 0.0) + e["pnl"]
    cash, held, last_px, prev, out = float(opening), {}, {}, float(opening), []
    for d in _trading_days(start, end, extra=[k for k in by_day if str(start) <= k <= str(end)]):
        todays = by_day.get(str(d), [])
        for f in todays:
            cash += _cash(f)
            held[f["symbol"]] = held.get(f["symbol"], 0) + (f["qty"] if f["side"] == "BUY" else -f["qty"])
            last_px[f["symbol"]] = f["price"]
        value, unpriced = 0.0, []
        for sym, q in held.items():
            if q <= 0:
                continue
            m = mark_price(conn, sym, d)
            if m is None:
                unpriced.append(sym)
                m = last_px.get(sym, 0.0)
            value += q * m
        equity = cash + value
        out.append({"date": str(d), "cash": round(cash, 2), "positions_value": round(value, 2),
                    "equity": round(equity, 2), "pnl": round(equity - prev, 2),
                    "booked": round(booked.get(str(d), 0.0), 2), "charges": round(sum(f["fees"] for f in todays), 2),
                    "fills": len(todays), "unpriced": unpriced})
        prev = equity
    return out


def _period_key(d: str, by: str) -> str:
    if by == "month":
        return d[:7]
    if by == "week":
        x = date.fromisoformat(d)
        return str(x - timedelta(days=x.weekday()))
    return d


def rollup(days: list, trips: list, by: str = "day") -> list[dict]:
    """Days summed into periods (newest first): P&L, booked, charges, the trades closed in each and
    how many made money, equity at the end and the return on the equity it started with."""
    if by not in PERIODS:
        raise ValueError(f"by must be one of {', '.join(PERIODS)}")
    groups = {}
    for r in days:
        groups.setdefault(_period_key(r["date"], by), []).append(r)
    closed = {}
    for t in trips:
        if t["closed"]:
            closed.setdefault(_period_key(t["closed"][:10], by), []).append(t)
    out = []
    for k, rows in groups.items():
        pnl = sum(r["pnl"] for r in rows)
        eq_end = rows[-1]["equity"]
        eq_start = eq_end - pnl
        done = closed.get(k, [])
        out.append({"period": k, "from": rows[0]["date"], "to": rows[-1]["date"], "days": len(rows),
                    "pnl": round(pnl, 2), "return_pct": round(pnl / eq_start * 100, 2) if eq_start else None,
                    "booked": round(sum(r["booked"] for r in rows), 2),
                    "charges": round(sum(r["charges"] for r in rows), 2),
                    "trades_closed": len(done), "wins": sum(1 for t in done if t["net"] > 0),
                    "equity": eq_end})
    return sorted(out, key=lambda r: r["period"], reverse=True)


def _scope(conn, scope: str, fills: list) -> dict:
    """Which fills a scope covers, the money it opened with and the day it starts."""
    from orders import sessions
    scope = (scope or "all").strip()
    if scope.lower() == "all":
        bal = None
        if _table_exists(conn, "paper_account"):
            r = conn.execute("SELECT balance FROM paper_account WHERE id=1").fetchone()
            bal = float(r[0]) if r else None
        if bal is None:
            from orders.paper import settings
            bal = float(settings()["paper_opening_balance"])
        opening = bal - sum(_cash(f) for f in fills)
        start = date.fromisoformat(fills[0]["at"][:10]) if fills else None
        return {"scope": "all", "label": "Whole paper account", "fills": fills, "opening": opening, "start": start}
    pools = [s for s in sessions.list_sessions(conn)["sessions"] if s.get("start_capital")]
    if scope.lower() == "pools":
        chosen, label = pools, f"All {len(pools)} session pools"
    else:
        chosen = [s for s in pools if s["id"].upper() == scope.upper()]
        if not chosen:
            s = sessions.get_session(conn, scope.upper())          # LookupError for an unknown session
            raise ValueError(f"session {s['id']} has no cash pool -- start one first")
        label = f"{chosen[0]['id']} {chosen[0]['name']}"
    if not chosen:
        return {"scope": scope, "label": "No session pools yet", "fills": [], "opening": 0.0, "start": None}
    since = {s["id"]: str(s.get("pool_started_at") or "") for s in chosen}
    mine = [f for f in fills if f["session"] in since and f["at"] >= since[f["session"]]]
    start = min(date.fromisoformat(v[:10]) for v in since.values() if v) if any(since.values()) else \
        (date.fromisoformat(mine[0]["at"][:10]) if mine else None)
    return {"scope": "pools" if scope.lower() == "pools" else chosen[0]["id"], "label": label, "fills": mine,
            "opening": sum(float(s["start_capital"]) for s in chosen), "start": start, "sessions": chosen}


def report(conn, scope: str = "all", by: str = "day", end: date | None = None) -> dict:
    """The P&L page: totals, periods, round-trip trades and the session pools."""
    from orders import sessions
    if by not in PERIODS:
        raise ValueError(f"by must be one of {', '.join(PERIODS)}")
    sc = _scope(conn, scope, _fills(conn))
    fills, opening = sc["fills"], sc["opening"]
    trips, _ = _replay(fills)
    days = daily(conn, fills, opening, sc["start"], end) if sc["start"] else []
    marks = {}
    for t in trips:
        if t["held_qty"] > 0:
            m = marks.setdefault(t["symbol"], mark_price(conn, t["symbol"]))
            cost_left = t["buy_value"] + t["charges"] - t["sell_value"] + t["net"]   # cost basis still held
            t["mark"] = m
            t["unrealised"] = round(m * t["held_qty"] - cost_left, 2) if m is not None else None
    equity = days[-1]["equity"] if days else opening
    closed = [t for t in trips if t["closed"]]
    wins = sum(1 for t in closed if t["net"] > 0)
    pools = [s for s in sessions.list_sessions(conn)["sessions"] if s.get("pool")]
    try:                                    # what auto-pick would do now, or why each session waits
        from orders.autopick import hold_min_score, run as autopick
        picks = {e["session"]: e for e in autopick(conn, dry_run=True)}
        hold_cut = hold_min_score(conn)
    except Exception as e:
        log.warning(f"  auto-pick status unavailable: {e}")
        picks, hold_cut = {}, None
    for t in trips:
        for k in ("buy_value", "sell_value", "charges", "net"):
            t[k] = round(t[k], 2)
        t["net_pct"] = round(t["net"] / t["buy_value"] * 100, 2) if t["closed"] and t["buy_value"] else None
    return {"scope": sc["scope"], "label": sc["label"], "by": by, "as_of": str(end or date.today()),
            "start": str(sc["start"]) if sc["start"] else None, "opening": round(opening, 2),
            "equity": round(equity, 2), "cash": days[-1]["cash"] if days else round(opening, 2),
            "pnl": round(equity - opening, 2),
            "return_pct": round((equity - opening) / opening * 100, 2) if opening else None,
            "booked": round(sum(t["net"] for t in trips), 2),
            "unrealised": round(sum(t.get("unrealised") or 0 for t in trips if t["held_qty"] > 0), 2),
            "charges": round(sum(f["fees"] for f in fills), 2),
            "trades": len(trips), "closed": len(closed), "open": len(trips) - len(closed), "wins": wins,
            "win_rate_pct": round(wins / len(closed) * 100, 1) if closed else None,
            "periods": rollup(days, trips, by), "trade_list": sorted(trips, key=lambda t: t["opened"], reverse=True),
            "sessions": [{"id": s["id"], "name": s["name"], "mode": s["mode"], "status": s["status"],
                          "target_pct": s.get("target_pct"), "stop_pct": s.get("stop_pct"),
                          "auto_pick": bool(s.get("auto_pick")), "pick": picks.get(s["id"]), **s["pool"]}
                         for s in pools],
            "occurrences": (sessions.ledger(conn, sc["scope"], history=True) or {}).get("occurrences")
            if sc["scope"] not in ("all", "pools") and sc.get("sessions") else None,
            "autopick_hold_min_score": hold_cut,
            "unpriced": sorted({u for r in days for u in r["unpriced"]})}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="ATIP paper P&L: trades and P&L by day, week or month")
    ap.add_argument("--scope", default="all", help="all (default), pools, or a session id such as S1")
    ap.add_argument("--by", default="month", choices=PERIODS)
    args = ap.parse_args()
    c = get_connection()
    try:
        r = report(c, args.scope, args.by)
    finally:
        c.close()
    print(f"{r['label']}: Rs {r['opening']:,.0f} -> Rs {r['equity']:,.0f} ({r['pnl']:+,.0f}, "
          f"{r['return_pct'] if r['return_pct'] is not None else '-'}%) since {r['start'] or '-'}; booked "
          f"{r['booked']:+,.0f}, open {r['unrealised']:+,.0f}, charges {r['charges']:,.0f}; "
          f"{r['closed']} closed / {r['open']} open, win rate {r['win_rate_pct'] or '-'}%")
    for p in r["periods"]:
        print(f"  {p['period']:<10} P&L {p['pnl']:>+11,.0f}  booked {p['booked']:>+10,.0f}  charges "
              f"{p['charges']:>7,.0f}  closed {p['trades_closed']:>2} ({p['wins']} won)  equity {p['equity']:>12,.0f}")
    for t in r["trade_list"]:
        print(f"  {t['opened'][:10]} {t['symbol']:<12} {t['session'] or '-':<4} bought {t['bought_qty']} @ "
              f"{t['avg_buy']}  sold {t['sold_qty']} @ {t['avg_sell'] or '-'}  net {t['net']:+,.2f}"
              + ("" if t["closed"] else f"  (open {t['held_qty']}, unrealised {t.get('unrealised')})"))
