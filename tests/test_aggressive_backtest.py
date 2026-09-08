"""
Scenario replay tests.

These scenarios are BEHAVIOUR fixtures, not evidence about profitability. The
price paths are hand-written to exercise each branch, so their win rate and
profit factor describe the paths I chose, nothing more. Assertions here are
about mechanics — did the partial book, did the checkpoint hold the runner, did
the stop fire — never about the strategy being good.

Measured performance belongs to `backtest_on_history()`, which uses real bars.
"""

import pytest

from strategy import aggressive as A
from strategy import backtest_aggressive as B


@pytest.fixture
def result(cfg):
    return B.run(cfg, cost_pct=0.25)


def test_all_seven_scenarios_run(result):
    assert len(result["scenarios"]) == 7


def test_stop_before_target_exits_whole_position(cfg):
    t = B.scenarios(cfg)["1_stop_before_target"]
    assert t.exit_reason == A.INITIAL_STOP_LOSS
    assert t.t1_hit is False
    assert t.realized < 0


def test_partial_then_reversal_keeps_the_partial_profit(cfg):
    t = B.scenarios(cfg)["2_partial_then_reversal"]
    assert t.t1_hit is True
    assert t.exit_reason == A.TRAILING_STOP
    # Booking half at +3% is what turns a round trip back to the entry into a
    # net gain; without it this path would be flat to negative.
    assert t.realized > 0


def test_strong_momentum_lets_the_runner_pass_target_2(cfg):
    t = B.scenarios(cfg)["3_runner_runs"]
    assert t.t2_reached is True
    assert t.t2_verdict == A.HOLD_RUNNER
    assert t.exit_reason == A.TRAILING_STOP
    assert t.peak_pct > 6.0, "the runner must be allowed above +6%"


def test_weak_momentum_banks_at_the_checkpoint(cfg):
    t = B.scenarios(cfg)["4_weak_at_checkpoint"]
    assert t.t2_verdict == A.EXIT_RUNNER
    assert t.exit_reason == A.TARGET_2_MOMENTUM_EXIT


def test_multiple_positions_are_independent(cfg):
    trades = B.scenarios(cfg)["6_multiple_positions"]
    assert len({t.symbol for t in trades}) == 3
    assert any(t.realized > 0 for t in trades)
    assert any(t.realized < 0 for t in trades)


def test_gap_through_both_targets_still_books_the_partial(cfg):
    t = B.scenarios(cfg)["7_gap_through_targets"]
    assert t.t1_hit is True
    assert t.t2_reached is True


def test_metrics_are_computed_not_declared(result):
    m = result["metrics"]
    # Wins + losses can be fewer than the total: a trade closing exactly flat is
    # neither, and silently folding it into either would misstate the win rate.
    assert m["wins"] + m["losses"] <= m["total_trades"]
    assert m["net_profit"] == round(m["gross_profit"] + m["gross_loss"], 2)
    assert m["expectancy"] == round(m["net_profit"] / m["total_trades"], 2)


def test_profit_factor_is_none_rather_than_infinite(cfg):
    winners = [B.simulate(100.0, 100, B._path([100, 103, 108, 115, 112]),
                          B.STRONG, cfg, cost_pct=0.0)]
    m = B.metrics(winners)
    assert m["losses"] == 0
    assert m["profit_factor"] is None


def test_costs_reduce_every_result(cfg):
    free = B.run(cfg, cost_pct=0.0)["metrics"]["net_profit"]
    costed = B.run(cfg, cost_pct=0.25)["metrics"]["net_profit"]
    assert costed < free


def test_same_bar_ambiguity_resolves_against_us(cfg):
    """
    A bar that touches both the trailing stop and a new high must be scored as
    the stop firing. Assuming the favourable order is how a losing strategy is
    made to look profitable.
    """
    bars = [B.Bar(high=103.5, low=103.0, close=103.2),   # books T1, starts trail
            B.Bar(high=130.0, low=90.0, close=128.0)]    # both, in one bar
    t = B.simulate(100.0, 100, bars, B.STRONG, cfg, cost_pct=0.0)
    assert t.exit_reason == A.TRAILING_STOP
    assert t.exit_price < 130.0


def test_simulation_is_deterministic(cfg):
    a = B.run(cfg, cost_pct=0.25)["metrics"]
    b = B.run(cfg, cost_pct=0.25)["metrics"]
    assert a == b


def test_unfinished_trade_is_flagged_not_counted_as_a_strategy_exit(cfg):
    """Running out of bars is not the strategy deciding anything."""
    t = B.simulate(100.0, 100, B._path([100, 103, 104]), B.STRONG, cfg, cost_pct=0.0)
    assert t.exit_reason == A.END_OF_DAY_EXIT
