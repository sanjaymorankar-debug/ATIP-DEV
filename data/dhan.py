"""
ATIP — Dhan API Integration (Real-Time + Historical Data)
===========================================================
Replaces yfinance for Indian stocks with Dhan's official API.

Dhan API provides:
  ✅ Real-time live quotes (LTP, OHLC, full market depth)
  ✅ WebSocket streaming (tick-by-tick)
  ✅ Historical daily OHLCV (replaces NSE Bhavcopy)
  ✅ Intraday 1-min / 5-min / 15-min OHLCV
  ✅ Option chain + F&O data
  ✅ Portfolio holdings (your Dhan account)
  ✅ Order placement (if needed later)

Setup:
  1. pip install dhanhq
  2. Login at https://dhanhq.co/
  3. Go to: My Profile → Apps → Create App → get Client ID + Access Token
  4. Add to atip_data/config.json:
       "dhan_client_id": "your_client_id",
       "dhan_access_token": "your_access_token"

Dhan Security IDs:
  Each stock has a unique security_id on Dhan (different from NSE symbol).
  Run: python -m data.dhan --download-securities
  This saves security_id_list.csv which maps symbols ↔ security_ids.

Run standalone:
  python -m data.dhan --download-securities   # one-time: get security ID map
  python -m data.dhan --live                  # start live feed (9:15–3:30 PM)
  python -m data.dhan --historical --days 365 # download 1-year history
  python -m data.dhan --quote RELIANCE TCS    # get live quotes for symbols
  python -m data.dhan --portfolio             # sync Dhan portfolio
"""

import os, json, time, logging, argparse, asyncio
import pandas as pd
from datetime import date, datetime, timedelta
from pathlib import Path
from db.schema import get_connection, log_job

log = logging.getLogger(__name__)

# ── Install check ─────────────────────────────────────────────────────────
try:
    from dhanhq import dhanhq as DhanHQ, marketfeed
    HAS_DHAN = True
except ImportError:
    HAS_DHAN = False
    log.warning("dhanhq not installed. Run: pip install dhanhq")

CONFIG_PATH  = Path("atip_data/config.json")
CACHE_DIR    = Path("atip_data/raw/dhan")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
SEC_LIST_PATH= CACHE_DIR / "security_id_list.csv"

# ── Dhan feed subscription types ──────────────────────────────────────────
TICKER = 15   # LTP only (lightest)
QUOTE  = 17   # LTP + OHLC + Volume + prev close
FULL   = 21   # Quote + Market Depth (full order book)


# ═════════════════════════════════════════════════════════════════════════
#  CONFIG & CLIENT
# ═════════════════════════════════════════════════════════════════════════

def load_dhan_config() -> dict:
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
        except Exception:
            pass
    cfg.setdefault("dhan_client_id",     os.getenv("DHAN_CLIENT_ID", ""))
    cfg.setdefault("dhan_access_token",  os.getenv("DHAN_ACCESS_TOKEN", ""))
    return cfg


def get_dhan_client():
    """Return authenticated Dhan client."""
    if not HAS_DHAN:
        raise RuntimeError("pip install dhanhq")
    cfg = load_dhan_config()
    if not cfg["dhan_client_id"] or not cfg["dhan_access_token"]:
        raise RuntimeError(
            "Dhan credentials not set.\n"
            "Add to atip_data/config.json:\n"
            '  "dhan_client_id": "your_client_id"\n'
            '  "dhan_access_token": "your_access_token"\n'
            "Get them from: https://dhanhq.co → My Profile → Apps"
        )
    from dhanhq import DhanContext
    ctx  = DhanContext(cfg["dhan_client_id"], cfg["dhan_access_token"])
    dhan = DhanHQ(ctx)
    return dhan, ctx


# ═════════════════════════════════════════════════════════════════════════
#  SECURITY ID MAP  (symbol ↔ Dhan security_id)
# ═════════════════════════════════════════════════════════════════════════

def download_security_list() -> pd.DataFrame:
    """
    Download Dhan's full security master list.
    Saves to atip_data/raw/dhan/security_id_list.csv
    Maps NSE symbol → security_id (needed for all API calls).
    """
    log.info("📥 Downloading Dhan security master list...")
    try:
        DhanHQ.fetch_security_list(mode="compact",
                                   filename=str(SEC_LIST_PATH))
        df = pd.read_csv(str(SEC_LIST_PATH))
        log.info(f"  ✓ {len(df)} securities downloaded → {SEC_LIST_PATH}")
        return df
    except Exception as e:
        log.error(f"  ✗ Security list failed: {e}")
        return pd.DataFrame()


def load_security_map() -> dict:
    """
    Returns dict: {NSE_SYMBOL: {"security_id": "...", "exchange": "NSE_EQ"}}
    Auto-downloads if not present.
    """
    if not SEC_LIST_PATH.exists():
        log.info("Security list not found — downloading...")
        download_security_list()

    if not SEC_LIST_PATH.exists():
        log.error("Could not download security list")
        return {}

    df = pd.read_csv(str(SEC_LIST_PATH))

    # Dhan CSV columns: SEM_SMST_SECURITY_ID, SEM_TRADING_SYMBOL,
    # SEM_EXM_EXCH_ID, SEM_INSTRUMENT_NAME, SEM_SERIES etc.
    sym_col  = next((c for c in df.columns if "TRADING_SYMBOL" in c.upper() or "SYMBOL" in c.upper()), None)
    sec_col  = next((c for c in df.columns if "SECURITY_ID" in c.upper()), None)
    exch_col = next((c for c in df.columns if "EXCH" in c.upper() or "EXCHANGE" in c.upper()), None)
    ser_col  = next((c for c in df.columns if "SERIES" in c.upper()), None)

    if not sym_col or not sec_col:
        log.error(f"  Unknown CSV columns: {list(df.columns)[:10]}")
        return {}

    # Filter EQ series on NSE only
    mask = pd.Series([True] * len(df))
    if ser_col:
        mask = mask & df[ser_col].isin(["EQ", "BE", "SM", "N"])
    if exch_col:
        mask = mask & df[exch_col].astype(str).str.contains("NSE", na=False)

    df_nse = df[mask]
    sec_map = {}
    for _, row in df_nse.iterrows():
        symbol = str(row[sym_col]).strip().upper()
        sec_id = str(row[sec_col]).strip()
        exch   = "NSE_EQ"
        sec_map[symbol] = {"security_id": sec_id, "exchange": exch}

    log.info(f"  ✓ Security map: {len(sec_map)} NSE EQ stocks")
    return sec_map


