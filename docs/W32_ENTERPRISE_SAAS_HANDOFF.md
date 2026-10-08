# W32: Enterprise SaaS handoff

**Status:** IMPLEMENTED BUT NOT VERIFIED. The tree compiles.

**Branch:** `w32-enterprise-saas`, from `w31-production-hardening`. It merges the earlier `w9-enterprise-saas` branch (3aae821), which had never been integrated with W10 to W31. Not merged to master, not deployed. ChatGPT testing is pending.

**Off by default:** everything here does nothing while `enterprise.enabled` is false (the default). The app stays local-only (127.0.0.1); internet exposure is still ENT-07 and an owner decision.

**Scratch smoke test** (`scratchpad/smoke_w32.py`): an isolated working folder with its own atip_data and a copy of the production DB, with enterprise enabled. It uses FastAPI TestClient only, with no network, and generated test credentials. It covered:
- **Accounts:** owner bootstrap → self-registration → activation → consent → e-mail verification (token taken from the sandbox outbox, MIME-decoded) → onboarding with a tenant paper account.
- **Account features:** notification preferences, vault validation, watchlist, report run (CSV), billing, privacy export request, admin console.
- **Isolation report:** 186 of 186 tables classified.
- **MFA recovery:** enrol MFA → generate recovery codes → log in with a recovery code → reuse of the same code refused (9 left).
- **Payments webhook:** signed → invoice PAID; event PROCESSED; a duplicate is acknowledged but not re-processed; an unknown payment gives event FAILED.
- **Intraday alert:** fires once, and does not fire again the same day.
- **SaaS jobs:** the tick / daily / digest jobs run.
- **Tenant order path:** a tenant strategy BUY → risk APPROVED but capped at 48 shares by the user's `max_capital` of Rs 50,000 → order FILLED into the tenant's own paper book, not the owner's. A tenant SHORT → BLOCKED (owner-book only).

## Delivered

| ID | Feature | Where |
|---|---|---|
| ENT-01 | Multi-tenant data isolation | W9 merge: per-tenant paper books (`execution/tenant_books.py`); tenant orders route to `TenantPaperAdapter` (`execution/adapters.get_adapter(..., tenant_id=)`); risk uses the tenant's own book. Every table is classified in `enterprise/scoping.py` (186/186); the W10–W31 tables were unclassified, so they failed closed for tenants |
| SEC-02 | MFA recovery codes | `enterprise/w32.py`; table `enterprise_mfa_recovery` (sha256 digests only). `GET/POST /api/account/mfa/recovery` (POST needs a current TOTP code). Login accepts a recovery code in place of the TOTP code; each code works once. WebAuthn not built (needs HTTPS origin = ENT-07, and a library) |
| ENT-02 | Registration / onboarding | W9 merge: e-mail verification (generic SMTP, off until configured; sandbox outbox otherwise), consent, onboarding checklist + tenant paper account. Verify link now `/app?verify=` |
| ENT-03 | Per-user capital accounting | `enterprise/w32.capital_usage` + risk check `profile_max_capital`. Deployed capital = open positions (oms_fill, at latest close) of the tenant's strategies, and of the strategies the user owns, against `max_capital` in the tenant and the user profile; the BUY quantity is capped by the smaller room. Strategies created by a signed-in user are now owned by that user (`strategy.owner` = user_id) |
| ENT-04 | Billing | W9 merge: invoices (DRAFT → OPEN → PAID), payment provider interface (sandbox / noop), dunning with grace. No real gateway: owner decision |
| ENT-08 | Multi-user web app | `/app` (user) and `/admin/console` (admin), `dashboard/saas_page.py`. Still local-only |
| ENT-10 | Notification channels | W9 merge: in-app, e-mail (SMTP), Telegram, webhook; per-category preferences, quiet hours, digests, unsubscribe links |
| ENT-11 | Intraday per-user alerts | `enterprise/w32.evaluate_alerts_intraday`: rules on close / price / ltp / change_pct / volume are evaluated on the newest live quote (≤ 20 min old) every 15 min in session; at most once a day per rule. Other features stay post-market |
| ENT-12 | Reports | W9 merge: render / export (HTML, CSV), schedules, stored outputs |
| ENT-13 | Admin console | W9 merge: tenants, onboarding, payments, dunning / billing-cycle runs, privacy decisions, API-key limits, isolation report |
| ENT-16 | Scalability | W9 merge: `ops/shared_state.py`, job leader lease (`ops/jobs.py`), `db/backend.py`, `tools/sqlite_to_postgres.py` (Postgres not adopted: DBS-05) |
| ENT-18 | Multi-device login | Session list + revoke (`/api/account/sessions`) |
| API-03 | Versioned public API | `python -m ops api-docs` now also writes the v1 contract (`docs/api/openapi-v1.json`) through `enterprise/public_api.write_docs`; per-key limits |
| API-04 | Webhook consumers | `enterprise/w32.consume`, run after `verify_inbound` accepts an event: `payments` (payment.succeeded / payment.failed by payment_id or provider_ref → invoice PAID / dunning; idempotent), `broker` (order.update by broker_order_id → OMS refresh). The event row's status becomes PROCESSED / IGNORED / FAILED. Endpoint admin: `GET/POST /api/ops/webhooks` |

