"""
W39 Phase 3 (item 3): the global-cue model on synchronised moves (15:30 -> 08:45 IST) -- snapshots, the
morning moves and the Nifty's opening gap, a planted sensitivity recovered walk-forward and compared with
"no change" and the GIFT estimate, today's estimate stored beside GIFT's, and the API. Yahoo is never
called: the fetcher is injected.
"""
import datetime as dt
import math

import numpy as np
import pytest

from research import global_sync as G


@pytest.fixture
def db(temp_db):
    from db.schema import get_connection, init_db
    from research import market_pulse as MP
    init_db()
    conn = get_connection()
    G.ensure_tables(conn)
    MP.ensure_tables(conn)
    yield conn
    conn.close()


def _sessions(n, end=None):
    from utils.trading_calendar import is_trading_day
    d, out = end or dt.date.today(), []
    while len(out) < n:
        if is_trading_day(d):
            out.append(d)
        d -= dt.timedelta(days=1)
    return out[::-1]


def _seed(conn, days, beta=0.6, seed=4, gift_noise=0.3, today_es=None):
    """Snapshots on every session, an ES move each morning, and a Nifty gap = beta x that move + noise."""
    rng = np.random.default_rng(seed)
    es, nifty = 5000.0, 20000.0
    for i, d in enumerate(days):
        others = {k: 100.0 + i * 0.01 for k in G.SERIES if k != "es"}
        if i:
            move = float(rng.normal(0, 0.6)) if (today_es is None or i < len(days) - 1) else today_es
            es_pre = es * math.exp(move / 100)
            for k, p in dict(others, es=es_pre).items():
                conn.execute("INSERT INTO global_snapshot (date, label, series, price) VALUES (?, 'pre', ?, ?)",
                             (str(d), k, p))
            gap = beta * move + float(rng.normal(0, 0.1))
            opn = nifty * (1 + gap / 100)
            if i < len(days) - 1:                                    # today has not opened in the data
                conn.execute("INSERT INTO index_levels (date, time, nifty50) VALUES (?, '09:15', ?)", (str(d), opn))
                conn.execute("INSERT INTO market_cue (date, captured_at, expected_gap_pct) VALUES (?, ?, ?)",
                             (str(d), f"{d} 09:05:00", gap + float(rng.normal(0, gift_noise))))
            es, nifty = es_pre, opn
        nifty *= 1.001
        conn.execute("INSERT INTO index_levels (date, time, nifty50) VALUES (?, '15:30', ?)", (str(d), nifty))
        for k, p in dict(others, es=es).items():
            conn.execute("INSERT INTO global_snapshot (date, label, series, price) VALUES (?, 'close', ?, ?)",
                         (str(d), k, p))
    conn.commit()


def test_capture_stores_each_series_and_survives_a_failed_fetch(db):
    now = dt.datetime(2026, 10, 6, 15, 31)
    out = G.capture(db, "close", fetch=lambda: {"es": (5000.0, "2026-10-06 15:30"), "nikkei": (38000.0, "x")}, now=now)
    assert out == {"status": "SUCCESS", "rows": 2, "label": "close"}
    assert db.execute("SELECT COUNT(*) FROM global_snapshot WHERE date='2026-10-06' AND label='close'").fetchone()[0] == 2

    def boom():
        raise ConnectionError("Yahoo down")
    assert G.capture(db, "pre", fetch=boom, now=now)["status"] == "FAILED"
    with pytest.raises(ValueError):
        G.capture(db, "noon", fetch=lambda: {})


