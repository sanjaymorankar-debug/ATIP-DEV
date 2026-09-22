"""
Regression tests for defects that actually occurred.

Each of these shipped, ran in production, and corrupted something quietly. They
are here because a test that was never written is why they lasted as long as
they did — every one was invisible in the logs and only surfaced through a
downstream number that looked wrong.
"""

import datetime as dt
import json

import pandas as pd
import pytest


# ── Dhan candles are stamped at IST midnight, not UTC ─────────────────────

def test_ist_conversion_does_not_shift_the_date():
    """
    pd.to_datetime(unit='s') reads epoch as UTC. Dhan stamps daily candles at
    00:00 IST = 18:30 UTC the PREVIOUS day, so every bar was filed one day
    early: Monday's data under Sunday, Friday's under Thursday.

    prices_daily ended up with 89 Sundays and 50 Fridays. NSE does not trade on
    Sunday.
    """
    from data.dhan import _ist_dates

    # Real epochs from /charts/historical for RELIANCE.
    epochs = [1787509800, 1788460200]
    naive = pd.to_datetime(epochs, unit="s").date
    ist = _ist_dates(epochs)

    assert [str(d) for d in naive] == ["2026-08-23", "2026-09-03"]   # the bug
    assert [str(d) for d in ist] == ["2026-08-24", "2026-09-04"]     # the fix
    assert ist[0].weekday() == 0, "should be Monday, not Sunday"
    assert ist[1].weekday() == 4, "should be Friday, not Thursday"


def test_ist_conversion_never_lands_on_a_weekend_for_trading_days():
    from data.dhan import _ist_dates
    # A full trading week of IST-midnight stamps.
    base = 1787509800                       # Mon 2026-08-24 00:00 IST
    week = [base + 86400 * i for i in range(5)]
    for d in _ist_dates(week):
        assert d.weekday() < 5, f"{d} is a weekend — NSE does not trade then"


# ── market-feed responses are double-wrapped by the SDK ───────────────────

def test_unwrap_feed_handles_the_sdk_envelope():
    """
    Quotes arrive at resp["data"]["data"][SEGMENT]. Reading one level too
    shallow returned {} with no exception, so the index job logged
    "0 indexes fetched" and reported SUCCESS — the dashboard showed no NSE index
    and it looked like Dhan was withholding data.
    """
    from data.dhan import _unwrap_feed

    wrapped = {"status": "success", "remarks": "",
               "data": {"status": "success",
                        "data": {"IDX_I": {"13": {"last_price": 23779.15}}}}}
    assert _unwrap_feed(wrapped) == {"IDX_I": {"13": {"last_price": 23779.15}}}


def test_unwrap_feed_handles_the_unwrapped_shape_too():
    """Must work whichever shape a given SDK version returns."""
    from data.dhan import _unwrap_feed
    flat = {"status": "success", "data": {"NSE_EQ": {"2885": {"last_price": 1309.5}}}}
    assert _unwrap_feed(flat) == {"NSE_EQ": {"2885": {"last_price": 1309.5}}}


@pytest.mark.parametrize("junk", [{}, {"data": None}, {"data": []}, None, "nope"])
def test_unwrap_feed_survives_junk(junk):
    from data.dhan import _unwrap_feed
    assert _unwrap_feed(junk) == {}


# ── signal log: dedupe on the signal, not the code version ────────────────

