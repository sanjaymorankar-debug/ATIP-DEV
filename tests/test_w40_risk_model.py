"""W40: the fundamental factor risk model (quant/risk_model.py, gap analysis 4 item 9).

Two kinds of evidence:
  * simulated data with a PLANTED factor structure (known exposures, factor returns drawn from a known
    covariance, known specific variances): the constrained WLS recovers the factor returns (exactly
    without noise, with calibrated standard errors with it), the EWMA / Newey-West covariance approaches
    the true one, specific risk is estimated and shrunk as specified, and the bias statistic is ~1 for
    the true model -- and for the model estimated end to end -- and clearly off for mis-specified ones;
  * a seeded database (tests/_risk_model_seed.py: 53 stocks in 4 NSE industries + a small one, filings
    with knowledge dates, a PAPER book): the nightly update is idempotent and incremental (== a full
    rebuild), the stored factor returns satisfy the industry constraint, a filing is invisible before its
    knowledge date, the book decomposition adds up, and the routes answer on it.
Every series is seeded; nothing touches the network.
"""

import json
import math
import sqlite3
import time
from datetime import timedelta

import numpy as np
import pytest

from quant import risk_model as RM
from tests import _risk_model_seed as SEED


# ── a planted factor structure ──────────────────────────────────────────────────

def _structure(N=400, J=5, S=3, seed=1):
    rng = np.random.default_rng(seed)
    ind = rng.integers(0, J, N)
    mcap = np.exp(rng.normal(8, 1.2, N))
    K = 1 + J + S
    X = np.zeros((N, K))
    X[:, 0] = 1.0
    X[np.arange(N), 1 + ind] = 1.0
    raw = rng.normal(0, 1, (N, S))
    est = np.ones(N, bool)
    X[:, 1 + J:] = np.column_stack([RM.standardise(raw[:, k], mcap, est) for k in range(S)])
    icap = np.array([mcap[ind == j].sum() for j in range(J)])
    c = icap / icap.sum()
    A = rng.normal(0, 1, (K, K))
    C = A @ A.T
    d = np.sqrt(np.diag(C))
    C = C / np.outer(d, d)
    vols = np.r_[0.011, np.full(J, 0.006), np.full(S, 0.004)]
    F = np.outer(vols, vols) * C
    P = np.eye(K)                                   # projection onto the industry constraint
    P[1:1 + J, 1:1 + J] -= np.outer(np.ones(J), c)
    spec = rng.uniform(0.01, 0.03, N)
    return {"rng": rng, "N": N, "J": J, "S": S, "K": K, "X": X, "mcap": mcap, "ind": ind, "icap": icap, "c": c,
            "F": F, "Fc": P @ F @ P.T, "P": P, "L": np.linalg.cholesky(F), "spec": spec,
            "ind_cols": list(range(1, 1 + J))}


def _simulate(s, T):
    """T days of constrained factor returns and stock returns r = X f + u."""
    rng = s["rng"]
    f = (s["P"] @ (s["L"] @ rng.normal(size=(s["K"], T)))).T
    u = s["spec"] * rng.normal(size=(T, s["N"]))
    return f, f @ s["X"].T + u


def test_regression_recovers_planted_factor_returns_with_calibrated_errors():
    s = _structure()
    X, v = s["X"], np.sqrt(s["mcap"])
    # no noise: exact, R^2 = 1
    f = s["P"] @ (s["L"] @ s["rng"].normal(size=s["K"]))
    out = RM.factor_regression(X, X @ f, v, s["ind_cols"], s["icap"])
    assert np.allclose(out["f"], f, atol=1e-13) and out["r2"] == pytest.approx(1.0)
    # with specific noise, 300 days
    fs, R = _simulate(s, 300)
    est, z = [], []
    for t in range(300):
        o = RM.factor_regression(X, R[t], v, s["ind_cols"], s["icap"])
        assert abs(float(o["f"][1:1 + s["J"]] @ s["c"])) < 1e-13            # the constraint holds every day
        est.append(o["f"])
        z.append((o["f"] - fs[t]) / o["se"])
    est, z = np.array(est), np.array(z)
    corr = [np.corrcoef(est[:, k], fs[:, k])[0, 1] for k in range(s["K"])]
    assert corr[0] > 0.98 and min(corr) > 0.8, corr
    assert 0.85 < z.std() < 1.15 and abs(z.mean()) < 0.1                       # the t statistics are honest


def test_industry_constraint_identifies_the_market_factor():
    s = _structure(seed=2)
    X, v, J = s["X"], np.sqrt(s["mcap"]), s["J"]
    f = s["L"] @ s["rng"].normal(size=s["K"])          # NOT on the constraint: the data cannot tell
    out = RM.factor_regression(X, X @ f, v, s["ind_cols"], s["icap"])
    a = float(s["c"] @ f[1:1 + J])                     # the market absorbs the cap-weighted industry mean
    assert out["f"][0] == pytest.approx(f[0] + a, abs=1e-13)
    assert np.allclose(out["f"][1:1 + J], f[1:1 + J] - a, atol=1e-13)
    assert np.allclose(out["f"][1 + J:], f[1 + J:], atol=1e-13)
    # styles are cap-weighted mean 0, so the market factor IS the cap-weighted universe return
    r = X @ f
    assert out["f"][0] == pytest.approx(float((s["mcap"] * r).sum() / s["mcap"].sum()), abs=1e-13)
    # an industry with no stock that day is not estimated; the constraint runs over the others
    keep = s["ind"] != 0
    o2 = RM.factor_regression(X[keep], r[keep], v[keep], s["ind_cols"],
                              [s["mcap"][keep & (s["ind"] == j)].sum() for j in range(J)])
    assert o2["f"][1] == 0.0 and not o2["active"][1] and math.isnan(o2["t"][1])
    c2 = np.array([s["mcap"][keep & (s["ind"] == j)].sum() for j in range(J)])
    assert abs(float(o2["f"][1:1 + J] @ c2)) < 1e-13


