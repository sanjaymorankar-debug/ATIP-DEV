"""
BT-04: Bayesian parameter search -- optimize(method="bayes"), a Tree-structured Parzen
Estimator (Bergstra, Bardenet, Bengio & Kegl 2011) in numpy (backtest/optimize.py).

Each trial's backtest is replaced by a synthetic objective (as tests/test_w39_qa_suite.py does
for the sensitivity analysis): sharpe = a smooth peak at stop_pct 7.3, target_pct 22,
max_hold_sessions 20, 50 trades. On it the search reaches the optimum region in fewer trials
than random search on average over seeds; it respects bounds, integer ranges, value lists,
step grids and categorical values; it is deterministic for a seed (its first trials ARE the
random method's); its trials count in the deflated Sharpe; it is refused on the test window;
and one small search runs through the real engine and is stored like any optimisation.
"""

import json
import math
import re
from types import SimpleNamespace

import numpy as np
import pytest

START, END = "2025-01-01", "2025-09-30"
WAVES = ("AAA", "BBB", "CCC")
SPACE = {"stop_pct": {"min": 1.0, "max": 10.0}, "target_pct": {"min": 2, "max": 40},
         "max_hold_sessions": {"values": [5, 10, 15, 20, 25, 30, 35, 40]}}


def _objective(p):
    return 2.0 - ((p["stop_pct"] - 7.3) / 2.0) ** 2 - ((p["target_pct"] - 22) / 5.0) ** 2 \
        - ((p["max_hold_sessions"] - 20) / 8.0) ** 2


@pytest.fixture
def db(temp_db, tmp_path, monkeypatch):
    import backtest.config as bc
    from db.schema import init_db
    monkeypatch.setattr(bc, "CONFIG_PATH", tmp_path / "no_config.json")
    init_db()
    return temp_db


@pytest.fixture
def stub(db, monkeypatch):
    """Every trial scores _objective (or `score` when given); the best trial's daily returns are a
    fixed series of 250 sessions, so the deflated Sharpe is computed."""
    import backtest.optimize as OPT
    calls = []
    state = {"score": _objective}

    def trial(base, params, kind, parent, idx):
        calls.append(dict(params))
        return f"T{idx}", {"status": "COMPLETED", "metrics": {"sharpe": state["score"](params), "trades": 50}}
    monkeypatch.setattr(OPT, "_trial", trial)
    monkeypatch.setattr(OPT, "_returns", lambda run_id: list(np.random.default_rng(5).normal(0.001, 0.01, 250)))
    return SimpleNamespace(calls=calls, state=state)


def _req(**kw):
    return {"strategy_id": "dip", "universe": list(WAVES), "start": START, "end": END, **kw}


def _first_hit(out, threshold):
    """1-based index of the first trial scoring >= threshold (budget + 1 if none)."""
    by_index = sorted(out["trials"], key=lambda t: t["index"])
    return next((t["index"] + 1 for t in by_index if t["score"] is not None and t["score"] >= threshold),
                len(by_index) + 1)


def test_bayes_reaches_the_optimum_region_in_fewer_trials_than_random(stub):
    from backtest.optimize import optimize
    hits = {"random": [], "bayes": []}
    best = {"random": [], "bayes": []}
    for seed in range(10):
        for method in hits:
            out = optimize(_req(), SPACE, method, max_trials=40, seed=seed)
            assert out["status"] == "COMPLETED" and len(out["trials"]) == 40
            hits[method].append(_first_hit(out, 1.5))
            best[method].append(out["best"]["score"])
    # the region scoring >= 1.5 is about 1% of the space: random search mostly misses it in 40 trials
    assert np.mean(hits["bayes"]) < 0.75 * np.mean(hits["random"])
    assert sum(h <= 40 for h in hits["bayes"]) == 10 > sum(h <= 40 for h in hits["random"])
    assert np.mean(best["bayes"]) > np.mean(best["random"])


def test_bayes_respects_bounds_integers_value_lists_and_grids(stub):
    from backtest.optimize import optimize
    space = {"stop_pct": {"min": 1.5, "max": 4.5}, "target_pct": {"min": 3, "max": 9},
             "max_hold_sessions": {"values": [7, 3, 11]}, "range_lookback": {"min": 10, "max": 30, "step": 5},
             "dip_zone_pct": {"values": ["low", "mid", "high"]}}
    stub.state["score"] = lambda p: (-(p["stop_pct"] - 4.4) ** 2 - abs(p["target_pct"] - 4)
                                     - (p["dip_zone_pct"] != "mid"))
    out = optimize(_req(params={"max_new_per_day": 2}), space, "bayes", max_trials=40, seed=3)
    assert len(stub.calls) == len(out["trials"]) == 40
    for p in stub.calls:
        assert p["max_new_per_day"] == 2                                   # outside the space: the request's
        assert isinstance(p["stop_pct"], float) and 1.5 <= p["stop_pct"] <= 4.5
        assert p["stop_pct"] == round(p["stop_pct"], 6)
        assert type(p["target_pct"]) is int and 3 <= p["target_pct"] <= 9
        assert p["max_hold_sessions"] in (7, 3, 11) and p["range_lookback"] in (10, 15, 20, 25, 30)
        assert p["dip_zone_pct"] in ("low", "mid", "high")
    assert len({tuple(sorted(p.items())) for p in stub.calls}) == 40               # never the same set twice
    # the model narrows in on the corner it was given
    late = stub.calls[20:]
    assert sum(p["dip_zone_pct"] == "mid" for p in late) >= 15 and np.mean([p["stop_pct"] for p in late]) > 3.5


