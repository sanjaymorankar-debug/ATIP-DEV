"""
research/session_ab_replay.py: the replay engine's exit mechanics on hand-made bars, its agreement with the 15 Nov 2026
evidence pack's engine, and -- when the backup of 15 Nov 2026 is on this machine -- the pack's numbers.

Behaviour tests only: nothing here says whether an exit is profitable.
"""

import importlib.util
import math
import os
import sys
from collections import namedtuple
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research import session_ab_replay as R

ROOT = Path(__file__).resolve().parents[1]
PACK_DIRS = [ROOT / ".ux_build/kite-gap-2026-11-15/scripts", Path.home() / "ATIP/.ux_build/kite-gap-2026-11-15/scripts"]
BACKUP = "atip-scheduled-20261115-191511.db"
DB_CANDIDATES = [os.environ.get("ATIP_REPLAY_DB") or "", ROOT / "atip_data/backups" / BACKUP,
                 Path.home() / "ATIP/atip_data/backups" / BACKUP]
EXACT = R.Fills(0.0, 0.0, 0.0, True, "exact")           # no slippage: every price is the level itself


def _bars(rows, sym="X"):
    """rows: [(ts, open, high, low, close), ...]"""
    f = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"])
    f["ts"] = pd.to_datetime(f["ts"])
    f["symbol"] = sym
    return R.Bars(f)


def _days(*ohlc, start="2026-11-02"):
    """One bar a weekday at 09:15 from `start` (the entry day's bar first)."""
    ds = pd.bdate_range(start, periods=len(ohlc))
    return _bars([(d + pd.Timedelta(hours=9, minutes=15), *x) for d, x in zip(ds, ohlc)])


def _one(bars, spec, fills=EXACT, entry=100.0, at="2026-11-02 09:20", **kw):
    return R.simulate(bars, "X", at, entry, spec, fills=fills, **kw)


# ── costs ───────────────────────────────────────────────────────────────────────────────────

def test_charges_match_a_real_session_cycle():
    # session_cycles id 1 (S1 PNBHOUSING): buy 24,022.90, sell 23,501.94, charges 68.78
    assert R.charges(24022.9, 23501.94) == pytest.approx(68.78, abs=0.005)
    assert R.charges(10000.0, 0.0) == pytest.approx(0.001 * 1e4 + 0.00015 * 1e4 + 1.18 * 0.0000297 * 1e4
                                                    + 1.18 * 0.000001 * 1e4)   # no DP charge without a sale


