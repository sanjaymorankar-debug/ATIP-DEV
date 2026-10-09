# Hosting ATIP at `*.bkesari.com/ATIP/`

How to run the **full interactive** ATIP server-side on PostgreSQL, reverse-proxied at
`/ATIP/`, across the dev → test → production tiers.

> **Database plan changed on 2026-10-09: PostgreSQL, not MySQL.** Earlier versions of
> this guide deployed on MySQL 8. PostgreSQL 16 is now the hosted database. The MySQL
> service is gone from `docker-compose.yml`, and `db/mysql.py` is kept only as legacy
> code. SQLite stays the default for a local install.

The project now lives at `/Users/agtci/Documents/Project_Documents/Projects/ATIP`
on macOS. Local operation is unchanged — see `README.md` and
`deploy/launchd/install.sh`.

---

## 1. Read this first: ATIP cannot go on Hostinger shared hosting

Shared hosting (where `bkesari.com`, `edu.bkesari.com` and the portal live) cannot
run ATIP, for reasons that are not configurable:

| ATIP needs | Shared hosting gives |
|---|---|
| A long-lived Python process (`main.py` = scheduler + dashboard + feed) | Per-request PHP/Node only; processes are reaped |
| An outbound WebSocket held open all session (Dhan live ticks) | No persistent outbound sockets |
| An in-process scheduler firing at 07:00 / 15:30 / 16:45 IST | No long-running timers |
| 186 tables, continuous writes | Fine, but only reachable from the app tier |

So `/ATIP/` is served by a **VPS** (Hostinger VPS, or any small cloud box) and the
public hostname proxies to it. Everything else on `bkesari.com` is unaffected.

Minimum workable box: 2 vCPU / 4 GB RAM / 40 GB disk.

## 2. Read this second: exposing ATIP is a gated step

`docker-compose.yml` binds every port to `127.0.0.1` on purpose, and says so:

> EXPOSURE: every port is published on the HOST'S 127.0.0.1 only. Putting ATIP on
> the internet is a separate, gated step (ENT-07: TLS proxy, auth on every route,
> `enterprise.enabled`, a security review).

That gate exists because **the interactive dashboard can create, confirm and cancel
orders**. The read-only snapshot design (`publish_snapshot.py` → the portal's
`/atip/` viewer) was built specifically so nothing order-capable faced the
internet. Hosting the real app gives that up, so the controls below are not
optional extras — they are what replaces that isolation.

Do not skip any of these before the hostname is public:

- [ ] **TLS terminated at the proxy**, HTTP redirected to HTTPS.
- [ ] **Authentication in front of every route**, not just the mutating ones.
      `dashboard/security.py` guards mutating routes with a token
      (`X-ATIP-Token`); read routes are unauthenticated by design for localhost
      and must not be left that way on a public host. Put HTTP Basic or an
      identity-aware proxy in front of the whole prefix.
- [ ] `enterprise.enabled: true` in `atip_data/config.json` so
      `enterprise/authz.py` stops being a pass-through.
- [ ] **`execution.mode` stays `paper`** until live trading is separately
      authorised. Hosting changes nothing about that; verify it after deploy.
- [ ] The dashboard token file is present and not world-readable.
- [ ] The VPS firewall exposes **only** 80/443 — never 8000 or 5432.
- [ ] A backup of `atip_data/` and the PostgreSQL database (`pg_dump -Fc atip`) before first public DNS.

---

## 3. PostgreSQL

ATIP's PostgreSQL backend is `db/postgres.py`. `tests/test_postgres_backend.py` verifies it
against a real server, and CI runs those tests against a PostgreSQL 16 service on every push.
They build the whole schema with `init_db()`, apply every migration, and round-trip upserts,
the append-only triggers, job locks and the error classes.

### 3.1 Create the database

**Option A: the bundled container** (simplest; the same box as ATIP):

```bash
printf '%s\n' "<strong password>" > deploy/pg_password.txt   # git-ignored; one line
docker compose --profile postgres up -d                      # PostgreSQL 16 as service "postgres"
```

The container creates the `atip` role and the `atip` database itself. Its port is
published on the host's `127.0.0.1:54320` only.

**Option B: PostgreSQL installed on the VPS, or a managed PostgreSQL:**

