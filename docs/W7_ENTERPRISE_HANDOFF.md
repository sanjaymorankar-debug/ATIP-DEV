# ATIP W7 Enterprise: development handoff

- **Independent functional testing:** PENDING — ChatGPT.
- **Testing performed by Claude:** none. Only build/integration checks (section 8).
- **Live trading:** DISABLED.
- **Deployment / restart:** NOT PERFORMED. Merged to `master`; the running ATIP picks the code up at its next restart.
- **Default state:** the enterprise layer ships **off** (`enterprise.enabled = false`), so ATIP behaves exactly as before until the owner bootstraps an admin and enables it.

Architecture and security: `docs/ENTERPRISE_ARCHITECTURE.md`.

## 0. Wave 6 verification and assumptions

- **W6 is committed and in `master`:** commits `79dd809` and `cf1997d`. All branches were merged, and no uncommitted work was found. W7 was built on branch `w7-enterprise` in the worktree and fast-forwarded into `master`.
- **Assumptions** (the brief was cut off at section 15; the owner said to proceed on these):
  1. The remaining scope (billing/subscription, notifications, API keys, audit, admin UI) follows the brief's architecture diagram.
  2. The layer is off by default.
  3. **No network exposure** (ENT-07 stays blocked).
  4. No payment gateway.
  5. No broker-credential vault.
  6. Merge to `master` without restarting ATIP.
  7. Tracker mapping: W7 = the ENT-*, SEC-02/03 and API-03 rows.

## 1. Implementation status

| Area | Coding status | Notes |
|---|---|---|
| User management | IN PROGRESS | Registration (admin-created / optional self-registration), login, passwords, status, profile, preferences, audit; no e-mail verification |
| Authentication / sessions | COMPLETED | PBKDF2 passwords, digest-only sessions, HttpOnly cookie / Bearer, lockout, revocation |
| RBAC | IN PROGRESS | 8 roles, 27 permissions, per-tenant roles, centralized route → permission map, role editing; no MFA |
| Multi-tenancy | IN PROGRESS | Tenants, lifecycle, limits, tenant_id on owned data, middleware isolation; single shared paper book (execution for the default tenant only) |
| Tenant management | COMPLETED | Create, status (ACTIVE / SUSPENDED / DISABLED / ARCHIVED), settings, limits, users, roles, audit |
| User / tenant risk profile | IN PROGRESS | Trading permissions, modes, brokers, assets, strategies, capital, order value; enforced in the middleware and the W4 risk engine (tighten only) |
| Broker permissions | IN PROGRESS | `allowed_brokers` / `allowed_modes` in profiles; per-user broker credentials NOT built (ENT-06) |
| API keys | COMPLETED | Scoped, expiring, revocable, digest-only |
| Notifications | IN PROGRESS | In-app inbox + preferences; W4 risk events; alert hits; Telegram (owner bot) only; no e-mail |
| Workspace (watchlists, alerts, reports) | IN PROGRESS | CRUD + post-market alert evaluation; report rendering not built |
| Billing / subscription | IN PROGRESS | Plans, subscriptions, usage metering, DRAFT invoices; no payment gateway, no prices invented |
| Audit | COMPLETED | enterprise_audit for mutations, denials and service events; secrets redacted |
| Admin console | IN PROGRESS | `/admin` (users, tenants, roles, plans, audit) + full admin API |
| W1–W6 integration | COMPLETED | Middleware in front of all routes; W4 `tenant_profile` check; W4 risk notifications; alert rules use W3/W6/ML features |
| Internet exposure (ENT-07) | BLOCKED | Deliberately not done; needs TLS, MFA, rate limiting, security review, and the ENT-14 legal review |

## 2. Files

- **New:**
  - `enterprise/`: `__init__`, config, security, rbac, audit, tenants, users, apikeys, profiles, notifications, billing, workspace, authz, service, `__main__`;
  - `dashboard/enterprise_routes.py`, `dashboard/enterprise_page.py`;
  - `docs/W7_ENTERPRISE_HANDOFF.md`, `docs/ENTERPRISE_ARCHITECTURE.md`.
- **Modified:**
  - `db/schema.py`: W7_TABLES + tenant_id columns;
  - `dashboard/server.py`: middleware install, routes, nav links;
  - `execution/risk_engine.py`: `tenant_profile` check;
  - `execution/pipeline.py`: risk notification hook;
  - `pipeline/scheduler.py`: `enterprise_jobs`;
  - `config_template.json`: section 14;
  - `docs/KNOWN_ISSUES.md`, `docs/ATIP_MASTER_TRACKER.csv`.

## 3. Database

- **New tables (19):**
  - tenancy and users: `enterprise_tenant`, `enterprise_user`;
  - RBAC: `enterprise_role`, `enterprise_permission`, `enterprise_role_permission`, `enterprise_user_role`;
  - credentials: `enterprise_session`, `enterprise_password_reset`, `enterprise_api_key`;
  - profiles and notifications: `enterprise_risk_profile`, `enterprise_notification`;
  - audit: `enterprise_audit`;
  - billing: `enterprise_plan`, `enterprise_subscription`, `enterprise_usage`, `enterprise_invoice`;
  - workspace: `enterprise_watchlist`, `enterprise_alert_rule`, `enterprise_report`.
