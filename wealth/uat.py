"""
Beta / user-acceptance support (W19, ATIP-UAT-001).

PERSONAS. seed(persona) builds a realistic investor in the separate tenant 'uat'
(owner 'persona_<name>'): Investor DNA answers, manual holdings, liabilities, goals
and a MANUAL ledger with a few trades. It never touches the house book (broker,
paper, OMS) or the owner's own data, and reset(persona) removes exactly the rows
that tenant / owner holds.

    conservative_retiree   66, pension, capital preservation, emergency fund, bonds / FD-like
                           (FDs excluded: held as cash), grandchild education goal
    young_accumulator      28, salaried, long horizon, aggressive, retirement + house goals
    family_planner         41, two children, home loan, education + retirement goals
    active_trader          34, trader mode, frequent trading, concentrated shares

VIEWING A PERSONA (single-user install, enterprise off): set config
    "wealth": {"uat_owner": "uat:persona_young_accumulator"}
and every /wealth page and /api/wealth call acts as that persona (the page shows a
UAT banner). It is ignored when enterprise mode is on (real principals always win) and
it only accepts owners in the 'uat' tenant, so it can never expose another tenant.

FEEDBACK. submit_feedback() / list_feedback(): testers' reports from the page
(category BUG / UX / DATA / IDEA, severity P0-P3) for triage (docs/W19_UAT_PLAN.md).
"""

from __future__ import annotations

from datetime import date, timedelta

from wealth import common as C

UAT_TENANT = "uat"
CATEGORIES = ("BUG", "UX", "DATA", "METHODOLOGY", "IDEA")
SEVERITIES = ("P0", "P1", "P2", "P3")
FEEDBACK_STATUSES = ("NEW", "TRIAGED", "FIXED", "WONT_FIX", "DUPLICATE")
OWNER_TABLES = ("investor_profile_version", "investor_profile", "wealth_holding", "wealth_liability",
                "wealth_classification", "wealth_snapshot", "wealth_goal", "wealth_goal_event", "wealth_goal_projection",
                "wealth_allocation_policy", "wealth_allocation_run", "wealth_rebalance_plan", "perf_ledger",
                "perf_ledger_void", "perf_report_run", "wealth_advice_log", "wealth_cycle_run", "wealth_preference")

_BASE = {"liquidity_need": "NONE", "objective": "BALANCED", "loss_comfort": "UNEASY", "max_annual_loss": "10",
         "target_return": "9", "experience_years": "3", "products": "LONG_TERM", "knowledge": "INTERMEDIATE",
         "trade_frequency": "RARELY", "holding_period": "YEARS", "check_frequency": "WEEKLY", "fomo": "RESEARCH",
         "loser": "REVIEW", "winner": "HOLD_PLAN", "confidence": "NEUTRAL", "after_good_year": "REBALANCE",
         "coin_flip": "20000", "mode": "INVESTOR", "drop20": "HOLD", "tradeoff": "B"}


def _in(years):
    return str(date.today() + timedelta(days=int(365.25 * years)))


