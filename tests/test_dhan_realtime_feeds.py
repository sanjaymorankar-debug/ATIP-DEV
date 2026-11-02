"""
Dhan real-time market data (2026-10-31): DP-01 index and stock WebSocket, DP-03 1-minute bars, DP-04 tick capture
and its retention, DP-05 20-level depth, DP-08 option chain, MON-07 feed health, BR-01 broker health, the Live tab
and the phone's Live screen.

Nothing here connects to Dhan or NSE: the SDK, the depth socket and both chain sources are fakes. The last test
replays a whole session (09:59 -> 15:45) through the stock feed on a driven clock.
"""
import asyncio
import json
import logging
import struct
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def conn(temp_db):
    from db.schema import get_connection, init_db
    init_db()
    c = get_connection()
    yield c
    c.close()


def _status(conn):
    return {r[0]: dict(zip(("feed", "mode", "subscribed", "ticks", "detail"), r)) for r in
            conn.execute("SELECT feed, mode, subscribed, ticks, detail FROM live_feed_status")}


class _Clock:
    """A settable datetime.now() for a module that did `from datetime import datetime`."""
    at = datetime(2026, 11, 2, 10, 0, 0)

    def __init__(self, at):
        _Clock.at = at

    @classmethod
    def patch(cls, monkeypatch, module, at):
        cls.at = at

        class D(datetime):
            @classmethod
            def now(cls_, tz=None):
                return cls.at
        monkeypatch.setattr(module, "datetime", D)
        return cls


# ══ index feed (data/dhan_ws.py): the two errors of 2026-10-30 ═════════════════════════════════════════════

class _SDK:
    instances = []

    def __init__(self, ctx, instruments, version="v2", on_connect=None, on_message=None, on_close=None, on_error=None):
        self.instruments, self.closed, self.started = instruments, False, False
        self.on_connect, self.on_message, self.on_close, self.on_error = on_connect, on_message, on_close, on_error
        _SDK.instances.append(self)

    def start(self):
        self.started = True

        class T:
            def is_alive(self):
                return True
        return T()

    def close_connection(self):
        self.closed = True


@pytest.fixture
def index_feed(monkeypatch):
    from data import dhan_ws
    _SDK.instances = []
    monkeypatch.setattr(dhan_ws, "HAS_MARKETFEED", True)
    monkeypatch.setattr(dhan_ws, "MarketFeed", _SDK, raising=False)
    monkeypatch.setattr(dhan_ws, "get_dhan_client", lambda: (None, None))
    _SDK.IDX, _SDK.Ticker = 0, 15
    return dhan_ws


def test_the_close_at_15_45_is_not_logged_as_an_error(index_feed, monkeypatch, conn, caplog):
    """2026-10-30 15:45:07: 'sent 1000 (OK); no close frame received' was ATIP's own close, logged as an ERROR."""
    monkeypatch.setattr(index_feed, "feed_window_open", lambda *a: True)
    m = index_feed.IndexFeedManager(flush_interval=60)
    m._connect()
    sdk = m._feed
    m._on_connect(sdk)
    monkeypatch.setattr(index_feed, "feed_window_open", lambda *a: False)
    caplog.set_level(logging.INFO, logger="data.dhan_ws")
    m._supervise_once()                                      # the window closed: detach and close
    assert sdk.closed and m._feed is None
    m._on_error(sdk, "sent 1000 (OK); no close frame received")
    m._on_close(sdk)
    assert not m._errors, "the SDK reporting ATIP's own close must not count toward an error burst"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING and "closed by ATIP" not in r.message
                and "parked" not in r.message]
    assert any("closed by ATIP" in r.message and r.levelno == logging.INFO for r in caplog.records)
    assert _status(conn)["indices"]["mode"] == "IDLE"


def test_a_mid_session_drop_is_a_warning_and_the_reconnect_is_counted(index_feed, monkeypatch, conn, caplog):
    """2026-10-30 13:29:56: Dhan dropped the socket and the SDK reconnected by 13:29:58 -- not an ATIP fault."""
    monkeypatch.setattr(index_feed, "feed_window_open", lambda *a: True)
    m = index_feed.IndexFeedManager(flush_interval=60)
    m._connect()
    sdk = m._feed
    m._on_connect(sdk)
    assert m.mode == "WS"
    caplog.set_level(logging.INFO, logger="data.dhan_ws")
    m._on_error(sdk, "no close frame received or sent")
    assert m.mode == "RECONNECTING"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any(r.levelno == logging.WARNING and "SDK is reconnecting" in r.message for r in caplog.records)
    m._on_connect(sdk)                                       # the SDK's own reconnect
    assert m.mode == "WS" and m._reconnects == 1 and not m._errors
    assert any("reconnected after" in r.message for r in caplog.records)
    m._report(force=True)
    assert "1 reconnect(s)" in _status(conn)["indices"]["detail"]


def test_a_refused_first_connect_is_still_an_error_and_a_burst_still_parks(index_feed, monkeypatch, caplog):
    monkeypatch.setattr(index_feed, "feed_window_open", lambda *a: True)
    m = index_feed.IndexFeedManager(flush_interval=60)
    m._connect()
    sdk = m._feed
    caplog.set_level(logging.INFO, logger="data.dhan_ws")
    for _ in range(index_feed.ERROR_BURST):
        m._on_error(sdk, "server rejected WebSocket connection: HTTP 429")
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    m._supervise_once()
    assert m._feed is None and m.mode == "PARKED"


