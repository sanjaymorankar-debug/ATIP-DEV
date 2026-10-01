"""
No-code strategy builder (W36: SE-10).

The /strategy-builder page lets the owner build a rule strategy from dropdowns -- no JSON, no code:

    entry   ALL / ANY / AT LEAST k of a list of conditions
    exit    ALL / ANY of a list of conditions
    each condition:  <feature> <operator> <a number | another feature | a list of words>
    sizing  position % of equity, max positions, new per day, stop %, target %, max holding sessions
    ranking the feature that orders candidates when more qualify than there are slots

compile_spec(spec) turns that form into a W3 "rule" definition (strategy_engine/definition.py):
    * every numeric threshold becomes a named, bounded PARAMETER ("<feature>_<min|max|...>_<n>"),
      so the strategy is tunable by the W23 optimiser / sensitivity tools without editing rules
    * features and operators are checked against strategy_engine.features.known() and rules.OPS
    * the result is validated by definition.validate() -- the same check every library strategy passes
preview(conn, defn, as_of) evaluates the entry rule on the latest session for the tracked universe
(or the given symbols), read-only, and returns the matches with how many conditions held -- so the
owner sees what the rule would pick before saving.
save(conn, spec) stores it through registry.create_strategy as a DRAFT (source 'builder'): from there
it follows the normal lifecycle (backtest -> validation -> approval -> paper), exactly like a coded one.
Nothing here can activate a strategy or reach execution.
"""

from __future__ import annotations

import re
from datetime import date

from strategy_engine import definition as D
from strategy_engine import features as F
from strategy_engine import rules as R

MAX_CONDITIONS = 12
_OPWORD = {"<": "max", "<=": "max", ">": "min", ">=": "min", "==": "eq", "!=": "ne",
           "crosses_above": "cross", "crosses_below": "cross"}


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    s = s if s and s[0].isalpha() else f"s_{s}"
    return s[:48] or "builder_strategy"


def _bounds(feature: str, v: float):
    if feature in F.SCORE_FEATURES or feature in ("mh_score",) or re.match(r"^rsi_\d+$", feature):
        return 0.0, 100.0
    span = max(abs(v), 1.0)
    return round(v - 2 * span, 6), round(v + 2 * span, 6)


def _condition(c: dict, params: list, path: str) -> dict:
    feat = str(c.get("feature") or "").strip()
    op = str(c.get("op") or "").strip()
    if not F.known(feat):
        raise D.DefinitionError(f"{path}: unknown feature '{feat}'")
    if op not in R.OPS:
        raise D.DefinitionError(f"{path}: operator must be one of {R.OPS}")
    kind, val = c.get("value_type", "number"), c.get("value")
    if kind == "feature":
        if not F.known(str(val)):
            raise D.DefinitionError(f"{path}: unknown comparison feature '{val}'")
        return {"feature": feat, "op": op, "value": {"feature": str(val)}}
    if kind == "list":
        items = [x.strip() for x in (val if isinstance(val, list) else str(val).split(",")) if str(x).strip()]
        if op not in ("in", "not_in") or not items:
            raise D.DefinitionError(f"{path}: a list needs the 'in' / 'not_in' operator and at least one item")
        return {"feature": feat, "op": op, "value": items}
    if op == "between":
        try:
            lo, hi = (float(x) for x in (val if isinstance(val, list) else str(val).split(",")))
        except (TypeError, ValueError):
            raise D.DefinitionError(f"{path}: 'between' needs two numbers, e.g. 40, 70")
        names = []
        for tag, x in (("lo", lo), ("hi", hi)):
            name = f"{feat}_{tag}_{len(params) + 1}"
            b = _bounds(feat, x)
            params.append({"name": name, "type": "float", "default": x, "min": b[0], "max": b[1],
                           "description": f"{feat} between: {tag}"})
            names.append({"param": name})
        return {"feature": feat, "op": op, "value": names}
    try:
        x = float(val)
    except (TypeError, ValueError):
        raise D.DefinitionError(f"{path}: value must be a number")
    name = f"{feat}_{_OPWORD.get(op, 'v')}_{len(params) + 1}"
    lo, hi = _bounds(feat, x)
    params.append({"name": name, "type": "float", "default": x, "min": lo, "max": hi,
                   "description": f"{feat} {op} threshold"})
    return {"feature": feat, "op": op, "value": {"param": name}}


def _group(g: dict, params: list, path: str, allow_min=True) -> dict:
    conds = g.get("conditions") or []
    if not conds:
        raise D.DefinitionError(f"{path}: add at least one condition")
    if len(conds) > MAX_CONDITIONS:
        raise D.DefinitionError(f"{path}: at most {MAX_CONDITIONS} conditions")
    leaves = [_condition(c, params, f"{path}[{i}]") for i, c in enumerate(conds)]
    mode = (g.get("mode") or "all").lower()
    if mode == "all":
        return {"all": leaves}
    if mode == "any":
        return {"any": leaves}
    if mode == "min" and allow_min:
        k = int(g.get("k") or 1)
        if not 1 <= k <= len(leaves):
            raise D.DefinitionError(f"{path}: 'at least k' needs 1 <= k <= {len(leaves)}")
        return {"min": k, "of": leaves}
    raise D.DefinitionError(f"{path}: mode must be all, any" + (" or min" if allow_min else ""))


