# ATIP — benchmark review and prioritised gap analysis (2026-10-26)

*Sources: measurements on `atip_data/atip.db` (§1), a read-only code audit at 912e654 whose P0 claims were
re-checked line by line (§3), today's runtime evidence (reports 01 and 02), and the 2026-10-26 master file. Effort:
S ≤ 1 day, M 2–5 days, L > 1 week.*

## 1. Signal quality, measured

Every stored score row (38 sessions, 27 Jul – 26 Oct 2026, ~499 stocks a session) against the return from the next
session's **open** to the close 5 or 10 sessions later, net of 0.32% round trip (statutory delivery ≈ 0.22% + 0.10%
slippage). IC = daily cross-sectional Spearman correlation. Quintiles are within each date.

| Score | IC 5d (t) | IC 10d (t) | Best fifth: hit / expectancy (10d) | Worst fifth (10d) | Reading |
|---|---|---|---|---|---|
| VPI | +0.009 (0.9) | **+0.036 (4.6)** | 34.8% / −2.05% | 28.4% / −2.20% | the one score with a consistent positive ranking |
| ZPI | −0.017 (−0.8) | −0.001 (0.0) | 33.3% / −1.92% | 31.3% / −2.06% | no edge |
| MRI | **−0.056 (−5.4)** | **−0.064 (−5.3)** | 28.6% / −2.55% | 34.1% / −1.65% | ranks the wrong way in this sample: high MRI did worse |
| RRI | −0.039 (−1.6) | −0.014 (−0.6) | 32.6% / −2.43% | 30.0% / −1.92% | no edge |
| CRI (low = best) | −0.037 (−2.3) | −0.031 (−2.9) | 29.2% / −2.50% | 30.7% / −2.33% | works as intended as a risk score (higher CRI → worse) but weakly |
| ACS | +0.019 (0.8) | +0.033 (1.8) | 30.4% / −2.26% | 29.1% / −2.22% | weak |
| News score | +0.023 (2.9) | +0.012 (1.2) | — | — | positive-news stocks (score > 55): hit 42.6% / −0.46% vs neutral 34.4% / −1.48% (5d); but 88% of rows are neutral |
| ATIP master | −0.034 (−1.7) | −0.008 (−0.4) | 32.2% / −2.39% | 28.3% / −2.17% | **no edge**: the top fifth did not beat the bottom fifth |
| MSI, MH (market-wide) | corr with next 5d universe return: MSI +0.24, MH −0.19 (33 dates) | | | | too few dates to say anything |
| **BUY signal** | 23 signals (5d): hit 30.4%, −1.62% | 21 signals (10d): hit 47.6%, −0.24% | touched +3% within 10 sessions: 52–57% | | **insufficient data** (≈ 22 signals) |

Context: the whole universe lost −1.38% (5d) / −2.11% (10d) net on average in this window — Nifty fell 5.2% from
27 Jul to 26 Oct (−13% in 2026). Every bucket is negative because the market was; what matters is the spread between
buckets, and it is small. The t-statistics are overstated (overlapping 5/10-day windows on consecutive dates, one
3-month phase). Honest reading: **no ATIP score has a demonstrated, tradable edge yet**; VPI is the best candidate
and MRI's direction deserves a second look (the master file already fixed one MRI sign error in the exit logic).
Raw: `evidence/signal_quality.json`.

## 2. ATIP against best practice