def test_index_ticks_are_counted_and_the_state_is_written(index_feed, monkeypatch, conn):
    monkeypatch.setattr(index_feed, "feed_window_open", lambda *a: True)
    m = index_feed.IndexFeedManager(flush_interval=60)
    m._connect()
    m._on_connect(m._feed)
    m._on_message(m._feed, {"type": "Ticker Data", "security_id": 13, "LTP": "25010.5"})
    m._report(force=True)
    row = _status(conn)["indices"]
    assert row["mode"] == "WS" and row["ticks"] == 1 and "1 of" in row["detail"]


def test_a_stopped_supervisor_writes_nothing_after_stop(index_feed, monkeypatch):
    """A thread left sleeping by stop() used to run once more -- after its test, against the real database."""
    monkeypatch.setattr(index_feed, "feed_window_open", lambda *a: False)
    monkeypatch.setattr(index_feed, "SUPERVISOR_TICK", 0.05)
    writes = []
    monkeypatch.setattr(index_feed, "record_feed_status", lambda *a, **k: writes.append(a))
    m = index_feed.IndexFeedManager(flush_interval=0.05)
    m.start()
    m.stop()
    n = len(writes)
    time.sleep(0.25)
    assert len(writes) == n


def test_credentials_are_masked_in_anything_logged_or_stored():
    from data.dhan_ws import safe
    fake_jwt = ".".join(["eyJ" + "a" * 24, "eyJ" + "b" * 24, "c" * 24])     # made up, built here so no scanner trips
    s = safe(f"InvalidURI: wss://api-feed.dhan.co?version=2&token={fake_jwt}&clientId=1234567890&authType=2")
    assert "eyJ" not in s and "1234567890" not in s and "token=****" in s and "clientId=****" in s


# ══ stock feed (data/stock_feed.py): 100 symbols, REST failover, gating, status ══════════════════════════════

@pytest.fixture
def stock(monkeypatch):
    from data import stock_feed as SF
    import data.dhan as D
    _SDK.instances = []
    monkeypatch.setattr(SF, "HAS_MARKETFEED", True)
    monkeypatch.setattr(SF, "MarketFeed", _SDK, raising=False)
    _SDK.NSE, _SDK.Quote = 1, 17
    monkeypatch.setattr(D, "get_dhan_client", lambda: (None, None))
    monkeypatch.setattr(D, "get_security_id", lambda s: {"security_id": str(1000 + int(s[1:])), "exchange": "NSE_EQ"})
    return SF


def test_at_most_100_symbols_on_one_connection(stock, monkeypatch, caplog):
    monkeypatch.setattr(stock, "feed_window_open", lambda *a: True)
    assert stock.symbol_cap({"max_symbols": 250}) == 100 and stock.symbol_cap({"max_symbols": 40}) == 40
    m = stock.StockFeedManager(cfg={"max_symbols": 250, "symbols": [f"S{i}" for i in range(150)]})
    caplog.set_level(logging.WARNING, logger="data.stock_feed")
    m._connect()
    assert len(_SDK.instances) == 1, "one connection"
    inst = _SDK.instances[0].instruments
    assert len(inst) == 100 and {i[2] for i in inst} == {17} and {i[0] for i in inst} == {1}
    assert any("cut to 100" in r.message for r in caplog.records)
    assert stock.MAX_SYMBOLS <= stock.DHAN_MAX_PER_CONNECTION


def test_no_connection_outside_the_market_window(stock, monkeypatch):
    monkeypatch.setattr(stock, "feed_window_open", lambda *a: False)
    m = stock.StockFeedManager(cfg={"symbols": ["S1"]})
    m._supervise_once()
    assert _SDK.instances == [] and m.mode == "IDLE"


def test_rest_failover_backs_off_on_refusals_and_resets_on_success(stock, monkeypatch):
    import data.dhan as D
    m = stock.StockFeedManager(cfg={"symbols": ["S1"], "rest_seconds": 60})
    m._sid_to_sym = {1001: "S1"}
    monkeypatch.setattr(D, "fetch_live_quotes", lambda syms: pd.DataFrame())      # DH-904: no quotes
    m._rest_poll()
    assert m._rest_wait == 120 and "next poll in 120s" in m.detail
    m._rest_poll(); m._rest_poll(); m._rest_poll()
    assert m._rest_wait == stock.REST_WAIT_MAX
    monkeypatch.setattr(D, "fetch_live_quotes", lambda syms: pd.DataFrame(
        [{"symbol": "S1", "ltp": 101.0, "open": 100, "high": 102, "low": 99, "prev_close": 100.0, "volume": 5000}]))
    m._rest_poll()
    assert m._rest_wait == 60 and m.snapshot()["S1"]["src"] == "rest"


def test_parked_websocket_falls_back_to_rest_polls(stock, monkeypatch):
    monkeypatch.setattr(stock, "feed_window_open", lambda *a: True)
    monkeypatch.setattr(stock, "HAS_MARKETFEED", False)
    polled = []
    m = stock.StockFeedManager(cfg={"symbols": ["S1"]})
    monkeypatch.setattr(m, "_rest_poll", lambda: polled.append(1))
    m._supervise_once()
    assert m.mode == "REST" and polled


