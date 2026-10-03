"""
SQLite -> MySQL / MariaDB migration tool. Runs against COPIES only.

    python tools/sqlite_to_mysql.py --source <copy.db> [--out DIR]           dry run (default)
    python tools/sqlite_to_mysql.py --source <copy.db> --target mysql://... --execute

Dry run: reads the copy's schema and row counts, translates every CREATE TABLE and
CREATE INDEX with db/mysql.py, scans the stored values for anything MySQL would
reject, and writes <out>/schema.mysql.sql and <out>/plan.json. No database is
contacted. Execute: needs PyMySQL; creates the schema, copies rows in batches of
1000 (one transaction per table), resets AUTO_INCREMENT, then verifies row counts.

Safety: refuses a --source that is the live database (db.schema.DB_PATH); make a
copy first (python -m ops backup, or python -m ops restore <id> --target <file>).
The ATIP runtime keeps using SQLite either way.

HOW THIS DIFFERS FROM tools/export_mysql.py
    export_mysql.py writes .sql files to import by hand in phpMyAdmin, and infers
    each column's type from the data it finds. This connects to a server and uses
    **db/mysql.py's own ddl()** - the same translation the MySQL backend applies at
    run time - so the database it produces is the one ATIP's MySQL queries are
    written against. Use export_mysql.py for shared hosting with no remote MySQL
    access; use this when you can reach the server.

WHY THE WHOLE STATEMENT LIST GOES TO keyed_columns() AT ONCE
    MySQL cannot index or key a TEXT column without a prefix length, so db/mysql.py
    widens any column that takes part in a key to VARCHAR(191) - but only if it is
    told which those are. A CREATE INDEX later in the file can key a column whose
    CREATE TABLE says nothing about it (alert_log.dedupe_key is one, and
    enterprise_user has a TEXT PRIMARY KEY), so the keys are collected across every
    statement before any of them is translated. Translating one statement at a time
    produces `dedupe_key TEXT` and the index then fails with errno 1170.

WHAT IT WILL NOT DO SILENTLY
    SQLite lets any column hold any type, so a column declared INTEGER can contain
    a float, or text, or both. MySQL in STRICT_TRANS_TABLES rejects those. Every
    such value is counted in the plan under "coercions", and:
      * lossless ones are converted (a float holding 7.0 into a BIGINT column, an
        empty string into a NULL date);
      * lossy ones (a fractional float into BIGINT, text into a numeric column, an
        unparseable date) stop the migration unless --allow-lossy is given, and are
        then rounded or nulled - and counted, so the number is in the output.
    Nothing is dropped without appearing in the report.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BATCH = 1000

#: MySQL's "duplicate key name" - a CREATE INDEX for an index that already exists.
ER_DUP_KEYNAME = 1061


def _errno(exc) -> int | None:
    """PyMySQL puts the server errno in args[0]."""
    args = getattr(exc, "args", ())
    return args[0] if args and isinstance(args[0], int) else None

#: Target types whose columns cannot hold arbitrary SQLite values.
_INT_TYPES = ("BIGINT", "INT", "SMALLINT", "TINYINT")
_FLOAT_TYPES = ("DOUBLE", "FLOAT", "DECIMAL")
_DATE_TYPES = ("DATE", "DATETIME", "TIMESTAMP")


def _target_types(ddl_stmt: str) -> dict:
    """{column: MySQL type} parsed out of a generated CREATE TABLE.

    Read back from the DDL this tool just produced rather than re-deriving it, so
    the scan and the copy can never disagree about what a column will be.
    """
    out = {}
    body = ddl_stmt[ddl_stmt.index("(") + 1:ddl_stmt.rindex(")")]
    for line in body.split("\n"):
        m = re.match(r"\s*`([^`]+)`\s+([A-Za-z]+(?:\(\d+(?:,\s*\d+)?\))?)", line)
        if m:
            out[m.group(1)] = m.group(2).upper()
    return out


def _kind(mysql_type: str) -> str:
    base = mysql_type.split("(")[0]
    if base in _INT_TYPES:
        return "int"
    if base in _FLOAT_TYPES:
        return "float"
    if base in _DATE_TYPES:
        return "date"
    return "text"


def _scan_column(c: sqlite3.Connection, table: str, col: str, kind: str) -> dict:
    """Counts stored values that MySQL would not accept for this column's type."""
    q = lambda s: '"' + s.replace('"', '""') + '"'
    t, cc = q(table), q(col)
    if kind == "int":
        # A whole-valued float converts losslessly; anything else does not.
        #
        # SQLite's type affinity normally prevents the lossless case from arising
        # at all: storing 5.0 in a column declared INTEGER or NUMERIC writes the
        # integer 5, so only a *fractional* real survives there. It stays a real
        # only in a column with BLOB affinity or no declared type - ATIP has one
        # BLOB column and no untyped ones, so the counter is insurance against a
        # column being added without a type rather than a case seen today.
        row = c.execute(
            f"SELECT SUM(typeof({cc})='real' AND {cc}={cc}*1.0 AND CAST({cc} AS INTEGER)={cc}),"
            f"       SUM(typeof({cc})='real' AND CAST({cc} AS INTEGER)<>{cc}),"
            f"       SUM(typeof({cc}) IN ('text','blob')) FROM {t}").fetchone()
        return {"lossless_float": row[0] or 0, "fractional_float": row[1] or 0, "non_numeric": row[2] or 0}
    if kind == "float":
        row = c.execute(f"SELECT SUM(typeof({cc}) IN ('text','blob')) FROM {t}").fetchone()
        return {"non_numeric": row[0] or 0}
    if kind == "date":
        # SQLite keeps dates as text; date() returns NULL for anything it cannot read.
        row = c.execute(
            f"SELECT SUM({cc} = ''),"
            f"       SUM({cc} IS NOT NULL AND {cc} <> '' AND date({cc}) IS NULL) FROM {t}").fetchone()
        return {"empty_string": row[0] or 0, "unparseable": row[1] or 0}
    return {}


