"""
A stored strategy version as a W2 backtest Strategy (backtest integration).

W2's engine calls on_bar(ctx) after each session's close with a point-in-time
view of prices; this adapter builds the same EvalEnv the live engine uses
from that view, asks the version's evaluator for decisions, and hands W2
Signals back:

    BUY                       -> Signal BUY (stop / target / max hold from the intent)
    EXIT                      -> Signal SELL (W2 closes the whole position)
    ADD    (held symbol)      -> Signal ADD (PF-06), quantity = the intent's quantity when the
                                 decision carries one (features.rebalance_qty: the portfolio
                                 kind's reweight, as strategy_engine.engine._quantity); else W2
                                 sizes it like a BUY within max_position_pct, as the W4 risk
                                 engine does. The decision's stop rides along for that sizing.
    REDUCE (held symbol)      -> Signal REDUCE (PF-06), quantity = the intent's quantity as for
                                 ADD; else W2 sells half the holding -- the W4 risk engine's
                                 REDUCE default (an intent carries no other size for it)
    HOLD / NO_ACTION / SELL   -> nothing (SELL on a symbol not held opens nothing)
    BLOCKED_BY_RISK           -> nothing
    SHORT  (symbol not held)  -> Signal SHORT (W40): W2 simulates it as a near-month stock
                                 future (backtest/futures.py), value = the intent's
                                 target_position_pct of equity at the decision close (W2 floors
                                 it to whole lots and caps it at max_position_pct, as the W4 risk
                                 engine's _futures_leg does); no target -> W2's default
                                 (max_position_pct). max_hold_sessions rides along.
    COVER  (held short)       -> Signal COVER (the whole short)
    ADD / REDUCE of a symbol not held (or held short), SHORT of a held symbol, COVER of one
    not held short, EXIT of a held short
                              -> nothing; counted in not_simulated ({action: n}), which the
                                 run's bias report states

Held shorts: W2's futures book reaches the evaluator as ctx.futures; they are merged into
`held` as {"qty": -shares, "short": True, ...} -- what the live engine passes
(strategy_engine/engine.py _held), so a pair / portfolio decides COVER for its short leg.
A pairs / portfolio definition with short_via_futures needs dv_fno (quant/derivatives_features)
to choose SHORT: when its inputs do not load the quant features and the engine running it
simulates futures legs (the W2 engine sets simulates_futures), the F&O flag alone is read
from fo_underlying_daily (_FnoFlags) -- the definition and its hash are unchanged. The
event-driven engine (BT-17) does not trade SHORT, so there the pair keeps its pre-W40 path
(the short leg is a SELL decision and the long leg is held back unless allow_single_leg).

Regime and benchmark series are read once from the database and only ever
consulted for dates on or before the session being decided, like prices.

A backtest records strategy_id, strategy_version and definition_hash in its
snapshot; W2 refuses to run a snapshot whose hash no longer matches.
"""

from __future__ import annotations

from datetime import date

from backtest.strategy import Signal, Strategy
from strategy_engine.decisions import A_ADD as ADD, A_BUY as BUY, A_COVER as COVER, A_EXIT as EXIT
from strategy_engine.decisions import A_REDUCE as REDUCE, A_SHORT as SHORT
from strategy_engine.definition import definition_hash, specs
from strategy_engine.kinds import EvalEnv, make_evaluator, session_ordinal
from strategy_engine.params import resolve


class _FnoFlags:
    """dv_fno alone: 1 when fo_underlying_daily holds the symbol that session (the meaning
    quant.derivatives_features.DerivHistory gives it), read once."""

    def __init__(self, conn):
        self._have = set()
        try:
            for d, s in conn.execute("SELECT date, symbol FROM fo_underlying_daily"):
                self._have.add((d if isinstance(d, date) else date.fromisoformat(str(d)[:10]), s))
        except Exception:
            pass

    def on(self, as_of, symbol) -> dict:
        key = {"NIFTY50": "NIFTY"}.get(symbol, symbol)
        return {"dv_fno": 1} if (as_of, key) in self._have else {}


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
        self.not_simulated = {}                         # action -> decisions W2 could not trade

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
            elif self.defn.get("short_via_futures") and getattr(self, "simulates_futures", False):
                self._quant = _FnoFlags(conn)           # W40: which legs are shortable via futures
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
        for s, f in (getattr(ctx, "futures", None) or {}).items():      # W40: short legs (futures)
            held.setdefault(s, {"qty": f.qty, "entry_price": f.entry_price, "held_sessions": f.held_sessions,
                                "short": True, "instrument": "FUT", "expiry": f.expiry})
        out = []
        for it in self._ev.decide(env, ctx.as_of, held, session_ordinal(ctx.as_of)):
            short = bool((held.get(it.symbol) or {}).get("short"))
            if it.action == BUY:
                out.append(Signal(it.symbol, "BUY", stop_price=it.stop_price, target_price=it.target_price,
                                  max_hold_sessions=it.max_hold_sessions, reason=it.reason[:200]))
            elif it.action == EXIT and it.symbol in held and not short:
                out.append(Signal(it.symbol, "SELL", reason=it.reason[:200]))
            elif it.action in (ADD, REDUCE) and it.symbol in held and not short:
                rq = (it.features or {}).get("rebalance_qty")
                out.append(Signal(it.symbol, it.action, stop_price=it.stop_price if it.action == ADD else None,
                                  quantity=int(rq) if rq else None, reason=it.reason[:200]))
            elif it.action == SHORT and it.symbol not in held:
                tgt, eq = it.target_position_pct, getattr(ctx, "equity", None)
                value = round(float(eq) * float(tgt) / 100, 2) if tgt and eq and eq > 0 else None
                out.append(Signal(it.symbol, "SHORT", value=value, max_hold_sessions=it.max_hold_sessions,
                                  reason=it.reason[:200]))
            elif it.action == COVER and short:
                out.append(Signal(it.symbol, "COVER", reason=it.reason[:200]))
            elif it.action in (ADD, REDUCE, SHORT, COVER) or (it.action == EXIT and short):
                key = ("SHORT (already held)" if it.action == SHORT else
                       "COVER (not held short)" if it.action == COVER else
                       f"{it.action} (held short)" if short else f"{it.action} (not held)")
                self.not_simulated[key] = self.not_simulated.get(key, 0) + 1
        return out

    def describe(self) -> dict:
        return {"strategy_id": self.strategy_id, "version": self.version, "params": dict(self.params),
                "kind": self.defn["kind"], "definition_hash": self.definition_hash,
                "warmup_bars": self.warmup_bars, "uses_scores": self.uses_scores,
                "position": self.defn.get("position"), "risk": self.defn.get("risk")}