def test_detached_socket_callbacks_are_not_errors(stock, monkeypatch, caplog):
    monkeypatch.setattr(stock, "feed_window_open", lambda *a: True)
    m = stock.StockFeedManager(cfg={"symbols": ["S1"]})
    m._connect()
    sdk = m._feed
    m._on_connect(sdk)
    m._disconnect("market closed")
    caplog.set_level(logging.INFO, logger="data.stock_feed")
    m._on_error(sdk, "sent 1000 (OK); no close frame received")
    assert not m._errors and not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_flush_writes_changed_quotes_and_the_status_row(stock, monkeypatch, conn):
    monkeypatch.setattr(stock, "feed_window_open", lambda *a: True)
    m = stock.StockFeedManager(cfg={"symbols": ["S1"]})
    m._sid_to_sym, m.mode = {1001: "S1"}, "WS"
    pkt = {"type": "Quote Data", "security_id": 1001, "LTP": "100.50", "open": "99", "high": "101", "low": "98.5",
           "volume": 1200, "total_buy_quantity": 10, "total_sell_quantity": 5}
    m._on_message(None, {"type": "Previous Close", "security_id": 1001, "prev_close": "99.00"})
    m._on_message(None, pkt)
    m._flush()
    m._flush()                                               # nothing changed: no second row
    n = conn.execute("SELECT COUNT(*) FROM live_quotes WHERE symbol='S1'").fetchone()[0]
    assert n == 1
    row = conn.execute("SELECT ltp, prev_close, chg_pct, source FROM live_quotes").fetchone()
    assert tuple(row) == (100.5, 99.0, 1.52, "dhan_ws")
    st = _status(conn)["stocks"]
    assert st["mode"] == "WS" and st["subscribed"] == 1 and st["ticks"] == 1


def test_off_by_default_every_stock_feed_row_reads_off(stock, monkeypatch, conn):
    monkeypatch.setattr(stock, "settings", lambda: dict(stock.DEFAULTS))
    assert stock.start_stock_feed() is None
    st = _status(conn)
    assert {st[f]["mode"] for f in ("stocks", "ticks", "minute_bars")} == {"OFF"}


# ══ 1-minute bars (data/ticks.py MinuteBars + REST fallback) ═══════════════════════════════════════════════

def test_minute_bars_from_packets_skip_the_partial_first_minute():
    from data.ticks import MinuteBars
    b = MinuteBars()
    t = datetime(2026, 11, 2, 10, 0, 30)
    b.update("ACME", 100.0, 1000, t)                              # 10:00, the first minute seen: partial
    b.update("ACME", 101.0, 1100, t + timedelta(seconds=20))
    b.update("ACME", 101.5, 1300, t + timedelta(seconds=40))      # 10:01
    b.update("ACME", 99.0, 1350, t + timedelta(seconds=55))
    b.update("ACME", 100.0, 1500, t + timedelta(seconds=70))      # 10:01:40
    b.update("ACME", 102.0, 1700, t + timedelta(seconds=95))      # 10:02
    assert b.take_closed(datetime(2026, 11, 2, 10, 1, 59)) == []  # 10:00 seeds, 10:01 still open
    got = b.take_closed(datetime(2026, 11, 2, 10, 2, 10))
    assert got == [("ACME", "2026-11-02 10:01:00", 101.5, 101.5, 99.0, 100.0, 400)]   # 1500 - 1100
    assert b.take_closed(datetime(2026, 11, 2, 10, 3, 0)) == [("ACME", "2026-11-02 10:02:00", 102.0, 102.0, 102.0,
                                                               102.0, 200)]


def test_rest_fallback_fills_only_the_missing_minutes(conn, monkeypatch):
    from data import ticks
    import data.dhan as D
    day = date(2026, 11, 2)
    ticks.write_minute_bars(conn, [("ACME", "2026-11-02 09:16:00", 1, 1, 1, 1, 10)], source="ws")
    full = [("FULL", f"2026-11-02 {9 + (15 + i) // 60:02d}:{(15 + i) % 60:02d}:00", 1, 1, 1, 1, 1) for i in range(375)]
    ticks.write_minute_bars(conn, full, source="ws")
    calls = []

    def fake(sym, f, t, interval=1, dhan=None):
        calls.append((sym, f, t, interval))
        ts = pd.date_range("2026-11-02 09:15", periods=4, freq="1min")
        return pd.DataFrame({"datetime": list(ts) + [pd.Timestamp("2026-11-01 15:29")], "open": [2] * 5,
                             "high": [3] * 5, "low": [1] * 5, "close": [2.5] * 5, "volume": [50] * 5})
    monkeypatch.setattr(D, "fetch_historical_intraday", fake)
    out = ticks.backfill_minute_bars(day, ["ACME", "FULL"], conn, now=datetime(2026, 11, 2, 16, 0))
    assert [c[0] for c in calls] == ["ACME"] and calls[0][3] == 1, "a symbol with every minute is not fetched"
    rows = conn.execute("SELECT CAST(ts AS TEXT), source, close FROM intraday_bars WHERE symbol='ACME' AND interval_min=1 "
                        "ORDER BY ts").fetchall()
    assert [tuple(r) for r in rows] == [("2026-11-02 09:15:00", "dhan", 2.5), ("2026-11-02 09:16:00", "ws", 1.0),
                                        ("2026-11-02 09:17:00", "dhan", 2.5), ("2026-11-02 09:18:00", "dhan", 2.5)]
    assert out["rows"] == 3, "the WebSocket's bar is kept, the other day's bar is not stored"


def test_bars_fallback_waits_while_the_websocket_streams(monkeypatch):
    from data import stock_feed as SF, ticks
    monkeypatch.setattr(SF, "settings", lambda: {**SF.DEFAULTS})
    assert ticks.run_bars_fallback()["status"] == "SKIPPED"
    monkeypatch.setattr(SF, "settings", lambda: {**SF.DEFAULTS, "stocks_enabled": True, "minute_bars": True})

    class F:
        mode = "WS"
    monkeypatch.setattr(SF, "_manager", F())
    assert "WebSocket" in ticks.run_bars_fallback()["reason"]


# ══ tick capture retention (DP-04, 90 days) ═══════════════════════════════════════════════════════════════

