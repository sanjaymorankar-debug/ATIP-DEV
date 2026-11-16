# ATIP — Today's Trading & Validation Report, Mon 16 Nov 2026 (post-market)

Owner prompt 16 Nov 2026, 4:39 PM IST. **PAPER only: no order was placed with Dhan or Kite.** All times IST.
Data: read-only SQLite backups of the live `atip_data/atip.db`, taken 16:41:36 IST (trading data) and 16:52:44 IST
(after the 16:45 post-market run finished at 16:49:38). Code: branch `claude/capital-plan-2026-11-16` (worktree
`.claude/worktrees/capital-2026-11-16`), **not committed and not live** — the live Mac still runs master `e41ccf3`
(pid 82833, since 15:32:42 IST).

---

## 1. Summary

| Item | 16 Nov 2026 |
|---|---|
| Paper capital (pool) | ₹5,00,000 opened; equity at the close **₹5,09,598.94** (cash ₹3,27,564.54 + stock ₹1,82,034.40 at the close) |
| Deployed (stock at cost) | **₹1,82,035.24 = 35.7 %** of equity — not the assumed ~₹2.5 L |
| Idle | **₹3,27,564.54 = 64.3 %**: ₹2,54,865.24 belongs to no session + ₹72,699.30 inside the sessions |
| Trades taken | 2 buys (GRASIM S6, DLF S9), 4 sales (KALYANKJIL S6, ATHERENERG S8, M&M S4, SAILIFE S9); 2 buys resting |
| Wins / losses (closed today) | 2 / 2 (50 %) |
| Gross P&L (closed today) | **+₹212.13** |
| Charges | ₹278.14 on today's four round trips (+ ₹37.59 on today's two buys, carried in their cost) |
| Net realised P&L | **−₹66.01** |
| Day change in paper equity (marks incl.) | +₹550.70 (pnl_daily PAPER) |
| Best trade | S6 KALYANKJIL +₹581.79 net (35 @ ₹698.85 → ₹717.50, trailing stop) |
| Worst trade | S9 SAILIFE −₹612.46 net (13 @ ₹1,921.16 → ₹1,879.50, 2 % stop) |
| Sessions since 29 Oct (10 × ₹25,000) | equity ₹2,54,733.70: **+₹4,733.70 (+1.89 %)**, 38 closed trades, 21 won (55.3 %) |
| Issues found / fixed | **18 found** (0 Critical, 5 High, 5 Medium, 6 Low, 2 Info); **4 High fixed + 1 High partly fixed** in code (not live yet) |

---

## 2. Task 1 — Capital utilisation and session planning

### 2.1 Per session at the close (app's own ledger code on the 16:52 snapshot)

| Session (arm) | Allocated | Cash | Deployed (cost) | Open position | Closed today | Realised today (net) | Unrealised | Closed / won since 29 Oct | Win rate |
|---|---:|---:|---:|---|---:|---:|---:|---:|---:|
| S1 Momentum | 25,000 | 1,311.87 | 24,773.65 | PVRINOX 15 | 0 | 0.00 | −182.65 | 5 / 3 | 60 % |
| S2 Buy zones (retest K2) | 25,000 | 887.34 | 23,935.24 | INDHOTEL 27 | 0 | 0.00 | −13.24 | 4 / 2 | 50 % |
| S3 Breakouts (retest K1) | 25,000 | 471.92 | 27,316.01 | AUROPHARMA 15 | 0 | 0.00 | −406.01 | 3 / 3 | 100 % |
| S4 Swing (retest K4) | 25,000 | 25,795.15 | 0.00 | — (CDSL resting buy ₹1,408.10 × 9) | 1 | −590.42 | 0.00 | 3 / 2 | 67 % |
| S5 Control | 25,000 | 360.41 | 24,460.98 | KOTAKBANK 50 | 0 | 0.00 | +249.02 | 3 / 1 | 33 % |
| S6 Recovery (ATR) | 25,000 | 13,597.59 | 12,143.19 | GRASIM 4 (bought today) | 1 | +581.79 | +194.41 | 5 / 3 | 60 % |
| S7 Wide-stop | 25,000 | 367.76 | 25,316.24 | ICICIBANK 16 | 0 | 0.00 | +43.76 | 1 / 1 | 100 % |
| S8 Sector | 25,000 | 24,985.47 | 0.00 | — (KALYANKJIL limit ₹700.45 unfilled) | 1 | +555.08 | 0.00 | 4 / 2 | 50 % |
| S9 Bracket 3/6 | 25,000 | 4,811.53 | 19,584.30 | DLF 26 (bought today) | 1 | −612.46 | +39.20 | 5 / 2 | 40 % |
| S10 Retest fast-gate | 25,000 | 110.26 | 24,505.63 | PNB 170 | 0 | 0.00 | +74.67 | 5 / 2 | 40 % |
| **All ten** | **2,50,000** | **72,699.30** | **1,82,035.24** | 8 open | **4** | **−66.01** | **−0.84** | **38 / 21** | **55.3 %** |
| Unallocated paper cash | — | 2,54,865.24 | — | | | | | | |

