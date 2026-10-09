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
    "sell_stt_pct": 0.1, "naked_margin_pct": 15, "naked_stress_move_pct": 15, "max_margin_total_pct": 30,
    "max_chain_age_sessions": 2}
Owner orders are entered by hand (POST /api/execution/options/order): long premium only, as above.

W40 (ENT-15): STRATEGY POSITIONS -- option-overlay strategies (strategy_engine/option_overlay.py)
    emit multi-leg OPTION intents; the W4 risk engine sizes them (execution/option_intents.py) and the
    OMS routes each approved one here as ONE order (instrument OPT, adapter paper_opt), filled
    all-or-nothing by fill_strategy_order(). These positions may hold SHORT legs (spreads, condors,
    covered calls, and naked shorts only where the definition allows them) and live in their own
    ledger, keyed by strategy, so they never mix with the owner's long-only book above:
        paper_option_strategy_position  one row per multi-leg position (status OPEN / CLOSED / SETTLED)
        paper_option_strategy_leg       its legs: type, strike, expiry, side, qty, entry / exit / mark
        paper_option_strategy_trade     every leg fill (open, close, expiry settlement)
        paper_option_strategy_mark      one mark per position per session
    FILL RULE   each leg at the chain mid (data/derivatives_store.chain_on: that day's snapshot mid of
                bid / ask when no older than max_quote_age_minutes, else the latest EOD close / settle)
                moved AGAINST the order by slippage_bps: BUY at mid x (1 + s), SELL at mid x (1 - s),
                never below 0.05. A leg with no price, an untraded contract, a chain more than
                max_chain_age_sessions sessions old or too little cash rejects the WHOLE order.
    CASH        open: + net premium (credit) or - debit, - fees, - margin blocked; close: + / - the
                legs' closing value, - fees, + margin released. realized_pnl carries every fee, so a
                closed position's realized_pnl equals its total cash effect.
    MARGIN      (an approximation -- not SPAN): see structure_metrics()
    MARK        mark_strategy_positions(as_of): every open position at the session's mid (no slippage),
                one paper_option_strategy_mark row per day, with the exit rule it would trigger
    EXPIRY      settle_expired() also closes strategy positions whose expiry has passed: every leg at
                its intrinsic value from the underlying's close ON the expiry day.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

DEFAULTS = {"enabled": False, "max_premium_trade_pct": 2.0, "max_premium_total_pct": 10.0, "no_new_days_to_expiry": 1,
            "max_quote_age_minutes": 30, "slippage_bps": 50.0, "brokerage_per_order": 20.0, "sell_stt_pct": 0.1,
            # W40 strategy positions: the margin approximation and its limits (structure_metrics)
            "naked_margin_pct": 15.0, "naked_stress_move_pct": 15.0, "max_margin_total_pct": 30.0,
            "max_chain_age_sessions": 2}


class OptionsRiskError(ValueError):
    pass


