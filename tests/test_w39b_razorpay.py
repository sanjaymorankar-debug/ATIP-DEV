"""W39b: the Razorpay gateway (ENT-04) and its webhook payload mapping (API-04) -- enterprise/razorpay.py.

Every Razorpay response comes from tests/fixtures/razorpay_v1.json through an injected transport, and
urllib's urlopen is replaced by one that fails the test: nothing here reaches the network."""

import base64
import copy
import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

FX = json.loads((Path(__file__).parent / "fixtures" / "razorpay_v1.json").read_text(encoding="utf-8"))
KEY_ID, KEY_SECRET, WH_SECRET = "rzp_test_1DP5mmOlF5G5ag", "Hn4zQ2x8VvTtYp1cR6sLkW0e", "whsec_atip_test_9f8e7d"
SUB, RZP_INV, PAY = "sub_Q7u3Gd1ShLrN9k", "inv_Q7u4Kp2Wm8sYtz", "pay_Q7u5Lr3Xn9tZuA"
SUB_PAY, SUB_INV = "pay_Q7u6SubPay1Cc", "inv_Q7u6SubCyc1Aaa"


class Transport:
    """Replays recorded Razorpay responses by (method, path) and records every request it is given."""

    def __init__(self):
        self.routes, self.calls = {}, []

    def on(self, method, path, *responses):
        self.routes[(method, path)] = list(responses)
        return self

    def __call__(self, method, url, headers, body, timeout):
        u = urlparse(url)
        assert (u.scheme, u.netloc) == ("https", "api.razorpay.com") and u.path.startswith("/v1/")
        path = u.path[len("/v1"):]
        self.calls.append({"method": method, "path": path, "query": {k: v[0] for k, v in parse_qs(u.query).items()},
                           "headers": dict(headers), "body": json.loads(body) if body else None, "timeout": timeout})
        queue = self.routes.get((method, path))
        if not queue:
            raise AssertionError(f"unexpected Razorpay call {method} {path}")
        status, payload = queue.pop(0) if len(queue) > 1 else queue[0]
        return status, json.dumps(payload).encode()

    def made(self, method, path):
        return [c for c in self.calls if (c["method"], c["path"]) == (method, path)]


def ok(name, **patch):
    d = copy.deepcopy(FX["api"][name])
    d.update(patch)
    return 200, d


def collection(*items):
    return 200, {"entity": "collection", "count": len(items), "items": [copy.deepcopy(i) for i in items]}


@pytest.fixture
def env(temp_db, tmp_path, monkeypatch):
    import urllib.request

    import enterprise.config as EC
    import enterprise.razorpay as R
    from db.schema import get_connection, init_db
    from enterprise import billing, payments, service, tenants
    from ops import resilience

    def no_network(*a, **k):
        raise AssertionError("a test tried to reach the network")
    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    monkeypatch.setenv("ATIP_ENV", "development")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"billing": {"provider": "razorpay"}}), encoding="utf-8")
    monkeypatch.setattr(payments, "CONFIG_PATH", cfg)
    monkeypatch.setattr(EC, "CONFIG_PATH", cfg)
    secrets = {"RAZORPAY_KEY_ID": KEY_ID, "RAZORPAY_KEY_SECRET": KEY_SECRET, "WEBHOOK_SECRET_RAZORPAY": WH_SECRET}
    monkeypatch.setattr(R, "_secret", lambda name, log_access=True: secrets.get(name))
    t = Transport()
    monkeypatch.setattr(R, "urllib_transport", t)
    resilience._BREAKERS.pop("razorpay", None)
    init_db()
    conn = get_connection()
    service.ensure_seeded(conn)
    tenants.create(conn, "acme", "Acme Research")
    billing.set_price(conn, "PRO", 999.0, "owner")
    billing.subscribe(conn, "acme", "PRO", "ACTIVE", days=30)
    yield SimpleNamespace(conn=conn, t=t, secrets=secrets, cfg=cfg)
    conn.close()


# ── helpers ───────────────────────────────────────────────────────────────────────────────────
def open_invoice(env, tenant="acme"):
    from enterprise import billing, payments
    inv = billing.draft_invoice(env.conn, tenant, "2026-09-08", "2026-10-08", "test")
    payments.finalize(env.conn, inv["invoice_id"])
    return inv["invoice_id"]


def issue(env, iid):
    """payments.collect through Razorpay: a first invoice for this ATIP invoice."""
    from enterprise import payments
    env.t.on("GET", "/invoices", collection()).on("POST", "/customers", ok("customer")) \
        .on("POST", "/invoices", ok("invoice_issued"))
    return payments.collect(env.conn, iid)


def event(name, at=None):
    ev = copy.deepcopy(FX["webhooks"][name])
    ev["created_at"] = int(time.time() if at is None else at)
    return ev


def sub_event(name, at, start=None, end=None, sub_id=None, tenant=None):
    ev = event(name, at)
    s = ev["payload"]["subscription"]["entity"]
    if start is not None:
        s["current_start"] = start
    if end is not None:
        s["current_end"] = end
    if sub_id:
        s["id"] = sub_id
    if tenant:
        s["notes"]["atip_tenant_id"] = tenant
    return ev


def with_payment(ev, pay_id, invoice_id=None):
    p = ev["payload"]["payment"]["entity"]
    p["id"] = pay_id
    if invoice_id:
        p["invoice_id"] = invoice_id
    return ev


def sign(body, secret=WH_SECRET):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def deliver(conn, ev, event_id=None, signature=None, raw=None):
    """What POST /api/webhooks/razorpay does: verify, then the W32 consumer."""
    from enterprise.w32 import consume
    from ops.webhooks import verify_inbound
    body = raw if raw is not None else json.dumps(ev).encode()
    headers = {"x-razorpay-signature": signature if signature is not None else sign(body),
               "x-razorpay-event-id": event_id or "Evt" + uuid.uuid4().hex[:11]}
    code, out = verify_inbound(conn, "razorpay", headers, body)
    if code == 200 and not out.get("duplicate"):
        out["status"] = consume(conn, "razorpay", out["event_id"], json.loads(body))
    return code, out


def sub_row(conn, tenant="acme"):
    return dict(conn.execute("SELECT * FROM enterprise_subscription WHERE tenant_id=?", (tenant,)).fetchone())


