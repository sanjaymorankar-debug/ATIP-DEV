"""
W39 (DP-11): seven years of daily history.

The weekly purge used to delete prices_daily (and the signals whose hit rate is tracked)
after 600 days with no archive; the backfill walks each symbol back to 7 years. No test
reaches Dhan: fetch_historical_daily is stubbed.
"""
import datetime as dt

import pandas as pd
import pytest


def _db(temp_db):
    from db.schema import init_db
    init_db()


def _bar(conn, sym, d, close=100.0, table="prices_daily"):
    if table == "prices_daily":
        conn.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) "
                     "VALUES (?,?,?,?,?,?,?,'dhan')", (sym, str(d), close, close, close, close, 1000))


def _fake_dhan(listing: dt.date, calls: list, refuse: str | None = None):
    """A Dhan that has weekday bars from `listing` onwards, recording each window asked for."""
    def fetch(symbol, start, end, dhan=None):
        calls.append((symbol, start, end))
        if refuse:
            bad = pd.DataFrame()
            bad.attrs["dhan_error"] = refuse
            return bad
        days = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
        days = [d for d in days if d >= listing and d.weekday() < 5]
        if not days:
            return pd.DataFrame()
        return pd.DataFrame({"date": days, "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
                             "volume": 500, "symbol": symbol, "source": "dhan"})
    return fetch


@pytest.fixture
def hb(temp_db, monkeypatch):
    from data import dhan as D
    from data import history_backfill as H
    _db(temp_db)
    monkeypatch.setattr(D, "HAS_DHAN", True)
    monkeypatch.setattr(D, "get_dhan_client", lambda: (object(), None))
    monkeypatch.setattr(H, "settings", lambda: {**H.DEFAULTS, "pause_seconds": 0.0})
    return H


# ── retention ─────────────────────────────────────────────────────────────

def test_purge_keeps_seven_years_of_prices_but_trims_derived_tables(temp_db, monkeypatch):
    from db import purge
    from db.schema import get_connection
    _db(temp_db)
    monkeypatch.setattr(purge, "retention_settings",
                        lambda: {"history_days": 2557, "long_days": 600, "short_days": 90})
    today = dt.date.today()
    conn = get_connection()
    for days_ago in (700, 2000, 2600):
        _bar(conn, "ACME", today - dt.timedelta(days=days_ago))
        conn.execute("INSERT INTO technical_indicators (symbol, date) VALUES (?,?)",
                     ("ACME", str(today - dt.timedelta(days=days_ago))))
    conn.commit(); conn.close()

    res = purge.purge_old_data(dry_run=False)
    conn = get_connection()
    try:
        kept = sorted(str(r[0]) for r in conn.execute("SELECT date FROM prices_daily WHERE symbol='ACME'"))
        assert kept == sorted(str(today - dt.timedelta(days=d)) for d in (700, 2000)), "only >7y goes"
        assert conn.execute("SELECT COUNT(*) FROM technical_indicators").fetchone()[0] == 0, "derived: 600 d"
    finally:
        conn.close()
    assert res["prices_daily"] == 1 and res["technical_indicators"] == 3


def test_history_days_zero_keeps_history_forever(temp_db, monkeypatch):
    from db import purge
    from db.schema import get_connection
    _db(temp_db)
    monkeypatch.setattr(purge, "retention_settings",
                        lambda: {"history_days": None, "long_days": 600, "short_days": 90})
    conn = get_connection()
    _bar(conn, "ACME", dt.date.today() - dt.timedelta(days=4000))
    conn.commit(); conn.close()
    res = purge.purge_old_data(dry_run=False)
    assert "prices_daily" not in res
    conn = get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM prices_daily").fetchone()[0] == 1
    finally:
        conn.close()


def test_retention_settings_read_config_and_never_undercut_long(monkeypatch):
    from db import purge
    import ops.config as C
    monkeypatch.setattr(C, "load", lambda: {"retention": {"history_days": 100, "long_days": 600}})
    assert purge.retention_settings()["history_days"] == 600
    monkeypatch.setattr(C, "load", lambda: {"retention": {"history_days": 0}})
    assert purge.retention_settings()["history_days"] is None
    monkeypatch.setattr(C, "load", lambda: {})
    assert purge.retention_settings() == {"history_days": 2557, "long_days": 600, "short_days": 90}


# ── backfill ──────────────────────────────────────────────────────────────

