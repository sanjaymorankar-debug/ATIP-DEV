"""
Slippage (BT-06): the price a backtest fill gets, relative to the reference
price the engine would otherwise use (the open for a queued order, the stop
or target for a protective exit). Always against the trade.

  none   fill at the reference price
  fixed  value = rupees per share
  pct    value = basis points of the reference price (5.0 = 0.05%) -- the
         paper broker's model (orders/paper.py paper_slippage_bps), and the default

Market-impact models that grow with order size belong to Advanced Quant (W6).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

KINDS = ("none", "fixed", "pct")


@dataclass(frozen=True)
class SlippageModel:
    kind: str = "pct"
    value: float = 5.0

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"slippage kind must be one of {KINDS}, not {self.kind!r}")
        if self.value < 0:
            raise ValueError("slippage value cannot be negative")

    def fill_price(self, side: str, ref_price: float) -> float:
        """BUY pays more, SELL receives less. Never below 0.01."""
        side = side.upper()
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, not {side!r}")
        if self.kind == "none" or self.value == 0:
            return round(ref_price, 4)
        slip = self.value if self.kind == "fixed" else ref_price * self.value / 10000
        px = ref_price + slip if side == "BUY" else ref_price - slip
        return round(max(px, 0.01), 4)

    def as_dict(self) -> dict:
        return asdict(self)


def slippage_from_config(cfg: dict | None = None) -> SlippageModel:
    if cfg is None:
        from backtest.config import backtest_config
        cfg = backtest_config()
    s = cfg.get("slippage") or {}
    return SlippageModel(kind=s.get("kind", "pct"), value=float(s.get("value", 5.0)))
