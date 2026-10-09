"""
Portfolio-level backtest engine (BT-08).

One shared cash balance, many positions, one dated equity curve. Each session d:

  1. FILLS      orders queued at the previous close fill at d's open: sells
                first (they free cash), then buys. Every fill passes the
                liquidity rule (volume participation, prior-bar turnover), is
                priced with slippage, is charged by the cost model, and a buy
                is cut to what cash allows.
  2. EXITS      protective stops/targets on d's bar: an open beyond the level
                fills at the open; otherwise the level itself. A bar whose range
                covers both stop and target counts as the stop -- daily bars
                cannot say which came first, and the pessimistic reading is used
                (as in scores/backtest.py).
  3. MAX HOLD   positions held max_hold_sessions are queued to sell at the next open.
  4. MARK       positions valued at d's close (the last close when a symbol has
                no bar that day -- counted as a stale mark); the equity point for
                d is recorded: cash, positions value, equity, exposure, P&L.
  5. STRATEGY   after the close, the strategy sees a point-in-time view of data
                up to d and returns signals; buys are sized with
                orders.risk.size_position (W1, RK-13) on current equity and
                queued for d+1's open. On the last session nothing is queued.

Signals are therefore never filled on the bar that produced them (no same-bar
look-ahead). Prices are prices_daily closes, split/bonus adjusted; dividends
are not added -- results are price returns.

Partial position changes (PF-06): ADD and REDUCE signals (backtest.strategy.Signal)
change a HELD position's size. A run with no ADD / REDUCE signal produces exactly
the result it did before they existed.

  QUEUEING   (step 5, at the close)
    REDUCE   for a symbol not held -> ignored (event). Skipped while a SELL or another
             REDUCE for it is queued. Its size is resolved at the fill: quantity, else
             floor(fraction x held) (at least 1), else half the held quantity (at least 1)
             -- the W4 risk engine's REDUCE default (execution/risk_engine.py).
    ADD      for a symbol not held -> ignored (event): an ADD only increases a held
             position, as the strategy engine emits it (strategy_engine/kinds.py) and
             the live engine maps it. Skipped while a SELL or another ADD for it is
             queued. Sized at the decision close: quantity; else value // close; else
             like a BUY (orders.risk.size_position on current equity, the signal's stop
             or default_stop_pct). Then capped so the position (held qty x close + the
             ADD) stays within sizing.max_position_pct of equity -- the W4 risk
             engine's max_position_pct check; a cap or a skip is an event.
             max_positions does not apply (no new position).
  FILLS      (step 1, at the next open) sells first, then buys, each group by symbol:
             SELL, REDUCE, BUY, ADD. So an exit queued for the same symbol wins over a
             REDUCE / ADD (they are then dropped with an event), and an ADD only gets
             the cash new entries leave.
    REDUCE   priced like a SELL of that size: slippage (SELL side) and the cost model;
             like a SELL it is not liquidity-capped, and with no bar it is deferred to
             the next session (event). It writes a trade row for the shares sold, with
             the position's average entry price and a PRO-RATA share of its accumulated
             entry costs: net = qty x (exit fill - avg entry) - entry_costs x qty / held
             - exit costs, return_pct on that leg's basis. The row carries
             "partial": true and exit_reason "REDUCE". The rest keeps its average
             price, its remaining entry costs, its stop / target and its max-hold clock.
             A REDUCE of the held quantity or more closes the position: an ordinary
             (not partial) row with exit_reason "REDUCE", plus an event.
    ADD      priced like a BUY of that size: liquidity cap (volume participation,
             prior-bar turnover), slippage (BUY side), the cost model, and the entry
             cash check -- cut to what cash allows, or rejected; either is an event.
             No bar at the fill -> dropped (event), like a BUY. The position's quantity
             grows and its entry price becomes the quantity-weighted average of the
             fills (entry_ref_price likewise); the ADD's costs are added to its entry
             costs. Stop, target, entry date and the max-hold clock (from the first
             entry) are unchanged. Rows of a position that was added to carry "adds": n.
  P&L        Every leg is realised once: over a position's life the trade rows' net
             P&L sums to the cash it returned minus the cash it took (fills and costs),
             so the rows' net P&L sums to final equity - initial capital when the run
             ends flat.
  METRICS    Trade statistics stay per closed position (round trip): with partial rows,
             the rows of one position are folded into one trade -- net P&L = the sum of
             its rows, return % = that over the summed basis -- so a position reduced
             twice and then closed is one trade, not three; a position still open at the
             end (close_out_at_end false) is not a closed trade, so its partial rows are
             in the trade rows and equity but not the statistics. Without partial rows
             the definitions and inputs are unchanged. When a run has any ADD or partial
             exit, metrics also carry trade_rows, partial_exits and adds.

Short legs (W40, QR-05 / QR-06): SHORT / COVER signals are simulated as near-month stock
FUTURES by backtest.futures.FuturesBook -- built on the first SHORT signal, so a run without
one never touches it and its result is exactly what it was (the digests are pinned by
tests/test_w40_futures_backtest.py). backtest/futures.py has the rules; in short:

  QUEUEING   (step 5) a SHORT is sized at the decision close in whole lots of the
             near-month future (value / quantity / max_position_pct, capped at it, rounded
             DOWN); no stored futures contract -> refused with an event (never priced off
             spot). COVER of a held short is queued; of anything else ignored (event).
  FUTURES    (step 3b, after the max-hold check, at d's END-OF-DAY futures closes) covers
             fill; held contracts roll futures.roll_days_before_expiry sessions before expiry
             into the next month (or settle at expiry with no roll); new shorts fill; every
             leg is marked to its contract's close with the variation paid through cash; a
             leg held max_hold_sessions is queued to cover at the next close.
  MARK       (step 4) equity = cash + longs + futures margin blocked; exposure is gross.
  ROWS       one trade row per contract leg (instrument FUT, direction SHORT, expiry, lots,
             lot_size, leg; rolled / calendar_spread / roll_cost on a rolled leg); a rolled
             position folds into one round trip like a position with partial exits.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date

from backtest import metrics as M
from backtest.costs import cost_model
from backtest.data import LookAheadError, PriceHistory, ScoresHistory, explicit_universe, tracked_universe
from backtest.liquidity import LiquidityRule
from backtest.slippage import SlippageModel
from backtest.strategies import make_strategy
from backtest.strategy import PositionView, StrategyContext
from utils.trading_calendar import sessions_until

log = logging.getLogger("atip.backtest")


class BacktestError(RuntimeError):
    pass


@dataclass
class Position:
    symbol: str
    qty: int
    entry_price: float           # fill price, after slippage
    entry_ref_price: float       # the open it was filled against
    entry_date: date
    entry_idx: int
    entry_costs: float
    stop: float | None
    target: float | None
    max_hold: int | None
    reason: str = ""
    uid: int = 0                 # which position a trade row belongs to (round trips in metrics)
    adds: int = 0                # ADD fills into it; entry_price is then a weighted average


# fill order at the open: sells first (they free cash), then buys; by symbol within each
_FILL_RANK = {"SELL": 0, "REDUCE": 1, "BUY": 2, "ADD": 3}


@dataclass
class _Order:
    symbol: str
    side: str
    qty: int = 0
    signal: object = None
    reason: str = ""
    queued: date | None = None


@dataclass
class SimState:
    cash: float
    positions: dict = field(default_factory=dict)
    pending: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    equity: list = field(default_factory=list)
    events: list = field(default_factory=list)
    realized: float = 0.0
    costs_paid: float = 0.0
    slippage_paid: float = 0.0
    turnover: float = 0.0
    stale_marks: int = 0
    last_close: dict = field(default_factory=dict)
    trade_legs: list = field(default_factory=list)   # (position uid, net, basis) per trade row, unrounded
    partials: int = 0                                # partial-exit trade rows
    adds: int = 0                                    # ADD fills
    next_uid: int = 0


def _d(x) -> date:
    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


def run(snapshot: dict, conn) -> dict:
    """
    Run a backtest from a resolved configuration snapshot (backtest.service
    resolve_config). Deterministic: the same snapshot over the same data gives
    the same result. Raises BacktestError for a configuration that cannot run.
    """
    stored = bool(snapshot.get("strategy_definition_hash"))      # a W3 strategy version
    strat = make_strategy(snapshot["strategy_id"], snapshot.get("params"),
                          snapshot.get("strategy_version") if stored else None)
    strat.simulates_futures = True      # W40: this engine trades SHORT / COVER (strategy_engine/adapter.py)
    if snapshot.get("strategy_version") and snapshot["strategy_version"] != strat.version:
        raise BacktestError(f"strategy {strat.strategy_id} is now version {strat.version}, the run was "
                            f"configured for {snapshot['strategy_version']} — results would not reproduce")
    if stored and getattr(strat, "definition_hash", None) != snapshot["strategy_definition_hash"]:
        raise BacktestError(f"{strat.strategy_id} {strat.version}: stored definition hash changed since the run "
                            f"was configured — results would not reproduce")
    start, end = _d(snapshot["start"]), _d(snapshot["end"])
    if start > end:
        raise BacktestError(f"start {start} is after end {end}")

    uni = snapshot.get("universe", "tracked_current")
    universe = tracked_universe(conn) if uni == "tracked_current" else explicit_universe(
        uni, snapshot.get("universe_survivorship_bias"))
    if not universe.symbols:
        raise BacktestError("empty universe")

    warm_days = max(60, int(math.ceil((strat.warmup_bars or 0) * 1.6)) + 10)
    history = PriceHistory.load(conn, universe.symbols, start, end, warmup_days=warm_days)
    scores = ScoresHistory(conn, start, end) if strat.uses_scores else None
    sessions = [d for d in history.sessions if start <= d <= end]
    if len(sessions) < 2:
        raise BacktestError(f"fewer than two sessions of data between {start} and {end}")

    costs = cost_model(snapshot["cost_model"], snapshot.get("cost_overrides") or None)
    slip = SlippageModel(**snapshot["slippage"])
    liq = LiquidityRule(**snapshot["liquidity"])
    sizing = snapshot["sizing"]
    from orders.risk import size_position

    st = SimState(cash=float(snapshot["initial_capital"]))
    fut = None                          # W40: the futures book, built on the first SHORT signal

    def close_position(pos: Position, ref_price: float, d: date, i: int, reason: str, qty: int | None = None):
        """Sell the whole position, or -- qty below the held quantity -- that many
        shares of it (a partial exit: pro-rata entry costs, the rest stays open)."""
        partial = qty is not None and qty < pos.qty
        q = qty if partial else pos.qty
        entry_costs = pos.entry_costs * q / pos.qty if partial else pos.entry_costs
        px = slip.fill_price("SELL", ref_price)
        value = q * px
        ch = costs.total("SELL", value)
        st.cash += value - ch
        st.costs_paid += ch
        st.slippage_paid += q * (ref_price - px)
        st.turnover += value
        gross = q * (px - pos.entry_price)
        net = gross - entry_costs - ch
        basis = q * pos.entry_price + entry_costs
        st.realized += net
        row = {
            "symbol": pos.symbol, "entry_date": pos.entry_date, "entry_price": round(pos.entry_price, 4),
            "entry_ref_price": round(pos.entry_ref_price, 4) if pos.adds else pos.entry_ref_price, "qty": q,
            "exit_date": d,
            "exit_price": round(px, 4), "exit_ref_price": round(ref_price, 4), "exit_reason": reason,
            "gross_pnl": round(gross, 2), "costs": round(entry_costs + ch, 2), "net_pnl": round(net, 2),
            "return_pct": round(net / basis * 100, 4) if basis else None,
            "holding_sessions": i - pos.entry_idx, "entry_reason": pos.reason}
        if pos.adds:
            row["adds"] = pos.adds
        if partial:
            row["partial"] = True
            st.partials += 1
            pos.qty -= q
            pos.entry_costs -= entry_costs
        else:
            del st.positions[pos.symbol]
        st.trades.append(row)
        st.trade_legs.append((pos.uid, net, basis))

    def event(d, symbol, text):
        st.events.append({"date": d, "symbol": symbol, "event": text})

    def reduce_quantity(o: _Order, pos: Position) -> int:
        sig = o.signal
        if sig.quantity is not None:
            return int(sig.quantity)
        if sig.fraction is not None:
            return max(1, int(pos.qty * float(sig.fraction) + 1e-9))
        return max(1, pos.qty // 2)

    for i, d in enumerate(sessions):
        # 1. fills at the open -- sells first (SELL, REDUCE), then buys (BUY, ADD)
        keep = []
        for o in sorted(st.pending, key=lambda o: (_FILL_RANK[o.side], o.symbol)):
            bar = history.bar(o.symbol, d)
            if o.side in ("SELL", "REDUCE"):
                pos = st.positions.get(o.symbol)
                if not pos:
                    if o.side == "REDUCE":
                        event(d, o.symbol, "reduce dropped: position no longer held")
                    continue
                if not bar:
                    keep.append(o)              # no market that day: try the next session
                    event(d, o.symbol, f"{o.side.lower()} deferred: no bar")
                    continue
                if o.side == "SELL":
                    close_position(pos, bar.open, d, i, o.reason or "SELL_SIGNAL")
                    continue
                q = reduce_quantity(o, pos)
                if q >= pos.qty:
                    event(d, o.symbol, f"reduce {q} >= {pos.qty} held: position closed")
                close_position(pos, bar.open, d, i, "REDUCE", q)
                continue
            pos = st.positions.get(o.symbol) if o.side == "ADD" else None
            if o.side == "ADD" and not pos:
                event(d, o.symbol, "add dropped: position no longer held")
                continue
            if not bar:
                event(d, o.symbol, f"{o.side.lower()} dropped: no bar at fill")
                continue
            prior = history.bars_before(o.symbol, d, liq.adv_lookback)
            adv = (sum(b.close * b.volume for b in prior) / len(prior)) if len(prior) >= liq.adv_lookback else None
            qty, why = liq.allowed_quantity(o.qty, bar.volume, adv)
            if why:
                event(d, o.symbol, f"liquidity: {why}")
            px = slip.fill_price("BUY", bar.open)
            wanted = qty
            while qty > 0 and qty * px + costs.total("BUY", qty * px) > st.cash:
                qty = min(qty - 1, int(st.cash // (px * 1.01)))    # cut to what cash allows
            if qty < 1:
                event(d, o.symbol, f"{o.side.lower()} rejected: no cash / liquidity")
                continue
            if o.side == "ADD" and qty < wanted:
                event(d, o.symbol, f"add cut to {qty} of {wanted} shares: cash")
            value = qty * px
            ch = costs.total("BUY", value)
            st.cash -= value + ch
            st.costs_paid += ch
            st.slippage_paid += qty * (px - bar.open)
            st.turnover += value
            if o.side == "ADD":                 # weighted-average entry; costs accumulate on the position
                n = pos.qty + qty
                pos.entry_price = (pos.qty * pos.entry_price + qty * px) / n
                pos.entry_ref_price = (pos.qty * pos.entry_ref_price + qty * bar.open) / n
                pos.entry_costs += ch
                pos.qty = n
                pos.adds += 1
                st.adds += 1
                continue
            sig = o.signal
            stop = sig.stop_price if sig.stop_price is not None else px * (1 - sizing["default_stop_pct"] / 100)
            st.next_uid += 1
            st.positions[o.symbol] = Position(o.symbol, qty, px, bar.open, d, i, ch, stop, sig.target_price,
                                              sig.max_hold_sessions, sig.reason, uid=st.next_uid)
        st.pending = keep

        # 2. protective exits
        for sym in sorted(st.positions):
            pos = st.positions[sym]
            bar = history.bar(sym, d)
            if not bar:
                continue
            if pos.stop is not None and bar.open <= pos.stop:
                close_position(pos, bar.open, d, i, "STOP_GAP")
            elif pos.stop is not None and bar.low <= pos.stop:
                close_position(pos, pos.stop, d, i, "STOP")
            elif pos.target is not None and bar.open >= pos.target:
                close_position(pos, bar.open, d, i, "TARGET_GAP")
            elif pos.target is not None and bar.high >= pos.target:
                close_position(pos, pos.target, d, i, "TARGET")

        # 3. max hold -> sell at the next open
        pending_sells = {o.symbol for o in st.pending if o.side == "SELL"}
        for sym, pos in st.positions.items():
            if pos.max_hold and i - pos.entry_idx >= pos.max_hold and sym not in pending_sells:
                st.pending.append(_Order(sym, "SELL", reason="MAX_HOLD", queued=d))

        # 3b. futures legs at d's end-of-day futures closes (W40)
        if fut:
            fut.session(d, i, sessions_until)

        # 4. mark to market
        pv = cost_basis = 0.0
        for sym, pos in st.positions.items():
            bar = history.bar(sym, d)
            if bar:
                st.last_close[sym] = bar.close
            else:
                st.stale_marks += 1
            mark = st.last_close.get(sym, pos.entry_price)
            pv += pos.qty * mark
            cost_basis += pos.qty * pos.entry_price
        eq = st.cash + pv
        if fut:                                 # W40: the blocked futures margin is equity
            f_margin, f_notional = fut.margin(), fut.notional()
            eq += f_margin
        prev_eq = st.equity[-1]["equity"] if st.equity else float(snapshot["initial_capital"])
        point = {"date": d, "cash": round(st.cash, 2), "positions_value": round(pv, 2),
                 "equity": round(eq, 2), "exposure_pct": round(pv / eq * 100, 4) if eq else None,
                 "n_positions": len(st.positions), "realized_cum": round(st.realized, 2),
                 "unrealized": round(pv - cost_basis, 2),
                 "daily_return": round(eq / prev_eq - 1, 8) if prev_eq else None}
        if fut:                                 # gross exposure; shorts count as positions
            point.update({"exposure_pct": round((pv + f_notional) / eq * 100, 4) if eq else None,
                          "n_positions": len(st.positions) + len(fut.positions),
                          "unrealized": round(pv - cost_basis + fut.unrealized(), 2),
                          "futures_margin": round(f_margin, 2), "futures_notional": round(f_notional, 2)})
        st.equity.append(point)

        # 5. strategy, after the close -- nothing queued on the last session
        if i == len(sessions) - 1:
            break
        ctx = StrategyContext(
            as_of=d, data=history.view(d), universe=universe.symbols,
            positions={s: PositionView(s, p.qty, p.entry_price, p.entry_date, i - p.entry_idx)
                       for s, p in st.positions.items()},
            cash=st.cash, equity=eq, params=dict(strat.params), scores=scores,
            futures=fut.views(i) if fut else {})
        try:
            signals = strat.on_bar(ctx) or []
        except LookAheadError as e:
            raise BacktestError(f"look-ahead blocked on {d}: {e}") from e
        queued_buys = {o.symbol for o in st.pending if o.side == "BUY"}
        queued_sells = {o.symbol for o in st.pending if o.side == "SELL"}
        queued_changes = {(o.side, o.symbol) for o in st.pending if o.side in ("ADD", "REDUCE")}
        for s in signals:
            if s.symbol not in universe.symbols:
                st.events.append({"date": d, "symbol": s.symbol, "event": "signal outside universe ignored"})
                continue
            if s.side in ("SHORT", "COVER"):    # W40: a short leg, as a near-month stock future
                if s.side == "COVER" and not fut:
                    event(d, s.symbol, f"cover ignored: {s.symbol} is not held short")
                    continue
                if not fut:
                    from backtest.futures import FuturesBook
                    fut = FuturesBook(conn, snapshot, universe.symbols, start, end, slip, st, event)
                fut.queue(s, d, eq, sizing, len(st.positions) + len(queued_buys))
                continue
            if s.side == "SELL":
                if s.symbol in st.positions and s.symbol not in queued_sells:
                    st.pending.append(_Order(s.symbol, "SELL", signal=s, reason="SELL_SIGNAL", queued=d))
                    queued_sells.add(s.symbol)
                continue
            if s.side in ("ADD", "REDUCE"):
                pos = st.positions.get(s.symbol)
                if not pos:
                    event(d, s.symbol, f"{s.side.lower()} ignored: {s.symbol} is not held")
                    continue
                if s.symbol in queued_sells or (s.side, s.symbol) in queued_changes:
                    continue                    # an exit, or the same change, is already queued
                if s.side == "REDUCE":          # sized at the fill, against what is held then
                    st.pending.append(_Order(s.symbol, "REDUCE", signal=s, reason="REDUCE", queued=d))
                    queued_changes.add(("REDUCE", s.symbol))
                    continue
                bar = history.bar(s.symbol, d)
                if not bar:
                    event(d, s.symbol, "add skipped: no bar at signal")
                    continue
                if s.quantity is not None:
                    q = int(s.quantity)
                elif s.value is not None:
                    q = int(float(s.value) // bar.close)
                else:
                    stop = s.stop_price if s.stop_price is not None else bar.close * (1 - sizing["default_stop_pct"] / 100)
                    sz = size_position(bar.close, stop, capital=eq, risk_per_trade_pct=sizing["risk_per_trade_pct"],
                                       max_position_pct=sizing["max_position_pct"])
                    q = int(sz["quantity"])
                    if q < 1:
                        event(d, s.symbol, f"add skipped: {sz['reason']}")
                        continue
                room = int((eq * sizing["max_position_pct"] / 100 - pos.qty * bar.close) // bar.close)
                if q > room:
                    if room < 1:
                        event(d, s.symbol, f"add skipped: position at max_position_pct "
                                           f"({sizing['max_position_pct']}% of equity)")
                        continue
                    event(d, s.symbol, f"add capped to {room} of {q} shares: max_position_pct "
                                       f"({sizing['max_position_pct']}% of equity)")
                    q = room
                if q < 1:
                    event(d, s.symbol, "add skipped: less than one share")
                    continue
                st.pending.append(_Order(s.symbol, "ADD", qty=q, signal=s, reason="ADD", queued=d))
                queued_changes.add(("ADD", s.symbol))
                continue
            if s.symbol in st.positions or s.symbol in queued_buys:
                continue
            if fut and s.symbol in fut.positions:
                event(d, s.symbol, f"buy ignored: {s.symbol} is held short (futures)")
                continue
            n_open = len(st.positions) + len(queued_buys) + (fut.open_count() if fut else 0)     # W40: + shorts
            if n_open >= int(sizing["max_positions"]):
                st.events.append({"date": d, "symbol": s.symbol, "event": "buy skipped: max_positions"})
                continue
            bar = history.bar(s.symbol, d)
            if not bar:
                st.events.append({"date": d, "symbol": s.symbol, "event": "buy skipped: no bar at signal"})
                continue
            stop = s.stop_price if s.stop_price is not None else bar.close * (1 - sizing["default_stop_pct"] / 100)
            sz = size_position(bar.close, stop, capital=eq, risk_per_trade_pct=sizing["risk_per_trade_pct"],
                               max_position_pct=sizing["max_position_pct"])
            if sz["quantity"] < 1:
                st.events.append({"date": d, "symbol": s.symbol, "event": f"buy skipped: {sz['reason']}"})
                continue
            st.pending.append(_Order(s.symbol, "BUY", qty=sz["quantity"], signal=s, queued=d))
            queued_buys.add(s.symbol)

    # end of window: close out at the last close (charged), or leave marked
    last_i, last_d = len(sessions) - 1, sessions[-1]
    for o in st.pending:
        st.events.append({"date": last_d, "symbol": o.symbol, "event": f"{o.side} unfilled at end of window"})
    for o in (fut.pending if fut else []):
        st.events.append({"date": last_d, "symbol": o.symbol, "event": f"{o.side} unfilled at end of window"})
    has_fut = bool(fut and fut.positions)
    if snapshot.get("close_out_at_end", True) and (st.positions or has_fut):
        for sym in sorted(st.positions):
            close_position(st.positions[sym], st.last_close.get(sym, st.positions[sym].entry_price),
                           last_d, last_i, "END_OF_WINDOW")
        if has_fut:
            fut.close_all(last_d, last_i)
        pt = st.equity[-1]
        prev = st.equity[-2]["equity"] if len(st.equity) > 1 else float(snapshot["initial_capital"])
        pt.update({"cash": round(st.cash, 2), "positions_value": 0.0, "equity": round(st.cash, 2),
                   "exposure_pct": 0.0, "n_positions": 0, "realized_cum": round(st.realized, 2),
                   "unrealized": 0.0, "daily_return": round(st.cash / prev - 1, 8) if prev else None})
        if fut:
            pt.update({"futures_margin": 0.0, "futures_notional": 0.0})
    if fut:                                     # every point of a futures run carries the columns
        for p in st.equity:
            p.setdefault("futures_margin", 0.0)
            p.setdefault("futures_notional", 0.0)

    # drawdown columns on the curve, metrics, episodes
    eqs = [p["equity"] for p in st.equity]
    dates = [p["date"] for p in st.equity]
    for p, (peak, dd) in zip(st.equity, M.drawdown_series(eqs)):
        p["peak_equity"], p["drawdown_pct"] = round(peak, 2), round(dd * 100, 4)
    rf = float(snapshot.get("risk_free_rate_pct") or 0) / 100
    pnls = [t["net_pnl"] for t in st.trades]
    rets = [t["return_pct"] for t in st.trades if t["return_pct"] is not None]
    if st.partials or (fut and fut.rolls):
        pnls, rets = _round_trips(st, {p.uid for p in fut.positions.values()} if fut else ())
    metrics = M.summarize(dates, eqs, pnls, rets, rf_annual=rf, exposure=[p["exposure_pct"] or 0 for p in st.equity])
    metrics.update({"costs_paid": round(st.costs_paid, 2), "slippage_paid": round(st.slippage_paid, 2),
                    "turnover": round(st.turnover, 2), "events": len(st.events),
                    "open_positions_at_end": len(st.positions)})
    if st.partials or st.adds:
        metrics.update({"trade_rows": len(st.trades), "partial_exits": st.partials, "adds": st.adds})
    if fut:
        metrics.update(fut.metrics())
        if fut.rolls:
            metrics["trade_rows"] = len(st.trades)
    episodes = M.drawdown_episodes(dates, eqs)

    bias = {
        "entry_timing": "signals decided at the close fill at the next session's open; never on the signal bar",
        "point_in_time_data": True,
        "universe": universe.as_dict(),
        "data_validation": {"bars_dropped": len(history.issues), "sample": history.issues[:10]},
        "stale_marks": st.stale_marks,
        "price_basis": "prices_daily closes, split/bonus adjusted; dividends not included (price return)",
        "warnings": ([ScoresHistory.CAVEAT] if strat.uses_scores else [])
                    + _not_simulated_warning(strat)
                    + (["survivorship bias: universe is today's constituents"] if universe.survivorship_bias else []),
    }
    if fut:
        bias["futures"], warns = fut.bias()
        bias["warnings"] = bias["warnings"] + warns
    return {"strategy": strat.describe(), "sessions": len(sessions), "trades": st.trades,
            "equity": st.equity, "metrics": metrics, "drawdowns": episodes, "events": st.events,
            "bias_report": bias, "data_fingerprint": history.fingerprint()}


def _not_simulated_warning(strat) -> list:
    """Decisions a strategy made that this engine could not trade. A W3 strategy
    version (strategy_engine/adapter.py) counts them in `not_simulated`
    ({action: n}). ADD / REDUCE of a held long are simulated, and so are SHORT / COVER
    (as near-month stock futures, W40), so only what is left appears: a change or exit of
    a position the run did not hold in that form (e.g. "REDUCE (not held)",
    "COVER (not held short)", "EXIT (held short)")."""
    skipped = getattr(strat, "not_simulated", None) or {}
    if not skipped:
        return []
    what = ", ".join(f"{a} x{n}" for a, n in sorted(skipped.items()))
    return [f"decisions not simulated: {what} -- each names a position the backtest did not hold in that form "
            f"(long legs are a cash book; SHORT / COVER are simulated as near-month stock futures)"]


def needs_folding(rows: list) -> bool:
    """True when stored rows hold more than one row for some position: partial exits
    (PF-06) or rolled futures legs (W40) -- fold them with round_trips_from_rows."""
    return any(t.get("partial") or t.get("rolled") for t in rows)


def round_trips_from_rows(rows: list) -> list:
    """Stored / returned trade rows -> one dict per CLOSED position, for consumers of saved
    runs (walk-forward stitched metrics, the Monte Carlo trade shuffle): a position's rows
    -- its partial exits ("partial") and the close -- share (symbol, entry_date), since a
    symbol holds one position at a time. Each: {"symbol", "entry_date", "exit_date",
    "net_pnl", "return_pct", "qty", "entry_price", "rows"}; return_pct is the summed net
    over the summed cost basis (each row's basis = net / return, else qty x entry price).
    A position with only partial rows is still open (close_out_at_end false): left out.
    Rows of runs without partial exits map one to one, figures unchanged.
    W40: a futures short's contract legs (instrument "FUT"; "rolled" on each leg a roll
    closed) group by (symbol, entry_date, instrument) too; each leg holds the whole
    position, so the basis, qty and entry price are those of its FIRST leg (leg 1) and the
    net is the sum of its legs. A position whose rows are all rolled is still open."""
    groups, order = {}, []
    for t in rows:
        k = (t["symbol"], str(t["entry_date"]), t.get("instrument"))
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(t)
    out = []
    for k in order:
        g = groups[k]
        if all(t.get("partial") or t.get("rolled") for t in g):
            continue
        if len(g) == 1:
            t = g[0]
            out.append({"symbol": t["symbol"], "entry_date": t["entry_date"], "exit_date": t["exit_date"],
                        "net_pnl": t["net_pnl"], "return_pct": t["return_pct"], "qty": t["qty"],
                        "entry_price": t["entry_price"], "rows": 1})
            continue
        net = sum(t["net_pnl"] or 0 for t in g)
        first = [t for t in g if (t.get("leg") or 1) == 1]          # every row, unless futures legs
        basis = sum((t["net_pnl"] / (t["return_pct"] / 100)) if t.get("return_pct") else
                    (t["qty"] * t["entry_price"]) for t in first)
        qty = sum(t["qty"] for t in first)
        out.append({"symbol": g[0]["symbol"], "entry_date": g[0]["entry_date"], "exit_date": g[-1]["exit_date"],
                    "net_pnl": round(net, 2), "return_pct": round(net / basis * 100, 4) if basis else None,
                    "qty": qty, "entry_price": round(sum(t["qty"] * t["entry_price"] for t in first) / qty, 4),
                    "rows": len(g)})
    out.sort(key=lambda r: str(r["exit_date"]))
    return out


def _round_trips(st: SimState, also_open=()) -> tuple[list, list]:
    """Net P&L and return % per CLOSED position (round trip) for the trade statistics:
    a position's rows (partial exits and the close; a futures short's rolled contract
    legs, W40) fold into one trade. Ordered by each position's closing row; a one-row
    position keeps its row's own figures. A position still open at the end
    (close_out_at_end false) is not a closed trade: its partial rows stay in the trade
    rows and the equity curve, not in the stats. also_open: uids of open futures legs."""
    return fold_round_trips(st.trades, st.trade_legs, {p.uid for p in st.positions.values()} | set(also_open))


def fold_round_trips(trades: list, trade_legs: list, still_open: set) -> tuple[list, list]:
    """_round_trips on plain lists, for any engine that keeps one (position uid, net,
    basis) leg per trade row (the event-driven engine, BT-17): `still_open` holds the
    uids of positions open at the end."""
    rows, legs = {}, {}
    for t, (uid, net, basis) in zip(trades, trade_legs):
        rows.setdefault(uid, []).append(t)
        legs.setdefault(uid, []).append((net, basis))
    last = {}
    for k, (uid, _n, _b) in enumerate(trade_legs):
        if uid not in still_open:
            last[uid] = k
    pnls, rets = [], []
    for uid in sorted(last, key=last.get):
        if len(rows[uid]) == 1:
            t = rows[uid][0]
            pnls.append(t["net_pnl"])
            if t["return_pct"] is not None:
                rets.append(t["return_pct"])
            continue
        net = sum(n for n, _b in legs[uid])
        basis = sum(b for _n, b in legs[uid])
        pnls.append(round(net, 2))
        if basis:
            rets.append(round(net / basis * 100, 4))
    return pnls, rets
