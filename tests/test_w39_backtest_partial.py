"""
PF-06: the W2 backtest engine simulates partial position changes.

  ADD     increases a held position at the next open (weighted-average entry, costs
          accumulate on the position), priced, liquidity-capped and cash-checked like a
          BUY of that size; capped at sizing.max_position_pct; ignored when not held
  REDUCE  sells part of a held position at the next open like a SELL of that size and
          writes a "partial" trade row with the pro-rata entry costs; at or above the
          holding it closes the position
  adapter strategy versions' ADD / REDUCE decisions become those Signals, and the
          "REDUCE / ADD decisions are not simulated" bias warning is gone

Prices are hand-written: PAR follows an explicit path of (open, high, low, close) bars
and QQQ sits at 50. A scripted strategy emits a fixed list of Signals per session. Costs
are the flat model at 0.1% per leg and slippage is off unless a test says otherwise, so
every expected number below is worked out by hand in the comments.

Runs that never ADD or REDUCE must not change at all: test_buy_sell_only_runs_are_unchanged
pins digests of full results computed with the engine before PF-06.
"""

import hashlib
import json
import math
from types import SimpleNamespace

import pytest

START, END = "2025-01-01", "2025-03-31"
FLAT_COSTS = {"cost_model": "flat", "cost_overrides": {"flat_pct": 0.1}, "slippage": {"kind": "none", "value": 0}}
SIZING = {"risk_per_trade_pct": 0.5, "max_position_pct": 20, "max_positions": 5, "default_stop_pct": 5}
ROW_KEYS = ["symbol", "entry_date", "entry_price", "entry_ref_price", "qty", "exit_date", "exit_price",
            "exit_ref_price", "exit_reason", "gross_pnl", "costs", "net_pnl", "return_pct", "holding_sessions",
            "entry_reason"]


@pytest.fixture
def db(temp_db, tmp_path, monkeypatch):
    import backtest.config as bc
    from db.schema import init_db
    monkeypatch.setattr(bc, "CONFIG_PATH", tmp_path / "no_config.json")     # DEFAULTS, whatever config.json says
    init_db()
    return temp_db


def _days(start=START, end=END):
    from backtest.walkforward import trading_sessions
    return trading_sessions(start, end)


