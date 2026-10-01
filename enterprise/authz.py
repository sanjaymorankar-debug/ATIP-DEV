"""
The centralized authorization layer: one HTTP middleware for the whole app.

enterprise.enabled = false (default): pass-through; ATIP behaves exactly as before.

enterprise.enabled = true, per request:
 1. PUBLIC paths (login page, /api/auth/login|register|reset, /api/enterprise/status)
    pass without a principal.
 2. PRINCIPAL, first found:
      Authorization: Bearer <session token>   (from /api/auth/login)
      Authorization: ApiKey <atk_...>          (permissions = role permissions ∩ key scopes)
      cookie atip_session                      (set by /api/auth/login for the browser)
      X-ATIP-Token alone                       only if accept_legacy_token (off by default)
    none -> 401 (API) or redirect to /login (pages).
    The user must be ACTIVE and still a member of the session's tenant; roles are
    re-read on every request, so role changes apply immediately.
 3. TENANT STATE: SUSPENDED -> read-only (GET); DISABLED / ARCHIVED -> 403.
    must_change_password -> only /api/auth/* and /api/account/profile.
 4. PERMISSION: ROUTE_RULES maps (method, path) to exactly one permission; first
    match wins; unmatched mutating /api routes need system:operate (fail closed).
 5. PAPER BOOK: execution / order / portfolio permissions act on ATIP's single
    paper book and broker account, which belong to the default tenant: other
    tenants are refused them. execution:trade also needs the caller's effective
    profile (enterprise/profiles.py) to allow trading in the current execution mode.
 6. TENANT ISOLATION:
      single resource (/api/strategies/{id}, /api/ml/models/{id}, orders, risk
      decisions, pairs, experiments, audit trails): another tenant's -> 404
      lists (GET JSON): items owned by another tenant are removed (owner found via
      strategy_id / model_id / order_id / intent_id / ...)
      creation (POST strategies / models / pairs / experiments / portfolios): the new
      row is stamped with the caller's tenant; tenant limits checked first
    The platform administrator (SUPER_ADMIN of the default tenant) sees all tenants.
 7. AUDIT + METERING: every mutating request and every denial -> enterprise_audit;
    api_requests counted per tenant per day.

The existing X-ATIP-Token guard on mutating routes is untouched and still applies.
"""

from __future__ import annotations

import json
import re

from enterprise.config import COOKIE, settings

PUBLIC = [("POST", r"^/api/auth/(login|register|reset|refresh|forgot|verify-email)$"), ("GET", r"^/login$"),
          ("GET", r"^/api/notifications/unsubscribe$"),                  # W9: signed one-click link
          ("GET", r"^/api/enterprise/status$"), ("GET", r"^/favicon\.ico$"),
          # W8: health probes (no sensitive data) and signed inbound webhooks (HMAC-verified)
          ("GET", r"^/health(/(live|ready|database|broker|data|scheduler|ml|storage|market_data|wealth|notifications|billing))?$"),
          ("POST", r"^/api/webhooks/[a-z0-9_]{1,32}$")]
SELF = r"^/api/auth/(me|logout|password|switch-tenant|consent)$"      # any signed-in principal