def plan(source: Path, scan: bool = True) -> dict:
    from db import mysql
    c = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        objs = c.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
            "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END, name").fetchall()

        # Every statement, before any is translated - see the module docstring.
        keyed = mysql.keyed_columns([sql for _, _, _, sql in objs])

        tables, ddl, skipped, types, coercions, notnull = [], [], [], {}, {}, {}
        for typ, name, tbl, sql in objs:
            if typ == "trigger":
                try:
                    ddl.append(mysql.trigger_ddl(sql).rstrip(";") + ";")
                except mysql.UnsupportedSQL as e:
                    skipped.append({"name": name, "kind": "trigger", "reason": str(e)})
                continue
            if typ == "view":
                skipped.append({"name": name, "kind": "view", "reason": "view: review manually"})
                continue
            try:
                stmt = mysql.ddl(sql, keyed.get(name if typ == "table" else tbl, set()))
            except mysql.UnsupportedSQL as e:
                skipped.append({"name": name, "kind": typ, "reason": str(e)})
                continue
            if typ == "index":
                # No `IF NOT EXISTS` here: MySQL has no such clause for CREATE
                # INDEX (MariaDB does), and db/mysql.py strips the one SQLite's
                # DDL carries. Re-running is made idempotent in execute() instead,
                # by tolerating errno 1061, "duplicate key name".
                pass
            else:
                stmt = re.sub(r"CREATE TABLE (?!IF NOT EXISTS)", "CREATE TABLE IF NOT EXISTS ", stmt)
                n = c.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                tables.append({"table": name, "rows": n})
                types[name] = _target_types(stmt)
                notnull[name] = {r[1] for r in c.execute(f'PRAGMA table_info("{name}")') if r[3]}
            ddl.append(stmt.rstrip(";") + ";")

        if scan:
            for t in tables:
                if not t["rows"]:
                    continue
                name = t["table"]
                found = {}
                for col, mt in types[name].items():
                    k = _kind(mt)
                    if k == "text":
                        continue
                    counts = {kk: v for kk, v in _scan_column(c, name, col, k).items() if v}
                    if not counts:
                        continue
                    entry = {"target": mt, **counts}
                    # A value that has to become NULL in a NOT NULL column cannot
                    # be migrated at all: SQLite accepted '' in a NOT NULL DATE
                    # because '' is not NULL, and MySQL has no way to store a date
                    # that is not a date. Only a person can decide what it meant.
                    if col in notnull.get(name, set()):
                        blocking = counts.get("empty_string", 0) + counts.get("unparseable", 0) \
                                   + counts.get("non_numeric", 0)
                        if blocking:
                            entry["blocking"] = blocking
                            entry["not_null"] = True
                    found[col] = entry
                if found:
                    coercions[name] = found

        return {"source": str(source), "tables": tables, "ddl": ddl, "skipped": skipped,
                "types": types, "coercions": coercions,
                "not_null": {t: sorted(cols) for t, cols in notnull.items()},
                "total_rows": sum(t["rows"] for t in tables)}
    finally:
        c.close()


