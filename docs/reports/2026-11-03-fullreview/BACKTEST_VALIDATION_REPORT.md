# Backtest / validation report: do the seven scores work? (3 Nov 2026, prompt 10:54 PM IST)

**Method.** `scores/validation.py` recomputes VPI, MRI, RRI, CRI, ZPI, ATIP and ACS point-in-time through the engine's
own `compute_*` functions, using indicators built only from bars up to each session. It runs on a copy of tonight's
live database (`atip.db` at 23:13 IST). Universe: 489 symbols with ≥ 300 sessions, 353 scored sessions. Survivorship:
before 28 Jul 2026 `prices_daily` holds only today's tracked list, so the universe is survivorship-biased (reported,
not corrected).

* **No look-ahead.** A score from the close of *t* is acted on at the **open of t+1**. The forward return is open(t+1) →
  close(t+h) for h = 1, 5, 10, 20. A window holding a one-day move over 35 % (an unadjusted corporate action) is dropped.
* **Cross-sectional per date.** Spearman rank IC and decile / quintile spreads are computed per session, then averaged.
  The t-stat uses n/h independent observations (overlapping windows).
* **Walk-forward.** In-sample = the first 70 % of sessions (3 Jun 2025 → 2 Jun 2026); out-of-sample = the last 30 %
  (3 Jun → 3 Nov 2026). Nothing is fitted in-sample except the variant test in §3, and that only under a rule fixed
  before the results were seen.
* **Hit rate.** BUY = the session's top decile (the bottom decile for CRI↓), net of a 0.25 % round trip; SELL = the
  bottom decile, a hit when the forward return is negative.
* **Before / after.** Before = the 2 Nov formulas (`validation_before.json`, committed with the 9:23 PM audit). After =
  master today, re-run tonight (`validation_recheck.json`). It is identical to the committed `validation_after.json`:
  0 differences in any out-of-sample IC. This patch changes no PIT score formula; its engine changes are NULL handling
  and the default day, which the PIT recompute does not exercise.

Reproduce: `python -m scores.validation --db <copy of atip.db> --out <dir> --tag recheck`.

## 1. Verdicts (out of sample)

| Score | Before | After | Evidence |
|---|---|---|---|
| ATIP | BROKEN | BROKEN, improving | OOS 5d IC −0.009 → **+0.017**, 20d +0.008 → +0.031; in-sample 1d t +2.6 |
| VPI | BROKEN | BROKEN | its V (ATR) component is inverted vs returns; see §2–3 |
| MRI | **INVERTED** | WEAK | 5d −0.031 (t −2.1) → +0.018 (t +1.3) |
| RRI | BROKEN | BROKEN | no component has a consistent sign (§2) |
| ZPI | BROKEN | WEAK | 1d +0.017 (t +1.6); in-sample 1d t +4.0 |
| CRI↓ | WORKING | WORKING | 1d −0.045 (t −4.4); its bottom decile (the BUY side) has the best 5d hit rate in the stored data |
| ACS | BROKEN | WEAK | 5d +0.006 → +0.020 |


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
| ATIP | 0.843 | 43.2 % |
| VPI | 0.865 | 40.2 % |
| MRI | 0.705 | 51.0 % |
| RRI | 0.901 | 35.3 % |
| ZPI | 0.811 | 41.3 % |
| CRI | 0.737 | 23.1 % |
| ACS | 0.925 | 31.2 % |

### Correlation between the scores (mean daily Spearman, out of sample, after). |ρ| > 0.8 = redundant

| | ATIP | VPI | MRI | RRI | ZPI | CRI | ACS |
|---|---|---|---|---|---|---|---|
| ATIP | 1.00 | 0.58 | 0.50 | **0.89** | 0.50 | -0.27 | 0.67 |
| VPI | 0.58 | 1.00 | 0.14 | 0.31 | -0.12 | 0.15 | 0.47 |
| MRI | 0.50 | 0.14 | 1.00 | 0.38 | 0.10 | -0.09 | 0.38 |
| RRI | **0.89** | 0.31 | 0.38 | 1.00 | 0.56 | -0.29 | 0.55 |
| ZPI | 0.50 | -0.12 | 0.10 | 0.56 | 1.00 | -0.47 | 0.33 |
| CRI | -0.27 | 0.15 | -0.09 | -0.29 | -0.47 | 1.00 | -0.43 |
| ACS | 0.67 | 0.47 | 0.38 | 0.55 | 0.33 | -0.43 | 1.00 |

