"""
Investor DNA (W11, ATIP-INV-001): who the investor is, in numbers that can be
explained line by line.

    questionnaire()            the versioned question bank (QUESTIONNAIRE_VERSION)
    validate(answers)          cleaned answers or ValueError naming the field
    compute(answers, ...)      the profile, without storing it (preview)
    save(conn, owner, answers) compute + append a new immutable version
    current(conn, owner)       latest version (+ STALE after profile_validity_days)
    history(conn, owner)

THE PROFILE (methodology METHODOLOGY_VERSION):

  risk_capacity   0-100  how much risk the investor's finances can absorb: horizon,
                         age, income stability, savings rate, debt burden, emergency
                         fund, dependents, liquidity need. Objective facts.
  risk_tolerance  0-100  how much risk the investor is willing to take: reaction to a
                         20% fall, return/loss trade-off, objective, loss comfort,
                         maximum acceptable annual loss. Psychometric.
  risk_requirement 0-100 how much risk the investor's goals need. From the stated target
                         return until goals exist; once W13 goals are saved, from the
                         return the essential + important goals require
                         (goals.required_return_for_owner).
  experience      0-100  years, products used, self-rated knowledge.

  risk_score = min(capacity, tolerance)  -- capacity caps willingness, and willingness
               caps capacity; an inexperienced investor (experience < 30) is further
               capped at 70 (NOVICE_CAP).
  band         CONSERVATIVE <20 <= MODERATELY_CONSERVATIVE <40 <= BALANCED <60 <=
               MODERATELY_AGGRESSIVE <80 <= AGGRESSIVE

  flags        TOLERANCE_EXCEEDS_CAPACITY, REQUIREMENT_EXCEEDS_PROFILE, LOW_EMERGENCY_FUND,
               HIGH_DEBT, NOVICE_CAP, INCONSISTENT_ANSWERS, NEGATIVE_SURPLUS
  biases       loss_aversion, disposition_effect, overconfidence, herding_fomo, recency,
               overtrading -- each 0-100, LOW / MODERATE / HIGH, with a coaching note
  observed     from the house paper book (default tenant only): trades in 90 days,
               average holding days, winners' vs losers' holding days (disposition
               actually shown). Informational; it does not change the scores.
  personality  archetype (PRESERVER, LONG_TERM_INVESTOR, POSITIONAL_TRADER, SWING_TRADER,
               ACTIVE_TRADER), style (INCOME / BALANCED / GROWTH) and the default ATIP
               mode (INVESTOR / TRADER / BOTH)
  horizon      years and bucket SHORT (<3) / MEDIUM (3-7) / LONG (>7)
  explanation  every score's components: input, component score, weight, contribution

Nothing here changes a trading limit. The enterprise risk profile (W7) and the W4
risk engine stay authoritative for trading.
"""

from __future__ import annotations

from datetime import timedelta

from wealth import common as C
from wealth.config import settings

QUESTIONNAIRE_VERSION = "Q1"
METHODOLOGY_VERSION = "DNA-1.0"

BANDS = (("CONSERVATIVE", 20), ("MODERATELY_CONSERVATIVE", 40), ("BALANCED", 60),
         ("MODERATELY_AGGRESSIVE", 80), ("AGGRESSIVE", 101))


def _q(qid, section, text, kind, options=None, lo=None, hi=None, required=True, help_=""):
    d = {"id": qid, "section": section, "text": text, "type": kind, "required": required}
    if options:
        d["options"] = [{"value": v, "label": lbl, "score": s} for v, lbl, s in options]
    if lo is not None:
        d["min"] = lo
    if hi is not None:
        d["max"] = hi
    if help_:
        d["help"] = help_
    return d