```sql
-- as the postgres superuser:  sudo -u postgres psql
CREATE ROLE atip LOGIN PASSWORD '<strong password>';
CREATE DATABASE atip OWNER atip ENCODING 'UTF8' TEMPLATE template0;
```

Keep PostgreSQL listening on localhost (`listen_addresses = 'localhost'`, the package
default), or limit it in `pg_hba.conf` to the app host.

**One database per tier.** dev, test and production never share one.

### 3.2 Turn the backend on

Both are required. The URL alone does nothing:

```bash
export ATIP_DATABASE_URL='postgresql://atip:<password>@127.0.0.1:5432/atip'
#   inside docker compose the host is the service name:  @postgres:5432/atip
#   the bundled container from the host:                 @127.0.0.1:54320/atip
```

```json
// atip_data/config.json
{
  "database": { "backend": "postgresql", "allow_experimental": true }
}
```

It must be `ATIP_DATABASE_URL`. A generic `DATABASE_URL` left by another project is
ignored on purpose. The driver is `psycopg` 3 (`pip install "psycopg[binary]"`). It is in
`requirements.txt`, and the Docker image installs it.

### 3.3 Create the schema

Use one of these:

```bash
python main.py --init                                                   # ATIP builds it
psql -d atip -v ON_ERROR_STOP=1 -f db/sql/atip_schema.postgresql.sql    # or restore the pre-built one
```

`db/postgres.py` translates ATIP's SQLite SQL on the way through: `?` placeholders,
`INSERT OR REPLACE` / `OR IGNORE` as `ON CONFLICT` upserts, `REAL` → `DOUBLE PRECISION`,
`BLOB` → `BYTEA`, `LIKE` → `ILIKE`, `PRAGMA` / `sqlite_master` onto `information_schema`,
and the append-only triggers as PL/pgSQL. `docs/POSTGRESQL_MIGRATION.md` has the full list.

What the connection does so that ATIP behaves the way it does on SQLite:

| SQLite behaviour | On PostgreSQL | Why it matters |
|---|---|---|
| `CURRENT_TIMESTAMP` / `date('now')` are UTC | Every session is pinned to `TimeZone=UTC` | A server installed from packages runs in the OS zone (IST). It would stamp defaulted rows 5h30m off. |
| A failed statement fails alone | Rolled back on its own: a savepoint inside a write transaction, otherwise a rollback of the read-only transaction | PostgreSQL otherwise aborts the whole transaction. Every "try the query, fall back" probe (live P&L, the risk checks, migrations) would then break the queries after it. |
| `BEGIN IMMEDIATE` takes the write lock | A transaction-scoped advisory lock | Job locks (`ops/jobs.py`) and migrations rely on it. Without it every job lock raised. |
| `rowid` is the entry order | An identity `seq` column on `perf_ledger` / `ml_dl_benefit` | A same-day BUY must sort before its SELL. |
| `cursor.lastrowid` | `lastval()` | Notifications and Telegram alerts read it. |

### 3.4 Moving existing data

To move existing SQLite data, use `tools/sqlite_to_postgres.py` **on a backup copy**. It upgrades
the copy to this release's schema, copies every table and verifies the row counts table by table.
See `docs/POSTGRESQL_MIGRATION.md`.

```bash
python -m ops backup                                   # take a copy first
python tools/sqlite_to_postgres.py --source <copy.db> --target "$ATIP_DATABASE_URL" --execute
```

### 3.5 Why the switch stays behind `allow_experimental`

`python -m db.dialect_scan --details` lists the statements that still need a hand
translation. These are a handful of SQLite-only maintenance tools, not runtime paths.
Everything else raises `UnsupportedSQL` naming the construct, so a missed query fails
loudly instead of returning different data. Watch `/compliance` and the job-health
panel for the first days on a new tier.

---

## 4. Serving at `/ATIP/`

ATIP is a FastAPI app. Served under a prefix it must be told that prefix, or every
generated link and every `fetch()` the page makes resolves against `/` and 404s.

```bash
export ATIP_ROOT_PATH=/ATIP          # or config.json "dashboard_root_path": "/ATIP"
export ATIP_DASHBOARD_HOST=127.0.0.1 # the proxy reaches it locally; never 0.0.0.0 on a public box
```

