# ATIP — status of all phases

*As of 2026-09-22 05:50 IST (first written 2026-09-21 16:19, updated after the Tier 0 price and index repairs). Every figure below was measured from the repository, the database or the live log at that time.*

This engagement ran in two parts. It began as a defect investigation — **why signals were not working** — and then became Phase 1 of the institutional-quant master plan. The defect work turned out to be a large share of the value: most of what was wrong with ATIP was not missing capability but existing capability quietly producing wrong numbers.

**22 commits, 23 files, +5,666 / −149 lines, all pushed to `origin/master` and deployed to `D:\Projects\ATIP`. Tests: 195 after the first fix, 304 now, all passing.**

---

## Part A — Defect remediation (complete)

### Root cause of "signals not working"

Post-market ran at **16:05**, but NSE publishes the CM Bhavcopy at **~16:33 IST**, and Dhan's daily history does not return day D's bar until D+1. So from 2026-09-10 every session was scored on the *previous* day's closes and stamped with today's date, and every signal was logged with `entry_price` NULL. `evaluate_outcomes()` skips NULL entries, so **no signal from 09-10 onward could ever be tracked**.

Four things made it worse: the 09-14 holiday was scored as a session (the calendar had no festival holidays); two post-markets never ran and nothing caught them up; Dhan refused every request on 09-16; and a scheduler that never retries a missed slot.

### Everything fixed, by area

| Area | Commit | What was wrong → what it does now |
|---|---|---|
| **Signal pipeline** | `a4a508b` | Post-market moved to 16:45; an EOD coverage guard refuses to score a date without its bars; 18:30 and startup catch-up; official 2026 NSE holidays; NULL entry prices backfilled from the signal date's close |
| | `cc8ce46` | Catch-up scanned one date only, so a skipped session was lost for good → scans the last 5 sessions oldest-first; `run_postmarket(target_date, backfill)` skips "as of now" steps for past sessions; Dhan history cache froze a window forever → 15-minute reuse for an incomplete window |
| **Broker API abuse** | `7d3ed18` | Index WebSocket never stopped: ~3,300 refused connections/hour for 24h (HTTP 429), 35 MB of log, 7,583 fabricated weekend index rows; an open dashboard tab fetched ~500 quotes every 15s all night → session window 09:00–15:45, backoff, snapshot cleared on disconnect, quotes served from cache when shut |
| | `b303d7a` | Dhan's market-status frame (`"Markets Open"`) crashed `_on_message` and would have parked a healthy feed at 09:15; a refused first connect killed the SDK thread silently → both handled, plus stall detection |
| **Data correctness** | `c57791e` | FII/DII flows stamped with the requested date → stored under NSE's own date |
| | `96fd1ba` | The session's own flows were never ingested (fetch sat behind an early return) → fetched on both paths; coverage guard's bar halved on each partial day (502→251→126) → measured against the tracked universe |
| | `d12b72e` | `re` never imported, so every Screener.in fundamentals fetch raised `NameError` → fixed |
| | `aca4845` | NSE's placeholder scrip DUMMYHEG was scored daily → excluded |
| | `3037d71` | No SQLite busy timeout (5s default) vs a ~50s scoring transaction → "database is locked" on the feed and a dead catch-up → 60s |
| **Scoring correctness** | `d74e595` | A re-score refreshed 12 of 22 columns, leaving regime/confidence stale and two Trades of the Day → full refresh, one TOD |
| | `a7d92a7` | **MACD histogram and signal line were swapped in all 7,164 rows** (`ta` and pandas_ta order columns differently), pinning MRI's heaviest input in 63.1% of rows → fixed |
| | `5a7ddff` | The MACD test asserted an identity that holds either way round, so it could not fail → now recomputes the signal line and catches the old ordering |
| | `bbd0540` | MACD mapped on an absolute ±5-rupee scale across prices from ₹7 to ₹134,860 → normalised by price, full scale 1.667% chosen from the measured distribution |
| **Price history** | `ba31981` | A session whose bars were byte copies of the previous one passed the coverage guard → refused when more than 5% of the tracked universe is identical |
| | `7132017` | The calendar had no 2025 holidays and rejected the two Budget weekend sessions → 13 NSE 2025 holidays and 2025-02-01 / 2026-02-01, each verified against NSE's archive |
| | `8526a9b` | The NIFTY50 re-sync refreshed close only, leaving 280 bars with close outside [low, high] → every price column refreshed |
| | `8dd04fb` | The benchmark ended one session before the stocks at every scoring run (Dhan's `to_date` is exclusive, and a session's index bar is not published until the next day), and relative strength paired rows by position → the session's close comes from NSE's daily index file, RS matches by date; `index_levels` (date, time) made unique |
| | `3135ea5` | A feed that stopped at midday still counted as covering the session → NSE's close is stored at 15:30 whenever the feed did not record the close |
| **Dashboard truth** | `db7f506` | Global panel always showed the day's *oldest* snapshot (S&P +1.14% shown, −0.17% true) — the "gold/S&P/USD-INR not updating" complaint; live CMP change used a baseline one session too old → both fixed |
| | `cc8ce46` | Hit rate counted resolved signals only (95.2% shown vs 52.6% measured) → denominator is every signal watched for at least one session |
| **Decisions you made** | `cc8ce46` | Fundamentals ingestion held off (`FUNDAMENTALS_ENABLED=False`) because `fundamental_score` is weighted twice (SPI 0.15 + FS 0.10) and defaults to a hardcoded 50.0 |

