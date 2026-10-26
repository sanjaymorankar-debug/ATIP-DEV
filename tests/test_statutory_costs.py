"""
P1-5: one statutory cost model (backtest/costs.py) for backtests, paper fills and paper options.

Before: the paper book charged a flat 0.05% a leg (under half the statutory delivery cost, no DP
charge); the DP charge defaulted to 0; the paper options book charged brokerage and a 0.1% sell STT
only; and the "0.25% round trip" was charged on both legs by strategy/aggressive.py (~0.5%) but once by
scores/backtest.py.
"""

import json
from datetime import date, timedelta

import pytest

from backtest import costs as C


@pytest.fixture
def no_config(tmp_path, monkeypatch):
    """No config.json anywhere: every model is the shipped default."""
    import backtest.config as BC
    import orders.paper as P
    monkeypatch.setattr(BC, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(P, "CONFIG_PATH", tmp_path / "config.json")
    return tmp_path / "config.json"


# ── equity: paper fills ──────────────────────────────────────────────────────

def test_paper_fills_pay_the_statutory_model_and_the_dp_charge(temp_db, no_config, monkeypatch):
    from db.schema import get_connection, init_db
    from orders import paper as P
    init_db()
    monkeypatch.setattr(P.PaperBroker, "_ltp", lambda self, s: 1000.0)
    b = P.PaperBroker(conn=get_connection())
    b.reset(100000.0)
    buy = b.get_order_by_id(b.place_order(transaction_type="BUY", quantity=10, symbol="ACME")["data"]["orderId"])["data"]
    sell = b.get_order_by_id(b.place_order(transaction_type="SELL", quantity=10, symbol="ACME")["data"]["orderId"])["data"]
    m = C.cost_model("nse_delivery")
    assert (buy["fill_price"], sell["fill_price"]) == (1000.5, 999.5)              # 5 bps adverse, as before
    assert buy["brokerage"] == round(m.total("BUY", 10005.0), 2) == 11.87          # STT, stamp, exchange, SEBI, GST
    assert sell["brokerage"] == round(m.total("SELL", 9995.0), 2) == 26.29         # ... and the DP charge
    assert m.charges("SELL", 9995.0)["dp"] == 15.93
    assert buy["brokerage"] + sell["brokerage"] > 0.0005 * (10005 + 9995) * 3, "the flat 0.05% a leg understated it"
    assert b.balance == pytest.approx(100000 - 10005 - 11.87 + 9995 - 26.29)
    b.close()


def test_the_dp_charge_is_set_once_for_backtests_and_paper(no_config):
    from orders.paper import paper_cost_model, settings
    assert C.cost_model("nse_delivery").dp_charge_per_sell == C.DP_CHARGE_PER_SELL == 15.93
    assert C.cost_model("nse_intraday").dp_charge_per_sell == C.cost_model("zero").dp_charge_per_sell == 0
    assert paper_cost_model(settings()) == C.cost_model_from_config() == C.cost_model("nse_delivery")
    no_config.write_text(json.dumps({"backtest": {"cost_overrides": {"dp_charge_per_sell": 14.75}}}), encoding="utf-8")
    assert C.cost_model_from_config().dp_charge_per_sell == 14.75
    assert paper_cost_model(settings()).charges("SELL", 10000)["dp"] == 14.75, "paper follows the backtest setting"
    assert paper_cost_model(settings(), "MIS").name == "nse_intraday"
    assert paper_cost_model(settings({"paper_cost_model": "zero"})).total("SELL", 10000) == 0.0


def test_a_rebalance_plan_estimates_the_same_charges(temp_db, no_config):
    from orders.paper import ensure_tables
    from portfolio.optimize import rebalance_plan
    from tests._wealth_seed import fresh
    conn, _ = fresh(temp_db)
    ensure_tables(conn)
    conn.execute("INSERT OR REPLACE INTO paper_account (id, balance, opened_at) VALUES (1, 100000, '2025-01-01')")
    conn.commit()
    plan = rebalance_plan(conn, {"ACME": 1.0}, store=False)
    t = next(x for x in plan["trades"] if x["symbol"] == "ACME")
    v = t["trade_value"]
    assert t["est_cost"] == pytest.approx(C.cost_model("nse_delivery").total("BUY", v) + v * 0.0005, abs=0.01)
    assert plan["summary"]["est_costs"] == pytest.approx(t["est_cost"], abs=0.01)
    conn.close()


# ── options ──────────────────────────────────────────────────────────────────

def test_an_option_leg_pays_every_statutory_charge():
    m = C.options_cost_model()
    buy, sell = m.charges("BUY", 10000), m.charges("SELL", 10000)
    assert (buy["brokerage"], buy["stt"], buy["exchange"], buy["sebi"], buy["stamp"]) == (20.0, 0.0, 3.503, 0.01, 0.3)
    assert buy["gst"] == pytest.approx(0.18 * (20 + 3.503 + 0.01), abs=1e-4)
    assert (sell["stt"], sell["stamp"]) == (15.0, 0.0)                     # 0.15% of the sell premium
    assert m.exercise(10000)["total"] == pytest.approx(20 + 15 + 3.6)        # STT on the intrinsic value
    assert m.assignment(10000)["total"] == pytest.approx(23.6)              # a writer pays no STT
    assert m.charges("BUY", 0)["total"] == m.exercise(0)["total"] == 0.0
    assert C.options_cost_model("zero").total("SELL", 10000) == 0.0
    with pytest.raises(ValueError):
        C.options_cost_model("nse_options", {"dp_charge_per_sell": 1})


def test_the_paper_options_book_charges_the_model_and_exercise_stt(temp_db, monkeypatch):
    from db.schema import get_connection, init_db
    from execution import options_paper as O
    init_db()
    db = get_connection()
    s = {**O.DEFAULTS, "enabled": True}
    monkeypatch.setattr(O, "settings", lambda: s)
    assert O._fees("BUY", 10000, s) == 28.05 and O._fees("SELL", 10000, s) == 42.75      # were 20 and 30
    assert O._fees("SELL", 10000, s, settle=True) == 38.6 and O._fees("BUY", 10000, s, settle=True) == 23.6
    assert O._fees("SELL", 10000, {**s, "sell_stt_pct": 0.1}) == 37.75, "an owner's sell_stt_pct still applies"
    O.ensure_tables(db)
    db.execute("INSERT OR REPLACE INTO paper_account (id,balance,opened_at) VALUES (1, 1000000, '2025-01-01')")
    exp = date.today() + timedelta(days=10)
    db.execute("INSERT INTO fo_contract_daily (date,symbol,instrument,expiry,strike,option_type,close,settle,oi,volume,"
               "underlying,lot_size) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
               (str(date.today()), "ACME", "STO", str(exp), 100.0, "CE", 8.0, 8.0, 1000, 500, 105.0, 50))
    db.commit()
    r = O.place(db, {"underlying": "ACME", "expiry": str(exp), "strike": 100, "option_type": "CE", "side": "BUY",
                     "lots": 1})
    assert r["fees"] == O._fees("BUY", r["qty"] * r["price"], s)
    db.execute("UPDATE paper_options_position SET expiry=?", (str(date.today() - timedelta(days=1)),))
    db.execute("INSERT INTO fo_underlying_daily (date,symbol,kind,underlying_price) VALUES (?,?,?,?)",
               (str(date.today() - timedelta(days=1)), "ACME", "STOCK", 112.0))
    db.commit()
    assert O.settle_expired(conn=db)["rows"] == 1
    fees = db.execute("SELECT fees FROM paper_options_trade WHERE reason LIKE 'EXPIRY%'").fetchone()[0]
    assert fees == round(C.options_cost_model().exercise(50 * 12.0)["total"], 2) == 24.5     # was 20 + 0.1% sell STT
    db.close()


# ── the 0.25% round trip, charged one way ────────────────────────────────────

def test_the_round_trip_is_charged_one_way_everywhere():
    from research import exit_target_study as S
    from scores import backtest as SB
    from strategy import aggressive as A, backtest_history as BH
    assert C.round_trip_cost(100, 103, 0.25) == pytest.approx(0.25375)     # half on 100 in, half on 103 out
    assert A.realised_pnl(100.0, 103.0, 1000, cost_pct=0.25) == 2746.25    # was 3000 - 507.50
    assert SB.net_of_costs(3.0, 0.25) == pytest.approx(A.realised_pnl(100.0, 103.0, 1000, cost_pct=0.25) / 1000)
    assert SB.net_of_costs(-3.0, 0.25) == pytest.approx(A.realised_pnl(100.0, 97.0, 1000, cost_pct=0.25) / 1000)
    assert SB.COST_PCT == BH.DEFAULT_COST_PCT == S.COST_PCT == C.ROUND_TRIP_PCT == 0.25
