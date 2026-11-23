# Pre-Open Checklist: testing report

Tested 22–23 Nov 2026 (Sunday night into Monday), 00:11–00:4x IST, on branch `claude/preopen-checklist-2026-11-22`
off GitHub master `0f8f539`. All times IST.

## What each "server" really was

| Server | What it was | Data | Notes |
|---|---|---|---|
| **Test** | Local ATIP on the Mac: `uvicorn dashboard.server:app`, port 8911, `ATIP_ENV=test` | A fresh SQLite DB (`main.py --init`, then migrations 0001–0007) | No Dhan credentials, no Telegram token. Yahoo reachable. NSE's redirect host was not allowed by the sandbox. |
| **Prod snapshot** | Local ATIP on the Mac: port 8912, `ATIP_ENV=staging`, **PostgreSQL** backend, as on the VPS | The VPS's own pg_dump of **20 Nov 2026 19:15 IST**, restored on a throwaway local PostgreSQL (restore 00:14:16–00:14:36 IST) | Never connected to the VPS. Feeds, scheduler and order monitor off. |
| **Prod (VPS)** | The live `bkesari-atip.service` | Live | Read-only source checks done 23 Nov 08:46–09:09 IST (§6a). The deploy goes through the VPS session with the owner's yes; paper mode allows market hours. The first live run is Wed 25 Nov 09:00 IST (24 Nov is an NSE holiday). See §6. |

## 1. Automated tests

| Run | When (IST) | Result |
|---|---|---|
| `tests/test_preopen_checklist.py` (new, 28 tests) | ~00:21 | **28 passed** |
| New + neighbouring suites (market pulse, morning brief, pre-open capture, W39 schedule, VPS deploy tool, RCA) | before 00:11 | **117 passed, 0 failed** |
| Full suite on the branch | 00:11:15–00:25:10 | Failed: only the 3 `test_w39_perf_budget.py` tests (sandbox refuses `socket.bind`) |
| Full suite on untouched master `0f8f539`, same hour | 00:11:15–00:25:00 | **The same 3**, nothing else |
| **Full suite again, with counts: branch** | 00:25:33–00:38:26 | **2,973 passed, 3 failed, 80 skipped** |
| **Full suite again, with counts: untouched master `0f8f539`, same minutes** | 00:25:33–00:38:24 | **2,945 passed, 3 failed, 80 skipped**: the same 3; the branch adds exactly the 28 new tests |

| **DEF-06 fix (23 Nov): checklist + market pulse + scheduler tests** | 09:19:32 | **51 passed** (44 before + 7 new, UT-29..35) |
| **DEF-06 fix: full suite, during market hours** | 09:20:20–09:34:05 | **2,980 passed, 3 failed, 80 skipped**: the same 3 sandbox failures; +7 = the new tests; no market-hours flakes |
| DEF-06 SQL through the PostgreSQL translator (`db.postgres.ddl` / `translate`) and `db.dialect_scan` | ~09:21 | New table and upsert translate (`ON CONFLICT (date) DO UPDATE`); the scan's 8 hand-change statements are all in older files |

