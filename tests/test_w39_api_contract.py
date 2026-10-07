"""W39 (API-05): the public API v1 contract declares a response schema for EVERY resource, and the
real handlers return bodies that conform to it.

enterprise/public_api.py RESPONSE_SCHEMAS maps each of the 23 v1 (method, path) pairs to the JSON
Schema of its 200 body (a widget resource reuses its WIDGETS schema; the others $ref a named
schema in SCHEMAS). These tests build a seeded database -- every list resource returns at least
one real row, produced wherever practical by the code that produces it in production (risk engine,
paper OMS fill, quant engine, lifecycle transitions, notifications, workspace saves) -- call every
GET resource through /api/v1 with an API key holding exactly the scopes RESOURCES names, call each
POST resource once, and validate every body with public_api.validate (dependency-free). An empty
list would pass any items schema, so each resource is also asserted non-empty.

Seeding notes:
  * commit before alerts.telegram.notify: it writes alert_log on its own connection, and an open
    write transaction on the fixture's connection would make it wait out the 60 s busy timeout;
  * the paper broker prices a fill from a live quote (data.dhan) -- PaperBroker._ltp is replaced,
    as tests/test_paper_broker.py does, so the OMS order really FILLs and /api/oms/fills has a row;
  * ml_prediction rows are written with ml/predict.py's own INSERT column list (a trained artifact
    is out of scope here), the model itself through ml.registry.create_model.
"""

import json
from datetime import date, datetime, timedelta

import pytest

PASSWORD = "Correct-Horse-Battery-9!"
SID, VER = "atip_zpi_momentum", "1.0.0"          # a shipped library strategy (strategy_engine/library)


def _v1(path):
    return "/api/v1" + path[len("/api"):].replace("{strategy_id}", SID)


