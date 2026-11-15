# ATIP — Consolidated Claude Development & Deployment Prompt

## Purpose

Use this prompt in **Claude Development Mode** to inspect, implement, test, commit, and prepare deployment of the existing ATIP application. The implementation combines the three agreed enhancements:

1. **Aggressive Trading Strategy:** 3% partial profit booking + 50% runner + 6% momentum continuation + dynamic trailing stop-loss.
2. **Authentication & Multi-User:** secure login, user isolation, individual Dhan account mapping, private portfolios, and personalized ATIP views.
3. **Testing & Deployment:** full regression/feature test suite, Git repository update, and deployment preparation for `test.bkesari.com/dhan`.

## NON-NEGOTIABLE — PRESERVE EXISTING ATIP

Before changing anything, inspect the entire repository and existing architecture.

Do NOT:
- rewrite the application;
- replace the architecture;
- refactor unrelated modules;
- remove existing features;
- change existing VPI, SPI, ZPI, RRI, MRI, CRI, MSI, ACS or Market Health formulas;
- replace existing Dhan market-data integrations unnecessarily;
- duplicate existing services;
- change unrelated UI;
- introduce a new framework when an existing project capability can be reused;
- modify production behaviour merely to make tests easier.

Implement the features as **small, isolated, backward-compatible additions**.

If a major architectural change is unavoidable, STOP and report exactly why before proceeding.

---

# PHASE 1 — DISCOVERY / BASELINE

Before coding:

1. Inspect repository structure.
2. Identify frontend/backend/framework/database.
3. Identify current ATIP modules.
4. Identify current Dhan integration and market-data service.
5. Identify current signal engine and formulas.
6. Identify current portfolio/position/order logic.
7. Identify current configuration mechanism.
8. Identify current authentication, if any.
9. Identify current test framework and CI/CD.
10. Identify current deployment configuration.
11. Identify environment variables/secrets.
12. Identify existing domain/base-path configuration.
13. Identify whether `/dhan` already exists or has routing conflicts.

Create a baseline report before modifying code:
- Relevant files/modules
- Existing Dhan architecture
- Existing portfolio architecture
- Existing trading strategy architecture
- Existing test framework
- Existing deployment mechanism
- Files proposed for modification
- Files explicitly proposed to remain untouched

Run the existing test suite before changes and record the baseline result.

---

# PHASE 2 — COMMON DHAN MARKET DATA

Maintain one common Dhan market-data service for ATIP market data wherever the current architecture supports it.

Common market data may include:
- quotes;
- OHLC;
- volume;
- historical market data;
- instrument/reference data;
- market-wide information.

Do not create separate market-data implementations per user.

However, each authenticated user must have an isolated Dhan account/portfolio context.

Architecture target:

```text
ATIP
 ├── Common Dhan Market Data Service
 │       └── shared market data
 │
 ├── Authentication / Authorization
 │
 ├── User A → Dhan Account A → Portfolio A
 ├── User B → Dhan Account B → Portfolio B
 └── User C → Dhan Account C → Portfolio C
```

---

# PHASE 3 — AUTHENTICATION & MULTI-USER MODULE

Add authentication as a separate module.

Minimum capabilities:
- registration;
- login;
- logout;
- secure password hashing;
- session/token management using the project's existing secure mechanism;
- user profile;
- ADMIN and USER roles;
- authorization middleware;
- Dhan-account mapping;
- user-specific preferences.

Never store passwords in plaintext.
Never expose Dhan tokens/secrets to the frontend.
Never put secrets in logs, source control, fixtures, or API responses.

## User isolation

Every user-specific backend query must be scoped to the authenticated user.

A user must not be able to access another user's portfolio by changing an ID in the URL/request.

Protect against IDOR/insecure direct object reference vulnerabilities.

## User-specific data

Each user must see only their own:
- Dhan account information;
- holdings;
- positions;
- orders;
- portfolio P&L;
- watchlist;
- strategy preferences;
- dashboard configuration.

## Personalization

Allow user-level configuration for items supported by the existing UI architecture, such as:
- visible widgets;
- dashboard layout;
- watchlist;
- preferred sectors;
- risk/strategy preferences;
- chart period;
- notifications;
- theme.

Do not allow ordinary users to alter global ATIP formulas/configuration.

---

# PHASE 4 — AGGRESSIVE TRADING STRATEGY

