# Billing through Razorpay (W39b: ENT-04, API-04)

**Decision:** the owner delegated the payment-gateway choice. ATIP uses **Razorpay**: UPI, cards and netbanking, a native Subscriptions API (UPI Autopay, card and eMandate mandates), and wide use by Indian SaaS.

**Status:** IMPLEMENTED BUT NOT VERIFIED against a real Razorpay account. The integration is complete and tested with recorded Razorpay responses (`tests/test_w39b_razorpay.py`, `tests/fixtures/razorpay_v1.json`). **It is inert until the owner configures it:** with no credentials, every collection is recorded `REFUSED`, the invoice stays `OPEN`, no dunning starts, and the webhook endpoint answers 503.

Code: `enterprise/razorpay.py` (provider, webhook mapping, reconcile), `enterprise/payments.py` (provider selection), `db/schema_billing.py` (`enterprise_billing_ref`).

## What the owner must provide

| # | Item | Notes |
|---|---|---|
| 1 | A Razorpay business account, **KYC activated** | Business PAN, bank account, business proof, GSTIN if registered. Razorpay also checks for a website with terms, privacy, refund / cancellation and contact pages (see ENT-14 items TERMS and PRIVACY). |
| 2 | **Invoices** and **Subscriptions** enabled on the account | Subscriptions is enabled on request for some accounts. Autopay needs it; one-off invoices need only Invoices. |
| 3 | **Test** API key id and secret (`rzp_test_...`) | Razorpay Dashboard → Account & Settings → API Keys, in Test Mode. |
| 4 | **Live** key id and secret (`rzp_live_...`) | Only after test mode has been run through. Live also needs `allow_live` and environment `production` (below). |
| 5 | A **webhook** URL and secret | URL: `https://<public host>/api/webhooks/razorpay`. Razorpay must reach it over HTTPS from the internet, which is the **ENT-07 exposure decision** (still BLOCKED). Until then payments settle by polling (see Reconcile). |
| 6 | GST treatment of subscription invoices | ENT-14 item PAYMENTS-TAX. ATIP sends untaxed line items. Tax rates and GST invoices are not built. |

Development never sees or enters these values. The owner types them into ATIP's secret store.

## Switching it on (test mode first)

1. Store the credentials in the encrypted vault. Each value is read from stdin, never from argv or a log:
   ```
   python -m ops keygen                         # once, if ATIP_ENCRYPTION_KEY is not set yet
   python -m ops vault-set RAZORPAY_KEY_ID      # paste rzp_test_..., Enter
   python -m ops vault-set RAZORPAY_KEY_SECRET
   python -m ops vault-set WEBHOOK_SECRET_RAZORPAY
   ```
   The environment, `.env` or `atip_data/secrets/<NAME>` also work (ops/secrets.py). **config.json is never read for these.** If a credential appears under `billing.razorpay`, the config check reports an error.
2. Select the provider in `atip_data/config.json`. Use either the W39b section or the existing W9 key `saas.payments.provider`; `billing` wins when both are set:
   ```json
   "billing": {"provider": "razorpay",
               "razorpay": {"allow_live": false, "period": "monthly", "interval": 1, "total_count": 120,
                            "auth_link_days": 7, "notify": true, "webhook_max_age_hours": 72}}
   ```
3. Price the plans. Prices start as NULL and are never invented: `PUT /api/admin/plans/{PRO|ENTERPRISE} {"price_month": 999}`.
4. Restart ATIP. Then check:
   - `GET /api/admin/payments/provider` should show state **TEST**;
   - `/admin/console` shows the same line;
   - `GET /api/ops/status` lists any config findings.
5. Run it through in test mode, with Razorpay's test UPI ids (e.g. `success@razorpay`) or test cards:
   - **One-off invoice:** in `/app`, Billing → Pay. This opens the Razorpay invoice page. After paying, run `POST /api/admin/payments/reconcile` (or wait for the daily job, or press Pay again). The invoice turns PAID.
   - **Autopay:** in `/app`, Billing → Start autopay. This opens the mandate page. Once authorised, reconcile marks the subscription ACTIVE on Razorpay autopay and records each charge as a PAID invoice.
6. **Webhook** (after ENT-07). Add a webhook in the Razorpay Dashboard:
   - URL: the endpoint above;
   - secret: the same value as `WEBHOOK_SECRET_RAZORPAY`;
   - events: `payment.captured`, `payment.failed`, `invoice.paid`, `subscription.activated`, `subscription.charged`, `subscription.pending`, `subscription.halted`, `subscription.cancelled`, `subscription.completed`, `refund.processed`.
7. **Live:**
   - `vault-set` the `rzp_live_` key id and secret;
   - set `"billing": {"razorpay": {"allow_live": true}}` (a real JSON `true`) and run with environment `production`;
   - sign off ENT-14 PAYMENTS-TAX.

   Test-mode customers, plans, invoices and subscriptions do not carry over: Razorpay ids exist in one mode only, and ATIP keeps them per mode.

**Live is refused** (state `LIVE_REFUSED`, nothing is called) unless `allow_live` is true **and** the environment is production. Config validation reports these cases:

| Case | Finding |
|---|---|
| A `rzp_live_` key outside production | error |
| `allow_live` outside production | error |
| A live key in production without the switch | warning (refused) |
| Test mode in production | warning |
| No webhook secret | warning (polling only) |
| No credentials | warning |

## How it works