# W40 (ENT-15): the strategy-position ledger. Registered in db/schema_w39b.py; ensure_tables() creates it too.
STRATEGY_TABLES = {
    "paper_option_strategy_position": (
        """CREATE TABLE IF NOT EXISTS paper_option_strategy_position (
            position_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, strategy_version TEXT, symbol TEXT NOT NULL,
            underlying TEXT NOT NULL, template TEXT, expiry DATE, lots INTEGER NOT NULL, lot_size INTEGER NOT NULL,
            status TEXT NOT NULL, spot_at_open REAL, net_premium REAL, max_profit REAL, max_loss REAL,
            risk_amount REAL, profit_base REAL, loss_base REAL, margin_blocked REAL NOT NULL DEFAULT 0,
            covered_shares INTEGER NOT NULL DEFAULT 0, fees REAL NOT NULL DEFAULT 0,
            realized_pnl REAL NOT NULL DEFAULT 0, unrealized REAL, liq_value REAL, last_mark_date DATE,
            exits_json TEXT, metrics_json TEXT, open_order_id TEXT, close_order_id TEXT, exit_reason TEXT,
            opened_at TIMESTAMP, closed_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_opt_strat_position ON paper_option_strategy_position(strategy_id, status)",
    ),
    "paper_option_strategy_leg": (
        """CREATE TABLE IF NOT EXISTS paper_option_strategy_leg (
            position_id TEXT NOT NULL, leg_no INTEGER NOT NULL, option_type TEXT NOT NULL, strike REAL NOT NULL,
            expiry DATE NOT NULL, side TEXT NOT NULL, lots INTEGER NOT NULL, qty INTEGER NOT NULL, role TEXT,
            entry_price REAL, entry_source TEXT, exit_price REAL, exit_source TEXT, mark REAL,
            PRIMARY KEY (position_id, leg_no))""",
    ),
    "paper_option_strategy_trade": (
        """CREATE TABLE IF NOT EXISTS paper_option_strategy_trade (
            trade_id TEXT PRIMARY KEY, position_id TEXT NOT NULL, strategy_id TEXT, order_id TEXT, leg_no INTEGER,
            underlying TEXT, expiry DATE, strike REAL, option_type TEXT, side TEXT, qty INTEGER, price REAL,
            fees REAL, price_source TEXT, reason TEXT, at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_opt_strat_trade ON paper_option_strategy_trade(position_id)",
    ),
    "paper_option_strategy_mark": (
        """CREATE TABLE IF NOT EXISTS paper_option_strategy_mark (
            position_id TEXT NOT NULL, date DATE NOT NULL, strategy_id TEXT, spot REAL, liq_value REAL,
            unrealized REAL, complete INTEGER, exit_signal TEXT, source TEXT, created_at TIMESTAMP,
            PRIMARY KEY (position_id, date))""",
    ),
}


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
    for ddls in STRATEGY_TABLES.values():                 # W40 strategy positions (also in db/schema_w39b.py)
        for ddl in ddls:
            conn.execute(ddl)
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
            spot = settlement_spot(conn, u, exp)
            if spot is None:
                continue
            value = intrinsic(ot, k, spot)
            c = {"underlying": u, "expiry": str(exp), "strike": k, "option_type": ot, "side": "SELL",
                 "lots": int(qty / lot) if lot else 0, "lot_size": lot, "qty": qty}
            fees = _fees("SELL", qty * value, s) if value > 0 else 0.0
            _apply(conn, c, round(value, 2), fees, f"EXPIRY settlement at underlying {spot:g}", "intrinsic value")
            n += 1
        conn.commit()
        ns = settle_strategy_positions(conn, d, s)         # W40: option-overlay strategy positions
        return {"status": "SUCCESS", "rows": n + ns, "strategy_positions": ns}
    finally:
        if own:
            conn.close()


def intrinsic(option_type: str, strike: float, spot: float) -> float:
    """Value at expiry per unit: CE max(S - K, 0), PE max(K - S, 0)."""
    return max(spot - float(strike), 0.0) if option_type == "CE" else max(float(strike) - spot, 0.0)


def settlement_spot(conn, underlying: str, expiry, exact: bool = False) -> float | None:
    """The underlying's close for an expiry: the F&O bhavcopy's underlying price, else the cash close
    (the F&O name, then ATIP's own symbol: NIFTY -> NIFTY50). exact=True (strategy positions) takes
    only the expiry day's own close, so a settlement never runs on the day before's price."""
    from data.derivatives_store import atip_symbol
    op, e = ("=", str(expiry)[:10]) if exact else ("<=", str(expiry)[:10])
    r = conn.execute(f"SELECT underlying_price FROM fo_underlying_daily WHERE symbol=? AND date{op}? AND "
                     f"underlying_price>0 ORDER BY date DESC LIMIT 1", (underlying, e)).fetchone()
    if r and r[0]:
        return float(r[0])
    for sym in dict.fromkeys((underlying, atip_symbol(underlying))):
        r = conn.execute(f"SELECT close FROM prices_daily WHERE symbol=? AND date{op}? AND close>0 ORDER BY date DESC "
                         f"LIMIT 1", (sym, e)).fetchone()
        if r and r[0]:
            return float(r[0])
    return None


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
            "positions": rows, "realized_total": round(realized, 2), "recent_trades": trades, "limits": s,
            # W40: option-overlay strategies' open multi-leg positions (their own ledger, below)
            "strategy_positions": strategy_positions(conn, status="OPEN")}


# ══ W40 (ENT-15): option-overlay strategy positions ═══════════════════════════════════════════════

SIGN = {"BUY": 1, "SELL": -1}
CLOSING = {"BUY": "SELL", "SELL": "BUY"}


def _rej(message: str) -> dict:
    return {"status": "REJECTED", "message": message}


def sessions_behind(session, today=None) -> int:
    """Trading sessions between a stored session and `today` (0 = the same session)."""
    from utils.trading_calendar import is_trading_day
    s, probe, n = _d(session), _d(today or date.today()), 0
    while probe > s and n < 60:
        if is_trading_day(probe):
            n += 1
        probe -= timedelta(days=1)
    return n


