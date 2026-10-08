"""W39: unsupervised market regime (ML-17), research-to-production lineage (ML-18),
volatility surface + term structure (AF-10)."""

import json
import math
from datetime import date, datetime, timedelta

import numpy as np
import pytest


@pytest.fixture
def db(temp_db):
    from db.schema import get_connection, init_db
    init_db()
    conn = get_connection()
    yield conn
    conn.close()


@pytest.fixture
def ml_config(tmp_path, monkeypatch):
    """Point ml.config at a throwaway config.json; returns a writer for its "ml" section."""
    from ml import config
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", path)

    def write(ml=None):
        path.write_text(json.dumps({"ml": ml or {}}), encoding="utf-8")
    return write


def _planted(seed=0, blocks=(150, 100, 150, 100, 100), calm=(0.0008, 0.006), wild=(-0.0015, 0.02)):
    """Daily returns alternating a low-vol up regime (0) and a high-vol down regime (1)."""
    rng = np.random.default_rng(seed)
    r, s = [], []
    for i, n in enumerate(blocks):
        mu, sd = calm if i % 2 == 0 else wild
        r.append(rng.normal(mu, sd, n))
        s += [i % 2] * n
    return np.concatenate(r), np.array(s)


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")      # a full TIMESTAMP (sqlite converters parse it)


def _sessions(n, d0=date(2022, 1, 3)):
    out, d = [], d0
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _nifty(conn, returns, regimes, vix=True):
    c = 15000 * np.exp(np.cumsum(np.r_[0.0, returns]))
    ds = _sessions(len(c))
    for i, (d, x) in enumerate(zip(ds, c)):
        conn.execute("INSERT OR REPLACE INTO prices_daily (symbol,date,open,high,low,close,volume) VALUES "
                     "('NIFTY50',?,?,?,?,?,0)", (str(d), x, x, x, x))
        if vix:
            v = 12.0 if (regimes[i - 1] if i else 0) == 0 else 27.0
            conn.execute("INSERT OR REPLACE INTO prices_daily (symbol,date,open,high,low,close,volume) VALUES "
                         "('INDIAVIX',?,?,?,?,?,0)", (str(d), v, v, v, v))
    conn.commit()
    return ds


# ── ML-17 ──
def test_hmm_recovers_two_planted_regimes_with_monotone_em():
    from ml.hmm import GaussianHMM
    x, truth = _planted(0)
    m = GaussianHMM(2).fit(x)
    assert m.labels_ == ["CALM_BULL", "VOLATILE_BEAR"]          # ordered best first, labelled by return / vol
    pred = np.array([0 if m.labels_[k].startswith("CALM") else 1 for k in m.predict(x)])
    assert (pred == truth).mean() > 0.9
    h = np.array(m.history_)
    assert len(h) >= 3 and m.converged_
    assert np.all(np.diff(h) >= -1e-8)                          # EM never lowers the log-likelihood
    assert m.score(x) == pytest.approx(h[-1], rel=1e-9)
    assert m.state_vol_[0] < 0.01 < m.state_vol_[1]
    f = m.filtered(x)
    assert f.shape == (len(x), 2) and np.allclose(f.sum(1), 1)
    assert (np.where(m.filtered(x).argmax(1) == 0, 0, 1) == truth).mean() > 0.85
    with pytest.raises(ValueError):
        GaussianHMM(5)
    with pytest.raises(ValueError):
        GaussianHMM(2).fit(x[:10])


def test_hmm_is_deterministic():
    from ml.hmm import GaussianHMM
    x, _ = _planted(1)
    vol = np.r_[np.full(19, np.nan), np.lib.stride_tricks.sliding_window_view(x, 20).std(1)]
    X = np.column_stack([x, vol])[19:]
    a, b = GaussianHMM(3).fit(X), GaussianHMM(3).fit(X.copy())
    assert a.history_ == b.history_ and a.labels_ == b.labels_
    assert np.array_equal(a.means, b.means) and np.array_equal(a.transmat, b.transmat)
    assert np.array_equal(a.predict(X), b.predict(X)) and np.array_equal(a.filtered(X), b.filtered(X))
    assert len(a.summary()) == 3 and {"label", "regime", "vol_ann_pct", "expected_duration"} <= set(a.summary()[0])


