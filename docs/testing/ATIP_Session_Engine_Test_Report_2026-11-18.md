# ATIP session engine: test report (Test and Prod), 18 Nov 2026

Owner prompt: 18 Nov 2026, 03:05 PM IST: "Independent ₹25K trading sessions (session engine)". **PAPER only.** Every
time below is IST. Design and decisions are in `docs/reports/2026-11-18-session-engine/SESSION_ENGINE.md`; the file list
is in `CHANGELOG.md` there. Evidence (JSON, scripts, screenshots) is in `docs/reports/2026-11-18-session-engine/evidence/`.

## What "Test server" and "Prod server" were

ATIP's live copy runs on the VPS (`bkesari-atip.service`, PostgreSQL) since 17 Nov 2026. This Mac session has no access
to it, and the VPS was **not** touched. The two servers here are local instances of this branch's code, as in the
earlier reports.

| | Test server | Prod server (rehearsal) |
|---|---|---|
| URL | http://localhost:8881 | http://localhost:8882 |
| Code | branch `claude/session-engine-2026-11-18` (worktree), from master `be077b2` | same |
| Database | fresh SQLite 3.53.4 (`.ux_build/session-engine-2026-11-18/srv/test/atip.db`) | **PostgreSQL 18.6** (throwaway Postgres.app server, socket only), loaded from the Mac's last live database (17 Nov 2026 11:58 IST, the moment the Mac was retired); 400,627 rows: all session, order, paper and ledger rows, with large research tables trimmed |
| Config | `ATIP_ENV=test`, PAPER, paper balance ₹2,50,000, NSE delivery charges, the live 10 % capital-plan buffer | `ATIP_ENV=staging`, PAPER, the live capital plan and risk limits, `database.backend=postgresql` (as on the VPS) |
| Credentials | none (no Dhan token, no Telegram): no order can reach a broker | none |

The market closed at 15:30 IST. Trades on the Test server were replayed through the real order code
(`orders.rules` → `orders.broker` → `orders.paper`) with a scripted quote feed.

