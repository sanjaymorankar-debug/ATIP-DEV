"""
Tick data capture (W35: DP-04).

The W27 stock feed (data/stock_feed.py) receives Dhan Quote packets for up to
live_feed.max_symbols stocks and keeps only the latest quote per symbol. With
live_feed.capture_ticks = true, every Quote packet is also captured here:

    capture(symbol, packet)    called from the feed's message handler: appends
                               {symbol, ltt, ltp, ltq, cum_volume, avg_price, received_at} to an
                               in-memory buffer (bounded at MAX_BUFFER; overflow is counted, never silent)
    flush()                    called by the feed's flush timer: the buffer becomes one Parquet / csv.gz
                               part of lake dataset "ticks" for the day (data/lake.py, DP-22) and
                               tick_capture_status is updated. Nothing tick-level goes into SQLite.
    build_minute_bars(day)     after the close: the day's ticks -> 1-minute OHLCV in intraday_bars
                               (interval_min=1, source 'ticks'); volume per minute from the cumulative
                               day volume, so a dropped packet does not lose volume
    ticks(day, symbol)         read back from the lake

A Dhan Quote packet is a snapshot sent on change, not every exchange trade: this is ATIP's tick
stream, not the exchange's full trade tape. received_at is the local clock; ltt is the exchange
last-trade time from the packet. Off by default.

2026-10-31 (DP-03 / DP-04), live 1-minute bars and the tick store's retention:

    MinuteBars                 live_feed.minute_bars = true: the stock feed hands every Quote packet
                               to this builder; each finished minute becomes an intraday_bars row
                               (interval_min=1, source 'ws') at the feed's next flush. Minutes are by
                               the local clock, so a packet can never land in a minute already written;
                               a stock's first minute after a (re)connect is partial and is not written
                               (its volume would be wrong) -- the REST fallback fills it.
    backfill_minute_bars(day)  REST fallback: the minutes the WebSocket did not build, from Dhan's
                               intraday 1-minute API (source 'dhan'), for the feed's symbols. Runs every
                               15 minutes while the stock feed is on REST failover, and at 15:50.
                               Existing bars are never overwritten (ON CONFLICT DO NOTHING); the
                               after-close build from captured ticks still replaces them (source 'ticks').
    retention                  the lake "ticks" partitions older than the SHORT window (90 days,
                               config.json retention.short_days) are deleted by db/purge.py with the
                               rest of the high-frequency tables (tick_capture_status included).
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import date, datetime

import pandas as pd

log = logging.getLogger(__name__)

MAX_BUFFER = 500_000
_lock = threading.Lock()
_buf: deque = deque()
_state = {"dropped": 0, "captured": 0}


def enabled() -> bool:
    try:
        from data.stock_feed import settings
        return settings().get("capture_ticks") is True
    except Exception:
        return False


def _ltt(v):
    """Dhan LTT arrives as 'HH:MM:SS' (today) or epoch seconds."""
    if v is None:
        return None
    try:
        if isinstance(v, (int, float)) or str(v).isdigit():
            return datetime.fromtimestamp(int(v)).strftime("%Y-%m-%d %H:%M:%S")
        s = str(v)
        return f"{date.today()} {s[-8:]}" if len(s) <= 8 else s[:19]
    except (ValueError, OSError):
        return None


def capture(symbol: str, pkt: dict):
    try:
        row = {"symbol": symbol, "ltt": _ltt(pkt.get("LTT")), "ltp": float(pkt.get("LTP")),
               "ltq": float(pkt.get("LTQ") or 0), "cum_volume": float(pkt.get("volume") or 0),
               "avg_price": float(pkt.get("avg_price") or 0) or None,
               "received_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:23]}
    except (TypeError, ValueError):
        return
    with _lock:
        if len(_buf) >= MAX_BUFFER:
            _state["dropped"] += 1
            return
        _buf.append(row)
        _state["captured"] += 1


def flush(conn=None) -> dict:
    with _lock:
        rows = list(_buf)
        _buf.clear()
        dropped, _state["dropped"] = _state["dropped"], 0
    if not rows:
        return {"rows": 0}
    from data import lake
    df = pd.DataFrame(rows)
    out = {"rows": 0}
    for day, g in df.groupby(df["received_at"].str[:10]):
        r = lake.write("ticks", day, g, knowledge_time=g["received_at"].max()[:19], source="dhan_quote", conn=conn)
        out["rows"] += r.get("rows", 0)
        _status(conn, day, g, dropped)
        dropped = 0
    try:
        from data.dhan_ws import record_feed_status
        record_feed_status("ticks", "LIVE", int(df["symbol"].nunique()), _state["captured"],
                           df["received_at"].max()[:19], datetime.now(),
                           f"{out['rows']} ticks to the lake this flush", conn=conn)
    except Exception as e:
        log.debug(f"  tick status: {e}")
    return out


def _status(conn, day, g, dropped):
    from db.schema import get_connection
    own = conn is None
    conn = conn or get_connection()
    try:
        conn.execute("INSERT INTO tick_capture_status (day,ticks,flushed,symbols,dropped,first_tick,last_tick,updated_at) "
                     "VALUES (?,?,1,?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET ticks=ticks+excluded.ticks, "
                     "flushed=flushed+1, symbols=MAX(symbols,excluded.symbols), dropped=dropped+excluded.dropped, "
                     "first_tick=MIN(first_tick,excluded.first_tick), last_tick=MAX(last_tick,excluded.last_tick), "
                     "updated_at=excluded.updated_at",
                     (day, len(g), int(g["symbol"].nunique()), dropped, g["received_at"].min()[:19],
                      g["received_at"].max()[:19], datetime.now()))
        conn.commit()
    finally:
        if own:
            conn.close()


def ticks(day, symbol=None, conn=None) -> pd.DataFrame:
    from data import lake
    df = lake.read("ticks", day, day, conn=conn)
    if symbol and not df.empty:
        df = df[df["symbol"] == symbol.upper()]
    return df


def build_minute_bars(day=None, conn=None) -> dict:
    from db.schema import get_connection
    day = str(day or date.today())[:10]
    df = ticks(day, conn=conn)
    if df.empty:
        return {"status": "EMPTY", "rows": 0, "day": day}
    df["t"] = pd.to_datetime(df["ltt"].fillna(df["received_at"].str[:19]), errors="coerce")
    df = df.dropna(subset=["t", "ltp"])
    df = df[df["t"].dt.strftime("%Y-%m-%d") == day]
    own = conn is None
    conn = conn or get_connection()
    n = 0
    try:
        for sym, g in df.sort_values("t").groupby("symbol"):
            g = g.set_index("t")
            o = g["ltp"].resample("1min")
            bars = pd.DataFrame({"open": o.first(), "high": o.max(), "low": o.min(), "close": o.last(),
                                 "cumv": g["cum_volume"].resample("1min").max()}).dropna(subset=["close"])
            bars["volume"] = bars["cumv"].diff().clip(lower=0)
            if len(bars):
                bars.iloc[0, bars.columns.get_loc("volume")] = 0
            for ts, b in bars.iterrows():
                conn.execute("INSERT INTO intraday_bars (symbol,ts,interval_min,open,high,low,close,volume,source,created_at) "
                             "VALUES (?,?,1,?,?,?,?,?, 'ticks', ?) ON CONFLICT(symbol,interval_min,ts) DO UPDATE SET "
                             "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, "
                             "volume=excluded.volume, source=excluded.source",
                             (sym, ts.strftime("%Y-%m-%d %H:%M:%S"), b["open"], b["high"], b["low"], b["close"],
                              int(b["volume"] or 0), datetime.now()))
                n += 1
        conn.execute("UPDATE tick_capture_status SET minute_bars=? WHERE day=?", (n, day))
        conn.commit()
        return {"status": "SUCCESS", "rows": n, "day": day, "symbols": int(df["symbol"].nunique())}
    finally:
        if own:
            conn.close()


def run_eod(trade_date=None) -> dict:
    """15:50: the day's ticks -> 1-minute bars (capture_ticks), then the REST fallback for the minutes still
    missing (minute_bars)."""
    out = {"status": "SKIPPED", "rows": 0, "day": str(trade_date or date.today())[:10]}
    if enabled():
        flush()
        out = build_minute_bars(trade_date)
    from data.stock_feed import settings
    s = settings()
    if s.get("minute_bars") is True and s.get("stocks_enabled"):
        fb = backfill_minute_bars(trade_date)
        out = {**out, "rest_fallback": fb, "rows": (out.get("rows") or 0) + fb.get("rows", 0),
               "status": "SUCCESS" if out.get("status") in ("SUCCESS", "SKIPPED", "EMPTY") else out.get("status")}
    return out


# ── 2026-10-31 (DP-03): live 1-minute bars from the stock feed, with a REST fallback ──────────────────
SESSION_OPEN_MIN, SESSION_CLOSE_MIN = 9 * 60 + 15, 15 * 60 + 30
BARS_SQL = ("INSERT INTO intraday_bars (symbol,ts,interval_min,open,high,low,close,volume,source,created_at) "
            "VALUES (?,?,1,?,?,?,?,?,?,?) ON CONFLICT(symbol,interval_min,ts) DO NOTHING")


class MinuteBars:
    """1-minute OHLCV per stock from the feed's Quote packets (LTP and the cumulative day volume).

        update(symbol, ltp, cum_volume, at)  one packet; `at` is the local receive time
        take_closed(now)                     every bar of a minute before now's minute, oldest first, as
                                             (symbol, "YYYY-MM-DD HH:MM:00", open, high, low, close, volume);
                                             volume = the cumulative volume's rise since the stock's previous
                                             bar (so a dropped packet loses no volume)
    A stock's first minute (after start or a reconnect: reset()) is partial: it seeds the volume and is not
    returned."""

    def __init__(self):
        self._lock = threading.Lock()
        self._bars = {}           # (symbol, minute) -> [open, high, low, close, last cum volume, partial]
        self._cum = {}            # symbol -> cumulative volume at the end of its previous bar
        self.built = 0

    def reset(self):
        with self._lock:
            self._bars.clear()
            self._cum.clear()

    def update(self, symbol: str, ltp: float, cum_volume: float, at: datetime):
        if not ltp or ltp <= 0:
            return
        minute = at.replace(second=0, microsecond=0)
        with self._lock:
            b = self._bars.get((symbol, minute))
            if b is None:
                partial = symbol not in self._cum and not any(k[0] == symbol for k in self._bars)
                self._bars[(symbol, minute)] = [ltp, ltp, ltp, ltp, float(cum_volume or 0), partial]
            else:
                b[1], b[2], b[3] = max(b[1], ltp), min(b[2], ltp), ltp
                b[4] = max(b[4], float(cum_volume or 0))

    def take_closed(self, now: datetime | None = None) -> list:
        cut = (now or datetime.now()).replace(second=0, microsecond=0)
        out = []
        with self._lock:
            for key in sorted((k for k in self._bars if k[1] < cut), key=lambda k: (k[1], k[0])):
                o, h, lo, c, cum, partial = self._bars.pop(key)
                sym, minute = key
                prev = self._cum.get(sym)
                self._cum[sym] = max(cum, prev or 0)
                if partial or prev is None:
                    continue
                out.append((sym, minute.strftime("%Y-%m-%d %H:%M:00"), o, h, lo, c, int(max(cum - prev, 0))))
            self.built += len(out)
        return out


def write_minute_bars(conn, bars: list, source: str = "ws") -> int:
    """Insert 1-minute bars; a minute already stored (by the feed, the fallback or the ticks build) is kept."""
    now = datetime.now()
    n = 0
    for sym, ts, o, h, lo, c, v in bars:
        cur = conn.execute(BARS_SQL, (sym, ts, o, h, lo, c, int(v or 0), source, now))
        n += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    conn.commit()
    return n


def expected_minutes(day: date, now: datetime | None = None) -> int:
    """Finished session minutes on `day` by `now` (375 for a whole session)."""
    now = now or datetime.now()
    if day < now.date():
        return SESSION_CLOSE_MIN - SESSION_OPEN_MIN
    if day > now.date():
        return 0
    m = now.hour * 60 + now.minute
    return max(0, min(m, SESSION_CLOSE_MIN) - SESSION_OPEN_MIN)


def backfill_minute_bars(day=None, symbols=None, conn=None, dhan=None, now=None, min_cover: float = 0.98) -> dict:
    """REST fallback: for each feed symbol with fewer 1-minute bars on `day` than min_cover x the finished session
    minutes, Dhan's intraday 1-minute bars fill the missing minutes (source 'dhan'). Dhan's Data APIs allow 5 calls a
    second; data.dhan paces every call."""
    from datetime import timedelta
    from db.schema import get_connection
    now = now or datetime.now()
    d = date.fromisoformat(str(day)[:10]) if day else now.date()
    want = expected_minutes(d, now)
    own = conn is None
    conn = conn or get_connection()
    out = {"status": "SUCCESS", "day": str(d), "rows": 0, "symbols": 0, "checked": 0, "failed": 0}
    try:
        if not symbols:
            from data.stock_feed import default_symbols, get_stock_feed, settings
            f = get_stock_feed()
            symbols = (list(f._sid_to_sym.values()) if f is not None and f._sid_to_sym else
                       settings().get("symbols") or default_symbols(conn))
        if want <= 0 or not symbols:
            out["status"] = "SKIPPED"
            return out
        have = {r[0]: r[1] for r in conn.execute(
            "SELECT symbol, COUNT(*) FROM intraday_bars WHERE interval_min=1 AND ts>=? AND ts<? GROUP BY symbol",
            (f"{d} 00:00:00", f"{d + timedelta(days=1)} 00:00:00"))}
        from data.dhan import fetch_historical_intraday
        for sym in symbols:
            out["checked"] += 1
            if have.get(sym, 0) >= want * min_cover:
                continue
            try:
                df = fetch_historical_intraday(sym, d - timedelta(days=1), d, interval=1, dhan=dhan)
            except Exception as e:
                out["failed"] += 1
                log.warning(f"  {sym} 1-min REST fallback: {e}")
                continue
            if df is None or df.empty:
                out["failed"] += 1
                continue
            bars = []
            for _, r in df.iterrows():
                t = r.get("datetime")
                if t is None or pd.isna(t) or pd.Timestamp(t).date() != d:
                    continue
                bars.append((sym, pd.Timestamp(t).strftime("%Y-%m-%d %H:%M:00"), r["open"], r["high"], r["low"],
                             r["close"], r.get("volume") or 0))
            n = write_minute_bars(conn, bars, source="dhan")
            out["rows"] += n
            out["symbols"] += 1 if n else 0
        return out
    finally:
        if own:
            conn.close()


def run_bars_fallback(now=None) -> dict:
    """Every 15 minutes in the session: the REST fallback, while the stock feed is not streaming (REST failover,
    parked, or not running). A no-op unless live_feed.minute_bars and live_feed.stocks_enabled are on."""
    from data.stock_feed import get_stock_feed, settings
    s = settings()
    if not (s.get("minute_bars") is True and s.get("stocks_enabled")):
        return {"status": "SKIPPED", "rows": 0, "reason": "live_feed.minute_bars is off"}
    f = get_stock_feed()
    if f is not None and f.mode == "WS":
        return {"status": "SKIPPED", "rows": 0, "reason": "the WebSocket is building the bars"}
    return backfill_minute_bars(now=now)
