**Date of prompt:** 3 Nov 2026 Time 08:13 AM > (testing 09:38–11:43 IST; every time in this report is IST)

# ATIP UX audit fixes: test report, 3 Nov 2026

Scope: findings H1–H7, M1–M7, L1–L3 of the 3 Nov 2026 UX audit, plus the 07:03 IST design-pack prompt's innovations, on branch
`claude/ux-audit-fix-2026-11-03`. PAPER throughout. Nothing was sent to Dhan; the live `atip_data/atip.db` was only ever read
(SQLite backup API, read-only) to make the Prod snapshot.

## 1. Where it ran

| "Server" | What it really was | Database | When |
|---|---|---|---|
| **Test** | Local instance from a scratch folder, `ATIP_ENV=test`, `ATIP_OPERATING_SCHEDULE=off`, port 8790 | fresh `main.py --init` database (empty) | 09:43–09:50 IST |
| **Prod** | Local instance, `ATIP_ENV=staging`, port 8791 (no hosted Prod server exists) | a fresh snapshot of the live `atip.db` taken 09:28 IST | 09:38–11:40 IST |
| **Before** | The untouched code (08:18 IST working tree) on port 8789 | a copy of the same live snapshot (08:24 IST) | 08:47–08:58 IST |

Config and secrets are cwd-relative, so neither instance could see the Mac's credentials. The live Mac itself (port 8000) was only
read: at 09:31 IST every page answered 200, PAPER, running, not halted, feed "Live · last tick 86 s ago" (see §7).

Tools: Node Playwright 1.62.1 with the cached Chromium 1248 headless shell, Firefox 157 (Playwright build 1555), WebKit 26.5
(Playwright 2251) and the installed Google Chrome; axe-core **4.13.0** (4.10 is not on this Mac; 4.13 runs the same WCAG rules
plus newer ones), tags wcag2a, wcag2aa, wcag21aa, wcag22aa. Edge is not installed, so Edge is covered by its Chromium engine only; WebKit could not run (§8).
Scripts: `.ux_build/auditfix-2026-11-03/ui/` (ux_check.js, price_moved.js, app_check.js, metrics.js).

## 2. Results at a glance

| Check | Test | Prod | Notes |
|---|---|---|---|
| Full pytest suite | **2,380 passed, 62 skipped, 9 failed, 0 errors** on the committed code (`f3718ec`, 11:30–11:38 IST); earlier 09:59–10:07 run: 2,374 / 62 / 9 + 1 teardown error | (same code) | 9 failures identical on the untouched code (6 `test_asia_markets`: stored Asia data 8 days old, no network; 3 `test_w39_perf_budget`: the sandbox refuses local sockets). The teardown error (`test_w39_mcp` "a tool created a table") is the same race as the 10th: the dashboard's order-monitor thread, started by an earlier test's app, keeps running into later tests' temporary databases and, in market hours, creates the session tables there. Proof: the untouched code run at 10:08–10:16 IST (market open) fails 11 and errors 2, including all of the above; the 08:22 IST pre-market run had only the 9. A real gap it exposed (the data inventory missed 10 tables) is fixed in `enterprise/privacy.py`. |
| New tests `tests/test_ux_audit_fix_2026_11_03*.py` | 49 + 61 + 125 passed, browser test skipped (no Python Playwright) | — | its Node twin passes (below) |
| axe, 20 audited pages × light/dark × 1440/390 (80 runs each) | **0 violations** | **0 violations** | before: 1,637 violation nodes |
| JS errors / 5xx on those 100 page loads (+ 320 px) | 0 | 0 | before: 0 |
| Sideways scroll at 390 and 320 px | 0 pages | 0 pages | before: 6 pages |
| `nav[aria-label="Main"]` per page | exactly 1 on every shell page | exactly 1 | |
| Price-moved dialog (mocked 409), 1440 and 390 | PASS | PASS | Cancel focused first and sends nothing; "Place at ₹102.10" re-confirms once |
| App mode (`ATIPApp/…`, 375 px, light + dark, 10 pages) | 20/20 | 20/20 | rail/tab bar hidden, Kill switch present, feed text = native banner's |
| Cross-browser (light/dark, 1440/390/320, axe) | Chrome: 20 pages clean | **Google Chrome 156, all 20 pages × light/dark × 1440/390/320 on Test and Prod (12:42–12:54 IST): 0 axe, 0 errors, no sideways scroll**; Firefox 157 on 8 key pages: the same; Safari replaced by Chrome at the owner's choice | Firefox 157, WebKit 26.5 (Safari engine), Google Chrome |

## 3. Accessibility before vs after (axe nodes, light + dark × 1440 + 390)