Redundant pairs (|ρ| > 0.8): ATIP–RRI 0.89.

## 2. Component-level rank IC (new tonight): VPI and RRI, the two BROKEN scores

Each component's own rank IC, in-sample (IS) and out-of-sample (OOS); t in brackets.

| Component | 1d IS | 1d OOS | 5d IS | 5d OOS | 20d IS | 20d OOS |
|---|---|---|---|---|---|---|
| VPI.V(atr%x12) | -0.057 (-7.0) | -0.058 (-4.6) | -0.037 (-1.8) | -0.030 (-1.1) | -0.028 (-0.6) | +0.001 (+0.0) |
| VPI.TS(adx) | -0.006 (-1.1) | +0.005 (+0.7) | +0.010 (+0.9) | +0.029 (+1.8) | +0.010 (+0.5) | +0.054 (+1.7) |
| VPI.RS(ret20-nifty) | +0.002 (+0.2) | -0.013 (-1.2) | -0.003 (-0.2) | -0.022 (-1.0) | +0.010 (+0.3) | -0.022 (-0.8) |
| VPI.LQ(turnover) | +0.018 (+2.6) | +0.025 (+2.6) | +0.019 (+1.4) | +0.018 (+0.9) | +0.015 (+0.5) | +0.018 (+0.4) |
| VPI.VOL(vr/3) | -0.002 (-0.5) | -0.010 (-1.8) | -0.001 (-0.1) | -0.001 (-0.1) | +0.013 (+0.7) | +0.015 (+0.5) |
| VPI.MR(rsi bucket) | -0.001 (-0.1) | +0.015 (+1.6) | -0.007 (-0.5) | +0.008 (+0.4) | -0.023 (-0.9) | +0.008 (+0.3) |
| RRI.Recovery(off 52w low) | -0.004 (-0.5) | -0.014 (-1.1) | +0.006 (+0.5) | +0.002 (+0.1) | +0.021 (+0.9) | +0.022 (+0.3) |
| RRI.RSIRecovery | +0.003 (+0.4) | -0.020 (-1.9) | +0.001 (+0.1) | -0.019 (-0.9) | +0.015 (+0.5) | -0.013 (-0.5) |
| RRI.Volume(vr/2) | -0.002 (-0.4) | -0.010 (-1.7) | -0.000 (-0.0) | -0.001 (-0.1) | +0.013 (+0.7) | +0.015 (+0.5) |
| RRI.Support(>200dma) | +0.025 (+2.1) | -0.003 (-0.3) | +0.049 (+1.9) | +0.000 (+0.0) | +0.055 (+1.4) | +0.007 (+0.1) |

* **VPI.V (ATR % ×12) is inverted in both periods** at 1 and 5 days: the low-volatility effect. It is VPI's biggest weight.
* VPI.LQ (liquidity) is positive in both periods (1d t +2.6 / +2.6). VPI.TS (ADX) is positive out of sample.
* VPI.RS (relative strength vs Nifty 50) is ~0 in-sample and negative out of sample: no momentum edge at 20 days here.
* RRI: Support is positive in-sample only; RSIRecovery is ~0 / negative. No component is consistent, so no evidence-based fix exists.

## 3. Variants tested (decision rule fixed in advance: better in BOTH periods at 1d and 5d, not worse at 20d)

