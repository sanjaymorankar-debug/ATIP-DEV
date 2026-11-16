"""
Dhan's daily-history toDate is EXCLUSIVE (16 Nov 2026).

The W39 backfill asked for each 365-day window as (start, end), so every window that ended on a session lost that
session's bar, and the next (older) window ends the day before: on the 15 Nov 2026 backup, 1,499 symbol-days in 331
symbols (3 Mar 2020-2023, the Saturday session of 2 Mar 2024). The existing W39 tests stubbed fetch_historical_daily
with an inclusive fake, so they could not see it. Here a fake Dhan CLIENT answers the way Dhan does -- bars from
fromDate up to the day before toDate, stamped at IST midnight -- and the real fetch, backfill and repair tool run
against it. Nothing reaches Dhan.
"""
import datetime as dt
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

IST = ZoneInfo("Asia/Kolkata")
HOLIDAYS = {dt.date(2023, 3, 7), dt.date(2025, 3, 14)}     # Holi 2023, Holi 2025
SPECIAL = {dt.date(2024, 3, 2)}                             # the Saturday session of 2 Mar 2024


def _is_session(d):
    return d in SPECIAL or (d.weekday() < 5 and d not in HOLIDAYS)


class FakeDhan:
    """historical_daily_data as Dhan answers it: toDate excluded. inclusive=True plays a Dhan that includes it."""

    def __init__(self, listing=dt.date(2000, 1, 1), inclusive=False):
        self.listing, self.inclusive, self.calls = listing, inclusive, []

    def historical_daily_data(self, security_id, exchange_segment, instrument_type, from_date, to_date):
        frm, to = dt.date.fromisoformat(from_date), dt.date.fromisoformat(to_date)
        self.calls.append((frm, to))
        last = to if self.inclusive else to - dt.timedelta(days=1)
        days = [frm + dt.timedelta(days=i) for i in range((last - frm).days + 1)]
        days = [d for d in days if d >= self.listing and _is_session(d)]
        if not days:
            return {"status": "success", "data": {}}
        n = len(days)
        return {"status": "success", "data": {
            "timestamp": [int(dt.datetime(d.year, d.month, d.day, tzinfo=IST).timestamp()) for d in days],
            "open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n, "close": [100.5] * n, "volume": [1000] * n}}


@pytest.fixture
def dhan(temp_db, tmp_path, monkeypatch):
    from data import dhan as D
    from db.schema import init_db
    init_db()
    cache = tmp_path / "raw_dhan"
    cache.mkdir()
    monkeypatch.setattr(D, "CACHE_DIR", cache)
    monkeypatch.setattr(D, "HAS_DHAN", True)
    monkeypatch.setattr(D, "get_security_id", lambda s: {"security_id": "1", "exchange": "NSE_EQ"})
    monkeypatch.setattr(D, "_guarded", lambda fn, *a, **k: fn(*a, **k))
    return D


def _bars(conn, sym, days):
    for d in days:
        conn.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) "
                     "VALUES (?,?,100,101,99,100.5,1000,'dhan')", (sym, str(d)))


def _sessions(a, b):
    return [a + dt.timedelta(days=i) for i in range((b - a).days + 1) if _is_session(a + dt.timedelta(days=i))]


def _stored(conn, sym):
    return [dt.date.fromisoformat(str(r[0])[:10])
            for r in conn.execute("SELECT date FROM prices_daily WHERE symbol=? ORDER BY date", (sym,))]


# ── the fetch ─────────────────────────────────────────────────────────────

def test_dhan_leaves_out_the_end_day_and_fetch_daily_through_asks_for_the_next_day(dhan):
    fake = FakeDhan()
    fri, mon = dt.date(2023, 3, 3), dt.date(2023, 3, 6)
    old = dhan.fetch_historical_daily("ACME", dt.date(2023, 2, 27), fri, fake)
    assert list(pd.to_datetime(old["date"]).dt.date) == _sessions(dt.date(2023, 2, 27), dt.date(2023, 3, 2)), \
        "the old call: Friday's bar never comes back"
    df = dhan.fetch_daily_through("ACME", dt.date(2023, 2, 27), fri, fake)
    assert fake.calls[-1] == (dt.date(2023, 2, 27), fri + dt.timedelta(days=1))
    assert list(pd.to_datetime(df["date"]).dt.date) == _sessions(dt.date(2023, 2, 27), fri)

    inclusive = FakeDhan(inclusive=True)        # were Dhan ever to include toDate, the day after is still dropped
    df = dhan.fetch_daily_through("ACME", fri, mon - dt.timedelta(days=1), inclusive)
    assert list(pd.to_datetime(df["date"]).dt.date) == [fri]