def test_hmm_provider_is_point_in_time(db, ml_config):
    from ml.hmm import market_regime
    from ml.regime import HMMRegime, provider_from_config
    from strategy_engine.regime import get_provider
    x, truth = _planted(2, blocks=(200, 120, 200, 120, 160), calm=(0.001, 0.006), wild=(-0.003, 0.022))
    ds = _nifty(db, x, truth)
    ml_config({"regime_source": "hmm", "regime_hmm": {"n_states": 2, "refit_every": 5}})
    prov = provider_from_config(db)
    assert isinstance(prov, HMMRegime) and prov.name == "hmm" and isinstance(get_provider(db), HMMRegime)
    wild_day, calm_day = ds[1 + 200 + 120 + 200 + 60], ds[1 + 200 + 120 + 100]
    w, c = prov.on(wild_day), prov.on(calm_day)
    assert w["regime_source"] == "hmm" and w["regime_hmm"] == "VOLATILE_BEAR" and w["regime"] == "HIGH_RISK"
    assert w["market_trend"] == "uncertain" and w["regime_deterministic"] is None
    assert c["regime_hmm"] == "CALM_BULL" and c["regime"] == "BULL" and c["regime_hmm_confidence"] >= 0.5
    assert w["regime_hmm_fit_end"] <= str(wild_day)
    # rewrite everything AFTER as_of: the state for as_of must not move
    keys = ("regime", "regime_hmm", "regime_hmm_confidence", "regime_hmm_probabilities", "regime_hmm_fit_end")
    rng = np.random.default_rng(9)
    for d in ds:
        if d > wild_day:
            db.execute("UPDATE prices_daily SET close=close*? WHERE symbol IN ('NIFTY50','INDIAVIX') AND date=?",
                       (float(np.exp(rng.normal(0, 0.05))), str(d)))
    db.commit()
    again = provider_from_config(db).on(wild_day)
    assert {k: again[k] for k in keys} == {k: w[k] for k in keys}
    early = prov.on(ds[100])                                    # too little history: deterministic, said why
    assert early["regime_source"] == "deterministic" and early["regime_hmm_status"] == "INSUFFICIENT_HISTORY"
    r = market_regime(db, wild_day, n_states=2)
    assert r["status"] == "OK" and r["features"] == ["ret", "rv20", "vix"] and r["fit_end"] == str(wild_day)
    assert r["state"] == "VOLATILE_BEAR" and r["viterbi_state"] == "VOLATILE_BEAR" and len(r["states"]) == 2
    assert market_regime(db, ds[0])["status"] == "NO_DATA"


def test_default_config_leaves_the_regime_provider_unchanged(db, ml_config):
    from ml.config import settings
    from ml.regime import provider_from_config
    from strategy_engine.regime import MarketHealthRegime
    db.execute("INSERT INTO market_health (date, mh_score, regime, vix_level, pct_advancing) VALUES "
               "('2026-01-05', 72, 'BULL', 13.5, 61)")
    db.commit()
    for cfg in (None, {}, {"regime_source": "ml"}, {"regime_source": "nonsense"}):
        if cfg is not None:
            ml_config(cfg)
        p = provider_from_config(db)
        assert type(p) is MarketHealthRegime
        assert p.on(date(2026, 1, 5)) == MarketHealthRegime(db).on(date(2026, 1, 5))
    assert settings()["regime_source"] == "deterministic"


# ── ML-18 ──
def _registered_version(db, monkeypatch, code="abc1234+dirty"):
    from ml import registry as REG
    monkeypatch.setattr(REG, "code_version", lambda: code)
    db.execute("INSERT INTO ml_dataset (dataset_id,name,version,spec_json,spec_hash,feature_set,label_json,start_date,"
               "end_date,status,snapshot_path,snapshot_hash,created_at) VALUES ('ds_w39','ds','1','{}','spech',"
               "'fs_w39@1','{}','2024-01-01','2025-06-30','BUILT','snap.npz','snaphash123456',?)", (_now(),))
    db.execute("INSERT INTO ml_feature_set (name,version,features_json,feature_versions_json,content_hash,created_at) "
               "VALUES ('fs_w39','1','[\"rsi_14\",\"ret_5\"]','{}','fshash0987654',?)", (_now(),))
    db.commit()
    REG.create_model(db, "w39_model", "W39 model", "logistic_regression", "direction", "fs_w39@1")
    ds = dict(db.execute("SELECT * FROM ml_dataset WHERE dataset_id='ds_w39'").fetchone())
    fs = {"name": "fs_w39", "version": "1", "content_hash": "fshash0987654"}
    v = REG.start_version(db, "w39_model", ds, fs, {"model_type": "logistic_regression", "params": {}},
                          links={"training_run_id": "TRW39"})
    REG.finish_version(db, "w39_model", v, True, ("artifact.json", "artsha256"), {"note": "x"},
                       {"start": "2024-01-01", "end": "2025-03-31"})
    return v


