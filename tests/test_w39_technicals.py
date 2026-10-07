"""
W39 (TA): technical indicators, candlestick patterns, scans, the technical rating, and the
signal engine with levels, confluence and a track record. Series are constructed so the
expected answer is known.
"""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from research import technicals as T


def _frame(closes, opens=None, highs=None, lows=None, vols=None, start="2024-01-01"):
    c = np.asarray(closes, dtype=float)
    o = np.asarray(opens, dtype=float) if opens is not None else np.r_[c[0], c[:-1]]
    h = np.asarray(highs, dtype=float) if highs is not None else np.maximum(o, c) * 1.005
    l = np.asarray(lows, dtype=float) if lows is not None else np.minimum(o, c) * 0.995
    v = np.asarray(vols, dtype=float) if vols is not None else np.full(len(c), 1e5)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": v},
                        index=pd.bdate_range(start, periods=len(c)))


# ── indicators ────────────────────────────────────────────────────────────

def test_rsi_bounds_and_wilder_value():
    up = pd.Series(np.arange(1, 40, dtype=float))
    assert T.rsi(up).iloc[-1] == 100.0
    down = pd.Series(np.arange(40, 1, -1, dtype=float))
    assert T.rsi(down).iloc[-1] == pytest.approx(0.0, abs=1e-9)
    alt = pd.Series([10, 11] * 30, dtype=float)
    assert 45 < T.rsi(alt).iloc[-1] < 55, "equal gains and losses sit near 50"


def test_supertrend_follows_a_trend_reversal():
    closes = list(np.linspace(100, 160, 80)) + list(np.linspace(160, 90, 60))
    _, direction = T.supertrend(_frame(closes))
    assert direction.iloc[75] == 1 and direction.iloc[-1] == -1


def test_atr_on_constant_range():
    df = _frame([100.0] * 40, opens=[100.0] * 40, highs=[101.0] * 40, lows=[99.0] * 40)
    assert T.atr(df).iloc[-1] == pytest.approx(2.0)


# ── scans ─────────────────────────────────────────────────────────────────

def test_golden_cross_fires_on_the_crossing_day_only():
    closes = list(np.linspace(200, 100, 230)) + list(np.linspace(100, 220, 80))
    d = T.indicators(_frame(closes))
    day = next(i for i in range(201, len(d)) if d["sma50"].iloc[i - 1] <= d["sma200"].iloc[i - 1]
               and d["sma50"].iloc[i] > d["sma200"].iloc[i])
    hits = lambda k: [h[0] for h in T.run_scans(d.iloc[:k + 1])]
    assert "golden_cross" in hits(day)
    assert "golden_cross" not in hits(day - 1) and "golden_cross" not in hits(day + 1)


def test_52_week_breakout_needs_volume():
    closes = [100 + np.sin(i / 5) * 3 for i in range(300)] + [115.0]
    quiet = T.indicators(_frame(closes))
    assert "high_52w_breakout" not in [h[0] for h in T.run_scans(quiet)]
    vols = [1e5] * 300 + [4e5]
    loud = T.indicators(_frame(closes, vols=vols))
    hit = [h for h in T.run_scans(loud) if h[0] == "high_52w_breakout"]
    assert hit and hit[0][2] == "BULL"


def test_rsi_oversold_reversal():
    closes = list(np.linspace(100, 60, 40)) + [63.0, 66.0]
    d = T.indicators(_frame([100.0] * 30 + closes))
    found = any("rsi_oversold_turn" in [h[0] for h in T.run_scans(d.iloc[:k])] for k in range(60, len(d) + 1))
    assert found


# ── candlestick patterns ──────────────────────────────────────────────────

def _with_last_bars(bars):
    """A gentle down-trend of 40 bars, then the given (o, h, l, c) bars."""
    base = [(100 - i * 0.5, 100.5 - i * 0.5, 99 - i * 0.5, 99.6 - i * 0.5) for i in range(40)]
    o, h, l, c = zip(*(base + bars))
    return T.indicators(_frame(c, opens=o, highs=h, lows=l))


def test_bullish_engulfing_after_a_decline():
    names = [p[0] for p in T.candles(_with_last_bars([(81.0, 81.2, 79.8, 80.0), (79.6, 82.0, 79.5, 81.8)]))]
    assert "Bullish engulfing" in names


def test_hammer_in_a_down_trend():
    names = [p[0] for p in T.candles(_with_last_bars([(80.0, 80.7, 76.0, 80.6)]))]   # body 0.6, lower shadow 4.0
    assert "Hammer" in names


def test_doji_and_inside_bar():
    pats = dict(T.candles(_with_last_bars([(80.0, 82.0, 78.0, 80.5), (80.2, 81.0, 79.0, 80.25)])))
    assert pats.get("Doji") == "NEUTRAL" and pats.get("Inside bar") == "NEUTRAL"


# ── rating and snapshot ───────────────────────────────────────────────────

