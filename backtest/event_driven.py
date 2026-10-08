"""
Event-driven backtester (W34: BT-17).

The portfolio engine (backtest/engine.py) is a session loop: signals at the close fill whole
at the next open. This one is an EVENT loop with an explicit queue, so it can model what that
loop cannot: intraday bars, latency, partial fills against bar volume, order types, order
size -> price impact (EX-12), and orders worked in slices (EX-11 TWAP).

    MarketEvent   one timestamp's bars (a daily session, or a 15-min bar when timeframe='15m')
    SignalEvent   from the strategy after the last bar of each session (same Strategy interface,
                  same point-in-time daily view as the engine -- strategies run unchanged)
    OrderEvent    sized like the engine (orders.risk.size_position, max_positions); BUY and ADD
                  orders may be split into `slices` TWAP children, one per bar; each order becomes
                  eligible `latency_bars` bars after it is created (default 1: never the signal bar)
    FillEvent     MARKET at the bar open; LIMIT when the bar trades through the limit (at the
                  better of open and limit); STOP when the bar touches the stop (at the worse of open
                  and stop). Fill quantity <= participation_cap x bar volume -- the rest stays working
                  until ttl_bars, then is cancelled (recorded as an event). Fill price = reference
                  moved by the square-root impact estimate for THAT fill's participation (point-in-time
                  ADV / sigma / spread from execution.impact), then charged by the cost model.

Protective exits: each position's stop / target (from the signal or default_stop_pct) is a resting
STOP / LIMIT order evaluated on every bar -- intraday bars decide which came first; on daily bars
a bar covering both counts as the stop (the engine's pessimistic rule). max_hold_sessions closes at
the next session's first bar.

Partial position changes (PF-06). ADD and REDUCE signals change a HELD position with the W2
engine's sizing and accounting (backtest/engine.py's module docstring), but through this engine's
order model: each is a working MARKET order -- eligible latency_bars after the signal, filled at
most participation_cap x bar volume per bar (the rest keeps working), priced by the impact model
for that fill's participation, charged by the cost model, cancelled after ttl_bars (an event).
Within a bar, working orders fill in the order they were created, after the protective exits.
A run with no ADD / REDUCE signal produces exactly the result it did before they existed.

  ADD     for a symbol not held -> ignored (event). Skipped while a SELL, an ADD, or the BUY that
          opened the position is still working. Sized at the decision close like the engine:
          quantity; else value // close; else like a BUY (orders.risk.size_position on equity, the
          signal's stop or default_stop_pct); then capped so the position (held qty x close + the
          ADD) stays within sizing.max_position_pct of equity -- a cap or a skip is an event.
          max_positions does not apply. Split into `slices` TWAP children like a BUY. Each fill:
          the BUY side of the impact model, the cash check of a BUY (what cash does not cover keeps
          working; the first shortfall of an order is an event "add cut to n of m shares: cash"),
          then the position grows: its cost (fills + costs) and reference cost accumulate, so the
          entry price is the quantity-weighted average; stop, target, entry date and the max-hold
          clock stay those of the first entry. Rows of a position that was added to carry
          "adds": n (its ADD fills; metrics "adds" counts them all).
  REDUCE  for a symbol not held -> ignored (event). Skipped while a SELL or another REDUCE for it
          is working. Sized at the decision against the quantity held then (the quantity the
          strategy saw): quantity, else floor(fraction x held) (at least 1), else half the held
          quantity (at least 1). At its first fill, a REDUCE of the held quantity or more closes
          the position (event "reduce q >= n held: position closed"). Each fill is a SELL of that
          many shares, never more than is held: a trade row with the position's pro-rata cost
          (average entry incl. entry costs, this engine's convention), exit_reason "REDUCE", and
          "partial": true while shares remain; the fill that empties the position writes an
          ordinary (not partial) row.
  EXITS   an exit wins over a change: a SELL signal or a max-hold exit cancels the symbol's working
          ADD / REDUCE orders (event "... dropped: exit queued"); an ADD / REDUCE whose position
          was closed (stop / target) or replaced before it fills is dropped (event "... dropped:
          position no longer held").
  METRICS as the engine: with partial rows, trade statistics fold a position's rows into one round
          trip (backtest.engine.fold_round_trips); a run with an ADD or a partial row also carries
          trade_rows, partial_exits and adds. Every leg is realised once, so the rows' net P&L sums
          to final equity - initial capital when the run ends flat.

Output has the engine's shape (trades, equity, metrics, drawdowns, events, bias_report), so it is
stored with backtest.store and shown wherever runs are listed (kind='event_driven').

    run(request, conn)            request = a normal backtest request (backtest.service.resolve_config)
                                  plus "event_driven": {timeframe, latency_bars, participation_cap,
                                  slices, ttl_bars, impact ("sqrt" | "none"), impact_y}
    run_and_store(request)        -> run_id
"""

