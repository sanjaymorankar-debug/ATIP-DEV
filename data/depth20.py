"""
W39 Phase 3 (item 2, DP-01..03, OF-01..03) — Dhan's 20-level market depth, a depth-weighted imbalance (DWI)
and the order-flow imbalance (OFI) for the watchlist, with a test of whether they predict anything.

    feed      wss://depth-api-feed.dhan.co/twentydepth?token=..&clientId=..&authType=2 (Dhan Data API), up to
              50 instruments per connection, subscribed with {"RequestCode": 23, "InstrumentCount": n,
              "InstrumentList": [{"ExchangeSegment": "NSE_EQ", "SecurityId": "1333"}]}
    frames    one binary frame carries one or more messages: a 12-byte little-endian header <hBBiI (message
              length, code 41 bid / 51 ask / 50 disconnect, exchange segment, security id, sequence -- or the
              disconnect reason, 805-809) and 20 levels of <dII (price, quantity, orders). The layout is the one
              in dhanhq's fulldepth.py (checked 2026-10-07); bids and asks arrive as separate messages.
    measures  dwi      the levels within "band_bp" (50 bp) of the mid, level k weighted e^(-0.5 (k-1)):
                       (sum w * bid qty - sum w * ask qty) / (sum w * bid qty + sum w * ask qty), -1..1
              imb_l1   the best level only;  imb_20  all 20 levels, unweighted -- stored beside the DWI so the
                       record can say which of them, if any, carries information
    OFI       OF-01, order-flow imbalance from quote changes, summed over EVERY book update (each bid or ask
              message), not only the sampled ones. For consecutive books n-1 -> n, at the best level (Cont,
              Kukanov & Stoikov 2014) and at the m-th best level (Xu, Gould & Howison 2019, multi-level OFI):
                e_n = 1{Pb_n >= Pb_n-1} qb_n - 1{Pb_n <= Pb_n-1} qb_n-1
                      - 1{Pa_n <= Pa_n-1} qa_n + 1{Pa_n >= Pa_n-1} qa_n-1
              (bids arriving or the bid raised count as buying; asks arriving or the ask lowered as selling)
              ofi_l1     the sum of e_n at the best level since the stock's previous snapshot, in shares
              ofi_depth  the average best-level depth over that interval, (qb + qa) / 2: ofi_l1 / ofi_depth is
                         the scale-free OFI (CKS: OFI's price impact is inversely proportional to depth)
              ofi_ml     the multi-level OFI at levels 1..ofi_levels (20): each level's sum divided by the average
                         depth per level and side over the interval (Cont, Cucuringu & Zhang's scaling, so stocks
                         compare), integrated with weights e^(-ofi_decay (m-1)) (0, the default: equal weights).
                         Levels are ranks, so a best quote moving a tick shifts every level and counts at each:
                         ofi_ml follows the interval's price move closely by construction
    storage   OF-02, the snapshot also keeps the best bid / ask price and quantity, and ofi_updates (the book
              updates summed). A new connection restarts the sums: the first snapshot after it has no interval
              behind it, so its OFI is empty (NULL). Tables from before get the columns (ADDED_COLUMNS).
    sampling  one snapshot per stock every "sample_seconds" (15), written every 10 seconds (depth20_snapshot)
    flag      persistent(): |DWI| > 0.3 in each of a stock's last 3 snapshots -> BUYERS / SELLERS
    validate  logistic regression of the direction of the mid's next 1- or 5-minute move on each measure:
              n, slope, z, and how often the sign was right. A measure is called predictive only with 500+
              snapshots and |z| >= 2. The research expectation (NSE studies): predictive for minutes at most.
              OF-03, OFI is also fitted against the mid's move over its own interval (previous snapshot -> this
              one), the strong contemporaneous relation CKS report (R^2 ~ 65 % on US stocks): the same logistic
              rule ("related"), plus CKS's linear fit stock by stock (median bp per unit of OFI, median R^2) --
              a sanity check of the data.

The feed is OFF by default (config.json "depth20": {"enabled": false, "symbols": [], "max_symbols": 50,
"sample_seconds": 15, "band_bp": 50, "decay": 0.5, "ofi_levels": 20, "ofi_decay": 0}); it needs the Dhan Data
API. It runs on trading days 09:00-15:45, reconnects with backoff, and parks on Dhan's refusals (805 too many
connections, 806 no Data API subscription, 807 token expired, 808 bad client id, 809 authentication failed),
saying which.
Symbols: config, else data/stock_feed.default_symbols (holdings, open positions, active rules, top ATIP
scores). Context for minutes, not a signal: the page states it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import struct
import threading
import time as _time
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

URL = "wss://depth-api-feed.dhan.co/twentydepth"
REQUEST_SUBSCRIBE = 23
HEADER, LEVEL = struct.Struct("<hBBiI"), struct.Struct("<dII")
BID, ASK, DISCONNECT = 41, 51, 50
REFUSALS = {805: "too many active WebSocket connections", 806: "subscribe to Dhan's Data APIs to continue",
            807: "access token expired", 808: "invalid client id", 809: "authentication failed"}
BATCH = 50
DEFAULTS = {"enabled": False, "symbols": [], "max_symbols": 50, "sample_seconds": 15, "band_bp": 50.0, "decay": 0.5,
            "flag_dwi": 0.3, "flag_snapshots": 3, "ofi_levels": 20, "ofi_decay": 0.0}
MIN_VALIDATE, Z_MIN = 500, 2.0
OFI_MEASURES = ("ofi_l1", "ofi_ml")
ROW = ("mid", "spread_bp", "dwi", "imb_l1", "imb_20", "bid_qty", "ask_qty", "levels", "best_bid", "best_bid_qty",
       "best_ask", "best_ask_qty", "ofi_l1", "ofi_ml", "ofi_depth", "ofi_updates")      # a snapshot, after (symbol, ts)

DDL = (
    """CREATE TABLE IF NOT EXISTS depth20_snapshot (
        symbol TEXT NOT NULL, ts TIMESTAMP NOT NULL, mid REAL, spread_bp REAL, dwi REAL, imb_l1 REAL, imb_20 REAL,
        bid_qty REAL, ask_qty REAL, levels INTEGER, best_bid REAL, best_bid_qty REAL, best_ask REAL, best_ask_qty REAL,
        ofi_l1 REAL, ofi_ml REAL, ofi_depth REAL, ofi_updates INTEGER, PRIMARY KEY (symbol, ts))""",
    "CREATE INDEX IF NOT EXISTS idx_depth20_ts ON depth20_snapshot(ts)",
)
# OF-02, columns added after DP-01 shipped: the best quotes and the order-flow imbalance
ADDED_COLUMNS = {"depth20_snapshot": {"best_bid": "REAL", "best_bid_qty": "REAL", "best_ask": "REAL",
                                      "best_ask_qty": "REAL", "ofi_l1": "REAL", "ofi_ml": "REAL", "ofi_depth": "REAL",
                                      "ofi_updates": "INTEGER"}}


def settings() -> dict:
    try:
        from ops.config import load
        raw = load().get("depth20") or {}
    except Exception:
        raw = {}
    out = dict(DEFAULTS)
    for k, v in raw.items():
        if k in DEFAULTS:
            out[k] = v
    return out


def ensure_tables(conn):
    for d in DDL:
        conn.execute(d)
    try:
        from db.schema import _add_missing_columns
        for table, cols in ADDED_COLUMNS.items():
            _add_missing_columns(conn, table, cols)
    except Exception as e:
        log.debug(f"depth20_snapshot column migration: {e}")


class FeedRefused(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(f"Dhan refused the 20-level feed ({code}): {REFUSALS.get(code, 'unknown reason')}")


# ── frames and measures (pure) ───────────────────────────────────────────────

def parse_frame(data: bytes) -> list:
    """Messages in one binary frame: {"security_id", "segment", "side": "bid"|"ask", "levels": [(price, qty,
    orders)]} with empty levels (price 0) dropped, or {"disconnect": code}. A truncated tail is ignored."""
    out, off = [], 0
    while off + HEADER.size <= len(data):
        length, code, seg, sid, extra = HEADER.unpack_from(data, off)
        if length <= 0 or off + length > len(data):
            break
        if code in (BID, ASK):
            levels = []
            for k in range(min(20, (length - HEADER.size) // LEVEL.size)):
                p, q, o = LEVEL.unpack_from(data, off + HEADER.size + k * LEVEL.size)
                if p > 0 and q > 0:
                    levels.append((p, q, o))
            out.append({"security_id": sid, "segment": seg, "side": "bid" if code == BID else "ask", "levels": levels})
        elif code == DISCONNECT:
            out.append({"disconnect": extra})
        off += length
    return out


def measures(bids: list, asks: list, band_bp: float = 50.0, decay: float = 0.5) -> dict | None:
    """DWI, best-level and 20-level imbalance, mid, spread and the best quotes from one book (levels as
    (price, qty, orders))."""
    bids = sorted(bids, key=lambda x: -x[0])
    asks = sorted(asks, key=lambda x: x[0])
    if not bids or not asks or bids[0][0] >= asks[0][0]:
        return None
    mid = (bids[0][0] + asks[0][0]) / 2
    lim = mid * band_bp / 1e4

    def weighted(levels, inside):
        return sum(math.exp(-decay * k) * q for k, (p, q, _o) in enumerate(levels) if inside(p))
    wb = weighted(bids, lambda p: mid - p <= lim)
    wa = weighted(asks, lambda p: p - mid <= lim)
    b20, a20 = sum(q for _p, q, _o in bids), sum(q for _p, q, _o in asks)

    def imb(b, a):
        return round((b - a) / (b + a), 4) if b + a > 0 else None
    return {"mid": round(mid, 4), "spread_bp": round((asks[0][0] - bids[0][0]) / mid * 1e4, 2),
            "dwi": imb(wb, wa), "imb_l1": imb(bids[0][1], asks[0][1]), "imb_20": imb(b20, a20),
            "bid_qty": b20, "ask_qty": a20, "levels": min(len(bids), len(asks)),
            "best_bid": bids[0][0], "best_bid_qty": bids[0][1], "best_ask": asks[0][0], "best_ask_qty": asks[0][1]}


def _level(levels, m, empty):
    return (levels[m][0], levels[m][1]) if m < len(levels) else (empty, 0)


def ofi_events(prev: tuple, new: tuple, k: int = 1) -> list:
    """e_n at levels 1..k between two consecutive books prev = (bids, asks) and new, levels sorted best first as
    (price, qty, ...): Cont, Kukanov & Stoikov (2014) at the best level, Xu, Gould & Howison (2019) at the m-th best
        e = 1{Pb_n >= Pb_n-1} qb_n - 1{Pb_n <= Pb_n-1} qb_n-1 - 1{Pa_n <= Pa_n-1} qa_n + 1{Pa_n >= Pa_n-1} qa_n-1
    A level a side does not have counts as quantity 0 at a price beyond every other (bids -inf, asks +inf): a level
    that empties counts as its quantity leaving, one that appears as its quantity arriving."""
    (b0, a0), (b1, a1) = prev, new
    out = []
    for m in range(k):
        pb0, qb0 = _level(b0, m, -math.inf)
        pb1, qb1 = _level(b1, m, -math.inf)
        pa0, qa0 = _level(a0, m, math.inf)
        pa1, qa1 = _level(a1, m, math.inf)
        out.append((qb1 if pb1 >= pb0 else 0) - (qb0 if pb1 <= pb0 else 0)
                   - (qa1 if pa1 <= pa0 else 0) + (qa0 if pa1 >= pa0 else 0))
    return out


class OFI:
    """One stock's order-flow imbalance between two snapshots, from every book update. update() applies a book;
    take() closes the interval and opens the next one:
        ofi_l1      sum of e_n at the best level (shares)
        ofi_depth   average best-level depth over the interval, (qb + qa) / 2
        ofi_ml      sum over levels m of w_m * OFI_m / Q, divided by sum w_m: OFI_m the sum of e_n at level m,
                    Q the average depth per level and side over the interval (levels 1..k, a missing level as 0),
                    w_m = e^(-decay (m-1))
        ofi_updates the book updates counted
    The first take() has no interval behind it: its OFI is None."""

    def __init__(self, levels: int = 20, decay: float = 0.0):
        self.k = max(1, min(20, int(levels)))
        self.w = [math.exp(-float(decay) * m) for m in range(self.k)]
        self.prev = None
        self.started = False
        self._clear()

    def _clear(self):
        self.n, self.l1, self.ml, self.d1, self.dk = 0, 0.0, [0.0] * self.k, 0.0, 0.0

    def _depth(self, bids, asks):
        return (bids[0][1] + asks[0][1]) / 2, (sum(x[1] for x in bids) + sum(x[1] for x in asks)) / (2 * self.k)

    def update(self, bids, asks) -> bool:
        """Apply one book update (the whole book after it). An empty or crossed book is skipped (False): the next
        good one is compared with the last good one."""
        bids = sorted(bids, key=lambda x: -x[0])[: self.k]
        asks = sorted(asks, key=lambda x: x[0])[: self.k]
        if not bids or not asks or bids[0][0] >= asks[0][0]:
            return False
        if self.prev is not None:
            e = ofi_events(self.prev, (bids, asks), self.k)
            self.l1 += e[0]
            self.ml = [s + x for s, x in zip(self.ml, e)]
            d1, dk = self._depth(bids, asks)
            self.d1, self.dk, self.n = self.d1 + d1, self.dk + dk, self.n + 1
        self.prev = (bids, asks)
        return True

    def take(self) -> dict:
        out = {"ofi_l1": None, "ofi_ml": None, "ofi_depth": None, "ofi_updates": None}
        if self.started and self.prev is not None:
            d1, dk = (self.d1 / self.n, self.dk / self.n) if self.n else self._depth(*self.prev)
            ml = sum(w * x for w, x in zip(self.w, self.ml)) / sum(self.w)
            out = {"ofi_l1": self.l1, "ofi_ml": round(ml / dk, 4) if dk > 0 else None, "ofi_depth": round(d1, 2),
                   "ofi_updates": self.n}
        self.started = True
        self._clear()
        return out


# ── the feed ─────────────────────────────────────────────────────────────────

class Depth20Feed:
    def __init__(self, symbols=None, cfg=None):
        self.cfg = {**DEFAULTS, **(cfg or settings())}
        self._symbols_req = symbols
        self._sid_to_sym = {}
        self._book = {}                       # security id -> {"bid": levels, "ask": levels}
        self._ofi = {}                        # symbol -> OFI, updated on every message
        self._last_sample = {}
        self._buffer = []
        self._lock = threading.Lock()
        self._running = False
        self.mode, self.detail = "IDLE", ""
        self.frames = self.snapshots = 0
        self.last_at = None

    def resolve(self) -> dict:
        from data.dhan import get_security_id
        syms = self._symbols_req or self.cfg.get("symbols") or None
        if not syms:
            from db.schema import get_connection
            from data.stock_feed import default_symbols
            c = get_connection()
            try:
                syms = default_symbols(c, int(self.cfg["max_symbols"]))
            finally:
                c.close()
        m = {}
        for s in syms[: int(self.cfg["max_symbols"])]:
            sec = get_security_id(s)
            if sec and str(sec.get("exchange", "NSE_EQ")) == "NSE_EQ":
                m[int(sec["security_id"])] = s.upper()
        self._sid_to_sym = m
        return m

    def subscriptions(self) -> list:
        sids = sorted(self._sid_to_sym)
        return [json.dumps({"RequestCode": REQUEST_SUBSCRIBE, "InstrumentCount": len(part),
                            "InstrumentList": [{"ExchangeSegment": "NSE_EQ", "SecurityId": str(s)} for s in part]})
                for part in (sids[i:i + BATCH] for i in range(0, len(sids), BATCH))]

    def consume(self, frame: bytes, now: datetime | None = None) -> int:
        """Apply one frame to the books and each book update to the stock's OFI; sample a snapshot per stock at most
        every sample_seconds, carrying the OFI summed since its previous one."""
        now = now or datetime.now()
        n = 0
        self.frames += 1
        for m in parse_frame(frame):
            if "disconnect" in m:
                raise FeedRefused(m["disconnect"])
            sym = self._sid_to_sym.get(m["security_id"])
            if not sym:
                continue
            book = self._book.setdefault(m["security_id"], {})
            book[m["side"]] = m["levels"]
            if "bid" not in book or "ask" not in book:
                continue
            ofi = self._ofi.get(sym)
            if ofi is None:
                ofi = self._ofi[sym] = OFI(int(self.cfg["ofi_levels"]), float(self.cfg["ofi_decay"]))
            ofi.update(book["bid"], book["ask"])
            last = self._last_sample.get(sym)
            if last and (now - last).total_seconds() < float(self.cfg["sample_seconds"]):
                continue
            x = measures(book["bid"], book["ask"], float(self.cfg["band_bp"]), float(self.cfg["decay"]))
            if x is None:
                continue
            x.update(ofi.take())
            self._last_sample[sym] = now
            with self._lock:
                self._buffer.append((sym, now.strftime("%Y-%m-%d %H:%M:%S"), x))
            n += 1
        self.snapshots += n
        if n:
            self.last_at = now
        return n

    def flush(self, conn) -> int:
        with self._lock:
            rows, self._buffer = self._buffer, []
        if not rows:
            return 0
        ensure_tables(conn)
        conn.executemany(f"INSERT OR REPLACE INTO depth20_snapshot (symbol, ts, {', '.join(ROW)}) "
                         f"VALUES ({','.join('?' * (len(ROW) + 2))})",
                         [(s, ts, *(x.get(k) for k in ROW)) for s, ts, x in rows])
        conn.commit()
        return len(rows)

    async def session(self, ws):
        """One connection: subscribe, then read frames until it closes; flush every 10 seconds. A new connection
        starts from empty books and OFI sums: a side left from the last one would count as a quote change."""
        from db.schema import get_connection
        self._book, self._ofi = {}, {}
        for msg in self.subscriptions():
            await ws.send(msg)
        self.mode, self.detail = "LIVE", f"{len(self._sid_to_sym)} stocks"
        last_flush = _time.monotonic()
        async for frame in ws:
            if isinstance(frame, (bytes, bytearray)):
                self.consume(bytes(frame))
            if _time.monotonic() - last_flush >= 10:
                c = get_connection()
                try:
                    self.flush(c)
                finally:
                    c.close()
                last_flush = _time.monotonic()
            if not self._running:
                break

    async def _main(self):
        from data.dhan import get_dhan_client
        from data.dhan_ws import feed_window_open
        import websockets
        backoff = 5
        while self._running:
            if not feed_window_open():
                self.mode, self.detail = "IDLE", "outside 09:00-15:45 on a trading day"
                await asyncio.sleep(30)
                continue
            try:
                if not self._sid_to_sym:
                    self.resolve()
                _dhan, ctx = get_dhan_client()
                url = f"{URL}?token={ctx.get_access_token()}&clientId={ctx.get_client_id()}&authType=2"
                async with websockets.connect(url, max_size=None) as ws:
                    backoff = 5
                    await self.session(ws)
            except FeedRefused as e:
                self.mode, self.detail = "PARKED", str(e)
                log.warning(f"  20-level depth: {e}")
                self._running = False
            except Exception as e:
                self.mode, self.detail = "RECONNECTING", f"{type(e).__name__}: {str(e)[:160]}"
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

    def start(self):
        if self._running:
            return
        self._running = True
        threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True, name="depth20-feed").start()
        log.info("  ✓ 20-level depth feed supervised")

    def stop(self):
        self._running = False

    def status(self) -> dict:
        return {"mode": self.mode, "detail": self.detail, "subscribed": len(self._sid_to_sym), "frames": self.frames,
                "snapshots": self.snapshots, "last_at": self.last_at}


_feed = None
_flock = threading.Lock()


def start_depth20(force=False):
    """Started by main.py next to the index and stock feeds when depth20.enabled (or force)."""
    global _feed
    if not (force or settings().get("enabled")):
        return None
    with _flock:
        if _feed is None:
            _feed = Depth20Feed()
            _feed.start()
        return _feed


def feed_status() -> dict:
    if _feed is not None:
        return {"running": True, **_feed.status()}
    return {"running": False, "enabled": bool(settings().get("enabled")),
            "detail": "off: set config.json depth20.enabled (needs the Dhan Data API)"}


# ── reading, flags, validation ───────────────────────────────────────────────

def latest(conn, day=None, limit: int = 100) -> list:
    """Each stock's latest snapshot on the day, with its persistent flag and the scale-free best-level OFI
    (ofi_l1_norm = ofi_l1 / ofi_depth)."""
    ensure_tables(conn)
    d = str(day or datetime.now().date())
    cur = conn.execute("SELECT s.* FROM depth20_snapshot s JOIN (SELECT symbol, MAX(ts) m FROM depth20_snapshot "
                       "WHERE ts>=? AND ts<? GROUP BY symbol) x ON s.symbol=x.symbol AND s.ts=x.m ORDER BY s.dwi DESC",
                       (d, str(datetime.fromisoformat(d).date() + timedelta(days=1))))
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()][: int(limit)]
    flags = persistent(conn, day)
    for r in rows:
        r["persistent"] = flags.get(r["symbol"])
        r["ofi_l1_norm"] = _ofi_norm(r.get("ofi_l1"), r.get("ofi_depth"))
    return rows


def _ofi_norm(ofi, depth):
    return round(ofi / depth, 4) if ofi is not None and depth else None


def persistent(conn, day=None, cfg=None) -> dict:
    """{symbol: BUYERS | SELLERS} where |DWI| > flag_dwi in each of the last flag_snapshots snapshots."""
    cfg = cfg or settings()
    ensure_tables(conn)
    d = str(day or datetime.now().date())
    by = {}
    for sym, dwi in conn.execute("SELECT symbol, dwi FROM depth20_snapshot WHERE ts>=? AND ts<? ORDER BY symbol, ts DESC",
                                 (d, str(datetime.fromisoformat(d).date() + timedelta(days=1)))):
        by.setdefault(sym, []).append(dwi)
    k, th = int(cfg["flag_snapshots"]), float(cfg["flag_dwi"])
    out = {}
    for sym, xs in by.items():
        last = xs[:k]
        if len(last) == k and all(x is not None and x > th for x in last):
            out[sym] = "BUYERS"
        elif len(last) == k and all(x is not None and x < -th for x in last):
            out[sym] = "SELLERS"
    return out


def _logit(x, y, iters=25):
    """Two-parameter logistic regression by Newton's method: (intercept, slope, z of the slope)."""
    import numpy as np
    X = np.column_stack([np.ones(len(x)), x])
    b = np.zeros(2)
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ b))
        W = p * (1 - p)
        H = X.T @ (X * W[:, None]) + 1e-9 * np.eye(2)
        step = np.linalg.solve(H, X.T @ (y - p))
        b += step
        if np.max(np.abs(step)) < 1e-8:
            break
    p = 1 / (1 + np.exp(-X @ b))
    cov = np.linalg.inv(X.T @ (X * (p * (1 - p))[:, None]) + 1e-9 * np.eye(2))
    return float(b[0]), float(b[1]), float(b[1] / math.sqrt(cov[1, 1])) if cov[1, 1] > 0 else 0.0


