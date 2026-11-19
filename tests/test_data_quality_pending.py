"""
The daily data-quality false alarm (19 Nov 2026).

On 18 Nov 2026 the post-market data-quality check ran at 16:48:10, before NSE had published the
session's index-close file; the evening watch stored the 13 closes at 17:00:04. The session was
stored -- and alerted on Telegram -- as DQS 89.86 ERROR: index_series 13/13, plus
non_session_dates for the 8 Nov Muhurat bars (real NSE data, filled on purpose on 16 Nov).
Every day until 8 Dec carried the same two findings.

Now: missing index closes are "pending" (INFO, outside the DQS, no alert) until the evening
watch's 21:30 deadline on the session's own day; recheck_pending() re-runs the checks once the
closes are stored, or at the deadline (still missing = ERROR, as before). Muhurat dates listed in
NSE_MUHURAT_SESSIONS are not counted as non-session dates.
"""

import datetime as dt
import json

import pytest

D = dt.date
T = dt.datetime
TD = D(2026, 11, 18)                   # Wednesday, the session of the false alarm
PREV = D(2026, 11, 17)
MUHURAT = D(2026, 11, 8)               # Sunday, Diwali Laxmi Pujan: Muhurat session
UNIVERSE = ["ACME", "BETA", "GAMMA"]


@pytest.fixture
def db(temp_db, monkeypatch):
    from db.schema import init_db, get_connection
    import data.dhan as dhan
    init_db()
    monkeypatch.setattr(dhan, "get_tracked_symbols", lambda conn=None: list(UNIVERSE))
    monkeypatch.setattr(dhan, "SEC_LIST_PATH", temp_db.parent / "no-security-list.csv")
    conn = get_connection()
    for i, d in enumerate((MUHURAT, D(2026, 11, 13), D(2026, 11, 16), PREV, TD)):
        for j, s in enumerate(UNIVERSE):
            c = 100.0 + i + j
            conn.execute("INSERT INTO prices_daily (symbol, date, open, high, low, close, volume, source) "
                         "VALUES (?,?,?,?,?,?,?,?)", (s, str(d), c, c + 1, c - 1, c, 1000 + i, "bhavcopy"))
    for d in (PREV,):                   # the previous session's closes are in, as on the VPS
        _store_index_closes(conn, d)
    conn.commit()
    yield conn
    conn.close()


def _store_index_closes(conn, d):
    from data.dhan import INDEX_SERIES_SYMBOLS
    for s in set(INDEX_SERIES_SYMBOLS.values()):
        conn.execute("INSERT OR REPLACE INTO prices_daily (symbol, date, open, high, low, close, volume, source) "
                     "VALUES (?,?,?,?,?,?,?,?)", (s, str(d), 500.0, 500.0, 500.0, 500.0, 0, "nse_index"))


@pytest.fixture
def nse(db, monkeypatch):
    """NSE's index-close file: stored by the fake sync once `published` is set; every call recorded."""
    import data.bhavcopy as B
    import alerts.telegram as tg
    from db.schema import get_connection
    state = {"published": False, "syncs": [], "alerts": []}

    def sync(trade_date=None, lookback=5):
        state["syncs"].append((str(trade_date), lookback))
        if not state["published"]:
            return {"status": "SKIPPED", "rows": 0, "sessions": {}, "waiting": [str(trade_date)],
                    "reason": "not published"}
        c = get_connection()
        try:
            _store_index_closes(c, trade_date)
            c.commit()
        finally:
            c.close()
        return {"status": "SUCCESS", "rows": 13, "sessions": {str(trade_date): 500.0}, "waiting": []}

    monkeypatch.setattr(B, "sync_nse_index_closes", sync)
    monkeypatch.setattr(tg, "notify", lambda message, **k: state["alerts"].append(message) or {"sent": False})
    return state


def _rows(conn, d=TD):
    return {r[0]: {"severity": r[1], "failed": r[2], "checked": r[3], "score": r[4], "detail": json.loads(r[5]),
                   "run_at": str(r[6])}
            for r in conn.execute("SELECT check_name, severity, failed, checked, score, detail, run_at "
                                  "FROM data_quality WHERE date=?", (str(d),))}


def _last_job(conn, d=TD):
    r = conn.execute("SELECT status FROM pipeline_log WHERE job_name='data_quality' AND run_date=? "
                     "ORDER BY id DESC LIMIT 1", (str(d),)).fetchone()
    return r[0] if r else None