| Page | Before (axe nodes, 4 runs) | Sideways scroll before | After Prod | After Test |
|---|---|---|---|---|
| `/` | aria-hidden-focus 4, color-contrast 1296, select-name 4 | 320, 390 | 0 | 0 |
| `/home` | aria-hidden-focus 4 | — | 0 | 0 |
| `/sessions` | aria-hidden-focus 8 | — | 0 | 0 |
| `/command` | aria-hidden-focus 4 | 320 | 0 | 0 |
| `/command/setup` | aria-hidden-focus 4 | — | 0 | 0 |
| `/trading` | aria-hidden-focus 4, color-contrast 73, scrollable-region-focusable 18, select-name 8, target-size 2 | 320, 390 | 0 | 0 |
| `/options-builder` | aria-hidden-focus 4, label 20, select-name 4, target-size 2 | — | 0 | 0 |
| `/screener` | aria-hidden-focus 4, select-name 16, target-size 2 | — | 0 | 0 |
| `/signals` | aria-hidden-focus 4, select-name 12, target-size 2 | — | 0 | 0 |
| `/market` | aria-hidden-focus 4, color-contrast 1, scrollable-region-focusable 6, select-name 12 | 320, 390 | 0 | 0 |
| `/research` | aria-hidden-focus 4, target-size 2 | — | 0 | 0 |
| `/wealth` | aria-hidden-focus 4 | — | 0 | 0 |
| `/accounting` | aria-hidden-focus 4 | — | 0 | 0 |
| `/reports` | aria-hidden-focus 4 | — | 0 | 0 |
| `/quant` | aria-hidden-focus 4, color-contrast 1, scrollable-region-focusable 10, select-name 4 | 320, 390 | 0 | 0 |
| `/strategy-builder` | aria-hidden-focus 4, select-name 48 | — | 0 | 0 |
| `/m` | aria-hidden-focus 4 | — | 0 | 0 |
| `/login` | 0 | — | 0 | 0 |
| `/global-asia` | aria-hidden-focus 4, color-contrast 4, scrollable-region-focusable 4 | — | 0 | 0 |
| `/market-pulse` | aria-hidden-focus 4, scrollable-region-focusable 4, target-size 2 | 320 | 0 | 0 |
| **Total** | **aria-hidden-focus 80, color-contrast 1375, label 20, scrollable-region-focusable 42, select-name 108, target-size 12** (= 1637) | 6 pages | **0** | **0** |

The audit's own baseline (3 Nov, axe 4.10 on the 2 Nov 19:15 backup) counted 401 contrast failures, 27 unlabelled selects,
20 aria-hidden-focus, 5 unlabelled inputs and 3 non-focusable scroll regions; this run counts every node in 4 runs per page
with axe 4.13, so the absolute numbers differ, and both go to 0. The audit's 7 sideways-scrolling pages at 390 px (`/`,
`/account`, `/data-platform`, `/market`, `/ml`, `/quant`, `/trading`) were checked by the helper run on its own snapshot copy:
none scroll sideways at 390 or 320 px (screenshots `after_<page>_<theme>_<width>.png`).

## 4. Per finding (what was tested)

| ID | Test evidence | Result |
|---|---|---|
| H1 | `test_feed_label_states` (live, stale > 120 s, closed 540 min, none ×2); `test_every_page_shows_the_same_feed_text` (Today, shell state, Command Center API, `/m` identical: "Market closed · last tick 09:00 IST"); `test_live_quotes_say_when_they_were_fetched`; app check: every `[data-ax-feed]` equals the native banner's text | PASS |
| H2 | `test_one_main_nav_and_no_back_link` on 22 pages; `?shell=0` keeps the old links; browser `navMain = 1` everywhere | PASS |
| H3 | `test_legacy_bridge`; 80 light + dark screenshots per server | PASS |
| H4 | `test_scores_page_is_lighter`, `test_ux_merge_regression` (tab ids/labels/order); first table row at **824 px** of 900 (before 1,035) | PASS |
| H5 | `test_score_headers_are_explained`; Screener legend in the options tests | PASS |
| H6 | axe 0 color-contrast on all 20 pages (incl. the 9 listed); `test_scores_css_has_no_text_under_12px` + legacy size tests | PASS |
| H7 | `test_no_native_dialogs_in_order_flows` (grep empty in `server.py`, `command_*.py`); price-moved dialog in a real browser on Test and Prod | PASS |
| M1 | 320 + 390 px scroll check on 20 pages, both servers | PASS |
| M2 | options tests 61/61; NIFTY iron condor (30 Oct bhavcopy, expiry 9 Nov): payoff, Greeks, max loss −₹1,573.00, max profit ₹4,927.00, breakevens 22,874 / 23,226, tier Intermediate, without pressing Analyse; no order button | PASS |
| M3 | no `W\d+` in any `<title>` / `<h1>`–`<h3>` (legacy + options tests); risk-limit table plain names | PASS |
| M4 | `test_one_money_format` (₹9,945.50; ₹12,31,900.00), `test_no_double_rupee`, arrows on changes | PASS |
| M5 | `test_order_ticket_layout_and_copy`, `test_broker_capability_route_reads_the_sdk_only`; screenshots of the ticket at 1440 and 390 | PASS |
| M6 | axe 0 select-name / label / aria-hidden-focus / scrollable-region-focusable on 20 pages; `test_drawer_is_inert_and_controls_are_named` | PASS |
| M7 | `test_command_center_states`; legacy loading/empty/error tests; empty Test DB shows the empty sentences | PASS |
| L1 | `test_title_matches_breadcrumb_on_every_page` (every rail route) | PASS |
| L2 | `test_shell_icons_are_svg` | PASS |
| L3 | `test_bell_previews_five_alerts_in_ist`, `test_countdown_is_one_line`; bell popover screenshot (dark) | PASS |
| C1–C4 | `tests/test_tod_buy_rules_2026_11_03.py` 12/12 | still PASS |
| Owner decision: Trade of the Day only on BUY | `test_no_trade_of_the_day_without_a_buy` (HOLD, WAIT, SELL: Scores card, ⭐, Today, `/m`), `test_a_buy_pick_is_still_shown`, `test_telegram_says_no_buy_signal`; live Mac `/` shows "No BUY signal for today" (11:27 IST) | PASS |
| Feed label on the live Mac | `test_feed_is_live_when_any_source_is_fresh`; live `/api/v2/shell/state` 11:29:51 IST: "Live · last tick 12 s ago" | PASS |

