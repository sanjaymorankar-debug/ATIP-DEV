"""
ML-only context features: market context and cross-sectional sector features.

These need data a single-symbol W3 FeatureContext does not hold (other index
series, global markets, every symbol of the same sector on the same date), so
they are computed here, per decision date, and merged into the feature rows by
the dataset builder and the prediction engine -- the same code on both paths.

MARKET CONTEXT (same value for every symbol on a date)
    banknifty_ret_5 / _20, midcap_ret_20, smallcap_ret_20
                        index returns from prices_daily (NIFTYBANK,
                        NIFTYMIDCAP150, NIFTYSMLCAP250), closes dated <= t
    gm_sp500_chg, gm_nasdaq_chg, gm_nikkei_chg, gm_brent_chg, gm_gold_chg,
    gm_usdinr_chg, gm_us10y_chg, gm_global_score
                        global_markets: the latest row dated <= t and created
                        before 15:30 IST (10:00 UTC; created_at is UTC) on t --
                        known before the Indian close

CROSS-SECTIONAL (per symbol, from all symbols with a row on that date)
    sector_ret_20       mean ret_20 of the symbol's NSE industry (the symbol included)
    stock_vs_sector_20  ret_20 - sector_ret_20
    sector_breadth      share of the industry's stocks with ret_5 > 0
  NSE industry from the cached Nifty 500 list (read directly, no network).
  A symbol with no industry, or an industry with < 3 members that day, gets None.

Availability: global markets from 2026-07-25; index series as prices_daily.
Nothing here is ever a W3 strategy feature or a label.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

INDEXES = {"banknifty": "NIFTYBANK", "midcap": "NIFTYMIDCAP150", "smallcap": "NIFTYSMLCAP250"}
MARKET_CONTEXT = {
    "banknifty_ret_5": ("banknifty", 5), "banknifty_ret_20": ("banknifty", 20),
    "midcap_ret_20": ("midcap", 20), "smallcap_ret_20": ("smallcap", 20),
}
GLOBAL = {"gm_sp500_chg": "sp500_chg", "gm_nasdaq_chg": "nasdaq_chg", "gm_nikkei_chg": "nikkei_chg",
          "gm_brent_chg": "crude_brent_chg", "gm_gold_chg": "gold_chg", "gm_usdinr_chg": "usd_inr_chg",
          "gm_us10y_chg": "us_10y_chg", "gm_global_score": "global_score"}
CROSS_SECTIONAL = ("sector_ret_20", "stock_vs_sector_20", "sector_breadth")
ALL = tuple(MARKET_CONTEXT) + tuple(GLOBAL) + CROSS_SECTIONAL
NEEDS = {"sector_ret_20": ("ret_20",), "stock_vs_sector_20": ("ret_20",), "sector_breadth": ("ret_5",)}

META = {
    **{k: ("market", f"{INDEXES[v[0]]} {v[1]}-session return %", v[1] + 1, "prices_daily")
       for k, v in MARKET_CONTEXT.items()},
    **{k: ("global", f"global_markets.{v} (latest row known before the Indian close)", 1, "2026-07-25")
       for k, v in GLOBAL.items()},
    "sector_ret_20": ("sector", "mean ret_20 of the NSE industry that day", 21, "prices_daily + Nifty 500 industries"),
    "stock_vs_sector_20": ("sector", "ret_20 minus sector_ret_20", 21, "prices_daily + Nifty 500 industries"),
    "sector_breadth": ("sector", "share of the industry with ret_5 > 0", 6, "prices_daily + Nifty 500 industries"),
}


def _d(x):
    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


class MarketContext:
    """Point-in-time market context for dates in [start, end]."""

    def __init__(self, conn, start, end):
        start, end = _d(start), _d(end)
        self.series = {}
        for key, sym in INDEXES.items():
            rows = conn.execute("SELECT date, close FROM prices_daily WHERE symbol=? AND date>=? AND date<=? "
                                "ORDER BY date", (sym, str(start - timedelta(days=60)), str(end))).fetchall()
            self.series[key] = [(_d(d), c) for d, c in rows if c]
        self.global_rows = []
        try:
            cols = ", ".join(GLOBAL.values())
            for r in conn.execute(f"SELECT date, created_at, {cols} FROM global_markets WHERE date<=? "
                                  f"ORDER BY date, created_at", (str(end),)):
                self.global_rows.append((_d(r[0]), str(r[1] or ""), dict(zip(GLOBAL, r[2:]))))
        except Exception:
            pass

    def _ret(self, key, t, n):
        s = [c for d, c in self.series.get(key, []) if d <= t]
        if len(s) < n + 1 or not s[-1 - n]:
            return None
        return (s[-1] / s[-1 - n] - 1) * 100

    def on(self, t) -> dict:
        t = _d(t)
        out = {k: self._ret(key, t, n) for k, (key, n) in MARKET_CONTEXT.items()}
        # global_markets.created_at is stored in UTC (the 07:00 IST pre-market row reads 01:31),
        # so the Indian close, 15:30 IST, is 10:00 UTC
        cutoff = datetime.combine(t, time(10, 0)).isoformat(sep=" ")
        last = None
        for d, created, vals in self.global_rows:
            if d <= t and (not created or created[:19] <= cutoff):
                last = vals
        out.update(last or {k: None for k in GLOBAL})
        return out


def add_cross_sectional(rows: dict, sectors: dict) -> None:
    """rows: {symbol: {feature: value}} for ONE date, containing ret_20 / ret_5.
    Adds sector_ret_20, stock_vs_sector_20, sector_breadth in place."""
    groups = {}
    for s, v in rows.items():
        ind = sectors.get(s)
        if ind:
            groups.setdefault(ind, []).append(s)
    for s, v in rows.items():
        ind = sectors.get(s)
        members = groups.get(ind, [])
        r20 = [rows[m].get("ret_20") for m in members if rows[m].get("ret_20") is not None]
        r5 = [rows[m].get("ret_5") for m in members if rows[m].get("ret_5") is not None]
        if not ind or len(r20) < 3:
            v.update(sector_ret_20=None, stock_vs_sector_20=None, sector_breadth=None)
            continue
        sr = sum(r20) / len(r20)
        v["sector_ret_20"] = sr
        v["stock_vs_sector_20"] = (v["ret_20"] - sr) if v.get("ret_20") is not None else None
        v["sector_breadth"] = (sum(1 for x in r5 if x > 0) / len(r5)) if len(r5) >= 3 else None


_SECTORS = {}


def sector_map() -> dict:
    """symbol -> NSE industry from the cached Nifty 500 list; {} when unavailable.
    Read directly (no network, no dependency on execution/)."""
    if not _SECTORS:
        try:
            import pandas as pd
            from data.index_constituents import NIFTY500_CACHE
            df = pd.read_csv(NIFTY500_CACHE)
            sym = next(c for c in ("Symbol", "SYMBOL", "symbol") if c in df.columns)
            ind = next(c for c in ("Industry", "INDUSTRY", "industry") if c in df.columns)
            _SECTORS.update({str(x).strip().upper(): str(i).strip() for x, i in zip(df[sym], df[ind])
                             if str(i).strip() and str(i).lower() != "nan"})
        except Exception:
            return {}
    return _SECTORS


def enrich(rows: dict, feature_names, t, market: MarketContext | None, sectors: dict | None) -> None:
    """Add every context feature in feature_names to rows (one date) in place."""
    want = [f for f in feature_names if f in ALL]
    if not want:
        return
    if any(f in MARKET_CONTEXT or f in GLOBAL for f in want) and market is not None:
        ctx = market.on(t)
        for v in rows.values():
            for f in want:
                if f in ctx:
                    v[f] = ctx[f]
    if any(f in CROSS_SECTIONAL for f in want):
        add_cross_sectional(rows, sectors or {})