PERSONAS = {
    "conservative_retiree": {
        "answers": {**_BASE, "age": 66, "dependents": 1, "employment": "RETIRED", "monthly_income": 70000,
                    "monthly_expenses": 50000, "monthly_emi": 0, "emergency_fund_months": 12, "horizon_years": 8,
                    "liquidity_need": "SOME", "drop20": "SELL_SOME", "tradeoff": "A", "objective": "INCOME",
                    "loss_comfort": "VERY_ANXIOUS", "max_annual_loss": "5", "target_return": "6"},
        "holdings": [("CASH", "BANK_ACCOUNT", "Savings + deposits (held as cash)", None, 2_500_000, None, None),
                     ("BONDS", "BOND", "GOI 7.2% 2033", None, 1500, 1000, 1018),
                     ("EQUITY", "ETF", "Nifty ETF", "NIFTYBEES", 3000, 240, None),
                     ("GOLD", "SGB", "Sovereign gold bond", None, 100, 5200, None)],
        "liabilities": [],
        "goals": [{"name": "Grandchild education", "goal_type": "CHILD_EDUCATION", "priority": "IMPORTANT",
                   "target_amount": 1_500_000, "target_date": _in(10), "current_amount": 300_000,
                   "monthly_contribution": 5000},
                  {"name": "Emergency reserve", "goal_type": "EMERGENCY_FUND", "priority": "ESSENTIAL",
                   "target_date": _in(1), "emergency_months": 12, "current_amount": 600_000}],
        "trades": []},
    "young_accumulator": {
        "answers": {**_BASE, "age": 28, "dependents": 0, "employment": "SALARIED_PRIVATE", "monthly_income": 150000,
                    "monthly_expenses": 55000, "monthly_emi": 0, "emergency_fund_months": 4, "horizon_years": 25,
                    "drop20": "BUY_MORE", "tradeoff": "D", "objective": "GROWTH", "loss_comfort": "CALM",
                    "max_annual_loss": "30", "target_return": "13", "experience_years": "1", "products": "ACTIVE",
                    "check_frequency": "DAILY", "fomo": "BUY_SOME", "confidence": "AGREE"},
        "holdings": [("CASH", "BANK_ACCOUNT", "Savings", None, 250_000, None, None),
                     ("EQUITY", "STOCK", "Reliance", "RELIANCE", 60, 1250, None),
                     ("EQUITY", "STOCK", "HDFC Bank", "HDFCBANK", 80, 1600, None),
                     ("EQUITY", "ETF", "Nifty ETF", "NIFTYBEES", 1500, 250, None),
                     ("INTL_EQUITY", "ETF", "Nasdaq 100 ETF", "MON100", 150, 190, None)],
        "liabilities": [],
        "goals": [{"name": "Retirement", "goal_type": "RETIREMENT", "priority": "ESSENTIAL", "target_date": _in(30),
                   "retirement_monthly_expense": 60000, "years_in_retirement": 25, "monthly_contribution": 25000,
                   "step_up_pct": 8},
                  {"name": "House down payment", "goal_type": "HOUSE", "priority": "IMPORTANT",
                   "target_amount": 3_000_000, "target_date": _in(6), "current_amount": 200_000,
                   "monthly_contribution": 20000}],
        "trades": [("RELIANCE", "BUY", 60, 1250, 380), ("HDFCBANK", "BUY", 80, 1600, 300)]},
    "family_planner": {
        "answers": {**_BASE, "age": 41, "dependents": 3, "employment": "SALARIED_GOVT", "monthly_income": 180000,
                    "monthly_expenses": 85000, "monthly_emi": 42000, "emergency_fund_months": 3, "horizon_years": 14},
        "holdings": [("CASH", "BANK_ACCOUNT", "Savings", None, 400_000, None, None),
                     ("EQUITY", "ETF", "Nifty ETF", "NIFTYBEES", 4000, 230, None),
                     ("GOLD", "PHYSICAL_GOLD", "Jewellery / coins (investment part)", None, 120, 6000, None),
                     ("REAL_ESTATE", "PROPERTY", "Home", None, 1, 7_000_000, 9_500_000)],
        "liabilities": [("HOME_LOAN", "Home loan", 3_800_000, 8.5, 42000)],
        "goals": [{"name": "Elder child college", "goal_type": "CHILD_EDUCATION", "priority": "ESSENTIAL",
                   "target_amount": 2_500_000, "target_date": _in(7), "current_amount": 400_000,
                   "monthly_contribution": 15000},
                  {"name": "Younger child college", "goal_type": "CHILD_EDUCATION", "priority": "ESSENTIAL",
                   "target_amount": 2_500_000, "target_date": _in(11), "current_amount": 150_000,
                   "monthly_contribution": 10000},
                  {"name": "Retirement", "goal_type": "RETIREMENT", "priority": "IMPORTANT", "target_date": _in(19),
                   "retirement_monthly_expense": 75000, "monthly_contribution": 15000, "step_up_pct": 5}],
        "trades": []},
    "active_trader": {
        "answers": {**_BASE, "age": 34, "dependents": 1, "employment": "SELF_EMPLOYED", "monthly_income": 250000,
                    "monthly_expenses": 90000, "monthly_emi": 20000, "emergency_fund_months": 6, "horizon_years": 5,
                    "drop20": "BUY_MORE", "tradeoff": "D", "objective": "GROWTH", "loss_comfort": "UNBOTHERED",
                    "max_annual_loss": "30", "target_return": "18", "experience_years": "6", "products": "DERIVATIVES",
                    "knowledge": "ADVANCED", "trade_frequency": "DAILY", "holding_period": "DAYS",
                    "check_frequency": "INTRADAY", "fomo": "BUY_BIG", "loser": "WAIT_BREAKEVEN", "winner": "SELL_ALL",
                    "confidence": "STRONGLY_AGREE", "after_good_year": "ALL_IN", "mode": "TRADER"},
        "holdings": [("CASH", "BANK_ACCOUNT", "Trading balance", None, 300_000, None, None),
                     ("EQUITY", "STOCK", "Tata Motors", "TATAMOTORS", 900, 700, None),
                     ("EQUITY", "STOCK", "Adani Ent", "ADANIENT", 250, 2400, None)],
        "liabilities": [("PERSONAL_LOAN", "Personal loan", 400_000, 13.5, 20000)],
        "goals": [{"name": "Trading capital buffer", "goal_type": "WEALTH_CREATION", "priority": "ASPIRATIONAL",
                   "target_amount": 2_000_000, "target_date": _in(3), "current_amount": 300_000,
                   "monthly_contribution": 30000}],
        "trades": [("TATAMOTORS", "BUY", 900, 700, 120), ("ADANIENT", "BUY", 300, 2400, 90),
                   ("ADANIENT", "SELL", 50, 2350, 60)]},
}