def _seed(conn, last, uid):
    from alerts.telegram import fmt, notify
    from enterprise import notifications, reports, workspace
    from execution import order_manager as OM, risk_engine as RE
    from ml import registry as MREG
    from orders.paper import ensure_tables as paper_tables
    from pipeline.health import EXPECTATIONS
    from pipeline.recover import ensure_table as recovery_table
    from quant import engine as QE
    from strategy_engine import lifecycle as L, registry as REG

    now = datetime.now()
    # /api/scores, /api/tod: the seeded ACME score row (tests/_wealth_seed.py) is the trade of the day
    conn.execute("UPDATE ai_scores SET is_tod=1, tod_score=81.5, atip_rank=1, confidence=0.72, acs=55.0, mri=48.0, "
                 "rri=52.0 WHERE symbol='ACME' AND date=?", (last,))
    conn.execute("INSERT INTO technical_indicators (symbol,date,rsi_14,adx_14,atr_14,atr_pct,volume_ratio) VALUES "
                 "('ACME',?,58.2,24.0,2.5,2.1,1.3)", (last,))
    conn.commit()
    # /api/alerts: an alert, a failing job (3 FAILED runs, pipeline/health.py) and a re-run today
    notify(fmt("🩺", "Job Missed: News", "no run"), category="job_health", key="health:contract")
    for i in range(3):
        t = now - timedelta(hours=3 - i)
        conn.execute("INSERT INTO pipeline_log (run_date, job_name, start_time, end_time, status, kind, error_msg) "
                     "VALUES (?,?,?,?,?,?,?)", (str(t.date()), EXPECTATIONS[0].jobs[0], t, t, "FAILED", "run", "boom"))
    recovery_table(conn)
    conn.execute("INSERT INTO job_recovery (day,step,attempts,last_at,last_result,problems) VALUES (?,?,?,?,?,?)",
                 (str(date.today()), "news", 1, now, "RAN", "MISSED News (morning)"))
    conn.commit()
    # strategies: the library, one strategy taken through its lifecycle with backtests, health, a run
    REG.sync_library(conn)
    for run_id, label in (("BT_FULL", "full"), ("BT_TEST", "test")):
        conn.execute("INSERT INTO backtest_run (run_id,kind,strategy_id,strategy_version,period_label,start_date,"
                     "end_date,initial_capital,config_json,metrics_json,status,created_at,finished_at) VALUES "
                     "(?,?,?,?,?,?,?,?,?,?,?,?,?)", (run_id, "single", SID, VER, label, "2025-01-01", "2025-12-31",
                                                     1000000.0, "{}", json.dumps({"sharpe": 1.4, "trades": 40}),
                                                     "COMPLETED", now, now))
    conn.commit()
    for st in ("BACKTEST", "VALIDATION", "APPROVED", "PAPER"):
        L.transition(conn, SID, st, "contract test")
    conn.execute("INSERT INTO strategy_health (strategy_id,version,as_of,status,metrics_json,issues_json,created_at) "
                 "VALUES (?,?,?,?,?,?,?)", (SID, VER, last, "WARNING", json.dumps({"status_reason": "drawdown"}),
                                           json.dumps([{"code": "DRAWDOWN"}]), now))
    # a decision + intent, written with strategy_engine/engine.py's column lists
    conn.execute("INSERT INTO strategy_decision_run (run_id,strategy_id,version,as_of,book,status,params_json,"
                 "n_universe,n_evaluated,counts_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 ("RUN1", SID, VER, last, "PAPER", "SUCCESS", "{}", 3, 3, json.dumps({"ENTER": 1}), now))
    conn.execute("INSERT INTO strategy_decision (decision_id,run_id,strategy_id,version,as_of,timestamp,symbol,"
                 "decision,action,confidence,score,regime,reasons_json,parameters_json,risk_requirement,"
                 "target_position_pct,stop_price,target_price,max_hold_sessions,blocked_reason,features_json,"
                 "reason_codes_json,signal_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("D1", "RUN1", SID, VER, last, now, "ACME", "BUY", "ENTER", 0.7, 72.0, "BULL",
                  json.dumps(["zpi 60 >= 55"]), json.dumps({"zpi_min": 55}), "STANDARD", 5.0, 105.0, 125.0, 10,
                  None, json.dumps({"close": 113.0, "zpi": 60.0}), json.dumps(["ENTRY_RULES_MET"]), "rules"))
    conn.execute("INSERT INTO strategy_position_intent (intent_id, decision_id, strategy_id, version, as_of, symbol, "
                 "side, action, quantity, stop_price, entry_reference, book) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("I1", "D1", SID, VER, last, "ACME", "BUY", "ENTER", 10, 105.0, 113.0, "PAPER"))
    paper_tables(conn)
    conn.execute("INSERT OR REPLACE INTO paper_account (id, balance, opened_at) VALUES (1,?,?)", (1000000.0, str(now)))
    conn.commit()
    # the W4 chain for real: risk decision -> PAPER order -> fill
    rd = RE.evaluate(conn, "I1")
    assert rd.risk_status == "APPROVED", rd.rejection_reason
    o = OM.submit_order(conn, OM.create_order(conn, rd.risk_decision_id)["order_id"])
    assert o["status"] == "FILLED", o.get("reason")
    # ML: a registered model and one stored prediction
    MREG.create_model(conn, "contract_model", "Contract model", "logistic_regression", "direction", "atip_technical@1")
    conn.execute("INSERT INTO ml_prediction (prediction_id,model_id,model_version,version_status,symbol,as_of,"
                 "prediction,prediction_value,probabilities_json,confidence,prob_up,ml_score,interval_low,"
                 "interval_high,feature_set,feature_set_hash,artifact_hash,explanation_json,features_json,created_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("PR1", "contract_model", "v1", "ACTIVE", "ACME", last, "UP", 0.64,
                  json.dumps({"DOWN": 0.36, "UP": 0.64}), 0.64, 0.64, 64.0, None, None, "atip_technical@1", "fsh",
                  "arth", json.dumps({"method": "linear_contribution", "explanation_version": "1",
                                      "top_features": ["rsi_14"], "feature_contributions": {"rsi_14": 0.2},
                                      "model_reason_codes": ["ML_POS_rsi_14"]}), json.dumps({"rsi_14": 58.2}), now))
    conn.commit()
    # quant: the engine computes two price-only factors over the seeded symbols
    res = QE.compute(conn, last, ["mom_3m", "vol_20"], universe=["ACME", "GOLDBEES", "LIQUIDBEES", "NIFTYMIDCAP150"])
    assert {f["status"] for f in res["factors"].values()} == {"OK"}
    # notifications: one read, two unread
    notifications.notify(conn, "default", "risk", "Risk decision APPROVED", "ACME BUY 10", user_ids=[uid])
    notifications.mark_read(conn, uid, None)
    notifications.notify(conn, "default", "system", "Welcome", "", user_ids=[uid])
    notifications.notify(conn, "default", "alert", "ACME rsi_14 > 50", "58.2", user_ids=[uid], severity="warning")
    # workspace: a shared watchlist, an alert rule, a report that has been run once
    workspace.save_watchlist(conn, "default", uid, "Banks", ["hdfcbank", "ICICIBANK"], shared=True)
    workspace.save_alert(conn, "default", uid, "ACME momentum", "acme", "rsi_14", ">", 60)
    rep = workspace.save_report(conn, "default", uid, "Backtests", "backtest_summary", {"top": 5})
    reports.run(conn, rep["report_id"], "html", uid)
    return rep["report_id"]


