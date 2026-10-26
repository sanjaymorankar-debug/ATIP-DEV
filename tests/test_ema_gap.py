"""Task 2 step 4: the null-EMA gap for short-history small/mid-caps is filled from the Bhavcopy archive."""

from datetime import date, timedelta

import pandas as pd
import pytest


def _sessions(n, end):
    from utils.trading_calendar import is_trading_day
    out, d = [], end
    while len(out) < n:
        if is_trading_day(d):
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


@pytest.fixture
def db(temp_db, tmp_path, monkeypatch):
    from db.schema import init_db, get_connection
    init_db()
    monkeypatch.chdir(tmp_path)
    end = date(2026, 10, 26)
    days = _sessions(120, end)
    c = get_connection()
    for i, d in enumerate(days[-33:]):                  # SMALLCO: only 33 bars, like AETHER / KSB on 2026-10-26
        c.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) VALUES (?,?,?,?,?,?,?,?)",
                  ("SMALLCO", str(d), 100 + i, 101 + i, 99 + i, 100 + i, 1000, "bhavcopy"))
    c.execute("INSERT INTO ai_scores (symbol, date, atip_score) VALUES ('SMALLCO', ?, 50)", (str(end),))
    c.commit()
    c.close()
    return days


def _fake_bhavcopy(calls):
    def dl(d, session):
        calls.append(d)
        return pd.DataFrame({"TckrSymb": ["SMALLCO", "OTHER"], "SctySrs": ["EQ", "EQ"], "OpnPric": [90.0, 5.0],
                             "HghPric": [91.0, 6.0], "LwPric": [89.0, 4.0], "ClsPric": [90.0, 5.0],
                             "TtlTradgVol": [500, 10]})
    return dl


def test_short_history_is_filled_and_ema50_appears(db):
    from data import ema_gap
    from db.schema import get_connection
    calls = []
    out = ema_gap.run(sessions=25, download=_fake_bhavcopy(calls), session=object())
    assert out["status"] == "SUCCESS" and out["below_ema50_before"] == 1 and out["below_ema50_after"] == 0
    assert len(calls) == 25 and calls == sorted(calls, reverse=True), "newest missing session first"
    c = get_connection()
    try:
        n = c.execute("SELECT COUNT(*) FROM prices_daily WHERE symbol='SMALLCO'").fetchone()[0]
        other = c.execute("SELECT COUNT(*) FROM prices_daily WHERE symbol='OTHER'").fetchone()[0]
        src = {r[0] for r in c.execute("SELECT DISTINCT source FROM prices_daily WHERE symbol='SMALLCO'")}
        ema = c.execute("SELECT ema_50 FROM technical_indicators WHERE symbol='SMALLCO' AND date='2026-10-26'").fetchone()
    finally:
        c.close()
    assert n == 58 and other == 0, "only the short universe symbols are filled"
    assert src == {"bhavcopy", "bhavcopy_archive"}
    assert ema and ema[0] is not None, "EMA-50 is no longer null"


def test_existing_rows_are_never_overwritten_and_sessions_not_retried(db):
    from data import ema_gap
    from db.schema import get_connection
    first = db[-34]                                      # the session right before SMALLCO's history
    c = get_connection()
    c.execute("INSERT INTO prices_daily (symbol,date,close,source) VALUES ('SMALLCO',?,77.7,'dhan')", (str(first),))
    c.commit()
    c.close()
    calls = []
    ema_gap.run(sessions=5, download=_fake_bhavcopy(calls), session=object(), recompute=False)
    c = get_connection()
    try:
        assert c.execute("SELECT close, source FROM prices_daily WHERE symbol='SMALLCO' AND date=?",
                         (str(first),)).fetchone()[:] == (77.7, "dhan")
    finally:
        c.close()
    assert first not in calls
    again = []
    ema_gap.run(sessions=5, download=_fake_bhavcopy(again), session=object(), recompute=False)
    assert not set(calls) & set(again), "an attempted session is not downloaded twice"


def test_a_failed_download_is_recorded_not_fatal(db):
    from data import ema_gap

    def dead(d, s):
        raise RuntimeError("404")
    out = ema_gap.run(sessions=3, download=dead, session=object(), recompute=False)
    assert out["status"] == "FAILED" and out["sessions_failed"] == 3
