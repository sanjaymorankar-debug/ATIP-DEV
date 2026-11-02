**Date of prompt:** 2 Nov 2026 Time 08:47 PM >

# ATIP — full UI/UX redesign, with session accounting, management and periodic reporting

## 0. Your role and the skills to use

You are a senior product designer and full-stack engineer working on ATIP (AI Trading Intelligence Platform), the owner's personal NSE/BSE trading intelligence system. Work through this in four modes and keep them separate:

1. **Design-thinking lead.** Run the five stages (Empathise → Define → Ideate → Prototype → Test) before writing production code. Use the `design:user-research`, `design:research-synthesis` and `design:design-critique` skills.
2. **UX architect.** Rebuild the information architecture and navigation, and design every flow for task completion, error prevention and trust. The money is real, so the UI must never leave the user unsure about what will happen.
3. **UI / visual designer.** Build one design system (tokens, components, states, light and dark themes) and apply it to every page. Use `frontend-design`, `design:design-system`, `design:ux-copy`, `design:accessibility-review` (WCAG 2.1 AA) and `dataviz` for every chart, KPI tile and sparkline.
4. **Engineer.** Implement the design inside the existing FastAPI codebase as additive delta patches. Use `design:design-handoff` to write the spec before you build, and `engineering:testing-strategy` / `engineering:code-review` before you hand it over.

Work from the project root (`~\agtci\ATIP`). Deliver all changes as **delta patches in a single zip**. Do not rewrite whole modules.

---

## 1. What ATIP is today (from the code audit, 2 Nov 2026)

**Stack.** Python 3.14, FastAPI, SQLite (Postgres-swappable), APScheduler, Dhan broker (Zerodha secondary). All UI is server-rendered HTML strings with inline CSS/JS inside `dashboard/*.py` (about 16,800 lines across 60+ files).

**Pages (31 HTML routes).** Each page is its own island with its own CSS and its own "← Dashboard" link:

| Area | Current routes / files |
|---|---|
| Home / scores | `/` (`dashboard/server.py`, 12 tabs: ATIP Scores, Portfolio, Top VPI, Buy Zones, RRI, MRI, CRI Risk, FII/DII, News, Signal History, Orders, Global & Asia) |
| Command Center | `/command`, `/command/signals`, `/command/setup`, `/command/monitor`, `/command/journal` (`command_page.py`, `command_more.py`, `command_routes.py`) |
| Research | `/research`, `/screener`, `/signals`, `/market-pulse`, `/options-builder` (`w39_page.py`, `w39_routes.py`), `stock_view.py` |
| Markets | `/market`, `/global-asia`, `/data-platform` |
| Strategy & testing | `/strategies`, `/strategy-builder`, `/backtests`, `/execution-lab`, `/quant`, `/ml` |
| Trading / execution | `/trading` (`execution_page.py`), `/baskets`, `/brokers` |
| Wealth | `/wealth` (7 tabs in `dashboard/wealth_ui/`) |
| Platform | `/account`, `/admin`, `/login`, `/app`, `/compliance`, `/privacy`, `/assistant`, `/health`, `/ops` |
| Mobile | `/m` (read-only PWA). `mobile/android` (Kotlin) and `mobile/ios` (SwiftUI) are thin WebView shells with tabs Today `/m`, Command `/command` and Global & Asia `/global-asia`, plus a native status banner polling `/api/status` |

**UX problems found:**
- The home top bar has 17 flat links with no grouping. Command Center has its own, different nav. Sub-pages only link back to "← Dashboard". There is no global search and no breadcrumb.
- Every page has its own styling (colours, tables, pills, buttons), so nothing feels like one product.
- Money figures are scattered: `funds/report.py` (virtual vs actual funds), `orders/sessions.py` `ledger()` / `journal()`, `portfolio/pnl.py`, `portfolio/paper_report.py`, `enterprise/reports.py`. No screen reconciles them.
- Sessions: `orders/sessions.py` already supports 10 sessions (S1–S10) with modes MANUAL / CONFIRM / AUTO, status RUNNING / PAUSED / OFF, cash pools (`start_capital`, `min_buy_value`, `target_pct`, `stop_pct`, `trail_pct`, `hold_days`, `max_occurrences`) and compounding occurrences (`_occurrences()`). But:
  - `setup_pools()` gives every session the **same** capital. A per-session amount is only possible one session at a time from the CLI or API, and there is no UI for it.
  - `min_buy_value` is a fixed floor. It never grows with the pool.
  - P&L per trade is stored **net of charges only**. Gross P&L, an itemised charge breakdown and tax are not shown. `wealth/rebalance.py` states that capital-gains tax is "outside ATIP's scope".