def test_a_refusal_passes_through_fetch_daily_through(dhan):
    class Refuse:
        def historical_daily_data(self, **k):
            return {"status": "failure", "remarks": {"error_code": "DH-904"}}
    df = dhan.fetch_daily_through("ACME", dt.date(2023, 1, 2), dt.date(2023, 3, 3), Refuse())
    assert df.empty and df.attrs["dhan_error"] == "DH-904"


# ── the backfill ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("inclusive", [False, True])
def test_backfill_windows_keep_their_last_session_and_store_each_day_once(dhan, inclusive):
    from data import history_backfill as H
    from db.schema import get_connection
    earliest, target = dt.date(2025, 3, 3), dt.date(2022, 3, 1)
    conn = get_connection()
    try:
        H.ensure_tables(conn)
        _bars(conn, "ACME", [earliest])
        conn.commit()
        fake = FakeDhan(inclusive=inclusive)
        r = H.backfill_symbol(conn, fake, "ACME", target, 365)
        ends = [to - dt.timedelta(days=1) for _, to in fake.calls]
        assert ends == [dt.date(2025, 3, 2), dt.date(2024, 3, 2), dt.date(2023, 3, 3), dt.date(2022, 3, 3)], \
            "the live backfill's window ends"
        assert [to for _, to in fake.calls] == [earliest, dt.date(2024, 3, 3), dt.date(2023, 3, 4),
                                                dt.date(2022, 3, 4)], "each asked up to the next window's first day"
        edges = {dt.date(2024, 3, 2), dt.date(2023, 3, 3), dt.date(2022, 3, 3)}
        stored = _stored(conn, "ACME")
        assert edges <= set(stored), "the Saturday session and the Fridays at the window edges are stored"
        assert stored == _sessions(target, earliest), "no session missing anywhere"
        assert r["rows"] == len(stored) - 1 and r["error"] is None, "rows_added counts each day once"
        assert H._state(conn)["ACME"]["rows_added"] == len(stored) - 1
    finally:
        conn.close()


# ── the repair tool ───────────────────────────────────────────────────────

@pytest.fixture
def gaps(dhan, tmp_path):
    """ACME, BETA, GAMMA, DELTA as the old backfill left them, and Dhan's cache of the windows it asked for."""
    from db.schema import get_connection
    conn = get_connection()
    try:
        march23 = _sessions(dt.date(2023, 2, 20), dt.date(2023, 3, 31))
        _bars(conn, "ACME", [d for d in march23 if d != dt.date(2023, 3, 3)])
        _bars(conn, "BETA", march23 + [dt.date(2024, 3, 2)])
        _bars(conn, "GAMMA", [d for d in _sessions(dt.date(2024, 2, 26), dt.date(2024, 3, 8)) if d.weekday() < 5])
        _bars(conn, "DELTA", [d for d in _sessions(dt.date(2025, 3, 10), dt.date(2025, 3, 21))
                              if d != dt.date(2025, 3, 13)])
        conn.commit()
    finally:
        conn.close()
    for name in ("ACME_2022-03-04_2023-03-03",     # Friday, no bar, BETA has one: a session
                 "ACME_2023-03-04_2023-03-07",     # Holi 2023: nobody has a bar, before the calendar: unconfirmed
                 "ACME_2023-01-01_2023-02-18",     # before ACME's history
                 "ACME_2023-03-05_2023-03-11",     # a Saturday nobody traded
                 "ACME_2023-03-01_2023-03-02",     # has its bar
                 "GAMMA_2023-03-03_2024-03-02",    # the Saturday session: BETA has the bar
                 "DELTA_2025-03-01_2025-03-14",    # Holi 2025, in NSE_HOLIDAYS
                 "DELTA_2025-03-01_2025-03-13"):   # a session by the calendar
        (dhan.CACHE_DIR / f"{name}_daily.json").write_text("{}")
    (dhan.CACHE_DIR / "security_id_list.csv").write_text("")
    return dhan.CACHE_DIR


