"""
research/kite_retest_study.py: setup detection, the resting-buy backtest and the breadth gate on hand-made bars; parity
with the live scanner (strategy/breakout_retest.py) and the production breadth (scores/breadth.py); and -- when the
backup of 15 Nov 2026 is on this machine -- the evidence pack's numbers.
"""

import os
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research import kite_retest_study as K
from research.session_ab_replay import PACK_FILLS, REALISTIC_FILLS, Fills, charges

ROOT = Path(__file__).resolve().parents[1]
BACKUP = "atip-scheduled-20261115-191511.db"
DB_CANDIDATES = [os.environ.get("ATIP_REPLAY_DB") or "", ROOT / "atip_data/backups" / BACKUP,
                 Path.home() / "ATIP/atip_data/backups" / BACKUP]
EXACT = Fills(0.0, 0.0, 0.0, True, "exact")
B, T = 30, 35            # the breakout and first-retest sessions of _setup_bars()


def _frame(ohlcv, sym="X", start="2026-01-01"):
    ds = pd.bdate_range(start, periods=len(ohlcv))
    f = pd.DataFrame(ohlcv, columns=["open", "high", "low", "close", "volume"])
    f.insert(0, "date", ds)
    f.insert(0, "symbol", sym)
    f["series"] = "EQ"
    return K.prepare_daily(f)


def _flat(n, c0=100.0):
    """n quiet sessions: closes 100.2 / 99.8, highs ~100.5, lows ~99.5, volume 1,000."""
    out, prev = [], c0
    for k in range(n):
        c = c0 + (0.2 if k % 2 else -0.2)
        out.append((prev, max(prev, c) + 0.3, min(prev, c) - 0.3, c, 1000.0))
        prev = c
    return out


def _setup_bars(breakout_vol=4000.0, green=True, retest=102.6, tail=()):
    """A breakout at session 30 (high 104.5, low 100.2, mid 102.35), a rally to 107, a close within 1 % of the mid
    at session 35."""
    o, c = (100.5, 104.0) if green else (104.0, 100.5)
    bars = _flat(B) + [(o, 104.5, 100.2, c, breakout_vol),
                       (104.0, 106.0, 103.8, 105.5, 1500.0), (105.5, 107.0, 105.0, 106.5, 1500.0),
                       (106.5, 106.8, 104.8, 105.0, 1200.0), (105.0, 105.2, 104.2, 104.5, 1200.0),
                       (104.5, 104.6, 102.4, retest, 1200.0)]
    return bars + list(tail)


# ── setups ──────────────────────────────────────────────────────────────────────────────────

def test_breakout_needs_a_green_3x_volume_close_above_the_20_day_high():
    f = _frame(_setup_bars())
    brk, av, ph = K.breakout_mask(*(f[k].to_numpy() for k in ("open", "high", "close", "volume")))
    assert list(np.flatnonzero(brk)) == [B]
    assert av[B] == pytest.approx(1000.0) and ph[B] == pytest.approx(100.5)
    for bars in (_setup_bars(breakout_vol=2900.0), _setup_bars(green=False)):
        f = _frame(bars)
        assert not K.breakout_mask(*(f[k].to_numpy() for k in ("open", "high", "close", "volume")))[0].any()


def test_setup_is_the_first_retest_day_with_the_breakout_low_as_the_buy():
    S = K.find_setups(_frame(_setup_bars()))
    assert len(S) == 1
    s = S.iloc[0]
    days = pd.bdate_range("2026-01-01", periods=40)
    assert (s["bdate"], s["tdate"], s["age"]) == (days[B], days[T], T - B)
    assert (s["b_low"], s["b_high"], s["max_after"]) == (100.2, 104.5, 107.0)
    assert s["mid"] == pytest.approx(102.35)
    assert s["volx"] == pytest.approx(4.0)


