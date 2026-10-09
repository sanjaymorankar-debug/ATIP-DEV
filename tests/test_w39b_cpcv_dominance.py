"""
BT-14 research hardening, round 2 (backtest/cpcv.py):

  stochastic dominance  Bailey, Borwein, Lopez de Prado & Zhu (2017): does the out-of-sample
        performance of the in-sample choice (one value per CSCV combination) dominate that of a
        RANDOM choice (every trial in every combination)? First order: its CDF at or below the
        other's everywhere, below somewhere. Second order: the integral of the CDF difference
        <= 0 everywhere, < 0 somewhere. Worked by hand on small samples and a 4 x 2 matrix; a
        trial best in every block dominates, noise and a reversed ranking do not; the streamed
        check (C x N too large to hold) agrees with the exact one; CPCV reports it per split
  CPCV on the event-driven engine  run_cpcv(engine="event_driven") runs every group through
        backtest/event_driven.py with the request's event_driven settings; purge, embargo,
        paths and report unchanged; W2-only settings and bad event_driven settings refused

The engine tests reuse test_w39b_cpcv_pbo's synthetic market (three sine waves the dip
strategy trades often). Assertions are about the mechanics, never about a strategy being good.
"""

import json
import math

import numpy as np
import pytest

from tests.test_w39b_cpcv_pbo import CANDIDATES, END, START, WAVES, _dip, _equity, _series