def test_registry_records_code_version_and_lineage(db, ml_config, monkeypatch):
    from backtest import store
    from ml import registry as REG
    assert REG.code_version is store.code_version              # the backtest runs' helper, not a copy
    v = _registered_version(db, monkeypatch)
    row = REG.get_version(db, "w39_model", v)
    assert row["code_version"] == "abc1234+dirty"
    lin = row["lineage"]
    assert lin["code_version"] == "abc1234+dirty" and lin["training_run_id"] == "TRW39"
    assert lin["dataset"]["dataset_id"] == "ds_w39" and lin["dataset"]["snapshot_hash"] == "snaphash123456"
    assert lin["feature_set"] == {"id": "fs_w39@1", "content_hash": "fshash0987654"}
    assert lin["training_config_hash"] == row["training_config_hash"]
    assert lin["artifact"] == {"path": "artifact.json", "sha256": "artsha256"}
    # a strategy reading the model's ml_* features, with a completed backtest after the training data
    ml_config({"default_model": "w39_model"})
    now = _now()
    defn = json.dumps({"entry": {"all": [{"feature": "ml_score", "op": ">=", "value": 60}]}})
    db.execute("INSERT INTO strategy (strategy_id,name,kind,status,current_version,created_at,updated_at) VALUES "
               "('w39_ml','W39 ML','rule','DRAFT','1.0.0',?,?)", (now, now))
    db.execute("INSERT INTO strategy_version (strategy_id,version,definition_json,definition_hash,created_at) VALUES "
               "('w39_ml','1.0.0',?,'h',?)", (defn, now))
    db.execute("INSERT INTO backtest_run (run_id,strategy_id,strategy_version,start_date,end_date,config_json,"
               "code_version,status,finished_at) VALUES ('bt_w39','w39_ml','1.0.0','2025-07-01','2026-06-30','{}',"
               "'abc1234','COMPLETED',?)", (now,))
    db.commit()
    m = REG.lineage(db, "w39_model")                             # no ACTIVE version: the latest
    assert m["version"] == v and m["code_version"] == "abc1234+dirty"
    assert m["dataset"]["snapshot_matches"] and m["feature_set"]["matches"]
    assert m["feature_set"]["features"] == ["rsi_14", "ret_5"]
    assert m["artifact"]["sha256"] == "artsha256" and m["recorded"]["training_run_id"] == "TRW39"
    refs = m["references"]
    assert refs["config_roles"] == ["default_model"]
    assert [s["strategy_id"] for s in refs["strategies"]] == ["w39_ml"]
    assert refs["backtests"][0]["run_id"] == "bt_w39" and refs["backtests"][0]["after_training_end"] is True
    assert m["chain"][0] == f"model w39_model@{v}" and "code abc1234+dirty" in m["chain"]
    assert m["chain"][-1].startswith("backtest bt_w39")
    with pytest.raises(REG.ModelRegistryError, match="^no version"):
        REG.lineage(db, "w39_model", "v9")
    with pytest.raises(REG.ModelRegistryError, match="^no model"):
        REG.lineage(db, "nope_model")


def test_lineage_route(tmp_path, monkeypatch, db, ml_config):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    v = _registered_version(db, monkeypatch)
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    c = TestClient(server.app)
    r = c.get(f"/api/ml/models/w39_model/lineage?version={v}")
    assert r.status_code == 200 and r.json()["code_version"] == "abc1234+dirty"
    assert r.json()["dataset"]["dataset_id"] == "ds_w39"
    assert c.get("/api/ml/models/w39_model/lineage").status_code == 200
    assert c.get("/api/ml/models/w39_model/lineage?version=v7").status_code == 404
    assert c.get("/api/ml/models/no_such_model/lineage").status_code == 404


# ── AF-10 ──
S0, D0 = 20000.0, date(2026, 3, 2)
EXPIRIES = {D0 + timedelta(days=10): 14.0, D0 + timedelta(days=38): 16.0}     # expiry -> ATM IV %


def _smile(atm, k):
    return atm - 30.0 * k + 50.0 * k * k                       # put skew: higher IV at low strikes


