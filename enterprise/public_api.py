"""
The public API v1 contract (W9, API-03 / API-05 / API-06).

V1 is the frozen PUBLIC surface: the resources below, served at /api/v1/<path> (the W8
alias of /api/<path>), authenticated with an API key (Authorization: ApiKey atk_...),
a Bearer session token, or the browser session cookie. Each resource needs one
permission (enterprise/authz.py ROUTE_RULES); an API key's scopes must include it.

    openapi_v1(app)        OpenAPI 3.1 document for exactly these resources, paths under /api/v1
    write_docs(app, dir)   docs/api/openapi-v1.json + docs/api/widgets/<name>.schema.json
    WIDGETS                JSON Schemas for dashboard widgets (API-06)

Limits per API key (W9): enterprise_api_key.rate_limit_per_minute (default
saas.api.key_rate_per_minute, 60) and daily_quota (default saas.api.key_daily_quota,
10,000), plus the tenant plan's max_api_calls_per_day -> 429 with Retry-After.
Versioning policy (docs/API_VERSIONING_POLICY.md): additive changes (new fields / resources)
keep v1; a breaking change ships as /api/v2 while v1 keeps working for at least 6 months.
Unversioned /api/... called with an API key answers with Deprecation: true and a Link to the
/api/v1 successor.

Deprecating one v1 resource (W39, API-03): an entry in DEPRECATIONS. From its `deprecated`
date every response carries
    Deprecation: @<unix time of that date>        (RFC 9745)
    Sunset: <HTTP date of `sunset`>                (RFC 8594)
    Link: <successor>; rel="successor-version", </docs/API_VERSIONING_POLICY.md>; rel="deprecation"
the OpenAPI operation is marked deprecated (with x-sunset / x-successor), and from the sunset
date the resource answers 410 GONE with the standard error envelope. validate_deprecations()
(run by the tests) refuses an entry whose notice is under MIN_NOTICE_DAYS.

Response schemas (W39, API-05): every v1 resource declares its 200 body.
    RESPONSE_SCHEMAS       (method, path) -> JSON Schema of the 200 body; a widget resource reuses
                           its WIDGETS schema, the others $ref a named schema in SCHEMAS
    SCHEMAS                named schemas (components/schemas in openapi-v1.json), incl. Error
    COMPONENTS             WIDGETS as widget_<name> + SCHEMAS: what a "$ref" resolves against
    validate(body, schema) -> [error strings]; dependency-free, the subset these schemas use
                           (type incl. a list of types, properties, required, items, enum, $ref)
    check_schema(schema)   -> [problems]: unsupported keywords, unresolvable $ref, a required
                           name not in properties, additionalProperties false
Response objects may gain fields (additive, docs/API_VERSIONING_POLICY.md), so no schema sets
additionalProperties false; a field the code can return as None is typed ["<type>", "null"].
Errors (4xx / 5xx): {"error": "..."} (W1-W7 routes) or the W8 envelope {"error": {code, message,
request_id, retryable}} -- schema Error accepts both. A query parameter of the wrong type is
rejected by FastAPI itself with 422 {"detail": [...]} (HTTPValidationError), not the envelope.
The contract test (tests/test_w39_api_contract.py) calls every resource on a seeded database
and validates the real body.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

VERSION = "1.0.0"
MIN_NOTICE_DAYS = 180           # deprecated -> sunset: at least 6 months

# (method, path under /api as in RESOURCES) -> {"deprecated": "YYYY-MM-DD", "sunset": "YYYY-MM-DD",
#                                               "successor": "/api/v2/..." | None, "reason": "..."}
# Nothing in v1 is deprecated yet.
DEPRECATIONS: dict = {}

# (method, path under /api, permission, summary, widget)
RESOURCES = [
    ("GET", "/api/scores", "dashboard:read", "ATIP scores for the latest scored session", "scores_table"),
    ("GET", "/api/mh", "dashboard:read", "Market health", "market_health"),
    ("GET", "/api/tod", "dashboard:read", "Top opportunities of the day", "top_of_day"),
    ("GET", "/api/alerts", "dashboard:read", "Job health and recent platform alerts", "alerts"),
    ("GET", "/api/strategies", "strategy:read", "Strategies of the caller's tenant", None),
    ("GET", "/api/strategies/{strategy_id}", "strategy:read", "One strategy", None),
    ("GET", "/api/strategy-decisions", "strategy:read", "Strategy decisions", None),
    ("GET", "/api/backtests", "research:read", "Backtest runs", None),
    ("GET", "/api/ml/models", "ml:read", "ML models", None),
    ("GET", "/api/ml/predictions", "ml:read", "ML predictions", None),
    ("GET", "/api/quant/factors", "quant:read", "Quant factor catalog", None),
    ("GET", "/api/quant/scores", "quant:read", "Latest factor scores", "factor_scores"),
    ("GET", "/api/risk/decisions", "risk:read", "Risk decisions", None),
    ("GET", "/api/oms/orders", "execution:read", "Orders (read-only in the public API)", None),
    ("GET", "/api/oms/fills", "execution:read", "Fills", None),
    ("GET", "/api/tenant/book", "execution:read", "The caller's tenant paper book", "tenant_book"),
    ("GET", "/api/account/notifications", "notifications:read", "The caller's notifications", "notifications"),
    ("GET", "/api/account/watchlists", "workspace:write", "The caller's watchlists", "watchlists"),
    ("POST", "/api/account/watchlists", "workspace:write", "Create / update a watchlist", None),
    ("GET", "/api/account/alerts", "workspace:write", "The caller's alert rules", None),
    ("POST", "/api/account/alerts", "workspace:write", "Create / update an alert rule", None),
    ("GET", "/api/account/reports", "workspace:write", "The caller's reports", None),
    ("POST", "/api/reports/{report_id}/run", "workspace:write", "Render a report", None),
]

_NUM = {"type": ["number", "null"]}
_STR = {"type": ["string", "null"]}
WIDGETS = {
    "scores_table": {"type": "array", "items": {"type": "object", "required": ["symbol"], "properties": {
        "symbol": {"type": "string"}, "acs": _NUM, "vpi": _NUM, "mri": _NUM, "rri": _NUM, "zpi": _NUM, "cri": _NUM,
        "signal": _STR}}},
    "market_health": {"type": "object", "properties": {"date": _STR, "mh_score": _NUM, "regime": _STR,
                                                        "nifty_close": _NUM, "breadth": _NUM}},
    "top_of_day": {"type": ["array", "object"]},
    "alerts": {"type": "object", "properties": {"job_health": {"type": ["array", "object"]},
                                                 "alerts": {"type": "array"}}},
    "factor_scores": {"type": "array", "items": {"type": "object", "properties": {
        "symbol": {"type": "string"}, "factor_key": _STR, "score": _NUM, "rank": _NUM}}},
    "tenant_book": {"type": "object", "required": ["equity", "cash", "positions"], "properties": {
        "tenant_id": _STR, "equity": _NUM, "cash": _NUM, "positions_value": _NUM, "realised_cum": _NUM,
        "positions": {"type": "array", "items": {"type": "object", "properties": {
            "symbol": {"type": "string"}, "qty": {"type": "integer"}, "avg_price": _NUM, "mark": _NUM, "value": _NUM,
            "unrealised": _NUM}}}}},
    "notifications": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "integer"}, "category": _STR, "severity": _STR, "title": _STR, "body": _STR,
        "created_at": _STR, "read_at": _STR}}},
    "watchlists": {"type": "array", "items": {"type": "object", "properties": {
        "watchlist_id": {"type": "string"}, "name": _STR, "symbols": {"type": "array", "items": {"type": "string"}},
        "shared": {"type": ["boolean", "integer"]}}}},
}

# ── response schemas (W39, API-05) ─────────────────────────────────────────────────────────────
_INT = {"type": ["integer", "null"]}
_OBJ = {"type": ["object", "null"]}
_S = {"type": "string"}
_I = {"type": "integer"}
_N = {"type": "number"}
_STRS = {"type": "array", "items": _S}


def _o(props, required=(), nullable=False, description=None) -> dict:
    return {"type": ["object", "null"] if nullable else "object",
            **({"description": description} if description else {}),
            **({"required": list(required)} if required else {}), "properties": props}


def _ref(name) -> dict:
    return {"$ref": f"#/components/schemas/{name}"}


def _list_of(name) -> dict:
    return {"type": "array", "items": _ref(name)}


def _add_fields(node, **props) -> None:
    """Add the rest of a widget endpoint's real fields. A field published above is frozen (v1):
    its type and the required list are never touched here -- this refuses to redefine one."""
    have = node.setdefault("properties", {})
    clash = sorted(set(props) & set(have))
    if clash:
        raise ValueError(f"widget fields {clash} are already published; change them only by the versioning policy")
    have.update(props)


# ai_scores (+ the dashboard's joins), shared by /api/scores rows and /api/tod
_AI_SCORES = {"id": _I, "date": _S, "spi": _NUM, "msi": _NUM, "tech_score": _NUM, "fund_score": _NUM,
              "inst_score": _NUM, "news_score": _NUM, "atip_score": _NUM, "atip_rank": _NUM, "confidence": _NUM,
              "beta_1y": _NUM, "tod_score": _NUM, "is_tod": _INT, "mh_score": _NUM, "regime": _STR,
              "top_factor_1": _STR, "top_factor_2": _STR, "top_factor_3": _STR, "created_at": _STR,
              "cmp": {**_NUM, "description": "close on the scored session"},
              "prev_close": {**_NUM, "description": "close of the session before"}}
_add_fields(WIDGETS["scores_table"]["items"], **_AI_SCORES, rsi_14=_NUM, adx_14=_NUM, atr_pct=_NUM, volume_ratio=_NUM)
_add_fields(WIDGETS["market_health"], id=_I, nifty_trend=_NUM, banknifty=_NUM, vix_score=_NUM, fii_score=_NUM,
            dii_score=_NUM, global_score=_NUM, sector_score=_NUM, adv_decline=_NUM, vix_level=_NUM, created_at=_STR,
            advances=_INT, declines=_INT, pct_advancing=_NUM, new_highs=_INT, new_lows=_INT, breadth_universe=_INT,
            mh_coverage=_NUM, mh_inputs=_STR, backfilled=_INT, portfolio_health=_NUM)
WIDGETS["market_health"]["description"] = "The scored session's market_health row; {} when there is none."
_add_fields(WIDGETS["top_of_day"], symbol=_S, acs=_NUM, vpi=_NUM, mri=_NUM, rri=_NUM, zpi=_NUM, cri=_NUM,
            signal=_STR, **_AI_SCORES, atr_14=_NUM,
            sl=_NUM, t1=_NUM, t2=_NUM)
WIDGETS["top_of_day"]["description"] = ("The scored session's trade-of-the-day ai_scores row (an object; {} when there "
                                        "is none). sl / t1 / t2 = cmp -1.5 / +2.0 / +3.5 x atr_14, only when both "
                                        "are known.")
WIDGETS["alerts"]["properties"]["job_health"]["items"] = _o({"label": _S, "kind": _S, "detail": _S},
                                                            ["label", "kind", "detail"])
WIDGETS["alerts"]["properties"]["alerts"]["items"] = _o({
    "created_at": _S, "category": _S, "severity": _S, "title": _STR, "telegram_sent": _I, "telegram_error": _STR})
_add_fields(WIDGETS["alerts"], recovered_today={"type": "array", "items": _o({
    "step": _S, "what": _S, "attempts": _I, "last_at": _S, "result": _STR, "problems": _STR})})
_add_fields(WIDGETS["factor_scores"]["items"], as_of=_S, kind=_S, raw=_NUM, norm=_NUM, pct=_NUM, sector=_STR,
            sector_rank=_NUM, universe_size=_INT, created_at=_STR)
_add_fields(WIDGETS["tenant_book"], env=_S, as_of=_S, n_positions=_I, unvalued=_STRS, cost=_NUM, unrealised=_NUM,
            starting_cash={**_NUM, "description": "a tenant's own book only (absent on the owner's book)"})
_add_fields(WIDGETS["tenant_book"]["properties"]["positions"]["items"], cost=_NUM)
_add_fields(WIDGETS["notifications"]["items"], tenant_id=_STR, user_id=_S)
_add_fields(WIDGETS["watchlists"]["items"], tenant_id=_S, user_id=_S, created_at=_STR, updated_at=_STR)

_STRATEGY = {"strategy_id": _S, "name": _S, "description": _STR, "kind": _S, "category": _STR, "status": _S,
             "current_version": _STR, "source": _STR, "owner": _STR, "priority": _INT, "weight": _NUM,
             "created_at": _STR, "updated_at": _STR, "activated_at": _STR, "tenant_id": _STR}
_STRATEGY_REQ = ["strategy_id", "name", "kind", "status"]
_HEALTH = _o({"version": _S, "as_of": _S, "status": _S, "issues": {"type": "array"}, "status_reason": _STR},
             ["status"], nullable=True, description="the latest strategy_health row; null when none")
_EVENT = _o({"id": _I, "strategy_id": _STR, "version": _STR, "event_type": _S, "from_state": _STR, "to_state": _STR,
             "message": _STR, "details": {"type": "object"}, "actor": _STR, "at": _STR}, ["id", "event_type"])

SCHEMAS = {
    "Error": {"type": "object", "required": ["error"], "description":
              "W1-W7 routes answer {\"error\": \"message\"}; newer failures the W8 envelope "
              "{\"error\": {code, message, request_id, retryable}} (codes in ops/errors.py).",
              "properties": {"error": {"type": ["string", "object"], "required": ["code", "message"], "properties": {
                  "code": _S, "message": _S, "request_id": _STR, "retryable": {"type": "boolean"}}}}},
    "Strategy": _o({**_STRATEGY,
                    "latest_backtest": _o({"run_id": _S, "period_label": _STR, "status": _S, "finished_at": _STR,
                                           "metrics": _OBJ}, ["run_id", "status"], nullable=True,
                                          description="the newest backtest of the current version"),
                    "health": _HEALTH,
                    "latest_run": _o({"as_of": _STR, "status": _S, "error": _STR, "counts": {"type": "object"},
                                      "top": {"type": "array", "items": _o({"symbol": _S, "decision": _S,
                                                                            "score": _NUM})}},
                                     ["status", "counts", "top"], nullable=True,
                                     description="the newest decision run of the current version")},
                   _STRATEGY_REQ + ["latest_backtest", "health", "latest_run"]),
    "StrategyDetail": _o({**_STRATEGY, "definition": _OBJ,
                          "versions": {"type": "array", "items": _o({
                              "strategy_id": _S, "version": _S, "definition_hash": _S, "notes": _STR,
                              "created_at": _STR}, ["version"])},
                          "lifecycle": {"type": "array", "items": _EVENT}, "events": {"type": "array", "items": _EVENT},
                          "allowed_transitions": _STRS, "health": _HEALTH,
                          "backtests": {"type": "array", "items": _o({
                              "run_id": _S, "strategy_version": _STR, "kind": _S, "period_label": _STR,
                              "start_date": _STR, "end_date": _STR, "status": _S,
                              "metrics_json": {**_STR, "description": "the metrics as stored: JSON text, not decoded"},
                              "created_at": _STR}, ["run_id", "status"])}},
                         _STRATEGY_REQ + ["definition", "versions", "lifecycle", "events", "allowed_transitions",
                                          "health", "backtests"]),
    "StrategyDecision": _o({"decision_id": _S, "run_id": _STR, "strategy_id": _S, "version": _S, "as_of": _S,
                            "timestamp": _STR, "symbol": _S, "decision": _S, "action": _S, "confidence": _NUM,
                            "score": _NUM, "regime": _STR, "risk_requirement": _STR, "target_position_pct": _NUM,
                            "stop_price": _NUM, "target_price": _NUM, "max_hold_sessions": _INT,
                            "blocked_reason": _STR, "signal_source": _STR, "reasons": {"type": ["array", "null"]},
                            "parameters": _OBJ, "features": _OBJ, "reason_codes": {"type": ["array", "null"]}},
                           ["decision_id", "strategy_id", "version", "as_of", "symbol", "decision", "action"]),
    "BacktestRun": _o({"run_id": _S, "parent_run_id": _STR, "kind": _S, "window_index": _INT, "strategy_id": _S,
                       "strategy_version": _STR, "period_label": _STR, "start_date": _STR, "end_date": _STR,
                       "initial_capital": _NUM, "status": _S, "created_at": _STR, "finished_at": _STR,
                       "metrics": _OBJ}, ["run_id", "kind", "strategy_id", "status"]),
    "MlModel": _o({"model_id": _S, "name": _S, "description": _STR, "model_type": _S, "task": _S, "label_kind": _S,
                   "feature_set": _S, "purpose": _S, "status": _S, "active_version": _STR, "owner": _STR,
                   "created_at": _STR, "updated_at": _STR, "tenant_id": _STR},
                  ["model_id", "name", "model_type", "task", "label_kind", "feature_set", "purpose", "status"]),
    "MlPrediction": _o({"prediction_id": _S, "model_id": _S, "model_version": _S, "version_status": _STR,
                        "symbol": _S, "as_of": _S, "prediction": _STR, "prediction_value": _NUM, "confidence": _NUM,
                        "prob_up": _NUM, "ml_score": _NUM, "interval_low": _NUM, "interval_high": _NUM,
                        "feature_set": _STR, "feature_set_hash": _STR, "artifact_hash": _STR, "created_at": _STR,
                        "probabilities": _OBJ, "explanation": _OBJ},
                       ["prediction_id", "model_id", "model_version", "symbol", "as_of"]),
    "QuantFactor": _o({"factor_id": _S, "version": _S, "name": _S, "category": _S, "description": _S, "formula": _S,
                       "inputs": _STRS, "lookback": _I, "frequency": _S, "normalization": {"type": "object"},
                       "direction": _I, "data_dependency": _STR, "cross_sectional": {"type": "boolean"},
                       "status": {**_S, "description": "e.g. ACTIVE, DATA_PENDING (needs data_dependency)"}},
                      ["factor_id", "version", "name", "category", "lookback", "status"]),
    "RiskDecision": _o({"risk_decision_id": _S, "intent_id": _S, "decision_id": _STR, "strategy_id": _STR,
                        "strategy_version": _STR, "symbol": _S, "side": _STR, "action": _STR, "book": _STR,
                        "mode": _STR, "requested_quantity": _INT, "approved_quantity": _INT,
                        "reference_price": _NUM, "est_value": _NUM, "equity": _NUM,
                        "risk_status": {**_S, "description": "e.g. APPROVED, REJECTED, BLOCKED, REVIEW_REQUIRED"},
                        "rejection_reason": _STR, "engine_version": _STR, "reviewed_by": _STR, "reviewed_at": _STR,
                        "created_at": _STR, "tenant_id": _STR,
                        "risk_checks": {"type": "array", "items": _o({
                            "check": _S, "status": {**_S, "description": "e.g. PASS, WARN, FAIL, SKIP"},
                            "message": _STR, "value": _NUM, "limit": _NUM}, ["check", "status"])}},
                       ["risk_decision_id", "intent_id", "symbol", "risk_status", "risk_checks"]),
    "OmsOrder": _o({"order_id": _S, "intent_id": _S, "risk_decision_id": _S, "decision_id": _STR, "strategy_id": _STR,
                    "strategy_version": _STR, "symbol": _S, "side": _S, "quantity": _I, "order_type": _S,
                    "limit_price": _NUM, "product_type": _STR, "mode": _S, "adapter": _STR, "status": _S,
                    "broker_order_id": _STR, "filled_quantity": _I, "avg_fill_price": _NUM, "fees": _N,
                    "reference_price": _NUM, "reason": _STR, "created_at": _STR, "updated_at": _STR,
                    "tenant_id": _STR, "trigger_price": _NUM, "parent_order_id": _STR, "modified_count": _INT,
                    "instrument": _STR, "algo_parent_id": _STR, "algo_slice": _INT},
                   ["order_id", "intent_id", "risk_decision_id", "symbol", "side", "quantity", "order_type", "mode",
                    "status", "filled_quantity", "fees"]),
    "OmsFill": _o({"fill_id": _S, "order_id": _S, "execution_id": _STR, "strategy_id": _STR, "strategy_version": _STR,
                   "symbol": _S, "side": _S, "quantity": _I, "price": _N, "fees": _N, "price_source": _STR,
                   "mode": _STR, "filled_at": _STR},
                  ["fill_id", "order_id", "symbol", "side", "quantity", "price", "fees"]),
    "Watchlist": WIDGETS["watchlists"]["items"],
    "AlertRule": _o({"rule_id": _S, "tenant_id": _S, "user_id": _S, "name": _STR, "symbol": _STR, "feature": _STR,
                     "op": _STR, "value": _NUM, "status": _STR, "last_value": _NUM, "last_triggered_at": _STR,
                     "created_at": _STR}, ["rule_id", "tenant_id", "user_id"]),
    "Report": _o({"report_id": _S, "tenant_id": _S, "user_id": _S, "name": _STR, "kind": _STR,
                  "params": {"description": "the params given when the report was saved (decoded JSON)"},
                  "shared": {"type": ["boolean", "integer"]}, "created_at": _STR, "schedule": _STR,
                  "formats_json": {**_STR, "description": "the scheduled formats as stored: JSON text, not decoded"},
                  "last_run_at": _STR}, ["report_id", "tenant_id", "user_id"]),
    "ReportRun": _o({"output_id": _S, "report_id": _S, "format": _S, "rows": _I},
                    ["output_id", "report_id", "format", "rows"]),
}
COMPONENTS = {**{f"widget_{k}": v for k, v in WIDGETS.items()}, **SCHEMAS}

_NON_WIDGET = {
    ("GET", "/api/strategies"): _list_of("Strategy"),
    ("GET", "/api/strategies/{strategy_id}"): _ref("StrategyDetail"),
    ("GET", "/api/strategy-decisions"): _list_of("StrategyDecision"),
    ("GET", "/api/backtests"): _list_of("BacktestRun"),
    ("GET", "/api/ml/models"): _list_of("MlModel"),
    ("GET", "/api/ml/predictions"): _list_of("MlPrediction"),
    ("GET", "/api/quant/factors"): _list_of("QuantFactor"),
    ("GET", "/api/risk/decisions"): _list_of("RiskDecision"),
    ("GET", "/api/oms/orders"): _list_of("OmsOrder"),
    ("GET", "/api/oms/fills"): _list_of("OmsFill"),
    ("POST", "/api/account/watchlists"): _ref("Watchlist"),
    ("GET", "/api/account/alerts"): _list_of("AlertRule"),
    ("POST", "/api/account/alerts"): _ref("AlertRule"),
    ("GET", "/api/account/reports"): _list_of("Report"),
    ("POST", "/api/reports/{report_id}/run"): _ref("ReportRun"),
}
RESPONSE_SCHEMAS = {(m, p): (_ref(f"widget_{w}") if w else _NON_WIDGET[(m, p)]) for m, p, _, _, w in RESOURCES
                    if w or (m, p) in _NON_WIDGET}

# ── validation (no third-party jsonschema) ────────────────────────────────────────────────────
_REF = "#/components/schemas/"
_ASSERTIONS = {"type", "properties", "required", "items", "enum", "$ref"}
_ANNOTATIONS = {"$schema", "$id", "title", "description", "default", "examples", "format", "deprecated"}
_PY = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _type_ok(v, t) -> bool:
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t not in _PY:
        raise ValueError(f"unsupported JSON Schema type {t!r}")
    return isinstance(v, _PY[t])


def _json_type(v) -> str:
    for t in ("null", "boolean", "integer", "number", "string", "array", "object"):
        if _type_ok(v, t):
            return t
    return type(v).__name__


def _resolve(ref, comps, path):
    name = ref[len(_REF):] if isinstance(ref, str) and ref.startswith(_REF) else None
    if name not in comps:
        raise ValueError(f"{path}: cannot resolve $ref {ref!r}")
    return comps[name]


def validate(instance, schema: dict, components: dict | None = None, path: str = "$") -> list:
    """Errors of `instance` (decoded JSON) against `schema`, as "<path>: <problem>" strings; [] = valid.
    Supports type (a name or a list of names), properties, required, items, enum and
    "$ref": "#/components/schemas/<name>" (resolved in `components`, default COMPONENTS).
    Annotations (description, title, ...) are ignored; any other keyword raises ValueError, so a
    schema can never claim a rule this validator would silently skip."""
    comps = COMPONENTS if components is None else components
    unknown = set(schema) - _ASSERTIONS - _ANNOTATIONS
    if unknown:
        raise ValueError(f"{path}: schema keyword(s) {sorted(unknown)} not supported by public_api.validate")
    if "$ref" in schema:
        return validate(instance, _resolve(schema["$ref"], comps, path), comps, path)
    if "type" in schema:
        ts = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_ok(instance, t) for t in ts):
            return [f"{path}: expected {' or '.join(ts)}, got {_json_type(instance)}"]
    errs = []
    if "enum" in schema and not any(_json_type(instance) == _json_type(e) and instance == e for e in schema["enum"]):
        errs.append(f"{path}: {instance!r} is not one of {schema['enum']}")
    if isinstance(instance, dict):
        errs += [f"{path}: missing required property '{k}'" for k in schema.get("required", []) if k not in instance]
        for k, sub in schema.get("properties", {}).items():
            if k in instance:
                errs += validate(instance[k], sub, comps, f"{path}.{k}")
    elif isinstance(instance, list) and "items" in schema:
        for i, v in enumerate(instance):
            errs += validate(v, schema["items"], comps, f"{path}[{i}]")
    return errs


def check_schema(schema: dict, components: dict | None = None, path: str = "$") -> list:
    """Static problems of a schema (empty = fine): keywords validate() does not support,
    unknown type names, an unresolvable $ref, a required name missing from properties, and
    additionalProperties false (v1 response objects may gain fields)."""
    comps = COMPONENTS if components is None else components
    if not isinstance(schema, dict):
        return [f"{path}: a schema must be an object"]
    out = []
    if schema.get("additionalProperties") is False:
        out.append(f"{path}: additionalProperties false (response objects may gain fields)")
    unknown = set(schema) - _ASSERTIONS - _ANNOTATIONS
    if unknown:
        out.append(f"{path}: unsupported keyword(s) {sorted(unknown)}")
    if "$ref" in schema:
        try:
            _resolve(schema["$ref"], comps, path)
        except ValueError as e:
            out.append(str(e))
    for t in (schema["type"] if isinstance(schema.get("type"), list) else [schema.get("type")]):
        if t is not None and t not in _PY and t not in ("integer", "number"):
            out.append(f"{path}: unknown type {t!r}")
    missing = set(schema.get("required", [])) - set(schema.get("properties", {}))
    if missing:
        out.append(f"{path}: required {sorted(missing)} not in properties")
    for k, sub in schema.get("properties", {}).items():
        out += check_schema(sub, comps, f"{path}.{k}")
    if "items" in schema:
        out += check_schema(schema["items"], comps, f"{path}[]")
    return out


def _match(app_path, res_path):
    """Same path, ignoring path-parameter names ({sid} == {strategy_id})."""
    import re
    norm = lambda x: re.sub(r"\{[^}]+\}", "{}", x)
    return norm(app_path) == norm(res_path)


def _day(v):
    from datetime import date
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def validate_deprecations(deps=None) -> list:
    """Problems with the registry (empty = fine): unknown resource, bad dates, notice under
    MIN_NOTICE_DAYS, missing reason."""
    deps = DEPRECATIONS if deps is None else deps
    known = {(m, p) for m, p, *_ in RESOURCES}
    out = []
    for key, d in deps.items():
        if key not in known:
            out.append(f"{key}: not a v1 resource")
            continue
        try:
            a, b = _day(d["deprecated"]), _day(d["sunset"])
        except Exception:
            out.append(f"{key}: deprecated / sunset must be YYYY-MM-DD dates")
            continue
        if (b - a).days < MIN_NOTICE_DAYS:
            out.append(f"{key}: sunset {b} is {(b - a).days} days after deprecation; the policy is >= "
                       f"{MIN_NOTICE_DAYS}")
        if not str(d.get("reason") or "").strip():
            out.append(f"{key}: give a reason")
    return out


def _concrete(template, path) -> bool:
    import re
    rx = "^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(template)) + "$"
    return re.match(rx, path) is not None


def deprecation_for(method, path, today=None, deps=None) -> dict | None:
    """The DEPRECATIONS entry for a request (path under /api, i.e. after the /api/v1 alias), with
    "headers" (list of (name, value)) and "gone" (past the sunset), or None when the resource is
    not deprecated or its deprecation date has not come."""
    from datetime import date, datetime, time, timezone
    from email.utils import format_datetime
    deps = DEPRECATIONS if deps is None else deps
    if not deps:
        return None
    today = today or date.today()
    m = str(method).upper()
    for (dm, dp), d in deps.items():
        if dm != m or not _concrete(dp, path):
            continue
        a, b = _day(d["deprecated"]), _day(d["sunset"])
        if today < a:
            return None
        ts = int(datetime.combine(a, time(0), tzinfo=timezone.utc).timestamp())
        links = ([f'<{d["successor"]}>; rel="successor-version"'] if d.get("successor") else []) + \
            ['</docs/API_VERSIONING_POLICY.md>; rel="deprecation"']
        return {**d, "resource": f"{dm} {dp}", "gone": today >= b,
                "headers": [("deprecation", f"@{ts}"),
                            ("sunset", format_datetime(datetime.combine(b, time(0), tzinfo=timezone.utc), usegmt=True)),
                            ("link", ", ".join(links))]}
    return None


def openapi_v1(app) -> dict:
    full = app.openapi()
    out = {"openapi": "3.1.0",
           "info": {"title": "ATIP public API", "version": VERSION,
                    "description": "Frozen v1 contract. Not financial advice; ATIP is not SEBI registered. "
                                   "Errors: {\"error\": {code, message, request_id, retryable}} (W8)."},
           "servers": [{"url": "/api/v1"}],
           "components": {"securitySchemes": {
               "apiKey": {"type": "apiKey", "in": "header", "name": "Authorization",
                          "description": "ApiKey atk_<id>_<secret>"},
               "bearer": {"type": "http", "scheme": "bearer"},
               "cookie": {"type": "apiKey", "in": "cookie", "name": "atip_session"}},
               "schemas": copy.deepcopy(COMPONENTS)},
           "security": [{"apiKey": []}, {"bearer": []}, {"cookie": []}],
           "paths": {}}
    missing = []
    err = {"application/json": {"schema": _ref("Error")}}
    for method, path, perm, summary, widget in RESOURCES:
        app_path = next((ap for ap in full.get("paths", {}) if _match(ap, path)), path)
        op = (full.get("paths", {}).get(app_path) or {}).get(method.lower())
        if op is None:
            missing.append(f"{method} {path}")
            op = {}
        op = copy.deepcopy(op)
        op["summary"] = summary
        op["x-atip-permission"] = perm
        op.setdefault("parameters", [])
        op["parameters"] += [{"name": "Idempotency-Key", "in": "header", "required": False, "schema":
                              {"type": "string", "maxLength": 64}}] if method != "GET" else [
            {"name": "page", "in": "query", "schema": {"type": "integer", "minimum": 1}},
            {"name": "page_size", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 500}},
            {"name": "sort", "in": "query", "schema": {"type": "string"}}]
        schema = RESPONSE_SCHEMAS.get((method, path))
        if schema is None:
            missing.append(f"{method} {path}: no response schema")
        else:
            op.setdefault("responses", {}).setdefault("200", {"description": "Successful Response"})["content"] = {
                "application/json": {"schema": copy.deepcopy(schema)}}
        op.setdefault("responses", {}).update({
            "401": {"description": "UNAUTHENTICATED", "content": err},
            "403": {"description": "PERMISSION_DENIED / scope missing", "content": err},
            "429": {"description": "RATE_LIMITED (per key, per tenant plan)", "content": err},
            "4XX": {"description": "other client errors: 400 bad request / VALIDATION_FAILED, 404 not found, "
                                   "409 CONFLICT, 413, 415", "content": err},
            "5XX": {"description": "INTERNAL / DEPENDENCY_UNAVAILABLE / DEPENDENCY_TIMEOUT; 503 for the account "
                                   "resources while the enterprise layer is off", "content": err}})
        dep = DEPRECATIONS.get((method, path))
        if dep:
            op.update({"deprecated": True, "x-deprecated-on": str(dep["deprecated"]), "x-sunset": str(dep["sunset"]),
                       "x-successor": dep.get("successor"), "x-deprecation-reason": dep.get("reason")})
            op["responses"]["410"] = {"description": "GONE: past the sunset date", "content": err}
        out["paths"].setdefault(path[len("/api"):], {})[method.lower()] = op
    # the operations copied from FastAPI reference its own components (422 -> HTTPValidationError)
    have, todo = out["components"]["schemas"], _refs(out["paths"])
    while todo:
        name = todo.pop()
        if name not in have and name in (full.get("components", {}).get("schemas") or {}):
            have[name] = copy.deepcopy(full["components"]["schemas"][name])
            todo |= _refs(have[name])
    out["x-atip-unresolved"] = missing
    return out


def _refs(node) -> set:
    """Names of every "#/components/schemas/<name>" $ref inside node."""
    if isinstance(node, dict):
        own = {node["$ref"][len(_REF):]} if str(node.get("$ref", "")).startswith(_REF) else set()
        return own.union(*(_refs(v) for v in node.values()))
    if isinstance(node, list):
        return set().union(*(_refs(v) for v in node))
    return set()


def write_docs(app, root="docs/api") -> dict:
    r = Path(root)
    (r / "widgets").mkdir(parents=True, exist_ok=True)
    spec = openapi_v1(app)
    (r / "openapi-v1.json").write_text(json.dumps(spec, indent=2, default=str), encoding="utf-8")
    for k, v in WIDGETS.items():
        (r / "widgets" / f"{k}.schema.json").write_text(json.dumps(
            {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": k, **v}, indent=2), encoding="utf-8")
    return {"paths": len(spec["paths"]), "unresolved": spec["x-atip-unresolved"], "widgets": len(WIDGETS)}


def key_limits(conn, key_id) -> dict:
    try:
        cfg = json.loads((Path("atip_data") / "config.json").read_text(encoding="utf-8")).get("saas") or {}
    except Exception:
        cfg = {}
    api = cfg.get("api") or {}
    r = conn.execute("SELECT rate_limit_per_minute, daily_quota FROM enterprise_api_key WHERE key_id=?",
                     (key_id,)).fetchone()
    return {"per_minute": (r[0] if r and r[0] else None) or int(api.get("key_rate_per_minute") or 60),
            "daily_quota": (r[1] if r and r[1] else None) or int(api.get("key_daily_quota") or 10000)}