Add an isolated aggressive exit-management layer.

Default strategy:

```text
Initial position = 100%
Initial SL = existing ATIP SL methodology

Before +3%:
    If initial SL triggers → EXIT 100%

At +3%:
    Sell 50% of original position
    Keep 50% as runner
    Activate trailing stop

At +6%:
    DO NOT automatically exit the runner
    Reassess momentum

If momentum remains strong:
    HOLD runner + continue trailing

If momentum weakens:
    tighten trailing stop / exit according to configured risk logic

If trailing stop triggers:
    EXIT remaining runner
```

The 3% and 6% levels must be configurable, not scattered hard-coded constants.

Suggested defaults:

```text
TARGET_1_PERCENT = 3.0
TARGET_1_EXIT_PERCENT = 50
TARGET_2_PERCENT = 6.0
RUNNER_PERCENT = 50
TRAILING_STOP_ENABLED = true
```

Reuse existing ATR/volatility/RSI/MACD/VPI/SPI/MRI/CRI/Market Health implementations wherever available. Do not duplicate indicators.

## Trailing stop requirements

- Activate after Target 1 partial exit.
- Trail upward as price makes new highs.
- Never move downward.
- Use existing volatility/ATR methodology where available.
- Avoid an arbitrary universal trailing percentage if the existing architecture supports volatility-aware trailing.
- Tighten protection when momentum weakens according to the defined implementation.

## Target 2 requirement

+6% is a momentum checkpoint, NOT a mandatory full-exit point.

If momentum remains strong, allow the runner to continue beyond +6%.

---

# PHASE 5 — USER-SPECIFIC AGGRESSIVE TRADING

The same market signal can be common, but execution state is private.

Example:

```text
Common signal:
XYZ BUY, Target 1 +3%, Target 2 +6%

User A:
Entry ₹1000, Qty 100
→ sell 50 at +3%
→ runner 50
→ personal trailing stop

User B:
Entry ₹1020, Qty 50
→ sell 25 at +3%
→ runner 25
→ personal trailing stop
```

Use each user's actual entry price, actual position quantity, and actual broker state.

Never calculate User A's P&L or exit quantity using User B's data.

---

# PHASE 6 — POSITION/P&L STATE

Track, using existing fields where possible:
- initial quantity;
- remaining quantity;
- partial-exit quantity;
- partial-exit price;
- Target 1 state;
- runner state;
- highest price;
- trailing-stop price;
- Target 2 state;
- realized P&L;
- unrealized P&L;
- total P&L;
- exit reason.

Do not create duplicate database fields when equivalent existing fields already exist.

Exit reasons should be machine-readable, reusing existing enums/constants where available, e.g.:
- INITIAL_STOP_LOSS
- TARGET_1_PARTIAL_EXIT
- TRAILING_STOP
- TARGET_2_MOMENTUM_EXIT
- MOMENTUM_REVERSAL
- MARKET_RISK_EXIT
- MANUAL_EXIT
- END_OF_DAY_EXIT

---

# PHASE 7 — FULL TEST SUITE

Do not rewrite the existing test framework. Add tests using the existing conventions.

## Aggressive strategy tests

### Configuration
- default 3%, 50%, 6%, 50%, trailing enabled;
- configurable 40/60 and 60/40 validation;
- invalid percentages rejected;
- existing strategies unaffected.

### Entry
- correct entry price;
- correct quantity;
- initial SL created correctly;
- existing ATIP scores/formulas unchanged.

### Initial SL
- SL before +3% exits 100%;
- price above SL does not exit;
- runner does not incorrectly use stale initial SL after trailing mode activates.

### +3% partial exit
- exact +3% triggers once;
- price below +3% does not trigger;
- price gap through +3% is handled;
- 50% is based on original position quantity;
- odd quantities follow existing broker rounding rules;
- no duplicate partial exit.

### Runner
- remaining 50% is correct;
- runner is linked to original trade;
- runner state survives restart/recovery;
- runner is independently tracked per user.

### +6%
- +6% does not automatically close runner;
- strong momentum keeps runner open;
- weak momentum tightens/exits according to configured rules.

### Trailing stop
- activates after +3%;
- rises as price rises;
- never falls when price falls;
- triggers final runner exit;
- handles high/low volatility appropriately;
- persists across restart.

