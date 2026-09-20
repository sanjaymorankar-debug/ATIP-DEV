"""
Regression tests for the Dhan index WebSocket feed, after the 2026-09-19/20
reconnect storm.

The feed was started once at process start and never stopped. Dhan drops the
connection outside market hours and the SDK's loop reconnects every second
forever, so over one weekend ATIP made ~3,300 refused connections an hour
(HTTP 429), wrote 35 MB of error lines, and -- because the flush loop kept
writing the last tick -- stored 7,583 index_levels rows stamped on a Saturday
and a Sunday, all holding Friday's close.
"""

import datetime as dt
import time

import pytest


# ── session window ────────────────────────────────────────────────────────

@pytest.mark.parametrize("when,open_", [
    (dt.datetime(2026, 9, 18, 9, 30), True),    # Friday, mid-session
    (dt.datetime(2026, 9, 18, 9, 0), True),     # pre-open auction
    (dt.datetime(2026, 9, 18, 15, 45), True),   # just past the close
    (dt.datetime(2026, 9, 18, 8, 59), False),   # trading day, too early
    (dt.datetime(2026, 9, 18, 16, 0), False),   # trading day, after the close
    (dt.datetime(2026, 9, 18, 20, 0), False),   # the Friday-night storm
    (dt.datetime(2026, 9, 19, 12, 0), False),   # Saturday
    (dt.datetime(2026, 9, 20, 7, 58), False),   # Sunday
    (dt.datetime(2026, 9, 14, 11, 0), False),   # Ganesh Chaturthi holiday
])
def test_feed_window(when, open_):
    from data.dhan_ws import feed_window_open
    assert feed_window_open(when) is open_


# ── a manager with a stub SDK ─────────────────────────────────────────────

class _FakeFeed:
    IDX = 0
    Ticker = 15

    instances = []

    def __init__(self, *a, **k):
        self.started = False
        self.closed = False
        _FakeFeed.instances.append(self)

    def start(self): self.started = True
    def close_connection(self): self.closed = True


@pytest.fixture
def feed(monkeypatch):
    from data import dhan_ws
    _FakeFeed.instances = []
    monkeypatch.setattr(dhan_ws, "HAS_MARKETFEED", True)
    monkeypatch.setattr(dhan_ws, "MarketFeed", _FakeFeed)
    monkeypatch.setattr(dhan_ws, "get_dhan_client", lambda: (None, None))
    return dhan_ws.IndexFeedManager(flush_interval=0.01)


def _window(monkeypatch, is_open):
    from data import dhan_ws
    monkeypatch.setattr(dhan_ws, "feed_window_open", lambda *a: is_open)


def test_no_connection_outside_the_session(feed, monkeypatch):
    """The storm in one line: at 20:00 on a Friday there is nothing to stream."""
    _window(monkeypatch, False)
    feed.start()
    assert feed._feed is None
    assert _FakeFeed.instances == []
    feed.stop()


def test_connects_inside_the_session(feed, monkeypatch):
    _window(monkeypatch, True)
    feed.start()
    assert feed._feed is not None and feed._feed.started
    feed.stop()


def test_connection_is_dropped_when_the_session_ends(feed, monkeypatch):
    _window(monkeypatch, True)
    feed.start()
    sdk = feed._feed
    _window(monkeypatch, False)
    feed._supervise_once()
    assert sdk.closed, "the SDK loop must be told to stop, or it retries every second"
    assert feed._feed is None
    feed._supervise_once()          # and stays down
    assert len(_FakeFeed.instances) == 1
    feed.stop()


def test_error_burst_parks_the_feed_instead_of_retrying(feed, monkeypatch):
    from data import dhan_ws
    _window(monkeypatch, True)
    feed.start()
    sdk = feed._feed
    for _ in range(dhan_ws.ERROR_BURST):
        feed._on_error(sdk, "server rejected WebSocket connection: HTTP 429")
    feed._supervise_once()
    assert sdk.closed and feed._feed is None
    assert feed._cooldown_until > time.monotonic()
    feed._supervise_once()
    assert feed._feed is None, "parked means parked -- no reconnect until the cooldown"
    # cooldown expired -> one more attempt, with a longer next backoff
    feed._cooldown_until = 0.0
    feed._supervise_once()
    assert feed._feed is not None
    assert feed._backoff > dhan_ws.BACKOFF_START
    feed.stop()


