# ATIP security architecture (as of W8)

## Boundary

- **Network:**
  - ATIP binds to 127.0.0.1 (`dashboard/security.py`).
  - Internet exposure (ENT-07) is **BLOCKED** pending an owner decision and a security review. W8 does not change the binding.
- **Legacy guard:** while the enterprise layer is off (the default), every mutating route needs `X-ATIP-Token`. Reads are open on localhost only.
- **Enterprise layer:** once enabled (W7), every request needs a principal:
  - a session cookie or Bearer token;
  - or an API key.
  - Routes map to exactly one permission (`enterprise/authz.py`), with 8 roles and 27 permissions.
  - Tenant isolation is enforced in the middleware.

## Authentication (W7 + W8)

| Control | Implementation |
|---|---|
| Passwords | PBKDF2-SHA256 (`enterprise/security.py`), a 12+ character policy, lockout after 5 failures for 15 min |
| Sessions | Random tokens with only the SHA-256 digest stored. 12 h expiry. HttpOnly, SameSite=Strict cookie. Revoked on logout, password change or disable |
| Refresh tokens (W8) | `atf_` tokens, digest stored, 14 days. **Rotated on every use.** Reusing a used token revokes the family and every session of that user. The cookie is scoped to `/api/auth/refresh` |
| MFA (W8) | TOTP (RFC 6238, SHA-1, 30 s, 6 digits, ±1 step). The secret is encrypted with AES-256-GCM. A code is accepted once. A wrong code counts toward lockout. Admins can reset it |
| API keys | Scoped, digest stored, expiry, revocable |
| Password reset | Admin-issued one-time token, 30 min |

## Authorization

- **RBAC:** see ENTERPRISE_ARCHITECTURE.md.
- **W8 rules:**
  - `/api/ops/*` requires `system:operate`.
  - MFA reset requires `admin:users`.
  - Health probes and signed inbound webhooks are public; they carry no sensitive data and are verified by HMAC.

## API security (W8, `ops/http.py`)

- **Request size:** 2 MB limit (413), including chunked bodies.
- **Mutating `/api` bodies:** must be `application/json` (415) and parse (400).
- **Rate limit:**
  - token bucket per client IP and credential digest (429 + `Retry-After`);
  - **off by default**, required in production.
- **Security headers:**
  - `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`;
  - `Cross-Origin-Opener-Policy: same-origin`, `Permissions-Policy` (camera, microphone, geolocation off);
  - `Cache-Control: no-store` on auth, account and admin;
  - `Strict-Transport-Security` only when `ops.tls_enabled`;
  - `Content-Security-Policy` only when `ops.csp` is set, because the built-in pages use inline scripts.
- **CORS:** none by default (same origin only). A wildcard origin is an error in production.
- **Idempotency:** `Idempotency-Key` stops duplicate orders and billing actions. Webhooks dedupe on event id.

## Secrets management (`ops/secrets.py`)

**Resolution order:**
1. the process environment;
2. `.env`;
3. `atip_data/secrets/<NAME>` (one file per secret; outside git);
4. the legacy `atip_data/config.json` key, which triggers a startup warning.

