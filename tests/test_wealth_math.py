"""W13 / W14 / W15.5 financial math against closed forms and hand-computed cases."""

from datetime import date, timedelta

import pytest

from wealth import allocation as AL
from wealth import goals as G
from wealth.perf import metrics as M


def test_level_sip_matches_the_annuity_formula():
    r = G.monthly_rate(12)
    n = 120
    assert G.project(0, 10000, 0, 12, n) == pytest.approx(10000 * ((1 + r) ** n - 1) / r, rel=1e-9)


def test_lump_sum_compounds_monthly_to_the_annual_rate():
    assert G.project(100000, 0, 0, 10, 12) == pytest.approx(110000, rel=1e-9)


def test_solved_return_reproduces_the_input_rate():
    fv = G.project(50000, 8000, 5, 11, 180)
    assert G.solve_return(50000, 8000, 5, 180, fv) == pytest.approx(11, abs=1e-3)


def test_required_monthly_reproduces_the_input_contribution():
    fv = G.project(20000, 12345, 10, 9, 96)
    assert G.required_monthly(20000, 10, 9, 96, fv) == pytest.approx(12345, rel=1e-6)


def test_unreachable_target_returns_none():
    assert G.solve_return(0, 1, 0, 12, 1e12) is None


def test_step_up_raises_the_corpus():
    assert G.project(0, 1000, 10, 10, 120) > G.project(0, 1000, 0, 10, 120)


def test_retirement_corpus_with_zero_real_return_is_expenses_times_months():
    # post-retirement return == inflation -> the corpus is simply first withdrawal x n
    c = G.retirement_corpus(50000, 0, 6, 20, 6)
    assert c == pytest.approx(50000 * 240, rel=1e-6)


def test_monte_carlo_is_reproducible_and_bounded():
    a = G.monte_carlo(100000, 5000, 0, 10, 15, 120, 1_000_000, 500, 42)
    b = G.monte_carlo(100000, 5000, 0, 10, 15, 120, 1_000_000, 500, 42)
    assert a == b and 0 <= a["success_probability"] <= 100 and a["p10"] <= a["p50"] <= a["p90"]


def test_zero_volatility_monte_carlo_equals_the_deterministic_answer():
    fv = G.project(100000, 5000, 0, 8, 60)
    # log-normal with sigma 0 compounds at the mean exactly
    mc = G.monte_carlo(100000, 5000, 0, 8, 0.0, 60, fv * 0.999, 100, 1)
    assert mc["success_probability"] == 100.0


def test_xirr_one_year_ten_percent():
    d0 = date(2025, 1, 1)
    assert M.xirr([(d0, -1000), (d0 + timedelta(days=365), 1100)]) == pytest.approx(0.10, abs=1e-6)


def test_xirr_needs_money_in_and_out():
    assert M.xirr([(date(2025, 1, 1), -1000)]) is None


def test_twr_counts_an_intraday_round_trip():
    # bought 1000 and sold for 1050 the same session, holding nothing at the close
    assert M.twr([0.0], [1000.0], [1050.0])[0] == pytest.approx(0.05)


def test_twr_ignores_the_size_of_later_deposits():
    # +10% then +10% on a much larger base: TWR is 21% whatever the deposit
    r = M.twr([110.0, 1221.0], [100.0, 1000.0], [0.0, 0.0])
    assert M.chain(r) == pytest.approx(0.21)


def test_trade_stats():
    s = M.trade_stats([{"pnl": 10, "ret": 0.1, "days": 5}, {"pnl": -5, "ret": -0.05, "days": 3}])
    assert s["win_rate_pct"] == 50.0 and s["profit_factor"] == 2.0 and s["avg_holding_days"] == 4.0


@pytest.mark.parametrize("score", [0, 10, 25, 50, 64, 70, 90, 100])
def test_strategic_allocation_sums_to_100(score):
    assert sum(AL.strategic_for_score(score).values()) == pytest.approx(100, abs=0.05)


def test_strategic_allocation_is_monotone_in_risk():
    eq = [AL.strategic_for_score(s)["EQUITY"] for s in range(0, 101, 5)]
    assert eq == sorted(eq)


def test_normalise_respects_bounds_and_totals_100():
    b = {k: [0, 100] for k in AL.CLASSES}
    b["GOLD"] = [5, 12]
    out = AL._normalise({"EQUITY": 80, "INTL_EQUITY": 10, "BONDS": 10, "GOLD": 30, "SILVER": 0, "CASH": 0}, b)
    assert sum(out.values()) == pytest.approx(100, abs=0.01) and 5 <= out["GOLD"] <= 12


def test_cash_only_portfolio_has_cash_volatility():
    st = AL.portfolio_stats({"CASH": 100})
    assert st["volatility_pct"] == pytest.approx(1.0) and st["expected_return_pct"] == pytest.approx(6.5)


def test_horizon_caps_risk():
    assert AL.horizon_cap(2) == 20 and AL.horizon_cap(4) == 40 and AL.horizon_cap(6) == 60 and AL.horizon_cap(10) is None
