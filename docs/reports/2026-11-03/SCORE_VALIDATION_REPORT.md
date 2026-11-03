# ATIP score validation — point-in-time backtest (3 Nov 2026)

All times are IST. Code: `scores/validation.py`. To reproduce:
`.venv/bin/python -m scores.validation --db <copy of atip.db> --out docs/reports/2026-11-03 --tag after`.
It was run on a copy of the live `atip.db` taken at 09:26 PM IST. Both the "before" and the "after" runs were between
09:30 and 09:55 PM IST on 3 Nov 2026.

## Method

* **Universe.** 489 EQ symbols with at least 300 sessions in `prices_daily` (the tracked Nifty 500 set), 3 Mar 2025 →
  3 Nov 2026. *Survivorship-biased*: the set is today's constituents.
* **Two views.** (1) **Point-in-time (PIT)** recompute: indicators are computed only from bars up to each session (Wilder
  RSI/ATR/ADX 14, MACD 12/26/9, EMA 9/21/50, SMA 200, 20-day volume ratio). They are fed through the engine's own
  `compute_*` functions. News is neutral (50), and fundamentals, ownership and historical accuracy are absent, because
  none of them is stored per session before 27 Jul. MSI is left out of ATIP: it is one value per day. (2) **Stored**
  `ai_scores` as deployed: 44 sessions, 27 Jul → 3 Nov 2026. The stored rows are the same before and after, because
  they were written by the old code. They are the as-deployed baseline.
* **No look-ahead.** The signal is formed at the close of *t*. The return is open(*t*+1) → close(*t*+*h*) for
  *h* = 1, 5, 10, 20. Any window that contains a one-day move above 35 % (an unadjusted corporate action) is dropped.
* **Cross-sectional per date.** The Spearman rank IC is computed per session and then averaged. Decile and quintile
  spreads are per session, then averaged. Nothing is normalised across history.
* **Walk-forward.** The first 70 % of sessions are in-sample (3 Jun 2025 → 2 Jun 2026) and the last 30 % are
  out-of-sample (3 Jun → 3 Nov 2026). Changes were chosen on the in-sample sign and **kept only if the out-of-sample IC
  also improved**.
* **t-stat.** Daily ICs at horizon *h* overlap, so the t-stat uses n/h effective observations.
* **Hit rate.** BUY = the score's top decile (bottom decile for CRI↓) that session; SELL = the opposite decile. BUY is
  net of a 0.25 % round trip. "Hit" = a BUY made money, or a SELL fell.

## Actual BUY signals (stored, 28 Jul – 29 Oct 2026; 35 signals on 25 stocks)

| Horizon | Outcomes | Hit % (net) | Avg win % | Avg loss % | Mean % |
|---|---|---|---|---|---|
| 1 day | 32 | 31.2 | +3.68 | −2.40 | −0.50 |
| 5 days | 28 | 35.7 | +7.22 | −6.00 | −1.28 |
| 10 days | 21 | 47.6 | +6.67 | −7.92 | −0.97 |
| 20 days | 19 | 36.8 | +12.54 | −7.61 | −0.19 |

There were no SELL signals stored, ever. The BUY branch is all but unreachable (ZPI rarely reaches its gate), and the
rescored 3 Nov again has **0 BUY** (best ATIP 64.8 against a gate of 65).

## Decisions taken on the evidence

| Change | In-sample effect | Out-of-sample effect | Kept? |
|---|---|---|---|
| MRI: ADX straight | 5d −0.017 → −0.011 | −0.031 → −0.014 | as part of the next row |
| MRI: ADX straight + MACD read as reversal | 5d **+0.014**, 20d +0.021 | 5d **+0.018**, 20d +0.032 | **yes** |
| MRI: ADX straight, MACD weight 0 | +0.002 | −0.000 | no (worse than the reversal reading) |
| ZPI: ATR inverted alone | 5d +0.020 | −0.003 | no on its own |
| ZPI: ATR inverted + ADX straight | 5d **+0.029 (t 2.5)** | 5d **+0.020**, 20d +0.027 | **yes** |
| ZPI: RSI or Volume buckets inverted | worse | worse | no |
| ATIP + SAFETY (100 − CRI) at 0.05 / 0.1 / **0.2** / 0.3 | 5d +0.017 / +0.020 / **+0.028** / +0.036 | 5d +0.010 / +0.013 / **+0.017** / +0.022 | **0.2 kept**. 0.3 was better in both periods and was not taken, so as not to tune to one window |
| VPI: flip V (volatility) | 5d sign consistent, 20d not | — | no. Proposal only, because it would redefine "Volatility-Potential" |

