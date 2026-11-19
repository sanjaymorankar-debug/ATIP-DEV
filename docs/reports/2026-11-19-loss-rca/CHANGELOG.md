# CHANGELOG — Loss RCA delta, 19 Nov 2026 (IST)

Base: GitHub master **0c0c452** (= c03fc29, which the VPS runs since 20:08:52 IST 19 Nov, + deploy tooling only).
Branch `claude/loss-rca-2026-11-19`, commit **e5903e1** (GitHub master since 00:01:25 IST 20 Nov). Delta patches only;
no file rewritten. **Live on the VPS since 00:46:11 IST 20 Nov** (pid 836452), with `strategy_v2` OFF.
"Flag" = active only when atip_data/config.json has `"strategy_v2": {"enabled": true}` (absent on the VPS = OFF).

## Fixed (bugs)
- **LRCA-01** `orders/rules.py` — `check_triggers` runs each rule in `_check_one` inside its own try/except (a failing
  rule records a `RULE_ERROR` event, rolls back and the next rule still runs); exit legs are tested before entries; a
  "database is locked" error is re-raised after the other rules ran (keeps the RCA-13 5-second retry).
- **LRCA-02** `dashboard/server.py` — each monitor pass checks exits (`check_triggers`) before auto-pick.
- **LRCA-03** `dashboard/server.py` — `order_monitor` heartbeat on every clean pass; one error alert after 4 failing
  passes in a row (~2 min), one info alert on recovery (keys `order_monitor_failing` / `order_monitor_recovered`).
- **LRCA-04** `db/postgres.py` — `SET lock_timeout = 60000ms` on every PostgreSQL connection (`LOCK_TIMEOUT_S`);
  SQLSTATE 55P03 is raised as sqlite3 `OperationalError("database is locked (…)")`.
- **LRCA-05** `orders/rules.py` — only finite, positive LTPs are prices (`_usable_price`); a NaN LTP no longer hides a stop.
- **LRCA-10** `db/postgres.py` — `PgConnection.in_transaction` (sqlite3 parity).
- **LRCA-11** `pipeline/health.py` — a run SKIPPED with "locked…" is not a working run.
- **LRCA-13** `data/dhan_ws.py`, `data/bhavcopy.py` — an unknown index change is stored as NULL (not 0 %); only feed
  rows with a Nifty change keep NSE's official close snapshot out.
- **LRCA-15** `dashboard/server.py` — `POST /api/refresh` refused 09:00–16:00 IST unless `?force=true` (HTTP 409).

## Added behind the flag (strategy v2)
- `orders/strategy_v2.py` (new): settings/enabled/on/value, `sessions_back`, `stale`, `nifty_gap_block`, CLI
  `python -m orders.strategy_v2`.
- **LRCA-06** `scores/breadth.py` `gate_state` — breadth older than `breadth_max_age_sessions` (1) closes the gate.
- **LRCA-07** `orders/autopick.py` `candidates` — scores older than `max_signal_age_sessions` (1) are not traded.
- **LRCA-08** `orders/algo_gtt.py` `place_dummy_buy` — no resting Kite buy while LTP ≤ its stop;
  `orders/sessions.py` `gate` — a resting "Algo retest" entry is held while its arm's regime gate is closed.
- **LRCA-09** `orders/autopick.py` — `nifty_gap_min_pct` filter (default null = off even with v2 on).
- **LRCA-14** `scores/engine.py` `determine_signal` — no BUY while market health is unknown.

## Tests
- `tests/test_loss_rca_2026_11_19.py` (new, 31 tests; 3 need `ATIP_TEST_POSTGRES_URL`).
- `tests/conftest.py` — `orders.strategy_v2.CONFIG_PATH` isolated in every test (never the live config).
- `tests/test_ui_fixes_v2.py` — `orders/strategy_v2.py` added to the order-path allow-list, with the reason.

## Docs
- `docs/reports/2026-11-19-loss-rca/LOSS_RCA_REPORT.md`, `CODE_REVIEW.md`, `CHANGELOG.md`, `evidence/`.
- `docs/testing/ATIP_Loss_RCA_Test_Report_2026-11-19.md`.
- `ATIP_MASTER_TRACKER.xlsx` (new first sheet "Loss RCA 2026-11-19") and `docs/ATIP_MASTER_TRACKER.csv` (phase 28,
  30 rows LRCA-*).

## Rollback
Revert the branch (or `git checkout 0c0c452 -- <file>` per file). The flag needs no rollback: remove the
`strategy_v2` block (or set `"enabled": false`) and ATIP behaves exactly as before.

## Review fixes, 20 Nov 2026 (IST) — on the branch after 514b439, not deployed
- **LRCA-16** `dashboard/server.py` — each monitor pass runs `autopick.cancel_stale` and `algo_gtt.maintain` before
  `check_triggers` (a leftover auto-pick could fill on a gap-down open before it was cancelled).
- **LRCA-17** `orders/entry_guards.py` — the Nifty-weak guard's message is NULL-safe.
- **LRCA-18** `dashboard/server.py` — one monitor alert per outage (key with its start), state reset at a new day.
- **LRCA-19** `orders/rules.py` — the exit-blind alert uses `_usable_price`.
- **LRCA-20** `data/markets.py`, `pipeline/scheduler.py` — unknown REST index change stored as NULL; `--status` prints n/a.
- Tests: `tests/test_review_fixes_2026_11_20.py` (7, all fail on e5903e1); stale fixtures fixed in
  `tests/test_kite_retest_study.py` and `tests/test_dhan_realtime_feeds.py`.