Environment:
* macOS 14.6.1 (arm64), Python 3.15.2, FastAPI 0.162.0, psycopg 3.3.9 (scratch folder, not the project's `.venv`);
* Android: Temurin 17 and Gradle 8.11.1;
* no Xcode.

## Results at a glance

| Area | Result |
|---|---|
| New tests (`tests/test_session_engine_2026_11_18.py`) | **18 / 18 passed** |
| 10 sessions × 500 trades simulation | **to the paisa** on SQLite (3.1 s) and on PostgreSQL 18.6 (9.0 s) |
| Related suites (sessions, cycles, spare cash, capital plan, risk, retail, PostgreSQL backend, UX shell) | 238 passed, 12 skipped (the live-PostgreSQL ones without a URL) |
| Full suite | **2,806 passed, 34 failed, 69 skipped** (15:54:28–16:05:37 IST). Untouched master `be077b2` at 15:43:32–15:54:28 IST: 2,789 passed, 33 failed. **The same 33 fail on both.** They depend on the clock (12 auto-pick tests after 14:45 IST, plus the board, RCA and verification tests built on them), on network or sandbox sockets (3), or on the market hour. The 34th is the order-path guard, whose allowlist half passes; it failed only because the branch was not committed yet. No new failure. |
| On the commit | Committed on the branch at 18:19 IST. The order-path guard plus the 18 engine tests: **19 passed** (18:19:39–18:19:55 IST). |
| Test server | 8 / 8 checks passed (1 informational) |
| Prod server (rehearsal) | 9 / 9 checks passed |
| Android app | built 15:24:24 IST (2026.11.18.1, build 12) |
| iOS app | **not built**: no Xcode on the Mac (needs the owner's Apple ID) |
| Live VPS | **not deployed, not tested**: it needs the owner's yes in the VPS session |

## Test cases

### Automated (pytest)

| # | Test | Result |
|---|---|---|
| 1 | The split is floor(funds ÷ ₹25,000) and the rest is the reserve. ₹1,12,345 gives 4 sessions (S11–S14), each ₹25,000 and CREATED with a ₹20,000 floor and an IST start; reserve ₹12,345. It refuses without ack, and refuses 5 sessions. | PASS |
| 2 | The split keeps the capital plan's 10 % buffer. ₹1,00,000 gives 3 sessions; with `respect_buffer: false`, 4. | PASS |
| 3 | A CREATED session holds its entries. `activate` needs ack and makes it ACTIVE (AUTO, auto-pick on). Its STATE events are CREATED then ACTIVE, with IST times. | PASS |
| 4 | Ledger maths on one real round trip with NSE delivery charges. 1 leg while open, 2 after. The ledger runs SEED, BUY, CHARGES, SELL, CHARGES, PNL. Net = sell − buy − both legs' charges; cash = seed + net. The `session_trades` row carries both leg ids and the security id. | PASS |
| 5 | Compounding: the next buy uses the whole grown cash, which is more than the ₹25,000 seed. | PASS |
| 6 | Every `order_log` row carries its session: S1's BUY and SELL, and a manual order as MANUAL. | PASS |
| 7 | `order_log` backfill on an old table: ATIP-S3 → S3, an untagged paper order → MANUAL, a refused send → UNTAGGED. | PASS |
| 8 | Isolation. S1 cannot buy ₹30,000 with its ₹25,000, even though S2 and the account have spare money: the gate holds it, and at the broker it is BLOCKED_INSUFFICIENT_FUNDS, logged with S1 and ₹25,000 available. S2's cash is untouched. | PASS |
| 9 | No daily order-count cap inside a pool, while a session without a pool keeps its `max_trades`. | PASS |
| 10 | Auto-stop. At the cap with a trade open: no new BUY, not yet COMPLETED. After the sale: COMPLETED, auto-pick off, IST time, final ledger frozen (`sync` leaves it alone), alert recorded, banner item. | PASS |
| 11 | Floor. A gap through the stop leaves ₹17,500 (< ₹20,000). The session becomes PAUSED – BELOW FLOOR with a warning alert. No transfer in, S2 untouched, `activate` refused, and it is not paused again on the next pass. | PASS |
| 12 | The shell state (the apps' banner) says "S1 paused: below floor" in amber. | PASS |
| 13 | The Sessions page shows the summary row, "1 of 500 trades", ACTIVE and the win rate. The drill-down shows the trade and ledger tables and the CSV links. | PASS |
| 14 | API and CSV: overview, plan (5 fit), create without ack → 400, create 2, activate, one session, trades CSV (header, 1 row, CLOSED) and ledger CSV (header and 4 lines), and a session with no seed → 400. | PASS |
| 15 | **10 sessions × 500 trades**, mock prices, real NSE delivery charges on every leg. For each session, these all equal a hand-walked cash figure to the paisa: the engine's cash, the frozen final ledger, the last `balance_after`, and the sum of the ledger's money lines. The PNL lines and the trade rows sum to the growth. 500 trades and 1,000 legs each. Every charge is booked once. Each ledger holds only its own session's lines. All 10 are COMPLETED. | PASS |
| 16 | The same simulation on **PostgreSQL 18.6** (`evidence/pg_simulation_10x500.json`). | PASS |
| 17 | A whole-cash LIMIT buy 0.5 % above its quote (as auto-pick sends it) is not refused: 250 shares on ₹25,000 at a ₹100.50 limit fill at ₹100. 251 shares are still refused. | PASS |
| 18 | The same on the real order path: a SESSION entry with a LIMIT above its trigger fills, with no BLOCKED row | PASS |
| 19 | A floor-paused session that the owner resumes loses its BELOW_FLOOR reason, so a later kill-switch pause shows plain PAUSED | PASS |

### Test server (http://localhost:8881, 15:25–15:31 IST)

| # | Check | Result |
|---|---|---|
| T1 | Sessions page before: reserve ₹2,50,000, buffer ₹25,000, "room for 9 more ₹25K sessions" | PASS |
| T2 | "Split reserve into ₹25K sessions…" without the tick: "Tick the box first." | PASS |
| T3 | With the tick: 9 sessions created (S11–S19), all CREATED at ₹25,000, "0 of 500 trades", floor ₹20,000; reserve ₹25,000 (the buffer) | PASS |
| T4 | Replay run 1 (15:29:03): the buys were refused by the existing daily-loss limit. A fresh database has no P&L history, so that limit fails closed. Every refused row still carried its session id. The Test config then dropped the two history-based limits. | INFO (not a defect) |
| T5 | Replay run 2 (15:29:21) through the real order path:<br>• S11: 2 winning trades, ₹26,369.33 (+5.48 %), 4 legs, charges ₹145.67. Its second buy was ₹25,600, more than the seed, so the profit was reinvested.<br>• S12: a gap through its stop, ₹18,735.38 (−25.06 %), **PAUSED – BELOW FLOOR**.<br>• S13: cap of 2 trades, **COMPLETED**. | PASS |
| T6 | All 10 order rows carry S11, S12 or S13 | PASS |
| T7 | S11 "Trades & ledger": wallet tiles, 2 trades, 11 ledger lines; `S11_ledger_IST.csv` downloads as text/csv | PASS |
| T8 | Phone width 375 px (`?app=1`, as the apps load it): no sideways scroll; the paused and completed cards read clearly. The banner text is "S12 paused: below floor · S13 completed 2 trades" (amber). | PASS |

### Prod server: rehearsal on the last live data, PostgreSQL (http://localhost:8882, 15:25–15:31 IST)

| # | Check | Result |
|---|---|---|
| P1 | Copy into PostgreSQL with this morning's fixed copy tool: 400,627 rows, every table's count equal. **68 / 68 id counters at MAX + 1**, including `session_events` (next 245) and `news_impact.article_id` (next 16,875). | PASS |
| P2 | `order_log.session_id` added on PostgreSQL and the 103 old rows backfilled: 85 to S1–S10, 7 MANUAL, 11 UNTAGGED | PASS |
| P3 | For each of S1–S10, the session ledger's last balance equals the engine's cash | PASS |
| P4 | **Deploy-day check:** `check()` changes nothing. All 10 are ACTIVE, ₹44,435–₹47,382 against a ₹36,000 floor, 1–6 of 500 trades. | PASS |
| P5 | Split plan: reserve ₹54,865.24, buffer ₹51,039.82, ₹3,825.42 above it, so 0 sessions fit. Rebase preview: 8 would fit if the 16 Nov top-ups went back (3 sessions idle now, 7 holding). | PASS |
| P6 | Create is refused: "no Rs 25,000 session fits: Rs 3,825.42 available above the Rs 51,039.82 cash buffer" | PASS |
| P7 | Sessions page: summary of 10 active, combined balance ₹4,55,532.93 on ₹4,50,000 (+₹5,532.93), reserve ₹54,865.24. Each card shows seed ₹25,000 + ₹20,000 moved in, growth, the 500-trade meter, win rate, legs and "pauses under ₹36,000". | PASS |
| P8 | S1 "Trades & ledger": since 27 Oct 2026, 5 of 500 trades, 11 legs, 1 open (PVRINOX), 6 trades and 29 ledger lines; the CSV carries real Dhan security ids | PASS |
| P9 | Shell state: no session alert (none paused or completed) | PASS |

### Mobile

| # | Check | Result |
|---|---|---|
| M1 | Android `./gradlew assembleDebug` (15:24:24 IST): BUILD SUCCESSFUL, `app-debug.apk` 5.6 MB, versionCode 12 / 2026.11.18.1. The banner change compiles. | PASS |
| M2 | iOS build | BLOCKED: Xcode is not installed (needs the owner's Apple ID; Xcode 16.2 is the newest for macOS 14.6). The Swift change mirrors the Kotlin one. |

## Defects found and fixed during testing

| # | Defect | Fix |
|---|---|---|
| D1 | New sessions were named after the copied session ("Options (spare cash)") | They are now named "₹25,000 session" |
| D2 | The card had two different "floor" figures: the existing minimum buy per cycle and the new pause floor | The new one reads "pauses under ₹x" and "Pause floor" |
| D3 | Two "Export CSV" buttons on the drill-down (the table's own, plus the new one) | The new link is "Full CSV (IST)" (every column) |
| D4 | The engine re-ran its table DDL on every 30-second pass, a lock request on PostgreSQL each time | Once per database per process |
| D5 | The order-path guard test did not list the new engine file | Listed, with the reason, as each earlier feature did |
| D6 | Two test assumptions: no `session_transfers` table without a transfer; no CHARGES lines in a zero-charge test | Tests corrected |
| D7 | **Found in self-review, before any run would have hit it.** The new broker check valued a pool's LIMIT buy at its limit price. An auto-pick buy is sized at the quote that fired it and sent up to 0.5 % above, so a session buying with all of its cash (S5, and every new ₹25K session, which copies S5) would have been refused. | In PAPER, valued at the lower of the limit and the quote, which is what the paper broker fills at. LIVE stays at the limit. Tests 17 and 18. |
| D8 | A floor-paused session resumed by hand kept its BELOW_FLOOR reason, so a later kill-switch pause would show as "below floor" | The reason is cleared once the session runs again. Test 19. |
| D9 | The tracker update first used the IDs SE-01…, which the Strategy Engine rows already use, and its "replace this change's rows" step removed 11 of them. The diff showed it before delivery. | Both tracker files restored from git. This change's rows are SEN-01 … SEN-DEPLOY; the CSV diff is now 14 lines added, 0 removed. |

No defect was found in the existing engine. On real data, the cash books of S1–S10 agree with the engine to the paisa.

## Not done, or for the owner

1. **Deploy to the VPS**: the branch is committed (18 Nov, 18:19 IST; not merged to master, not pushed). Deploy through the VPS session with the owner's yes there. Then rerun P2–P9 on the live database.
2. **The live split**: today no ₹25K session fits above the buffer. More sessions need either `rebase` (returning the 16 Nov top-ups, which reverses that choice) or a smaller buffer.
3. **iOS build**: needs Xcode.
4. The floor is 80 % of capital (₹36,000 for S1–S10) by default. 80 % of the ₹25,000 seed (₹20,000) is the alternative.
