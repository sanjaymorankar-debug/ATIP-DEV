"""
The instrument master (owner item 5, 16 Nov 2026): Dhan's scrip master downloaded every morning at 08:00 IST, cached
on disk and stored in the table instrument_master, plus the symbol renames and demergers ATIP's own lists still used.

Why. Dhan's security list was downloaded once, when atip_data/raw/dhan/security_id_list.csv was missing, and read
into memory once per process, so a symbol NSE renamed stayed unknown until someone deleted the file. From 28 Sep to
13 Nov atip.log carries 3,534 "security_id not found" warnings -- 121 to 151 a day -- for six symbols:

    ZOMATO      renamed ETERNAL on NSE (Eternal Ltd, ISIN INE758T01015, Dhan security_id 5097)
    TATAMOTORS  the Tata Motors demerger (Oct 2025): the listed company kept its ISIN INE155A01022 and Dhan
                security_id 3456 (dhan_test.py still has TATAMOTORS: 3456) and trades as TMPV (passenger vehicles);
                the commercial-vehicles business listed separately as TMCV (759782, ISIN INE1TAE01010). The old
                ticker is mapped to TMPV, the same security; TMCV is its own row of the Nifty 500 list.
    MOFSL       not an NSE symbol: Motilal Oswal Financial Services is MOTILALOFS (14947)
    EMBASSY, BIRET, BAGMANE
                REITs (series RR, Dhan instrument type REIT) that the Nifty 500 list now carries. They are units,
                not shares: NSE's Bhavcopy EQ rows, fundamentals and Dhan's equity map do not have them. The
                equity universe drops series RR / IV (data/index_constituents.py); a direct lookup still resolves
                them here, deliberately, as a REIT row.

Every mapping above was read from Dhan's scrip master of 14 Nov 2026 (Last-Modified 02:50 GMT) and NSE's
ind_nifty500list.csv of 15 Nov, not from memory. The first three came from data/dhan.py _default_symbols(), the
universe of the 15-minute quote refresh and the live feed; the REITs from the Nifty 500 list.

    refresh(conn=None)           download the compact scrip master (images.dhan.co, public, no credentials) to
                                 atip_data/raw/dhan/security_id_list.csv, store its NSE equity-segment rows (all
                                 series: EQ, BE, BZ, SM, RR, IV, ETFs, bonds), add ISINs from the Nifty 500 list,
                                 and make data/dhan.py re-read the file. With "zerodha_enabled" true and a Kite
                                 session, Kite's instruments list too (read-only, source 'kite').
    lookup(symbol, conn=None)    {"symbol", "security_id", "tick_size", "lot_size", "series", "isin", ...} or
                                 None. Aliases resolve (ZOMATO -> ETERNAL). Cached per day and process: one query
                                 a day, re-validated every 5 minutes. orders/ticks.py reads tick_size here.
    status(conn=None)            the last refresh per source: as_of (IST day), rows, published_at (Dhan's
                                 Last-Modified), fresh (as_of == today) -- the 08:45 pre-open gate's check
    ensure_today(conn=None)      refresh when today's copy is missing or Dhan published a newer file since.
                                 Dhan's file showed Last-Modified 08:20 IST on 14 Nov, so the 08:00 run can get
                                 the previous build; the 08:45 gate (ops/preopen_health.py) calls this.
    load_kite_csv(path)          rows from a Kite Connect instruments CSV (tick_size, lot_size keyed on
                                 exchange + tradingsymbol), for store()

Units: Dhan's compact SEM_TICK_SIZE is in paise (5.0000 = Rs 0.05: ETERNAL 5, SIEMENS 10, MRF 500, MOTHERSON 1,
matching NSE's price-band ticks); stored here in rupees. Kite's tick_size is already in rupees.

The view-only server never downloads (run_scheduled is a no-op there): it reads the table from the Mac's database.

Run by hand:  python -m data.instruments --refresh | --status | --lookup ZOMATO TATAMOTORS | --kite-csv FILE
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import threading
import time
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger("atip.instruments")

COMPACT_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
CONFIG_PATH = Path("atip_data") / "config.json"
NSE = "NSE"

# Old ticker -> NSE's current one. Each verified against Dhan's scrip master of 14 Nov 2026 (see the docstring).
ALIASES = {
    "ZOMATO": "ETERNAL",
    "TATAMOTORS": "TMPV",
    "MOFSL": "MOTILALOFS",
}
# Listed on NSE's equity segment but not shares: kept out of the equity universe, resolved on a direct lookup.
NON_EQUITY_SERIES = {"RR": "REIT", "IV": "InvIT"}
# Which row a symbol means when NSE lists it in several series (MOTHERSON: EQ and an NCD in D1)
SERIES_ORDER = ("EQ", "BE", "BZ", "SM", "ST", "RR", "IV")
SOURCE_ORDER = ("dhan", "kite")

REVALIDATE_SECONDS = 300

_lock = threading.Lock()
_cache = {"key": None, "rows": {}, "stamp": None, "checked": 0.0}


def _today() -> date:
    return datetime.now().date()


def _config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def settings() -> dict:
    c = _config().get("instrument_master") or {}
    return {"enabled": c.get("enabled", True) is not False, "time": str(c.get("time") or "08:00")}


def clean(symbol) -> str:
    """'nse:zomato-eq ' -> 'ZOMATO'."""
    s = str(symbol or "").strip().upper()
    if ":" in s:
        s = s.split(":", 1)[1]
    for suf in ("-EQ", "-BE", "-BZ", "-SM", "-ST"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    return s


def canonical(symbol) -> str:
    """The symbol NSE trades it under today (ALIASES), else the symbol itself."""
    s = clean(symbol)
    return ALIASES.get(s, s)


# ── storage ───────────────────────────────────────────────────────────────
def ensure_table(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS instrument_master (
        source TEXT NOT NULL, exchange TEXT NOT NULL, symbol TEXT NOT NULL, series TEXT NOT NULL DEFAULT '',
        segment TEXT, security_id TEXT, instrument_type TEXT, name TEXT, tick_size REAL, lot_size REAL,
        isin TEXT, as_of TEXT NOT NULL, published_at TEXT, updated_at TEXT,
        PRIMARY KEY (source, exchange, symbol, series))""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_instrument_master_symbol ON instrument_master (symbol)")
    conn.execute("""CREATE TABLE IF NOT EXISTS instrument_master_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, as_of TEXT NOT NULL, status TEXT NOT NULL,
        `rows` INTEGER, published_at TEXT, error TEXT, at TEXT NOT NULL)""")


def _num(v, default=None):
    try:
        x = float(v)
        return default if x != x else x
    except (TypeError, ValueError):
        return default


def _pick(cols, *names):
    up = {c.strip().upper(): c for c in cols}
    for n in names:
        if n in up:
            return up[n]
    return None


def parse_dhan_master(source, as_of=None, published_at=None) -> list:
    """NSE equity-segment rows (every series) of Dhan's scrip master: a path, a file object or CSV text.
    Reads the compact file's SEM_* columns, or the detailed file's plain names."""
    import pandas as pd
    if isinstance(source, (str, Path)) and Path(str(source)).exists():
        df = pd.read_csv(str(source), dtype=str, low_memory=False)
    elif isinstance(source, str):
        df = pd.read_csv(io.StringIO(source), dtype=str, low_memory=False)
    else:
        df = pd.read_csv(source, dtype=str, low_memory=False)
    c = list(df.columns)
    exch = _pick(c, "SEM_EXM_EXCH_ID", "EXCH_ID")
    seg = _pick(c, "SEM_SEGMENT", "SEGMENT")
    sid = _pick(c, "SEM_SMST_SECURITY_ID", "SECURITY_ID")
    sym = _pick(c, "SEM_TRADING_SYMBOL", "TRADING_SYMBOL", "UNDERLYING_SYMBOL")
    tick = _pick(c, "SEM_TICK_SIZE", "TICK_SIZE")
    lot = _pick(c, "SEM_LOT_UNITS", "LOT_SIZE")
    ser = _pick(c, "SEM_SERIES", "SERIES")
    typ = _pick(c, "SEM_EXCH_INSTRUMENT_TYPE", "INSTRUMENT_TYPE")
    name = _pick(c, "SM_SYMBOL_NAME", "SEM_CUSTOM_SYMBOL", "DISPLAY_NAME", "SYMBOL_NAME")
    isin = _pick(c, "ISIN")
    if not (exch and sid and sym):
        raise ValueError(f"not a Dhan scrip master: columns {c[:8]}")
    m = df[exch].astype(str).str.strip().str.upper() == NSE
    if seg:
        m &= df[seg].astype(str).str.strip().str.upper() == "E"
    as_of, now = str(as_of or _today()), datetime.now().isoformat(timespec="seconds")
    rows = []
    for r in df[m].to_dict("records"):
        s = clean(r.get(sym))
        if not s or s == "NAN":
            continue
        t = _num(r.get(tick)) if tick else None
        rows.append({"source": "dhan", "exchange": NSE, "symbol": s,
                     "series": str(r.get(ser) or "").strip().upper() if ser else "",
                     "segment": "E", "security_id": str(r.get(sid)).strip(),
                     "instrument_type": str(r.get(typ) or "").strip().upper() if typ else None,
                     "name": (str(r.get(name)).strip() if name and r.get(name) else None),
                     # paise in Dhan's compact file (5.0000 = Rs 0.05)
                     "tick_size": round(t / 100.0, 4) if t else None,
                     "lot_size": _num(r.get(lot), 1.0) if lot else 1.0,
                     "isin": (str(r.get(isin)).strip() or None) if isin and r.get(isin) else None,
                     "as_of": as_of, "published_at": published_at, "updated_at": now})
    return rows


def _is_file(text: str) -> bool:
    """True when `text` names an existing file. CSV text is never one: on Linux (the VPS, CI) asking the
    filesystem about a 100-kB "name" raises OSError ENAMETOOLONG instead of answering False (23 Nov 2026)."""
    if "\n" in text or len(text) > 4096:
        return False
    try:
        return Path(text).is_file()
    except (OSError, ValueError):
        return False


def parse_kite_instruments(source, as_of=None, exchanges=("NSE",)) -> list:
    """Kite Connect's instruments list -- a CSV (text, path or file) or the SDK's list of dicts -- as rows keyed on
    exchange + tradingsymbol. Kite's tick_size is in rupees already; security_id holds the instrument_token."""
    if isinstance(source, (list, tuple)):
        recs = [dict(r) for r in source]
    else:
        if isinstance(source, Path) or (isinstance(source, str) and _is_file(source)):
            text = Path(str(source)).read_text(encoding="utf-8")
        elif isinstance(source, str):
            text = source
        else:
            text = source.read()
        recs = list(csv.DictReader(io.StringIO(text)))
    as_of, now = str(as_of or _today()), datetime.now().isoformat(timespec="seconds")
    rows = []
    for r in recs:
        ex = str(r.get("exchange") or "").strip().upper()
        if exchanges and ex not in exchanges:
            continue
        s = str(r.get("tradingsymbol") or "").strip().upper()
        if not s:
            continue
        rows.append({"source": "kite", "exchange": ex, "symbol": s, "series": "",
                     "segment": str(r.get("segment") or "").strip().upper() or None,
                     "security_id": str(r.get("instrument_token") or "").strip() or None,
                     "instrument_type": str(r.get("instrument_type") or "").strip().upper() or None,
                     "name": str(r.get("name") or "").strip() or None,
                     "tick_size": _num(r.get("tick_size")), "lot_size": _num(r.get("lot_size"), 1.0),
                     "isin": None, "as_of": as_of, "published_at": None, "updated_at": now})
    return rows