def test_tick_lake_and_capture_status_older_than_90_days_are_purged(conn, monkeypatch, tmp_path):
    from data import lake
    from db import purge
    monkeypatch.setattr(lake, "ROOT", tmp_path / "lake")
    old, new = date.today() - timedelta(days=100), date.today() - timedelta(days=3)
    df = pd.DataFrame({"symbol": ["ACME"], "ltp": [1.0], "received_at": ["x"]})
    p_old = lake.write("ticks", old, df, conn=conn)["path"]
    p_new = lake.write("ticks", new, df, conn=conn)["path"]
    for d in (old, new):
        conn.execute("INSERT INTO tick_capture_status (day, ticks) VALUES (?, 1)", (str(d),))
    conn.commit()
    res = purge.purge_old_data(dry_run=True)
    assert res["lake:ticks"] == 1 and Path(p_old).exists()
    res = purge.purge_old_data(dry_run=False)
    assert res["lake:ticks"] == 1 and res["tick_capture_status"] == 1
    from db.schema import get_connection
    c = get_connection()
    try:
        assert not Path(p_old).exists() and Path(p_new).exists()
        assert [str(r[0]) for r in c.execute("SELECT partition_date FROM lake_partition WHERE dataset='ticks'")] == [str(new)]
        assert [str(r[0]) for r in c.execute("SELECT day FROM tick_capture_status")] == [str(new)]
    finally:
        c.close()


# ══ 20-level depth (data/depth20.py) ═══════════════════════════════════════════════════════════════════════

def _msg(code, sid, levels):
    body = b"".join(struct.pack("<dII", p, q, o) for p, q, o in levels) + b"\0" * (16 * (20 - len(levels)))
    return struct.pack("<hBBiI", 12 + len(body), code, 1, sid, 0) + body


BIDS = [(100.00, 500, 5), (99.95, 400, 4)]
ASKS = [(100.05, 200, 2), (100.10, 300, 3)]


class _DepthWS:
    """A socket with recv(): its frames, then silence."""

    def __init__(self, frames):
        self.sent, self.frames = [], list(frames)

    async def send(self, m):
        self.sent.append(m)

    async def recv(self):
        if self.frames:
            return self.frames.pop(0)
        await asyncio.sleep(10)


def _depth(monkeypatch):
    from data import depth20 as D
    f = D.Depth20Feed(cfg={"sample_seconds": 0})
    f._sid_to_sym = {1333: "HDFCBANK"}
    f._running = True
    return D, f


def test_depth_session_closes_when_the_window_closes(conn, monkeypatch):
    D, f = _depth(monkeypatch)
    monkeypatch.setattr(D, "WINDOW_CHECK", 0.0)
    monkeypatch.setattr(D, "RECV_TIMEOUT", 0.01)
    open_ = iter([True, True, False])
    asyncio.run(asyncio.wait_for(f.session(_DepthWS([_msg(41, 1333, BIDS), _msg(51, 1333, ASKS)]),
                                           window=lambda: next(open_, False)), timeout=5))
    assert f.snapshots == 1
    assert conn.execute("SELECT COUNT(*) FROM depth20_snapshot").fetchone()[0] == 1, "flushed as the session ended"
    st = _status(conn)["depth20"]
    assert st["mode"] == "LIVE" and st["ticks"] == 1


def test_a_quiet_depth_socket_still_sees_the_window_close(monkeypatch):
    D, f = _depth(monkeypatch)
    monkeypatch.setattr(D, "WINDOW_CHECK", 0.0)
    monkeypatch.setattr(D, "RECV_TIMEOUT", 0.01)
    t0 = time.monotonic()
    asyncio.run(asyncio.wait_for(f.session(_DepthWS([]), window=lambda: False), timeout=5))
    assert time.monotonic() - t0 < 2


def test_depth_refusals_park_instead_of_killing_the_feed(conn):
    from data import depth20 as D
    f = D.Depth20Feed(cfg={})
    now = datetime(2026, 11, 2, 10, 0)
    f.refused(D.FeedRefused(805), now)
    assert f.mode == "PARKED" and f._park_until == now + timedelta(minutes=30) and "10:30" in f.detail
    f.refused(D.FeedRefused(806), now)
    assert f._park_until == datetime(2026, 11, 3) and "Data APIs" in f.detail and "next trading day" in f.detail
    assert _status(conn)["depth20"]["mode"] == "PARKED"


def test_depth_takes_at_most_50_stocks_on_one_connection(monkeypatch):
    from data import depth20 as D
    import data.dhan as DH
    monkeypatch.setattr(DH, "get_security_id", lambda s: {"security_id": s[1:], "exchange": "NSE_EQ"})
    f = D.Depth20Feed(cfg={"max_symbols": 80, "symbols": [f"S{i}" for i in range(1, 81)]})
    assert len(f.resolve()) == 50 and len(f.subscriptions()) == 1


def test_depth_status_detail_never_carries_the_token(conn):
    from data import depth20 as D
    f = D.Depth20Feed(cfg={})
    f._set("RECONNECTING", "InvalidURI: wss://depth-api-feed.dhan.co/twentydepth?token=SECRETTOKEN123&clientId=1100777&authType=2")
    assert "SECRETTOKEN123" not in f.detail and "1100777" not in f.detail
    assert "SECRETTOKEN123" not in _status(conn)["depth20"]["detail"]


def test_depth_off_by_default_reads_off(conn, monkeypatch):
    from data import depth20 as D
    monkeypatch.setattr(D, "settings", lambda: dict(D.DEFAULTS))
    assert D.start_depth20() is None and _status(conn)["depth20"]["mode"] == "OFF"


# ══ option chain (data/derivatives_store.py) ═══════════════════════════════════════════════════════════════

