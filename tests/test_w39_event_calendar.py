"""
W39 Phase 2 (item 8): the macro event calendar -- FOMC, US CPI, US payrolls and RBI dates, the NSE session
each one hits (US releases after India's close hit the next open; the RBI decides during the session),
the gap estimate's band widened after a US release, measured once enough mornings are stored, and the
market pulse / API / page.
"""
import datetime as dt

import pytest

from research import event_calendar as EC

D = dt.date


# ── time zones and sessions ───────────────────────────────────────────────

def test_us_releases_in_india_time_follow_us_daylight_saving():
    assert EC.us_dst(D(2026, 10, 14)) and not EC.us_dst(D(2026, 12, 9))
    assert not EC.us_dst(D(2026, 3, 7)) and EC.us_dst(D(2026, 3, 8)), "second Sunday of March"
    assert EC.us_dst(D(2026, 10, 31)) and not EC.us_dst(D(2026, 11, 1)), "first Sunday of November"
    assert EC.ist("2026-10-14", "US_CPI") == dt.datetime(2026, 10, 14, 18, 0)
    assert EC.ist("2026-01-13", "US_CPI") == dt.datetime(2026, 1, 13, 19, 0)
    assert EC.ist("2026-10-28", "FOMC") == dt.datetime(2026, 10, 28, 23, 30)
    assert EC.ist("2026-12-09", "FOMC") == dt.datetime(2026, 12, 10, 0, 30), "after midnight in India in winter"
    assert EC.ist("2026-10-07", "RBI_POLICY") == dt.datetime(2026, 10, 7, 10, 0)


@pytest.mark.parametrize("day,kind,session,timing", [
    ("2026-10-14", "US_CPI", "2026-10-15", "before_open"),      # Wednesday evening -> Thursday's open
    ("2026-10-02", "US_NFP", "2026-10-05", "before_open"),      # Friday (an NSE holiday too) -> Monday
    ("2026-11-06", "US_NFP", "2026-11-09", "before_open"),
    ("2026-10-28", "FOMC", "2026-10-29", "before_open"),
    ("2026-12-09", "FOMC", "2026-12-10", "before_open"),        # 00:30 IST on the session day itself
    ("2026-10-07", "RBI_POLICY", "2026-10-07", "intraday"),
    ("2026-11-10", "US_CPI", "2026-11-11", "before_open"),      # 11-10 is an NSE holiday; the release is after it
])
def test_the_session_each_event_hits(day, kind, session, timing):
    assert EC.exposure(day, kind) == (D.fromisoformat(session), timing)


# ── the stored calendar ───────────────────────────────────────────────────

@pytest.fixture
def db(temp_db):
    from db.schema import get_connection, init_db
    from research import market_pulse as MP
    init_db()
    conn = get_connection()
    MP.ensure_tables(conn)
    yield conn
    conn.close()


def test_seeded_dates_and_the_sessions_they_hit(db):
    ev = EC.for_session(db, "2026-10-15")
    assert [(e["kind"], e["event_date"], e["time_ist"], e["widens_gap"]) for e in ev] == [
        ("US_CPI", "2026-10-14", "18:00", True)]
    rbi = EC.for_session(db, "2026-10-07")
    assert [e["kind"] for e in rbi] == ["RBI_POLICY"] and rbi[0]["widens_gap"] is False
    assert EC.for_session(db, "2026-10-13") == []
    winter = EC.for_session(db, "2026-12-10")
    assert winter[0]["kind"] == "FOMC" and winter[0]["time_ist"] == "00:30 (next day)"
    up = EC.upcoming(db, "2026-10-07", days=10)
    assert [e["kind"] for e in up] == ["RBI_POLICY", "US_CPI"], "today's RBI and next week's CPI"
    assert all(len(v) == len(set(v)) for v in EC.SEED.values())


def test_config_events_add_and_soft_delete(db, monkeypatch):
    EC._SEEDED.clear()
    db.execute("DELETE FROM macro_event")
    monkeypatch.setattr(EC, "settings", lambda: dict(EC.DEFAULTS, events=[
        {"date": "2026-11-03", "kind": "fomc", "title": "Unscheduled Fed meeting"},
        {"date": "2026-11-04", "kind": "ECB"}, {"date": "not a date", "kind": "FOMC"}]))
    EC.ensure_tables(db)
    ev = EC.events(db, "2026-11-01", "2026-11-05")
    assert [(e["kind"], e["label"], e["source"]) for e in ev] == [("FOMC", "Unscheduled Fed meeting", "config")]
    added = EC.add_event(db, "2027-02-10", "us_cpi", "US CPI (January)")
    assert added["session"] == "2027-02-11" and added["source"] == "manual"
    with pytest.raises(ValueError):
        EC.add_event(db, "2027-02-10", "ECB")
    with pytest.raises(ValueError):
        EC.add_event(db, None, "FOMC")
    assert EC.delete_event(db, "2026-10-14", "US_CPI")["deleted"] == 1
    EC._SEEDED.clear()
    EC.ensure_tables(db)
    assert EC.for_session(db, "2026-10-15") == [], "a deleted seeded date does not come back"
    with pytest.raises(LookupError):
        EC.delete_event(db, "2026-10-14", "US_CPI")


# ── the band ──────────────────────────────────────────────────────────────

