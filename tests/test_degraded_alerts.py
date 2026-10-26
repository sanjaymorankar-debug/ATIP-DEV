"""
P1-7: alerts on degraded states.

A job that ran but did not do its work used to count as working -- run_job alerts only on FAILED: the
Dhan quote refresh returning PARTIAL (no quotes: the Data API plan has been inactive since ~18 Sep
2026), and the aggressive exit manager skipping positions it may not manage (MODE_MISMATCH). Intraday
data freshness was counted in sessions, so quotes that stopped at 10:00 looked current until the next day.
"""

import datetime as dt

import pandas as pd
import pytest

MON = dt.date(2026, 10, 26)                 # a trading Monday


def at(h, m):
    return dt.datetime.combine(MON, dt.time(h, m))


def _run(conn, name, status, start, rows=5, error=None):
    conn.execute("INSERT INTO pipeline_log (run_date,job_name,start_time,end_time,status,rows_processed,error_msg,kind) "
                 "VALUES (?,?,?,?,?,?,?,'run')", (str(start.date()), name, start, start, status, rows, error))
    conn.commit()


@pytest.fixture
def conn(temp_db):
    from db.schema import get_connection, init_db
    init_db()
    c = get_connection()
    _run(c, "ops_monitor", "SUCCESS", dt.datetime(2026, 10, 1, 7, 0))          # monitoring began
    yield c
    c.close()


def _probs(conn, h, m):
    from pipeline.health import check_job_health
    return {p.label: p for p in check_job_health(conn, at(h, m))}


# ── pipeline/health.py ───────────────────────────────────────────────────────

def test_a_partial_quote_refresh_is_degraded_not_working(conn):
    for t in (at(10, 0), at(10, 15)):
        _run(conn, "dhan_quotes_intraday", "PARTIAL", t, rows=0, error="Dhan returned no quotes for 500 symbols")
    p = _probs(conn, 10, 40)["Live quotes (Dhan)"]
    assert p.kind == "DEGRADED"
    assert p.detail == "last run PARTIAL at 10:15 (0 rows): Dhan returned no quotes for 500 symbols"
    assert p.key == "DEGRADED:dhan_quotes_intraday|dhan_quotes_catchup|dhan_quotes_preclose"
    _run(conn, "dhan_quotes_intraday", "SUCCESS", at(10, 30), rows=480)
    assert "Live quotes (Dhan)" not in _probs(conn, 10, 40)
    assert _probs(conn, 12, 0)["Live quotes (Dhan)"].kind == "STALLED", "nothing since 10:30"
    assert "Live quotes (Dhan)" not in _probs(conn, 18, 0), "after the close nothing is due"


def test_a_mode_mismatch_is_degraded_while_the_market_is_open(conn):
    assert "Aggressive exits" not in _probs(conn, 11, 0), "it runs only while a position is open"
    _run(conn, "aggressive_manage", "MODE_MISMATCH", at(10, 58), rows=None,
         error="1 open position(s) not managed: opened in PAPER, orders now go to LIVE")
    p = _probs(conn, 11, 10)["Aggressive exits"]
    assert p.kind == "DEGRADED" and p.detail.startswith("last run MODE_MISMATCH at 10:58: 1 open position(s)")
    _run(conn, "aggressive_manage", "SUCCESS", at(11, 12), rows=None)
    assert "Aggressive exits" not in _probs(conn, 11, 20)
    _run(conn, "aggressive_manage", "MODE_MISMATCH", at(15, 28), rows=None, error="not managed")
    assert "Aggressive exits" in _probs(conn, 15, 40), "the 15:40 check still sees the session's last pass"
    assert "Aggressive exits" not in _probs(conn, 20, 0)


def test_degraded_problems_alert_once_a_day_and_are_not_re_run(conn):
    from alerts.telegram import send_job_health_alerts
    from pipeline.recover import plan
    _run(conn, "dhan_quotes_intraday", "PARTIAL", at(10, 15), rows=0)
    probs = [p for p in _probs(conn, 10, 40).values() if p.label == "Live quotes (Dhan)"]
    assert send_job_health_alerts(probs) == 1 and send_job_health_alerts(probs) == 0
    row = conn.execute("SELECT title, severity FROM alert_log WHERE category='job_health'").fetchone()
    assert tuple(row) == ("Job Degraded: Live quotes (Dhan)", "warning")
    assert plan(conn, probs, at(10, 40)) == [], "re-running cannot bring a lapsed data plan back"


# ── strategy/live.py: MODE_MISMATCH alerts at once, one alert a day either way ─

