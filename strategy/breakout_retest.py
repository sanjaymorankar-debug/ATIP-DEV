"""
The Kite "high-volume breakout and retest" scanner, ported to ATIP's own daily candles (KGF-10, owner prompt 15 Nov 2026
item 10; evidence .ux_build/kite-gap-2026-11-15/). Pure functions first, then the database edge. Every time is IST.

THE RULE (exact parameters, one pass per stock)
    normalise(candles)      dates as YYYY-MM-DD, numeric OHLCV, oldest first; a repeated date keeps its last row;
                            a row without a date or a positive open / high / low / close is dropped
    >= 25 candles           (MIN_CANDLES) or the stock is not scanned
    candidates              from the second-last candle backward to the earliest eligible one: >= 20 candles before it
                            (PRICE_LOOKBACK, VOL_LOOKBACK) and <= 20 candles before the latest (MAX_AGE)
    breakout candle i       avg = mean volume of the 20 candles before i, prev_high = highest high of the 20 before i
                            (i itself excluded from both); accept when avg > 0, close > open (green),
                            volume >= 3 x avg (VOL_MULT) and close > prev_high
    later high              the highest high after i, up to the latest candle, must be above i's high
    retest                  midpoint = (high + low) / 2 of i; distance = (current - midpoint) / midpoint x 100;
                            age = candles after i; accept when |distance| <= 1 (MID_TOL_PCT) and age <= 20
    one per stock           the most recent candidate that passes everything (the scan stops at it)
    rank                    by age, then |distance| (then symbol, so a tie never reorders between runs)

    current = the latest candle's close, or the price the caller passes (scan_symbol(current_price=...)). The stored
    runs use the close of the scan date, so a run is reproducible from prices_daily alone.

KITE OUTPUT FIELDS (names exactly as in the Kite spec)
    symbol, companyName, currentPrice, breakoutDate, bIndex, daysSince, breakoutLow, breakoutHigh, midpoint,
    distanceFromMidpoint (%), volMultiple (x), prevHigh, maxPriceAfter, returnFromCloseToSubHigh (% from the
    breakout close to maxPriceAfter: "Peak gains"), retracementHigh (% from maxPriceAfter down to current), candles.
    `candles` is the window the rule read -- the 20 candles before the breakout through the latest -- not the whole
    history (a stored run would otherwise carry ~1,700 candles a stock); candles[bIndex] is the breakout candle.

ATIP ADDITIONS ON EACH ROW
    buy_price   the breakout candle's low rounded DOWN to the tick (orders/ticks.to_tick BUY / ENTRY): the Kite rule
                bids a resting buy at the structural level, never at LTP (KGF-11)
    stop_price  buy_price x 0.95 rounded DOWN (to_tick SELL / STOP, Decimal via ticks.mul): 5 % under the bid
    tick_size   one tick per stock (the instrument master's, else NSE's band for the breakout low), used for both
                prices -- JKCEMENT 5,136.00 x 0.95 = 4,879.20 -> 4,879.00 on its 0.50 tick, as the study bid it on
                the 15 Nov backup; the live scan of 16 Nov 2026 bids 5,136.30 / 4,879.40, Dhan's newer instrument
                master giving JKCEMENT a 0.10 tick (the test's 13 Nov reference stays on the backup's master)
    nifty50 / next50 / midcap150 / smallcap250 / nifty500
                index membership where ATIP has the list (index_flags); None = no list on this machine, not "no"
    rank, breakoutClose, avgVolume, lastDate

THE DATABASE EDGE
    scan_universe(conn, as_of)  prices_daily (series 'EQ', dates <= as_of) for the symbols scored in ai_scores on the
                                latest scored date <= as_of. A stock with no candle on the scan date (suspended, moved
                                to BE) is skipped and counted, as the 13 Nov study did: its "current" price would be old.
    run_scan(conn, as_of)       scan_universe, ranked, stored in algo_scan_runs / algo_scan_results (one transaction)
    latest_scan(conn)           the last stored run, rows in rank order: the ranking stays fixed until the next run
    live_prices(conn, symbols)  the page's current price: the newest WebSocket LTP today (live_quotes source
                                dhan_ws), else the newest OHLC snapshot LTP today (dhan / dhan_rest), else the last close

    Reads candles from CANDLE_WINDOW_DAYS calendar days: the rule never looks further back than 41 candles (20 before
    the earliest eligible breakout, 20 after it, the latest), and 120 days hold ~80 sessions. Nothing here places,
    changes or cancels an order.

    python -m strategy.breakout_retest [--as-of YYYY-MM-DD] [--db PATH] [--store]      prints the ranked setups
"""