def owner_for(persona: str) -> dict:
    if persona not in PERSONAS:
        raise ValueError(f"persona must be one of {sorted(PERSONAS)}")
    return {"tenant_id": UAT_TENANT, "owner_id": f"persona_{persona}"}


def reset(conn, persona: str) -> dict:
    o = owner_for(persona)
    n = {}
    for t in OWNER_TABLES:
        if C.table_exists(conn, t):
            n[t] = conn.execute(f"DELETE FROM {t} WHERE tenant_id=? AND owner_id=?",           # t: fixed table list
                                (o["tenant_id"], o["owner_id"])).rowcount
    conn.commit()
    return {"owner": o, "deleted": {k: v for k, v in n.items() if v}}


def seed(conn, persona: str) -> dict:
    from wealth import dna, goals, holdings
    from wealth.perf import ledger
    o = owner_for(persona)                      # validates the name
    p = PERSONAS[persona]
    reset(conn, persona)
    dna.save(conn, o, p["answers"], actor="uat_seed")
    for cls, inst, name, sym, qty, cost, px in p["holdings"]:
        b = {"asset_class": cls, "instrument": inst, "name": name, "quantity": qty}
        if sym:
            b["symbol"] = sym
        if cost is not None:
            b["avg_cost"] = cost
        if px is not None:
            b["manual_price"] = px
            b["valuation"] = "MANUAL"
        if inst == "SGB":
            b["valuation"] = "GOLD_SPOT"
        holdings.add_holding(conn, o, b, actor="uat_seed")
    for kind, name, out, rate, emi in p["liabilities"]:
        holdings.add_liability(conn, o, {"kind": kind, "name": name, "outstanding": out, "interest_pct": rate,
                                         "emi": emi}, actor="uat_seed")
    for g in p["goals"]:
        goals.create(conn, o, g, actor="uat_seed")
    for sym, side, qty, px, days in p["trades"]:
        td = date.today() - timedelta(days=days)
        while td.weekday() >= 5:
            td -= timedelta(days=1)
        try:
            ledger.add_manual(conn, o, {"portfolio": "MANUAL", "trade_date": str(td), "kind": side, "symbol": sym,
                                        "quantity": qty, "price": px, "fees": round(qty * px * 0.0012, 2)},
                              actor="uat_seed")
        except ValueError:
            pass
    dna.refresh_requirement(conn, o, actor="uat_seed")
    return {"persona": persona, "owner": o, "holdings": len(p["holdings"]), "goals": len(p["goals"]),
            "trades": len(p["trades"])}


