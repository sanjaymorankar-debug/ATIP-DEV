"""
W8 operations routes (ops/).

  health (public: uptime probes; nothing sensitive)
    GET /health  /health/live  /health/ready  /health/{database|broker|data|scheduler|ml}
  inbound webhooks (public, HMAC-verified: ops/webhooks.py)
    POST /api/webhooks/{source}
  operations (system:operate when the enterprise layer is on; mutating ones also need
  the dashboard token when it is off, like every other mutating route)
    GET  /api/ops/metrics              Prometheus text format
    GET  /api/ops/status               environment, config fingerprint + findings, migrations,
                                       trading safety, component health
    GET  /api/ops/config               effective configuration, secrets MASKED
    GET  /api/ops/secrets              presence / source / rotation (never values)
    GET  /api/ops/data                 data freshness + latest data-quality results
    GET  /api/ops/jobs                 heartbeat, locks, next runs, last 24 h runs
    GET  /api/ops/alerts               monitoring alert state
    POST /api/ops/monitor/run          evaluate the rules now (notifies like the scheduled job)
    GET  /api/ops/backups              POST /api/ops/backups (verified backup now)
    POST /api/ops/backups/prune
    GET  /api/ops/audit/verify         enterprise_audit hash-chain check
    GET  /api/ops/webhooks             POST /api/ops/webhooks {url, events, secret_name}
    POST /api/ops/webhooks/deliveries/{id}/requeue
"""

from __future__ import annotations

from fastapi.responses import JSONResponse, PlainTextResponse


def register(app, guard, Req, get_connection, json_safe):
    from ops import health as H

    def ok(x, code=200):
        return JSONResponse(json_safe(x), status_code=code)

    async def body(request):
        try:
            b = await request.json()
        except Exception:
            return {}
        return b if isinstance(b, dict) else {}

    # -- health ------------------------------------------------------------------------
    @app.get("/health")
    def health_all():
        p = H.overall()
        return ok(p, H.http_status(p))

    @app.get("/health/live")
    def health_live():
        return ok(H.live())

    @app.get("/health/ready")
    def health_ready():
        p = H.ready()
        return ok(p, H.http_status(p))

    @app.get("/health/{component}")
    def health_component(component: str):
        if component not in H.COMPONENTS:
            return ok({"error": {"code": "NOT_FOUND", "message": "unknown component"}}, 404)
        p = H.component(component)
        return ok(p, H.http_status(p))

    # -- inbound webhooks ----------------------------------------------------------------
    @app.post("/api/webhooks/{source}")
    async def webhook_in(source: str, request: Req):
        from ops.webhooks import verify_inbound
        raw = await request.body()
        c = get_connection()
        try:
            code, payload = verify_inbound(c, source, {k.lower(): v for k, v in request.headers.items()}, raw)
        finally:
            c.close()
        return ok(payload, code)

    # -- operations ----------------------------------------------------------------------
    @app.get("/api/ops/metrics")
    def ops_metrics():
        from ops import metrics
        c = get_connection()
        try:
            metrics.collect(c)
        finally:
            c.close()
        return PlainTextResponse(metrics.exposition(), media_type="text/plain; version=0.0.4")

    @app.get("/api/ops/status")
    def ops_status():
        from ops.config import environment, fingerprint, validate
        from ops.migrations import status as mstatus
        from ops.trading_safety import report
        c = get_connection()
        try:
            mig = mstatus(c)
            cv = c.execute("SELECT fingerprint, recorded_at FROM ops_config_version ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            c.close()
        return ok({"environment": environment(), "config_fingerprint": fingerprint(),
                   "config_recorded": dict(cv) if cv else None, "config_findings": validate(), "migrations": mig,
                   "trading_safety": report(), "health": H.overall()})

    @app.get("/api/ops/config")
    def ops_config():
        from ops.config import load, masked
        return ok(masked(load()))

    @app.get("/api/ops/secrets")
    def ops_secrets():
        from ops.secrets import rotation_status, validate
        c = get_connection()
        try:
            return ok({"secrets": rotation_status(c), "findings": validate()})
        finally:
            c.close()

    @app.get("/api/ops/data")
    def ops_data():
        from ops.data_health import quality, sources
        c = get_connection()
        try:
            return ok({"sources": sources(c), "quality": quality(c)})
        finally:
            c.close()

    @app.get("/api/ops/jobs")
    def ops_jobs():
        from ops import jobs as J
        c = get_connection()
        try:
            return ok({"heartbeat_age_s": J.heartbeat_age(c), "locks": J.locks(c), "scheduled": J.scheduled(),
                       "recent_runs": J.recent_runs(c)})
        finally:
            c.close()

    @app.get("/api/ops/alerts")
    def ops_alerts():
        from ops.monitor import alerts
        c = get_connection()
        try:
            return ok(alerts(c))
        finally:
            c.close()

    @app.post("/api/ops/monitor/run", dependencies=guard)
    def ops_monitor_run():
        from ops.monitor import run_monitor
        return ok(run_monitor())

    @app.get("/api/ops/backups")
    def ops_backups():
        from ops.backup import list_backups
        return ok(list_backups())

    @app.post("/api/ops/backups", dependencies=guard)
    def ops_backup_now():
        from ops.backup import backup
        r = backup("manual")
        return ok(r, 200 if r["status"] == "VERIFIED" else 500)

    @app.post("/api/ops/backups/prune", dependencies=guard)
    def ops_backup_prune():
        from ops.backup import prune
        return ok({"pruned": prune()})

    @app.get("/api/ops/audit/verify")
    def ops_audit_verify():
        from enterprise.audit import verify_chain
        c = get_connection()
        try:
            return ok(verify_chain(c))
        finally:
            c.close()

    @app.get("/api/ops/webhooks")
    def ops_webhooks():
        from ops.webhooks import overview
        c = get_connection()
        try:
            return ok(overview(c))
        finally:
            c.close()

    @app.post("/api/ops/webhooks", dependencies=guard)
    async def ops_webhook_add(request: Req):
        from ops.webhooks import add_endpoint
        b = await body(request)
        c = get_connection()
        try:
            return ok(add_endpoint(c, b.get("url") or "", b.get("events") or ["*"], b.get("secret_name") or "",
                                   b.get("tenant_id") or "default"))
        except ValueError as e:
            return ok({"error": {"code": "VALIDATION_FAILED", "message": str(e)}}, 400)
        finally:
            c.close()

    @app.post("/api/ops/webhooks/deliveries/{delivery_id}/requeue", dependencies=guard)
    def ops_webhook_requeue(delivery_id: str):
        from ops.webhooks import requeue
        c = get_connection()
        try:
            done = requeue(c, delivery_id)
        finally:
            c.close()
        return ok({"requeued": done}, 200 if done else 404)
