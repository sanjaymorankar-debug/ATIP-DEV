---
name: share-market
description: >
  Comprehensive Indian share market skill covering ATIP pre-market intelligence,
  portfolio analysis, stock research, technical analysis, and intraday monitoring.
  Triggers on: stocks, shares, NSE, BSE, Nifty, Sensex, Zerodha, Groww, holdings,
  mutual funds, SIP, GIFT Nifty, market regime, VIX, FII/DII, sector rotation,
  ATIP, pre-market report, trade-of-the-day, intraday, buy zone, momentum, reversal,
  crash risk, or any Indian stock ticker. Use for: 8:30 AM ATIP daily report,
  intraday refresh, end-of-day learning loop, portfolio CSV analysis, fundamentals,
  technical analysis, P&L review, and market sentiment. Trigger if user pastes
  stocks, asks "how is my portfolio doing", "what does the market look like today",
  or "run the pre-market report". Always use for anything Indian or global share market.
---

# Share Market Skill (ATIP-Enhanced)

A full-stack Indian share market skill covering the complete ATIP daily intelligence
cycle: pre-market report, intraday monitoring, end-of-day learning loop, portfolio
review, stock fundamentals, and technical analysis — delivered as rich visual reports.

---

## Workflow Decision Tree

```
User Input
    │
    ├── Pre-market / ATIP report request?
    │       └── Yes → Module A: Pre-Market Intelligence (8:30 AM Report)
    │
    ├── Intraday monitoring / refresh?
    │       └── Yes → Module B: Intraday Monitoring (09:15–15:30)
    │
    ├── End-of-day / accuracy / learning?
    │       └── Yes → Module C: End-of-Day Learning Loop
    │
    ├── CSV/file uploaded?
    │       └── Yes → Module D: Portfolio Analysis → offer Stock Research
    │
    ├── Single stock mentioned?
    │       └── Yes → Module E: Stock Research + Technical Analysis
    │
    ├── Market / news query?
    │       └── Yes → Module F: Market News & Sentiment
    │
    └── Portfolio pasted as text?
            └── Yes → Parse → Module D: Portfolio Analysis
```

---

## Module A: Pre-Market Intelligence — 8:30 AM ATIP Report

Run this when the user asks for the pre-market report, ATIP daily report,
"what does the market look like today", or anything about pre-market conditions.

### A0. ATIP 9:00 AM Pre-Open Checklist (read this first)

"Run pre-market report" always includes ATIP's Pre-Open Checklist. Read it from ATIP; do not recompute it:

- `GET <ATIP URL>/api/preopen` (JSON) or the dashboard page `/preopen` (Reports → Pre-open checklist; also the first
  card on Home). `today` is today's latest run: `preliminary` (08:15 IST, inside ATIP's pre-market report), `0900`
  (09:00 IST) or `0909` (09:09 IST, after NSE's pre-open auction has matched). `record` holds each check's hit rate
  over the last 20 / 60 scored days. `GET /api/preopen?compact=1` is the one-line version.
- The four checks and how ATIP scores them (thresholds are in `config`, never assume them):
  1. **US cues** — Nasdaq Composite + Dow previous close, NQ + YM futures at run time: green +1 / red −1 / mixed 0.
  2. **India VIX** — above 15 "Volatile", else "Stable". A volatility modifier only: it does NOT vote. On Volatile
     days ATIP's advice is size ×0.5, stops ×1.5, avoid buying options outright, prefer defined-risk spreads.
  3. **NSE Advances vs Declines** — NSE's pre-open book (all securities); before 09:00 it is the PRIOR DAY's A/D and
     ATIP labels it "prior-day" — say so in the report.
  4. **GIFT Nifty** (NSE IX; formerly SGX Nifty) — the basis-adjusted move (GIFT now vs GIFT at 15:30 IST yesterday)
     within ±0.15 % is a "Flat" open, above Gap-Up, below Gap-Down. The raw "GIFT vs previous Nifty close" is shown too.
- Report: **Market Bias** (Bullish / Bearish / Neutral from the vote sum), **Volatility Regime**, **Expected Open**,
  each check's raw value, source and fetch time in IST, and every caveat (stale / unavailable / prior-day). A check
  with status STALE or UNAVAILABLE did not vote — never fill it in from a web search and present it as ATIP's.
- What the backtest (611 sessions, Jun 2024 – Nov 2026) says, to state honestly: US cues call the opening gap about
  72 % of the time (base rate 57 %) and the day's close vs the previous close about 64 % (base 50 %), but they say
  nothing about the move AFTER 09:15 (open → close ≈ 49 %). Prior-day A/D adds nothing. VIX above 15 roughly doubles
  the typical gap and day range. GIFT and the live pre-open A/D have too little clean history yet — use `record`.
- Paper only, informational. ATIP's checklist never places or sizes an order.

If ATIP is unreachable, say so and fall back to A1 with web search, labelled "skill estimate, not ATIP".

### A1. Global Market Check

Search and display the following (use web search for live data):

| Index / Asset     | Value | Change | Direction |
|-------------------|-------|--------|-----------|
| GIFT Nifty        |       |        |           |
| Previous Nifty 50 |       |        |           |
| Bank Nifty        |       |        |           |
| India VIX         |       |        |           |
| Dow Jones         |       |        |           |
| S&P 500           |       |        |           |
| Nasdaq            |       |        |           |
| Nikkei            |       |        |           |
| Hang Seng         |       |        |           |
| DAX / FTSE        |       |        |           |
| Crude Oil (WTI)   |       |        |           |
| Gold              |       |        |           |
| USD/INR           |       |        |           |

Also summarise: overnight direction, key overnight events, US Fed / macro news.

### A2. Market Regime Classification

Before stock-level analysis, determine today's regime:

| Regime             | Indicators                                              |
|--------------------|---------------------------------------------------------|
| 🟢 Bull            | Nifty above 200 SMA, VIX < 15, breadth positive        |
| 🔴 Bear            | Nifty below 200 SMA, VIX > 20, FII selling             |
| 🟡 Sideways        | Nifty range-bound, mixed breadth                        |
| 🟠 High Volatility | VIX > 20, wide swings, conflicting signals              |
| 🔵 Recovery        | Nifty recovering from recent lows, breadth improving   |

State the regime clearly. This gates the Decision Engine output below.

### A3. Market Health Score

Use ATIP's own Market Health score (`GET <ATIP URL>/api/mh`, also in `/api/preopen` → the pre-market report) and its
regime. ATIP weights: Nifty trend 0.20, Bank Nifty 0.15, breadth (% above 200-DMA) 0.10, India VIX (inverted) 0.10,
FII 0.10, DII 0.10, global 0.10, sector 0.10, A/D 0.05, Asia 0.05 — GIFT Nifty is NOT an input (it is the checklist's
expected-open check). Only when ATIP is unreachable compute the fallback below, labelled "skill estimate, not ATIP's
Market Health" — its weights differ from ATIP's and the two must not be mixed in one report.

Fallback only — compute a 0–100 Market Health Score from:
- Nifty trend vs 50/200 SMA (20 pts)
- VIX level and trend (15 pts)
- FII/DII net activity (15 pts)
- Advance/Decline ratio (15 pts)
- Sector breadth (15 pts)
- Global cues alignment (10 pts)
- GIFT Nifty premium/discount (10 pts)

Score → label: 0–30 Weak | 31–50 Cautious | 51–70 Moderate | 71–85 Healthy | 86–100 Strong

### A4. Stock-Level Pre-Market Analysis

For each ATIP-covered stock, evaluate and report:

**Price & Trend Context**
- Previous close, expected gap (up/down/flat)
- 1D / 5D / 20D / 50D trend direction
- 52-week position (near high / mid-range / near low)
- SMA 200 — above / below / at
- EMA 9 / 21 / 50 alignment

**Momentum & Volatility**
- RSI (14) — value and zone
- MACD — signal (bullish/bearish cross, histogram)
- ADX — trend strength (>25 = trending)
- ATR and ATR% — volatility context

**Volume & Institutional**
- Volume trend vs 20D average
- Relative strength vs Nifty
- Institutional activity (FII/DII/bulk deals if available)

**Sector & Macro**
- Sector trend today
- Support and resistance levels
- News sentiment (positive / neutral / negative)

**ATIP Proprietary Signals** (use if available in ATIP data pipeline):
- VPI — Volatility Pressure Index
- MRI — Market Risk Index
- RRI — Risk-Reward Index
- ZPI — Zone Pressure Index
- CRI — Crash Risk Index
- ACS — ATIP Composite Score

### A5. Pre-Market Trend Classification

Assign each stock one classification:

| Label             | Meaning                                      |
|-------------------|----------------------------------------------|
| 🟢 Strong Bullish  | High probability of upward continuation     |
| 🟢 Bullish         | Positive setup, favourable risk/reward      |
| 🟡 Neutral         | No clear directional edge                   |
| 🟠 Weak / Bearish  | Negative setup, caution warranted           |
| 🔴 Strong Bearish  | Significant downside risk                   |
| ⚪ Wait            | Conflicting or insufficient signals         |

**Important:** Do not label a stock Bullish purely on technical trend.
Cross-check against CRI, ACS, Market Health Score, and the regime
before assigning a positive classification.

### A6. Pre-Market Opportunity Ranking

Generate the following ranked lists:

| List                        | Count |
|-----------------------------|-------|
| 🟢 Top Bullish Stocks        | 25    |
| 🔴 Top Bearish Stocks        | 25    |
| ⚡ Top Momentum Stocks        | 10    |
| 🔄 Top Recovery Stocks       | 10    |
| 🎯 Buy-Zone Candidates       | 10    |
| 🔁 Reversal Candidates       | 10    |
| ⚠️ High-Risk Stocks          | 10    |
| 🚫 Stocks to Avoid Today     | 10    |

### A7. 8:30 AM ATIP Daily Report Output

Structured report answering: *"What is the market likely to look like today,
and where are the highest-probability opportunities and risks?"*

```
═══════════════════════════════════════════════
  ATIP DAILY PRE-MARKET REPORT — [DATE]  08:30 IST
═══════════════════════════════════════════════

🌏 GLOBAL OVERNIGHT SUMMARY
[3–4 lines: US close, Asian open, key events]

🧭 PRE-OPEN CHECKLIST (ATIP, [08:15 preliminary | 09:00 | 09:09] IST)
   Bias: [Bullish/Bearish/Neutral] (votes [±n] of [k])  ·  Volatility: [Volatile/Stable] (VIX [x] vs 15)
   Expected open: [Gap-Up/Flat/Gap-Down] (GIFT [±x %] basis-adjusted; raw vs Nifty close [±y %])
   US: [green/red/mixed] (Nasdaq ±, Dow ±, NQ ±, YM ±)  ·  A/D: [adv]/[dec] ([pre-open | PRIOR-DAY])
   Track record (last 20 scored days): bias [h/n], open call [h/n] — [caveats]

📍 GIFT NIFTY DIRECTION:  [↑↓→] [value] [premium/discount] (from the checklist's GIFT source)
📍 EXPECTED NIFTY OPEN:   [level + gap estimate] (the checklist's expected open)

🏛 MARKET REGIME:         [Regime label]
💊 MARKET HEALTH SCORE:   [X/100] — [Label]

🔄 SECTOR ROTATION:       [Leading / lagging sectors today]
🏦 FII/DII CONTEXT:       [Net buy/sell trend]

───────────────────────────────────────────────
🟢 TOP BULLISH STOCKS     (up to 10 key names)
🔴 TOP BEARISH STOCKS     (up to 10 key names)
🎯 BUY-ZONE STOCKS        (up to 5)
⚡ MOMENTUM / REVERSAL    (up to 5)
💥 CRASH-RISK STOCKS      (up to 5)
───────────────────────────────────────────────

🏆 TRADE-OF-THE-DAY CANDIDATES
[2–3 high-conviction setups with brief rationale]

🔔 PORTFOLIO WARNINGS
[Any holdings with elevated CRI or adverse setups]

📋 FINAL DAILY LIST
BUY:    [symbols]
WATCH:  [symbols]
AVOID:  [symbols]
WAIT:   [symbols]
═══════════════════════════════════════════════
⚠️ For informational use only. Not financial advice.
```

Render as a styled React artifact with collapsible sections and color coding.

---

## Module B: Intraday Monitoring (09:15–15:30)

Run when user asks for an intraday refresh, live update, or monitoring check.
Refresh cadence: every 15 minutes (or on user request).

Each refresh covers:
- Nifty / Bank Nifty level vs morning expectation
- VIX current vs pre-market
- Sector movement (leaders and laggards)
- Volume — above/below average
- Breakouts / breakdowns triggered since open
- Reversals vs pre-market classification
- ZPI / MRI / CRI alert status
- Regime change flag (did regime shift intraday?)

Output: a compact status card, not a full report.

```
⏱ ATIP INTRADAY UPDATE — [TIME]
Nifty: [value] [+/-] | Bank Nifty: [value] [+/-]
VIX: [value] | Regime: [label]
Volume: [above/below avg]

🚨 Alerts: [breakout / reversal / CRI spike if any]
📊 Sector Leaders: [X, Y] | Laggards: [A, B]
```

---

## Module C: End-of-Day Learning Loop (16:00–18:00)

Run after market close. Compares the morning ATIP prediction to actual outcomes.

For each stock in the morning report, record:

| Field               | Morning | Actual |
|---------------------|---------|--------|
| Predicted direction |         |        |
| Actual direction    |         |        |
| Predicted score     |         |        |
| Actual return (1D)  |         |        |
| 5D outcome          | TBD     |        |
| 10D outcome         | TBD     |        |
| 20D outcome         | TBD     |        |

Store this in the ATIP Accuracy Tracker. Summarise:
- Bullish calls accuracy %
- Bearish calls accuracy %
- Regime call accuracy
- Trade-of-the-Day outcome
- Learning notes (what signals worked / failed today)

---

## Module D: Portfolio Analysis

### D1. Data Ingestion

**CSV Upload (Zerodha / Groww / Angel / Upstox)**
1. Read: `head -5 /mnt/user-data/uploads/<filename>`
2. Detect broker format (see `references/broker-formats.md`)
3. Parse: symbol, quantity, avg cost, LTP, current value, P&L, P&L%

**Pasted / Manual Data**
```
RELIANCE 10 shares @ 2400, TCS 5 @ 3800, INFY 20 @ 1500
```
Parse symbol, quantity, buy price. Fetch current prices via web search.

### D2. Portfolio Output

- Total invested, current value, overall P&L (₹ and %)
- Top gainers and losers (top 3 each)
- Sector allocation breakdown
- Concentration risk flags (any stock > 20%)
- Diversification score (1–10)

**React artifact** with tabs: Overview | Holdings | Analysis | Technical | News

Use Recharts for charts, Tailwind for styling.

```
📊 Portfolio Summary
Total Invested:   ₹X,XX,XXX
Current Value:    ₹X,XX,XXX
Overall P&L:      ₹XX,XXX (X.X%) 🟢/🔴
Day's Change:     ₹X,XXX (X.X%)

🏆 Top Gainers     📉 Top Losers
1. STOCK +XX%     1. STOCK -XX%
2. STOCK +XX%     2. STOCK -XX%
```

---

## Module E: Stock Research + Technical Analysis

### E1. Fundamentals

| Metric           | Value | Industry Avg |
|------------------|-------|-------------|
| P/E Ratio        |       |             |
| P/B Ratio        |       |             |
| ROE (%)          |       |             |
| Debt/Equity      |       |             |
| Dividend Yield   |       |             |
| EPS (TTM)        |       |             |
| Revenue Growth   |       |             |
| Profit Growth    |       |             |

Include: sector/industry, market cap category, 3 strengths, 3 risks, analyst consensus.

### E2. Technical Analysis

```
📈 Technical Analysis — STOCKNAME
Current Price: ₹XXX

Trend:      [Bullish 🟢 / Bearish 🔴 / Sideways 🟡]
RSI (14):   XX — [Overbought/Neutral/Oversold]
MACD:       [Signal]
ADX:        XX — [Trending/Ranging]
50-DMA:     ₹XXX  |  200-DMA: ₹XXX
EMA 9/21:   [Alignment]

ATR:        ₹XX (X.X%)
Support:    ₹XXX  |  Resistance: ₹XXX

🎯 Short-term Outlook: [1-2 lines]
⚠️ Risk Level: [Low / Medium / High]
```

Always caveat: technical analysis is indicative, not a buy/sell recommendation.

---

## Module F: Market News & Sentiment

Web search for:
- "NSE Nifty today" / "BSE Sensex today"
- "Indian stock market news [today's date]"
- FII/DII activity, specific stock news

Produce:
- Market summary (Nifty/Sensex level, change)
- Top 3 market-moving news items
- FII/DII net activity
- Sector in focus
- Overall sentiment: Bullish / Bearish / Neutral with reasoning

---

## Output Guidelines

- Use ₹ symbol for Indian prices
- Show both ₹ and % for all P&L figures
- Color-code: green = gains / positive, red = losses / risk
- Render rich reports as React artifacts (Recharts + Tailwind)
- Compact intraday updates stay in-chat (no artifact needed)

### Disclaimer (always append to reports):
> ⚠️ *This analysis is for informational purposes only and does not constitute
> financial advice. Past performance is not indicative of future results.
> Please consult a SEBI-registered investment advisor before making investment decisions.*

---

## References

- `references/broker-formats.md` — Column mappings for Zerodha, Groww, Angel, Upstox CSVs
- `references/sectors.md` — NSE sector classification for 200+ stocks

Read these during data ingestion or sector tagging.