QUESTIONS = [
    # ── Financial situation (capacity) ─────────────────────────────────────
    _q("age", "situation", "Your age", "number", lo=18, hi=100),
    _q("dependents", "situation", "People who depend on your income", "number", lo=0, hi=15),
    _q("employment", "situation", "Your main source of income", "choice", [
        ("SALARIED_GOVT", "Salaried: government / PSU", 90), ("SALARIED_PRIVATE", "Salaried: private sector", 70),
        ("BUSINESS", "Own business", 60), ("SELF_EMPLOYED", "Self-employed / professional", 55),
        ("RETIRED", "Retired (pension / savings)", 30), ("OTHER", "Other / irregular", 35)]),
    _q("monthly_income", "situation", "Monthly take-home income (Rs)", "number", lo=0, hi=1e9),
    _q("monthly_expenses", "situation", "Monthly household expenses excluding loan EMIs (Rs)", "number", lo=0, hi=1e9),
    _q("monthly_emi", "situation", "Monthly loan EMIs (Rs)", "number", lo=0, hi=1e9),
    _q("emergency_fund_months", "situation", "Months of expenses you hold in cash / savings for emergencies",
       "number", lo=0, hi=120),
    _q("horizon_years", "situation", "In how many years will you need most of this money?", "number", lo=1, hi=60),
    _q("liquidity_need", "situation", "How much of the money might you need at short notice?", "choice", [
        ("NONE", "Almost none within 3 years", 100), ("SOME", "Some of it within 3 years", 55),
        ("HIGH", "Most of it within a year", 15)]),
    # ── Willingness (tolerance) ────────────────────────────────────────────
    _q("drop20", "tolerance", "Your portfolio falls 20% in one month. You would:", "choice", [
        ("SELL_ALL", "Sell everything to stop the loss", 0), ("SELL_SOME", "Sell some", 25),
        ("HOLD", "Hold and wait", 60), ("BUY_MORE", "Invest more at lower prices", 100)]),
    _q("tradeoff", "tolerance", "Which one-year range of outcomes would you pick?", "choice", [
        ("A", "Best +8%, worst -2%", 0), ("B", "Best +15%, worst -8%", 35),
        ("C", "Best +25%, worst -15%", 70), ("D", "Best +40%, worst -30%", 100)]),
    _q("objective", "tolerance", "Your main objective", "choice", [
        ("PRESERVE", "Protect what I have", 0), ("INCOME", "Steady income", 30),
        ("BALANCED", "Balanced growth", 60), ("GROWTH", "Maximum long-term growth", 100)]),
    _q("loss_comfort", "tolerance", "How do investment losses affect you?", "choice", [
        ("VERY_ANXIOUS", "I lose sleep over them", 0), ("UNEASY", "I am uneasy but stay put", 40),
        ("CALM", "I accept them as part of investing", 75), ("UNBOTHERED", "They do not bother me", 100)]),
    _q("max_annual_loss", "tolerance", "The largest one-year fall you could live with", "choice", [
        ("5", "5%", 0), ("10", "10%", 30), ("20", "20%", 65), ("30", "30% or more", 100)]),
    _q("target_return", "requirement", "The yearly return you are aiming for", "choice", [
        ("6", "About 6% (deposit-like)", 10), ("9", "8-10%", 35), ("13", "12-15%", 65), ("18", "18% or more", 95)],
       help_="Used for risk requirement until you add goals; goals then take over."),
    # ── Knowledge and experience ───────────────────────────────────────────
    _q("experience_years", "experience", "Years of investing in shares", "choice", [
        ("0", "None", 0), ("1", "Less than 2", 30), ("3", "2-5", 60), ("6", "More than 5", 100)]),
    _q("products", "experience", "The most complex product you have used", "choice", [
        ("DEPOSITS", "Only deposits / savings", 0), ("LONG_TERM", "Shares or ETFs held long term", 40),
        ("ACTIVE", "Active share trading", 70), ("DERIVATIVES", "Futures & options", 100)]),
    _q("knowledge", "experience", "Your investing knowledge", "choice", [
        ("BEGINNER", "Beginner", 0), ("INTERMEDIATE", "Intermediate", 40), ("ADVANCED", "Advanced", 75),
        ("EXPERT", "Expert / professional", 100)]),
    # ── Behaviour ──────────────────────────────────────────────────────────
    _q("trade_frequency", "behaviour", "How often do you buy or sell?", "choice", [
        ("RARELY", "A few times a year", 0), ("MONTHLY", "Monthly", 35), ("WEEKLY", "Weekly", 70),
        ("DAILY", "Daily or intraday", 100)]),
    _q("holding_period", "behaviour", "How long do you usually hold a position?", "choice", [
        ("YEARS", "Years", 0), ("MONTHS", "Months", 35), ("WEEKS", "Weeks", 70), ("DAYS", "Days or less", 100)]),
    _q("check_frequency", "behaviour", "How often do you check your portfolio?", "choice", [
        ("MONTHLY", "Monthly or less", 0), ("WEEKLY", "Weekly", 35), ("DAILY", "Daily", 70),
        ("INTRADAY", "Several times a day", 100)]),
    _q("fomo", "behaviour", "A share you do not own rises 30% in a month. You:", "choice", [
        ("IGNORE", "Ignore it", 0), ("RESEARCH", "Research it first", 30), ("BUY_SOME", "Buy some", 70),
        ("BUY_BIG", "Buy a large position before it rises more", 100)]),
    _q("loser", "behaviour", "A share is down 25% and your reason for buying no longer holds. You:", "choice", [
        ("SELL", "Sell and move on", 0), ("REVIEW", "Review, then decide", 35),
        ("WAIT_BREAKEVEN", "Wait until it gets back to my price", 100)]),
    _q("winner", "behaviour", "A share is up 25% and the reason for owning it still holds. You:", "choice", [
        ("HOLD_PLAN", "Hold per plan", 0), ("TRIM", "Book part of the profit", 45),
        ("SELL_ALL", "Book the whole profit before it disappears", 100)]),
    _q("confidence", "behaviour", "\"I can pick shares that beat the market consistently.\"", "choice", [
        ("DISAGREE", "Disagree", 0), ("NEUTRAL", "Not sure", 35), ("AGREE", "Agree", 70),
        ("STRONGLY_AGREE", "Strongly agree", 100)]),
    _q("after_good_year", "behaviour", "After a very good year for shares, you:", "choice", [
        ("REBALANCE", "Rebalance back to my plan", 0), ("STAY", "Leave things as they are", 30),
        ("ADD", "Add more to shares", 70), ("ALL_IN", "Move almost everything into shares", 100)]),
    _q("coin_flip", "behaviour", "A coin flip loses you Rs 10,000 on tails. The least you would need to win on "
                                "heads to take the bet:", "choice", [
        ("10000", "Rs 10,000", 0), ("15000", "Rs 15,000", 35), ("20000", "Rs 20,000", 65),
        ("30000", "Rs 30,000 or would not take it", 100)]),
    _q("mode", "preference", "How do you want to use ATIP?", "choice", [
        ("INVESTOR", "Investor: goals, allocation, long-term wealth", 0),
        ("TRADER", "Trader: signals, strategies, trading", 0), ("BOTH", "Both", 0)]),
]
QMAP = {q["id"]: q for q in QUESTIONS}