### 2.2 Why the capital sits idle (in order of size)

1. **Allocation cap (₹2,54,865 = 50 % of the pool).** S1–S10 were given ₹25,000 each on 29 Oct (the owner's
   10 × ₹25,000 plan); the account opened with ₹5,00,000. Nothing ever allocated the other half. KGF-23 (live 15:32)
   added "New paper session…", but no S11 was made (owner's choice).
2. **Position-size limits by design (₹~23,000 at the close, more from tomorrow).** Since 06:29 IST the arms size a buy
   at 2.5 % of the session's cash at risk: max(floor, 2.5 % × cash / stop %). A 5 % stop (S2/S3/S4/S10) buys 50 % of
   the cash, 3 % (S8/S9) 83 %, 4 % (S7) 62.5 %; only S5 (control) buys its whole cash. Today S6 bought GRASIM for
   ₹12,143 of ₹25,741 (stop 4.84 %). With every session in a trade at today's sizes, only ₹1.61 L (31.6 %) would be
   deployed; the 30 Oct–16 Nov average occupancy (62 % of session-days in a trade) puts it nearer **20 %**.
3. **One position per session; exits late in the day.** S4 (M&M stop 10:27) and S8 (ATHERENERG trail 10:26) sold and
   then waited: S8's KALYANKJIL pick was a LIMIT at ₹700.45 that the price (₹712) never came back to; S4's Kite retest
   arm rests a DUMMY buy at the CDSL breakout low (₹1,408.10) by design. The 14:45 cut-off stops later picks.
4. **Signal scarcity.** 13 Nov had **0 BUY** signals (0 BUY on every day 6–13 Nov; 1 on 16 Nov). Rank-fill (SES-09)
   fills sessions from the top HOLD stocks, so scarcity does not leave sessions empty, but it lowers pick quality.
5. **Not the cause:** no funds-check bug (the paper funds check reads the whole paper balance, ₹3,27,564.54, and
   each session's own cash bounds its order); every session was RUNNING / AUTO / auto-pick on all day; the breadth
   gates were OPEN (breadth50 56.79 %, breadth20 80.69 % on 13 Nov).

### 2.3 Options compared (₹5,09,599 pool, 10 % buffer = ₹50,960, headroom ₹2,03,905)

| | (a) Raise each session's allocation | (b) Add parallel sessions | (c) Hybrid |
|---|---|---|---|
| What | +₹20,000 to each of S1–S10 (₹45,000 each) by transfer | S11–S18, ₹25,000 each (8 new) | +₹10,000 each to S1–S10 + S11–S14 at ₹25,000 |
| Allocated after | 89.2 % | 89.2 % | 89.2 % |
| Positions max | 10 | 18 | 14 |
| Deployed, all in a trade | 56.5 % (₹2.88 L) | 55 % with risk-budget arms; 71 % if the new sessions copy S5's whole-cash rule (the add_session default) | 56–64 % (same two cases) |
| Pick quality | unchanged (ranks 1–10) | ranks 11–18 of a list that had 0 BUY | ranks 11–14 |
| A/B arms (judged 3 Jan 2027) | stay comparable (same top-up each) | 8 new arms with no history | mixed |
| Design | stays within the "up to 10 parallel sessions" target | **exceeds the 10-session target** (MAX_SESSIONS = 10) | exceeds it |
| Orders/day (limit 50) | ≤ 20 | ≤ 36 | ≤ 28 |
| Risk per trade (2.5 % of cash) | ₹625 → ₹1,125 | ₹625 each | ₹875 / ₹625 |

**Guardrails, each option:** max per trade = the session's own cash, risk-budgeted (largest planned buy ₹44,821, S5
control); max per session = `max_session_pct` 10 % of the pool (₹50,960); max open positions = 1 per session (10 / 18 /
14) under the account's 50; daily loss = 6 % of each session's capital (₹2,700 at ₹45,000; was ₹1,500 at ₹25,000) and
the account's ₹10,000 / 10 % drawdown (unchanged, owner's 16 Nov limits); cash buffer ≥ 10 % never allocated.
Note: ten ₹1,125 stops on one day (₹11,250) would exceed the account's ₹10,000 daily loss limit — that limit then
blocks further BUYs (never sells), as designed.

