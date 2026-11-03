"""The 06:00 resume (step 4) runs the 20:30 technical signals for a scored session that has none
(2026-11-02: ai_scores for the Monday, technical_signal still on the Friday)."""

from datetime import date


def _seed(conn, d, snap=False):
    conn.execute("INSERT INTO ai_scores (symbol, date, atip_score) VALUES ('ACME', ?, 60)", (d,))
    if snap:
        conn.execute("INSERT INTO technical_snapshot (symbol, date, close) VALUES ('ACME', ?, 100)", (d,))


def test_a_scored_session_without_signals_is_caught_up_and_today_is_left_alone(temp_db, monkeypatch):
    from db.schema import init_db, get_connection
    from research import tech_signals as TS
    init_db()
    c = get_connection()
    TS.ensure_tables(c)
    _seed(c, "2026-10-30", snap=True)
    _seed(c, "2026-11-02")                         # scored, no 20:30 run
    _seed(c, "2026-11-03")                         # today: its own 20:30 slot
    c.commit()
    assert TS.missing_sessions(c, date(2026, 11, 3)) == ["2026-11-02"]
    c.close()
    ran = []
    monkeypatch.setattr(TS, "run_technical", lambda as_of=None, **k: ran.append(as_of) or {"rows": 7})
    out = TS.run_missing(date(2026, 11, 3))
    assert ran == ["2026-11-02"] and out == {"status": "SUCCESS", "rows": 7, "sessions": {"2026-11-02": 7}}


def test_nothing_missing_is_no_new(temp_db, monkeypatch):
    from db.schema import init_db, get_connection
    from research import tech_signals as TS
    init_db()
    c = get_connection()
    TS.ensure_tables(c)
    _seed(c, "2026-11-02", snap=True)
    c.commit()
    c.close()
    monkeypatch.setattr(TS, "run_technical", lambda **k: (_ for _ in ()).throw(AssertionError("must not run")))
    assert TS.run_missing(date(2026, 11, 3))["status"] == "NO_NEW"


def test_resume_step_4_runs_the_catch_up(temp_db, monkeypatch):
    from db.schema import init_db
    from pipeline import operating_schedule as OS
    from pipeline import scheduler as S
    from research import tech_signals as TS
    import data.ema_gap as EG
    init_db()
    monkeypatch.setenv("ATIP_OPERATING_SCHEDULE", "off")
    monkeypatch.setattr(S, "run_postmarket_if_missing", lambda: None)
    monkeypatch.setattr(EG, "run", lambda *a, **k: {"status": "SUCCESS", "rows": 1})
    monkeypatch.setattr(TS, "run_missing", lambda *a, **k: {"status": "SUCCESS", "rows": 3})
    out = OS.step_analysis({})
    assert out["parts"]["technical_signals_catchup"] == "SUCCESS"