The 3 failures are environmental (the Mac's sandbox forbids opening a local port) and fail identically on untouched
master. **The change adds no failure.**

### Unit test cases (tests/test_preopen_checklist.py)

| ID | Case | Result |
|---|---|---|
| UT-01 | US: all four legs green → +1; all red → −1 | PASS |
| UT-02 | US: 2 green / 2 red → 0; all legs inside ±0.10 % → 0; a tighter configured band changes the vote | PASS |
| UT-03 | US: Yahoo raises → UNAVAILABLE, no vote, caveat names the error | PASS |
| UT-04 | US: falls back to stored `global_markets` closes; futures 5 h old are stale and not counted | PASS |
| UT-05 | VIX 16.4 → Volatile with ×0.5 / ×1.5 advice; exactly 15 → Stable; VIX changes no vote; threshold 17 configurable | PASS |
| UT-06 | VIX: no Dhan quote → stored prior close (FALLBACK); nothing stored → Unknown / UNAVAILABLE | PASS |
| UT-07 | A/D from today's pre-open book (after matching vs indicative); counts from items when NSE gives no totals | PASS |
| UT-08 | A/D ratio band configurable (1.1 / 0.9 makes 1,050 / 1,000 neutral) | PASS |
| UT-09 | A/D: NSE still shows Monday's page on Wednesday → PRIOR-DAY (23 Nov, across the 24 Nov holiday), labelled; nothing → UNAVAILABLE | PASS |
| UT-10 | GIFT: basis-adjusted move votes (+0.19 % Gap-Up; +0.08 % Flat although +0.27 % raw; Gap-Down), raw stored | PASS |
| UT-11 | GIFT: no 15:31 reference → NSE day change, else raw "basis not removed"; `gift_vote_basis=raw` | PASS |
| UT-12 | GIFT: band configurable; a 45-min-old quote is STALE and does not vote; no quote → UNAVAILABLE + Dhan-frozen caveat | PASS |
| UT-13 | NSE `marketStatus` `giftnifty` parsing (upper / camel case, list form, "-" price, missing block) | PASS |
| UT-14 | Pre-open payload from a previous day is refused | PASS |
| UT-15 | Vote sum → Bullish / Bearish / Neutral; `bias_min_votes` 2; no votes → Neutral | PASS |
| UT-16 | Config overlay; invalid values fall back to the defaults; unknown keys ignored | PASS |
| UT-17 | Holiday (24 Nov 2026) and Sunday skipped; `enabled: false` skipped; nothing stored | PASS |
| UT-18 | Run stores one row per label with IST time; the 09:00 Telegram line once; 09:09 sends only when the read changed | PASS |
| UT-19 | One broken check does not cost the other three | PASS |
| UT-20 | Learning loop: actual open / class / close / direction; 20-day record (bias 2/3, open call 2/4, VIX means) | PASS |
| UT-21 | Learning loop waits for a missing open, then gives up after 7 days | PASS |
| UT-22 | 15:31 GIFT reference feeds the next session's move across a holiday; skipped on a holiday | PASS |
| UT-23 | App payload (`?compact=1`): not ready → ready with title / body; holiday → no notification | PASS |
| UT-24 | Paper only: the module imports nothing from `orders` | PASS |
| UT-25 | Migration 0007 creates the table on a fresh DB and records APPLIED | PASS |
| UT-26 | Scheduler registers 09:00 / 09:09 / 15:31 / 15:45; jobs do nothing on a non-market day | PASS |
| UT-27 | API, `/preopen` page, `/home` card; POST without token → 401; view-only server → 403 | PASS |
| UT-28 | Backtest scoring compares with the base rate, not 50 % | PASS |
| UT-29 | DEF-06: a 15:31 reference from another contract (25-Nov → 30-Dec) is not used: NSE day change votes, caveat names both contracts; no day change → raw | PASS |
| UT-30 | DEF-06: the same contract written differently ("30-DEC-2026" / "30-Dec-2026") is still used | PASS |
| UT-31 | DEF-06: a reference with no contract on record (as 86db41e stores it) is used, with a caveat | PASS |
| UT-32 | `expiry_key` reads NSE spellings and ISO; "-" / junk → None | PASS |
| UT-33 | The VPS's real `marketStatus` payload of 08:46 IST 23 Nov parses (26,085, +0.5 %, 29-Dec-2026, 08:43 IST) | PASS |
| UT-34 | `market_pulse` (08:45 GIFT estimate) skips a reference from another contract | PASS |
| UT-35 | Migration 0008 creates `preopen_gift_ref`, records APPLIED; 0007 / 0008 show no drift | PASS |

## 2. Test server (local, fresh SQLite, port 8911)

| ID | Case | When (IST) | Result |
|---|---|---|---|
| TS-01 | `main.py --init`, then migrations 0001–0007 applied, `validate()` clean | 00:15 / 00:22:50 | PASS |
| TS-02 | **Live run against real Yahoo** (`run --label preliminary`). US closes for Fri 20 Nov read live (Nasdaq −5.28 %, Dow −1.93 %). The weekend futures quote (03:25 IST 21 Nov) was correctly STALE and not counted. Dhan (no credentials) and NSE (blocked) became caveats, not crashes. Bias = Bearish from 1 of 3 votes, as stored. | 00:17:07–00:17:30 | PASS — [evidence/test_live_run.json](evidence/test_live_run.json) |
| TS-03 | Scripted 09:00 / 09:09 morning with a **labelled SYNTHETIC feed**, through the real code. The basis-adjusted GIFT move was computed from a stored 15:31 reference. Telegram text and app payload generated. | 00:18 | PASS — [evidence/test_synthetic_morning.json](evidence/test_synthetic_morning.json) |
| TS-04 | `/home` card at desktop width: bias / regime / open chips, four checks with value, source and IST time, track-record line, Market card GIFT from the live source | 00:19 | PASS — [screenshots/test_home_card_desktop_dark.jpg](screenshots/test_home_card_desktop_dark.jpg) |
| TS-05 | `/home?app=1` at 375 px (what the apps show) | 00:19 | **FAIL → DEF-01 → fixed → PASS** — [before](screenshots/test_home_card_mobile_375_BEFORE_fix_DEF.jpg), [after](screenshots/test_home_card_mobile_375.jpg) |

## 3. Prod snapshot (VPS data of 20 Nov on PostgreSQL, port 8912)

| ID | Case | When (IST) | Result |
|---|---|---|---|
| PS-01 | Server on the PostgreSQL backend (`PgConnection`) | 00:15 | PASS |
| PS-02 | Migration 0007 applied on PostgreSQL (64 ms); `validate()` clean | 00:16:39 | PASS |
| PS-03 | Replayed the mornings of 18, 19 and 20 Nov (09:00 + 09:09, 6 runs) through the real code. US from Yahoo history as at 09:00. VIX from the stored prior close and A/D from the stored 09:09 pre-open capture, both FALLBACK. GIFT UNAVAILABLE (no history). | 00:16:51–00:16:53 | PASS — [evidence/prod_snapshot_replay.json](evidence/prod_snapshot_replay.json) |
| PS-04 | Learning loop on the real stored opens and closes. **18 Nov:** Bearish, gap −0.28 %, close −0.63 % (right). **19 Nov:** Neutral, gap −0.20 %, close +1.14 %. **20 Nov:** Bearish, gap −0.44 %, close +1.12 % (wrong). Record: bias 1/2, US 2/3, A/D 1/3. | 00:16:52 | PASS (scoring correct) |
| PS-05 | `/preopen` page and Home card on PostgreSQL: card with "last run is from 2026-11-20" banner, track record, 30-day history, thresholds | 00:20 | PASS — [screenshots/prodsnapshot_preopen_page_top.jpg](screenshots/prodsnapshot_preopen_page_top.jpg), [screenshots/prodsnapshot_preopen_track_record.jpg](screenshots/prodsnapshot_preopen_track_record.jpg) |

Note on PS-03: the 09:00 replay could see the 09:09 pre-open capture stored that day. That is a replay artefact:
live, at 09:00, that capture does not exist yet and the check falls back to prior-day.

## 4. Defects found and fixed

| ID | Found by | Defect | Fix | Status |
|---|---|---|---|---|
| DEF-01 | TS-05 | The Home card's 3-column table overflowed at 375 px (scrollWidth 381 > 307) | Stacked rows (name + signal, details below); 341 = 341 after | Fixed, re-tested |
| DEF-02 | UT-27 | `/preopen` returned 422: `from __future__ import annotations` hid the `Req` type from FastAPI | Import removed (comment explains) | Fixed, re-tested |
| DEF-03 | TS-02 | A multi-line Dhan error message went into a caveat | First line only | Fixed |
| DEF-04 | TS-03 | Wording: "Volatile volatility" in the app notification; "(09:09 update IST)" in Telegram | "VIX 17.9 (Volatile)"; "(09:09 IST update)" | Fixed, tests updated |
| DEF-05 | PS-05 | The track-record note repeated its own sentence | One note plus the backtest line | Fixed |
| DEF-06 | PR-01 (VPS live payload, 23 Nov) | The 15:31 GIFT reference kept no contract. NSE already showed the 29-Dec-2026 contract on 23 Nov, so the morning after a switch the "basis-free" move would include the gap between two months' prices (about 0.3–0.7 %) and cast a false Gap-Up / Gap-Down vote. Same in `market_pulse`. | New table `preopen_gift_ref` (migration 0008) stores the contract; a reference from another contract is not used (NSE's day change instead, with a caveat); `market_pulse` skips it too | Fixed after 86db41e, UT-29..35 |