def _fit(x, y, flag: str = "predictive") -> dict:
    """One measure against the direction of a move (y 1 up, 0 down): n, the logistic slope and its z, how often the
    sign was right (|x| > 0.1), and `flag` -- true only with MIN_VALIDATE+ rows, |z| >= Z_MIN and a positive slope.
    When the measure separates the ups from the downs (every down at or below every up, or the reverse) the slope
    has no finite estimate and its Wald z collapses towards 0 however strong the relation: slope None, "separated",
    and the score test's z, sqrt(n) x the correlation of x and y, stands in. OFI against its own interval's move
    can separate: a quote moving a tick moves every level's OFI with it."""
    import numpy as np
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    if len(x) < 30 or len(set(y.tolist())) < 2 or np.std(x) == 0:
        return {"n": len(x), "slope": None, "z": None, "sign_right_pct": None, "separated": False, flag: False}
    down, up = x[y == 0], x[y == 1]
    separated = bool(down.max() <= up.min() or up.max() <= down.min())
    if separated:
        xc, yb = x - x.mean(), y.mean()
        slope, z = None, float(xc @ (y - yb) / math.sqrt(yb * (1 - yb) * (xc @ xc)))
    else:
        _a, slope, z = _logit(x, y)
    big = np.abs(x) > 0.1
    right = float(np.mean((x[big] > 0) == (y[big] > 0)) * 100) if big.any() else None
    return {"n": len(x), "slope": round(slope, 3) if slope is not None else None, "z": round(z, 2),
            "sign_right_pct": round(right, 1) if right is not None else None, "separated": separated,
            flag: bool(len(x) >= MIN_VALIDATE and z >= Z_MIN)}          # z >= 2: |z| >= 2 and a positive slope


