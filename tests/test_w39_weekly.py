"""
W39 Phase 2 (item 3): the weekly technical rating on completed weekly bars, the daily / weekly
agreement, and how signals, the screener and the track record use it.
"""

import numpy as np
import pandas as pd
import pytest

from research import technicals as T


def _daily(closes, start="2025-01-06", vols=None):
    c = np.asarray(closes, dtype=float)
    o = np.r_[c[0], c[:-1]]
    v = np.asarray(vols, dtype=float) if vols is not None else np.full(len(c), 1e5)
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.004, "low": np.minimum(o, c) * 0.996, "close": c,
                         "volume": v}, index=pd.bdate_range(start, periods=len(c)))


# ── weekly bars and rating ────────────────────────────────────────────────

def test_weekly_bars_aggregate_and_keep_only_completed_weeks():
    df = _daily(np.arange(1, 14, dtype=float), start="2026-09-07")      # Mon 7 Sep .. Wed 23 Sep
    w = T.weekly_bars(df)
    assert [str(d.date()) for d in w.index] == ["2026-09-11", "2026-09-18"], "the week of 21 Sep is still open"
    first = w.iloc[0]
    assert first["open"] == 1 and first["close"] == 5 and first["high"] == pytest.approx(5 * 1.004)
    assert first["volume"] == 5e5
    assert len(T.weekly_bars(df.iloc[:10])) == 2, "on Friday 18 Sep that week is complete"


def test_a_week_cut_short_by_a_friday_holiday_counts_once_the_next_week_starts():
    df = _daily(np.arange(1, 12, dtype=float), start="2026-09-07").drop(pd.Timestamp("2026-09-18"))   # .. Mon 21 Sep
    upto_thu = df[df.index <= "2026-09-17"]
    assert [str(d.date()) for d in T.weekly_bars(upto_thu).index] == ["2026-09-11"]
    w = T.weekly_bars(df)                                                # Monday 21 Sep has arrived
    assert [str(d.date()) for d in w.index] == ["2026-09-11", "2026-09-18"] and w.iloc[1]["close"] == 9


def test_weekly_rating_needs_history_and_follows_the_trend():
    assert T.weekly_rating(_daily(np.linspace(100, 120, 150)))["tech_rating_w"] is None, "30 weeks are too few"
    up = T.weekly_rating(_daily(np.linspace(100, 200, 400)))
    assert up["tech_rating_w_label"] in ("BUY", "STRONG_BUY") and up["supertrend_dir_w"] == 1 and up["week_ending"]
    down = T.weekly_rating(_daily(np.linspace(200, 100, 400)))
    assert down["tech_rating_w_label"] in ("SELL", "STRONG_SELL") and down["supertrend_dir_w"] == -1


def test_alignment_and_agreement_truth_tables():
    assert T.mtf_alignment("BUY", "STRONG_BUY") == "BULL" and T.mtf_alignment("STRONG_SELL", "SELL") == "BEAR"
    assert T.mtf_alignment("BUY", "NEUTRAL") == "MIXED" and T.mtf_alignment("BUY", None) is None
    assert T.weekly_agrees("BULL", "BUY") == 1 and T.weekly_agrees("BULL", "SELL") == 0
    assert T.weekly_agrees("BEAR", "STRONG_SELL") == 1 and T.weekly_agrees("BEAR", "NEUTRAL") == 0
    assert T.weekly_agrees("BULL", None) is None and T.weekly_agrees("NEUTRAL", "BUY") is None


def test_snapshot_carries_the_weekly_rating():
    snap = T.snapshot(_daily(np.linspace(100, 200, 400)))
    assert snap["tech_rating_w_label"] in ("BUY", "STRONG_BUY") and snap["mtf_alignment"] == "BULL"
    short = T.snapshot(_daily(np.linspace(100, 120, 100)))
    assert short["tech_rating_w"] is None and short["mtf_alignment"] is None


# ── stored, screened, tracked ─────────────────────────────────────────────

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