### Existing problems found in the audit, outside this change's tests

| ID | Problem | State |
|---|---|---|
| F1 | Dhan's GIFT Nifty quote is frozen; four screens and jobs read it | **Fixed** in this change (PO-CL-08) |
| F2 | `prices_daily` NIFTY50 / INDIAVIX are close-only (open = high = low = close) | Reported, not changed |
| F3 | `www.nseindia.com` answered 301 → `www.nseindia.in` on the Mac (23:28 IST 22 Nov) | **Mac only.** The VPS sees no redirect (08:47 IST 23 Nov: curl 403 / HTTP/2 error, no 301), and ATIP's NSE client there gets both feeds (PR-01, PR-02). Nothing to fix on the VPS. |
| F4 | NSE holiday list ends 25 Dec 2026 | Known, open |

## 5. Mobile apps

Version **2026.11.22.1** (build 13) on both apps. The card needs no native change: both apps load the server's
`/home`, which TS-05 checked at 375 px.

| ID | Case | When (IST) | Result |
|---|---|---|---|
| MB-01 | Android `./gradlew assembleDebug` | 00:07:26 | **BUILD SUCCESSFUL** (35 tasks, no Kotlin warnings). The APK carries versionCode 13 / 2026.11.22.1, `POST_NOTIFICATIONS`, `RECEIVE_BOOT_COMPLETED` and the receiver (`aapt2`). It is in the zip under `mobile_build/`. |
| MB-02 | Android notification rules (`PreOpenRules.kt`) compiled with Kotlin 2.1.0 on the JVM, 20 cases: IST window on weekdays, Fri→Mon, phone set to New York / London time, holiday / not ready / ready / stale date / missing title | 00:0x | 20/20 PASS (a minimal `org.json` stand-in was used) |
| MB-03 | Android contract: `GET {server}/api/preopen?compact=1` with the WebView cookies; `trading_day:false` → no notification; ready → server title and body; otherwise one retry ~2 min later, then a generic "tap to open" notification; tap opens `/preopen` | review | Matches the server (UT-23) |
| MB-04 | iOS: all Swift passes `swiftc -parse`; a full type-check was impossible (SDK/compiler mismatch on this Mac) | 00:2x | Syntax only, **not built** (no Xcode / Apple ID) |
| MB-05 | On a real phone: the alarm firing in Doze, cookies in the receiver while the app is closed, the permission dialog, the tap | — | **Not tested**: needs the owner's phone after the VPS deploy |