class _DhanChain:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def expiry_list(self, sid, seg):
        self.calls.append(("expiry_list", sid, seg))
        return {"status": "success", "remarks": "", "data": {"data": ["2026-11-03", "2026-11-10", "2026-11-17"],
                                                             "status": "success"}}

    def option_chain(self, sid, seg, expiry):
        self.calls.append(("option_chain", sid, seg, expiry))
        if self.fail:
            return {"status": "failure", "remarks": {"error_code": "DH-904", "error_message": "Too many requests"},
                    "data": ""}
        leg = lambda px, oi: {"last_price": px, "previous_close_price": px - 10, "oi": oi, "previous_oi": oi - 100,
                              "implied_volatility": 12.5, "volume": 900, "top_bid_price": px - 0.5,
                              "top_ask_price": px + 0.5, "top_bid_quantity": 75, "top_ask_quantity": 150}
        return {"status": "success", "remarks": "", "data": {"data": {"last_price": 25010.2, "oc": {
            "25000.000000": {"ce": leg(120.0, 5000), "pe": leg(95.0, 4000)}}}, "status": "success"}}


class _NSE:
    def json(self, url):
        return {"records": {"data": [{"strikePrice": 25000, "expiryDate": "03-Nov-2026",
                                      "CE": {"strikePrice": 25000, "expiryDate": "03-Nov-2026", "lastPrice": 121,
                                             "underlyingValue": 25009}}]}}


def test_dhan_chain_rows_for_the_nearest_expiries(conn, monkeypatch, tmp_path):
    from data import derivatives_store as DS, lake
    monkeypatch.setattr(lake, "ROOT", tmp_path / "lake")
    monkeypatch.setattr(DS, "_expiries", {})
    fake = _DhanChain()
    out = DS.snapshot_option_chain("NIFTY", conn, source="dhan", dhan=fake)
    assert out["status"] == "SUCCESS" and out["source"] == "dhan" and out["rows"] == 4      # 2 expiries x CE/PE
    assert [c for c in fake.calls if c[0] == "option_chain"] == [("option_chain", 13, "IDX_I", "2026-11-03"),
                                                                 ("option_chain", 13, "IDX_I", "2026-11-10")]
    r = conn.execute("SELECT ltp, `change`, iv, oi, oi_chg, bid, ask, bid_qty, ask_qty, underlying FROM "
                     "option_chain_snapshot WHERE option_type='CE' AND expiry='2026-11-03'").fetchone()
    assert tuple(r) == (120.0, 10.0, 12.5, 5000.0, 100.0, 119.5, 120.5, 75.0, 150.0, 25010.2)
    DS.snapshot_option_chain("NIFTY", conn, source="dhan", dhan=fake)
    assert sum(1 for c in fake.calls if c[0] == "expiry_list") == 1, "the expiry list is asked once a day"


def test_a_dhan_refusal_falls_back_to_the_nse_chain(conn, monkeypatch, tmp_path):
    from data import derivatives_store as DS, lake
    import data.nse_api as N
    monkeypatch.setattr(lake, "ROOT", tmp_path / "lake")
    monkeypatch.setattr(DS, "_expiries", {})
    monkeypatch.setattr(N, "client", lambda: _NSE())
    out = DS.snapshot_option_chain("BANKNIFTY", conn, source="dhan", dhan=_DhanChain(fail=True))
    assert out["source"] == "nse" and out["rows"] == 1 and "DH-904" in out["note"]


def test_chain_is_due_every_chain_minutes(conn):
    from data import derivatives_store as DS
    now = datetime(2026, 11, 2, 10, 0)
    assert DS.chain_due(conn, now, {"chain_minutes": 15})
    conn.execute("INSERT INTO option_chain_snapshot (ts, symbol, expiry, strike, option_type) VALUES "
                 "('2026-11-02 09:50:00','NIFTY','2026-11-03',25000,'CE')")
    conn.commit()
    assert not DS.chain_due(conn, now, {"chain_minutes": 15})
    assert DS.chain_due(conn, now + timedelta(minutes=5), {"chain_minutes": 15})
    assert DS.chain_due(conn, now, {"chain_minutes": 5})


def test_chain_tick_states_off_idle_and_live(conn, monkeypatch, tmp_path):
    from data import derivatives_store as DS, dhan_ws, lake
    monkeypatch.setattr(lake, "ROOT", tmp_path / "lake")
    monkeypatch.setattr(DS, "settings", lambda: dict(DS.DEFAULTS))
    assert DS.run_chain_tick()["status"] == "SKIPPED" and _status(conn)["option_chain"]["mode"] == "OFF"
    monkeypatch.setattr(DS, "settings", lambda: {**DS.DEFAULTS, "option_chain_enabled": True})
    monkeypatch.setattr(dhan_ws, "feed_window_open", lambda *a: False)
    DS.run_chain_tick()
    assert _status(conn)["option_chain"]["mode"] == "IDLE"
    monkeypatch.setattr(dhan_ws, "feed_window_open", lambda *a: True)
    monkeypatch.setattr(DS, "_expiries", {})
    fake = _DhanChain()
    import data.dhan as D
    monkeypatch.setattr(D, "get_dhan_client", lambda: (fake, None))
    out = DS.run_chain_tick()
    st = _status(conn)["option_chain"]
    assert out["rows"] == 8 and st["mode"] == "LIVE" and "NIFTY 4 rows (dhan)" in st["detail"]


def test_option_chain_calls_take_their_own_3_second_turn(monkeypatch):
    import data.dhan as D
    monkeypatch.setattr(D, "PACE_SECONDS", {"quote": 0.0, "data": 0.0, "chain": 3.1})
    monkeypatch.setattr(D, "_next_call", {})

    def option_chain(*a):
        return None
    D._pace(option_chain)
    assert D._next_call["chain"] - time.monotonic() > 3.0 and "data" not in D._next_call


