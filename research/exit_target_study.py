"""
Which profit target -- 3 %, 4 %, 5 %, 6 % or 8 % -- and which way of exiting at it, measured.

strategy/backtest_history.py answers "is the aggressive split better than a flat 3 % or 6 %" on
ATIP's own prices_daily (418 sessions, one regime, random entries). This study asks the wider
question the owner asked on 2026-10-26: across ATIP's entry rules, at what profit % should a
trade be taken, and how? It keeps every ATIP parameter that applies and varies only the exit.

ENTRIES  (signal on the close of day t, filled at the OPEN of t+1 -- scoring runs after the close)
    random     liquid names in random order: the control, "you are in a trade, now what?"
    momentum   close > SMA50 > SMA200, RSI-14 50-70, 20-day return > 0; best 60-day return first
               (the trend/momentum profile ATIP's ZPI and BUY gates reward)
    breakout   strategy_engine/library/12_volume_breakout.json: close above the prior 20-day high
               on >= 1.5x volume, above SMA50; highest volume ratio first
    rsi_mr     library/11_rsi_mean_reversion.json: RSI-14 < 30 above SMA200, volume >= 0.5x mean;
               most oversold (20-day z-score) first
    dip        library/40_dip.json / scores/backtest.py dip mode: close in the bottom 25 % of its
               20-day range; deepest first
  Every rule: 20-day average turnover >= Rs 5 Cr (scores/backtest.py), EQ series, no new entry on
  a day the market-health regime is BEAR or HIGH_RISK (scores/engine.py determine_signal), at most
  TOP_K candidates a day, and a symbol is not re-entered within COOLDOWN sessions.

EXITS  (same entries, same stop, same window, same costs -- only the exit changes)
    flat_T     sell everything at +T %
    atip_T     strategy/aggressive.py exactly: half at +T % (T1), the runner trails at
               clamp(4 x ATR %, 3, 20) below its high-water mark, +2T % is the momentum checkpoint
               (strong: hold, neutral: halve the trail, weak: bank the runner)
    atip_be_T  atip_T, but the runner's stop never sits below the entry price
    trail_T    no sale at +T %: the whole position's stop moves to breakeven and trails 4 x ATR
    hold       no target at all: initial stop and the time limit only (the "do nothing" control)
  Before the target the initial stop (STOP_PCT, ATIP's 3 %; or --stop-atr k for clamp(k x ATR %,
  2, 10)) protects 100 %; whatever is still held after HOLD sessions (ATIP's 30) is sold at that
  close.

DAILY BARS CANNOT SAY WHICH CAME FIRST, so two conventions are run:
    pessimistic   ATIP's own (strategy/backtest_aggressive.py): the low before the high; a runner
                  created on a bar is checked against that bar's low.
    ohlc          the usual bar path: a green bar runs open-low-high-close, a red bar open-high-
                  low-close.
  A conclusion that only holds under one of them is not a conclusion. Gaps fill at the open, not
  at the level: a stop gapped through fills at the (worse) open, a target gapped through at the
  (better) open -- which is what a resting order or the 15-minute live loop would get.

COSTS   COST_PCT round trip on every rupee that goes in and comes out (half on entry, half on each
        exit leg). 0.25 % is ATIP's assumption (backtest.costs.ROUND_TRIP_PCT); the statutory
        delivery cost alone is ~0.24 %. strategy/backtest_history.py (realised_pnl) charges it the
        same way since P1-5; it used to charge the 0.25 % on BOTH legs, ~0.5 % a round trip.

PORTFOLIO  The per-trade average hides the point of a lower target: capital comes back sooner and
        takes the next signal. So each policy is also run as ATIP would trade it -- MAX_POSITIONS
        slots, each sized at equity / MAX_POSITIONS at entry, best-ranked candidate first, cash
        from a partial exit idle until the slot closes -- giving CAGR, max drawdown and Sharpe on
        a daily marked-to-market equity curve.

DATA
    --source eod2   a checkout of github.com/BennyThadikaran/eod2_data (NSE bhavcopy, split- and
                    bonus-adjusted, 1995 -> last week). Survivorship bias: delisted names are not
                    in it, which flatters every long policy, the longest holds most.
    --source atip   ATIP's own prices_daily + market_health (run it on the Mac).

USAGE
    python -m research.exit_target_study --source eod2 --path ../eod2_data
    python -m research.exit_target_study --source atip --start 2025-01-27
    python -m research.exit_target_study --source eod2 --path ../eod2_data --cost 0.5 --hold 60
    python -m research.exit_target_study --source atip --start 2025-01-27 --stop-atr 3
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("atip.research.exit_study")

# ── ATIP's parameters (strategy/aggressive.py DEFAULTS, scores/backtest.py, scores/engine.py) ──
STOP_PCT = 3.0              # initial stop; live defaults it to target_1_pct (3) -- strategy/live.py
HOLD = 30                   # sessions; backtest_history HOLD_BARS / signal_log MAX_TRACK_SESSIONS
COST_PCT = 0.25             # round trip, = backtest.costs.ROUND_TRIP_PCT (tests/test_statutory_costs.py)
PARTIAL = 0.50              # target_1_exit_pct
TRAIL_ATR_MULT = 4.0
TRAIL_MIN, TRAIL_MAX = 3.0, 20.0
TIGHTEN = 0.5
STRONG, WEAK = 60.0, 40.0
MIN_TURNOVER_CR = 5.0
BLOCKED_REGIMES = ("BEAR", "HIGH_RISK")
TARGETS = (3.0, 4.0, 5.0, 6.0, 8.0)
RULES = ("random", "momentum", "breakout", "rsi_mr", "dip")
TOP_K = 10                  # candidates kept per signal day
COOLDOWN = 10               # sessions before the same symbol can be a candidate again
MAX_POSITIONS = 10          # library strategies' max_positions

# MH weights (db/schema.py); FII, DII and Global are not in bhavcopy data and are renormalised
# away exactly as compute_mh does for any missing input.
MH_WEIGHTS = {"NiftyTrend": 0.20, "BankNifty": 0.15, "Breadth": 0.10, "VIX": 0.10,
              "Sector": 0.10, "AdvanceDecline": 0.05}


# ═══════════════════════════════════════════════════════════════════════════════════════════
#  DATA
# ═══════════════════════════════════════════════════════════════════════════════════════════

@dataclass
class Market:
    dates: pd.DatetimeIndex
    symbols: list
    o: np.ndarray            # (dates, symbols) float64, NaN where the stock did not trade
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    eq: np.ndarray           # bool: the row is EQ series
    regime: np.ndarray       # (dates,) object: MH regime label or None


def _minmax(x, lo, hi, invert=False):
    s = np.clip((x - lo) / (hi - lo) * 100.0, 0.0, 100.0)
    return 100.0 - s if invert else s


def mh_regime(score):
    """scores/engine.py mh_regime."""
    if score is None or (isinstance(score, float) and math.isnan(score)):
        return None
    return ("STRONG_BULL" if score >= 80 else "BULL" if score >= 60 else "NEUTRAL" if score >= 40
            else "BEAR" if score >= 20 else "HIGH_RISK")


def _wide(frames: dict, col: str, dates) -> np.ndarray:
    return pd.DataFrame({s: f[col] for s, f in frames.items()}).reindex(dates).to_numpy(float, copy=True)


def load_eod2(path: str, start: str, end: str | None) -> Market:
    """Stocks (ISIN INE...) from an eod2_data checkout, plus a market-health regime per session."""
    root = Path(path)
    daily = root / "daily"
    sym2isin = json.loads((root / "isin_symbol_map.json").read_text())["sym2isin"]
    stocks = sorted(s.lower() for s, isin in sym2isin.items() if str(isin).startswith("INE"))
    warm = (pd.Timestamp(start) - pd.Timedelta(days=420)).strftime("%Y-%m-%d")
    frames = {}
    for s in stocks:
        f = daily / f"{s}.csv"
        if not f.exists():
            continue
        df = pd.read_csv(f, usecols=["Date", "Open", "High", "Low", "Close", "Volume", "Series"])
        df = df[df["Date"] >= warm]
        if end:
            df = df[df["Date"] <= end]
        if len(df) < 260:
            continue
        df.index = pd.to_datetime(df.pop("Date"))
        df = df[~df.index.duplicated(keep="last")]
        frames[s.upper()] = df
    nifty = _index(daily, "nifty 50", warm, end)
    dates = nifty.index                                   # the NSE session calendar
    syms = sorted(frames)
    log.info(f"eod2: {len(syms)} stocks, {len(dates)} sessions {dates[0].date()} .. {dates[-1].date()}")
    o, h, l, c, v = (_wide(frames, k, dates) for k in ("Open", "High", "Low", "Close", "Volume"))
    eq = pd.DataFrame({s: f["Series"].eq("EQ") for s, f in frames.items()}).reindex(dates)
    eq = eq.fillna(False).to_numpy(bool)
    bad = ~((h >= l) & (l > 0) & (o > 0) & (c > 0))
    for a in (o, h, l, c, v):
        a[bad] = np.nan
    m = Market(dates, syms, o, h, l, c, v, eq, np.full(len(dates), None, dtype=object))
    m.regime = _eod2_regime(m, daily, warm, end)
    return m


def _index(daily: Path, name: str, warm: str, end: str | None) -> pd.Series:
    df = pd.read_csv(daily / f"{name}.csv", usecols=["Date", "Close"])
    df = df[df["Date"] >= warm]
    if end:
        df = df[df["Date"] <= end]
    s = pd.Series(df["Close"].to_numpy(float), index=pd.to_datetime(df["Date"]))
    return s[~s.index.duplicated(keep="last")].dropna()


def _eod2_regime(m: Market, daily: Path, warm: str, end: str | None) -> np.ndarray:
    """
    compute_mh rebuilt from what bhavcopy data has: NiftyTrend, BankNifty, VIX, Sector (midcap 150
    + smallcap 250), Breadth (% above 200-DMA) and AdvanceDecline (% advancing), weighted as in
    db/schema.py and renormalised over what is present, like weighted_score.
    """
    idx = pd.DatetimeIndex(m.dates)
    chg = lambda n: (_index(daily, n, warm, end).pct_change() * 100).reindex(idx).to_numpy()
    comps = {"NiftyTrend": _minmax(chg("nifty 50"), -3, 3),
             "BankNifty": _minmax(chg("nifty bank"), -4, 4)}
    vix = _index(daily, "india vix", warm, end).reindex(idx).to_numpy()
    comps["VIX"] = _minmax(vix, 8, 35, invert=True)
    comps["Sector"] = _minmax((chg("nifty midcap 150") + chg("nifty smallcap 250")) / 2, -4, 4)
    c = m.c
    sma200 = pd.DataFrame(c).rolling(200, min_periods=200).mean().to_numpy()
    with np.errstate(invalid="ignore"):
        above = np.where(np.isnan(sma200) | np.isnan(c), np.nan, (c > sma200).astype(float))
        prev = np.vstack([np.full((1, c.shape[1]), np.nan), c[:-1]])
        adv = np.where(np.isnan(prev) | np.isnan(c), np.nan, (c > prev).astype(float))
        dec = np.where(np.isnan(prev) | np.isnan(c), np.nan, (c < prev).astype(float))
    with warnings.catch_warnings():               # sessions before any 200-DMA exists
        warnings.simplefilter("ignore", RuntimeWarning)
        comps["Breadth"] = np.nanmean(above, axis=1) * 100
    a, d = np.nansum(adv, axis=1), np.nansum(dec, axis=1)
    comps["AdvanceDecline"] = np.where(a + d > 0, a / np.maximum(a + d, 1) * 100, np.nan)
    num = np.zeros(len(idx)); den = np.zeros(len(idx))
    for k, w in MH_WEIGHTS.items():
        x = comps[k]
        ok = ~np.isnan(x)
        num[ok] += w * x[ok]; den[ok] += w
    # MH_MIN_COVERAGE = 0.50 of ATIP's full weight set (which sums to 1.0)
    score = np.where(den >= 0.5, num / np.maximum(den, 1e-9), np.nan)
    return np.array([mh_regime(float(x)) for x in score], dtype=object)


def load_atip(start: str, end: str | None) -> Market:
    """ATIP's prices_daily (EQ, not the benchmark) and its stored market_health regime."""
    from db.schema import get_connection
    warm = (pd.Timestamp(start) - pd.Timedelta(days=420)).strftime("%Y-%m-%d")
    conn = get_connection()
    try:
        q = ("SELECT symbol,date,open,high,low,close,volume FROM prices_daily "
             "WHERE series='EQ' AND source NOT IN ('dhan_index','nse_index') "
             "AND close>0 AND high>0 AND low>0 AND date>=?")
        args = [warm]
        if end:
            q += " AND date<=?"; args.append(end)
        rows = [tuple(r) for r in conn.execute(q, args).fetchall()]
        mh = {str(r[0])[:10]: r[1] for r in conn.execute(
            "SELECT date, regime FROM market_health WHERE regime IS NOT NULL").fetchall()}
    finally:
        conn.close()
    df = pd.DataFrame(rows, columns=["symbol", "date", "o", "h", "l", "c", "v"])
    df["date"] = pd.to_datetime(df["date"].astype(str).str[:10])
    df = df.drop_duplicates(["symbol", "date"], keep="last")
    dates = pd.DatetimeIndex(sorted(df["date"].unique()))
    wide = {k: df.pivot(index="date", columns="symbol", values=k).reindex(dates) for k in "ohlcv"}
    syms = list(wide["c"].columns)
    arr = {k: wide[k][syms].to_numpy(float) for k in "ohlcv"}
    regime = np.array([mh.get(str(d.date())) for d in dates], dtype=object)
    log.info(f"atip: {len(syms)} stocks, {len(dates)} sessions {dates[0].date()} .. {dates[-1].date()}")
    return Market(dates, syms, arr["o"], arr["h"], arr["l"], arr["c"], arr["v"],
                  ~np.isnan(arr["c"]), regime)