def load_kite_csv(path, as_of=None) -> list:
    return parse_kite_instruments(Path(path), as_of=as_of)


_COLS = ("source", "exchange", "symbol", "series", "segment", "security_id", "instrument_type", "name", "tick_size",
         "lot_size", "isin", "as_of", "published_at", "updated_at")


def store(conn, rows: list, source: str) -> int:
    """Replace one source's snapshot with `rows` (one transaction). No commit."""
    ensure_table(conn)
    conn.execute("DELETE FROM instrument_master WHERE source=?", (source,))
    conn.executemany(f"INSERT OR REPLACE INTO instrument_master ({','.join(_COLS)}) VALUES ({','.join('?' * len(_COLS))})",
                     [tuple(r.get(k) for k in _COLS) for r in rows if r.get("source") == source])
    _invalidate()
    return len(rows)


def _log(conn, source, as_of, status, rows=None, published_at=None, error=None):
    ensure_table(conn)
    conn.execute("INSERT INTO instrument_master_log (source, as_of, status, `rows`, published_at, error, at) "
                 "VALUES (?,?,?,?,?,?,?)", (source, str(as_of), status, rows, published_at,
                                            (str(error)[:500] if error else None),
                                            datetime.now().isoformat(timespec="seconds")))


def _nifty500_isins() -> dict:
    """symbol -> ISIN from NSE's ind_nifty500list.csv (the compact scrip master has no ISIN)."""
    try:
        from data.index_constituents import NIFTY500_CACHE
        out = {}
        with open(NIFTY500_CACHE, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                s, i = str(r.get("Symbol") or "").strip().upper(), str(r.get("ISIN Code") or "").strip()
                if s and i:
                    out[s] = i
        return out
    except Exception:
        return {}


# ── refresh ───────────────────────────────────────────────────────────────
def _sec_list_path() -> Path:
    from data.dhan import SEC_LIST_PATH
    return Path(SEC_LIST_PATH)


def _published_at(timeout=15):
    """Dhan's Last-Modified for the scrip master (a HEAD request), or None."""
    import requests
    try:
        r = requests.head(COMPACT_URL, timeout=timeout)
        return r.headers.get("last-modified") if r.status_code == 200 else None
    except Exception:
        return None


def download(dest: Path = None, timeout=90):
    """Download the compact scrip master to dest (atomically: a half-written file never replaces a good one).
    Returns (path, Last-Modified)."""
    import requests
    dest = Path(dest or _sec_list_path())
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    with requests.get(COMPACT_URL, timeout=timeout, stream=True) as r:
        r.raise_for_status()
        with open(part, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
        published = r.headers.get("last-modified")
    return part, published


def _reset_dhan_map():
    """data/dhan.py keeps the security list in memory for the process: make it re-read the new file."""
    try:
        import data.dhan as D
        D._SECURITY_MAP = {}
        getattr(D, "_NOT_FOUND_WARNED", {}).clear()
    except Exception:
        pass


def refresh(conn=None, *, csv_path=None, download_file=True, kite=True) -> dict:
    """Today's instrument master: download (unless csv_path is given), parse, store, log. Never raises."""
    from db.schema import get_connection
    own = conn is None
    conn = conn or get_connection()
    today = _today()
    out = {"status": "FAILED", "as_of": str(today), "rows": 0, "source": "dhan"}
    try:
        published = None
        if csv_path is None and download_file:
            part, published = download()
            rows = parse_dhan_master(part, as_of=today, published_at=published)
            if len(rows) < 1000:
                raise ValueError(f"only {len(rows)} NSE equity rows in the downloaded file -- kept the old one")
            os.replace(part, _sec_list_path())
        else:
            src = Path(csv_path) if csv_path else _sec_list_path()
            rows = parse_dhan_master(src, as_of=today, published_at=published)
        isins = _nifty500_isins()
        for r in rows:
            if not r.get("isin") and r["series"] in ("EQ", "BE", "RR") and r["symbol"] in isins:
                r["isin"] = isins[r["symbol"]]
        store(conn, rows, "dhan")
        _log(conn, "dhan", today, "SUCCESS", len(rows), published)
        conn.commit()
        _invalidate()
        _reset_dhan_map()
        out.update(status="SUCCESS", rows=len(rows), published_at=published,
                   by_series=_series_counts(rows))
        log.info(f"  ✓ Instrument master: {len(rows)} NSE equity-segment rows (Dhan, published {published or '?'})")
    except Exception as e:
        try:
            conn.rollback()
            _log(conn, "dhan", today, "FAILED", None, None, f"{type(e).__name__}: {e}")
            conn.commit()
        except Exception:
            pass
        out["error"] = f"{type(e).__name__}: {e}"
        log.warning(f"  Instrument master refresh failed: {out['error']}")
    if kite:
        try:
            k = refresh_kite(conn)
            if k:
                out["kite"] = k
        except Exception as e:
            out["kite"] = {"status": "FAILED", "error": f"{type(e).__name__}: {e}"}
    if own:
        conn.close()
    return out


def _series_counts(rows) -> dict:
    out = {}
    for r in rows:
        out[r["series"] or "-"] = out.get(r["series"] or "-", 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1])[:8])


