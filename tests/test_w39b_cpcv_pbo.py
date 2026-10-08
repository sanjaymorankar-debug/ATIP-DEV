"""
W39b research hardening (docs/ATIP_GAP_ANALYSIS_2026-10.md §4 item 9): combinatorially
purged cross-validation (CPCV, Lopez de Prado 2018 ch. 12) and the probability of backtest
overfitting (PBO by CSCV, Bailey, Borwein, Lopez de Prado & Zhu 2015) -- backtest/cpcv.py.

  PBO   pure numpy on hand-built return matrices: one trial best in every block -> 0, pure
        noise -> about 0.5, a ranking that reverses out of sample -> 1, a 4 x 2 matrix worked
        by hand, the block-sum implementation against a direct one, and the bounds
  CPCV  the combinatorics (C(N, k) splits; k/N x C(N, k) paths, each covering every group
        once), purge / embargo of training groups, selection worked by hand on constructed
        returns; then through the engine on test_w39_backtest's synthetic market (three sine
        waves the dip strategy trades often): M x N group runs, the stored parent and its
        path distribution, PBO of an optimisation's trials, the CLI and the routes
Assertions are about the mechanics, never about a strategy being good.
"""

import itertools
import json
import math
from datetime import date, timedelta

import numpy as np
import pytest

START, END = "2025-01-01", "2025-09-30"
WAVES = ("AAA", "BBB", "CCC")


# ── PBO by CSCV: pure ──────────────────────────────────────────────────────