def test_a_few_errors_do_not_park_a_live_feed(feed, monkeypatch):
    from data import dhan_ws
    _window(monkeypatch, True)
    feed.start()
    sdk = feed._feed
    for _ in range(dhan_ws.ERROR_BURST - 1):
        feed._on_error(sdk, "no close frame received")
    feed._supervise_once()
    assert feed._feed is sdk and not sdk.closed
    feed.stop()


def test_connecting_clears_the_error_history(feed, monkeypatch):
    _window(monkeypatch, True)
    feed.start()
    feed._on_error(feed._feed, "boom")
    feed._on_connect(feed._feed)
    assert not feed._errors
    feed.stop()


# ── nothing stale is ever written ─────────────────────────────────────────

def test_disconnect_clears_the_snapshot(feed, monkeypatch):
    _window(monkeypatch, True)
    feed.start()
    sec = next(iter(feed._sec_to_col))
    feed._ltp[sec] = 23346.4
    feed._prev_close[sec] = 23270.6
    assert feed.snapshot()
    _window(monkeypatch, False)
    feed._supervise_once()
    assert feed.snapshot() == {}, "a stale tick must not look current after the close"
    feed.stop()


def test_flush_writes_nothing_when_the_market_is_shut(feed, monkeypatch, temp_db):
    """Friday's close must not be stored again under Saturday's date."""
    from db.schema import init_db, get_connection
    init_db()
    sec = next(iter(feed._sec_to_col))
    feed._ltp[sec] = 23346.4
    feed._prev_close[sec] = 23270.6
    _window(monkeypatch, False)
    feed._flush_to_db()
    conn = get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM index_levels").fetchone()[0] == 0
    finally:
        conn.close()


def test_flush_stores_a_row_during_the_session(feed, monkeypatch, temp_db):
    from db.schema import init_db, get_connection
    init_db()
    sec = next(iter(feed._sec_to_col))
    col = feed._sec_to_col[sec]
    feed._ltp[sec] = 23346.4
    feed._prev_close[sec] = 23270.6
    _window(monkeypatch, True)
    feed._flush_to_db()
    conn = get_connection()
    try:
        row = conn.execute(f"SELECT {col} FROM index_levels").fetchone()
        assert row[0] == 23346.4
    finally:
        conn.close()


# ── the dashboard's own quote poll ────────────────────────────────────────

def _live_quotes_json(monkeypatch, market_open, cache):
    import asyncio, json
    pytest.importorskip("fastapi")
    from dashboard import server
    from pipeline import scheduler
    monkeypatch.setattr(scheduler, "is_market_hours", lambda: market_open)

    def boom(*a, **k):
        raise AssertionError("Dhan must not be polled while the market is shut")

    monkeypatch.setattr("data.dhan.fetch_live_quotes", boom)
    for k, v in cache.items():
        monkeypatch.setitem(server._live_quotes_cache, k, v)
    return json.loads(asyncio.run(server.api_live_quotes()).body)


def test_closed_market_serves_the_last_quotes_without_calling_dhan(monkeypatch):
    """An open tab polls every 15s; each miss cost ~500 Dhan quote requests."""
    today = dt.date.today()
    last = {"ACME": {"ltp": 101.5, "chg_pct": 1.25}}
    assert _live_quotes_json(monkeypatch, False, {"date": str(today), "data": last}) == last


def test_closed_market_does_not_serve_yesterdays_quotes(monkeypatch):
    out = _live_quotes_json(monkeypatch, False,
                            {"date": "2026-01-01", "data": {"ACME": {"ltp": 1.0}}})
    assert out == {}


# ── universe hygiene ──────────────────────────────────────────────────────

def test_nse_placeholder_scrips_are_not_tracked():
    """
    NSE's ind_nifty500list.csv carries corporate-action placeholders:
        Dummy HEG Ltd.,Metals & Mining,DUMMYHEG,EQ,DUM545A01024
    Not tradeable, no Dhan security_id -- DUMMYHEG was scored daily on empty
    inputs and warned "security_id not found" on every quote fetch.
    """
    import pandas as pd
    from data.index_constituents import _extract_symbols
    df = pd.DataFrame({"Symbol": ["RELIANCE", "DUMMYHEG", "heg", " TCS "]})
    assert _extract_symbols(df) == ["HEG", "RELIANCE", "TCS"]
