"""
Stock-level live feed with failover (DP-01), W27.

The index feed (data/dhan_ws.py) streams indices only; stock prices reached ATIP
through the 15-minute REST poll. StockFeedManager streams Dhan Quote packets for a
bounded symbol set and writes the latest quote per symbol to live_quotes on a timer.

    MODES   WS        Dhan WebSocket (MarketFeed, NSE segment, Quote packets: LTP, OHLC,
                      volume, LTT); prev close from the "Previous Close" packet
            REST      failover: Dhan quote_data() every rest_seconds when the socket is
                      parked, its SDK thread died, or it went silent mid-session
            IDLE      outside the session window (same window as the index feed)

    SYMBOLS live_feed.symbols in config.json when given; otherwise LIVE holdings + open
            PAPER positions + pending order-rule symbols + the latest top-25 ATIP
            scores, capped at live_feed.max_symbols (default 100).

    STATUS  live_feed_status row 'stocks' (mode, subscribed, ticks, last tick / flush)
            and status() for /api/market/feed-status.

OFF BY DEFAULT: config.json  "live_feed": {"stocks_enabled": true, "max_symbols": 100,
"flush_seconds": 60, "rest_seconds": 60, "symbols": []}. Supervision mirrors the index
feed: connect only inside the window, park with doubling backoff after an error
burst, recycle a silent socket -- and, unlike the index feed, fall back to REST
instead of leaving prices stale while parked. It never places orders.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, time as _time
from pathlib import Path

from data.dhan_ws import (BACKOFF_MAX, BACKOFF_START, ERROR_BURST, ERROR_WINDOW, SUPERVISOR_TICK,
                          feed_window_open)

log = logging.getLogger(__name__)

DEFAULTS = {"stocks_enabled": False, "max_symbols": 100, "flush_seconds": 60, "rest_seconds": 60, "symbols": []}
TICK_FROM, TICK_TO = _time(9, 16), _time(15, 30)
STALL_SECONDS = 300.0

try:
    from dhanhq.marketfeed import MarketFeed
    HAS_MARKETFEED = True
except ImportError:          # pragma: no cover
    MarketFeed = None
    HAS_MARKETFEED = False


def settings() -> dict:
    try:
        cfg = json.loads((Path("atip_data") / "config.json").read_text(encoding="utf-8"))
        return {**DEFAULTS, **(cfg.get("live_feed") or {})}
    except Exception:
        return dict(DEFAULTS)


def default_symbols(conn, cap=100) -> list:
    syms = []

    def add(rows):
        for r in rows:
            s = str(r[0]).upper()
            if s and s not in syms:
                syms.append(s)
    for sql in ("SELECT symbol FROM portfolio_holdings WHERE date=(SELECT MAX(date) FROM portfolio_holdings)",
                "SELECT symbol FROM paper_position WHERE quantity<>0",
                "SELECT DISTINCT symbol FROM order_rules WHERE status IN ('ACTIVE','PENDING','TRIGGERED')",
                "SELECT symbol FROM ai_scores WHERE date=(SELECT MAX(date) FROM ai_scores) "
                "ORDER BY atip_score DESC LIMIT 25"):
        try:
            add(conn.execute(sql).fetchall())
        except Exception:
            continue
    return syms[:cap]


class StockFeedManager:
    def __init__(self, symbols=None, cfg=None):
        self.cfg = {**DEFAULTS, **(cfg or settings())}
        self._symbols_req = symbols
        self._lock = threading.Lock()
        self._quotes = {}             # symbol -> dict
        self._sid_to_sym = {}
        self._feed = None
        self._sdk_thread = None
        self._running = False
        self._errors = deque()
        self._cooldown_until = 0.0
        self._backoff = BACKOFF_START
        self._connected_since = None
        self._last_tick = None
        self._last_rest = 0.0
        self._last_flush = None
        self._ticks = 0
        self.mode = "IDLE"
        self.detail = ""

    # ── symbols ──
    def _resolve(self):
        from data.dhan import get_security_id
        syms = self._symbols_req or self.cfg.get("symbols") or None
        if not syms:
            from db.schema import get_connection
            c = get_connection()
            try:
                syms = default_symbols(c, int(self.cfg.get("max_symbols") or 100))
            finally:
                c.close()
        m = {}
        for s in syms[: int(self.cfg.get("max_symbols") or 100)]:
            sec = get_security_id(s)
            if sec:
                m[int(sec["security_id"])] = s
        self._sid_to_sym = m
        return m

    # ── lifecycle ──
    def start(self):
        if self._running:
            return
        self._running = True
        threading.Thread(target=self._supervise_loop, daemon=True, name="stock-feed-supervisor").start()
        threading.Thread(target=self._flush_loop, daemon=True, name="stock-feed-flush").start()
        log.info("  ✓ Stock live feed supervised (WebSocket, REST failover)")

    def stop(self):
        self._running = False
        self._disconnect("stop() called")

    def _connect(self):
        if not HAS_MARKETFEED:
            self._park("dhanhq marketfeed not installed")
            return
        try:
            from data.dhan import get_dhan_client
            sids = self._resolve()
            if not sids:
                self.detail = "no symbols resolved to Dhan security ids"
                return
            _, ctx = get_dhan_client()
            instruments = [(MarketFeed.NSE, str(sid), MarketFeed.Quote) for sid in sids]
            self._feed = MarketFeed(ctx, instruments, version="v2", on_connect=self._on_connect,
                                    on_message=self._on_message, on_close=self._on_close, on_error=self._on_error)
            self._errors.clear()
            self._sdk_thread = self._feed.start()
            self._connected_since = time.monotonic()
            self.mode = "WS"
            self.detail = f"{len(instruments)} symbols subscribed"
            log.info(f"  ✓ Stock WebSocket feed started — {len(instruments)} symbols")
        except Exception as e:
            self._feed = None
            self._park(f"could not start: {e}")

    def _disconnect(self, why):
        feed, self._feed = self._feed, None
        self._sdk_thread = None
        self._connected_since = None
        if feed is not None:
            try:
                feed.close_connection()
            except Exception as e:
                log.warning(f"  stock feed close: {e}")
            log.info(f"  Stock WebSocket disconnected — {why}")

    def _park(self, why):
        self._disconnect(why)
        self._cooldown_until = time.monotonic() + self._backoff
        self.mode = "REST" if feed_window_open() else "IDLE"
        self.detail = f"parked {self._backoff / 60:.0f} min: {why}"
        log.warning(f"  ⚠ Stock feed parked ({self.detail}) — REST failover")
        self._backoff = min(self._backoff * 2, BACKOFF_MAX)

    def _stalled(self) -> bool:
        now = datetime.now()
        if not (TICK_FROM <= now.time() <= TICK_TO):
            return False
        if self._last_tick is not None:
            return (now - self._last_tick).total_seconds() > STALL_SECONDS
        return self._connected_since is not None and time.monotonic() - self._connected_since > STALL_SECONDS

    def _error_burst(self) -> bool:
        cut = time.monotonic() - ERROR_WINDOW
        while self._errors and self._errors[0] < cut:
            self._errors.popleft()
        return len(self._errors) >= ERROR_BURST

    def _supervise_once(self):
        want = feed_window_open()
        if not want:
            if self._feed is not None:
                self._disconnect("market closed")
            self._backoff = BACKOFF_START
            self.mode = "IDLE"
            return
        if self._feed is not None and self._sdk_thread is not None and not self._sdk_thread.is_alive():
            self._park("SDK feed thread died")
        elif self._feed is not None and self._stalled():
            self._park("connected but silent")
        elif self._feed is not None and self._error_burst():
            self._park(f"{len(self._errors)} errors in {ERROR_WINDOW:.0f}s")
        elif self._feed is None and time.monotonic() >= self._cooldown_until:
            self._connect()
        if self._feed is None:
            self.mode = "REST"
            if time.monotonic() - self._last_rest >= float(self.cfg.get("rest_seconds") or 60):
                self._rest_poll()

    def _supervise_loop(self):
        self._supervise_once()
        while self._running:
            time.sleep(min(SUPERVISOR_TICK, float(self.cfg.get("rest_seconds") or 60)))
            try:
                self._supervise_once()
            except Exception as e:
                log.error(f"  stock feed supervisor: {e}")

    def _rest_poll(self):
        self._last_rest = time.monotonic()
        try:
            from data.dhan import fetch_live_quotes
            syms = list(self._sid_to_sym.values()) or list(self._resolve().values())
            df = fetch_live_quotes(syms)
            with self._lock:
                for _, r in df.iterrows():
                    self._quotes[r["symbol"]] = {"ltp": r.get("ltp"), "open": r.get("open"), "high": r.get("high"),
                                                 "low": r.get("low"), "prev_close": r.get("prev_close"),
                                                 "volume": r.get("volume"), "src": "rest"}
        except Exception as e:
            self.detail = f"REST failover failed: {e}"

    # ── SDK callbacks ──
    def _on_connect(self, instance):
        self._errors.clear()
        self._backoff = BACKOFF_START

    def _on_error(self, instance, error):
        first = not self._errors
        self._errors.append(time.monotonic())
        if first:
            log.error(f"  Stock WebSocket error: {error}")

    def _on_close(self, instance):
        log.warning("  Stock WebSocket closed")

    def _on_message(self, instance, data):
        if not isinstance(data, dict):
            return
        try:
            sym = self._sid_to_sym.get(int(data.get("security_id")))
        except (TypeError, ValueError):
            return
        if not sym:
            return
        with self._lock:
            q = self._quotes.setdefault(sym, {})
            if data.get("type") == "Previous Close":
                try:
                    q["prev_close"] = float(data["prev_close"])
                except (KeyError, TypeError, ValueError):
                    pass
            elif data.get("type") == "Quote Data":
                try:
                    q.update(ltp=float(data["LTP"]), open=float(data["open"]), high=float(data["high"]),
                             low=float(data["low"]), volume=int(data["volume"]), src="ws")
                    self._ticks += 1
                    self._last_tick = datetime.now()
                except (KeyError, TypeError, ValueError):
                    pass

    # ── flush ──
    def snapshot(self) -> dict:
        with self._lock:
            return {s: dict(q) for s, q in self._quotes.items() if q.get("ltp")}

    def _flush_loop(self):
        while self._running:
            time.sleep(float(self.cfg.get("flush_seconds") or 15))
            try:
                self._flush()
            except Exception as e:
                log.error(f"  stock feed flush: {e}")

    def _flush(self):
        from db.schema import get_connection
        snap = self.snapshot() if feed_window_open() else {}
        conn = get_connection()
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for s, q in snap.items():
                prev = q.get("prev_close")
                chg = round((q["ltp"] - prev) / prev * 100, 2) if prev else None
                conn.execute("INSERT OR REPLACE INTO live_quotes (symbol,ltp,open,high,low,prev_close,volume,chg_pct,"
                             "timestamp,source) VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (s, q["ltp"], q.get("open"), q.get("high"), q.get("low"), prev, q.get("volume"), chg, ts,
                              f"dhan_{q.get('src', 'ws')}"))
            if snap:
                self._last_flush = datetime.now()
            conn.execute("INSERT OR REPLACE INTO live_feed_status (feed,mode,subscribed,ticks,last_tick_at,last_flush_at,"
                         "detail,updated_at) VALUES ('stocks',?,?,?,?,?,?,?)",
                         (self.mode, len(self._sid_to_sym), self._ticks, self._last_tick, self._last_flush,
                          self.detail, datetime.now()))
            conn.commit()
        finally:
            conn.close()

    def status(self) -> dict:
        return {"mode": self.mode, "subscribed": len(self._sid_to_sym), "ticks": self._ticks,
                "last_tick_at": self._last_tick, "last_flush_at": self._last_flush, "detail": self.detail,
                "window_open": feed_window_open()}


_manager = None
_mlock = threading.Lock()


def start_stock_feed(force=False):
    """Started by main.py next to the index feed when live_feed.stocks_enabled (or force)."""
    global _manager
    if not (force or settings().get("stocks_enabled")):
        return None
    with _mlock:
        if _manager is None:
            _manager = StockFeedManager()
            _manager.start()
        return _manager


def get_stock_feed():
    return _manager


def stop_stock_feed():
    global _manager
    with _mlock:
        if _manager is not None:
            _manager.stop()
            _manager = None


def feed_status(conn=None) -> dict:
    if _manager is not None:
        return {"running": True, **_manager.status()}
    out = {"running": False, "enabled": bool(settings().get("stocks_enabled"))}
    if conn is not None:
        r = conn.execute("SELECT * FROM live_feed_status WHERE feed='stocks'").fetchone()
        if r:
            out["last"] = dict(r)
    return out
