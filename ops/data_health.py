"""
Data-pipeline freshness (read-only).

sources(conn) -> one row per market-data source:
    source, table, latest (date / timestamp), age_days (calendar), sessions_behind
    (NSE weekday sessions after `latest` up to the last completed session), rows_latest,
    status OK / STALE / MISSING, and the threshold used.
Thresholds are in NSE sessions (utils/trading_calendar; weekdays if unavailable).
The latest W1 data-quality results (data_quality, check DQS = the session score)
come from quality().

P1-7: the intraday sources (live quotes, index levels) also carry age_minutes, the
minutes since their newest row. While the NSE session is open they are judged on it
(basis "minutes": STALE beyond max_age_minutes) -- counted in sessions, quotes that
stopped at 10:00 looked current until the next day.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

# source, table, time expression, max sessions behind before STALE, intraday?
SOURCES = (
    ("prices_daily", "prices_daily", "MAX(date)", 1, False),
    ("ai_scores", "ai_scores", "MAX(date)", 1, False),
    ("market_health", "market_health", "MAX(date)", 1, False),
    ("fii_dii", "fii_dii_market", "MAX(date)", 2, False),
    ("global_markets", "global_markets", "MAX(date)", 2, False),
    ("index_levels", "index_levels", "MAX(date)", 1, True),
    ("live_quotes", "live_quotes", "MAX(timestamp)", 1, True),
    ("news", "news_articles", "MAX(fetched_at)", 2, False),
)

# P1-7: intraday source -> (its newest row's timestamp, max minutes old while the session is open).
# Both refresh every 15 minutes; live_quotes rows from the WebSocket writer carry a 'T'.
INTRADAY_MINUTES = {
    "live_quotes": ("MAX(REPLACE(SUBSTR(timestamp,1,19),'T',' '))", 20),
    "index_levels": ("MAX(date || ' ' || time)", 30),
}
SESSION_OPEN, SESSION_CLOSE = time(9, 15), time(15, 30)


def in_session(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    return _is_session(now.date()) and SESSION_OPEN <= now.time() <= SESSION_CLOSE


def _is_session(d: date) -> bool:
    try:
        from utils.trading_calendar import is_trading_day
        return is_trading_day(d)
    except Exception:
        return d.weekday() < 5


def last_session(now: datetime | None = None) -> date:
    """The latest trading day whose close (15:30 IST) has passed."""
    now = now or datetime.now()
    d = now.date()
    if now.hour * 60 + now.minute < 15 * 60 + 30:
        d -= timedelta(days=1)
    for _ in range(15):
        if _is_session(d):
            break
        d -= timedelta(days=1)
    return d


def sessions_between(a: date, b: date) -> int:
    """Trading sessions in (a, b]."""
    if a >= b:
        return 0
    n, d = 0, a + timedelta(days=1)
    while d <= b:
        n += _is_session(d)
        d += timedelta(days=1)
    return n


def _as_date(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.fromisoformat(str(v)[:19].replace("T", " ")).date()
    except ValueError:
        try:
            return date.fromisoformat(str(v)[:10])
        except ValueError:
            return None


def sources(conn, now: datetime | None = None) -> list:
    now = now or datetime.now()
    ref = last_session(now)
    out = []
    for name, table, expr, max_behind, _intraday in SOURCES:
        row = {"source": name, "table": table, "latest": None, "age_days": None, "sessions_behind": None,
               "max_sessions_behind": max_behind, "status": "MISSING"}
        try:
            latest = conn.execute(f"SELECT {expr} FROM {table}").fetchone()[0]
        except Exception as e:
            row["error"] = type(e).__name__
            out.append(row)
            continue
        d = _as_date(latest)
        if d:
            row["latest"] = str(latest)[:19]
            row["age_days"] = (now.date() - d).days
            row["sessions_behind"] = sessions_between(d, ref)
            row["status"] = "OK" if row["sessions_behind"] <= max_behind else "STALE"
        row["basis"] = "sessions"
        if name in INTRADAY_MINUTES:
            _minutes(conn, row, name, table, now)
        out.append(row)
    return out


def _minutes(conn, row, name, table, now):
    """age_minutes / max_age_minutes for an intraday source; in the session, once its first refresh is
    due, its status is decided on them."""
    expr, limit = INTRADAY_MINUTES[name]
    row["max_age_minutes"] = limit
    try:
        ts = datetime.fromisoformat(str(conn.execute(f"SELECT {expr} FROM {table}").fetchone()[0])[:19])
    except Exception:
        ts = None
    row["age_minutes"] = round((now - ts).total_seconds() / 60) if ts else None
    if ts and in_session(now) and now >= datetime.combine(now.date(), SESSION_OPEN) + timedelta(minutes=limit):
        row["status"] = "OK" if row["age_minutes"] <= limit else "STALE"
        row["basis"] = "minutes"


def quality(conn, limit_days=3) -> list:
    try:
        rows = conn.execute("SELECT date, check_name, severity, failed, checked, score FROM data_quality "
                            "WHERE date >= (SELECT DATE(MAX(date), ?) FROM data_quality) ORDER BY date DESC, "
                            "check_name", (f"-{limit_days} days",)).fetchall()
        return [dict(r) if hasattr(r, "keys") else dict(zip(("date", "check_name", "severity", "failed", "checked",
                                                             "score"), r)) for r in rows]
    except Exception:
        return []


def symbol_bar_age(conn, symbol, now: datetime | None = None) -> int | None:
    """Sessions between the symbol's last daily bar and the last completed session (None = no bars)."""
    try:
        r = conn.execute("SELECT MAX(date) FROM prices_daily WHERE symbol=?", (symbol,)).fetchone()
    except Exception:
        return None
    d = _as_date(r[0] if r else None)
    return None if d is None else sessions_between(d, last_session(now))


