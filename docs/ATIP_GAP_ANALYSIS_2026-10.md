# ATIP gap analysis: Dhan, Zerodha and institutional research (7 October 2026)

**What this compares:** ATIP against the AI and automated-trading features Dhan and Zerodha offer retail traders, and against how institutions research stocks and issue recommendations.

**How it was compiled:**
- **Broker and institutional features:** web research done on 2026-10-07 (sources at the end).
- **ATIP side:** a read-only check of the code on this branch.

"Present" means the code exists. As `docs/WAVE_STATUS.md` says, most waves from W3 on are **implemented but not independently verified**.

**Legend:**
- ✅ present
- 🟡 partial
- ❌ missing
- 🆕 built in W39 (this change; see `docs/W39_RESEARCH_HISTORY_OPTIONS_HANDOFF.md`; the tools-and-signals research is in `docs/ANALYSIS_TOOLS_AND_SIGNALS_PLAN_2026-10.md`)

## 1. Where ATIP stands

**Ahead of both brokers:**
- Research, scoring and backtesting depth: composite indices, factor library with IC research, walk-forward with purge/embargo, deflated Sharpe, Monte Carlo, event studies.
- Paper execution with real risk controls.
- ML lifecycle tooling.
- An audit trail.

Neither broker ships a factor research platform or model-governance tooling to retail users.

**Behind both brokers:**
- **Live execution:** ATIP can only place plain market/limit orders through the old W1 path. GTT, super, bracket, basket and AMO orders don't exist at the broker.
- **Charts and screeners.**
- **Everyday investing tools:** mutual fund analytics, SIPs, tax P&L, margin.

**Behind institutions:** before W39 there was no valuation, no target price, no research report, and nothing tracked analyst-style calls. W39 adds these. Estimates, consensus and a factor risk model are still missing.

## 2. Against Dhan and Zerodha

### 2.1 Orders and risk