**Catalog:**
- `DHAN_CLIENT_ID`, `DHAN_ACCESS_TOKEN` (1-day rotation)
- `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`
- `ANTHROPIC_API_KEY`, `ALPHA_VANTAGE_KEY`
- `KITE_API_KEY`, `KITE_API_SECRET`
- `ATIP_ENCRYPTION_KEY`
- `WEBHOOK_SECRET_*`
- `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `WEBHOOK_SECRET_RAZORPAY` (W39b; no config.json location at all)

**Never:** a value logged, stored in a W8 table, returned by an API, printed by the CLI, or written to the config version.

**Access log:** `ops_secret_access` holds name, source, found and caller.

**Rotation:**
- `python -m ops rotate <NAME>` records the date, and an audit row is written.
- `python -m ops secrets` shows what is overdue.
- The owner performs the rotation itself. Development never handles credentials.

**Masking:** every log handler masks configured secret values and known token shapes (JWT, Telegram bot token, `sk-ant-`, ATIP `ats_`, `atr_`, `atf_`, `atk_`).

## Encryption

- **At rest (field level):**
  - AES-256-GCM (`ops/crypto.py`) with a 96-bit random nonce.
  - The AAD binds each ciphertext to its record (`enterprise_user.mfa:<user_id>`).
  - Format `enc:v1:<key id>:<b64>`.
  - The key is 32 random bytes in `ATIP_ENCRYPTION_KEY` (`python -m ops keygen` writes `atip_data/secrets/ATIP_ENCRYPTION_KEY` and never prints it).
  - With no key, encryption is refused and nothing falls back to plaintext.
  - The key id allows future rotation by re-encrypting.
- **Sensitive-field classification:**

| Class | Fields | Protection |
|---|---|---|
| Secret (never stored in plaintext by ATIP) | passwords, session / refresh / reset / API tokens | one-way hash (PBKDF2 / SHA-256 digest) |
| Secret (reversible, needed) | TOTP secret | AES-256-GCM |
| Credential (owner-managed) | Dhan / Telegram / Kite / Anthropic keys | secrets store; per-user vault = ENT-06 (not built) |
| Personal | username, email, display name, IP, user agent | access-controlled; data privacy = ENT-17 |
| Financial | positions, orders, P&L | tenant-scoped via authz |

- **Whole database:** SQLite is not encrypted at rest. The owner's disk encryption (BitLocker) is the control. Backups inherit it (W8-R2).
- **In transit:**
  - Today traffic is loopback only.
  - **TLS 1.3 architecture** for any future exposure: a reverse proxy (Caddy or nginx) terminates TLS 1.3 only, with modern ciphers (TLS_AES_128_GCM_SHA256 / TLS_AES_256_GCM_SHA384 / TLS_CHACHA20_POLY1305_SHA256) and automatic certificates, and forwards to 127.0.0.1:8000.
  - Set `ops.tls_enabled: true` for HSTS.
  - Outbound calls (Dhan, NSE, Telegram, webhooks) use HTTPS. Outbound webhooks require https outside development.

## Audit (tamper evidence)

- **Coverage:** `enterprise_audit` records:
  - every mutating API request, including when the enterprise layer is off (W8), with actor `local-token` or `anonymous`;
  - denials and auth events (login, failure, MFA, refresh, reuse);
  - account, tenant, role, key and billing events;
  - configuration changes (key names) and secret rotations.
- **Append-only triggers:** block UPDATE and DELETE on `enterprise_audit` and `oms_order_event`.
- **Hash chain:**
  - `row_hash = sha256(prev_hash | fields)`;
  - `python -m ops audit-verify` or `/api/ops/audit/verify` reports the first broken row.

## Webhooks

See `ops/webhooks.py`.
- **Inbound:**
  - HMAC-SHA256 over `timestamp.body`, compared in constant time;
  - timestamps older than 300 s are refused;
  - event ids dedupe;
  - 256 KB limit;
  - 503 when no secret is configured.
- **Razorpay (W39b, `/api/webhooks/razorpay`):** `X-Razorpay-Signature` = HMAC-SHA256 of the raw body with `WEBHOOK_SECRET_RAZORPAY`, constant time; a bad one is 401, recorded REJECTED and logged; `X-Razorpay-Event-Id` dedupes, and so does the body digest (the id header is not signed); events older than 72 h are refused. See docs/BILLING_RAZORPAY.md.
- **Outbound:** signed the same way, retried with backoff, DEAD letter, circuit breaker per host.

## Security scanning

`python -m ops scan`:
- secret patterns in git-tracked files (it reports the file, line and pattern, never the value);
- forbidden tracked files (`atip_data/`, `*.db`, `.env`, `secrets/`, keys);
- configuration and secret findings;
- `pip-audit` when installed (currently NOT RUN).

Run it before every release (PRODUCTION_DEPLOYMENT.md).
