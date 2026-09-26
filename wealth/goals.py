"""
Goal planning (W13, ATIP-GOL-001): what each goal needs, whether the plan gets
there, and what changes if the assumptions do.

A GOAL: name, goal_type, target_amount (in TODAY's rupees), target_date, priority
(ESSENTIAL / IMPORTANT / ASPIRATIONAL), inflation_pct, current funding (an amount,
and/or linked wealth positions with a share of each), monthly_contribution with
an annual step-up, and the return assumption (expected_return_pct / volatility_pct;
when not given: the W14 allocation's expected return for the owner, else the
Investor DNA band's default).

    RETIREMENT     target_amount may be left empty and derived from
                   retirement_monthly_expense (today's rupees), years_in_retirement and
                   post_retirement_return_pct: the corpus that funds an inflation-growing
                   monthly withdrawal for that long.
    EMERGENCY_FUND target_amount may be left empty: emergency_months x the monthly expenses
                   in the Investor DNA.

EVALUATION (methodology GOAL-1.0, monthly compounding, contributions at month end):
    future_target      target_amount x (1 + inflation)^years
    projected          current x (1+r_m)^n + sum of stepped-up contributions compounded
    gap                projected - future_target (surplus > 0, shortfall < 0)
    required_monthly   the starting monthly contribution (same step-up) that closes the gap
    required_return    the annual return at which projected = future_target (bisection);
                       None when not reachable below 100%
    lumpsum_today      the extra amount invested today that would close a shortfall
    success_probability Monte Carlo, log-normal monthly returns with the goal's mean and
                       volatility; `monte_carlo_paths` paths seeded from the goal id, so the
                       same inputs give the same number
    percentiles        P10 / P50 / P90 of the final corpus
    status             ON_TRACK (P >= 75%), AT_RISK (50-75%), OFF_TRACK (< 50%);
                       ACHIEVED when funding already covers the future target
    scenarios          pessimistic (-3% return), base, optimistic (+2%), inflation +2%,
                       contributions stop
    glide_path         suggested growth-asset share by years left, capped by the DNA band

Nothing here moves money or places an order.
"""

from __future__ import annotations

import hashlib
import math
from datetime import date

from wealth import common as C
from wealth.config import settings

METHODOLOGY_VERSION = "GOAL-1.0"
GOAL_TYPES = ("RETIREMENT", "CHILD_EDUCATION", "HOUSE", "VEHICLE", "WEDDING", "TRAVEL", "EMERGENCY_FUND",
              "WEALTH_CREATION", "CUSTOM")
PRIORITIES = ("ESSENTIAL", "IMPORTANT", "ASPIRATIONAL")
STATUSES = ("ACTIVE", "ACHIEVED", "PAUSED", "ABANDONED")
TYPE_INFLATION = {"CHILD_EDUCATION": 10.0, "HOUSE": 7.0, "WEDDING": 7.0, "VEHICLE": 5.0, "TRAVEL": 6.0}
# return / volatility by DNA band, used until a W14 allocation exists (annual %)
BAND_RETURN = {"CONSERVATIVE": (7.5, 5.0), "MODERATELY_CONSERVATIVE": (9.0, 8.0), "BALANCED": (10.5, 11.0),
               "MODERATELY_AGGRESSIVE": (11.5, 14.0), "AGGRESSIVE": (12.5, 17.0)}
LIQUID_RETURN = (6.5, 1.0)
BAND_MAX_GROWTH = {"CONSERVATIVE": 30, "MODERATELY_CONSERVATIVE": 45, "BALANCED": 60, "MODERATELY_AGGRESSIVE": 75,
                   "AGGRESSIVE": 90}
FIELDS = ("name", "goal_type", "target_amount", "target_date", "priority", "inflation_pct", "current_amount",
          "linked_positions", "monthly_contribution", "step_up_pct", "expected_return_pct", "volatility_pct",
          "retirement_monthly_expense", "years_in_retirement", "post_retirement_return_pct", "emergency_months",
          "notes")


# ── Financial math ─────────────────────────────────────────────────────────
def months_between(start: date, end: date) -> int:
    return max(0, (end.year - start.year) * 12 + (end.month - start.month) - (1 if end.day < start.day else 0))


def monthly_rate(annual_pct: float) -> float:
    return (1 + annual_pct / 100) ** (1 / 12) - 1


