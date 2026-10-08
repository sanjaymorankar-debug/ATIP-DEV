"""
W39 (RS-01..RS-05) — valuation the way a sell-side desk does it, kept to the data ATIP
actually has (NSE XBRL fundamentals, prices, the Nifty 500 industry map). Pure functions:
no database, no network, so every number in a report can be reproduced from its inputs.

Methods (each skipped, never guessed, when its inputs are missing):

  DCF (non-financials)   two-stage: 10 explicit years whose growth fades linearly from g1
                         to the terminal rate, then Gordon growth. The equity cash flow is
                         earnings x (1 - g / ROE): the reinvestment a company growing at g
                         with return ROE needs (FCFE ~ E(1 - g/ROE)). Discounted at the CAPM
                         cost of equity ke = rf + beta x ERP.
  Scenarios              bear / base / bull move g1, ke and the terminal rate together and are
                         probability-weighted (default 25 / 50 / 25), as Morgan Stanley's
                         risk-reward framework does; plus a ke x terminal-growth grid.
  Justified P/B          financials (banks, NBFCs, insurers): P/B = (ROE - g) / (ke - g).
  Peer multiples         median P/E (and P/B for financials) of the same NSE industry,
                         applied to the stock's EPS / book value; needs 3+ peers.
  Own history            the stock's median P/E (P/B) over its stored quarters.

Fair value = weighted blend of the methods present. 12-month target = fair value rolled
forward a year at ke less the dividend yield. Rating from the expected return, with the
BUY hurdle raised as valuation uncertainty rises (Morningstar's margin-of-safety idea):

    BUY      upside >= hurdle (LOW 10%, MEDIUM 15%, HIGH 20%, VERY_HIGH 30%)
    ADD      5% <= upside < hurdle
    REDUCE   -5% <= upside < 5%
    SELL     upside < -5%
    NOT_RATED  no method could value the stock

Units (normalise_fundamentals converts a stored row): growth rates in PERCENT, ROE / ROCE
as FRACTIONS, dividend yield as a FRACTION. These are model outputs, not advice:
research/report.py attaches the disclosures.
"""

from __future__ import annotations

import math
from statistics import median

MODEL_VERSION = "w39-val-1"

DEFAULTS = {
    "risk_free_pct": 7.0,            # India 10-year G-sec, roughly
    "equity_risk_premium_pct": 5.5,  # the range Indian brokerages use for large caps; configurable
    "terminal_growth_pct": 6.0,      # below India's long-run nominal GDP growth
    "explicit_years": 10,
    "max_growth_pct": 25.0,
    "scenario_weights": {"bear": 0.25, "base": 0.5, "bull": 0.25},
    "method_weights": {"dcf": 0.5, "justified_pb": 0.5, "peer": 0.3, "history": 0.2},
    "buy_hurdle_pct": {"LOW": 10.0, "MEDIUM": 15.0, "HIGH": 20.0, "VERY_HIGH": 30.0},
    "add_pct": 5.0,
    "sell_pct": -5.0,
    "min_peers": 3,
}

RATINGS = ("BUY", "ADD", "REDUCE", "SELL", "NOT_RATED")
RATING_DEFINITIONS = {
    "BUY": "expected 12-month return at or above the BUY hurdle for its uncertainty "
           "(LOW 10%, MEDIUM 15%, HIGH 20%, VERY HIGH 30%)",
    "ADD": "expected 12-month return of 5% up to the BUY hurdle",
    "REDUCE": "expected 12-month return between -5% and +5%",
    "SELL": "expected 12-month return below -5%",
    "NOT_RATED": "not enough data to value the stock",
}
FINANCIAL_WORDS = ("financial", "bank", "nbfc", "insurance", "finance")


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def normalise_fundamentals(row: dict) -> dict:
    """
    One fundamental_data row in the units this module uses. The sources disagree: NSE XBRL
    rows (data/nse_filings.py) store growth in % and ROE as a fraction; Screener stores ROE
    and dividend yield in %; Alpha Vantage stores both as fractions.
    """
    f = dict(row or {})
    src = str(f.get("source") or "")
    for k in ("roe", "roce", "roa"):
        v = _num(f.get(k))
        if v is not None and (src == "screener" or abs(v) > 1.5):
            v = v / 100.0
        f[k] = v
    dy = _num(f.get("dividend_yield"))
    if dy is not None and (src == "screener" or dy >= 0.2):
        dy = dy / 100.0
    f["dividend_yield"] = _clip(dy, 0.0, 0.10) if dy is not None else None
    for k in ("eps_growth_yoy", "revenue_growth_yoy", "profit_growth_yoy"):
        v = _num(f.get(k))
        if v is not None and src == "alpha_vantage" and abs(v) <= 1.5:
            v = v * 100.0
        f[k] = v
    return f


