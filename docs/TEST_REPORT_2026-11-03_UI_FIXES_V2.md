# Test report — UI fixes v2 and the trading workspace (3 Nov 2026)

| | |
|---|---|
| Brief | `ATIP_UI_Fix_Prompt_v2.md` (3 Nov 2026 13:58 IST): H1–H10, G1–G8, W1–W6, mobile, tracker, testing |
| Code | branch `claude/eloquent-hopper-1oou1b` on `origin/master` `234274d`; commits and PR in `docs/ATIP_MASTER_TRACKER.csv` (phase 16) |
| Where tested | Claude Code cloud container (Linux, Python 3.13, FastAPI 0.162, Chromium 1243 via Playwright), on a throwaway seeded database (`python -m tools.ui_review serve`: the synthetic market plus the 3 Nov findings). **Not** the Mac, test.bkesari.com or bkesari.com (see §5). |
| When | 3 Nov 2026, 19:20–21:10 IST |
| Verdict | **PASS locally** for every functional, visual, accessibility and phone / tablet case. **BLOCKED** on the Test and Prod servers and the PAPER fill on the Mac (no access from this session). |

## 1. Regression suites

| Suite | Result |
|---|---|
| Full suite, master before the change (baseline, 3 Nov 19:25 IST) | Failures in `test_asia_markets` (fixture data dated 26 Oct, now "stale"), `test_w38_platform_compliance::test_inventory…` (order-dependent; passes alone) and `test_w39_mcp` (stdio session) |
| **Full suite, final (20:43–20:51 IST, 4 workers)** | **2,258 tests: 2,216 passed, 36 skipped, 6 failed.** The 6 are all `test_asia_markets` and fail identically on untouched master: their fixtures are now more than 8 days old. Skips: no live MySQL / PostgreSQL, no `psycopg`, 3 browser tests that look for Chromium by a fixed path. |
| Shell + UI files re-run after the last edits (21:00 IST) | `test_ux_shell`, `test_ui_fixes_v2`, `test_ui_fixes_v2_browser`, `test_w39_ui_smoke`, `test_ux_console_browser`, `test_ux_command_browser`, `test_viewer_role`, `test_ux_merge_regression`, `test_ui_options_chain`, `test_regressions`: **266 passed** |
| Flaky under 4 workers only | `test_ux_console::test_switch_on_sends_auto_session_entries_without_asking` failed once under `-n 4`. It passes alone and the file passes serially (28 / 28). No code under `orders/` changed. |
| Previously skipped browser tests now running | `tests/test_w39_ui_smoke.py` had a hard-coded Chromium path, so it never ran here. It now finds this machine's browser and **passes**, after two product defects it exposed were fixed (§4: D1, D2). |

New tests (all pass):

| File | Tests | What they prove |
|---|---|---|
| `tests/test_ui_fixes_v2.py` | 49 | formatter (Indian grouping, sign, compact, tick bands, scores, inverse tone, missing values); JS ↔ Python formatter identity (node runs `atip.js`); alert cleaning and grouping; plain job names; Home vs Accounting vs ledger for Today / This month / Since start; card pattern; idle sessions; VIX amber; GIFT missing; breadcrumb / overflow / frames; banner rule; no raw floats / None on `/`; plain English; freshness; watchlist API; quotes; the ticket's payload through the unchanged `POST /api/orders` (token, `require_confirmation`); depth OFF and ON (book read, not changed); stock page + local chart library; P&L strip; new tokens' contrast; no raw colours in `workspace.css`; **empty `git diff` of `orders/` and `execution/`** |
| `tests/test_ui_fixes_v2_browser.py` | 3 | DOM scan of every route at 1440 and 360 px; phone bottom-sheet ticket with B / Esc and no page scroll; watchlist with W, remembered; layout shift < 0.1 on the stock page while quotes, depth and the strip refresh |
| `tests/test_ui_options_chain.py` | 13 | W6 chain: ATM-centred rows, Greek signs, PCR, max pain, nulls not zeros, 400s, the no-data message, JS syntax |
| `tests/test_tech_signals_catchup.py` | 3 | G8 job: the missing scored session is caught up at the 06:00 resume, today is left alone, nothing missing → NO_NEW |

## 2. Functional cases (local)

