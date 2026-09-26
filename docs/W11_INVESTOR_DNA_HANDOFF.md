# W11: Investor DNA (ATIP-INV-001) handoff

**Status:** IMPLEMENTED BUT NOT VERIFIED. Build check (`py_compile`) and a scratch-database smoke run of compute / save / history only. Independent (ChatGPT) testing is pending.
**Branch:** `w11-investor-dna` (dev worktree `D:\Projects\ATIP-dev`). Not merged to `master`, not deployed.

## Scope delivered

| Requirement | Where |
|---|---|
| Investor profile questionnaire (versioned, Q1, 28 questions in six sections) | `wealth/dna.py` `QUESTIONS`, `questionnaire()` |
| Risk capacity (objective finances: horizon, age, income stability, savings rate, debt, emergency fund, dependents, liquidity need) | `compute()` |
| Risk tolerance (psychometric: reaction to a 20% fall, return/loss trade-off, objective, loss comfort, maximum loss) | `compute()` |
| Risk requirement: from the stated target return now; from goals once W13 exists (`goals.required_return_for_owner`) | `compute()`, `save()` |
| Final risk score = min(capacity, tolerance); novice cap 70; five bands | `compute()`, `band_for()` |
| Investment horizon (years + SHORT / MEDIUM / LONG) | `compute()` |
| Behavioural profile: loss aversion, disposition, overconfidence, herding/FOMO, recency, overtrading, each with level and coaching note | `compute()` |
| Observed behaviour from the paper book (FIFO round trips; default tenant only) | `observed_behaviour()` |
| Trading personality (archetype, style, ATIP mode) | `compute()` |
| Flags: tolerance > capacity, requirement > profile, low emergency fund, high debt, negative surplus, inconsistent answers, novice cap | `compute()` |
| Explainability: every score's components (input, score, weight, contribution) | `explanation` |
| Immutable version history; STALE after `profile_validity_days` (365) | `investor_profile_version`, `investor_profile`, `current()` |

## Interfaces

- Tables (additive, `db/schema.py` `WEALTH_TABLES`): `investor_profile_version` (one row per saved version, append-only by convention; W20 migration adds the trigger), `investor_profile` (pointer to the current version).
- API (`dashboard/wealth_routes.py`): `GET /api/wealth/dna/questionnaire`, `GET|POST /api/wealth/dna`, `POST /api/wealth/dna/preview`, `GET /api/wealth/dna/history`, `GET /api/wealth/dna/{profile_id}`. POSTs need `X-ATIP-Token`.
- Page: `/wealth`, tab "Investor DNA". Linked from the main dashboard's top bar ("Wealth").
- RBAC: new permissions `wealth:read` and `wealth:write` (every built-in role gets them through `_READ`, and VIEWER gets `wealth:read`). `seed()` now grants a newly introduced permission to the built-in roles that list it on an existing database, without undoing the owner's edits. Authz rules: `GET /api/wealth*` needs `wealth:read`; any other method needs `wealth:write`.
- Ownership: `(tenant_id, owner_id)` from the principal; `('default','owner')` when enterprise is off. No route accepts an owner in the request.
- Config: new optional section `wealth` (`wealth/config.py`); nothing needs to be set.

## Not changed

No trading path, risk limit, execution setting or existing table changed. The W7 enterprise risk profile stays authoritative for trading. Investor DNA is advisory input for W12–W17.

## Known limitations

- The questionnaire and weights are ATIP's own methodology (DNA-1.0), not a regulator-prescribed risk-profiling instrument. The weights sit in `compute()` and should be reviewed.
- Observed behaviour reads only the paper book. The live account keeps no trade history in ATIP (the broker reports average cost only).
