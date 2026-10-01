# W28b — News weighting (NS-05) and announcement NLP (NS-06): handoff

**Branch:** `w28b-news-weighting` (worktree `D:\Projects\ATIP-dev-news`), on top of `w33-wealth-qa` (`7139d00`).

**Why it exists:** two sessions built a W28 in parallel. The line this branch sits on has W28 `d4dcd93`. The other build was `80daf6e` on `w28-signals-news-dashboards`; it covered the same nine feature IDs plus **NS-05** and **NS-06**. This branch carries over only those two, rebuilt on this line's W28.

That means it reuses this line's code rather than adding a second copy:
- `data/news_ai.py` categories, Haiku model path, `_call`, `ai_usage_log` and the `news.daily_cost_cap_usd` cap
- the W30 `market_event` store

Do not merge `w28-signals-news-dashboards` itself. It duplicates `data/news_ai.py`, `strategy_engine/performance.py` and the lists / scans / AI-strategy features.

**Status:** developed and compiled (`py_compile`). Not run, not tested, not merged, not deployed.

## NS-05 — novelty, decay, source weighting (`data/news_weighting.py`)

Before storing, each new article gets:

| Field | How it is set |
|---|---|
| `novelty` | 1 − its highest 2-word-shingle Jaccard similarity to articles from the last 72 h and earlier in the same batch |
| `dup_of` | The id of the matching article when similarity ≥ 0.6 |
| `half_life_h` | Set by the W28 category: EARNINGS 96 h, M_AND_A / REGULATORY_LEGAL 120 h, GLOBAL 18 h, FLOWS 24 h, … |
| `source_weight` | The source's measured weight, or its configured feed weight |

**Source quality** (`news_source_quality`, post-market):
- Per source: duplicate share, share of articles about a stock, and the next-session reaction hit rate for articles with |sentiment| ≥ 0.3.
- `weight = configured × (1 − 0.5 × duplicate share)`, multiplied by `(0.5 + hit rate)` once there are ≥ 20 reactions.
- The weight is clamped to 0.2–1.5.

**Per-stock score** (`news_symbol_score`, post-market, **before** `ai_scoring_engine`):

```
w     = importance × source_weight × novelty × confidence × 0.5^(age / half_life)
score = 50 + 50 × Σ(w × sentiment) / (Σw + 0.5)
```

- Covers the last 7 days up to the end of the scoring day.
- Duplicates are skipped.
- The +0.5 shrinkage keeps a single weak headline near neutral.

**`scores/engine.get_ns`:**
- Reads `news_symbol_score` first.
- If that has no row, it computes the score on the fly.
- With no news, it returns 50.
- It falls back to the old 7-day average only if the new columns are missing.

**Behaviour change:** news enters VPI, MRI, RRI, CRI and ZPI through `get_ns`, so those scores move once this is merged.

**Also changed in `data/news.py`:** only *unseen* articles (not stored in the last 3 days) are now sent to the classifier. Before this change, the overlapping 14 h and 5 h windows sent already-stored headlines to the paid model again.

## NS-06 — corporate announcements (`data/announcements.py`)

NSE `corporate-announcements` → `corporate_announcement`. Every row is classified from its subject by rules (event type, importance), and its tone and confidence come from the W28 rule lexicon (`classify_rule`). These rows get `classifier='rule'`.

**Document analysis:** when `news.ai_enabled` is on, the attached PDF is analysed for:
- event types EARNINGS_CALL, RESULTS, BOARD_OUTCOME and INVESTOR_MEET
- tracked stocks only
- at most 10 documents per run

The PDF goes through `news_ai._call` (purpose `announcement`, same model, same daily cap, logged in `ai_usage_log`). It returns:
- tone and confidence
- guidance (RAISED / LOWERED / MAINTAINED / INITIATED / NONE)
- key points, risks and stated figures

These rows get `classifier='claude'`, with the analysis in `nlp_json`.

**Research events:** `sync_events` writes material announcements to `market_event` as `ANNOUNCEMENT`, which was QR-09's pending type. Direction comes from tone (±0.2). `known_at` is the broadcast date, or the next day for anything after 15:30, matching EARNINGS.

**Scheduling:** runs at midday (`announcements_midday`) and in the evening job (`announcements_eod`), after results have landed.

Announcement tone feeds no score.

## Files

**New:**
- `db/schema_w28b.py`
- `data/news_weighting.py`
- `data/announcements.py`
- `dashboard/w28b_routes.py`
- `dashboard/w28b_assets.py`

**Changed:**

| File | Change |
|---|---|
| `db/schema.py` | Applies the W28b tables and columns |
| `data/news.py` | Unseen-only classification, enrich, new columns stored |
| `scores/engine.py` | `get_ns` |
| `pipeline/scheduler.py` | `news_weights` before scoring; announcements midday and EOD |
| `dashboard/server.py` | Registers the routes and assets |
| `enterprise/authz.py` | `POST /api/news/announcements/run` → `research:run` |
| `db/purge.py` | Retention for the new tables |
| `docs/ATIP_MASTER_TRACKER.csv` | NS-05 and NS-06 rows |
| `docs/WAVE_STATUS.md` | W28b row |

**Tables:**
- `news_source_quality`
- `news_symbol_score`
- `corporate_announcement`

**Columns added to `news_articles`:**
- `novelty`
- `dup_of`
- `source_weight`
- `half_life_h`
- `published_at`

**API:**
- `GET /api/news/symbol-scores`
- `GET /api/news/sources`
- `GET /api/news/announcements`
- `POST /api/news/announcements/run` (token)

**UI:** the News tab gets two cards, placed below the W28 market brief: *News weight by stock* (with source weights) and *Corporate announcements*.

## For QA

1. Run `init_db()` on a copy of the database. Expect the 3 tables and the 5 columns.
2. Run `python -m data.news --hours 12` twice. On the second run nothing should be re-classified (check `ai_usage_log` when AI is on). Repeated stories should get `dup_of`.
3. Run `python -m data.news_weighting --date <session>`. Check that a stock with one weak article stays close to 50 and that duplicates are excluded. Recompute one score by hand.
4. Score a session. `get_ns` should equal `news_symbol_score.score` for that session.
5. Run `python -m data.announcements --days 2 --no-deep`. **Verify NSE's field names live:** `symbol`, `sm_name`, `desc`, `attchmntText`, `attchmntFile`, `an_dt`, `seq_id`. Check that `market_event` gets ANNOUNCEMENT rows with `known_at` on the next day for anything after 15:30.
6. With `news.ai_enabled` and a working key, analyse one earnings-call transcript. Check that the cap applies and that the `announcement` purpose appears in `ai_usage_log`.
7. On the dashboard News tab, the two cards render and their rows open the stock panel.
