"""
Tables and additive columns added in W39 (tracker reconciliation 2026-10-07: the owner's
Wave 1-20 deployment tracker, the PERF-001 detail sheet and the owner notes), applied by
db/schema.py.

    perf_ledger (+ columns)   PERF-001-01  entry_seq (portable entry order: MySQL has no rowid),
                                           order_ref / signal_ref (exact order and signal links),
                                           fee_breakdown (JSON: brokerage / stt / exchange / sebi /
                                           stamp / gst / other, when the source gives it)
"""

W39_TABLES = {}

W39_COLUMNS = {
    "perf_ledger": {"entry_seq": "INTEGER", "order_ref": "TEXT", "signal_ref": "TEXT", "fee_breakdown": "TEXT"},
}
