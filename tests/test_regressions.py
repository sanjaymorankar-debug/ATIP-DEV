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
