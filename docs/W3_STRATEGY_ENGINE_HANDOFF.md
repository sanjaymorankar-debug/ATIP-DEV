# ATIP W3 Strategy Engine: development handoff

**Independent functional validation: PENDING — ChatGPT**

- **Built:** 2026-09-24 in `D:\Projects\ATIP-dev` on branch `w3-strategy-engine`, on top of W1+W2 (`ba4028f`).
- **Merged:** into `master` in `D:\Projects\ATIP` (see section 10).
- **Not done:** no deployment, no restart of the live ATIP, no orders of any kind.
- **Checks run:** only the code-level checks in section 9. They are not a functional test.

## 1. Implementation Summary

The Strategy Engine turns a **versioned strategy definition** into **StrategyDecisions** and, for decisions that would change a position, into **PositionIntents** that are always `NOT_AUTHORIZED`.

Pipeline: market data and ATIP scores → point-in-time features → strategy version's evaluator → StrategyDecision → PositionIntent. Nothing in W3 sends, queues or authorises an order. There is no code path from `strategy_engine/` to a broker; execution is W4.

| ID | Feature | Implemented as |
|---|---|---|
| SE-01 | Interface & registry | JSON definitions of a known *kind*, a DB registry (`strategy`, `strategy_version`), library discovery (`strategy_engine/library/*.json`, synced on first API call and before scheduled runs). Metadata: id, name, description, category, kind, version, status, owner, source, created/updated/activated, priority, weight |
| SE-02 | Parameters | `ParamSpec`: name, type (int/float/bool/str/choice/int_list), default, min, max, allowed, required, description. Validated on every resolve. Stored per version in `strategy_parameter`; a parameter change is a new version |
| SE-03 | Rule engine | `rules.py`: all / any / not / min-of, 11 operators including crosses_above/below. Entry, exit and confirmation rules; indicator, price, volume, trend, momentum and volatility features |
| SE-04 | Quant framework | `quant_rank` kind: weighted score terms, filter, ranking, top_n, exit_rank, rebalance cadence. Uses the same feature catalogue and `register_feature()` hook that future ML features will use (no W5 work done) |
| SE-06 | Multi-factor | `multi_factor` kind: per-factor weight, direction, scaling to 0..100, factor thresholds, minimum confirmations, coverage. Output is a **standardised composite score 0..100** (stored as the decision's `score`) |
| SE-07 | Combination / selection | (a) `composite` kind (vote / weighted / priority / regime_select) as a strategy of its own; (b) `selection.combine()` across running strategies: priority, weighting, vote, conflict handling, NO_TRADE veto. Enable/disable via lifecycle state and the mapping's `enabled` flag |
| SE-08 | Regime-aware selection | Reuses Market Health `regime` (`scores/engine.py compute_mh`); no new regime engine. Configurable mapping STRONG_BULL / BULL / NEUTRAL / BEAR / HIGH_RISK (+ DEFAULT) → strategies with priority/weight, or NO_TRADE |
| SE-09 | Lifecycle | DRAFT, RESEARCH, BACKTEST, VALIDATION, APPROVED, PAPER, READY, ACTIVE, PAUSED, DISABLED, RETIRED, ARCHIVED. Invalid transitions are refused. ACTIVE needs a completed backtest of the current version. Versions are immutable; first activation is stamped on the version |
| SE-11 | Health | HEALTHY / WARNING / ERROR / STALE / DISABLED, with: last execution, last signal, signal frequency, error count, performance reference (latest real backtest, quoted as-is), data availability, parameter validity |
| — | StrategyDecision | strategy_id, strategy_version, decision_id, timestamp, symbol, decision (BUY/SELL/HOLD/WAIT/NO_TRADE), action, confidence, score, regime, reasons, parameters, stop/target/max-hold, risk requirement |
| — | PositionIntent | symbol, side, target_position (%), quantity (indicative), strategy_id, strategy_version, decision_id, timestamp, confidence, reason, **authorization_status = NOT_AUTHORIZED** |
| — | W2 integration | A stored version runs through the existing W2 backtester (section 7) |
| — | API / UI / CLI | `/api/strategies*`, page `/strategies`, `python -m strategy_engine …` |
| — | Scheduling | Post-market jobs `strategy_decisions` (PAPER/READY/ACTIVE strategies only) and `strategy_health` |

Existing strategies were kept:
- **W2 code strategies** (`REGISTRY` in `backtest/strategies.py`) still run unchanged when no version is given.
- **Library wrappers:** the `dip` and `atip_signal` library definitions wrap them as `python`-kind versions.
- **Aggressive exit:** `strategy/` and its tables `strategy_position` / `strategy_event` are untouched.

## 2. Files Added

| File | Purpose |
|---|---|
| strategy_engine/__init__.py | Package overview |
| strategy_engine/params.py | ParamSpec, resolve, validation |
| strategy_engine/definition.py | Definition schema, validation, content hash, kinds, categories |
| strategy_engine/features.py | Point-in-time feature catalogue, `register_feature` |
| strategy_engine/rules.py | Rule language and evaluator |
| strategy_engine/regime.py | RegimeProvider, MarketHealthRegime |
| strategy_engine/decisions.py | StrategyDecision, PositionIntent, action→decision map, advisory risk gate |
| strategy_engine/kinds.py | Evaluators: rule, multi_factor, quant_rank, composite, python |
| strategy_engine/registry.py | Registry, versions, metadata, library sync |
| strategy_engine/lifecycle.py | States, transitions, evidence checks, event log |
| strategy_engine/selection.py | Regime mapping, cross-strategy combination |
| strategy_engine/engine.py | Decision generation, intent persistence, scheduled job |
| strategy_engine/health.py | Health metrics and states |
| strategy_engine/adapter.py | Stored version → W2 `Strategy` |
| strategy_engine/__main__.py | CLI |
| strategy_engine/library/10_atip_zpi_momentum.json, 11_rsi_mean_reversion.json, 12_volume_breakout.json, 20_atip_multi_factor.json, 30_momentum_rank.json, 40_dip.json, 41_atip_signal.json, 90_regime_switch.json, 91_consensus.json | 9 shipped strategies |
| strategy_engine/library/regime_mapping.json | Default regime mapping (seeded once into an empty mapping table) |
| dashboard/strategy_routes.py | API routes |
| dashboard/strategy_page.py | `/strategies` page |
| docs/W3_STRATEGY_ENGINE_HANDOFF.md | This handoff |
| docs/KNOWN_DEFECTS.md | Defect register |

## 3. Files Modified

| File | Change |
|---|---|
| db/schema.py | `W3_TABLES`, created additively by `get_connection()` like W1/W2 |
| backtest/strategies.py | `make_strategy(strategy_id, params, version=None)`: code strategy when no version is given and the id is in REGISTRY, else the stored version |
| backtest/service.py | Request key `strategy_version`. Snapshot also records `strategy_definition_hash` and `strategy_kind`. A W3 strategy's position rules are sizing defaults; an explicit request wins |
| backtest/engine.py | Refuses a run whose stored definition hash no longer matches its snapshot; warning that REDUCE/ADD are not simulated |
| dashboard/server.py | Registers the strategy routes; "Strategies" link in the top bar |
| pipeline/scheduler.py | Post-market `strategy_decisions` and `strategy_health` jobs (skipped on backfill) |
| docs/ATIP_MASTER_TRACKER.csv | W3 rows (SE-01..04, 06..09, 11) updated; validation pending |

No file was deleted. No W1/W2 defect was fixed. None blocked W3, so **no compatibility fix was needed**.

## 4. Database Changes

All additive (`CREATE TABLE IF NOT EXISTS` in `W3_TABLES`, applied by `get_connection()`). No existing W1/W2 table was altered.

| Table | Key | Purpose |
|---|---|---|
| strategy | strategy_id | Registry and metadata: name, description, kind, category, status, current_version, source, owner, priority, weight, created/updated/activated |
| strategy_version | (strategy_id, version) | Immutable definition JSON + SHA-256 `definition_hash`, notes, created, first_activated_at |
| strategy_parameter | (strategy_id, version, name) | Parameter specs per version: type, default, min, max, allowed, required, description |
| strategy_regime_mapping | id; UNIQUE (regime, strategy_id) | Regime → strategy with priority, weight, enabled; or a NO_TRADE row |
| strategy_engine_event | id | Audit log: LIFECYCLE, VERSION, METADATA, REGIME_MAPPING, ERROR events |
| strategy_decision_run | run_id | One per generation: date, book, status (SUCCESS/FAILED), error, parameters, universe/evaluated counts, counts by action |
| strategy_decision | decision_id; UNIQUE (strategy_id, version, as_of, symbol) | StrategyDecisions |
| strategy_position_intent | intent_id; UNIQUE decision_id | PositionIntents, `authorization_status` DEFAULT 'NOT_AUTHORIZED' |
| strategy_health | (strategy_id, version, as_of) | Health snapshots |

`strategy_event` (named in the brief) already exists as the aggressive-exit ledger. The engine's event log is therefore `strategy_engine_event`.

## 5. API Changes

New routes (token = `X-ATIP-Token` required):

| Method | Path | Purpose |
|---|---|---|
| GET | /api/strategies | List with version, status, latest backtest, latest health |
| POST (token) | /api/strategies | Create from a definition (DRAFT) |
| GET | /api/strategies/catalog | Kinds, features, operators, decisions, actions, states, transitions, health states, regimes, combination modes |
| GET | /api/strategies/regime-mapping | Mapping; `?regime=` also shows the strategies selected for it |
| PUT (token) | /api/strategies/regime-mapping/{regime} | Replace one regime's mapping: `{"entries": "NO_TRADE"}` or `{"entries": [{strategy_id, priority, weight, enabled}]}` |
| GET | /api/strategies/combined | Combined decision per symbol (`as_of`, `mode` vote/priority/weighted, `threshold`, `min_votes`) |
| GET | /api/strategies/{id} | Detail: definition, versions, lifecycle history, events, allowed transitions, health, backtests |
| PUT (token) | /api/strategies/{id} | Metadata: name, description, category, owner, priority, weight (behaviour changes are new versions) |
| POST (token) | /api/strategies/{id} | Same as PUT (kept from the first W3 draft) |
| POST (token) | /api/strategies/{id}/versions | New immutable version |
| GET | /api/strategies/{id}/versions/{v} | One version |
| POST (token) | /api/strategies/{id}/current-version | Choose the current version |
| GET | /api/strategies/{id}/parameters | Parameter specs (`?version=`) |
| POST (token) | /api/strategies/{id}/activate, /pause, /disable, /retire, /archive | Lifecycle shortcuts `{reason?}` |
| POST (token) | /api/strategies/{id}/lifecycle | Any allowed transition `{to_state, reason, evidence}` |
| GET | /api/strategies/{id}/health | Compute + store health |
| GET | /api/strategies/{id}/decisions | Stored decisions, intents, runs (`as_of`, `limit`, `include_wait`) |
| POST (token) | /api/strategies/{id}/decisions | Generate now `{version?, as_of?, book PAPER/LIVE, store?}` |
| POST (token) | /api/strategies/{id}/backtest | W2 backtest of a version (background) |
| GET | /strategies | UI page |

Modified: `POST /api/backtests` now also accepts `strategy_version`. Without it the W2 behaviour is unchanged.

## 6. Strategy Framework

**Registry.**
- A strategy is a row in `strategy` plus immutable rows in `strategy_version`.
- Library files are registered by `sync_library()`. Re-syncing identical content is a no-op; different content under the same version is refused, and version numbers must increase.
- `update_metadata` changes descriptive fields only.

**Parameters.**
- Declared in the definition and referenced in rules as `{"param": "name"}`.
- `resolve(specs, overrides)` does the following:
  - coerces types;
  - enforces min, max and allowed values;
  - rejects unknown names;
  - requires required parameters.
- Every stored decision carries the resolved parameters.

**Rule engine.**
- A leaf is `{feature, op, value}`, where `value` is a literal, `{"param"}` or `{"feature"}`.
- Leaves combine with all / any / not / `{"min": k, "of": [...]}`.
- A missing feature value is "not met".
- Confidence = met leaves / total leaves of the entry tree.
- Every evaluation keeps a readable trace, which becomes the decision's reasons.

**Multi-factor engine.**
- Each factor is scaled to 0..100: `(x − min)/(max − min)·100`, clamped. "Lower is better" factors use 100 − s.
- Composite = Σw·s / Σw over the factors present. It is NULL when coverage is below `min_factor_coverage` (default 0.6).
- Entry needs composite ≥ entry_threshold and at least min_confirmations factors above their thresholds.
- Held positions: EXIT / REDUCE / ADD / HOLD by thresholds.
- Score = composite.

**Strategy selection (SE-07).** `selection.combine(votes, mode)` for one symbol:
- **priority:** the lowest priority number with an opinion (anything but WAIT) decides.
- **weighted:** net = Σ weight·confidence·(+1 BUY / −1 SELL) / Σ weight. BUY if net ≥ threshold, SELL if net ≤ −threshold, else HOLD/WAIT.
- **vote:** majority with `min_votes`. A BUY/SELL tie is a **conflict → WAIT**.
- **In every mode:** a member's NO_TRADE vetoes a combined BUY, and a NO_TRADE regime turns BUY into NO_TRADE. SELL is never vetoed.
- **Who takes part:** only strategies in PAPER / READY / ACTIVE that are enabled in the mapping.
- Example: A BUY, B BUY, C HOLD → BUY.

**Regime mapping (SE-08).** Seeded from `library/regime_mapping.json` when the table is empty:

| Regime | Mapped to |
|---|---|
| STRONG_BULL | momentum_rank, atip_zpi_momentum |
| BULL | atip_zpi_momentum, volume_breakout |
| NEUTRAL | rsi_mean_reversion, atip_multi_factor |
| BEAR | NO_TRADE (no defensive strategy exists yet) |
| HIGH_RISK | NO_TRADE |

The mapping is editable through the API or the CLI. A strategy can also block regimes itself (`risk.blocked_regimes`).

**Lifecycle.**
- Every move is checked against `TRANSITIONS` and logged (from, to, version, reason, evidence, actor).
- →ACTIVE and →VALIDATION need a COMPLETED backtest of the current version; →APPROVED needs an out-of-sample run.
- Decisions are generated only in PAPER / READY / ACTIVE:
  - PAPER and READY run against the PAPER book;
  - ACTIVE runs against the LIVE book, producing intents only.
- ARCHIVED is terminal. RETIRED and ARCHIVED strategies take no new versions.

**Health.** States in order of precedence:

| State | When |
|---|---|
| DISABLED | The lifecycle state is PAUSED, DISABLED, RETIRED or ARCHIVED |
| ERROR | The last run failed, or the parameters no longer resolve |
| STALE | In a decision state with no successful run for more than 4 days |
| WARNING | Any warning: errors, data availability < 80%, inactivity, excessive signals, no/weak backtest, many risk blocks |
| HEALTHY | Otherwise |

The performance reference quotes a stored backtest; nothing is estimated.

**Decisions.**

| Action | Decision |
|---|---|
| BUY, ADD | BUY |
| HOLD | HOLD |
| REDUCE, EXIT, SELL | SELL |
| NO_ACTION | WAIT |
| BLOCKED_BY_RISK | NO_TRADE |

- The signal engine's BUY/SELL/HOLD/WAIT map one-to-one.
- An advisory risk gate blocks BUY/ADD in three cases: the strategy's own `max_cri`, its `blocked_regimes`, or the W1 kill switch.

**PositionIntent.**
- Made only for BUY / ADD / REDUCE / EXIT.
- `target_position_pct` is 0 for EXIT.
- `quantity` is indicative:
  - PAPER book: the W1 `orders.risk.size_position` sizer on PAPER equity;
  - LIVE book: `None`, because the broker is never asked from here.
- `authorization_status` is forced to `NOT_AUTHORIZED` in the constructor and the DB default.

**Features.** Point-in-time at the decision date's close. Formulas are in the `features.py` docstring.
- **Bars:** close, open, high, low, volume, prev_close, change_pct, gap_pct.
- **Parametric:** sma_N, ema_N, rsi_N, atr_pct_N, ret_N, vol_ratio_N, zscore_N, range_pos_N, prior_high_N, prior_low_N, volatility_N, rel_strength_N.
- **ATIP scores:** vpi, spi, rri, mri, cri, msi, zpi, acs, atip_score, score_signal.
- **Market:** regime, mh_score, vix, vol_regime, market_trend.
- **New sources** (fundamentals, news, ML predictions) plug in via `register_feature()`.

## 7. W2 Integration

- **Running a stored version.** `backtest.strategies.make_strategy(id, params, version)` returns an `adapter.DefinitionStrategy`, a W2 `Strategy` built from the stored version.
  - W2 calls `on_bar(ctx)` with its point-in-time view, and the adapter evaluates the same `EvalEnv` the live engine uses.
  - BUY → W2 `Signal BUY` (with stop/target/max-hold); EXIT → `SELL`.
  - REDUCE/ADD are not simulated, and the run's warnings say so.
- **Request.** `POST /api/backtests` or `POST /api/strategies/{id}/backtest` with `strategy_version` (defaults to the current one); `params` overrides are validated against the version's specs.
- **Traceability.** The snapshot stores strategy_id, strategy_version, resolved params, `strategy_definition_hash` and `strategy_kind`. A re-run whose stored definition no longer hashes the same is refused ("would not reproduce").
- **Sizing.** Defaults come from the definition's position rules (max_positions, target_position_pct, stop_pct); explicit request sizing wins.
- **Existing W2 runs are unaffected.** Requests without a version for REGISTRY ids (`dip`, `atip_signal`, `buy_and_hold` code classes; the library `dip` / `atip_signal` definitions are used only when a `strategy_version` is given) take the unchanged code path.
- **Lifecycle and health read W2 results.** Lifecycle evidence and the health performance reference read `backtest_run`.

## 8. Known Defects

See `docs/KNOWN_DEFECTS.md`.
- **ChatGPT's W1/W2 list:** not provided to Claude, so those defects are not itemised yet. A placeholder row is waiting for them.
- **Development observations (KD-001..006):**
  - Anthropic key returns 401.
  - Invalid RSS feeds.
  - UTC news rows.
  - Pre-market stalls around 07:00.
  - Loss limits fail closed without P&L history.
  - The W2 stitched OOS curve starts at the first test close.
- **W3 limitations W3-L1..L6:** execution is W4; quantity is indicative; no max-hold live; no ADD/REDUCE in backtests; combined decisions aren't stored; `strategy_engine_event` naming.

All are OPEN. None was fixed in W3.

## 9. Developer Checks

**Independent functional validation: PENDING — ChatGPT**

Code-level checks actually performed (2026-09-24). The DB checks ran on a **copy** of the live database in a scratch folder; the live `atip.db` was only read, for the copy:

| Check | Result |
|---|---|
| `py_compile` of strategy_engine, dashboard, backtest, db, pipeline | 43 files, 0 errors |
| Import of all 21 W3/affected modules (incl. `pipeline.scheduler`, `dashboard.strategy_routes`) | 0 errors |
| Validate the 9 library definitions | 0 invalid |
| `get_connection()` migrations on the DB copy | All W3 tables created; W1/W2 and `strategy/` tables intact |
| `sync_library` on the copy | 9 strategies added; default regime mapping seeded |
| Lifecycle guard | DRAFT→ACTIVE without a backtest refused with the evidence message |
| `combine()` interface examples | A BUY, B BUY, C HOLD → BUY; BUY vs SELL tie → WAIT (conflict); BUY in a NO_TRADE regime → NO_TRADE |
| `generate_decisions("atip_zpi_momentum")` on the copy | Ran; decisions and intents written to the copy; every intent NOT_AUTHORIZED |
| Failure path | Unknown strategy → error raised, FAILED run row recorded |
| `compute_health` on the copy | Returns a status and metrics |
| FastAPI route registration (TestClient on a bare app) | 24 strategy routes; `regime-mapping` / `combined` / `catalog` registered before `/{sid}`; GET routes return 200 and an unknown id returns 404 |
| `git diff` review | Only W3 files and the listed integration points changed |

The functional test scenarios below are **for ChatGPT and were not run** by Claude.

| Test ID | Feature | Input | Expected |
|---|---|---|---|
| W3-T01 | SE-02 | int param with max 5 given 6 | ParamError |
| W3-T02 | SE-02 | Required param without value | ParamError |
| W3-T03 | SE-01 | Definition with an unknown top-level key | DefinitionError |
| W3-T04 | SE-01 | `add_version` same version, different content | RegistryError |
| W3-T05 | SE-01 | PUT metadata with `kind` | 400 |
| W3-T06 | SE-03 | rsi_14=25, rule rsi_14 < 30 | met; reason "✓ rsi_14=25 < 30" |
| W3-T07 | SE-03 | min 3 of 5 leaves, 4 true | met, confidence 0.8 |
| W3-T08 | SE-06 | Factors vpi 80 (w .5), cri 20 lower (w .5) | composite 80 |
| W3-T09 | SE-04 | 5 symbols, top_n 2 on a rebalance session | 2 BUY |
| W3-T10 | SE-07 | combine vote A BUY, B BUY, C HOLD | BUY |
| W3-T11 | SE-07 | combine vote A BUY, B SELL | WAIT, conflict true |
| W3-T12 | SE-07 | combine with a member NO_TRADE | no combined BUY |
| W3-T13 | SE-08 | PUT mapping BEAR NO_TRADE; combined on a BEAR day | BUY → NO_TRADE |
| W3-T14 | SE-08 | Mapping with unknown strategy | 400 |
| W3-T15 | SE-09 | DRAFT→ACTIVE without backtest | refused |
| W3-T16 | SE-09 | ARCHIVED→anything | refused |
| W3-T17 | SE-09 | pause / disable routes | status changes; event logged |
| W3-T18 | SE-11 | Strategy PAUSED | DISABLED |
| W3-T19 | SE-11 | PAPER strategy, last run > 4 days | STALE |
| W3-T20 | SE-11 | Latest run FAILED | ERROR |
| W3-T21 | Decision | Any stored decision | decision ∈ BUY/SELL/HOLD/WAIT/NO_TRADE; reasons, parameters, regime present |
| W3-T22 | Intent | Any stored intent | authorization_status NOT_AUTHORIZED; one per BUY/ADD/REDUCE/EXIT decision |
| W3-T23 | Safety | Search for broker/order calls from strategy_engine | none |
| W3-T24 | W2 | Backtest `atip_zpi_momentum` 1.0.0 | Completes; snapshot has version + hash |
| W3-T25 | W2 | Existing W2 code strategy without version | Unchanged behaviour |
| W3-T26 | API | POST without token | 401/403 |
| W3-T27 | UI | /strategies list, regime mapping, detail, health, decisions, intents | Renders |
| W3-T28 | PIT | Decision for a past date | Uses only data dated ≤ as_of |
| W3-T29 | Regression | W1/W2 test suite | Still passes |

## 10. Git Information

| Item | Value |
|---|---|
| W2 base commit | `ba4028f` (W2 research & backtesting) |
| Branch | `w3-strategy-engine` (worktree `D:\Projects\ATIP-dev`) |
| W3 commit | `5e54cc2` feat(atip): implement W3 strategy engine |
| Handoff git-record commits | `c813f2b` and the path fix after it (docs only) |
| Merge | `git merge --ff-only w3-strategy-engine` into `master` in `D:\Projects\ATIP`. A fast-forward, so **no merge commit**; W1/W2 history is preserved unchanged |
| Final main HEAD | `master` = the docs commit above (`git log --oneline -1` in `D:\Projects\ATIP`) |
| Backups before the merge | DB: `D:\Projects\ATIPtip_datatip.db.bak-before-w3-20260924-2341` (online backup, `PRAGMA integrity_check` = ok); repo: branch `backup/pre-w3-master` at `ba4028f`. Earlier backups kept |
| Live process | Not restarted. The running ATIP keeps the W2 code it loaded; the W3 post-market jobs and `/strategies` routes take effect after the owner restarts it with bare `python main.py` |
