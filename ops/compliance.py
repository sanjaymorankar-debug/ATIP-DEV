"""
Compliance monitoring (W38, SEC-05): the controls ATIP already has, checked continuously instead of
assumed. Each check reads state only -- nothing is changed -- and returns PASS / WARN / FAIL / NA with
the evidence it looked at. A run is stored (compliance_run / compliance_result), shown on /compliance
and on the ops page, and a check that gets WORSE than in the previous run raises an alert.

    audit_chain           enterprise_audit hash chain intact (tamper evidence)
    live_trading_gate     real-money orders impossible unless deliberately enabled (master switch)
    algo_order_controls   order limits + kill switch configured (exchange / broker algo-order rules)
    dashboard_exposure    API bound to loopback unless the multi-user platform (auth) is enabled
    admin_mfa             every administrator has MFA (multi-user platform only)
    plaintext_secrets     no secret still read from config.json in plaintext
    encryption_key        ATIP_ENCRYPTION_KEY present (vault, encrypted off-site backups)
    secret_rotation       no catalogued secret overdue for rotation
    backup_recency        a verified backup in the last 26 h
    restore_drill         a successful restore drill in the last 35 days
    privacy_purge         retention purge ran in the last 2 days (multi-user platform only)
    dsr_sla               no data-subject request past its due date (enterprise/privacy.py SLA)
    data_inventory        every table classified in the data inventory (ENT-17)
    regulatory_signoff    no gate crossed (other users / live trading) with ENT-14 items unsigned

What this is NOT: a legal opinion or a SEBI / DPDP audit. ENT-14 (docs/ENT14_REGULATORY_PACK.md)
lists what needs a lawyer's sign-off; these checks keep the technical controls honest in between.

    python -m ops.compliance            # run, store, print
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

PASS, WARN, FAIL, NA = "PASS", "WARN", "FAIL", "NA"
RANK = {PASS: 0, NA: 0, WARN: 1, FAIL: 2}

DDL = (
    """CREATE TABLE IF NOT EXISTS compliance_run (
        run_id TEXT PRIMARY KEY, at TIMESTAMP NOT NULL, trigger TEXT, passed INTEGER, warned INTEGER,
        failed INTEGER, summary TEXT)""",
    """CREATE TABLE IF NOT EXISTS compliance_result (
        run_id TEXT NOT NULL, check_id TEXT NOT NULL, title TEXT, status TEXT NOT NULL, detail TEXT,
        evidence_json TEXT, PRIMARY KEY (run_id, check_id))""",
    "CREATE INDEX IF NOT EXISTS idx_compliance_run_at ON compliance_run(at)",
)


def ensure_tables(conn):
    for d in DDL:
        conn.execute(d)
    conn.commit()


def _ts(v):
    if v is None:
        return None
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v)[:26].replace(" ", "T"))


def _enterprise_on() -> bool:
    try:
        from enterprise.config import settings
        return bool(settings().get("enabled"))
    except Exception:
        return False


def _table_exists(conn, name) -> bool:
    try:
        conn.execute(f"SELECT 1 FROM {name} LIMIT 1")
        return True
    except Exception:
        return False


# ── checks: each (conn, now) -> (status, detail, evidence) ─────────────────
def c_audit_chain(conn, now):
    from enterprise.audit import verify_chain
    if not _table_exists(conn, "enterprise_audit"):
        return NA, "no audit table yet", {}
    r = verify_chain(conn)
    if r["ok"]:
        return PASS, f"{r['checked']} hashed rows verified" + (f", {r['unhashed']} legacy unhashed" if r["unhashed"]
                                                                else ""), r
    return FAIL, f"chain broken at row {r['first_break']}: {r.get('reason')}", r


def c_live_trading_gate(conn, now):
    from ops.trading_safety import report
    r = report()
    if not r.get("LIVE_TRADING_ENABLED"):
        return PASS, f"real-money orders impossible ({r['master_switch']['reason']})", r
    return WARN, "LIVE trading is ENABLED -- confirm this was a deliberate owner decision with ENT-14 sign-off", r


def c_algo_order_controls(conn, now):
    from orders import risk
    try:
        limits = risk.limits()
    except Exception as e:
        return FAIL, f"risk limits unreadable ({type(e).__name__})", {}
    missing = [k for k in ("max_order_value", "max_orders_per_day", "max_open_positions") if not limits.get(k)]
    halted, why = risk.halted()
    ev = {"limits": {k: limits.get(k) for k in risk.LIMIT_KEYS}, "kill_switch": str(risk.HALT_FLAG),
          "halted": halted, "halt_reason": why}
    try:
        from execution.config import execution_settings
        ex = execution_settings()
        ev["max_orders_per_second"] = ex.get("max_orders_per_second")
    except Exception:
        ex = {}
    from ops.trading_safety import report
    live = bool(report().get("LIVE_TRADING_ENABLED"))
    if missing:
        return (FAIL if live else WARN), (f"order limits not set: {', '.join(missing)} (config.json risk_limits)"
                                          + ("" if live else " -- required before any live order")), ev
    if not ex.get("max_orders_per_second"):
        if live:
            return FAIL, ("LIVE trading without a per-second order-rate cap (execution.max_orders_per_second): "
                          "exchange algo rules cap API order rates"), ev
        # paper only: the cap is a pre-live requirement, gated by the ENT-14 LIVE-RISK / SEBI-ALGO items
        return PASS, (f"limits set, kill switch {'ENGAGED' if halted else 'available'}; per-second cap still to "
                      "set before live (ENT-14 LIVE-RISK)"), ev
    return PASS, f"limits set, kill switch {'ENGAGED' if halted else 'available'}", ev


def c_dashboard_exposure(conn, now):
    from dashboard.security import LOCAL_HOST, dashboard_host
    h = dashboard_host()
    if h in (LOCAL_HOST, "localhost", "::1"):
        return PASS, f"bound to {h}", {"host": h}
    if _enterprise_on():
        return WARN, f"bound to {h} with the multi-user platform (sign-in) enabled -- needs ENT-07 TLS / review", \
            {"host": h}
    return FAIL, f"bound to {h} WITHOUT sign-in: anyone on the network can read the API", {"host": h}


def c_admin_mfa(conn, now):
    if not _enterprise_on():
        return NA, "multi-user platform off (single owner)", {}
    from enterprise.rbac import ROLES
    admin_roles = sorted(r for r, (_d, perms) in ROLES.items() if any(p.startswith("admin:") for p in perms))
    rows = conn.execute(
        "SELECT DISTINCT u.username FROM enterprise_user u JOIN enterprise_user_role r ON r.user_id=u.user_id "
        f"WHERE r.role IN ({','.join('?' * len(admin_roles))}) AND u.status='ACTIVE' "
        "AND COALESCE(u.mfa_enabled,0)=0", admin_roles).fetchall()
    names = [r[0] for r in rows]
    return (FAIL, f"{len(names)} administrator(s) without MFA: {', '.join(names[:10])}", {"users": names}) if names \
        else (PASS, "every active administrator has MFA", {})


def c_plaintext_secrets(conn, now):
    from ops.secrets import validate
    legacy = [v["key"] for v in validate() if "plaintext" in v["message"]]
    if legacy:
        return WARN, (f"{len(legacy)} secret(s) still read from config.json in plaintext: {', '.join(legacy)} -- "
                      "move them to atip_data/secrets/ or the environment"), {"secrets": legacy}
    return PASS, "no plaintext secrets in config.json", {}


def c_encryption_key(conn, now):
    from ops.secrets import status
    s = status("ATIP_ENCRYPTION_KEY")
    if s.get("present"):
        return PASS, "present", {}
    return WARN, "ATIP_ENCRYPTION_KEY missing: vault unusable, off-site backups unencrypted (python -m ops keygen)", {}


def c_secret_rotation(conn, now):
    from ops.secrets import rotation_status
    over = [r["name"] for r in rotation_status(conn) if r["overdue"]]
    return (WARN, f"overdue for rotation: {', '.join(over)}", {"overdue": over}) if over \
        else (PASS, "no secret overdue", {})


def c_backup_recency(conn, now):
    if not _table_exists(conn, "ops_backup"):
        return FAIL, "no backup table: backups have never run", {}
    r = conn.execute("SELECT backup_id, finished_at, integrity FROM ops_backup WHERE status='VERIFIED' "
                     "ORDER BY finished_at DESC LIMIT 1").fetchone()
    if not r:
        return FAIL, "no successful backup on record", {}
    at = _ts(r[1])
    age = (now - at).total_seconds() / 3600 if at else None
    ev = {"backup_id": r[0], "finished_at": str(r[1]), "integrity": r[2], "age_hours": round(age or 0, 1)}
    if age is None or age > 26:
        return FAIL, f"last verified backup {age:.0f} h ago" if age else "last backup time unknown", ev
    return PASS, f"verified backup {age:.1f} h ago ({r[2]})", ev


def c_restore_drill(conn, now):
    if not _table_exists(conn, "ops_restore_drill"):
        return WARN, "no restore drill recorded", {}
    r = conn.execute("SELECT drill_id, finished_at FROM ops_restore_drill WHERE status='PASSED' "
                     "ORDER BY finished_at DESC LIMIT 1").fetchone()
    if not r:
        return WARN, "no successful restore drill on record (python -m ops restore-drill)", {}
    age = (now - _ts(r[1])).days
    return (PASS if age <= 35 else WARN), f"last successful drill {age} day(s) ago", {"drill_id": r[0], "days": age}


def c_privacy_purge(conn, now):
    if not _enterprise_on():
        return NA, "multi-user platform off", {}
    # privacy.purge_expired runs inside the 06:30 saas_daily job (enterprise/w32.py)
    r = conn.execute("SELECT MAX(start_time) FROM pipeline_log WHERE kind='run' AND job_name='saas_daily' "
                     "AND status IN ('SUCCESS','EMPTY')").fetchone()
    at = _ts(r[0]) if r else None
    if not at or now - at > timedelta(days=2):
        return FAIL, "retention purge has not run in 2 days", {"last": str(at) if at else None}
    return PASS, f"retention purge ran {at:%Y-%m-%d %H:%M}", {"last": str(at)}


def c_dsr_sla(conn, now):
    if not _table_exists(conn, "enterprise_privacy_request"):
        return NA, "no data-subject requests table", {}
    from enterprise.privacy import sla_status
    s = sla_status(conn, now)
    if s["overdue"]:
        return FAIL, f"{len(s['overdue'])} request(s) past due: {', '.join(r['request_id'] for r in s['overdue'])}", s
    if s["due_soon"]:
        return WARN, f"{len(s['due_soon'])} request(s) due within 7 days", s
    return PASS, f"{s['open']} open, none overdue", s


def c_data_inventory(conn, now):
    from enterprise.privacy import inventory_gaps
    gaps = inventory_gaps(conn)
    if gaps:
        return WARN, f"{len(gaps)} table(s) not classified: {', '.join(gaps[:8])}" + (" ..." if len(gaps) > 8 else ""), \
            {"unclassified": gaps}
    return PASS, "every table is classified", {}


def c_regulatory_signoff(conn, now):
    from ops import regulatory as REG
    from ops.trading_safety import report
    multi, live = _enterprise_on(), bool(report().get("LIVE_TRADING_ENABLED"))
    pm, pl = REG.open_items(conn, "pre_multi_user"), REG.open_items(conn, "pre_live")
    ev = {"open_pre_multi_user": [i["item_id"] for i in pm], "open_pre_live": [i["item_id"] for i in pl]}
    crossed = ([f"multi-user platform enabled with {len(pm)} pre-launch item(s) open"] if multi and pm else []) + \
              ([f"LIVE trading enabled with {len(pl)} pre-live item(s) open"] if live and pl else [])
    if crossed:
        return FAIL, "; ".join(crossed) + " (ENT-14 register)", ev
    if pm or pl:
        return NA, (f"single-owner paper mode; {len(pm)} item(s) to sign off before other users, {len(pl)} before "
                    "live trading"), ev
    return PASS, "every gating item signed off", ev


CHECKS = [
    ("audit_chain", "Audit trail is tamper-evident", c_audit_chain),
    ("live_trading_gate", "Real-money trading is gated", c_live_trading_gate),
    ("algo_order_controls", "Algorithmic order controls", c_algo_order_controls),
    ("dashboard_exposure", "Dashboard network exposure", c_dashboard_exposure),
    ("admin_mfa", "Administrators use MFA", c_admin_mfa),
    ("plaintext_secrets", "No plaintext secrets", c_plaintext_secrets),
    ("encryption_key", "Encryption key present", c_encryption_key),
    ("secret_rotation", "Secrets rotated on schedule", c_secret_rotation),
    ("backup_recency", "Recent verified backup", c_backup_recency),
    ("restore_drill", "Restore drill exercised", c_restore_drill),
    ("privacy_purge", "Retention purge running", c_privacy_purge),
    ("dsr_sla", "Data-subject requests within SLA", c_dsr_sla),
    ("data_inventory", "Data inventory complete", c_data_inventory),
    ("regulatory_signoff", "Regulatory sign-off before each gate", c_regulatory_signoff),
]


def evaluate(conn, now=None) -> list[dict]:
    now = now or datetime.now()
    out = []
    for cid, title, fn in CHECKS:
        try:
            st, detail, ev = fn(conn, now)
        except Exception as e:
            st, detail, ev = FAIL, f"check could not run: {type(e).__name__}: {e}"[:300], {}
        out.append({"check_id": cid, "title": title, "status": st, "detail": detail, "evidence": ev})
    return out


def previous(conn) -> dict:
    r = conn.execute("SELECT run_id FROM compliance_run ORDER BY at DESC LIMIT 1").fetchone()
    if not r:
        return {}
    return {x[0]: x[1] for x in conn.execute("SELECT check_id, status FROM compliance_result WHERE run_id=?", (r[0],))}


def run(conn=None, trigger="scheduled", now=None) -> dict:
    from db.schema import get_connection
    own = conn is None
    conn = conn or get_connection()
    try:
        ensure_tables(conn)
        before = previous(conn)
        results = evaluate(conn, now)
        rid = "cmp_" + uuid.uuid4().hex[:12]
        n = {s: sum(1 for r in results if r["status"] == s) for s in (PASS, WARN, FAIL, NA)}
        worse = [r for r in results if RANK[r["status"]] > RANK.get(before.get(r["check_id"], PASS), 0)]
        summary = f"{n[PASS]} pass, {n[WARN]} warn, {n[FAIL]} fail, {n[NA]} n/a"
        conn.execute("INSERT INTO compliance_run (run_id, at, `trigger`, passed, warned, failed, summary) VALUES "
                     "(?,?,?,?,?,?,?)", (rid, now or datetime.now(), trigger, n[PASS], n[WARN], n[FAIL], summary))
        for r in results:
            conn.execute("INSERT INTO compliance_result (run_id, check_id, title, status, detail, evidence_json) "
                         "VALUES (?,?,?,?,?,?)", (rid, r["check_id"], r["title"], r["status"], r["detail"],
                                                  json.dumps(r["evidence"], default=str)[:20000]))
        conn.commit()
    finally:
        if own:
            conn.close()
    if worse:
        _alert(worse)
    return {"run_id": rid, "summary": summary, "counts": n, "results": results,
            "worsened": [r["check_id"] for r in worse]}


def _alert(worse):
    try:
        from alerts.telegram import notify, fmt, _html
        body = "\n".join(f"{'❌' if r['status'] == FAIL else '⚠️'} <b>{_html(r['title'])}</b>: {_html(r['detail'])}"
                         for r in worse)
        notify(fmt("🛡", "Compliance check changed", body), category="compliance",
               severity="error" if any(r["status"] == FAIL for r in worse) else "warning",
               key=f"compliance:{'|'.join(r['check_id'] + ':' + r['status'] for r in worse)}:{datetime.now():%Y-%m-%d}")
    except Exception as e:
        log.warning(f"  compliance alert: {e}")


def latest(conn, history: int = 30) -> dict:
    ensure_tables(conn)
    runs = [dict(r) for r in conn.execute("SELECT * FROM compliance_run ORDER BY at DESC LIMIT ?", (history,))]
    if not runs:
        return {"run": None, "results": [], "history": []}
    res = []
    for r in conn.execute("SELECT check_id, title, status, detail, evidence_json FROM compliance_result WHERE run_id=?",
                          (runs[0]["run_id"],)):
        d = dict(r)
        d["evidence"] = json.loads(d.pop("evidence_json") or "{}")
        res.append(d)
    order = {cid: i for i, (cid, _, _) in enumerate(CHECKS)}
    res.sort(key=lambda d: order.get(d["check_id"], 99))
    return {"run": runs[0], "results": res, "history": runs}


def run_job() -> dict:
    """Scheduler entry (pipeline/scheduler.py, daily 07:50)."""
    r = run(trigger="scheduled")
    return {"status": "SUCCESS", "rows": len(r["results"]), "summary": r["summary"]}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    r = run(trigger="cli")
    print(r["summary"])
    for x in r["results"]:
        print(f"  {x['status']:<5} {x['title']}: {x['detail']}")