def test_signal_log_dedupes_on_date_symbol_signal(temp_db):
    """
    The first guard keyed on model_version, so ANY commit re-appended a date's
    whole signal set and double-counted it in every hit rate — including the
    commit that added the guard, which cannot change what the engine
    recommended.
    """
    from scores import signal_log as S
    from db.schema import get_connection

    conn = get_connection()
    S.ensure_tables(conn)
    import uuid
    cols = ("id, run_id, logged_at, signal_date, symbol, signal, entry_price, "
            "model_version, weights_hash")

    def insert(model, logged_at):
        conn.execute(f"INSERT INTO signal_log ({cols}) VALUES (?,?,?,?,?,?,?,?,?)",
                     (str(uuid.uuid4()), "run", logged_at, "2026-09-07",
                      "ACME", "BUY", 100.0, model, "w1"))
    insert("aaaaaaa", "2026-09-08T01:00:00")
    insert("bbbbbbb", "2026-09-08T02:00:00")     # same signal, newer code
    conn.commit()

    flagged = S.flag_duplicates(conn)
    assert flagged == 1
    counted = conn.execute(
        "SELECT COUNT(*) FROM signal_log WHERE duplicate_of IS NULL").fetchone()[0]
    retained = conn.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0]
    assert counted == 1, "the same signal must count once"
    assert retained == 2, "append-only: nothing is deleted, only flagged"
    kept = conn.execute("SELECT model_version FROM signal_log "
                        "WHERE duplicate_of IS NULL").fetchone()[0]
    assert kept == "aaaaaaa", "the FIRST emission is the record"
    assert S.flag_duplicates(conn) == 0, "must be idempotent"
    conn.close()


def test_signal_log_keeps_a_genuinely_different_signal(temp_db):
    """A BUY that becomes a SELL on the same date is new information."""
    from scores import signal_log as S
    from db.schema import get_connection
    import uuid

    conn = get_connection()
    S.ensure_tables(conn)
    for sig in ("BUY", "SELL"):
        conn.execute(
            "INSERT INTO signal_log (id, run_id, logged_at, signal_date, symbol, "
            "signal, entry_price, model_version, weights_hash) VALUES (?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), "r", "2026-09-08T01:00:00", "2026-09-07",
             "ACME", sig, 100.0, "m", "w"))
    conn.commit()
    assert S.flag_duplicates(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM signal_log "
                        "WHERE duplicate_of IS NULL").fetchone()[0] == 2
    conn.close()


# ── trading-session arithmetic ────────────────────────────────────────────

def test_sessions_between_counts_sessions_not_calendar_days():
    """
    Timing was once measured in BARS, so a signal whose next available bar was
    27 sessions later reported "hit in 1 day".
    """
    from dashboard.server import sessions_between
    mon, tue = dt.date(2026, 9, 7), dt.date(2026, 9, 8)
    assert sessions_between(mon, tue) == 1
    fri, nxt_mon = dt.date(2026, 9, 4), dt.date(2026, 9, 7)
    assert sessions_between(fri, nxt_mon) == 1, "a weekend is not two sessions"


def test_sessions_between_skips_the_weekend():
    from dashboard.server import sessions_between
    assert sessions_between(dt.date(2026, 9, 4), dt.date(2026, 9, 11)) == 5


# ── API serialisation ─────────────────────────────────────────────────────

def test_json_safe_encodes_dates():
    """
    PARSE_DECLTYPES returns real date/datetime objects, which json.dumps cannot
    encode — /api/scores, /api/mh and /api/news all returned 500.
    """
    from dashboard.server import json_safe
    payload = json_safe([{"date": dt.date(2026, 9, 7),
                          "when": dt.datetime(2026, 9, 7, 15, 30),
                          "score": 68.2, "sym": "ACME", "none": None}])
    encoded = json.dumps(payload)
    assert "2026-09-07" in encoded
    assert json.loads(encoded)[0]["score"] == 68.2


# ── the index panel must not be capped at a stale score date ──────────────

def test_index_panel_shows_the_latest_reading_not_the_scored_date(temp_db):
    """
    Two faults, opposite directions. Matching the scored date exactly left the
    panel blank outside market hours. The `date<=td` fix also CAPPED it, so when
    scoring fell a session behind the panel showed a pre-open snapshot at
    +0.00% while that day's actual close sat unused in the table.
    """
    from db.schema import get_connection
    from dashboard.server import get_indexes

    conn = get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS index_levels "
                 "(date DATE, time TEXT, nifty50 REAL, nifty50_chg REAL)")
    conn.execute("INSERT INTO index_levels VALUES ('2026-09-07','08:43:16',23779.15,0.0)")
    conn.execute("INSERT INTO index_levels VALUES ('2026-09-08','15:31:02',23635.10,-0.606)")
    conn.commit()
    conn.close()

    idx = get_indexes("2026-09-07")          # scores a session behind
    assert idx["nifty50"] == 23635.10, "must show the newest reading available"
    assert idx["nifty50_chg"] == -0.606


