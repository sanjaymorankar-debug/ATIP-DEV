# Hosting ATIP at `*.bkesari.com/ATIP/`

How to run the **full interactive** ATIP server-side on MySQL, reverse-proxied at
`/ATIP/`, across the dev → test → production tiers.

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
- [ ] The VPS firewall exposes **only** 80/443 — never 8000 or 3306.
- [ ] A backup of `atip_data/` and the MySQL database before first public DNS.

---

## 3. MySQL

ATIP's MySQL backend is `db/mysql.py`. It is verified against a real server by
`tests/test_mysql_backend.py`: all 186 tables and 67 indexes are created by MySQL
itself, and upsert/trigger semantics are round-tripped.

### 3.1 Create the database

```sql
CREATE DATABASE atip CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'atip'@'%' IDENTIFIED BY '<strong password>';
GRANT ALL PRIVILEGES ON atip.* TO 'atip'@'%';
FLUSH PRIVILEGES;
```

Or use the bundled container: `docker compose --profile mysql up -d` (writes
`deploy/mysql_password.txt`, git-ignored).

**One database per tier.** dev, test and production never share one.

### 3.2 Turn the backend on

Both are required — the URL alone does nothing:

```bash
export ATIP_DATABASE_URL='mysql://atip:<password>@127.0.0.1:3306/atip'
```

```json
// atip_data/config.json
{
  "database": { "backend": "mysql", "allow_experimental": true }
}
```

The same two-key gate gates the PostgreSQL path (`db/schema.pg_runtime_url`), and
for the same reason: see §3.4.

### 3.3 Create the schema

```bash
python main.py --init
```

`db/mysql.ddl()` translates each statement on the way through. What it has to do
that the PostgreSQL path does not:

| SQLite | MySQL | Why |
|---|---|---|
| `run_id TEXT, PRIMARY KEY (run_id, seq)` | `VARCHAR(191)` | MySQL cannot index `TEXT` without a prefix length. 191 chars × 4 bytes (utf8mb4) keeps a 4-column composite key inside InnoDB's 3072-byte limit. |
| `status TEXT DEFAULT 'DRAFT'` | `VARCHAR(255)` | "BLOB, TEXT … can't have a default value" |
| `rows`, `status`, `key`, `rank`, `format` | `` `rows` `` … | Reserved words differ across MySQL 8 / MariaDB 10.6 / 10.11, so **every** generated identifier is quoted. |
| `INSERT OR REPLACE` | `ON DUPLICATE KEY UPDATE` | Fires on any unique key — the same rule SQLite follows, so no key has to be chosen. |
| `INSERT OR IGNORE` | `INSERT IGNORE` | |
| `RAISE(ABORT, 'msg')` in a trigger | `SIGNAL SQLSTATE '45000'` | MySQL has no trigger `WHEN`, so a conditional guard becomes an `IF` in the body. |

`GROUP_CONCAT`, `IFNULL`, `LIKE`, `SUM(a > b)`, `BLOB` and `DATETIME` are native
MySQL and are deliberately left alone — the PostgreSQL rewrites for them would be
syntax errors here.

### 3.4 What is still SQLite-only

The backend stays behind `allow_experimental` because hand-written queries across
the wider code base still contain constructs MySQL rejects. Two scanners report
them:

```bash
python -c "from db.dialect_scan import scan; r=scan('.'); print(r['pct_ready'], r['by_kind'])"
python -c "
from db import mysql; import db.schema as S, re
stmts=[d for n in dir(S) if re.fullmatch(r'(W\d+\w*|WEALTH)_TABLES',n)
       for ds in getattr(S,n).values() for d in ds]
print(mysql.reserved_columns(stmts))"
```

`rowid` and `strftime()` raise `UnsupportedSQL` rather than being silently
mistranslated: MySQL has no stable physical row identifier, so an
`ORDER BY rowid` entry-order tie-breaker cannot be reproduced at all.

**Migrating existing data** out of SQLite is a separate job and is not covered by
`--init`. `tools/sqlite_to_postgres.py` is the model to follow; there is no MySQL
equivalent yet.

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
| MySQL database | its own | its own | its own |
| `ATIP_ROOT_PATH` | `/ATIP` | `/ATIP` | `/ATIP` |
| `execution.mode` | `paper` | `paper` | `paper` until separately authorised |

---

## 5. Deploy

```bash
cd /opt/atip && git pull
printf '%s\n' "<mysql password>" > deploy/mysql_password.txt   # git-ignored
cp deploy/atip.env.example deploy/atip.env                      # then edit it
docker compose --profile mysql up -d --build
docker compose logs -f atip
```

`deploy/atip.env` should carry at least:

```
ATIP_DATABASE_URL=mysql://atip:<password>@mysql:3306/atip
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
the scheduler, so it is the one that proves MySQL is wired up.

---

## 6. If you want the read-only model back

It still works and is still the lower-risk option: run ATIP locally under launchd
(`deploy/launchd/install.sh`) and let `publish_snapshot.sh` push a rendered page
to the portal every 5 minutes. Nothing order-capable faces the internet, and no
VPS is needed. See `README.md` and `bkesari-platform/DEPLOY.md` part 3.