def _chain(conn, iv=True):
    from data.derivatives import RISK_FREE
    from quant.derivatives import bs_price
    for exp, atm in EXPIRIES.items():
        T = (exp - D0).days / 365
        for K in np.arange(0.85, 1.1501, 0.01) * S0:
            K = round(float(K), 2)
            v = _smile(atm, math.log(K / S0))
            for ot, kind in (("CE", "call"), ("PE", "put")):
                conn.execute("INSERT INTO fo_contract_daily (date,symbol,instrument,expiry,strike,option_type,close,oi,"
                             "volume,underlying,iv) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (str(D0), "NIFTY", "IDO", str(exp), K, ot, bs_price(S0, K, T, RISK_FREE, v / 100, kind),
                              1000, 50, S0, round(v, 3) if iv else None))
        conn.execute("INSERT INTO fo_contract_daily (date,symbol,instrument,expiry,strike,option_type,close,"
                     "underlying) VALUES (?,?,?,?,0,'XX',?,?)", (str(D0), "NIFTY", "IDF", str(exp), S0 * 1.004, S0))
    conn.commit()


def test_vol_surface_reproduces_the_smile_term_structure_and_greeks(db):
    from data.derivatives import RISK_FREE
    from quant.derivatives import MONEYNESS_GRID, greeks, vol_surface
    _chain(db)
    s = vol_surface(db, "nifty")
    assert s["status"] == "OK" and s["as_of"] == str(D0) and s["underlying"] == S0 and s["risk_free"] == RISK_FREE
    near, far = sorted(EXPIRIES)
    ts = s["term_structure"]
    assert [t["expiry"] for t in ts] == [str(near), str(far)] and [t["dte"] for t in ts] == [10, 38]
    assert [t["atm_iv"] for t in ts] == pytest.approx([14.0, 16.0], abs=1e-3)
    assert s["term_shape"] == "CONTANGO" and s["term_slope"] == pytest.approx(2.0, abs=1e-3)
    for t, atm in zip(ts, (14.0, 16.0)):
        assert t["skew_95_105"] > 0 and t["rr_25d"] < 0       # put skew
        assert t["skew_95_105"] == pytest.approx(_smile(atm, math.log(0.95)) - _smile(atm, math.log(1.05)), abs=0.01)
    grid = s["surface"]
    assert grid["moneyness"] == list(MONEYNESS_GRID) and grid["dte"] == [10, 38]
    row = grid["iv"][0]
    assert row[0] is None and row[-1] is None                    # 0.80 / 1.20: outside the stored strikes
    assert row[MONEYNESS_GRID.index(1.0)] == pytest.approx(14.0, abs=1e-3)
    assert row[MONEYNESS_GRID.index(0.9)] == pytest.approx(_smile(14.0, math.log(0.9)), abs=0.01)
    k = next(x for x in s["expiries"][0]["strikes"] if x["strike"] == 21000.0)
    g = greeks(S0, 21000.0, 10 / 365, RISK_FREE, k["call"]["iv"] / 100, "call")
    assert {n: k["call"][n] for n in g} == pytest.approx(g)
    gp = greeks(S0, 21000.0, 10 / 365, RISK_FREE, k["put"]["iv"] / 100, "put")
    assert k["put"]["delta"] == pytest.approx(gp["delta"]) and k["put"]["delta"] < 0
    assert k["smile_iv"] == k["call"]["iv"]                      # above the underlying: the call side


def test_vol_surface_says_when_data_is_missing(db):
    from quant.derivatives import vol_surface
    assert vol_surface(db, "NIFTY")["status"] == "NO_DATA"
    _chain(db, iv=False)
    assert vol_surface(db, "NOSUCH")["status"] == "NO_DATA"
    assert vol_surface(db, "NIFTY", "2026-01-01")["status"] == "NO_DATA"
    r = vol_surface(db, "NIFTY")
    assert r["status"] == "NO_IV" and r["reason"]


def test_surface_route(tmp_path, monkeypatch, db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    _chain(db)
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    c = TestClient(server.app)
    r = c.get(f"/api/data/fo/surface?symbol=NIFTY&date={D0}")
    assert r.status_code == 200 and r.json()["term_structure"][0]["atm_iv"] == pytest.approx(14.0, abs=1e-3)
    assert c.get("/api/data/fo/surface?symbol=NIFTY").status_code == 200
    miss = c.get("/api/data/fo/surface?symbol=NOSUCH")
    assert miss.status_code == 404 and miss.json()["error"].startswith("NO_DATA")
    assert c.get("/api/data/fo/surface?symbol=NIFTY&date=2026-01-01").status_code == 404
    assert c.get("/api/data/fo/surface?symbol=NIFTY&date=not-a-date").status_code == 400