ROUTE_RULES = [
    ("GET", r"^/api/admin/audit", "audit:read"),
    # W9
    ("*", r"^/api/admin/(privacy|onboarding)", "admin:users"),
    ("*", r"^/api/admin/(payments|dunning|billing-cycle)", "admin:billing"),
    ("*", r"^/api/admin/(isolation|console)", "admin:tenants"),
    ("*", r"^/api/billing", "admin:billing"),
    ("*", r"^/api/onboarding", "workspace:write"),
    ("*", r"^/api/reports", "workspace:write"),
    ("GET", r"^/api/tenant/book", "execution:read"),
    ("GET", r"^/api/risk/exposure", "portfolio:read"),          # the owner's W1 book
    ("POST", r"^/api/execution/run$", "system:operate"),       # runs the cycle for every tenant
    ("*", r"^/api/admin/users/[^/]+/mfa-reset$", "admin:users"),
    # W8 operations: metrics, status, backups, config, secrets status, webhooks
    ("*", r"^/api/ops", "system:operate"),
    ("*", r"^/api/admin/(users|password-reset)", "admin:users"),
    ("*", r"^/api/admin/tenants", "admin:tenants"),
    ("*", r"^/api/admin/(roles|permissions)", "admin:roles"),
    ("*", r"^/api/admin/(plans|subscriptions|usage|invoices)", "admin:billing"),
    ("*", r"^/api/admin/risk-profiles", "risk:configure"),
    ("GET", r"^/api/account/notifications", "notifications:read"),
    ("*", r"^/api/account", "workspace:write"),
    # W3 strategies
    ("POST", r"^/api/strategies/[^/]+/backtest$", "research:run"),
    ("*", r"^/api/strategies/[^/]+/(activate|pause|disable|retire|archive|lifecycle|current-version)$",
     "strategy:lifecycle"),
    ("PUT", r"^/api/strategies/regime-mapping", "strategy:lifecycle"),
    ("POST", r"^/api/strategies/[^/]+/ml-activate$", "strategy:lifecycle"),          # W28 SE-05
    ("GET", r"^/api/(strategies|strategy-decisions)", "strategy:read"),
    ("*", r"^/api/strategies", "strategy:write"),
    # W2 backtests
    ("GET", r"^/api/backtests", "research:read"),
    ("*", r"^/api/backtests", "research:run"),
    # W5 ML
    ("POST", r"^/api/ml/models/[^/]+/(activate|pause|lifecycle)$", "ml:lifecycle"),
    ("GET", r"^/api/ml", "ml:read"),
    ("*", r"^/api/ml", "ml:write"),
    # W22 research platform
    ("POST", r"^/api/quant/approvals/", "strategy:lifecycle"),
    ("POST", r"^/api/quant/research-report$", "research:run"),
    ("GET", r"^/api/research/", "research:read"),
    ("*", r"^/api/research/", "research:run"),
    ("GET", r"^/api/(formulas|scores/components)", "strategy:read"),
    # W6 quant
    ("POST", r"^/api/quant/(research|experiments)", "research:run"),
    ("GET", r"^/api/quant", "quant:read"),
    ("*", r"^/api/quant", "quant:write"),
    # W4 risk + execution
    ("POST", r"^/api/risk/decisions/[^/]+/approve$", "risk:approve"),
    ("POST", r"^/api/risk/emergency-exit$", "risk:approve"),          # W25 RK-16
    ("GET", r"^/api/risk", "risk:read"),
    ("*", r"^/api/risk", "risk:configure"),
    ("GET", r"^/api/(oms|execution|position-intents|audit)", "execution:read"),
    ("*", r"^/api/(oms|execution)", "execution:trade"),
    # W11-W17 wealth track: the caller's own investor data (owner-scoped in wealth/)
    ("GET", r"^/api/wealth", "wealth:read"),
    ("*", r"^/api/wealth", "wealth:write"),
    # W1 order rules, portfolio
    ("GET", r"^/api/orders", "execution:read"),
    ("*", r"^/api/orders", "orders:manage"),
    ("GET", r"^/api/(portfolio|holdings|pnl|positions)", "portfolio:read"),
    ("*", r"^/api/(portfolio|holdings|pnl|positions)", "portfolio:manage"),
    # everything else
    ("GET", r"^/api/", "dashboard:read"),
    ("*", r"^/api/", "system:operate"),
    ("GET", r"^/", "dashboard:read"),
    ("*", r"^/", "system:operate"),
]
# W9: execution:read / execution:trade act on the caller's OWN book (per-tenant paper books,
# execution/tenant_books.py); W1 order rules and the portfolio are the owner's broker account.
PAPER_BOOK = {"orders:manage", "portfolio:read", "portfolio:manage"}

OWNERS = {"strategy_id": ("strategy", "strategy_id"), "model_id": ("ml_model", "model_id"),
          "run_id": ("backtest_run", "run_id"), "report_id": ("enterprise_report", "report_id"),
          "experiment_id": ("quant_experiment", "experiment_id"), "pair_id": ("quant_pair", "pair_id"),
          "portfolio_id": ("quant_portfolio", "portfolio_id"), "order_id": ("oms_order", "order_id"),
          "intent_id": ("strategy_position_intent", "intent_id"),
          "risk_decision_id": ("risk_decision", "risk_decision_id")}
