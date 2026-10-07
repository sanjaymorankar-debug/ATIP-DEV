# ATIP API reference

Generated 2026-10-07 20:19 by `python -m ops api-docs` from the running application (552 method + path pairs). Do not edit by hand -- regenerate.

- **Base URL:** `http://127.0.0.1:8000` (local only until ENT-07). `/api/v1/...` is an alias of every `/api/...` route (ops/http.py) and adds the `API-Version` header, pagination, sort and filter on list endpoints, and the standard error envelope `{"error": {"code", "message", "request_id"}}`.
- **Auth:** with `enterprise.enabled`, a session cookie or `Authorization: Bearer <api key>`; the *Permission* column is what the authz middleware requires (enterprise/authz.py). Without enterprise, the dashboard is single-owner and local.
- **Token:** routes marked *token* change state and need the dashboard token header `X-ATIP-Token` (dashboard/security.py). Mutating order routes also accept `Idempotency-Key`.
- Full machine-readable schema: [`openapi.json`](openapi.json).

## `/`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/` | dashboard:read |  |  |  |

## `/account`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/account` | dashboard:read |  |  |  |

## `/admin`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/admin` | dashboard:read |  |  |  |
| GET | `/admin/console` | dashboard:read |  |  |  |

## `/api/account`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/account/alerts` | workspace:write |  |  |  |
| POST | `/api/account/alerts` | workspace:write |  |  |  |
| DELETE | `/api/account/alerts/{item_id}` | workspace:write |  | `item_id` |  |
| GET | `/api/account/api-keys` | workspace:write |  |  |  |
| POST | `/api/account/api-keys` | workspace:write |  |  |  |
| DELETE | `/api/account/api-keys/{key_id}` | workspace:write |  | `key_id` |  |
| GET | `/api/account/consents` | workspace:write |  |  |  |
| GET | `/api/account/deliveries` | workspace:write |  |  |  |
| POST | `/api/account/email/verify` | workspace:write |  |  | GET /api/account/consents |
| GET | `/api/account/mfa` | workspace:write |  |  |  |
| POST | `/api/account/mfa/confirm` | workspace:write |  |  |  |
| POST | `/api/account/mfa/disable` | workspace:write |  |  |  |
| POST | `/api/account/mfa/enroll` | workspace:write |  |  |  |
| GET | `/api/account/mfa/recovery` | workspace:write |  |  |  |
| POST | `/api/account/mfa/recovery` | workspace:write |  |  |  |
| GET | `/api/account/notification-preferences` | workspace:write |  |  | GET /api/account/deliveries |
| PUT | `/api/account/notification-preferences` | workspace:write |  |  | GET /api/account/deliveries |
| GET | `/api/account/notifications` | notifications:read |  | `unread`=False | POST /api/account/notifications/read {ids?} |
| POST | `/api/account/notifications/read` | workspace:write |  |  |  |
| GET | `/api/account/privacy` | workspace:write |  |  | {kind, reason} |
| POST | `/api/account/privacy` | workspace:write |  |  | {kind, reason} |
| GET | `/api/account/profile` | workspace:write |  |  |  |
| PUT | `/api/account/profile` | workspace:write |  |  |  |
| GET | `/api/account/reports` | workspace:write |  |  |  |
| POST | `/api/account/reports` | workspace:write |  |  |  |
| DELETE | `/api/account/reports/{item_id}` | workspace:write |  | `item_id` |  |
| GET | `/api/account/risk-profile` | workspace:write |  |  |  |
| GET | `/api/account/sessions` | workspace:write |  |  |  |
| DELETE | `/api/account/sessions/{session_id}` | workspace:write |  | `session_id` |  |
| GET | `/api/account/vault` | workspace:write |  |  | DELETE /api/account/vault/{credential_id} |
| POST | `/api/account/vault` | workspace:write |  |  | DELETE /api/account/vault/{credential_id} |
| GET | `/api/account/vault/expiring` | workspace:write |  | `hours`=12 | ?hours=12 |
| DELETE | `/api/account/vault/{credential_id}` | workspace:write |  | `credential_id` |  |
| POST | `/api/account/vault/{credential_id}/verify` | workspace:write | token | `credential_id` | read-only connection check |
| GET | `/api/account/watchlists` | workspace:write |  |  |  |
| POST | `/api/account/watchlists` | workspace:write |  |  |  |
| DELETE | `/api/account/watchlists/{item_id}` | workspace:write |  | `item_id` |  |

## `/api/admin`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| PUT | `/api/admin/api-keys/{key_id}/limits` | system:operate |  | `key_id` |  |
| GET | `/api/admin/audit` | audit:read |  | `tenant_id`, `user_id`, `action`, `limit`=200 |  |
| POST | `/api/admin/billing-cycle/run` | admin:billing |  |  |  |
| GET | `/api/admin/console` | admin:tenants |  |  |  |
| POST | `/api/admin/dunning/run` | admin:billing |  |  | POST /api/admin/billing-cycle/run   (platform admin) |
| POST | `/api/admin/invoices` | admin:billing |  |  |  |
| GET | `/api/admin/isolation` | admin:tenants |  |  |  |
| GET | `/api/admin/onboarding` | admin:users |  |  | GET /api/admin/payments |
| GET | `/api/admin/payments` | admin:billing |  |  |  |
| GET | `/api/admin/permissions` | admin:roles |  |  |  |
| GET | `/api/admin/plans` | admin:billing |  |  | PUT /api/admin/plans/{id}   GET/PUT /api/admin/subscriptions/{tenant} |
| PUT | `/api/admin/plans/{pid}` | admin:billing |  | `pid` |  |
| GET | `/api/admin/privacy` | admin:users |  |  | POST /api/admin/privacy/{request_id}/decision |
| POST | `/api/admin/privacy/{request_id}/decision` | admin:users |  | `request_id` |  |
| GET | `/api/admin/risk-profiles/{scope}/{sid}` | risk:configure |  | `scope`, `sid` |  |
| PUT | `/api/admin/risk-profiles/{scope}/{sid}` | risk:configure |  | `scope`, `sid` |  |
| GET | `/api/admin/roles` | admin:roles |  |  | PUT /api/admin/roles/{role}   GET /api/admin/permissions |
| PUT | `/api/admin/roles/{role}` | admin:roles |  | `role` |  |
| GET | `/api/admin/subscriptions/{tid}` | admin:billing |  | `tid` |  |
| PUT | `/api/admin/subscriptions/{tid}` | admin:billing |  | `tid` |  |
| GET | `/api/admin/tenants` | admin:tenants |  |  |  |
| POST | `/api/admin/tenants` | admin:tenants |  |  |  |
| PUT | `/api/admin/tenants/{tid}` | admin:tenants |  | `tid` |  |
| POST | `/api/admin/tenants/{tid}/status` | admin:tenants |  | `tid` |  |
| GET | `/api/admin/usage` | admin:billing |  | `tenant_id`, `days`=30 | POST /api/admin/invoices   GET /api/admin/audit |
| GET | `/api/admin/users` | admin:users |  | `tenant_id` |  |
| POST | `/api/admin/users` | admin:users |  |  |  |
| PUT | `/api/admin/users/{uid}` | admin:users |  | `uid` |  |
| POST | `/api/admin/users/{uid}/mfa-reset` | admin:users |  | `uid` |  |
| POST | `/api/admin/users/{uid}/password-reset` | admin:users |  | `uid` |  |
| PUT | `/api/admin/users/{uid}/roles` | admin:users |  | `uid` |  |

## `/api/alerts`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/alerts` | dashboard:read |  |  |  |

## `/api/assistant`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| POST | `/api/assistant/ask` | research:run | token |  | {question, conversation_id?} |
| GET | `/api/assistant/conversations` | dashboard:read |  | `limit`=30 | ?limit |
| GET | `/api/assistant/conversations/{cid}` | dashboard:read |  | `cid` |  |
| GET | `/api/assistant/status` | dashboard:read |  |  | settings (no secrets) + today's spend |