# ── the 18 Nov replay ─────────────────────────────────────────────────────────────────────────────

def test_18_nov_at_1648_the_missing_index_closes_are_pending_not_an_error(db, nse):
    from data.quality import run_data_quality
    res = run_data_quality(TD, now=T(2026, 11, 18, 16, 48, 10))
    rows = _rows(db)
    assert rows["index_series"]["severity"] == "INFO"
    assert rows["index_series"]["failed"] == rows["index_series"]["checked"] == 13
    assert rows["index_series"]["detail"]["pending"] is True
    assert "pending" in rows["index_series"]["detail"]["note"]
    assert rows["non_session_dates"]["severity"] == "OK", "the 8 Nov Muhurat bars are real data"
    assert rows["DQS"]["severity"] == "OK" and rows["DQS"]["score"] == 100.0
    assert rows["DQS"]["failed"] == 0, "a pending check is outside the DQS counts"
    assert res["worst"] == "OK" and res["pending"] == ["index_series"]
    assert nse["alerts"] == [], "no Telegram alert for closes NSE has not published yet"
    assert _last_job(db) == "OK"


def test_18_nov_at_1700_the_watch_stores_the_closes_and_the_check_turns_ok(db, nse):
    from data.quality import run_data_quality, recheck_pending
    run_data_quality(TD, now=T(2026, 11, 18, 16, 48, 10))
    nse["published"] = True
    res = recheck_pending(now=T(2026, 11, 18, 17, 0, 4))
    assert res["rechecked"] == {str(TD): "OK"}
    assert nse["syncs"] == [(str(TD), 1)]
    rows = _rows(db)
    assert rows["index_series"]["severity"] == "OK" and rows["index_series"]["failed"] == 0
    assert "pending" not in rows["index_series"]["detail"]
    assert rows["DQS"]["severity"] == "OK" and nse["alerts"] == []
    assert recheck_pending(now=T(2026, 11, 18, 17, 10))["reason"] == "nothing pending"
    assert len(nse["syncs"]) == 1, "nothing left to fetch"


def test_while_nse_has_not_published_nothing_is_rerun(db, nse):
    from data.quality import run_data_quality, recheck_pending
    run_data_quality(TD, now=T(2026, 11, 18, 16, 48, 10))
    before = _rows(db)["index_series"]["run_at"]
    res = recheck_pending(now=T(2026, 11, 18, 17, 10))
    assert res["status"] == "SKIPPED" and res["waiting"] == [str(TD)] and res["rechecked"] == {}
    assert nse["syncs"] == [(str(TD), 1)], "it did ask NSE"
    assert _rows(db)["index_series"]["run_at"] == before and _rows(db)["index_series"]["severity"] == "INFO"


def test_closes_still_missing_at_the_2130_deadline_are_an_error_and_alert(db, nse):
    from data.quality import run_data_quality, recheck_pending
    run_data_quality(TD, now=T(2026, 11, 18, 16, 48, 10))
    res = recheck_pending(now=T(2026, 11, 18, 21, 30))
    assert res["rechecked"] == {str(TD): "ERROR"}
    rows = _rows(db)
    assert rows["index_series"]["severity"] == "ERROR" and rows["index_series"]["failed"] == 13
    assert rows["DQS"]["severity"] == "ERROR" and rows["DQS"]["score"] < 90.0
    assert len(nse["alerts"]) == 1 and "index_series" in nse["alerts"][0], "a real alarm still alerts"
    assert _last_job(db) == "ERROR"


def test_a_session_left_pending_by_a_stopped_process_is_settled_the_next_morning(db, nse):
    from data.quality import run_data_quality, recheck_pending
    run_data_quality(TD, now=T(2026, 11, 18, 16, 48, 10))
    nse["published"] = True                               # the file came while ATIP was down
    res = recheck_pending(now=T(2026, 11, 19, 6, 0))
    assert res["rechecked"] == {str(TD): "OK"} and _rows(db)["index_series"]["severity"] == "OK"


def test_a_past_session_and_a_late_run_are_never_pending(db, nse):
    """Catch-up and backfill runs judge a session as before: a close still missing is an ERROR."""
    from data.quality import run_data_quality
    for now in (T(2026, 11, 19, 8, 0), T(2026, 11, 18, 21, 45)):
        res = run_data_quality(TD, now=now)
        assert res["worst"] == "ERROR" and res["pending"] == []
        assert _rows(db)["index_series"]["severity"] == "ERROR"


