"""
BT-17: the event-driven engine (backtest/event_driven.py) reports the decisions it does not
simulate the way the W2 engine (backtest/engine.py) does, and its last daily return includes
the close-out at the end of the window.

  skipped decisions  a W3 strategy version's SHORT / COVER and its ADD / REDUCE of a symbol not
        held never become signals; strategy_engine/adapter.py counts them in `not_simulated`
        and the W2 engine states them in the bias report's warnings. The event engine now
        states the same line. A signal for a symbol outside the universe is an event in both.
  close-out return   the positions closed at the end of the window used to change the last
        equity row but not its daily_return, so chained daily returns (CPCV, PBO, the return
        bootstrap) overstated the final equity by the close-out costs

The market and the scripted strategy are test_w39_backtest_partial's (hand-written prices, flat
0.1% costs, no slippage / impact), so the numbers are worked out by hand in the comments.
"""

from types import SimpleNamespace

import pytest

from tests.test_w39_backtest_partial import (LEGACY, WAVES, _days, _flat, _mf, _run, _run_ed, _script, _seed,
                                             _seed_waves, _sig, _store)

NOT_SIMULATED = ("decisions not simulated: ADD (not held) x1, COVER x2, REDUCE (not held) x1, SHORT x1 -- the "
                 "backtest is a long-only cash book (no short legs)")


@pytest.fixture
def db(temp_db, tmp_path, monkeypatch):
    import backtest.config as bc
    from db.schema import init_db
    monkeypatch.setattr(bc, "CONFIG_PATH", tmp_path / "no_config.json")     # DEFAULTS, whatever config.json says
    init_db()
    return temp_db


def test_a_versions_skipped_decisions_are_reported_by_both_engines(db, monkeypatch):
    """The real adapter (strategy_engine/adapter.py) over a stored version whose evaluator is replaced:
    on the first session it decides SHORT AAA, COVER BBB twice and BUY CCC; on the second ADD AAA and
    REDUCE BBB, neither held. The BUY trades; the other five are counted, never traded."""
    import strategy_engine.adapter as AD
    from backtest import engine, service
    from backtest import event_driven as ED
    from db.schema import get_connection
    from strategy_engine.decisions import StrategyDecision
    _seed_waves()
    _store(_mf("mf_skips"))
    d = _days(*LEGACY)
    plan = {d[0]: [("AAA", "SHORT"), ("BBB", "COVER"), ("BBB", "COVER"), ("CCC", "BUY")],
            d[1]: [("AAA", "ADD"), ("BBB", "REDUCE")]}

    def decide(env, as_of, held, idx):
        return [StrategyDecision("mf_skips", "1.0.0", sym, as_of, action, None, ["scripted"])
                for sym, action in plan.get(as_of, [])]
    monkeypatch.setattr(AD, "make_evaluator", lambda defn, params, load: SimpleNamespace(decide=decide))
    req = {"strategy_id": "mf_skips", "universe": list(WAVES), "start": LEGACY[0], "end": str(d[20])}
    conn = get_connection()
    try:
        w2 = engine.run(service.resolve_config(req), conn)
        ed = ED.run({**req, "event_driven": {"impact": "none"}}, conn)
    finally:
        conn.close()
    for res in (w2, ed):
        assert NOT_SIMULATED in res["bias_report"]["warnings"]
        assert [(t["symbol"], t["exit_reason"]) for t in res["trades"]] == [("CCC", "END_OF_WINDOW")]
        assert not any(e["symbol"] in ("AAA", "BBB") for e in res["events"])     # warnings, not events
    assert not any("signals not simulated by the event-driven engine" in w for w in ed["bias_report"]["warnings"])
    # a version that only BUYs / EXITs: no such warning in either engine
    plan.clear()
    plan[d[0]] = [("CCC", "BUY")]
    conn = get_connection()
    try:
        ed = ED.run({**req, "event_driven": {"impact": "none"}}, conn)
    finally:
        conn.close()
    assert not any("not simulated" in w for w in ed["bias_report"]["warnings"])


def test_a_signal_outside_the_universe_is_an_event_in_both_engines(db, monkeypatch):
    _seed(_flat(100))
    _script(monkeypatch, {0: [_sig("BUY", "ZZZ", stop_price=95.0), _sig("BUY", stop_price=95.0)],
                          2: [_sig("SELL", "ZZZ")]})
    d = _days()
    want = [{"date": d[0], "symbol": "ZZZ", "event": "signal outside universe ignored"},
            {"date": d[2], "symbol": "ZZZ", "event": "signal outside universe ignored"}]
    w2, ed = _run(), _run_ed()
    assert w2["events"] == ed["events"] == want
    assert [t["symbol"] for t in w2["trades"]] == [t["symbol"] for t in ed["trades"]] == ["PAR"]
    assert ed["metrics"]["events"] == 2


def test_the_close_out_is_in_the_last_daily_return(db, monkeypatch):
    # PAR stays at 100. BUY 100 at s1's open (0.5% risk over a 5.00 stop): 10,000 + 10.00 costs, equity
    # 99,990 from s1. At the end (s15) the 100 shares are closed at 100: 10,000 - 10.00, cash 99,980.
    # The last return is 99,980 / 99,990 - 1 = -0.00010001 (it used to be the mark's 0.0)
    _seed(_flat(100))
    _script(monkeypatch, {0: [_sig("BUY", stop_price=95.0)]})
    w2, ed = _run(), _run_ed()
    rets = [p["daily_return"] for p in ed["equity"]]
    assert rets == [p["daily_return"] for p in w2["equity"]]
    assert (rets[1], rets[-1], ed["equity"][-1]["equity"]) == (-0.0001, -0.00010001, 99_980.0)
    growth = 100_000.0
    for r in rets:
        growth *= 1 + r
    assert growth == pytest.approx(ed["equity"][-1]["equity"], abs=0.01)
    # left open at the end: the last row is the mark, as before
    ed = _run_ed(close_out_at_end=False)
    assert (ed["equity"][-1]["daily_return"], ed["equity"][-1]["equity"]) == (0.0, 99_990.0)
