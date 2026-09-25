"""
ATIP W8 -- production hardening (the operations layer).

    context.py        request / correlation id, tenant, user, job (contextvars)
    config.py         environment (development / test / staging / production), layered
                      configuration, validation, feature flags, config version tracking
    secrets.py        secret provider abstraction (env / .env / legacy config.json),
                      validation, access logging (names only), rotation metadata, masking
    crypto.py         AES-256-GCM field encryption (cryptography), key management
    logs.py           structured JSON log file + secret-masking filter on every handler
    metrics.py        in-process metrics registry, Prometheus text exposition, DB collectors
    errors.py         error codes, exception hierarchy, API error envelope
    resilience.py     timeout, retry with exponential backoff, circuit breaker
    idempotency.py    Idempotency-Key storage for mutating requests
    http.py           ASGI middleware: request ids, /api/v1 versioning, size limits, JSON
                      validation, rate limiting, security headers, metrics, idempotency,
                      pagination / sorting / filtering, audit when enterprise is off
    health.py         /health, /health/live, /health/ready, /health/{database,broker,data,
                      scheduler,ml}  -> LIVE / READY / DEGRADED / FAILED
    migrations.py     versioned, recorded, rollback-aware migrations (schema_migrations)
    backup.py         online SQLite backups, verification, retention, restore to a new file
    jobs.py           job locks (no concurrent runs), scheduler heartbeat, job status
    data_health.py    per-source freshness / missing sessions / quality summary
    monitor.py        operational alert rules -> existing alert channel (alert_log / owner bot)
    webhooks.py       signed inbound webhooks (replay / idempotency) and outbound delivery
                      with retry + dead letter
    trading_safety.py live-trading invariants checked at startup and in W4
    scan.py           secret / insecure-configuration scanner for the repository
    startup.py        startup validation run by main.py
Nothing here enables live trading or exposes ATIP beyond its configured bind.
"""