def inv_row(conn, iid):
    return dict(conn.execute("SELECT * FROM enterprise_invoice WHERE invoice_id=?", (iid,)).fetchone())


def pays(conn, iid):
    return [dict(r) for r in conn.execute("SELECT * FROM enterprise_payment WHERE invoice_id=? ORDER BY created_at",
                                          (iid,))]


def paid_invoices(conn, tenant):
    return [dict(r) for r in conn.execute("SELECT * FROM enterprise_invoice WHERE tenant_id=? AND status='PAID'",
                                          (tenant,))]


def start_autopay(env, tenant="acme", sub_id=SUB):
    from enterprise import razorpay as R
    env.t.on("POST", "/plans", ok("plan")).on("POST", "/subscriptions", ok("subscription_created", id=sub_id))
    return R.start_subscription(env.conn, tenant, "PRO", actor="admin")


# ── money and request building ────────────────────────────────────────────────────────────────
def test_paise_are_exact_and_bad_amounts_are_refused():
    from enterprise.razorpay import from_paise, to_paise
    assert to_paise(999) == 99900 and to_paise("999.99") == 99999 and to_paise(0.1 + 0.2 + 0.7) == 100
    assert to_paise(1.005) == 101 and to_paise(19.999) == 2000            # half-up on the decimal value
    for bad in (None, True, -1, 0.99, "abc", float("nan"), float("inf")):
        with pytest.raises(ValueError):
            to_paise(bad)
    assert from_paise(99999) == 999.99 and from_paise(None) == 0.0


def test_invoice_request_has_basic_auth_paise_and_the_idempotent_receipt(env):
    iid = open_invoice(env)
    r = issue(env, iid)
    assert r["status"] == "PENDING" and r["pay_url"] == "https://rzp.io/i/Xy7Zq1Pa" and r["error"] is None
    assert [(c["method"], c["path"]) for c in env.t.calls] == [("GET", "/invoices"), ("POST", "/customers"),
                                                               ("POST", "/invoices")]
    auth = "Basic " + base64.b64encode(f"{KEY_ID}:{KEY_SECRET}".encode()).decode()
    assert all(c["headers"]["Authorization"] == auth and c["timeout"] == 20 for c in env.t.calls)
    receipt = f"{iid}-1"
    assert env.t.calls[0]["query"] == {"receipt": receipt} and env.t.calls[0]["body"] is None
    post = env.t.made("POST", "/invoices")[0]
    assert post["headers"]["Content-Type"] == "application/json"
    b = post["body"]
    assert set(b) == {"type", "customer_id", "currency", "receipt", "description", "line_items", "sms_notify",
                      "email_notify", "notes"}, "Razorpay rejects undocumented fields"
    assert (b["type"], b["receipt"], b["currency"], b["customer_id"]) == ("invoice", receipt, "INR", "cust_Q7u1ZkHKwXkF0e")
    assert b["line_items"] == [{"name": "ATIP PRO plan", "description": "2026-09-08 to 2026-10-08", "amount": 99900,
                                "currency": "INR", "quantity": 1}]
    assert b["notes"] == {"atip_invoice_id": iid, "atip_tenant_id": "acme"} and b["sms_notify"] == b["email_notify"] == 1
    assert env.t.made("POST", "/customers")[0]["body"] == {"name": "Acme Research", "fail_existing": "0",
                                                           "notes": {"atip_tenant_id": "acme"}}
    p = pays(env.conn, iid)
    assert len(p) == 1 and (p[0]["provider"], p[0]["status"], p[0]["provider_ref"], p[0]["idempotency_key"]) == \
        ("razorpay", "PENDING", RZP_INV, f"{iid}:1")
    dump = "\n".join(str(tuple(r)) for t in ("enterprise_billing_ref", "enterprise_payment", "enterprise_audit")
                     for r in env.conn.execute(f"SELECT * FROM {t}"))
    assert KEY_SECRET not in dump and auth.split()[1] not in dump, "credentials are never stored"


def test_issuing_is_idempotent_by_receipt_and_by_the_local_ref(env):
    from enterprise import payments
    iid = open_invoice(env)
    # Razorpay made the invoice but ATIP crashed before storing it: GET ?receipt= adopts it
    env.t.on("GET", "/invoices", collection(FX["api"]["invoice_issued"]))
    r = payments.collect(env.conn, iid)
    assert r["status"] == "PENDING" and r["pay_url"] == "https://rzp.io/i/Xy7Zq1Pa"
    assert not env.t.made("POST", "/invoices") and not env.t.made("POST", "/customers")
    # a retry while it is still payable: the same link and a reminder, never a second invoice
    env.t.calls.clear()
    env.t.on("GET", f"/invoices/{RZP_INV}", ok("invoice_issued")) \
        .on("POST", f"/invoices/{RZP_INV}/notify_by/email", (200, {"success": True}))
    r2 = payments.collect(env.conn, iid)
    assert r2["status"] == "PENDING" and r2["pay_url"] == "https://rzp.io/i/Xy7Zq1Pa"
    assert [(c["method"], c["path"]) for c in env.t.calls] == [("GET", f"/invoices/{RZP_INV}"),
                                                               ("POST", f"/invoices/{RZP_INV}/notify_by/email")]
    assert [(p["status"], p["idempotency_key"]) for p in pays(env.conn, iid)] == [("SUPERSEDED", f"{iid}:1"),
                                                                                    ("PENDING", f"{iid}:2")]
    # paid at Razorpay and no webhook reached ATIP: pressing Pay again settles it
    env.t.on("GET", f"/invoices/{RZP_INV}", ok("invoice_paid"))
    r3 = payments.collect(env.conn, iid)
    assert r3["status"] == "SUCCEEDED" and r3["invoice"]["status"] == "PAID"
    assert [p["status"] for p in pays(env.conn, iid)] == ["SUPERSEDED", "SUPERSEDED", "SUCCEEDED"]
    assert pays(env.conn, iid)[-1]["provider_payment_id"] == PAY


