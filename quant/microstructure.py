"""
Microstructure foundation.

What ATIP has: live_quotes -- intraday LTP / OHLC / cumulative volume snapshots
(every ~15 minutes in market hours, via the Dhan quote API). What it does NOT
have: tick data (live_ticks is empty), bid / ask, market depth, order-book
imbalance. So:

  computed from live_quotes, per symbol per day (microstructure_feature):
    n_snapshots          snapshots stored that day
    intraday_rv          annualised stdev of snapshot-to-snapshot log returns x
                         sqrt(snapshots per day x 252) -- short-term volatility
    intraday_range_pct   (max ltp - min ltp) / first ltp x 100
    trade_intensity      mean volume added per snapshot interval (shares)
    last_hour_move_pct   ltp change over the last 4 snapshots
  interface only (DATA_PENDING): spread, depth, order_imbalance, tick counts,
    market-impact inputs beyond participation

  participation(order_qty, adv)   order size as % of average daily volume -- the
                                  basic market-impact input, usable today

W30 (AF-07): from the stored 15-minute bars (intraday_bars, DP-03) and daily bars, per
symbol per day, written to microstructure_feature with source 'bars':
    bar_rv               annualised stdev of 15-min log returns x sqrt(25 bars x 252)
    roll_spread_bps      Roll (1984) effective spread: 2 sqrt(-cov(dp_t, dp_t-1)) / mean price,
                         bps (None when the autocovariance is >= 0 -- no estimate)
    cs_spread_bps        Corwin-Schultz (2012) high-low spread from two daily bars, bps
                         (negative estimates floored at 0, as the paper does)
    vwap_dev_pct         close vs session VWAP from the bars
    close_location       (close - low) / (high - low) of the session, 0..1
    last_hour_share      share of the day's volume traded in the last 4 bars (hour)
    order_imbalance      (buy qty - sell qty) / (buy + sell) from the stock feed's Quote packets
                         (live_quotes.buy_qty / sell_qty), mean over the day -- None until the
                         W27 stock feed runs
These are spread / liquidity ESTIMATES from trade prices, not quoted spreads: a real
bid-ask spread and depth still need quotes (tick / depth feeds, DP-04 / DP-05).
Strategy features (quant/microstructure.MicrostructureHistory, via QuantHistory):
    ms_bar_rv, ms_roll_spread_bps, ms_cs_spread_bps, ms_vwap_dev_pct, ms_close_location,
    ms_last_hour_share, ms_order_imbalance, and ms_spread_20 (mean of the available spread
    estimates over the last 20 sessions)
"""

from __future__ import annotations

import math
from datetime import date, datetime

PENDING = {"spread": "needs bid/ask quotes", "depth": "needs market depth (Dhan 20-level feed not integrated)",
           "order_imbalance": "needs order book", "tick_count": "live_ticks is empty (tick feed not scheduled)"}


def participation(order_qty, adv):
    return None if not adv else order_qty / adv * 100


def _roll(closes):
    dp = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    if len(dp) < 4:
        return None
    a, b = dp[1:], dp[:-1]
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (len(a) - 1)
    mean_p = sum(closes) / len(closes)
    return 2 * math.sqrt(-cov) / mean_p * 1e4 if cov < 0 and mean_p else None


def _corwin_schultz(h1, l1, h2, l2):
    if not all(x and x > 0 for x in (h1, l1, h2, l2)) or h1 < l1 or h2 < l2:
        return None
    beta = math.log(h1 / l1) ** 2 + math.log(h2 / l2) ** 2
    gamma = math.log(max(h1, h2) / min(l1, l2)) ** 2
    k = 3 - 2 * math.sqrt(2)
    alpha = (math.sqrt(2 * beta) - math.sqrt(beta)) / k - math.sqrt(gamma / k)
    s = 2 * (math.exp(alpha) - 1) / (1 + math.exp(alpha))
    return max(0.0, s) * 1e4


