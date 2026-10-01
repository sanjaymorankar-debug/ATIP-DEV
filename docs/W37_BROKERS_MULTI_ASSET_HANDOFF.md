# W37 — Brokers and multi-asset: handoff

**Branch:** `w37-brokers-multi-asset` (worktree `D:\Projects\ATIP-dev-w37`), on top of `w36-ml-strategy-tooling` → `master` `eb6f282`.

**Scope:** ENT-06 broker credential vault, BR-07 additional brokers, PF-12 multi-broker import and ENT-15 multi-asset trading.

**Status:** developed. 9 new tests and the full suite pass (**508/508**). The page and routes were smoke-tested on a throwaway database. Not merged and not deployed.

**Safety:**
- No LIVE order can be sent through anything here. The new order adapters build the request and refuse to send it, exactly like the Dhan adapter since W4.
- The options book is paper-only and off by default (`options.enabled`).

## ENT-06 — Per-user broker credential vault (completes the W9 foundation)

W9 shipped AES-256-GCM storage, an account page and write-only routes, but nothing used it. W37 adds:

**Expiry:**
- Each credential stores `expires_at`. Dhan, Zerodha and Upstox access tokens default to 24 h.
- Expired credentials are reported and never used.
- A daily 08:10 reminder alerts on credentials that expire within 16 h.

**`credentials_for(conn, tenant, user, broker, purpose)`** is the only way a connector or importer gets a credential:
- It takes the newest ACTIVE, unexpired entry and decrypts it server-side.
- Every use is audited (`vault.accessed` with the purpose) and counted.
- For the owner's default tenant with no vault entry, it falls back to the owner's existing Dhan / Zerodha configuration, labelled `source: config`.

**`verify(credential_id)`** makes a read-only profile call through the connector. The result is stored in `last_verified_at` / `verify_status` / `verify_detail`. Endpoint: `POST /api/account/vault/{id}/verify`.

**Key rotation:** `ops/crypto.rotate_key` now re-encrypts per-user broker credentials too. Before, it covered only MFA secrets and the owner's secrets file.

## BR-07 — Additional brokers (`brokers/`)

**Read-only connectors** for four brokers, normalised to `Holding` / `Position` / `Trade`:

| Broker | Library |
|---|---|
| Dhan | dhanhq |
| Zerodha | kiteconnect, installed |
| Upstox | REST v2 |
| Angel One | SmartAPI REST, logging in with PIN plus a TOTP generated from the stored secret |

Each connector offers `profile()`, `holdings()`, `positions()`, `funds()` and `trades()`. They have no write methods.

**Order adapters** (W4 `BrokerAdapter` interface):
- `build_payload(order)` maps an OMS order to the broker's request body.
- submit / cancel / modify / status raise `LiveTradingDisabled`.
- `POST /api/brokers/payload-preview` shows what would be sent.
- Turning live orders on is an owner and legal (ENT-14) decision.

**Consolidated view:** `consolidated()` merges holdings across every broker with a usable credential and keeps the per-broker breakdown. It is the Accounts tab on `/brokers`.

## PF-12 — Multi-broker import (`portfolio/imports.py`)

**File import:** `parse()` previews the file, then `commit()` writes it. It accepts tradebook, order-history and holdings CSVs from:
- Zerodha Console
- Upstox
- Groww
- ICICI Direct
- Angel One
- any CSV with recognisable headers

**How rows are read:**
- Columns are matched by aliases (case and punctuation insensitive).
- Symbols are validated against NSE symbol format. Otherwise the ISIN is looked up in the newest NSE bhavcopy, then the company name. Rows that can't be resolved are reported, never guessed.
- Cancelled, rejected or failed orders are skipped.

**What gets written:**
- Tradebook rows become BUY / SELL in the wealth ledger (portfolio LIVE, source `import:<broker>`).
- **Importing the same file twice adds nothing.**
- Holdings files open positions only once per broker and symbol.
- Dhan files are refused while the Dhan daily sync feeds the ledger, because that would double count.

**API import:** `import_from_broker` pulls today's trades, and the holdings the first time, through the read-only connector.

Every import is recorded in `broker_import_run` and shown on the `/brokers` Import tab.

## ENT-15 — Multi-asset trading

**`execution/options_paper.py`** is a paper options book with option-specific risk rules:
- long premium only: SELL can only close what is held, so no option writing;
- whole lots, with the lot size from the F&O bhavcopy;
- premium caps per trade (2 %) and in total (10 %) of paper cash;
- no new positions on expiry day;
- only contracts that traded in the latest data.

**Pricing and settlement:**
- Fills use the latest option-chain snapshot (≤ 30 min old: ask to buy, bid to sell). Otherwise the EOD contract close plus or minus slippage.
- Fees are per-order brokerage plus STT on sell premium.
- Expiry settlement at intrinsic value runs daily at 16:20.
- The book is marked to market.
- Owner-entered orders only (`/api/execution/options/order`, with a `/check` dry run).

**`execution/assets.py`** lists every asset class and its path:
- EQUITY, ETF, FUTURE and OPTION are tradable on paper.
- COMMODITY, CURRENCY and BOND are explicitly research-only: there is no MCX / CDS / bond connection.

## Files

**New:**
- `brokers/` (`__init__`, `base`, `connectors`, `registry`)
- `portfolio/imports.py`
- `execution/options_paper.py`, `execution/assets.py`
- `db/schema_w37.py`
- `dashboard/w37_routes.py`, `dashboard/w37_page.py`
- `tests/test_w37_brokers_multi_asset.py`

**Changed:**

| File | Change |
|---|---|
| `enterprise/vault.py` | Expiry, resolver, verify, reminders |
| `ops/crypto.py` | Rotation covers broker credentials |
| `db/schema.py` | Applies the W37 tables / columns |
| `dashboard/server.py` | Routes + Brokers link |
| `enterprise/authz.py` | `/api/brokers` |
| `pipeline/scheduler.py` | Options expiry, vault reminder |
| `config_template.json` | `options` |
| `docs/ATIP_MASTER_TRACKER.csv`, `docs/WAVE_STATUS.md` | W37 rows |

## For QA

1. Run `python -m pytest -q`. Expect 508 passed.
2. **Vault** (after `python -m ops keygen`): store an Upstox or Zerodha credential on `/account`, then run verify. Check the audit rows. A `rotate-key` dry run should list `evault:secret_enc`.
3. **Connectors** (real accounts, read-only): `GET /api/brokers/zerodha/snapshot`. Compare with the broker's app, then check `/api/brokers/consolidated`.
4. **Import:** take the owner's real Zerodha tradebook and Groww order history. Preview, import, then import again (0 added). Check the ledger on `/wealth`.
5. **Options** (`options.enabled`, after an F&O session is stored): check and place a NIFTY CE; a naked SELL and an oversized order are refused. A past-expiry position settles at intrinsic value.

## Notes

- Upstox and Angel One fields follow their published APIs and were not exercised against real accounts here.
- The connectors are read-only, but the brokers' API terms apply to their use.
