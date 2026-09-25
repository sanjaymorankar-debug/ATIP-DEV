# ATIP production architecture (as of W8)

## Runtime today

ATIP runs as one Windows process on the owner's machine:

```
python main.py                           (start_atip.bat / atip_autostart.vbs / Task Scheduler)
 ├─ single-instance guard (Windows mutex)
 ├─ ops.startup.run()                    logging, config + secrets validation, trading-safety
 │                                       report, versioned migrations, config version
 ├─ dashboard thread: FastAPI + uvicorn on 127.0.0.1:8000
 │    ASGI stack (outermost first):
 │      OpsMiddleware (ops/http.py)      request ids, /api/v1, size, JSON, rate limit,
 │                                       idempotency, security headers, metrics, audit
 │      [CORSMiddleware only if configured]
 │      enterprise authz (W7)            pass-through unless enterprise.enabled
 │      routes: W1 dashboard, W2–W7 routes, ops routes (health, metrics, backups, webhooks)
 ├─ index feed thread (Dhan WebSocket, market hours)
 └─ scheduler loop (schedule, every 15 s)
      run_job → job lock → pipeline_log → metrics
      heartbeat every 60 s → ops_heartbeat
      ops_monitor (15 min), ops_backup (19:15), ops_webhook_dispatch (5 min, when due)
```

**Storage:**
- SQLite in WAL mode: `atip_data/atip.db`, about 65 MB. The busy timeout is 60 s.
- Additive migrations run once per process. Versioned migrations are recorded in `schema_migrations`.

**Files:**
- `atip_data/atip.log`: text log.
- `atip_data/atip.jsonl`: JSON log.
- `atip_data/backups/`: verified backups.
- `atip_data/secrets/`: optional secret files, one per name.
- `atip_data/ml/`: model artifacts.

## Layers

| Layer | Package | Wave |
|---|---|---|
| Data platform, scores, alerts, orders (W1 rules) | `data/`, `scores/`, `alerts/`, `orders/`, `pipeline/` | W1 |
| Backtesting | `backtest/` | W2 |
| Strategy engine | `strategy_engine/` | W3 |
| Risk + execution (paper) | `execution/` | W4 |
| AI / ML | `ml/` | W5 |
| Advanced quant | `quant/` | W6 |
| Enterprise (users, tenants, RBAC, billing foundation) | `enterprise/` | W7 |
| Operations / production hardening | `ops/` | W8 |

## API conventions (W8)

**Versioning:**
- `/api/v1/<path>` serves the current `/api/<path>` and responds with `API-Version: v1`.
- Unversioned `/api` remains for the built-in pages.
- A future breaking change will get `/api/v2` while v1 stays.

**Request ids:**
- `X-Request-ID` (1–64 chars `[A-Za-z0-9._-]`) is accepted, or generated when absent.
- `X-Correlation-ID` is propagated and defaults to the request id.
- Both are returned on the response and written into the JSON log.

**Errors:** `{"error": {"code", "message", "request_id", "retryable"}}`.

| Code | HTTP | Retryable | Meaning |
|---|---|---|---|
| VALIDATION_FAILED | 400 / 415 | no | bad request, wrong content type, invalid JSON |
| UNAUTHENTICATED | 401 | no | |
| PERMISSION_DENIED | 403 | no | |
| NOT_FOUND | 404 | no | |
| CONFLICT | 409 | no* | idempotency clash (*in progress: yes) |
| PAYLOAD_TOO_LARGE | 413 | no | over `ops.max_request_bytes` |
| DATA_QUALITY | 422 | no | |
| RATE_LIMITED | 429 | yes | honour `Retry-After` |
| INTERNAL | 500 | no | details only in the log, under the request id |
| DEPENDENCY_UNAVAILABLE | 503 | yes | |
| DEPENDENCY_TIMEOUT | 504 | yes | |
| TRADING_SAFETY | 409 | **never** | blocked by a trading control |

W1–W7 routes keep their existing `{"error": "..."}` bodies (see KNOWN_ISSUES W8-R9).

**Pagination:** on `/api/v1` GET lists.
- Query parameters: `page`, `page_size` (default 50, max 500), `sort=field|-field`, `filter[field]=value`.
- Response headers: `X-Total-Count`, `X-Page`, `X-Page-Size`.

**Idempotency:**
- Send `Idempotency-Key` on POST, PUT, PATCH or DELETE.
- Only a 2xx response is stored and replayed (with `Idempotent-Replay: true`) for 24 h, per caller.
- The same key with a different request returns 409.

**Health:** see OBSERVABILITY.md.

## Trading path and its controls

```
strategy decision (W3) → PositionIntent → risk engine (W4)
     gates: kill switch, strategy state, ML model active (W5), tenant profile (W7), live gate
     validity: intent valid, intent fresh, market data, market_data_fresh (W8, BUY)
     limits: sizing + 14 risk limits
→ APPROVED risk decision → order manager: create (one per intent) → validate
     (kill switch, live gate, LIVE session hours (W8), quantity) → submit (never retried)
→ Paper adapter (LIVE: Dhan adapter refuses every call)
```

The live gate requires all of the following:
- `execution.mode = LIVE`;
- `execution.live_trading_enabled = true`;
- **environment = production** (W8).

Even when all three hold, the W4 Dhan adapter refuses. **LIVE_TRADING_ENABLED = FALSE.**

## Performance notes (evidence-based)

- `get_connection()` used to run about 20 probes (`sqlite_master` / `PRAGMA table_info`) on every call. Those now run once per process (W7-R8).
- New indexes, chosen from the queries that need them:
  - `prices_daily(date)`: freshness and coverage queries by date;
  - `live_quotes(timestamp)`: freshness;
  - `pipeline_log(status, start_time)`: job health and monitor;
  - `enterprise_audit(at)`: audit-failure rule.
- No other optimisation was made without a measurement.

## Scalability path (documented, not built)

1. **Database:**
   - SQLite, with a single writer process, is the ceiling.
   - Multi-user SaaS (Phase 9) needs PostgreSQL (DBS-05). The repository layer then moves to SQLAlchemy with connection pooling.
   - `ops/migrations.py` keeps its versioned contract; migration SQL is rewritten per dialect.
2. **Processes:**
   - Split the API (stateless: many workers) from the scheduler (one leader, and the `ops_job_lock` rows become DB-level locks) and the market-data feed.
3. **Shared state:**
   - Rate limiting, idempotency and circuit state move to Redis.
   - Metrics move to a Prometheus server; logs ship to a collector from `atip.jsonl`.
4. **Queue:**
   - Scheduled heavy work (post-market scoring, ML training, quant research) moves behind a queue with workers.
   - Not needed at the current single-user scale, so none was added.
5. **Edge:**
   - TLS 1.3 reverse proxy (see SECURITY_ARCHITECTURE.md), only with ENT-07.
