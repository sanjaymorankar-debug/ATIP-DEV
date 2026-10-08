# ATIP enterprise architecture (W7)

```
                          ATIP (W1–W6, unchanged business logic)
                                          │
 request ─► enterprise.authz middleware ──┼─► existing routes (W1 X-ATIP-Token guard still applies)
             │ 1 public?  2 principal  3 tenant state  4 permission  5 paper-book rule
             │ 6 tenant isolation (resource 404 · list filter · create stamp · limits)
             │ 7 audit + usage metering
             ▼
   enterprise_* tables: tenants · users · roles/permissions · sessions · API keys · profiles
                        · notifications · watchlists/alerts/reports · plans/subscriptions/usage/invoices · audit
```

**Off by default** (`config.json enterprise.enabled = false`): the middleware passes everything through and ATIP stays single-user.

## Hard boundaries

- **Network binding is unchanged.** `dashboard/security.py` binds to 127.0.0.1 unless the owner sets `dashboard_host`. Exposing ATIP to other machines or the internet is tracker item ENT-07 (TLS, MFA, rate limiting, security review) and is **not** part of W7.
- **Live trading stays off.** Profiles and roles can only restrict. W4's gates (mode PAPER, `live_trading_enabled` false, the Dhan adapter refusing every call) are untouched.
- **No credentials are handled by Claude.**
  - The bootstrap administrator's password is typed by the owner (`python -m enterprise bootstrap`, getpass).
  - There is no broker-credential vault (ENT-06 not built).
- **No payment processing.** Plans have limits and features; prices are NULL until the owner sets them; invoices are DRAFT records. *(Superseded in W39b: the payment provider is SANDBOX by default and Razorpay once the owner configures it -- docs/BILLING_RAZORPAY.md.)*

## Identity

| Credential | Where | Stored |
|---|---|---|
| Password | `/api/auth/login` | PBKDF2-HMAC-SHA256, 390k iterations, random salt |
| Session | HttpOnly SameSite=Strict cookie `atip_session`, or `Authorization: Bearer` | SHA-256 digest, expiry, IP, user agent |
| API key | `Authorization: ApiKey atk_…` | SHA-256 digest, scopes, expiry |
| Reset token | Admin-issued, one-time, 30 minutes | SHA-256 digest |
| Legacy X-ATIP-Token | Accepted as the platform SUPER_ADMIN only if `accept_legacy_token` is true (default false: every page embeds it) | — |

**Account protection:**
- Accounts lock after `max_failed_logins` (5) for `lockout_minutes` (15).
- Password policy: 12+ characters, a letter and a digit.
- Sessions are revoked on logout, password change and disable.
- Users created by an admin must change their password at first sign-in.

## RBAC

| Role | Summary |
|---|---|
| SUPER_ADMIN | Everything (platform admin when in the default tenant) |
| RESEARCHER | Research, factors, strategies (read), backtests |
| QUANT | Researcher + builds strategies, factors, ML models |
| STRATEGY_MANAGER | Strategy lifecycle, model activation |
| RISK_MANAGER | Risk limits, approvals, exposure, audit, operate |
| TRADER | Execution, orders, positions |
| PORTFOLIO_MANAGER | Portfolio construction and oversight |
| VIEWER | Read-only |

- **Permissions:** 27 `module:action` permissions (see `enterprise/rbac.py`), editable per role by the platform admin (`PUT /api/admin/roles/{role}`).
- **Route mapping:** `enterprise/authz.py ROUTE_RULES` maps every API route to one permission, first match wins. Anything unmatched and mutating needs `system:operate`, so it fails closed. The build check confirms that all 185 registered routes map to a defined permission.
- **Business logic** contains no role checks.

## Multi-tenancy

**Tenant lifecycle:**

| Status | Access |
|---|---|
| ACTIVE | Full |
| SUSPENDED | Read-only |
| DISABLED | None |
| ARCHIVED | Terminal |

The owner's `default` tenant cannot leave ACTIVE.

**Tenant-owned data:**
- W7 tables carry `tenant_id`.
- `strategy`, `ml_model`, `quant_pair`, `quant_experiment` and `quant_portfolio` gained `tenant_id` (existing rows = `default`).
- Intents, risk decisions and orders are owned **through their strategy**.

**Isolation (middleware):**

| Case | Behaviour |
|---|---|
| Another tenant's resource by id | 404 |
| Lists | Other tenants' items removed |
| New strategies / models / pairs / experiments / portfolios | Stamped with the caller's tenant |
| Tenant limits | Checked before creation (users, strategies, models, API keys, backtests per day), tighter of tenant and plan limits |

**Shared single paper book:**
- ATIP has one paper book and one broker account. Only the default tenant may read or trade execution, orders and portfolio.
- Other tenants can research, build strategies and models, and backtest.
- Per-tenant books are future work.

**Market data** (prices, scores, factors, ML predictions) is shared, not tenant-owned.

## Profiles & W4

Tenant and user trading profiles:
- trading_enabled
- allowed_modes, allowed_brokers, asset_classes, allowed_strategies
- max_capital, max_order_value, max_daily_orders

The effective profile is the tighter of the tenant's and the user's.

**Enforced:**
- **middleware:** `execution:trade` needs trading enabled and the current mode allowed.
- **W4 risk engine** (`tenant_profile` check): the strategy's tenant must have trading enabled and allow the strategy (else BLOCKED); `max_order_value` caps quantity.

## Notifications, workspace, billing, audit

| Area | What exists |
|---|---|
| Notifications | In-app inbox and per-category opt-out; risk events from the W4 cycle; alert-rule hits; Telegram only via the owner's bot for default-tenant users who opt in; no e-mail |
| Workspace | Watchlists (own or shared), alert rules on any W3/W6/ML feature (evaluated post-market), saved report definitions |
| Billing | Plans FREE / PRO / ENTERPRISE (limits, features; no prices), subscriptions, daily usage metering, DRAFT invoices |
| Audit | `enterprise_audit`: every mutating request, every denial, and all account / tenant / role / key / billing events; secrets redacted |

## Enabling (owner)

1. `python -m enterprise bootstrap --username <you>`: prompts for a password; creates the default tenant and its SUPER_ADMIN.
2. `config.json`: `"enterprise": {"enabled": true}`.
3. Restart ATIP (`python main.py`).
4. Sign in at `http://127.0.0.1:8000/login`.