def test_intraday_index_snapshot_refuses_a_past_date(monkeypatch):
    """
    The row stamps `now`'s clock time and `now`'s prices, so dating it in the
    past corrupts that day's index history.
    """
    import datetime as _dt
    import logging
    from data import markets

    monkeypatch.setattr(markets, "fetch_index_quotes", lambda cols: pd.DataFrame())

    warnings = []
    monkeypatch.setattr(markets.log, "warning", lambda m, *a, **k: warnings.append(str(m)))

    markets.fetch_intraday_indexes(_dt.date(2020, 1, 1))
    assert any("LIVE prices" in w for w in warnings),         f"a past date must be refused with an explanation, got: {warnings}"


# ── CMP % change must be measured against the PREVIOUS close ──────────────

def test_pct_change_is_against_the_previous_close():
    """
    The dashboard compared a live price against the DISPLAYED session's own
    close. With scores a day behind that reported the move since that session —
    NIACL showed -11.2%, which was the real 09-07 to 09-08 move, not today's
    change. With scores current it reported 0.00%, comparing a close to itself.
    """
    from dashboard.server import pct_change
    assert pct_change(205.22, 231.20) == -11.24     # NIACL, as it really moved
    assert pct_change(1231.30, 1156.50) == 6.47     # PVRINOX
    assert pct_change(100.0, 100.0) == 0.0


@pytest.mark.parametrize("last,prev", [
    (100.0, None), (100.0, 0), (100.0, -5), (None, 100.0),
    ("", 100.0), (100.0, "n/a"),
])
def test_pct_change_is_none_when_unknowable(last, prev):
    """A missing or nonsensical previous close must not render as 0%."""
    from dashboard.server import pct_change
    assert pct_change(last, prev) is None


def test_chg_span_shows_absolute_and_percentage():
    """Industry standard for a quote: the move in rupees and in percent."""
    from dashboard.server import chg_span
    out = chg_span(205.22, 231.20)
    assert "-25.98" in out and "-11.24%" in out
    assert "#dc2626" in out                       # red for a fall
    up = chg_span(1231.30, 1156.50)
    assert "+74.80" in up and "+6.47%" in up
    assert "#059669" in up                        # green for a rise


def test_chg_span_is_empty_without_a_previous_close():
    from dashboard.server import chg_span
    assert chg_span(100.0, None) == ""


# ── stale global data must be visibly stale ───────────────────────────────

def test_stale_days_measures_age():
    """
    global_markets had not been written since 2026-08-03 — over a month — and
    the sidebar showed those figures with no date at all, so a month-old reading
    was indistinguishable from a live one. The NSE panel beside it had always
    shown "as of ...".
    """
    from dashboard.server import _stale_days
    today = dt.date.today()
    assert _stale_days(today.isoformat()) == 0
    assert _stale_days((today - dt.timedelta(days=36)).isoformat()) == 36
    assert _stale_days("not-a-date") is None
    assert _stale_days(None) is None


def test_signal_history_reports_a_one_year_window(temp_db):
    """
    An all-time hit rate keeps counting signals from a model version that no
    longer exists. The tab now reports the last 12 months separately.
    """
    from dashboard.server import get_signal_history
    sh = get_signal_history()
    assert "report_1y" in sh
    assert "since_1y" in sh
    if sh["since_1y"]:
        since = dt.date.fromisoformat(sh["since_1y"])
        assert 364 <= (dt.date.today() - since).days <= 366


def test_global_panel_shows_the_newest_snapshot_of_the_day(temp_db):
    """
    global_markets holds three rows per date and `time` is a LABEL, not a clock
    ('premarket', 'intraday', 'overnight'). With no tie-break, SQLite served
    `ORDER BY date DESC` from the UNIQUE(date,time) index -- alphabetical within
    a date -- so the panel always showed the 01:30 premarket row: on 2026-09-18
    S&P +1.138% while the 17:30 row said -0.174%, the opposite sign.
    """
    from db.schema import get_connection
    from dashboard.server import get_global

    conn = get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS global_markets (id INTEGER PRIMARY KEY, date DATE, "
                 "time TEXT, sp500_chg REAL, created_at TIMESTAMP, UNIQUE(date,time))")
    conn.executemany("INSERT INTO global_markets (id,date,time,sp500_chg,created_at) VALUES (?,?,?,?,?)", [
        (254, "2026-09-18", "premarket", 1.138, "2026-09-18 01:30:08"),
        (272, "2026-09-18", "intraday", 1.138, "2026-09-18 10:00:15"),
        (273, "2026-09-18", "overnight", -0.174, "2026-09-18 17:30:05"),
    ])
    conn.commit()
    conn.close()

    assert get_global("2026-09-18")["sp500_chg"] == -0.174


