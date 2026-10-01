# W28 — Signals, news intelligence and dashboards: handoff

**Branch:** `w28-signals-news-dashboards` (worktree `D:\Projects\ATIP-dev-w28`), branched from `769383c`
(W26 dashboard fixes + MySQL export, on top of W25).
**Built in parallel with W27** (`w27-data-scores`, worktree `D:\Projects\ATIP-dev`), which another session was
developing at the same time. The W28 code avoids W27's files where it can (see *Merge notes*).
**Status:** developed, compiled (`py_compile`). **Not run, not tested.** Independent validation (ChatGPT) is pending.
Not merged to master and not deployed.

## Features

| ID | Feature | What was built |
|---|---|---|
| SG-08 | Intraday scans | `scores/intraday_scan.py` scans the session's **stored** 15-min bars for every tracked stock and writes hits to `intraday_scan_hit`. It runs after each 30-min bar fetch and again at 14:45. Optional Telegram alert when a hit agrees with the stock's BUY or SELL signal. |
| DB-11 | Crash Risk + Top 25 SPI / MSI | `/insights` → Crash risk / SPI / MSI tab. Lists the top 25 by CRI with the 3 largest CRI contributions (`score_components`) and the change since the previous session, and the top 25 by SPI. MSI is shown as a gauge with 120-day history. |
| DB-16 | Strategy performance panel | `strategy_engine/performance.py` computes per-strategy-version metrics, stored post-market in `strategy_performance` for 20, 60 and 250-day windows. The panel includes an equity-curve chart. |
| SE-05 | AI-driven strategies | `ml/ai_strategies.py` has a readiness check and an owner activation step (`POST /api/insights/ai-strategies/activate`). |
| NS-02 | News classification | `data/news_ai.py`: a 29-type event taxonomy, batched Claude classification with a daily cost cap, and a lexicon fallback. |
| NS-03 | Sentiment + confidence | A finance lexicon with negation and intensifiers, scored on every article. Its confidence comes from the evidence. |
| NS-04 | AI news summary | `data/news_digest.py` writes premarket, midday and close digests to `news_digest`. |
| DB-06 | AI News Summary panel | A digest block on the main dashboard's News tab, plus the full view on `/insights`. |
| DB-18 | Quant / model panel | `/insights` → Models tab, with charts and tables of model and factor monitoring. |
| NS-05 | Novelty / decay / source weighting | Novelty, event half-lives, measured source weights and a per-symbol score in `news_symbol_score`. `scores/engine.get_ns` now reads it. |
| NS-06 | Earnings call / announcement NLP | `data/announcements.py` classifies NSE corporate announcements. Claude reads the PDFs of transcripts, results and presentations for tracked stocks. |

### Feature details

**SG-08 scans:**
- ORB up/down
- VWAP reclaim/loss
- Same-time-slot volume surge
- Power hour up/down
- Gap hold up/down

Each hit is an observation, not a signal: nothing here creates a strategy decision or an order.

**DB-16 metrics**, per strategy version:
- Forward outcome of each BUY/SELL decision at 5, 10 and 20 sessions: hit rate, mean return, and excess return over the Nifty 50.
- FIFO-matched paper-book P&L: realised, unrealised, win rate and fees.

**SE-05 readiness checks:**
- `ml.default_model` is set
- An ACTIVE version exists
- Predictions are out of sample
- Predictions are fresh
- `ml.enabled` is true
- Decay health is OK

Activation does three things:
1. Writes `ml.default_model` and `ml.enabled=true` to `config.json`, keeping a timestamped backup.
2. Moves `ml_direction` and `atip_ml_blend` to **PAPER**, but only where the lifecycle allows that step.
3. Never moves anything to READY or ACTIVE, and never touches live trading.

**NS-02 classification:**
- Headlines go to Claude in batches of 20, one request per batch.
- Calls use the system prompt (cached), structured JSON output, and the server-side refusal fallback (`fallbacks="default"`).
- `news_ai_usage` keeps a daily ledger of calls, tokens and estimated USD. When the day's spend reaches `news_ai.daily_budget_usd`, the remaining articles stay lexicon-classified.
- Only unseen articles are classified. The old per-article loop is gone, and so is the stale `claude-sonnet-5` model id.

**NS-03 confidence:** confidence comes from how many sentiment terms matched and how one-sided they were. When Claude and the lexicon strongly disagree, the stored confidence drops by 0.25. Articles labelled only by the lexicon get `classifier='lexicon'` and stay out of ACS NewsConfidence, the same treatment the old `rule` fallback had.

**NS-04 digest:** written by Claude when `news_ai.enabled` is on and the day's budget allows. Otherwise it is extractive. The `method` column records which, and the panel shows it.

**NS-05 weighting:**
- Novelty: word-shingle Jaccard similarity against the last 72 hours. A similarity of 0.6 or more sets `dup_of`.
- Event-type half-lives.
- Measured source weights: duplicate share, and the next-session reaction hit rate once a source has at least 20 reactions.
- A per-symbol, decayed, shrunk score in `news_symbol_score`. `scores/engine.get_ns` reads it, falling back to the old 7-day average and then to 50.

**NS-06 document analysis:** Claude reports tone, guidance direction, key points, risks and stated figures. This shares the news budget, at no more than 10 documents per run.

