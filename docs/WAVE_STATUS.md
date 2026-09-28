# ATIP wave status (factual, as of release ATIP-W24-RC1, deployed 2026-09-28 11:31)

**Status vocabulary** (W9 brief section 4):

- **COMPLETED:** code, git and handoff evidence in master. Automated tests exist in `tests/` for W1 and W2.
- **IMPLEMENTED BUT NOT VERIFIED:** in master and compiled, with build and smoke checks only. Independent (ChatGPT) testing is still pending.
- **PARTIALLY COMPLETED:** some of the scope is deferred or blocked (see the handoff).

No wave is claimed as functionally tested by development.

| Wave | Scope | Code evidence (master) | Key commits | In master | Status | Blocking issue |
|---|---|---|---|---|---|---|
| W1 | Foundation: job monitoring, alerts, P&L, loss limits, sizing, data quality | `alerts/`, `orders/risk.py`, `portfolio/pnl.py`, `data/quality.py`, `pipeline/health.py`; `tests/` (job monitoring, risk controls, paper broker, regressions) | `3c13f63` | yes | COMPLETED | none |
| W2 | Research / backtesting | `backtest/` (16 modules: engine, metrics, costs, walk-forward, Monte Carlo); `tests/test_aggressive_backtest.py` | `ba4028f` | yes | COMPLETED | none |
| W3 | Strategy engine | `strategy_engine/` (15 modules); W3_STRATEGY_ENGINE_HANDOFF.md | `5e54cc2`, `d563978`, `b0a48e4` | yes | IMPLEMENTED BUT NOT VERIFIED | QA pending |
| W4 | Risk and execution (paper) | `execution/` (risk engine, OMS, adapters; Dhan adapter refuses); `/trading` | `50bdb8e`, `d3b8715` | yes | IMPLEMENTED BUT NOT VERIFIED | QA pending; live execution not built (by design) |
| W5 | AI / ML | `ml/` (17 modules); ML_ARCHITECTURE.md | `aa26e16`, `febe4fc`, `077e12c`, `123b340` | yes | IMPLEMENTED BUT NOT VERIFIED | QA pending; `ml.enabled` false; no active model |
| W6 | Advanced quant | `quant/` (17 modules); QUANT_ARCHITECTURE.md | `79dd809`, `cf1997d` | yes | IMPLEMENTED BUT NOT VERIFIED | QA pending; `quant.enabled` false; derivatives data pending |
| W7 | Enterprise (users, tenants, RBAC, billing foundation) | `enterprise/` (16 modules); `/login`, `/account`, `/admin` | `90c7807`, `3e52bea` | yes | PARTIALLY COMPLETED (implemented, not verified; ENT-07 exposure BLOCKED; e-mail / payments not built) | QA pending; enterprise layer off in production |
| W8 | Production hardening | `ops/` (22 modules); health, metrics, backups, migrations, MFA, refresh tokens, audit chain | `1740ab4`, `a7d7187` | yes | IMPLEMENTED BUT NOT VERIFIED; **deployed** (production restarted onto W8 on 2026-09-26 00:42; migrations 0001–0004 applied) | QA pending |
| W9 | Final release and deployment readiness | release tooling (`ops/release.py`), deploy / rollback scripts (`deploy/`), master live-trading switch, health storage / market data, docs | see `git log ATIP-W9-RC2` | yes | IMPLEMENTED BUT NOT VERIFIED; deployed with ATIP-W24-RC1 (2026-09-28) | QA pending |

Waves 11–20 (wealth track, brief "ATIP WAVES 11–20"; the brief has no Wave 10):

