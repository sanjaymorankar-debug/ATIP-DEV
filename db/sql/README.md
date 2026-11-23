# ATIP database scripts

A fresh, empty ATIP database: every table, index and append-only trigger, plus the
seed rows of `weight_config` (default scoring weights) and `schema_migrations` (so
`ops.migrations` sees the schema as current).

**Generated, not hand-written.** `tools/schema_sql.py` builds an empty database with
ATIP's own start-up code — `init_db()` + `seed_weights()` + `ops.migrations.apply()` +
every table a running ATIP creates on first use (its `ON_FIRST_USE` / `DDL_ONLY` lists)
— and writes both files from it. `tests/test_schema_sql.py` fails when a checked-in
file no longer matches, or when a module that creates a table is missing from those
lists, so after any schema change run:

```bash
python tools/schema_sql.py            # rewrite both files
python tools/schema_sql.py --check    # exit 1 if either is stale
```

| File | Engine | Use |
|---|---|---|
| `atip_schema.postgresql.sql` | PostgreSQL 16 — **ATIP's server database** (the VPS, DBS-05) | `createdb atip && psql -d atip -v ON_ERROR_STOP=1 -f db/sql/atip_schema.postgresql.sql`, then switch the runtime over per `docs/POSTGRESQL_MIGRATION.md` step 4. Its DDL is `tools/sqlite_to_postgres.plan()` — `db.postgres.ddl()` for tables and indexes, `trigger_ddl()` for the append-only guards — i.e. exactly what a data migration creates. A shortcut rather than a necessity: `main.py --init` builds the schema on PostgreSQL itself. |
| `atip_schema.sqlite.sql` | SQLite — the default local runtime | Pre-build `atip_data/atip.db` (`sqlite3 atip_data/atip.db < db/sql/atip_schema.sqlite.sql`). Normally unnecessary: `python main.py --init` / first start does the same. |

Both are for **empty** databases. To move existing data use
`tools/sqlite_to_postgres.py` on a backup copy (`docs/POSTGRESQL_MIGRATION.md`).

**MySQL is no longer supported.** PostgreSQL is ATIP's one server database; the MySQL
backend (`db/mysql.py`), its migration and phpMyAdmin export tools
(`tools/sqlite_to_mysql.py`, `tools/export_mysql.py`) and `atip_schema.mysql.sql` were
removed on 23 Nov 2026. A `mysql://` URL or a `config.json` `"database": {"backend": "mysql"}`
is refused with an error; ATIP does not fall back to SQLite while one is set.

Generated 23 Nov 2026 from master `24398e9` + the MySQL removal: 290 tables, 129 indexes,
16 append-only triggers, 106 `weight_config` rows, migrations 0001–0008.
