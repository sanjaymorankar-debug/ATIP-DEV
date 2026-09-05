"""ATIP — NSE Trading Calendar

Lets pipelines know whether "today" is an actual NSE trading day, so they
stop repeatedly requesting a Bhavcopy that will never exist on weekends
and holidays, and instead fall back to the last working day's data.

⚠️  NSE_HOLIDAYS below only has the fixed-date holidays pre-filled
(Republic Day, Independence Day, Gandhi Jayanti, Christmas). Festival
holidays (Holi, Ram Navami, Eid, Ganesh Chaturthi, Dussehra, Diwali
Laxmi Puja, Gurpurab, etc.) move every year on the lunar calendar and
must be added manually from NSE's official holiday circular:
  https://www.nseindia.com/resources/exchange-communication-holidays
Until you add this year's festival dates, the pipeline will still try
(and gracefully fail-forward to the last cached trading day) on those
specific dates — it just won't skip them proactively in advance.
"""
from datetime import date, timedelta, datetime, time as _time

NSE_HOLIDAYS = {
    date(2026, 1, 26),   # Republic Day
    date(2026, 8, 15),   # Independence Day
    date(2026, 10, 2),   # Gandhi Jayanti
    date(2026, 12, 25),  # Christmas
    # TODO: add this year's festival holidays from the NSE circular linked above.
}


def is_trading_day(d: date) -> bool:
    """Mon–Fri and not in the known NSE holiday list."""
    return d.weekday() < 5 and d not in NSE_HOLIDAYS


def last_trading_day(d: date = None) -> date:
    """Walk backward from d (default: today) to the most recent trading day."""
    if d is None:
        d = date.today()
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


POSTMARKET_CUTOFF = _time(16, 0)  # 4:00 PM IST — NSE Bhavcopy/EOD data is typically out by then


def postmarket_target_date(now: datetime = None) -> date:
    """
    Which trading day's post-market (EOD) data should be downloaded right now.

    - Today is a trading day AND it's 4:00 PM IST or later
        -> today's EOD data should be published; target today.
    - Today is a trading day but it's before 4:00 PM IST
        -> today's EOD data isn't out yet; target the previous trading day.
    - Today isn't a trading day at all (weekend/holiday)
        -> target the previous trading day.
    """
    now = now or datetime.now()
    today = now.date()
    if is_trading_day(today) and now.time() >= POSTMARKET_CUTOFF:
        return today
    return last_trading_day(today - timedelta(days=1))
