# W39: QA performance, load and browser tests (Wave 18, development side)

**Scope:** what development delivers for the owner's Wave 18 "Full QA / Security / Performance" gate:

- a load / latency tool;
- a CI performance budget;
- browser smoke tests;
- first automated tests for seven tracker features that had none.

Independent QA on the 70 MB production copy (W18 §3, W33 §2) is still to do; this document tells QA how to run the tools and what development measured.

| File | What it is |
|---|---|
| `tools/load_test.py` | Load / latency tool: read-only GETs, per-route p50 / p95 / p99 / max and error rate, checked against targets; exit 1 on a miss |
| `tests/test_w39_perf_budget.py` | Statistics unit tests, a short in-process load run (no 5xx, p95 < 3 s), the W33 alerts target, server mode over real HTTP, CLI exit codes |
| `tests/test_w39_ui_smoke.py` | Playwright / headless Chromium: `/`, the stock panel, every `/wealth` tab, `/trading`, `/baskets`, `/data-platform`, `/compliance`, `/m` |
| `tests/test_w39_qa_suite.py` | EX-01, OPS-02, QR-11, ML-05, MON-06, DB-17 and OPS-11 (see §5) |

## 1. Running the load test

The tool needs the standard library, plus `httpx` if it is installed (otherwise it falls back to `urllib`). It only ever sends GET requests.

**Against a running dashboard (server mode)**, for example a scratch install on a copy of production:

```
python main.py --dashboard                         # in another terminal
python tools/load_test.py --base-url http://127.0.0.1:8000 --requests 720 --concurrency 4
python tools/load_test.py --duration 60 --concurrency 8 --json load.json
```

- `{sym}` routes use `--symbol`, or else the first row of `/api/scores`.
- With enterprise mode on, pass a session cookie or an API key with `--header "Cookie: ..."` (repeatable).

**In-process (no server; what CI runs):**

```
python tools/load_test.py --in-process --requests 720 --concurrency 4
python tools/load_test.py --in-process --serve --duration 20 --concurrency 8
```

In-process mode builds a throwaway install in a temp folder and makes it the current directory. The real `atip_data/` (database, `config.json`, token, caches) is never read or written. The temp install has:

- the deterministic W18 market from `tests/_wealth_seed.py`: NIFTY50, ACME, GOLDBEES, LIQUIDBEES and the midcap index, 260 sessions;
- 50 extra synthetic stocks with scores (`--extra-symbols`);
- the order-rules table;
- a Nifty 500 cache file, as a production install keeps one.

**How in-process mode runs:**

- **Client:** by default it drives the FastAPI app through starlette's `TestClient`. `--serve` runs the app under uvicorn on a free local port and loads it over real HTTP.
- **Network:** outbound connections and DNS lookups are refused, so NSE, yfinance and broker paths fail at once.
- **Clean-up:** everything is restored afterwards, and the temp folder is removed unless you pass `--keep`.

**Options:**

| Option | What it does |
|---|---|
| `--requests N` | Total requests; the default is 200 when no duration is given |
| `--duration S` | Run for this many seconds; with `--requests` too, the run stops at whichever comes first |
| `--concurrency C` | Number of worker threads; default 4 |
| `--route PATH` | Repeatable; replaces the default route set |
| `--p95-ms`, `--max-error-rate` | Override the default targets |
| `--no-targets` | Report only |
| `--no-warmup` | Also record the first request of each route. By default, one unrecorded warm-up request per route keeps import and first-call costs out of the numbers |
| `--skip-functions` | Skip the W33 alerts benchmark |
| `--json FILE` | Also write the full report |

**Exit codes:**

- 0: every target met;
- 1: a target was missed (the misses are listed at the end of the output);
- 2: the run could not start (for example, the server was unreachable).

**Default route set** (36 routes):

- **Pages:** `/`, `/wealth`, `/trading`, `/baskets`, `/market`, `/strategies`, `/backtests`, `/ml`, `/quant`, `/data-platform`, `/compliance`, `/m`.
- **APIs:**
  - health: `/health`, `/health/ready`;
  - main dashboard: `/api/scores`, `/api/mh`, `/api/tod`, `/api/alerts`, `/api/portfolio`, `/api/news`;
  - orders: `/api/orders`, `/api/orders/pending`;
  - stock panel: `/api/stock/{sym}/history`;
  - wealth: `/api/wealth/overview`, `/api/wealth/summary`, `/api/wealth/goals`;
  - risk: `/api/risk/portfolio`, `/api/risk/limits`, `/api/risk/exposure`;
  - execution: `/api/execution/status`, `/api/oms/orders`, `/api/pnl/live`;
  - other: `/api/mobile/summary`, `/api/ops/status`, `/api/backtests`, `/api/strategies`.

