"""
W39 Phase 3 (item 1): intraday scans on stored 15-minute bars -- opening-range breakout, open = low /
high, the intraday squeeze -- stored once a day, measured from the price ATIP saw them at, evaluated at
the close against the Nifty, alerts held until a positive 30-hit record; plus the fix that keeps 1-minute
bars out of the existing 15-minute scanner.
"""
import datetime as dt

import pytest

from research import intraday_signals as I

DAY = dt.date(2026, 10, 6)                      # Tuesday; the sessions before: 10-05, 10-01, 09-30 (10-02 a holiday)
PRIOR = [dt.date(2026, 9, 30), dt.date(2026, 10, 1), dt.date(2026, 10, 5)]


def _slots(day, n):
    t0 = dt.datetime.combine(day, dt.time(9, 15))
    return [t0 + dt.timedelta(minutes=15 * i) for i in range(n)]


def _frame(sessions):
    """sessions: [(day, [(o, h, l, c, v), ...])] -> the DataFrame load_bars returns."""
    import pandas as pd
    idx, rows = [], []
    for day, bars in sessions:
        for t, b in zip(_slots(day, len(bars)), bars):
            idx.append(t)
            rows.append(b)
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx), columns=["open", "high", "low", "close", "volume"]).astype(float)


FLAT = [(100.0, 100.5, 99.5, 100.0, 1e4)] * 25


def _hits(df, **cfg):
    return {h["scan"]: h for h in I.scan_symbol(df, DAY, dict(I.DEFAULTS, **cfg))}


# ── the scans ─────────────────────────────────────────────────────────────

def test_opening_range_breakout_needs_volume_and_an_early_first_close_beyond_it():
    hist = [(d, FLAT) for d in PRIOR]
    today = [(100.5, 101.0, 100.0, 100.6, 3e4), (100.6, 100.9, 100.3, 100.8, 1e4), (100.8, 101.6, 100.7, 101.4, 2e4),
             (101.4, 101.8, 101.2, 101.7, 1e4)]
    h = _hits(_frame(hist + [(DAY, today)]))
    assert "orb_up" in h and "orb_down" not in h
    assert h["orb_up"]["bar_ts"] == dt.datetime(2026, 10, 6, 9, 45) and h["orb_up"]["level"] == 101.0
    assert h["orb_up"]["rvol_slot"] == 2.0, "20k against 10k at 09:45 in the three sessions before"
    assert "above the 15-minute opening range high 101.00 at 09:45 on 2.0x" in h["orb_up"]["reason"]
    assert _hits(_frame(hist + [(DAY, today)]), or_minutes=30)["orb_up"]["bar_ts"] == dt.datetime(2026, 10, 6, 9, 45)
    quiet = today[:2] + [(100.8, 101.6, 100.7, 101.4, 1.2e4)] + today[3:]
    assert "orb_up" not in _hits(_frame(hist + [(DAY, quiet)])), "1.2x the usual volume"
    assert "orb_up" not in _hits(_frame([(DAY, today)])), "no volume history yet"
    late = today[:2] + [(100.8, 100.95, 100.5, 100.9, 1e4)] * 8 + [(100.9, 101.6, 100.8, 101.4, 2e4)]
    assert "orb_up" not in _hits(_frame(hist + [(DAY, late)])), "first close beyond the range at 11:45"
    down = [(100.5, 101.0, 100.0, 100.2, 3e4), (100.2, 100.4, 99.6, 99.8, 2e4)]
    assert "orb_down" in _hits(_frame(hist + [(DAY, down)]))
    assert _hits(_frame(hist + [(DAY, today[:1])])) == {}, "nothing after the range yet"


def test_open_equals_low_and_high_on_the_first_hour():
    up = [(100.0, 100.6, 99.95, 100.5, 1e4), (100.5, 101.2, 100.4, 101.0, 1e4), (101.0, 101.3, 100.8, 101.1, 1e4),
          (101.1, 101.4, 100.9, 101.2, 1e4)]
    h = _hits(_frame([(DAY, up)]))
    assert "open_low" in h and h["open_low"]["level"] == 100.0 and "open_high" not in h
    assert h["open_low"]["bar_ts"] == dt.datetime(2026, 10, 6, 10, 0), "the hour's last bar (done 10:15)"
    assert h["open_low"]["reason"].endswith("101.20 at 10:15")
    broke = up[:2] + [(101.0, 101.1, 99.7, 100.4, 1e4), up[3]]
    assert "open_low" not in _hits(_frame([(DAY, broke)])), "traded 0.3 % below the open"
    flat = up[:3] + [(101.1, 101.2, 100.2, 100.3, 1e4)]
    assert "open_low" not in _hits(_frame([(DAY, flat)])), "only 0.3 % above the open after an hour"
    dn = [(100.0, 100.05, 99.4, 99.6, 1e4), (99.6, 99.8, 99.0, 99.1, 1e4), (99.1, 99.3, 98.9, 99.0, 1e4),
          (99.0, 99.2, 98.7, 98.9, 1e4)]
    assert "open_high" in _hits(_frame([(DAY, dn)]))
    assert "open_low" not in _hits(_frame([(DAY, up[:3])])), "needs the whole first hour"


