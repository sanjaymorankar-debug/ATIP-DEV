"""
W39 (RG): the market regime gate -- distribution days, corrections, rally attempts, follow-through
days, the 200-DMA, and how signals carry the gate. Price paths are built so the expected state is
known on every session.
"""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from research import regime_gate as R


def _path(steps, start=100.0, first="2025-01-01"):
    """steps: [(change %, volume ratio vs the previous session)]; returns (close, volume ratio)."""
    c, v = [start], [np.nan]
    for ch, vr in steps:
        c.append(c[-1] * (1 + ch / 100))
        v.append(vr)
    idx = pd.bdate_range(first, periods=len(c))
    return pd.Series(c, index=idx), pd.Series(v, index=idx)


CFG = {"warmup_sessions": 0}
QUIET = (0.2, 0.9)                       # a calm up day on lower volume


def _run(steps, **cfg):
    close, vol = _path(steps)
    return R.replay(close, vol, {**CFG, **cfg})


# ── distribution days ─────────────────────────────────────────────────────

def test_distribution_day_needs_the_drop_and_higher_volume():
    rows = _run([QUIET] * 5 + [(-0.5, 1.2), (-0.5, 0.8), (-0.1, 1.5), (-0.2, 1.01)])
    assert [r["distribution_day"] for r in rows[-4:]] == [1, 0, 0, 1], "lower volume or a 0.1% dip does not count"
    assert rows[-1]["dd_count"] == 2 and rows[-1]["status"] == "CONFIRMED_UPTREND"


def test_distribution_days_expire_after_25_sessions_or_a_5pct_rally():
    rows = _run([QUIET] * 3 + [(-0.5, 1.2)] + [QUIET] * 24 + [(0.0, 0.9)])
    assert rows[4]["dd_count"] == 1 and rows[28]["dd_count"] == 1 and rows[29]["dd_count"] == 0, \
        "the distribution day is session 1 of its 25"
    rows = _run([QUIET] * 3 + [(-0.5, 1.2)] + [(1.0, 0.9)] * 6)
    assert rows[8]["dd_count"] == 1 and rows[9]["dd_count"] == 0, "removed once the Nifty is 5% above that close"


# ── status and gate ───────────────────────────────────────────────────────

DIST = [(-0.8, 1.2), (0.1, 0.8)]        # a distribution day, then a quiet day


def test_pressure_correction_rally_attempt_and_follow_through():
    steps = [QUIET] * 5 + DIST * 6 + [(-1.0, 0.9)] * 3 + [(0.4, 0.9), (0.2, 0.9), (0.2, 0.9), (1.6, 1.3), QUIET]
    rows = _run(steps)
    st = [r["status"] for r in rows]
    assert st[12] == "UPTREND_UNDER_PRESSURE" and rows[12]["gate"] == "CAUTION", "4 distribution days"
    assert st[16] == "CORRECTION" and rows[16]["gate"] == "CLOSED", "6 distribution days"
    i = st.index("RALLY_ATTEMPT", 21)
    assert rows[i]["rally_day"] == 1 and rows[i + 2]["rally_day"] == 3 and rows[i + 2]["gate"] == "CLOSED"
    ftd = rows[i + 3]
    assert ftd["follow_through"] == 1 and ftd["status"] == "CONFIRMED_UPTREND" and ftd["dd_count"] == 0
    assert ftd["ftd_date"] == str(ftd["date"]) and rows[-1]["ftd_date"] == str(ftd["date"])


def test_no_follow_through_before_day_4_or_without_volume():
    base = [QUIET] * 5 + DIST * 6 + [(-1.0, 0.9)] * 2
    early = _run(base + [(0.4, 0.9), (2.0, 1.5)])               # day 2: too early
    assert early[-1]["status"] == "RALLY_ATTEMPT" and early[-1]["follow_through"] == 0
    quiet = _run(base + [(0.4, 0.9), (0.2, 0.9), (0.2, 0.9), (2.0, 0.9)])   # day 4, lower volume
    assert quiet[-1]["status"] == "RALLY_ATTEMPT" and quiet[-1]["rally_day"] == 4


def test_a_lower_close_resets_the_rally_attempt():
    rows = _run([QUIET] * 5 + DIST * 6 + [(-1.0, 0.9), (0.5, 0.9), (0.2, 0.9), (-2.0, 0.9), (0.3, 0.9)])
    assert rows[-2]["status"] == "CORRECTION" and "new low" in rows[-2]["reason"]
    assert rows[-1]["status"] == "RALLY_ATTEMPT" and rows[-1]["rally_day"] == 1


