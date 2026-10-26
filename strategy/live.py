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
  * Unattended (the scheduled pass), a position whose recorded mode is not where
    orders now go: one opened in PAPER is never sold through a LIVE broker, nor the
    reverse. It is skipped and reported (status MODE_MISMATCH) for a person to settle.

When it runs
------------
`schedule_jobs` (called by pipeline/scheduler.py) runs a pass every
`manage_interval_minutes` (default 2) from 09:15 to 15:30 on trading days, and only
while a position is open; `python -m strategy.live --manage` runs one by hand.
"""

from __future__ import annotations

import logging
from datetime import datetime, time

from db.schema import get_connection
from strategy import aggressive as A
from strategy import positions as P

log = logging.getLogger("atip.strategy")

MAX_QUOTE_AGE_SECONDS = 180
MARKET_OPEN, MARKET_CLOSE = time(9, 15), time(15, 30)


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

def _last_price(symbol: str) -> float | None:
    """A fresh LTP to size an entry on, or None: no quote, or one older than MAX_QUOTE_AGE_SECONDS."""
    from data.dhan import fetch_live_quotes
    try:
        q = fetch_live_quotes([symbol])
    except Exception as e:
        log.warning(f"  {symbol}: quote fetch failed, cannot size the entry: {e}")
        return None
    if q is None or q.empty:
        return None
    age = _quote_age_seconds(q)
    if age is not None and age > MAX_QUOTE_AGE_SECONDS:
        log.warning(f"  {symbol}: quote is {age:.0f}s old (limit {MAX_QUOTE_AGE_SECONDS}s) — not sizing on it")
        return None
    prices = {r["symbol"]: r["ltp"] for _, r in q.iterrows() if r.get("ltp")}
    px = prices.get(symbol)
    return float(px) if px else None


def size_entry(symbol: str, stop_pct: float, cfg: dict = None, price: float = None) -> dict:
    """
    Shares to buy so that the initial stop loses risk_per_trade_pct of capital: orders.risk's
    size_for_account (config.json position_sizing; capital defaults to the account's equity),
    on a fresh quote. {"quantity": 0, "reason": ...} when it cannot be sized.
    """
    price = price if price is not None else _last_price(symbol)
    if not price:
        return {"quantity": 0, "reason": f"no fresh quote for {symbol} to size on"}
    from orders.risk import size_for_account
    conn = get_connection()
    try:
        out = size_for_account(conn, price, stop_pct=stop_pct)
    finally:
        conn.close()
    out["price"] = price
    return out


def enter(symbol: str, quantity: int = None, cfg: dict = None, cost_pct: float = 0.0,
          stop_pct: float = None) -> dict:
    """
    Buy and start managing the position.

    The tracked entry price is the ACTUAL fill, not the last quote. Every level
    that follows — target, stop, trail — is measured from it, and measuring them
    from a price you did not get puts every one of them in the wrong place.

    quantity: given, it is used as is. None with risk_sizing on: sized so the
    initial stop loses risk_per_trade_pct of capital (size_entry). None with it
    off: one share, as before.

    The initial stop is decided here and stored on the position when anything
    chose it at entry: `stop_pct` (--stop-pct), else the ATR stop
    (stop_atr_mult), else, for a risk-sized entry, target_1_pct, because the
    size was worked out on it and a later change of target must not change the
    risk. Otherwise the position follows target_1_pct as it always has.
    """
    cfg = cfg or A.config()
    if not cfg.get("aggressive_enabled"):
        # W39b (EX-17): the flag gates NEW aggressive entries. Positions already open keep being
        # managed (run_once / manage_position) whatever the flag says -- switching it off must
        # never leave a position without its stop, target and trail.
        return {"opened": False, "reason": "aggressive_enabled is false: no new entries "
                                           "(open positions are still managed)"}
    from orders.broker import place_buy_order
    symbol = symbol.upper()
    atr = atr_for(symbol)
    stop = float(stop_pct) if stop_pct is not None else A.initial_stop_pct(atr, cfg)
    sizing = None
    if quantity is None and cfg.get("risk_sizing"):
        if stop is None:
            stop = float(cfg["target_1_pct"])
        sizing = size_entry(symbol, stop, cfg)
        quantity = sizing["quantity"]
        if quantity < 1:
            log.warning(f"  {symbol}: no entry — risk sizing gave no position: {sizing['reason']}")
            return {"opened": False, "reason": f"risk sizing gave no position: {sizing['reason']}",
                    "sizing": sizing}
        log.info(f"  {symbol}: risk-sized {quantity} sh at ~{sizing['price']} with a {stop}% stop — "
                 f"risking Rs.{sizing['risk_amount']:,.0f} of {sizing['capital']:,.0f} "
                 f"({sizing['capital_source']}), capped by {sizing['capped_by']}")
    elif quantity is None:
        quantity = 1

    resp = place_buy_order(symbol, quantity, confirm=True, tag=f"ENTRY:{symbol}")
    ok, fill, filled = _filled(resp)
    if not ok or not fill:
        return {"opened": False, "reason": "entry order did not fill", "response": resp,
                "sizing": sizing}

    pos = P.open_position(symbol, fill, filled or quantity, cfg=cfg,
                          atr_pct=atr, cost_pct=cost_pct, stop_pct=stop)
    log.info(f"  ✓ {symbol}: entered {filled or quantity} @ {fill} "
             f"(T1 {A.target_price(fill, cfg['target_1_pct'])}, "
             f"T2 {A.target_price(fill, cfg['target_2_pct'])}, "
             f"trail {pos['trail_pct']}%"
             + (f", stop {pos['initial_stop_pct']}%" if pos.get("initial_stop_pct") else "")
             + ")")
    return {"opened": True, "position": pos, "fill_price": fill, "sizing": sizing}


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

    The initial stop is, in order: `initial_stop_pct` when given (--stop-pct),
    else the stop stored on the position at entry (see enter), else
    target_1_pct.
    """
    cfg = cfg or A.config()
    pid = pos["id"]
    sym = pos["symbol"]
    long_side = pos["side"] == "BUY"
    entry = pos["entry_price"]
    events = []

    if pos["status"] == A.ST_CLOSED or pos["remaining_qty"] <= 0:
        return {"symbol": sym, "events": []}

    # ── W39b (EX-17): crash-risk exit, before anything else, whatever the stage ──
    cri_t = cfg.get("cri_exit_threshold")
    if cri_t is not None:
        cri = momentum_for(sym).cri
        if cri is not None and cri >= float(cri_t):
            r = _act(pos, f"CLOSE:{A.MARKET_RISK_EXIT}", A.MARKET_RISK_EXIT, pos["remaining_qty"], price,
                     lambda f, q: P.close_position(pid, f, A.MARKET_RISK_EXIT, q, pre_claimed=True))
            if r.get("acted"):
                log.info(f"  ✗ {sym}: CRI {cri:.0f} >= {float(cri_t):g} -- exited {pos['remaining_qty']}")
                events.append({"event": A.MARKET_RISK_EXIT, "price": price, "cri": cri})
            return {"symbol": sym, "events": events}

    gain_pct = (price - entry) / entry * 100 * (1 if long_side else -1)

    # ── before T1: the initial stop protects the whole position ────────────
    if pos["t1_state"] == "PENDING":
        stop_pct = initial_stop_pct
        if stop_pct is None:
            stop_pct = pos.get("initial_stop_pct")
        if stop_pct is None:
            stop_pct = float(cfg["target_1_pct"])
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
             initial_stop_pct: float = None, only_mode: str = None) -> dict:
    """
    One management pass over every open position.

    `prices` is for tests and replay; live, quotes come from Dhan.
    `only_mode` ("PAPER" / "LIVE") manages only the positions opened in that mode
    and reports the rest as MODE_MISMATCH -- the scheduled pass passes where orders
    go now, so nobody's absence lets a PAPER position be sold for real.
    """
    cfg = cfg or A.config()
    open_pos = P.open_positions()
    mismatched = []
    if only_mode:
        mismatched = [{"symbol": p["symbol"], "mode": p.get("mode") or "PAPER", "id": p["id"]}
                      for p in open_pos if (p.get("mode") or "PAPER") != only_mode]
        open_pos = [p for p in open_pos if (p.get("mode") or "PAPER") == only_mode]
    if mismatched:
        names = ", ".join(f"{m['symbol']} ({m['mode']})" for m in mismatched)
        log.warning(f"  strategy: NOT managing {names}: orders now go to {only_mode}. "
                    f"Close or re-open them by hand, or set broker_env back to their mode.")
    if not open_pos:
        return {"positions": 0, "events": [], "status": "MODE_MISMATCH" if mismatched else "IDLE",
                "aggressive_enabled": bool(cfg.get("aggressive_enabled")),
                **_mismatch_report(mismatched, only_mode)}

    if prices is None:
        from data.dhan import fetch_live_quotes
        symbols = sorted({p["symbol"] for p in open_pos})
        try:
            q = fetch_live_quotes(symbols)
        except Exception as e:
            log.warning(f"  strategy: quote fetch failed, skipping this pass: {e}")
            return {"positions": len(open_pos), "events": [], "status": "NO_QUOTES",
                    **_mismatch_report(mismatched, only_mode)}
        if q is None or q.empty:
            return {"positions": len(open_pos), "events": [], "status": "NO_QUOTES",
                    **_mismatch_report(mismatched, only_mode)}
        age = _quote_age_seconds(q)
        if age is not None and age > MAX_QUOTE_AGE_SECONDS:
            # Acting on an old price after a sleep or outage is this system's
            # documented failure mode. Refuse and say so.
            log.warning(f"  strategy: quotes are {age:.0f}s old "
                        f"(limit {MAX_QUOTE_AGE_SECONDS}s) — skipping this pass")
            return {"positions": len(open_pos), "events": [], "status": "STALE_QUOTES",
                    "quote_age_s": round(age), **_mismatch_report(mismatched, only_mode)}
        prices = {r["symbol"]: r["ltp"] for _, r in q.iterrows() if r.get("ltp")}

    events = []
    for pos in open_pos:
        price = prices.get(pos["symbol"])
        if not price:
            continue
        r = manage_position(pos, float(price), cfg, initial_stop_pct)
        events.extend(r["events"])
    return {"positions": len(open_pos), "events": events,
            "status": "MODE_MISMATCH" if mismatched else "SUCCESS",
            **_mismatch_report(mismatched, only_mode)}