def project(current: float, monthly: float, step_up_pct: float, annual_return_pct: float, n: int) -> float:
    """Corpus after n months: current compounding, plus a monthly contribution at
    each month end that rises by step_up_pct every 12 months."""
    r = monthly_rate(annual_return_pct)
    total = current * (1 + r) ** n
    c = monthly
    for m in range(n):
        if m and m % 12 == 0:
            c *= 1 + step_up_pct / 100
        total += c * (1 + r) ** (n - m - 1)
    return total


def solve_return(current, monthly, step_up_pct, n, target, lo=-50.0, hi=100.0):
    """Annual return (%) at which project() reaches target; None if unreachable."""
    if n <= 0:
        return None
    f = lambda x: project(current, monthly, step_up_pct, x, n) - target         # noqa: E731
    if f(hi) < 0:
        return None
    if f(lo) >= 0:
        return lo
    for _ in range(80):
        mid = (lo + hi) / 2
        if f(mid) >= 0:
            hi = mid
        else:
            lo = mid
    return round(hi, 3)


def required_monthly(current, step_up_pct, annual_return_pct, n, target) -> float:
    if n <= 0:
        return max(0.0, target - current)
    unit = project(0.0, 1.0, step_up_pct, annual_return_pct, n)
    need = target - project(current, 0.0, 0.0, annual_return_pct, n)
    return max(0.0, need / unit) if unit > 0 else float("inf")


def retirement_corpus(monthly_expense_today, years_to_retire, inflation_pct, years_in_retirement,
                      post_return_pct) -> float:
    """Corpus at retirement that pays an inflation-growing monthly expense (start-of-month)
    for years_in_retirement years, earning post_return_pct meanwhile."""
    first = monthly_expense_today * (1 + inflation_pct / 100) ** years_to_retire
    r = monthly_rate(post_return_pct)
    g = monthly_rate(inflation_pct)
    n = int(round(years_in_retirement * 12))
    if abs(r - g) < 1e-12:
        return first * n
    return first * (1 - ((1 + g) / (1 + r)) ** n) / (r - g) * (1 + r)


def monte_carlo(current, monthly, step_up_pct, mean_pct, vol_pct, n, target, paths, seed) -> dict:
    """Vectorized over paths (numpy); the seed makes it reproducible."""
    if n <= 0:
        return {"success_probability": 100.0 if current >= target else 0.0, "p10": current, "p50": current,
                "p90": current, "paths": 0}
    import numpy as np
    rng = np.random.default_rng(seed)
    mu = (math.log(1 + mean_pct / 100) - (vol_pct / 100) ** 2 / 2) / 12
    sd = vol_pct / 100 / math.sqrt(12)
    v = np.full(paths, float(current))
    c = float(monthly)
    for m in range(n):
        if m and m % 12 == 0:
            c *= 1 + step_up_pct / 100
        v = v * np.exp(rng.normal(mu, sd, paths)) + c
    p10, p50, p90 = (float(x) for x in np.percentile(v, [10, 50, 90]))
    return {"success_probability": round(float((v >= target).mean()) * 100, 1), "p10": round(p10, 2),
            "p50": round(p50, 2), "p90": round(p90, 2), "paths": paths}


