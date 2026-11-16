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

---

# 3 Nov 2026 night — full code review, scores re-audit, backtest, design system (prompt 10:54 PM IST)

Branch `claude/full-review-2026-11-03` (from master `29b488e`). Reports: `docs/reports/2026-11-03-fullreview/`
(CODE_REVIEW_REPORT, SCORE_AUDIT_REPORT, BACKTEST_VALIDATION_REPORT, CHANGES), `DESIGN.md`, and the test report
`docs/testing/ATIP_Full_Review_Test_Report_2026-11-03.md`. Delta zip: `deliverables/2026-11-03/ATIP_full_review_delta_2026-11-03.zip`.

**What changed for the owner**
- **Orders**
  - One trigger pass can no longer sell a position twice (a stop firing cancelled its target, and the stale target was sent too).
  - A stop or target that fails is tried again instead of being dropped.
  - While the kill switch is on, automatic exits wait and fire when it lifts.
  - An expired confirmation can't be tapped through after hours.
- **Every order send asks first.** One dialog shows the symbol, quantity, price, value, stop-loss and PAPER/LIVE: the Confirm buttons, the order ticket, baskets and SIP runs.
- **Order ticket.** It refuses a Buy on a stock without a BUY signal. Sessions are not affected.
- **Ten sessions.**
  - Each card shows the stock's rank, why it was picked, the seven score badges, entry / target / stop, P&L and wins.
  - The status line says how many sessions are held, queued or only planned.
  - Home has the KPI strip: market health, Nifty 50, sessions filled, today's P&L, "Updated HH:MM:SS IST".
- **Screens and apps.** The dashboard and the apps open dark; light is one click away. A changed price flashes once.
- **Data.** TDPOWERSYS's history had 165 raw pre-split rows; the repair runs at deploy. An unknown 200-day average is no longer counted as "below".
- **Alerts.** VIX / selloff / CRI / Trade-of-the-Day messages go out once a day. A missing market-health score no longer stops the post-market alerts.
- **Calendar.** From December, ATIP warns when next year's NSE holidays are missing; 2027's are not listed yet.

**Owner decisions offered (not changed):**
- Flip VPI's ATR reading and re-set the BUY gate (evidence in the backtest report).
- A server-side Buy lock.
- Moving the evening jobs out of the 21:00 stop window.
- Add NSE's 2027 holidays.
- S1's target is 4 %; the others are 3 %.

---

# 4 Nov 2026: independent verification of the 3 Nov work, and its fixes (master 7887351, live since 23:45 IST)

Owner prompt 4 Nov 2026 06:00 PM IST: verify every 3 Nov claim with evidence, then fix what fails. Report:
`docs/reports/2026-11-04-verification/VERIFICATION_REPORT.md`; testing report `docs/testing/ATIP_Verification_Test_Report_2026-11-04.md`;
delta zip `deliverables/2026-11-04/ATIP_verification_delta_2026-11-04.zip`. Merged into master at 22:55 IST (`7887351`); live on the Mac since 23:45:47 IST (`f8002be`, pid 42595).

**Verified as claimed:** the code-review fixes; ten sessions with ten distinct stocks (four dates, independent ranking); the seven
score formulas (42/42 recomputed within 0.01 without the engine); no look-ahead; the backtest (reproduced to the last digit);
the order dialog; dark default; the KPI strip and cards.

**Found and fixed**
- **Scores.** A re-score of a past day read news published after it (MSI). A stock with no indicators yet was scored from
  stand-ins (a "safe" CRI). A score's breakdown kept parts that no longer counted.
- **Sessions.** After the day's 10 buys, sessions that sold kept queuing buys the daily limit refused (3 failed NEULANDLAB buys
  on 4 Nov); they now wait and say why. Command Center's Confirm & send now shows shares and the order value.
- **Screens.** The dark-theme alert badge was unreadable (1.9:1); tables sort numbers as numbers; numbers line up right;
  P&L shows ▲ + / ▼ − as well as colour; the stock panel holds keyboard focus; "/" shows "Updated HH:MM:SS IST".
- **Tests.** A test dated to 3 Nov failed from 4 Nov; a flipped SAFETY sign in ATIP passed every test — both closed.
- **Apps** 2026.11.04.1 (8); no native change.

**Owner decisions (not changed):** ATIP, VPI and RRI stay BROKEN out of sample (VPI flip + BUY gate re-set, RRI redesign);
"momentum" sessions rank by ATIP, not price momentum; `max_orders_per_day` 10 = the number of sessions, so no same-day re-entry.

# 5 Nov 2026: the top bar in the stop window, and the session card's P&L (VF-34, VF-35)

