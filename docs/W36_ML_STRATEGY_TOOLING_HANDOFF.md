# W36 — ML and strategy tooling: handoff

**Branch:** `w36-ml-strategy-tooling` (worktree `D:\Projects\ATIP-dev-w36`), on deployed `master` `eb6f282` (ATIP-W35-RC1).

**Scope:** six features that were Yet To Start or Blocked: ML-02, ML-04, ML-16, SE-10, API-06, and AF-06 (unblocked by W35's derivatives data).

**Status:** developed. 13 new tests and the full suite pass (**499/499**). Every new page and route was smoke-tested on a throwaway database through the FastAPI app. Not merged and not deployed.

**Defaults (nothing new runs on its own):**
- The assistant is off.
- The neural network cannot be approved or activated without passing its benefit check.
- The RL agent is research only.
- Builder strategies are saved as DRAFT.

## ML-02 — Deep learning (`ml/deep.py`)

**The model.** `NeuralNetwork` (`model_type="neural_network"`) replaces the W5 placeholder. It is a native NumPy MLP:
- ReLU hidden layers (default `[32, 16]`), dropout and L2.
- Adam optimiser with mini-batches.
- **Time-ordered early stopping**: the most recent 15 % of training rows is the validation block. Training stops after `patience` epochs without improvement and keeps the best weights.
- Softmax output for classification, linear (standardised target) for regression.
- Importances are the mean absolute input gradient.
- No torch or GPU dependency.

It plugs into `make_model`, `training.train` and `validation.walk_forward` like every other family. On an XOR-type target it scores 93 % against logistic regression's 58 %.

**The adoption gate.** The tracker's condition was "only if validated benefit". `benefit_check(conn, dataset_spec)` runs walk-forward validation for the network and its baselines (logistic or linear regression, plus `native_gbm`) on the same dataset and windows. It returns **ADOPTABLE** only when all three hold:
- the pooled out-of-sample score beats the best baseline by at least 1 %;
- the network wins at least 60 % of the windows;
- the network's own verdict is EDGE.

Results are stored in `ml_dl_benefit`. `ml/registry.transition` now refuses APPROVED / ACTIVE for a `neural_network` version unless its dataset's latest check is ADOPTABLE.

## ML-04 — Reinforcement learning (`ml/rl.py`, research only)

**The agent.** Tabular Q-learning decides long or flat per stock:
- **State:** 36 states built from the 20-day momentum tercile, trend versus the 50-day average, the 20-day volatility tercile and the current position. The tercile cut points come from the training window only.
- **Reward:** next-day return, minus costs (15 bps per change), minus an optional risk penalty.

**Evaluation.** It is trained on start..split. The greedy policy is then run on split..end and compared with buy-and-hold and a trend baseline: return, Sharpe, max drawdown, exposure and turnover.

**Verdict.** BEATS_BASELINES needs a Sharpe at least 0.2 above both baselines; otherwise NO_ADVANTAGE. Runs are stored in `ml_rl_run`. The policy can't be activated and creates no decisions or orders.

## ML-16 — AI chat assistant (`ml/chat.py`, `/assistant`)

**How it works.** A manual Claude tool-use loop (default `claude-opus-5-5`, effort `medium`) over **7 read-only tools**:
- market overview
- stock detail (the W5 `explain_symbol` facts plus price and news)
- symbol search
- ranked lists (top ATIP, BUY / SELL signals, crash risk, buy zone, momentum)
- portfolio
- news
- score history

No tool can write, trade or change settings. A test asserts the tool set.

**Cost and safety:**
- The assistant has its **own** daily cost cap (`assistant.daily_cost_cap_usd`, $2), separate from news AI.
- Each request is logged in `ai_usage_log` (purpose `assistant`).
- Server-side refusal fallback is on. The system prompt and tools are cached.
- At most `max_tool_rounds` rounds per question.

**Fallback.** When the assistant is off, has no key, has hit its cap or gets an API error, it answers with the W5 rule-based facts. The answer is labelled as such.

**Conversations** are stored in `assistant_conversation` / `assistant_message`. Earlier turns are replayed as plain question-and-answer text.

**The page** has a conversation list, the thread, each answer's mode, tools and cost, and suggested questions. There is an *Assistant* link on the main dashboard.

## SE-10 — No-code strategy builder (`strategy_engine/builder.py`, `/strategy-builder`)

**The form:**
- **Entry:** ALL, ANY or AT LEAST k conditions.
- **Exit:** ALL or ANY.
- **Conditions:** a feature compared with a number, another feature, or a list.
- **Sizing:** position size, max positions, new positions per day, stop, target and max hold.
- **Ranking and risk:** the ranking feature, a CRI cap and blocked regimes, plus an optional symbol list.

`compile_spec` turns the form into a W3 `rule` definition:
- every threshold becomes a **named, bounded parameter**, so the W23 optimiser can tune it;
- features and operators are checked;
- the result goes through `definition.validate`.

`preview` evaluates the entry rule on the latest session, read-only. On live data, 499 stocks take 1.8 s. `save` creates a **DRAFT** (source `builder`) that follows the normal lifecycle. There is a *+ Build a strategy* link on `/strategies`.

## AF-06 — Derivatives factors (`quant/factors.py`)

There are 8 factors in category `derivatives`:

| Factor | Measures |
|---|---|
| `fut_basis_ann` | Futures basis, annualised |
| `oi_price_5` | OI build-up over 5 sessions, signed by price |
| `pcr_oi` | Put-call ratio on OI |
| `pcr_oi_chg_5` | PCR change over 5 sessions |
| `atm_iv` | ATM implied volatility |
| `iv_rank_252` | IV rank over 1 year |
| `iv_skew` | IV skew |
| `max_pain_gap` | Distance from max pain |

They are computed from `fo_underlying_daily` point in time (`derivatives_as_of`, through a new `FactorContext.deriv`). Coverage is F&O-segment symbols only; every other symbol gets None. The live table fills from the next post-market F&O job, so IC research needs a few weeks of history.

## API-06 — Widget JSON schemas (`dashboard/widget_schemas.py`, `dashboard/schemas/`)

**Coverage:** 16 widgets (market health, trade of the day, scores, news list / brief / usage / weights, alerts, portfolio, crash risk, top SPI, MSI, intraday scans, live P&L, feed status, strategy performance). Each has its endpoint and a JSON Schema.

**How they were made:** the schemas were drafted from the live responses on 2026-10-02 and hand-corrected:
- the "no data yet" `note` fields are optional;
- the market brief is an `anyOf` of its two shapes.

**Validation:** a dependency-free validator (type, anyOf, required, properties, items, enum, min / max). Endpoints:
- `/api/schemas` lists the catalogue;
- `/api/schemas/{widget}` returns one schema;
- `/api/schemas/{widget}/check` and `/api/schemas/check-all` fetch the live endpoint and validate it.

## Files

**New:**
- `ml/deep.py`, `ml/rl.py`, `ml/chat.py`
- `strategy_engine/builder.py`
- `dashboard/widget_schemas.py`, `dashboard/schemas/*.json` (16)
- `dashboard/w36_routes.py`, `dashboard/assistant_page.py`, `dashboard/strategy_builder_page.py`
- `db/schema_w36.py`
- `tests/test_w36_ml_strategy_tooling.py`

**Changed:**

| File | Change |
|---|---|
| `ml/models.py` | Registers NeuralNetwork |
| `ml/registry.py` | Activation gate |
| `quant/factors.py`, `quant/engine.py` | Derivatives inputs |
| `quant/derivatives.py` | Status text |
| `db/schema.py` | Applies the W36 tables |
| `dashboard/server.py` | Routes + Assistant link |
| `dashboard/strategy_page.py` | Builder link |
| `enterprise/authz.py` | Assistant / builder → `research:run`; save → `strategy:write`; schemas → `dashboard:read` |
| `config_template.json` | `assistant` section |
| `docs/ATIP_MASTER_TRACKER.csv`, `docs/WAVE_STATUS.md` | W36 rows |

## For QA

1. **Run the tests:** `python -m pytest -q`. Expect 499 passed.
2. **Benefit check:** `POST /api/ml/deep/benefit-check` with a real dataset spec from `/ml`. Check that the network and the baselines are validated on the same windows and that the verdict follows the three rules. Then try to move a `neural_network` version to ACTIVE without an ADOPTABLE check: it must be refused.
3. **RL:** `POST /api/ml/rl/runs` with 10 symbols, start, split and end. Check that out-of-sample dates begin after the split and that both baselines are reported.
4. **Assistant:** with `assistant.enabled` and a **working** key (the `.env` key returned 401 earlier), ask the five suggested questions. Check that answers cite dates, that tools are listed, that cost is logged under purpose `assistant`, and that the cap applies. With it disabled, the labelled rule-based answer appears.
5. **Builder:** build, check, preview and save. The DRAFT appears on `/strategies`. Backtest it.
6. **Schemas:** `GET /api/schemas/check-all` on the running server should show every widget `ok: true`.

## Notes

- `/api/schemas/*/check` fetches its own server over HTTP. With `enterprise.enabled` the internal request carries no session, so use it with enterprise auth off, or extend it to forward the cookie.
- The assistant uses the default Opus model per the API guidance. Set `assistant.model` to `claude-haiku-4-5` to match the news AI's cheaper choice.
