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
import re
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


# ── reserved identifiers ──────────────────────────────────────────────────
#
# MySQL 8 reserves six of the column names ATIP declares. Unquoted, each is
# errno 1064 -- the single most common failure when the runtime moved to MySQL
# (73 of 164 statement errors, plus the lock-wait and duplicate-key cascades that
# followed from the transactions they left open). Backticks are portable: SQLite
# accepts them for MySQL compatibility and db.postgres.translate turns them into
# double quotes, so one statement serves all three backends.
MYSQL_RESERVED = ("change", "key", "rank", "rows", "signal", "trigger")

# A word is already quoted after a backtick (MySQL/SQLite) or a double quote
# (PostgreSQL); KEY in PRIMARY KEY, RANK() OVER and ROWS BETWEEN are syntax.
_RESERVED_RX = re.compile(rf"(?<![`\"\w])(?:{'|'.join(MYSQL_RESERVED)})(?![`\"\w])", re.I)
_STARTS_SQL = re.compile(r"^\s*(SELECT|INSERT\s+(INTO|OR)|UPDATE|DELETE\s+FROM|REPLACE\s+INTO)\b", re.I)

# A bare column list, handed to a helper that splices it into a SELECT -- e.g.
# research/screener.py's _latest_rows(conn, table, "t.atip_score, t.signal", ...).
# It carries no verb, so the check above cannot see it. The shape is strict: only
# comma-separated identifiers, optionally qualified, and nothing else -- which no
# prose string in the code base matches.
_COLUMN_LIST = re.compile(r"^\s*[\w`]+(?:\.[\w`]+)?(?:\s*,\s*[\w`]+(?:\.[\w`]+)?)+\s*$")
_IS_STATEMENT = re.compile(r"\b(FROM|INTO|UPDATE|SET)\b", re.I)


def _reserved_is_syntax(sql: str, m) -> bool:
    before = sql[max(0, m.start() - 24):m.start()].rstrip().upper()
    after = sql[m.end():m.end() + 16].lstrip().upper()
    return bool(re.search(r"\b(PRIMARY|FOREIGN|UNIQUE|DUPLICATE)$", before)
                or re.search(r"\b(CREATE|DROP)$", before)
                or re.search(r"^\(\s*\)\s*OVER", after)
                or re.search(r"^(BETWEEN|UNBOUNDED|CURRENT)\b", after))


def reserved_identifiers(root: Path):
    """(file, line, word, excerpt) for each MySQL-reserved word used unquoted.

    Every string literal that reads as a SQL statement is checked, not just the
    ones passed straight to execute(): ATIP also builds SQL in a variable and
    hands it to helpers like dashboard's _rows(), and those sites failed exactly
    the same way. Bare column lists spliced into a SELECT are checked too, since
    they carry no verb to recognise. DDL is skipped -- db.mysql.ddl() quotes its
    own identifiers.
    """
    for p in sorted(root.rglob("*.py")):
        if SKIP_DIRS - {"tests"} & set(p.relative_to(root).parts):
            continue
        try:
            txt = p.read_text(encoding="utf-8")
            tree = ast.parse(txt)
        except (SyntaxError, UnicodeDecodeError):
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.JoinedStr):
                sql = "".join(v.value if isinstance(v, ast.Constant) else " x " for v in n.values)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str):
                sql = n.value
            else:
                continue
            is_statement = _STARTS_SQL.match(sql) and _IS_STATEMENT.search(sql)
            if not (is_statement or _COLUMN_LIST.match(sql)):
                continue                                   # prose that opens with a verb is not SQL
            if re.match(r"\s*(CREATE|ALTER|DROP|PRAGMA)\b", sql, re.I):
                continue
            for m in _RESERVED_RX.finditer(sql):
                if sql[:m.start()].count("'") % 2 or _reserved_is_syntax(sql, m):
                    continue
                yield (str(p.relative_to(root)).replace("\\", "/"), n.lineno, m.group(0),
                       " ".join(sql[max(0, m.start() - 40):m.end() + 24].split()))

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
