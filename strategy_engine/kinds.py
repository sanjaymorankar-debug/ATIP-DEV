"""
The strategy kinds: generic evaluators that turn a definition + features into
position intents for a universe on one date. A new strategy is a new
definition of an existing kind; a new KIND is the only thing that needs code.

Every evaluator implements decide(env, as_of, held, session_index) and returns
StrategyDecisions (decisions.py) whose action is BUY / ADD / HOLD / REDUCE /
EXIT / SELL / NO_ACTION / BLOCKED_BY_RISK -- the engine's BUY / SELL / HOLD /
WAIT / NO_TRADE decision is derived from the action. `held` is {symbol: {"qty", "entry_price", "held_sessions"}}.

rule (SE-03)
  Four rule types, each a condition tree (rules.py):
    entry         required; when a new position is wanted
    exit          optional; when a held position is closed
    confirmation  optional; must ALSO hold for an entry to become a BUY --
                  entry met but not confirmed -> NO_ACTION "awaiting confirmation"
    filter        optional; symbols failing it are not considered for entry
                  (held positions are still managed, so exits are never filtered)
  rank_by: feature (optional).
  held:     exit met, or held_sessions >= max_hold_sessions -> EXIT; else HOLD
  not held: filter passes AND entry met AND confirmation met -> BUY candidate;
            confidence = leaf conditions met / leaf conditions in the entry
            (+ confirmation) trees
  candidates ranked by rank_by (desc) else confidence, then symbol; the first
  max_new_per_day that fit under max_positions are BUY, the rest NO_ACTION.

multi_factor (SE-06)
  factors: [{feature, weight, direction higher|lower, min, max, threshold}]
  factor score s = clamp((x - min) / (max - min) x 100, 0, 100), min/max default
  0/100 (ATIP scores are already 0-100); direction lower -> 100 - s.
  composite = sum(w x s) / sum(w) over the factors that have a value, required
  to cover at least min_factor_coverage (default 0.6) of the total weight.
  confirmations = factors with s >= their threshold (default 50).
  not held: composite >= entry_threshold AND confirmations >= min_confirmations
            (default 1) AND filter -> BUY candidate, ranked by composite
  held:     composite < exit_threshold -> EXIT; < reduce_threshold -> REDUCE;
            >= add_threshold -> ADD; else HOLD      (each threshold optional)
  confidence = composite / 100.

quant_rank (SE-04)
  score = sum(weight x feature) over score.terms (all must be present),
  ranked asc|desc among symbols passing `filter`. Every rebalance_every
  sessions (default 1; counted from 2020-01-01 so live and backtest agree):
    held and rank > exit_rank (default 2 x top_n) -> EXIT
    not held and rank <= top_n                   -> BUY (limits apply)
  other sessions: held -> HOLD. confidence = 1 - (rank - 1) / eligible.

composite (SE-07 / SE-08)
  members: [{strategy_id, version, weight}] evaluated with their own definitions.
  vote       BUY when >= min_agree members BUY; held: EXIT when >= min_agree_exit
             (default 1) members EXIT; confidence = agreeing / members
  weighted   BUY when sum(w x confidence of BUYing members) / sum(w) >= entry_threshold
             (0..1); held: EXIT when the weighted EXIT share >= exit_threshold (default 0.5)
  priority   per symbol, the first member (in order) with a decision other than
             NO_ACTION decides
  regime_select  the members listed for the current regime (regime_key, default
             market_trend) decide, in priority order; regime_map may have a
             "default"; with no member for the regime, held positions HOLD
             (EXIT when exit_on_unmapped_regime is true)

python
  a W2 code strategy (backtest.strategies.REGISTRY) run through the same
  point-in-time view; its Signals become intents (BUY; SELL of a held symbol
  -> EXIT).
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

from strategy_engine import rules as R
from strategy_engine.decisions import A_ADD as ADD, A_BUY as BUY, A_EXIT as EXIT, A_HOLD as HOLD
from strategy_engine.decisions import A_NONE as NO_ACTION, A_REDUCE as REDUCE, A_SELL as SELL
from strategy_engine.decisions import StrategyDecision, risk_gate
from strategy_engine.features import FeatureContext
from strategy_engine.params import value as pval

EPOCH = date(2020, 1, 1)


@lru_cache(maxsize=4096)
def session_ordinal(d: date) -> int:
    """Trading sessions from EPOCH to d (inclusive of d if it is a session)."""
    from utils.trading_calendar import is_trading_day
    n, x = 0, EPOCH
    while x <= d:
        if is_trading_day(x):
            n += 1
        x += timedelta(days=1)
    return n


class EvalEnv:
    """
    Point-in-time access for evaluators. Built from W2's PriceHistory, so the
    same object serves live decisions (engine.py) and backtests (adapter.py).
    Everything it returns is dated on or before the as_of it is asked about.
    """

    def __init__(self, history, universe, scores=None, regime=None, benchmark=None, ml=None):
        self.history, self.universe = history, tuple(universe)
        self.scores, self.regime = scores, regime
        self.ml = ml                  # ml/strategy_features.MLPredictionHistory or None
        self.benchmark = benchmark or {}
        self._ctx = {}

    def market(self, as_of):
        return self.regime.on(as_of) if self.regime else {}

    def context(self, symbol, as_of) -> FeatureContext | None:
        key = (symbol, as_of)
        if key in self._ctx:
            return self._ctx[key]
        bars = self.history.view(as_of).history(symbol)
        if not bars or bars[-1].date != as_of:
            self._ctx[key] = None                 # no bar on the decision date: no decision
            return None
        prev_date = bars[-2].date if len(bars) > 1 else None
        rows = self.scores.on(as_of) if self.scores else {}
        bench = {d: c for d, c in self.benchmark.items() if d <= as_of}
        ctx = FeatureContext(symbol, as_of, bars, rows.get(symbol, {}), self.market(as_of), bench,
                             previous=(lambda: self.context(symbol, prev_date)) if prev_date else None,
                             ml=self.ml.on(as_of, symbol) if self.ml else None)
        self._ctx[key] = ctx
        return ctx


def _features_snapshot(ctx, names):
    out = {}
    for n in names:
        v = ctx.get(n)
        out[n] = round(v, 6) if isinstance(v, float) else v
    return out


class Evaluator:
    def __init__(self, defn: dict, params: dict, loader=None, depth: int = 0):
        self.defn, self.params = defn, params
        self.loader, self.depth = loader, depth
        self.pos = {k: pval(v, params) for k, v in (defn.get("position") or {}).items()}
        self.risk = {k: pval(v, params) for k, v in (defn.get("risk") or {}).items()}

    # shared helpers ------------------------------------------------------
    def intent(self, symbol, as_of, action, confidence, reason, ctx=None, entry=False):
        """A StrategyDecision for one symbol (named `intent` for the evaluators'
        readability; the PositionIntent itself is built from it in engine.py)."""
        close = ctx.get("close") if ctx else None
        stop = target = None
        if entry and close:
            if self.pos.get("stop_pct"):
                stop = round(close * (1 - self.pos["stop_pct"] / 100), 2)
            if self.pos.get("target_pct"):
                target = round(close * (1 + self.pos["target_pct"] / 100), 2)
        feats = _features_snapshot(ctx, self.defn.get("features_used", [])) if ctx else {}
        if close is not None:
            feats.setdefault("close", close)
        market = ctx.market if ctx else {}
        it = StrategyDecision(
            strategy_id=self.defn["strategy_id"], strategy_version=self.defn["version"], symbol=symbol,
            as_of=as_of, action=action,
            target_position_pct=(self.pos.get("target_position_pct") if action in (BUY, ADD, HOLD) else
                                 0.0 if action == EXIT else None),
            confidence=None if confidence is None else round(max(0.0, min(1.0, confidence)), 4),
            reasons=[reason[:1000]], regime=market.get("regime"), parameters=dict(self.params),
            risk_requirement=self.risk.get("requirement", "STANDARD"),
            stop_price=stop, target_price=target, max_hold_sessions=self.pos.get("max_hold_sessions"),
            features=feats,
            signal_source=f"{self.defn['kind']}:{self.defn['strategy_id']}@{self.defn['version']}")
        return risk_gate(it, self.risk, feats)

    @staticmethod
    def _coded(dec, *codes):
        dec.reason_codes += list(codes)
        return dec

    def limit_buys(self, candidates, held_count):
        """candidates: [(sort_key, symbol, intent)] best first. Applies
        max_new_per_day and max_positions; the rest become NO_ACTION."""
        max_new = self.pos.get("max_new_per_day")
        max_pos = self.pos.get("max_positions")
        out, taken = [], 0
        for _k, _s, it in sorted(candidates, key=lambda c: (c[0], c[1])):
            if it.action != BUY:
                out.append(it); continue
            room = (max_pos is None or held_count + taken < max_pos) and (max_new is None or taken < max_new)
            if room:
                taken += 1
                out.append(it)
            else:
                it.action = NO_ACTION
                it.reason_codes.append("POSITION_LIMIT")
                it.reasons = it.reasons + ["entry conditions met but position limits reached "
                                           "(max_positions / max_new_per_day)"]
                it.target_position_pct = None
                out.append(it)
        return out

    def max_hold_exit(self, h):
        mh = self.pos.get("max_hold_sessions")
        return mh is not None and h.get("held_sessions", 0) >= mh

    def decide(self, env, as_of, held, session_index=None) -> list:
        raise NotImplementedError


class RuleEvaluator(Evaluator):
    def decide(self, env, as_of, held, session_index=None):
        out, cands = [], []
        for sym in env.universe:
            ctx = env.context(sym, as_of)
            if ctx is None:
                continue
            if sym in held:
                if self.max_hold_exit(held[sym]):
                    out.append(self._coded(self.intent(sym, as_of, EXIT, 1.0, "max hold reached", ctx), "MAX_HOLD")); continue
                if self.defn.get("exit") is not None:
                    met, t, n, tr = R.evaluate(self.defn["exit"], ctx, self.params)
                    if met:
                        out.append(self.intent(sym, as_of, EXIT, t / n if n else 1.0, "exit: " + "; ".join(tr), ctx))
                        continue
                out.append(self.intent(sym, as_of, HOLD, None, "held; exit conditions not met", ctx))
                continue
            if self.defn.get("filter") is not None and not R.evaluate(self.defn["filter"], ctx, self.params)[0]:
                continue                               # filtered out of the entry universe
            met, t, n, tr = R.evaluate(self.defn["entry"], ctx, self.params)
            if not met:
                continue                               # NO_ACTION, not reported per symbol
            why = "entry: " + "; ".join(tr)
            if self.defn.get("confirmation") is not None:
                cmet, ct, cn, ctr = R.evaluate(self.defn["confirmation"], ctx, self.params)
                t, n = t + ct, n + cn
                why += " | confirmation: " + "; ".join(ctr)
                if not cmet:
                    wait = self.intent(sym, as_of, NO_ACTION, t / n if n else None,
                                       why + " | entry met, awaiting confirmation", ctx)
                    wait.reason_codes += ["ENTRY_RULES_MET", "AWAITING_CONFIRMATION"]
                    out.append(wait)
                    continue
            conf = t / n if n else 1.0
            it = self.intent(sym, as_of, BUY, conf, why, ctx, entry=True)
            rb = self.defn.get("rank_by")
            rv = ctx.get(rb) if rb else None
            key = (-(rv if rv is not None else float("-inf")) if rb else -conf)
            cands.append((key, sym, it))
        return out + self.limit_buys(cands, len(held))


class MultiFactorEvaluator(Evaluator):
    def composite(self, ctx):
        tot_w = have_w = acc = 0.0
        confirms, parts = 0, []
        for f in self.defn["factors"]:
            w = float(pval(f.get("weight", 1), self.params))
            tot_w += w
            x = ctx.get(f["feature"])
            if x is None or isinstance(x, str):
                parts.append(f"{f['feature']}=missing"); continue
            lo, hi = float(pval(f.get("min", 0), self.params)), float(pval(f.get("max", 100), self.params))
            s = max(0.0, min(100.0, (x - lo) / (hi - lo) * 100)) if hi > lo else 50.0
            if f.get("direction", "higher") == "lower":
                s = 100 - s
            if s >= float(pval(f.get("threshold", 50), self.params)):
                confirms += 1
            have_w += w; acc += w * s
            parts.append(f"{f['feature']}={x:.4g}->{s:.0f}")
        coverage = float(pval(self.defn.get("min_factor_coverage", 0.6), self.params))
        if not tot_w or have_w / tot_w < coverage:
            return None, confirms, parts
        return acc / have_w, confirms, parts

    def decide(self, env, as_of, held, session_index=None):
        d, p = self.defn, self.params
        entry_t = float(pval(d["entry_threshold"], p))
        exit_t = pval(d.get("exit_threshold"), p)
        reduce_t = pval(d.get("reduce_threshold"), p)
        add_t = pval(d.get("add_threshold"), p)
        min_conf = int(pval(d.get("min_confirmations", 1), p))
        out, cands = [], []
        for sym in env.universe:
            ctx = env.context(sym, as_of)
            if ctx is None:
                continue
            comp, confirms, parts = self.composite(ctx)
            why = f"composite {comp:.1f}" if comp is not None else "composite: too few factors"
            why += f", {confirms} confirmations; " + ", ".join(parts)
            if sym in held:
                if self.max_hold_exit(held[sym]):
                    out.append(self._coded(self.intent(sym, as_of, EXIT, None, "max hold reached; " + why, ctx), "MAX_HOLD")); continue
                if comp is None:
                    out.append(self.intent(sym, as_of, HOLD, None, why, ctx)); continue
                conf = comp / 100
                if exit_t is not None and comp < float(exit_t):
                    out.append(self.intent(sym, as_of, EXIT, conf, f"below exit {exit_t}: " + why, ctx))
                elif reduce_t is not None and comp < float(reduce_t):
                    out.append(self.intent(sym, as_of, REDUCE, conf, f"below reduce {reduce_t}: " + why, ctx))
                elif add_t is not None and comp >= float(add_t):
                    out.append(self.intent(sym, as_of, ADD, conf, f"above add {add_t}: " + why, ctx, entry=True))
                else:
                    out.append(self.intent(sym, as_of, HOLD, conf, why, ctx))
                continue
            if comp is None or comp < entry_t or confirms < min_conf:
                continue
            if d.get("filter") and not R.evaluate(d["filter"], ctx, p)[0]:
                continue
            it = self.intent(sym, as_of, BUY, comp / 100, f"above entry {entry_t}: " + why, ctx, entry=True)
            cands.append((-comp, sym, it))
        top_n = pval(d.get("top_n"), p)
        if top_n is not None:
            cands = sorted(cands)[:int(top_n)]
        return out + self.limit_buys(cands, len(held))


class QuantRankEvaluator(Evaluator):
    def decide(self, env, as_of, held, session_index=None):
        d, p = self.defn, self.params
        top_n = int(pval(d["top_n"], p))
        exit_rank = int(pval(d.get("exit_rank", 2 * top_n), p))
        every = int(pval(d.get("rebalance_every", 1), p))
        idx = session_index if session_index is not None else session_ordinal(as_of)
        rebalance = every <= 1 or idx % every == 0
        scored = []
        ctxs = {}
        for sym in env.universe:
            ctx = env.context(sym, as_of)
            if ctx is None:
                continue
            ctxs[sym] = ctx
            vals = [(ctx.get(t["feature"]), float(pval(t.get("weight", 1), p))) for t in d["score"]["terms"]]
            if any(v is None or isinstance(v, str) for v, _w in vals):
                continue
            if d.get("filter") and not R.evaluate(d["filter"], ctx, p)[0]:
                continue
            scored.append((sum(v * w for v, w in vals), sym))
        desc = d["score"].get("direction", "desc") == "desc"
        scored.sort(key=lambda x: (-x[0] if desc else x[0], x[1]))
        rank = {sym: i + 1 for i, (_s, sym) in enumerate(scored)}
        n = len(scored)
        out, cands = [], []
        for sym, h in held.items():
            ctx = ctxs.get(sym)
            if ctx is None:
                continue
            r = rank.get(sym)
            if self.max_hold_exit(h):
                out.append(self._coded(self.intent(sym, as_of, EXIT, None, "max hold reached", ctx), "MAX_HOLD"))
            elif rebalance and (r is None or r > exit_rank):
                out.append(self.intent(sym, as_of, EXIT, None,
                                       f"rank {r if r else 'unranked'} beyond exit_rank {exit_rank}", ctx))
            else:
                out.append(self.intent(sym, as_of, HOLD, (1 - (r - 1) / n) if r else None,
                                       f"rank {r} of {n}" + ("" if rebalance else " (not a rebalance session)"), ctx))
        if rebalance:
            for score, sym in scored[:top_n]:
                if sym in held:
                    continue
                r = rank[sym]
                it = self.intent(sym, as_of, BUY, 1 - (r - 1) / n, f"rank {r} of {n}, score {score:.4g}",
                                 ctxs[sym], entry=True)
                cands.append((r, sym, it))
        return out + self.limit_buys(cands, len(held))


class CompositeEvaluator(Evaluator):
    def __init__(self, defn, params, loader=None, depth=0):
        super().__init__(defn, params, loader, depth)
        if depth > 3:
            raise ValueError("composite strategies nested more than 3 deep")
        if loader is None:
            raise ValueError("a composite needs a loader for its member definitions")
        self.members = []
        for m in defn["members"]:
            mdef = loader(m["strategy_id"], m["version"])
            from strategy_engine.definition import specs
            from strategy_engine.params import resolve
            self.members.append((m, make_evaluator(mdef, resolve(specs(mdef)), loader, depth + 1)))

    def decide(self, env, as_of, held, session_index=None):
        d, p = self.defn, self.params
        mode = d["mode"]
        active = self.members
        if mode == "regime_select":
            key = d.get("regime_key", "market_trend")
            reg = env.market(as_of).get(key)
            ids = d["regime_map"].get(reg, d["regime_map"].get("default", []))
            active = [m for m in self.members if m[0]["strategy_id"] in ids]
            if not active:
                out = []
                for sym in held:
                    ctx = env.context(sym, as_of)
                    if ctx is None:
                        continue
                    dec = EXIT if pval(d.get("exit_on_unmapped_regime", False), p) else HOLD
                    out.append(self.intent(sym, as_of, dec, None, f"no member strategy for regime {reg}", ctx))
                return out
        per = {}                               # symbol -> [(member, intent)]
        for m, ev in active:
            for it in ev.decide(env, as_of, held, session_index):
                per.setdefault(it.symbol, []).append((m, it))
        out, cands = [], []
        n = len(active)
        tot_w = sum(float(m.get("weight", 1)) for m, _e in active) or 1.0
        for sym, lst in sorted(per.items()):
            ctx = env.context(sym, as_of)
            if ctx is None:
                continue
            tag = lambda it_list: "; ".join(f"{m['strategy_id']}:{it.action}" for m, it in it_list)
            buys = [(m, it) for m, it in lst if it.action in (BUY, ADD)]
            exits = [(m, it) for m, it in lst if it.action in (EXIT,)]
            if mode in ("priority", "regime_select"):
                order = [m["strategy_id"] for m, _e in active]
                first = sorted([x for x in lst if x[1].action != NO_ACTION],
                               key=lambda x: order.index(x[0]["strategy_id"]))
                if not first:
                    continue
                m, it = first[0]
                dec = it.action
                if dec in (BUY,) and sym in held:
                    dec = HOLD
                it2 = self.intent(sym, as_of, dec, it.confidence, f"{m['strategy_id']} decides: {it.reason}", ctx,
                                  entry=dec == BUY)
                (cands.append((-(it.confidence or 0), sym, it2)) if dec == BUY else out.append(it2))
                continue
            if sym in held:
                if mode == "vote":
                    need = int(pval(d.get("min_agree_exit", 1), p))
                    ex = len(exits) >= need
                    conf = len(exits) / n
                else:
                    share = sum(float(m.get("weight", 1)) for m, _i in exits) / tot_w
                    ex = share >= float(pval(d.get("exit_threshold", 0.5), p))
                    conf = share
                out.append(self.intent(sym, as_of, EXIT if ex else HOLD, conf, tag(lst), ctx))
                continue
            if mode == "vote":
                ok = len(buys) >= int(pval(d.get("min_agree", 2), p))
                conf = len(buys) / n
            else:
                conf = sum(float(m.get("weight", 1)) * (it.confidence or 0) for m, it in buys) / tot_w
                ok = bool(buys) and conf >= float(pval(d.get("entry_threshold", 0.5), p))
            if ok:
                cands.append((-conf, sym, self.intent(sym, as_of, BUY, conf, tag(lst), ctx, entry=True)))
        return out + self.limit_buys(cands, len(held))


class PythonEvaluator(Evaluator):
    """A W2 code strategy behind the same interface."""

    def decide(self, env, as_of, held, session_index=None):
        from backtest.strategies import REGISTRY
        from backtest.strategy import PositionView, StrategyContext
        strat = REGISTRY[self.defn["python_class"]](**self.params)
        ctx = StrategyContext(as_of=as_of, data=env.history.view(as_of), universe=env.universe,
                              positions={s: PositionView(s, h.get("qty", 0), h.get("entry_price", 0), as_of,
                                                         h.get("held_sessions", 0)) for s, h in held.items()},
                              cash=0.0, equity=0.0, params=dict(self.params), scores=env.scores)
        out, cands = [], []
        for sig in strat.on_bar(ctx) or []:
            fctx = env.context(sig.symbol, as_of)
            if sig.side == "SELL":
                dec = EXIT if sig.symbol in held else SELL
                out.append(self.intent(sig.symbol, as_of, dec, None, sig.reason or "sell signal", fctx))
            else:
                it = self.intent(sig.symbol, as_of, BUY, None, sig.reason or "buy signal", fctx)
                it.stop_price, it.target_price = sig.stop_price, sig.target_price
                it.max_hold_sessions = sig.max_hold_sessions
                cands.append((0, sig.symbol, it))
        for sym in held:
            if not any(i.symbol == sym for i in out):
                fctx = env.context(sym, as_of)
                if fctx is not None:
                    out.append(self.intent(sym, as_of, HOLD, None, "held", fctx))
        return out + self.limit_buys(cands, len(held))


EVALUATORS = {"rule": RuleEvaluator, "multi_factor": MultiFactorEvaluator, "quant_rank": QuantRankEvaluator,
              "composite": CompositeEvaluator, "python": PythonEvaluator}


def make_evaluator(defn: dict, params: dict, loader=None, depth: int = 0) -> Evaluator:
    cls = EVALUATORS[defn["kind"]]
    if cls is CompositeEvaluator:
        return cls(defn, params, loader, depth)
    return cls(defn, params, loader, depth)