def chain_for_fill(conn, underlying: str, s=None) -> dict:
    """The chain a fill / close uses now: a same-day snapshot no older than max_quote_age_minutes,
    else the latest EOD session (data/derivatives_store.chain_on)."""
    from data.derivatives_store import chain_on
    s = s or settings()
    return chain_on(conn, underlying, None, fresh_minutes=int(s["max_quote_age_minutes"]))


def chain_age(ch: dict, today=None) -> int | None:
    """Sessions since the chain's data (None: no chain at all)."""
    if ch.get("snapshot_ts"):
        return 0
    return sessions_behind(ch["session"], today) if ch.get("session") else None


def leg_quote(conn, underlying, expiry, strike, option_type, side, s=None, chain=None) -> dict | None:
    """The W40 FILL RULE for one strategy leg: the chain mid moved against the order by slippage_bps
    (BUY at mid x (1 + s), SELL at mid x (1 - s), floor 0.05). None when ATIP has no price."""
    s = s or settings()
    ch = chain if chain is not None else chain_for_fill(conn, underlying, s)
    q = ch["quotes"].get((str(expiry)[:10], float(strike), option_type))
    if not q:
        return None
    bps = float(s["slippage_bps"])
    adj = bps / 1e4 * (1 if side == "BUY" else -1)
    return {"price": round(max(0.05, q["price"] * (1 + adj)), 2), "mid": q["price"],
            "source": f"{q['source']} mid {'+' if side == 'BUY' else '-'} {bps:g} bps",
            "lot_size": q.get("lot_size") or ch.get("lot_size"), "iv": q.get("iv"),
            "traded": bool((q.get("oi") or 0) > 0 or (q.get("volume") or 0) > 0)}


def structure_metrics(legs: list, spot: float, lot_size: int, lots: int, *, cover_shares: int = 0,
                      uses_shares: bool = False, allow_naked: bool = False, s=None, as_of=None) -> dict:
    """
    Risk, margin and cash of a multi-leg position of `lots` structure lots. `legs` are ONE structure lot
    each: [{option_type, side, strike, expiry, price (per unit: the expected fill), iv (decimal) or None}].

    payoff       quant/options_strategy.analyse at the first expiry: net premium (credit > 0), max profit /
                 max loss (None = unlimited), breakevens, probability of profit, net Greeks
    naked calls  short calls not matched lot for lot by long calls of the same expiry. With uses_shares
                 (covered_call) they are covered by `cover_shares` free held shares when there are enough;
                 otherwise they are NAKED and `refused` says why -- unless allow_naked
    risk_amount  what the position can lose; it counts toward the strategy's max loss:
                   bounded payoff         -max_loss
                   share-covered call     0 (the premium is in hand; the shares pay for a rally)
                   allowed naked call     the expiry loss after a naked_stress_move_pct% (15) rally
    MARGIN       the paper book's APPROXIMATION of SPAN + exposure -- not the exchange's figure:
                   long options only              0 (premium paid in full)
                   defined risk (every short leg matched by a long leg of the same type and expiry)
                                                  max(0, risk_amount - debit): a credit spread blocks its
                                                  max loss, a debit spread nothing beyond the debit
                   share-covered short call       0 (the held shares are the collateral)
                   naked short leg (unmatched short put, allowed naked call)
                                                  naked_margin_pct% (15) x spot x quantity, per leg
    fees         per leg: brokerage_per_order + STT on SELL premium (sell_stt_pct), as the owner book
    profit_base  max profit; when unlimited, the debit paid (a profit-take is then % of premium paid)
    loss_base    -max loss; when unlimited, the credit received (a stop is then % of the credit)
    """
    from quant.options_strategy import analyse, pnl_at
    s = s or settings()
    L, n = int(lot_size), int(lots)
    q = L * n
    a = analyse([{"kind": l["option_type"], "side": l["side"], "strike": float(l["strike"]),
                  "expiry": str(l["expiry"])[:10], "lots": n, "premium": float(l["price"]), "iv": l.get("iv")}
                 for l in legs], spot, lot_size=L, as_of=as_of)
    net = float(a["net_premium"])
    debit, credit = max(0.0, -net), max(0.0, net)
    pairs = {}
    for l in legs:
        b = pairs.setdefault((l["option_type"], str(l["expiry"])[:10]), [0, 0])
        b[0 if l["side"] == "BUY" else 1] += 1
    naked_calls = sum(max(0, sh - lg) for (ot, _e), (lg, sh) in pairs.items() if ot == "CE")
    naked_puts = sum(max(0, sh - lg) for (ot, _e), (lg, sh) in pairs.items() if ot == "PE")
    covered, refused = 0, None
    if naked_calls:
        need = naked_calls * q
        if uses_shares and cover_shares >= need:
            covered, naked_calls = need, 0
        elif not allow_naked:
            refused = (f"naked short call: {naked_calls * n} short call lot(s) ({need} units) have no long call "
                       f"of the same expiry" + (f" and only {cover_shares} free shares cover them" if uses_shares
                                                else "") + " -- the definition does not allow naked short calls")
    if a["max_loss"] is not None:
        risk = -float(a["max_loss"])
    elif covered and not naked_calls:
        risk = 0.0
    else:
        exp1 = min(_d(l["expiry"]) for l in legs)
        signed = [{**l, "sign": SIGN[l["side"]]} for l in a["legs"]]
        risk = max(0.0, -pnl_at(signed, spot * (1 + float(s["naked_stress_move_pct"]) / 100), exp1, L))
    naked_margin = (naked_calls + naked_puts) * q * float(spot) * float(s["naked_margin_pct"]) / 100
    margin = naked_margin if (naked_calls or naked_puts) else max(0.0, risk - debit)
    fees = round(sum(_fees(l["side"], q * float(l["price"]), s) for l in legs), 2)
    mp, ml = a["max_profit"], a["max_loss"]
    return {"lots": n, "lot_size": L, "qty": q, "net_premium": round(net, 2), "debit": round(debit, 2),
            "credit": round(credit, 2), "fees": fees, "margin": round(margin, 2), "risk_amount": round(risk, 2),
            "cash_needed": round(debit + fees + margin, 2), "max_profit": mp, "max_loss": ml,
            "breakevens": a["breakevens"], "pop_pct": a["probability_of_profit_pct"], "greeks": a["greeks"],
            "reward_to_risk": a["reward_to_risk"], "naked_call_lots": naked_calls * n, "naked_put_lots": naked_puts * n,
            "covered_shares": covered, "refused": refused,
            "profit_base": round(mp, 2) if mp is not None else (round(debit, 2) or None),
            "profit_base_kind": "max profit" if mp is not None else "premium paid",
            "loss_base": round(-ml, 2) if ml is not None else (round(credit, 2) or round(risk, 2) or None),
            "loss_base_kind": "max loss" if ml is not None else ("credit received" if credit else "stress loss"),
            "margin_rule": ("naked: naked_margin_pct of notional per naked leg" if (naked_calls or naked_puts) else
                            "share-covered call: 0" if covered else "defined risk: max(0, max loss - debit)")}