def refresh_kite(conn) -> dict | None:
    """Kite's instruments (NSE) when the owner has switched Zerodha on (portfolio/zerodha.enabled) and a Kite
    session exists. Read-only (brokers/connectors.ZerodhaConnector.instruments). None when Zerodha is off."""
    try:
        from portfolio.zerodha import enabled, load_cfg, TOKEN_PATH
    except Exception:
        return None
    if not enabled():
        return None
    try:
        key = load_cfg().get("kite_api_key")
        tok = json.loads(Path(TOKEN_PATH).read_text(encoding="utf-8")).get("access_token") if Path(TOKEN_PATH).exists() else None
        if not (key and tok):
            return {"status": "SKIPPED", "reason": "no Kite session (python main.py --login-zerodha)"}
        from brokers.connectors import ZerodhaConnector
        rows = parse_kite_instruments(ZerodhaConnector({"api_key": key, "access_token": tok}).instruments("NSE"))
        store(conn, rows, "kite")
        _log(conn, "kite", _today(), "SUCCESS", len(rows))
        conn.commit()
        _invalidate()
        return {"status": "SUCCESS", "rows": len(rows)}
    except Exception as e:
        try:
            conn.rollback()
            _log(conn, "kite", _today(), "FAILED", None, None, f"{type(e).__name__}: {e}")
            conn.commit()
        except Exception:
            pass
        return {"status": "FAILED", "error": f"{type(e).__name__}: {e}"}