# ── Inputs ─────────────────────────────────────────────────────────────────
def _clean(b: dict, existing: dict | None = None) -> dict:
    unknown = set(b) - set(FIELDS)
    if unknown:
        raise ValueError(f"unknown fields {sorted(unknown)}; known {list(FIELDS)}")
    x = dict(existing or {})
    x.update(b)
    gt = str(x.get("goal_type") or "CUSTOM").upper()
    if gt not in GOAL_TYPES:
        raise ValueError(f"goal_type must be one of {list(GOAL_TYPES)}")
    pr = str(x.get("priority") or "IMPORTANT").upper()
    if pr not in PRIORITIES:
        raise ValueError(f"priority must be one of {list(PRIORITIES)}")
    td = C.parse_date(x.get("target_date"), "target_date")
    if td <= date.today():
        raise ValueError("target_date must be in the future")
    links = x.get("linked_positions") or []
    if isinstance(links, str):
        links = C.loads(links, [])
    if not isinstance(links, list):
        raise ValueError("linked_positions must be a list of {key, pct}")
    clean_links = []
    for ln in links:
        if not isinstance(ln, dict) or not ln.get("key"):
            raise ValueError("each linked position needs key (from /api/wealth/positions) and pct")
        clean_links.append({"key": str(ln["key"])[:80], "pct": C.num(ln.get("pct", 100), "linked pct", 0, 100)})
    out = {
        "name": C.text(x.get("name"), "name", 120), "goal_type": gt, "priority": pr, "target_date": td,
        "target_amount": C.num(x.get("target_amount"), "target_amount", 0, 1e13,
                               required=gt not in ("RETIREMENT", "EMERGENCY_FUND")),
        "inflation_pct": C.num(x.get("inflation_pct"), "inflation_pct", 0, 30, required=False),
        "current_amount": C.num(x.get("current_amount"), "current_amount", 0, 1e13, required=False) or 0.0,
        "linked_positions": clean_links,
        "monthly_contribution": C.num(x.get("monthly_contribution"), "monthly_contribution", 0, 1e10,
                                      required=False) or 0.0,
        "step_up_pct": C.num(x.get("step_up_pct"), "step_up_pct", 0, 50, required=False) or 0.0,
        "expected_return_pct": C.num(x.get("expected_return_pct"), "expected_return_pct", -20, 40, required=False),
        "volatility_pct": C.num(x.get("volatility_pct"), "volatility_pct", 0, 80, required=False),
        "retirement_monthly_expense": C.num(x.get("retirement_monthly_expense"), "retirement_monthly_expense", 0,
                                            1e9, required=False),
        "years_in_retirement": C.num(x.get("years_in_retirement"), "years_in_retirement", 1, 60, required=False),
        "post_retirement_return_pct": C.num(x.get("post_retirement_return_pct"), "post_retirement_return_pct", -5,
                                            20, required=False),
        "emergency_months": C.num(x.get("emergency_months"), "emergency_months", 1, 36, required=False),
        "notes": C.text(x.get("notes"), "notes", 500, required=False),
    }
    if gt == "RETIREMENT" and out["target_amount"] is None and not out["retirement_monthly_expense"]:
        raise ValueError("a RETIREMENT goal needs target_amount or retirement_monthly_expense")
    return out


def _assumptions(conn, owner, g: dict) -> dict:
    """Return / volatility / inflation actually used, with where each came from."""
    cfg = settings()
    infl = g["inflation_pct"]
    infl_src = "goal"
    if infl is None:
        infl = TYPE_INFLATION.get(g["goal_type"], cfg["default_inflation_pct"])
        infl_src = "goal-type default" if g["goal_type"] in TYPE_INFLATION else "config default_inflation_pct"
    band = None
    try:
        from wealth import dna
        band = dna.summary(conn, owner).get("band")
    except Exception:
        pass
    ret, vol, src = g["expected_return_pct"], g["volatility_pct"], "goal"
    years = months_between(date.today(), g["target_date"]) / 12
    if g["goal_type"] == "EMERGENCY_FUND" and (ret is None or vol is None):
        # an emergency fund is held in cash / liquid assets whatever the risk profile
        ret = ret if ret is not None else LIQUID_RETURN[0]
        vol = vol if vol is not None else LIQUID_RETURN[1]
        src = "liquid assets (emergency fund)"
    if ret is None or vol is None:
        alloc = None
        try:
            from wealth import allocation as AL                          # W14; absent before it
            alloc = AL.expected_for_owner(conn, owner, years)
        except (ImportError, AttributeError):
            alloc = None
        if alloc:
            ret = ret if ret is not None else alloc["expected_return_pct"]
            vol = vol if vol is not None else alloc["volatility_pct"]
            src = alloc["source"]
        else:
            b = band or "BALANCED"
            # money needed soon should not ride equity risk: horizon caps the band
            cap = "CONSERVATIVE" if years < 3 else "MODERATELY_CONSERVATIVE" if years < 5 else None
            order = list(BAND_RETURN)
            if cap and order.index(b) > order.index(cap):
                b = cap
            r0, v0 = BAND_RETURN[b]
            ret = ret if ret is not None else r0
            vol = vol if vol is not None else v0
            src = (f"Investor DNA band {band} default" if band else "BALANCED default (no Investor DNA yet)") +                   (f", capped to {b} for a {years:.1f}-year horizon" if b != (band or "BALANCED") else "")
    return {"inflation_pct": infl, "inflation_source": infl_src, "expected_return_pct": ret,
            "volatility_pct": vol, "return_source": src, "band": band}