def test_backfill_walks_back_to_the_listing_and_stops(hb, monkeypatch):
    from data import dhan as D
    from db.schema import get_connection
    today = dt.date.today()
    listing = today - dt.timedelta(days=1000)
    conn = get_connection()
    _bar(conn, "ACME", today - dt.timedelta(days=30))
    conn.commit(); conn.close()
    calls = []
    monkeypatch.setattr(D, "fetch_historical_daily", _fake_dhan(listing, calls))

    out = hb.run_backfill(symbols=["ACME"])
    assert out["status"] == "SUCCESS" and out["symbols"] == 1 and out["rows"] > 600
    r = out["results"][0]
    assert r["exhausted"] and r["error"] is None
    assert all((end - start).days < 365 for _, start, end in calls), "one year per request"
    assert calls[0][2] == today - dt.timedelta(days=31), "starts just before the earliest stored bar"
    conn = get_connection()
    try:
        first = conn.execute("SELECT MIN(date) FROM prices_daily WHERE symbol='ACME'").fetchone()[0]
        assert str(first) >= str(listing) and str(first) <= str(listing + dt.timedelta(days=3))
        st = hb._state(conn)["ACME"]
        assert st["exhausted"] and st["failures"] == 0 and st["rows_added"] == out["rows"]
    finally:
        conn.close()

    calls.clear()
    monkeypatch.setattr(D, "get_tracked_symbols", lambda conn=None: ["ACME"])
    again = hb.run_backfill()                       # nightly run: nothing left to ask Dhan
    assert again["status"] == "SKIPPED" and calls == []


def test_a_symbol_with_seven_years_is_left_alone(hb, monkeypatch):
    from data import dhan as D
    from db.schema import get_connection
    target = hb.target_start(7)
    conn = get_connection()
    _bar(conn, "OLD", target + dt.timedelta(days=2))
    conn.commit()
    try:
        assert not hb.needs_backfill(hb._earliest(conn, "OLD"), None, target)
        assert hb.needs_backfill(None, None, target)
    finally:
        conn.close()
    calls = []
    monkeypatch.setattr(D, "fetch_historical_daily", _fake_dhan(target, calls))
    assert hb.run_backfill(symbols=["OLD"])["status"] == "SKIPPED" and calls == []


def test_refusals_are_not_mistaken_for_the_listing_and_park_after_three(hb, monkeypatch):
    from data import dhan as D
    from db.schema import get_connection
    calls = []
    monkeypatch.setattr(D, "fetch_historical_daily", _fake_dhan(dt.date(2000, 1, 1), calls, refuse="DH-904"))
    monkeypatch.setattr(D, "get_tracked_symbols", lambda conn=None: ["ACME"])
    for n in range(1, 4):
        out = hb.run_backfill()
        assert out["status"] == "FAILED" and out["results"][0]["error"] == "DH-904"
        conn = get_connection()
        try:
            st = hb._state(conn)["ACME"]
        finally:
            conn.close()
        assert not st["exhausted"] and st["failures"] == n
    calls.clear()
    assert hb.run_backfill()["status"] == "SKIPPED" and calls == [], "parked after three refusals"
    conn = get_connection()
    try:
        cov = hb.coverage(conn, symbols=["ACME"])
    finally:
        conn.close()
    assert cov["parked"] == 1 and cov["parked_errors"] == {"ACME": "DH-904"}
    hb.run_backfill(symbols=["ACME"])               # an explicit retry still asks
    assert calls


def test_an_exception_in_the_fetch_is_tagged_as_an_error(monkeypatch):
    from data import dhan as D
    monkeypatch.setattr(D, "get_security_id", lambda s: {"security_id": "1", "exchange": "NSE_EQ"})
    monkeypatch.setattr(D, "_cached_daily", lambda f, d: None)

    class Boom:
        def historical_daily_data(self, **k):
            raise ConnectionError("down")
    monkeypatch.setattr(D, "_guarded", lambda fn, *a, **k: fn(*a, **k))
    df = D.fetch_historical_daily("ACME", dt.date(2020, 1, 1), dt.date(2020, 12, 31), Boom())
    assert df.empty and df.attrs["dhan_error"] == "exception:ConnectionError"


def test_backfill_and_reports_are_scheduled_nightly(monkeypatch):
    """Without the `schedule` package (CI leaves it out): a recorder stands in for it."""
    from pipeline import scheduler as S

    class _Job:
        def __init__(self, jobs):
            self.jobs, self.t = jobs, None

        @property
        def day(self):
            return self

        @property
        def minutes(self):
            return self

        def at(self, t):
            self.t = t
            return self

        def do(self, fn, *a, **k):
            self.jobs.append((self.t, fn))
            return self

    class _Schedule:
        def __init__(self):
            self.jobs = []

        def every(self, *a):
            return _Job(self.jobs)

    fake = _Schedule()
    monkeypatch.setattr(S, "schedule", fake, raising=False)
    S._schedule_w39_jobs()
    assert fake.jobs == [(None, S._w39_order_pressure_tick), ("08:45", S._w39_gift), ("09:05", S._w39_gift),
                         ("09:35", S._w39_gap_eval), ("20:15", S._w39_participant_oi),
                         ("23:20", S._w39_nifty_history), ("20:30", S._w39_technical_signals),
                         ("20:35", S._w39_earnings_surprise),
                         ("20:40", S._w39_research_reports), ("20:50", S._w39_saved_screens),
                         ("22:20", S._w39_history_backfill), (None, S._w39_intraday_tick),
                         ("15:31", S._w39_global_sync), ("08:42", S._w39_global_sync)]
