"""
W7 enterprise API and pages. Authentication / authorization is done once, by
enterprise/authz.py (middleware); these handlers read request.state.principal.
While enterprise.enabled is false every route below except /api/enterprise/status
answers 503 -- ATIP stays single-user.

  public   GET  /api/enterprise/status
           POST /api/auth/login        {username, password, tenant_id?} -> token + HttpOnly cookie
           POST /api/auth/register     {username, password, email, tenant_name}  (allow_self_registration)
           POST /api/auth/reset        {reset_token, new_password}
  self     GET  /api/auth/me     POST /api/auth/logout     POST /api/auth/password {old_password, new_password}
  account  GET/PUT /api/account/profile          GET /api/account/risk-profile
           GET /api/account/notifications        POST /api/account/notifications/read {ids?}
           GET/POST /api/account/api-keys        DELETE /api/account/api-keys/{key_id}
           GET/POST /api/account/watchlists      DELETE /api/account/watchlists/{id}
           GET/POST /api/account/alerts          DELETE /api/account/alerts/{id}
           GET/POST /api/account/reports         DELETE /api/account/reports/{id}
  admin    GET/POST /api/admin/users   PUT /api/admin/users/{id} {status}   PUT /api/admin/users/{id}/roles
           POST /api/admin/users/{id}/password-reset
           GET/POST /api/admin/tenants   PUT /api/admin/tenants/{id}   POST /api/admin/tenants/{id}/status
           GET /api/admin/roles   PUT /api/admin/roles/{role}   GET /api/admin/permissions
           GET/PUT /api/admin/risk-profiles/{TENANT|USER}/{id}
           GET /api/admin/plans   PUT /api/admin/plans/{id}   GET/PUT /api/admin/subscriptions/{tenant}
           GET /api/admin/usage   POST /api/admin/invoices   GET /api/admin/audit
  pages    GET /login   /account   /admin

Tenant administrators (SUPER_ADMIN of a non-default tenant) act on their own tenant
only; creating tenants, changing tenant status, plans and prices is reserved to
the platform administrator (SUPER_ADMIN of the default tenant).
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
from fastapi.responses import HTMLResponse, JSONResponse


def register(app, guard, Req, get_connection, json_safe):
    from enterprise import apikeys, audit, billing, notifications, profiles, rbac, service, tenants, users, workspace
    from enterprise.config import COOKIE, enabled, settings
    from enterprise.users import AuthError

    BAD = (ValueError, KeyError, TypeError)

    def err(e, code=400):
        return JSONResponse({"error": str(e)}, status_code=code)

    def off():
        return err("enterprise features are disabled (config.json enterprise.enabled = false)", 503)

    async def body(request):
        try:
            b = await request.json()
        except Exception:
            return {}
        return b if isinstance(b, dict) else {}

    def me(request):
        return getattr(request.state, "principal", None) or {}

    def conn_ready():
        c = get_connection()
        service.ensure_seeded(c)
        return c

    def scope_tenant(p, tenant_id):
        """Tenant admins may only act on their own tenant."""
        if not p.get("platform_admin") and tenant_id != p.get("tenant_id"):
            raise ValueError("you can only administer your own tenant")

    def run(fn, code=400):
        """Wrap a handler body: disabled -> 503; errors -> 4xx; connection closed."""
        async def _h(request, *a, **k):
            if not enabled():
                return off()
            c = conn_ready()
            try:
                return JSONResponse(json_safe(await fn(request, c, *a, **k)))
            except AuthError as e:
                return err(e, 401)
            except BAD as e:
                return err(e, 404 if str(e).startswith("no ") else code)
            finally:
                c.close()
        return _h

    # -- public ------------------------------------------------------------------------
    @app.get("/api/enterprise/status")
    async def ent_status():
        s = settings()
        return JSONResponse({"enabled": s["enabled"], "self_registration": s["allow_self_registration"],
                             "network": "bound per dashboard_host (127.0.0.1 by default); W7 does not change it"})

    @app.post("/api/auth/login")
    async def ent_login(request: Req):
        if not enabled():
            return off()
        b = await body(request)
        c = conn_ready()
        try:
            out = users.login(c, b.get("username"), b.get("password"), b.get("tenant_id"),
                              request.client.host if request.client else None, request.headers.get("user-agent"))
        except AuthError as e:
            return err(e, 401)
        finally:
            c.close()
        resp = JSONResponse(out)
        resp.set_cookie(COOKIE, out["token"], httponly=True, samesite="strict",
                        max_age=int(float(settings()["session_hours"]) * 3600))
        return resp

    @app.post("/api/auth/register")
    async def ent_register(request: Req):
        if not enabled():
            return off()
        if not settings()["allow_self_registration"]:
            return err("self-registration is disabled", 403)
        b = await body(request)
        c = conn_ready()
        try:
            name = (b.get("tenant_name") or "").strip()
            tid = "t-" + "".join(ch for ch in name.lower() if ch.isalnum())[:30]
            if len(tid) < 4:
                raise ValueError("tenant_name required")
            tenants.create(c, tid, name, actor="self-registration")
            billing.subscribe(c, tid, "FREE", "TRIAL", days=30, actor="self-registration")
            u = users.create_user(c, b.get("username"), b.get("password"), tid, ["SUPER_ADMIN"], email=b.get("email"),
                                  status="PENDING", actor="self-registration")
            return JSONResponse({"user_id": u["user_id"], "tenant_id": tid, "status": "PENDING",
                                 "note": "a platform administrator must activate the account"})
        except BAD as e:
            return err(e)
        finally:
            c.close()

    @app.post("/api/auth/reset")
    async def ent_reset(request: Req):
        async def f(req, c):
            b = await body(req)
            users.reset_password(c, b.get("reset_token"), b.get("new_password"))
            return {"status": "password reset; sign in again"}
        return await run(f)(request)

    # -- self --------------------------------------------------------------------------
    @app.get("/api/auth/me")
    async def ent_me(request: Req):
        async def f(req, c):
            p = me(req)
            if not p.get("user_id"):
                return {**p, "note": "legacy token principal"}
            return {"principal": p, "user": users.get(c, p["user_id"]),
                    "permissions": sorted(rbac.permissions_for(c, p.get("roles", []))),
                    "trading_profile": profiles.effective(c, p["tenant_id"], p["user_id"])}
        return await run(f)(request)

    @app.post("/api/auth/logout")
    async def ent_logout(request: Req):
        if not enabled():
            return off()
        c = conn_ready()
        try:
            tok = request.cookies.get(COOKIE) or (request.headers.get("authorization") or "")[7:].strip()
            if tok:
                users.logout(c, tok)
        finally:
            c.close()
        resp = JSONResponse({"status": "signed out"})
        resp.delete_cookie(COOKIE)
        return resp

    @app.post("/api/auth/password")
    async def ent_password(request: Req):
        async def f(req, c):
            b = await body(req)
            users.change_password(c, me(req)["user_id"], b.get("old_password"), b.get("new_password"))
            return {"status": "password changed; all sessions signed out"}
        return await run(f)(request)

    # -- account -------------------------------------------------------------------------
    @app.get("/api/account/profile")
    async def acc_profile(request: Req):
        return await run(lambda req, c: _a(users.get(c, me(req)["user_id"])))(request)

    @app.put("/api/account/profile")
    async def acc_profile_put(request: Req):
        async def f(req, c):
            b = await body(req)
            return users.update_profile(c, me(req)["user_id"], b.get("email"), b.get("display_name"),
                                        b.get("preferences"))
        return await run(f)(request)

    @app.get("/api/account/risk-profile")
    async def acc_risk(request: Req):
        return await run(lambda req, c: _a(profiles.effective(c, me(req)["tenant_id"], me(req)["user_id"])))(request)

    @app.get("/api/account/notifications")
    async def acc_notes(request: Req, unread: bool = False):
        return await run(lambda req, c: _a(notifications.inbox(c, me(req)["user_id"], unread)))(request)

    @app.post("/api/account/notifications/read")
    async def acc_notes_read(request: Req):
        async def f(req, c):
            b = await body(req)
            return {"marked": notifications.mark_read(c, me(req)["user_id"], b.get("ids"))}
        return await run(f)(request)

    @app.get("/api/account/api-keys")
    async def acc_keys(request: Req):
        return await run(lambda req, c: _a(apikeys.list_keys(c, me(req)["user_id"])))(request)

    @app.post("/api/account/api-keys")
    async def acc_keys_new(request: Req):
        async def f(req, c):
            b = await body(req)
            p = me(req)
            return apikeys.create(c, p["user_id"], p["tenant_id"], b.get("name") or "key", b.get("scopes") or [],
                                  int(b.get("days", 90)))
        return await run(f)(request)

    @app.delete("/api/account/api-keys/{key_id}")
    async def acc_keys_del(key_id: str, request: Req):
        async def f(req, c):
            apikeys.revoke(c, me(req)["user_id"], key_id, me(req)["user_id"])
            return {"revoked": key_id}
        return await run(f)(request)

    for kind, table, key in (("watchlists", "enterprise_watchlist", "watchlist_id"),
                             ("alerts", "enterprise_alert_rule", "rule_id"),
                             ("reports", "enterprise_report", "report_id")):
        _workspace_routes(app, Req, run, body, me, kind, table, key, workspace)

    # -- admin ---------------------------------------------------------------------------
    @app.get("/api/admin/users")
    async def adm_users(request: Req, tenant_id: str = None):
        async def f(req, c):
            p = me(req)
            t = tenant_id or (None if p.get("platform_admin") else p["tenant_id"])
            if t:
                scope_tenant(p, t)
            return users.list_users(c, t)
        return await run(f)(request)

    @app.post("/api/admin/users")
    async def adm_user_new(request: Req):
        async def f(req, c):
            b = await body(req)
            p = me(req)
            t = b.get("tenant_id") or p["tenant_id"]
            scope_tenant(p, t)
            if "SUPER_ADMIN" in (b.get("roles") or []) and not p.get("platform_admin") and t != p["tenant_id"]:
                raise ValueError("only the platform administrator can create administrators of other tenants")
            return users.create_user(c, b.get("username"), b.get("password"), t, b.get("roles") or ["VIEWER"],
                                     b.get("email"), b.get("display_name"), b.get("status", "ACTIVE"),
                                     actor=p.get("username"), must_change_password=bool(b.get("must_change_password",
                                                                                                True)))
        return await run(f)(request)

    def _member_check(c, p, uid):
        if p.get("platform_admin"):
            return
        if p["tenant_id"] not in users.memberships(c, uid):
            raise ValueError(f"no user {uid}")

    @app.put("/api/admin/users/{uid}")
    async def adm_user_status(uid: str, request: Req):
        async def f(req, c):
            b = await body(req)
            _member_check(c, me(req), uid)
            return users.set_status(c, uid, b.get("status"), actor=me(req).get("username"))
        return await run(f)(request)

    @app.put("/api/admin/users/{uid}/roles")
    async def adm_user_roles(uid: str, request: Req):
        async def f(req, c):
            b = await body(req)
            p = me(req)
            t = b.get("tenant_id") or p["tenant_id"]
            scope_tenant(p, t)
            return users.set_roles(c, uid, t, b.get("roles") or [], actor=p.get("username"))
        return await run(f)(request)

    @app.post("/api/admin/users/{uid}/password-reset")
    async def adm_user_reset(uid: str, request: Req):
        async def f(req, c):
            _member_check(c, me(req), uid)
            return users.issue_reset(c, uid, me(req).get("username"))
        return await run(f)(request)

    @app.get("/api/admin/tenants")
    async def adm_tenants(request: Req):
        async def f(req, c):
            p = me(req)
            rows = tenants.list_all(c) if p.get("platform_admin") else [tenants.get(c, p["tenant_id"])]
            for t in rows:
                t["usage"] = tenants.usage(c, t["tenant_id"])
                t["effective_limits"] = tenants.effective_limits(c, t["tenant_id"])
                t["subscription"] = billing.subscription(c, t["tenant_id"])
            return rows
        return await run(f)(request)

    def _platform(req):
        if not me(req).get("platform_admin"):
            raise ValueError("platform administrator only")

    @app.post("/api/admin/tenants")
    async def adm_tenant_new(request: Req):
        async def f(req, c):
            _platform(req)
            b = await body(req)
            t = tenants.create(c, b.get("tenant_id"), b.get("name"), b.get("settings"), b.get("limits"),
                               actor=me(req).get("username"))
            if b.get("plan"):
                billing.subscribe(c, t["tenant_id"], b["plan"], actor=me(req).get("username"))
            return t
        return await run(f)(request)

    @app.put("/api/admin/tenants/{tid}")
    async def adm_tenant_put(tid: str, request: Req):
        async def f(req, c):
            scope_tenant(me(req), tid)
            b = await body(req)
            if b.get("limits") and not me(req).get("platform_admin"):
                raise ValueError("tenant limits are set by the platform administrator")
            return tenants.update(c, tid, b.get("name"), b.get("settings"), b.get("limits"),
                                  actor=me(req).get("username"))
        return await run(f)(request)

    @app.post("/api/admin/tenants/{tid}/status")
    async def adm_tenant_status(tid: str, request: Req):
        async def f(req, c):
            _platform(req)
            b = await body(req)
            return tenants.set_status(c, tid, b.get("status"), b.get("reason", ""), actor=me(req).get("username"))
        return await run(f)(request)

    @app.get("/api/admin/roles")
    async def adm_roles(request: Req):
        return await run(lambda req, c: _a({r[0]: {"description": r[1], "permissions": sorted(
            rbac.role_permissions(c, r[0]))} for r in c.execute("SELECT role, description FROM enterprise_role")}))(request)

    @app.put("/api/admin/roles/{role}")
    async def adm_role_put(role: str, request: Req):
        async def f(req, c):
            _platform(req)
            b = await body(req)
            perms = sorted(rbac.set_role_permissions(c, role, b.get("permissions") or []))
            audit.record(c, "role.permissions", actor=me(req).get("username"), resource=role,
                         details={"permissions": perms})
            return {"role": role, "permissions": perms}
        return await run(f)(request)

    @app.get("/api/admin/permissions")
    async def adm_perms(request: Req):
        return await run(lambda req, c: _a(rbac.PERMISSIONS))(request)

    @app.get("/api/admin/risk-profiles/{scope}/{sid}")
    async def adm_prof(scope: str, sid: str, request: Req):
        async def f(req, c):
            t = sid if scope.upper() == "TENANT" else me(req)["tenant_id"]
            scope_tenant(me(req), t)
            if scope.upper() == "USER":
                _member_check(c, me(req), sid)
            return profiles.get(c, scope.upper(), sid)
        return await run(f)(request)

    @app.put("/api/admin/risk-profiles/{scope}/{sid}")
    async def adm_prof_put(scope: str, sid: str, request: Req):
        async def f(req, c):
            t = sid if scope.upper() == "TENANT" else me(req)["tenant_id"]
            scope_tenant(me(req), t)
            if scope.upper() == "USER":
                _member_check(c, me(req), sid)
            return profiles.set_profile(c, scope.upper(), sid, await body(req), actor=me(req).get("username"))
        return await run(f)(request)

    @app.get("/api/admin/plans")
    async def adm_plans(request: Req):
        return await run(lambda req, c: _a(billing.plans(c)))(request)

    @app.put("/api/admin/plans/{pid}")
    async def adm_plan_put(pid: str, request: Req):
        async def f(req, c):
            _platform(req)
            return billing.set_price(c, pid, (await body(req)).get("price_month"), me(req).get("username"))
        return await run(f)(request)

    @app.get("/api/admin/subscriptions/{tid}")
    async def adm_sub(tid: str, request: Req):
        async def f(req, c):
            scope_tenant(me(req), tid)
            return billing.subscription(c, tid)
        return await run(f)(request)

    @app.put("/api/admin/subscriptions/{tid}")
    async def adm_sub_put(tid: str, request: Req):
        async def f(req, c):
            _platform(req)
            b = await body(req)
            return billing.subscribe(c, tid, b.get("plan_id"), b.get("status", "ACTIVE"), int(b.get("days", 30)),
                                     actor=me(req).get("username"))
        return await run(f)(request)

    @app.get("/api/admin/usage")
    async def adm_usage(request: Req, tenant_id: str = None, days: int = 30):
        async def f(req, c):
            t = tenant_id or me(req)["tenant_id"]
            scope_tenant(me(req), t)
            return [dict(r) for r in c.execute("SELECT * FROM enterprise_usage WHERE tenant_id=? ORDER BY date DESC, "
                                               "metric LIMIT ?", (t, int(days) * 10))]
        return await run(f)(request)

    @app.post("/api/admin/invoices")
    async def adm_invoice(request: Req):
        async def f(req, c):
            _platform(req)
            b = await body(req)
            return billing.draft_invoice(c, b.get("tenant_id"), b.get("period_start"), b.get("period_end"),
                                         me(req).get("username"))
        return await run(f)(request)

    @app.get("/api/admin/audit")
    async def adm_audit(request: Req, tenant_id: str = None, user_id: str = None, action: str = None,
                        limit: int = 200):
        async def f(req, c):
            p = me(req)
            t = tenant_id or (None if p.get("platform_admin") else p["tenant_id"])
            if t:
                scope_tenant(p, t)
            return audit.query(c, t, user_id, action, limit)
        return await run(f)(request)

    # -- pages -----------------------------------------------------------------------------
    @app.get("/login", response_class=HTMLResponse)
    async def login_page():
        from dashboard.enterprise_page import render_login
        return HTMLResponse(render_login())

    @app.get("/account", response_class=HTMLResponse)
    async def account_page():
        from dashboard.enterprise_page import render_account
        return HTMLResponse(render_account())

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_page():
        from dashboard.enterprise_page import render_admin
        return HTMLResponse(render_admin())


async def _a(v):
    return v


def _workspace_routes(app, Req, run, body, me, kind, table, key, workspace):
    save = {"watchlists": lambda c, p, b: workspace.save_watchlist(c, p["tenant_id"], p["user_id"], b.get("name"),
                                                                   b.get("symbols"), b.get("shared", False),
                                                                   b.get("watchlist_id")),
            "alerts": lambda c, p, b: workspace.save_alert(c, p["tenant_id"], p["user_id"], b.get("name"),
                                                           b.get("symbol", ""), b.get("feature"), b.get("op"),
                                                           b.get("value"), b.get("rule_id")),
            "reports": lambda c, p, b: workspace.save_report(c, p["tenant_id"], p["user_id"], b.get("name"),
                                                             b.get("kind"), b.get("params"), b.get("shared", False),
                                                             b.get("report_id"))}[kind]

    async def lst(request: Req):
        return await run(lambda req, c: _a(workspace.list_owned(c, table, me(req)["tenant_id"],
                                                                me(req)["user_id"])))(request)

    async def new(request: Req):
        async def f(req, c):
            return save(c, me(req), await body(req))
        return await run(f)(request)

    async def rm(item_id: str, request: Req):
        async def f(req, c):
            workspace.delete_owned(c, table, key, item_id, me(req)["tenant_id"], me(req)["user_id"])
            return {"deleted": item_id}
        return await run(f)(request)

    lst.__name__, new.__name__, rm.__name__ = f"acc_{kind}", f"acc_{kind}_new", f"acc_{kind}_del"
    app.get(f"/api/account/{kind}")(lst)
    app.post(f"/api/account/{kind}")(new)
    app.delete(f"/api/account/{kind}/{{item_id}}")(rm)