def is_financial(industry: str | None, fund: dict | None = None) -> bool:
    if industry and any(w in industry.lower() for w in FINANCIAL_WORDS):
        return True
    return bool(fund and fund.get("bank"))


def cost_of_equity(beta, cfg: dict) -> float:
    """CAPM, as a fraction. Beta clipped to 0.6..2.0; a missing beta counts as 1."""
    b = _num(beta)
    b = 1.0 if b is None else _clip(b, 0.6, 2.0)
    return (cfg["risk_free_pct"] + b * cfg["equity_risk_premium_pct"]) / 100.0


def stage1_growth(fund: dict, cfg: dict) -> float | None:
    """Starting growth (fraction): EPS / revenue / profit growth (%), averaged, clipped to 0..max and to ROE."""
    gs = [g for g in (_num(fund.get("eps_growth_yoy")), _num(fund.get("revenue_growth_yoy")),
                      _num(fund.get("profit_growth_yoy"))) if g is not None]
    if not gs:
        return None
    g = _clip(sum(gs) / len(gs) / 100.0, 0.0, cfg["max_growth_pct"] / 100.0)
    roe = _num(fund.get("roe"))
    if roe and roe > 0:
        g = min(g, roe)                       # cannot grow faster than it earns on equity for long
    return g


def dcf_value(eps, roe, g1, ke, gt, years=10) -> float | None:
    """Per-share value of a two-stage FCFE stream (fractions). None when it cannot be valued."""
    eps, roe = _num(eps), _num(roe)
    if eps is None or eps <= 0 or g1 is None or ke is None or gt is None or ke - gt < 0.01:
        return None
    roe = 0.15 if roe is None or roe <= 0 else _clip(roe, 0.08, 0.40)
    pv, e = 0.0, eps
    for t in range(1, years + 1):
        g = g1 + (gt - g1) * (t - 1) / max(1, years - 1)
        e *= 1 + g
        cf = e * (1 - _clip(g / roe, 0.0, 0.9))
        pv += cf / (1 + ke) ** t
    e_next = e * (1 + gt)
    terminal = e_next * (1 - _clip(gt / roe, 0.0, 0.9)) / (ke - gt)
    pv += terminal / (1 + ke) ** years
    return round(pv, 2)


def scenarios(fund: dict, ke: float, cfg: dict) -> dict | None:
    g1 = stage1_growth(fund, cfg)
    if g1 is None:
        return None
    gt = cfg["terminal_growth_pct"] / 100.0
    cap = cfg["max_growth_pct"] / 100.0 * 1.2
    cases = {
        "bear": {"g1": g1 * 0.5, "ke": ke + 0.01, "gt": max(0.0, gt - 0.01)},
        "base": {"g1": g1, "ke": ke, "gt": gt},
        "bull": {"g1": min(cap, g1 * 1.5 if g1 > 0 else 0.05), "ke": max(gt + 0.02, ke - 0.005), "gt": gt + 0.005},
    }
    out = {}
    for name, c in cases.items():
        v = dcf_value(fund.get("eps_ttm"), fund.get("roe"), c["g1"], c["ke"], c["gt"], cfg["explicit_years"])
        out[name] = {"value": v, "g1_pct": round(c["g1"] * 100, 2), "ke_pct": round(c["ke"] * 100, 2),
                     "terminal_pct": round(c["gt"] * 100, 2), "weight": cfg["scenario_weights"][name]}
    vals = [(c["value"], c["weight"]) for c in out.values() if c["value"] is not None]
    if not vals or out["base"]["value"] is None:
        return None
    w = sum(x[1] for x in vals)
    out["weighted"] = round(sum(v * wt for v, wt in vals) / w, 2)
    return out