def test_no_setup_without_a_higher_high_after_the_breakout():
    bars = _setup_bars()
    bars[B + 1:B + 6] = [(104.0, 104.4, 103.8, 104.2, 1500.0), (104.2, 104.5, 103.9, 104.3, 1500.0),
                         (104.3, 104.4, 103.6, 103.8, 1200.0), (103.8, 104.0, 103.2, 103.5, 1200.0),
                         (103.5, 103.6, 102.4, 102.6, 1200.0)]    # 104.5 equals the breakout high: not above it
    assert K.find_setups(_frame(bars)).empty


def test_no_setup_when_the_close_never_comes_back_to_the_mid():
    assert K.find_setups(_frame(_setup_bars(retest=103.5))).empty    # 1.1 % above the mid


def test_no_retest_later_than_20_sessions():
    bars = _setup_bars(retest=104.0)[:B + 5] + [(104.0, 104.6, 103.8, 104.2, 1000.0)] * 16 + [(104.2, 104.3, 102.2, 102.5, 1000.0)]
    assert len(bars) - 1 - B == 21
    assert K.find_setups(_frame(bars)).empty
    bars = bars[:-2] + [(104.2, 104.3, 102.2, 102.5, 1000.0)]        # the 20th session: still a setup
    assert len(K.find_setups(_frame(bars))) == 1


def test_a_split_sized_jump_restarts_the_history():
    bars = _setup_bars()
    bars[12] = tuple(x * 0.5 if i < 4 else x for i, x in enumerate(bars[12]))   # one -50 % print, then back
    S = K.find_setups(_frame(bars))
    assert S.empty                       # the segments are too short for a 20-session look-back before session 30


# ── the backtest ────────────────────────────────────────────────────────────────────────────

def _bt(path, variant=K.Variant(), fills=EXACT, b_low=100.0, max_after=112.0, extra_setups=()):
    """path: OHLC sessions after the setup day (session 0 = the setup day itself)."""
    f = _frame([(*x, 1000.0) for x in path])
    d0 = f["date"].iloc[0]
    S = pd.DataFrame([dict(sym="X", tdate=d0, bdate=d0 - pd.Timedelta(days=7), b_low=b_low, max_after=max_after),
                      *extra_setups])
    return K.backtest(S, f, variant, fills)


def test_resting_buy_fills_at_the_level_and_at_the_open_below_it():
    R = _bt([(103, 104, 102, 103), (102, 102.5, 99.5, 101), (101, 101, 100.5, 100.8)])
    assert (R["filled"].iloc[0], R["entry"].iloc[0], R["fdate"].iloc[0]) == (1, 100.0, pd.Timestamp("2026-01-02"))
    R = _bt([(103, 104, 102, 103), (98.0, 99, 97.5, 98.5)])
    assert R["entry"].iloc[0] == 98.0


def test_resting_buy_expires_after_ten_sessions_and_blocks_the_symbol_meanwhile():
    path = [(103, 104, 101, 103)] * 13
    second = dict(sym="X", tdate=pd.bdate_range("2026-01-01", periods=13)[5], bdate=pd.Timestamp("2025-12-20"),
                  b_low=102.0, max_after=110.0)
    R = _bt(path, extra_setups=[second])
    assert list(R["filled"]) == [0]                 # the second setup rested inside the first order's 10 sessions


def test_stop_target_and_time_exits():
    stop = _bt([(103, 104, 101, 103), (101, 101, 100, 100.5), (100.5, 100.6, 94.0, 95)])
    assert (stop["reason"].iloc[0], stop["exit"].iloc[0], stop["gap"].iloc[0]) == ("Stop", 95.0, False)
    tgt = _bt([(103, 104, 101, 103), (101, 101, 100, 100.5), (101, 111, 100.5, 110)])
    assert (tgt["reason"].iloc[0], tgt["exit"].iloc[0]) == ("Target", pytest.approx(110.0))
    calm = [(103, 104, 101, 103), (101, 101, 100, 100.5)] + [(101, 102, 100.5, 101.5)] * 25
    t = _bt(calm)
    assert (t["reason"].iloc[0], t["days"].iloc[0], t["exit"].iloc[0]) == ("Time", 20, 101.5)


