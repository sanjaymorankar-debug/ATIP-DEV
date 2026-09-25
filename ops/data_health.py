"""
Data-pipeline freshness (read-only).

sources(conn) -> one row per market-data source:
    source, table, latest (date / timestamp), age_days (calendar), sessions_behind
    (NSE weekday sessions after `latest` up to the last completed session), rows_latest,
    status OK / STALE / MISSING, and the threshold used.
Thresholds are in NSE sessions (utils/trading_calendar; weekdays if unavailable).
The latest W1 data-quality results (data_quality, check DQS = the session score)
come from quality().
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

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
        out.append(row)
    return out


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
