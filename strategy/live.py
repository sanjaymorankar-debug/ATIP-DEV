"""
Runs the aggressive strategy against a broker, in real time.

This is the join between three pieces that were deliberately built apart:

    strategy.aggressive   decides   (pure, no I/O)
    strategy.positions    remembers (persistent, idempotent)
    orders.broker         executes  (PAPER by default)

Nothing here decides anything. It reads a price, asks `aggressive` what that
means, tells `positions` what happened, and asks `broker` to do it — so a bug in
the wiring cannot silently change the strategy, and the strategy stays testable
without any of this.

The order of operations
-----------------------
Every action follows reserve -> place -> apply:

    1. `claim_event` reserves the transition. Two overlapping passes cannot both
       win the reservation, because it is an INSERT against a UNIQUE constraint.
    2. The order goes to the broker, tagged with the position and event.
    3. Only a real fill applies the state change, using the ACTUAL fill price
       and quantity — never the theoretical target.
    4. A rejected order releases the reservation so the next pass can retry.

Recording first and ordering second would leave a rejected order looking like a
completed sale. Ordering first and recording second would repeat the order after
a crash. The reservation is what removes both.

Where it will not act
---------------------
  * Stale quotes. A price older than MAX_QUOTE_AGE_SECONDS is not acted on; the
    documented failure mode of this system is a process that wakes after a sleep
    and fires on a price from hours ago.
  * A closed position, a position with nothing left, or a symbol with no quote.
  * LIVE, unless `broker_env` says LIVE. PAPER is the default everywhere.
"""

from __future__ import annotations

import logging
from datetime import datetime

from db.schema import get_connection
from strategy import aggressive as A
from strategy import positions as P

log = logging.getLogger("atip.strategy")

MAX_QUOTE_AGE_SECONDS = 180


# ═══════════════════════════════════════════════════════════════════════════
#  MOMENTUM FROM ATIP'S OWN INDICATORS
# ═══════════════════════════════════════════════════════════════════════════

def momentum_for(symbol: str, conn=None) -> A.Momentum:
    """
    Build the checkpoint's momentum reading from ATIP's existing scores.

    Reads only; no formula here. Missing values stay None rather than becoming
    zero — `Momentum.composite()` averages what is present and a zero would read
    as maximum weakness, which is the opposite of "unknown".
    """
    own = conn is None
    conn = conn or get_connection()
    try:
        s = conn.execute(
            "SELECT zpi, msi, mri, cri, mh_score FROM ai_scores WHERE symbol=? "
            "ORDER BY date DESC LIMIT 1", (symbol,)).fetchone()
        t = conn.execute(
            "SELECT rsi_14, macd_hist, volume_ratio FROM technical_indicators "
            "WHERE symbol=? ORDER BY date DESC LIMIT 1", (symbol,)).fetchone()
        s = dict(s) if s else {}
        t = dict(t) if t else {}
        return A.Momentum(
            zpi=s.get("zpi"), msi=s.get("msi"), mri=s.get("mri"),
            cri=s.get("cri"), mh_score=s.get("mh_score"),
            rsi_14=t.get("rsi_14"), macd_hist=t.get("macd_hist"),
            volume_ratio=t.get("volume_ratio"))
    finally:
        if own:
            conn.close()


def atr_for(symbol: str, conn=None) -> float | None:
    own = conn is None
    conn = conn or get_connection()
    try:
        r = conn.execute("SELECT atr_pct FROM technical_indicators WHERE symbol=? "
                         "ORDER BY date DESC LIMIT 1", (symbol,)).fetchone()
        return float(r[0]) if r and r[0] else None
    finally:
        if own:
            conn.close()


# ═══════════════════════════════════════════════════════════════════════════
#  EXECUTION
# ═══════════════════════════════════════════════════════════════════════════

def _sell(symbol: str, quantity: int, tag: str, reference_price: float = None) -> dict:
    """
    Send a SELL through the normal order path.

    Goes through orders.broker so it obeys the same environment routing, funds
    check, order log and confirmation rules as every other order in ATIP —
    rather than a private path that only the strategy uses and only the
    strategy's tests cover.
    """
    from orders.broker import place_sell_order
    return place_sell_order(symbol, quantity, confirm=True, tag=tag,
                            reference_price=reference_price)


