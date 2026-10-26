# ATIP — trade and profit feasibility to 31 December 2027

**Plan under review:** 10 parallel trading sessions of ₹25,000 each (₹2,50,000 in total).
**Rule followed:** every number below is backtest output on ATIP's own database, a measured broker figure, or a
published SEBI figure. Where the data cannot support a number, the report says **insufficient data**. Nothing is
assumed about future returns. *Prepared 26 Oct 2026. This is an engineering assessment, not investment advice.*

## Answer in brief

- On ATIP's own data, **no strategy ATIP can run today has a proven edge after costs.** Of 60 combinations of entry rule,
  exit and position count, run as ₹25,000 books, one was profitable (+₹1,700 over 0.7 years). It has 33 trades and
  turns negative when it holds 5 positions instead of 3. ATIP's default exit lost money with every entry rule. ATIP's
  own BUY signals lost on average (−0.8% to −2.1% per trade, about 15–19 trades, insufficient data).
- **Projection to 31 Dec 2027 for ₹2.5 lakh**, from 5,000 resamples of the best qualifying backtest (dip entries, stop and
  time exit, 5 positions, Sharpe 0.06): conservative (P10) **−₹51,907**, base (P50) **+₹1,100** (+₹872 after tax),
  optimistic (P90) **+₹63,126** (+₹49,996 after tax). **Probability of a loss: 49%.** Median worst drawdown −19.8%; 1 path
  in 20 is worse than −33.7%. Run as a mix of distinct strategies, the median is **−₹32,821** and the probability of a
  loss **77%**.
- **₹25,000 per session limits what can be traded.** One lot of NIFTY futures needs ₹1,67,593 of margin and writing one NIFTY
  option needs ₹1,58,395, both from Dhan's margin calculator. Only equity delivery trades (3–5 positions) and buying one
  option lot fit in a session.
- **The data does not support a reliable projection yet.** There are 1.65 years of daily bars covering one rally and one
  decline, no intraday or options history, and three months of keyword-classified news. Dhan's market-data plan is inactive.
- SEBI's July 2025 study found that **91% of individual F&O traders lost money in FY25**, with aggregate net losses of
  **₹1,05,603 crore**. That is the base rate any plan has to beat.
- **Recommendation:** do not deploy ₹2.5 lakh now. Follow the staged plan in §6. Paper trading comes first. Then one small
  live session, and scale up only if live results match the backtest.

## 1. Data sufficiency

| Data | Available in ATIP | What is missing |
|---|---|---|
| Daily equity bars | 2025-03-03 → 2026-10-26: **407 sessions (1.65 years)**. 475 symbols have full history; the full NSE list (~3,300) is stored only from Sep 2026. 0 sessions are missing after today's repair | 5–10+ years; point-in-time constituents including delisted names (before Sep 2026 the universe is today's tracked Nifty 500 = **survivorship bias**); split/bonus adjustment for untracked names |
| Market regimes covered | Nifty +18.1% in 2025 (peak 26,329 on 2 Jan 2026), then −13% in 2026 (March fall to 22,331; 22,753 on 26 Oct). Market Health: 263 NEUTRAL, 104 BULL, 31 BEAR, 5 HIGH_RISK, 3 STRONG_BULL sessions | a full cycle; a sustained bear market; a high-volatility year |
| Intraday | 15-minute bars for **6 sessions** (28 Sep – 5 Oct 2026); no ticks | everything intraday: **insufficient data** for intraday strategies |
| Options chain / IV | EOD F&O Bhavcopy for 17 sessions (28 Sep – 26 Oct 2026): OI, PCR, ATM IV, lot sizes. **No live option-chain snapshots** (Dhan plan inactive) | IV history, intraday premiums, bid-ask: **insufficient data** for any options backtest |
| News | 3,664 articles, 26 Jul – 26 Oct 2026, all keyword-classified, none 13–22 Oct | AI-classified history; stock-level coverage (88% of scored rows neutral) |
| ATIP scores and signals | `ai_scores` 38 sessions (27 Jul – 26 Oct); `signal_log` 76 BUY signals | years of point-in-time scores: **insufficient data** to measure the signal engine's expectancy |
| Live data | Dhan Data API plan **Deactive** since ~18 Sep 2026 (quotes 806, history DH-902) | quotes, WebSocket, intraday bars, chains, 7-year backfill |

