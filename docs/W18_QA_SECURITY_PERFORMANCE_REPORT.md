# W18: Full QA / security / performance gate (ATIP-QA-001) report

**Branch:** `w18-qa-security-performance` (on top of W17). Not merged, not deployed.
**Division of work (standing rule):** Claude develops; ChatGPT tests. Claude wrote the automated suite and did a static security / performance review. **The suite was compiled but NOT run by development.** Its first execution is ChatGPT's (`python -m pytest tests/test_wealth_*.py`). Smoke evidence from W11–W17 (scratch copy of production data) is listed separately and does not count as test execution.

## 1. Automated suite added (70 test functions; 3 parametrized, 84 cases)

| File | Covers |
|---|---|
| `tests/_wealth_seed.py` | Deterministic synthetic market (260 sessions: NIFTY50, ACME, GOLDBEES, LIQUIDBEES, Midcap-150), regime, scores, global data; a complete DNA answer set; two owners |
| `test_wealth_math.py` (19 + param.) | SIP / lump-sum closed forms, solved return, required SIP, unreachable targets, step-up, retirement corpus identity, reproducible and zero-vol Monte Carlo, XIRR 10% case, TWR intraday round trip and deposit independence, trade stats, strategic allocation sum / monotonicity, bounded normalisation, CMA stats, horizon caps |
| `test_wealth_dna_holdings.py` (14 + param.) | DNA validation, min(capacity, tolerance), novice cap, capacity flags, goal-driven requirement, explanations, versioning; exclusions MF / FD / NPS / insurance; ETF classification incl. GOLDIAM / JETFREIGHT; overrides; valuation and net worth; missing price valued at cost and flagged; owner isolation; paper book never counted and never visible to other tenants |
| `test_wealth_planning.py` (18) | Goal fields and inflation; past dates refused; emergency fund; horizon-capped defaults; goals → DNA requirement; allocation explanation / bounds / risk guard / policy; rebalance detection, cash-flow mode never sells, to-target drift, one-way decisions, isolation; advisor evidence on every claim, no trading, blocked advice without a profile, narration failure fallback; investor cycle all steps; scheduler off by default; suitability; mode |
| `test_wealth_performance.py` (11) | PERF-001-14 edge cases: average cost, realized P&L, fee reconciliation; intraday round trip; contributions reconcile (including a standalone fee); oversell and future dates refused; void semantics; **split between buy and today**; idempotent, owner-scoped import; **position-attribution identity**; model vs executable timing and costs; four returns + audit + CSV export + isolation |
| `test_wealth_api_security.py` (8) | **Every** mutating `/api/wealth` route (≥ 30) returns 401 without the token; every wealth route maps to `wealth:read` / `wealth:write` in the authz rules; the RBAC upgrade path grants new permissions to existing roles; 12 malformed requests → 400 (never 500), including a script-injection symbol; unknown ids → 404; API happy path; **AST check that `wealth/` imports no order / execution module**; parameterised-SQL check |

## 2. Security review (static, OWASP-oriented)

| # | Area | Finding | Status |
|---|---|---|---|
| S1 | XSS via inline handlers | A symbol (classification override) was echoed into `onclick="…('SYM')"`. HTML escaping does not protect a JS string inside an attribute | **FIXED:** `common.symbol()` restricts symbols to `[A-Z0-9&_.-]`, max 40, on every entry point (holdings, overrides, ledger, suitability). `esc()` now also escapes `'`. All other inline handlers carry server-generated ids only |
| S2 | Resource exhaustion | Monte Carlo (up to 20,000 paths × 720 months per goal) and valuation scale with goals / holdings | **FIXED:** at most 50 active goals and 1,000 active holdings per owner; paths capped at 20,000; report period ≤ 10 years; advisor question ≤ 500 chars. Global rate limiting from W8 `ops.http` still applies |
| S3 | Broken access control | Every query filters on (tenant_id, owner_id) from the principal; no route accepts an owner; the house book (broker, paper, OMS) only for the default tenant | OK (tests in §1) |
| S4 | CSRF | Every mutating route has the `X-ATIP-Token` guard (a cross-site page cannot set the header) | OK (enumerated test) |
| S5 | Injection | All SQL parameterised; f-strings only for placeholder lists / fixed column names | OK (test) |
| S6 | Trading safety | `wealth/` has no import path into orders / execution; the advisor and rebalancer state they cannot trade | OK (AST test) |
| S7 | Data sent to third parties | Optional advisor narration sends the evidence pack (holdings values, goals, profile scores) to Anthropic | OPEN by design: **off by default**, owner opt-in, documented in W16 |
| S8 | Secrets | No new secrets. The Anthropic key only from the environment / `ant` profile; never stored or logged | OK |
| S9 | Audit | DNA versions, allocation runs, rebalance decisions, advice log, perf reports and ledger voids are all recorded with actor and time | OK. Append-only **triggers** arrive with migration 0005 in W20 |
| S10 | CSV export | Symbols are validated (no leading `=`, `+`, `@`); free-text notes are not exported | OK |

No penetration test was performed. Enterprise mode (multi-user) remains off in production, and network exposure is still blocked by ENT-07.

## 3. Performance (measured in the smoke runs on a 70 MB production copy, one process)

| Operation | Time |
|---|---|
| Overview (whole chain) | 0.3 s |
| Investor cycle (8 steps) | 1.7 s |
| Performance report (PAPER, 3 months, 40 signals) | ~1.5 s |
| 3 goals created + evaluated (2,000-path Monte Carlo each) | 1.6 s |
| Wealth summary | < 0.3 s |

Indexes exist on every owner column. The scheduled cycle is off by default, and when on it runs after the post-market scoring.

## 4. Resilience

- **Investor cycle:** each step is isolated. A failure is recorded (`wealth_cycle_run`) and the rest continue; the scheduler records the job through `run_job`.
- **Narration:** every error or refusal falls back to the deterministic answer.
- **Missing data:** a holding with no price is valued at cost and flagged; missing price bars are carried forward and counted; signals without data are reported NO_DATA and carry no weight.

## 5. Model validation (what still needs a human)

The DNA weights (DNA-1.0), model portfolios / CMA / signal weights (AAL-1.0) and scenario shocks are **ATIP methodology, not validated against outcomes**. They are versioned and fully explained, so they can be reviewed. The W19 UAT should include a methodology review with a SEBI-registered adviser.

## 6. Exit criteria for W19 (UAT)

1. ChatGPT runs the new suite and the existing suite; all pass, or the defects are logged in `docs/KNOWN_DEFECTS.md`.
2. No P0 / P1 security finding open. S7 is accepted as opt-in.
3. The regression check of the existing trader pages (`/`, `/strategies`, `/trading`, `/backtests`, `/ml`, `/quant`) passes; the W17 HTTP smoke showed `/` and `/api/scores` unaffected.