def lossy_total(coercions: dict) -> int:
    """Values that cannot be converted without changing them."""
    return sum(v.get("fractional_float", 0) + v.get("non_numeric", 0) + v.get("unparseable", 0)
               + v.get("empty_string", 0)
               for cols in coercions.values() for v in cols.values())


def blocking(coercions: dict) -> list:
    """Values that cannot be migrated at all, with the query that finds them.

    These are not a matter of precision: the only value MySQL would accept is
    NULL and the column forbids it. `--allow-lossy` does not override this.
    """
    out = []
    for table, cols in coercions.items():
        for col, v in cols.items():
            if not v.get("blocking"):
                continue
            k = _kind(v["target"])
            test = (f"date(\"{col}\") IS NULL" if k == "date"
                    else f"typeof(\"{col}\") IN ('text','blob')")
            out.append({"table": table, "column": col, "target": v["target"], "rows": v["blocking"],
                        "find": f'SELECT rowid, "{col}" FROM "{table}" '
                                f'WHERE "{col}" IS NOT NULL AND {test};'})
    return out


def _convert(value, kind: str, counts: dict):
    """One stored value as MySQL will accept it, counting anything changed."""
    if value is None:
        return None
    if kind == "int" and isinstance(value, float):
        if float(value).is_integer():
            counts["lossless_float"] = counts.get("lossless_float", 0) + 1
            return int(value)
        counts["rounded"] = counts.get("rounded", 0) + 1
        return round(value)
    if kind in ("int", "float") and isinstance(value, (str, bytes)):
        try:
            return int(value) if kind == "int" else float(value)
        except (TypeError, ValueError):
            counts["nulled_non_numeric"] = counts.get("nulled_non_numeric", 0) + 1
            return None
    if kind == "date" and isinstance(value, str) and value.strip() == "":
        counts["nulled_empty"] = counts.get("nulled_empty", 0) + 1
        return None
    return value