def test_run_technical_stores_weekly_fields_and_tags_signals(db):
    from research import tech_signals as S
    closes = list(np.linspace(100, 160, 330)) + [170.0]
    df = _daily(closes, start="2025-06-02", vols=[1e5] * 330 + [4e5])
    _store(db, "UPCO", df)
    as_of = df.index[-1].date()
    S.run_technical(["UPCO"], as_of=as_of, conn=db)
    snap = S.latest_snapshot(db, ["UPCO"])["UPCO"]
    assert snap["tech_rating_w_label"] in ("BUY", "STRONG_BUY") and snap["mtf_alignment"] == "BULL"
    sig = S.todays_signals(db, str(as_of))
    assert sig and all(s["weekly_agrees"] == (1 if s["direction"] == "BULL" else 0) for s in sig)
    assert sig[0]["tech_rating_w_label"] == snap["tech_rating_w_label"]


def test_forward_stats_split_by_weekly_agreement(db):
    from research import tech_signals as S
    rows = [("a1", 1, 2.0), ("a2", 1, 4.0), ("d1", 0, -1.0), ("n1", None, 0.5)]
    for sid, wk, x in rows:
        db.execute("INSERT INTO technical_signal (signal_id, symbol, date, scan, name, direction, confluence, status, "
                   "market_gate, weekly_agrees, excess_20d) VALUES (?, ?, '2026-06-01', 'golden_cross', 'Golden cross', "
                   "'BULL', 2, 'OPEN', 'OPEN', ?, ?)", (sid, sid, wk, x))
    db.commit()
    wk = {b["weekly"]: b["cells"] for b in S.forward_stats(db, 20)["by_weekly"]}
    assert wk["AGREES"]["ALL"]["n"] == 2 and wk["AGREES"]["OPEN"]["median_excess_pct"] == 3.0
    assert wk["DISAGREES"]["ALL"]["beat_nifty_pct"] == 0.0 and wk["NO_WEEKLY"]["ALL"]["n"] == 1


def test_screener_fields_and_presets():
    from research import screener as SC
    rows = [{"symbol": "A", "mtf_alignment": "BULL", "tech_rating_w": 0.6, "scan_donchian_20_breakout": 1},
            {"symbol": "B", "mtf_alignment": "MIXED", "tech_rating_w": 0.2, "scan_donchian_20_breakout": 1},
            {"symbol": "C", "mtf_alignment": "BULL", "tech_rating_w": 0.4, "scan_donchian_20_breakout": 0,
             "scan_high_52w_breakout": 0}]
    presets = {p["key"]: p for p in SC.PRESETS}
    r = SC.run_screen(None, presets["t_mtf_bull"]["query"], rows=rows)
    assert sorted(x["symbol"] for x in r["rows"]) == ["A", "C"] and "tech_rating_w_label" in r["columns"]
    r = SC.run_screen(None, presets["t_breakout_weekly"]["query"], rows=rows)
    assert [x["symbol"] for x in r["rows"]] == ["A"]
    assert SC.field_key("weekly_rating") == "tech_rating_w" and SC.field_key("mtf") == "mtf_alignment"


def test_old_snapshot_table_gets_the_weekly_columns(tmp_path):
    import sqlite3
    from research import tech_signals as S
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute("CREATE TABLE technical_snapshot (symbol TEXT NOT NULL, date DATE NOT NULL, tech_rating REAL, "
                 "PRIMARY KEY (symbol, date))")
    conn.commit()
    S.ensure_tables(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(technical_snapshot)").fetchall()}
    assert {"tech_rating_w", "tech_rating_w_label", "rsi_14_w", "supertrend_dir_w", "mtf_alignment"} <= cols
    conn.close()


def test_signals_page_shows_the_weekly_column(tmp_path, monkeypatch, db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    page = TestClient(server.app).get("/signals").text
    assert "Does the weekly trend add?" in page and "'Weekly'" in page
