# Why ATIP didn't call Monday 23 Nov's fall (Nifty −1.75 %)

Checked 21:00–21:20 IST from the VPS, read-only. Sources: alert_log (the 08:30 brief), market_cue, market_health,
market_regime_gate, market_breadth, asia_sentiment, global_markets, global_market_history, index_levels,
post_market_rca, and the code at ade78e9 (what the VPS runs).

## What happened
- **Before Monday, the world fell. India didn't follow.**
  - US Thursday: S&P −2.02 %, Nasdaq −2.25 %.
  - Asia Friday: Nikkei −5.06 %, TAIEX −6.23 %, KOSPI −3.01 %.
  - **US Friday night: S&P −3.70 %, Nasdaq −5.28 %, Dow −1.93 %**, gold −4.6 %, silver −7.4 %.
  - Nifty still closed **+1.14 % on Thursday and +1.12 % on Friday**, with FII buying ₹+10,308 Cr on Friday.
- **Monday:** Nifty opened up (+0.5 %, 25,929). It turned down at 09:16 and fell steadily to 25,349.65 at the close
  (low at 15:12). VIX went from 17.3 to 18.5. PSU banks and metals led the fall; IT was up. In short, India caught up
  with the global sell-off in one day.

## What ATIP said at 08:30 (Morning Brief)
It **did** carry warnings:
- Asia **12 BEARISH**.
- News impact **15 NEGATIVE, strong**.
- Global score **3**.
- Stance "Reduced sizing — only ACS > 70".
- No Trade of the Day.

The bullish signals were India's own:
- Market Health 53 NEUTRAL.
- GIFT pointed to a gap up. That part was right: it opened +0.5 %.
- Regime gate CONFIRMED_UPTREND with 0 distribution days.
- Breadth50 56.8, so the gate was open.

## Why that didn't become a "market will fall" call (5 reasons)
1. **ATIP's main formulas pick stocks, not the index.** ATIP, VPI, ZPI, ACS and CRI rank stocks on Friday's close.
   None of them forecasts where Nifty goes today.
2. **The gates that control buying only look at India's own trend.**
   - The regime gate and the breadth gate were open, because Nifty had just risen 1.1 % twice.
   - The global, Asia, news and VIX signals are text in the brief only. `orders/autopick.py`, `orders/arms.py` and
     `orders/strategy_v2.py` never read them (VIX "advice only" was decision D2).
   - The "Reduced sizing" stance is not enforced either.
   - So at 09:25, S7 re-bought ICICIBANK (score 65.4) with Nifty already below its open. That re-buy is −₹927 today.
3. **Market Health averaged the warning away.** FII 100 and Nifty trend 69 offset Global 3 and Asia 12, which gave
   53 = NEUTRAL.
4. **Data bugs hid the strongest signal:**
   - **The Monday brief shows Friday's market data.** `alerts/telegram.py` `send_morning_digest` reads
     global_markets, index_levels and market_health "as of the last scored day" (Friday). It printed S&P −2.02 %
     (Thursday's move). Friday night's −3.70 % S&P and −5.28 % Nasdaq were already stored at 06:00 IST Monday but
     weren't shown. "Nifty +1.12 %" and "GIFT +0.00 %" were Friday's numbers too.
   - **The global-cue model has never run.** `research/market_pulse.global_cue_model` turns last night's
     S&P/Nasdaq/Nikkei/Hang Seng/crude/dollar/gold moves into an expected Nifty move, with a walk-forward hit rate.
     It needs 80+ sessions of global history. The VPS has only 31–46 days (since 29 Sep), so `model_expected_pct`,
     `cue_score` and `cue_label` are empty every day. The backfill (`data.markets.backfill_global_history`) was
     never run.
   - **GIFT:** the Dhan GIFT quote was frozen at 25,854.5 all morning. The reference was Friday 08:45's GIFT, not
     Friday's close. The 15:31 reference with its contract (DEF-06) was first stored today, so it is used from 25 Nov.
5. **The pre-open checklist missed its first run.** In 611 sessions, its US-cue bias called the close direction
   62.8 % of the time (base 50.4 %). It went live at 09:36, after today's 09:00 run. First run: Wed 25 Nov.

Honest limit: ATIP's own 611-session study found **no signal that predicts open → close** (47 %, a coin flip).
Today's gap-up was predicted correctly. The fall after the open is the part nothing predicts reliably. The
realistic goal is to **react**: buy less on risk-off mornings and after a weak open.

## Fixes, smallest first
| # | Fix | Effect | Needs |
|---|---|---|---|
| 1 | Monday brief: read global, index and market rows for **today**; keep Friday only for the scores | The brief shows the real overnight move | code fix + deploy (owner yes) |
| 2 | Turn ON the tested guard "no buy while Nifty is below its open or 0.5 %+ down" (and "buy only above the 09:15–09:44 high after 09:45") | RCA: +₹658 today each; would have blocked the 09:25 ICICIBANK re-buy | owner yes (built, OFF) |
| 3 | Backfill 5 years of global history so the global-cue model runs; check its walk-forward hit rate on a copy first | A number for "last night's world says Nifty ±x %" every morning | local test first, then VPS (owner yes) |
| 4 | Only if #3 shows an edge: a "global risk-off" hold on new auto-pick buys (e.g. model ≤ −0.5 % or S&P ≤ −2 %) | Stops buying into catch-up days like today | backtest/replay before any code (house rule) |
| 5 | Pre-open checklist: nothing to do; it runs from Wed 25 Nov | US-cue bias vote at 09:00 | — |