def test_razorpay_errors_are_parsed_and_never_start_dunning(env):
    from enterprise import payments
    from enterprise.razorpay import RazorpayError, RazorpayProvider
    iid = open_invoice(env)
    env.t.on("GET", "/invoices", collection()).on("POST", "/customers", ok("customer")) \
        .on("POST", "/invoices", (400, FX["api"]["error_bad_request"]))
    r = payments.collect(env.conn, iid)
    assert r["status"] == "ERROR" and r["invoice"]["status"] == "OPEN"
    assert "BAD_REQUEST_ERROR (HTTP 400): The amount must be atleast INR 1.00 [field amount]" in r["error"]
    s = sub_row(env.conn)
    assert (s["status"], s["dunning_state"]) == ("ACTIVE", None), "a rejected request is not a failed payment"
    t = Transport().on("GET", "/customers/cust_x", (401, FX["api"]["error_auth"]))
    with pytest.raises(RazorpayError) as e:
        RazorpayProvider(transport=t).request("GET", "/customers/cust_x")
    assert (e.value.status, e.value.code, e.value.description) == (401, "BAD_REQUEST_ERROR", "Authentication failed")

    def reset(*a):
        raise ConnectionResetError("connection reset by peer")
    with pytest.raises(RazorpayError, match="NETWORK: ConnectionResetError") as e:
        RazorpayProvider(transport=reset).request("GET", "/invoices")
    assert KEY_SECRET not in str(e.value)
    with pytest.raises(RazorpayError, match="BAD_RESPONSE"):
        RazorpayProvider(transport=lambda *a: (200, b"[]")).request("GET", "/invoices")


def test_customer_plan_subscription_and_cancel_requests(env):
    from enterprise import razorpay as R
    out = start_autopay(env)
    assert out == {"subscription_id": SUB, "status": "created", "short_url": "https://rzp.io/i/AbC12dEf",
                   "plan_id": "PRO", "existing": False}
    plan = env.t.made("POST", "/plans")[0]["body"]
    assert set(plan) == {"period", "interval", "item", "notes"} and set(plan["item"]) == {"name", "amount", "currency",
                                                                                         "description"}
    assert (plan["period"], plan["interval"], plan["item"]["amount"], plan["item"]["currency"]) == ("monthly", 1, 99900,
                                                                                                    "INR")
    sub = env.t.made("POST", "/subscriptions")[0]["body"]
    assert set(sub) == {"plan_id", "total_count", "quantity", "customer_notify", "expire_by", "notes"}
    assert (sub["plan_id"], sub["total_count"], sub["quantity"], sub["customer_notify"]) == ("plan_Q7u2bWv3xkHnAx", 120,
                                                                                            1, 1)
    assert sub["notes"] == {"atip_tenant_id": "acme", "atip_plan_id": "PRO"} and sub["expire_by"] > time.time() + 6 * 86400
    again = R.start_subscription(env.conn, "acme", "PRO")
    assert again["existing"] and again["short_url"] == "https://rzp.io/i/AbC12dEf"
    assert len(env.t.made("POST", "/plans")) == len(env.t.made("POST", "/subscriptions")) == 1
    assert R.autopay_status(env.conn, "acme")["short_url"] == "https://rzp.io/i/AbC12dEf"
    # customers are made once per tenant and mode
    env.t.on("POST", "/customers", ok("customer"))
    p = R.RazorpayProvider()
    assert p.create_customer(env.conn, "acme")["id"] == "cust_Q7u1ZkHKwXkF0e"
    assert p.create_customer(env.conn, "acme") == {"id": "cust_Q7u1ZkHKwXkF0e", "existing": True}
    assert len(env.t.made("POST", "/customers")) == 1
    from enterprise.tenants import create
    create(env.conn, "zz", "Z-")
    assert R._customer_fields(env.conn, "zz") == {"name": "ATIP zz"}, "Razorpay names need 3-50 plain characters"
    # a yearly plan is twelve months of the monthly price; an unpriced plan is refused
    t = Transport().on("POST", "/plans", ok("plan", id="plan_Q7uYearly0001x"))
    R.RazorpayProvider(transport=t, cfg={"billing": {"razorpay": {"period": "yearly"}}}).ensure_plan(env.conn, "PRO")
    assert (t.calls[0]["body"]["period"], t.calls[0]["body"]["item"]["amount"]) == ("yearly", 99900 * 12)
    with pytest.raises(ValueError, match="no price"):
        R.start_subscription(env.conn, "acme", "ENTERPRISE")
    # cancel at the end of the cycle: Razorpay keeps it active until then
    env.t.on("POST", f"/subscriptions/{SUB}/cancel", ok("subscription_active"))
    c = R.cancel_subscription(env.conn, "acme", at_cycle_end=True)
    assert env.t.made("POST", f"/subscriptions/{SUB}/cancel")[0]["body"] == {"cancel_at_cycle_end": 1}
    assert c["status"] == "active" and sub_row(env.conn)["cancel_at"] is not None
    # cancel now: Razorpay answers cancelled and ATIP applies it at once
    env.t.on("POST", f"/subscriptions/{SUB}/cancel", ok("subscription_cancelled"))
    R.cancel_subscription(env.conn, "acme", at_cycle_end=False)
    assert env.t.made("POST", f"/subscriptions/{SUB}/cancel")[1]["body"] == {"cancel_at_cycle_end": 0}
    assert sub_row(env.conn)["status"] == "CANCELLED"
    with pytest.raises(ValueError, match="no Razorpay autopay"):
        R.cancel_subscription(env.conn, "acme")


# ── configuration: unconfigured, live mode ────────────────────────────────────────────────────
def _findings(cfg):
    from ops.config import validate
    return {(x["level"], x["key"]) for x in validate(cfg)
            if x["key"].startswith(("RAZORPAY", "billing", "WEBHOOK_SECRET_RAZORPAY"))}


