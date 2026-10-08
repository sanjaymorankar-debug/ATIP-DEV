"""
W32 enterprise additions on top of the W9 SaaS layer (merged and wired in W32).

SEC-02  MFA recovery codes -- enterprise_mfa_recovery holds sha256 digests only:
        generate_recovery_codes(conn, user_id, totp_code)  10 one-time codes (XXXX-XXXX),
            shown ONCE; needs a valid current TOTP code; replaces any earlier set
        use_recovery_code(conn, user_id, code)  consumes one (audited); users.login falls
            back to it when the second factor is not a valid TOTP code
        recovery_status(conn, user_id)  {remaining, generated_at}
        (WebAuthn is not built: it needs a browser origin over HTTPS -- ENT-07 -- and a
        library that is not installed.)

ENT-11  evaluate_alerts_intraday(conn): ACTIVE per-user alert rules whose feature exists
        intraday -- close / price / ltp (latest live quote), change_pct, volume -- are
        evaluated on the newest live_quotes row of today (no older than 20 min); a rule fires
        at most once per day, like the post-market evaluation. Other features stay EOD-only.

API-04  webhook CONSUMERS act on verified inbound events (ops/webhooks.verify_inbound
        records them first; consume() runs after, and the event row's status becomes
        PROCESSED / IGNORED / FAILED):
            payments   payment.succeeded / payment.failed {payment_id | provider_ref}
                       -> the PENDING payment settles, the invoice is PAID or dunning starts
            broker     order.update {broker_order_id} -> that OMS order is refreshed through
                       its adapter (PAPER today; the Dhan adapter still refuses)
            razorpay   W39b: Razorpay's own events (payment.captured / failed, invoice.paid,
                       subscription.*, refund.processed) -> enterprise/razorpay.consume

        SaaS scheduled jobs (enterprise.enabled only): saas_tick (queued deliveries, every
        5 min), saas_daily (billing cycle, dunning, payment-provider reconcile, report
        schedules, privacy retention, 06:30), saas_digest (digest deliveries, 19:00), alerts_intraday (every 15 min in session).
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import date, datetime, timedelta

from enterprise import audit

RECOVERY_COUNT = 10


def _h(code: str) -> str:
    return hashlib.sha256(code.replace("-", "").upper().encode()).hexdigest()


def generate_recovery_codes(conn, user_id, totp_code) -> dict:
    from enterprise import mfa
    if not mfa.verify(conn, user_id, totp_code):
        raise ValueError("a valid current authenticator code is required")
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    codes = ["".join(secrets.choice(alphabet) for _ in range(4)) + "-" + "".join(secrets.choice(alphabet)
                                                                               for _ in range(4))
             for _ in range(RECOVERY_COUNT)]
    now = datetime.now()
    conn.execute("DELETE FROM enterprise_mfa_recovery WHERE user_id=?", (user_id,))
    conn.executemany("INSERT INTO enterprise_mfa_recovery (code_hash,user_id,created_at) VALUES (?,?,?)",
                     [(_h(c), user_id, now) for c in codes])
    audit.record(conn, "mfa.recovery_codes_generated", user_id=user_id, actor=user_id, commit=False)
    conn.commit()
    return {"codes": codes, "note": "store these offline now; they are not shown again and each works once"}


def use_recovery_code(conn, user_id, code) -> bool:
    if not code or len(code.replace("-", "")) != 8:
        return False
    cur = conn.execute("UPDATE enterprise_mfa_recovery SET used_at=? WHERE user_id=? AND code_hash=? AND used_at IS NULL",
                       (datetime.now(), user_id, _h(code)))
    if cur.rowcount:
        audit.record(conn, "mfa.recovery_code_used", user_id=user_id, actor=user_id, commit=False)
        conn.commit()
        return True
    return False


def recovery_status(conn, user_id) -> dict:
    r = conn.execute("SELECT SUM(CASE WHEN used_at IS NULL THEN 1 ELSE 0 END), MAX(created_at) FROM "
                     "enterprise_mfa_recovery WHERE user_id=?", (user_id,)).fetchone()
    return {"remaining": int(r[0] or 0), "generated_at": r[1]}


# ── ENT-11: intraday alerts ─────────────────────────────────────────────────
LIVE_FEATURES = {"close": "ltp", "price": "ltp", "ltp": "ltp", "change_pct": "chg_pct", "volume": "volume"}


def evaluate_alerts_intraday(conn, now=None, max_age_min=20) -> dict:
    from enterprise.notifications import notify
    from enterprise.workspace import OPS
    now = now or datetime.now()
    rules = [dict(r) for r in conn.execute("SELECT * FROM enterprise_alert_rule WHERE status='ACTIVE'")
             if r["feature"] in LIVE_FEATURES]
    fired = 0
    since = (now - timedelta(minutes=max_age_min)).strftime("%Y-%m-%d %H:%M:%S")
    for r in rules:
        if str(r.get("last_triggered_at") or "")[:10] == str(now.date()):
            continue
        q = conn.execute("SELECT ltp, chg_pct, volume, timestamp FROM live_quotes WHERE symbol=? AND "
                         "REPLACE(SUBSTR(timestamp,1,19),'T',' ')>=? ORDER BY timestamp DESC LIMIT 1",
                         (r["symbol"], since)).fetchone()
        if not q:
            continue
        v = {"ltp": q[0], "chg_pct": q[1], "volume": q[2]}[LIVE_FEATURES[r["feature"]]]
        if not isinstance(v, (int, float)):
            continue
        conn.execute("UPDATE enterprise_alert_rule SET last_value=? WHERE rule_id=?", (v, r["rule_id"]))
        if OPS[r["op"]](v, r["value"]):
            notify(conn, r["tenant_id"], "alert", r["name"],
                   f"{r['symbol']} {r['feature']} = {v:.4g} {r['op']} {r['value']} (intraday, {str(q[3])[11:16]})",
                   user_ids=[r["user_id"]])
            conn.execute("UPDATE enterprise_alert_rule SET last_triggered_at=? WHERE rule_id=?", (now, r["rule_id"]))
            fired += 1
    conn.commit()
    return {"rules": len(rules), "triggered": fired}


# ── ENT-03: per-user / per-tenant capital accounting ────────────────────────
def _owner_user(conn, tenant, owner):
    """strategy.owner -> an enterprise user of that tenant (user_id or username), else None."""
    if not owner:
        return None
    r = conn.execute("SELECT u.user_id FROM enterprise_user u WHERE (u.user_id=? OR u.username=?) AND EXISTS "
                     "(SELECT 1 FROM enterprise_user_role r WHERE r.user_id=u.user_id AND r.tenant_id=?)",
                     (owner, owner, tenant)).fetchone()
    return r[0] if r else None


def capital_usage(conn, strategy_id) -> dict | None:
    """Deployed capital (oms_fill positions at the latest close) against max_capital of the
    strategy's tenant profile and of its owning user's profile. None when neither scope
    has a max_capital (nothing to account against) or the enterprise tables are absent."""
    try:
        from enterprise.profiles import get as prof
        from execution.positions import strategy_positions
        r = conn.execute("SELECT COALESCE(tenant_id,'default'), owner FROM strategy WHERE strategy_id=?",
                         (strategy_id,)).fetchone()
        if not r:
            return None
        tenant, owner = r
        uid = _owner_user(conn, tenant, owner)
        t_cap = prof(conn, "TENANT", tenant).get("max_capital")
        u_cap = prof(conn, "USER", uid).get("max_capital") if uid else None
        if t_cap is None and u_cap is None:
            return None
        sids = [x[0] for x in conn.execute("SELECT strategy_id, owner FROM strategy WHERE COALESCE(tenant_id,"
                                            "'default')=?", (tenant,))]
        mine = {x[0] for x in conn.execute("SELECT strategy_id, owner FROM strategy WHERE COALESCE(tenant_id,"
                                            "'default')=?", (tenant,)) if uid and _owner_user(conn, tenant, x[1]) == uid}
        val = {}
        for p in strategy_positions(conn):
            if p["strategy_id"] in sids and p["quantity"]:
                val[p["strategy_id"]] = val.get(p["strategy_id"], 0.0) + (p["value"] or 0.0)
        t_used = round(sum(val.values()), 2)
        u_used = round(sum(v for k, v in val.items() if k in mine), 2)
        out = {"tenant": tenant, "user_id": uid, "tenant_max": t_cap, "tenant_used": t_used,
               "user_max": u_cap, "user_used": u_used}
        rooms = [x for x in ((t_cap - t_used) if t_cap is not None else None,
                             (u_cap - u_used) if u_cap is not None else None) if x is not None]
        out["room"] = round(max(0.0, min(rooms)), 2)
        return out
    except Exception:
        return None


# ── API-04: webhook consumers ───────────────────────────────────────────────
def _payments(conn, p) -> str:
    t = p.get("type")
    if t not in ("payment.succeeded", "payment.failed"):
        return "IGNORED"
    row = None
    if p.get("payment_id"):
        row = conn.execute("SELECT payment_id, invoice_id, tenant_id, status FROM enterprise_payment WHERE payment_id=?",
                           (p["payment_id"],)).fetchone()
    elif p.get("provider_ref"):
        row = conn.execute("SELECT payment_id, invoice_id, tenant_id, status FROM enterprise_payment WHERE "
                           "provider_ref=?", (p["provider_ref"],)).fetchone()
    if not row:
        raise ValueError("unknown payment")
    pid, inv, tid, st = row
    if st in ("SUCCEEDED", "FAILED"):
        return "IGNORED"                                 # already settled: idempotent
    now = datetime.now()
    if t == "payment.succeeded":
        conn.execute("UPDATE enterprise_payment SET status='SUCCEEDED', updated_at=? WHERE payment_id=?", (now, pid))
        conn.execute("UPDATE enterprise_invoice SET status='PAID', paid_at=?, payment_id=? WHERE invoice_id=?",
                     (now, pid, inv))
        conn.execute("UPDATE enterprise_subscription SET status='ACTIVE', dunning_state=NULL, grace_until=NULL, "
                     "updated_at=? WHERE tenant_id=? AND status IN ('PAST_DUE','ACTIVE','TRIAL')", (now, tid))
    else:
        from enterprise.payments import _enter_dunning
        conn.execute("UPDATE enterprise_payment SET status='FAILED', error=?, updated_at=? WHERE payment_id=?",
                     (str(p.get("reason") or "provider reported failure")[:300], now, pid))
        n = conn.execute("SELECT COUNT(*) FROM enterprise_payment WHERE invoice_id=?", (inv,)).fetchone()[0]
        _enter_dunning(conn, tid, n)
    audit.record(conn, "billing.webhook", tenant_id=tid, actor="webhook:payments", resource=inv,
                 details={"payment_id": pid, "type": t}, commit=False)
    conn.commit()
    return "PROCESSED"


def _broker(conn, p) -> str:
    if p.get("type") != "order.update" or not p.get("broker_order_id"):
        return "IGNORED"
    r = conn.execute("SELECT order_id FROM oms_order WHERE broker_order_id=?", (str(p["broker_order_id"]),)).fetchone()
    if not r:
        raise ValueError("unknown broker order")
    from execution.order_manager import refresh_order
    refresh_order(conn, r[0])
    return "PROCESSED"


def _razorpay(conn, p) -> str:
    from enterprise.razorpay import consume as rzp
    return rzp(conn, p)


CONSUMERS = {"payments": _payments, "broker": _broker, "razorpay": _razorpay}


def consume(conn, source, event_id, payload) -> str:
    fn = CONSUMERS.get(source)
    if fn is None:
        status, err = "RECEIVED", None                  # verified + stored, no consumer for this source
    else:
        try:
            status, err = fn(conn, payload if isinstance(payload, dict) else {}), None
        except Exception as e:
            status, err = "FAILED", f"{type(e).__name__}: {e}"[:300]
    conn.execute("UPDATE ops_webhook_event SET status=?, error=COALESCE(?, error) WHERE source=? AND event_id=?",
                 (status, err, source, event_id))
    conn.commit()
    return status


# ── scheduled SaaS jobs ──────────────────────────────────────────────────────
def _enabled():
    from enterprise.config import enabled
    return enabled()


def _run(fn):
    if not _enabled():
        return {"status": "SKIPPED", "rows": 0, "reason": "enterprise.enabled is false"}
    from db.schema import get_connection
    from enterprise.service import ensure_seeded
    conn = get_connection()
    try:
        ensure_seeded(conn)
        return {"status": "SUCCESS", **fn(conn)}
    finally:
        conn.close()


def saas_tick() -> dict:
    from enterprise.channels import run_pending
    return _run(lambda c: {"rows": run_pending(c).get("delivered", 0)})


def saas_digest() -> dict:
    from enterprise.channels import run_pending
    return _run(lambda c: {"rows": run_pending(c, digest=True).get("delivered", 0)})


def saas_daily() -> dict:
    def f(c):
        from enterprise import payments, privacy, reports
        try:                                                                    # W39b: settle what no
            rec = payments.reconcile(c)                                         # webhook reported
        except Exception as e:
            rec = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
        out = {"payments_reconcile": rec, "billing_cycle": payments.run_cycle(c), "dunning": payments.run_dunning(c),
               "reports": reports.run_due(c), "retention": privacy.purge_expired(c),
               "dsr_reminders": privacy.sla_reminders(c)}                      # W38 (ENT-17)
        return {"rows": 6, **out}
    return _run(f)


def alerts_intraday() -> dict:
    return _run(lambda c: {"rows": evaluate_alerts_intraday(c)["triggered"]})