# ── 2026-10-31 (MON-07): every Dhan live feed, from live_feed_status ─────────────────────────────────
# feed, label, the config.json switch that turns it on, seconds without data before STALE in the session.
# Every feed writes its own row (data/dhan_ws.record_feed_status); a feed switched off records mode OFF when
# ATIP starts, so it reads OFF here -- not missing, not a problem.
FEEDS = (
    ("indices", "Index WebSocket (NSE indexes)", None, 300),
    ("stocks", "Stock WebSocket (up to 100 stocks)", "live_feed.stocks_enabled", 300),
    ("ticks", "Tick capture", "live_feed.capture_ticks", 300),
    ("minute_bars", "1-minute bars", "live_feed.minute_bars", 300),
    ("depth20", "20-level depth (DWI / OFI)", "depth20.enabled", 120),
    ("option_chain", "Option chain", "derivatives.option_chain_enabled", None),
)
FEED_SESSION = (time(9, 20), time(15, 30))      # when a silent feed is judged stale
HEARTBEAT_SECONDS = 180                         # a status row not refreshed for this long: the feed stopped
PROBLEM_STATES = ("STALE", "NOT STARTED", "PARKED", "ERROR", "RECONNECTING")
WORKING_STATES = ("LIVE", "WS", "REST", "PARTIAL", "CONNECTING")


def _feed_enabled(feed: str) -> bool:
    if feed == "indices":
        return True
    if feed in ("stocks", "ticks", "minute_bars"):
        from data.stock_feed import settings
        s = settings()
        return bool(s.get("stocks_enabled")) and (feed == "stocks" or s.get({"ticks": "capture_ticks",
                                                                             "minute_bars": "minute_bars"}[feed]) is True)
    if feed == "depth20":
        from data.depth20 import settings
        return bool(settings().get("enabled"))
    from data.derivatives_store import settings
    return bool(settings().get("option_chain_enabled"))


def _ts(v):
    try:
        return datetime.fromisoformat(str(v)[:19].replace("T", " ")) if v else None
    except ValueError:
        return None


