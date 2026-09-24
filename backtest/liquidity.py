"""
Liquidity constraint (BT-07): stop a backtest assuming fills the market could
not have given. Deliberately basic -- it prevents the obvious, it does not
simulate the order book (that is W6).

For each fill:
  * the order may be at most max_participation_pct of the bar's traded volume;
  * the symbol's average daily turnover (close x volume over adv_lookback bars
    BEFORE the fill day -- never including it) must be at least
    min_avg_turnover rupees, when that is set;
  * a bar with no volume cannot fill at all.

on_breach: "cap" fills what the participation limit allows (the rest is
dropped); "reject" refuses the order.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class LiquidityRule:
    max_participation_pct: float | None = 10.0
    min_avg_turnover: float | None = None
    on_breach: str = "cap"
    adv_lookback: int = 20

    def __post_init__(self):
        if self.on_breach not in ("cap", "reject"):
            raise ValueError("on_breach must be 'cap' or 'reject'")

    def allowed_quantity(self, qty: int, bar_volume: float | None,
                         avg_turnover: float | None) -> tuple[int, str | None]:
        """(quantity that may fill, reason when less than asked)."""
        if qty <= 0:
            return 0, "no quantity"
        if not bar_volume or bar_volume <= 0:
            return 0, "no volume traded on the fill bar"
        if self.min_avg_turnover is not None:
            if avg_turnover is None:
                return 0, "average turnover unknown (not enough prior bars)"
            if avg_turnover < self.min_avg_turnover:
                return 0, f"average turnover {avg_turnover:,.0f} below {self.min_avg_turnover:,.0f}"
        if self.max_participation_pct is not None:
            cap = int(bar_volume * self.max_participation_pct / 100)
            if qty > cap:
                if self.on_breach == "reject" or cap < 1:
                    return 0, f"{qty} shares exceed {self.max_participation_pct}% of volume {bar_volume:,.0f}"
                return cap, f"capped to {cap} ({self.max_participation_pct}% of volume {bar_volume:,.0f})"
        return qty, None

    def as_dict(self) -> dict:
        return asdict(self)


def liquidity_from_config(cfg: dict | None = None) -> LiquidityRule:
    if cfg is None:
        from backtest.config import backtest_config
        cfg = backtest_config()
    l = cfg.get("liquidity") or {}
    return LiquidityRule(max_participation_pct=l.get("max_participation_pct", 10.0),
                         min_avg_turnover=l.get("min_avg_turnover"),
                         on_breach=l.get("on_breach", "cap"),
                         adv_lookback=int(l.get("adv_lookback", 20)))
