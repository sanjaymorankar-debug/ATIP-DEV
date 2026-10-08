"""
W39 Phase 3 (the 75-minute rating left for later): the technical rating on 75-minute bars built from the stored
15-minute bars -- the session's five bar boundaries, completed bars only (including a half day and a bar the
feed never delivered), the minimum history, the rating on a planted trend, the daily / 75-minute agreement,
how the 20:30 run stores it, and the screener fields and preset.
"""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from research import technicals as T

DAY = dt.date(2026, 10, 6)                          # a Tuesday
STARTS = ["09:15", "10:30", "11:45", "13:00", "14:15"]


def _bars(day, n=25, c0=100.0, step=0.1, skip=(), first="09:15"):
    """n consecutive 15-minute bars from `first` on `day` (close c0 + step a bar), leaving out the slots in skip."""
    t0 = dt.datetime.combine(day, dt.time(*map(int, first.split(":"))))
    idx, rows = [], []
    for i in range(n):
        t = t0 + dt.timedelta(minutes=15 * i)
        if t.strftime("%H:%M") in skip:
            continue
        c = c0 + step * i
        idx.append(t)
        rows.append((c - 0.05, c + 0.2, c - 0.2, c, 1000.0 + i))
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx), columns=["open", "high", "low", "close", "volume"])


def _trend(days, start=100.0, step=0.05):
    """Full sessions of 15-minute bars drifting by `step` a bar with a small zigzag."""
    idx, rows, c = [], [], start
    for d in days:
        t0 = dt.datetime.combine(d, dt.time(9, 15))
        for i in range(25):
            o = c
            c = c + step + (0.04 if i % 2 else -0.04)
            idx.append(t0 + dt.timedelta(minutes=15 * i))
            rows.append((o, max(o, c) + 0.05, min(o, c) - 0.05, c, 1e4))
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx), columns=["open", "high", "low", "close", "volume"])


def _at(day, hhmm):
    return dt.datetime.combine(day, dt.time(*map(int, hhmm.split(":"))))


def _starts(b):
    return [t.strftime("%H:%M") for t in b.index]


# ── aggregation boundaries ────────────────────────────────────────────────

def test_the_session_splits_into_five_75_minute_bars_at_the_nse_boundaries():
    b = T.bars_75(_bars(DAY), now=_at(DAY, "15:30"))
    assert _starts(b) == STARTS and list(b["bars"]) == [5] * 5
    first = b.iloc[0]                                  # 09:15, 09:30, 09:45, 10:00, 10:15
    assert first["open"] == pytest.approx(99.95) and first["close"] == pytest.approx(100.4), "10:15 is the last bar in"
    assert first["high"] == pytest.approx(100.6) and first["low"] == pytest.approx(99.8)
    assert first["volume"] == sum(1000.0 + i for i in range(5))
    assert b.iloc[1]["open"] == pytest.approx(100.45), "10:30 opens the second bar"
    assert b.iloc[-1]["close"] == pytest.approx(102.4), "the 15:15 bar closes the session's last 75 minutes"


def test_bars_outside_the_regular_session_are_ignored():
    extra = pd.concat([_bars(DAY, n=1, first="09:00", c0=50.0), _bars(DAY), _bars(DAY, n=4, first="15:30", c0=200.0)])
    b = T.bars_75(extra, now=_at(DAY, "23:00"))
    assert _starts(b) == STARTS and b["low"].min() > 90 and b["high"].max() < 110, "no pre-open or after-hours bar"


# ── completed bars only ───────────────────────────────────────────────────

