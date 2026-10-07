"""W39: AF-09 time-series / volatility-adjusted momentum factors, PF-14 turnover-penalised
mean-variance, PF-15 factor neutrality (normalize.apply "neutralize", optimise neutral_to).

Expected numbers are computed here with plain numpy or by brute force over a grid of
feasible weights; the "unchanged by default" tests compare the solver with itself, bit for
bit. Every series is deterministic.
"""

import math
from datetime import date, timedelta

import numpy as np
import pytest

from portfolio import optimize as OPT
from quant import factors as F
from quant import normalize as NZ

SECTORS = {"AAA": "Banks", "BBB": "Banks", "CCC": "IT", "DDD": "IT", "EEE": "Pharma", "FFF": "Pharma",
           "GGG": "Autos", "HHH": "Autos"}
SYMS = sorted(SECTORS)


# ── helpers ──────────────────────────────────────────────────────────────

def _ctx(closes, symbol="X"):
    """A FactorContext over weekday bars with these closes (W3 features computed from them)."""
    from backtest.data import Bar
    from strategy_engine.features import FeatureContext
    d, days = date(2026, 9, 30), []
    while len(days) < len(closes):
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    bars = [Bar(dt, c, c, c, c, 1_000_000) for dt, c in zip(days, closes)]
    return F.FactorContext(symbol, days[-1], bars, FeatureContext(symbol, days[-1], bars))


def _path(rets, p0=100.0):
    return [float(x) for x in p0 * np.cumprod(np.concatenate([[1.0], 1 + np.asarray(rets, dtype=float)]))]


def _alternating(n, up, down):
    return [up if i % 2 == 0 else down for i in range(n)]


def _hand_mom(c):
    c = np.asarray(c)
    return (c[-22] / c[-253] - 1) * 100


def _hand_vol(c, n):
    c = np.asarray(c)
    r = c[-n:] / c[-n - 1:-1] - 1
    return r.std(ddof=1) * math.sqrt(252) * 100


def _hand_tsmom(c):
    r, v = _hand_mom(c), _hand_vol(c, 60)
    return np.sign(r) * min(40.0 / v, 2.0)


def _problem(n=8, seed=39):
    """Synthetic annualised (mu, Sigma) from a one-factor return panel."""
    rng = np.random.default_rng(seed)
    R = rng.normal(0.0004, 0.012, (250, n)) + rng.normal(0.0003, 0.009, (250, 1))
    return rng.normal(0.14, 0.06, n), OPT.shrink_cov(R, 0.3) * 252


def _constraints(n=8, max_weight=0.3, sector_cap=0.45):
    syms = SYMS[:n]
    return OPT._constraints(syms, max_weight, None, sector_cap, SECTORS)


# ── AF-09 ────────────────────────────────────────────────────────────────

def test_tsmom_sign_and_vol_scaling():
    up = _path(_alternating(300, 0.025, -0.015))           # trending up, ~32% vol -> scale 40/32
    down = _path(_alternating(300, -0.025, 0.015))         # trending down, same vol
    calm = _path(_alternating(300, 0.006, -0.004))         # trending up, ~8% vol -> 40/8 = 5, capped at 2
    fn = F.get("tsmom_12m").fn
    for c in (up, down, calm):
        assert fn(_ctx(c)) == pytest.approx(_hand_tsmom(c), rel=1e-12)
    assert 1.1 < fn(_ctx(up)) < 1.4 and fn(_ctx(down)) == pytest.approx(-fn(_ctx(up)), rel=1e-2)
    assert fn(_ctx(calm)) == F.TSMOM_MAX_SCALE == 2.0
    # twice the wiggle at the same sign -> about half the size (realised vol doubles)
    wide = _path(_alternating(300, 0.05, -0.04))
    assert fn(_ctx(wide)) == pytest.approx(40.0 / _hand_vol(wide, 60), rel=1e-12)
    assert fn(_ctx(wide)) < 0.65 * fn(_ctx(up))
    # the last month is skipped: up for 12-1, then a crash in the last 21 sessions -> still long
    crash = _path(_alternating(279, 0.01, -0.005) + [-0.05] * 21)
    assert crash[-1] < crash[-253] and _hand_mom(crash) > 0
    assert fn(_ctx(crash)) == pytest.approx(_hand_tsmom(crash), rel=1e-12) and fn(_ctx(crash)) > 0
    assert fn(_ctx(up[-200:])) is None                       # < 253 sessions: no value, nothing estimated
    assert fn(_ctx(list(np.asarray(down)))) == pytest.approx(fn(_ctx(down)), rel=1e-12)   # numpy closes too


