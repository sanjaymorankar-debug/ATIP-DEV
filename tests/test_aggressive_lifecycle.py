"""
Position lifecycle against a real (temporary) database: state transitions,
P&L reconciliation, restart survival, and the idempotency guarantees that stop
a repeated market event from selling the same shares twice.
"""

import pytest

from strategy import aggressive as A
from strategy import positions as P

STRONG = A.Momentum(zpi=80, msi=75, cri=15, mri=20, rsi_14=65,
                    macd_hist=0.5, volume_ratio=1.8)
WEAK = A.Momentum(zpi=20, msi=25, cri=80, mri=75, rsi_14=35,
                  macd_hist=-0.5, volume_ratio=0.5)


# ── opening ───────────────────────────────────────────────────────────────

def test_open_position_records_the_entry(position):
    assert position["symbol"] == "ACME"
    assert position["entry_price"] == 100.0
    assert position["initial_qty"] == 100
    assert position["remaining_qty"] == 100
    assert position["status"] == A.ST_OPEN
    assert position["t1_state"] == "PENDING"
    assert position["t2_state"] == "PENDING"
    assert position["realized_pnl"] == 0


def test_new_position_defaults_to_paper(position):
    assert position["mode"] == "PAPER"


# ── target 1 ──────────────────────────────────────────────────────────────

def test_target_1_books_half_and_creates_a_runner(position, cfg):
    r = P.record_partial_exit(position["id"], 103.0, cfg=cfg)
    assert r["applied"] is True
    assert r["quantity"] == 50
    assert r["remaining_qty"] == 50
    p = r["position"]
    assert p["status"] == A.ST_RUNNER
    assert p["t1_state"] == "DONE"
    assert p["realized_pnl"] == 150.0


def test_target_1_activates_the_trailing_stop(position, cfg):
    assert position["trail_stop"] is None
    r = P.record_partial_exit(position["id"], 103.0, cfg=cfg)
    assert r["position"]["trail_stop"] is not None
    assert r["position"]["trail_stop"] < 103.0


def test_target_1_cannot_sell_twice(position, cfg):
    """The requirement that matters most: a repeated +3% event sells nothing."""
    first = P.record_partial_exit(position["id"], 103.0, cfg=cfg)
    second = P.record_partial_exit(position["id"], 103.0, cfg=cfg)
    third = P.record_partial_exit(position["id"], 104.0, cfg=cfg)
    assert first["applied"] is True
    assert second["applied"] is False
    assert third["applied"] is False
    p = P.get_position(position["id"])
    assert p["remaining_qty"] == 50
    assert p["realized_pnl"] == 150.0


def test_ten_duplicate_events_still_sell_once(position, cfg):
    for _ in range(10):
        P.record_partial_exit(position["id"], 103.0, cfg=cfg)
    p = P.get_position(position["id"])
    assert p["remaining_qty"] == 50
    assert p["t1_qty"] == 50


def test_single_share_position_is_not_split(temp_db, cfg):
    """One share cannot be halved; it becomes the runner rather than an exit."""
    pos = P.open_position("TINY", 100.0, 1, atr_pct=2.0, cfg=cfg)
    r = P.record_partial_exit(pos["id"], 103.0, cfg=cfg)
    assert r["applied"] is True
    assert r["quantity"] == 0
    p = r["position"]
    assert p["remaining_qty"] == 1
    assert p["status"] == A.ST_RUNNER
    assert p["trail_stop"] is not None


def test_odd_quantity_rounds_down_and_keeps_a_runner(temp_db, cfg):
    pos = P.open_position("ODD", 100.0, 7, atr_pct=2.0, cfg=cfg)
    r = P.record_partial_exit(pos["id"], 103.0, cfg=cfg)
    assert r["quantity"] == 3
    assert r["remaining_qty"] == 4


# ── the runner and the trail ──────────────────────────────────────────────

def test_high_water_ratchets_the_stop_up(position, cfg):
    P.record_partial_exit(position["id"], 103.0, cfg=cfg)
    a = P.update_high_water(position["id"], 110.0)
    b = P.update_high_water(position["id"], 120.0)
    assert b["trail_stop"] > a["trail_stop"]