def cover_available(conn, symbol: str) -> int:
    """Shares of `symbol` held in the paper cash book that no open covered call already covers."""
    try:
        r = conn.execute("SELECT quantity FROM paper_position WHERE symbol=?", (symbol,)).fetchone()
        held = int(r[0] or 0) if r else 0
        used = conn.execute("SELECT COALESCE(SUM(covered_shares),0) FROM paper_option_strategy_position WHERE "
                            "symbol=? AND status='OPEN'", (symbol,)).fetchone()[0]
    except Exception:
        return 0
    return max(0, held - int(used or 0))


def _legs(conn, position_id) -> list:
    return [dict(r) for r in conn.execute("SELECT * FROM paper_option_strategy_leg WHERE position_id=? ORDER BY leg_no",
                                          (position_id,))]


def get_position(conn, position_id: str) -> dict | None:
    try:
        r = conn.execute("SELECT * FROM paper_option_strategy_position WHERE position_id=?", (position_id,)).fetchone()
    except Exception:
        return None
    if not r:
        return None
    p = dict(r)
    p["legs"] = _legs(conn, position_id)
    p["exits"] = json.loads(p.get("exits_json") or "{}")
    p["metrics"] = json.loads(p.get("metrics_json") or "{}")
    return p


def open_positions(conn, strategy_id: str | None = None, symbol: str | None = None) -> list:
    """Open strategy positions (with legs); [] before the tables exist."""
    sql, args = "SELECT position_id FROM paper_option_strategy_position WHERE status='OPEN'", []
    if strategy_id is not None:
        sql += " AND strategy_id=?"
        args.append(strategy_id)
    if symbol is not None:
        sql += " AND symbol=?"
        args.append(symbol)
    try:
        ids = [r[0] for r in conn.execute(sql + " ORDER BY opened_at, position_id", args)]
    except Exception:
        return []
    return [get_position(conn, i) for i in ids]