def _filled(resp: dict) -> tuple[bool, float | None, int | None]:
    """(ok, fill_price, filled_qty) from a broker response."""
    if not resp or resp.get("status") not in ("PLACED", "success"):
        return False, None, None
    data = (resp.get("response") or {}).get("data") or resp.get("data") or {}
    price = data.get("averageTradedPrice")
    qty = data.get("filledQty")
    return True, (float(price) if price else None), (int(qty) if qty else None)


def _act(pos: dict, event_key: str, event_type: str, quantity: int,
         price_hint: float, apply_fn) -> dict:
    """
    reserve -> place -> apply, with release on failure.

    `apply_fn(fill_price, filled_qty)` records the state change and is called
    ONLY when the broker actually filled.
    """
    pid = pos["id"]
    if not P.claim_event(pid, event_key, event_type, price_hint, quantity):
        return {"acted": False, "reason": "already handled"}

    # Fill at the price the decision was made on, not a separately-fetched one.
    resp = _sell(pos["symbol"], quantity, tag=f"{pid[:8]}:{event_key}",
                 reference_price=price_hint)
    ok, fill, filled = _filled(resp)
    if not ok:
        # The order never happened, so the reservation must go back — otherwise
        # this position would be stuck, unable to ever retry the exit.
        P.release_event(pid, event_key)
        err = resp.get("error") or resp.get("response") or resp.get("status")
        log.warning(f"  {pos['symbol']}: {event_type} order failed, reservation released — {err}")
        return {"acted": False, "reason": "order failed", "response": resp}

    # Use what actually filled, not what was intended.
    return {"acted": True, "response": resp,
            **apply_fn(fill if fill is not None else price_hint, filled or quantity)}


# ═══════════════════════════════════════════════════════════════════════════
#  ENTRY
# ═══════════════════════════════════════════════════════════════════════════

def enter(symbol: str, quantity: int, cfg: dict = None, cost_pct: float = 0.0) -> dict:
    """
    Buy and start managing the position.

    The tracked entry price is the ACTUAL fill, not the last quote. Every level
    that follows — target, stop, trail — is measured from it, and measuring them
    from a price you did not get puts every one of them in the wrong place.
    """
    cfg = cfg or A.config()
    from orders.broker import place_buy_order
    symbol = symbol.upper()
    resp = place_buy_order(symbol, quantity, confirm=True, tag=f"ENTRY:{symbol}")
    ok, fill, filled = _filled(resp)
    if not ok or not fill:
        return {"opened": False, "reason": "entry order did not fill", "response": resp}

    pos = P.open_position(symbol, fill, filled or quantity, cfg=cfg,
                          atr_pct=atr_for(symbol), cost_pct=cost_pct)
    log.info(f"  ✓ {symbol}: entered {filled or quantity} @ {fill} "
             f"(T1 {A.target_price(fill, cfg['target_1_pct'])}, "
             f"T2 {A.target_price(fill, cfg['target_2_pct'])}, "
             f"trail {pos['trail_pct']}%)")
    return {"opened": True, "position": pos, "fill_price": fill}


# ═══════════════════════════════════════════════════════════════════════════
#  MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════

