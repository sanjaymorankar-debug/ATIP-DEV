"""
Reference strategies on the backtest.strategy interface.

  dip           mean reversion: the close sits in the bottom zone of its recent
                range -- the entry rule of scores/backtest.py's "dip" mode,
                expressed as a Strategy so it runs at portfolio level
  atip_signal   ATIP's own BUY signals from ai_scores (point in time; see the
                ScoresHistory caveat in the bias report)
  buy_and_hold  buys the first max_positions symbols once and holds them --
                the control any strategy should be compared with

Register a new strategy by adding it to REGISTRY. None of this is advice; a
backtest result is a description of the past under stated assumptions.
"""

from __future__ import annotations

from backtest.strategy import Signal, Strategy, StrategyContext


def _avg_turnover(bars) -> float | None:
    if not bars:
        return None
    return sum(b.close * b.volume for b in bars) / len(bars)


class DipMeanReversion(Strategy):
    strategy_id = "dip"
    version = "1"
    default_params = {
        "range_lookback": 20,          # bars defining the recent range
        "dip_zone_pct": 25.0,          # enter when the close is in the bottom X% of it
        "min_avg_turnover": 5e7,       # Rs/day over range_lookback (scores/backtest.py MIN_TURNOVER_CR=5)
        "stop_pct": 3.0,
        "target_pct": 6.0,
        "max_hold_sessions": 10,
        "max_new_per_day": 5,
    }

    @property
    def warmup_bars(self):
        return int(self.params["range_lookback"]) + 1

    def on_bar(self, ctx: StrategyContext) -> list:
        p = self.params
        n = int(p["range_lookback"])
        cands = []
        for sym in ctx.universe:
            if sym in ctx.positions:
                continue
            hist = ctx.data.history(sym, n)
            if len(hist) < n or hist[-1].date != ctx.as_of:
                continue                              # needs a bar today and a full window
            turnover = _avg_turnover(hist)
            if turnover is None or turnover < p["min_avg_turnover"]:
                continue
            hi, lo = max(b.high for b in hist), min(b.low for b in hist)
            if hi <= lo:
                continue
            pos = (hist[-1].close - lo) / (hi - lo) * 100
            if pos <= p["dip_zone_pct"]:
                cands.append((pos, sym, hist[-1].close))
        cands.sort()                                  # deepest in its range first; ties by symbol
        out = []
        for pos, sym, close in cands[:int(p["max_new_per_day"])]:
            out.append(Signal(sym, "BUY",
                              stop_price=round(close * (1 - p["stop_pct"] / 100), 2),
                              target_price=round(close * (1 + p["target_pct"] / 100), 2),
                              max_hold_sessions=int(p["max_hold_sessions"]),
                              reason=f"close at {pos:.0f}% of its {n}-bar range"))
        return out


class AtipSignal(Strategy):
    strategy_id = "atip_signal"
    version = "1"
    uses_scores = True
    warmup_bars = 0
    default_params = {
        "min_zpi": None,               # optional extra filters on the BUY rows
        "max_cri": None,
        "stop_pct": 3.0,
        "target_pct": 6.0,
        "max_hold_sessions": 10,
        "max_new_per_day": 5,
        "exit_on_sell_signal": True,
    }

    def on_bar(self, ctx: StrategyContext) -> list:
        p = self.params
        rows = ctx.scores.on(ctx.as_of) if ctx.scores else {}
        out = []
        if p["exit_on_sell_signal"]:
            out += [Signal(s, "SELL", reason="ai_scores SELL") for s in ctx.positions
                    if (rows.get(s) or {}).get("signal") == "SELL"]
        buys = []
        for sym, r in rows.items():
            if r.get("signal") != "BUY" or sym in ctx.positions or sym not in ctx.universe:
                continue
            if p["min_zpi"] is not None and (r.get("zpi") or 0) < p["min_zpi"]:
                continue
            if p["max_cri"] is not None and (r.get("cri") or 100) > p["max_cri"]:
                continue
            bar = ctx.data.bar(sym)
            if not bar:
                continue
            buys.append((-(r.get("atip_score") or 0), sym, bar.close))
        buys.sort()
        for _s, sym, close in buys[:int(p["max_new_per_day"])]:
            out.append(Signal(sym, "BUY", stop_price=round(close * (1 - p["stop_pct"] / 100), 2),
                              target_price=round(close * (1 + p["target_pct"] / 100), 2),
                              max_hold_sessions=int(p["max_hold_sessions"]), reason="ai_scores BUY"))
        return out


class BuyAndHold(Strategy):
    strategy_id = "buy_and_hold"
    version = "1"
    default_params = {"stop_pct": 99.0}   # effectively no stop: sizing still needs one

    def on_bar(self, ctx: StrategyContext) -> list:
        if ctx.positions:
            return []
        out = []
        for sym in ctx.universe:
            bar = ctx.data.bar(sym)
            if bar:
                out.append(Signal(sym, "BUY", stop_price=round(bar.close * (1 - self.params["stop_pct"] / 100), 2),
                                  reason="buy and hold"))
        return out


REGISTRY = {c.strategy_id: c for c in (DipMeanReversion, AtipSignal, BuyAndHold)}


def make_strategy(strategy_id: str, params: dict | None = None, version: str | None = None) -> Strategy:
    """
    A code strategy from REGISTRY (no version given), or a stored strategy
    version from the W3 strategy registry (strategy_engine) -- the current
    version when only the id is known to the registry.
    """
    if version is None and strategy_id in REGISTRY:
        return REGISTRY[strategy_id](**(params or {}))
    from db.schema import get_connection
    from strategy_engine import registry
    from strategy_engine.adapter import DefinitionStrategy
    conn = get_connection()
    try:
        v = registry.get_version(conn, strategy_id, version)
        if not v:
            known = sorted(set(REGISTRY) | {s["strategy_id"] for s in registry.list_strategies(conn)})
            raise ValueError(f"unknown strategy {strategy_id!r}" + (f" version {version}" if version else "")
                             + f"; known: {known}")
        return DefinitionStrategy(v["definition"], params, registry.loader(conn))
    finally:
        conn.close()