## `/api/audit`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| POST | `/api/audit/export` | system:operate | token |  |  |
| GET | `/api/audit/export-status` | execution:read |  |  | ; POST /api/audit/export {source?}                                     SEC-03 |
| GET | `/api/audit/intent/{iid}` | execution:read |  | `iid` |  |
| GET | `/api/audit/order/{oid}` | execution:read |  | `oid` |  |

## `/api/auth`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| POST | `/api/auth/consent` | signed-in |  |  |  |
| POST | `/api/auth/forgot` | public |  |  |  |
| POST | `/api/auth/login` | public |  |  | {username, password, tenant_id?} -> token + HttpOnly cookie |
| POST | `/api/auth/logout` | signed-in |  |  |  |
| GET | `/api/auth/me` | signed-in |  |  |  |
| POST | `/api/auth/password` | signed-in |  |  |  |
| POST | `/api/auth/refresh` | public |  |  | W8: exchange a refresh token (body refresh_token or the atip_refresh cookie) for a new session + a new refresh token. Reuse of a used token revokes everything. |
| POST | `/api/auth/register` | public |  |  | {username, password, email, tenant_name}  (allow_self_registration) |
| POST | `/api/auth/reset` | public |  |  | {reset_token, new_password} |
| POST | `/api/auth/switch-tenant` | signed-in |  |  |  |
| POST | `/api/auth/verify-email` | public |  |  |  |

## `/api/backtests`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/backtests` | research:read |  | `limit`=50, `strategy_id` |  |
| POST | `/api/backtests` | research:run | token |  | Body: a backtest request (backtest/service.py). Returns run_id; runs in the background. |
| POST | `/api/backtests/event-driven` | research:run | token |  | a backtest request + {"event_driven": {...}}    BT-17 |
| POST | `/api/backtests/optimize` | research:run | token |  | Body: {request, space, method?, select_by?, max_trials?, seed?, min_trades?}. |
| POST | `/api/backtests/robustness` | research:run | token |  | Body: {request (start/end), n_subsamples?, seed?}. |
| POST | `/api/backtests/sensitivity` | research:run | token |  | Body: {request, space, select_by?, steps?, pairwise?}. |
| GET | `/api/backtests/strategies` | research:read |  |  |  |
| POST | `/api/backtests/study` | research:run | token |  | Body: {strategy_id, start, end, space?, hypothesis?, method?, max_trials?, universe?}. |
| POST | `/api/backtests/walkforward` | research:run | token |  | Body: {request:{...start,end...}, train, validation, test, step, candidates?, select_by?}. |
| GET | `/api/backtests/{run_id}` | research:read |  | `run_id` |  |
| GET | `/api/backtests/{run_id}/drawdowns` | research:read |  | `run_id` |  |
| GET | `/api/backtests/{run_id}/equity` | research:read |  | `run_id` |  |
| GET | `/api/backtests/{run_id}/metrics` | research:read |  | `run_id` |  |
| GET | `/api/backtests/{run_id}/montecarlo` | research:read |  | `run_id` |  |
| POST | `/api/backtests/{run_id}/montecarlo` | research:run | token | `run_id` | Body: {method: trade_shuffle\|return_bootstrap, n_sims, seed, block_size}. |
| GET | `/api/backtests/{run_id}/trades` | research:read |  | `run_id` |  |

## `/api/billing`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/billing` | admin:billing |  |  | POST /api/billing/plan {plan_id} |
| POST | `/api/billing/invoices/{invoice_id}/pay` | admin:billing |  | `invoice_id` |  |
| POST | `/api/billing/plan` | admin:billing |  |  |  |

## `/api/brokers`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/brokers` | portfolio:read |  |  | supported brokers, credential fields, live status |
| GET | `/api/brokers/consolidated` | portfolio:read |  |  | holdings across the caller's brokers |
| GET | `/api/brokers/open-orders` | portfolio:read |  |  | -> portfolio:read; other GETs -> dashboard:read. |
| POST | `/api/brokers/payload-preview` | portfolio:manage | token |  | {order_id, broker} |
| GET | `/api/brokers/{broker}/snapshot` | portfolio:read |  | `broker` | holdings + positions at one broker (read-only) |

## `/api/compliance`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/compliance` | audit:read |  |  | latest run (checks + evidence) and run history |
| GET | `/api/compliance/dsr` | audit:read |  |  | open data-subject requests against the SLA |
| GET | `/api/compliance/inventory` | audit:read |  |  | every table: class / personal / retention (gaps = null) |
| GET | `/api/compliance/regulatory` | audit:read |  |  | the ENT-14 register |
| POST | `/api/compliance/regulatory/{item_id}` | system:operate | token | `item_id` | {status, reviewer, reference, note} |
| POST | `/api/compliance/run` | system:operate | token |  | run every check now |

## `/api/data`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/data/alt` | dashboard:read |  |  | sources, health                                   AD-01/02 |
| POST | `/api/data/alt/{source_id}/run` | research:run | token | `source_id` | {as_of?} |
| GET | `/api/data/alt/{source_id}/{metric}` | dashboard:read |  | `source_id`, `metric`, `entity`, `start`, `end`, `as_of` | ?entity&start&end&as_of |
| GET | `/api/data/assets` | dashboard:read |  |  | catalogue (assets + MF)                           DP-21 |
| POST | `/api/data/assets/refresh` | research:run | token |  |  |
| GET | `/api/data/assets/{asset_class}/{symbol}` | dashboard:read |  | `asset_class`, `symbol`, `start`, `end` | ?start&end |
| GET | `/api/data/depth` | dashboard:read |  | `symbol`, `limit`=200 | ?symbol&limit=200  latest order-book snapshots     DP-05 |
| GET | `/api/data/depth/features` | dashboard:read |  | `symbol`, `day` | ?symbol&day |
| POST | `/api/data/depth/snapshot` | research:run | token |  | {symbols?} |
| GET | `/api/data/fo/chain` | dashboard:read |  | `symbol`, `expiry`, `ts` | ?symbol&expiry&ts |
| POST | `/api/data/fo/chain/snapshot` | research:run | token |  | {symbol} |
| GET | `/api/data/fo/contracts` | dashboard:read |  | `symbol`, `date`, `expiry` | ?symbol&date&expiry                               DP-08 |
| POST | `/api/data/history/backfill` | research:run | token |  | {symbols?, max?, years?} one budgeted backfill pass |
| GET | `/api/data/history/coverage` | dashboard:read |  |  | how much of the universe reaches back 7 years |
| GET | `/api/data/lake` | dashboard:read |  |  | datasets, partitions, rows, bytes, write format   DP-22 |
| POST | `/api/data/lake/archive` | research:run | token |  | {table, date_col, start?, end?, knowledge_col?} |
| POST | `/api/data/lake/verify` | research:run | token |  | re-hash every file against the manifest |
| GET | `/api/data/macro` | dashboard:read |  |  | latest point-in-time snapshot + calendar           DP-14 |
| POST | `/api/data/macro/calendar` | research:run | token |  | {event_date, event, importance?, series_id?, note?} |
| POST | `/api/data/macro/refresh` | research:run | token |  |  |
| GET | `/api/data/macro/{series_id}` | dashboard:read |  | `series_id`, `as_of` | ?as_of |
| GET | `/api/data/mf` | dashboard:read |  | `q`, `limit`=50 | ?q=&limit=50  latest NAVs, name search |
| GET | `/api/data/ticks` | dashboard:read |  | `day`, `symbol`, `limit`=2000 | ?day&symbol&limit=2000 |
| POST | `/api/data/ticks/minute-bars` | research:run | token |  | {day?}  rebuild 1-min bars from ticks |
| GET | `/api/data/ticks/status` | dashboard:read |  | `days`=10 | ?days=10  tick capture per day                    DP-04 |

