# CHANGELOG: session engine (18 Nov 2026)

All changes are additive: no module was rewritten. Branch `claude/session-engine-2026-11-18`, from master `be077b2`.
Times are IST.

## New files (4)

| File | What it is |
|---|---|
| `orders/session_engine.py` | The engine, covering: the ₹25K split (`plan` / `create`); wallet and counts (`stats`); the `session_trades` and `session_ledger` tables (`sync`); auto-stop and the floor pause (`check`); `activate`; the owner-only `rebase`; the card and banner data (`overview` / `alerts`); and the CSV export. It has a CLI: `python -m orders.session_engine plan|create|rebase|status|sync|check`. |
| `dashboard/engine_routes.py` | `/api/v2/engine` (overview, plan, create, activate, sync, one session) and the trade and ledger CSV exports. |
| `tests/test_session_engine_2026_11_18.py` | 18 tests: split and reserve, buffer, CREATED holds entries, ledger maths and charges, compounding, `order_log.session_id` and its backfill, isolation, no order-count cap, COMPLETED and freeze, floor pause and alert, shell banner data, page and drill-down, API and CSV, the 10 × 500 simulation, a whole-cash LIMIT buy above its quote, and a resumed session losing its floor reason. |
| `docs/reports/2026-11-18-session-engine/*` | This changelog, `SESSION_ENGINE.md` (design and decisions) and the evidence. The test report is `docs/testing/ATIP_Session_Engine_Test_Report_2026-11-18.md`. |

## Changed files (13)

| File | Change |
|---|---|
| `orders/sessions.py` | `STATUSES` adds CREATED and COMPLETED. `gate()` no longer applies the daily `max_trades` count to a cash-pool session; it still applies to sessions without a pool. |
| `orders/broker.py` | `order_log.session_id`: an additive column, written on every row from the order's `ATIP-S<n>` tag (`MANUAL` outside a session). Older rows are backfilled from the paper order's tag (`UNTAGGED` where nothing matches). New `check_session_funds()`: a pool's BUY is checked against that session's own free cash. In PAPER, a LIMIT buy is valued at the lower of its limit and the quote that fired it, which is what the paper broker fills at; LIVE is valued at the limit. `_place_order` is now a thin wrapper that carries the session id; the placement itself is `_place_order_inner`, unchanged. |
| `dashboard/server.py` | The order monitor pass runs `session_engine.check()` before auto-pick, so a session it pauses or completes is not picked. The engine routes are registered. Not in `pipeline/scheduler.py`. |
| `dashboard/shell.py` | `/api/v2/shell/state` gains `session_alerts` and `session_alert` (the apps' banner text). |
| `dashboard/ux_pages.py` | Sessions page: the engine's summary row (sessions, combined balance, combined P&L, reserve) and a "Split reserve into ₹25K sessions…" dialog with a confirmation tick. Each card gains state, balance against seed, % growth, a meter of trades out of 500, win rate, legs and the pause floor. Session detail gains a "Trades & ledger" tab with the wallet tiles, trade list, ledger history and full-CSV links. |
| `dashboard/ux_kit.py` | The status chip knows CREATED (◌) and COMPLETED (✓). |
| `tests/conftest.py` | The engine's config path is isolated per test, as the capital plan's already is. |
| `tests/test_ui_fixes_v2.py` | The order-path guard lists `orders/session_engine.py` and explains the `broker.py` and `sessions.py` changes. |
| `mobile/android/app/src/main/java/com/atip/mobile/StatusText.kt` | The native banner shows the session alert: ⚠ amber for paused below the floor, ✓ blue for completed. |
| `mobile/android/app/build.gradle.kts` | `versionCode` 12, `versionName` 2026.11.18.1. |
| `mobile/ios/ATIP/StatusBanner.swift` | The same session alert on iOS. |
| `mobile/ios/project.yml`, `mobile/ios/ATIP/WebScreen.swift` | `CFBundleShortVersionString` 2026.11.18.1, `CFBundleVersion` 12, and the user-agent fallback. |

`mobile/README.md` and the tracker (`ATIP_MASTER_TRACKER.xlsx`, `docs/ATIP_MASTER_TRACKER.csv`) are updated with the
feature status.

## Database changes (additive, created on first use)

* New tables:
  * `session_trades`, keyed by `trade_id`, the entry rule's id;
  * `session_ledger`, keyed by `entry_id`, text ids (so no id counter: nothing to reset after a copy).
* New columns on `trading_sessions`: `current_balance`, `cash_available`, `invested_value`, `realised_pnl`, `unrealised_pnl`, `completed_trades`, `legs`, `wins`, `losses`, `balance_floor_pct`, `pause_reason`, `created_at_ist`, `completed_at_ist`, `final_ledger`, `engine_synced_at`.
* New column `order_log.session_id`.
* Nothing is dropped or rewritten; old rows keep their values.

## Not changed

* `pipeline/scheduler.py`.
* The risk limits.
* The exits, sizing, auto-pick and arms.
* The kill switch.
* The LIVE gates.
* `atip_data/config.json`: the engine runs on its defaults. An optional `"session_engine": {...}` block can override `seed_capital`, `floor_pct`, `max_completed_trades` and `respect_buffer`.