# Cache in memory during session
_SECURITY_MAP: dict = {}

def get_security_id(symbol: str) -> dict:
    """Get Dhan security_id for an NSE symbol."""
    global _SECURITY_MAP
    if not _SECURITY_MAP:
        _SECURITY_MAP = load_security_map()
    sym = symbol.upper().strip()
    if sym in _SECURITY_MAP:
        return _SECURITY_MAP[sym]
    # Try common suffixes
    for suffix in ["-EQ", "&", ""]:
        if sym + suffix in _SECURITY_MAP:
            return _SECURITY_MAP[sym + suffix]
    log.warning(f"  {symbol}: security_id not found")
    return {}


# ═════════════════════════════════════════════════════════════════════════
#  HISTORICAL DAILY DATA  (replaces NSE Bhavcopy + yfinance)
# ═════════════════════════════════════════════════════════════════════════

def fetch_historical_daily(symbol: str, from_date: date, to_date: date,
                           dhan=None) -> pd.DataFrame:
    """
    Fetch daily OHLCV for a symbol from Dhan.
    Returns DataFrame with columns: date, open, high, low, close, volume.
    """
    if dhan is None:
        dhan, _ = get_dhan_client()

    sec = get_security_id(symbol)
    if not sec:
        return pd.DataFrame()

    cache_file = CACHE_DIR / f"{symbol}_{from_date}_{to_date}_daily.json"
    if cache_file.exists():
        try:
            return pd.read_json(str(cache_file))
        except Exception:
            pass

    try:
        resp = dhan.historical_daily_data(
            security_id   = sec["security_id"],
            exchange_segment = sec["exchange"],
            instrument_type  = "EQUITY",
            from_date     = from_date.strftime("%Y-%m-%d"),
            to_date       = to_date.strftime("%Y-%m-%d"),
        )

        if not resp or resp.get("status") == "failure":
            log.warning(f"  {symbol}: Dhan API error — {resp}")
            # An empty frame alone cannot distinguish "the broker refused" from
            # "this symbol simply had no bars", and callers were treating both as
            # nothing-to-do. Tag the failure so the pipeline can count it.
            bad = pd.DataFrame()
            try:
                rem = (resp or {}).get("remarks") or {}
                bad.attrs["dhan_error"] = (rem.get("error_code")
                                           if isinstance(rem, dict) else str(rem)) or "unknown"
            except Exception:
                bad.attrs["dhan_error"] = "unknown"
            return bad

        data = resp.get("data", {})
        if not data:
            return pd.DataFrame()

        df = pd.DataFrame({
            "date":   pd.to_datetime(data.get("timestamp", []), unit="s").date,
            "open":   data.get("open",   []),
            "high":   data.get("high",   []),
            "low":    data.get("low",    []),
            "close":  data.get("close",  []),
            "volume": data.get("volume", []),
        })
        df["symbol"] = symbol
        df["source"] = "dhan"

        if not df.empty:
            df.to_json(str(cache_file))
            log.debug(f"  ✓ {symbol}: {len(df)} daily bars")

        return df

    except Exception as e:
        log.warning(f"  {symbol} daily: {e}")
        return pd.DataFrame()


def fetch_historical_intraday(symbol: str, from_date: date, to_date: date,
                               interval: int = 1, dhan=None) -> pd.DataFrame:
    """
    Fetch intraday OHLCV bars (1, 5, 15, 25, 60 minute).
    interval: 1 = 1-min, 5 = 5-min, 15 = 15-min, 25 = 25-min, 60 = 1-hour
    """
    if dhan is None:
        dhan, _ = get_dhan_client()

    sec = get_security_id(symbol)
    if not sec:
        return pd.DataFrame()

    try:
        resp = dhan.intraday_minute_data(
            security_id      = sec["security_id"],
            exchange_segment = sec["exchange"],
            instrument_type  = "EQUITY",
            from_date        = from_date.strftime("%Y-%m-%d"),
            to_date          = to_date.strftime("%Y-%m-%d"),
            interval         = interval,
        )

        if not resp or resp.get("status") == "failure":
            return pd.DataFrame()

        data = resp.get("data", {})
        if not data:
            return pd.DataFrame()

        df = pd.DataFrame({
            "datetime": pd.to_datetime(data.get("timestamp", []), unit="s"),
            "open":     data.get("open",   []),
            "high":     data.get("high",   []),
            "low":      data.get("low",    []),
            "close":    data.get("close",  []),
            "volume":   data.get("volume", []),
        })
        df["symbol"]   = symbol
        df["interval"] = interval
        log.debug(f"  ✓ {symbol}: {len(df)} {interval}-min bars")
        return df

    except Exception as e:
        log.warning(f"  {symbol} intraday {interval}m: {e}")
        return pd.DataFrame()


# ═════════════════════════════════════════════════════════════════════════
#  LIVE QUOTE DATA  (REST — snapshot, not streaming)
# ═════════════════════════════════════════════════════════════════════════