## Verdict summary

| Score | Verdict before | Verdict after |
|---|---|---|
| ATIP | BROKEN | BROKEN (IC now positive at every horizon, OOS t < 1) |
| VPI | BROKEN | BROKEN |
| MRI | **INVERTED** | **WEAK** |
| RRI | BROKEN | BROKEN |
| ZPI | BROKEN | **WEAK** |
| CRI↓ | WORKING | WORKING |
| ACS | BROKEN | WEAK |

**What the numbers do not say.** No score's top decile made money after costs out of sample. The window (Jun–Nov 2026)
was a falling market for this universe: every SELL decile "hit" 56–66 %. The ranks are weakly informative, and the
long-only fixed +3 % / −2 % session plan has a negative expectancy on this evidence. The rank-filled sessions (Task 1)
will trade more often, so they will show that expectancy faster. They are PAPER-only.

## Point-in-time recompute
point-in-time recompute, 489 symbols; 353 sessions; in-sample 2025-06-03 → 2026-06-02, out-of-sample 2026-06-03 → 2026-11-03.

### Rank IC (Spearman), out of sample — mean (t, effective-n)

| Score | Expected sign | Verdict before | Verdict after | 1d before | 1d after | 5d before | 5d after | 10d before | 10d after | 20d before | 20d after |
|---|---|---|---|---|---|---|---|---|---|---|---|
| ATIP | + | BROKEN | BROKEN | -0.0150 (-1.37) | +0.0088 (+0.76) | -0.0090 (-0.33) | +0.0174 (+0.58) | -0.0023 (-0.06) | +0.0185 (+0.46) | +0.0077 (+0.13) | +0.0306 (+0.51) |
| VPI | + | BROKEN | BROKEN | -0.0042 (-0.51) | -0.0042 (-0.51) | +0.0063 (+0.35) | +0.0063 (+0.35) | +0.0153 (+0.57) | +0.0153 (+0.57) | +0.0319 (+0.75) | +0.0319 (+0.75) |
| MRI | + | INVERTED | WEAK | -0.0138 (-1.74) | +0.0059 (+0.94) | -0.0311 (-2.08) | +0.0175 (+1.27) | -0.0263 (-1.08) | +0.0211 (+1.15) | -0.0370 (-1.16) | +0.0324 (+1.12) |
| RRI | + | BROKEN | BROKEN | -0.0165 (-1.43) | -0.0165 (-1.43) | -0.0030 (-0.10) | -0.0030 (-0.10) | +0.0052 (+0.12) | +0.0052 (+0.12) | +0.0149 (+0.24) | +0.0149 (+0.24) |
| ZPI | + | BROKEN | WEAK | -0.0048 (-0.51) | +0.0169 (+1.61) | -0.0116 (-0.47) | +0.0203 (+0.71) | -0.0225 (-0.66) | +0.0131 (+0.36) | -0.0188 (-0.37) | +0.0275 (+0.48) |
| CRI↓ | − | WORKING | WORKING | -0.0453 (-4.41) | -0.0453 (-4.41) | -0.0312 (-1.23) | -0.0312 (-1.23) | -0.0182 (-0.63) | -0.0182 (-0.63) | -0.0138 (-0.39) | -0.0137 (-0.39) |
| ACS | + | BROKEN | WEAK | +0.0057 (+0.55) | +0.0125 (+1.16) | +0.0061 (+0.25) | +0.0204 (+0.80) | +0.0081 (+0.25) | +0.0263 (+0.76) | +0.0139 (+0.26) | +0.0394 (+0.74) |

### Rank IC, in sample (after) — mean (t)

