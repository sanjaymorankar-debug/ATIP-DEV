"""
ATIP — Real-time NSE Index Feed via Dhan WebSocket (Live Market Feed)

Replaces the 15-min REST poll (data.markets.fetch_intraday_indexes) with
a persistent WebSocket connection for near-real-time index updates, per
Dhan's documented Live Market Feed API:

    wss://api-feed.dhan.co?version=2&token=...&clientId=...&authType=2

We use the dhanhq SDK's own `marketfeed.MarketFeed` class rather than
hand-rolling the binary protocol — its URL construction, request codes,
and packet byte-layouts were checked line-by-line against Dhan's docs and
match exactly (response code 2 = Ticker, 4 = Quote, 6 = Previous Close,
8 = Full).

Packet types subscribed:
  - RequestCode 15 (Ticker) → response code 2: LTP + LTT only (lightest).
    Indices don't need volume/depth/OI, so Ticker is enough — no reason to
    pay the bandwidth/parsing cost of Quote (17) or Full (21) packets.
  - Previous Close (response code 6) is sent automatically by Dhan right
    after subscribing to ANY packet type for an instrument — it is not
    something you request separately. This is the ONLY reliable source for
    prior-day close over the WebSocket: the Quote/Full packet's own "close"
    field is documented by Dhan as "Day Close Value — only sent post market
    close", i.e. it's not populated with yesterday's close during live
    trading hours, unlike the REST /marketfeed/ohlc endpoint where
    ohlc.close IS always previous close. Mixing these two up would silently
    reintroduce the exact 0.00%-forever bug we already fixed once.

DB writes are throttled (default: every 15s), not on every tick — NSE
indices can tick multiple times a second, and writing every tick to SQLite
would be pure disk I/O with no dashboard-visible benefit. The in-memory
snapshot is still updated on every tick if you want lower-latency access
later (e.g. a polling JSON endpoint) via IndexFeedManager.snapshot().

The connection is only held DURING the session (see FEED_OPEN/FEED_CLOSE),
and a burst of failures parks it for a while instead of retrying forever.
The SDK's own loop reconnects every second with no backoff and no ceiling
(dhanhq marketfeed._run_async), so a feed left up outside market hours ran
~3,300 rejected connections an hour — 24h of HTTP 429 over one weekend,
two log lines each (the SDK reports a failed connect twice), which is what
filled atip.log with 35 MB of noise and most likely got the account
throttled. Dhan has nothing to stream when the market is shut, so the fix
is to not be connected then.
"""
import logging
import threading
import time
from collections import deque
from datetime import date, datetime, time as _time

from data.dhan import get_dhan_client, NSE_INDEX_SECURITY_IDS
from db.schema import get_connection
from utils.trading_calendar import is_trading_day

log = logging.getLogger(__name__)

# Hold the connection from the pre-open auction until a little past the close,
# so the last ticks and the post-close snapshot still land.
FEED_OPEN  = _time(9, 0)
FEED_CLOSE = _time(15, 45)

SUPERVISOR_TICK  = 30.0    # how often the window/health check runs
ERROR_BURST      = 10      # errors within ERROR_WINDOW that mean "stop asking"
ERROR_WINDOW     = 60.0
BACKOFF_START    = 300.0   # 5 min after a burst, doubling
BACKOFF_MAX      = 1800.0  # ...to 30 min


def feed_window_open(now=None) -> bool:
    """Is the Dhan index feed worth holding open right now?"""
    now = now or datetime.now()
    return is_trading_day(now.date()) and FEED_OPEN <= now.time() <= FEED_CLOSE

try:
    from dhanhq.marketfeed import MarketFeed
    HAS_MARKETFEED = True
except ImportError:
    HAS_MARKETFEED = False