def _mismatch_report(mismatched: list, only_mode: str | None) -> dict:
    """The skipped positions, and an `error` line run_job writes to pipeline_log."""
    if not mismatched:
        return {}
    return {"mode_mismatch": mismatched,
            "error": f"{len(mismatched)} open position(s) not managed: opened in "
                     f"{'/'.join(sorted({m['mode'] for m in mismatched}))}, orders now go to {only_mode}"}


# ═══════════════════════════════════════════════════════════════════════════
#  SCHEDULED MANAGEMENT  (pipeline/scheduler.py calls schedule_jobs)
# ═══════════════════════════════════════════════════════════════════════════

def in_market_hours(now: datetime = None) -> bool:
    """09:15-15:30 on an NSE trading day (utils/trading_calendar.py)."""
    from utils.trading_calendar import is_trading_day
    now = now or datetime.now()
    return is_trading_day(now.date()) and MARKET_OPEN <= now.time() <= MARKET_CLOSE


def orders_mode() -> str:
    """Where an exit placed now would go, in a position's terms: LIVE for the real account
    (orders.environment), PAPER otherwise -- PAPER and the Dhan SANDBOX hold no real shares."""
    from orders.environment import is_live as env_is_live
    return "LIVE" if env_is_live() else "PAPER"


def scheduled_pass(run_job, now: datetime = None):
    """
    The scheduler's tick. Outside market hours, or with nothing open, it does nothing
    at all -- no Dhan call and no pipeline_log row every few minutes for no reason.
    Otherwise one run_once pass through run_job (job lock, pipeline_log, metrics,
    failure alert), for the positions opened where orders go now.

    A wake-up after a sleep is safe: `schedule` runs a missed interval job once,
    not once per missed slot, and run_once refuses quotes older than
    MAX_QUOTE_AGE_SECONDS. Positions are managed whatever aggressive_enabled says;
    that flag gates new entries only (EX-17). The initial stop is the one stored at entry
    (initial_stop_pct), else target_1_pct below entry: the same as `--manage` without `--stop-pct`.
    """
    if not in_market_hours(now):
        return None
    if not P.open_positions():
        return None
    return run_job("aggressive_manage", run_once, only_mode=orders_mode())