**Left out on purpose** (their latency belongs to another system):

- `/api/live-quotes`: Dhan, during market hours;
- `/api/zerodha/*`;
- `/api/schemas/check-all`: calls the server back over HTTP on port 8000;
- `/api/platform/postgres`: connects to a database server;
- the enterprise `/api/account/*` and `/api/admin/*` routes: they return 503 while enterprise mode is off.

## 2. Targets and where they come from

| Target | Value | Source |
|---|---|---|
| Error rate per route (5xx or no response) | 0 | W18 §1 / W33 §1: a read never returns 5xx |
| p95 per route | ≤ 1,000 ms | Development's own budget. **No document sets a per-route HTTP target**: W33 §2 lists none |
| `/api/wealth/overview` p50 | ≤ 300 ms | W18 §3: "Overview (whole chain) 0.3 s", measured on the production copy |
| `/api/wealth/summary` p50 | ≤ 300 ms | W18 §3: "Wealth summary < 0.3 s" |
| `alerts_intraday`, 1,000 ACTIVE rules | < 2,000 ms | W33 §2: "< 2 s for 1,000 rules". Measured in-process: `enterprise/w32.evaluate_alerts_intraday` on 1,000 rules over 100 symbols with a full session of 1-minute `live_quotes` |
| `capital_usage` | < 200 ms | W33 §2, "at today's fill count". **Not measured here**: it needs the production fill count, so it stays on the QA production-copy checklist |

