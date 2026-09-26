"""
Versioned, auditable migrations (on top of the additive layer in db/schema.py).

db/schema.py keeps creating tables / columns additively (W1-W8, idempotent, never
drops). This module records VERSIONED migrations in schema_migrations:

    version | name | checksum (sha256 of the migration's SQL + name) | applied_at |
    duration_ms | status APPLIED / FAILED | rollback_note | error

apply(conn) runs every pending migration in version order, each in one transaction,
and records it (status FAILED + error when it raises; the transaction is rolled back,
later migrations are not attempted). A migration already APPLIED whose checksum
changed is reported by validate() as DRIFT -- migrations are immutable once applied;
change the database with a NEW version.

Rollback: SQLite cannot drop columns portably, and ATIP's policy is additive only,
so every migration carries a written rollback note (what to drop / how to restore);
the supported rollback of a whole release is restoring the pre-release verified
backup (docs/ROLLBACK_PROCEDURE.md). Nothing here deletes data.
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime

log = logging.getLogger("atip.ops.migrations")

BASELINE_TABLES = ("prices_daily", "ai_scores", "market_health", "pipeline_log", "alert_log", "strategy",
                   "strategy_decision", "risk_decision", "oms_order", "ml_model", "quant_factor", "enterprise_user",
                   "enterprise_audit")


def _sql_0001(conn):
    """Baseline: W1-W7 additive schema present (validation only, no change)."""
    missing = [t for t in BASELINE_TABLES
               if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()]
    if missing:
        raise RuntimeError(f"baseline tables missing: {missing} (run python main.py --init on a new database)")


def _sql_0002(conn):
    """W8 tables (created by db/schema.py W8_TABLES): verify."""
    from db.schema import W8_TABLES
    missing = [t for t in W8_TABLES
               if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()]
    if missing:
        raise RuntimeError(f"W8 tables missing: {missing}")


INDEXES_0003 = (
    "CREATE INDEX IF NOT EXISTS idx_prices_date ON prices_daily(date)",
    "CREATE INDEX IF NOT EXISTS idx_lq_timestamp ON live_quotes(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_pipeline_log_status ON pipeline_log(status, start_time)",
    "CREATE INDEX IF NOT EXISTS idx_ent_audit_at ON enterprise_audit(at)",
)


def _sql_0003(conn):
    for ddl in INDEXES_0003:
        conn.execute(ddl)


TRIGGERS_0004 = (
    """CREATE TRIGGER IF NOT EXISTS trg_ent_audit_no_update BEFORE UPDATE ON enterprise_audit
       BEGIN SELECT RAISE(ABORT, 'enterprise_audit is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_ent_audit_no_delete BEFORE DELETE ON enterprise_audit
       BEGIN SELECT RAISE(ABORT, 'enterprise_audit is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_oms_event_no_update BEFORE UPDATE ON oms_order_event
       BEGIN SELECT RAISE(ABORT, 'oms_order_event is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_oms_event_no_delete BEFORE DELETE ON oms_order_event
       BEGIN SELECT RAISE(ABORT, 'oms_order_event is append-only'); END""",
)


def _sql_0004(conn):
    """Append-only audit trails (the hash chain columns come from db/schema.py)."""
    for ddl in TRIGGERS_0004:
        tbl = "oms_order_event" if "oms_order_event" in ddl else "enterprise_audit"
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)).fetchone():
            conn.execute(ddl)


APPEND_ONLY_0005 = ("investor_profile_version", "perf_ledger", "perf_ledger_void", "perf_report_run",
                    "wealth_allocation_run", "wealth_goal_event")


def _triggers_0005():
    """Append-only W11-W20 audit tables. UPDATE is always refused; DELETE is refused
    except for the 'uat' tenant (W19 persona reset)."""
    out = []
    for t in APPEND_ONLY_0005:
        out.append(f"CREATE TRIGGER IF NOT EXISTS trg_{t}_no_update BEFORE UPDATE ON {t} "
                   f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END")
        out.append(f"CREATE TRIGGER IF NOT EXISTS trg_{t}_no_delete BEFORE DELETE ON {t} WHEN OLD.tenant_id <> 'uat' "
                   f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END")
    return tuple(out)


TRIGGERS_0005 = _triggers_0005()


def _sql_0005(conn):
    """W11-W20 wealth tables (db/schema.py WEALTH_TABLES) present + append-only triggers."""
    from db.schema import WEALTH_TABLES
    missing = [t for t in WEALTH_TABLES
               if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()]
    if missing:
        raise RuntimeError(f"wealth tables missing: {missing}")
    for ddl in TRIGGERS_0005:
        conn.execute(ddl)


MIGRATIONS = (
    ("0001", "baseline_w1_w7", _sql_0001, "none (validation only)"),
    ("0002", "w8_ops_tables", _sql_0002, "DROP the W8 ops_* tables, enterprise_refresh_token and schema_migrations "
                                         "-- or restore the pre-W8 backup"),
    ("0003", "w8_indexes", _sql_0003, "DROP INDEX idx_prices_date, idx_lq_timestamp, idx_pipeline_log_status, "
                                      "idx_ent_audit_at"),
    ("0004", "w8_audit_append_only", _sql_0004, "DROP TRIGGER trg_ent_audit_no_update, trg_ent_audit_no_delete, "
                                                "trg_oms_event_no_update, trg_oms_event_no_delete"),
    ("0005", "w20_wealth_append_only", _sql_0005, "DROP TRIGGER trg_<table>_no_update / trg_<table>_no_delete for "
                                                  "investor_profile_version, perf_ledger, perf_ledger_void, "
                                                  "perf_report_run, wealth_allocation_run, wealth_goal_event; the "
                                                  "wealth tables themselves are additive (restore the pre-W20 backup "
                                                  "to remove them)"),
)


def _checksum(version, name, fn) -> str:
    import inspect
    src = inspect.getsource(fn)
    extra = {"0003": "\n".join(INDEXES_0003), "0004": "\n".join(TRIGGERS_0004),
             "0005": "\n".join(TRIGGERS_0005)}.get(version, "")
    return hashlib.sha256(f"{version}|{name}|{src}|{extra}".encode()).hexdigest()


def status(conn) -> dict:
    try:
        rows = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT version, status, checksum FROM schema_migrations")}
    except Exception:
        rows = {}
    applied = [v for v, (st, _) in rows.items() if st == "APPLIED"]
    failed = [v for v, (st, _) in rows.items() if st == "FAILED"]
    pending = [v for v, *_ in MIGRATIONS if v not in applied]
    return {"applied": sorted(applied), "failed": sorted(failed), "pending": pending,
            "latest": max(applied) if applied else None}


def validate(conn) -> list:
    out = []
    try:
        rows = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT version, status, checksum FROM schema_migrations")}
    except Exception as e:
        return [{"level": "error", "version": None, "message": f"schema_migrations unreadable: {e}"}]
    known = {v for v, *_ in MIGRATIONS}
    for v, name, fn, _ in MIGRATIONS:
        if v in rows and rows[v][0] == "APPLIED" and rows[v][1] != _checksum(v, name, fn):
            out.append({"level": "warning", "version": v, "message": f"{v} {name}: DRIFT (changed after it was applied)"})
        if v in rows and rows[v][0] == "FAILED":
            out.append({"level": "error", "version": v, "message": f"{v} {name}: FAILED"})
    for v in rows:
        if v not in known:
            out.append({"level": "warning", "version": v, "message": f"{v}: applied by a newer release"})
    return out


def apply(conn) -> list:
    """Apply pending migrations in order; returns [{version, name, status, duration_ms, error}]."""
    done = set(status(conn)["applied"])
    results = []
    for v, name, fn, note in MIGRATIONS:
        if v in done:
            continue
        t = time.perf_counter()
        try:
            conn.execute("BEGIN IMMEDIATE")
            fn(conn)
            ms = round((time.perf_counter() - t) * 1000, 1)
            conn.execute("INSERT INTO schema_migrations (version,name,checksum,applied_at,duration_ms,status,"
                         "rollback_note,error) VALUES (?,?,?,?,?,?,?,NULL) ON CONFLICT(version) DO UPDATE SET "
                         "checksum=excluded.checksum, applied_at=excluded.applied_at, duration_ms=excluded.duration_ms,"
                         " status='APPLIED', error=NULL",
                         (v, name, _checksum(v, name, fn), datetime.now(), ms, "APPLIED", note))
            conn.execute("COMMIT")
            results.append({"version": v, "name": name, "status": "APPLIED", "duration_ms": ms})
            log.info(f"  migration {v} {name} applied ({ms} ms)")
        except Exception as e:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            ms = round((time.perf_counter() - t) * 1000, 1)
            try:
                conn.execute("INSERT INTO schema_migrations (version,name,checksum,applied_at,duration_ms,status,"
                             "rollback_note,error) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(version) DO UPDATE SET "
                             "status='FAILED', error=excluded.error, applied_at=excluded.applied_at",
                             (v, name, _checksum(v, name, fn), datetime.now(), ms, "FAILED", note, str(e)[:500]))
                conn.commit()
            except Exception:
                pass
            log.error(f"  migration {v} {name} FAILED: {e}")
            results.append({"version": v, "name": name, "status": "FAILED", "duration_ms": ms, "error": str(e)})
            break
    return results