### Momentum/risk
Test combinations of existing VPI, SPI, MRI, RSI, MACD, volume, Market Health and CRI without changing their formulas.

### P&L
Verify:
- realized P&L;
- unrealized P&L;
- runner P&L;
- total P&L;
- existing brokerage/tax/slippage calculations;
- reconciliation after final exit.

### Idempotency
- repeated +3% event cannot sell twice;
- repeated trailing event cannot close twice;
- duplicate market events do not create duplicate orders.

### Broker/order handling
- correct partial quantity;
- rejected order does not mark position as sold;
- partial fill reconciles against actual fill;
- trailing-stop order failures are handled safely.

### Edge cases
- 1-share position;
- odd quantity;
- missing entry;
- zero/negative quantity;
- missing ATR when required;
- stale market data;
- market close/EOD according to existing policy;
- price gap from below +3% to above +6%.

## Authentication tests

- valid registration;
- duplicate registration rejected;
- invalid email rejected;
- weak password rejected;
- password is hashed;
- valid login;
- invalid login;
- logout;
- expired session/token;
- unauthorized access rejected.

## Authorization/security tests

- user can access own portfolio;
- user cannot access another user's portfolio;
- user cannot access another user's positions;
- user cannot access another user's orders;
- user cannot access another user's P&L;
- user cannot access another user's Dhan credentials;
- user cannot modify global ATIP configuration;
- admin functions work for ADMIN only.

## Dhan isolation tests

- User A maps to Dhan A;
- User B maps to Dhan B;
- User A sees only Dhan A portfolio;
- User B sees only Dhan B portfolio;
- common market data remains shared/reused;
- cache keys cannot leak portfolio data across users.

## Personalization tests

- dashboard changes affect only that user;
- watchlist changes affect only that user;
- strategy preferences affect only that user;
- User A changes never alter User B's view.

## Regression

Run the COMPLETE existing ATIP test suite after all changes.

Existing tests for:
- VPI;
- SPI;
- ZPI;
- RRI;
- MRI;
- CRI;
- MSI;
- ACS;
- Market Health;
- portfolio;
- broker integration;
- data ingestion;
- existing APIs/UI
must remain green unless a test is demonstrably obsolete because of an approved requirement change.

Do not modify a failing test merely to make it pass.

---

# PHASE 8 — BACKTEST VALIDATION

Create deterministic test/backtest scenarios:

1. Entry → SL → full exit.
2. Entry → +3% → 50% exit → runner → reversal → trailing exit.
3. Entry → +3% → +6% → strong momentum → +8%/+10% → trailing exit.
4. Entry → +3% → +6% → weak momentum → tightened trailing stop → exit.
5. Entry → never reaches +3% → SL.
6. Multiple simultaneous users with independent positions.
7. Multiple simultaneous stocks for one user.

Calculate, without hard-coding results:
- total trades;
- wins/losses;
- win rate;
- average win/loss;
- gross profit/loss;
- net profit;
- profit factor;
- expectancy;
- maximum drawdown;
- average holding time;
- Target 1 hit rate;
- Target 2 hit rate;
- runner success rate;
- trailing-stop exit rate;
- initial-SL exit rate;
- profit captured after +3%.

Do not claim a strategy win rate is profitable unless the backtest actually demonstrates it after costs/slippage.

---

# PHASE 9 — UI / ROUTING

Create or extend the authenticated Dhan/ATIP experience at:

`https://test.bkesari.com/dhan`

The `/dhan` path must be treated as the test deployment route/base path.

After login, a user should have a private dashboard containing, as supported by the existing application:
- Dhan account status;
- own holdings;
- own positions;
- own orders;
- own P&L;
- ATIP signals;
- aggressive-trade state;
- Target 1 status;
- runner status;
- trailing stop;
- personalized widgets/watchlist.

Do not redesign the entire ATIP dashboard.

Keep UI changes minimal and consistent with the current application.

---

# PHASE 10 — ENVIRONMENT & DEPLOYMENT

Deployment target:

`test.bkesari.com/dhan`

First inspect the repository's current deployment mechanism and hosting configuration.

Do not assume a deployment provider or invent credentials.

Use existing deployment files/scripts/configuration where possible.

Required environment variables/secrets must be externalized. Never commit real credentials.

At minimum verify:
- production/test mode separation;
- Dhan API configuration;
- database configuration;
- authentication secret/session configuration;
- allowed origins/host configuration;
- base URL/path configuration;
- HTTPS;
- secure cookies where applicable;
- frontend/backend API routing;
- health check.