**Recommended: (a).** It uses the money without exceeding the 10-session design, keeps the A/B experiment fair, picks
from the same top-ranked stocks, and moves cash by the existing SES-03 transfer (no books restart, no open trade touched).
Option (b) deploys more only when its new sessions buy with their whole cash: the lever for deployment is the sizing
rule (CAP-09, owner decision), not the number of sessions.

### 2.4 Implemented (paper, config-driven) — `orders/capital_plan.py`

* Config `atip_data/config.json` `"paper_capital_plan"`: `cash_buffer_pct` 10, `max_session_pct` 10, `sessions`
  "running", `round_to` 1000, `session_loss_limit_pct` 6, `pool` "equity" — shares only, no rupee amounts.
* Top-up = round_down((spare − buffer) / running sessions, round_to), capped by `max_session_pct`; applied by
  `accounting/ledger.transfer` from the unallocated pot; daily loss limit rescaled; one `CAPITAL_PLAN` event.
* **Funds check / over-allocation (CAP-01):** a transfer in never takes more than the cash no session owns; with the
  block, nothing (transfer, `start_pool`/`setup_pools`, `add_session`) eats into the buffer or passes the per-session
  cap; `spare_paper_cash` now reports `over_allocated`.
* **P&L (CAP-03):** a session's P&L and return are on its capital (start + transfers) — the live code would have shown
  the top-up as +₹2,04,733.70 profit. The return % is a simple money-weighted figure: right after the top-up S3 reads
  +5.29 % (₹2,382 on ₹45,000) where it read +9.53 % on ₹25,000; the rupee P&L is unchanged. A time-weighted return
  per session is the refinement if the owner wants the before/after percentages comparable.
* `python -m orders.capital_plan` (plan), `--apply --ack`; `GET /api/v2/capital/plan`, `POST /api/v2/capital/apply`;
  "Top up sessions…" dialog on /sessions (tick to confirm). PAPER only; refused on the viewer.

### 2.5 Before and after (applied on the Prod copy of today's data, 17:09 IST)

| | Before | After top-up (now) | After, every session in a trade |
|---|---:|---:|---:|
| Allocated to sessions | ₹2,54,733.70 (49.99 %) | ₹4,54,733.70 (89.2 %) | 89.2 % |
| Unallocated (buffer) | ₹2,54,865.24 | ₹54,865.24 (10.8 %) | 10.8 % |
| Deployed (cost) | ₹1,82,035 (35.7 %) | ₹1,82,035 (35.7 %) — open trades unchanged | ₹2,87,850 (56.5 %) vs ₹1,60,893 (31.6 %) without the top-up |
| Deployed at the 62 % historical occupancy | ~₹1.00 L (~20 %) | ~₹1.80 L (~35 %) | — |
| Risk at each stop (2.5 % of cash) | ~₹625 | ~₹1,125 | |
| S4 / S8 next buy (free now) | ₹12,898 / ₹20,820 | ₹22,898 / ₹37,486 | |