def test_covariance_estimate_approaches_the_true_one():
    rng = np.random.default_rng(5)
    K = 4
    A = rng.normal(size=(K, K))
    F = A @ A.T * 1e-5
    L = np.linalg.cholesky(F)
    errs = []
    for T in (250, 1000, 6000):
        Fr = (L @ rng.normal(size=(K, T))).T
        Fh = RM.factor_covariance(Fr, T, T, 0)         # half-life ~ the sample: close to equal weights
        errs.append(np.linalg.norm(Fh - F) / np.linalg.norm(F))
    assert errs[0] > errs[2] and errs[2] < 0.06, errs
    # volatilities from the vol half-life, correlations from the corr half-life
    Fr = (L @ rng.normal(size=(K, 800))).T
    Fh = RM.factor_covariance(Fr, 60, 240, 2)
    Sv, Sc = RM.ewma_nw(Fr, 60, 2), RM.ewma_nw(Fr, 240, 2)
    assert np.allclose(np.diag(Fh), np.diag(Sv), rtol=1e-10)
    corr = lambda M: M / np.outer(np.sqrt(np.diag(M)), np.sqrt(np.diag(M)))
    assert np.allclose(corr(Fh), corr(Sc), atol=1e-8)
    assert np.linalg.eigvalsh(Fh).min() > -1e-15
    # Newey-West: AR(1) returns, phi = 0.4 -> Bartlett(2) long-run variance 1 + 2 (2/3 phi + 1/3 phi^2)
    phi, n = 0.4, 40000
    e = rng.normal(size=n)
    x = np.zeros(n)
    for i in range(1, n):
        x[i] = phi * x[i - 1] + e[i]
    ratio = RM.ewma_nw(x[:, None], 1e9, 2)[0, 0] / RM.ewma_nw(x[:, None], 1e9, 0)[0, 0]
    assert ratio == pytest.approx(1 + 2 * (2 / 3 * phi + 1 / 3 * phi ** 2), rel=0.04)
    # from REGRESSION-estimated factor returns (estimation noise included) it still approaches the truth
    s = _structure(N=600, seed=3)
    fs, R = _simulate(s, 1500)
    est = np.array([RM.factor_regression(s["X"], R[t], np.sqrt(s["mcap"]), s["ind_cols"], s["icap"])["f"]
                    for t in range(1500)])
    Fh = RM.factor_covariance(est, 1500, 1500, 0)
    vol_err = np.abs(np.sqrt(np.diag(Fh)) / np.sqrt(np.diag(s["Fc"])) - 1)
    assert np.median(vol_err) < 0.1 and vol_err[0] < 0.06, vol_err


def test_bias_statistic_is_one_for_a_correct_model_and_off_for_a_mis_specified_one():
    s = _structure(N=300, seed=4)
    T = 600
    fs, R = _simulate(s, T)
    X, rng = s["X"], s["rng"]
    W = np.zeros((s["N"], 12))
    for p in range(12):
        W[rng.choice(s["N"], 25, replace=False), p] = rng.dirichlet(np.ones(25))
    real = R @ W                                                              # T x P

    def sigma(F, spec_var):
        XW = X.T @ W
        return np.sqrt(np.einsum("kp,kl,lp->p", XW, F, XW) + (W ** 2 * spec_var[:, None]).sum(0))
    right = [RM.bias_statistic(real[:, p] / sigma(s["Fc"], s["spec"] ** 2)[p]) for p in range(12)]
    # the 12 portfolios share one draw of the factor returns, so their biases move together: the median
    # sits inside one band half-width of 1 and none is more than two half-widths away
    hw = 1.96 / math.sqrt(2 * (T - 1))
    biases = [b["bias"] for b in right]
    assert abs(float(np.median(biases)) - 1) < hw and max(abs(b - 1) for b in biases) < 2 * hw, biases
    assert sum(b["in_band"] for b in right) >= 8
    half = [RM.bias_statistic(real[:, p] / sigma(s["Fc"] / 4, s["spec"] ** 2 / 4)[p]) for p in range(12)]
    double = [RM.bias_statistic(real[:, p] / sigma(s["Fc"] * 4, s["spec"] ** 2 * 4)[p]) for p in range(12)]
    no_factor = [RM.bias_statistic(real[:, p] / sigma(s["Fc"] * 0, s["spec"] ** 2)[p]) for p in range(12)]
    assert all(b["bias"] > b["band"][1] and 1.8 < b["bias"] < 2.2 for b in half)        # risk under-forecast
    assert all(b["bias"] < b["band"][0] and 0.45 < b["bias"] < 0.55 for b in double)    # over-forecast
    assert all(b["bias"] > 1.5 and not b["in_band"] for b in no_factor)
    b = RM.bias_statistic(np.r_[np.ones(50), -np.ones(51)])
    assert b["T"] == 101 and b["band"] == pytest.approx([1 - 1.96 / math.sqrt(200), 1 + 1.96 / math.sqrt(200)])
    assert RM.bias_statistic([1.0, 2.0])["bias"] is None                      # too short to say anything

    # the model ESTIMATED end to end (regression -> EWMA / NW covariance -> EWMA + shrunk specific risk)
    est, U = [], []
    for t in range(T):
        o = RM.factor_regression(X, R[t], np.sqrt(s["mcap"]), s["ind_cols"], s["icap"])
        est.append(o["f"])
        U.append(o["resid"])
    est, U = np.array(est), np.array(U)
    buckets = RM.size_buckets(s["mcap"], 3)
    z = {p: [] for p in range(12)}
    for t in range(200, T):
        F = RM.factor_covariance(est[:t], 90, 180, 2)
        sig, _ = RM.specific_vol(U[:t][-360:], 90, 42)
        sh, _ = RM.shrink_specific(sig, s["mcap"], buckets, 0.1)
        sd = sigma(F, sh ** 2)
        for p in range(12):
            z[p].append(real[t, p] / sd[p])
    est_b = [RM.bias_statistic(z[p])["bias"] for p in range(12)]
    assert 0.9 < float(np.median(est_b)) < 1.1 and max(abs(b - 1) for b in est_b) < 0.2, est_b