def _insert(rows):
    from db.schema import get_connection
    conn = get_connection()
    conn.executemany("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def _seed(path):
    """PAR: path[i] = (open, high, low, close) on session i, None = no bar that day; after
    the path it stays at its last close. QQQ: 50 every day. Volume never binds."""
    rows, last = [], None
    for i, d in enumerate(_days()):
        bar = path[i] if i < len(path) else (last, last, last, last)
        if bar is not None:
            rows.append(("PAR", str(d), *bar, 10_000_000))
            last = bar[3]
        rows.append(("QQQ", str(d), 50.0, 50.0, 50.0, 50.0, 10_000_000))
    _insert(rows)


def _flat(*prices):
    return [(p, p, p, p) for p in prices]


def _script(monkeypatch, plan):
    """A strategy that returns plan[i] (a list of Signals) after session i's close."""
    from backtest import strategies
    from backtest.strategy import Strategy
    by_date = {_days()[i]: sigs for i, sigs in plan.items()}

    class Scripted(Strategy):
        strategy_id = "scripted"
        version = "1"
        default_params = {}

        def on_bar(self, ctx):
            return list(by_date.get(ctx.as_of, []))

    monkeypatch.setitem(strategies.REGISTRY, "scripted", Scripted)


def _run(sid="scripted", universe=("PAR", "QQQ"), last=15, **kw):
    from backtest import engine, service
    from db.schema import get_connection
    req = {"strategy_id": sid, "universe": list(universe), "start": str(_days()[0]), "end": str(_days()[last]),
           "initial_capital": 100_000, "sizing": dict(SIZING), **FLAT_COSTS}
    req.update(kw)
    snap = service.resolve_config(req)
    conn = get_connection()
    try:
        return engine.run(snap, conn)
    finally:
        conn.close()


def _sig(side, sym="PAR", **kw):
    from backtest.strategy import Signal
    return Signal(sym, side, **kw)


def _events(res):
    return [e["event"] for e in res["events"]]


def _eq(res, i):
    return res["equity"][i]["equity"]


# ── ADD / REDUCE: hand-computed ────────────────────────────────────────────

def test_add_then_reduce_then_sell_hand_computed(db, monkeypatch):
    _seed([(100, 100, 100, 100), (100, 102, 100, 102), (104, 106, 104, 106), (110, 110, 108, 108)]
          + _flat(105))
    _script(monkeypatch, {0: [_sig("BUY", stop_price=95.0, reason="in")], 1: [_sig("ADD", quantity=50)],
                          2: [_sig("REDUCE", quantity=60)], 3: [_sig("SELL")]})
    res = _run()
    d = _days()
    # BUY: risk 0.5% of 100,000 = 500 over a 5.00 stop distance -> 100 shares (20% cap allows 200)
    #   s1 open 100: 100 x 100 = 10,000 + 10.00 costs; cash 89,990.00
    # ADD 50 at s2 open 104: 5,200 + 5.20; cash 84,784.80
    #   avg entry (10,000 + 5,200) / 150 = 101.3333..., entry costs 15.20
    # REDUCE 60 at s3 open 110: 6,600 - 6.60; cash 91,378.20
    #   gross 60 x (110 - 101.3333) = 520.00; pro-rata entry costs 15.20 x 60/150 = 6.08
    #   net 520 - 6.08 - 6.60 = 507.32 on a basis of 6,080 + 6.08 = 6,086.08
    # SELL 90 at s4 open 105: 9,450 - 9.45; cash 100,818.75
    #   gross 90 x (105 - 101.3333) = 330.00; entry costs left 9.12; net 330 - 9.12 - 9.45 = 311.43
    assert len(res["trades"]) == 2
    red, sell = res["trades"]
    assert red == {"symbol": "PAR", "entry_date": d[1], "entry_price": 101.3333, "entry_ref_price": 101.3333,
                   "qty": 60, "exit_date": d[3], "exit_price": 110.0, "exit_ref_price": 110.0,
                   "exit_reason": "REDUCE", "gross_pnl": 520.0, "costs": 12.68, "net_pnl": 507.32,
                   "return_pct": round(507.32 / 6086.08 * 100, 4), "holding_sessions": 2, "entry_reason": "in",
                   "adds": 1, "partial": True}
    assert sell == {"symbol": "PAR", "entry_date": d[1], "entry_price": 101.3333, "entry_ref_price": 101.3333,
                    "qty": 90, "exit_date": d[4], "exit_price": 105.0, "exit_ref_price": 105.0,
                    "exit_reason": "SELL_SIGNAL", "gross_pnl": 330.0, "costs": 18.57, "net_pnl": 311.43,
                    "return_pct": round(311.43 / 9129.12 * 100, 4), "holding_sessions": 3, "entry_reason": "in",
                    "adds": 1}
    eq = res["equity"]
    assert [(p["cash"], p["positions_value"], p["n_positions"]) for p in eq[:5]] == [
        (100_000.0, 0.0, 0), (89_990.0, 10_200.0, 1), (84_784.8, 15_900.0, 1), (91_378.2, 9_720.0, 1),
        (100_818.75, 0.0, 0)]
    assert eq[3]["realized_cum"] == 507.32
    assert eq[3]["unrealized"] == pytest.approx(90 * (108 - 15_200 / 150), abs=0.01)
    # the rows account for every rupee: their net P&L is the run's P&L
    assert sum(t["net_pnl"] for t in res["trades"]) == pytest.approx(818.75, abs=1e-9)
    assert eq[-1]["equity"] - 100_000 == pytest.approx(818.75, abs=1e-9)
    m = res["metrics"]
    assert m["costs_paid"] == 31.25 and m["slippage_paid"] == 0.0
    assert m["turnover"] == 10_000 + 5_200 + 6_600 + 9_450
    # trade statistics per round trip: one position, one (winning) trade
    assert (m["trades"], m["wins"], m["win_rate"]) == (1, 1, 1.0)
    assert m["expectancy"] == pytest.approx(818.75)
    assert m["expectancy_return"] == pytest.approx(round(818.75 / 15_215.2 * 100, 4))
    assert (m["trade_rows"], m["partial_exits"], m["adds"]) == (2, 1, 1)
    assert res["events"] == []


def test_partial_fills_are_priced_like_buys_and_sells_of_that_size(db, monkeypatch):
    from backtest.costs import cost_model
    from backtest.slippage import SlippageModel
    _seed([(100, 100, 100, 100), (100, 102, 100, 102), (104, 106, 104, 106), (110, 110, 108, 108)]
          + _flat(105))
    _script(monkeypatch, {0: [_sig("BUY", stop_price=95.0)], 1: [_sig("ADD", quantity=50)],
                          2: [_sig("REDUCE", quantity=60)], 3: [_sig("SELL")]})
    res = _run(cost_model="nse_delivery", cost_overrides={}, slippage={"kind": "pct", "value": 5.0})
    slip, cm = SlippageModel("pct", 5.0), cost_model("nse_delivery")
    pb, pa, pr, ps = (slip.fill_price("BUY", 100), slip.fill_price("BUY", 104),
                      slip.fill_price("SELL", 110), slip.fill_price("SELL", 105))
    cb, ca, cr, cs = cm.total("BUY", 100 * pb), cm.total("BUY", 50 * pa), cm.total("SELL", 60 * pr), cm.total("SELL", 90 * ps)
    avg = (100 * pb + 50 * pa) / 150
    net_r = 60 * (pr - avg) - (cb + ca) * 60 / 150 - cr
    net_s = 90 * (ps - avg) - (cb + ca) * 90 / 150 - cs
    red, sell = res["trades"]
    assert (red["qty"], red["exit_price"], red["partial"]) == (60, pr, True)
    assert red["entry_price"] == round(avg, 4) and red["entry_ref_price"] == round((100 * 100 + 50 * 104) / 150, 4)
    assert red["costs"] == round((cb + ca) * 60 / 150 + cr, 2) and red["net_pnl"] == round(net_r, 2)
    assert (sell["qty"], sell["exit_price"]) == (90, ps) and sell["net_pnl"] == round(net_s, 2)
    m = res["metrics"]
    assert m["costs_paid"] == round(cb + ca + cr + cs, 2)
    assert m["slippage_paid"] == round(100 * (pb - 100) + 50 * (pa - 104) + 60 * (110 - pr) + 90 * (105 - ps), 2)
    assert _eq(res, -1) - 100_000 == pytest.approx(net_r + net_s, abs=0.01)
    assert sum(t["net_pnl"] for t in res["trades"]) == pytest.approx(_eq(res, -1) - 100_000, abs=0.01)


@pytest.mark.parametrize("sig", [{"quantity": 500}, {"fraction": 1.0}], ids=["quantity", "fraction"])
def test_reduce_at_or_above_the_holding_closes_the_position(db, monkeypatch, sig):
    _seed(_flat(100, 100) + [(103, 103, 103, 103)])
    _script(monkeypatch, {0: [_sig("BUY", stop_price=95.0)], 1: [_sig("REDUCE", **sig)]})
    res = _run()
    d = _days()
    # 100 bought at 100 (10.00 costs), all 100 sold at 103 (10.30): net 300 - 20.30 = 279.70
    [row] = res["trades"]
    assert list(row) == ROW_KEYS                      # an ordinary closing row: not partial, no extra keys
    assert (row["qty"], row["exit_date"], row["exit_reason"], row["net_pnl"]) == (100, d[2], "REDUCE", 279.7)
    assert res["equity"][2]["n_positions"] == 0 and _eq(res, -1) == 100_279.7
    assert _events(res) == ["reduce 500 >= 100 held: position closed" if "quantity" in sig else
                            "reduce 100 >= 100 held: position closed"]
    assert res["metrics"]["trades"] == 1 and "partial_exits" not in res["metrics"]


@pytest.mark.parametrize("sig,sold", [({}, 50), ({"fraction": 0.3}, 30), ({"fraction": 0.29}, 29),
                                      ({"quantity": 1}, 1)], ids=["half", "fraction", "fraction_floor", "one"])
def test_reduce_size(db, monkeypatch, sig, sold):
    _seed(_flat(100, 100, 102))
    _script(monkeypatch, {0: [_sig("BUY", stop_price=95.0)], 1: [_sig("REDUCE", **sig)]})
    res = _run()
    part, rest = res["trades"]
    assert (part["qty"], part.get("partial"), rest["qty"], rest["exit_reason"]) == (sold, True, 100 - sold,
                                                                                   "END_OF_WINDOW")
    # 2 x sold over the entry at 100, less 0.1% of 100 x sold entry (pro rata) and of 102 x sold exit
    assert part["net_pnl"] == round(sold * 2 - sold * 0.1 - sold * 0.102, 2)
    assert res["equity"][2]["n_positions"] == 1


def test_reduce_without_a_bar_waits_like_a_sell(db, monkeypatch):
    _seed(_flat(100, 100) + [None] + _flat(104))
    _script(monkeypatch, {0: [_sig("BUY", stop_price=95.0)], 1: [_sig("REDUCE", quantity=40)]})
    res = _run()
    d = _days()
    assert res["events"][0] == {"date": d[2], "symbol": "PAR", "event": "reduce deferred: no bar"}
    part = res["trades"][0]
    assert (part["exit_date"], part["qty"], part["exit_price"], part["partial"]) == (d[3], 40, 104.0, True)


def test_stops_keep_working_on_what_is_left(db, monkeypatch):
    _seed(_flat(100, 100, 101) + [(99, 99, 94, 96)] + _flat(96))
    _script(monkeypatch, {0: [_sig("BUY", stop_price=95.0)], 1: [_sig("REDUCE", quantity=40)]})
    res = _run()
    d = _days()
    part, stop = res["trades"]
    # 40 sold at 101: 40 - 4.00 (pro-rata entry) - 4.04 = 31.96; 60 stopped at 95: -300 - 6.00 - 5.70 = -311.70
    assert (part["qty"], part["exit_date"], part["costs"], part["net_pnl"]) == (40, d[2], 8.04, 31.96)
    assert (stop["qty"], stop["exit_date"], stop["exit_reason"], stop["exit_price"]) == (60, d[3], "STOP", 95.0)
    assert (stop["costs"], stop["net_pnl"]) == (11.7, -311.7) and "partial" not in stop
    assert _eq(res, -1) - 100_000 == pytest.approx(31.96 - 311.7, abs=1e-9)
    m = res["metrics"]
    assert (m["trades"], m["wins"], m["losses"], m["partial_exits"], m["adds"]) == (1, 0, 1, 1, 0)
    assert m["expectancy"] == pytest.approx(-279.74)


def test_max_hold_counts_from_the_first_entry_and_closes_the_added_position(db, monkeypatch):
    _seed(_flat(100, 100) + [(102, 102, 102, 102)] + _flat(102))
    _script(monkeypatch, {0: [_sig("BUY", stop_price=95.0, max_hold_sessions=3)], 1: [_sig("ADD", quantity=50)]})
    res = _run()
    d = _days()
    [row] = res["trades"]                    # filled s1; held 3 sessions at s4 -> sold at s5's open
    assert (row["qty"], row["entry_date"], row["exit_date"], row["exit_reason"]) == (150, d[1], d[5], "MAX_HOLD")
    assert row["holding_sessions"] == 4 and row["adds"] == 1 and "partial" not in row
    assert row["entry_price"] == round((100 * 100 + 50 * 102) / 150, 4)
    # 150 x (102 - 100.6667) = 200.00, less costs 10.00 + 5.10 + 15.30 = 30.40
    assert row["net_pnl"] == 169.6 and _eq(res, -1) == 100_169.6
    assert res["metrics"]["adds"] == 1 and res["metrics"]["partial_exits"] == 0


# ── ADD sizing, caps, cash ─────────────────────────────────────────────────

def test_add_by_value_and_by_default_sizing(db, monkeypatch):
    from orders.risk import size_position
    _seed(_flat(100, 102, 102, 102, 102))
    _script(monkeypatch, {0: [_sig("BUY", stop_price=95.0)], 1: [_sig("ADD", value=2550)],
                          2: [_sig("ADD", stop_price=97.0)]})
    res = _run(sizing={**SIZING, "max_position_pct": 60})
    eq2 = _eq(res, 2)
    # s1: 2,550 // 102 = 25 shares, filled at s2's open; s2: sized like a BUY on s2 equity
    want = size_position(102, 97.0, capital=eq2, risk_per_trade_pct=0.5, max_position_pct=60)["quantity"]
    room = int((eq2 * 0.6 - 125 * 102) // 102)
    [row] = res["trades"]
    assert row["qty"] == 100 + 25 + min(want, room) and row["adds"] == 2
    assert res["metrics"]["adds"] == 2


def test_add_is_capped_at_max_position_pct(db, monkeypatch):
    _seed(_flat(100, 100, 100))
    _script(monkeypatch, {0: [_sig("BUY", stop_price=95.0)], 1: [_sig("ADD", quantity=500)]})
    res = _run()
    d = _days()
    # s1 equity 99,990: 20% is 19,998, 100 x 100 held -> room 99 shares
    assert res["events"] == [{"date": d[1], "symbol": "PAR",
                              "event": "add capped to 99 of 500 shares: max_position_pct (20% of equity)"}]
    assert res["trades"][0]["qty"] == 199


def test_add_at_the_cap_is_skipped(db, monkeypatch):
    _seed(_flat(100, 100, 100))
    _script(monkeypatch, {0: [_sig("BUY", stop_price=95.0)], 1: [_sig("ADD", quantity=5)]})
    res = _run(sizing={**SIZING, "max_position_pct": 10})
    # the BUY took the whole 10% (100 shares); s1 equity 99,990 leaves no room for another share
    assert _events(res) == ["add skipped: position at max_position_pct (10% of equity)"]
    assert res["trades"][0]["qty"] == 100 and "adds" not in res["trades"][0]


def test_unaffordable_add_is_cut_to_cash_with_an_event(db, monkeypatch):
    _seed(_flat(100, 100) + [(101, 101, 101, 101)] + _flat(101))
    _script(monkeypatch, {0: [_sig("BUY", stop_price=95.0)], 1: [_sig("ADD", quantity=2000)]})
    res = _run(sizing={**SIZING, "max_position_pct": 100})
    d = _days()
    # s1: room (99,990 - 10,000) // 100 = 899; at s2's open 101, 899 shares cost 90,799 + 90.80 > 89,990 cash:
    # cut like a BUY -> int(89,990 // (101 x 1.01)) = 882 shares, 89,082 + 89.08 costs
    assert _events(res) == ["add capped to 899 of 2000 shares: max_position_pct (100% of equity)",
                            "add cut to 882 of 899 shares: cash"]
    assert res["events"][1]["date"] == d[2]
    assert res["equity"][2]["cash"] == round(89_990 - 89_082 - 89.082, 2)
    [row] = res["trades"]
    assert row["qty"] == 982 and row["entry_price"] == round((10_000 + 882 * 101) / 982, 4)


def test_add_with_no_cash_is_rejected_with_an_event(db, monkeypatch):
    _seed(_flat(100, 100) + [(104, 104, 104, 104)] + _flat(104))
    _script(monkeypatch, {0: [_sig("BUY", stop_price=95.0)], 1: [_sig("ADD", quantity=10)]})
    res = _run(initial_capital=10_000, sizing={**SIZING, "risk_per_trade_pct": 50, "max_position_pct": 200})
    # the BUY wants 200 shares and is cut to 99 (9,900 + 9.90); 90.10 cash cannot buy one more at 104
    assert _events(res) == ["add rejected: no cash / liquidity"]
    [row] = res["trades"]
    assert row["qty"] == 99 and "adds" not in row and "adds" not in res["metrics"]


def test_add_or_reduce_of_a_symbol_not_held_is_ignored(db, monkeypatch):
    _seed(_flat(100, 100, 100))
    _script(monkeypatch, {0: [_sig("ADD", "QQQ", quantity=10), _sig("REDUCE", "QQQ", quantity=5)]})
    res = _run()
    assert _events(res) == ["add ignored: QQQ is not held", "reduce ignored: QQQ is not held"]
    assert res["trades"] == [] and _eq(res, -1) == 100_000


def test_an_exit_wins_over_a_change_queued_for_the_same_open(db, monkeypatch):
    _seed(_flat(100, 100, 103))
    _script(monkeypatch, {0: [_sig("BUY", stop_price=95.0)],
                          1: [_sig("REDUCE", quantity=30), _sig("ADD", quantity=10), _sig("SELL")]})
    res = _run()
    assert [t["qty"] for t in res["trades"]] == [100]
    assert res["trades"][0]["exit_reason"] == "SELL_SIGNAL"
    assert _events(res) == ["reduce dropped: position no longer held", "add dropped: position no longer held"]


def test_signal_validation():
    from backtest.strategy import Signal
    for kw in ({"side": "BUY", "quantity": 5}, {"side": "SELL", "fraction": 0.5}, {"side": "ADD", "fraction": 0.5},
               {"side": "REDUCE", "value": 100.0}, {"side": "ADD", "quantity": 0}, {"side": "ADD", "quantity": 2.5},
               {"side": "REDUCE", "fraction": 0}, {"side": "REDUCE", "fraction": 1.5},
               {"side": "ADD", "quantity": 5, "value": 100.0}, {"side": "HOLD"}):
        with pytest.raises(ValueError):
            Signal("X", **kw)
    assert Signal("X", "REDUCE").quantity is None and Signal("X", "ADD", value=1e4).value == 1e4
    assert Signal("X", "BUY", 95.0, 110.0, 5, "r") == Signal("X", "BUY", stop_price=95.0, target_price=110.0,
                                                           max_hold_sessions=5, reason="r")


# ── existing BUY / SELL runs are unchanged ─────────────────────────────────

def test_buy_and_sell_rows_are_unchanged(db, monkeypatch):
    _seed(_flat(100, 100, 100, 105))
    _script(monkeypatch, {0: [_sig("BUY", stop_price=95.0, reason="in")], 2: [_sig("SELL")]})
    res = _run()
    d = _days()
    # 100 at 100 (10.00 costs), sold at 105 (10.50): gross 500.00, net 479.50 on a 10,010 basis
    assert res["trades"] == [{"symbol": "PAR", "entry_date": d[1], "entry_price": 100.0, "entry_ref_price": 100.0,
                              "qty": 100, "exit_date": d[3], "exit_price": 105.0, "exit_ref_price": 105.0,
                              "exit_reason": "SELL_SIGNAL", "gross_pnl": 500.0, "costs": 20.5, "net_pnl": 479.5,
                              "return_pct": round(479.5 / 10_010 * 100, 4), "holding_sessions": 2,
                              "entry_reason": "in"}]
    assert {"trade_rows", "partial_exits", "adds"}.isdisjoint(res["metrics"])


WAVES = ("AAA", "BBB", "CCC")
LEGACY = ("2025-01-01", "2025-09-30")


def _seed_waves():
    """test_w39_backtest's synthetic market: three sine waves the dip strategy trades
    often and three straight lines."""
    days = _days(*LEGACY)
    rows = []
    for k, sym in enumerate(WAVES):
        prev = None
        for t, d in enumerate(days):
            c = round(100 * (1 + 0.0008 * t) * (1 + 0.07 * math.sin(2 * math.pi * (t + 5 * k) / 17)), 2)
            o = prev or c
            rows.append((sym, str(d), o, round(max(o, c) * 1.004, 2), round(min(o, c) * 0.996, 2), c, 2_000_000))
            prev = c
    for sym, a, b in (("UPP", 100, 101), ("DWN", 100, 98), ("RUN", 100, 120)):
        for t, d in enumerate(days):
            c = round(a + (b - a) * t / (len(days) - 1), 4)
            rows.append((sym, str(d), c, c, c, c, 2_000_000))
    _insert(rows)


def _digest(res) -> str:
    from backtest.metrics import jsonable
    return hashlib.sha256(json.dumps(jsonable(res), sort_keys=True).encode()).hexdigest()


# Full-result digests from the engine BEFORE PF-06 (same data, same requests).
LEGACY_RUNS = {
    "dip": ({"strategy_id": "dip", "universe": list(WAVES)},
            30, "b8232ed79bf5e308437a1f19d7793115803876f952eb1fd7b2910f22dc3588c8"),
    "dip_flat_liq": ({"strategy_id": "dip", "universe": list(WAVES), "cost_model": "flat",
                      "liquidity": {"max_participation_pct": 0.2}, "close_out_at_end": False},
                     29, "afa78a9843d9986349fb10bafc1f4518f9ba16774ac2891d198c4488782d6bb7"),
    "buy_and_hold": ({"strategy_id": "buy_and_hold", "universe": ["UPP", "DWN", "RUN"], "cost_model": "flat",
                      "slippage": {"kind": "none", "value": 0}},
                     3, "2ebeaa0cd6166f2a887938505b5e5907664ffcade2cce1fa165514e7c0a40735"),
}


@pytest.mark.parametrize("name", sorted(LEGACY_RUNS))
def test_buy_sell_only_runs_are_unchanged(db, name):
    from backtest import engine, service
    from db.schema import get_connection
    _seed_waves()
    req, trades, digest = LEGACY_RUNS[name]
    snap = service.resolve_config({**req, "start": LEGACY[0], "end": LEGACY[1]})
    conn = get_connection()
    try:
        res = engine.run(snap, conn)
    finally:
        conn.close()
    assert all(list(t) == ROW_KEYS for t in res["trades"])
    assert {"trade_rows", "partial_exits", "adds"}.isdisjoint(res["metrics"])
    assert (res["metrics"]["trades"], _digest(res)) == (trades, digest)


# ── strategy versions: ADD / REDUCE decisions reach the engine ─────────────

def _mf(sid, **kw):
    return {"strategy_id": sid, "name": sid, "version": "1.0.0", "kind": "multi_factor",
            "universe": {"type": "symbols", "symbols": list(WAVES)},
            "factors": [{"feature": "rsi_14", "weight": 1, "min": 0, "max": 100, "threshold": 50}],
            "entry_threshold": 60, "exit_threshold": 30,
            "position": {"target_position_pct": 10, "max_positions": 3, "stop_pct": 10}, **kw}


def _run_version(sid):
    """A stored strategy version over the wave market, on the configured defaults (its own
    position rules as sizing, 1,000,000 capital, nse_delivery costs, 5 bps slippage)."""
    from backtest import engine, service
    from db.schema import get_connection
    snap = service.resolve_config({"strategy_id": sid, "universe": list(WAVES), "start": LEGACY[0], "end": LEGACY[1]})
    conn = get_connection()
    try:
        return engine.run(snap, conn)
    finally:
        conn.close()


def _store(*defs):
    from db.schema import get_connection
    from strategy_engine import registry
    conn = get_connection()
    try:
        for d in defs:
            registry.create_strategy(conn, d)
    finally:
        conn.close()


def test_a_strategy_version_that_reduces_now_changes_position_size(db):
    _seed_waves()
    _store(_mf("mf_plain"), _mf("mf_reduce", reduce_threshold=45))
    plain, red = _run_version("mf_plain"), _run_version("mf_reduce")
    for r in (plain, red):
        assert not any("not simulated" in w for w in r["bias_report"]["warnings"])
    assert not any(t.get("partial") for t in plain["trades"]) and "partial_exits" not in plain["metrics"]
    parts = [t for t in red["trades"] if t.get("partial")]
    assert parts and all(t["exit_reason"] == "REDUCE" for t in parts)
    # each REDUCE decision (no quantity on it) sells half of what is held, as the W4 risk engine would
    by_pos = {}
    for t in red["trades"]:
        by_pos.setdefault((t["symbol"], t["entry_date"]), []).append(t)
    reduced = [rows for rows in by_pos.values() if rows[0].get("partial")]
    assert reduced
    for rows in reduced:
        held = sum(t["qty"] for t in rows)
        for t in rows:
            if t.get("partial"):
                assert t["qty"] == held // 2
            held -= t["qty"]
        assert held == 0 and not rows[-1].get("partial")
    m = red["metrics"]
    assert m["partial_exits"] == len(parts) and m["trade_rows"] == len(red["trades"])
    assert m["trades"] == len(by_pos) < len(red["trades"])
    initial = 1_000_000.0
    assert sum(t["net_pnl"] for t in red["trades"]) == pytest.approx(_eq(red, -1) - initial,
                                                                     abs=0.01 * len(red["trades"]))
    assert red["equity"][-1]["realized_cum"] == pytest.approx(_eq(red, -1) - initial, abs=0.011)


def test_a_strategy_version_that_adds_is_capped_at_its_position_size(db):
    from db.schema import get_connection
    _seed_waves()
    # a 20% stop sizes each BUY at 5% of equity (1% risk); ADDs may take it to target_position_pct (10%)
    _store(_mf("mf_add", add_threshold=70, position={"target_position_pct": 10, "max_positions": 3, "stop_pct": 20}))
    res = _run_version("mf_add")
    [added] = [t for t in res["trades"] if t.get("adds")]
    assert added["adds"] == res["metrics"]["adds"] == 1
    # the ADD (sized like a BUY: another 5%) was capped to what keeps the position within 10% of
    # equity at the decision close, and later ADD decisions found no room
    [cap] = [e for e in res["events"] if e["event"].startswith("add capped")]
    assert cap["symbol"] == added["symbol"] and cap["event"].endswith("max_position_pct (10% of equity)")
    room = int(cap["event"].split()[3])
    conn = get_connection()
    try:
        close = conn.execute("SELECT close FROM prices_daily WHERE symbol=? AND date=?",
                             (cap["symbol"], str(cap["date"]))).fetchone()[0]
    finally:
        conn.close()
    equity = next(p["equity"] for p in res["equity"] if p["date"] == cap["date"])
    assert room > 0 and added["qty"] * close <= 0.1 * equity < (added["qty"] + 1) * close
    assert any(e["event"].startswith("add skipped: position at max_position_pct") for e in res["events"]
               if e["symbol"] == cap["symbol"] and e["date"] > cap["date"])
    assert not any("not simulated" in w for w in res["bias_report"]["warnings"])


def test_adapter_maps_add_and_reduce_and_counts_what_it_cannot_trade(db):
    from datetime import date

    from backtest.engine import _not_simulated_warning
    from backtest.strategy import PositionView, Signal
    from strategy_engine.adapter import DefinitionStrategy
    from strategy_engine.decisions import StrategyDecision
    as_of = date(2025, 3, 3)

    def dec(sym, action, **kw):
        return StrategyDecision("mf", "1.0.0", sym, as_of, action, None, ["why"], **kw)

    decisions = [dec("AAA", "ADD", stop_price=95.0, features={"close": 100.0, "rebalance_qty": 7}),
                 dec("BBB", "REDUCE", features={"close": 50.0}),
                 dec("CCC", "REDUCE", features={"rebalance_qty": 3}),
                 dec("DDD", "REDUCE"), dec("DDD", "SHORT"), dec("EEE", "BUY", stop_price=9.0),
                 dec("AAA", "HOLD")]
    s = DefinitionStrategy(_mf("mf"), {})
    s._ev = SimpleNamespace(decide=lambda env, as_of, held, idx: decisions)
    held = {sym: PositionView(sym, 10, 100.0, as_of, 3) for sym in ("AAA", "BBB", "CCC")}
    ctx = SimpleNamespace(data=SimpleNamespace(_h=None), universe=("AAA", "BBB", "CCC", "DDD", "EEE"), scores=None,
                          positions=held, as_of=as_of)
    assert s.on_bar(ctx) == [Signal("AAA", "ADD", stop_price=95.0, quantity=7, reason="why"),
                             Signal("BBB", "REDUCE", reason="why"),
                             Signal("CCC", "REDUCE", quantity=3, reason="why"),
                             Signal("EEE", "BUY", stop_price=9.0, reason="why")]
    assert s.not_simulated == {"REDUCE (not held)": 1, "SHORT": 1}
    [w] = _not_simulated_warning(s)
    assert "REDUCE (not held) x1, SHORT x1" in w and "REDUCE / ADD" not in w
