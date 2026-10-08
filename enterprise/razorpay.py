"""
Razorpay payment gateway (W39b: ENT-04 gateway decision, API-04 provider payload mapping).

The owner delegated the gateway choice: Razorpay (UPI, cards, netbanking; a native Subscriptions
API; widely used by Indian SaaS). The integration is complete and testable without credentials and
INERT until the owner configures them -- see docs/BILLING_RAZORPAY.md.

CONFIG   atip_data/config.json: "saas": {"payments": {"provider": "razorpay"}} or the alias
         "billing": {"provider": "razorpay", "razorpay": {...}} (enterprise/payments.payment_config):
             allow_live             false    a live key is refused unless this is a real JSON true AND
                                             the environment is production
             period / interval      monthly / 1   the Razorpay plan made for an ATIP plan (autopay);
                                             monthly or yearly -- ATIP prices are per month
             total_count            120      billing cycles of an autopay subscription (Razorpay needs one)
             auth_link_days         7        an autopay link not authorised by then expires
             notify                 true     Razorpay e-mails / texts invoice links and reminders
             webhook_max_age_hours  72       older events are refused (Razorpay retries for 24 h)
SECRETS  ops/secrets.get -- environment, .env, atip_data/secrets/<NAME> or the encrypted vault
         (python -m ops vault-set NAME); never config.json:
             RAZORPAY_KEY_ID           rzp_test_... = TEST mode, rzp_live_... = LIVE mode
             RAZORPAY_KEY_SECRET
             WEBHOOK_SECRET_RAZORPAY   the secret typed into the Razorpay webhook (no WEBHOOK_SECRET_DEFAULT
                                       fallback)
STATE    RazorpayProvider().status()["state"]: UNCONFIGURED (no key id / secret), INVALID_KEY (neither
         prefix), LIVE_REFUSED, TEST, LIVE. Only TEST / LIVE make calls; otherwise ProviderNotEnabled, so
         payments.collect records the attempt REFUSED and the invoice stays OPEN -- nothing breaks.

REST v1  https://api.razorpay.com/v1, HTTP Basic key_id:key_secret, JSON, amounts in paise (INR only).
         Razorpay rejects unknown request fields, so only documented ones are sent. The transport is
         injectable (tests replay recorded responses; nothing here touches the network in tests):
             POST /customers {name, email, contact, fail_existing "0", notes}     one per tenant and mode
             POST /plans {period, interval, item {name, amount, currency, description}, notes}
             POST /subscriptions {plan_id, total_count, quantity, customer_notify, expire_by, notes}
             GET  /subscriptions/{id}      POST /subscriptions/{id}/cancel {cancel_at_cycle_end}
             POST /invoices {type, customer_id, line_items, currency, receipt, description, sms_notify,
                             email_notify, notes}      GET /invoices?receipt= | ?subscription_id=
             GET  /invoices/{id}           POST /invoices/{id}/notify_by/email
         Idempotency: receipt = ATIP's payment key <invoice_id>-<attempt>. Before creating, the local ref and
         GET /invoices?receipt= are checked, so a crash between Razorpay's reply and ATIP's commit never makes
         a second invoice; an ATIP invoice whose Razorpay invoice is still payable is never re-issued (a dunning
         retry returns the same link and sends a reminder). Customers are idempotent by fail_existing "0".

TWO MODES, never both for one tenant
    one-off   payments.run_cycle drafts / finalizes the invoice; collect() -> charge() issues a Razorpay invoice
              (pay link: UPI / card / netbanking) -> PENDING until it is paid
    autopay   start_subscription(): the customer authorises a mandate at the short_url (UPI Autopay, card,
              eMandate); on activation enterprise_subscription.payment_provider = 'razorpay', run_cycle skips the
              tenant, and every charge becomes a PAID ATIP invoice

WEBHOOK  POST /api/webhooks/razorpay -> ops/webhooks.verify_inbound -> verify_inbound() here
    X-Razorpay-Signature = hex HMAC-SHA256(WEBHOOK_SECRET_RAZORPAY, raw body), compared in constant time; a bad
    one is 401, recorded REJECTED in ops_webhook_event and logged. X-Razorpay-Event-Id is the idempotency key (a
    redelivery is 200 {"duplicate": true}); the same body under another id is a duplicate too (the id header is
    not signed). consume() -> PROCESSED / IGNORED (an exception = FAILED, with its writes rolled back):
        payment.captured         the ATIP invoice (via its Razorpay invoice) PAID, attempt SUCCEEDED, dunning cleared
        invoice.paid             the same; a subscription invoice becomes a PAID ATIP invoice for the cycle
        payment.failed           attempt FAILED -> PAST_DUE / RETRY_n, grace starts; IGNORED once the invoice is PAID
        subscription.activated   ACTIVE, payment_provider razorpay, period end, dunning cleared
        subscription.charged     a PAID ATIP invoice for the cycle (+ ACTIVE as above)
        subscription.pending     PAST_DUE / PROVIDER_RETRY (Razorpay is retrying the charge), grace starts
        subscription.halted      PAST_DUE / HALTED (Razorpay stopped retrying); when grace runs out
                                 payments.run_dunning expires the subscription and suspends the tenant
        subscription.cancelled   CANCELLED            subscription.completed   EXPIRED (term over, no suspension)
        refund.processed         payment REFUNDED / PARTIALLY_REFUNDED; invoice REFUNDED when fully refunded
        anything else            IGNORED
    Order-safe: a subscription status event older (payload created_at) than the newest one applied does not change
    the status (charged before activated is fine), period ends only move forward, and money events always apply,
    once per Razorpay payment / refund id. A payment received after the grace period lifts the billing suspension
    (only a billing one).
RECONCILE  reconcile(conn) (saas_daily; POST /api/admin/payments/reconcile) fetches the Razorpay invoices of OPEN
    ATIP invoices and the current autopay subscriptions (and their paid invoices) and applies them the same way:
    the path that settles payments while no webhook can reach ATIP (ENT-07 keeps it on 127.0.0.1).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import urlencode

from enterprise import audit
from enterprise.payments import ProviderError, ProviderNotEnabled

log = logging.getLogger("atip.enterprise.razorpay")

PROVIDER = SOURCE = "razorpay"
BASE_URL = "https://api.razorpay.com/v1"
TIMEOUT = 20
KEY_ID, KEY_SECRET, WEBHOOK_SECRET = "RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "WEBHOOK_SECRET_RAZORPAY"
DEFAULTS = {"allow_live": False, "period": "monthly", "interval": 1, "total_count": 120, "auth_link_days": 7,
            "notify": True, "webhook_max_age_hours": 72}
PERIOD_MONTHS = {"monthly": 1, "yearly": 12}     # ATIP prices are per month
MIN_PAISE = 100                                  # Razorpay's minimum charge, INR 1.00
PAYABLE = ("draft", "issued", "partially_paid")  # Razorpay invoice states that can still be paid
SUB_OPEN = ("created", "authenticated", "active", "pending", "halted")
BILLING_SUSPENSION = "unpaid invoice after the grace period"          # payments.run_dunning's reason


class RazorpayError(ProviderError):
    def __init__(self, status, code, description, field=None):
        self.status, self.code, self.description, self.field = status, code, description, field
        super().__init__(f"razorpay {code}" + (f" (HTTP {status})" if status else "") + f": {description}"
                         + (f" [field {field}]" if field else ""))


# ── configuration, credentials, money ────────────────────────────────────────────────────────
def settings(cfg: dict | None = None) -> dict:
    from enterprise.payments import payment_config
    raw = payment_config(cfg).get("razorpay") or {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in raw.items() if k in DEFAULTS})
    out["allow_live"] = raw.get("allow_live") is True              # only a real JSON true opens live mode
    out["notify"] = out["notify"] is not False
    return out


def _secret(name: str, log_access: bool = True):
    from ops.secrets import get
    return get(name, log_access=log_access)


def mode_of(key_id) -> str | None:
    k = str(key_id or "")
    return "test" if k.startswith("rzp_test_") else "live" if k.startswith("rzp_live_") else None


def _mode() -> str:
    return mode_of(_secret(KEY_ID, log_access=False)) or "test"


def to_paise(amount) -> int:
    """Rupees -> integer paise (half-up on the exact decimal value); Razorpay's minimum is 100."""
    if amount is None or isinstance(amount, bool):
        raise ValueError("an amount is required")
    try:
        d = Decimal(str(amount))
    except InvalidOperation:
        raise ValueError(f"bad amount {amount!r}") from None
    if not d.is_finite() or d < 0:
        raise ValueError(f"bad amount {amount!r}")
    p = int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if p < MIN_PAISE:
        raise ValueError(f"Razorpay's minimum charge is INR 1.00 (got {amount})")
    return p