def test_exposures_are_winsorised_standardised_filled_and_flagged():
    rng = np.random.default_rng(7)
    n = 300
    mcap = np.where(np.arange(n) < 260, np.exp(rng.normal(8, 1, n)), np.nan)
    estu = np.isfinite(mcap)
    ind = rng.integers(0, 4, n)
    raw = {s: rng.normal(0, 1, n) for s in RM.STYLES}
    raw["size"] = np.log(mcap)
    raw["book_to_price"][5] = 1e6                                             # a data error
    raw["momentum"][[10, 11, 12, 270]] = np.nan                               # missing descriptors
    raw["quality"][:] = np.nan
    raw["quality"][:40] = rng.normal(0, 1, 40)                                # < 30% of the estu: off
    Z, filled, off = RM.build_exposures(raw, mcap, estu, ind)
    k = {s: i for i, s in enumerate(RM.STYLES)}
    assert off == ["quality"] and (Z[:, k["quality"]] == 0).all() and filled[:, k["quality"]].all()
    for s in RM.STYLES:
        if s == "quality":
            continue
        z = Z[estu, k[s]]
        assert float((mcap[estu] * z).sum() / mcap[estu].sum()) == pytest.approx(0, abs=1e-12)    # cap-weighted 0
        assert float(z.std()) == pytest.approx(1, abs=1e-12)                                      # equal-weighted 1
    assert Z[5, k["book_to_price"]] <= np.sort(Z[estu, k["book_to_price"]])[-2] + 0.05 and Z[5, k["book_to_price"]] < 4
    m = k["momentum"]
    assert filled[[10, 11, 12, 270], m].all() and filled[:, m].sum() == 4
    for i in (10, 11, 12):                     # the industry's median: equal within the industry, inside its range
        same = estu & (ind == ind[i]) & ~filled[:, m]
        assert Z[i, m] == pytest.approx(float(np.median(Z[same, m])), abs=1e-12)
    assert filled[~estu, k["size"]].all() and not filled[estu, k["size"]].any()              # no market cap
    # resid_vol is orthogonal to size and beta (cap-weighted, on the rows that had all three)
    clean = estu & ~filled[:, k["resid_vol"]] & ~filled[:, k["size"]] & ~filled[:, k["beta"]]
    A = np.column_stack([np.ones(n), Z[:, k["size"]], Z[:, k["beta"]]])[clean]
    w = np.sqrt(mcap[clean])
    coef = np.linalg.lstsq(A * w[:, None], Z[clean, k["resid_vol"]] * w, rcond=None)[0]
    assert np.abs(coef[1:]).max() < 1e-10


def test_industry_model_merges_small_industries(monkeypatch):
    sec = {}
    for name, n in (("Financial Services", 10), ("Chemicals", 9), ("Textiles", 3), ("Consumer Durables", 3),
                    ("Realty", 3), ("Forest Materials", 2), ("Power", 1), ("Space Tourism", 2), ("Insurance", 2)):
        for i in range(n):
            sec[f"{name[:3].upper()}{i}"] = name
    syms = sorted(sec) + ["NOSECTOR"]
    monkeypatch.setitem(RM.NSE_MACRO, "Insurance", "Financial Services")
    im = RM.industry_model(syms, sec, 8)
    m = im["map"]
    assert m["Financial Services"] == "Financial Services" and m["Chemicals"] == "Chemicals"
    assert m["Insurance"] == "Financial Services"                              # its parent is a kept industry
    assert {m["Textiles"], m["Consumer Durables"], m["Realty"]} == {"Consumer Discretionary"}   # pooled: 9 >= 8
    assert m["Forest Materials"] == m["Power"] == m["Space Tourism"] == RM.OTHER                 # too small
    assert im["industries"] == ["Chemicals", "Consumer Discretionary", "Financial Services", RM.OTHER]
    assert im["counts"][RM.OTHER] == 2 + 1 + 2 + 1 and im["counts"]["Financial Services"] == 12
    assert sorted(im["merged"]["Consumer Discretionary"]) == ["Consumer Durables", "Realty", "Textiles"]


def test_specific_risk_ewma_and_bayesian_shrinkage():
    rng = np.random.default_rng(8)
    true = rng.uniform(0.01, 0.04, 200)
    U = rng.normal(size=(2000, 200)) * true
    U[:, 0] = np.nan
    U[-10:, 1] = np.nan                                     # gaps are skipped, not zeros
    sig, nobs = RM.specific_vol(U, 1e9, 42)
    assert math.isnan(sig[0]) and nobs[0] == 0 and nobs[1] == 1990
    assert np.nanmedian(np.abs(sig / true - 1)) < 0.03
    w = RM.ewma_weights(5, 2)
    assert np.allclose(w, 0.5 ** (np.array([4, 3, 2, 1, 0]) / 2))
    # shrinkage by hand: one bucket
    s = np.array([0.01, 0.02, 0.05, np.nan])
    mc = np.array([1.0, 1.0, 2.0, 1.0])
    out, fb = RM.shrink_specific(s, mc, np.array(["A"] * 4, dtype=object), q=0.1)
    mean = (0.01 + 0.02 + 2 * 0.05) / 4
    disp = math.sqrt(((s[:3] - mean) ** 2).mean())
    for i in range(3):
        v = 0.1 * abs(s[i] - mean) / (disp + 0.1 * abs(s[i] - mean))
        assert out[i] == pytest.approx(v * mean + (1 - v) * s[i], rel=1e-12)
    assert out[3] == pytest.approx(mean) and fb.tolist() == [False, False, False, True]
    assert abs(out[2] - mean) < abs(s[2] - mean)                                            # pulled toward the mean
    # no market cap -> shrunk toward every bucket together
    out2, _ = RM.shrink_specific(np.array([0.01, 0.03, 0.06]), np.array([1.0, 1.0, np.nan]),
                                 np.array(["A", "B", None], dtype=object))
    assert 0.01 < out2[2] < 0.06
    # AMFI size buckets (research/tech_signals.py CAP_BUCKETS) and quantiles
    b = RM.size_buckets(np.r_[np.arange(300, 0, -1.0), np.nan])
    assert list(b[:100]) == ["LARGE"] * 100 and list(b[100:250]) == ["MID"] * 150 and set(b[250:300]) == {"SMALL"}
    assert b[300] is None
    q = RM.size_buckets(np.arange(9, 0, -1.0), 3)
    assert list(q) == ["Q1"] * 3 + ["Q2"] * 3 + ["Q3"] * 3