def test_live_quote_payload_carries_the_brokers_previous_close():
    """
    The page's data-prev is the close before the SCORED date, and scores only
    land at 16:45 — so through a session it is one close too old. The payload
    must carry Dhan's own prev_close for the page to use instead.
    """
    import inspect
    from dashboard import server
    src = inspect.getsource(server)
    assert '"prev_close": prev' in src
    assert "if(qd.prev_close!=null && qd.prev_close>0) prev=qd.prev_close;" in src


def test_macd_columns_are_not_swapped():
    """
    compute_indicators reads the MACD frame BY POSITION, and the two libraries
    behind the wrapper disagree: pandas_ta emits (MACD, Hist, Signal) while the
    installed `ta` fallback used to emit (MACD, Signal, Hist). That put the
    signal line — a price-scale number — into macd_hist for all 7,164 stored
    rows, and macd_hist is MRI's heaviest input (weight 0.25), whose
    min(50+|x|*10, 100) mapping saturated in 63% of them.

    The invariant that catches either library drifting: hist == macd - signal.
    """
    import numpy as np
    from data.technical import ta, HAS_TA

    if not HAS_TA:
        import pytest
        pytest.skip("no TA library installed")

    close = pd.Series(100 + np.cumsum(np.sin(np.arange(200) / 7.0)), name="close")
    frame = ta.macd(close, fast=12, slow=26, signal=9)
    macd_col, col1, col2 = (frame.iloc[:, i] for i in range(3))

    # `macd == signal + hist` holds whichever way round the two are, so it
    # cannot catch the swap. Recompute the signal line from the MACD column
    # instead: it is an EMA of it, and the histogram is the remainder.
    want_signal = macd_col.ewm(span=9, adjust=False).mean()
    want_hist = macd_col - want_signal

    assert abs(col1.iloc[-1] - want_hist.iloc[-1]) < 1e-3, (
        f"column 1 must be the HISTOGRAM: got {col1.iloc[-1]:.4f}, "
        f"expected {want_hist.iloc[-1]:.4f} (signal is {want_signal.iloc[-1]:.4f})")
    assert abs(col2.iloc[-1] - want_signal.iloc[-1]) < 1e-3, (
        f"column 2 must be the SIGNAL line: got {col2.iloc[-1]:.4f}, "
        f"expected {want_signal.iloc[-1]:.4f}")
    # and they must be distinguishable at all, or the test proves nothing
    assert abs(want_signal.iloc[-1] - want_hist.iloc[-1]) > 0.1


def test_connections_wait_for_a_busy_writer(temp_db):
    """
    WAL lets readers and one writer coexist, but a second writer waits — and
    sqlite3's default patience is 5s, shorter than this system's own write
    transactions (run_scoring_pipeline holds one across ~502 symbols). That is
    what produced "Index feed DB flush failed: database is locked" and killed a
    post-market catch-up on 2026-09-20.
    """
    from db.schema import get_connection, init_db, BUSY_TIMEOUT_MS
    init_db()
    conn = get_connection()
    try:
        assert BUSY_TIMEOUT_MS >= 30_000, "must outlast a full scoring loop"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS
    finally:
        conn.close()


# ── MACD must be comparable across a universe priced Rs 7 to Rs 134,860 ─────