def test_unconfigured_provider_refuses_and_nothing_breaks(env):
    from enterprise import payments
    from enterprise import razorpay as R
    env.secrets.clear()
    st = payments.provider_status()
    assert (st["state"], st["key_id"], st["webhook_configured"]) == ("UNCONFIGURED", None, False)
    iid = open_invoice(env)
    r = payments.collect(env.conn, iid)
    assert r["status"] == "REFUSED" and "UNCONFIGURED" in r["error"] and r["invoice"]["status"] == "OPEN"
    assert (sub_row(env.conn)["status"], sub_row(env.conn)["dunning_state"]) == ("ACTIVE", None)
    assert payments.run_dunning(env.conn) == {"retried": 0, "suspended": 0}
    assert payments.reconcile(env.conn) == {"skipped": "UNCONFIGURED"}
    with pytest.raises(payments.ProviderNotEnabled):
        R.start_subscription(env.conn, "acme", "PRO")
    assert env.t.calls == [], "nothing is called without credentials"
    code, out = deliver(env.conn, event("payment.captured"))
    assert code == 503 and out["error"]["code"] == "DEPENDENCY_UNAVAILABLE", "no webhook secret: the endpoint is off"
    assert _findings({"environment": "development", "billing": {"provider": "razorpay"}}) == {("warning",
                                                                                               "RAZORPAY_KEY_ID")}
    assert _findings({"environment": "development", "billing": {"provider": "razorpay",
                                                                "razorpay": {"key_secret": "x" * 12}}}) == \
        {("warning", "RAZORPAY_KEY_ID"), ("error", "billing.razorpay")}, "credentials never belong in config.json"


def test_live_keys_are_refused_without_the_switch_and_outside_production(env):
    from enterprise import payments
    from enterprise.razorpay import RazorpayProvider
    live = "rzp_live_9QwErTy1234AbCd"
    on = {"billing": {"provider": "razorpay", "razorpay": {"allow_live": True}}}

    def state(cfg, envname, key=live):
        return RazorpayProvider(key_id=key, key_secret="s3cret", cfg=cfg, environment=envname).status()["state"]
    assert state({}, "production") == "LIVE_REFUSED"
    assert state({"billing": {"razorpay": {"allow_live": "true"}}}, "production") == "LIVE_REFUSED", "JSON true only"
    assert state(on, "staging") == "LIVE_REFUSED" and state(on, "development") == "LIVE_REFUSED"
    assert state(on, "production") == "LIVE"
    assert state({}, "production", "rzp_test_abcdef123456") == "TEST" and state({}, "development", "key_1234567890") \
        == "INVALID_KEY"
    t = Transport()
    with pytest.raises(payments.ProviderNotEnabled, match="LIVE_REFUSED"):
        RazorpayProvider(key_id=live, key_secret="s3cret", transport=t, cfg={}, environment="production") \
            .request("GET", "/invoices")
    assert t.calls == []
    env.secrets["RAZORPAY_KEY_ID"] = live
    r = payments.collect(env.conn, open_invoice(env))
    assert r["status"] == "REFUSED" and "LIVE_REFUSED" in r["error"] and env.t.calls == []
    assert ("error", "RAZORPAY_KEY_ID") in _findings({"environment": "development", "billing": {"provider": "razorpay"}})
    assert ("error", "billing.razorpay.allow_live") in _findings({"environment": "staging", **on})
    assert _findings({"environment": "production", "billing": {"provider": "razorpay"}}) == \
        {("warning", "billing.razorpay.allow_live")}
    assert _findings({"environment": "production", **on}) == set()
    env.secrets["RAZORPAY_KEY_ID"] = KEY_ID
    del env.secrets["WEBHOOK_SECRET_RAZORPAY"]
    assert _findings({"environment": "production", "billing": {"provider": "razorpay"}}) == \
        {("warning", "RAZORPAY_KEY_ID"), ("warning", "WEBHOOK_SECRET_RAZORPAY")}     # test mode; polling only


# ── webhook verification ──────────────────────────────────────────────────────────────────────
def test_signature_good_bad_and_tampered(env, caplog):
    from enterprise.razorpay import verify_signature
    iid = open_invoice(env)
    issue(env, iid)
    body = json.dumps(event("payment.captured")).encode()
    assert verify_signature(WH_SECRET, body, sign(body)) and verify_signature(WH_SECRET, body, " " + sign(body))
    assert not verify_signature(WH_SECRET, body, sign(body, "another-secret"))
    assert not verify_signature(WH_SECRET, body + b" ", sign(body))
    assert not verify_signature(WH_SECRET, body, "") and not verify_signature(WH_SECRET, body, "é" * 64)
    with caplog.at_level(logging.WARNING, logger="atip.enterprise.razorpay"):
        code, out = deliver(env.conn, None, raw=body, signature=sign(body, "another-secret"))
        assert code == 401 and out["error"]["code"] == "UNAUTHENTICATED"
        tampered = body.replace(b'"amount": 99900', b'"amount": 100', 1)
        assert tampered != body
        code, _ = deliver(env.conn, None, raw=tampered, signature=sign(body))
        assert code == 401
    assert caplog.text.count("invalid X-Razorpay-Signature") == 2
    rows = env.conn.execute("SELECT status, signature_ok, error FROM ops_webhook_event WHERE source='razorpay'").fetchall()
    assert [tuple(r) for r in rows] == [("REJECTED", 0, "bad signature")] * 2
    assert inv_row(env.conn, iid)["status"] == "OPEN", "a rejected event changes nothing"
    code, out = deliver(env.conn, None, raw=body, signature="")
    assert code == 400 and "X-Razorpay-Signature" in out["error"]["message"]
    code, out = deliver(env.conn, None, raw=body)
    assert code == 200 and out["status"] == "PROCESSED" and inv_row(env.conn, iid)["status"] == "PAID"


def test_redelivery_and_a_replayed_body_are_duplicates_and_old_events_refused(env):
    iid = open_invoice(env)
    issue(env, iid)
    ev = event("payment.captured")
    assert deliver(env.conn, ev, event_id="EvtCap000001")[1]["status"] == "PROCESSED"
    code, out = deliver(env.conn, ev, event_id="EvtCap000001")             # Razorpay retries the same event
    assert (code, out["duplicate"]) == (200, True)
    code, out = deliver(env.conn, ev, event_id="EvtForged0001")            # the signed body, a new unsigned id
    assert (code, out["duplicate"]) == (200, True)
    assert deliver(env.conn, event("invoice.paid"))[1]["status"] == "IGNORED", "the same payment, another event"
    assert [p["status"] for p in pays(env.conn, iid)] == ["SUCCEEDED"]
    code, out = deliver(env.conn, event("payment.authorized", at=time.time() - 73 * 3600))
    assert code == 401 and "replay window" in out["error"]["message"]
    st = {r[0]: r[1] for r in env.conn.execute("SELECT event_id, status FROM ops_webhook_event WHERE source='razorpay' "
                                               "AND signature_ok=1")}
    assert st["EvtCap000001"] == "PROCESSED" and "EvtForged0001" not in st