def test_a_10pct_slide_is_a_correction_even_without_volume():
    rows = _run([QUIET] * 5 + [(-1.2, 0.9)] * 9)
    assert rows[-1]["status"] == "CORRECTION" and "below the uptrend's high" in rows[-1]["reason"]
    assert all(r["distribution_day"] == 0 for r in rows)


def test_a_follow_through_fails_below_the_correction_low():
    steps = ([QUIET] * 5 + DIST * 6 + [(-1.0, 0.9)] * 2 + [(0.4, 0.9), (0.2, 0.9), (0.2, 0.9), (1.6, 1.3)]
             + [(-1.0, 0.9)] * 4)
    rows = _run(steps)
    assert rows[-5]["follow_through"] == 1
    flip = next(r for r in rows[-4:] if r["status"] == "CORRECTION")
    assert flip is rows[-2] and "follow-through failed" in flip["reason"], "the 3rd down day undercuts the low"
    assert flip["drawdown_pct"] is None and rows[-1]["status"] == "CORRECTION"


def test_gate_uses_the_200_dma_and_warm_up():
    up = [(0.1, 0.9)] * 230
    rows = R.replay(*_path(up), {"warmup_sessions": 60})
    assert rows[10]["gate"] is None and rows[10]["status"] is None and "warming up" in rows[10]["reason"]
    assert rows[-1]["gate"] == "OPEN" and rows[-1]["above_200dma"] == 1
    below = R.replay(*_path([(-0.04, 0.9)] * 230), {"warmup_sessions": 60, "correction_drawdown_pct": 50})
    assert below[-1]["status"] == "CONFIRMED_UPTREND" and below[-1]["gate"] == "CAUTION"
    assert "below its 200-DMA" in below[-1]["reason"]


def test_replay_is_point_in_time():
    rng = np.random.default_rng(5)
    steps = [(float(rng.normal(0.05, 1.0)), float(rng.uniform(0.7, 1.3))) for _ in range(300)]
    close, vol = _path(steps)
    full = R.replay(close, vol, {"warmup_sessions": 30})
    for cut in (120, 200, 260):
        part = R.replay(close.iloc[:cut], vol.iloc[:cut], {"warmup_sessions": 30})
        assert part[-1] == full[cut - 1], "a row never depends on later sessions"


def test_alignment_truth_table():
    assert R.alignment("BULL", "OPEN") == "WITH" and R.alignment("BULL", "CLOSED") == "AGAINST"
    assert R.alignment("BEAR", "CLOSED") == "WITH" and R.alignment("BEAR", "OPEN") == "AGAINST"
    assert R.alignment("BULL", "CAUTION") == "MIXED" == R.alignment("BEAR", "CAUTION")
    assert R.alignment("BULL", None) is None and R.alignment("NEUTRAL", "OPEN") is None


# ── stored data ───────────────────────────────────────────────────────────

@pytest.fixture
def db(temp_db):
    from db.schema import get_connection, init_db
    from research import tech_signals as S
    init_db()
    conn = get_connection()
    S.ensure_tables(conn)
    R.ensure_tables(conn)
    yield conn
    conn.close()


def _seed_nifty(conn, closes, end):
    days = pd.bdate_range(end=end, periods=len(closes))
    for d, c in zip(days, closes):
        conn.execute("INSERT INTO global_market_history (series, date, close) VALUES ('nifty50',?,?)",
                     (str(d.date()), float(c)))
    conn.commit()
    return days


def test_market_volume_uses_stocks_present_on_both_days(db):
    days = pd.bdate_range("2026-09-01", periods=3)
    for k in range(25):
        for d, v in zip(days, (100, 120, 90)):
            db.execute("INSERT INTO prices_daily (symbol,date,close,volume,source) VALUES (?,?,10,?,'dhan')",
                       (f"S{k}", str(d.date()), v))
    db.execute("INSERT INTO prices_daily (symbol,date,close,volume,source) VALUES ('NEWCO',?,10,1e9,'dhan')",
               (str(days[1].date()),))
    db.commit()
    ratio, src = R.market_volume_ratio(db, days[-1].date())
    assert ratio.iloc[1] == pytest.approx(1.2) and ratio.iloc[2] == pytest.approx(0.75), "NEWCO's debut is ignored"
    assert "26 stocks" in src


