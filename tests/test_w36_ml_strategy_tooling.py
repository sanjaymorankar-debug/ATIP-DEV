"""W36: deep learning (ML-02), RL (ML-04), assistant fallback (ML-16), strategy builder (SE-10),
derivatives factors (AF-06), widget schemas (API-06)."""

import math
from datetime import date, timedelta

import numpy as np
import pytest


@pytest.fixture
def db(temp_db):
    from db.schema import get_connection, init_db
    init_db()
    conn = get_connection()
    yield conn
    conn.close()


def _prices(conn, symbols, n=320):
    d0 = date.today() - timedelta(days=int(n * 1.5))
    ds, d = [], d0
    while len(ds) < n:
        if d.weekday() < 5:
            ds.append(d)
        d += timedelta(days=1)
    for k, s in enumerate(symbols):
        for i, d in enumerate(ds):
            c = 100 * (1 + 0.0004 * (k + 1)) ** i * (1 + 0.05 * math.sin(i / (7 + k)))
            conn.execute("INSERT OR REPLACE INTO prices_daily (symbol,date,open,high,low,close,volume) VALUES "
                         "(?,?,?,?,?,?,?)", (s, str(d), c, c * 1.01, c * 0.99, c, 100000))
    conn.commit()
    return ds


# ── ML-02 ──
def test_network_learns_a_nonlinear_target_and_round_trips():
    from ml.models import make_model
    rng = np.random.default_rng(0)
    X = rng.normal(size=(1200, 4))
    y = np.where(X[:, 0] * X[:, 1] > 0, "UP", "DOWN")
    nn = make_model("neural_network", "classification", {"epochs": 60}, ["a", "b", "c", "d"]).fit(X[:1000], list(y[:1000]))
    lr = make_model("logistic_regression", "classification", None, ["a", "b", "c", "d"]).fit(X[:1000], list(y[:1000]))
    acc = lambda m: np.mean(np.array(m.predict(X[1000:])) == y[1000:])
    assert acc(nn) > acc(lr) + 0.2
    clone = make_model("neural_network", "classification").load_state(nn.to_state())
    assert np.allclose(nn.predict_proba(X[:5]), clone.predict_proba(X[:5]))
    assert set(nn.importances()) == {"a", "b", "c", "d"}


def test_network_activation_needs_an_adoptable_benefit_check(db):
    from ml.deep import allowed_to_activate
    ok, why = allowed_to_activate(db, "DS1")
    assert not ok and "no deep-learning benefit check" in why
    db.execute("INSERT INTO ml_dl_benefit (check_id,dataset_id,verdict,reason,result_json,created_at) VALUES "
               "('a','DS1','NOT_ADOPTABLE','worse','{}','2026-01-01 10:00:00')")
    assert not allowed_to_activate(db, "DS1")[0]
    db.execute("INSERT INTO ml_dl_benefit (check_id,dataset_id,verdict,reason,result_json,created_at) VALUES "
               "('b','DS1','ADOPTABLE','better','{}','2026-01-02 10:00:00')")
    assert allowed_to_activate(db, "DS1")[0]


# ── ML-04 ──
def test_rl_trains_evaluates_out_of_sample_and_stores(db):
    ds = _prices(db, ["AAA", "BBB", "CCC"])
    split = ds[220]
    r = __import__("ml.rl", fromlist=["x"]).train_and_evaluate(db, ["AAA", "BBB", "CCC"], ds[60], ds[-1], split,
                                                                {"episodes": 5})
    assert r["verdict"] in ("BEATS_BASELINES", "NO_ADVANTAGE")
    oos = r["out_of_sample"]
    assert oos["agent"]["start"] > str(split) or oos["agent"]["start"] == str(ds[221])
    assert {"buy_and_hold", "trend"} <= set(oos) and len(r["policy_when_long"]) == 18
    assert db.execute("SELECT COUNT(*) FROM ml_rl_run").fetchone()[0] == 1
    with pytest.raises(ValueError):
        __import__("ml.rl", fromlist=["x"]).train_and_evaluate(db, ["AAA"], ds[-1], ds[0], ds[10])