# ── event mapping: one-off invoices ───────────────────────────────────────────────────────────
def test_payment_captured_marks_the_invoice_paid(env):
    iid = open_invoice(env)
    issue(env, iid)
    code, out = deliver(env.conn, event("payment.captured"), event_id="EvtCap000002")
    assert (code, out["status"]) == (200, "PROCESSED")
    inv, p = inv_row(env.conn, iid), pays(env.conn, iid)
    assert inv["status"] == "PAID" and inv["paid_at"] and inv["payment_id"] == p[0]["payment_id"]
    assert len(p) == 1 and (p[0]["status"], p[0]["provider_payment_id"], p[0]["amount"]) == ("SUCCEEDED", PAY, 999.0)
    assert env.conn.execute("SELECT status FROM ops_webhook_event WHERE event_id='EvtCap000002'").fetchone()[0] == \
        "PROCESSED"
    assert env.conn.execute("SELECT COUNT(*) FROM enterprise_audit WHERE action='billing.webhook' AND resource=?",
                            (iid,)).fetchone()[0] == 1


def test_invoice_paid_alone_settles_and_a_later_capture_is_ignored(env):
    iid = open_invoice(env)
    issue(env, iid)
    assert deliver(env.conn, event("invoice.paid"))[1]["status"] == "PROCESSED"
    assert inv_row(env.conn, iid)["status"] == "PAID"
    assert deliver(env.conn, event("payment.captured"))[1]["status"] == "IGNORED"
    assert len(pays(env.conn, iid)) == 1


def test_payment_failed_starts_dunning_and_a_capture_clears_it(env):
    from enterprise import payments
    iid = open_invoice(env)
    issue(env, iid)
    t0 = int(time.time())
    assert deliver(env.conn, event("payment.failed", at=t0 - 60))[1]["status"] == "PROCESSED"
    p = pays(env.conn, iid)
    assert (p[0]["status"], p[0]["provider_payment_id"]) == ("FAILED", "pay_Q7u5F0aIl3dWsB") and \
        "bank declined" in p[0]["error"]
    s = sub_row(env.conn)
    assert (s["status"], s["dunning_state"]) == ("PAST_DUE", "RETRY_1") and s["grace_until"]
    assert deliver(env.conn, event("payment.failed", at=t0 - 59))[1]["status"] == "IGNORED", "same payment again"
    # retry day 1: the same Razorpay invoice again, with a reminder -- not a second invoice
    env.t.on("GET", f"/invoices/{RZP_INV}", ok("invoice_issued")) \
        .on("POST", f"/invoices/{RZP_INV}/notify_by/email", (200, {"success": True}))
    assert payments.run_dunning(env.conn, today=date.today() + timedelta(days=1))["retried"] == 1
    assert len(env.t.made("POST", "/invoices")) == 1 and len(env.t.made("POST", f"/invoices/{RZP_INV}/notify_by/email")) == 1
    assert [x["status"] for x in pays(env.conn, iid)] == ["FAILED", "PENDING"]
    # the customer pays: PAID, ACTIVE, dunning and grace cleared
    assert deliver(env.conn, event("payment.captured", at=t0))[1]["status"] == "PROCESSED"
    assert inv_row(env.conn, iid)["status"] == "PAID"
    s = sub_row(env.conn)
    assert (s["status"], s["dunning_state"], s["grace_until"]) == ("ACTIVE", None, None)
    assert [x["status"] for x in pays(env.conn, iid)] == ["FAILED", "SUCCEEDED"]
    # a failure delivered out of order, after the payment: ignored, no dunning
    late = with_payment(event("payment.failed", at=t0 - 30), "pay_Q7u5LateFail9")
    assert deliver(env.conn, late)[1]["status"] == "IGNORED"
    assert sub_row(env.conn)["status"] == "ACTIVE" and len(pays(env.conn, iid)) == 2


def test_refunds_partial_then_full_and_each_once(env):
    iid = open_invoice(env)
    issue(env, iid)
    deliver(env.conn, event("payment.captured"))
    assert deliver(env.conn, event("refund.processed"))[1]["status"] == "PROCESSED"           # Rs 400 of 999
    p = pays(env.conn, iid)[0]
    assert (p["status"], p["refunded_amount"]) == ("PARTIALLY_REFUNDED", 400.0) and inv_row(env.conn, iid)["status"] == "PAID"
    assert deliver(env.conn, event("refund.processed", at=time.time() + 1))[1]["status"] == "IGNORED", "same refund id"
    rest = event("refund.processed")
    rest["payload"]["refund"]["entity"].update({"id": "rfnd_Q7u9Rf2Rest4X", "amount": 59900})
    assert deliver(env.conn, rest)[1]["status"] == "PROCESSED"
    p = pays(env.conn, iid)[0]
    assert (p["status"], p["refunded_amount"]) == ("REFUNDED", 999.0) and inv_row(env.conn, iid)["status"] == "REFUNDED"
    other = event("refund.processed")
    other["payload"]["refund"]["entity"].update({"id": "rfnd_Q7u9NotAtip01", "payment_id": "pay_NotAtip0000001"})
    assert deliver(env.conn, other)[1]["status"] == "IGNORED"