def test_update_stores_the_series_and_extends_yahoo_with_market_health(db):
    end = dt.date(2026, 10, 6)
    days = _seed_nifty(db, np.linspace(20000, 24000, 260), end - dt.timedelta(days=1))
    db.execute("INSERT INTO market_health (date, nifty_close) VALUES (?, 24100)", (str(end),))
    db.commit()
    out = R.update(db, end)
    assert out["status"] == "SUCCESS" and out["as_of"] == str(end) and "market_health" in out["close_source"]
    assert out["gate"] == "OPEN" and out["rows"] == 261 and out["volume_source"] is None
    cur = R.current(db)
    assert cur["gate"] == "OPEN" and cur["status_since"] == str(days[60].date()) and cur["rules"]["dd_window"] == 25
    assert len(R.history(db, 30)) == 30 and R.gate_on(db, days[100].date())["date"] == str(days[100].date())


def test_update_skips_without_history(db):
    assert R.update(db, dt.date(2026, 10, 6))["status"] == "SKIPPED"
    assert R.current(db)["status"] == "EMPTY"


# ── signals carry the gate ────────────────────────────────────────────────

def _frame(closes, vols, start):
    c = np.asarray(closes, dtype=float)
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.005, "low": np.minimum(o, c) * 0.995, "close": c,
                         "volume": np.asarray(vols, dtype=float)}, index=pd.bdate_range(start, periods=len(c)))


def _breakout_stock(conn):
    closes = [100 + np.sin(i / 5) * 3 for i in range(300)] + [115.0]
    df = _frame(closes, [1e5] * 300 + [4e5], "2025-06-02")
    for d, r in df.iterrows():
        conn.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) VALUES (?,?,?,?,?,?,?,"
                     "'dhan')", ("BRK", str(d.date()), r.open, r.high, r.low, r.close, r.volume))
    conn.commit()
    return df.index[-1].date()


def test_signals_are_tagged_and_against_market_signals_are_not_alerted(db, monkeypatch):
    from research import tech_signals as S
    import alerts.telegram as TG
    sent = []
    monkeypatch.setattr(TG, "notify", lambda text, **k: sent.append(text))
    as_of = _breakout_stock(db)
    falling = list(np.linspace(20000, 24000, 230)) + list(np.linspace(24000, 20500, 30))   # -15%: correction
    _seed_nifty(db, falling, as_of)
    out = S.run_technical(["BRK"], as_of=as_of, conn=db)
    assert out["market_gate"]["gate"] == "CLOSED"
    sig = S.todays_signals(db, str(as_of))
    bull = [s for s in sig if s["direction"] == "BULL"]
    assert bull and all(s["market_gate"] == "CLOSED" and s["alignment"] == "AGAINST" for s in bull)
    assert S.todays_signals(db, str(as_of), alignment="not_against") == [s for s in sig if s["alignment"] != "AGAINST"]
    for s in bull:
        db.execute("UPDATE technical_signal SET confluence=5 WHERE symbol=? AND scan=?", (s["symbol"], s["scan"]))
    db.commit()
    assert S.alert_top(db, str(as_of)) == {"alerted": 0} and not sent, "never alert against the market"


def test_old_signals_get_their_own_days_gate_and_gate_effect_splits(db):
    from research import tech_signals as S
    days = _seed_nifty(db, np.linspace(20000, 24000, 260), dt.date(2026, 10, 6))
    R.update(db, days[-1].date())
    d_open = str(days[-5].date())
    db.execute("UPDATE market_regime_gate SET gate='CLOSED' WHERE date=?", (str(days[-10].date()),))
    rows = [("w1", d_open, "BULL", "TARGET", 2.0), ("w2", d_open, "BULL", "STOPPED", -1.0),
            ("a1", str(days[-10].date()), "BULL", "STOPPED", -1.0), ("u1", "2020-01-01", "BULL", "OPEN", None)]
    for sid, d, direction, status, r in rows:
        db.execute("INSERT INTO technical_signal (signal_id, symbol, date, scan, name, direction, confluence, status, "
                   "r_multiple) VALUES (?, 'X', ?, ?, 'Scan', ?, 1, ?, ?)", (sid, d, sid, direction, status, r))
    db.commit()
    assert S.backfill_gate(db) == 3, "the 2020 signal predates the stored gate"
    got = dict(db.execute("SELECT signal_id, alignment FROM technical_signal").fetchall())
    assert got == {"w1": "WITH", "w2": "WITH", "a1": "AGAINST", "u1": None}
    eff = {g["alignment"]: g for g in S.gate_effect(db)["groups"]}
    assert eff["WITH"]["closed"] == 2 and eff["WITH"]["avg_r"] == 0.5 and eff["AGAINST"]["avg_r"] == -1.0
    assert eff["UNKNOWN"]["open"] == 1
    assert [o["closed"] for o in S.scan_stats(db, alignment="AGAINST")] == [1]