Both were found on the live Mac right after the 4 Nov deploy and were there before it. Branch `claude/fix-vf34-vf35-2026-11-05`,
merged into master at 17:33 IST (`fde1951`); live on the Mac since 21:11 IST 5 Nov.
- **Top bar (VF-34).** Between 21:00 and 06:00 the "⏸ ATIP stopped — resumes 06:00 IST" pill, next to the feed pill, pushed the
  alert, theme and profile icons past the right edge whenever the watchlist was open (Command Center, Signals, Orders &
  execution, and every page after them). The bar now gives way in a fixed order before anything is pushed out: the search's
  "Ctrl K" hint goes, the feed pill says "Stale · 20 h 24 min" / "Closed · 16:45", the run pill says "Stopped till 06:00", and
  only then do the icons fold into "⋯" (which shows the unread count); on a very tight bar the breadcrumb keeps
  its section ("Trade") and drops the page name, which the page's heading repeats. The full sentence stays in the pill's tooltip and in the
  stop banner under the bar. The search box no longer shrinks under its own label.
- **Session card (VF-35).** On Home a card split "P&L +₹1,144.40" after the sign. A figure now stays whole; when the P&L and
  the wins do not fit side by side, "Wins" moves to the next line.
- No database change, no app change (the apps hide the run pill and load the cards from the server).

# 5 Nov 2026: profit-calculation audit of the ten sessions (prompt 8:05 PM IST)

Report: `docs/reports/2026-11-05-profit-audit/PROFIT_AUDIT_REPORT.md`. Branch `claude/pnl-audit-2026-11-05`,
merged into master at 22:42 IST, 5 Nov (`fd0e93a`, not pushed); live at the next restart. Tracker rows PA-REC, PA-01..PA-11.

**Verified:** today's realised P&L (5 trades: gross ₹1,704.13, charges ₹358.01, net ₹1,346.12, ₹777.04 after the tax
estimate) and every session's since-start P&L (10 trades, net ₹3,609.50, ₹2,404.84 after the estimate). An independent
recompute from the raw fills (`tests/verification/pnl_reconcile.py`, no app P&L code) agrees with Home, Accounting, the
APIs, `session_cycles` and the general ledger to ₹0.01 (per-order rounding of charges). Dhan's trade book is empty, as it
should be: the sessions trade on paper. No orphan, duplicate or mis-tagged fill.

**Found and fixed**
- **PA-01 (high).** The real account's P&L (`/api/pnl`, `pnl_daily` LIVE) carried the paper account's cash (₹3.17 lakh;
  the real Dhan account holds ₹2,350.16), so the paper sessions' buys read as a LIVE loss of ₹1,43,596.19 on 4 Nov and a
  25-32 % drawdown, which would have refused every BUY once LIVE. The LIVE book now asks the real account; day P&L compares
  like with like. `tools/repair_live_pnl_daily.py` clears the stored rows (dry run by default; run after a backup).
- **PA-02.** A partial fill now records, and puts exits on, only the shares that filled.
- **PA-03.** A trade sold in parts over two days books each part on its own day (the session's daily loss limit counted
  the first part twice).
- **PA-04.** `/execution`'s "realised" now says it is gross and covers the whole paper account.

**Owner decisions (not changed):** Home's headline is after a per-trade tax estimate without same-day set-off (PA-05); the
session card's P&L vs the table's realised (PA-06); before LIVE, settle rules from Dhan's trade book (PA-07); S7's
−₹15.70 cash from a stale sizing price (PA-08); the DP charge rate (PA-09); unpriced open trades (PA-10).

---

# 5 Nov 2026: independent re-verification and the owner's decisions (branch claude/verify-2026-11-05; merged 16 Nov 2026 on claude/kite-formula-fix-2026-11-16)

Owner prompt (4 Nov 06:00 PM IST) run again on 5 Nov from 01:05 PM IST as a fresh full pass; owner decisions the same
afternoon: VF-01 fix only if out of sample proves it and re-gate BUY, VF-08 keep the ATIP rank, VF-04 raise
`max_orders_per_day` to 20. Report: `docs/reports/2026-11-05-verification/VERIFICATION_REPORT.md`; testing report
`docs/testing/ATIP_Verification_Test_Report_2026-11-05.md`; delta zip
`deliverables/2026-11-05/ATIP_verification_delta_2026-11-05.zip`. Not merged and not deployed: the live Mac still runs `0ab3de9`.

**Verified as claimed:** the code-review fixes (24 of 26 High/Medium; Q-H1 and P-H1 partial); ten sessions on four more dates and
the live board; the seven scores for all 498 stocks within 0.01 by an independent script, with the indicators recomputed from
OHLCV; no look-ahead (extended corruption); the backtest to the last digit; the 3 Nov reweights better in and out of sample;
the UI sweep (168 runs, 0 axe violations); the order dialog.

