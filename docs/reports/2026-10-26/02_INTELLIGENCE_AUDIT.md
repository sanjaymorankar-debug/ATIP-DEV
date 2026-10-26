# ATIP — pre-market / post-market and news intelligence audit (2026-10-26)

*Evidence only: `pipeline_log` run records, table contents and timestamps in `atip_data/atip.db`, `atip.log`, and a
re-scoring experiment on copies of the database. The code was read only to know where to look.*

## Status table

| # | Capability | Status | Evidence (short) |
|---|---|---|---|
| 1 | News ingestion before market open | **PARTIAL** | Scheduled 07:00 (14 h window) + 08:20 catch-up + 12:00. Fetched before 09:15 on **1 of the last 10 trading days** (12 Oct, 07:00). 13–23 Oct ATIP was not running; 26 Oct it started at 18:47. 9 RSS feeds: 5 deliver, 3 have failed 21 times in a row (Business Standard ×2, Financial Express: malformed XML), MoneyControl returns 0 items. ~200–255 articles a day when running (231 on 26 Oct) |
| 2a | News understanding — AI sentiment | **MISSING** | 0 of 3,664 stored articles (26 Jul – 26 Oct) are AI-classified; every one is `classifier='rule'`. `ai_usage_log` is empty: no Claude call was ever attempted. `news_summary.fallback_reason` = "news.ai_enabled is false" (no `news` section in config.json; the Anthropic key returned 401 on 24 Sep, KD-001). The fallback is recorded, not silent, but the README/dashboard call it "AI-classified" |
| 2b | News → symbols / sectors | **PARTIAL** | ~55–60% of articles map to ≥ 1 symbol (aliases from the security master); no sector-level mapping (categories only). Of 18,928 scored rows, 88.4% carry a neutral news score; **0 of 76 logged BUY signals had any stock-specific news** on their signal date |
| 2c | Sentiment stored and fresh | **PARTIAL** | `news_symbol_score` 26 Oct: 83 symbols (18:49); 23 Oct: 10; 9 Oct: 233. Keyword sentiment is 0 for ~50% of headlines |
| 3 | 08:30 pre-market report with global cues + news | **PARTIAL** | The Morning Brief (Telegram + `/m`) went out at 08:30 on 12 Oct only; on 26 Oct an auto-recovery sent it at 18:49. It shows Market Health/regime, Nifty and VIX, a GIFT Nifty % (never captured, so blank), the global score and S&P, the previous evening's BUY/SELL list and Trade of the Day. It has **no** crude, USD-INR or FII/DII lines and **no news sentiment**, and nothing is re-scored in the morning |
| 4a | Pre-open session data (09:00–09:15) | **MISSING** | `preopen_snapshot` has 0 rows ever; `global_snapshot` 0 rows; no `preopen_capture` / `gift_preopen` / `global_sync_pre` run in `pipeline_log`. These W39 jobs (code of 7–10 Oct) never ran: the old Windows build had no W39 schedule (its 12 Oct log shows `broker_health` at 08:45 but no GIFT job), and the Mac has only run since 18:47 today. Due at 08:42–09:09 on 27 Oct |
| 4b | Post-market: Bhavcopy | **WORKING** (after repair) | 10/10 recent sessions now have 3,283–3,316 bars; 9 were missing until today's backfill |
| 4c | Delivery % | **WORKING** (after repair) | 3,060–3,094 symbols a session for all 10 sessions |
| 4d | F&O OI changes | **PARTIAL** | `fo_underlying_daily` 219 underlyings a session (OI, OI change, PCR, max pain, ATM IV, lot size) but only 16+ sessions of history; 26 Oct's first run was EMPTY (file not out at 18:48) |
| 4e | FII/DII | **PARTIAL** | Stored for 8, 9, 23, 26 Oct; NULL for the sessions ATIP missed — NSE's API serves only the latest day, so they cannot be recovered |
| 4f | Bulk/block deals | **PARTIAL** (fixed today) | Genuine rows for 9, 13, 23, 26 Oct; the downloader ignored the date and stamped the latest file onto past sessions — repaired and fixed (1ecda6b) |
| 4g | Participant OI | **WORKING since today** | First run ever 26 Oct 20:15, SUCCESS, 20 rows (21–26 Oct) |
| 5 | Is this analysis used to create trades? | **PARTIAL / GAP** | Post-market flows and global cues **do** change signals (below); news barely does; pre-market and pre-open inputs **never** enter the signal; no signal has ever become a strategy decision or an order (`strategy_decision` 0 rows, `risk_decision` 0, `oms_order` 0; 7 paper orders, last 17 Sep) |

## 1. News ingestion: the last 10 trading days

Earliest start time of each job per session (IST), from `pipeline_log` (`*` = a non-SUCCESS status that day):

