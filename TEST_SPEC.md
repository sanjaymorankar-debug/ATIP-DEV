# ATIP — test specification

ATIP, the AI Trading Intelligence Platform, is one Python process on the owner's Mac. It
holds the scheduler (07:00 / 15:30 / 16:45 IST), the FastAPI dashboard on
`127.0.0.1:8000`, the Dhan / yfinance data feeds, scoring, strategy, and the order and
execution paths (paper by default). Every 5 minutes `publish_snapshot.py` pushes a
rendered, read-only copy of the dashboard to `bkesari.com/ATIP/`. The database is
SQLite by default. PostgreSQL, hosted on Neon, is the opt-in server database
(`docs/POSTGRESQL_MIGRATION.md`).

This spec says what "tested" means for this repository: the commands, the scratch
databases, the checks that must never regress, and the report. It covers the Python
code, the PostgreSQL backend against a local server, a Neon stand-in, and the snapshot
publisher's dry run. It does not cover bkesari.com's viewer (bkesari-platform), real
brokers, or real Neon. How to set up a development machine is in `docs/DEV_SETUP.md`;
how the PostgreSQL backend works is in `docs/POSTGRESQL_MIGRATION.md`.

Testing agent: `.claude/agents/atip-tester.md`. It runs automatically after every code
change (the test gate, `.claude/hooks/test_gate.py`); run it by hand by asking Claude to
"run the atip-tester agent".

**The safety rule.** No test, smoke run or check may reach a real brokerage,
place / modify / cancel an order, use live credentials, or connect to a hosted database.
Everything below runs from a **scratch copy** of the working tree, which has no
`atip_data/` and no `.env`, in a virtualenv that has **only CI's packages**: no `dhanhq`,
`kiteconnect`, `anthropic` or `schedule`. On the Mac the project folder's own
`atip_data/` is the live one: the real `config.json` with the Dhan credentials, the live
database and the dashboard's cache. Its `.venv` has the real broker SDKs. Never run tests
from the project folder or with that `.venv`.

## 1. Scope

| Component | Path | Language / framework |
|---|---|---|
| Entry point, CLI, single-instance lock | `main.py` | Python 3.11+ (CI: 3.12) |
| Dashboard and JSON API (596 method + path pairs, 241 mutating) | `dashboard/`, `ops/http.py`, `enterprise/authz.py` | FastAPI + uvicorn |
| Scheduler and jobs | `pipeline/` | `schedule` (not installed for tests; a recorder stands in) |
| Market data | `data/`, `altdata/` | requests, yfinance, Dhan SDK (never installed for tests) |
| Scores, signals, research, ML | `scores/`, `research/`, `quant/`, `ml/`, `backtest/` | pandas, numpy, ta |
| Orders, risk, execution, brokers, strategies | `orders/`, `execution/`, `brokers/`, `strategy/`, `strategy_engine/`, `portfolio/` | Python |
| Database layer | `db/` (`schema.py`, `backend.py`, `postgres.py`, `sql/`), `ops/migrations.py`, `tools/sqlite_to_postgres.py`, `tools/schema_sql.py` | sqlite3, psycopg 3 |
| Ops, security, tenancy, wealth | `ops/`, `enterprise/`, `wealth/` | Python |
| Snapshot publisher | `publish_snapshot.py`, `publish_snapshot.sh` | requests |
| Deploy files | `Dockerfile`, `docker-compose.yml`, `deploy/`, `.github/workflows/` | Docker (CI only) |

Out of scope:
- **bkesari.com's `/ATIP/` viewer and its Content-Security-Policy.** Both live in bkesari-platform (`portal/server.js`, `ATIP_CSP`) and are tested there. This repo checks its own side: the snapshot never carries the write token, and a dry run uploads nothing.
- **Real Dhan, Zerodha, Telegram, Anthropic, Alpha Vantage and NSE.** These are never contacted. Their imports are guarded, and the canary run (S6.4) proves no test reaches them.
- **Real Neon.** Never contacted: there are no credentials here, and tests must never reach a hosted database. S7 uses a local stand-in.
- **The Windows launchers** (`*.bat`, `*.ps1`, `*.vbs`, `ATIP_TaskScheduler.xml`). There is no Windows machine.
- **The Docker image** (`docker.yml`). It is built in CI only; locally, S4.3 runs only where a Docker daemon exists.
- **`master` history.** Pre-existing failures are checked in a separate `git worktree`, never by switching branches.

## 2. Environment

### Tools

