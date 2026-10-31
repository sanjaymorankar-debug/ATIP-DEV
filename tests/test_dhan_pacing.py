"""
Dhan's per-second limits (data/dhan.py PACE_SECONDS) and the dashboard's shared session quotes.

2026-10-30: the dashboard's tabs asked Dhan for ~500 quotes each, two or three in the same second, and
Dhan (1 quote call a second) refused all but one -- 639 times that day. The 12:59 broker-health check
landed in one of those seconds and read DOWN; at 16:48 two index histories were refused (DH-904) when
the post-market jobs made more than five Data API calls in one second.
"""

import threading
import time

import pandas as pd
import pytest

import data.dhan as D

QUOTE_GAP, DATA_GAP = 0.2, 0.05        # short gaps keep the suite fast; the rule is the same
SLACK = 0.02                           # time.sleep can wake a hair early


@pytest.fixture
def paced(monkeypatch):
    monkeypatch.setattr(D, "PACE_SECONDS", {"quote": QUOTE_GAP, "data": DATA_GAP})
    monkeypatch.setattr(D, "_next_call", {})
    from ops import resilience
    monkeypatch.setattr(resilience, "_BREAKERS", {})


def _recorder(name, calls):
    def fn(*a, **k):
        calls.append((name, time.monotonic()))
        return {"status": "success", "data": {}}
    fn.__name__ = name
    return fn


def _gaps(times):
    return [b - a for a, b in zip(times, times[1:])]


def test_quote_calls_from_many_threads_are_a_gap_apart(paced):
    calls = []
    quote, ohlc = _recorder("quote_data", calls), _recorder("ohlc_data", calls)
    threads = [threading.Thread(target=D._guarded, args=(fn, {})) for fn in (quote, ohlc, quote, ohlc)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    times = sorted(t for _, t in calls)
    assert len(times) == 4
    assert min(_gaps(times)) >= QUOTE_GAP - SLACK, "quote and ohlc share Dhan's one-a-second Quote API limit"


def test_history_calls_are_a_data_gap_apart(paced):
    calls = []
    daily, intraday = _recorder("historical_daily_data", calls), _recorder("intraday_minute_data", calls)
    for fn in (daily, intraday, daily, intraday, daily, daily):
        D._guarded(fn, security_id="13")
    times = [t for _, t in calls]
    assert min(_gaps(times)) >= DATA_GAP - SLACK
    assert times[-1] - times[0] < 5 * QUOTE_GAP, "history calls are not held to the quote gap"


def test_a_history_call_does_not_wait_behind_a_quote_call(paced):
    calls = []
    D._guarded(_recorder("quote_data", calls), {})
    t0 = time.monotonic()
    D._guarded(_recorder("historical_daily_data", calls), security_id="13")
    assert time.monotonic() - t0 < QUOTE_GAP / 2


def test_an_open_circuit_fails_fast_and_books_no_turn(paced):
    from ops.errors import DependencyUnavailable
    from ops.resilience import breaker
    b = breaker("dhan_data", failure_threshold=6, reset_seconds=120)
    b.state, b.opened_at = "OPEN", time.monotonic()
    calls = []
    t0 = time.monotonic()
    with pytest.raises(DependencyUnavailable):
        D._guarded(_recorder("quote_data", calls), {})
    assert time.monotonic() - t0 < QUOTE_GAP / 2 and not calls
    assert "quote" not in D._next_call


# ── the dashboard: every tab shares one quote fetch per window ─────────────

@pytest.fixture
def session(temp_db, monkeypatch):
    from db.schema import get_connection, init_db
    import dashboard.server as srv
    init_db()
    c = get_connection()
    c.execute("INSERT INTO ai_scores (symbol, date, atip_score) VALUES ('ACME', '2026-10-30', 60)")
    c.commit()
    c.close()
    monkeypatch.setattr(srv, "latest_scored_date", lambda: "2026-10-30")
    fetches = []

    def fetch(syms, *a):
        fetches.append(sorted(syms))
        time.sleep(0.05)                      # long enough for the other requests to arrive meanwhile
        return pd.DataFrame([{"symbol": "ACME", "ltp": 110.0, "prev_close": 100.0}])

    monkeypatch.setattr(D, "fetch_live_quotes", fetch)
    return srv, fetches


def test_requests_in_the_same_moment_share_one_dhan_fetch(session):
    srv, fetches = session
    results = []
    threads = [threading.Thread(target=lambda: results.append(srv._session_live_quotes())) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert fetches == [["ACME"]]
    assert results == [{"ACME": {"ltp": 110.0, "chg_pct": 10.0, "prev_close": 100.0}}] * 4


def test_the_next_window_fetches_again(session, monkeypatch):
    srv, fetches = session
    srv._session_live_quotes()
    srv._session_live_quotes()
    assert len(fetches) == 1
    monkeypatch.setitem(srv._live_quotes_cache, "at", time.monotonic() - srv.LIVE_QUOTES_TTL - 1)
    srv._session_live_quotes()
    assert len(fetches) == 2


def test_a_failed_fetch_is_not_retried_within_the_window(session, monkeypatch):
    srv, fetches = session

    def refused(syms, *a):
        fetches.append(sorted(syms))
        return pd.DataFrame()

    monkeypatch.setattr(D, "fetch_live_quotes", refused)
    assert srv._session_live_quotes() == {}
    assert srv._session_live_quotes() == {}
    assert len(fetches) == 1


def test_the_endpoint_serves_the_shared_quotes_in_the_session(session, monkeypatch):
    import asyncio, json
    import pipeline.scheduler as S
    srv, fetches = session
    monkeypatch.setattr(S, "is_market_hours", lambda: True)
    first = json.loads(asyncio.run(srv.api_live_quotes()).body)
    second = json.loads(asyncio.run(srv.api_live_quotes()).body)
    assert first == second == {"ACME": {"ltp": 110.0, "chg_pct": 10.0, "prev_close": 100.0}}
    assert len(fetches) == 1
