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

Signals: BUY (open), SELL (close all), and for a held position ADD (increase)
and REDUCE (partial exit) -- see Signal. SHORT / COVER open and close a short leg, which
the W2 engine simulates as a near-month stock future (backtest/futures.py, W40).

A strategy must be deterministic given (params, data): no clock, no random
state, no reads outside ctx. That is what makes a run reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


SIDES = ("BUY", "SELL", "ADD", "REDUCE", "SHORT", "COVER")


@dataclass(frozen=True)
class Signal:
    """
    BUY opens a position (ignored when one is already held or pending);
    SELL closes the whole position. Orders are filled at the next session's
    open. stop_price / target_price become protective exits checked on every
    bar from the fill; max_hold_sessions closes the position at the next open
    after that many sessions. Missing stop -> the configured default_stop_pct.

    Partial changes to a HELD position (PF-06; the engine's module docstring has
    the full rules). Both fill at the next session's open like BUY / SELL:

      ADD     increase it by `quantity` shares, or by `value` rupees (whole shares
              at the decision close), or -- neither given -- by what a BUY would be
              sized at (stop_price, else default_stop_pct). Capped so the position
              stays within sizing.max_position_pct of equity. Ignored (an event)
              for a symbol not held. stop / target / max hold of the position are
              not changed by an ADD.
      REDUCE  sell `quantity` shares, or `fraction` (0 < f <= 1) of the held
              quantity, or -- neither given -- half of it (the W4 risk engine's
              REDUCE default). At or above the held quantity it closes the position.

    Short legs (W40, QR-05 / QR-06; the W2 engine simulates them as near-month stock
    FUTURES -- backtest/futures.py has the rules):

      SHORT   open a short of `quantity` shares or `value` rupees of notional, rounded DOWN
              to whole lots of the near-month future at the decision close; neither given ->
              sizing.max_position_pct of equity. Capped at max_position_pct. Ignored while
              the symbol is held (long or short). max_hold_sessions applies; stop_price /
              target_price do not (no protective stops on futures legs).
      COVER   buy the whole short back. Ignored (an event) when the symbol is not held short.

    BUY / SELL take none of quantity / fraction / value, so every existing
    Signal means exactly what it did.
    """
    symbol: str
    side: str
    stop_price: float | None = None
    target_price: float | None = None
    max_hold_sessions: int | None = None
    reason: str = ""
    quantity: int | None = None       # ADD / REDUCE: shares
    fraction: float | None = None     # REDUCE: share of the held quantity, 0 < f <= 1
    value: float | None = None        # ADD: rupees, converted at the decision close

    def __post_init__(self):
        if self.side not in SIDES:
            raise ValueError(f"Signal side must be one of {SIDES}, not {self.side!r}")
        given = [k for k in ("quantity", "fraction", "value") if getattr(self, k) is not None]
        if self.side in ("BUY", "SELL", "COVER") and given:
            raise ValueError(f"{self.side} trades a whole position: {given[0]} applies to ADD / REDUCE only")
        if self.side == "SHORT" and (self.stop_price is not None or self.target_price is not None):
            raise ValueError("SHORT takes no stop_price / target_price: futures legs have no protective stops")
        if len(given) > 1:
            raise ValueError(f"give at most one of quantity / fraction / value, not {given}")
        q = self.quantity
        if q is not None and (isinstance(q, bool) or not isinstance(q, int) or q < 1):
            raise ValueError(f"quantity must be a whole number of shares >= 1, not {q!r}")
        if self.fraction is not None:
            if self.side != "REDUCE":
                raise ValueError("fraction applies to REDUCE only")
            if not 0 < float(self.fraction) <= 1:
                raise ValueError(f"fraction must be within (0, 1], not {self.fraction!r}")
        if self.value is not None:
            if self.side not in ("ADD", "SHORT"):
                raise ValueError("value applies to ADD / SHORT only")
            if not float(self.value) > 0:
                raise ValueError(f"value must be positive, not {self.value!r}")


@dataclass(frozen=True)
class PositionView:
    symbol: str
    qty: int
    entry_price: float
    entry_date: date
    held_sessions: int


@dataclass(frozen=True)
class FuturesView:
    """A short futures leg the strategy holds (W40): qty is negative shares (lots x lot size)."""
    symbol: str
    qty: int
    entry_price: float
    entry_date: date
    held_sessions: int
    lots: int = 0
    lot_size: int = 0
    expiry: date | None = None
    short: bool = True


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
    futures: dict = field(default_factory=dict)   # symbol -> FuturesView: short legs held (W40)


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