## 2. Capital feasibility at current lot sizes

Lot sizes from NSE's F&O Bhavcopy (26 Oct 2026); margins from Dhan's margin calculator (read-only, 26 Oct 2026, prices
from the same Bhavcopy). Raw: `evidence/dhan_margins_2026-10-26.json`.

| Strategy family | Lot | Notional | Margin for 1 lot | Possible with ₹25,000? |
|---|---|---|---|---|
| NIFTY future (Nov) | 65 | ₹14,87,941 | **₹1,67,593** | No |
| BANKNIFTY future (Nov) | 30 | ₹16,43,178 | **₹1,85,675** | No |
| RELIANCE stock future (Nov) | 500 | ₹6,03,500 | **₹1,06,248** | No |
| Write 1 NIFTY 22750 CE (3 Nov) | 65 | premium ₹16,972 | **₹1,58,395** | No |
| Hedged spreads / iron condors | 65 | — | not measured: the calculator prices single legs only, not hedge benefit | Unknown; very likely above ₹25,000 per lot |
| Buy 1 NIFTY 22750 CE (3 Nov) | 65 | — | **₹16,972** (the premium) | 1 lot only, 68% of the session in one premium |
| Equity delivery (CNC) | 1 share | — | full price, no leverage | **Yes**: 3–5 positions of ₹5,000–8,300 |

At that size the depository (DP) charge per sell, ₹15.93 (an input to verify against a Dhan contract note), costs
0.19% of a ₹8,333 position and 0.32% of a ₹5,000 position, in addition to statutory charges of 0.222% (STT, stamp,
exchange, SEBI, GST; Dhan delivery brokerage ₹0) and an assumed 0.10% slippage.

## 3. Backtests per session (₹25,000 book)

