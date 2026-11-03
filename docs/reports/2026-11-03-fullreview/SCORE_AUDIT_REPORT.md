# Score audit: ATIP, VPI, MRI, RRI, ZPI, CRI↓, ACS (3 Nov 2026, prompt 10:54 PM IST)

This builds on the 9:23 PM session's audit ([../2026-11-03/SCORE_AUDIT_REPORT.md](../2026-11-03/SCORE_AUDIT_REPORT.md)),
which is already on master. That audit fixed MRI (inverted), ZPI's ATR and ADX, the NaN and no-200-DMA cases, and added
SAFETY = 100 − CRI to ATIP. This report re-audits every score and adds a component-level check. It also tests the
one change the brief asked about that was not made: cross-sectional normalisation. Line numbers are for
`scores/engine.py` on this branch. Evidence: [BACKTEST_VALIDATION_REPORT.md](BACKTEST_VALIDATION_REPORT.md).

## 1. Spec table (as implemented)

All scores are 0–100. `weighted_score` (l.21) is Σ wᵢ·cᵢ / Σ wᵢ over the components **present**: a missing input is
left out and the weights are renormalised, never set to 0. Weights come from `weight_config` (active = 1). Scoring runs
after the close of day *t* on bars up to *t*, and the sessions buy at the next open.

| Score | Formula (file:line) | Inputs → components | Lookbacks | Normalisation | Weights (sum) | Range | Intended meaning |
|---|---|---|---|---|---|---|---|
| **VPI** | `compute_vpi` l.473 | V = ATR %×12 · TS = ADX/60 · RS = 20-day return − Nifty 50 20-day, ±15 % · LQ = 20-day turnover (₹100 Cr = 100) · VOL = vol ratio/3 · MR = RSI room bucket · FG = EPS growth · IS = FII+DII · NS = news | 14, 20, 20 | fixed-range min-max, clipped | .18 .15 .12 .10 .10 .10 .10 .08 .07 (1.00) | 0–100 | Room and energy to move |
| **MRI** | `compute_mri` l.492 | MACD = 100 − histogram component (reversal) · RSI 40–55 → 85 · ADX straight · volume/2 · EMA 9 > 21 · news | 12/26/9, 14 | fixed range | .25 .20 .15 .15 .15 .10 (1.00) | 0–100 | A dip that is turning |
| **RRI** | `compute_rri` l.518 | Recovery = (close − 52w low)/low ×200 · RSI − 30 ×2 · volume · DII · Support vs 200-DMA (absent without one) · news | 260, 14, 200 | fixed range | .25 .15 .15 .15 .20 .10 (1.00) | 0–100 | Recovering off a low, with support |
| **ZPI** | `compute_zpi` l.596 | Support / Trend vs 200-DMA (absent without one) · Resistance = 2×ADX · RSI 30–50 → 90 · ATR = 100 − ATR %×12 (calm) · quiet volume · delivery accumulation · sector rank · news | 14, 10 vs 60 | fixed range | .15 .15 .10 .10 .10 .10 .10 .10 .10 (1.00) | 0–100 | A calm, supported buy zone |
| **CRI↓** | `compute_cri` l.535 | ATR %×15 · D/E 0–5 · distribution (vol ratio > 1.5) · below 200-DMA +40, death cross +40 (absent without a 200-DMA) · 100 − news · market weakness (MH < 40, VIX > 18, > 25) | 14, 200, 50 | fixed range | .20 .20 .15 .15 .15 .15 (1.00) | 0–100 | Crash risk: **higher = riskier, lower is better** |
| **ACS** | `compute_acs` l.789 | 90-day historical accuracy · agreement (share of VPI/MRI/RRI/ZPI/MSI > 60) · market regime · data quality · liquidity · news confidence | 90, 7 | fixed range | .25 .20 .20 .15 .10 .10 (1.00) | 0–100 | How far to trust today's call |
| **ATIP** | composite l.978 | VPI .20 · SAFETY = 100 − CRI .20 · SPI .15 · RRI .10 · MRI .10 · MSI .10 · ZPI .10 · TS .10 · FS .10 · INS .05 | — | renormalised over the parts present | 1.20 stored, effective w/Σ | 0–100 | Overall attractiveness |