def personas(conn) -> list:
    out = []
    for name in PERSONAS:
        o = owner_for(name)
        r = conn.execute("SELECT band, risk_score, updated_at FROM investor_profile WHERE tenant_id=? AND owner_id=?",
                         (o["tenant_id"], o["owner_id"])).fetchone()
        out.append({"persona": name, "owner": f"{o['tenant_id']}:{o['owner_id']}", "seeded": bool(r),
                    "band": r[0] if r else None, "updated_at": r[2] if r else None})
    return out


def uat_owner_from_config() -> dict | None:
    """config wealth.uat_owner 'uat:<owner_id>' -> owner; anything outside the uat tenant is ignored."""
    from wealth.config import settings
    v = settings().get("uat_owner")
    if not isinstance(v, str) or ":" not in v:
        return None
    t, _, o = v.partition(":")
    if t != UAT_TENANT or not o or len(o) > 80:
        return None
    return {"tenant_id": t, "owner_id": o}


# ── Feedback ───────────────────────────────────────────────────────────────
def submit_feedback(conn, owner, b: dict, actor="owner") -> dict:
    unknown = set(b) - {"page", "category", "severity", "message", "context"}
    if unknown:
        raise ValueError(f"unknown fields {sorted(unknown)}")
    cat = str(b.get("category") or "").upper()
    if cat not in CATEGORIES:
        raise ValueError(f"category must be one of {list(CATEGORIES)}")
    sev = str(b.get("severity") or "P3").upper()
    if sev not in SEVERITIES:
        raise ValueError(f"severity must be one of {list(SEVERITIES)}")
    fid = C.new_id("fb")
    conn.execute("INSERT INTO wealth_feedback (feedback_id,tenant_id,owner_id,created_at,created_by,page,category,"
                 "severity,message,context_json,status) VALUES (?,?,?,?,?,?,?,?,?,?,'NEW')",
                 (fid, owner["tenant_id"], owner["owner_id"], C.now(), actor, C.text(b.get("page"), "page", 40,
                                                                                    required=False),
                  cat, sev, C.text(b.get("message"), "message", 2000), C.dumps(b.get("context") or {})[:4000]))
    conn.commit()
    return {"feedback_id": fid, "status": "NEW"}


def list_feedback(conn, owner=None, status=None, limit=200) -> list:
    q, args = "SELECT * FROM wealth_feedback WHERE 1=1", []
    if owner:
        q += " AND tenant_id=? AND owner_id=?"
        args += [owner["tenant_id"], owner["owner_id"]]
    if status:
        q += " AND status=?"
        args.append(status)
    return [dict(r) for r in conn.execute(q + " ORDER BY severity, created_at DESC LIMIT ?", (*args, int(limit)))]


def triage(conn, fid, status, note=None, actor="owner") -> dict:
    st = str(status or "").upper()
    if st not in FEEDBACK_STATUSES:
        raise ValueError(f"status must be one of {list(FEEDBACK_STATUSES)}")
    n = conn.execute("UPDATE wealth_feedback SET status=?, triage_note=?, triaged_at=?, triaged_by=? WHERE "
                     "feedback_id=?", (st, C.text(note, "note", 500, required=False), C.now(), actor, fid)).rowcount
    conn.commit()
    if not n:
        raise LookupError("feedback not found")
    return {"feedback_id": fid, "status": st}