**Projected effect.** Rupee P&L per trade scales about ×1.8 for the same percentage result, in both directions. Since
29 Oct the ten sessions made +1.89 % (+₹4,734 net of charges; +₹1,413 after the per-cycle tax reserve); the 27 Jul–29 Oct
study put the expectancy near −0.5 % a trade, so a bigger allocation is not a forecast of profit — it is paper.
**Not applied on the live Mac:** the top-up must go live together with CAP-03, or the live pages would show it as
profit. It needs the branch merged and ATIP restarted (owner's go-ahead), then `python -m orders.capital_plan --apply --ack`.

---

## 3. Task 2 — Formula and signal validation

### 3.1 Formula validation (scores of 16 Nov, universe 499)

| Formula | Executed today (IST) | Coverage | Accuracy check | Status |
|---|---|---:|---|---|
| VPI | Y — ai_scoring_engine 16:49:18 | 100 % | 499/499 recomputed from stored components × weights (re-normalised): max diff 0.005 | OK |
| ZPI | Y — 16:49:18 | 100 % | 499/499, max 0.005 | OK |
| MRI | Y — 16:49:18 | 100 % | 499/499, max 0.005 | OK |
| RRI | Y — 16:49:18 | 100 % | 499/499, max 0.005 | OK |
| CRI | Y — 16:49:18 | 100 % | 499/499, max 0.005 | OK |
| ACS | Y — 16:49:18 | 100 % | 499/499, max 0.005 | OK |
| MSI | Y — 16:49:18 | 100 % | market-level inputs; FII/DII for 16 Nov not yet published → signal_log HELD, re-score when NSE publishes (by design) | OK (pending re-score) |
| INS (institutional) | Y — 16:49:18 | 100 % | 499/499, max 0.004 | OK |
| TOD score | Y — 16:49:18 | 100 % | 499/499, max 0.005 | OK |
| ATIP composite | Y — 16:49:18 | 100 % | 499/499, max 0.005 | OK |
| SPI / FS (fundamentals) | Y (ran) | **0 %** | null for all 499 since ≥ 29 Oct: switch `fundamentals.score_enabled` off (W27) | Gap — owner decision (CAP-08) |
| Market Health Score | Y — 16 Nov row, 51.91 NEUTRAL, 10/10 inputs | 100 % | NSE index closes "not published" at 16:48 → data_quality index_series ERROR 13/13 | Caveat (CAP-07) |
| 37 technical indicators (RSI, Stoch, W%R, CCI, MACD ×4, ADX, EMA 9/21/50, SMA200, ATR, ATR %, BB ×4, OBV, Vol SMA/ratio/rel, pivots ×5, Fib ×4, crosses, >200DMA, gap %, tech score) | Y — technical_indicators 16:48:31 (3,375 rows) | 36 of 38 columns 100 %; SMA200 / above_200dma 98.6 % (7 listings with < 200 sessions, by design); **EMA 100 %, no small/mid-cap gaps** | 10 stocks (RELIANCE, HDFCBANK, INFY, DLF, GRASIM, KALYANKJIL, CDSL, IGIL, ATHERENERG, SAILIFE) recomputed with plain pandas from prices_daily on the pipeline's 260-bar window: **311 of 320 values within 0.5 %**; the 9 others are OBV, which matches the `ta` library exactly (an unchanged close counts as up-volume) | OK (CAP-12 note) |
| technical_ext (Supertrend, Ichimoku, Keltner, Donchian, MFI, CMF, ROC, Aroon, PSAR, β, weekly RSI, sector RS, S/R) | Y — 16:48:31 | 11 of 15 at 100 %; CMF 99.4, sector RS 98.6, support 98.6, resistance 95.6 % | coverage only | OK |
| Per-stock target / stop (predictions) | Y — 16:49:25 | entry 100 %; stop/targets for BUY only (1: LAURUSLABS entry 2,220, SL 2,095.91, T1 2,286.60, T2 2,353.20) — by design | session exits use the arms' rules (checked in 3.3) | OK |
| Breadth regime gate (Kite port) | Y — breadth_and_scan 07:30:03 (13 Nov: breadth50 56.79 %, breadth20 80.69 %) | 523 stocks | gates OPEN, as the picks show | OK (16 Nov value at 21:20) |
| Kite breakout-retest scanner | Y — 07:30:03 (as of 13 Nov: 495 scanned, 11 setups) | 495/499 (2 no candle, 2 < 25 candles) | CDSL resting buy = breakout low ₹1,408.10, stop ₹1,337.60 (5 %, tick-rounded) | OK (16 Nov scan at 21:20) |
| **Kite vs Dhan** | — | — | **Not possible: Zerodha is OFF at the owner's request (no Kite data).** Substituted Dhan vs NSE Bhavcopy: 13 Nov open/high/low identical (0.00 %) for 282 full-day stocks; close median 0.17 %, 29 > 0.5 %, max 1.39 % (TATACOMM) — NSE's close is the 15:00–15:30 VWAP, Dhan's is the last trade; EOD formulas use NSE's. 16 Nov opens identical for 493 | Info (CAP-17) |

### 3.2 Signal audit — entries

* Signals the day traded on (13 Nov scores): **0 BUY**, 368 HOLD, 131 WAIT → auto-pick rank-fill (SES-09).
* **GRASIM (S6, 09:25:37)** — HOLD 61.44, 5th of the ranked list after PVRINOX/INDHOTEL/PNB (held by S1/S2/S10) and
  IGIL (falling); LIMIT 3,045.80 = LTP 3,030.70 × 1.005 tick-down; filled 3,032.20; 4 sh = ₹12,143 (risk budget ₹13,284). ✔
* **KALYANKJIL (S8, 10:26:39)** — HOLD 61.41 (free after S6 sold it 09:24); LIMIT 700.45 = 13 Nov close × 1.005 (LTP
  712.05, +2.2 % < 3 % gap rule); not reached; cancelled by design at the next open. ✔
* **DLF (S9, 11:18:02)** — HOLD 60.67 after IGIL (−1.95 %) and LAURUSLABS (−5.5 %) were skipped as falling (≥ 1.5 %);
  BOSCHLTD (61.28) passed over — one share (≈ ₹49,390) is more than S9's ₹20,330 budget (inferred; not logged, CAP-15). ✔
* **CDSL (S4, 10:27:42)** — Kite retest rank 2 (KALYANKJIL rank 1 was held), DUMMY resting buy, valid to 1 Dec. ✔
* Trades with no matching signal: **none** (both fills have AUTO_PICK events). False triggers: **none**.

### 3.3 Signal audit — exits

| Position | Rule | Trigger | Fill (IST) | Verdict |
|---|---|---|---|---|
| S6 KALYANKJIL | trailing stop (high 727.30) | 720.00 | 717.50 at 09:24:55 (LTP 717.88) | on time ✔ |
| S8 ATHERENERG | trailing stop 1 % (high 2,018.00, last moved 10:03:45) | 1,997.80 | 1,983.20 at 10:26:08 (LTP 1,984.21) | **late**: no quotes 09:47–10:16 (Dhan outage), 0.73 % under the stop ≈ ₹175 (CAP-05) |
| S4 M&M | fixed stop 2 % | 3,328.18 | 3,321.50 at 10:27:11 | on time (0.2 % slip) ✔ |
| S9 SAILIFE | fixed stop 2 % | 1,882.74 | 1,879.50 at 11:17:31 | on time (0.17 % slip) ✔ |
| 8 open positions | stops / targets vs 16 Nov NSE high-low | — | — | **no missed exit**; closest AUROPHARMA low 1,783.00 vs stop 1,782.53 |
| Time exits | 15-day sale (from 27 Nov earliest), retest 20/40 days | — | — | none due ✔ |

### 3.4 Logs (app, scheduler, orders, websocket, Dhan; Kite is off) — 16 Nov

* 3,207 INFO / 78 WARNING / 54 ERROR in `atip.jsonl`.
* **09:47–10:16 network outage**: DNS failure, 33 Dhan REST errors (60 s read timeouts, aborted connections) and 22 "Quote data failed" warnings, Index WebSocket parked
  twice, Telegram alert failed after 3 attempts (09:58), yfinance 14 global symbols failed (10:01), "database is
  locked" (10:07 index flush, 10:15 cycle sync). Scheduler leader lease lost 09:47, 16:13, 16:37 (CAP-02).
* **dhan_15min_bars SKIPPED 11 times 10:30–15:30** ("locked: another runner holds this job") → bars stop at 10:15 (CAP-02/06).
* 06:00 FRED macro: 9 series timed out (macro_data PARTIAL ×4).
* **Silent fallbacks:** sentiment on keyword rules all day ("News AI off … rule-based", 0 AI calls; by config,
  CAP-11); `ta` library in place of pandas-ta (expected); 3 of 9 news feeds failing all day (CAP-10).
* **Stale data:** NSE index closes not published at 16:48 → benchmark stays at 13 Nov for the 16:49 scores (CAP-07);
  FII/DII not yet published → signals HELD for a re-score (by design).

---

## 4. Task 3 — Today vs the previous 5 trading sessions

| Session | Buys | Sales | Won | Win rate | Gross | Charges | Net realised | Equity change | BUY signals available | Utilisation (cost / equity) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 6 Nov | 5 | 4 | 1 | 25.0 % | −825.68 | 282.17 | −1,107.85 | −771.02 | 0 | 41.8 % |
| 9 Nov | 7 | 8 | 5 | 62.5 % | +2,991.64 | 547.90 | +2,443.74 | +2,096.92 | 0 | 38.8 % |
| 11 Nov | 4 | 3 | 1 | 33.3 % | −374.37 | 213.04 | −587.41 | −903.45 | 0 | 43.9 % |
| 12 Nov | 3 | 2 | 2 | 100 % | +1,378.32 | 146.03 | +1,232.29 | +3.99 | 0 | 48.5 % |
| 13 Nov | 7 | 7 | 3 | 42.9 % | −292.73 | 496.99 | −789.72 | −1,525.76 | 0 | 48.4 % |
| **16 Nov** | **2** | **4** | **2** | **50.0 %** | **+212.13** | **278.14** | **−66.01** | **+550.70** | **0** | **35.7 %** |
| 5-session average | 5.2 | 4.8 | 2.4 | 50.0 % | +575.44 | 337.23 | +238.21 | −219.86 | 0 | 44.3 % |

(10 Nov was an NSE holiday.) Today had the fewest buys of the six days and the lowest utilisation: two sessions sold
mid-morning and their next buys rested below the market, and the new risk-budget sizing (06:29) shrank today's buys.

## 5. Issues / bugs register

See `ISSUES_REGISTER.md` (18 issues: 0 Critical, 5 High, 5 Medium, 6 Low, 2 Info; 4 High fixed, 1 High partly fixed,
all in code on the branch, live after merge + restart).

## 6. Close-out

* **Mobile:** ATIP's Android and iOS apps are WebView shells around the server's screens, so the new "Top up
  sessions…" dialog, the "moved in" tiles and the exit-monitor alert reach them with no native change. Version
  bumped to **2026.11.16.2 (build 11)**; Android APK built on the Mac (see the testing report); iOS not built (no Xcode).
* **Tracker:** `ATIP_MASTER_TRACKER.xlsx` sheet "Capital review 2026-11-16" + master rows CAP-01..CAP-REVIEW; CSV.
* **Testing:** `docs/testing/ATIP_Capital_Review_Test_Report_2026-11-16.md` — full suite 2,779 passed / 32 failed / 65 skipped (17:19–17:29 IST); every failure also fails on untouched master at the same hour except the order-path guard, which passes once the branch is committed; new tests 14/14; Test server 21/21, Prod server 20/20.
