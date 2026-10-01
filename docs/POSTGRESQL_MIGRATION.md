# Moving ATIP from SQLite to PostgreSQL (DBS-05)

ATIP runs on SQLite (WAL) and **keeps doing so by default**. W38 added everything needed to move to PostgreSQL in steps, and proved it on a copy of the live database.

## What exists

| Piece | Purpose |
|---|---|
| `db/postgres.py` | `translate()` converts ATIP's SQLite SQL to PostgreSQL. `ddl()` converts CREATE statements. `trigger_ddl()` handles the append-only guards. `PgConnection` gives a sqlite3-shaped connection over psycopg 3. |
| `db/dialect_scan.py` | Scans every SQL statement in the code (via the AST) and lists what does not translate automatically. |
| `tools/sqlite_to_postgres.py` | Migrates a **copy** of the database: upgrade it to the release schema, translate the schema, copy the data, reset identity sequences, then verify row counts per table. |
| `db/schema.pg_runtime_url()` | The opt-in runtime switch (below). |

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
- `rowid` used as an ORDER BY tie-breaker becomes `ctid`. This is only valid on append-only tables, which is where ATIP uses it.
- `sqlite_master` and `PRAGMA table_info` are answered from `information_schema`.
- The connection PRAGMAs become no-ops.

Anything else raises `UnsupportedSQL` and names the construct. A missed query fails loudly rather than returning different data.

**Status (W38):**
- 1,327 statements; 99.7 % translate automatically.
- The 4 that don't belong to SQLite-only tools: `tools/export_mysql.py` and the one-off `tools/repair_news_timezone.py`.

Run `python -m db.dialect_scan --details` to see the current list.

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
4. **Switch the runtime (experimental).** All three are required:
   - `ATIP_DATABASE_URL=postgresql://…`
     - This must be the ATIP-specific variable. A generic `DATABASE_URL` left by another project is ignored on purpose.
   - In `config.json`:
     - `"database": {"backend": "postgresql", "allow_experimental": true}`
5. **Watch the first days.** `/compliance`, `/api/platform/postgres` and the job-health panel.

## Known limits

- **Concurrency.** Writes go through one connection per call, as before. There is no pool in the scheduler yet, so expect SQLite-like concurrency, not more.
- **`date()` comparisons.** The translation compares against a `date`. A column declared `TEXT` that holds dates compares as text in SQLite and fails on PostgreSQL. The route smoke found none, but the write paths of every job have not all been run on PostgreSQL.
- **Rolling back** is switching the three settings back. SQLite stays untouched, but anything written while on PostgreSQL stays there.