def test_bayes_is_deterministic_for_a_seed_and_starts_with_the_random_draws(stub):
    from backtest.optimize import optimize, tpe_startup
    a = optimize(_req(), SPACE, "bayes", max_trials=30, seed=11)
    b = optimize(_req(), SPACE, "bayes", max_trials=30, seed=11)
    c = optimize(_req(), SPACE, "bayes", max_trials=30, seed=12)
    seq = [[t["params"] for t in sorted(o["trials"], key=lambda t: t["index"])] for o in (a, b, c)]
    assert seq[0] == seq[1] and seq[0] != seq[2]
    rnd = optimize(_req(), SPACE, "random", max_trials=30, seed=11)
    n0 = tpe_startup(30)
    assert n0 == 6 and a["search"]["startup_trials"] == n0
    assert seq[0][:n0] == [t["params"] for t in sorted(rnd["trials"], key=lambda t: t["index"])][:n0]
    assert a["search"] == {"estimator": "TPE (Bergstra et al. 2011)", "startup_trials": 6, "gamma": 0.25,
                           "good_max": 25, "candidates": 24, "seed": 11}
    assert "search" not in rnd
    assert [tpe_startup(n) for n in (1, 3, 5, 24, 60, 400)] == [1, 3, 5, 5, 12, 80]


def test_every_bayes_trial_counts_in_the_deflated_sharpe(stub):
    from backtest.optimize import deflated_sharpe, optimize
    rets = list(np.random.default_rng(5).normal(0.001, 0.01, 250))
    small = optimize(_req(), SPACE, "bayes", max_trials=8, seed=1)
    big = optimize(_req(), SPACE, "bayes", max_trials=60, seed=1)
    for out, n in ((small, 8), (big, 60)):
        dsr = out["diagnostics"]["deflated_sharpe"]
        assert dsr["trials"] == out["diagnostics"]["trials"] == n
        ran = sorted(out["trials"], key=lambda t: t["index"])
        assert dsr == deflated_sharpe(rets, [t["metrics"]["sharpe"] for t in ran])
        # the same Sharpes counted as fewer trials would expect less from luck: the count matters
        assert deflated_sharpe(rets, [t["metrics"]["sharpe"] for t in ran][:2])["trials"] == 2


def test_unscored_trials_are_never_good_and_a_small_space_ends_the_search(stub):
    from backtest.optimize import TPE, optimize
    # trials under min_trades score None: they model g(x), never l(x)
    tpe = TPE(SPACE, 0)
    trials = [{"index": i, "params": {"stop_pct": 1.0 + i, "target_pct": 5, "max_hold_sessions": 5}, "score": s}
              for i, s in enumerate([None, 0.5, None, 1.5, 0.2, None, 0.9, 1.5])]
    good, bad = tpe.split(trials)
    assert [t["index"] for t in good] == [3, 7] and {0, 2, 5} <= {t["index"] for t in bad}
    assert TPE(SPACE, 0).split([trials[0]]) == ([], [trials[0]])
    # 3 x 2 = 6 combinations: six distinct trials, then the search stops (budget 20)
    stub.state["score"] = lambda p: p["stop_pct"] - abs(p["target_pct"] - 5)
    out = optimize(_req(), {"stop_pct": {"values": [2, 3, 4]}, "target_pct": {"min": 5, "max": 6}}, "bayes",
                   max_trials=20, seed=0)
    assert len(out["trials"]) == len(stub.calls) == 6
    assert sorted((p["stop_pct"], p["target_pct"]) for p in stub.calls) == [(a, b) for a in (2, 3, 4) for b in (5, 6)]


def test_bayes_is_refused_on_the_test_window_and_validated_like_the_others(stub):
    from backtest.optimize import optimize, prepare
    periods = {"research": [START, "2025-06-30"], "test": ["2025-07-01", END]}
    for req in ({"strategy_id": "dip", "universe": list(WAVES), "periods": periods, "period_label": "test",
                 "allow_test": True}, _req(allow_test=True)):
        with pytest.raises(ValueError, match="test window is refused"):
            optimize(req, SPACE, "bayes")
    with pytest.raises(ValueError, match=re.escape("method must be one of ('grid', 'random', 'adaptive', 'bayes')")):
        prepare(_req(), SPACE, "bayesian", 10)
    with pytest.raises(ValueError, match="max_trials"):
        prepare(_req(), SPACE, "bayes", 401)
    with pytest.raises(ValueError, match="min <= max"):
        prepare(_req(), {"stop_pct": {"min": 5, "max": 1}}, "bayes", 10)
    assert stub.calls == []


