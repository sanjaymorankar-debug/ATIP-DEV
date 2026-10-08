r"""
MySQL / MariaDB support -- the path off SQLite for the hosted deployment.

Sibling of db/postgres.py, built the same way and to the same contract: give the
runtime what MySQL needs without forcing it.

  translate(sql)        SQLite -> MySQL for the dialect ATIP actually uses
  ddl(create, keyed)    a CREATE TABLE / INDEX statement in MySQL types
  keyed_columns(stmts)  {table: {columns that take part in any key or index}} --
                        needed by ddl(), see "TEXT cannot be indexed" below
  trigger_ddl(sql)      ATIP's append-only guards as MySQL triggers
  keys_from_ddl(stmts)  {table: [[key columns], ...]} (shared shape with db/postgres.py)
  MySQLConnection       a sqlite3-shaped connection over PyMySQL (execute / executemany /
                        fetchone / fetchall, rows readable by index and by name like
                        sqlite3.Row), translating every statement on the way through.

Activation (db/schema.get_connection): config.json "database": {"backend": "mysql",
"allow_experimental": true} plus ATIP_DATABASE_URL=mysql://user:pass@host/db.
Requires `pip install PyMySQL` (in requirements.txt as an extra, not a core dep).

Why MySQL needs LESS translation than PostgreSQL
------------------------------------------------
Several SQLite habits are native MySQL and are deliberately left alone:
  * GROUP_CONCAT(x)      -- MySQL has it, and its default separator is ',' too
  * IFNULL(a, b)         -- native
  * LIKE                 -- MySQL's default collation is case-insensitive, so plain
                            LIKE already behaves the way SQLite's does. There is no
                            ILIKE in MySQL; rewriting to it (as the Postgres path
                            does) would be a syntax error.
  * SUM(a > b)           -- MySQL comparisons yield 0/1 exactly like SQLite's, so the
                            boolean-aggregate rewrite Postgres needs is not needed.
  * BLOB, DATETIME       -- native type names

...and where it needs MORE
--------------------------
1. TEXT cannot be indexed without a prefix length. ATIP's schema is full of
   `run_id TEXT NOT NULL, ... PRIMARY KEY (run_id, seq)` and `strategy_id TEXT
   PRIMARY KEY` (48 composite primary keys across the schema modules). MySQL
   rejects every one of them. So ddl() takes the set of columns that take part in
   any key or index for that table -- gathered across ALL statements first by
   keyed_columns(), because a CREATE INDEX later in the list can key a column the
   CREATE TABLE statement itself says nothing about -- and emits VARCHAR for those
   instead of TEXT.

   VARCHAR(191), not (255): InnoDB's index key limit is 3072 bytes and utf8mb4
   costs 4 bytes per character, so 191 chars = 764 bytes leaves room for a
   four-column composite key. 255 would overflow at three.

2. TEXT cannot carry a DEFAULT at all ("BLOB, TEXT, GEOMETRY or JSON column 'x'
   can't have a default value"). ATIP has `status TEXT NOT NULL DEFAULT 'DRAFT'`
   and friends, so a defaulted TEXT column also becomes VARCHAR.

3. Reserved words. `key`, `rank`, `interval`, `lead`, `lag` and others are column
   names in ATIP's schema and reserved in MySQL 8 / MariaDB 10.11. Identifiers are
   backtick-quoted in DDL.

4. Upserts are keyed differently. MySQL has no `ON CONFLICT (a, b) DO UPDATE`; it
   has `ON DUPLICATE KEY UPDATE`, which fires on ANY unique key. That is actually
   closer to SQLite's `INSERT OR REPLACE` than PostgreSQL is -- so translate()
   does not need pk_of() to pick a key the way the Postgres path does. `VALUES(col)`
   is used rather than the 8.0.20+ `new.col` alias, because MariaDB and MySQL < 8.0.20
   do not understand the newer form and Hostinger's MySQL version is not pinned.

5. The error classes are not the ones callers catch. PyMySQL and sqlite3 both
   implement PEP 249, but their hierarchies are unrelated, and they sort the same
   conditions into different classes -- a missing table is OperationalError in
   SQLite and ProgrammingError in PyMySQL; a CHECK violation is the reverse. ATIP's
   additive migrations are built on `except sqlite3.OperationalError`, so
   MySQLConnection raises the class SQLite would raise for the same condition,
   keeping args so args[0] is still the MySQL errno. See as_sqlite_error().

6. There is no IF NOT EXISTS for CREATE INDEX. ddl() strips the clause SQLite's DDL
   carries, so MySQLConnection honours it instead -- init_db() runs the whole schema
   on every start with no try/except around those statements, and relied on SQLite
   making the repeat a no-op.
"""

from __future__ import annotations

import contextlib
import re
import sqlite3


class UnsupportedSQL(ValueError):
    pass


# Constructs with no safe automatic MySQL translation. Kept deliberately strict --
# the Postgres path learned that silently mangling these is worse than refusing.
# rowid stays here: MySQL has no stable per-row physical identifier (no rowid, no
# ctid), so `ORDER BY rowid` cannot be rewritten. Two tables genuinely need that
# entry order, and they get a real column for it instead -- see ENTRY_ORDER_TABLES
# and db.backend.entry_order_column(); their queries ask for the column by name and
# so never reach this guard.
_UNSUPPORTED = [
    (re.compile(r"\bsqlite_master\b", re.I), "sqlite_master"),   # handled below; left for the DML guard
    (re.compile(r"\browid\b", re.I), "rowid"),
    (re.compile(r"\bstrftime\s*\(", re.I), "strftime()"),
]

# sqlite_master as MySQL sees it (type, name, tbl_name, sql) -- catalog queries run unchanged.
# MySQL's information_schema needs DATABASE() rather than current_schema().
#
# `sql` is BACKTICKED, and so is any reference a caller makes to it (see translate()):
# SQL is a reserved word in MySQL 8, so a bare `AS sql` is errno 1064 and every
# sqlite_master query -- the existence check in 13 call sites -- failed on it. That is
# the hazard this module's own rule exists for; it was written into a literal here and
# so escaped the quoting that generated identifiers get.
_SQLITE_MASTER = (
    "(SELECT 'table' AS type, TABLE_NAME AS name, TABLE_NAME AS tbl_name, CAST(NULL AS CHAR) AS `sql` "
    "FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_TYPE='BASE TABLE' "
    "UNION ALL SELECT 'view', TABLE_NAME, TABLE_NAME, VIEW_DEFINITION FROM information_schema.VIEWS "
    "WHERE TABLE_SCHEMA=DATABASE() "
    "UNION ALL SELECT DISTINCT 'index', INDEX_NAME, TABLE_NAME, CAST(NULL AS CHAR) "
    "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() "
    "UNION ALL SELECT 'trigger', TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_STATEMENT "
    "FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE()) AS sqlite_master_my")

