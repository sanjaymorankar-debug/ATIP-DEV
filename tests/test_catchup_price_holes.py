"""
Holes in prices_daily are recovered by the post-market catch-up (2026-10-26).

ATIP was off from 2026-10-13 to the 26th (the Windows to Mac move). The catch-up only re-ran sessions
whose bars were already stored, so nine sessions never got bars and the indicators and scores of the
23rd and 26th were computed across a two-week hole.
"""

import datetime as dt

import pytest

D = dt.date


@pytest.fixture
def db(temp_db):
    from db.schema import init_db, get_connection
    init_db()
    conn = get_connection()
    yield conn
    conn.close()


def _bar(conn, d, symbols=("ACME", "BETA")):
    for s in symbols:
        conn.execute("INSERT OR REPLACE INTO prices_daily (symbol, date, open, high, low, close, volume, source) "
                     "VALUES (?,?,?,?,?,?,?,?)", (s, str(d), 100.0, 100.0, 100.0, 100.0, 1000, "bhavcopy"))
    conn.commit()


def _score(conn, d, symbols=("ACME", "BETA")):
    for s in symbols:
        conn.execute("INSERT INTO ai_scores (symbol, date, atip_score, `signal`) VALUES (?,?,?,?)",
                     (s, str(d), 60.0, "HOLD"))
    conn.commit()


class _FrozenNow(dt.datetime):
    frozen = None

    @classmethod
    def now(cls, tz=None):
        return cls.frozen


@pytest.fixture
def calls(db, monkeypatch):
    """run_postmarket, the Bhavcopy / delivery fetches and the alert, recorded instead of run."""
    from pipeline import scheduler as S
    import alerts.telegram as T
    import data.bhavcopy as B
    got = {"post": [], "bhav": [], "delivery": [], "alerts": []}
    monkeypatch.setattr(S, "run_postmarket", lambda force=False, target_date=None, backfill=False:
                        got["post"].append((target_date, backfill)))
    monkeypatch.setattr(B, "run_bhavcopy_pipeline",
                        lambda d: got["bhav"].append(d) or {"status": "SUCCESS", "rows": 2})
    monkeypatch.setattr(B, "run_delivery_pipeline", lambda d=None, lookback=5:
                        got["delivery"].append((d, lookback)) or {"status": "SUCCESS", "rows": 2})
    monkeypatch.setattr(T, "notify", lambda message, **k: got["alerts"].append(message) or
                        {"sent": False, "recorded": True})
    monkeypatch.setattr(S, "release_held_signals", lambda now=None: None)
    monkeypatch.setattr(S, "postmarket_target_date", lambda: D(2026, 10, 26))
    _FrozenNow.frozen = dt.datetime(2026, 10, 26, 20, 5)
    monkeypatch.setattr(S, "datetime", _FrozenNow)
    return got


def test_holes_are_found_between_stored_sessions_only(db):
    from pipeline.scheduler import _price_holes
    for d in (D(2026, 10, 9), D(2026, 10, 23), D(2026, 10, 26)):
        _bar(db, d)
    assert _price_holes(db, D(2026, 10, 26)) == [D(2026, 10, 12), D(2026, 10, 13), D(2026, 10, 14),
                                                 D(2026, 10, 15), D(2026, 10, 16), D(2026, 10, 19),
                                                 D(2026, 10, 21), D(2026, 10, 22)]   # 20th: Dussehra


def test_a_hole_in_the_window_is_rescored_and_older_ones_get_their_bars(db, calls):
    from pipeline import scheduler as S
    for d in (D(2026, 10, 9), D(2026, 10, 23), D(2026, 10, 26)):
        _bar(db, d)
        _score(db, d)
    S.run_postmarket_if_missing()
    # the five-session window: 19, 21, 22 have no bars but the 23rd does -> re-scored, oldest first
    assert calls["post"] == [(D(2026, 10, 19), True), (D(2026, 10, 21), True), (D(2026, 10, 22), True)]
    # older holes: bars and delivery only, no re-score
    assert calls["bhav"] == [D(2026, 10, 12), D(2026, 10, 13), D(2026, 10, 14), D(2026, 10, 15),
                             D(2026, 10, 16)]
    assert calls["delivery"] == [(D(2026, 10, 16), S.GAP_SCAN_SESSIONS)]
    assert len(calls["alerts"]) == 1 and "8 session(s)" in calls["alerts"][0]


def test_a_fresh_install_does_not_fan_out(db, calls):
    from pipeline import scheduler as S
    S.run_postmarket_if_missing()
    assert calls["post"] == [(D(2026, 10, 26), False)]
    assert calls["bhav"] == [] and calls["delivery"] == [] and calls["alerts"] == []


def test_a_complete_history_stays_a_noop(db, calls):
    from pipeline import scheduler as S
    for d in S._recent_sessions(S.GAP_SCAN_SESSIONS, D(2026, 10, 26)):
        _bar(db, d)
        _score(db, d)
    S.run_postmarket_if_missing()
    assert calls == {"post": [], "bhav": [], "delivery": [], "alerts": []}
