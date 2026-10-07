"""
Tables added in W39 (history, research & options tooling), applied by db/schema.py.

    prices_daily_backfill   DP-11  per-symbol progress of the 7-year daily history backfill
    research_report         RS-06  one equity research report per symbol per day; calls and their outcomes
    research_screen         SC-20  saved fundamental screens (query, sort, columns, notify, last matches)
    technical_snapshot      TA-05  per-symbol daily technical rating, indicators, patterns, scan hits
    technical_signal        TA-06  every scan hit with entry / stop / target, confluence and its outcome
    order_book_pressure     OB-01  total pending buy / sell quantity and imbalance per stock, every 15 minutes
    fo_participant_oi       MP-04  NSE participant-wise open interest (Client / DII / FII / Pro)
    market_cue              MP-02  pre-open GIFT Nifty and global-model gap estimates, with the actual open
    market_regime_gate      RG-01  the Nifty market gate per session: distribution days, status, OPEN / CAUTION / CLOSED
    fundamental_scorecard   FS-03  each stock's scorecard per day: checks passed of 30, per axis, pass / fail flags
    macro_event             EV-01  FOMC, US CPI, US payrolls and RBI policy dates (seeded; config and API add more)
    intraday_signal         IN-01  intraday scan hits on 15-minute bars, the price seen, the record at the close

Columns added after a table first shipped (W39_COLUMNS, applied with ALTER TABLE ADD COLUMN):
    technical_signal        RG-03  market_gate, alignment (the gate each signal was born under)
    market_cue              EV-02  events, band_pct (the macro events behind a morning and the band given)
"""

from data.history_backfill import DDL as _BACKFILL
from research.report import DDL as _REPORT
from research.screener import DDL as _SCREEN
from research.tech_signals import DDL as _TECH
from data.order_pressure import DDL as _BOOK
from data.participant_oi import DDL as _POI
from research.market_pulse import DDL as _CUE
from research.market_pulse import ADDED_COLUMNS as _CUE_COLS
from research.event_calendar import DDL as _EVENTS
from research.intraday_signals import DDL as _INTRA
from research.regime_gate import DDL as _GATE
from research.scorecard import DDL as _SCORE
from research.tech_signals import ADDED_COLUMNS as _TECH_COLS

W39_TABLES = {
    "prices_daily_backfill": (_BACKFILL,),
    "research_report": _REPORT,
    "research_screen": _SCREEN,
    "technical_snapshot": (_TECH[0], _TECH[1]),
    "technical_signal": (_TECH[2], _TECH[3], _TECH[4]),
    "order_book_pressure": _BOOK,
    "fo_participant_oi": _POI,
    "market_cue": _CUE,
    "market_regime_gate": _GATE,
    "fundamental_scorecard": _SCORE,
    "macro_event": _EVENTS,
    "intraday_signal": _INTRA,
}

W39_COLUMNS = {**_TECH_COLS, **_CUE_COLS}
