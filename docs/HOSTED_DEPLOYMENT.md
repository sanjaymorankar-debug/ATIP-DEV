# Hosting ATIP at `*.bkesari.com/ATIP/`

How to run the **full interactive** ATIP server-side on PostgreSQL, reverse-proxied at
`/ATIP/`, across the dev → test → production tiers.

> **Plan change (2026-10-09): PostgreSQL is ATIP's final server database.** The MySQL
> backend this document originally described (`db/mysql.py`, `tools/sqlite_to_mysql.py`,
> `tools/export_mysql.py`, the `mysql` compose profile) has been removed and MySQL /
> MariaDB is no longer supported — a `mysql://` URL is refused. Everything below now
> targets PostgreSQL 16.

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
| 250 tables, continuous writes on **PostgreSQL** | MySQL / MariaDB only — shared hosting has no PostgreSQL at all |

So `/ATIP/` is served by a **VPS** (Hostinger VPS, or any small cloud box) and the
public hostname proxies to it. Everything else on `bkesari.com` is unaffected.

**The database moves too.** Hostinger shared hosting (and its phpMyAdmin) provides
MySQL / MariaDB only, which ATIP no longer supports. PostgreSQL has to come from either:

- **the VPS itself** — the bundled `postgres:16` container (`docker compose --profile
  postgres`, §5) or a distro `postgresql-16` package; simplest, and the database never
  leaves the box; or
- **a managed PostgreSQL** service (any provider offering PostgreSQL 16) — backups and
  failover handled for you; restrict it to the VPS's IP and require TLS
  (`?sslmode=require` on the URL).

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
- [ ] A backup of `atip_data/` and the PostgreSQL database (`pg_dump -Fc`) before first public DNS.

---

## 3. PostgreSQL

ATIP's PostgreSQL backend is `db/postgres.py` (`translate()`, `ddl()`, `trigger_ddl()`,
`PgConnection` over psycopg 3). It is verified against a real server by
`tests/test_postgres_backend.py` and `tests/test_schema_sql.py` (set
`ATIP_TEST_POSTGRES_URL`), and W38 ran a copy of the live database on it — 213 tables
and 497,317 rows identical, 198 GET routes with 0 server errors
(`docs/POSTGRESQL_MIGRATION.md`).

### 3.1 Create the database

```sql
CREATE ROLE atip LOGIN PASSWORD '<strong password>';
CREATE DATABASE atip OWNER atip ENCODING 'UTF8';
```

Or use the bundled container: `docker compose --profile postgres up -d` (reads
`deploy/pg_password.txt`, git-ignored), or a managed PostgreSQL 16 (§1).

**One database per tier.** dev, test and production never share one.

### 3.2 Turn the backend on

Both are required — the URL alone does nothing:

```bash
export ATIP_DATABASE_URL='postgresql://atip:<password>@127.0.0.1:5432/atip'
```

```json
// atip_data/config.json
{
  "database": { "backend": "postgresql", "allow_experimental": true }
}
```

This is the gate in `db/schema.pg_runtime_url()`, and it stays a gate for the reason
in §3.4. `pip install "psycopg[binary]"` (in `requirements.txt`; the container image
has it).

### 3.3 Create the schema

Either let ATIP build it:

```bash
python main.py --init
```

or load the generated script into the empty database, then start ATIP:

```bash
psql "$ATIP_DATABASE_URL" -v ON_ERROR_STOP=1 -f db/sql/atip_schema.postgresql.sql
```

Both produce the same schema: `db/sql/atip_schema.postgresql.sql` is generated by
`tools/schema_sql.py` from the same `init_db()` path, translated by `db.postgres.ddl()`.
What the translation does:

| SQLite | PostgreSQL |
|---|---|
| `INTEGER PRIMARY KEY [AUTOINCREMENT]` | `BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY` |
| `REAL`, `BLOB`, `DATETIME` | `DOUBLE PRECISION`, `BYTEA`, `TIMESTAMP` |
| `INSERT OR IGNORE` / `INSERT OR REPLACE` | `ON CONFLICT DO NOTHING` / an upsert on the unique key the inserted columns cover |
| `RAISE(ABORT, 'msg')` in a trigger | a row trigger calling `atip_append_only('msg')` |
| `` `signal` `` (backticked columns) | `"signal"` |
| `rowid` on `perf_ledger` / `ml_dl_benefit` | their identity `seq` column (other ORDER BY tie-breakers: `ctid`, append-only tables only) |
| `BEGIN IMMEDIATE` | a transaction-scoped advisory lock |
| `CREATE TRIGGER IF NOT EXISTS` | `CREATE OR REPLACE TRIGGER` |

**Existing data:** migrate a backup copy with `tools/sqlite_to_postgres.py`
(dry run, then `--target postgresql://... --execute`; it verifies row counts per table).

### 3.4 What is still SQLite-only

The backend stays behind `allow_experimental` because a few hand-written queries still
use constructs with no automatic translation. The scanner lists them:

```bash
python -m db.dialect_scan --details
```

`strftime()` and any `rowid` that is neither an entry-order column nor an ORDER BY tie-breaker raise `UnsupportedSQL`
rather than being silently mistranslated. Run the scheduler's write jobs for a few
days on a PostgreSQL copy before relying on it (DBS-05's open item).

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

`deploy/atip.env` should carry at least:

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