- Reports: `enterprise/reports.py` renders HTML/CSV/JSON on daily/weekly/monthly schedules, but it has no session report, no financial-year (Apr–Mar) report, no tax statement and no PDF.

---

## 2. Design-thinking phase (do this first, show the outputs, then build)

**Empathise.** Write two personas and their jobs-to-be-done:
- **Owner-trader:** pre-market at 08:30 IST, intraday monitoring, post-market review, weekend planning, FY-end tax.
- **Read-only viewer:** the `viewer` role in `ops/role.py`.

Map the day-in-the-life journey across ATIP's operating schedule (`pipeline/operating_schedule.py`: stopped 21:00–06:00 IST, resuming at 06:00).

**Define.** Write 8–12 "How might we…" statements. Must include:
- know in five seconds whether ATIP is safe, running and making money;
- see what each session really earned after charges and tax;
- set each session's money without touching the CLI.

**Ideate.** Produce three IA / navigation options (for example a left-rail workspace, a hub-and-spoke command center, and a task-based top nav). Score them on findability, number of clicks to the top 10 tasks, and mobile fit. Pick one and justify it.

**Prototype.** Make low-fidelity wireframes of every top-level screen, then high-fidelity mockups of the Home, Sessions, Session detail, Accounting and Reports screens in both themes.