from __future__ import annotations

import csv
import json
import logging
import math
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("atip.breakout_retest")

IST = ZoneInfo("Asia/Kolkata")

VOL_LOOKBACK = 20
VOL_MULT = 3
PRICE_LOOKBACK = 20
MAX_AGE = 20
MID_TOL_PCT = 1.0
MIN_CANDLES = 25
STOP_FACTOR = "0.95"                 # stop = buy x 0.95 (5 % under the bid), Decimal-exact through orders/ticks.mul
SCANNER = "breakout_retest"
TITLE = "High-volume breakout and retest setups"
CANDLE_WINDOW_DAYS = 120
PARAMS = {"VOL_LOOKBACK": VOL_LOOKBACK, "VOL_MULT": VOL_MULT, "PRICE_LOOKBACK": PRICE_LOOKBACK, "MAX_AGE": MAX_AGE,
          "MID_TOL_PCT": MID_TOL_PCT, "MIN_CANDLES": MIN_CANDLES, "STOP_FACTOR": float(STOP_FACTOR)}

# Index lists ATIP may hold under atip_data/raw (NSE's ind_*list.csv files). Only ind_nifty500list.csv is downloaded
# today (data/index_constituents.py); the others are read when someone drops them there. Never downloaded from here.
RAW_DIR = Path("atip_data") / "raw"
INDEX_FILES = {"nifty50": "ind_nifty50list.csv", "next50": "ind_niftynext50list.csv",
               "midcap150": "ind_niftymidcap150list.csv", "smallcap250": "ind_niftysmallcap250list.csv",
               "nifty500": "ind_nifty500list.csv"}


# -- normalisation ------------------------------------------------------------------------------------------------------

def _num(v) -> float | None:
    """A finite float from a number or a numeric string ("1,234.50", " 98.2 "); None for anything else."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        x = float(v)
    else:
        s = str(v).strip().replace(",", "").replace("₹", "")
        if not s:
            return None
        try:
            x = float(s)
        except ValueError:
            return None
    return x if math.isfinite(x) else None


_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%b-%Y", "%d %b %Y",
                 "%d-%B-%Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%d-%b-%y", "%m/%d/%Y")


def _day(v) -> str | None:
    """The trading day of a candle as YYYY-MM-DD. A zoned timestamp is read in IST (Kite's "2026-11-13T00:00:00+0530",
    or the same instant in UTC "2026-11-12T18:30:00Z", is 13 Nov); a naive one is taken as written; epoch seconds or
    milliseconds are IST; NSE's "13-Nov-2026" and the day-first "13-11-2026" / "13/11/2026" are understood."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, datetime):
        return (v.astimezone(IST) if v.tzinfo else v).date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (int, float)):
        x = float(v)
        if not math.isfinite(x) or x <= 0:
            return None
        if x >= 1e11:                              # epoch milliseconds
            x /= 1000.0
        try:
            return datetime.fromtimestamp(x, IST).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    s = str(v).strip()
    if not s:
        return None
    if len(s) > 10 and s[:4].isdigit() and s[4] in "-/":
        iso = s.replace("/", "-", 2).replace("Z", "+00:00")
        if len(iso) >= 5 and iso[-5] in "+-" and iso[-4:].isdigit() and ":" not in iso[-5:]:
            iso = iso[:-2] + ":" + iso[-2:]       # +0530 -> +05:30
        try:
            d = datetime.fromisoformat(iso)
            return (d.astimezone(IST) if d.tzinfo else d).date().isoformat()
        except ValueError:
            s = s[:10]                             # "2026-11-13 00:00:00.000000000" and the like: the date part
    for f in _DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date().isoformat()
        except ValueError:
            continue
    return None


def _fields(c):
    """(date, open, high, low, close, volume) from a dict (any key case; o/h/l/c/v accepted), a sqlite3.Row, or a
    Kite-style list [date, open, high, low, close, volume]."""
    if c is None:
        return None
    if not isinstance(c, dict) and hasattr(c, "keys"):
        c = {k: c[k] for k in c.keys()}
    if isinstance(c, dict):
        k = {str(a).strip().lower(): b for a, b in c.items()}
        pick = lambda *names: next((k[n] for n in names if n in k and k[n] is not None), None)  # noqa: E731
        return (pick("date", "timestamp", "datetime", "time", "day"), pick("open", "o"), pick("high", "h"),
                pick("low", "l"), pick("close", "c", "ltp"), pick("volume", "v", "vol", "qty"))
    if isinstance(c, (list, tuple)) and len(c) >= 5:
        return tuple(c[:6]) if len(c) >= 6 else (*c[:5], None)
    return None


