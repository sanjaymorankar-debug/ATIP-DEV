# W33: QA / security / performance and UAT handoff (QA-001, UAT-001)

**Status:** development preparation complete; independent testing (ChatGPT) and UAT have not started.

**Branch:** `w33-wealth-qa`, from `w32-enterprise-saas`. This branch is the **release-candidate line**: it contains the wealth track (W11–W20, already gated by W18 QA and the W19 UAT plan) and W21–W32. Nothing after ATIP-W24-RC1 is merged to master or deployed.

W18 (`W18_QA_SECURITY_PERFORMANCE_REPORT.md`) and W19 (`W19_UAT_PLAN.md`) still apply to the wealth features. This document extends them to everything delivered in W25–W32, which has only been smoke-tested by development on scratch copies of the production DB.

## 1. Security review (static, OWASP-oriented, W25–W32 surfaces)

| ID | Finding | Severity | Status |
|---|---|---|---|
| S-W33-1 | **Open redirect after login:** the login page accepted any `next` beginning with `/`, including `//host` (protocol-relative) and `/\host` | Medium | FIXED: only same-origin paths (`/x`, not `//` or a backslash); `dashboard/enterprise_page.py` |
| S-W33-2 | **Query lost on the sign-in redirect:** a signed-out user opening `/app?verify=<token>` lost the token, so the e-mail verification link failed | Medium (functional) | FIXED: the query string is kept and encoded into `next`; `enterprise/authz.py` |
| S-W33-3 | **Unescaped NSE strings:** two NSE-sourced fields (F&O `kind`, insider `txn_type`) went into innerHTML without escaping | Low | FIXED: `esc()`; `dashboard/market_page.py`. The other interpolations on the W27–W32 pages are numbers, server-generated ids or constants |
| S-W33-4 | **Mislabelled API reference:** public routes (login, register, webhooks, health, unsubscribe) and self-service auth routes showed the fallback permission | Low (docs) | FIXED: shown as `public` / `signed-in`; `ops/api_docs.py` |
| W32-1 | **Lost column on fresh installs:** a duplicate `oms_order` additive-column key dropped `tenant_id` | High | FIXED in W32 |
| W32-2 | **Unclassified tables:** tables added in W10–W31 were not classified for tenant scoping (they failed closed) | Medium | FIXED in W32 (186/186) |

**Checks that passed** (all derived from the generated `docs/API_REFERENCE.md`, 400+ method + path pairs):
- **Permissions:**
  - every mutating route requires a write / operate / lifecycle permission, never a `:read` one;
  - every mutating route outside the account / auth / admin / billing families also needs the dashboard token.
- **Public routes:** exactly login, register, reset, refresh, forgot, verify-email, unsubscribe (signed link), health probes and HMAC-verified webhooks.
- **Webhooks:**
  - HMAC-SHA256 signature, 300 s replay window, event-id dedupe;
  - consumers are idempotent (a settled payment is never re-settled).
- **Secrets:**
  - the MFA recovery codes table holds sha256 digests only;
  - TOTP secrets are AES-GCM encrypted;
  - credentials go in the encrypted vault (W31);
  - the Anthropic key is read from env only, and the news AI is off by default with a daily cost cap.
- **Tenant isolation:**
  - single-resource 404 and list filtering in the authz middleware;
  - tenants are PAPER-only and have no futures legs;
  - W1 limits apply to the owner's book only.

