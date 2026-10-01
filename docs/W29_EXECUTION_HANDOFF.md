# W29: Execution handoff

**Status:** IMPLEMENTED BUT NOT VERIFIED. The tree compiles, and the /trading script passes `node --check`.
- **Scratch end-to-end run** (copy of the production DB):
  - paper SL-M, LIMIT, the matching loop and modify;
  - an OMS LIMIT order: risk decision → submit (rests) → modify → match → refresh to FILLED → protective SL-M;
  - reconciliation, analytics, broker health (network off), live P&L and audit export.
- **API:** every new endpoint was exercised through TestClient.

ChatGPT testing is pending.
**Branch:** `w29-execution`, from `w28-strategy-ai`. Not merged, not deployed.
**Safety:** **live execution is still not built.** The Dhan adapter refuses submit, cancel, status *and* the new modify. Protective stops are paper-only. Kite order placement is not built.

## Delivered

| ID | Feature | Where |
|---|---|---|
| EX-02 | Stop-loss / stop-limit | See "Stop orders" below |
| EX-08 | Order modify | `order_manager.modify_order`. See "Order modify" below |
| BR-05 | EOD reconciliation | `execution/reconcile.py`. See "Reconciliation" below |
| BR-06 | Connection health | `execution/broker_health.py`. See "Broker health" below |
| RK-17 | Broker / operational risk | New risk check `broker_health`: a BUY is REJECTED while the latest check (≤ 30 min old) is DOWN or STALE. SKIP when there is no recent check. Exits are never blocked. Switch: `execution.block_on_broker_health` (default true) |
| BR-08 | Sandbox | See "Sandbox" below. **Blocked on a Dhan-issued sandbox token** |
| EX-09 | Measured slippage | `execution/analytics.slippage`: per fill, bps vs the decision reference (vs the trigger for stops), positive = adverse, plus rupee cost |
| EX-10 | Execution analytics | `analytics()`: fill rate (qty and orders), rejection rate with ranked reasons, cancels / failures, time to fill p50 / p90 (from order events), slippage mean / median / p90 / worst, cost and fees; by strategy, order type, mode and day. /trading panel |
| BR-02 | Zerodha / Kite | See "Zerodha session" below |
| MON-04 | Live P&L | `portfolio/live_pnl.py`. See "Live P&L" below |
| SEC-03 | Audit export / retention / off-box | `enterprise/audit_export.py`. See "Audit export" below |

### Stop orders (EX-02)

- **Types:** SL and SL-M in the paper broker and the OMS (`order_type` / `limit_price` / `trigger_price` per order, validated).
  - SL rests until its trigger, then becomes a LIMIT.
  - SL-M rests until its trigger, then fills as a MARKET order.
- **Paper matching loop** (`execution/paper_matching.py`, every 2 min in the session, only when something is pending): fills resting LIMIT / SL / SL-M orders against the newest `live_quotes` price, then refreshes the OMS orders. This fixes W4-R4: resting paper LIMITs used to stay ACKNOWLEDGED forever.
- **Protective stops:** an optional child SL-M (or SL) after a BUY fills, at the intent's stop price.
  - Switch: `execution.protective_stops`, default off.
  - Children carry `parent_order_id` and the synthetic intent / risk ids `PSTOP-<parent>`, because those columns are NOT NULL UNIQUE.
- `POST /api/oms/orders` accepts `order_type`, `limit_price` and `trigger_price`.

### Order modify (EX-08)

- **What changes:** quantity, limit price, trigger price and order type.
- **CREATED / VALIDATED orders:** edited in place.
- **ACKNOWLEDGED (resting) orders:** changed through the adapter.
- **Quantity can only go down:** raising it needs a new risk decision.
- **Audit:** every change is an append-only event (old → new).
- `POST /api/oms/orders/{id}/modify`, plus a Modify button on /trading.

### Reconciliation (BR-05)

- **PAPER positions:** OMS fills vs `paper_position`. Differences caused by non-OMS paper orders are "explained".
- **Orders:** OMS status and filled quantity vs `paper_order`.
- **Fill rows:** checked against each order's filled quantity.
- **LIVE holdings:** listed as information only.
- **Output:** `reconciliation_run`; an alert on breaks. Runs post-market after the execution cycle, and from /trading.
- In the scratch run it caught OMS orders whose paper book had been reset.