def strategy_positions(conn, strategy_id: str | None = None, status: str | None = None, limit: int = 100) -> list:
    """Strategy positions, newest first, with legs and their last 30 marks."""
    sql, args = "SELECT position_id FROM paper_option_strategy_position WHERE 1=1", []
    if strategy_id:
        sql += " AND strategy_id=?"
        args.append(strategy_id)
    if status:
        sql += " AND status=?"
        args.append(status)
    try:
        ids = [r[0] for r in conn.execute(sql + " ORDER BY opened_at DESC, position_id LIMIT ?", args + [int(limit)])]
    except Exception:
        return []
    out = []
    for i in ids:
        p = get_position(conn, i)
        p["marks"] = [dict(r) for r in conn.execute("SELECT date, spot, liq_value, unrealized, complete, exit_signal "
                                                    "FROM paper_option_strategy_mark WHERE position_id=? ORDER BY date "
                                                    "DESC LIMIT 30", (i,))]
        out.append(p)
    return out


def strategy_book(conn, strategy_id: str) -> dict | None:
    """One strategy's OPTIONS book for the performance page; None when it never had a position."""
    try:
        r = conn.execute("SELECT COUNT(*), COALESCE(SUM(realized_pnl),0), SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 "
                         "END), COALESCE(SUM(CASE WHEN status='OPEN' THEN COALESCE(unrealized,0) ELSE 0 END),0), "
                         "COALESCE(SUM(fees),0), COALESCE(SUM(margin_blocked),0) FROM paper_option_strategy_position "
                         "WHERE strategy_id=?", (strategy_id,)).fetchone()
    except Exception:
        return None
    if not r or not r[0]:
        return None
    fills = conn.execute("SELECT COUNT(*) FROM paper_option_strategy_trade WHERE strategy_id=?",
                         (strategy_id,)).fetchone()[0]
    return {"realized": round(float(r[1]), 2), "unrealized": round(float(r[3]), 2), "open_positions": int(r[2] or 0),
            "positions": int(r[0]), "fills": int(fills), "fees": round(float(r[4]), 2),
            "margin_blocked": round(float(r[5]), 2), "cost_basis": None}


def _trade(conn, pos, leg, side, qty, price, fees, source, reason, order_id, now):
    conn.execute("INSERT INTO paper_option_strategy_trade (trade_id,position_id,strategy_id,order_id,leg_no,underlying,"
                 "expiry,strike,option_type,side,qty,price,fees,price_source,reason,at) VALUES "
                 "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("OST" + uuid.uuid4().hex[:14].upper(), pos["position_id"], pos["strategy_id"], order_id,
                  leg["leg_no"], pos["underlying"], str(leg["expiry"])[:10], float(leg["strike"]), leg["option_type"],
                  side, int(qty), float(price), float(fees), source, reason, now))