**Test.** Run a heuristic review (Nielsen's 10 heuristics) and a cognitive walkthrough of the top 10 tasks. Record the issues found and fixed in a design-review appendix.

---

## 3. New information architecture (target, adjust after Ideate)

One persistent shell on every page: a left rail on desktop and a bottom tab bar on mobile. It contains a global symbol and command search (Ctrl/⌘ K), the environment badge (PAPER / SANDBOX / LIVE, always visible and colour-coded), the ATIP run-state banner (same wording as `/api/status`), the kill switch, notifications and the profile.

| # | Section | Contains (existing pages folded in) |
|---|---|---|
| 1 | **Home** | Today: market health, Nifty/VIX, Trade of the Day, total P&L across sessions, alerts, pipeline status. Customisable widget grid |
| 2 | **Markets** | Market pulse, Global & Asia, FII/DII, News & impact, Data platform |
| 3 | **Research** | ATIP Scores and all score tabs (VPI, ZPI, RRI, MRI, CRI), Screener, Signals, Signal Explorer, Stock view, Options builder, Signal history |
| 4 | **Trade** | Command Center, Trade Setup, Live Monitor, Orders, Baskets, Execution lab |
| 5 | **Sessions** | New. The 10 sessions as a portfolio of mini-funds (section 4) |
| 6 | **Accounting** | New. Ledger, charges, tax, funds (virtual vs actual), reconciliation (section 5) |
| 7 | **Reports** | New. Periodic and on-demand reports, schedules, archive (section 6) |
| 8 | **Strategy Lab** | Strategies, Strategy builder, Backtests, Quant, ML |
| 9 | **Wealth** | The existing 7 wealth tabs, restyled |
| 10 | **Settings & Admin** | Account, Brokers, Admin, Compliance, Privacy, Ops/Health, Assistant |

Keep every old URL working: redirect or render it inside the new shell. Nothing bookmarked may break.

**Every module gets the same three-part structure** so the user always knows where to look:
- **Manage**: configure, act, edit (forms and controls).
- **Accounting**: the money that module touched: ledger lines, charges, P&L gross vs net.
- **Reports**: that module's own report, exportable, and schedulable from the Reports section.

---

## 4. Sessions: management, compounding cycles and P&L (core requirement)

### 4.1 Session setup (UI and API, per session)
On the Sessions page, each of S1–S10 is a card. A **Session setup** drawer lets the user set, independently per session:
- **Name, strategy, universe, mode, status.** These already exist.
- **Starting amount** (₹). Each session has its own value; there is no common amount. Keep the "split a total across N sessions" helper (`setup_pools(total=…)`) only as a shortcut that pre-fills the cards, and the user can then edit each one.
- **Minimum starting amount per cycle** (₹), per session. This is the floor below which a new cycle will not start (today's `min_buy_value`). Add a **floor growth rule** per session:
  - `FIXED`: stays as entered (today's behaviour);
  - `RATCHET_PCT`: floor = max(base floor, X % of the session's highest cycle-start amount so far), so the floor rises as the pool compounds;
  - `STEP`: floor rises by ₹Y after every N profitable cycles.
- **Exit rules:** target %, stop %, trailing %, hold days. These already exist.
- **Max cycles** (`max_occurrences`) and **auto-pick** on or off.
- **Compounding basis:** `CASH_AFTER_CHARGES` (default, the money actually in the account) or `AFTER_TAX_RESERVE` (each cycle's estimated tax is set aside and not reinvested).

Validation copy must be plain English, for example "Minimum ₹30,000 is more than the ₹25,000 starting amount." Changing a starting amount while a trade is open stays refused, as `start_pool` does today, and the UI explains why. Every change is written to `session_events` and shown in a per-session activity timeline.

### 4.2 Compounding cycle logic
One **cycle** = one buy with the session's available cash plus the sale that closes it. This is the existing "occurrence". Rules:
- Cycle *n* starting amount = cycle *n−1* ending amount. The ending amount is cash after the sale with charges paid, minus the tax reserve if the basis is `AFTER_TAX_RESERVE`.
- Profit grows the next cycle and a loss shrinks it. The new floor is checked before each cycle starts. If the cash is under the floor, the session shows **"Waiting: below minimum (₹x of ₹y)"** instead of buying.
- Quantity is a whole number of shares (`pool_quantity`). The undeployed remainder is shown as "cash left idle this cycle".

### 4.3 P&L: show both, per cycle and in total
For **every cycle** and for the **session total** (and the all-sessions total), show these columns. Gross and actual must never be blended into one number:

| Field | Meaning |
|---|---|
| Cycle start amount | Cash the cycle began with |
| Buy value / Sell value | Executed value (price × qty) |
| **Gross P&L ("Total P&L")** | Sell value − buy value, before any cost |
| Charges, itemised | Brokerage, STT, exchange txn, SEBI fee, stamp duty, GST, DP charge (from `backtest.costs.CostModel.charges()`, or the paper fill's own charges) |
| Net P&L before tax | Gross − charges (today's `pnl`) |
| Estimated tax | See 4.4 |
| **Actual P&L ("Net after charges & tax")** | Net before tax − estimated tax |
| Cycle end amount | Start + net before tax (or + actual P&L under `AFTER_TAX_RESERVE`) |
| Return % (gross / actual) | Each on that cycle's start amount |
| Holding days, exit reason | Target / stop / trail / time exit / manual |

Session summary tiles: starting amount, current equity, total gross P&L, total charges, total tax (estimated), total actual P&L, CAGR and absolute return (gross and actual), cycles done / max, win rate, average R, max drawdown, best and worst cycle, current floor.

Charts (follow the `dataviz` skill): an equity curve per session with gross and actual lines, a waterfall from gross P&L through charges and tax to actual P&L, a cycle-start-amount staircase that shows the compounding, and a small-multiples comparison of all 10 sessions.

### 4.4 Tax (estimated; configurable; labelled "estimate, not tax advice")
- Add `atip_data/config.json` `"tax": {...}` with the rates and an effective-from date, so they can be changed without code.
- Defaults as of FY 2026-27 (verify before go-live):
  - delivery equity held ≤ 12 months → STCG 20 %;
  - held > 12 months → LTCG 12.5 % above the ₹1.25 lakh annual exemption;
  - intraday (MIS) → speculative business income at a user-set slab rate;
  - F&O → non-speculative business income at the slab rate;
  - surcharge and 4 % cess as configurable add-ons.
- Per-cycle tax is **provisional** (marked "est."). The **FY view** does the proper calculation: set-off of short-term losses against gains, the LTCG exemption applied once per FY across all sessions, and FY boundaries Apr 1 – Mar 31.
- Remove the "tax is out of scope" note for sessions only. Wealth rebalance can keep its note.

### 4.5 Data model (additive only, `CREATE IF NOT EXISTS` / `ALTER TABLE ADD COLUMN`, as `ensure_tables()` does)
- `trading_sessions`: add `floor_rule`, `floor_value`, `floor_step_every`, `compound_basis`.
- New `session_cycles`: one row per cycle. Fields: session_id, n, rule_id, symbol, start_amount, buy_value, sell_value, gross_pnl, the charge columns, net_pnl, tax_est, tax_head, actual_pnl, end_amount, floor_at_start, opened_at, closed_at, exit_reason, env (VIRTUAL/ACTUAL). Write it when a cycle closes. Backfill it from `_trades()` for history.
- New `ledger_entries` (section 5).
- Keep `ledger()` / `journal()` return shapes backward compatible. Only add new keys.

### 4.6 Worked example (put it in the tests and in the on-screen help)
S1 starts with ₹25,000. Cycle 1 buys ₹25,000 and sells at +3 % for ₹25,750.
- Gross P&L ₹750.00.
- Charges on the `nse_delivery` model ₹72.27: STT ₹50.75, exchange ₹1.51, SEBI ₹0.05, stamp ₹3.75, GST ₹0.28, DP ₹15.93.
- Net before tax ₹677.73. STCG est. 20 % = ₹135.55. Actual P&L ₹542.18.
- Cycle 2 starts with ₹25,677.73 under `CASH_AFTER_CHARGES`, or ₹25,542.18 under `AFTER_TAX_RESERVE`.

The test must assert these figures, with whole-share quantity rounding handled in a separate case.

---

## 5. Accounting module (separate from trading screens)

- **Double-entry style ledger** (`ledger_entries`): every allocation, buy, sell, charge (one line per charge type), tax reserve, transfer between sessions and manual adjustment. Each line carries session, symbol, order id, env (VIRTUAL/ACTUAL) and FY.
- Views: **General ledger**, **Session ledger**, **Charges register** (by type, by session, by month), **Tax register** (STCG/LTCG/speculative/business by FY), **Funds** (restyle `funds/report.py`: actual funds, current invested at cost and at market, cumulative deployed, allocated to pools, virtual vs actual kept strictly separate).
- **Reconciliation:** ATIP's computed charges against the broker contract note (CSV upload or Dhan API when available), and session cash against the paper/broker balance. Differences are highlighted with an "accept / investigate" action.
- **Session transfers:** move money between sessions, or back to unallocated, with a reason. Allowed only when the source session has no open trade.

---

## 6. Reporting module (periodic and on demand)

Extend `enterprise/reports.py` (keep its render/run/run_due design). New report kinds:

| Report | Content | Default schedule |
|---|---|---|
| Daily session summary | Per session: cycles opened/closed, gross, charges, tax est., actual P&L, floor, cash, open trade | Every trading day, after the post-market pipeline |
| Weekly performance | All sessions compared, equity curves, win rate, best/worst trades | Monday 08:00 IST |
| Monthly P&L statement | Gross → charges → tax → actual, by session and in total; charges register | 1st of month |
| Quarterly review | Strategy-level performance, drawdowns, rule changes from `session_events` | Quarter start |
| FY tax statement (Apr–Mar) | Realised gains by tax head, set-offs, LTCG exemption, estimated liability; CA-ready | 1 Apr, and on demand |
| Session cycle book | Every cycle of one session, the full 4.3 table | On demand |
| Custom | User picks sections, sessions, date range | User-defined |

Requirements:
- Formats: HTML (print-ready), CSV, XLSX and PDF. Add a PDF engine (WeasyPrint or a headless-browser print) and pin it in `requirements.txt`.
- Schedules are editable in the UI: daily, weekly, monthly, quarterly, FY, or custom cron, in IST.
- Delivery through the existing channels (`enterprise/notifications.py`).
- An archive lists every generated report with re-download.
- Every report carries the existing disclaimer and the env (VIRTUAL/ACTUAL) on each figure.

---

## 7. Redesign every other menu and option

Apply the design system and the Manage / Accounting / Reports pattern to every page in section 1. For each page:
- (a) write its primary job in one sentence;
- (b) put the primary action top-right and dangerous actions behind confirmation with ₹ impact shown;
- (c) give it empty, loading, error, stale-data and market-closed states;
- (d) use consistent tables (sticky header, column chooser, sort, filter, CSV export, row → detail drawer);
- (e) number formatting in Indian grouping (₹1,25,000.00), green/red with a +/− sign for colour-blind safety, IST timestamps;
- (f) keyboard shortcuts and focus order;
- (g) make it responsive down to 360 px.

Specific asks:
- **Home:** a five-second answer to "is ATIP safe, running, and making money today?"
- **Trade Setup:** keep the ₹ risk preview and add the session's floor and cash.
- **Live Monitor:** add a per-session cycle-progress bar.
- **Command Center:** session cards with gross vs actual P&L.
- **Strategy Lab:** a single run → compare → promote flow.
- **Settings:** group credentials, risk limits, tax config and notification channels.

---

## 8. Non-negotiable constraints

- **Additive only.** Do not change existing behaviour of: the kill switch (`orders/risk.py`), the PAPER-only auto-send switch, AUTO's `ack` requirement, the SEBI Algo ID guard for LIVE, exit legs never being held by `gate()`, or EOD signal computation. Do not modify `pipeline/scheduler.py` beyond adding report jobs through its documented hook.
- Keep all existing API routes and response keys. Add new routes under `/api/v2/...` where shapes change.
- One shared CSS/JS asset bundle served from `/static/` replaces the inline per-page styles. Keep server rendering; do not add a heavy SPA framework unless the Ideate stage justifies it.
- Every write action still needs the dashboard token, and stays refused with HTTP 423 while ATIP is stopped.

---

## 9. Mandatory completion steps

1. **Update the mobile apps (Android and Apple iOS).**
   - Change the native tabs to Home, Trade, Sessions, Reports and More, pointing at the new mobile-optimised routes.
   - Restyle the status banner to the design system and add the environment badge.
   - Add a Sessions tab that shows per-session gross vs actual P&L.
   - Bump `versionCode`/`versionName` (Android `app/build.gradle.kts`) and the iOS version in `mobile/ios/project.yml`.
   - Build both (`./gradlew assembleDebug`; `xcodegen` + `xcodebuild` for a simulator) and record the results.
2. **Update the product tracker** (`ATIP_MASTER_TRACKER.xlsx`, sheets `ATIP_MASTER_TRACKER` and the dated Status sheet).
   - Add new IDs for every feature in this prompt: UXD-xx design system and IA, SES-xx session setup/cycles/P&L, ACC-xx accounting, RPT-xx reporting, MOB-xx mobile.
   - Fill Status (DONE / IN PROGRESS / GAP / BLOCKED), evidence, key files and next action.
   - Update the UXR-01…UXR-10 rows this work touches.
3. **Complete testing on both Test and Prod servers and generate a testing report.**
   - Unit tests for cycle maths (the 4.6 example, losses, floor rules, both compounding bases, FY set-off) and report rendering.
   - UI smoke tests extending `tests/test_w39_ui_smoke.py`, plus Playwright screenshots of every top-level page in both themes at 1440 px and 390 px.
   - An accessibility audit and regression of the full existing `pytest` suite.
   - Deploy to **Test** (test.bkesari.com) and run everything there, then deploy to **Prod** (bkesari.com) and re-run smoke and reconciliation checks.
   - Produce `docs/testing/ATIP_UIUX_Test_Report_<date>.md` (also as XLSX): test counts pass/fail per server, defects found and fixed, screenshots, accessibility results, open items.

---

## 10. Deliverables and acceptance criteria

Deliverables:
- Design-thinking pack: personas, journey map, HMW, IA options with scoring, wireframes, mockups, heuristic review.
- Design system spec and `design:design-handoff` spec.
- Code as delta patches in **one zip**, plus a `PATCH_NOTES.md` entry and migration notes.
- Updated mobile apps, updated tracker, test report for Test and Prod.

Done means:
- Every one of the 31 pages renders inside the new shell with no broken old URL.
- Each session can be given its own starting amount and floor rule from the UI.
- Every cycle and every total shows gross P&L, itemised charges, estimated tax and actual P&L, and the 4.6 figures match to the paisa.
- Daily, weekly, monthly, quarterly and FY reports generate on schedule in HTML, CSV, XLSX and PDF.
- The full test suite passes on Test and Prod.
- The tracker and test report are updated.

Work through all phases without stopping for minor decisions. Record each assumption in the patch notes. Stop only for something irreversible on Prod or something that would weaken a trading-safety guard.