| Job | 12 Oct | 13 | 14 | 15 | 16 | 19 | 21 | 22 | 23 | 26 Oct |
|---|---|---|---|---|---|---|---|---|---|---|
| news_premarket | 07:00 | — | — | — | — | — | — | — | — | — |
| news_catchup | — | — | — | — | — | — | — | — | — | 18:49 |
| news_midday | 12:00 | — | — | — | — | — | — | — | — | — |
| global_premarket | 07:00 | — | — | — | — | — | — | — | — | — |
| macro_data (07:15) | 07:15* | — | — | — | — | — | — | — | — | — |
| morning_digest (08:30) | 08:30 | — | — | — | — | — | — | — | — | 18:49 |
| preopen_capture (09:09) | — | — | — | — | — | — | — | — | — | — |
| gift_preopen (08:45/09:05) | — | — | — | — | — | — | — | — | — | — |
| bhavcopy | — | 20:17* | — | — | — | — | — | — | — | 18:47 |
| delivery | — | 20:17* | — | — | — | — | — | — | — | 18:49* |
| fii_dii_watch | — | — | — | — | — | — | — | — | — | 18:49 |
| ai_scoring | — | — | — | — | — | — | — | — | — | 18:49 |

(20 Oct was Dussehra. 23 Oct's post-market ran on Saturday 24 Oct at 22:40 as a catch-up.)

News stored per session (by publish date): 12 Oct 172, 13–22 Oct 0, 23 Oct 10, 26 Oct 231.

## 2. Pre-market and post-market data stored for the last 10 sessions (after today's repair)

| Item | 12 | 13 | 14 | 15 | 16 | 19 | 21 | 22 | 23 | 26 |
|---|---|---|---|---|---|---|---|---|---|---|
| Bhavcopy bars | 3294 | 3290 | 3296 | 3290 | 3291 | 3296 | 3309 | 3283 | 3313 | 3296 |
| Delivery % rows | 3066 | 3067 | 3071 | 3060 | 3063 | 3072 | 3087 | 3062 | 3094 | 3075 |
| F&O underlyings | 219 | 219 | 219 | 219 | 219 | 219 | 219 | 219 | 219 | 219 |
| Participant OI | 0 | 0 | 0 | 0 | 0 | 0 | 5 | 5 | 5 | 5 |
| Pre-open rows | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| ai_scores | 498 | 498 | 498 | 498 | 498 | 498 | 498 | 498 | 499 | 499 |

## 3. Tracing one real signal end to end: TTML, BUY on 26 Oct

| Stage | What ATIP has |
|---|---|
| News | No article mapped to TTML in the window; news score = the neutral default 50 |
| Sentiment | n/a for TTML; the market-wide MSI (63.96) includes the 24 h average news score |
| Scores | VPI 65.3, ZPI 55.6, MRI 71.1, RRI 76.1, CRI 49.4, ACS 69.5, INS 67.3, TS 63.8 → **ATIP 65.98, rank 1**; MH 56.74 (NEUTRAL) |
| Signal | BUY via the loose gate: ATIP ≥ 65 ✓, VPI ≥ 60 ✓, ZPI ≥ 55 ✓ (55.57), MH ≥ 40 ✓ |
| Logged | `signal_log` 26 Oct, entry ₹41.53, model_version 912e654, weights hash 245d9621d713 |
| Trade suggestion | Dashboard and phone "Buy candidates" (TTML, MRPL), tomorrow's 08:30 brief. Nothing further: no strategy decision, no position intent, no risk decision, no order |

## 4. Do news / pre-market / post-market inputs change the signal? (measured)

26 Oct was re-scored three times on copies of the database (`evidence/counterfactual_2026-10-26.json`):

| Variant | Signals changed (of 499) | BUYs | ATIP score change | MH / MSI |
|---|---|---|---|---|
| Baseline | — | 2 (TTML, MRPL) | — | 56.74 / 63.96 |
| News neutral (2,089 articles set to 50, 2,443 symbol scores removed) | **12** (all HOLD→WAIT) | 2 | mean 0.30, max 1.57 | 56.74 / 62.07 |
| FII/DII, global and bulk deals neutral | **54** (52 HOLD→WAIT, **2 BUY→HOLD**) | **0** | mean 1.95, max 2.25 | 54.26 / 57.98 |

So the evening flows and global cues decide this session's BUYs; keyword news moves scores slightly and never a BUY
here. Pre-market news (07:00), GIFT Nifty, the pre-open auction, participant OI and the market-pulse model are not read
by `scores/engine.py` at all, and scoring runs after the close — they are **displayed, never fed into the signal**
(gap GAP-2610-10).

## 5. P0 breaks found here and fixed (delta patches, re-verified)

| Break | Fix | Verified |
|---|---|---|
| Sessions without bars were never recovered, so indicators and scores ran across a 2-week hole | ac58625: catch-up treats them as holes; data backfilled | 0 missing sessions; sawtooth artefact gone; 4 new tests |
| The Dhan data outage was invisible (broker health "empty quote response"; exit manager NO_QUOTES unalerted; nothing on the phone) | 63767d8 | broker health now names the plan; mobile card visible on a phone viewport; 7 new tests |
| Bulk deals stamped onto the wrong sessions | 1ecda6b + data restored from the verified backup | 2 new tests; only genuine sessions remain |

**Not fixable in code — owner actions:** subscribe to Dhan's Data APIs; keep the Mac awake and ATIP running on trading
days (pre-market jobs need it running by 07:00); add a working Anthropic key and `"news": {"ai_enabled": true}` if AI
sentiment is wanted; replace the 3 dead RSS feeds.
