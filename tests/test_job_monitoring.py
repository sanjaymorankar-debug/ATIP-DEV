"""
Job logging and health (MON-01, MON-02).

pipeline_log could not say whether the pipeline was working: run_job wrote
nothing, the rows job functions wrote had no end time, a job that returned
FAILED was logged with a green tick, and a job that silently stopped -- the
news job, from 2026-09-10 -- left no trace at all.
"""

import datetime as dt


def _runs(conn, name=None):
    q = "SELECT job_name, status, rows_processed, error_msg, kind, run_date, start_time, end_time, duration_s " \
        "FROM pipeline_log WHERE kind='run'"
    return [dict(r) for r in conn.execute(q + (" AND job_name=?" if name else ""), (name,) if name else ())]


def test_every_run_is_recorded_with_its_start_end_and_rows(temp_db):
    from db.schema import init_db, get_connection
    from pipeline.scheduler import run_job
    init_db()
    run_job("demo", lambda td: {"status": "SUCCESS", "rows": 7}, dt.date(2026, 9, 23))
    conn = get_connection()
    try:
        (r,) = _runs(conn, "demo")
        assert (r["status"], r["rows_processed"], r["kind"], str(r["run_date"])) == ("SUCCESS", 7, "run", "2026-09-23")
        assert r["end_time"] >= r["start_time"] and r["duration_s"] is not None
    finally:
        conn.close()


def test_a_returned_failure_is_a_failure_and_alerts(temp_db, monkeypatch):
    from db.schema import init_db, get_connection
    from pipeline import scheduler
    init_db()
    alerts = []
    monkeypatch.setattr(scheduler, "_alert_failure", lambda name, err: alerts.append((name, err)))
    scheduler.run_job("sync", lambda: {"status": "FAILED", "error": "token expired"})
    scheduler.run_job("boom", lambda: 1 / 0)
    conn = get_connection()
    try:
        runs = {r["job_name"]: r for r in _runs(conn)}
        assert runs["sync"]["status"] == "FAILED" and runs["sync"]["error_msg"] == "token expired"
        assert runs["boom"]["status"] == "FAILED" and "ZeroDivisionError" in runs["boom"]["error_msg"]
        assert runs["boom"]["rows_processed"] is None
    finally:
        conn.close()
    assert [a[0] for a in alerts] == ["sync", "boom"]


def test_success_with_nothing_processed_is_recorded_as_empty(temp_db):
    from db.schema import init_db, get_connection
    from pipeline.scheduler import run_job
    init_db()
    run_job("news", lambda: {"status": "SUCCESS", "rows": 0})
    run_job("skipped", lambda: {"status": "SKIPPED", "rows": 0, "reason": "not a session"})
    run_job("opaque", lambda: None)
    conn = get_connection()
    try:
        runs = {r["job_name"]: r for r in _runs(conn)}
        assert runs["news"]["status"] == "EMPTY"
        assert (runs["skipped"]["status"], runs["skipped"]["error_msg"]) == ("SKIPPED", "not a session")
        assert (runs["opaque"]["status"], runs["opaque"]["rows_processed"]) == ("SUCCESS", None)
    finally:
        conn.close()


def test_a_step_row_from_inside_a_job_is_marked_as_a_step(temp_db):
    from db.schema import init_db, get_connection, log_job
    init_db()
    log_job("signal_log", "HELD", 0, run_date=dt.date(2026, 9, 23))
    conn = get_connection()
    try:
        assert tuple(conn.execute("SELECT kind, end_time IS NOT NULL FROM pipeline_log").fetchone()) == ("step", 1)
    finally:
        conn.close()


# ── MON-02: job health ───────────────────────────────────────────────────────

def _run(conn, name, status, start, rows=5, error=None):
    conn.execute("INSERT INTO pipeline_log (run_date,job_name,start_time,end_time,status,rows_processed,"
                 "error_msg,kind) VALUES (?,?,?,?,?,?,?,'run')",
                 (str(start.date()), name, start, start, status, rows, error))


def test_a_daily_job_that_did_not_run_is_missed(temp_db):
    from db.schema import init_db, get_connection
    from pipeline.health import check_job_health
    init_db()
    conn = get_connection()
    try:
        _run(conn, "global_premarket", "SUCCESS", dt.datetime(2026, 9, 22, 7, 0))   # monitoring began
        _run(conn, "news_premarket", "SUCCESS", dt.datetime(2026, 9, 22, 7, 1))
        _run(conn, "global_premarket", "SUCCESS", dt.datetime(2026, 9, 23, 7, 0))
        now = dt.datetime(2026, 9, 23, 8, 40)
        probs = {p.label: p for p in check_job_health(conn, now)}
        assert probs["News (morning)"].kind == "MISSED"
        assert "Global markets (pre-market)" not in probs
        assert "AI scoring" not in probs, "its deadline today has not passed"
    finally:
        conn.close()


