# Broker-side protective orders and the LIVE checklist (KGF-16, KGF-20, KGF-21) — 16 Nov 2026 (IST)

**Status: design and simulation only.** Nothing in this change places, modifies or cancels an order, a Forever Order,
a Super Order or a GTT at any broker. Every broker-side construct below is written as a local `DUMMY-` record in
`algo_dummy_gtt` (`orders/algo_gtt.py`); `broker_env` stays `PAPER`; `confirm_live` exists for API compatibility only.
`tests/test_kite_formula_fix_2026_11_16.py::test_no_order_changing_broker_method_is_ever_called` proves it.

## 1. Why broker-side (KGF-16)

Today every stop lives in ATIP: `orders/rules.py check_triggers` tests it every 30 s from the dashboard process
(`dashboard/server.py _order_monitor_loop`). If the Mac sleeps, loses the network or the process dies, the stop and
the trailing level freeze (the trail is documented as "NOT a broker-side stop" in `update_trailing_stops`). A stop held
by the broker survives all of that. The Kite breakout-retest entry also rests for days (10 trading days), which only a
broker-side trigger can do without the Mac watching.

## 2. The mapping

| ATIP leg (orders/rules.py)                  | DUMMY- record (kind)                 | Dhan (v2)                                                                   | Kite Connect v3                                       |
|---|---|---|---|
| Resting buy at the breakout low (ENTRY, LIMIT, trigger BELOW) | `BUY`, trigger = limit = buy price | **Forever Order SINGLE** (`orderFlag SINGLE`, CNC, LIMIT, `price` = `triggerPrice` = buy) | **GTT `single`**, one LIMIT order at the trigger |
| Stop + target after the fill (STOP + TARGET, one `oco_group_id`) | `OCO`, stop_trigger, target_trigger | **Forever Order OCO** (`orderFlag OCO`: `triggerPrice`/`price` for the target leg, `triggerPrice1`/`price1` for the stop leg, `quantity1`) | **GTT `two-leg`** (`[stop, target]` triggers, two LIMIT orders) |
| Trailing stop (trail_value from trail_after_pct) | `OCO.stop_trigger` follows the rule's stop (sync) | **Super Order** with `trailingJump` (entry + target + stop + trail in one), or modify the OCO's stop leg on each ratchet (`modify_forever`, at most a few times a day) | GTT `modify` of the stop leg (25 modifications per order) |
| Time exit (max_hold_days)                    | none (the OCO is cancelled, then a market/LIMIT sell) | cancel the OCO, then a CNC SELL LIMIT at the bid | delete the GTT, then a SELL LIMIT |

Notes for the LIVE design:
- **Kite GTTs accept LIMIT orders only.** A stop leg therefore needs a protective limit below its trigger (e.g.
  trigger × (1 − 0.5 %), tick-rounded down) so it still fills in a fast fall.
- **Dhan Forever / Super Order APIs require the static IP to be whitelisted** (see §3).
- Every price on a broker-side order must be on the tick: `orders/ticks.py` (BUY limit / trigger and a long's stop round
  down, a long's target rounds up; Decimal arithmetic). The scrip master's `tick_size` comes from `data/instruments.py`.
- The buy's quantity is final only when it executes (session cash); the OCO is created **after** the fill, never before
  (`stoploss_status = planned_after_buy` until then), so no stop can exist without a position.
- Duplicate prevention must run against the broker's own state before each placement (open CNC BUY today, net position,
  holding, active Forever / GTT for the symbol) — `orders/algo_gtt.py broker_state()` already reads all of it, read-only.
- Reconciliation: on every monitor pass, compare the broker's Forever / GTT list with `algo_dummy_gtt` (status, trigger,
  quantity); a mismatch alerts on Telegram and halts new entries (same pattern as the PA-07 trade-book settlement).

## 3. SEBI retail-algo rules (mandatory since 1 Apr 2026) — checklist for LIVE only (KGF-20)

| Rule | ATIP today | Needed before LIVE |
|---|---|---|
| Static IP whitelisted with the broker | The Mac's home IP (dynamic) — **does not qualify** | A VPS with a static IP for the order path (the viewer server is view-only by design; the order engine must move or proxy through it under ops/role.py rules) |
| ≤ 10 orders per second per exchange | `max_orders_per_day` 50 (the owner's `risk_limits`, 16 Nov 2026 05:36 IST; was 10, then 20 under VF-04); one order per pass | Keep a per-second limiter in `orders/broker.py` (Dhan pacing exists for data calls: `data/dhan.py PACE_SECONDS`) |
| Orders tagged with the broker's generic algo ID | `config.json sebi_algo_id` is checked by `orders/sessions._auto_guards` for LIVE AUTO | Pass the algo ID on every order (`tag` / `algoId` field) |
| Daily 2FA login | Dhan token renewal by TOTP (`ops/dhan_token.py`, retried 06:30/08:00/09:00) | Keep; the 08:45 pre-open health gate (`ops/preopen_health.py`) holds auto-pick when the token is bad |
| Market orders become market-price-protection orders | ATIP's entries are now LIMIT (KGF-02); exits are MARKET | Convert exits to LIMIT with protection (stop: trigger − 0.5 %); prefer LIMIT everywhere |
| Dhan Forever and Super Order APIs need static-IP whitelisting | not used | VPS first |

## 4. Kite Connect v3 limits (KGF-21), if the Zerodha connector is used

`brokers/connectors.py` stays read-only (Zerodha is OFF at the owner's request until `"zerodha_enabled": true`).
- Rate limits: quotes 1 request/s, historical 3/s, order placement 10/s; 400 orders a minute, 5,000 a day, 25
  modifications per order.
- WebSocket `wss://ws.kite.trade`, modes `ltp`, `quote`, `full` (5-level depth), up to 3,000 instruments per
  connection, 3 connections per API key.
- The instruments CSV carries `tick_size` and `lot_size`: fetch daily, key on exchange + tradingsymbol
  (`data/instruments.py`).
- `/quote` takes 500 instruments; `/quote/ohlc` and `/quote/ltp` take 1,000.
- The GTT list returns active triggers plus the last 7 days.
- Docs: https://kite.trade/docs/connect/v3/ (gtt/, orders/, websocket/, market-quotes/, exceptions/).

## 5. What runs today (PAPER)

- Kite-retest arms (S2, S3, S4, S10) write a `DUMMY-` `BUY` record and a mirrored LIMIT entry rule; the rule fills in
  PAPER when the LTP is at or under the buy price, at min(LTP, buy) (`orders/rules._paper_reference`); `algo_gtt.sync`
  then marks the record `TRIGGERED` and writes the `DUMMY-` `OCO` with the rule's stop and target; a trailing move is
  mirrored onto the OCO's stop; the OCO closes when the legs are done.
- Validity: 10 trading days (`VALID_SESSIONS`); cancelled when a newer breakout of the same stock supersedes the setup
  or a close falls under the stop before a fill.
- Endpoints: `POST /api/algo-orders/gtt-state` (read-only), `POST /api/algo-orders/live-gtt` (dummy only; requires the
  JSON boolean `confirm_live: true`, repeats every state check server-side, never falls back to real placement),
  `GET /api/algo-orders`.