def test_only_completed_75_minute_bars_count_during_the_session():
    df = _bars(DAY)
    assert _starts(T.bars_75(df, now=_at(DAY, "10:29"))) == [], "09:15-10:30 is still open at 10:29"
    assert _starts(T.bars_75(df, now=_at(DAY, "10:30"))) == ["09:15"]
    assert _starts(T.bars_75(df, now=_at(DAY, "11:44"))) == ["09:15"], "10:30-11:45 waits for its end"
    assert _starts(T.bars_75(df, now=_at(DAY, "11:45"))) == ["09:15", "10:30"]
    late = df[df.index < _at(DAY, "11:30")]            # the 11:30 bar has not been fetched yet
    assert _starts(T.bars_75(late, now=_at(DAY, "11:50"))) == ["09:15"], "waits for its closing 15 minutes"
    assert _starts(T.bars_75(df, now=_at(DAY, "11:40"))) == ["09:15"], "a 15-minute bar is done 15 minutes after it starts"
    assert len(T.bars_75(df)) == 5, "now defaults to the end of the last stored 15-minute bar"
    assert _starts(T.bars_75(df[df.index < _at(DAY, "11:30")])) == ["09:15"]


def test_a_half_day_and_a_missing_bar_count_once_the_session_is_over():
    half = _bars(DAY, n=13)                            # an early close: the last bar is 12:15 (ends 12:30)
    assert _starts(T.bars_75(half, now=_at(DAY, "13:05"))) == ["09:15", "10:30"], "the session might still go on"
    b = T.bars_75(half, now=_at(DAY, "15:30"))
    assert _starts(b) == ["09:15", "10:30", "11:45"] and b.iloc[-1]["bars"] == 3
    assert b.iloc[-1]["close"] == pytest.approx(101.2), "the half day's last bar closes with the session"
    gap = _bars(DAY, skip=("10:00",))                  # one 15-minute bar the feed never delivered
    g = T.bars_75(gap, now=_at(DAY, "15:30"))
    assert list(g["bars"]) == [4, 5, 5, 5, 5] and g.iloc[0]["volume"] == 1000 + 1001 + 1002 + 1004
    no_close = _bars(DAY, skip=("10:15",))             # the closing 15 minutes of the first bar missing
    assert _starts(T.bars_75(no_close, now=_at(DAY, "11:00"))) == [], "in the session it waits for 10:15"
    assert _starts(T.bars_75(no_close, now=_at(DAY, "15:30")))[0] == "09:15", "after the close it counts"
    two = pd.concat([_bars(DAY - dt.timedelta(days=1)), _bars(DAY, n=6)])
    assert len(T.bars_75(two, now=_at(DAY, "12:00"))) == 6, "yesterday's five, today's first"


# ── the rating ─────────────────────────────────────────────────────────────

SESSIONS = [d.date() for d in pd.bdate_range("2026-09-01", periods=14)]


def test_the_rating_needs_35_completed_bars():
    six, seven = _trend(SESSIONS[:6]), _trend(SESSIONS[:7])
    r6 = T.rating_75(six, day=SESSIONS[5])
    assert r6["bars_75"] == 30 and r6["tech_rating_75"] is None and r6["tech_rating_75_label"] is None
    r7 = T.rating_75(seven, day=SESSIONS[6])
    assert r7["bars_75"] == 35 and r7["tech_rating_75"] is not None
    assert T.rating_75(None)["tech_rating_75"] is None and T.rating_75(pd.DataFrame())["bars_75"] == 0


def test_the_rating_follows_a_planted_trend():
    end = _at(SESSIONS[-1], "15:30")
    up = T.rating_75(_trend(SESSIONS, step=0.05), now=end, day=SESSIONS[-1])
    assert up["tech_rating_75_label"] in ("BUY", "STRONG_BUY") and up["tech_rating_75"] > 0.1
    assert up["supertrend_dir_75"] == 1 and up["bar_75_end"] == f"{SESSIONS[-1]} 15:30" and up["bars_75"] == 70
    down = T.rating_75(_trend(SESSIONS, step=-0.05), now=end, day=SESSIONS[-1])
    assert down["tech_rating_75_label"] in ("SELL", "STRONG_SELL") and down["supertrend_dir_75"] == -1
    flip = pd.concat([_trend(SESSIONS[:10], step=0.05),
                      _trend(SESSIONS[10:], start=float(_trend(SESSIONS[:10], step=0.05)["close"].iloc[-1]), step=-0.3)])
    assert T.rating_75(flip, now=end, day=SESSIONS[-1])["tech_rating_75"] < up["tech_rating_75"], "turns with the chart"