def status(conn=None, today=None) -> dict:
    """The last successful refresh per source and whether Dhan's is today's."""
    from db.schema import get_connection
    own = conn is None
    conn = conn or get_connection()
    today = str(today or _today())
    try:
        ensure_table(conn)
        out = {"today": today, "sources": {}}
        for src in SOURCE_ORDER:
            r = conn.execute("SELECT as_of, `rows`, published_at, at FROM instrument_master_log WHERE source=? AND "
                             "status='SUCCESS' ORDER BY id DESC LIMIT 1", (src,)).fetchone()
            if r:
                out["sources"][src] = {"as_of": r[0], "rows": r[1], "published_at": r[2], "at": r[3]}
        last_fail = conn.execute("SELECT as_of, error, at FROM instrument_master_log WHERE source='dhan' AND "
                                 "status='FAILED' ORDER BY id DESC LIMIT 1").fetchone()
        d = out["sources"].get("dhan") or {}
        out.update(as_of=d.get("as_of"), rows=d.get("rows"), published_at=d.get("published_at"),
                   fresh=d.get("as_of") == today,
                   last_error=(dict(zip(("as_of", "error", "at"), last_fail)) if last_fail else None))
        return out
    finally:
        if own:
            conn.close()


def ensure_today(conn=None) -> dict:
    """Refresh unless today's copy is stored and Dhan has published nothing newer since."""
    st = status(conn)
    if st.get("fresh"):
        pub = _published_at()
        if not pub or pub == st.get("published_at"):
            return {"status": "SKIPPED", "reason": "today's scrip master is current", "as_of": st.get("as_of")}
    return refresh(conn, kite=False)