| | CI (`.github/workflows/tests.yml`) | Mac |
|---|---|---|
| OS | ubuntu-latest | macOS |
| Python | 3.12 | Homebrew `python@3.12` or `python@3.13` (README: never Apple's 3.9) |
| Packages | `pytest httpx pandas numpy ta fastapi uvicorn requests yfinance cryptography PyYAML "psycopg[binary]"` | the same list, in a separate test virtualenv |
| PostgreSQL | service `postgres:16`, user `postgres`, database `atip_test`, no TLS | Homebrew `postgresql@16` |
| Neon stand-in (S7) | none: those checks skip | PostgreSQL with TLS + `pgbouncer` in transaction mode |

### One-time setup (Mac)

```bash
REPO=/Users/agtci/Documents/Project_Documents/Projects/ATIP-dev       # the checkout
VENV="$HOME/.cache/atip-test-venv"                                     # never the project's .venv
"$(brew --prefix)/bin/python3.12" -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install pytest httpx pandas numpy ta fastapi uvicorn requests yfinance \
                        cryptography PyYAML "psycopg[binary]"
PY="$VENV/bin/python"

brew install postgresql@16 && brew services start postgresql@16
psql postgres -v ON_ERROR_STOP=1 <<'SQL'
DO $$BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='neon_superuser')
  THEN CREATE ROLE neon_superuser NOLOGIN CREATEDB CREATEROLE; END IF; END$$;
CREATE ROLE t_atip LOGIN CREATEDB PASSWORD '<a local password>';
GRANT neon_superuser TO t_atip;
CREATE DATABASE t_atip OWNER t_atip;
CREATE DATABASE t_atip_pool OWNER t_atip;
SQL
```

For S7 the server also needs TLS (`ssl = on`, with a self-signed `server.crt` / `server.key`
in the data directory) and a PgBouncer in front of it (`brew install pgbouncer`) with:
`pool_mode = transaction`, `max_prepared_statements = 1000`, `auth_type = scram-sha-256`,
`client_tls_sslmode = require` (plus its cert and key), `server_tls_sslmode = require`, and
`listen_port = 6432`. Without them, S7 is reported **SKIPPED: no TLS / pooler stand-in**
(see section 6 for when that still allows PASS).

### Scratch databases (local only, all thrown away)

| Name | Used by | What happens to it |
|---|---|---|
| `t_atip` | `ATIP_TEST_POSTGRES_URL`: S3, R1 | **DROPped and recreated** by `tests/test_postgres_backend.py` |
| `t_atip_schema` | made by `tests/test_schema_sql.py` (the URL's name + `_schema`) | **DROPped and recreated** |
| `t_atip_pool` | `ATIP_TEST_PG_TLS_URL` / `ATIP_TEST_PG_POOLER_URL`: S7, R1 | its `public` schema is **DROPped and recreated** |

The role `t_atip` is an ordinary login role: not a superuser, a member of the
`neon_superuser` stand-in, with CREATEDB (the live tests recreate their database), owning
all three databases. That is how the app's role works on Neon.

`t_atip_pool` is separate from `t_atip` on purpose. A PgBouncer with `auth_query` keeps
a superuser session open in every database it has served. A non-superuser cannot
`DROP DATABASE … WITH (FORCE)` past that session, and `tests/test_postgres_backend.py`
does exactly that to `t_atip`. So nothing pooled may ever connect to `t_atip`.

### Environment variables

| Variable | Value for a test run | Notes |
|---|---|---|
| `ATIP_TEST_POSTGRES_URL` | `postgresql://t_atip:<pw>@127.0.0.1:5432/t_atip` | S3, R1. Must be local: the tests DROP the database it names |
| `ATIP_TEST_PG_TLS_URL` | `postgresql://t_atip:<pw>@127.0.0.1:5432/t_atip_pool?sslmode=require&channel_binding=require` | S7, R1. The direct endpoint, TLS on |
| `ATIP_TEST_PG_POOLER_URL` | `postgresql://t_atip:<pw>@127.0.0.1:6432/t_atip_pool?sslmode=require` | S7, R1. PgBouncer 1.22 cannot do channel binding, so `sslmode=require` only |
| `TZ` | `UTC` | what CI runs on. The scheduler's IST comes from the launchers (CR-13) |
| `ATIP_DATABASE_URL`, `DATABASE_URL`, `ATIP_DB_PATH` | **unset** for R1 | `tests/conftest.py` also clears the first two and pins the SQLite runtime |
| `ATIP_DB_PATH` | `$SCRATCH/smoke-sqlite/atip.db` | S5 only |
| `ATIP_DATABASE_URL` | the TLS URL (`--init`), then the pooler URL (the app) | S7 smoke only, in the scratch copy |
| `ATIP_ROOT_PATH` | `/ATIP` | S5 base-path step only |
| `ATIP_ENV`, `ATIP_CHROMIUM` | unset | Python Playwright is not installed, so the 7 browser tests skip, as in CI |

Port: **8100** for every smoke server (S5, S7), one at a time. Nothing listens on 8000.

### Every run: make the scratch copy

```bash
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/atip-test.XXXXXX")"
mkdir -p "$SCRATCH/src"
tar -C "$REPO" --exclude=./.git --exclude=./atip_data --exclude=./.env --exclude=./.venv \
    --exclude=./.claude/worktrees --exclude='__pycache__' -cf - . | tar -C "$SCRATCH/src" -xf -
cd "$SCRATCH/src"          # every command below runs here unless it says $REPO
```

The copy is the working tree as it is, uncommitted changes included. It has no
`atip_data/` and no `.env`, so the code sees the defaults a fresh CI checkout sees.
Delete `$SCRATCH` at the end.

## 3. Standard suite

Run in order. Do not stop at a failure: finish the pass and report everything.

The commands, in `$SCRATCH/src`:

```bash
# S1 static
$PY -m compileall -q -x '/(\.git|\.venv|atip_data|node_modules)/' .                             # S1.1
$PY -c "import glob, yaml; [yaml.safe_load(open(f, encoding='utf-8')) for f in
  glob.glob('.github/workflows/*.yml') + ['docker-compose.yml', 'deploy/cloud/cloud-init.yaml']]"  # S1.2
$PY -m db.dialect_scan --details                                                                    # S1.3
$PY -c "from execution.config import live_gate; from orders.environment import broker_env
ok, why = live_gate(); assert not ok and broker_env() == 'PAPER', why
print('paper; live gate closed:', why)"                                                             # S1.4
# S2 unit
$PY -m strategy.backtest_aggressive > "$SCRATCH/replay1.txt" &&
$PY -m strategy.backtest_aggressive > "$SCRATCH/replay2.txt" &&
diff "$SCRATCH/replay1.txt" "$SCRATCH/replay2.txt"                                                  # S2.1
# S3 integration: the live PostgreSQL tests on their own
env -u ATIP_DATABASE_URL -u DATABASE_URL ATIP_TEST_POSTGRES_URL="$ATIP_TEST_POSTGRES_URL" \
  $PY -m pytest tests/test_postgres_backend.py tests/test_schema_sql.py -p no:cacheprovider -rs     # S3
# S4 build
$PY tools/schema_sql.py --check                                                                     # S4.1
$PY -m pip wheel . --no-deps -q -w "$SCRATCH/wheel" && $PY - "$SCRATCH/wheel" <<'PYEOF'           # S4.2
import glob, sys, zipfile
names = zipfile.ZipFile(glob.glob(sys.argv[1] + "/atip-0.2-*.whl")[0]).namelist()
assert not [n for n in names if "canary" in n or n.startswith(("dhanhq/", "kiteconnect/"))], "stand-ins in the wheel"
print(len(names), "files")
PYEOF
docker build -t atip:test .                                                                         # S4.3, where Docker exists
```

| # | Level | Passes when |
|---|---|---|
| S1.1 | static: every file compiles | exit 0 |
| S1.2 | static: workflows, compose and cloud-init parse | exit 0 |
| S1.3 | static: SQL that PostgreSQL cannot take | exactly `1 need a hand change`, and that one is `tools/repair_news_timezone.py` (`strftime()`, SQLite-only by design). A new one is a statement that raises `UnsupportedSQL` on PostgreSQL |
| S1.4 | static: paper by default | prints `paper; live gate closed: execution.mode is PAPER` |
| S2.1 | unit: CI's deterministic scenario replay, twice | both exit 0, outputs identical |
| S2.2 | unit: the pytest run | counted in R1 (not run twice) |
| S3 | integration: live PostgreSQL (`t_atip`, `t_atip_schema`) | 0 failed, **0 skipped** |
| S4.1 | build: generated `db/sql/*.sql` match the code | exit 0 |
| S4.2 | build: the package builds, without the test stand-ins | exit 0. Needs PyPI for the build backend; offline it is **SKIPPED** with that reason |
| S4.3 | build: the container image | CI runs it (`docker.yml`). Locally only where a Docker daemon exists, else **SKIPPED: no Docker daemon** |
| S5 | smoke on SQLite | every step below |
| S6 | security and hygiene | every step below |
| S7 | Neon parity | every step below |

### S5 — smoke on SQLite (port 8100)

```bash
mkdir -p "$SCRATCH/smoke-sqlite"; export ATIP_DB_PATH="$SCRATCH/smoke-sqlite/atip.db"
$PY main.py --init > "$SCRATCH/init1.log" 2>&1; $PY main.py --init > "$SCRATCH/init2.log" 2>&1
$PY main.py --dashboard --port 8100 > "$SCRATCH/dash-sqlite.log" 2>&1 & echo $! > "$SCRATCH/dash.pid"
for i in $(seq 1 60); do curl -fs -o /dev/null http://127.0.0.1:8100/health/live && break; sleep 1; done
```

| Step | Check | Passes when |
|---|---|---|
| S5.1 | `--init` twice | both exit 0; both print `Database created  : SQLite — …/smoke-sqlite/atip.db` and the same `Weights seeded : N configurations` line; the table count (`sqlite_master`) is the same after each run |
| S5.2 | `curl -s http://127.0.0.1:8100/health/live` / `/health/ready` | 200 / 200 with `"READY"` |
| S5.3 | `curl -s http://127.0.0.1:8100/` | 200, body contains `ATIP` and `const ATIP_TOKEN=` |
| S5.4 | `GET /api/scores`, `/api/mh`, `/api/orders`, `/api/pnl`, `/openapi.json` | 200 each |
| S5.5 | `curl -s -X POST http://127.0.0.1:8100/api/orders -H 'Content-Type: application/json' -d '{"symbol":"ACME"}'` (and with `-H 'X-ATIP-Token: wrong'`) | 401, body names `X-ATIP-Token` |
| S5.6 | `$PY tools/route_sweep.py http://127.0.0.1:8100` | exit 0: no parameterless GET route answers 5xx (503 = enterprise off is allowed), and **all 241 mutating routes refuse an anonymous caller** (401 / 503 / 4xx) |
| S5.7 | stop it: `kill "$(cat "$SCRATCH/dash.pid")"` | the process exits; port 8100 is free |
| S5.8 | base path: start again with `ATIP_ROOT_PATH=/ATIP`, then `curl -s http://127.0.0.1:8100/openapi.json` | contains `"servers":[{"url":"/ATIP"}]`; `/health/live` is 200 (the proxy strips the prefix). Stop it |
| S5.9 | snapshot dry run: `PYTHONPATH=tests/canary ATIP_CANARY_LOG="$SCRATCH/publish-canary.jsonl" $PY publish_snapshot.py --dry-run` | exit 0, logs `Snapshot for trade date … built (N KB)`, and `$SCRATCH/publish-canary.jsonl` does not exist (nothing tried to leave the machine). There is no `atip_data/publish.json` in the copy, so there is nowhere to upload to anyway |
| S5.10 | the snapshot carries no write token: `$PY -c "from dashboard.security import token; import publish_snapshot as P; s=P.build_snapshot(); assert token() not in s['html'] and 'const ATIP_TOKEN=\"\"' in s['html']; print('token stripped')"` | prints `token stripped` |

`unset ATIP_DB_PATH ATIP_ROOT_PATH` afterwards.

### S6 — security and hygiene

| Step | Command | Passes when |
|---|---|---|
| S6.1 | `git -C "$REPO" ls-files > "$SCRATCH/tracked.txt"`, then the `grep` below | the `grep` prints nothing |
| S6.2 | the secret scan of `python -m ops scan`, run from the copy over the checkout's tracked files (it reads them; nothing is written there): `$PY -c "import json, sys; from ops.scan import scan_secrets; print(json.dumps(scan_secrets(sys.argv[1])))" "$REPO"` | the list is exactly the two known test-fixture passwords, `tests/test_w39_api_contract.py:27` and `tests/test_w39_exec_data_tests.py:724` (each file's fixed `PASSWORD` constant for a throwaway test user). Any other finding fails S6.2. Run it after the files are committed too: it only sees tracked files |
| S6.3 | anonymous mutating routes | S5.6's mutating count is 241 with 0 not refused (it moves with the API: compare it with `docs/API_REFERENCE.md`) |
| S6.4 | **canary run**: the full suite with every live switch on and fake broker SDKs (below) | `tests/canary/report.py` exits 0: no broker API call, no market feed, no Zerodha client, no connection leaving the machine |
| S6.5 | dependency audit, report-only, in a virtualenv of its own (pip-audit's dependencies must not change the test one): `python3 -m venv "$SCRATCH/audit" && "$SCRATCH/audit/bin/pip" install -q pip-audit && "$SCRATCH/audit/bin/pip-audit" -r requirements.txt --progress-spinner off` | report the findings. Fix one only when the fix is a trivial version bump; otherwise list it under open items (baseline: section 5). Offline it is **NOT RUN** |

```bash
grep -E '(^|/)\.env$|(^|/)atip_data/|(^|/)secrets/|\.(db|pem|key|pfx)$' "$SCRATCH/tracked.txt"   # S6.1
```

**S6.4, the canary run.** It shows that no test can take the LIVE path or reach the
network, even on a machine set up like the owner's: real-looking credentials, every live
switch on, broker SDKs importable. It uses its own scratch copy, made like the first one:

```bash
C="$SCRATCH/canary"; mkdir -p "$C/src"
tar -C "$REPO" --exclude=./.git --exclude=./atip_data --exclude=./.env --exclude=./.venv \
    --exclude=./.claude/worktrees --exclude='__pycache__' -cf - . | tar -C "$C/src" -xf -
mkdir -p "$C/src/atip_data"
cp "$C/src/tests/canary/hostile_config.json" "$C/src/atip_data/config.json"
cp "$C/src/tests/canary/hostile.env" "$C/src/.env"
cd "$C/src" && env -u ATIP_DATABASE_URL -u DATABASE_URL -u ATIP_DB_PATH -u ATIP_TEST_POSTGRES_URL \
  -u ATIP_TEST_PG_TLS_URL -u ATIP_TEST_PG_POOLER_URL TZ=UTC \
  PYTHONPATH="$C/src/tests/canary" ATIP_CANARY_LOG="$C/canary.jsonl" \
  HTTPS_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 https_proxy=http://127.0.0.1:9 \
  http_proxy=http://127.0.0.1:9 NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost \
  $PY -m pytest tests/ -p no:cacheprovider -q -rs > "$C/pytest.log" 2>&1
$PY "$C/src/tests/canary/report.py" "$C/canary.jsonl"; cd "$SCRATCH/src"
```

`tests/canary/` holds the stand-ins. Fake `dhanhq` and `kiteconnect` packages record
every client built and every method called, then refuse it. `sitecustomize.py` refuses
and records every connection that leaves the machine. It does the same for anything sent
to the dead local port 9: the proxy variables point there, and so does `hostile.env`'s
`ATIP_DATABASE_URL`. The hostile `config.json` also switches the runtime to PostgreSQL,
so a test that bypassed `tests/conftest.py`'s SQLite pin would show up as `dead_port`.
Conftest's `_offline` guard already refuses in-process connections. What the canary mainly
catches, then, is code outside it: subprocesses that tests start, and anything at import
time. Building a Dhan client object is listed but allowed: the SDK's
constructor sends nothing. Test **failures** in this run are expected: production
environment, live switches and fake credentials change what many tests see. They are not
the verdict. The verdict is `report.py`'s exit code. List the failure count in the report
for information. The canary run may run at the same time as R1, since it uses no
database server.

### S7 — Neon parity (TLS, non-superuser, pooler, dropped connections)

Needs `ATIP_TEST_PG_TLS_URL` and `ATIP_TEST_PG_POOLER_URL` (section 2). Never point
either at Neon: both checks refuse a host that is not this machine.

| Step | Command | Passes when |
|---|---|---|
| S7.1 | `env -u ATIP_DATABASE_URL -u DATABASE_URL ATIP_TEST_PG_TLS_URL=… ATIP_TEST_PG_POOLER_URL=… $PY -m pytest tests/test_neon_parity.py -p no:cacheprovider -rs` | 8 passed, 0 skipped |
| S7.2 | reset the pool database: `$PY -c "import os, psycopg; c = psycopg.connect(os.environ['ATIP_TEST_PG_TLS_URL'], autocommit=True); c.execute('DROP SCHEMA IF EXISTS public CASCADE'); c.execute('CREATE SCHEMA public')"` | exit 0 |
| S7.3 | switch the **copy** to PostgreSQL: `mkdir -p atip_data && printf '%s' '{"database": {"backend": "postgresql", "allow_experimental": true}}' > atip_data/config.json` | — |
| S7.4 | schema over the direct TLS URL, twice: `ATIP_DATABASE_URL="$ATIP_TEST_PG_TLS_URL" $PY main.py --init` (×2) | both exit 0 and print `Database created  : PostgreSQL — postgresql://t_atip:***@127.0.0.1:5432/t_atip_pool?sslmode=require&channel_binding=require` (password masked) |
| S7.5 | the app on the pooled URL: `ATIP_DATABASE_URL="$ATIP_TEST_PG_POOLER_URL" $PY main.py --dashboard --port 8100 > "$SCRATCH/dash-pg.log" 2>&1 &` | `/health/ready` is 200 with `{"status":"READY","database":"READY"}`; `/`, `/api/scores`, `/api/orders` are 200; `$PY tools/route_sweep.py http://127.0.0.1:8100` exits 0 |
| S7.6 | it runs as a non-superuser: `$PY -c "import os, psycopg; print(psycopg.connect(os.environ['ATIP_TEST_PG_TLS_URL']).execute('SELECT rolsuper FROM pg_roles WHERE rolname = current_user').fetchone())"` | `(False,)` |
| S7.7 | the server drops idle connections: after 30 s with no requests, `$PY -c "import os, psycopg; c = psycopg.connect(os.environ['ATIP_TEST_PG_TLS_URL'], autocommit=True); print(c.execute('SELECT COUNT(*) FILTER (WHERE pg_terminate_backend(pid)) FROM pg_stat_activity WHERE usename = current_user AND datname = current_database() AND pid <> pg_backend_pid()').fetchone()[0])"` | prints ≥ 1 (the pooler's idle server connections); the dashboard process is still running; then `/health/ready`, `/` and `/api/scores` are 200, and the log has no new traceback |
| S7.8 | stop it; `rm atip_data/config.json` | the process exits; port 8100 is free |

## 4. Regression suite

### R1 — the full test suite

R1 runs in a scratch copy of its own, made like the first one, so that nothing the smoke
runs wrote (S5 and S7 leave an `atip_data/` behind in `$SCRATCH/src`) can be seen by it:

```bash
mkdir -p "$SCRATCH/r1/src"
tar -C "$REPO" --exclude=./.git --exclude=./atip_data --exclude=./.env --exclude=./.venv \
    --exclude=./.claude/worktrees --exclude='__pycache__' -cf - . | tar -C "$SCRATCH/r1/src" -xf -
cd "$SCRATCH/r1/src" && env -u ATIP_DATABASE_URL -u DATABASE_URL -u ATIP_DB_PATH TZ=UTC \
  ATIP_TEST_POSTGRES_URL="postgresql://t_atip:<pw>@127.0.0.1:5432/t_atip" \
  ATIP_TEST_PG_TLS_URL="postgresql://t_atip:<pw>@127.0.0.1:5432/t_atip_pool?sslmode=require&channel_binding=require" \
  ATIP_TEST_PG_POOLER_URL="postgresql://t_atip:<pw>@127.0.0.1:6432/t_atip_pool?sslmode=require" \
  $PY -m pytest tests/ -v -rs -p no:cacheprovider --durations=15
```

This is CI's `python -m pytest tests/ -v` plus the two Neon stand-in URLs.
`-p no:cacheprovider` keeps `.pytest_cache/` out of the tree, since it is not git-ignored.
`-rs` lists every skip reason. Do not run R1 and S7 at the same time: both use
`t_atip_pool`.

### R2 — critical regressions

| ID | Behaviour that must never break | How it is checked |
|---|---|---|
| ATIP-CR-01 | Paper is the default. A missing, unreadable or misspelt `broker_env` means PAPER, and the paper client is never a real Dhan client | `tests/test_paper_broker.py::test_default_environment_is_paper`, `::test_unrecognised_environment_falls_back_to_paper`, `::test_unreadable_config_falls_back_to_paper`, `::test_paper_client_is_never_a_real_dhan_client`; S1.4 |
| ATIP-CR-02 | Real money needs every switch: `execution.mode` LIVE + `live_trading_enabled` JSON `true` + environment production (W4); broker_env LIVE + `confirm=True` + the same master switch (W1), else `BLOCKED_LIVE_DISABLED` and nothing sent | `tests/test_live_gate.py` (all 6); `tests/test_w40_option_intents.py::test_live_is_refused_by_the_risk_engine_the_oms_and_the_adapter` |
| ATIP-CR-03 | No test reaches a broker or the network, even with live switches and credentials configured | S6.4 canary: `report.py` exit 0 |
| ATIP-CR-04 | The kill switch and the risk limits refuse a confirmed order before it reaches the broker | `tests/test_risk_controls.py::test_a_confirmed_order_is_refused_while_trading_is_halted`, `::test_a_confirmed_order_is_refused_when_a_limit_would_break`; `tests/test_w39_risk_gateway.py::test_manual_limit_30pct_above_ltp_is_blocked` |
| ATIP-CR-05 | Every route that can change something refuses an anonymous caller (X-ATIP-Token, or 503 while enterprise is off) | `tests/test_risk_controls.py::test_every_mutating_route_is_guarded`, `::test_a_mutating_route_refuses_an_unauthenticated_caller`; `tests/test_wealth_api_security.py::test_every_mutating_wealth_route_needs_the_token`; S5.6 / S6.3 (all 241) |
| ATIP-CR-06 | The dashboard binds to 127.0.0.1 unless told otherwise | `tests/test_risk_controls.py::test_the_dashboard_binds_to_this_machine` |
| ATIP-CR-07 | The snapshot sent to bkesari.com never carries the write token, and a dry run uploads nothing | `tests/test_w39_exec_data_tests.py::test_snapshot_payload_is_built_from_the_local_database_without_the_write_token`, `::test_publish_refuses_without_a_target_config_and_never_uploads`; S5.9, S5.10 |
| ATIP-CR-08 | Tests never touch the checkout's `atip_data/` (the Mac's live cache, token and config), a configured PostgreSQL runtime, or another machine | `tests/test_isolation.py` (all 4); `tests/conftest.py` pins SQLite at import and keeps every test offline (`_offline`) |
| ATIP-CR-09 | `--init` / `init_db()` are idempotent and add only what is missing, on SQLite and on PostgreSQL | S5.1, S7.4; `tests/test_schema_sql.py::test_the_sqlite_script_builds_the_database_init_db_would`; `tests/test_postgres_backend.py::test_init_db_creates_the_whole_schema_on_postgres_and_is_idempotent`; `tests/test_neon_parity.py::test_the_schema_is_built_over_tls_by_a_non_superuser_and_a_rerun_adds_nothing` |
| ATIP-CR-10 | The checked-in schema scripts match the code | `tests/test_schema_sql.py::test_the_checked_in_scripts_match_the_generator`; S4.1 |
| ATIP-CR-11 | PostgreSQL runs only behind all three switches, the URL is read where the LaunchAgent sees it, validation reports the database actually in use, and a leftover MySQL runtime refuses to start | `tests/test_w38_platform_compliance.py::test_runtime_stays_on_sqlite_unless_all_three_switches`; `tests/test_postgres_backend.py::test_config_validation_reports_the_database_the_runtime_actually_uses`, `::test_the_runtime_url_is_read_where_the_launchagent_can_see_it`, `::test_a_runtime_still_switched_to_mysql_refuses_to_start` |
| ATIP-CR-12 | Neon parity: the TLS parameters reach libpq unchanged; the schema builds as a non-superuser; the app works through a transaction pooler and lets it reuse server connections; the next call succeeds after the server drops connections | `tests/test_neon_parity.py` (all 8); S7 |
| ATIP-CR-13 | The schedule: 07:00 pre-market, the 15-minute refresh from 09:15 to the 15:30 close, 16:45 post-market, on IST | `tests/test_schedule_times.py` (all 5); `tests/test_signal_pipeline.py::test_postmarket_runs_after_nse_publishes`; `tests/test_w38_platform_compliance.py` (the image's `TZ=Asia/Kolkata`) |
| ATIP-CR-14 | Determinism: the scenario replay and the seeded searches give the same answer every time | S2.1; `tests/test_aggressive_backtest.py::test_simulation_is_deterministic`; `tests/test_w39b_bayes_optimize.py::test_bayes_is_deterministic_for_a_seed_and_starts_with_the_random_draws` |
| ATIP-CR-15 | Serving under `/ATIP/`: the prefix is normalised and reaches FastAPI | `tests/test_root_path.py` (both); S5.8 |
| ATIP-CR-16 | Past incident: one broken indicator must not silence the rest, and scores never use a session without its closes | `tests/test_regressions.py::test_macd_columns_are_not_swapped`, `::test_indicators_store_the_normalised_histogram`; `tests/test_signal_pipeline.py::test_postmarket_does_not_score_a_day_without_its_closes` |
| ATIP-CR-17 | Append-only audit and ledger tables refuse edits, on both engines | `tests/test_w38_platform_compliance.py::test_ddl_and_append_only_triggers`; `tests/test_w39_performance_detail.py::test_migration_0006_numbers_old_rows_and_the_ledger_stays_append_only`; `tests/test_schema_sql.py::test_the_postgresql_script_loads_into_an_empty_database` |

### R3 — change-area map

| Changed path | Emphasise |
|---|---|
| `orders/**`, `execution/**`, `brokers/**`, `strategy/**`, `strategy_engine/**`, `portfolio/**` | CR-01, 02, 03, 04; `tests/test_paper_broker.py`, `test_risk_controls.py`, `test_live_gate.py`, `test_strategy_live.py`, `test_w39_risk_gateway.py`, `test_w40_option_intents.py`; S6.4 |
| `dashboard/**`, `ops/http.py`, `enterprise/authz.py` | CR-05, 06, 07, 15; S5 in full; `tests/test_wealth_api_security.py`, `test_w39_api_contract.py`, `test_w39_ops_tests.py` (API reference) |
| `db/**`, `ops/migrations.py`, `ops/config.py`, `tools/sqlite_to_postgres.py`, `tools/schema_sql.py` | CR-08, 09, 10, 11, 12, 17; S1.3, S3, S4.1, S7 (required) |
| `pipeline/**`, `utils/trading_calendar.py`, `deploy/launchd/**` | CR-13; `tests/test_signal_pipeline.py`, `test_auto_recover.py`, `test_job_monitoring.py` |
| `scores/**`, `data/technical.py`, `research/**`, `quant/**`, `ml/**`, `backtest/**` | CR-14, 16; S2.1 |
| `data/**` (feeds), `alerts/**` | CR-03 (S6.4); `tests/test_index_feed.py`, `test_regressions.py` |
| `publish_snapshot.*` | CR-07; S5.9, S5.10 |
| `tests/conftest.py`, `tests/canary/**` | CR-03, 08; S6.4 |
| `Dockerfile`, `docker-compose.yml`, `.github/workflows/**`, `.dockerignore` | S1.2, S4.3 (CI), `tests/test_w38_platform_compliance.py` |
| `docs/**` only | S1 and R1 still run (docs are asserted on: API reference, owner notes); no smoke required |

### R4 — baseline comparison

Compare R1 with section 5. It fails when **passed goes down** (with tests not removed
on purpose), **skipped goes up**, or a test that ran before is now skipped. Only a skip
listed as expected in section 5 is allowed. A new test raises both collected and passed.
Say which ones. The same applies to S3 (0 skipped) and S7.1 (8 passed).

## 5. Baseline

Measured 2026-10-24 on this branch (`claude/fervent-bell-wcmmdu`, base `cc1e54c`, plus
this spec's uncommitted changes). Linux container, Python 3.12.3, the CI package set plus
`psycopg[binary]` 3.3.6, PostgreSQL 16.15 with TLS, PgBouncer 1.22, `TZ=UTC`. Four CPUs
were shared with six other jobs, so the durations are pessimistic.

| Level | Collected | Passed | Skipped | Failed | Time |
|---|---|---|---|---|---|
| R1 on `cc1e54c` as committed (#13 claimed 1523 / 11; not reproduced) | 1548 | 1535 | 13 | 0 | 14 min 23 s |
| R1, first run with this spec's tests | 1564 | 1551 | 13 | 0 | 17 min 14 s |
| R1, round 2 (before the D9 and D12 fixes) | 1577 | 1564 | 13 | 0 | 13 min 49 s |
| **R1, final (the baseline)** | **1578** | **1565** | **13** | **0** | 14 min 0 s |
| S3 live PostgreSQL | 36 | 36 | 0 | 0 | — |
| S7.1 Neon parity tests | 8 | 8 | 0 | 0 | — |
| S6.4 canary | — | — | — | — | `report.py` exit 0: 0 order-capable calls, 0 refused calls or connections |

Expected skips in R1 (all of them, and only these):

| Count | Test | Needs |
|---|---|---|
| 1 | `tests/test_index_feed.py:187` | `dhanhq` |
| 9 | `tests/test_w39_ui_smoke.py` (lines 96, 117, 150 ×5, 163) | `playwright` |
| 1 | `tests/test_w39b_patterns_more.py:361` | `playwright` |
| 1 | `tests/test_w39b_tf75_signals.py:267` | `playwright` |
| 1 | `tests/test_w40_stock_chart.py:333` | `playwright` |
| 1 | `tests/test_wealth_planning.py:146` | `anthropic` |

Earlier measurement, same container, on `44bd83e` (master before PostgreSQL #13, CI
packages, no database server): 1601 collected, 1553 passed, 48 skipped, 0 failed, 19 min
26 s. 25 of those skips were MySQL tests that #13 deleted.

## 6. Pass criteria

- **PASS**
  - Every level S1–S7 and R1 ran (S4.2, S4.3, S6.5 and S7 may be SKIPPED or NOT RUN only for the reasons given in their rows).
  - Every one is green, and R1 is at or above the baseline under R4's rules.
  - Every CR row is checked, and nothing above S4 is open.
  - S7 may be SKIPPED only when the change touches none of the R3 database row's paths; otherwise a skipped S7 makes the run BLOCKED.
- **FAIL**
  - A defect is still open after the fix loop (three rounds), a CR check is red, or R4 finds a regression.
  - The canary (S6.4) reporting a broker call, a feed, a Zerodha client or a connection off the machine is always FAIL.
- **BLOCKED**
  - The suite could not run: no local PostgreSQL for S3 / R1, the scratch copy or virtualenv could not be made, or port 8100 is taken by something else.
  - Or a hard rule stopped the run, for example an `ATIP_TEST_*` or `ATIP_DATABASE_URL` naming a host that is not this machine, or a Neon host.

## 7. Defect handling

| Severity | Meaning | Example from this project |
|---|---|---|
| S1 | data loss, security, money, or the app does not start | Tests run on a Mac switched to Neon wrote their rows into the production database: `get_connection()` bypassed `DB_PATH` (fixed 2026-10-24; CR-08) |
| S2 | a feature broken | A test passed only where no Dhan credentials existed; with the owner's, it made a live Dhan API call (`test_history_sync…`, fixed 2026-10-24) |
| S3 | degraded behaviour, workaround exists | Validation reported "EXPERIMENTAL PostgreSQL runtime" while ATIP ran on SQLite, because it read a generic `DATABASE_URL` (fixed 2026-10-24) |
| S4 | cosmetic | `GET /api/ml/regime` answered 500 instead of 404 on a database with no `market_health` rows (fixed 2026-10-24) |

The agent may fix on its own:
- a product defect with a clear root cause inside this repository, in the smallest change that fixes it, plus a test that fails without the fix;
- a test proven wrong by the code's documented behaviour (say how in the report);
- a pre-existing defect, when the fix is small.

Report instead of fixing:
- anything in bkesari-platform, such as the viewer or its CSP;
- anything that needs the owner, such as broker accounts, Neon, the Mac's `atip_data/`, or `execution.mode`;
- dependency upgrades beyond a trivial bump;
- schema changes that would need a migration on deployed databases (a checksummed migration's text is part of its checksum: `ops/migrations.py`).

Never:
- skip, delete, quarantine or loosen a test, or change an expected value to match wrong output;
- install `dhanhq`, `kiteconnect`, `anthropic` or `schedule` into the test virtualenv;
- run anything from the project folder's `atip_data/` or with its `.env` / `.venv`;
- point any URL at a non-local database, or at a database other than the three scratch ones;
- commit, push, deploy, publish a snapshot for real, or touch a server.

## 8. Report format

Written to `.claude/test-runs/<UTC yyyymmdd-hhmmss>-atip-tester.md` and copied to
`.claude/test-runs/latest-atip-tester.md`:

```markdown
# atip-tester report — <UTC date time>

- Commit: <short sha> on <branch> (+ uncommitted: yes/no)
- Changed files (vs origin/master, and uncommitted): <list, each with its R3 area>
- Environment: Python <x.y.z>, psycopg <v>, PostgreSQL <v> (TLS: yes/no), PgBouncer <v or none>, TZ=UTC

## Results

| Level | Command | Result | Counts | Duration | vs baseline |
|---|---|---|---|---|---|
| S1.1 compileall | … | PASS/FAIL | – | | |
| S1.2 YAML | … | | | | |
| S1.3 dialect scan | … | | N statements, M hand changes | | |
| S1.4 paper / live gate | … | | | | |
| S2.1 replay ×2 | … | | identical: yes/no | | |
| S3 live PostgreSQL | … | | p/f/s | | |
| S4.1 schema scripts | … | | | | |
| S4.2 wheel | … | | files | | |
| S4.3 docker build | … | SKIPPED (reason) | | | |
| S5 smoke SQLite | steps 5.1–5.10 | | GET routes / 5xx, mutating / refused | | |
| S6.1 tracked secrets | … | | | | |
| S6.2 ops scan | … | | findings | | |
| S6.4 canary | … | | records by kind; pytest p/f/s (info) | | |
| S6.5 pip-audit | … | | findings | | |
| S7.1 parity tests | … | | p/f/s | | |
| S7 smoke on the pooler | steps 7.2–7.8 | | | | |
| R1 full suite | … | | collected / passed / failed / skipped | | |

## Critical regressions

| ID | Result | Evidence |
|---|---|---|
| ATIP-CR-01 … CR-17 | PASS/FAIL | test ids or step |

## Defects

| ID | Severity | Area | Symptom | Root cause | Fix | Files | Test added | Status |
|---|---|---|---|---|---|---|---|---|

## Re-test

<the failing tests run first, then the complete S1–S7 and R1 again: counts and durations>

## Open items

<what is still open, blocked or needs the owner, and why>

TESTER: atip-tester
VERDICT: PASS | FAIL | BLOCKED
```

## 9. Known gaps

- **No browser end-to-end tests.** The 7 Playwright tests need the Python `playwright` package, which CI does not install, so they skip there and here. S5 probes the served page as text.
- **No load or soak tests.** `tests/test_w39_perf_budget.py` is an in-process latency budget, not a load test. A day of scheduler jobs is not run.
- **The scheduler is never started for real.** `schedule` is left out, so `tests/test_schedule_times.py` records the registrations rather than firing them. The IST part rests on the launchers' `TZ=Asia/Kolkata`.
- **External APIs are never contacted.** Dhan, NSE, yfinance, Telegram, Anthropic and Alpha Vantage are only seen through recorded payloads and stubs. A change in their responses is not caught.
- **Real Neon is never contacted.** S7 covers TLS, the non-superuser role, transaction pooling, prepared statements and dropped connections against a local stand-in. Not covered: Neon's own proxy (SNI routing, `options=endpoint=`), a real cold start's latency, and channel binding through a pooler, which PgBouncer 1.22 cannot do.
- **The PostgreSQL runtime is still experimental** (`allow_experimental`). The suite runs on SQLite. Only `tests/test_postgres_backend.py`, `test_schema_sql.py`, `test_neon_parity.py` and S7 exercise PostgreSQL, so most jobs' write paths have not run on it (docs/POSTGRESQL_MIGRATION.md, "Known limits").
- **The snapshot viewer's CSP** is bkesari-platform's and is tested there. The snapshot still contains the dashboard's Buy / Sell buttons; on bkesari.com they are inert only because of that CSP and the stripped token.
- **The Docker image** is built and started only in CI (`docker.yml`).
- **Windows launchers** are not tested.
- **Dependency audit** needs network (S6.5) and is report-only.
