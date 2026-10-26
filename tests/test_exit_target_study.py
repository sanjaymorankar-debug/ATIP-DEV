"""
research/exit_target_study.py: the exit mechanics, and agreement with ATIP's own simulator.

Like test_aggressive_backtest, these are behaviour tests on hand-made paths -- nothing here says
whether any exit is profitable.
"""

import math

import numpy as np
import pytest

from research import exit_target_study as S
from strategy.aggressive import Momentum
from strategy.backtest_aggressive import Bar, simulate as atip_simulate


def _paths(bars, entry=100.0, atr=2.0, mom=50.0):
    """bars: [(o, h, l, c), ...] from the entry session on; the entry fills at bars[0]'s open."""
    a = np.array(bars, dtype=float)[None, :, :]
    n = a.shape[1]
    return S.Paths(np.zeros(1, int), np.zeros(1, int), np.zeros(1), np.array([entry]),
                   a[:, :, 0], a[:, :, 1], a[:, :, 2], a[:, :, 3], np.array([atr]),
                   np.full((1, n), mom))


def _one(bars, pol, order="pessimistic", cost=0.0, **kw):
    r = S.simulate(_paths(bars, **kw), pol, cost, order)
    return float(r.net[0]), int(r.reason[0]), int(r.exit_bar[0])


FLAT3 = S.Policy("flat_3", "flat", 3.0, 6.0, 3.0)


def test_flat_target_books_everything_at_the_level():
    net, why, k = _one([(100, 101, 99.5, 100.5), (100.5, 103.5, 100.2, 103)], FLAT3)
    assert (net, why, k) == (pytest.approx(3.0), 2, 1)


def test_bar_touching_both_levels_counts_as_the_stop_when_pessimistic():
    net, why, _ = _one([(100, 103.5, 96.5, 101)], FLAT3)
    assert (net, why) == (pytest.approx(-3.0), 1)


def test_ohlc_order_lets_a_red_bar_reach_the_high_first():
    net, why, _ = _one([(100, 103.5, 96.5, 97)], FLAT3, order="ohlc")
    assert (net, why) == (pytest.approx(3.0), 2)


def test_a_gap_through_the_stop_fills_at_the_worse_open():
    net, why, _ = _one([(100, 100.5, 99, 99.5), (95, 96, 94, 95.5)], FLAT3)
    assert (net, why) == (pytest.approx(-5.0), 1)


def test_costs_are_charged_on_the_way_in_and_out():
    net, _, _ = _one([(100, 103.5, 99.5, 103)], FLAT3, cost=0.25)
    assert net == pytest.approx(3.0 - 0.125 - 0.125 * 1.03)


def test_unfinished_trade_is_sold_at_the_last_close():
    net, why, k = _one([(100, 101, 99, 100.5), (100.5, 102, 100, 101.5)], FLAT3)
    assert (net, why, k) == (pytest.approx(1.5), 5, 1)


def test_atr_stop_is_scaled_and_clamped():
    pol = S.Policy("hold", "hold", stop=3.0, stop_atr=3.0)
    # ATR 1 % -> 3 x 1 = 3 %; ATR 5 % -> 15 %, clamped to 10 %
    assert _one([(100, 100, 96.9, 97)], pol, atr=1.0)[0] == pytest.approx(-3.0)
    assert _one([(100, 100, 89.0, 90)], pol, atr=5.0)[0] == pytest.approx(-10.0)


def test_breakeven_runner_never_closes_below_entry():
    pol = S.Policy("atip_be_3", "atip_be", 3.0, 6.0, 3.0)
    bars = [(100, 100.5, 99.8, 100.2), (100.2, 103.2, 100.1, 103), (103, 103.1, 97, 97.5)]
    net, why, _ = _one(bars, pol)
    assert why == 3
    assert net == pytest.approx(0.5 * 3.0 + 0.5 * 0.0)


def test_split_matches_strategy_backtest_aggressive(cfg):
    """
    On paths without gaps (each open = the previous close), the study's atip_3 must reproduce
    strategy.backtest_aggressive.simulate trade for trade. ATIP rounds levels to 2 decimals and
    the study does not, so a trade sitting exactly on a level may flip; allow 1 %.
    The study's cost is a round trip; ATIP charges cost_pct per leg on turnover, hence 2 x 0.25.
    """
    rng = np.random.default_rng(1)
    n, length = 400, 30
    ours, theirs = [], []
    for _ in range(n):
        c = 100 * np.cumprod(1 + rng.normal(0.002, 0.025, length))
        o = np.concatenate([[100.0], c[:-1]])
        h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.012, length)))
        lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.012, length)))
        atr = float(rng.uniform(0.5, 5))
        mom = float(round(rng.uniform(0, 100), 2))
        t = atip_simulate(100.0, 1000, [Bar(high=a, low=b, close=d) for a, b, d in zip(h, lo, c)],
                          Momentum(zpi=mom, msi=mom), cfg, stop_pct=3.0, cost_pct=0.25, atr_pct=atr)
        theirs.append(t.realized / 1000)
        flat = [(100.0, 100.0, 100.0, 100.0)]          # the study's entry session
        bars = flat + list(zip(o, h, lo, c))
        r = S.simulate(_paths(bars, atr=atr, mom=mom), S.Policy("atip_3", "atip", 3.0, 6.0, 3.0),
                       0.5, "pessimistic")
        ours.append(float(r.net[0]))
    diff = np.abs(np.array(ours) - np.array(theirs))
    assert (diff > 0.05).mean() <= 0.01
    assert math.isclose(np.mean(ours), np.mean(theirs), abs_tol=0.02)
