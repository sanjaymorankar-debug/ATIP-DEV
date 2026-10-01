"""
Regulatory sign-off register (W38, ENT-14). ENT-14 is legal work: software cannot make ATIP compliant.
What it can do is keep the list of obligations that a qualified professional must confirm, record who
signed each off and on what evidence, and refuse to call the platform ready while a gate is open:

    gate pre_multi_user   before anyone other than the owner uses ATIP (enterprise.enabled)
    gate pre_live         before any real-money order (execution.live_trading_enabled)
    gate ongoing          recurring duties (reviews, incident reporting, filings)

ops/compliance.py's regulatory_signoff check FAILS when a gate is crossed with items in it not
SIGNED_OFF. The register text is a starting point prepared by engineering, not legal advice: every
item says what to confirm, and the reviewer records the actual position. docs/ENT14_REGULATORY_PACK.md
explains each item.

    status: OPEN -> IN_REVIEW -> SIGNED_OFF | NOT_APPLICABLE   (SIGNED_OFF / NOT_APPLICABLE need a reviewer
    and a reference: an opinion, registration number, contract clause ...; every change is audited)
"""

from __future__ import annotations

from datetime import datetime

STATUSES = ("OPEN", "IN_REVIEW", "SIGNED_OFF", "NOT_APPLICABLE")
GATES = ("pre_multi_user", "pre_live", "ongoing")

DDL = """CREATE TABLE IF NOT EXISTS regulatory_item (
    item_id TEXT PRIMARY KEY, area TEXT NOT NULL, title TEXT NOT NULL, confirm TEXT NOT NULL, gate TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN', reviewer TEXT, reference TEXT, note TEXT, updated_at TIMESTAMP,
    signed_at TIMESTAMP)"""

# (item_id, area, title, what the reviewer must confirm, gate)
ITEMS = [
    ("SEBI-RA-IA", "SEBI", "Research Analyst / Investment Adviser registration",
     "Whether showing ATIP's BUY / SELL signals, scores or model portfolios to other people is 'research' or "
     "'investment advice' under the SEBI (Research Analysts) Regulations, 2014 / (Investment Advisers) "
     "Regulations, 2013, and which registration (or exemption) applies", "pre_multi_user"),
    ("SEBI-ALGO", "SEBI", "Retail algorithmic trading framework",
     "Obligations under SEBI's framework for retail participation in algorithmic trading (Feb 2025 circular and "
     "exchange implementation standards): broker approval / empanelment, algo registration and IDs, order-per-"
     "second thresholds, static-IP API access, 2FA -- and the dates currently in force", "pre_live"),
    ("BROKER-API", "Broker", "Broker API terms of use",
     "Dhan / Zerodha (Kite Connect) / Upstox / Angel One API terms: personal use only, or may the API serve "
     "other users' accounts; automated-order permissions; rate limits", "pre_live"),
    ("NSE-DATA", "Exchange", "Market-data redistribution licence",
     "Showing NSE prices, indices or bhavcopy-derived data to third parties may need an exchange data-vendor "
     "/ redistribution licence; confirm what is permitted from each data source (NSE files, broker feeds, "
     "yfinance)", "pre_multi_user"),
    ("DPDP", "Privacy", "Digital Personal Data Protection Act, 2023 and Rules",
     "Notice and consent content, the data inventory, retention, data-principal rights and response times, "
     "grievance officer, breach notification, processor contracts -- against the Act and the Rules in force",
     "pre_multi_user"),
    ("CERT-IN", "Security", "CERT-In directions (2022)",
     "Incident reporting to CERT-In within the required window, log retention (in India) and NTP "
     "synchronisation, as they apply to this service and its hosting", "pre_multi_user"),
    ("TERMS", "Contract", "Terms of service and risk disclosure",
     "Terms, limitation of liability, 'not investment advice' / model-output disclaimers and the risk disclosure "
     "shown at sign-up (enterprise_consent documents terms / privacy / risk_disclosure)", "pre_multi_user"),
    ("PAYMENTS-TAX", "Finance", "Payments and GST",
     "Payment-aggregator terms (no card data is stored by ATIP), GST registration / invoicing on subscriptions",
     "pre_multi_user"),
    ("LIVE-RISK", "Trading", "Owner sign-off of live-trading risk controls",
     "Order limits, kill switch, daily-loss and drawdown limits, per-second order cap and the reconciliation "
     "process reviewed and accepted by the owner before the master switch is turned on", "pre_live"),
    ("REVIEW-ANNUAL", "Governance", "Annual compliance review",
     "Re-confirm every item above once a year and after any regulatory change", "ongoing"),
]


def ensure(conn):
    conn.execute(DDL)
    for iid, area, title, confirm, gate in ITEMS:
        conn.execute("INSERT OR IGNORE INTO regulatory_item (item_id, area, title, confirm, gate, status, updated_at) "
                     "VALUES (?,?,?,?,?, 'OPEN', ?)", (iid, area, title, confirm, gate, datetime.now()))
    conn.commit()


def items(conn) -> list:
    ensure(conn)
    order = {g: i for i, g in enumerate(GATES)}
    rows = [dict(r) for r in conn.execute("SELECT * FROM regulatory_item")]
    return sorted(rows, key=lambda r: (order.get(r["gate"], 9), r["item_id"]))


def update(conn, item_id, status, actor, reviewer=None, reference=None, note=None) -> dict:
    ensure(conn)
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    r = conn.execute("SELECT * FROM regulatory_item WHERE item_id=?", (item_id,)).fetchone()
    if not r:
        raise LookupError(f"no register item {item_id}")
    if status in ("SIGNED_OFF", "NOT_APPLICABLE") and not ((reviewer or "").strip() and (reference or "").strip()):
        raise ValueError(f"{status} needs the reviewer's name and a reference (opinion, registration no., clause)")
    now = datetime.now()
    conn.execute("UPDATE regulatory_item SET status=?, reviewer=?, reference=?, note=?, updated_at=?, signed_at=? "
                 "WHERE item_id=?", (status, reviewer or r["reviewer"], reference or r["reference"],
                                     note if note is not None else r["note"], now,
                                     now if status in ("SIGNED_OFF", "NOT_APPLICABLE") else None, item_id))
    try:
        from enterprise import audit
        audit.record(conn, "regulatory.item_updated", actor=actor, resource=item_id,
                     details={"from": r["status"], "to": status, "reviewer": reviewer, "reference": reference},
                     commit=False)
    except Exception:
        pass
    conn.commit()
    return dict(conn.execute("SELECT * FROM regulatory_item WHERE item_id=?", (item_id,)).fetchone())


def open_items(conn, gate) -> list:
    return [i for i in items(conn) if i["gate"] == gate and i["status"] not in ("SIGNED_OFF", "NOT_APPLICABLE")]