| Score | 1d | 5d | 10d | 20d |
|---|---|---|---|---|
| ATIP | +0.0188 (+2.58) | +0.0280 (+1.96) | +0.0311 (+1.43) | +0.0372 (+1.16) |
| VPI | -0.0099 (-1.56) | -0.0010 (-0.09) | +0.0028 (+0.17) | +0.0053 (+0.24) |
| MRI | +0.0046 (+1.08) | +0.0140 (+1.51) | +0.0196 (+1.49) | +0.0205 (+1.07) |
| RRI | -0.0007 (-0.10) | +0.0085 (+0.63) | +0.0166 (+0.88) | +0.0261 (+1.04) |
| ZPI | +0.0206 (+3.98) | +0.0301 (+2.58) | +0.0238 (+1.35) | +0.0205 (+0.80) |
| CRI | -0.0569 (-7.89) | -0.0498 (-2.85) | -0.0441 (-1.75) | -0.0484 (-1.44) |
| ACS | +0.0109 (+1.62) | +0.0223 (+1.70) | +0.0287 (+1.49) | +0.0306 (+1.11) |

### Decile / quintile spread, out of sample (top minus bottom bucket, % forward return; after)

| Score | 1d decile | 1d quintile | 5d decile | 5d quintile | 10d decile | 10d quintile | 20d decile | 20d quintile | 20d monotonic |
|---|---|---|---|---|---|---|---|---|---|
| ATIP | +0.025 | +0.025 | +0.155 | +0.049 | +0.232 | +0.061 | +0.584 | +0.371 | +0.18 |
| VPI | -0.076 | -0.014 | -0.100 | +0.018 | -0.003 | +0.202 | +0.404 | +0.585 | +0.83 |
| MRI | +0.009 | +0.031 | +0.146 | +0.166 | +0.303 | +0.270 | +0.959 | +0.694 | +0.92 |
| RRI | -0.147 | -0.057 | -0.302 | -0.088 | -0.277 | -0.045 | -0.138 | +0.022 | +0.27 |
| ZPI | +0.090 | +0.053 | +0.035 | -0.018 | -0.120 | -0.082 | +0.329 | +0.126 | +0.37 |
| CRI | -0.166 | -0.089 | -0.112 | +0.038 | +0.242 | +0.441 | +0.587 | +0.830 | +0.33 |
| ACS | +0.035 | +0.050 | +0.092 | +0.168 | +0.131 | +0.343 | +0.651 | +0.657 | +0.49 |

### BUY / SELL hit rates, out of sample (after). BUY = top decile (bottom for CRI↓), net of 0.25 % round trip

| Score | 5d BUY n | hit % | avg win % | avg loss % | expectancy % | 5d SELL hit % | 20d BUY hit % | 20d expectancy % | 20d SELL hit % |
|---|---|---|---|---|---|---|---|---|---|
| ATIP | 4848 | 43.8 | 3.77 | -3.73 | -0.447 | 59.9 | 42.5 | -0.608 | 64.3 |
| VPI | 4848 | 41.8 | 4.17 | -4.17 | -0.685 | 58.1 | 39.7 | -1.322 | 64.5 |
| MRI | 4848 | 43.1 | 3.78 | -3.53 | -0.383 | 56.5 | 42.6 | -0.474 | 60.4 |
| RRI | 4848 | 42.1 | 3.92 | -4.18 | -0.774 | 57.0 | 40.1 | -1.475 | 61.8 |
| ZPI | 4848 | 42.8 | 3.31 | -3.14 | -0.381 | 57.5 | 39.9 | -0.872 | 62.4 |
| CRI | 4848 | 41.3 | 2.49 | -2.61 | -0.499 | 57.5 | 37.8 | -1.531 | 60.1 |
| ACS | 4848 | 42.9 | 3.93 | -3.74 | -0.454 | 60.1 | 41.1 | -0.988 | 66.0 |

### Stability (out of sample, after)

| Score | Day-to-day rank autocorrelation | Top-decile turnover / day |
|---|---|---|
| ATIP | 0.947 | 23.0 % |
| VPI | 0.873 | 31.6 % |
| MRI | 0.677 | 47.9 % |
| RRI | 0.925 | 41.0 % |
| ZPI | 0.855 | 37.1 % |
| CRI | 0.757 | 19.3 % |
| ACS | 0.923 | 27.5 % |

### Correlation between the scores (mean daily Spearman, out of sample, after). |ρ| > 0.8 = redundant