def fetch_live_quotes(symbols: list, dhan=None) -> pd.DataFrame:
    """
    Fetch live LTP, OHLC, volume, prev_close for a list of symbols.
    Uses Dhan's quote_data() REST endpoint (batch, up to 1000 symbols).
    Returns DataFrame with live snapshot.
    """
    if dhan is None:
        dhan, _ = get_dhan_client()

    sec_map = {}
    instruments = {}

    for sym in symbols:
        sec = get_security_id(sym)
        if sec:
            sid = sec["security_id"]
            exch = sec["exchange"]
            sec_map[sid] = sym
            if exch not in instruments:
                instruments[exch] = []
            instruments[exch].append(int(sid))

    if not instruments:
        log.warning("  No valid security IDs found")
        return pd.DataFrame()

    try:
        resp = dhan.quote_data(instruments)
        if not resp or resp.get("status") == "failure":
            log.warning(f"  Quote data failed: {resp}")
            return pd.DataFrame()

        rows = []
        data = resp.get("data", {})

        # Dhan's quote_data() (full quote w/ market depth) has been observed
        # to double-wrap the payload — {"status":"success","data":{"status":
        # "success","data":{"NSE_EQ":{...}}}} — one level deeper than
        # ohlc_data()'s {"data":{"NSE_EQ":{...}}}. Unwrap any such envelope
        # layers (dicts whose only keys are status/data/remarks) until we
        # reach the actual exchange-segment dict. This was confirmed against
        # a real response on 2026-07-30 — the un-unwrapped version silently
        # produced a "quote" that was really {sec_id: {...}}, so every field
        # read off it came back None.
        _seen = 0
        while isinstance(data, dict) and set(data.keys()) <= {"status", "data", "remarks"} and "data" in data:
            data = data["data"]
            _seen += 1
            if _seen > 5:  # sanity guard against any unexpected infinite-envelope response
                break

        if not isinstance(data, dict):
            log.error(f"  Live quotes failed: expected dict for resp['data'], got {type(data).__name__}: {str(data)[:200]}")
            return pd.DataFrame()

        for exch, stocks in data.items():
            if not isinstance(stocks, dict):
                log.warning(f"  {exch}: expected dict of quotes, got {type(stocks).__name__}: {str(stocks)[:200]} — skipping this exchange")
                continue
            for sec_id_str, q in stocks.items():
                if not isinstance(q, dict):
                    log.warning(f"  {exch}/{sec_id_str}: expected quote dict, got {type(q).__name__} — skipping")
                    continue
                sym = sec_map.get(sec_id_str, sec_id_str)
                # Dhan's full quote sometimes nests OHLC under an "ohlc" sub-object
                # rather than flat top-level keys — check both.
                ohlc = q.get("ohlc") if isinstance(q.get("ohlc"), dict) else {}
                ltp = q.get("last_price", q.get("LTP"))
                prev_close = q.get("prev_close", q.get("previous_close", q.get("close", ohlc.get("close"))))
                # Dhan's quote payload doesn't actually include a ready-made
                # change_percentage field (confirmed: it came back None even
                # once ltp resolved correctly) — compute it the same way
                # dhan_test.py's working reference code does.
                if ltp is not None and prev_close:
                    chg_pct = round((ltp - prev_close) / prev_close * 100, 2)
                else:
                    chg_pct = 0.0
                row = {
                    "symbol":     sym,
                    "ltp":        ltp,
                    "open":       q.get("open", ohlc.get("open")),
                    "high":       q.get("high", ohlc.get("high")),
                    "low":        q.get("low", ohlc.get("low")),
                    "close":      q.get("close", ohlc.get("close")),
                    "prev_close": prev_close,
                    "volume":     q.get("volume"),
                    "chg_pct":    chg_pct,
                    "timestamp":  datetime.now(),
                }
                if ltp is None:
                    log.warning(f"  {sym}: quote resolved with ltp=None — raw fields seen: {list(q.keys())}")
                rows.append(row)

        df = pd.DataFrame(rows)
        log.info(f"  ✓ Live quotes: {len(df)} stocks fetched")
        return df

    except Exception as e:
        log.error(f"  Live quotes failed: {e}")
        return pd.DataFrame()


# ═════════════════════════════════════════════════════════════════════════
#  INDEX QUOTES  (NSE indices — Nifty50, sector indices, etc.)
# ═════════════════════════════════════════════════════════════════════════
# Indices aren't in load_security_map() (it only keeps EQ/BE/SM/N series
# stocks), so they need their own security-id map. IDs below are pulled
# from Dhan's security master (SEM_EXCH_INSTRUMENT_TYPE == "INDEX",
# SEM_EXM_EXCH_ID == "NSE") — re-run download_security_list() and re-derive
# if any of these ever stop resolving.
NSE_INDEX_SECURITY_IDS = {
    "nifty50":       "13",
    "banknifty":     "25",
    "nifty_it":      "29",
    "nifty_pharma":  "32",
    "india_vix":     "21",
    "midcap150":     "1",
    "smallcap250":   "3",
    "nifty_auto":    "14",
    "nifty_fmcg":    "28",
    "nifty_metal":   "31",
    "nifty_realty":  "34",
    "nifty_psubank": "33",
    "nifty_energy":  "42",
    "gift_nifty":    "5024",
}
INDEX_EXCHANGE_SEGMENT = "IDX_I"