class IndexFeedManager:
    """
    Maintains a persistent Dhan WebSocket subscription to every index in
    NSE_INDEX_SECURITY_IDS and keeps a thread-safe in-memory snapshot,
    flushed to the index_levels DB table on a timer — so the existing
    dashboard code (dashboard/server.py: get_indexes()) keeps working
    completely unchanged, just fed much more frequently than the old
    15-min REST poll.
    """

    def __init__(self, flush_interval: float = 15.0):
        if not HAS_MARKETFEED:
            raise RuntimeError("pip install dhanhq  (WebSocket feed needs dhanhq>=2.0)")
        self.flush_interval = flush_interval
        self._lock = threading.Lock()
        self._prev_close = {}    # security_id (int) -> float
        self._ltp = {}           # security_id (int) -> float
        self._last_tick_at = {}  # security_id (int) -> datetime
        self._sec_to_col = {int(v): k for k, v in NSE_INDEX_SECURITY_IDS.items()}
        self._feed = None
        self._flush_thread = None
        self._supervisor_thread = None
        self._running = False
        self._connected = False
        self._errors = deque()       # monotonic timestamps of recent feed errors
        self._cooldown_until = 0.0   # monotonic; no connect attempts before this
        self._backoff = BACKOFF_START

    # ── WebSocket lifecycle ────────────────────────────────────────────
    def start(self):
        """
        Begin supervising the feed. The connection itself is opened only inside
        the session window, by the supervisor, so this is safe to call at any
        hour — including from a process that starts at night.
        """
        if self._running:
            log.info("  Index WebSocket feed already running")
            return
        self._running = True
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._flush_thread.start()
        self._supervisor_thread = threading.Thread(target=self._supervise_loop, daemon=True)
        self._supervisor_thread.start()
        if feed_window_open():
            self._connect()
        else:
            log.info(f"  ✓ Index WebSocket feed supervised — market closed, connecting at "
                     f"{FEED_OPEN.strftime('%H:%M')} on the next trading day")

    def stop(self):
        self._running = False
        self._disconnect("stop() called")

    # ── Connect / disconnect (only ever called from ONE thread: the
    #    supervisor, or start()/stop() before it matters) ───────────────
    def _connect(self):
        try:
            dhan, ctx = get_dhan_client()
            # MarketFeed.IDX == 0 (maps to "IDX_I" internally); Ticker == 15
            instruments = [(MarketFeed.IDX, str(sid), MarketFeed.Ticker)
                            for sid in self._sec_to_col.keys()]
            self._feed = MarketFeed(
                ctx, instruments, version="v2",
                on_connect=self._on_connect,
                on_message=self._on_message,
                on_close=self._on_close,
                on_error=self._on_error,
            )
            self._errors.clear()
            self._feed.start()  # SDK spawns its own daemon thread for the WS loop
            log.info(f"  ✓ Index WebSocket feed started — {len(instruments)} indexes subscribed")
        except Exception as e:
            self._feed = None
            self._park(f"could not start feed: {e}")

    def _disconnect(self, why):
        feed, self._feed = self._feed, None
        self._connected = False
        # Drop the snapshot: without this the last tick stays "current" and the
        # flush loop keeps writing it. That is how index_levels collected 7,583
        # rows stamped 2026-09-19/20 -- a Saturday and a Sunday -- all holding
        # Friday's 23,346.4 close and a BULLISH sentiment, one every 15 seconds.
        with self._lock:
            self._ltp.clear()
            self._prev_close.clear()
            self._last_tick_at.clear()
        if feed is None:
            return
        try:
            feed.close_connection()   # sets the SDK's _running False, ending its retry loop
        except Exception as e:
            log.warning(f"  Feed close error: {e}")
        log.info(f"  Index WebSocket feed disconnected — {why}")

    def _park(self, why):
        """Stop asking for a while, then let the supervisor try again."""
        self._disconnect(why)
        self._cooldown_until = time.monotonic() + self._backoff
        log.warning(f"  ⚠ Index WebSocket parked for {self._backoff / 60:.0f} min — {why}")
        self._backoff = min(self._backoff * 2, BACKOFF_MAX)

    def _supervise_loop(self):
        """
        Owns the connection: holds it open during the session, drops it when the
        session ends, and parks it after a burst of errors. All state changes
        happen here so the SDK's callback thread never races with them.
        """
        while self._running:
            time.sleep(SUPERVISOR_TICK)
            try:
                self._supervise_once()
            except Exception as e:
                log.error(f"  Index feed supervisor error: {e}")

    def _supervise_once(self):
        """One lifecycle decision: hold, drop, park, or (re)connect."""
        want = feed_window_open()
        if self._feed is not None and not want:
            self._disconnect("market closed")
            self._backoff = BACKOFF_START
        elif self._feed is not None and self._error_burst():
            self._park(f"{len(self._errors)} errors in the last "
                       f"{ERROR_WINDOW:.0f}s (Dhan is refusing the connection)")
        elif self._feed is None and want and time.monotonic() >= self._cooldown_until:
            self._connect()

    def _error_burst(self) -> bool:
        cutoff = time.monotonic() - ERROR_WINDOW
        while self._errors and self._errors[0] < cutoff:
            self._errors.popleft()
        return len(self._errors) >= ERROR_BURST

    # ── Callbacks (called by the SDK's internal WS thread) ─────────────
    def _on_connect(self, instance):
        log.info("  Index WebSocket connected")
        self._connected = True
        self._errors.clear()
        self._backoff = BACKOFF_START

    def _on_error(self, instance, error):
        # Record only; the supervisor decides whether to park. Logged at most
        # once per ERROR_WINDOW so a refusing endpoint can't fill the log.
        first = not self._errors
        self._errors.append(time.monotonic())
        if first or len(self._errors) == ERROR_BURST:
            log.error(f"  Index WebSocket error: {error}")

    def _on_close(self, instance):
        self._connected = False
        log.warning("  Index WebSocket closed")

    def _on_message(self, instance, data):
        if not data:
            return
        try:
            sec_id = int(data.get("security_id"))
        except (TypeError, ValueError):
            return
        col = self._sec_to_col.get(sec_id)
        if not col:
            return
        with self._lock:
            if data.get("type") == "Previous Close":
                try:
                    self._prev_close[sec_id] = float(data["prev_close"])
                except (KeyError, TypeError, ValueError):
                    pass
            elif data.get("type") == "Ticker Data":
                try:
                    self._ltp[sec_id] = float(data["LTP"])
                    self._last_tick_at[sec_id] = datetime.now()
                except (KeyError, TypeError, ValueError):
                    pass

    # ── Snapshot / DB flush ─────────────────────────────────────────────
    def snapshot(self) -> dict:
        """{col: {"ltp":..., "prev_close":..., "chg_pct":...}} for every
        index that has received at least one LTP tick so far."""
        out = {}
        with self._lock:
            for sec_id, col in self._sec_to_col.items():
                ltp = self._ltp.get(sec_id)
                prev = self._prev_close.get(sec_id)
                if ltp is None:
                    continue
                chg = None
                if prev:
                    try:
                        chg = round((ltp - prev) / prev * 100, 3)
                    except ZeroDivisionError:
                        chg = None
                out[col] = {"ltp": ltp, "prev_close": prev, "chg_pct": chg}
        return out

    def _flush_loop(self):
        while self._running:
            time.sleep(self.flush_interval)
            if self._feed is None:
                continue  # not connected: nothing is ticking, so nothing to store
            try:
                self._flush_to_db()
            except Exception as e:
                log.error(f"  Index feed DB flush failed: {e}")

    def _flush_to_db(self):
        if not feed_window_open():
            return  # never stamp a row on a day (or an hour) with no session
        snap = self.snapshot()
        if not snap:
            return  # no ticks yet (e.g. market closed) — nothing to write
        record = {"date": str(date.today()), "time": datetime.now().strftime("%H:%M:%S")}
        for col, vals in snap.items():
            record[col] = vals["ltp"]
            record[f"{col}_chg"] = vals["chg_pct"] if vals["chg_pct"] is not None else 0
        # This feed only sees index LTPs (not full market breadth like markets.py),
        # so derive a lightweight sentiment from Nifty's own move rather than
        # leaving the column NULL — a NULL here overwrites the proper reading
        # from the 15-min markets.py job since this flush runs far more often.
        nifty_chg = record.get("nifty50_chg")
        if nifty_chg is not None:
            record["overall_sentiment"] = (
                "BULLISH" if nifty_chg > 0.3 else
                "BEARISH" if nifty_chg < -0.3 else "NEUTRAL"
            )
        cols = list(record.keys())
        placeholders = ",".join("?" * len(cols))
        conn = get_connection()
        conn.execute(
            f"INSERT INTO index_levels ({','.join(cols)}) VALUES ({placeholders})",
            [record[c] for c in cols],
        )
        conn.commit()
        log.debug(f"  Index feed flush → {len(snap)} indexes @ {record['time']}")


_manager = None
_manager_lock = threading.Lock()


def start_index_feed(flush_interval: float = 15.0) -> "IndexFeedManager":
    """Idempotent singleton starter — safe to call more than once."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = IndexFeedManager(flush_interval=flush_interval)
            _manager.start()
        return _manager


def get_index_feed_manager():
    return _manager


def stop_index_feed():
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.stop()
            _manager = None