| Variant | 1d IS | 1d OOS | 5d IS | 5d OOS | 20d IS | 20d OOS | OOS 5d decile spread % |
|---|---|---|---|---|---|---|---|
| VPI now | -0.010 (-1.6) | -0.004 (-0.5) | -0.000 (-0.0) | +0.006 (+0.3) | +0.007 (+0.3) | +0.032 (+0.8) | -0.098 |
| VPI V dropped | +0.007 (+1.1) | +0.011 (+1.3) | +0.009 (+0.8) | +0.012 (+0.7) | +0.012 (+0.4) | +0.030 (+0.7) | -0.073 |
| VPI V inverted | +0.026 (+4.0) | +0.028 (+2.9) | +0.021 (+1.5) | +0.020 (+1.0) | +0.021 (+0.6) | +0.030 (+0.7) | -0.066 |
| VPI xs-pct | -0.031 (-4.5) | -0.026 (-3.0) | -0.012 (-0.8) | +0.002 (+0.1) | -0.004 (-0.1) | +0.041 (+0.9) | -0.081 |
| RRI now | -0.001 (-0.1) | -0.017 (-1.4) | +0.009 (+0.6) | -0.003 (-0.1) | +0.026 (+1.0) | +0.015 (+0.2) | -0.306 |
| RRI xs-pct | +0.001 (+0.2) | -0.015 (-1.4) | +0.010 (+0.7) | -0.003 (-0.1) | +0.028 (+1.1) | +0.015 (+0.3) | -0.236 |

ATIP composite under each VPI (full engine, point-in-time):

| VPI variant | ATIP 1d IS | ATIP 1d OOS | ATIP 5d IS | ATIP 5d OOS | ATIP 20d OOS | VPI verdict | ATIP verdict |
|---|---|---|---|---|---|---|---|
| current | +0.019 (t 2.6) | +0.009 | +0.028 | +0.017 | +0.031 | BROKEN | BROKEN |
| V dropped | +0.026 (3.6) | +0.014 | +0.033 | +0.020 | +0.031 | WEAK | WEAK |
| V flipped | +0.037 (4.9) | +0.021 (1.8) | +0.040 | +0.024 | +0.033 | WORKING | WEAK |

What the sessions actually buy (top 10 by ATIP a day, CRI < 75, price ≥ ₹20, turnover ≥ ₹2 Cr; net of 0.25 %):

| VPI variant | IS 1d | IS 5d | OOS 1d | OOS 5d | OOS 5d excess vs universe | OOS 5d hit |
|---|---|---|---|---|---|---|
| current | −0.33 % | **+0.05 %** | −0.42 % | −0.38 % | +0.11 % | 42 % |
| V flipped | −0.33 % | −0.16 % | **−0.30 %** | **−0.17 %** | +0.32 % | 44 % |

BUY gate (ATIP ≥ 65, VPI ≥ 60, ZPI ≥ 55, CRI < 60; neutral market health) under each VPI:

| VPI variant | IS rows / day | IS 5d net | OOS rows / day | OOS 5d net |
|---|---|---|---|---|
| current | 2.4 | −0.11 % | 7.0 | −0.06 % |
| V flipped | 14.0 | −0.64 % | 25.0 | −0.38 % |

**Result: no variant adopted.** The flipped V wins on whole-universe IC in both periods, but for the ten stocks a day the
sessions buy it is worse in-sample (5d +0.05 % → −0.16 %). It also lets six times as many stocks through the calibrated
BUY gate, and those do worse. Re-calibrating the gate on ~600 days would be fitting. The flip and a gate re-set are
offered to the owner as one decision. Cross-sectional percentile normalisation made VPI worse and RRI no better.

## 4. Before vs after, in one line each

* **Ranking:** ATIP's out-of-sample 5-day IC went from −0.009 to +0.017, MRI's from −0.031 to +0.018, ZPI's from −0.012 to +0.020.
* **Money:** the top decile of every score still loses after costs out of sample (5d expectancy −0.38 % to −0.77 %), in a
  falling market. The 35 stored BUY signals (28 Jul – 29 Oct) won 36 % at 5 days, net −1.28 %.
* **Redundancy:** ATIP–ACS 0.87 and ATIP–RRI 0.85 (above 0.8) out of sample.

Files: `validation_recheck.json`, `component_ic.json`, `variant_ic.json`, `analysis/*.py` (the scripts),
`analysis/atip_variant_*.txt` (their output).