def fetch_index_quotes(cols: list = None, dhan=None) -> pd.DataFrame:
    """
    Fetch live LTP/OHLC for NSE indices via Dhan's POST /marketfeed/ohlc.
    cols: list of keys from NSE_INDEX_SECURITY_IDS (default: all of them).
    Returns DataFrame with columns: index, ltp, open, high, low, prev_close,
    chg_pct, timestamp. prev_close is Dhan's ohlc.close field (previous day's
    close) — Dhan does not send a separate prev_close/previous_close field.
    """
    if dhan is None:
        dhan, _ = get_dhan_client()

    cols = cols or list(NSE_INDEX_SECURITY_IDS.keys())
    sec_map = {}
    sec_ids = []
    for col in cols:
        sid = NSE_INDEX_SECURITY_IDS.get(col)
        if sid:
            sec_map[sid] = col
            sec_ids.append(int(sid))

    if not sec_ids:
        log.warning("  No valid index security IDs found")
        return pd.DataFrame()

    try:
        resp = dhan.ohlc_data({INDEX_EXCHANGE_SEGMENT: sec_ids})
        if not resp or resp.get("status") == "failure":
            log.warning(f"  Index quote data failed: {resp}")
            return pd.DataFrame()

        data = resp.get("data", {})
        if not isinstance(data, dict):
            log.error(f"  Index quotes failed: expected dict, got {type(data).__name__}: {str(data)[:200]}")
            return pd.DataFrame()

        stocks = data.get(INDEX_EXCHANGE_SEGMENT, {})
        if not isinstance(stocks, dict):
            log.warning(f"  {INDEX_EXCHANGE_SEGMENT}: expected dict of quotes, got {type(stocks).__name__} — skipping")
            return pd.DataFrame()

        rows = []
        for sec_id_str, q in stocks.items():
            if not isinstance(q, dict):
                continue
            col = sec_map.get(sec_id_str, sec_id_str)
            # Per Dhan's documented /marketfeed/ohlc & /marketfeed/quote response,
            # OHLC is nested under "ohlc", and there is NO top-level prev_close /
            # previous_close field — "ohlc.close" IS the previous day's close.
            # (This was the actual bug: the old code looked for a top-level
            # prev_close that Dhan never sends, so chg_pct silently came back
            # None and the dashboard kept showing 0.00%.)
            ohlc = q.get("ohlc") or {}
            prev_close = ohlc.get("close")
            ltp = q.get("last_price")
            chg = None
            try:
                if ltp is not None and prev_close:
                    chg = round((float(ltp) - float(prev_close)) / float(prev_close) * 100, 3)
            except (TypeError, ValueError, ZeroDivisionError):
                chg = None
            rows.append({
                "index":      col,
                "ltp":        ltp,
                "open":       ohlc.get("open"),
                "high":       ohlc.get("high"),
                "low":        ohlc.get("low"),
                "prev_close": prev_close,
                "chg_pct":    chg,
                "timestamp":  datetime.now(),
            })

        df = pd.DataFrame(rows)
        log.info(f"  ✓ Index quotes: {len(df)} indexes fetched")
        return df

    except Exception as e:
        log.error(f"  Index quotes failed: {e}")
        return pd.DataFrame()


BETA_BENCHMARK_SYMBOL = "NIFTY50"  # synthetic symbol stored in prices_daily as the beta benchmark

def sync_index_benchmark_history(days: int = 420, end_date: date = None,
                                  index_key: str = "nifty50", dhan=None) -> dict:
    """
    Downloads daily OHLC for the Nifty 50 index itself (via Dhan's IDX_I
    segment) and stores it in prices_daily under the synthetic symbol
    "NIFTY50" (source='dhan_index'). This gives compute_beta() a benchmark
    return series to regress each stock's returns against, reusing the
    existing prices_daily table rather than adding a new one.

    NIFTY50 rows are never picked up by get_tracked_symbols() or the
    scoring universe (that comes from the Nifty 500 constituent list /
    portfolio holdings, not from scanning prices_daily), so this doesn't
    risk it being treated as a tradeable stock anywhere downstream.
    """
    if not HAS_DHAN:
        return {"status": "FAILED", "error": "pip install dhanhq"}
    try:
        if dhan is None:
            dhan, _ = get_dhan_client()
    except RuntimeError as e:
        log.error(str(e)); return {"status": "FAILED", "error": str(e)}

    sec_id = NSE_INDEX_SECURITY_IDS.get(index_key)
    if not sec_id:
        return {"status": "FAILED", "error": f"unknown index_key {index_key}"}

    end_dt   = end_date or date.today()
    start_dt = end_dt - timedelta(days=days)
    try:
        resp = dhan.historical_daily_data(
            security_id      = sec_id,
            exchange_segment = INDEX_EXCHANGE_SEGMENT,
            instrument_type  = "INDEX",
            from_date        = start_dt.strftime("%Y-%m-%d"),
            to_date           = end_dt.strftime("%Y-%m-%d"),
        )
        if not resp or resp.get("status") == "failure":
            log.warning(f"  Benchmark ({index_key}) history failed: {resp}")
            return {"status": "FAILED", "error": str(resp)}
        data = resp.get("data", {})
        if not data:
            return {"status": "FAILED", "error": "empty response"}

        dates = pd.to_datetime(data.get("timestamp", []), unit="s").date
        closes = data.get("close", [])
        conn = get_connection()
        count = 0
        for d, c in zip(dates, closes):
            if c is None:
                continue
            conn.execute("""
                INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol,date) DO UPDATE SET close=excluded.close
            """, (BETA_BENCHMARK_SYMBOL, str(d), c, c, c, c, 0, "dhan_index"))
            count += 1
        conn.commit(); conn.close()
        log.info(f"  ✓ Benchmark ({index_key}) history: {count} days stored as {BETA_BENCHMARK_SYMBOL}")
        return {"status": "SUCCESS", "rows": count}
    except Exception as e:
        log.warning(f"  Benchmark ({index_key}) history: {e}")
        return {"status": "FAILED", "error": str(e)}


