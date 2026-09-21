# ATIP — status of all phases

*As of 2026-09-21 16:19 IST. Every figure below was measured from the repository, the database or the live log at that time.*

This engagement ran in two parts. It began as a defect investigation — **why signals were not working** — and then became Phase 1 of the institutional-quant master plan. The defect work turned out to be a large share of the value: most of what was wrong with ATIP was not missing capability but existing capability quietly producing wrong numbers.

**15 commits, 20 files, +4,769 / −131 lines, all pushed to `origin/master` and deployed to `D:\Projects\ATIP`. Tests: 195 after the first fix, 270 now, all passing.**

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

### Measured impact

| Measure | Before | Now |
|---|---|---|
| Signals being outcome-tracked | 21 | **58** |
| Hit rate shown at the 3% target | 95.2% (resolved only) | **48.8%** (21 / 43 watched) |
| WebSocket errors per hour outside the session | ~3,300 | **0** |
| MACD component pinned at 0 or 100 | 63.1% | **1.7%** |
| Historical BUY signals in `ai_scores` | 52 | **26** |
| Friday 2026-09-18 BUY list | 10 (stale-data run) | **3** — SYRMA, MEESHO, FINCABLES |
| Non-session dates in scoring tables | 3 | **0** |

**Nearly half of every BUY ATIP ever produced was an artefact of the swapped MACD column.** `signal_log` was deliberately not rewritten — it remains the contemporaneous record of what ATIP said (67 rows), so its hit rates describe signals generated before the fix.

### Claims I corrected along the way

Stated plainly, because each was repeated before it was caught:

- **SELL was not unreachable.** I twice said the BEAR gate blocked it. The CRI-danger SELL branch runs *before* the regime gates. SELL has never fired because no row has ever met any SELL condition: max CRI 62.69 against a threshold of 75, min ATIP 37.74 against a floor of 30, and `cri>60 AND mri<40` has matched zero rows.
- **My first MACD regression test could not fail.** `macd == signal + hist` holds whichever way round the columns are.
- **The AI news key is not set**, rather than rejected with a 401 as I first said — sentiment has always been the rule-based fallback.

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

1. **8,845 stale duplicate bars** — on the first session after every NSE holiday, ~500 symbols carry a bar byte-identical to the pre-holiday one (38 date-pairs). The coverage guard checks that a bar *exists*, not that it is *fresh*, so it passes these days.
2. **4,993 rows on 10 non-trading dates** in `prices_daily`, including Republic Day and two weekend dates.
3. **No adjusted prices.** `adj_close` is NULL in all 234,311 rows and 304 single-session moves exceed 25% — e.g. HEG 179.63 → 572.30 → 204.33 with two days sharing the identical volume. Every indicator, beta and backtest reads unadjusted prices.
4. **The NIFTY50 benchmark is corrupt** — 280 of 425 rows are impossible bars and OHL is shifted one session forward, so every `beta_1y` (and the dashboard's beta sort) is unreliable.
5. **Delivery data never stored** — `delivery_qty` / `delivery_pct` NULL in every row despite being parsed.
6. `index_levels` history still holds 30,706 out-of-window rows and 2,191 duplicates from before the fix (today's data is clean).

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
Passed: 270
Failed: 0
Blocked: 0

Research status: NOT READY
  price series integrity (Tier 0); factor panel ~14 sessions deep;
  no IC, decay or walk-forward machinery
Paper trading: NOT READY
  the paper broker works, but there are no pre-trade risk limits
  and no kill switch
Live trading: DISABLED
  PAPER by default; LIVE requires config AND an explicit confirm

Known limitations:
  unadjusted prices; stale post-holiday bars; corrupt benchmark;
  four frozen scoring inputs; news sentiment rule-based only

Missing data dependencies:
  corporate actions; bid/ask and depth; tick data (feed exists, never run);
  intraday bars (fetched, never stored); delivery data; working AI key;
  Dhan holdings and index history (DH-901 for this account)

Production blockers:
  no kill switch; no pre-trade limits; unauthenticated order routes on
  0.0.0.0; no job-health monitoring
```

---

## Current state

- **Live** at `D:\Projects\ATIP`, running `bbd0540`, restarted 10:05:14 IST.
- **Today's session:** 1,489 index rows 09:00:27 → 15:44:52, **0 outside the session window, 0 duplicates**; the feed closed on schedule at 15:45:29. Five WebSocket error lines, all network events: two handshake timeouts during an 11:34 hiccup (reconnected 11:38:35), two mid-session keepalive drops (recovered), and the SDK logging my scheduled 15:45 close. No lock errors, no tracebacks.
- **16:45 today** is the first scoring run with both MACD fixes end to end.
- The local repo carries one unpushed commit from another session, `4e864ef` (the bkesari snapshot publisher), left alone deliberately.
