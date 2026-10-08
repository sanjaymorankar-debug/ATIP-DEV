"""
W9 enterprise SaaS routes. Like the W7 enterprise routes they need the enterprise layer
(config.json enterprise.enabled); while it is off they answer 503 -- except
GET /api/tenant/book, which then shows the owner's paper book.

  public       POST /api/auth/forgot {identifier}          POST /api/auth/verify-email {token}
               GET  /api/notifications/unsubscribe?token=
  self         POST /api/auth/switch-tenant {tenant_id}    POST /api/auth/consent {document, version?}
  account      GET  /api/account/sessions                  DELETE /api/account/sessions/{session_id}
               POST /api/account/email/verify              GET /api/account/consents
               GET|PUT /api/account/notification-preferences   GET /api/account/deliveries
               GET|POST /api/account/vault                 DELETE /api/account/vault/{credential_id}
               GET|POST /api/account/privacy {kind, reason}
  tenant       GET  /api/tenant/book                        GET /api/onboarding   POST /api/onboarding/setup
               POST /api/reports/{report_id}/run {format}   PUT /api/reports/{report_id}/schedule
               GET  /api/reports/{report_id}/outputs        GET /api/reports/{report_id}/outputs/{output_id}
               GET  /api/billing                            POST /api/billing/plan {plan_id}
               POST /api/billing/invoices/{invoice_id}/pay  (W39b: Razorpay answers with pay_url)
               POST /api/billing/autopay {plan_id?}         POST /api/billing/autopay/cancel {at_cycle_end}
                                                            (W39b: Razorpay subscription; short_url to authorise)
  admin        GET  /api/admin/console                      GET /api/admin/isolation (platform admin)
               GET  /api/admin/privacy                      POST /api/admin/privacy/{request_id}/decision
               GET  /api/admin/onboarding                   GET /api/admin/payments
               GET  /api/admin/payments/provider            POST /api/admin/payments/reconcile  (W39b; platform admin)
               POST /api/admin/dunning/run                  POST /api/admin/billing-cycle/run   (platform admin)
               PUT  /api/admin/api-keys/{key_id}/limits
  pages        GET /app (tenant workspace)                  GET /admin/console
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
from fastapi.responses import HTMLResponse, JSONResponse, Response


def register(app, guard, Req, get_connection, json_safe):
    from enterprise import (channels, email_flows, onboarding, payments, privacy, reports, service, tenants, users,
                            vault)
    from enterprise.config import COOKIE, REFRESH_COOKIE, enabled, settings
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

    def ip(request):
        return request.client.host if request.client else None

    def run(fn, code=400, need_enabled=True):
        async def _h(request, *a, **k):
            if need_enabled and not enabled():
                return off()
            c = get_connection()
            try:
                service.ensure_seeded(c)
                out = await fn(request, c, *a, **k)
                return out if isinstance(out, Response) else JSONResponse(json_safe(out))
            except AuthError as e:
                return err(e, 401)
            except PermissionError as e:
                return err(e, 403)
            except BAD as e:
                return err(e, 404 if str(e).startswith("no ") else code)
            finally:
                c.close()
        return _h

    def platform_only(p):
        if not p.get("platform_admin"):
            raise PermissionError("platform administrator only")

    def cookies(resp, out):
        resp.set_cookie(COOKIE, out["token"], httponly=True, samesite="strict",
                        max_age=int(float(settings()["session_hours"]) * 3600))
        resp.set_cookie(REFRESH_COOKIE, out["refresh_token"], httponly=True, samesite="strict",
                        path="/api/auth/refresh", max_age=int(float(settings().get("refresh_days", 14)) * 86400))
        return resp

    # -- public ------------------------------------------------------------------------
    @app.post("/api/auth/forgot")
    async def saas_forgot(request: Req):
        async def f(req, c):
            return email_flows.forgot_password(c, (await body(req)).get("identifier"), ip(req))
        return await run(f)(request)

    @app.post("/api/auth/verify-email")
    async def saas_verify_email(request: Req):
        async def f(req, c):
            return email_flows.verify(c, (await body(req)).get("token"))
        return await run(f)(request)

    @app.get("/api/notifications/unsubscribe")
    async def saas_unsubscribe(request: Req):
        async def f(req, c):
            out = channels.unsubscribe(c, req.query_params.get("token") or "")
            return HTMLResponse(f"<p>Unsubscribed from ATIP '{out['unsubscribed']}' notifications. In-app "
                                f"notifications continue; change this under Account.</p>")
        return await run(f)(request)

    # -- self --------------------------------------------------------------------------
    @app.post("/api/auth/switch-tenant")
    async def saas_switch(request: Req):
        async def f(req, c):
            tok = req.cookies.get(COOKIE) or (req.headers.get("authorization") or "")[7:].strip()
            out = users.switch_tenant(c, tok, (await body(req)).get("tenant_id"), ip(req),
                                      req.headers.get("user-agent"))
            return cookies(JSONResponse(json_safe(out)), out)
        return await run(f, 403)(request)

    @app.post("/api/auth/consent")
    async def saas_consent(request: Req):
        async def f(req, c):
            b = await body(req)
            p = me(req)
            return privacy.record_consent(c, p["user_id"], p["tenant_id"], b.get("document") or "terms",
                                          b.get("version"), ip(req))
        return await run(f)(request)

    # -- account -----------------------------------------------------------------------
    @app.get("/api/account/sessions")
    async def acc_sessions(request: Req):
        async def f(req, c):
            tok = req.cookies.get(COOKIE) or (req.headers.get("authorization") or "")[7:].strip()
            return users.list_sessions(c, me(req)["user_id"], tok)
        return await run(f)(request)

    @app.delete("/api/account/sessions/{session_id}")
    async def acc_session_revoke(session_id: str, request: Req):
        async def f(req, c):
            return users.revoke_session(c, me(req)["user_id"], session_id)
        return await run(f)(request)

    @app.post("/api/account/email/verify")
    async def acc_email_verify(request: Req):
        async def f(req, c):
            return email_flows.send_verification(c, me(req)["user_id"])
        return await run(f)(request)

    @app.get("/api/account/consents")
    async def acc_consents(request: Req):
        async def f(req, c):
            return {"consents": privacy.consents(c, me(req)["user_id"]), "terms_version": privacy.terms_version(),
                    "privacy_version": privacy.privacy_version()}
        return await run(f)(request)

    @app.get("/api/account/notification-preferences")
    async def acc_prefs(request: Req):
        async def f(req, c):
            p = me(req)
            return {"preferences": channels.list_prefs(c, p["tenant_id"], p["user_id"]),
                    "channels": channels.CHANNELS, "modes": {"email": channels.settings()["email_mode"],
                                                             "telegram": channels.settings()["telegram_mode"]}}
        return await run(f)(request)

    @app.put("/api/account/notification-preferences")
    async def acc_prefs_set(request: Req):
        async def f(req, c):
            b = await body(req)
            p = me(req)
            return channels.set_prefs(c, p["tenant_id"], p["user_id"], b.get("category") or "*", b.get("channels"),
                                      b.get("mode"), b.get("quiet_start"), b.get("quiet_end"), b.get("unsubscribed"))
        return await run(f)(request)

    @app.get("/api/account/deliveries")
    async def acc_deliveries(request: Req):
        async def f(req, c):
            p = me(req)
            return channels.deliveries(c, p["tenant_id"], p["user_id"], 100)
        return await run(f)(request)

    @app.get("/api/account/vault")
    async def acc_vault(request: Req):
        async def f(req, c):
            p = me(req)
            return {"credentials": vault.list_for(c, p["tenant_id"], p["user_id"]), "brokers": vault.BROKERS,
                    "note": "write-only: values are never returned; no broker connection uses the vault in W9"}
        return await run(f)(request)

    @app.post("/api/account/vault")
    async def acc_vault_store(request: Req):
        async def f(req, c):
            b = await body(req)
            p = me(req)
            return vault.store(c, p["tenant_id"], p["user_id"], b.get("broker"), b.get("fields"),
                               b.get("label") or "default")
        return await run(f)(request)

    @app.delete("/api/account/vault/{credential_id}")
    async def acc_vault_revoke(credential_id: str, request: Req):
        async def f(req, c):
            p = me(req)
            return vault.revoke(c, p["tenant_id"], p["user_id"], credential_id)
        return await run(f)(request)

    @app.get("/api/account/privacy")
    async def acc_privacy(request: Req):
        async def f(req, c):
            return privacy.list_requests(c, user_id=me(req)["user_id"])
        return await run(f)(request)

    @app.post("/api/account/privacy")
    async def acc_privacy_new(request: Req):
        async def f(req, c):
            b = await body(req)
            p = me(req)
            return privacy.request(c, p["tenant_id"], p["user_id"], b.get("kind"), b.get("reason") or "")
        return await run(f)(request)

    # -- tenant ------------------------------------------------------------------------
    @app.get("/api/tenant/book")
    async def tenant_book(request: Req):
        async def f(req, c):
            from execution import tenant_books as TB
            from execution.positions import book
            tid = me(req).get("tenant_id") or TB.default_tenant()
            return book(c) if TB.is_default(tid) else TB.book(c, tid)
        return await run(f, need_enabled=False)(request)

    @app.get("/api/onboarding")
    async def onb_status(request: Req):
        async def f(req, c):
            p = me(req)
            return onboarding.status(c, p["tenant_id"], p.get("user_id"))
        return await run(f)(request)

    @app.post("/api/onboarding/setup")
    async def onb_setup(request: Req):
        async def f(req, c):
            p = me(req)
            return onboarding.setup_tenant(c, p["tenant_id"], p.get("username"), (await body(req)).get("starting_cash"))
        return await run(f)(request)

    def _own_report(c, p, rid):
        r = c.execute("SELECT tenant_id, user_id, shared FROM enterprise_report WHERE report_id=?", (rid,)).fetchone()
        if not r or r[0] != p["tenant_id"] or (r[1] != p.get("user_id") and not r[2]):
            raise ValueError(f"no report {rid}")

    @app.post("/api/reports/{report_id}/run")
    async def rep_run(report_id: str, request: Req):
        async def f(req, c):
            _own_report(c, me(req), report_id)
            return reports.run(c, report_id, (await body(req)).get("format") or "html", me(req).get("user_id"))
        return await run(f)(request)

    @app.put("/api/reports/{report_id}/schedule")
    async def rep_schedule(report_id: str, request: Req):
        async def f(req, c):
            b = await body(req)
            p = me(req)
            return reports.set_schedule(c, report_id, p["tenant_id"], p["user_id"], b.get("schedule"), b.get("formats"))
        return await run(f)(request)

    @app.get("/api/reports/{report_id}/outputs")
    async def rep_outputs(report_id: str, request: Req):
        async def f(req, c):
            _own_report(c, me(req), report_id)
            return [dict(r) for r in c.execute("SELECT output_id, format, rows, created_at FROM enterprise_report_output "
                                               "WHERE report_id=? ORDER BY created_at DESC LIMIT 50", (report_id,))]
        return await run(f)(request)

    @app.get("/api/reports/{report_id}/outputs/{output_id}")
    async def rep_output(report_id: str, output_id: str, request: Req):
        async def f(req, c):
            _own_report(c, me(req), report_id)
            o = reports.output(c, output_id)
            if not o or o["report_id"] != report_id:
                raise ValueError(f"no output {output_id}")
            mt = {"html": "text/html", "csv": "text/csv", "json": "application/json"}[o["format"]]
            return Response(o["content"], media_type=mt + "; charset=utf-8")
        return await run(f)(request)

    @app.get("/api/billing")
    async def bill_overview(request: Req):
        async def f(req, c):
            from enterprise import billing
            tid = me(req)["tenant_id"]
            return {"subscription": billing.subscription(c, tid), "plans": billing.plans(c),
                    "limits": tenants.effective_limits(c, tid), "usage": tenants.usage(c, tid),
                    "invoices": [dict(r) for r in c.execute("SELECT * FROM enterprise_invoice WHERE tenant_id=? ORDER "
                                                            "BY created_at DESC LIMIT 50", (tid,))],
                    "payments": payments.payments(c, tid, 50), "provider": payments.settings()["provider"],
                    "provider_state": payments.provider_status().get("state"), "autopay": _autopay(c, tid)}
        return await run(f)(request)

    @app.post("/api/billing/plan")
    async def bill_plan(request: Req):
        async def f(req, c):
            p = me(req)
            return payments.change_plan(c, p["tenant_id"], (await body(req)).get("plan_id"), p.get("username"))
        return await run(f)(request)

    @app.post("/api/billing/invoices/{invoice_id}/pay")
    async def bill_pay(invoice_id: str, request: Req):
        async def f(req, c):
            p = me(req)
            r = c.execute("SELECT tenant_id, status FROM enterprise_invoice WHERE invoice_id=?", (invoice_id,)).fetchone()
            if not r or (r[0] != p["tenant_id"] and not p.get("platform_admin")):
                raise ValueError(f"no invoice {invoice_id}")
            if r[1] == "DRAFT":
                payments.finalize(c, invoice_id, p.get("username"))
            inv = c.execute("SELECT status FROM enterprise_invoice WHERE invoice_id=?", (invoice_id,)).fetchone()
            if inv[0] == "PAID":
                return {"status": "PAID"}
            return payments.collect(c, invoice_id, p.get("username"))
        return await run(f)(request)

    # W39b: Razorpay autopay (a subscription the customer authorises at short_url)
    def _autopay(c, tid):
        if payments.settings()["provider"] != "razorpay":
            return None
        from enterprise.razorpay import autopay_status
        return autopay_status(c, tid)

    def _gateway(fn):
        try:
            return fn()
        except (payments.ProviderNotEnabled, payments.ProviderError) as e:
            raise ValueError(str(e)) from None

    @app.post("/api/billing/autopay")
    async def bill_autopay(request: Req):
        async def f(req, c):
            from enterprise import razorpay
            p, b = me(req), await body(req)
            return _gateway(lambda: razorpay.start_subscription(c, p["tenant_id"], b.get("plan_id"),
                                                                actor=p.get("username") or "user"))
        return await run(f)(request)

    @app.post("/api/billing/autopay/cancel")
    async def bill_autopay_cancel(request: Req):
        async def f(req, c):
            from enterprise import razorpay
            p, b = me(req), await body(req)
            return _gateway(lambda: razorpay.cancel_subscription(c, p["tenant_id"], b.get("at_cycle_end") is not False,
                                                                 actor=p.get("username") or "user"))
        return await run(f)(request)

    # -- admin -------------------------------------------------------------------------
    def _scope(p):
        return None if p.get("platform_admin") else p["tenant_id"]

    @app.get("/api/admin/console")
    async def adm_console(request: Req):
        async def f(req, c):
            from enterprise import billing
            from ops.health import overall
            p = me(req)
            tids = [t["tenant_id"] for t in tenants.list_all(c)] if p.get("platform_admin") else [p["tenant_id"]]
            rows = []
            for tid in tids:
                t = tenants.get(c, tid) or {}
                sub = billing.subscription(c, tid) or {}
                mfa = c.execute("SELECT COUNT(*), SUM(u.mfa_enabled) FROM enterprise_user u JOIN enterprise_user_role r "
                                "ON r.user_id=u.user_id AND r.role='SUPER_ADMIN' WHERE r.tenant_id=?", (tid,)).fetchone()
                rows.append({"tenant_id": tid, "name": t.get("name"), "status": t.get("status"),
                             "plan": sub.get("plan_id"), "subscription": sub.get("status"),
                             "dunning": sub.get("dunning_state"), "usage": tenants.usage(c, tid),
                             "onboarding_complete": onboarding.status(c, tid)["complete"],
                             "admins": mfa[0], "admins_with_mfa": mfa[1] or 0,
                             "api_keys": [dict(r) for r in c.execute(
                                 "SELECT key_id, user_id, name, created_at, expires_at, last_used_at, revoked_at, "
                                 "rate_limit_per_minute, daily_quota FROM enterprise_api_key WHERE tenant_id=?", (tid,))],
                             "webhook_endpoints": c.execute("SELECT COUNT(*) FROM ops_webhook_endpoint WHERE "
                                                            "tenant_id=?", (tid,)).fetchone()[0],
                             "pending_privacy_requests": c.execute(
                                 "SELECT COUNT(*) FROM enterprise_privacy_request WHERE tenant_id=? AND "
                                 "status='PENDING'", (tid,)).fetchone()[0]})
            return {"tenants": rows, "health": overall() if p.get("platform_admin") else None,
                    "payments_provider": payments.settings()["provider"],
                    "payments_status": payments.provider_status(),                     # W39b: never a credential
                    "notification_modes": {"email": channels.settings()["email_mode"],
                                           "telegram": channels.settings()["telegram_mode"]}}
        return await run(f)(request)

    @app.get("/api/admin/isolation")
    async def adm_isolation(request: Req):
        async def f(req, c):
            from enterprise.scoping import isolation_check
            platform_only(me(req))
            return isolation_check(c)
        return await run(f)(request)

    @app.get("/api/admin/privacy")
    async def adm_privacy(request: Req):
        async def f(req, c):
            return {"requests": privacy.list_requests(c, tenant_id=_scope(me(req))), "inventory": privacy.inventory()}
        return await run(f)(request)

    @app.post("/api/admin/privacy/{request_id}/decision")
    async def adm_privacy_decide(request_id: str, request: Req):
        async def f(req, c):
            p = me(req)
            r = privacy.get_request(c, request_id)
            if _scope(p) and r["tenant_id"] != p["tenant_id"]:
                raise ValueError(f"no request {request_id}")
            b = await body(req)
            return privacy.decide(c, request_id, bool(b.get("approve")), p.get("username"), b.get("note") or "")
        return await run(f)(request)

    @app.get("/api/admin/onboarding")
    async def adm_onboarding(request: Req):
        async def f(req, c):
            p = me(req)
            tids = [t["tenant_id"] for t in tenants.list_all(c)] if p.get("platform_admin") else [p["tenant_id"]]
            return [onboarding.status(c, t) for t in tids]
        return await run(f)(request)

    @app.get("/api/admin/payments")
    async def adm_payments(request: Req):
        async def f(req, c):
            return payments.payments(c, _scope(me(req)), 200)
        return await run(f)(request)

    @app.get("/api/admin/payments/provider")
    async def adm_payments_provider(request: Req):
        async def f(req, c):
            return payments.provider_status()                 # state only, never a credential
        return await run(f)(request)

    @app.post("/api/admin/payments/reconcile")
    async def adm_payments_reconcile(request: Req):
        async def f(req, c):
            platform_only(me(req))
            return _gateway(lambda: payments.reconcile(c))
        return await run(f)(request)

    @app.post("/api/admin/dunning/run")
    async def adm_dunning(request: Req):
        async def f(req, c):
            platform_only(me(req))
            return payments.run_dunning(c)
        return await run(f)(request)

    @app.post("/api/admin/billing-cycle/run")
    async def adm_cycle(request: Req):
        async def f(req, c):
            platform_only(me(req))
            return payments.run_cycle(c)
        return await run(f)(request)

    @app.put("/api/admin/api-keys/{key_id}/limits")
    async def adm_key_limits(key_id: str, request: Req):
        async def f(req, c):
            p = me(req)
            r = c.execute("SELECT tenant_id FROM enterprise_api_key WHERE key_id=?", (key_id,)).fetchone()
            if not r or (_scope(p) and r[0] != p["tenant_id"]):
                raise ValueError(f"no key {key_id}")
            b = await body(req)
            vals = []
            for k in ("rate_limit_per_minute", "daily_quota"):
                v = b.get(k)
                if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 1):
                    raise ValueError(f"{k} must be a positive integer or null")
                vals.append(v)
            c.execute("UPDATE enterprise_api_key SET rate_limit_per_minute=?, daily_quota=? WHERE key_id=?",
                      (*vals, key_id))
            from enterprise import audit
            audit.record(c, "apikey.limits", tenant_id=r[0], actor=p.get("username"), resource=key_id,
                         details={"rate_limit_per_minute": vals[0], "daily_quota": vals[1]}, commit=False)
            c.commit()
            return {"key_id": key_id, "rate_limit_per_minute": vals[0], "daily_quota": vals[1]}
        return await run(f)(request)

    # -- pages -------------------------------------------------------------------------
    @app.get("/app", response_class=HTMLResponse)
    async def page_app():
        from dashboard.saas_page import render_app
        return HTMLResponse(render_app())

    @app.get("/admin/console", response_class=HTMLResponse)
    async def page_console():
        from dashboard.saas_page import render_console
        return HTMLResponse(render_console())