- **Modified tables (additive column `tenant_id TEXT DEFAULT 'default'`):** `strategy`, `strategy_position_intent`, `risk_decision`, `oms_order`, `ml_model`, `quant_pair`, `quant_experiment`, `quant_portfolio`. Existing rows are set to `default`.

## 4. APIs (new; none modified)

| Group | Routes |
|---|---|
| Auth | `GET /api/enterprise/status`; `POST /api/auth/login`, `/register`, `/reset`, `/logout`, `/password`; `GET /api/auth/me` |
| Account | `/api/account/profile` (GET/PUT), `/risk-profile`, `/notifications` (+ `/read`), `/api-keys` (GET/POST/DELETE), `/watchlists`, `/alerts`, `/reports` (GET/POST/DELETE) |
| Admin | `/api/admin/users` (+ `/{id}`, `/{id}/roles`, `/{id}/password-reset`), `/tenants` (+ `/{id}`, `/{id}/status`), `/roles` (+ `/{role}`), `/permissions`, `/risk-profiles/{scope}/{id}`, `/plans` (+ `/{id}`), `/subscriptions/{tenant}`, `/usage`, `/invoices`, `/audit` |
| Pages | `/login`, `/account`, `/admin` |

The middleware gates all existing routes (W1–W6) when enabled; their behaviour is unchanged when disabled.

## 5. Configuration

`config.json` "enterprise" (defaults):

| Key | Default |
|---|---|
| enabled | false |
| session_hours | 12 |
| allow_self_registration | false |
| accept_legacy_token | false |
| password_min_length | 12 |
| max_failed_logins | 5 |
| lockout_minutes | 15 |
| default_tenant | "default" |

CLI: `python -m enterprise status | bootstrap | add-user | add-tenant | tenant-status | roles | reset | audit` (passwords via getpass).

## 6. Deferred work

- MFA.
- E-mail verification and delivery.
- Per-tenant paper books / broker accounts.
- The broker credential vault (ENT-06).
- A versioned public API (`/api/v1`) and webhooks.
- Report rendering and export.
- A payment gateway.
- Internet exposure (ENT-07) with TLS, rate limiting and a security review.
- SQL-level tenant scoping inside W3–W6 modules (today isolation is enforced at the API layer).

## 7. Known issues

See `docs/KNOWN_ISSUES.md` (W7-R1..R9).

## 8. Build/integration checks performed (not testing)

| Check | Result |
|---|---|
| `py_compile` of enterprise, quant, ml, strategy_engine, execution, dashboard, backtest, db, pipeline | 111 files, 0 errors |
| pyflakes on `enterprise/`, the enterprise dashboard files, `execution/risk_engine.py`, `pipeline.py` | Clean |
| Migrations on a **copy** of the production DB | 19 enterprise tables; tenant_id added to 8 tables; all 15 existing strategies = `default`; other rows kept |
| Seeding on the copy | 8 roles, 27 permissions, plans FREE / PRO / ENTERPRISE, default tenant on ENTERPRISE |
| App startup | Middleware registered; 185 routes, 48 of them W7; no duplicate method+path |
| Static route map | Every registered route maps to a defined permission (0 unmapped); unmatched mutations fail closed to `system:operate` |

## 9. Deferred testing items (for ChatGPT)

| ID | Scenario |
|---|---|
| W7-T01 | Disabled (default): every existing page and API behaves as before; enterprise routes answer 503 |
| W7-T02 | Bootstrap an admin; enable; `/` redirects to `/login`; `/api/*` without a session returns 401 |
| W7-T03 | Login sets an HttpOnly cookie; wrong password × 5 locks the account; lockout expires |
| W7-T04 | VIEWER: GETs allowed; any mutation returns 403 with the permission named; audit row written |
| W7-T05 | TRADER can submit a paper order; RESEARCHER cannot (403) |
| W7-T06 | User of tenant B: GET `/api/strategies/{tenant A strategy}` returns 404; `/api/strategies` list omits A's |
| W7-T07 | Tenant B creates a strategy: row stamped `tenant_id = B`; `max_strategies` limit enforced |
| W7-T08 | Tenant B: execution / orders / portfolio routes return 403 (paper book belongs to default) |
| W7-T09 | SUSPENDED tenant: GET ok, POST 403; DISABLED: all 403; default tenant cannot be suspended |
| W7-T10 | Role change applies on the next request; disable revokes sessions |
| W7-T11 | API key with scopes ⊂ role: out-of-scope call returns 403; revoked or expired key returns 401 |
| W7-T12 | Tenant profile `trading_enabled` false: W4 intents BLOCKED (`tenant_profile`); `/api/oms` trade returns 403 |
| W7-T13 | Tenant `max_order_value`: W4 caps quantity (WARN) |
| W7-T14 | Password reset token: one-time, 30 minutes; sessions revoked |
| W7-T15 | Must-change-password users can only reach auth / account-profile routes |
| W7-T16 | Notifications: a risk REJECTED in the cycle notifies risk:read users of the strategy's tenant; opt-out respected |
| W7-T17 | Alert rule `rsi_14 < 30` fires once per day post-market into the owner's inbox |
| W7-T18 | Billing: plan limits merge with tenant limits (tighter wins); invoice is DRAFT with a NULL amount when no price is set |
| W7-T19 | Audit redacts passwords / tokens; entries for denials and mutations |
| W7-T20 | `accept_legacy_token` false: X-ATIP-Token alone (no session) returns 401 when enabled |
| W7-T21 | Regression: W1–W6 unchanged with enterprise disabled |