def schedule_jobs(schedule, run_job, cfg: dict = None) -> list:
    """Register the market-hours management pass; returns what was scheduled, for the log."""
    every = (cfg or A.config()).get("manage_interval_minutes")
    if not every:
        return [("aggressive_manage not scheduled (manage_interval_minutes is null): "
                 "exits run only from python -m strategy.live --manage")]
    schedule.every(int(every)).minutes.do(scheduled_pass, run_job)
    return [(f"aggressive_manage every {int(every)} min, {MARKET_OPEN:%H:%M}-{MARKET_CLOSE:%H:%M} "
             "on trading days, while a position is open")]


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
            "stop_pct": p.get("initial_stop_pct"),
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
    p.add_argument("--qty", type=int, default=None,
                   help="shares to buy; omitted: risk-sized when risk_sizing is on, else 1")
    p.add_argument("--manage", action="store_true", help="one management pass")
    p.add_argument("--status", action="store_true")
    p.add_argument("--stop-pct", type=float, default=None,
                   help="initial stop distance. With --enter: the stop stored on the new "
                        "position (and sized on). With --manage: overrides every stored stop. "
                        "Otherwise the stop set at entry, else target_1_pct")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from orders.environment import describe
    print(f"  environment: {describe()}")

    if args.enter:
        r = enter(args.enter, args.qty, stop_pct=args.stop_pct)
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
                  f"trail {r['trail_stop']}  "
                  f"stop {str(r['stop_pct']) + '%' if r['stop_pct'] else 'target_1_pct'}  "
                  f"P&L {r['total_pnl']}")


if __name__ == "__main__":
    _cli()
