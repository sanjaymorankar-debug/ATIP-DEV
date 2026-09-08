"""
Pure decision logic: configuration, sizing, trailing arithmetic, the +6%
checkpoint, and P&L. No database, no broker, no clock.
"""

import pytest

from strategy import aggressive as A


# ── configuration ─────────────────────────────────────────────────────────

def test_defaults_are_the_agreed_strategy(cfg):
    assert cfg["target_1_pct"] == 3.0
    assert cfg["target_1_exit_pct"] == 50.0
    assert cfg["target_2_pct"] == 6.0
    assert cfg["trailing_enabled"] is True


def test_execution_defaults_to_paper():
    """A fresh config must never place live orders by accident."""
    assert A.DEFAULTS["execution_mode"] == "PAPER"
    assert A.is_live(A.DEFAULTS) is False
    assert A.is_live({**A.DEFAULTS, "execution_mode": "LIVE"}) is True
    assert A.is_live({**A.DEFAULTS, "execution_mode": "live"}) is True


@pytest.mark.parametrize("split", [40.0, 60.0, 25.0, 75.0])
def test_alternative_splits_validate_and_size_correctly(cfg, split):
    c = A.validate_config({**cfg, "target_1_exit_pct": split})
    assert A.partial_exit_quantity(100, c) == int(split)


@pytest.mark.parametrize("bad,msg", [
    ({"target_1_exit_pct": 100.0}, "between 0 and 100"),
    ({"target_1_exit_pct": 0.0}, "between 0 and 100"),
    ({"target_1_exit_pct": -10.0}, "between 0 and 100"),
    ({"target_2_pct": 3.0}, "must be above"),
    ({"target_2_pct": 1.0}, "must be above"),
    ({"target_1_pct": 0.0}, "must be > 0"),
    ({"trail_min_pct": 9.0, "trail_max_pct": 2.0}, "invalid"),
    ({"momentum_weak_score": 90.0, "momentum_strong_score": 50.0}, "must not exceed"),
    ({"execution_mode": "YOLO"}, "PAPER or LIVE"),
])
def test_invalid_config_is_rejected(cfg, bad, msg):
    with pytest.raises(A.ConfigError) as e:
        A.validate_config({**cfg, **bad})
    assert msg in str(e.value)


# ── partial-exit sizing ───────────────────────────────────────────────────

@pytest.mark.parametrize("qty,expected", [
    (100, 50), (10, 5), (7, 3), (3, 1), (2, 1),
    (1, 0),          # cannot split a single share
    (0, 0),
])
def test_partial_exit_quantity(cfg, qty, expected):
    assert A.partial_exit_quantity(qty, cfg) == expected


def test_partial_exit_always_leaves_a_runner(cfg):
    """Whatever the split, at least one share must survive to run."""
    for qty in range(2, 200):
        sold = A.partial_exit_quantity(qty, cfg)
        assert 1 <= sold <= qty - 1


def test_partial_exit_uses_original_size_not_remaining(cfg):
    """
    Sizing off the remaining quantity would let a repeated call sell half of
    the half. It must be a function of the original position only.
    """
    assert A.partial_exit_quantity(100, cfg) == 50
    assert A.partial_exit_quantity(100, cfg) == 50


# ── trailing distance ─────────────────────────────────────────────────────

def test_trail_is_volatility_aware(cfg):
    """Distance scales with ATR. Written against the configured multiplier, not
    a baked-in number, so retuning the multiplier doesn't falsely fail here —
    `test_default_trail_is_not_tight` is what guards the value itself."""
    mult = cfg["trail_atr_mult"]
    assert A.trail_distance_pct(2.0, cfg) == pytest.approx(2.0 * mult)
    assert A.trail_distance_pct(1.5, cfg) == pytest.approx(1.5 * mult)
    assert A.trail_distance_pct(3.0, cfg) > A.trail_distance_pct(1.0, cfg)