def from_paise(p) -> float:
    return round(int(p or 0) / 100.0, 2)


def _currency(c) -> str:
    c = str(c or "INR").upper()
    if c != "INR":
        raise ValueError(f"currency {c}: ATIP bills through Razorpay in INR only")
    return c


def urllib_transport(method, url, headers, body, timeout):
    """The real transport: (status, body bytes). An HTTP error status is returned, not raised."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:      # noqa: S310 (fixed https:// base URL)
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ── gateway objects ATIP knows (enterprise_billing_ref, db/schema_billing.py) ──────────────────
def _now():
    return datetime.now()


def _ts(v):
    try:
        return datetime.fromtimestamp(int(v)) if v else None
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _row(r):
    if not r:
        return None
    d = dict(r)
    d["data"] = json.loads(d.pop("data_json") or "{}")
    return d


def _ref(conn, remote_id):
    if not remote_id:
        return None
    return _row(conn.execute("SELECT * FROM enterprise_billing_ref WHERE provider=? AND remote_id=?",
                             (PROVIDER, str(remote_id))).fetchone())


def _latest(conn, kind, local_id, mode=None):
    q, a = "SELECT * FROM enterprise_billing_ref WHERE provider=? AND kind=? AND local_id=?", [PROVIDER, kind,
                                                                                           str(local_id)]
    if mode:
        q += " AND mode=?"
        a.append(mode)
    return _row(conn.execute(q + " ORDER BY created_at DESC LIMIT 1", a).fetchone())


def _put(conn, remote_id, kind, mode, local_id, tenant_id, status=None, url=None, data=None, created_at=None):
    now = _now()
    conn.execute("INSERT INTO enterprise_billing_ref (provider,remote_id,kind,mode,local_id,tenant_id,status,url,"
                 "event_at,data_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(provider, "
                 "remote_id) DO UPDATE SET status=COALESCE(excluded.status, enterprise_billing_ref.status), "
                 "url=COALESCE(excluded.url, enterprise_billing_ref.url), updated_at=excluded.updated_at",
                 (PROVIDER, str(remote_id), kind, mode, str(local_id), tenant_id, status, url, 0,
                  json.dumps(data or {}), created_at or now, now))
    return _ref(conn, remote_id)


def _touch(conn, remote_id, status=None, event_at=0):
    conn.execute("UPDATE enterprise_billing_ref SET status=COALESCE(?, status), "
                 "event_at=MAX(COALESCE(event_at, 0), ?), updated_at=? WHERE provider=? AND remote_id=?",
                 (status, int(event_at or 0), _now(), PROVIDER, str(remote_id)))


def _default_tenant():
    from enterprise.config import settings as ent
    return ent()["default_tenant"]


def _customer_fields(conn, tenant_id) -> dict:
    from enterprise.tenants import get
    t = get(conn, tenant_id)
    if not t:
        raise ValueError(f"no tenant {tenant_id}")
    st = t.get("settings") or {}

    def clean(s):                                # Razorpay: 3-50 chars of letters, digits, space . ' / @ ( )
        return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 .'/@()]", " ", str(s or ""))).strip()[:50]
    name = clean(t.get("name"))
    if len(name) < 3:
        name = clean(f"ATIP {tenant_id}")
    email = st.get("billing_email")
    if not email:
        r = conn.execute("SELECT u.email FROM enterprise_user u JOIN enterprise_user_role r ON r.user_id=u.user_id "
                         "WHERE r.tenant_id=? AND r.role='SUPER_ADMIN' AND u.status='ACTIVE' AND u.email IS NOT NULL "
                         "AND u.email<>'' ORDER BY u.created_at LIMIT 1", (tenant_id,)).fetchone()
        email = r[0] if r else None
    out = {"name": name}
    if email:
        out["email"] = str(email)[:64]
    if st.get("billing_contact"):
        out["contact"] = str(st["billing_contact"])[:15]
    return out


# ── the provider ─────────────────────────────────────────────────────────────────────────────
class RazorpayProvider:
    """payments.provider() for "razorpay". charge() is the payments.collect interface; the other
    methods are the Razorpay operations ATIP uses. Every call needs state TEST or LIVE."""
    name = PROVIDER

    def __init__(self, key_id=None, key_secret=None, transport=None, cfg=None, environment=None):
        self.key_id = key_id if key_id is not None else _secret(KEY_ID, log_access=False)
        self._key_secret = key_secret             # None: read (and access-logged) from ops/secrets per call
        self.transport = transport or urllib_transport
        self.cfg, self._env = cfg, environment
        self.mode = mode_of(self.key_id)
        self.last = {}                            # the last charge's payment id / pay link, read by collect()

    # -- state ------------------------------------------------------------------------------
    def status(self) -> dict:
        from ops.config import environment
        s = settings(self.cfg)
        env = self._env or environment()
        k = str(self.key_id or "")
        out = {"provider": PROVIDER, "mode": self.mode, "key_id": k[:9] + "..." + k[-4:] if len(k) > 13 else None,
               "allow_live": s["allow_live"], "environment": env,
               "webhook_configured": bool(_secret(WEBHOOK_SECRET, log_access=False))}
        if not k or not (self._key_secret or _secret(KEY_SECRET, log_access=False)):
            state, why = "UNCONFIGURED", (f"{KEY_ID} / {KEY_SECRET} are not set (owner: python -m ops vault-set "
                                          f"{KEY_ID}, then {KEY_SECRET})")
        elif self.mode is None:
            state, why = "INVALID_KEY", f"{KEY_ID} must start with rzp_test_ or rzp_live_"
        elif self.mode == "live" and not (s["allow_live"] and env == "production"):
            state, why = "LIVE_REFUSED", (f"a live key needs billing.razorpay.allow_live true in production "
                                          f"(allow_live {str(s['allow_live']).lower()}, environment {env})")
        else:
            state, why = self.mode.upper(), ("test mode: no real money moves" if self.mode == "test"
                                             else "LIVE: real charges")
        return {**out, "state": state, "reason": why}

    def _require(self):
        st = self.status()
        if st["state"] not in ("TEST", "LIVE"):
            raise ProviderNotEnabled(f"razorpay is {st['state']}: {st['reason']}")

    # -- REST -------------------------------------------------------------------------------
    def _auth(self) -> str:
        secret = self._key_secret if self._key_secret is not None else _secret(KEY_SECRET)
        return "Basic " + base64.b64encode(f"{self.key_id}:{secret}".encode()).decode()

    def request(self, method, path, body=None, params=None) -> dict:
        self._require()
        from ops.errors import DependencyUnavailable
        from ops.resilience import breaker
        url = BASE_URL + path + ("?" + urlencode(params) if params else "")
        data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        headers = {"Authorization": self._auth(), "Accept": "application/json", "User-Agent": "ATIP-Billing/1"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            code, raw = breaker("razorpay").call(self.transport, method, url, headers, data, TIMEOUT)
        except DependencyUnavailable as e:
            raise RazorpayError(None, "UNAVAILABLE", str(e)) from None
        except Exception as e:                    # network / TLS / timeout (the message never holds the key)
            raise RazorpayError(None, "NETWORK", f"{type(e).__name__}: {str(e)[:200]}") from None
        try:
            js = json.loads(raw or b"{}")
        except Exception:
            js = None
        if not 200 <= int(code) < 300:
            er = (js.get("error") or {}) if isinstance(js, dict) else {}
            raise RazorpayError(code, er.get("code") or f"HTTP_{code}", er.get("description") or "request failed",
                                er.get("field"))
        if not isinstance(js, dict):
            raise RazorpayError(code, "BAD_RESPONSE", "the response is not a JSON object")
        return js

    # -- customers / plans / subscriptions ----------------------------------------------------
    def create_customer(self, conn, tenant_id) -> dict:
        self._require()
        ref = _latest(conn, "customer", tenant_id, self.mode)
        if ref:
            return {"id": ref["remote_id"], "existing": True}
        body = {**_customer_fields(conn, tenant_id), "fail_existing": "0", "notes": {"atip_tenant_id": tenant_id}}
        c = self.request("POST", "/customers", body)
        _put(conn, c["id"], "customer", self.mode, tenant_id, tenant_id, status="active")
        conn.commit()
        return c

    def ensure_plan(self, conn, plan_id) -> dict:
        """The Razorpay plan for an ATIP plan at its current price (a new price makes a new plan)."""
        self._require()
        from enterprise.billing import plans
        p = plans(conn, plan_id)
        if not p:
            raise ValueError(f"no plan {plan_id}")
        p = p[0]
        if p.get("price_month") is None or p["price_month"] <= 0:
            raise ValueError(f"plan {plan_id} has no price (PUT /api/admin/plans/{plan_id}); autopay needs one")
        s = settings(self.cfg)
        if s["period"] not in PERIOD_MONTHS:
            raise ValueError(f"billing.razorpay.period must be one of {sorted(PERIOD_MONTHS)}")
        interval = int(s["interval"])
        if interval < 1:
            raise ValueError("billing.razorpay.interval must be >= 1")
        months = PERIOD_MONTHS[s["period"]] * interval
        paise, cur = to_paise(Decimal(str(p["price_month"])) * months), _currency(p.get("currency"))
        key = f"{plan_id}|{paise}|{cur}|{s['period']}|{interval}"
        ref = _latest(conn, "plan", key, self.mode)
        if ref:
            return {"id": ref["remote_id"], "existing": True, "key": key}
        body = {"period": s["period"], "interval": interval,
                "item": {"name": f"ATIP {p.get('name') or plan_id}"[:60], "amount": paise, "currency": cur,
                         "description": f"ATIP {plan_id} plan, {months} month(s) per cycle"},
                "notes": {"atip_plan_id": plan_id, "atip_price_key": key}}
        r = self.request("POST", "/plans", body)
        _put(conn, r["id"], "plan", self.mode, key, _default_tenant(), status="active",
             data={"plan_id": plan_id, "amount": paise})
        conn.commit()
        return r

    def create_subscription(self, conn, tenant_id, plan_id, actor="system") -> dict:
        self._require()
        cur = _latest(conn, "subscription", tenant_id, self.mode)
        if cur and cur["status"] in SUB_OPEN:
            if cur["data"].get("plan_id") == plan_id:
                return {"subscription_id": cur["remote_id"], "status": cur["status"], "short_url": cur["url"],
                        "plan_id": plan_id, "existing": True}
            if cur["status"] != "created":        # a live mandate: a second one would charge twice
                raise ValueError(f"autopay is {cur['status']} on plan {cur['data'].get('plan_id')}: cancel it "
                                 "first (POST /api/billing/autopay/cancel)")
        # an older link for another plan that was never authorised simply expires (expire_by)
        rp = self.ensure_plan(conn, plan_id)
        s = settings(self.cfg)
        body = {"plan_id": rp["id"], "total_count": int(s["total_count"]), "quantity": 1,
                "customer_notify": 1 if s["notify"] else 0,
                "expire_by": int(time.time()) + int(s["auth_link_days"]) * 86400,
                "notes": {"atip_tenant_id": tenant_id, "atip_plan_id": plan_id}}
        r = self.request("POST", "/subscriptions", body)
        _put(conn, r["id"], "subscription", self.mode, tenant_id, tenant_id, status=r.get("status"),
             url=r.get("short_url"), data={"plan_id": plan_id, "razorpay_plan_id": rp["id"]})
        audit.record(conn, "billing.autopay_started", tenant_id=tenant_id, actor=actor, resource=r["id"],
                     details={"plan": plan_id, "mode": self.mode}, commit=False)
        conn.commit()
        return {"subscription_id": r["id"], "status": r.get("status"), "short_url": r.get("short_url"),
                "plan_id": plan_id, "existing": False}

    def fetch_subscription(self, remote_id) -> dict:
        return self.request("GET", f"/subscriptions/{remote_id}")

    def cancel_subscription(self, conn, tenant_id, at_cycle_end=True, actor="system") -> dict:
        self._require()
        cur = _latest(conn, "subscription", tenant_id, self.mode)
        if not cur or cur["status"] not in SUB_OPEN:
            raise ValueError("no Razorpay autopay subscription to cancel")
        r = self.request("POST", f"/subscriptions/{cur['remote_id']}/cancel",
                         {"cancel_at_cycle_end": 1 if at_cycle_end else 0})
        if r.get("status") == "cancelled":
            _apply_status(conn, cur, r, "cancelled", int(time.time()))
        else:
            _touch(conn, cur["remote_id"], status=r.get("status"))
            end = _ts(r.get("current_end"))
            if end:
                conn.execute("UPDATE enterprise_subscription SET cancel_at=?, updated_at=? WHERE tenant_id=?",
                             (end, _now(), tenant_id))
        audit.record(conn, "billing.autopay_cancelled", tenant_id=tenant_id, actor=actor, resource=cur["remote_id"],
                     details={"at_cycle_end": bool(at_cycle_end), "razorpay_status": r.get("status")}, commit=False)
        conn.commit()
        return {"subscription_id": cur["remote_id"], "status": r.get("status"), "at_cycle_end": bool(at_cycle_end)}

    # -- one-off invoices -----------------------------------------------------------------------
    def fetch_invoice(self, remote_id) -> dict:
        return self.request("GET", f"/invoices/{remote_id}")

    def create_invoice(self, conn, tenant_id, invoice_id, amount, currency, receipt) -> dict:
        self._require()
        paise, cur = to_paise(amount), _currency(currency)
        receipt = re.sub(r"[^A-Za-z0-9_-]", "-", str(receipt))[:40]
        found = [i for i in (self.request("GET", "/invoices", params={"receipt": receipt}).get("items") or [])
                 if i.get("status") in PAYABLE + ("paid",)]
        if found:                                 # created before a crash: adopt it, never issue twice
            inv = found[0]
        else:
            row = conn.execute("SELECT plan_id, period_start, period_end FROM enterprise_invoice WHERE invoice_id=?",
                               (invoice_id,)).fetchone()
            plan, ps, pe = (row[0], row[1], row[2]) if row else (None, None, None)
            n = 1 if settings(self.cfg)["notify"] else 0
            body = {"type": "invoice", "customer_id": self.create_customer(conn, tenant_id)["id"], "currency": cur,
                    "receipt": receipt, "description": f"ATIP invoice {invoice_id}",
                    "line_items": [{"name": f"ATIP {plan or 'subscription'} plan",
                                    "description": f"{ps} to {pe}" if ps and pe else str(invoice_id),
                                    "amount": paise, "currency": cur, "quantity": 1}],
                    "sms_notify": n, "email_notify": n,
                    "notes": {"atip_invoice_id": invoice_id, "atip_tenant_id": tenant_id}}
            inv = self.request("POST", "/invoices", body)
        _put(conn, inv["id"], "invoice", self.mode, invoice_id, tenant_id, status=inv.get("status"),
             url=inv.get("short_url"), data={"receipt": receipt, "amount": paise})
        conn.commit()
        return inv

    def _remind(self, remote_id):
        if not settings(self.cfg)["notify"]:
            return
        try:
            self.request("POST", f"/invoices/{remote_id}/notify_by/email")
        except RazorpayError as e:                # a reminder is best effort
            log.info(f"razorpay reminder for {remote_id} not sent: {e}")

    def charge(self, conn, tenant_id, amount, currency, reference, invoice_id=None) -> tuple:
        """payments.collect: (status, provider_ref, error). PENDING = the customer has a pay link."""
        self._require()
        self.last = {}
        invoice_id = invoice_id or str(reference).rsplit(":", 1)[0]
        ref = _latest(conn, "invoice", invoice_id, self.mode)
        if ref:
            inv = self.fetch_invoice(ref["remote_id"])
            _touch(conn, ref["remote_id"], status=inv.get("status"))
            if inv.get("status") == "paid":
                _supersede_pending(conn, invoice_id)
                conn.commit()
                self.last = {"payment_id": inv.get("payment_id"), "url": inv.get("short_url")}
                return "SUCCEEDED", inv["id"], None
            if inv.get("status") in PAYABLE:      # still payable: the same link again, plus a reminder
                _supersede_pending(conn, invoice_id)
                conn.commit()
                self._remind(inv["id"])
                self.last = {"url": inv.get("short_url")}
                return "PENDING", inv["id"], None
            conn.commit()                         # cancelled / expired: issue a new one
        inv = self.create_invoice(conn, tenant_id, invoice_id, amount, currency, reference)
        self.last = {"url": inv.get("short_url")}
        if inv.get("status") == "paid":
            self.last["payment_id"] = inv.get("payment_id")
            return "SUCCEEDED", inv["id"], None
        return "PENDING", inv["id"], None


def start_subscription(conn, tenant_id, plan_id=None, actor="system", prov=None) -> dict:
    """POST /api/billing/autopay: a Razorpay subscription link for the tenant's (or the given) plan."""
    from enterprise import billing
    from enterprise.payments import settings as pay_settings
    if pay_settings()["provider"] != PROVIDER:
        raise ValueError("autopay needs the razorpay payment provider (config billing.provider)")
    plan_id = plan_id or (billing.subscription(conn, tenant_id) or {}).get("plan_id")
    if not plan_id:
        raise ValueError("plan_id is required")
    return (prov or RazorpayProvider()).create_subscription(conn, tenant_id, plan_id, actor=actor)