| Area | Best in class (who) | ATIP today | Gap |
|---|---|---|---|
| Strategy research and backtesting | QuantConnect/LEAN: point-in-time universes incl. delisted, tick/minute data, fill models, walk-forward; AmiBroker: fast portfolio-level backtests, Monte Carlo, walk-forward optimisation | Event-driven engine, walk-forward, CPCV/PBO, Monte Carlo, an itemised NSE cost model (`backtest/`); the headline harnesses (`scores/backtest.py`, `strategy/backtest_history.py`) are simpler: in-sample, today's Nifty 500 back-filled, stops filled at the level on gap-downs, no circuit limits | P1: OOS gate + point-in-time adjusted universe + gap-through fills in the harnesses everyone quotes |
| Data depth | Institutional: 15–20+ years EOD, intraday, options history | 1.6 years EOD (Mar 2025 →); 15-min bars for 6 sessions only; F&O EOD 16+ sessions; no option-chain history; 7-year backfill blocked (Dhan plan) | P0/P1: Dhan Data API; historical source for 2018+ |
| No-code strategies + live deployment | Zerodha Streak, Tradetron: scanners, alerts, broker-integrated deployment with exchange-compliant order flow | Strategy library (17), builder, lifecycle DRAFT → ACTIVE, PAPER default; `strategy_decision` has never had a row; LIVE path lacks fill confirmation and broker-side stops | P0 before real money (below) |
| Options analytics | Sensibull, Opstra: live chains, IV/IV-percentile history, payoff and greeks, margin calculator | Options builder, paper option strategies, EOD F&O analytics with ATM IV and PCR; no live chain (Dhan plan), no IV history, no margin calculator | P1 (only if options are in scope; ₹25K cannot carry most option-selling margins) |
| Options backtesting | AlgoTest: tick/minute options backtests with real premiums and slippage | none (no options history) | P2 |
| Execution | Institutional: OMS/EMS, exchange-held stops, order/trade-book reconciliation, rate limiting, full audit trail | W4 OMS (PAPER-only, append-only events); W1 LIVE path: MARKET/LIMIT only, no fill polling, no broker stops, no rate cap | P0 |
| Risk | Institutional: pre-trade limits on every path, VaR, correlation/concentration, kill switch, independent risk | Strong in the W4 engine (1% risk, 10%/30%/25% caps, 3% daily loss, 15% DD); the W1 LIVE path checks kill switch, funds, price band only and values a MARKET order at 0 without a quote | P0 |
| Pre-market intelligence | Claude **share-market** skill's 08:30 report: GIFT Nifty, US/Asia/Europe, crude, gold, USD-INR, regime, Market Health incl. GIFT premium, per-stock expected gap, news sentiment, trend classification | Morning brief: MH/regime, Nifty, VIX, GIFT % (never captured), global score + S&P, previous evening's BUYs; no crude/FX/FII lines, no news, no re-ranking before the open | P1: build the morning re-rank and fill the brief (GAP-2610-10) |
| News intelligence | AI classification with entity linking (institutional NLP; the skill's Module F) | Keyword classifier only; 3 of 9 feeds dead; 88% of rows neutral | P1 |
| Terminology | — | The share-market skill expands VPI, MRI, RRI, ZPI and ACS differently from ATIP's code (e.g. "Market Risk Index" vs code's Momentum-Reversal Index) — the same disagreement the master file found in the strategy library and formulas workbook | P2 (R8): one glossary |
| Compliance | Brokers' SEBI 2025 retail-algo stacks: static IP, order-rate cap, Algo ID, exchange registration above the threshold | None of the controls are wired; LIVE is off | P0 before any automated LIVE order |
| Monitoring | External dead-man switch, multiple channels, SLOs | In-process health checks, job-health alerts, broker health, Telegram; 24 h backup RPO | P1 |

## 3. Prioritised gap list

### P0 — must be fixed before real money