## 5. Performance and layout stability (Prod snapshot, ATIP Scores `/`, 1440 × 900, warm server)

| Metric | Before | After |
|---|---|---|
| Page load (load event) | 1,029–1,855 ms | 303–476 ms |
| First table row from the top | 1,035 px (below the fold) | 824 px |
| Visible tabs / KPI tiles | 14 / 9 | 2 (of a group) / 5 |
| Layout shift (CLS, 6 s after load) | 0.014 | 0.001 |
| One live-price update, 583 CMP cells | 5.1–5.7 ms | 5.8–6.2 ms, CLS from the update 0.001 |
| CMP cells saying "price at HH:MM:SS IST" | 0 | 583 |
| Today / Command Center load | 190 / 94 ms | 104 / 74 ms |

Targets from the 07:03 prompt: load under 2 s (met), update under 100 ms (met), no layout shift from live prices (met).

## 6. Persona tasks (owner-trader, before vs after)

| Task (audit) | Before | After |
|---|---|---|
| Trust the price before acting | Today "feed live", Command Center "540 min ago" | one label everywhere; each price says when it was fetched |
| Find a screen fast | rail + 17-link bar + page sub-nav + "← Dashboard" | rail + breadcrumb only; `<title>` matches |
| 08:30 pre-market scan | first row below the fold, 14 tabs, 4 banners | first row visible, 4 groups, one status line |
| Compare stocks on scores | 8 unexplained acronyms | hover/keyboard definitions and a legend with the real bands |
| Decide on a re-priced order | browser `confirm()` with a sentence | dialog with trigger, price now, ₹ and % move, safe default |
| Set bracket exits correctly | plain checkboxes, CLI text | switches with explanations, warning when confirmation is off, ₹ at risk, charges |
| Read P&L correctly | "₹ ₹0.00", ₹9945.5 | ₹0.00 with ▲/▼, ₹9,945.50 |
| Act from a phone | 6 pages scroll sideways | none |
| Build an iron condor safely | blank until Analyse | preset → payoff, Greeks, max loss, tier as you edit |
| Keyboard / screen reader | 1,637 axe nodes | 0 |

Nielsen re-score (team estimate, baseline 2.1): 1 status 4, 2 real world 4, 3 control 4, 4 consistency 4, 5 error prevention 4,
6 recognition 4, 7 efficiency 4, 8 minimalist 3.5, 9 recover from errors 3.5, 10 help 3.5 → **≈ 3.85**, none below 3.

## 7. Defects found and fixed during testing

