# ATIP database scripts

A fresh, empty ATIP database (216 tables + seed `weight_config` and
`schema_migrations`), generated from master by running ATIP's own code:
`init_db()` + `seed_weights()` + `ops.migrations.apply()` + the order / paper /
signal / strategy / recovery tables those modules create on first use.

| File | Engine | Use |
|---|---|---|
| `atip_schema.sqlite.sql` | SQLite — **production** | Pre-build `atip_data/atip.db` (`sqlite3 atip_data/atip.db < db/sql/atip_schema.sqlite.sql`). Normally unnecessary: `python main.py --init` / first start does the same. |
| `atip_schema.postgresql.sql` | PostgreSQL 16 — experimental (DBS-05) | `psql -d atip -v ON_ERROR_STOP=1 -f db/sql/atip_schema.postgresql.sql`, then switch over per `docs/POSTGRESQL_MIGRATION.md` step 4. Needed because `main.py --init` cannot build a PostgreSQL database itself (`init_db()` uses `conn.cursor()`, which `PgConnection` lacks). |
| `atip_schema.mysql.sql` | MySQL 8 / MariaDB | phpMyAdmin import of an **empty reporting copy** (tables prefixed `atip_`). ATIP cannot run on MySQL; for real data use `tools/export_mysql.py`. |

All three are for **empty** databases. To move existing data use
`tools/sqlite_to_postgres.py` or `tools/export_mysql.py` on a backup copy.

Verified: re-running `init_db()` on a database built from the SQLite file changes
nothing; the PostgreSQL file restores into PG 16 identically to what
`tools/sqlite_to_postgres.py --execute` produced; the MySQL file imports
into MariaDB 10.11 with 216 tables and 105 weight rows.