## `/api/data-quality`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/data-quality` | dashboard:read |  |  | DP-19: the latest session's checks and DQS, plus the DQS history. |

## `/api/enterprise`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/enterprise/status` | public |  |  |  |

## `/api/execution`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/execution/algos` | execution:read |  | `status`, `limit`=100 | ?status=&limit=        algo parents            EX-11 |
| POST | `/api/execution/algos` | execution:trade | token |  | {risk_decision_id, algo, params?}  start one by hand |
| POST | `/api/execution/algos/tick` | execution:trade | token |  | work every parent now (in session) |
| GET | `/api/execution/algos/{pid}` | execution:read |  | `pid` | parent, children, execution quality |
| POST | `/api/execution/algos/{pid}/cancel` | execution:trade | token | `pid` | {reason?} |
| POST | `/api/execution/algos/{pid}/resume` | execution:trade | token | `pid` | {reason?} |
| GET | `/api/execution/analytics` | execution:read |  | `days`=30, `start`, `end` | ?days=30 \| start&end                                     EX-10 |
| GET | `/api/execution/assets` | execution:read |  |  | asset classes and their execution / risk paths |
| GET | `/api/execution/broker-health` | execution:read |  |  | latest + 24 h history                                    BR-06 |
| POST | `/api/execution/broker-health/check` | execution:trade | token |  | {network?: true} |
| GET | `/api/execution/events` | execution:read |  | `topic`, `key`, `limit`=200 | ?topic&key&limit   recent OMS events           EX-16 |
| POST | `/api/execution/events/dispatch` | execution:trade | token |  | deliver pending events now |
| GET | `/api/execution/events/stats` | execution:read |  | `hours`=24 | ?hours=24  by topic, dead letters, handlers |
| GET | `/api/execution/impact` | execution:read |  | `symbol`, `quantity`, `side`=BUY, `price` | ?symbol&quantity&side=BUY&price=             EX-12 |
| POST | `/api/execution/impact/calibrate` | execution:trade | token |  | refit Y from LIVE fills |
| GET | `/api/execution/latency` | execution:read |  | `minutes`=1440 | ?minutes=1440  per-stage summary               EX-15 |
| POST | `/api/execution/latency/flush` | execution:trade | token |  |  |
| GET | `/api/execution/latency/{stage}` | execution:read |  | `stage`, `minutes`=1440 | ?minutes=1440  per-minute series |
| GET | `/api/execution/options/book` | execution:read |  |  | positions (marked), cash, premium held, recent trades |
| POST | `/api/execution/options/check` | execution:trade | token |  | {underlying, expiry, strike, option_type, side, lots} (no fill) |
| POST | `/api/execution/options/order` | execution:trade | token |  | same body: risk-checked paper fill |
| POST | `/api/execution/options/settle` | execution:trade | token |  | expiry settlement now |
| POST | `/api/execution/paper-match` | execution:trade | token |  | fill resting paper orders that have crossed              EX-02 |
| GET | `/api/execution/reconciliation` | execution:read |  | `limit`=5 | ?limit=5                                                 BR-05 |
| POST | `/api/execution/reconciliation/run` | execution:trade | token |  |  |
| POST | `/api/execution/run` | system:operate | token |  | {execute?: bool, as_of?}. execute false = risk only; true = also send APPROVED orders to the PAPER adapter. |
| GET | `/api/execution/sandbox-check` | execution:read |  |  | read-only steps                                          BR-08 |
| GET | `/api/execution/slippage` | execution:read |  | `days`=30, `strategy_id` | ?days=30&strategy_id=                                    EX-09 |
| GET | `/api/execution/status` | execution:read |  |  | mode, live gate, auto-execute, adapter |

## `/api/formulas`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/formulas` | strategy:read |  |  | ; GET /api/formulas/{weights_hash} |
| GET | `/api/formulas/{weights_hash}` | strategy:read |  | `weights_hash` |  |

## `/api/health`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| POST | `/api/health/recover` | system:operate | token |  | Re-run every missed / failed job now (pipeline/recover.py) and report what is still open. |

## `/api/lists`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/lists/crash-risk` | dashboard:read |  | `n`=25, `date` | ?n=25&date= |
| GET | `/api/lists/msi` | dashboard:read |  | `sessions`=60 | ?sessions=60 |
| GET | `/api/lists/spi` | dashboard:read |  | `n`=25, `date` | ?n=25&date= |

## `/api/live-quotes`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/live-quotes` | dashboard:read |  |  | Live LTP for every symbol currently shown on the dashboard (scores + portfolio). |

## `/api/market`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/market/deal-signal` | dashboard:read |  |  | latest AD-03 event study |
| GET | `/api/market/derivatives` | dashboard:read |  | `date`, `kind`, `limit`=50 | ?date=&kind=INDEX\|STOCK&limit=50  (by total OI) |
| GET | `/api/market/feed-status` | dashboard:read |  |  | DP-01 stock feed (and index feed) status |
| GET | `/api/market/fundamentals/{symbol}` | dashboard:read |  | `symbol` | quarters (newest first) + filings stored |
| GET | `/api/market/global-history` | dashboard:read |  | `series`=us_10y, `days`=365 | ?series=us_10y&days=365 |
| GET | `/api/market/ownership/{symbol}` | dashboard:read |  | `symbol` | shareholding history, insider trades, SAST, features |
| GET | `/api/market/preopen` | dashboard:read |  |  | GIFT Nifty now + implied gap vs Nifty's last close, |
| POST | `/api/market/refresh/{job}` | system:operate | token | `job` | (token) job = fundamentals \| institutional \| fo \| |

## `/api/market-pulse`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/market-pulse` | dashboard:read |  |  | everything + the overall context and its reasons |
| GET | `/api/market-pulse/events` | dashboard:read |  | `days`=30 | FOMC / US CPI / payrolls / RBI dates, the session each hits, today's band |
| POST | `/api/market-pulse/events` | research:run | token |  | {date, kind, title?} add an event; POST .../events/delete {date, kind} |
| POST | `/api/market-pulse/events/delete` | research:run | token |  |  |
| GET | `/api/market-pulse/fii` | dashboard:read |  |  |  |
| POST | `/api/market-pulse/gift` | research:run | token |  | capture the GIFT Nifty / global-model gap estimate now |
| GET | `/api/market-pulse/global` | dashboard:read |  |  | \| /fii \| /positioning   the parts |
| GET | `/api/market-pulse/global-sync` | dashboard:read |  |  | the 15:30 -> 08:45 synchronised model: record and today's estimate |
| POST | `/api/market-pulse/global-sync/capture` | research:run | token |  | {label: close \| pre} take the global snapshot now |
| GET | `/api/market-pulse/positioning` | dashboard:read |  |  |  |
| POST | `/api/market-pulse/refresh` | research:run | token |  | fetch NSE participant OI (7 days) and the Nifty history now |

## `/api/market-regime`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/market-regime` | dashboard:read |  |  | the market gate today: status, distribution days, 200-DMA, changes |
| GET | `/api/market-regime/history` | dashboard:read |  | `days`=250 | one row per session (Nifty, DMAs, distribution days, gate) |
| POST | `/api/market-regime/run` | research:run | token |  | recompute the gate now |

## `/api/mh`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/mh` | dashboard:read |  |  |  |

