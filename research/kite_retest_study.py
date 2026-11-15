"""
The Kite breakout-retest formula backtested on ATIP's own daily bars: setups, a resting buy at the breakout candle's
low, exit variants K1-K6, and the breadth20 / breadth50 regime gates.

The 15 Nov 2026 evidence pack (.ux_build/kite-gap-2026-11-15/scripts: kite_scan.py, kite_bt.py, kite_gate.py) made
importable and tested; with fills=PACK_FILLS it reproduces the pack's numbers. Reads one SQLite file read-only (a
backup), never calls a broker. strategy/breakout_retest.py is the live scanner of the same formula (one symbol, today);
tests/test_kite_retest_study.py checks the two agree on when a setup appears and where the buy rests.

THE FORMULA
    breakout   a green candle (close > open) on >= 3x the mean volume of the previous 20 sessions that closes above the
               highest high of the previous 20 sessions
    retest     within 20 sessions of the breakout, a high above the breakout candle's high has printed since, and the
               close is within +-1 % of the breakout candle's mid ((high + low) / 2)
    order      on the FIRST retest day (when the screener would show it and the GTT is placed) a resting buy at the
               breakout candle's low, valid 10 sessions; filled at min(open, level) on the first day the low reaches it
  Daily bars, EQ series, weekdays, ATIP's scored universe (529 on 15 Nov 2026). A close-to-close move over 35 % is a
  split, bonus or bad print: the symbol's history restarts there. A breakout needs 20 sessions before it, a retest day
  24 (so the 20-day windows are full). While a symbol's order rests or its position is open, its new setups are skipped.

EXITS (Variant)  stop % under the fill, target % (or "swing" = the high printed since the breakout), max_days sessions,
               optional trail (from %, by %). Per day: an open at or under the stop sells at that open (not on the fill
               day), a low through it at the stop; then the target (an open above it after the fill day at the open; a
               high through it at the target -- on the fill day only if the close is also above it); then the high-water
               mark and the trail. Still held after max_days -> sold at that close ("Time"; "OPEN" at the end of data).
    K1  GTT, stop 5 %, target 10 %, 20 days          (the Kite formula as the owner runs it)
    K2  GTT, stop 5 %, target 5 %, 20 days
    K3  GTT, stop 5 %, target = the post-breakout high, 20 days
    K4  GTT, stop 5 %, trail 3 % from +5 %, 40 days
    K5  GTT, ATIP's stop 2 % / target 3 %, 20 days
    K6  market buy at the next open instead of the resting order, stop 5 %, target 10 %, 20 days
  Rs 25,000 a trade, NSE delivery charges (research.session_ab_replay.charges), slippage per Fills (the pack: 5 bps on
  every exit and on a market entry; REALISTIC_FILLS: 15 bps on stop exits, owner item 17).

BREADTH  breadth(): breadth20 / breadth50 = % of the scored universe whose close is above its own 20- / 50-session simple
  average that day; a stock with fewer closes than the window counts as not above (kite_gate.py; the same definition as
  scores/breadth.py). 13 Nov 2026: breadth20 80.7, breadth50 56.8. A gate keeps the trades whose SETUP day (the retest
  day, when the order is placed) had the metric >= min.

PACK NUMBERS (2019-2026, backup of 15 Nov 2026): 4,705 setups, fill rate 56 %. K1 n=2,349 win 42.3 % avg +0.67 % PF 1.23,
  last 12 months -0.08 %; K1 with breadth50 >= 50: n=1,685 +0.97 % (last 12 months +1.14 %, n=177). Gated: K4 +0.62 %,
  K2 +0.34 %. Ungated: K5 -0.39 %, K6 +0.08 %.

USAGE
    python -m research.kite_retest_study --db atip_data/backups/atip-scheduled-20261115-191511.db --out OUT
    python -m research.kite_retest_study --db DB --out OUT --fills pack,realistic --variants K1,K4
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd

from research.session_ab_replay import CAPITAL, FILLS, PACK_FILLS, Fills, charges, connect_ro

log = logging.getLogger("atip.research.kite_retest")

VOL_LB, VOL_MULT, PX_LB = 20, 3.0, 20      # breakout: volume >= 3x the previous 20; close > the previous 20 highs
MAX_AGE, MID_TOL = 20, 1.0                 # retest within 20 sessions, close within +-1 % of the breakout mid
JUMP = 0.35                                # a bigger close-to-close move restarts the history (split / bad print)
MIN_SEGMENT, MIN_BREAKOUT_I, MIN_RETEST_T = 25, 20, 24
VALID_DAYS = 10                            # the resting buy (GTT) is valid this many sessions
GATES = (("none", None, None, None), ("b50>=50", "breadth50", 50.0, True), ("b50<50", "breadth50", 50.0, False),
         ("b20>=50", "breadth20", 50.0, True), ("b20<50", "breadth20", 50.0, False))


# ═══════════════════════════════════════════════════════════════════════════════════════════
#  DATA AND BREADTH
# ═══════════════════════════════════════════════════════════════════════════════════════════

def prepare_daily(raw: pd.DataFrame) -> pd.DataFrame:
    """EQ rows, one per (symbol, date) -- the latest written -- weekdays only, sorted (pack kite_scan.py)."""
    d = raw[raw["series"] == "EQ"].copy() if "series" in raw else raw.copy()
    d["date"] = pd.to_datetime(d["date"])
    keys = ["symbol", "date"] + (["created_at"] if "created_at" in d else [])
    d = d.sort_values(keys, kind="stable").drop_duplicates(["symbol", "date"], keep="last")
    d = d[d["date"].dt.day_of_week < 5]
    for k in ("open", "high", "low", "close", "volume"):
        if k in d:
            d[k] = d[k].astype(float)
    return d.reset_index(drop=True)


def load_daily(db_path) -> pd.DataFrame:
    """prices_daily of every symbol ATIP has scored, read-only, prepared."""
    con = connect_ro(db_path)
    try:
        raw = pd.read_sql("SELECT symbol, date, open, high, low, close, volume, series, created_at FROM prices_daily "
                          "WHERE symbol IN (SELECT DISTINCT symbol FROM ai_scores)", con)
    finally:
        con.close()
    return prepare_daily(raw)


def breadth(daily: pd.DataFrame, windows=(20, 50)) -> pd.DataFrame:
    """
    Pure. daily: symbol, date, close -- one row per symbol and trading day (prepare_daily). Returns one row per date:
    n (symbols with a close that day), above<w> and breadth<w> = 100 x above / n, where above = close > the symbol's own
    simple average of its last w closes (fewer than w closes: not above).
    """
    d = daily[["symbol", "date", "close"]].sort_values(["symbol", "date"], kind="stable")
    g = d.groupby("symbol")["close"]
    X = pd.DataFrame({"date": d["date"].to_numpy()})
    for w in windows:
        sma = g.transform(lambda s: s.rolling(w).mean()).to_numpy()
        with np.errstate(invalid="ignore"):
            X[f"above{w}"] = (d["close"].to_numpy() > sma).astype(int)
    out = X.groupby("date").agg(n=("date", "size"), **{f"above{w}": (f"above{w}", "sum") for w in windows})
    for w in windows:
        out[f"breadth{w}"] = out[f"above{w}"] / out["n"] * 100
    return out


# ═══════════════════════════════════════════════════════════════════════════════════════════
#  SETUPS
# ═══════════════════════════════════════════════════════════════════════════════════════════

def breakout_mask(o, h, c, v, vol_lb: int = VOL_LB, vol_mult: float = VOL_MULT, px_lb: int = PX_LB):
    """Pure, vectorised: (is_breakout, mean volume of the previous vol_lb, highest high of the previous px_lb)."""
    av = pd.Series(v, dtype=float).rolling(vol_lb).mean().shift(1).to_numpy()
    ph = pd.Series(h, dtype=float).rolling(px_lb).max().shift(1).to_numpy()
    with np.errstate(invalid="ignore"):
        brk = (c > o) & (v >= vol_mult * av) & (c > ph) & ~np.isnan(av)
    return brk, av, ph


def segment_setups(dates, o, h, l, c, v, max_age: int = MAX_AGE, mid_tol: float = MID_TOL, min_t: int = MIN_RETEST_T,
                   min_i: int = MIN_BREAKOUT_I, **kw) -> list[dict]:
    """Setups of one continuous price history (arrays oldest first): per breakout, its first retest day."""
    n = len(c)
    brk, av, ph = breakout_mask(o, h, c, v, **kw)
    out = []
    for i in np.flatnonzero(brk):
        if i < min_i:
            continue
        mid = (h[i] + l[i]) / 2
        ts = np.arange(i + 1, min(n, i + max_age + 1))
        if not len(ts):
            continue
        run_hi = np.maximum.accumulate(h[ts])
        ok = (ts >= min_t) & (run_hi > h[i]) & (np.abs((c[ts] - mid) / mid * 100) <= mid_tol)
        if not ok.any():
            continue
        j = int(np.argmax(ok))
        t = int(ts[j])
        out.append(dict(bdate=dates[i], tdate=dates[t], b_low=l[i], b_high=h[i], b_close=c[i], mid=mid, cur=c[t],
                        age=t - i, volx=v[i] / av[i], prev_high=ph[i], max_after=run_hi[j], seg_start=dates[0]))
    return out


def find_setups(daily: pd.DataFrame, jump: float = JUMP, min_segment: int = MIN_SEGMENT, **kw) -> pd.DataFrame:
    """Every setup in prepared daily bars (pack kite_scan.py): sym, bdate (breakout), tdate (first retest day = when the
    order is placed), b_low (the buy level), b_high, b_close, mid, cur, age, volx, prev_high, max_after, seg_start."""
    d = daily.sort_values(["symbol", "date"], kind="stable")
    rows = []
    for sym, g in d.groupby("symbol", sort=True):
        c_all = g["close"].to_numpy(float)
        pc = np.r_[np.nan, c_all[:-1]]
        with np.errstate(invalid="ignore", divide="ignore"):
            seg = np.cumsum(np.abs(c_all / pc - 1) > jump)
        dates = g["date"].to_numpy()
        arr = {k: g[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume")}
        for s in np.unique(seg):
            m = seg == s
            if m.sum() < min_segment:
                continue
            for r in segment_setups(dates[m], arr["open"][m], arr["high"][m], arr["low"][m], arr["close"][m],
                                    arr["volume"][m], **kw):
                rows.append(dict(sym=sym, **r))
    S = pd.DataFrame(rows)
    if len(S):
        for k in ("bdate", "tdate", "seg_start"):
            S[k] = pd.to_datetime(S[k])
    return S


# ═══════════════════════════════════════════════════════════════════════════════════════════
#  BACKTEST
# ═══════════════════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Variant:
    mode: str = "gtt"                 # gtt: resting buy at the breakout low; mkt: buy the next open
    stop: float = 5.0                 # % under the fill
    target: float | str | None = 10.0  # % above the fill; "swing" = the post-breakout high; None = no target
    max_days: int = 20                # sessions after the fill
    trail: tuple | None = None        # (from %, by %)
    window: int = VALID_DAYS          # sessions the resting buy is valid


VARIANTS = {
    "K1 GTT@breakout-low, SL5/TP10, 20d":   Variant(),
    "K2 GTT, SL5/TP5, 20d":                 Variant(target=5.0),
    "K3 GTT, SL5/TP=post-breakout high":    Variant(target="swing"),
    "K4 GTT, SL5, trail 3% from +5%, 40d":  Variant(target=None, max_days=40, trail=(5.0, 3.0)),
    "K5 GTT, ATIP-style SL2/TP3, 20d":      Variant(stop=2.0, target=3.0),
    "K6 market next open, SL5/TP10, 20d":   Variant(mode="mkt"),
}


def _arrays(daily: pd.DataFrame) -> dict:
    return {s: (g["date"].to_numpy("datetime64[ns]"), g["open"].to_numpy(float), g["high"].to_numpy(float),
                g["low"].to_numpy(float), g["close"].to_numpy(float))
            for s, g in daily.sort_values(["symbol", "date"], kind="stable").groupby("symbol", sort=False)}


def backtest(setups: pd.DataFrame, daily: pd.DataFrame | dict, variant: Variant, fills: Fills = PACK_FILLS,
             cap: float = CAPITAL) -> pd.DataFrame:
    """
    One trade per setup not blocked by the same symbol's resting order or open position (pack kite_bt.py run()).
    Rows: sym, tdate, bdate, b_low, filled (0: the resting buy expired); for fills also fdate, entry, exit, reason
    (Stop | Target | Time | OPEN), gap (stop sold at a gapped open), net (Rs after charges), netpct, days.
    """
    A = daily if isinstance(daily, dict) else _arrays(daily)
    S = setups.sort_values(["sym", "tdate"], kind="stable")
    up, xs, ss = 1 + fills.entry_slip_bps / 1e4, 1 - fills.exit_slip_bps / 1e4, 1 - fills.stop_slip_bps / 1e4
    V, sl, tp, tmax, trail = variant.window, variant.stop, variant.target, variant.max_days, variant.trail
    out, busy_until = [], {}
    for s in S.itertuples(index=False):
        ds, o, h, l, c = A[s.sym]
        t = int(np.searchsorted(ds, np.datetime64(s.tdate, "ns")))
        if busy_until.get(s.sym, -1) >= t:
            continue
        base = dict(sym=s.sym, tdate=s.tdate, bdate=s.bdate, b_low=s.b_low)
        if variant.mode == "gtt":
            trig, fi = s.b_low, None
            for k in range(t + 1, min(len(ds), t + 1 + V)):
                if l[k] <= trig:
                    fi, fpx = k, min(o[k], trig)
                    break
            if fi is None:
                busy_until[s.sym] = min(len(ds) - 1, t + V)
                out.append(dict(base, filled=0))
                continue
        elif variant.mode == "mkt":
            if t + 1 >= len(ds):
                continue
            fi = t + 1
            fpx = o[fi] * up
        else:
            raise ValueError(f"mode {variant.mode!r}")
        stop = fpx * (1 - sl / 100)
        tgt = (s.max_after if tp == "swing" else fpx * (1 + tp / 100)) if tp else None
        hw, reason, gap = fpx, None, False
        for k in range(fi, min(len(ds), fi + tmax + 1)):
            if k > fi and o[k] <= stop:
                gap = fills.gap_fill_at_open
                px, reason = (o[k] if gap else stop), "Stop"
                break
            if l[k] <= stop:
                px, reason = stop, "Stop"
                break
            if tgt and k > fi and o[k] >= tgt:
                px, reason = o[k], "Target"
                break
            if tgt and h[k] >= tgt and (k > fi or c[k] >= tgt):
                px, reason = tgt, "Target"
                break
            hw = max(hw, h[k])
            if trail and hw >= fpx * (1 + trail[0] / 100):
                stop = max(stop, hw * (1 - trail[1] / 100))
        if reason is None:
            k = min(len(ds) - 1, fi + tmax)
            px, reason = c[k], ("Time" if fi + tmax < len(ds) else "OPEN")
        px *= ss if reason == "Stop" else xs
        q = max(1, int(cap // fpx))
        B, Sv = q * fpx, q * px
        net = Sv - B - charges(B, Sv)
        busy_until[s.sym] = k
        out.append(dict(base, filled=1, fdate=pd.Timestamp(ds[fi]), entry=fpx, exit=px, reason=reason,
                        gap=bool(gap and reason == "Stop"), net=net, netpct=net / B * 100, days=k - fi))
    return pd.DataFrame(out)


# ═══════════════════════════════════════════════════════════════════════════════════════════
#  GATES AND SUMMARIES
# ═══════════════════════════════════════════════════════════════════════════════════════════

def with_breadth(trades: pd.DataFrame, br: pd.DataFrame) -> pd.DataFrame:
    """The filled trades with breadth20 / breadth50 of their setup day (tdate)."""
    f = trades[trades["filled"] == 1]
    return f.merge(br[["breadth20", "breadth50"]], left_on="tdate", right_index=True, how="left")


def gate(trades_b: pd.DataFrame, metric: str | None, min_value: float | None = 50.0, above: bool = True) -> pd.DataFrame:
    """Trades whose setup-day metric is >= min (above) or < min (not above); metric None = no gate."""
    if metric is None:
        return trades_b
    x = trades_b[metric]
    return trades_b[x >= min_value] if above else trades_b[x < min_value]


def block(x: pd.DataFrame) -> dict:
    """n, win %, average net %, profit factor of filled trades."""
    if len(x) == 0:
        return dict(n=0, win=None, avg=None, pf=None)
    w, ls = x[x["net"] > 0]["net"].sum(), -x[x["net"] <= 0]["net"].sum()
    return dict(n=int(len(x)), win=round(100 * float((x["net"] > 0).mean()), 1), avg=round(float(x["netpct"].mean()), 3),
                pf=round(float(w / max(1e-9, ls)), 2))


def periods(last_day) -> dict:
    """ALL; 2025-26; the last 12 months (from the day after last_day a year earlier: 14 Nov 2025 for 13 Nov 2026)."""
    last = pd.Timestamp(last_day)
    return {"all": None, "2025-26": pd.Timestamp("2025-01-01"),
            "last12m": last + pd.Timedelta(days=1) - pd.DateOffset(years=1)}


def summarise(results: dict, br: pd.DataFrame, last_day, fills_name: str = "pack") -> pd.DataFrame:
    """One row per variant x gate x period: fill rate, n, win %, avg %, PF."""
    rows = []
    for name, R in results.items():
        fb = with_breadth(R, br)
        fill_rate = round(100 * float(R["filled"].mean()), 1) if len(R) else None
        for gname, metric, lo, above in GATES:
            x = gate(fb, metric, lo, above)
            for pname, since in periods(last_day).items():
                y = x if since is None else x[x["fdate"] >= since]
                rows.append(dict(fills=fills_name, variant=name, gate=gname, period=pname, fill_rate=fill_rate,
                                 **block(y)))
    return pd.DataFrame(rows)


def run(daily: pd.DataFrame, variants: dict | None = None, fills: Fills = PACK_FILLS, setups: pd.DataFrame | None = None):
    """(setups, {variant: trades}) for prepared daily bars."""
    variants = variants or VARIANTS
    S = setups if setups is not None else find_setups(daily)
    A = _arrays(daily)
    return S, {name: backtest(S, A, v, fills) for name, v in variants.items()}


# ═══════════════════════════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════════════════════════

def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(prog="python -m research.kite_retest_study", description=__doc__.split("\n")[1])
    ap.add_argument("--db", required=True, help="SQLite file, opened read-only (a backup)")
    ap.add_argument("--out", required=True, help="directory for the CSV outputs")
    ap.add_argument("--fills", default="pack,realistic", help="comma list of: pack, realistic")
    ap.add_argument("--stop-slip-bps", type=float, help="override the realistic fills' stop-exit slippage")
    ap.add_argument("--variants", help="comma list of K1..K6 (default all)")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    daily = load_daily(a.db)
    last_day = daily["date"].max()
    br = breadth(daily)
    br.round(2).to_csv(out / "breadth.csv")
    S = find_setups(daily)
    S.to_csv(out / "kite_setups.csv", index=False)
    print(f"setups {len(S)} symbols {S['sym'].nunique()} by year {S['tdate'].dt.year.value_counts().sort_index().to_dict()}")
    print(f"breadth on {last_day.date()}: breadth20 {br['breadth20'].iloc[-1]:.1f}  breadth50 {br['breadth50'].iloc[-1]:.1f}")
    keys = [k.strip() for k in a.variants.split(",")] if a.variants else None
    variants = {k: v for k, v in VARIANTS.items() if keys is None or k.split()[0] in keys}
    allsum = []
    for fname in a.fills.split(","):
        f = FILLS[fname.strip()]
        if a.stop_slip_bps is not None and f.name == "realistic":
            f = replace(f, stop_slip_bps=a.stop_slip_bps)
        _, res = run(daily, variants, f, setups=S)
        for name, R in res.items():
            R.to_csv(out / f"kite_trades_{name.split()[0]}_{f.name}.csv", index=False)
        summ = summarise(res, br, last_day, f.name)
        allsum.append(summ)
        print(f"\n== {f.name} fills (stop exits {f.stop_slip_bps:g} bps, other exits {f.exit_slip_bps:g} bps)")
        for name in res:
            x = summ[(summ["variant"] == name)]
            for gname in ("none", "b50>=50", "b50<50", "b20>=50"):
                y = x[x["gate"] == gname].set_index("period")
                print(f"  {name[:3]} gate {gname:8s} fill {y['fill_rate'].iloc[0]:.0f}% | " + " | ".join(
                    f"{p} n={r['n']} win={r['win']}% avg={r['avg']:+.2f}% pf={r['pf']}" if r["n"] else f"{p} n=0"
                    for p, r in y.iterrows()))
    pd.concat(allsum).to_csv(out / "kite_summary.csv", index=False)
    return pd.concat(allsum)


if __name__ == "__main__":
    main()