def run_scheduled() -> dict:
    """The 08:00 IST job: every day (renames take effect on any session). A no-op on the view-only server."""
    from ops.role import is_viewer
    if is_viewer():
        return {"status": "SKIPPED", "reason": "view-only copy: the Mac refreshes the instrument master"}
    if not settings()["enabled"]:
        return {"status": "SKIPPED", "reason": "instrument_master.enabled is false"}
    return refresh()


# ── lookup ────────────────────────────────────────────────────────────────
def _invalidate():
    with _lock:
        _cache.update(key=None, rows={}, stamp=None, checked=0.0)


def _db_key(conn):
    if conn is None:
        from db import schema
        return str(schema.DB_PATH)
    try:
        r = conn.execute("PRAGMA database_list").fetchone()
        return str(r[2])
    except Exception:
        return "external"


def _rank(r):
    s = r["series"] or ""
    return (SOURCE_ORDER.index(r["source"]) if r["source"] in SOURCE_ORDER else 9,
            SERIES_ORDER.index(s) if s in SERIES_ORDER else len(SERIES_ORDER))


def _load(conn) -> tuple:
    try:
        stamp = conn.execute("SELECT MAX(updated_at) FROM instrument_master").fetchone()[0]
        recs = conn.execute("SELECT source, exchange, symbol, series, security_id, instrument_type, name, tick_size, "
                            "lot_size, isin, as_of FROM instrument_master WHERE exchange=?", (NSE,)).fetchall()
    except Exception:
        return None, {}
    best = {}
    for rec in recs:
        r = {"source": rec[0], "exchange": rec[1], "symbol": rec[2], "series": rec[3], "security_id": rec[4],
             "instrument_type": rec[5], "name": rec[6], "tick_size": rec[7], "lot_size": rec[8], "isin": rec[9],
             "as_of": rec[10]}
        cur = best.get(r["symbol"])
        if cur is None or _rank(r) < _rank(cur):
            best[r["symbol"]] = r
    return stamp, best