| # | Case | Result | Evidence |
|---|---|---|---|
| F1 | H1 money labelled by period; Today = realised today + open; toggle Today / Since start | PASS | `test_home_labels_every_figure…`; screenshot `after/home__desktop__light.jpg` (Flat ₹0.00 today, Since start −₹589.74) |
| F2 | H1 reconciles: Home = Accounting "P&L by period" = general ledger | PASS | same test; `/accounting?tab=pnl` shows "✓ Home and the general ledger agree" |
| F3 | H2 three cards in one row at ≥ 900 px, stacked at 768 / 375 | PASS | screenshots desktop / tablet / phone |
| F4 | H3 one card pattern (icon, word, border, arrow) | PASS | `test_home_cards_share_one_pattern…` |
| F5 | H4 alerts: `slot's` not `slot&#x27;s`; 2 lines + Show more; no repeated title; chips; "Intraday signals · 4" | PASS | `test_home_alerts_are_clean…`, DOM scan (no escaped entities) |
| F6 | H5 "S2–S10 · no trades yet (9)", expandable; trend or dash | PASS | `test_home_idle_sessions_fold…` |
| F7 | H6 "Broker connection" once; status words; IST; raw name in tooltip | PASS | `test_home_pipeline_score_vix_gift` |
| F8 | H7 market health 61 on Home and `/` | PASS | same test; `/` KPI uses `:.0f` |
| F9 | H8 breadcrumb whole at 1024 / 1280 / 1440; page part hides under 600 px | PASS | screenshots; top-bar measurements below |
| F10 | H9 India VIX +3.48% amber ▲ | PASS | test + screenshot |
| F11 | H10 no green banner when running normally; banner when halted / stopped / LIVE | PASS | test (rule present, `halted` in `/api/status`); visual: no banner in the after shots |
| F12 | G1 GIFT missing → grey "—" with reason; no red 0.00% anywhere | PASS | DOM scan `red_zero` 0 on 54 page × width pairs |
| F13 | G2 no second navigation bar; See also row | PASS | DOM scan `dup_nav` 0 |
| F14 | G3 no `python -m` / `.py` / `config.json` in user text | PASS | DOM scan `dev_text` 0 |
| F15 | G4 How this works + term tooltips (Signals, Market gate) | PASS | test + screenshot |
| F16 | G5 no clickable element without a role / keyboard | PASS | DOM scan `clickable` 0 |
| F17 | G6 52W high / low and returns follow the live price; labels collision-aware | PASS | `test_live_stats…`; JS mirrors it |
| F18 | G7 top bar one row at 1024, 1100, 1280 (watchlist open) and 1440 px | PASS | measured 57 px high at each width |
| F19 | G8 stale chip "Technical signals · as of 30 Oct · stale"; the catch-up job | PASS | tests; cause in `docs/UX_FIXES_V2_2026-11.md` |
| F20 | W1 watchlist: lists, add by search, remove, rename, live LTP / %, B / S / chart / depth | PASS | API tests + browser test; screenshots `watchlist__*`, rail on `command__desktop__*` |
| F21 | W2 ticket: Qty ↔ ₹, product, price type, stop / target; risk, R:R, charges, margin, budget, gap; saves through `POST /api/orders` with `require_confirmation: true`; bottom sheet at 375 px; B / S / Esc | PASS | `test_the_ticket_saves…`, `test_phone_ticket_is_a_bottom_sheet_with_keys` |
| F22 | W3 `/stock/{symbol}`: candles, volume, RSI, MACD, MA, BB, crosshair legend, ATIP markers, rule price lines, 1D–All | PASS | `test_stock_page…`; screenshots `stock_ACME__*` |
| F23 | W4 depth: 5 / 20 levels from the feed's book; OFF message | PASS | two tests |
| F24 | W5 P&L strip on Trade screens only | PASS | `test_shell_breadcrumb…` |
| F25 | W6 option chain with OI bars, IV, Greeks; payoff, max P/L, breakevens | PASS | 13 tests; screenshot `options-builder__desktop__light.jpg` |
| F26 | **PAPER order smoke (local)**: the ticket's rule is saved ACTIVE and waits for confirmation | PASS (rule level) | `test_the_ticket_saves…`. The fill itself needs the Mac's paper broker and a market session: §5 |
| F27 | Order behaviour unchanged: `git diff` of `orders/` and `execution/` empty | PASS | `test_order_paths_are_untouched` |

## 3. Visual, phone / tablet, accessibility, performance

**DOM scan** (`python -m tools.ui_review scan`, brief §5):

| | Before (3 Nov 19:30 IST) | After (21:00 IST) |
|---|---|---|
| Page × width pairs | 50 (25 routes × 1440 / 360 px) | 54 (27 routes × 1440 / 360 px) |
| Clean | 6 | **54** |

The scan checks for:
- raw floats beyond 2 dp;
- None / NaN / undefined;
- "₹—" and escaped entities;
- red 0.00%;
- developer text;
- second navigation bars;
- clickables without a role;
- horizontal scroll at 360 px;
- JS errors;
- HTTP ≥ 400.

**Screenshots:**
- 25 routes before and 27 after (162 images), each at desktop 1440, tablet 768 and phone 375, in light and dark.
- A curated set is in `docs/ux_fixes_v2/before|after/`; the full sets are in the delta zip.
- No JS errors on any page (scan `js_errors` 0).

**axe-core 4.10.2** (critical and serious only, 27 routes, 1440 px):

| | First run | Final |
|---|---|---|
| Light theme | 9 rule types, 442 nodes | **0 on 27 / 27** |
| Dark theme | — | **0 on 27 / 27** |

Fixed along the way:
- closed drawers that could still take focus (`inert`);
- `role=tab` without a `tablist`;
- unlabelled selects and inputs on older pages;
- scrollable boxes with no keyboard access;
- a chart container marked as an image;
- low-contrast greys, reds and greens on the older dark pages (`/`, the stock panel, /trading, /strategies);
- the new disclosures on dark pages.