## `/api/ml`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/ml/ai-strategy-readiness` | ml:read |  |  | gate chain per ML strategy |
| GET | `/api/ml/anomalies` | ml:read |  | `as_of` | ?as_of ; POST (token) {as_of?, threshold?} |
| POST | `/api/ml/anomalies` | ml:write | token |  |  |
| POST | `/api/ml/bootstrap` | ml:write | token |  | (token) {start, end, families?} -- background |
| GET | `/api/ml/clusters` | ml:read |  |  | latest run ; POST (token) {as_of?, k?} |
| POST | `/api/ml/clusters` | ml:write | token |  |  |
| GET | `/api/ml/datasets` | ml:read |  |  | POST (token) {spec..., build?: false} |
| POST | `/api/ml/datasets` | ml:write | token |  | Body = a DatasetSpec (+ build: true to build and snapshot it in the background). |
| GET | `/api/ml/datasets/{did}` | ml:read |  | `did` |  |
| POST | `/api/ml/deep/benefit-check` | ml:write | token |  | {dataset_spec, params?, n_windows?} |
| GET | `/api/ml/deep/benefit-checks` | ml:read |  | `limit`=50 |  |
| GET | `/api/ml/explain/{symbol}` | ml:read |  | `symbol`, `as_of`, `q` | ?as_of&q= -- read-only "why" facts |
| GET | `/api/ml/feature-sets` | ml:read |  |  | POST (token) {name, version, features, description} |
| POST | `/api/ml/feature-sets` | ml:write | token |  |  |
| GET | `/api/ml/features` | ml:read |  |  | feature registry (metadata) |
| GET | `/api/ml/health` | ml:read |  |  | latest model / factor health ; POST (token) run now |
| POST | `/api/ml/health` | ml:write | token |  |  |
| GET | `/api/ml/labels` | ml:read |  |  | label kinds + stored label definitions |
| POST | `/api/ml/labels` | ml:write | token |  | (token) {name, version, spec, description} |
| GET | `/api/ml/models` | ml:read |  |  | POST (token) {model_id, name, model_type, label_kind, feature_set, ...} |
| POST | `/api/ml/models` | ml:write | token |  |  |
| GET | `/api/ml/models/{mid}` | ml:read |  | `mid` |  |
| POST | `/api/ml/models/{mid}/activate` | ml:lifecycle | token | `mid` |  |
| POST | `/api/ml/models/{mid}/lifecycle` | ml:lifecycle | token | `mid` |  |
| POST | `/api/ml/models/{mid}/pause` | ml:lifecycle | token | `mid` |  |
| POST | `/api/ml/models/{mid}/train` | ml:write | token | `mid` | {dataset: DatasetSpec, params?, validation_fraction?, embargo?} -- runs in the background; follow it in /api/ml/training-runs. |
| GET | `/api/ml/monitoring` | ml:read |  | `model_id`, `limit`=60 | ?model_id |
| GET | `/api/ml/monitoring-series` | ml:read |  | `model_id`, `version` | ?model_id=&version=   drift / realised metrics over time |
| GET | `/api/ml/pca` | ml:read |  | `as_of`, `lookback`=120 | ?as_of&lookback |
| POST | `/api/ml/predict` | ml:write | token |  | (token) {model_id, version?, as_of?, store?} |
| GET | `/api/ml/predictions` | ml:read |  | `model_id`, `symbol`, `as_of`, `limit`=200 | filters; GET /api/ml/predictions/{id} |
| GET | `/api/ml/predictions/{pid}` | ml:read |  | `pid` |  |
| GET | `/api/ml/regime` | ml:read |  | `as_of` | deterministic vs configured provider for a date |
| GET | `/api/ml/rl/runs` | ml:read |  | `limit`=30 | ; GET /api/ml/rl/runs/{run_id} |
| POST | `/api/ml/rl/runs` | ml:write | token |  | {symbols, start, end, split, params?} |
| GET | `/api/ml/rl/runs/{run_id}` | ml:read |  | `run_id` |  |
| GET | `/api/ml/status` | ml:read |  |  | config, model families available, counts |
| GET | `/api/ml/training-runs` | ml:read |  | `model_id`, `limit`=100 |  |
| GET | `/api/ml/validation` | ml:read |  |  | reports ; GET /api/ml/validation/{id} |
| POST | `/api/ml/validation` | ml:write | token |  | (token) {model_type, dataset, params?, windows?} -- background |
| GET | `/api/ml/validation/{rid}` | ml:read |  | `rid` |  |
| GET | `/api/ml/validation/{rid}/selection` | ml:read |  | `rid`, `min_stability`=0.6 |  |
| POST | `/api/ml/validation/{rid}/selection` | ml:write | token | `rid` |  |

## `/api/mobile`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/mobile/summary` | dashboard:read |  |  |  |

## `/api/news`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/news` | dashboard:read |  |  |  |
| GET | `/api/news/ai-usage` | dashboard:read |  | `days`=7 | ?days=7  AI requests, tokens, cost, the daily cap |
| GET | `/api/news/announcements` | dashboard:read |  | `symbol`, `event_type`, `days`=7, `n`=100 | ?symbol=&event_type=&days=7&n=100 |
| POST | `/api/news/announcements/run` | research:run | token |  | (token) fetch today's announcements now; body {days?, deep?} |
| GET | `/api/news/sources` | dashboard:read |  |  | measured source weights + feed status |
| GET | `/api/news/summary` | dashboard:read |  |  | latest market brief (Claude or rule-based, labelled) |
| POST | `/api/news/summary` | system:operate | token |  | (token) regenerate now; body {hours?} |
| GET | `/api/news/symbol-scores` | dashboard:read |  | `date`, `n`=50 | ?date=&n=50  weighted news score per stock (50 = neutral) |

## `/api/notifications`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/notifications/unsubscribe` | public |  |  |  |

## `/api/oms`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/oms/executions` | execution:read |  | `order_id`, `limit`=200 | GET /api/oms/fills   GET /api/oms/positions |
| GET | `/api/oms/fills` | execution:read |  | `strategy_id`, `symbol`, `limit`=200 |  |
| GET | `/api/oms/orders` | execution:read |  | `status`, `strategy_id`, `symbol`, `limit`=200 | GET /api/oms/orders/{id} |
| POST | `/api/oms/orders` | execution:trade | token |  | {risk_decision_id, submit?: true, order_type?: MARKET\|LIMIT\|SL\|SL-M, limit_price?, trigger_price?} (W29 EX-02). PAPER only. |
| GET | `/api/oms/orders/{oid}` | execution:read |  | `oid` |  |
| POST | `/api/oms/orders/{oid}/cancel` | execution:trade | token | `oid` |  |
| POST | `/api/oms/orders/{oid}/modify` | execution:trade | token | `oid` | {quantity?, limit_price?, trigger_price?, order_type?}   EX-08 |
| POST | `/api/oms/orders/{oid}/protective-stop` | execution:trade | token | `oid` | {stop_price?, limit_offset_pct?}                         EX-02 |
| POST | `/api/oms/orders/{oid}/refresh` | execution:trade | token | `oid` |  |
| POST | `/api/oms/orders/{oid}/submit` | execution:trade | token | `oid` |  |
| GET | `/api/oms/positions` | execution:read |  |  |  |

## `/api/onboarding`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/onboarding` | workspace:write |  |  |  |
| POST | `/api/onboarding/setup` | workspace:write |  |  |  |

## `/api/ops`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/ops/alerts` | system:operate |  |  | monitoring alert state |
| GET | `/api/ops/audit/verify` | system:operate |  |  | enterprise_audit hash-chain check |
| GET | `/api/ops/backups` | system:operate |  |  | POST /api/ops/backups (verified backup now) |
| POST | `/api/ops/backups` | system:operate | token |  |  |
| POST | `/api/ops/backups/prune` | system:operate | token |  |  |
| GET | `/api/ops/config` | system:operate |  |  | effective configuration, secrets MASKED |
| GET | `/api/ops/data` | system:operate |  |  | data freshness + latest data-quality results |
| GET | `/api/ops/jobs` | system:operate |  |  | heartbeat, locks, next runs, last 24 h runs |
| GET | `/api/ops/metrics` | system:operate |  |  | Prometheus text format |
| POST | `/api/ops/monitor/run` | system:operate | token |  | evaluate the rules now (notifies like the scheduled job) |
| GET | `/api/ops/secrets` | system:operate |  |  | presence / source / rotation (never values) |
| GET | `/api/ops/status` | system:operate |  |  | environment, config fingerprint + findings, migrations, |
| GET | `/api/ops/webhooks` | system:operate |  |  | POST /api/ops/webhooks {url, events, secret_name} |
| POST | `/api/ops/webhooks` | system:operate | token |  |  |
| POST | `/api/ops/webhooks/deliveries/{delivery_id}/requeue` | system:operate | token | `delivery_id` |  |