def test_decomposition_adds_up():
    rng = np.random.default_rng(9)
    n, K = 30, 8
    X = rng.normal(size=(n, K))
    A = rng.normal(size=(K, K))
    F = A @ A.T * 1e-5
    d = rng.uniform(1e-4, 9e-4, n)
    w = rng.dirichlet(np.ones(n))
    wb = rng.dirichlet(np.ones(n))
    o = RM.decompose(w, X, F, d, wb=wb)
    assert o["var_factor"] + o["var_specific"] == pytest.approx(o["var"], rel=1e-15)
    assert float(o["contrib"].sum()) == pytest.approx(o["var_factor"], rel=1e-12)
    assert np.allclose(o["contrib"], (X.T @ w) * (F @ (X.T @ w)))                           # x_k (F X'w)_k
    assert float(o["ctr"].sum()) == pytest.approx(o["sd"], rel=1e-12)
    assert np.allclose(o["ctr_factor"] + o["ctr_specific"], o["ctr"])
    sd = lambda ww: math.sqrt(ww @ (X @ F @ X.T + np.diag(d)) @ ww)
    h = 1e-7
    grad = [(sd(w + h * np.eye(n)[i]) - sd(w - h * np.eye(n)[i])) / (2 * h) for i in range(n)]
    assert np.allclose(o["mcr"], grad, rtol=1e-5)                                           # MCR = d sigma / d w
    S = X @ F @ X.T + np.diag(d)
    assert o["beta"] == pytest.approx(float(w @ S @ wb / (wb @ S @ wb)), rel=1e-12)
    assert o["var_active"] == pytest.approx(float((w - wb) @ S @ (w - wb)), rel=1e-12)
    same = RM.decompose(w, X, F, d, wb=w)
    assert same["beta"] == pytest.approx(1.0) and same["var_active"] == pytest.approx(0.0, abs=1e-20)
    idx = RM.decompose(w, X, F, d, bench_x=X.T @ wb, bench_resid_var=2e-5)
    assert idx["var_bench"] == pytest.approx(float((X.T @ wb) @ F @ (X.T @ wb)) + 2e-5)


# ── the stored model on a seeded database ─────────────────────────────────────

@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """One seeded database with the model built, copied for every test that wants it."""
    import db.schema as schema
    path = tmp_path_factory.mktemp("rm") / "built.db"
    old = schema.DB_PATH
    schema.DB_PATH = path
    try:
        schema.init_db()
        conn = schema.get_connection()
        info = SEED.seed(conn)
        cfg = SEED.cfg()
        first = RM.update(conn, sectors=info["sectors"], cfg=cfg)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
    finally:
        schema.DB_PATH = old
    return {"path": path, "info": info, "cfg": cfg, "first": first}


@pytest.fixture
def db(built, tmp_path, monkeypatch):
    import db.schema as schema
    dest = tmp_path / "rm.db"
    src, dst = sqlite3.connect(str(built["path"])), sqlite3.connect(str(dest))
    src.backup(dst)
    src.close()
    dst.close()
    monkeypatch.setattr(schema, "DB_PATH", dest)
    monkeypatch.setattr(RM, "settings", lambda: built["cfg"])
    monkeypatch.setattr(RM, "sector_map", lambda: built["info"]["sectors"])
    conn = schema.get_connection()
    yield conn
    conn.close()


def _dump(conn):
    out = {}
    for t, key in (("quant_risk_exposure", "as_of, symbol"), ("quant_risk_factor_return", "date, factor"),
                   ("quant_risk_regression", "date"), ("quant_risk_covariance", "as_of")):
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY {key}")]
        for r in rows:
            r.pop("computed_at", None)
        out[t] = rows
    return out


def _same(a, b, tol=1e-9):
    assert a.keys() == b.keys()
    for t in a:
        assert len(a[t]) == len(b[t]), t
        for ra, rb in zip(a[t], b[t]):
            assert ra.keys() == rb.keys()
            for k in ra:
                x, y = ra[k], rb[k]
                if k == "cov_json":
                    assert np.allclose(json.loads(x), json.loads(y), rtol=1e-6, atol=1e-14), (t, k)
                elif isinstance(x, float) or isinstance(y, float):
                    assert x is not None and y is not None and abs(x - y) <= tol * max(1.0, abs(x)), (t, k, ra, rb)
                else:
                    assert x == y, (t, k, x, y)