def questionnaire() -> dict:
    return {"version": QUESTIONNAIRE_VERSION, "methodology": METHODOLOGY_VERSION, "questions": QUESTIONS,
            "sections": ["situation", "tolerance", "requirement", "experience", "behaviour", "preference"]}


def validate(answers: dict) -> dict:
    if not isinstance(answers, dict):
        raise ValueError("answers must be an object {question_id: value}")
    unknown = set(answers) - set(QMAP)
    if unknown:
        raise ValueError(f"unknown questions {sorted(unknown)}")
    out = {}
    for q in QUESTIONS:
        v = answers.get(q["id"])
        if q["type"] == "number":
            f = C.num(v, q["id"], q.get("min"), q.get("max"), required=q["required"])
            out[q["id"]] = f
        else:
            if v is None or v == "":
                if q["required"]:
                    raise ValueError(f"{q['id']} is required")
                continue
            allowed = [o["value"] for o in q["options"]]
            if str(v) not in allowed:
                raise ValueError(f"{q['id']} must be one of {allowed}")
            out[q["id"]] = str(v)
    return out


def _opt_score(qid, v):
    for o in QMAP[qid]["options"]:
        if o["value"] == v:
            return float(o["score"])
    return None


def _clip(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def _weighted(components):
    """components: [(factor, input, score, weight)] -> (score, explanation)."""
    tw = sum(w for *_, w in components)
    total = sum(s * w for _, _, s, w in components) / tw if tw else 50.0
    expl = [{"factor": f, "input": i, "score": round(s, 1), "weight": round(w / tw, 3),
             "contribution": round(s * w / tw, 2)} for f, i, s, w in components]
    return round(total, 1), expl


def band_for(score: float) -> str:
    for name, upper in BANDS:
        if score < upper:
            return name
    return BANDS[-1][0]


def _level(x):
    return "HIGH" if x >= 65 else "MODERATE" if x >= 35 else "LOW"


BIAS_NOTES = {
    "loss_aversion": "Losses weigh on you much more than equal gains. Pre-commit to rules (stop levels, rebalancing "
                     "bands) so a fall does not force a decision in the moment.",
    "disposition_effect": "A tendency to sell winners early and hold losers hoping to break even. Judge each holding "
                          "on its prospects, not on your purchase price.",
    "overconfidence": "Confidence above your stated experience. Size positions from ATIP's risk limits, and track "
                      "your picks against a benchmark (Performance tab).",
    "herding_fomo": "A pull to chase sharp rallies. Wait for a signal and a plan (entry, stop, size) before buying "
                    "something that has already run.",
    "recency": "Recent returns shape your next move. Rebalancing to a target allocation counters it.",
    "overtrading": "Frequent checking and trading raise costs and reaction to noise. Compare your executable and "
                   "actual returns with the model return (Performance tab).",
}


def compute(answers: dict, observed: dict | None = None, goal_requirement: dict | None = None) -> dict:
    """The full profile from validated or raw answers. goal_requirement (from W13):
    {"required_return_pct": x, "goals": n} overrides the stated target return."""
    a = validate(answers)
    flags = []

    # ── Risk capacity ──────────────────────────────────────────────────────
    age, horizon = a["age"], a["horizon_years"]
    income, expenses, emi = a["monthly_income"], a["monthly_expenses"], a["monthly_emi"]
    surplus = income - expenses - emi
    savings_rate = surplus / income if income > 0 else 0.0
    debt_ratio = emi / income if income > 0 else (1.0 if emi > 0 else 0.0)
    age_s = _clip(100 - (age - 30) * 2) if age > 30 else 100.0            # 100 at <=30, 10 at 75
    age_s = max(age_s, 10.0)
    cap, cap_expl = _weighted([
        ("horizon", f"{horizon:g} years", _clip(min(horizon, 20) / 20 * 100), 25),
        ("age", f"{age:g}", age_s, 15),
        ("income_stability", a["employment"], _opt_score("employment", a["employment"]), 12),
        ("savings_rate", f"{savings_rate * 100:.1f}%", _clip(savings_rate / 0.5 * 100), 12),
        ("debt_burden", f"EMI {debt_ratio * 100:.1f}% of income", _clip(100 - debt_ratio / 0.5 * 100), 12),
        ("emergency_fund", f"{a['emergency_fund_months']:g} months", _clip(a["emergency_fund_months"] / 6 * 100), 10),
        ("dependents", f"{a['dependents']:g}", _clip(100 - a["dependents"] * 15, 20), 7),
        ("liquidity_need", a["liquidity_need"], _opt_score("liquidity_need", a["liquidity_need"]), 7),
    ])

    # ── Risk tolerance ─────────────────────────────────────────────────────
    tol, tol_expl = _weighted([(k, a[k], _opt_score(k, a[k]), w) for k, w in
                               (("drop20", 25), ("tradeoff", 25), ("objective", 15), ("loss_comfort", 15),
                                ("max_annual_loss", 20))])

    # ── Experience ─────────────────────────────────────────────────────────
    exp, exp_expl = _weighted([(k, a[k], _opt_score(k, a[k]), 1) for k in
                               ("experience_years", "products", "knowledge")])

    # ── Risk requirement ───────────────────────────────────────────────────
    if goal_requirement and goal_requirement.get("required_return_pct") is not None:
        rr = float(goal_requirement["required_return_pct"])
        # 4% needs next to no risk, 16%+ needs an all-equity profile
        req = _clip((rr - 4.0) / 12.0 * 100)
        req_expl = [{"factor": "goals_required_return", "input": f"{rr:.2f}% a year across "
                     f"{goal_requirement.get('goals', 0)} goal(s)", "score": round(req, 1), "weight": 1.0,
                     "contribution": round(req, 1)}]
        req_source = "GOALS"
    else:
        req = _opt_score("target_return", a["target_return"])
        req_expl = [{"factor": "stated_target_return", "input": f"{a['target_return']}%", "score": req,
                     "weight": 1.0, "contribution": req}]
        req_source = "STATED_TARGET"

    # ── Combined score, band, flags ────────────────────────────────────────
    risk = min(cap, tol)
    if exp < 30 and risk > 70:
        risk = 70.0
        flags.append({"code": "NOVICE_CAP", "severity": "info",
                      "message": "Limited experience: the risk score is capped at 70 until experience grows."})
    if tol - cap >= 20:
        flags.append({"code": "TOLERANCE_EXCEEDS_CAPACITY", "severity": "warning",
                      "message": f"You are willing to take more risk ({tol:.0f}) than your finances can absorb "
                                 f"({cap:.0f}); the profile uses capacity."})
    if req - risk >= 15:
        flags.append({"code": "REQUIREMENT_EXCEEDS_PROFILE", "severity": "warning",
                      "message": f"Your {'goals need' if req_source == 'GOALS' else 'target return needs'} more "
                                 f"risk ({req:.0f}) than your profile supports ({risk:.0f}). Consider a later "
                                 f"date, a larger contribution or a smaller target rather than more risk."})
    if a["emergency_fund_months"] < 3:
        flags.append({"code": "LOW_EMERGENCY_FUND", "severity": "warning",
                      "message": "Less than 3 months of expenses in reserve: build an emergency fund first."})
    if debt_ratio > 0.4:
        flags.append({"code": "HIGH_DEBT", "severity": "warning",
                      "message": f"EMIs take {debt_ratio * 100:.0f}% of income (above 40%)."})
    if surplus < 0:
        flags.append({"code": "NEGATIVE_SURPLUS", "severity": "warning",
                      "message": "Expenses and EMIs exceed income: there is no monthly surplus to invest."})
    inconsistent = []
    if a["max_annual_loss"] == "5" and a["tradeoff"] in ("C", "D"):
        inconsistent.append("max annual loss 5% but chose a range with a 15-30% worst case")
    if a["drop20"] == "SELL_ALL" and a["objective"] == "GROWTH":
        inconsistent.append("maximum growth objective but would sell everything after a 20% fall")
    if a["liquidity_need"] == "HIGH" and horizon > 7:
        inconsistent.append("most money needed within a year but a horizon over 7 years")
    if inconsistent:
        flags.append({"code": "INCONSISTENT_ANSWERS", "severity": "info",
                      "message": "Some answers conflict: " + "; ".join(inconsistent) + ". Review them."})

    # ── Behavioural biases ─────────────────────────────────────────────────
    s = lambda k: _opt_score(k, a[k])                                              # noqa: E731
    over = _clip(s("confidence") * 0.6 + max(0.0, s("knowledge") - exp) * 0.4 + max(0.0, s("confidence") - exp) * 0.3)
    biases = {
        "loss_aversion": s("coin_flip"),
        "disposition_effect": round((s("loser") + s("winner")) / 2, 1),
        "overconfidence": round(over, 1),
        "herding_fomo": s("fomo"),
        "recency": s("after_good_year"),
        "overtrading": round((s("trade_frequency") + s("check_frequency")) / 2, 1),
    }
    bias_out = {k: {"score": v, "level": _level(v), "note": BIAS_NOTES[k]} for k, v in biases.items()}

    # ── Personality and mode ───────────────────────────────────────────────
    hp, tf = a["holding_period"], a["trade_frequency"]
    if risk < 30 and a["mode"] != "TRADER":
        archetype = "PRESERVER"
    elif hp == "DAYS" or tf == "DAILY":
        archetype = "ACTIVE_TRADER"
    elif hp == "WEEKS" or tf == "WEEKLY":
        archetype = "SWING_TRADER"
    elif hp == "MONTHS":
        archetype = "POSITIONAL_TRADER"
    else:
        archetype = "LONG_TERM_INVESTOR"
    style = {"PRESERVE": "INCOME", "INCOME": "INCOME", "BALANCED": "BALANCED", "GROWTH": "GROWTH"}[a["objective"]]
    personality = {"archetype": archetype, "style": style, "mode": a["mode"],
                   "default_view": "TRADER" if a["mode"] == "TRADER" else "INVESTOR"}

    bucket = "SHORT" if horizon < 3 else "MEDIUM" if horizon <= 7 else "LONG"
    return {
        "questionnaire_version": QUESTIONNAIRE_VERSION, "methodology_version": METHODOLOGY_VERSION,
        "answers": a,
        "scores": {"risk_capacity": cap, "risk_tolerance": tol, "risk_requirement": round(req, 1),
                   "experience": exp, "risk_score": round(risk, 1)},
        "band": band_for(risk),
        "requirement_source": req_source,
        "horizon": {"years": horizon, "bucket": bucket},
        "finances": {"monthly_surplus": round(surplus, 2), "savings_rate_pct": round(savings_rate * 100, 2),
                     "debt_to_income_pct": round(debt_ratio * 100, 2),
                     "emergency_fund_months": a["emergency_fund_months"]},
        "flags": flags,
        "biases": bias_out,
        "observed_behaviour": observed or {"status": "NOT_AVAILABLE"},
        "personality": personality,
        "explanation": {"risk_capacity": cap_expl, "risk_tolerance": tol_expl, "experience": exp_expl,
                        "risk_requirement": req_expl,
                        "risk_score": f"min(capacity {cap}, tolerance {tol})"
                                      + (" capped at 70 (NOVICE_CAP)" if any(f["code"] == "NOVICE_CAP"
                                                                               for f in flags) else "")},
    }


# ── Observed behaviour (house paper book only) ─────────────────────────────
def observed_behaviour(conn, owner: dict, days: int = 90) -> dict:
    """Round trips in the paper book, matched FIFO per symbol. Only the default
    tenant's owner has a paper book (W7 rule)."""
    if not C.owns_house_book(owner) or not C.table_exists(conn, "paper_order"):
        return {"status": "NOT_AVAILABLE", "reason": "no paper book for this owner"}
    rows = conn.execute("SELECT created_at, symbol, transaction_type, COALESCE(filled_qty, quantity), fill_price "
                        "FROM paper_order WHERE status IN ('FILLED','TRADED','COMPLETE') AND fill_price IS NOT NULL "
                        "ORDER BY created_at").fetchall()
    if not rows:
        return {"status": "INSUFFICIENT_DATA", "trades": 0}
    from datetime import datetime as _dt
    lots, trips = {}, []
    recent = 0
    cutoff = C.now() - timedelta(days=days)
    for at, sym, side, qty, px in rows:
        at = at if isinstance(at, _dt) else _dt.fromisoformat(str(at)[:19])
        if at >= cutoff:
            recent += 1
        qty = float(qty or 0)
        if str(side).upper() == "BUY":
            lots.setdefault(sym, []).append([at, qty, float(px)])
            continue
        q = lots.get(sym) or []
        while qty > 0 and q:
            lot = q[0]
            take = min(qty, lot[1])
            trips.append({"days": max(0, (at - lot[0]).days), "ret": float(px) / lot[2] - 1})
            lot[1] -= take
            qty -= take
            if lot[1] <= 1e-9:
                q.pop(0)
    out = {"status": "OK" if trips else "INSUFFICIENT_DATA", "trades_last_%dd" % days: recent,
           "round_trips": len(trips), "source": "paper_order (FIFO round trips)"}
    if trips:
        win = [t["days"] for t in trips if t["ret"] > 0]
        loss = [t["days"] for t in trips if t["ret"] <= 0]
        out["avg_holding_days"] = round(sum(t["days"] for t in trips) / len(trips), 1)
        out["avg_winner_holding_days"] = round(sum(win) / len(win), 1) if win else None
        out["avg_loser_holding_days"] = round(sum(loss) / len(loss), 1) if loss else None
        if win and loss and out["avg_loser_holding_days"] > 1.5 * max(out["avg_winner_holding_days"], 0.5):
            out["disposition_observed"] = "Losers are held much longer than winners in the paper book."
    return out


# ── Storage ────────────────────────────────────────────────────────────────
def save(conn, owner: dict, answers: dict, actor: str = "owner") -> dict:
    goal_req = None
    try:
        from wealth import goals as G                    # W13; absent before it
        goal_req = G.required_return_for_owner(conn, owner)
    except (ImportError, AttributeError):
        pass
    prof = compute(answers, observed_behaviour(conn, owner), goal_req)
    r = conn.execute("SELECT COALESCE(MAX(version),0) FROM investor_profile_version WHERE tenant_id=? AND owner_id=?",
                     (owner["tenant_id"], owner["owner_id"])).fetchone()
    version = int(r[0]) + 1
    pid = C.new_id("dna")
    at = C.now()
    conn.execute(
        "INSERT INTO investor_profile_version (profile_id,tenant_id,owner_id,version,questionnaire_version,"
        "methodology_version,answers_json,answers_hash,result_json,band,risk_score,created_at,created_by) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, owner["tenant_id"], owner["owner_id"], version, prof["questionnaire_version"],
         prof["methodology_version"], C.dumps(prof["answers"]), C.digest(prof["answers"]), C.dumps(prof),
         prof["band"], prof["scores"]["risk_score"], at, actor))
    conn.execute(
        "INSERT INTO investor_profile (tenant_id,owner_id,profile_id,version,band,risk_score,mode,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(tenant_id,owner_id) DO UPDATE SET profile_id=excluded.profile_id,"
        "version=excluded.version,band=excluded.band,risk_score=excluded.risk_score,mode=excluded.mode,"
        "updated_at=excluded.updated_at",
        (owner["tenant_id"], owner["owner_id"], pid, version, prof["band"], prof["scores"]["risk_score"],
         prof["personality"]["mode"], at))
    conn.commit()
    return current(conn, owner)