## `/api/options`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| POST | `/api/options/analyse` | research:run | token |  | {symbol?, spot, legs, lot_size?, target_date?} |
| POST | `/api/options/build` | research:run | token |  | {template, symbol, expiry, spot?, width_steps?, lot_size?, target_date?} |
| GET | `/api/options/chain/{symbol}` | dashboard:read |  | `symbol` | spot, lot size, expiries and strikes ATIP has stored |
| GET | `/api/options/templates` | dashboard:read |  |  |  |

## `/api/orderbook`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/orderbook/depth20` | dashboard:read |  |  | 20-level depth: latest DWI per watchlist stock, persistent flags, feed |
| GET | `/api/orderbook/depth20/validation` | dashboard:read |  | `horizon`=1 | does DWI / best-level / 20-level imbalance predict the next 1 / 5 minutes? |
| GET | `/api/orderbook/pressure` | dashboard:read |  | `side`, `limit`=100 | latest pending buy / sell totals per stock today |
| GET | `/api/orderbook/pressure/{symbol}` | dashboard:read |  | `symbol` | today's polls for one stock |
| POST | `/api/orderbook/snapshot` | research:run | token |  | poll the whole universe now |

## `/api/orders`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/orders` | execution:read |  | `symbol`, `status` |  |
| POST | `/api/orders` | orders:manage | token |  |  |
| GET | `/api/orders/broker-status` | execution:read |  |  |  |
| GET | `/api/orders/pending` | execution:read |  |  |  |
| DELETE | `/api/orders/{rule_id}` | orders:manage | token | `rule_id` |  |
| POST | `/api/orders/{rule_id}/confirm` | orders:manage | token | `rule_id`, `force`=False |  |
| POST | `/api/orders/{rule_id}/reject` | orders:manage | token | `rule_id` |  |

## `/api/platform`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/platform/postgres` | system:operate |  |  | dialect scan: how far the code is from PostgreSQL |

## `/api/pnl`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/pnl` | portfolio:read |  |  | PF-02 / PF-13: both books valued now (no broker call), today's risk state, and the last 60 pnl_daily rows. |
| GET | `/api/pnl/live` | portfolio:read |  |  | ; GET /api/pnl/live/series ?day=                                                  MON-04 |
| GET | `/api/pnl/live/series` | portfolio:read |  | `day` |  |

## `/api/portfolio`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/portfolio` | portfolio:read |  |  |  |
| POST | `/api/portfolio/imports/commit` | portfolio:manage | token |  | {filename, content, broker?, as_of?, force?} |
| POST | `/api/portfolio/imports/from-broker` | portfolio:manage | token |  | {broker} |
| POST | `/api/portfolio/imports/preview` | portfolio:manage | token |  | {filename, content}   (CSV text) |
| GET | `/api/portfolio/imports/runs` | portfolio:read |  |  |  |

## `/api/position-intents`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/position-intents` | execution:read |  | `status`, `as_of`, `strategy_id`, `limit`=200 | intents with authorization status |

## `/api/privacy`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/privacy/policy` | public |  |  |  |

## `/api/quant`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/quant/approvals` | quant:read |  |  | every factor's approval state |
| GET | `/api/quant/approvals/{key}` | quant:read |  | `key` | evidence evaluation + history (key e.g. mom_12_1@1) |
| POST | `/api/quant/approvals/{key}` | strategy:lifecycle | token | `key` | (token) {decision, reason} |
| GET | `/api/quant/composites` | quant:read |  |  | POST (token) {name, version, components, min_coverage} |
| POST | `/api/quant/composites` | quant:write | token |  |  |
| POST | `/api/quant/compute` | quant:write | token |  | Factor scores + composites for a date (background; ~all tracked symbols). |
| GET | `/api/quant/derivatives` | quant:read |  |  | instruments + data status (PENDING) |
| GET | `/api/quant/events` | quant:read |  | `symbol`, `event_type`, `limit`=200 | ?symbol ; POST /api/quant/events/sync (token) |
| POST | `/api/quant/events/sync` | quant:write | token |  |  |
| GET | `/api/quant/experiments` | quant:read |  |  | POST (token) create; POST /{id}/run (token, background) |
| POST | `/api/quant/experiments` | research:run | token |  |  |
| POST | `/api/quant/experiments/{eid}/run` | research:run | token | `eid` |  |
| GET | `/api/quant/factor-sets` | quant:read |  |  | POST (token) {name, version, factors} |
| POST | `/api/quant/factor-sets` | quant:write | token |  |  |
| GET | `/api/quant/factors` | quant:read |  | `category` | registry;  GET /api/quant/factors/{id} |
| GET | `/api/quant/factors/{fid}` | quant:read |  | `fid` |  |
| GET | `/api/quant/microstructure` | quant:read |  | `symbol`, `date`, `limit`=200 | ?symbol&date |
| GET | `/api/quant/options` | quant:read |  | `limit`=200 | analytics (PENDING) ; GET /api/quant/options/price  BS calculator |
| GET | `/api/quant/options/price` | quant:read |  | `S`, `K`, `days`, `sigma`, `r`=0.065, `kind`=call, `price`, `q`=0.0 | Black-Scholes calculator: give sigma for price + greeks, or price for implied vol. |
| GET | `/api/quant/pairs` | quant:read |  |  | POST (token) create; POST /{id}/status (token); |
| POST | `/api/quant/pairs` | quant:write | token |  |  |
| POST | `/api/quant/pairs/screen` | quant:write | token |  | (token) {symbols, lookback, min_corr} |
| GET | `/api/quant/pairs/{pid}/analyze` | quant:read |  | `pid`, `as_of`, `store`=False |  |
| POST | `/api/quant/pairs/{pid}/status` | quant:write | token | `pid` |  |
| GET | `/api/quant/portfolios` | quant:read |  | `limit`=20 | with positions and exposures |
| POST | `/api/quant/portfolios` | quant:write | token |  | (token) {name, key, top_n, bottom_n, method, constraints, long_short} |
| GET | `/api/quant/rankings` | quant:read |  | `key`, `as_of`, `top`, `bottom`, `sector`, `by` | ?as_of&key&top&bottom&sector ; ?by=sector for sector ranks |
| GET | `/api/quant/research` | quant:read |  | `key`, `limit`=50 | POST (token) {key, kind: ic\|ic_decay\|quantiles\|correlation, ...} |
| POST | `/api/quant/research` | research:run | token |  | {key, kind: ic\|ic_decay\|quantiles\|correlation, start, end, horizon?, keys?, as_of?} -- background. |
| POST | `/api/quant/research-report` | research:run | token |  | (token) {start, end} IC / decay / redundancy for all factors |
| GET | `/api/quant/scores` | quant:read |  | `as_of`, `key`, `symbol`, `limit`=200 | ?as_of&key&symbol&limit |
| GET | `/api/quant/spreads` | quant:read |  | `pair_id`, `limit`=100 | stored spread snapshots |
| GET | `/api/quant/status` | quant:read |  |  | config, factor coverage, data dependencies |
| GET | `/api/quant/strategies` | quant:read |  |  | W3 strategies of kinds pairs / portfolio / multi_factor / |
| GET | `/api/quant/volatility/{symbol}` | quant:read |  | `symbol`, `as_of` | close-to-close / Parkinson / GK / downside / regime |