def test_closes_in_by_1648_are_ok_at_once(db, nse):
    from data.quality import run_data_quality
    _store_index_closes(db, TD)
    db.commit()
    res = run_data_quality(TD, now=T(2026, 11, 18, 16, 48, 10))
    assert res["worst"] == "OK" and res["pending"] == []
    assert _rows(db)["index_series"]["severity"] == "OK"


def test_one_index_close_missing_counts_as_pending_then_as_its_own_failure(db, nse):
    from data.quality import run_data_quality
    from data.dhan import INDEX_SERIES_SYMBOLS
    _store_index_closes(db, TD)
    db.execute("DELETE FROM prices_daily WHERE date=? AND symbol=?", (str(TD), INDEX_SERIES_SYMBOLS["india_vix"]))
    db.commit()
    assert run_data_quality(TD, now=T(2026, 11, 18, 18, 0))["pending"] == ["index_series"]
    res = run_data_quality(TD, now=T(2026, 11, 18, 21, 30))
    assert res["problems"] == {"index_series": 1}
    assert _rows(db)["index_series"]["detail"]["sample"] == [INDEX_SERIES_SYMBOLS["india_vix"]]


# ── Muhurat ───────────────────────────────────────────────────────────────────────────────────────

def test_muhurat_bars_are_not_non_session_dates_but_other_weekend_bars_are(db, nse):
    from data.quality import run_data_quality
    from utils.trading_calendar import NSE_MUHURAT_SESSIONS, is_trading_day
    assert MUHURAT in NSE_MUHURAT_SESSIONS
    assert not is_trading_day(MUHURAT), "a Muhurat Sunday is still not a session (nothing scores it)"
    db.execute("INSERT INTO prices_daily (symbol, date, open, high, low, close, volume, source) "
               "VALUES ('ACME', '2026-11-07', 1, 1, 1, 1, 1, 'dhan')")          # a Saturday: a real problem
    db.commit()
    run_data_quality(TD, now=T(2026, 11, 18, 16, 48, 10))
    r = _rows(db)["non_session_dates"]
    assert r["failed"] == 1 and r["detail"]["sample"] == ["2026-11-07"]


def test_muhurat_list_holds_only_closed_days():
    """A Muhurat on a working day that is not a holiday (2025-10-21) is already a session."""
    from utils.trading_calendar import NSE_MUHURAT_SESSIONS, NSE_HOLIDAYS
    for d in NSE_MUHURAT_SESSIONS:
        assert d.weekday() >= 5 or d in NSE_HOLIDAYS


# ── wiring ────────────────────────────────────────────────────────────────────────────────────────

def test_the_deadline_is_the_evening_watch_end():
    from data.quality import INDEX_CLOSES_DUE
    from pipeline.scheduler import FII_DII_WATCH_END
    assert INDEX_CLOSES_DUE.strftime("%H:%M") == FII_DII_WATCH_END


def test_the_evening_watch_rechecks_even_when_the_signals_are_not_held(monkeypatch):
    from pipeline import scheduler as S
    import data.quality as Q
    got = []
    monkeypatch.setattr(S, "is_market_day", lambda *a, **k: True)
    monkeypatch.setattr(S, "_debounce", lambda name: True)
    monkeypatch.setattr(S, "_signal_log_state", lambda d: "logged")
    monkeypatch.setattr(Q, "recheck_pending", lambda now=None: got.append("recheck"))
    monkeypatch.setattr(S, "run_job", lambda *a, **k: got.append("settle"))
    S.run_fii_dii_watch()
    assert got == ["recheck"]


def test_the_post_market_catch_up_rechecks(temp_db, monkeypatch):
    from db.schema import init_db
    from pipeline import scheduler as S
    import data.quality as Q
    init_db()
    got = []
    monkeypatch.setattr(S, "postmarket_target_date", lambda: TD)
    monkeypatch.setattr(S, "run_postmarket", lambda **k: got.append("post"))
    monkeypatch.setattr(S, "release_held_signals", lambda now=None: got.append("release"))
    monkeypatch.setattr(Q, "recheck_pending", lambda now=None: got.append("recheck"))
    S.run_postmarket_if_missing()
    assert got[:2] == ["release", "recheck"]


def test_a_failing_recheck_never_stops_the_watch(monkeypatch):
    from pipeline import scheduler as S
    import data.quality as Q

    def boom(now=None):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(Q, "recheck_pending", boom)
    S._recheck_data_quality()                 # logs a warning, raises nothing