def test_the_rating_is_only_given_for_the_daily_bars_own_session():
    df = _trend(SESSIONS[:-1])                       # the 15-minute fetch failed on the last session
    stale = T.rating_75(df, now=_at(SESSIONS[-1], "15:30"), day=SESSIONS[-1])
    assert stale["tech_rating_75"] is None and stale["bars_75"] == 65, "yesterday's chart is not passed off as today's"
    full = _trend(SESSIONS)
    cut = T.rating_75(full, now=_at(SESSIONS[-2], "15:30"), day=SESSIONS[-2])
    assert cut["bar_75_end"] == f"{SESSIONS[-2]} 15:30" and cut["bars_75"] == 65, "no look-ahead past `now`"
    assert cut == T.rating_75(full[full.index.normalize() <= pd.Timestamp(SESSIONS[-2])], day=SESSIONS[-2])


# ── agreement ──────────────────────────────────────────────────────────────

LABELS = ("STRONG_BUY", "BUY", "NEUTRAL", "SELL", "STRONG_SELL")


@pytest.mark.parametrize("daily", LABELS + (None,))
@pytest.mark.parametrize("m75", LABELS + (None,))
def test_agreement_truth_table(daily, m75):
    up, down = ("BUY", "STRONG_BUY"), ("SELL", "STRONG_SELL")
    want = (None if daily is None or m75 is None else "BULL" if daily in up and m75 in up
            else "BEAR" if daily in down and m75 in down else "MIXED")
    assert T.mtf_alignment(daily, m75) == want


def _daily(closes, end):
    c = np.asarray(closes, dtype=float)
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.004, "low": np.minimum(o, c) * 0.996, "close": c,
                         "volume": np.full(len(c), 1e5)}, index=pd.bdate_range(end=end, periods=len(c)))


def test_the_snapshot_carries_the_75_minute_rating_and_the_agreement():
    up_d, down_d = _daily(np.linspace(100, 200, 300), SESSIONS[-1]), _daily(np.linspace(200, 100, 300), SESSIONS[-1])
    up15, down15 = _trend(SESSIONS, step=0.05), _trend(SESSIONS, step=-0.05)
    s = T.snapshot(up_d, bars15=up15)
    assert s["tech_rating_75_label"] in ("BUY", "STRONG_BUY") and s["mtf_alignment_75"] == "BULL"
    assert s["bar_75_end"] == f"{SESSIONS[-1]} 15:30" and s["rsi_14_75"] is not None and s["supertrend_dir_75"] == 1
    assert T.snapshot(up_d, bars15=down15)["mtf_alignment_75"] == "MIXED"
    assert T.snapshot(down_d, bars15=down15)["mtf_alignment_75"] == "BEAR"
    none = T.snapshot(up_d)
    assert none["tech_rating_75"] is None and none["mtf_alignment_75"] is None and none["mtf_alignment"] == "BULL"
    assert T.snapshot(up_d, bars15=_trend(SESSIONS[:-1]))["mtf_alignment_75"] is None, "stale 15-minute bars"


# ── stored nightly, screened ──────────────────────────────────────────────

@pytest.fixture
def db(temp_db):
    from db.schema import get_connection, init_db
    from research import screener as SC
    from research import tech_signals as S
    init_db()
    SC.clear_cache()
    conn = get_connection()
    S.ensure_tables(conn)
    yield conn
    conn.close()
    SC.clear_cache()


def _store15(conn, sym, df, interval=15):
    for t, r in df.iterrows():
        conn.execute("INSERT INTO intraday_bars (symbol, ts, interval_min, open, high, low, close, volume, source) "
                     "VALUES (?,?,?,?,?,?,?,?,'dhan')",
                     (sym, t.strftime("%Y-%m-%d %H:%M:%S"), interval, r.open, r.high, r.low, r.close, int(r.volume)))
    conn.commit()