def cancel_subscription(conn, tenant_id, at_cycle_end=True, actor="system", prov=None) -> dict:
    return (prov or RazorpayProvider()).cancel_subscription(conn, tenant_id, at_cycle_end, actor)


def autopay_status(conn, tenant_id) -> dict | None:
    """The tenant's current Razorpay subscription as ATIP last saw it (no network call)."""
    cur = _latest(conn, "subscription", tenant_id)
    if not cur:
        return None
    return {"subscription_id": cur["remote_id"], "status": cur["status"], "mode": cur["mode"],
            "short_url": cur["url"] if cur["status"] in ("created", "authenticated") else None,
            "plan_id": cur["data"].get("plan_id"), "updated_at": cur["updated_at"]}


# ── webhook ──────────────────────────────────────────────────────────────────────────────────
def verify_signature(secret: str, body: bytes, signature) -> bool:
    """X-Razorpay-Signature: hex HMAC-SHA256 of the raw body with the webhook secret (constant time)."""
    if not secret or not signature or not isinstance(signature, str):
        return False
    want = hmac.new(secret.encode(), body or b"", hashlib.sha256).hexdigest()
    try:
        return hmac.compare_digest(want, signature.strip())
    except TypeError:                             # a non-ASCII header value
        return False


def verify_inbound(conn, headers: dict, body: bytes) -> tuple:
    """(http_status, payload) for POST /api/webhooks/razorpay; records the event when accepted."""
    from ops.webhooks import MAX_BODY, _record
    h = {str(k).lower(): v for k, v in (headers or {}).items()}
    secret = _secret(WEBHOOK_SECRET)
    if not secret:
        return 503, {"error": {"code": "DEPENDENCY_UNAVAILABLE", "message": "webhook source razorpay not configured"}}
    if len(body) > MAX_BODY:
        return 413, {"error": {"code": "PAYLOAD_TOO_LARGE", "message": "webhook body too large"}}
    sig, eid = h.get("x-razorpay-signature"), h.get("x-razorpay-event-id")
    if not sig:
        return 400, {"error": {"code": "VALIDATION_FAILED", "message": "missing X-Razorpay-Signature"}}
    if eid and len(str(eid)) > 128:
        return 400, {"error": {"code": "VALIDATION_FAILED", "message": "bad X-Razorpay-Event-Id"}}
    if not verify_signature(secret, body, sig):
        _record(conn, SOURCE, eid, False, "REJECTED", body, None, "bad signature")
        log.warning(f"razorpay webhook REJECTED: invalid X-Razorpay-Signature (event id {str(eid)[:64]!r}, "
                    f"{len(body)} bytes)")
        return 401, {"error": {"code": "UNAUTHENTICATED", "message": "invalid signature"}}
    try:
        payload = json.loads(body or b"{}")
    except Exception:
        return 400, {"error": {"code": "VALIDATION_FAILED", "message": "body is not JSON"}}
    if not isinstance(payload, dict) or not payload.get("event"):
        return 400, {"error": {"code": "VALIDATION_FAILED", "message": "not a Razorpay event"}}
    digest = hashlib.sha256(body).hexdigest()
    eid = str(eid) if eid else "sha256:" + digest[:40]
    etype = str(payload["event"])[:64]
    at = payload.get("created_at")
    if isinstance(at, (int, float)) and not isinstance(at, bool) and \
            time.time() - at > float(settings()["webhook_max_age_hours"]) * 3600:
        _record(conn, SOURCE, eid, False, "REJECTED", body, etype, "outside the replay window")
        log.warning(f"razorpay webhook REJECTED: event {eid!r} ({etype}) is older than the replay window")
        return 401, {"error": {"code": "UNAUTHENTICATED", "message": "event outside the replay window"}}
    # a redelivery (same id) or the same signed body replayed under a new, unsigned id: processed once
    if conn.execute("SELECT 1 FROM ops_webhook_event WHERE source=? AND (event_id=? OR (payload_sha256=? AND "
                    "signature_ok=1))", (SOURCE, eid, digest)).fetchone():
        return 200, {"received": True, "duplicate": True, "event_id": eid}
    _record(conn, SOURCE, eid, True, "RECEIVED", body, etype, None)
    return 200, {"received": True, "duplicate": False, "event_id": eid}