def _direct(x, s, metric="sharpe"):
    """The paper's procedure, literally: concatenate the blocks of each combination and score
    each column with numpy -- the reference for the block-sum implementation."""
    t, n = x.shape
    rows = t // s
    x = x[t - rows * s:]
    blocks = [x[i * rows:(i + 1) * rows] for i in range(s)]
    lam, xs, ys = [], [], []
    for comb in itertools.combinations(range(s), s // 2):
        a = np.vstack([blocks[i] for i in comb])
        b = np.vstack([blocks[i] for i in range(s) if i not in comb])
        if metric == "sharpe":
            ra, rb = (m.mean(0) / m.std(0, ddof=1) * math.sqrt(252) for m in (a, b))
        else:
            ra, rb = a.mean(0), b.mean(0)
        k = int(np.argmax(ra))
        rank = (rb < rb[k]).sum() + ((rb == rb[k]).sum() + 1) / 2
        lam.append(math.log(rank / (n + 1) / (1 - rank / (n + 1))))
        xs.append(ra[k])
        ys.append(rb[k])
    return np.array(lam), np.array(xs), np.array(ys)


def test_pbo_on_a_four_by_two_matrix_worked_by_hand():
    from backtest.cpcv import pbo
    x = [[0.01, 0.02], [0.03, 0.00],          # block 0: means 0.02 / 0.01
         [0.02, 0.01], [0.00, 0.01]]          # block 1: means 0.01 / 0.01
    r = pbo(x, 2, "mean")
    # {block 0} in-sample: trial 0 best; out of sample (block 1) it ties trial 1: rank 1.5 of 2,
    #   w = 1.5 / 3 = 0.5, logit 0 -- counts as overfit (<= 0)
    # {block 1} in-sample: a tie, so trial 0 (the lowest index); out of sample (block 0) it is best:
    #   rank 2, w = 2 / 3, logit ln 2
    assert r["logit_values"] == [0.0, round(math.log(2), 6)]
    assert (r["pbo"], r["prob_loss"], r["n_combinations"], r["rows_per_partition"]) == (0.5, 0.0, 2, 2)
    # R'[n*] against R[n*]: (0.02, 0.01) and (0.01, 0.02) -> slope -1 through 0.03
    assert r["degradation"] == {"slope": -1.0, "intercept": 0.03, "r2": 1.0, "n": 2}
    assert r["selected_trials"] == [{"trial": 0, "share": 1.0}]
    assert [b["count"] for b in r["logit_histogram"]] == [0, 0, 1, 1, 0, 0]


@pytest.mark.parametrize("metric", ["sharpe", "mean", "total_return"])
def test_pbo_is_zero_when_one_trial_is_best_in_every_block(metric):
    from backtest.cpcv import pbo
    # every trial sees the same shocks, with its own drift: in ANY subset of sessions the ranking is
    # the ranking of the drifts, so the in-sample best (trial 9) is also best out of sample
    shocks = np.random.default_rng(1).normal(0, 0.01, (400, 1))
    r = pbo(shocks + np.linspace(-0.001, 0.002, 10), 10, metric)
    assert r["pbo"] == 0.0 and r["selected_trials"] == [{"trial": 9, "share": 1.0}]
    assert r["logits"]["min"] == r["logits"]["max"] == round(math.log(10), 6)        # rank 10 of 10
    assert r["logit_histogram"][-1] == {"bin": "> 2", "count": 252} and r["n_combinations"] == math.comb(10, 5)


def test_pbo_of_pure_noise_is_about_one_half():
    from backtest.cpcv import pbo
    # no trial has an edge: the in-sample best is a random draw out of sample. One matrix's PBO
    # is noisy (its 70 combinations share the data), so average over 20 independent matrices
    runs = [pbo(np.random.default_rng(seed).normal(0, 0.01, (480, 20)), 8) for seed in range(20)]
    assert abs(np.mean([r["pbo"] for r in runs]) - 0.5) < 0.1
    assert abs(np.mean([r["logits"]["mean"] for r in runs])) < 0.5
    assert abs(np.mean([r["oos_of_is_best"]["mean"] for r in runs])) < 0.25          # OOS Sharpe near 0


def test_pbo_is_one_when_the_ranking_reverses_out_of_sample():
    from backtest.cpcv import pbo
    shocks = np.random.default_rng(2).normal(0, 0.01, (40, 1))
    drift = np.vstack([np.tile(0.001 * np.arange(5), (20, 1)),             # block 0: trial 4 best
                       np.tile(0.001 * np.arange(5)[::-1], (20, 1))])      # block 1: trial 4 worst
    r = pbo(shocks + drift, 2, "mean")
    # either half's winner is the other half's loser: rank 1 of 5, w = 1/6, logit ln(1/5)
    assert r["pbo"] == 1.0 and r["logit_values"] == [round(math.log(1 / 5), 6)] * 2
    assert r["degradation"]["slope"] < 0


@pytest.mark.parametrize("s,metric", [(6, "sharpe"), (6, "mean"), (14, "sharpe")])
def test_pbo_block_sums_match_the_direct_procedure(s, metric):
    from backtest.cpcv import _CHUNK, pbo
    x = np.random.default_rng(7).normal(0.0002, 0.01, (705, 6))
    r = pbo(x, s, metric)
    lam, xs, ys = _direct(x, s, metric)
    assert r["n_combinations"] == len(lam) == math.comb(s, s // 2)
    if s == 14:
        assert len(lam) > _CHUNK                                           # crosses a numpy chunk
    assert r["pbo"] == round(float((lam <= 0).mean()), 6) and r["prob_loss"] == round(float((ys < 0).mean()), 6)
    assert r["logits"]["mean"] == pytest.approx(lam.mean(), abs=2e-6)
    assert r["is_best"]["mean"] == pytest.approx(xs.mean(), abs=2e-5)
    assert r["degradation"]["slope"] == pytest.approx(np.polyfit(xs, ys, 1)[0], abs=1e-5)
    if "logit_values" in r:
        assert r["logit_values"] == [round(v, 6) for v in lam]
    assert (r["sessions"], r["sessions_used"], r["sessions_dropped"]) == (705, 705 - 705 % s, 705 % s)


def test_pbo_drops_the_oldest_rows_that_do_not_fill_a_block():
    from backtest.cpcv import pbo
    x = np.random.default_rng(3).normal(0, 0.01, (10, 3))
    r = pbo(x, 4)
    assert (r["rows_per_partition"], r["sessions_used"], r["sessions_dropped"]) == (2, 8, 2)
    y = x.copy()
    y[:2] = 0.5                                                            # the two dropped rows
    assert pbo(y, 4) == r


def test_pbo_bounds_are_errors():
    from backtest.cpcv import pbo
    ok = np.random.default_rng(4).normal(0, 0.01, (64, 4))
    for args, msg in (((ok, 3), "even"), ((ok, 0), "even"), ((ok, 18), "even"), ((ok[:7], 4), "partitions"),
                      ((ok[:, :1], 4), "trials"), ((np.zeros((64, 1001)), 4), "trials"),
                      ((ok[:, 0], 4), "2-D"), (([["a", "b"]] * 8, 2), "numbers"),
                      ((np.vstack([ok[:-1], [np.nan] * 4]), 4), "non-finite"),
                      ((np.vstack([ok[:-1], [np.inf] * 4]), 4), "non-finite"),
                      ((np.zeros((50_001, 2)), 4), "50000")):
        with pytest.raises(ValueError, match=msg):
            pbo(*args)
    with pytest.raises(ValueError, match="-100%"):
        pbo(np.vstack([ok[:-1], [-1.0] * 4]), 4, "total_return")
    with pytest.raises(ValueError, match="metric"):
        pbo(ok, 4, "calmar")
    with pytest.raises(ValueError, match="labels"):
        pbo(ok, 4, labels=["a"])


def test_load_matrix_reads_csv_with_a_header_and_json(tmp_path):
    from backtest.cpcv import load_matrix
    (tmp_path / "m.csv").write_text("fast,slow\n0.01,0.02\n-0.01,0.00\n", encoding="utf-8")
    (tmp_path / "m.json").write_text(json.dumps({"matrix": [[0.1, 0.2]], "labels": ["a", "b"]}), encoding="utf-8")
    (tmp_path / "l.json").write_text("[[0.1, 0.2], [0.3, 0.4]]", encoding="utf-8")
    assert load_matrix(tmp_path / "m.csv") == ([[0.01, 0.02], [-0.01, 0.0]], ["fast", "slow"])
    assert load_matrix(tmp_path / "m.json") == ([[0.1, 0.2]], ["a", "b"])
    assert load_matrix(tmp_path / "l.json") == ([[0.1, 0.2], [0.3, 0.4]], None)


# ── CPCV: pure ─────────────────────────────────────────────────────────────

def test_split_groups_are_contiguous_and_nearly_equal():
    from backtest.cpcv import split_groups
    days = list(range(23))
    groups = split_groups(days, 5)
    assert [len(g) for g in groups] == [5, 5, 5, 4, 4]
    assert sum(groups, []) == days


@pytest.mark.parametrize("n,k", [(6, 2), (5, 1), (4, 2), (8, 3), (12, 6)])
def test_splits_and_paths_cover_every_group_once(n, k):
    from backtest.cpcv import assemble_paths, cpcv_splits, n_paths
    splits = cpcv_splits(n, k)
    paths = assemble_paths(n, splits)
    assert len(splits) == math.comb(n, k) and len(set(splits)) == len(splits)
    assert len(paths) == n_paths(n, k) == k * math.comb(n, k) // n                  # phi = k/N x C(N, k)
    used = [(s, g) for p in paths for g, s in enumerate(p)]
    assert all(g in splits[s] for s, g in used)                                    # a path's group g is tested there
    assert sorted(used) == sorted((s, g) for s, t in enumerate(splits) for g in t)  # every test result used once
    if (n, k) == (6, 2):
        assert (len(splits), len(paths)) == (15, 5)                                # AFML's worked example


def test_training_slices_purge_before_and_embargo_after_each_test_group():
    from backtest.cpcv import training_slices
    assert training_slices([10] * 5, (1, 3), purge=2, embargo=1) == [(0, 8), None, (1, 8), None, (1, 10)]
    assert training_slices([10] * 4, (0,), purge=2, embargo=1) == [None, (1, 10), (0, 10), (0, 10)]
    assert training_slices([10] * 4, (3,), purge=2, embargo=1) == [(0, 10), (0, 10), (0, 8), None]
    assert training_slices([10] * 3, (1,)) == [(0, 10), None, (0, 10)]


def _series(per_group, size=2, start=date(2025, 1, 1)):
    """[[(date, r), ...] per group] from one return per group, repeated `size` sessions."""
    out, d = [], start
    for r in per_group:
        g = []
        for _ in range(size):
            g.append((d, r))
            d += timedelta(days=1)
        out.append(g)
    return out


def test_cpcv_selection_and_paths_worked_by_hand():
    from backtest.cpcv import cpcv_paths
    a = _series([0.03, 0.01, -0.02, 0.00])
    b = _series([0.00, -0.01, 0.02, 0.01])
    out = cpcv_paths([a, b], 4, 2, select_by="total_return")
    # per group (two sessions each) A grows 1.0609, 1.0201, 0.9604, 1.0; B 1.0, 0.9801, 1.0404, 1.0201.
    # Training on the two groups NOT tested:
    #   test (0,1) train {2,3}: A 0.9604  < B 1.0613 -> B      test (1,2) train {0,3}: A 1.0609 > B 1.0201 -> A
    #   test (0,2) train {1,3}: A 1.0201  > B 0.9998 -> A      test (1,3) train {0,2}: A 1.0189 < B 1.0404 -> B
    #   test (0,3) train {1,2}: A 0.9797  < B 1.0197 -> B      test (2,3) train {0,1}: A 1.0822 > B 0.9801 -> A
    assert [s["test_groups"] for s in out["splits"]] == [[0, 1], [0, 2], [0, 3], [1, 2], [1, 3], [2, 3]]
    assert [s["chosen"] for s in out["splits"]] == [1, 0, 1, 0, 1, 0]
    # phi = 3 paths; path j takes each group from the j-th split that tests it
    assert [p["splits"] for p in out["paths"]] == [[0, 0, 1, 2], [1, 3, 3, 4], [2, 4, 5, 5]]
    assert [p["candidates"] for p in out["paths"]] == [[1, 1, 0, 1], [0, 0, 0, 1], [1, 1, 0, 0]]
    want = [1.0 * 0.9801 * 0.9604 * 1.0201, 1.0609 * 1.0201 * 0.9604 * 1.0201, 1.0 * 0.9801 * 0.9604 * 1.0]
    assert [p["total_return"] for p in out["paths"]] == pytest.approx([w - 1 for w in want], abs=1e-6)
    assert all(p["sessions"] == 8 for p in out["paths"])
    assert out["distribution"]["total_return"]["n"] == 3
    assert out["distribution"]["total_return"]["median"] == pytest.approx(want[0] - 1, abs=1e-6)
    assert [f["chosen"] for f in out["selection_frequency"]] == [3, 3]
    # out of sample the training winner is the loser in every split: rank 1 of 2, logit ln(1/2)
    assert [s["logit"] for s in out["splits"]] == [round(math.log(0.5), 6)] * 6
    assert out["pbo"]["pbo"] == 1.0
    assert out["pbo"]["prob_loss"] == round(4 / 6, 6)          # B on {0,1}, A on {1,2}, B on {1,3}, A on {2,3}


def test_cpcv_with_a_dominant_candidate_has_one_path_shape_and_pbo_zero():
    from backtest.cpcv import cpcv_paths
    good, bad = _series([0.02, 0.01, 0.03, 0.02, 0.01]), _series([0.01, 0.00, 0.01, 0.01, 0.00])
    out = cpcv_paths([good, bad], 5, 2, select_by="total_return")      # better in every group
    assert all(s["chosen"] == 0 for s in out["splits"]) and len(out["paths"]) == 4
    assert len({p["total_return"] for p in out["paths"]}) == 1 and out["pbo"]["pbo"] == 0.0
    assert out["distribution"]["sharpe"]["n"] == 4 and out["selection_frequency"][0]["share"] == 1.0


def test_cpcv_purge_and_embargo_remove_the_boundary_sessions_that_would_decide_the_selection():
    from backtest.cpcv import cpcv_paths
    steady = _series([0.002, 0.002, 0.002], size=5)
    # a jump on the last session of group 0 (just before test group 1) and on the first of group 2 (just
    # after it): the sessions a leak would sit on
    leak = _series([0.0, 0.0, 0.0], size=5)
    leak[0][-1] = (leak[0][-1][0], 0.10)
    leak[2][0] = (leak[2][0][0], 0.10)

    def chosen(**kw):
        out = cpcv_paths([leak, steady], 3, 1, select_by="total_return", **kw)
        return out["splits"][1]                                    # the split testing group 1

    assert chosen()["chosen"] == 0                                 # the jumps win the training score
    assert chosen(purge=1)["chosen"] == 0                          # the jump after the test group remains
    s = chosen(purge=1, embargo=1)
    assert s["chosen"] == 1 and (s["purged_sessions"], s["embargoed_sessions"], s["train_sessions"]) == (1, 1, 8)


def test_cpcv_paths_refuse_inconsistent_series():
    from backtest.cpcv import cpcv_paths
    a = _series([0.01, 0.02, 0.03])
    with pytest.raises(ValueError, match="groups"):
        cpcv_paths([a[:2]], 3, 1)
    with pytest.raises(ValueError, match="different sessions"):
        cpcv_paths([a, _series([0.01, 0.02, 0.03], start=date(2024, 1, 1))], 3, 1)
    with pytest.raises(ValueError, match="training sessions"):
        cpcv_paths([a], 3, 1, purge=1)
    with pytest.raises(ValueError, match="select_by"):
        cpcv_paths([a], 3, 1, select_by="calmar")


# ── through the engine ─────────────────────────────────────────────────────

@pytest.fixture
def market(temp_db, tmp_path, monkeypatch):
    """test_w39_backtest's synthetic market: three sine waves the dip strategy trades often."""
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


def _dip(**kw):
    return {"strategy_id": "dip", "universe": list(WAVES), "start": START, "end": END, **kw}


CANDIDATES = [{"target_pct": 3}, {"target_pct": 6}, {"target_pct": 12}]


def _equity(run_id):
    from backtest import store
    from db.schema import get_connection
    conn = get_connection()
    try:
        return store.get_rows(conn, "backtest_equity", run_id)
    finally:
        conn.close()


def test_run_cpcv_through_the_engine(market):
    from backtest import store
    from backtest.cpcv import run_cpcv
    from db.schema import get_connection
    out = run_cpcv(_dip(), 4, 2, CANDIDATES, "sharpe", purge_sessions=2, embargo_sessions=1)
    assert out["status"] == "COMPLETED"
    assert (out["n_splits"], out["n_paths"], len(out["runs"])) == (6, 3, 12)
    assert all(r["status"] == "COMPLETED" for r in out["runs"])
    sizes = [g["sessions"] for g in out["groups"]]
    assert sum(sizes) == len(market) and sizes == [47, 47, 47, 46]
    assert [g["start"] for g in out["groups"]] == [str(market[i]) for i in (0, 47, 94, 141)]
    conn = get_connection()
    try:
        parent = store.get_run(conn, out["run_id"])
        kids = store.child_runs(conn, out["run_id"])
    finally:
        conn.close()
    assert (parent["kind"], parent["status"]) == ("cpcv", "COMPLETED")
    assert parent["summary"]["n_paths"] == 3 and parent["config"]["cpcv"]["candidates"] == CANDIDATES
    assert [k["kind"] for k in kids] == ["cpcv_trial"] * 12 and [k["window_index"] for k in kids] == list(range(12))
    by_index = {k["window_index"]: k for k in kids}
    for m in range(3):                                  # candidate m on group g is window m x 4 + g, over that group
        for g, grp in enumerate(out["groups"]):
            k = by_index[m * 4 + g]
            assert (str(k["start_date"]), str(k["end_date"]), k["params"]["target_pct"]) == (
                grp["start"], grp["end"], CANDIDATES[m]["target_pct"])
    # purge / embargo per split: (0,1) tests the start, so only group 2 (after it) is embargoed;
    # (1,2): group 0 is purged before it and group 3 embargoed after it
    s01, s12 = out["splits"][0], out["splits"][3]
    assert (s01["test_groups"], s01["purged_sessions"], s01["embargoed_sessions"]) == ([0, 1], 0, 1)
    assert s01["train_sessions"] == sizes[2] - 1 + sizes[3]
    assert (s12["test_groups"], s12["purged_sessions"], s12["embargoed_sessions"]) == ([1, 2], 2, 1)
    assert s12["train_sessions"] == sizes[0] - 2 + sizes[3] - 1
    # a path is the chosen candidates' group runs chained: rebuild path 0 from the stored equity
    p0 = out["paths"][0]
    growth = 1.0
    for g, m in enumerate(p0["candidates"]):
        assert m == out["splits"][p0["splits"][g]]["chosen"]
        for row in _equity(by_index[m * 4 + g]["run_id"]):
            growth *= 1 + (row["daily_return"] or 0)
    assert p0["total_return"] == pytest.approx(growth - 1, abs=1e-6) and p0["sessions"] == len(market)
    d = out["distribution"]
    assert d["sharpe"]["n"] == d["total_return"]["n"] == 3
    assert d["total_return"]["min"] <= d["total_return"]["median"] <= d["total_return"]["max"]
    assert sum(f["share"] for f in out["selection_frequency"]) == pytest.approx(1.0)
    assert out["pbo"]["n_splits"] == 6 and 0 <= out["pbo"]["pbo"] <= 1
    assert parent["metrics"]["paths"] == 3 and parent["metrics"]["sharpe"] == d["sharpe"]["median"]
    assert out["recommended_purge_sessions"] == 10 and any("maximum holding period" in n for n in out["notes"])


def test_cpcv_with_one_candidate_gives_identical_paths(market):
    from backtest.cpcv import run_cpcv
    out = run_cpcv(_dip(), 4, 2)
    assert len(out["runs"]) == 4 and len(out["paths"]) == 3 and out["pbo"] is None
    assert len({p["total_return"] for p in out["paths"]}) == 1
    assert out["distribution"]["sharpe"]["std"] == 0.0 and any("one candidate" in n for n in out["notes"])


def test_cpcv_refuses_what_it_cannot_bound_or_would_leak(market):
    from backtest.cpcv import prepare_cpcv, run_cpcv
    periods = {"research": [START, "2025-06-30"], "test": ["2025-07-01", END]}
    for kwargs, msg in (({"n_groups": 13}, "n_groups"), ({"n_groups": 1}, "n_groups"), ({"k_test": 0}, "k_test"),
                        ({"n_groups": 4, "k_test": 4}, "k_test"), ({"candidates": [{}] * 21}, "candidates"),
                        ({"candidates": ["stop_pct"]}, "parameter objects"), ({"select_by": "calmar"}, "select_by"),
                        ({"purge_sessions": -1}, ">= 0"), ({"n_groups": 4, "purge_sessions": 40, "embargo_sessions": 6},
                                                           "training sessions"),
                        ({"candidates": [{"no_such_param": 1}]}, "unknown parameters")):
        with pytest.raises(ValueError, match=msg):
            prepare_cpcv(_dip(), **{"n_groups": 6, "k_test": 2, **kwargs})
    with pytest.raises(ValueError, match="per group"):
        prepare_cpcv(_dip(end="2025-02-14"), 12, 2)              # 33 sessions: 2 or 3 per group
    with pytest.raises(ValueError, match="not periods"):
        prepare_cpcv({"strategy_id": "dip", "universe": list(WAVES), "periods": periods,
                      "period_label": "research"})
    with pytest.raises(ValueError, match="test window"):
        run_cpcv({"strategy_id": "dip", "universe": list(WAVES), "periods": periods, "period_label": "test",
                  "allow_test": True})


def test_pbo_of_an_optimisation_is_stored_under_it(market):
    from backtest import store
    from backtest.cpcv import pbo, pbo_for_run
    from backtest.optimize import optimize
    from db.schema import get_connection
    opt = optimize(_dip(), {"target_pct": {"values": [3, 6, 12]}, "stop_pct": {"values": [2, 4]}}, "grid",
                   min_trades=1)
    r = pbo_for_run(opt["run_id"], 8)
    assert (r["status"], r["source_run_id"], r["n_trials"], r["n_partitions"]) == ("COMPLETED", opt["run_id"], 6, 8)
    assert r["sessions"] == len(market) - 1 and r["n_combinations"] == 70
    assert [t["run_id"] for t in r["trials"]] == [t["run_id"] for t in sorted(opt["trials"], key=lambda t: t["index"])]
    # the same matrix by hand: each trial's daily returns, first session left out
    mat = np.array([[row["daily_return"] or 0.0 for row in _equity(t["run_id"])][1:] for t in r["trials"]]).T
    assert pbo(mat, 8)["pbo"] == r["pbo"]
    conn = get_connection()
    try:
        saved = store.get_run(conn, r["run_id"])
    finally:
        conn.close()
    assert (saved["kind"], saved["parent_run_id"], saved["status"]) == ("pbo", opt["run_id"], "COMPLETED")
    assert saved["summary"]["pbo"] == saved["metrics"]["pbo"] == r["pbo"]
    assert pbo_for_run(opt["run_id"], 4)["n_trials"] == 6              # its own pbo run is not a trial
    with pytest.raises(ValueError, match="no backtest run"):
        pbo_for_run("nope")
    with pytest.raises(ValueError, match="at least two"):
        pbo_for_run(opt["trials"][0]["run_id"])                       # a single run has no trials


def test_cli_and_routes_expose_cpcv_and_pbo(market, tmp_path, monkeypatch, capsys):
    from backtest.__main__ import main
    from backtest.optimize import optimize
    base = ["--strategy", "dip", "--universe", ",".join(WAVES), "--start", START, "--end", END]
    main(["cpcv", *base, "--groups", "4", "--test-groups", "2", "--candidates", json.dumps(CANDIDATES[:2]),
          "--purge", "2", "--embargo", "1"])
    out = json.loads(capsys.readouterr().out)
    assert (out["status"], out["n_splits"], out["n_paths"]) == ("COMPLETED", 6, 3)
    assert out["distribution"]["sharpe"]["n"] == 3 and len(out["selection_frequency"]) == 2
    cpcv_id = out["run_id"]
    opt = optimize(_dip(), {"target_pct": {"values": [3, 6, 12]}}, "grid", min_trades=1)
    main(["pbo", opt["run_id"], "--partitions", "4"])
    out = json.loads(capsys.readouterr().out)
    assert (out["n_trials"], out["n_partitions"], out["source_run_id"]) == (3, 4, opt["run_id"])
    (tmp_path / "m.csv").write_text("a,b\n0.01,0.02\n0.03,0.00\n0.02,0.01\n0.00,0.01\n", encoding="utf-8")
    main(["pbo", "--matrix", str(tmp_path / "m.csv"), "--partitions", "2", "--metric", "mean"])
    out = json.loads(capsys.readouterr().out)
    assert out["pbo"] == 0.5 and out["selected_trials"] == [{"trial": 0, "label": "a", "share": 1.0}]
    with pytest.raises(SystemExit):
        main(["pbo"])                                                 # a run or a matrix

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    c = TestClient(server.app)
    h = {security.TOKEN_HEADER: security.token()}
    # validation is synchronous, so a bad setting is a 400 and nothing starts in the background
    r = c.post("/api/backtests/cpcv", headers=h, json={"request": _dip(), "groups": 13})
    assert r.status_code == 400 and "n_groups" in r.json()["error"]
    r = c.post("/api/backtests/cpcv", headers=h, json={"request": _dip(), "candidates": [{}] * 21})
    assert r.status_code == 400 and "candidates" in r.json()["error"]
    r = c.post(f"/api/backtests/{opt['run_id']}/pbo", headers=h, json={"partitions": 4})
    assert r.status_code == 200 and r.json()["n_trials"] == 3 and r.json()["source_run_id"] == opt["run_id"]
    r = c.post(f"/api/backtests/{opt['run_id']}/pbo", headers=h, json={"partitions": 3})
    assert r.status_code == 400 and "even" in r.json()["error"]
    assert c.post("/api/backtests/nope/pbo", headers=h, json={}).status_code == 404
    r = c.get(f"/api/backtests/{cpcv_id}", headers=h)
    assert r.status_code == 200 and len(r.json()["children"]) == 8 and r.json()["kind"] == "cpcv"
