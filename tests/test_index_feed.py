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

class _FakeThread:
    """Stands in for the SDK's WS thread, which dies on a refused connect."""
    def __init__(self): self.alive = True
    def is_alive(self): return self.alive


class _FakeFeed:
    IDX = 0
    Ticker = 15

    instances = []

    def __init__(self, *a, **k):
        self.started = False
        self.closed = False
        self.thread = _FakeThread()
        _FakeFeed.instances.append(self)

    def start(self):
        self.started = True
        return self.thread          # dhanhq's MarketFeed.start() returns its thread

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


# ── frames that are not ticks ─────────────────────────────────────────────

def test_market_status_frame_is_not_treated_as_an_error(feed, monkeypatch):
    """
    dhanhq decodes Dhan's market-status frame (response code 7) to the bare
    string "Markets Open", and calls on_error from the same except clause it
    uses for a dropped socket. Dhan sends one per subscribed instrument at the
    09:15 open -- 14 of them -- which is above ERROR_BURST, so a healthy feed
    used to be parked at the open.
    """
    from data import dhan_ws
    _window(monkeypatch, True)
    feed.start()
    sdk = feed._feed
    for _ in range(len(feed._sec_to_col)):          # 14 indexes -> 14 status frames
        feed._on_message(sdk, "Markets Open")
    assert not feed._errors, "a status frame is not a connection error"
    feed._supervise_once()
    assert feed._feed is sdk and not sdk.closed, "the feed must survive the open"
    feed.stop()


@pytest.mark.parametrize("frame", ["Markets Open", "", None, 0, [1, 2], 3.5])
def test_non_tick_frames_are_ignored_quietly(feed, frame):
    feed._on_message(None, frame)                   # must not raise
    assert feed.snapshot() == {}


def test_a_real_status_packet_from_the_sdk_is_handled(feed):
    """The actual bytes Dhan sends, decoded by the installed SDK."""
    import struct
    pytest.importorskip("dhanhq")
    from dhanhq.marketfeed import MarketFeed
    packet = struct.pack("<BHBI", 7, 8, 0, 13)
    try:
        decoded = MarketFeed.process_data(packet)
    except TypeError:
        decoded = MarketFeed.process_data(object.__new__(MarketFeed), packet)
    assert not isinstance(decoded, dict), "if this becomes a dict, revisit _on_message"
    feed._on_message(None, decoded)                 # must not raise
    assert not feed._errors


# ── a feed that looks alive and is not ─────────────────────────────────────

def test_dead_sdk_thread_is_recycled(feed, monkeypatch):
    """
    dhanhq's run() awaits its FIRST connect outside its try block and catches
    only KeyboardInterrupt, so a refused connect at 09:00 kills the SDK thread
    while _feed stays bound: the feed is dead, silently, for the whole session.
    """
    _window(monkeypatch, True)
    feed.start()
    sdk = feed._feed
    sdk.thread.alive = False                        # the thread died on its first connect
    feed._supervise_once()
    assert sdk.closed and feed._feed is None, "a dead thread must be recycled"
    assert feed._cooldown_until > time.monotonic(), "and retried under the backoff"
    feed._cooldown_until = 0.0
    feed._supervise_once()
    assert feed._feed is not None and feed._feed is not sdk
    feed.stop()


def test_a_live_thread_is_left_alone(feed, monkeypatch):
    _window(monkeypatch, True)
    feed.start()
    sdk = feed._feed
    feed._supervise_once()
    assert feed._feed is sdk and not sdk.closed
    feed.stop()


@pytest.mark.parametrize("now,last_tick_min_ago,stalled", [
    (dt.datetime(2026, 9, 21, 12, 0), 11, True),    # mid-session silence
    (dt.datetime(2026, 9, 21, 12, 0), 2, False),    # ticking normally
    (dt.datetime(2026, 9, 21, 9, 5), 11, False),    # pre-open: no ticks expected yet
    (dt.datetime(2026, 9, 21, 15, 40), 11, False),  # after the close: silence is correct
    (dt.datetime(2026, 9, 21, 12, 0), None, False),  # freshly connected, no tick yet
])
def test_stall_detection(feed, now, last_tick_min_ago, stalled):
    sec = next(iter(feed._sec_to_col))
    if last_tick_min_ago is not None:
        feed._last_tick_at[sec] = now - dt.timedelta(minutes=last_tick_min_ago)
    assert feed._stalled(now) is stalled


def test_a_connection_that_never_ticks_is_stalled_too(feed, monkeypatch):
    """Subscription accepted, nothing ever delivered -- the silent failure."""
    from data import dhan_ws
    _window(monkeypatch, True)
    feed.start()
    assert feed._stalled(dt.datetime(2026, 9, 21, 12, 0)) is False   # just connected
    feed._connected_since = time.monotonic() - dhan_ws.TICK_STALL_SECONDS - 1
    assert feed._stalled(dt.datetime(2026, 9, 21, 12, 0)) is True
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