def test_rating_is_bounded_and_follows_the_trend():
    upd = T.indicators(_frame(np.linspace(100, 200, 260)))
    s, label, votes = T.rating(upd)
    assert -1 <= s <= 1 and s > 0.3 and label in ("BUY", "STRONG_BUY") and len(votes) >= 8
    dnd = T.indicators(_frame(np.linspace(200, 100, 260)))
    assert T.rating(dnd)[0] < -0.3


def test_snapshot_needs_history_and_flags_every_scan():
    assert T.snapshot(_frame(np.linspace(100, 110, 30))) is None
    s = T.snapshot(_frame(np.linspace(100, 150, 300)))
    assert set(f"scan_{k}" for k in T.SCANS) <= set(s)
    assert s["above_200dma"] == 1 and s["tech_rating_label"]


# ── signal engine ─────────────────────────────────────────────────────────

def test_levels_and_confluence():
    from research import tech_signals as S
    assert S.levels("BULL", 100, 2) == (96.0, 108.0)
    assert S.levels("BEAR", 100, 2) == (104.0, 92.0)
    snap = {"tech_rating": 0.6, "vol_ratio": 2.0, "rs_63_pct": 5.0, "_patterns": [("Hammer", "BULL")]}
    n, ev = S.confluence("BULL", snap, "STRONG_BULL", "BUY")      # market_health regime labels
    assert n == 6 and all(ev.values())
    n, ev = S.confluence("BEAR", snap, "BULL", "BUY")
    assert n == 1 and ev["volume > 1.5x"]
    assert not S.confluence("BULL", snap, "HIGH_RISK", None)[1]["market regime"]


@pytest.fixture
def db(temp_db):
    from db.schema import get_connection, init_db
    from research import tech_signals as S
    init_db()
    conn = get_connection()
    S.ensure_tables(conn)
    yield conn
    conn.close()


def _store(conn, sym, df):
    for d, r in df.iterrows():
        conn.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) VALUES (?,?,?,?,?,?,?,"
                     "'dhan')", (sym, str(d.date()), r.open, r.high, r.low, r.close, r.volume))
    conn.commit()


def test_run_technical_stores_snapshots_and_signals(db):
    from research import tech_signals as S
    closes = [100 + np.sin(i / 5) * 3 for i in range(300)] + [115.0]
    df = _frame(closes, vols=[1e5] * 300 + [4e5], start="2025-06-02")
    _store(db, "BRK", df)
    _store(db, "FLAT", _frame([50.0 + (i % 3) * 0.1 for i in range(301)], start="2025-06-02"))
    _store(db, "STALE", _frame(np.linspace(10, 20, 299), start="2025-06-02"))
    as_of = df.index[-1].date()
    out = S.run_technical(["BRK", "FLAT", "STALE"], as_of=as_of, conn=db)
    assert out["rows"] == 2, "STALE has no bar on the run date"
    sig = {s["scan"]: s for s in S.todays_signals(db, str(as_of)) if s["symbol"] == "BRK"}
    assert "high_52w_breakout" in sig
    b = sig["high_52w_breakout"]
    assert b["direction"] == "BULL" and b["stop"] < b["entry"] < b["target"] and b["evidence"]["volume > 1.5x"]
    snap = S.latest_snapshot(db, ["BRK"])["BRK"]
    assert snap["scan_high_52w_breakout"] == 1 and snap["tech_rating_label"]


def _signal(conn, sid, sym, d0, direction, entry, stop, target, horizon=3):
    conn.execute("INSERT INTO technical_signal (signal_id, symbol, date, scan, name, direction, entry, stop, target, "
                 "horizon, confluence, status) VALUES (?,?,?,?,?,?,?,?,?,?,1,'OPEN')",
                 (sid, sym, d0, "golden_cross", "Golden cross", direction, entry, stop, target, horizon))


def test_outcomes_target_stop_expiry_and_stats(db):
    from research import tech_signals as S
    d0 = dt.date(2026, 9, 1)
    for i, (hi, lo, cl) in enumerate([(103, 99, 102), (109, 101, 108), (110, 100, 105)]):
        db.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) VALUES "
                   "('UP',?,?,?,?,?,1,'dhan')", (str(d0 + dt.timedelta(days=i + 1)), cl, hi, lo, cl))
        db.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) VALUES "
                   "('DN',?,?,?,?,?,1,'dhan')", (str(d0 + dt.timedelta(days=i + 1)), cl, hi, lo, cl))
        db.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) VALUES "
                   "('SIDE',?,?,?,?,?,1,'dhan')", (str(d0 + dt.timedelta(days=i + 1)), 101, 102, 99, 101))
    _signal(db, "a", "UP", str(d0), "BULL", 100, 96, 108)          # target on day 2
    _signal(db, "b", "DN", str(d0), "BULL", 100, 99.5, 120)        # stopped on day 1 (low 99)
    _signal(db, "c", "SIDE", str(d0), "BULL", 100, 90, 120)        # expires after 3 bars at 101
    db.commit()
    assert S.evaluate_signals(db) == {"TARGET": 1, "STOPPED": 1, "EXPIRED": 1}
    got = {r[0]: r[1:] for r in db.execute("SELECT signal_id, status, r_multiple, return_pct FROM technical_signal")}
    assert got["a"] == ("TARGET", 2.0, 8.0) and got["b"][0] == "STOPPED" and got["b"][1] == -1.0
    assert got["c"] == ("EXPIRED", 0.1, 1.0)
    st = S.scan_stats(db)[0]
    assert st["closed"] == 3 and st["win_rate_pct"] == pytest.approx(33.3) and st["avg_r"] == pytest.approx(0.37)


