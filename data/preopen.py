"""
NSE pre-open session capture (PO-01..PO-03): the call auction from 09:00 to 09:08 IST is the first
time anyone sees the real queue of buy and sell orders for the day -- after-market orders (AMOs) sit
inside each broker until 09:00 and nobody publishes them. NSE's pre-open page shows, per stock, the
indicative equilibrium price (IEP) the auction is converging on and the total buy and sell quantity
waiting at that moment. Order entry closes at a random second between 09:07 and 09:08, the auction
matches at the IEP, and normal trading starts at 09:15. This keeps the final picture once a day.

Source: https://www.nseindia.com/api/market-data-pre-open?key=<KEY>  (KEY: NIFTY, BANKNIFTY, FO, ALL,
SME, OTHERS), through data/nse_api.py (cookies, throttling, the "nse" breaker). Per item:
    metadata                symbol, previousClose, iep, pChange, finalQuantity, lastPrice
    detail.preOpenMarket    IEP, prevClose, perChange, finalQuantity, totalBuyQuantity, totalSellQuantity,
                            atoBuyQty / atoSellQty (at-the-open market orders), lastUpdateTime
Both blocks are read, the auction block first; an empty or zero IEP means no price was discovered.

    preopen_snapshot   (date, symbol): nse_time, prev_close, iep, iep_chg_pct, final_qty,
                       total_buy_qty / total_sell_qty, imbalance = (buy - sell) / (buy + sell)   -1 .. +1,
                       ato_buy_qty / ato_sell_qty, pressure (data/order_pressure.label's bands), in_nifty50,
                       and after the close (evaluate): open_price, close_price, iep_error_bps = official
                       open vs the IEP, first15_pct (the 09:15 15-minute bar, data/dhan.py's intraday_bars)
                       and open_to_close_pct

capture() refuses a page whose lastUpdateTime is not today (before 09:00 and on holidays NSE still
shows the previous session's auction) -- STALE, nothing stored. evaluate() fills the outcomes once
prices_daily has the day (post-market, 16:45); a day still unpriced after EVAL_GIVE_UP_DAYS is closed
with empty outcomes so it stops being retried.

Reading it honestly: the auction's job is to discover the opening price, so the imbalance mostly
explains the IEP itself -- already the gap. Whether it says anything about the move AFTER 09:15 is an
open question, and record() answers it from ATIP's own stored days: the hit rate of the imbalance's
sign against the first 15 minutes and against open -> close for one-sided books (|imbalance| >=
threshold), against the 50% of a coin, with its standard error, and the same for "the gap continues".
It says INSUFFICIENT until MIN_OBSERVATIONS rows exist and claims an edge only when the hit rate
clears 50% by two standard errors. Context, not a signal: market_pulse shows it without a vote.

Config (config.json "preopen"): {"enabled": true, "keys": ["NIFTY", "FO"]}
Scheduled on trading days: capture 09:09, evaluate 17:10 (pipeline/scheduler.py).
CLI: python -m data.preopen capture | evaluate | now [--side buy|sell] [--nifty50] | record [--days 120]
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

URL = "https://www.nseindia.com/api/market-data-pre-open?key={key}"
DEFAULTS = {"enabled": True, "keys": ["NIFTY", "FO"]}
EVAL_GIVE_UP_DAYS = 7
MIN_OBSERVATIONS = 200
ONE_SIDED = 0.3                       # |imbalance| at which a book counts as one-sided (STRONG_* band)

DDL = (
    """CREATE TABLE IF NOT EXISTS preopen_snapshot (
        date DATE NOT NULL, symbol TEXT NOT NULL, captured_at TIMESTAMP, nse_time TEXT,
        prev_close REAL, iep REAL, iep_chg_pct REAL, final_qty REAL,
        total_buy_qty REAL, total_sell_qty REAL, imbalance REAL, ato_buy_qty REAL, ato_sell_qty REAL,
        pressure TEXT, in_nifty50 INTEGER DEFAULT 0,
        open_price REAL, close_price REAL, iep_error_bps REAL, first15_pct REAL, open_to_close_pct REAL,
        evaluated_at TIMESTAMP, PRIMARY KEY (date, symbol))""",
    "CREATE INDEX IF NOT EXISTS idx_preopen_snapshot_eval ON preopen_snapshot(evaluated_at)",
)

_COLS = ("date", "symbol", "captured_at", "nse_time", "prev_close", "iep", "iep_chg_pct", "final_qty",
         "total_buy_qty", "total_sell_qty", "imbalance", "ato_buy_qty", "ato_sell_qty", "pressure", "in_nifty50")


def settings() -> dict:
    try:
        from ops.config import load
        raw = load().get("preopen") or {}
    except Exception:
        raw = {}
    out = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}
    out["enabled"] = out.get("enabled") is not False
    keys = out.get("keys")
    out["keys"] = [str(k).upper() for k in keys] if isinstance(keys, (list, tuple)) and keys else list(DEFAULTS["keys"])
    return out


def ensure_tables(conn):
    for d in DDL:
        conn.execute(d)


def _f(v):
    if isinstance(v, str):
        v = v.replace(",", "").strip()
        if v in ("", "-"):
            return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _first(*vals):
    for v in vals:
        f = _f(v)
        if f is not None:
            return f
    return None


def nse_time(s) -> datetime | None:
    """NSE's lastUpdateTime ("09-Oct-2026 09:07:59") -> datetime; None when absent or unreadable."""
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M"):
        try:
            return datetime.strptime(str(s).strip(), fmt)
        except (TypeError, ValueError):
            continue
    return None