def test_update_builds_the_model_and_a_second_run_is_a_no_op(built, db):
    first = built["first"]
    assert first["status"] == "SUCCESS" and not first["rebuilt"] and first["exposures"]["dates"] > 200
    assert first["regressions"]["dates"] == first["exposures"]["dates"] - 1 and first["forecasts"]["dates"] > 150
    before = _dump(db)
    again = RM.update(db, sectors=built["info"]["sectors"], cfg=built["cfg"])
    assert again["status"] == "NO_NEW" and again["rows"] == 0
    _same(before, _dump(db), tol=0)
    # through the scheduler's run_job (pipeline_log), as the nightly job runs it
    from pipeline import w39_jobs
    from pipeline.scheduler import run_job
    r = run_job("risk_model", w39_jobs.risk_model_run)
    assert r["status"] == "NO_NEW"
    log = db.execute("SELECT status, rows_processed FROM pipeline_log WHERE job_name='risk_model' AND kind='run'").fetchall()
    assert [tuple(x) for x in log] == [("NO_NEW", 0)]
    _same(before, _dump(db), tol=0)


def test_incremental_update_equals_a_full_rebuild(built, db):
    cal = built["info"]["calendar"]
    full = _dump(db)
    RM.update(db, sectors=built["info"]["sectors"], cfg=built["cfg"], rebuild=True)
    _same(full, _dump(db))                                         # deterministic
    for t in ("quant_risk_exposure", "quant_risk_factor_return", "quant_risk_regression", "quant_risk_covariance"):
        db.execute(f"DELETE FROM {t}")
    db.commit()
    a = RM.update(db, as_of=cal[-40], sectors=built["info"]["sectors"], cfg=built["cfg"])
    assert a["status"] == "SUCCESS" and a["as_of"] == str(cal[-40])
    assert str(db.execute("SELECT MAX(as_of) FROM quant_risk_exposure").fetchone()[0])[:10] == str(cal[-40])
    nxt = db.execute("SELECT ret_next FROM quant_risk_exposure WHERE as_of=? AND ret_next IS NOT NULL",
                     (str(cal[-40]),)).fetchall()
    assert nxt == []                                               # tomorrow is not known yet
    for k in range(39, 0, -13):                                    # three nightly catch-ups
        RM.update(db, as_of=cal[-k], sectors=built["info"]["sectors"], cfg=built["cfg"])
    b = RM.update(db, sectors=built["info"]["sectors"], cfg=built["cfg"])
    assert b["status"] == "SUCCESS"
    _same(full, _dump(db))


def test_a_session_whose_stock_bars_are_late_waits_for_them(built, db):
    """NIFTY50 arrives before the Bhavcopy: the session is not modelled half-filled but on the next run."""
    cal = built["info"]["calendar"]
    full = _dump(db)
    bars = [tuple(r) for r in db.execute("SELECT symbol, date, open, high, low, close, volume, source FROM prices_daily "
                                         "WHERE date=? AND symbol<>'NIFTY50'", (str(cal[-1]),))]
    db.execute("DELETE FROM prices_daily WHERE date=? AND symbol<>'NIFTY50'", (str(cal[-1]),))
    db.commit()
    out = RM.update(db, sectors=built["info"]["sectors"], cfg=built["cfg"], rebuild=True)
    assert out["as_of"] == str(cal[-2]) and out["incomplete_sessions"] == [str(cal[-1])]
    assert str(db.execute("SELECT MAX(as_of) FROM quant_risk_exposure").fetchone()[0])[:10] == str(cal[-2])
    again = RM.update(db, sectors=built["info"]["sectors"], cfg=built["cfg"])
    assert again["status"] == "NO_NEW" and "waiting for the stock bars of " + str(cal[-1]) in again["reason"]
    db.executemany("INSERT INTO prices_daily (symbol, date, open, high, low, close, volume, source) "
                   "VALUES (?,?,?,?,?,?,?,?)", bars)
    db.commit()
    late = RM.update(db, sectors=built["info"]["sectors"], cfg=built["cfg"])
    assert late["status"] == "SUCCESS" and late["exposures"]["dates"] == 1 and late["regressions"]["dates"] == 1
    _same(full, _dump(db))


def test_without_market_caps_nothing_is_modelled_and_the_status_says_why(built, db):
    db.execute("DELETE FROM fundamental_data")
    db.commit()
    out = RM.update(db, sectors=built["info"]["sectors"], cfg=built["cfg"], rebuild=True)
    assert out["status"] == "SKIPPED" and out["rows"] == 0 and "with a market cap" in out["reason"]
    st = RM.status(db)
    assert st["status"] == "NO_DATA" and "shares_out" in st["note"]
    assert RM.decompose_weights(db, {"FIN00": 1.0}, cfg=built["cfg"])["status"] == "NOT_BUILT"
    assert RM.validate({"min_history": 300})["min_history"] == RM.DEFAULTS["beta_window"]


def test_a_parameter_change_rebuilds_the_model(built, db):
    old = RM.get_state(db, "model")["model_hash"]
    cfg = SEED.cfg(vol_halflife=45)
    out = RM.update(db, sectors=built["info"]["sectors"], cfg=cfg)
    assert out["rebuilt"] and out["model_hash"] != old and out["status"] == "SUCCESS"
    assert RM.get_state(db, "model")["model_hash"] == out["model_hash"]
    # a parameter that changes no stored number (the bias window) does not
    assert not RM.update(db, sectors=built["info"]["sectors"], cfg=SEED.cfg(vol_halflife=45, bias_window=120))["rebuilt"]


def test_a_vanished_universe_or_industry_map_does_not_rebuild_a_degenerate_model(built, db):
    before = _dump(db)
    small = RM.update(db, universe=SEED.SYMBOLS[:10], sectors=built["info"]["sectors"], cfg=built["cfg"])
    assert small["status"] == "SKIPPED" and "shrank from 53 to" in small["reason"]
    blind = RM.update(db, sectors={}, cfg=built["cfg"])
    assert blind["status"] == "SKIPPED" and "industry map" in blind["reason"]
    _same(before, _dump(db), tol=0)                                # nothing was touched
    forced = RM.update(db, universe=SEED.SYMBOLS[:40], sectors=built["info"]["sectors"], cfg=built["cfg"], rebuild=True)
    assert forced["rebuilt"] and forced["status"] == "SUCCESS"