def test_the_normal_mass_and_the_parameter_encodings():
    from statistics import NormalDist

    from backtest.optimize import _Dim, _mass
    nd = NormalDist()
    for a, b in ((-1.0, 1.0), (2.0, 2.5), (-7.0, -6.0), (6.0, 9.0), (0.1, 0.1005), (-40.0, 40.0)):
        want = nd.cdf(b) - nd.cdf(a)
        assert float(_mass(a, b)) == pytest.approx(want, rel=1e-5, abs=1e-15), (a, b)
    d = _Dim({"values": [30, 10, 20]})
    assert (d.kind, d.values, d.encode(20), d.decode(1.4), d.decode(-0.5), d.decode(2.49)) == (
        "ord", [10, 20, 30], 1.0, 20, 10, 30)
    d = _Dim({"min": 3, "max": 9})
    assert (d.kind, d.decode(2.6), d.decode(9.49), d.decode(5.5), d.size()) == ("int", 3, 9, 6, 7)
    d = _Dim({"min": 0.5, "max": 2})
    assert (d.kind, d.decode(1.23456789), d.decode(7.0), d.size()) == ("float", 1.234568, 2.0, None)
    assert _Dim({"min": 2.0, "max": 2.0}).decode(0.0) == 2.0 and _Dim({"values": ["a", "b"]}).kind == "cat"
    # densities: each value's unit bin of a numeric grid; the probabilities over the values sum to 1
    d = _Dim({"min": 1, "max": 6, "step": 1})
    model = d.model(np.array([1.0, 1.0, 2.0]))
    assert math.fsum(np.exp(d.log_density(model, np.arange(6.0)))) == pytest.approx(1.0, abs=1e-9)
    d = _Dim({"values": ["x", "y", "z"]})
    assert np.exp(d.log_density(d.model(np.array([0.0, 0.0])), np.arange(3.0))) == pytest.approx(
        [(2 + 1 / 3) / 3, (1 / 3) / 3, (1 / 3) / 3])


def test_a_bayes_search_through_the_engine_is_stored_like_any_optimisation(db, capsys):
    """Real backtests on test_w39b_cpcv_pbo's wave market: six opt_trial runs under one optimization
    parent; the CLI takes --method bayes; PBO reads the trials."""
    from backtest import store
    from backtest.__main__ import main
    from backtest.cpcv import pbo_for_run
    from backtest.optimize import optimize
    from db.schema import get_connection
    _seed_market()
    space = {"target_pct": {"min": 3, "max": 12}, "stop_pct": {"values": [2, 4]}}
    out = optimize(_req(), space, "bayes", max_trials=6, seed=4, min_trades=1)
    assert out["status"] == "COMPLETED" and len(out["trials"]) == 6
    conn = get_connection()
    try:
        parent = store.get_run(conn, out["run_id"])
        kids = store.child_runs(conn, out["run_id"])
    finally:
        conn.close()
    assert (parent["kind"], parent["config"]["optimization"]["method"]) == ("optimization", "bayes")
    assert parent["summary"]["search"]["startup_trials"] == 5
    assert [k["kind"] for k in kids] == ["opt_trial"] * 6 and all(k["status"] == "COMPLETED" for k in kids)
    assert sorted(k["params"]["target_pct"] for k in kids) == sorted(t["params"]["target_pct"] for t in out["trials"])
    assert pbo_for_run(out["run_id"], 4)["n_trials"] == 6
    main(["optimize", "--strategy", "dip", "--universe", ",".join(WAVES), "--start", START, "--end", END,
          "--space", json.dumps(space), "--method", "bayes", "--trials", "6", "--seed", "4", "--min-trades", "1"])
    cli = json.loads(capsys.readouterr().out)
    assert cli["status"] == "COMPLETED" and cli["best"]["params"] == out["best"]["params"]


def _seed_market():
    from backtest.walkforward import trading_sessions
    from db.schema import get_connection
    rows = []
    for k, sym in enumerate(WAVES):
        prev = None
        for t, d in enumerate(trading_sessions(START, END)):
            c = round(100 * (1 + 0.0008 * t) * (1 + 0.07 * math.sin(2 * math.pi * (t + 5 * k) / 17)), 2)
            o = prev or c
            rows.append((sym, str(d), o, round(max(o, c) * 1.004, 2), round(min(o, c) * 0.996, 2), c, 2_000_000))
            prev = c
    conn = get_connection()
    conn.executemany("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
