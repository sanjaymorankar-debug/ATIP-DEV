# Pre-Open Checklist: backtest and accuracy summary

Run 23 Nov 2026, 00:13 IST, by `tools/preopen_backtest.py`, on the history that exists. It uses the live checklist's
rules and default thresholds: US band ±0.10 %, VIX 15, A/D ratio 1.0, GIFT band ±0.15 %, bias at a vote sum of ±1.

**Sessions:** 611 Nifty sessions, 4 Jun 2024 to 20 Nov 2026.
**Outputs:** [backtest/backtest.json](backtest/backtest.json), [backtest/backtest_days.csv](backtest/backtest_days.csv)
(one row per session, every input and outcome), [backtest/backtest_tables.md](backtest/backtest_tables.md) (all
windows and years).

## What could be replayed, and what could not

| Check | Replayed? | Data, and what 09:00 IST would have known |
|---|---|---|
| 1. US cues | **Yes**, 611 sessions | Nasdaq / Dow: the last US daily close before the Indian date (Yahoo). NQ / YM: the close of the last hourly bar that ended by 09:00 IST (in practice 08:30 IST) against the previous settlement. Hourly futures exist only from 2 Jul 2024. |
| 2. India VIX | **Yes**, 608 sessions | Previous session's close (Yahoo `^INDIAVIX`). |
| 3. NSE A/D, prior-day | **Yes**, 389 sessions | ATIP's universe breadth (`market_health`, ~500 stocks, from Mar 2025). Cross-checked on the 96 days where NSE's whole-market Bhavcopy is stored (Jun–Nov 2026): the two agree on direction **84.4 %** of the time. |
| 3. NSE A/D, live pre-open | Only 15 mornings | `preopen_snapshot` (Nifty + F&O set, from 27 Oct 2026). Too few to judge. |
| 4. GIFT Nifty | **No** | No GIFT history at 09:00 exists. The only stored value, Dhan's `IDX_I` 5024, is frozen (one value all day), so its 11 scored mornings are not a valid test. GIFT is **forward-test only**: the learning loop scores it from the first live run. |
| Outcomes | — | Nifty open / close from Yahoo `^NSEI` (ATIP's `prices_daily` NIFTY50 is close-only). Yahoo's closes match ATIP's stored closes exactly on all **420** shared days (max difference 0.0000 %). |

## Results: full period, 611 sessions

Each check is scored only on days it voted (+1 / −1) and the outcome moved. **Base rate** is how often the commoner
direction occurred on those same days, so an **edge** means beating "always say the usual thing", not beating 50 %.
✓ marks an edge larger than two standard errors.

| Check | Opening gap | Day's close vs previous close | After the open (open → close) |
|---|---|---|---|
| **1. US cues** (all four legs) | **72.5 %** (base 56.7 %, **+15.8 pp** ✓), n 462 | **64.5 %** (base 50.4 %, **+14.1 pp** ✓) | 49.4 % (base 53.9 %, −4.5 pp) |
| US closes only | 69.5 % (+13.3 pp ✓) | 57.8 % (+7.6 pp ✓) | 46.8 % (−6.8 pp ✓, a fade) |
| US futures only (~08:30 IST) | 56.8 % (+3.2 pp) | 60.3 % (+8.7 pp ✓) | 51.1 % (−0.9 pp) |
| **3. A/D prior-day** (ATIP universe) | 53.9 % (base 54.4 %, −0.5 pp) | 53.1 % (+2.1 pp) | 46.1 % (−6.2 pp ✓) |
| A/D prior-day (NSE Bhavcopy, 113 days) | 47.8 % (−5.3 pp) | 52.2 % (+1.8 pp) | 48.7 % (−1.8 pp) |
| **Bias (US + A/D; GIFT not replayable)** | **69.3 %** (base 53.7 %, **+15.6 pp** ✓), n 417 | **62.8 %** (base 50.4 %, **+12.5 pp** ✓) | 47.0 % (base 53.7 %, **−6.7 pp** ✓) |

The bias was Neutral on 193 of 611 sessions.

**2. India VIX (a modifier, not a vote).** On days after VIX closed above 15 (181 sessions), the mean absolute gap was
0.487 % and the mean absolute day move 0.862 %. After VIX at or below 15 (427 sessions) the same figures were 0.293 %
and 0.518 %. Volatile days moved about **1.7 times as much**. The regime does what the advice assumes: on those days
smaller size and wider stops fit.

**Expected-open classes, actual:** Gap-Up 227, Flat 222, Gap-Down 162 sessions (±0.15 % band).

## By year and recent windows

| Window | US cues → gap | US cues → day | Bias → day | Bias → open→close |
|---|---|---|---|---|
| 2024 (Jun–Dec, 144 sessions) | 76.7 % (base 67.2 %) | 68.1 % (base 53.4 %) | 68.1 % (base 53.4 %) | 46.6 % |
| 2025 (249) | 66.3 % (base 57.3 %) | 61.2 % (base 50.0 %) | 61.3 % (base 50.7 %) | 46.0 % |
| 2026 to 20 Nov (218) | 76.2 % (base 51.2 %) | 65.5 % (base 51.2 %) | 60.3 % (base 54.3 %) | 48.3 % |
| Last 250 sessions | 74.5 % (+24.0 pp ✓) | 65.1 % (+13.5 pp ✓) | 60.9 % (+6.5 pp) | 49.1 % |
| Last 60 | 68.1 % (+12.8 pp) | 57.4 % (+6.4 pp) | 57.8 % (+4.4 pp) | 57.8 % |
| Last 20 (23 Oct – 20 Nov) | 43.8 % (−18.8 pp) | 43.8 % (−31.2 pp ✓) | 43.8 % (−25.0 pp ✓) | 62.5 % |

The last 20 sessions went against the US cues. That is 16 voting days, and every one of the 20 was a Volatile-VIX
day. The rolling 20-day record on the dashboard exists for exactly this: to show when the read stops working.

## What this means

1. **The checklist does what it claims, and no more.** US cues, and the bias built mostly on them, call the opening
   gap and the day's close against yesterday's close clearly better than the base rate, steadily across 2024, 2025
   and 2026.
2. **It is not an intraday entry signal.** By 09:15 the gap has already priced the information. From the open to the
   close the bias is right only 47 % of the time, below the base rate, and the US-close leg even shows a small,
   significant fade. Buying at the open because the bias says Bullish would not have worked. The checklist stays
   informational, as the owner asked (D2): nothing here feeds autopick, the arms, stops or sizing.
3. **Prior-day A/D adds nothing** to the gap or the close (53.9 % vs a 54.4 % base). It votes only as a labelled
   fallback until NSE's live pre-open book exists at 09:00–09:09. The live pre-open A/D looked strong on 15 mornings
   (12/15 on the gap), but that is too few to trust: the learning loop will measure it.
   *Proposed, not done:* if the live pre-open A/D also shows no edge after 60 scored days, stop prior-day A/D from
   voting. That is a rule change, and it needs the owner's yes.
4. **GIFT Nifty cannot be backtested** from ATIP's data. Its first honest record starts with the first live run on
   the VPS.
5. **VIX above 15 works as a volatility flag**: gaps and day moves are about 1.7 times larger.

## Reproduce

```bash
python -m tools.preopen_backtest --data <dir> --fetch --db atip_data/atip.db --out docs/reports/2026-11-22-preopen-checklist/backtest
```

On 23 Nov this used the Mac's `atip_data/atip.db`, opened read-only. It stops at 17 Nov 2026, so the A/D breadth runs to
16 Nov. The Yahoo files were downloaded at 00:13 IST 23 Nov.
