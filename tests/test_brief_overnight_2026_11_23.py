"""
BR-01 (23 Nov 2026): the Monday morning brief showed Friday's market data.

send_morning_digest read global_markets, index_levels and market_health "as of the last scored date" (Friday). On
Mon 23 Nov it printed S&P -2.02 % (Thursday's US move) while Friday night's S&P -3.70 % / Nasdaq -5.28 % had been
stored at 06:00 IST, and Nifty then fell 1.75 %. The scores stay Friday's; the market lines now read the newest row up
to today, say which close an older row is from, and a big US move after the scores is said at the top.
"""

import datetime as dt

import pytest

MON = dt.datetime(2026, 11, 23, 8, 30)
FRI = "2026-11-20"
THU = "2026-11-19"


@pytest.fixture
def conn(temp_db):
    from db.schema import get_connection, init_db
    init_db()
    c = get_connection()
    c.execute("INSERT INTO ai_scores (symbol,date,atip_score,`signal`) VALUES ('ACME',?,60,'HOLD')", (FRI,))
    c.execute("INSERT INTO market_health (date,mh_score,regime) VALUES (?,?,?)", (FRI, 53.47, "NEUTRAL"))
    c.execute("INSERT INTO index_levels (date,time,nifty50_chg,india_vix) VALUES (?,?,?,?)", (FRI, "15:44:47", 1.12, 17.32))
    # Friday morning's fetch carries Thursday's US session; Monday morning's carries Friday's
    c.execute("INSERT INTO global_markets (date,time,sp500_chg,nasdaq_chg,dow_chg,global_score,global_sentiment) "
              "VALUES (?,?,?,?,?,?,?)", (FRI, "premarket", -2.02, -2.25, -2.19, 2.9, "BEARISH"))
    c.execute("INSERT INTO global_markets (date,time,sp500_chg,nasdaq_chg,dow_chg,global_score,global_sentiment) "
              "VALUES (?,?,?,?,?,?,?)", ("2026-11-23", "overnight", -3.70, -5.28, -1.93, 30.8, "BEARISH"))
    c.execute("INSERT INTO global_markets (date,time,sp500_chg,nasdaq_chg,dow_chg,global_score,global_sentiment) "
              "VALUES (?,?,?,?,?,?,?)", ("2026-11-23", "premarket", -3.70, -5.28, -1.93, 34.7, "BEARISH"))
    for d, v in ((THU, 7175.83), (FRI, 6910.39)):
        c.execute("INSERT INTO global_market_history (series,date,close) VALUES ('sp500',?,?)", (d, v))
    c.commit()
    yield c
    c.close()


def _brief(conn, now=MON):
    from alerts.telegram import send_morning_digest
    assert send_morning_digest(now=now) is True
    return conn.execute("SELECT message FROM alert_log WHERE category='digest' ORDER BY id DESC").fetchone()[0]


def test_monday_brief_shows_friday_nights_us_move_not_thursdays(conn):
    body = _brief(conn)
    assert "<b>Global:</b> score 35 (BEARISH), S&amp;P -3.70%, Nasdaq -5.28%, Dow -1.93% (US close 20 Nov)" in body
    assert "-2.02%" not in body


def test_a_big_us_move_after_the_scores_is_said_at_the_top(conn):
    body = _brief(conn)
    assert "Overnight: the US fell after these 2026-11-20 scores" in body
    assert "S&amp;P -3.70%, Nasdaq -5.28% (US close 20 Nov)" in body
    assert body.index("Overnight:") < body.index("<b>Market Health:</b>")


def test_older_rows_say_which_close_they_are_from(conn):
    body = _brief(conn)
    assert "<b>Market Health:</b> 53 (NEUTRAL) (20 Nov close)" in body
    assert "<b>Last session (20 Nov close):</b> Nifty +1.12% · VIX 17.3" in body
    assert "<b>Open:</b>" not in body


def test_todays_index_row_is_the_open_line(conn):
    conn.execute("INSERT INTO index_levels (date,time,nifty50_chg,india_vix) VALUES (?,?,?,?)",
                 ("2026-11-23", "09:16:00", -0.4, 18.5))
    conn.commit()
    body = _brief(conn, now=dt.datetime(2026, 11, 23, 9, 16, 30))
    assert "<b>Open:</b> Nifty -0.40% · VIX 18.5" in body
    assert "Last session" not in body


def test_a_quiet_night_adds_no_warning(conn):
    conn.execute("UPDATE global_markets SET sp500_chg=0.4, nasdaq_chg=-0.9 WHERE date='2026-11-23'")
    conn.commit()
    body = _brief(conn)
    assert "Overnight:" not in body
    assert "S&amp;P +0.40%, Nasdaq -0.90%" in body


def test_a_big_rise_is_said_too(conn):
    conn.execute("UPDATE global_markets SET sp500_chg=1.1, nasdaq_chg=2.4 WHERE date='2026-11-23'")
    conn.commit()
    assert "Overnight: the US rose after these 2026-11-20 scores" in _brief(conn)


def test_no_fetch_today_falls_back_and_says_so(conn):
    conn.execute("DELETE FROM global_markets WHERE date='2026-11-23'")
    conn.execute("DELETE FROM global_market_history WHERE date='2026-11-20'")
    conn.commit()
    body = _brief(conn)
    assert "S&amp;P -2.02%" in body and "fetched 2026-11-20, none today" in body
    assert "(US close 19 Nov)" in body
    # Thursday's US session closed before Friday's scores were made: no "after these scores" warning
    assert "Overnight:" not in body


def test_overnight_line_unit():
    from alerts.telegram import _overnight_line
    row = {"sp500_chg": -0.2, "nasdaq_chg": -1.6}
    assert "fell" in _overnight_line(row, "2026-11-20", "2026-11-20")
    assert _overnight_line({"sp500_chg": 1.49, "nasdaq_chg": None}, None, "2026-11-20") is None
    assert _overnight_line(None, None, "2026-11-20") is None