# ── event mapping: autopay subscriptions ──────────────────────────────────────────────────────
def test_autopay_charged_before_activated(env):
    from enterprise import payments
    start_autopay(env)
    now = int(time.time())
    start, end = now - 60, now + 35 * 86400                     # past ATIP's own period end: it moves forward
    assert deliver(env.conn, sub_event("subscription.charged", now, start, end))[1]["status"] == "PROCESSED"
    s = sub_row(env.conn)
    assert (s["status"], s["payment_provider"], s["plan_id"]) == ("ACTIVE", "razorpay", "PRO")
    assert str(s["current_period_end"])[:10] == str(datetime.fromtimestamp(end).date())
    paid = paid_invoices(env.conn, "acme")
    assert len(paid) == 1 and paid[0]["amount"] == 999.0
    assert str(paid[0]["period_end"]) == str(datetime.fromtimestamp(end).date())
    lines = json.loads(paid[0]["lines_json"])
    assert (lines["razorpay_payment_id"], lines["razorpay_invoice_id"], lines["subscription_id"]) == (SUB_PAY, SUB_INV, SUB)
    # activated was created earlier but arrives later: no status change, the period end does not move back
    assert deliver(env.conn, sub_event("subscription.activated", now - 5, start, end - 86400))[1]["status"] == "IGNORED"
    assert sub_row(env.conn)["current_period_end"] == s["current_period_end"]
    # the same cycle reported again by invoice.paid and payment.captured: recorded once
    assert deliver(env.conn, event("invoice.paid.subscription", now + 1))[1]["status"] == "IGNORED"
    assert deliver(env.conn, with_payment(event("payment.captured", now + 2), SUB_PAY, SUB_INV))[1]["status"] == "IGNORED"
    assert len(paid_invoices(env.conn, "acme")) == 1
    assert env.conn.execute("SELECT COUNT(*) FROM enterprise_payment WHERE provider_payment_id=?",
                            (SUB_PAY,)).fetchone()[0] == 1
    # Razorpay bills autopay tenants: ATIP's own billing cycle leaves them alone
    env.conn.execute("UPDATE enterprise_subscription SET current_period_end=? WHERE tenant_id='acme'",
                     (datetime.now() - timedelta(days=1),))
    env.conn.commit()
    assert payments.run_cycle(env.conn)["billed"] == 0
    with pytest.raises(ValueError, match="autopay"):
        payments.change_plan(env.conn, "acme", "FREE", "admin")


def test_invoice_paid_for_a_cycle_without_subscription_charged(env):
    start_autopay(env)
    assert deliver(env.conn, event("invoice.paid.subscription"))[1]["status"] == "PROCESSED"
    paid = paid_invoices(env.conn, "acme")
    assert len(paid) == 1 and str(paid[0]["period_start"]) == str(datetime.fromtimestamp(1759900200).date())
    # subscription.charged for the same payment afterwards: the status applies, the money is not recorded twice
    assert deliver(env.conn, sub_event("subscription.charged", time.time()))[1]["status"] == "PROCESSED"
    assert len(paid_invoices(env.conn, "acme")) == 1 and sub_row(env.conn)["payment_provider"] == "razorpay"
    assert env.conn.execute("SELECT COUNT(*) FROM enterprise_payment WHERE provider_payment_id=?",
                            (SUB_PAY,)).fetchone()[0] == 1


def test_other_webhook_sources_keep_the_atip_signature(env, monkeypatch):
    from ops.webhooks import sign as atip_sign
    from ops.webhooks import verify_inbound
    monkeypatch.setenv("WEBHOOK_SECRET_PAYMENTS", "atip-payments-secret-1")
    body, ts = b'{"type": "payment.succeeded", "payment_id": "pay_x"}', str(int(time.time()))
    h = {"x-atip-timestamp": ts, "x-atip-event-id": "atip-evt-1",
         "x-atip-signature": atip_sign("atip-payments-secret-1", ts, body)}
    assert verify_inbound(env.conn, "payments", h, body)[0] == 200
    assert verify_inbound(env.conn, "payments", {"x-razorpay-signature": sign(body)}, body)[0] == 400, \
        "the Razorpay scheme applies to source razorpay only"


def test_autopay_failure_halt_grace_expiry_and_recovery(env):
    from enterprise import payments, tenants
    start_autopay(env)
    t0 = int(time.time())
    assert deliver(env.conn, sub_event("subscription.activated", t0, t0 - 60, t0 + 30 * 86400))[1]["status"] == \
        "PROCESSED"
    assert (sub_row(env.conn)["status"], sub_row(env.conn)["payment_provider"]) == ("ACTIVE", "razorpay")
    assert deliver(env.conn, sub_event("subscription.pending", t0 + 10))[1]["status"] == "PROCESSED"
    s = sub_row(env.conn)
    assert (s["status"], s["dunning_state"]) == ("PAST_DUE", "PROVIDER_RETRY") and s["grace_until"]
    assert deliver(env.conn, sub_event("subscription.halted", t0 + 20))[1]["status"] == "PROCESSED"
    s2 = sub_row(env.conn)
    assert (s2["status"], s2["dunning_state"], s2["grace_until"]) == ("PAST_DUE", "HALTED", s["grace_until"])
    # the grace period runs out: ATIP's dunning expires the subscription and suspends the tenant
    env.conn.execute("UPDATE enterprise_subscription SET grace_until=? WHERE tenant_id='acme'",
                     (datetime.now() - timedelta(minutes=1),))
    env.conn.commit()
    assert payments.run_dunning(env.conn)["suspended"] == 1
    assert (sub_row(env.conn)["status"], tenants.get(env.conn, "acme")["status"]) == ("EXPIRED", "SUSPENDED")
    assert deliver(env.conn, sub_event("subscription.halted", t0 + 25))[1]["status"] == "IGNORED", "already ended"
    # the customer fixes the mandate and Razorpay charges: the payment lifts the billing suspension
    ev = with_payment(sub_event("subscription.charged", t0 + 30, t0 + 30, t0 + 31 * 86400), "pay_Q7u6SubPay2Dd",
                      "inv_Q7u6SubCyc2Ee")
    assert deliver(env.conn, ev)[1]["status"] == "PROCESSED"
    s = sub_row(env.conn)
    assert (s["status"], s["dunning_state"], tenants.get(env.conn, "acme")["status"]) == ("ACTIVE", None, "ACTIVE")
    # a suspension for another reason is an administrator's to lift, not a payment's
    tenants.set_status(env.conn, "acme", "SUSPENDED", reason="terms violation", actor="admin")
    env.conn.execute("UPDATE enterprise_subscription SET status='EXPIRED', dunning_state='EXPIRED' WHERE tenant_id='acme'")
    env.conn.commit()
    ev = with_payment(sub_event("subscription.charged", t0 + 40), "pay_Q7u6SubPay3Ff", "inv_Q7u6SubCyc3Gg")
    assert deliver(env.conn, ev)[1]["status"] == "PROCESSED"
    assert sub_row(env.conn)["status"] == "ACTIVE" and tenants.get(env.conn, "acme")["status"] == "SUSPENDED"