def test_a_bar_touching_both_levels_counts_as_stopped(db):
    from research import tech_signals as S
    db.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) VALUES "
               "('WIDE','2026-09-02',100,110,90,100,1,'dhan')")
    _signal(db, "w", "WIDE", "2026-09-01", "BULL", 100, 95, 105)
    db.commit()
    S.evaluate_signals(db)
    assert db.execute("SELECT status FROM technical_signal WHERE signal_id='w'").fetchone()[0] == "STOPPED"


# ── screener integration and API ──────────────────────────────────────────

def test_screener_reads_the_technical_snapshot_and_contains(db, monkeypatch):
    from research import screener as SC
    from research import tech_signals as S
    import data.index_constituents as IC
    monkeypatch.setattr(IC, "get_symbol_industry_map", lambda: {})
    closes = [100 + np.sin(i / 5) * 3 for i in range(300)] + [115.0]
    df = _frame(closes, vols=[1e5] * 300 + [4e5], start="2025-06-02")
    _store(db, "BRK", df)
    S.run_technical(["BRK"], as_of=df.index[-1].date(), conn=db)
    SC.clear_cache()
    rows = SC.build_snapshot(db, as_of=df.index[-1].date())
    assert [r["symbol"] for r in rows] == ["BRK"], "a stock with no fundamentals is in a technical screen"
    r = SC.run_screen(db, "scan_high_52w_breakout = 1 AND signals CONTAINS \"breakout\"", rows=rows)
    assert r["count"] == 1
    assert SC.run_screen(db, "patterns CONTAINS \"no such pattern\"", rows=rows)["count"] == 0
    with pytest.raises(SC.ScreenError):
        SC.parse("patterns CONTAINS 5")
    for p in SC.PRESETS:
        SC.parse(p["query"])


@pytest.fixture
def api(tmp_path, monkeypatch, db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    return TestClient(server.app), security


def test_signals_api(api, db):
    from research import tech_signals as S
    client, security = api
    closes = [100 + np.sin(i / 5) * 3 for i in range(300)] + [115.0]
    df = _frame(closes, vols=[1e5] * 300 + [4e5], start="2025-06-02")
    _store(db, "BRK", df)
    S.run_technical(["BRK"], as_of=df.index[-1].date(), conn=db)
    r = client.get("/api/signals/technical")
    assert r.status_code == 200 and any(x["scan"] == "high_52w_breakout" for x in r.json())
    assert client.get("/api/signals/technical", params={"direction": "SIDEWAYS"}).status_code == 400
    assert client.get("/api/signals/technical/stats").status_code == 200
    one = client.get("/api/signals/technical/symbol/BRK").json()
    assert one["snapshot"]["symbol"] == "BRK" and one["signals"]
    assert client.get("/api/signals/technical/symbol/NOPE").status_code == 404
    assert client.post("/api/signals/technical/run").status_code == 401
    assert client.get("/signals").status_code == 200


def test_signal_routes_permissions_and_tables():
    from enterprise.authz import permission_for
    from enterprise.privacy import classify
    from enterprise.scoping import TABLES
    assert permission_for("GET", "/api/signals/technical") == "dashboard:read"
    assert permission_for("POST", "/api/signals/technical/run") == "research:run"
    for t in ("technical_snapshot", "technical_signal"):
        assert classify(t) is not None and TABLES[t] == "GLOBAL"


def test_rs_rating_ranks_the_universe_and_pocket_pivot():
    from research import tech_signals as S
    snaps = {"A": {"_rs_raw": 0.5}, "B": {"_rs_raw": -0.2}, "C": {"_rs_raw": 0.1}, "D": {"_rs_raw": None}}
    S.rs_rank(snaps)
    assert snaps["A"]["rs_rating"] == 99 and snaps["B"]["rs_rating"] == 33 and snaps["D"]["rs_rating"] is None
    assert T.rs_raw(pd.Series(np.linspace(100, 200, 200))) is None, "needs a year of bars"
    closes = list(np.linspace(100, 130, 80)) + [129.0, 128.5, 130.0, 129.5, 132.0]
    vols = [1e5] * 80 + [1.5e5, 1.6e5, 1e5, 1.7e5, 3e5]
    d = T.indicators(_frame(closes, vols=vols))
    assert "pocket_pivot" in [h[0] for h in T.run_scans(d)]