# ═══════════════════════════════════════════════════════════════════════════════════════════
#  FEATURES AND ENTRIES
# ═══════════════════════════════════════════════════════════════════════════════════════════

def features(m: Market) -> dict:
    """Everything an entry rule or the checkpoint needs, known at the close of each session."""
    C, H, L, V = (pd.DataFrame(a) for a in (m.c, m.h, m.l, m.v))
    f = {}
    f["turnover_cr"] = (C * V).rolling(20, min_periods=15).mean() / 1e7
    f["sma20"] = C.rolling(20, min_periods=20).mean()
    f["sma50"] = C.rolling(50, min_periods=50).mean()
    f["sma200"] = C.rolling(200, min_periods=200).mean()
    d = C.diff()
    up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    f["rsi14"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    pc = C.shift(1)
    tr = pd.concat([(H - L), (H - pc).abs(), (L - pc).abs()]).groupby(level=0).max()
    f["atr_pct"] = tr.rolling(14, min_periods=10).mean() / C * 100
    f["vol_ratio"] = V / V.rolling(20, min_periods=15).mean()
    f["prior_high20"] = H.rolling(20, min_periods=20).max().shift(1)
    lo20, hi20 = L.rolling(20, min_periods=20).min(), H.rolling(20, min_periods=20).max()
    f["range_pos"] = (C - lo20) / (hi20 - lo20).replace(0, np.nan) * 100
    f["zscore20"] = (C - f["sma20"]) / C.rolling(20, min_periods=20).std()
    f["ret20"] = C / C.shift(20) - 1
    f["ret60"] = C / C.shift(60) - 1
    # The checkpoint's momentum read, as strategy/backtest_history._momentum_from_bars builds it.
    m5 = C.rolling(5, min_periods=5).mean()
    zpi = (50 + (m5 / m5.shift(5) - 1) * 1000).clip(0, 100)
    msi = (C > C.shift(1)).astype(float).rolling(5, min_periods=5).sum() / 5 * 100
    f["momentum"] = (zpi + msi) / 2
    return {k: x.to_numpy(float) for k, x in f.items()}


def candidates(m: Market, f: dict, rule: str, start: str, hold: int, seed: int = 7) -> pd.DataFrame:
    """
    Top TOP_K entries per signal day for one rule: columns t (signal session), s (symbol column),
    rank (higher = taken first). Needs `hold` sessions after the fill, so the tail is cut.
    """
    n_d, n_s = m.c.shape
    with np.errstate(invalid="ignore"):
        ok = (f["turnover_cr"] >= MIN_TURNOVER_CR) & m.eq & ~np.isnan(m.c)
        c = m.c
        if rule == "random":
            rng = np.random.default_rng(seed)
            cond, rank = ok, rng.random((n_d, n_s))
        elif rule == "momentum":
            cond = ok & (c > f["sma50"]) & (f["sma50"] > f["sma200"]) & (f["rsi14"] >= 50) \
                & (f["rsi14"] <= 70) & (f["ret20"] > 0)
            rank = f["ret60"]
        elif rule == "breakout":
            cond = ok & (c > f["prior_high20"]) & (f["vol_ratio"] >= 1.5) & (c > f["sma50"])
            rank = f["vol_ratio"]
        elif rule == "rsi_mr":
            cond = ok & (f["rsi14"] < 30) & (c > f["sma200"]) & (f["vol_ratio"] >= 0.5)
            rank = -f["zscore20"]
        elif rule == "dip":
            cond = ok & (f["range_pos"] <= 25)
            rank = -f["range_pos"]
        else:
            raise ValueError(rule)
    blocked = np.array([r in BLOCKED_REGIMES for r in m.regime])
    t0 = int(np.searchsorted(m.dates.values, np.datetime64(pd.Timestamp(start))))
    last_signal = n_d - 1 - hold                    # fill at t+1, `hold` bars from there
    out, cool = [], np.full(n_s, -10 ** 9)
    for t in range(max(t0, 1), last_signal + 1):
        if blocked[t]:
            continue
        idx = np.flatnonzero(cond[t] & ~np.isnan(rank[t]) & (t - cool > COOLDOWN))
        if not idx.size:
            continue
        top = idx[np.argsort(-rank[t, idx], kind="stable")][:TOP_K]
        for s in top:
            out.append((t, s, float(rank[t, s])))
            cool[s] = t
    return pd.DataFrame(out, columns=["t", "s", "rank"])


@dataclass
class Paths:
    t: np.ndarray            # signal session
    s: np.ndarray
    rank: np.ndarray
    entry: np.ndarray        # open of t+1
    o: np.ndarray            # (N, hold) bars from the entry session on
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    atr: np.ndarray          # ATR % at the signal close
    mom: np.ndarray          # (N, hold) momentum read at the close BEFORE each bar


def build_paths(m: Market, f: dict, cand: pd.DataFrame, hold: int) -> Paths:
    t, s = cand["t"].to_numpy(), cand["s"].to_numpy()
    k = np.arange(hold)
    rows = (t + 1)[:, None] + k[None, :]
    o, h, l, c = (a[rows, s[:, None]] for a in (m.o, m.h, m.l, m.c))
    mom = f["momentum"][rows - 1, s[:, None]]
    keep = ~(np.isnan(o).any(1) | np.isnan(h).any(1) | np.isnan(l).any(1) | np.isnan(c).any(1))
    # A >40 % one-session jump in adjusted data is a corporate action the adjustment missed.
    prev = np.concatenate([m.c[t, s][:, None], c[:, :-1]], axis=1)
    keep &= (np.abs(c / prev - 1) < 0.4).all(1) & (np.abs(o / prev - 1) < 0.4).all(1)
    atr = f["atr_pct"][t, s]
    return Paths(t[keep], s[keep], cand["rank"].to_numpy()[keep], o[keep, 0].copy(), o[keep],
                 h[keep], l[keep], c[keep], atr[keep], mom[keep])


# ═══════════════════════════════════════════════════════════════════════════════════════════
#  EXIT SIMULATION (vectorised over trades, one bar at a time)
# ═══════════════════════════════════════════════════════════════════════════════════════════

@dataclass
class Policy:
    name: str
    kind: str                 # flat | atip | atip_be | trail | hold
    t1: float = math.inf
    t2: float = math.inf
    stop: float = STOP_PCT
    stop_atr: float | None = None     # initial stop = clamp(stop_atr x ATR %, 2, 10) instead of `stop`

    @staticmethod
    def grid(targets=TARGETS, stop=STOP_PCT, kinds=("flat", "atip", "atip_be", "trail"), stop_atr=None):
        out = [Policy(f"{k}_{t:g}", k, t, 2 * t, stop, stop_atr) for k in kinds for t in targets]
        out.append(Policy("hold", "hold", stop=stop, stop_atr=stop_atr))
        return out


@dataclass
class Result:
    net: np.ndarray           # per trade, % of capital put in, after costs
    cap_days: np.ndarray      # position-fraction x sessions held
    exit_bar: np.ndarray
    t1: np.ndarray
    t2: np.ndarray
    reason: np.ndarray        # 1 stop, 2 target (all), 3 trail, 4 checkpoint exit, 5 time
    rem: np.ndarray = field(repr=False, default=None)   # (N, hold) fraction still held after bar
    cash: np.ndarray = field(repr=False, default=None)  # (N, hold) cumulative cash back per unit


def trail_pct(atr):
    """strategy/aggressive.trail_distance_pct."""
    base = np.where(np.isnan(atr) | (atr <= 0), np.nan, atr * TRAIL_ATR_MULT)
    return np.clip(np.where(np.isnan(base), TRAIL_MIN, base), TRAIL_MIN, TRAIL_MAX)


def simulate(p: Paths, pol: Policy, cost_pct: float = COST_PCT, order: str = "pessimistic",
             keep_paths: bool = False) -> Result:
    n, hold = p.o.shape
    E = p.entry
    half = cost_pct / 100 / 2
    rem = np.ones(n)
    cash = np.zeros(n)                      # per unit invested, net of exit costs
    t1 = np.zeros(n, bool); t2 = np.zeros(n, bool)
    exit_bar = np.full(n, hold - 1); reason = np.zeros(n, int)
    cap_days = np.zeros(n)
    stop_pct = np.full(n, pol.stop)
    if pol.stop_atr:
        stop_pct = np.where(np.isnan(p.atr), pol.stop, np.clip(pol.stop_atr * p.atr, 2.0, 10.0))
    stop_px = E * (1 - stop_pct / 100)
    t1_px = E * (1 + pol.t1 / 100) if math.isfinite(pol.t1) else np.full(n, np.inf)
    t2_px = E * (1 + pol.t2 / 100) if math.isfinite(pol.t2) else np.full(n, np.inf)
    trail = trail_pct(p.atr)
    tstop = np.full(n, np.nan)
    hw = E.copy()
    floor = E.copy() if pol.kind in ("atip_be", "trail") else np.full(n, -np.inf)
    runner_kind = pol.kind in ("atip", "atip_be", "trail")
    sell_at_t1 = 1.0 if pol.kind == "flat" else PARTIAL if pol.kind in ("atip", "atip_be") else 0.0
    checkpoint = pol.kind in ("atip", "atip_be")
    R = np.zeros((n, hold)) if keep_paths else None
    Cc = np.zeros((n, hold)) if keep_paths else None

    def sell(mask, frac_of_rem, px, why, k):
        if not mask.any():
            return
        q = rem[mask] * frac_of_rem if frac_of_rem is not None else None
        qty = q if q is not None else rem[mask]
        cash[mask] += qty * px[mask] / E[mask] * (1 - half)
        rem[mask] -= qty
        done = mask.copy(); done[mask] = rem[mask] <= 1e-12
        rem[done] = 0.0
        exit_bar[done] = k; reason[done] = why

    def ratchet(mask):
        cand = hw * (1 - trail / 100)
        cand = np.maximum(cand, floor)
        tstop[mask] = np.where(np.isnan(tstop[mask]), cand[mask], np.maximum(tstop[mask], cand[mask]))

    def down(px, k, at_open, which):
        """Price moves down to px. which: 'all' | 'new' (runners created this bar only)."""
        live = rem > 0
        if which == "all":
            pre = live & ~t1
            hit = pre & (px <= stop_px)
            sell(hit, None, np.minimum(stop_px, px) if at_open else stop_px, 1, k)
        run = (rem > 0) & t1 & ~np.isnan(tstop)
        if which == "new":
            run &= new_runner
        hit = run & (px <= tstop)
        sell(hit, None, np.minimum(tstop, px) if at_open else tstop, 3, k)

    def up(px, k, at_open):
        nonlocal new_runner
        live = rem > 0
        hit = live & ~t1 & (px >= t1_px)
        fill = np.maximum(t1_px, px) if at_open else t1_px
        if hit.any():
            t1[hit] = True
            if sell_at_t1 == 1.0:
                sell(hit, None, fill, 2, k)
            elif sell_at_t1 > 0:
                # half of the ORIGINAL position (partial_exit_quantity)
                sell(hit, sell_at_t1, fill, 2, k)
            if runner_kind:
                hw[hit] = np.maximum(hw[hit], px[hit])
                new_runner |= hit
                ratchet(hit)
        return hit

    def after_up(px, k, at_open):
        """Checkpoint, then the high-water mark and the ratchet, for every open runner."""
        run = (rem > 0) & t1
        if checkpoint:
            cp = run & ~t2 & (px >= t2_px)
            if cp.any():
                t2[cp] = True
                mom = p.mom[:, k]
                strong = cp & (mom >= STRONG)
                weak = cp & (mom < WEAK)
                neutral = cp & ~strong & ~weak       # incl. no reading: TIGHTEN_TRAIL
                fill = np.maximum(t2_px, px) if at_open else t2_px
                sell(weak, None, fill, 4, k)
                trail[neutral] = np.maximum(TRAIL_MIN, trail[neutral] * TIGHTEN)
        run = (rem > 0) & t1
        hw[run] = np.maximum(hw[run], px[run])
        ratchet(run)

    for k in range(hold):
        o, h, l, c = p.o[:, k], p.h[:, k], p.l[:, k], p.c[:, k]
        cap_days += rem
        new_runner = np.zeros(n, bool)
        if k > 0:                                    # the entry bar opens AT the entry price
            down(o, k, True, "all")
            up(o, k, True)
            after_up(o, k, True)
            new_runner[:] = False
        if order == "pessimistic":
            # strategy/backtest_aggressive.simulate: stop on the low, then target on the high; a
            # runner born on this bar is checked against this bar's low before the checkpoint.
            down(l, k, False, "all")
            up(h, k, False)
            down(l, k, False, "new")
            after_up(h, k, False)
        else:
            green = c >= o
            for g in (True, False):
                sel = green if g else ~green
                if not sel.any():
                    continue
                saved = rem.copy()
                rem[~sel] = 0.0                      # mask the other colour for this pass
                if g:                                # open -> low -> high -> close
                    down(l, k, False, "all")
                    up(h, k, False)
                    after_up(h, k, False)
                    down(c, k, False, "all")
                else:                                # open -> high -> low -> close
                    up(h, k, False)
                    after_up(h, k, False)
                    down(l, k, False, "all")
                rem[~sel] = saved[~sel]
        if keep_paths:
            R[:, k] = rem; Cc[:, k] = cash
    # time limit: sell what is left at the last close
    last = rem > 0
    if last.any():
        cash[last] += rem[last] * p.c[last, -1] / E[last] * (1 - half)
        reason[last] = 5
        exit_bar[last] = hold - 1
        rem[last] = 0.0
        if keep_paths:
            Cc[:, -1] = cash; R[:, -1] = 0.0
    net = (cash - (1 + half)) * 100              # invested 1 unit plus the entry half of costs
    return Result(net, cap_days, exit_bar, t1, t2, reason, R, Cc)


# ═══════════════════════════════════════════════════════════════════════════════════════════
#  METRICS
# ═══════════════════════════════════════════════════════════════════════════════════════════

def trade_metrics(r: Result) -> dict:
    x = r.net
    n = len(x)
    if not n:
        return {"trades": 0}
    gp, gl = x[x > 0].sum(), -x[x < 0].sum()
    return {
        "trades": n,
        "avg_net_pct": round(float(x.mean()), 3),
        "median_net_pct": round(float(np.median(x)), 3),
        "win_rate_pct": round(float((x > 0).mean() * 100), 1),
        "profit_factor": round(float(gp / gl), 2) if gl > 0 else None,
        "avg_days": round(float(r.cap_days.mean()), 1),
        # return per unit of capital per session: the efficiency figure
        "bps_per_day": round(float(x.sum() / r.cap_days.sum() * 100), 2),
        "t1_hit_pct": round(float(r.t1.mean() * 100), 1),
        "stopped_pct": round(float((r.reason == 1).mean() * 100), 1),
        "trail_pct": round(float((r.reason == 3).mean() * 100), 1),
        "time_exit_pct": round(float((r.reason == 5).mean() * 100), 1),
        "worst_pct": round(float(x.min()), 2),
        "best_pct": round(float(x.max()), 2),
    }


def portfolio(m: Market, p: Paths, r: Result, slots: int = MAX_POSITIONS,
              cost_pct: float = COST_PCT) -> dict:
    """
    Daily equity of a slot-limited book: fills at t+1's open in rank order while a slot is free,
    each sized at equity / slots; a partial exit's cash waits until its slot closes.
    """
    half = cost_pct / 100 / 2
    n_d = len(m.dates)
    by_day: dict[int, list] = {}
    for i in np.lexsort((-p.rank, p.t)):
        by_day.setdefault(int(p.t[i]) + 1, []).append(i)
    first = min(by_day) if by_day else 0
    cash, equity = 1.0, [1.0]
    open_pos: dict[int, tuple] = {}          # trade -> (alloc, entry day, cash already counted)
    held = set()
    curve_days = []
    for d in range(first, n_d):
        # fills at today's open, sized on yesterday's equity
        for i in by_day.get(d, []):
            if len(open_pos) >= slots:
                break
            if p.s[i] in held:
                continue
            alloc = min(equity[-1] / slots, cash / (1 + half))
            if alloc < 0.5 * equity[-1] / slots:
                continue
            cash -= alloc * (1 + half)
            open_pos[i] = (alloc, d, 0.0)
            held.add(p.s[i])
        # end of day: cash from exits, mark the rest to the close
        mtm = 0.0
        for i in list(open_pos):
            alloc, d0, got = open_pos[i]
            k = d - d0
            back = r.cash[i, k]
            cash += alloc * (back - got)
            if r.rem[i, k] <= 0:
                del open_pos[i]
                held.discard(p.s[i])
                continue
            open_pos[i] = (alloc, d0, back)
            mtm += alloc * r.rem[i, k] * p.c[i, k] / p.entry[i]
        equity.append(cash + mtm)
        curve_days.append(d)
        if not open_pos and d > max(by_day, default=0):
            break
    eq = np.array(equity[1:])
    if len(eq) < 2:
        return {}
    years = (m.dates[curve_days[-1]] - m.dates[curve_days[0]]).days / 365.25
    ret = np.diff(np.concatenate([[1.0], eq])) / np.concatenate([[1.0], eq[:-1]])
    peak = np.maximum.accumulate(eq)
    return {
        "cagr_pct": round(float((eq[-1] ** (1 / years) - 1) * 100), 2) if years > 0 and eq[-1] > 0 else None,
        "max_dd_pct": round(float(((eq - peak) / peak).min() * 100), 1),
        "sharpe": round(float(ret.mean() / ret.std() * math.sqrt(252)), 2) if ret.std() > 0 else None,
        "final_multiple": round(float(eq[-1]), 3),
        "years": round(years, 1),
    }


def by_period(p: Paths, r: Result, dates, edges) -> dict:
    yrs = pd.DatetimeIndex(dates[p.t])
    out = {}
    for a, b in edges:
        sel = (yrs >= pd.Timestamp(a)) & (yrs < pd.Timestamp(b))
        out[f"{a[:4]}-{str(int(b[:4]) - 1)}"] = round(float(r.net[sel].mean()), 3) if sel.any() else None
    return out


# ═══════════════════════════════════════════════════════════════════════════════════════════
#  DRIVER
# ═══════════════════════════════════════════════════════════════════════════════════════════

PERIODS = (("2015-01-01", "2020-01-01"), ("2020-01-01", "2023-01-01"), ("2023-01-01", "2027-01-01"))


def run(m: Market, start: str, rules=RULES, policies=None, cost_pct=COST_PCT, hold=HOLD,
        orders=("pessimistic", "ohlc"), with_portfolio=True) -> dict:
    f = features(m)
    policies = policies or Policy.grid()
    out = {"start": start, "end": str(m.dates[-1].date()), "cost_pct": cost_pct, "hold": hold,
           "stop_pct": f"{policies[0].stop_atr:g}x ATR" if policies[0].stop_atr else policies[0].stop,
           "rules": {}}
    for rule in rules:
        cand = candidates(m, f, rule, start, hold)
        p = build_paths(m, f, cand, hold)
        log.info(f"{rule}: {len(p.t)} trades")
        rr = {"trades": int(len(p.t)), "orders": {}}
        for order in orders:
            res = {}
            for pol in policies:
                r = simulate(p, pol, cost_pct, order, keep_paths=with_portfolio and order == orders[0])
                row = trade_metrics(r)
                row["by_period"] = by_period(p, r, m.dates, PERIODS)
                if with_portfolio and order == orders[0]:
                    row["portfolio"] = portfolio(m, p, r, cost_pct=cost_pct)
                res[pol.name] = row
            rr["orders"][order] = res
        out["rules"][rule] = rr
    return out


def print_report(out: dict):
    print(f"\n{'=' * 118}")
    stop = out["stop_pct"] if isinstance(out["stop_pct"], str) else f"{out['stop_pct']:g}%"
    print(f"  Exit study {out['start']} .. {out['end']}  stop {stop}  hold {out['hold']}  "
          f"costs {out['cost_pct']}% round trip   by period: avg % for " + ", ".join(
              f"{a[:4]}-{int(b[:4]) - 1}" for a, b in PERIODS))
    for rule, rr in out["rules"].items():
        for order, res in rr["orders"].items():
            print(f"\n  {rule.upper()}  ({rr['trades']} trades, {order} bar order)")
            print(f"  {'policy':12s}{'avg%':>8s}{'win%':>7s}{'PF':>6s}{'days':>6s}{'bps/d':>7s}"
                  f"{'T1%':>6s}{'stop%':>7s}{'time%':>7s}  {'by period':30s}"
                  f"{'CAGR':>7s}{'maxDD':>7s}{'Shrp':>6s}")
            for name, row in res.items():
                pf = row.get("portfolio") or {}
                per = " ".join(f"{v:+.2f}" if v is not None else "  -  "
                               for v in row["by_period"].values())
                print(f"  {name:12s}{row['avg_net_pct']:+8.3f}{row['win_rate_pct']:7.1f}"
                      f"{(row['profit_factor'] or 0):6.2f}{row['avg_days']:6.1f}{row['bps_per_day']:+7.2f}"
                      f"{row['t1_hit_pct']:6.1f}{row['stopped_pct']:7.1f}{row['time_exit_pct']:7.1f}  "
                      f"{per:30s}"
                      + (f"{pf.get('cagr_pct') or 0:7.1f}{pf.get('max_dd_pct') or 0:7.1f}"
                         f"{pf.get('sharpe') or 0:6.2f}" if pf else ""))
    print()


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--source", choices=["eod2", "atip"], default="atip")
    ap.add_argument("--path", help="eod2_data checkout (with --source eod2)")
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end")
    ap.add_argument("--cost", type=float, default=COST_PCT)
    ap.add_argument("--hold", type=int, default=HOLD)
    ap.add_argument("--stop", type=float, default=STOP_PCT)
    ap.add_argument("--stop-atr", type=float, help="initial stop = clamp(k x ATR %%, 2, 10) instead of --stop")
    ap.add_argument("--rules", default=",".join(RULES))
    ap.add_argument("--no-portfolio", action="store_true")
    ap.add_argument("--json", help="write the full result here")
    a = ap.parse_args(argv)
    m = load_eod2(a.path, a.start, a.end) if a.source == "eod2" else load_atip(a.start, a.end)
    out = run(m, a.start, rules=a.rules.split(","), policies=Policy.grid(stop=a.stop, stop_atr=a.stop_atr),
              cost_pct=a.cost, hold=a.hold, with_portfolio=not a.no_portfolio)
    out["source"] = a.source
    print_report(out)
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=1, default=str))
    return out


if __name__ == "__main__":
    main()
