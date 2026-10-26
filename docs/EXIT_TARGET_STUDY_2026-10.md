# Profit target study: 3 %, 4 % or 6 %? (26 Oct 2026)

**Question:** with ATIP's rule engine and parameters, which profit % works better, 3 %, 4 % or 6 %,
and what exit strategy should go with it?

**Tool:** `research/exit_target_study.py` (this commit). It replays the same entries through
different exits and changes nothing else. It can run on ATIP's own `prices_daily`
(`--source atip`) or on public NSE data (`--source eod2`).

---

## Short answer

1. **6 % beats 3 % and 4 %, and 3 % is the weakest.** Over 2015–2026 (≈ 89,000 trades, five
   ATIP entry rules) the average profit per trade rose steadily from 3 % → 4 % → 6 % (→ 8 %).
   The order held under both ways of reading daily bars, at every stop width and at both cost
   levels tested. Rule by rule, 6 % beat 3 % in 9 of 10 cases for a full exit and 8 of 10 for
   the split. The exceptions are the breakout rule and, for the split under one bar reading,
   momentum. A 3 % target also uses capital worst: it sells after about 2.6 sessions and
   re-enters, so a small loss per trade repeats many times.
2. **Don't sell the whole position at the target.** ATIP's existing split (sell half at the
   target, trail the rest at 4 × ATR, momentum check at twice the target) beat selling everything
   at the same %, on average. Best of what was tested: **sell half at +6 %, checkpoint at +12 %,
   trail the rest as now.**
3. **The 3 % stop is the bigger problem.** The median daily range (ATR-14) of a liquid NSE stock
   is **3.65 %**. For 70 % of entries the 3 % stop sits inside one ordinary day's move, and it was
   hit on **roughly 50–70 % of trades**. A stop scaled to volatility (2–3 × ATR) turned the average from a
   loss to a profit, even after allowing for the bigger risk per trade.
