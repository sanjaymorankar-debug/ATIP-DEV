# ATIP database scripts

A fresh, empty ATIP database: every table, index and append-only trigger, plus the
seed rows of `weight_config` (default scoring weights) and `schema_migrations` (so
`ops.migrations` sees the schema as current).

**Generated, not hand-written.** `tools/schema_sql.py` builds an empty database with
ATIP's own start-up code — `init_db()` + `seed_weights()` + `ops.migrations.apply()` +
the order / paper / signal / strategy / recovery tables those modules create on first
use — and writes both files from it. `tests/test_schema_sql.py` fails when a checked-in
file no longer matches, so after any schema change run:

```bash
python tools/schema_sql.py            # rewrite both files
python tools/schema_sql.py --check    # exit 1 if either is stale
```

| File | Engine | Use |
|---|---|---|
| `atip_schema.postgresql.sql` | PostgreSQL 16 — **ATIP's server database** (DBS-05) | `createdb atip && psql -d atip -v ON_ERROR_STOP=1 -f db/sql/atip_schema.postgresql.sql`, then switch the runtime over per `docs/POSTGRESQL_MIGRATION.md` step 4. Its DDL is `tools/sqlite_to_postgres.plan()` — `db.postgres.ddl()` for tables and indexes, `trigger_ddl()` for the append-only guards — i.e. exactly what a data migration creates. A shortcut rather than a necessity: `main.py --init` builds the same schema on PostgreSQL itself. |
| `atip_schema.sqlite.sql` | SQLite — the default runtime | Pre-build `atip_data/atip.db` (`sqlite3 atip_data/atip.db < db/sql/atip_schema.sqlite.sql`). Normally unnecessary: `python main.py --init` / first start does the same. |

Both are for **empty** databases. To move existing data use
`tools/sqlite_to_postgres.py` on a backup copy (`docs/POSTGRESQL_MIGRATION.md`).

**MySQL is no longer supported.** PostgreSQL is ATIP's one server database target;
the MySQL backend (`db/mysql.py`), its migration and phpMyAdmin export tools and
`atip_schema.mysql.sql` were removed on 2026-10-09. A `mysql://` URL is refused.

Verified (2026-10-09): `atip_schema.postgresql.sql` loads into an empty PostgreSQL
16.15 database with `ON_ERROR_STOP` (250 tables, 16 triggers, 105 weight rows), and
`init_db()` + `ops.migrations` pointed at the result add nothing and report no
pending or drifted migration; re-running `init_db()` on a database built from the
SQLite file changes nothing.
