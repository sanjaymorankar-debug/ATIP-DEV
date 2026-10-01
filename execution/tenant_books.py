"""
Per-tenant PAPER books (W9, closes W7-R3).

The owner's tenant ("default", enterprise.default_tenant) keeps the existing W1
paper book exactly as before (orders/paper.py PaperBroker, paper_position,
paper_account, pnl_daily). Every OTHER tenant gets its own simulated book:

    tenant_paper_account   tenant_id, starting_cash, cash, realized_pnl, peak_equity
    tenant_paper_position  (tenant_id, symbol) quantity, avg_price, realized_pnl
    tenant_paper_fill      every simulated fill (order_id, side, qty, price, fees)
    tenant_pnl_daily       (tenant_id, date) equity snapshot -> daily-loss / drawdown limits

A tenant's order fills IMMEDIATELY and only in its own book:
    MARKET  at the order's reference price (the decision close), else the latest close
    LIMIT   fills at the limit only when marketable against that price, else REJECTED
    BUY     needs cash >= value + fees; SELL needs the quantity held
Nothing here talks to any broker. LIVE is owner-only and remains gated off
(execution/config.live_gate); other tenants can never be routed to LIVE.

Tenancy of an order / intent comes from its strategy (strategy.tenant_id), the same
rule the W7 middleware uses.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

DEFAULT_STARTING_CASH = 1_000_000.0
FEE_RATE = 0.001                           # 0.1% simulated charges per side (flat, documented)


def default_tenant() -> str:
    try:
        from enterprise.config import settings
        return settings()["default_tenant"]
    except Exception:
        return "default"


def is_default(tenant_id) -> bool:
    return tenant_id in (None, "", default_tenant())


def tenant_of_strategy(conn, strategy_id) -> str:
    try:
        r = conn.execute("SELECT COALESCE(tenant_id,'default') FROM strategy WHERE strategy_id=?",
                         (strategy_id,)).fetchone()
        return r[0] if r else default_tenant()
    except Exception:
        return default_tenant()


def ensure_account(conn, tenant_id, starting_cash=None) -> dict:
    r = conn.execute("SELECT * FROM tenant_paper_account WHERE tenant_id=?", (tenant_id,)).fetchone()
    if r:
        return dict(r)
    cash = float(starting_cash if starting_cash is not None else _plan_cash(conn, tenant_id))
    now = datetime.now()
    conn.execute("INSERT INTO tenant_paper_account (tenant_id,starting_cash,cash,realized_pnl,peak_equity,created_at,"
                 "updated_at) VALUES (?,?,?,0,?,?,?)", (tenant_id, cash, cash, cash, now, now))
    conn.commit()
    return dict(conn.execute("SELECT * FROM tenant_paper_account WHERE tenant_id=?", (tenant_id,)).fetchone())


def _plan_cash(conn, tenant_id) -> float:
    try:
        from enterprise.tenants import get
        t = get(conn, tenant_id) or {}
        v = (t.get("settings") or {}).get("paper_starting_cash")
        return float(v) if v else DEFAULT_STARTING_CASH
    except Exception:
        return DEFAULT_STARTING_CASH


def _mark(conn, symbol):
    from execution.positions import latest_close
    return latest_close(conn, symbol)[0]


def held_quantity(conn, tenant_id, symbol) -> int:
    r = conn.execute("SELECT quantity FROM tenant_paper_position WHERE tenant_id=? AND symbol=?",
                     (tenant_id, symbol)).fetchone()
    return int(r[0] or 0) if r else 0


def book(conn, tenant_id) -> dict:
    """Same shape as portfolio.pnl.portfolio_summary(), for one tenant's book."""
    acct = ensure_account(conn, tenant_id)
    pos = []
    for sym, qty, avg in conn.execute("SELECT symbol, quantity, avg_price FROM tenant_paper_position WHERE "
                                      "tenant_id=? AND quantity>0 ORDER BY symbol", (tenant_id,)):
        mark = _mark(conn, sym)
        value = qty * mark if mark is not None else None
        cost = qty * avg
        pos.append({"symbol": sym, "qty": int(qty), "avg_price": float(avg), "mark": mark, "value": value,
                    "cost": cost, "unrealised": (value - cost) if value is not None else None})
    pv = round(sum(p["value"] for p in pos if p["value"] is not None), 2)
    cash = round(float(acct["cash"]), 2)
    return {"env": "PAPER", "tenant_id": tenant_id, "as_of": str(date.today()), "positions": pos,
            "n_positions": len(pos), "unvalued": [p["symbol"] for p in pos if p["value"] is None],
            "positions_value": pv, "cost": round(sum(p["cost"] for p in pos), 2),
            "unrealised": round(sum(p["unrealised"] or 0 for p in pos), 2),
            "realised_cum": round(float(acct["realized_pnl"] or 0), 2), "cash": cash,
            "equity": round(cash + pv, 2), "starting_cash": acct["starting_cash"]}


