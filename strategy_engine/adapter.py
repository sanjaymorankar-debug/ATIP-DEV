"""
A stored strategy version as a W2 backtest Strategy (backtest integration).

W2's engine calls on_bar(ctx) after each session's close with a point-in-time
view of prices; this adapter builds the same EvalEnv the live engine uses
from that view, asks the version's evaluator for decisions, and hands W2
Signals back:

    BUY                       -> Signal BUY (stop / target / max hold from the intent)
    EXIT                      -> Signal SELL (W2 closes the whole position)
    HOLD / NO_ACTION / SELL   -> nothing (SELL on a symbol not held opens nothing)
    REDUCE / ADD              -> nothing: W2 trades whole positions, so a partial
                                 change is not simulated (stated in the run's bias report)
    BLOCKED_BY_RISK           -> nothing

Regime and benchmark series are read once from the database and only ever
consulted for dates on or before the session being decided, like prices.

A backtest records strategy_id, strategy_version and definition_hash in its
snapshot; W2 refuses to run a snapshot whose hash no longer matches.
"""

from __future__ import annotations

from datetime import date

from backtest.strategy import Signal, Strategy
from strategy_engine.decisions import A_BUY as BUY, A_EXIT as EXIT
from strategy_engine.definition import definition_hash, specs
from strategy_engine.kinds import EvalEnv, make_evaluator, session_ordinal
from strategy_engine.params import resolve


class DefinitionStrategy(Strategy):
    default_params = {}

    def __init__(self, defn: dict, params: dict | None = None, loader=None):
        self.defn = defn
        self.strategy_id = defn["strategy_id"]
        self.version = defn["version"]
        self.definition_hash = definition_hash(defn)
        self.params = resolve(specs(defn), params)       # validated overrides (ParamError on bad ones)
        self.default_params = {s.name: s.default for s in specs(defn)}
        self._loader = loader
        self.uses_scores = "scores" in self._inputs()
        self.warmup_bars = 260                          # enough for 250-bar features
        self._ev = None
        self._regime = self._bench = None

    def _inputs(self) -> set:
        ins = set(self.defn.get("inputs") or [])
        if self.defn["kind"] in ("composite", "python"):
            ins |= {"scores", "regime", "benchmark", "ml", "quant"}    # members / code may use any
        return ins

    def _lazy(self):
        if self._ev is not None:
            return
        from db.schema import get_connection
        from strategy_engine import registry
        from strategy_engine.regime import get_provider
        conn = get_connection()
        try:
            load = self._loader or registry.loader(conn)
            if self.defn["kind"] == "composite":        # resolve members now, while the connection is open
                defs = {}
                def cached(sid, ver):
                    if (sid, ver) not in defs:
                        defs[(sid, ver)] = load(sid, ver)
                    return defs[(sid, ver)]
                load = cached
            self._ev = make_evaluator(self.defn, self.params, load)
            ins = self._inputs()
            self._regime = get_provider(conn) if "regime" in ins else None
            self._ml = None
            if "ml" in ins:           # W5: only predictions made out of sample (after the model's training end)
                from ml.strategy_features import MLPredictionHistory
                self._ml = MLPredictionHistory(conn)
            self._quant = None
            if "quant" in ins:        # W6: factor / composite scores and events stored per date
                from quant.strategy_features import QuantHistory
                self._quant = QuantHistory(conn)
            self._bench = {}
            if "benchmark" in ins:
                for d, c in conn.execute("SELECT date, close FROM prices_daily WHERE symbol='NIFTY50'"):
                    self._bench[d if isinstance(d, date) else date.fromisoformat(str(d)[:10])] = c
        finally:
            conn.close()

    def on_bar(self, ctx) -> list:
        self._lazy()
        hist = ctx.data._h                              # PriceHistory behind the point-in-time view
        env = EvalEnv(hist, ctx.universe, ctx.scores, self._regime, self._bench, getattr(self, "_ml", None),
                      getattr(self, "_quant", None))
        held = {s: {"qty": p.qty, "entry_price": p.entry_price, "held_sessions": p.held_sessions}
                for s, p in ctx.positions.items()}
        out = []
        for it in self._ev.decide(env, ctx.as_of, held, session_ordinal(ctx.as_of)):
            if it.action == BUY:
                out.append(Signal(it.symbol, "BUY", stop_price=it.stop_price, target_price=it.target_price,
                                  max_hold_sessions=it.max_hold_sessions, reason=it.reason[:200]))
            elif it.action == EXIT and it.symbol in held:
                out.append(Signal(it.symbol, "SELL", reason=it.reason[:200]))
        return out

    def describe(self) -> dict:
        return {"strategy_id": self.strategy_id, "version": self.version, "params": dict(self.params),
                "kind": self.defn["kind"], "definition_hash": self.definition_hash,
                "warmup_bars": self.warmup_bars, "uses_scores": self.uses_scores,
                "position": self.defn.get("position"), "risk": self.defn.get("risk")}