def compute_bars_day(conn, day=None) -> dict:
    """W30 (AF-07): bar-based microstructure features for every symbol with bars that day."""
    day = str(day or date.today())[:10]
    rows = conn.execute("SELECT symbol, ts, high, low, close, volume FROM intraday_bars WHERE ts>=? AND ts<? "
                        "ORDER BY symbol, ts", (day, day + " 99")).fetchall()
    by = {}
    for s, ts, h, l, c, v in rows:
        if c:
            by.setdefault(s, []).append((float(h or c), float(l or c), float(c), float(v or 0)))
    imb = {}
    try:
        for s, b, sl in conn.execute("SELECT symbol, buy_qty, sell_qty FROM live_quotes WHERE substr(timestamp,1,10)=? "
                                     "AND buy_qty IS NOT NULL AND sell_qty IS NOT NULL", (day,)):
            if b + sl > 0:
                imb.setdefault(s, []).append((b - sl) / (b + sl))
    except Exception:
        pass
    now = datetime.now()
    n = 0
    for s, bars in by.items():
        if len(bars) < 5:
            continue
        cl = [x[2] for x in bars]
        rets = [math.log(cl[i] / cl[i - 1]) for i in range(1, len(cl)) if cl[i - 1] > 0]
        m = sum(rets) / len(rets)
        rv = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(25 * 252) * 100
        vol = sum(x[3] for x in bars)
        vwap = sum((x[0] + x[1] + x[2]) / 3 * x[3] for x in bars) / vol if vol else None
        hi, lo = max(x[0] for x in bars), min(x[1] for x in bars)
        d = conn.execute("SELECT high, low FROM prices_daily WHERE symbol=? AND date<? AND high>0 ORDER BY date DESC "
                         "LIMIT 1", (s, day)).fetchone()
        feats = {"bar_rv": rv, "roll_spread_bps": _roll(cl),
                 "cs_spread_bps": _corwin_schultz(d[0], d[1], hi, lo) if d else None,
                 "vwap_dev_pct": (cl[-1] / vwap - 1) * 100 if vwap else None,
                 "close_location": (cl[-1] - lo) / (hi - lo) if hi > lo else None,
                 "last_hour_share": sum(x[3] for x in bars[-4:]) / vol if vol else None,
                 "order_imbalance": sum(imb[s]) / len(imb[s]) if imb.get(s) else None}
        for k, v in feats.items():
            if v is None:
                continue
            conn.execute("INSERT INTO microstructure_feature (symbol,date,feature,value,source,created_at) "
                         "VALUES (?,?,?,?,?,?) ON CONFLICT(symbol,date,feature) DO UPDATE SET value=excluded.value,"
                         "source=excluded.source,created_at=excluded.created_at", (s, day, k, float(v), "bars", now))
        n += 1
    conn.commit()
    return {"date": day, "symbols": n}


MS_FEATURES = ("ms_bar_rv", "ms_roll_spread_bps", "ms_cs_spread_bps", "ms_vwap_dev_pct", "ms_close_location",
               "ms_last_hour_share", "ms_order_imbalance", "ms_spread_20")


class MicrostructureHistory:
    """Point-in-time ms_* features: a day's values are known at that day's close."""

    def __init__(self, conn, start=None, end=None):
        self._by = {}
        q, args = "SELECT symbol, date, feature, value FROM microstructure_feature WHERE source='bars'", []
        if end:
            q += " AND date<=?"
            args.append(str(end))
        if start:
            q += " AND date>=?"
            args.append(str(date.fromisoformat(str(start)[:10]) - __import__("datetime").timedelta(days=40)))
        for s, d, f, v in conn.execute(q, args):
            self._by.setdefault(s, {}).setdefault(str(d)[:10], {})[f] = v

    def on(self, as_of, symbol) -> dict:
        days = self._by.get(symbol)
        if not days:
            return {}
        k = str(as_of)[:10]
        out = {f"ms_{f}": v for f, v in days.get(k, {}).items()}
        recent = sorted(d for d in days if d <= k)[-20:]
        sp = [days[d].get("roll_spread_bps") or days[d].get("cs_spread_bps") for d in recent]
        sp = [x for x in sp if x is not None]
        out["ms_spread_20"] = sum(sp) / len(sp) if len(sp) >= 5 else None
        return out


def compute_day(conn, day=None) -> dict:
    day = str(day or date.today())[:10]
    rows = conn.execute("SELECT symbol, ltp, volume, timestamp FROM live_quotes WHERE substr(timestamp,1,10)=? "
                        "ORDER BY symbol, timestamp", (day,)).fetchall()
    by = {}
    for s, ltp, vol, ts in rows:
        if ltp:
            by.setdefault(s, []).append((ts, float(ltp), float(vol or 0)))
    now = datetime.now()
    n = 0
    for s, snaps in by.items():
        if len(snaps) < 3:
            continue
        p = [x[1] for x in snaps]
        rets = [math.log(p[i] / p[i - 1]) for i in range(1, len(p)) if p[i - 1] > 0]
        rv = None
        if len(rets) >= 2:
            m = sum(rets) / len(rets)
            rv = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(len(snaps) * 252) * 100
        dv = [snaps[i][2] - snaps[i - 1][2] for i in range(1, len(snaps)) if snaps[i][2] >= snaps[i - 1][2]]
        feats = {"n_snapshots": len(snaps), "intraday_rv": rv,
                 "intraday_range_pct": (max(p) - min(p)) / p[0] * 100 if p[0] else None,
                 "trade_intensity": sum(dv) / len(dv) if dv else None,
                 "last_hour_move_pct": (p[-1] / p[-5] - 1) * 100 if len(p) >= 5 and p[-5] else None}
        for k, v in feats.items():
            conn.execute("INSERT INTO microstructure_feature (symbol,date,feature,value,source,created_at) "
                         "VALUES (?,?,?,?,?,?) ON CONFLICT(symbol,date,feature) DO UPDATE SET value=excluded.value,"
                         "created_at=excluded.created_at", (s, day, k, v, "live_quotes", now))
        n += 1
    conn.commit()
    return {"date": day, "symbols": n, "pending": PENDING}