def fetch_live_ohlc(symbols: list, dhan=None) -> pd.DataFrame:
    """Lighter than quote_data — only LTP + OHLC, no market depth."""
    if dhan is None:
        dhan, _ = get_dhan_client()

    instruments = {}
    sec_map = {}
    for sym in symbols:
        sec = get_security_id(sym)
        if sec:
            sid = sec["security_id"]
            exch = sec["exchange"]
            sec_map[sid] = sym
            instruments.setdefault(exch, []).append(int(sid))

    if not instruments:
        return pd.DataFrame()

    try:
        resp = dhan.ohlc_data(instruments)
        if not resp or resp.get("status") == "failure":
            return pd.DataFrame()

        rows = []
        for exch, stocks in resp.get("data", {}).items():
            for sid, q in stocks.items():
                rows.append({
                    "symbol":   sec_map.get(sid, sid),
                    "ltp":      q.get("last_price"),
                    "open":     q.get("open"),
                    "high":     q.get("high"),
                    "low":      q.get("low"),
                    "close":    q.get("close"),
                    "volume":   q.get("volume"),
                    "timestamp":datetime.now(),
                })
        return pd.DataFrame(rows)

    except Exception as e:
        log.error(f"  OHLC data failed: {e}")
        return pd.DataFrame()


# ═════════════════════════════════════════════════════════════════════════
#  WEBSOCKET LIVE FEED  (streaming tick-by-tick)
# ═════════════════════════════════════════════════════════════════════════

class DhanLiveFeed:
    """
    WebSocket streaming feed from Dhan.
    Receives tick-by-tick data during market hours (9:15 AM – 3:30 PM IST).

    Usage:
        feed = DhanLiveFeed(symbols=["RELIANCE","TCS","INFY"])
        feed.start()       # blocking — runs until market close
        feed.stop()        # stop streaming
    """

    def __init__(self, symbols: list, feed_type: int = QUOTE,
                 on_tick=None, store_to_db: bool = True):
        self.symbols      = symbols
        self.feed_type    = feed_type      # TICKER=15, QUOTE=17, FULL=21
        self.on_tick      = on_tick        # optional callback(tick_data)
        self.store_to_db  = store_to_db
        self._feed        = None
        self._tick_count  = 0
        self._last_store  = datetime.now()
        self._buffer: list = []

        # Build instrument list: [(security_id, exchange_segment, feed_type), ...]
        self._instruments = self._build_instruments()

    def _build_instruments(self) -> list:
        instruments = []
        for sym in self.symbols:
            sec = get_security_id(sym)
            if sec:
                try:
                    exch_code = self._exchange_to_code(sec["exchange"])
                    instruments.append((int(sec["security_id"]), exch_code, self.feed_type))
                except Exception as e:
                    log.warning(f"  {sym} instrument build: {e}")
        log.info(f"  ✓ {len(instruments)} instruments ready for live feed")
        return instruments

    def _exchange_to_code(self, exchange: str) -> int:
        """Map exchange string to Dhan numeric code."""
        mapping = {
            "NSE_EQ":       1,
            "NSE_FNO":      2,
            "BSE_EQ":       3,
            "MCX_COMM":     7,
            "NSE_CURRENCY": 10,
        }
        return mapping.get(exchange, 1)

    def _on_message(self, data: dict):
        """Callback when tick data arrives."""
        self._tick_count += 1

        # Enrich with symbol name
        sec_id = str(data.get("security_id", data.get("LTP_security_id", "")))
        sym = _SECURITY_MAP.get(sec_id, {})
        data["symbol"] = sym if isinstance(sym, str) else sec_id
        data["received_at"] = datetime.now().isoformat()

        # User callback
        if self.on_tick:
            try:
                self.on_tick(data)
            except Exception as e:
                log.warning(f"  on_tick callback error: {e}")

        # Buffer for batch DB writes
        if self.store_to_db:
            self._buffer.append(data)
            # Write to DB every 100 ticks or 30 seconds
            if len(self._buffer) >= 100 or \
               (datetime.now() - self._last_store).seconds >= 30:
                self._flush_buffer()

        # Log every 500 ticks
        if self._tick_count % 500 == 0:
            ltp = data.get("LTP", data.get("last_price", "?"))
            log.info(f"  📡 {self._tick_count} ticks received | Last: {data.get('symbol')} @ ₹{ltp}")

    def _on_connect(self):
        log.info("  📡 Dhan WebSocket connected — live feed started")

    def _on_close(self):
        log.info(f"  📡 Feed closed. Total ticks: {self._tick_count}")
        self._flush_buffer()

    def _on_error(self, error):
        log.error(f"  📡 Feed error: {error}")

    def _flush_buffer(self):
        """Write buffered ticks to database."""
        if not self._buffer:
            return
        try:
            conn = get_connection()
            # Create live_ticks table if not exists
            conn.execute("""
                CREATE TABLE IF NOT EXISTS live_ticks (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol      TEXT,
                    security_id TEXT,
                    ltp         REAL,
                    open        REAL,
                    high        REAL,
                    low         REAL,
                    close       REAL,
                    volume      INTEGER,
                    timestamp   TEXT,
                    received_at TEXT
                )
            """)
            for tick in self._buffer:
                ltp    = tick.get("LTP",    tick.get("last_price"))
                open_  = tick.get("open",   tick.get("Open"))
                high   = tick.get("high",   tick.get("High"))
                low    = tick.get("low",    tick.get("Low"))
                close  = tick.get("close",  tick.get("Close"))
                vol    = tick.get("volume", tick.get("vol"))
                ts     = tick.get("exchange_timestamp", tick.get("timestamp", ""))
                conn.execute(
                    "INSERT INTO live_ticks (symbol,security_id,ltp,open,high,low,close,volume,timestamp,received_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (tick.get("symbol"), str(tick.get("security_id","")),
                     ltp, open_, high, low, close, vol, str(ts), tick.get("received_at",""))
                )
            conn.commit()
            conn.close()
            log.debug(f"  💾 Flushed {len(self._buffer)} ticks to DB")
        except Exception as e:
            log.warning(f"  Tick flush failed: {e}")
        finally:
            self._buffer = []
            self._last_store = datetime.now()

    def start(self):
        """Start the WebSocket feed (blocking)."""
        if not self._instruments:
            log.error("  No instruments to subscribe")
            return

        cfg = load_dhan_config()
        try:
            from dhanhq import DhanContext
            ctx = DhanContext(cfg["dhan_client_id"], cfg["dhan_access_token"])
            self._feed = marketfeed.MarketFeed(
                dhan_context = ctx,
                instruments  = self._instruments,
                version      = "v2",
                on_connect   = self._on_connect,
                on_message   = self._on_message,
                on_close     = self._on_close,
                on_error     = self._on_error,
            )
            log.info(f"  🚀 Starting Dhan live feed for {len(self._instruments)} instruments...")
            self._feed.run_forever()
        except Exception as e:
            log.error(f"  Feed start failed: {e}")

    def stop(self):
        if self._feed:
            try:
                self._feed.disconnect()
            except Exception:
                pass