| Wave | Scope | Code evidence (master) | Key commits | In master | Status | Blocking issue |
|---|---|---|---|---|---|---|
| W11 | Investor DNA (ATIP-INV-001) | `wealth/dna.py`; W11_INVESTOR_DNA_HANDOFF.md | `1da609f` | yes (deployed 2026-09-28) | IMPLEMENTED BUT NOT VERIFIED | QA pending |
| W12 | Multi-asset wealth dashboard (ATIP-WLT-001) | `wealth/assets.py`, `wealth/holdings.py` | `45f0b4b` | yes (deployed 2026-09-28) | IMPLEMENTED BUT NOT VERIFIED | QA pending |
| W13 | Goal planning engine (ATIP-GOL-001) | `wealth/goals.py` | `9680ee6` | yes (deployed 2026-09-28) | IMPLEMENTED BUT NOT VERIFIED | QA pending |
| W14 | Dynamic asset allocation (ATIP-AAL-001) | `wealth/allocation.py` | `c790b95` | yes (deployed 2026-09-28) | IMPLEMENTED BUT NOT VERIFIED | QA pending; CMA need review |
| W15 | Portfolio rebalancing (ATIP-RBL-001) | `wealth/rebalance.py` | `643ad57` | yes (deployed 2026-09-28) | IMPLEMENTED BUT NOT VERIFIED | QA pending |
| W15.5 | Investor performance attribution (ATIP-PERF-001) | `wealth/perf/` (6 modules) | `8210de6` | yes (deployed 2026-09-28) | IMPLEMENTED BUT NOT VERIFIED | QA pending; LIVE sells approximate until contract-note entry |
| W16 | AI investment advisor (ATIP-AIA-001) | `wealth/advisor.py` | `4340cc8` | yes (deployed 2026-09-28) | IMPLEMENTED BUT NOT VERIFIED | QA pending; narration off by default |
| W17 | Integrated intelligence (ATIP-INT-001) | `wealth/integrated.py`; scheduler hook (`wealth.enabled`, default off) | `8346d8d` | yes (deployed 2026-09-28) | IMPLEMENTED BUT NOT VERIFIED (HTTP + browser smoke only) | QA pending |
| W18 | QA / security / performance (ATIP-QA-001) | `tests/test_wealth_*.py` (70 tests); W18 report; S1/S2 fixed | `757116e` | yes (deployed 2026-09-28) | SUITE WRITTEN, NOT RUN | ChatGPT to execute |
| W19 | Beta / UAT (ATIP-UAT-001) | `wealth/uat.py`, feedback, W19_UAT_PLAN.md | `7c837be` | yes (deployed 2026-09-28) | TOOLING READY; UAT NOT EXECUTED | user / ChatGPT acceptance |
| W20 | Production hardening (ATIP-REL-001) | migration 0005, `/health/wealth`, monitor rule, backup tables, docs | see `git log ATIP-W20-RC1` | yes (deployed 2026-09-28; migration 0005 applied) | IMPLEMENTED BUT NOT VERIFIED | W18 suite + W19 UAT exit criteria |

Waves 21–25 (remaining tracker features, built in tracker Seq order):

| Wave | Scope | Code evidence | Key commits | In master | Status | Blocking issue |
|---|---|---|---|---|---|---|
| W21 | Data & technical analysis (VWAP, S/R, betas, multi-timeframe, extra indicators, sector breadth) | `data/technical_ext.py`, `data/market_series.py`; W21 handoff | `52893af` | yes (deployed 2026-09-28) | IMPLEMENTED BUT NOT VERIFIED | QA pending; Dhan 15-min bars fail (0 rows) |
| W22 | Factor research platform (score formulas, factors, approval gate, studies) | `scores/formulas.py`, `quant/{factors,approval,studies,research}.py`; W22 handoff | `609c6d6` | yes (deployed 2026-09-28) | IMPLEMENTED BUT NOT VERIFIED | QA pending; factor backfill not run |
| W23 | Research & backtesting depth (optimisation, sensitivity, robustness, studies) | `backtest/{optimize,sensitivity,robustness,study}.py`; W23 handoff | `c00e6a7` | yes (deployed 2026-09-28) | IMPLEMENTED BUT NOT VERIFIED | QA pending |
| W24 | Machine learning (native GBM / RF / ensemble, walk-forward validation, clusters, anomalies, decay) | `ml/{trees,validation,selection,presets,unsupervised,decay}.py`; W24 handoff | `0e648af` | yes (deployed 2026-09-28, tag ATIP-W24-RC1) | IMPLEMENTED BUT NOT VERIFIED | QA pending; models show NO_EDGE, none active |
| W25 | Portfolio risk (exposure, concentration, correlation, VaR / ES, attribution, optimiser, rebalance plan, pre-trade vol / ADV / correlation / VaR limits, emergency exit, /trading risk panels) | `portfolio/{risk,optimize,limits,emergency}.py`; W25 handoff | branch `w25-portfolio-risk` | no (branch) | IMPLEMENTED BUT NOT VERIFIED; NOT DEPLOYED | QA pending; deploy needs owner authorization |

Notes:
- Delivered-baseline work before W1 (data platform, scores, dashboard) is tracked in `ATIP_MASTER_TRACKER.csv`, not as a wave.
- Git evidence: every listed commit is an ancestor of master (`git merge-base --is-ancestor`, checked on 2026-09-26; W11–W24 merged to master and deployed 2026-09-28). The whole tree compiles (`python -m compileall`).
