# W34 — Execution microstructure: handoff

**Branch:** `w34-execution-microstructure` (worktree `D:\Projects\ATIP-dev-w34`), on top of `w28b-news-weighting` → `w33-wealth-qa`.

**Scope:** five Yet-To-Start features that W29 left out: EX-11, EX-12, EX-15, EX-16 and BT-17.

**Status:** developed, `compileall` clean. Not run, not tested, not merged, not deployed.

**Safety:**
- Everything here is **PAPER**. No LIVE path was added, and the Dhan adapter still refuses every call.
- Algos are **off** (`execution.algo.enabled=false`).

## EX-11 — Execution algos (`execution/algos.py`)

**When an algo is used.** An APPROVED risk decision normally becomes one order. With `execution.algo.enabled`, `pipeline.execute_approved` asks `algos.select()` first. The order becomes an **algo parent** when either:
- its value is at least `min_order_value` (₹5 lakh), or
- its EX-12 participation is at least `min_participation` (2 % of ADV).

**The four algos:**

| Algo | How it works |
|---|---|
| **TWAP** | Linear schedule in `interval_min` slots |
| **VWAP** | Cumulative slot-volume share from the last 20 sessions of 15-min bars. Needs at least 5 sessions; otherwise it falls back to TWAP. |
| **POV** | `rate` × market volume since the start (today's 15-min bars) |
| **ICEBERG** | One LIMIT child of `display_qty` at a time |

**Children.** Each slice is an ordinary `oms_order`:
- linked by `algo_parent_id` / `algo_slice`;
- intent and risk-decision ids are synthetic (`<id>:<n>`), because those columns are UNIQUE;
- children only ever sum to the risk-approved quantity.

**Lifecycle:**
- States: WAITING → WORKING → COMPLETED / EXPIRED / PAUSED / CANCELLED.
- A REJECTED or FAILED child **pauses** the parent; nothing is retried blindly. `resume` only ignores failed slices that came before it.
- With `complete_at_end`, whatever is left at `end_at` goes as one final MARKET child.

**Scheduling.**
- A parent created by the post-market cycle starts at the next session's 09:15.
- `tick()` runs every minute in session (`execution_algos`, logged only when there is work).
- Progress is recomputed from the children on every tick, so a missed event cannot leave a parent wrong.

**Report.** `report()` compares the average price with the arrival (reference) price and with the interval VWAP of the stored bars, against the pre-trade estimate.

**Risk change.** `max_daily_trades` now counts an algo parent **once**, not once per slice. This applies to both `risk_engine._orders_today` and `tenant_books.orders_today`.

## EX-12 — Market impact model (`execution/impact.py`)

**Formula:** `total_bps = half spread + Y × σ_daily × √(Q / ADV)`.

**Inputs:**
- σ: 60-session daily log returns.
- ADV: median of 20 sessions of daily volume.
- Spread: Corwin–Schultz high-low estimator over 20 sessions, floored at 2 bps.

**The Y coefficient:**
- The default is **0.7**.
- `calibrate()` (Saturdays) fits Y from **LIVE fills only**. Paper fills are priced by a fixed bps model, so fitting them would only recover that constant.
- A fitted Y is adopted only once there are at least 30 LIVE fills, which means not before live trading exists.
- Above 25 % participation, the estimate is flagged `out_of_model`.

**Used by:** algo selection (EX-11), the event-driven backtester (BT-17), and `GET /api/execution/impact`. It never blocks anything.

## EX-15 — Latency telemetry (`ops/latency.py`)

- `timed(stage)` and `record(stage, ms)` keep an in-memory, bounded buffer and feed the W8 `atip_latency_ms` histogram. Nothing writes to SQLite on the hot path.
- `flush()` runs every 5 minutes (not logged as a job). It writes per-minute n / p50 / p95 / p99 / max / mean to `latency_rollup`, with 90-day retention.

**Stages:**
- `data.live_quotes`
- `feed.tick_to_flush`
- `risk.evaluate`
- `order.decision_to_submit`
- `order.submit_adapter`
- `order.submit_to_fill`
- `algo.tick`
- `events.dispatch`

## EX-16 — Event-driven OMS (`execution/events.py`, `execution/event_handlers.py`)

**Publishing.** A transactional outbox: `order_manager.transition()` and `_record_fill()` publish `order.state` and `order.fill` **before their own commit**. The event and the change it describes are therefore one transaction. Algo parents publish `algo.parent`.

**Delivery:**
- The dispatcher (minute tick, `POST /api/execution/events/dispatch`) is at-least-once.
- `oms_event_delivery` makes each (event, handler) pair idempotent.
- A handler that fails is retried up to 5 times, then the event becomes a dead letter (shown on `/execution-lab`).
- `replay()` backfills a new handler.

**Handlers:**

| Handler | What it does |
|---|---|
| `algo_progress` | Works the parent immediately on a child fill or terminal state |
| `fill_latency` | Records `order.submit_to_fill` |
| `audit_line` | Writes a log line |

**Retention:** the outbox is kept (it is part of the audit trail). Delivery rows follow 90-day retention.

## BT-17 — Event-driven backtester (`backtest/event_driven.py`)

**Input:** a normal backtest request plus `"event_driven": {timeframe: 1d|15m|5m|1m, latency_bars, participation_cap, slices, ttl_bars, impact: sqrt|none, impact_y}`.

**Strategies:** they run unchanged, using the same Strategy interface and point-in-time daily view, and are sized exactly like the engine.

**What it models that the engine does not:**
- latency in bars;
- fills capped at a share of bar volume (partial fills and expiry);
- MARKET / LIMIT / STOP order types;
- impact-priced fills;
- TWAP slices;
- intrabar protective exits, with the pessimistic stop rule on daily bars.

**Storage:** results go to the backtest tables as `kind='event_driven'`, so they appear on `/backtests`.

**Limitation:** intraday runs only cover sessions still in `intraday_bars` (90-day retention).

## Files

**New:**
- `db/schema_w34.py`
- `execution/impact.py`
- `execution/algos.py`
- `execution/events.py`
- `execution/event_handlers.py`
- `ops/latency.py`
- `backtest/event_driven.py`
- `dashboard/w34_routes.py`
- `dashboard/w34_page.py` (`/execution-lab`)

**Changed:**

| File | Change |
|---|---|
| `db/schema.py` | Applies the W34 tables and columns |
| `execution/order_manager.py` | Publishes events; latency hooks |
| `execution/pipeline.py` | Algo selection; `risk.evaluate` timing |
| `execution/risk_engine.py` | Algo parent counted once |
| `execution/tenant_books.py` | Algo parent counted once |
| `execution/config.py` | `algo` setting |
| `data/dhan.py` | `data.live_quotes` timing |
| `data/dhan_ws.py` | `feed.tick_to_flush` timing |
| `pipeline/scheduler.py` | Algo tick, latency flush, weekly calibration |
| `dashboard/server.py` | Registers the routes |
| `dashboard/execution_page.py` | Link to Execution lab |
| `db/purge.py` | Retention for the new tables |
| `config_template.json` | `execution.algo` |

**New tables:**
- `exec_algo_parent`
- `execution_impact_calibration`
- `latency_rollup`
- `oms_event_outbox`
- `oms_event_delivery`

**New columns on `oms_order`:** `algo_parent_id`, `algo_slice`.

## For QA

1. Run `init_db()` on a copy of the database. Expect 5 tables and 2 `oms_order` columns.
2. Create and submit one ordinary paper order. The outbox gets `order.state` events (CREATED → VALIDATED → SUBMITTED → …) and an `order.fill`. Run dispatch: deliveries are OK, the latency summary shows `order.submit_adapter` and `order.submit_to_fill`, and a second dispatch delivers nothing again.
3. Break a handler on purpose (e.g. raise in `audit_line`). Check that the event is retried, becomes a dead letter after 5 attempts, and that the other handlers were not called twice.
4. **Algos**, on a copy with `execution.algo.enabled`, using an APPROVED decision above ₹5 lakh:
   - TWAP over 30 minutes: children appear every 5 minutes and sum to the total, and the parent ends COMPLETED.
   - A rejected child (kill switch on) pauses the parent.
   - Cancel cancels the working children.
   - ICEBERG keeps one working child.
   - VWAP with fewer than 5 sessions of bars behaves as TWAP.
   - `max_daily_trades` counts the parent once.
5. **Impact:** `GET /api/execution/impact?symbol=RELIANCE&quantity=1000`. Recompute σ, ADV and the Corwin–Schultz spread by hand for one symbol. `calibrate` with no live fills reports "default stays".
6. **BT-17:** run the same strategy and window with the engine and with `event_driven` (1d, participation_cap 1.0, impact none, slices 1, latency 1). Results should be close (same entry timing; the remaining differences are explainable). Then turn on impact, participation 0.05 and slices 4, and check that partial fills and slippage appear in the metrics.
7. Open `/execution-lab`. All sections load, and the latency chart draws for a stage that has rollups.

## Follow-ups

- **Protective stops after an algo completes are not placed:** W29's `place_protective_stop` works per order. Add a stop for the parent's total on completion if wanted.
- **POV reads market volume from the stored 15-min bars,** which are refreshed every 30 minutes. With a stock-level live volume feed this could be tighter.
- **Y calibration needs LIVE fills,** and live execution is not built.