def test_macd_component_is_scale_free():
    """
    The histogram is a price-scale number, so the old absolute mapping
    (50 + |hist| * 10, full scale at +/-5 rupees) meant something completely
    different on a Rs 3,000 stock than on a Rs 50 one, and pinned the component
    at exactly 0 or 100 in 37.4% of stored rows. Normalised by price, the same
    percentage move must score the same at any price.
    """
    from data.technical import macd_component

    cheap = macd_component(15.0 / 100 * 100)        # Rs 15 hist on a Rs 100 share
    rich = macd_component(450.0 / 3000 * 100)       # Rs 450 hist on a Rs 3,000 share
    assert cheap == rich, "15% of price must score the same at either price"

    # ...and a rupee amount that used to saturate must no longer do so
    modest_on_expensive = macd_component(5.0 / 3000 * 100)   # Rs 5 on Rs 3,000
    assert modest_on_expensive == 55.0, modest_on_expensive   # barely off neutral...
    real_on_cheap = macd_component(5.0 / 50 * 100)           # Rs 5 on Rs 50
    assert real_on_cheap == 100.0


@pytest.mark.parametrize("pct,expected", [
    (0.0, 50.0),          # no momentum is neutral, not a score
    (1.667, 100.0),       # full scale
    (-1.667, 0.0),
    (5.0, 100.0),         # clamped, not extrapolated
    (-5.0, 0.0),
])
def test_macd_component_shape(pct, expected):
    from data.technical import macd_component
    assert macd_component(pct) == expected


def test_macd_component_is_signed_around_fifty():
    from data.technical import macd_component
    assert macd_component(0.5) > 50 > macd_component(-0.5)
    assert macd_component(0.5) - 50 == pytest.approx(50 - macd_component(-0.5))


def test_macd_component_drops_out_when_unavailable():
    """None must not become 50 — a fabricated neutral is indistinguishable from
    a measured one, and weighted_score already renormalises over what is there."""
    from data.technical import macd_component
    assert macd_component(None) is None
    assert macd_component("") is None


def test_indicators_store_the_normalised_histogram(temp_db):
    import numpy as np
    from data.technical import compute_indicators, HAS_TA
    if not HAS_TA:
        pytest.skip("no TA library installed")

    n = 120
    close = 3000 + np.cumsum(np.sin(np.arange(n) / 5.0) * 8)
    df = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n).astype(str),
        "open": close, "high": close * 1.01, "low": close * 0.99,
        "close": close, "adj_close": [None] * n, "volume": [100000] * n,
    })
    ind = compute_indicators("EXPENSIVE", df)

    assert ind["macd_hist_pct"] == pytest.approx(
        ind["macd_hist"] / close[-1] * 100, abs=1e-4)   # stored to 4 decimals
    assert abs(ind["macd_hist_pct"]) < 10, "a daily histogram is a small % of price"


def test_benchmark_resync_overwrites_every_price(temp_db):
    """
    The upsert refreshed close only, so re-syncing the NIFTY50 series after the
    2026-09-08 date fix corrected close and left open/high/low holding the next
    session's close in 280 rows — close outside [low, high] in every one.
    """
    import datetime as _dt
    from db.schema import init_db, get_connection
    from data.dhan import _store_benchmark_rows
    init_db()
    conn = get_connection()
    try:
        conn.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) "
                     "VALUES ('NIFTY50','2025-02-27',22124.7,22124.7,22124.7,22545.05,0,'dhan_index')")
        _store_benchmark_rows(conn, "NIFTY50", [_dt.date(2025, 2, 27)], [22545.05])
        row = conn.execute("SELECT open, high, low, close FROM prices_daily "
                           "WHERE symbol='NIFTY50' AND date='2025-02-27'").fetchone()
        assert tuple(row) == (22545.05,) * 4, tuple(row)
    finally:
        conn.close()


# ── the benchmark lagged the stocks by one session ────────────────────────

_NSE_INDEX_CLOSE_CSV = (
    "Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,"
    "Closing Index Value,Points Change,Change(%),Volume,Turnover (Rs. Cr.),P/E,P/B,Div Yield\n"
    "Nifty 50,21-09-2026,23330.2,23466.8,23314.8,23414.30,67.90,.29,213639227,17770.46,19.79,2.83,1.21\n"
    "Nifty Bank,18-09-2026,56172.85,56497.45,56073.55,56358.70,302.95,.54,237801140,8027.68,13.33,1.69,.7\n"
    "India VIX,21-09-2026,11.385,11.825,11.18,11.25,-0.14,-1.21,-,-,-,-,-\n"
).encode()


