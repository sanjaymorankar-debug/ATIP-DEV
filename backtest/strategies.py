"""
Reference strategies on the backtest.strategy interface.

  dip           mean reversion: the close sits in the bottom zone of its recent
                range -- the entry rule of scores/backtest.py's "dip" mode,
                expressed as a Strategy so it runs at portfolio level
  atip_signal   ATIP's own BUY signals from ai_scores (point in time; see the
                ScoresHistory caveat in the bias report)
  buy_and_hold  buys the first max_positions symbols once and holds them --
                the control any strategy should be compared with
  momentum      (W23, QR-07) breakout to a new N-session high with a momentum and trend
                filter, ranked by momentum
  mean_reversion_rsi  (W23, QR-08) short-period RSI washout above the long average,
                exit on RSI recovery / stop / time

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


def _rsi(closes, n):
    if len(closes) < n + 1:
        return None
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(len(closes) - n, len(closes))]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(len(closes) - n, len(closes))]
    g, l_ = sum(gains) / n, sum(losses) / n
    return 100.0 if l_ == 0 else 100 - 100 / (1 + g / l_)


class MomentumBreakout(Strategy):
    """QR-07 (W23): entry-side momentum research. Buy a breakout to a new N-session
    high when the stock's medium-term momentum is strong and it trades above its long
    average; rank the day's candidates by momentum. Exits: stop, target, time."""
    strategy_id = "momentum"
    version = "1"
    default_params = {
        "breakout_lookback": 55,        # close above the highest high of the prior N sessions
        "momentum_lookback": 60,        # sessions for the momentum filter / ranking
        "min_momentum_pct": 10.0,       # N-session return at least this
        "trend_sma": 200,               # close above its SMA (0 = no filter)
        "min_avg_turnover": 5e7,
        "stop_pct": 7.0,
        "target_pct": 20.0,
        "max_hold_sessions": 40,
        "max_new_per_day": 3,
    }

    @property
    def warmup_bars(self):
        p = self.params
        return int(max(p["breakout_lookback"], p["momentum_lookback"], p["trend_sma"] or 0)) + 1

    def on_bar(self, ctx: StrategyContext) -> list:
        p = self.params
        need = self.warmup_bars
        cands = []
        for sym in ctx.universe:
            if sym in ctx.positions:
                continue
            hist = ctx.data.history(sym, need)
            if len(hist) < need or hist[-1].date != ctx.as_of:
                continue
            t = _avg_turnover(hist[-20:])
            if t is None or t < p["min_avg_turnover"]:
                continue
            closes = [b.close for b in hist]
            prior_high = max(b.high for b in hist[-1 - int(p["breakout_lookback"]):-1])
            if closes[-1] <= prior_high:
                continue
            m = int(p["momentum_lookback"])
            mom = (closes[-1] / closes[-1 - m] - 1) * 100 if closes[-1 - m] else None
            if mom is None or mom < p["min_momentum_pct"]:
                continue
            ts = int(p["trend_sma"] or 0)
            if ts and closes[-1] <= sum(closes[-ts:]) / ts:
                continue
            cands.append((-mom, sym, closes[-1], mom))
        cands.sort()
        return [Signal(sym, "BUY", stop_price=round(c * (1 - p["stop_pct"] / 100), 2),
                       target_price=round(c * (1 + p["target_pct"] / 100), 2),
                       max_hold_sessions=int(p["max_hold_sessions"]),
                       reason=f"{int(p['breakout_lookback'])}-session breakout, {int(p['momentum_lookback'])}-session "
                              f"momentum {mom:.1f}%")
                for _k, sym, c, mom in cands[:int(p["max_new_per_day"])]]


class MeanReversionRSI(Strategy):
    """QR-08 (W23): standalone mean-reversion research. Buy a short-term washout
    (RSI(n) below entry_rsi) in a stock that is above its long average; sell when RSI
    recovers above exit_rsi, on the stop, or after max_hold sessions."""
    strategy_id = "mean_reversion_rsi"
    version = "1"
    default_params = {
        "rsi_period": 2,
        "entry_rsi": 10.0,
        "exit_rsi": 70.0,
        "trend_sma": 200,
        "min_avg_turnover": 5e7,
        "stop_pct": 6.0,
        "max_hold_sessions": 7,
        "max_new_per_day": 5,
    }

    @property
    def warmup_bars(self):
        return int(max(self.params["trend_sma"] or 0, self.params["rsi_period"] + 1, 20)) + 1

    def on_bar(self, ctx: StrategyContext) -> list:
        p = self.params
        n = int(p["rsi_period"])
        out = []
        for sym in list(ctx.positions):
            hist = ctx.data.history(sym, n + 1)
            r = _rsi([b.close for b in hist], n)
            if r is not None and r > p["exit_rsi"]:
                out.append(Signal(sym, "SELL", reason=f"RSI({n}) {r:.0f} > {p['exit_rsi']:.0f}"))
        need = self.warmup_bars
        cands = []
        for sym in ctx.universe:
            if sym in ctx.positions:
                continue
            hist = ctx.data.history(sym, need)
            if len(hist) < need or hist[-1].date != ctx.as_of:
                continue
            t = _avg_turnover(hist[-20:])
            if t is None or t < p["min_avg_turnover"]:
                continue
            closes = [b.close for b in hist]
            ts = int(p["trend_sma"] or 0)
            if ts and closes[-1] <= sum(closes[-ts:]) / ts:
                continue
            r = _rsi(closes, n)
            if r is not None and r < p["entry_rsi"]:
                cands.append((r, sym, closes[-1]))
        cands.sort()
        out += [Signal(sym, "BUY", stop_price=round(c * (1 - p["stop_pct"] / 100), 2),
                       max_hold_sessions=int(p["max_hold_sessions"]), reason=f"RSI({n}) {r:.1f} washout above SMA")
                for r, sym, c in cands[:int(p["max_new_per_day"])]]
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


REGISTRY = {c.strategy_id: c for c in (DipMeanReversion, AtipSignal, BuyAndHold, MomentumBreakout,
                                       MeanReversionRSI)}


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
        if v["definition"].get("kind") == "option_overlay":         # W40 (ENT-15)
            raise ValueError(f"{strategy_id} is an option_overlay strategy: W2 simulates cash (and futures) "
                             f"positions, not option legs -- replay it on the stored daily option prices with "
                             f"strategy_engine.option_overlay.replay()")
        return DefinitionStrategy(v["definition"], params, registry.loader(conn))
    finally:
        conn.close()