**Changed:** VPI's V reads ATR as calm = high (VPI BROKEN → WORKING out of sample; ATIP BROKEN → WEAK) with the VPI BUY gates
re-set in-sample to 76.5 / 66.5; MRI labelled "Momentum-reversal"; the evening re-score fetches the session's F&O file (MSI's
PCR); a PCR-only FII/DII row is not "flows in"; a stop fill also cancels a target pending confirmation; `ATIP_ORDER_MONITOR=off`
for tests and QA copies (the suite was flaky in market hours); dead `FILL_TARGET` removed; DESIGN.md screenshots in git; apps
2026.11.05.1 (build 9).

**Not changed:** RRI (its in-sample redesign failed the out-of-sample rule; still BROKEN, owner); hard-coded colours in legacy
pages (822); session names; VF-34 / VF-35 (another session). **To do at deploy:** `max_orders_per_day: 20` in the live
`atip_data/config.json`.

# 6 Nov 2026: post-market RCA of the ten sessions, and a daily RCA at 16:00 IST (prompt 05:28 PM IST)

Report: `docs/reports/2026-11-06-post-market-rca/POST_MARKET_RCA_2026-11-06.html` (+ `.pdf`). Testing:
`docs/testing/ATIP_PostMarket_RCA_Test_Report_2026-11-06.md`. Branch `claude/rca-2026-11-06` from master `ea4f2a0`;
**committed as `1966119` (6 Nov 2026, 21:44 IST); merged to master as `2885582` (16 Nov 2026, 00:02 IST); live since 00:52 IST 16 Nov 2026.** Delta zip: `deliverables/2026-11-06/ATIP_post_market_rca_delta_2026-11-06.zip`.

The day (all PAPER, IST): realised net −₹1,107.85 (4 sold: 1 won, 3 lost; charges ₹282.17), day P&L marked −₹763.12,
while Nifty closed +2.01 %. BHEL (S10), LODHA (S9) and ADANIENT (S2) were stopped by the 2 % stop, which sat at
0.55 / 0.53 / 0.67 of each stock's 14-day ATR; each was sold within minutes of its low and closed at or above its entry
(−0.09 %, +1.93 %, +0.39 %). None was a BUY signal (rank-fill HOLD picks), and LODHA was bought at 09:16 into a −1.00 %
gap. Not market-wide, not news.

The tempting fix fails: a 1.5 × ATR stop turns today into +₹1,641, but over 42 sessions (420 auto-pick trades,
`tools/rca_validate_rules.py`) it loses 2.4 times more than the 2 % stop. The rules that held up: buy only on a break of
the first 30 minutes' high (−0.31 % a trade vs −0.64 %, better on 30 of 42 days), skip rank-fill stocks under 63
(−0.45 %), no buys while Nifty is weak (−0.44 %). None of them makes the picks profitable alone.

## Added

