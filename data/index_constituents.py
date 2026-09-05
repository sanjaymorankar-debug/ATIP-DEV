"""ATIP — NSE Index Constituent Lists

Fetches the official Nifty 500 constituent list from NSE archives so the
tracked/scored universe can be a broader, index-defined set of stocks
instead of a small hardcoded list. Falls back gracefully:

  1. Fresh download from NSE (cached locally for a week)
  2. Last successfully cached copy, however old, if NSE is unreachable
  3. The original curated Nifty High-Beta ~50 list, if neither of the
     above is available (e.g. first run, no internet yet)

Nothing here ever raises — a broken/unreachable NSE endpoint degrades to
a smaller universe rather than crashing the pipeline.
"""
import time, logging
from pathlib import Path
from datetime import datetime, timedelta
import requests, pandas as pd

log = logging.getLogger(__name__)

RAW_DIR = Path("atip_data/raw"); RAW_DIR.mkdir(parents=True, exist_ok=True)
NIFTY500_URL   = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
NIFTY500_CACHE = RAW_DIR / "ind_nifty500list.csv"
NSE_HOME       = "https://www.nseindia.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0",
           "Accept": "*/*", "Referer": "https://www.nseindia.com/"}
CACHE_MAX_AGE_DAYS = 7  # index reshuffles happen quarterly; a week-old list is fine


def _cache_is_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    return age < timedelta(days=CACHE_MAX_AGE_DAYS)


def fetch_nifty500_symbols(force_refresh: bool = False) -> list:
    """
    Returns the current Nifty 500 constituent symbols (uppercase, no
    exchange suffix). Never raises — returns [] on total failure so
    callers can fall back to their own default list.
    """
    if not force_refresh and _cache_is_fresh(NIFTY500_CACHE):
        try:
            df = pd.read_csv(NIFTY500_CACHE)
            syms = _extract_symbols(df)
            if syms:
                return syms
        except Exception as e:
            log.warning(f"  Nifty500 cache read failed, will re-download: {e}")

    try:
        session = requests.Session(); session.headers.update(HEADERS)
        try:
            session.get(NSE_HOME, timeout=12); time.sleep(1.0)
        except Exception as e:
            log.warning(f"  NSE session warm-up: {e}")
        r = session.get(NIFTY500_URL, timeout=20)
        r.raise_for_status()
        NIFTY500_CACHE.write_bytes(r.content)
        df = pd.read_csv(NIFTY500_CACHE)
        syms = _extract_symbols(df)
        if syms:
            log.info(f"  ✓ Nifty 500 list refreshed from NSE: {len(syms)} symbols")
            return syms
    except Exception as e:
        log.warning(f"  Nifty500 download failed: {e}")

    # Fall back to whatever cached copy exists, even if stale
    if NIFTY500_CACHE.exists():
        try:
            df = pd.read_csv(NIFTY500_CACHE)
            syms = _extract_symbols(df)
            if syms:
                log.warning(f"  Using stale cached Nifty 500 list ({len(syms)} symbols)")
                return syms
        except Exception as e:
            log.warning(f"  Stale Nifty500 cache unreadable: {e}")

    log.warning("  No Nifty 500 list available (no network, no cache) — caller should fall back to defaults")
    return []


def _extract_symbols(df: pd.DataFrame) -> list:
    for col in ("Symbol", "SYMBOL", "symbol"):
        if col in df.columns:
            return sorted({str(s).strip().upper() for s in df[col].dropna() if str(s).strip()})
    return []


def get_symbol_industry_map() -> dict:
    """
    Symbol -> NSE industry/sector classification, read from the same
    ind_nifty500list.csv used for the tracked universe (it carries an
    Industry column alongside Symbol — no extra download needed).

    This replaces the hardcoded sector_rank=5 stub that used to be passed
    into compute_zpi()/Trade-of-Day scoring for every stock. Returns {} if
    the cache is missing/unreadable — callers must degrade gracefully
    (e.g. fall back to a neutral sector score), never raise.
    """
    if not _cache_is_fresh(NIFTY500_CACHE):
        fetch_nifty500_symbols()  # best-effort refresh; keeps stale cache on failure
    if not NIFTY500_CACHE.exists():
        return {}
    try:
        df = pd.read_csv(NIFTY500_CACHE)
    except Exception as e:
        log.warning(f"  Industry map read failed: {e}")
        return {}
    sym_col = next((c for c in ("Symbol", "SYMBOL", "symbol") if c in df.columns), None)
    ind_col = next((c for c in ("Industry", "INDUSTRY", "industry") if c in df.columns), None)
    if not sym_col or not ind_col:
        return {}
    out = {}
    for _, row in df.iterrows():
        sym = str(row[sym_col]).strip().upper()
        ind = str(row[ind_col]).strip()
        if sym and ind and ind.lower() != "nan":
            out[sym] = ind
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    syms = fetch_nifty500_symbols(force_refresh=True)
    print(f"{len(syms)} symbols" + (f" — e.g. {syms[:10]}" if syms else " (fetch failed)"))
