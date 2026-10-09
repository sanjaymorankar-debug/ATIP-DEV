"""
Shortable instrument: a paper STOCK-FUTURES book for short legs (QR-05 / QR-06), W30.

The cash book cannot hold a short overnight in India; stock futures can. Pair trades
(kinds.PairsEvaluator) and long/short portfolios (PortfolioEvaluator) with
"short_via_futures": true now emit SHORT / COVER intents for their short legs instead of
an unexecutable SELL; the W4 risk engine sizes them in whole lots against this book's
margin, and the OMS routes them here (instrument FUT). LIVE futures are not built: the
risk engine and the adapter refuse them.

INSTRUMENT
    near-month stock (or index) future from fo_underlying_daily (the NSE F&O bhavcopy of the
    latest session): fut_close, near_expiry, lot_size. Shortable when it exists and has at
    least futures.min_days_to_expiry days left (no new shorts in the expiry week).

PRICING (honest about its limits)
    Fills at the latest stored futures close (end-of-day bhavcopy) moved against the order
    by futures.slippage_bps; there is no live futures quote. Fees: brokerage_per_lot.

LEDGER  paper_futures_position (one row per strategy x underlying x expiry; lots < 0 = short)
        paper_futures_trade    every fill, incl. EXPIRY settlements
    margin  futures.margin_pct of notional is blocked from paper_account cash on open and
            released on close; realised P&L is credited / debited on close.

EXPIRY  settle_expired(as_of) (post-market) closes every position whose expiry has passed at
        that expiry's final futures close (reason EXPIRY). With futures.auto_roll false (the
        default) there is no automatic roll: the strategy re-enters on its next decision if its
        signal still holds.

AUTO-ROLL (W40, QR-06; PAPER only, off by default)
        futures.auto_roll true: the same post-market settle_expired(as_of) first ROLLS every
        short with futures.roll_days_before_expiry (default 2) or fewer NSE sessions left to
        its expiry (utils.trading_calendar.sessions_until; 0 = on the expiry day): the held
        contract is bought back at as_of's close of THAT contract (fo_contract_daily, else the
        summary's near-month close when it is that contract) and the next month sold at its
        as_of close (fo_contract_daily: the first listed futures expiry after it), each moved
        by slippage_bps against the order and charged brokerage_per_lot x lots; the same lot
        count (re-derived for the same shares if NSE changed the lot size); the old margin is
        released and the new one blocked at futures.margin_pct of the new notional. Both fills
        are paper_futures_trade rows with reason ROLL. A roll that cannot be priced (no close
        for either contract that session) or paid for (cash for the new margin) is skipped
        and reported in "roll_skipped"; it is retried the next session, and a contract that
        reaches expiry unrolled is settled at expiry as before. After a roll the near-month
        contract in fo_underlying_daily is still the expiring one for a session or two: a
        COVER then prices off the held (later) contract's latest stored close, and book() /
        gross_notional() mark it the same way. The backtest (backtest/futures.py) rolls by the
        same rule.

config.json "futures": {"enabled": false, "margin_pct": 20, "min_days_to_expiry": 3,
                        "slippage_bps": 5, "brokerage_per_lot": 20,
                        "auto_roll": false, "roll_days_before_expiry": 2}
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger("atip.execution")

DEFAULTS = {"enabled": False, "margin_pct": 20.0, "min_days_to_expiry": 3, "slippage_bps": 5.0,
            "brokerage_per_lot": 20.0, "auto_roll": False, "roll_days_before_expiry": 2}
ALIAS = {"NIFTY50": "NIFTY"}           # ATIP index symbol -> NSE F&O underlying


def settings() -> dict:
    try:
        cfg = json.loads((Path("atip_data") / "config.json").read_text(encoding="utf-8"))
        out = {**DEFAULTS, **(cfg.get("futures") or {})}
    except Exception:
        out = dict(DEFAULTS)
    out["enabled"] = out.get("enabled") is True
    out["auto_roll"] = out.get("auto_roll") is True                 # W40: strictly opt-in
    rd = out.get("roll_days_before_expiry")
    if isinstance(rd, bool) or not isinstance(rd, int) or rd < 0:
        if rd != DEFAULTS["roll_days_before_expiry"]:
            log.warning(f"  futures.roll_days_before_expiry {rd!r} is not a whole number >= 0 -- "
                        f"{DEFAULTS['roll_days_before_expiry']} used")
        out["roll_days_before_expiry"] = DEFAULTS["roll_days_before_expiry"]
    return out


def _d(x):
    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


def contract(conn, symbol: str, as_of=None) -> dict | None:
    """The near-month future for `symbol` from the latest session <= as_of, or None."""
    und = ALIAS.get(symbol, symbol)
    sql = ("SELECT date, fut_close, near_expiry, lot_size, underlying_price FROM fo_underlying_daily WHERE symbol=? "
           "AND fut_close IS NOT NULL")
    args = [und]
    if as_of:
        sql += " AND date<=?"
        args.append(str(as_of)[:10])
    r = conn.execute(sql + " ORDER BY date DESC LIMIT 1", args).fetchone()
    if not r or not r[1] or not r[3]:
        return None
    return {"underlying": und, "session": str(r[0])[:10], "price": float(r[1]), "expiry": str(r[2])[:10] if r[2]
            else None, "lot_size": int(r[3]), "spot": r[4]}


def _contract_close(conn, underlying, expiry, as_of=None) -> float | None:
    """One contract's close: on session as_of, or (as_of None) its latest stored one.
    fo_contract_daily (every listed futures expiry), else the summary's near-month close
    when the summary's near contract is that expiry."""
    import sqlite3
    e = str(expiry)[:10]
    sql = ("SELECT close FROM fo_contract_daily WHERE symbol=? AND instrument IN ('STF','IDF') AND expiry=? "
           "AND close IS NOT NULL")
    args = [underlying, e]
    if as_of is not None:
        sql += " AND date=?"
        args.append(str(as_of)[:10])
    try:
        r = conn.execute(sql + " ORDER BY date DESC LIMIT 1", args).fetchone()
    except sqlite3.OperationalError:
        r = None
    if r and r[0]:
        return float(r[0])
    sql = "SELECT fut_close FROM fo_underlying_daily WHERE symbol=? AND near_expiry=? AND fut_close IS NOT NULL"
    args = [underlying, e]
    if as_of is not None:
        sql += " AND date=?"
        args.append(str(as_of)[:10])
    r = conn.execute(sql + " ORDER BY date DESC LIMIT 1", args).fetchone()
    return float(r[0]) if r and r[0] else None


