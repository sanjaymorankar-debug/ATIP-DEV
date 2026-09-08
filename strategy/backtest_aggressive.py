"""
Deterministic replay of the aggressive exit strategy over a price path.

Pure simulation: bars in, trade record out. No database, no broker, no clock,
so a scenario means exactly the same thing on every machine and every run — and
the same function can later be pointed at real `prices_daily` bars.

Two conventions matter, and both are deliberately pessimistic, because a
backtest that flatters itself is worse than no backtest:

  * **Same-bar ambiguity resolves against us.** If a bar's low hits the stop and
    its high hits the target, we assume the stop went first. Intraday order is
    unknowable from a daily bar, and assuming the favourable order is how a
    losing strategy is made to look profitable.
  * **Costs are charged on every leg.** `cost_pct` is a round-trip percentage of
    turnover applied to the partial exit and the final exit separately. Last
    session's work on this database put a realistic figure near 0.25%, and a 3%
    target does not survive being measured without it.

The strategy being replayed is the one in `strategy.aggressive`: stop protects
100% until +3%, +3% books half and starts the trail, +6% is a momentum
checkpoint rather than a sale, and whatever is left leaves on the trail.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from strategy.aggressive import (
    Momentum, checkpoint_decision, config, partial_exit_quantity, ratchet,
    realised_pnl, target_price, trail_distance_pct,
    EXIT_RUNNER, INITIAL_STOP_LOSS, TARGET_1_PARTIAL_EXIT, TARGET_2_MOMENTUM_EXIT,
    TRAILING_STOP, END_OF_DAY_EXIT,
)


@dataclass
class Bar:
    high: float
    low: float
    close: float


@dataclass
class Trade:
    symbol: str
    entry_price: float
    quantity: int
    bars_held: int = 0
    t1_hit: bool = False
    t1_price: float | None = None
    t1_qty: int = 0
    t2_reached: bool = False
    t2_verdict: str | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    realized: float = 0.0
    peak_pct: float = 0.0
    events: list = field(default_factory=list)

    @property
    def is_win(self) -> bool:
        return self.realized > 0

    @property
    def runner_profit(self) -> float:
        """Money earned after the +3% partial — what the runner was worth."""
        if not self.t1_hit or self.exit_price is None:
            return 0.0
        return round(self.realized - self._t1_component(), 2)

    def _t1_component(self) -> float:
        if not self.t1_hit:
            return 0.0
        return realised_pnl(self.entry_price, self.t1_price, self.t1_qty,
                            cost_pct=self._cost)

    _cost: float = 0.0


def simulate(entry_price: float, quantity: int, bars: list[Bar],
             momentum: Momentum = None, cfg: dict = None,
             stop_pct: float = 3.0, cost_pct: float = 0.25,
             symbol: str = "SIM", exit_all_at_t1: bool = False,
             atr_pct: float = None) -> Trade:
    """
    Replay one trade. `momentum` is the reading used at the +6% checkpoint.

    `exit_all_at_t1=True` models a plain fixed-target strategy — sell the whole
    position at target 1 and stop. It exists so a comparison against "flat 3%"
    is actually flat: expressing it as a 99% partial leaves one share trailing
    (partial_exit_quantity always keeps a runner), which quietly turns the
    baseline into a different strategy and flatters or penalises it at random.

    Returns a Trade with computed P&L — nothing about the outcome is asserted or
    hard-coded, so a scenario that stops working will show it in the numbers.
    """
    cfg = cfg or config()
    t = Trade(symbol=symbol, entry_price=entry_price, quantity=quantity)
    t._cost = cost_pct

    t1_price = target_price(entry_price, float(cfg["target_1_pct"]))
    t2_price = target_price(entry_price, float(cfg["target_2_pct"]))
    initial_stop = round(entry_price * (1 - stop_pct / 100.0), 2)

    remaining = quantity
    # Pass the real ATR through. Leaving it None silently collapses the trail
    # onto its floor, so every "volatility-aware" result would actually be
    # measuring one fixed percentage.
    trail_pct = trail_distance_pct(atr_pct, cfg)
    trail_stop = None
    high_water = entry_price

    for i, b in enumerate(bars, 1):
        t.bars_held = i
        t.peak_pct = max(t.peak_pct, round((b.high - entry_price) / entry_price * 100, 2))

        # ── before the partial: the initial stop protects the whole position ──
        if not t.t1_hit:
            if b.low <= initial_stop:
                t.exit_price = initial_stop
                t.exit_reason = INITIAL_STOP_LOSS
                t.realized += realised_pnl(entry_price, initial_stop, remaining,
                                           cost_pct=cost_pct)
                t.events.append((i, INITIAL_STOP_LOSS, initial_stop, remaining))
                remaining = 0
                break
            if b.high >= t1_price:
                qty = quantity if exit_all_at_t1 else partial_exit_quantity(quantity, cfg)
                t.t1_hit, t.t1_price, t.t1_qty = True, t1_price, qty
                t.realized += realised_pnl(entry_price, t1_price, qty, cost_pct=cost_pct)
                remaining -= qty
                high_water = max(high_water, b.high)
                trail_stop = ratchet(None, high_water, trail_pct)
                t.events.append((i, TARGET_1_PARTIAL_EXIT, t1_price, qty))
                if remaining <= 0:
                    t.exit_price, t.exit_reason = t1_price, TARGET_1_PARTIAL_EXIT
                    break
            else:
                continue        # nothing else can happen this bar

        # ── the runner ────────────────────────────────────────────────────
        # Stop first: same-bar ambiguity resolves against us.
        if trail_stop is not None and b.low <= trail_stop:
            t.exit_price = trail_stop
            t.exit_reason = TRAILING_STOP
            t.realized += realised_pnl(entry_price, trail_stop, remaining, cost_pct=cost_pct)
            t.events.append((i, TRAILING_STOP, trail_stop, remaining))
            remaining = 0
            break

        # +6% checkpoint, once.
        if not t.t2_reached and b.high >= t2_price:
            t.t2_reached = True
            d = checkpoint_decision(momentum or Momentum(), cfg, trail_pct, atr_pct)
            t.t2_verdict = d.verdict
            t.events.append((i, "CHECKPOINT", t2_price, d.verdict))
            if d.verdict == EXIT_RUNNER:
                t.exit_price = t2_price
                t.exit_reason = TARGET_2_MOMENTUM_EXIT
                t.realized += realised_pnl(entry_price, t2_price, remaining, cost_pct=cost_pct)
                t.events.append((i, TARGET_2_MOMENTUM_EXIT, t2_price, remaining))
                remaining = 0
                break
            if d.trail_pct is not None:
                trail_pct = d.trail_pct

        high_water = max(high_water, b.high)
        trail_stop = ratchet(trail_stop, high_water, trail_pct)

    # Still holding when the data runs out: close at the last close, flagged so
    # it is never mistaken for a strategy exit.
    if remaining > 0 and bars:
        last = bars[-1].close
        t.exit_price = last
        t.exit_reason = t.exit_reason or END_OF_DAY_EXIT
        t.realized += realised_pnl(entry_price, last, remaining, cost_pct=cost_pct)
        t.events.append((len(bars), END_OF_DAY_EXIT, last, remaining))

    t.realized = round(t.realized, 2)
    return t


# ═══════════════════════════════════════════════════════════════════════════
#  METRICS
# ═══════════════════════════════════════════════════════════════════════════

def metrics(trades: list[Trade]) -> dict:
    """
    Every figure is computed from the trades, none is asserted.

    `profit_factor` is None rather than infinity when there are no losses — a
    strategy with no losing trades in the sample has an undefined profit factor,
    and printing "inf" invites reading it as a result.
    """
    if not trades:
        return {"total_trades": 0}

    wins = [t for t in trades if t.realized > 0]
    losses = [t for t in trades if t.realized < 0]
    gross_profit = round(sum(t.realized for t in wins), 2)
    gross_loss = round(sum(t.realized for t in losses), 2)
    net = round(gross_profit + gross_loss, 2)

    equity, peak, max_dd = 0.0, 0.0, 0.0
    for t in trades:
        equity += t.realized
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)

    n = len(trades)
    win_rate = round(len(wins) / n * 100, 1)
    avg_win = round(gross_profit / len(wins), 2) if wins else 0.0
    avg_loss = round(gross_loss / len(losses), 2) if losses else 0.0
    runners = [t for t in trades if t.t1_hit]
    runner_wins = [t for t in runners if t.runner_profit > 0]

    return {
        "total_trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "net_profit": net,
        "profit_factor": round(gross_profit / abs(gross_loss), 2) if gross_loss else None,
        "expectancy": round(net / n, 2),
        # Rupee expectancy is meaningless without knowing the position size.
        # This is the same number as a percentage of capital actually deployed,
        # which is what can be compared across policies and against costs.
        "expectancy_pct": round(
            net / sum(t.entry_price * t.quantity for t in trades) * 100, 4)
        if sum(t.entry_price * t.quantity for t in trades) else None,
        "max_drawdown": round(max_dd, 2),
        "avg_bars_held": round(statistics.mean(t.bars_held for t in trades), 1),
        "target_1_hit_rate_pct": round(len(runners) / n * 100, 1),
        "target_2_reached_pct": round(sum(t.t2_reached for t in trades) / n * 100, 1),
        "runner_success_pct": round(len(runner_wins) / len(runners) * 100, 1) if runners else None,
        "trailing_exit_pct": round(sum(t.exit_reason == TRAILING_STOP for t in trades) / n * 100, 1),
        "initial_stop_exit_pct": round(sum(t.exit_reason == INITIAL_STOP_LOSS for t in trades) / n * 100, 1),
        "momentum_exit_pct": round(sum(t.exit_reason == TARGET_2_MOMENTUM_EXIT for t in trades) / n * 100, 1),
        "profit_after_target_1": round(sum(t.runner_profit for t in runners), 2),
    }


# ═══════════════════════════════════════════════════════════════════════════
#  THE SPEC'S SCENARIOS
# ═══════════════════════════════════════════════════════════════════════════

def _path(prices, spread=0.006):
    """Turn a close-only path into bars with a plausible high/low band."""
    return [Bar(high=round(p * (1 + spread), 2),
                low=round(p * (1 - spread), 2),
                close=p) for p in prices]


STRONG = Momentum(zpi=80, msi=75, cri=15, mri=20, rsi_14=66, macd_hist=0.6, volume_ratio=1.9)
WEAK = Momentum(zpi=22, msi=28, cri=78, mri=72, rsi_14=36, macd_hist=-0.4, volume_ratio=0.6)
NEUTRAL = Momentum(zpi=50, msi=50, cri=50, mri=50, rsi_14=50, macd_hist=0.0, volume_ratio=1.0)


def scenarios(cfg: dict = None, cost_pct: float = 0.25) -> dict:
    """The seven deterministic scenarios the specification asks for."""
    cfg = cfg or config()
    out = {}

    # 1. entry -> stop -> full exit, never reaching +3%
    out["1_stop_before_target"] = simulate(
        100.0, 100, _path([100, 99, 98.2, 97.4, 96.5]), NEUTRAL, cfg, cost_pct=cost_pct)

    # 2. +3% -> runner -> reversal -> trailing exit
    out["2_partial_then_reversal"] = simulate(
        100.0, 100, _path([100, 102, 104, 103, 101, 99, 97]), NEUTRAL, cfg, cost_pct=cost_pct)

    # 3. +3% -> +6% -> strong momentum -> +10% -> trailing exit
    out["3_runner_runs"] = simulate(
        100.0, 100, _path([100, 103, 106, 108, 111, 113, 110, 106]), STRONG, cfg, cost_pct=cost_pct)

    # 4. +3% -> +6% -> weak momentum -> banked at the checkpoint
    out["4_weak_at_checkpoint"] = simulate(
        100.0, 100, _path([100, 103, 106, 107, 105]), WEAK, cfg, cost_pct=cost_pct)

    # 5. never reaches +3%, drifts, then stops out
    out["5_drift_then_stop"] = simulate(
        100.0, 100, _path([100, 100.5, 101, 100.2, 99, 97.5, 96]), NEUTRAL, cfg, cost_pct=cost_pct)

    # 6. several users / positions at once, independent
    out["6_multiple_positions"] = [
        simulate(100.0, 100, _path([100, 103, 107, 112, 108]), STRONG, cfg, cost_pct=cost_pct, symbol="AAA"),
        simulate(250.0, 40, _path([250, 245, 240, 236]), WEAK, cfg, cost_pct=cost_pct, symbol="BBB"),
        simulate(50.0, 200, _path([50, 51.5, 53, 52, 50.5]), NEUTRAL, cfg, cost_pct=cost_pct, symbol="CCC"),
    ]

    # 7. a price gap straight through both targets
    out["7_gap_through_targets"] = simulate(
        100.0, 100, _path([100, 109, 112, 107]), STRONG, cfg, cost_pct=cost_pct)

    return out


def run(cfg: dict = None, cost_pct: float = 0.25) -> dict:
    """Run every scenario and summarise. Returns {'scenarios':…, 'metrics':…}."""
    sc = scenarios(cfg, cost_pct)
    flat: list[Trade] = []
    for v in sc.values():
        flat.extend(v if isinstance(v, list) else [v])
    return {"scenarios": sc, "metrics": metrics(flat), "cost_pct": cost_pct}


def print_report(cfg: dict = None, cost_pct: float = 0.25):
    r = run(cfg, cost_pct)
    print(f"\n{'=' * 72}")
    print(f"  Aggressive strategy — deterministic scenarios (costs {cost_pct}% round trip)")
    print(f"{'=' * 72}")
    for name, v in r["scenarios"].items():
        for t in (v if isinstance(v, list) else [v]):
            t1 = f"T1@{t.t1_price}" if t.t1_hit else "no T1"
            t2 = f"T2:{t.t2_verdict}" if t.t2_reached else "no T2"
            print(f"  {name:24s} {t.symbol:4s} {t1:11s} {t2:22s} "
                  f"exit {t.exit_reason:22s} P&L {t.realized:>9.2f}")
    print(f"\n  {'-' * 68}")
    for k, v in r["metrics"].items():
        print(f"  {k:26s} {v}")
    print()
    return r


if __name__ == "__main__":
    print_report()
