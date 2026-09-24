"""
The rule language (SE-03): conditions over features, combined with logic.

Leaf condition
    {"feature": "rsi_14", "op": "<", "value": 30}
    {"feature": "rsi_14", "op": "<", "value": {"param": "rsi_entry"}}
    {"feature": "close",  "op": ">", "value": {"feature": "sma_50"}}
    {"feature": "rsi_14", "op": "between", "value": [40, {"param": "rsi_hi"}]}
    {"feature": "regime", "op": "not_in", "value": ["BEAR", "HIGH_RISK"]}
    {"feature": "close",  "op": "crosses_above", "value": {"feature": "sma_20"}}

Groups
    {"all":  [c1, c2, ...]}              every condition
    {"any":  [c1, c2, ...]}              at least one
    {"not":  c}                          negation
    {"min":  3, "of": [c1, ... c5]}      at least k of them ("minimum confirmations";
                                         k may be {"param": ...})

Operators: < <= > >= == != between in not_in crosses_above crosses_below.
crosses_above: today feature > value AND on the previous session feature <= value.

A condition whose feature (or compared feature) is None -- not computable
from the data available -- is NOT met, and the trace says "missing".

evaluate() returns (met, leaves_true, leaves_total, trace). leaves_* count the
leaf conditions that held, which is what a rule strategy's confidence is
built from.
"""

from __future__ import annotations

from strategy_engine import features as F
from strategy_engine.params import value as pval

OPS = ("<", "<=", ">", ">=", "==", "!=", "between", "in", "not_in", "crosses_above", "crosses_below")


class RuleError(ValueError):
    pass


def _operand(x, ctx, params):
    x = pval(x, params)
    if isinstance(x, dict) and set(x) == {"feature"}:
        return ctx.get(x["feature"])
    return x


def _compare(op, a, b):
    if a is None or b is None:
        return None
    try:
        if op == "<": return a < b
        if op == "<=": return a <= b
        if op == ">": return a > b
        if op == ">=": return a >= b
        if op == "==": return a == b
        if op == "!=": return a != b
    except TypeError:
        return None
    raise RuleError(f"bad operator {op}")


def _leaf(c, ctx, params):
    f, op = c["feature"], c["op"]
    a = ctx.get(f)
    if op in ("crosses_above", "crosses_below"):
        prev = ctx.previous()
        b = _operand(c["value"], ctx, params)
        if prev is None:
            return None, f"{f} {op}: no previous session"
        pa, pb = prev.get(f), _operand(c["value"], prev, params)
        now = _compare(">" if op == "crosses_above" else "<", a, b)
        before = _compare("<=" if op == "crosses_above" else ">=", pa, pb)
        if now is None or before is None:
            return None, f"{f} {op}: missing"
        return (now and before), f"{f} {op} ({pa}->{a} vs {pb}->{b})"
    if op == "between":
        lo, hi = (_operand(v, ctx, params) for v in c["value"])
        if a is None or lo is None or hi is None:
            return None, f"{f} between: missing"
        return lo <= a <= hi, f"{f}={_fmt(a)} between [{_fmt(lo)}, {_fmt(hi)}]"
    if op in ("in", "not_in"):
        allowed = [pval(v, params) for v in c["value"]]
        if a is None:
            return None, f"{f} {op}: missing"
        r = a in allowed
        return (r if op == "in" else not r), f"{f}={a} {op} {allowed}"
    b = _operand(c["value"], ctx, params)
    r = _compare(op, a, b)
    if r is None:
        return None, f"{f} {op}: missing"
    return r, f"{f}={_fmt(a)} {op} {_fmt(b)}"


def _fmt(x):
    return f"{x:.4g}" if isinstance(x, float) else str(x)


def evaluate(cond, ctx, params) -> tuple:
    """(met, leaves_true, leaves_total, trace)."""
    if cond is None:
        return False, 0, 0, ["no condition"]
    if "all" in cond or "any" in cond:
        kids = [evaluate(c, ctx, params) for c in cond.get("all", cond.get("any"))]
        met = all(k[0] for k in kids) if "all" in cond else any(k[0] for k in kids)
        return (met, sum(k[1] for k in kids), sum(k[2] for k in kids), [t for k in kids for t in k[3]])
    if "not" in cond:
        m, t, n, tr = evaluate(cond["not"], ctx, params)
        return (not m), (n - t), n, [f"NOT({'; '.join(tr)})"]
    if "min" in cond:
        k = int(pval(cond["min"], params))
        kids = [evaluate(c, ctx, params) for c in cond["of"]]
        hits = sum(1 for x in kids if x[0])
        return (hits >= k, sum(x[1] for x in kids), sum(x[2] for x in kids),
                [f"{hits}/{len(kids)} confirmations (need {k})"] + [t for x in kids for t in x[3]])
    r, trace = _leaf(cond, ctx, params)
    return bool(r), int(bool(r)), 1, [("✓ " if r else ("… " if r is None else "✗ ")) + trace]


def validate(cond, declared_params: set, path="rule") -> set:
    """Static check of a condition tree. Returns the features it uses."""
    if not isinstance(cond, dict):
        raise RuleError(f"{path}: a condition must be an object")
    used = set()
    keys = set(cond)
    if keys & {"all", "any"}:
        key = "all" if "all" in cond else "any"
        if not isinstance(cond[key], list) or not cond[key]:
            raise RuleError(f"{path}.{key}: needs a non-empty list")
        for i, c in enumerate(cond[key]):
            used |= validate(c, declared_params, f"{path}.{key}[{i}]")
        return used
    if "not" in keys:
        return validate(cond["not"], declared_params, f"{path}.not")
    if "min" in keys:
        if not isinstance(cond.get("of"), list) or not cond["of"]:
            raise RuleError(f"{path}: 'min' needs a non-empty 'of' list")
        _check_param(cond["min"], declared_params, path)
        for i, c in enumerate(cond["of"]):
            used |= validate(c, declared_params, f"{path}.of[{i}]")
        return used
    if keys != {"feature", "op", "value"}:
        raise RuleError(f"{path}: a leaf needs exactly feature, op, value (got {sorted(keys)})")
    if not F.known(cond["feature"]):
        raise RuleError(f"{path}: unknown feature {cond['feature']!r}")
    if cond["op"] not in OPS:
        raise RuleError(f"{path}: operator must be one of {OPS}")
    used.add(cond["feature"])
    vals = cond["value"] if isinstance(cond["value"], list) else [cond["value"]]
    if cond["op"] == "between" and (not isinstance(cond["value"], list) or len(cond["value"]) != 2):
        raise RuleError(f"{path}: between needs [low, high]")
    if cond["op"] in ("in", "not_in") and not isinstance(cond["value"], list):
        raise RuleError(f"{path}: {cond['op']} needs a list")
    for v in vals:
        if isinstance(v, dict) and set(v) == {"feature"}:
            if not F.known(v["feature"]):
                raise RuleError(f"{path}: unknown feature {v['feature']!r}")
            used.add(v["feature"])
        else:
            _check_param(v, declared_params, path)
    return used


def _check_param(v, declared, path):
    if isinstance(v, dict):
        if set(v) != {"param"}:
            raise RuleError(f"{path}: a value object must be {{'param': name}} or {{'feature': name}}")
        if v["param"] not in declared:
            raise RuleError(f"{path}: undeclared parameter {v['param']!r}")
