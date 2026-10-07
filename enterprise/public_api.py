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
               "schemas": {f"widget_{k}": v for k, v in WIDGETS.items()}},
           "security": [{"apiKey": []}, {"bearer": []}, {"cookie": []}],
           "paths": {}}
    missing = []
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
        if widget:
            op.setdefault("responses", {}).setdefault("200", {})["content"] = {
                "application/json": {"schema": {"$ref": f"#/components/schemas/widget_{widget}"}}}
        op.setdefault("responses", {}).update({
            "401": {"description": "UNAUTHENTICATED"}, "403": {"description": "PERMISSION_DENIED / scope missing"},
            "429": {"description": "RATE_LIMITED (per key, per tenant plan)"}})
        dep = DEPRECATIONS.get((method, path))
        if dep:
            op.update({"deprecated": True, "x-deprecated-on": str(dep["deprecated"]), "x-sunset": str(dep["sunset"]),
                       "x-successor": dep.get("successor"), "x-deprecation-reason": dep.get("reason")})
            op["responses"]["410"] = {"description": "GONE: past the sunset date"}
        out["paths"].setdefault(path[len("/api"):], {})[method.lower()] = op
    out["x-atip-unresolved"] = missing
    return out


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