def consume(conn, event: dict) -> str:
    """enterprise/w32.consume for source razorpay: PROCESSED / IGNORED; raises -> FAILED (rolled back)."""
    fn = HANDLERS.get(str((event or {}).get("event") or ""))
    if fn is None:
        return "IGNORED"
    ents = {k: (v.get("entity") or {}) for k, v in (event.get("payload") or {}).items() if isinstance(v, dict)}
    at = event.get("created_at")
    at = int(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else int(time.time())
    try:
        return fn(conn, ents, at)
    except Exception:
        conn.rollback()
        raise


# ── mapping onto ATIP invoices / payments / subscriptions ────────────────────────────────────
def _notes(e) -> dict:
    n = (e or {}).get("notes")
    return n if isinstance(n, dict) else {}       # Razorpay sends [] for "no notes"


def _tell(conn, tenant_id, title, body, severity="info"):
    from enterprise.payments import _tell as tell
    tell(conn, tenant_id, title, body, severity)


def _invoice_row(conn, invoice_id):
    r = conn.execute("SELECT * FROM enterprise_invoice WHERE invoice_id=?", (invoice_id,)).fetchone()
    return dict(r) if r else None


def _payment_by_remote(conn, pay_id):
    if not pay_id:
        return None
    r = conn.execute("SELECT * FROM enterprise_payment WHERE provider_payment_id=? ORDER BY created_at DESC LIMIT 1",
                     (str(pay_id),)).fetchone()
    return dict(r) if r else None


def _insert_payment(conn, tenant_id, invoice_id, amount, currency, status, ref, pay_id, error=None) -> str:
    pid, now = "pay_" + uuid.uuid4().hex[:16], _now()
    conn.execute("INSERT INTO enterprise_payment (payment_id,tenant_id,invoice_id,provider,amount,currency,status,"
                 "provider_ref,idempotency_key,error,created_at,updated_at,provider_payment_id) VALUES "
                 "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (pid, tenant_id, invoice_id, PROVIDER, amount, currency, status, ref,
                  f"razorpay:{pay_id or uuid.uuid4().hex}", error, now, now, pay_id))
    return pid