| Feature | Dhan | Zerodha | ATIP | Gap |
|---|---|---|---|---|
| Market / limit / SL / SL-M | ✅ | ✅ | 🟡 | All four in paper; live is MARKET/LIMIT only (W1 `orders/broker.py`) |
| Super order: entry + target + SL + trailing, multiple targets | ✅ (`/v2/super-order`, up to 4 targets) | — (GTT OCO instead) | 🟡 | ATIP keeps bracket and trailing rules itself (`orders/rules.py`), so they stop when the dashboard process stops. Nothing is held at the broker. |
| GTT / Forever / OCO, trailing GTT | ✅ | ✅ (trailing GTT on web since June 2026) | 🟡 | Same: ATIP-side triggers only |
| Basket orders, basket auto-trigger | ✅ | ✅ (Alert Triggers Order, 20 legs) | ❌ | — |
| Iceberg / auto-slicing above freeze limit | ✅ | ✅ | 🟡 | Paper ICEBERG algo (W34) |
| AMO | ✅ | ✅ | ❌ | — |
| Technical-indicator conditional orders | ✅ (Conditional Trigger API, May 2026) | ✅ (ATO on alerts) | 🟡 | Strategy rules exist, but there is no broker-side trigger |
| Daily P&L exit / kill switch | ✅ (Trader's Control API) | ✅ (manual, per segment) | ✅ | `orders/risk.py` halts, loss and drawdown limits (KD-005: loss limits block BUYs when there is no P&L history) |
| Margin calculator, max-quantity sizing | ✅ | ✅ | ❌ | Funds check only |
| See all your pending orders (order book, forever / GTT) | ✅ | ✅ | 🆕 | W39 `GET /api/brokers/open-orders` and `/market-pulse`: Dhan open and forever orders (read-only) next to ATIP's resting paper orders and target / stop rules |

### 2.2 API, algo and SEBI's retail-algo rules

| Feature | Brokers | ATIP | Gap |
|---|---|---|---|
| Order-update WebSocket / postbacks | Both | ❌ | `ops/webhooks.py`: "broker postbacks are future work" |
| Static IP for order APIs | **Mandatory for both since 1 April 2026** | ❌ | **Owner action:** register the Mac's static IP with Dhan before any live API order. Without it Dhan rejects orders. |
| Algo-ID tagging; at most 10 orders/second without registration | Both (Kite has an `algo_id` parameter) | 🟡 | `max_orders_per_second` is checked for being configured but not enforced. No algo ID is sent. |
| Historical depth | Dhan: daily since listing, 5 years intraday. Kite: daily since the 1990s. | 🆕 | Was ~20 months, and the weekly purge **deleted** prices older than 600 days. W39 keeps 7 years and backfills them. |
| TradingView webhook → order | Dhan native; Kite no | ❌ | Webhook intake exists, but nothing maps a webhook to an order |
| Sandbox | Dhan (fills at ₹100); Kite none | ✅ | ATIP's paper broker is the better simulator |

### 2.3 Strategy, scanning and options

| Feature | Brokers | ATIP | Gap |
|---|---|---|---|
| No-code strategy builder, backtest, paper deploy | Streak; Dhan via partners | ✅ | Live deploy missing (refused at the Dhan adapter) |
| Options strategy builder: payoff, breakevens, POP, Greeks, templates | Sensibull (30+ templates), Dhan Options Trader | 🆕 | W39 `/options-builder`: 16 templates, multi-leg, payoff at expiry and at a chosen date, POP, net Greeks, premiums from stored chains |
| Rule-based strike selection (Dhan "Quant Mode") | Dhan | ❌ | — |
| Option chain with PCR, max pain, IV, IV percentile | Both | ✅ | Chain snapshot off by default; Greeks not computed per chain row |
| Technical screener, scans, candlestick screens, saved screens | ScanX, Streak, Chartink, Kite Screener (July 2026) | 🆕 (EOD) | W39: 36 end-of-day scans (crosses, breakouts on volume, oscillator turns, Supertrend, squeezes, Minervini template, pocket pivots, and Darvas box / VCP / double bottom / ascending triangle / head-and-shoulders breakouts), 19 candle patterns, technical rating (daily and weekly, with their agreement), IBD-style RS rating, all as screener fields with 20 technical and 3 combined presets, plus RS-line new highs, RS ranks within large / mid / small caps and NSE delivery-% spikes. Intraday: the 5 fixed scans plus (W39 Phase 3) opening-range breakout on volume, open = low / high and an intraday squeeze on 15-minute bars, each with its own record. |
| Fundamental multi-filter screener, saved screens | ScanX, Kite Screener, Screener.in | 🆕 | W39 `/screener`: 139 fields (fundamental, earnings surprise (W39b), the fundamental scorecard and the DVM view (W39b), technical incl. the 75-minute rating (W39b), order book, ATIP score, research rating), a safe query language with AND / OR / NOT / IN / CONTAINS, 44 presets including a magic-formula rank and scorecard screens, sortable results, CSV export, saved screens with daily new-match alerts |
| Signals with entry, stop, target and a track record | Trade Ideas Holly, Tickeron, Streak | 🆕 | W39 `/signals`: each scan hit gets 2 × ATR stop, 4 × ATR target, a confluence count of 6, and is followed to TARGET / STOPPED / EXPIRED; win rate and average R per scan. Phase 2: each also carries the market gate (distribution days, follow-through days, 200-DMA); signals against it are hidden by default and never alerted |
| Market depth / pending buy-sell quantity across stocks | Both (per stock) | 🆕 | W39 order-book pressure: total pending buy vs sell for every tracked stock every 15 minutes, persistent buyers / sellers, screener fields; and (Phase 3) Dhan's 20-level depth for up to 50 watchlist stocks with a depth-weighted imbalance and the order-flow imbalance (OFI, best level and multi-level) tested against the next 1 / 5 minutes, OFI also against its own interval. Needs the Dhan Data API. |
| Price and indicator alerts | Both | 🟡 | Needs `enterprise.enabled`; Telegram not configured |
| FII/DII, pre-market dashboard, heatmaps | Both | ✅ | `/market` |
| Global cues → expected Nifty open; FII flow pressure; FII derivatives positioning | Sensibull FII page, Moneycontrol / ET pre-market | 🆕 | W39 `/market-pulse`: global-cue model fitted on ATIP's data with its walk-forward record, basis-free GIFT gap checked against the actual open, with a band widened after FOMC / US CPI / payroll releases (event calendar with RBI dates too), FII flow surprise and pressure, NSE participant OI with a covering-aware crowded-short rule, OI walls |

### 2.4 AI

| Feature | Brokers | ATIP | Gap |
|---|---|---|---|
| Official MCP server (Claude, Cursor) | Kite MCP (May 2025); Dhan MCP (May 2026, places orders) | 🆕 | W39 `tools/atip_mcp.py`: a **read-only ATIP MCP server** (stdio) with 12 tools: screener fields and runs, signals and their records, intraday scans, stock technicals, market pulse and gate, research reports, the scorecard, the event calendar, open orders. Its database connection cannot write. The Dhan MCP covers orders. |
| AI concall and earnings summaries | Zerodha via Tijori | 🟡 | `data/announcements.py` PDF → Claude is built, but the Anthropic key returns 401 (KD-001) |
| AI assistant | Dhan support bot only | 🟡 | `ml/chat.py` with 7 read-only tools; same key problem |
| Natural language → screen or strategy | Neither broker (verified); TradingView, Trendlyne and Screener.in ship it | ❌ | Plan Phase 4 (`docs/ANALYSIS_TOOLS_AND_SIGNALS_PLAN_2026-10.md`) |

### 2.5 Investing, portfolio and charting

| Feature | Brokers | ATIP | Gap |
|---|---|---|---|
| Stock page: fundamentals, peers, shareholding, events | Both | 🆕 | W39 research report adds peer table, ownership trend, events and 7-year statistics |
| Direct mutual funds, SIP / step-up SIP, XIRR | Coin, Dhan | 🟡 | AMFI NAVs ingested daily. W39b adds read-only analytics on them (`data/mf_analytics.py`): returns, rolling returns, risk, category rank and a SIP / lump-sum XIRR calculator. No step-up SIP; MF holdings are still not tracked (`wealth/assets.py` refuses MUTUAL_FUND) |
| Tax P&L (STCG/LTCG), verified P&L | Console, Dhan Journal | ❌ | — |
| Portfolio performance vs NIFTY | Console | ✅ | `wealth/perf/` |
| MTF, pledge, stock lending | Both | ❌ | Broker products; low priority for ATIP |
| TradingView charts, 100+ indicators, trade from chart | Both | 🟡 | Hand-drawn SVG chart: no indicator overlays, no intraday chart. Candlestick patterns are now detected (W39 technicals) but not drawn. |
| Terminal / workspaces, order flow | Dhan DEXT T3, Kite Terminal Mode | ❌ | Not a goal |

## 3. Against institutional research

| Institutional capability | Who does it | ATIP | Gap / what W39 did |
|---|---|---|---|
| Point-in-time fundamentals, 10+ years | Screener.in, Capitaline, FactSet | 🟡 | NSE XBRL from ~2025; first history backfill not run (W27-1) |
| 3-statement model with KPI drivers | Every sell-side analyst | ❌ | Needs full statements (only key lines are parsed today) |
| DCF with WACC, terminal value, bull/base/bear, sensitivity | Sell-side; Morningstar fair value | 🆕 | `research/valuation.py`: two-stage FCFE (E × (1 − g/ROE)), CAPM cost of equity, scenarios weighted 25/50/25, cost of equity × terminal-growth grid |
| Relative valuation against peers and own history | All | 🆕 | Industry-median P/E (P/B for financials); own P/E and P/B at each quarter end from stored prices |
| Bank / NBFC valuation (justified P/B from ROE) | All India banks coverage | 🆕 | `justified_pb` |
| SOTP, insurers (P/EV) | Conglomerate and insurance coverage | ❌ | — |
| Target price and rating bands (Kotak: BUY > 15 %, ADD 5–15, REDUCE −5–5, SELL < −5) | Sell-side | 🆕 | Same bands; the BUY hurdle rises with uncertainty (Morningstar margin-of-safety idea) |
| Uncertainty / moat rating | Morningstar | 🆕 | Method and scenario dispersion → LOW … VERY HIGH. ROCE level and stability give a *moat proxy*, labelled as a proxy. |
| Thesis, catalysts, risks | Every report | 🆕 | Rule-based; every bullet names its number. No generative text. |
| Research report with SEBI disclosures | SEBI RA regulations | 🆕 | Rating definitions, horizon, method, holdings conflict, AI-use statement, "not a registered RA" |
| Recommendation hit rate (TipRanks / Trendlyne target-met) | TipRanks, Trendlyne | 🆕 | Calls (new rating, or target moves > 5 %) marked HIT/MISSED over 12 months, split-adjusted; success rate by rating. ATIP's signal hit rate already existed (`scores/signal_log.py`). |
| Consensus EPS, estimate revisions (Zacks Rank) | Zacks, Visible Alpha, Trendlyne | ❌ | No licensed estimates feed. Biggest remaining data gap. W39b adds only a *proxy*: `eps_trend`, whether trailing-4-quarter EPS growth sped up or slowed on the latest filing. It is not analyst revisions. |
| Earnings surprise (SUE) / post-earnings drift | Quant funds | 🆕 | W39b `research/earnings_surprise.py`: SUE and revenue SUE against the same quarter a year earlier (ATIP's own filings, not consensus), counted from each filing's broadcast. A post-earnings-drift scan (SUE ≥ +2 / ≤ −2, first session after the filing) runs in the signal engine with its own record; its alerts are held until it proves itself. Screener fields and a "Positive earnings surprise" preset. |
| Composite quant grades (value, growth, profitability, momentum, revisions) | Seeking Alpha, CFRA, Value Research, Trendlyne DVM | ✅ | ATIP indices and SPI; W39 adds an explainable scorecard (Simply Wall St style: 5 axes × 6 pass / fail checks, each with its numbers, with its own record vs the Nifty); revisions grade missing |
| Alt data: GST e-way bills, VAHAN, UPI | Indian buy-side | 🟡 | Framework plus 3 sources; VAHAN would be a good next source for autos |
| Factor library, IC, approval gate | Quant funds | ✅ | Factor backfill not run |
| Factor risk model (Barra-style) | MSCI Barra, Axioma | ❌ | `portfolio/risk.py` uses shrunk covariance and beta |
| CPCV, probability of backtest overfitting | López de Prado | 🟡 | Purged walk-forward and deflated Sharpe present; no CPCV or PBO |
| Triple-barrier labels, meta-labelling | Quant funds | 🟡 | Direction / forward-return labels; no meta-labelling |
| Time-series foundation models (TimesFM, Chronos, Kronos) | 2025–26 research | ❌ | 2026 evaluations: useful low-data priors, not reliable alpha. Low priority. |
| LLM research assistant / RAG over filings, agentic research | JPMorgan LLM Suite, Morgan Stanley AskResearchGPT, Man Group AlphaGPT | 🟡 | `ml/chat.py` exists; key 401; no retrieval over filings |
| LLM look-ahead-bias controls | 2025–26 research | ❌ | Needed before any LLM signal is backtested |
| Retail algo compliance (algo ID, 10 orders/second, black-box algos need an RA licence) | SEBI, fully in force 1 April 2026 | 🟡 | See 2.2 |

## 4. Priorities

Ranked by value to this owner (single user, Dhan account, research-led) against risk:

1. **Done in W39:** 7-year history (purge fix + backfill), valuation and research reports with tracked calls, and the options strategy builder.
2. **Done in W39 (follow-up):** fundamental screener (Screener.in / ScanX style) with presets, saved screens and new-match alerts.
   - **Second follow-up:** technical screener and signal engine with track record, market pulse (global cues, GIFT gap, FII pressure and positioning, OI walls), market-wide order-book pressure and the open-orders view. The next technical and AI steps are ordered in `docs/ANALYSIS_TOOLS_AND_SIGNALS_PLAN_2026-10.md` §8.
3. **Mutual fund analytics** (your note in `ATIP-.txt`): returns, rolling returns, XIRR and SIP tracking on the AMFI NAVs already stored; let the wealth ledger hold MFs.
   - **Analytics done in W39b** (`data/mf_analytics.py`, `/api/data/mf/{scheme}/analytics`, `/sip`, `/compare`; scheme detail on the Data platform page, Multi-asset tab): point-to-point and rolling returns, volatility, drawdown with dates, Sharpe / Sortino, beta / alpha / tracking error against a stored index, AMFI-category rank with stated coverage, and a SIP / lump-sum calculator with XIRR on the real NAV history. Read-only. Letting the wealth ledger hold MFs is still open: it is in your Scope Exclusions sheet and needs your decision.
4. **Tax P&L** (STCG/LTCG with grandfathering, FIFO lots) from the holdings and trade ledger.
5. **Charts:** indicator overlays and drawing the detected candle patterns on the stock view; intraday chart once the 15-minute bar job is fixed (W9-J1).
6. **ATIP MCP server: done in W39** (Phase 4): read-only tools so Claude can work with ATIP the way it works with Kite MCP and Dhan MCP (`tools/atip_mcp.py`).
7. **Earnings surprise / PEAD and a revisions proxy: done in W39b** (`research/earnings_surprise.py`), from ATIP's own quarterly EPS history. There are still no consensus estimates.
8. **Live execution with broker-held orders** (Dhan super order, forever / GTT order, order-update WebSocket, algo-ID, per-second throttle, static IP). **This is real-money work and needs your explicit go-ahead and a test plan.** Until then ATIP stays paper-first, which is the safer default given SEBI's finding that 91 % of individual F&O traders lost money in FY25.
9. Factor risk model, CPCV / PBO, meta-labelling: research hardening.

## 5. Owner actions found during this review

- **Dhan Data API subscription** (₹499/month): history, quotes and the new 7-year backfill need it. Without it Dhan refuses with DH-902 ("not subscribed"), which shows in `pipeline_log` for `dhan_historical` and `history_backfill`.
- **Static IP** registered at web.dhan.co, before any live API order (mandatory since 1 April 2026).
- **Dhan PIN + TOTP secret** for the token auto-refresh (PR #4).
- **Anthropic API key** (KD-001, returns 401): it blocks news AI, concall analysis and the assistant.
- **SEBI:** ATIP's reports are for your own use. Sharing ratings or targets with others, or offering a black-box algo, requires SEBI Research Analyst registration.

## Sources

**Dhan:**
- https://dhanhq.co/docs/v2/super-order/
- https://dhanhq.co/docs/v2/conditional-trigger/
- https://dhanhq.co/docs/v2/traders-control/
- https://dhan.co/support/platforms/dhanhq-api/what-timeframe-data-is-available-through-dhan-s-historical-data-apis/
- https://dhan.co/support/platforms/dhanhq-api/how-does-the-dhanhq-data-api-subscription-work/
- https://docs.dhanhq.co/mcp/
- https://dhan.co/support/platforms/options-trader/what-is-the-strategy-builder/
- https://dhan.co/scanx-stock-screener/

**Zerodha:**
- https://github.com/zerodha/kite-mcp-server
- https://github.com/zerodha/pykiteconnect
- https://zerodha.com/z-connect/business-updates/introducing-trailing-stoploss-on-kite-web
- https://zerodha.com/z-connect/kite/introducing-screener-on-kite-web
- https://kite.trade/forum/discussion/15912/preparing-to-comply-with-sebis-retail-algo-rules-static-ip-ratelimits-order-types
- https://blog.sensibull.com/2023/07/24/sensibull-is-free-for-all-zerodha-users/

**Institutional methods:**
- https://www.kotakneo.com/disclaimer/research-v2/ (rating bands)
- https://s205.q4cdn.com/437373358/files/doc_downloads/2025/07/Morningstar-Equity-Research-Methodology-2023-06-14.pdf
- https://www.nasdaq.com/articles/zacks-rank-explained-how-find-strong-buy-basic-materials-stocks-1
- https://help.seekingalpha.com/premium/quant-ratings-and-factor-grades-faq
- https://trendlyne.com/score-details/
- https://www.msci.com/documents/10199/242721/Barra_US_Equity_Model_USE4.pdf
- https://papers.ssrn.com/abstract=2460551 (deflated Sharpe)
- https://arxiv.org/html/2508.02739v1 (Kronos)

**Regulation:**
- https://taxmann.com/post/blog/sebi-mandates-ias-and-ras-to-disclose-use-of-ai-tools-in-providing-investment-advice-research-services-to-clients
- https://www.outlookbusiness.com/markets/sebi-extends-deadline-to-implement-retail-algo-trading-by-april-2026
- https://moneylife.in/article/91-percentage-of-retail-traders-lost-money-in-derivatives-losses-in-fo-surged-41-percentage-to-rs105-lakh-crore-in-fy2425-sebi-study/77613.html

**Not verified in this research:**
- Dhan MCP's exact launch date.
- Whether Kite MCP's hosted server places orders today (sources conflict).
- How Dhan tags orders with an algo ID in practice.
- Whether SEBI's final AI/ML guidelines have been issued.
