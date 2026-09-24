"""
The Strategy interface: the only thing the engine knows about a strategy.

A strategy is called once per session, after the close, with a
StrategyContext. It returns Signals; the engine does everything else --
sizing, queueing for the next open, costs, slippage, liquidity, accounting,
protective exits. No strategy-specific logic lives in the engine, and no
engine logic in a strategy.

    class MyStrategy(Strategy):
        strategy_id = "my_strategy"
        version = "1"
        warmup_bars = 50
        def on_bar(self, ctx):
            return [Signal("RELIANCE", "BUY", stop_price=..., reason="...")]

A strategy must be deterministic given (params, data): no clock, no random
state, no reads outside ctx. That is what makes a run reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class Signal:
    """
    BUY opens a position (ignored when one is already held or pending);
    SELL closes the whole position. Orders are filled at the next session's
    open. stop_price / target_price become protective exits checked on every
    bar from the fill; max_hold_sessions closes the position at the next open
    after that many sessions. Missing stop -> the configured default_stop_pct.
    """
    symbol: str
    side: str
    stop_price: float | None = None
    target_price: float | None = None
    max_hold_sessions: int | None = None
    reason: str = ""

    def __post_init__(self):
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"Signal side must be BUY or SELL, not {self.side!r}")


@dataclass(frozen=True)
class PositionView:
    symbol: str
    qty: int
    entry_price: float
    entry_date: date
    held_sessions: int


@dataclass
class StrategyContext:
    as_of: date                       # the session just closed
    data: object                      # backtest.data.PointInTimeView
    universe: tuple                   # symbols the strategy may trade
    positions: dict                   # symbol -> PositionView
    cash: float
    equity: float
    params: dict = field(default_factory=dict)
    scores: object | None = None      # backtest.data.ScoresHistory when the strategy asked for it


class Strategy:
    strategy_id: str = "base"
    version: str = "0"
    warmup_bars: int = 0
    uses_scores: bool = False         # load ai_scores (point in time) into ctx.scores
    default_params: dict = {}

    def __init__(self, **params):
        unknown = set(params) - set(self.default_params)
        if unknown:
            raise ValueError(f"{self.strategy_id}: unknown parameters {sorted(unknown)}")
        self.params = {**self.default_params, **params}

    def on_bar(self, ctx: StrategyContext) -> list:
        raise NotImplementedError

    def describe(self) -> dict:
        return {"strategy_id": self.strategy_id, "version": self.version, "params": dict(self.params),
                "warmup_bars": self.warmup_bars, "uses_scores": self.uses_scores}