# ══ MON-07 / BR-01: feed health ════════════════════════════════════════════════════════════════════════════

def _feeds_cfg(monkeypatch, stocks=False, depth=False, chain=False):
    from data import stock_feed as SF, depth20 as D, derivatives_store as DS
    monkeypatch.setattr(SF, "settings", lambda: {**SF.DEFAULTS, "stocks_enabled": stocks, "minute_bars": stocks})
    monkeypatch.setattr(D, "settings", lambda: {**D.DEFAULTS, "enabled": depth})
    monkeypatch.setattr(DS, "settings", lambda: {**DS.DEFAULTS, "option_chain_enabled": chain})


def test_off_feeds_read_off_not_missing_and_are_no_problem(conn, monkeypatch):
    from ops.data_health import feeds
    from data import dhan_ws
    _feeds_cfg(monkeypatch)
    monkeypatch.setattr(dhan_ws, "feed_window_open", lambda *a: True)
    got = {f["feed"]: f for f in feeds(conn, datetime(2026, 11, 2, 11, 0), use_config=True)}
    assert {k: v["state"] for k, v in got.items() if k != "indices"} == {
        "stocks": "OFF", "ticks": "OFF", "minute_bars": "OFF", "depth20": "OFF", "option_chain": "OFF"}
    assert not any(f["problem"] for k, f in got.items() if k != "indices")
    assert got["indices"]["state"] == "NOT STARTED" and got["indices"]["problem"]
    assert "depth20.enabled" in got["depth20"]["detail"]


def test_feed_states_idle_live_and_stale(conn, monkeypatch):
    from ops.data_health import feeds
    from data import dhan_ws
    _feeds_cfg(monkeypatch, stocks=True)
    now = datetime(2026, 11, 2, 11, 0)
    dhan_ws.record_feed_status("stocks", "WS", 40, 900, now - timedelta(seconds=20), now, "40 symbols", conn=conn)
    conn.execute("UPDATE live_feed_status SET updated_at=? WHERE feed='stocks'", (now - timedelta(seconds=30),))
    conn.commit()
    monkeypatch.setattr(dhan_ws, "feed_window_open", lambda *a: False)
    assert {f["feed"]: f["state"] for f in feeds(conn, now, True)}["stocks"] == "IDLE"
    monkeypatch.setattr(dhan_ws, "feed_window_open", lambda *a: True)
    s = {f["feed"]: f for f in feeds(conn, now, True)}["stocks"]
    assert s["state"] == "WS" and not s["problem"] and s["data_age_s"] == 20
    s = {f["feed"]: f for f in feeds(conn, now + timedelta(minutes=10), True)}["stocks"]
    assert s["state"] == "STALE" and s["problem"]


def test_a_viewer_goes_by_the_macs_rows(conn, monkeypatch):
    from ops.data_health import feeds
    from data import dhan_ws
    _feeds_cfg(monkeypatch)                                  # the viewer's own config has every feed off
    monkeypatch.setattr(dhan_ws, "feed_window_open", lambda *a: False)
    dhan_ws.record_feed_status("depth20", "LIVE", 20, 10, datetime(2026, 11, 2, 15, 0), None, "20 stocks", conn=conn)
    dhan_ws.record_feed_status("option_chain", "OFF", detail="off", conn=conn)
    got = {f["feed"]: f["state"] for f in feeds(conn, datetime(2026, 11, 2, 18, 0), use_config=False)}
    assert got["depth20"] == "IDLE" and got["option_chain"] == "OFF"


def test_broker_health_shows_off_and_never_blocks_a_buy_over_a_feed(conn, monkeypatch):
    from execution import broker_health as BH
    from data import dhan_ws
    _feeds_cfg(monkeypatch, stocks=True)
    monkeypatch.setattr(dhan_ws, "feed_window_open", lambda *a: True)
    monkeypatch.setattr(BH, "_dhan_checks", lambda network: [])
    monkeypatch.setattr(BH, "_zerodha", lambda: {"check": "zerodha_token", "status": "SKIPPED", "detail": ""})
    from orders.paper import ensure_tables
    ensure_tables(conn)
    now = datetime(2026, 11, 2, 11, 0)
    conn.execute("INSERT INTO live_quotes (symbol, ltp, timestamp) VALUES ('ACME', 1, ?)", (str(now)[:19],))
    dhan_ws.record_feed_status("indices", "WS", 14, 99, now, now, "14 of 14", conn=conn)
    dhan_ws.record_feed_status("stocks", "WS", 40, 9, now - timedelta(minutes=30), now, "40 symbols", conn=conn)
    conn.execute("UPDATE live_feed_status SET updated_at=?", (now - timedelta(seconds=20),))   # both still reporting
    conn.commit()
    r = BH.check(network=False, now=now, notify=False)
    by = {c["check"]: c for c in r["checks"]}
    assert by["depth20_feed"]["status"] == "OFF" and by["option_chain"]["status"] == "OFF"
    assert by["index_feed"]["status"] == "OK"
    assert by["stock_feed"]["status"] == "DEGRADED" and by["stock_feed"]["state"] == "STALE"
    assert r["overall"] == "DEGRADED"
    assert BH.gate(conn, "BUY")[0] == "PASS", "a feed problem informs; it does not change order behaviour"


def test_off_checks_alone_leave_the_overall_ok(conn, monkeypatch):
    from execution import broker_health as BH
    from orders.paper import ensure_tables
    ensure_tables(conn)
    monkeypatch.setattr(BH, "_dhan_checks", lambda network: [])
    monkeypatch.setattr(BH, "_feeds", lambda now, s: [{"check": "depth20_feed", "status": "OFF", "detail": "off"}])
    monkeypatch.setattr(BH, "_zerodha", lambda: {"check": "zerodha_token", "status": "SKIPPED", "detail": ""})
    assert BH.check(network=False, now=datetime(2026, 11, 1, 11, 0), notify=False)["overall"] == "OK"