@pytest.fixture
def v1(temp_db, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    from enterprise import apikeys, service, users
    from enterprise import config as ecfg
    from enterprise.public_api import RESOURCES
    from execution import config as XC
    from orders import paper, risk
    from tests._wealth_seed import fresh

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"enterprise": {"enabled": True},
                               "execution": {"order_type": "MARKET", "max_market_data_age_sessions": None},
                               "w4_risk_limits": {"max_sector_exposure_pct": None, "daily_loss_limit_pct": None,
                                                  "portfolio_drawdown_limit_pct": None,
                                                  "strategy_drawdown_limit_pct": None}}), encoding="utf-8")
    for mod in (ecfg, security, XC, risk):
        monkeypatch.setattr(mod, "CONFIG_PATH", cfg)
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(risk, "HALT_FLAG", tmp_path / "TRADING_HALTED")
    monkeypatch.setattr(paper.PaperBroker, "_ltp", lambda self, symbol: 113.2)
    conn, sessions = fresh(temp_db)
    last = str(sessions[-1])
    service.ensure_seeded(conn)
    # STRATEGY_MANAGER holds all nine v1 permissions; the key is scoped to exactly those
    u = users.create_user(conn, "api.contract", PASSWORD, "default", ["STRATEGY_MANAGER"])
    k = apikeys.create(conn, u["user_id"], "default", "contract", sorted({p for _, _, p, _, _ in RESOURCES}))
    report_id = _seed(conn, last, u["user_id"])
    yield {"client": TestClient(server.app), "conn": conn, "user": u, "h": {"Authorization": f"ApiKey {k['api_key']}"},
           "cfg": cfg, "report_id": report_id}
    conn.close()


def _check(body, method, path):
    from enterprise.public_api import RESPONSE_SCHEMAS, validate
    errs = validate(body, RESPONSE_SCHEMAS[(method, path)])
    assert errs == [], f"{method} {path} does not match its declared schema:\n" + "\n".join(errs[:20])


# ── the contract itself ───────────────────────────────────────────────────────────────────────