# MySQL 8 / MariaDB 10.11 reserved words that ATIP uses, or could plausibly use, as
# identifiers. Only consulted for DDL column/table names, never for free-form SQL.
_RESERVED = {
    "key", "keys", "rank", "interval", "lead", "lag", "order", "group", "index", "range",
    "primary", "unique", "check", "default", "left", "right", "inner", "outer", "join",
    "select", "insert", "update", "delete", "from", "where", "having", "limit", "offset",
    "add", "all", "and", "as", "asc", "desc", "between", "by", "case", "cast", "column",
    "condition", "current_date", "current_time", "current_timestamp", "cursor", "database",
    "dec", "decimal", "declare", "distinct", "div", "double", "each", "else", "exists",
    "exit", "explain", "false", "float", "for", "force", "foreign", "function", "if",
    "ignore", "in", "int", "integer", "into", "is", "like", "lines", "load", "lock",
    "long", "match", "mod", "not", "null", "on", "option", "or", "out", "partition",
    "precision", "procedure", "real", "references", "regexp", "release", "rename",
    "repeat", "replace", "require", "return", "revoke", "rlike", "schema", "separator",
    "set", "show", "signal", "smallint", "specific", "sql", "ssl", "starting", "table",
    "then", "to", "trigger", "true", "union", "usage", "use", "using", "values",
    "varchar", "when", "with", "window", "write", "xor", "zerofill", "status", "system",
    "position", "signed", "stored", "virtual", "call", "change", "cross", "describe",
    "distinctrow", "elseif", "enclosed", "escaped", "fetch", "fulltext", "grant", "high_priority",
    "hour_microsecond", "hour_minute", "hour_second", "infile", "inout", "insensitive",
    "iterate", "leading", "leave", "localtime", "localtimestamp", "loop", "low_priority",
    "mediumblob", "mediumint", "mediumtext", "middleint", "natural", "no_write_to_binlog",
    "numeric", "optimize", "optionally", "outfile", "purge", "read", "reads", "recursive",
    "sensitive", "spatial", "sqlexception", "sqlstate", "sqlwarning", "straight_join",
    "terminated", "tinyblob", "tinyint", "tinytext", "trailing", "undo", "unlock",
    "unsigned", "varbinary", "varcharacter", "varying", "while", "blob", "text",
    # reserved in MariaDB 10.6+/MySQL 8 and used as column names in ATIP's schema
    "rows", "format", "over", "filter", "groups", "within", "current_role", "except",
    "intersect", "offset", "page_checksum", "slow", "statement", "returning", "body",
}


def reserved_columns(stmts) -> dict:
    """{table: [columns whose name is a MySQL/MariaDB reserved word]}.

    Generated DDL quotes every identifier, so these are safe to CREATE. Hand-written
    queries elsewhere in the code base are not: `SELECT rows FROM audit_export` is a
    syntax error on MySQL even though the table exists. This reports them so they can
    be fixed, the same way db/dialect_scan.py reports SQLite-only constructs.
    """
    out: dict = {}
    for s in stmts:
        m = re.search(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_]\w*)\s*\((.*)\)\s*;?\s*$",
                      s, re.I | re.S)
        if not m:
            continue
        table, body = m.group(1), m.group(2)
        for part in _split_top_level(body):
            p = part.strip()
            if re.match(r"(PRIMARY\s+KEY|UNIQUE|CONSTRAINT|FOREIGN\s+KEY|CHECK)\b", p, re.I):
                continue
            mm = re.match(r"([A-Za-z_]\w*)\s+\w", p)
            if mm and mm.group(1).lower() in _RESERVED:
                out.setdefault(table, []).append(mm.group(1))
    return out

# Length used for a TEXT column that MySQL will not accept as TEXT (see the module
# docstring): 191 keeps a four-column composite key inside InnoDB's 3072-byte limit.
# Tables whose row ORDER carries meaning, not just their contents. SQLite gets that
# order from its implicit rowid; MySQL has no equivalent, so ddl() gives these tables
# an AUTO_INCREMENT column and db.backend.entry_order_column() tells a query which
# name to order by on the backend it is actually running against.
#
# perf_ledger is here for a measured reason, recorded in wealth/perf/engine.py: its
# own txn_id is a random id, and ordering by it put a same-day SELL before its BUY
# about half the time, which capped the sell as EXCESS_SELL and lost the round trip.
# This is P&L correctness, not tidiness. ml_dl_benefit wants the latest row when two
# benefit checks share a created_at.
ENTRY_ORDER_TABLES = frozenset(("perf_ledger", "ml_dl_benefit"))
ENTRY_ORDER_COLUMN = "seq"

KEYED_TEXT_LEN = 191
# A TEXT column that only needs VARCHAR because it carries a DEFAULT is not in any
# index, so it can be longer.
DEFAULTED_TEXT_LEN = 255

TABLE_SUFFIX = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"


def quote(ident: str) -> str:
    """Backtick-quote an identifier.

    Every generated identifier is quoted unconditionally rather than only the ones
    on a keyword list. ATIP's schema has columns called `rows`, `status`, `key`,
    `rank` and `format`, and which of those are reserved differs between MySQL 8,
    MariaDB 10.6 and MariaDB 10.11 -- a hand-maintained list silently rots into a
    deployment failure on whatever version the host happens to run. Quoting
    everything costs nothing and cannot rot. _RESERVED is kept only for
    reserved_in_dml(), which reports the risk in hand-written queries.
    """
    i = ident.strip().strip("`\"[]")
    return f"`{i}`"


# ── PRAGMA / catalog ──────────────────────────────────────────────────────

def _table_info(t):
    """PRAGMA table_info(t) shaped as (cid, name, type, notnull, dflt_value, pk)."""
    return ("SELECT ORDINAL_POSITION-1 AS cid, COLUMN_NAME AS name, UPPER(DATA_TYPE) AS type, "
            "CASE WHEN IS_NULLABLE='NO' THEN 1 ELSE 0 END AS notnull, COLUMN_DEFAULT AS dflt_value, "
            "CASE WHEN COLUMN_KEY='PRI' THEN 1 ELSE 0 END AS pk FROM information_schema.COLUMNS "
            f"WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='{t}' ORDER BY ORDINAL_POSITION")


def _catalog(sql):
    """PRAGMAs ATIP uses -> MySQL equivalents (or harmless no-ops); None if not one of them."""
    m = re.match(r"\s*PRAGMA\s+table_info\s*\(\s*[\"']?(\w+)[\"']?\s*\)\s*;?\s*$", sql, re.I)
    if m:
        return _table_info(m.group(1))
    # MySQL/InnoDB has no equivalent knob for these; they are advisory in SQLite.
    if re.match(r"\s*PRAGMA\s+(busy_timeout|foreign_keys|synchronous|cache_size|journal_mode)\b", sql, re.I):
        if re.match(r"\s*PRAGMA\s+journal_mode\b", sql, re.I):
            return "SELECT 'wal' AS journal_mode"
        return "SELECT 1"
    if re.match(r"\s*PRAGMA\s+(integrity_check|quick_check)\s*;?\s*$", sql, re.I):
        return "SELECT 'ok' AS integrity_check"
    return None