| | ATIP | VPI | MRI | RRI | ZPI | CRI | ACS |
|---|---|---|---|---|---|---|---|
| ATIP | 1.00 | 0.65 | 0.36 | **0.85** | 0.77 | -0.53 | **0.87** |
| VPI | 0.65 | 1.00 | 0.30 | 0.46 | 0.27 | 0.07 | 0.72 |
| MRI | 0.36 | 0.30 | 1.00 | 0.23 | 0.14 | 0.09 | 0.26 |
| RRI | **0.85** | 0.46 | 0.23 | 1.00 | 0.55 | -0.35 | 0.70 |
| ZPI | 0.77 | 0.27 | 0.14 | 0.55 | 1.00 | -0.68 | 0.63 |
| CRI | -0.53 | 0.07 | 0.09 | -0.35 | -0.68 | 1.00 | -0.39 |
| ACS | **0.87** | 0.72 | 0.26 | 0.70 | 0.63 | -0.39 | 1.00 |

Redundant pairs (|ρ| > 0.8): ATIP–RRI 0.85, ACS–ATIP 0.87.

## Stored scores (as deployed)
stored ai_scores (as deployed); 44 sessions; in-sample 2026-07-27 → 2026-10-13, out-of-sample 2026-10-14 → 2026-11-03.

### Rank IC (Spearman), out of sample — mean (t, effective-n)

| Score | Expected sign | Verdict before | Verdict after | 1d before | 1d after | 5d before | 5d after | 10d before | 10d after | 20d before | 20d after |
|---|---|---|---|---|---|---|---|---|---|---|---|
| ATIP | + | WEAK | WEAK | -0.0102 (-0.24) | -0.0102 (-0.24) | +0.0463 (+0.51) | +0.0463 (+0.51) | +0.0703 (+1.04) | +0.0703 (+1.04) | — (—) | — (—) |
| VPI | + | WEAK | WEAK | -0.0009 (-0.04) | -0.0009 (-0.04) | +0.0494 (+0.99) | +0.0494 (+0.99) | +0.1093 (+1.59) | +0.1093 (+1.59) | — (—) | — (—) |
| MRI | + | BROKEN | BROKEN | -0.0026 (-0.06) | -0.0026 (-0.06) | -0.0444 (-0.80) | -0.0444 (-0.80) | -0.0276 (-1.17) | -0.0276 (-1.17) | — (—) | — (—) |
| RRI | + | BROKEN | BROKEN | -0.0159 (-0.36) | -0.0159 (-0.36) | +0.0422 (+0.40) | +0.0422 (+0.40) | +0.0430 (+0.52) | +0.0430 (+0.52) | — (—) | — (—) |
| ZPI | + | BROKEN | BROKEN | -0.0143 (-0.43) | -0.0143 (-0.43) | +0.0188 (+0.18) | +0.0188 (+0.18) | -0.0189 (-0.32) | -0.0189 (-0.32) | — (—) | — (—) |
| CRI↓ | − | WORKING | WORKING | -0.0834 (-2.12) | -0.0834 (-2.12) | -0.1570 (-3.72) | -0.1570 (-3.72) | -0.1947 (-1.91) | -0.1947 (-1.91) | — (—) | — (—) |
| ACS | + | WORKING | WORKING | +0.0590 (+1.29) | +0.0590 (+1.29) | +0.1495 (+2.70) | +0.1495 (+2.70) | +0.2095 (+3.73) | +0.2095 (+3.73) | — (—) | — (—) |

### Rank IC, in sample (after) — mean (t)

| Score | 1d | 5d | 10d | 20d |
|---|---|---|---|---|
| ATIP | -0.0059 (-0.29) | -0.0327 (-0.66) | -0.0077 (-0.13) | +0.0297 (+0.31) |
| VPI | +0.0162 (+0.95) | +0.0166 (+0.72) | +0.0422 (+1.96) | +0.0867 (+1.68) |
| MRI | -0.0280 (-1.92) | -0.0646 (-2.88) | -0.0639 (-1.71) | -0.0495 (-0.76) |
| RRI | -0.0152 (-0.72) | -0.0377 (-0.62) | -0.0166 (-0.23) | +0.0129 (+0.12) |
| ZPI | +0.0100 (+0.49) | -0.0132 (-0.25) | -0.0068 (-0.10) | -0.0020 (-0.03) |
| CRI | -0.0490 (-3.22) | -0.0180 (-0.44) | -0.0340 (-0.98) | -0.0686 (-1.40) |
| ACS | +0.0375 (+2.23) | +0.0087 (+0.17) | +0.0445 (+0.82) | +0.0895 (+0.98) |

