"""
Market data for performance work (W15.5).

BASIS. prices_daily is kept on today's share basis (data/corporate_actions.py:
history is adjusted for splits / bonuses once reconciled). A trade recorded on an
older basis is carried onto it with corporate_actions.entry_factor(): price x F,
quantity / F, value unchanged. Every engine in this package works on that basis,
so a split between a buy and today neither creates nor destroys return.

CALENDAR. Trading sessions are the dates of the NIFTY50 series in prices_daily
(weekends and market holidays are simply absent). A symbol with no bar on a
session is carried forward from its last close and the gap is counted.

BENCHMARKS. Any prices_daily symbol; the named ones: NIFTY50, NIFTYBANK,
NIFTYMIDCAP150, NIFTYSMLCAP250. W40 (PERF-001-05): NIFTY50_TR ("nifty50tr"), the Nifty 50 with
dividends reinvested, read from index_total_return (data/total_return.py: the exact price index
plus an estimate of its dividends).
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date

BENCHMARKS = {"nifty50": "NIFTY50", "nifty50tr": "NIFTY50_TR", "niftybank": "NIFTYBANK",
              "midcap150": "NIFTYMIDCAP150", "smallcap250": "NIFTYSMLCAP250"}


def _d(v):
    return v if isinstance(v, date) and not hasattr(v, "hour") else date.fromisoformat(str(v)[:10])


def calendar(conn, start: date, end: date) -> list:
    rows = conn.execute("SELECT DISTINCT date FROM prices_daily WHERE symbol='NIFTY50' AND date>=? AND date<=? "
                        "ORDER BY date", (str(start), str(end))).fetchall()
    return [_d(r[0]) for r in rows]


def _is_tr(sym) -> bool:
    from data.total_return import is_tr_symbol
    return is_tr_symbol(sym)


def _tr_rows(conn, sym, end):
    from data.total_return import SUFFIX
    try:
        return conn.execute("SELECT date, tri FROM index_total_return WHERE index_symbol=? AND date<=? ORDER BY date",
                            (sym.upper()[:-len(SUFFIX)], str(end))).fetchall()
    except Exception:                       # the table is created by the first build
        return []


def has_prices(conn, sym) -> bool:
    """True when the benchmark has any stored value (prices_daily, or index_total_return for *_TR)."""
    if _is_tr(sym):
        return bool(_tr_rows(conn, sym, date(2100, 1, 1))[:1])
    return conn.execute("SELECT 1 FROM prices_daily WHERE symbol=? LIMIT 1", (sym,)).fetchone() is not None


def rows(conn, sym, start, end) -> list:
    """(date, open, close, volume) of a symbol between start and end, from prices_daily, or from
    index_total_return for a *_TR series (open = close = the index value, no volume)."""
    if _is_tr(sym):
        return [(d, t, t, None) for d, t in _tr_rows(conn, sym, end) if str(d)[:10] >= str(start)]
    return conn.execute("SELECT date, open, close, volume FROM prices_daily WHERE symbol=? AND date>=? AND date<=? "
                        "ORDER BY date", (sym, str(start), str(end))).fetchall()


def last_date(conn, sym, before: date | None = None):
    """The latest stored date of a symbol (strictly before `before` when given) as YYYY-MM-DD, or None."""
    if _is_tr(sym):
        ds = [r[0] for r in _tr_rows(conn, sym, date(2100, 1, 1)) if before is None or _d(r[0]) < before]
        v = ds[-1] if ds else None
    elif before is None:
        v = conn.execute("SELECT MAX(date) FROM prices_daily WHERE symbol=?", (sym,)).fetchone()[0]
    else:
        v = conn.execute("SELECT MAX(date) FROM prices_daily WHERE symbol=? AND date<?", (sym, str(before))).fetchone()[0]
    return str(v)[:10] if v else None


def benchmark_symbol(name: str | None) -> str:
    n = (name or "nifty50").strip()
    return BENCHMARKS.get(n.lower(), n.upper())


class Prices:
    """Close / open series per symbol, loaded once per report, with carry-forward."""

    def __init__(self, conn, start: date, end: date):
        self.conn, self.start, self.end = conn, start, end
        self._s = {}
        self.gaps = {}

    def _load(self, sym):
        if sym not in self._s:
            if _is_tr(sym):                 # a total-return series: one value a day, open = close
                rows = [(d, t, t, None) for d, t in _tr_rows(self.conn, sym, self.end)]
            else:
                rows = self.conn.execute("SELECT date, open, close, volume FROM prices_daily WHERE symbol=? AND "
                                         "date<=? AND close>0 ORDER BY date", (sym, str(self.end))).fetchall()
            ds = [_d(r[0]) for r in rows]
            self._s[sym] = (ds, [float(r[1]) if r[1] else None for r in rows], [float(r[2]) for r in rows],
                            [float(r[3]) if r[3] else None for r in rows])
        return self._s[sym]

    def close(self, sym, d: date, count_gap=True):
        ds, _, cl, _ = self._load(sym)
        i = bisect_right(ds, d) - 1
        if i < 0:
            return None
        if count_gap and ds[i] != d:
            self.gaps[sym] = self.gaps.get(sym, 0) + 1
        return cl[i]

    def bar(self, sym, d: date):
        """(open, close, volume) of the exact session, or None."""
        ds, op, cl, vo = self._load(sym)
        i = bisect_right(ds, d) - 1
        if i < 0 or ds[i] != d:
            return None
        return op[i], cl[i], vo[i]

    def next_session(self, sym, d: date):
        """First session strictly after d that has a bar: (date, open, close, volume)."""
        ds, op, cl, vo = self._load(sym)
        i = bisect_right(ds, d)
        if i >= len(ds):
            return None
        return ds[i], op[i], cl[i], vo[i]

    def nth_session_after(self, sym, d: date, n: int):
        ds, op, cl, vo = self._load(sym)
        i = bisect_right(ds, d) - 1 + n
        if i < 0 or i >= len(ds):
            return None
        return ds[i], op[i], cl[i], vo[i]

    def adv_value(self, sym, d: date, n=20):
        """Average daily traded value (close x volume) over the n sessions up to d."""
        ds, _, cl, vo = self._load(sym)
        i = bisect_right(ds, d)
        pts = [c * v for c, v in zip(cl[max(0, i - n):i], vo[max(0, i - n):i]) if v]
        return sum(pts) / len(pts) if pts else None

    def last_date(self, sym):
        ds = self._load(sym)[0]
        return ds[-1] if ds else None


FALLBACK_KEY = "__basis_fallback__"


def basis_factor(conn, symbol: str, on: date, cache: dict) -> float:
    """corporate_actions.entry_factor; 1.0 when it fails -- W39: and the failure is listed
    under cache[FALLBACK_KEY], which the engine reports as data quality (BASIS_FALLBACK)
    instead of silently treating the trade as already on today's basis."""
    k = (symbol, on)
    if k not in cache:
        try:
            from data.corporate_actions import entry_factor
            cache[k] = entry_factor(conn, symbol, on) or 1.0
        except Exception as e:
            cache[k] = 1.0
            cache.setdefault(FALLBACK_KEY, []).append(f"{symbol}@{on}: {type(e).__name__}")
    return cache[k]
