"""
W39 Phase 2 (item 7, the DVM view left for later) — a DVM-style three-axis view (Trendlyne's Durability /
Valuation / Momentum): three 0-100 scores per stock from what ATIP already stores, each one explainable: it
returns the inputs it used, with their numbers, the way the fundamental scorecard does.

    Durability   the fundamental scorecard's financial-health and past-performance checks (research/scorecard.py,
                 12 in all): 100 x the checks passed / the checks that could be made. It needs 4+ checks that
                 could be made (a bank's debt checks are not meaningful and drop out, so banks are scored on the
                 rest rather than failed on them).
    Valuation    60 % price vs the research model's fair value (research/valuation.py, stored in research_report):
                 50 + 1.25 x the discount, so 40 % below fair value scores 100, at fair value 50 and 40 % above 0;
                 40 % the P/E against the median of its industry's (P/B for banks, NBFCs and insurers, the multiple
                 valuation.py itself values them on): 100 x (1.5 - multiple / median), so half the industry's
                 multiple scores 100, level with it 50 and 1.5x it 0. A loss-maker scores 0 on the multiple (no
                 P/E), as on the scorecard. The median needs 3+ industry peers. Either part alone when the other
                 is missing. High = cheap.
    Momentum     50 % the daily technical rating (research/technicals.py, -1..+1 mapped to 0..100) and 50 % the
                 RS rating (1-99, IBD-style percentile of the weighted 3/6/9/12-month return across the universe).
                 Either part alone when the other is missing.

Levels: HIGH at 55 or more, LOW below 35, MEDIUM between (config.json "dvm": {"high": 55, "low": 35}).
Zones need all three scores; the first rule that holds wins:

    STRONG_PERFORMER     D, V and M all HIGH            a sound business, not expensive, and in favour
    VALUE_TRAP           V HIGH but D LOW               cheap, but the business behind it is weak
    MOMENTUM_TRAP        M HIGH but D LOW               the price is running on a weak business
    EXPENSIVE_PERFORMER  D and M HIGH, V not HIGH       sound and in favour; the market already pays for it
    VALUE_UNDER_RADAR    D and V HIGH, M not HIGH       sound and cheap, not yet in favour
    WEAK                 D and M both LOW               a weak business and a weak price
    MID_RANGE            anything else                  no clear edge on the three axes

    apply(rows, cfg)             adds dvm_d / dvm_v / dvm_m / dvm_zone to the screener's rows (research/screener.py)
                                 and the explanation as row["_dvm"]
    evaluate(row, ctx, cfg)      the explanation for one row: per axis its score, level, basis and components
                                 (each with the input, its 0-100 score, its weight and a sentence with the numbers)
    for_symbol(conn, symbol)     the explanation for one stock, from the cached screener snapshot

Stored nightly with the day's scorecard (fundamental_scorecard.dvm_*, the 20:50 saved-screens job): the DVM is
built from the same screener rows the scorecard is, one row per stock per day, so it shares that row rather
than a table of its own. A description of the stock, not advice; the scores have no track record yet.
"""

from __future__ import annotations

import statistics
from datetime import date

from research import valuation as V

FIELDS = ("dvm_d", "dvm_v", "dvm_m", "dvm_zone")
DEFAULTS = {"high": 55.0, "low": 35.0, "min_durability_checks": 4, "min_peers": 3,
            "fair_value_weight": 0.6, "multiple_weight": 0.4, "rating_weight": 0.5, "rs_weight": 0.5}
FV_POINTS = 1.25              # score points per 1 % below (above) fair value: 40 % below -> 100, 40 % above -> 0
MULTIPLE_CAP = 1.5            # a multiple 1.5x its industry median scores 0, half of it 100
DURABILITY_AXES = (("health", "Financial health"), ("past", "Past performance"))