def test_an_unauthorised_link_is_replaced_but_a_live_mandate_is_never_doubled(env):
    from enterprise import billing
    from enterprise import razorpay as R
    start_autopay(env)                                                     # a PRO link, never authorised
    billing.set_price(env.conn, "ENTERPRISE", 4999.0, "owner")
    ent = "sub_Q7uEnterprise02"
    env.t.on("POST", "/plans", ok("plan", id="plan_Q7uEnterprise01")) \
        .on("POST", "/subscriptions", ok("subscription_created", id=ent))
    assert R.start_subscription(env.conn, "acme", "ENTERPRISE")["subscription_id"] == ent
    assert env.t.made("POST", "/plans")[-1]["body"]["item"]["amount"] == 499900
    assert deliver(env.conn, sub_event("subscription.activated", time.time(), sub_id=ent))[1]["status"] == "PROCESSED"
    assert sub_row(env.conn)["plan_id"] == "ENTERPRISE", "the plan comes from ATIP's record of the link, not notes"
    with pytest.raises(ValueError, match="cancel it first"):
        R.start_subscription(env.conn, "acme", "PRO")
    assert len(env.t.made("POST", "/subscriptions")) == 2


def test_a_stale_halt_does_not_undo_a_newer_charge(env):
    start_autopay(env)
    t0 = int(time.time())
    deliver(env.conn, sub_event("subscription.charged", t0, t0 - 60, t0 + 30 * 86400))
    assert deliver(env.conn, sub_event("subscription.halted", t0 - 100))[1]["status"] == "IGNORED"
    s = sub_row(env.conn)
    assert (s["status"], s["dunning_state"]) == ("ACTIVE", None)


def test_cancelled_and_completed_end_autopay(env):
    from enterprise import billing, tenants
    start_autopay(env)
    t0 = int(time.time())
    env.conn.execute("UPDATE enterprise_subscription SET status='TRIAL' WHERE tenant_id='acme'")
    env.conn.commit()
    assert deliver(env.conn, sub_event("subscription.activated", t0, t0 - 60, t0 + 40 * 86400))[1]["status"] == \
        "PROCESSED"
    s = sub_row(env.conn)
    assert (s["status"], s["payment_provider"]) == ("ACTIVE", "razorpay"), "TRIAL -> ACTIVE on activation"
    assert str(s["current_period_end"])[:10] == str(datetime.fromtimestamp(t0 + 40 * 86400).date())
    assert deliver(env.conn, sub_event("subscription.cancelled", t0 + 10))[1]["status"] == "PROCESSED"
    s = sub_row(env.conn)
    assert (s["status"], s["payment_provider"], s["dunning_state"]) == ("CANCELLED", None, None) and s["cancel_at"]
    assert billing.plan_limits(env.conn, "acme") == {}
    # a Razorpay subscription ATIP never stored (found by its notes) that completed its term
    tenants.create(env.conn, "beta", "Beta Capital")
    billing.subscribe(env.conn, "beta", "PRO", "ACTIVE")
    ev = sub_event("subscription.completed", t0 + 20, sub_id="sub_Q7uBeta0Done01", tenant="beta")
    assert deliver(env.conn, ev)[1]["status"] == "PROCESSED"
    b = sub_row(env.conn, "beta")
    assert (b["status"], b["dunning_state"], tenants.get(env.conn, "beta")["status"]) == ("EXPIRED", None, "ACTIVE")


def test_events_of_a_superseded_subscription_do_not_change_the_status(env):
    from enterprise import razorpay as R
    start_autopay(env)
    t0 = int(time.time())
    deliver(env.conn, sub_event("subscription.activated", t0, t0 - 60, t0 + 30 * 86400))
    env.t.on("POST", f"/subscriptions/{SUB}/cancel", ok("subscription_cancelled"))
    R.cancel_subscription(env.conn, "acme", at_cycle_end=False)
    new = "sub_Q7uNewOne00002"
    start_autopay(env, sub_id=new)
    deliver(env.conn, sub_event("subscription.activated", t0 + 20, t0 + 20, t0 + 31 * 86400, sub_id=new))
    assert (sub_row(env.conn)["status"], R.autopay_status(env.conn, "acme")["subscription_id"]) == ("ACTIVE", new)
    assert deliver(env.conn, sub_event("subscription.cancelled", t0 + 30))[1]["status"] == "IGNORED"
    assert deliver(env.conn, sub_event("subscription.halted", t0 + 40))[1]["status"] == "IGNORED"
    assert sub_row(env.conn)["status"] == "ACTIVE"


def test_unknown_and_foreign_events(env):
    from enterprise.razorpay import consume
    assert consume(env.conn, {"event": "order.paid", "payload": {}}) == "IGNORED"
    assert deliver(env.conn, event("payment.authorized"))[1]["status"] == "IGNORED"
    assert deliver(env.conn, event("payment.captured"))[1]["status"] == "IGNORED", "not one of ATIP's invoices"
    foreign = event("subscription.activated")
    foreign["payload"]["subscription"]["entity"].update({"id": "sub_Foreign000001", "notes": []})
    assert deliver(env.conn, foreign)[1]["status"] == "IGNORED"
    bad = with_payment(event("payment.captured", time.time() + 1), "pay_BadNotes00001", "inv_Unknown00001")
    bad["payload"]["payment"]["entity"]["notes"] = {"atip_invoice_id": "INVNOSUCH000"}
    code, out = deliver(env.conn, bad, event_id="EvtBadNotes01")
    assert (code, out["status"]) == (200, "FAILED")
    ghost = sub_event("subscription.activated", time.time() + 2, sub_id="sub_Ghost00000001", tenant="ghost")
    assert deliver(env.conn, ghost, event_id="EvtGhost00001")[1]["status"] == "FAILED"
    err = dict(env.conn.execute("SELECT event_id, error FROM ops_webhook_event WHERE event_id IN ('EvtBadNotes01', "
                                "'EvtGhost00001')").fetchall())
    assert "unknown ATIP invoice INVNOSUCH000" in err["EvtBadNotes01"] and "unknown tenant ghost" in err["EvtGhost00001"]
    assert not env.conn.execute("SELECT 1 FROM enterprise_billing_ref WHERE remote_id='sub_Ghost00000001'").fetchone()