def test_mom_12_1_vol_adj_is_the_12_1_return_over_250_session_vol():
    rng = np.random.default_rng(9)
    c = _path(rng.normal(0.0006, 0.017, 299))
    ctx = _ctx(c)
    want = _hand_mom(c) / _hand_vol(c, 250)
    assert F.get("mom_12_1_vol_adj").fn(ctx) == pytest.approx(want, rel=1e-12)
    assert F.get("mom_12_1_vol_adj").fn(ctx) == pytest.approx(F.get("mom_12_1").fn(ctx) / ctx.feat("volatility_250"))
    flat = _ctx([100.0] * 300)
    assert F.get("mom_12_1_vol_adj").fn(flat) is None and F.get("tsmom_12m").fn(flat) is None


def test_new_factors_through_the_engine_match_hand_values(temp_db):
    """The point-in-time path (prices_daily -> PriceHistory -> W3 features -> raw factor)."""
    from backtest.data import is_trading_day
    from db.schema import get_connection, init_db
    from quant import engine as E
    from tests._wealth_seed import sessions
    init_db()
    conn = get_connection()
    try:
        ds = [d for d in sessions(330) if is_trading_day(d)]
        rng = np.random.default_rng(39)
        for k, s in enumerate(("AAA", "BBB", "CCC")):
            for d, c in zip(ds, _path(rng.normal(0.001 * (k - 1), 0.01 + 0.006 * k, len(ds) - 1))):
                conn.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)",
                             (s, str(d), c, c, c, c, 1_000_000))
        conn.commit()
        raw = E.raw_factors(conn, ds[-1], ["tsmom_12m", "mom_12_1_vol_adj"], ["AAA", "BBB", "CCC"])
        for s in ("AAA", "BBB", "CCC"):
            c = [r[0] for r in conn.execute("SELECT close FROM prices_daily WHERE symbol=? ORDER BY date", (s,))]
            assert raw["tsmom_12m"][s] == pytest.approx(_hand_tsmom(c), rel=1e-9)
            assert raw["mom_12_1_vol_adj"][s] == pytest.approx(_hand_mom(c) / _hand_vol(c, 250), rel=1e-9)
        assert raw["tsmom_12m"]["AAA"] < 0 < raw["tsmom_12m"]["CCC"]
    finally:
        conn.close()


def test_new_factors_are_registered_and_the_daily_set_moves_to_a_new_version(temp_db):
    from db.schema import get_connection, init_db
    for fid in ("tsmom_12m", "mom_12_1_vol_adj"):
        m = F.get(fid).meta()
        assert (m["category"], m["direction"], m["lookback"], m["inputs"], m["status"]) == \
            ("momentum", 1, 253, ["bars"], "ACTIVE")
        assert fid in F.BUILTIN_SET and fid not in [f.factor_id for f in F.FACTORS if f.category == "derivatives"]
    init_db()
    conn = get_connection()
    try:
        assert F.sync(conn) == len(F.FACTORS)
        old = [f for f in F.BUILTIN_SET if f not in ("tsmom_12m", "mom_12_1_vol_adj")]
        F.save_factor_set(conn, "atip_factors", "1", old, "pre-W39 daily set")
        s = F.ensure_builtin_set(conn)                       # @1 differs -> @2, not an error on every request
        assert s["version"] == "2" and "tsmom_12m@1" in s["factors"] and "mom_12_1_vol_adj@1" in s["factors"]
        assert F.ensure_builtin_set(conn)["version"] == "2"
        assert len(conn.execute("SELECT * FROM quant_factor_set WHERE name='atip_factors'").fetchall()) == 2
    finally:
        conn.close()


# ── PF-15: normalize.apply {"neutralize": [...]} ─────────────────────────────

def _cross_section(n=40, seed=5):
    rng = np.random.default_rng(seed)
    syms = [f"S{i:02d}" for i in range(n)]
    sec = {s: ("Banks", "IT", "Pharma", "Autos")[i % 4] for i, s in enumerate(syms)}
    beta = {s: float(x) for s, x in zip(syms, rng.normal(1.0, 0.3, n))}
    size = {s: float(x) for s, x in zip(syms, rng.normal(10.0, 1.5, n))}
    v = {s: 2.0 * beta[s] - 0.5 * size[s] + (1.0 if sec[s] == "IT" else 0.0) + float(e)
         for s, e in zip(syms, rng.normal(0, 1, n))}
    return syms, sec, beta, size, v