# ═════════════════════════════════════════════════════════════════════════
#  BULK HISTORICAL DOWNLOAD  (replace yfinance for Nifty 500)
# ═════════════════════════════════════════════════════════════════════════

def _default_symbols() -> list:
    """Nifty High Beta ~50 — emergency fallback only, used when the Nifty
    500 constituent list can't be fetched AND no local cache exists yet
    (e.g. very first run with no internet access)."""
    return [
        "ADANIGREEN","ADANIENT","ADANIENSOL","ANGELONE","RPOWER",
        "BSE","MOFSL","LODHA","HINDCOPPER","WOCKPHARMA",
        "TATAMOTORS","TATASTEEL","JSWSTEEL","SAIL","BHEL",
        "VEDL","BEL","HAL","RECLTD","PFC","IRFC","IREDA",
        "DLF","NHPC","SJVN","RVNL","HUDCO","INOXWIND",
        "SUZLON","ADANIPOWER","GODREJPROP","PRESTIGE","OBEROIRLTY",
        "NMDC","NATIONALUM","IRCTC","ZOMATO","JIOFIN",
        "PNB","CANBK","BANKBARODA","YESBANK","PAYTM","IDEA",
        "MAZDOCK","HDFCAMC","COALINDIA","HINDALCO","NYKAA","TATAPOWER",
    ]


def get_tracked_symbols(conn=None) -> list:
    """
    The curated symbol universe ATIP actually tracks and scores — the
    official Nifty 500 constituent list (broader than the original ~50-stock
    High Beta list, still bounded — not the full ~3,000-symbol market) plus
    anything currently held in the portfolio. Falls back to the small
    hardcoded High-Beta list only if the Nifty 500 list can't be fetched or
    read from cache at all (e.g. first run, no internet yet).

    This is deliberately NOT "whatever symbols exist in prices_daily",
    because prices_daily also receives a full NSE Bhavcopy dump every day
    (~3,000 EQ/BE/SM tickers market-wide) for archival purposes. Using that
    table's contents as "the universe to fetch/score" previously caused the
    pipeline to try to download and score the entire market instead of the
    tracked watchlist — 18-minute historical pulls, SQLite lock contention,
    and thousands of junk scored rows for stocks that were never actually
    part of the strategy.
    """
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    from data.index_constituents import fetch_nifty500_symbols
    nifty500 = fetch_nifty500_symbols()
    symbols = set(nifty500) if nifty500 else set(_default_symbols())

    try:
        rows = conn.execute("SELECT DISTINCT symbol FROM portfolio_holdings").fetchall()
        symbols.update(r["symbol"] for r in rows)
    except Exception:
        pass
    if close_conn:
        conn.close()
    return sorted(symbols)


def run_historical_pipeline(symbols: list = None, days: int = 365,
                             interval_min: int = 0, end_date: date = None) -> dict:
    """
    Download historical data for all symbols and store in prices_daily.
    interval_min=0 → daily data
    interval_min=1 → 1-minute intraday (stored separately)
    interval_min=5 → 5-minute intraday
    end_date: last day to fetch through (defaults to today). Callers like the
    post-market pipeline pass the resolved trading-day target so a run before
    4 PM IST (when today's EOD data isn't published yet) doesn't try to pull
    a partial/nonexistent "today".
    """
    if not HAS_DHAN:
        return {"status": "FAILED", "error": "pip install dhanhq"}

    try:
        dhan, _ = get_dhan_client()
    except RuntimeError as e:
        log.error(str(e)); return {"status": "FAILED", "error": str(e)}

    end_dt   = end_date or date.today()
    start_dt = end_dt - timedelta(days=days)

    if not symbols:
        # Use the curated tracked universe, NOT "every symbol currently in
        # prices_daily" — that table also holds the full-market Bhavcopy
        # dump and would otherwise balloon this into thousands of symbols.
        symbols = get_tracked_symbols()

    log.info(f"📥 Dhan historical ({interval_min or 'daily'}): {len(symbols)} symbols, {days} days")
    conn  = get_connection()
    count = 0
    errors= 0
    failed = 0          # broker refused (DH-902 not subscribed, DH-905 bad params, ...)
    empty  = 0          # broker answered, but this symbol had no bars
    codes  = {}

    for i, sym in enumerate(symbols):
        try:
            if interval_min == 0:
                df = fetch_historical_daily(sym, start_dt, end_dt, dhan)
            else:
                df = fetch_historical_intraday(sym, start_dt, end_dt, interval_min, dhan)

            if df.empty:
                # Separate a refusal from a genuinely empty symbol, so a total
                # outage cannot be reported as a clean run.
                if df.attrs.get("dhan_error"):
                    failed += 1
                    codes[df.attrs["dhan_error"]] = codes.get(df.attrs["dhan_error"], 0) + 1
                else:
                    empty += 1
                continue

            if interval_min == 0:
                # Store in prices_daily
                for _, row in df.iterrows():
                    raw_date = row.get("date")
                    # Normalize to a plain YYYY-MM-DD string. Values can arrive as a
                    # datetime.date (fresh API fetch) OR a pandas Timestamp with a
                    # 00:00:00 time component (round-tripped through the JSON cache
                    # via pd.read_json). str()'ing a Timestamp directly yields
                    # "YYYY-MM-DD HH:MM:SS", which corrupts the DATE column and
                    # breaks sqlite3's PARSE_DECLTYPES date parsing on every later
                    # read (int() failing on the stray time fragment). Always route
                    # through pd.to_datetime(...).strftime(...) so the stored value
                    # is a clean date regardless of source.
                    date_str = pd.to_datetime(raw_date).strftime("%Y-%m-%d") if raw_date else ""
                    conn.execute("""
                        INSERT INTO prices_daily
                            (symbol,date,open,high,low,close,volume,source)
                        VALUES(?,?,?,?,?,?,?,?)
                        ON CONFLICT(symbol,date) DO UPDATE SET
                            open=excluded.open, high=excluded.high,
                            low=excluded.low,   close=excluded.close,
                            volume=excluded.volume
                    """, (sym, date_str, row.get("open"),
                          row.get("high"), row.get("low"), row.get("close"),
                          row.get("volume"), "dhan"))
                count += len(df)

            # Commit every 10 symbols
            if (i + 1) % 10 == 0:
                conn.commit()
                log.info(f"  Progress: {i+1}/{len(symbols)} ({count} rows so far)")

            time.sleep(0.3)  # Dhan rate limit: ~3 req/sec on free plan

        except Exception as e:
            errors += 1
            log.warning(f"  {sym}: {e}")

    conn.commit()
    conn.close()

    # Tonight's run logged "0 rows, 0 errors" while every single symbol was
    # refused with DH-902, because a refusal came back as an empty frame and the
    # loop skipped it silently. A run that fetched nothing is not a success.
    attempted = len(symbols)
    refused_share = failed / attempted if attempted else 0
    if count == 0 and attempted:
        status = "FAILED"
    elif refused_share > 0.2:
        status = "PARTIAL"
    else:
        status = "SUCCESS"

    detail = f"{count} rows, {errors} errors"
    if failed:
        top = ", ".join(f"{c}x{k}" for k, c in sorted(codes.items(), key=lambda kv: -kv[1])[:3])
        detail += f", {failed}/{attempted} refused by broker ({top})"
    if empty:
        detail += f", {empty} with no bars"
    mark = "✓" if status == "SUCCESS" else "✗" if status == "FAILED" else "!"
    (log.info if status == "SUCCESS" else log.warning)(f"  {mark} Historical download: {detail}")

    result = {"status": status, "rows": count, "errors": errors,
              "refused": failed, "empty": empty, "error_codes": codes}
    log_job("dhan_historical", status, count,
            error=(f"{failed}/{attempted} refused: {codes}" if failed else None))
    return result