def compile_spec(spec: dict) -> dict:
    name = str(spec.get("name") or "").strip()
    if len(name) < 3:
        raise D.DefinitionError("give the strategy a name (3+ characters)")
    params: list = []
    entry = _group(spec.get("entry") or {}, params, "entry")
    exit_ = _group(spec.get("exit") or {}, params, "exit", allow_min=False)
    pos = spec.get("position") or {}

    def p(key, default, lo, hi, typ="float", desc=""):
        v = pos.get(key, default)
        v = int(v) if typ == "int" else float(v)
        if not lo <= v <= hi:
            raise D.DefinitionError(f"{key} must be between {lo} and {hi}")
        params.append({"name": key, "type": typ, "default": v, "min": lo, "max": hi, "description": desc})
        return {"param": key}
    position = {"target_position_pct": p("position_pct", 10, 1, 25, desc="target position, % of equity"),
                "max_positions": int(pos.get("max_positions", 10)), "max_new_per_day": int(pos.get("max_new_per_day", 3)),
                "stop_pct": p("stop_pct", 5, 0.5, 25, desc="protective stop below entry, %"),
                "target_pct": p("target_pct", 10, 1, 60, desc="profit target above entry, %"),
                "max_hold_sessions": p("max_hold", 20, 1, 250, "int", "maximum holding sessions")}
    if not 1 <= position["max_positions"] <= 50 or not 1 <= position["max_new_per_day"] <= 20:
        raise D.DefinitionError("max positions must be 1-50 and new per day 1-20")
    rank = str(spec.get("rank_by") or "atip_score")
    if not F.known(rank):
        raise D.DefinitionError(f"rank_by: unknown feature '{rank}'")
    blocked = [r for r in (spec.get("blocked_regimes") or ["HIGH_RISK"]) if r]
    uni = spec.get("symbols")
    defn = {"strategy_id": _slug(spec.get("strategy_id") or f"nb_{name}"), "name": name, "version": "1.0.0",
            "description": (spec.get("description") or f"Built with the no-code builder: {name}")[:500],
            "kind": "rule", "category": "builder",
            "universe": {"type": "symbols", "symbols": [s.upper() for s in uni]} if uni else {"type": "tracked_current"},
            "timeframe": "1d", "parameters": params, "entry": entry, "exit": exit_, "rank_by": rank,
            "position": position,
            "risk": {"requirement": "STANDARD", "max_cri": float(spec.get("max_cri", 75)), "blocked_regimes": blocked}}
    D.validate(defn)
    return defn


def preview(conn, defn: dict, as_of=None, symbols=None, limit=50) -> dict:
    """Which symbols the ENTRY rule picks on as_of (default: the latest scored session). Read-only."""
    from strategy_engine.engine import build_env
    from strategy_engine.params import resolve
    d = as_of or conn.execute("SELECT MAX(date) FROM ai_scores").fetchone()[0]
    if not d:
        return {"as_of": None, "matches": [], "evaluated": 0}
    as_of = d if isinstance(d, date) else date.fromisoformat(str(d)[:10])
    if not symbols:
        from data.dhan import get_tracked_symbols
        symbols = sorted(get_tracked_symbols(conn))
    env = build_env(conn, list(symbols), as_of)
    params = resolve(D.specs(defn), {})
    matches, evaluated, near = [], 0, []
    for s in symbols:
        ctx = env.context(s, as_of)
        if ctx is None:
            continue
        evaluated += 1
        met, held, total, trace = R.evaluate(defn["entry"], ctx, params)
        rank = ctx.get(defn.get("rank_by") or "atip_score")
        item = {"symbol": s, "conditions_met": held, "conditions": total,
                "rank_value": round(rank, 3) if isinstance(rank, float) else rank}
        if met:
            matches.append(item)
        elif total and held == total - 1:
            near.append(item)
    key = lambda x: (x["rank_value"] is None, -(x["rank_value"] or 0) if isinstance(x["rank_value"], (int, float)) else 0)
    matches.sort(key=key)
    near.sort(key=key)
    return {"as_of": str(as_of), "evaluated": evaluated, "matches": matches[:limit], "match_count": len(matches),
            "one_condition_short": near[:20],
            "note": "Today's entry matches only -- not a backtest. Run a backtest before moving the strategy past DRAFT."}


def save(conn, spec: dict, actor: str = "owner") -> dict:
    from strategy_engine.registry import create_strategy
    defn = compile_spec(spec)
    return create_strategy(conn, defn, actor=actor, source="builder", notes="created with the no-code builder (SE-10)")


def options() -> dict:
    """What the form offers: operators and the feature catalogue grouped for dropdowns."""
    cat = F.catalogue()
    return {"operators": list(R.OPS), "catalogue": cat,
            "rank_suggestions": ["atip_score", "zpi", "vpi", "mri", "rri", "ret_20", "rsi_14"],
            "regimes": ["BULL", "NEUTRAL", "BEAR", "HIGH_RISK"]}