def test_neutralize_leaves_residuals_orthogonal_to_every_exposure():
    syms, sec, beta, size, v = _cross_section()
    spec = {"method": "raw", "winsorize": 0, "neutralize": ["beta_250", "size_log", "sector"]}
    out = NZ.apply(v, spec, sec, {"beta_250": beta, "size_log": size})
    r = np.array([out[s] for s in syms])
    inds = sorted(set(sec.values()))
    X = np.array([[1.0, beta[s], size[s]] + [1.0 if sec[s] == g else 0.0 for g in inds] for s in syms])
    assert np.abs(X.T @ r).max() < 1e-8                     # orthogonal to beta, size and EVERY sector dummy
    y = np.array([v[s] for s in syms])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    assert np.allclose(r, y - X @ coef, atol=1e-10)          # the OLS residual, by hand
    z = NZ.apply(v, {"method": "zscore", "winsorize": 0, "neutralize": ["beta_250", "size_log", "sector"]}, sec,
                 {"beta_250": beta, "size_log": size})
    assert z == NZ.zscore(out)                               # neutralize first, then the method


def test_sector_neutralize_demeans_within_each_sector_and_missing_stays_missing():
    syms, sec, beta, size, v = _cross_section()
    out = NZ.apply(v, {"method": "raw", "winsorize": 0, "neutralize": ["sector"]}, sec)
    for g in set(sec.values()):
        members = [s for s in syms if sec[s] == g]
        m = sum(v[s] for s in members) / len(members)
        for s in members:
            assert out[s] == pytest.approx(v[s] - m, abs=1e-12)
    beta2 = dict(beta, S00=None)
    v2 = dict(v, S01=None)
    out2 = NZ.apply(v2, {"method": "raw", "winsorize": 0, "neutralize": ["beta_250"]}, sec, {"beta_250": beta2})
    assert out2["S00"] is None and out2["S01"] is None and out2["S02"] is not None
    with pytest.raises(ValueError):
        NZ.apply(v, {"method": "raw", "neutralize": ["beta_250"]}, sec)          # no exposures passed
    with pytest.raises(ValueError):
        NZ.apply(v, {"method": "raw", "neutralize": [3]}, sec)
    plain = {"method": "percentile", "winsorize": 1.0}
    assert NZ.apply(v, plain, sec) == NZ.percentile(NZ.winsorize(v, 1.0))         # no key: unchanged


# ── PF-14: turnover penalty ──────────────────────────────────────────────

def test_zero_turnover_penalty_is_the_current_solver(monkeypatch):
    mu, S = _problem()
    ub, lb, groups, caps = _constraints()
    base = OPT._solve(mu, S, 4.0, ub, lb, groups, caps)
    w0 = np.full(8, 1 / 8)

    def boom(*a, **k):
        raise AssertionError("the plain solve must not reach the W39 proximal step")
    monkeypatch.setattr(OPT, "_prox", boom)
    for obj in ("mean_variance", "min_variance"):
        a = OPT._solve(mu, S, 4.0, ub, lb, groups, caps, obj=obj)
        b = OPT._solve(mu, S, 4.0, ub, lb, groups, caps, obj=obj, kappa=0.0, w_ref=w0)
        assert np.array_equal(a, b)
    assert np.array_equal(base, OPT._solve(mu, S, 4.0, ub, lb, groups, caps, kappa=0.0, w_ref=w0))


def _grid3(fun, ub, step=0.001):
    g = np.arange(0, 1 + 1e-12, step)
    W1, W2 = np.meshgrid(g, g, indexing="ij")
    W = np.stack([W1, W2, 1 - W1 - W2], -1)
    ok = ((W >= -1e-12) & (W <= ub + 1e-12)).all(-1)
    vals = np.where(ok, fun(W), np.inf)
    return float(vals.min())


def test_turnover_penalised_optimum_matches_brute_force():
    mu = np.array([0.18, 0.10, 0.06])
    _, S = _problem(3, seed=4)
    ub, lb = np.full(3, 0.6), np.zeros(3)
    w0 = np.array([0.2, 0.3, 0.5])
    for kappa in (0.0, 0.01, 0.04, 0.2):
        w = OPT._solve(mu, S, 4.0, ub, lb, {"A": np.arange(3)}, {}, kappa=kappa, w_ref=w0)
        obj = lambda W: 2.0 * np.einsum("...i,ij,...j->...", W, S, W) - W @ mu + kappa * np.abs(W - w0).sum(-1)
        assert abs(w.sum() - 1) < 1e-12 and (w >= 0).all() and (w <= 0.6 + 1e-12).all()
        assert obj(w) <= _grid3(obj, ub) + 1e-7            # no feasible grid point does better