@pytest.fixture
def market(temp_db, tmp_path, monkeypatch):
    """test_w39b_cpcv_pbo's synthetic market: three sine waves the dip strategy trades often."""
    import backtest.config as bc
    from backtest.walkforward import trading_sessions
    from db.schema import get_connection, init_db
    monkeypatch.setattr(bc, "CONFIG_PATH", tmp_path / "no_config.json")     # DEFAULTS, whatever config.json says
    init_db()
    days = trading_sessions(START, END)
    rows = []
    for k, sym in enumerate(WAVES):
        prev = None
        for t, d in enumerate(days):
            c = round(100 * (1 + 0.0008 * t) * (1 + 0.07 * math.sin(2 * math.pi * (t + 5 * k) / 17)), 2)
            o = prev or c
            rows.append((sym, str(d), o, round(max(o, c) * 1.004, 2), round(min(o, c) * 0.996, 2), c, 2_000_000))
            prev = c
    conn = get_connection()
    conn.executemany("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return days


DOM_KEYS = ("first_order", "second_order", "max_cdf_gap", "max_cdf_gap_at", "max_integral", "grid", "cdf_selected",
            "cdf_random", "integral")


def _dom(selected, reference):
    from backtest.cpcv import stochastic_dominance
    d = stochastic_dominance(selected, reference)
    return {k: d[k] for k in DOM_KEYS}


# ── stochastic dominance: pure ─────────────────────────────────────────────

def test_dominance_worked_by_hand():
    # first order: selected {2, 3} against the reference {0, 1, 2, 3}. On the observed values
    #   F_sel = 0, 0, 1/2, 1   F_ref = 1/4, 1/2, 3/4, 1   -> at or below everywhere, below at 0, 1, 2
    #   D(x) = E[(x - sel)+] - E[(x - ref)+]: 0 - 0, 0 - 1/4, 0 - 3/4 (2: (2+1+0)/4), 1/2 - 3/2 = -1
    assert _dom([2, 3], [1, 2, 3, 0]) == {
        "first_order": True, "second_order": True, "max_cdf_gap": 0.0, "max_cdf_gap_at": 3.0, "max_integral": 0.0,
        "grid": [0.0, 1.0, 2.0, 3.0], "cdf_selected": [0.0, 0.0, 0.5, 1.0], "cdf_random": [0.25, 0.5, 0.75, 1.0],
        "integral": [0.0, -0.25, -0.75, -1.0]}
    # second order only: a sure 2 against a coin flip of 1 or 3 (same mean). F_sel(2) = 1 > F_ref(2) = 1/2,
    # but the area under F_sel - F_ref, -1/2 on [1, 2) then +1/2 on [2, 3), never rises above 0
    assert _dom([2.0, 2.0], [1.0, 3.0]) == {
        "first_order": False, "second_order": True, "max_cdf_gap": 0.5, "max_cdf_gap_at": 2.0, "max_integral": 0.0,
        "grid": [1.0, 2.0, 3.0], "cdf_selected": [0.0, 1.0, 1.0], "cdf_random": [0.5, 0.5, 1.0],
        "integral": [0.0, -0.5, 0.0]}
    # the coin flip against the sure 2: neither (F_sel(1) = 1/2 > 0, D(2) = +1/2)
    d = _dom([1.0, 3.0], [2.0, 2.0])
    assert (d["first_order"], d["second_order"], d["max_cdf_gap"], d["max_integral"]) == (False, False, 0.5, 0.5)
    # the same distribution dominates in neither order (nothing is strictly better)
    d = _dom([1, 2, 3], [3, 2, 1, 1, 2, 3])
    assert (d["first_order"], d["second_order"], d["max_cdf_gap"], d["integral"]) == (False, False, 0.0,
                                                                                       [0.0, 0.0, 0.0])


def test_dominance_with_infinite_values():
    # None is -inf, as in the CSCV ranks (a zero-variance Sharpe): one -inf in the reference only puts
    # F_ref above F_sel below every finite value -- the integral from -inf is -inf: second order holds;
    # at 2, F_sel = 1 > F_ref = 3/4: no first order
    d = _dom([1.0, 2.0], [None, 1.0, 2.0, 3.0])
    assert (d["first_order"], d["second_order"], d["integral"], d["cdf_random"]) == (False, True, None,
                                                                                      [0.5, 0.75, 1.0])
    # -inf in the selection only: F_sel above F_ref down there -- neither
    d = _dom([None, 3.0], [1.0, 3.0])
    assert (d["first_order"], d["second_order"], d["max_cdf_gap_at"]) == (False, False, "-inf")
    # +inf (a positive mean with no variance) in the selection: never below any finite x
    d = _dom([math.inf, 2.0], [1.0, 2.0])
    assert (d["first_order"], d["second_order"], d["cdf_selected"]) == (True, True, [0.0, 0.5])
    from backtest.cpcv import stochastic_dominance
    assert stochastic_dominance([None], [None])["first_order"] is None             # nothing finite to compare
    with pytest.raises(ValueError, match="numbers"):
        stochastic_dominance([float("nan")], [1.0])


def test_pbo_reports_dominance_on_the_four_by_two_matrix_by_hand():
    from backtest.cpcv import pbo
    x = [[0.01, 0.02], [0.03, 0.00],          # block 0: means 0.02 / 0.01
         [0.02, 0.01], [0.00, 0.01]]          # block 1: means 0.01 / 0.01
    d = pbo(x, 2, "mean")["stochastic_dominance"]
    # {block 0} in-sample picks trial 0; out of sample (block 1) the trials score 0.01 / 0.01
    # {block 1} in-sample ties -> trial 0;    out of sample (block 0) they score 0.02 / 0.01
    # selected: {0.01, 0.02}; random: {0.01, 0.01, 0.02, 0.01}
    #   F_sel = 1/2, 1 and F_ref = 3/4, 1 at 0.01, 0.02: first order
    #   D(0.02) = (0.01 + 0) / 2 - (0.01 + 0.01 + 0 + 0.01) / 4 = 0.005 - 0.0075 = -0.0025
    assert (d["first_order"], d["second_order"], d["n_selected"], d["n_random"]) == (True, True, 2, 4)
    assert (d["grid"], d["cdf_selected"], d["cdf_random"], d["integral"]) == (
        [0.01, 0.02], [0.5, 1.0], [0.75, 1.0], [0.0, -0.0025])
    assert (d["mean_selected"], d["mean_random"], d["checked_at"]) == (0.015, 0.0125, "every observed value (exact)")
    assert d["reference"].startswith("random selection")


@pytest.mark.parametrize("metric", ["sharpe", "mean", "total_return"])
def test_a_trial_best_in_every_block_dominates_random_selection(metric):
    from backtest.cpcv import pbo
    # shared shocks, distinct drifts: the in-sample best (trial 9) is the out-of-sample best of every
    # combination, so its OOS value is each combination's maximum -- F_sel <= F_ref everywhere
    shocks = np.random.default_rng(1).normal(0, 0.01, (400, 1))
    d = pbo(shocks + np.linspace(-0.001, 0.002, 10), 10, metric)["stochastic_dominance"]
    assert (d["first_order"], d["second_order"], d["max_cdf_gap"], d["max_integral"]) == (True, True, 0.0, 0.0)
    assert d["mean_selected"] > d["mean_random"] and (d["n_selected"], d["n_random"]) == (252, 2520)
    assert len(d["grid"]) == len(d["cdf_selected"]) == len(d["integral"]) == 41                # evenly spaced
    assert all(a <= b for a, b in zip(d["cdf_selected"], d["cdf_random"]))
    assert d["cdf_selected"] == sorted(d["cdf_selected"]) and d["cdf_random"][-1] == d["cdf_selected"][-1] == 1.0


def test_noise_or_a_reversed_ranking_does_not_dominate():
    from backtest.cpcv import pbo
    # pure noise: the in-sample best is a random draw out of sample -- first order essentially never
    # holds; second order only by chance
    doms = [pbo(np.random.default_rng(seed).normal(0, 0.01, (480, 20)), 8)["stochastic_dominance"]
            for seed in range(20)]
    assert sum(d["first_order"] for d in doms) <= 1 and sum(d["second_order"] for d in doms) <= 8
    assert all(d["max_cdf_gap"] > 0 for d in doms if not d["first_order"])
    # either half's winner is the other half's loser: dominated, not dominating
    shocks = np.random.default_rng(2).normal(0, 0.01, (40, 1))
    drift = np.vstack([np.tile(0.001 * np.arange(5), (20, 1)), np.tile(0.001 * np.arange(5)[::-1], (20, 1))])
    d = pbo(shocks + drift, 2, "mean")["stochastic_dominance"]
    assert (d["first_order"], d["second_order"]) == (False, False) and d["max_integral"] > 0


def test_the_streamed_check_agrees_with_the_exact_one(monkeypatch):
    """Above DOMINANCE_EXACT_MAX reference values the pool is not held: it is streamed again and
    checked on the selected values plus an even grid. The 41 reported points are the same grid in
    both modes, where both are exact."""
    from backtest import cpcv as CV
    for seed, drift in ((7, 0.0005), (8, 0.0)):
        m = np.random.default_rng(seed).normal(0.0002, 0.01, (705, 30)) + np.linspace(0, drift, 30)
        exact = CV.pbo(m, 12)["stochastic_dominance"]
        monkeypatch.setattr(CV, "DOMINANCE_EXACT_MAX", 0)
        streamed = CV.pbo(m, 12)["stochastic_dominance"]
        monkeypatch.undo()
        assert exact["checked_at"] == "every observed value (exact)"
        assert streamed["checked_at"] == f"every selected value and {CV.DOMINANCE_GRID} evenly spaced points"
        for k in ("first_order", "second_order", "grid", "cdf_selected", "cdf_random", "n_random", "mean_random"):
            assert exact[k] == streamed[k], k
        assert streamed["integral"] == pytest.approx(exact["integral"], abs=1e-9)


# ── CPCV: the same test on the splits ──────────────────────────────────────

def test_cpcv_reports_dominance_per_split():
    from backtest.cpcv import cpcv_paths
    good, bad = _series([0.02, 0.01, 0.03, 0.02, 0.01]), _series([0.01, 0.00, 0.01, 0.01, 0.00])
    d = cpcv_paths([good, bad], 5, 2, select_by="total_return")["pbo"]["stochastic_dominance"]
    assert (d["first_order"], d["second_order"], d["n_selected"], d["n_random"]) == (True, True, 10, 20)
    assert "total_return on each split's test groups" in d["reference"]
    # test_w39b_cpcv_pbo's worked example: the training winner loses every split out of sample
    a = _series([0.03, 0.01, -0.02, 0.00])
    b = _series([0.00, -0.01, 0.02, 0.01])
    d = cpcv_paths([a, b], 4, 2, select_by="total_return")["pbo"]["stochastic_dominance"]
    assert (d["first_order"], d["second_order"]) == (False, False) and d["mean_selected"] < d["mean_random"]
    assert cpcv_paths([a], 4, 2)["pbo"] is None                       # one candidate: nothing selected


# ── CPCV through the event-driven engine ───────────────────────────────────

ED = {"impact": "none", "slices": 2, "participation_cap": 0.05}


def test_run_cpcv_on_the_event_driven_engine(market):
    from backtest import event_driven as EDE
    from backtest import store
    from backtest.cpcv import run_cpcv
    from db.schema import get_connection
    out = run_cpcv(_dip(event_driven=ED), 4, 2, CANDIDATES[:2], "sharpe", purge_sessions=2, embargo_sessions=1,
                   engine="event_driven")
    assert out["status"] == "COMPLETED" and out["engine"] == "event_driven"
    assert out["event_driven"] == {**EDE.ED_DEFAULTS, **ED}
    assert (out["n_splits"], out["n_paths"], len(out["runs"])) == (6, 3, 8)
    assert all(r["status"] == "COMPLETED" for r in out["runs"])
    conn = get_connection()
    try:
        parent = store.get_run(conn, out["run_id"])
        kids = store.child_runs(conn, out["run_id"])
        kid0 = store.get_run(conn, kids[0]["run_id"])
    finally:
        conn.close()
    assert (parent["kind"], parent["status"], parent["config"]["cpcv"]["engine"]) == ("cpcv", "COMPLETED",
                                                                                       "event_driven")
    assert "event_driven (BT-17)" in parent["bias_report"]["engine"] and "impact none" in parent["bias_report"]["engine"]
    assert [k["kind"] for k in kids] == ["cpcv_trial"] * 8 and [k["window_index"] for k in kids] == list(range(8))
    # each group run is an event-driven run: its stored snapshot carries the settings, its bias report the engine
    assert kid0["config"]["event_driven"] == out["event_driven"] and kid0["bias_report"]["engine"] == \
        "event_driven (BT-17)"
    # ... and is exactly what backtest/event_driven.py gives for that group's window
    g0 = out["groups"][0]
    conn = get_connection()
    try:
        direct = EDE.run(_dip(start=g0["start"], end=g0["end"], params=CANDIDATES[0], event_driven=ED), conn)
    finally:
        conn.close()
    stored = _equity(kids[0]["run_id"])
    assert [(str(r["date"]), r["equity"], r["daily_return"]) for r in stored] == \
        [(str(p["date"]), p["equity"], p["daily_return"]) for p in direct["equity"]]
    # the chained daily returns reach the run's final equity (the close-out is in the last one)
    growth = 1.0
    for r in stored:
        growth *= 1 + r["daily_return"]
    assert growth * direct["snapshot"]["initial_capital"] == pytest.approx(stored[-1]["equity"], abs=0.05)
    # everything after the runs is the W2 path: purge / embargo per split, paths chained from the runs
    sizes = [g["sessions"] for g in out["groups"]]
    s12 = out["splits"][3]
    assert (s12["test_groups"], s12["purged_sessions"], s12["embargoed_sessions"], s12["train_sessions"]) == (
        [1, 2], 2, 1, sizes[0] - 2 + sizes[3] - 1)
    by_index = {k["window_index"]: k for k in kids}
    p0 = out["paths"][0]
    growth = 1.0
    for g, m in enumerate(p0["candidates"]):
        for row in _equity(by_index[m * 4 + g]["run_id"]):
            growth *= 1 + (row["daily_return"] or 0)
    assert p0["total_return"] == pytest.approx(growth - 1, abs=1e-6) and p0["sessions"] == len(market)
    assert out["pbo"]["n_splits"] == 6 and out["pbo"]["stochastic_dominance"]["n_selected"] == 6


def test_cpcv_event_driven_differs_from_w2_only_through_the_runs(market):
    """Same study on both engines: the same groups and splits; different fills, so different returns."""
    from backtest.cpcv import run_cpcv
    w2 = run_cpcv(_dip(), 3, 1, CANDIDATES[:2])
    ed = run_cpcv(_dip(event_driven={"impact": "none"}), 3, 1, CANDIDATES[:2], engine="event_driven")
    assert (w2["engine"], ed["engine"]) == ("w2", "event_driven") and "event_driven" not in w2
    assert w2["groups"] == ed["groups"]
    assert [s["test_groups"] for s in w2["splits"]] == [s["test_groups"] for s in ed["splits"]]
    assert [p["splits"] for p in w2["paths"]] == [p["splits"] for p in ed["paths"]]
    assert [p["total_return"] for p in w2["paths"]] != [p["total_return"] for p in ed["paths"]]


def test_a_failed_event_driven_group_run_is_stored_failed_and_its_candidate_left_out(market, monkeypatch):
    from backtest import event_driven as EDE
    from backtest import store
    from backtest.cpcv import run_cpcv
    from db.schema import get_connection
    real = EDE.run

    def flaky(request, conn):
        if request["params"] == CANDIDATES[1] and request["start"] > START:
            raise RuntimeError("no bars today")
        return real(request, conn)
    monkeypatch.setattr(EDE, "run", flaky)
    out = run_cpcv(_dip(event_driven=ED), 3, 1, CANDIDATES[:2], engine="event_driven")
    assert out["status"] == "COMPLETED" and out["candidates_used"] == [0] and out["pbo"] is None
    failed = [r for r in out["runs"] if r["status"] == "FAILED"]
    assert [(r["candidate"], r["group"], r["error"]) for r in failed] == [(1, 1, "no bars today"),
                                                                         (1, 2, "no bars today")]
    conn = get_connection()
    try:
        rows = [store.get_run(conn, r["run_id"]) for r in failed]
    finally:
        conn.close()
    assert all((r["kind"], r["status"], r["error"]) == ("cpcv_trial", "FAILED", "RuntimeError: no bars today")
               for r in rows)
    assert any("left out" in n for n in out["notes"])


def test_cpcv_refuses_what_the_engine_cannot_honour(market, monkeypatch):
    from backtest.cpcv import prepare_cpcv, run_cpcv
    for req, engine, msg in (
            (_dip(), "zipline", "engine must be one of"),
            (_dip(event_driven={"slices": 2}), "w2", "only with engine 'event_driven'"),
            (_dip(slippage={"kind": "bps", "value": 5}), "event_driven", "slippage cannot be honoured"),
            (_dip(liquidity={"max_participation_pct": 1}), "event_driven", "liquidity cannot be honoured"),
            (_dip(event_driven={"slice": 2}), "event_driven", "unknown event_driven settings ['slice']"),
            (_dip(event_driven={"timeframe": "1h"}), "event_driven", "timeframe must be"),
            (_dip(event_driven={"impact": "linear"}), "event_driven", "impact must be sqrt or none"),
            (_dip(event_driven={"participation_cap": 0}), "event_driven", "participation_cap must be > 0"),
            (_dip(event_driven={"slices": "two"}), "event_driven", "must be integers"),
            (_dip(event_driven=[1]), "event_driven", "must be an object"),
            (_dip(event_driven={"timeframe": "15m"}), "event_driven", "DP-03 retention")):
        with pytest.raises(ValueError, match=msg.replace("[", r"\[").replace("]", r"\]").replace("(", r"\(")):
            prepare_cpcv(req, 4, 2, engine=engine)
    with pytest.raises(ValueError, match="test window"):
        run_cpcv({**_dip(event_driven=ED), "period_label": "test", "allow_test": True}, 4, 2, engine="event_driven")
    # intraday bars on every session of every group: accepted
    from backtest.cpcv import _intraday_sessions
    monkeypatch.setattr("backtest.cpcv._intraday_sessions", lambda tf, groups: [len(g) for g in groups])
    groups, _ = prepare_cpcv(_dip(event_driven={"timeframe": "15m"}), 4, 2, engine="event_driven")
    assert len(groups) == 4 and _intraday_sessions("15m", groups) == [0, 0, 0, 0]


def test_cli_and_route_take_the_engine(market, tmp_path, monkeypatch, capsys):
    from backtest.__main__ import main
    base = ["--strategy", "dip", "--universe", ",".join(WAVES), "--start", START, "--end", END]
    main(["cpcv", *base, "--groups", "3", "--test-groups", "1", "--candidates", json.dumps(CANDIDATES[:2]),
          "--engine", "event_driven", "--event-driven", json.dumps({"impact": "none"})])
    out = json.loads(capsys.readouterr().out)
    assert (out["status"], out["engine"], out["event_driven"]["impact"], out["n_paths"]) == (
        "COMPLETED", "event_driven", "none", 1)
    assert "stochastic_dominance" in out["pbo"]
    with pytest.raises(ValueError, match="only with engine 'event_driven'"):
        main(["cpcv", *base, "--groups", "3", "--test-groups", "1", "--event-driven", "{}"])

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    c = TestClient(server.app)
    h = {security.TOKEN_HEADER: security.token()}
    r = c.post("/api/backtests/cpcv", headers=h, json={"request": _dip(slippage={"kind": "none", "value": 0}),
                                                        "engine": "event_driven"})
    assert r.status_code == 400 and "cannot be honoured by the event-driven engine" in r.json()["error"]
    r = c.post("/api/backtests/cpcv", headers=h, json={"request": _dip(), "engine": "nope"})
    assert r.status_code == 400 and "engine must be one of" in r.json()["error"]