def test_the_2030_run_stores_the_75_minute_rating_from_15_minute_bars_only(db):
    from research import screener as SC
    from research import tech_signals as S
    from utils.trading_calendar import is_trading_day
    as_of = dt.date(2026, 10, 6)
    days, d = [], as_of
    while len(days) < 12:
        if is_trading_day(d):
            days.append(d)
        d -= dt.timedelta(days=1)
    days.reverse()
    daily = _daily(np.linspace(100, 170, 331), as_of)
    for t, r in daily.iterrows():
        db.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) VALUES (?,?,?,?,?,?,?,"
                   "'dhan')", ("UPCO", str(t.date()), r.open, r.high, r.low, r.close, r.volume))
    db.commit()
    b15 = _trend(days, start=160.0, step=0.03)
    _store15(db, "UPCO", b15)
    crash = b15.iloc[-60:].copy()
    crash[["open", "high", "low", "close"]] = 10.0
    _store15(db, "UPCO", crash.set_axis(crash.index + pd.Timedelta(minutes=1)), interval=1)   # 1-minute tick bars
    out = S.run_technical(["UPCO"], as_of=as_of, conn=db)
    assert out["rated_75"] == 1
    snap = S.latest_snapshot(db, ["UPCO"])["UPCO"]
    want = T.rating_75(b15, now=_at(as_of, "15:30"), day=as_of)
    assert snap["tech_rating_75"] == want["tech_rating_75"] and snap["tech_rating_75_label"] in ("BUY", "STRONG_BUY")
    assert snap["mtf_alignment_75"] == "BULL" and snap["bar_75_end"] == f"{as_of} 15:30", "1-minute bars never mix in"
    rows = {r["symbol"]: r for r in SC.build_snapshot(db, as_of=as_of)}
    assert rows["UPCO"]["mtf_alignment_75"] == "BULL" and rows["UPCO"]["tech_rating_75"] == want["tech_rating_75"]


def test_old_snapshot_table_gets_the_75_minute_columns(tmp_path):
    import sqlite3
    from db.schema_w39 import W39_COLUMNS
    from research import tech_signals as S
    want = {"tech_rating_75", "tech_rating_75_label", "rsi_14_75", "supertrend_dir_75", "bar_75_end", "mtf_alignment_75"}
    assert want <= set(W39_COLUMNS["technical_snapshot"]) and want <= set(S.SNAP_COLS)
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute("CREATE TABLE technical_snapshot (symbol TEXT NOT NULL, date DATE NOT NULL, tech_rating REAL, "
                 "PRIMARY KEY (symbol, date))")
    conn.commit()
    S.ensure_tables(conn)
    assert want <= {r[1] for r in conn.execute("PRAGMA table_info(technical_snapshot)").fetchall()}
    conn.close()


def test_screener_fields_and_the_preset():
    from research import screener as SC
    rows = [{"symbol": "A", "mtf_alignment_75": "BULL", "tech_rating_75": 0.4, "tech_rating_75_label": "BUY"},
            {"symbol": "B", "mtf_alignment_75": "MIXED", "tech_rating_75": 0.6, "tech_rating_75_label": "STRONG_BUY"},
            {"symbol": "C", "mtf_alignment_75": "BULL", "tech_rating_75": 0.7, "tech_rating_75_label": "STRONG_BUY"},
            {"symbol": "D", "mtf_alignment_75": None, "tech_rating_75": None}]
    p = {x["key"]: x for x in SC.PRESETS}["t_75m_daily_bull"]
    assert p["name"] == "75-minute and daily both bullish" and p["group"] == "technical"
    r = SC.run_screen(None, p["query"], p["sort"], rows=rows)
    assert [x["symbol"] for x in r["rows"]] == ["C", "A"] and "tech_rating_75" in r["columns"]
    assert SC.field_key("mtf_75") == "mtf_alignment_75" and SC.field_key("rating_75m") == "tech_rating_75"
    assert {"tech_rating_75", "tech_rating_75_label", "mtf_alignment_75", "rsi_14_75",
            "supertrend_dir_75"} <= set(SC.FIELDS)
    assert SC.FIELDS["tech_rating_75"]["group"] == "Technical"
    strong = SC.run_screen(None, 'tech_rating_75_label = "STRONG_BUY"', rows=rows)["rows"]
    assert sorted(x["symbol"] for x in strong) == ["B", "C"]
