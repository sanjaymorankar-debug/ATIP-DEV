# News Impact Estimate (NI-01..NI-05, 2026-10-27)

ATIP now reads the Indian and world news feeds itself and estimates how that news is likely to move the Indian market
in the coming NSE session. It also keeps a running record of how often that estimate was right.

**Informational only.** It feeds no ATIP score, and it never places, sizes or blocks an order.

## What it does

1. **Fetch.** It reads the 9 Indian feeds and the 3 world feeds through the existing news pipeline. The world feeds are
   Wall Street and US yields, Asian markets, and Pakistan. The pipeline de-duplicates, classifies and stores each
   headline once.
2. **Assess** each new HIGH or MEDIUM headline from the last 18 hours. It skips repeats of the same story and market
   recaps ("Sensex jumps 483 points" reports a move, it does not cause one). For each headline it records:
   - Nifty direction: up, down or none.
   - Size: none, small, medium or large.
   - Horizon: intraday, next session or several days.
   - Channel: FII flows, crude, rupee, rates, global risk, tariffs, geopolitics, policy, sector or company.
   - Affected sectors, across the 11 NSE indices ATIP stores.
   - Relevance to India and confidence.
   - A one-line reason.
3. **Combine** the headlines into one call for the session:
   - a score from 0 to 100,
   - a bias of POSITIVE, NEUTRAL or NEGATIVE, with a strength,
   - a score for each sector,
   - the top drivers,
   - a short brief.
4. **Track** each call against the Nifty 50 close of its session (the 15:30 row in `index_levels`). Sector calls and
   single headlines are checked the same way.

## Who does the analysis

- **Claude.** When `news.ai_enabled` is true and `ANTHROPIC_API_KEY` works, Claude Haiku 4.5 does the analysis. It
  shares the news daily cost cap ($1 by default) and is logged in `ai_usage_log` as `news_impact` and
  `news_impact_brief`. Expect a few cents a day.
- **Keyword rules.** Otherwise keyword rules take over, and the page says so in yellow. The rules handle crude, yield
  and dollar moves the India way round: "crude jumps" reads as negative, "oil decline" as positive. They keep
  single-company news off the Nifty, never call a move "large", and cap confidence at 0.5. The rules are a fallback and
  are clearly weaker than Claude.

## When it runs

- **06:00** resume (step 3, after the news summary): this is the overnight read for the day.
- **07:40** pre-market, before the 08:15 pre-market report and the 08:30 brief.
- **12:20** midday.
- **15:50** after the close. This one, and the 20:15 run, target the next session.
- **20:15** evening, before the 21:00 stop.
- **16:20** scoring: each call whose session has closed is checked against the market.

All of these are skipped during the 21:00–06:00 stop window, like every other job.

## Where it shows

- **Dashboard (`/`):** a panel on top of the **News** tab and the **Global & Asia** tab.
- **`/global-asia`:** the panel sits on top of the page.
- **Phone (`/m`):** on the Global & Asia screen, which the Android and iOS apps also load.
- **Pre-market report:** a `news_impact` section, plus a caveat when there is no estimate for today.
- **Telegram morning brief:** a "News impact" line.
- **API:**
  - `GET /api/news-impact`
  - `GET /api/news-impact/accuracy?days=90`
  - `POST /api/news-impact/refresh`, which needs the owner token and is refused while ATIP is stopped.

## Reading the track record

The page shows the hit rate of the last call made before the open in each session. It sits next to two naive
baselines:

- always predicting the most common outcome;
- predicting that today repeats yesterday's direction.

A NEUTRAL call counts as right when the Nifty moves by 0.30% or less. Until there are 20 or more sessions, the page says
"too few sessions to judge yet". Trust the estimate only if it beats both baselines.

## Configuration (`atip_data/config.json`, all optional)

```json
"news_impact": {
  "enabled": true, "window_hours": 18, "max_items": 40, "batch_size": 20,
  "fetch_feeds": true, "fetch_hours": 6, "brief": true,
  "half_life_hours": 12, "score_scale": 4.0, "positive_at": 58, "negative_at": 42,
  "neutral_band_pct": 0.30,
  "run_times_ist": {"premarket": "07:40", "midday": "12:20", "close": "15:50", "evening": "20:15"},
  "evaluate_time_ist": "16:20"
}
```

## Manual run

```bash
.venv/bin/python -m data.news_impact                # fetch, assess, combine, score
.venv/bin/python -m data.news_impact --no-fetch     # use the headlines already stored
.venv/bin/python -m data.news_impact --accuracy     # print the track record
```

## Storage

| Table | What it holds | Retention |
|---|---|---|
| `news_impact` | per-headline assessments | 90 days |
| `news_impact_daily` | session calls | 600 days |
| `news_impact_eval` | the track record | 600 days |

All three are classified as research data, not personal data.
