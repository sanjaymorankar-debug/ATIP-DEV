# ATIP UI/UX redesign, session accounting and reports: test report (2 Nov 2026)

The spreadsheet version, with every check and every screenshot listed, is `ATIP_UIUX_Test_Report_2026-11-02.xlsx`. The evidence (scripts, logs, JSON) is in `deliverables/2026-11-02/uiux/test_evidence/`.

## 1. What each "server" was

The brief asks for runs on test.bkesari.com and bkesari.com. **No hosted ATIP exists** (tracker UXR-11), so nothing was deployed there. As in the 26 and 31 Oct reports, both "servers" were local instances of this code.

| Server | What it really was | Port |
|---|---|---|
| **Test** | A scratch folder with its own database: the W18 synthetic market (`tools/load_test.seed`), plus ten paper sessions that really traded through the order engine (`orders.rules` → `orders.broker` → `orders.paper`, nse_delivery charges). It holds 12 closed cycles, wins, stop-outs, two open trades, a session waiting below a ratcheting floor, and one session on AFTER_TAX_RESERVE (`seed_test_server.py`). `ATIP_ENV=test`, with the operating schedule off so writes could be exercised at 22:00 IST. | 8771 |
| **Prod** | A **read-only SQLite backup of the live `atip_data/atip.db`** taken at 22:15 IST: 10 pools of ₹25,000, 13 order rules, 9 paper fills, S1's PNBHOUSING cycle. `ATIP_ENV=staging`, operating schedule on. ATIP was STOPPED (after 21:00), so every write was refused with 423, as it should be. | 8772 |

The live Mac was not restarted, and nothing was written to the live database. No credentials reached either instance.

## 2. Results

| Run | Checks | Passed | Failed |
|---|---|---|---|
| New tests: `test_session_cycles.py` (20), `test_session_reports.py` (16), `test_ux_shell.py` (76) | 112 | 112 | 0 |
| Full regression suite, `pytest` over all tests (sandboxed) | 2,190 (60 skipped) | 2,122 | 8: 5 × `test_asia_markets`, **pre-existing** (they fail identically on the untouched code: fixtures dated 26 Oct now read as stale); 3 × `test_w39_perf_budget`, the sandbox refusing local sockets (14 / 14 pass outside it) |
| HTTP smoke, **Test** (`smoke_servers.py`: 33 old URLs in the shell, 23 new pages, 15 APIs, books balance, token refusal, setup validation + save, transfer, weekly report in 4 formats + downloads, cash reconciliation) | 82 | 82 | 0 |
| HTTP smoke, **Prod** snapshot (the same reads; the books balance; a write refused with 423 while ATIP is stopped; the env badge reads PAPER) | 75 | 75 | 0 |
| Screenshots, **Test**: 12 top-level pages × light/dark × 1440/390 px (DevTools-driven headless Chrome, full page) | 48 | 48 | 0 JS errors, 0 5xx, 0 horizontal overflow |
| Screenshots, **Prod** snapshot: the same set | 48 | 48 | the same |

**The §4.6 worked example matches to the paisa** (`test_worked_example_to_the_paisa`):

| Figure | Value |
|---|---|
| Gross P&L | ₹750.00 |
| Charges | ₹72.27: STT 50.75, exchange 1.51, SEBI 0.05, stamp 3.75, GST 0.28, DP 15.93 |
| Net before tax | ₹677.73 |
| STCG est. | ₹135.55 |
| Actual P&L | ₹542.18 |
| Next cycle starts with | ₹25,677.73 (CASH_AFTER_CHARGES) or ₹25,542.18 (AFTER_TAX_RESERVE) |

The whole-share case is tested separately. ₹25,000 at ₹1,091.95 buys 22 shares (₹24,022.90) and pays ₹28.50 in buy charges, leaving ₹948.60 idle. A sale at +3 % gives gross ₹720.72, charges ₹70.07, net ₹650.65, tax est. ₹130.13 and actual ₹520.52.

**Real data (Prod snapshot).** The backfill turned S1's PNBHOUSING trade into cycle 1:

- 22 shares bought for ₹24,022.90 and stopped out for ₹23,501.94.
- Gross −₹520.96, charges ₹68.78 (₹0.01 of fill-vs-model rounding shown as "Other"), net and actual −₹589.74 (no tax on a loss).
- Ending amount ₹24,410.26, equal to the engine's cash to the paisa.
- The trial balance balances (₹2,98,114.58 each side). All 10 sessions' cash matched between the engine and the ledger.

## 3. Defects found and fixed during testing