def sensitivity(fund: dict, ke: float, cfg: dict, steps=(-0.01, -0.005, 0.0, 0.005, 0.01)) -> dict | None:
    """Base-case DCF value over cost of equity (rows) x terminal growth (columns)."""
    g1 = stage1_growth(fund, cfg)
    if g1 is None:
        return None
    gt = cfg["terminal_growth_pct"] / 100.0
    rows = []
    for dk in steps:
        rows.append([dcf_value(fund.get("eps_ttm"), fund.get("roe"), g1, ke + dk, gt + dg, cfg["explicit_years"])
                     for dg in steps])
    return {"ke_pct": [round((ke + d) * 100, 2) for d in steps],
            "terminal_pct": [round((gt + d) * 100, 2) for d in steps], "values": rows}


def justified_pb(fund: dict, ke: float, cfg: dict) -> float | None:
    """(ROE - g) / (ke - g) x book value per share: the bank / NBFC anchor."""
    roe, bv = _num(fund.get("roe")), _num(fund.get("book_value_ps"))
    if roe is None or bv is None or bv <= 0:
        return None
    g = cfg["terminal_growth_pct"] / 100.0
    if roe <= 0 or ke - g < 0.01:
        return None
    pb = _clip((roe - g) / (ke - g), 0.2, 8.0)
    return round(pb * bv, 2)