If the GitHub/Claude environment has access to the actual deployment mechanism, deploy to the test environment.

If deployment access is NOT available from the development environment, do NOT fake deployment success. Prepare the deployment configuration and report the exact remaining deployment step/credential requirement.

---

# PHASE 11 — GIT WORKFLOW

Work safely in Git.

Before changes:
- record current branch/commit;
- inspect working tree;
- do not overwrite unrelated local changes.

Implementation should be committed as logically grouped changes, preferably:

1. Authentication / user isolation module.
2. Aggressive strategy / trailing-stop module.
3. Tests.
4. Deployment/configuration documentation.

Use clear commit messages.

Do not commit:
- secrets;
- access tokens;
- passwords;
- `.env` credentials;
- private keys;
- temporary debug files;
- generated local artifacts that do not belong in the repository.

Push the completed changes to the configured ATIP Git repository/branch according to the repository's existing workflow.

---

# PHASE 12 — DEPLOYMENT VALIDATION

After deployment, validate:

### URL
`https://test.bkesari.com/dhan`

### Authentication
- login page loads;
- valid login works;
- invalid login is rejected;
- logout works.

### User isolation
- User A sees only User A portfolio;
- User B sees only User B portfolio;
- direct URL/API manipulation cannot cross accounts.

### Dhan
- common market data loads;
- connected user's Dhan portfolio loads only for that user.

### Aggressive strategy
- +3% partial exit state works in simulation/paper mode;
- runner activates;
- +6% does not force full exit;
- trailing stop updates upward;
- trailing stop does not move downward.

### Regression
Existing ATIP screens/functions continue to work.

---

# PHASE 13 — LIVE TRADING SAFETY

Unless the existing system already has an explicitly controlled production execution mode, the new aggressive strategy must initially run in **simulation/paper/safe test mode**.

Do not accidentally send live orders from the test deployment.

For `test.bkesari.com/dhan`, default to safe test/paper execution unless explicitly configured otherwise through a protected environment setting.

---

# PHASE 14 — FINAL REPORT

After implementation provide a concise but complete report containing:

## 1. Baseline
- original commit;
- original test result;
- existing architecture discovered.

## 2. Changes
- authentication files;
- user/Dhan isolation;
- aggressive strategy files;
- test files;
- deployment files.

## 3. Existing code preserved
Explicitly state which major ATIP modules/formulas were not changed.

## 4. Test result

```text
Total tests:
Passed:
Failed:
Skipped:
New tests:
Regression tests:
Coverage:
```

## 5. Security result
Confirm:
- passwords hashed;
- secrets protected;
- no cross-user portfolio access;
- no Dhan credential leakage;
- no secrets committed.

## 6. Git
Provide:
- branch;
- commits;
- final commit SHA;
- push status.

## 7. Deployment
Provide:
- deployment status;
- deployed commit SHA;
- test URL;
- health-check result;
- any remaining manual deployment requirement.

Do NOT claim deployment is complete unless the environment was actually deployed and verified.

---

# FINAL ACCEPTANCE CRITERIA

The work is complete only when all of the following are true:

1. Existing ATIP functionality remains intact.
2. Authentication is implemented as a separate module.
3. Users can securely log in.
4. Each user can connect/use their own Dhan account.
5. Each user sees only their own portfolio/orders/positions/P&L.
6. Common Dhan market data is reused across ATIP.
7. Users can personalize their own ATIP view.
8. Aggressive strategy is implemented:
   - initial SL → 100% exit before +3%;
   - +3% → 50% partial exit;
   - remaining 50% → runner;
   - +6% → momentum checkpoint, not mandatory full exit;
   - runner → dynamic trailing stop;
   - strong momentum → allow profit to run;
   - reversal/TSL → final exit.
9. Full feature and regression test suites pass.
10. No duplicate orders or partial exits occur.
11. P&L reconciles correctly.
12. Test deployment is available at `test.bkesari.com/dhan` if deployment credentials/access are available.
13. No secrets are committed.
14. No large or unnecessary rewrite was performed.

**Core principle:**

> **Inspect first. Reuse existing ATIP architecture. Add isolated modules. Test everything. Preserve existing functionality. Deploy only what is verified. Never claim success without evidence.**
