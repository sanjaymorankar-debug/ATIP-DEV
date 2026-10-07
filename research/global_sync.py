"""
W39 Phase 3 (item 3, GS-01..03) — the global-cue model on SYNCHRONISED moves: what global futures, Asia
and currencies did between India's close (15:30 IST) and the morning (08:45 IST), against the Nifty's
opening gap. The daily-close model in research/market_pulse.py regresses the Nifty's close-to-close move
on the previous session's closes; markets close at different hours, so part of what it "predicts" has
already happened by India's close. This one only uses moves India has not yet traded on.

    SERIES      S&P 500 and Nasdaq 100 futures, the Nikkei and the Hang Seng (cash: by 08:45 IST Tokyo has
                traded since 05:30 and Hong Kong since 07:00), Brent and gold futures, the dollar index and
                USD/INR, from Yahoo through data/markets.py's yfinance wrapper (breaker and retry)
    capture()   "close" at 15:31 and "pre" at 08:42 on trading days, the last 5-minute price of each series
                and its time, in global_snapshot
    design()    one row per morning: each series' log move from the previous session's 15:30 snapshot to
                that morning's 08:45 one, and the Nifty's opening gap (first index_levels reading between 09:15
                and 09:30 against the previous session's last up to 15:30)
    model()     ridge regression of the gap on those moves (inputs standardised, as the daily model), once
                40 mornings exist; walk-forward over the latest mornings (each fitted only on the mornings
                before it, 30 at least) against "no change" and against the GIFT Nifty estimate made the same
                mornings. Today's expected gap from this morning's moves.

capture_gift() stores this model's estimate with the GIFT one (market_cue.sync_expected_pct) so the
open-gap record scores all three. Until 40 mornings are stored the page says how many it has.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta

import numpy as np

log = logging.getLogger(__name__)

SERIES = {
    "es": ("ES=F", "S&P 500 futures"), "nq": ("NQ=F", "Nasdaq 100 futures"), "nikkei": ("^N225", "Nikkei 225"),
    "hangseng": ("^HSI", "Hang Seng"), "brent": ("BZ=F", "Brent futures"), "gold": ("GC=F", "Gold futures"),
    "dxy": ("DX-Y.NYB", "Dollar index"), "usdinr": ("INR=X", "USD/INR"),
}
LABELS = ("close", "pre")
MIN_MORNINGS, MIN_FIT, EVAL_MORNINGS, RIDGE = 40, 30, 60, 1.0

DDL = (
    """CREATE TABLE IF NOT EXISTS global_snapshot (
        date DATE NOT NULL, label TEXT NOT NULL, series TEXT NOT NULL, price REAL, quote_ts TEXT,
        captured_at TIMESTAMP, PRIMARY KEY (date, label, series))""",
)


def ensure_tables(conn):
    for d in DDL:
        conn.execute(d)


def _d(v) -> date:
    return v if isinstance(v, date) and not isinstance(v, datetime) else date.fromisoformat(str(v)[:10])


def fetch_last() -> dict:
    """{series: (price, quote time)} -- the last 5-minute close of each Yahoo ticker over the last 2 days."""
    from data.markets import _yf_download
    tick = {t: k for k, (t, _l) in SERIES.items()}
    df = _yf_download(list(tick), period="2d", interval="5m", progress=False, auto_adjust=True, group_by="column")
    out = {}
    close = df["Close"] if "Close" in df else df
    for t, k in tick.items():
        if t not in close:
            continue
        s = close[t].dropna()
        if len(s):
            out[k] = (float(s.iloc[-1]), str(s.index[-1]))
    return out


def capture(conn, label: str, fetch=None, now: datetime | None = None) -> dict:
    """Store the global snapshot: label "close" (15:30 IST) or "pre" (08:45 IST)."""
    if label not in LABELS:
        raise ValueError(f"label must be one of {LABELS}")
    ensure_tables(conn)
    now = now or datetime.now()
    try:
        prices = (fetch or fetch_last)()
    except Exception as e:
        return {"status": "FAILED", "rows": 0, "error": str(e)[:200]}
    for k, (p, ts) in prices.items():
        conn.execute("INSERT OR REPLACE INTO global_snapshot (date, label, series, price, quote_ts, captured_at) "
                     "VALUES (?,?,?,?,?,?)", (str(now.date()), label, k, p, ts, now))
    conn.commit()
    return {"status": "SUCCESS" if prices else "EMPTY", "rows": len(prices), "label": label}


def _prev_session(d: date) -> date:
    from utils.trading_calendar import is_trading_day
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def _snapshots(conn) -> dict:
    out = {}
    for d, lab, s, p in conn.execute("SELECT date, label, series, price FROM global_snapshot WHERE price>0"):
        out.setdefault((str(d)[:10], lab), {})[s] = float(p)
    return out


def _gap(conn, d: date):
    """The Nifty's opening gap on d, in %: the first reading between 09:15 and 09:30 (a later one is not the
    open) against the previous session's last reading up to 15:30."""
    try:
        o = conn.execute("SELECT nifty50 FROM index_levels WHERE date=? AND time>='09:15' AND time<='09:30' AND "
                         "nifty50>0 ORDER BY time LIMIT 1", (str(d),)).fetchone()
        c = conn.execute("SELECT nifty50 FROM index_levels WHERE date=? AND time<='15:30' AND nifty50>0 ORDER BY time "
                         "DESC LIMIT 1", (str(_prev_session(d)),)).fetchone()
    except Exception:
        return None
    return (float(o[0]) / float(c[0]) - 1) * 100 if o and c else None