def _next_contract(conn, underlying, as_of, after) -> dict | None:
    """The first futures contract listed on session as_of expiring after `after`, with its close."""
    import sqlite3
    try:
        r = conn.execute("SELECT expiry, close, lot_size FROM fo_contract_daily WHERE symbol=? AND date=? AND "
                         "instrument IN ('STF','IDF') AND expiry>? AND close IS NOT NULL ORDER BY expiry LIMIT 1",
                         (underlying, str(as_of)[:10], str(after)[:10])).fetchone()
    except sqlite3.OperationalError:
        r = None
    if not r or not r[1]:
        return None
    return {"underlying": underlying, "expiry": str(r[0])[:10], "price": float(r[1]),
            "lot_size": int(r[2]) if r[2] else None, "session": str(as_of)[:10]}


def _mark(conn, underlying, expiry, near=None):
    """A held contract's mark: the near-month close when the held contract is the near month (as
    before W40); after an auto-roll -- held contract later than the near one -- that
    contract's own latest close when stored."""
    c = near if near is not None else contract(conn, underlying)
    if c and expiry and c.get("expiry") and str(expiry)[:10] > str(c["expiry"])[:10]:
        px = _contract_close(conn, underlying, expiry)
        if px:
            return px
    return c["price"] if c else None


def shortable(conn, symbol: str, as_of=None) -> tuple:
    """(ok, contract or None, reason)."""
    s = settings()
    if not s["enabled"]:
        return False, None, "futures.enabled is false"
    c = contract(conn, symbol, as_of)
    if not c:
        return False, None, f"{symbol} has no stock future (not in the F&O segment, or no F&O data stored)"
    if not c["expiry"]:
        return False, c, "no expiry on the stored contract"
    dte = (_d(c["expiry"]) - _d(as_of or date.today())).days
    if dte < int(s["min_days_to_expiry"]):
        return False, c, f"near-month future expires in {dte} day(s) (< {s['min_days_to_expiry']}): no new shorts"
    return True, c, f"{c['underlying']} {c['expiry']} future, lot {c['lot_size']}, close {c['price']}"


