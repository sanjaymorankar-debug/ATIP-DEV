# Moving ATIP from SQLite to PostgreSQL (DBS-05)

> **2026-10-09: PostgreSQL is the hosted deployment's database.** It replaces the earlier MySQL plan.
> `docs/HOSTED_DEPLOYMENT.md` §3 is the deployment walkthrough. A local install still runs on SQLite (WAL) by default.

W38 added everything needed to move to PostgreSQL in steps, and proved it on a copy of the live database. The 2026-10-09 switch closed the gaps that only a database built natively on PostgreSQL shows (see below).

## What exists

| Piece | Purpose |
|---|---|
| `db/postgres.py` | `translate()` converts ATIP's SQLite SQL to PostgreSQL. `ddl()` converts CREATE statements. `trigger_ddl()` handles the append-only guards. `PgConnection` gives a sqlite3-shaped connection over psycopg 3. |
| `db/dialect_scan.py` | Scans every SQL statement in the code (via the AST) and lists what does not translate automatically. |
| `tools/sqlite_to_postgres.py` | Migrates a **copy** of the database: upgrade it to the release schema, translate the schema, copy the data, reset identity sequences, then verify row counts per table. |
| `db/schema.pg_runtime_url()` | The opt-in runtime switch (below). |
| `db/sql/atip_schema.postgresql.sql` | A fresh, empty database, built natively on PostgreSQL and dumped (239 tables). |
| `docker-compose.yml` profile `postgres` | PostgreSQL 16 in a container, on the host's 127.0.0.1 only. |

## What `translate()` handles

- `?` placeholders become `%s`, and every `%` is doubled for psycopg.
- `INSERT OR IGNORE` becomes `ON CONFLICT DO NOTHING`.
- `INSERT OR REPLACE` becomes an upsert on the unique key that the inserted columns cover. SQLite replaces on any unique key, so the surrogate `id` is not used when it isn't inserted.
- `ON CONFLICT … DO UPDATE SET a=a+excluded.a`: the bare `a` is qualified with the table name. PostgreSQL rejects it as ambiguous otherwise.
- `date(x, '-N days')` and `date('now')`.
- `GROUP_CONCAT`, `IFNULL`.
- 2-argument `MAX` / `MIN` become `GREATEST` / `LEAST`.
- `LIKE` becomes `ILIKE`, because SQLite's LIKE is case-insensitive.
- `SUM(<comparison>)` gets a cast to int, because SQLite booleans are 0 and 1.
- `rowid` on `perf_ledger` / `ml_dl_benefit` becomes their identity `seq` column (`db.backend.ENTRY_ORDER_TABLES`). Any other `rowid` used as an ORDER BY tie-breaker becomes `ctid`, which is only valid on append-only tables.
- `BEGIN IMMEDIATE` takes a transaction-scoped advisory lock (one writer at a time, as in SQLite); `COMMIT` / `ROLLBACK` go through the connection.
- `CREATE TRIGGER IF NOT EXISTS` becomes `CREATE OR REPLACE TRIGGER`. `DROP TRIGGER name` gets its `ON table` from the catalogue.
- `PRAGMA query_only = ON` makes the following transactions `READ ONLY` (the MCP tools rely on this).
- `cursor.lastrowid` is read with `lastval()`.
- `sqlite_master` and `PRAGMA table_info` are answered from `information_schema`.
- The connection PRAGMAs become no-ops.

Anything else raises `UnsupportedSQL` and names the construct. A missed query fails loudly rather than returning different data.

**Status (2026-10-09):**
- 1,662 statements; 99.8 % translate automatically.
- The 4 that don't belong to SQLite-only tools: `tools/export_mysql.py` and the one-off `tools/repair_news_timezone.py`.

## What the connection does beyond translating

`PgConnection` makes PostgreSQL behave the way ATIP's SQLite code expects:

- **Session time zone pinned to UTC.** SQLite's `CURRENT_TIMESTAMP` and `date('now')` are UTC. A PostgreSQL server installed from packages runs in the OS zone (IST), so without the pin, defaulted timestamps were 5h30m off.
- **A failed statement fails alone.** PostgreSQL normally aborts the whole transaction. Inside a write transaction each statement now runs in a savepoint; a failed read outside one rolls back a transaction that has written nothing. Without this, `/api/pnl/live` failed: its "is the paper table there yet?" probe poisoned the queries after it.
- **Entry order.** New tables get `seq`. An entry-order table made by an earlier release gains the column on the next start; rows are numbered in physical order, which on these append-only tables is the order they were written. `tools/sqlite_to_postgres.py` copies them in rowid order.

Run `python -m db.dialect_scan --details` to see the current list.

## Verified (2026-10-09, native build)

The test used a throwaway PostgreSQL 16 and a database built from nothing with `init_db()` + `ops.migrations.apply()`, with the runtime switched over the normal way:
- 239 tables.
- Migrations 0001-0006 all apply, and a second run finds nothing pending. Before the fix every one failed at `BEGIN IMMEDIATE`, so a database built this way had no append-only guards.
- Every GET route without a path parameter was called. 0 server errors are specific to PostgreSQL. The 2 remaining 500s (`/api/schemas/check-all`, `/api/ml/regime`) fail the same way on an empty SQLite database.
- `db/sql/atip_schema.postgresql.sql` restores cleanly. `init_db()` on top of it changes nothing, and `ops.migrations.validate()` is clean.
- `tests/test_postgres_backend.py` runs live in CI against a `postgres:16` service.

## Verified (2026-10-02)

The test used a throwaway PostgreSQL 16 and a copy of the live database:
- 213 tables and 497,317 rows copied; the row counts are identical table by table.
- All 198 GET API routes ran on PostgreSQL with **0 server errors**. 31 returned 503 because enterprise features are off.
- Upserts, alert dedupe, the job-health check, the append-only triggers and the catalog queries were also exercised.

The same test found a W36 bug that also affected SQLite (the quant factor set). It is fixed.

## Migrating

1. **Take a copy.** For example `python -m ops backup`, then use the backup file. The tool refuses the live database.
2. **Dry run:**
   `python tools/sqlite_to_postgres.py --source <copy.db> --out atip_data/pg_migration`
   It writes `schema.postgresql.sql` and `plan.json`.
3. **Copy and verify:**
   `pip install "psycopg[binary]"`, then
   `python tools/sqlite_to_postgres.py --source <copy.db> --target postgresql://user@host:5432/atip --execute`
4. **Switch the runtime.** All three are required:
   - `ATIP_DATABASE_URL=postgresql://…`
     - This must be the ATIP-specific variable. A generic `DATABASE_URL` left by another project is ignored on purpose.
   - In `config.json`:
     - `"database": {"backend": "postgresql", "allow_experimental": true}`
5. **Watch the first days.** `/compliance`, `/api/platform/postgres` and the job-health panel.

## Known limits

- **Concurrency.** Writes go through one connection per call, as before. There is no pool in the scheduler yet, so expect SQLite-like concurrency, not more. `BEGIN IMMEDIATE` sections are serialised database-wide by one advisory lock, which is what SQLite does too.
- **`date()` comparisons.** The translation compares against a `date`. A column declared `TEXT` that holds dates compares as text in SQLite and fails on PostgreSQL. The route smoke found none, but the write paths of every job have not all been run on PostgreSQL.
- **Rolling back** is switching the three settings back. SQLite stays untouched, but anything written while on PostgreSQL stays there.
