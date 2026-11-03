**Date of prompt:** 3 Nov 2026 (all times IST)

# ATIP delta patch: UX audit Critical fixes C1–C4 (3 Nov 2026)

Source: the Step 1 Current State Audit (3 Nov 2026), owner instructions given at 07:28 IST.
Scope: 4 dashboard files and 3 tests changed, 1 test file added. No database, config, scheduler or broker code is touched.

| ID | Owner instruction | Root cause | Fix |
|---|---|---|---|
| C1 | HOLD → no Buy | `order_btns()` always drew Buy + Sell, whatever the signal (by design, per its old docstring) | `order_btns(..., signal=)` draws Buy only when the signal is BUY; otherwise a "No buy" marker with the reason in its tooltip. The order modal takes the signal and locks its Buy side (`setSide` and `saveOrderRule` refuse BUY too). Stock view: Buy only when the latest stored signal is BUY. Sell stays available everywhere. |
| C2 | Order window must show PAPER / LIVE | The modal never read `orders.environment` | Badge in the modal title (teal PAPER, red LIVE), a status line with `orders.environment.describe()`, and the save button reads "Save rule · PAPER" / "· LIVE". SANDBOX shows as SANDBOX. |
| C3 | Today and ATIP Scores showed different stop/targets for the same Trade of the Day | Two formulas. `/home` reads the stored plan from `scores/predictions.compute_trade_plan()` (no levels for HOLD/WAIT; BUY stop = 1.5×ATR clamped to 2–6 %, targets +3 % / +6 %). `get_tod()` in `dashboard/server.py` computed its own: stop = close − 1.5×ATR, targets = close + 2×ATR / + 3.5×ATR, for any signal. PETRONET (HOLD) 2 Nov: 300.95 − 1.5×7.2656 = ₹290.05 | `get_tod()` now calls `compute_trade_plan()`. A HOLD/WAIT Trade of the Day reads "No trade plan — signal is HOLD" on both pages. |
| C4 | Two emergency buttons, find the difference | No functional difference: "Kill switch" (shell top bar) and "Halt all trading" (Command Center + its sub-pages) both `POST /api/risk/halt`, which writes `atip_data/TRADING_HALTED`; the broker then refuses every order, exits and stop-losses included | One control: the top-bar **Kill switch** (its dialog also shows open trades left unprotected). Command Center's button is hidden until trading is halted, then reads "Halted · Resume…". The shell already sends a halted click to `/command#kill`, which now focuses that Resume button. |

Found during the fix (not changed, for your decision): every Trade of the Day stored so far is HOLD; on 2 Nov the engine produced 0 BUY, 327 HOLD and 172 WAIT. So with C1, no Buy button shows until the engine emits a BUY.

## Files

- `dashboard/server.py`: `get_tod()`, `order_btns()`, Trade of the Day card, order modal (env badge/line, Buy lock), CSS
- `dashboard/stock_view.py`: `svSig()`, Buy only on BUY, signal passed to the modal
- `dashboard/command_page.py`, `dashboard/command_more.py`: page-level halt button → hidden Resume control
- `tests/test_tod_buy_rules_2026_11_03.py` (new, 12 tests)
- `tests/test_ux_console.py`, `tests/test_ux_command_center.py`, `tests/test_ux_command_browser.py`: updated to the one-kill-switch rule (the browser test now halts with the top-bar switch and checks Resume appears)

## Apply

Unzip over the project root (`~/ATIP`); paths are relative to it. Restart the dashboard. Rollback: the `_orig/` folder in the zip holds the 7 changed files as they were before this patch.

## Mobile apps

The Android and iOS apps are WebView shells over `/m`, `/command` and the shell pages, so they pick these changes up from the server with no native build. No version bump is needed.
