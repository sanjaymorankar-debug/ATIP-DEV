"""
W39 Phase 2 (item 2): the track record by regime and horizon -- each signal's return 5, 20 and 60
sessions on, against the Nifty and signed for its direction; split by the market gate it was born
under and by confluence; today's signals carry their scan's record. Constructed prices with a known
answer.
"""
import datetime as dt

import pandas as pd
import pytest


@pytest.fixture
def db(temp_db):
    from db.schema import get_connection, init_db
    from research import regime_gate as R
    from research import tech_signals as S
    init_db()
    conn = get_connection()
    S.ensure_tables(conn)
    R.ensure_tables(conn)
    yield conn
    conn.close()


D0 = dt.date(2026, 6, 1)


def _bars(conn, sym, closes, start=D0):
    days = pd.bdate_range(start, periods=len(closes))
    for d, c in zip(days, closes):
        conn.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) VALUES (?,?,?,?,?,?,1,"
                     "'dhan')", (sym, str(d.date()), c, c, c, c))
    conn.commit()
    return days


def _nifty(conn, closes, start=D0):
    for d, c in zip(pd.bdate_range(start, periods=len(closes)), closes):
        conn.execute("INSERT INTO market_health (date, nifty_close) VALUES (?, ?)", (str(d.date()), c))
    conn.commit()


def _signal(conn, sid, sym, d, direction, entry, gate=None, confluence=2, scan="golden_cross", name="Golden cross", **vals):
    cols = ["signal_id", "symbol", "date", "scan", "name", "direction", "entry", "confluence", "status", "market_gate"]
    args = [sid, sym, str(d), scan, name, direction, entry, confluence, "OPEN", gate]
    for k, v in vals.items():
        cols.append(k)
        args.append(v)
    conn.execute(f"INSERT INTO technical_signal ({', '.join(cols)}) VALUES ({','.join('?' * len(cols))})", args)
    conn.commit()


def test_forward_returns_are_signed_and_measured_against_the_nifty(db):
    from research import tech_signals as S
    # the stock: 100 on the signal day, then +1 a session; the Nifty: 20,000, then +0.1% a session
    days = _bars(db, "UP", [100.0 + k for k in range(31)])
    _nifty(db, [20000 * 1.001 ** k for k in range(80)], start=D0 - dt.timedelta(days=60))
    d0 = days[0].date()
    _signal(db, "long", "UP", d0, "BULL", 100.0)
    _signal(db, "short", "UP", d0, "BEAR", 100.0, scan="death_cross", name="Death cross")
    out = S.evaluate_forward(db)
    assert out["updated"] == 2
    got = {r[0]: r[1:] for r in db.execute("SELECT signal_id, ret_5d, excess_5d, ret_20d, excess_20d, ret_60d, "
                                           "excess_60d FROM technical_signal")}
    n5 = 1.001 ** 5 - 1                                                    # same sessions for stock and Nifty
    assert got["long"][0] == pytest.approx(5.0) and got["long"][1] == pytest.approx((0.05 - n5) * 100, abs=1e-3)
    assert got["short"][0] == pytest.approx(-5.0) and got["short"][1] == pytest.approx(-(0.05 - n5) * 100, abs=1e-3)
    assert got["long"][2] == pytest.approx(20.0)
    assert got["long"][4] is None and got["long"][5] is None, "only 30 sessions have passed"


def test_forward_fills_in_as_sessions_pass_and_never_rewrites(db):
    from research import tech_signals as S
    days = _bars(db, "UP", [100.0 + k for k in range(21)])
    _nifty(db, [20000.0] * 120, start=D0 - dt.timedelta(days=60))
    _signal(db, "s", "UP", days[0].date(), "BULL", 100.0)
    S.evaluate_forward(db)
    assert tuple(db.execute("SELECT ret_20d, ret_60d FROM technical_signal").fetchone()) == (pytest.approx(20.0), None)
    db.execute("UPDATE technical_signal SET ret_5d=99")                    # a stored value stays as stored
    _bars(db, "UP", [130.0 + k for k in range(45)], start=(days[-1] + pd.offsets.BDay(1)).date())
    S.evaluate_forward(db)
    r5, r60, x60 = tuple(db.execute("SELECT ret_5d, ret_60d, excess_60d FROM technical_signal").fetchone())
    assert r5 == 99 and r60 is not None and x60 == pytest.approx(r60), "a flat Nifty: excess = return"
    assert S.evaluate_forward(db) == {"updated": 0, "pending": 0}