def _trimmed_median(xs):
    xs = sorted(x for x in xs if x is not None and x > 0)
    if len(xs) >= 6:
        k = max(1, len(xs) // 10)
        xs = xs[k:-k]
    return median(xs) if xs else None


def peer_multiple_value(fund: dict, peers: list, financial: bool, cfg: dict) -> dict | None:
    """peers: [{"symbol", "pe", "pb", ...}] excluding the stock itself."""
    key, base = ("pb", _num(fund.get("book_value_ps"))) if financial else ("pe", _num(fund.get("eps_ttm")))
    vals = [_num(p.get(key)) for p in peers]
    vals = [v for v in vals if v is not None and 0 < v < (15 if financial else 150)]
    if len(vals) < cfg["min_peers"] or base is None or base <= 0:
        return None
    m = _trimmed_median(vals)
    return {"multiple": key, "peer_median": round(m, 2), "peers": len(vals), "value": round(m * base, 2)}


def history_multiple_value(fund: dict, history: list, financial: bool) -> dict | None:
    """history: the stock's own past multiples [{"pe", "pb"}], 4 quarters or more."""
    key, base = ("pb", _num(fund.get("book_value_ps"))) if financial else ("pe", _num(fund.get("eps_ttm")))
    vals = [_num(h.get(key)) for h in history]
    vals = [v for v in vals if v is not None and 0 < v < (15 if financial else 150)]
    if len(vals) < 4 or base is None or base <= 0:
        return None
    m = median(vals)
    return {"multiple": key, "own_median": round(m, 2), "quarters": len(vals), "value": round(m * base, 2)}


def uncertainty(method_values: list, scen: dict | None, completeness: float) -> str:
    """LOW / MEDIUM / HIGH / VERY_HIGH from how far the methods and the scenarios disagree."""
    vals = [v for v in method_values if v]
    if not vals:
        return "VERY_HIGH"
    spread = (max(vals) - min(vals)) / (sum(vals) / len(vals)) if len(vals) > 1 else 0.5
    if scen and scen.get("base", {}).get("value") and scen.get("bull", {}).get("value") \
            and scen.get("bear", {}).get("value"):
        spread = max(spread, (scen["bull"]["value"] - scen["bear"]["value"]) / scen["base"]["value"] / 2)
    if len(vals) == 1:
        spread = max(spread, 0.5)
    spread += (1 - completeness) * 0.5
    if spread < 0.25:
        return "LOW"
    if spread < 0.5:
        return "MEDIUM"
    if spread < 0.9:
        return "HIGH"
    return "VERY_HIGH"


def rating_for(upside_pct, unc: str, cfg: dict) -> str:
    if upside_pct is None:
        return "NOT_RATED"
    if upside_pct >= cfg["buy_hurdle_pct"].get(unc, cfg["buy_hurdle_pct"]["VERY_HIGH"]):
        return "BUY"
    if upside_pct >= cfg["add_pct"]:
        return "ADD"
    if upside_pct >= cfg["sell_pct"]:
        return "REDUCE"
    return "SELL"


def value_stock(fund: dict, price, *, industry: str | None = None, beta=None, peers: list | None = None,
                history: list | None = None, cfg: dict | None = None) -> dict:
    """
    The whole valuation for one stock. `fund` is its latest fundamental_data row (eps_ttm,
    book_value_ps, roe, growth rates, dividend_yield ...). Returns every method's inputs and
    output, the blended fair value, the 12-month target, upside, uncertainty and rating.
    """
    cfg = {**DEFAULTS, **(cfg or {})}
    price = _num(price)
    fin = is_financial(industry, fund)
    ke = cost_of_equity(beta, cfg)
    methods = {}
    scen = sens = None
    if fin:
        jpb = justified_pb(fund, ke, cfg)
        if jpb:
            methods["justified_pb"] = {"value": jpb, "ke_pct": round(ke * 100, 2)}
    else:
        scen = scenarios(fund, ke, cfg)
        if scen:
            methods["dcf"] = {"value": scen["weighted"], "ke_pct": round(ke * 100, 2)}
            sens = sensitivity(fund, ke, cfg)
    peer = peer_multiple_value(fund, peers or [], fin, cfg)
    if peer:
        methods["peer"] = peer
    hist = history_multiple_value(fund, history or [], fin)
    if hist:
        methods["history"] = hist

    weights = cfg["method_weights"]
    total_w = sum(weights[k] for k in methods)
    fair = round(sum(m["value"] * weights[k] for k, m in methods.items()) / total_w, 2) if methods else None
    for k in methods:
        methods[k]["weight"] = round(weights[k] / total_w, 3)

    needed = ("eps_ttm", "book_value_ps", "roe", "eps_growth_yoy", "revenue_growth_yoy")
    completeness = sum(1 for k in needed if _num(fund.get(k)) is not None) / len(needed)
    unc = uncertainty([m["value"] for m in methods.values()], scen, completeness)
    dy = _num(fund.get("dividend_yield")) or 0.0
    target = round(fair * (1 + ke - dy), 2) if fair else None
    upside = round((target / price - 1) * 100, 2) if (target and price) else None
    rating = rating_for(upside, unc, cfg) if methods else "NOT_RATED"
    return {"model_version": MODEL_VERSION, "price": price, "financial": fin, "industry": industry,
            "cost_of_equity_pct": round(ke * 100, 2), "beta": _num(beta), "methods": methods,
            "scenarios": scen, "sensitivity": sens, "fair_value": fair, "target_price": target,
            "upside_pct": upside, "uncertainty": unc if methods else None, "rating": rating,
            "data_completeness": round(completeness, 2),
            "assumptions": {k: cfg[k] for k in ("risk_free_pct", "equity_risk_premium_pct",
                                                "terminal_growth_pct", "explicit_years", "scenario_weights")}}


def quality_profile(history: list) -> dict:
    """
    A quantitative moat proxy from the stored quarters (newest first): level and stability
    of ROCE (ROE for financials), leverage and cash conversion. Labelled a proxy on purpose:
    Morningstar's moat is an analyst judgement this cannot replace.
    """
    def series(key):
        out = []
        for h in history:
            v = _num(h.get(key))
            if v is not None:
                out.append(v / 100.0 if abs(v) > 1.5 else v)
        return out
    roce = series("roce") or series("roe")
    if len(roce) < 4:
        return {"moat_proxy": None, "quality_score": None, "basis": f"{len(roce)} quarters with returns"}
    avg = sum(roce) / len(roce)
    sd = math.sqrt(sum((x - avg) ** 2 for x in roce) / (len(roce) - 1)) if len(roce) > 1 else 0.0
    de = _num(history[0].get("debt_equity"))
    score = _clip(avg / 0.30, 0, 1) * 50 + _clip(1 - sd / max(avg, 0.01), 0, 1) * 30
    score += 20 if de is None or de < 0.5 else 10 if de < 1.0 else 0
    moat = "WIDE" if avg >= 0.20 and min(roce) >= 0.15 else "NARROW" if avg >= 0.12 else "NONE"
    return {"moat_proxy": moat, "quality_score": round(score, 1), "avg_return_pct": round(avg * 100, 2),
            "return_stability_sd_pct": round(sd * 100, 2), "quarters": len(roce),
            "basis": "ROCE (ROE for financials) level and stability, leverage"}
