"""
Tables added in W39 (history, research & options tooling), applied by db/schema.py.

    prices_daily_backfill   DP-11  per-symbol progress of the 7-year daily history backfill
    research_report         RS-06  one equity research report per symbol per day; calls and their outcomes
"""

from data.history_backfill import DDL as _BACKFILL
from research.report import DDL as _REPORT

W39_TABLES = {
    "prices_daily_backfill": (_BACKFILL,),
    "research_report": _REPORT,
}