def _row_to_profile(r) -> dict:
    prof = C.loads(r["result_json"], {})
    prof.update({"profile_id": r["profile_id"], "version": r["version"], "created_at": r["created_at"],
                 "created_by": r["created_by"]})
    return prof


def current(conn, owner: dict) -> dict | None:
    r = conn.execute("SELECT v.* FROM investor_profile p JOIN investor_profile_version v ON v.profile_id=p.profile_id "
                     "WHERE p.tenant_id=? AND p.owner_id=?", (owner["tenant_id"], owner["owner_id"])).fetchone()
    if not r:
        return None
    prof = _row_to_profile(r)
    created = r["created_at"]
    from datetime import datetime as _dt
    created = created if isinstance(created, _dt) else _dt.fromisoformat(str(created)[:19])
    age_days = (C.now() - created).days
    prof["age_days"] = age_days
    prof["status"] = "STALE" if age_days > settings()["profile_validity_days"] else "CURRENT"
    return prof


def history(conn, owner: dict, limit: int = 50) -> list:
    rows = conn.execute("SELECT profile_id, version, band, risk_score, questionnaire_version, methodology_version, "
                        "created_at, created_by FROM investor_profile_version WHERE tenant_id=? AND owner_id=? "
                        "ORDER BY version DESC LIMIT ?", (owner["tenant_id"], owner["owner_id"], int(limit))).fetchall()
    return [dict(r) for r in rows]


def refresh_requirement(conn, owner: dict, actor: str = "system") -> dict | None:
    """Re-score the current answers (e.g. after goals change) as a new version.
    Returns None when there is no profile yet."""
    cur = current(conn, owner)
    if not cur:
        return None
    return save(conn, owner, cur["answers"], actor=actor)


def summary(conn, owner: dict) -> dict:
    """The small view every later wave reads: band, score, horizon, flags."""
    p = current(conn, owner)
    if not p:
        return {"status": "MISSING", "band": None, "risk_score": None}
    return {"status": p["status"], "band": p["band"], "risk_score": p["scores"]["risk_score"],
            "risk_capacity": p["scores"]["risk_capacity"], "risk_tolerance": p["scores"]["risk_tolerance"],
            "risk_requirement": p["scores"]["risk_requirement"], "horizon": p["horizon"],
            "personality": p["personality"], "flags": [f["code"] for f in p["flags"]],
            "age": p["answers"].get("age"), "profile_id": p["profile_id"], "version": p["version"],
            "monthly_surplus": p["finances"]["monthly_surplus"],
            "monthly_expenses": p["answers"].get("monthly_expenses")}