def test_nothing_is_judged_before_monitoring_began(temp_db):
    from db.schema import init_db, get_connection
    from pipeline.health import check_job_health
    init_db()
    conn = get_connection()
    try:
        assert check_job_health(conn, dt.datetime(2026, 9, 23, 22, 0)) == [], "no run rows yet"
        _run(conn, "global_premarket", "SUCCESS", dt.datetime(2026, 9, 23, 9, 0))
        labels = {p.label for p in check_job_health(conn, dt.datetime(2026, 9, 23, 22, 0))}
        assert "News (morning)" not in labels, "monitoring started after that morning"
    finally:
        conn.close()


def test_repeated_failures_and_empty_runs_are_reported(temp_db):
    from db.schema import init_db, get_connection
    from pipeline.health import check_job_health
    init_db()
    conn = get_connection()
    try:
        for h in (7, 8, 9):
            _run(conn, "dhan_portfolio", "FAILED", dt.datetime(2026, 9, 22, h, 0), rows=None, error="DH-901")
        _run(conn, "news_premarket", "EMPTY", dt.datetime(2026, 9, 23, 7, 1), rows=0)
        probs = {p.label: p for p in check_job_health(conn, dt.datetime(2026, 9, 23, 8, 45))}
        assert probs["Portfolio sync"].kind == "FAILING" and "DH-901" in probs["Portfolio sync"].detail
        assert probs["News (morning)"].kind == "EMPTY"
    finally:
        conn.close()


def test_an_intraday_job_that_stops_is_stalled_only_in_market_hours(temp_db):
    from db.schema import init_db, get_connection
    from pipeline.health import check_job_health
    init_db()
    conn = get_connection()
    try:
        _run(conn, "intraday_indexes", "SUCCESS", dt.datetime(2026, 9, 23, 9, 15))
        at = lambda h, m: {p.label for p in check_job_health(conn, dt.datetime(2026, 9, 23, h, m))}
        assert "Index levels (intraday)" not in at(9, 45)
        assert "Index levels (intraday)" in at(11, 0)
        assert "Index levels (intraday)" not in at(16, 0), "after the close nothing is due"
    finally:
        conn.close()


# ── MON-03 / DB-14 / RK-15: alerts ───────────────────────────────────────────

def test_every_alert_is_recorded_even_without_telegram_and_deduped_by_key(temp_db):
    from db.schema import init_db, get_connection
    from alerts.telegram import notify, fmt
    init_db()
    a = notify(fmt("🩺", "Job Missed: News", "no run"), category="job_health", key="health:x")
    b = notify(fmt("🩺", "Job Missed: News", "no run"), category="job_health", key="health:x")
    assert (a["recorded"], a["sent"], b["duplicate"]) == (True, False, True)
    conn = get_connection()
    try:
        rows = conn.execute("SELECT title, category, telegram_sent, telegram_error FROM alert_log").fetchall()
        assert [tuple(r) for r in rows] == [("Job Missed: News", "job_health", 0, "test")]
    finally:
        conn.close()


def test_telegram_errors_never_carry_the_token(monkeypatch):
    import alerts.telegram as tg
    import requests
    monkeypatch.setattr(tg, "load_config", lambda: {"telegram_token": "123:SECRET", "telegram_chat_id": "9"})

    def boom(url, **kw):
        raise requests.HTTPError(f"401 Client Error: Unauthorized for url: {url}")
    monkeypatch.setattr(tg.requests, "post", boom)
    ok, err = tg._real_deliver_telegram("hi")        # conftest stubs the public one
    assert not ok and "SECRET" not in err and "<token>" in err


def test_a_failed_job_and_a_halt_raise_recorded_alerts(temp_db, tmp_path, monkeypatch):
    from db.schema import init_db, get_connection
    from pipeline.scheduler import run_job
    from orders import risk
    init_db()
    monkeypatch.setattr(risk, "HALT_FLAG", tmp_path / "TRADING_HALTED")
    run_job("dhan_portfolio", lambda: {"status": "FAILED", "error": "token expired"})
    risk.halt("drawdown")
    risk.resume()
    conn = get_connection()
    try:
        got = [tuple(r) for r in conn.execute("SELECT category, title FROM alert_log ORDER BY id")]
        assert got == [("job", "Pipeline Failure"), ("risk", "Trading Halted"), ("risk", "Trading Resumed")]
    finally:
        conn.close()


def test_the_morning_brief_counts_as_delivered_on_the_dashboard(temp_db):
    from db.schema import init_db, get_connection
    from alerts.telegram import send_morning_digest
    init_db()
    conn = get_connection()
    conn.execute("INSERT INTO ai_scores (symbol,date,atip_score,`signal`) VALUES ('ACME','2026-09-23',60,'HOLD')")
    conn.commit(); conn.close()
    assert send_morning_digest() is True
    conn = get_connection()
    try:
        assert conn.execute("SELECT category FROM alert_log").fetchone()[0] == "digest"
    finally:
        conn.close()
