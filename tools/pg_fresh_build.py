"""Build a brand-new ATIP database on PostgreSQL from scratch, prove it, and compare it with another one.

    python tools/pg_fresh_build.py --url postgresql://.../atip_fresh [--compare-url postgresql://.../atip]
                                   [--workdir DIR] [--json OUT]

What a new server does on its first start, nothing more: db.schema.init_db() then ops.migrations.apply()
against the EMPTY database --url names (it must exist and hold no tables; the tool refuses otherwise, so it
can never build over a live database). Then it checks:

  * every migration APPLIED with its canonical checksum (ops.migrations.validate() is empty);
  * a second start-up applies nothing;
  * the append-only guards (migrations 0004 / 0005) refuse an UPDATE and a DELETE of enterprise_audit
    (one test row is written to the NEW database only);
  * with --compare-url: the tables and columns of the new database against that one, read in a READ ONLY
    session -- nothing is written there.

It runs from a work directory of its own (default: a temp dir) holding only atip_data/config.json with the
PostgreSQL gate, so it never reads or writes an install's atip_data/. Exit 0 = the fresh build is complete.
Added 23 Nov 2026, when a native build first passed every migration (db/postgres.py creates
atip_append_only(); ops/migrations.py has a PostgreSQL stand-in for 0006).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _masked(url: str) -> str:
    from db.backend import masked_url
    return masked_url(url)


def _psycopg():
    import psycopg
    return psycopg


def _tables(url: str, read_only: bool) -> dict:
    """{table: sorted [column, ...]} of the current schema."""
    psycopg = _psycopg()
    with psycopg.connect(url, autocommit=True) as c:
        if read_only:
            c.execute("SET default_transaction_read_only = on")
        rows = c.execute("SELECT table_name, column_name FROM information_schema.columns "
                         "WHERE table_schema = current_schema() ORDER BY table_name, ordinal_position").fetchall()
    out: dict = {}
    for t, col in rows:
        out.setdefault(t, []).append(col)
    return {t: sorted(cols) for t, cols in out.items()}


def compare(new: dict, ref: dict) -> dict:
    only_new = sorted(set(new) - set(ref))
    only_ref = sorted(set(ref) - set(new))
    cols = {}
    for t in sorted(set(new) & set(ref)):
        a, b = set(new[t]), set(ref[t])
        if a != b:
            cols[t] = {"only_new": sorted(a - b), "only_ref": sorted(b - a)}
    return {"tables_new": len(new), "tables_ref": len(ref), "only_in_new": only_new, "only_in_ref": only_ref,
            "column_differences": cols}


def build(url: str, workdir: str | None = None) -> dict:
    if not url.lower().startswith(("postgres://", "postgresql://")):
        raise SystemExit("--url must be a postgresql:// URL")
    existing = _tables(url, read_only=True)
    if existing:
        raise SystemExit(f"refusing: {_masked(url)} already holds {len(existing)} table(s) -- "
                         "this tool only builds into an EMPTY database")
    work = Path(workdir or tempfile.mkdtemp(prefix="atip-fresh-"))
    (work / "atip_data").mkdir(parents=True, exist_ok=True)
    (work / "atip_data" / "config.json").write_text(json.dumps(
        {"database": {"backend": "postgresql", "allow_experimental": True}, "broker_env": "PAPER"}))
    os.chdir(work)
    os.environ["ATIP_DATABASE_URL"] = url
    from db import schema
    from ops import migrations as M

    out = {"target": _masked(url), "workdir": str(work)}
    schema.init_db()
    conn = schema.get_connection()
    try:
        first = M.apply(conn)
        out["apply"] = [{k: r.get(k) for k in ("version", "name", "status", "error")} for r in first]
        out["schema_migrations"] = [{"version": r[0], "status": r[1]} for r in conn.execute(
            "SELECT version, status FROM schema_migrations ORDER BY version").fetchall()]
        out["validate"] = M.validate(conn)
        schema.init_db()                                           # the next start-up
        out["second_apply"] = M.apply(conn)
        out["triggers"] = conn.execute("SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal").fetchone()[0]
        conn.execute("INSERT INTO enterprise_audit (at, tenant_id, user_id, actor, action, resource, method, path) "
                     "VALUES (CURRENT_TIMESTAMP, 'fresh-build', 'tool', 'tools/pg_fresh_build.py', 'check', "
                     "'guards', 'CLI', '-')")
        conn.commit()
        guards = {}
        for sql in ("UPDATE enterprise_audit SET action='changed'", "DELETE FROM enterprise_audit"):
            try:
                conn.execute(sql)
                conn.commit()
                guards[sql.split()[0]] = "NOT REFUSED"
            except Exception as e:
                conn.rollback()
                guards[sql.split()[0]] = "refused: " + str(e).splitlines()[0][:80]
        out["guards"] = guards
    finally:
        conn.close()
    out["tables"] = len(_tables(url, read_only=True))
    applied = [m["version"] for m in out["schema_migrations"] if m["status"] == "APPLIED"]
    out["ok"] = (applied == [m[0] for m in M.MIGRATIONS] and not out["validate"] and not out["second_apply"]
                 and all(v.startswith("refused") for v in out["guards"].values()))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python tools/pg_fresh_build.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", required=True, help="an EMPTY PostgreSQL database to build ATIP into")
    ap.add_argument("--compare-url", help="a database to compare tables / columns with (read only)")
    ap.add_argument("--workdir", help="work directory (default: a new temp dir)")
    ap.add_argument("--json", help="also write the result here")
    a = ap.parse_args(argv)
    out = build(a.url, a.workdir)
    if a.compare_url:
        new = _tables(a.url, read_only=True)
        ref = _tables(a.compare_url, read_only=True)
        out["compare"] = {"reference": _masked(a.compare_url), **compare(new, ref)}
    text = json.dumps(out, indent=2, default=str)
    print(text)
    if a.json:
        Path(a.json).write_text(text)
    print("FRESH BUILD", "OK" if out["ok"] else "FAILED", file=sys.stderr)
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