def test_plan_lists_the_window_ends_missing_inside_the_history(gaps):
    from db.schema import get_connection
    from tools.dhan_boundary_days import plan
    conn = get_connection()
    try:
        todo = plan(conn, gaps)
        assert todo == [{"symbol": "ACME", "date": "2023-03-03", "confirmed": True},
                        {"symbol": "ACME", "date": "2023-03-07", "confirmed": False},
                        {"symbol": "DELTA", "date": "2025-03-13", "confirmed": True},
                        {"symbol": "GAMMA", "date": "2024-03-02", "confirmed": True}]
        assert plan(conn, gaps, ["gamma"]) == todo[3:]
    finally:
        conn.close()


def test_apply_fills_the_sessions_and_a_second_run_finds_only_the_holiday(gaps, monkeypatch, capsys):
    from data import dhan as D
    from db.schema import get_connection
    from tools import dhan_boundary_days as T
    fake = FakeDhan()
    monkeypatch.setattr(D, "get_dhan_client", lambda: (fake, None))
    assert T.main(["--cache-dir", str(gaps)]) == 0
    out = capsys.readouterr().out
    assert "4 symbol-day(s) missing at a window edge in 3 symbol(s) (1 unconfirmed weekday(s)) -- dry run" in out
    assert "2024-03-02 Sat    1 session  GAMMA" in out and fake.calls == []
    conn = get_connection()
    try:
        assert "2023-03-03" not in {str(d) for d in _stored(conn, "ACME")}, "a dry run writes nothing"
    finally:
        conn.close()

    assert T.main(["--cache-dir", str(gaps), "--apply"]) == 0
    out = capsys.readouterr().out
    assert "filled 3, held for corporate actions 0, no bar from Dhan 1, refused 0" in out
    assert "no bar: ACME 2023-03-07" in out
    assert sorted(fake.calls) == sorted((d, d + dt.timedelta(days=1)) for d in
                                        (dt.date(2023, 3, 3), dt.date(2023, 3, 7), dt.date(2024, 3, 2),
                                         dt.date(2025, 3, 13))), "one day per request, read-only history calls"
    conn = get_connection()
    try:
        assert _stored(conn, "ACME") == _sessions(dt.date(2023, 2, 20), dt.date(2023, 3, 31))
        assert dt.date(2024, 3, 2) in _stored(conn, "GAMMA") and dt.date(2025, 3, 13) in _stored(conn, "DELTA")
        assert conn.execute("SELECT COUNT(*) FROM prices_daily WHERE date='2023-03-03' AND symbol='ACME' "
                            "AND source='dhan'").fetchone()[0] == 1
        assert T.plan(conn, gaps) == [{"symbol": "ACME", "date": "2023-03-07", "confirmed": False}], \
            "the filled days are gone; Holi stays unconfirmed (Dhan has no bar for it)"
    finally:
        conn.close()


def test_a_dry_run_on_a_copy_opens_it_immutable_and_apply_writes_only_the_copy(gaps, tmp_path, monkeypatch, capsys):
    import shutil
    from data import dhan as D
    from db import schema
    from tools import dhan_boundary_days as T
    copy = tmp_path / "copy.db"
    shutil.copy(schema.DB_PATH, copy)
    before = copy.read_bytes()
    monkeypatch.setattr(D, "get_dhan_client", lambda: (FakeDhan(), None))
    assert T.main(["--db", str(copy), "--cache-dir", str(gaps)]) == 0
    assert copy.read_bytes() == before, "the dry run never writes"
    assert T.main(["--db", str(copy), "--cache-dir", str(gaps), "--apply"]) == 0
    assert "filled 3" in capsys.readouterr().out
    import sqlite3
    c = sqlite3.connect(str(copy))
    try:
        assert c.execute("SELECT COUNT(*) FROM prices_daily WHERE symbol='ACME' AND date='2023-03-03'"
                         ).fetchone()[0] == 1
    finally:
        c.close()
    from db.schema import get_connection
    conn = get_connection()
    try:
        assert "2023-03-03" not in {str(d) for d in _stored(conn, "ACME")}, "the database the process uses is untouched"
    finally:
        conn.close()