| # | Found by | Defect | Fix |
|---|---|---|---|
| 1 | browser | Shell dialogs sat in the top-left corner on the older pages (their `*{margin:0}` reset; the kill-switch dialog too) | `margin:auto` on `dialog.ax-dialog` |
| 2 | browser | Top bar crowded at 1440 px: search label wrapped, icon buttons shrank | short "Search" label, `flex:none`, short feed text below 1366 px |
| 3 | browser | Bell showed on phones and pushed the theme button to a second row | hidden below 768 px as before |
| 4 | metrics | Status strip and tab groups collapsed after first paint (CLS 0.072) | strip lays out as it is parsed; non-first-group tabs hidden by CSS until the script is ready (CLS 0.001) |
| 5 | browser | Command Center's auto-send choice overflowed at 320 px | `auto-fit` columns |
| 6 | axe | Inline legacy colours (`style="color:#64748b"`) failed contrast in light | inline-colour bridge in `atip.css` |
| 7 | Test server (empty DB) | Scores and Top-25 tables showed nothing when empty | empty-state sentences |
| 8 | price-moved test | A sell showed "buy back" charges | a sell counts only its own charges |
| 9 | suite | Live-tab test broke on an inline `data-hide` | grouping via CSS, markup unchanged |
| 10 | helper (legacy) | "esc is not defined" on Account / Admin / Admin console | shared script loads before the page script |
| 11 | suite (market hours) | The privacy data inventory did not classify the 10 tables added on 2 Nov; `inventory_gaps()` listed all 10 on the live DB | classified in `enterprise/privacy.py` |
| 12 | live Mac after the restart | The feed label said "stale" with a working feed (it read only the 15-minute quote snapshots) | newest of snapshots, WebSocket ticks and the 15 s fetch (`f3718ec`) |

## 7b. Deployment to the live Mac (port 8000), 3 Nov 2026

| Time (IST) | Step | Result |
|---|---|---|
| 11:22 | Committed as `7cd1dec` (by the redesign session at the owner's request) | tree clean |
| 11:26 | `.venv/bin/python -m ops backup --kind pre-release` | `atip_data/backups/atip-pre-release-20261103-112652.db` |
| 11:27:06 | `launchctl kickstart -k gui/$UID/com.atip.platform` (owner: "restart now") | pid 28651; 17 pages 200; PAPER, running, not halted, 10 sessions running; "No BUY signal for today" on `/` |
| 11:27 | Feed label said "stale" with a working feed: quote snapshots land only every 15 min | defect #12, fixed and committed as `f3718ec` |
| 11:29:48 | Second kickstart for the fix | pid 29396; pages 200; feed "Live · last tick 12 s ago"; `X-ATIP-Quotes-At` on live quotes |

The index WebSocket was ticking (14 of 14) after both restarts; the stock, tick, depth and option-chain feeds stay OFF in config.

## 8. Open issues

- **Not merged into master, not pushed** (`7cd1dec`, `f3718ec` on the branch); the owner decides the merge.
- **Mobile builds BLOCKED:** no JDK, Android SDK or Gradle (no `gradlew`); only Xcode Command Line Tools (no Xcode), no `xcodegen`.
  The Swift change passes `swiftc -parse`; app mode was tested in the browser with the app's user agent.
- **Live market data not exercised:** WebSocket ticks and 15 s CMP updates were simulated (mocked quotes, a stale snapshot tick);
  check them on the Mac during market hours after the restart.
- Test hygiene (pre-existing): the dashboard's order-monitor thread leaks between tests and, in market hours, writes session
  tables into later tests' databases (`test_w39_mcp` teardown, a paper-book OCO test). Fix suggestion: let tests switch the
  monitor off (an env flag read at startup) or stop starting the app's startup hooks in tests.
- **Safari / WebKit not tested, by the owner's choice:** at 12:40 IST the owner chose Google Chrome instead (run in full,
  above). For the record, Playwright's WebKit (build 2251, the right one for macOS 14 on Apple silicon, not
  quarantined) launches but never opens a page, also when run from the owner's own Terminal (12:0x IST check). To test real
  Safari instead: Safari › Settings › Advanced › "Show features for web developers"; Develop › "Allow Remote Automation";
  then `safaridriver --enable` in Terminal (asks for the Mac password). Edge is not installed (Chrome covers its engine).
- Clickable `<tr>`/`<div>` rows on Strategies, Backtests and Assistant are still mouse-only (no audit rule flagged them).
- Some API status text still names a wave ("refuses every call in W4"); not in any heading.
- `/api/tod` (data endpoint, contract-tested) still returns the engine's pick as stored, BUY or not; only the screens and messages follow the owner's rule.

## 9. Screenshots

- Before: `design/audit_fix_2026-11-03/before/before_<page>_<theme>_<width>.png` (80).
- After, Prod: `design/audit_fix_2026-11-03/after_prod/prod_*.png` (80); Test: `after_test/test_*.png` (80); Google Chrome on Prod: `chrome/chrome_prod_*.png` (80).
- App mode 375 px: `design/audit_fix_2026-11-03/app/` (20). Price-moved dialog: `after_price_moved_dialog_1440.png`, `_390.png`.
- Helper close-ups: `design/audit_fix_2026-11-03/after_<page>_<theme>_<width>.png` (33).
- Raw results: `before/before_results.json`, `after_prod/prod_results.json`, `after_test/test_results.json`, `app/*_app_results.json`.