## New and changed files

**New:**
- `db/schema_w28.py` — 7 tables and the `news_articles` columns
- `data/news_ai.py`
- `data/news_digest.py`
- `data/announcements.py`
- `scores/intraday_scan.py`
- `strategy_engine/performance.py`
- `ml/ai_strategies.py`
- `dashboard/insights_routes.py`
- `dashboard/insights_page.py`

**Changed:**

| File | Change |
|---|---|
| `db/schema.py` | Applies the W28 tables in a separate loop |
| `data/news.py` | Classify → enrich → store; unseen-only classification; new columns stored |
| `scores/engine.py` | `get_ns` |
| `pipeline/scheduler.py` | Digest (premarket, midday, close), announcements (midday, EOD), intraday scan (30-min and 14:45), news weights before scoring, strategy performance after strategy health |
| `dashboard/server.py` | `/insights` registered, nav link, digest block |
| `enterprise/authz.py` | Permissions for `/api/insights` |
| `db/purge.py` | Retention for the new tables |
| `config_template.json` | `news_ai` and `intraday_scan` sections |

**New tables:**
- `news_ai_usage`
- `news_source_quality`
- `news_symbol_score`
- `news_digest`
- `corporate_announcement`
- `intraday_scan_hit`
- `strategy_performance`

**Additive columns on `news_articles`:**
- `event_type`
- `novelty`
- `dup_of`
- `source_weight`
- `half_life_h`
- `sentiment_lex`
- `lex_confidence`
- `model`
- `entities_json`
- `published_at`

## Defaults (nothing new turns on by itself)

- `news_ai.enabled = false`. With it off, nothing is sent to Anthropic, the lexicon classifies everything, and digests are extractive. Turning it on also needs `ANTHROPIC_API_KEY`. Default model is `claude-opus-5-5` at effort `low`, with a budget of $1.00/day.
- `intraday_scan.alerts = false`.
- SE-05 activation happens only through the token-guarded API, run by the owner.
- **Behaviour change once merged:** `get_ns` returns the NS-05 weighted score instead of the plain 7-day average of `news_score`. This feeds VPI, MRI, RRI, CRI and ZPI, so scores move once it is merged.

## For QA (ChatGPT)

1. **Schema:** run `python -c "from db.schema import init_db; init_db()"` on a copy of the DB. Expect 7 tables and the 10 `news_articles` columns.
2. **News:** run `python -m data.news --hours 12` with `news_ai` off. Rows get `classifier='lexicon'`, `event_type`, `novelty`, `source_weight` and `half_life_h`. Repeats get `dup_of`.
3. **Lexicon:** `python -m data.news_ai --test "Tata Steel Q2 profit jumps 40%, beats estimates"` should give positive sentiment and `EARNINGS_BEAT`. Also check negation: "fails to beat".
4. **Cost cap:** with `news_ai.enabled` and the budget set to 0.0001, one batch runs and then everything falls back to the lexicon. The `news_ai_usage` row is recorded.
5. **Symbol scores:** `python -m data.news_ai --sources --scores`. Check that a stock with one weak article stays near 50 (shrinkage) and that duplicates are excluded.
6. **Digest:** `python -m data.news_digest --session close --extractive`, then check the main dashboard News tab and `/insights`.
7. **Announcements:** `python -m data.announcements --days 2 --no-deep`. **Verify NSE's field names live** (`desc`, `attchmntText`, `attchmntFile`, `an_dt`, `seq_id`). They were written from the documented feed and have not been confirmed on this machine.
8. **Scans:** `python -m scores.intraday_scan --date <a session with bars>`. Check ORB and VWAP against a chart, and that VOLUME_SURGE needs at least 3 prior sessions of the same slot.
9. **Strategy performance:** `POST /api/insights/strategies/refresh`. Recompute the forward returns of a few decisions by hand.
10. **SE-05:** `GET /api/insights/ai-strategies` should report not ready with no model. Activate with a model that has no ACTIVE version: expect 400.
11. **Authz** (`enterprise.enabled`): a viewer can GET `/api/insights/*`. Activate needs `ml:lifecycle`.

## Merge notes (W27 ↔ W28)

- **`db/schema.py`:** W28 adds a separate loop *after* the `W3_W4_COLUMNS` loop, and W27 edits the table-dict line above it. Expect an automatic merge, or at worst a trivial one.
- **`scores/engine.py`:** W28 changes only `get_ns`. W27 rewrites `get_fund`, `compute_ins`, `compute_msi` and the scoring loop. `get_ns` sits right after `get_inst`, so check that hunk.
- **`pipeline/scheduler.py`:** W28 inserts a news-weights step just before `ai_scoring_engine`. If W27 adds fundamentals or institutional steps there, keep both.
- **DB-11 SPI list:** stays empty until W27 populates SPI. The panel says so.

## Not done / follow-ups

- **NS-03:** the lexicon confidence is evidence-based but not *calibrated*. Calibrate it against next-session reactions once `news_source_quality` has history.
- **NS-06:** documents are only analysed for tracked stocks. Announcement tone is not fed into any score. That is a research decision (`quant/events.py` can read the table).
- **SE-05:** needs a trained model promoted to ACTIVE. That is an owner action on `/ml`.