def test_larger_penalty_cuts_turnover_and_a_huge_one_keeps_the_current_book():
    mu, S = _problem()
    ub, lb, groups, caps = _constraints()
    w0 = np.array([0.05, 0.25, 0.1, 0.2, 0.05, 0.15, 0.1, 0.1])   # feasible: <= 0.3 each, sectors <= 0.45
    assert abs(w0.sum() - 1) < 1e-12 and all(w0[i].sum() <= 0.45 for i in groups.values())
    def turnover(k):
        return float(np.abs(OPT._solve(mu, S, 4.0, ub, lb, groups, caps, kappa=k, w_ref=w0) - w0).sum())
    turn = [turnover(k) for k in (0.0, 0.025, 0.04, 0.06, 0.08)]
    assert all(a > b + 1e-3 for a, b in zip(turn, turn[1:])), turn         # strictly less at each step
    fine = [turnover(k) for k in np.linspace(0, 0.1, 21)]
    assert all(b <= a + 1e-7 for a, b in zip(fine, fine[1:])), fine        # never more along the way
    for k in (0.1, 50.0):
        w = OPT._solve(mu, S, 4.0, ub, lb, groups, caps, kappa=k, w_ref=w0)
        assert np.abs(w - w0).max() < 1e-9


# ── PF-15: optimiser neutral_to ──────────────────────────────────────────

def test_neutral_solve_meets_the_equalities_and_beats_every_feasible_grid_point():
    mu = np.array([0.16, 0.12, 0.09, 0.14])
    _, S = _problem(4, seed=8)
    b = np.array([1.2, -0.4, 0.5, -1.3])
    ub = np.full(4, 0.6)
    w = OPT._solve(mu, S, 4.0, ub, np.zeros(4), {"A": np.arange(4)}, {}, aff=(b[:, None], np.array([0.0])))
    assert abs(b @ w) < 1e-9 and abs(w.sum() - 1) < 1e-12 and (w >= 0).all() and (w <= 0.6 + 1e-12).all()
    # brute force: w0, w1 on a grid; w2, w3 from sum = 1 and b'w = 0
    g = np.arange(0, 0.6 + 1e-12, 0.002)
    A0, A1 = np.meshgrid(g, g, indexing="ij")
    rest, expo = 1 - A0 - A1, -(b[0] * A0 + b[1] * A1)
    W2 = (b[3] * rest - expo) / (b[3] - b[2])
    W = np.stack([A0, A1, W2, rest - W2], -1)
    ok = ((W >= -1e-12) & (W <= 0.6 + 1e-12)).all(-1)
    vals = np.where(ok, 2.0 * np.einsum("...i,ij,...j->...", W, S, W) - W @ mu, np.inf)
    assert 2.0 * w @ S @ w - w @ mu <= vals.min() + 1e-7


def test_neutral_solve_with_caps_and_turnover_respects_everything():
    mu, S = _problem()
    ub, lb, groups, caps = _constraints()
    rng = np.random.default_rng(2)
    B = rng.normal(0, 1, (8, 2))
    B -= B.mean(0)                                           # demeaned: equal weight is exactly neutral
    tgt = np.array([0.0, 0.1])
    for kappa in (0.0, 0.02):
        w = OPT._solve(mu, S, 4.0, ub, lb, groups, caps, kappa=kappa, w_ref=np.full(8, 1 / 8), aff=(B, tgt))
        assert np.abs(B.T @ w - tgt).max() < 1e-6
        assert abs(w.sum() - 1) < 1e-9 and (w >= -1e-12).all() and (w <= 0.3 + 1e-12).all()
        assert all(w[i].sum() <= 0.45 + 1e-9 for i in groups.values())
    free = OPT._solve(mu, S, 4.0, ub, lb, groups, caps)
    assert np.abs(B.T @ free - tgt).max() > 1e-3             # the constraint is doing something


# ── end to end: optimise() and POST /api/risk/optimize ───────────────────

@pytest.fixture
def market(temp_db, monkeypatch):
    """init_db + 260 sessions of NIFTY50 and eight synthetic names in four sectors."""
    from tests._wealth_seed import fresh
    conn, ds = fresh(temp_db)
    rng = np.random.default_rng(39)
    mkt = rng.normal(0.0004, 0.009, len(ds))
    for k, s in enumerate(SYMS):
        r = mkt * (0.6 + 0.1 * k) + rng.normal(0.0002 * (k % 3), 0.011, len(ds))
        for d, c in zip(ds, 100 * np.cumprod(1 + r)):
            conn.execute("INSERT OR REPLACE INTO prices_daily (symbol,date,open,high,low,close,volume) VALUES "
                         "(?,?,?,?,?,?,?)", (s, str(d), c, c, c, float(c), 1_000_000))
    conn.commit()
    import portfolio.risk as RK
    monkeypatch.setattr(RK, "_sectors", lambda: dict(SECTORS))
    yield conn
    conn.close()


