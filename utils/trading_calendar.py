"""ATIP — NSE Trading Calendar

Lets pipelines know whether "today" is an actual NSE trading day, so they
stop repeatedly requesting a Bhavcopy that will never exist on weekends
and holidays, and instead fall back to the last working day's data.

NSE_HOLIDAYS holds NSE's official Capital Market (equity) trading holidays,
taken verbatim from NSE's own API (https://www.nseindia.com/api/holiday-master?
type=trading, "CM" segment) on 2026-09-18. Festival holidays move every year on
the lunar calendar, so this set must be refreshed each year from that endpoint
or the circular at https://www.nseindia.com/resources/exchange-communication-holidays

Why it matters: the list used to hold only the four fixed-date holidays, with a
TODO for the rest. Ganesh Chaturthi (2026-09-14) was therefore treated as a
trading day, and the post-market pipeline scored it -- producing four BUY
signals for a day NSE was closed, computed on prices five days stale. The
post-market pipeline now also refuses to score any day with no EOD prices
(see pipeline/scheduler.py), so a holiday missing from this list degrades to
"skipped, with a log line" rather than "phantom signals" -- but keep it current.
"""
from datetime import date, timedelta, datetime, time as _time

NSE_HOLIDAYS = {
    # 2026 — NSE CM segment, official (holiday-master API, fetched 2026-09-18).
    # Weekend entries are kept for completeness; weekday logic skips them anyway.
    date(2026, 1, 15),   # Municipal Corporation Election - Maharashtra
    date(2026, 1, 26),   # Republic Day
    date(2026, 2, 15),   # Mahashivratri (Sunday)
    date(2026, 3, 3),    # Holi
    date(2026, 3, 21),   # Id-Ul-Fitr (Ramadan Eid) (Saturday)
    date(2026, 3, 26),   # Shri Ram Navami
    date(2026, 3, 31),   # Shri Mahavir Jayanti
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 5, 28),   # Bakri Id
    date(2026, 6, 26),   # Muharram
    date(2026, 8, 15),   # Independence Day (Saturday)
    date(2026, 9, 14),   # Ganesh Chaturthi
    date(2026, 10, 2),   # Mahatma Gandhi Jayanti
    date(2026, 10, 20),  # Dussehra
    date(2026, 11, 8),   # Diwali Laxmi Pujan (Sunday; Muhurat session is special)
    date(2026, 11, 10),  # Diwali-Balipratipada
    date(2026, 11, 24),  # Prakash Gurpurb Sri Guru Nanak Dev
    date(2026, 12, 25),  # Christmas
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


# Which DAY post-market should target: after 16:00 the session (closed 15:30)
# is over, so "today" is the day to process. This is NOT when the data exists:
# NSE's CM Bhavcopy carries Last-Modified 11:03 GMT = 16:33 IST, and Dhan's
# daily-history endpoint does not return a day's bar until the next day. The
# old comment here claimed Bhavcopy was "typically out by" 16:00, and the
# scheduler ran at 16:05 on that belief -- every attempt 404'd, and from
# 2026-09-10 every day was scored on the previous day's closes. The scheduler
# now runs post-market at 16:45 and re-checks at 18:30.
POSTMARKET_CUTOFF = _time(16, 0)


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
