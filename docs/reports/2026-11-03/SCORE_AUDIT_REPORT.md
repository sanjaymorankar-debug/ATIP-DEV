# ATIP score audit — ATIP, VPI, MRI, RRI, ZPI, CRI↓, ACS (3 Nov 2026)

Branch `fix/sessions-score-audit-2026-11-03`. All times IST. Line numbers are for `scores/engine.py` **after** this patch.
The evidence behind every verdict is in [SCORE_VALIDATION_REPORT.md](SCORE_VALIDATION_REPORT.md). The raw numbers are in
`validation_before.json` and `validation_after.json`.

## 1. Spec table (as implemented)

All scores are 0–100. `weighted_score` (l.21) is Σ wᵢ·cᵢ / Σ wᵢ over the components **present**. A missing input is
left out and the remaining weights are renormalised; it is never set to 0. Weights live in `weight_config`
(active = 1). Scoring runs after the close on day *t*, using bars up to and including *t*. The signals are acted on at
the next open (auto-pick, `orders/autopick.py`).

| Score | Function (file:line) | Intended meaning | Inputs → component mapping | Lookbacks | Normalisation | Weights (active) | Range |
|---|---|---|---|---|---|---|---|
| **VPI** Volatility-Potential | `compute_vpi` engine.py:473 | Room and energy to move | V = ATR%×12; TS = minmax(ADX,0,60); RS = minmax(20d return − Nifty 50 20d return, −15, +15) (l.302); LQ = 20d avg turnover, ₹100 Cr = 100 (l.291); VOL = vol_ratio/3×100; MR = RSI bucket; FG = EPS growth; IS = FII+DII; NS = news | ATR/ADX/RSI 14, RS 20, LQ 20, vol 20 | fixed-range min-max, clipped | V .18 · TS .15 · RS .12 · LQ .10 · VOL .10 · MR .10 · FG .10 · IS .08 · NS .07 = **1.00** | 0–100 |
| **MRI** Momentum-Reversal | `compute_mri` engine.py:492 | A reversal setup that is gaining strength | MACD = 100 − macd_component(hist % of price) **(reversal, SV-03)**; RSI 40–55 → 85, <40 → 60, >70 → 20, else 50; ADX = minmax(ADX,0,60) **(straight, SV-03)**; Volume = vol_ratio/2×100; EMA 9>21 → 75 else 30; News | MACD 12/26/9, RSI/ADX 14, EMA 9/21 | fixed range | MACD .25 · RSI .20 · ADX .15 · Volume .15 · EMA .15 · News .10 = **1.00** | 0–100 |
| **RRI** Recovery | `compute_rri` engine.py:518 | Recovering from a low with support | Recovery = (close − 52w low)/low ×200; RSIRecovery = (RSI−30)×2; Volume; Institutional = DII; Support = 80/30 vs 200-DMA **(absent when no 200-DMA, SV-02)**; News | 260 sessions, RSI 14, 200-DMA | fixed range | Recovery .25 · Support .20 · RSIRecovery .15 · Volume .15 · Institutional .15 · News .10 = **1.00** | 0–100 |
| **ZPI** Buy-Zone | `compute_zpi` engine.py:596 | A calm, supported, trending stock to buy | Support 80/30 and Trend 85/15 vs 200-DMA **(absent when none)**; Resistance = min(100, 2×ADX) **(straight, SV-03)**; RSI 30–50 → 90 …; ATR = 100 − ATR%×12 **(calm = high, SV-03)**; Volume (low vol = high); Institutional = delivery accumulation (l.565); Sector rank; News | ADX/RSI/ATR 14, delivery 10 vs 60 | fixed range | Support .15 · Resistance .15 · ATR .10 · Institutional .10 · News .10 · RSI .10 · Sector .10 · Trend .10 · Volume .10 = **1.00** | 0–100 |
| **CRI↓** Crash-Risk | `compute_cri` engine.py:535 | Downside / crash risk. **Higher = riskier, lower is better** | Volatility = ATR%×15; Debt = minmax(D/E,0,5); Distribution = vol_ratio×30 when > 1.5, else 20; WeakTrend = +40 below 200-DMA, +40 death cross **(absent when no 200-DMA)**; NegativeNews = 100 − news; MarketWeakness = MH < 40 +50, VIX > 18 +30, > 25 +20 | ATR 14, 200-DMA, EMA 50 | fixed range | Volatility .20 · Debt .20 · Distribution .15 · WeakTrend .15 · NegativeNews .15 · MarketWeakness .15 = **1.00** | 0–100 |
| **ACS** Confidence | `compute_acs` engine.py:789 | How far to trust today's call | HistoricalAccuracy (90d, 5-day outcomes known by *t*); Agreement = share of VPI/MRI/RRI/ZPI/MSI > 60; MarketRegime = MH; DataQuality = share of scores present; NewsConfidence; Liquidity | 90 days, 7 days news | fixed range | HistoricalAccuracy .25 · Agreement .20 · MarketRegime .20 · DataQuality .15 · Liquidity .10 · NewsConfidence .10 = **1.00** | 0–100 |
| **ATIP** master | composite, engine.py:946 | Overall attractiveness | VPI, SPI, RRI, MRI, MSI, ZPI, TS (technical score), FS, INS, **SAFETY = 100 − CRI (new, SV-04)** | — | renormalised over present | VPI .20 · SAFETY .20 · SPI .15 · RRI .10 · MRI .10 · MSI .10 · ZPI .10 · TS .10 · FS .10 · INS .05 = 1.20 stored; effective share = w/1.20. With fundamentals and ownership off (today), the present set is VPI, SAFETY, RRI, MRI, MSI, ZPI, TS = 0.90 | 0–100 |