### Decile / quintile spread, out of sample (top minus bottom bucket, % forward return; after)

| Score | 1d decile | 1d quintile | 5d decile | 5d quintile | 10d decile | 10d quintile | 20d decile | 20d quintile | 20d monotonic |
|---|---|---|---|---|---|---|---|---|---|
| ATIP | -0.173 | -0.103 | -0.505 | +0.556 | +0.350 | +1.454 | — | — | — |
| VPI | -0.245 | -0.113 | -0.135 | +0.100 | +0.865 | +1.281 | — | — | — |
| MRI | -0.116 | -0.087 | -1.280 | -0.597 | -1.509 | -0.640 | — | — | — |
| RRI | -0.235 | -0.148 | +0.226 | +0.484 | +0.740 | +1.296 | — | — | — |
| ZPI | -0.183 | -0.029 | -0.173 | +0.459 | +0.026 | -0.182 | — | — | — |
| CRI | -0.535 | -0.359 | -2.962 | -1.796 | -4.312 | -3.013 | — | — | — |
| ACS | +0.289 | +0.198 | +1.752 | +1.440 | +2.943 | +3.121 | — | — | — |

### BUY / SELL hit rates, out of sample (after). BUY = top decile (bottom for CRI↓), net of 0.25 % round trip

| Score | 5d BUY n | hit % | avg win % | avg loss % | expectancy % | 5d SELL hit % | 20d BUY hit % | 20d expectancy % | 20d SELL hit % |
|---|---|---|---|---|---|---|---|---|---|
| ATIP | 405 | 37.8 | 4.76 | -6.4 | -2.181 | 60.0 | — | — | — |
| VPI | 405 | 37.5 | 3.71 | -5.81 | -2.234 | 62.7 | — | — | — |
| MRI | 405 | 40.2 | 3.71 | -5.6 | -1.853 | 51.9 | — | — | — |
| RRI | 405 | 41.2 | 5.0 | -6.17 | -1.567 | 59.8 | — | — | — |
| ZPI | 405 | 43.2 | 5.69 | -6.42 | -1.188 | 55.8 | — | — | — |
| CRI | 405 | 49.6 | 4.27 | -4.18 | 0.013 | 67.7 | — | — | — |
| ACS | 405 | 45.7 | 4.4 | -5.3 | -0.868 | 64.4 | — | — | — |

### Stability (out of sample, after)

| Score | Day-to-day rank autocorrelation | Top-decile turnover / day |
|---|---|---|
| ATIP | 0.849 | 42.9 % |
| VPI | 0.865 | 40.2 % |
| MRI | 0.795 | 48.0 % |
| RRI | 0.901 | 35.3 % |
| ZPI | 0.84 | 38.6 % |
| CRI | 0.737 | 23.1 % |
| ACS | 0.928 | 28.9 % |

### Correlation between the scores (mean daily Spearman, out of sample, after). |ρ| > 0.8 = redundant

| | ATIP | VPI | MRI | RRI | ZPI | CRI | ACS |
|---|---|---|---|---|---|---|---|
| ATIP | 1.00 | 0.58 | 0.53 | **0.90** | 0.49 | -0.26 | 0.67 |
| VPI | 0.58 | 1.00 | 0.13 | 0.31 | -0.14 | 0.15 | 0.47 |
| MRI | 0.53 | 0.13 | 1.00 | 0.42 | 0.11 | -0.13 | 0.41 |
| RRI | **0.90** | 0.31 | 0.42 | 1.00 | 0.57 | -0.29 | 0.55 |
| ZPI | 0.49 | -0.14 | 0.11 | 0.57 | 1.00 | -0.46 | 0.32 |
| CRI | -0.26 | 0.15 | -0.13 | -0.29 | -0.46 | 1.00 | -0.43 |
| ACS | 0.67 | 0.47 | 0.41 | 0.55 | 0.32 | -0.43 | 1.00 |

Redundant pairs (|ρ| > 0.8): ATIP–RRI 0.90.

⚠️ For informational use only. Not financial advice. Past performance is not indicative of future results.