**One-off invoices.**
- `payments.run_cycle` drafts and finalizes the invoice (as before).
- `collect()` asks the provider. Razorpay issues an invoice for the ATIP invoice (`POST /v1/invoices`, amounts in paise, INR) and returns its pay link. The attempt is `PENDING`.
- `receipt` = `<invoice_id>-<attempt>`. Before creating an invoice, ATIP checks its own reference and `GET /v1/invoices?receipt=`, so a crash never issues a second invoice.
- A dunning retry for a still-payable invoice reuses the same link, with an e-mail reminder.

**Autopay.**
- `POST /api/billing/autopay` creates a Razorpay plan for the ATIP plan's price and a subscription. The customer authorises the mandate at its `short_url`.
- On activation, `enterprise_subscription.payment_provider = 'razorpay'`. From then on ATIP's billing cycle skips the tenant (Razorpay bills it), and every charge becomes a PAID ATIP invoice.
- A plan change on autopay is refused: cancel autopay (`POST /api/billing/autopay/cancel`), change the plan, then start autopay again.

**Webhook mapping** (`POST /api/webhooks/razorpay`).
- **Verification:** `X-Razorpay-Signature` is the HMAC-SHA256 of the raw body with `WEBHOOK_SECRET_RAZORPAY`, compared in constant time. A bad signature gets 401, a REJECTED row in `ops_webhook_event` and a log warning.
- **Idempotency:** `X-Razorpay-Event-Id` makes each event processed once. The same body under a new id is also a duplicate, because the id header is not signed.
- **Replay window:** events older than 72 h are refused.
- **Event status:** each event row ends PROCESSED, IGNORED or FAILED.

| Event | Effect on ATIP |
|---|---|
| `payment.captured`, `invoice.paid` | The ATIP invoice becomes PAID and the attempt SUCCEEDED. Subscription ACTIVE, dunning and grace cleared. For a subscription invoice, `invoice.paid` records the cycle's PAID invoice. |
| `payment.failed` | The attempt becomes FAILED. Subscription PAST_DUE / `RETRY_n` and the grace period starts. IGNORED once the invoice is PAID (a late failure). |
| `subscription.activated` | ACTIVE, `payment_provider` razorpay, period end, dunning cleared. |
| `subscription.charged` | The cycle's PAID invoice and SUCCEEDED payment, then as activated. |
| `subscription.pending` | PAST_DUE / `PROVIDER_RETRY` (Razorpay is retrying); grace starts. |
| `subscription.halted` | PAST_DUE / `HALTED` (Razorpay stopped retrying). When grace runs out, `run_dunning` expires the subscription and suspends the tenant. |
| `subscription.cancelled` / `completed` | CANCELLED / EXPIRED (term over, no suspension). |
| `refund.processed` | Payment PARTIALLY_REFUNDED or REFUNDED; the invoice becomes REFUNDED when fully refunded. |
| anything else; payments that are not ATIP's | IGNORED |
| notes naming an unknown ATIP invoice / tenant | FAILED (its writes rolled back) |

**Ordering and duplicates.**
- Money events apply once per Razorpay payment or refund id, whatever the order. A cycle reported by `subscription.charged`, `invoice.paid` and `payment.captured` is one invoice.
- A subscription status event older than the newest one applied does not change the status. `charged` before `activated` is fine, and a stale `halted` cannot undo a newer charge.
- Period ends only move forward.
- Events for a subscription the tenant has since replaced do not touch the tenant.
- A payment after the grace period lifts a **billing** suspension. An administrator's suspension stays.

**Reconcile.** This is the path that works today, with no public webhook. `reconcile()` runs:
- in `saas_daily` (06:30, before dunning);
- from `POST /api/admin/payments/reconcile` (platform admin; also a button in `/admin/console`).

It fetches the Razorpay invoice of every OPEN ATIP invoice, and each tenant's current subscription with its paid invoices. It applies them exactly as the webhook would. Pressing Pay on an invoice that was paid at Razorpay also settles it.

**Errors.**
- A rejected or unreachable request is recorded as an `ERROR` attempt: nothing was charged and no dunning starts.
- A circuit breaker (`ops/resilience`) stops hammering Razorpay when it is down.
- Credentials never appear in errors, logs, the database or any API.

## Not built

- GST tax lines and GST invoices (ENT-14 PAYMENTS-TAX).
- Proration or plan changes on autopay (cancel and restart).
- Partial payments and non-INR currencies.
- Refunds started from ATIP (do them in the Razorpay Dashboard; the webhook records them).
- Stripe (still refused).
- Razorpay Checkout.js: customers pay on Razorpay's hosted invoice and mandate pages, so ATIP never handles card or UPI data.

## API

| Method | Path | Permission | Notes |
|---|---|---|---|
| POST | `/api/billing/invoices/{invoice_id}/pay` | admin:billing | Razorpay: `{"status": "PENDING", "pay_url": ...}` |
| POST | `/api/billing/autopay` `{plan_id?}` | admin:billing | `{subscription_id, status, short_url}` |
| POST | `/api/billing/autopay/cancel` `{at_cycle_end}` | admin:billing | default at the end of the cycle |
| GET | `/api/admin/payments/provider` | admin:billing | state / mode / masked key id, never a secret |
| POST | `/api/admin/payments/reconcile` | admin:billing, platform admin | |
| POST | `/api/webhooks/razorpay` | public, signed | |