### Broker health (BR-06)

- **Checks:** Dhan market data (with latency), the Dhan token (read-only fund-limits call; DH-901 means expired), index and stock feeds, quote freshness (STALE after 20 min in the session), the Zerodha token and the paper book.
- **Schedule:** every 5 min in the session, plus 08:45.
- **Alerts:** on a transition into or out of DOWN / STALE.
- /trading panel.

### Sandbox (BR-08)

- **Credentials fixed:** SANDBOX now uses its own credentials (`dhan_sandbox_client_id` / `dhan_sandbox_access_token`). The old path reused the production token, which the sandbox rejects (DH-906).
- **Redirect verified:** the client is checked to be pointed at sandbox.dhan.co, and it raises rather than fall through to LIVE.
- **Checker:** `python -m orders.sandbox_check [--place-test-order]` checks credentials, redirect, reachability and auth, plus an optional place → status → cancel round trip on the sandbox. A read-only version is at `/api/execution/sandbox-check`.

### Zerodha session (BR-02)

- **Token states:** VALID / EXPIRED (06:00 IST daily) / MISSING.
- **Login:** `/api/zerodha/login-url` gives the URL. You log in at Zerodha; ATIP never sees the password or TOTP. `/zerodha/callback` then exchanges the request_token.
- **Setup:** **register `http://127.0.0.1:8000/zerodha/callback` as the redirect URL in the Kite developer console.**
- **Expiry handling:** the pre-market job alerts on an expired session, and the holdings sync skips an expired session instead of failing.

### Live P&L (MON-04)

- **Coverage:** LIVE holdings, the PAPER book and per-strategy open positions, valued at the newest live quote.
- **Figures:** day P&L, unrealised and realised P&L, and cash.
- **Prices:** each row shows its price source and quote age; stale prices are listed.
- **History:** 15-min snapshots feed an intraday chart.
- /trading section, refreshed every 60 s.

### Audit export (SEC-03)

- **Segments:** daily (19:30) incremental gzip JSONL segments of `enterprise_audit` (chain verified first; a broken chain is exported anyway and alerted) and `oms_order_event`, each with a sha256 sidecar.
- **Off-box copy:** to `audit.offbox_dir`, with the hash re-checked. Segments are registered in `audit_export`.
- **Retention:** policy only. The tables stay append-only, and pruning is an operator decision after the off-box copy exists.

## Defects fixed along the way

- **Paper LIMIT fill worse than its limit:** a LIMIT order could fill worse than its limit. Adverse slippage was added after the limit check, so a BUY limited at 3000 filled at 3000.50. Fills are now capped at the limit, in placement and in matching.
- **Stale resting paper orders:** resting paper orders never filled (W4-R4), as above.

## Owner actions

1. `audit.offbox_dir` → a second drive, NAS or synced folder. Without it, exports stay on this machine (the status says so).
2. For the sandbox: a Dhan sandbox token → config `dhan_sandbox_client_id` / `dhan_sandbox_access_token`, then `python -m orders.sandbox_check --place-test-order`.
3. For Zerodha: `kite_api_key` / `kite_api_secret` in config, and the redirect URL registered in the Kite console.
4. Optional: `execution.protective_stops: true` (paper).

## Files

- **New:** `execution/{paper_matching,reconcile,broker_health,analytics}.py`, `orders/sandbox_check.py`, `portfolio/live_pnl.py`, `enterprise/audit_export.py`, `dashboard/w29_routes.py`
- **Changed:** `orders/paper.py`, `orders/environment.py`, `execution/{order_manager,adapters,models,config,pipeline,risk_engine}.py`, `portfolio/zerodha.py`, `db/schema.py` (W29_TABLES: reconciliation_run, broker_health_check, live_pnl_snapshot, audit_export; oms_order trigger_price / parent_order_id / modified_count), `db/purge.py`, `pipeline/scheduler.py`, `dashboard/{execution_routes,execution_page,server}.py`