ZONES = {
    "STRONG_PERFORMER": ("Strong performer", "durability, valuation and momentum all {high}+",
                         "a sound business, not expensive, and in favour"),
    "VALUE_TRAP": ("Value trap", "valuation {high}+ but durability below {low}",
                   "cheap, but the business behind it is weak: often cheap for a reason"),
    "MOMENTUM_TRAP": ("Momentum trap", "momentum {high}+ but durability below {low}",
                      "the price is running on a weak business"),
    "EXPENSIVE_PERFORMER": ("Expensive performer", "durability and momentum {high}+, valuation below {high}",
                            "sound and in favour; the market already pays for it"),
    "VALUE_UNDER_RADAR": ("Value, under the radar", "durability and valuation {high}+, momentum below {high}",
                          "sound and cheap, not yet in favour: the trend has not turned"),
    "WEAK": ("Weak", "durability and momentum both below {low}", "a weak business and a weak price"),
    "MID_RANGE": ("Mid-range", "none of the rules above", "no clear edge on the three axes"),
}


def settings() -> dict:
    try:
        from ops.config import load
        raw = load().get("dvm") or {}
    except Exception:
        raw = {}
    return {k: type(v)(raw[k]) if k in raw else v for k, v in DEFAULTS.items()}


def _clip(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def _f(v, nd=1):
    return f"{v:,.{nd}f}"


def level(score, cfg=None) -> str | None:
    cfg = cfg or DEFAULTS
    if score is None:
        return None
    return "HIGH" if score >= cfg["high"] else "LOW" if score < cfg["low"] else "MEDIUM"


def zone(d, v, m, cfg=None) -> str | None:
    """The DVM zone for three 0-100 scores (None unless all three are known); the first rule that holds wins."""
    cfg = cfg or DEFAULTS
    if None in (d, v, m):
        return None
    ld, lv, lm = level(d, cfg), level(v, cfg), level(m, cfg)
    if ld == lv == lm == "HIGH":
        return "STRONG_PERFORMER"
    if lv == "HIGH" and ld == "LOW":
        return "VALUE_TRAP"
    if lm == "HIGH" and ld == "LOW":
        return "MOMENTUM_TRAP"
    if ld == "HIGH" and lm == "HIGH":
        return "EXPENSIVE_PERFORMER"
    if ld == "HIGH" and lv == "HIGH":
        return "VALUE_UNDER_RADAR"
    if ld == "LOW" and lm == "LOW":
        return "WEAK"
    return "MID_RANGE"


def _blend(parts) -> tuple:
    """(score, parts with their effective weights): the weighted mean of the parts that have a score."""
    have = [p for p in parts if p["score"] is not None]
    w = sum(p["weight"] for p in have)
    for p in parts:
        p["weight"] = round(p["weight"] / w, 3) if p["score"] is not None and w else 0.0
    return (round(sum(p["score"] * p["weight"] for p in have), 1) if have and w else None), parts


def _basis(comps) -> str:
    """'Price vs fair value 75 x 60 % + P/E vs its industry 75 x 40 %' over the parts that have a score."""
    return " + ".join(f"{c['label']} {_f(c['score'], 0)} × {_f(c['weight'] * 100, 0)} %"
                      for c in comps if c["score"] is not None)


def _part(key, label, inp, score, weight, detail):
    return {"key": key, "label": label, "input": inp, "score": None if score is None else round(score, 1),
            "weight": weight, "detail": detail}


# ── the three axes ───────────────────────────────────────────────────────────

def durability(row: dict, cfg=None) -> dict:
    cfg = cfg or DEFAULTS
    sc = row.get("_scorecard")
    axes = {a["key"]: a for a in (sc or {}).get("axes", [])}
    comps, passed, known = [], 0, 0
    for key, label in DURABILITY_AXES:
        ax = axes.get(key)
        checks = [{"label": c["label"], "pass": c["pass"], "detail": c["detail"]} for c in (ax or {}).get("checks", [])]
        p, k = (ax["passed"], ax["known"]) if ax else (0, 0)
        passed, known = passed + p, known + k
        comps.append(dict(_part(key, label, {"passed": p, "known": k, "checks": len(checks)},
                                p / k * 100 if k else None, None,
                                f"{p} of the {k} {label.lower()} checks that could be made passed" if k else
                                f"no {label.lower()} check could be made"), checks=checks))
    for c in comps:
        c["weight"] = round(c["input"]["known"] / known, 3) if known else 0.0
    total = sum(c["input"]["checks"] for c in comps) or 12
    if not sc:
        score, basis = None, "no fundamentals stored: no scorecard"
    elif known < cfg["min_durability_checks"]:
        score, basis = None, (f"only {known} of the {total} financial-health and past-performance checks could be "
                              f"made; {cfg['min_durability_checks']} needed")
    else:
        score = round(passed / known * 100, 1)
        basis = (f"{passed} of {known} financial-health and past-performance checks passed" +
                 (f" ({total - known} of the {total} had no data or are not meaningful here)" if known < total else ""))
    return {"key": "durability", "label": "Durability", "score": score, "level": level(score, cfg), "basis": basis,
            "components": comps}


def _industry_median(ctx, row, key, cfg):
    peers = [p.get(key) for p in ctx["industry"].get(row.get("industry"), [])
             if p["symbol"] != row["symbol"] and p.get(key) is not None and p[key] > 0]
    return (statistics.median(peers), len(peers)) if len(peers) >= cfg["min_peers"] else None


def valuation(row: dict, ctx: dict, cfg=None) -> dict:
    cfg = cfg or DEFAULTS
    px, fv = row.get("price"), row.get("fair_value")
    if px and fv and fv > 0:
        disc = (1 - px / fv) * 100
        s = _clip(50 + FV_POINTS * disc)
        fvp = _part("fair_value_gap", "Price vs fair value",
                    {"price": px, "fair_value": fv, "discount_pct": round(disc, 2)}, s, cfg["fair_value_weight"],
                    f"₹{_f(px, 2)} vs a fair value of ₹{_f(fv, 2)} (research model): "
                    + (f"{_f(disc)} % below" if disc >= 0 else f"{_f(-disc)} % above") + f" → {_f(s, 0)}")
    else:
        fvp = _part("fair_value_gap", "Price vs fair value", {"price": px, "fair_value": fv}, None,
                    cfg["fair_value_weight"], "no research fair value yet" if px else "no price")
    sc = row.get("_scorecard") or {}
    fin = sc["financial"] if "financial" in sc else V.is_financial(row.get("industry"))
    key, name = ("pb", "P/B") if fin else ("pe", "P/E")
    label = f"{name} vs its industry"
    mult, med = row.get(key), _industry_median(ctx, row, key, cfg)
    base = row.get("book_value_ps") if fin else row.get("eps_ttm")
    inp = {"multiple": name, "value": mult, "industry": row.get("industry"),
           "industry_median": round(med[0], 2) if med else None, "peers": med[1] if med else 0}
    if base is not None and base <= 0:
        mp = _part("multiple_vs_industry", label, inp, 0.0, cfg["multiple_weight"],
                   "negative book value: no P/B → 0" if fin else "loss-making: no P/E → 0")
    elif mult is None or med is None:
        mp = _part("multiple_vs_industry", label, inp, None, cfg["multiple_weight"],
                   f"no {name}" if mult is None else
                   f"fewer than {cfg['min_peers']} {row['industry']} peers with a {name}" if row.get("industry") else
                   "no industry")
    else:
        rel = mult / med[0]
        s = _clip(100 * (MULTIPLE_CAP - rel))
        inp["relative"] = round(rel, 3)
        mp = _part("multiple_vs_industry", label, inp, s, cfg["multiple_weight"],
                   f"{name} {_f(mult, 2 if fin else 1)}x vs {_f(med[0], 2 if fin else 1)}x, the median of {med[1]} "
                   f"{row['industry']} peers ({_f(rel, 2)}x) → {_f(s, 0)}")
    score, comps = _blend([fvp, mp])
    basis = "no fair value and no industry multiple to compare with" if score is None else _basis(comps)
    return {"key": "valuation", "label": "Valuation", "score": score, "level": level(score, cfg), "basis": basis,
            "components": comps}


def momentum(row: dict, cfg=None) -> dict:
    cfg = cfg or DEFAULTS
    tr, lab, rs = row.get("tech_rating"), row.get("tech_rating_label"), row.get("rs_rating")
    tp = _part("tech_rating", "Technical rating (daily)", {"rating": tr, "label": lab},
               None if tr is None else _clip((tr + 1) * 50), cfg["rating_weight"],
               "no technical snapshot" if tr is None else
               f"{_f(tr, 3)} on -1..+1 ({str(lab or '').replace('_', ' ').lower()}: the mean of its moving-average and "
               f"oscillator votes) → {_f(_clip((tr + 1) * 50), 0)}")
    rp = _part("rs_rating", "RS rating", {"rs_rating": rs}, None if rs is None else _clip(float(rs)), cfg["rs_weight"],
               "no RS rating (needs a year of prices)" if rs is None else
               f"RS rating {int(rs)} of 99: a weighted 3-12-month return ahead of about {int(rs)} % of the universe")
    score, comps = _blend([tp, rp])
    basis = "no technical rating and no RS rating" if score is None else _basis(comps)
    return {"key": "momentum", "label": "Momentum", "score": score, "level": level(score, cfg), "basis": basis,
            "components": comps}


# ── one row, every row, one symbol ───────────────────────────────────────────

def context(rows: list) -> dict:
    ctx = {"industry": {}}
    for r in rows:
        if r.get("industry"):
            ctx["industry"].setdefault(r["industry"], []).append(r)
    return ctx


def evaluate(row: dict, ctx: dict, cfg: dict | None = None) -> dict:
    """{symbol, dvm_d, dvm_v, dvm_m, dvm_zone, zone: {key, label, rule, reading} | None, axes: [...]}."""
    cfg = cfg or settings()
    axes = [durability(row, cfg), valuation(row, ctx, cfg), momentum(row, cfg)]
    d, v, m = (a["score"] for a in axes)
    z = zone(d, v, m, cfg)
    th = {"high": _f(cfg["high"], 0), "low": _f(cfg["low"], 0)}
    zi = None
    if z:
        label, rule, reading = ZONES[z]
        zi = {"key": z, "label": label, "rule": rule.format(**th), "reading": reading,
              "detail": f"durability {_f(d, 0)}, valuation {_f(v, 0)}, momentum {_f(m, 0)}: {rule.format(**th)}"}
    missing = [a["label"].lower() for a in axes if a["score"] is None]
    return {"symbol": row["symbol"], "dvm_d": d, "dvm_v": v, "dvm_m": m, "dvm_zone": z, "zone": zi, "axes": axes,
            "levels": {"high": cfg["high"], "low": cfg["low"]},
            "note": None if z else f"no zone: needs all three scores ({', '.join(missing)} missing)"}


def apply(rows: list, cfg: dict | None = None) -> list:
    """Score every screener row in place (dvm_d / dvm_v / dvm_m / dvm_zone + the explanation under "_dvm")."""
    cfg = cfg or settings()
    ctx = context(rows)
    for r in rows:
        x = evaluate(r, ctx, cfg)
        for k in FIELDS:
            r[k] = x[k]
        r["_dvm"] = x
    return rows


def for_symbol(conn, symbol: str, as_of=None) -> dict | None:
    from research import screener as SC
    kw = {"as_of": as_of} if as_of else {}
    rows, built = SC.snapshot(conn, **kw)
    row = next((r for r in rows if r["symbol"] == symbol.upper()), None)
    if not row or not row.get("_dvm"):
        return None
    return dict(row["_dvm"], as_of=str(as_of or date.today()), price=row.get("price"), industry=row.get("industry"),
                snapshot_at=built)


def main(argv=None):
    import argparse
    from db.schema import get_connection
    p = argparse.ArgumentParser(description="DVM: durability, valuation and momentum, 0-100 each")
    p.add_argument("symbol")
    a = p.parse_args(argv)
    conn = get_connection()
    try:
        x = for_symbol(conn, a.symbol)
    finally:
        conn.close()
    if not x:
        print("no such symbol in the screener snapshot")
        return
    print(f"{x['symbol']}: " + (f"{x['zone']['label']} ({x['zone']['detail']})" if x["zone"] else x["note"]))
    for ax in x["axes"]:
        print(f"  {ax['label']}: {'—' if ax['score'] is None else _f(ax['score'], 0)}  {ax['basis']}")
        for c in ax["components"]:
            print(f"    {'—' if c['score'] is None else _f(c['score'], 0):>4}  {c['label']} "
                  f"(weight {_f(c['weight'] * 100, 0)} %): {c['detail']}")


if __name__ == "__main__":
    main()
