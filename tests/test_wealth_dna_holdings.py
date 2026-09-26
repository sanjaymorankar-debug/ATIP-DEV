"""W11 Investor DNA and W12 wealth: validation, scoring rules, valuation, scope exclusions, ownership."""

import pytest

from tests._wealth_seed import ANSWERS, OTHER, OWNER, fresh
from wealth import assets as A
from wealth import dna
from wealth import holdings as H


def test_every_question_is_required_and_validated():
    with pytest.raises(ValueError, match="drop20"):
        dna.validate({**ANSWERS, "drop20": "PANIC"})
    with pytest.raises(ValueError, match="age"):
        dna.validate({**ANSWERS, "age": 12})
    with pytest.raises(ValueError, match="unknown"):
        dna.validate({**ANSWERS, "favourite_colour": "blue"})
    missing = dict(ANSWERS)
    missing.pop("horizon_years")
    with pytest.raises(ValueError, match="horizon_years"):
        dna.validate(missing)


def test_risk_score_is_min_of_capacity_and_tolerance():
    p = dna.compute(ANSWERS)
    s = p["scores"]
    assert s["risk_score"] == min(s["risk_capacity"], s["risk_tolerance"])
    assert p["band"] == dna.band_for(s["risk_score"])


def test_novice_is_capped_at_70():
    a = {**ANSWERS, "experience_years": "0", "products": "DEPOSITS", "knowledge": "BEGINNER", "drop20": "BUY_MORE",
         "tradeoff": "D", "loss_comfort": "UNBOTHERED", "max_annual_loss": "30", "age": 25, "horizon_years": 30}
    p = dna.compute(a)
    assert p["scores"]["risk_score"] <= 70 and "NOVICE_CAP" in [f["code"] for f in p["flags"]]


def test_tolerance_above_capacity_is_flagged():
    a = {**ANSWERS, "age": 70, "employment": "RETIRED", "horizon_years": 2, "liquidity_need": "HIGH",
         "emergency_fund_months": 1, "drop20": "BUY_MORE", "tradeoff": "D", "loss_comfort": "UNBOTHERED",
         "max_annual_loss": "30"}
    codes = [f["code"] for f in dna.compute(a)["flags"]]
    assert "TOLERANCE_EXCEEDS_CAPACITY" in codes and "LOW_EMERGENCY_FUND" in codes


def test_goal_requirement_overrides_the_stated_target():
    p = dna.compute(ANSWERS, goal_requirement={"required_return_pct": 16, "goals": 2})
    assert p["requirement_source"] == "GOALS" and p["scores"]["risk_requirement"] == 100


def test_every_score_is_explained():
    e = dna.compute(ANSWERS)["explanation"]
    for k in ("risk_capacity", "risk_tolerance", "experience", "risk_requirement"):
        assert e[k] and all({"factor", "input", "score", "weight", "contribution"} <= set(x) for x in e[k])


def test_profiles_are_versioned_and_owner_scoped(temp_db):
    conn, _ = fresh(temp_db)
    dna.save(conn, OWNER, ANSWERS)
    dna.save(conn, OWNER, {**ANSWERS, "age": 45})
    assert [h["version"] for h in dna.history(conn, OWNER)] == [2, 1]
    assert dna.current(conn, OTHER) is None


@pytest.mark.parametrize("cls", ["MUTUAL_FUND", "FD", "NPS", "INSURANCE"])
def test_scope_exclusions_are_refused(cls):
    with pytest.raises(ValueError, match="outside ATIP's current scope"):
        A.check_class(cls)


@pytest.mark.parametrize("sym,cls,inst", [("GOLDBEES", "GOLD", "ETF"), ("LIQUIDBEES", "CASH", "ETF"),
                                          ("NIFTYBEES", "EQUITY", "ETF"), ("GOLDIAM", "EQUITY", "STOCK"),
                                          ("JETFREIGHT", "EQUITY", "STOCK"), ("MON100", "INTL_EQUITY", "ETF"),
                                          ("RELIANCE", "EQUITY", "STOCK")])
def test_symbol_classification(sym, cls, inst):
    c = A.classify_symbol(sym)
    assert (c["asset_class"], c["instrument"]) == (cls, inst)


def test_override_wins():
    assert A.classify_symbol("XYZETF", {"XYZETF": {"asset_class": "BONDS", "instrument": "ETF"}})["asset_class"] == \
        "BONDS"


def test_valuation_and_net_worth(temp_db):
    conn, ds = fresh(temp_db)
    H.add_holding(conn, OWNER, {"asset_class": "CASH", "instrument": "BANK_ACCOUNT", "name": "Savings",
                                "quantity": 100000})
    H.add_holding(conn, OWNER, {"asset_class": "EQUITY", "instrument": "STOCK", "name": "Acme", "symbol": "ACME",
                                "quantity": 10, "avg_cost": 90})
    H.add_holding(conn, OWNER, {"asset_class": "REAL_ESTATE", "instrument": "PROPERTY", "name": "Flat", "quantity": 1,
                                "manual_price": 5_000_000})
    H.add_liability(conn, OWNER, {"kind": "HOME_LOAN", "name": "loan", "outstanding": 1_000_000})
    s = H.summary(conn, OWNER)
    acme = next(p for p in s["positions"] if p["symbol"] == "ACME")
    close = conn.execute("SELECT close FROM prices_daily WHERE symbol='ACME' ORDER BY date DESC LIMIT 1").fetchone()[0]
    assert acme["value"] == pytest.approx(10 * close, abs=0.01) and acme["price_source"] == "prices_daily"
    assert s["totals"]["net_worth"] == pytest.approx(100000 + 10 * close + 5_000_000 - 1_000_000, abs=0.05)
    assert sum(v["weight_pct"] for v in s["by_asset_class"].values()) == pytest.approx(100, abs=0.05)


def test_a_holding_without_a_price_is_valued_at_cost_and_flagged(temp_db):
    conn, _ = fresh(temp_db)
    H.add_holding(conn, OWNER, {"asset_class": "EQUITY", "instrument": "STOCK", "name": "Ghost", "symbol": "NOPRICE",
                                "quantity": 10, "avg_cost": 50})
    p = next(x for x in H.summary(conn, OWNER)["positions"] if x["symbol"] == "NOPRICE")
    assert p["value"] == 500 and p["stale"]


def test_another_owner_cannot_read_or_change_a_holding(temp_db):
    conn, _ = fresh(temp_db)
    h = H.add_holding(conn, OWNER, {"asset_class": "CASH", "instrument": "BANK_ACCOUNT", "name": "x", "quantity": 1})
    with pytest.raises(LookupError):
        H.get_holding(conn, OTHER, h["holding_id"])
    with pytest.raises(LookupError):
        H.close_holding(conn, OTHER, h["holding_id"])
    assert H.list_holdings(conn, OTHER) == []


def test_paper_book_is_never_net_worth_and_other_tenants_never_see_the_house_book(temp_db):
    conn, _ = fresh(temp_db)
    from orders.paper import ensure_tables
    ensure_tables(conn)
    conn.execute("INSERT INTO paper_position (symbol,quantity,avg_price,realized_pnl,updated_at) VALUES "
                 "('ACME',10,100,0,CURRENT_TIMESTAMP)")
    conn.commit()
    pos, _ = H.positions(conn, OWNER)
    assert any(p["source"] == "PAPER" and not p["include_in_net_worth"] for p in pos)
    other, _ = H.positions(conn, OTHER)
    assert not any(p["source"] in ("PAPER", "BROKER") for p in other)