def test_a_gap_through_the_stop_sells_at_the_open_with_stop_slippage():
    path = [(103, 104, 101, 103), (101, 101, 100, 100.5), (92.0, 93, 91, 92.5)]
    R = _bt(path, fills=REALISTIC_FILLS)
    assert (R["reason"].iloc[0], R["gap"].iloc[0]) == ("Stop", True)
    assert R["exit"].iloc[0] == pytest.approx(92.0 * (1 - 0.0015))
    R = _bt(path, fills=Fills(5, 5, 15, gap_fill_at_open=False))
    assert R["exit"].iloc[0] == pytest.approx(95.0 * (1 - 0.0015))
    tgt = _bt([(103, 104, 101, 103), (101, 101, 100, 100.5), (101, 111, 100.5, 110)], fills=REALISTIC_FILLS)
    assert tgt["exit"].iloc[0] == pytest.approx(110.0 * (1 - 0.0005))        # a target keeps 5 bps


def test_k4_trails_and_k6_buys_the_next_open():
    path = [(103, 104, 101, 103), (101, 101, 100, 100.5), (101, 108, 100.8, 107.5), (107.5, 107.6, 104.0, 104.5)]
    R = _bt(path, K.VARIANTS["K4 GTT, SL5, trail 3% from +5%, 40d"])
    assert (R["reason"].iloc[0], R["exit"].iloc[0]) == ("Stop", pytest.approx(108 * 0.97))
    R = _bt(path, K.VARIANTS["K6 market next open, SL5/TP10, 20d"], fills=PACK_FILLS)
    assert R["entry"].iloc[0] == pytest.approx(101 * 1.0005)


