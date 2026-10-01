"""W13 goals, W14 allocation, W15 rebalancing, W16 advisor, W17 cycle -- on the synthetic market."""

from datetime import date, timedelta

import pytest

from tests._wealth_seed import ANSWERS, OTHER, OWNER, fresh
from wealth import advisor as ADV
from wealth import allocation as AL
from wealth import dna
from wealth import goals as G
from wealth import holdings as H
from wealth import integrated as INT
from wealth import rebalance as RB


@pytest.fixture
def book(temp_db):
    conn, ds = fresh(temp_db)
    dna.save(conn, OWNER, ANSWERS)
    H.add_holding(conn, OWNER, {"asset_class": "CASH", "instrument": "BANK_ACCOUNT", "name": "Savings",
                                "quantity": 600000})
    H.add_holding(conn, OWNER, {"asset_class": "EQUITY", "instrument": "STOCK", "name": "Acme", "symbol": "ACME",
                                "quantity": 1000, "avg_cost": 90})
    return conn


def _in(years):
    return (date.today() + timedelta(days=int(365.25 * years))).isoformat()


def test_goal_evaluation_fields(book):
    g = G.create(book, OWNER, {"name": "House", "goal_type": "HOUSE", "priority": "ESSENTIAL", "target_amount": 5e6,
                               "target_date": _in(8), "current_amount": 5e5, "monthly_contribution": 30000})
    e = g["evaluation"]
    assert e["future_target"] == pytest.approx(5e6 * 1.07 ** e["years_left"], rel=1e-3)   # house inflation 7%
    assert e["status"] in ("ON_TRACK", "AT_RISK", "OFF_TRACK") and len(e["scenarios"]) == 5
    assert e["assumptions"]["return_source"].startswith("W14 strategic allocation")


def test_past_target_date_is_refused(book):
    with pytest.raises(ValueError, match="future"):
        G.create(book, OWNER, {"name": "x", "target_amount": 1, "target_date": "2020-01-01"})


def test_emergency_fund_uses_dna_expenses_and_liquid_return(book):
    g = G.create(book, OWNER, {"name": "EF", "goal_type": "EMERGENCY_FUND", "priority": "ESSENTIAL",
                               "target_date": _in(1), "emergency_months": 6})
    e = g["evaluation"]
    assert e["target_today"] == pytest.approx(6 * ANSWERS["monthly_expenses"])
    assert e["assumptions"]["return_source"] == "liquid assets (emergency fund)"


def test_short_goal_without_allocation_uses_a_capped_band(temp_db):
    conn, _ = fresh(temp_db)
    dna.save(conn, OWNER, ANSWERS)
    import wealth.allocation as alloc_mod
    orig = alloc_mod.expected_for_owner
    alloc_mod.expected_for_owner = lambda *a, **k: None
    try:
        g = G.create(conn, OWNER, {"name": "Car", "goal_type": "VEHICLE", "target_amount": 8e5, "target_date": _in(2)})
    finally:
        alloc_mod.expected_for_owner = orig
    assert "capped to CONSERVATIVE" in g["evaluation"]["assumptions"]["return_source"]


def test_goals_feed_the_dna_requirement(book):
    G.create(book, OWNER, {"name": "Retire", "goal_type": "RETIREMENT", "priority": "ESSENTIAL",
                           "target_date": _in(20), "retirement_monthly_expense": 80000, "monthly_contribution": 20000})
    p = dna.refresh_requirement(book, OWNER)
    assert p["requirement_source"] == "GOALS" and p["version"] == 2


def test_allocation_run_is_explained_and_totals_100(book):
    r = AL.run(book, OWNER)
    assert sum(r["target"].values()) == pytest.approx(100, abs=0.01)
    assert [s["step"] for s in r["steps"]][:3] == ["strategic", "tactical", "constraints"]
    assert any(s["status"] == "OK" for s in r["signals"])
    for k, (lo, hi) in r["bounds"].items():
        assert lo - 1e-6 <= r["target"][k] <= hi + 1e-6


def test_risk_guard_keeps_the_bad_year_within_the_stated_loss(temp_db):
    conn, _ = fresh(temp_db)
    dna.save(conn, OWNER, {**ANSWERS, "max_annual_loss": "5"})
    r = AL.compute(conn, OWNER)
    assert r["risk_guard"]["within_limit"] or r["risk_guard"]["points_moved"] > 0