RESOURCES = [(r"^/api/strategies/([^/]+)", "strategy_id", {"regime-mapping", "combined", "catalog"}),
             (r"^/api/backtests/([^/]+)", "run_id", {"walkforward", "montecarlo", "compare", "strategies"}),
             (r"^/api/reports/([^/]+)", "report_id", set()),
             (r"^/api/ml/models/([^/]+)", "model_id", set()),
             (r"^/api/quant/pairs/([^/]+)", "pair_id", {"screen"}),
             (r"^/api/quant/experiments/([^/]+)", "experiment_id", set()),
             (r"^/api/oms/orders/([^/]+)", "order_id", set()),
             (r"^/api/risk/decisions/([^/]+)", "risk_decision_id", set()),
             (r"^/api/audit/intent/([^/]+)", "intent_id", set()),
             (r"^/api/audit/order/([^/]+)", "order_id", set())]
CREATES = {r"^/api/strategies$": ("strategy_id", "strategies"), r"^/api/ml/models$": ("model_id", "models"),
           r"^/api/quant/pairs$": ("pair_id", None), r"^/api/quant/experiments$": ("experiment_id", None),
           r"^/api/quant/portfolios$": ("portfolio_id", None)}
BACKTEST_CREATE = r"^/api/(backtests|strategies/[^/]+/backtest)$"
CREATES[BACKTEST_CREATE] = ("run_id", None)          # W9: stamp new backtest runs with the caller's tenant
FILTER_PREFIX = r"^/api/(strategies|strategy-decisions|position-intents|risk|oms|ml|quant|backtests|audit)"


def permission_for(method: str, path: str) -> str:
    for m, rx, perm in ROUTE_RULES:
        if (m == "*" or m == method) and re.match(rx, path):
            return perm
    return "system:operate"


def _public(method, path):
    return any((m == method) and re.match(rx, path) for m, rx in PUBLIC)


VIA_STRATEGY = {"oms_order", "risk_decision", "strategy_position_intent"}   # owned through their strategy


def _owner_tenant(conn, key, value, cache):
    table, col = OWNERS[key]
    ck = (table, value)
    if ck not in cache:
        try:
            if table in VIA_STRATEGY:          # created by W3/W4 without a tenant: the strategy decides
                r = conn.execute(f"SELECT COALESCE(s.tenant_id,'default') FROM {table} t JOIN strategy s ON "
                                 f"s.strategy_id=t.strategy_id WHERE t.{col}=?", (value,)).fetchone()
            else:
                r = conn.execute(f"SELECT COALESCE(tenant_id,'default') FROM {table} WHERE {col}=?",
                                 (value,)).fetchone()
            cache[ck] = r[0] if r else None
        except Exception:
            cache[ck] = None
    return cache[ck]


def resolve(conn, request) -> dict | None:
    """The principal for this request, with roles, permissions and flags; or None."""
    from dashboard.security import TOKEN_HEADER, token_ok
    from enterprise import apikeys, rbac, users
    s = settings()
    auth = request.headers.get("authorization") or ""
    p = None
    if auth.lower().startswith("bearer "):
        p = users.session_principal(conn, auth[7:].strip())
    elif auth.lower().startswith("apikey "):
        p = apikeys.principal(conn, auth[7:].strip())
    elif request.cookies.get(COOKIE):
        p = users.session_principal(conn, request.cookies.get(COOKIE))
    elif s["accept_legacy_token"] and token_ok(request.headers.get(TOKEN_HEADER)):
        return {"user_id": None, "username": "system", "tenant_id": s["default_tenant"], "roles": ["SUPER_ADMIN"],
                "permissions": set(rbac.PERMISSIONS), "via": "legacy_token", "platform_admin": True,
                "must_change_password": False}
    if not p:
        return None
    u = conn.execute("SELECT username, status, must_change_password FROM enterprise_user WHERE user_id=?",
                     (p["user_id"],)).fetchone()
    if not u or u[1] != "ACTIVE":
        return None
    roles = users.memberships(conn, p["user_id"]).get(p["tenant_id"], [])
    if not roles:
        return None
    perms = rbac.permissions_for(conn, roles)
    if p.get("scopes") is not None:
        perms &= p["scopes"]
    return {**p, "username": u[0], "roles": roles, "permissions": perms, "must_change_password": bool(u[2]),
            "platform_admin": "SUPER_ADMIN" in roles and p["tenant_id"] == s["default_tenant"]}


