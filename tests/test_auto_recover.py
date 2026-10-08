"""Automatic recovery of missed / failed jobs (pipeline/recover.py) and the health rules it relies on."""

import datetime as dt

import pytest


def _run(conn, name, status, start, rows=5):
    conn.execute("INSERT INTO pipeline_log (run_date,job_name,start_time,end_time,status,rows_processed,kind) "
                 "VALUES (?,?,?,?,?,?,'run')", (str(start.date()), name, start, start, status, rows))
    conn.commit()


@pytest.fixture
def conn(temp_db):
    from db.schema import init_db, get_connection
    init_db()
    c = get_connection()
    _run(c, "ops_monitor", "SUCCESS", dt.datetime(2026, 9, 29, 7, 0))          # monitoring began
    yield c
    c.close()


def _labels(conn, now):
    from pipeline.health import check_job_health
    return {p.label: p for p in check_job_health(conn, now)}


def test_a_miss_is_cleared_by_a_later_recovery_run(conn):
    now = dt.datetime(2026, 10, 1, 13, 0)
    probs = _labels(conn, now)
    assert probs["Global markets (pre-market)"].kind == "MISSED"
    assert probs["News (morning)"].kind == "MISSED" and probs["News (midday)"].kind == "MISSED"
    _run(conn, "global_recover", "SUCCESS", dt.datetime(2026, 10, 1, 12, 50))
    _run(conn, "news_recover", "SUCCESS", dt.datetime(2026, 10, 1, 12, 51))
    probs = _labels(conn, now)
    assert "Global markets (pre-market)" not in probs
    assert "News (morning)" not in probs and "News (midday)" not in probs
    # the next day (a holiday): yesterday's misses re-run today also count
    later = dt.datetime(2026, 10, 2, 10, 0)
    assert "Morning brief" in _labels(conn, later)
    _run(conn, "morning_digest", "SUCCESS", dt.datetime(2026, 10, 2, 9, 59))
    assert "Morning brief" not in _labels(conn, later)


def test_a_morning_catchup_does_not_count_as_the_midday_news(conn):
    _run(conn, "news_catchup", "SUCCESS", dt.datetime(2026, 10, 1, 8, 20))
    probs = _labels(conn, dt.datetime(2026, 10, 1, 13, 0))
    assert "News (morning)" not in probs and probs["News (midday)"].kind == "MISSED"


def test_plan_orders_steps_dedupes_and_limits_attempts(conn):
    from pipeline.health import Problem
    from pipeline.recover import plan, MAX_ATTEMPTS
    probs = [Problem("Morning brief", "MISSED", "", ("morning_digest",)),
             Problem("News (midday)", "MISSED", "", ()), Problem("News (morning)", "MISSED", "", ()),
             Problem("Global markets (pre-market)", "MISSED", "", ()),
             Problem("NSE index closes", "MISSED", "", ())]                     # no remedy: reported only
    now = dt.datetime(2026, 10, 2, 10, 0)
    p = plan(conn, probs, now)
    assert [s["step"] for s in p] == ["global", "news", "brief"]
    assert len(p[1]["labels"]) == 2 and all(s["allowed"] for s in p)
    conn.execute("INSERT INTO job_recovery (day,step,attempts,last_at) VALUES (?,?,?,?)",
                 ("2026-10-02", "news", 1, dt.datetime(2026, 10, 2, 9, 50)))
    conn.execute("INSERT INTO job_recovery (day,step,attempts,last_at) VALUES (?,?,?,?)",
                 ("2026-10-02", "brief", MAX_ATTEMPTS, dt.datetime(2026, 10, 2, 8, 0)))
    conn.commit()
    p = {s["step"]: s for s in plan(conn, probs, now)}
    assert p["global"]["allowed"]
    assert not p["news"]["allowed"] and "next try" in p["news"]["reason"]
    assert not p["brief"]["allowed"] and "needs attention" in p["brief"]["reason"]


def test_check_and_recover_runs_the_remedy_records_it_and_rechecks(conn, monkeypatch):
    import pipeline.recover as R
    calls = []

    def fake_step(step, now):
        calls.append(step)
        from db.schema import get_connection
        c = get_connection()
        name = {"global": "global_recover", "news": "news_recover", "brief": "morning_digest"}.get(step)
        if name:
            _run(c, name, "SUCCESS", dt.datetime.now())
        c.close()
        return {"status": "FAILED", "error": "token expired"} if step == "portfolio" else {"status": "SUCCESS"}
    monkeypatch.setattr(R, "_run_step", fake_step)
    out = R.check_and_recover()
    assert calls[0] == "global" and calls[-1] == "brief"
    assert "Global markets (pre-market)" in out["recovered"]
    assert "Global markets (pre-market)" not in {p.label for p in out["problems"]}
    assert "Portfolio sync" in {p.label for p in out["problems"]}             # still open, reported
    rec = {r["step"]: r for r in R.today(conn)}
    assert rec["global"]["result"] == "RAN" and rec["portfolio"]["result"].startswith("FAILED")
    assert R.check_and_recover()["ran"] == [], "within MIN_GAP nothing is re-run again"


def test_recovery_notice_and_brief_report_reruns(conn, monkeypatch):
    from alerts.telegram import send_recovery_notice, send_morning_digest
    r = send_recovery_notice({"ran": [{"step": "news", "labels": ["MISSED News (morning)"], "result": "RAN"}],
                              "recovered": ["News (morning)"], "skipped": []})
    assert r["recorded"]
    from pipeline.recover import ensure_table
    ensure_table(conn)
    conn.execute("INSERT INTO ai_scores (symbol,date,atip_score,`signal`) VALUES ('ACME','2026-10-01',60,'HOLD')")
    conn.execute("INSERT INTO job_recovery (day,step,attempts,last_at,last_result,problems) VALUES (?,?,?,?,?,?)",
                 (str(dt.date.today()), "news", 1, dt.datetime.now(), "RAN", "MISSED News (morning)"))
    conn.commit()
    assert send_morning_digest() is True
    body = conn.execute("SELECT message FROM alert_log WHERE category='digest'").fetchone()[0]
    assert "Re-run today" in body and "Morning brief" not in body


def test_wake_from_sleep_runs_catchup_and_recovery(monkeypatch):
    import pipeline.scheduler as S
    seen = []
    for n in ("run_startup_market_catchup", "run_morning_catchup", "run_postmarket_if_missing", "run_health_check"):
        monkeypatch.setattr(S, n, lambda n=n: seen.append(n))
    S._on_wake(dt.datetime.now() - dt.timedelta(hours=7))
    assert seen == ["run_startup_market_catchup", "run_morning_catchup", "run_postmarket_if_missing",
                    "run_health_check"]