| # | Gap | Effort | Files |
|---|---|---|---|
| P0-1 | **Dhan Data API plan inactive** (quotes, WebSocket, intraday bars, history, chains). Without it paper fills, the exit manager, intraday scans and stale-data gates cannot work | owner action (subscription); verification S | data/dhan.py (now reports it) |
| P0-2 | **LIVE fill confirmation**: poll order/trade book or use the order-update feed; partial fills; a real buy whose acknowledgement has no price is reported "did not fill" and never managed; exits are booked at the hint price on acknowledgement | M | strategy/live.py, orders/broker.py, orders/rules.py |
| P0-3 | **Broker-side protective stop at entry** (SL-M / Forever order), not only ATIP's 2-minute / 30-second LTP checks; let exits pass the kill switch | M | orders/broker.py, strategy/live.py, orders/risk.py |
| P0-4 | **LIVE path fails closed**: refuse a BUY when the price is unknown (`_estimate_order_value` returns 0 today); route W1 orders through `execution/risk_engine.py` limits | M | orders/broker.py, orders/risk.py |
| P0-5 | **LIVE reconciliation** of `order_log` / `strategy_position` / `order_rules` against Dhan's order and trade books, daily and at start-up; alert on every failed live order | M | execution/reconcile.py, strategy/live.py |
| P0-6 | **SEBI retail-algo controls** (circular 4 Feb 2025; full compliance from 1 Apr 2026): order-rate limiter (`execution.max_orders_per_second` is discarded as an unknown key, so the compliance check can never pass), Algo ID tagging if above the threshold, static-IP design with Dhan, live switch enforcing the regulatory register | M | execution/config.py, orders/broker.py, ops/compliance.py, ops/trading_safety.py |
| P0-7 | **One trading instance per broker account** (lock is per `atip_data` folder; a second folder or worktree trades the same Dhan account) | S | main.py, ops/jobs.py |
| P0-8 | **Proven edge**: no score or strategy has positive out-of-sample expectancy after costs on ATIP's own data (§1; report 04) | L (research + calendar time) | scores/, strategy/, research/exit_target_study.py |
| — | *Fixed today:* holes in `prices_daily` (ac58625); silent data outage + unalerted unmanaged exits (63767d8); bulk deals on wrong sessions (1ecda6b) | done | — |

### P1

| # | Gap | Effort | Files |
|---|---|---|---|
| P1-1 | Pre-market inputs feed the decision: 09:10 gate / re-rank on GIFT gap, pre-open imbalance, global cues, overnight news | M | scores/engine.py, data/preopen.py, research/market_pulse.py, pipeline/scheduler.py |
| P1-2 | News: replace 3 dead feeds, NSE announcements as primary stock news, AI classification with a working key and cost cap | S–M | data/news.py, data/news_ai.py, data/announcements.py |
| P1-3 | Morning brief: crude, USD-INR, FII/DII, news tone, data-health line; never sent after 09:15 | S | alerts/telegram.py, pipeline/recover.py |
| P1-4 | Out-of-sample gate and point-in-time, split/bonus-adjusted universe for the headline backtests; gap-through stop fills; circuit limits; no overlapping trades in one symbol | M | scores/backtest.py, strategy/backtest_history.py, backtest/data.py |
| P1-5 | One statutory cost model everywhere: DP charge (defaults 0), options costs (exchange, stamp, GST, STT on exercise), paper charges (0.05%/leg + 5 bps understates), the 0.25% applied two different ways | S | backtest/costs.py, orders/paper.py, execution/options_paper.py, strategy/aggressive.py |
| P1-6 | Paper trading without Dhan quotes: next-open fills from the Bhavcopy for EOD strategies; W4 PAPER orders must not fill at the signal-day close | S | orders/paper.py, execution/order_manager.py |
| P1-7 | Alerts on degraded statuses (PARTIAL quote refresh, MODE_MISMATCH), quote staleness in minutes, an external dead-man switch | S | pipeline/health.py, ops/data_health.py |
| P1-8 | VPS readiness: NSE blocks cloud IPs for the Bhavcopy, so a hosted ATIP needs a data relay from a residential IP or a licensed feed | M | data/bhavcopy.py, deploy/ |
| P1-9 | Risk across parallel sessions: correlation/VaR/ADV limits are off; ten ₹25K sessions on the same signals are one ₹2.5L position set | S | execution/config.py, execution/risk_engine.py |

### P2

| # | Gap | Effort | Files |
|---|---|---|---|
| P2-1 | R12 — **fixed by the cloud session in 5ee26cb, merged here as f6ceb25**: per-session tokens for other tenants. Left: rotate the install token before ever enabling `accept_legacy_token`; browser-test a tenant session's writes | S | dashboard/security.py, enterprise/authz.py |
| P2-2 | R10: hosted test server behind the owner gate (PR #11) | M | deploy/, Dockerfile |
| P2-3 | `pnl_daily` LIVE day P&L after a missing day | S | portfolio/pnl.py |
| P2-4 | One glossary for VPI/MRI/RRI/ZPI/ACS across code, library, workbook and the share-market skill | S | docs/, strategy_engine/library |
| P2-5 | Backups: off-site copy on, RPO below 24 h | S | ops/backup.py |
| P2-6 | Options backtesting (only if options come into scope) | L | backtest/ |