def position(conn, strategy_id, underlying) -> dict | None:
    r = conn.execute("SELECT * FROM paper_futures_position WHERE strategy_id=? AND underlying=? AND lots<>0 "
                     "ORDER BY expiry LIMIT 1", (strategy_id or "", ALIAS.get(underlying, underlying))).fetchone()
    return dict(r) if r else None


def held_shorts(conn, strategy_id=None) -> dict:
    """{symbol: {"qty": -shares, "short": True, ...}} -- what strategies see as held shorts."""
    sql, args = "SELECT * FROM paper_futures_position WHERE lots<0", []
    if strategy_id:
        sql += " AND strategy_id=?"
        args.append(strategy_id)
    out = {}
    rev = {v: k for k, v in ALIAS.items()}
    for r in conn.execute(sql, args):
        r = dict(r)
        out[rev.get(r["underlying"], r["underlying"])] = {"qty": r["lots"] * r["lot_size"], "short": True,
                                                         "instrument": "FUT", "entry_price": r["avg_price"],
                                                         "expiry": r["expiry"], "held_sessions": None}
    return out


def _cash(conn, delta):
    conn.execute("UPDATE paper_account SET balance=balance+? WHERE id=1", (float(delta),))


def _trade(conn, order_id, strategy_id, c, side, lots, price, fees, reason):
    conn.execute("INSERT INTO paper_futures_trade (trade_id,order_id,strategy_id,underlying,expiry,side,lots,lot_size,"
                 "price,fees,reason,at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("FT" + uuid.uuid4().hex[:14].upper(), order_id, strategy_id, c["underlying"], c["expiry"], side,
                  lots, c["lot_size"], price, fees, reason, datetime.now()))