def test_every_v1_resource_declares_a_well_formed_response_schema():
    pytest.importorskip("fastapi")
    from dashboard import server
    from enterprise import public_api as PA
    keys = [(m, p) for m, p, *_ in PA.RESOURCES]
    assert len(keys) == 23 and set(PA.RESPONSE_SCHEMAS) == set(keys)
    for name, schema in PA.COMPONENTS.items():
        assert PA.check_schema(schema, path=name) == []
    for key, schema in PA.RESPONSE_SCHEMAS.items():
        assert PA.check_schema(schema, path=str(key)) == []
    spec = PA.openapi_v1(server.app)
    assert spec["x-atip-unresolved"] == []
    comps = spec["components"]["schemas"]
    assert PA._refs(spec) <= set(comps), "every $ref in openapi-v1.json resolves (incl. FastAPI's 422 schema)"
    for m, p, _, _, widget in PA.RESOURCES:
        op = spec["paths"][p[len("/api"):]][m.lower()]
        schema = op["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema == PA.RESPONSE_SCHEMAS[(m, p)] and schema != {}
        if widget:                                         # widget resources keep their published $ref
            assert schema == {"$ref": f"#/components/schemas/widget_{widget}"}
        for code in ("401", "403", "429", "4XX", "5XX"):
            assert op["responses"][code]["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/Error"}
    assert '"additionalProperties": false' not in json.dumps(PA.COMPONENTS)


def test_every_get_resource_returns_real_rows_that_match_its_schema(v1):
    from enterprise.public_api import RESOURCES, RESPONSE_SCHEMAS, validate
    client, h = v1["client"], v1["h"]
    seen, drift = {}, []
    for method, path, *_ in RESOURCES:
        if method != "GET":
            continue
        r = client.get(_v1(path), headers=h)
        assert r.status_code == 200, (path, r.status_code, r.text[:300])
        assert r.headers.get("api-version") == "v1"
        body = r.json()
        assert body, f"{path} returned an empty body -- the contract check would be vacuous"
        drift += [f"GET {path} {e}" for e in validate(body, RESPONSE_SCHEMAS[(method, path)])]
        seen[path] = body
    assert drift == [], "handlers that do not match their declared schema:\n" + "\n".join(drift[:40])
    assert len(seen) == 20
    # the nested parts of the schemas were exercised with real values, not only nulls / []
    z = next(s for s in seen["/api/strategies"] if s["strategy_id"] == SID)
    assert z["latest_backtest"]["metrics"]["sharpe"] == 1.4 and z["health"]["status"] == "WARNING"
    assert z["latest_run"]["top"][0]["symbol"] == "ACME"
    d = seen["/api/strategies/{strategy_id}"]
    assert d["status"] == "PAPER" and d["definition"]["strategy_id"] == SID and d["versions"] and d["backtests"]
    assert [e["to_state"] for e in d["lifecycle"]][:1] == ["PAPER"] and "READY" in d["allowed_transitions"]
    a = seen["/api/alerts"]
    assert a["job_health"][0]["kind"] == "FAILING" and a["recovered_today"][0]["step"] == "news" and a["alerts"]
    tod = seen["/api/tod"]
    assert tod["symbol"] == "ACME" and tod["sl"] == round(tod["cmp"] - 1.5 * 2.5, 2)
    assert seen["/api/risk/decisions"][0]["risk_status"] == "APPROVED" and seen["/api/risk/decisions"][0]["risk_checks"]
    assert seen["/api/oms/orders"][0]["status"] == "FILLED" and seen["/api/oms/fills"][0]["quantity"] == 10
    assert seen["/api/tenant/book"]["positions"][0]["symbol"] == "ACME"
    assert {n["read_at"] is None for n in seen["/api/account/notifications"]} == {True, False}
    assert {f["status"] for f in seen["/api/quant/factors"]} == {"ACTIVE", "DATA_PENDING"}
    assert seen["/api/account/watchlists"][0]["symbols"] == ["HDFCBANK", "ICICIBANK"]
    assert seen["/api/account/alerts"][0]["feature"] == "rsi_14"
    assert seen["/api/account/reports"][0]["params"] == {"top": 5} and seen["/api/account/reports"][0]["last_run_at"]


def test_v1_list_pagination_keeps_the_declared_shape(v1):
    client, h = v1["client"], v1["h"]
    r = client.get("/api/v1/quant/factors?page=2&page_size=5&sort=-lookback", headers=h)
    assert r.status_code == 200 and len(r.json()) == 5 and int(r.headers["x-total-count"]) > 5
    _check(r.json(), "GET", "/api/quant/factors")


def test_each_post_resource_returns_a_body_that_matches_its_schema(v1):
    client, h = v1["client"], v1["h"]
    r = client.post("/api/v1/account/watchlists", headers=h, json={"name": "Core", "symbols": ["acme", "TCS"]})
    assert r.status_code == 200 and r.json()["symbols"] == ["ACME", "TCS"]
    _check(r.json(), "POST", "/api/account/watchlists")
    r = client.post("/api/v1/account/alerts", headers=h, json={"symbol": "acme", "feature": "rsi_14", "op": ">",
                                                               "value": 50})
    assert r.status_code == 200 and r.json()["symbol"] == "ACME" and r.json()["status"] == "ACTIVE"
    _check(r.json(), "POST", "/api/account/alerts")
    r = client.post(f"/api/v1/reports/{v1['report_id']}/run", headers=h, json={"format": "json"})
    assert r.status_code == 200 and r.json()["report_id"] == v1["report_id"] and r.json()["rows"] == 2
    _check(r.json(), "POST", "/api/reports/{report_id}/run")
    # what the POSTs created reads back through the GET resources, still on contract
    for path in ("/api/account/watchlists", "/api/account/alerts"):
        body = client.get(_v1(path), headers=h).json()
        assert len(body) == 2
        _check(body, "GET", path)


def test_a_tenants_own_paper_book_matches_the_tenant_book_widget(v1):
    """The owner's book is portfolio.pnl.portfolio_summary; any other tenant gets execution/tenant_books.book
    (which adds tenant_id and starting_cash) -- both shapes are the one widget schema."""
    from enterprise import apikeys, tenants, users
    from execution import tenant_books as TB
    client, conn = v1["client"], v1["conn"]
    tenants.create(conn, "acme_fund", "Acme Fund")
    t = users.create_user(conn, "acme.viewer", PASSWORD, "acme_fund", ["VIEWER"])
    k = apikeys.create(conn, t["user_id"], "acme_fund", "book", ["execution:read"])
    TB.apply_fill(conn, "acme_fund", "OX1", "ACME", "BUY", 5, 113.0)
    r = client.get("/api/v1/tenant/book", headers={"Authorization": f"ApiKey {k['api_key']}"})
    assert r.status_code == 200 and r.json()["tenant_id"] == "acme_fund" and r.json()["starting_cash"]
    assert r.json()["positions"][0]["qty"] == 5
    _check(r.json(), "GET", "/api/tenant/book")


def test_error_bodies_match_the_error_schema(v1):
    from enterprise import apikeys
    from enterprise.public_api import validate
    client, h = v1["client"], v1["h"]
    err = {"$ref": "#/components/schemas/Error"}
    cases = [(client.get("/api/v1/scores"), 401),                                              # no credentials
             (client.get("/api/v1/strategies/no_such_strategy", headers=h), 404),             # {"error": "..."}
             (client.post("/api/v1/reports/no_such_report/run", headers=h, json={}), 404),
             (client.post("/api/v1/account/alerts", headers=h, json={"feature": "zzz"}), 400),
             (client.post("/api/v1/account/watchlists", headers={**h, "Content-Type": "text/plain"}, content="x"),
              415)]                                                                            # the W8 envelope
    for r, code in cases:
        assert r.status_code == code, (code, r.text)
        assert validate(r.json(), err) == [], r.text
    assert isinstance(cases[-1][0].json()["error"], dict) and isinstance(cases[1][0].json()["error"], str)
    narrow = apikeys.create(v1["conn"], v1["user"]["user_id"], "default", "narrow", ["strategy:read"])
    r = client.get("/api/v1/oms/orders", headers={"Authorization": f"ApiKey {narrow['api_key']}"})
    assert r.status_code == 403 and validate(r.json(), err) == []                    # a scope the key lacks
    # a query parameter of the wrong type is FastAPI's own 422, documented as HTTPValidationError
    r = client.get("/api/v1/backtests?limit=abc", headers=h)
    assert r.status_code == 422 and "detail" in r.json() and "error" not in r.json()


def test_with_the_enterprise_layer_off_reads_keep_their_shapes_and_account_routes_answer_503(v1):
    """Single-owner local mode (enterprise.enabled false): reads need no credentials and return the
    same shapes; the account resources answer 503 with an Error body."""
    from enterprise.public_api import RESOURCES, validate
    v1["cfg"].write_text(json.dumps({"enterprise": {"enabled": False}}), encoding="utf-8")
    client = v1["client"]
    for method, path, *_ in RESOURCES:
        if method != "GET":
            continue
        r = client.get(_v1(path))
        if path.startswith("/api/account/"):
            assert r.status_code == 503 and validate(r.json(), {"$ref": "#/components/schemas/Error"}) == []
            continue
        assert r.status_code == 200 and r.json(), (path, r.text[:300])
        _check(r.json(), method, path)


# ── the validator ─────────────────────────────────────────────────────────────────────────────

def test_validator_reports_type_mismatch_missing_required_nullable_and_nested_items():
    from enterprise.public_api import validate
    row = {"type": "object", "required": ["id", "qty"], "properties": {
        "id": {"type": "string"}, "qty": {"type": "integer"}, "px": {"type": ["number", "null"]},
        "tags": {"type": "array", "items": {"type": "string"}},
        "legs": {"type": "array", "items": {"type": "object", "required": ["side"], "properties": {
            "side": {"type": "string", "enum": ["BUY", "SELL"]}, "fill": {"type": ["number", "null"]}}}}}}
    schema = {"type": "array", "items": row}
    ok = [{"id": "A", "qty": 3, "px": None, "tags": ["x"], "legs": [{"side": "BUY", "fill": 1}], "extra": {"any": 1}}]
    assert validate(ok, schema) == []                                       # null allowed; unknown fields allowed
    assert validate([{"id": 1, "qty": 3}], schema) == ["$[0].id: expected string, got integer"]
    assert validate([{"id": "A"}], schema) == ["$[0]: missing required property 'qty'"]
    assert validate([{"id": "A", "qty": 2.5}], schema) == ["$[0].qty: expected integer, got number"]
    assert validate([{"id": "A", "qty": True}], schema) == ["$[0].qty: expected integer, got boolean"]
    assert validate([{"id": "A", "qty": 1, "px": "1.5"}], schema) == ["$[0].px: expected number or null, got string"]
    assert validate([{"id": "A", "qty": 1, "tags": ["x", 2]}], schema) == ["$[0].tags[1]: expected string, got integer"]
    bad = [{"id": "A", "qty": 1, "legs": [{"side": "BUY"}, {"fill": None}, {"side": "HOLD"}]}]
    assert validate(bad, schema) == ["$[0].legs[1]: missing required property 'side'",
                                     "$[0].legs[2].side: 'HOLD' is not one of ['BUY', 'SELL']"]
    assert validate({"id": "A"}, schema) == ["$: expected array, got object"]
    assert validate(None, {"type": ["object", "null"], "required": ["x"]}) == []      # required: objects only


def test_validator_resolves_refs_and_refuses_what_it_cannot_check():
    from enterprise import public_api as PA
    comps = {"Leg": {"type": "object", "required": ["side"], "properties": {"side": {"type": "string"}}}}
    s = {"type": "array", "items": {"$ref": "#/components/schemas/Leg"}}
    assert PA.validate([{"side": "BUY"}], s, comps) == []
    assert PA.validate([{}], s, comps) == ["$[0]: missing required property 'side'"]
    with pytest.raises(ValueError, match="cannot resolve"):
        PA.validate([{}], {"type": "array", "items": {"$ref": "#/components/schemas/Nope"}}, comps)
    with pytest.raises(ValueError, match="not supported"):
        PA.validate({}, {"type": "object", "additionalProperties": False})
    with pytest.raises(ValueError, match="unsupported JSON Schema type"):
        PA.validate(1, {"type": "int"})
    # the shipped Error schema accepts both error styles and rejects a half envelope
    err = {"$ref": "#/components/schemas/Error"}
    assert PA.validate({"error": "no strategy x"}, err) == []
    assert PA.validate({"error": {"code": "GONE", "message": "retired", "request_id": None, "retryable": False}},
                       err) == []
    assert PA.validate({"error": {"message": "m"}}, err) == ["$.error: missing required property 'code'"]
    assert PA.validate({"detail": "x"}, err) == ["$: missing required property 'error'"]
    # check_schema: the static rules the shipped schemas are held to
    assert PA.check_schema({"type": "object", "additionalProperties": False}) == [
        "$: additionalProperties false (response objects may gain fields)",
        "$: unsupported keyword(s) ['additionalProperties']"]
    assert PA.check_schema({"type": "object", "required": ["a"], "properties": {}}) == [
        "$: required ['a'] not in properties"]
    assert PA.check_schema({"$ref": "#/components/schemas/Nope"}) == [
        "$: cannot resolve $ref '#/components/schemas/Nope'"]


def test_validator_catches_a_handler_drifting_from_its_schema(v1):
    """A real body with one field changed the way a careless handler change would change it."""
    from enterprise.public_api import RESPONSE_SCHEMAS, validate
    body = v1["client"].get("/api/v1/oms/orders", headers=v1["h"]).json()
    body[0]["quantity"] = "10"                                  # int -> str: a breaking type change
    del body[0]["status"]                                       # a removed field
    assert validate(body, RESPONSE_SCHEMAS[("GET", "/api/oms/orders")]) == [
        "$[0]: missing required property 'status'", "$[0].quantity: expected integer, got string"]
