# UI fixes v2 — every open UI issue, and the trading workspace (3 Nov 2026)

Brief: `ATIP_UI_Fix_Prompt_v2.md` (owner, 3 Nov 2026 13:58 IST). Branch `claude/eloquent-hopper-1oou1b`, from
`origin/master` `234274d` (2 Nov 2026 23:37 IST). All times IST.

**Read with:** `docs/UI_DESIGN_SYSTEM.md` (tokens, formatter rules, components), the test report
`docs/TEST_REPORT_2026-11-03_UI_FIXES_V2.md`, and the screenshots in `docs/ux_fixes_v2/` (`before/` and `after/`;
the full 25 routes × 3 widths × 2 themes set is in the delta zip).

## How this was checked

- **The Mac was not reachable.** The brief's step 0.1 (commit the Mac's working tree first) could not be done from the
  cloud session: the `ATIP` folder on the Mac (`smoru`) is in the Remote Control list, but the computer does not
  offer it to cloud sessions. The work starts from `origin/master` `234274d`. If the Mac holds UI changes that are not
  on GitHub, they must be committed and merged with this branch before it is deployed (see "Open items").
- **The issues were reproduced, not assumed.** `tools/ui_review.py` seeds a throwaway database with the 3 Nov findings:
  - S1 −₹589.74 since start and nothing today; S2–S10 with money and no trades;
  - alerts stored with `&#x27;` and with their title repeated in the body;
  - `broker_health` logged twice;
  - India VIX +3.48%; no GIFT Nifty quote; market health 60.73;
  - technical signals from 30 Oct beside scores from 2 Nov.
- **Before / after.** Screenshots were taken of each route before and after the changes, and an automated DOM scan
  checked the brief's §5 rules on every route at 1440 and 360 px. The baseline scan passed **6 of 50** pages; the
  scan now passes **54 of 54**.

## Status by ID

