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
NIFTYMIDCAP150, NIFTYSMLCAP250.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date

BENCHMARKS = {"nifty50": "NIFTY50", "niftybank": "NIFTYBANK", "midcap150": "NIFTYMIDCAP150",
              "smallcap250": "NIFTYSMLCAP250"}


def _d(v):
    return v if isinstance(v, date) and not hasattr(v, "hour") else date.fromisoformat(str(v)[:10])


def calendar(conn, start: date, end: date) -> list:
    rows = conn.execute("SELECT DISTINCT date FROM prices_daily WHERE symbol='NIFTY50' AND date>=? AND date<=? "
                        "ORDER BY date", (str(start), str(end))).fetchall()
    return [_d(r[0]) for r in rows]


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
            rows = self.conn.execute("SELECT date, open, close, volume FROM prices_daily WHERE symbol=? AND date<=? "
                                     "AND close>0 ORDER BY date", (sym, str(self.end))).fetchall()
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


def basis_factor(conn, symbol: str, on: date, cache: dict) -> float:
    k = (symbol, on)
    if k not in cache:
        try:
            from data.corporate_actions import entry_factor
            cache[k] = entry_factor(conn, symbol, on) or 1.0
        except Exception:
            cache[k] = 1.0
    return cache[k]
