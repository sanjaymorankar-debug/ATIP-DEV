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
    flush()
    return build_minute_bars(trade_date)
