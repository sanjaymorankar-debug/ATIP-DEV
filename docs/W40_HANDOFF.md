# W40 — What remained buildable after W39b: handoff

> **2026-10-09 plan change:** PostgreSQL is now ATIP's final server database and MySQL / MariaDB is no longer supported -- the MySQL backend and its tools were removed (see `docs/POSTGRESQL_MIGRATION.md`). MySQL references below are history.

**Branch:** `claude/wizardly-curie-fbjeoa` (PR sanjaymorankar-debug/ATIP-DEV#9), based on master `d01705e`: W39b (#5) plus the MySQL runtime (#8).

**Date:** 2026-10-09

**Status:**
- Developed, with automated tests.
- Independent QA and owner UAT are pending.
- **Deploy:** `docs/W40_DEPLOY_RUNBOOK.md`.

**Scope.** After W39b the analysis plan (`docs/ANALYSIS_TOOLS_AND_SIGNALS_PLAN_2026-10.md`) was complete, except for the items that need a licensed feed or your go-ahead. W40 builds what was still buildable without your input. It comes from three sources:
- the gap analysis, §4 item 5 (charts) and item 9 (a factor risk model and meta-labelling);
- the tracker's next actions: BT-04, BT-14, BT-17, QR-05 / QR-06, ENT-15, PERF-001-05;
- the limits stated by the W39b builders: the 75-minute column, MCP tools, mutual fund SIP details.

Tax P&L stays out (your Scope Exclusions). Live execution and consensus estimates stay blocked.

## Decisions taken (your instruction: take the decisions, report them here)

| Item | Decision | How to reverse |
|---|---|---|
| Total-return benchmark (PERF-001-05) | NSE's TRI feed is not reachable from ATIP, so the series is **estimated**. The price index is exact. Its dividend points come from the top 50 stored stocks by market cap, picked monthly and weighted daily by their previous-close caps; dividends come from `corporate_actions`, split-adjusted. It is labelled as an estimate everywhere. | Pick the `nifty50` benchmark instead of `nifty50tr` |
| Futures legs in backtests | When no futures history is stored, the leg is **refused** with an event and a bias warning. It is never priced off spot: with no data there is no lot size, basis or expiry, and the stock may not have been in F&O then. | — |
| Paper futures roll | `futures.auto_roll` ships **off**. When on, it rolls 2 sessions before expiry. | One setting |
| Option overlays | **PAPER only**: LIVE is refused by the risk engine, the order system and the adapter.<br>Margin is an approximation, not SPAN: long options 0; defined-risk spreads their max loss less the debit; 15 % of notional per naked short leg.<br>Naked short calls are refused unless a definition explicitly allows them.<br>Each leg fills at the chain mid ± 50 bp, all-or-nothing.<br>`options.enabled` stays **off**. | One setting |
| Meta-labelling | Ships **off**. A version is ADOPTABLE only with 200+ out-of-fold signals, 30+ kept and 30+ rejected, and a t-statistic ≥ 2 on kept vs all. The registry refuses to activate anything else. The bet size is reported, never used to size an order. | `meta_label.enabled` |
| Factor risk model | The nightly job ships **on**. It is read-only research and needs 30+ stocks with filed share counts.<br>Its benchmark is a cap-weighted Nifty 50 proxy (the 50 largest by full cap; no free-float or index weights are stored), with the index itself as fallback. | `risk_model.enabled` |
| Stochastic dominance | The reference distribution is the **random** selection, every trial in every combination, as in Bailey et al.<br>"Dominates" requires being strictly better somewhere, so a selection no better than random never dominates. | — |
| Bayesian search | A Tree-structured Parzen Estimator (TPE) in numpy (no new dependency), after a seeded random start that equals the random method's first draws. | Use another `method` |
| Mutual fund SIP | Stamp duty (0.005 %) and units rounded down to 3 decimals are **on** by default, as AMCs allot. The exit load is off.<br>Volatility is annualised by the NAVs actually observed per year, not a fixed 252. Liquid funds publish a NAV every calendar day. | `stamp_duty=0`, `round_units=0` per request |
| 75-minute rating | The `/signals` column shows the 20:30 snapshot. An on-demand endpoint gives the current value from the stored 15-minute bars. | — |

## Built

| Area | What | Tests |
|---|---|---|
| PERF-001-05 | `data/total_return.py`: the estimated Nifty 50 TRI.<br>Table `index_total_return`, nightly 21:10, `GET /api/data/total-return`.<br>The wealth benchmark `nifty50tr` is part of the report's audited inputs. | `tests/test_w40_total_return.py` |
| DB-20 / gap 4.5 | Stock chart:<br>• RSI 14 and MACD 12-26-9 panels, matching `research/technicals.py` to 1e-9;<br>• an intraday chart (1D / 5D on 15-minute bars, session VWAP, previous close, intraday scan marks);<br>• `GET /api/stock/{symbol}/intraday`. | `tests/test_w40_stock_chart.py` (incl. Playwright) |
| W39B-TF75 | The 75m column on `/signals`; `GET /api/stock/{symbol}/rating-75` on demand. | `tests/test_w39b_tf75_signals.py` |
| W39-MCP | 20 read-only tools (8 new):<br>• `dvm`, `earnings_surprise`;<br>• `mf_search`, `mf_analytics`, `mf_sip`, `mf_compare`;<br>• `backtest_validation`, `chart_marks`. | `tests/test_w39_mcp.py` |
| W39B-MFA | Step-up SIP, stamp duty and unit rounding, an optional exit load, and annualisation by the observed NAVs. | `tests/test_w39b_mf_analytics.py` |
| BT-14 | First / second-order stochastic dominance in `pbo()` and CPCV, and CPCV on the event-driven engine. | `tests/test_w39b_cpcv_dominance.py` |
| BT-17 | The event engine reports the adapter's skipped decisions like W2. | `tests/test_w39b_event_driven_skips.py` |
| BT-04 | `optimize(method="bayes")` (TPE). In the benchmark it reached the top region in 17 trials on average, vs 32 for random. | `tests/test_w39b_bayes_optimize.py` |
| QR-05 / QR-06 | SHORT / COVER in the W2 backtest as near-month stock futures:<br>• whole lots, margin, daily variation margin;<br>• an NSE F&O cost model;<br>• a roll N sessions before expiry, or settlement at expiry.<br>The paper futures book can auto-roll. BUY / SELL-only runs are unchanged: 11 new pins plus the existing ones. | `tests/test_w40_futures_backtest.py` |
| ENT-15 | `option_overlay` strategies emit multi-leg OPTION intents:<br>• covered call, protective put, spreads, iron condor and more;<br>• strikes by delta or % OTM, monthly expiry;<br>• sized by the risk engine;<br>• filled in the paper options book, marked daily, exited by rule, settled at expiry.<br>Also a dry run, and a replay on stored daily option prices. | `tests/test_w40_option_intents.py`, `docs/W40_OPTION_INTENTS_HANDOFF.md` |
| Gap 4.9 | `quant/risk_model.py`, a Barra-style model:<br>• 8 styles plus NSE industries;<br>• constrained √cap-weighted regression with robust t statistics;<br>• EWMA / Newey–West covariance;<br>• shrunk specific risk.<br>It decomposes the PAPER and LIVE books, runs a bias test, has cards on `/trading` and `/quant`, and runs nightly at 21:45. | `tests/test_w40_risk_model.py` |
| Gap 4.9 | `ml/meta_label.py`, meta-labelling:<br>• triple-barrier labels from the engine's own outcome rule;<br>• 37 point-in-time features, uniqueness weights, purged k-fold;<br>• out-of-fold expectancy of the kept signals and an adoption gate;<br>• a /signals column when adopted. | `tests/test_w40_meta_label.py` |

## Bugs fixed

- **Event-driven close-out (BT-17).** At the end of the window the engine updated the last day's cash and equity, but not its daily return. Chained daily returns therefore overstated final equity by the close-out costs.
  - Three event-driven pins were re-pinned, plus one from W40's futures tests, each with a diff proving exactly one value changed: the last `daily_return`.
- **Code strategies.** SHORT / COVER from a W2 code strategy became a BUY in the live adapter. They are now decisions.
- **`db/dialect_scan.py`** scanned `.claude/` (local agent worktrees), so #8's reserved-word test failed on stale copies of the code.

## Merging the branches

Seven feature branches were built in parallel and merged here. Conflicts were confined to registries (schema, privacy, scoping, scheduler, the user guide) and to two shared pages; each was resolved by keeping both sides. Two tests had to change where two features met:
- The futures pins vs the event-engine fix: one pin was re-pinned, with the one-leaf diff.
- The skipped-decision test vs futures. W2 now simulates SHORT as a futures leg, so it no longer lists SHORT as "not simulated". Each engine's own report is now asserted.

The chart's browser test was also taught the new `/rating-75` request.

## Limits stated by the builders

- **Total return:** the dividend part is an estimate. Compare it once against NSE's published TRI.
- **Factor risk model:**
  - The universe and industries are today's (survivorship).
  - There is no free-float data.
  - Prices are unadjusted, so moves above 35 % are left out of that day's regression.
  - Book bias uses today's weights.
- **Meta-labelling:**
  - Daily bars can't order a stop and a target hit on the same bar; it counts as the stop.
  - Entry is assumed at the close.
  - It needs a few hundred closed signals before any verdict.
- **Futures backtest:**
  - Legs fill at the futures close, cash legs at the open.
  - Futures legs have no protective stops.
  - Margin is fixed at entry.
  - Settlement is in cash, though NSE stock futures settle physically.
  - Rolls need `fo_contract_daily` history.
- **Option overlays:**
  - One position per strategy and underlying.
  - All legs on one expiry, no adjustments.
  - Exits are checked once a day.
  - The replay skips the risk-engine caps.
- **Intraday chart:** no date picker, no live refresh.
- **Bayes search:** TPE models each parameter independently.

## Blocked (needs you)

Unchanged from W39b (`docs/W39_TRACKER_RECONCILIATION_HANDOFF.md`, Blocked), plus one item:

| Item | What is needed |
|---|---|
| **GitHub Actions** | Since 2026-10-09 every CI job fails before any step runs (no runner assigned), on this PR's pushes. The same workflows were green on master on 2026-10-08. Check Settings → Billing and plans for Actions minutes or the spending limit. Until then the full suite is run locally. |
| Deploy | Run `docs/W40_DEPLOY_RUNBOOK.md` on the Mac. |
| Live execution | Your go-ahead (option overlays, futures and GTT stay PAPER). |
| Dhan Data API | Needed for the intraday chart, 75-minute rating, 20-level depth / OFI and order-book pressure. |
| Others | BR-08 sandbox token, ENT-04 Razorpay account, ENT-07 / 08 exposure, ENT-14 legal, ENT-16 PostgreSQL + host, consensus estimates, UAT-001, SE-05 evidence. |

## Test results

**`pytest tests/`** with all seven branches merged, on the tip: **1,556 passed, 37 skipped, 0 failures** (the coverage run that generates the tracker).
- **Baseline:** 1,349 on the first W40 commit (`d01705e` plus the total-return work); 563 before W39.
- **Merge:** one scheduler test failed first, because it counted every job in the module. It now checks its own job.
- **CI:** GitHub Actions could not run (see Blocked).

**Tracker:** 333 rows, regenerated with that coverage.
- 126 COMPLETED
- 197 IMPLEMENTED BUT NOT VERIFIED
- 9 BLOCKED
- 1 IN PROGRESS
