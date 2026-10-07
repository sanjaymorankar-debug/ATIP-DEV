# W39: history, research, options, screener, signals and market pulse handoff

**Branch:** `ccr-643d84fc-yig8ts` (PR #4, which also carries the Dhan token-refresh fix).

**Why this wave:**
- The owner's notes (`ATIP-.txt`): compare with Zerodha and Dhan, show stock history, store 7 years of data.
- `docs/ATIP_GAP_ANALYSIS_2026-10.md`: how ATIP compares with Dhan, Zerodha and institutional research.
- `docs/ANALYSIS_TOOLS_AND_SIGNALS_PLAN_2026-10.md`: how the leading FA / TA / AI tools work, the evidence on global cues, FII flows and order books, and the phased plan that the technical screener, signals and market pulse below start.

**Status:** developed. 91 W39 test cases and the full suite pass. The five pages (/research, /screener, /signals, /market-pulse, /options-builder) were rendered in a headless browser on seeded data; /options-builder and /market-pulse also at a 390 px phone width with no horizontal scroll. Not merged and not deployed. Nothing in this wave places an order; the open-orders view only reads.

## DP-11: 7 years of daily history

**Bug fixed:**
- **Before:** the weekly `db_purge` (`pipeline/scheduler.py`, `purge_old_data(dry_run=False)`) deleted `prices_daily`, `ai_scores`, `predictions`, `accuracy_tracker`, ownership and deal rows older than 600 days, without archiving them. ATIP's history starts on 2025-01-27, so the oldest prices were about to go.
- **Now:** `db/purge.py` has a **HISTORY** tier, 2557 days (7 years) by default, for those tables. Derived tables (indicators, components) keep 600 days. `retention_settings()` reads `config.json` `"retention": {"history_days", "long_days", "short_days"}`, where `history_days` 0 means keep forever.

**Backfill (`data/history_backfill.py`):**
- Walks each tracked symbol back from its earliest stored bar to today minus 7 years, one 365-day Dhan window at a time. Dhan serves daily candles from listing.
- Resumable: the earliest stored bar is the cursor, and progress is kept in table `prices_daily_backfill`.
- Listing-aware: an empty window, or bars starting more than 10 days into the window, marks the symbol `exhausted`.
- Refusals and exceptions are errors, never the listing date. After 3 consecutive failures the symbol is parked; `status` shows the reason and `run --symbols` retries it.
- Bars go through `data.dhan.store_daily_bars`, extracted from `run_historical_pipeline` so both use the same corporate-action basis.
- `fetch_historical_daily` now tags exceptions as `exception:<Type>` errors instead of returning an empty frame. So `run_historical_pipeline` counts a breaker-open or network failure as refused, not as "no bars", and a night where every fetch raised now reports FAILED instead of a clean run.

**Schedule:** nightly at 22:20, 40 symbols per run (`config.json` `"history": {"backfill_enabled", "years", "symbols_per_run", "chunk_days", "pause_seconds"}`). The Nifty 500 completes in about 2 weeks.

**CLI:**
- `python -m data.history_backfill status`
- `python -m data.history_backfill run [--symbols ...] [--max N]`

**API:**
- `GET /api/data/history/coverage`
- `POST /api/data/history/backfill`

**Needs:** the Dhan Data API subscription. Without it Dhan refuses with DH-902, and the symbols are parked after 3 runs.

## RS-01..RS-10: equity research (`research/`)

### `research/valuation.py` (pure, no database)

**Cost of equity:** CAPM, 7 % risk-free + beta × 5.5 % equity risk premium, beta clipped to 0.6–2.0.

**DCF:**
- Two-stage, 10 years, growth fading to 6 % terminal.
- Equity cash flow = E × (1 − g/ROE).
- Collapses exactly to Gordon growth when growth is flat (tested).
- Starting growth is the average of EPS, revenue and profit growth, capped at 25 % and at ROE.

**Scenarios:** bear / base / bull, weighted 25 / 50 / 25, plus a cost-of-equity × terminal-growth sensitivity grid.

**Financials** (NSE industry contains financial / bank / NBFC / insurance): justified P/B = (ROE − g)/(ke − g) instead of the DCF.

**Peer and own-history multiples:**
- Peers: industry-median P/E (P/B for financials), trimmed, 3+ peers.
- Own history: the stock's own P/E and P/B **at each quarter end**. The stored `pe_ratio` uses one current price for every quarter, so it is not used.

**Fair value and target:**
- Fair value = weighted blend: DCF or justified P/B 0.5, peers 0.3, own history 0.2, renormalised over the methods present.
- 12-month target = fair value × (1 + ke − dividend yield).

**Rating:**
- BUY when upside ≥ the hurdle for its uncertainty: LOW 10 %, MEDIUM 15 %, HIGH 20 %, VERY HIGH 30 %.
- ADD ≥ 5 %, REDUCE ≥ −5 %, SELL below that.
- NOT_RATED when no method applies.
- Uncertainty comes from the spread between methods and between scenarios, and from data completeness.

**Units:** `normalise_fundamentals` reconciles the three sources. XBRL stores growth in %, ROE as a fraction; Screener stores both in %; Alpha Vantage stores both as fractions.

**`quality_profile`:** a **moat proxy** (WIDE / NARROW / NONE) from ROCE level and stability plus leverage, explicitly labelled a proxy.

**Config:** every assumption can be overridden in `config.json` `"research": {"valuation": {...}}`.

### `research/report.py`

- **`Universe`:** latest fundamentals and prices for all symbols, read once per batch, plus the Nifty 500 industry map.
- **`gather`:**
  - Price statistics: 52-week range, 1-year return, 3/5/7-year CAGR from the new history, volatility, drawdown, 200-DMA.
  - Fundamentals and their history, peers, ATIP scores and signal.
  - Shareholding trend, insider trades over 90 days.
  - Upcoming events: `market_event`, corporate actions, board-meeting intimations.
  - Whether the operator holds the stock.
- **Thesis / risks / catalysts:** from explicit rules; every bullet names its number. No generative AI.
- **Disclosures** (modelled on the SEBI RA rules):
  - Automated model, no analyst review, no generative AI.
  - **Not a SEBI-registered RA; own research only.**
  - Rating definitions, horizon and method with assumptions.
  - Holding conflict.
  - Past hit rates are unverified.
- **`research_report` table:** one row per symbol per day. A row is a **call** when it is the first report, the rating changes, or the target moves more than 5 %.
- **`evaluate_targets`:** marks each call HIT (target touched within 365 days; upside calls use the high, downside calls the low) or MISSED at the horizon. Prices are adjusted with `corporate_actions.entry_factor`, so a split does not fake a miss.
- **`hit_rate`:** success rate and average return by rating.
- **Schedule:** daily at 20:40 on market days, after the evening scoring (`research.reports_enabled`, default on; DB only, no network).
- **CLI:** `python -m research report SYM | run | evaluate | hit-rate | ratings`.

**API:**
- `GET /api/research/equity[?rating]`
- `GET /api/research/equity/{sym}[?fresh=1]`
- `POST /api/research/equity/{sym}/refresh`
- `POST /api/research/equity/run`
- `GET /api/research/equity/{sym}/history`
- `GET /api/research/hit-rate`

**Page:** `/research`.

## SC-20: stock screener, fundamental + technical (`research/screener.py`)

**Snapshot:** one row per stock that has fundamentals and a price, about 50 fields:
- Valuation: price, market cap (price × shares / 10⁷), P/E, P/B, PEG, earnings / dividend / FCF yield.
- Profitability: ROE, ROCE, ROA, net and operating margin.
- Growth: revenue, profit and EPS, YoY and QoQ.
- Size and balance sheet: revenue, profit, EPS, book value, debt/equity, current ratio, interest cover, cash, FCF.
- Ownership: promoter %, its change over the latest quarter, pledge, FPI, MF.
- Price: 1-year return, 3-year CAGR (from the 7-year history), distance from the 52-week high / low.
- Technical (from `technical_snapshot`, see TA below): technical rating and label, RS rating 1–99, RSI, MACD histogram, ADX, Supertrend direction, ATR %, distance from the 50- and 200-DMA, Bollinger width, volume ÷ 20-day average, 3-month relative strength vs the Nifty, 1- and 3-month return, candle patterns, today's signals, and one `scan_<key>` 1/0 field for each of the 30 scans.
- Order book (from `order_book_pressure`, see OB below): `book_imbalance`, `book_pressure`, `book_persistent`.
- ATIP: score and signal.
- Research model: rating, upside, fair value, moat proxy, quality score.
- **Magic-formula rank:** Greenblatt's earnings-yield rank + ROCE rank, approximated with E/P; financials excluded.

**Universe:** stocks with fundamentals, plus stocks with a technical snapshot in the last 10 days, so a chart-only screen also covers stocks without fundamentals. 99 fields in all.

**Columns follow the query:** a technical-only query shows technical columns, a mixed one shows both, otherwise the fundamental set.

**Units:** percentages in %, `_cr` fields in ₹ crore.

**Cache:** the snapshot is cached for 10 minutes per database.

**Query language:**
- A small recursive-descent parser. No `eval` and no SQL from the query.
- `AND` binds tighter than `OR`; `NOT`, parentheses, `IN (...)` and `CONTAINS "text"` (case-insensitive substring, for `patterns` and `signals`) are supported.
- Text matching is case-insensitive.
- Field names and aliases (`ROCE`, `PE`, `mcap`, `debt_to_equity`, ...) are case-insensitive.
- A stock missing a field never matches a condition on it.
- Bad queries return a 400 that names the problem.

**28 presets, in three groups:**
- **Fundamental (12):** quality compounders, value, GARP, dividend, debt-free, promoters adding, undervalued by ATIP's model, strong near the 52-week high, turnaround, magic formula top 30, oversold quality, pledge risk.
- **Technical (13):** 52-week breakout on volume, golden cross, Supertrend buy, MACD bullish above the 200-DMA, RSI oversold reversal, Minervini trend template, squeeze fired, bullish candle at support, technical STRONG BUY, RS leaders (RS ≥ 80), pocket pivots, breakdowns, buyers queuing with a positive chart.
- **Combined (3):** quality stock breaking out, value stock turning up, model BUY in an up-trend.

**Saved screens:**
- Table `research_screen`, class OWNER.
- Name, query, sort, columns, notify flag, and the last matches. Each run reports **new** and **dropped** stocks.
- Job `saved_screens` runs at 20:50 on market days, after the research reports. It alerts (alert log + Telegram when configured) on new matches for screens with notify on.

**CLI:** `python -m research.screener "roce_pct > 20 AND debt_equity < 0.5" [--sort ...] [--csv]` or `--preset quality_compounders`.

**API:**
- `GET /api/screener/fields`
- `GET /api/screener/run`
- `GET /api/screener/run.csv`
- `GET` / `POST /api/screener/saved`
- `GET /api/screener/saved/{id}/run`
- `POST /api/screener/saved/{id}/delete`

Saving and deleting require `workspace:write` and the token; running is a read.

**Page:** `/screener` (now titled "Stock screener"): grouped presets, query box, condition builder, sortable results linking to `/research?symbol=...`, CSV, saved screens.

## OP-01..OP-05: options strategy builder (`quant/options_strategy.py`)

**Legs:** CE / PE / FUT, buy or sell, strike, expiry, lots, premium, IV.

**16 templates:**
- Long / short call and put
- Bull / bear call and put spreads
- Long / short straddle and strangle
- Iron condor and iron butterfly
- Call butterfly
- Covered call, protective put
- Call ratio spread

Strikes snap to the ATM on the symbol's strike grid (NIFTY 50, BANKNIFTY 100, otherwise from the stored chain or about 1 % of spot).

**`analyse`:**
- Payoff at the first expiry, and on a chosen earlier date (Black-Scholes for legs still alive).
- Breakevens.
- Max profit and max loss, with **unlimited** flagged from the slope beyond the strikes.
- Net premium (credit / debit), net Greeks (Δ units, Γ, Θ ₹/day, ν ₹/vol point).
- Probability of profit: lognormal at the legs' average IV.
- Reward : risk.
- A missing IV is implied from the premium.
- The chart range is ±4 standard deviations of the move to expiry (3–30 %).

**Pricing:** `chain_quotes` / `price_legs` fill premiums from the latest `option_chain_snapshot` (bid/ask mid, else LTP), then the F&O bhavcopy (settle / close). A leg with no stored quote is priced with Black-Scholes at 18 % IV, and its source says so.

**Not computed:** margin (SPAN + exposure). The page says to use the broker's calculator.

**API:**
- `GET /api/options/templates`
- `GET /api/options/chain/{sym}`
- `POST /api/options/build`
- `POST /api/options/analyse` (`research:run`; analysis only)

**Page:** `/options-builder`. The payoff chart uses validated categorical colours with a dashed target-date line, plus a legend, crosshair tooltip and table view.

## TA-01..TA-04: technical analysis and signals (`research/technicals.py`, `research/tech_signals.py`)

**Indicators** (`indicators(df, bench)`, pure pandas): SMA 20/50/200, EMA 9/21, Wilder RSI(14), MACD(12,26,9), Wilder ATR(14), ADX(14) with ±DI, Supertrend(10,3), Bollinger(20,2), Keltner(20, 1.5 ATR), Donchian 20/55, 52-week high/low (and the prior 52-week high, so today's bar can break it), volume ÷ 20-day average, 63-day relative strength vs the Nifty.

**Candle patterns** (`candles`, 19, each BULL / BEAR / NEUTRAL): engulfing, harami, marubozu, hammer and hanging man, inverted hammer and shooting star (trend-qualified), doji, morning and evening star, piercing line, dark cloud cover, three white soldiers, three black crows, inside bar, NR7.

**30 scans** (`SCANS`, completed daily bars only; 20 bullish, 9 bearish, 1 neutral):
- Crosses, firing on the crossing day only: golden / death cross, EMA 9/21, price across the 200-DMA, MACD signal and zero line, RSI out of oversold / overbought and across 50, Supertrend flips.
- Breakouts: 52-week high **on volume** (> 1.5 × the 20-day average), 52-week low, Donchian 20 and 55, 20-day breakdown.
- Others: volume surges, TTM squeeze firing, Bollinger lower-band bounce, ADX crossing 25, Minervini trend template, RS leader, pullback to the 50-DMA in an up-trend, pocket pivot, gap up, NR7 inside bar.

**Technical rating** (`rating`): 11 votes of −1 / 0 / +1 (close vs SMA20/50/200, EMA9/21, SMA50/200, Supertrend, RSI, MACD, ADX/DI, Bollinger), averaged; bands as TradingView's (±0.1, ±0.5) → STRONG_SELL … STRONG_BUY.

**RS rating:** 0.4 ROC63 + 0.2 ROC126 + 0.2 ROC189 + 0.2 ROC252, ranked 1–99 across the day's universe (the community replica of IBD's rating).

**Signal engine** (`run_technical`, 20:30 on market days):
- Writes `technical_snapshot` (one row per stock and day, kept 600 days) and a `technical_signal` row per scan hit.
- **Levels:** entry = the close; stop 2 × ATR away; target 4 × ATR away (2R); horizon 20 sessions.
- **Confluence out of 6:** technical rating agrees, volume > 1.5 ×, relative strength agrees, ATIP's market regime is not against it, a candle pattern agrees, ATIP's research rating agrees.
- **Outcomes** (`evaluate_signals`): TARGET, STOPPED or EXPIRED (at the close after 20 sessions), with return % and R multiple. A bar that touches both levels counts as STOPPED.
- **Track record** (`scan_stats`): per scan, open / closed / target / stopped / expired, win rate, average R and average return, filterable by minimum confluence.
- **Alerts:** up to 10 of the day's signals with confluence ≥ 4 go out through `alerts.telegram.notify` (category `signals`; alert log, and Telegram when configured).
- Stocks without a bar on the run date are skipped (no stale signals). The benchmark is `market_health.nifty_close`, then `index_levels`, then NIFTYBEES.

**CLI:** `python -m research.tech_signals run [--date YYYY-MM-DD] [--symbols ...] | evaluate | stats | today`

**API:**
- `GET /api/signals/technical?date&direction&min_confluence&limit`
- `GET /api/signals/technical/stats?min_confluence`
- `GET /api/signals/technical/symbol/{symbol}`
- `POST /api/signals/technical/run` (`research:run`, token)

**Page:** `/signals`: Today (one row per stock and direction, with all its scans, levels, confluence and evidence) and Track record.

## MP-01..MP-05: market pulse (`research/market_pulse.py`, `data/participant_oi.py`)

**Global-cue model** (`global_cue_model`):
- Ridge regression over the last 250 sessions of Nifty log returns on the *previous* session's moves in the S&P 500, Nasdaq, Nikkei, Hang Seng, Brent, dollar index, USD/INR and gold (log returns) and the US 10-year yield (bp).
- Inputs are standardised; the output gives each factor's sensitivity **with its unit** ("Nifty % per 1 % move", "per 1 bp"), today's contribution and 60-day correlation.
- **Walk-forward** over the last 120 sessions: direction hit rate on days that moved > 0.2 %, and RMSE vs a "no change" forecast. The page says whether it beats "no change".
- "INSUFFICIENT" below 80 overlapping sessions. `python -m research.market_pulse nifty-history` loads 5 years of the Nifty (Yahoo); the scheduler tops it up nightly at 23:20.

**GIFT Nifty gap** (`capture_gift` 08:45 and 09:05, `evaluate_gaps` 09:35, table `market_cue`):
- Move = GIFT now ÷ GIFT at ≈ 15:30 yesterday (from `index_levels`, else the GIFTNIFTY daily close). This is basis-free: the futures premium over the spot close is not counted as a gap.
- Expected gap % and points = move × beta (1.0 until 30 evaluated mornings, then fitted and clipped 0.3–1.5).
- The model's expected move is stored alongside, and 09:35 fills in the actual open, so `gap_record` shows the direction hit rate for both.

**FII flow pressure** (`fii_pressure`, from `fii_dii_market`): 5-day FII net and its z-score; the **surprise** (residual of FII net regressed on today's and the last two Nifty returns); selling streak; DII absorption over 20 days; MTD / YTD; USD/INR 20-day change. Score 0.5 z(5-day) + 0.3 z(surprise) + 0.2 z(−ΔINR) → STRONG_INFLOW … STRONG_OUTFLOW.

**Participant OI** (`data/participant_oi.py`, 20:15, table `fo_participant_oi`): NSE's `fao_participant_oi_DDMMYYYY.csv` for Client / DII / FII / Pro (title line and stray tabs in the headers handled). 404 = holiday or not yet published, skipped. `python -m data.participant_oi --days 60` loads history.

**Positioning** (`positioning`):
- FII index-futures long % with its 5-day change in pp and percentile.
- Net index futures, calls and puts; client long %.
- Nifty futures build-up (`buildup`: long build-up, short build-up, short covering, long unwinding; only when |ΔOI| > 2 % and |Δprice| > 0.3 %).
- PCR z-score (after 20 days of history).
- **Read:** CROWDED_SHORT below 15 % long; reported as CROWDED_SHORT_COVERING (bullish) **only** if long % rose more than 5 pp in 5 days and futures show short covering. CROWDED_LONG above 65 %. This avoids the contrarian rule that failed through 2026.

**OI walls** (`oi_walls`): nearest-expiry max call OI above spot and max put OI below spot for each index, with distance from spot. Display only.

**Pulse** (`pulse`): context score from the global model, GIFT, FII pressure, positioning and ATIP's regime → RISK_ON (> 0.25) / NEUTRAL / RISK_OFF (< −0.25), with every reason listed.

**CLI:** `python -m research.market_pulse pulse | gift | evaluate | nifty-history`

**API:**
- `GET /api/market-pulse`, `/api/market-pulse/global`, `/fii`, `/positioning`
- `POST /api/market-pulse/gift` (capture now), `POST /api/market-pulse/refresh` (participant OI + Nifty history), both `research:run` and the token

**Page:** `/market-pulse`.

## OB-01..OB-03: pending orders, market-wide and yours (`data/order_pressure.py`, `portfolio/open_orders.py`)

**Market-wide pressure:**
- Every 15 minutes in market hours (`config.json` `"order_pressure": {"enabled", "interval_minutes", "max_symbols"}`), one Dhan `quote_data` call per 1,000 instruments.
- Stores per stock: LTP, change %, volume, total pending buy and sell quantity, `total_imbalance` = (buy − sell) ÷ (buy + sell), top-5 bid and ask quantity and imbalance, spread in bp, and a label (STRONG_BUYERS > 0.3 > BUYERS > 0.1 > BALANCED > −0.1 > SELLERS > −0.3 > STRONG_SELLERS).
- Table `order_book_pressure`, kept 90 days.
- `persistent()`: one side beyond ±0.2 in 3 of the last 4 polls, the newest included.
- The page and docstring state the limits: the evidence says predictive for minutes, gone within 30 in NSE stocks; totals include far-from-market orders; size can be spoofed or hidden.
- **API:** `GET /api/orderbook/pressure?side=buy|sell&limit`, `GET /api/orderbook/pressure/{symbol}` (the day's polls), `POST /api/orderbook/snapshot` (`research:run`, token).

**Your open orders** (`GET /api/brokers/open-orders`, read-only):
- Dhan order list filtered to open statuses (TRANSIT, PENDING, PART_TRADED, TRIGGER_PENDING, CONFIRM, OPEN) and forever orders, normalised to kind, symbol, side, type, product, quantity, filled, price, trigger, status, created.
- ATIP paper orders (PENDING, PARTIALLY_FILLED) and order rules (ACTIVE, PENDING_CONFIRMATION).
- A missing `dhanhq` or token returns UNAVAILABLE with the reason; a failing call returns FAILED or PARTIAL with the Dhan error.

## Wiring

- Tables: `db/schema_w39.py` (`prices_daily_backfill`, `research_report`, `research_screen`, `technical_snapshot`, `technical_signal`, `order_book_pressure`, `fo_participant_oi`, `market_cue`), applied by `db/schema.py`. Scoping: GLOBAL, except `research_screen`, which is OWNER. Privacy inventory: `order_book_pressure` (market, 90 days) and `market_cue` (research, kept) added; the rest are classified by the existing rules.
- Retention (`db/purge.py`): `technical_snapshot` in the LONG tier (600 days), `order_book_pressure` in the SHORT tier (90 days). Signals and cues are kept.
- Routes: `dashboard/w39_routes.py`, registered in `server.py`. Authz rules: `POST /api/options/(build|analyse)` → `research:run`; `POST /api/screener/` → `workspace:write`; `POST /api/signals/` and `POST /api/(market-pulse|orderbook)/` → `research:run`. GETs fall under the existing read rules (`/api/brokers/` → `portfolio:read`).
- Scheduler (`_schedule_w39_jobs`): order-book poll every 15 minutes (market hours only); GIFT capture 08:45 and 09:05; gap evaluation 09:35; participant OI 20:15; technical signals 20:30; research reports 20:40; saved screens 20:50; history backfill 22:20; Nifty history 23:20. Each job is logged in `pipeline_log` through `run_job`.
- `docs/API_REFERENCE.md` and `docs/openapi.json` regenerated. They were also stale from W34–W38: 402 → 511 routes.
- `db/sql/*.sql` are pinned snapshots (master @ 6896bea) and were not regenerated. The new tables are created on first start like any additive migration.

## Tests

| File | Tests | Covers |
|---|---|---|
| `tests/test_w39_history.py` | 8 | retention tiers and config; backfill walk-back, listing stop, resume; refusal vs listing; parking; exception tagging; schedule |
| `tests/test_w39_research.py` | 20 | DCF = Gordon invariant; scenarios; sensitivity monotonicity; growth caps; justified P/B; unit normalisation; rating hurdles; peers; financials; blend; quality; end-to-end report on a seeded DB; calls and hit rate; split-adjusted outcome; batch run; single-report read limited to the stock and its peers; API and token; authz; table classification |
| `tests/test_w39_screener.py` | 19 | query precedence / NOT / IN / aliases; missing values; malicious and malformed queries refused; presets valid; snapshot fields on a seeded DB; magic rank; filter / sort / columns / CSV; cache; saved screens with new matches and an alert; API and token; permissions and classification |
| `tests/test_w39_options.py` | 11 | payoff and breakevens; unlimited flags; condor risk = width − credit; every template; POP sanity; time value; implied IV; validation; chain pricing; API |
| `tests/test_w39_technicals.py` | 19 | Wilder RSI bounds and value; Supertrend reversal; ATR; golden cross on the crossing day only; 52-week breakout needs volume; RSI oversold turn; engulfing / hammer / doji / inside bar; rating; snapshot flags every scan; levels and confluence with ATIP regime labels; run stores snapshots and signals and skips stale stocks; TARGET / STOPPED / EXPIRED and stats; both-touch = STOPPED; screener integration and CONTAINS; API; permissions and tables; RS rank and pocket pivot |
| `tests/test_w39_market_pulse.py` | 14 | the global model recovers a planted 0.5 S&P beta and beats "no change" walk-forward; INSUFFICIENT on short history; GIFT gap against yesterday's 15:30 GIFT (not the 23:00 reading or the spot close) and the open check; FII streak, absorption and label; build-up truth table; crowded short is bullish only when covering; OI walls; order-book parse and labels; persistence needs the newest poll; participant-OI CSV with title line and tab-polluted headers; open orders keep only open statuses and report Dhan errors; pulse; API, token and 400s; permissions and tables |

## Known limits and next steps

**Valuation inputs:**
- Consensus estimates and revisions are not used; there is no licensed feed.
- XBRL parses key lines only, not full statements. No 3-statement model, SOTP or insurer P/EV.
- With few stored quarters, own-history multiples are skipped and uncertainty rises. Run the XBRL history backfill (W27-1) to fill them.

**Moat proxy:** quantitative only, not an analyst judgement.

**Hit rates:**
- Exist only from the day reports start.
- Measured against ATIP's own prices and not independently verified.
- SEBI's PaRRVA rules apply before any past performance is shown to others.

**Options builder:** analysis only. One-click execution would need the live OMS (gap analysis §4, item 8), which needs the owner's explicit go-ahead.

**Technical signals:**
- End of day only; no intraday scans yet (they need the 15-minute bar job and the Dhan Data API).
- No chart-pattern detection (Darvas, VCP, triangles, head and shoulders) yet.
- The regime enters as one confluence factor; there is no distribution-day gate yet.
- Track records start on the first run and are measured on ATIP's own prices. Trust a scan only after ~30 closed signals.

**Market pulse:**
- The global model uses daily closes (previous session), not synchronised 15:30 → 08:45 moves; it needs the Nifty and global history loaded.
- GIFT capture needs Dhan index quotes; participant OI needs NSE to answer (Indian connection).
- Order-book pressure needs the Dhan Data API and is context, not a signal.
- The early-October 2026 market figures in the plan doc are press reports, not verified.

**Next on the roadmap** (details and order in `docs/ANALYSIS_TOOLS_AND_SIGNALS_PLAN_2026-10.md` §8):
- Regime gate (distribution days), track record by regime and horizon, weekly rating, chart patterns, RS-line highs, delivery spikes, explainable fundamental composite, event calendar
- Intraday scans and 20-level depth imbalance (Dhan Data API)
- English → screener query and an ATIP MCP server (Anthropic key)
- MF analytics / SIP, tax P&L, earnings-surprise signal
- Live execution only with the owner's explicit go-ahead
