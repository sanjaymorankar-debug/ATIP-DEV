"""
SQLite -> PostgreSQL migration tool (W9, DBS-05). Runs against COPIES only.

    python tools/sqlite_to_postgres.py --source <copy.db> [--out DIR]            dry run (default)
    python tools/sqlite_to_postgres.py --source <copy.db> --target postgresql://... --execute

Dry run: reads the copy's schema and row counts, translates every CREATE TABLE /
CREATE INDEX with db/backend.translate(), writes <out>/schema.postgresql.sql and
<out>/plan.json (table order, rows, untranslatable statements) -- no database is
contacted. Execute: needs psycopg; creates the schema, copies rows in batches of
1000 per table in one transaction per table, then verifies row counts per table.

Safety: refuses a --source that is the live database (db.schema.DB_PATH); make a
copy first (python -m ops backup, or python -m ops restore <id> --target <file>).
The ATIP runtime keeps using SQLite either way.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def plan(source: Path) -> dict:
    from db.backend import translate
    from db.postgres import APPEND_ONLY_FN, UnsupportedSQL, trigger_ddl
    c = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        objs = c.execute("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE "
                         "'sqlite_%' ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END, name"
                         ).fetchall()
        tables, ddl, skipped = [], [], []
        for typ, name, tbl, sql in objs:
            if typ == "trigger":
                # W38: the append-only guards become PostgreSQL row triggers; anything else is listed
                try:
                    stmt = trigger_ddl(sql)
                except UnsupportedSQL as e:
                    skipped.append({"name": name, "reason": str(e)})
                    continue
                if APPEND_ONLY_FN + ";" not in ddl:
                    ddl.append(APPEND_ONLY_FN + ";")
                ddl.append(stmt + ";")
                continue
            if typ == "view":
                skipped.append({"name": name, "reason": "view: review manually"})
                continue
            stmt = translate(sql)
            if typ == "index":
                stmt = re.sub(r"CREATE (UNIQUE )?INDEX (?!IF NOT EXISTS)", r"CREATE \1INDEX IF NOT EXISTS ", stmt)
            else:
                stmt = re.sub(r"CREATE TABLE (?!IF NOT EXISTS)", "CREATE TABLE IF NOT EXISTS ", stmt)
                n = c.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                tables.append({"table": name, "rows": n})
            ddl.append(stmt.rstrip(";") + ";")
        return {"source": str(source), "tables": tables, "ddl": ddl, "skipped": skipped,
                "total_rows": sum(t["rows"] for t in tables)}
    finally:
        c.close()


def execute(source: Path, target: str, p: dict) -> dict:
    from db.backend import PgConnection
    pg = PgConnection(target)
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    results = []
    try:
        for stmt in p["ddl"]:
            pg.execute(stmt)
        pg.commit()
        for t in p["tables"]:
            name = t["table"]
            cols = [r[1] for r in src.execute(f'PRAGMA table_info("{name}")')]
            ph = ",".join("?" * len(cols))
            cur = src.execute(f'SELECT {",".join(chr(34) + x + chr(34) for x in cols)} FROM "{name}"')
            while True:
                batch = cur.fetchmany(1000)
                if not batch:
                    break
                pg.executemany(f'INSERT INTO "{name}" ({",".join(chr(34) + x + chr(34) for x in cols)}) VALUES ({ph})',
                               batch)
            if "id" in cols and re.search(rf'CREATE TABLE IF NOT EXISTS "?{name}"?\s*\([^;]*\bid BIGINT GENERATED',
                                          "\n".join(p["ddl"])):
                pg.reset_identity(name)                     # W38: new rows must not reuse copied ids
            pg.commit()
            n = pg.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            results.append({"table": name, "source_rows": t["rows"], "target_rows": n, "ok": n == t["rows"]})
    finally:
        src.close()
        pg.close()
    return {"tables": results, "ok": all(r["ok"] for r in results)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--out", default="atip_data/pg_migration")
    ap.add_argument("--target")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--no-upgrade", action="store_true", help="do not apply this release's schema to the copy first")
    a = ap.parse_args(argv)
    from db.schema import DB_PATH
    source = Path(a.source)
    if source.resolve() == Path(DB_PATH).resolve():
        print("refused: --source is the live database; migrate a copy (python -m ops backup)")
        return 2
    if not source.exists():
        print(f"no such file {source}")
        return 2
    if not a.no_upgrade:
        # W38: bring the COPY to this release's schema first (what init_db does at start-up on SQLite);
        # otherwise tables / columns added since the copy was taken are missing on PostgreSQL
        import os
        import subprocess
        env = {**os.environ, "ATIP_DB_PATH": str(source.resolve())}
        r = subprocess.run([sys.executable, "-c", "from db.schema import init_db; init_db()"], env=env,
                           cwd=str(Path(__file__).resolve().parents[1]), capture_output=True, text=True)
        if r.returncode:
            print(f"schema upgrade of the copy failed:\n{r.stderr[-2000:]}")
            return 1
    p = plan(source)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "schema.postgresql.sql").write_text("\n\n".join(p["ddl"]) + "\n", encoding="utf-8")
    (out / "plan.json").write_text(json.dumps({k: v for k, v in p.items() if k != "ddl"}, indent=2), encoding="utf-8")
    print(json.dumps({"tables": len(p["tables"]), "rows": p["total_rows"], "skipped": len(p["skipped"]),
                      "written": [str(out / "schema.postgresql.sql"), str(out / "plan.json")]}, indent=2))
    if not a.execute:
        print("dry run only (add --target postgresql://... --execute to copy)")
        return 0
    if not a.target:
        print("--execute needs --target")
        return 2
    r = execute(source, a.target, p)
    print(json.dumps(r, indent=2))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