def manage_position(pos: dict, price: float, cfg: dict = None,
                    initial_stop_pct: float = None) -> dict:
    """
    One decision pass over one position at one price.

    Order matters and mirrors the backtest exactly, so live behaviour and
    measured behaviour cannot drift: stop before target, then the checkpoint,
    then the trail.
    """
    cfg = cfg or A.config()
    pid = pos["id"]
    sym = pos["symbol"]
    long_side = pos["side"] == "BUY"
    entry = pos["entry_price"]
    events = []

    if pos["status"] == A.ST_CLOSED or pos["remaining_qty"] <= 0:
        return {"symbol": sym, "events": []}

    gain_pct = (price - entry) / entry * 100 * (1 if long_side else -1)

    # ── before T1: the initial stop protects the whole position ────────────
    if pos["t1_state"] == "PENDING":
        stop_pct = initial_stop_pct if initial_stop_pct is not None else float(cfg["target_1_pct"])
        stop_price = A.target_price(entry, -stop_pct) if long_side else A.target_price(entry, stop_pct)
        hit_stop = price <= stop_price if long_side else price >= stop_price
        if hit_stop:
            r = _act(pos, f"CLOSE:{A.INITIAL_STOP_LOSS}", A.INITIAL_STOP_LOSS,
                     pos["remaining_qty"], price,
                     lambda f, q: P.close_position(pid, f, A.INITIAL_STOP_LOSS,
                                                   q, pre_claimed=True))
            if r.get("acted"):
                log.info(f"  ✗ {sym}: initial stop at {price} — exited {pos['remaining_qty']}")
                events.append({"event": A.INITIAL_STOP_LOSS, "price": price})
            return {"symbol": sym, "events": events}

        if gain_pct >= float(cfg["target_1_pct"]):
            qty = A.partial_exit_quantity(pos["initial_qty"], cfg)
            if qty <= 0:
                # Too small to split. Promote to runner without selling, so the
                # trail takes over rather than the position silently becoming a
                # plain fixed-target trade.
                P.record_partial_exit(pid, price, quantity=0, cfg=cfg)
                events.append({"event": "T1_SKIPPED_TOO_SMALL", "price": price})
            else:
                r = _act(pos, "T1", A.TARGET_1_PARTIAL_EXIT, qty, price,
                         lambda f, q: P.record_partial_exit(pid, f, q, cfg=cfg,
                                                            pre_claimed=True))
                if r.get("acted"):
                    log.info(f"  ✓ {sym}: +{gain_pct:.2f}% — booked {r.get('quantity', qty)} "
                             f"of {pos['initial_qty']}, runner {r.get('remaining_qty')} "
                             f"trailing at {r.get('trail_stop')}")
                    events.append({"event": A.TARGET_1_PARTIAL_EXIT, "price": price,
                                   "quantity": r.get("quantity")})
            pos = P.get_position(pid)

    # ── the runner ────────────────────────────────────────────────────────
    if pos["status"] != A.ST_RUNNER or pos["remaining_qty"] <= 0:
        P.mark_to_market(pid, price)
        return {"symbol": sym, "events": events}

    # Trail first: the same-bar convention as the backtest, resolving an
    # ambiguous moment against the position rather than in its favour.
    P.update_high_water(pid, price)
    pos = P.get_position(pid)
    stop = pos["trail_stop"]
    if stop is not None:
        hit = price <= stop if long_side else price >= stop
        if hit:
            r = _act(pos, f"CLOSE:{A.TRAILING_STOP}", A.TRAILING_STOP,
                     pos["remaining_qty"], price,
                     lambda f, q: P.close_position(pid, f, A.TRAILING_STOP, q,
                                                   pre_claimed=True))
            if r.get("acted"):
                log.info(f"  ✓ {sym}: trailing stop at {price} (from high {pos['high_water']}) "
                         f"— exited runner {pos['remaining_qty']}")
                events.append({"event": A.TRAILING_STOP, "price": price})
            return {"symbol": sym, "events": events}

    # ── the +6% checkpoint ────────────────────────────────────────────────
    if pos["t2_state"] == "PENDING" and gain_pct >= float(cfg["target_2_pct"]):
        res = P.record_checkpoint(pid, price, momentum_for(sym), atr_for(sym), cfg=cfg)
        if res.get("applied"):
            d = res["decision"]
            log.info(f"  • {sym}: +{gain_pct:.2f}% checkpoint — {d['verdict']}: {d['reason']}")
            events.append({"event": "CHECKPOINT", "verdict": d["verdict"], "price": price})
            if d["verdict"] == A.EXIT_RUNNER:
                pos = P.get_position(pid)
                r = _act(pos, f"CLOSE:{A.TARGET_2_MOMENTUM_EXIT}", A.TARGET_2_MOMENTUM_EXIT,
                         pos["remaining_qty"], price,
                         lambda f, q: P.close_position(pid, f, A.TARGET_2_MOMENTUM_EXIT,
                                                       q, pre_claimed=True))
                if r.get("acted"):
                    log.info(f"  ✓ {sym}: runner banked at {price} on weak momentum")
                    events.append({"event": A.TARGET_2_MOMENTUM_EXIT, "price": price})
                return {"symbol": sym, "events": events}

    P.mark_to_market(pid, price)
    return {"symbol": sym, "events": events}