def _rows(conn=None) -> dict:
    key = (_db_key(conn), _today())
    now = time.monotonic()
    with _lock:
        fresh = _cache["key"] == key and now - _cache["checked"] < REVALIDATE_SECONDS
        if fresh:
            return _cache["rows"]
    from db.schema import get_connection
    c = conn or get_connection()
    try:
        try:
            stamp = c.execute("SELECT MAX(updated_at) FROM instrument_master").fetchone()[0]
        except Exception:
            stamp = None
        with _lock:
            if _cache["key"] == key and _cache["stamp"] == stamp and stamp is not None:
                _cache["checked"] = now
                return _cache["rows"]
        stamp, rows = _load(c)
    finally:
        if conn is None:
            c.close()
    with _lock:
        _cache.update(key=key, rows=rows, stamp=stamp, checked=now)
    return rows


def lookup(symbol, conn=None) -> dict | None:
    """The instrument for an NSE symbol (an old ticker resolves through ALIASES): symbol, security_id, tick_size
    (rupees), lot_size, series, instrument_type, isin, source, as_of -- or None when the master does not know it
    (none stored yet, or a symbol NSE does not list). Cheap: a dictionary read after the day's first call."""
    s = clean(symbol)
    if not s:
        return None
    rows = _rows(conn)
    r = rows.get(s) or (rows.get(ALIASES[s]) if s in ALIASES else None)
    if not r:
        return None
    out = dict(r)
    out["requested"] = s
    out["equity"] = (r.get("series") or "") not in NON_EQUITY_SERIES
    return out


def resolve_security(symbol, sec_map: dict = None) -> dict:
    """For data/dhan.get_security_id on a miss: the alias's entry in Dhan's equity map, else a Dhan row of the
    instrument master (a REIT, an ETF, an SME series the equity map leaves out) as {"security_id", "exchange"}.
    {} when neither knows the symbol."""
    s = clean(symbol)
    alias = ALIASES.get(s)
    if alias and sec_map and alias in sec_map:
        return sec_map[alias]
    try:
        r = lookup(s)
    except Exception:
        r = None
    if r and r.get("source") == "dhan" and r.get("security_id"):
        return {"security_id": str(r["security_id"]), "exchange": "NSE_EQ", "symbol": r["symbol"],
                "series": r.get("series")}
    return {}


def is_equity_symbol(symbol, conn=None) -> bool:
    """False for a REIT / InvIT row of the master; True otherwise (unknown symbols included)."""
    r = lookup(symbol, conn)
    return True if r is None else r["equity"]


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--lookup", nargs="*")
    ap.add_argument("--kite-csv")
    a = ap.parse_args()
    if a.refresh:
        print(json.dumps(refresh(), default=str, indent=1))
    if a.kite_csv:
        from db.schema import get_connection
        c = get_connection()
        n = store(c, load_kite_csv(a.kite_csv), "kite")
        _log(c, "kite", _today(), "SUCCESS", n)
        c.commit(); c.close()
        print(f"{n} Kite instruments stored")
    if a.status:
        print(json.dumps(status(), default=str, indent=1))
    for s in a.lookup or []:
        print(s, lookup(s))