def test_net_is_after_charges_on_25000_rupees():
    R = _bt([(103, 104, 101, 103), (101, 101, 100, 100.5), (101, 111, 100.5, 110)])
    q = int(25000 // 100.0)
    assert R["net"].iloc[0] == pytest.approx(q * 110.0 - q * 100.0 - charges(q * 100.0, q * 110.0))


# ── breadth and the gate ────────────────────────────────────────────────────────────────────

def test_breadth_counts_closes_above_their_own_average_and_short_histories_as_not_above():
    days = pd.bdate_range("2026-01-01", periods=25)
    rows = [dict(symbol="UP", date=d, close=100.0 + k) for k, d in enumerate(days)]
    rows += [dict(symbol="DOWN", date=d, close=200.0 - k) for k, d in enumerate(days)]
    rows += [dict(symbol="NEW", date=d, close=50.0 + k) for k, d in enumerate(days[-5:])]   # 5 closes only
    br = K.breadth(pd.DataFrame(rows), windows=(20,))
    last = br.iloc[-1]
    assert (last["n"], last["above20"], last["breadth20"]) == (3, 1, pytest.approx(100 / 3))
    assert br.loc[days[18], "breadth20"] == 0.0                                  # no 20 closes yet anywhere
    assert br.loc[days[19], "breadth20"] == 50.0                                 # UP is above, DOWN is not


def test_the_gate_keeps_trades_by_their_setup_day_breadth():
    t = pd.DataFrame([dict(sym="A", tdate=pd.Timestamp("2026-03-02"), filled=1, net=10.0, netpct=0.4,
                           fdate=pd.Timestamp("2026-03-03")),
                      dict(sym="B", tdate=pd.Timestamp("2026-03-03"), filled=1, net=-5.0, netpct=-0.2,
                           fdate=pd.Timestamp("2026-03-04")),
                      dict(sym="C", tdate=pd.Timestamp("2026-03-03"), filled=0)])
    br = pd.DataFrame({"breadth20": [40.0, 70.0], "breadth50": [55.0, 45.0]},
                      index=pd.DatetimeIndex(["2026-03-02", "2026-03-03"], name="date"))
    fb = K.with_breadth(t, br)
    assert list(K.gate(fb, None)["sym"]) == ["A", "B"]                          # gate off: every filled trade
    assert list(K.gate(fb, "breadth50", 50)["sym"]) == ["A"]
    assert list(K.gate(fb, "breadth50", 50, above=False)["sym"]) == ["B"]
    assert list(K.gate(fb, "breadth20", 50)["sym"]) == ["B"]


# ── parity with the production modules ──────────────────────────────────────────────────────

def _candles(f):
    return [dict(date=d.strftime("%Y-%m-%d"), open=o, high=h, low=lo, close=c, volume=v)
            for d, o, h, lo, c, v in f[["date", "open", "high", "low", "close", "volume"]].itertuples(index=False)]


def _random_series(seed, n=160):
    """A random walk with a few high-volume breakouts, so setups and overlapping candidates occur."""
    rng = np.random.default_rng(seed)
    out, c = [], 100.0
    spikes = set(rng.choice(np.arange(22, n - 5), size=8, replace=False))
    for k in range(n):
        o = c
        if k in spikes:
            hi20 = max(x[1] for x in out[-20:])
            c = hi20 * (1 + rng.uniform(0.005, 0.03))
            o = c * (1 - rng.uniform(0.01, 0.03))
            v = 1000 * rng.uniform(3.5, 6)
        else:
            c = o * (1 + rng.normal(0, 0.012))
            v = 1000 * rng.uniform(0.7, 1.3)
        out.append((round(o, 2), round(max(o, c) * (1 + rng.uniform(0, 0.008)), 2),
                    round(min(o, c) * (1 - rng.uniform(0, 0.008)), 2), round(c, 2), round(v)))
    return _frame(out, start="2025-01-01")


def test_setups_agree_with_the_live_scanner():
    try:
        from strategy.breakout_retest import scan_symbol
    except ImportError as e:
        pytest.skip(f"strategy.breakout_retest not importable yet: {e}")
    checked = 0
    for f in [_frame(_setup_bars(tail=_flat(15, 101.0))), *(_random_series(s) for s in range(8))]:
        cs, dates = _candles(f), [d.strftime("%Y-%m-%d") for d in f["date"]]
        shown, first = {}, {}
        for t in range(len(cs)):
            r = scan_symbol("X", cs[:t + 1])
            if r:
                shown[t] = r
                first.setdefault(r["breakoutDate"], t)
        mine = {s.bdate.strftime("%Y-%m-%d"): s for s in K.find_setups(f).itertuples(index=False)}
        for bd, s in mine.items():                       # my first retest day: the scanner shows this setup or a newer one
            t = dates.index(s.tdate.strftime("%Y-%m-%d"))
            assert t in shown, (bd, t)
            r = shown[t]
            assert r["breakoutDate"] >= bd
            if r["breakoutDate"] == bd:
                assert (r["breakoutLow"], r["breakoutHigh"], r["maxPriceAfter"]) == (s.b_low, s.b_high, s.max_after)
                assert first[bd] == t
                checked += 1
        for bd, t in first.items():                      # the scanner's first day for a breakout is mine (or masked)
            assert bd in mine, bd
            mt = dates.index(mine[bd].tdate.strftime("%Y-%m-%d"))
            assert mt <= t and (mt == t or shown[mt]["breakoutDate"] > bd)
    assert checked >= 5


def test_breadth_agrees_with_scores_breadth(tmp_path):
    try:
        from scores import breadth as prod
    except ImportError as e:
        pytest.skip(f"scores.breadth not importable yet: {e}")
    if not hasattr(prod, "compute"):
        pytest.skip("scores.breadth has no compute()")
    rng = np.random.default_rng(3)
    days = pd.bdate_range("2026-01-01", periods=170)
    rows = []
    for k, sym in enumerate(["A", "B", "C", "D", "E", "F", "G"]):
        start = 140 if sym == "G" else 0                  # G: 30 closes -- never above its 50-day average
        c = 100.0
        for d in days[start:]:
            c *= 1 + rng.normal(0.0005 * (k - 3), 0.015)
            rows.append(dict(symbol=sym, date=d, close=round(c, 2)))
    con = sqlite3.connect(tmp_path / "breadth.db")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE ai_scores (symbol TEXT, date TEXT)")
    # prices_daily has an id (db/schema.py); scores.breadth keeps the newest row of a symbol-day by it (17 Nov, a439e41)
    con.execute("CREATE TABLE prices_daily (id INTEGER PRIMARY KEY, symbol TEXT, date TEXT, close REAL, series TEXT)")
    con.executemany("INSERT INTO ai_scores VALUES (?, ?)", [(s, "2026-08-01") for s in "ABCDEFG"])
    con.executemany("INSERT INTO prices_daily (symbol, date, close, series) VALUES (?, ?, ?, 'EQ')",
                    [(r["symbol"], r["date"].strftime("%Y-%m-%d"), r["close"]) for r in rows])
    con.commit()
    lo, hi = days[120].strftime("%Y-%m-%d"), days[-1].strftime("%Y-%m-%d")
    theirs = {r["date"]: r for r in prod.compute(con, lo, hi)}
    con.close()
    ours = K.breadth(pd.DataFrame(rows))
    ours = ours[(ours.index >= lo) & (ours.index <= hi)]
    assert len(theirs) == len(ours) == 50
    for d, r in ours.iterrows():
        t = theirs[d.strftime("%Y-%m-%d")]
        assert t["n"] == r["n"]
        assert t["breadth20"] == pytest.approx(r["breadth20"], abs=0.005)
        assert t["breadth50"] == pytest.approx(r["breadth50"], abs=0.005)


# ── the pack's numbers on the backup of 15 Nov 2026 ─────────────────────────────────────────

@pytest.fixture(scope="module")
def study():
    p = next((Path(c) for c in DB_CANDIDATES if c and Path(c).exists()), None)
    if p is None:
        pytest.skip(f"backup {BACKUP} not on this machine (set ATIP_REPLAY_DB)")
    d = K.load_daily(p)
    variants = {k: v for k, v in K.VARIANTS.items() if k[:2] in ("K1", "K2", "K4")}
    S, res = K.run(d, variants)
    br = K.breadth(d)
    return d, S, br, K.summarise(res, br, d["date"].max())


def _row(summ, k, gate="none", period="all"):
    x = summ[summ["variant"].str.startswith(k) & (summ["gate"] == gate) & (summ["period"] == period)]
    return x.iloc[0]


def test_reproduces_the_pack_setups_and_breadth(study):
    d, S, br, _ = study
    assert abs(len(S) - 4705) <= 0.05 * 4705
    b = br.loc[pd.Timestamp("2026-11-13")]
    assert (round(b["breadth20"], 1), round(b["breadth50"], 1)) == (80.7, 56.8)


def test_reproduces_k1_and_the_breadth50_gate(study):
    *_, summ = study
    k1 = _row(summ, "K1")
    assert abs(k1["n"] - 2349) <= 0.05 * 2349
    assert k1["avg"] == pytest.approx(0.67, abs=0.02) and k1["win"] == pytest.approx(42.3, abs=1.0)
    assert k1["fill_rate"] == pytest.approx(56, abs=1)
    assert _row(summ, "K1", period="last12m")["avg"] == pytest.approx(-0.08, abs=0.03)
    g = _row(summ, "K1", "b50>=50")
    assert abs(g["n"] - 1685) <= 0.05 * 1685 and g["avg"] == pytest.approx(0.97, abs=0.02)
    g12 = _row(summ, "K1", "b50>=50", "last12m")
    assert (g12["n"], g12["avg"]) == (177, pytest.approx(1.14, abs=0.02))
    assert _row(summ, "K4", "b50>=50")["avg"] == pytest.approx(0.62, abs=0.02)
    assert _row(summ, "K2", "b50>=50")["avg"] == pytest.approx(0.34, abs=0.02)