def parse(item: dict) -> dict | None:
    """One entry of the pre-open payload's "data" list -> the stored fields (None without a symbol
    or without any auction quantities)."""
    if not isinstance(item, dict):
        return None
    from data.order_pressure import label
    md = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    po = (item.get("detail") or {}).get("preOpenMarket") if isinstance(item.get("detail"), dict) else None
    po = po if isinstance(po, dict) else {}
    sym = str(md.get("symbol") or "").strip().upper()
    if not sym:
        return None
    ato = po.get("ato") if isinstance(po.get("ato"), dict) else {}
    buy = _first(po.get("totalBuyQuantity"))
    sell = _first(po.get("totalSellQuantity"))
    if buy is None and sell is None:
        return None
    iep = _first(po.get("IEP"), md.get("iep"))
    iep = iep if iep else None                          # 0 = no price discovered
    prev = _first(po.get("prevClose"), md.get("previousClose"))
    chg = _first(po.get("perChange"), md.get("pChange"))
    if iep and prev and chg is None:
        chg = round((iep / prev - 1) * 100, 2)
    if not iep:
        chg = None
    imb = (round((buy - sell) / (buy + sell), 4)
           if buy is not None and sell is not None and buy + sell > 0 else None)
    t = nse_time(po.get("lastUpdateTime"))
    return {"symbol": sym, "nse_time": t.strftime("%Y-%m-%d %H:%M:%S") if t else None,
            "prev_close": prev, "iep": iep, "iep_chg_pct": chg,
            "final_qty": _first(po.get("finalQuantity"), md.get("finalQuantity")),
            "total_buy_qty": buy, "total_sell_qty": sell, "imbalance": imb,
            "ato_buy_qty": _first(po.get("atoBuyQty"), ato.get("buy")),
            "ato_sell_qty": _first(po.get("atoSellQty"), ato.get("sell")),
            "pressure": label(imb)}


def _fetch(key: str):
    from data.nse_api import client
    return client().json(URL.format(key=key))


def capture(conn=None, day=None, fetch=None) -> dict:
    """Fetch every configured key, merge per symbol (in_nifty50 from the NIFTY key) and store the day's rows.
    fetch(key) -> the decoded JSON (tests inject it; default data/nse_api.py)."""
    from db.schema import get_connection
    cfg = settings()
    day = str(day or date.today())
    fetch = fetch or _fetch
    rows, failed = {}, []
    for key in cfg["keys"]:
        payload = fetch(key)
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            failed.append(key)
            continue
        for it in items:
            p = parse(it)
            if not p:
                continue
            r = rows.setdefault(p["symbol"], {**p, "in_nifty50": 0})
            if key == "NIFTY":
                r["in_nifty50"] = 1
    if not rows:
        return {"status": "FAILED" if failed else "EMPTY", "rows": 0, "failed_keys": failed,
                "error": "no pre-open payload from NSE" if failed else None}
    stamps = sorted(r["nse_time"] for r in rows.values() if r["nse_time"])
    if stamps and stamps[-1][:10] != day:
        return {"status": "STALE", "rows": 0, "nse_time": stamps[-1],
                "reason": f"NSE's pre-open page is from {stamps[-1][:10]}, not {day} (before 09:00, or a holiday)"}
    own = conn is None
    conn = conn or get_connection()
    try:
        ensure_tables(conn)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sql = (f"INSERT OR REPLACE INTO preopen_snapshot ({', '.join(_COLS)}) "
               f"VALUES ({', '.join('?' for _ in _COLS)})")
        for r in rows.values():
            r.update(date=day, captured_at=now)
            conn.execute(sql, tuple(r[c] for c in _COLS))
        conn.commit()
    finally:
        if own:
            conn.close()
    return {"status": "SUCCESS", "rows": len(rows), "date": day, "nse_time": stamps[-1] if stamps else None,
            "failed_keys": failed, "nifty50": sum(r["in_nifty50"] for r in rows.values())}