def _closed(conn, prefix, alignment, rs):
    for i, r in enumerate(rs):
        conn.execute("INSERT INTO technical_signal (signal_id, symbol, date, scan, name, direction, confluence, status, "
                     "r_multiple, market_gate, alignment) VALUES (?, 'X', '2026-01-01', ?, 'Scan', 'BULL', 1, ?, ?, "
                     "'OPEN', ?)", (f"{prefix}{i}", f"{prefix}{i}", "TARGET" if r > 0 else "STOPPED", r, alignment))
    conn.commit()


def test_gate_effect_only_speaks_when_the_gap_is_beyond_noise(db):
    from research import tech_signals as S
    _closed(db, "w", "WITH", [2.0, -1.0, -1.0] * 12)          # +0.0 R each side: no difference
    _closed(db, "a", "AGAINST", [-1.0, 2.0, -1.0] * 12)
    assert S.gate_effect(db)["verdict"].startswith("no clear difference yet")
    db.execute("DELETE FROM technical_signal")
    _closed(db, "w", "WITH", [2.0, 2.0, -1.0] * 12)           # +1.0 R vs -0.5 R
    _closed(db, "a", "AGAINST", [2.0, -1.0, -1.0, -1.0] * 9)
    out = S.gate_effect(db)
    assert out["verdict"].startswith("the gate helps") and out["t_stat"] > 2
    db.execute("DELETE FROM technical_signal WHERE alignment='AGAINST'")
    _closed(db, "a", "AGAINST", [2.0] * 5)
    assert S.gate_effect(db)["verdict"] is None, "5 closed signals against the market are too few"


def test_old_technical_signal_table_gets_the_new_columns(tmp_path):
    import sqlite3
    from research import tech_signals as S
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute("CREATE TABLE technical_signal (signal_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, date DATE NOT NULL, "
                 "scan TEXT NOT NULL, name TEXT, direction TEXT NOT NULL, confluence INTEGER, status TEXT)")
    conn.commit()
    S.ensure_tables(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(technical_signal)").fetchall()}
    assert {"market_gate", "alignment"} <= cols
    conn.close()


# ── pulse, API, permissions ───────────────────────────────────────────────

def test_pulse_reports_the_gate(db):
    from research import market_pulse as MP
    _seed_nifty(db, np.linspace(20000, 24000, 260), dt.date(2026, 10, 6))
    R.update(db, dt.date(2026, 10, 6))
    p = MP.pulse(db)
    assert p["market_gate"]["gate"] == "OPEN" and any(r.startswith("market gate OPEN") for r in p["reasons"])


@pytest.fixture
def api(tmp_path, monkeypatch, db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    return TestClient(server.app)


def test_regime_api(api, db):
    assert api.get("/api/market-regime").json()["status"] == "EMPTY"
    _seed_nifty(db, np.linspace(20000, 24000, 260), dt.date(2026, 10, 6))
    R.update(db, dt.date(2026, 10, 6))
    g = api.get("/api/market-regime").json()
    assert g["gate"] == "OPEN" and g["changes"]
    h = api.get("/api/market-regime/history", params={"days": 50}).json()
    assert len(h) == 50 and h[-1]["date"] == "2026-10-06"
    assert api.post("/api/market-regime/run").status_code == 401
    assert api.get("/api/signals/technical/gate-effect").json()["groups"] == []
    assert api.get("/api/signals/technical", params={"alignment": "SIDEWAYS"}).status_code == 400
    assert api.get("/api/signals/technical", params={"alignment": "not_against"}).status_code == 200
    assert api.get("/api/signals/technical/stats", params={"alignment": "WITH"}).status_code == 200
    page = api.get("/market-pulse").text
    assert "Market gate" in page and "drawGate" in page
    assert "Hide signals against the market" in api.get("/signals").text


def test_permissions_and_table():
    from enterprise.authz import permission_for
    from enterprise.privacy import classify
    from enterprise.scoping import TABLES
    assert permission_for("GET", "/api/market-regime") == "dashboard:read"
    assert permission_for("POST", "/api/market-regime/run") == "research:run"
    assert classify("market_regime_gate") is not None and TABLES["market_regime_gate"] == "GLOBAL"