def test_stop_never_moves_down_on_a_pullback(position, cfg):
    P.record_partial_exit(position["id"], 103.0, cfg=cfg)
    P.update_high_water(position["id"], 130.0)
    peak = P.get_position(position["id"])["trail_stop"]
    for price in (125, 118, 101, 90):
        P.update_high_water(position["id"], price)
        assert P.get_position(position["id"])["trail_stop"] == peak


def test_trail_state_survives_a_restart(position, cfg):
    """State lives in the database, not in memory, so a crash cannot lose it."""
    P.record_partial_exit(position["id"], 103.0, cfg=cfg)
    P.update_high_water(position["id"], 125.0)
    before = P.get_position(position["id"])
    reloaded = P.get_position(position["id"])      # a fresh connection each call
    assert reloaded["trail_stop"] == before["trail_stop"]
    assert reloaded["high_water"] == 125.0
    assert reloaded["status"] == A.ST_RUNNER


# ── the +6% checkpoint ────────────────────────────────────────────────────

def test_checkpoint_does_not_close_the_runner(position, cfg):
    P.record_partial_exit(position["id"], 103.0, cfg=cfg)
    r = P.record_checkpoint(position["id"], 106.0, STRONG, atr_pct=1.5, cfg=cfg)
    assert r["applied"] is True
    assert r["decision"]["verdict"] == A.HOLD_RUNNER
    p = r["position"]
    assert p["status"] == A.ST_RUNNER
    assert p["remaining_qty"] == 50


def test_weak_checkpoint_recommends_exit_but_does_not_itself_sell(position, cfg):
    P.record_partial_exit(position["id"], 103.0, cfg=cfg)
    r = P.record_checkpoint(position["id"], 106.0, WEAK, atr_pct=1.5, cfg=cfg)
    assert r["decision"]["verdict"] == A.EXIT_RUNNER
    assert r["position"]["remaining_qty"] == 50     # the caller places the order


def test_checkpoint_runs_once(position, cfg):
    P.record_partial_exit(position["id"], 103.0, cfg=cfg)
    first = P.record_checkpoint(position["id"], 106.0, STRONG, cfg=cfg)
    second = P.record_checkpoint(position["id"], 106.5, WEAK, cfg=cfg)
    assert first["applied"] is True
    assert second["applied"] is False
    assert P.get_position(position["id"])["t2_verdict"] == A.HOLD_RUNNER


def test_checkpoint_never_loosens_the_stop(position, cfg):
    P.record_partial_exit(position["id"], 103.0, cfg=cfg)
    P.update_high_water(position["id"], 112.0)
    before = P.get_position(position["id"])["trail_stop"]
    r = P.record_checkpoint(position["id"], 106.0, WEAK, atr_pct=6.0, cfg=cfg)
    assert r["trail_stop"] >= before


# ── closing ───────────────────────────────────────────────────────────────

def test_initial_stop_before_target_1_exits_everything(position, cfg):
    r = P.close_position(position["id"], 97.0, A.INITIAL_STOP_LOSS)
    assert r["applied"] is True
    assert r["quantity"] == 100
    p = r["position"]
    assert p["status"] == A.ST_CLOSED
    assert p["remaining_qty"] == 0
    assert p["exit_reason"] == A.INITIAL_STOP_LOSS
    assert p["realized_pnl"] == -300.0


def test_close_is_idempotent(position, cfg):
    first = P.close_position(position["id"], 97.0, A.INITIAL_STOP_LOSS)
    second = P.close_position(position["id"], 97.0, A.INITIAL_STOP_LOSS)
    assert first["applied"] is True
    assert second["applied"] is False
    assert P.get_position(position["id"])["realized_pnl"] == -300.0


def test_a_second_close_with_a_different_reason_still_cannot_double_book(position, cfg):
    P.close_position(position["id"], 97.0, A.INITIAL_STOP_LOSS)
    r = P.close_position(position["id"], 96.0, A.TRAILING_STOP)
    assert r["applied"] is False
    assert P.get_position(position["id"])["realized_pnl"] == -300.0


def test_full_winning_sequence_reconciles(position, cfg):
    """+3% partial, runner rides to 130, trail takes it out at 124."""
    P.record_partial_exit(position["id"], 103.0, cfg=cfg)
    P.update_high_water(position["id"], 130.0)
    P.record_checkpoint(position["id"], 106.0, STRONG, atr_pct=1.5, cfg=cfg)
    r = P.close_position(position["id"], 124.0, A.TRAILING_STOP)
    p = r["position"]
    assert p["status"] == A.ST_CLOSED
    assert p["remaining_qty"] == 0
    # 50 @ +3 = 150, then 50 @ +24 = 1200
    assert p["realized_pnl"] == 1350.0
    assert p["exit_reason"] == A.TRAILING_STOP