def test_one_alert_when_a_feed_turns_degraded_and_one_on_recovery(monkeypatch):
    from execution import broker_health as BH
    import alerts.telegram as tg
    sent = []
    monkeypatch.setattr(tg, "notify", lambda msg, **kw: sent.append((msg, kw)))
    now = datetime(2026, 11, 2, 11, 0)
    ok = [{"check": "stock_feed", "status": "OK", "detail": "WS"}]
    bad = [{"check": "stock_feed", "status": "DEGRADED", "detail": "REST: failover"}]
    BH._feed_alerts(ok, bad, now)
    BH._feed_alerts(bad, bad, now)
    BH._feed_alerts(bad, ok, now)
    BH._feed_alerts([{"check": "stock_feed", "status": "SKIPPED"}], ok, now)
    BH._feed_alerts(ok, [{"check": "quote_freshness", "status": "DEGRADED"}], now)
    assert [m for m, _ in sent] == ["⚠️ <b>Live feed stock_feed: OK → DEGRADED</b>\nREST: failover",
                                    "🟢 <b>Live feed stock_feed: DEGRADED → OK</b>\nWS"]


def test_mon07_market_data_health_lists_every_feed(conn, monkeypatch):
    from ops import health
    _feeds_cfg(monkeypatch)
    out = health.market_data()
    assert set(out["feeds"]) == {"indices", "stocks", "ticks", "minute_bars", "depth20", "option_chain"}
    assert out["feeds"]["depth20"]["state"] == "OFF"


# ══ the Live tab, /api/live-feeds and the phone ════════════════════════════════════════════════════════════

@pytest.fixture
def client(conn):
    from fastapi.testclient import TestClient
    from dashboard.server import app
    return TestClient(app)


def test_api_live_feeds_returns_every_feed(client, monkeypatch):
    _feeds_cfg(monkeypatch)
    d = client.get("/api/live-feeds").json()
    assert [f["feed"] for f in d["feeds"]] == ["indices", "stocks", "ticks", "minute_bars", "depth20", "option_chain"]
    assert {f["state"] for f in d["feeds"][1:]} == {"OFF"} and "window_open" in d


def test_api_live_feeds_on_a_viewer_reads_the_copied_rows(client, monkeypatch, conn):
    from data import dhan_ws
    monkeypatch.setenv("ATIP_ROLE", "viewer")
    dhan_ws.record_feed_status("stocks", "WS", 40, 5, datetime.now(), datetime.now(), "40 symbols", conn=conn)
    d = {f["feed"]: f for f in client.get("/api/live-feeds").json()["feeds"]}
    assert d["stocks"]["enabled"] is True and d["depth20"]["enabled"] is False


def test_the_dashboard_has_a_live_tab_and_the_phone_a_live_screen(client):
    from dashboard.server import build_html
    html = build_html({})
    assert "showTab('livefeeds',this);atipLoadFeeds('lfBox')\">Live</div>" in html and 'id="livefeeds"' in html
    assert "window.atipLoadFeeds" in html
    m = client.get("/m").text
    assert 'id="tLive"' in m and "tab')==='live'" in m and "window.atipLoadFeeds" in m
    assert "window.atipLoadFeeds" in client.get("/market").text


def test_both_apps_have_a_live_tab():
    kt = (ROOT / "mobile/android/app/src/main/java/com/atip/mobile/MainActivity.kt").read_text()
    assert '"/m?tab=live" to R.string.tab_live' in kt
    assert '<string name="tab_live">Live</string>' in (ROOT / "mobile/android/app/src/main/res/values/strings.xml").read_text()
    assert 'url("/m?tab=live")' in (ROOT / "mobile/ios/ATIP/ContentView.swift").read_text()


# ══ constraints ═════════════════════════════════════════════════════════════════════════════════════════════

def test_no_feed_module_can_place_an_order():
    for f in ("data/dhan_ws.py", "data/stock_feed.py", "data/depth20.py", "data/derivatives_store.py", "data/ticks.py",
              "ops/data_health.py"):
        src = (ROOT / f).read_text()
        for word in ("place_order", "from orders", "import orders", "execution.oms", "get_execution_client"):
            assert word not in src, f"{f} mentions {word}"


def test_every_feed_is_off_in_the_template():
    cfg = json.loads((ROOT / "config_template.json").read_text())
    lf = cfg["live_feed"]
    assert lf["stocks_enabled"] is False and lf["capture_ticks"] is False and lf["minute_bars"] is False
    assert cfg["depth20"]["enabled"] is False and cfg["derivatives"]["option_chain_enabled"] is False
    from data import stock_feed, depth20, derivatives_store
    assert not stock_feed.DEFAULTS["stocks_enabled"] and not depth20.DEFAULTS["enabled"]
    assert not derivatives_store.DEFAULTS["option_chain_enabled"]


# ══ a whole session, replayed (the Test-server run on a day without a market) ══════════════════════════════

