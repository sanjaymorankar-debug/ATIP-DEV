"""
Multi-asset trading: a paper OPTIONS book (W37: ENT-15).

ATIP trades cash equities (W4 paper / LIVE-refused) and, since W30, short stock-futures legs on a paper
futures book. This adds index and stock OPTIONS on paper, with option-specific risk -- the asset-specific
execution and risk adapter ENT-15 asked for. LIVE options are not built (like every LIVE path).

ASSET RULES (execution/assets.py lists every class and its rules)
    OPTION  long premium only: BUY to open, SELL only to close what is held -- no naked option writing
            whole lots only (lot size from the latest F&O bhavcopy contract)
            expiry today or later, and no NEW positions in the last `no_new_days_to_expiry` days
            premium of one order <= max_premium_trade_pct of the paper cash balance
            premium held in all open options <= max_premium_total_pct of the paper cash balance
            the contract must have traded (volume or OI > 0) in the latest session

PRICING
    the latest intraday option-chain snapshot (W35, option_chain_snapshot) when it is no older than
    `max_quote_age_minutes` during the session -- BUY at the ask (else LTP), SELL at the bid (else LTP);
    otherwise the latest EOD contract close (W35 fo_contract_daily), moved against the order by
    slippage_bps. Fees: brokerage_per_order + STT on sell premium (sell_stt_pct).

LEDGER   paper_options_position (underlying x expiry x strike x CE/PE; qty in units = lots x lot size)
         paper_options_trade    every fill and every expiry settlement
         cash   premium + fees debited on BUY, premium - fees credited on SELL (paper_account)
EXPIRY   settle_expired(as_of) (post-market): every position whose expiry has passed is closed at its
         intrinsic value from the underlying's close that day (CE: max(S-K,0), PE: max(K-S,0)).
MTM      book(conn) values open positions at the same quotes the fills would use.

config.json "options": {"enabled": false, "max_premium_trade_pct": 2.0, "max_premium_total_pct": 10.0,
    "no_new_days_to_expiry": 1, "max_quote_age_minutes": 30, "slippage_bps": 50, "brokerage_per_order": 20,
    "sell_stt_pct": 0.1}
Orders are entered by the owner (POST /api/execution/options/order); no strategy emits option intents yet.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

DEFAULTS = {"enabled": False, "max_premium_trade_pct": 2.0, "max_premium_total_pct": 10.0, "no_new_days_to_expiry": 1,
            "max_quote_age_minutes": 30, "slippage_bps": 50.0, "brokerage_per_order": 20.0, "sell_stt_pct": 0.1}


class OptionsRiskError(ValueError):
    pass


def settings() -> dict:
    try:
        cfg = json.loads((Path("atip_data") / "config.json").read_text(encoding="utf-8"))
        raw = cfg.get("options") or {}
    except Exception:
        raw = {}
    out = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}
    out["enabled"] = out.get("enabled") is True
    return out


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS paper_options_position (
        underlying TEXT NOT NULL, expiry DATE NOT NULL, strike REAL NOT NULL, option_type TEXT NOT NULL,
        qty REAL NOT NULL DEFAULT 0, lot_size INTEGER, avg_price REAL, realized REAL NOT NULL DEFAULT 0,
        opened_at TIMESTAMP, updated_at TIMESTAMP, PRIMARY KEY (underlying, expiry, strike, option_type))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS paper_options_trade (
        trade_id TEXT PRIMARY KEY, underlying TEXT, expiry DATE, strike REAL, option_type TEXT, side TEXT,
        lots INTEGER, lot_size INTEGER, qty REAL, price REAL, premium REAL, fees REAL, price_source TEXT,
        reason TEXT, realized REAL, at TIMESTAMP)""")
    from orders.paper import ensure_tables as paper_tables
    paper_tables(conn)


def _d(x):
    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


def _contract(conn, u, expiry, strike, ot):
    r = conn.execute("SELECT * FROM fo_contract_daily WHERE symbol=? AND expiry=? AND strike=? AND option_type=? "
                     "ORDER BY date DESC LIMIT 1", (u, str(expiry), float(strike), ot)).fetchone()
    return dict(r) if r else None