def _tight(n):
    return [(100.0, 100.5, 99.5, 100.0 + (0.05 if i % 2 else -0.05), 1e4) for i in range(n)]


def test_intraday_squeeze_fires_on_release_after_six_tight_bars():
    hist = [(d, _tight(25)) for d in PRIOR]
    today = _tight(6) + [(100.0, 106.5, 99.9, 106.0, 5e4)]
    h = _hits(_frame(hist + [(DAY, today)]))
    assert "squeeze_up" in h and h["squeeze_up"]["bar_ts"] == dt.datetime(2026, 10, 6, 10, 45)
    assert "after 6+ bars of squeeze" in h["squeeze_up"]["reason"]
    down = _tight(6) + [(100.0, 100.1, 93.5, 94.0, 5e4)]
    assert "squeeze_down" in _hits(_frame(hist + [(DAY, down)]))
    assert "squeeze_up" not in _hits(_frame([(DAY, today)])), "needs 27+ bars of history"


# ── storage, record, alerts ───────────────────────────────────────────────

@pytest.fixture
def db(temp_db):
    from db.schema import get_connection, init_db
    init_db()
    conn = get_connection()
    I.ensure_tables(conn)
    yield conn
    conn.close()


def _store(conn, sym, day, bars, interval=15):
    step = dt.timedelta(minutes=interval)
    t = dt.datetime.combine(day, dt.time(9, 15))
    for o, h, lo, c, v in bars:
        conn.execute("INSERT INTO intraday_bars (symbol, ts, interval_min, open, high, low, close, volume) VALUES "
                     "(?,?,?,?,?,?,?,?)", (sym, t.strftime("%Y-%m-%d %H:%M:%S"), interval, o, h, lo, c, v))
        t += step


ORB_DAY = [(100.5, 101.0, 100.0, 100.6, 3e4), (100.6, 100.9, 100.3, 100.8, 1e4), (100.8, 101.6, 100.7, 101.4, 2e4),
           (101.4, 101.8, 101.2, 101.7, 1e4)] + [(101.7, 102.4, 101.5, 102.0, 1e4)] * 21


def test_load_bars_keeps_completed_15_minute_bars_only(db):
    _store(db, "ACME", DAY, ORB_DAY)
    _store(db, "ACME", DAY, [(1, 1, 1, 1, 1)] * 30, interval=1)
    db.commit()
    b = I.load_bars(db, DAY, dt.datetime(2026, 10, 6, 10, 0))
    assert list(b) == ["ACME"] and len(b["ACME"]) == 3, "09:15, 09:30, 09:45 are done at 10:00; 1-minute bars ignored"
    _history(db, "ACME")
    db.commit()
    assert len(I.load_bars(db, DAY, dt.datetime(2026, 10, 6, 10, 0), sessions=4)["ACME"]) == 3 + 3 * 25


def _history(conn, sym):
    for d in PRIOR:
        _store(conn, sym, d, FLAT)


def test_run_stores_once_measures_from_what_was_seen_and_evaluates_at_the_close(db, monkeypatch):
    monkeypatch.setattr(I, "settings", lambda: dict(I.DEFAULTS, alerts=False))
    _history(db, "ACME")
    _store(db, "ACME", DAY, ORB_DAY)
    for hhmm, v in (("10:00", 25000.0), ("15:30", 25125.0)):
        db.execute("INSERT INTO index_levels (date, time, nifty50) VALUES (?, ?, ?)", (str(DAY), hhmm, v))
    db.commit()
    out = I.run(db, now=dt.datetime(2026, 10, 6, 10, 1))
    assert out["new"] == 1 and out["evaluated"] == 0, "orb_up only (it traded below its open); the session is not over"
    row = db.execute("SELECT bar_ts, price, seen_at, seen_price FROM intraday_signal WHERE scan='orb_up'").fetchone()
    assert str(row[0])[:19] == "2026-10-06 09:45:00" and row[1] == 101.4
    assert str(row[2])[:19] == "2026-10-06 10:00:00" and row[3] == 101.4
    assert I.run(db, now=dt.datetime(2026, 10, 6, 10, 31))["new"] == 0, "once a day per scan"
    out = I.run(db, now=dt.datetime(2026, 10, 6, 15, 50))
    assert out["evaluated"] == 1
    r = db.execute("SELECT close_price, ret_close_pct, excess_close_pct, mfe_pct, mae_pct FROM intraday_signal "
                   "WHERE scan='orb_up'").fetchone()
    assert r[0] == 102.0 and r[1] == pytest.approx((102 / 101.4 - 1) * 100, abs=1e-3)
    assert r[2] == pytest.approx((102 / 101.4 - 1) * 100 - 0.5, abs=1e-3), "minus the Nifty's 0.5 %"
    assert r[3] == pytest.approx((102.4 / 101.4 - 1) * 100, abs=1e-3) and r[4] == pytest.approx((101.2 / 101.4 - 1) * 100, abs=1e-3)
    st = {s["scan"]: s for s in I.stats(db)}
    assert st["orb_up"]["closed"] == 1 and st["orb_up"]["alerts"] == "held" and not st["orb_up"]["enough"]