## `/api/refresh`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| POST | `/api/refresh` | system:operate | token |  |  |

## `/api/reports`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/reports/{report_id}/outputs` | workspace:write |  | `report_id` | GET /api/reports/{report_id}/outputs/{output_id} |
| GET | `/api/reports/{report_id}/outputs/{output_id}` | workspace:write |  | `report_id`, `output_id` |  |
| POST | `/api/reports/{report_id}/run` | workspace:write |  | `report_id` | {format}   PUT /api/reports/{report_id}/schedule |
| PUT | `/api/reports/{report_id}/schedule` | workspace:write |  | `report_id` |  |

## `/api/research`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/research/equity` | research:read |  | `rating`, `limit`=500 | latest rating per symbol |
| POST | `/api/research/equity/run` | research:run | token |  | {symbols?} build and store reports for the universe |
| GET | `/api/research/equity/{symbol}` | research:read |  | `symbol`, `fresh`=0 | today's stored report, else built now (?fresh=1 rebuilds) |
| GET | `/api/research/equity/{symbol}/history` | research:read |  | `symbol` | rating / target calls and their outcomes |
| POST | `/api/research/equity/{symbol}/refresh` | research:run | token | `symbol` | build and store today's report |
| GET | `/api/research/hit-rate` | research:read |  |  | closed calls by rating |
| GET | `/api/research/scorecard-record` | research:read |  | `horizon`=60 | return vs the Nifty after 20 / 60 / 120 / 250 sessions by checks passed |
| GET | `/api/research/scorecard/{symbol}` | research:read |  | `symbol` | the fundamental scorecard: 5 axes x 6 checks, each with its numbers |
| GET | `/api/research/studies` | research:read |  | `status` | ?status ; POST (token) {title, hypothesis, method, tags, supersedes} |
| POST | `/api/research/studies` | research:run | token |  |  |
| GET | `/api/research/studies/{sid}` | research:read |  | `sid` |  |
| POST | `/api/research/studies/{sid}/abandon` | research:run | token | `sid` |  |
| POST | `/api/research/studies/{sid}/conclude` | research:run | token | `sid` |  |
| POST | `/api/research/studies/{sid}/links` | research:run | token | `sid` |  |