def feeds(conn, now: datetime | None = None, use_config: bool | None = None) -> list:
    """One row per feed: feed, label, switch, enabled, state, mode, detail, subscribed, ticks, last_data_at,
    data_age_s, updated_at, problem.

    state  OFF          switched off (config.json, or the row the Mac wrote)
           NOT STARTED  switched on but no status yet: it starts at ATIP's next restart or 06:00 resume
           IDLE         outside the feed window (09:00-15:45 on a trading day, not in the stop window)
           LIVE / WS / REST / PARTIAL / CONNECTING / RECONNECTING / PARKED / ERROR   the feed's own mode
           STALE        in the session: no data for longer than the feed's limit, or its status row has not
                        been refreshed for HEARTBEAT_SECONDS (the feed, or ATIP, has stopped)
    problem: switched on, inside the session, and in one of PROBLEM_STATES.
    use_config (default: not a VIEWER) reads the switches from this copy's config.json; a VIEWER has none and
    goes by the rows the Mac's database carries."""
    now = now or datetime.now()
    if use_config is None:
        try:
            from ops.role import is_viewer
            use_config = not is_viewer()
        except Exception:
            use_config = True
    try:
        rows = {r[0]: r for r in conn.execute(
            "SELECT feed, mode, subscribed, ticks, CAST(last_tick_at AS TEXT), CAST(last_flush_at AS TEXT), detail, "
            "CAST(updated_at AS TEXT) FROM live_feed_status")}
    except Exception:
        rows = {}
    try:
        from data.dhan_ws import feed_window_open
        window = feed_window_open(now)
    except Exception:
        window = False
    judged = window and _is_session(now.date()) and FEED_SESSION[0] <= now.time() <= FEED_SESSION[1]
    out = []
    for feed, label, switch, stale_s in FEEDS:
        r = rows.get(feed)
        mode = r[1] if r else None
        if feed == "option_chain":
            try:
                from data.derivatives_store import settings
                stale_s = float(settings().get("chain_minutes") or 15) * 120 + 120
            except Exception:
                stale_s = 1920
        enabled = _feed_enabled(feed) if use_config else (r is not None and mode != "OFF")
        last = _ts(r[4]) or _ts(r[5]) if r else None
        updated = _ts(r[7]) if r else None
        row = {"feed": feed, "label": label, "switch": switch, "enabled": enabled, "mode": mode,
               "detail": (r[6] if r else None) or "", "subscribed": r[2] if r else None, "ticks": r[3] if r else None,
               "last_data_at": str(last)[:19] if last else None,
               "data_age_s": round((now - last).total_seconds()) if last else None,
               "updated_at": str(updated)[:19] if updated else None, "problem": False}
        if not enabled:
            row["state"] = "OFF"
            if mode != "OFF" or not row["detail"]:
                row["detail"] = f"off: set config.json {switch}" if switch else "off"
        elif r is None or (mode == "OFF" and use_config):
            row["state"] = "NOT STARTED"
            row["detail"] = "switched on; starts with ATIP's next restart or the 06:00 resume"
        elif not window:
            row["state"] = "IDLE"
            row["detail"] = "market closed"
        elif judged and updated and (now - updated).total_seconds() > max(HEARTBEAT_SECONDS, stale_s):
            row["state"] = "STALE"
            row["detail"] = f"no status for {(now - updated).total_seconds() / 60:.0f} min (feed or ATIP stopped)"
        elif mode in WORKING_STATES and judged and (last is None or (now - last).total_seconds() > stale_s):
            row["state"] = "STALE"
            row["detail"] = (f"no data for {(now - last).total_seconds() / 60:.0f} min" if last else
                             "no data yet this session") + (f" ({row['detail']})" if row["detail"] else "")
        else:
            row["state"] = mode or "IDLE"
        row["problem"] = bool(enabled and judged and row["state"] in PROBLEM_STATES)
        out.append(row)
    return out