def _latest_pending(conn, invoice_id):
    r = conn.execute("SELECT payment_id FROM enterprise_payment WHERE invoice_id=? AND provider=? AND status='PENDING' "
                     "ORDER BY created_at DESC LIMIT 1", (invoice_id, PROVIDER)).fetchone()
    return r[0] if r else None


def _supersede_pending(conn, invoice_id, keep=None):
    """At most one PENDING Razorpay attempt per invoice: older ones (the same link) become SUPERSEDED."""
    conn.execute("UPDATE enterprise_payment SET status='SUPERSEDED', updated_at=? WHERE invoice_id=? AND provider=? "
                 "AND status='PENDING' AND payment_id<>?", (_now(), invoice_id, PROVIDER, keep or ""))


def _atip_invoice(conn, pay=None, inv=None):
    """The ATIP invoice a Razorpay payment / invoice is for, or None (not ATIP's one-off invoice)."""
    for rid in ((inv or {}).get("id"), (pay or {}).get("invoice_id")):
        ref = _ref(conn, rid) if rid else None
        if ref and ref["kind"] == "invoice":
            return ref["local_id"]
    for e in (inv, pay):
        n = _notes(e).get("atip_invoice_id")
        if n:
            if not _invoice_row(conn, n):
                raise ValueError(f"unknown ATIP invoice {n}")
            return n
    return None


def _lift_billing_suspension(conn, tenant_id) -> bool:
    """A tenant suspended by dunning (and nothing else) is reactivated once it pays."""
    from enterprise.tenants import get, set_status
    t = get(conn, tenant_id)
    if not t or t["status"] != "SUSPENDED":
        return False
    r = conn.execute("SELECT details_json FROM enterprise_audit WHERE action='tenant.status' AND tenant_id=? "
                     "ORDER BY id DESC LIMIT 1", (tenant_id,)).fetchone()
    try:
        reason = json.loads(r[0] or "{}").get("reason") if r else None
    except Exception:
        reason = None
    if reason != BILLING_SUSPENSION:              # suspended for another reason: an administrator decides
        return False
    set_status(conn, tenant_id, "ACTIVE", reason="paid after the grace period (razorpay)", actor="billing")
    return True