def fill(conn, order: dict) -> dict:
    """Execute an OMS FUT order on the paper futures book. SELL opens / adds a short;
    BUY covers. Returns {status, filled_qty, price, fees, message}."""
    from orders.paper import ensure_tables
    ensure_tables(conn)
    s = settings()
    c = contract(conn, order["symbol"])
    if not c:
        return {"status": "REJECTED", "message": f"no futures contract for {order['symbol']}"}
    if order["side"] != "SELL":
        # W40: after an auto-roll the held short is the NEXT month while the stored near contract is
        # still the expiring one -- cover it at the held contract's own latest close
        held = conn.execute("SELECT expiry, lot_size FROM paper_futures_position WHERE strategy_id=? AND "
                            "underlying=? AND lots<0 ORDER BY expiry LIMIT 1",
                            (order.get("strategy_id") or "", c["underlying"])).fetchone()
        if held and str(held[0])[:10] > str(c["expiry"])[:10]:
            px = _contract_close(conn, c["underlying"], held[0])
            if px:
                c = {**c, "expiry": str(held[0])[:10], "price": px, "lot_size": int(held[1])}
    lots = int(order["quantity"]) // c["lot_size"]
    if lots <= 0 or lots * c["lot_size"] != int(order["quantity"]):
        return {"status": "REJECTED", "message": f"quantity {order['quantity']} is not a whole number of lots "
                                                  f"({c['lot_size']})"}
    slip = float(s["slippage_bps"]) / 1e4
    px = round(c["price"] * (1 - slip) if order["side"] == "SELL" else c["price"] * (1 + slip), 2)
    fees = round(float(s["brokerage_per_lot"]) * lots, 2)
    sid = order.get("strategy_id") or ""
    pos = conn.execute("SELECT * FROM paper_futures_position WHERE strategy_id=? AND underlying=? AND expiry=?",
                       (sid, c["underlying"], str(c["expiry"])[:10])).fetchone()
    pos = dict(pos) if pos else None
    now = datetime.now()
    if order["side"] == "SELL":
        margin = px * lots * c["lot_size"] * float(s["margin_pct"]) / 100
        bal = conn.execute("SELECT balance FROM paper_account WHERE id=1").fetchone()[0]
        if bal < margin + fees:
            return {"status": "REJECTED", "message": f"insufficient paper cash for margin {margin:,.0f}"}
        _cash(conn, -(margin + fees))
        if pos and pos["lots"] < 0:
            n = pos["lots"] - lots
            avg = (pos["avg_price"] * -pos["lots"] + px * lots) / -n
            conn.execute("UPDATE paper_futures_position SET lots=?, avg_price=?, margin_blocked=margin_blocked+?, "
                         "realized_pnl=realized_pnl-?, updated_at=? WHERE id=?", (n, avg, margin, fees, now, pos["id"]))
        else:
            # realized_pnl carries every fee (open and close), so it equals the cash effect
            conn.execute("INSERT INTO paper_futures_position (strategy_id,underlying,expiry,lots,lot_size,avg_price,"
                         "realized_pnl,margin_blocked,opened_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
                         "ON CONFLICT(strategy_id,underlying,expiry) DO UPDATE SET lots=excluded.lots, "
                         "avg_price=excluded.avg_price, realized_pnl=realized_pnl+excluded.realized_pnl, "
                         "margin_blocked=excluded.margin_blocked, updated_at=excluded.updated_at",
                         (sid, c["underlying"], str(c["expiry"])[:10], -lots, c["lot_size"], px, -fees, margin, now,
                          now))
        msg = f"short {lots} lot(s) {c['underlying']} {c['expiry']} @ {px} (EOD futures close {c['price']})"
    else:
        # cover the strategy's short in this underlying whatever its expiry (after a month
        # rolls, the stored near contract is the NEXT one; the held one is settled by
        # settle_expired once its expiry has passed)
        pos = conn.execute("SELECT * FROM paper_futures_position WHERE strategy_id=? AND underlying=? AND lots<0 "
                           "ORDER BY expiry LIMIT 1", (sid, c["underlying"])).fetchone()
        pos = dict(pos) if pos else None
        if pos and str(pos["expiry"])[:10] != str(c["expiry"])[:10]:
            return {"status": "REJECTED", "message": f"held {c['underlying']} short is the {pos['expiry']} contract; "
                                                      f"it is settled at expiry (settle_expired), not covered here"}
        if not pos or pos["lots"] >= 0:
            return {"status": "REJECTED", "message": f"no short {c['underlying']} {c['expiry']} futures position "
                                                      f"held by {sid or 'this book'} to cover"}
        lots = min(lots, -pos["lots"])
        pnl = (pos["avg_price"] - px) * lots * c["lot_size"]
        rel = pos["margin_blocked"] * lots / -pos["lots"]
        _cash(conn, rel + pnl - fees)
        conn.execute("UPDATE paper_futures_position SET lots=lots+?, realized_pnl=realized_pnl+?, "
                     "margin_blocked=margin_blocked-?, updated_at=? WHERE id=?", (lots, pnl - fees, rel, now, pos["id"]))
        msg = f"covered {lots} lot(s) {c['underlying']} @ {px}: P&L {pnl:,.0f}"
    _trade(conn, order.get("order_id"), sid, c, order["side"], lots, px, fees, "ORDER")
    conn.commit()
    return {"status": "FILLED", "filled_qty": lots * c["lot_size"], "price": px, "fees": fees, "message": msg}