def evaluate(conn=None, today=None) -> dict:
    """Fill each unevaluated row's outcome from prices_daily (open, close) and the 09:15 15-minute bar.
    Rows of a day not yet priced wait; after EVAL_GIVE_UP_DAYS they are closed with empty outcomes."""
    from db.schema import get_connection
    today = today or date.today()
    own = conn is None
    conn = conn or get_connection()
    try:
        ensure_tables(conn)
        pending = conn.execute("SELECT date, symbol, iep FROM preopen_snapshot WHERE evaluated_at IS NULL "
                               "ORDER BY date").fetchall()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        done = waiting = given_up = 0
        for d, sym, iep in pending:
            d = str(d)[:10]
            px = conn.execute("SELECT open, close FROM prices_daily WHERE symbol=? AND date=?", (sym, d)).fetchone()
            if not px or not px[0] or px[1] is None:
                if (today - date.fromisoformat(d)).days > EVAL_GIVE_UP_DAYS:
                    conn.execute("UPDATE preopen_snapshot SET evaluated_at=? WHERE date=? AND symbol=?", (now, d, sym))
                    given_up += 1
                else:
                    waiting += 1
                continue
            op, cl = float(px[0]), float(px[1])
            bar = conn.execute("SELECT open, close FROM intraday_bars WHERE symbol=? AND interval_min=15 AND ts>=? "
                               "AND ts<? ORDER BY ts LIMIT 1", (sym, f"{d} 09:15", f"{d} 09:16")).fetchone()
            f15 = round((bar[1] / bar[0] - 1) * 100, 3) if bar and bar[0] and bar[1] is not None else None
            conn.execute("UPDATE preopen_snapshot SET open_price=?, close_price=?, iep_error_bps=?, first15_pct=?, "
                         "open_to_close_pct=?, evaluated_at=? WHERE date=? AND symbol=?",
                         (op, cl, round((op / iep - 1) * 1e4, 1) if iep else None, f15,
                          round((cl / op - 1) * 100, 3), now, d, sym))
            done += 1
        conn.commit()
    finally:
        if own:
            conn.close()
    return {"status": "SUCCESS" if done or given_up else "EMPTY", "rows": done, "waiting": waiting,
            "given_up": given_up}


def _dicts(cur) -> list:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def latest(conn, day=None, side=None, nifty50=False, limit=100) -> list:
    """A day's rows (the newest captured day when none is given), most one-sided book first."""
    ensure_tables(conn)
    if day is None:
        r = conn.execute("SELECT MAX(date) FROM preopen_snapshot").fetchone()
        if not r or not r[0]:
            return []
        day = r[0]
    sql = "SELECT * FROM preopen_snapshot WHERE date=? AND imbalance IS NOT NULL"
    if nifty50:
        sql += " AND in_nifty50=1"
    if side == "buy":
        sql += " AND imbalance>0 ORDER BY imbalance DESC"
    elif side == "sell":
        sql += " AND imbalance<0 ORDER BY imbalance ASC"
    else:
        sql += " ORDER BY ABS(imbalance) DESC"
    return _dicts(conn.execute(sql + ", symbol LIMIT ?", (str(day)[:10], int(limit))))


def summary(conn, day=None) -> dict:
    """Breadth of the day's auction: how many stocks indicate up / down, how many books lean each way,
    the median imbalance (per stock, so one illiquid name's lakh-share book does not swamp the rest),
    the same for the Nifty 50, and the most one-sided names."""
    rows = latest(conn, day=day, limit=100000)
    if not rows:
        return {"status": "NO_DATA"}

    def breadth(rs):
        chg = [r["iep_chg_pct"] for r in rs if r["iep_chg_pct"] is not None]
        imb = [r["imbalance"] for r in rs]
        return {"stocks": len(rs), "advances": sum(1 for c in chg if c > 0), "declines": sum(1 for c in chg if c < 0),
                "unchanged": sum(1 for c in chg if c == 0),
                "median_iep_chg_pct": round(statistics.median(chg), 2) if chg else None,
                "median_imbalance": round(statistics.median(imb), 3) if imb else None,
                "buy_side_books": sum(1 for x in imb if x > 0.1), "sell_side_books": sum(1 for x in imb if x < -0.1)}

    n50 = [r for r in rows if r["in_nifty50"]]
    trim = ("symbol", "iep", "iep_chg_pct", "imbalance", "pressure")
    return {"status": "OK", "date": str(rows[0]["date"])[:10],
            "nse_time": max((r["nse_time"] for r in rows if r["nse_time"]), default=None),
            "all": breadth(rows), "nifty50": breadth(n50) if n50 else None,
            "top_buy_side": [{k: r[k] for k in trim} for r in sorted(rows, key=lambda r: -r["imbalance"])[:5]
                             if r["imbalance"] > 0],
            "top_sell_side": [{k: r[k] for k in trim} for r in sorted(rows, key=lambda r: r["imbalance"])[:5]
                              if r["imbalance"] < 0],
            "caveat": "the auction discovers the opening price: its imbalance is largely priced into the IEP. "
                      "See record() for whether it has said anything about the move after 09:15."}


