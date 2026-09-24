"""
Historical data for backtests, with the bias controls (BT-15).

  data validation      bars with missing/non-positive prices, high < low, or an
                       open/close outside [low, high] are dropped and listed
  timestamp validation bars dated on a non-trading day or after today are
                       dropped and listed; dates are strictly increasing per symbol
  look-ahead           a strategy never gets PriceHistory itself -- it gets a
                       PointInTimeView bound to one as_of date, which returns
                       only bars dated on or before it and raises LookAheadError
                       for anything later
  survivorship         the universe says what it is. ATIP's deep history covers
                       only the symbols it tracks today (the current Nifty 500),
                       so a backtest over that universe carries survivorship
                       bias and the run record says so (universe_bias)
  unavailable info     inputs that were not available historically are named in
                       the bias report -- e.g. ai_scores rows are the current
                       formula re-applied to history, not what ATIP said then

Every loaded dataset has a fingerprint (row count, symbols, dates, and a
SHA-256 over symbol/date/OHLCV) so a run can be checked against the data it
was produced from.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from utils.trading_calendar import is_trading_day

log = logging.getLogger("atip.backtest")

INDEX_SOURCES = ("dhan_index", "nse_index")     # synthetic index series, never tradeable


class LookAheadError(RuntimeError):
    """A strategy asked for data dated after the moment it is deciding at."""


@dataclass(frozen=True)
class Bar:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float


def _d(x) -> date:
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    return date.fromisoformat(str(x)[:10])


def _bar_problem(o, h, l, c) -> str | None:
    if any(v is None for v in (o, h, l, c)):
        return "missing price"
    if min(o, h, l, c) <= 0:
        return "non-positive price"
    tol = 1e-6
    if h < l * (1 - tol):
        return "high below low"
    if not (l * (1 - tol) <= c <= h * (1 + tol)) or not (l * (1 - tol) <= o <= h * (1 + tol)):
        return "open/close outside [low, high]"
    return None


class PriceHistory:
    """
    Daily bars for a set of symbols over [start - warmup, end], validated.

    bars[symbol] is a list of Bar ascending by date. sessions is every date
    any loaded symbol has a bar on, ascending. issues lists what was dropped.
    """

    def __init__(self, bars: dict, issues: list, requested: tuple, source: str = "prices_daily"):
        self.bars = bars
        self.issues = issues
        self.requested = requested
        self.source = source
        self.sessions = sorted({b.date for s in bars.values() for b in s})
        self._index = {sym: {b.date: i for i, b in enumerate(s)} for sym, s in bars.items()}

    # -- loading ----------------------------------------------------------
    @classmethod
    def load(cls, conn, symbols, start, end, warmup_days: int = 400) -> "PriceHistory":
        start, end = _d(start), _d(end)
        lo = start - timedelta(days=warmup_days)
        syms = sorted(set(symbols))
        bars, issues = {}, []
        if syms:
            q = (f"SELECT symbol, date, open, high, low, close, volume FROM prices_daily "
                 f"WHERE date>=? AND date<=? AND source NOT IN ({','.join('?' * len(INDEX_SOURCES))}) "
                 f"AND symbol IN ({','.join('?' * len(syms))}) ORDER BY symbol, date")
            rows = conn.execute(q, (str(lo), str(end), *INDEX_SOURCES, *syms)).fetchall()
        else:
            rows = []
        today = date.today()
        for sym, d, o, h, l, c, v in rows:
            d = _d(d)
            if d > today:
                issues.append({"symbol": sym, "date": str(d), "issue": "dated in the future"}); continue
            if not is_trading_day(d):
                issues.append({"symbol": sym, "date": str(d), "issue": "not a trading day"}); continue
            prob = _bar_problem(o, h, l, c)
            if prob:
                issues.append({"symbol": sym, "date": str(d), "issue": prob}); continue
            series = bars.setdefault(sym, [])
            if series and series[-1].date >= d:
                issues.append({"symbol": sym, "date": str(d), "issue": "date not after previous bar"}); continue
            series.append(Bar(d, float(o), float(h), float(l), float(c), float(v or 0)))
        if issues:
            log.warning(f"  backtest data: {len(issues)} bar(s) dropped by validation")
        return cls(bars, issues, (str(start), str(end), warmup_days))

    # -- access (engine only) -------------------------------------------
    def bar(self, symbol: str, d: date) -> Bar | None:
        i = self._index.get(symbol, {}).get(d)
        return self.bars[symbol][i] if i is not None else None

    def bars_before(self, symbol: str, d: date, n: int) -> list:
        """Up to n bars strictly before d (for turnover averages at a fill)."""
        s = self.bars.get(symbol, [])
        idx = self._index.get(symbol, {})
        i = idx.get(d)
        if i is None:
            i = sum(1 for b in s if b.date < d)
        return s[max(0, i - n):i]

    def view(self, as_of: date) -> "PointInTimeView":
        return PointInTimeView(self, _d(as_of))

    def fingerprint(self) -> dict:
        h = hashlib.sha256()
        n = 0
        for sym in sorted(self.bars):
            for b in self.bars[sym]:
                h.update(f"{sym}|{b.date}|{b.open}|{b.high}|{b.low}|{b.close}|{b.volume}\n".encode())
                n += 1
        return {"source": self.source, "bars": n, "symbols": len(self.bars),
                "first_date": str(self.sessions[0]) if self.sessions else None,
                "last_date": str(self.sessions[-1]) if self.sessions else None,
                "dropped_by_validation": len(self.issues), "sha256": h.hexdigest()}


class PointInTimeView:
    """
    What a strategy may see when deciding at the close of `as_of`: bars dated
    on or before it, nothing later. Asking for a later date raises -- a
    look-ahead is a failure, not a quietly empty answer.
    """

    def __init__(self, history: PriceHistory, as_of: date):
        self._h = history
        self.as_of = as_of

    def _guard(self, d: date):
        if d > self.as_of:
            raise LookAheadError(f"requested {d} while deciding at {self.as_of}")

    def symbols(self) -> list:
        return [s for s, bars in self._h.bars.items() if bars and bars[0].date <= self.as_of]

    def history(self, symbol: str, n: int | None = None) -> list:
        """Bars on or before as_of, oldest first; the last n when given."""
        s = self._h.bars.get(symbol, [])
        idx = self._h._index.get(symbol, {})
        i = idx.get(self.as_of)
        end = (i + 1) if i is not None else sum(1 for b in s if b.date <= self.as_of)
        out = s[:end]
        return out[-n:] if n else out

    def bar(self, symbol: str, d: date | None = None) -> Bar | None:
        d = _d(d) if d else self.as_of
        self._guard(d)
        return self._h.bar(symbol, d)

    def closes(self, symbol: str, n: int | None = None) -> list:
        return [b.close for b in self.history(symbol, n)]


# -- universes ---------------------------------------------------------------

@dataclass(frozen=True)
class Universe:
    """The symbols a backtest may trade, and what that choice implies."""
    symbols: tuple
    kind: str                 # tracked_current | explicit
    survivorship_bias: bool
    note: str

    def as_dict(self) -> dict:
        return {"kind": self.kind, "n_symbols": len(self.symbols), "survivorship_bias": self.survivorship_bias,
                "note": self.note}


def tracked_universe(conn) -> Universe:
    """Today's tracked symbols (Nifty 500 + holdings). Survivorship-biased:
    stocks that left the index or delisted before today are not in it."""
    from data.dhan import get_tracked_symbols
    syms = tuple(sorted(get_tracked_symbols(conn)))
    return Universe(syms, "tracked_current", True,
                    "Current constituents only: names that dropped out or delisted before today are "
                    "missing, which flatters results. ATIP holds no point-in-time constituent history.")


def explicit_universe(symbols, survivorship_bias: bool | None = None, note: str = "") -> Universe:
    """A caller-supplied list. Unless told otherwise it is assumed to be as
    biased as the tracked list, since it was most likely chosen today."""
    return Universe(tuple(sorted(set(s.upper() for s in symbols))), "explicit",
                    True if survivorship_bias is None else bool(survivorship_bias),
                    note or "Caller-supplied list; treated as chosen with hindsight unless stated.")


# -- ai_scores, point in time -------------------------------------------------

class ScoresHistory:
    """
    ai_scores rows as a strategy input. Scores dated d are produced after the
    close of d, so a strategy deciding at the close of d may use them -- and
    the engine fills at the next session's open. Never rows after as_of.

    Caveat recorded in the bias report: ai_scores are recomputed when formulas
    or data are repaired (MACD fix, VIX, breadth...), so history reflects
    today's formulas, not what ATIP actually said then. signal_log is the
    contemporaneous record.
    """

    CAVEAT = ("ai_scores history is today's formulas re-applied to history (rows are recomputed "
              "after repairs); it is not what ATIP said at the time — signal_log is.")

    def __init__(self, conn, start, end):
        self._rows = {}
        for r in conn.execute("SELECT date, symbol, signal, atip_score, vpi, zpi, cri, acs, mri, rri "
                              "FROM ai_scores WHERE date>=? AND date<=?", (str(_d(start)), str(_d(end)))):
            self._rows.setdefault(_d(r[0]), {})[r[1]] = {
                "signal": r[2], "atip_score": r[3], "vpi": r[4], "zpi": r[5], "cri": r[6],
                "acs": r[7], "mri": r[8], "rri": r[9]}

    def on(self, as_of: date, d: date | None = None) -> dict:
        d = _d(d) if d else as_of
        if d > as_of:
            raise LookAheadError(f"scores for {d} requested while deciding at {as_of}")
        return self._rows.get(d, {})