def test_policy_exclusion_and_bounds(book):
    AL.set_policy(book, OWNER, {"bounds": {"GOLD": [5, 6]}, "excluded_classes": ["SILVER"], "tactical_enabled": False})
    t = AL.compute(book, OWNER)["target"]
    assert t["SILVER"] == 0 and 5 <= t["GOLD"] <= 6
    with pytest.raises(ValueError):
        AL.set_policy(book, OWNER, {"bounds": {"MUTUAL_FUND": [0, 5]}})


def test_rebalance_detects_the_cash_heavy_book_and_cash_flow_never_sells(book):
    chk = RB.check_public(book, OWNER)
    assert chk["verdict"] == "REBALANCE" and any(d["out_of_band"] for d in chk["drift"])
    p = RB.plan(book, OWNER, "cash_flow", 200000)
    assert p["trades"] and all(t["side"] in ("BUY", "ADD") for t in p["trades"])
    assert "Tax-neutral" in p["tax_note"] and "no order" in p["execution_note"]


def test_to_target_leaves_little_drift(book):
    p = RB.plan(book, OWNER, "to_target")
    assert p["after"]["max_drift_pp"] < 3


def test_plan_decisions_are_one_way(book):
    p = RB.plan(book, OWNER, "to_band")
    RB.decide(book, OWNER, p["plan_id"], "DISMISSED")
    with pytest.raises(ValueError):
        RB.decide(book, OWNER, p["plan_id"], "ACCEPTED")
    with pytest.raises(LookupError):
        RB.get_plan(book, OTHER, p["plan_id"])


def test_advisor_claims_carry_evidence_and_never_trade(book):
    for q in ADV.suggested_questions():
        r = ADV.ask(book, OWNER, q, narrate=False)
        # every claim carries evidence; with nothing to go on (no goals, no stored report)
        # the advisor says so instead of producing claims
        assert all(c["evidence"] for c in r["claims"])
        assert r["claims"] or any("no data" in a for a in r["answer"])
        assert "cannot place" in r["trading_note"]
        assert r["narration"]["status"] == "OFF"


def test_advisor_blocks_risky_advice_without_a_profile(temp_db):
    conn, _ = fresh(temp_db)
    H.add_holding(conn, OTHER, {"asset_class": "CASH", "instrument": "BANK_ACCOUNT", "name": "x", "quantity": 1e6})
    r = ADV.ask(conn, OTHER, "Tell me about ACME", narrate=False)
    # ACME carries a BUY signal: its sizing suggestion raises risk, so without a DNA it is blocked
    assert r["recommendations"]
    assert all(x["suitability"] == "BLOCKED_NO_PROFILE" for x in r["recommendations"])


def test_narration_failure_falls_back(book, monkeypatch):
    from wealth import config
    monkeypatch.setattr(config, "settings", lambda: {**config.DEFAULTS, "advisor_llm_enabled": True,
                                                     "monte_carlo_paths": 200})
    monkeypatch.setattr(ADV, "settings", config.settings)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    anthropic = pytest.importorskip("anthropic")

    class Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("network down")
    monkeypatch.setattr(anthropic, "Anthropic", Boom)
    r = ADV.ask(book, OWNER, "How am I doing overall?")
    assert r["narration"]["status"] == "ERROR" and r["claims"]


def test_investor_cycle_runs_every_step(book):
    r = INT.investor_cycle(book, OWNER, alerts=False)
    assert r["status"] == "SUCCESS", r["failed_steps"]
    assert set(r["steps"]) == {"snapshot", "ledger", "dna", "allocation", "goals", "rebalance", "performance", "alerts"}


def test_scheduled_cycle_is_off_by_default():
    assert INT.run_scheduled()["status"] == "SKIPPED"


def test_signal_suitability(book):
    s = INT.signal_suitability(book, OWNER, "ACME")
    assert s["verdict"] in ("SUITABLE", "CAUTION", "NOT_SUITABLE") and s["checks"]


def test_mode_preference(book):
    assert INT.set_mode(book, OWNER, "TRADER")["mode"] == "TRADER"
    with pytest.raises(ValueError):
        INT.set_mode(book, OWNER, "GAMBLER")