def test_forward_stats_split_by_gate_and_confluence(db):
    from research import tech_signals as S
    for i, x in enumerate([3.0, 1.0, -2.0, 4.0]):
        _signal(db, f"o{i}", f"O{i}", D0, "BULL", 100, gate="OPEN", confluence=4, excess_20d=x)
    for i, x in enumerate([-1.0, -3.0, 2.0]):
        _signal(db, f"c{i}", f"C{i}", D0, "BULL", 100, gate="CLOSED", confluence=1, excess_20d=x)
    _signal(db, "u", "U", D0, "BULL", 100, gate=None, confluence=2, excess_20d=5.0)
    _signal(db, "pending", "P", D0, "BULL", 100, gate="OPEN", confluence=4)
    f = S.forward_stats(db, 20)
    gc = f["by_scan"][0]["cells"]
    assert gc["OPEN"] == {"n": 4, "beat_nifty_pct": 75.0, "median_excess_pct": 2.0, "mean_excess_pct": 1.5,
                          "enough": False}
    assert gc["CLOSED"]["median_excess_pct"] == -1.0 and gc["UNKNOWN"]["n"] == 1 and gc["ALL"]["n"] == 8
    bands = {b["band"]: b["cells"] for b in f["by_confluence"]}
    assert bands["4-6"]["ALL"]["n"] == 4 and bands["0-1"]["CLOSED"]["n"] == 3 and bands["2-3"]["ALL"]["n"] == 1
    assert f["overall"]["OPEN"]["n"] == 4
    assert S.forward_stats(db, 20, min_confluence=4)["overall"]["ALL"]["n"] == 4
    assert S.forward_stats(db, 5)["by_scan"] == []
    with pytest.raises(ValueError):
        S.forward_stats(db, 7)


def test_todays_signals_carry_the_scans_record_in_todays_market(db):
    from research import tech_signals as S
    for i in range(12):
        _signal(db, f"h{i}", "OLD", D0 - dt.timedelta(days=i + 40), "BULL", 100, gate="OPEN", excess_20d=1.0 + i)
    _signal(db, "today", "NEW", D0, "BULL", 100, gate="OPEN")
    _signal(db, "today2", "NEW", D0, "BULL", 100, gate="CLOSED", scan="death_cross", name="Death cross")
    sig = {s["scan"]: s for s in S.todays_signals(db, str(D0))}
    g = sig["golden_cross"]
    assert g["record_scope"] == "gate" and g["record"]["n"] == 12 and g["record"]["enough"]
    assert g["record"]["beat_nifty_pct"] == 100.0 and g["record_horizon"] == 20
    assert sig["death_cross"]["record"] is None
    assert "record" not in S.todays_signals(db, str(D0), record_horizon=None)[0]


def test_the_gate_and_status_at_birth_are_filled_for_older_signals(db):
    from research import regime_gate as R
    from research import tech_signals as S
    days = pd.bdate_range(end=dt.date(2026, 10, 6), periods=260)
    for d, c in zip(days, [20000 + 10 * k for k in range(260)]):
        db.execute("INSERT INTO global_market_history (series, date, close) VALUES ('nifty50',?,?)", (str(d.date()), c))
    db.commit()
    R.update(db, days[-1].date())
    _signal(db, "old", "X", days[-3].date(), "BULL", 100)
    assert S.backfill_gate(db) == 1
    assert tuple(db.execute("SELECT market_gate, alignment, market_status FROM technical_signal").fetchone()) == \
        ("OPEN", "WITH", "CONFIRMED_UPTREND")


def test_forward_api(tmp_path, monkeypatch, db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    client = TestClient(server.app)
    _signal(db, "o", "A", D0, "BULL", 100, gate="OPEN", excess_20d=2.0)
    r = client.get("/api/signals/technical/forward", params={"horizon": 20})
    assert r.status_code == 200 and r.json()["by_scan"][0]["cells"]["OPEN"]["n"] == 1
    assert client.get("/api/signals/technical/forward", params={"horizon": 7}).status_code == 400
    page = client.get("/signals").text
    assert "Against the Nifty, by market and holding period" in page and "Does confluence add?" in page