def test_optimise_defaults_unchanged_and_new_options_reported(market):
    kw = dict(max_weight=0.3, sector_cap=0.45, store=False)
    plain = OPT.optimise(market, SYMS, "mean_variance", **kw)
    assert "turnover" not in plain and "neutrality" not in plain and "turnover_penalty" not in plain["inputs"]
    cur = {s: 1 / 8 for s in SYMS}
    same = OPT.optimise(market, SYMS, "mean_variance", turnover_penalty=0.0, current_weights=cur, **kw)
    assert same["weights"] == plain["weights"]
    held = OPT.optimise(market, SYMS, "mean_variance", turnover_penalty=0.05, current_weights=cur, **kw)
    assert held["turnover"]["l1_vs_current"] < same["turnover"]["l1_vs_current"]
    w = np.array([held["weights"].get(s, 0.0) for s in SYMS])
    assert held["turnover"]["l1_vs_current"] == pytest.approx(np.abs(w - 1 / 8).sum(), abs=1e-5)
    expo = {s: x for s, x in zip(SYMS, (1.3, 0.7, 1.1, 0.9, 0.6, 1.4, 1.0, 1.0))}       # mean 1.0
    neu = OPT.optimise(market, SYMS, "min_variance", neutral_to={"beta": expo}, neutral_targets={"beta": 1.0}, **kw)
    assert abs(neu["neutrality"]["beta"]["achieved"] - 1.0) < 1e-6
    assert sum(neu["weights"].values()) == pytest.approx(1.0, abs=1e-5)
    assert max(neu["weights"].values()) <= 0.3 + 1e-6 and max(neu["sector_weights"].values()) <= 0.45 + 1e-4
    assert abs(sum(expo[s] * x for s, x in neu["weights"].items()) - 1.0) < 1e-5      # rounded weights
    with pytest.raises(ValueError, match="infeasible"):
        OPT.optimise(market, SYMS, "min_variance", neutral_to={"beta": expo}, **kw)        # beta 0, long only
    with pytest.raises(ValueError):
        OPT.optimise(market, SYMS, "mean_variance", turnover_penalty=0.01, **kw)          # no current weights
    with pytest.raises(ValueError):
        OPT.optimise(market, SYMS, "min_variance", neutral_to={"beta": dict(expo, AAA=None)}, **kw)


def test_optimize_route_accepts_turnover_and_neutrality(market, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    from orders import rules
    import portfolio.risk as RK
    rules.init_orders_table()
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    client, h = TestClient(server.app), {security.TOKEN_HEADER: security.token()}
    expo = {s: x for s, x in zip(SYMS, (0.3, -0.3, 0.1, -0.1, 0.4, -0.4, 0.2, -0.2))}
    body = {"symbols": SYMS, "objective": "mean_variance", "max_weight": 0.3, "sector_cap": 0.45,
            "turnover_penalty": 0.02, "current_weights": {s.lower(): 0.125 for s in SYMS},
            "neutral_to": {"size": expo}}
    assert client.post("/api/risk/optimize", json=body).status_code == 401
    r = client.post("/api/risk/optimize", json=body, headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    assert abs(out["neutrality"]["size"]["achieved"]) < 1e-6 and out["turnover"]["penalty"] == 0.02
    assert out["inputs"]["current_weights"]["AAA"] == 0.125
    # no current_weights: the book's position weights (book_snapshot) are the reference
    monkeypatch.setattr(RK, "book_snapshot", lambda conn, book="PAPER", on=None: {
        "book": book, "positions": [{"symbol": "AAA", "weight": 0.6}, {"symbol": "CCC", "weight": 0.4}]})
    r = client.post("/api/risk/optimize", headers=h, json={k: v for k, v in body.items() if k != "current_weights"})
    assert r.status_code == 200, r.text
    assert r.json()["inputs"]["current_weights"] == {"AAA": 0.6, "CCC": 0.4}
    bad_bodies = ({"neutral_to": ["size"]},                                        # not {name: {symbol: x}}
                  {"neutral_to": {"size": dict(expo, AAA=5.0)}, "neutral_targets": {"size": 9}},   # unreachable
                  {"turnover_penalty": -1, "current_weights": {"AAA": 1}},
                  {"neutral_targets": {"x": 1}, "neutral_to": {}})
    for bad in bad_bodies:
        r = client.post("/api/risk/optimize", headers=h, json={**body, **bad})
        assert r.status_code == 400, (bad, r.text)