def place_multileg(conn, order: dict, plan: dict) -> dict:
    """
    Open one strategy position from an OMS OPT order: every leg priced by leg_quote (the fill rule) from
    the latest stored chain, the structure's margin and cash checked, then ALL legs written in one
    transaction -- or nothing (status REJECTED with the reason). order: {order_id, strategy_id,
    strategy_version, quantity = structure lots}; plan: oms_order.legs_json (execution/option_intents.py).
    """
    s = settings()
    if not s["enabled"]:
        return _rej("the paper options book is off (config.json options.enabled)")
    ensure_tables(conn)
    sid, sym, u = order.get("strategy_id") or "", plan["symbol"], plan["underlying"]
    lots = int(order.get("quantity") or 0)
    if lots < 1:
        return _rej("quantity (structure lots) must be at least 1")
    if open_positions(conn, sid, sym):
        return _rej(f"{sid} already holds an open option position on {sym}")
    ch = chain_for_fill(conn, u, s)
    age = chain_age(ch)
    if age is None:
        return _rej(f"no option chain stored for {u}")
    if age > int(s["max_chain_age_sessions"]):
        return _rej(f"the {u} option chain is {age} sessions old ({ch['session']}): no fill on stale prices")
    today = date.today()
    priced, missing = [], []
    for leg in plan["legs"]:
        label = f"{leg['side']} {u} {leg['expiry']} {float(leg['strike']):g} {leg['option_type']}"
        exp = _d(leg["expiry"])
        if (exp - today).days < int(s["no_new_days_to_expiry"]):
            return _rej(f"{label}: no new positions within {s['no_new_days_to_expiry']} day(s) of expiry")
        q = leg_quote(conn, u, leg["expiry"], leg["strike"], leg["option_type"], leg["side"], s, chain=ch)
        if not q:
            missing.append(f"{label} (no price)")
            continue
        if not q["traded"]:
            missing.append(f"{label} (no volume or OI)")
            continue
        priced.append({**leg, "price": q["price"], "iv": q["iv"], "source": q["source"], "lot_size": q["lot_size"]})
    if missing:
        return _rej("all-or-nothing: " + "; ".join(missing))
    sizes = {p["lot_size"] for p in priced}
    if len(sizes) != 1 or not next(iter(sizes)):
        return _rej(f"lot size unknown or inconsistent across legs ({sorted(map(str, sizes))})")
    L = int(next(iter(sizes)))
    spot = float(ch["spot"] or order.get("reference_price") or 0)
    if spot <= 0:
        return _rej(f"no underlying price for {u}")
    m = structure_metrics(priced, spot, L, lots, cover_shares=cover_available(conn, sym),
                          uses_shares=bool(plan.get("uses_shares")),
                          allow_naked=bool(plan.get("allow_naked_short_calls")), s=s)
    if m["refused"]:
        return _rej(m["refused"])
    cash = _cash(conn)
    if cash < m["cash_needed"]:
        return _rej(f"insufficient paper cash: needs {m['cash_needed']:,.0f} (debit {m['debit']:,.0f} + margin "
                    f"{m['margin']:,.0f} + fees {m['fees']:,.0f}), has {cash:,.0f}")
    pid = "OP" + uuid.uuid4().hex[:14].upper()
    now = datetime.now()
    pos = {"position_id": pid, "strategy_id": sid, "underlying": u}
    conn.execute("INSERT INTO paper_option_strategy_position (position_id,strategy_id,strategy_version,symbol,underlying,"
                 "template,expiry,lots,lot_size,status,spot_at_open,net_premium,max_profit,max_loss,risk_amount,"
                 "profit_base,loss_base,margin_blocked,covered_shares,fees,realized_pnl,unrealized,liq_value,"
                 "exits_json,metrics_json,open_order_id,opened_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                 "?,?,?,?,?)",
                 (pid, sid, order.get("strategy_version"), sym, u, plan.get("template"),
                  str(min(_d(p["expiry"]) for p in priced)), lots, L, "OPEN", spot, m["net_premium"], m["max_profit"],
                  m["max_loss"], m["risk_amount"], m["profit_base"], m["loss_base"], m["margin"], m["covered_shares"],
                  m["fees"], -m["fees"], 0.0, -m["net_premium"],
                  json.dumps(plan.get("exits") or {}, sort_keys=True),
                  json.dumps({k: m[k] for k in ("breakevens", "pop_pct", "greeks", "reward_to_risk", "profit_base_kind",
                                                 "loss_base_kind", "margin_rule", "debit", "credit", "naked_call_lots",
                                                 "naked_put_lots")}, default=str),
                  order.get("order_id"), now))
    for i, p in enumerate(priced, start=1):
        qty = lots * L
        conn.execute("INSERT INTO paper_option_strategy_leg (position_id,leg_no,option_type,strike,expiry,side,lots,qty,"
                     "role,entry_price,entry_source,mark) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (pid, i, p["option_type"], float(p["strike"]), str(p["expiry"])[:10], p["side"], lots, qty,
                      p.get("role"), p["price"], p["source"], p["price"]))
        _trade(conn, pos, {**p, "leg_no": i}, p["side"], qty, p["price"], _fees(p["side"], qty * p["price"], s),
               p["source"], "OPEN", order.get("order_id"), now)
    conn.execute("UPDATE paper_account SET balance=balance+? WHERE id=1", (m["net_premium"] - m["fees"] - m["margin"],))
    conn.commit()
    kind = "credit" if m["net_premium"] > 0 else "debit"
    return {"status": "FILLED", "filled_qty": lots, "price": round(abs(m["net_premium"]) / (L * lots), 2),
            "fees": m["fees"], "position_id": pid, "metrics": m,
            "message": f"{plan.get('template')} {u} {lots} lot(s) x {L}: net {kind} {abs(m['net_premium']):,.2f}, "
                       f"margin {m['margin']:,.0f}, max loss {m['risk_amount']:,.0f} ({len(priced)} legs, "
                       f"{ch['source']})"}


def _close(conn, pos, exits, reason, status, order_id, s):
    """Close every leg of `pos` at exits [(price, source)] in one transaction; returns the realised P&L."""
    now = datetime.now()
    flow = pnl = fees = 0.0
    for leg, (px, src) in zip(pos["legs"], exits):
        side = CLOSING[leg["side"]]
        qty = int(leg["qty"])
        f = _fees(side, qty * px, s) if px > 0 else 0.0
        flow += SIGN[leg["side"]] * qty * px
        pnl += SIGN[leg["side"]] * qty * (px - float(leg["entry_price"]))
        fees += f
        conn.execute("UPDATE paper_option_strategy_leg SET exit_price=?, exit_source=?, mark=? WHERE position_id=? AND "
                     "leg_no=?", (px, src, px, pos["position_id"], leg["leg_no"]))
        _trade(conn, pos, leg, side, qty, px, f, src, reason, order_id, now)
    conn.execute("UPDATE paper_account SET balance=balance+? WHERE id=1",
                 (flow - fees + float(pos["margin_blocked"] or 0),))
    conn.execute("UPDATE paper_option_strategy_position SET status=?, realized_pnl=realized_pnl+?, fees=fees+?, "
                 "margin_blocked=0, unrealized=0, liq_value=0, exit_reason=?, close_order_id=?, closed_at=? "
                 "WHERE position_id=?", (status, round(pnl - fees, 2), round(fees, 2), reason, order_id, now,
                                         pos["position_id"]))
    conn.commit()
    return round(pnl - fees, 2), round(fees, 2), flow


