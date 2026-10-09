# ATIP — project memory

Read this before working on the database layer, deployment or docs.

## Database decision (2026-10-09): PostgreSQL, not MySQL

- **PostgreSQL 16 is the server database for the hosted deployment.** It replaces the
  earlier MySQL plan (PR #8, "MySQL runtime"). New database work targets PostgreSQL.
- **SQLite stays the default for a local install** (`atip_data/atip.db`, WAL). Nothing
  changes for a local run unless the switch below is set.
- **MySQL is legacy.** `db/mysql.py`, `tools/sqlite_to_mysql.py`, `tools/export_mysql.py`
  and their tests are kept so nothing that imports them breaks, but MySQL is not deployed:
  `docker-compose.yml` has no MySQL service. Do not add MySQL-only work.

### How the runtime is switched to PostgreSQL

All three are required (`db/schema.pg_runtime_url()`):

1. `ATIP_DATABASE_URL=postgresql://atip:<password>@<host>:5432/atip` (the ATIP-specific
   variable; a generic `DATABASE_URL` is ignored on purpose)
2. `atip_data/config.json`: `"database": {"backend": "postgresql", "allow_experimental": true}`
3. the driver: `psycopg[binary]` (in `requirements.txt` and the Docker image)

Container: `docker compose --profile postgres up -d` (password in the git-ignored
`deploy/pg_password.txt`). Walkthrough: `docs/HOSTED_DEPLOYMENT.md` §3.
Data move: `tools/sqlite_to_postgres.py` on a backup copy (`docs/POSTGRESQL_MIGRATION.md`).
Empty pre-built schema: `db/sql/atip_schema.postgresql.sql`.

### Rules for SQL in this code base

- Write SQL in ATIP's SQLite dialect with `?` placeholders. `db/postgres.py` translates it
  on the way through (`PgConnection`), so the same query runs on both engines.
- Never write `rowid` for entry order. Use `db.backend.entry_order_column(table)`
  (`rowid` on SQLite, the identity `seq` column on PostgreSQL). Tables that need it are
  listed in `db.backend.ENTRY_ORDER_TABLES`.
- Constructs with no safe translation raise `UnsupportedSQL` on PostgreSQL rather than
  returning different data. `python -m db.dialect_scan --details` lists what is left.
- `PgConnection` pins the session to UTC (SQLite's `CURRENT_TIMESTAMP` is UTC). It makes
  a failed statement fail alone, as SQLite does. It maps `BEGIN IMMEDIATE` to an advisory
  lock. Keep those behaviours if you touch it.

## Testing

- `python -m pytest tests/`. The live PostgreSQL tests in
  `tests/test_postgres_backend.py` run when `ATIP_TEST_POSTGRES_URL` points at a
  throwaway database (it is dropped and recreated), e.g.
  `ATIP_TEST_POSTGRES_URL=postgresql://postgres@localhost:5432/atip_test`.
  CI (`.github/workflows/tests.yml`) runs them against a `postgres:16` service.
- Live MySQL tests (`ATIP_TEST_MYSQL_URL`) are legacy and skipped by default.
