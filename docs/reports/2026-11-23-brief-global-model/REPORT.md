# Morning-brief fix (BR-01) and global-cue model backtest, 23 Nov 2026

## Part 1: the Monday brief now shows the overnight move (BR-01)

**Bug.** `alerts/telegram.py` `send_morning_digest` read global_markets, index_levels and market_health "as of the
last scored day". On a Monday that is Friday. On 23 Nov the brief therefore printed S&P −2.02 % (Thursday's US move).
Friday night's S&P −3.70 % and Nasdaq −5.28 % had been stored at 06:00 IST but were not shown.

**Fix** (branch `claude/brief-global-model-2026-11-23`, worktree `.claude/worktrees/brief-global-2026-11-23`, off
GitHub master 819e825):
- The scores stay "last scored day". The market lines now read the newest row up to today.
- Every older row says which close it is from: "Market Health 53 (NEUTRAL) (20 Nov close)" and
  "Last session (20 Nov close): Nifty +1.12 % · VIX 17.3". The line says "Open:" only for today's row.
- The Global line shows the score, its label, S&P, Nasdaq and Dow, and the US close date.
- New warning at the top when the S&P or Nasdaq moved ≥ 1.5 % after the scores were made (up or down).
  `OVERNIGHT_MOVE_PCT = 1.5`.
- `send_morning_digest(now=...)` takes a clock, for tests.

Monday 23 Nov with the fix (rebuilt from that morning's numbers):
```
⚠️ Overnight: the US fell after these 2026-11-20 scores — S&P -3.70%, Nasdaq -5.28% (US close 20 Nov).
   Market Health and the Stance below are from the last close and do not include it.
Market Health: 53 (NEUTRAL) (20 Nov close)
Stance: Reduced sizing — only ACS > 70 setups
Last session (20 Nov close): Nifty +1.12% · VIX 17.3
Global: score 35 (BEARISH), S&P -3.70%, Nasdaq -5.28%, Dow -1.93% (US close 20 Nov)
```
Before the fix: `Open: Nifty +1.12% · VIX 17.3 · GIFT +0.00%` and `Global: score 3, S&P -2.02%`.

**Tests.**
- `tests/test_brief_overnight_2026_11_23.py`: 8 new, all pass.
- `tests/test_morning_brief.py`: 5, still pass.
- Every test file that calls the brief (6 files, 120 tests, including test_asia_markets, test_news_impact,
  test_auto_recover and test_job_monitoring) passes.

**Status.** Committed and pushed to GitHub on 23 Nov with the owner's "commit and push". Not on the VPS: deploying it
needs the owner's yes in the VPS session. The next brief is Wed 25 Nov at 08:30 (24 Nov is a holiday).

Seen, not changed: `compute_global_score` (data/markets.py) counts gold up as bullish, and its US and Asia parts
bottom out at 0 for any average fall of 1.5 % or more. So Monday's score was 35, mostly from gold +2.6 %, even though
the US fell more than on Friday (score 3). The new US line shows the real moves.

## Part 2: does last night's world predict Nifty? (global-cue model backtest)

**Model.** `research/market_pulse.global_cue_model`, the live code, unchanged. It is a ridge regression of Nifty's
daily return on the previous move of 9 factors: S&P, Nasdaq, Nikkei, Hang Seng, Brent, dollar index, USD/INR, gold and
US 10y. It is refitted every day on the 250 sessions before it, so nothing is seen in advance.
On the VPS it has never produced a number: it needs 80+ sessions and the VPS has 31–46 days of global history.

**Data.** 10 years from yfinance in a scratch database (`.ux_build/brief-global-2026-11-23/`). The live database was
not touched.
- **2,213 sessions tested, 28 Nov 2017 – 23 Nov 2026.**
- Script: `evidence/backtest_global_cue.py`. Output: `evidence/results.json`.

| Question | Result |
|---|---|
| Day's direction (close to close) | right **58.0 %** of days. "Always up" is right 53.8 % |
| …when the model calls a move of 0.4 %+ | right 65.1 % |
| Size of the move | error 1.070 % vs 1.067 % for "predict 0". **No better** |
| Link to the **opening gap** | correlation **0.41**: strong |
| Link to **open → close** (when ATIP buys) | correlation **−0.08**: none (slightly opposite) |
| Stability | hit rate on big moves by year: 49 % (2025) to 66 % (2023) |

**Mornings the model or the US said "down":**
| Days | n | Gap | Close-to-close | Open → close | Open → 3rd close |
|---|---|---|---|---|---|
| All days | 2,213 | +0.10 % | +0.05 % | −0.06 % (down 52 %) | +0.04 % |
| Model ≤ −0.75 % | 75 | **−0.86 %** | −0.73 % (down 67 %) | **+0.13 %** (down 47 %) | +0.39 % (down 40 %) |
| S&P prior night ≤ −2 % | 87 | **−0.93 %** | −0.74 % (down 69 %) | **+0.20 %** (down 40 %) | +0.25 % (down 38 %) |
| S&P ≤ −1.5 % but Nifty **opened up** (like 23 Nov) | 35 | +0.61 % | +0.64 % (down 37 %) | +0.04 % (down 49 %) | +0.44 % |

**The 23 Nov call (out of sample):** **−0.004 % = FLAT**, against an actual −1.76 %.
- S&P −3.77 % pulled the forecast down by 1.43 points.
- Nasdaq (+0.80), the 14 bp fall in the US 10-year yield (+0.31) and Nikkei (+0.26) pushed it back up. The ridge
  splits S&P and Nasdaq, which move together, into opposite signs.
- So with full history, this model would still not have warned on 23 Nov.

**Verdict.**
- Global cues forecast the **opening gap**, and GIFT Nifty already measures that directly.
- After the open, which is where auto-pick buys, they have **no edge**. On "risk-off" mornings Nifty on average
  *recovered* from the open (+0.13 % to +0.20 % to the close, +0.25 % to +0.39 % over 3 days).
- A "hold new buys on global risk-off mornings" rule would therefore have skipped small gains on average. **It fails
  the replay test, so it should not be wired into auto-pick.**
- 23 Nov (opened up, closed −1.75 %) is the minority case of a pattern that is a coin flip (49 %).
- Backfilling the VPS just to show this number in the brief is optional and of low value.

**What does help, from ATIP's own studies:** reacting after the open. The guard "no buy while Nifty is below its
open or 0.5 %+ down" passed the 42-session RCA study (+₹658 on 23 Nov). It is built and switched OFF.
