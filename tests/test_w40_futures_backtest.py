"""
W40 (QR-05 / QR-06): futures short legs in the W2 backtest, and auto-roll in the paper futures book.

  backtest/futures.py      SHORT / COVER signals are simulated as near-month stock futures:
                           whole lots (rounded down), margin blocked from cash, daily variation
                           margin through cash, the F&O cost segment (backtest/costs.py
                           FuturesCostModel), roll N sessions before expiry into the next month
                           (or settle at expiry), missing futures history -> the leg is refused
  backtest/engine.py       hooks the book in only when a SHORT arrives; BUY / SELL-only runs are
                           byte-identical (LEGACY_W40 below + LEGACY_RUNS / ED_LEGACY_RUNS of
                           tests/test_w39_backtest_partial.py)
  execution/futures_paper  futures.auto_roll (off by default): settle_expired rolls instead of
                           letting a short expire

Market: LNG (the cash long leg) and SHT (the futures short leg) on hand-written prices. SHT's
near-month future (lot 500) expires E1 = session 7 (2025-01-10); the next month E2 = session 21
(2025-01-30) is listed in fo_contract_daily on sessions 5-7, and is the near month from session 8.
The cash leg pays the flat 0.1% model; slippage is off unless a test says otherwise; futures pay
the nse_futures preset, whose charges are worked out by hand in test_fo_cost_segment_hand_computed.
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

# SHT futures closes: the near (E1) contract on sessions 0-7, the next (E2) from session 5 on
E1_CLOSE = [100.0, 100.0, 98.0, 101.0, 99.0, 97.0, 96.0, 96.5]
E2_CLOSE = {5: 98.0, 6: 97.0, 7: 99.0, 8: 95.0, 9: 94.0}
LNG = [200.0, 200.0, 202.0, 199.0, 201.0, 200.0, 198.0, 199.0, 197.0, 196.0]


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


def _exec(sql, rows):
    from db.schema import get_connection
    conn = get_connection()
    conn.executemany(sql, rows)
    conn.commit()
    conn.close()


def _prices(path_by_symbol, n=40):
    rows = []
    for sym, path in path_by_symbol.items():
        for i, d in enumerate(_days()[:n]):
            p = path[min(i, len(path) - 1)]
            rows.append((sym, str(d), p, p, p, p, 10_000_000))
    _exec("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)", rows)


def _seed(contracts=True, summary_days=None, lot=500, sym="SHT"):
    """LNG and SHT spot bars; SHT's futures as described in the module docstring. contracts=False
    stores no fo_contract_daily rows (no next-month price before E1 expires)."""
    d = _days()
    e1, e2 = str(d[7]), str(d[21])
    _prices({"LNG": LNG, sym: [99.0]})
    summ = []
    for i in range(len(d[:40])):
        if summary_days is not None and i not in summary_days:
            continue
        if i <= 7:
            summ.append((str(d[i]), sym, "STOCK", 99.0, E1_CLOSE[i], e1, lot))
        else:
            summ.append((str(d[i]), sym, "STOCK", 99.0, E2_CLOSE.get(i, 94.0), e2, lot))
    _exec("INSERT INTO fo_underlying_daily (date,symbol,kind,underlying_price,fut_close,near_expiry,lot_size) "
          "VALUES (?,?,?,?,?,?,?)", summ)
    if contracts:
        rows = []
        for i in (5, 6, 7):
            rows += [(str(d[i]), sym, "STF", e1, 0.0, "XX", E1_CLOSE[i], lot),
                     (str(d[i]), sym, "STF", e2, 0.0, "XX", E2_CLOSE[i], lot)]
        _exec("INSERT INTO fo_contract_daily (date,symbol,instrument,expiry,strike,option_type,close,lot_size) "
              "VALUES (?,?,?,?,?,?,?,?)", rows)


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


def _run(last=15, universe=("LNG", "SHT"), capital=1_000_000, **kw):
    from backtest import engine, service
    from db.schema import get_connection
    req = {"strategy_id": "scripted", "universe": list(universe), "start": str(_days()[0]),
           "end": str(_days()[last]), "initial_capital": capital, "sizing": dict(SIZING), **FLAT_COSTS}
    req.update(kw)
    snap = service.resolve_config(req)
    conn = get_connection()
    try:
        return engine.run(snap, conn)
    finally:
        conn.close()


def _sig(sym, side, **kw):
    from backtest.strategy import Signal
    return Signal(sym, side, **kw)


def _events(res):
    return [e["event"] for e in res["events"]]


def _fo_charges(side, value, lots=2):
    from backtest.costs import futures_cost_model
    return futures_cost_model().total(side, value, lots)


# ── the F&O cost segment ───────────────────────────────────────────────────

def test_fo_cost_segment_hand_computed():
    from backtest.costs import FuturesCostModel, futures_cost_model
    m = futures_cost_model()
    # SELL 2 lots, notional 100,000: brokerage min(0.03% = 30, 20) = 20; STT 0.02% = 20;
    # exchange 0.00173% = 1.73; SEBI 0.0001% = 0.10; GST 18% x 21.83 = 3.9294; total 45.7594
    assert m.charges("SELL", 100_000, 2) == {"brokerage": 20.0, "stt": 20.0, "exchange": 1.73, "sebi": 0.1,
                                             "stamp": 0.0, "gst": 3.9294, "total": 45.7594}
    # BUY: no STT, stamp 0.002% = 2.00 -> 27.7594
    assert m.charges("BUY", 100_000, 2)["total"] == 27.7594
    # 10 lakh notional: STT 200, exchange 17.30, SEBI 1.00, GST 18% x 38.30 = 6.894
    assert m.total("SELL", 1_000_000) == 245.194 and m.total("BUY", 1_000_000) == 65.194
    # a small order pays 0.03% (below the Rs 20 cap); per-lot brokerage (the paper book's model) on top
    assert m.charges("BUY", 10_000)["brokerage"] == 3.0
    per_lot = futures_cost_model("nse_futures", {"brokerage_pct": 0, "brokerage_max": None,
                                                 "brokerage_per_lot": 20})
    assert per_lot.charges("SELL", 100_000, 3)["brokerage"] == 60.0
    assert futures_cost_model("zero").total("SELL", 1e6, 5) == 0.0
    with pytest.raises(ValueError):
        futures_cost_model("nse_delivery")
    with pytest.raises(ValueError):
        futures_cost_model("nse_futures", {"dp_charge_per_sell": 1})
    assert isinstance(m, FuturesCostModel)


# ── a pair trade, hand-computed: long cash leg + short futures leg, MTM, margin, a roll ──

def test_pair_trade_with_a_roll_hand_computed(db, monkeypatch):
    _seed()
    _script(monkeypatch, {0: [_sig("SHT", "SHORT", value=120_000.0, reason="pair short"),
                              _sig("LNG", "BUY", reason="pair long")],
                          8: [_sig("SHT", "COVER"), _sig("LNG", "SELL")]})
    res = _run()
    d = _days()
    # SHORT: 120,000 at the decision close 100 x lot 500 (50,000 a lot) -> 2 lots (2.4 rounded DOWN),
    #   1,000 shares. Fills at s1's futures close 100: margin 20% x 100,000 = 20,000 blocked,
    #   charges 45.7594 (SELL 100,000). BUY LNG: 0.5% risk / 5% stop -> 500 shares at s1's open 200,
    #   100,000 + 100.00 (flat 0.1%).
    # variation (previous mark - close) x 1,000: s2 98 +2,000; s3 101 -3,000; s4 99 +2,000
    # ROLL at s5 (2 sessions left to E1 = s7): buy back E1 at 97 -- variation +2,000, margin 20,000
    #   released, charges BUY 97,000 = 27.6346; sell E2 at 98 -- margin 19,600, charges SELL 98,000 =
    #   45.3162. Calendar spread 98 - 97 = +1.00 / share; roll cost 27.6346 + 45.3162 = 72.9508.
    #   Leg 1: gross 1,000 x (100 - 97) = 3,000; net 3,000 - 45.7594 - 27.6346 = 2,926.606 on
    #   100,000 + 45.7594
    # s6 97 +1,000; s7 99 -2,000; s8 95 +4,000 (E2 is now the near month in fo_underlying_daily)
    # s9: LNG sold at the open 196: 98,000 - 98.00; COVER at the futures close 94: +1,000 variation,
    #   19,600 released, charges BUY 94,000 = 27.5098. Leg 2: gross 1,000 x (98 - 94) = 4,000;
    #   net 4,000 - 45.3162 - 27.5098 = 3,927.174 on 98,000 + 45.3162
    sht1, lng, sht2 = res["trades"]
    assert sht1 == {"symbol": "SHT", "entry_date": d[1], "entry_price": 100.0, "entry_ref_price": 100.0,
                    "qty": 1000, "exit_date": d[5], "exit_price": 97.0, "exit_ref_price": 97.0, "exit_reason": "ROLL",
                    "gross_pnl": 3000.0, "costs": 73.39, "net_pnl": 2926.61,
                    "return_pct": round(2926.606 / 100_045.7594 * 100, 4), "holding_sessions": 4,
                    "entry_reason": "pair short", "instrument": "FUT", "direction": "SHORT", "expiry": d[7],
                    "lots": 2, "lot_size": 500, "leg": 1, "rolled": True, "calendar_spread": 1.0,
                    "roll_cost": 72.95}
    assert lng == {"symbol": "LNG", "entry_date": d[1], "entry_price": 200.0, "entry_ref_price": 200.0, "qty": 500,
                   "exit_date": d[9], "exit_price": 196.0, "exit_ref_price": 196.0, "exit_reason": "SELL_SIGNAL",
                   "gross_pnl": -2000.0, "costs": 198.0, "net_pnl": -2198.0,
                   "return_pct": round(-2198 / 100_100 * 100, 4), "holding_sessions": 8, "entry_reason": "pair long"}
    assert sht2 == {"symbol": "SHT", "entry_date": d[1], "entry_price": 98.0, "entry_ref_price": 98.0,
                    "qty": 1000, "exit_date": d[9], "exit_price": 94.0, "exit_ref_price": 94.0,
                    "exit_reason": "COVER_SIGNAL", "gross_pnl": 4000.0, "costs": 72.83, "net_pnl": 3927.17,
                    "return_pct": round(3927.174 / 98_045.3162 * 100, 4), "holding_sessions": 8,
                    "entry_reason": "pair short", "instrument": "FUT", "direction": "SHORT", "expiry": d[21],
                    "lots": 2, "lot_size": 500, "leg": 2}
    assert list(lng) == ROW_KEYS                                      # the cash row is an ordinary row
    # free cash, longs, margin and equity, session by session
    eq = res["equity"]
    got = [(p["cash"], p["positions_value"], p["futures_margin"], p["futures_notional"], p["equity"])
           for p in eq[:10]]
    assert got == [
        (1_000_000.0, 0.0, 0.0, 0.0, 1_000_000.0),
        (879_854.24, 100_000.0, 20_000.0, 100_000.0, 999_854.24),     # 1e6 - 100,100 - 20,000 - 45.7594
        (881_854.24, 101_000.0, 20_000.0, 98_000.0, 1_002_854.24),
        (878_854.24, 99_500.0, 20_000.0, 101_000.0, 998_354.24),
        (880_854.24, 100_500.0, 20_000.0, 99_000.0, 1_001_354.24),
        (883_181.29, 100_000.0, 19_600.0, 98_000.0, 1_002_781.29),    # + 21,972.3654 - 19,645.3162
        (884_181.29, 99_000.0, 19_600.0, 97_000.0, 1_002_781.29),
        (882_181.29, 99_500.0, 19_600.0, 99_000.0, 1_001_281.29),
        (886_181.29, 98_500.0, 19_600.0, 95_000.0, 1_004_281.29),
        (1_004_655.78, 0.0, 0.0, 0.0, 1_004_655.78)]                  # + 97,902 + 20,572.4902
    assert eq[1]["exposure_pct"] == round(200_000 / 999_854.2406 * 100, 4)       # gross: long + futures
    assert eq[2]["unrealized"] == 1_000.0 + 2_000.0 and eq[1]["n_positions"] == 2
    assert eq[5]["realized_cum"] == 2926.61
    # every rupee accounted for: rows' net P&L = the run's P&L = 6,853.78 (futures) - 2,198 (cash)
    assert sum(t["net_pnl"] for t in res["trades"]) == pytest.approx(4655.78, abs=1e-6)
    assert eq[-1]["equity"] == 1_004_655.78
    m = res["metrics"]
    # round trips: the rolled short is ONE trade (net 6,853.78 on its first leg's basis), the long another
    assert (m["trades"], m["wins"], m["losses"], m["win_rate"]) == (2, 1, 1, 0.5)
    assert m["expectancy"] == pytest.approx((6853.78 - 2198.0) / 2)
    assert m["costs_paid"] == round(198 + 45.7594 + 27.6346 + 45.3162 + 27.5098, 2) == 344.22
    assert m["turnover"] == 100_000 + 98_000 + 100_000 + 97_000 + 98_000 + 94_000
    assert (m["futures_trades"], m["futures_rolls"], m["roll_costs"], m["futures_costs"], m["trade_rows"],
            m["futures_refused"], m["open_futures_at_end"]) == (1, 1, 72.95, 146.22, 3, 0, 0)
    assert _events(res) == ["rolled 2 lot(s) 2025-01-10 -> 2025-01-30: spread +1.00/share, cost 72.95"]
    fb = res["bias_report"]["futures"]
    assert fb["rolls"] == 1 and fb["refused"] == {} and fb["rolls_sample"][0]["calendar_spread"] == 1.0
    assert not any("not simulated" in w for w in res["bias_report"]["warnings"])
    assert any("filled hours apart" in w for w in res["bias_report"]["warnings"])


def test_the_strategy_sees_its_short_as_a_futures_view(db, monkeypatch):
    from backtest import strategies
    _seed()
    _script(monkeypatch, {0: [_sig("SHT", "SHORT", value=120_000.0)]})
    made = []
    orig = strategies.REGISTRY["scripted"].on_bar

    def spy(self, ctx):
        made.append((ctx.as_of, dict(ctx.futures), dict(ctx.positions)))
        return orig(self, ctx)
    monkeypatch.setattr(strategies.REGISTRY["scripted"], "on_bar", spy)
    _run(last=6)
    d = _days()
    by = {a: (f, p) for a, f, p in made}
    assert by[d[0]] == ({}, {})
    f, p = by[d[2]]
    assert p == {} and set(f) == {"SHT"}
    v = f["SHT"]
    assert (v.qty, v.lots, v.lot_size, v.entry_price, v.held_sessions, v.expiry, v.short) == (
        -1000, 2, 500, 100.0, 1, d[7], True)
    assert by[d[5]][0]["SHT"].expiry == d[21]                        # rolled at s5's close


# ── lot rounding and sizing ───────────────────────────────────────────────

@pytest.mark.parametrize("kw, lots, event", [
    ({"value": 120_000.0}, 2, None),                                  # 2.4 lots -> 2
    ({"quantity": 1499}, 2, None),                                    # 1,499 shares -> 2 lots of 500
    ({}, 4, None),                                                    # neither: max_position_pct 20% = 200,000
    ({"value": 500_000.0}, 4, "short capped to 200,000 of 500,000: max_position_pct (20% of equity)"),
    ({"value": 40_000.0}, 0, "short refused: one lot (500 x 100.0 = 50,000) is more than the leg's size "
                             "(40,000); lots are never rounded up"),
])
def test_shorts_are_whole_lots_rounded_down(db, monkeypatch, kw, lots, event):
    _seed()
    _script(monkeypatch, {0: [_sig("SHT", "SHORT", **kw)]})
    res = _run(last=3)
    rows = [t for t in res["trades"] if t.get("instrument") == "FUT"]
    if lots:
        [t] = rows
        assert (t["lots"], t["qty"], t["exit_reason"]) == (lots, lots * 500, "END_OF_WINDOW")
    else:
        assert rows == [] and res["metrics"]["futures_refused"] == 1
    assert ([e for e in _events(res)] == [event]) if event else _events(res) == []


def test_margin_must_fit_free_cash_or_the_lots_are_cut(db, monkeypatch):
    _seed()
    _script(monkeypatch, {0: [_sig("SHT", "SHORT", value=5_000_000.0)]})
    # margin 100% of notional, max_position_pct 100: 1,000,000 -> 20 lots, but 20 lots need
    # 1,000,000 margin + charges > 1,000,000 cash -> 19 lots
    res = _run(last=3, sizing={**SIZING, "max_position_pct": 100}, futures={"margin_pct": 100})
    [t] = res["trades"]
    assert t["lots"] == 19
    assert "short cut to 19 of 20 lot(s): cash for margin" in _events(res)


def test_fill_slippage_shows_in_equity_the_same_day(db, monkeypatch):
    _seed()
    _script(monkeypatch, {0: [_sig("SHT", "SHORT", value=120_000.0)]})
    res = _run(last=3, slippage={"kind": "pct", "value": 10.0})        # 10 bps
    # SELL fill 100 x (1 - 0.001) = 99.90; marked at the 100 close that evening: -0.10 x 1,000
    eq1 = res["equity"][1]
    charges = _fo_charges("SELL", 99_900)
    assert eq1["equity"] == round(1_000_000 - 100 - charges, 2)
    assert eq1["futures_margin"] == round(99_900 * 0.2, 2)
    t = res["trades"][0]
    assert (t["entry_price"], t["entry_ref_price"]) == (99.9, 100.0)
    # covered at the end at the last mark 101 (s3) plus 10 bps: 101.101
    assert (t["exit_price"], t["exit_ref_price"]) == (101.101, 101.0)
    assert res["metrics"]["slippage_paid"] == pytest.approx(1000 * 0.1 + 1000 * 0.101)


# ── missing futures history: the leg is refused, never priced off spot ────

def test_no_futures_history_refuses_the_leg_and_says_so(db, monkeypatch):
    _seed()
    _prices({"NOF": [50.0]})
    _script(monkeypatch, {0: [_sig("NOF", "SHORT", value=100_000.0), _sig("LNG", "BUY")]})
    res = _run(last=5, universe=("LNG", "SHT", "NOF"))
    d = _days()
    assert _events(res) == [f"short refused: no futures history for NOF on {d[0]} (no near-month contract in "
                            f"fo_underlying_daily / fo_contract_daily) -- leg not simulated"]
    assert [t["symbol"] for t in res["trades"]] == ["LNG"]            # the long leg traded, unhedged
    assert res["metrics"]["futures_refused"] == 1 and res["bias_report"]["futures"]["refused"] == {"NOF": 1}
    [w] = [w for w in res["bias_report"]["warnings"] if w.startswith("futures legs refused")]
    assert "NOF x1" in w and "never priced off spot" in w and "unhedged" in w


def test_a_short_with_no_futures_close_at_the_fill_is_dropped(db, monkeypatch):
    _seed(summary_days={0} | set(range(2, 40)), contracts=False)       # nothing stored for session 1
    _script(monkeypatch, {0: [_sig("SHT", "SHORT", value=120_000.0)]})
    res = _run(last=4)
    assert _events(res) == [f"short dropped: no futures close for SHT on {_days()[1]} at the fill"]
    assert res["trades"] == [] and res["bias_report"]["futures"]["refused"] == {"SHT": 1}


def test_a_held_leg_with_a_missing_close_keeps_its_mark(db, monkeypatch):
    _seed(summary_days=set(range(40)) - {3}, contracts=False)
    _script(monkeypatch, {0: [_sig("SHT", "SHORT", value=120_000.0)]})
    res = _run(last=4)
    eq = res["equity"]
    assert eq[3]["futures_notional"] == 98_000.0                     # s2's mark carried
    assert eq[3]["cash"] == eq[2]["cash"]
    assert res["metrics"]["futures_stale_marks"] == 1
    assert any(w.startswith("futures stale marks: 1") for w in res["bias_report"]["warnings"])


# ── expiry with and without a roll ────────────────────────────────────────

def test_without_a_roll_the_leg_settles_at_expiry(db, monkeypatch):
    _seed()
    _script(monkeypatch, {0: [_sig("SHT", "SHORT", value=120_000.0)]})
    res = _run(last=12, futures={"roll_days_before_expiry": None})
    d = _days()
    [t] = res["trades"]
    # held to E1 (s7) and settled at its final close 96.50 -- no slippage, charged as a closing leg
    sell, buy = _fo_charges("SELL", 100_000), _fo_charges("BUY", 96_500)
    assert (t["exit_date"], t["exit_price"], t["exit_reason"], t["expiry"]) == (d[7], 96.5, "EXPIRY", d[7])
    assert t["net_pnl"] == round(3_500 - sell - buy, 2) and "rolled" not in t
    assert res["equity"][-1]["equity"] == round(1_000_000 + 3_500 - sell - buy, 2)
    assert res["equity"][8]["futures_margin"] == 0.0                  # nothing opened in the next month
    assert _events(res) == []
    assert any("settled at expiry in cash" in w and "physically settled" in w
               for w in res["bias_report"]["warnings"])
    assert res["bias_report"]["futures"]["expiry_settlements"] == 1


def test_a_roll_with_no_next_month_price_waits_then_settles_at_expiry(db, monkeypatch):
    _seed(contracts=False)                                            # E2 unknown until s8
    _script(monkeypatch, {0: [_sig("SHT", "SHORT", value=120_000.0)]})
    res = _run(last=10)
    d = _days()
    assert _events(res) == [f"roll deferred: no next-month close stored on {d[5]}",
                            f"roll deferred: no next-month close stored on {d[6]}",
                            f"roll deferred: no next-month close stored on {d[7]}",
                            "roll failed: no next-month close before the 2025-01-10 expiry; settled at expiry"]
    [t] = res["trades"]
    assert (t["exit_reason"], t["exit_date"], t["exit_price"]) == ("EXPIRY", d[7], 96.5)
    assert res["bias_report"]["futures"]["roll_waits"] == 3


def test_a_new_short_inside_the_roll_window_opens_the_next_month(db, monkeypatch):
    _seed()
    _script(monkeypatch, {5: [_sig("SHT", "SHORT", value=120_000.0)]})
    res = _run(last=8)
    [t] = res["trades"]
    d = _days()
    # decided at s5 (sized on the 97 close: 2 lots), filled at s6: E1 has 1 session left -> E2 at 97
    assert (t["expiry"], t["entry_date"], t["entry_price"], t["lots"], t["leg"]) == (d[21], d[6], 97.0, 2, 1)


def test_a_new_short_without_a_next_month_inside_the_window_is_refused(db, monkeypatch):
    _seed(contracts=False)
    _script(monkeypatch, {5: [_sig("SHT", "SHORT", value=120_000.0)]})
    res = _run(last=8)
    assert res["trades"] == []
    assert _events(res) == [f"short refused: the near-month future expires 2025-01-10 (inside the roll window / "
                            f"min_days_to_expiry) and no next-month close is stored for {_days()[6]}"]


def test_max_hold_covers_a_short_at_the_next_close(db, monkeypatch):
    _seed()
    _script(monkeypatch, {0: [_sig("SHT", "SHORT", value=120_000.0, max_hold_sessions=2)]})
    res = _run(last=6)
    [t] = res["trades"]
    d = _days()
    assert (t["exit_reason"], t["exit_date"], t["exit_price"], t["holding_sessions"]) == ("MAX_HOLD", d[4], 99.0, 3)


def test_cover_and_buy_rules(db, monkeypatch):
    _seed()
    _script(monkeypatch, {0: [_sig("SHT", "COVER"), _sig("LNG", "BUY")],
                          1: [_sig("LNG", "SHORT", value=120_000.0), _sig("SHT", "SHORT", value=120_000.0)],
                          2: [_sig("SHT", "BUY"), _sig("LNG", "COVER")]})
    res = _run(last=4)
    d = _days()
    assert _events(res) == ["cover ignored: SHT is not held short",
                            "short ignored: LNG is held long in the cash book",
                            "buy ignored: SHT is held short (futures)",
                            "cover ignored: LNG is not held short"]
    assert [e["date"] for e in res["events"]] == [d[0], d[1], d[2], d[2]]


def test_open_futures_at_the_end_without_close_out(db, monkeypatch):
    _seed()
    _script(monkeypatch, {0: [_sig("SHT", "SHORT", value=120_000.0)]})
    res = _run(last=6, close_out_at_end=False)
    # rolled at s5 and still open: the rolled leg is a row, the position is not a closed trade
    [t] = res["trades"]
    assert t["rolled"] and res["metrics"]["trades"] == 0 and res["metrics"]["open_futures_at_end"] == 1
    last = res["equity"][-1]
    assert last["futures_margin"] == 19_600.0
    assert last["equity"] == pytest.approx(last["cash"] + last["futures_margin"], abs=0.011)


# ── round trips from stored rows; Monte Carlo; walk-forward input ─────────

def test_round_trips_from_rows_fold_rolled_legs_on_the_first_basis():
    from backtest.engine import needs_folding, round_trips_from_rows
    rows = [{"symbol": "SHT", "entry_date": "2025-01-02", "exit_date": "2025-01-08", "net_pnl": 2926.61,
             "return_pct": 2.9253, "qty": 1000, "entry_price": 100.0, "instrument": "FUT", "leg": 1, "rolled": 1},
            {"symbol": "LNG", "entry_date": "2025-01-02", "exit_date": "2025-01-14", "net_pnl": -2198.0,
             "return_pct": -2.1958, "qty": 500, "entry_price": 200.0, "instrument": None, "leg": None},
            {"symbol": "SHT", "entry_date": "2025-01-02", "exit_date": "2025-01-14", "net_pnl": 3927.17,
             "return_pct": 4.0055, "qty": 1000, "entry_price": 98.0, "instrument": "FUT", "leg": 2}]
    assert needs_folding(rows) and not needs_folding([rows[1]])
    trips = round_trips_from_rows(rows)
    assert [(t["symbol"], t["net_pnl"], t["qty"], t["entry_price"], t["rows"]) for t in trips] == [
        ("SHT", 6853.78, 1000, 100.0, 2), ("LNG", -2198.0, 500, 200.0, 1)]
    basis = 2926.61 / (2.9253 / 100)                                  # leg 1's, not the sum of both legs
    assert trips[0]["return_pct"] == round(6853.78 / basis * 100, 4)
    assert round_trips_from_rows(rows[:1]) == []                      # only a rolled leg: still open


def test_stored_runs_keep_futures_columns_and_fold_for_monte_carlo(db, monkeypatch):
    from backtest import service, store
    from backtest.engine import round_trips_from_rows
    from db.schema import get_connection
    _seed()
    _script(monkeypatch, {0: [_sig("SHT", "SHORT", value=120_000.0, reason="pair short"),
                              _sig("LNG", "BUY", reason="pair long")],
                          8: [_sig("SHT", "COVER"), _sig("LNG", "SELL")]})
    d = _days()
    r = service.create_and_run({"strategy_id": "scripted", "universe": ["LNG", "SHT"], "start": str(d[0]),
                                "end": str(d[15]), "initial_capital": 1_000_000, "sizing": dict(SIZING),
                                **FLAT_COSTS})
    assert r["status"] == "COMPLETED"
    conn = get_connection()
    try:
        rows = store.get_rows(conn, "backtest_trade", r["run_id"])
        eq = store.get_rows(conn, "backtest_equity", r["run_id"])
    finally:
        conn.close()
    fut = [t for t in rows if t["instrument"] == "FUT"]
    assert [(t["expiry"], t["lots"], t["lot_size"], t["leg"], t["rolled"], t["direction"]) for t in fut] == [
        (d[7], 2, 500, 1, 1, "SHORT"), (d[21], 2, 500, 2, None, "SHORT")]
    assert fut[0]["calendar_spread"] == 1.0 and fut[0]["roll_cost"] == 72.95
    assert [p["futures_margin"] for p in eq[:3]] == [0.0, 20_000.0, 20_000.0]
    trips = round_trips_from_rows(rows)
    assert len(trips) == r["metrics"]["trades"] == 2
    assert r["metrics"]["win_rate"] == 0.5
    mc = service.run_montecarlo(r["run_id"], n_sims=20, seed=1)
    assert (mc.get("result") or mc)["n_trades"] == 2


def test_walk_forward_stitched_stats_count_a_rolled_short_once(db, monkeypatch):
    from backtest.walkforward import run_walk_forward
    _seed()
    _script(monkeypatch, {2: [_sig("SHT", "SHORT", value=120_000.0), _sig("LNG", "BUY")],
                          10: [_sig("SHT", "COVER"), _sig("LNG", "SELL")]})
    d = _days()
    out = run_walk_forward({"strategy_id": "scripted", "universe": ["LNG", "SHT"], "start": str(d[0]),
                            "end": str(d[16]), "initial_capital": 1_000_000, "sizing": dict(SIZING), **FLAT_COSTS},
                           train_sessions=2, validation_sessions=0, test_sessions=15, step_sessions=15)
    assert out["status"] == "COMPLETED"
    [w] = out["windows"]
    # the test window holds three rows (the short rolled at s5, so two contract legs) but two trades
    assert w["test_metrics"]["trade_rows"] == 3 and w["test_metrics"]["futures_rolls"] == 1
    assert out["oos_metrics"]["trades"] == w["test_metrics"]["trades"] == 2


# ── the paper futures book: auto-roll (off by default, PAPER only) ─────────

def _paper(conn, lots=2, lot=500, avg=100.0, margin=20_000.0, realized=-40.0, balance=1_000_000.0):
    from orders.paper import ensure_tables
    ensure_tables(conn)
    d = _days()
    conn.execute("INSERT INTO paper_account (id, balance, opened_at) VALUES (1, ?, 'x')", (balance,))
    conn.execute("INSERT INTO paper_futures_position (strategy_id, underlying, expiry, lots, lot_size, avg_price, "
                 "realized_pnl, margin_blocked) VALUES ('pr','SHT',?,?,?,?,?,?)",
                 (str(d[7]), -lots, lot, avg, realized, margin))
    conn.commit()


def _fp_settings(monkeypatch, **kw):
    from execution import futures_paper as FP
    monkeypatch.setattr(FP, "settings", lambda: {**FP.DEFAULTS, "enabled": True, "slippage_bps": 10.0, **kw})
    return FP


def _book(conn):
    pos = [dict(r) for r in conn.execute("SELECT strategy_id, underlying, expiry, lots, lot_size, avg_price, "
                                         "realized_pnl, margin_blocked FROM paper_futures_position ORDER BY expiry")]
    trades = [dict(r) for r in conn.execute("SELECT expiry, side, lots, price, fees, reason FROM paper_futures_trade "
                                            "ORDER BY rowid")]
    bal = conn.execute("SELECT balance FROM paper_account WHERE id=1").fetchone()[0]
    return pos, trades, bal


def test_paper_auto_roll_rolls_inside_the_window_hand_computed(db, monkeypatch):
    from db.schema import get_connection
    _seed()
    FP = _fp_settings(monkeypatch, auto_roll=True)
    d = _days()
    conn = get_connection()
    _paper(conn)
    conn.close()
    assert FP.settle_expired(d[4]) == {"status": "SUCCESS", "rows": 0, "settled": [], "rolled": [],
                                       "roll_skipped": []}               # 3 sessions left: not yet
    out = FP.settle_expired(d[5])                                      # 2 sessions left: roll
    # buy back E1 at 97 x 1.001 = 97.10 (10 bps), sell E2 at 98 x 0.999 = 97.90; 20 per lot per leg
    # P&L (100 - 97.10) x 1,000 = 2,900; cash 1,000,000 + 20,000 + 2,900 - 40 - (19,580 margin + 40)
    assert out["rows"] == 1 and out["settled"] == [] and out["roll_skipped"] == []
    [r] = out["rolled"]
    assert r == {"strategy_id": "pr", "underlying": "SHT", "from_expiry": str(d[7]), "to_expiry": str(d[21]),
                 "lots": 2, "to_lots": 2, "close_price": 97.1, "open_price": 97.9, "calendar_spread": 1.0,
                 "fees": 80.0, "pnl": pytest.approx(2860.0)}
    conn = get_connection()
    try:
        pos, trades, bal = _book(conn)
    finally:
        conn.close()
    assert bal == pytest.approx(1_003_240.0)
    assert pos[0]["lots"] == 0 and pos[0]["margin_blocked"] == 0 and pos[0]["realized_pnl"] == pytest.approx(2820.0)
    assert pos[1] == {"strategy_id": "pr", "underlying": "SHT", "expiry": d[21], "lots": -2, "lot_size": 500,
                      "avg_price": 97.9, "realized_pnl": -40.0, "margin_blocked": pytest.approx(19_580.0)}
    assert [(t["expiry"], t["side"], t["lots"], t["price"], t["fees"], t["reason"]) for t in trades] == [
        (d[7], "BUY", 2, 97.1, 40.0, "ROLL"), (d[21], "SELL", 2, 97.9, 40.0, "ROLL")]
    # once rolled, nothing expires at E1
    assert FP.settle_expired(d[8])["settled"] == []


def test_paper_auto_roll_is_off_by_default_and_then_the_short_expires(db, monkeypatch):
    from db.schema import get_connection
    from execution import futures_paper as FP
    assert FP.DEFAULTS["auto_roll"] is False
    _seed()
    _fp_settings(monkeypatch)
    d = _days()
    conn = get_connection()
    _paper(conn)
    conn.close()
    assert FP.settle_expired(d[5]) == {"status": "SUCCESS", "rows": 0, "settled": []}    # the pre-W40 shape
    out = FP.settle_expired(d[8])
    assert [(s["expiry"], s["price"], s["lots"]) for s in out["settled"]] == [(d[7], 96.5, 2)]


def test_paper_auto_roll_without_a_next_month_price_is_skipped_then_settles(db, monkeypatch):
    from db.schema import get_connection
    _seed(contracts=False)
    FP = _fp_settings(monkeypatch, auto_roll=True)
    d = _days()
    conn = get_connection()
    _paper(conn)
    conn.close()
    out = FP.settle_expired(d[5])
    assert out["rolled"] == [] and out["roll_skipped"] == [
        {"strategy_id": "pr", "underlying": "SHT", "expiry": str(d[7]),
         "reason": f"no next-month close stored for {d[5]}"}]
    assert [s["expiry"] for s in FP.settle_expired(d[8])["settled"]] == [d[7]]


def test_paper_cover_after_a_roll_prices_off_the_held_contract(db, monkeypatch):
    from db.schema import get_connection
    _seed()
    FP = _fp_settings(monkeypatch, auto_roll=True)
    d = _days()
    conn = get_connection()
    _paper(conn)
    # only sessions up to s5 stored: the near contract in fo_underlying_daily is still E1
    conn.execute("DELETE FROM fo_underlying_daily WHERE date>?", (str(d[5]),))
    conn.execute("DELETE FROM fo_contract_daily WHERE date>?", (str(d[5]),))
    conn.commit()
    conn.close()
    FP.settle_expired(d[5])
    conn = get_connection()
    try:
        assert FP.contract(conn, "SHT")["expiry"] == str(d[7])
        assert FP.book(conn)["positions"][0]["mark"] == 98.0          # the held E2 contract's close
        assert FP.gross_notional(conn) == 98_000.0
        res = FP.fill(conn, {"symbol": "SHT", "side": "BUY", "quantity": 1000, "strategy_id": "pr", "order_id": "O1"})
        # covered at E2's 98 + 10 bps = 98.10 (not rejected as "the held short is the next contract")
        assert res["status"] == "FILLED" and res["price"] == 98.1
        pos, _t, _b = _book(conn)
    finally:
        conn.close()
    assert pos[1]["lots"] == 0


def test_paper_auto_roll_settings_are_strict(tmp_path, monkeypatch):
    from execution import futures_paper as FP
    monkeypatch.chdir(tmp_path)
    assert (FP.settings()["auto_roll"], FP.settings()["roll_days_before_expiry"]) == (False, 2)   # no config.json
    (tmp_path / "atip_data").mkdir()
    cfg = tmp_path / "atip_data" / "config.json"
    for given, want in (({"auto_roll": "yes", "roll_days_before_expiry": -1}, (False, 2)),
                        ({"auto_roll": 1, "roll_days_before_expiry": True}, (False, 2)),
                        ({"auto_roll": True, "roll_days_before_expiry": 0}, (True, 0))):
        cfg.write_text(json.dumps({"futures": given}), encoding="utf-8")
        assert (FP.settings()["auto_roll"], FP.settings()["roll_days_before_expiry"]) == want


# ── strategy versions: pairs with short_via_futures, end to end ────────────

WAVES = ("AAA", "BBB", "CCC")
LEGACY = ("2025-01-01", "2025-09-30")


def _seed_waves():
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
    _exec("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)", rows)
    return days


def _seed_wave_futures(days, symbols=("AAA", "BBB"), lot=250):
    """Monthly futures on the last session of each month: near = spot x 1.004, next = spot x 1.010."""
    by_month = {}
    for d in days:
        by_month[(d.year, d.month)] = d
    expiries = sorted(by_month.values())
    closes = {}
    from db.schema import get_connection
    conn = get_connection()
    for s, d, c in conn.execute("SELECT symbol, date, close FROM prices_daily"):
        closes[(s, str(d)[:10])] = c
    conn.close()
    summ, cons = [], []
    for d in days:
        live = [e for e in expiries if e >= d]
        for s in symbols:
            c = closes[(s, str(d))]
            near = live[0]
            summ.append((str(d), s, "STOCK", c, round(c * 1.004, 2), str(near), lot))
            cons.append((str(d), s, "STF", str(near), 0.0, "XX", round(c * 1.004, 2), lot))
            if len(live) > 1:
                cons.append((str(d), s, "STF", str(live[1]), 0.0, "XX", round(c * 1.010, 2), lot))
    _exec("INSERT INTO fo_underlying_daily (date,symbol,kind,underlying_price,fut_close,near_expiry,lot_size) "
          "VALUES (?,?,?,?,?,?,?)", summ)
    _exec("INSERT INTO fo_contract_daily (date,symbol,instrument,expiry,strike,option_type,close,lot_size) "
          "VALUES (?,?,?,?,?,?,?,?)", cons)


def _pairs(sid, **kw):
    return {"strategy_id": sid, "name": sid, "version": "1.0.0", "kind": "pairs",
            "pairs": [{"asset_a": "AAA", "asset_b": "BBB", "lookback": 30, "entry_z": 1.0, "exit_z": 0.3,
                       "stop_z": 3.5, "capital_allocation_pct": 20}],
            "position": {"target_position_pct": 10, "max_positions": 4}, **kw}


def _store(*defs):
    from db.schema import get_connection
    from strategy_engine import registry
    conn = get_connection()
    try:
        for d in defs:
            registry.create_strategy(conn, d)
    finally:
        conn.close()


def test_a_pairs_version_with_short_via_futures_trades_both_legs(db):
    from backtest import service, store
    from backtest.engine import round_trips_from_rows
    from db.schema import get_connection
    days = _seed_waves()
    _seed_wave_futures(days)
    _store(_pairs("pr_fut", short_via_futures=True))
    r = service.create_and_run({"strategy_id": "pr_fut", "universe": ["AAA", "BBB"], "start": LEGACY[0],
                                "end": LEGACY[1]})
    assert r["status"] == "COMPLETED", r
    conn = get_connection()
    try:
        rows = store.get_rows(conn, "backtest_trade", r["run_id"])
        eq = store.get_rows(conn, "backtest_equity", r["run_id"])
        bias = json.loads(conn.execute("SELECT bias_report_json FROM backtest_run WHERE run_id=?",
                                       (r["run_id"],)).fetchone()[0])
    finally:
        conn.close()
    fut = [t for t in rows if t["instrument"] == "FUT"]
    cash = [t for t in rows if not t["instrument"]]
    assert fut and cash, "both legs must trade"
    assert all(t["qty"] == t["lots"] * t["lot_size"] and t["lot_size"] == 250 and t["direction"] == "SHORT"
               for t in fut)
    rolled = [t for t in fut if t["rolled"]]
    assert rolled, "a short held across a month end is rolled"
    for t in rolled:                                                  # the next leg continues the position
        nxt = [u for u in fut if u["symbol"] == t["symbol"] and u["entry_date"] == t["entry_date"]
               and u["leg"] == t["leg"] + 1]
        assert len(nxt) == 1 and nxt[0]["expiry"] > t["expiry"] and t["roll_cost"] > 0
        assert t["calendar_spread"] > 0                               # next = 1.010 x spot, near 1.004 x spot
    m = r["metrics"]
    assert m["futures_rolls"] == len(rolled) and m["trade_rows"] == len(rows)
    assert m["trades"] == len(round_trips_from_rows(rows)) == len(rows) - len(rolled)
    assert sum(t["net_pnl"] for t in rows) == pytest.approx(eq[-1]["equity"] - 1_000_000, abs=0.01 * len(rows))
    for p in eq:                                                      # equity = cash + longs + margin
        assert p["equity"] == pytest.approx(p["cash"] + p["positions_value"] + p["futures_margin"], abs=0.02)
    assert "futures" in bias and not any("not simulated" in w for w in bias["warnings"])


def test_the_event_driven_engine_keeps_the_pre_w40_pair_path(db):
    """BT-17 does not trade SHORT, so the adapter does not offer it dv_fno: the pair's short leg is
    a SELL decision and its long leg is held back, as before W40 -- never a naked long."""
    days = _seed_waves()
    _seed_wave_futures(days)
    _store(_pairs("pr_fut", short_via_futures=True))
    res = _legacy_run("pr_fut", ["AAA", "BBB"], event_driven=True)
    assert res["trades"] == [] and not any("SHORT" in e["event"] for e in res["events"])


def test_without_fno_data_a_short_via_futures_pair_is_unchanged(db):
    """No F&O rows: dv_fno is absent, the evaluator takes its old SELL path -- the result is the
    pre-W40 digest (LEGACY_W40 pr_fut_nofno)."""
    _seed_waves()
    _store(_pairs("pr_fut_nofno", short_via_futures=True))
    res = _legacy_run("pr_fut_nofno", ["AAA", "BBB"])
    assert "futures" not in res["bias_report"] and _digest(res) == LEGACY_W40["pr_fut_nofno"][2]


# ── live: a code strategy's SHORT / COVER become SHORT / COVER decisions ────

def test_code_strategy_short_and_cover_become_decisions_live(db):
    from backtest import strategies
    from backtest.strategy import Signal, Strategy
    from strategy_engine.kinds import PythonEvaluator
    seen = {}

    class LS(Strategy):
        strategy_id, version, default_params = "ls", "1", {}

        def on_bar(self, ctx):
            seen["positions"], seen["futures"] = dict(ctx.positions), dict(ctx.futures)
            return [Signal("NEW", "SHORT", value=1e5), Signal("HLD", "COVER"), Signal("LNG", "COVER"),
                    Signal("LNG", "SHORT")]

    strategies.REGISTRY["ls"] = LS
    try:
        ev = PythonEvaluator.__new__(PythonEvaluator)
        ev.defn, ev.params = {"python_class": "ls", "position": {}, "risk": {}}, {}

        def intent(sym, as_of, act, conf, why, ctx, entry=False):
            return SimpleNamespace(symbol=sym, action=act, features={}, stop_price=None, target_price=None,
                                   max_hold_sessions=None, reason=why)
        ev.intent = intent
        ev.limit_buys = lambda cands, n: [c[2] for c in cands]
        env = SimpleNamespace(history=SimpleNamespace(view=lambda d: None), universe=["NEW", "HLD", "LNG"],
                              scores=None, context=lambda s, d: {"close": 100.0})
        out = ev.decide(env, _days()[5], {"LNG": {"qty": 10, "entry_price": 100},
                                          "HLD": {"qty": -500, "short": True, "entry_price": 50, "expiry": "x"}})
    finally:
        strategies.REGISTRY.pop("ls", None)
    acts = sorted((i.symbol, i.action) for i in out)
    assert acts == [("HLD", "COVER"), ("LNG", "HOLD"), ("NEW", "SHORT")]
    assert set(seen["positions"]) == {"LNG"} and seen["futures"]["HLD"].qty == -500


def test_signal_validation_for_short_legs():
    from backtest.strategy import Signal
    for kw in ({"side": "COVER", "quantity": 5}, {"side": "SHORT", "fraction": 0.5},
               {"side": "SHORT", "stop_price": 110.0}, {"side": "SHORT", "value": 0},
               {"side": "SHORT", "quantity": 5, "value": 1e5}, {"side": "BUY", "value": 1e5}):
        with pytest.raises(ValueError):
            Signal("X", **kw)
    assert Signal("X", "SHORT", value=1e5).value == 1e5 and Signal("X", "SHORT", quantity=500).quantity == 500
    assert Signal("X", "COVER").side == "COVER"


def test_futures_settings_are_validated_and_only_snapshotted_when_set(db, monkeypatch):
    from backtest import service
    _script(monkeypatch, {})
    base = {"strategy_id": "scripted", "universe": ["LNG"], "start": "2025-01-01", "end": "2025-01-31"}
    assert "futures" not in service.resolve_config(base)               # unchanged snapshot
    snap = service.resolve_config({**base, "futures": {"roll_days_before_expiry": None}})
    assert snap["futures"] == {"margin_pct": 20.0, "roll_days_before_expiry": None, "min_days_to_expiry": 3,
                               "cost_model": "nse_futures", "cost_overrides": {}}
    for bad in ({"margin_pct": 0}, {"margin_pct": 120}, {"roll_days_before_expiry": -1},
                {"roll_days_before_expiry": True}, {"cost_model": "nse_delivery"}, {"lots": 2},
                {"cost_overrides": {"nope": 1}}):
        with pytest.raises(ValueError):
            service.resolve_config({**base, "futures": bad})


# ── BUY / SELL-only runs are byte-identical ────────────────────────────────

def _digest(res) -> str:
    from backtest.metrics import jsonable
    return hashlib.sha256(json.dumps(jsonable(res), sort_keys=True).encode()).hexdigest()


def _pf(sid, **kw):
    return {"strategy_id": sid, "name": sid, "version": "1.0.0", "kind": "portfolio",
            "universe": {"type": "symbols", "symbols": list(WAVES) + ["UPP", "DWN", "RUN"]},
            "score": {"feature": "rsi_14"}, "top_n": 2, "bottom_n": 2, "method": "equal", "rebalance_every": 5,
            "long_short": "dollar", "position": {"target_position_pct": 10, "max_positions": 6, "stop_pct": 25}, **kw}


LEGACY_DEFS = [_pairs("pr_single", allow_single_leg=True), _pairs("pr_fut_nofno", short_via_futures=True),
               _pairs("pr_fut_single_nofno", short_via_futures=True, allow_single_leg=True),
               _pf("pf_ls_lo", allow_long_only=True), _pf("pf_fut_nofno", short_via_futures=True),
               {"strategy_id": "mf_reduce", "name": "mf_reduce", "version": "1.0.0", "kind": "multi_factor",
                "universe": {"type": "symbols", "symbols": list(WAVES)},
                "factors": [{"feature": "rsi_14", "weight": 1, "min": 0, "max": 100, "threshold": 50}],
                "entry_threshold": 60, "exit_threshold": 30, "reduce_threshold": 45,
                "position": {"target_position_pct": 10, "max_positions": 3, "stop_pct": 10}}]
ALL6 = list(WAVES) + ["UPP", "DWN", "RUN"]

# Full-result digests computed with the engines BEFORE W40 (commit d01705e), same data, same requests.
# None of these strategies emits a SHORT reaching the engine (pairs / portfolios without F&O data take
# the old SELL path), so W40 must not change a byte of them. ("ed_" = the event-driven engine, whose
# result embeds the resolved snapshot: the snapshot must not change either.)
LEGACY_W40 = {
    "momentum": ({"strategy_id": "momentum", "universe": ALL6},
                 0, "7f8b45aeefc564aa3fbb7ecce14a4d38abdcd18c4cb28d4d2e741668094ba689"),
    "mean_reversion_rsi": ({"strategy_id": "mean_reversion_rsi", "universe": list(WAVES)},
                           0, "f28d3dd58b51777e1edd0f3e67a1673422b80487fe54a433d285415453bdc1d5"),
    "dip_open_end": ({"strategy_id": "dip", "universe": list(WAVES), "close_out_at_end": False},
                     29, "47b3ea9d983232036a5acdedd22c3ebcc7b24cacc78a68557dcc40bf757bbe2c"),
    "mf_reduce": ({"strategy_id": "mf_reduce", "universe": list(WAVES)},
                  19, "72515ab8de678d451fde7215e28d7e8fcb500ea920d1cd44294e9724593d1cb8"),
    "pr_single": ({"strategy_id": "pr_single", "universe": ["AAA", "BBB"]},
                  28, "75fb78d0c879d18b3cf90719a662b1565f78e2d74a15ff5bd8926217902c941c"),
    "pr_fut_nofno": ({"strategy_id": "pr_fut_nofno", "universe": ["AAA", "BBB"]},
                     0, "84b24511703fd887904b68725cbfb9a00e9f7d542f85d7640b33329b2d1bf139"),
    "pr_fut_single_nofno": ({"strategy_id": "pr_fut_single_nofno", "universe": ["AAA", "BBB"]},
                            28, "3e90f5680fdd183364dd60b5dd9a3a0bd976f93a08aeddc3c8db0d4a324d46f2"),
    "pf_ls_lo": ({"strategy_id": "pf_ls_lo", "universe": ALL6},
                 2, "cbd709b1ced8eb8f06b39d71f153a30218a94377ad53322c1618e3d95c238f7f"),
    "pf_fut_nofno": ({"strategy_id": "pf_fut_nofno", "universe": ALL6},
                     2, "68a431a00de983165f939174ed2b77c22922a00fb1cec5ea2ffc1e20d919c918"),
    "ed_pr_single": ({"strategy_id": "pr_single", "universe": ["AAA", "BBB"]},
                     28, "d3198e9d3640e330ccf1caba73f672e284296ab3803a3ae9a599f072de0060fd"),
    # re-pinned on merging BT-17's close-out fix (was b920c72d...): the event engine's last equity row now
    # carries the end-of-window close-out in its daily_return; the full result differs in exactly that one
    # leaf (equity[186].daily_return 4.418e-05 -> -4.59e-05). Nothing of the W40 futures work changes it.
    "ed_pf_fut_nofno": ({"strategy_id": "pf_fut_nofno", "universe": ALL6},
                        2, "f39275a58471f4341a135abc124d03d282bc53fdfbde626788e675ef9a71e133"),
}


def _legacy_run(sid, universe, event_driven=False, **kw):
    from backtest import engine, event_driven as ED, service
    from db.schema import get_connection
    req = {"strategy_id": sid, "universe": universe, "start": LEGACY[0], "end": LEGACY[1], **kw}
    conn = get_connection()
    try:
        if event_driven:
            return ED.run({**req, "event_driven": {}}, conn)
        return engine.run(service.resolve_config(req), conn)
    finally:
        conn.close()


@pytest.mark.parametrize("name", sorted(LEGACY_W40))
def test_buy_sell_only_runs_are_byte_identical(db, name):
    _seed_waves()
    _store(*LEGACY_DEFS)
    req, trades, digest = LEGACY_W40[name]
    req = dict(req)
    res = _legacy_run(req.pop("strategy_id"), req.pop("universe"), event_driven=name.startswith("ed_"), **req)
    assert all(not ({"instrument", "lots", "expiry"} & set(t)) for t in res["trades"])
    assert "futures" not in res["bias_report"] and not any(k.startswith("futures") for k in res["metrics"])
    assert all("futures_margin" not in p for p in res["equity"])
    assert (res["metrics"]["trades"], _digest(res)) == (trades, digest)