def _clear_dunning(conn, tenant_id):
    r = conn.execute("SELECT status, dunning_state FROM enterprise_subscription WHERE tenant_id=?",
                     (tenant_id,)).fetchone()
    expired_unpaid = bool(r and r[0] == "EXPIRED" and r[1] == "EXPIRED")
    conn.execute("UPDATE enterprise_subscription SET status='ACTIVE', dunning_state=NULL, grace_until=NULL, "
                 "updated_at=? WHERE tenant_id=? AND (status IN ('PAST_DUE','ACTIVE','TRIAL') OR (status='EXPIRED' AND "
                 "dunning_state='EXPIRED'))", (_now(), tenant_id))
    if expired_unpaid:
        _lift_billing_suspension(conn, tenant_id)


def _settle(conn, invoice_id, pay, at, actor="webhook:razorpay") -> str:
    """A captured Razorpay payment for an ATIP invoice: the attempt SUCCEEDED, the invoice PAID."""
    inv = _invoice_row(conn, invoice_id)
    if not inv:
        raise ValueError(f"no invoice {invoice_id}")
    pay_id = pay.get("id")
    seen = _payment_by_remote(conn, pay_id)
    if seen and seen["status"] in ("SUCCEEDED", "REFUNDED", "PARTIALLY_REFUNDED", "SUPERSEDED"):
        return "IGNORED"                          # this Razorpay payment is already applied
    amount = from_paise(pay["amount"]) if pay.get("amount") is not None else inv["amount"]
    cur = str(pay.get("currency") or inv.get("currency") or "INR")
    now = _now()
    ap = _latest_pending(conn, invoice_id)
    if ap:
        conn.execute("UPDATE enterprise_payment SET status='SUCCEEDED', provider_payment_id=?, amount=?, error=NULL, "
                     "updated_at=? WHERE payment_id=?", (pay_id, amount, now, ap))
        _supersede_pending(conn, invoice_id, keep=ap)
    else:
        ap = _insert_payment(conn, inv["tenant_id"], invoice_id, amount, cur, "SUCCEEDED",
                             pay.get("invoice_id") or pay.get("order_id"), pay_id)
    already = inv["status"] in ("PAID", "REFUNDED")
    if not already:
        conn.execute("UPDATE enterprise_invoice SET status='PAID', paid_at=?, payment_id=?, finalized_at=COALESCE("
                     "finalized_at, ?) WHERE invoice_id=?", (now, ap, now, invoice_id))
        _clear_dunning(conn, inv["tenant_id"])
    audit.record(conn, "billing.overpayment" if already else "billing.webhook", tenant_id=inv["tenant_id"],
                 actor=actor, resource=invoice_id, details={"payment_id": ap, "razorpay_payment_id": pay_id,
                                                            "amount": amount}, commit=False)
    conn.commit()
    _tell(conn, inv["tenant_id"], f"Payment succeeded for invoice {invoice_id}", f"{cur} {amount:,.2f} (Razorpay)")
    return "PROCESSED"


def _on_payment_captured(conn, e, at) -> str:
    pay = e.get("payment") or {}
    iid = _atip_invoice(conn, pay=pay)
    if not iid:
        return "IGNORED"           # an autopay charge is applied from subscription.charged / invoice.paid
    return _settle(conn, iid, pay, at)


def _on_payment_failed(conn, e, at) -> str:
    pay = e.get("payment") or {}
    iid = _atip_invoice(conn, pay=pay)
    if not iid:
        return "IGNORED"           # an autopay charge failure arrives as subscription.pending / halted
    inv = _invoice_row(conn, iid)
    if inv["status"] != "OPEN":
        return "IGNORED"           # paid meanwhile (a late / out-of-order failure) or not collectable: no dunning
    pay_id = pay.get("id")
    if _payment_by_remote(conn, pay_id):
        return "IGNORED"
    reason = str(pay.get("error_description") or pay.get("error_reason") or pay.get("error_code")
                 or "payment failed")[:300]
    now = _now()
    ap = _latest_pending(conn, iid)
    if ap:
        conn.execute("UPDATE enterprise_payment SET status='FAILED', provider_payment_id=?, error=?, updated_at=? "
                     "WHERE payment_id=?", (pay_id, reason, now, ap))
    else:
        ap = _insert_payment(conn, inv["tenant_id"], iid, from_paise(pay["amount"]) if pay.get("amount") is not None
                             else inv["amount"], str(pay.get("currency") or inv["currency"] or "INR"), "FAILED",
                             pay.get("invoice_id"), pay_id, error=reason)
    n = conn.execute("SELECT COUNT(*) FROM enterprise_payment WHERE invoice_id=? AND status='FAILED'",
                     (iid,)).fetchone()[0]
    from enterprise.payments import _enter_dunning
    _enter_dunning(conn, inv["tenant_id"], n)
    audit.record(conn, "billing.webhook", tenant_id=inv["tenant_id"], actor="webhook:razorpay", resource=iid,
                 details={"payment_id": ap, "razorpay_payment_id": pay_id, "type": "payment.failed"}, commit=False)
    conn.commit()
    _tell(conn, inv["tenant_id"], f"Payment failed for invoice {iid}", reason, "warning")
    return "PROCESSED"


def _sub_ref(conn, sub):
    """The ref of a Razorpay subscription -- made from its notes if ATIP has not seen it; None: not ATIP's."""
    sid = (sub or {}).get("id")
    ref = _ref(conn, sid) if sid else None
    if ref:
        return ref
    tid = _notes(sub).get("atip_tenant_id")
    if not sid or not tid:
        return None
    from enterprise.tenants import get
    if not get(conn, tid):
        raise ValueError(f"unknown tenant {tid}")
    return _put(conn, sid, "subscription", _mode(), tid, tid, status=sub.get("status"), url=sub.get("short_url"),
                data={"plan_id": _notes(sub).get("atip_plan_id")}, created_at=_ts(sub.get("created_at")))


def _is_current(conn, ref) -> bool:
    cur = _latest(conn, "subscription", ref["tenant_id"])
    return bool(cur and cur["remote_id"] == ref["remote_id"])


def _push_period_end(conn, tenant_id, end):
    if not end:
        return
    r = conn.execute("SELECT current_period_end FROM enterprise_subscription WHERE tenant_id=?",
                     (tenant_id,)).fetchone()
    cur = r[0] if r else None
    if cur is not None and not isinstance(cur, datetime):
        try:
            cur = datetime.fromisoformat(str(cur)[:19].replace(" ", "T"))
        except ValueError:
            cur = None
    if cur is None or end > cur:                  # period ends only move forward
        conn.execute("UPDATE enterprise_subscription SET current_period_end=? WHERE tenant_id=?", (end, tenant_id))