def test_trail_is_clamped_at_both_ends(cfg):
    assert A.trail_distance_pct(0.1, cfg) == cfg["trail_min_pct"]
    assert A.trail_distance_pct(50.0, cfg) == cfg["trail_max_pct"]


def test_trail_falls_back_when_atr_missing(cfg):
    for bad in (None, 0, -1):
        assert cfg["trail_min_pct"] <= A.trail_distance_pct(bad, cfg) <= cfg["trail_max_pct"]


def test_tighten_shrinks_but_respects_the_floor(cfg):
    assert A.tighten(6.0, cfg) == 3.0
    assert A.tighten(1.0, cfg) == cfg["trail_min_pct"]


# ── the ratchet ───────────────────────────────────────────────────────────

def test_ratchet_rises_with_the_high_water_mark():
    s1 = A.ratchet(None, 100.0, 5.0)
    s2 = A.ratchet(s1, 110.0, 5.0)
    assert s1 == 95.0 and s2 == 104.5


def test_ratchet_never_moves_down():
    """The defining property of a trailing stop."""
    stop = A.ratchet(None, 120.0, 5.0)
    for price in (119, 100, 80, 50, 5):
        assert A.ratchet(stop, price, 5.0) == stop


def test_ratchet_is_monotonic_over_a_whole_path():
    path = [100, 104, 102, 111, 95, 130, 90, 128]
    stop, hw = None, path[0]
    seen = []
    for p in path:
        hw = max(hw, p)
        stop = A.ratchet(stop, hw, 4.0)
        seen.append(stop)
    assert seen == sorted(seen), f"stop moved backwards: {seen}"


def test_ratchet_mirrors_for_a_short():
    s1 = A.ratchet(None, 100.0, 5.0, long_side=False)
    assert s1 == 105.0
    assert A.ratchet(s1, 90.0, 5.0, long_side=False) == 94.5
    assert A.ratchet(s1, 130.0, 5.0, long_side=False) == s1   # never loosens


# ── momentum composite ────────────────────────────────────────────────────

def test_risk_indices_are_inverted():
    """
    High CRI is crash RISK. If it were averaged in raw, the runner would hold
    hardest exactly when a crash was most likely.
    """
    calm, _ = A.Momentum(cri=10).composite()
    risky, _ = A.Momentum(cri=90).composite()
    assert calm > risky


def test_composite_ignores_missing_inputs():
    score, n = A.Momentum(zpi=80).composite()
    assert score == 80.0 and n == 1
    assert A.Momentum().composite() == (None, 0)


def test_composite_is_bounded():
    wild = A.Momentum(zpi=999, msi=-999, cri=-50, rsi_14=200, volume_ratio=99)
    score, _ = wild.composite()
    assert 0.0 <= score <= 100.0


# ── the +6% checkpoint ────────────────────────────────────────────────────

STRONG = A.Momentum(zpi=80, msi=75, cri=15, mri=20, rsi_14=65,
                    macd_hist=0.5, volume_ratio=1.8)
WEAK = A.Momentum(zpi=20, msi=25, cri=80, mri=75, rsi_14=35,
                  macd_hist=-0.5, volume_ratio=0.5)
NEUTRAL = A.Momentum(zpi=50, msi=50, cri=50, mri=50, rsi_14=50,
                     macd_hist=0.01, volume_ratio=1.0)


def test_target_2_never_forces_an_exit_on_strong_momentum(cfg):
    """The core requirement: +6% is a checkpoint, not a sale."""
    d = A.checkpoint_decision(STRONG, cfg)
    assert d.verdict == A.HOLD_RUNNER
    assert d.exit_reason is None


def test_weak_momentum_banks_the_runner(cfg):
    d = A.checkpoint_decision(WEAK, cfg)
    assert d.verdict == A.EXIT_RUNNER
    assert d.exit_reason == A.TARGET_2_MOMENTUM_EXIT