def test_a_short_gains_when_the_stock_falls(db, monkeypatch):
    monkeypatch.setattr(I, "settings", lambda: dict(I.DEFAULTS, alerts=False))
    _history(db, "DOWN")
    _store(db, "DOWN", DAY, [(100.5, 101.0, 100.0, 100.2, 3e4), (100.2, 100.4, 99.6, 99.8, 2e4)] +
           [(99.8, 99.9, 98.8, 99.0, 1e4)] * 23)
    db.commit()
    I.run(db, now=dt.datetime(2026, 10, 6, 9, 46))
    I.evaluate(db, now=dt.datetime(2026, 10, 6, 16, 0))
    r = db.execute("SELECT ret_close_pct, excess_close_pct FROM intraday_signal WHERE scan='orb_down'").fetchone()
    assert r[0] == pytest.approx((1 - 99.0 / 99.8) * 100, abs=1e-3) and r[1] is None, "no Nifty readings stored"


def test_alerts_wait_for_a_positive_30_hit_record(db, monkeypatch):
    import alerts.telegram as TG
    sent = []
    monkeypatch.setattr(TG, "notify", lambda text, **k: sent.append(text))
    new = [{"scan": "orb_up", "symbol": "ACME", "direction": "BULL", "reason": "r", "signal_id": "ACME:x:orb_up"}]
    assert I.alert(db, new) == 0 and not sent
    for i in range(30):
        db.execute("INSERT INTO intraday_signal (signal_id, symbol, date, scan, direction, seen_price, ret_close_pct, "
                   "excess_close_pct) VALUES (?, 'X', '2026-09-01', 'orb_up', 'BULL', 100, 0.5, ?)",
                   (f"old{i}", 0.4 if i % 3 else -0.2))
    db.commit()
    assert I.proven_scans(db) == {"orb_up"}
    assert I.alert(db, new) == 1 and "Opening-range breakout" in sent[-1]
    assert {s["scan"]: s for s in I.stats(db)}["orb_up"]["alerts"] == "on"


def test_the_existing_scanner_no_longer_mixes_1_minute_bars(db):
    from strategy import intraday_scan as IS
    _store(db, "ACME", DAY, ORB_DAY[:4])
    _store(db, "ACME", DAY, [(1, 1, 1, 1, 1)] * 30, interval=1)
    db.commit()
    bars = IS.load_bars(db, DAY, dt.datetime(2026, 10, 6, 11, 0))
    assert len(bars["ACME"]) == 4 and all(b[3] > 50 for b in bars["ACME"])


# ── API and page ──────────────────────────────────────────────────────────

def test_intraday_api_and_page(tmp_path, monkeypatch, db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    client = TestClient(server.app)
    h = {security.TOKEN_HEADER: security.token()}
    _history(db, "ACME")
    _store(db, "ACME", DAY, ORB_DAY)
    db.commit()
    I.run(db, now=dt.datetime(2026, 10, 6, 10, 1))
    rows = client.get("/api/signals/intraday", params={"date": str(DAY)}).json()
    assert [r["scan"] for r in rows] == ["orb_up"] and rows[0]["alerts"] == "held"
    assert len(client.get("/api/signals/intraday/stats").json()) == len(I.SCANS)
    assert client.get("/api/signals/intraday", params={"date": "bad"}).status_code == 400
    assert client.post("/api/signals/intraday/run").status_code == 401
    assert client.post("/api/signals/intraday/run", headers=h).status_code == 200
    assert "Intraday" in client.get("/signals").text


def test_permissions_and_classification():
    from enterprise.authz import permission_for
    from enterprise.privacy import classify
    from enterprise.scoping import TABLES
    assert permission_for("POST", "/api/signals/intraday/run") == "research:run"
    assert classify("intraday_signal") is not None and TABLES["intraday_signal"] == "GLOBAL"