`dashboard/security.dashboard_root_path()` normalises this (`ATIP`, `/ATIP/`,
`//ATIP//` all become `/ATIP`), and it is passed to both `FastAPI(root_path=…)`
and `uvicorn.run(root_path=…)`.

### 4.1 nginx on the VPS

```nginx
# /ATIP/ -> the ATIP container on 127.0.0.1:8000
location /ATIP/ {
    # Strip the prefix: ATIP_ROOT_PATH tells FastAPI to re-add it when building URLs.
    proxy_pass http://127.0.0.1:8000/;

    proxy_http_version 1.1;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Prefix /ATIP;

    # The dashboard streams and holds connections; a job can run for ~60s.
    proxy_read_timeout 120s;
    proxy_buffering off;

    # Authentication in front of EVERY route (section 2).
    auth_basic           "ATIP";
    auth_basic_user_file /etc/nginx/atip.htpasswd;
}

# Redirect the bare /ATIP to /ATIP/ so relative links resolve.
location = /ATIP { return 301 /ATIP/; }
```

The trailing slash on **both** `location /ATIP/` and `proxy_pass …:8000/` is what
strips the prefix. Dropping either one sends `/ATIP/api/state` through to the app
as `/ATIP/api/state`, which it does not serve.

### 4.2 The portal's `/ATIP/` tab

`bkesari-platform` serves the menu. Its ATIP tab points at `/ATIP/` and the portal
redirects the old lowercase `/atip/` there, so existing links keep working. Pick
one of:

- **Proxy from the portal** — the portal forwards `/ATIP/` to the VPS. One
  hostname, no DNS change, and the portal's own login covers it.
- **A subdomain** — `atip.bkesari.com` on the VPS, with the tab pointing at it.
  Simpler to reason about, needs a DNS record and its own certificate.

### 4.3 Per tier

| | dev | test | production |
|---|---|---|---|
| URL | `dev.bkesari.com/ATIP/` | `test.bkesari.com/ATIP/` | `bkesari.com/ATIP/` |
| PostgreSQL database | its own | its own | its own |
| `ATIP_ROOT_PATH` | `/ATIP` | `/ATIP` | `/ATIP` |
| `execution.mode` | `paper` | `paper` | `paper` until separately authorised |

---

## 5. Deploy

```bash
cd /opt/atip && git pull
printf '%s\n' "<postgres password>" > deploy/pg_password.txt     # git-ignored
cp deploy/atip.env.example deploy/atip.env                      # then edit it
docker compose --profile postgres up -d --build
docker compose logs -f atip
```

`deploy/atip.env` should carry at least the lines below. `atip_data/config.json` also needs
`"database": {"backend": "postgresql", "allow_experimental": true}` (§3.2):

```
ATIP_DATABASE_URL=postgresql://atip:<password>@postgres:5432/atip
ATIP_ROOT_PATH=/ATIP
ATIP_DASHBOARD_HOST=0.0.0.0
TZ=Asia/Kolkata
```

`ATIP_DASHBOARD_HOST=0.0.0.0` is correct **inside** the container — compose still
publishes it on the host's `127.0.0.1:8000` only, and nginx is what faces the
internet. Do not change the compose port bindings.

### Verify

```bash
curl -fsS http://127.0.0.1:8000/health/live                    # on the VPS
curl -fsS -u user:pass https://dev.bkesari.com/ATIP/health/ready
curl -fsS -u user:pass https://dev.bkesari.com/ATIP/openapi.json | head -c 200
#   -> "servers":[{"url":"/ATIP"}]   confirms the prefix reached FastAPI
curl -sS -o /dev/null -w '%{http_code}\n' https://dev.bkesari.com/ATIP/   # 401 without auth
```

`/health/live` = the process answers. `/health/ready` also checks the database and
the scheduler, so it is the one that proves PostgreSQL is wired up.

---

## 6. If you want the read-only model back

It still works and is still the lower-risk option: run ATIP locally under launchd
(`deploy/launchd/install.sh`) and let `publish_snapshot.sh` push a rendered page
to the portal every 5 minutes. Nothing order-capable faces the internet, and no
VPS is needed. See `README.md` and `bkesari-platform/DEPLOY.md` part 3.