def normalise(candles) -> list[dict]:
    """Daily candles as [{"date": "YYYY-MM-DD", "open", "high", "low", "close", "volume"}], oldest first, one per date
    (the last row given for a date wins). Prices are floats; volume is a number (0 when missing)."""
    by_day: dict = {}
    for c in candles or []:
        f = _fields(c)
        if not f:
            continue
        d = _day(f[0])
        o, h, l, cl = (_num(x) for x in f[1:5])
        if not d or None in (o, h, l, cl) or min(o, h, l, cl) <= 0:
            continue
        v = _num(f[5])
        v = 0.0 if v is None or v < 0 else v
        by_day[d] = {"date": d, "open": o, "high": h, "low": l, "close": cl,
                     "volume": int(v) if float(v).is_integer() else v}
    return [by_day[d] for d in sorted(by_day)]


# -- the rule -----------------------------------------------------------------------------------------------------------

def _scan(symbol: str, cs: list, company_name=None, current_price=None) -> dict | None:
    """scan_symbol on candles normalise() has already produced."""
    n = len(cs)
    if n < MIN_CANDLES:
        return None
    o = [c["open"] for c in cs]
    h = [c["high"] for c in cs]
    lo = [c["low"] for c in cs]
    cl = [c["close"] for c in cs]
    v = [float(c["volume"]) for c in cs]
    t = n - 1
    cur = _num(current_price)
    if not cur or cur <= 0:
        cur = cl[t]
    first = max(PRICE_LOOKBACK, VOL_LOOKBACK, t - MAX_AGE)       # >= 20 candles before it, <= 20 before the latest
    for i in range(n - 2, first - 1, -1):
        avg = sum(v[i - VOL_LOOKBACK:i]) / VOL_LOOKBACK
        prev_high = max(h[i - PRICE_LOOKBACK:i])
        if not (avg > 0 and cl[i] > o[i] and v[i] >= VOL_MULT * avg and cl[i] > prev_high):
            continue
        after_high = max(h[i + 1:])
        if not after_high > h[i]:
            continue
        mid = (h[i] + lo[i]) / 2
        dist = (cur - mid) / mid * 100
        age = t - i
        if abs(dist) <= MID_TOL_PCT and age <= MAX_AGE:
            w0 = max(0, i - PRICE_LOOKBACK)
            return {
                "symbol": symbol, "companyName": company_name or "", "currentPrice": round(cur, 2),
                "breakoutDate": cs[i]["date"], "bIndex": i - w0, "daysSince": age,
                "breakoutLow": lo[i], "breakoutHigh": h[i], "midpoint": round(mid, 4),
                "distanceFromMidpoint": round(dist, 4), "volMultiple": round(v[i] / avg, 4), "prevHigh": prev_high,
                "maxPriceAfter": after_high, "returnFromCloseToSubHigh": round((after_high / cl[i] - 1) * 100, 4),
                "retracementHigh": round((after_high - cur) / after_high * 100, 4),
                "candles": [dict(c) for c in cs[w0:]],
                "breakoutClose": cl[i], "avgVolume": round(avg, 2), "lastDate": cs[t]["date"],
            }
    return None


def scan_symbol(symbol: str, candles, company_name: str | None = None, current_price=None) -> dict | None:
    """The stock's most recent breakout-retest setup, or None. Pure: candles in any shape normalise() takes."""
    return _scan(symbol, normalise(candles), company_name, current_price)


def rank_rows(rows: list) -> list:
    """Setups in rank order (age, then |distance|, then symbol), each with its 1-based "rank"."""
    out = sorted(rows, key=lambda r: (r["daysSince"], abs(r["distanceFromMidpoint"]), r["symbol"]))
    for k, r in enumerate(out, 1):
        r["rank"] = k
    return out