def _quote_age_seconds(quotes) -> float | None:
    try:
        ts = quotes["timestamp"].max()
        return (datetime.now() - ts.to_pydatetime()).total_seconds()
    except Exception:
        return None


def run_once(cfg: dict = None, prices: dict = None,
             initial_stop_pct: float = None) -> dict:
    """
    One management pass over every open position.

    `prices` is for tests and replay; live, quotes come from Dhan.
    """
    cfg = cfg or A.config()
    open_pos = P.open_positions()
    if not open_pos:
        return {"positions": 0, "events": [], "status": "IDLE"}

    if prices is None:
        from data.dhan import fetch_live_quotes
        symbols = sorted({p["symbol"] for p in open_pos})
        try:
            q = fetch_live_quotes(symbols)
        except Exception as e:
            log.warning(f"  strategy: quote fetch failed, skipping this pass: {e}")
            return {"positions": len(open_pos), "events": [], "status": "NO_QUOTES"}
        if q is None or q.empty:
            return {"positions": len(open_pos), "events": [], "status": "NO_QUOTES"}
        age = _quote_age_seconds(q)
        if age is not None and age > MAX_QUOTE_AGE_SECONDS:
            # Acting on an old price after a sleep or outage is this system's
            # documented failure mode. Refuse and say so.
            log.warning(f"  strategy: quotes are {age:.0f}s old "
                        f"(limit {MAX_QUOTE_AGE_SECONDS}s) — skipping this pass")
            return {"positions": len(open_pos), "events": [], "status": "STALE_QUOTES",
                    "quote_age_s": round(age)}
        prices = {r["symbol"]: r["ltp"] for _, r in q.iterrows() if r.get("ltp")}

    events = []
    for pos in open_pos:
        price = prices.get(pos["symbol"])
        if not price:
            continue
        r = manage_position(pos, float(price), cfg, initial_stop_pct)
        events.extend(r["events"])
    return {"positions": len(open_pos), "events": events, "status": "SUCCESS"}


def status() -> list[dict]:
    """Human-readable state of every open position."""
    out = []
    for p in P.open_positions():
        out.append({
            "symbol": p["symbol"], "status": p["status"],
            "entry": p["entry_price"], "qty": f"{p['remaining_qty']}/{p['initial_qty']}",
            "t1": p["t1_state"], "t2": p["t2_state"] + (f" ({p['t2_verdict']})"
                                                        if p["t2_verdict"] else ""),
            "high_water": p["high_water"], "trail_stop": p["trail_stop"],
            "realized": p["realized_pnl"], "unrealized": p["unrealized_pnl"],
            "total_pnl": P.total_pnl(p), "mode": p["mode"],
        })
    return out


def _cli():
    import argparse
    import json

    p = argparse.ArgumentParser(
        prog="python -m strategy.live",
        description="Run the aggressive strategy against the configured broker "
                    "(PAPER by default — fills are simulated at live prices).")
    p.add_argument("--enter", metavar="SYMBOL", help="buy and start managing")
    p.add_argument("--qty", type=int, default=1)
    p.add_argument("--manage", action="store_true", help="one management pass")
    p.add_argument("--status", action="store_true")
    p.add_argument("--stop-pct", type=float, default=None,
                   help="initial stop distance; defaults to target_1_pct")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from orders.environment import describe
    print(f"  environment: {describe()}")

    if args.enter:
        r = enter(args.enter, args.qty)
        print(json.dumps(r.get("position") or r, indent=2, default=str))
    if args.manage:
        print(json.dumps(run_once(initial_stop_pct=args.stop_pct), indent=2, default=str))
    if args.status or not (args.enter or args.manage):
        rows = status()
        if not rows:
            print("  no open positions")
        for r in rows:
            print(f"  {r['symbol']:<12} {r['status']:<7} qty {r['qty']:<8} "
                  f"entry {r['entry']:<10} T1 {r['t1']:<8} T2 {r['t2']:<22} "
                  f"trail {r['trail_stop']}  P&L {r['total_pnl']}")


if __name__ == "__main__":
    _cli()