def close_multileg(conn, position_id: str, order_id: str | None = None, reason: str = "ORDER") -> dict:
    """Close an open strategy position: every leg bought / sold back at the fill rule, all-or-nothing.
    An expired position is not closed here: settle_expired() settles it at intrinsic value."""
    s = settings()
    ensure_tables(conn)
    pos = get_position(conn, position_id)
    if not pos or pos["status"] != "OPEN":
        return _rej(f"no open option position {position_id}")
    if _d(pos["expiry"]) < date.today():
        return _rej(f"{position_id} expired on {pos['expiry']}: it is settled at intrinsic value (settle_expired)")
    ch = chain_for_fill(conn, pos["underlying"], s)
    age = chain_age(ch)
    if age is None or age > int(s["max_chain_age_sessions"]):
        return _rej(f"the {pos['underlying']} option chain is missing or stale ({ch.get('session')}): no close on "
                    f"stale prices")
    exits, missing = [], []
    for leg in pos["legs"]:
        q = leg_quote(conn, pos["underlying"], leg["expiry"], leg["strike"], leg["option_type"], CLOSING[leg["side"]],
                      s, chain=ch)
        if not q:
            missing.append(f"{leg['option_type']} {float(leg['strike']):g} {leg['expiry']}")
        else:
            exits.append((q["price"], q["source"]))
    if missing:
        return _rej("all-or-nothing: no price to close " + "; ".join(missing))
    pnl, fees, flow = _close(conn, pos, exits, reason, "CLOSED", order_id, s)
    q = pos["lots"] * pos["lot_size"]
    return {"status": "FILLED", "filled_qty": pos["lots"], "price": round(abs(flow) / q, 2) if q else 0.0,
            "fees": fees, "position_id": position_id, "realized": pnl,
            "message": f"closed {pos['template']} {pos['underlying']} ({reason}): realised {pnl:,.2f}"}


def fill_strategy_order(conn, order: dict) -> dict:
    """The paper options adapter's entry point (execution/adapters.py OptionsPaperAdapter)."""
    try:
        plan = json.loads(order.get("legs_json") or "{}")
    except ValueError:
        plan = {}
    if not plan:
        return _rej("the order carries no option legs")
    if plan.get("action") == "OPTION_CLOSE":
        return close_multileg(conn, plan["position_id"], order.get("order_id"), plan.get("exit_reason") or "ORDER")
    if plan.get("action") == "OPTION_OPEN":
        return place_multileg(conn, order, plan)
    return _rej(f"unknown option order action {plan.get('action')!r}")


def settle_strategy_positions(conn, as_of=None, s=None) -> int:
    """Settle open strategy positions whose expiry is on or before `as_of`: each leg at its intrinsic value
    from the underlying's close ON the expiry day (an earlier close only once that day has passed with
    no close stored). Returns the number of positions settled."""
    s = s or settings()
    ensure_tables(conn)
    d = _d(as_of or date.today())
    n = 0
    for pos in open_positions(conn):
        exp = _d(pos["expiry"])
        if exp > d:
            continue
        spot = settlement_spot(conn, pos["underlying"], exp, exact=True)
        if spot is None and d > exp:
            spot = settlement_spot(conn, pos["underlying"], exp)
        if spot is None:
            continue
        exits = [(round(intrinsic(l["option_type"], l["strike"], spot), 2), f"intrinsic at underlying {spot:g}")
                 for l in pos["legs"]]
        _close(conn, pos, exits, "EXPIRY", "SETTLED", None, s)
        n += 1
    return n