### Data repairs executed

| Repair | Rows |
|---|---|
| Bhavcopy backfilled for 09-10, 09-11, 09-18 | 3,263 / 3,258 / 3,263 |
| NULL entry prices filled from the signal date's close | 27 |
| Sessions backfilled and scored (bars present, never scored) | 09-11, 09-15 |
| Fabricated weekend index rows deleted | 7,583 |
| Placeholder-scrip scores deleted | 6 |
| Wrongly dated FII/DII rows deleted | 2 |
| Non-session scoring artefacts deleted (07-25, 08-30, 09-14) | 1,630 |
| Indicators and scores recomputed for every real session (MACD, twice) | 14 sessions |
| Holiday-dated copies of the next session's bars (pre-09-08 UTC date shift) deleted | 8,858 on 18 dates |
| Pre-listing copies of a new listing's first bar deleted | 22 symbols |
| NIFTY50 bars with close outside [low, high] reset to close-only | 280 |
| Indicators and scores recomputed on the repaired price history | 15 sessions |
| `index_levels`: duplicates / rows outside 09:00–15:45 deleted | 2,191 / 28,160 |
| Sessions given NSE's official index close (the feed never recorded it) | 7 |
| NIFTY50 benchmark row for 09-21 from NSE | 1 |
| Scores recomputed with each session's own index data and benchmark | 6 sessions |

Each repair was a dry run first, then one count-checked transaction after an integrity-checked backup (`atip.db.bak-before-shift-repair-20260921-2329`, `atip.db.bak-before-index-repair-20260922-0533`).

### Measured impact

