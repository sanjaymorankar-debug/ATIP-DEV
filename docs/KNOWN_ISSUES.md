# ATIP known issues and deferred work (development TODO)

This is the running list of items deliberately deferred during W3/W4 development.

- **Defect detail** (wave, feature, impact) is kept in `docs/KNOWN_DEFECTS.md`. Both files use the same IDs.
- **Testing:** nothing here has been functionally tested. ChatGPT performs the independent testing.
- **What was fixed during W3/W4:** no earlier defect was fixed, because none met the fix-now criteria (blocks startup, blocks W3/W4 structurally, risks data corruption, or could allow unintended live trading).

Last updated: 2026-09-26 (W20 wealth track).

## Deferred defects

| Issue ID | Description | Affected Module | Severity | Current Status | Deferred To |
|---|---|---|---|---|---|
| CG-TBD | ChatGPT's W1/W2 defect list. It has not been provided to Claude and must be added here when received | W1/W2 | TBD | OPEN | Defect-resolution phase |
| KD-001 | The Anthropic API key in `.env` returns 401 | AI commentary / news classification | Medium | OPEN | Defect-resolution phase |
| KD-002 | Business Standard, Business Standard Companies and Financial Express RSS feeds return invalid XML; MoneyControl returns 0 items | data/news.py | Low | OPEN | Defect-resolution phase |
| KD-003 | News rows stored before the timezone fix are still in UTC | news_articles | Low | OPEN | Defect-resolution phase |
| KD-004 | The pre-market run stalls around 07:00. It is mitigated, but the root cause is unknown | pipeline/scheduler.py | Medium | OPEN (mitigated) | Defect-resolution phase |
| KD-005 | Loss limits fail closed without `pnl_daily` history. This applies to the W1 limits **and** the W4 `daily_loss_limit_pct` / `portfolio_drawdown_limit_pct` checks | orders/risk.py, execution/risk_engine.py | Low | OPEN (by design, to confirm) | Defect-resolution phase |
| KD-006 | The W2 stitched OOS equity curve starts at the first test window's close | backtest/ | Low | OPEN | Defect-resolution phase |

## W11–W20 wealth track: open items

| Issue ID | Description | Affected Module | Severity | Current Status | Deferred To |
|---|---|---|---|---|---|
| WLT-1 | The live broker account has no fill history in ATIP. LIVE ledger sells are inferred at that session's close (APPROX_CLOSE); buys are exact from the average-cost identity | wealth/perf/ledger.py | Medium | OPEN (by data) | Owner enters contract-note trades / broker trade-book import |
| WLT-2 | International prices are manual; gold / silver are spot × USD/INR × assumed domestic premium (9%), not IBJA / MCX | wealth/holdings.py | Low | OPEN | Data source wave |
| WLT-3 | DNA-1.0, AAL-1.0 (model portfolios, CMA, signal weights), scenario shocks: ATIP methodology, not validated against outcomes | wealth/ | Medium | OPEN | W19 methodology review (SEBI RIA) |
| WLT-4 | `fii_score` 0 on days FII data is pending reads as maximum outflow in the tactical signal (weight 0.05) | wealth/allocation.py | Low | OPEN | Signal hygiene follow-up |
| WLT-5 | No dividend history source: dividends are manual ledger entries | wealth/perf | Low | OPEN | Data source wave |
| WLT-6 | W18 suite written and compiled, not run by development | tests/test_wealth_*.py | — | PENDING | ChatGPT QA |
| WLT-7 | Advisor narration (opt-in) sends the evidence pack to Anthropic | wealth/advisor.py | — (accepted, opt-in) | BY DESIGN | Owner decision |
| WLT-8 | Excluded by the brief: NPS, FDs, tax, insurance, mutual funds (refused explicitly; rebalancing is tax-neutral) | wealth/assets.py | — | OUT OF SCOPE | Future brief |

## W3/W4 items not built (remaining work)