def test_a_mode_mismatch_alerts_at_once_under_the_health_checks_key(temp_db, cfg, monkeypatch):
    from alerts.telegram import send_job_health_alerts
    from db.schema import get_connection, init_db
    from pipeline.health import EXPECTATIONS, Problem
    from pipeline.scheduler import run_job
    from strategy import aggressive as A, live as L, positions as P
    init_db()
    P.open_position("ACME", entry_price=100.0, quantity=10, atr_pct=1.5, cfg=cfg)
    monkeypatch.setattr(A, "config", lambda refresh=False: cfg)
    monkeypatch.setattr("orders.environment.broker_env", lambda: "LIVE")      # orders go LIVE, the position is PAPER
    monkeypatch.setattr("data.dhan.fetch_live_quotes", lambda *a, **k: pytest.fail("no quote call expected"))
    assert L.scheduled_pass(run_job, now=at(11, 0))["status"] == "MODE_MISMATCH"
    c = get_connection()
    try:
        rows = c.execute("SELECT title, message, dedupe_key FROM alert_log").fetchall()
    finally:
        c.close()
    assert len(rows) == 1 and rows[0]["dedupe_key"] == "health:DEGRADED:aggressive_manage"
    assert rows[0]["title"] == "Job Degraded: Aggressive exits"
    assert "ACME (PAPER)" in rows[0]["message"] and "not managed" in rows[0]["message"]
    e = next(x for x in EXPECTATIONS if x.label == "Aggressive exits")
    assert send_job_health_alerts([Problem(e.label, "DEGRADED", "the 30-minute check", e.jobs)]) == 0, \
        "the health check's alert for it is the same alert, not a second one"
    L.scheduled_pass(run_job, now=at(11, 2))
    c = get_connection()
    try:
        assert c.execute("SELECT COUNT(*) FROM alert_log").fetchone()[0] == 1
    finally:
        c.close()


def test_an_empty_quote_refresh_says_why(monkeypatch):
    import data.dhan as D
    monkeypatch.setattr(D, "HAS_DHAN", True)
    monkeypatch.setattr(D, "get_dhan_client", lambda: (object(), None))
    monkeypatch.setattr(D, "fetch_live_quotes", lambda symbols, dhan=None: pd.DataFrame())
    assert D.run_live_quote_refresh(["ACME", "TCS"]) == {"status": "PARTIAL", "rows": 0,
                                                         "error": "Dhan returned no quotes for 2 symbols"}


# ── ops/data_health.py: quote staleness in minutes ──────────────────────────

def test_intraday_quotes_are_judged_in_minutes_while_the_session_is_open(conn, monkeypatch):
    from ops import data_health as DH
    from ops.monitor import r_data_stale
    conn.execute("CREATE TABLE IF NOT EXISTS live_quotes (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, ltp REAL, "
                 "open REAL, high REAL, low REAL, prev_close REAL, volume INTEGER, chg_pct REAL, timestamp TEXT)")
    conn.execute("INSERT INTO live_quotes (symbol, ltp, timestamp) VALUES ('ACME', 100, '2026-10-26 10:00:00')")
    conn.execute("INSERT INTO live_quotes (symbol, ltp, timestamp) VALUES ('TCS', 3000, '2026-10-26T10:05:00')")
    conn.execute("INSERT INTO index_levels (date, time, nifty50) VALUES ('2026-10-26', '10:10:00', 22750)")
    conn.commit()

    def src(h, m, name):
        return next(s for s in DH.sources(conn, now=at(h, m)) if s["source"] == name)
    q = src(10, 15, "live_quotes")
    assert (q["age_minutes"], q["max_age_minutes"], q["basis"], q["status"]) == (10, 20, "minutes", "OK")
    q = src(10, 45, "live_quotes")
    assert (q["age_minutes"], q["status"]) == (40, "STALE"), "a session-day count would still say OK"
    assert src(10, 40, "index_levels")["status"] == "OK"                       # 30 min: at the limit
    assert (src(10, 41, "index_levels")["age_minutes"], src(10, 41, "index_levels")["status"]) == (31, "STALE")
    q = src(18, 0, "live_quotes")
    assert (q["basis"], q["status"], q["age_minutes"]) == ("sessions", "OK", 475), "after the close: sessions"
    assert src(9, 20, "live_quotes")["basis"] == "sessions", "before the first refresh is due"
    real = DH.sources
    monkeypatch.setattr(DH, "sources", lambda c: real(c, now=at(10, 45)))
    firing, _, msg = r_data_stale(conn)
    assert firing and "live_quotes STALE (40 min old)" in msg