def _pack_module(name, monkeypatch=None):
    d = next((p for p in PACK_DIRS if (p / f"{name}.py").exists()), None)
    if d is None:
        pytest.skip("the 15 Nov 2026 evidence pack (.ux_build/kite-gap-2026-11-15/scripts) is not on this machine")
    if name == "engine":                         # engine.py does `from common import charges`
        monkeypatch.setitem(sys.modules, "common", _pack_module("common"))
    spec = importlib.util.spec_from_file_location(f"_pack_{name}", d / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pack_exits():
    d = next((p for p in PACK_DIRS if (p / "exits.py").exists()), None)
    if d is None:
        pytest.skip("the evidence pack is not on this machine")
    ns = {}
    exec((d / "exits.py").read_text(), ns)
    return ns["EXITS"]


def test_charges_equal_the_pack_common_charges():
    common = _pack_module("common")
    rng = np.random.default_rng(7)
    for B, S in zip(rng.uniform(1e3, 5e5, 200), rng.uniform(0, 5e5, 200)):
        assert R.charges(B, S) == common.charges(B, S)
    assert R.charges(24000.0, 0.0) == common.charges(24000.0, 0.0)


# ── levels and specs ────────────────────────────────────────────────────────────────────────

def test_atr_levels_clamp_and_fall_back_to_the_floor():
    a = R.Atr(2, 2, 8)
    assert (a.pct(3.0), a.pct(0.5), a.pct(5.0), a.pct(float("nan")), a.pct(None)) == (6.0, 2.0, 8.0, 2.0, 2.0)
    assert R.level_pct(("atr", 1, 1, 4), 1.5) == 1.5
    assert R.level_pct(3, 9.9) == 3.0
    assert R.level_pct({"type": "atr", "mult": 1.5, "min": 3, "max": 6}, 3.0) == 4.5


def test_pack_dicts_read_as_the_same_exit_specs():
    assert R.ExitSpec.from_dict(dict(sl=2, act=3, tr=1, tp=3, tp_after_days=15)) == R.EXITS[R.CURRENT]
    assert R.ExitSpec.from_dict(dict(sl=("atr", 2, 2, 8), act=("atr", 2, 2, 8), tr=("atr", 1, 1, 4))) == \
        R.EXITS["X9 ATR: SL2xATR, trail+2xATR/1xATR"]


def test_all_fourteen_pack_exits_are_ported():
    pack = _pack_exits()
    assert list(pack) == list(R.EXITS)
    for name, P in pack.items():
        assert R.ExitSpec.from_dict(P) == R.EXITS[name], name


def test_session_arm_exits_read_as_specs():
    control = {"stop": {"type": "pct", "value": 2.0},
               "trail": {"after": {"type": "pct", "value": 3.0}, "by": {"type": "pct", "value": 1.0}}, "hold_days": 15}
    assert R.ExitSpec.from_arm(control) == R.EXITS[R.CURRENT]
    bracket = {"stop": {"type": "pct", "value": 3.0}, "target_pct": 6.0, "risk_budget": 625.0}
    assert R.ExitSpec.from_arm(bracket) == R.EXITS["X11 bracket SL3/TP6"]
    kite = {"entry": "kite_retest", "stop": {"type": "pct", "value": 5.0}, "target_pct": 10.0, "max_days": 20}
    assert R.ExitSpec.from_dict(kite) == R.ExitSpec(5.0, target=10.0, max_days=20)
    atr = {"stop": {"type": "atr", "mult": 2.0, "min": 2.0, "max": 8.0},
           "trail": {"after": {"type": "atr", "mult": 2.0, "min": 2.0, "max": 8.0},
                     "by": {"type": "atr", "mult": 1.0, "min": 1.0, "max": 4.0}}, "hold_days": 15}
    s = R.ExitSpec.from_arm(atr)
    assert (s.stop, s.trail_after, s.trail_by, s.target, s.target_after_days) == \
        (R.Atr(2, 2, 8), R.Atr(2, 2, 8), R.Atr(1, 1, 4), R.Atr(2, 2, 8), 15)


def test_the_control_arm_is_todays_exit():
    try:
        from orders.arms import ARMS
    except Exception as e:                                   # noqa: BLE001
        pytest.skip(f"orders.arms not importable: {e}")
    if "S5" not in ARMS:
        pytest.skip("no S5 arm")
    assert R.ExitSpec.from_arm(ARMS["S5"]) == R.EXITS[R.CURRENT]


def test_s1_trails_and_sells_from_four_percent():
    s1, s2 = R.session_exit("S1"), R.session_exit("S2")
    assert (s1.trail_after, s1.target, s2.trail_after, s2.target) == (4.0, 4.0, 3.0, 3.0)


# ── the engine ──────────────────────────────────────────────────────────────────────────────

X1 = R.EXITS[R.CURRENT]
STOP2 = R.ExitSpec(2.0)


def test_the_entry_bar_itself_is_not_replayed():
    b = _bars([("2026-11-02 09:15", 100, 100.5, 90, 100), ("2026-11-02 09:30", 100, 101, 99.5, 100.5),
               ("2026-11-02 09:45", 100.5, 100.6, 97.9, 98.5)])
    s = _one(b, STOP2, at="2026-11-02 09:20")
    assert (s["reason"], s["exit_ts"]) == ("Stop", pd.Timestamp("2026-11-02 09:45"))


def test_stop_hit_sells_at_the_stop_with_stop_slippage():
    b = _bars([("2026-11-02 09:15", 100, 100, 100, 100), ("2026-11-02 09:30", 100, 100.5, 99, 99.5),
               ("2026-11-02 09:45", 99.5, 99.6, 97.5, 98)])
    s = _one(b, STOP2, fills=R.REALISTIC_FILLS)
    assert s["reason"] == "Stop" and not s["gap"]
    assert s["exit_px"] == pytest.approx(98.0 * (1 - 0.0015))
    assert _one(b, STOP2, fills=R.PACK_FILLS)["exit_px"] == pytest.approx(98.0 * (1 - 0.0005))


def test_a_bar_opening_through_the_stop_sells_at_its_open():
    b = _days((100, 100, 100, 100), (99, 99.5, 98.5, 99), (96, 97, 95, 96.5))
    s = _one(b, STOP2, fills=R.REALISTIC_FILLS)
    assert (s["reason"], s["gap"]) == ("Stop", True)
    assert s["exit_px"] == pytest.approx(96.0 * (1 - 0.0015))          # not the 98 stop: -4 %, not -2 %
    s = _one(b, STOP2, fills=R.Fills(5, 5, 15, gap_fill_at_open=False))
    assert (s["exit_px"], s["gap"]) == (pytest.approx(98.0 * (1 - 0.0015)), False)


def test_a_bar_that_opens_above_the_stop_and_falls_through_it_sells_at_the_stop():
    # BHEL 6 Nov 2026: the 09:15 bar opened 415.90 over the 410.78 stop and fell to 400.75 -- not a gap
    b = _days((100, 100, 100, 100), (99.5, 99.6, 95.0, 96.0))
    s = _one(b, STOP2)
    assert (s["reason"], s["exit_px"], s["gap"]) == ("Stop", pytest.approx(98.0), False)


def test_trail_arms_at_plus_three_and_follows_the_high_by_one():
    b = _days((100, 100, 100, 100), (100, 104, 99.5, 103.8), (103.8, 104.2, 103.5, 104), (104, 104.1, 102.5, 102.8))
    s = _one(b, X1)
    assert s["reason"] == "Trail"
    assert s["exit_px"] == pytest.approx(104.2 * 0.99)
    assert s["mfe"] == pytest.approx(4.2)


def test_the_x1_sale_at_plus_three_waits_fifteen_days():
    spec = R.ExitSpec(stop=2.0, target=3.0, target_after_days=15)
    ohlc = ([(100, 100, 100, 100), (100, 103.5, 99.5, 101)] + [(101, 102, 100.5, 101)] * 9
            + [(101, 103.2, 100.8, 102), (102, 103.2, 101.8, 102.5)])
    s = _one(_days(*ohlc), spec)                                       # entry 2 Nov 09:20
    assert s["reason"] == "Target"
    assert s["exit_ts"] == pd.Timestamp("2026-11-18 09:15")            # 3 Nov's 103.5 and 17 Nov's (14 days) did not sell
    assert s["exit_px"] == pytest.approx(103.0)


def test_a_bracket_target_is_live_at_once_and_a_gap_above_it_sells_at_the_open():
    x10 = R.EXITS["X10 bracket SL2/TP3"]
    s = _one(_days((100, 100, 100, 100), (100, 103.5, 99.5, 101)), x10, fills=R.REALISTIC_FILLS)
    assert (s["reason"], s["exit_px"]) == ("Target", pytest.approx(103.0 * (1 - 0.0005)))   # 5 bps, not 15
    s = _one(_days((100, 100, 100, 100), (104, 105, 103.5, 104.5)), x10)
    assert (s["reason"], s["exit_px"]) == ("Target", pytest.approx(104.0))


def test_a_bar_touching_stop_and_target_counts_as_the_stop():
    s = _one(_days((100, 100, 100, 100), (100, 103.5, 97.5, 101)), R.EXITS["X10 bracket SL2/TP3"])
    assert s["reason"] == "Stop"


def test_time_exit_sells_at_the_close_of_the_nth_session_after_entry():
    spec = R.ExitSpec(stop=10.0, max_days=3)
    s = _one(_days(*[(100, 101, 99, 100)] * 2 + [(100, 101, 99, 100.5), (100.5, 101, 99, 101.25), (101, 102, 100, 101)]),
             spec, fills=R.REALISTIC_FILLS)
    assert (s["reason"], s["exit_ts"]) == ("Time", pd.Timestamp("2026-11-05 09:15"))
    assert s["exit_px"] == pytest.approx(101.25 * (1 - 0.0005))


def test_still_held_at_the_end_is_marked_at_the_last_close_without_slippage():
    s = _one(_days((100, 100, 100, 100), (100, 101, 99, 100.7)), STOP2, fills=R.REALISTIC_FILLS)
    assert (s["reason"], s["exit_px"]) == ("OPEN", 100.7)


def test_breakeven_lifts_the_stop_over_the_entry():
    x2 = R.EXITS["X2 SL2 trail+3/1 + BE@1.5"]
    s = _one(_days((100, 100, 100, 100), (100, 101.6, 99.8, 101), (101, 101.2, 100.2, 100.5)), x2)
    assert (s["reason"], s["exit_px"]) == ("Stop", pytest.approx(100.3))


def test_a_partial_leg_then_the_trail():
    spec = R.ExitSpec(stop=5.0, trail_after=6.0, trail_by=3.0, partial=(0.5, 6.0))
    s = _one(_days((100, 100, 100, 100), (100, 107, 99.5, 106.5), (106.5, 106.6, 103, 103.5)), spec)
    assert s["reason"] == "Trail"
    assert s["exit_px"] == pytest.approx(0.5 * 106.0 + 0.5 * 107.0 * 0.97)


def test_atr_stop_uses_the_trade_atr():
    x9 = R.EXITS["X9 ATR: SL2xATR, trail+2xATR/1xATR"]
    b = _days((100, 100, 100, 100), (100, 100.5, 94.5, 95))
    assert _one(b, x9, atr_pct=3.0)["reason"] == "OPEN"                 # stop 6 %: 94 not reached
    assert _one(b, x9, atr_pct=2.0)["exit_px"] == pytest.approx(96.0)   # stop 4 %


def test_net_is_after_charges_on_the_real_quantity():
    s = _one(_days((100, 100, 100, 100), (100, 100.5, 97.5, 98)), STOP2, qty=50)
    assert s["qty"] == 50 and s["B"] == 5000.0
    assert s["net"] == pytest.approx(50 * 98.0 - 5000.0 - R.charges(5000.0, 4900.0))
    assert _one(_days((100, 100, 100, 100), (100, 100.5, 97.5, 98)), STOP2)["qty"] == 250   # Rs 25,000 / 100


def test_engine_matches_the_pack_engine_on_random_paths(monkeypatch):
    eng = _pack_module("engine", monkeypatch)
    pack_exits = _pack_exits()
    rng = np.random.default_rng(11)
    for trial in range(6):
        days = pd.bdate_range("2026-10-01", periods=18)
        rows, c = [], 100.0
        for d in days:
            gap = rng.normal(0, 0.02) if rng.random() < 0.3 else 0.0
            for k in range(25):
                o = c * (1 + gap) if k == 0 else c
                c = o * (1 + rng.normal(0.0004, 0.006))
                rows.append((d + pd.Timedelta(minutes=15 * k + 555), o, max(o, c) * (1 + abs(rng.normal(0, 0.002))),
                             min(o, c) * (1 - abs(rng.normal(0, 0.002))), c))
        f = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"])
        f["symbol"] = "X"
        mine, theirs = R.Bars(f), eng.Bars(f)
        e = int(rng.integers(0, 60))
        at, entry, atrp = f["ts"].iloc[e] + pd.Timedelta(minutes=4), float(f["close"].iloc[e]), float(rng.uniform(1, 5))
        for name, P in pack_exits.items():
            a = R.simulate(mine, "X", at, entry, R.EXITS[name], atr_pct=atrp, fills=R.PACK_FILLS)
            b = eng.simulate(theirs, "X", at, entry, P, atrp=atrp)
            assert (a["reason"], a["exit_ts"], a["qty"]) == (b["reason"], b["exit_ts"], b["qty"]), (trial, name)
            assert a["exit_px"] == pytest.approx(b["exit_px"], rel=1e-12), (trial, name)
            assert a["net"] == pytest.approx(b["net"], rel=1e-9, abs=1e-9), (trial, name)


# ── calibration and population plumbing ────────────────────────────────────────────────────

def _data(bars_rows, cycles=None, daily=None, scores=None, rules=None):
    f = pd.DataFrame(bars_rows, columns=["symbol", "ts", "open", "high", "low", "close"])
    f["ts"] = pd.to_datetime(f["ts"])
    f["volume"] = 1000.0
    empty = pd.DataFrame(columns=["symbol", "date", "open", "high", "low", "close", "volume"])
    return R.ReplayData(daily if daily is not None else empty, f, scores if scores is not None else pd.DataFrame(),
                        cycles if cycles is not None else pd.DataFrame(), rules if rules is not None else pd.DataFrame())


def test_calibration_replays_each_cycle_with_its_session_exit():
    bars = [("A", "2026-11-03 09:15", 100, 100, 100, 100), ("A", "2026-11-03 09:30", 100, 100.5, 97.5, 98),
            ("B", "2026-11-03 09:15", 200, 200, 200, 200), ("B", "2026-11-03 09:30", 200, 206.2, 199.8, 206),
            ("B", "2026-11-03 09:45", 206, 206.1, 203.5, 204)]
    cyc = pd.DataFrame([dict(session_id="S2", symbol="A", qty=10.0, buy_price=100.0, sell_price=97.8, net_pnl=-25.0,
                             exit_reason="Stop", opened_at="2026-11-03T09:16:00", closed_at="2026-11-03T09:40:00"),
                        dict(session_id="S2", symbol="B", qty=5.0, buy_price=200.0, sell_price=204.0, net_pnl=15.0,
                             exit_reason="Trail", opened_at="2026-11-03T09:16:00", closed_at="2026-11-03T09:50:00")])
    T = R.calibrate(_data(bars, cyc), R.REALISTIC_FILLS, since="2026-11-01")
    assert list(T["sim_reason"]) == ["Stop", "Trail"]
    assert T["sim_exit"].iloc[0] == pytest.approx(round(98.0 * 0.9985, 2))
    assert T["sim_exit"].iloc[1] == pytest.approx(round(206.2 * 0.99 * 0.9985, 2))   # armed at +3 %, trails 1 %
    cyc.loc[1, "session_id"] = "S1"                                                    # S1 arms only at +4 %
    assert R.calibrate(_data(bars, cyc), R.REALISTIC_FILLS, since="2026-11-01")["sim_reason"].iloc[1] == "OPEN"
    s = R.calibration_summary(T)
    assert (s["n"], s["reason_match"]) == (2, 2)


def test_entry_modes_of_the_population():
    b = _days((100, 100.4, 98.8, 99.5), (101.0, 102.5, 100.2, 102), (99.0, 99.5, 97.5, 98))
    Cand = namedtuple("Cand", "sym nday c h l")
    cal = list(pd.bdate_range("2026-11-02", periods=3))
    f = R.Fills(10.0, 5.0, 15.0)
    nd = pd.Timestamp("2026-11-03")
    ts, px = R.entry_fill(b, Cand("X", nd, 100.0, 101.0, 99.0), "E0", cal, f)       # open 101 > 100.5, low reaches it
    assert (ts, px) == (pd.Timestamp("2026-11-03 09:15"), pytest.approx(100.5 * 1.001))
    assert R.entry_fill(b, Cand("X", nd, 101.0, 101.5, 99.0), "E0", cal, f)[1] == pytest.approx(101.0 * 1.001)
    assert R.entry_fill(b, Cand("X", nd, 102.5, 103.0, 99.0), "E1", cal, f) is None    # 101 is > 1 % under 102.5
    assert R.entry_fill(b, Cand("X", nd, 100.0, 101.5, 99.0), "E2", cal, f)[1] == pytest.approx(101.5 * 1.001)
    ts, px = R.entry_fill(b, Cand("X", nd, 100.0, 101.0, 99.0), "E3", cal, f)         # 4 Nov reaches the low
    assert (ts, px) == (pd.Timestamp("2026-11-04 09:15"), pytest.approx(99.0 * 1.001))


def test_rank_fill_candidates_buy_first_then_hold_by_score_with_the_hold_filters():
    days = pd.bdate_range("2026-10-01", periods=25)
    daily = pd.DataFrame([dict(symbol=s, date=d, open=100.0, high=101.0, low=99.0, close=100.0, volume=1e6)
                          for s in "ABCDE" for d in days])
    day = days[22]
    sc = pd.DataFrame([dict(symbol="A", date=day, signal="BUY", atip_score=60, cri=10),
                       dict(symbol="B", date=day, signal="HOLD", atip_score=90, cri=10),
                       dict(symbol="C", date=day, signal="HOLD", atip_score=95, cri=80),     # CRI >= 75: skipped
                       dict(symbol="D", date=day, signal="BUY", atip_score=70, cri=10),
                       dict(symbol="E", date=day, signal="SELL", atip_score=99, cri=10)])
    C = R.rank_fill_candidates(_data([], daily=daily, scores=sc), days[20], days[-1])
    assert list(C["sym"]) == ["D", "A", "B"]
    assert set(C["nday"]) == {days[23]}
    assert len(R.rank_fill_candidates(_data([], daily=daily, scores=sc), days[20], days[-1], per_day=2)) == 2


# ── the pack's numbers on the backup of 15 Nov 2026 ─────────────────────────────────────────

@pytest.fixture(scope="module")
def backup():
    p = next((Path(c) for c in DB_CANDIDATES if c and Path(c).exists()), None)
    if p is None:
        pytest.skip(f"backup {BACKUP} not on this machine (set ATIP_REPLAY_DB)")
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return R.load(p)


def test_reproduces_the_pack_calibration(backup):
    T = R.calibrate(backup, R.PACK_FILLS)
    s = R.calibration_summary(T)
    assert (s["n"], s["reason_match"], s["corr"]) == (34, 34, 0.968)
    assert s["sum_actual"] == pytest.approx(4800.55) and s["sum_sim"] == pytest.approx(4069.89)
    bhel = T[T["sym"] == "BHEL"].iloc[0]
    assert (bhel["act_exit"], bhel["sim_exit"]) == (408.2, 410.57)


def test_realistic_fills_keep_every_exit_reason(backup):
    T = R.calibrate(backup, R.REALISTIC_FILLS)
    assert R.calibration_summary(T)["reason_match"] == 34
    assert T[T["sym"] == "BHEL"].iloc[0]["sim_exit"] == pytest.approx(410.16)


def test_reproduces_the_pack_whatif(backup):
    S = R.whatif_summary(R.whatif(backup, fills=R.PACK_FILLS))
    assert (S.loc[R.CURRENT, "n"], S.loc[R.CURRENT, "win"], S.loc[R.CURRENT, "total_net"]) == (44, 45.5, 3064)
    assert S.loc["X9 ATR: SL2xATR, trail+2xATR/1xATR", "total_net"] == 14147
    assert S.loc["X8 SL5 trail+5/2.5 (Kite SL)", "total_net"] == 11957


def test_reproduces_the_pack_population(backup):
    P = R.population(backup, "2026-07-28", "daily", fills=R.PACK_FILLS, entries=("E0",))
    S = R.population_summary(P, "2026-09-25").set_index("exit")
    assert S.loc[R.CURRENT, "n"] == 446
    assert (S.loc[R.CURRENT, "win"], S.loc[R.CURRENT, "avg_net"]) == (31.6, -0.895)
    assert (S["avg_net"] < 0).all() and len(S) == 14
    assert math.isclose(S["avg_net"].max(), -0.822) and math.isclose(S["avg_net"].min(), -2.297)
