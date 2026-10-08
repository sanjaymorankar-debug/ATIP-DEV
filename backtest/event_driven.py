"""
Event-driven backtester (W34: BT-17).

The portfolio engine (backtest/engine.py) is a session loop: signals at the close fill whole
at the next open. This one is an EVENT loop with an explicit queue, so it can model what that
loop cannot: intraday bars, latency, partial fills against bar volume, order types, order
size -> price impact (EX-12), and orders worked in slices (EX-11 TWAP).

    MarketEvent   one timestamp's bars (a daily session, or a 15-min bar when timeframe='15m')
    SignalEvent   from the strategy after the last bar of each session (same Strategy interface,
                  same point-in-time daily view as the engine -- strategies run unchanged)
    OrderEvent    sized like the engine (orders.risk.size_position, max_positions); BUY orders
                  may be split into `slices` TWAP children, one per bar; each order becomes
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
    not_simulated = {}                                 # W39: ADD / REDUCE signals this engine does not trade
    oid = itertools.count(1)
    seq = itertools.count()
    q: list = []
    last_close = {}
    costs_paid = slip_paid = turnover = realized = 0.0
    session_idx = -1
    fills_n = partial_n = 0

    # the event queue: market events per timestamp, end-of-session markers after each session's last bar
    days = [p[0].date() for p in bars.points]
    for i, (ts, row) in enumerate(bars.points):
        heapq.heappush(q, _Ev(ts, 0, next(seq), "MARKET", {"idx": i, "row": row}))
        if i == len(bars.points) - 1 or days[i + 1] != days[i]:
            heapq.heappush(q, _Ev(ts, 9, next(seq), "SESSION_END", {"idx": i, "day": days[i]}))

    def equity_now():
        return cash + sum(p.qty * last_close.get(s, p.cost / max(1, p.qty)) for s, p in positions.items())

    def fill(o: _Order, bar, i, d):
        nonlocal cash, costs_paid, slip_paid, turnover, realized, fills_n, partial_n
        # reference price for this order type on this bar
        if o.otype == "MARKET":
            ref = bar.open
        elif o.otype == "LIMIT":
            hit = bar.low <= o.price if o.side == "BUY" else bar.high >= o.price
            if not hit:
                return
            ref = min(bar.open, o.price) if o.side == "BUY" else max(bar.open, o.price)
        else:  # STOP
            hit = bar.high >= o.price if o.side == "BUY" else bar.low <= o.price
            if not hit:
                return
            ref = max(bar.open, o.price) if o.side == "BUY" else min(bar.open, o.price)
        want = o.qty - o.filled
        room = int(cap * bar.volume) if bar.volume else want
        qty = min(want, room)
        if o.side == "BUY":
            px0, _ = impact(o.symbol, "BUY", qty, ref, d)
            afford = int(cash // (px0 * 1.002)) if px0 > 0 else 0
            qty = min(qty, afford)
        if qty < 1:
            if room < 1:
                events.append({"date": d, "symbol": o.symbol, "event": f"{o.side} no fill: bar volume {bar.volume}"})
            return
        px, bps = impact(o.symbol, o.side, qty, ref, d)
        value = qty * px
        ch = costs.total(o.side, value)
        costs_paid += ch
        slip_paid += abs(px - ref) * qty
        turnover += value
        o.filled += qty
        fills_n += 1
        if o.filled < o.qty:
            partial_n += 1
        if o.side == "BUY":
            cash -= value + ch
            p = positions.get(o.symbol) or _Pos(o.symbol, entry_date=d, entry_idx=i, entry_session=session_idx,
                                                reason=o.reason)
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
            trades.append({"symbol": o.symbol, "entry_date": p.entry_date, "entry_price": round(basis / qty, 4),
                           "entry_ref_price": round(p.ref_cost / p.qty, 4), "qty": qty, "exit_date": d,
                           "exit_price": round(px, 4), "exit_ref_price": round(ref, 4),
                           "exit_reason": o.exit_kind or o.reason or "SELL_SIGNAL", "gross_pnl": round(qty * px - basis, 2),
                           "costs": round(ch, 2), "net_pnl": round(net, 2),
                           "return_pct": round(net / basis * 100, 4) if basis else None,
                           "holding_sessions": session_idx - p.entry_session, "entry_reason": p.reason,
                           "impact_bps": round(bps, 2)})
            p.cost -= basis
            p.ref_cost -= p.ref_cost * qty / p.qty
            p.qty -= qty
            if p.qty == 0:
                del positions[o.symbol]

    while q:
        ev = heapq.heappop(q)
        i = ev.data["idx"]
        d = days[i]
        if ev.kind == "MARKET":
            row = ev.data["row"]
            if i == 0 or days[i - 1] != d:
                session_idx += 1
                for s, p in list(positions.items()):           # max hold: out at the session's first bar
                    if p.max_hold and session_idx - p.entry_session >= p.max_hold:
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
                if o.eligible_idx <= i and bar:
                    fill(o, bar, i, d)
                if o.filled >= o.qty:
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
            for sig in signals:
                if sig.symbol not in universe.symbols:
                    continue
                if sig.side in ("ADD", "REDUCE"):           # W39: PF-06 partial changes are W2-engine only
                    events.append({"date": d, "symbol": sig.symbol,
                                   "event": f"{sig.side} not simulated by the event-driven engine"})
                    not_simulated[sig.side] = not_simulated.get(sig.side, 0) + 1
                    continue
                if sig.side == "SELL":
                    if sig.symbol in positions and sig.symbol not in pending_sells:
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
            trades.append({"symbol": s, "entry_date": p.entry_date, "entry_price": round(p.cost / p.qty, 4),
                           "entry_ref_price": round(p.ref_cost / p.qty, 4), "qty": p.qty, "exit_date": last_d,
                           "exit_price": round(px, 4), "exit_ref_price": round(px, 4), "exit_reason": "END_OF_WINDOW",
                           "gross_pnl": round(p.qty * px - p.cost, 2), "costs": round(ch, 2), "net_pnl": round(net, 2),
                           "return_pct": round(net / p.cost * 100, 4) if p.cost else None,
                           "holding_sessions": session_idx - p.entry_session, "entry_reason": p.reason})
        positions.clear()
        equity[-1].update({"cash": round(cash, 2), "positions_value": 0.0, "equity": round(cash, 2), "exposure_pct": 0.0,
                           "n_positions": 0, "realized_cum": round(realized, 2), "unrealized": 0.0})
    for o in working:
        events.append({"date": last_d, "symbol": o.symbol, "event": f"{o.side} {o.qty - o.filled} unfilled at end"})

    eqs = [p["equity"] for p in equity]
    dts = [p["date"] for p in equity]
    for p, (peak, dd) in zip(equity, M.drawdown_series(eqs)):
        p["peak_equity"], p["drawdown_pct"] = round(peak, 2), round(dd * 100, 4)
    rf = float(snap.get("risk_free_rate_pct") or 0) / 100
    metrics = M.summarize(dts, eqs, [t["net_pnl"] for t in trades],
                          [t["return_pct"] for t in trades if t["return_pct"] is not None],
                          rf_annual=rf, exposure=[p["exposure_pct"] or 0 for p in equity])
    metrics.update({"costs_paid": round(costs_paid, 2), "slippage_paid": round(slip_paid, 2),
                    "turnover": round(turnover, 2), "fills": fills_n, "partial_fills": partial_n,
                    "events": len(events), "open_positions_at_end": len(positions)})
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
                    + ([f"partial position changes not simulated by the event-driven engine: "
                        f"{', '.join(f'{k} x{n}' for k, n in sorted(not_simulated.items()))} (the W2 engine trades them)"]
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