Signal (`determine_signal`, l.840): CRI > 75 → SELL if ATIP < 40, else HOLD. HIGH_RISK regime → WAIT. BEAR and
ATIP < 70 → WAIT. BUY_STRICT is ATIP ≥ 75, VPI ≥ 70, ZPI ≥ 65, ACS ≥ 60 and MH ≥ 50. BUY_LOOSE is ATIP ≥ 65, VPI ≥ 60,
ZPI ≥ 55 and MH ≥ 40. **Both BUY gates now also need CRI < 60 (SV-04).** ATIP < 30, or CRI > 60 with MRI < 40, gives
SELL. Otherwise ATIP ≥ 50 is HOLD and anything lower is WAIT.

## 2. Findings and fixes

| # | Finding | Severity | Fix (this patch) |
|---|---|---|---|
| SV-03a | **MRI ranked backwards.** The MACD component was read as momentum, so a rising histogram scored high. In this universe a rising histogram is followed by lower returns. Old MRI out-of-sample 5-day IC −0.031 (t −2.1); in-sample also negative (−0.017). | High: INVERTED | MACD read as **reversal** (100 − component), which fits the score's own name |
| SV-03b | **MRI's ADX inverted.** A trendless stock scored high, yet ADX predicts positively in both periods (OOS IC +0.029 at 5d, +0.054 at 20d) | Medium | ADX read straight, as VPI's TS already does |
| SV-03c | **ZPI's ATR rewarded volatility** (ATR%×12). High-ATR stocks returned less in both periods (IC −0.037 IS, −0.030 OOS at 5d) | Medium: sign inverted | ATR = 100 − ATR%×12 (a buy zone is a calm one) |
| SV-03d | **ZPI "Resistance" = 100 − 2×ADX** penalised trend strength (same evidence as SV-03b) | Medium | min(100, 2×ADX) |
| SV-02a | **NaN as 0 on the 200-DMA.** `data/technical.py` stores `above_200dma = 0` when there is no 200-day average (under 200 sessions). RRI and ZPI then scored "below the 200-DMA" (Support 30, Trend 15), and CRI added +40 WeakTrend to every recent listing | High (false high CRI) | `above_200()` returns None without `sma_200`; the components drop out and are renormalised |
| SV-02b | `minmax(NaN)` returned **100**, because `max(0, min(100, nan))` is 100 in Python | Medium | NaN treated like None (neutral 50, same contract as before) |
| SV-02c | `determine_signal` read **CRI = 0 as 100** (`cri or 100`), so the safest possible stock was treated as the riskiest | Low (rare) | None → 100, 0 stays 0 |
| SV-04a | **ATIP ignored crash risk entirely** (no CRI input). CRI is the only score whose ranking holds out of sample | High | SAFETY = 100 − CRI enters ATIP at 0.2 (OOS 5-day IC +0.008 → +0.017; IS +0.013 → +0.028). 0.3 did better in both periods and was **not** taken, to avoid fitting one 5-month window |
| SV-04b | **The BUY gates had no CRI cap**, so a CRI of 74 could still be BUY | Medium | BUY needs CRI < 60 |
| — | ACS and ATIP are 0.87 correlated, and ATIP and RRI 0.85 (OOS, after). ACS's Agreement re-counts VPI/MRI/RRI/ZPI, so it double-counts the components ATIP already holds | Info | Flagged, not changed. Fixing it needs a new definition of ACS, which is out of scope for this patch |
| — | MSI is one number per day, so it carries no cross-sectional information. Inside ATIP it only shifts every stock by the same amount | Info | Flagged |
| — | MH weights sum to 1.05 (renormalised, so harmless) | Low | Flagged |
| — | `prices_daily.adj_close` is NULL in 100 % of rows. The adjusted basis is kept in `close` by `data/corporate_actions.py`, but **237 corporate actions are still `pending`** | Medium (data) | The validation harness drops any window with a one-day move > 35 %. The pending reconciliation is an open item |
| — | Sawtooth check: in the 489-symbol tracked universe, 31 symbols have ≥ 4 alternating ±5 % moves. The 229-symbol break on 2026-09-07 is entirely `bhavcopy`-only, untracked symbols, so it is outside the scored universe | Low | Masked by the jump filter |