def test_nse_index_closes_are_read_for_their_own_date_only():
    """A row NSE dates to another session is never filed under this one."""
    from data.bhavcopy import parse_index_closes
    closes = parse_index_closes(_NSE_INDEX_CLOSE_CSV, dt.date(2026, 9, 21))
    assert closes["nifty50"] == (23414.30, 0.291)   # 67.90 / 23346.40, the 09-18 close
    assert closes["india_vix"][0] == 11.25
    assert "banknifty" not in closes, "a 09-18 row must not be read as 09-21"


def _nse_closes(monkeypatch, closes):
    from data import bhavcopy
    monkeypatch.setattr(bhavcopy, "get_nse_session", lambda: None)
    monkeypatch.setattr(bhavcopy, "download_index_closes", lambda d, s: closes)


def test_benchmark_gets_the_session_being_scored(temp_db, monkeypatch):
    """
    Dhan's to_date is exclusive and it had no 2026-09-21 index bar even at
    01:14 the next day, so at 16:45 the NIFTY50 benchmark always ended the
    session before the stocks it was compared with.
    """
    from db.schema import init_db, get_connection
    from data.bhavcopy import sync_nse_index_closes
    init_db()
    _nse_closes(monkeypatch, {"nifty50": (23414.3, 0.291), "india_vix": (11.25, -1.229)})
    res = sync_nse_index_closes(dt.date(2026, 9, 21))
    assert res["status"] == "SUCCESS"
    conn = get_connection()
    try:
        row = conn.execute("SELECT open, high, low, close, source FROM prices_daily "
                           "WHERE symbol='NIFTY50' AND date='2026-09-21'").fetchone()
        assert tuple(row) == (23414.3, 23414.3, 23414.3, 23414.3, "nse_index")
    finally:
        conn.close()


def test_unpublished_index_file_changes_nothing(temp_db, monkeypatch):
    from db.schema import init_db, get_connection
    from data.bhavcopy import sync_nse_index_closes
    init_db()
    _nse_closes(monkeypatch, None)                     # NSE answered 404
    assert sync_nse_index_closes(dt.date(2026, 9, 21))["status"] == "SKIPPED"
    conn = get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM prices_daily").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM index_levels").fetchone()[0] == 0
    finally:
        conn.close()


def test_session_missed_by_the_feed_gets_nse_closing_values(temp_db, monkeypatch):
    """
    2026-09-16 had no index_levels row inside market hours -- only rows
    stamped 00:00-07:00 holding 09-15's close -- so compute_mh read 09-15's
    -1.195% and scored a +0.43% day BEAR. The session's latest row must now
    be NSE's own close for it.
    """
    from db.schema import init_db, get_connection
    from data.bhavcopy import sync_nse_index_closes
    from scores.engine import get_idx
    init_db()
    conn = get_connection()
    conn.execute("INSERT INTO index_levels (date,time,nifty50,nifty50_chg) "
                 "VALUES ('2026-09-16','07:00:02',23118.6,-1.195)")
    conn.commit(); conn.close()
    _nse_closes(monkeypatch, {"nifty50": (23217.6, 0.428), "banknifty": (56000.0, 0.5)})
    sync_nse_index_closes(dt.date(2026, 9, 16))
    sync_nse_index_closes(dt.date(2026, 9, 16))        # a re-run adds nothing
    conn = get_connection()
    try:
        idx = get_idx("2026-09-16", conn)
        assert (idx["time"], idx["nifty50"], idx["nifty50_chg"]) == ("15:30:00", 23217.6, 0.428)
        assert conn.execute("SELECT COUNT(*) FROM index_levels WHERE date='2026-09-16'"
                            ).fetchone()[0] == 2
    finally:
        conn.close()