def test_stored_factor_returns_satisfy_the_industry_constraint(built, db):
    st = RM.get_state(db, "model")
    inds = [f for f in st["factors"] if RM.kind_of(f) == "industry"]
    assert inds == ["Capital Goods", "Financial Services", "Healthcare", "Information Technology", "Other"]
    assert st["merged"] == {"Other": ["Textiles"]}
    n = 0
    for (d,) in db.execute("SELECT date FROM quant_risk_regression WHERE r2 IS NOT NULL"):
        e = str(db.execute("SELECT exposure_date FROM quant_risk_regression WHERE date=?", (d,)).fetchone()[0])[:10]
        cap = dict.fromkeys(inds, 0.0)
        for g, mc in db.execute("SELECT industry, mcap_cr FROM quant_risk_exposure WHERE as_of=? AND in_estu=1 AND "
                                "ret_next IS NOT NULL", (e,)):
            cap[g] += mc
        f = dict(db.execute("SELECT factor, ret FROM quant_risk_factor_return WHERE date=?", (d,)).fetchall())
        assert abs(sum(cap[g] * f[g] for g in inds) / sum(cap.values())) < 1e-12
        n += 1
    assert n > 200
    r2 = [r[0] for r in db.execute("SELECT r2 FROM quant_risk_regression WHERE r2 IS NOT NULL")]
    assert 0 < np.mean(r2) < 1


def test_point_in_time_a_fundamental_filed_later_is_invisible_earlier(built, db):
    late = built["info"]["late_filed"]                       # FIN11's first filing: 2026-05-10 18:00
    cal = built["info"]["calendar"]
    before = max(d for d in cal if d < late)
    after = min(d for d in cal if d >= late)
    row = lambda d: dict(db.execute("SELECT * FROM quant_risk_exposure WHERE as_of=? AND symbol=?",
                                    (str(d), SEED.LATE)).fetchone())
    b, a = row(before), row(after)
    assert b["in_estu"] == 0 and b["mcap_cr"] is None and "size" in b["filled"].split(",")
    assert "book_to_price" in b["filled"].split(",")
    assert a["in_estu"] == 1 and "size" not in a["filled"].split(",")
    close = db.execute("SELECT close FROM prices_daily WHERE symbol=? AND date=?", (SEED.LATE, str(after))).fetchone()[0]
    sh = built["info"]["info"][SEED.LATE]["shares"]
    assert a["mcap_cr"] == pytest.approx(close * sh / 1e7, rel=1e-6)
    # the panel: a value appears exactly on its knowledge date, never before
    days = [late - timedelta(days=1), late, late + timedelta(days=1)]
    p = RM.fundamentals_panel(db, [SEED.LATE, "FIN00"], days)
    assert math.isnan(p["shares_out"][0, 0]) and p["shares_out"][1, 0] == pytest.approx(sh)
    # a restated / late row for FIN00 changes nothing before it is known
    fin00 = p["book_value_ps"][:, 1].copy()
    db.execute("INSERT INTO fundamental_data (symbol, quarter, report_date, period_end, available_from, shares_out, "
               "book_value_ps) VALUES ('FIN00','Q9-2026','2026-06-30','2026-06-30',?,?,999.0)",
               (f"{late + timedelta(days=1)} 09:00:00", sh))
    db.commit()
    p2 = RM.fundamentals_panel(db, [SEED.LATE, "FIN00"], days)
    assert p2["book_value_ps"][0, 1] == fin00[0] and p2["book_value_ps"][1, 1] == fin00[1]
    assert p2["book_value_ps"][2, 1] == 999.0
    # the same knowledge date as quant/factors.fundamentals_as_of
    from quant.factors import fundamentals_as_of
    assert fundamentals_as_of(db, ["FIN00"], late)["FIN00"][-1]["book_value_ps"] != 999.0
    assert fundamentals_as_of(db, ["FIN00"], late + timedelta(days=1))["FIN00"][-1]["book_value_ps"] == 999.0


def test_stored_exposures_are_standardised_on_the_estimation_universe(built, db):
    d = str(db.execute("SELECT MAX(as_of) FROM quant_risk_exposure").fetchone()[0])[:10]
    rows = [dict(r) for r in db.execute("SELECT * FROM quant_risk_exposure WHERE as_of=?", (d,))]
    est = [r for r in rows if r["in_estu"]]
    mc = np.array([r["mcap_cr"] for r in est])
    for c in RM._XCOLS:
        z = np.array([r[c] for r in est])
        if not z.any():
            continue
        assert float(mc @ z / mc.sum()) == pytest.approx(0, abs=1e-9) and float(z.std()) == pytest.approx(1, abs=1e-9)
    zz = next(r for r in rows if r["symbol"] == "ZZZ01")              # no NSE industry, no filings
    assert zz["industry"] == RM.OTHER and {"industry", "size"} <= set(zz["filled"].split(","))
    assert all(r["spec_var"] > 0 for r in rows)
    first_listed = str(db.execute("SELECT MIN(as_of) FROM quant_risk_exposure WHERE symbol='ZZZ00'").fetchone()[0])[:10]
    cal = [str(x) for x in built["info"]["calendar"]]
    assert cal.index(first_listed) - 150 == built["cfg"]["min_history"] - 1  # exposures only after min_history