def _hits(pairs, min_n) -> dict:
    """pairs of (predicted sign source, realised move): the share with the same sign (zeros dropped)."""
    pairs = [(a, b) for a, b in pairs if a and b]
    n = len(pairs)
    if not n:
        return {"n": 0, "hit_rate": None, "se": None, "edge": "INSUFFICIENT"}
    hit = sum(1 for a, b in pairs if (a > 0) == (b > 0)) / n
    se = math.sqrt(0.25 / n)
    edge = "INSUFFICIENT" if n < min_n else ("YES" if hit - 0.5 > 2 * se else "NO")
    return {"n": n, "hit_rate": round(hit, 3), "se": round(se, 3), "edge": edge}


def _corr(xs, ys):
    pts = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pts) < 3:
        return None
    mx = sum(p[0] for p in pts) / len(pts)
    my = sum(p[1] for p in pts) / len(pts)
    sxy = sum((x - mx) * (y - my) for x, y in pts)
    sx = math.sqrt(sum((x - mx) ** 2 for x, _ in pts))
    sy = math.sqrt(sum((y - my) ** 2 for _, y in pts))
    return round(sxy / (sx * sy), 3) if sx and sy else None


def record(conn, days=120, threshold=ONE_SIDED, min_n=MIN_OBSERVATIONS, today=None) -> dict:
    """What the pre-open has actually said, from the evaluated rows of the last `days` calendar days."""
    ensure_tables(conn)
    since = str((today or date.today()) - timedelta(days=int(days)))
    rows = _dicts(conn.execute("SELECT date, imbalance, iep_chg_pct, iep_error_bps, first15_pct, open_to_close_pct "
                               "FROM preopen_snapshot WHERE evaluated_at IS NOT NULL AND open_to_close_pct IS NOT NULL "
                               "AND date>=?", (since,)))
    if not rows:
        return {"status": "NO_DATA", "min_observations": min_n,
                "note": "nothing evaluated yet: capture runs at 09:09 and evaluation after the close"}
    one = [r for r in rows if r["imbalance"] is not None and abs(r["imbalance"]) >= threshold]
    err = [abs(r["iep_error_bps"]) for r in rows if r["iep_error_bps"] is not None]

    def mean(xs):
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 3) if xs else None

    return {"status": "OK", "days": len({str(r["date"])[:10] for r in rows}), "rows": len(rows),
            "threshold": threshold, "min_observations": min_n,
            "iep_vs_open_median_abs_bps": round(statistics.median(err), 1) if err else None,
            "imbalance_vs_first15": _hits([(r["imbalance"], r["first15_pct"]) for r in one], min_n),
            "imbalance_vs_open_to_close": _hits([(r["imbalance"], r["open_to_close_pct"]) for r in one], min_n),
            "gap_continues_open_to_close": _hits([(r["iep_chg_pct"], r["open_to_close_pct"]) for r in rows], min_n),
            "corr_imbalance_open_to_close": _corr([r["imbalance"] for r in rows], [r["open_to_close_pct"] for r in rows]),
            "mean_open_to_close_pct": {
                "buy_side": mean(r["open_to_close_pct"] for r in one if r["imbalance"] > 0),
                "sell_side": mean(r["open_to_close_pct"] for r in one if r["imbalance"] < 0),
                "all": mean(r["open_to_close_pct"] for r in rows)},
            "note": f"hit rate of the sign against 50%; an edge is claimed only with >= {min_n} observations and "
                    f"a hit rate more than two standard errors above 50%"}


def main(argv=None):
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(prog="python -m data.preopen")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("capture")
    sub.add_parser("evaluate")
    n = sub.add_parser("now")
    n.add_argument("--side", choices=("buy", "sell"))
    n.add_argument("--nifty50", action="store_true")
    r = sub.add_parser("record")
    r.add_argument("--days", type=int, default=120)
    a = ap.parse_args(argv)
    if a.cmd == "capture":
        print(json.dumps(capture(), indent=2, default=str))
        return
    if a.cmd == "evaluate":
        print(json.dumps(evaluate(), indent=2, default=str))
        return
    from db.schema import get_connection
    conn = get_connection()
    try:
        if a.cmd == "record":
            out = record(conn, days=a.days)
        else:
            out = {"summary": summary(conn), "rows": latest(conn, side=a.side, nifty50=a.nifty50, limit=30)}
        print(json.dumps(out, indent=2, default=str))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