4. **Caution: the exit does not create an edge.** Most years lose money with every exit. The
   average gain comes from strong years (2017, 2020–21, 2023). In the last 21 months
   (Jan 2025 – Oct 2026, the period ATIP's own database covers) **every exit lost money, and 3 %
   lost slightly less than 6 %**. A better exit improves the average. Profit has to come from
   the entry signal.

### Recommended exit, to run on PAPER first

| Setting | ATIP today | Recommended |
|---|---|---|
| T1: sell 50 % | +3 % | **+6 %** |
| T2: momentum checkpoint | +6 % | **+12 %** |
| Runner trail | 4 × ATR, 3–20 % | unchanged |
| Initial stop | 3 % (live: equal to `target_1_pct`) | **2 × ATR, kept within 2–10 %** |
| Position size | fixed | **from risk:** qty = (equity × risk %) ÷ (entry × stop %), so a wider stop means fewer shares and the same rupee risk |

All of it goes in `atip_data/config.json`:

```json
"target_1_pct": 6, "target_2_pct": 12,
"stop_atr_mult": 2, "stop_min_pct": 2, "stop_max_pct": 10,
"risk_sizing": true,
"position_sizing": {"capital": null, "risk_per_trade_pct": 1.0, "max_position_pct": 10.0}
```

The ATR stop is worked out once per position, from the ATR at entry, and stored, so it never
moves afterwards. Positions that are already open keep the stop they had. An explicit
`--stop-pct` on `python -m strategy.live --manage` still overrides it.

With `risk_sizing` on, `python -m strategy.live --enter SYMBOL` without `--qty` buys enough
shares that hitting the initial stop loses `risk_per_trade_pct` of capital. The size is
capped at `max_position_pct` of capital. Capital is `position_sizing.capital`, or the account's
equity when that is null. The sizing uses a fresh quote and the same stop the position will
use. That stop is stored, so a later change of target cannot change the risk. An explicit
`--qty` is used as given.

**Watch out:** with no stop chosen at entry (no `stop_atr_mult`, no ATR yet, no
`--stop-pct`, no risk sizing), the live stop still falls back to `target_1_pct`. If you change
T1 to 6 that way, the stop becomes 6 % as well.

---

## What was tested

**Data.** NSE bhavcopy daily bars, adjusted for splits and bonuses, from
[eod2_data](https://github.com/BennyThadikaran/eod2_data). The data covers 2,205 stocks (ISIN INE…)
from 2015-01-01 to 2026-10-23. ATIP's own database was not reachable from the cloud session, so
run the same script on the Mac to confirm (see the end of this file).

**ATIP parameters kept as they are:** 3 % initial stop, 30-session window (`HOLD_BARS`,
`MAX_TRACK_SESSIONS`), 0.25 % round-trip cost, ₹5 Cr/day liquidity floor, EQ series, no entry on a
BEAR/HIGH_RISK market-health day (`determine_signal`; MH rebuilt from Nifty, Bank Nifty, VIX,
mid/small-cap, breadth, A/D with the `db/schema.py` weights), half sold at T1, trail
`clamp(4 × ATR, 3, 20)`, checkpoint at T2 (composite ≥ 60 hold / < 40 exit / else halve the trail).

**Entries:** the signal on day *t*'s close fills at *t+1*'s open. Rules (top 10 a day,
10-session cooldown):

| Rule | From |
|---|---|
| random | control: any liquid stock |
| momentum | close > SMA50 > SMA200, RSI 50–70, 20-day return > 0 (the ZPI/BUY-gate profile) |
| breakout | `library/12_volume_breakout.json` |
| rsi_mr | `library/11_rsi_mean_reversion.json` |
| dip | `library/40_dip.json` / `scores/backtest.py` dip mode |

ATIP's own BUY signal could not be replayed: `scores/engine.py` documents that the BUY gate has
never fired on stored scores.

**Exits:** `flat_T` sells all at +T %. `atip_T` is `strategy/aggressive.py` with T1 = T and
T2 = 2T. `hold` has no target: stop and time limit only.

**Bar order.** A daily bar does not show whether the high or the low came first, so every
result was computed twice: *pessimistic* (ATIP's own convention: low first) and *ohlc* (a
green bar goes open–low–high–close, a red bar open–high–low–close). Gaps fill at the open.
The study's `atip_3` matches `strategy/backtest_aggressive.simulate` on more than 99 % of random
test paths (`tests/test_exit_target_study.py`). The rest differ because ATIP rounds prices to two
decimals, or because the study fills a stop at the open when the price opens below it.

---

## Results (3 % stop, 30 sessions, 0.25 % cost, 2015–2026)

Average net profit per trade, % of the money put in. Each cell is the mean of the five entry
rules; the two numbers are *pessimistic / ohlc*.

| Target | Sell all at target | ATIP split (half at T, trail rest) |
|---|---|---|
| 3 % | −0.414 / −0.304 | −0.226 / −0.158 *(ATIP today)* |
| 4 % | −0.354 / −0.285 | −0.170 / −0.118 |
| **6 %** | **−0.253 / −0.224** | **−0.120 / −0.081** |
| 8 % | −0.193 / −0.178 | −0.107 / −0.068 |
| no target | +0.102 | |

Per entry rule (pessimistic; split policy):

| Rule | split 3 % | split 4 % | split 6 % | flat 3 % | flat 6 % |
|---|---|---|---|---|---|
| random | −0.203 | −0.170 | **−0.137** | −0.379 | −0.232 |
| momentum | −0.158 | −0.144 | **−0.131** | −0.418 | −0.296 |
| breakout | −0.544 | −0.538 | −0.539 | −0.666 | −0.575 |
| rsi_mr | +0.074 | +0.249 | **+0.384** | −0.173 | +0.148 |
| dip | −0.300 | −0.249 | **−0.175** | −0.435 | −0.310 |

Exception: under the *ohlc* order, the breakout rule and the momentum split were slightly better
at 3 %. Breakout lost money with every exit tested.

**Capital efficiency** (profit per position per session held, in basis points; mean of rules,
pessimistic):

| | flat 3 % | flat 4 % | flat 6 % | split 3 % | split 4 % | split 6 % |
|---|---|---|---|---|---|---|
| bps / day | −16.6 | −11.7 | −6.5 | −3.6 | −2.6 | −2.0 |
| sessions held | 2.6 | 3.2 | 4.4 | 6.1 | 6.6 | 7.2 |

With the 3 % stop, the T1 hit rate was 48 % for a 3 % target, 42 % for 4 % and 33 % for 6 %.
The stop caught the rest.

### Year by year (pessimistic, mean of the 5 rules)

| Year | flat 3 | flat 4 | flat 6 | split 3 | split 4 | split 6 |
|---|---|---|---|---|---|---|
| 2015 | −0.27 | −0.25 | **−0.19** | −0.39 | −0.44 | −0.49 |
| 2016 | −0.66 | −0.68 | **−0.57** | −0.65 | −0.69 | −0.52 |
| 2017 | −0.09 | +0.07 | +0.36 | +0.54 | +0.76 | **+0.99** |
| 2018 | −0.71 | −0.69 | **−0.68** | −1.07 | −1.05 | −1.05 |
| 2019 | −0.57 | −0.51 | **−0.49** | −0.58 | −0.55 | −0.51 |
| 2020 | −0.30 | −0.09 | +0.03 | +0.12 | +0.29 | **+0.42** |
| 2021 | −0.37 | −0.31 | −0.14 | +0.15 | +0.28 | **+0.34** |
| 2022 | −0.54 | −0.48 | **−0.44** | −0.54 | −0.65 | −0.61 |
| 2023 | −0.11 | +0.04 | +0.36 | +0.87 | +0.99 | **+1.16** |
| 2024 | −0.58 | −0.54 | **−0.49** | −0.69 | −0.69 | −0.64 |
| 2025 | **−0.41** | −0.42 | −0.44 | −0.62 | −0.62 | −0.71 |
| 2026 | −0.55 | **−0.52** | −0.64 | −0.53 | −0.50 | −0.63 |

Of 60 rule-years, flat 6 % beat flat 3 % in 44 (pessimistic) or 36 (ohlc), and split 6 % beat
split 3 % in 39 or 33. The split wins big in good years and gives more back in bad years,
because the runner's 4 × ATR trail is wide.

### The window ATIP's database covers (27 Jan 2025 – Oct 2026)

| | flat 3 | flat 4 | flat 6 | split 3 | split 4 | split 6 | no target |
|---|---|---|---|---|---|---|---|
| pessimistic | −0.44 | **−0.43** | −0.44 | −0.49 | −0.48 | −0.54 | −0.74 |
| ohlc | **−0.33** | −0.37 | −0.41 | −0.44 | −0.44 | −0.50 | −0.74 |

Everything lost money here; the market went nowhere (Nifty 50 −1 % over the window). ATIP's
earlier run on its own database (notes in `strategy/aggressive.py`) also found a flat 3 % target
negative after costs. No target at all, the best exit over the twelve years, was the worst here.

### The stop matters more than the target

Split at 6 % / checkpoint 12 %, mean of the 5 rules. "Per unit of risk" divides the result by the
stop distance, so different stop widths can be compared fairly.

| Initial stop | avg % (pess / ohlc) | per unit of risk (ohlc) |
|---|---|---|
| 3 % (today) | −0.120 / −0.081 | −0.027 |
| 5 % | −0.005 / +0.044 | +0.009 |
| 8 % | +0.075 / +0.131 | +0.016 |
| 2 × ATR (2–10 %, avg ≈ 7.5 %) | +0.091 / +0.139 | +0.019 |
| 3 × ATR (2–10 %, avg ≈ 9.2 %) | +0.150 / +0.208 | +0.023 |

Every exit improved as the stop widened from 3 %. An ATR stop did better than a fixed stop of
the same average width.

### As a 10-position book (pessimistic, equal weight, 2015–2026)

| Exit | random: CAGR / max DD | momentum: CAGR / max DD |
|---|---|---|
| flat 3 %, 3 % stop | −28.3 % / −98 % | −35.2 % / −99 % |
| flat 6 %, 3 % stop | −10.2 % / −73 % | −19.7 % / −93 % |
| split 3/6 (ATIP today), 3 % stop | −5.4 % / −53 % | −0.2 % / −39 % |
| split 6/12, 3 % stop | −2.5 % / −44 % | −0.5 % / −47 % |
| split 6/12, 3 × ATR stop | +1.6 % / −45 % | +5.0 % / −47 % |

For comparison, over the same period the Nifty 50 rose 8.9 % a year and the Nifty 500 10.2 %.
None of the target-based exits beat holding the index. The only configuration that did was
momentum entries with no target and a 3 × ATR stop (+11.9 % a year, max drawdown −55 %). That is
also the setup that suffered most in 2025–26, so it is not recommended as the default.

---

## Caveats

- **Survivorship:** delisted stocks are missing from the data. This flatters every long exit,
  the long holds most.
- The entry rules approximate ATIP's rule library. They are not ATIP's VPI/ZPI/CRI scores, which
  have too little history.
- Costs are a flat 0.25 % round trip. Fills come from daily bars, with no market impact.
- 2015–2026 was mostly a rising market. In 2025–26, a flat or falling one, the ranking turned
  around.

## Found along the way

1. `strategy/live.py`: when neither `--stop-pct` nor the ATR stop (`stop_atr_mult`) applies,
   the live initial stop falls back to `target_1_pct`. Changing the target therefore changes the
   stop.
2. `strategy/backtest_history.py`: `realised_pnl` applies `cost_pct` to the turnover of both
   legs. The documented 0.25 % round trip is therefore really about 0.5 %.
3. The breakout rule (`library/12_volume_breakout.json`) lost 0.3–0.7 % per trade with every exit
   and stop tested.

## Run it on ATIP's own database (Mac)

```bash
python -m research.exit_target_study --source atip --start 2025-01-27 --json atip_data/exit_study.json
python -m research.exit_target_study --source atip --start 2025-01-27 --stop-atr 2
# the long history, from a checkout of eod2_data:
python -m research.exit_target_study --source eod2 --path ../eod2_data --start 2015-01-01
```