# ── ML-16 ──
def test_assistant_falls_back_when_disabled_and_keeps_the_thread(db, monkeypatch):
    from ml import chat
    monkeypatch.setattr(chat, "settings", lambda: {**chat.DEFAULTS, "enabled": False})
    r = chat.ask(db, "What is the market doing?")
    assert r["mode"] == "deterministic" and "assistant.enabled is false" in r["fallback_reason"]
    r2 = chat.ask(db, "And now?", r["conversation_id"])
    assert r2["conversation_id"] == r["conversation_id"]
    assert len(chat.conversation(db, r["conversation_id"])["messages"]) == 2
    with pytest.raises(ValueError):
        chat.ask(db, "   ")


def test_assistant_tools_are_read_only():
    from ml import chat
    names = {t["name"] for t in chat.TOOLS}
    assert names == set(chat.HANDLERS)
    assert not any(w in n for n in names for w in ("order", "trade", "buy", "sell", "update", "delete", "set_"))


# ── SE-10 ──
SPEC = {"name": "Builder test", "entry": {"mode": "min", "k": 2, "conditions": [
    {"feature": "rsi_14", "op": "<", "value": 35}, {"feature": "atip_score", "op": ">=", "value": 60},
    {"feature": "close", "op": ">", "value_type": "feature", "value": "sma_200"}]},
    "exit": {"mode": "any", "conditions": [{"feature": "rsi_14", "op": ">", "value": 60}]}}


def test_builder_compiles_to_a_valid_rule_with_tunable_parameters():
    from strategy_engine.builder import compile_spec
    d = compile_spec(SPEC)
    assert d["kind"] == "rule" and d["entry"]["min"] == 2
    names = {p["name"] for p in d["parameters"]}
    assert {"rsi_14_max_1", "atip_score_min_2", "rsi_14_min_3", "position_pct", "stop_pct"} <= names


@pytest.mark.parametrize("bad", [
    {**SPEC, "entry": {"conditions": [{"feature": "no_such_feature", "op": "<", "value": 1}]}},
    {**SPEC, "entry": {"mode": "min", "k": 7, "conditions": SPEC["entry"]["conditions"]}},
    {**SPEC, "name": "x"},
    {**SPEC, "position": {"position_pct": 90}},
])
def test_builder_refuses_bad_specs(bad):
    from strategy_engine.builder import compile_spec
    with pytest.raises(ValueError):
        compile_spec(bad)


def test_builder_saves_a_draft_and_refuses_a_duplicate(db):
    from strategy_engine.builder import save
    r = save(db, SPEC)
    row = db.execute("SELECT status, source FROM strategy WHERE strategy_id=?", (r["strategy_id"],)).fetchone()
    assert tuple(row) == ("DRAFT", "builder")
    with pytest.raises(ValueError):
        save(db, SPEC)


# ── AF-06 ──
def test_derivatives_factors_point_in_time():
    from quant import factors as F
    base = date(2026, 9, 1)
    rows = [{"date": str(base + timedelta(days=i)), "underlying_price": 1000 + i, "fut_close": 1005 + i,
             "fut_oi": 1e6 * (1 + 0.01 * i), "pcr_oi": 0.8 + 0.005 * i, "atm_iv": 20 + (i % 10), "iv_skew": 2.5,
             "max_pain": 1000.0, "near_expiry": str(base + timedelta(days=i + 20))} for i in range(80)]
    ctx = F.FactorContext("X", base + timedelta(days=79), [], {}, deriv=rows)
    assert F.get("fut_basis_ann").fn(ctx) == pytest.approx((1084 / 1079 - 1) * 365 / 20 * 100)
    assert F.get("max_pain_gap").fn(ctx) == pytest.approx(7.9)
    assert F.get("iv_rank_252").fn(ctx) == pytest.approx(100.0)
    assert F.get("pcr_oi").fn(F.FactorContext("Y", base, [], {})) is None
    assert F.get("atm_iv").meta()["status"] == "ACTIVE"


# ── API-06 ──
def test_every_widget_schema_loads_and_the_validator_catches_drift():
    from dashboard import widget_schemas as W
    for w in W.WIDGETS:
        s = W.get(w)
        assert s["title"] and ("type" in s or "anyOf" in s)
    mh = W.get("market_health")
    good = {k: None for k in mh["required"]}
    assert W.validate(good, mh) == []
    assert any("missing required" in e for e in W.validate({}, mh))
    assert W.validate({"note": "x"}, W.get("news_brief")) == []
    assert W.validate({"headline": "h", "tone": "NOPE", "bullets": [], "classifier": "rule", "article_count": 1},
                      W.get("news_brief"))
