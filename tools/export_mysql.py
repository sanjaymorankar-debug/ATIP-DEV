"""
Export ATIP's SQLite database as MySQL / MariaDB .sql files for phpMyAdmin (Hostinger).

    python tools/export_mysql.py                         # atip_data/atip.db -> atip_data/mysql_export/<ts>/
    python tools/export_mysql.py --prefix atip_ --part-mb 40
    python tools/export_mysql.py --source D:/path/atip.db.bak --out D:/exports --no-gzip
    python tools/export_mysql.py --tables prices_daily ai_scores       # only these
    python tools/export_mysql.py --exclude alert_log                     # all but these

OUTPUT  (import in this order in phpMyAdmin -> your database -> Import)
    00_schema.sql[.gz]     DROP TABLE IF EXISTS + CREATE TABLE for every exported table
    01_data_part001.sql.gz ... data, split so each file stays under --part-mb (uncompressed)
    99_verify.sql          one query: expected vs actual row count per table (run it last)
    README.txt             the steps, what was redacted / skipped, type decisions
    manifest.json          the same, machine-readable

WHAT IT DOES
  * Reads a point-in-time snapshot (SQLite backup API into a temp copy), so ATIP can keep
    running and the export is consistent across tables.
  * Types come from the declared SQLite type AND the data actually stored (SQLite lets a
    column hold anything): INTEGER -> BIGINT (DOUBLE if a float is stored, text type if text
    is), REAL -> DOUBLE, DATE / TIMESTAMP -> DATE / DATETIME(6) when every stored value
    parses (else kept as text, reported), TEXT -> VARCHAR(n) when indexed, else TEXT /
    MEDIUMTEXT / LONGTEXT by the longest value, BLOB -> LONGBLOB. utf8mb4, InnoDB.
  * INTEGER PRIMARY KEY -> BIGINT AUTO_INCREMENT PRIMARY KEY; composite keys, UNIQUE and
    plain indexes are kept. Not exported (listed in README): foreign keys, partial /
    expression indexes, CHECK constraints, triggers, views, column defaults.
  * Every table name gets --prefix (default "atip_") so ATIP cannot collide with the
    website's own tables. Re-importing replaces ONLY those prefixed tables.

SENSITIVE DATA (a full copy, minus secrets)
  * no rows exported:   enterprise_session, enterprise_password_reset,
                        enterprise_refresh_token, ops_idempotency (cached API responses)
  * blanked columns:    enterprise_user.password_hash / mfa_secret_enc / mfa_pending_enc,
                        enterprise_api_key.key_hash
  * JSON values:        any key that looks like a credential (token, secret, password,
                        api_key, client_secret, pin, totp, ...) -> "[REDACTED]"
                        (ops_config_version keeps copies of config.json, which holds keys)
  * every text value:   JWTs, Telegram bot tokens, sk-/sk-ant- keys, AWS keys and Bearer
                        tokens -> [REDACTED]
  Counts per table are in the README. Nothing is ever read from .env / config.json.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import re
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_DATA = {"enterprise_session", "enterprise_password_reset", "enterprise_refresh_token", "ops_idempotency"}
COLUMN_RULES = {  # (table, column) -> replacement(row_dict) ; None keeps NULL
    ("enterprise_user", "password_hash"): lambda r: "!redacted-login-disabled",
    ("enterprise_user", "mfa_secret_enc"): lambda r: None,
    ("enterprise_user", "mfa_pending_enc"): lambda r: None,
    ("enterprise_api_key", "key_hash"): lambda r: f"redacted:{r.get('key_id')}",
}
SECRET_KEY_RE = re.compile(r"(?i)(pass(word|wd)?|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|"
                           r"client[_-]?secret|client[_-]?id|\bpin\b|totp|otp_|mfa|bot_?token|chat_?id|auth(orization)?$)")
# (pattern, needs_entropy). Key-shaped patterns also match ordinary slugs -- a news URL
# ".../samsung-sk-hynix-plunge-up-to-11-as-investors..." looks like "sk-..." -- so those
# only count when the match looks random: upper AND lower case AND digits.
SECRET_VALUE_RES = [
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), False),   # JWT (Dhan access tokens)
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"), True),                               # Telegram bot token
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), True),                                  # Anthropic
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}"), True),                            # OpenAI-style
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), False),                                        # AWS access key
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"), True),                       # Bearer tokens
]


def _random_looking(s: str) -> bool:
    return any(c.isupper() for c in s) and any(c.islower() for c in s) and sum(c.isdigit() for c in s) >= 3
REDACTED = "[REDACTED]"

DT_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?)?$")
TZ_RE = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")


# -- helpers -------------------------------------------------------------------------------

def q(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def mysql_str(s: str) -> str:
    out = s.replace("\\", "\\\\").replace("'", "\\'").replace("\0", "\\0").replace("\n", "\\n") \
        .replace("\r", "\\r").replace("\x1a", "\\Z")
    return "'" + out + "'"


def affinity(decl: str) -> str:
    d = (decl or "").upper()
    if "INT" in d:
        return "INTEGER"
    if any(k in d for k in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if "BLOB" in d or not d:
        return "BLOB" if "BLOB" in d else "NONE"
    if any(k in d for k in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def is_temporal(decl: str) -> str | None:
    d = (decl or "").upper()
    if "TIMESTAMP" in d or "DATETIME" in d:
        return "DATETIME"
    if d.startswith("DATE") or d == "DATE":
        return "DATE"
    if d == "TIME":
        return None
    return None


def norm_dt(v: str):
    """(kind, normalised) for a date / datetime string, or None when it does not parse
    (or carries a timezone, which DATETIME would silently drop)."""
    s = v.strip()
    if TZ_RE.search(s) and len(s) > 10:
        return None
    m = DT_RE.match(s)
    if not m:
        return None
    y, mo, d, hh, mi, ss, frac = m.groups()
    try:
        datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0))
    except ValueError:
        return None
    if hh is None:
        return "DATE", f"{y}-{mo}-{d}"
    return "DATETIME", f"{y}-{mo}-{d} {hh}:{mi}:{ss or '00'}" + (f".{frac}" if frac else "")


class Redactor:
    def __init__(self):
        self.counts = {}

    def _hit(self, table, what):
        self.counts.setdefault(table, {}).setdefault(what, 0)
        self.counts[table][what] += 1

    def _json_walk(self, o, table):
        if isinstance(o, dict):
            out = {}
            for k, v in o.items():
                if isinstance(k, str) and SECRET_KEY_RE.search(k) and v not in (None, "", [], {}) \
                        and not isinstance(v, (bool, dict, list)):
                    out[k] = REDACTED
                    self._hit(table, "json_key")
                else:
                    out[k] = self._json_walk(v, table)
            return out
        if isinstance(o, list):
            return [self._json_walk(x, table) for x in o]
        if isinstance(o, str):
            return self.text(o, table, json_inner=True)
        return o

    def text(self, s: str, table: str, json_inner: bool = False) -> str:
        if not json_inner and len(s) > 1 and s[0] in "{[":
            try:
                parsed = json.loads(s)
            except (ValueError, RecursionError):
                parsed = None
            if isinstance(parsed, (dict, list)):
                red = self._json_walk(parsed, table)
                if red != parsed:
                    return json.dumps(red, ensure_ascii=False, separators=(",", ":"))
        for rx, needs_entropy in SECRET_VALUE_RES:
            if not rx.search(s):
                continue

            def sub(m):
                tok = m.group(0)
                if needs_entropy and not _random_looking(tok.split(":", 1)[-1] if ":" in tok else tok):
                    return tok
                self._hit(table, "value_pattern")
                return REDACTED
            s = rx.sub(sub, s)
        return s


# -- schema inspection ------------------------------------------------------------------------

def table_list(conn):
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                                       "ORDER BY name")]


def indexes(conn, table):
    out = []
    for seq, name, unique, origin, partial in conn.execute(f"PRAGMA index_list({q(table)})").fetchall():
        cols = [r[2] for r in conn.execute(f"PRAGMA index_info({q(name)})").fetchall()]
        out.append({"name": name, "unique": bool(unique), "origin": origin, "partial": bool(partial), "cols": cols})
    return out


def profile(conn, table, cols, redactor):
    """Per column: python types seen, max text chars / bytes, temporal kinds seen, unparsable."""
    prof = {c["name"]: {"types": set(), "max_chars": 0, "max_bytes": 0, "dt": set(), "dt_bad": 0, "n": 0}
            for c in cols}
    names = [c["name"] for c in cols]
    temporal = {c["name"]: is_temporal(c["type"]) for c in cols}
    for row in conn.execute(f"SELECT {', '.join(q(n) for n in names)} FROM {q(table)}"):
        for n, v in zip(names, row):
            if v is None:
                continue
            p = prof[n]
            p["n"] += 1
            p["types"].add(type(v).__name__)
            if isinstance(v, str):
                ln = len(v)
                if ln > p["max_chars"]:
                    p["max_chars"] = ln
                bl = len(v.encode("utf-8"))
                if bl > p["max_bytes"]:
                    p["max_bytes"] = bl
                if temporal[n]:
                    r = norm_dt(v)
                    if r is None:
                        p["dt_bad"] += 1
                    else:
                        p["dt"].add(r[0])
            elif isinstance(v, bytes):
                p["max_bytes"] = max(p["max_bytes"], len(v))
            elif temporal[n]:
                p["dt_bad"] += 1          # a number in a date column
    return prof


def text_type(max_bytes: int) -> str:
    if max_bytes <= 65535:
        return "TEXT"
    if max_bytes <= 16777215:
        return "MEDIUMTEXT"
    return "LONGTEXT"


def decide_types(cols, prof, indexed: set, notes: list, table: str):
    """column -> (mysql_type, value_kind) where value_kind in int/float/text/blob/date/datetime."""
    out = {}
    for c in cols:
        n, decl, p = c["name"], c["type"], prof[c["name"]]
        aff, tmp, ts = affinity(decl), is_temporal(decl), p["types"]
        idx = n in indexed

        def txt():
            if idx:
                ln = max(32, min(768, math.ceil(max(p["max_chars"], 1) * 1.25)))
                return f"VARCHAR({ln})", "text"
            return text_type(p["max_bytes"]), "text"
        if "bytes" in ts:
            t = ("VARBINARY(768)" if idx else "LONGBLOB", "blob")
        elif tmp and p["dt_bad"] == 0 and ts <= {"str"}:
            kind = "DATETIME" if (tmp == "DATETIME" or "DATETIME" in p["dt"]) else "DATE"
            t = ("DATETIME(6)" if kind == "DATETIME" else "DATE", kind.lower())
        elif tmp:
            t = txt()
            notes.append(f"{table}.{n}: declared {decl} but {p['dt_bad']} value(s) are not plain dates -> {t[0]}")
        elif "str" in ts:
            t = txt()
            if aff in ("INTEGER", "REAL", "NUMERIC"):
                notes.append(f"{table}.{n}: declared {decl} but holds text -> {t[0]}")
        elif "float" in ts:
            t = ("DOUBLE", "float")
        elif "int" in ts:
            t = ("DOUBLE", "float") if aff == "REAL" else ("BIGINT", "int")
        else:                                        # empty column: go by the declaration
            t = {"INTEGER": ("BIGINT", "int"), "REAL": ("DOUBLE", "float"), "NUMERIC": ("DOUBLE", "float"),
                 "BLOB": ("LONGBLOB", "blob")}.get(aff) or (("VARCHAR(191)", "text") if idx else ("TEXT", "text"))
            if aff == "TEXT" and tmp:
                t = ("DATETIME(6)", "datetime") if tmp == "DATETIME" else ("DATE", "date")
        out[n] = t
    return out


def short_name(name: str, limit: int = 64) -> str:
    if len(name) <= limit:
        return name
    h = hashlib.sha1(name.encode()).hexdigest()[:8]
    return name[:limit - 9] + "_" + h


# -- export -------------------------------------------------------------------------------------

class PartWriter:
    def __init__(self, out: Path, prefix: str, part_bytes: int, gz: bool, header: str, footer: str):
        self.out, self.prefix, self.limit, self.gz = out, prefix, part_bytes, gz
        self.header, self.footer = header, footer
        self.n, self.f, self.size, self.files = 0, None, 0, []

    def _open(self):
        self.n += 1
        name = f"{self.prefix}{self.n:03d}.sql" + (".gz" if self.gz else "")
        p = self.out / name
        self.f = gzip.open(p, "wt", encoding="utf-8", compresslevel=6) if self.gz else open(p, "w", encoding="utf-8")
        self.f.write(self.header)
        self.size = len(self.header)
        self.files.append(name)

    def write(self, stmt: str):
        if self.f is None or (self.size + len(stmt) > self.limit and self.size > len(self.header)):
            self.close()
            self._open()
        self.f.write(stmt)
        self.size += len(stmt)

    def close(self):
        if self.f:
            self.f.write(self.footer)
            self.f.close()
            self.f = None


HEADER = ("-- ATIP export ({ts}) part {{part}}\n"
          "SET NAMES utf8mb4;\nSET FOREIGN_KEY_CHECKS=0;\nSET UNIQUE_CHECKS=0;\n"
          "SET SQL_MODE='NO_AUTO_VALUE_ON_ZERO';\nSET AUTOCOMMIT=0;\nSTART TRANSACTION;\n\n")
FOOTER = "\nCOMMIT;\nSET UNIQUE_CHECKS=1;\nSET FOREIGN_KEY_CHECKS=1;\n"


def literal(v, kind, table, redactor):
    if v is None:
        return "NULL"
    if kind == "int":
        if isinstance(v, bool):
            return "1" if v else "0"
        return str(int(v))
    if kind == "float":
        f = float(v)
        return "NULL" if (math.isnan(f) or math.isinf(f)) else repr(f)
    if kind == "blob":
        b = v if isinstance(v, bytes) else str(v).encode("utf-8")
        return "X'" + b.hex() + "'" if b else "''"
    if kind in ("date", "datetime"):
        r = norm_dt(str(v))
        if r is None:
            return "NULL"                      # cannot happen: such columns become text
        val = r[1] if kind == "datetime" or r[0] == "DATE" else r[1][:10]
        if kind == "datetime" and r[0] == "DATE":
            val = r[1] + " 00:00:00"
        return "'" + val + "'"
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    if not isinstance(v, str):
        v = repr(v) if isinstance(v, float) else str(v)
    return mysql_str(redactor.text(v, table))


def export(source: Path, out_root: Path, prefix: str, part_mb: float, gz: bool, only=None, exclude=None,
           rows_per_insert: int = 1000, stmt_bytes: int = 1_000_000) -> dict:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = out_root / ts
    out.mkdir(parents=True, exist_ok=False)
    tmp_dir = tempfile.mkdtemp(prefix="atip_export_")
    snap = Path(tmp_dir) / "snapshot.db"
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(str(snap))
    src.backup(dst)
    src.close()
    dst.close()
    conn = sqlite3.connect(f"file:{snap.as_posix()}?mode=ro", uri=True)
    conn.text_factory = lambda b: b.decode("utf-8", "replace")
    redactor = Redactor()
    notes, skipped_idx, tables_meta = [], [], []
    tables = table_list(conn)
    if only:
        missing = [t for t in only if t not in tables]
        if missing:
            raise SystemExit(f"no such table(s): {missing}")
        tables = [t for t in tables if t in only]
    if exclude:
        tables = [t for t in tables if t not in set(exclude)]
    triggers = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name")]
    views = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view' ORDER BY name")]
    fks = {}

    schema_parts = [HEADER.format(ts=ts).replace("{part}", "schema")]
    data = PartWriter(out, "01_data_part", int(part_mb * 1024 * 1024), gz,
                      HEADER.format(ts=ts).replace("{part}", "data"), FOOTER)
    for t in tables:
        mt = short_name(prefix + t)
        cols = [{"name": r[1], "type": r[2], "notnull": bool(r[3]), "pk": r[5]}
                for r in conn.execute(f"PRAGMA table_info({q(t)})").fetchall()]
        fk = conn.execute(f"PRAGMA foreign_key_list({q(t)})").fetchall()
        if fk:
            fks[t] = len(fk)
        idx = indexes(conn, t)
        pk_cols = [c["name"] for c in sorted((c for c in cols if c["pk"]), key=lambda c: c["pk"])]
        indexed = set(pk_cols)
        for ix in idx:
            if ix["partial"] or any(c is None for c in ix["cols"]):
                continue
            indexed.update(ix["cols"])
        prof = profile(conn, t, cols, redactor)
        types = decide_types(cols, prof, indexed, notes, t)
        rowid_alias = len(pk_cols) == 1 and affinity(next(c["type"] for c in cols if c["name"] == pk_cols[0])) == \
            "INTEGER" and (cols[[c["name"] for c in cols].index(pk_cols[0])]["type"] or "").upper().strip() == "INTEGER"
        defs = []
        for c in cols:
            mtype, kind = types[c["name"]]
            if rowid_alias and c["name"] == pk_cols[0] and kind == "int":
                defs.append(f"  {q(c['name'])} BIGINT NOT NULL AUTO_INCREMENT")
                continue
            nn = " NOT NULL" if (c["notnull"] or c["name"] in pk_cols) and not (
                (t, c["name"]) in COLUMN_RULES and COLUMN_RULES[(t, c["name"])]({}) is None) else ""
            defs.append(f"  {q(c['name'])} {mtype}{nn}")
        if pk_cols:
            defs.append(f"  PRIMARY KEY ({', '.join(q(c) for c in pk_cols)})")
        used_names = set()
        for ix in idx:
            if ix["origin"] == "pk":
                continue
            if ix["partial"] or any(c is None for c in ix["cols"]):
                skipped_idx.append(f"{t}.{ix['name']} ({'partial' if ix['partial'] else 'expression'} index)")
                continue
            kn = short_name(ix["name"] if not ix["name"].startswith("sqlite_autoindex") else
                            f"uq_{t}_{'_'.join(ix['cols'])}")
            while kn in used_names:
                kn = short_name(kn + "_x")
            used_names.add(kn)
            parts = []
            total = 0
            for col in ix["cols"]:
                mtype = types[col][0]
                m = re.match(r"VARCHAR\((\d+)\)", mtype)
                w = int(m.group(1)) if m else 0
                total += w
                parts.append(q(col))
            if total > 768 and not ix["unique"]:       # stay under InnoDB's 3,072-byte key limit
                parts = [f"{q(col)}({min(191, int(re.match(r'VARCHAR[(](\d+)', types[col][0]).group(1)))})"
                         if types[col][0].startswith("VARCHAR") else q(col) for col in ix["cols"]]
            defs.append(f"  {'UNIQUE KEY' if ix['unique'] else 'KEY'} {q(kn)} ({', '.join(parts)})")
        schema_parts.append(f"DROP TABLE IF EXISTS {q(mt)};\nCREATE TABLE {q(mt)} (\n" + ",\n".join(defs) +
                            "\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;\n\n")

        n_rows = conn.execute(f"SELECT COUNT(*) FROM {q(t)}").fetchone()[0]
        exported = 0
        if t not in SKIP_DATA and n_rows:
            names = [c["name"] for c in cols]
            head = f"INSERT INTO {q(mt)} ({', '.join(q(n) for n in names)}) VALUES\n"
            buf, blen = [], 0
            for row in conn.execute(f"SELECT {', '.join(q(n) for n in names)} FROM {q(t)}"):
                rd = dict(zip(names, row))
                vals = []
                for n in names:
                    v = rd[n]
                    rule = COLUMN_RULES.get((t, n))
                    if rule is not None:
                        nv = rule(rd)
                        if nv != v:
                            redactor._hit(t, f"column {n}")
                        v = nv
                    vals.append(literal(v, types[n][1], t, redactor))
                tup = "(" + ",".join(vals) + ")"
                if buf and (len(buf) >= rows_per_insert or blen + len(tup) > stmt_bytes):
                    data.write(head + ",\n".join(buf) + ";\n")
                    buf, blen = [], 0
                buf.append(tup)
                blen += len(tup) + 2
                exported += 1
            if buf:
                data.write(head + ",\n".join(buf) + ";\n")
        tables_meta.append({"table": t, "mysql_table": mt, "rows": n_rows, "exported_rows": exported,
                            "data": "schema only (sensitive)" if t in SKIP_DATA else "full",
                            "columns": {n: types[n][0] for n in types}})
    data.close()
    conn.close()
    try:
        snap.unlink()
        Path(tmp_dir).rmdir()
    except OSError:
        pass

    schema_sql = "".join(schema_parts) + FOOTER
    schema_name = "00_schema.sql" + (".gz" if gz else "")
    if gz:
        with gzip.open(out / schema_name, "wt", encoding="utf-8", compresslevel=6) as f:
            f.write(schema_sql)
    else:
        (out / schema_name).write_text(schema_sql, encoding="utf-8")
    checks = [f"SELECT {mysql_str(m['mysql_table'])} AS tbl, {m['exported_rows']} AS expected, "
              f"(SELECT COUNT(*) FROM {q(m['mysql_table'])}) AS actual" for m in tables_meta]
    verify = ("-- Run after importing every file. Every row should say OK.\n"
              "SELECT tbl, expected, actual, IF(expected = actual, 'OK', 'MISMATCH') AS status FROM (\n  " +
              "\n  UNION ALL ".join(checks) + "\n) v ORDER BY status DESC, tbl;\n")
    (out / "99_verify.sql").write_text(verify, encoding="utf-8")

    files = [schema_name] + data.files + ["99_verify.sql"]
    sizes = {f: (out / f).stat().st_size for f in files}
    manifest = {"created_at": ts, "source": str(source), "prefix": prefix, "files": files, "file_bytes": sizes,
                "tables": tables_meta, "redactions": redactor.counts, "type_notes": notes,
                "skipped_indexes": skipped_idx, "not_exported": {"triggers": triggers, "views": views,
                                                                "foreign_keys": fks},
                "totals": {"tables": len(tables_meta), "rows": sum(m["exported_rows"] for m in tables_meta)}}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (out / "README.txt").write_text(readme(manifest), encoding="utf-8")
    return {"out": str(out), **manifest}


def readme(m: dict) -> str:
    mb = lambda b: f"{b / 1024 / 1024:.1f} MB"
    L = [f"ATIP -> MySQL export, {m['created_at']}", "=" * 60, "",
         f"{m['totals']['tables']} tables, {m['totals']['rows']:,} rows. Table prefix: '{m['prefix']}'.", "",
         "IMPORT (Hostinger hPanel -> Databases -> phpMyAdmin -> pick the database -> Import)",
         "  Import the files one at a time, in this order. Leave the format as SQL; .gz files",
         "  can be uploaded as they are. Wait for each to finish before the next.", ""]
    for i, f in enumerate([f for f in m["files"] if f != "99_verify.sql"], 1):
        L.append(f"  {i:>2}. {f:<28} {mb(m['file_bytes'][f])}")
    L += ["",
          "  Then open the SQL tab, paste the contents of 99_verify.sql and run it (or Import it):",
          "  every row must say OK.",
          "",
          "  00_schema drops and recreates ONLY the tables named with the prefix above, so",
          "  re-importing refreshes the copy. Take a phpMyAdmin Export of the database first if",
          "  it holds anything else you care about.",
          "",
          "  If an upload is refused as too large, re-run with a smaller --part-mb.",
          "",
          "SENSITIVE DATA",
          f"  Rows NOT exported (schema only): {', '.join(sorted(SKIP_DATA))}",
          "  Blanked: enterprise_user.password_hash / mfa_secret_enc / mfa_pending_enc,",
          "           enterprise_api_key.key_hash. (Nobody can log in with the copied users.)",
          "  Redactions by table:"]
    if m["redactions"]:
        for t, c in sorted(m["redactions"].items()):
            L.append(f"    {t}: " + ", ".join(f"{k} x{v}" for k, v in sorted(c.items())))
    else:
        L.append("    none needed")
    L += ["", "NOT EXPORTED",
          f"  Triggers ({len(m['not_exported']['triggers'])}, ATIP's append-only guards; not needed on a copy): "
          f"{', '.join(m['not_exported']['triggers']) or '-'}",
          f"  Views ({len(m['not_exported']['views'])}): {', '.join(m['not_exported']['views']) or '-'}",
          f"  Foreign keys: {sum(m['not_exported']['foreign_keys'].values())} in "
          f"{', '.join(sorted(m['not_exported']['foreign_keys'])) or '-'} (row data is kept; constraints are not)",
          f"  Partial / expression indexes: {', '.join(m['skipped_indexes']) or '-'}",
          "  Column defaults and CHECK constraints (the data carries the values).",
          "", "TYPE NOTES (columns whose stored data did not match the declared type)"]
    L += [f"  {n}" for n in m["type_notes"]] or ["  none"]
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Export ATIP's SQLite database as MySQL .sql files for phpMyAdmin.")
    ap.add_argument("--source", default=str(ROOT / "atip_data" / "atip.db"))
    ap.add_argument("--out", default=str(ROOT / "atip_data" / "mysql_export"))
    ap.add_argument("--prefix", default="atip_", help='table name prefix on MySQL ("" for none)')
    ap.add_argument("--part-mb", type=float, default=40.0, help="max uncompressed size of one data file")
    ap.add_argument("--no-gzip", action="store_true")
    ap.add_argument("--tables", nargs="*")
    ap.add_argument("--exclude", nargs="*")
    a = ap.parse_args(argv)
    src = Path(a.source)
    if not src.exists():
        raise SystemExit(f"no database at {src}")
    if a.prefix and not re.match(r"^[A-Za-z0-9_]{1,20}$", a.prefix):
        raise SystemExit("prefix: letters, digits, underscore; up to 20 characters")
    r = export(src, Path(a.out), a.prefix, a.part_mb, not a.no_gzip, a.tables, a.exclude)
    print(f"exported {r['totals']['tables']} tables, {r['totals']['rows']:,} rows -> {r['out']}")
    for f in r["files"]:
        print(f"  {f:<28} {r['file_bytes'][f] / 1024 / 1024:8.2f} MB")
    red = sum(sum(c.values()) for c in r["redactions"].values())
    print(f"  redactions: {red}; type notes: {len(r['type_notes'])}; see README.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
