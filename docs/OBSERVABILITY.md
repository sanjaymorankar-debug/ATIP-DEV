# Observability (W8)

## Logs

| File | Format | Contents |
|---|---|---|
| `atip_data/atip.log` | text (unchanged) | everything, 10 MB rotation |
| `atip_data/atip.jsonl` | JSON lines, 20 MB × 5 | the same records, structured |

**JSON fields:**
- `ts`, `level`, `service` (`atip`), `module`, `logger`, `message`;
- `request_id`, `correlation_id`, `tenant_id`, `user_id`, `job` (from `ops/context.py`);
- `event_type`, `error_code`, `exc` (masked).

Both files pass through the secret-masking filter. To follow one request across the log, grep `atip.jsonl` for its `X-Request-ID`.

Turn JSON logging off with `ops.json_logs: false`.

## Metrics

Scrape `GET /api/ops/metrics` (Prometheus text format). With the enterprise layer on, this needs `system:operate`.

| Metric | Type | Labels |
|---|---|---|
| atip_http_requests_total | counter | method, route, status |
| atip_http_request_duration_seconds | histogram | method, route |
| atip_http_rate_limited_total | counter | |
| atip_job_runs_total | counter | job, status |
| atip_job_duration_seconds | histogram | job |
| atip_webhook_deliveries_total | counter | status |
| atip_db_latency_seconds | gauge | |
| atip_data_age_days | gauge | source |
| atip_risk_decisions_today | gauge | status |
| atip_orders_today | gauge | status |
| atip_ml_predictions_today | gauge | |
| atip_backup_age_hours | gauge | |
| atip_scheduler_heartbeat_age_seconds | gauge | |
| atip_alerts_firing | gauge | |
| atip_circuit_state | gauge | dependency (0 closed, 1 half-open, 2 open) |

Counters are per process and reset on restart. Gauges are read from the database at scrape time.

## Health endpoints

All are public and contain no secrets. HTTP 200 means LIVE, READY or DEGRADED; 503 means FAILED.

| Endpoint | Checks | FAILED when | DEGRADED when |
|---|---|---|---|
| `/health/live` | process answers | — | — |
| `/health/ready` | database answers, migrations not failed | database down / a migration FAILED | — |
| `/health/database` | latency, journal mode, size, WAL size, migrations | cannot connect | pending/failed migration, latency > 2 s |
| `/health/broker` | execution mode, live gate, credential *presence*, last portfolio sync, circuits | — | gate open, credentials missing, last sync not SUCCESS (for example DH-901 token expired), a circuit OPEN |
| `/health/data` | per-source freshness in NSE sessions, latest data-quality score | prices or scores MISSING | any source STALE |
| `/health/scheduler` | heartbeat age, failed jobs (24 h), locks | no heartbeat for > 5 min | no heartbeat yet, failed jobs |
| `/health/ml` | enabled, active models, predictions today, drift alerts | — | enabled with no active model, drift |
| `/health` | the worst of the above | | |

The CLI equivalent is `python -m ops status`.

## Alerting (`ops/monitor.py`)

- The `ops_monitor` job runs every 15 min (`ops.monitor_minutes`).
- State per rule lives in `ops_alert`.
- A rule notifies when it starts firing, again at most every 6 h while it keeps firing, and once when it resolves.
- **Delivery:** the W1 channel, `alerts.telegram.notify(category="ops")`. The alert always lands in `alert_log`, which the dashboard alert panel shows, and goes to Telegram only when a bot is configured. No other external service is contacted.

| Rule | Condition | Severity |
|---|---|---|
| database | SELECT 1 fails or > 2 s | critical |
| data_stale | a source STALE / MISSING | warning |
| scheduler | heartbeat > 5 min old | critical |
| job_failures | `pipeline.health.check_job_health` problems | warning |
| error_rate | ≥ 5% 5xx over ≥ 50 requests | warning |
| auth_failures | ≥ 20 failed / denied auth events in 1 h | warning |
| failed_orders | REJECTED / FAILED orders today | warning |
| risk_failures | the execution_cycle job (intents → risk engine → orders) FAILED today | warning |
| ml_failures | failed ML training runs or `ml_predictions` job runs today | warning |
| disk_space | < 2 GB free | critical |
| backup | latest backup FAILED, or none VERIFIED in 26 h | critical / warning |
| live_trading | live gate open | critical |

Read the state with `GET /api/ops/alerts`. Evaluate once without notifying: `python -m ops monitor`.

## Operations endpoints

| Endpoint | Returns |
|---|---|
| `/api/ops/status` | environment, config fingerprint and findings, migrations, trading safety, health |
| `/api/ops/jobs` | heartbeat, locks, next runs, last 24 h runs |
| `/api/ops/data` | source freshness and data quality |
| `/api/ops/backups` | backups |
| `/api/ops/webhooks` | webhook endpoints and deliveries |
| `/api/ops/secrets` | secret presence and rotation, never values |
| `/api/ops/config` | configuration, masked |