# ── polling, HTTP, wiring ─────────────────────────────────────────────────────────────────────
def test_reconcile_settles_without_any_webhook(env):
    from enterprise import billing, payments, tenants
    iid = open_invoice(env)
    issue(env, iid)
    tenants.create(env.conn, "beta", "Beta Capital")
    billing.subscribe(env.conn, "beta", "PRO", "TRIAL")
    start_autopay(env, tenant="beta")
    end = int(time.time()) + 40 * 86400
    env.t.on("GET", f"/invoices/{RZP_INV}", ok("invoice_paid")) \
        .on("GET", f"/subscriptions/{SUB}", ok("subscription_active", current_end=end)) \
        .on("GET", "/invoices", collection(FX["api"]["subscription_invoice_paid"]))
    out = payments.reconcile(env.conn)
    assert out == {"mode": "test", "invoices_checked": 1, "invoices_settled": 1, "subscriptions_synced": 1, "errors": 0}
    assert env.t.made("GET", "/invoices")[-1]["query"] == {"subscription_id": SUB, "count": "100"}
    assert inv_row(env.conn, iid)["status"] == "PAID" and pays(env.conn, iid)[-1]["provider_payment_id"] == PAY
    b = sub_row(env.conn, "beta")
    assert (b["status"], b["payment_provider"]) == ("ACTIVE", "razorpay")
    assert str(b["current_period_end"])[:10] == str(datetime.fromtimestamp(end).date())
    assert [p["amount"] for p in paid_invoices(env.conn, "beta")] == [999.0]
    again = payments.reconcile(env.conn)
    assert (again["invoices_checked"], again["subscriptions_synced"]) == (0, 1)
    assert len(paid_invoices(env.conn, "beta")) == 1, "a second pass records nothing twice"


def test_webhook_route_end_to_end(env, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from dashboard import security, server
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "dash_config.json")
    iid = open_invoice(env)
    issue(env, iid)
    client = TestClient(server.app)
    body = json.dumps(event("payment.captured")).encode()
    h = {"Content-Type": "application/json", "X-Razorpay-Signature": sign(body), "X-Razorpay-Event-Id": "EvtHttp000001"}
    r = client.post("/api/webhooks/razorpay", content=body, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "PROCESSED", r.text
    assert client.post("/api/webhooks/razorpay", content=body, headers=h).json()["duplicate"] is True
    bad = client.post("/api/webhooks/razorpay", content=body, headers={**h, "X-Razorpay-Signature": "0" * 64,
                                                                       "X-Razorpay-Event-Id": "EvtHttp000002"})
    assert bad.status_code == 401
    assert inv_row(env.conn, iid)["status"] == "PAID"


def test_billing_routes_with_the_enterprise_layer_on(env, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from dashboard import security, server
    from enterprise import apikeys, users
    env.cfg.write_text(json.dumps({"enterprise": {"enabled": True}, "billing": {"provider": "razorpay"}}),
                       encoding="utf-8")
    monkeypatch.setattr(security, "CONFIG_PATH", env.cfg)
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    u = users.create_user(env.conn, "acme.admin", "Correct-Horse-Battery-9!", "acme", ["SUPER_ADMIN"],
                          email="billing@acme.example")
    h = {"Authorization": f"ApiKey {apikeys.create(env.conn, u['user_id'], 'acme', 'billing', ['admin:billing'])['api_key']}"}
    client = TestClient(server.app)
    env.t.on("POST", "/plans", ok("plan")).on("POST", "/subscriptions", ok("subscription_created"))
    r = client.post("/api/billing/autopay", json={"plan_id": "PRO"}, headers=h)
    assert r.status_code == 200 and r.json()["short_url"] == "https://rzp.io/i/AbC12dEf", r.text
    b = client.get("/api/billing", headers=h).json()
    assert (b["provider"], b["provider_state"], b["autopay"]["status"]) == ("razorpay", "TEST", "created")
    st = client.get("/api/admin/payments/provider", headers=h)
    assert st.json()["state"] == "TEST" and st.json()["key_id"] == "rzp_test_...G5ag" and KEY_SECRET not in st.text
    iid = open_invoice(env)
    env.t.on("GET", "/invoices", collection()).on("POST", "/customers", ok("customer")) \
        .on("POST", "/invoices", ok("invoice_issued"))
    r = client.post(f"/api/billing/invoices/{iid}/pay", json={}, headers=h)
    assert (r.json()["status"], r.json()["pay_url"]) == ("PENDING", "https://rzp.io/i/Xy7Zq1Pa"), r.text
    assert env.t.made("POST", "/customers")[0]["body"]["email"] == "billing@acme.example", "the tenant admin's e-mail"
    assert client.post("/api/admin/payments/reconcile", json={}, headers=h).status_code == 403, "platform admin only"
    env.secrets.clear()
    r = client.post("/api/billing/autopay/cancel", json={}, headers=h)
    assert r.status_code == 400 and "UNCONFIGURED" in r.json()["error"]


def test_tables_secrets_and_routes_are_wired(env):
    from enterprise.authz import _public, permission_for
    from enterprise.payments import payment_config
    from enterprise.privacy import classify
    from enterprise.scoping import TABLES
    from ops.secrets import CATALOG
    assert TABLES["enterprise_billing_ref"] == "DIRECT" and classify("enterprise_billing_ref")["class"] == "financial"
    names = ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "WEBHOOK_SECRET_RAZORPAY")
    assert all(n in CATALOG and CATALOG[n]["legacy"] is None for n in names), "never read from config.json"
    cols = {r[1] for r in env.conn.execute("PRAGMA table_info(enterprise_payment)")}
    assert {"provider_payment_id", "refunded_amount"} <= cols
    assert _public("POST", "/api/webhooks/razorpay")
    for m, p in (("POST", "/api/billing/autopay"), ("POST", "/api/billing/autopay/cancel"),
                 ("GET", "/api/admin/payments/provider"), ("POST", "/api/admin/payments/reconcile")):
        assert permission_for(m, p) == "admin:billing", (m, p)
    assert payment_config({"saas": {"payments": {"provider": "sandbox", "grace_days": 5,
                                                 "razorpay": {"period": "monthly"}}},
                           "billing": {"provider": "razorpay", "razorpay": {"allow_live": True}}}) == \
        {"provider": "razorpay", "grace_days": 5, "razorpay": {"period": "monthly", "allow_live": True}}