def install(app):
    """Register the middleware on the FastAPI app (a no-op per request while disabled)."""
    from fastapi.responses import JSONResponse, RedirectResponse, Response

    @app.middleware("http")
    async def enterprise_authz(request, call_next):
        if not settings()["enabled"]:
            return await call_next(request)
        method, path = request.method.upper(), request.url.path
        if method == "OPTIONS" or _public(method, path):
            return await call_next(request)
        from db.schema import get_connection
        from enterprise import audit, billing, profiles, tenants
        s = settings()
        conn = get_connection()
        try:
            ip = request.client.host if request.client else None
            p = resolve(conn, request)
            if p is None:
                if path.startswith("/api/"):
                    return JSONResponse({"error": "authentication required"}, status_code=401)
                return RedirectResponse(f"/login?next={path}", status_code=302)

            def deny(code, msg):
                audit.record(conn, "authz.denied", tenant_id=p["tenant_id"], user_id=p.get("user_id"),
                             actor=p["username"], method=method, path=path, status_code=code, ip=ip,
                             details={"reason": msg})
                return JSONResponse({"error": msg}, status_code=code)

            t = tenants.get(conn, p["tenant_id"])
            if not t or t["status"] in ("DISABLED", "ARCHIVED"):
                return deny(403, f"tenant {p['tenant_id']} is {t['status'] if t else 'missing'}")
            if t["status"] == "SUSPENDED" and method != "GET":
                return deny(403, f"tenant {p['tenant_id']} is SUSPENDED (read-only)")
            if p["must_change_password"] and not re.match(r"^/api/(auth/|account/profile)", path) and \
                    path.startswith("/api/"):
                return deny(403, "password change required (POST /api/auth/password)")
            if not re.match(SELF, path):
                perm = permission_for(method, path)
                if perm not in p["permissions"]:
                    return deny(403, f"permission {perm} required")
                if perm in PAPER_BOOK and p["tenant_id"] != s["default_tenant"] and not p["platform_admin"]:
                    return deny(403, "the paper book and broker account belong to the owner's tenant")
                if perm == "execution:trade":
                    from execution.config import execution_settings
                    prof = profiles.effective(conn, p["tenant_id"], p.get("user_id"))
                    mode = execution_settings()["mode"]
                    if not prof["trading_enabled"] or mode not in (prof["allowed_modes"] or []):
                        return deny(403, f"trading not permitted by the {p['tenant_id']} / user profile "
                                         f"(mode {mode})")
            cache = {}
            for rx, key, skip in RESOURCES:
                m = re.match(rx, path)
                if m and m.group(1) not in skip and not p["platform_admin"]:
                    owner = _owner_tenant(conn, key, m.group(1), cache)
                    if owner is not None and owner != p["tenant_id"]:
                        return deny(404, "not found")
            create = next(((k, lim) for rx, (k, lim) in CREATES.items() if method == "POST" and re.match(rx, path)),
                          None)
            if create and create[1]:
                try:
                    tenants.check_limit(conn, p["tenant_id"], create[1])
                except ValueError as e:
                    return deny(403, str(e))
            if method == "POST" and re.match(BACKTEST_CREATE, path):
                lim = tenants.effective_limits(conn, p["tenant_id"]).get("max_backtests_per_day")
                from datetime import date
                r = conn.execute("SELECT value FROM enterprise_usage WHERE tenant_id=? AND date=? AND metric="
                                 "'backtests'", (p["tenant_id"], str(date.today()))).fetchone()
                if lim is not None and (r[0] if r else 0) >= lim:
                    return deny(403, f"max_backtests_per_day {lim} reached")
            # W9: per-API-key rate limit + daily quota, and the tenant plan's daily API calls
            if p.get("via") == "api_key":
                from enterprise.public_api import key_limits
                from ops.shared_state import store
                kl = key_limits(conn, p["key_id"])
                ok, wait = store().bucket(f"apikey:{p['key_id']}", kl["per_minute"])
                if not ok:
                    return JSONResponse({"error": {"code": "RATE_LIMITED", "message": "API key rate limit",
                                                   "retryable": True}}, status_code=429,
                                        headers={"Retry-After": str(wait)})
                from datetime import date as _d
                used = conn.execute("SELECT calls FROM enterprise_api_usage WHERE key_id=? AND date=?",
                                    (p["key_id"], str(_d.today()))).fetchone()
                if used and used[0] >= kl["daily_quota"]:
                    return deny(429, f"API key daily quota {kl['daily_quota']} reached")
                conn.execute("INSERT INTO enterprise_api_usage (key_id,tenant_id,date,calls) VALUES (?,?,?,1) ON "
                             "CONFLICT(key_id,date) DO UPDATE SET calls=calls+1", (p["key_id"], p["tenant_id"],
                                                                                    str(_d.today())))
                conn.commit()
            lim_calls = tenants.effective_limits(conn, p["tenant_id"]).get("max_api_calls_per_day")
            if lim_calls is not None and path.startswith("/api/"):
                from datetime import date as _d
                r = conn.execute("SELECT value FROM enterprise_usage WHERE tenant_id=? AND date=? AND "
                                 "metric='api_requests'", (p["tenant_id"], str(_d.today()))).fetchone()
                if r and r[0] >= lim_calls:
                    return deny(429, f"plan limit max_api_calls_per_day {lim_calls} reached")
            request.state.principal = {k: v for k, v in p.items() if k != "permissions"}
        finally:
            conn.close()

        response = await call_next(request)

        body = None
        needs_body = (method == "GET" and re.match(FILTER_PREFIX, path) and not p["platform_admin"]) or \
                     (create and response.status_code < 300)
        if needs_body and "application/json" in (response.headers.get("content-type") or ""):
            body = b"".join([c async for c in response.body_iterator])
        conn = get_connection()
        try:
            if body is not None:
                try:
                    data = json.loads(body)
                except Exception:
                    data = None
                if create and response.status_code < 300 and isinstance(data, dict) and data.get(create[0]):
                    table, col = OWNERS[create[0]]
                    conn.execute(f"UPDATE {table} SET tenant_id=? WHERE {col}=?", (p["tenant_id"], data[create[0]]))
                    conn.commit()
                elif method == "GET" and data is not None:
                    data = _filter(conn, data, p["tenant_id"], {})
                    body = json.dumps(data).encode()
            if method == "POST" and re.match(BACKTEST_CREATE, path) and response.status_code < 300:
                billing.meter(conn, p["tenant_id"], "backtests", increment=1)
            billing.meter(conn, p["tenant_id"], "api_requests", increment=1)
            if method != "GET":
                audit.record(conn, "api." + method.lower(), tenant_id=p["tenant_id"], user_id=p.get("user_id"),
                             actor=p["username"], method=method, path=path, status_code=response.status_code, ip=ip,
                             commit=False)
            conn.commit()
        finally:
            conn.close()
        if body is not None:
            headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
            return Response(content=body, status_code=response.status_code, headers=headers,
                            media_type="application/json")
        return response

    return app


def _owned_elsewhere(conn, item, tenant, cache):
    if not isinstance(item, dict):
        return False
    for key in OWNERS:
        v = item.get(key)
        if isinstance(v, str) and v:
            owner = _owner_tenant(conn, key, v, cache)
            return owner is not None and owner != tenant
    return False


def _filter(conn, data, tenant, cache):
    if isinstance(data, list):
        return [x for x in data if not _owned_elsewhere(conn, x, tenant, cache)]
    if isinstance(data, dict):
        if _owned_elsewhere(conn, data, tenant, cache):
            return {"error": "not found"}
        return {k: (_filter(conn, v, tenant, cache) if isinstance(v, list) else v) for k, v in data.items()}
    return data