def _funding(conn, owner, g: dict, positions=None) -> dict:
    linked, parts = 0.0, []
    if g["linked_positions"]:
        if positions is None:
            from wealth import holdings as H
            positions, _ = H.positions(conn, owner, include_paper=False)
        byk = {p["key"]: p for p in positions}
        for ln in g["linked_positions"]:
            p = byk.get(ln["key"])
            v = (p["value"] if p else 0.0) * ln["pct"] / 100
            linked += v
            parts.append({"key": ln["key"], "pct": ln["pct"], "value": round(v, 2),
                          "name": p["name"] if p else None, "missing": p is None})
    return {"current_amount": g["current_amount"], "linked_value": round(linked, 2), "linked": parts,
            "total": round(g["current_amount"] + linked, 2)}


def _target_today(conn, owner, g, infl, years) -> tuple[float, str]:
    if g["target_amount"] is not None:
        return g["target_amount"], "entered"
    if g["goal_type"] == "EMERGENCY_FUND":
        exp = None
        try:
            from wealth import dna
            s = dna.summary(conn, owner)
            exp = s.get("monthly_expenses")
        except Exception:
            pass
        if not exp:
            raise ValueError("EMERGENCY_FUND without target_amount needs monthly expenses from the Investor DNA")
        months = g["emergency_months"] or 6
        return exp * months, f"{months:g} months x Rs {exp:,.0f} monthly expenses (Investor DNA)"
    # RETIREMENT from expenses: corpus in future rupees -> express in today's rupees
    post = g["post_retirement_return_pct"] if g["post_retirement_return_pct"] is not None else 7.0
    yrs = g["years_in_retirement"] or 25
    fut = retirement_corpus(g["retirement_monthly_expense"], years, infl, yrs, post)
    today = fut / (1 + infl / 100) ** years if years > 0 else fut
    return today, (f"corpus for Rs {g['retirement_monthly_expense']:,.0f}/month (today's rupees) for {yrs:g} years "
                   f"at {post:g}% post-retirement return, {infl:g}% inflation")


def evaluate(conn, owner, g: dict, positions=None, seed_key: str = "", mc=True) -> dict:
    today = date.today()
    n = months_between(today, g["target_date"])
    years = n / 12
    a = _assumptions(conn, owner, g)
    infl, ret, vol = a["inflation_pct"], a["expected_return_pct"], a["volatility_pct"]
    tgt_today, tgt_src = _target_today(conn, owner, g, infl, years)
    fut = tgt_today * (1 + infl / 100) ** years
    fund = _funding(conn, owner, g, positions)
    cur = fund["total"]
    mc_ = g["monthly_contribution"]
    step = g["step_up_pct"]
    proj = project(cur, mc_, step, ret, n)
    gap = proj - fut
    req_m = required_monthly(cur, step, ret, n, fut)
    req_r = solve_return(cur, mc_, step, n, fut)
    lump = max(0.0, -gap) / (1 + monthly_rate(ret)) ** n if n else max(0.0, -gap)
    sim = None
    if mc:
        paths = settings()["monte_carlo_paths"]
        seed = int(hashlib.sha256(f"{seed_key}|{cur:.2f}|{mc_}|{step}|{ret}|{vol}|{n}|{fut:.2f}".encode())
                   .hexdigest()[:12], 16)
        sim = monte_carlo(cur, mc_, step, ret, vol, n, fut, paths, seed)
    if cur >= fut:
        status = "ACHIEVED"
    else:
        p = sim["success_probability"] if sim else (100.0 if gap >= 0 else 0.0)
        status = "ON_TRACK" if p >= 75 else "AT_RISK" if p >= 50 else "OFF_TRACK"

    def scen(name, r=ret, i=infl, m=mc_):
        f2 = tgt_today * (1 + i / 100) ** years
        pj = project(cur, m, step, r, n)
        return {"scenario": name, "return_pct": round(r, 2), "inflation_pct": round(i, 2),
                "monthly_contribution": m, "future_target": round(f2, 2), "projected": round(pj, 2),
                "gap": round(pj - f2, 2), "funded_pct": round(pj / f2 * 100, 1) if f2 else None}
    scenarios = [scen("pessimistic", r=ret - 3), scen("base"), scen("optimistic", r=ret + 2),
                 scen("inflation +2%", i=infl + 2), scen("contributions stop", m=0.0)]
    cap = BAND_MAX_GROWTH.get(a["band"] or "BALANCED", 60)
    glide = [{"years_left": y, "growth_assets_pct": min(cap, max(10, 10 + 12 * y))}
             for y in sorted({int(math.ceil(years)), 10, 7, 5, 3, 2, 1, 0}) if y <= math.ceil(years)]
    return {
        "methodology_version": METHODOLOGY_VERSION, "as_of": str(today), "months_left": n,
        "years_left": round(years, 2), "target_today": round(tgt_today, 2), "target_source": tgt_src,
        "future_target": round(fut, 2), "funding": fund, "assumptions": a,
        "projected": round(proj, 2), "gap": round(gap, 2),
        "funded_pct_today": round(cur / fut * 100, 1) if fut else None,
        "projected_funded_pct": round(proj / fut * 100, 1) if fut else None,
        "required_monthly": round(req_m, 2) if math.isfinite(req_m) else None,
        "additional_monthly": round(max(0.0, req_m - mc_), 2) if math.isfinite(req_m) else None,
        "required_return_pct": req_r, "lumpsum_today": round(lump, 2),
        "monte_carlo": sim, "status": status, "scenarios": scenarios, "glide_path": glide,
        "note": ("The average-return projection reaches the target, but with this volatility fewer than "
                 f"{'half' if sim['success_probability'] < 50 else 'three in four'} of the simulated paths do: "
                 "the typical (median) outcome is below the average." if sim and gap >= 0 and
                 sim["success_probability"] < 75 and status != "ACHIEVED" else None),
    }


