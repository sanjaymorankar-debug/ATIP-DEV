"""RCA-13 (6 Nov 2026): the order monitor's first pass after the 09:15 open lost a minute to 'database is locked'.

atip.log / atip.jsonl, 6 Nov 2026 (IST): the monitor's first market-hours pass began about 09:15:07. At 09:15:07.80
auto-pick's cancel_stale cancelled S9's LODHA pick left from 5 Nov, then waited the whole 60 s busy timeout to cancel
S1's AUROPHARMA and logged 'auto-pick error: database is locked' at 09:16:09.689. check_triggers ran only after that,
and the pass after it, at 09:16:40, sold S10's BHEL at 408.20 on a 410.78 stop. The lock was auto-pick's own
uncommitted event (tests/test_autopick.py: test_two_picks_left_from_yesterday_are_both_cancelled_without_a_lock).
These tests hold the monitor to the rest: a pass that loses to a lock -- whoever holds it -- still runs
check_triggers, and the monitor runs it again in seconds instead of after the usual 30."""

import sqlite3
import threading

import pytest

from tests.test_autopick import _sessions, _signals
from tests.test_w39_qa_suite import paper  # noqa: F401  (pytest fixture)


@pytest.fixture
def srv():
    import dashboard.server as srv
    return srv


def _locked(*a, **k):
    raise sqlite3.OperationalError("database is locked")


class _Stop(BaseException):
    """Ends the monitor's endless loop from inside time.sleep."""


def _run_loop(srv, monkeypatch, sleeps: int) -> list:
    """Run _order_monitor_loop in this thread until it has slept `sleeps` times; the waits it asked for."""
    import time
    me, real, slept = threading.get_ident(), time.sleep, []

    def sleep(s):
        if threading.get_ident() != me:                  # any other thread sleeps for real
            return real(s)
        slept.append(s)
        if len(slept) == sleeps:
            raise _Stop

    monkeypatch.setattr(time, "sleep", sleep)
    with pytest.raises(_Stop):
        srv._order_monitor_loop()
    return slept


def _market(monkeypatch, open_=True):
    import pipeline.operating_schedule as OS
    import pipeline.scheduler as SC
    monkeypatch.setattr(SC, "is_market_hours", lambda *a, **k: open_)
    monkeypatch.setattr(OS, "is_stopped", lambda *a, **k: False)


# ── one pass ────────────────────────────────────────────────────────────────

def test_a_pass_whose_auto_pick_is_locked_still_checks_triggers_and_says_so(srv, monkeypatch):
    import orders.autopick as AP
    checked = []
    monkeypatch.setattr(AP, "run", _locked)
    monkeypatch.setattr(srv.oe, "check_triggers", lambda: checked.append(1) or [])
    assert srv._order_monitor_pass() is True and checked == [1]


def test_a_pass_whose_trigger_check_is_locked_says_so(srv, monkeypatch):
    import orders.autopick as AP
    monkeypatch.setattr(AP, "run", lambda: [])
    monkeypatch.setattr(srv.oe, "check_triggers", _locked)
    assert srv._order_monitor_pass() is True


def test_any_other_failure_is_not_a_lock(srv, monkeypatch):
    import orders.autopick as AP

    def bad_row():
        raise ValueError("bad row")

    monkeypatch.setattr(AP, "run", bad_row)
    monkeypatch.setattr(srv.oe, "check_triggers", lambda: [])
    assert srv._order_monitor_pass() is False


def test_a_pass_locked_out_by_another_writer_queues_its_pick_on_the_retry(paper, srv, monkeypatch):
    """A real lock: another connection holds a write while the pass runs. With a 200 ms busy timeout the pass
    reports the lock and queues nothing; once that writer is done, the retry queues the pick."""
    import db.schema as S
    from orders import rules
    monkeypatch.setattr(S, "BUSY_TIMEOUT_MS", 200)
    _signals(ACME=(80, "BUY", 100.0))
    _sessions("S1")
    other = S.get_connection()
    try:
        other.execute("BEGIN IMMEDIATE")
        assert srv._order_monitor_pass() is True
        assert rules.list_rules() == []
    finally:
        other.rollback()
        other.close()
    assert srv._order_monitor_pass() is False
    assert [r["symbol"] for r in rules.list_rules()] == ["ACME"]


# ── the loop ────────────────────────────────────────────────────────────────

def test_the_monitor_runs_again_in_seconds_after_a_locked_pass(srv, monkeypatch):
    _market(monkeypatch)
    passes = iter([True, False, True])
    monkeypatch.setattr(srv, "_order_monitor_pass", lambda: next(passes))
    assert _run_loop(srv, monkeypatch, 3) == [srv.MONITOR_LOCK_RETRY_S, srv.MONITOR_EVERY_S, srv.MONITOR_LOCK_RETRY_S]
    assert srv.MONITOR_LOCK_RETRY_S <= 5 and srv.MONITOR_EVERY_S == 30      # the usual cadence is unchanged


def test_outside_market_hours_the_monitor_runs_no_pass(srv, monkeypatch):
    _market(monkeypatch, open_=False)
    ran = []
    monkeypatch.setattr(srv, "_order_monitor_pass", lambda: ran.append(1) or True)
    assert _run_loop(srv, monkeypatch, 2) == [srv.MONITOR_EVERY_S] * 2 and ran == []