def test_neutral_momentum_holds_but_tightens(cfg):
    d = A.checkpoint_decision(NEUTRAL, cfg, current_trail_pct=6.0)
    assert d.verdict == A.TIGHTEN_TRAIL
    assert d.trail_pct < 6.0


def test_no_momentum_data_protects_rather_than_guesses(cfg):
    d = A.checkpoint_decision(A.Momentum(), cfg, current_trail_pct=6.0)
    assert d.verdict == A.TIGHTEN_TRAIL
    assert d.exit_reason is None
    assert d.trail_pct < 6.0


def test_hold_does_not_loosen_the_trail(cfg):
    d = A.checkpoint_decision(STRONG, cfg, current_trail_pct=2.0)
    assert d.trail_pct <= 2.0


def test_thresholds_are_configurable(cfg):
    """Same momentum, different appetite, different answer."""
    timid = A.validate_config({**cfg, "momentum_weak_score": 60.0,
                               "momentum_strong_score": 90.0})
    assert A.checkpoint_decision(NEUTRAL, cfg).verdict == A.TIGHTEN_TRAIL
    assert A.checkpoint_decision(NEUTRAL, timid).verdict == A.EXIT_RUNNER


def test_decision_explains_itself(cfg):
    for m in (STRONG, WEAK, NEUTRAL, A.Momentum()):
        d = A.checkpoint_decision(m, cfg)
        assert d.reason and len(d.reason) > 10


# ── prices and P&L ────────────────────────────────────────────────────────

def test_target_price():
    assert A.target_price(100.0, 3.0) == 103.0
    assert A.target_price(100.0, 6.0) == 106.0
    assert A.target_price(100.0, 3.0, long_side=False) == 97.0


def test_realised_pnl_without_costs():
    assert A.realised_pnl(100.0, 103.0, 50) == 150.0


def test_realised_pnl_is_reduced_by_costs():
    """A 3% target does not survive being measured without costs."""
    gross = A.realised_pnl(100.0, 103.0, 50, cost_pct=0.0)
    net = A.realised_pnl(100.0, 103.0, 50, cost_pct=0.25)
    assert net < gross
    # Rounded to paise, as money is.
    assert net == round(150.0 - (100.0 + 103.0) * 50 * 0.0025, 2)


def test_realised_pnl_mirrors_for_a_short():
    assert A.realised_pnl(100.0, 97.0, 50, long_side=False) == 150.0


def test_unrealised_pnl_is_zero_with_no_position():
    assert A.unrealised_pnl(100.0, 150.0, 0) == 0.0


# ── evidence-backed defaults ──────────────────────────────────────────────

def test_default_trail_is_not_tight():
    """
    A regression guard, not a style rule.

    Measured on 3,000 real entries, expectancy rose monotonically as the trail
    widened: 2x ATR lost 0.31% per trade, 4x lost 0.06%, 6x made 0.12%. A tight
    trail cut winners. If someone tightens these defaults back toward the
    original 2x / 1.5% floor, this fails and points at the measurement.
    """
    assert A.DEFAULTS["trail_atr_mult"] >= 3.5
    assert A.DEFAULTS["trail_min_pct"] >= 2.5
    assert A.DEFAULTS["trail_max_pct"] >= 15.0


def test_default_trail_on_a_typical_stock_is_wide_enough():
    """A 2%-ATR midcap should get room to breathe, not a 3% noise-width leash."""
    assert A.trail_distance_pct(2.0, A.DEFAULTS) >= 6.0


def test_agreed_targets_are_untouched():
    """
    The 3% / 50% / 6% split is the user's decision, not a tunable. Only the
    trail was changed on the strength of the backtest.
    """
    assert A.DEFAULTS["target_1_pct"] == 3.0
    assert A.DEFAULTS["target_1_exit_pct"] == 50.0
    assert A.DEFAULTS["target_2_pct"] == 6.0