# ── Storage ────────────────────────────────────────────────────────────────
def _row(r) -> dict:
    d = dict(r)
    d["linked_positions"] = C.loads(d.pop("linked_json", None), [])
    return d


def _event(conn, owner, gid, kind, details, actor):
    conn.execute("INSERT INTO wealth_goal_event (goal_id,tenant_id,owner_id,at,kind,details_json,actor) "
                 "VALUES (?,?,?,?,?,?,?)", (gid, owner["tenant_id"], owner["owner_id"], C.now(), kind,
                                            C.dumps(details), actor))


def create(conn, owner, b: dict, actor="owner") -> dict:
    g = _clean(b)
    gid = C.new_id("goal")
    at = C.now()
    conn.execute(
        "INSERT INTO wealth_goal (goal_id,tenant_id,owner_id,name,goal_type,priority,target_amount,target_date,"
        "inflation_pct,current_amount,linked_json,monthly_contribution,step_up_pct,expected_return_pct,volatility_pct,"
        "retirement_monthly_expense,years_in_retirement,post_retirement_return_pct,emergency_months,notes,status,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'ACTIVE',?,?)",
        (gid, owner["tenant_id"], owner["owner_id"], g["name"], g["goal_type"], g["priority"], g["target_amount"],
         g["target_date"], g["inflation_pct"], g["current_amount"], C.dumps(g["linked_positions"]),
         g["monthly_contribution"], g["step_up_pct"], g["expected_return_pct"], g["volatility_pct"],
         g["retirement_monthly_expense"], g["years_in_retirement"], g["post_retirement_return_pct"],
         g["emergency_months"], g["notes"], at, at))
    _event(conn, owner, gid, "CREATED", g, actor)
    conn.commit()
    return get(conn, owner, gid)


def _load(conn, owner, gid) -> dict:
    r = conn.execute("SELECT * FROM wealth_goal WHERE goal_id=? AND tenant_id=? AND owner_id=?",
                     (gid, owner["tenant_id"], owner["owner_id"])).fetchone()
    if not r:
        raise LookupError("goal not found")
    return _row(r)


def _inputs(d: dict) -> dict:
    out = {k: d.get(k) for k in FIELDS}
    out["target_date"] = C.parse_date(out["target_date"], "target_date")
    for k in ("current_amount", "monthly_contribution", "step_up_pct"):
        out[k] = out[k] or 0.0
    out["linked_positions"] = out["linked_positions"] or []
    return out