**Engine:** `research/exit_target_study.py` (merged with #18) on `prices_daily`.
- **Entries and fills:** signal at the close, fill at the next open; no entries in BEAR/HIGH_RISK regimes; turnover ≥ ₹5 Cr a day; 10-session cooldown.
- **Stops and costs:** stops and targets that gap through fill at the open; costs on every leg, plus the DP charge in rupees on every sell leg.
- **Sizing:** integer shares, each position sized at equity ÷ slots.
- **Exits and convention:** stop 3% and hold up to 30 sessions; same-bar ambiguity resolved against the trade.

A **successful trade** = a closed trade with net profit after all costs. Raw: `evidence/phase4_backtests.json`.

| Book (entry · exit · positions) | Years | Trades / yr | Successful / yr | Win rate | Avg win | Avg loss | Expectancy / trade | Net P&L | Sharpe | Max DD | Worst month |
|---|---|---|---|---|---|---|---|---|---|---|---|
| RSI mean reversion · flat +4% · 3 | 0.70 | 47.1 | 28.5 | 60.6% | +3.47% | −3.51% | **+0.72%** (₹51.5) | +₹1,700 | **1.03** | −4.4% | −1.6% (Jun 2026) |
| RSI mean reversion · flat +4% · 5 | 0.70 | 59.9 | 31.4 | 52.4% | +3.31% | −3.71% | −0.04% | −₹44 | −0.01 | −5.3% | −3.3% |
| Dip · stop + time exit · 5 | 1.49 | 95.7 | 21.4 | 22.4% | +13.31% | −3.76% | +0.06% (−₹1.7) | −₹245 | 0.06 | −14.9% | −10.5% (Mar 2026) |
| Momentum · ATIP default (T1 +3%) · 3 | 0.77 | 49.4 | 20.8 | 42.1% | +4.67% | −3.51% | −0.07% | −₹483 | −0.10 | −10.0% | −7.4% |
| Momentum · ATIP default · 5 | 0.77 | 95.8 | 35.0 | 36.5% | +4.91% | −3.68% | −0.55% | −₹2,572 | −1.09 | −15.4% | −7.0% |
| Momentum · ATIP at +4% · 5 | 0.80 | 86.0 | 33.7 | 39.1% | +5.81% | −3.62% | +0.07% (−₹7.5) | −₹517 | −0.16 | −12.1% | −8.7% |
| Dip · ATIP default · 5 | 1.48 | 111.1 | 43.4 | 39.0% | +4.66% | −3.64% | −0.40% | −₹3,596 | −0.95 | −23.1% | −8.3% |
| Breakout · ATIP default · 5 | 1.42 | 103.5 | 28.9 | 27.9% | +5.08% | −3.41% | −1.04% | −₹5,785 | −1.44 | −25.4% | −9.2% |
| RSI mean reversion · ATIP default · 5 | 0.79 | 47.9 | 17.6 | 36.8% | +3.91% | −3.35% | −0.68% | −₹1,123 | −0.81 | −6.4% | −3.9% |
| Random entries (control) · ATIP default · 5 | 1.53 | 99.3 | 37.2 | 37.5% | +3.75% | −3.69% | −0.90% | −₹5,787 | −1.51 | −24.7% | −9.4% |
| Momentum · flat +4% · 5 | 0.72 | 401.5 | 178.4 | 44.4% | +3.28% | −3.83% | −0.67% | −₹7,317 | −3.15 | −31.2% | −7.4% |
| Breakout · flat +4% · 5 | 1.33 | 389.4 | 157.0 | 40.3% | +3.08% | −3.96% | −1.12% | −₹15,819 | −5.06 | −62.5% | −13.0% |

All 60 books are in the evidence file. **ATIP's own BUY signals** (27 Jul – 26 Oct; 15–19 trades per book; 0.16–0.22
years): win rate 12.5–33%, expectancy −0.81% to −2.06% per trade, net −₹995 to −₹2,901 per ₹25,000 — **insufficient
data** for annual figures.

**Reading the table:**

1. **The one profitable book rests on 33 trades.** Two were before 2026 (+3.47% each) and 31 after (+0.54% each).
   It was selected as the best of 60, which flatters it. With 5 positions it is negative.
2. **The edge decides everything, and the evidence has not found one.** The random-entry control and the rule-based
   entries land in the same band once costs are paid.
3. **A high win rate does not mean profit.** Flat-target books win 40–45% of trades at about +3.3% and lose about −3.9%
   on the rest. After costs the expectancy is negative.

## 4. Projection to 31 December 2027

**Method.** For each book that has at least 0.8 years of history, I resampled its backtested daily returns in
20-session blocks over the 293 sessions from 27 Oct 2026 to 31 Dec 2027, 5,000 times. This adds no assumption
about returns: it replays what the backtest earned, in a different order. Tax: short-term capital gains on listed
shares (s.111A) at 20% plus 4% cess, charged on a positive gain only. That is an assumption to confirm with a CA;
frequent trading can instead be taxed as business income at slab rates.

**Scenarios for ₹2,50,000**, using the best qualifying book (Dip · stop + time exit · 5 positions) in all 10 sessions:

| Scenario | Net P&L before tax | After tax | Return on ₹2.5L after tax |
|---|---|---|---|
| Conservative (P10) | **−₹51,907** | −₹51,907 | −20.8% |
| Base (P50) | **+₹1,100** | +₹872 | +0.3% |
| Optimistic (P90) | **+₹63,126** | +₹49,996 | +20.0% |

| Risk measure | Value |
|---|---|
| Probability of a loss by 31 Dec 2027 | **49.1%** |
| Max drawdown (median path / 1 in 20 paths) | −19.8% / **−33.7%** |
| Successful trades across 10 sessions | ≈ 21.4 per session-year × 1.18 years × 10 ≈ **250** (of ≈ 1,130 trades) |