def test_feed_that_stopped_before_the_close_gets_the_close(temp_db, monkeypatch):
    """
    On 2026-09-09 the feed's last market-hours row was 12:00:13 and still held
    09-08's close at 0.00%; the real close (-0.861%) survived only in rows
    stamped that evening. Rows during the session are not the close.
    """
    from db.schema import init_db, get_connection
    from data.bhavcopy import sync_nse_index_closes
    from scores.engine import get_idx
    init_db()
    conn = get_connection()
    conn.execute("INSERT INTO index_levels (date,time,nifty50,nifty50_chg) "
                 "VALUES ('2026-09-09','12:00:13',23635.1,0.0)")
    conn.commit(); conn.close()
    _nse_closes(monkeypatch, {"nifty50": (23431.5, -0.861)})
    assert sync_nse_index_closes(dt.date(2026, 9, 9))["snapshot"] is True
    conn = get_connection()
    try:
        idx = get_idx("2026-09-09", conn)
        assert (idx["time"], idx["nifty50_chg"]) == ("15:30:00", -0.861)
    finally:
        conn.close()


def test_session_the_feed_covered_is_left_alone(temp_db, monkeypatch):
    from db.schema import init_db, get_connection
    from data.bhavcopy import sync_nse_index_closes
    init_db()
    conn = get_connection()
    conn.execute("INSERT INTO index_levels (date,time,nifty50,nifty50_chg) "
                 "VALUES ('2026-09-21','15:44:52',23414.3,0.291)")
    conn.commit(); conn.close()
    _nse_closes(monkeypatch, {"nifty50": (23414.3, 0.291)})
    assert sync_nse_index_closes(dt.date(2026, 9, 21))["snapshot"] is False
    conn = get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM index_levels").fetchone()[0] == 1
    finally:
        conn.close()


def _series(dates, closes):
    return pd.DataFrame({"date": dates, "close": closes})


def test_relative_strength_matches_dates_not_rows():
    """
    With the benchmark one session short, row 0 of the stock was today and
    row 0 of the index was yesterday: each stock's 20-session return was
    compared with the index's 20 sessions ending a day earlier.
    """
    from scores.engine import compute_relative_strength, minmax
    days = [f"2026-08-{d:02d}" for d in range(31, 6, -1)]           # 25 sessions, newest first
    stock = [118] + [110 - 0.4 * i for i in range(1, 25)]           # jumps today
    index = [1000 * (1.01 ** (i % 3)) for i in range(25)]
    assert compute_relative_strength(_series(days, stock), _series(days, index)) is not None
    lagging = compute_relative_strength(_series(days, stock), _series(days[1:], index[1:]))
    both = days[1:]
    expected = compute_relative_strength(_series(both, stock[1:]), _series(both, index[1:]))
    assert lagging == expected, "compared over the dates both series have"
    positional = minmax((stock[0] / stock[20] - 1) * 100 - (index[1] / index[21] - 1) * 100, -15, 15)
    assert lagging != positional, "row positions pair the stock's today with the index's yesterday"


def test_benchmark_rows_never_enter_the_backtest(temp_db):
    from db.schema import init_db, get_connection
    from scores.backtest import load_bars
    init_db()
    conn = get_connection()
    try:
        for sym, src in (("NIFTY50", "nse_index"), ("NIFTY50X", "dhan_index"), ("ACME", "bhavcopy")):
            conn.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) "
                         "VALUES (?, '2026-09-21', 10, 11, 9, 10, 100, ?)", (sym, src))
        assert set(load_bars(conn)) == {"ACME"}
    finally:
        conn.close()


def test_duplicate_index_rows_do_not_block_startup(temp_db, monkeypatch):
    """The unique index is skipped, not forced, on a table that still holds
    duplicates: nothing is deleted and every connection still opens."""
    import sqlite3
    from db import schema
    monkeypatch.setattr(schema, "_index_levels_unique_blocked", False)
    raw = sqlite3.connect(str(temp_db))
    raw.execute("CREATE TABLE index_levels (id INTEGER PRIMARY KEY, date DATE, time TEXT)")
    raw.executemany("INSERT INTO index_levels (date, time) VALUES (?, ?)",
                    [("2026-09-09", "20:08:32")] * 2)
    raw.commit(); raw.close()
    conn = schema.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM index_levels").fetchone()[0] == 2
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?",
                                (schema.INDEX_LEVELS_UNIQUE,)).fetchone()
    finally:
        conn.close()
    assert schema._index_levels_unique_blocked
