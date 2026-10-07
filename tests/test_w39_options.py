"""
W39 (OP): options strategy builder -- payoff, breakevens, bounded / unbounded risk, Greeks,
probability of profit, templates, pricing from stored chains, and the API.
"""
import datetime as dt

import pytest

from quant import options_strategy as O
from quant.derivatives import bs_price

TODAY = dt.date(2026, 10, 7)
EXP = "2026-10-29"


def _priced(legs, spot=24000.0, iv=0.15):
    T = (dt.date.fromisoformat(EXP) - TODAY).days / 365
    for l in legs:
        if l["kind"] != "FUT":
            l["premium"] = round(bs_price(spot, l["strike"], T, O.RISK_FREE, iv,
                                          "call" if l["kind"] == "CE" else "put"), 2)
    return legs


def test_long_call_payoff_breakeven_and_unlimited_profit():
    a = O.analyse([{"kind": "CE", "side": "BUY", "strike": 24000, "expiry": EXP, "premium": 300}], 24000,
                  lot_size=75, as_of=TODAY)
    assert a["breakevens"] == [24300.0]
    assert a["unlimited_profit"] and a["max_loss"] == pytest.approx(-300 * 75)
    assert a["net_premium"] == pytest.approx(-22500) and a["premium_type"] == "debit"
    assert a["greeks"]["delta"] > 0 and a["greeks"]["theta"] < 0 and a["greeks"]["vega"] > 0


def test_short_straddle_has_unlimited_loss_and_two_breakevens():
    legs = [{"kind": "CE", "side": "SELL", "strike": 24000, "expiry": EXP, "premium": 300},
            {"kind": "PE", "side": "SELL", "strike": 24000, "expiry": EXP, "premium": 280}]
    a = O.analyse(legs, 24000, lot_size=1, as_of=TODAY)
    assert a["unlimited_loss"] and a["max_profit"] == pytest.approx(580)
    assert a["breakevens"] == [23420.0, 24580.0]
    assert a["greeks"]["theta"] > 0 and a["greeks"]["vega"] < 0


def test_iron_condor_risk_is_wing_width_minus_credit():
    legs = _priced(O.build("iron_condor", 24000, EXP, symbol="NIFTY"))
    a = O.analyse(legs, 24000, lot_size=75, as_of=TODAY)
    credit = a["net_premium"] / 75
    width = legs[1]["strike"] - legs[0]["strike"]
    assert a["max_profit"] == pytest.approx(credit * 75, abs=1)
    assert a["max_loss"] == pytest.approx(-(width - credit) * 75, abs=1)
    assert len(a["breakevens"]) == 2 and not a["unlimited_loss"] and not a["unlimited_profit"]
    assert a["reward_to_risk"] == pytest.approx(a["max_profit"] / -a["max_loss"], abs=0.01)


def test_every_template_builds_and_analyses():
    for key in O.TEMPLATES:
        legs = _priced(O.build(key, 24130, EXP, symbol="NIFTY"))
        a = O.analyse(legs, 24130, lot_size=75, as_of=TODAY)
        assert 0 <= a["probability_of_profit_pct"] <= 100, key
        assert len(a["payoff"]["spot"]) == len(a["payoff"]["expiry"]) == len(a["payoff"]["target"])
    assert O.build("long_straddle", 24130, EXP, symbol="NIFTY")[0]["strike"] == 24150, "ATM on the 50 grid"
    with pytest.raises(O.StrategyError):
        O.build("nope", 24000, EXP)


def test_unbounded_cases_are_flagged():
    for key, profit, loss in (("short_call", False, True), ("call_ratio_spread", False, True),
                              ("long_strangle", True, False), ("covered_call", False, False),
                              ("bull_call_spread", False, False)):
        a = O.analyse(_priced(O.build(key, 24000, EXP, symbol="NIFTY")), 24000, as_of=TODAY)
        assert (a["unlimited_profit"], a["unlimited_loss"]) == (profit, loss), key


def test_probability_of_profit_is_sensible():
    deep_itm = O.analyse([{"kind": "PE", "side": "SELL", "strike": 20000, "expiry": EXP, "premium": 5}], 24000,
                         as_of=TODAY)
    atm_long = O.analyse([{"kind": "CE", "side": "BUY", "strike": 24000, "expiry": EXP, "premium": 300}], 24000,
                         as_of=TODAY)
    assert deep_itm["probability_of_profit_pct"] > 95
    assert 20 < atm_long["probability_of_profit_pct"] < 50