def _morning(conn, day, exp, act, events):
    conn.execute("INSERT INTO market_cue (date, captured_at, expected_gap_pct, actual_gap_pct, events) VALUES "
                 "(?, ?, ?, ?, ?)", (str(day), f"{day} 09:05:00", exp, act, events))


def test_the_band_is_the_usual_miss_widened_after_a_us_release(db):
    start = D(2025, 1, 6)
    out = EC.gap_band(db, "2026-10-15")
    assert out["band_pct"] is None and "10 evaluated mornings" in out["note"]
    for i in range(12):
        _morning(db, start + dt.timedelta(days=i), 0.5, 0.3, "")                 # missed by 0.2
    db.commit()
    band = EC.gap_band(db, "2026-10-15")
    assert band["typical_miss_pct"] == pytest.approx(0.2) and band["widen"] == 1.5
    assert band["band_pct"] == pytest.approx(0.3) and "assumed" in band["widen_basis"]
    quiet = EC.gap_band(db, "2026-10-13")
    assert quiet["widen"] == 1.0 and quiet["band_pct"] == pytest.approx(0.2)
    rbi = EC.gap_band(db, "2026-10-07")
    assert rbi["widen"] == 1.0, "the RBI decides after the open"
    for i in range(30):
        _morning(db, start + dt.timedelta(days=100 + i), 0.5, 0.3, "RBI_POLICY")   # intraday: an ordinary morning
    for i in range(10):
        _morning(db, start + dt.timedelta(days=200 + i), 0.5, 0.0, "US_CPI")       # missed by 0.5
    db.commit()
    w = EC.widen_factor(db)
    assert w == {"factor": 2.5, "basis": "measured", "event_mornings": 10, "other_mornings": 42}
    assert EC.gap_band(db, "2026-10-15")["band_pct"] == pytest.approx(0.5)


def test_mornings_stored_before_the_calendar_are_looked_up(db):
    _morning(db, "2026-10-15", 0.4, 0.1, None)              # the CPI morning, events column empty
    _morning(db, "2026-10-13", 0.4, 0.3, None)
    db.commit()
    ev_m, no_m = EC._misses(db)
    assert ev_m == [pytest.approx(0.3)] and no_m == [pytest.approx(0.1)]


# ── market pulse, API, page ───────────────────────────────────────────────

def test_capture_stores_the_events_and_band_and_pulse_explains_them(db, monkeypatch):
    from research import market_pulse as MP
    cpi = {"event_date": "2026-10-14", "kind": "US_CPI", "label": "US CPI inflation", "source": "seed",
           "time_ist": "18:00", "session": str(D.today()), "timing": "before_open", "widens_gap": True}
    monkeypatch.setattr(EC, "gap_band", lambda conn, session=None, cfg=None: {
        "events": [cpi], "typical_miss_pct": 0.2, "widen": 1.5, "widen_basis": "assumed", "band_pct": 0.3,
        "mornings": 12, "note": None})
    MP.capture_gift(db, quotes={"gift_nifty": {"ltp": 24100.0}, "nifty50": {"prev_close": 24000.0}})
    row = db.execute("SELECT events, band_pct FROM market_cue").fetchone()
    assert row[0] == "US_CPI" and row[1] == pytest.approx(0.3)
    p = MP.pulse(db)
    assert p["events"]["today"][0]["kind"] == "US_CPI"
    assert any("US CPI inflation came out overnight" in r and "×1.5" in r for r in p["reasons"])
    assert "by_events" in p["gap_record"]


def test_old_market_cue_tables_get_the_new_columns(temp_db):
    import sqlite3
    import db.schema as S
    from research import market_pulse as MP
    raw = sqlite3.connect(S.DB_PATH)
    raw.execute(MP.DDL[0])
    raw.commit()
    MP.ensure_tables(raw)
    cols = {r[1] for r in raw.execute("PRAGMA table_info(market_cue)")}
    raw.close()
    assert {"events", "band_pct"} <= cols


def test_events_api_and_page(tmp_path, monkeypatch, db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    client = TestClient(server.app)
    h = {security.TOKEN_HEADER: security.token()}
    o = client.get("/api/market-pulse/events", params={"days": 400}).json()
    assert set(o["kinds"]) == set(EC.KINDS) and "band_pct" in o["today"] and o["upcoming"]
    assert client.post("/api/market-pulse/events", json={"date": "2027-03-10", "kind": "US_CPI"}).status_code == 401
    r = client.post("/api/market-pulse/events", headers=h, json={"date": "2027-03-10", "kind": "US_CPI"})
    assert r.status_code == 200 and r.json()["session"] == "2027-03-11"
    assert client.post("/api/market-pulse/events", headers=h, json={"date": "2027-03-10", "kind": "X"}).status_code == 400
    assert client.post("/api/market-pulse/events/delete", headers=h,
                       json={"date": "2027-03-10", "kind": "US_CPI"}).status_code == 200
    assert client.post("/api/market-pulse/events/delete", headers=h,
                       json={"date": "2027-03-10", "kind": "US_CPI"}).status_code == 404
    assert "Event calendar" in client.get("/market-pulse").text


def test_permissions_and_classification():
    from enterprise.authz import permission_for
    from enterprise.privacy import classify
    from enterprise.scoping import TABLES
    assert permission_for("POST", "/api/market-pulse/events") == "research:run"
    assert permission_for("GET", "/api/market-pulse/events") == "dashboard:read"
    assert classify("macro_event") is not None and TABLES["macro_event"] == "GLOBAL"