# ═════════════════════════════════════════════════════════════════════════
#  LIVE QUOTE REFRESH  (every 15 min intraday — REST, not WebSocket)
# ═════════════════════════════════════════════════════════════════════════

def run_live_quote_refresh(symbols: list = None) -> dict:
    """
    Fetch live quotes for all tracked symbols via Dhan REST API.
    Stores in index_levels table (for dashboard) and live_quotes table.
    Called by scheduler every 15 min during market hours.
    """
    if not HAS_DHAN:
        return {"status": "FAILED", "error": "pip install dhanhq"}

    try:
        dhan, _ = get_dhan_client()
    except RuntimeError as e:
        log.error(str(e)); return {"status": "FAILED"}

    if not symbols:
        symbols = _default_symbols()

    df = fetch_live_quotes(symbols, dhan)
    if df.empty:
        return {"status": "PARTIAL", "rows": 0}

    # Also update live prices in prices_daily (today's intraday latest)
    conn  = get_connection()
    today = str(date.today())
    count = 0

    # Create live_quotes table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS live_quotes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol     TEXT,
            ltp        REAL,
            open       REAL,
            high       REAL,
            low        REAL,
            prev_close REAL,
            volume     INTEGER,
            chg_pct    REAL,
            timestamp  TEXT,
            UNIQUE(symbol, timestamp)
        )
    """)

    for _, row in df.iterrows():
        conn.execute("""
            INSERT OR REPLACE INTO live_quotes
                (symbol,ltp,open,high,low,prev_close,volume,chg_pct,timestamp)
            VALUES(?,?,?,?,?,?,?,?,?)
        """, (row.get("symbol"), row.get("ltp"), row.get("open"),
              row.get("high"), row.get("low"), row.get("prev_close"),
              row.get("volume"), row.get("chg_pct"),
              datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        count += 1

    conn.commit()
    conn.close()
    log.info(f"  ✓ Live quotes refreshed: {count} stocks")
    log_job("dhan_live_quotes", "SUCCESS", count)
    return {"status": "SUCCESS", "rows": count}


# ═════════════════════════════════════════════════════════════════════════
#  PORTFOLIO SYNC  (Dhan holdings)
# ═════════════════════════════════════════════════════════════════════════

def sync_dhan_portfolio(trade_date: date = None) -> dict:
    """
    Sync Dhan portfolio holdings into portfolio_holdings table.
    Works alongside or instead of Zerodha sync.
    """
    if trade_date is None:
        trade_date = date.today()

    try:
        dhan, _ = get_dhan_client()
    except RuntimeError as e:
        log.error(str(e)); return {"status": "FAILED"}

    try:
        resp = dhan.get_holdings()
        if not resp or resp.get("status") == "failure":
            log.error(f"  Holdings failed: {resp}")
            return {"status": "FAILED"}

        holdings = resp.get("data", [])
        if not holdings:
            log.info("  No holdings found in Dhan account")
            return {"status": "SUCCESS", "rows": 0}

        conn  = get_connection()
        count = 0
        total_val = 0

        # Get live quotes for all held symbols
        symbols = [h.get("tradingSymbol", h.get("symbol","")) for h in holdings]
        quotes  = fetch_live_quotes(symbols, dhan)
        q_map   = {}
        if not quotes.empty:
            q_map = {row["symbol"]: row for _, row in quotes.iterrows()}

        for h in holdings:
            sym     = h.get("tradingSymbol", h.get("symbol",""))
            qty     = h.get("totalQty", h.get("quantity", 0))
            avg     = h.get("avgCostPrice", h.get("averageBuyPrice", 0))
            cmp     = q_map.get(sym, {}).get("ltp") or h.get("lastTradedPrice", avg)
            cur_val = qty * cmp if qty and cmp else 0
            pnl     = (cmp - avg) * qty if (cmp and avg and qty) else 0
            pnl_pct = round((cmp - avg) / avg * 100, 2) if (avg and avg > 0) else 0

            # Get ATIP scores for this stock
            sc = conn.execute(
                "SELECT atip_score,vpi,cri,zpi,signal FROM ai_scores WHERE symbol=? AND date=?",
                (sym, str(trade_date))
            ).fetchone()

            conn.execute("""
                INSERT INTO portfolio_holdings
                    (date,symbol,qty,avg_price,cmp,current_val,pnl,pnl_pct,
                     atip_score,vpi,cri,zpi,signal)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol,date) DO UPDATE SET
                    cmp=excluded.cmp, current_val=excluded.current_val,
                    pnl=excluded.pnl, pnl_pct=excluded.pnl_pct
            """, (str(trade_date), sym, qty, avg, cmp, cur_val, pnl, pnl_pct,
                  sc["atip_score"] if sc else None, sc["vpi"] if sc else None,
                  sc["cri"] if sc else None, sc["zpi"] if sc else None,
                  sc["signal"] if sc else "NO DATA"))
            count   += 1
            total_val += cur_val

        conn.commit()
        conn.close()
        log.info(f"  ✓ Dhan portfolio synced: {count} holdings  Total: ₹{total_val:,.0f}")
        log_job("dhan_portfolio", "SUCCESS", count, run_date=trade_date)
        return {"status": "SUCCESS", "rows": count, "total_value": total_val}

    except Exception as e:
        log.error(f"  Portfolio sync failed: {e}")
        return {"status": "FAILED", "error": str(e)}


# ═════════════════════════════════════════════════════════════════════════
#  DEFAULTS  (see get_tracked_symbols() / _default_symbols() above, near
#  run_historical_pipeline)
# ═════════════════════════════════════════════════════════════════════════


# ═════════════════════════════════════════════════════════════════════════
#  CLI
# ═════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)s  %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")

    ap = argparse.ArgumentParser(description="ATIP — Dhan API Integration")
    ap.add_argument("--download-securities", action="store_true",
                    help="Download Dhan security master list (run once)")
    ap.add_argument("--historical", action="store_true",
                    help="Download historical daily data for all symbols")
    ap.add_argument("--intraday",   type=int, metavar="MINS",
                    help="Download intraday data (1/5/15/25/60 minute bars)")
    ap.add_argument("--live",       action="store_true",
                    help="Start WebSocket live feed (market hours only)")
    ap.add_argument("--quote",      nargs="+", metavar="SYMBOL",
                    help="Get live quotes for specific symbols")
    ap.add_argument("--portfolio",  action="store_true",
                    help="Sync Dhan portfolio holdings")
    ap.add_argument("--days",       type=int, default=365,
                    help="Days of history to download (default: 365)")
    ap.add_argument("--symbols",    nargs="+",
                    help="Specific symbols to process")
    ap.add_argument("--feed-type",  choices=["ticker","quote","full"],
                    default="quote",
                    help="WebSocket feed type (default: quote)")
    args = ap.parse_args()

    if args.download_securities:
        df = download_security_list()
        print(f"\n✅ Downloaded {len(df)} securities → {SEC_LIST_PATH}")
        if not df.empty:
            print(f"   Columns: {list(df.columns)}")
            print(f"   Sample:\n{df.head(3).to_string()}")

    elif args.historical:
        result = run_historical_pipeline(
            symbols=args.symbols, days=args.days, interval_min=0
        )
        print(f"\n{'✅' if result['status']=='SUCCESS' else '❌'} {result}")

    elif args.intraday:
        result = run_historical_pipeline(
            symbols=args.symbols, days=min(args.days, 90),  # Dhan limit
            interval_min=args.intraday
        )
        print(f"\n{'✅' if result['status']=='SUCCESS' else '❌'} {result}")

    elif args.live:
        feed_type = {"ticker": TICKER, "quote": QUOTE, "full": FULL}.get(
            args.feed_type, QUOTE
        )
        symbols = args.symbols or _default_symbols()
        print(f"\n🚀 Starting live feed for {len(symbols)} symbols ({args.feed_type} mode)")
        print("   Press Ctrl+C to stop\n")
        feed = DhanLiveFeed(symbols=symbols, feed_type=feed_type, store_to_db=True)
        try:
            feed.start()
        except KeyboardInterrupt:
            feed.stop()
            print("\n⏹  Feed stopped")

    elif args.quote:
        df = fetch_live_quotes(args.quote)
        if not df.empty:
            print(f"\n{'Symbol':<15} {'LTP':>10} {'Open':>10} {'High':>10} {'Low':>10} {'Chg%':>8} {'Volume':>12}")
            print("-" * 80)
            for _, row in df.iterrows():
                print(f"{row.get('symbol',''):<15} "
                      f"₹{(row.get('ltp') or 0):>9,.2f} "
                      f"₹{(row.get('open') or 0):>9,.2f} "
                      f"₹{(row.get('high') or 0):>9,.2f} "
                      f"₹{(row.get('low') or 0):>9,.2f} "
                      f"{(row.get('chg_pct') or 0):>7,.2f}% "
                      f"{(row.get('volume') or 0):>12,}")
        else:
            print("❌ No quote data. Check credentials and security IDs.")

    elif args.portfolio:
        result = sync_dhan_portfolio()
        print(f"\n{'✅' if result['status']=='SUCCESS' else '❌'} {result}")

    else:
        ap.print_help()
