# W39: history, research, options, screener, signals and market pulse handoff

**Branch:** `ccr-643d84fc-yig8ts` (PR #4, which also carries the Dhan token-refresh fix).

**Why this wave:**
- The owner's notes (`ATIP-.txt`): compare with Zerodha and Dhan, show stock history, store 7 years of data.
- `docs/ATIP_GAP_ANALYSIS_2026-10.md`: how ATIP compares with Dhan, Zerodha and institutional research.
- `docs/ANALYSIS_TOOLS_AND_SIGNALS_PLAN_2026-10.md`: how the leading FA / TA / AI tools work, the evidence on global cues, FII flows and order books, and the phased plan that the technical screener, signals and market pulse below start.

**Status:** developed. 222 W39 test cases and the full suite pass. The five pages (/research, /screener, /signals, /market-pulse, /options-builder) were rendered in a headless browser on seeded data; /options-builder and /market-pulse also at a 390 px phone width with no horizontal scroll. Not merged and not deployed. Nothing in this wave places an order; the open-orders view only reads.

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
- Technical (from `technical_snapshot`, see TA below): technical rating and label, RS rating 1–99, RSI, MACD histogram, ADX, Supertrend direction, ATR %, distance from the 50- and 200-DMA, Bollinger width, volume ÷ 20-day average, 3-month relative strength vs the Nifty, 1- and 3-month return, candle patterns, today's signals, chart patterns in place (`chart_patterns`, `vcp_setup`), the RS line at a high, size group and RS rank within it, NSE delivery % and its ratio to the 20-day average, and one `scan_<key>` 1/0 field for each of the 39 scans.
- Order book (from `order_book_pressure`, see OB below): `book_imbalance`, `book_pressure`, `book_persistent`.
- ATIP: score and signal.
- Research model: rating, upside, fair value, moat proxy, quality score.
- **Magic-formula rank:** Greenblatt's earnings-yield rank + ROCE rank, approximated with E/P; financials excluded.

**Universe:** stocks with fundamentals, plus stocks with a technical snapshot in the last 10 days, so a chart-only screen also covers stocks without fundamentals. 120 fields in all.

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

**35 presets, in three groups:**
- **Fundamental (12):** quality compounders, value, GARP, dividend, debt-free, promoters adding, undervalued by ATIP's model, strong near the 52-week high, turnaround, magic formula top 30, oversold quality, pledge risk.
- **Technical (20):** delivery spike on an up day, RS line new high before price, RS leaders in their size group, 52-week breakout on volume, golden cross, Supertrend buy, MACD bullish above the 200-DMA, RSI oversold reversal, Minervini trend template, squeeze fired, bullish candle at support, technical STRONG BUY, daily and weekly both bullish, breakout with the weekly trend, chart pattern breakouts, VCP setups, RS leaders (RS ≥ 80), pocket pivots, breakdowns, buyers queuing with a positive chart.
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

**39 scans** (`SCANS`, completed daily bars only; 27 bullish, 11 bearish, 1 neutral; the 6 chart-pattern breakouts are described under CP, the 2 RS-line scans under RL and the delivery spike under DL below):
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

## RG-01..RG-04: market regime gate (`research/regime_gate.py`), Phase 2 item 1

**What it answers:** should new long (or short) signals be taken today? It gives an IBD-style read of the Nifty, replayed one session at a time from ATIP's data. It is causal: row t uses only data up to t, which a test checks.

**Rules** (`config.json` `"regime_gate"`, defaults shown):
- **Distribution day:** the Nifty closes down ≥ 0.2 % on higher market volume than the session before.
  - It is active for 25 sessions, or until the Nifty closes 5 % above that day's close.
  - Market volume is the summed volume of every stock in `prices_daily` that traded on both days, so a stock joining the universe cannot move the ratio. It falls back to NIFTYBEES under 20 stocks.
  - A day without a volume comparison counts as neither a distribution day nor a follow-through day.
- **Status:**
  - CONFIRMED_UPTREND: fewer than 4 active distribution days.
  - UPTREND_UNDER_PRESSURE: 4 or 5.
  - CORRECTION: 6 or more; or the Nifty 10 % below the uptrend's highest close; or, after a follow-through, a close below the correction low it launched from.
  - RALLY_ATTEMPT: in a correction, the first up close after the lowest close is day 1; a lower close resets it.
  - A follow-through day (day 4 or later, Nifty up ≥ 1.25 % on higher volume) restores the uptrend and clears the count.
- **Gate:**
  - OPEN: confirmed uptrend above the 200-DMA.
  - CAUTION: under pressure, or a confirmed uptrend still below the 200-DMA.
  - CLOSED: correction or rally attempt.
  - The first 60 sessions of history are warm-up (gate unknown).
- **Nifty closes:** `global_market_history` `nifty50` (5 years from Yahoo), extended with `market_health.nifty_close` for later days; else `market_health`; else NIFTYBEES.

**Table** `market_regime_gate`, one row per session. It holds: close, change, volume ratio, distribution day, the active count and dates, SMA 50/200, drawdown, status, gate, rally day, follow-through, reason, volume source. It is recomputed by `run_technical` before the 20:30 signals, so no separate job is needed.

**Signals:**
- `technical_signal` gains `market_gate` and `alignment`: WITH, MIXED or AGAINST.
  - A BULL signal is WITH the market when the gate is OPEN, MIXED on CAUTION, and AGAINST when CLOSED. A BEAR signal is the reverse.
  - The columns are added by `ALTER TABLE` on existing databases (`W39_COLUMNS`).
  - A signal keeps the gate it was born under. `backfill_gate` gives older signals the gate of their own date.
- `alert_top` never alerts AGAINST signals, and the alert names the gate.
- `todays_signals(alignment=...)` and `scan_stats(alignment=...)` filter by alignment.
- `gate_effect` compares closed signals WITH, MIXED and AGAINST the market. It gives a verdict only with 30+ closed signals on each side and a gap beyond noise (Welch t, |t| ≥ 2).
- `market_pulse.pulse` adds the gate to the context score and reasons.

**CLI:**
- `python -m research.regime_gate [now | history --days 60 | run --date YYYY-MM-DD]`
- `python -m research.tech_signals gate-effect`

**API:**
- `GET /api/market-regime`: today, the change log and the rules.
- `GET /api/market-regime/history?days=250`
- `POST /api/market-regime/run` (`research:run`, token).
- `GET /api/signals/technical?alignment=WITH|MIXED|AGAINST|not_against`
- `GET /api/signals/technical/stats?alignment=`
- `GET /api/signals/technical/gate-effect`

**Pages:**
- `/market-pulse` has a Market gate card: status, why, distribution days, Nifty vs 200-DMA, last follow-through, the rules and the recent changes. Its chart shows the Nifty and the 200-DMA (dashed) over 250 sessions, with distribution days (▼) and follow-through days (▲), a gate strip (open / caution / closed in the reserved status colours, with labels), crosshair tooltip, legend and table view.
- `/signals` has a gate banner and a market filter. "Hide signals against the market" is the default and shows how many it hid. Each signal has a Market column. The Track record tab has "Does the market gate help?" and a "born with / mixed / against" filter.

**Also fixed while rendering it:**
- A global factor whose data stopped updating used to have its last move repeated forever, and its zero-variance 60-day correlation (NaN) made `/api/market-pulse` answer 400.
- `design()` now leaves a factor out after 7 days without data (`stale_factors`, shown on the page).
- W39 routes turn any NaN or inf into null.

## TR-01..TR-03: track record by regime and horizon (`research/tech_signals.py`), Phase 2 item 2

**Forward record** (`evaluate_forward`, run after `evaluate_signals` in every `run_technical`):
- For each signal it fills `ret_5d` / `ret_20d` / `ret_60d`: the stock's return from the signal day's close to the close 5, 20 and 60 of its own sessions later.
- It also fills `excess_5d` / `excess_20d` / `excess_60d`: that return minus the Nifty's over the same dates. Nifty closes come from `regime_gate.nifty_closes`.
- Both are signed for the direction (a short gains when the stock falls).
- Values fill in as sessions pass. A stored value is never recomputed.
- This is separate from the stop / target outcome, so a scan's edge can be judged even where the 2R levels do not fit it.

**Regime at birth:** `technical_signal.market_status` (the gate's status) joins `market_gate` / `alignment`. `backfill_gate` fills all three for older signals from their own day's gate row.

**Stats:**
- `forward_stats(conn, horizon=5|20|60, min_confluence)` returns:
  - per scan × gate at birth (ALL / OPEN / CAUTION / CLOSED / UNKNOWN);
  - per confluence band (0–1, 2–3, 4–6) × gate;
  - overall by gate.
- Each cell gives n, % that beat the Nifty, median and mean excess, and `enough` (n ≥ 10).
- `record_map` feeds `todays_signals`. Each signal gets `record` (its scan in the same gate, else all markets), `record_scope` and `record_horizon`.
- `alert_top` quotes the record once it is `enough`.

**CLI:** `python -m research.tech_signals forward [--horizon 20]`

**API:** `GET /api/signals/technical/forward?horizon=20&min_confluence=0`. A horizon other than 5 / 20 / 60 returns 400.

**Page `/signals`:**
- Today: a bracket after each scan shows its record in today's market: beat-the-Nifty % · median excess. It is grey under 10 signals, * when the record is across all markets. A tooltip explains it.
- Track record: a card "Against the Nifty, by market and holding period" with a horizon selector. It holds an all-signals row, "Does confluence add?", and the per-scan table by gate.

## WK-01..WK-02: weekly technical rating (`research/technicals.py`), Phase 2 item 3

**Weekly bars:**
- `weekly_bars(df, as_of)` resamples daily bars to weeks ending Friday: first open, highest high, lowest low, last close, summed volume.
- It keeps **completed weeks only**: a week counts once its Friday is on or before `as_of`. So the weekly rating never changes mid-week, and a week cut short by a Friday holiday counts from the next Monday's run.

**Rating:**
- `weekly_rating` runs the same `indicators` + 11-vote `rating` on those bars. It reports the weekly rating, label, RSI and Supertrend direction.
- It needs 35 completed weeks (`MIN_WEEKS`). With the 600-day signal lookback (~85 weeks), the 200-week votes are absent, and they stay absent until ~4 years are stored.

**Agreement:**
- `mtf_alignment`: BULL when daily and weekly are both BUY / STRONG_BUY, BEAR when both are SELL / STRONG_SELL, MIXED otherwise, None without both.
- `weekly_agrees(direction, weekly_label)`: 1 / 0 / None.

**Stored:**
- `technical_snapshot` gains `tech_rating_w`, `tech_rating_w_label`, `rsi_14_w`, `supertrend_dir_w` and `mtf_alignment`.
- `technical_signal` gains `weekly_agrees`.
- Both are added to existing databases by `ALTER TABLE` (`W39_COLUMNS`).
- `weekly_agrees` is deliberately **not** part of the confluence count.

**Screener:**
- Fields: `tech_rating_w`, `tech_rating_w_label`, `mtf_alignment`, `rsi_14_w`, `supertrend_dir_w`.
- Presets: "Daily and weekly both bullish" and "Breakout with the weekly trend".
- The technical default columns include the weekly label.

**Signals:**
- `/signals` Today has a Weekly column: the weekly label, with ✓ when it is on the signal's side.
- Alerts note "weekly trend agrees".
- `forward_stats` adds `by_weekly`: agrees / disagrees / no weekly rating × gate. It is shown on the Track record tab as "Does the weekly trend add?".

## CP-01..CP-03: chart patterns (`research/patterns.py`), Phase 2 item 4

**Pivots:**
- `swings(high, low, k=3)`: a swing high is higher than the 3 bars before and not exceeded by the 3 after, so it is confirmed 3 sessions later. Lows are mirrored.
- Every pattern is found on the bars **before** today (`_base`). Today's close only decides the breakout, so the trigger bar never shapes the pattern, and a stored signal reproduces from its day's bars.

**Patterns** (thresholds are ATIP's, in the module docstring):
- **Darvas box:** the latest 20-session high, within 3 % of the 52-week high, not exceeded since (3+ sessions). The bottom is the lowest low since, which must have held 3 sessions. 5+ sessions, ≤ 25 % tall.
- **VCP:** 2–4 pullbacks (swing high to the lowest swing low before the next swing high), each shallower than the one before, the last ≤ 12 % and the first ≤ 40 %, highs within 15 % of the base top. Volume 10-day < 85 % of 50-day. Above the 50-DMA, with the 50-DMA above the 200-DMA when known. Pivot = the last pullback's high, not closed above since.
- **Double bottom:** two swing lows within 3 %, 10–60 sessions apart, the second within 30 sessions, after a 10 % decline. The neckline (middle peak) is 6 %+ above them and not closed above since.
- **Ascending triangle:** 2+ swing highs within 1.5 % of the resistance, spread over 10+ sessions, the last being the latest swing high. Rising swing lows (+1 % each) since the first touch.
- **Head and shoulders top:** the last three swing highs, the head 3 %+ above both shoulders, the shoulders within 8 % of each other, after an advance. The neckline through the two troughs is extended to today and was not broken before today.

**Scans:**
- `darvas_breakout`, `darvas_breakdown`, `vcp_breakout` (needs ≥ 1.4x volume), `double_bottom_breakout`, `ascending_triangle_breakout`, `head_shoulders_breakdown`.
- Each rule returns its reason text with the pattern's levels; `run_scans` now accepts a string reason.
- `detect(d)` computes all patterns once per frame. The cache is keyed to the frame's last bar, because pandas copies `attrs` onto slices.

**Alerts:**
- `T.PATTERN_SCANS` alert only after `PROVE_CLOSED` (30) closed signals with average R > 0 (`proven_scans`).
- `scan_stats` rows carry `pattern` and `alerts` (`on` / `held`). The Track record shows "held · n/30".

**Snapshot and screener:**
- `chart_patterns`: patterns in place near their trigger, with levels: the upper half of a Darvas box or double-bottom base, within 3 % of a triangle's resistance, any VCP below its pivot, a head and shoulders above its neckline.
- `vcp_setup` 1/0.
- Presets: "Chart pattern breakouts" and "VCP setups (not yet broken out)".

**Calibration:**
- On 4,800 random-walk stock-days, breakouts fired on 0.02 % (VCP) to 1.2 % (Darvas breakdown) of days.
- Setups were listed on 0.2 % (VCP) to 7 % (Darvas box).

## RL-01..RL-02: RS line and size-group ranks, Phase 2 item 5

**RS line:**
- `indicators` adds `rs_line` (close ÷ the benchmark, the Nifty) and `rs_line_prior_hi` (its prior 252-session high, at least 120 sessions).
- Scan `rs_line_new_high`: the first day the RS line closes above that high.
- Scan `rs_line_leads`: the same while the close is still at or below its prior 52-week high (IBD's "RS line leads price").
- Snapshot field `rs_line_at_high` (1/0, None without a benchmark).

**Size groups** (`tech_signals.cap_rank`, after `rs_rank` in every run):
- `market_caps` = close × the latest `fundamental_data.shares_out` with `COALESCE(period_end, report_date)` on or before the run date.
- Ranked over ATIP's universe with AMFI's rule: top 100 LARGE, 101–250 MID, the rest SMALL. Stocks without shares get no group.
- `rs_rating_cap` is the RS percentile within the group.
- Stored in `technical_snapshot` (`rs_line_at_high`, `cap_bucket`, `rs_rating_cap`; ALTER TABLE for older databases).

**Screener:** the three fields, with presets "RS line new high before price" (`scan_rs_line_leads = 1`) and "RS leaders in their size group" (`rs_rating_cap >= 90`).

## DL-01: delivery-% spike, Phase 2 item 6

**Data:** `prices_daily.delivery_pct` is already filled from NSE's bhavcopy (`DELIV_PER`, `data/bhavcopy.py`). `load_bars` now reads it.

**Indicators:** when the frame carries `delivery_pct`, `indicators` adds `deliv20` (its 20-session average up to yesterday, at least 10 values) and `deliv_ratio`.

**Scan** `delivery_spike_up`: `deliv_ratio` ≥ 1.5, delivery % ≥ 30, and an up close.

**Stored:** `technical_snapshot.delivery_pct` and `delivery_ratio`.

**Screener:** the two fields, with the preset "Delivery spike on an up day".

**Caveat:** a common Indian heuristic with no peer-reviewed return evidence. Its track record decides.

## FS-01..FS-03: explainable fundamental scorecard (`research/scorecard.py`), Phase 2 item 7

**What it is:** Simply Wall St's Snowflake layout rebuilt on ATIP's stored fundamentals: five axes of six pass / fail checks. Every check is a sentence carrying the numbers it used, for example "P/E 15.0x vs 25.0x, the median of 12 Capital Goods peers".

| Axis | The six checks |
|---|---|
| Value | below the research model's fair value; 20 %+ below it; P/E below the market's median; P/E below its industry's median; PEG below 1; P/B below its industry's median |
| Growth | EPS growth above a savings rate (the 7 % risk-free rate in `research/valuation.py`); above the market's median; 20 %+; revenue growth above the market's median; 20 %+; growth it can fund itself, ROE × the share of profit kept, of 10 %+ |
| Past performance | EPS (TTM) up over 3 years; growth accelerating (this year's above the 3-year rate); EPS growth above the industry's median; net margin up on a year ago; ROE 20 %+; positive free cash flow |
| Financial health | current ratio 1+; net debt under 40 % of equity (gross debt / equity when no debt and cash split is stored); debt / equity not up on a year ago; interest covered 3x+; free cash flow covering 20 %+ of debt; promoter pledge under 5 %. A company with practically no debt (D/E ≤ 0.05) passes the debt-rising, interest and FCF-to-debt checks outright |
| Dividend | yield in the top 75 % of dividend payers, and the top 25 %; paid in each of the last two years; per-share dividend up on the year before; payout under 75 % of earnings; dividends covered by free cash flow |

**Rules:**
- A check is pass, fail, or unknown (no data). An unknown never counts as a pass. `checks_known` says how many could be checked.
- Banks and NBFCs: the five debt checks and the FCF checks are unknown ("borrowing is its raw material"), so compare financials with financials.
- A loss-maker fails the P/E, PEG, self-funded-growth and payout checks. A company paying no dividend fails the whole dividend axis, as on the Snowflake.
- Growth uses the latest reported figures: ATIP holds no analyst forecasts.
- Thresholds are in `config.json` `"scorecard"` (`DEFAULTS` in the module).

**Inputs:**
- The market and industry comparisons use the screener's snapshot of every stock. Industry medians exclude the stock itself and need 3+ peers; market medians need 10+ stocks.
- The history checks read the stored quarters, point in time (period end on or before the day). A quarter counts as "a year ago" within ±50 days.
- Dividends: the stored yield, else NSE's corporate-action calendar (`corporate_actions`, subjects like "Interim Dividend - Rs 6 Per Share", several amounts per subject summed), with old amounts adjusted for later splits and bonuses. The two-year checks stay unknown until the calendar covers two years; meanwhile "paid steadily" falls back to a stored yield in every quarter over two years.

**Where it shows:**
- Screener fields `checks_passed` (0–30), `value_checks`, `growth_checks`, `past_checks`, `health_checks`, `dividend_checks` (0–6), group "Scorecard". `checks_passed` is in the default fundamental columns.
- Presets: "Scorecard all-rounders" (20+, no axis below 2), "Healthy and growing", "Undervalued with a clean record", "Dependable dividends".
- `/research`: a scorecard card under the report header with a five-axis chart (one series, `#3987e5`) and every check listed with its numbers, which is also the chart's text equivalent.
- `python -m research.scorecard show RELIANCE` prints the same.

**Its own record (FS-03):**
- The 20:50 saved-screens job first stores the day's scorecards in `fundamental_scorecard` (counts, plus pass / fail flags as JSON).
- `record(conn, horizon)`: return minus the Nifty's over the next 20 / 60 / 120 / 250 sessions, by checks-passed band (0–10, 11–15, 16–20, 21–30). One sample per stock per calendar month, so a slow-moving score is not counted twenty times. A band needs 30 samples; the top-minus-bottom spread is only given when both ends have them.
- `/research` → Ratings & hit rate → "Does the scorecard pay?".

**API:** `GET /api/research/scorecard/{symbol}` (404 without fundamentals), `GET /api/research/scorecard-record?horizon=60` (400 on another horizon).

## EV-01..EV-03: macro event calendar (`research/event_calendar.py`), Phase 2 item 8

**Events (EV-01):**
- Table `macro_event` (event date, kind, title, source). Kinds: `FOMC` (14:00 New York), `US_CPI` and `US_NFP` (08:30 New York), `RBI_POLICY` (10:00 IST).
- Seeded with the Fed's 2026 decisions and its tentative 2027 calendar, the BLS 2026 CPI and jobs-report dates (January payrolls moved to 11 Feb by the 2026 funding lapse), and RBI's 2026-27 MPC decisions (press release 23 Mar 2026). Each date was checked against two sources on 2026-10-07; the BLS and Fed sites themselves were blocked from here. The seed needs a yearly refresh.
- More dates: `config.json` `"event_calendar": {"events": [{"date", "kind", "title"?}]}`, `POST /api/market-pulse/events`, or the add form on `/market-pulse`. A delete marks the row deleted, so a seeded date does not come back.
- `python -m research.event_calendar upcoming --days 30` lists them.

**Sessions:**
- New York times follow US daylight saving (second Sunday of March to first Sunday of November), computed in the module, so nothing depends on the machine's time-zone database.
- `exposure()`: a US release lands after India's close (18:00 / 19:00 IST, or 23:30 / 00:30 IST for the Fed), so it hits the next NSE session's open, skipping NSE holidays from `utils/trading_calendar.py`. The RBI decides during the session.

**The gap band (EV-02):**
- `capture_gift` stores each morning's events and band in `market_cue` (`events`, `band_pct`; ALTER TABLE for older databases).
- Band = the mean absolute miss of past GIFT estimates (10+ evaluated mornings), × a widening factor after a US release overnight.
- The factor is measured, the ratio of the miss after US releases to the miss on other mornings, clipped 1–3, once 10 and 30 such mornings exist. Until then it is an assumed ×1.5 (`"default_widen"`), and the page says so. Mornings stored before the calendar existed are looked up in it.
- RBI days are marked but not widened: the decision comes after the open.

**Market pulse (EV-03):**
- `pulse()` returns `events` (today's, the next 14 days, the band) and adds a reason line on event days.
- `gap_record()` splits the miss into mornings after a US release and the rest.
- `/market-pulse`: an "Event calendar" card and "± band (typical miss, widened after US CPI overnight)" next to the expected open.

**API:** `GET /api/market-pulse/events?days=30`, `POST /api/market-pulse/events` `{date, kind, title?}`, `POST /api/market-pulse/events/delete` `{date, kind}` (`research:run`, token).

## IN-01..IN-03: intraday scans on 15-minute bars (`research/intraday_signals.py`), Phase 3 item 1

**Bars:** `intraday_bars` already receives 15-minute bars for the tracked universe every 30 minutes from 10:00 (`run_intraday_30min`, Dhan intraday API). `load_bars` reads the last 6 sessions of 15-minute bars that have completed (a bar stamped at its start is done 15 minutes later).

**Scans (IN-01)**, each at most once per stock, day and scan:

| Scan | Rule |
|---|---|
| Opening-range breakout / breakdown | the first close beyond the 09:15–09:30 range (`"or_minutes": 30` for 09:15–09:45), only if it comes by 11:30 (`"orb_until"`) on 1.5× (`"orb_min_rvol"`) the same slot's average volume over the last 5 sessions |
| Open = low / open = high | judged once on the first hour: no trade more than 0.1 % beyond the open, and the 10:15 close 0.5 %+ away from it |
| Intraday squeeze up / down | `research/technicals.py` Bollinger (20, 2) inside Keltner (20, 1.5 ATR) for 6+ bars, released on a bar with 1.2× slot volume that closes beyond SMA 20 in its direction |

**Calibration:** on random-walk bars with independent volumes (500 stocks), the unfiltered breakout fired on ~78 % of stocks a day per side; with the time and volume filters ~8–10 %, open = low / high ~10 %, the squeeze under 2 %. About 10 seconds for 500 stocks.

**Record (IN-02):**
- Each hit stores its trigger bar and close, and the price when ATIP saw it: the latest completed bar at the run, since bars arrive every 30 minutes. The record is measured from that seen price.
- After the session: return to the last close (signed for direction), the same minus the Nifty's move over those minutes (`index_levels`), and the best and worst move after it was seen.
- `stats()`: per scan, hits, closed, how often it made money and beat the Nifty, median and mean. A scan alerts (Telegram digest) only after 30 closed hits with a positive mean excess; `/signals` shows "held · n/30" until then.

**Job and page (IN-03):**
- Every 15 minutes, 09:45–15:50 on trading days (`_w39_intraday_tick`); it does nothing without stored bars. Config `"intraday_signals"`.
- `/signals` → Intraday: today's hits (or any date), and the record per scan.
- **API:** `GET /api/signals/intraday?date`, `GET /api/signals/intraday/stats`, `POST /api/signals/intraday/run` (`research:run`, token).
- Table `intraday_signal` (kept: its own record).

**Fixed on the way:** `strategy/intraday_scan.py load_bars` read every interval from `intraday_bars`. With tick capture on, 1-minute bars (source `ticks`) would have been mixed into the 15-minute series of the five existing intraday scans; it now reads one interval.

## DP-01..DP-03: 20-level depth and a depth-weighted imbalance (`data/depth20.py`), Phase 3 item 2

**Feed (DP-01):**
- `wss://depth-api-feed.dhan.co/twentydepth?token&clientId&authType=2` (Dhan Data API), subscribed with `{"RequestCode": 23, "InstrumentCount", "InstrumentList": [{"ExchangeSegment": "NSE_EQ", "SecurityId"}]}`, 50 per message (Dhan's limit per connection).
- Frames follow dhanhq's `fulldepth.py` (fetched 2026-10-07): each message is a 12-byte header `<hBBiI` (length, code 41 bid / 51 ask / 50 disconnect, segment, security id, sequence or disconnect reason) and 20 levels of `<dII` (price, quantity, orders). One frame can carry several messages.
- `Depth20Feed`: resolves the watchlist (config `depth20.symbols`, else `data/stock_feed.default_symbols`: holdings, open positions, active rules, top ATIP scores), runs on trading days 09:00–15:45, reconnects with backoff up to 5 minutes, and parks on Dhan's refusals 805–809 with the reason (806: subscribe to the Data APIs). Started by `main.py` with the index and stock feeds, only when `depth20.enabled` (off by default; `config_template.json`).

**Measures (DP-02):**
- `dwi`: levels within `band_bp` (50 bp) of the mid, level k weighted e^(−`decay`(k−1)), (Σ w·bid − Σ w·ask) / (Σ w·bid + Σ w·ask).
- Stored beside it: `imb_l1` (best level), `imb_20` (all 20 levels, unweighted), mid, spread in bp, total quantities, levels.
- One snapshot per stock every `sample_seconds` (15), written every 10 seconds to `depth20_snapshot` (SHORT retention, 90 days).
- `persistent()`: |DWI| > 0.3 in each of the stock's last 3 snapshots → BUYERS / SELLERS.

**Validation (DP-03):** `validate(conn, 1 | 5)` pairs each snapshot with the same stock's snapshot 1 or 5 minutes later (within 2 minutes) and fits a logistic regression of the direction of the mid's move on each measure (Newton's method, no new dependency). It reports n, slope, z and how often the sign was right (|measure| > 0.1). A measure is called predictive only with 500+ pairs, |z| ≥ 2 and a positive slope.

**Page and API:** `/market-pulse` → "20-level depth (watchlist)"; `GET /api/orderbook/depth20` (latest per stock, flags, feed status), `GET /api/orderbook/depth20/validation?horizon=1|5`.

## OF-01..OF-03: order-flow imbalance (OFI) from quote changes (`data/depth20.py`), Phase 3 item 2

**Measure (OF-01):** for consecutive books n−1 → n (every bid or ask message the feed receives, not only the 15-second samples), Cont, Kukanov & Stoikov (2014) at the best level:

  e_n = 1{Pᵇₙ ≥ Pᵇₙ₋₁} qᵇₙ − 1{Pᵇₙ ≤ Pᵇₙ₋₁} qᵇₙ₋₁ − 1{Pᵃₙ ≤ Pᵃₙ₋₁} qᵃₙ + 1{Pᵃₙ ≥ Pᵃₙ₋₁} qᵃₙ₋₁

- Bids arriving or the bid raised count as buying; asks arriving or the ask lowered as selling. A side that did not change contributes 0, so separate bid and ask messages add up to the same e as one update of both.
- The same formula at the m-th best level gives Xu, Gould & Howison's (2019) multi-level OFI. A level a side lacks counts as quantity 0 at a price beyond every other (bids −∞, asks +∞): a level that empties counts as its quantity leaving, one that appears as its quantity arriving.
- `OFI` keeps each stock's running sums between snapshots. At a snapshot:
  - `ofi_l1`: Σ e_n at the best level since the previous snapshot (shares).
  - `ofi_depth`: the average best-level depth over that interval, (qᵇ + qᵃ) / 2. `ofi_l1 / ofi_depth` is the scale-free OFI (CKS: OFI's price impact is inversely proportional to depth), shown on the page and used in the validation.
  - `ofi_ml`: Σₘ wₘ · OFIₘ / Q, divided by Σ wₘ, over levels 1..`ofi_levels` (20). Q is the average depth per level and side over the interval (Cont, Cucuringu & Zhang's scaling, a missing level as 0), so a stock 400× deeper with the same flow scores the same. wₘ = e^(−`ofi_decay`(m−1)), default 0: equal weights. Cont, Cucuringu & Zhang integrate with the first principal component instead, which needs a fitted history.
  - `ofi_updates`: the book updates summed.
- An empty or crossed book is skipped; the next good book is compared with the last good one. A new connection clears the books and the sums: the first snapshot after it has no interval behind it, so its OFI is NULL.

**Storage (OF-02):** `depth20_snapshot` gains `best_bid`, `best_bid_qty`, `best_ask`, `best_ask_qty`, `ofi_l1`, `ofi_ml`, `ofi_depth`, `ofi_updates`. They are in the DDL for new databases and in `ADDED_COLUMNS` (via `W39_COLUMNS` and the module's `ensure_tables`) for tables created before. Old rows keep NULLs.

**Validation (OF-03):** `validate()` now fits each measure on the rows that have it, so snapshots from before OFI still count for DWI.
- **Next interval:** `ofi_l1` (scale-free) and `ofi_ml` join DWI and the two imbalances against the direction of the mid's next 1 / 5-minute move. Same rule: predictive only with 500+ pairs and z ≥ 2 (|z| ≥ 2, positive slope).
- **Contemporaneous** (`contemporaneous`): each OFI against the mid's move over its own interval (the stock's previous snapshot, at most 2 minutes earlier, to this one). It uses the same logistic rule ("related"), plus CKS's linear fit of the move in bp on the OFI, stock by stock (30+ pairs, unchanged mids included): the median `bp_per_unit` and median `r2`. This is the sanity check: CKS report R² ≈ 65 % on US stocks. On a simulated one-tick book fed frame by frame through the feed, the best-level R² comes out at ≈ 0.7.
- **Separation:** when a measure separates ups from downs (every down at or below every up), the logistic slope has no finite estimate and its Wald z collapses towards 0. `ofi_ml` against its own interval can do this because levels are ranks: a quote moving a tick shifts every level. The fit then reports `separated`, slope None, and the score test's z (√n × the correlation). Ordinary fits are unchanged.

**Page:** the 20-level card shows OFI and OFI 20 next to DWI, both in the next-minute line and in a same-interval line with R². Off with the feed (`depth20.enabled`); nothing changes while it is off except the empty columns.

## GS-01..GS-03: the global-cue model on synchronised moves (`research/global_sync.py`), Phase 3 item 3

**Why:** the daily-close model regresses the Nifty's close-to-close move on the previous session's closes. Markets close at different hours, so part of that is already priced by India's close. This model uses only what moved between India's close and the next morning.

**Snapshots (GS-01):** at 15:31 ("close") and 08:42 ("pre") on trading days, the last 5-minute price of S&P 500 futures (ES=F), Nasdaq 100 futures (NQ=F), the Nikkei and the Hang Seng (both trading by 08:45 IST), Brent and gold futures, the dollar index and USD/INR, from Yahoo through `data/markets.py`'s wrapper (breaker and retry). Table `global_snapshot`.

**Model (GS-02):**
- One row per morning: each series' log move from the previous session's 15:30 snapshot to that morning's 08:45 one. The target is the Nifty's opening gap: the first `index_levels` reading between 09:15 and 09:30 against the previous session's last reading up to 15:30.
- Ridge regression (standardised inputs, like the daily model) once 40 mornings exist. Until then the status is COLLECTING, with the count.
- Walk-forward over the latest 60 mornings, each fitted only on the mornings before it (30 at least): RMSE against "no change" and against the GIFT estimate on the same mornings, plus the direction hit rate.
- Today's expected gap, with each series' contribution.

**Use (GS-03):** `capture_gift` stores the estimate in `market_cue.sync_expected_pct`; `gap_record()` scores GIFT, the daily model and this one; `/market-pulse` shows it in the global-cues card. API: `GET /api/market-pulse/global-sync`, `POST /api/market-pulse/global-sync/capture {label}`.

**Also fixed:** `evaluate_gaps` took the first Nifty reading from 09:15 with no upper bound, so a morning whose early readings were missing would have recorded a midday price as the open. It now uses only 09:15–09:30, as the new model does.

## NL-01..NL-03: English → screener query (`research/screener_nl.py`), Phase 4 item 1

**What it does:** turns a request such as "debt-free capital goods companies with ROCE above 20 % near their 52-week high" into `debt_equity < 0.1 AND from_52w_high_pct > -5 AND industry IN ("Capital Goods") AND roce_pct > 20`. It only ever writes ATIP's query language, which the screener's safe parser validates. Nothing runs until the user has seen, and can edit, the query.

**Claude path (NL-01):**
- `config.json` `"screener_ai": {"enabled": false, "model": "claude-sonnet-5-5", "daily_cost_cap_usd": 0.5}` (in `config_template.json`).
- One `messages.create` with a cached system prompt (grammar, every field with unit and aliases, scans, presets, the industries in the snapshot) and a JSON-schema structured output: `{query, sort, desc, explanation, unmatched}`.
- A query the parser rejects is sent back once with the parser's error; a second failure falls back to the rules. An unknown sort field is dropped.
- Usage and cost go to `ai_usage_log` (purpose `screener_nl`) and count against this feature's own daily cap. A 401 is reported as "Anthropic API key rejected (401)".

**Rule path (NL-02):** always available. Preset names (only when the request is the preset's name); "<field> above / below / at least / at most / between … and … <number>" with %, crore and 1,000-style numbers; phrases (debt-free, low debt, no pledge, near the 52-week high / low, above / below the 200-DMA, profitable, dividend payers, high ROCE / ROE / growth / dividend, low P/E, undervalued, uptrend, oversold, large / mid / small caps); scan names; industries; "sorted by …", "cheapest first". What it could not map is returned as `unmatched`.

**Page and API (NL-03):** "Ask in English" on `/screener` fills the query box and the sort, says whether Claude or the rules wrote it (and why Claude was not used), and lists what was not understood. `POST /api/screener/ask {text}` (`workspace:write`, token), 400 on empty or 500+ characters.

## MC-01..MC-03: read-only ATIP MCP server (`tools/atip_mcp.py`), Phase 4 item 2

**Transport (MC-01):** MCP over stdio, one JSON-RPC 2.0 message per line: `initialize` (protocol 2025-06-18, 2025-03-26 or 2024-11-05, echoed when supported), `ping`, `tools/list`, `tools/call`; notifications get no reply. Written to the specification directly, so no new dependency. Stdout carries only protocol messages: `sys.stdout` is pointed at stderr while serving, because some modules print on import.

**Tools (MC-02):** `screener_fields`, `screener_run`, `signals_today`, `signal_track_record`, `intraday_signals`, `stock_technicals`, `market_pulse`, `market_gate`, `research_report` (today's stored report, else built and not saved), `scorecard`, `event_calendar`, `open_orders` (reads the Dhan order book). All annotated `readOnlyHint`, none destructive; only `open_orders` is open-world. Bad arguments and missing data come back as tool errors (`isError`), not protocol errors; results over 200,000 characters are truncated with a note.

**Read-only by construction (MC-03):** the SQLite connection runs with `PRAGMA query_only`, so a reader that tried to write would fail rather than write. `CREATE TABLE IF NOT EXISTS` on existing tables still passes, so the existing readers work. The event calendar's one-time seeding now skips quietly on such a connection. MySQL / PostgreSQL deployments are read-only by the tool set alone.

**Set-up** (Claude Desktop `claude_desktop_config.json`, or `.mcp.json` for Claude Code):
```json
{"mcpServers": {"atip": {"command": "/path/to/ATIP-DEV/.venv/bin/python",
                         "args": ["/path/to/ATIP-DEV/tools/atip_mcp.py"],
                         "env": {"ATIP_DB_PATH": "/path/to/ATIP-DEV/atip_data/atip.db"}}}}
```

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

- Tables: `db/schema_w39.py` (`prices_daily_backfill`, `research_report`, `research_screen`, `technical_snapshot`, `technical_signal`, `order_book_pressure`, `fo_participant_oi`, `market_cue`, `market_regime_gate`, `fundamental_scorecard`, `macro_event`, `intraday_signal`, `depth20_snapshot`, `global_snapshot`), applied by `db/schema.py`. `W39_COLUMNS` adds `technical_signal.market_gate` / `alignment` / `market_status` / `weekly_agrees`, the forward columns (`ret_*`, `excess_*` at 5/20/60), the weekly snapshot columns, `market_cue.events` / `band_pct` / `sync_expected_pct` and the `depth20_snapshot` best-quote and OFI columns (OF-02) to databases created before them. Scoping: GLOBAL, except `research_screen`, which is OWNER. Privacy inventory: `order_book_pressure` (market, 90 days), `market_cue`, `market_regime_gate`, `fundamental_scorecard`, `macro_event` and `intraday_signal` (research, kept), `depth20_snapshot` (market, 90 days) and `global_snapshot` (market, kept) added; the rest are classified by the existing rules.
- Retention (`db/purge.py`): `technical_snapshot` in the LONG tier (600 days), `order_book_pressure` in the SHORT tier (90 days). Signals and cues are kept.
- Routes: `dashboard/w39_routes.py`, registered in `server.py`. Authz rules: `POST /api/options/(build|analyse)` → `research:run`; `POST /api/screener/` → `workspace:write`; `POST /api/signals/` and `POST /api/(market-pulse|orderbook|market-regime)/` → `research:run`. GETs fall under the existing read rules (`/api/brokers/` → `portfolio:read`).
- Scheduler (`_schedule_w39_jobs`): order-book poll every 15 minutes (market hours only); GIFT capture 08:45 and 09:05; gap evaluation 09:35; participant OI 20:15; technical signals 20:30; research reports 20:40; scorecards and saved screens 20:50; intraday scans every 15 minutes 09:45–15:50; global snapshots 15:31 and 08:42; history backfill 22:20; Nifty history 23:20. Each job is logged in `pipeline_log` through `run_job`.
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
| `tests/test_w39_market_pulse.py` | 15 | the global model recovers a planted 0.5 S&P beta and beats "no change" walk-forward; INSUFFICIENT on short history; GIFT gap against yesterday's 15:30 GIFT (not the 23:00 reading or the spot close) and the open check; FII streak, absorption and label; build-up truth table; crowded short is bullish only when covering; OI walls; order-book parse and labels; persistence needs the newest poll; a factor that stops updating is left out, not repeated; participant-OI CSV with title line and tab-polluted headers; open orders keep only open statuses and report Dhan errors; pulse; API, token and 400s; permissions and tables |

| `tests/test_w39_screener_nl.py` | 11 | five everyday phrasings to exact queries (units, 1,000 crore, between, sorted by, cheapest first, industries, scans); presets only when named outright; unmatched words; Claude path with a fake client: cached catalogue, structured output, usage logged; a rejected query retried once with the parser's error, twice falls back; 401, the cost cap and the off switch fall back with the reason; empty / long text; API, token, page |
| `tests/test_w39_mcp.py` | 7 | handshake, version negotiation, ping, notifications, unknown methods; 12 tools all annotated read-only; screener tools and their errors; stock tools and missing data as tool errors; market tools on a query-only connection, which rejects an INSERT; stdout carries only protocol messages even when a module prints; a real subprocess session over stdio |
| `tests/test_w39_depth20.py` | 9 | binary frames with bid and ask messages and a truncated tail; DWI against a hand calculation, the far level excluded; sampling every 15 s, storage, the subscription message; Dhan's 806 parks the feed with its reason; a session subscribes then reads frames; batches of 50; persistent flag; validation recovers a planted slope and calls noise not predictive; API, page, permissions |
| `tests/test_w39b_ofi.py` | 19 | e_n by hand: bid and ask price up / down / unchanged, quantity changes, both sides vs separate messages, deeper levels and levels that appear or empty; sums between snapshots restart, crossed books skipped; multi-level normalisation (400× deeper stock scores the same, weights, missing levels as 0 depth); the frame path (every message counted, another stock ignored, stored values and best quotes); packing of messages into frames; a reconnect restarts the sums; an old table gets the columns and keeps its rows; off by default; validation recovers a planted contemporaneous and a planted next-minute OFI relation and calls noise not predictive; too few pairs are not called; separated moves get the score z; a simulated book through the feed shows the strong contemporaneous relation (R² ≈ 0.7); API and page |
| `tests/test_w39_global_sync.py` | 7 | capture and a failed fetch; morning moves from the previous session's close, across a holiday; the gap needs a reading by 09:30; COLLECTING under 40 mornings; a planted sensitivity recovered walk-forward, beating "no change" and a noisier GIFT; capture_gift stores the estimate and the record scores it; API and permissions; the open check ignores a reading after 09:30 |
| `tests/test_w39_intraday.py` | 10 | opening-range breakout needs 1.5× slot volume and a first close beyond the range by 11:30, 30-minute range, breakdown; open = low / high on the first hour, broken and too-small cases; squeeze after 6 tight bars released up and down, not without history; only completed 15-minute bars load; stored once, seen at the bar's completion, record to the close vs the Nifty with best / worst; a short gains on a fall; alerts held until a positive 30-hit record; the existing scanner no longer mixes 1-minute bars; API and page; permissions |
| `tests/test_w39_event_calendar.py` | 16 | New York → IST across daylight saving, the Fed after midnight in winter; the session each event hits (next open, holidays skipped; RBI intraday); seeded dates by session and upcoming; config events, bad kinds and dates, add, soft delete that survives re-seeding; band = usual miss, assumed ×1.5, measured ×2.5 once enough mornings, RBI not widened; old mornings looked up; capture stores events and band, pulse explains; old market_cue gets the columns; API, token, 400 / 404, page; permissions and classification |
| `tests/test_w39_scorecard.py` | 12 | value checks with their numbers; a loss-maker fails the earnings checks; growth vs a savings rate and the market, self-funded growth; non-payer fails and unknown skips the dividend axis, quartiles and cover; banks not scored on debt, debt-free passes, net debt from debt and cash; past checks from stored quarters (3-year EPS, acceleration, margin, debt trend); point-in-time history in %; dividends parsed from the calendar and split-adjusted, under-2-year calendar unknown; screener fields, presets, for_symbol and the 20:50 job storing them; record by band, one sample a month, too-few guard, bad horizon; API 200 / 404 / 400 and the page; permissions and classification |
| `tests/test_w39_delivery.py` | 4 | the spike needs the ratio, the 30 % level and an up day, and no data means no hit; snapshot fields; bars read from prices_daily and stored by a run that signals; preset |
| `tests/test_w39_rs_line.py` | 7 | RS-line new high on the first day only; RS line leads while price is below its high, and not when price is at a high; snapshot flag; AMFI cut-offs and RS within each group; market caps from the latest shares on or before the date; run stores group and rank; screener fields and presets |
| `tests/test_w39_patterns.py` | 11 | Darvas box and breakout, still-inside and steady-climb negatives, breakdown; VCP contractions, dry-up and volume-confirmed breakout, widening pullbacks and no dry-up rejected; double bottom breakout and uneven lows rejected; ascending triangle; head-and-shoulders breakdown; pattern scans carry their levels; snapshot lists setups; screener fields and presets; pattern alerts held until 30 closed with positive R |
| `tests/test_w39_weekly.py` | 10 | weekly bars aggregate and keep only completed weeks; a Friday-holiday week counts from the next Monday; the weekly rating needs 35 weeks and follows the trend; agreement truth tables; snapshot fields; run stores weekly fields and tags signals; forward split by weekly agreement; screener fields and presets; old snapshot table gets the columns; page |
| `tests/test_w39_track_record.py` | 6 | forward returns signed for direction and measured against the Nifty on the same sessions; filled as sessions pass, never rewritten; stats by scan × gate and by confluence band with n / beat % / median / mean; today's signals carry their scan's record in today's market; gate and status at birth for older signals; API and 400 |
| `tests/test_w39_regime_gate.py` | 20 | distribution day needs the drop and higher volume; expiry after 25 sessions or a 5 % rally; pressure → correction → rally attempt → follow-through day with the count cleared; no follow-through before day 4 or without volume; a lower close resets the rally; a 10 % slide is a correction; a failed follow-through; 200-DMA and warm-up; replay is point-in-time; alignment truth table; market volume over stocks on both days; storage with Yahoo + market_health; signals tagged and against-market ones never alerted; old signals get their own day's gate; gate-effect only speaks beyond noise; old table gets the new columns; pulse; API, token and 400s; permissions |

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
- End of day; the intraday scans are separate (IN-01..03) and have their own record.
- Track records start on the first run and are measured on ATIP's own prices. Trust a scan only after ~30 closed signals.

**Chart patterns:**
- Rule-based approximations of patterns drawn by eye; they will miss some textbook shapes and catch some that a trader would reject.
- Closes only for breakouts (no intraday trigger).
- Inverse head and shoulders, descending triangles, channels and wedges are not built yet.

**Track record by horizon:**
- Measured from the signal day's close, not the next open.
- Excess is against the Nifty 50, not a sector or size benchmark.
- With a few weeks of signals most cells are below 10. The record becomes useful after a few months.

**Fundamental scorecard:**
- Growth checks use reported growth, not forecasts; a cyclical at a peak scores well on growth just before it turns.
- The dividend history checks need two years of NSE's corporate-action calendar; until then they use stored yields or stay unknown.
- Banks and NBFCs miss six checks by design: their scores are only comparable with other financials.
- Its record starts the night the 20:50 job first stores scorecards; with one sample per stock per month, a band needs months before it speaks.

**Market regime gate:**
- The thresholds are ATIP's choices in the IBD tradition, not IBD's published rules. Whether they help on Indian stocks is what "Does the market gate help?" will show once 30+ signals have closed on each side.
- Market volume is the volume of the stocks ATIP stores, not exchange-wide volume; days with fewer than 10 common stocks have no comparison.
- Stalling days and the intraday-low rule for rally attempts are not modelled (closes only).

**English → screener query:**
- The rule translator knows the phrasings listed above; anything else is reported as not understood rather than guessed. The Claude path needs a working Anthropic key (KD-001).

**MCP server:**
- Local stdio only (no hosted / HTTP transport); it reads the same database the platform writes.
- `open_orders` and `research_report` can be slow (a broker call; a report built on the fly).

**20-level depth:**
- Off by default; it needs the Dhan Data API and one connection (Dhan allows a limited number at once).
- OFI is built from Dhan's 20-level snapshots (each message is a full side), not from an order-by-order feed. Changes between two messages are netted: an order added and cancelled between them is invisible.
- `ofi_ml` follows the interval's price move by construction (a quote moving a tick counts at every level), so its same-interval fit is close to perfect. The best-level fit is the meaningful sanity check.
- The integrated multi-level OFI uses fixed weights (equal by default), not Cont, Cucuringu & Zhang's fitted principal component.
- The 15-second sample is taken on the first message 15 seconds after the last one, sometimes between a frame's bid and ask messages. The rest of the frame counts toward the next snapshot, so no flow is lost.
- Snapshots every 15 seconds miss faster changes in the stored measures (OFI itself is summed over every update); the 1-minute validation is the shortest horizon it supports.

**Synchronised global model:**
- Needs 40 mornings of both snapshots (about two months) before it gives an estimate.
- Yahoo's futures quotes can lag by up to 10–15 minutes; the snapshot stores each quote's own time.
- The dollar index and USD/INR barely move overnight; the ridge penalty keeps their weights small when they carry nothing.

**Intraday scans:**
- They need the Dhan Data API (the 15-minute bar job) and 3 sessions of stored bars before the volume filter can work.
- Bars arrive every 30 minutes, so a hit is seen up to 30 minutes after its bar; the record is measured from the price when it was seen.
- No intraday stop / target: the record is to the session's close, with the best and worst move along the way.

**Event calendar:**
- Seeded dates only reach the end of 2026 for US CPI and payrolls, and to February 2027 for the RBI; add the next year's dates each December (config or the page). Unscheduled meetings must be added by hand.
- Only scheduled releases: no Union Budget, US GDP, PCE or ECB, and no surprise events.
- The ×1.5 widening is an assumption until 10 mornings after a US release are evaluated.

**Market pulse:**
- The global model uses daily closes (previous session), not synchronised 15:30 → 08:45 moves; it needs the Nifty and global history loaded.
- GIFT capture needs Dhan index quotes; participant OI needs NSE to answer (Indian connection).
- Order-book pressure needs the Dhan Data API and is context, not a signal.
- The early-October 2026 market figures in the plan doc are press reports, not verified.

**Next on the roadmap** (details and order in `docs/ANALYSIS_TOOLS_AND_SIGNALS_PLAN_2026-10.md` §8):
- MF analytics / SIP, tax P&L, earnings-surprise signal
- Live execution only with the owner's explicit go-ahead
