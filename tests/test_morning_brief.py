"""
P1-3: the morning brief.

On 2026-10-26 ATIP started at 18:47 and the auto-recovery (pipeline/recover.py) sent the "morning" brief
at 18:49: an automatic brief now goes out only on a trading day between its 08:30 slot and the 09:15
open. The brief also gains the crude, USD-INR, FII/DII and news-tone lines it lacked (report 02, item 3).
"""

import datetime as dt
import json

import pytest

MON = dt.date(2026, 10, 26)


def at(h, m, d=MON):
    return dt.datetime.combine(d, dt.time(h, m))


def test_the_brief_window():
    from alerts.telegram import morning_brief_window
    assert morning_brief_window(at(8, 30)) == (True, "") and morning_brief_window(at(9, 14))[0]
    ok, why = morning_brief_window(at(18, 49))
    assert not ok and "09:15" in why
    assert not morning_brief_window(at(9, 15))[0]
    assert "08:30 run" in morning_brief_window(at(7, 10))[1]
    assert "not a trading day" in morning_brief_window(at(8, 45, dt.date(2026, 10, 20)))[1]    # Dussehra


@pytest.fixture
def conn(temp_db):
    from db.schema import get_connection, init_db
    init_db()
    c = get_connection()
    c.execute("INSERT INTO pipeline_log (run_date,job_name,start_time,end_time,status,rows_processed,kind) "
              "VALUES ('2026-10-01','ops_monitor',?,?,'SUCCESS',1,'run')",
              (dt.datetime(2026, 10, 1, 7, 0), dt.datetime(2026, 10, 1, 7, 0)))      # monitoring began
    c.commit()
    yield c
    c.close()


def test_the_auto_recovery_never_sends_the_brief_after_the_open(conn, monkeypatch):
    import pipeline.recover as R
    from pipeline.health import Problem
    missed = [Problem("Morning brief", "MISSED", "no run on 2026-10-26 by 08:45", ("morning_digest",))]
    (st,) = R.plan(conn, missed, at(18, 49))
    assert not st["allowed"] and st["reason"].startswith("not sent: the market opened at 09:15")
    assert R.plan(conn, missed, at(8, 50))[0]["allowed"]
    monkeypatch.setattr("alerts.telegram.send_morning_digest", lambda *a, **k: pytest.fail("brief sent"))
    assert R._run_step("brief", at(9, 16))["status"] == "SKIPPED", "planned at 09:14, its turn came at 09:16"
    # the whole check at 18:49, as on 26 Oct: the other misses are re-run, the brief is not
    ran = []
    monkeypatch.setattr(R, "_run_step", lambda step, now: ran.append(step) or {"status": "SUCCESS"})
    out = R.check_and_recover(at(18, 49))
    assert "brief" not in ran and ran, ran
    assert [s["step"] for s in out["skipped"]] == ["brief"]
    assert "Morning brief" in {p.label for p in out["problems"]}, "still reported as missed"


def test_a_late_scheduled_brief_is_not_sent(temp_db, monkeypatch):
    from db.schema import init_db
    from pipeline import scheduler as S
    init_db()
    sent = []
    monkeypatch.setattr(S, "is_market_day", lambda: True)
    monkeypatch.setattr("alerts.telegram.send_morning_digest", lambda *a, **k: sent.append(1) or True)
    S.run_morning_digest(now=at(10, 5))                   # the Mac woke at 10:05: schedule runs the 08:30 slot
    assert sent == []
    S.run_morning_digest(now=at(8, 31))
    assert sent == [1]


def _seed_scores(conn, d):
    conn.execute("INSERT INTO ai_scores (symbol,date,atip_score,`signal`) VALUES ('ACME',?,60,'HOLD')", (str(d),))
    conn.commit()


def _brief(conn):
    from alerts.telegram import send_morning_digest
    assert send_morning_digest() is True
    return conn.execute("SELECT message FROM alert_log WHERE category='digest' ORDER BY id DESC").fetchone()[0]


def test_the_brief_has_crude_usd_inr_fii_dii_and_the_news_tone(conn):
    td = dt.date.today() - dt.timedelta(days=1)
    _seed_scores(conn, td)
    conn.execute("INSERT INTO global_markets (date,time,crude_brent,crude_brent_chg,crude_wti,crude_wti_chg,usd_inr,"
                 "usd_inr_chg) VALUES (?,?,?,?,?,?,?,?)", (str(dt.date.today()), "premarket", 64.25, 1.2, 60.1, 1.05,
                                                          88.21, -0.08))
    conn.execute("INSERT INTO fii_dii_market (date,fii_net_cr,dii_net_cr) VALUES (?,?,?)", (str(td), -1234.4, 2345.6))
    conn.execute("INSERT INTO news_summary (created_at,window_hours,article_count,classifier,model,summary_json) "
                 "VALUES (?,?,?,?,?,?)", (dt.datetime.now() - dt.timedelta(hours=1), 14, 231, "rule", None,
                                          json.dumps({"headline": "231 headlines", "tone": "RISK_OFF"})))
    conn.commit()
    body = _brief(conn)
    assert "<b>Crude:</b> Brent $64.25 (+1.20%) · WTI $60.10 (+1.05%)" in body
    assert "<b>USD-INR:</b> 88.21 (-0.08%)" in body
    assert f"<b>FII/DII ({td:%d %b}):</b> FII ₹-1,234 Cr · DII ₹+2,346 Cr" in body
    assert "<b>News tone:</b> RISK-OFF · 231 headlines (keyword rules, not AI" in body


def test_the_brief_says_when_those_inputs_are_missing(conn):
    td = dt.date.today() - dt.timedelta(days=1)
    _seed_scores(conn, td)
    conn.execute("INSERT INTO fii_dii_market (date,fii_net_cr,dii_net_cr) VALUES (?,?,?)",
                 (str(td - dt.timedelta(days=3)), 500.0, None))
    conn.commit()
    body = _brief(conn)
    assert "<b>Crude:</b> — (no global markets fetch stored)" in body
    assert "<b>USD-INR:</b> — (no global markets fetch stored)" in body
    assert f"FII ₹+500 Cr — not yet published for {td}" in body
    assert "<b>News tone:</b> — (no news summary in the last 24 h)" in body