def _apply_status(conn, ref, sub, state, at) -> bool:
    """A Razorpay subscription state onto the tenant's ATIP subscription. False: stale (an older event than
    one already applied), not the tenant's current subscription, or nothing to change."""
    if int(at) < int(ref.get("event_at") or 0):
        return False
    _touch(conn, ref["remote_id"], status=sub.get("status") or state, event_at=at)
    if not _is_current(conn, ref):
        return False
    from enterprise import billing
    from enterprise.payments import settings as pay_settings
    tid, now = ref["tenant_id"], _now()
    row = billing.subscription(conn, tid)
    if state == "active":
        plan = ref["data"].get("plan_id") or _notes(sub).get("atip_plan_id")
        plan = plan if plan and billing.plans(conn, plan) else None
        if not row:
            if not plan:
                raise ValueError(f"tenant {tid} has no subscription and the Razorpay one names no ATIP plan")
            billing.subscribe(conn, tid, plan, "ACTIVE", actor="webhook:razorpay")
            row = billing.subscription(conn, tid)
        expired_unpaid = row["status"] == "EXPIRED" and row.get("dunning_state") == "EXPIRED"
        conn.execute("UPDATE enterprise_subscription SET status='ACTIVE', plan_id=COALESCE(?, plan_id), "
                     "dunning_state=NULL, grace_until=NULL, payment_provider=?, cancel_at=NULL, updated_at=? "
                     "WHERE tenant_id=?", (plan, PROVIDER, now, tid))
        _push_period_end(conn, tid, _ts(sub.get("current_end")))
        if expired_unpaid:
            _lift_billing_suspension(conn, tid)
    elif state in ("pending", "halted"):
        if not row or row["status"] not in ("ACTIVE", "TRIAL", "PAST_DUE"):
            return False                          # ATIP already ended it (grace expired / cancelled)
        grace = row.get("grace_until") or now + timedelta(days=pay_settings()["grace_days"])
        conn.execute("UPDATE enterprise_subscription SET status='PAST_DUE', dunning_state=?, grace_until=?, "
                     "updated_at=? WHERE tenant_id=?", ("HALTED" if state == "halted" else "PROVIDER_RETRY", grace,
                                                        now, tid))
    elif state == "cancelled":
        if not row:
            return False
        conn.execute("UPDATE enterprise_subscription SET status='CANCELLED', dunning_state=NULL, grace_until=NULL, "
                     "payment_provider=NULL, cancel_at=?, updated_at=? WHERE tenant_id=?",
                     (_ts(sub.get("ended_at")) or now, now, tid))
    elif state == "completed":
        if not row:
            return False
        conn.execute("UPDATE enterprise_subscription SET status='EXPIRED', dunning_state=NULL, grace_until=NULL, "
                     "payment_provider=NULL, updated_at=? WHERE tenant_id=?", (now, tid))
    else:
        return False
    return True


def _record_cycle(conn, ref, sub, pay, rinv=None) -> bool:
    """A paid autopay cycle -> one PAID ATIP invoice and its SUCCEEDED payment. False: already recorded."""
    pay_id = (pay or {}).get("id")
    if not pay_id or _payment_by_remote(conn, pay_id):
        return False
    rinv = rinv or {}
    rinv_id = rinv.get("id") or pay.get("invoice_id")
    linked = _ref(conn, rinv_id) if rinv_id else None
    if linked and linked["kind"] == "invoice":   # this Razorpay invoice already has its ATIP invoice
        return _settle(conn, linked["local_id"], {**pay, "invoice_id": rinv_id}, 0) == "PROCESSED"
    from enterprise import billing
    tid = ref["tenant_id"]
    plan = ref["data"].get("plan_id") or _notes(sub).get("atip_plan_id") or \
        (billing.subscription(conn, tid) or {}).get("plan_id")
    paise = pay.get("amount") if pay.get("amount") is not None else (rinv.get("amount_paid") or rinv.get("amount"))
    amount, cur = from_paise(paise), str(pay.get("currency") or rinv.get("currency") or "INR")
    ps, pe = _ts(rinv.get("billing_start") or sub.get("current_start")), _ts(rinv.get("billing_end")
                                                                               or sub.get("current_end"))
    now = _now()
    iid = "INV" + uuid.uuid4().hex[:10].upper()
    conn.execute("INSERT INTO enterprise_invoice (invoice_id,tenant_id,period_start,period_end,plan_id,amount,currency,"
                 "status,lines_json,created_at,finalized_at,due_date,paid_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (iid, tid, str(ps.date()) if ps else None, str(pe.date()) if pe else None, plan, amount, cur, "PAID",
                  json.dumps({"plan": plan, "provider": PROVIDER, "subscription_id": ref["remote_id"],
                              "razorpay_invoice_id": rinv_id, "razorpay_payment_id": pay_id,
                              "method": pay.get("method")}), now, now, str(now.date()), now))
    ap = _insert_payment(conn, tid, iid, amount, cur, "SUCCEEDED", rinv_id or ref["remote_id"], pay_id)
    conn.execute("UPDATE enterprise_invoice SET payment_id=? WHERE invoice_id=?", (ap, iid))
    if rinv_id:
        _put(conn, rinv_id, "invoice", ref["mode"], iid, tid, status="paid", url=rinv.get("short_url"),
             data={"subscription_id": ref["remote_id"]})
    audit.record(conn, "billing.invoice_paid", tenant_id=tid, actor="razorpay", resource=iid,
                 details={"amount": amount, "razorpay_payment_id": pay_id, "subscription": ref["remote_id"]},
                 commit=False)
    conn.commit()
    _tell(conn, tid, f"Autopay payment received ({iid})", f"{cur} {amount:,.2f}")
    return True


def _on_invoice_paid(conn, e, at) -> str:
    inv, pay = e.get("invoice") or {}, dict(e.get("payment") or {})
    if not pay.get("invoice_id"):
        pay["invoice_id"] = inv.get("id")
    if not pay.get("id"):
        pay["id"] = inv.get("payment_id")
    if pay.get("amount") is None and inv.get("amount_paid") is not None:
        pay["amount"] = inv["amount_paid"]
    iid = _atip_invoice(conn, pay=pay, inv=inv)
    if iid:
        return _settle(conn, iid, pay, at)
    if not inv.get("subscription_id"):
        return "IGNORED"
    ref = _ref(conn, inv["subscription_id"]) or _sub_ref(conn, {"id": inv["subscription_id"], "notes": _notes(inv)})
    if not ref or not _record_cycle(conn, ref, {"id": inv["subscription_id"]}, pay, inv):
        return "IGNORED"
    if _is_current(conn, ref):
        _clear_dunning(conn, ref["tenant_id"])
        conn.commit()
    return "PROCESSED"