**Contrast tokens:** `test_ux_shell::test_design_tokens…` and `test_ui_fixes_v2::test_new_tokens…` pass (every text token ≥ 4.5:1 on every surface, both themes).

**Performance** (this container, seeded database, 1440 px):

| | Result |
|---|---|
| First contentful paint | /home 196–292 ms · / 308–364 ms · /command 184–256 ms · /stock/ACME 228–252 ms · /watchlist 184–196 ms · /options-builder 224–256 ms — all under the 1.5 s target (to be confirmed on the Mac) |
| Live refresh without layout shift | Stock-page CLS 0.021 over load plus 3 s of refreshes (quotes 15 s, depth 5 s, strip 30 s); watchlist rows have a fixed height |

Page-load layout shift (CLS), master → branch:

| Page | Master | Branch |
|---|---|---|
| / | 0.014 | 0.001 |
| /home | 0 | 0 |
| /command | 0.354 | 0.32 |
| /trading | 0.254 | 0.115 |
| /market-pulse | 0.124 | 0.11 |
| /strategies | 0.186 | 0.212 |
| /options-builder | 0.001 | 0.04 |

The remaining shift on /command, /trading, /market-pulse and /strategies is those pages filling their own cards after load (it is on master too).

## 4. Defects found and fixed while testing

| # | Severity | Defect | Fix |
|---|---|---|---|
| D1 | S3 (pre-existing) | On phones the closed menu rail was only moved off-screen: keyboard users tabbed into invisible links (`test_w39_ui_smoke` failed on master) | `visibility: hidden` while closed |
| D2 | S3 (pre-existing) | Escape did not close the search palette in current Chromium (`test_search_palette…` failed on master) | Escape closes any open dialog first |
| D3 | S2 | New SQL used `change` / `signal` unquoted: MySQL (the hosted viewer) would refuse it | backticked; `test_mysql_backend` passes |
| D4 | S3 | Raw prices (`₹113.27309768426964`) and `None` in the hidden tabs of `/` (the browser scan only sees the open tab) | every price cell through `fmt_price`; a server-side test reads all tabs |
| D5 | S4 | The server's generated-JS check flagged an apostrophe in a new comment | comment reworded |
| D6 | S3 | Layout shift from folding older pages' nav bars after first paint | content stays invisible until folded (1.5 s CSS fail-safe) |
| D7 | S4 | The top bar's search label wrapped when the watchlist took width at 1280 px | container queries on the bar's own width; the rail opens by default only from 1440 px |

Open (not fixed here): page-load layout shift that older pages have on master too; the older pages stay dark in the light
theme.

## 5. Test and Prod servers, and the Mac (brief §7.3)

| Case | Test (test.bkesari.com) | Prod (bkesari.com) | Why |
|---|---|---|---|
| Functional, visual, phone / tablet, accessibility on the deployed build | BLOCKED | BLOCKED | Not deployed; deploying is the owner's step (PR → CI → staging → prod) |
| Hosted ATIP viewer | BLOCKED | BLOCKED | Both hosts answer (200 on `/`), but `/ATIP/` is behind the owner's login and serves a snapshot pushed from the Mac |
| PAPER-order smoke test (fill) | — | — | BLOCKED: needs the Mac's paper broker in a market session. The rule level passes locally (F26). |

Still to run on the servers (and on the Mac after merging):

1. On the Mac, before deploying: commit and push the Mac's uncommitted UI on its own branch, then merge this branch
   (or apply the delta with `apply_ui_fixes_v2.sh --dry-run` first; it keeps locally changed files).
2. `python -m pytest -q` on the Mac. Expect only the `test_asia_markets` stale-fixture failures.
3. Restart ATIP. Then:
   - `python -m tools.ui_review scan --base http://localhost:8000`: 0 failures expected (the live data differs from the seed);
   - axe on the same routes;
   - screenshots at 1440 / 768 / 375 in light and dark.
4. PAPER smoke, in market hours:
   1. Open `/stock/<a liquid symbol>`.
   2. Press B, then set qty 1, stop and target. Check that the risk, charges and budget lines read correctly.
   3. Save. Confirm on Command Center when the rule triggers, and see it fill in the paper book.
   4. Halt with the kill switch: check that the banner shows and the ticket refuses to save.
   5. Resume.
5. Live CMP: watch `/stock/<symbol>` and the watchlist for 2 minutes in the session. Prices should change every 15 s
   and nothing should jump.
6. Deploy to Test, then repeat steps 3 and 5 through the viewer. Then Prod.
7. Run the technical-signals catch-up for 2 Nov on the Mac:

   ```bash
   python -m research.tech_signals run --date 2026-11-02
   ```

   Then check that /signals shows the 2 Nov date and the stale chip goes away.
8. Build the apps: Android Studio (`./gradlew assembleDebug`) and Xcode (`xcodegen && xcodebuild`).