| Issue ID | Description | Affected Module | Severity | Current Status | Deferred To |
|---|---|---|---|---|---|
| W4-R1 | Live execution: `DhanBrokerAdapter` refuses every call. There is no live order path in W4 | execution/adapters.py | — (by design) | NOT BUILT | Later wave, only with explicit authorisation |
| W4-R2 | Order **modify** is not implemented; cancel is | execution/order_manager.py | Low | FIXED in W29 (EX-08) | — |
| W4-R3 | (FIXED in W29, EX-02) Stop-loss and stop-limit order types are not implemented. The paper broker supports MARKET and LIMIT only; intents carry stop and target prices, but no bracket is placed | execution/ | Medium | NOT BUILT | W4 follow-up (reuse orders/rules.py brackets) |
| W4-R4 | (FIXED in W29: execution/paper_matching.py) Resting paper LIMIT orders are never filled later, because the paper broker has no matching loop. Such an order stays ACKNOWLEDGED until cancelled | orders/paper.py | Low | OPEN | W4 follow-up |
| W4-R5 | These risk controls are not built: volatility limits (RK-09), liquidity limits (RK-10), correlation risk (RK-11), VaR/ES (RK-12) and emergency exit (RK-16) | execution/risk_engine.py | Medium | NOT BUILT | W4 follow-up / W6 |
| W4-R6 | Broker reconciliation (OMS vs broker book) is not built. The paper broker is the book of record | execution/ | Medium | FIXED in W29 (BR-05) | W4 follow-up |
| W4-R7 | Per-strategy attribution comes only from W4 fills. Positions opened outside W4 (order rules, aggressive exit, manual paper orders) belong to no strategy, but they do count in portfolio, sector and position limits | execution/positions.py | Low | OPEN (by design) | — |
| W4-R8 | The sector map is read from the cached Nifty 500 list. When the cache is missing, sector exposure cannot be measured and every BUY is REJECTED (fail closed) | execution/positions.py | Low | OPEN | Defect-resolution phase if it occurs |
| W4-R9 | The post-market cycle runs after the close. With `paper_fill_price` "live", an auto-executed order fills at the Dhan LTP at that time (the day's last price), not the next open | execution/pipeline.py | Low | OPEN (by design) | Scheduling decision for the owner |
| W3-L1..L6 | W3 limitations (see KNOWN_DEFECTS.md section 3). L1 (intents never authorised) is now superseded by the W4 risk engine | strategy_engine/ | — | See KNOWN_DEFECTS.md | — |

## W5 items not built / known limits

| Issue ID | Description | Affected Module | Severity | Current Status | Deferred To |
|---|---|---|---|---|---|
| W5-R1 | No model has been trained, validated or activated. Everything ML is framework only, so strategies `ml_direction` and `atip_ml_blend` get no ML values | ml/ | — (by design) | NOT DONE | Owner + ChatGPT validation |
| W5-R2 | The scikit-learn / XGBoost / LightGBM adapters need packages that are not installed (nothing was installed). The numpy logistic and ridge models work without them | ml/models.py | Low | OPEN | Owner decision |
| W5-R3 | ATIP score history starts 2026-07-27 and FII/DII flows 2026-07-28, so `atip_core` datasets are short. `atip_technical` covers the full price history | ml/feature_registry.py | Medium | OPEN (data) | W1 backfill / later |
| W5-R4 | Dataset building is pure Python over EvalEnv and slow at full scale (all symbols × all sessions). Use `sampling` | ml/dataset.py | Low | OPEN | W6 (vectorised features) |
| W5-R5 | Tree models get global importances only; there is no SHAP-style per-row attribution | ml/explain.py | Low | NOT BUILT | W6 |
| W5-R6 | Monitoring persists drift and realised metrics but raises no alerts and has no dashboard charts. Realised metrics for `market_regime` models are not computed | ml/monitoring.py | Low | NOT BUILT | W6 / W8 |
| W5-R7 | The assistant is templated (no LLM). An LLM layer needs a working Anthropic key (KD-001) | ml/assistant.py | Low | PARTIAL | Later |
| W5-R8 | The universe for ML datasets is today's constituents (survivorship bias, as in W2) | ml/dataset.py | Medium | OPEN | W6 |
| W5-R9 | Sector features are built as ML-only cross-sectional features (ml/context_features.py). They are not W3 strategy features, because a single-symbol FeatureContext cannot see other symbols | ml/context_features.py | Low | PARTIAL (by design) | W6 if strategies need them |
| W5-R10 | Global market history starts 2026-07-25 (25 dates), so `atip_extended` datasets are short. The index series for Bank Nifty / Midcap / Smallcap go back to ~2025-01 | ml/context_features.py | Medium | OPEN (data) | Later |
| W5-R11 | The industry map comes from the cached Nifty 500 list. If the cache is missing, sector features are None (and the W4 sector limit fails closed, W4-R8) | ml/context_features.py | Low | OPEN | — |

## W6 items not built / known limits

| Issue ID | Description | Module | Severity | Status | Deferred To |
|---|---|---|---|---|---|
| W6-R1 | `fundamental_data` is empty, so value, quality and growth factors, and the vqm / vqmg composites, produce no values; `vqm_multi_factor` has no candidates | quant/factors.py | Medium | OPEN (data) | Fundamentals ingestion |
| W6-R2 | Shares outstanding are not collected, so market cap, size, turnover, FCF yield and price-to-sales are DATA_PENDING | quant/factors.py | Medium | OPEN (data) | Data platform |
| W6-R3 | No shortable instrument in the PAPER cash book. Pairs short legs and long/short books produce SELL decisions without intents; neutral strategies hold back their longs | strategy_engine/kinds.py | Medium | OPEN (by design) | Futures / SLB integration (W4 follow-up) |
| W6-R4 | No futures or options data, so derivatives and options analytics are schema plus maths only; implied vs realised volatility is unavailable | quant/derivatives.py | Medium | BLOCKED (data) | Derivatives data source |
| W6-R5 | No tick, bid/ask or depth data (`live_ticks` is empty), so spread, imbalance and depth features are pending | quant/microstructure.py | Low | BLOCKED (data) | Tick feed |
| W6-R6 | Corporate-action events are known only at ex_date (announcement dates not stored). Earnings, dividend, macro and index-change events have no source | quant/events.py | Low | OPEN (data) | Event data sources |
| W6-R7 | Factor computation is pure Python per symbol and date; research over many dates needs a back-fill run | quant/engine.py | Low | OPEN | Performance work |
| W6-R8 | Portfolio-strategy HOLD does not rebalance held weights (no ADD/REDUCE toward target): the equity-relative current weight is not known inside the evaluator | strategy_engine/kinds.py | Low | OPEN | W6 follow-up |

## W7 items not built / known limits

| Issue ID | Description | Module | Severity | Status | Deferred To |
|---|---|---|---|---|---|
| W7-R1 | Internet exposure (ENT-07) was deliberately not done. The dashboard stays bound to 127.0.0.1; TLS, MFA, rate limiting and a security review are prerequisites | dashboard/security.py | — (by design) | BLOCKED | Owner decision + ENT-14 legal review |
| W7-R2 | No MFA — **addressed in W8**: TOTP MFA (enterprise/mfa.py); needs an encryption key (`python -m ops keygen`) | enterprise/mfa.py | Medium | DEVELOPED (W8, untested) | Independent testing |
| W7-R3 | Single paper book / broker account, so only the default tenant can read or trade execution, orders and portfolio | enterprise/authz.py | Medium | OPEN (by design) | Per-tenant books |
| W7-R4 | Tenant isolation for W3–W6 data is enforced at the API layer (middleware); module-level SQL is not tenant-scoped, so CLI / scheduled jobs see all tenants | enterprise/authz.py | Medium | OPEN | SQL-level scoping |
| W7-R5 | No e-mail: no verification, delivery or password-reset mail (admins hand out reset tokens) | enterprise/users.py, notifications.py | Low | NOT BUILT | Mail sender |
| W7-R6 | No payment gateway: prices NULL until set, invoices DRAFT only | enterprise/billing.py | — (by design) | NOT BUILT | Owner decision |
| W7-R7 | No per-user broker credential vault (ENT-06) | — | Medium | NOT BUILT | Design with the owner |
| W7-R8 | The middleware opens a DB connection per request when enabled. **W8:** get_connection no longer runs the migrations on every call (once per process per database), so a connection is cheap; there is still no pool | enterprise/authz.py; db/schema.py | Low | PARTLY ADDRESSED (W8) | Pool with DBS-05 |
| W7-R9 | Workspace reports are saved definitions only; there is no rendering / export | enterprise/workspace.py | Low | NOT BUILT | Reporting work |

## W8 items not built / known limits

| ID | Item | Where | Severity | Status | Next |
|---|---|---|---|---|---|
| W8-R1 | TLS is architecture only: ATIP stays on 127.0.0.1 (ENT-07 BLOCKED). HSTS is sent only when `ops.tls_enabled` is true | docs/SECURITY_ARCHITECTURE.md | — (by design) | NOT DEPLOYED | With ENT-07 |
| W8-R2 | Backups stay on the same disk as the database; no off-machine copy and no backup encryption | ops/backup.py | Medium | OPEN | Off-site target (owner decision) |
| W8-R3 | No encryption key exists until the owner runs `python -m ops keygen`; until then MFA enrollment is refused and field encryption is unavailable (nothing falls back to plaintext) | ops/crypto.py | Low | OWNER ACTION | Generate + back up the key |
| W8-R4 | Legacy secrets (Dhan client id / token, Alpha Vantage, Telegram) are still read from config.json when not in the environment / .env / atip_data/secrets; startup warns | ops/secrets.py | Medium | OWNER ACTION | Move them (the owner handles credentials) |
| W8-R5 | Rate limiting is in-process (per ATIP process) and off by default | ops/http.py | Low | OPEN | Enable for production; shared store at scale |
| W8-R6 | Metrics are in-process counters (reset on restart); no Prometheus server or dashboard is deployed | ops/metrics.py | Low | OPEN | Scraper when hosted |
| W8-R7 | Resilience primitives (retry, breaker, timeout) are used by webhook delivery only; existing data-source clients keep their own error handling | ops/resilience.py | Low | OPEN | Wrap data sources gradually |
| W8-R8 | The /api/v1 alias maps to the current API; there is no separate frozen v1 contract / OpenAPI reference yet | ops/http.py | Low | OPEN | API-05 |
| W8-R9 | Existing W1–W7 routes keep their `{"error": "..."}` bodies; the new envelope covers new failures and unhandled exceptions | ops/errors.py | Low | OPEN (compatibility) | Migrate per route |
| W8-R10 | enterprise_audit rows written before W8 have no hash; the chain starts at the first W8 row. Hash chain is process-locked (single writer process) | enterprise/audit.py | Low | OPEN | — |
| W8-R11 | Job locks use pid liveness; on Windows a reused pid could keep a stale lock until it expires (6 h) | ops/jobs.py | Low | OPEN | — |
| W8-R12 | pip-audit is not installed, so the dependency audit in `python -m ops scan` reports NOT RUN | ops/scan.py | Low | OPEN | Install in a dev environment |
| W8-R13 | W8 is merged to MAIN but the running ATIP process loads it only after a restart (bare `python main.py`) | — | — | PENDING RESTART | Owner decision |

## W9 release classification (ATIP-W9-RC2)

- **P0** blocks release, or is a serious trading / security / data risk.
- **P1** is an important production issue.
- **P2** is non-blocking.
- **P3** is a future enhancement.

Only P0 blocks the release. **Open P0 items: none.**

| ID | Priority | Issue | Status |
|---|---|---|---|
| W9-T1 | P0 | W1 real orders (order rules, aggressive strategy) could reach Dhan with `broker_env: LIVE` + confirm without consulting `LIVE_TRADING_ENABLED` (`execution.live_trading_enabled`); only the owner's config could open it (broker_env is PAPER) | **FIXED in W9**: master switch in `orders/broker._place_order` (needs live_trading_enabled + environment production) |
| W9-S1 | P1 | W7 workspace upserts (`enterprise/workspace.py` save_watchlist / save_alert / save_report) accepted a client-supplied id and could overwrite another user's or tenant's row | **FIXED in W9** (ownership check). Only reachable with the enterprise layer enabled (it is off in production) |
| W9-S2 | P1 | Session / refresh cookies had no `Secure` flag | **FIXED in W9**: Secure when `ops.tls_enabled` (plain-HTTP localhost unchanged) |
| W9-D1 | P1 | `cryptography` (used by ops/crypto.py) was installed but not declared; no dependency lock | **FIXED in W9**: requirements.txt + requirements.lock.txt |
| W9-Q1 | P1 | W3–W9 have not been independently tested; CI (`.github/workflows/tests.yml`) has not run on W2–W9 because nothing has been pushed since W1 (master 50+ commits ahead of origin) | OPEN: ChatGPT QA pending; pushing is the owner's decision |
| W9-B1 | P1 | No W8 scheduled backup has run yet (the first is due 19:15 on 2026-09-26; `atip_data/backups` does not exist yet). The deploy script takes its own VERIFIED pre-release backup | OPEN (resolves at the first 19:15 run or the first deploy) |
| W8-R2 | P1 | Backups are on the same disk as the database; no off-machine copy | OPEN (owner decision) |
| W8-R4 | P1 | Seven credentials are still plaintext in `atip_data/config.json` (gitignored; never committed; the git history scan found only truncated placeholders) | OPEN (owner action; development never handles credentials) |
| W9-J1 | P2 | `dhan_portfolio` job FAILED 2026-09-25 10:31 (later runs succeeded; not investigated); `dhan_15min_bars` fails daily (owner: ignore) | OPEN; keeps `/health/scheduler` DEGRADED for 24 h |
| W9-C1 | P2 | The production instance runs as environment `development` (single-user, enterprise off); `production` would refuse to start without the enterprise layer, the encryption key and rate limiting | OPEN (by design; see PRODUCTION_CONFIGURATION.md) |
| W9-H1 | P2 | `/health` is DEGRADED, not READY, until the first verified backup exists and no job has failed in the last 24 h (storage / scheduler components) | OPEN (accurate reporting, not a fault) |
| W9-R1 | P2 | Deploy and rollback scripts are syntax-checked and dry-run only; an authorized deployment and a rollback have **not** been executed | OPEN (needs owner authorization) |
| W8-R5..R12 | P2 | In-process rate limit / metrics, no off-box metrics, resilience wrappers only on webhooks, no frozen v1 OpenAPI, legacy error bodies, pre-W8 audit rows unhashed, pid reuse on locks, pip-audit not installed | OPEN (see the W8 table) |
| W9-P1 | P3 | Enterprise-SaaS work (per-tenant books, vault, PostgreSQL tooling, notification channels, billing sandbox, privacy) was started under the earlier W9 prompt, then superseded by the release brief. It is parked unmerged on branch `w9-enterprise-saas` (`3aae821`), incomplete and untested | PARKED |
| W7-R5/R6/R9 | P3 | No e-mail sender, no payment gateway, no report rendering | OPEN |
| W9-M1 | P3 | `orders/broker.py` has an unused `PAPER` import (pyflakes); `atip.db.bak-before-w8-*` has `-wal` / `-shm` side files from its verification open | OPEN (cosmetic) |

## W27 data & scores: open items

| Issue ID | Description | Affected Module | Severity | Current Status | Deferred To |
|---|---|---|---|---|---|
| W27-1 | First full NSE fundamentals / ownership backfill not run (about an hour; run off-hours) | data/nse_filings.py, data/institutional.py | Medium | OPEN | Owner, before enabling the score switches |
| W27-2 | The bulk / block deal study contradicts INS BulkDeals' direction (net SELL deals outperform, 5d t=4.1) on 4 weeks of data | scores/engine.compute_ins, quant/deal_signal.py | Medium | OPEN (needs owner decision) | Re-check as history grows |
| W27-3 | Stock live feed never exercised in a live session (compiled only; off by default) | data/stock_feed.py | Low | OPEN | First enabled session |
| W27-4 | MSI Options now reads the real NIFTY PCR (was a constant 1.0); this changes MSI / ATIP scores on deploy without a switch | scores/engine.compute_msi | Low | BY DESIGN (defect fix) | — |
| W27-5 | No India 10Y G-sec yield, no per-stock daily MF flow, no IV / Greeks | data/markets.py, data/institutional.py, data/derivatives.py | Low | OPEN (no free source / later DP-08) | Data-source wave |
| W27-6 | Bank FS / SPI exclude D/E, coverage, ROCE by design and do not use GNPA / NNPA yet | data/nse_filings.py | Low | OPEN | Fundamentals follow-up |

## W28 strategy & AI: open items

| Issue ID | Description | Affected Module | Severity | Current Status | Deferred To |
|---|---|---|---|---|---|
| W28-1 | News AI never succeeded end to end: the .env Anthropic key returns 401 (KD-001); rule path in use | data/news_ai.py | Medium | OPEN (owner: key) | Owner |
| W28-2 | Intraday scan thresholds are first guesses | strategy/intraday_scan.py | Low | OPEN | Tune after a few sessions |
| W28-3 | SE-05 cannot complete without an ML model with demonstrated edge (none registered; W24 NO_EDGE) | ml/ai_strategy.py | Medium | BLOCKED (model) | ML research |
| W28-4 | Strategy performance / model monitoring panels are empty until strategies run in PAPER and ml.enabled | dashboard | Low | OPEN (by data) | — |

## W29 execution: open items

| Issue ID | Description | Affected Module | Severity | Current Status | Deferred To |
|---|---|---|---|---|---|
| W29-1 | Sandbox path verified only up to "credentials": no Dhan sandbox token | orders/sandbox_check.py | Medium | BLOCKED (owner: token) | Owner |
| W29-2 | audit.offbox_dir unset: audit exports stay on this machine | enterprise/audit_export.py | Medium | OPEN (owner) | Owner |
| W29-3 | Broker health / live P&L / stock feed not yet observed in a live session | execution/broker_health.py, portfolio/live_pnl.py | Low | OPEN | First live session |
| W29-4 | A paper LIMIT could fill worse than its limit (slippage after the limit check) | orders/paper.py | Medium | FIXED in W29 | — |
| W29-5 | Kite order placement not built (owner decision required) | portfolio/zerodha.py | — | NOT BUILT (by design) | Owner |

## Deferred testing items (for ChatGPT)

All W3 to W7 functionality. The test scenarios are listed in:
- `W3_STRATEGY_ENGINE_HANDOFF.md` section 9
- `W4_RISK_EXECUTION_HANDOFF.md` section 11
- `W5_AI_ML_HANDOFF.md` section 9
- `W6_ADVANCED_QUANT_HANDOFF.md` section 10
- `W7_ENTERPRISE_HANDOFF.md` section 9
- `W8_PRODUCTION_HARDENING_HANDOFF.md` section 9
- `W9_FINAL_RELEASE.md` (the QA handoff checklist)
