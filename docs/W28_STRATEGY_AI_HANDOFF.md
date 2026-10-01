# W28: Strategy and AI handoff

**Status:** IMPLEMENTED BUT NOT VERIFIED. The tree compiles, and every inline dashboard script passes `node --check`. Smoke runs used a scratch copy of the production database:
- intraday scans at 12:30 and 14:45 on 2026-10-01 data;
- rule-path news classification and summary;
- one real Anthropic request (returned 401, see below);
- every new endpoint and the 4 changed pages through FastAPI TestClient.

ChatGPT testing is pending.
**Branch:** `w28-strategy-ai`, from `w27-data-scores`. Not merged, not deployed.
**Defaults:** news AI is off (`news.ai_enabled` false). Intraday scans are on, but they only read data and send a Telegram digest.

## Owner decision applied (2026-10-01)

News AI uses Claude Haiku 4.5 (`claude-haiku-4-5`, $1 / $5 per million input / output tokens) under a daily cost cap, with a rule fallback.

## ⚠ Before enabling news AI

The `ANTHROPIC_API_KEY` in `D:\Projects\ATIP\.env` still returns **401** (KD-001), re-checked today. Put a working key there, then set:

```json
"news": {"ai_enabled": true, "daily_cost_cap_usd": 1.0, "batch_size": 20}
```

The failed request is in `ai_usage_log` (ok = 0). Expected cost is roughly:
- **Classification:** 20 headlines a request, about 1.5k input + 2.5k output tokens, about $0.014 a request.
- **Daily total:** about 100–150 headlines a day comes to well under $0.20 a day.

## Delivered

| ID | Feature | Where |
|---|---|---|
| NS-02 | News classification | `data/news_ai.classify`. See "News AI" below. `data/news.classify_with_claude` delegates here; the old per-headline Sonnet call is kept unused as `_classify_with_claude_legacy` |
| NS-03 | Sentiment + confidence | **AI path:** the model's sentiment (−1..1) and confidence (0..1), `classifier='claude'`, which is what ACS's NewsConfidence reads. **Rule path, rebuilt:** see "News AI" below |
| NS-04 | AI news summary | `data/news_ai.market_summary`. See "News AI" below |
| DB-06 | AI News Summary panel | Card at the top of the dashboard **News** tab: AI or "rule-based — not AI", tone, bullets, risks, clickable stocks in focus, today's AI spend vs the cap |
| SG-08 | Intraday scans | `strategy/intraday_scan.py`. See "Intraday scans" below |
| DB-11 | Crash-risk + Top-25 lists | Dashboard tab **Lists & scans**. See "Lists & scans tab" below |
| DB-16 | Strategy performance panel | `/strategies` Performance. See "Strategy performance" below |
| SE-05 | AI-driven strategies | `ml/ai_strategy.py`. See "AI-driven strategies" below |
| DB-18 | Quant / model panel | `/ml` Model monitoring: max / mean feature PSI and features-shifted over time, realised metric series, health-check history (`GET /api/ml/monitoring-series`) |

### News AI (NS-02 / NS-03 / NS-04)

- **Classification:** Haiku 4.5 classifies **20 headlines per request** with a JSON-schema structured output: sentiment, confidence, importance, one of 17 categories, NSE symbols (filtered to the tracked universe), and a rationale.
- **Cost cap:** every request's tokens and cost go to `ai_usage_log`. At `news.daily_cost_cap_usd` the rest of the day falls back to rules. A 401, rate limit or unparseable batch also falls back to rules.
- **Prompt caching:** not used. Haiku 4.5's minimum cacheable prefix is 4,096 tokens, so batching is the cost lever.
- **Rule path (NS-03):** a weighted lexicon with negation, keyword categories, and confidence **measured** from the evidence (it was a constant 0.5). The classifier stays `rule`, so ACS still ignores it.
- **Summary (NS-04):** one request over the window's headlines (default 24 h, up to 60) produces a headline, tone (RISK_ON / MIXED / RISK_OFF / QUIET), bullets citing headline numbers (mapped to article ids), key risks and stocks in focus.
  - Without AI it falls back to a rule-based summary, labelled as such.
  - It runs after the pre-market and midday news jobs. `GET/POST /api/news/summary`.