def get(conn, owner, gid, mc=True) -> dict:
    d = _load(conn, owner, gid)
    try:
        d["evaluation"] = evaluate(conn, owner, _inputs(d), seed_key=gid, mc=mc) if d["status"] == "ACTIVE" else None
    except ValueError as e:
        d["evaluation"] = {"error": str(e)}
    return d


def update(conn, owner, gid, b: dict, actor="owner") -> dict:
    cur = _load(conn, owner, gid)
    if cur["status"] in ("ABANDONED",):
        raise ValueError("goal is abandoned")
    g = _clean(b, {k: cur.get(k) for k in FIELDS})
    conn.execute(
        "UPDATE wealth_goal SET name=?,goal_type=?,priority=?,target_amount=?,target_date=?,inflation_pct=?,"
        "current_amount=?,linked_json=?,monthly_contribution=?,step_up_pct=?,expected_return_pct=?,volatility_pct=?,"
        "retirement_monthly_expense=?,years_in_retirement=?,post_retirement_return_pct=?,emergency_months=?,notes=?,"
        "updated_at=? WHERE goal_id=? AND tenant_id=? AND owner_id=?",
        (g["name"], g["goal_type"], g["priority"], g["target_amount"], g["target_date"], g["inflation_pct"],
         g["current_amount"], C.dumps(g["linked_positions"]), g["monthly_contribution"], g["step_up_pct"],
         g["expected_return_pct"], g["volatility_pct"], g["retirement_monthly_expense"], g["years_in_retirement"],
         g["post_retirement_return_pct"], g["emergency_months"], g["notes"], C.now(), gid, owner["tenant_id"],
         owner["owner_id"]))
    _event(conn, owner, gid, "UPDATED", b, actor)
    conn.commit()
    return get(conn, owner, gid)


def set_status(conn, owner, gid, status: str, actor="owner") -> dict:
    st = str(status or "").upper()
    if st not in STATUSES:
        raise ValueError(f"status must be one of {list(STATUSES)}")
    _load(conn, owner, gid)
    conn.execute("UPDATE wealth_goal SET status=?, updated_at=? WHERE goal_id=? AND tenant_id=? AND owner_id=?",
                 (st, C.now(), gid, owner["tenant_id"], owner["owner_id"]))
    _event(conn, owner, gid, "STATUS", {"status": st}, actor)
    conn.commit()
    return get(conn, owner, gid)


def simulate(conn, owner, gid, overrides: dict) -> dict:
    """What-if: the goal with some inputs changed, evaluated, not saved."""
    cur = _load(conn, owner, gid)
    g = _clean(overrides or {}, {k: cur.get(k) for k in FIELDS})
    return {"goal_id": gid, "overrides": overrides, "evaluation": evaluate(conn, owner, g, seed_key=gid)}


def list_goals(conn, owner, include_inactive=False, mc=True) -> list:
    q = "SELECT * FROM wealth_goal WHERE tenant_id=? AND owner_id=?" + ("" if include_inactive else
                                                                       " AND status='ACTIVE'")
    rows = [_row(r) for r in conn.execute(q + " ORDER BY target_date", (owner["tenant_id"], owner["owner_id"]))]
    positions = None
    if any(r["linked_positions"] for r in rows):
        from wealth import holdings as H
        positions, _ = H.positions(conn, owner, include_paper=False)
    for d in rows:
        try:
            d["evaluation"] = evaluate(conn, owner, _inputs(d), positions, d["goal_id"], mc) \
                if d["status"] == "ACTIVE" else None
        except ValueError as e:
            d["evaluation"] = {"error": str(e)}
    return rows


def link_warnings(goals: list) -> list:
    """A position linked to several goals for more than 100% in total."""
    tot = {}
    for g in goals:
        for ln in g.get("linked_positions") or []:
            tot[ln["key"]] = tot.get(ln["key"], 0.0) + ln["pct"]
    return [{"key": k, "total_pct": v} for k, v in tot.items() if v > 100.0001]