## `/api/risk`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/risk/decisions` | risk:read |  | `status`, `strategy_id`, `symbol`, `limit`=200 | risk decisions (filters) |
| GET | `/api/risk/decisions/{rid}` | risk:read |  | `rid` |  |
| POST | `/api/risk/decisions/{rid}/approve` | risk:approve | token | `rid` |  |
| GET | `/api/risk/emergency-exit` | risk:read |  | `book`=PAPER | ?book  the plan + confirm code |
| POST | `/api/risk/emergency-exit` | risk:approve | token |  | (token, risk:approve) {book, confirm, reason?, last_close?} |
| GET | `/api/risk/emergency-exit/history` | risk:read |  | `limit`=20 |  |
| POST | `/api/risk/evaluate` | risk:configure | token |  | {intent_ids?: [...], as_of?} -- risk only, never orders. |
| GET | `/api/risk/exposure` | portfolio:read |  |  | equity, positions, sector, strategy exposure |
| POST | `/api/risk/frontier` | risk:configure | token |  | (token) {symbols? \| book?, points?, ...} |
| GET | `/api/risk/limits` | risk:read |  |  | effective limits + source |
| PUT | `/api/risk/limits` | risk:configure | token |  | {key: value \| null \| "default"} |
| POST | `/api/risk/optimize` | risk:configure | token |  | (token) {symbols? \| book?, objective, max_weight?, sector_cap?, |
| GET | `/api/risk/optimize/{opt_id}` | risk:read |  | `opt_id` |  |
| GET | `/api/risk/portfolio` | risk:read |  | `book`=PAPER, `lookback`=250, `days`=60 | ?book=PAPER\|LIVE&lookback=250&days=60  full analysis |
| GET | `/api/risk/portfolio/snapshots` | risk:read |  | `book`, `limit`=60 | ?book&limit  stored post-market headlines |
| POST | `/api/risk/portfolio/what-if` | risk:configure | token |  | (token) {weights: {sym: w}, value?} |
| POST | `/api/risk/rebalance-plan` | risk:configure | token |  | (token) {opt_id \| weights, book?, band_pct?, min_trade_value?, |

## `/api/scans`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/scans/intraday` | dashboard:read |  | `session`, `scan` | ?session=&scan=   latest run of the session |
| POST | `/api/scans/intraday/run` | system:operate | token |  | (token) run the scans now |

## `/api/schemas`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/schemas` | dashboard:read |  |  | ; GET /api/schemas/check-all ; GET /api/schemas/{widget} ; GET /api/schemas/{widget}/check |
| GET | `/api/schemas/check-all` | dashboard:read |  |  |  |
| GET | `/api/schemas/{widget}` | dashboard:read |  | `widget` |  |
| GET | `/api/schemas/{widget}/check` | dashboard:read |  | `widget` |  |

## `/api/scores`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/scores` | dashboard:read |  |  |  |
| GET | `/api/scores/components/{symbol}` | strategy:read |  | `symbol`, `date` | the sub-factors behind each ATIP index |

## `/api/screener`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/screener/fields` | dashboard:read |  |  | field catalogue (groups, units, aliases), presets, operators |
| GET | `/api/screener/run` | dashboard:read |  | `query`, `sort`, `desc`=1, `limit`=200, `columns` | run a screen (read-only) |
| GET | `/api/screener/run.csv` | dashboard:read |  | `query`, `sort`, `desc`=1, `limit`=2000, `columns` | the same, as CSV |
| GET | `/api/screener/saved` | dashboard:read |  |  | saved screens; POST {name, query, sort?, desc?, columns?, notify?, screen_id?} |
| POST | `/api/screener/saved` | workspace:write | token |  |  |
| POST | `/api/screener/saved/{screen_id}/delete` | workspace:write | token | `screen_id` |  |
| GET | `/api/screener/saved/{screen_id}/run` | dashboard:read |  | `screen_id` |  |

## `/api/signals`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/signals/intraday` | dashboard:read |  | `date` | intraday scan hits on 15-minute bars (ORB, open = low / high, squeeze) |
| POST | `/api/signals/intraday/run` | research:run | token |  | scan today's stored 15-minute bars now |
| GET | `/api/signals/intraday/stats` | dashboard:read |  |  | their record: return to the close and vs the Nifty, alert status |
| GET | `/api/signals/technical` | dashboard:read |  | `date`, `direction`, `min_confluence`=0, `limit`=300, `alignment` | signals with levels, confluence, market gate |
| GET | `/api/signals/technical/forward` | dashboard:read |  | `horizon`=20, `min_confluence`=0 | vs the Nifty after 5 / 20 / 60 sessions, by scan x |
| GET | `/api/signals/technical/gate-effect` | dashboard:read |  | `min_confluence`=0 | closed signals WITH / MIXED / AGAINST the market gate |
| POST | `/api/signals/technical/run` | research:run | token |  | {symbols?} compute today's snapshot and signals now |
| GET | `/api/signals/technical/stats` | dashboard:read |  | `min_confluence`=0, `alignment` | track record per scan: win rate, average R |
| GET | `/api/signals/technical/symbol/{symbol}` | dashboard:read |  | `symbol` | latest technical snapshot + recent signals for one stock |

## `/api/stock`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/stock/{symbol}/history` | dashboard:read |  | `symbol`, `sessions`=400 |  |

## `/api/strategies`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/strategies` | strategy:read |  |  | list (+ latest backtest, health) |
| POST | `/api/strategies` | strategy:write | token |  | create from a definition (DRAFT) |
| GET | `/api/strategies/catalog` | strategy:read |  |  | kinds, features, operators, states |
| GET | `/api/strategies/combined` | strategy:read |  | `as_of`, `mode`=vote, `threshold`=0.3, `min_votes`=1 | combined decision across strategies |
| GET | `/api/strategies/regime-mapping` | strategy:read |  | `regime` | regime -> strategies mapping |
| PUT | `/api/strategies/regime-mapping/{regime}` | strategy:lifecycle | token | `regime` | Body: {"entries": "NO_TRADE"} or {"entries": [{strategy_id, priority, weight, enabled}]}. |
| GET | `/api/strategies/{sid}` | strategy:read |  | `sid` |  |
| POST | `/api/strategies/{sid}` | strategy:write | token | `sid` | Same as PUT (kept for clients that cannot send PUT). |
| PUT | `/api/strategies/{sid}` | strategy:write | token | `sid` | Metadata only: name, description, category, owner, priority, weight. Behaviour (rules, parameters, kind) changes are new versions. |
| POST | `/api/strategies/{sid}/activate` | strategy:lifecycle | token | `sid` | -> ACTIVE (needs a completed backtest of the current version). ACTIVE means daily decisions against the LIVE book -- intents only, never orders. |
| POST | `/api/strategies/{sid}/archive` | strategy:lifecycle | token | `sid` |  |
| POST | `/api/strategies/{sid}/backtest` | research:run | token | `sid` | A W2 backtest of a stored version. Body = a W2 backtest request without strategy_id; version defaults to the current one. Runs in the background. |
| POST | `/api/strategies/{sid}/current-version` | strategy:lifecycle | token | `sid` |  |
| GET | `/api/strategies/{sid}/decisions` | strategy:read |  | `sid`, `as_of`, `limit`=200, `include_wait`=False |  |
| POST | `/api/strategies/{sid}/decisions` | strategy:write | token | `sid` | {version?, as_of?, book: PAPER\|LIVE, store: true}. Decisions and NOT_AUTHORIZED intents only -- never orders. |
| POST | `/api/strategies/{sid}/disable` | strategy:lifecycle | token | `sid` |  |
| GET | `/api/strategies/{sid}/health` | strategy:read |  | `sid`, `version` |  |
| POST | `/api/strategies/{sid}/lifecycle` | strategy:lifecycle | token | `sid` | {to_state, reason, evidence}: any transition lifecycle.TRANSITIONS allows. |
| POST | `/api/strategies/{sid}/ml-activate` | strategy:lifecycle | token | `sid` | (token, strategy:lifecycle) {to_state: PAPER\|ACTIVE, reason?} |
| GET | `/api/strategies/{sid}/parameters` | strategy:read |  | `sid`, `version` |  |
| POST | `/api/strategies/{sid}/pause` | strategy:lifecycle | token | `sid` |  |
| POST | `/api/strategies/{sid}/retire` | strategy:lifecycle | token | `sid` |  |
| POST | `/api/strategies/{sid}/versions` | strategy:write | token | `sid` |  |
| GET | `/api/strategies/{sid}/versions/{version}` | strategy:read |  | `sid`, `version` |  |

## `/api/strategy-builder`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| POST | `/api/strategy-builder/compile` | research:run | token |  | {spec} -> W3 definition (validated) |
| GET | `/api/strategy-builder/options` | dashboard:read |  |  | operators + feature catalogue |
| POST | `/api/strategy-builder/preview` | research:run | token |  | {spec, symbols?} -> today's entry matches (read-only) |
| POST | `/api/strategy-builder/save` | strategy:write | token |  | {spec} -> DRAFT strategy |

## `/api/strategy-decisions`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/strategy-decisions` | strategy:read |  | `as_of`, `strategy_id`, `decision`, `limit`=200, `include_wait`=False | all strategies' decisions (filters) |

## `/api/strategy-performance`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/strategy-performance` | dashboard:read |  | `days`=30 | ?days=30 |

## `/api/tenant`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/tenant/book` | execution:read |  |  |  |

## `/api/tod`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/tod` | dashboard:read |  |  |  |

## `/api/wealth`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| POST | `/api/wealth/advisor/ask` | wealth:write | token |  | (token) {question, topic?, narrate?} -> claims + evidence, |
| GET | `/api/wealth/advisor/history` | wealth:read |  | `limit`=50 | ; GET /api/wealth/advisor/{id} |
| GET | `/api/wealth/advisor/topics` | wealth:read |  |  | topics + suggested questions + narration setting |
| GET | `/api/wealth/advisor/{aid}` | wealth:read |  | `aid` |  |
| POST | `/api/wealth/advisor/{aid}/feedback` | wealth:write | token | `aid` |  |
| GET | `/api/wealth/allocation` | wealth:read |  |  | latest stored run (404 if none) |
| GET | `/api/wealth/allocation/cma` | wealth:read |  |  | capital market assumptions + model portfolios + bounds |
| GET | `/api/wealth/allocation/policy` | wealth:read |  |  | ; PUT (token) {bounds, excluded_classes, tactical_enabled, max_tilt_pct} |
| PUT | `/api/wealth/allocation/policy` | wealth:write | token |  |  |
| POST | `/api/wealth/allocation/preview` | wealth:write | token |  | (token) compute, not stored |
| POST | `/api/wealth/allocation/run` | wealth:write | token |  | (token) compute + store a run |
| GET | `/api/wealth/allocation/runs` | wealth:read |  | `limit`=50 | ; GET /api/wealth/allocation/runs/{id} |
| GET | `/api/wealth/allocation/runs/{rid}` | wealth:read |  | `rid` |  |
| GET | `/api/wealth/allocation/signals` | wealth:read |  |  | tactical signals now (ATIP market intelligence) |
| GET | `/api/wealth/assets` | wealth:read |  |  | asset-class registry (supported / EXCLUDED classes) |
| GET | `/api/wealth/classifications` | wealth:read |  |  | symbol overrides ; PUT (token) {symbol, asset_class, instrument} |
| PUT | `/api/wealth/classifications` | wealth:write | token |  |  |
| DELETE | `/api/wealth/classifications/{symbol}` | wealth:write | token | `symbol` | (token) |
| POST | `/api/wealth/cycle` | wealth:write | token |  | (token) run the investor cycle now |
| GET | `/api/wealth/cycles` | wealth:read |  | `limit`=30 | cycle history |
| GET | `/api/wealth/dna` | wealth:read |  |  | current profile (404 if none) + status CURRENT / STALE |
| POST | `/api/wealth/dna` | wealth:write | token |  | (token) {answers} -> new immutable version |
| GET | `/api/wealth/dna/history` | wealth:read |  |  | versions |
| POST | `/api/wealth/dna/preview` | wealth:write | token |  | (token) {answers} -> computed, not stored |
| GET | `/api/wealth/dna/questionnaire` | wealth:read |  |  | question bank (version Q1) |
| POST | `/api/wealth/dna/refresh` | wealth:write | token |  | (token) re-score the current answers (risk requirement |
| GET | `/api/wealth/dna/{profile_id}` | wealth:read |  | `profile_id` | one stored version (own only) |
| GET | `/api/wealth/feedback` | wealth:read |  |  | the caller's own feedback |
| POST | `/api/wealth/feedback` | wealth:write | token |  | (token) {page, category, severity, message, context} |
| GET | `/api/wealth/goals` | wealth:read |  | `include_inactive`=False | active goals with evaluation (?include_inactive=true) |
| POST | `/api/wealth/goals` | wealth:write | token |  | (token) create |
| GET | `/api/wealth/goals/overview` | wealth:read |  |  | totals, status counts, required return, link warnings |
| DELETE | `/api/wealth/goals/{gid}` | wealth:write | token | `gid` |  |
| GET | `/api/wealth/goals/{gid}` | wealth:read |  | `gid` |  |
| PUT | `/api/wealth/goals/{gid}` | wealth:write | token | `gid` |  |
| GET | `/api/wealth/goals/{gid}/events` | wealth:read |  | `gid` |  |
| POST | `/api/wealth/goals/{gid}/projection` | wealth:write | token | `gid` |  |
| GET | `/api/wealth/goals/{gid}/projections` | wealth:read |  | `gid` |  |
| POST | `/api/wealth/goals/{gid}/simulate` | wealth:write | token | `gid` |  |
| POST | `/api/wealth/goals/{gid}/status` | wealth:write | token | `gid` |  |
| GET | `/api/wealth/holdings` | wealth:read |  | `include_closed`=False | manual holdings ; POST (token) create |
| POST | `/api/wealth/holdings` | wealth:write | token |  |  |
| DELETE | `/api/wealth/holdings/{hid}` | wealth:write | token | `hid` |  |
| GET | `/api/wealth/holdings/{hid}` | wealth:read |  | `hid` |  |
| PUT | `/api/wealth/holdings/{hid}` | wealth:write | token | `hid` |  |
| GET | `/api/wealth/liabilities` | wealth:read |  |  | ; POST (token) ; PUT / DELETE /{id} (token) |
| POST | `/api/wealth/liabilities` | wealth:write | token |  |  |
| DELETE | `/api/wealth/liabilities/{lid}` | wealth:write | token | `lid` |  |
| PUT | `/api/wealth/liabilities/{lid}` | wealth:write | token | `lid` |  |
| GET | `/api/wealth/mode` | wealth:read |  |  | ; PUT (token) {mode: INVESTOR\|TRADER} |
| PUT | `/api/wealth/mode` | wealth:write | token |  |  |
| GET | `/api/wealth/overview` | wealth:read |  |  | Investor-mode home: DNA, wealth, goals, allocation, performance, |
| GET | `/api/wealth/performance/benchmarks` | wealth:read |  |  | named benchmarks |
| GET | `/api/wealth/performance/ledger` | wealth:read |  | `portfolio`, `start`, `end`, `include_void`=False | ?portfolio&start&end&include_void |
| POST | `/api/wealth/performance/ledger` | wealth:write | token |  | (token) manual transaction |
| POST | `/api/wealth/performance/ledger/{txn_id}/void` | wealth:write | token | `txn_id` | (token) {reason} |
| GET | `/api/wealth/performance/portfolios` | wealth:read |  |  | ledger portfolios (PAPER / OMS / LIVE / MANUAL) |
| POST | `/api/wealth/performance/report` | wealth:write | token |  | (token) {portfolio, start, end, benchmark, options, store} |
| GET | `/api/wealth/performance/reports` | wealth:read |  | `limit`=50 | ; GET .../reports/{id} |
| GET | `/api/wealth/performance/reports/{rid}` | wealth:read |  | `rid` |  |
| GET | `/api/wealth/performance/reports/{rid}/export` | wealth:read |  | `rid`, `format`=csv |  |
| POST | `/api/wealth/performance/sync` | wealth:write | token |  | (token) import new ledger rows from the sources |
| GET | `/api/wealth/positions` | wealth:read |  |  | normalized positions (broker + manual + paper) |
| GET | `/api/wealth/rebalance/check` | wealth:read |  |  | drift vs target, triggers, verdict |
| POST | `/api/wealth/rebalance/plan` | wealth:write | token |  | (token) {mode: to_band\|to_target\|cash_flow, new_cash, store} |
| GET | `/api/wealth/rebalance/plans` | wealth:read |  | `limit`=50 | ; GET /api/wealth/rebalance/plans/{id} |
| GET | `/api/wealth/rebalance/plans/{pid}` | wealth:read |  | `pid` |  |
| POST | `/api/wealth/rebalance/plans/{pid}/decision` | wealth:write | token | `pid` |  |
| GET | `/api/wealth/signals` | wealth:read |  | `limit`=15 | today's ATIP signals checked against the investor's profile / plan |
| GET | `/api/wealth/snapshots` | wealth:read |  | `days`=730 | net-worth history ; POST (token) record today's |
| POST | `/api/wealth/snapshots` | wealth:write | token |  |  |
| GET | `/api/wealth/status` | wealth:read |  |  | which parts of the chain exist and how fresh they are |
| GET | `/api/wealth/suitability/{symbol}` | wealth:read |  | `symbol` | one symbol |
| GET | `/api/wealth/summary` | wealth:read |  | `positions`=False | net worth, by class / source / sector, concentration, |
| GET | `/api/wealth/uat/context` | wealth:read |  |  | is a UAT persona active (config wealth.uat_owner)? |

## `/api/webhooks`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| POST | `/api/webhooks/{source}` | public |  | `source` |  |

## `/api/zerodha`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/api/zerodha/login-url` | dashboard:read |  |  |  |
| GET | `/api/zerodha/status` | dashboard:read |  |  | \| /api/zerodha/login-url ; GET /zerodha/callback?request_token=           BR-02 |

## `/app`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/app` | dashboard:read |  |  |  |

## `/assistant`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/assistant` | dashboard:read |  |  | chat page |

## `/backtests`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/backtests` | dashboard:read |  |  |  |

## `/brokers`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/brokers` | dashboard:read |  |  | the page (dashboard/w37_page.py) |

## `/compliance`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/compliance` | dashboard:read |  |  | the page (dashboard/w38_page.py) |

## `/data-platform`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/data-platform` | dashboard:read |  |  | the page (dashboard/w35_page.py) |

## `/execution-lab`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/execution-lab` | dashboard:read |  |  | the page (dashboard/w34_page.py) |

## `/health`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/health` | public |  |  | /health/live  /health/ready  /health/{database\|broker\|data\|scheduler\|ml\|storage\|market_data\|wealth} |
| GET | `/health/live` | public |  |  |  |
| GET | `/health/ready` | public |  |  |  |
| GET | `/health/{component}` | dashboard:read |  | `component` |  |

## `/icon.svg`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/icon.svg` | public |  |  |  |

## `/login`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/login` | public |  |  |  |

## `/m`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/m` | dashboard:read |  |  | /api/mobile/summary                  mobile web app (installable PWA) |

## `/manifest.webmanifest`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/manifest.webmanifest` | public |  |  | /sw.js  /icon.svg PWA shell |

## `/market`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/market` | dashboard:read |  |  | the page |

## `/market-pulse`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/market-pulse` | dashboard:read |  |  | global cues, FII flows, positioning, order book, your orders |

## `/ml`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/ml` | dashboard:read |  |  | page |

## `/options-builder`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/options-builder` | dashboard:read |  |  | the strategy builder page |

## `/privacy`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/privacy` | public |  |  | /api/privacy/policy            the privacy notice (DRAFT until legally approved) |

## `/quant`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/quant` | dashboard:read |  |  | page |

## `/research`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/research` | dashboard:read |  |  | the research page (dashboard/w39_page.py) |

## `/screener`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/screener` | dashboard:read |  |  | the stock screener page (fundamental + technical) |

## `/signals`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/signals` | dashboard:read |  |  | technical signals page (today, track record) |

## `/strategies`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/strategies` | dashboard:read |  |  |  |

## `/strategy-builder`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/strategy-builder` | dashboard:read |  |  | builder page |

## `/sw.js`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/sw.js` | public |  |  |  |

## `/trading`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/trading` | dashboard:read |  |  | page |

## `/wealth`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/wealth` | dashboard:read |  |  | page |

## `/zerodha`

| Method | Path | Permission | Token | Parameters | Summary |
|---|---|---|---|---|---|
| GET | `/zerodha/callback` | dashboard:read |  | `request_token`, `status` |  |