def validate(conn, horizon_minutes: int = 1, days: int = 20) -> dict:
    """Does each measure predict the direction of the mid's next move? One row per snapshot whose stock has a
    snapshot `horizon_minutes` later (within 2 minutes of it); unchanged mids are left out, and so is a measure
    the snapshot does not have (OFI: snapshots stored before it, or the first after a reconnect). OFI enters as
    ofi_l1 / ofi_depth (scale-free) and ofi_ml.
    "contemporaneous": each OFI against the mid's move over its own interval, from the stock's previous snapshot
    (at most 2 minutes earlier) to this one -- the same logistic rule ("related"), plus CKS's linear fit of the move
    in bp on the OFI, stock by stock (30+ pairs, unchanged mids included): the median bp per unit and R^2."""
    import numpy as np
    if horizon_minutes not in (1, 5):
        raise ValueError("horizon_minutes must be 1 or 5")
    ensure_tables(conn)
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = conn.execute("SELECT symbol, ts, mid, dwi, imb_l1, imb_20, ofi_l1, ofi_depth, ofi_ml FROM depth20_snapshot "
                        "WHERE ts>=? AND mid>0 ORDER BY symbol, ts", (since,)).fetchall()
    h, tol = timedelta(minutes=horizon_minutes), timedelta(minutes=2)
    names = ("dwi", "imb_l1", "imb_20") + OFI_MEASURES
    nxt = {k: ([], []) for k in names}                   # the measure, did the mid rise by the horizon
    con = {k: {} for k in OFI_MEASURES}                  # per stock: the OFI, the mid's move (bp) over its interval
    by = {}
    for r in rows:
        x = {"dwi": r[3], "imb_l1": r[4], "imb_20": r[5], "ofi_l1": _ofi_norm(r[6], r[7]), "ofi_ml": r[8]}
        by.setdefault(r[0], []).append((datetime.fromisoformat(str(r[1])[:19]), r[2], x))
    pairs = same = 0
    for sym, snaps in by.items():
        j = 0
        for i, (t, mid, x) in enumerate(snaps):
            if i and t - snaps[i - 1][0] <= tol and any(x[k] is not None for k in OFI_MEASURES):
                same += 1
                move = (mid / snaps[i - 1][1] - 1) * 1e4
                for k in OFI_MEASURES:
                    if x[k] is not None:
                        xs, moves = con[k].setdefault(sym, ([], []))
                        xs.append(x[k])
                        moves.append(move)
            j = max(j, i + 1)
            while j < len(snaps) and snaps[j][0] < t + h:
                j += 1
            if j >= len(snaps) or snaps[j][0] > t + h + tol or snaps[j][1] == mid:
                continue
            pairs += 1
            for k in names:
                if x[k] is not None:
                    nxt[k][0].append(x[k])
                    nxt[k][1].append(1.0 if snaps[j][1] > mid else 0.0)
    out = {"horizon_minutes": horizon_minutes, "days": days, "n": pairs, "min_n": MIN_VALIDATE,
           "measures": {k: _fit(*nxt[k]) for k in names}, "contemporaneous": {"pairs": same, "measures": {}}}
    for k, per in con.items():
        x = np.array([v for xs, _m in per.values() for v in xs], dtype=float)
        move = np.array([v for _x, moves in per.values() for v in moves], dtype=float)
        moved = move != 0
        fit = _fit(x[moved], (move[moved] > 0).astype(float), "related")
        lines = [_line(xs, moves) for xs, moves in per.values() if len(xs) >= 30]
        lines = [ln for ln in lines if ln]
        fit.update({"pairs": len(x), "stocks": len(lines),
                    "bp_per_unit": round(float(np.median([b for b, _r in lines])), 3) if lines else None,
                    "r2": round(float(np.median([r for _b, r in lines])), 3) if lines else None})
        out["contemporaneous"]["measures"][k] = fit
    return out


def _line(x, y):
    """(slope, R^2) of the least-squares line of y on x; None when either is constant."""
    import numpy as np
    c = np.cov(np.asarray(x, dtype=float), np.asarray(y, dtype=float), bias=True)
    if c[0, 0] <= 0 or c[1, 1] <= 0:
        return None
    return float(c[0, 1] / c[0, 0]), float(c[0, 1] ** 2 / (c[0, 0] * c[1, 1]))