Limits:
- Android delivers between **09:00:30 and 09:10:30 IST**, because Android 12+ stretches inexact windows to 10
  minutes. That window avoids the special exact-alarm permission.
- iOS shows a fixed reminder at 09:00 IST on weekdays and cannot skip NSE holidays (it never calls the server).

## 6. Prod (VPS)

The owner said "Yes, commit & deploy" at about 08:42 IST on 23 Nov. 86db41e went to GitHub master at 08:44:20 IST,
and the deploy was handed to the VPS session. The owner then said paper mode makes a market-hours deploy safe, and asked
for "all the changes to production right now".

### 6a. Read-only checks on the VPS, Mon 23 Nov (before the deploy, by the VPS session)

| ID | Check | When (IST) | Result |
|---|---|---|---|
| PR-00 | A fresh clone of 86db41e against the 14 deploy files' sha256 | ~08:46 | All 14 match. Outside docs and mobile, nothing changed since 43ca948. All files compile. |
| PR-01 | NSE `marketStatus` through ATIP's own `data.nse_api` client | 08:46:14 | Keys `marketState, marketcap, indicativenifty50, giftnifty, niftyusd`. `giftnifty`: LASTPRICE 26,085, PERCHANGE +0.5, EXPIRYDATE 29-Dec-2026, TIMESTMP "23-Nov-2026 08:43". **The GIFT source works** (UT-33 parses this exact payload). It also exposed **DEF-06**. |
| PR-02 | NSE pre-open `key=ALL` | 09:09:08 | 2,421 items (`metadata` + `detail`); top-level advances 1,437 / declines 626 / unchanged 358. **The live A/D source works.** |
| PR-03 | `www.nseindia.com` from the VPS | 08:47 | No 301 (plain curl: HTTP/2 INTERNAL_ERROR; browser agent: 403). F3 is Mac-only. The journal has no NSE errors since 08:00; the 08:45:05 pre-open gate is OK. |

### 6b. Still to run

1. **Deploy and start-up.**
   - After the restart, `schema_migrations` shows 0007 APPLIED, and 0008 too once the DEF-06 fix is deployed.
   - `GET /atip/api/preopen` returns 200.
2. **Mon 23 Nov 15:31 IST.** The first GIFT reference is stored. If only 86db41e is live by then, it carries no contract (caveat on Wednesday, UT-31).
3. **Wed 25 Nov 09:00 and 09:09 IST.**
   - Rows are stored.
   - The Telegram line arrives.
   - The Android notification shows the read.
   - The Home card shows on the web and in both apps.
4. **Wed 25 Nov 15:31 and 15:45 IST.**
   - The GIFT reference is stored.
   - The runs are scored.
   - `/preopen` shows the first scored day.
5. Record the times in IST in this report and in the tracker (PO-PROD).