def quote(conn, u, expiry, strike, ot, side, s=None) -> dict | None:
    """{price, source, lot_size, traded} for a fill of `side`, or None when ATIP has no price."""
    s = s or settings()
    c = _contract(conn, u, expiry, strike, ot)
    lot = int(c["lot_size"]) if c and c.get("lot_size") else None
    cutoff = (datetime.now() - timedelta(minutes=int(s["max_quote_age_minutes"]))).strftime("%Y-%m-%d %H:%M:%S")
    q = conn.execute("SELECT ts, ltp, bid, ask, oi, volume FROM option_chain_snapshot WHERE symbol=? AND expiry=? AND "
                     "strike=? AND option_type=? AND ts>=? ORDER BY ts DESC LIMIT 1",
                     (u, str(expiry), float(strike), ot, cutoff)).fetchone()
    if q:
        px = (q[3] if side == "BUY" else q[2]) or q[1]
        if px and px > 0:
            return {"price": round(float(px), 2), "source": f"option chain {str(q[0])[11:16]}", "lot_size": lot,
                    "traded": bool((q[4] or 0) > 0 or (q[5] or 0) > 0)}
    if not c:
        return None
    base = c.get("close") or c.get("settle")
    if not base or base <= 0:
        return None
    adj = float(s["slippage_bps"]) / 1e4 * (1 if side == "BUY" else -1)
    return {"price": round(max(0.05, base * (1 + adj)), 2), "source": f"EOD close {c['date']} +/- slippage",
            "lot_size": lot, "traded": bool((c.get("volume") or 0) > 0 or (c.get("oi") or 0) > 0)}


def _cash(conn):
    r = conn.execute("SELECT balance FROM paper_account WHERE id=1").fetchone()
    return float(r[0]) if r else 0.0


def _premium_held(conn) -> float:
    return float(conn.execute("SELECT COALESCE(SUM(qty*avg_price),0) FROM paper_options_position WHERE qty>0").fetchone()[0])


def check(conn, o: dict, s=None) -> dict:
    """Validate an order against the OPTION asset rules. Returns the priced order or raises OptionsRiskError."""
    s = s or settings()
    u, ot, side = str(o.get("underlying") or "").upper(), str(o.get("option_type") or "").upper(), \
        str(o.get("side") or "").upper()
    if ot not in ("CE", "PE"):
        raise OptionsRiskError("option_type must be CE or PE")
    if side not in ("BUY", "SELL"):
        raise OptionsRiskError("side must be BUY or SELL")
    lots = int(o.get("lots") or 0)
    if lots <= 0:
        raise OptionsRiskError("lots must be a positive whole number")
    expiry, strike = _d(o.get("expiry")), float(o.get("strike") or 0)
    today = date.today()
    if expiry < today:
        raise OptionsRiskError(f"expiry {expiry} has passed")
    pos = conn.execute("SELECT qty, lot_size FROM paper_options_position WHERE underlying=? AND expiry=? AND strike=? "
                       "AND option_type=?", (u, str(expiry), strike, ot)).fetchone()
    held = float(pos[0]) if pos else 0.0
    q = quote(conn, u, expiry, strike, ot, side, s)
    if not q:
        raise OptionsRiskError(f"no price for {u} {expiry} {strike:g} {ot}: the contract is not in the latest F&O data")
    lot = q["lot_size"] or (int(pos[1]) if pos and pos[1] else None)
    if not lot:
        raise OptionsRiskError("lot size unknown for this contract")
    qty = lots * lot
    if side == "SELL":
        if qty > held + 1e-9:
            raise OptionsRiskError(f"SELL {qty:g} exceeds the {held:g} held: writing (short) options is not allowed")
    else:
        if (expiry - today).days < int(s["no_new_days_to_expiry"]):
            raise OptionsRiskError(f"no new positions within {s['no_new_days_to_expiry']} day(s) of expiry")
        if not q["traded"]:
            raise OptionsRiskError("the contract had no volume or open interest in the latest data")
        cash = _cash(conn)
        prem = qty * q["price"]
        if cash <= 0 or prem > cash * float(s["max_premium_trade_pct"]) / 100:
            raise OptionsRiskError(f"premium Rs {prem:,.0f} exceeds {s['max_premium_trade_pct']}% of paper cash "
                                   f"(Rs {cash:,.0f})")
        if _premium_held(conn) + prem > cash * float(s["max_premium_total_pct"]) / 100:
            raise OptionsRiskError(f"total option premium would exceed {s['max_premium_total_pct']}% of paper cash")
    return {"underlying": u, "expiry": str(expiry), "strike": strike, "option_type": ot, "side": side, "lots": lots,
            "lot_size": lot, "qty": qty, "price": q["price"], "price_source": q["source"], "held_before": held}


def _fees(side, premium, s):
    return round(float(s["brokerage_per_order"]) + (premium * float(s["sell_stt_pct"]) / 100 if side == "SELL" else 0), 2)