def moves_for(snaps: dict, d: date) -> dict:
    """Log moves (%) of each series from the previous session's close snapshot to d's pre snapshot."""
    pre, close = snaps.get((str(d), "pre"), {}), snaps.get((str(_prev_session(d)), "close"), {})
    return {k: math.log(pre[k] / close[k]) * 100 for k in SERIES if k in pre and k in close and close[k] > 0}


def design(conn) -> tuple:
    """(dates, X rows as dicts, gaps %, GIFT estimates %) for every morning with both snapshots and an open."""
    ensure_tables(conn)
    snaps = _snapshots(conn)
    days = sorted({_d(k[0]) for k in snaps if k[1] == "pre"})
    gift = {}
    try:
        for d, g in conn.execute("SELECT date, expected_gap_pct FROM market_cue WHERE expected_gap_pct IS NOT NULL "
                                 "ORDER BY captured_at"):
            gift[str(d)[:10]] = g                       # the latest capture of the morning wins
    except Exception:
        pass
    out_d, out_x, out_y, out_g = [], [], [], []
    for d in days:
        m = moves_for(snaps, d)
        y = _gap(conn, d)
        if len(m) < 4 or y is None:
            continue
        out_d.append(d)
        out_x.append(m)
        out_y.append(y)
        out_g.append(gift.get(str(d)))
    return out_d, out_x, out_y, out_g


def _ridge(X, y, lam=RIDGE):
    mu, sd = X.mean(0), X.std(0)
    sd[sd == 0] = 1.0
    Z = (X - mu) / sd
    ym = y.mean()
    b = np.linalg.solve(Z.T @ Z + lam * np.eye(Z.shape[1]), Z.T @ (y - ym))
    return b, mu, sd, ym


def model(conn, today: date | None = None) -> dict:
    """Fit on the stored mornings, report the walk-forward record, and estimate today's gap."""
    dates, xs, y, g = design(conn)
    n = len(y)
    if n < MIN_MORNINGS:
        return {"status": "COLLECTING", "mornings": n, "needed": MIN_MORNINGS,
                "reason": f"{n} of {MIN_MORNINGS} mornings with both snapshots and an open stored"}
    cols = [k for k in SERIES if sum(1 for x in xs if k in x) >= 0.8 * n]
    X = np.array([[x.get(k, np.nan) for k in cols] for x in xs], float)
    colmean = np.nanmean(X, 0)
    X = np.where(np.isnan(X), colmean, X)
    yv = np.array(y, float)
    preds, acts, gifts = [], [], []
    for t in range(max(MIN_FIT, n - EVAL_MORNINGS), n):
        b, mu, sd, ym = _ridge(X[:t], yv[:t])
        preds.append(float(ym + ((X[t] - mu) / sd) @ b))
        acts.append(yv[t])
        gifts.append(g[t])
    preds, acts = np.array(preds), np.array(acts)
    rmse, rmse0 = float(np.sqrt(np.mean((preds - acts) ** 2))), float(np.sqrt(np.mean(acts ** 2)))
    pairs = [(p, a, gg) for p, a, gg in zip(preds, acts, gifts) if gg is not None]
    rmse_gift = float(np.sqrt(np.mean([(gg - a) ** 2 for _p, a, gg in pairs]))) if len(pairs) >= 10 else None
    rmse_same = float(np.sqrt(np.mean([(p - a) ** 2 for p, a, _gg in pairs]))) if len(pairs) >= 10 else None
    big = np.abs(acts) > 0.2
    hit = float(np.mean(np.sign(preds[big]) == np.sign(acts[big])) * 100) if big.any() else None
    b, mu, sd, ym = _ridge(X, yv)
    out = {"status": "OK", "mornings": n, "inputs": [SERIES[k][1] for k in cols],
           "walk_forward": {"mornings": len(acts), "rmse_pct": round(rmse, 3), "rmse_zero_pct": round(rmse0, 3),
                            "beats_zero": rmse < rmse0, "direction_hit_rate_pct": round(hit, 1) if hit is not None else None,
                            "rmse_same_mornings_pct": round(rmse_same, 3) if rmse_same is not None else None,
                            "rmse_gift_pct": round(rmse_gift, 3) if rmse_gift is not None else None,
                            "beats_gift": (rmse_same < rmse_gift) if rmse_gift is not None else None},
           "sensitivity": {SERIES[k][1]: round(float(b[i] / sd[i]), 3) for i, k in enumerate(cols)}}
    t = today or date.today()
    m = moves_for(_snapshots(conn), t)
    if len([k for k in cols if k in m]) >= max(3, len(cols) // 2):
        x = np.array([m.get(k, mu[i]) for i, k in enumerate(cols)])
        z = (x - mu) / sd
        out["today"] = {"date": str(t), "expected_gap_pct": round(float(ym + z @ b), 3),
                        "moves_pct": {SERIES[k][1]: round(v, 3) for k, v in m.items()},
                        "contributions_pct": {SERIES[k][1]: round(float(z[i] * b[i]), 3) for i, k in enumerate(cols)}}
    return out


def run_capture(label: str) -> dict:
    """Scheduler entry: 15:31 ("close") and 08:42 ("pre") on trading days."""
    from db.schema import get_connection
    conn = get_connection()
    try:
        return capture(conn, label)
    finally:
        conn.close()