The CI test (`test_w39_perf_budget.py`) does not assert these targets for HTTP routes. It asserts no 5xx on any default route, every default route answering 200, and a deliberately generous ceiling (overall p95 and every route's max < 3 s), so a slow CI runner cannot flake it. The W33 alerts target is asserted as written (< 2 s).

## 3. Results measured by development

The runs used this container:

- 4 vCPU, Python 3.13;
- in-process mode on the seeded temp database (W18 market plus 50 stocks);
- network blocked;
- 2026-10-07.

A single dashboard process is bound by the GIL, so concurrency raises latency without raising throughput (about 90–110 requests per second whatever the concurrency).

| Run | Requests | Errors | p50 ms | p95 ms | p99 ms | max ms | req/s |
|---|---|---|---|---|---|---|---|
| TestClient, concurrency 1 | 720 | 0 | 6.0 | 32.2 | 71.4 | 89.5 | 105 |
| TestClient, concurrency 4 | 720 | 0 | 18.3 | 147.3 | 362.1 | 395.4 | 109 |
| uvicorn over HTTP, concurrency 4 | 720 | 0 | 26.8 | 144.9 | 185.1 | 206.2 | 90 |
| uvicorn over HTTP, concurrency 8, 20 s | 1,908 | 0 | 70.2 | 184.3 | 209.1 | 292.7 | 95 |

**Per route** (ms; TestClient at concurrency 1, then uvicorn at concurrency 8):

| Route | p50 (c1) | p95 (c1) | p50 (c8) | p95 (c8) |
|---|---|---|---|---|
| `/` | 68.3 | 81.6 | 162.4 | 191.0 |
| `/wealth` | 3.8 | 4.7 | 159.6 | 191.7 |
| `/trading` | 2.5 | 3.0 | 152.9 | 194.0 |
| `/api/scores` | 10.2 | 12.4 | 80.6 | 113.7 |
| `/api/stock/{sym}/history` | 9.8 | 11.5 | 92.3 | 117.5 |
| `/api/wealth/overview` | 20.7 | 28.2 | 60.4 | 82.2 |
| `/api/wealth/summary` | 10.2 | 12.3 | 59.3 | 82.3 |
| `/api/risk/portfolio` | 6.3 | 6.8 | 86.4 | 142.7 |
| `/api/ops/status` | 32.8 | 40.1 | 181.1 | 271.2 |
| `/health` | 29.0 | 33.9 | 147.6 | 189.5 |

**Against the targets:**

- Every target was met in all four runs: 0 errors, the worst route p95 was 393 ms (`/` with 4 TestClient threads), and wealth overview and summary had p50 ≤ 75 ms.
- `alerts_intraday` with 1,000 rules took 14–19 ms, against the < 2,000 ms target.
- The CI budget run (72 requests, concurrency 2) gave an overall p95 of about 110 ms, against the 3,000 ms ceiling.

These are numbers on a small seeded database. The production-copy run that W18 and W33 ask for is still QA's, using server mode against a scratch install on the copy.

### Finding P-1: wealth reads retry the NSE download while the Nifty 500 cache is missing or stale

`data/index_constituents.get_symbol_industry_map()` calls `fetch_nifty500_symbols()` whenever the cache file is missing or older than 7 days. Each call makes a warm-up GET and a download GET to NSE (timeouts 12 s and 20 s). `/api/wealth/overview` reaches it three times (through `wealth/holdings._sectors`). Nothing remembers a failure, so every request retries.

| Measured in-process, NSE refused at once | Cache present | Cache missing |
|---|---|---|
| `/api/wealth/overview` | 14 ms, 0 outbound calls | 1,722 ms, 6 outbound calls |
| `/api/wealth/summary` | 7.6 ms | 597 ms, 2 outbound calls |
| `/api/wealth/positions` | 7.4 ms | 667 ms, 2 outbound calls |

- **When the connection is refused at once:** the delay is about 0.3 s per attempt, as in the table.
- **When NSE times out instead:** this is the case once the cache is more than a week old and NSE blocks or hangs. One overview can then wait up to 3 × (12 s + 20 s).

The load tool seeds a fresh cache, so its numbers reflect a healthy install. This finding is reported, not fixed: `data/` is outside this change.

**Suggested fix:** after a failed download, skip further attempts for some minutes (a negative cache) and serve the stale file meanwhile.

## 4. Browser smoke tests

```
python -m pytest tests/test_w39_ui_smoke.py -q
ATIP_CHROMIUM=/path/to/chrome python -m pytest tests/test_w39_ui_smoke.py -q
```

**Requirements:** `pip install playwright` and a Chromium binary. The test looks for one in this order:

1. `$ATIP_CHROMIUM`;
2. `/opt/pw-browsers/chromium-1194/chrome-linux/chrome`;
3. Playwright's own download.

Without Playwright or Chromium, the tests skip with the reason. CI installs neither (`.github/workflows/tests.yml`), so they skip there. Do not run `playwright install` on the owner's machine just for this.

**Set-up for each test:**

- the seeded install (the same seed as the load tool, with 10 extra stocks) served by uvicorn on a free port;
- the browser may only reach that server: every other request is aborted;
- the Python side runs with outbound network refused.

**What every page must show:**

- no uncaught JavaScript error (`pageerror`);
- no same-origin 5xx response;
- its key elements:

| Page | Asserted |
|---|---|
| `/` | 11 score rows (ACME plus 10). `openStock('ACME')` opens the panel with the chart, "52W high" and the last close ₹113.27, −0.05% (hand-derived from the seed formula). `svClose()` closes it |
| `/wealth` | 8 tabs (`ovw wlt gol aal rbl prf adv dna`). Clicking each shows only its panel, marks its button, and never ends in "could not load" |
| `/trading` | Mode PAPER, the risk-limits table, "the PAPER book holds nothing", the portfolio-risk "as of" line |
| `/baskets` | `broker_env: PAPER`, an empty basket list |
| `/data-platform`, `/compliance` | Their tab bars (every tab clicked) and first panels |
| `/m` | Market health 62 / BULL from the seed, and the scores date |

Measured run: 8 passed in about 21 s.

## 5. Tests for features that had none (`tests/test_w39_qa_suite.py`)

**EX-01: `orders/rules.py`**, end to end through the real `orders.broker` and `orders.paper` path:

- `broker_env` is PAPER; quotes come from a fixed table; the Dhan client is unreachable; the network is refused.
- **Validation:**
  - trigger and stop resolution (percent and amount);
  - direction defaults;
  - unknown symbol;
  - RISK sizing rules;
  - a refused rule is never stored.
- **Triggers:**
  - `check_triggers` only flags a confirmation rule (30-minute window, nothing placed) and expires it unconfirmed;
  - all four direction / role combinations.
- **Bracket with OCO legs, worked by hand:**
  - fill at 99 gives legs T1 103.95 ×5, T2 108.90 ×5, STOP 95.04 ×10;
  - T1 sells 5 and the stop shrinks to 5;
  - the stop sells 5 and cancels T2;
  - paper cash ends at 1,00,005, realized +5.
- **`settle_oco_group`:** with no group it does nothing; when the targets cover the whole position, the stop is closed.
- **Auto-exit:** refused on a 600 s old quote.
- **Confirmation:** refused at +1.41% drift, and placed when forced.
- **Trailing stops:**
  - percent, amount with jump steps, and the short side, each worked by hand;
  - a stop never widens;
  - `check_triggers` trails before it fires.

**OPS-02: post-market job.** `run_postmarket` runs for a seeded session: NIFTY50 plus a rising, a falling and a waving stock over 260 sessions, with delivery and FII/DII data.

- **What runs:**
  - the offline steps run for real: data quality, indicators, scoring, accuracy, PHS, signal log, outcomes, dashboard rebuild;
  - every download step is recorded as skipped (bhavcopy, bulk deals, F&O, corporate actions, Dhan history, index series, NSE closes, market series, news weights);
  - live quotes and the portfolio sync are stubbed.
- **What the test asserts:**
  - the steps run in order;
  - `ai_scores` has exactly the three tracked stocks, with every score in 0..100, a valid signal and ranks 1..3;
  - the riser outranks the faller;
  - the indicator and market-health rows are written;
  - the pipeline log shows SUCCESS;
  - a re-run rewrites rather than duplicates;
  - a session whose bars copy the previous one is refused (SKIPPED_STALE_EOD).
- **Limit of the slice:** the network steps themselves are not exercised. This is the largest offline slice.

**QR-11: `backtest/sensitivity.py`.**

- `_around` neighbourhoods, six cases by hand.
- A real dip-strategy sweep where the turnover filter never binds: a flat curve, stability 1.0, 3 persisted trials.
- A stubbed trial with sharpe = A[stop] × B[target]:
  - stability 0.8 (plateau) and 0.4 (knife edge);
  - `robust_share` 0.5;
  - 9 trials, or a 5 × 5 heat map with 25;
  - sign-flip rules.
- Refusals: test window, steps, unknown parameter.
- A crashed trial marks the run FAILED with the error.

**ML-05: `ml/presets.py`.**

- `regime()` on a NIFTY50 series with 40-session BULL / BEAR blocks:
  - 295 rows (300 sessions minus the 5-session horizon);
  - every label is Market Health's regime 5 sessions later;
  - a DRAFT `regime_gbm` with purpose `regime` and a TRAINED v1;
  - nothing ACTIVE;
  - a re-run gives v2;
  - a conflicting model id is refused.
- `bootstrap()`:
  - an unknown family is refused before anything is written;
  - `dir5_logistic`, the DEFAULT_LABEL spec, rows = 4 × dates;
  - `train=False` gives no version.

**MON-06: `main.py SafeRotatingFileHandler`.** Importing `main.py` creates `atip_data/` next to it and opens `atip.log`, so:

- the class is compiled from the source with `ast`, and the module wiring is checked by importing a copy of `main.py` from a temp folder in a subprocess;
- rotation, worked by hand: 2 backups, files ≤ maxBytes;
- a refused rename (Windows): the stdlib handler drops records 3–9, while the safe handler keeps all of them, waits 600 s, then rotates;
- the root logger gets 20 MB × 10 backups on `<folder>/atip_data/atip.log`.

**DB-17: risk dashboard.**

- `/trading` renders with its portfolio-risk section.
- Every data route the page's JavaScript calls (taken from the page itself; Zerodha excluded), plus the snapshots and history routes, returns 200 JSON on an empty database.
- The empty PAPER book reads "holds nothing".
- A bad book returns 400.

**OPS-11: `ops/rollback_drill.py` helpers.** No clone and no server are started.

- `_isolate`:
  - renames the production mutex only in a release that hard-codes it;
  - leaves today's `main.py` (which honours `ATIP_INSTANCE_NAME`) unchanged.
- `_db_into`:
  - with no VERIFIED backup, takes an online copy;
  - otherwise restores the newest VERIFIED backup over a leftover file, at the backup's point in time;
  - ignores a pruned backup and a backup whose file is missing.

### Source fix made because a test exposed it

**Where:** `orders/rules.py`, `execute_rule()`.

**The bug:** `_create_bracket_legs` documents that bracket percentages are measured "from the ACTUAL fill price, not the trigger price". `execute_rule` passed the trigger-hit price instead, and recorded it as `execution_price`.

**Failing case:**

- a rule triggered at 99 is confirmed (forced) when the paper broker fills at 100.40;
- the legs were STOP 95.04 / T1 103.95, which is 4% and 5% from a price nobody paid;
- they should be 96.38 / 105.42.

**The fix:** when the broker reports `averageTradedPrice`, that price becomes the execution price and the bracket anchor. The paper broker reports it; Dhan's `place_order` response does not, so LIVE behaviour is unchanged.

**Test:** `test_bracket_legs_are_measured_from_the_actual_fill`.

### Observation (not changed)

`backtest/sensitivity.py` drops a ±1 neighbour whose metric is None from both the stability ratio and the knife-edge test. A neighbour is None when it has fewer than `min_trades` trades. So a parameter whose next step stops the strategy trading is not flagged as a knife edge. Whether that should count as one is a methodology decision for the owner.

## 6. Running everything

```
python -m pytest tests/test_w39_perf_budget.py tests/test_w39_qa_suite.py tests/test_w39_ui_smoke.py -q
python tools/load_test.py --in-process
```

None of these touches the network, a broker or the real `atip_data/`.