### Scheduled jobs (`pipeline/scheduler._schedule_w32_jobs`)

None of these writes a pipeline_log row while `enterprise.enabled` is false.

| Job | When | Does |
|---|---|---|
| saas_tick | every 5 min, only when a delivery is QUEUED | sends deliveries whose quiet hours have ended |
| alerts_intraday | every 15 min in market hours | ENT-11 |
| saas_daily | 06:30 | billing cycle, dunning, scheduled reports, privacy retention purge |
| saas_digest | 19:00 | digest deliveries |

## Merge notes (W9 branch into W31)

- **Schema:** `oms_order` additive columns merged into one entry (`tenant_id`, `trigger_price`, `parent_order_id`, `modified_count`, `instrument`). The duplicate key (a W29 regression) had silently dropped `tenant_id` on fresh installs.
- **Adapters:** a tenant's FUT order → BrokerError; tenants are PAPER only. In the risk engine, W1 limits apply to the owner's book only, and futures short legs are owner-book only (`_futures_leg`).
- **authz:** route families `notifications` and `billing` were added to the account pattern.

## Testing scenarios for ChatGPT

1. **Off by default:** with enterprise disabled, every `/api/account/*`, `/app` and `/admin/console` route returns 503 / is disabled, and no W32 job writes pipeline_log.
2. **Isolation:**
   - with two tenants, each sees only its own book, strategies, orders, watchlists, reports, alerts and deliveries;
   - a guessed id of another tenant's resource returns 404.
3. **Recovery codes:**
   - generation needs a valid TOTP;
   - a code works once and in place of the TOTP code;
   - regenerating invalidates the old set;
   - a wrong code counts toward lockout.
4. **ENT-03:**
   - user and tenant `max_capital` cap BUY quantity;
   - SELL is never capped;
   - with no `max_capital` the check is absent.
5. **Payments webhook:**
   - signature, replay window and duplicate checks;
   - succeeded / failed transitions;
   - a second delivery of the same event changes nothing.
6. **Intraday alerts:**
   - a stale quote (> 20 min) does not fire;
   - a rule fires once per day.
7. **Tenant execution:**
   - LIVE refused for tenants;
   - SHORT blocked;
   - fills hit `tenant_paper_*` and never `paper_*`.

## Owner actions

- Decide on ENT-07 (TLS, exposure) before anyone outside this machine can use the web app.
- ~~Choose a payment gateway (ENT-04).~~ W39b: **Razorpay** (decision delegated by the owner) is built and inert until configured. The owner still provides the Razorpay account, KYC and the three secrets: see [BILLING_RAZORPAY.md](BILLING_RAZORPAY.md).
- Set the SMTP settings (`channels.email`) to send real e-mail.
- Set webhook secrets (`WEBHOOK_SECRET_PAYMENTS`, `WEBHOOK_SECRET_BROKER`) only when a provider is chosen. Razorpay uses its own, `WEBHOOK_SECRET_RAZORPAY`.