def execute(source: Path, target: str, p: dict, allow_lossy: bool = False) -> dict:
    from db.backend import MySQLConnection
    my = MySQLConnection(target)
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    results, changed = [], {}
    try:
        for stmt in p["ddl"]:
            try:
                my.execute(stmt)
            except Exception as e:
                # 1061 duplicate key name: the index is already there, which is
                # what `CREATE INDEX IF NOT EXISTS` would have meant if MySQL had
                # it. Anything else is a real failure.
                if _errno(e) != ER_DUP_KEYNAME:
                    raise
        my.commit()

        for t in p["tables"]:
            name = t["table"]
            cols = [r[1] for r in src.execute(f'PRAGMA table_info("{name}")')]
            kinds = [_kind(p["types"][name].get(col, "TEXT")) for col in cols]
            needs_convert = any(k != "text" for k in kinds) and name in p["coercions"]
            quoted = ",".join(f'`{x}`' for x in cols)
            ph = ",".join("?" * len(cols))
            counts: dict = {}

            cur = src.execute(f'SELECT {",".join(chr(34) + x + chr(34) for x in cols)} FROM "{name}"')
            while True:
                batch = cur.fetchmany(BATCH)
                if not batch:
                    break
                if needs_convert:
                    batch = [tuple(_convert(v, k, counts) for v, k in zip(row, kinds)) for row in batch]
                my.executemany(f"INSERT INTO `{name}` ({quoted}) VALUES ({ph})", batch)
            # A copied explicit id must not be handed out again to a new row.
            if "id" in cols and "AUTO_INCREMENT" in "\n".join(
                    s for s in p["ddl"] if re.search(rf"CREATE TABLE IF NOT EXISTS `{re.escape(name)}`", s)):
                my.reset_identity(name)
            my.commit()
            if counts:
                changed[name] = counts
            n = my.execute(f"SELECT COUNT(*) FROM `{name}`").fetchone()[0]
            results.append({"table": name, "source_rows": t["rows"], "target_rows": n, "ok": n == t["rows"]})
    finally:
        src.close()
        my.close()
    return {"tables": results, "converted": changed, "ok": all(r["ok"] for r in results)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--out", default="atip_data/mysql_migration")
    ap.add_argument("--target")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--allow-lossy", action="store_true",
                    help="convert values MySQL would reject, rounding or nulling them (counted in the output)")
    ap.add_argument("--no-scan", action="store_true", help="skip the stored-value scan (faster on a large copy)")
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
        # Bring the COPY to this release's schema first, which is what init_db does
        # at start-up on SQLite; otherwise tables added since the copy was taken
        # are simply missing on MySQL.
        import os
        import subprocess
        env = {**os.environ, "ATIP_DB_PATH": str(source.resolve())}
        r = subprocess.run([sys.executable, "-c", "from db.schema import init_db; init_db()"], env=env,
                           cwd=str(Path(__file__).resolve().parents[1]), capture_output=True, text=True)
        if r.returncode:
            print(f"schema upgrade of the copy failed:\n{r.stderr[-2000:]}")
            return 1

    p = plan(source, scan=not a.no_scan)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "schema.mysql.sql").write_text("\n\n".join(p["ddl"]) + "\n", encoding="utf-8")
    (out / "plan.json").write_text(
        json.dumps({k: v for k, v in p.items() if k != "ddl"}, indent=2, default=str), encoding="utf-8")

    lossy = lossy_total(p["coercions"])
    blocked = blocking(p["coercions"])
    print(json.dumps({"tables": len(p["tables"]), "rows": p["total_rows"], "skipped": len(p["skipped"]),
                      "columns_needing_coercion": sum(len(v) for v in p["coercions"].values()),
                      "lossy_values": lossy,
                      "unmigratable_values": sum(b["rows"] for b in blocked),
                      "written": [str(out / "schema.mysql.sql"), str(out / "plan.json")]}, indent=2))
    if blocked:
        print("\nThese values cannot be migrated: the column forbids NULL and MySQL has no other")
        print("value for them. --allow-lossy does not override this. Fix them in the copy first:")
        for b in blocked:
            print(f"  {b['table']}.{b['column']} ({b['target']}, NOT NULL): {b['rows']} row(s)")
            print(f"      {b['find']}")

    if not a.execute:
        print("dry run only (add --target mysql://... --execute to copy)")
        return 0
    if not a.target:
        print("--execute needs --target")
        return 2
    if blocked:
        print(f"\nrefused: {sum(b['rows'] for b in blocked)} stored values cannot be migrated (listed above)")
        return 4
    if lossy and not a.allow_lossy:
        print(f"refused: {lossy} stored values cannot be converted without changing them "
              f"(see coercions in plan.json). Clean the source, or re-run with --allow-lossy.")
        return 3

    r = execute(source, a.target, p, allow_lossy=a.allow_lossy)
    print(json.dumps(r, indent=2))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