| ID | Status | Cause | Change |
|---|---|---|---|
| H1 | Done | "Making money today?" summed only the cycles closed today, but Sessions summed the cycles since each pool started. Two periods were presented with no labels. | New `accounting/periods.py`: Today / This month / Since start, each with realised (after charges and est. tax) + open (unrealised) = total. The top card shows Today's total with "Today: realised + open · Since start". The Sessions card has a Today / Since start toggle (Today by default, remembered). Accounting has a new **P&L by period** tab (`/accounting?tab=pnl`) that reads the same function and checks it against the general ledger (✓ agree). Tested: Home = Accounting = ledger for all three periods. |
| H2 | Done | `.ax-five` used `auto-fit minmax(240px)`, which wraps 2 + 1 at ~800–1000 px. | The three answers use `.ax-answers`: one column below 900 px, `repeat(3, minmax(0, 1fr))` from 900 px. |
| H3 | Done | The money card used a bare `₹`/`•` and an empty tone. | One pattern for all three cards: icon + word (Yes / Check / Idle / **Up / Flat / Down**) + coloured left border (green / amber / red / **grey for flat**) + ▲▼ and sign on the amount. |
| H4 | Done | Alert messages are stored as Telegram HTML (`&#x27;`, `<b>`). The page escaped them again and cut them at 160 characters, and the title repeated in the body. | `clean_alert()` decodes the text once (twice-escaped text too) and drops tags. It removes the repeated title and turns job names into words. Text is clamped to 2 lines (CSS `line-clamp`) with "Show more" only when it overflows. Alerts of one kind are grouped ("Intraday signals · 4", newest first, "3 earlier"). Symbol chips open `/stock/{symbol}`. |
| H5 | Done | Every session with money was listed, so the 9 idle ones showed ₹0.00 and an empty Trend. | Active sessions (a cycle, an open trade or any P&L) come first. Idle ones fold into one expandable row, "S2–S10 · no trades yet (9)". Trend is a sparkline when there is data; otherwise a grey "—" with a tooltip. |
| H6 | Done | The latest 6 `pipeline_log` rows were listed raw, so a job run twice appeared twice. | One row per job (its latest run), with the plain name from `dashboard/labels.py` ("Broker connection", "Global markets (intraday)"), a status word (Done / Failed / Skipped / Nothing new), the IST time, and the raw name in the tooltip. Data freshness chips sit below. |
| H7 | Done | Home printed `mh_score` raw; `/` rounded it. | `fmt_score()` (and `AX.fmt.score`): whole numbers everywhere. 60.73 → 61. |
| H8 | Done | `.ax-crumbs` was the flex item that shrank first. | The section part never shrinks; only the page part can ellipsize. That part hides when the top bar is under 560 px (a container query on the bar's own width), and the crumb hides only at 380 px and below. The crumb is a `div role=navigation`, so older pages' `nav {}` CSS cannot squash it. |
| H9 | Done | Every percentage coloured a rise green. | `fmt_pct` + `tone_class(v, inverse=True)`: India VIX rising is amber ▲, falling green ▼, with "(rising = more fear)". The same flag is in `AX.fmt.pctHtml(v, true)` and on `/`. |
| H10 | Done | The full-width status banner was always shown, beside the top-bar pill. | The banner shows only for STOPPED, RESUMING, kill switch on (new `halted` in `/api/status`), LIVE, a manual override, or a page without the shell. Normal running shows the pill alone. The native apps follow the same rule. |
| G1 | Done | `chg(None)` printed `+0.00%` in red. Prices were printed raw (`₹113.27309768426964`). Missing values printed `None` / `₹—`. | `chg()` on `/`: missing → grey "—" with the reason ("No GIFT Nifty quote in the latest index read (13:55 IST)"). Zero → grey "0.00%". Up / down get ▲▼ and a sign. Prices go through `fmt_price` (tick-rounded) in every table on `/`, including the live poller. `None` → "—". The scan now fails on any red 0.00% and checks every route. |
| G2 | Done | Older pages kept their own top link bars (and the Command Center its own side list) inside the shell. | `AX.foldLegacyNav()`: a bar of four or more links to other pages hides. Its status chips and buttons stay, and the links reappear once in a **See also** row at the bottom. |
| G3 | Done | Developer text was written into user copy, by the pages and by API messages. | Static texts are rewritten (Signals, Market gate, Settings, Command Center). `AX.foldTech()` catches the rest — API messages too — and moves file names, `python -m` commands, API paths and config keys into a closed **ⓘ Technical details** at the end of the sentence's paragraph. The Dhan-token banner's commands go into a closed "How to fix". |
| G4 | Done | The Signals intro and the Market gate card were one long paragraph. | One-line summaries plus **ⓘ How this works** disclosures. Hover / focus definitions for confluence, market gate, 75m, weekly, ATR, ATIP score and EOD (`labels.GLOSSARY`; `AX.terms()` adds them to table headers). |
| G5 | Done | Clickable `<div>`s (the tabs on `/`, Signals and Research, the stock-panel tabs, rows on /strategies) were not keyboard reachable. | `AX.keyboardClickables()` gives each one a role (`tab` inside a `tablist`, `button`, `link`), a tab stop, and Enter / Space. Containers whose own buttons do the work (a modal backdrop) get no role. The focus ring is a 3 px `--ax-focus`. The scan finds none left. |
| G6 | Done | The stock panel's stats came from stored daily bars only. Pattern labels were drawn at the line end, with no collision check. | `live_stats()` / `liveStats()`: the 52W high / low take in the LTP, and returns are measured from it. Line labels are placed collision-aware: inside the plot, clear of the axis, of each other (12 px) and of the live price's level, with a halo. |
| G7 | Done | The top bar's icons and run text wrapped at ~1024 px. The Command Center repeated the brand, the PAPER badge and "ATIP running". | The top bar is a CSS container. Under 860 px of bar the notification, theme and profile icons fold into **⋯** (a menu that also links the watchlist), and the run pill shows only its dot. Measured at 1024 / 1100 / 1280 (watchlist open) / 1440 px: one row, 57 px. Inside the shell the Command Center header drops its brand and env badge. H10 removes the duplicate banner. |
| G8 | Done | **UI:** nothing showed that a dataset was behind. **Job:** the technical-signals job has a single chance at 20:30 IST and no catch-up (cause below). | **UI:** "as of" / **stale** chips (`freshness()`, `GET /api/v2/freshness`) on Home and the stock page, e.g. "Technical signals · as of 30 Oct · stale". **Job (separate commit):** the 06:00 resume (step 4) runs technical signals for any scored session that has none (`research/tech_signals.py run_missing`). |
| W1 | Done | — | **Watchlist**: a collapsible rail beside the menu (☆ or **W**). It opens by default on the Trade screens from 1440 px, and the choice is remembered. The `/watchlist` page serves phones and the apps. Several lists (up to 10, add / rename / delete), add-by-search, live LTP and ▲▼ % every 15 s in place, the ATIP score and signal chip. Hover / focus actions: **B**, **S**, chart, depth, remove. Stored in `enterprise_watchlist` (no new table), in your order. The phone's bottom tab bar now reads Home · **Watchlist** · Trade · Sessions · More, the same as the apps; Reports moved under More. |
| W2 | Done | — | **One order ticket** for every Buy / Sell: Trade of the Day, the watchlist, the stock page, the scores table and the stock panel (`openOrderModal` now opens it; the old modal stays one click away under "Advanced"). Fields: Qty ↔ ₹ toggle, product (CNC / MIS), price type (Market / Limit), trigger, stop-loss, target. Before saving it shows **₹ at risk, reward:risk, charges, margin, the loss budget left, and the EOD-signal vs LTP gap**, plus every pre-trade check (`/api/command/preview`). It saves through **`POST /api/orders` unchanged**, always `require_confirmation: true`, and is disabled while a blocking check fails. A bottom sheet on phones. Keys: **B** buy, **S** sell, **Esc** close. |
| W3 | Done | — | **`/stock/{symbol}`** on TradingView Lightweight Charts 5.0.9 (Apache-2.0, served from `/static/vendor/`). Chart: candles / line, volume pane, RSI 14 and MACD panes, MA 20/50/200 and BB 20,2 overlays, crosshair with an OHLC legend, ATIP BUY / SELL markers, price lines for active buy rules / stops / targets. Timeframes 1D and 5D (15-minute bars), 1M–All. The live last bar and the stats move with the LTP. The classic modal stays at `/?stock=SYM`. |
| W4 | Done | — | **Market depth** beside the chart, 5 or 20 levels, every 5 s. It reads the 20-level depth feed's own book under its lock; nothing changes. With the feed OFF it shows "The 20-level depth feed is switched off. Turn it on in Settings › Live feeds (it needs the Dhan Data API)" and the last stored best bid / ask. |
| W5 | Done | — | **P&L strip** under the top bar on the Trade screens (/command\*, /trading, /stock, /execution-lab, /baskets): Today (realised + open), open positions, orders waiting for your tap (links to Command Center), loss budget used. Its box is drawn by the server, so nothing shifts. |
| W6 | Done | — | **Option chain** card on /options-builder (`dashboard/options_v2.py`, `GET /api/v2/options/chain/{symbol}`). It shows CE \| strike \| PE with OI bars, OI change, IV (stored or implied), LTP, and a Greeks toggle (Δ Γ Θ ν). ITM is shaded, ATM highlighted, and the spot line drawn. Above the chain: PCR, max pain, an "as of" chip and a stale chip. Clicking an LTP adds a leg (shift-click: the other side) to the builder's own legs. The payoff chart shows the line at expiry and today. Summary: max profit / loss ("Unlimited" when it is), breakevens, net premium, chance of profit. Margin reads "Not computed" (the analyser has no margin model). |

### Mobile apps (brief §7.1)

Android `versionCode 4` / iOS build 4, both `2026.11.03`. Changes in both apps:

- **Tabs:** Home · **Watchlist** · Trade · Sessions · More. Reports moved under More.
- **Banner:** H10's rule; normal state is a compact env pill plus "● Running".
- **Colours:** the web tokens, following light / dark.
- **Dialogs:** JavaScript alert / confirm work (the ticket uses them).
- **Keyboard:** the screen resizes for the keyboard (bottom-sheet ticket).
- **Links:** ATIP's own links (`/stock/…` too) stay in the tab; other sites open in the browser.

The Today cards, alerts, watchlist, ticket, chart and depth are the web pages, so they reach the apps without a
store release. **Not built here:** this Linux box has no Android SDK, Kotlin or Swift, and iOS needs a Mac (see
`mobile/README.md`).

### G8 — why the technical signals stopped at 30 Oct

Cause (from the code; the Mac's logs were not reachable — the checks to confirm are below):

1. **One chance a day, no catch-up.** `technical_signals` runs once, at 20:30 IST, behind the evening queue: delivery
   at 19:30, audit export, alt data, participant OI at 20:15, `fii_dii_watch` every 10 minutes (which re-runs scoring
   when NSE's data lands), and health checks. The `schedule` loop runs one job at a time, and `run_job` refuses
   anything after 21:00. If the queue is still busy at 21:00, the run is recorded **SKIPPED** ("ATIP stopped
   (scheduled stop window 21:00–06:00 IST)") and moves to the next day. Scores have a catch-up
   (`run_postmarket_if_missing` at start-up, 18:30 and resume step 4); technical signals had none.
2. A deferred run is replayed at 06:00 only if the same process is still alive. **A restart overnight loses it**, and
   two merges landed on master that night (21:39 and 23:37 IST).
3. Less likely: the run ended FAILED or EMPTY. Nothing in the technical-signal code changed after 20 Oct.

Fix (separate commit): the 06:00 resume, step 4, runs technical signals for each of the last 3 scored sessions that
has no `technical_snapshot` rows (`research/tech_signals.py missing_sessions / run_missing`). Today's session is left
to its 20:30 slot. 3 new tests are in `tests/test_tech_signals_catchup.py`.

Fix the missing day now, on the Mac:

```bash
python -m research.tech_signals run --date 2026-11-02
```

Confirm the cause on the Mac:

```sql
SELECT id, run_date, start_time, end_time, status, rows_processed, error_msg FROM pipeline_log
 WHERE job_name LIKE 'technical_signals%' ORDER BY id DESC LIMIT 10;
SELECT * FROM deferred_jobs WHERE window_date = '2026-11-02';
```

Also noted, not changed: the jobs scheduled inside 21:00–06:00 (risk model 21:45, history backfill 22:20, Nifty
history 23:20, the late health checks) never run at their slot, and replay only if the process lives until 06:00.

## Order behaviour unchanged

- `git diff origin/master -- orders/ execution/` is **empty**; `tests/test_ui_fixes_v2.py::test_order_paths_are_untouched` checks it.
- The kill switch, the confirmation gate, the PAPER/LIVE switch and the X-ATIP-Token checks are not touched.
- **What the ticket does:** it calls the same `POST /api/orders` as the old modal, with the same payload shape. It
  always asks before the entry is placed, and it needs the token (the test checks 401/403 without it).
- **What depth reads:** the depth feed's book, under its lock. It never changes it (a test checks this).
- **What else writes:** only the watchlist API (token-guarded, refused while ATIP is stopped by the existing guard).

## Files

| Area | Files |
|---|---|
| Formatter, labels, periods | `dashboard/fmt.py` (new), `dashboard/labels.py` (new), `accounting/periods.py` (new) |
| Home, Accounting, Settings | `dashboard/ux_pages.py` |
| Shell | `dashboard/shell.py`, `dashboard/static/atip/atip.css`, `dashboard/static/atip/atip.js` |
| Workspace W1–W5 | `dashboard/workspace.py` (new), `dashboard/static/atip/workspace.css` / `.js` (new), `dashboard/ux_routes.py` (registration) |
| W6 | `dashboard/options_v2.py`, `dashboard/static/atip/options.css` / `.js` (new), `dashboard/w39_routes.py` / `w39_page.py` (wiring) |
| Older pages | `dashboard/server.py` (`/`: G1, prices, None, greys), `dashboard/global_asia.py` (H10 banner), `dashboard/w39_page.py` (G4), `dashboard/stock_view.py` (G6), `dashboard/execution_page.py`, `dashboard/ml_page.py`, `dashboard/command_more.py` (−0.00%) |
| Chart library | `dashboard/static/vendor/lightweight-charts-5.0.9/` (+ LICENSE) |
| G8 job | `research/tech_signals.py`, `pipeline/operating_schedule.py`, `tests/test_tech_signals_catchup.py` |
| Review harness, tests | `tools/ui_review.py` (new), `tests/test_ui_fixes_v2.py`, `tests/test_ui_fixes_v2_browser.py`, `tests/test_ui_options_chain.py` (new), `tests/test_w39_ui_smoke.py` (finds this machine's Chromium) |
| Apps | `mobile/android`, `mobile/ios`, `mobile/README.md` |

## Open items

1. **The Mac's uncommitted UI** (brief §0.1). Commit it on its own branch and push it, then merge or rebase this
   branch onto it. If both changed the same file (`shell.py`, `atip.css`), resolve by hand.
2. **Deploy to Test and Prod** and the PAPER smoke test on the Mac (brief §6.1, §7.3). This session can reach neither
   server nor the Mac. The run list is in the test report (§ "Still to run on the servers").
3. **Mobile builds** need Android Studio / Gradle and Xcode on the Mac.
4. **Older pages are dark in the light theme.** They keep their own colours inside the shell. Their accessibility
   issues were reduced, but not to zero: see the test report's axe section.
