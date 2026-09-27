# W22: Factor research platform handoff

**Status:** IMPLEMENTED BUT NOT VERIFIED. Build check plus a scratch-copy smoke run. ChatGPT testing is pending.
**Branch:** `w22-factor-research` (on top of W21). Not merged, not deployed.

## Tracker reconciliation

W6 (`quant/`) already delivered several rows the tracker still showed as missing:

| ID | Tracker said | Actually present since W6 |
|---|---|---|
| AF-01 | Factor library / registry: none | `quant/factors.py` registry (factor_id, version, formula, inputs, normalization, direction, data dependency), `quant_factor`, factor sets, content-hash versioning |
| AF-02 | Normalisation contract: min-max only | `quant/normalize.py` per-factor spec: winsorize, z-score, percentile, rank, min-max, market / sector relative, neutralize |
| QR-01/02/03 | IC / decay / correlation code | `quant/research.py` |

W22 builds the real gaps on top.

## Delivered

| ID | Feature | Where |
|---|---|---|
| AF-03 | Factor panel persistence | `scores.engine.weighted_score(..., label)` captures each index's components during the scoring run, stored in `score_components` (symbol, date, index, component, value, weight). **The score computation is unchanged**: re-scoring 2026-09-25 left all 501 ATIP scores and signals identical |
| AF-04 | Standalone factor families | 11 new point-in-time factors from W3 / W21 features: mean reversion (`mr_rsi_14`, `mr_zscore_20`), trend (`trend_adx_14`, `trend_dist_200`, `trend_mtf`, `trend_supertrend`), volume (`volume_ratio_20`, `volume_mfi_14`, `volume_cmf_20`), recovery (`recovery_range_252`), risk (`risk_bri`, lower is better) |
| SC-18 | Formula registry | `formula_registry`: each distinct `weights_hash` (the one stamped on every signal) with the full weight set per index, code version and formula notes. Recorded at every scoring run. `/api/formulas[/{hash}]` |
| DBS-07 / QR-12 | Research registry | `research_study` + `research_link`: hypothesis, method, status (PROPOSED → RUNNING → CONCLUDED / ABANDONED), outcome, links to factor research / experiments / backtests / factors / strategies (existence checked); concluded studies are frozen |
| QR-01/02/03 | Run on history | `python -m quant backfill --sessions N` (resumable) and `python -m quant report --start --end`: IC, IC decay, and a redundancy report (pairs \|ρ\| ≥ 0.7), stored |
| AF-08 | Factor approval gate | `quant/approval.py`: evidence criteria (\|IC\| ≥ 0.02 over ≥ 60 dates, \|t\| ≥ 2, hit rate ≥ 52% in the factor's direction, sign kept at 10 sessions, correlation < 0.8 with approved factors) → PASS / FAIL / INCOMPLETE. Decisions APPROVED / REJECTED / RETIRED with append-only history. Approving against failing evidence needs a recorded override reason. **Enforcement** in the strategy lifecycle (→ APPROVED / ACTIVE) only when `quant.require_approved_factors = true` (default false) |

## API

- New `dashboard/research_routes.py`:
  - `/api/quant/approvals[/{key}]`
  - `/api/quant/research-report`
  - `/api/research/studies*`
  - `/api/formulas*`
  - `/api/scores/components/{symbol}`
- Authz rules: approvals need `strategy:lifecycle`, studies `research:*`, formulas / components `strategy:read`.

## Smoke run (scratch copy)

- **Re-score 2026-09-25:** 501 symbols in 29.8 s. 0 scores or signals changed. Components are stored for every index. The formula hash `29a27daf0548` resolves the 70 existing signals.
- **Factor backfill:** 25 sessions × 13 factors in 1,014 s (~40 s per session). The re-run skipped all 25 (resumable).
- **Research report (20 dated ICs each, 5-session horizon):**
  - Strongest: `trend_adx_14` (IC 0.055, t 5.7), `risk_bri` (0.073, t 2.6), `mr_zscore_20` (0.052, t 2.6), `trend_dist_200` (0.068, t 2.3).
  - Negative: `volume_ratio_20` (−0.033, t −3.5).
  - Redundant pairs: `recovery_range_252` / `trend_dist_200` ρ 0.95, `mr_rsi_14` / `mr_zscore_20` 0.88, plus three more ≥ 0.7.
  - 20 dates is far below the 60 the gate requires, so these are illustrative, not findings.
- **Approval:**
  - `mr_rsi_14` evaluated FAIL (IC and t below threshold, under 60 dates).
  - Approving it was refused without a reason and accepted with an override reason, both recorded.
  - Unreviewed factors report UNREVIEWED.
- **Studies:** created, then RUNNING on the first link. A missing backtest link was refused. Concluded INCONCLUSIVE and then frozen: a further link was refused.
- **Lifecycle gate:** a strategy with no `qf_` features passes.

## Owner decisions raised

1. **`quant.require_approved_factors`:** turn it on once factors have been reviewed.
2. **Full history:** a full backfill of all factors over 400 sessions takes several hours at ~40 s per session for 13 factors. Run it off-hours on a copy, then promote the database. Speeding up the per-symbol feature context is a candidate follow-up.