Signal (`determine_signal` l.840): CRI > 75 → SELL if ATIP < 40, else HOLD · HIGH_RISK → WAIT · BEAR with ATIP < 70 → WAIT ·
BUY needs **CRI < 60** and either ATIP ≥ 75, VPI ≥ 70, ZPI ≥ 65, ACS ≥ 60, MH ≥ 50 or ATIP ≥ 65, VPI ≥ 60, ZPI ≥ 55, MH ≥ 40 ·
ATIP < 30, or CRI > 60 with MRI < 40 → SELL · otherwise ATIP ≥ 50 → HOLD, else WAIT.

## 2. Checks the brief listed

| Check | Result |
|---|---|
| Formula vs intent; inverted factors | MRI and ZPI were fixed by the 9:23 PM audit. **New tonight:** VPI's V component (ATR % ×12, its largest weight) points the **wrong way for returns**: high-ATR stocks did worse at 1 and 5 days in both periods. 1-day IC −0.057 (t −7.0) in-sample and −0.058 (t −4.6) out-of-sample. That is the low-volatility effect. It matches VPI's stated intent ("room to move") but fights CRI↓'s Volatility inside ATIP. See §4 for why it was not flipped. The technical score's ATR component has the same sign (Low). |
| CRI↓ direction | Verified everywhere it ranks, colours or gates (table in the 9:23 PM report), plus tonight: the board's rank breaks ATIP ties by **CRI ascending** (`session_board._ranks`, tested); the badge colours a low CRI green ("● CRI↓ 22, strong"); the legend states the CRI < 60 BUY cap. Risk lists sorted `cri DESC` are labelled "riskiest first". |
| Normalisation | Every component is **fixed-range** min-max clipped to 0–100 (ADX 0–60, RS ±15 %, …). There is no full-history min/max, so no time-series look-ahead, and the clipping bounds outliers like a winsorise. **Tested tonight:** per-date cross-sectional percentile ranks of the same components. For VPI that is worse (1-day IC −0.031 in-sample, −0.026 out of sample, against −0.010 / −0.004); for RRI it is the same. **Not adopted.** |
| Look-ahead | Scores use bars up to *t*; fundamentals filter on `available_from`; accuracy counts only outcomes known by *t*; the validation measures open(*t*+1) → close(*t*+*h*). |
| NaN / NULL | Missing EMA, MACD, ADX, RSI and a missing 200-DMA are left out, never 0. **Fixed tonight:** `data/technical.py` stored the unknown 200-DMA as 0 for the *technical score* (TS feeds ATIP at 0.10); `determine_signal` read MH 0 as 50 and raised on MRI None; a weekend CLI run scored a day with no bars. |
| Corporate actions | Prices are kept on one adjusted basis in `close` (`adj_close` is NULL by design). **Fixed tonight:** the archive gap-filler inserted raw pre-split rows (TDPOWERSYS), and the repair runs at deploy. 233 pending actions are all on untracked, unscored symbols (I-M5, open). |
| Composites | ATIP weights 1.20 stored, renormalised over the parts present (0.90 present today). All components are on the same 0–100 scale. **Double-counting:** ACS's Agreement re-counts VPI/MRI/RRI/ZPI, so ATIP–ACS ρ = 0.87; ATIP–RRI ρ = 0.85. Both pairs are above 0.8, flagged, not changed. ACS's HistoricalAccuracy counts HOLD/WAIT as correct when \|ret\| < 2 %, so it rewards quiet stocks (Q-M2). |

## 3. Verdicts

Point-in-time recompute: 489 symbols, 353 sessions, out of sample = the last 30 % (3 Jun → 3 Nov 2026). Rule
(`scores/validation.verdict`): signed IC t ≥ 2 → WORKING; ≤ −2 → INVERTED; 1–2 → WEAK; otherwise BROKEN. "Before" =
the 2 Nov formulas; "after" = master today, which this patch does not change for the PIT scores (re-run tonight, identical
to the committed run).

