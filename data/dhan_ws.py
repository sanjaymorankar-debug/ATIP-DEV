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

2026-10-31 (DP-01 / MON-07), from the two errors of 2026-10-30:
  13:29:56  "no close frame received or sent": Dhan dropped the socket mid-session
            and the SDK reconnected and resubscribed by 13:29:58; index_levels kept
            its 15-second rows (13:29:53, 13:30:08). Not a fault in ATIP, but it
            was logged as an ERROR with nothing saying it recovered: it is now a
            WARNING, and the reconnect is logged with how long it took and counted.
  15:45:07  "sent 1000 (OK); no close frame received": ATIP's own close at the end
            of the window. The supervisor detaches the SDK instance and closes it;
            the SDK's read loop then reports the closed socket through on_error.
            Callbacks from an instance ATIP has detached are expected and logged
            at INFO, and never counted toward an error burst.
The feed also writes its state to live_feed_status (row 'indices'), like the stock
feed, so the health checks and the dashboard read every feed from one table.
"""
import logging
import re
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
# A connected-but-silent feed is recycled: indices tick constantly while the
# market is open, so this only fires when the socket is up and dead.
TICK_STALL_FROM    = _time(9, 20)   # after the pre-open auction settles
TICK_CLOSE         = _time(15, 30)
TICK_STALL_SECONDS = 600.0


def feed_window_open(now=None) -> bool:
    """Is the Dhan index feed worth holding open right now?"""
    now = now or datetime.now()
    if not (is_trading_day(now.date()) and FEED_OPEN <= now.time() <= FEED_CLOSE):
        return False
    try:                                     # Task 2: never inside the stop window / under a manual stop
        from pipeline.operating_schedule import is_stopped
        return not is_stopped()
    except Exception:
        return True


# A WebSocket URL carries the access token and client id as query parameters, and websockets puts the URL in
# some of its exceptions: nothing that reaches a log line, live_feed_status or the dashboard keeps them.
_URL_SECRET = re.compile(r"(?i)\b(token|access_token|access-token|clientId|client_id|client-id)=([^&\s'\"]+)")


def safe(text) -> str:
    """str(text) with credentials masked (ops.secrets.mask: tokens, JWTs, configured secret values)."""
    s = _URL_SECRET.sub(lambda m: f"{m.group(1)}=****", str(text))
    try:
        from ops.secrets import mask
        return mask(s)
    except Exception:
        return s


STATUS_COLUMNS = ("mode", "subscribed", "ticks", "last_tick_at", "last_flush_at", "detail")


def record_feed_status(feed: str, mode: str, subscribed=None, ticks=None, last_tick_at=None, last_flush_at=None,
                       detail: str = "", conn=None):
    """One live_feed_status row per feed (indices, stocks, ticks, minute_bars, depth20, option_chain), updated on
    each write. mode OFF means switched off in config.json -- written when ATIP starts, so OFF reads as OFF, not as
    a feed that never reported. A write without a last-data / last-flush time or an instrument count keeps the
    stored one, so a restart (or a feed switched off) still shows when the feed last had data, and for how many. Never raises: a status write must not take a
    feed down."""
    own = conn is None
    try:
        conn = conn or get_connection()
        conn.execute("INSERT INTO live_feed_status (feed,mode,subscribed,ticks,last_tick_at,last_flush_at,detail,"
                     "updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(feed) DO UPDATE SET mode=excluded.mode, "
                     "subscribed=COALESCE(excluded.subscribed, live_feed_status.subscribed), ticks=excluded.ticks, "
                     "last_tick_at=COALESCE(excluded.last_tick_at, live_feed_status.last_tick_at), "
                     "last_flush_at=COALESCE(excluded.last_flush_at, live_feed_status.last_flush_at), "
                     "detail=excluded.detail, updated_at=excluded.updated_at",
                     (feed, mode, subscribed, ticks, last_tick_at, last_flush_at, safe(detail)[:300], datetime.now()))
        conn.commit()
    except Exception as e:
        log.debug(f"  live_feed_status {feed}: {e}")
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass

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
        self._sdk_thread = None      # the SDK's own WS thread; dies on a failed connect
        self._flush_thread = None
        self._supervisor_thread = None
        self._running = False
        self._connected = False
        self._errors = deque()       # monotonic timestamps of recent feed errors
        self._cooldown_until = 0.0   # monotonic; no connect attempts before this
        self._connected_since = None # monotonic; when the current socket was opened
        self._backoff = BACKOFF_START
        self._dropped_at = None      # monotonic; a live socket Dhan dropped, until the SDK reconnects
        self._reconnects = 0         # mid-session drops the SDK recovered from, since start
        self._ticks = 0
        self._last_tick = None       # kept across disconnects (the snapshot is not), for the status row
        self._last_flush = None
        self.detail = ""
        self._reported = (None, 0.0) # (mode, monotonic) of the last live_feed_status write

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
        self._report(force=True)

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
            # Keep the thread: dhanhq's run() awaits its FIRST connect outside
            # its try block and catches only KeyboardInterrupt, so a refused
            # connect (429 at 09:00, a DNS blip) kills this thread while
            # self._feed stays bound -- a feed that looks alive and is not.
            self._sdk_thread = self._feed.start()
            self._connected_since = time.monotonic()
            log.info(f"  ✓ Index WebSocket feed started — {len(instruments)} indexes subscribed")
        except Exception as e:
            self._feed = None
            self._sdk_thread = None
            self._park(f"could not start feed: {safe(e)}")

    def _disconnect(self, why):
        feed, self._feed = self._feed, None
        self._sdk_thread = None
        self._connected = False
        self._connected_since = None
        self._dropped_at = None
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
        self.detail = f"parked {self._backoff / 60:.0f} min: {why}"
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
            if not self._running:        # stop() during the sleep: nothing more to decide, or to write
                break
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
        elif self._feed is not None and self._sdk_thread is not None \
                and not self._sdk_thread.is_alive():
            self._park("the SDK feed thread died (its first connect was refused)")
        elif self._feed is not None and self._stalled():
            self._park(f"connected but no index tick for over "
                       f"{TICK_STALL_SECONDS / 60:.0f} min during the session")
        elif self._feed is not None and self._error_burst():
            self._park(f"{len(self._errors)} errors in the last "
                       f"{ERROR_WINDOW:.0f}s (Dhan is refusing the connection)")
        elif self._feed is None and want and time.monotonic() >= self._cooldown_until:
            self._connect()
        self._report(want)

    @property
    def mode(self) -> str:
        """WS (connected), CONNECTING, RECONNECTING (Dhan dropped it, the SDK is reconnecting), PARKED, IDLE."""
        if self._feed is not None:
            return "RECONNECTING" if self._dropped_at else "WS" if self._connected else "CONNECTING"
        return "PARKED" if time.monotonic() < self._cooldown_until and feed_window_open() else "IDLE"

    def _report(self, want=None, force=False):
        """live_feed_status 'indices': on every change of mode, and once a minute while the window is open."""
        mode = self.mode
        last_mode, at = self._reported
        if not (force or mode != last_mode or (want and time.monotonic() - at >= 60)):
            return
        self._reported = (mode, time.monotonic())
        with self._lock:
            n = len(self._ltp)
        last = self._last_tick
        detail = (f"{n} of {len(self._sec_to_col)} indexes ticking" if mode == "WS" else
                  "outside 09:00-15:45 on a trading day" if mode == "IDLE" else self.detail or mode.lower())
        if self._reconnects:
            detail += f"; {self._reconnects} reconnect(s) today"
        record_feed_status("indices", mode, len(self._sec_to_col), self._ticks, last, self._last_flush, detail)

    def status(self) -> dict:
        with self._lock:
            n = len(self._ltp)
        return {"mode": self.mode, "subscribed": len(self._sec_to_col), "ticking": n, "ticks": self._ticks,
                "reconnects": self._reconnects, "last_flush_at": self._last_flush, "detail": self.detail,
                "window_open": feed_window_open()}

    def _error_burst(self) -> bool:
        cutoff = time.monotonic() - ERROR_WINDOW
        while self._errors and self._errors[0] < cutoff:
            self._errors.popleft()
        return len(self._errors) >= ERROR_BURST

    def _stalled(self, now=None) -> bool:
        """
        Connected, inside the ticking part of the session, and silent.

        NSE indices tick several times a second while the market is open, so a
        long silence means the socket is up but dead -- which otherwise shows up
        only as a missing afternoon in index_levels. Checked from TICK_STALL_FROM
        (after the pre-open auction) until the close, and never on the
        first tick-less minutes of a fresh connection.
        """
        now = now or datetime.now()
        if not (TICK_STALL_FROM <= now.time() <= TICK_CLOSE):
            return False
        with self._lock:
            last = max(self._last_tick_at.values()) if self._last_tick_at else None
        if last is not None:
            return (now - last).total_seconds() > TICK_STALL_SECONDS
        # Never ticked at all -- a subscription that was accepted and then went
        # nowhere. Measure from when this connection was opened instead, so the
        # silent case is caught too and not mistaken for "just connected".
        return (self._connected_since is not None
                and time.monotonic() - self._connected_since > TICK_STALL_SECONDS)

    # ── Callbacks (called by the SDK's internal WS thread) ─────────────
    def _detached(self, instance) -> bool:
        """A callback from an SDK instance ATIP has already let go of (closed at 15:45, parked, stopped).
        None means "the current one" (the callers that cannot tell)."""
        return instance is not None and instance is not self._feed

    def _on_connect(self, instance):
        if self._detached(instance):
            return
        if self._dropped_at is not None:
            self._reconnects += 1
            log.info(f"  Index WebSocket reconnected after {time.monotonic() - self._dropped_at:.0f}s "
                     f"(reconnect {self._reconnects} today)")
            self._dropped_at = None
        else:
            log.info("  Index WebSocket connected")
        self._connected = True
        self._errors.clear()
        self._backoff = BACKOFF_START

    def _on_error(self, instance, error):
        # 2026-10-30 15:45:07: the SDK reports the socket ATIP itself just closed as an error ("sent 1000 (OK);
        # no close frame received"). That is the close working, not a fault.
        if self._detached(instance):
            log.info(f"  Index WebSocket closed by ATIP ({safe(error)})")
            return
        # Record only; the supervisor decides whether to park. Logged at most
        # once per ERROR_WINDOW so a refusing endpoint can't fill the log.
        first = not self._errors
        self._errors.append(time.monotonic())
        if first and self._connected:
            # 2026-10-30 13:29:56: a live socket Dhan dropped. The SDK reconnects and resubscribes by itself
            # (13:29:58 that day, no index row lost); _on_connect logs how long it took.
            self._dropped_at = time.monotonic()
            self._connected = False
            log.warning(f"  Index WebSocket dropped by the server ({safe(error)}) — the SDK is reconnecting")
        elif first or len(self._errors) == ERROR_BURST:
            log.error(f"  Index WebSocket error: {safe(error)}")

    def _on_close(self, instance):
        if self._detached(instance):
            log.info("  Index WebSocket closed by ATIP")
            return
        self._connected = False
        log.warning("  Index WebSocket closed")

    def _on_message(self, instance, data):
        # Not every frame is a tick dict: the SDK hands back a bare string for
        # Dhan's market-status frame (response code 7 -> "Markets Open"), which
        # arrives per subscribed instrument at the 09:15 open. data.get() on a
        # str raises AttributeError, and the SDK calls on_error from the same
        # except clause it uses for a dropped socket -- so 14 status frames used
        # to look like 14 connection errors and, past ERROR_BURST, parked a
        # perfectly healthy feed at the open.
        if not isinstance(data, dict) or not data:
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
                    self._last_tick_at[sec_id] = self._last_tick = datetime.now()
                    self._ticks += 1
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
            if not self._running:
                break
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
        try:                                    # W34 (EX-15): age of the newest tick at flush time
            from ops.latency import record as _lat
            with self._lock:
                newest = max(self._last_tick_at.values()) if self._last_tick_at else None
            if newest:
                _lat("feed.tick_to_flush", (datetime.now() - newest).total_seconds() * 1000.0)
        except Exception:
            pass
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
        try:
            # (date, time) is unique. OR IGNORE: a row already stamped this second
            # is either our own or markets.py's fuller snapshot, and neither
            # should be overwritten by this lighter one.
            conn.execute(
                f"INSERT OR IGNORE INTO index_levels ({','.join(cols)}) VALUES ({placeholders})",
                [record[c] for c in cols],
            )
            conn.commit()
            self._last_flush = datetime.now()
        finally:
            conn.close()          # W39: this flush (every 15 s) never closed its connection
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
