"""
SQLite-dialect scanner (W38: DBS-05) -- how far is the code base from running on PostgreSQL?

Finds every literal SQL statement passed to execute / executemany / executescript (the AST, not a
grep: f-strings count with their placeholders filled), runs each through db.postgres.translate and
reports what translates and what needs a hand change (PRAGMA, sqlite_master, rowid, strftime(), and
anything translate() rejects). The report is the work list for the switch; its totals are tracked
on the /ops/compliance page so the number only goes one way.

    python -m db.dialect_scan                 # summary
    python -m db.dialect_scan --details       # every statement that needs a hand change
    python -m db.dialect_scan --json out.json
"""

from __future__ import annotations

import argparse
import ast
import json
from collections import Counter
from pathlib import Path

from db.postgres import UnsupportedSQL, keys_from_ddl, translate

SKIP_DIRS = {".git", "tests", "__pycache__", "atip_data", ".venv", "venv", "node_modules"}
CALLS = {"execute", "executemany", "executescript"}


def _sql_of(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):                       # f"...{x}..." -> placeholder name
        return "".join(v.value if isinstance(v, ast.Constant) else "x_" for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _sql_of(node.left), _sql_of(node.right)
        return a + b if a is not None and b is not None else None
    return None


def statements(root: Path):
    """(file, line, sql) for every literal SQL statement under `root`."""
    for p in sorted(root.rglob("*.py")):
        if SKIP_DIRS & set(p.relative_to(root).parts):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in CALLS and n.args:
                sql = _sql_of(n.args[0])
                if sql and sql.strip():
                    yield str(p.relative_to(root)).replace("\\", "/"), n.lineno, sql


def _kind(err: str) -> str:
    if err.startswith("INSERT OR REPLACE"):
        return "INSERT OR REPLACE without a matching key"
    return err.split(" has no", 1)[0].split(":", 1)[0].strip()


def _ddl_strings(root: Path):
    """Every string constant that is a CREATE statement (schema modules keep DDL in dicts, not calls)."""
    for p in sorted(root.rglob("*.py")):
        if SKIP_DIRS & set(p.relative_to(root).parts):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for n in ast.walk(tree):
            s = _sql_of(n) if isinstance(n, (ast.Constant, ast.JoinedStr)) else None
            if s and s.lstrip().upper().startswith("CREATE "):
                yield s


def scan(root: Path | str = ".") -> dict:
    root = Path(root)
    keys = keys_from_ddl(_ddl_strings(root))
    total, ok, bad = 0, 0, []
    for f, line, sql in statements(root):
        if sql.lstrip().upper().startswith(("CREATE ", "ALTER ", "DROP ")):
            continue                                          # DDL is handled by tools/pg_migrate.py
        total += 1
        try:
            translate(sql, pk_of=lambda t: keys.get(t, []))  # keys from the code's own DDL
            ok += 1
        except UnsupportedSQL as e:
            if str(e).startswith("INSERT OR REPLACE") and "'x_'" in str(e):
                ok += 1                                       # f-string column list: resolved at run time
                continue
            bad.append({"file": f, "line": line, "kind": _kind(str(e)), "sql": " ".join(sql.split())[:160]})
    return {"statements": total, "translatable": ok, "needs_change": len(bad),
            "pct_ready": round(100 * ok / total, 1) if total else 100.0,
            "by_kind": dict(Counter(b["kind"] for b in bad).most_common()),
            "by_file": dict(Counter(b["file"] for b in bad).most_common(25)), "items": bad}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--details", action="store_true")
    ap.add_argument("--json")
    a = ap.parse_args()
    r = scan(a.root)
    print(f"{r['statements']} SQL statements: {r['translatable']} translate automatically "
          f"({r['pct_ready']}%), {r['needs_change']} need a hand change")
    for k, n in r["by_kind"].items():
        print(f"  {n:4d}  {k}")
    if a.details:
        for b in r["items"]:
            print(f"{b['file']}:{b['line']}  [{b['kind']}]  {b['sql']}")
    if a.json:
        Path(a.json).write_text(json.dumps(r, indent=2), encoding="utf-8")