def apply_fill(conn, tenant_id, order_id, symbol, side, qty, price) -> dict:
    """Book one fill. Raises ValueError when cash / holdings do not allow it."""
    acct = ensure_account(conn, tenant_id)
    value = qty * price
    fees = round(value * FEE_RATE, 2)
    cur = conn.execute("SELECT quantity, avg_price, realized_pnl FROM tenant_paper_position WHERE tenant_id=? AND "
                       "symbol=?", (tenant_id, symbol)).fetchone()
    held, avg, rpnl = (int(cur[0]), float(cur[1]), float(cur[2] or 0)) if cur else (0, 0.0, 0.0)
    now = datetime.now()
    if side == "BUY":
        if acct["cash"] < value + fees:
            raise ValueError(f"insufficient paper cash ({acct['cash']:,.2f} < {value + fees:,.2f})")
        new_q = held + qty
        new_avg = (held * avg + value) / new_q
        cash = acct["cash"] - value - fees
        realised = 0.0
    else:
        if qty > held:
            raise ValueError(f"only {held} {symbol} held")
        new_q, new_avg = held - qty, (avg if held - qty else 0.0)
        realised = (price - avg) * qty - fees
        cash = acct["cash"] + value - fees
    conn.execute("INSERT INTO tenant_paper_position (tenant_id,symbol,quantity,avg_price,realized_pnl,updated_at) "
                 "VALUES (?,?,?,?,?,?) ON CONFLICT(tenant_id,symbol) DO UPDATE SET quantity=excluded.quantity, "
                 "avg_price=excluded.avg_price, realized_pnl=excluded.realized_pnl, updated_at=excluded.updated_at",
                 (tenant_id, symbol, new_q, round(new_avg, 6), round(rpnl + realised, 2), now))
    conn.execute("UPDATE tenant_paper_account SET cash=?, realized_pnl=realized_pnl+?, updated_at=? WHERE tenant_id=?",
                 (round(cash, 2), round(realised, 2), now, tenant_id))
    fid = "tpf_" + uuid.uuid4().hex[:16]
    conn.execute("INSERT INTO tenant_paper_fill (fill_id,tenant_id,order_id,symbol,side,quantity,price,fees,filled_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (fid, tenant_id, order_id, symbol, side, qty, price, fees, now))
    conn.commit()
    return {"fill_id": fid, "fees": fees}


def snapshot(conn, tenant_id, on=None) -> dict:
    """End-of-day equity row (tenant_pnl_daily) for the daily-loss / drawdown limits."""
    on = on or date.today()
    b = book(conn, tenant_id)
    prev = conn.execute("SELECT equity, peak_equity FROM tenant_pnl_daily WHERE tenant_id=? AND date<? ORDER BY date "
                        "DESC LIMIT 1", (tenant_id, str(on))).fetchone()
    peak = max(b["equity"], float(prev[1]) if prev else b["starting_cash"])
    day = round(b["equity"] - float(prev[0]), 2) if prev else None
    conn.execute("INSERT INTO tenant_pnl_daily (tenant_id,date,equity,cash,positions_value,day_pnl,peak_equity,"
                 "drawdown_pct,recorded_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(tenant_id,date) DO UPDATE SET "
                 "equity=excluded.equity, cash=excluded.cash, positions_value=excluded.positions_value, "
                 "day_pnl=excluded.day_pnl, peak_equity=excluded.peak_equity, drawdown_pct=excluded.drawdown_pct, "
                 "recorded_at=excluded.recorded_at",
                 (tenant_id, str(on), b["equity"], b["cash"], b["positions_value"], day, peak,
                  round((peak - b["equity"]) / peak * 100, 4) if peak else 0.0, datetime.now()))
    conn.execute("UPDATE tenant_paper_account SET peak_equity=MAX(COALESCE(peak_equity,0), ?) WHERE tenant_id=?",
                 (peak, tenant_id))
    conn.commit()
    return {"tenant_id": tenant_id, "equity": b["equity"], "day_pnl": day, "peak_equity": peak}


def snapshot_all(conn, on=None) -> int:
    n = 0
    for (tid,) in conn.execute("SELECT tenant_id FROM tenant_paper_account").fetchall():
        snapshot(conn, tid, on)
        n += 1
    return n


def risk_state(conn, tenant_id) -> dict:
    """Mirror of portfolio.pnl.risk_state for a tenant book: day P&L vs the last snapshot."""
    b = book(conn, tenant_id)
    prev = conn.execute("SELECT equity, peak_equity FROM tenant_pnl_daily WHERE tenant_id=? AND date<? ORDER BY date "
                        "DESC LIMIT 1", (tenant_id, str(date.today()))).fetchone()
    base = float(prev[0]) if prev else float(b["starting_cash"])       # first day: against the starting cash
    peak = max(b["equity"], float(prev[1]) if prev else float(b["starting_cash"]))
    return {"equity": b["equity"], "day_pnl": round(b["equity"] - base, 2), "peak_equity": peak,
            "drawdown_pct": round((peak - b["equity"]) / peak * 100, 4) if peak else 0.0,
            "prev_date": None, "measurable": True}


def orders_today(conn, tenant_id) -> int:
    return conn.execute("SELECT COUNT(*) FROM oms_order o JOIN strategy s ON s.strategy_id=o.strategy_id WHERE "
                        "COALESCE(s.tenant_id,'default')=? AND DATE(o.created_at)=?",
                        (tenant_id, str(date.today()))).fetchone()[0]