def _split_strings(sql):
    """[(is_string_literal, text)] so rewrites never touch quoted text."""
    out, i, buf = [], 0, []
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            if buf:
                out.append((False, "".join(buf)))
                buf = []
            j = i + 1
            while j < len(sql):
                if sql[j] == "'" and (j + 1 >= len(sql) or sql[j + 1] != "'"):
                    break
                j += 2 if sql[j] in ("'", "\\") else 1
            out.append((True, sql[i:j + 1]))
            i = j + 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        out.append((False, "".join(buf)))
    return out


def _split_top_level(body, sep=","):
    """Split on commas that are not inside parentheses."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(body):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append(body[start:i])
            start = i + 1
    parts.append(body[start:])
    return parts


_UNITS = {"day": "DAY", "days": "DAY", "month": "MONTH", "months": "MONTH", "year": "YEAR",
          "years": "YEAR", "hour": "HOUR", "hours": "HOUR", "minute": "MINUTE", "minutes": "MINUTE"}


def _date_mod(m):
    """date(x, '+N days') -> DATE_ADD(x, INTERVAL N DAY) / DATE_SUB for '-'."""
    arg, sign, n, unit = m.group(1).strip(), m.group(2), m.group(3), m.group(4).lower()
    u = _UNITS.get(unit)
    if not u:
        raise UnsupportedSQL(f"date modifier unit {unit!r}")
    base = "CURRENT_DATE" if arg.lower() == "'now'" else f"CAST({arg} AS DATE)"
    fn = "DATE_ADD" if sign == "+" else "DATE_SUB"
    return f"{fn}({base}, INTERVAL {n} {u})"


def _greatest(sql):
    """MAX(a, b) / MIN(a, b) with a top-level comma -> GREATEST / LEAST (aggregates untouched)."""
    out, i = [], 0
    pat = re.compile(r"\b(MAX|MIN)\s*\(", re.I)
    while True:
        m = pat.search(sql, i)
        if not m:
            out.append(sql[i:])
            return "".join(out)
        depth, j, comma = 1, m.end(), False
        while j < len(sql) and depth:
            c = sql[j]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            elif c == "," and depth == 1:
                comma = True
            j += 1
        out.append(sql[i:m.start()])
        out.append(("GREATEST(" if m.group(1).upper() == "MAX" else "LEAST(") if comma else m.group(0))
        i = m.end()


# ── DML translation ───────────────────────────────────────────────────────

# SQLite CAST target types -> MySQL's. MySQL accepts none of SQLite's five storage
# classes as a CAST target except REAL: INTEGER, TEXT, BLOB and NUMERIC are all
# errno 1064. Verified against 8.0.46 rather than assumed.
_CAST_TYPES = {"integer": "SIGNED", "int": "SIGNED", "bigint": "SIGNED", "smallint": "SIGNED",
               "text": "CHAR", "varchar": "CHAR", "blob": "BINARY",
               "real": "REAL", "float": "FLOAT", "double": "DOUBLE"}

# The integer cast needs more than a rename: see _cast().
_CAST_TRUNCATES = frozenset(("integer", "int", "bigint", "smallint"))

# NUMERIC has no faithful MySQL target. SQLite keeps CAST(1.7 AS NUMERIC) as 1.7;
# MySQL's DECIMAL defaults to (10,0) and rounds it to 2, and DECIMAL(65,30) -- the
# only target that keeps the value -- returns it zero-padded, so the Python value
# still differs. ATIP has no such cast; raising keeps it that way rather than
# picking a mapping that is quietly wrong.
_CAST_UNSUPPORTED = frozenset(("numeric", "boolean", "bool"))


def _alias_derived(text: str) -> str:
    """Name the derived tables SQLite lets go unnamed (errno 1248).

    SQLite accepts FROM (SELECT ...) with no alias; MySQL answers "Every derived
    table must have its own alias". data/bhavcopy.py's 5-day-average UPDATE has two
    of them. The generated names are positional, so the same statement always
    translates to the same SQL.

    The whole FROM list is walked, not just the element after the keyword: a
    derived table can be any comma-separated sibling. Only a FROM / JOIN list is
    touched -- a (SELECT ...) anywhere else is a scalar subquery or an IN list,
    where an alias would itself be a syntax error.
    """
    # What may follow a derived table and still be its alias. A clause or operator
    # keyword is not an alias, so the table is unnamed and needs one.
    not_alias = ("ON|USING|WHERE|GROUP|ORDER|HAVING|LIMIT|OFFSET|UNION|JOIN|LEFT|RIGHT|"
                 "INNER|OUTER|CROSS|STRAIGHT_JOIN|SET|AND|OR|IS|IN|NOT|LIKE")
    aliased = re.compile(rf"\s*(?:AS\s+)?(?!(?:{not_alias})\b)[`A-Za-z_]\w*", re.I)

    out, i, n = [], 0, 0
    for m in re.finditer(r"\b(?:FROM|JOIN)\b", text, re.I):
        if m.start() < i:
            continue
        pos = m.end()
        while True:                                         # each element of this FROM list
            while pos < len(text) and text[pos].isspace():
                pos += 1
            if pos < len(text) and text[pos] == "(" and re.match(r"\s*SELECT\b", text[pos + 1:], re.I):
                j, depth = pos + 1, 1
                while j < len(text) and depth:
                    depth += {"(": 1, ")": -1}.get(text[j], 0)
                    j += 1
                if not aliased.match(text[j:]):
                    n += 1
                    out.append(text[i:j])
                    out.append(f" AS _d{n}")
                    i = j
                pos = j
            else:                                           # a named table, with or without an alias
                ref = re.match(rf"\s*[`\w.]+(?:\s+(?:AS\s+)?(?!(?:{not_alias})\b)[`\w]+)?",
                               text[pos:], re.I)
                if not ref:
                    break
                pos += ref.end()
            sep = re.match(r"\s*,", text[pos:])             # a sibling in the same list?
            if not sep:
                break
            pos += sep.end()
    out.append(text[i:])
    return "".join(out)


def _cast(text: str) -> str:
    """Rewrite CAST target types, parenthesis-aware (ATIP casts a ROUND()).

    The integer cast is not a rename. SQLite truncates toward zero; MySQL's
    CAST(x AS SIGNED) ROUNDS, so the one-word substitution disagrees on every
    fractional value -- 1.7 -> 2 where SQLite says 1, -1.7 -> -2 where SQLite
    says -1, 0.5 -> 1 where SQLite says 0. TRUNCATE(x, 0) reproduces SQLite
    exactly, NULL and non-numeric text included, so the expression is wrapped
    rather than the type renamed.

    ATIP's three call sites all CAST a ROUND(), where truncating and rounding
    agree -- so the plain rename would have passed today and silently skewed
    volume and delivery_qty the first time a caller dropped the ROUND(). The
    sites are data/bhavcopy.py and data/corporate_actions.py (x2).
    """
    out, i = [], 0
    for m in re.finditer(r"\bCAST\s*\(", text, re.I):
        if m.start() < i:
            continue                                        # nested in a CAST already rewritten
        j, depth = m.end(), 1
        while j < len(text) and depth:
            depth += {"(": 1, ")": -1}.get(text[j], 0)
            j += 1
        inner = text[m.end():j - 1]
        tm = re.search(r"\bAS\s+(\w+)\s*$", inner, re.I)
        if not tm:
            continue
        kind = tm.group(1).lower()
        if kind in _CAST_UNSUPPORTED:
            raise UnsupportedSQL(f"CAST AS {tm.group(1)} has no faithful MySQL target: {text.strip()[:120]}")
        if kind not in _CAST_TYPES:
            continue                                        # DATE, CHAR, SIGNED, DECIMAL(p,s): MySQL's own
        expr = _cast(inner[:tm.start()].strip())
        if kind in _CAST_TRUNCATES:
            expr = f"TRUNCATE({expr}, 0)"
        out.append(text[i:m.start()])
        out.append(f"CAST({expr} AS {_CAST_TYPES[kind]})")
        i = j
    out.append(text[i:])
    return "".join(out)


def translate(sql: str, pk_of=None) -> str:
    """SQLite SQL -> MySQL.

    pk_of is accepted for signature parity with db.postgres.translate and is not
    needed: MySQL's ON DUPLICATE KEY UPDATE fires on any unique key, which is the
    same rule SQLite's INSERT OR REPLACE follows, so no key has to be chosen.
    """
    cat = _catalog(sql)
    if cat is not None:
        return cat
    if re.search(r"\bsqlite_master\b", sql, re.I):            # code parts only, never inside a literal
        # The caller's references to the `sql` column are quoted FIRST -- the
        # subquery substituted in below already carries its own backticks, and
        # quoting afterwards would mangle them.
        sql = "".join(p if s else re.sub(r"(?<![\w`.])sql(?![\w`])", "`sql`", p, flags=re.I)
                      for s, p in _split_strings(sql))
        sql = "".join(p if s else re.sub(r"\bsqlite_master\b", _SQLITE_MASTER, p, flags=re.I)
                      for s, p in _split_strings(sql))
    code_only = "".join(p for s, p in _split_strings(sql) if not s)
    for rx, name in _UNSUPPORTED:
        if name == "sqlite_master":
            continue                                           # already substituted above
        if rx.search(code_only):
            raise UnsupportedSQL(f"{name} has no automatic MySQL translation: {sql.strip()[:120]}")

    parts = _split_strings(sql)
    text = "\x00".join(p for s, p in parts if not s)           # code only, joined by markers
    strings = [p for s, p in parts if s]

    # INSERT OR REPLACE / OR IGNORE -> MySQL's own forms.
    replace_target = None
    m = re.search(r"\bINSERT\s+OR\s+REPLACE\s+INTO\s+([A-Za-z_]\w*)\s*\(([^)]*)\)", text, re.I)
    if m:
        replace_target = (m.group(1), [c.strip() for c in m.group(2).split(",")])
        text = re.sub(r"\bINSERT\s+OR\s+REPLACE\s+INTO\b", "INSERT INTO", text, flags=re.I)
    if re.search(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", text, re.I):
        text = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT IGNORE INTO", text, flags=re.I)

    # ON CONFLICT -> ON DUPLICATE KEY UPDATE. Three shapes appear in ATIP:
    #   ON CONFLICT DO NOTHING
    #   ON CONFLICT (cols) DO NOTHING
    #   ON CONFLICT (cols) DO UPDATE SET a=excluded.a, b=b+excluded.b
    text = re.sub(r"\bON\s+CONFLICT\s*(\([^)]*\))?\s*DO\s+NOTHING\b", "\x01DUPNOTHING\x01", text, flags=re.I)
    mcon = re.search(r"\bON\s+CONFLICT\s*(\([^)]*\))?\s*DO\s+UPDATE\s+SET\b", text, re.I)
    if mcon:
        text = text[:mcon.start()] + " ON DUPLICATE KEY UPDATE " + text[mcon.end():]
    # The column may be backticked -- ATIP quotes the names MySQL 8 reserves, and
    # `signal`=excluded.`signal` is a real upsert in scores/engine.py. A backtick is
    # not a word character, so \w+ alone left `excluded.` in place and MySQL then
    # failed on a table it has no name for.
    text = re.sub(r"\bexcluded\.(`[^`]+`|\w+)", lambda g: f"VALUES({g.group(1)})", text, flags=re.I)

    text = _greatest(text)
    text = _cast(text)
    text = _alias_derived(text)

    # rejoin with the string literals back in place, then the date() modifiers (they contain literals)
    chunks = text.split("\x00")
    out = []
    for i, ch in enumerate(chunks):
        out.append(ch)
        if i < len(strings):
            out.append(strings[i])
    sql2 = "".join(out)
    sql2 = re.sub(r"\bdate\s*\(\s*([^,()]+?)\s*,\s*'([-+])\s*(\d+)\s+([a-z]+)'\s*\)", _date_mod, sql2, flags=re.I)
    sql2 = re.sub(r"\bdate\s*\(\s*'now'\s*\)", "CURRENT_DATE", sql2, flags=re.I)
    sql2 = re.sub(r"\bdatetime\s*\(\s*'now'\s*\)", "CURRENT_TIMESTAMP", sql2, flags=re.I)

    # ? -> %s outside literals; every % is doubled, INSIDE literals too (LIKE 'a%'):
    # PyMySQL %-formats the whole statement when parameters are passed, and
    # MySQLConnection always passes them.
    sql2 = "".join(p.replace("%", "%%") if s else p.replace("%", "%%").replace("?", "%s")
                   for s, p in _split_strings(sql2))

    # An INSERT OR REPLACE with no explicit conflict clause becomes a full upsert of
    # every inserted column. MySQL keys it on whichever unique key the row collides
    # with, which is SQLite's rule too.
    if replace_target and "\x01DUPNOTHING\x01" not in sql2 and "ON DUPLICATE KEY UPDATE" not in sql2.upper():
        _, cols = replace_target
        sets = ", ".join(f"{quote(c)}=VALUES({quote(c)})" for c in cols)
        sql2 = sql2.rstrip().rstrip(";") + f" ON DUPLICATE KEY UPDATE {sets}"

    # ON CONFLICT DO NOTHING -> INSERT IGNORE (set on the verb, not the tail).
    if "\x01DUPNOTHING\x01" in sql2:
        sql2 = sql2.replace("\x01DUPNOTHING\x01", "").rstrip().rstrip(";")
        if not re.search(r"\bINSERT\s+IGNORE\b", sql2, re.I):
            sql2 = re.sub(r"\bINSERT\s+INTO\b", "INSERT IGNORE INTO", sql2, count=1, flags=re.I)
    return sql2.strip()


# ── DDL translation ───────────────────────────────────────────────────────

def keyed_columns(stmts) -> dict:
    """{table: {columns taking part in any PRIMARY KEY / UNIQUE / INDEX}}.

    Gathered across every statement before any of them is translated, because a
    CREATE INDEX can key a column whose CREATE TABLE says nothing about it -- and
    MySQL needs that column to be VARCHAR rather than TEXT.
    """
    keyed: dict = {}
    for s in stmts:
        m = re.search(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_]\w*)\s*\((.*)\)\s*;?\s*$",
                      s, re.I | re.S)
        if m:
            t, body = m.group(1), m.group(2)
            cols = keyed.setdefault(t, set())
            for c in re.finditer(r"\b(?:PRIMARY\s+KEY|UNIQUE)\s*\(([^)]*)\)", body, re.I):
                cols.update(x.strip().strip("`\"").split()[0] for x in c.group(1).split(",") if x.strip())
            # inline `col TYPE ... PRIMARY KEY` / `... UNIQUE`
            for part in _split_top_level(body):
                p = part.strip()
                mm = re.match(r"([A-Za-z_]\w*)\s+\w+", p)
                if mm and re.search(r"\b(PRIMARY\s+KEY|UNIQUE)\b", p, re.I) \
                        and not re.match(r"\s*(PRIMARY|UNIQUE|CONSTRAINT|FOREIGN|CHECK)\b", p, re.I):
                    cols.add(mm.group(1))
            continue
        m = re.search(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"]?[\w{}.]+[`\"]?\s+ON\s+"
                      r"([A-Za-z_]\w*)\s*\(([^)]*)\)", s, re.I)
        if m:
            keyed.setdefault(m.group(1), set()).update(
                x.strip().strip("`\"").split()[0] for x in m.group(2).split(",") if x.strip())
    return keyed


def index_target(stmt: str):
    """(table, [columns]) for a CREATE INDEX statement, else None.

    The columns come back as bare names: a prefix length or a DESC/ASC qualifier
    is dropped, because callers want to know WHICH columns the index keys.
    """
    m = re.search(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"]?[\w{}.]+[`\"]?\s+ON\s+"
                  r"([A-Za-z_]\w*)\s*\((.*)\)\s*;?\s*$", stmt, re.I | re.S)
    if not m:
        return None
    cols = []
    for part in m.group(2).split(","):
        mm = re.match(r"\s*[`\"]?([A-Za-z_]\w*)", part)
        if mm:
            cols.append(mm.group(1))
    return m.group(1), cols


_TYPE_RULES = [
    (r"\bREAL\b", "DOUBLE"),
    (r"\bDOUBLE\s+PRECISION\b", "DOUBLE"),
    (r"\bBOOL(EAN)?\b", "TINYINT(1)"),       # SQLite stores 0/1 and the code compares with 1
    (r"\bNUMERIC\b", "DECIMAL(20,6)"),
]


def _checked_columns(body: str) -> set:
    """Columns any CHECK constraint in this CREATE TABLE refers to.

    MySQL refuses a CHECK that refers to an AUTO_INCREMENT column (errno 3818,
    "cannot refer to an auto-increment column"), and SQLite's
    `id INTEGER PRIMARY KEY CHECK (id = 1)` -- its idiom for a single-row table --
    is exactly that combination once INTEGER PRIMARY KEY becomes AUTO_INCREMENT.
    A constrained key is not a sequence, so _column_def() leaves AUTO_INCREMENT off
    for these and the CHECK does the work it was written to do.
    """
    cols = set()
    for m in re.finditer(r"\bCHECK\s*\(", body, re.I):
        depth, i = 0, m.end() - 1
        while i < len(body):                       # the matching close paren
            if body[i] == "(":
                depth += 1
            elif body[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        cols.update(re.findall(r"[A-Za-z_]\w*", body[m.end():i]))
    return cols


def _column_def(part: str, keyed: set, checked: set = frozenset()) -> str:
    """One column definition -> MySQL. Handles the TEXT restrictions and quoting."""
    p = part.strip()
    m = re.match(r"([A-Za-z_]\w*)\s+(.*)$", p, re.S)
    if not m:
        return p
    name, rest = m.group(1), m.group(2)

    # INTEGER PRIMARY KEY is the rowid alias in SQLite: auto-numbered either way --
    # unless a CHECK pins it, which SQLite uses to mean "one row only" and MySQL
    # will not allow over an AUTO_INCREMENT column. See _checked_columns().
    if re.match(r"INTEGER\s+PRIMARY\s+KEY(\s+AUTOINCREMENT)?\b", rest, re.I):
        tail = re.sub(r"^INTEGER\s+PRIMARY\s+KEY(\s+AUTOINCREMENT)?\b", "", rest, flags=re.I)
        auto = "" if name in checked else " AUTO_INCREMENT"
        return f"{quote(name)} BIGINT NOT NULL{auto} PRIMARY KEY{tail}"

    rest = re.sub(r"\bAUTOINCREMENT\b", "", rest, flags=re.I)
    rest = re.sub(r"\s+COLLATE\s+NOCASE\b", "", rest, flags=re.I)
    # Expression defaults: MySQL wants CURRENT_TIMESTAMP bare, CURRENT_DATE parenthesised.
    rest = re.sub(r"DEFAULT\s*\(?\s*datetime\s*\(\s*'now'\s*\)\s*\)?", "DEFAULT CURRENT_TIMESTAMP", rest, flags=re.I)
    rest = re.sub(r"DEFAULT\s*\(?\s*date\s*\(\s*'now'\s*\)\s*\)?", "DEFAULT (CURRENT_DATE)", rest, flags=re.I)

    # TEXT -> VARCHAR where MySQL will not take TEXT: in a key, or with a DEFAULT.
    #
    # A column that is BOTH takes the LARGER length, which is what keeps the two
    # paths into this module agreeing. research/tech_signals.py's
    # technical_signal.status is the first such column: TEXT NOT NULL DEFAULT
    # 'OPEN' with its own CREATE INDEX. Given the whole statement list up front
    # (the migration tool) the key is known and the keyed length applied; reached
    # one statement at a time (the runtime) only the DEFAULT is visible when the
    # CREATE TABLE runs, and the later CREATE INDEX does not narrow a column it
    # can already index -- so the same table came out VARCHAR(191) one way and
    # VARCHAR(255) the other. Taking the max satisfies both rules: 255 utf8mb4
    # characters is 1020 bytes, well inside InnoDB's 3072-byte index key limit.
    if re.search(r"\bTEXT\b", rest, re.I):
        has_default = bool(re.search(r"\bDEFAULT\b", rest, re.I))
        width = None
        if name in keyed:
            width = max(KEYED_TEXT_LEN, DEFAULTED_TEXT_LEN) if has_default else KEYED_TEXT_LEN
        elif has_default:
            width = DEFAULTED_TEXT_LEN
        if width is not None:
            rest = re.sub(r"\bTEXT\b", f"VARCHAR({width})", rest, count=1, flags=re.I)

    for rx, repl in _TYPE_RULES:
        rest = re.sub(rx, repl, rest, flags=re.I)
    rest = re.sub(r"\bINTEGER\b", "BIGINT", rest, flags=re.I)
    return f"{quote(name)} {rest.strip()}"


def ddl(stmt: str, keyed=None) -> str:
    """A CREATE TABLE / CREATE INDEX / ALTER TABLE statement in MySQL's dialect.

    `keyed` is the set of this table's columns that take part in any key or index
    (keyed_columns() over the whole statement list). Without it, a TEXT primary key
    is emitted as TEXT and MySQL rejects the statement -- so callers that create
    tables should always pass it.
    """
    keyed = set(keyed or ())
    s = stmt.strip().rstrip(";")
    # WITHOUT ROWID is a table option AFTER the closing paren, so it has to go before
    # the CREATE TABLE body is matched -- otherwise the statement fails to match at
    # all and passes through as untranslated SQLite.
    s = re.sub(r"\s*WITHOUT\s+ROWID\s*$", "", s, flags=re.I)

    m = re.match(r"(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)([A-Za-z_]\w*)\s*\((.*)\)\s*$", s, re.I | re.S)
    if m:
        head, table, body = m.group(1), m.group(2), m.group(3)
        checked = _checked_columns(body)
        out = []
        for part in _split_top_level(body):
            p = part.strip()
            if not p:
                continue
            if re.match(r"(PRIMARY\s+KEY|UNIQUE|CONSTRAINT|FOREIGN\s+KEY|CHECK)\b", p, re.I):
                # table-level constraint: quote the column list, leave the rest alone
                def _q(mm):
                    cols = ", ".join(quote(c) for c in mm.group(2).split(",") if c.strip())
                    return f"{mm.group(1)}({cols})"
                p = re.sub(r"\b(PRIMARY\s+KEY\s*|UNIQUE\s*)\(([^)]*)\)", _q, p, flags=re.I)
                out.append(p)
            else:
                out.append(_column_def(p, keyed, checked))
        # Only when the table has no auto column of its own: MySQL allows exactly one
        # (errno 1075), and a table that already has one already has its entry order
        # from it, so a second would be both illegal and redundant.
        if table in ENTRY_ORDER_TABLES and not any(
                re.match(rf"\s*`?{ENTRY_ORDER_COLUMN}`?\s", c) or "AUTO_INCREMENT" in c.upper()
                for c in out):
            # AUTO_INCREMENT needs a key of its own; UNIQUE is the cheapest that
            # satisfies MySQL without claiming to be the table's identity, which
            # belongs to the TEXT primary key these tables already have.
            out.append(f"{quote(ENTRY_ORDER_COLUMN)} BIGINT NOT NULL AUTO_INCREMENT UNIQUE")
        return f"{head}{quote(table)} (\n  " + ",\n  ".join(out) + "\n)" + TABLE_SUFFIX

    m = re.match(r"(CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?)([`\"]?[\w{}.]+[`\"]?)\s+ON\s+"
                 r"([A-Za-z_]\w*)\s*\(([^)]*)\)\s*$", s, re.I | re.S)
    if m:
        head, name, table, cols = m.group(1), m.group(2).strip("`\""), m.group(3), m.group(4)
        # MySQL has no "IF NOT EXISTS" for CREATE INDEX before 8.0.29 / MariaDB 10.5;
        # the caller swallows a duplicate-key error instead.
        head = re.sub(r"\s*IF\s+NOT\s+EXISTS\s*", " ", head, flags=re.I)
        cl = ", ".join(quote(c.strip().split()[0]) + (" DESC" if re.search(r"\bDESC\b", c, re.I) else "")
                       for c in cols.split(",") if c.strip())
        return f"{head}{quote(name)} ON {quote(table)} ({cl})"

    m = re.match(r"(ALTER\s+TABLE\s+)([A-Za-z_]\w*)(\s+ADD\s+(?:COLUMN\s+)?)(.*)$", s, re.I | re.S)
    if m:
        return f"{m.group(1)}{quote(m.group(2))}{m.group(3)}{_column_def(m.group(4), keyed)}"

    return s


# ── triggers ──────────────────────────────────────────────────────────────

def trigger_ddl(sql: str) -> str:
    """ATIP's SQLite triggers are all append-only guards:
         BEFORE UPDATE|DELETE ON t [WHEN cond] BEGIN SELECT RAISE(ABORT, 'msg'); END
    -> a MySQL row trigger raising SQLSTATE 45000 (the conventional user-defined error).
    MySQL has no WHEN clause on triggers, so a conditional guard becomes an IF inside
    the body. Anything else raises UnsupportedSQL rather than being dropped silently.
    """
    m = re.match(r"\s*CREATE\s+TRIGGER\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s+(BEFORE|AFTER)\s+(UPDATE|DELETE|INSERT)\s+"
                 r"ON\s+(\w+)\s+(?:FOR\s+EACH\s+ROW\s+)?(?:WHEN\s+(.+?)\s+)?BEGIN\s+SELECT\s+RAISE\s*\(\s*ABORT\s*,\s*"
                 r"'((?:[^']|'')*)'\s*\)\s*;\s*END\s*;?\s*$", sql, re.I | re.S)
    if not m:
        raise UnsupportedSQL(f"trigger is not an append-only guard: {' '.join(sql.split())[:120]}")
    name, when, event, table, cond, msg = m.groups()
    raise_stmt = f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{msg}';"
    body = f"IF {cond} THEN {raise_stmt} END IF;" if cond else raise_stmt
    return (f"CREATE TRIGGER {quote(name)} {when.upper()} {event.upper()} ON {quote(table)} "
            f"FOR EACH ROW BEGIN {body} END")


def keys_from_ddl(stmts) -> dict:
    """{table: [[key columns], ...]} -- same shape as db.postgres.keys_from_ddl, so the
    two backends can be used interchangeably by callers that need key information."""
    from db.postgres import keys_from_ddl as _k
    return _k(stmts)


# ── a sqlite3-shaped connection over PyMySQL ──────────────────────────────

class Row(tuple):
    """Index and name access, like sqlite3.Row."""
    def __new__(cls, values, names):
        r = super().__new__(cls, values)
        r._names = names
        return r

    def __getitem__(self, k):
        if isinstance(k, str):
            return super().__getitem__(self._names.index(k))
        return super().__getitem__(k)

    def keys(self):
        return list(self._names)


# ── errors: PyMySQL's exceptions as the sqlite3 ones callers catch ─────────
#
# The two libraries both implement PEP 249, but their class hierarchies are
# unrelated -- `except sqlite3.OperationalError` catches nothing a PyMySQL cursor
# raises. ATIP's migration helpers are built on that clause (db/schema.py swallows
# "duplicate column name" to make ALTER TABLE ADD COLUMN idempotent), so the
# connection has to raise what they catch, exactly as it already answers to `?`
# placeholders and sqlite3.Row.
#
# The mapping is by CONDITION, not by class name, because the two libraries
# categorise the same conditions differently. Mapping name-to-name would be wrong
# in both directions:
#
#   missing table   SQLite: OperationalError   PyMySQL: ProgrammingError (1146)
#   syntax error    SQLite: OperationalError   PyMySQL: ProgrammingError (1064)
#   CHECK violated  SQLite: IntegrityError     PyMySQL: OperationalError (3819)
#   over-long value SQLite: (truncates)        PyMySQL: DataError        (1406)
#
# So SQLite's own rule is applied instead: IntegrityError when a constraint was
# violated, OperationalError for everything else a statement can get wrong -- a
# missing table, an unknown column, bad syntax, a locked or unreachable database.
# All measured against MySQL 8.0.46; see tests/test_mysql_backend.py.

_INTEGRITY_ERRNOS = frozenset((
    1048,                     # ER_BAD_NULL_ERROR            NOT NULL violated
    1062,                     # ER_DUP_ENTRY                 duplicate unique key
    1169,                     # ER_DUP_UNIQUE                duplicate on a unique index
    1216, 1217, 1451, 1452,   # foreign key, child and parent side
    3819,                     # ER_CHECK_CONSTRAINT_VIOLATED
))


def as_sqlite_error(exc: Exception) -> Exception:
    """`exc` as the sqlite3 class SQLite would raise for the same condition.

    `args` is carried over unchanged, so args[0] is still the MySQL errno and the
    message still reads the way PyMySQL wrote it -- callers that switch on the
    errno (tools/sqlite_to_mysql.py) keep working. Anything that is not a PyMySQL
    database error is returned untouched, which is what keeps UnsupportedSQL and
    ordinary programming mistakes from being disguised as database trouble.
    """
    try:
        import pymysql
    except ImportError:                       # pragma: no cover - MySQLConnection cannot exist
        return exc
    if not isinstance(exc, pymysql.err.Error):
        return exc
    errno = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    cls = sqlite3.IntegrityError if errno in _INTEGRITY_ERRNOS else sqlite3.OperationalError
    return cls(*exc.args).with_traceback(exc.__traceback__)


@contextlib.contextmanager
def sqlite_errors():
    """Re-raise whatever PyMySQL raises inside as the sqlite3 class SQLite would."""
    try:
        yield
    except Exception as e:
        translated = as_sqlite_error(e)
        if translated is e:
            raise
        raise translated from e


class _Cursor:
    def __init__(self, cur):
        self._cur = cur
        self._names = [d[0] for d in cur.description] if cur.description else []

    def _wrap(self, r):
        return None if r is None else Row(r, self._names)

    def fetchone(self):
        with sqlite_errors():
            return self._wrap(self._cur.fetchone())

    def fetchall(self):
        with sqlite_errors():
            return [self._wrap(r) for r in self._cur.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        return self._cur.lastrowid

    @property
    def description(self):
        return self._cur.description


class _NoRows:
    """What sqlite3 hands back for a DDL statement that did nothing.

    Measured against sqlite3 for a no-op `CREATE INDEX IF NOT EXISTS`, so a caller
    cannot tell the two backends apart: no description, rowcount -1, lastrowid 0,
    and fetching gives nothing rather than raising. PyMySQL's own cursor raises
    "execute() first" after a statement that failed, which is why this exists.
    """

    description = None
    rowcount = -1
    lastrowid = 0

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __iter__(self):
        return iter(())


class MySQLConnection:
    """sqlite3-shaped connection over PyMySQL, translating every statement."""

    def __init__(self, dsn: str):
        try:
            import pymysql
        except ImportError as e:
            raise RuntimeError('MySQL backend needs: pip install PyMySQL') from e
        from urllib.parse import urlparse, unquote
        u = urlparse(dsn.replace("mysql+pymysql://", "mysql://"))
        self._conn = pymysql.connect(
            host=u.hostname or "localhost",
            port=u.port or 3306,
            user=unquote(u.username or ""),
            password=unquote(u.password or ""),
            database=(u.path or "").lstrip("/"),
            charset="utf8mb4",
            autocommit=False,
            # keep DATE/DATETIME as returned types; ATIP's SQLite path uses
            # PARSE_DECLTYPES and compares against date/datetime objects.
            #
            # Pin the session time zone. MySQL converts a TIMESTAMP to UTC on the
            # way in and back on the way out using THIS setting, and the schema has
            # 306 TIMESTAMP columns against 1 DATETIME -- so left at the default
            # (SYSTEM) every stored time would mean whatever the host's OS time zone
            # happened to be, and a value written on one host would read back
            # shifted on another. Measured on 8.0.46: 09:30 written at +05:30 reads
            # back as 04:00 at +00:00, while the lone DATETIME is untouched.
            #
            # UTC is the value that matches SQLite rather than merely being stable:
            # ATIP's DEFAULT CURRENT_TIMESTAMP columns are SQLite's
            # CURRENT_TIMESTAMP, which is UTC (see tools/repair_news_timezone.py,
            # written after the last time these two disagreed). Explicitly written
            # IST values round-trip unchanged either way, since the same offset
            # applies in both directions.
            init_command="SET time_zone = '+00:00'",
        )
        self.row_factory = None                       # accepted for sqlite3 compatibility
        self._keys: dict = {}

    def pk_of(self, table):
        """[[key columns], ...] for `table`, primary key first -- read from
        information_schema and cached. Kept for parity with PgConnection; MySQL's
        upserts do not need it, but callers may."""
        if table in self._keys:
            return self._keys[table]
        cur = self._conn.cursor()
        with sqlite_errors():
            cur.execute(
                "SELECT INDEX_NAME, COLUMN_NAME FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND NON_UNIQUE=0 "
            "ORDER BY INDEX_NAME='PRIMARY' DESC, INDEX_NAME, SEQ_IN_INDEX", (table,))
        groups: dict = {}
        for idx, col in cur.fetchall():
            groups.setdefault(idx, []).append(col)
        keys = ([groups.pop("PRIMARY")] if "PRIMARY" in groups else []) + list(groups.values())
        self._keys[table] = keys
        return keys

    @staticmethod
    def _is_ddl(sql):
        return sql.lstrip().upper().startswith(("CREATE ", "ALTER ", "DROP "))

    def _prepare(self, sql):
        """One statement in MySQL's dialect. Pure: it touches no server.

        The keyed_columns() call sees only this statement, so it widens a TEXT
        column to VARCHAR only where the key is declared in the CREATE TABLE
        itself. A key declared by a SEPARATE CREATE INDEX cannot be known here at
        all; _widen_keyed_text() handles that case when the index arrives.
        """
        if self._is_ddl(sql):
            if re.match(r"\s*CREATE\s+TRIGGER\b", sql, re.I):
                return trigger_ddl(sql)
            return ddl(sql, keyed_columns([sql]).get(_table_of(sql), set()))
        return translate(sql, self.pk_of)

    # The column types MySQL will not index without a prefix length.
    _TEXT_TYPES = frozenset(("tinytext", "text", "mediumtext", "longtext"))

    def _widen_keyed_text(self, sql):
        """Before a CREATE INDEX runs: widen any TEXT column it keys to VARCHAR.

        ddl() widens the columns it is TOLD are keyed, but a connection is handed
        one statement at a time, and a CREATE TABLE cannot know that a CREATE
        INDEX later on will key one of its TEXT columns. 68 columns in this schema
        are keyed that way, and every one of their indexes failed with errno 1170,
        "BLOB/TEXT column used in key specification without a key length" -- so
        init_db() could not create the schema on MySQL at all.

        Handing keyed_columns() the whole schema up front is the obvious fix, and
        is what tools/sqlite_to_mysql.py does, but it is not available here:
        init_db() is hundreds of separate execute() calls with no list to hand
        over. So the missing fact is read off the server instead, at the one
        moment it is known for certain -- when the index arrives.

        The COLUMN is widened rather than the INDEX given a prefix length
        (`col(191)`, which MySQL would also accept) for two reasons: a database
        built by init_db() then matches one built by tools/sqlite_to_mysql.py, and
        a prefix on a UNIQUE index would quietly weaken the constraint to
        uniqueness over the first 191 characters instead of the whole value.
        """
        target = index_target(sql)
        if target is None:
            return
        table, cols = target
        for col, definition in self._text_columns(table, cols):
            self._assert_fits(table, col)
            # The definition comes from SHOW CREATE TABLE rather than being
            # reassembled from information_schema, because MODIFY COLUMN replaces
            # the whole definition: anything not repeated is silently dropped.
            # SHOW CREATE TABLE gives it as re-executable SQL, so swapping just
            # the type keeps the collation, the NOT NULL, the default and the
            # comment exactly as MySQL spells them. (information_schema reports an
            # expression default in an escaped form that is NOT valid SQL --
            # _latin1\'x\' for DEFAULT ('x') -- so reassembling would corrupt it.)
            widened = re.sub(r"^\s*`[^`]+`\s+\w+(?:\([^)]*\))?",
                             f"{quote(col)} VARCHAR({KEYED_TEXT_LEN})", definition, count=1)
            self._conn.cursor().execute(f"ALTER TABLE {quote(table)} MODIFY COLUMN {widened}")

    def _text_columns(self, table, cols):
        """[(column, its SHOW CREATE TABLE definition)] for those of `cols` that
        are a TEXT type on the server.

        Empty when the table does not exist -- information_schema simply returns
        no rows, and the CREATE INDEX that follows raises MySQL's own error for a
        missing table rather than one invented here.
        """
        cur = self._conn.cursor()
        cur.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND DATA_TYPE IN %s",
            (table, tuple(sorted(self._TEXT_TYPES))))
        wanted = {r[0] for r in cur.fetchall()} & set(cols)
        if not wanted:
            return []
        cur.execute(f"SHOW CREATE TABLE {quote(table)}")
        found = []
        for line in cur.fetchone()[1].splitlines():
            m = re.match(r"\s*`([^`]+)`\s+(\w+)", line)
            if m and m.group(1) in wanted and m.group(2).lower() in self._TEXT_TYPES:
                found.append((m.group(1), line.strip().rstrip(",")))
        return found

    def _assert_fits(self, table, column):
        """Refuse to widen a column that already holds a longer value.

        Narrowing to VARCHAR(191) raises error 1406 under a strict sql_mode and
        TRUNCATES under a permissive one, and which of those a managed server
        gives is not ours to choose -- so the check is made here, where it can
        name the table, the column and what it found.
        """
        cur = self._conn.cursor()
        cur.execute(f"SELECT MAX(CHAR_LENGTH({quote(column)})) FROM {quote(table)}")
        longest = cur.fetchone()[0] or 0
        if longest > KEYED_TEXT_LEN:
            raise UnsupportedSQL(
                f"{table}.{column} is indexed, so MySQL needs it as "
                f"VARCHAR({KEYED_TEXT_LEN}), but it already holds a value of "
                f"{longest} characters. Shorten the data, or drop the index.")

    # CREATE INDEX IF NOT EXISTS, which MySQL has no syntax for: ddl() strips the
    # clause, so honouring it falls to the connection.
    _INDEX_IF_NOT_EXISTS = re.compile(r"\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\b", re.I)
    ER_DUP_KEYNAME = 1061

    def execute(self, sql, params=()):
        # _prepare first: it is pure, so a statement this backend cannot express
        # raises UnsupportedSQL before anything is sent or altered.
        stmt = self._prepare(sql)
        try:
            with sqlite_errors():
                if self._is_ddl(sql):
                    self._widen_keyed_text(sql)
                cur = self._conn.cursor()
                cur.execute(stmt, tuple(params) if params else None)
        except sqlite3.OperationalError as e:
            # SQLite makes `CREATE INDEX IF NOT EXISTS` a no-op when the index is
            # already there, and init_db() relies on that: it runs the whole schema
            # on every start, with no try/except around those statements. MySQL has
            # no such clause, so ddl() removes it and the second run died on errno
            # 1061. The clause is honoured here instead -- only when the caller
            # actually wrote it, so a duplicate index name is still an error for
            # anyone who did not ask for it to be ignored.
            if e.args and e.args[0] == self.ER_DUP_KEYNAME and self._INDEX_IF_NOT_EXISTS.match(sql):
                return _NoRows()
            raise
        return _Cursor(cur)

    def executemany(self, sql, seq):
        stmt = self._prepare(sql)
        with sqlite_errors():
            cur = self._conn.cursor()
            cur.executemany(stmt, [tuple(p) for p in seq])
        return _Cursor(cur)

    def reset_identity(self, table, column="id"):
        with sqlite_errors():
            cur = self._conn.cursor()
            cur.execute(f"SELECT COALESCE(MAX({quote(column)}), 0) + 1 FROM {quote(table)}")
            nxt = cur.fetchone()[0]
            cur.execute(f"ALTER TABLE {quote(table)} AUTO_INCREMENT = {int(nxt)}")

    def commit(self):
        with sqlite_errors():
            self._conn.commit()

    def rollback(self):
        with sqlite_errors():
            self._conn.rollback()

    def close(self):
        self._conn.close()

    def cursor(self):
        """sqlite3 compatibility: a connection-level cursor that behaves like ours."""
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if exc and exc[0]:
            self.rollback()
        else:
            self.commit()
        return False


def _table_of(sql):
    m = re.search(r"\b(?:TABLE|INDEX\s+\S+\s+ON)\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"]?([A-Za-z_]\w*)", sql, re.I)
    return m.group(1) if m else ""