from __future__ import annotations

import heapq
import itertools
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

from backtest import metrics as M

ED_DEFAULTS = {"timeframe": "1d", "latency_bars": 1, "participation_cap": 0.1, "slices": 1, "ttl_bars": 26,
               "impact": "sqrt", "impact_y": None}


@dataclass(order=True)
class _Ev:
    ts: datetime
    prio: int
    seq: int
    kind: str = field(compare=False)
    data: dict = field(compare=False, default_factory=dict)


@dataclass
class _Order:
    oid: int
    symbol: str
    side: str
    qty: int
    otype: str = "MARKET"           # MARKET | LIMIT | STOP
    price: float | None = None
    eligible_idx: int = 0
    expire_idx: int = 10 ** 9
    reason: str = ""
    filled: int = 0
    signal: object = None
    exit_kind: str | None = None    # STOP / TARGET for protective orders
    uid: int = 0                    # ADD / REDUCE: the position they were decided for
    group: int = 0                  # ADD / REDUCE: shared by an order's TWAP children
    done: bool = False              # ADD / REDUCE: dropped or cancelled -- leaves the working list


@dataclass
class _Pos:
    symbol: str
    qty: int = 0
    cost: float = 0.0               # Rs paid incl. entry costs
    ref_cost: float = 0.0           # qty x reference prices (for slippage reporting)
    entry_date: date | None = None
    entry_idx: int = 0
    entry_session: int = 0
    stop: float | None = None
    target: float | None = None
    max_hold: int | None = None
    reason: str = ""
    uid: int = 0                    # which position a trade row belongs to (round trips in metrics)
    adds: int = 0                   # ADD fills into it


class _Bars:
    """Timeline of (timestamp, {symbol: bar}) for the run window."""

    def __init__(self, conn, symbols, start, end, timeframe, history):
        self.points = []
        if timeframe == "1d":
            for d in history.sessions:
                if start <= d <= end:
                    row = {s: history.bar(s, d) for s in symbols}
                    self.points.append((datetime.combine(d, datetime.min.time()).replace(hour=15, minute=30),
                                        {s: b for s, b in row.items() if b}))
        else:
            mins = int(timeframe.rstrip("m"))
            by_ts = defaultdict(dict)
            qmarks = ",".join("?" * len(symbols))
            for s, ts, o, h, lo, c, v in conn.execute(
                    f"SELECT symbol, ts, open, high, low, close, volume FROM intraday_bars WHERE interval_min=? AND "
                    f"symbol IN ({qmarks}) AND ts>=? AND ts<=? ORDER BY ts", [mins, *symbols, f"{start} 00:00:00",
                                                                             f"{end} 23:59:59"]):
                if None in (o, h, lo, c):
                    continue
                from backtest.data import Bar
                t = datetime.fromisoformat(str(ts)[:19].replace(" ", "T"))
                by_ts[t][s] = Bar(t.date(), float(o), float(h), float(lo), float(c), float(v or 0))
            self.points = sorted(by_ts.items())


