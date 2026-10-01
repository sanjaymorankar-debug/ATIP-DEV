"""
Payments, invoicing, dunning and plan changes (W9, ENT-04). NO REAL CHARGE IS EVER MADE
in W9: the default provider is SANDBOX and the real-provider adapters refuse.

Providers (config.json "saas.payments.provider"):
    sandbox    simulated: succeeds, unless the tenant setting sandbox_fail_payments is true
               (lets testers exercise dunning)
    noop       records the attempt as PENDING; nothing ever settles
    razorpay / stripe   adapters exist so the interface is fixed, but they REFUSE
               (ProviderNotEnabled) -- enabling a real provider is an owner decision with
               credentials the owner enters, and a separate build step
Validation (ops/config) rejects any non-sandbox/noop provider outside production.

Invoice lifecycle: DRAFT (billing.draft_invoice) -> finalize -> OPEN (due in 7 days)
    -> collect -> PAID | payment FAILED (invoice stays OPEN)
Subscription dunning on a failed collection:
    ACTIVE -> PAST_DUE, dunning_state RETRY_1, grace_until = now + grace_days (7)
    retries on days retry_days (1, 3, 5) after the failure (run_dunning, daily)
    paid -> ACTIVE, dunning cleared
    grace over and still unpaid -> subscription EXPIRED and the tenant SUSPENDED (read-only);
    the owner's default tenant is never suspended
Plan change (change_plan): immediate; prorated credit for the unused part of the old plan
and charge for the new plan's remaining period -> one DRAFT invoice; a downgrade is
refused while current usage exceeds the new plan's limits.
Billing cycle (run_cycle, daily): subscriptions whose period ended get a DRAFT invoice for
the period, finalized and collected, and the period rolls forward.
Payments are idempotent per (invoice, attempt number) (enterprise_payment.idempotency_key).
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from enterprise import audit


class ProviderNotEnabled(RuntimeError):
    pass


def settings() -> dict:
    try:
        cfg = json.loads((Path("atip_data") / "config.json").read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    p = (cfg.get("saas") or {}).get("payments") or {}
    return {"provider": str(p.get("provider") or "sandbox").lower(), "grace_days": int(p.get("grace_days") or 7),
            "retry_days": list(p.get("retry_days") or [1, 3, 5]), "due_days": int(p.get("due_days") or 7),
            "currency": p.get("currency") or "INR"}


class SandboxProvider:
    name = "sandbox"

    def charge(self, conn, tenant_id, amount, currency, reference) -> tuple:
        from enterprise.tenants import get
        fail = bool(((get(conn, tenant_id) or {}).get("settings") or {}).get("sandbox_fail_payments"))
        if fail:
            return "FAILED", None, "sandbox: simulated card decline (tenant setting sandbox_fail_payments)"
        return "SUCCEEDED", "sbx_" + uuid.uuid4().hex[:12], None


class NoopProvider:
    name = "noop"

    def charge(self, conn, tenant_id, amount, currency, reference) -> tuple:
        return "PENDING", None, "noop provider: nothing is charged"


class _Refusing:
    def __init__(self, name):
        self.name = name

    def charge(self, *a, **k):
        raise ProviderNotEnabled(f"payment provider {self.name} is not enabled in W9 (owner decision + build step)")


def provider():
    name = settings()["provider"]
    if name == "sandbox":
        return SandboxProvider()
    if name == "noop":
        return NoopProvider()
    return _Refusing(name)


def _inv(conn, invoice_id) -> dict:
    r = conn.execute("SELECT * FROM enterprise_invoice WHERE invoice_id=?", (invoice_id,)).fetchone()
    if not r:
        raise ValueError(f"no invoice {invoice_id}")
    return dict(r)


def finalize(conn, invoice_id, actor="billing") -> dict:
    inv = _inv(conn, invoice_id)
    if inv["status"] != "DRAFT":
        raise ValueError(f"invoice {invoice_id} is {inv['status']}; only DRAFT invoices are finalized")
    if inv["amount"] is None:
        raise ValueError("the plan has no price (set it with PUT /api/admin/plans/{id}); invoice stays DRAFT")
    status = "PAID" if inv["amount"] <= 0 else "OPEN"
    now = datetime.now()
    conn.execute("UPDATE enterprise_invoice SET status=?, finalized_at=?, due_date=?, paid_at=? WHERE invoice_id=?",
                 (status, now, str(date.today() + timedelta(days=settings()["due_days"])),
                  now if status == "PAID" else None, invoice_id))
    audit.record(conn, "billing.invoice_finalized", tenant_id=inv["tenant_id"], actor=actor, resource=invoice_id,
                 details={"amount": inv["amount"], "status": status}, commit=False)
    conn.commit()
    return _inv(conn, invoice_id)


def collect(conn, invoice_id, actor="billing") -> dict:
    inv = _inv(conn, invoice_id)
    if inv["status"] != "OPEN":
        raise ValueError(f"invoice {invoice_id} is {inv['status']}; only OPEN invoices are collected")
    attempt = conn.execute("SELECT COUNT(*) FROM enterprise_payment WHERE invoice_id=?", (invoice_id,)).fetchone()[0] + 1
    key = f"{invoice_id}:{attempt}"
    prov = provider()
    pid = "pay_" + uuid.uuid4().hex[:16]
    now = datetime.now()
    try:
        status, ref, err = prov.charge(conn, inv["tenant_id"], inv["amount"], inv["currency"], key)
    except ProviderNotEnabled as e:
        status, ref, err = "REFUSED", None, str(e)
    conn.execute("INSERT INTO enterprise_payment (payment_id,tenant_id,invoice_id,provider,amount,currency,status,"
                 "provider_ref,idempotency_key,error,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (pid, inv["tenant_id"], invoice_id, prov.name, inv["amount"], inv["currency"], status, ref, key, err,
                  now, now))
    if status == "SUCCEEDED":
        conn.execute("UPDATE enterprise_invoice SET status='PAID', paid_at=?, payment_id=? WHERE invoice_id=?",
                     (now, pid, invoice_id))
        conn.execute("UPDATE enterprise_subscription SET status='ACTIVE', dunning_state=NULL, grace_until=NULL, "
                     "updated_at=? WHERE tenant_id=? AND status IN ('PAST_DUE','ACTIVE','TRIAL')",
                     (now, inv["tenant_id"]))
    elif status == "FAILED":
        _enter_dunning(conn, inv["tenant_id"], attempt)
    audit.record(conn, "billing.payment", tenant_id=inv["tenant_id"], actor=actor, resource=invoice_id,
                 details={"payment_id": pid, "provider": prov.name, "status": status, "attempt": attempt},
                 commit=False)
    conn.commit()
    if status in ("SUCCEEDED", "FAILED"):
        _tell(conn, inv["tenant_id"], f"Payment {status.lower()} for invoice {invoice_id}",
              err or f"{inv['currency']} {inv['amount']:,.2f}", "warning" if status == "FAILED" else "info")
    return {"payment_id": pid, "status": status, "error": err, "invoice": _inv(conn, invoice_id)}


def _tell(conn, tenant_id, title, body, severity="info"):
    try:
        from enterprise.notifications import notify
        notify(conn, tenant_id, "billing", title, body, permission="admin:billing", severity=severity)
    except Exception:
        pass


def _enter_dunning(conn, tenant_id, attempt):
    s = settings()
    sub = conn.execute("SELECT status, grace_until FROM enterprise_subscription WHERE tenant_id=?",
                       (tenant_id,)).fetchone()
    grace = sub[1] if sub and sub[1] else datetime.now() + timedelta(days=s["grace_days"])
    conn.execute("UPDATE enterprise_subscription SET status='PAST_DUE', dunning_state=?, grace_until=?, updated_at=? "
                 "WHERE tenant_id=?", (f"RETRY_{attempt}", grace, datetime.now(), tenant_id))


def run_dunning(conn, today=None) -> dict:
    """Daily: retry PAST_DUE collections on the retry days; expire + suspend after grace."""
    from enterprise.config import settings as ent
    from enterprise.tenants import set_status
    today = today or date.today()
    s = settings()
    retried = suspended = 0
    for tid, grace in conn.execute("SELECT tenant_id, grace_until FROM enterprise_subscription WHERE status='PAST_DUE'"
                                   ).fetchall():
        inv = conn.execute("SELECT invoice_id FROM enterprise_invoice WHERE tenant_id=? AND status='OPEN' ORDER BY "
                           "created_at", (tid,)).fetchall()
        g = datetime.fromisoformat(str(grace)[:19]) if grace else None
        if g and datetime.now() > g:
            conn.execute("UPDATE enterprise_subscription SET status='EXPIRED', dunning_state='EXPIRED', updated_at=? "
                         "WHERE tenant_id=?", (datetime.now(), tid))
            if tid != ent()["default_tenant"]:
                try:
                    set_status(conn, tid, "SUSPENDED", reason="unpaid invoice after the grace period", actor="billing")
                    suspended += 1
                except ValueError:
                    pass
            conn.commit()
            _tell(conn, tid, "Subscription expired", "The grace period ended with an unpaid invoice; the workspace is "
                                                      "read-only until it is paid.", "warning")
            continue
        for (iid,) in inv:
            last = conn.execute("SELECT MIN(created_at) FROM enterprise_payment WHERE invoice_id=? AND status='FAILED'",
                                (iid,)).fetchone()[0]
            if not last:
                continue
            days = (today - datetime.fromisoformat(str(last)[:19]).date()).days
            done = conn.execute("SELECT COUNT(*) FROM enterprise_payment WHERE invoice_id=? AND DATE(created_at)=?",
                                (iid, str(today))).fetchone()[0]
            if days in s["retry_days"] and not done:
                collect(conn, iid, actor="dunning")
                retried += 1
    return {"retried": retried, "suspended": suspended}


def change_plan(conn, tenant_id, plan_id, actor) -> dict:
    from enterprise import billing
    from enterprise.tenants import usage
    plan = billing.plans(conn, plan_id)
    if not plan:
        raise ValueError(f"no plan {plan_id}")
    plan = plan[0]
    sub = billing.subscription(conn, tenant_id)
    if not sub:
        return billing.subscribe(conn, tenant_id, plan_id, actor=actor)
    use = usage(conn, tenant_id)
    over = [k for k, v in (plan.get("limits") or {}).items()
            if v is not None and (use.get(k.replace("max_", "")) or 0) > v]
    if over:
        raise ValueError(f"current usage exceeds {plan_id} limits: {over}")
    old = billing.plans(conn, sub["plan_id"])[0]
    now = datetime.now()
    end = datetime.fromisoformat(str(sub["current_period_end"])[:19]) if sub.get("current_period_end") else \
        now + timedelta(days=30)
    remaining = max(0.0, (end - now).total_seconds() / 86400)
    frac = min(1.0, remaining / 30.0)
    lines = []
    if old.get("price_month") is not None:
        lines.append({"description": f"credit: unused {sub['plan_id']} ({remaining:.1f} days)",
                      "amount": round(-old["price_month"] * frac, 2)})
    if plan.get("price_month") is not None:
        lines.append({"description": f"{plan_id} for the remaining {remaining:.1f} days",
                      "amount": round(plan["price_month"] * frac, 2)})
    conn.execute("UPDATE enterprise_subscription SET plan_id=?, updated_at=? WHERE tenant_id=?", (plan_id, now, tenant_id))
    inv = None
    if lines:
        inv = "inv_" + uuid.uuid4().hex[:12]
        amount = round(sum(x["amount"] for x in lines), 2)
        conn.execute("INSERT INTO enterprise_invoice (invoice_id,tenant_id,period_start,period_end,plan_id,amount,"
                     "currency,status,lines_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (inv, tenant_id, str(now.date()), str(end.date()), plan_id, max(0.0, amount),
                      plan.get("currency") or "INR", "DRAFT", json.dumps(lines), now))
    audit.record(conn, "billing.plan_change", tenant_id=tenant_id, actor=actor, resource=plan_id,
                 details={"from": sub["plan_id"], "to": plan_id, "proration_invoice": inv, "lines": lines},
                 commit=False)
    conn.commit()
    return {"tenant_id": tenant_id, "from": sub["plan_id"], "to": plan_id, "proration_invoice": inv, "lines": lines}


def run_cycle(conn, today=None) -> dict:
    """Daily: bill every ACTIVE / PAST_DUE subscription whose period ended, then roll it."""
    from enterprise import billing
    today = today or date.today()
    billed = 0
    for tid, pid, start, end in conn.execute(
            "SELECT tenant_id, plan_id, started_at, current_period_end FROM enterprise_subscription WHERE status IN "
            "('ACTIVE','PAST_DUE') AND current_period_end IS NOT NULL AND DATE(current_period_end)<=?",
            (str(today),)).fetchall():
        pend = datetime.fromisoformat(str(end)[:19])
        inv = billing.draft_invoice(conn, tid, str((pend - timedelta(days=30)).date()), str(pend.date()), "billing")
        try:
            finalize(conn, inv["invoice_id"])
            if _inv(conn, inv["invoice_id"])["status"] == "OPEN":
                collect(conn, inv["invoice_id"])
        except ValueError:
            pass                                   # unpriced plan: the invoice stays DRAFT
        conn.execute("UPDATE enterprise_subscription SET current_period_end=?, updated_at=? WHERE tenant_id=?",
                     (pend + timedelta(days=30), datetime.now(), tid))
        conn.commit()
        billed += 1
    return {"billed": billed}


def payments(conn, tenant_id=None, limit=100) -> list:
    q, a = "SELECT * FROM enterprise_payment", []
    if tenant_id:
        q += " WHERE tenant_id=?"; a.append(tenant_id)
    return [dict(r) for r in conn.execute(q + " ORDER BY created_at DESC LIMIT ?", a + [int(limit)])]