def test_total_pnl_combines_realised_and_unrealised(position, cfg):
    P.record_partial_exit(position["id"], 103.0, cfg=cfg)
    P.mark_to_market(position["id"], 110.0)
    p = P.get_position(position["id"])
    assert p["realized_pnl"] == 150.0
    assert p["unrealized_pnl"] == 500.0          # 50 shares x +10
    assert P.total_pnl(p) == 650.0


def test_closing_zeroes_unrealised(position, cfg):
    P.record_partial_exit(position["id"], 103.0, cfg=cfg)
    P.mark_to_market(position["id"], 110.0)
    P.close_position(position["id"], 110.0, A.TRAILING_STOP)
    p = P.get_position(position["id"])
    assert p["unrealized_pnl"] == 0
    assert P.total_pnl(p) == p["realized_pnl"]


# ── multiple positions stay independent ───────────────────────────────────

def test_two_positions_do_not_share_state(temp_db, cfg):
    a = P.open_position("AAA", 100.0, 100, atr_pct=1.5, cfg=cfg)
    b = P.open_position("BBB", 200.0, 50, atr_pct=1.5, cfg=cfg)
    P.record_partial_exit(a["id"], 103.0, cfg=cfg)
    assert P.get_position(a["id"])["remaining_qty"] == 50
    assert P.get_position(b["id"])["remaining_qty"] == 50
    assert P.get_position(b["id"])["t1_state"] == "PENDING"
    assert P.get_position(b["id"])["realized_pnl"] == 0


def test_same_symbol_twice_is_tracked_separately(temp_db, cfg):
    """Two entries in one stock must not be confused for one position."""
    a = P.open_position("ACME", 100.0, 100, atr_pct=1.5, cfg=cfg)
    b = P.open_position("ACME", 105.0, 40, atr_pct=1.5, cfg=cfg)
    P.close_position(a["id"], 97.0, A.INITIAL_STOP_LOSS)
    assert P.get_position(b["id"])["status"] == A.ST_RUNNER or \
           P.get_position(b["id"])["status"] == A.ST_OPEN
    assert P.get_position(b["id"])["remaining_qty"] == 40
    assert len(P.open_positions("ACME")) == 1


def test_open_positions_excludes_closed(temp_db, cfg):
    a = P.open_position("AAA", 100.0, 10, cfg=cfg)
    P.open_position("BBB", 100.0, 10, cfg=cfg)
    assert len(P.open_positions()) == 2
    P.close_position(a["id"], 99.0, A.MANUAL_EXIT)
    assert len(P.open_positions()) == 1


# ── edge cases ────────────────────────────────────────────────────────────

def test_unknown_position_is_handled(temp_db, cfg):
    for fn in (lambda: P.record_partial_exit("nope", 100.0, cfg=cfg),
               lambda: P.close_position("nope", 100.0, A.MANUAL_EXIT),
               lambda: P.record_checkpoint("nope", 100.0, STRONG, cfg=cfg)):
        assert fn()["applied"] is False


def test_gap_straight_through_both_targets(position, cfg):
    """
    Price gaps from below +3% to above +6% overnight. The partial must fill at
    the real price, not the theoretical +3% level, and the checkpoint still runs.
    """
    r = P.record_partial_exit(position["id"], 108.0, cfg=cfg)
    assert r["quantity"] == 50
    assert r["realized_pnl"] == 400.0            # filled at 108, not 103
    c = P.record_checkpoint(position["id"], 108.0, STRONG, cfg=cfg)
    assert c["applied"] is True
    assert c["position"]["remaining_qty"] == 50


def test_checkpoint_on_a_closed_position_is_refused(position, cfg):
    P.close_position(position["id"], 97.0, A.INITIAL_STOP_LOSS)
    r = P.record_checkpoint(position["id"], 106.0, STRONG, cfg=cfg)
    assert r["applied"] is False


def test_partial_exit_cannot_exceed_the_position(position, cfg):
    r = P.record_partial_exit(position["id"], 103.0, quantity=99999, cfg=cfg)
    assert r["quantity"] == 100
    assert r["remaining_qty"] == 0
