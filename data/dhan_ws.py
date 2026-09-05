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
"""
import logging
import threading
import time
from datetime import date, datetime

from data.dhan import get_dhan_client, NSE_INDEX_SECURITY_IDS
from db.schema import get_connection

log = logging.getLogger(__name__)

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
        self._running = False

    # ── WebSocket lifecycle ────────────────────────────────────────────
    def start(self):
        if self._running:
            log.info("  Index WebSocket feed already running")
            return
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
        self._running = True
        self._feed.start()  # SDK spawns its own daemon thread for the WS loop
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._flush_thread.start()
        log.info(f"  ✓ Index WebSocket feed started — {len(instruments)} indexes subscribed")

    def stop(self):
        self._running = False
        if self._feed:
            try:
                self._feed.close_connection()
            except Exception as e:
                log.warning(f"  Feed close error: {e}")

    # ── Callbacks (called by the SDK's internal WS thread) ─────────────
    def _on_connect(self, instance):
        log.info("  Index WebSocket connected")

    def _on_error(self, instance, error):
        log.error(f"  Index WebSocket error: {error}")

    def _on_close(self, instance):
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
            try:
                self._flush_to_db()
            except Exception as e:
                log.error(f"  Index feed DB flush failed: {e}")

    def _flush_to_db(self):
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