def entry_prices(symbol: str, breakout_low: float, conn=None) -> dict:
    """What the order engine bids for a setup (the Kite rule, KGF-11): buy at the breakout low rounded DOWN to the tick,
    stop at buy x 0.95 rounded DOWN, both on the one tick of the stock. Never the LTP."""
    from orders.ticks import mul, tick_size, to_tick
    tick = tick_size(symbol, breakout_low, conn)
    buy = to_tick(breakout_low, "BUY", "ENTRY", symbol=symbol, tick=tick, conn=conn)
    stop = to_tick(mul(buy, STOP_FACTOR), "SELL", "STOP", symbol=symbol, tick=tick, conn=conn)
    return {"buy_price": buy, "stop_price": stop, "tick_size": float(tick)}


# -- index membership and names -----------------------------------------------------------------------------------------

def _read_list(path: Path) -> dict | None:
    """{SYMBOL: company name} from an NSE ind_*list.csv, or None when the file is missing / unreadable."""
    try:
        with path.open(newline="", encoding="utf-8-sig") as f:
            out = {}
            for row in csv.DictReader(f):
                k = {str(a).strip().lower(): (b or "").strip() for a, b in row.items() if a}
                sym = k.get("symbol", "").upper()
                if sym and not sym.startswith("DUMMY"):
                    out[sym] = k.get("company name", "")
            return out or None
    except Exception:
        return None


def index_flags(conn=None, raw_dir: Path | None = None) -> tuple[dict, dict]:
    """({flag: set of symbols or None}, {symbol: company name}). Lists come from the NSE CSVs under atip_data/raw when
    present; Nifty 50 falls back to the pre-open snapshot's in_nifty50 (the newest day that marked any). A flag whose
    list ATIP does not have is None, so a row says "unknown" rather than "not in the index". Cheap: no download."""
    raw = Path(raw_dir) if raw_dir else RAW_DIR
    sets, names = {}, {}
    for flag, fname in INDEX_FILES.items():
        lst = _read_list(raw / fname)
        sets[flag] = set(lst) if lst else None
        for s, nm in (lst or {}).items():
            if nm and s not in names:
                names[s] = nm
    if sets["nifty50"] is None and conn is not None:
        try:
            day = conn.execute("SELECT MAX(date) FROM preopen_snapshot WHERE in_nifty50=1").fetchone()[0]
            if day:
                sets["nifty50"] = {r[0] for r in conn.execute(
                    "SELECT symbol FROM preopen_snapshot WHERE date=? AND in_nifty50=1", (day,))}
        except Exception:
            pass
    return sets, names


def _flags_for(sym: str, sets: dict) -> dict:
    return {k: (None if s is None else sym in s) for k, s in sets.items()}


# -- the database edge --------------------------------------------------------------------------------------------------

def _iso(v) -> str | None:
    return _day(v) if v is not None else None


def scan_universe(conn, as_of: str | None = None, raw_dir: Path | None = None) -> dict:
    """Scan the scored universe on prices_daily. {"scanner", "as_of" (the scan date: the newest candle <= as_of),
    "scored_date", "universe", "scanned", "skipped" {reason: count}, "params", "elapsed_ms", "rows" (ranked)}."""
    t0 = time.monotonic()
    want = _iso(as_of) if as_of else None
    q = "SELECT MAX(date) FROM ai_scores" + (" WHERE date<=?" if want else "")
    scored = _iso(conn.execute(q, (want,) if want else ()).fetchone()[0])
    out = {"scanner": SCANNER, "as_of": want, "scored_date": scored, "universe": 0, "scanned": 0, "skipped": {},
           "params": dict(PARAMS), "rows": []}
    if not scored:
        out["elapsed_ms"] = round((time.monotonic() - t0) * 1000)
        return out
    syms = sorted({r[0] for r in conn.execute("SELECT DISTINCT symbol FROM ai_scores WHERE date=?", (scored,))})
    out["universe"] = len(syms)
    if not syms:
        return out
    marks = ",".join("?" * len(syms))
    end = want or _iso(conn.execute(f"SELECT MAX(date) FROM prices_daily WHERE series='EQ' AND symbol IN ({marks})",
                                    syms).fetchone()[0])
    if not end:
        out["skipped"] = {"no EQ candles": len(syms)}
        return out
    start = (date.fromisoformat(end) - timedelta(days=CANDLE_WINDOW_DAYS)).isoformat()
    by_sym: dict = {}
    for r in conn.execute(f"SELECT symbol, date, open, high, low, close, volume FROM prices_daily WHERE series='EQ' "
                          f"AND date>=? AND date<=? AND symbol IN ({marks}) ORDER BY symbol, date", (start, end, *syms)):
        by_sym.setdefault(r[0], []).append(tuple(r)[1:])
    cs_by = {s: normalise(by_sym.get(s, [])) for s in syms}
    scan_day = max((cs[-1]["date"] for cs in cs_by.values() if cs), default=None)
    out["as_of"] = scan_day
    sets, names = index_flags(conn, raw_dir)
    skipped: dict = {}
    rows = []
    for s in syms:
        cs = cs_by[s]
        if len(cs) < MIN_CANDLES:
            skipped[f"fewer than {MIN_CANDLES} candles"] = skipped.get(f"fewer than {MIN_CANDLES} candles", 0) + 1
            continue
        if cs[-1]["date"] != scan_day:
            skipped[f"no candle on {scan_day}"] = skipped.get(f"no candle on {scan_day}", 0) + 1
            continue
        out["scanned"] += 1
        r = _scan(s, cs, names.get(s))
        if r:
            r.update(entry_prices(s, r["breakoutLow"], conn))
            r.update(_flags_for(s, sets))
            rows.append(r)
    out["skipped"] = skipped
    out["rows"] = rank_rows(rows)
    out["elapsed_ms"] = round((time.monotonic() - t0) * 1000)
    return out