If the 10 sessions run **the same book**, they are one position set held ten times. The returns are perfectly
correlated, so the percentages above apply unchanged. A **mix of the four distinct qualifying books** does not help
either: P10 −₹77,213, P50 **−₹32,821**, P90 +₹27,302, probability of a loss 77.1%, median drawdown −23.3%. ATIP's
default exit on dip entries gives P50 −₹74,123 with a 97.1% probability of a loss.

**Insufficient data** to project the one profitable book (RSI mean reversion · flat +4% · 3 positions): it has 0.7 years
and 33 trades, below the 0.8-year minimum used here.

## 5. Reality check against SEBI's studies

SEBI's July 2025 study of individual F&O traders (FY25) found:
- 91% lost money;
- aggregate net losses of ₹1,05,603 crore, up 41% from ₹74,812 crore in FY24;
- an average loss of about ₹1.1 lakh per trader.

ATIP's measured numbers agree with that base rate. Neither its signals nor its exits show an edge yet, and ₹25,000
cannot carry the margin that option-selling strategies need. The only F&O route that fits is option buying, the
group the study shows losing most often. The SEBI retail-algo framework (circular of 4 Feb 2025, full compliance from
1 Apr 2026) also has to be met before any automated live order: a static IP, an order-rate cap, and an Algo ID or
exchange registration above the threshold. None of these is implemented yet (gap list P0-6).

## 6. Recommended staged rollout, November 2026 to December 2027

| Stage | When | What | Go / no-go gate |
|---|---|---|---|
| 0. Prerequisites | Nov 2026 | Subscribe to Dhan's Data APIs. Keep the Mac awake from 07:00 on trading days. Fix the cost model, paper next-open fills and alerts (gap list P1-5/6/7). Pre-register 2 profiles, e.g. RSI mean reversion · flat +4% · 3 positions, and Dip · stop + time · 5 | Data, scores and paper fills run every session for 2 weeks without gaps |
| 1. Paper trading | Dec 2026 – Feb 2027 | ≥ 60 sessions and ≥ 30 closed paper trades per profile. Weekly review. Walk-forward on the new data | Paper expectancy > 0 after all costs and inside the backtest's 90% band; probability of backtest overfitting (PBO) < 0.3 |
| 2. One live session, small | Mar – May 2027 | First fix the LIVE P0s (fill confirmation, broker-side stops, fail-closed pricing, reconciliation, single instance, SEBI controls). Then run 1 session of ₹25,000 with at most 3 positions, a ₹500 daily-loss limit and a stop at −10% drawdown | 30 live trades; live slippage and fills within the paper results; expectancy confidence interval not below 0 |
| 3. Three sessions | Jun – Sep 2027 | Only if Stage 2 passes: 3 sessions with different, low-correlation profiles; correlation and VaR limits on | Drawdown ≤ 10%; combined expectancy > 0 after tax |
| 4. Toward 10 sessions | Oct – Dec 2027 | Add sessions only while every earlier gate still holds. Otherwise hold or stop | The same gates, re-checked monthly |
| Kill criteria, any stage | — | Expectancy confidence interval below 0 after 50 trades; drawdown > 10%; any reconciliation break or unconfirmed live fill | Stop and go back one stage |

Two facts the owner should weigh. The Dhan account shows **₹0 available funds** (fund-limit check, 26 Oct). Its
holdings are worth about **₹5.78 lakh**, so ₹2.5 lakh would have to be added or freed. Every figure in §3–§4 also
covers one rally and one decline only. A third phase, such as a sustained bear market, is not in the data.

*Sources: `evidence/phase4_backtests.json`, `evidence/signal_quality.json`, `evidence/dhan_margins_2026-10-26.json`;
SEBI study as reported by Business Standard (7 Jul 2025) and Value Research; SEBI circular of 4 Feb 2025 on retail
algorithmic trading, as summarised by Zerodha and Fyers.*