def test_morning_moves_run_from_the_previous_sessions_close(db):
    days = _sessions(3, end=dt.date(2026, 10, 6))                    # 10-01, 10-05, 10-06 (10-02 a holiday)
    assert days[0] == dt.date(2026, 10, 1)
    for k, p in (("es", 5000.0), ("nikkei", 38000.0)):
        db.execute("INSERT INTO global_snapshot (date, label, series, price) VALUES ('2026-10-01', 'close', ?, ?)", (k, p))
    for k, p in (("es", 5050.0), ("nikkei", 37620.0)):
        db.execute("INSERT INTO global_snapshot (date, label, series, price) VALUES ('2026-10-05', 'pre', ?, ?)", (k, p))
    db.commit()
    m = G.moves_for(G._snapshots(db), dt.date(2026, 10, 5))
    assert m["es"] == pytest.approx(math.log(1.01) * 100) and m["nikkei"] == pytest.approx(math.log(0.99) * 100)
    db.execute("INSERT INTO index_levels (date, time, nifty50) VALUES ('2026-10-01', '15:30', 25000)")
    db.execute("INSERT INTO index_levels (date, time, nifty50) VALUES ('2026-10-05', '09:15', 25100)")
    db.commit()
    assert G._gap(db, dt.date(2026, 10, 5)) == pytest.approx(0.4)
    db.execute("INSERT INTO index_levels (date, time, nifty50) VALUES ('2026-10-06', '15:30', 25300)")
    db.commit()
    assert G._gap(db, dt.date(2026, 10, 6)) is None, "no reading by 09:30: the close is not the open"


def test_collecting_until_40_mornings(db):
    _seed(db, _sessions(20))
    out = G.model(db)
    assert out["status"] == "COLLECTING" and out["mornings"] == 18 and "of 40 mornings" in out["reason"]


def test_the_model_recovers_a_planted_sensitivity_and_beats_no_change_and_gift(db):
    days = _sessions(90)
    _seed(db, days, beta=0.6, today_es=1.0)
    out = G.model(db, today=days[-1])
    assert out["status"] == "OK" and out["mornings"] == 88
    wf = out["walk_forward"]
    assert wf["beats_zero"] and wf["rmse_pct"] < 0.2 and wf["beats_gift"], "GIFT here has 0.3 noise"
    assert out["sensitivity"]["S&P 500 futures"] == pytest.approx(0.6, abs=0.1)
    assert out["today"]["expected_gap_pct"] == pytest.approx(0.6, abs=0.15), "ES +1 % overnight"


def test_capture_gift_stores_the_synchronised_estimate(db, monkeypatch):
    from research import market_pulse as MP
    monkeypatch.setattr(G, "model", lambda conn, today=None: {"status": "OK", "today": {"expected_gap_pct": 0.42}})
    MP.capture_gift(db, quotes={"gift_nifty": {"ltp": 24100.0}, "nifty50": {"prev_close": 24000.0}})
    assert db.execute("SELECT sync_expected_pct FROM market_cue").fetchone()[0] == pytest.approx(0.42)
    db.execute("UPDATE market_cue SET actual_gap_pct=0.5")
    db.commit()
    assert MP.gap_record(db)["synchronised"]["mornings"] == 1


def test_api(tmp_path, monkeypatch, db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    from enterprise.authz import permission_for
    from enterprise.privacy import classify
    from enterprise.scoping import TABLES
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(G, "fetch_last", lambda: {"es": (5000.0, "t")})
    client = TestClient(server.app)
    h = {security.TOKEN_HEADER: security.token()}
    assert client.get("/api/market-pulse/global-sync").json()["status"] == "COLLECTING"
    assert client.post("/api/market-pulse/global-sync/capture", json={"label": "pre"}).status_code == 401
    assert client.post("/api/market-pulse/global-sync/capture", headers=h, json={"label": "pre"}).json()["rows"] == 1
    assert client.post("/api/market-pulse/global-sync/capture", headers=h, json={"label": "x"}).status_code == 400
    assert permission_for("POST", "/api/market-pulse/global-sync/capture") == "research:run"
    assert classify("global_snapshot") is not None and TABLES["global_snapshot"] == "GLOBAL"


def test_the_open_check_ignores_a_reading_after_0930(db):
    from research import market_pulse as MP
    db.execute("INSERT INTO market_cue (date, captured_at, nifty_prev_close) VALUES ('2026-10-05', '2026-10-05 09:05:00', "
               "25000)")
    db.execute("INSERT INTO index_levels (date, time, nifty50) VALUES ('2026-10-05', '11:00', 25500)")
    db.commit()
    assert MP.evaluate_gaps(db)["rows"] == 0, "an 11:00 reading is not the open"
    db.execute("INSERT INTO index_levels (date, time, nifty50) VALUES ('2026-10-05', '09:15', 25100)")
    db.commit()
    assert MP.evaluate_gaps(db)["rows"] == 1
    assert db.execute("SELECT actual_gap_pct FROM market_cue").fetchone()[0] == pytest.approx(0.4)