def overview(conn, owner) -> dict:
    goals = list_goals(conn, owner)
    ev = [g for g in goals if g.get("evaluation") and "error" not in g["evaluation"]]
    counts = {}
    for g in ev:
        counts[g["evaluation"]["status"]] = counts.get(g["evaluation"]["status"], 0) + 1
    req = required_return_for_owner(conn, owner, goals)
    return {"goals": len(goals), "status_counts": counts,
            "total_future_target": round(sum(g["evaluation"]["future_target"] for g in ev), 2),
            "total_projected": round(sum(g["evaluation"]["projected"] for g in ev), 2),
            "monthly_contribution": round(sum(g["monthly_contribution"] or 0 for g in goals), 2),
            "required_monthly": round(sum(g["evaluation"]["required_monthly"] or 0 for g in ev), 2),
            "additional_monthly": round(sum(g["evaluation"]["additional_monthly"] or 0 for g in ev), 2),
            "required_return": req, "link_warnings": link_warnings(goals),
            "items": [{"goal_id": g["goal_id"], "name": g["name"], "priority": g["priority"],
                       "target_date": g["target_date"], "status": g["evaluation"]["status"],
                       "success_probability": (g["evaluation"]["monte_carlo"] or {}).get("success_probability"),
                       "future_target": g["evaluation"]["future_target"], "projected": g["evaluation"]["projected"],
                       "gap": g["evaluation"]["gap"]} for g in ev],
            "errors": [{"goal_id": g["goal_id"], "error": g["evaluation"]["error"]} for g in goals
                       if g.get("evaluation") and "error" in g["evaluation"]]}


def required_return_for_owner(conn, owner, goals=None) -> dict | None:
    """The return the ESSENTIAL + IMPORTANT goals need (future-target weighted); what
    the Investor DNA uses as risk requirement. None without such goals."""
    goals = goals if goals is not None else list_goals(conn, owner, mc=False)
    pts = []
    for g in goals:
        ev = g.get("evaluation")
        if g["priority"] == "ASPIRATIONAL" or not ev or "error" in ev:
            continue
        r = ev["required_return_pct"]
        pts.append((20.0 if r is None else max(0.0, min(20.0, r)), ev["future_target"], r is None))
    if not pts:
        return None
    w = sum(p[1] for p in pts) or 1.0
    return {"required_return_pct": round(sum(p[0] * p[1] for p in pts) / w, 2), "goals": len(pts),
            "unreachable": sum(1 for p in pts if p[2])}


def record_projection(conn, owner, gid) -> dict:
    d = get(conn, owner, gid)
    ev = d["evaluation"]
    if not ev or "error" in ev:
        raise ValueError("goal cannot be evaluated")
    conn.execute("INSERT INTO wealth_goal_projection (goal_id,tenant_id,owner_id,as_of,status,success_probability,"
                 "projected,future_target,gap,result_json,methodology_version,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,"
                 "?,?) ON CONFLICT(goal_id,as_of) DO UPDATE SET status=excluded.status,"
                 "success_probability=excluded.success_probability,projected=excluded.projected,"
                 "future_target=excluded.future_target,gap=excluded.gap,result_json=excluded.result_json,"
                 "created_at=excluded.created_at",
                 (gid, owner["tenant_id"], owner["owner_id"], ev["as_of"], ev["status"],
                  (ev["monte_carlo"] or {}).get("success_probability"), ev["projected"], ev["future_target"],
                  ev["gap"], C.dumps(ev), METHODOLOGY_VERSION, C.now()))
    conn.commit()
    return {"goal_id": gid, "as_of": ev["as_of"], "status": ev["status"]}


def projections(conn, owner, gid) -> list:
    _load(conn, owner, gid)
    return [dict(r) for r in conn.execute(
        "SELECT as_of,status,success_probability,projected,future_target,gap,methodology_version FROM "
        "wealth_goal_projection WHERE goal_id=? AND tenant_id=? AND owner_id=? ORDER BY as_of",
        (gid, owner["tenant_id"], owner["owner_id"]))]


def events(conn, owner, gid) -> list:
    _load(conn, owner, gid)
    out = []
    for r in conn.execute("SELECT at, kind, details_json, actor FROM wealth_goal_event WHERE goal_id=? AND tenant_id=? "
                          "AND owner_id=? ORDER BY id", (gid, owner["tenant_id"], owner["owner_id"])):
        d = dict(r)
        d["details"] = C.loads(d.pop("details_json"), {})
        out.append(d)
    return out
