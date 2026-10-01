# ENT-14 — Regulatory compliance pack

**Status: IN PROGRESS. This is not legal advice.**

Engineering prepared this to brief a qualified professional: a SEBI-registered compliance officer or a lawyer practising securities and data-protection law. Software cannot make ATIP compliant; a reviewer must confirm each item.

The live register is the **Regulatory** tab on `/compliance` (table `regulatory_item`, `ops/regulatory.py`):
- each item records its status, the reviewer and a reference;
- `SIGNED_OFF` and `NOT_APPLICABLE` cannot be saved without a reviewer and a reference;
- every change is written to the audit trail.

The compliance check `regulatory_signoff` **fails** if either of these happens while items in that gate are open:
- the multi-user platform is turned on (`enterprise.enabled`);
- live trading is turned on.

## Where ATIP stands today

- One owner, paper trading only. The master switch is off (`execution.live_trading_enabled`).
- Signals and scores are shown only to the owner.
- No payment is taken and no other person's data is held.

Most items below become binding only when one of those changes. That is why they are gated, not "due now".

## Items

| ID | Gate | What to confirm |
|---|---|---|
| SEBI-RA-IA | before other users | Showing ATIP's BUY/SELL signals, scores or model portfolios to others may be "research" (SEBI Research Analysts Regulations, 2014) or "investment advice" (SEBI Investment Advisers Regulations, 2013). Which registration or exemption applies, and what disclosures it requires. |
| NSE-DATA | before other users | Showing exchange prices, indices or bhavcopy-derived data to third parties can need a data-vendor or redistribution licence. Confirm what each source permits: NSE files, broker feeds, yfinance. |
| DPDP | before other users | The Digital Personal Data Protection Act, 2023 and the Rules in force: notice and consent content, the inventory, retention, data-principal rights and response times, grievance officer, breach notification, processor contracts. The draft notice at `/privacy` is generated from the inventory. |
| CERT-IN | before other users | CERT-In directions (2022): incident-reporting window, log retention and NTP sync for this service and its hosting. |
| TERMS | before other users | Terms of service, limitation of liability, "model output, not investment advice" wording, the risk disclosure shown at sign-up. |
| PAYMENTS-TAX | before other users | Payment-aggregator terms (ATIP never stores card data); GST registration and invoicing on subscriptions. |
| SEBI-ALGO | before live trading | SEBI's framework for retail algorithmic trading (Feb 2025 circular and the exchange implementation standards). It covers broker approval or empanelment, algo registration and IDs, order-per-second thresholds, static-IP API access and 2FA, plus the dates now in force. |
| BROKER-API | before live trading | Each broker's API terms: personal use only or serving other users' accounts, automated-order permissions, rate limits. |
| LIVE-RISK | before live trading | The owner reviews and accepts the order limits, kill switch, loss and drawdown limits, per-second order cap and reconciliation. Today the compliance check reports `risk_limits` as **not configured**. |
| REVIEW-ANNUAL | ongoing | Re-confirm every item once a year and after any regulatory change. |

## Technical controls already in place

The reviewer can rely on these as evidence. Each one is checked daily by `ops/compliance.py`.

- **Tamper-evident audit trail:** a hash chain plus append-only triggers on audit and ledger tables.
- **Live trading:** off unless an explicit master switch is set in production. Every broker adapter refuses to send orders.
- **Order limits and kill switch:** `orders/risk.py`.
- **Encryption:** AES-256-GCM for stored credentials; per-user vault with an audited purpose for every use.
- **Account security:** MFA, sessions, RBAC on every route when multi-user.
- **Privacy:** data inventory covering every table, export / delete requests with a response SLA, retention purge.
- **Backups:** verified backups, restore drills, encrypted off-site copies.

## Owner actions found by the first compliance run (2026-10-02)

1. **Set order limits.** Configure `config.json` → `risk_limits`, at least `max_order_value`, `max_orders_per_day` and `max_open_positions`. Needed before any live order.
2. **Move secrets out of `config.json`.** Seven are still there in plaintext. Move them to `atip_data/secrets/` or the environment.
3. **Create an encryption key.** Run `python -m ops keygen` and store `ATIP_ENCRYPTION_KEY`. Without it the vault doesn't work and off-site backups are unencrypted.
4. **Run a restore drill.** Use `python -m ops restore-drill`; none has been recorded.
