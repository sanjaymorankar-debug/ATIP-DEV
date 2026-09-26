"""
Dynamic asset allocation (W14, ATIP-AAL-001): a strategic allocation from the
investor, tilted tactically by ATIP's own market intelligence, inside explicit
limits, with every number explained.

INVESTABLE CLASSES: EQUITY, INTL_EQUITY, BONDS, GOLD, SILVER, CASH. Real estate and
OTHER are held as they are: they count in net worth (W12) but are not allocated.

1. STRATEGIC (SAA, methodology AAL-1.0). Model portfolios are defined at the middle
   of each Investor-DNA band (risk score 10/30/50/70/90) and interpolated linearly
   for the investor's exact score, so a score of 64 sits between BALANCED and
   MODERATELY_AGGRESSIVE rather than jumping at a band edge.
   The risk score used is the DNA risk score, capped by horizon: the goal-weighted
   horizon (essential + important goals, weighted by target) or else the DNA horizon;
   under 3 years 20, under 5 years 40, under 7 years 60.
   A LOW_EMERGENCY_FUND flag raises the cash floor to 10%.

2. TACTICAL (TAA). Signals read from ATIP data, each a score in [-1, +1] with its value,
   date and source:
      regime          market_health.regime / mh_score (latest)
      trend           NIFTY50 close vs its 200-session average (prices_daily)
      volatility      India VIX (index_levels / prices_daily INDIAVIX)
      breadth         market_health.breadth
      crash_risk      median CRI across the scored universe (ai_scores, latest date)
      accumulation    median ZPI across the universe
      flows           market_health.fii_score
      global          global_markets.global_score -> international equity
      gold_trend      gold (USD/oz) change over the available window, and risk-off
      inr             USD/INR change (a weaker rupee favours gold / international)
   Per class tilt = clip(weighted sum, -1, 1) x max_tilt_pct x (0.5 + risk_score/200):
   a conservative investor gets half the tilt an aggressive one does. A negative
   equity tilt is moved to bonds (60%) and cash (40%); a positive one is funded from
   them the same way.

3. CONSTRAINTS. Per-class bounds (band defaults, tightened by the owner's policy),
   excluded classes, then a normalisation to 100% inside the bounds.

4. RISK GUARD. Expected return and volatility from the capital market assumptions
   (CMA: return, volatility, correlations; config wealth.cma overrides). If the 1-in-20
   bad year (mu - 1.645 sigma) would lose more than the investor's stated maximum
   annual loss (DNA), growth assets are cut 1 point at a time into bonds until it
   does not.

Equity is further split LARGE / MID / SMALL by band, tilted by regime and by the
Midcap-150 vs NIFTY-50 relative trend; sector views list the three strongest and
weakest sectors by median ATIP score (informational only).

ADVISORY ONLY: the result is a target W15 compares holdings against. Nothing here
orders anything.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

from wealth import common as C
from wealth.config import settings

METHODOLOGY_VERSION = "AAL-1.0"
CLASSES = ("EQUITY", "INTL_EQUITY", "BONDS", "GOLD", "SILVER", "CASH")
GROWTH = ("EQUITY", "INTL_EQUITY", "SILVER")

# Capital market assumptions: long-term, nominal, INR, pre-tax, annual %.
DEFAULT_CMA = {
    "returns": {"EQUITY": 12.0, "INTL_EQUITY": 11.0, "BONDS": 7.2, "GOLD": 9.0, "SILVER": 8.0, "CASH": 6.5},
    "volatility": {"EQUITY": 18.0, "INTL_EQUITY": 17.0, "BONDS": 4.0, "GOLD": 15.0, "SILVER": 25.0, "CASH": 1.0},
    "correlation": {
        ("EQUITY", "INTL_EQUITY"): 0.55, ("EQUITY", "BONDS"): 0.0, ("EQUITY", "GOLD"): -0.05,
        ("EQUITY", "SILVER"): 0.25, ("INTL_EQUITY", "BONDS"): 0.05, ("INTL_EQUITY", "GOLD"): 0.10,
        ("INTL_EQUITY", "SILVER"): 0.20, ("BONDS", "GOLD"): 0.20, ("BONDS", "SILVER"): 0.05,
        ("GOLD", "SILVER"): 0.75,
    },
    "source": "ATIP default long-term assumptions (AAL-1.0); override with config wealth.cma",
}

# Model portfolios at band mid-points (risk score -> weights %)
MODELS = {
    10: {"EQUITY": 15, "INTL_EQUITY": 5, "BONDS": 55, "GOLD": 10, "SILVER": 0, "CASH": 15},
    30: {"EQUITY": 30, "INTL_EQUITY": 5, "BONDS": 45, "GOLD": 10, "SILVER": 0, "CASH": 10},
    50: {"EQUITY": 45, "INTL_EQUITY": 10, "BONDS": 30, "GOLD": 10, "SILVER": 0, "CASH": 5},
    70: {"EQUITY": 60, "INTL_EQUITY": 10, "BONDS": 18, "GOLD": 8, "SILVER": 0, "CASH": 4},
    90: {"EQUITY": 70, "INTL_EQUITY": 12, "BONDS": 8, "GOLD": 7, "SILVER": 0, "CASH": 3},
}
# Bounds by band (min, max) %
BOUNDS = {
    "CONSERVATIVE": {"EQUITY": (5, 30), "INTL_EQUITY": (0, 10), "BONDS": (35, 75), "GOLD": (0, 15), "SILVER": (0, 3),
                     "CASH": (5, 30)},
    "MODERATELY_CONSERVATIVE": {"EQUITY": (15, 45), "INTL_EQUITY": (0, 10), "BONDS": (25, 65), "GOLD": (0, 15),
                                "SILVER": (0, 5), "CASH": (3, 25)},
    "BALANCED": {"EQUITY": (30, 60), "INTL_EQUITY": (0, 15), "BONDS": (15, 50), "GOLD": (0, 15), "SILVER": (0, 5),
                 "CASH": (2, 20)},
    "MODERATELY_AGGRESSIVE": {"EQUITY": (45, 75), "INTL_EQUITY": (0, 20), "BONDS": (5, 35), "GOLD": (0, 15),
                              "SILVER": (0, 7), "CASH": (1, 15)},
    "AGGRESSIVE": {"EQUITY": (55, 85), "INTL_EQUITY": (0, 25), "BONDS": (0, 25), "GOLD": (0, 15), "SILVER": (0, 10),
                   "CASH": (0, 10)},
}
CAP_SPLIT = {"CONSERVATIVE": (80, 15, 5), "MODERATELY_CONSERVATIVE": (75, 18, 7), "BALANCED": (65, 22, 13),
             "MODERATELY_AGGRESSIVE": (55, 27, 18), "AGGRESSIVE": (45, 30, 25)}
# signal -> {class: weight}; the equity weight also drives bonds / cash inversely
SIGNAL_WEIGHTS = {
    "regime": {"EQUITY": 0.35, "GOLD": -0.10},
    "trend": {"EQUITY": 0.20},
    "volatility": {"EQUITY": 0.15},
    "breadth": {"EQUITY": 0.10},
    "crash_risk": {"EQUITY": 0.15, "GOLD": -0.05},
    "accumulation": {"EQUITY": 0.05},
    "flows": {"EQUITY": 0.05},
    "global": {"INTL_EQUITY": 0.6},
    "gold_trend": {"GOLD": 0.6, "SILVER": 0.4},
    "inr": {"GOLD": 0.2, "INTL_EQUITY": 0.3},
}


def cma() -> dict:
    over = settings().get("cma") or {}
    out = {"returns": dict(DEFAULT_CMA["returns"]), "volatility": dict(DEFAULT_CMA["volatility"]),
           "correlation": dict(DEFAULT_CMA["correlation"]), "source": DEFAULT_CMA["source"]}
    if isinstance(over, dict):
        for k in ("returns", "volatility"):
            for c, v in (over.get(k) or {}).items():
                if c in CLASSES and isinstance(v, (int, float)) and not isinstance(v, bool):
                    out[k][c] = float(v)
        for pair, v in (over.get("correlation") or {}).items():
            a, _, b = str(pair).partition("/")
            if a in CLASSES and b in CLASSES and isinstance(v, (int, float)) and -1 <= v <= 1:
                out["correlation"][(a, b)] = float(v)
        if over:
            out["source"] = "config wealth.cma over ATIP defaults"
    return out


def _corr(c, a, b):
    if a == b:
        return 1.0
    return c["correlation"].get((a, b), c["correlation"].get((b, a), 0.0))


def portfolio_stats(weights_pct: dict, c: dict | None = None) -> dict:
    c = c or cma()
    w = {k: weights_pct.get(k, 0.0) / 100 for k in CLASSES}
    mu = sum(w[k] * c["returns"][k] for k in CLASSES)
    var = sum(w[a] * w[b] * c["volatility"][a] * c["volatility"][b] * _corr(c, a, b) for a in CLASSES for b in CLASSES)
    sd = math.sqrt(max(var, 0.0))
    rf = settings()["risk_free_pct"]
    return {"expected_return_pct": round(mu, 2), "volatility_pct": round(sd, 2),
            "bad_year_pct": round(mu - 1.645 * sd, 2), "sharpe": round((mu - rf) / sd, 2) if sd else None,
            "note": "bad_year = expected - 1.645 x volatility (a 1-in-20 year under a normal approximation)"}


def strategic_for_score(score: float) -> dict:
    pts = sorted(MODELS)
    s = max(pts[0], min(pts[-1], score))
    lo = max(p for p in pts if p <= s)
    hi = min(p for p in pts if p >= s)
    if lo == hi:
        return {k: float(v) for k, v in MODELS[lo].items()}
    t = (s - lo) / (hi - lo)
    return {k: round(MODELS[lo][k] * (1 - t) + MODELS[hi][k] * t, 2) for k in CLASSES}


def horizon_cap(years: float | None) -> float | None:
    if years is None:
        return None
    return 20.0 if years < 3 else 40.0 if years < 5 else 60.0 if years < 7 else None


def _band(score):
    from wealth.dna import band_for
    return band_for(score)


def goal_horizon(conn, owner) -> dict | None:
    """Target-weighted years to the essential + important active goals."""
    rows = conn.execute("SELECT target_date, COALESCE(target_amount, 0) FROM wealth_goal WHERE tenant_id=? AND "
                        "owner_id=? AND status='ACTIVE' AND priority IN ('ESSENTIAL','IMPORTANT')",
                        (owner["tenant_id"], owner["owner_id"])).fetchall() if C.table_exists(conn, "wealth_goal") \
        else []
    pts = []
    for td, amt in rows:
        d = td if isinstance(td, date) else date.fromisoformat(str(td)[:10])
        yrs = max(0.0, (d - date.today()).days / 365.25)
        pts.append((yrs, float(amt) or 1.0))
    if not pts:
        return None
    w = sum(p[1] for p in pts)
    return {"years": round(sum(p[0] * p[1] for p in pts) / w, 2), "goals": len(pts)}


def expected_for_owner(conn, owner, horizon_years: float | None = None) -> dict | None:
    """The strategic allocation's expected return / volatility for a horizon (used by
    W13 goals). None without an Investor DNA. Never reads goals (no recursion)."""
    from wealth import dna
    s = dna.summary(conn, owner)
    if s.get("status") == "MISSING":
        return None
    score = s["risk_score"]
    cap = horizon_cap(horizon_years)
    eff = min(score, cap) if cap is not None else score
    w = strategic_for_score(eff)
    st = portfolio_stats(w)
    return {"expected_return_pct": st["expected_return_pct"], "volatility_pct": st["volatility_pct"],
            "source": f"W14 strategic allocation (risk score {eff:.0f}"
                      + (f", capped for a {horizon_years:.1f}-year horizon" if cap is not None and cap < score else "")
                      + ")"}


# ── Tactical signals ───────────────────────────────────────────────────────
def _clip(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def _series(conn, symbol, n=260):
    rows = conn.execute("SELECT date, close FROM prices_daily WHERE symbol=? AND close>0 ORDER BY date DESC LIMIT ?",
                        (symbol, n)).fetchall()
    return [(str(r[0])[:10], float(r[1])) for r in reversed(rows)]


def signals(conn) -> list:
    out = []

    def add(key, score, value, as_of, source, note):
        out.append({"signal": key, "score": round(_clip(score), 3) if score is not None else None, "value": value,
                    "as_of": as_of, "source": source, "note": note,
                    "status": "OK" if score is not None else "NO_DATA"})

    mh = conn.execute("SELECT * FROM market_health ORDER BY date DESC LIMIT 1").fetchone() \
        if C.table_exists(conn, "market_health") else None
    mh = dict(mh) if mh else {}
    reg = mh.get("regime")
    rmap = {"STRONG_BULL": 0.6, "BULL": 0.3, "NEUTRAL": 0.0, "BEAR": -0.5, "HIGH_RISK": -1.0}
    add("regime", rmap.get(reg) if reg else None, {"regime": reg, "mh_score": mh.get("mh_score")},
        str(mh.get("date") or "")[:10] or None, "market_health", "ATIP market regime")

    nifty = _series(conn, "NIFTY50")
    if len(nifty) >= 200:
        last = nifty[-1][1]
        ma = sum(x for _, x in nifty[-200:]) / 200
        ma_prev = sum(x for _, x in nifty[-220:-20]) / 200 if len(nifty) >= 220 else ma
        gap = last / ma - 1
        sc = _clip(gap / 0.08) * 0.7 + (0.3 if ma > ma_prev else -0.3)
        add("trend", sc, {"close": last, "ma200": round(ma, 2), "gap_pct": round(gap * 100, 2),
                          "ma200_rising": ma > ma_prev}, nifty[-1][0], "prices_daily NIFTY50",
            "NIFTY 50 vs its 200-session average")
    else:
        add("trend", None, {"sessions": len(nifty)}, None, "prices_daily NIFTY50", "needs 200 sessions")

    vix = None
    r = conn.execute("SELECT india_vix, date FROM index_levels WHERE india_vix>0 ORDER BY date DESC, time DESC "
                     "LIMIT 1").fetchone() if C.table_exists(conn, "index_levels") else None
    if r:
        vix = (float(r[0]), str(r[1])[:10])
    else:
        s = _series(conn, "INDIAVIX", 1)
        vix = (s[-1][1], s[-1][0]) if s else None
    if vix:
        v = vix[0]
        sc = 0.3 if v < 12 else 0.15 if v < 15 else 0.0 if v < 20 else -0.5 if v < 25 else -1.0
        add("volatility", sc, {"india_vix": v}, vix[1], "India VIX", "high VIX = risk-off")
    else:
        add("volatility", None, None, None, "India VIX", "no VIX data")

    b = mh.get("breadth")
    add("breadth", (b - 50) / 50 if b is not None else None, {"breadth": b}, str(mh.get("date") or "")[:10] or None,
        "market_health.breadth", "share of stocks participating")
    f = mh.get("fii_score")
    add("flows", (f - 50) / 50 if f is not None else None, {"fii_score": f}, str(mh.get("date") or "")[:10] or None,
        "market_health.fii_score", "foreign institutional flows")

    if C.table_exists(conn, "ai_scores"):
        d = conn.execute("SELECT MAX(date) FROM ai_scores").fetchone()[0]
        if d:
            cri = sorted(x[0] for x in conn.execute("SELECT cri FROM ai_scores WHERE date=? AND cri IS NOT NULL",
                                                    (d,)))
            zpi = sorted(x[0] for x in conn.execute("SELECT zpi FROM ai_scores WHERE date=? AND zpi IS NOT NULL",
                                                    (d,)))
            med = lambda xs: xs[len(xs) // 2] if xs else None                                    # noqa: E731
            mc, mz = med(cri), med(zpi)
            add("crash_risk", (50 - mc) / 40 if mc is not None else None, {"median_cri": mc, "stocks": len(cri)},
                str(d)[:10], "ai_scores.cri (median)", "high universe crash risk = risk-off")
            add("accumulation", (mz - 50) / 40 if mz is not None else None, {"median_zpi": mz, "stocks": len(zpi)},
                str(d)[:10], "ai_scores.zpi (median)", "institutional accumulation")

    gm = []
    if C.table_exists(conn, "global_markets"):
        gm = [dict(x) for x in conn.execute("SELECT date, gold, usd_inr, global_score FROM global_markets ORDER BY "
                                            "date, id")]
    gs = next((x for x in reversed(gm) if x.get("global_score") is not None), None)
    add("global", (gs["global_score"] - 50) / 50 if gs else None, {"global_score": gs and gs["global_score"]},
        gs and str(gs["date"])[:10], "global_markets.global_score", "global equity conditions")
    gold = [(str(x["date"])[:10], x["gold"]) for x in gm if x.get("gold")]
    if len(gold) >= 20:
        chg = gold[-1][1] / gold[0][1] - 1
        risk_off = 0.3 if reg in ("BEAR", "HIGH_RISK") else 0.0
        add("gold_trend", _clip(chg / 0.08) * 0.7 + risk_off, {"change_pct": round(chg * 100, 2), "from": gold[0][0],
                                                              "risk_off_bonus": risk_off},
            gold[-1][0], "global_markets.gold", "gold momentum over the stored window + safe haven in risk-off")
    else:
        add("gold_trend", None, {"points": len(gold)}, None, "global_markets.gold", "needs 20 observations")
    fx = [(str(x["date"])[:10], x["usd_inr"]) for x in gm if x.get("usd_inr")]
    if len(fx) >= 20:
        chg = fx[-1][1] / fx[0][1] - 1
        add("inr", _clip(chg / 0.03), {"usd_inr_change_pct": round(chg * 100, 2), "from": fx[0][0]}, fx[-1][0],
            "global_markets.usd_inr", "a weakening rupee favours gold / international assets")
    else:
        add("inr", None, {"points": len(fx)}, None, "global_markets.usd_inr", "needs 20 observations")
    return out


def _tilts(sigs, max_tilt, scale) -> tuple[dict, list]:
    raw = {k: 0.0 for k in CLASSES}
    expl = []
    for s in sigs:
        if s["score"] is None:
            continue
        for cls, w in SIGNAL_WEIGHTS.get(s["signal"], {}).items():
            raw[cls] += s["score"] * w
            expl.append({"signal": s["signal"], "class": cls, "score": s["score"], "weight": w,
                         "contribution": round(s["score"] * w, 3)})
    pp = {k: round(_clip(v) * max_tilt * scale, 2) for k, v in raw.items()}
    eq = pp["EQUITY"]
    pp["BONDS"] = round(pp["BONDS"] - 0.6 * eq, 2)
    pp["CASH"] = round(pp["CASH"] - 0.4 * eq, 2)
    return pp, expl


def _normalise(w: dict, bounds: dict) -> dict:
    """Clip into bounds, then scale the free classes so the total is 100 (water-filling)."""
    w = {k: max(bounds[k][0], min(bounds[k][1], v)) for k, v in w.items()}
    for _ in range(50):
        tot = sum(w.values())
        diff = 100.0 - tot
        if abs(diff) < 1e-6:
            break
        free = [k for k in w if (diff > 0 and w[k] < bounds[k][1] - 1e-9) or (diff < 0 and w[k] > bounds[k][0] + 1e-9)]
        if not free:
            break
        base = sum(w[k] for k in free) or len(free)
        for k in free:
            share = (w[k] / base) if sum(w[k] for k in free) else 1 / len(free)
            w[k] = max(bounds[k][0], min(bounds[k][1], w[k] + diff * share))
    out = {k: round(v, 2) for k, v in w.items()}
    resid = round(100.0 - sum(out.values()), 2)
    if resid and abs(resid) < 0.1:                 # rounding only: give it to the largest class
        big = max(out, key=out.get)
        out[big] = round(out[big] + resid, 2)
    return out


def policy(conn, owner) -> dict:
    r = conn.execute("SELECT policy_json, updated_at FROM wealth_allocation_policy WHERE tenant_id=? AND owner_id=?",
                     (owner["tenant_id"], owner["owner_id"])).fetchone()
    p = C.loads(r[0], {}) if r else {}
    return {"bounds": p.get("bounds") or {}, "excluded_classes": p.get("excluded_classes") or [],
            "tactical_enabled": p.get("tactical_enabled", True), "max_tilt_pct": p.get("max_tilt_pct"),
            "updated_at": r[1] if r else None}


def set_policy(conn, owner, b: dict) -> dict:
    unknown = set(b) - {"bounds", "excluded_classes", "tactical_enabled", "max_tilt_pct"}
    if unknown:
        raise ValueError(f"unknown fields {sorted(unknown)}")
    bounds = b.get("bounds") or {}
    if not isinstance(bounds, dict):
        raise ValueError("bounds must be {class: [min, max]}")
    clean = {}
    for k, v in bounds.items():
        if k not in CLASSES:
            raise ValueError(f"bounds: unknown class {k}; allocatable classes {list(CLASSES)}")
        if not isinstance(v, (list, tuple)) or len(v) != 2:
            raise ValueError(f"bounds.{k} must be [min, max]")
        lo, hi = C.num(v[0], f"{k} min", 0, 100), C.num(v[1], f"{k} max", 0, 100)
        if lo > hi:
            raise ValueError(f"bounds.{k}: min above max")
        clean[k] = [lo, hi]
    ex = b.get("excluded_classes") or []
    if not isinstance(ex, list) or any(x not in CLASSES for x in ex):
        raise ValueError(f"excluded_classes must be a list of {list(CLASSES)}")
    if "EQUITY" in ex and "BONDS" in ex and "CASH" in ex:
        raise ValueError("cannot exclude equity, bonds and cash together")
    te = b.get("tactical_enabled", True)
    if not isinstance(te, bool):
        raise ValueError("tactical_enabled must be true or false")
    mt = C.num(b.get("max_tilt_pct"), "max_tilt_pct", 0, 20, required=False)
    p = {"bounds": clean, "excluded_classes": ex, "tactical_enabled": te, "max_tilt_pct": mt}
    conn.execute("INSERT INTO wealth_allocation_policy (tenant_id,owner_id,policy_json,updated_at) VALUES (?,?,?,?) "
                 "ON CONFLICT(tenant_id,owner_id) DO UPDATE SET policy_json=excluded.policy_json,"
                 "updated_at=excluded.updated_at", (owner["tenant_id"], owner["owner_id"], C.dumps(p), C.now()))
    conn.commit()
    return policy(conn, owner)


def _equity_split(conn, band, regime) -> dict:
    lg, md, sm = CAP_SPLIT[band]
    notes = []
    if regime in ("BEAR", "HIGH_RISK"):
        shift = min(10, md + sm - 5)
        md_share = md / (md + sm)
        md, sm = md - shift * md_share, sm - shift * (1 - md_share)
        lg = 100 - md - sm
        notes.append(f"{regime}: {shift:.0f} points moved from mid / small to large caps")
    elif regime == "STRONG_BULL":
        lg -= 5
        md += 3
        sm += 2
        notes.append("STRONG_BULL: 5 points from large to mid / small caps")
    mid, big = _series(conn, "NIFTYMIDCAP150", 70), _series(conn, "NIFTY50", 70)
    if len(mid) >= 60 and len(big) >= 60:
        rel = (mid[-1][1] / mid[-60][1]) / (big[-1][1] / big[-60][1]) - 1
        t = max(-3.0, min(3.0, rel * 50))
        md += t
        lg -= t
        notes.append(f"Midcap-150 vs NIFTY-50 over 60 sessions {rel * 100:+.1f}%: {t:+.1f} points to mid caps")
    tot = lg + md + sm
    return {"LARGE": round(lg / tot * 100, 1), "MID": round(md / tot * 100, 1), "SMALL": round(sm / tot * 100, 1),
            "notes": notes}


def _sector_views(conn) -> dict:
    if not C.table_exists(conn, "ai_scores"):
        return {}
    d = conn.execute("SELECT MAX(date) FROM ai_scores").fetchone()[0]
    if not d:
        return {}
    try:
        from data.index_constituents import get_symbol_industry_map
        ind = get_symbol_industry_map() or {}
    except Exception:
        ind = {}
    if not ind:
        return {"status": "NO_SECTOR_MAP"}
    by = {}
    for sym, sc in conn.execute("SELECT symbol, atip_score FROM ai_scores WHERE date=? AND atip_score IS NOT NULL",
                                (d,)):
        if sym in ind:
            by.setdefault(ind[sym], []).append(sc)
    rows = [(k, sorted(v)[len(v) // 2], len(v)) for k, v in by.items() if len(v) >= 3]
    rows.sort(key=lambda x: -x[1])
    fmt = lambda xs: [{"sector": k, "median_atip_score": round(m, 1), "stocks": n} for k, m, n in xs]   # noqa: E731
    return {"as_of": str(d)[:10], "strongest": fmt(rows[:3]), "weakest": fmt(rows[-3:][::-1]),
            "note": "informational: median ATIP score by sector; not an instruction to trade"}


def compute(conn, owner) -> dict:
    from wealth import dna
    cfg = settings()
    prof = dna.current(conn, owner)
    if not prof:
        raise ValueError("an Investor DNA is required before an allocation (answer the questionnaire)")
    ds = dna.summary(conn, owner)
    score = ds["risk_score"]
    gh = goal_horizon(conn, owner)
    horizon = gh["years"] if gh else float(prof["horizon"]["years"])
    cap = horizon_cap(horizon)
    eff = min(score, cap) if cap is not None else score
    band = _band(eff)
    pol = policy(conn, owner)
    c = cma()

    saa = strategic_for_score(eff)
    steps = [{"step": "strategic", "detail": f"model portfolio for risk score {eff:.1f} (DNA {score:.1f}"
              + (f", capped at {cap:.0f} for a {horizon:.1f}-year {'goal-weighted ' if gh else ''}horizon" if
                 cap is not None and cap < score else "") + ")", "weights": dict(saa)}]
    if "LOW_EMERGENCY_FUND" in ds["flags"] and saa["CASH"] < 10:
        add = 10 - saa["CASH"]
        saa["CASH"] = 10.0
        saa["BONDS"] = max(0.0, saa["BONDS"] - add)
        steps.append({"step": "liquidity", "detail": "LOW_EMERGENCY_FUND: cash floor raised to 10% (from bonds)",
                      "weights": dict(saa)})

    bounds = {k: list(v) for k, v in BOUNDS[band].items()}
    for k, (lo, hi) in pol["bounds"].items():
        bounds[k] = [max(bounds[k][0], lo), min(bounds[k][1], hi)] if max(bounds[k][0], lo) <= min(bounds[k][1], hi) \
            else [lo, hi]
    for k in pol["excluded_classes"]:
        bounds[k] = [0.0, 0.0]

    sigs = signals(conn)
    max_tilt = pol["max_tilt_pct"] if pol["max_tilt_pct"] is not None else cfg["tactical_max_tilt_pct"]
    scale = 0.5 + eff / 200
    if pol["tactical_enabled"]:
        tilt_pp, tilt_expl = _tilts(sigs, max_tilt, scale)
    else:
        tilt_pp, tilt_expl = {k: 0.0 for k in CLASSES}, []
    raw = {k: saa[k] + tilt_pp[k] for k in CLASSES}
    final = _normalise(raw, bounds)
    steps.append({"step": "tactical", "detail": ("tilts applied (max %.1f pts x scale %.2f)" % (max_tilt, scale))
                  if pol["tactical_enabled"] else "tactical tilts disabled by policy", "tilts_pp": tilt_pp,
                  "weights": dict(raw)})
    steps.append({"step": "constraints", "detail": "clipped to bounds and normalised to 100%", "bounds": bounds,
                  "weights": dict(final)})

    # risk guard against the stated maximum annual loss
    max_loss = float(prof["answers"].get("max_annual_loss") or 30)
    st = portfolio_stats(final, c)
    moved = 0.0
    while st["bad_year_pct"] < -max_loss and moved < 60:
        src = max((k for k in GROWTH if final[k] > bounds[k][0] + 1e-9), key=lambda k: final[k], default=None)
        if not src or final["BONDS"] >= bounds["BONDS"][1] - 1e-9:
            break
        final[src] = round(final[src] - 1, 2)
        final["BONDS"] = round(final["BONDS"] + 1, 2)
        moved += 1
        st = portfolio_stats(final, c)
    if moved:
        steps.append({"step": "risk_guard", "detail": f"{moved:.0f} points moved from growth assets to bonds so a "
                      f"1-in-20 bad year stays within your {max_loss:g}% maximum loss", "weights": dict(final)})
    guard_ok = st["bad_year_pct"] >= -max_loss

    reg = next((s["value"]["regime"] for s in sigs if s["signal"] == "regime" and s["value"]), None)
    return {
        "methodology_version": METHODOLOGY_VERSION, "as_of": str(date.today()),
        "investor": {"risk_score": score, "effective_risk_score": round(eff, 1), "band": band,
                     "dna_band": ds["band"], "profile_id": ds["profile_id"], "profile_version": ds["version"],
                     "horizon_years": horizon, "horizon_source": "goals" if gh else "Investor DNA",
                     "max_annual_loss_pct": max_loss, "flags": ds["flags"]},
        "strategic": steps[0]["weights"], "tactical_tilts_pp": tilt_pp, "target": final,
        "not_allocated": ["REAL_ESTATE", "OTHER"],
        "stats": {"strategic": portfolio_stats(steps[0]["weights"], c), "target": st},
        "risk_guard": {"max_annual_loss_pct": max_loss, "within_limit": guard_ok, "points_moved": moved},
        "signals": sigs, "tilt_explanation": tilt_expl, "steps": steps, "bounds": bounds, "policy": pol,
        "equity_split": _equity_split(conn, band, reg), "sector_views": _sector_views(conn),
        "cma": {"returns": c["returns"], "volatility": c["volatility"], "source": c["source"]},
        "disclaimer": C.DISCLAIMER,
    }


# ── Storage ────────────────────────────────────────────────────────────────
def run(conn, owner, actor="owner") -> dict:
    res = compute(conn, owner)
    rid = C.new_id("alloc")
    conn.execute("INSERT INTO wealth_allocation_run (run_id,tenant_id,owner_id,as_of,methodology_version,profile_id,"
                 "band,target_json,result_json,inputs_hash,created_at,created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (rid, owner["tenant_id"], owner["owner_id"], res["as_of"], METHODOLOGY_VERSION,
                  res["investor"]["profile_id"], res["investor"]["band"], C.dumps(res["target"]), C.dumps(res),
                  C.digest({"i": res["investor"], "s": res["signals"], "p": res["policy"]}), C.now(), actor))
    conn.commit()
    return {"run_id": rid, **res}


def latest(conn, owner) -> dict | None:
    r = conn.execute("SELECT run_id, result_json, created_at FROM wealth_allocation_run WHERE tenant_id=? AND owner_id=? "
                     "ORDER BY created_at DESC LIMIT 1", (owner["tenant_id"], owner["owner_id"])).fetchone()
    if not r:
        return None
    d = C.loads(r["result_json"], {})
    d.update({"run_id": r["run_id"], "created_at": r["created_at"]})
    return d


def get_run(conn, owner, rid) -> dict:
    r = conn.execute("SELECT run_id, result_json, created_at FROM wealth_allocation_run WHERE run_id=? AND tenant_id=? "
                     "AND owner_id=?", (rid, owner["tenant_id"], owner["owner_id"])).fetchone()
    if not r:
        raise LookupError("allocation run not found")
    d = C.loads(r["result_json"], {})
    d.update({"run_id": r["run_id"], "created_at": r["created_at"]})
    return d


def runs(conn, owner, limit=50) -> list:
    out = []
    for r in conn.execute("SELECT run_id, as_of, band, target_json, methodology_version, created_at, created_by FROM "
                          "wealth_allocation_run WHERE tenant_id=? AND owner_id=? ORDER BY created_at DESC LIMIT ?",
                          (owner["tenant_id"], owner["owner_id"], int(limit))):
        d = dict(r)
        d["target"] = C.loads(d.pop("target_json"), {})
        out.append(d)
    return out


def current_target(conn, owner, max_age_days: int = 7) -> dict | None:
    """The target W15 rebalances to: the latest stored run if recent, else a fresh
    computation (not stored). None without an Investor DNA."""
    lt = latest(conn, owner)
    if lt:
        created = lt["created_at"]
        from datetime import datetime as _dt
        created = created if isinstance(created, _dt) else _dt.fromisoformat(str(created)[:19])
        if C.now() - created <= timedelta(days=max_age_days):
            return {"source": f"allocation run {lt['run_id']} ({str(created)[:10]})", **lt}
    try:
        res = compute(conn, owner)
    except ValueError:
        return None
    return {"source": "computed now (no recent stored run)", "run_id": None, **res}