### Intraday scans (SG-08)

- **What changed:** the 12:30 job used to call `check_zpi_alerts(today)`, which reads today's `ai_scores`. Those rows only exist after 16:05, so it could never fire.
- **Scans now:** zpi_pullback, breakout (20-session high + relative volume), vwap_reclaim (session VWAP from the stored 15-min bars), momentum, and breakdown_risk.
- **Inputs:** live quotes plus today's **closed** 15-min bars, against the previous session's scores and levels.
- **Point in time:** a re-run at 12:30 uses only bars complete by 12:30 (verified: the latest bar used was 12:15), and the reclaim needs price above VWAP.
- **Schedule:** 10:30, 12:30 and 14:45 (`intraday_scans.times`).
- **Output:** `intraday_scan_hit`, plus one Telegram digest per run.

### Lists & scans tab (DB-11)

- **Crash risk 25:** CRI, change over 5 sessions, main CRI driver from `score_components`, and a 💼 flag on LIVE holdings.
- **Top SPI 25:** from ATIP scores when `fundamentals.score_enabled`, otherwise the NSE-fundamentals SPI. The source is shown.
- **MSI:** the series, plus its components. Components are now stored per run under symbol `__MARKET__`.
- **Intraday scans:** the latest run's hits.
- Rows open the W26 stock panel.

### Strategy performance (DB-16)

- Health and issues, the current version's backtest metrics, and decisions by action over 7 / 30 / 90 days (with the blocked count).
- PAPER and LIVE book P&L from W4 fills (average cost, crossing through zero handled; realised net of fees; unrealised at the latest close).
- Production has no runs yet (15 strategies, all DRAFT), so the books show "no fills".

### AI-driven strategies (SE-05)

- **Gate chain**, per strategy whose rules read an `ml_*` feature, showing the first blocking gate:
  1. `ml.enabled`
  2. `ml.default_model`
  3. model exists
  4. ACTIVE version
  5. walk-forward verdict not NO_EDGE
  6. fresh ACTIVE predictions
  7. completed backtest
  8. W4 `ml_model_active` risk gate
- **Activation:** `POST /api/strategies/{sid}/ml-activate` (strategy:lifecycle) moves a strategy to **PAPER** only when gates 1–7 pass. ACTIVE is allowed only from PAPER. Both go through `lifecycle.transition`.
- **Production today:** `ml_direction` and `atip_ml_blend` are blocked at gate 1.
- **Status:** stays IN PROGRESS. A model with demonstrated edge is the real blocker.

## Score change on deploy

`compute_msi` now records its components (`scores.engine.LAST_MSI`), stored under `__MARKET__`. This doesn't change the MSI value.

## Not built / limits

- **News AI** hasn't been exercised end to end (401 key). The JSON-schema path follows the documented SDK shape (`output_config.format`, anthropic 1.4.0).
- **Intraday scans** use only what has been fetched intraday: live quotes every 15 min (or the W27 stock feed) and 15-min bars. The thresholds are first guesses: tune them after a few sessions.
- **SE-05 / DB-16 / DB-18 panels** stay mostly empty until a strategy runs in PAPER and a model runs with `ml.enabled`.

## Files

- **New:** `data/news_ai.py`, `strategy/intraday_scan.py`, `scores/lists.py`, `strategy_engine/performance.py`, `ml/ai_strategy.py`, `dashboard/w28_routes.py`, `dashboard/w28_assets.py`
- **Changed:** `data/news.py`, `db/schema.py` (W28_TABLES: ai_usage_log, news_summary, intraday_scan_hit), `db/purge.py`, `scores/engine.py` (MSI components), `pipeline/scheduler.py` (scans, news summary), `enterprise/authz.py` (ml-activate → strategy:lifecycle), `dashboard/server.py`, `dashboard/strategy_page.py`, `dashboard/ml_page.py`