def _on_subscription(state):
    def handle(conn, e, at) -> str:
        sub = e.get("subscription") or {}
        ref = _sub_ref(conn, sub)
        if not ref:
            return "IGNORED"
        money = _record_cycle(conn, ref, sub, e.get("payment") or {}) if state == "charged" else False
        changed = _apply_status(conn, ref, sub, _STATE_EVENT[state], at)
        if changed:
            audit.record(conn, f"billing.subscription_{state}", tenant_id=ref["tenant_id"], actor="webhook:razorpay",
                         resource=ref["remote_id"], details={"razorpay_status": sub.get("status")}, commit=False)
        conn.commit()
        if changed and state in ("pending", "halted"):
            _tell(conn, ref["tenant_id"], "Automatic payment failed",
                  "Razorpay could not charge the autopay mandate" + (" and stopped retrying" if state == "halted"
                                                                       else "; it will retry") +
                  ". Pay before the grace period ends to keep the workspace active.", "warning")
        return "PROCESSED" if changed or money else "IGNORED"
    return handle


def _on_refund(conn, e, at) -> str:
    rf = e.get("refund") or {}
    rid, pay_id = rf.get("id"), rf.get("payment_id") or (e.get("payment") or {}).get("id")
    row = _payment_by_remote(conn, pay_id)
    if not rid or not row or row["status"] not in ("SUCCEEDED", "PARTIALLY_REFUNDED") or _ref(conn, rid):
        return "IGNORED"                          # not an ATIP payment, or this refund is already applied
    amt = from_paise(rf.get("amount"))
    total = round(float(row.get("refunded_amount") or 0) + amt, 2)
    full = total >= float(row.get("amount") or 0) - 0.005
    conn.execute("UPDATE enterprise_payment SET refunded_amount=?, status=?, updated_at=? WHERE payment_id=?",
                 (total, "REFUNDED" if full else "PARTIALLY_REFUNDED", _now(), row["payment_id"]))
    if full and row.get("invoice_id"):
        conn.execute("UPDATE enterprise_invoice SET status='REFUNDED' WHERE invoice_id=? AND status='PAID'",
                     (row["invoice_id"],))
    _put(conn, rid, "refund", _mode(), row["payment_id"], row["tenant_id"], status=rf.get("status") or "processed",
         data={"amount": amt, "razorpay_payment_id": pay_id})
    audit.record(conn, "billing.refund", tenant_id=row["tenant_id"], actor="webhook:razorpay",
                 resource=row["invoice_id"], details={"payment_id": row["payment_id"], "refund": rid, "amount": amt,
                                                      "full": full}, commit=False)
    conn.commit()
    _tell(conn, row["tenant_id"], f"Refund processed for invoice {row['invoice_id']}",
          f"{row.get('currency') or 'INR'} {amt:,.2f}" + (" (full refund)" if full else ""))
    return "PROCESSED"


HANDLERS = {
    "payment.captured": _on_payment_captured,
    "payment.failed": _on_payment_failed,
    "invoice.paid": _on_invoice_paid,
    "subscription.activated": _on_subscription("activated"),
    "subscription.charged": _on_subscription("charged"),
    "subscription.pending": _on_subscription("pending"),
    "subscription.halted": _on_subscription("halted"),
    "subscription.cancelled": _on_subscription("cancelled"),
    "subscription.completed": _on_subscription("completed"),
    "refund.processed": _on_refund,
}
# webhook suffix / fetched Razorpay status -> the state _apply_status maps onto ATIP
_STATE_EVENT = {"activated": "active", "charged": "active", "active": "active", "pending": "pending",
                "halted": "halted", "cancelled": "cancelled", "completed": "completed"}


# ── polling: the path that works without a reachable webhook ─────────────────────────────────
def sync_subscription(conn, tenant_id, prov=None) -> dict:
    """Fetch the tenant's current Razorpay subscription and its paid invoices; apply them."""
    prov = prov or RazorpayProvider()
    prov._require()
    ref = _latest(conn, "subscription", tenant_id)
    if not ref:
        raise ValueError(f"no Razorpay subscription for {tenant_id}")
    sub = prov.fetch_subscription(ref["remote_id"])
    items = prov.request("GET", "/invoices", params={"subscription_id": ref["remote_id"], "count": 100}).get("items")
    recorded = 0
    for inv in sorted(items or [], key=lambda i: i.get("created_at") or 0):
        if inv.get("status") == "paid" and inv.get("payment_id"):
            pay = {"id": inv["payment_id"], "amount": inv.get("amount_paid") or inv.get("amount"),
                   "currency": inv.get("currency"), "invoice_id": inv["id"]}
            recorded += int(_record_cycle(conn, ref, sub, pay, inv))
    state = _STATE_EVENT.get(str(sub.get("status")))
    if state:                                     # the fetched state is authoritative as of now
        applied = _apply_status(conn, ref, sub, state, int(time.time()))
    else:
        _touch(conn, ref["remote_id"], status=sub.get("status"))
        applied = False
    conn.commit()
    return {"tenant_id": tenant_id, "subscription_id": ref["remote_id"], "razorpay_status": sub.get("status"),
            "invoices_recorded": recorded, "status_applied": applied}


def reconcile(conn, limit=50, prov=None) -> dict:
    """Settle OPEN ATIP invoices Razorpay reports paid and sync current autopay subscriptions."""
    prov = prov or RazorpayProvider()
    st = prov.status()
    if st["state"] not in ("TEST", "LIVE"):
        return {"skipped": st["state"]}
    settled = synced = errors = 0
    rows = conn.execute("SELECT r.remote_id, r.local_id FROM enterprise_billing_ref r JOIN enterprise_invoice i ON "
                        "i.invoice_id=r.local_id WHERE r.provider=? AND r.kind='invoice' AND r.mode=? AND "
                        "i.status='OPEN' ORDER BY r.updated_at LIMIT ?", (PROVIDER, prov.mode, int(limit))).fetchall()
    for remote, local in rows:
        try:
            inv = prov.fetch_invoice(remote)
        except ProviderError as e:
            errors += 1
            log.warning(f"razorpay reconcile: invoice {remote}: {e}")
            continue
        _touch(conn, remote, status=inv.get("status"))
        conn.commit()
        if inv.get("status") == "paid":
            pay = {"id": inv.get("payment_id"), "amount": inv.get("amount_paid"), "currency": inv.get("currency"),
                   "invoice_id": remote}
            settled += int(_settle(conn, local, pay, 0, actor="reconcile:razorpay") == "PROCESSED")
    tids = [r[0] for r in conn.execute("SELECT DISTINCT tenant_id FROM enterprise_billing_ref WHERE provider=? AND "
                                       "kind='subscription' AND mode=?", (PROVIDER, prov.mode))]
    for tid in tids:
        cur = _latest(conn, "subscription", tid)
        if not cur or cur["status"] not in SUB_OPEN:
            continue
        try:
            sync_subscription(conn, tid, prov)
            synced += 1
        except ProviderError as e:
            errors += 1
            log.warning(f"razorpay reconcile: subscription of {tid}: {e}")
    return {"mode": prov.mode, "invoices_checked": len(rows), "invoices_settled": settled,
            "subscriptions_synced": synced, "errors": errors}