def _auto_roll(conn, as_of, s) -> tuple[list, list]:
    """W40: roll every short within roll_days_before_expiry sessions of its expiry into the next
    month at as_of's closes (module docstring). Returns (rolled, skipped)."""
    from utils.trading_calendar import sessions_until
    rd = int(s["roll_days_before_expiry"])
    slip = float(s["slippage_bps"]) / 1e4
    per_lot = float(s["brokerage_per_lot"])
    rolled, skipped = [], []
    rows = [dict(x) for x in conn.execute("SELECT * FROM paper_futures_position WHERE lots<0 AND expiry>=? "
                                          "ORDER BY strategy_id, underlying, expiry", (str(as_of),)).fetchall()]
    for r in rows:
        und, exp, sid = r["underlying"], str(r["expiry"])[:10], r["strategy_id"]
        if sessions_until(as_of, _d(exp)) > rd:
            continue
        old = _contract_close(conn, und, exp, as_of)
        nxt = _next_contract(conn, und, as_of, exp)
        why = (f"no {exp} close on {as_of}" if old is None else
               f"no next-month close stored for {as_of}" if nxt is None else None)
        lots, lot_size = -int(r["lots"]), int(r["lot_size"])
        new_lot = (nxt or {}).get("lot_size") or lot_size
        new_lots = lots if new_lot == lot_size else (lots * lot_size) // new_lot
        if why is None and new_lots < 1:
            why = f"{lots * lot_size} shares are less than one {nxt['expiry']} lot ({new_lot})"
        if why is None:
            buy_px = round(old * (1 + slip), 2)
            sell_px = round(nxt["price"] * (1 - slip), 2)
            fee_old, fee_new = round(per_lot * lots, 2), round(per_lot * new_lots, 2)
            pnl = (r["avg_price"] - buy_px) * lots * lot_size
            margin = sell_px * new_lots * new_lot * float(s["margin_pct"]) / 100
            bal = conn.execute("SELECT balance FROM paper_account WHERE id=1").fetchone()[0]
            if bal + r["margin_blocked"] + pnl - fee_old < margin + fee_new:
                why = f"paper cash does not cover the {nxt['expiry']} margin {margin:,.0f}"
        if why:
            skipped.append({"strategy_id": sid, "underlying": und, "expiry": exp, "reason": why})
            continue
        now = datetime.now()
        _cash(conn, r["margin_blocked"] + pnl - fee_old)
        conn.execute("UPDATE paper_futures_position SET lots=0, realized_pnl=realized_pnl+?, margin_blocked=0, "
                     "updated_at=? WHERE id=?", (pnl - fee_old, now, r["id"]))
        _trade(conn, None, sid, {"underlying": und, "expiry": exp, "lot_size": lot_size}, "BUY", lots, buy_px,
               fee_old, "ROLL")
        _cash(conn, -(margin + fee_new))
        cur = conn.execute("SELECT * FROM paper_futures_position WHERE strategy_id=? AND underlying=? AND expiry=?",
                           (sid, und, nxt["expiry"])).fetchone()
        cur = dict(cur) if cur else None
        if cur and cur["lots"] < 0:                  # already short that month: add to it
            n = cur["lots"] - new_lots
            avg = (cur["avg_price"] * -cur["lots"] + sell_px * new_lots) / -n
            conn.execute("UPDATE paper_futures_position SET lots=?, avg_price=?, margin_blocked=margin_blocked+?, "
                         "realized_pnl=realized_pnl-?, updated_at=? WHERE id=?",
                         (n, avg, margin, fee_new, now, cur["id"]))
        elif cur:                                    # a flat row of that month: reuse it
            conn.execute("UPDATE paper_futures_position SET lots=?, lot_size=?, avg_price=?, margin_blocked=?, "
                         "realized_pnl=realized_pnl-?, updated_at=? WHERE id=?",
                         (-new_lots, new_lot, sell_px, margin, fee_new, now, cur["id"]))
        else:
            conn.execute("INSERT INTO paper_futures_position (strategy_id,underlying,expiry,lots,lot_size,avg_price,"
                         "realized_pnl,margin_blocked,opened_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (sid, und, nxt["expiry"], -new_lots, new_lot, sell_px, -fee_new, margin, now, now))
        _trade(conn, None, sid, {"underlying": und, "expiry": nxt["expiry"], "lot_size": new_lot}, "SELL",
               new_lots, sell_px, fee_new, "ROLL")
        rolled.append({"strategy_id": sid, "underlying": und, "from_expiry": exp, "to_expiry": nxt["expiry"],
                       "lots": lots, "to_lots": new_lots, "close_price": buy_px, "open_price": sell_px,
                       "calendar_spread": round(nxt["price"] - old, 4), "fees": round(fee_old + fee_new, 2),
                       "pnl": round(pnl - fee_old, 2)})
    return rolled, skipped


def settle_expired(as_of=None) -> dict:
    """Close every position whose expiry is before `as_of` at that expiry's final futures close.
    W40: with futures.auto_roll on, shorts inside the roll window are first rolled into the
    next month instead (paper only; module docstring)."""
    from db.schema import get_connection
    as_of = _d(as_of or date.today())
    s = settings()
    conn = get_connection()
    try:
        rolled = skipped = None
        if s["auto_roll"]:
            rolled, skipped = _auto_roll(conn, as_of, s)
        done = []
        for r in [dict(x) for x in conn.execute("SELECT * FROM paper_futures_position WHERE lots<>0 AND expiry<?",
                                                (str(as_of),)).fetchall()]:
            px = conn.execute("SELECT fut_close FROM fo_underlying_daily WHERE symbol=? AND date<=? AND fut_close IS "
                              "NOT NULL ORDER BY date DESC LIMIT 1", (r["underlying"], r["expiry"])).fetchone()
            if not px:
                continue
            px = float(px[0])
            lots = -r["lots"]
            pnl = (r["avg_price"] - px) * lots * r["lot_size"]
            _cash(conn, r["margin_blocked"] + pnl)
            conn.execute("UPDATE paper_futures_position SET lots=0, realized_pnl=realized_pnl+?, margin_blocked=0, "
                         "updated_at=? WHERE id=?", (pnl, datetime.now(), r["id"]))
            _trade(conn, None, r["strategy_id"], {"underlying": r["underlying"], "expiry": r["expiry"],
                                                  "lot_size": r["lot_size"]}, "BUY", lots, px, 0.0, "EXPIRY")
            done.append({"underlying": r["underlying"], "expiry": r["expiry"], "lots": lots, "price": px,
                         "pnl": round(pnl, 2)})
        conn.commit()
        if rolled is None:
            return {"status": "SUCCESS", "rows": len(done), "settled": done}
        return {"status": "SUCCESS", "rows": len(done) + len(rolled), "settled": done, "rolled": rolled,
                "roll_skipped": skipped}
    finally:
        conn.close()


def gross_notional(conn) -> float:
    """RK-21 (W39): sum of |lots| x lot size x mark over the open positions -- the gross
    exposure the futures book adds (mark: the latest stored futures close, else the
    position's average price). 0.0 before the futures tables exist."""
    import sqlite3
    try:
        rows = conn.execute("SELECT underlying, lots, lot_size, avg_price, expiry FROM paper_futures_position "
                            "WHERE lots<>0").fetchall()
    except sqlite3.OperationalError:
        return 0.0
    total = 0.0
    for und, lots, lot_size, avg, exp in rows:
        m = _mark(conn, und, exp)                    # W40: a rolled leg marks at its own contract
        total += abs(int(lots)) * int(lot_size) * (m if m else float(avg))
    return round(total, 2)


def book(conn) -> dict:
    rows = []
    for r in conn.execute("SELECT * FROM paper_futures_position WHERE lots<>0 ORDER BY strategy_id, underlying"):
        r = dict(r)
        mark = _mark(conn, r["underlying"], r["expiry"])
        r["mark"] = mark
        r["unrealized"] = round((r["avg_price"] - mark) * -r["lots"] * r["lot_size"], 2) if mark else None
        rows.append(r)
    realized = conn.execute("SELECT COALESCE(SUM(realized_pnl),0) FROM paper_futures_position").fetchone()[0]
    return {"positions": rows, "margin_blocked": round(sum(r["margin_blocked"] for r in rows), 2),
            "unrealized": round(sum(r["unrealized"] or 0 for r in rows), 2), "realized": round(realized, 2),
            "settings": settings()}