def test_value_before_expiry_sits_above_expiry_for_a_long_option():
    a = O.analyse([{"kind": "CE", "side": "BUY", "strike": 24000, "expiry": EXP, "premium": 300, "iv": 0.15}],
                  24000, as_of=TODAY, target_date=TODAY)
    i = a["payoff"]["spot"].index(24000.0)
    assert a["payoff"]["target"][i] > a["payoff"]["expiry"][i], "time value"


def test_missing_iv_is_implied_from_the_premium():
    a = O.analyse([{"kind": "CE", "side": "BUY", "strike": 24000, "expiry": EXP,
                    "premium": round(bs_price(24000, 24000, 22 / 365, O.RISK_FREE, 0.2, "call"), 2)}],
                  24000, as_of=TODAY)
    assert a["legs"][0]["iv"] == pytest.approx(0.2, abs=0.002) and a["legs"][0]["iv_source"] == "implied from premium"


def test_validation_errors():
    with pytest.raises(O.StrategyError):
        O.analyse([], 24000)
    with pytest.raises(O.StrategyError):
        O.analyse([{"kind": "XX", "side": "BUY", "strike": 1, "expiry": EXP, "premium": 1}], 24000)
    with pytest.raises(O.StrategyError):
        O.analyse([{"kind": "CE", "side": "BUY", "strike": 24000, "expiry": EXP}], 24000), "no premium"
    with pytest.raises(O.StrategyError):
        O.analyse([{"kind": "CE", "side": "BUY", "strike": 24000, "expiry": "2020-01-01", "premium": 1}], 24000,
                  as_of=TODAY)


def test_premiums_come_from_the_stored_chain(temp_db):
    from db.schema import get_connection, init_db
    init_db()
    conn = get_connection()
    try:
        conn.execute("INSERT INTO option_chain_snapshot (ts, symbol, expiry, strike, option_type, ltp, iv, bid, ask, "
                     "underlying) VALUES ('2026-10-07 10:00:00','NIFTY',?,24000,'CE',305,14.5,300,302,24010)", (EXP,))
        conn.execute("INSERT INTO fo_contract_daily (date, symbol, instrument, expiry, strike, option_type, settle, "
                     "lot_size, underlying) VALUES ('2026-10-06','NIFTY','OPTIDX',?,24000,'PE',250,75,23990)", (EXP,))
        conn.commit()
        q = O.chain_quotes(conn, "nifty")
    finally:
        conn.close()
    assert q["lot_size"] == 75 and q["spot"] == 24010
    legs = O.price_legs([{"kind": "CE", "side": "BUY", "strike": 24000, "expiry": EXP},
                         {"kind": "PE", "side": "BUY", "strike": 24000, "expiry": EXP},
                         {"kind": "CE", "side": "BUY", "strike": 25000, "expiry": EXP}], q["quotes"])
    assert legs[0]["premium"] == 301.0 and legs[0]["iv"] == pytest.approx(0.145)
    assert legs[1]["premium"] == 250 and "bhavcopy" in legs[1]["price_source"]
    assert legs[2].get("premium") is None


@pytest.fixture
def api(tmp_path, monkeypatch, temp_db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    from db.schema import init_db
    init_db()
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    return TestClient(server.app), security


def test_options_api(api):
    client, security = api
    h = {security.TOKEN_HEADER: security.token()}
    assert len(client.get("/api/options/templates").json()) == len(O.TEMPLATES)
    exp = str(dt.date.today() + dt.timedelta(days=21))
    assert client.post("/api/options/build", json={"template": "iron_condor", "spot": 24000, "expiry": exp}
                       ).status_code == 401
    r = client.post("/api/options/build", headers=h,
                    json={"template": "iron_condor", "spot": 24000, "expiry": exp, "symbol": "NIFTY", "lot_size": 75})
    assert r.status_code == 200, r.text
    b = r.json()
    assert len(b["legs"]) == 4 and all("model" in l["price_source"] for l in b["legs"])
    r2 = client.post("/api/options/analyse", headers=h, json={"spot": 24000, "legs": b["legs"], "lot_size": 75})
    assert r2.status_code == 200 and r2.json()["max_loss"] == b["max_loss"]
    bad = client.post("/api/options/analyse", headers=h, json={"spot": 24000, "legs": []})
    assert bad.status_code == 400 and "leg" in bad.json()["error"]
    assert client.get("/options-builder").status_code == 200
