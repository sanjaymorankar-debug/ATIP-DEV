# Analysis tools, signals and the market-pulse plan (7 October 2026)

**What this covers:**
- How the leading fundamental, technical and AI analysis tools (free and paid, Indian and global) analyse stocks and generate signals.
- How global markets, FII flows and pending orders affect the Indian market, and what the evidence says is usable.
- What ATIP built in this round (technical screener, signal engine, market pulse, order-book pressure, open-orders view).
- The best plan for what to build next, in order.

**How it was compiled:**
- **Tools and evidence:** web research on 2026-10-07. Most vendor sites blocked direct fetching, so many facts come from search summaries. Anything marked **[unverified]** came from a single third-party page or could not be confirmed.
- **ATIP side:** the code on this branch. Companion documents: `docs/ATIP_GAP_ANALYSIS_2026-10.md` (brokers and institutional research) and `docs/W39_RESEARCH_HISTORY_OPTIONS_HANDOFF.md` (what W39 built, with tests).

Nothing here is investment advice. ATIP's signals are for the owner's own use; sharing them with others needs SEBI Research Analyst registration.

---

## 1. In one page

**What the best tools have in common.** No serious product trusts one indicator. They:
1. **Vote across many indicators**, with a neutral band, so one indicator can't flip the call (TradingView, Barchart, Investing.com, Trendlyne).
2. **Rank instead of using fixed thresholds** (IBD/MarketSmith RS 1–99, StockCharts SCTR 0–99.9 within a cap bucket, Seeking Alpha sector-relative grades).
3. **Demand volume confirmation** for breakouts (StockCharts, pocket pivots, MarketSmith Buyer Demand).
4. **Evaluate completed candles only**, so signals don't repaint (Dhan ScanX).
5. **Gate signals by market regime** (IBD distribution days and follow-through days).
6. **Publish a track record per signal** (Tickeron odds of success, Danelfin probability advantage, Trade Ideas Holly's nightly backtests).

**What ATIP now does (this round):**
- **Technical screener:** 39 end-of-day scans (6 chart-pattern breakouts, 2 RS-line scans and a delivery spike added in Phase 2) and 19 candle patterns, plus a technical rating, an IBD-style RS rating and the order-book pressure as screener fields. They mix freely with the fundamentals in one query language, and there are 21 technical and 6 combined presets (Phase 2 added a weekly rating, the daily / weekly agreement, chart patterns and the DVM view; Phase 3 a 75-minute rating and the daily / 75-minute agreement).
- **Signal engine:** every scan hit gets an entry, a stop (2 × ATR), a target (4 × ATR) and a confluence count out of 6. Each signal is followed until it hits its target or stop, or 20 sessions pass. Each scan then shows a win rate and average R.
- **Market pulse:**
  - a global-cue model fitted on ATIP's own data, with its walk-forward record;
  - the GIFT Nifty gap, measured the right way (basis-free) and checked against the actual open;
  - FII flow pressure, including the part of the flow returns don't explain;
  - FII derivatives positioning from NSE's participant-wise OI, with a rule built to avoid the contrarian trap that failed in 2026;
  - option OI walls;
  - market-wide pending-order pressure.
- **Your pending orders:** Dhan open orders and forever orders (read-only), plus ATIP's own resting paper orders and target/stop rules, on one page.

**The best plan from here** (section 8):
- **Phase 2 (uses data ATIP already has):** regime gate, a per-signal record split by regime and horizon, weekly-timeframe rating, chart patterns, RS-line highs and size-group ranks, delivery spikes, an explainable fundamental composite with a DVM-style three-axis view and an event calendar (all **built**, see §8).
- **Phase 3:** intraday scans, depth imbalance and a 75-minute rating from the stored 15-minute bars (needs the Dhan Data API).
- **Phase 4:** natural-language screening and a read-only ATIP MCP server (**built**; English screening works without a working Anthropic key through a rule translator, and better with one).
- **Phase 5:** live execution, only with your explicit go-ahead.

---

## 2. How the leading tools analyse stocks

### 2.1 Technical tools

| Tool | Cost | How it analyses | How it signals | Track record shown |
|---|---|---|---|---|
| **TradingView** Technical Ratings | Free; real-time and alerts paid | 26 votes of −1/0/+1: 15 moving averages (SMA/EMA 10–200, Hull, VWMA, Ichimoku) and 11 oscillators (RSI, Stoch, CCI, ADX, AO, Momentum, MACD, StochRSI, Williams %R, Bull Bear Power, UO). MA mean and oscillator mean are averaged. Any timeframe. | Strong Sell < −0.5 < Sell < −0.1 < Neutral < 0.1 < Buy < 0.5 < Strong Buy. Pine Screener runs custom logic on up to 1,000 symbols. | No |
| **StockCharts** SCTR | $19.95–49.95/month | 30 % distance from EMA200, 30 % ROC125, 15 % distance from EMA50, 15 % ROC20, 5 % PPO-histogram slope, 5 % RSI14 | Percentile 0–99.9 **within** large, mid or small cap. Predefined scans (52-week highs, volume > 4 × 20-day average, 50/200 crosses, candlesticks, P&F). | No |
| **Barchart** Opinion | Free | 13 indicators in short (≈20 d), medium (≈50 d) and long (100–200 d) groups | % Buy/Sell per group, plus *Strength* (vs the signal's own history) and *Direction* (3-day change) | No |
| **Investing.com** | Free | 12 MA votes and ~12 oscillator votes | Strong Buy … Strong Sell (cut-offs [unverified]) | No |
| **Finviz** | Free; Elite $39.50/month | Signal filters: new highs/lows, unusual volume, overbought/oversold, plus chart patterns (channels, triangles, wedges, double tops/bottoms, head and shoulders) | Screener filters | Elite backtests |
| **TrendSpider** | ≈ $107–197/month | Automatic trendlines, Fibonacci, candle and chart patterns, synced multi-timeframe, Raindrop charts. AI Strategy Lab trains user models (Naive Bayes, logistic regression, k-NN, random forest). | Alerts and bots | Backtests |
| **Trade Ideas** Holly | ≈ $169–228/month | Backtests 60–70 strategies every night; only those that pass its statistical bar trade the next day | 5–25 intraday signals a day, each with entry, stop and target | Live win % per strategy |
| **Tickeron** | ≈ $25–250/month | AI pattern search; Trend Prediction Engine | "Odds of success" per pattern on that ticker, plus a confidence % | Yes, per pattern |
| **Chartink** (India) | Free (5-minute delay); ₹780/month | Visual scan builder mixing technical and fundamental conditions | Popular scans: 15-minute breakout, open = high/low, Bollinger squeeze, engulfing, MA crosses. Alerts by SMS, email or webhook. | No |
| **Streak** (Zerodha) | Free to Zerodha users | 70+ predefined scans, 100+ indicators, universes such as Nifty 50/500 and F&O | Scanner, alerts, strategy deploy | Backtests |
| **Dhan ScanX** | Free | Golden/death cross, RSI zones, S/R breakouts, squeezes, candlesticks; custom screens mixing technicals, financials and shareholding | **Completed candles only** (no repainting) | No |
| **StockEdge** | ₹2,499/month (Club) | 70+ technical, 60+ fundamental and F&O scans; combination scans (up to 10); AI chart patterns; sector rotation from breadth and delivery | Scans | No |

### 2.2 Fundamental and combined research tools

| Tool | Cost | Method | Output |
|---|---|---|---|
| **Screener.in** | Premium ₹4,999/year | Query language over 10 years of Indian financials; Piotroski and Darvas screens | Screens, filing alerts. Screener AI answers from annual reports and concalls. |
| **Trendlyne** DVM | Freemium | Durability, Valuation and Momentum, each 0–100. Momentum uses 20–30+ indicators at end of day (> 70 strong, < 35 weak). | DVM class, SWOT from rule screens, checklists, Piotroski. Also an AI query mode and an MCP server. |
| **Tickertape** Scorecard | Freemium | Performance, valuation, growth and profitability, each 0–10 | Red flags (pledge, ASM/GSM, default probability), entry point |
| **MarketSmith India** (IBD style) | ₹13,900/year | EPS Rating 1–99, RS Rating 1–99, Buyer Demand A–E, Group Rank of 197 groups, Master Score | CAN SLIM checklist, 7 base patterns |
| **Value Research** | Free | Quality, Growth, Valuation and Momentum (each out of 10) | 1–5 stars |
| **Simply Wall St** Snowflake | ≈ $11/month | 5 axes × 6 pass/fail checks | The most explainable composite |
| **Morningstar** | Paid | Analyst DCF fair value, uncertainty, moat | Stars from price ÷ fair value against uncertainty bands |
| **Seeking Alpha** Quant | ≈ $299/year | 100+ metrics graded within sector: Value, Growth, Profitability, Momentum, EPS Revisions | Score 1–5 → Strong Sell … Strong Buy |
| **Zacks Rank** | Freemium | Estimate revisions (agreement, magnitude, upside, surprise), recomputed nightly | 5 buckets plus Style Scores |
| **GuruFocus** GF Score | Paid | Five 1–10 ranks (financial strength, profitability, growth, GF Value, momentum), backtested weights | 0–100 |
| **Stock Rover** | ≈ $29–149/month | Percentile scores for value, growth, quality, sentiment, momentum, dividend | 0–100 each |
| **Danelfin** | Free; $19–79/month | ML on ≈ 900 features (600 technical, 150 fundamental, 150 sentiment) → probability of beating the market over ≈ 60 sessions | AI Score 1–10 plus the top driving signals; backtest claims are self-reported |
| **Kavout** K Score | Paid | Ensemble over 200+ factors | 1–9 (30-day upside probability) |
| **LSEG StarMine** (institutional) | Institutional | Analyst revisions, SmartEstimate, a static blend of 8 sub-models | 1–100 ranks |

### 2.3 AI features, 2024–2026

| Product | What it does |
|---|---|
| TradingView AI Copilot, AI Screener, MCP server (public beta 16 Sep 2026) | Chart assistant; natural language → screener filters; MCP access for Essential plans and above |
| Zerodha Kite MCP (May 2025, free) | Holdings, positions, orders, quotes and GTTs from Claude and similar assistants |
| Dhan MCP | Market data, orders, alerts and margins |
| Screener.in AI, Trendlyne AI mode and MCP | Answers from filings; English → screen |
| Perplexity Finance (India since Aug 2025) | Live NSE/BSE concall transcripts, results calendar, natural-language screening |
| Fiscal.ai (ex-FinChat) | Sourced answers, KPI and segment data, API and MCP |
| Bloomberg ASKB, FactSet Mercury, Morgan Stanley AskResearchGPT | Agentic assistants over proprietary research |

**The pattern:** AI is used as an *interface* (English to screen, chat over filings, MCP), not as the signal itself. The signals that ship with track records (Danelfin, Tickeron, Holly) are statistical models with published hit rates.

---

## 3. What separates good signals from noise, and how ATIP applies it

| Practice | Who does it | ATIP now |
|---|---|---|
| Vote across many indicators with a neutral band | TradingView, Barchart, Trendlyne | ✅ `research/technicals.py rating()`: 11 votes (close vs SMA20/50/200, EMA9/21, SMA50/200, Supertrend, RSI, MACD, ADX/DI, Bollinger). Same bands as TradingView (±0.1, ±0.5). It is a single mean, simpler than TradingView's MA/oscillator split. |
| Rank, don't threshold | IBD, SCTR, Seeking Alpha | ✅ RS rating 1–99 across ATIP's universe (0.4 ROC63 + 0.2 ROC126 + 0.2 ROC189 + 0.2 ROC252, the community replica of IBD). 🟡 No cap-bucket ranking yet. |
| Volume confirmation | StockCharts, O'Neil | ✅ 52-week breakout needs volume > 1.5 × the 20-day average; pocket pivot needs volume above the largest down-day volume of the last 10 sessions; volume > 1.5 × counts as confluence |
| Completed candles only | Dhan ScanX | ✅ Scans run at 20:30 on stored daily bars; a stock with no bar for the day is skipped rather than reusing yesterday's |
| Regime gate | IBD | ✅ `research/regime_gate.py` (Phase 2, item 1): distribution days, correction / rally attempt / follow-through day and the 200-DMA give an OPEN / CAUTION / CLOSED gate. Every signal carries the gate it was born under; signals against it are hidden by default and never alerted. ATIP's `market_health` regime still counts as one confluence factor. |
| Independent confluence | Trade Ideas, analysts | ✅ Out of 6: technical rating, volume, relative strength, regime, a candle pattern, ATIP's research rating |
| Track record per signal | Tickeron, Danelfin, Holly | ✅ `technical_signal`: each signal closes as TARGET, STOPPED or EXPIRED (20 sessions); per-scan win rate, average R and average return, filterable by minimum confluence. A bar touching both levels counts as STOPPED (conservative). Phase 2: also split by the market gate at birth, plus forward returns vs the Nifty at 5/20/60 sessions by scan, gate and confluence, shown next to each of today's signals. |

---

## 4. Global markets, FII flows and the Indian market

### 4.1 What the evidence says

- **US → India runs mostly one way.**
  - The US (S&P, Nasdaq) Granger-causes the Nifty.
  - Nasdaq's daytime return predicts the Nifty's next overnight (close-to-open) move.
  - Asian markets (Hang Seng, Kospi) also lead the Nifty open.
  - The S&P–Nifty correlation has fallen over time and rises in crises, so any fixed beta goes stale. Fit it on a rolling window.
- **Crude, the rupee and gold.**
  - Brent explains only a few percent of Nifty variance [unverified].
  - The Nifty, USD/INR and Brent are connected.
  - Gold's link is weak.
  - FII flows and the rupee have been highly correlated recently.
- **GIFT Nifty is the best single pre-open input.** The offshore contract leads price discovery.
  - **The trap:** GIFT is a *future*, so its premium over the spot close includes carry. Measure its move against **GIFT's own price at 15:30 the previous day**, not against the Nifty close.
  - Vendor claims of 80–90 % direction accuracy are unaudited, so ATIP measures its own.
- **FII flows mostly *follow* returns.** Flows lead returns only in the very short term, so the raw daily FII figure is largely an echo of what the market already did. The informative part is the **surprise**: the flow that recent returns don't explain.
- **Derivatives positioning is read with the cash flows.**
  - NSE's participant-wise OI file gives FII, DII, Pro and Client longs and shorts every evening.
  - **The usual contrarian rule failed through 2026.** "FII index-futures long % below 15 % means a bounce is coming" did not work: FIIs stayed heavily short (long % ≈ 8 % in early October) while the index kept falling.
  - Crowded shorts mattered only when they were actually being covered.
- **Context as reported in early October 2026 [unverified press figures]:**
  - Nifty ≈ 22,400 on 1 Oct (−14 % YTD) after its longest weekly losing streak in 25 years.
  - FPI outflows ≈ ₹2.7 lakh crore YTD.
  - INR ≈ 95.9/USD, a record low.
  - US 10-year yield ≈ 5.27 %, the highest since 2002.

### 4.2 What ATIP built (`research/market_pulse.py`, page `/market-pulse`)

| Part | Method | Honest limits |
|---|---|---|
| **Global-cue model** | Ridge regression on a rolling 250 sessions. It regresses Nifty daily log returns on the previous session's S&P 500, Nasdaq, Nikkei, Hang Seng, Brent, dollar index, USD/INR and gold moves and the US 10-year yield change (bp). Outputs: today's expected move, each factor's contribution, sensitivity with explicit units, 60-day correlations, and a walk-forward record (direction hit rate on days that moved > 0.2 %, RMSE vs a "no change" forecast). | Daily closes, not synchronised 15:30→08:45 moves. It reports "INSUFFICIENT" under 80 overlapping sessions. Run `python -m research.market_pulse nifty-history` once to load 5 years of the Nifty. |
| **GIFT Nifty gap** | Captured at 08:45 and 09:05. Move = GIFT now ÷ GIFT at ≈ 15:30 yesterday (basis-free). Expected gap = move × a fitted beta (1.0 until 30 mornings exist). At 09:35 the actual open (first reading from 09:15) is filled in, and the page shows the direction hit rate for both GIFT and the global model. | Needs Dhan index quotes and stored `index_levels` |
| **FII flow pressure** | z-score of the 5-day FII net; the flow **surprise** (residual after regressing flows on today's and the last two days' returns); selling streak; DII absorption (DII buying ÷ FII selling); MTD and YTD; USD/INR 20-day change. Score = 0.5 z(5-day) + 0.3 z(surprise) + 0.2 z(−ΔINR), shown as STRONG_INFLOW … STRONG_OUTFLOW. | A context variable for 1–20 days, not a next-day trade |
| **Positioning** | FII index-futures long % with its 5-day change and percentile; net futures, calls and puts; client long %; Nifty futures build-up (long build-up, short build-up, short covering, long unwinding, counted only when \|ΔOI\| > 2 % and \|Δprice\| > 0.3 %); PCR z-score. The read is CROWDED_SHORT_COVERING **only** when long % rose more than 5 pp in 5 days **and** futures show short covering. Otherwise a crowded short is reported as a headwind. | NSE serves the file to Indian connections; nightly at 20:15 |
| **OI walls** | Nearest-expiry max call OI above spot and max put OI below spot, shown as distance from spot | Display only. An IIMB study finds no max-pain effect in India. |
| **Market context** | Combines global cues, GIFT, FII pressure, positioning and the ATIP regime into RISK_ON / NEUTRAL / RISK_OFF, listing every reason | A summary, not a forecast. Each part keeps its own record. |

---

## 5. Pending orders: the market's and yours

### 5.1 The market's pending orders (`data/order_pressure.py`)

- **The data.** Dhan's quote call returns, per stock, the **total pending buy and sell quantity across the whole book** plus 5 levels of depth, for up to 1,000 instruments per request. Every 15 minutes in market hours, ATIP stores for every tracked stock:
  - the totals;
  - `total_imbalance` = (buy − sell) ÷ (buy + sell);
  - the top-5 imbalance and spread;
  - a label from STRONG_BUYERS to STRONG_SELLERS.
- **Persistence.** A stock is flagged "persistent buyers/sellers" only if one side passed ±0.2 in 3 of the last 4 polls, **including the newest**. Persistent pressure is harder to fake than one reading.
- **Where it shows:**
  - `/market-pulse`: market-wide buy/sell ratio, most bid, most offered, persistent lists;
  - the screener: `book_imbalance`, `book_pressure`, `book_persistent`, and the preset "Buyers queuing, chart positive";
  - the API: `/api/orderbook/pressure[/{symbol}]`.
- **Limits, from the evidence:**
  - Book imbalance predicts price over seconds to minutes. In NSE's most active stocks the effect is strong for 5 minutes and gone within 30.
  - Totals include orders far from the market.
  - Visible size can be spoofed (SEBI's 2025 Patel Wealth order) or hidden (icebergs).
  - So this is **context ("who is waiting where right now")**, not a signal on its own, and the page says so.

### 5.2 Your pending orders (`portfolio/open_orders.py`, `GET /api/brokers/open-orders`)

- **Dhan:** the order book filtered to open statuses (TRANSIT, PENDING, PART_TRADED and the trigger and confirm states), plus forever (GTT) orders. **Read-only**, and reading needs no static IP.
- **ATIP:** resting paper orders (PENDING, PARTIALLY_FILLED) and waiting target/stop rules (ACTIVE, PENDING_CONFIRMATION).
- **Where:** shown at the bottom of `/market-pulse`.

---

## 6. What was built in this round

| Feature | Code | Where you see it | Schedule |
|---|---|---|---|
| Indicators, 39 scans (incl. 6 chart-pattern breakouts, 2 RS-line scans and a delivery spike), 19 candle patterns, technical rating | `research/technicals.py`, `research/patterns.py` | — | — |
| Technical snapshot, RS rating, signals with levels, confluence and outcomes | `research/tech_signals.py` (`technical_snapshot`, `technical_signal`) | `/signals` (Today, Track record); alerts (category "signals") | 20:30 daily |
| Technical + combined screener | `research/screener.py` (139 fields, `CONTAINS`, 44 presets, columns that follow the query) | `/screener` | Saved screens 20:50 |
| Market pulse | `research/market_pulse.py` (`market_cue`) | `/market-pulse` | GIFT 08:45 and 09:05; gap check 09:35; Nifty history 23:20 |
| Participant OI | `data/participant_oi.py` (`fo_participant_oi`) | `/market-pulse` | 20:15 |
| Order-book pressure | `data/order_pressure.py` (`order_book_pressure`, kept 90 days) | `/market-pulse`, screener | Every 15 minutes in market hours |
| Your open orders | `portfolio/open_orders.py` | `/market-pulse` | On page load |
| Market regime gate (Phase 2, item 1) | `research/regime_gate.py` (`market_regime_gate`; `technical_signal.market_gate`, `alignment`) | `/market-pulse` (gate card and chart), `/signals` (banner, filter, "Does the market gate help?") | With the 20:30 signal run |
| Weekly rating and daily / weekly agreement (Phase 2, item 3) | `research/technicals.py` `weekly_bars`, `weekly_rating`, `mtf_alignment`; `technical_snapshot.*_w`, `technical_signal.weekly_agrees` | `/signals` Weekly column and "Does the weekly trend add?"; screener fields and 2 presets | With the 20:30 signal run |
| 75-minute rating and daily / 75-minute agreement (Phase 3) | `research/technicals.py` `bars_75`, `rating_75`; `technical_snapshot.*_75`, `mtf_alignment_75` | Screener fields and the preset "75-minute and daily both bullish" | With the 20:30 signal run |
| DVM view (Phase 2, item 7) | `research/dvm.py`; `fundamental_scorecard.dvm_d / dvm_v / dvm_m / dvm_zone` | `/research` (next to the scorecard), screener fields and 3 presets | Stored with the 20:50 scorecards |
| Track record by regime and horizon (Phase 2, item 2) | `research/tech_signals.py` `evaluate_forward`, `forward_stats` (`technical_signal.ret_*` / `excess_*` at 5/20/60, `market_status`) | `/signals`: a record next to each scan; Track record → "Against the Nifty, by market and holding period" | With the 20:30 signal run |

**API:**
- `GET /api/signals/technical[...]`
- `GET /api/market-pulse[/global|/fii|/positioning]`
- `GET /api/orderbook/pressure[/{symbol}]`
- `GET /api/brokers/open-orders`
- POST actions to recompute, capture GIFT, refresh NSE files, or poll the book. These need the dashboard token and the `research:run` permission.

The full list is in `docs/API_REFERENCE.md`.

**Tests:**
- `tests/test_w39_technicals.py`
- `tests/test_w39_market_pulse.py`
- `tests/test_w39_screener.py`

They use synthetic series with known answers. Examples:
- the global model must recover a planted 0.5 S&P beta;
- a golden cross fires only on the crossing day;
- a 52-week breakout without volume does not fire;
- a crowded short without covering is not called bullish.

---

## 7. Gaps that remain (ATIP vs the best tools)

| Gap | Best-in-class | Phase |
|---|---|---|
| ~~Distribution-day regime gate; follow-through day~~ | IBD / MarketSmith | 2: **built** |
| ~~Track record by regime; forward returns at 5/20/60 days vs Nifty~~ | Tickeron, Danelfin | 2: **built** |
| ~~Weekly technical rating; 75-minute rating~~ | TradingView any-timeframe | 2 / 3: **built** |
| Chart patterns: Darvas box, VCP, ascending triangle, double bottom, head and shoulders (**built**, Phase 2); channels, wedges, inverse head and shoulders, descending triangle | Finviz, TrendSpider, StockEdge | 2 (rest: later) |
| ~~RS-line new high; rank within cap bucket~~ | IBD, StockCharts | 2: **built** |
| ~~Delivery-% spike scan (NSE `DELIV_PER`)~~ | StockEdge, Chartink | 2: **built** |
| ~~Explainable fundamental composite (Snowflake-style 5 × 6 checks); a DVM-style three-axis view~~ | Simply Wall St, Trendlyne | 2: **built** |
| ~~Intraday scans: 15-minute opening-range breakout, open = low/high, intraday squeeze~~ | Chartink, Streak | 3: **built** |
| Depth-weighted imbalance from 20-level depth (**built**, Phase 3); order-flow imbalance (OFI) from quote changes | Institutional microstructure | 3 (OFI: later) |
| ~~English → screener query~~ | TradingView AI Screener, Trendlyne, Screener.in | 4: **built** |
| ~~ATIP MCP server~~ | Kite MCP, Dhan MCP, TradingView MCP, Trendlyne MCP | 4: **built** (read-only) |
| ~~Event calendar (FOMC, US CPI, RBI) widening the gap forecast~~ | Institutional desks | 2: **built** |
| Consensus estimates and revisions | Zacks, StarMine | Needs a licensed feed |
| Live orders from signals | Streak, Trade Ideas | 5, with your go-ahead |

---

## 8. The best plan, in order

**Rule for every phase:** a new signal ships with its own track record. It goes into Telegram alerts only after it has at least ~30 closed signals and positive expectancy on ATIP's own stocks. That is what separates Tickeron, Danelfin and Holly from scan lists.

### Phase 1: done in this change
- Technical screener, signal engine with track record, market pulse, participant OI, order-book pressure, open-orders view.

### Phase 2: next, using data ATIP already stores (no new subscriptions)
1. **Regime gate. Built** (`research/regime_gate.py`).
   - **Distribution day:** the Nifty down ≥ 0.2 % on higher market volume (the summed volume of the stocks ATIP stores, compared like for like). It counts for 25 sessions, or until the Nifty closes 5 % above it.
   - **Status:** confirmed uptrend; under pressure at 4 distribution days; correction at 6, at 10 % off the uptrend's high, or on a close below the low a follow-through day launched from. In a correction, the first up close after the low starts a rally attempt. A follow-through day (day 4 or later, Nifty up ≥ 1.25 % on higher volume) restores the uptrend and clears the count.
   - **Gate:** OPEN (confirmed uptrend above the 200-DMA), CAUTION (under pressure, or an uptrend still below the 200-DMA), CLOSED (correction or rally attempt). Thresholds are configurable (`config.json` `"regime_gate"`).
   - **Signals:** each carries the gate it was born under and its alignment (with, mixed or against the market). Signals against the market are hidden on `/signals` by default and never alerted. "Does the market gate help?" compares their results. It only gives a verdict once both sides have 30 closed signals and the gap is beyond noise (|t| ≥ 2).
   - *Why first:* it is the single filter every successful retail method uses, and it is cheap.
2. **Track record by regime and horizon. Built** (`research/tech_signals.py`).
   - Every signal's return 5, 20 and 60 sessions after its close is recorded, independent of its stop and target. So is that return minus the Nifty's over the same sessions (excess). Both are signed for the direction, so a short gains when the stock falls.
   - Split by scan × the market gate it was born under, and by confluence band (0–1, 2–3, 4–6): how often it beat the Nifty, and its median and mean excess. Cells with fewer than 10 signals are marked too few.
   - Each of today's signals shows its scan's record in today's market (20 sessions), or across all markets when today's market has none yet. Alerts quote it once it has 10+ signals.
   - Measured from the signal day's close; a real entry at the next open differs by the opening gap.
3. **Weekly technical rating. Built** (`research/technicals.py`).
   - The same 11-vote rating on weekly bars (weeks ending Friday), using **completed weeks only**: a week counts once its Friday has passed, so the rating does not change mid-week, and a week cut short by a Friday holiday counts from the next Monday.
   - It needs 35+ weeks of history. The 200-week votes stay absent until ~4 years are stored.
   - `mtf_alignment`: BULL when the daily and weekly ratings are both BUY / STRONG_BUY, BEAR when both are SELL / STRONG_SELL, else MIXED. It is a screener field, with presets "Daily and weekly both bullish" and "Breakout with the weekly trend".
   - Each signal records `weekly_agrees` (the weekly rating on its side or not). This is kept out of the confluence count, so "Does the weekly trend add?" can test whether the agreement improves results.
4. **Chart patterns. Built** (`research/patterns.py`).
   - Detected from confirmed swing pivots (3 bars each side) on the bars **before** today; only today's close decides a breakout, so the triggering bar never shapes the pattern.
   - **Darvas box:** a new high near the 52-week high, not exceeded for 3+ sessions, with a bottom that held 3 sessions. The box spans 5+ sessions and is at most 25 % tall. Breakout above the top, or breakdown below the bottom.
   - **VCP:** 2–4 pullbacks, each shallower than the last (last ≤ 12 %), highs within 15 % of the base top, volume drying up (10-day below 85 % of 50-day), in an up-trend. Breakout above the last pullback's high on 1.4x+ volume.
   - **Double bottom:** two lows within 3 %, 10–60 sessions apart (the second within 30), after a 10 % decline. Breakout above a neckline 6 %+ above them.
   - **Ascending triangle:** 2+ flat highs (within 1.5 %) over 10+ sessions with rising lows. Breakout above the resistance.
   - **Head and shoulders:** a head 3 %+ above two shoulders within 8 % of each other, after an advance. Breakdown below the neckline extended to today.
   - **Signals:** each breakout is a scan, so it gets levels, confluence, the market gate, the forward record and a screener field like the rest. Its reason names the pattern's own levels.
   - **Alerts:** a pattern scan only alerts once 30 of its signals have closed with a positive average R. The Track record shows "held · n/30" until then.
   - **Screener:** `chart_patterns` lists patterns in place near their trigger (upper half of the box / base, within 3 % of a triangle's resistance) with their levels. `vcp_setup` flags a VCP below its pivot. Presets: "Chart pattern breakouts" and "VCP setups".
   - **Calibration:** on random-walk prices, each breakout fires on 0.02–1.2 % of stock-days, and setups are listed on 0.2–7 %. Patterns are rare, as intended.
5. **RS-line new high and size-group ranks. Built.**
   - **RS line:** the close divided by the Nifty. The scan "RS line new high" fires on the first day it tops its prior 52-week high. "RS line new high before price" fires when it does so while the price is still below its own 52-week high (IBD's early-leadership tell). `rs_line_at_high` marks stocks currently at an RS high.
   - **Size groups (AMFI's rank rule over ATIP's universe):** large = top 100 by market cap (close × the latest shares outstanding on or before the day), mid = 101–250, small = the rest. `rs_rating_cap` is the RS rating ranked only within the stock's own group, so a mid-cap leader is not hidden behind large-cap moves.
   - **Screener:** `rs_line_at_high`, `cap_bucket`, `rs_rating_cap`. Presets "RS line new high before price" and "RS leaders in their size group".
6. **Delivery-% spike. Built.**
   - ATIP already stores NSE's `DELIV_PER` in `prices_daily.delivery_pct` (`data/bhavcopy.py`); the signal engine now reads it.
   - Scan "Delivery spike, up day": delivery % at 1.5× or more its 20-day average and 30 %+, on an up close.
   - Screener: `delivery_pct`, `delivery_ratio`, preset "Delivery spike on an up day".
   - A popular Indian heuristic (StockEdge, Chartink) with no peer-reviewed return evidence; its track record will show whether it pays.
7. **Explainable fundamental composite. Built** (`research/scorecard.py`).
   - Five axes of six pass / fail checks over the stored fundamentals, each check a sentence with the numbers it used:
     - **Value:** below the research model's fair value, and 20 %+ below; P/E below the market's and its industry's median; PEG below 1; P/B below its industry's median.
     - **Growth** (the latest reported figures; ATIP has no analyst forecasts): EPS growth above a 7 % savings rate, above the market's median, 20 %+; revenue growth above the market's median, 20 %+; growth it can fund itself (ROE × profit kept) of 10 %+.
     - **Past performance:** EPS up over 3 years; growth accelerating; EPS growth above the industry's; net margin up on a year ago; ROE 20 %+; positive free cash flow.
     - **Financial health:** current ratio 1+; net debt under 40 % of equity; debt not rising; interest cover 3x+; free cash flow covering 20 %+ of debt (a company with no debt passes these); promoter pledge under 5 %.
     - **Dividend:** yield in the top 75 % and top 25 % of payers; paid in each of the last two years; growing; covered by earnings (payout under 75 %) and by free cash flow. Dividend history comes from NSE's corporate-action calendar, split-adjusted.
   - A check with no data never passes; the debt checks are not scored for banks and NBFCs; a loss-maker fails the earnings checks and a non-payer the dividend axis.
   - Shown on `/research` under the report header, with a five-axis chart. Screener fields `checks_passed` (0–30) and one 0–6 count per axis, with presets "Scorecard all-rounders", "Healthy and growing", "Undervalued with a clean record" and "Dependable dividends".
   - **Its own record:** the 20:50 job stores each day's scorecards; "Does the scorecard pay?" on `/research` shows return vs the Nifty after 20 / 60 / 120 / 250 sessions by checks passed, one sample per stock per month, 30 samples a band before it counts.
   - **DVM view. Built** (`research/dvm.py`): Trendlyne-style durability, valuation and momentum, 0–100 each, every score returned with its inputs and their numbers.
     - **Durability:** the share of the scorecard's 12 financial-health and past-performance checks that passed, of those that could be made (4 needed; a bank's debt checks drop out rather than fail).
     - **Valuation** (high = cheap): 60 % price vs the research model's fair value (50 + 1.25 × the % discount: 40 % below → 100, at fair value 50, 40 % above → 0) and 40 % the P/E against its industry's median, P/B for banks and NBFCs (100 × (1.5 − multiple ÷ median); a loss-maker scores 0 on it; 3+ peers needed). Either part alone when the other is missing.
     - **Momentum:** half the daily technical rating (−1..+1 → 0..100), half the RS rating (1–99).
     - **Zones** (all three scores needed; high 55+, low below 35; the first rule that holds wins): *Strong performer* (all three high), *Value trap* (valuation high, durability low), *Momentum trap* (momentum high, durability low), *Expensive performer* (durability and momentum high, valuation not), *Value, under the radar* (durability and valuation high, momentum not), *Weak* (durability and momentum low), *Mid-range* (the rest).
     - Stored nightly on the scorecard's own row (`fundamental_scorecard.dvm_*`, the 20:50 job): it is built from the same screener rows, one per stock per day. Screener fields `dvm_d`, `dvm_v`, `dvm_m`, `dvm_zone`; presets "DVM strong performers", "Sound and cheap, not yet in favour" and "DVM value and momentum traps". On `/research` under the scorecard. No track record yet: the stored zones make one possible.
8. **Event calendar. Built** (`research/event_calendar.py`).
   - **Dates:** Fed decisions (2026, and the Fed's tentative 2027 calendar), US CPI and US jobs-report releases for 2026, RBI decisions for 2026-27, seeded from the official calendars (checked against two sources each on 2026-10-07). More come from `config.json` `"event_calendar": {"events": [...]}` or the add form on `/market-pulse`. The seed needs a yearly refresh, like the NSE holiday list.
   - **Which session it hits:** US releases come after India's close (CPI and payrolls at 08:30 New York = 18:00 / 19:00 IST; the Fed at 14:00 New York = 23:30 / 00:30 IST, following US daylight saving), so they hit the next NSE session's open, holidays skipped. The RBI decides at 10:00 IST, during the session.
   - **The gap band:** the morning estimate now carries ± the typical miss of past GIFT estimates. After a US release the band is widened: by the measured ratio of the miss on such mornings to the miss on other mornings once 10 and 30 of them are stored, by an assumed ×1.5 until then (the page says which). An RBI day is marked but not widened, since the decision comes after the open.
   - **Recorded:** each morning's events and band are stored with the estimate (`market_cue.events`, `band_pct`), and the open-gap record is split into mornings after a US release and the rest.
   - **Market pulse:** an event card (today's events, the next two weeks, the record split) and a line in the reasons on event days.

### Phase 3: intraday (needs the Dhan Data API, ₹499/month)
1. **Intraday scans. Built** (`research/intraday_signals.py`).
   - **Bars:** ATIP already stores 15-minute bars for the tracked universe every 30 minutes from 10:00 (`intraday_bars`, `run_intraday_30min`); the scans read them, 15-minute bars only.
   - **Opening-range breakout / breakdown:** the first close beyond the 09:15–09:30 range (or 09:15–09:45), counted only if it comes by 11:30 on 1.5× the slot's usual volume (the same 15 minutes over the last 5 sessions).
   - **Open = low / open = high:** judged once on the first hour: no trade more than 0.1 % beyond the open, and 0.5 %+ away from it by 10:15.
   - **Intraday squeeze:** `research/technicals.py`'s Bollinger / Keltner on 15-minute bars, 6+ bars of squeeze released on 1.2× volume, in the bar's direction.
   - **Calibration:** on random-walk bars without the filters, the breakout fired on ~78 % of stocks a day per side; with them ~8–10 %, the squeeze under 2 %.
   - **Record:** each hit is stored once per stock and day, with the price when ATIP saw it (bars arrive every 30 minutes), and measured from that price to the close, against the Nifty over the same minutes, with its best and worst move. A scan alerts only after 30 closed hits with a positive mean excess. `/signals` → Intraday.
   - Also fixed: the existing intraday scanner (`strategy/intraday_scan.py`) read every interval from `intraday_bars`, so 1-minute bars from tick capture would have been mixed into its 15-minute series.
2. **20-level depth. Built** (`data/depth20.py`).
   - Dhan's 20-level WebSocket (`wss://depth-api-feed.dhan.co/twentydepth`, up to 50 stocks per connection), frames parsed with the layout of dhanhq's `fulldepth.py` (12-byte header, 20 × price / quantity / orders; bids and asks as separate messages).
   - **DWI:** the levels within 50 bp of the mid, level k weighted e^(−0.5(k−1)), stored every 15 seconds per stock beside the best-level and plain 20-level imbalances. Flagged when |DWI| > 0.3 in 3 snapshots running.
   - **Validation:** logistic regression of the direction of the mid's next 1- and 5-minute move on each measure; "predictive" only with 500+ snapshots and |z| ≥ 2. A planted effect is recovered in the tests and noise is not.
   - Off by default (`depth20.enabled`), started with the other feeds by `main.py`, trading hours only, parks on Dhan's refusals (e.g. 806: no Data API subscription) with the reason. `/market-pulse` → "20-level depth".
3. **Synchronised global moves. Built** (`research/global_sync.py`).
   - Global snapshots at 15:31 and 08:42 IST on trading days: S&P 500 and Nasdaq 100 futures, the Nikkei and Hang Seng (trading by 08:45 IST), Brent and gold futures, the dollar index and USD/INR, from Yahoo.
   - Each morning: the log moves from India's close to 08:45 against the Nifty's opening gap (first reading 09:15–09:30). Ridge regression once 40 mornings exist, walk-forward against "no change" and against the GIFT estimate on the same mornings.
   - `capture_gift` stores its estimate next to GIFT's and the daily-close model's, and the open-gap record scores all three. Until 40 mornings exist the page says how many are stored.
4. **75-minute technical rating. Built** (`research/technicals.py` `bars_75`, `rating_75`).
   - The daily rating's votes on 75-minute bars (the session in five: 09:15–10:30, 10:30–11:45, 11:45–13:00, 13:00–14:15, 14:15–15:30), built from the stored 15-minute bars only (interval 15, regular session, each bar counted once done).
   - **Completed bars only:** a 75-minute bar counts once its end has passed and its closing 15-minute bar is stored, or once the day's session is over; so a bar still waiting for its last 15 minutes never counts, and a half day's last bar (or a bar missing a 15-minute bar the feed never delivered) counts after the close with the bars it has.
   - Needs 35 completed 75-minute bars (7 sessions); the SMA 200 votes need 40 sessions (`intraday_bars` keeps 90 days, about 60). Given only when the last completed bar is on the daily bar's own session, so a failed 15-minute fetch never passes off an older chart as today's.
   - **When:** with the 20:30 signal run, not every 15 minutes: after the close all five of the day's bars are complete and the agreement compares two ratings of the same session; a 75-minute bar completes only five times a day and the 15-minute bars arrive every 30 minutes, so an intraday cadence would mostly recompute the same value, and the snapshot row it lives in is a daily one.
   - `mtf_alignment_75`: BULL when the daily and 75-minute ratings are both BUY / STRONG_BUY, BEAR when both SELL / STRONG_SELL, else MIXED. Screener fields `tech_rating_75`, `tech_rating_75_label`, `mtf_alignment_75`, `rsi_14_75`, `supertrend_dir_75`; preset "75-minute and daily both bullish".

### Phase 4: AI as the interface
1. **English → screener query. Built** (`research/screener_nl.py`).
   - "Ask in English" on `/screener` writes a query in ATIP's language into the query box; it never runs it. The safe parser validates it, and the page says what was not understood.
   - **Claude path** (`config.json` `"screener_ai"`, off by default, its own daily cost cap): one structured-output request with the field catalogue, presets and industries as a cached system prompt; a query the parser rejects goes back once with the parser's error. Usage goes to `ai_usage_log`.
   - **Rule path**, always available and used whenever Claude is off, over its cap, refused (the current key returns 401, KD-001) or wrong twice: preset names, "<field> above / below / at least / between … <number>" (1,000 crore, 20 %), phrases such as debt-free, no pledge, near the 52-week high, above the 200-DMA, scan names, industries, size groups, "sorted by …", "cheapest first".
2. **ATIP MCP server. Built** (`tools/atip_mcp.py`), read-only:
   - MCP over stdio (JSON-RPC 2.0, protocol 2025-06-18 / 2025-03-26 / 2024-11-05), written to the specification without a new package.
   - 12 tools: screener fields and runs, signals today and their record, intraday scans, stock technicals, market pulse, market gate, research report, scorecard, event calendar, open orders. All annotated read-only.
   - The SQLite connection runs with `PRAGMA query_only`, so no tool can write even by a bug; stdout carries only protocol messages.
   - Lets you use ATIP from Claude, alongside the Dhan MCP (configuration in the module docstring and the handoff).
3. **LLM explanations** of a signal or report: covered by the W36 assistant (`ml/chat.py`, read-only tools over ATIP's tables) and now by Claude over the MCP server, both answering from the stored numbers. Rule kept for later: no LLM output is backtested without look-ahead-bias controls (only data available on the day, model knowledge cutoff after the test window ruled out).

### Phase 5: execution (only with your explicit go-ahead and a test plan)
- Signals → Dhan super orders or forever orders with the signal's stop and target.
- Needs a static IP, an algo ID, per-second throttling, a kill switch and a paper-first period. This is real-money work: ATIP stays paper-first until you ask for it.

---

## 9. Daily routine with the new pages

| Time (IST) | What happens | Where to look |
|---|---|---|
| 08:45, 09:05 | GIFT Nifty move and expected open captured | `/market-pulse` → Global cues |
| 09:35 | Actual open compared with the forecast | `/market-pulse` → open-gap record |
| 09:15–15:30, every 15 min | Pending-order pressure across the universe | `/market-pulse` → Pending orders; screener `book_*` fields |
| Any time | Your Dhan open and forever orders, ATIP paper orders and rules | `/market-pulse` → Your pending orders |
| 20:15 | NSE participant OI (FII positioning) | `/market-pulse` → Derivatives positioning |
| 20:30 | Technical snapshot and signals; outcomes of open signals | `/signals`; screener technical presets |
| 20:40, 20:50 | Research reports; saved screens with new-match alerts | `/research`, `/screener` |

---

## 10. Owner actions

- **Dhan Data API subscription (₹499/month):** needed for history, quotes, order-book pressure and GIFT capture.
- **Run once:** `python -m research.market_pulse nifty-history` (5 years of the Nifty, for the global model) and `python -c "from data.markets import backfill_global_history as b; print(b())"` (global series).
- **NSE files (participant OI):** NSE serves them to Indian connections. The 20:15 job on your Mac fetches them; run `python -m data.participant_oi --days 60` once to load history.
- **Read the track record before trusting a scan.** Check `/signals` → Track record. A scan with fewer than ~30 closed signals has no record yet.

---

## Sources

**Technical tools:**
- https://in.tradingview.com/support/solutions/43000614331
- https://tradingview.com/support/solutions/43000742436
- https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/stockcharts-technical-rank-sctr
- https://help.stockcharts.com/scanning-and-alerts/advanced-scan-library/predefined-scans
- https://www.barchart.com/trader/help/technical_opinion/opinions.php
- https://www.investing.com/equities/nvidia-corp-technical
- https://www.liberatedstocktrader.com/finviz-pricing-discounts/
- https://www.trade-ideas.com/hollyguide/What_Holly_Does.html
- https://www.liberatedstocktrader.com/tickeron-review/
- https://chartink.com/subscription
- https://chartink.com/articles/page/4
- https://zerodha.com/z-connect/streak/introducing-scanner-by-streak
- https://dhan.co/support/platforms/scanx/what-types-of-candlestick-screeners-are-available-in-scanx/
- https://blog.stockedge.com/ready-combination-scans/
- https://discussion.fool.com/t/ibd-follow-through-day-definition/107779

**Fundamental, combined and AI tools:**
- https://www.screener.in/docs/changelog/Screener-AI/
- https://help.trendlyne.com/support/solutions/articles/84000347982
- https://trendlyne.com/subscription/mcp/plans/
- https://www.tickertape.in/blog/introducing-scorecard-stock-analysis-got-quicker-and-better-with-quantitative-insights/
- https://marketsmithindia.com/mstool/evaluationFAQ.jsp
- https://www.valueresearchonline.com/stock-rating-methodology
- https://support.simplywall.st/hc/en-us/articles/360001740916
- https://help.seekingalpha.com/premium/quant-ratings-and-factor-grades-faq
- https://gurufocus.com/tutorial/article/28/gf-score
- https://www.stockrover.com/metrics/stock-rover-ratings/
- https://tooliverse.ai/tools/danelfin
- https://www.kavout.com/k-score/
- https://www.lseg.com/en/data-analytics/financial-data/analytics/quantitative-analytics/starmine-combined-alpha-model
- https://www.tradingview.com/blog/en/ai-copilot-now-on-tradingview-61231/
- https://www.tradingview.com/support/solutions/43000785770-how-to-use-the-ai-screener/
- https://www.tradingview.com/blog/en/tradingview-mcp-server-public-beta-60864/
- https://docs.dhanhq.co/mcp/
- https://techcrunch.com/2025/08/18/perplexity-now-supports-live-earnings-call-transcripts-for-indian-stocks/
- https://www.matchmybroker.com/tools/fiscal-ai-review
- https://www.marketsmedia.com/bloomberg-introduces-agentic-ai-to-the-terminal/

**Global cues, GIFT Nifty and FII flows:**
- https://nsearchives.nseindia.com/content/research/Paper39.pdf
- https://iupindia.in/1207/IJFE_VolatilitySpillovers7.pdf
- https://mse.ac.in/wp-content/uploads/2026/01/34.pdf
- https://publishingindia.com/archive/jcar/dynamic-effects-of-us-and-asian-markets-on-indian-stock-market
- https://reference-global.com/article/10.2478/eoik-2025-0039
- https://www.valueresearchonline.com/learn/stocks/brent-crude-indian-stocks-portfolio-impact/
- https://www.sciencedirect.com/science/article/pii/S2214845026000918
- https://emerald.com/insight/content/doi/10.1108/IJOEM-07-2022-1097/full/html
- https://openthemagazine.com/business/gift-nifty-trading-hours-impact-and-role-in-nifty-price-discovery-explained
- https://serialsjournals.com/abstract/23573_26.pdf
- https://www.emerald.com/insight/content/doi/10.1108/17554191211274794/full/html
- https://www.freepressjournal.in/business/fiis-pull-40-billion-from-india-in-two-years-know-what-is-keeping-foreign-money-away
- https://algotest.in/blog/participant-wise-open-interest/
- https://www.business-standard.com/amp/markets/news/fiis-hold-10-short-bets-for-every-long-trade-in-index-futures-125080700077_1.html
- https://www.marketcalls.in/futures-and-options/what-do-we-actually-know-about-fii-index-futures-shorts.html
- https://www.businesstoday.in/markets/stocks/story/rs-20000-crore-fpi-outflows-in-2-days-stock-market-headed-for-more-pain-558929-2026-10-01
- https://www.deccanherald.com/amp/story/business/markets/rupee-crashes-to-record-9580-against-usd-settles-near-all-time-low-at-9566-4001257
- https://www.cnbc.com/2026/10/06/treasury-yields-fed-fomc-minutes.html
- https://equalsmoney.com/economic-calendar/events/fomc-meeting, https://fedratecalc.com/fomc-meeting-schedule/, https://www.federalreserve.gov/newsevents/2026-october.htm (FOMC 2026); https://www.mnimarkets.com/articles/mni-federal-reserve-sets-2027-fomc-meeting-schedule-1757093400287 (FOMC 2027, tentative)
- https://fedratecalc.com/cpi-release-date/, https://cpichart.com/cpi-release-calendar/, https://cpiinflationcalculator.com/cpi-release-schedule/ (US CPI 2026)
- https://www.bls.gov/schedule/news_release/empSit.htm, https://www.bls.gov/bls/2025-lapse-revised-release-dates.htm (US payrolls 2026)
- https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx?prid=62422 (RBI MPC 2026-27)
- https://repository.iimb.ac.in/handle/2074/21032 (no max-pain effect in India)

**Order book:**
- https://arxiv.org/abs/1011.6402 (order-flow imbalance, Cont-Kukanov-Stoikov)
- https://arxiv.org/abs/1907.06230 (multi-level OFI)
- https://arxiv.org/abs/2112.13213 (integrated OFI)
- https://arxiv.org/abs/1512.03492 (queue imbalance)
- https://ideas.repec.org/a/eee/finlet/v41y2021ics1544612320316779.html (NSE: predictability gone within 30 minutes)
- https://raw.githubusercontent.com/dhan-oss/DhanHQ-py/main/src/dhanhq/marketfeed.py
- https://raw.githubusercontent.com/dhan-oss/DhanHQ-py/main/src/dhanhq/fulldepth.py
- https://www.business-standard.com/markets/news/sebi-bars-patel-wealth-advisors-4-directors-over-order-spoofing-charges-125042800957_1.html

**Not verified in this research:**
- Vendor GIFT accuracy claims (80–90 %).
- The exact Investing.com and Finviz thresholds.
- Seeking Alpha and Zacks bucket cut-offs.
- Danelfin's self-reported backtest.
- The early-October 2026 market figures quoted in 4.1.
- Whether Dhan's REST order list reports PART_TRADED (it is confirmed only in third-party wrappers; ATIP treats it as open).