def _apply(conn, c, price, fees, reason, source):
    now = datetime.now()
    key = (c["underlying"], c["expiry"], c["strike"], c["option_type"])
    pos = conn.execute("SELECT qty, avg_price, realized FROM paper_options_position WHERE underlying=? AND expiry=? AND "
                       "strike=? AND option_type=?", key).fetchone()
    qty0, avg0, real0 = (float(pos[0]), float(pos[1] or 0), float(pos[2] or 0)) if pos else (0.0, 0.0, 0.0)
    premium = c["qty"] * price
    realized = 0.0
    if c["side"] == "BUY":
        new_qty = qty0 + c["qty"]
        new_avg = (qty0 * avg0 + premium) / new_qty
        cash_delta = -(premium + fees)
    else:
        new_qty = qty0 - c["qty"]
        new_avg = avg0
        realized = c["qty"] * (price - avg0) - fees
        cash_delta = premium - fees
    conn.execute("INSERT INTO paper_options_position (underlying,expiry,strike,option_type,qty,lot_size,avg_price,realized,"
                 "opened_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(underlying,expiry,strike,option_type) "
                 "DO UPDATE SET qty=excluded.qty, avg_price=excluded.avg_price, realized=excluded.realized, "
                 "updated_at=excluded.updated_at",
                 (*key, new_qty, c["lot_size"], round(new_avg, 4) if new_qty else None, real0 + realized, now, now))
    conn.execute("UPDATE paper_account SET balance=balance+? WHERE id=1", (cash_delta,))
    tid = "OT" + uuid.uuid4().hex[:14].upper()
    conn.execute("INSERT INTO paper_options_trade (trade_id,underlying,expiry,strike,option_type,side,lots,lot_size,qty,"
                 "price,premium,fees,price_source,reason,realized,at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (tid, *key, c["side"], c["lots"], c["lot_size"], c["qty"], price, round(premium, 2), fees, source,
                  reason, round(realized, 2), now))
    return tid, round(realized, 2), round(cash_delta, 2)


def place(conn, o: dict, actor="owner") -> dict:
    s = settings()
    if not s["enabled"]:
        raise OptionsRiskError("the paper options book is off (config.json options.enabled)")
    ensure_tables(conn)
    c = check(conn, o, s)
    fees = _fees(c["side"], c["qty"] * c["price"], s)
    tid, realized, cash_delta = _apply(conn, c, c["price"], fees, f"manual order by {actor}", c["price_source"])
    conn.commit()
    return {"trade_id": tid, **c, "fees": fees, "cash_change": cash_delta, "realized": realized}


def settle_expired(as_of=None, conn=None) -> dict:
    from db.schema import get_connection
    own = conn is None
    conn = conn or get_connection()
    try:
        ensure_tables(conn)
        d = _d(as_of or date.today())
        s = settings()
        n = 0
        for u, exp, k, ot, qty, lot in conn.execute(
                "SELECT underlying, expiry, strike, option_type, qty, lot_size FROM paper_options_position WHERE qty>0 "
                "AND expiry<=?", (str(d),)).fetchall():
            r = conn.execute("SELECT underlying_price FROM fo_underlying_daily WHERE symbol=? AND date<=? ORDER BY date "
                             "DESC LIMIT 1", (u, str(exp))).fetchone()
            spot = float(r[0]) if r and r[0] else None
            if spot is None:
                r = conn.execute("SELECT close FROM prices_daily WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 1",
                                 (u, str(exp))).fetchone()
                spot = float(r[0]) if r and r[0] else None
            if spot is None:
                continue
            intrinsic = max(spot - k, 0.0) if ot == "CE" else max(k - spot, 0.0)
            c = {"underlying": u, "expiry": str(exp), "strike": k, "option_type": ot, "side": "SELL",
                 "lots": int(qty / lot) if lot else 0, "lot_size": lot, "qty": qty}
            fees = _fees("SELL", qty * intrinsic, s) if intrinsic > 0 else 0.0
            _apply(conn, c, round(intrinsic, 2), fees, f"EXPIRY settlement at underlying {spot:g}", "intrinsic value")
            n += 1
        conn.commit()
        return {"status": "SUCCESS", "rows": n}
    finally:
        if own:
            conn.close()


def book(conn) -> dict:
    ensure_tables(conn)
    s = settings()
    rows = []
    for r in conn.execute("SELECT * FROM paper_options_position WHERE qty<>0 ORDER BY expiry, underlying, strike"):
        p = dict(r)
        q = quote(conn, p["underlying"], p["expiry"], p["strike"], p["option_type"], "SELL", s)
        p["mark"] = q["price"] if q else None
        p["mark_source"] = q["source"] if q else "no price"
        p["unrealized"] = round(p["qty"] * (p["mark"] - p["avg_price"]), 2) if q and p["avg_price"] is not None else None
        rows.append(p)
    trades = [dict(r) for r in conn.execute("SELECT * FROM paper_options_trade ORDER BY at DESC LIMIT 50")]
    realized = float(conn.execute("SELECT COALESCE(SUM(realized),0) FROM paper_options_trade").fetchone()[0])
    return {"enabled": s["enabled"], "cash": round(_cash(conn), 2), "premium_held": round(_premium_held(conn), 2),
            "positions": rows, "realized_total": round(realized, 2), "recent_trades": trades, "limits": s}
