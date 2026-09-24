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


def _d(x) -> date:
    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


def run(snapshot: dict, conn) -> dict:
    """
    Run a backtest from a resolved configuration snapshot (backtest.service
    resolve_config). Deterministic: the same snapshot over the same data gives
    the same result. Raises BacktestError for a configuration that cannot run.
    """
    strat = make_strategy(snapshot["strategy_id"], snapshot.get("params"))
    if snapshot.get("strategy_version") and snapshot["strategy_version"] != strat.version:
        raise BacktestError(f"strategy {strat.strategy_id} is now version {strat.version}, the run was "
                            f"configured for {snapshot['strategy_version']} — results would not reproduce")
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

    def close_position(pos: Position, ref_price: float, d: date, i: int, reason: str):
        px = slip.fill_price("SELL", ref_price)
        value = pos.qty * px
        ch = costs.total("SELL", value)
        st.cash += value - ch
        st.costs_paid += ch
        st.slippage_paid += pos.qty * (ref_price - px)
        st.turnover += value
        gross = pos.qty * (px - pos.entry_price)
        net = gross - pos.entry_costs - ch
        basis = pos.qty * pos.entry_price + pos.entry_costs
        st.realized += net
        st.trades.append({
            "symbol": pos.symbol, "entry_date": pos.entry_date, "entry_price": round(pos.entry_price, 4),
            "entry_ref_price": pos.entry_ref_price, "qty": pos.qty, "exit_date": d,
            "exit_price": round(px, 4), "exit_ref_price": round(ref_price, 4), "exit_reason": reason,
            "gross_pnl": round(gross, 2), "costs": round(pos.entry_costs + ch, 2), "net_pnl": round(net, 2),
            "return_pct": round(net / basis * 100, 4) if basis else None,
            "holding_sessions": i - pos.entry_idx, "entry_reason": pos.reason})
        del st.positions[pos.symbol]

    for i, d in enumerate(sessions):
        # 1. fills at the open -- sells first
        keep = []
        for o in sorted(st.pending, key=lambda o: (o.side != "SELL", o.symbol)):
            bar = history.bar(o.symbol, d)
            if o.side == "SELL":
                pos = st.positions.get(o.symbol)
                if not pos:
                    continue
                if not bar:
                    keep.append(o)              # no market that day: try the next session
                    st.events.append({"date": d, "symbol": o.symbol, "event": "sell deferred: no bar"})
                    continue
                close_position(pos, bar.open, d, i, o.reason or "SELL_SIGNAL")
                continue
            if not bar:
                st.events.append({"date": d, "symbol": o.symbol, "event": "buy dropped: no bar at fill"})
                continue
            prior = history.bars_before(o.symbol, d, liq.adv_lookback)
            adv = (sum(b.close * b.volume for b in prior) / len(prior)) if len(prior) >= liq.adv_lookback else None
            qty, why = liq.allowed_quantity(o.qty, bar.volume, adv)
            if why:
                st.events.append({"date": d, "symbol": o.symbol, "event": f"liquidity: {why}"})
            px = slip.fill_price("BUY", bar.open)
            while qty > 0 and qty * px + costs.total("BUY", qty * px) > st.cash:
                qty = min(qty - 1, int(st.cash // (px * 1.01)))    # cut to what cash allows
            if qty < 1:
                st.events.append({"date": d, "symbol": o.symbol, "event": "buy rejected: no cash / liquidity"})
                continue
            value = qty * px
            ch = costs.total("BUY", value)
            st.cash -= value + ch
            st.costs_paid += ch
            st.slippage_paid += qty * (px - bar.open)
            st.turnover += value
            sig = o.signal
            stop = sig.stop_price if sig.stop_price is not None else px * (1 - sizing["default_stop_pct"] / 100)
            st.positions[o.symbol] = Position(o.symbol, qty, px, bar.open, d, i, ch, stop, sig.target_price,
                                              sig.max_hold_sessions, sig.reason)
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
        prev_eq = st.equity[-1]["equity"] if st.equity else float(snapshot["initial_capital"])
        st.equity.append({"date": d, "cash": round(st.cash, 2), "positions_value": round(pv, 2),
                          "equity": round(eq, 2), "exposure_pct": round(pv / eq * 100, 4) if eq else None,
                          "n_positions": len(st.positions), "realized_cum": round(st.realized, 2),
                          "unrealized": round(pv - cost_basis, 2),
                          "daily_return": round(eq / prev_eq - 1, 8) if prev_eq else None})

        # 5. strategy, after the close -- nothing queued on the last session
        if i == len(sessions) - 1:
            break
        ctx = StrategyContext(
            as_of=d, data=history.view(d), universe=universe.symbols,
            positions={s: PositionView(s, p.qty, p.entry_price, p.entry_date, i - p.entry_idx)
                       for s, p in st.positions.items()},
            cash=st.cash, equity=eq, params=dict(strat.params), scores=scores)
        try:
            signals = strat.on_bar(ctx) or []
        except LookAheadError as e:
            raise BacktestError(f"look-ahead blocked on {d}: {e}") from e
        queued_buys = {o.symbol for o in st.pending if o.side == "BUY"}
        queued_sells = {o.symbol for o in st.pending if o.side == "SELL"}
        for s in signals:
            if s.symbol not in universe.symbols:
                st.events.append({"date": d, "symbol": s.symbol, "event": "signal outside universe ignored"})
                continue
            if s.side == "SELL":
                if s.symbol in st.positions and s.symbol not in queued_sells:
                    st.pending.append(_Order(s.symbol, "SELL", signal=s, reason="SELL_SIGNAL", queued=d))
                    queued_sells.add(s.symbol)
                continue
            if s.symbol in st.positions or s.symbol in queued_buys:
                continue
            if len(st.positions) + len(queued_buys) >= int(sizing["max_positions"]):
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
    if snapshot.get("close_out_at_end", True) and st.positions:
        for sym in sorted(st.positions):
            close_position(st.positions[sym], st.last_close.get(sym, st.positions[sym].entry_price),
                           last_d, last_i, "END_OF_WINDOW")
        pt = st.equity[-1]
        prev = st.equity[-2]["equity"] if len(st.equity) > 1 else float(snapshot["initial_capital"])
        pt.update({"cash": round(st.cash, 2), "positions_value": 0.0, "equity": round(st.cash, 2),
                   "exposure_pct": 0.0, "n_positions": 0, "realized_cum": round(st.realized, 2),
                   "unrealized": 0.0, "daily_return": round(st.cash / prev - 1, 8) if prev else None})

    # drawdown columns on the curve, metrics, episodes
    eqs = [p["equity"] for p in st.equity]
    dates = [p["date"] for p in st.equity]
    for p, (peak, dd) in zip(st.equity, M.drawdown_series(eqs)):
        p["peak_equity"], p["drawdown_pct"] = round(peak, 2), round(dd * 100, 4)
    rf = float(snapshot.get("risk_free_rate_pct") or 0) / 100
    metrics = M.summarize(dates, eqs, [t["net_pnl"] for t in st.trades],
                          [t["return_pct"] for t in st.trades if t["return_pct"] is not None],
                          rf_annual=rf, exposure=[p["exposure_pct"] or 0 for p in st.equity])
    metrics.update({"costs_paid": round(st.costs_paid, 2), "slippage_paid": round(st.slippage_paid, 2),
                    "turnover": round(st.turnover, 2), "events": len(st.events),
                    "open_positions_at_end": len(st.positions)})
    episodes = M.drawdown_episodes(dates, eqs)

    bias = {
        "entry_timing": "signals decided at the close fill at the next session's open; never on the signal bar",
        "point_in_time_data": True,
        "universe": universe.as_dict(),
        "data_validation": {"bars_dropped": len(history.issues), "sample": history.issues[:10]},
        "stale_marks": st.stale_marks,
        "price_basis": "prices_daily closes, split/bonus adjusted; dividends not included (price return)",
        "warnings": ([ScoresHistory.CAVEAT] if strat.uses_scores else [])
                    + (["survivorship bias: universe is today's constituents"] if universe.survivorship_bias else []),
    }
    return {"strategy": strat.describe(), "sessions": len(sessions), "trades": st.trades,
            "equity": st.equity, "metrics": metrics, "drawdowns": episodes, "events": st.events,
            "bias_report": bias, "data_fingerprint": history.fingerprint()}