def test_book_decomposition_adds_up_and_is_back_tested(built, db):
    from portfolio.risk import factor_risk
    out = factor_risk(db, "PAPER")
    assert out["status"] == "OK" and out["portfolio"] == "PAPER"
    assert out["coverage"]["covered"] == 5 and out["coverage"]["value_share"] == pytest.approx(1.0)
    ch = out["checks"]
    assert abs(ch["factor_plus_specific_minus_total"]) < 1e-14 and abs(ch["factor_contributions_minus_factor_var"]) < 1e-14
    assert abs(ch["stock_contributions_minus_vol"]) < 1e-12
    r = out["risk"]
    assert r["factor_share"] + r["specific_share"] == pytest.approx(1, abs=1e-5)
    assert r["total_var_daily"] == pytest.approx(r["factor_var_daily"] + r["specific_var_daily"], rel=1e-6)
    assert sum(out["groups"].values()) == pytest.approx(1, abs=1e-5)
    assert sum(f["share_of_variance"] for f in out["factors"]) == pytest.approx(r["factor_share"], abs=1e-5)
    assert sum(s["risk_share"] for s in out["stocks"]) == pytest.approx(1, abs=1e-5)
    assert {s["symbol"] for s in out["stocks"]} == set(SEED.BOOK)
    assert out["benchmark"]["kind"] == "NIFTY50_PROXY" and out["benchmark"]["names"] == 15
    assert 0.3 < out["beta"]["value"] < 2 and out["active"]["active_vol_pct_annual"] > 0
    b = out["bias"]["portfolios"][0]
    assert b["portfolio"] == "PAPER" and b["sessions"] == 150 and b["band"][0] < 1 < b["band"][1]
    assert 0.7 < b["bias"] < 1.3
    assert out["bias_test"]["portfolios"] >= 10
    assert out["risk_value"]["var95_1d_value"] > 0
    assert factor_risk(db, "LIVE")["status"] == "EMPTY"
    with pytest.raises(ValueError):
        factor_risk(db, "OTHER")
    # a name the model does not cover is reported, and the rest renormalised -- not dropped silently
    d = RM.decompose_weights(db, {"FIN00": 0.5, "NOTHERE": 0.5}, cfg=built["cfg"])
    assert d["coverage"]["value_share"] == 0.5 and d["coverage"]["uncovered"][0]["symbol"] == "NOTHERE"
    assert d["stocks"][0]["weight"] == 1.0


def test_the_standard_bias_test_is_near_one_on_the_planted_market(built, db):
    b = RM.get_state(db, "bias")
    s = b["summary"]
    assert s["sessions"] == built["cfg"]["bias_window"] and s["portfolios"] >= 10
    assert s["in_band"] >= s["portfolios"] - 2 and 0.9 < s["median_bias"] < 1.1
    names = {p["portfolio"] for p in b["portfolios"]}
    assert {"market (cap-weighted estu)", "equal-weighted estu", "Nifty 50 proxy", "random 1"} <= names


def test_benchmark_falls_back_to_the_index_without_market_caps(built, db):
    cfg = SEED.cfg(benchmark_min_names=500)
    d = RM.decompose_weights(db, {s: 1.0 for s in SEED.BOOK}, cfg=cfg)
    bm = d["benchmark"]
    assert bm["kind"] == "NIFTY50_INDEX" and bm["sessions"] > 100 and bm["r2"] > 0.5
    assert "NIFTY50 index" in bm["description"] and d["active"]["active_vol_pct_annual"] > 0
    assert d["beta"]["value"] is not None


