"""
Session accounting (SES / ACC, 2 Nov 2026): the money each session's cycles made, gross and after charges and
estimated tax, a double-entry style ledger of it, and the registers and reconciliation built on that ledger.

    tax.py         the tax rates (atip_data/config.json "tax"), the per-cycle estimate and the FY statement
    cycles.py      one row per closed cycle (session_cycles), floor rules, compounding basis, the session book
    ledger.py      ledger_entries: allocations, buys, sells, charges (one line per type), tax, transfers, adjustments
    reconcile.py   ATIP's charges against a broker contract note; session cash against the books

Nothing here places, changes or cancels an order. Every figure is labelled VIRTUAL (paper) or ACTUAL (broker).
"""
