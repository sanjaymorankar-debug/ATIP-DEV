"""
Billing gateway tables and columns (W39b: ENT-04 Razorpay, API-04 provider payload mapping),
applied by db/schema.py.

    enterprise_billing_ref   one row per object ATIP created at -- or learned from -- the gateway:
                             kind customer (local_id = tenant), plan (local_id = plan|paise|currency|
                             period|interval), subscription (local_id = tenant; the newest row is the
                             tenant's current one), invoice (local_id = ATIP invoice_id), refund
                             (local_id = ATIP payment_id). PRIMARY KEY (provider, remote_id), so a
                             gateway id maps to exactly one ATIP object. mode test / live: ids of one
                             mode are meaningless in the other. event_at = created_at (unix) of the
                             newest webhook event applied to the object -- older events do not change
                             its status (out-of-order delivery). Gateway ids only: no card / UPI data.

Columns added to existing tables (BILLING_COLUMNS, ALTER TABLE ADD COLUMN):
    enterprise_payment   provider_payment_id   the gateway's payment id (Razorpay pay_...): a payment
                                               is applied once, whatever event reports it
                         refunded_amount       sum of processed refunds (rupees)
"""

BILLING_TABLES = {
    "enterprise_billing_ref": (
        """CREATE TABLE IF NOT EXISTS enterprise_billing_ref (
            provider TEXT NOT NULL, remote_id TEXT NOT NULL, kind TEXT NOT NULL, mode TEXT NOT NULL,
            local_id TEXT NOT NULL, tenant_id TEXT NOT NULL, status TEXT, url TEXT, event_at INTEGER,
            data_json TEXT, created_at TIMESTAMP, updated_at TIMESTAMP, PRIMARY KEY (provider, remote_id))""",
        "CREATE INDEX IF NOT EXISTS idx_ent_billing_ref_local ON enterprise_billing_ref(provider, kind, local_id, "
        "created_at)",
    ),
}

BILLING_COLUMNS = {
    "enterprise_payment": {"provider_payment_id": "TEXT", "refunded_amount": "REAL"},
}

# indexes on the columns above (created after the columns exist)
BILLING_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_ent_payment_remote ON enterprise_payment(provider_payment_id)",
)