**For QA to verify dynamically** (static review can't prove these):
- isolation between two tenants on every `/api/account/*`, `/api/tenant/*`, `/api/reports/*` route;
- lockout counting for wrong TOTP and recovery codes;
- refresh-token reuse detection;
- CSRF posture: SameSite cookie plus token header;
- rate limits per API key.

## 2. Performance: what to measure

Use a 70 MB production copy, as W18 did.

| Area | Concern | Expectation |
|---|---|---|
| `enterprise/w32.capital_usage` | Replays every `oms_fill` on each BUY risk check while a `max_capital` is set | < 200 ms at today's fill count; flag if fills exceed ~50k |
| `alerts_intraday` (every 15 min) | One live_quotes query per ACTIVE rule | < 2 s for 1,000 rules |
| News AI batch (W28) | Haiku 4.5 calls; daily cost cap | One batch per news run; stops at the cap and falls back to rules |
| NSE filings / PIT / SAST ingestion (W27) | Polite rate limit + circuit breaker | Full fundamentals backfill is a long owner-run job, not a scheduler task |
| Stock live feed (W27, off) | WebSocket with REST failover, max 100 symbols, flush every 60 s | CPU < 10% on the owner's machine |
| Rollback drill (W31) | RTO | 47.6 s measured; repeat on the RC line |

## 3. QA matrix: what to test per wave

All features are **off by default** unless stated. Enable them in a scratch install, never on production.

| Wave | Enable | Key scenarios |
|---|---|---|
| W25 portfolio risk | — | VaR / ES / stress / limit checks; risk limits reject correctly (W25 handoff) |
| W26 dashboard history | — | Signals history pages render; pagination |
| W27 data and scores | `fundamentals.score_enabled`, `institutional.score_enabled`, `live_feed.stocks_enabled` | See below |
| W28 strategy and AI | `news_ai.enabled` (needs a valid key, KD-001), intraday scans | See below |
| W29 execution | paper | See below |
| W30 advanced quant | `futures.enabled` (paper) | See below |
| W31 hardening | `ops.backup_offsite_dir`, keygen | Encrypted backup → off-site → restore drill; tampered file refused; vault migrate dry-run vs apply; key rotation keeps old backups readable; `ops rollback-drill` PASSED |
| W32 enterprise | `enterprise.enabled` | W32 handoff, "Testing scenarios" 1–7 |
| W33 fixes | — | `/login?next=//evil.example` stays on `/`; a signed-out `/app?verify=x` → login → back to `/app?verify=x`; insider / F&O tables escape markup |

**W27 data and scores**
- **Point-in-time fundamentals:** a quarter's numbers are not used before `available_from`.
- **Scores with too few inputs:** FS / SPI are None when fewer than 3 inputs.
- **Scores off:** with both switches off, the scores are byte-identical to before.
- **MSI:** uses the real PCR.
- **/market page:** loads every section with the feeds off.

**W28 strategy and AI**
- **No lookahead:** intraday scans never use a bar that closes after `now`.
- **Classification:** regulatory headlines are classified before orders.
- **Cost cap:** stops AI calls, and the rule fallback is labelled "not AI".
- **ML activation:** needs `strategy:lifecycle`.

**W29 execution**
- **Stop orders:** SL / SL-M trigger correctly; a LIMIT fill is never worse than its limit.
- **Modify / cancel:** the state machine refuses invalid transitions.
- **Protective stop:** references the entry fill price.
- **Reconciliation:** reports breaks.
- **Broker health gate:** RK-17 rejects BUYs while DOWN / STALE; exits are never blocked.
- **Sandbox:** uses only its own credentials.
- **Audit export:** runs daily.

**W30 advanced quant**
- **Short round trip:** SHORT → COVER through the paper futures book; P&L includes both fees.
- **Short sizing:** fewer than one lot is REJECTED.
- **Tenant shorts:** BLOCKED.
- **IV rank:** stays within 0..100.
- **Event study:** day 0 within 4 days; the gap guard holds.

**Wealth (W11–W20):** follow W18 §1 and W19 §3.

## 4. UAT (extends W19)

| Persona | Setup | Journeys and acceptance |
|---|---|---|
| Wealth personas (W19) | `wealth/uat.py` seed, tenant `uat` | As in W19 §3 |
| Owner / platform admin | `bootstrap` | Activates users; manages tenants, plans, dunning and privacy decisions in `/admin/console`; sees the isolation report at 186/186; can always reach production pages |
| Tenant admin (self-registered) | `/login` → register | Registers → is activated → accepts terms → verifies e-mail from the link (also when signed out) → onboarding sets up a paper account → watchlist, alert, report, notification preferences → sees only their own tenant |
| Tenant trader | Strategy owned by the user; user `max_capital` set | A BUY is capped by the remaining capital; the fill lands in the tenant book; SHORT blocked; LIVE refused |
| Security-conscious user | MFA | Enrols TOTP → generates recovery codes → signs in with one code once → regenerates; lost-device recovery path works |

**Feedback:**
- Use the W19 feedback / triage flow (`wealth/uat.py`).
- Report SaaS defects with tenant id and request id (from the error envelope).

## 5. Exit criteria (gate to a release candidate after W24-RC1)

1. ChatGPT QA passes §1 (dynamic checks), §3 for every wave, and the W18 suite. No open High; every Medium is fixed or accepted by the owner.
2. Performance numbers in §2 recorded on the production copy.
3. UAT journeys in §4 accepted by the owner.
4. A rollback drill from the RC tag to ATIP-W24-RC1 has PASSED.
5. **Owner authorization** to tag, merge to master and deploy. Development does not deploy.

## 6. Owner actions outstanding (all waves)

- **Keys and vault:** `python -m ops keygen` (keep a copy of the key off this machine), then `vault-migrate --apply`, and set `ops.backup_offsite_dir`.
- **Anthropic key (KD-001):** the key in `.env` returns 401, so news AI stays off until it is replaced.
- **Dhan sandbox:** token for the sandbox checks (BR-08).
- **Backfills:** NSE fundamentals and IV (long-running; see the W27 / W30 handoffs).
- **AD-03:** review the bulk / block-deal finding before enabling `institutional.score_enabled`.
- **Enterprise:** ENT-07 exposure and the payment gateway (ENT-04); SMTP settings.
- **Deployment:** authorization for any deployment.