| Measure | Before | Now |
|---|---|---|
| Signals being outcome-tracked | 21 | **59** |
| Hit rate shown at the 3% target | 95.2% (resolved only) | **46.6%** (27 / 58 watched) |
| WebSocket errors per hour outside the session | ~3,300 | **0** |
| MACD component pinned at 0 or 100 | 63.1% | **1.7%** |
| Historical BUY signals in `ai_scores` | 52 | **27** |
| Friday 2026-09-18 BUY list | 10 (stale-data run) | **2** — SYRMA, CPPLUS |
| Non-session dates in scoring tables | 3 | **0** |
| Bars that copy another session's bar | 8,858 | **0** |
| One-session price moves over 25% | 304 | **270** (not yet classified; expected to be mostly splits and bonuses, Tier 0 #3) |
| Sessions scored from another moment's index data | 5 | **0** |
| 2026-09-16 regime | BEAR, from 09-15's −1.195% | **NEUTRAL**, from its own +0.428% |
| Benchmark sessions behind the stocks when scored | 1, every day | **0** |

**Nearly half of every BUY ATIP ever produced was an artefact of the swapped MACD column.** `signal_log` was deliberately not rewritten — it remains the contemporaneous record of what ATIP said (67 rows), so its hit rates describe signals generated before the fix.

### Claims I corrected along the way

Stated plainly, because each was repeated before it was caught:

- **SELL was not unreachable.** I twice said the BEAR gate blocked it. The CRI-danger SELL branch runs *before* the regime gates. SELL has never fired because no row has ever met any SELL condition: max CRI 62.69 against a threshold of 75, min ATIP 37.74 against a floor of 30, and `cri>60 AND mri<40` has matched zero rows.
- **My first MACD regression test could not fail.** `macd == signal + hist` holds whichever way round the columns are.
- **The AI news key is not set**, rather than rejected with a 401 as I first said — sentiment has always been the rule-based fallback.
- **Dhan does not refuse index history for this account.** I said the benchmark stopped at 09-17 because of DH-901. That error was an expired token on the morning of 09-20 and has not recurred; the benchmark trails by a session because Dhan's `to_date` is exclusive and a session's daily bar is not out until the next day.
- **The first `index_levels` rule was wrong.** I planned to treat any row during market hours as "the feed covered the session". The dry run showed 09-09, whose last market-hours row was 12:00 and still held 09-08's close at 0.00%; deleting its evening rows would have made the engine read that. The rule is now "the feed recorded the close".

---

## Part B — The master plan's 14 phases

### Phase 1 — Repository audit and baseline: **complete**

| Deliverable | Where |
|---|---|
| Machine-extracted baseline, re-runnable | `docs/baseline/BASELINE.md`, `baseline.json`, `tools/baseline.py` |
| Gap analysis, 140 capabilities with `file:line` or row-count evidence | `docs/ATIP_ALPHA_GAP_ANALYSIS.md` |
| Adversarial correction pass | inside the gap analysis — overturned 9 claims |

Baseline: 57 Python files, 15,754 lines, 28 tables, 299,105 rows, 15 API routes, 11 scheduled jobs.

**Caveat:** subagents hit the account session limit three times. Wave 1 (five domains) was audited and adversarially checked by independent agents. Wave 2 (risk, execution, decision, safety, UI/API) I audited myself, verified against the code and database, but **no independent critique pass has run over wave 2**.

### Phases 2–14: **not started as phases**

The defect work closed a handful of gap items incidentally — the busy timeout, the feed lifecycle, the hit-rate definition, the MACD factor — but no phase-2+ capability has been built. Where each phase starts from:

| Phase | Domain in the gap analysis | Present | Partial | Absent | Where it starts from |
|---|---|---|---|---|---|
| 2 · Data & feature architecture | Data platform and quality | 0 | 5 | 8 | Price series integrity (Tier 0 below) must come first |
| 3 · Quant factor library | Feature engineering | 0 | 11 | 6 | A (date, symbol) × 38-factor panel **already exists** in `technical_indicators`; no registry, normalisation contract or versioning |
| 4 · Factor research & validation | Factor research | 0 | 2 | 8 | Rank ICs are computable today, but the panel is only ~14 sessions deep; first measured ICs were slightly negative (`atip_score` −0.049) |
| 5 · Regime engine | Regime and ML | 0 | 5 | 9 | Rule-based regime exists; `market_health` holds 14 rows; several inputs are fabricated constants |
| 6 · ML / AI engine | Regime and ML | — | — | — | No ML library is installed or used anywhere |
| 7 · Advanced backtesting | Backtesting | 2 | 8 | 16 | Bar-based backtest exists over 502 symbols; no walk-forward, Monte Carlo, run records or seeding |
| 8 · Portfolio & risk engine | Risk and portfolio | 1 | 6 | 9 | Portfolio Health and CRI exist; no covariance, VaR, ES or optimiser; sizing is orphaned |
| 9 · Execution engine | Execution and microstructure | 1 | 4 | 6 | Paper broker works; no bid/ask, depth or measured slippage anywhere |
| 10 · Dashboard | Dashboard, API, docs, tests | 1 | 7 | 5 | Daily workflow is covered; no quant, risk or model panels |
| 11 · Testing | (same) | | | | 270 passing, but no test touches `scores/engine.py`, `data/technical.py` or `orders/rules.py` (945 lines, places orders) |
| 12 · Performance | not assessed | | | | Nothing profiled yet |
| 13 · Paper-trading validation | Execution / safety | | | | Blocked on pre-trade risk checks |
| 14 · Production readiness | Monitoring, safety, security | 2 | 6 | 3 | Live trading correctly gated; no kill switch, no limits, open API |

---

## Part C — What remains, in the order it should be done

### Tier 0 — Price series integrity (blocks any trustworthy research)

Research built on these would be measuring the data's defects rather than the market.

1. ~~8,845 stale duplicate bars~~ **Fixed.** They were not stale fetches: before `70269e4` Dhan's IST-midnight candles were read as UTC, filing each bar one day early, and a bar filed under a day with no session was never overwritten. 8,858 copies on 18 holiday dates and 22 pre-listing copies removed; the freshness guard (`ba31981`) now refuses such a session.
2. ~~4,993 rows on 10 non-trading dates~~ **Fixed** with 1 — they were the same copies. The calendar now has 2025 (`7132017`).
3. **No adjusted prices — open.** `adj_close` is NULL everywhere and 270 single-session moves exceed 25% (304 before the repair; HEG's 2.8× spikes were copies and are gone). Needs NSE corporate-actions data, and changes every affected stock's indicators, so the design comes to you first.
4. ~~The NIFTY50 benchmark is corrupt~~ **Fixed.** 280 impossible bars reset (`8526a9b`); each session's close now comes from NSE (`8dd04fb`); every stored close checked matches NSE on 8 sessions.
5. **Delivery data never stored — open.** `delivery_qty` / `delivery_pct` are NULL in every row; the UDiFF CM Bhavcopy ATIP downloads has no delivery columns (header checked). Populating them shifts the INS score, so it needs your decision.
6. ~~`index_levels` leftovers~~ **Fixed.** Duplicates and out-of-hours rows removed, (date, time) unique, 7 sessions given NSE's close, 6 sessions re-scored (`8dd04fb`, `3135ea5`).

### Tier 1 — Safety (mandatory in the master plan before paper validation)

1. **No kill switch exists anywhere.** The cheapest mandatory item: one config flag, checked at the top of both order paths.
2. **Pre-trade checks are a funds check only** — no daily loss, position, exposure, order-value, order-count, drawdown, sector or leverage limit.
3. **15 unauthenticated API routes on `0.0.0.0`**, including `POST /api/orders` and `POST /api/orders/{id}/confirm`. Bind to localhost and require a token on mutating routes.
4. **Risk-based sizing is orphaned** — `position_size_pct` is computed and read by nothing; orders are sized by each rule's own fixed quantity.

### Tier 2 — Monitoring (the platform cannot currently report its own health)

1. **Nothing notices a job that stops** — the news job has logged nothing since 09-10, eleven days, and no surface reported it.
2. 93 `pipeline_log` rows say SUCCESS with zero rows processed; `log_job` swallows every exception and never records an end time.
3. **Two clocks in one database** — IST `start_time` beside UTC `created_at` in the same row (5h30m apart); news recency is inflated by 5.5 hours.
4. `atip.log` has no rotation; Telegram is unconfigured, so every alert and the morning brief go nowhere.

### Tier 3 — Scoring honesty (before any recalibration)

1. **Four weighted inputs are frozen constants** — Breadth 25.93, PCR 61.54, VIX 15, NewsConfidence 50 — from `or`-default idioms at about six call sites. Tuning before fixing these is tuning against constants.
2. SPI and FS double-weight `fundamental_score` (ingestion held off pending your decision).
3. SELL thresholds have never been met; a decision on them needs a backtest.
4. VWAP is weighted 0.08 in `weight_config` and has never been computed.
5. The 15-minute bars job fetches ~500 symbols every 30 minutes and stores nothing.
6. `config.json`'s `schedule_*` keys are read by no code; `setup.py` lists the uninstallable pandas-ta and omits `ta`, so a fresh install computes no indicators.
7. **No formula registry yet** — the MACD and hit-rate formulas changed today with your approval, recorded only in git. `ai_scores` history now reflects the new MACD while `signal_log` rows were produced under the old one.

### Then — Phases 2 to 14 as specified

With Tiers 0–3 done, Phase 2 starts from clean data and working monitoring, Phase 3 starts from the factor panel that already exists, and Phase 4's IC machinery measures the market rather than the data's defects.

---

## Status in the master plan's required format

```
ATIP QUANT PLATFORM

Existing ATIP capabilities preserved: YES
  (nothing removed; two behaviour changes made with explicit approval —
   fundamentals ingestion held off, hit-rate denominator changed)
New quantitative capabilities: 0
New factors: 0   (1 existing factor corrected and normalised: MACD)
New formulas: 0  (2 corrected with approval: MACD component, hit rate)
ML models: 0
Regime models: 0 new (existing rule-based market-health regime)
Risk models: 0 new
Execution algorithms: 0
Backtest capabilities: 0 new

Tests:
Passed: 304
Failed: 0
Blocked: 0

Research status: NOT READY
  unadjusted prices and no delivery data (Tier 0); factor panel ~15 sessions deep;
  no IC, decay or walk-forward machinery
Paper trading: NOT READY
  the paper broker works, but there are no pre-trade risk limits
  and no kill switch
Live trading: DISABLED
  PAPER by default; LIVE requires config AND an explicit confirm

Known limitations:
  unadjusted prices; four frozen scoring inputs;
  news sentiment rule-based only

Missing data dependencies:
  corporate actions; bid/ask and depth; tick data (feed exists, never run);
  intraday bars (fetched, never stored); delivery data; working AI key

Production blockers:
  no kill switch; no pre-trade limits; unauthenticated order routes on
  0.0.0.0; no job-health monitoring
```

---

## Current state

- **Live** at `D:\Projects\ATIP`, running `3135ea5`, restarted 05:34:25 IST on 2026-09-22 with no migration warnings and nothing to catch up.
- **`index_levels`:** 12,769 rows, 0 duplicates, 0 outside 09:00–15:45, the unique index in place, and every stored session holds its close.
- **NIFTY50 benchmark:** through 2026-09-21 (from NSE). The shipped job was run once against the live database: it stored nothing new for 09-21 and correctly skipped a weekend.
- **16:45 today** is the first post-market run that fetches the session's own NSE close before scoring.
- **Disk:** D: has 6.1 GB free; the two repair backups take 63 MB each.
- The local repo carries one unpushed commit from another session, `4e864ef` (the bkesari snapshot publisher), left alone deliberately.