### CRI↓ consistency (verified)

| Where | Direction | OK? |
|---|---|---|
| `dashboard/score_glossary.band` (colours) | low CRI = strong / green | ✓ |
| Score table headers `server.py` | `CRI↓` | ✓. The trade-history header and the stock page and W28 risk table were plain `CRI` and are now `CRI↓` |
| `determine_signal` SELL | CRI > 75 → SELL/HOLD; CRI > 60 + MRI < 40 → SELL | ✓ |
| `determine_signal` BUY | had no cap | **fixed**: CRI < 60 |
| Trade of the Day `cri < 30` | low is good | ✓ |
| Telegram buys `cri < 40`; sells / risk lists `ORDER BY cri DESC` | riskiest-first lists, labelled as risk | ✓ |
| `scores/lists.py` crash-risk list `ORDER BY cri DESC` | risk list (riskiest first), not a quality ranking | ✓ |
| Rank-fill (`orders/autopick._fill_rows`) | `ORDER BY atip_score DESC, cri ASC` | ✓ (new; tested) |
| ATIP composite | was absent | **fixed**: 100 − CRI |
| `backtest/strategies.max_cri` | upper cap | ✓ |

### Look-ahead, normalisation, NaN

* **Look-ahead.** Scores use bars up to *t*. Fundamentals are filtered by `available_from` (W27). Historical accuracy
  counts only outcomes known by *t*. Delivery and FII use the session before at 16:45. No future rows enter a rolling
  window. Auto-pick buys at the next session's price. The validation measures open(*t*+1) → close(*t*+*h*).
* **Normalisation.** Every score uses **fixed-range** min-max clipped to 0–100 (for example ADX 0–60 and RS ±15 %).
  None uses full-history min/max, so there is no time-series look-ahead. The clipping also bounds outliers, which plays
  the same role as winsorising. The scores are not cross-sectionally normalised per date. The validation is rank-based
  per date (Spearman), so its verdicts do not depend on that. A switch to per-date percentile ranks would change every
  displayed number and every gate (BUY ≥ 65 …). That is proposed for a separate change, not done here.
* **NaN.** EMA, MACD, ADX and RSI that are missing are left out, never 0 (checked; tested). The 200-DMA case (SV-02a)
  and NaN-in-minmax (SV-02b) were the exceptions and are fixed.

## 3. Verdicts

Point-in-time recompute, 489 symbols, 353 sessions. Out of sample = 3 Jun → 3 Nov 2026. The verdict rule is
`scores/validation.verdict`: signed IC t ≥ 2 → WORKING; ≤ −2 → INVERTED; 1–2 → WEAK; otherwise BROKEN (no measurable
relationship).

| Score | Before | After | Evidence (OOS signed IC, 5d / 20d) | Action |
|---|---|---|---|---|
| ATIP | BROKEN | BROKEN (improving) | −0.009 / +0.008 → **+0.017 / +0.031** | Fixed components plus SAFETY. IS 1-day t +2.6 after |
| VPI | BROKEN | BROKEN | +0.006 / +0.032 (unchanged) | No change. Its largest weight, V (volatility), points the wrong way at 5d but not at 20d, so it is not robust enough to flip. Proposal only |
| MRI | **INVERTED** | WEAK | −0.031 / −0.037 → **+0.018 / +0.032** | Fixed (SV-03a/b) |
| RRI | BROKEN | BROKEN | −0.003 / +0.015 | No change. No component had a consistent sign |
| ZPI | BROKEN | WEAK | −0.012 / −0.019 → **+0.020 / +0.028** (IS 5d t +2.6) | Fixed (SV-03c/d) |
| CRI↓ | WORKING | WORKING | −0.031 / −0.014; **1-day −0.045, t −4.4** | Direction made consistent (SV-04) |
| ACS | BROKEN | WEAK | +0.006 / +0.014 → +0.020 / +0.039 | Changes only through its inputs |

**Bottom line.** These scores rank stocks only weakly: the best |IC| is about 0.03–0.05. Nothing here, before or after,
turns a top-decile long into a positive expectancy after costs out of sample (the market fell over the OOS window; see
the hit-rate table). The 35 stored BUY signals (28 Jul – 29 Oct) won 36 % of trades at 5 days, with a net average of
−1.28 %. Treat every score as a ranking aid, not as a trade signal.