| File | What |
|---|---|
| `portfolio/post_market_rca.py` | The daily RCA: session summary (realised, day MTM, max drawdown), intraday path of every stock held (low / high after entry, recovery start on a stated 5-minute rule, back to entry, stopped before recovery), root causes a–h with evidence, 15 what-if rules replayed on 1-minute candles, chart data. Read-only; one row a day in `post_market_rca`. `python -m portfolio.post_market_rca [--date] [--no-fetch] [--json]` |
| `pipeline/w39_jobs.py` | `post_market_rca` at 16:00 IST on market days (config `"post_market_rca": {"enabled": true, "time": "16:00"}`); a no-op on the view-only server |
| `dashboard/rca_routes.py` | `/rca` (Reports → Post-market RCA; the apps get it under More), `/api/rca`, `/api/rca/guards`, POST `/api/rca/run`, POST `/api/rca/guards` (token; viewer refused) |
| `orders/entry_guards.py` | Five auto-pick guards, **all OFF**: `guard_orb_minutes`, `guard_min_score`, `guard_nifty_weak`, `guard_entry_not_before`, `guard_sector_cap`. Turning one on needs ack (the page's dialog or `--ack`) and is logged as a session event `GUARD`. One call in `orders/autopick.py` |
| `tools/rca_validate_rules.py` | Re-runs the 42-session study on any dates (Dhan 5-minute candles, cached) |
| `tests/test_post_market_rca.py` | 14 tests |

Mobile apps 2026.11.06.1 (Android versionCode 9, iOS build 9). Tracker: sheet "Post-market RCA 2026-11-06", rows
RCA-REP, RCA-01 … RCA-19 (master Seq 544–563, CSV Seq 571–590, phase 24).

## Found, not changed (owner's call)

* Rank-fill HOLD picks lose 0.66 % a trade (384 study trades): keep, restrict (guards) or pause (VF-01).
* 09:16:09 IST "auto-pick error: database is locked": the open's first monitor pass failed; BHEL filled ₹152 under its stop.
* The 30-second price poll misses stop wicks (ADANIENT: touched 10:54, sold 12:03 at the same price).
* SPI and fund_score are NULL for every stock on every day; the global feed's S&P −4.36 % stayed unchanged all day;
  TRAIL_MOVED is logged every 30 s without a move.

## Test results

Full suite on the branch (17:54–18:03 IST): 2,572 passed, 65 skipped, 4 failed — the 3 sandbox-socket tests that fail on
master too, and `test_order_paths_are_untouched`, which needs the work committed (passes in a committed clone).

# 6 Nov 2026: the order monitor's 09:15 pass lost a minute to "database is locked" (RCA-13)

Branch `claude/amazing-cray-703f0e` (from master `ea4f2a0`); merged to master as `c865ae2` (16 Nov 2026, 00:02 IST); live since 00:52 IST 16 Nov 2026. Found by the 6 Nov post-market RCA.

**What happened (IST, `atip_data/atip.log` / `atip.jsonl`, `order_rules`, `session_events`).** S9 (LODHA) and S1
(AUROPHARMA) each had an auto-pick still waiting from 5 Nov. The order monitor's first market-hours pass began about
09:15:07. Auto-pick's `cancel_stale` cancelled LODHA at 09:15:07.80 (`rules.cancel_rule`, on its own connection, committed)
and wrote S9's AUTO_PICK_CANCELLED event on the pass's connection without committing it. The next `cancel_rule`, on a new
connection, then waited the whole busy timeout (60 s) behind that event and logged `auto-pick error: database is locked`
at 09:16:09.689; closing the pass's connection rolled S9's event back (it is missing from `session_events`). check_triggers
ran only after that, and the pass after it cancelled AUROPHARMA at 09:16:39.81 and sold S10's BHEL at 408.20 at 09:16:46 on
its 410.78 stop (59 shares, ₹152 under the stop). BHEL had already traded at 410.00 by 09:15:03 (`live_quotes` low); there
are no BHEL ticks to say where it was at 09:15:08 or 09:15:38, when the two passes the lock swallowed would have checked it.

**Not the cause:** the 09:15:03 live-quote refresh and the 09:15:05 `session_reports` tick had both finished (09:15:03.53,
09:15:05.27) before the first cancel; no index-feed flush failure is logged. The lock was auto-pick's own connection.

**Earlier days:** this is the only `auto-pick error` and the only order-monitor lock in the log (from 24 Sep). 5 Nov had one
stale pick (RADICO, cancelled with its event at 09:15:24); 6 Nov was the first day with two. Any morning after two or more
picks are left waiting would have repeated it.

**Fixed**
- `orders/autopick.py` `cancel_stale` commits each cancellation's event before the next `cancel_rule` opens its connection.
- `dashboard/server.py`: the monitor's pass is `_order_monitor_pass()` (auto-pick, then check_triggers, as before). A pass that
  loses to a database lock, whoever holds it, runs again after 5 s (`MONITOR_LOCK_RETRY_S`) instead of 30 s, with a warning
  in the log. The 30 s cadence, the order of the two steps, the 60 s busy timeout and every trading rule are unchanged.

Tests: `tests/test_rca13_monitor_lock.py` (new, 6) and one new case in `tests/test_autopick.py`; the new auto-pick case fails
on master with `database is locked` raised from `cancel_stale` → `rules.cancel_rule`, as in production.

**Test results (6 Nov, 22:04–22:22 IST, outside market hours):** master `ea4f2a0` 2,559 passed, 65 skipped, 3 failed; this
branch 2,566 passed, 65 skipped, the same 3 failed (`test_w39_perf_budget`: real sockets, refused by the sandbox).

# 16 Nov 2026: the 9–13 Nov session review and the Kite breakout-retest port (KGF-01..KGF-21, PAPER only)

Owner prompt 15 Nov 2026 11:52 PM IST. Branch `claude/kite-formula-fix-2026-11-16` from master `7244fd0`. Report:
`docs/testing/ATIP_KiteFix_Test_Report_2026-11-16.md`; tracker rows KGF-01..KGF-21 (sheet "Kite formula fix 2026-11-16").
Evidence reproduced first (`.ux_build/kite-fix-2026-11-16/repro/`): week MTM −₹328.30 = `pnl_daily` PAPER equity change,
calibration 34/34 exit reasons (corr 0.968), K1 2,349 trades at +0.67 %, K1 with breadth50 ≥ 50 +0.97 %.

**Merged first (KGF-01):** `claude/verify-2026-11-05` (VF5-01..16: VPI calm volatility, BUY gate, VF5-06 a stop fill cancels a
target pending confirmation, PCR, order monitor off in tests), `claude/rca-2026-11-06` (daily 16:00 IST RCA, `/rca`, entry
guards all OFF), `claude/amazing-cray-703f0e` (RCA-13: auto-pick no longer locks its own 09:15 pass; a locked pass retries
in 5 s). Conflicts were docs, the tracker and the apps' build 9 (one kept, 2026.11.06.1).

**Order path (PAPER; nothing here can place a real order)**
- **Fresh price, LIMIT entry (KGF-02).** A pick needs an LTP under 60 s old (`waiting_for_ltp` otherwise, nothing queued);
  it buys with a LIMIT at min(LTP, signal close) × 1.005, tick-rounded down; `signal_close`, `pick_ltp`, `pick_ltp_ts` are
  stored on the rule; a stock more than 1.5 % under its signal close is skipped (`skipped_falling`, logged once a day).
- **14:45 IST cut-off (KGF-03)** for new swing entries, per session (`no_entry_after`).
- **Tick rounding (KGF-04)**, `orders/ticks.py`, Decimal only: BUY limit/trigger and a long's stop round down, a long's
  target rounds up; on create, on bracket legs, on trailing moves and on paper fills.
- **Regime gate (KGF-09)**: `scores/breadth.py` (`market_breadth`, breadth20/50 of the scored universe); a gated arm takes no
  new entry under its gate; exits always run. Banner on Sessions, `/ab` and the apps.
- **Arms (KGF-13)**, `orders/arms.py` (`session_settings arm:S1..S10`): S1 Momentum (ATR stop, gate), S2/S3/S4 Kite
  retest K2/K1/K4, **S5 Control** (today's rules, no gate), S6 Recovery (ATR), S7 Wide-stop, S8 Sector (≤ 2 per industry),
  S9 Bracket 3/6, S10 Retest fast-gate (breadth20). Risk-budget arms buy min(cash, max(₹10,000, ₹625 ÷ stop %)). Open
  trades keep their legs. Weekly A/B report Sundays 18:00 IST (`portfolio/ab_report.py`, `/ab`).
- **Kite breakout-retest (KGF-10/11/12):** `strategy/breakout_retest.py` (exact parameters), `/api/scanner-data`, the
  "Algo Signals" page; the retest arms rest a **DUMMY-** buy at the breakout low (`orders/algo_gtt.py`, table
  `algo_dummy_gtt`), stop 5 % below, created only after the buy; valid 10 trading days; one active per symbol (BEGIN
  IMMEDIATE + partial unique index); `POST /api/algo-orders/gtt-state` (read-only) and `/live-gtt` (dummy only, needs the
  JSON boolean `confirm_live: true`). Broker reads are read-only through `ReadOnlyBroker`.
- **Time exit** (`max_hold_days`, 20 / 40 trading days) through the entry's own STOP leg; **stop exits slip 15 bps** in
  paper (KGF-17, `paper_stop_slippage_bps`, default 3 × `paper_slippage_bps`); **re-entry cooldown** (KGF-19) default off.

**Data and operations**
- **Instrument master (KGF-05):** `data/instruments.py`, Dhan scrip master daily at 08:00 IST (`instrument_master`, tick in
  rupees); ZOMATO→ETERNAL, TATAMOTORS→TMPV, MOFSL→MOTILALOFS; REITs (series RR) dropped from the equity universe. A
  simulated full day of lookups: 0 `security_id not found` warnings (was 121–151 a day).
- **Pre-open health gate (KGF-06):** `ops/preopen_health.py` at 08:45 IST (retries 08:50 / 09:00 / 09:10): Dhan token
  (DH-901), quote feed, today's scrip master, yesterday's scores. Failing → auto-pick waits, a banner says why, the token
  renewal is retried and one Telegram alert is sent; Telegram delivery itself now retries with backoff.
- **Bhavcopy (KGF-07):** retries with backoff, then the day's bars from Dhan (whose daily-history end date is exclusive —
  why the 16:45 run never got the same day's bar), both sources logged (`eod_price_source`).
- **Intraday completeness (KGF-18):** 16:15 IST check, alert under 95 % of the universe; `tools/backfill_intraday_bars.py`
  for the 13 sessions of 6–23 Oct with no 15-minute bars.
- **Kite Connect (KGF-21):** `brokers/connectors.py` stays read-only, plus GTT list and instruments reads.

**Accounting**
- **LIVE cash flows (KGF-08), found first:** the LIVE "cash" fall ₹5,04,275 → ₹2,62,861 and the 41.8 % drawdown are PA-01
  (the paper balance stored as the real account's cash) still running — the Mac was not restarted after the 5 Nov merge —
  not withdrawals; real LIVE P&L 2–13 Nov +₹515.34. Under PAPER nothing was blocked. Built: `portfolio/cash_flows.py`
  (deposits / withdrawals / transfers, PENDING until confirmed), day P&L and drawdown net of confirmed flows in
  `portfolio/pnl.py` and `orders/risk.py`, so a withdrawal never trips `max_drawdown_pct` while a real loss still does;
  `tools/repair_live_pnl_daily.py --paper-match`. **The LIVE history repair writes the live DB: owner's yes needed.**
- **Tax (KGF-14):** FY short-term capital gains after set-off, 20 % + 4 % cess: FY 2026-27 ₹1,346.74 on the 34 cycles
  (net STCG ₹6,474.72 with STT added back) against the per-cycle ₹3,065.29, which is now labelled "Tax reserve (per-cycle,
  cash set aside)". "Estimate only — confirm the treatment with a CA; not tax advice."

**Research (KGF-17):** `research/session_ab_replay.py`, `research/kite_retest_study.py` reproduce the 15 Nov evidence
exactly; with 15 bps stop slippage the calibration stays 34/34 (corr 0.968).

**Apps:** 2026.11.16.1 (build 10): native regime-gate strip; the rest are server screens. Not compiled on this Mac (no
Xcode, no Java / Android SDK).

**Tests:** full suite 00:41:42–00:51:20 IST 2,754 passed, 65 skipped, 4 failed (pre-existing: 3 sandbox sockets, 1 time-of-day
feeds test; 0 regressions); 166 new tests. Pages: Test + Prod 88/88 clean, live 20/20.

## Merge report (16 Nov 2026, IST)

- 00:20 branch `claude/kite-formula-fix-2026-11-16` merged the three fix branches (`98597f0`, `2885582`, `c865ae2`).
- 00:42–00:49 committed `e1f9fe2` (code + tests), `38d91a7` (apps build 10), `2b8a3f4` (session board for retest entries).
- CI 00:41:42–00:51:20 (suite above) → staging 00:30–00:47 (Test / Prod local servers, replay, sweep) → prod:
  rollback branch `claude/live-running-2026-11-16` = `424ae48`; backup `atip-pre-release-20261116-004836.db` (00:48:36);
  **merged into master `2e407e6` at 00:51:59**; restart 00:52:23 (pid 55822 → 14211); arms, breadth, instrument master,
  scan and the 6–23 Oct intraday backfill (168,194 bars) run on the live DB 00:52:58–00:56:05. Not pushed.
- 16 untracked screenshots under `docs/testing/screenshots/full-review-2026-11-03/` were identical to the copies the
  5 Nov verification branch committed; moved aside for the merge and recreated identically. All other untracked files in
  the folder (125 entries: tracker backups, prompts, `.ux_build/`, `UI_UX/`, `deliverables/`, `design/`, screenshots)
  were left untouched; list in `.ux_build/kite-fix-2026-11-16/untracked_before_merge.txt`.
- **Owner, still open:** `max_orders_per_day` 20 (VF-04) not applied (the session's permission check refused the live
  config edit); the LIVE `pnl_daily` repair (`tools/repair_live_pnl_daily.py --paper-match`); PA-05.

# 16 Nov 2026 (morning): the arms' risk budget compounds with the session (owner)

The owner set `risk_limits` themself at 05:36 IST (`max_order_value` ₹1,00,000, `max_orders_per_day` 50,
`max_open_positions` 50, `max_daily_loss_value` ₹10,000, `max_drawdown_pct` 10) — which closes the VF-04 item above — and
asked whether anything stops a session compounding. `max_order_value` does not (a session's orders are exempt, 29 Oct);
the KGF-13 risk budget did: a fixed ₹625 at the stop held nine arms to ₹10,000–31,250 per buy however large the session grew.

- `orders/arms.py`: `risk_budget_pct` 2.5 (of the session's cash) replaces the fixed `risk_budget` ₹625 in every arm, so the
  buy is min(cash, max(min_buy_value, cash × 2.5 % / stop %)): the same ₹12,500 at a 5 % stop on ₹25,000 as before, ₹50,000
  once the session has ₹1,00,000. A fixed `risk_budget` still works for an arm that sets one. S5 (control) is unchanged.
- `orders/sessions.py` `pool_quantity` passes the session's investable cash to `arms.value_cap`.
- Live DB: the nine stored arms switched by `.ux_build/risk-budget-2026-11-16/migrate_arms.py --apply` (`arms.set_arm`,
  one ARM session event each); open trades keep their exit legs (checked: 20 open rules identical on a copy).
- Test: `test_risk_budget_grows_with_the_session`.

# 16 Nov 2026: the history backfill lost the last session of every window (Dhan's toDate is exclusive)

Dhan's daily-history API returns bars from `fromDate` up to the day **before** `toDate`. `data/history_backfill.py`
asked for each 365-day window as (start, end), so every window that ended on a session came back without it, and the
next (older) window ends the day before, so nothing asked for that day again. The W39 tests stubbed the fetch with an
inclusive fake, so they could not see it.

**Measured** on the 15 Nov 2026 19:15 IST backup (`atip-scheduled-20261115-191511.db`, opened `mode=ro&immutable=1`,
16 Nov 05:46 IST): **1,499 symbol-days missing at a window edge in 331 symbols**: 264–319 symbols each on 3 Mar 2020,
2021, 2022 and 2023 and the Saturday session of 2 Mar 2024, plus 47 more on other edges. Of all the sessions missing
before 3 Mar 2025 in the backfilled symbols (1,572 in 330 symbols), 1,486 are window edges. 13 fall in the last year:
2025-11-13 for CUPID, INOXINDIA, KIRLOSBROS, KSB, MTARTECH, PRIVISCL, RUBICON, SANSERA, SHRIPISTON and STLTECH;
ICICILIFE 2025-10-21 and 2026-10-21; HEGAM 2026-09-21. (8 more edges on 2023-11-14 are listed "unconfirmed": Diwali
Balipratipada, a holiday.)

**Fixed:** `data.dhan.fetch_daily_through(symbol, start, end)` asks Dhan for end + 1 and drops anything after `end`;
the backfill uses it, so each window keeps its last session and back-to-back windows never store a day twice.
`fetch_historical_daily` is unchanged (its docstring now says `to_date` is exclusive).

**Repair tool:** `tools/dhan_boundary_days.py` lists the edge days from Dhan's window cache (`atip_data/raw/dhan`)
that have no bar inside a symbol's stored history; `--apply` asks Dhan for each day on its own (read-only history call,
never an order call) and stores it like the backfill. A dry run opens the database read-only (a `--db` copy immutable).

    python -m tools.dhan_boundary_days                  # dry run on the live DB, nothing written
    python -m tools.dhan_boundary_days --apply          # owner's yes, after `python -m ops backup --kind pre-release`

**After**, on a scratch copy of the same backup, with a **simulated** Dhan (toDate exclusive, a session on every day
some stored symbol traded; the real token is in the vault and was not used): `--apply` made 1,507 requests and added
exactly 1,499 rows (744,688 → 746,187, no duplicate symbol-date); the edge gap is **0** (only the 8 holiday rows stay
unconfirmed); all missing pre-2025 sessions 1,572 → 86 in 5 symbols, none at a window edge.

**Not changed, checked:** `run_historical_pipeline` (16:30 IST, 5 days ending the session) and
`sync_index_benchmark_history` also leave out their end day, but nothing is lost: NSE's Bhavcopy (16:45) or the Dhan
fallback (`data/bhavcopy.py`, already asks day + 1) fills the session, and the next day's run covers it. Asking for
end + 1 there would let Dhan's bar arrive before the Bhavcopy, whose upsert only refreshes close and volume, so
the session's open, high, low and `source` would stay Dhan's: a change to the live EOD path, left for the owner.

**Tests:** `tests/test_dhan_window_edges.py` (a fake Dhan client that answers as Dhan does: the old call loses
Friday 3 Mar 2023, the new one keeps it; the backfill over 2022–2025 stores every session once; a candle sent twice
is kept once; DH-907 on a holiday is no bar; the tool's plan, apply, second run and read-only copy); two assertions
in `tests/test_w39_history.py` follow the end + 1 window. Full suite on the branch merged with master (06:17–06:27
IST 16 Nov): 2,764 passed, 65 skipped, 4 failed — the same 4 fail on master `ea53a82` (3 sandbox sockets, 1
time-of-day feeds test); 0 regressions.

## Live (owner's yes, 16 Nov 2026, IST)

- **Backup** `atip-pre-release-20261116-060358.db` (06:03:58, VERIFIED, sha256 `33b10be6…`).
- **Fill** 06:04:30–06:12:21: `--apply` on the live DB, 1,507 Dhan requests (read-only history). **1,499 new rows**
  (744,688 → 746,187), no duplicate symbol-date; 2 Mar 2024 now has 330 bars. The 8 rows of 2023-11-14 came back
  DH-907 "no data present" (Diwali Balipratipada; NSE's archive has no file for it either) and stay listed as
  unconfirmed. Dhan sent three candles twice (ICICILIFE 2026-10-21, POONAWALLA 3 Mar 2020 and 2021, identical):
  one row each; `fetch_daily_through` now drops a repeated date and the tool counts DH-907 on an unconfirmed day as
  "no bar" (commit `ee5ce66`).
- **Re-score** 06:13:16–06:13:32: `technical_indicators` and `technical_ext` of the 12 symbols whose filled bars sit
  inside their indicator windows (CUPID, INOXINDIA, KIRLOSBROS, KSB, MTARTECH, PRIVISCL, RUBICON, SANSERA,
  SHRIPISTON, STLTECH, ICICILIFE, HEGAM) recomputed on their 27 sessions (291 rows each, bars up to each session
  only). On 13 Nov their `tech_score` moved by at most 0.06. The 13 Nov universe re-score that followed also applied
  the VPI formula deployed at 00:52 (VF5-01) to all 499 stocks (VPI +4.3 on average, top-15 ATIP overlap 10 of 15);
  **the owner chose to keep the recorded 13 Nov scores**, restored at 06:16:07 from the backup: `ai_scores` 499,
  `predictions` 499, `score_components` 27,421, `market_health` 1, `technical_ext.rs_sector_pctile` 3,240 (the
  industry percentiles of the 27 sessions). Checked against the backup: those tables are identical; the live DB
  differs only by the 1,499 bars and the 12 symbols' indicator rows. Earlier sessions' scores stay as recorded.
- **Merge and restart:** master fast-forwarded to `ffa2051` at 06:28:12 (this branch with the risk-budget merge
  `1a4c7ff`, the mobile build `1b99fee` and the RCA docs `e1cdfda`); restart 06:28:23, pid 14211 → 11912,
  dashboard 200 at 06:28:27, startup log clean (PAPER). Rollback branch `claude/live-running-2026-11-16-0628` =
  `ea53a82` (what pid 14211 ran). Not pushed. From tonight the 22:20 IST backfill asks each window up to end + 1.

## Built: seven years of NSE's Bhavcopy kept locally, and a weekly check against it (owner, 16 Nov 2026)

**Why.** The 7-year history in `prices_daily` is Dhan's, adjusted for splits and bonuses as of the day it was fetched;
NSE's own end-of-day file was kept only from 30 Jan 2025 (309 CSVs in `atip_data/raw`), full-market rows start
28 Jul 2026 and delivery % 3 Mar 2025. NSE's archive serves all seven years to this Mac (checked 06:05–06:43 IST):
the old `cmDDMONYYYYbhav.csv.zip` up to 5 Jul 2024, UDiFF `BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip` from 5 Jul
2024, `sec_bhavdata_full_DDMMYYYY.csv` (delivery) throughout; a holiday is a 404 in both layouts.

**`data/bhavcopy_history.py`**
- Keeps each session's files as published (`atip_data/raw/nse_archive/cm/<YYYY>/`, `delivery/<YYYY>/*.csv.gz`) and
  every row, all series, in one column set as the lake dataset `nse_cm_bhavcopy` (raw prices, knowledge time 20:00
  IST of the session). `nse_archive_session` records each date (session / holiday / pending / error), NSE's own
  calendar for 2019–2024, which `utils/trading_calendar.py` lacks; `tools/dhan_boundary_days.py` now takes its word.
- Runs newest first inside a time budget (the scheduler runs one job at a time, and the evening re-score must finish
  before 21:00): every day 19:50 IST for 5 minutes, Saturday and Sunday 10:00 IST for 90 minutes, one request every
  1.2 s (about 1,830 dates, two files each: the first weekend run finishes the backlog). A 404 within 3 days is
  "pending", older a holiday — unless 50 or more bars are stored for the day, then "error". Three errors in a row
  (403 / 5xx / timeouts: NSE's bot protection) stop the run, so ATIP's other NSE calls on this address are not put
  at risk; the next run asks again.
- `verify()` (Sunday 12:00 IST, report in `atip_data/reports/price_check/`): per stock, stored close / NSE close is
  one number between corporate actions when the history is right. A new ratio that holds 3 sessions is a re-basing,
  explained when `corporate_actions` or NSE's adjusted previous close shows an event that day — NSE's UDiFF file kept
  the *unadjusted* previous close on CONCORDBIO's 1:1 bonus ex-date (4 Nov 2026), so NSE's previous close alone is not
  enough; a shorter deviation over 0.5 % is a mismatch; NSE rows inside a stock's history with no stored bar are
  missing; stored bars on a day NSE had no session are phantom. Only the range archived without a gap is checked.
- `fill()` writes missing days from NSE on the stored basis (prices × the day's level, volume × the stored/NSE volume
  ratio nearby), source `bhavcopy_hist`, never over a stored row; dry run unless `--apply`.
- Seven-year retention through `db/purge.py` (lake partitions, kept files, session rows).

    python -m data.bhavcopy_history status | run [--minutes 80] | verify [--list] | fill [--apply]

**Measured** on a copy of the live DB (06:42–06:46 IST, real NSE): the 12 newest dates (11 sessions, 40,701 rows;
10 Nov a holiday) plus 2 Mar 2024 (Saturday session), 3 Mar 2023, 15 Nov 2019, 5 and 8 Jul 2024 (both layouts), and
2023-11-14 recorded as a holiday; delivery for all 16. About 290 KB of files and 125 KB of lake per session: roughly
0.5–0.6 GB for seven years. The check over 30 Oct–13 Nov: 504 stocks, **5,517 stock-days, all equal to NSE**; one
re-basing (CONCORDBIO, explained by `corporate_actions`); **27 stocks have no bar for the Sunday Muhurat session of
8 Nov 2026**, which NSE published (other stocks have Dhan's bar for it; `utils/trading_calendar.py` lists the day as
a holiday) — `fill --apply` would add them; left for the owner.

**Not done here:** delivery % before 3 Mar 2025 is in the archive but not copied into `prices_daily`; the scheduled
jobs start with the next restart (none was done for this change). Tests: `tests/test_bhavcopy_history.py` (14, a fake
NSE archive serving both layouts, delivery and 404s). Full suite 06:46–06:57 IST: 2,777 passed, 65 skipped, 4 failed —
the same 4 as on master (3 sandbox sockets, 1 time-of-day feeds test); 0 regressions.