def test_routes_on_a_seeded_database(built, db, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import risk_model_routes, security, server
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    client = TestClient(server.app)
    s = client.get("/api/quant/risk-model/status")
    assert s.status_code == 200
    s = s.json()
    assert s["status"] == "OK" and s["model"]["model_hash"] and s["covariance"]["sessions"] > 150
    assert s["exposures"]["latest_universe"] == len(SEED.SYMBOLS) and s["bias"]["summary"]["portfolios"] >= 10
    f = client.get("/api/quant/risk-model/factors", params={"days": 10})
    assert f.status_code == 200
    f = f.json()
    K = len(f["factors"])
    assert K == 1 + 5 + len(RM.STYLES) and len(f["correlation"]["values"]) == K
    assert all(f["correlation"]["values"][i][i] == 1.0 for i in range(K))
    assert all(x["vol_pct_annual"] > 0 for x in f["factors"]) and "cum_return_pct_10d" in f["factors"][0]
    assert 0 < f["regression"]["latest"]["r2"] < 1
    for bad in ({"days": 0}, {"days": 999}, {"as_of": "yesterday"}):
        assert client.get("/api/quant/risk-model/factors", params=bad).status_code == 400
    p = client.get("/api/portfolio/risk-model", params={"portfolio": "paper"})
    assert p.status_code == 200 and p.json()["status"] == "OK" and len(p.json()["stocks"]) == 5
    assert client.get("/api/portfolio/risk-model", params={"portfolio": "LIVE"}).json()["status"] == "EMPTY"
    assert client.get("/api/portfolio/risk-model", params={"portfolio": "X"}).status_code == 400
    # book= as /api/risk/portfolio (the card uses it); empty or missing = PAPER; a contradiction is refused
    assert client.get("/api/portfolio/risk-model?book=LIVE").json()["portfolio"] == "LIVE"
    assert client.get("/api/portfolio/risk-model?portfolio=").json()["portfolio"] == "PAPER"
    assert client.get("/api/portfolio/risk-model").json()["portfolio"] == "PAPER"
    assert client.get("/api/portfolio/risk-model?portfolio=PAPER&book=LIVE").status_code == 400
    # the one state change needs the token; with it the update runs in the background (here: nothing new)
    assert client.post("/api/quant/risk-model/run", json={}).status_code in (401, 403)
    r = client.post("/api/quant/risk-model/run", json={}, headers={"X-ATIP-Token": security.token()})
    assert r.status_code == 200 and r.json()["started"] is True
    for _ in range(300):
        if not risk_model_routes._RUNNING.locked():
            break
        time.sleep(0.05)
    assert not risk_model_routes._RUNNING.locked()
    last = client.get("/api/quant/risk-model/status").json()["last_job"]
    assert last["status"] == "NO_NEW"
    # the card: /trading (portfolio risk) and /quant read these routes
    t = client.get("/trading").text
    assert "/api/portfolio/risk-model?book=" in t and 'id="pfrm"' in t
    assert "/api/quant/risk-model/factors" in client.get("/quant").text


def test_routes_before_the_model_is_built(temp_db, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    from db.schema import get_connection, init_db
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    init_db()
    conn = get_connection()
    try:
        from orders.paper import ensure_tables
        ensure_tables(conn)
        conn.execute("INSERT INTO prices_daily (symbol, date, close, volume) VALUES ('ACME','2026-09-30',100,1000)")
        conn.execute("INSERT INTO paper_position (symbol, quantity, avg_price) VALUES ('ACME', 10, 90)")
        conn.commit()
    finally:
        conn.close()
    client = TestClient(server.app)
    assert client.get("/api/quant/risk-model/status").json()["status"] == "NOT_BUILT"
    assert client.get("/api/quant/risk-model/factors").json()["status"] == "NOT_BUILT"
    p = client.get("/api/portfolio/risk-model").json()
    assert p["status"] == "NOT_BUILT" and "risk-model/run" in p["note"]


def test_permissions_are_the_existing_quant_and_portfolio_rules():
    from enterprise.authz import PAPER_BOOK, permission_for
    assert permission_for("GET", "/api/quant/risk-model/status") == "quant:read"
    assert permission_for("GET", "/api/quant/risk-model/factors") == "quant:read"
    assert permission_for("POST", "/api/quant/risk-model/run") == "quant:write"
    assert permission_for("GET", "/api/portfolio/risk-model") == "portfolio:read" and "portfolio:read" in PAPER_BOOK
    pytest.importorskip("fastapi")
    from dashboard import server
    routes = {(m, r.path) for r in server.app.routes if "risk-model" in getattr(r, "path", "")
              for m in (getattr(r, "methods", None) or ())}
    assert routes == {("GET", "/api/quant/risk-model/status"), ("GET", "/api/quant/risk-model/factors"),
                      ("POST", "/api/quant/risk-model/run"), ("GET", "/api/portfolio/risk-model")}


def test_nightly_job_is_registered_after_post_market_and_obeys_its_switch(monkeypatch, temp_db):
    from pipeline import w39_jobs

    class _Job:
        def __init__(self, jobs):
            self.jobs, self.t = jobs, None

        @property
        def day(self):
            return self

        def at(self, t):
            self.t = t
            return self

        def do(self, fn, *a, **k):
            self.jobs.append((self.t, fn, a))
            return self

    class _Schedule:
        def __init__(self):
            self.jobs = []

        def every(self, *a):
            return _Job(self.jobs)

    monkeypatch.setattr(w39_jobs, "config", lambda: {"sip_enabled": False})
    fake = _Schedule()
    lines = w39_jobs.schedule_jobs(fake, "RUN_JOB")
    assert [(t, a) for t, fn, a in fake.jobs] == [("21:45", ("risk_model", w39_jobs.risk_model_run))]
    assert any("risk_model" in x for x in lines)
    from pipeline.scheduler import POSTMARKET_CATCHUP_TIME, POSTMARKET_RUN_TIME, EOD_LATE_RUN_TIME
    assert max(POSTMARKET_RUN_TIME, POSTMARKET_CATCHUP_TIME, EOD_LATE_RUN_TIME) < "21:45"
    monkeypatch.setattr(RM, "settings", lambda: RM.validate({"enabled": False}))
    fake2 = _Schedule()
    w39_jobs.schedule_jobs(fake2, "RUN_JOB")
    assert fake2.jobs == []
    assert RM.run_scheduled()["status"] == "SKIPPED"
    monkeypatch.setattr(RM, "settings", lambda: RM.validate({"time": "22:05"}))
    fake3 = _Schedule()
    w39_jobs.schedule_jobs(fake3, "RUN_JOB")
    assert fake3.jobs[0][0] == "22:05"


def test_settings_read_config_json_and_ignore_bad_values(tmp_path, monkeypatch):
    from quant import config as QC
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"risk_model": {"vol_halflife": 60, "corr_halflife": -5, "nope": 1, "enabled": False,
                                            "shrinkage_buckets": "deciles", "history_sessions": 10}}))
    monkeypatch.setattr(QC, "CONFIG_PATH", p)
    s = RM.settings()
    assert s["vol_halflife"] == 60.0 and s["corr_halflife"] == RM.DEFAULTS["corr_halflife"] and s["enabled"] is False
    assert s["shrinkage_buckets"] == "amfi" and s["history_sessions"] == max(s["specific_window"], s["bias_window"]) + 2
    assert len(s["_warnings"]) == 4
    assert RM.model_hash(s, ["market"]) != RM.model_hash({**s, "vol_halflife": 61.0}, ["market"])
    assert RM.model_hash(s, ["market"]) == RM.model_hash({**s, "bias_window": 99, "time": "23:00"}, ["market"])


def test_tables_are_created_classified_and_translate_to_mysql(temp_db):
    from db import mysql as my
    from db.schema import get_connection, init_db
    from db.schema_w39b import W39B_TABLES
    from enterprise.privacy import classify
    from enterprise.scoping import TABLES
    init_db()
    conn = get_connection()
    try:
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    for t in RM.TABLES:
        assert t in W39B_TABLES and t in have
        assert TABLES[t] == "GLOBAL" and classify(t)["class"] == "research" and classify(t)["source"] == "explicit"
    stmts = [s for t in RM.TABLES for s in RM.TABLES[t]]
    keyed = my.keyed_columns(stmts)
    for s in stmts:
        my.ddl(s, keyed.get(my._table_of(s), set()))
    assert not my.reserved_columns(stmts)