def test_a_replayed_session_from_open_to_close(stock, monkeypatch, conn):
    """09:59:30 -> 10:04 of packets for 3 stocks through the stock feed with minute bars on, then 15:45: the window
    closes and the socket is let go. live_quotes, live_feed_status and intraday_bars must all show it."""
    from data import ticks
    clock = _Clock.patch(monkeypatch, stock, datetime(2026, 11, 2, 9, 59, 30))
    window = {"open": True}
    monkeypatch.setattr(stock, "feed_window_open", lambda *a: window["open"])
    m = stock.StockFeedManager(cfg={"symbols": ["S1", "S2", "S3"], "minute_bars": True, "flush_seconds": 60})
    m._supervise_once()                                      # inside the window: connect
    sdk = _SDK.instances[-1]
    assert m.mode == "WS" and len(sdk.instruments) == 3
    m._on_connect(sdk)
    vol = {1001: 0, 1002: 0, 1003: 0}
    t = clock.at
    while t < datetime(2026, 11, 2, 10, 4, 0):
        for sid in vol:
            vol[sid] += 100
            m._on_message(sdk, {"type": "Quote Data", "security_id": sid, "LTP": f"{100 + t.minute + sid % 10:.2f}",
                                "open": "100", "high": "110", "low": "95", "volume": vol[sid]})
        t += timedelta(seconds=10)
        clock.at = t
        if t.second == 0:
            m._flush()
    bars = conn.execute("SELECT symbol, CAST(ts AS TEXT), volume, source FROM intraday_bars WHERE interval_min=1 "
                        "ORDER BY symbol, ts"
                        ).fetchall()
    s1 = [(r[1][11:16], r[2]) for r in bars if r[0] == "S1"]
    assert s1 == [("10:00", 600), ("10:01", 600), ("10:02", 600), ("10:03", 600)], \
        "09:59 is partial (not written); 10:03 is closed by the 10:04:00 flush"
    assert {r[3] for r in bars} == {"ws"}
    st = _status(conn)
    assert st["stocks"]["mode"] == "WS" and st["minute_bars"]["ticks"] == 12
    assert conn.execute("SELECT COUNT(DISTINCT symbol) FROM live_quotes").fetchone()[0] == 3
    clock.at, window["open"] = datetime(2026, 11, 2, 15, 45, 30), False
    m._supervise_once()
    assert sdk.closed and m._feed is None and m.mode == "IDLE"
    m._on_error(sdk, "sent 1000 (OK); no close frame received")
    assert not m._errors
    m._flush()
    assert _status(conn)["stocks"]["mode"] == "IDLE"
    assert ticks.expected_minutes(date(2026, 11, 2), datetime(2026, 11, 2, 16, 0)) == 375


def test_a_restart_keeps_when_each_feed_last_had_data(conn):
    from data.dhan_ws import record_feed_status
    record_feed_status("stocks", "WS", 5, 900, datetime(2026, 10, 30, 15, 30), datetime(2026, 10, 30, 15, 31), "5",
                       conn=conn)
    record_feed_status("stocks", "IDLE", 0, 0, None, None, "outside the window", conn=conn)     # a new process
    r = conn.execute("SELECT mode, ticks, CAST(last_tick_at AS TEXT), CAST(last_flush_at AS TEXT) FROM live_feed_status "
                     "WHERE feed='stocks'").fetchone()
    assert tuple(r) == ("IDLE", 0, "2026-10-30 15:30:00", "2026-10-30 15:31:00")


def test_switched_on_feeds_read_idle_from_the_start_not_not_started(stock, conn, monkeypatch):
    """Prod smoke 2026-10-31: right after a start on a Saturday the stock feed and the chain read NOT STARTED."""
    from data import derivatives_store as DS, dhan_ws
    from ops.data_health import feeds
    monkeypatch.setattr(stock, "feed_window_open", lambda *a: False)
    monkeypatch.setattr(dhan_ws, "feed_window_open", lambda *a: False)
    m = stock.StockFeedManager(cfg={"symbols": ["S1"], "capture_ticks": True, "minute_bars": True, "rest_seconds": 3600})
    monkeypatch.setattr(stock, "SUPERVISOR_TICK", 3600)
    m.start()
    m.stop()
    monkeypatch.setattr(DS, "settings", lambda: {**DS.DEFAULTS, "option_chain_enabled": True})
    DS.report_state()
    assert {f: _status(conn)[f]["mode"] for f in ("stocks", "ticks", "minute_bars", "option_chain")} == {
        "stocks": "IDLE", "ticks": "IDLE", "minute_bars": "IDLE", "option_chain": "IDLE"}
    _feeds_cfg(monkeypatch, stocks=True, chain=True)
    assert "NOT STARTED" not in {f["state"] for f in feeds(conn, datetime(2026, 10, 31, 15, 0), True)
                                 if f["feed"] != "indices"}


def test_depth_waits_before_reconnecting_when_dhan_closes_the_socket(monkeypatch, conn):
    from data import depth20 as D, dhan_ws
    import data.dhan as DH
    import websockets
    f = D.Depth20Feed(cfg={})
    f._sid_to_sym, f._running = {1333: "HDFCBANK"}, True
    monkeypatch.setattr(dhan_ws, "feed_window_open", lambda *a: True)

    class Ctx:
        def get_access_token(self):
            return "t"

        def get_client_id(self):
            return "c"
    monkeypatch.setattr(DH, "get_dhan_client", lambda: (None, Ctx()))

    class Closed:                                            # subscribes, then Dhan closes it normally
        async def send(self, m):
            pass

        def __aiter__(self):
            async def gen():
                return
                yield
            return gen()
    connects, sleeps = [], []

    class CM:
        async def __aenter__(self):
            connects.append(1)
            return Closed()

        async def __aexit__(self, *a):
            return False
    monkeypatch.setattr(websockets, "connect", lambda url, max_size=None: CM())

    async def no_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 2:
            f._running = False
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    asyncio.run(f._main())
    assert len(connects) == 2 and sleeps == [5, 5], "a wait between reconnects, never a tight loop"
    assert f.mode == "RECONNECTING" and "closed by Dhan" in f.detail
