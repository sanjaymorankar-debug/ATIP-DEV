# ATIP Consolidated Patch — September 2026

Supersedes the incremental patches v1–v5. Everything below is already included;
apply this one package only.

**Scope:** 12 files modified, 2 files added, +1,933 lines. No file outside
`atip/` is touched, so your `atip_data/` (database, logs, `config.json`) and
`main.py` are left alone.

---

## New files

| File | Purpose |
|---|---|
| `scores/predictions.py` | Writes each day's scores into the `predictions` table as a tradeable plan (entry / stop / target_1 / target_2 / R:R / position size). Nothing wrote this table before, which is why the accuracy tracker could never produce a row. |
| `scores/backtest.py` | Replays `prices_daily` to measure a fixed-percentage-target strategy. Modes: `dip` ("buy at its low"), `vol50`, `signal` (ATIP's own scores), `all` (no-selection benchmark). Models costs and exposes an entry-timing switch. |

---

## 1. The big one — 23 technical indicators were silently dead

`compute_indicators()` had every indicator inside a **single** `try/except`.
`ta.adx()` threw for every symbol, so everything computed after it was skipped.

Measured on your database (2,134 rows):

```
rsi_14      2134/2134   macd_*  2134/2134
adx_14         0/2134   <== execution stopped here
ema_*, sma_200, atr_*, bb_*, obv, volume_ratio,
pivot, r1/r2/s1/s2, fib_*, above_200dma,
golden/death_cross, gap_pct  ... ALL 0/2134
```

**This is why the Decision Engine never emitted a single BUY** (1,598 HOLD +
529 WAIT, zero BUY/SELL ever). ZPI sat at ~43 against a gate of 55 purely
because Support / Trend / ATR / Volume / Resistance were reading NULLs — the
formula itself is fine: run with real inputs, a typical stock scores 58.9 and
best case 88.7.

Fixed:
- each indicator now runs in isolation (`_step()`), so one broken library call
  costs one indicator, not the other 23;
- `ON CONFLICT` now refreshes **all 37** columns, not 4 — previously a row
  written with NULLs could never be repaired by a re-run;
- failures are summarised per run (`⚠ Indicators that failed: adx×502`) instead
  of vanishing silently;
- `rel_volume` (schema column nothing populated) is now computed.

**After deploying, re-run technicals to repair the existing NULL rows:**
`python -m data.technical --date <last trading day>`

---

## 2. Hardcoded score components replaced with real data

| Component | Was | Now |
|---|---|---|
| VPI `RS` | weight seeded, never computed | 20-day return vs the Nifty 50 benchmark series |
| ZPI / TOD `Sector` | constant `5` / `60` for every stock | real daily industry-relative rank from the Nifty 500 `Industry` column |
| TOD `Breakout` | constant `60` | 20-day-high breach + volume confirmation |
| ACS `NewsConfidence` | constant `50.0` | mean article confidence, 7-day, recency-weighted |
| ACS `Liquidity` | constant `60.0` | 20-day average turnover (shared with VPI's `LQ`) |
| ATIP `INS` | always `None` — silently dropped | new `compute_ins()` from FII/DII + promoter holding + bulk/block deals |

`MutualFund` and `Insider` from the doc's INS formula are **deliberately
omitted, not faked** — NSE publishes no free per-stock source for them. Their
weight is redistributed across the three real inputs.

New: NSE bulk & block deal fetcher (`download_bulk_block_deals`) + `bulk_deals`
table, wired into the post-market pipeline.

---

## 3. Orders — working stoploss, brackets, trailing

**The stoploss was non-functional in both directions.** Direction was inferred
from side, so:
- on a BUY rule the stop sat below the buy trigger and `price <= trigger`
  matched first — the stop branch was unreachable;
- on a SELL rule the stop was below the trigger but the branch tested
  `price >= …`, so a *falling* price triggered nothing. A crash exited nothing.

Fixed with an explicit `trigger_direction` (`ABOVE`/`BELOW`), which makes all
four cases expressible — including "sell if it falls", i.e. an actual stop.

Added:
- **Bracket orders** — an entry that fills auto-creates its exit legs, measured
  from the **actual fill price**, not the trigger;
- **Staged exit** — e.g. 50% at +3%, remainder at +6%, configurable split;
- **Trailing stop** — keeps a fixed distance below the high-water mark, only
  ever moves in your favour, optional ₹ step (like Dhan's trailing jump);
- **Correct partial-fill reconciliation** — a filled target *shrinks* the stop's
  quantity and leaves the other target alone. (An earlier version cancelled the
  whole group, which would have left the remaining shares unprotected.)
- **Telegram on trigger** — pending / executed / failed / expired, plus a loud
  `POSITION UNPROTECTED` if an entry fills but its legs can't be created.
- Confirmation window **120s → 30 min**, with a re-check on confirm that refuses
  if price moved >1% against you since the trigger (overridable).
- Auto-execution refuses to act on a **quote older than 180s** — aimed at the
  2+ hour silent-process failure your `orders.py` documents.

### ⚠ Trailing is computed by ATIP, not by Dhan

It only trails while the dashboard process is alive. For an overnight position
the stop should live at the broker. `_place_order()` currently passes no bracket
params, and `requirements.txt` pins only `dhanhq>=1.4.0` (v1 and v2 differ), so
broker-side placement is **scaffolded but not implemented**. Run:

```
python -m orders.rules --probe-broker
```

It reports what your installed SDK actually exposes. Note Dhan's Bracket/Cover
products are **intraday-only** — a multi-day CNC hold needs a GTT/Forever order.

---

## 4. Measurement — you can now know your hit rate

- `predictions` is written every scoring run (also fills `risk_reward` and
  `position_size_pct`, both previously always NULL).
- `accuracy.py` rewritten. Five real bugs fixed, the worst: `hit_target_1` only
  compared the **close on one single day**, so a stock that touched +3% and fell
  back was recorded as "never hit target" — exactly backwards for a
  take-profit-at-3% strategy. It now scans the whole holding window on intraday
  high/low. Also self-backfilling (a missed run no longer leaves a permanent
  hole), holiday-aware, and no longer interpolates a symbol into SQL.
- Accuracy now runs **daily** post-market, not Saturday-only.
- `python -m scores.predictions --backfill` recovers plans from existing scores.

---

## 5. Dashboard

- **Buy/Sell buttons on every screen** (ATIP Scores, Portfolio, Top VPI, Buy
  Zones, CRI Risk, and the Trade-of-the-Day card). Previously only the main
  scores table had one, and the Portfolio tab had a phantom `Action` column
  header with no button — the Signal value was rendering under it.
- Bracket panel: Target 1 + % of qty, Target 2, stoploss, trailing, auto-exit.
- Missing-CMP handling: falls back to the live-quote cell, then locks the form
  to an absolute price rather than storing a rule against a ₹0 reference.
- Real validation and success feedback on save.

---

## 6. Other fixes

- **News symbol attribution**: was a hardcoded 19-symbol list (11% of articles
  linked, 15 symbols ever matched). Now derives aliases for the whole universe
  from the Dhan security master, with the curated list as overrides and generic
  words (BANK, POWER, STEEL…) filtered out.
- **Telegram placeholder detection**: `YOUR_TELEGRAM_BOT_TOKEN` is non-empty, so
  the old check passed it to the API and got a 404 — every alert this system ever
  attempted failed looking like a network fault. Now reported as *not configured*.
- `bulk_deals` added to the 600-day purge tier.
- Decision Engine thresholds extracted into documented constants, plus a warning
  when a scoring run produces zero actionable signals.

---

## Deploy

```bash
# 1. extract over your existing install (touches only atip/)
python -m zipfile -e ATIP_consolidated_patch.zip D:\Projects\

# 2. additive schema migration — safe on your existing database
python main.py --init

# 3. repair the NULL indicator rows the old ON CONFLICT could not fix
python -m data.technical --date 2026-07-30

# 4. recover measurable history from scores you already have
python -m scores.predictions --backfill

# 5. restart the dashboard (it hosts the order monitor)
```

Then reproduce the strategy measurements:
```bash
python -m scores.backtest --mode dip --sweep
python -m orders.rules --probe-broker
```

---

## Still needs YOUR action — these are config, not code

| Setting | Where | Without it |
|---|---|---|
| `telegram_token`, `telegram_chat_id` | `atip_data/config.json` | no alert ever reaches you |
| `alpha_vantage_key` | `atip_data/config.json` | `fundamental_data` stays empty → SPI/FS NULL → 25% of ATIP weight dead |
| `ANTHROPIC_API_KEY` | **environment variable** | news stays on the keyword fallback, never Claude |
| `kite_api_key/secret` | `atip_data/config.json` | Zerodha portfolio fallback keeps failing (Dhan is primary, so optional) |

---

## Known limitations (not fixed)

- **9 of 18 `config.json` keys are read by nothing** — `schedule_*_time`,
  `intraday_refresh_mins`, `dashboard_port`, `max_stocks_to_score`,
  `min_data_days`, `primary/eod/global_data_source`. Editing them does nothing;
  the values are hardcoded in `scheduler.py` / `server.py`.
- **`institutional_data` is never written by any code path** — VPI `IS`,
  RRI/ZPI `Institutional` remain absent.
- **`pcr` and `adv_decline` are never written** — so MSI `Options` is a constant
  61.54 and MH `Breadth`+`AdvanceDecline` a constant 25.93 (0.10 and 0.15 of
  those indexes).
- **`adj_close` is never populated** — every indicator runs on unadjusted close,
  a correctness risk for recent splits/bonuses.
- **NSE holiday list** has only 4 fixed-date holidays; festival dates need
  adding manually each year.
- Dead code still present: `atip/orders/*.py`, root `./orders/`,
  `static/orders_panel.*` — superseded by `orders/rules.py`, safe to delete.
- `bhavcopy` was failing 11× vs 2 successes and `portfolio_sync` 9× in your job
  log; those root causes are not addressed here.

## Verification notes — what I did and didn't test

Tested end-to-end against your database: bracket creation, OCO/partial-fill
reconciliation, trailing ratchet, trigger direction (all four cases), the
accuracy tracker (synthetic matured prediction, all three horizons), prediction
backfill (1,563 rows), dashboard rendering, and the backtester (~18,700 trades
per configuration).

**Not tested:** the technical-indicator fix could not be run here (no pandas in
my environment) — it compiles and the restructure is mechanical, but its first
real run is the proof. No real Dhan order was placed; the order path was tested
with a stubbed broker. Dry-run a single share before sizing up.

---

# 2 Nov 2026: UI/UX redesign, session accounting and periodic reports

Delta on `8092f74` plus the uncommitted Dhan-feeds work of 31 Oct (branch `claude/dhan-pacing-2026-10-31`). Additive only: every old URL, API route and response key still works, and the trading guards are unchanged. Those guards are the kill switch, PAPER-only auto-send, AUTO's `ack`, the SEBI Algo ID guard, exit legs never held by `gate()`, and EOD signals. Writes still need the dashboard token, and get a 423 while ATIP is stopped and a 403 on the viewer.

## What you get

- **One shell on every page.** It has a left rail of 10 sections and, on phones, a bottom tab bar. The top bar carries Ctrl/⌘K search (pages, commands, symbols), the PAPER / SANDBOX / LIVE badge, the run state in `/api/status`'s own words, a kill switch on every page (it shows the ₹ in open trades), alerts, the theme and the profile. Light and dark themes are served from `/static/atip/`.
- **New pages:** `/home` (safe, running, making money?), `/sessions`, `/sessions/S1…`, `/accounting`, `/reports`, `/settings`, `/more`, `/markets`, `/trade`, `/lab` and `/ops` (the link on `/compliance` was a 404).
- **Each session's own money, from the UI.** Each session gets its own starting amount, a minimum per cycle that grows (FIXED, RATCHET_PCT or STEP), a compounding basis (CASH_AFTER_CHARGES or AFTER_TAX_RESERVE), exits, max cycles, auto-pick, mode and status. Validation is in plain English, and every change is logged in `session_events`.
- **Every cycle and every total shows gross P&L, each charge, estimated tax and actual P&L.** These are never blended into one number. The §4.6 example matches to the paisa (`tests/test_session_cycles.py`).
- **Accounting:**
  - a double-entry ledger, a session ledger, a trial balance, the charges register and the FY tax register (set-offs, LTCG exemption once a year);
  - funds, with VIRTUAL and ACTUAL kept apart;
  - reconciliation of contract-note CSV and session cash;
  - transfers between sessions (refused out of an open trade) and manual adjustments.
- **Reports:**
  - kinds: daily session summary, weekly performance, monthly P&L statement, quarterly review, FY tax statement, session cycle book and custom;
  - formats: HTML, CSV, XLSX and PDF;
  - schedules, editable in IST (daily, weekly, monthly, quarterly, FY or cron), with an archive;
  - delivery through `enterprise/notifications.py`.
- **Trade screens:**
  - Command Center session tiles show gross vs actual.
  - Trade Setup shows the chosen session's free cash and floor.
  - Live Monitor has a cycle-progress bar for each session.
- **Phone apps:** tabs are Home, Trade, Sessions, Reports and More, with the env badge in the native banner. Versions are 2026.11.02 (Android versionCode 3, iOS build 3).

## Files

**New:**

| Area | Files |
|---|---|
| Accounting | `accounting/__init__.py`, `accounting/tax.py`, `accounting/cycles.py`, `accounting/ledger.py`, `accounting/reconcile.py` |
| Reports | `enterprise/session_reports.py`, `enterprise/pdf_writer.py` |
| Dashboard | `dashboard/shell.py`, `dashboard/ux_kit.py`, `dashboard/ux_pages.py`, `dashboard/ux_routes.py`, `dashboard/static/atip/atip.css`, `dashboard/static/atip/atip.js` |
| Tests | `tests/test_session_cycles.py`, `tests/test_session_reports.py`, `tests/test_ux_shell.py` |
| Docs | `docs/design/*` (design pack, design system, handoff), `docs/testing/ATIP_UIUX_Test_Report_2026-11-02.md` (+ `.xlsx`), `docs/testing/screenshots/` |

**Changed:**

- `orders/sessions.py`:
  - 4 pool columns;
  - `update_session` accepts the floor and basis fields and `max_occurrences`;
  - `ledger()` adds keys;
  - `gate()` and `pool_quantity()` read the floor and the investable cash, which are unchanged with the defaults.
- `enterprise/reports.py`: session kinds as sections; quarterly and FY schedules.
- `pipeline/w39_jobs.py`: the 15-minute `session_reports` job (06:00–20:45 IST), through the scheduler's documented hook. `pipeline/scheduler.py` is not touched.
- `dashboard/server.py`: two lines registering `dashboard/ux_routes.py`.
- `dashboard/global_asia.py`: `/api/status` gains an `env` key.
- `config_template.json`: the `tax` and `reports` blocks.
- `requirements.txt`: notes on the PDF engine.
- `tests/test_w39_ui_smoke.py`: Playwright checks of the new pages.
- `mobile/` (Android and iOS).
- The tracker.

## Migration

There is nothing to run. Every new table and column is created on first use (`CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ADD COLUMN`). The first visit to `/sessions`, or the first 15-minute job, backfills `session_cycles` and the ledger from the existing trades. To recompute stored cycles after changing tax rates, run `POST /api/v2/sessions/sync {"force": true}`.

**Restart ATIP after applying,** so the dashboard and the scheduler load the new code.

## Assumptions (record of the decisions made without stopping)

1. **`/` stays the ATIP Scores page.** The new Home is `/home`, which the logo, the rail and the phone's Home tab open. No old URL redirects.
2. **Older pages are wrapped in the shell; their inline styles are not rewritten.** Rewriting about 60 page modules would break "delta patches, not rewrites". The shared bundle carries the tokens and the shell. The older page bodies keep their own look; the design pack's open items HR-12, 14, 20, 21 and 24 track this.
3. **Tax per cycle taxes net P&L after every charge, including STT**, to match the owner's §4.6 example. Under s.48 STT is not deductible from capital gains, so `"tax": {"stt_deductible": false}` taxes it the other way. A loss carries no tax per cycle; the FY view does the set-off. Surcharge and cess are shown but only added when `addons_enabled` is on, again to match §4.6. Rates are the FY 2026-27 defaults from the brief and must be verified with a CA.
4. **A cycle is written when it closes,** by the sync that the Sessions pages, the reports and the 15-minute job run. The trading path itself is not modified to write it. Charges are itemised with the cost model the fill used. Any paisa difference from the fill's recorded charges is shown as "Other", so the totals always equal what the books paid.
5. **The ledger follows each session's current pool.** A pool restart returns the old pool's cash to Owner capital and allocates the new amount. Charges are posted on the day the cycle closed.
6. **RATCHET_PCT counts the starting amount and every cycle start so far,** and never goes below the base minimum. STEP counts cycles with a positive net P&L.
7. **AFTER_TAX_RESERVE:** the tax set aside is not reinvested (`investable_cash` = `cash` − reserve). Switching the basis restates the whole current pool.
8. **There is no `funds/report.py` in the codebase.** Funds was built from the paper account, `paper_position` and the latest Dhan holdings sync. Broker cash is read live on `/brokers`, not stored.
9. **PDFs:** WeasyPrint when installed, else a headless Chrome print, else the built-in writer (`enterprise/pdf_writer.py`, so a PDF is always produced). WeasyPrint is not installed; it needs Pango from Homebrew, and the live venv was not changed.
10. **Report delivery** goes to the inbox and channels of the default tenant's users through `notify()`. On an install with no enterprise users the report stays in the archive. Nothing is generated or sent on the view-only server.
11. **"Test" and "Prod" servers are local instances,** as in the 26 and 31 Oct runs; no hosted test.bkesari.com or bkesari.com exists to deploy to. Test was a scratch folder with a seeded database and real paper trades through the order engine. Prod was a read-only SQLite backup of the live `atip.db`. The live Mac was not restarted or written to.
12. **The phone apps were not built:** this Mac has no Android SDK, Gradle or Java, and no full Xcode or xcodegen. The Swift sources pass `swiftc -parse` and a Mac Catalyst type-check. The Kotlin was reviewed by hand.

---

# 3 Nov 2026: tax estimates brought into line with the law (tax year 2026-27)

The owner asked for the tax rates and the STT setting to follow Indian law, checked on the web. This replaces assumption 3 of the 2 Nov entry. Everything is still an estimate, labelled "est.", for a CA to check.

## What the law says (checked 3 Nov 2026)

- **The Income-tax Act, 2025 replaced the 1961 Act from 1 April 2026.** Budget 2026 (Finance Act 2026) left the slabs and the capital-gains rates as they were for FY 2025-26. ATIP now shows the new section numbers with the old ones in brackets.
- **Delivery trades held 12 months or less:** 20% (s.196, was s.111A). **Held longer:** 12.5% above ₹1.25 lakh a year (s.198, was s.112A). These are flat rates, whatever the owner's slab.
- **STT is never deducted from a capital gain** (s.72(3), was the proviso to s.48). Brokerage, exchange, SEBI, stamp, GST and DP charges are deducted as costs of the transfer. Against intraday and F&O income, which is business income, STT is an ordinary expense.
- **Intraday (speculative) and F&O (business) income** is taxed at the slabs.
  - New regime, the default (s.202): ₹0–4 lakh nil, 4–8 lakh 5%, 8–12 lakh 10%, 12–16 lakh 15%, 16–20 lakh 20%, 20–24 lakh 25%, above 30%.
  - Old regime: ₹0–2.5 lakh nil, 2.5–5 lakh 5%, 5–10 lakh 20%, above 30%.
- **The 4% health and education cess always applies,** on tax plus surcharge. Surcharge starts above ₹50 lakh of income and is never more than 15% on the 20% and 12.5% gains.
- **The s.156 rebate (was s.87A)** never covers those gains. A resident whose other income is below the nil slab uses the shortfall against them (s.196(2)).
- **STT on futures went from 0.02% to 0.05% of the sale from 1 April 2026** (Finance Act 2026; NSE circular NSE/FATAX/73524 of 31 Mar 2026). Options had already been updated on 26 Oct. Delivery and intraday STT did not change.

Sources: secondary summaries (TaxGuru, CAclubindia, eztax.in, ICICI Direct, Business Today, Upstox), plus the e-filing portal's surcharge and cess pages. The official incometaxindia.gov.in section pages refused automated reads, so the section numbers come from the Act's text as reproduced on eztax.in and TaxGuru.

## What changed

- `accounting/tax.py`:
  - New defaults: `addons_enabled: true` (the cess) and `stt_deductible: false`. STT is added back on STCG and LTCG only; intraday and F&O always deduct it.
  - New settings `regime` ("new" or "old") and `other_income`. With `other_income` given, intraday and F&O are taxed at the slab on the next rupee. The FY statement then uses the exact slab tax that trading adds, and sets the unused nil slab against STCG first, then LTCG. Without it, `slab_pct` (30%, the top slab) is used, so the estimate errs high.
  - The surcharge is capped at 15 on capital gains.
  - Head labels carry the new section numbers.
- `accounting/cycles.py`: `sync()` re-estimates any stored cycle whose tax (or compounding basis) no longer matches the settings. Saving tax settings, editing config.json by hand or a change of defaults all do it, so `{"force": true}` is no longer needed for tax.
- `accounting/ledger.py`: `true_up_tax()` posts the difference between a cycle's new estimate and what the ledger already provisioned (and the reserve under AFTER_TAX_RESERVE). It is dated the day the cycle closed. Posted lines are never edited.
- `dashboard/ux_routes.py`: `POST /api/v2/tax/config` re-estimates and returns `retaxed`. `GET` also returns the slab tables.
- `dashboard/ux_pages.py`:
  - Settings › Tax gains the regime, other income and slab table, and clearer STT and cess options.
  - Accounting › Tax states the regime, the F&O rate and the STT rule.
- `enterprise/session_reports.py`: the FY tax statement lists the regime, the STT rule and any nil-slab use.
- `backtest/costs.py`: the futures STT is 0.05% (`nse_futures`).
- `config_template.json`: the new `tax` block and note.
- Tests:
  - `tests/test_session_cycles.py`: the brief's worked example keeps its own settings and still matches to the paisa. New tests cover the statutory figures, the slabs, the surcharge cap, the FY slab and nil-slab maths, and a re-estimate that keeps the books balanced and the cash reconciled.
  - `tests/test_w40_futures_backtest.py`: the futures rate is hand-checked at 0.05%. The two long hand-computed runs keep the 2024 rate, because they test the engine's arithmetic.

## The §4.6 worked example, both ways

| | Brief's settings | The law (new defaults) |
|---|---|---|
| Net before tax | ₹677.73 | ₹677.73 |
| Taxed amount | ₹677.73 | ₹728.48 (STT ₹50.75 added back) |
| Tax est. | ₹135.55 | ₹151.52 (20% + 4% cess) |
| Actual P&L | ₹542.18 | ₹526.21 |
| Next cycle, AFTER_TAX_RESERVE | ₹25,542.18 | ₹25,526.21 |
| Next cycle, CASH_AFTER_CHARGES | ₹25,677.73 | ₹25,677.73 |

## Effect on the live Mac

- Nothing changes until the next ATIP restart. After it, the first sync re-estimates the stored cycles.
- On 3 Nov there is one stored cycle, a loss with no tax.
- Every live session compounds on CASH_AFTER_CHARGES, so the estimate cannot change any order size. It would only matter for a session switched to AFTER_TAX_RESERVE.
- `atip_data/config.json` has no `tax` block, so the new defaults apply. Any change made on Settings › Tax is written there.

## Assumptions

1. Resident individual, below 60, new regime unless chosen otherwise. Senior-citizen slabs, NRIs and HUFs are not modelled.
2. The s.156 rebate is not counted. It can only lower slab tax on intraday or F&O income when total income is ₹12 lakh or less, so leaving it out errs high.
3. Earlier years' carried-forward losses, marginal relief and advance-tax dates are still not modelled.
4. Charges other than STT are deducted from capital gains in full, DP charges included, as costs of the transfer.

## Phone apps: the Live tab is back (3 Nov 2026)

The owner asked for the Live tab back on both phone apps. It is the 31 Oct page `/m?tab=live`: every Dhan live feed as OFF / IDLE / LIVE / REST / STALE, refreshed every 15 s.

- **Tabs are now Home, Trade, Live, Sessions, More.** Live takes Reports' place, and Reports is one tap away under More. Android's bottom bar takes at most five items (a sixth throws at start-up), and a sixth tab on iPhone folds into the system's own "More". So adding a tab meant giving one up, and Reports is the least time-critical.
- **A phone browser (not the app) is unchanged:** the page shell's own bottom bar is still Home, Trade, Sessions, Reports, More.
- **Changed files:**
  - `mobile/android/.../MainActivity.kt`, `res/values/strings.xml` (`tab_live` in place of `tab_reports`), `res/drawable/ic_tab_live.xml` (Material "sensors"; `ic_tab_reports.xml` removed);
  - `mobile/ios/ATIP/ContentView.swift` (SF Symbol `dot.radiowaves.left.and.right`), `ATIPApp.swift`, `WebScreen.swift`;
  - `mobile/README.md`;
  - the More page's subtitle in `dashboard/ux_pages.py`.
- **Version:** 2026.11.03 (Android `versionCode` 4, iOS `CFBundleVersion` 4).
- **Test:** `tests/test_dhan_realtime_feeds.py::test_both_apps_have_a_live_tab` checks the exact tab order in both apps.
- **Still not built** (no Android SDK or Xcode on this Mac).
  - All Swift files pass `swiftc -parse`.
  - The Mac Catalyst type-check used on 2 Nov now fails with "redefinition of module 'SwiftBridging'" inside the Command Line Tools, and fails identically on the code from before this change, so it is the toolchain.
  - `/m?tab=live` and `/more` were checked in app mode at 375 px on the Test server: no sideways scroll, the shell's own navigation hidden, Reports listed under More, no server errors.

# 3 Nov 2026: recheck of all code and every screen after the merge (master 897f9af)

The owner asked for all the code to be rechecked and every screen verified against the new UI. Branch `claude/recheck-fixes-2026-11-03`.

## How it was checked

- **Code:** three independent reviews of `git diff 234274d..master`, covering:
  - accounting and tax;
  - the trading screens and the shell;
  - every other page.
  Each finding below was confirmed (run or rendered) before it was fixed.
- **Screens:** every HTML route the dashboard serves (45 routes, 58 screens with their tabs) on two local servers: Test (fresh database) and Prod (a read-only snapshot of the live database). Each screen was checked at 1440 px light and dark, 390 px and 320 px, for:
  - the shell's parts (PAPER badge, run state, kill switch, search, alerts, theme, profile, rail or phone tab bar);
  - exactly one main menu and no "← Dashboard" link;
  - theme-following colours;
  - text of at least 12 px;
  - no sideways scroll;
  - JavaScript and server errors;
  - `alert()` / `confirm()`;
  - axe-core WCAG 2.2 AA.
  The 14 ATIP Scores tabs were checked one by one in light and dark.
- **Before the fixes:** every one of the 57 shell pages rendered inside the shell with all its parts (88 of 88 desktop and phone checks), with no broken old URL. The defects are below.

## Fixed

| Area | What was wrong | Fix |
|---|---|---|
| Trade of the Day (C3) | On a day with no BUY, the HOLD card (e.g. PETRONET, with a Sell button) still showed under "No BUY signal for today": the page's `.tod-card{display:grid}` beat `[hidden]` | `.tod-card[hidden]{display:none}` (`dashboard/server.py`) |
| Buy only on BUY (C1) | Trade signals offered "Set up a BUY →" for every HOLD and WAIT row | BUY set-up only on a BUY signal, SELL on SELL, "No buy · signal is HOLD" otherwise (`command_more.py`) |
| Buy only on BUY (C1) | The stock view's Buy read the stock's last stored score, however old (a stock scored BUY weeks ago and since dropped showed Buy) | The data carries `scored_date`; Buy only when the last score is from that date (`stock_view.py`) |
| One feed label (H1) | 8 price columns said a fixed "●live" while the top bar said "Feed stale" | A small mark of the same feed label: "Live · 85 s", "Stale · 15 min", "Closed" (`server.py`, `atip.css`) |
| Kill switch (C4) | "Halted" in the top bar did nothing while already on Command Center (a hash-only link) | It focuses the Resume button on the page (`atip.js`) |
| `/ops` | "AX is not defined": the Operating status panel never loaded | The script waits for the deferred `atip.js` (`ux_pages.py`) |
| Options builder | An analysis that came back after the legs were edited filled the old premium / IV into the changed legs | Every edit drops a result still on its way (`w39_page.py`) |
| Options builder | Two defined-risk spreads in two expiries were labelled "Advanced: Undefined risk" | Each sold leg is matched with a bought leg that outlives it (`quant/options_strategy.suitability`) |
| Loading states | `/trading`'s emergency-exit preview and `/market`'s history stayed "Loading" for ever when their data failed | They show the error (`execution_page.py`, `market_page.py`) |
| Tax settings | `nan`, `inf`, `1e400` passed validation; the FY estimate became NaN and `/api/v2/tax/config` answered 500 | Refused as "must be a number"; a stored non-finite rate falls back to the default (`accounting/tax.py`) |
| Tax on intraday / F&O | With other income below ₹4 L, every intraday or F&O cycle was estimated at 0 % (the slab of other income alone) | The slab the cycle's profit reaches on top of other income ("from 5 %" in Settings) |
| Cycle re-estimate (my 3 Nov change) | Editing the cost model alone rewrote stored cycles' charges and net P&L, so the cycle book and the ledger's charge lines disagreed | A re-estimate only moves the tax and what follows from it (actual P&L, end amount, and start amounts under AFTER_TAX_RESERVE), from each stored cycle's own net and STT |
| Ledger tax true-up | Two syncs at once could post the same correction twice (a random id per correction) | Corrections are numbered (`…:TAX:1`, `…:TAX:2`); the second sync's lines hit the UNIQUE key |
| New UI: older pages | 14 older pages kept their own "📊 ATIP" brand, and the six Command Center pages their "AT ATIP" logo and a second PAPER pill, under the shell's | Hidden inside the shell; the page title stays, in the shell's text colour (`atip.css`, `command_page.py`, `command_more.py`, `global_asia.py`, `enterprise_page.py`) |
| New UI: `/login` | Always dark, its own colours: the one page off the design system | Rebuilt on the tokens (light and dark, the saved theme), labelled fields, Enter signs in; still no shell, as nobody is signed in |
| Theme | The News tab's impact panel kept its dark-theme colours in light mode (91 low-contrast texts); the Orders tab's broker banner stayed pale yellow in dark mode; the market-tone badge had blue text on dark green | Theme tokens (`global_asia.py`, `news_impact_view.py`, `server.py`, `w28_assets.py`) |
| Sizes | Labels under 12 px in the new kit itself (Sessions tiles 11.5 px, chart axes 11 px, Compliance pills 11 px, Lists & scans chart 9 px) | 12 px |
| Phone | `/compliance` scrolled sideways at 390 px (a long unbroken word in a table) | Table cells wrap |
| Accessibility | Dark-mode primary buttons: white on light blue; 3 unlabelled selects on `/app`; two scroll boxes keyboard users could not reach; the two tax check boxes under the 24 px target size | Brand ink colour, labels, `tabindex` + names, spaced 20 px boxes. axe-core now finds no violation on any screen |
| Test | `test_pages_serve_with_token_nav_and_kill_switch` failed whenever the random dashboard token held "__" | The token is removed before looking for a left-over `__PLACEHOLDER__` |

Tests: `tests/test_recheck_fixes_2026_11_03.py` (new, 12) and 9 new cases in `tests/test_session_cycles.py`. The race case fails on the old ledger code and passes on the new.

## Found, not changed (owner's call)

- **Server-side Buy lock.** C1 is enforced in the screens only: `POST /api/orders` and a watched rule saved from Trade setup accept a BUY on any signal. Enforcing it on the server changes order behaviour, so it waits for the owner.
- **Environment at fire time.** A rule saved while the order window said PAPER fires in whatever environment is set when it triggers (order rules store none), and the window's badge is read once per page load.
- **Futures STT in backtests** is the 2026-27 rate (0.05 %) for every date. A backtest over 2024 – Mar 2026 charges more STT than was in force then (0.02 %).
- **Older pools' cycles** are not re-estimated when the tax settings change (only the current pool's are), so an FY that spans a pool restart mixes rates.
- **The enterprise pages** (`/account`, `/admin`, `/admin/console`, `/app`) are themed and inside the shell but keep their older plain layout. In single-user mode they only say enterprise features are off.
- `/command/signals` is 5 px too wide at 320 px, below the brief's 360 px minimum.

## Test results

- **Full suite on the branch (12:50 IST):** 2,400 passed, 62 skipped, 9 failed. The 9 are known, and none is from this change:
  - 6 `test_asia_markets` tests, date-dependent: their fixture bars end 26 Oct, and they fail the same way on master;
  - 3 `test_w39_perf_budget` tests, blocked by the sandbox; outside it 14 of 14 pass.
- **Market-hours race.** Earlier runs during market hours also hit the known race: a dashboard order-monitor thread started by one test fires triggers in a later test's database. The same file set failed or passed on master and on this branch depending on the minute it ran, and passed alone.
- **Screens after the fixes, on a server running this branch over a fresh live-database snapshot:**
  - 58 screens and 14 Scores tabs, light and dark;
  - no JavaScript error, no axe-core violation, no text under 12 px;
  - no sideways scroll at 390 px;
  - every shell part shown on desktop and phone.