def _impact_cache(conn, y):
    from execution.impact import estimate, stock_inputs
    cache = {}

    def price(symbol, side, qty, ref, d):
        key = (symbol, d)
        if key not in cache:
            cache[key] = stock_inputs(conn, symbol, d)
        inp = cache[key]
        if not inp.get("ok"):
            return ref, 0.0
        est = estimate(conn, symbol, qty, side, ref, d, y=y, inputs=inp)
        bps = est.get("total_bps") or 0.0
        sign = 1 if side == "BUY" else -1
        return max(0.01, ref * (1 + sign * bps / 1e4)), bps
    return price


def run(request: dict, conn) -> dict:
    from backtest.costs import cost_model
    from backtest.data import PriceHistory, ScoresHistory, explicit_universe, tracked_universe
    from backtest.engine import BacktestError
    from backtest.service import resolve_config
    from backtest.strategies import make_strategy
    from backtest.strategy import PositionView, StrategyContext
    from orders.risk import size_position

    req = dict(request)
    ed = dict(ED_DEFAULTS)
    ed.update(req.pop("event_driven", None) or {})
    if ed["timeframe"] not in ("1d", "15m", "5m", "1m"):
        raise ValueError("event_driven.timeframe must be 1d, 15m, 5m or 1m")
    snap = resolve_config(req)
    strat = make_strategy(snap["strategy_id"], snap.get("params"), snap.get("strategy_version")
                          if snap.get("strategy_definition_hash") else None)
    start, end = date.fromisoformat(str(snap["start"])), date.fromisoformat(str(snap["end"]))
    uni = snap.get("universe", "tracked_current")
    universe = tracked_universe(conn) if uni == "tracked_current" else explicit_universe(
        uni, snap.get("universe_survivorship_bias"))
    if not universe.symbols:
        raise BacktestError("empty universe")
    history = PriceHistory.load(conn, universe.symbols, start, end,
                                warmup_days=max(60, int(math.ceil((strat.warmup_bars or 0) * 1.6)) + 10))
    scores = ScoresHistory(conn, start, end) if strat.uses_scores else None
    bars = _Bars(conn, universe.symbols, start, end, ed["timeframe"], history)
    if len(bars.points) < 2:
        raise BacktestError(f"fewer than two {ed['timeframe']} bars between {start} and {end}"
                            + (" (intraday_bars holds only recent sessions)" if ed["timeframe"] != "1d" else ""))
    costs = cost_model(snap["cost_model"], snap.get("cost_overrides") or None)
    sizing = snap["sizing"]
    impact = _impact_cache(conn, ed["impact_y"]) if ed["impact"] == "sqrt" else (lambda s, sd, q, ref, d: (ref, 0.0))
    cap = float(ed["participation_cap"])
    lat = max(0, int(ed["latency_bars"]))
    slices = max(1, int(ed["slices"]))
    ttl = max(1, int(ed["ttl_bars"]))

    cash = float(snap["initial_capital"])
    positions: dict = {}
    working: list = []
    trades, events, equity = [], [], []
    trade_legs = []                                    # (position uid, net, basis) per trade row, unrounded
    not_simulated = {}                                 # signal sides this engine cannot trade
    oid = itertools.count(1)
    uids = itertools.count(1)
    seq = itertools.count()
    q: list = []
    last_close = {}
    costs_paid = slip_paid = turnover = realized = 0.0
    session_idx = -1
    fills_n = partial_n = 0
    partial_rows = adds_n = 0                          # PF-06: partial-exit trade rows, ADD fills
    cash_noted = set()                                 # ADD order groups whose cash shortfall is an event
    closes_noted = set()                               # REDUCE orders found to cover the whole position

    # the event queue: market events per timestamp, end-of-session markers after each session's last bar
    days = [p[0].date() for p in bars.points]
    for i, (ts, row) in enumerate(bars.points):
        heapq.heappush(q, _Ev(ts, 0, next(seq), "MARKET", {"idx": i, "row": row}))
        if i == len(bars.points) - 1 or days[i + 1] != days[i]:
            heapq.heappush(q, _Ev(ts, 9, next(seq), "SESSION_END", {"idx": i, "day": days[i]}))

    def equity_now():
        return cash + sum(p.qty * last_close.get(s, p.cost / max(1, p.qty)) for s, p in positions.items())

    def drop_change(o: _Order, d, text):
        """An ADD / REDUCE that can no longer fill: it and its TWAP siblings leave the working list."""
        for w in working:
            if w.group == o.group and w.side == o.side:
                w.done = True
        o.done = True
        events.append({"date": d, "symbol": o.symbol, "event": text})

    def cancel_changes(symbol, d):
        """An exit wins over a change: cancel the symbol's working ADD / REDUCE orders."""
        for w in working:
            if w.symbol == symbol and w.side in ("ADD", "REDUCE") and not w.done:
                drop_change(w, d, f"{w.side.lower()} dropped: exit queued")

    def fill(o: _Order, bar, i, d):
        nonlocal cash, costs_paid, slip_paid, turnover, realized, fills_n, partial_n, partial_rows, adds_n
        side = "SELL" if o.side in ("SELL", "REDUCE") else "BUY"     # ADD buys, REDUCE sells
        if o.side == "SELL":                    # W39b fix: sell what is held, and nothing once it is gone --
            p0 = positions.get(o.symbol)        # a max-hold exit queued on the bar a stop fired used to be
            if not p0 or p0.qty <= 0:           # charged costs, turnover and a fill for no shares
                o.done = True
                return
            o.qty = min(o.qty, o.filled + p0.qty)
        # reference price for this order type on this bar
        if o.otype == "MARKET":
            ref = bar.open
        elif o.otype == "LIMIT":
            hit = bar.low <= o.price if side == "BUY" else bar.high >= o.price
            if not hit:
                return
            ref = min(bar.open, o.price) if side == "BUY" else max(bar.open, o.price)
        else:  # STOP
            hit = bar.high >= o.price if side == "BUY" else bar.low <= o.price
            if not hit:
                return
            ref = max(bar.open, o.price) if side == "BUY" else min(bar.open, o.price)
        held = None
        if o.side in ("ADD", "REDUCE"):         # a change of the position it was decided for, or nothing
            held = positions.get(o.symbol)
            if not held or held.uid != o.uid:
                drop_change(o, d, f"{o.side.lower()} dropped: position no longer held")
                return
            if o.side == "REDUCE" and not o.filled and o.qty >= held.qty and o.group not in closes_noted:
                closes_noted.add(o.group)       # at its first fill: once, even if that bar has no volume
                events.append({"date": d, "symbol": o.symbol,
                               "event": f"reduce {o.qty} >= {held.qty} held: position closed"})
                o.qty = held.qty
        want = o.qty - o.filled
        if o.side == "REDUCE":
            want = min(want, held.qty)          # never more than is held
        room = int(cap * bar.volume) if bar.volume else want
        qty = min(want, room)
        if side == "BUY":
            px0, _ = impact(o.symbol, "BUY", qty, ref, d)
            afford = int(cash // (px0 * 1.002)) if px0 > 0 else 0
            if o.side == "ADD" and afford < qty and o.group not in cash_noted:
                cash_noted.add(o.group)
                events.append({"date": d, "symbol": o.symbol,
                               "event": f"add cut to {max(0, afford)} of {qty} shares: cash"})
            qty = min(qty, afford)
        if qty < 1:
            if room < 1:
                events.append({"date": d, "symbol": o.symbol, "event": f"{o.side} no fill: bar volume {bar.volume}"})
            return
        px, bps = impact(o.symbol, side, qty, ref, d)
        value = qty * px
        ch = costs.total(side, value)
        costs_paid += ch
        slip_paid += abs(px - ref) * qty
        turnover += value
        o.filled += qty
        fills_n += 1
        if o.filled < o.qty:
            partial_n += 1
        if side == "BUY":
            cash -= value + ch
            if o.side == "ADD":                 # weighted-average entry; stop / target / clock unchanged
                held.qty += qty
                held.cost += value + ch
                held.ref_cost += qty * ref
                held.adds += 1
                adds_n += 1
                return
            p = positions.get(o.symbol) or _Pos(o.symbol, entry_date=d, entry_idx=i, entry_session=session_idx,
                                                reason=o.reason, uid=next(uids))
            p.qty += qty
            p.cost += value + ch
            p.ref_cost += qty * ref
            sig = o.signal
            if sig is not None:
                p.stop = sig.stop_price if sig.stop_price is not None else ref * (1 - sizing["default_stop_pct"] / 100)
                p.target, p.max_hold = sig.target_price, sig.max_hold_sessions
            positions[o.symbol] = p
        else:
            p = positions.get(o.symbol)
            if not p or p.qty <= 0:
                return
            qty = min(qty, p.qty)
            basis = p.cost * qty / p.qty
            proceeds = qty * px - ch
            cash += proceeds
            net = proceeds - basis
            realized += net
            row = {"symbol": o.symbol, "entry_date": p.entry_date, "entry_price": round(basis / qty, 4),
                   "entry_ref_price": round(p.ref_cost / p.qty, 4), "qty": qty, "exit_date": d,
                   "exit_price": round(px, 4), "exit_ref_price": round(ref, 4),
                   "exit_reason": o.exit_kind or o.reason or "SELL_SIGNAL", "gross_pnl": round(qty * px - basis, 2),
                   "costs": round(ch, 2), "net_pnl": round(net, 2),
                   "return_pct": round(net / basis * 100, 4) if basis else None,
                   "holding_sessions": session_idx - p.entry_session, "entry_reason": p.reason,
                   "impact_bps": round(bps, 2)}
            if p.adds:
                row["adds"] = p.adds
            if o.side == "REDUCE" and qty < p.qty:
                row["partial"] = True
                partial_rows += 1
            trades.append(row)
            trade_legs.append((p.uid, net, basis))
            p.cost -= basis
            p.ref_cost -= p.ref_cost * qty / p.qty
            p.qty -= qty
            if p.qty == 0:
                del positions[o.symbol]
                o.done = True                   # the position is gone: nothing left to sell or reduce

    while q:
        ev = heapq.heappop(q)
        i = ev.data["idx"]
        d = days[i]
        if ev.kind == "MARKET":
            row = ev.data["row"]
            if i == 0 or days[i - 1] != d:
                session_idx += 1
                for s, p in list(positions.items()):           # max hold: out at the session's first bar
                    if p.max_hold and session_idx - p.entry_session >= p.max_hold and not any(
                            w.symbol == s and w.side == "SELL" and not w.done and w.filled < w.qty
                            for w in working):                 # W39b fix: once, not again every session
                        cancel_changes(s, d)
                        working.append(_Order(next(oid), s, "SELL", p.qty, eligible_idx=i, reason="MAX_HOLD"))
            # protective exits first (they rest at the broker), then working orders
            for s, p in list(positions.items()):
                bar = row.get(s)
                if not bar:
                    continue
                hit_stop = p.stop is not None and bar.low <= p.stop
                hit_tgt = p.target is not None and bar.high >= p.target
                if hit_stop or hit_tgt:
                    kind = "STOP" if hit_stop else "TARGET"
                    o = _Order(next(oid), s, "SELL", p.qty, "STOP" if hit_stop else "LIMIT",
                               p.stop if hit_stop else p.target, eligible_idx=i, exit_kind=kind)
                    fill(o, bar, i, d)
            still = []
            for o in working:
                bar = row.get(o.symbol)
                if o.eligible_idx <= i and bar and not o.done:
                    fill(o, bar, i, d)
                if o.done or o.filled >= o.qty:
                    continue
                if i >= o.expire_idx:
                    events.append({"date": d, "symbol": o.symbol, "event": f"{o.side} {o.qty - o.filled} unfilled, "
                                                                           f"expired after {ttl} bars"})
                    continue
                still.append(o)
            working = still
            for s, b in row.items():
                last_close[s] = b.close
        else:  # SESSION_END: mark, then the strategy
            eq = equity_now()
            prev = equity[-1]["equity"] if equity else float(snap["initial_capital"])
            pv = eq - cash
            equity.append({"date": d, "cash": round(cash, 2), "positions_value": round(pv, 2), "equity": round(eq, 2),
                           "exposure_pct": round(pv / eq * 100, 4) if eq else 0.0, "n_positions": len(positions),
                           "realized_cum": round(realized, 2), "unrealized": round(sum(
                               p.qty * last_close.get(s, 0) - p.cost for s, p in positions.items()), 2),
                           "daily_return": round(eq / prev - 1, 8) if prev else None})
            if i == len(bars.points) - 1:
                break
            view = history.view(d)
            pviews = {s: PositionView(s, p.qty, p.cost / p.qty, p.entry_date, session_idx - p.entry_session)
                      for s, p in positions.items()}
            ctx = StrategyContext(as_of=d, data=view, universe=universe.symbols, positions=pviews, cash=cash, equity=eq,
                                  params=strat.params, scores=scores)
            signals = strat.on_bar(ctx) or []
            pending_buys = {o.symbol for o in working if o.side == "BUY"}
            pending_sells = {o.symbol for o in working if o.side == "SELL"}
            pending_changes = {(o.side, o.symbol) for o in working if o.side in ("ADD", "REDUCE") and not o.done}
            for sig in signals:
                if sig.symbol not in universe.symbols:
                    continue
                if sig.side not in ("BUY", "SELL", "ADD", "REDUCE"):
                    events.append({"date": d, "symbol": sig.symbol,
                                   "event": f"{sig.side} not simulated by the event-driven engine"})
                    not_simulated[sig.side] = not_simulated.get(sig.side, 0) + 1
                    continue
                if sig.side in ("ADD", "REDUCE"):           # PF-06: a change of a held position
                    p = positions.get(sig.symbol)
                    if not p:
                        events.append({"date": d, "symbol": sig.symbol,
                                       "event": f"{sig.side.lower()} ignored: {sig.symbol} is not held"})
                        continue
                    if sig.symbol in pending_sells or (sig.side, sig.symbol) in pending_changes or (
                            sig.side == "ADD" and sig.symbol in pending_buys):
                        continue                    # an exit, the same change, or the entry is still working
                    e0 = i + max(1, lat)
                    if sig.side == "REDUCE":        # sized against what is held at the decision
                        if sig.quantity is not None:
                            qr = int(sig.quantity)
                        elif sig.fraction is not None:
                            qr = max(1, int(p.qty * float(sig.fraction) + 1e-9))
                        else:
                            qr = max(1, p.qty // 2)
                        k0 = next(oid)
                        working.append(_Order(k0, sig.symbol, "REDUCE", qr, eligible_idx=e0, expire_idx=e0 + ttl,
                                              reason="REDUCE", signal=sig, uid=p.uid, group=k0))
                        pending_changes.add(("REDUCE", sig.symbol))
                        continue
                    bar = history.bar(sig.symbol, d)
                    if not bar:
                        events.append({"date": d, "symbol": sig.symbol, "event": "add skipped: no bar at signal"})
                        continue
                    if sig.quantity is not None:
                        total = int(sig.quantity)
                    elif sig.value is not None:
                        total = int(float(sig.value) // bar.close)
                    else:
                        stop = sig.stop_price if sig.stop_price is not None else \
                            bar.close * (1 - sizing["default_stop_pct"] / 100)
                        sz = size_position(bar.close, stop, capital=eq, risk_per_trade_pct=sizing["risk_per_trade_pct"],
                                           max_position_pct=sizing["max_position_pct"])
                        total = int(sz["quantity"])
                        if total < 1:
                            events.append({"date": d, "symbol": sig.symbol, "event": f"add skipped: {sz['reason']}"})
                            continue
                    room = int((eq * sizing["max_position_pct"] / 100 - p.qty * bar.close) // bar.close)
                    if total > room:
                        cap_note = f"max_position_pct ({sizing['max_position_pct']}% of equity)"
                        if room < 1:
                            events.append({"date": d, "symbol": sig.symbol,
                                           "event": f"add skipped: position at {cap_note}"})
                            continue
                        events.append({"date": d, "symbol": sig.symbol,
                                       "event": f"add capped to {room} of {total} shares: {cap_note}"})
                        total = room
                    if total < 1:
                        events.append({"date": d, "symbol": sig.symbol, "event": "add skipped: less than one share"})
                        continue
                    n = min(slices, total)
                    base = total // n
                    k0 = None
                    for k in range(n):              # TWAP children, as for a BUY
                        qk = base + (1 if k < total - base * n else 0)
                        ok = next(oid)
                        k0 = k0 or ok
                        working.append(_Order(ok, sig.symbol, "ADD", qk, eligible_idx=e0 + k, expire_idx=e0 + k + ttl,
                                              reason="ADD", signal=sig, uid=p.uid, group=k0))
                    pending_changes.add(("ADD", sig.symbol))
                    continue
                if sig.side == "SELL":
                    if sig.symbol in positions and sig.symbol not in pending_sells:
                        cancel_changes(sig.symbol, d)
                        working.append(_Order(next(oid), sig.symbol, "SELL", positions[sig.symbol].qty,
                                              eligible_idx=i + max(1, lat), expire_idx=i + max(1, lat) + ttl,
                                              reason="SELL_SIGNAL"))
                        pending_sells.add(sig.symbol)
                    continue
                if sig.symbol in positions or sig.symbol in pending_buys:
                    continue
                if len(positions) + len(pending_buys) >= int(sizing["max_positions"]):
                    events.append({"date": d, "symbol": sig.symbol, "event": "buy skipped: max_positions"})
                    continue
                bar = history.bar(sig.symbol, d)
                if not bar:
                    continue
                stop = sig.stop_price if sig.stop_price is not None else bar.close * (1 - sizing["default_stop_pct"] / 100)
                sz = size_position(bar.close, stop, capital=eq, risk_per_trade_pct=sizing["risk_per_trade_pct"],
                                   max_position_pct=sizing["max_position_pct"])
                total = int(sz["quantity"])
                if total < 1:
                    events.append({"date": d, "symbol": sig.symbol, "event": f"buy skipped: {sz['reason']}"})
                    continue
                n = min(slices, total)
                base = total // n
                for k in range(n):                      # TWAP children: one per bar from the first eligible bar
                    qk = base + (1 if k < total - base * n else 0)
                    e0 = i + max(1, lat) + k
                    working.append(_Order(next(oid), sig.symbol, "BUY", qk, eligible_idx=e0, expire_idx=e0 + ttl,
                                          reason=sig.reason or "BUY_SIGNAL", signal=sig))
                pending_buys.add(sig.symbol)

    # end of window: close out at the last close (charged), like the engine
    last_d = days[-1]
    if snap.get("close_out_at_end", True) and positions:
        for s, p in list(positions.items()):
            px = last_close.get(s, p.cost / p.qty)
            ch = costs.total("SELL", p.qty * px)
            net = p.qty * px - ch - p.cost
            cash += p.qty * px - ch
            realized += net
            costs_paid += ch
            row = {"symbol": s, "entry_date": p.entry_date, "entry_price": round(p.cost / p.qty, 4),
                   "entry_ref_price": round(p.ref_cost / p.qty, 4), "qty": p.qty, "exit_date": last_d,
                   "exit_price": round(px, 4), "exit_ref_price": round(px, 4), "exit_reason": "END_OF_WINDOW",
                   "gross_pnl": round(p.qty * px - p.cost, 2), "costs": round(ch, 2), "net_pnl": round(net, 2),
                   "return_pct": round(net / p.cost * 100, 4) if p.cost else None,
                   "holding_sessions": session_idx - p.entry_session, "entry_reason": p.reason}
            if p.adds:
                row["adds"] = p.adds
            trades.append(row)
            trade_legs.append((p.uid, net, p.cost))
        positions.clear()
        equity[-1].update({"cash": round(cash, 2), "positions_value": 0.0, "equity": round(cash, 2), "exposure_pct": 0.0,
                           "n_positions": 0, "realized_cum": round(realized, 2), "unrealized": 0.0})
    for o in working:
        if not o.done:
            events.append({"date": last_d, "symbol": o.symbol, "event": f"{o.side} {o.qty - o.filled} unfilled at end"})

    eqs = [p["equity"] for p in equity]
    dts = [p["date"] for p in equity]
    for p, (peak, dd) in zip(equity, M.drawdown_series(eqs)):
        p["peak_equity"], p["drawdown_pct"] = round(peak, 2), round(dd * 100, 4)
    rf = float(snap.get("risk_free_rate_pct") or 0) / 100
    pnls = [t["net_pnl"] for t in trades]
    rets = [t["return_pct"] for t in trades if t["return_pct"] is not None]
    if partial_rows:                                   # PF-06: statistics per closed position (round trip)
        from backtest.engine import fold_round_trips
        pnls, rets = fold_round_trips(trades, trade_legs, {p.uid for p in positions.values()})
    metrics = M.summarize(dts, eqs, pnls, rets, rf_annual=rf, exposure=[p["exposure_pct"] or 0 for p in equity])
    metrics.update({"costs_paid": round(costs_paid, 2), "slippage_paid": round(slip_paid, 2),
                    "turnover": round(turnover, 2), "fills": fills_n, "partial_fills": partial_n,
                    "events": len(events), "open_positions_at_end": len(positions)})
    if partial_rows or adds_n:
        metrics.update({"trade_rows": len(trades), "partial_exits": partial_rows, "adds": adds_n})
    bias = {
        "engine": "event_driven (BT-17)",
        "entry_timing": f"signals at the session close; orders eligible {max(1, lat)} bar(s) later; never on the "
                        f"signal bar",
        "fills": f"<= {cap:.0%} of each bar's volume; impact {ed['impact']}"
                 + (f" (Y={ed['impact_y']})" if ed["impact_y"] else " (current Y)") + "; costs " + snap["cost_model"],
        "timeframe": ed["timeframe"], "slices": slices, "ttl_bars": ttl,
        "point_in_time_data": True, "universe": universe.as_dict(),
        "warnings": (["intraday bars: only sessions stored in intraday_bars (DP-03 retention) are simulated"]
                     if ed["timeframe"] != "1d" else [])
                    + (["survivorship bias: universe is today's constituents"] if universe.survivorship_bias else [])
                    + ([f"signals not simulated by the event-driven engine: "
                        f"{', '.join(f'{k} x{n}' for k, n in sorted(not_simulated.items()))}"]
                       if not_simulated else []),
    }
    snap_out = dict(snap, event_driven=ed)
    return {"strategy": strat.describe(), "sessions": len(set(days)), "bars": len(bars.points), "trades": trades,
            "equity": equity, "metrics": metrics, "drawdowns": M.drawdown_episodes(dts, eqs), "events": events,
            "bias_report": bias, "data_fingerprint": history.fingerprint(), "snapshot": snap_out}


def run_and_store(request: dict) -> dict:
    from backtest import store
    from db.schema import get_connection
    conn = get_connection()
    try:
        res = run(request, conn)
        snap = res.pop("snapshot")
        snap["timeframe"] = snap["event_driven"]["timeframe"]
        rid = store.create_run(conn, snap, kind="event_driven")
        store.mark_running(conn, rid)
        store.save_result(conn, rid, res, summary={k: res["metrics"].get(k) for k in
                                                   ("total_return", "cagr", "sharpe", "max_drawdown",
                                                    "fills", "partial_fills", "slippage_paid", "costs_paid")})
        return {"run_id": rid, "metrics": res["metrics"], "trades": len(res["trades"]), "bars": res["bars"]}
    finally:
        conn.close()