| Score | Before | After | OOS signed IC 1d / 5d / 20d (after) | Tonight |
|---|---|---|---|---|
| ATIP | BROKEN | BROKEN (improving) | +0.009 / +0.017 / +0.031; in-sample 1d t +2.6 | Board rank now shown per session. Composite unchanged |
| VPI | BROKEN | BROKEN | −0.004 / +0.006 / +0.032 | V found inverted vs returns; the flip tested and **not adopted** (§4) |
| MRI | INVERTED | WEAK | +0.006 / +0.018 / +0.032 | The glossary still said "Momentum now": corrected to "a dip that is turning" |
| RRI | BROKEN | BROKEN | −0.017 / −0.003 / +0.015 | No component has a consistent sign (§4); nothing to fix without fitting |
| ZPI | BROKEN | WEAK | +0.017 / +0.020 / +0.028 | Glossary: "calm buy zone" |
| CRI↓ | WORKING | WORKING | −0.045 (t −4.4) / −0.031 / −0.014 | Direction checked everywhere; board rank uses it |
| ACS | BROKEN | WEAK | +0.013 / +0.020 / +0.039 | HistoricalAccuracy flaw flagged (Q-M2) |

## 4. What was tested and not adopted (and why)

The decision rule was set before the results were looked at: adopt a variant only if it beats the current formula in
**both** the in-sample and the out-of-sample period, at 1 and 5 days, without being worse at 20.

| Variant | Score IC (IS / OOS, 1d) | ATIP IC (IS / OOS, 1d · 5d) | Top-10 ATIP per day, 5-day net (IS / OOS) | BUY-gate rows a day | Verdict |
|---|---|---|---|---|---|
| Current VPI | −0.010 / −0.004 | +0.019 / +0.009 · +0.028 / +0.017 | **+0.05 %** / −0.38 % | 2.4 / 7.0 | — |
| VPI with V dropped | +0.007 / +0.011 | +0.026 / +0.014 · +0.033 / +0.020 | — | — | weaker than the flip |
| VPI with V flipped (calm = high) | **+0.026 (t 4.0) / +0.028 (t 2.8)** | **+0.037 / +0.021 · +0.040 / +0.024** | −0.16 % / **−0.17 %** | **14.0 / 25.0** | **not adopted** |
| Cross-sectional percentile VPI | −0.031 / −0.026 | — | — | — | worse |
| Cross-sectional percentile RRI | +0.001 / −0.015 | — | — | — | no change |

**Why the flip was not adopted** although it makes VPI "WORKING":
1. The sessions buy the **top 10** by ATIP, not the whole cross-section. For those ten, the flip is worse in-sample at
   5 days (+0.05 % → −0.16 %) and better only out of sample. It fails the rule.
2. The BUY gates (VPI ≥ 60 / 70) were calibrated on the current VPI scale. With the flip, six times as many stocks pass
   (2.4 → 14 a day in-sample), and those BUYs average −0.64 % at 5 days instead of −0.11 %. Re-calibrating the gates on
   ~600 days is the fine-tuning the brief warns against.
3. CRI↓'s Volatility already penalises ATR inside ATIP (through SAFETY). Flipping VPI's V would count low volatility twice.

**Owner decision offered:** read VPI's V as "calm" and re-set the BUY gate to keep 2–7 BUYs a day. The evidence is in
`analysis/` and `variant_ic.json`. Until then VPI keeps its stated meaning, and the badge text says what it measures.

## 5. Bottom line

The scores rank stocks weakly: the best |IC| is 0.02–0.05, and CRI↓ is the only one that holds out of sample with t > 2.
Before and after alike, no score's top decile makes money after costs out of sample (the market fell over that window).
Treat every score as a ranking aid, not as a trade signal, and keep the sessions in PAPER.
