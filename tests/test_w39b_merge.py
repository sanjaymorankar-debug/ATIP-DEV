"""Fixes made while merging W39 (PR #4) into W39b (PR #5)."""

from datetime import date, timedelta

import pandas as pd


def test_backfill_tests_corporate_actions_as_of_today_not_the_old_window(temp_db, monkeypatch):
    """W39's backfill stored each old window with the window's END as the basis date, so the
    'recent unreconciled ex-date' rule looked at dates years ago: an old bar of a stock with a
    pending (unreconciled) split last week was written as Dhan sent it, on a basis that does
    not match the stored history. W39b's fix (basis as of the fetch day) is ported."""
    from data import dhan as D
    from data import history_backfill as H
    from db.schema import get_connection, init_db
    init_db()
    conn = get_connection()
    try:
        win_end = date.today() - timedelta(days=3 * 365)
        df = pd.DataFrame([{"date": win_end - timedelta(days=1), "open": 10, "high": 11, "low": 9, "close": 10,
                            "volume": 100}])
        recent_pending = [(str(date.today() - timedelta(days=5)), "pending", 0.5, True)]
        n, held, _ = D.store_daily_bars(conn, "ACME", df, win_end - timedelta(days=30), win_end, recent_pending)
        assert (n, held) == (1, 0), "the old default: tested against the window's own end"
        conn.execute("DELETE FROM prices_daily")
        n, held, _ = D.store_daily_bars(conn, "ACME", df, win_end - timedelta(days=30), win_end, recent_pending,
                                        basis_date=date.today())
        assert (n, held) == (0, 1), "as of today the bar waits for the split to be reconciled"
        seen = {}

        def fake_store(conn_, sym, df_, start, end, events=None, basis_date=None):
            seen["basis"] = basis_date
            return 0, 0, 0
        monkeypatch.setattr(D, "store_daily_bars", fake_store)
        monkeypatch.setattr(D, "fetch_historical_daily", lambda *a, **k: df)
        H.backfill_symbol(conn, object(), "ACME", win_end - timedelta(days=400), 365, recent_pending)
        assert seen["basis"] == date.today()
    finally:
        conn.close()


def test_both_w39_schema_modules_are_applied(temp_db):
    from db.schema import get_connection, init_db
    from db.schema_w39 import W39_TABLES
    from db.schema_w39b import W39B_TABLES
    assert not set(W39_TABLES) & set(W39B_TABLES)
    init_db()
    conn = get_connection()
    try:
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert set(W39_TABLES) <= have and set(W39B_TABLES) <= have
        cols = {r[1] for r in conn.execute("PRAGMA table_info(perf_ledger)")}
        assert {"entry_seq", "fee_breakdown"} <= cols
    finally:
        conn.close()