| ID | Defect | Found by | Fix |
|---|---|---|---|
| D-01 | Setup drawer showed fields that should be hidden (`[hidden]` lost to `display:flex`) | Test server, browser | CSS: `[hidden]` wins inside ax- containers |
| D-02 | A locked starting amount did not look disabled | browser | disabled style |
| D-03 | Ten buttons all named "Setup" | accessibility tree | `aria-label` "Set up S1"… |
| D-04 | Report period inputs had unassociated labels | structural a11y audit | `label for`/`id` |
| D-05 | Compounding chart: bars on a non-zero baseline, oversized at full width | screenshot review | a line with the floor dashed, half width |
| D-06 | Activity timeline showed raw JSON | screenshot review | events in plain words |
| D-07 | Home alerts showed Telegram HTML tags | Prod screenshot | tags stripped |
| D-08 | Tax rates shown signed and green like P&L | screenshot / PDF review | unsigned "rate" cells in pages, reports and XLSX |
| D-09 | JSON data blocks failed the suite's `node --check` of inline scripts (3 tests) | full suite | data moved to `data-json` attributes |
| D-10 | Unquoted MySQL reserved words `signal`, `rows` in new SQL | full suite | backticked |
| D-11 | A 5-minute job broke the scheduler hook's test fakes (2 W40 tests) | full suite | 15-minute daily slots, 06:00–20:45 IST |
| D-12 | PDF: a note overlapped its table header; "→" printed as "?"; "₹," became "Rs ," | PDF review | fixed in the writer |
| D-13 | The 31 Oct test expected a native Live tab; this brief sets exactly five tabs | full suite | test now checks Live is reachable via More → Markets → Live feeds (**owner to confirm**) |
| D-14 | Smoke harness expected 401 for a token-less write while stopped; the stop guard answers 423 first | Prod smoke | harness corrected (the product behaviour was right) |

## 4. Accessibility audit (WCAG 2.1 AA, automated plus manual review)

- **Structural audit** of the new pages (Home, Sessions, Session detail, Accounting transfers and reconciliation, Reports, Schedules, Settings). Each has `lang="en-IN"`, exactly one `h1`, a skip link, a `main` and labelled `nav` landmarks, a label on every field and a name on every button. **Passed.**
- **Contrast:** every text token on every surface is ≥ 4.5:1 in both themes, as are the status pills, the env badges and the run states. **Passed** (computed by the test, not eyeballed).
- **Keyboard:** Ctrl/⌘K and `/` search; `g`-keys; sortable headers on Enter; table rows open on Enter; Esc closes drawers and returns focus; visible focus rings. Checked in the browser.
- **Colour is never the only cue:** P&L carries + / −; statuses carry a word and a symbol; the env badge carries its word.
- **Reflow:** no horizontal overflow at 390 px on any of the 12 top-level pages, in either theme, on either server. Tables scroll inside their own frame.
- **Not done:** a manual screen-reader pass. The bodies of older pages keep known issues (design pack HR-12/14/20/21/24).

## 5. Mobile apps

Both apps were updated (tabs Home / Trade / Sessions / Reports / More; env badge; version 2026.11.02, build 3) but **neither was built**. This Mac has no Android SDK, Gradle or Java, and no full Xcode or xcodegen.

- Swift: `swiftc -parse` passes on every file, and a Mac Catalyst type-check against the macOS SDK passes in Swift 5 and 6 modes.
- Kotlin: reviewed by hand; the XML resources parse.

## 6. Open items

| Item | Status | Detail |
|---|---|---|
| Hosted Test / Prod (bkesari.com) | BLOCKED | no hosted servers (UXR-11) |
| Android / iOS builds | BLOCKED | toolchains missing on this Mac |
| Older page bodies: own styles, `alert()` errors | IN PROGRESS | shell + tokens on every page; bodies not rewritten |
| Chrome-print PDF engine | NOT RUN | the sandbox blocks Chrome; all PDFs here came from the built-in writer |
| Scheduled reports in the live scheduler | PENDING | restart ATIP on the Mac; watch one trading day (17:30 daily summary) |
| Tax rates, STT treatment | OWNER | verify the FY 2026-27 rates with a CA; decide `stt_deductible` |
| `test_asia_markets`: 5 failures | PRE-EXISTING | fixtures dated 26 Oct are now "stale" by today's date; they fail identically on the code from before this work |

## 7. Screenshots

- Test: `docs/testing/screenshots/test_<page>_<light|dark>_<1440|390>.png` (48).
- Prod snapshot: `docs/testing/screenshots/prod/prod_<page>_<theme>_<width>.png` (48).
- Per-page logs: `test_screenshots.json` and `prod/prod_screenshots.json` record each capture's JS errors, server errors and overflow.