def ensure_tables(conn) -> None:
    """The scanner's two tables (additive; nothing else in the schema changes)."""
    conn.execute("""CREATE TABLE IF NOT EXISTS algo_scan_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, scanner TEXT NOT NULL DEFAULT 'breakout_retest',
        as_of DATE, scored_date DATE, run_at TEXT NOT NULL, universe INTEGER, scanned INTEGER, results INTEGER,
        skipped TEXT, params TEXT, elapsed_ms INTEGER, source TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS algo_scan_results (
        run_id INTEGER NOT NULL, rank INTEGER NOT NULL, symbol TEXT NOT NULL, breakout_date DATE, days_since INTEGER,
        distance_pct REAL, buy_price REAL, stop_price REAL, tick_size REAL, current_price REAL, data TEXT NOT NULL,
        PRIMARY KEY (run_id, rank))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_algo_scan_runs_scanner ON algo_scan_runs(scanner, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_algo_scan_results_symbol ON algo_scan_results(symbol, run_id)")


def _now_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def run_scan(conn, as_of: str | None = None, source: str = "api", raw_dir: Path | None = None) -> dict:
    """Scan, rank and store one run (algo_scan_runs + its algo_scan_results, one transaction); returns it as
    latest_scan() would. The stored ranking is what the page and the order engine read until the next run."""
    res = scan_universe(conn, as_of, raw_dir=raw_dir)
    ensure_tables(conn)
    run_at = _now_ist()
    cur = conn.execute(
        "INSERT INTO algo_scan_runs (scanner, as_of, scored_date, run_at, universe, scanned, results, skipped, params, "
        "elapsed_ms, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (SCANNER, res["as_of"], res["scored_date"], run_at, res["universe"], res["scanned"], len(res["rows"]),
         json.dumps(res["skipped"]), json.dumps(res["params"]), res.get("elapsed_ms"), source))
    run_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO algo_scan_results (run_id, `rank`, symbol, breakout_date, days_since, distance_pct, buy_price, "
        "stop_price, tick_size, current_price, data) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(run_id, r["rank"], r["symbol"], r["breakoutDate"], r["daysSince"], r["distanceFromMidpoint"], r["buy_price"],
          r["stop_price"], r["tick_size"], r["currentPrice"], json.dumps(r, default=str)) for r in res["rows"]])
    conn.commit()
    log.info(f"  breakout-retest scan {run_id}: {len(res['rows'])} setups of {res['scanned']} scanned, as of "
             f"{res['as_of']} ({res.get('elapsed_ms')} ms)")
    return {**res, "run_id": run_id, "run_at": run_at, "source": source, "stored": True}


def latest_scan(conn) -> dict | None:
    """The newest stored run with its rows in rank order, or None when no run is stored (or the tables do not exist,
    e.g. a viewer's copied database from before this change)."""
    try:
        run = conn.execute("SELECT id, as_of, scored_date, run_at, universe, scanned, results, skipped, params, "
                           "elapsed_ms, source FROM algo_scan_runs WHERE scanner=? ORDER BY id DESC LIMIT 1",
                           (SCANNER,)).fetchone()
    except Exception:
        return None
    if not run:
        return None
    run = tuple(run)
    rows = [json.loads(r[0]) for r in conn.execute(
        "SELECT data FROM algo_scan_results WHERE run_id=? ORDER BY `rank`", (run[0],))]
    return {"scanner": SCANNER, "run_id": run[0], "as_of": _iso(run[1]), "scored_date": _iso(run[2]),
            "run_at": run[3], "universe": run[4], "scanned": run[5], "skipped": json.loads(run[7] or "{}"),
            "params": json.loads(run[8] or "{}"), "elapsed_ms": run[9], "source": run[10], "rows": rows,
            "stored": True}


def live_prices(conn, symbols, now: datetime | None = None) -> dict:
    """{symbol: {"ltp", "source": "ws" | "snapshot" | "close", "at"}}: the newest WebSocket LTP today (live_quotes
    rows from the stock feed, source dhan_ws), else the newest OHLC snapshot LTP today (the 15-minute quote poll
    'dhan' or the feed's REST failover 'dhan_rest'), else the last EQ close in prices_daily. A WebSocket price older
    than a snapshot by more than a minute gives way to the snapshot (the socket stopped, the poll did not)."""
    syms = sorted({s for s in symbols or [] if s})
    if not syms:
        return {}
    now = now or datetime.now(IST).replace(tzinfo=None)
    day = now.strftime("%Y-%m-%d")
    marks = ",".join("?" * len(syms))
    best: dict = {}
    try:
        for sym, ltp, ts, src in conn.execute(
                f"SELECT symbol, ltp, REPLACE(SUBSTR(timestamp,1,19),'T',' ') ts, COALESCE(source,'') FROM live_quotes "
                f"WHERE symbol IN ({marks}) AND timestamp>=? ORDER BY ts", (*syms, day)):
            x = _num(ltp)
            if not x or x <= 0:
                continue
            kind = "ws" if "ws" in str(src).lower() else "snapshot"
            best.setdefault(sym, {})[kind] = {"ltp": x, "source": kind, "at": ts}
    except Exception as e:                       # no live_quotes table (fresh install)
        log.debug(f"  scanner live prices: {e}")
    out = {}
    for s in syms:
        b = best.get(s, {})
        ws, snap = b.get("ws"), b.get("snapshot")
        if ws and snap and ws["at"] < snap["at"]:
            try:
                gap = (datetime.fromisoformat(snap["at"]) - datetime.fromisoformat(ws["at"])).total_seconds()
            except ValueError:
                gap = 0
            if gap > 60:
                ws = None
        if ws or snap:
            out[s] = ws or snap
    for s in syms:
        if s in out:
            continue
        try:
            r = conn.execute("SELECT close, date FROM prices_daily WHERE symbol=? AND series='EQ' AND close>0 "
                             "ORDER BY date DESC LIMIT 1", (s,)).fetchone()
        except Exception:
            r = None
        if r and r[0]:
            out[s] = {"ltp": float(r[0]), "source": "close", "at": _iso(r[1])}
    return out


def _main(argv=None) -> int:
    import argparse
    import sqlite3
    ap = argparse.ArgumentParser(description=TITLE)
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--db", default=None, help="a database file to read (opened read-only unless --store)")
    ap.add_argument("--store", action="store_true", help="store the run (algo_scan_runs / algo_scan_results)")
    a = ap.parse_args(argv)
    if a.db:
        conn = sqlite3.connect(a.db if a.store else f"file:{a.db}?mode=ro", uri=not a.store)
    else:
        from db.schema import get_connection
        conn = get_connection()
    try:
        res = run_scan(conn, a.as_of, source="cli") if a.store else scan_universe(conn, a.as_of)
    finally:
        conn.close()
    print(f"{TITLE}: {len(res['rows'])} of {res['scanned']} scanned (universe {res['universe']}, scored "
          f"{res['scored_date']}), as of {res['as_of']}; skipped {res['skipped'] or 'none'}")
    for r in res["rows"]:
        print(f"{r['rank']:>3} {r['symbol']:<12} {r['breakoutDate']} age {r['daysSince']:>2}  cur {r['currentPrice']:>10.2f}"
              f"  vol {r['volMultiple']:5.2f}x  dist {r['distanceFromMidpoint']:+6.2f}%  peak "
              f"{r['returnFromCloseToSubHigh']:+6.2f}%  retr {r['retracementHigh']:+6.2f}%  buy {r['buy_price']:.2f}"
              f"  stop {r['stop_price']:.2f}  tick {r['tick_size']:g}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