def exit_signal(pos: dict, mark: dict, as_of=None) -> tuple | None:
    """(code, why) when one of the position's exit rules (exits_json) fires on this mark, else None.
        EXPIRY_WINDOW  days to expiry <= days_before_expiry
        PROFIT_TAKE    unrealised P&L >= profit_take_pct% of profit_base (max profit, or premium paid)
        STOP_LOSS      unrealised P&L <= -stop_loss_pct% of loss_base (max loss, or the credit received)
    A mark with a leg missing a price (incomplete) can only fire the expiry rule."""
    ex = pos.get("exits")
    if ex is None:
        ex = json.loads(pos.get("exits_json") or "{}")
    dte = (_d(pos["expiry"]) - _d(as_of or date.today())).days
    dbe = ex.get("days_before_expiry")
    if dbe is not None and dte <= int(dbe):
        return "EXPIRY_WINDOW", f"{dte} day(s) to the {pos['expiry']} expiry <= days_before_expiry {dbe}"
    if not mark.get("complete") or mark.get("unrealized") is None:
        return None
    u = float(mark["unrealized"])
    pt, pb = ex.get("profit_take_pct"), pos.get("profit_base")
    if pt and pb and u >= float(pb) * float(pt) / 100:
        return "PROFIT_TAKE", f"P&L {u:,.0f} >= {pt}% of {float(pb):,.0f} (profit base)"
    sl, lb = ex.get("stop_loss_pct"), pos.get("loss_base")
    if sl and lb and u <= -float(lb) * float(sl) / 100:
        return "STOP_LOSS", f"P&L {u:,.0f} <= -{sl}% of {float(lb):,.0f} (loss base)"
    return None


def mark_position(conn, pos: dict, as_of=None, chain=None) -> dict:
    """The position at the session's chain mid (no slippage): liquidation value, unrealised P&L,
    whether every leg had a price, and the exit rule this mark fires."""
    from data.derivatives_store import chain_on
    d = _d(as_of or date.today())
    ch = chain if chain is not None else chain_on(conn, pos["underlying"], d)
    value = unreal = 0.0
    complete, legs = True, []
    for leg in pos["legs"]:
        q = ch["quotes"].get((str(leg["expiry"])[:10], float(leg["strike"]), leg["option_type"]))
        if not q:
            complete = False
            legs.append({"leg_no": leg["leg_no"], "mark": None})
            continue
        sg, qty = SIGN[leg["side"]], int(leg["qty"])
        value += sg * qty * q["price"]
        unreal += sg * qty * (q["price"] - float(leg["entry_price"]))
        legs.append({"leg_no": leg["leg_no"], "mark": q["price"]})
    out = {"date": str(d), "spot": ch.get("spot"), "liq_value": round(value, 2) if complete else None,
           "unrealized": round(unreal, 2) if complete else None, "complete": complete, "legs": legs,
           "source": ch.get("source"), "session": ch.get("session")}
    sig = exit_signal(pos, out, d)
    out["exit_signal"], out["exit_why"] = (sig[0], sig[1]) if sig else (None, None)
    return out


def mark_strategy_positions(as_of=None, conn=None) -> dict:
    """Post-market: mark every open strategy position (one paper_option_strategy_mark row per day) and
    report the exit rules that fire -- the strategy's next decision run turns them into OPTION_CLOSE intents."""
    from db.schema import get_connection
    own = conn is None
    conn = conn or get_connection()
    try:
        ensure_tables(conn)
        d = _d(as_of or date.today())
        n, signals = 0, []
        for pos in open_positions(conn):
            mk = mark_position(conn, pos, d)
            conn.execute("INSERT OR REPLACE INTO paper_option_strategy_mark (position_id,date,strategy_id,spot,liq_value,"
                         "unrealized,complete,exit_signal,source,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (pos["position_id"], str(d), pos["strategy_id"], mk["spot"], mk["liq_value"], mk["unrealized"],
                          int(mk["complete"]), mk["exit_signal"], mk["source"], datetime.now()))
            if mk["complete"]:
                conn.execute("UPDATE paper_option_strategy_position SET unrealized=?, liq_value=?, last_mark_date=? "
                             "WHERE position_id=?", (mk["unrealized"], mk["liq_value"], str(d), pos["position_id"]))
                for lg in mk["legs"]:
                    conn.execute("UPDATE paper_option_strategy_leg SET mark=? WHERE position_id=? AND leg_no=?",
                                 (lg["mark"], pos["position_id"], lg["leg_no"]))
            if mk["exit_signal"]:
                signals.append({"position_id": pos["position_id"], "strategy_id": pos["strategy_id"],
                                "signal": mk["exit_signal"], "why": mk["exit_why"]})
            n += 1
        conn.commit()
        return {"status": "SUCCESS" if n else "SKIPPED", "rows": n, "exit_signals": signals,
                "reason": None if n else "no open strategy option positions"}
    finally:
        if own:
            conn.close()
