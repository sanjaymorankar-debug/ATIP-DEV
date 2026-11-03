"""
Score validation (SV-01, 3 Nov 2026): do VPI / MRI / RRI / ZPI / CRI / ATIP / ACS predict anything?

Two views, because they answer different questions:

  STORED   the ai_scores rows production wrote (27 Jul - 3 Nov 2026, 44 sessions). The scores as deployed,
           every input real, but a short history: few sessions have a 20-day outcome.
  PIT      the price-derived scores recomputed point-in-time for every session in prices_daily (413
           sessions), by calling the engine's own compute_* functions on indicators computed only from bars
           up to and including that session. News, fundamentals and ownership were not stored per session
           before 27 Jul, so they are absent (the engine renormalises over what is present, exactly as it
           does in production when they are missing) and MSI -- one number per day, no cross-sectional
           information -- is left out of the ATIP composite.

NO LOOK-AHEAD. A signal is formed from the close of session t. It is acted on at the OPEN of t+1 and the
return to horizon h is measured to the CLOSE of t+h (h = 1, 5, 10, 20): open(t+1) -> close(t+h). Every
statistic is cross-sectional per session (Spearman rank IC, decile spread) and then averaged over
sessions; nothing is normalised across the whole history. A forward window containing a one-day move of
more than JUMP_LIMIT (an unadjusted split or bonus, a bad bar) is dropped, not scored.

WALK-FORWARD. Sessions are split by time: the first 70 % in-sample, the last 30 % out-of-sample. Nothing
here is fitted in-sample except the optional re-weighting, which is only adopted if it also improves the
out-of-sample IC.

KNOWN LIMITS (reported, not hidden): the universe is the tracked set that is in prices_daily today, so it
is survivorship-biased; prices_daily holds one adjusted basis (data/corporate_actions.py) but 237 corporate
actions are still pending, so JUMP_LIMIT masks the damaged windows.

    python -m scores.validation --db atip_data/atip.db --out docs/reports/2026-11-03
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

HORIZONS = (1, 5, 10, 20)
OOS_FRACTION = 0.30
JUMP_LIMIT = 0.35            # one-day |return| above this inside a window: unadjusted corporate action
MIN_BARS = 300               # a symbol needs this many sessions to be in the tracked-history universe
WARMUP = 60                  # sessions before a symbol's first scored day (RSI/ADX/MACD settle)
SCORES = ("atip", "vpi", "mri", "rri", "zpi", "cri", "acs")
EXPECTED_SIGN = {"atip": 1, "vpi": 1, "mri": 1, "rri": 1, "zpi": 1, "acs": 1, "cri": -1}   # CRI: lower is better
WORKING_T = 2.0              # |IC t-stat| out of sample for WORKING
WEAK_T = 1.0


# ── data ────────────────────────────────────────────────────────────────────────────────────────
def load_panel(db_path, min_bars: int = MIN_BARS) -> dict:
    """{field: DataFrame[date x symbol]} for open/high/low/close/volume of the long-history EQ symbols."""
    c = sqlite3.connect(str(db_path))
    df = pd.read_sql("SELECT symbol,date,open,high,low,close,volume FROM prices_daily WHERE series='EQ' "
                     "AND symbol NOT IN ('NIFTY50','INDIAVIX') ORDER BY date", c)
    c.close()
    n = df.groupby("symbol")["date"].transform("size")
    df = df[n >= min_bars]
    return {f: df.pivot(index="date", columns="symbol", values=f).sort_index()
            for f in ("open", "high", "low", "close", "volume")}


def _wilder(s: pd.DataFrame, n: int) -> pd.DataFrame:
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def indicators(p: dict) -> dict:
    """Point-in-time indicators, one DataFrame[date x symbol] each. Every value at row t uses bars <= t."""
    c, h, l, v = p["close"], p["high"], p["low"], p["volume"]
    d = c.diff()
    rs = _wilder(d.clip(lower=0), 14) / _wilder((-d).clip(lower=0), 14)
    rsi = 100 - 100 / (1 + rs)
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()]).groupby(level=0).max()
    atr = _wilder(tr, 14)
    up, dn = h.diff(), -l.diff()
    pdm = up.where((up > dn) & (up > 0), 0.0)
    mdm = dn.where((dn > up) & (dn > 0), 0.0)
    pdi = 100 * _wilder(pdm, 14) / atr
    mdi = 100 * _wilder(mdm, 14) / atr
    adx = _wilder(100 * (pdi - mdi).abs() / (pdi + mdi), 14)
    e12, e26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    macd = e12 - e26
    hist = macd - macd.ewm(span=9, adjust=False).mean()
    e50 = c.ewm(span=50, adjust=False).mean()
    s200 = c.rolling(200, min_periods=200).mean()
    gap = e50 - s200
    return {"rsi": rsi, "atr_pct": atr / c * 100, "adx": adx, "macd_hist_pct": hist / c * 100,
            "ema9": c.ewm(span=9, adjust=False).mean(), "ema21": c.ewm(span=21, adjust=False).mean(),
            "sma200": s200, "volume_ratio": v / v.rolling(20, min_periods=20).mean(),
            "death_cross": ((gap.shift(5) > 0) & ~(gap > 0)).astype(float).where(gap.notna() & gap.shift(5).notna()),
            "turnover20": (c * v).rolling(20, min_periods=20).mean(),
            "ret20": c / c.shift(20) - 1, "low252": c.rolling(252, min_periods=60).min()}


def _w(conn, name):
    from scores.engine import load_weights
    return load_weights(conn, name)


def score_panel(db_path, p: dict | None = None, fns: dict | None = None, weights: dict | None = None) -> pd.DataFrame:
    """Long frame [date, symbol, vpi, mri, rri, cri, zpi, atip, acs] from the engine's own compute_* functions."""
    from scores import engine as E
    fns = fns or {}
    p = p or load_panel(db_path)
    ind = indicators(p)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    W = {k: _w(conn, k) for k in ("VPI", "MRI", "RRI", "CRI", "ZPI", "ATIP", "ACS")}
    conn.close()
    for k, v in (weights or {}).items():
        W[k] = v
    close = p["close"]
    bench = _nifty_ret20(db_path, close.index)
    dates, syms = close.index, close.columns
    arr = {k: v.to_numpy() for k, v in ind.items()}
    cl = close.to_numpy()
    rows = []
    neutral_mh = {"mh_score": 50, "vix_level": None}
    first = np.argmax(~np.isnan(cl), axis=0)
    for j, s in enumerate(syms):
        for i in range(first[j] + WARMUP, len(dates)):
            def g(k):
                x = arr[k][i, j]
                return None if x != x else float(x)
            s200 = g("sma200")
            tech = {"rsi_14": g("rsi"), "adx_14": g("adx"), "atr_pct": g("atr_pct"), "volume_ratio": g("volume_ratio"),
                    "macd_hist_pct": g("macd_hist_pct"), "ema_9": g("ema9"), "ema_21": g("ema21"), "sma_200": s200,
                    # unknown (< 200 sessions) is None, never 0: the engine treats 0 as "below the 200-DMA"
                    "above_200dma": None if s200 is None else int(cl[i, j] > s200),
                    "death_cross": g("death_cross")}
            tech = {k: v for k, v in tech.items() if v is not None}
            lo = g("low252")
            sub = pd.DataFrame({"close": [cl[i, j], lo if lo else cl[i, j]], "volume": [np.nan, np.nan]})
            to = g("turnover20")
            liq = None if to is None else round(min(to / 1e9 * 100, 100), 2)
            r20 = g("ret20")
            b20 = bench.iloc[i]
            rsv = None if r20 is None or b20 != b20 else E.minmax((r20 - b20) * 100, -15, 15)
            vpi = fns.get('vpi', E.compute_vpi)(tech, sub, {}, pd.DataFrame(), 50.0, W["VPI"], rs=rsv, liquidity=liq)
            mri = fns.get('mri', E.compute_mri)(tech, 50.0, W["MRI"])
            rri = fns.get('rri', E.compute_rri)(tech, sub, pd.DataFrame(), 50.0, W["RRI"])
            cri = fns.get('cri', E.compute_cri)(tech, {}, 50.0, neutral_mh, W["CRI"])
            zpi = fns.get('zpi', E.compute_zpi)(tech, pd.DataFrame(), 50.0, 50.0, W["ZPI"])
            atip = round(E.weighted_score({"VPI": vpi, "RRI": rri, "MRI": mri, "ZPI": zpi, "SAFETY": 100 - cri},
                                          W["ATIP"]), 2)
            ag = [x for x in (vpi, mri, rri, zpi) if x]
            acs = round(E.weighted_score({"Agreement": sum(1 for x in ag if x > 60) / len(ag) * 100 if ag else None,
                                          "Liquidity": liq}, W["ACS"]), 2)
            rows.append((dates[i], s, vpi, mri, rri, cri, zpi, atip, acs))
    return pd.DataFrame(rows, columns=["date", "symbol", "vpi", "mri", "rri", "cri", "zpi", "atip", "acs"])


def _nifty_ret20(db_path, index) -> pd.Series:
    c = sqlite3.connect(str(db_path))
    b = pd.read_sql("SELECT date,close FROM prices_daily WHERE symbol='NIFTY50' ORDER BY date", c).set_index("date")["close"]
    c.close()
    return (b / b.shift(20) - 1).reindex(index)


def stored_scores(db_path) -> pd.DataFrame:
    c = sqlite3.connect(str(db_path))
    df = pd.read_sql("SELECT symbol,date,vpi,mri,rri,cri,zpi,atip_score AS atip,acs,`signal` AS `signal` FROM ai_scores", c)
    c.close()
    return df


# ── forward returns ─────────────────────────────────────────────────────────────────────────────
def forward_returns(p: dict, horizons=HORIZONS) -> dict:
    """{h: DataFrame[date x symbol]}: open(t+1) -> close(t+h); NaN where the window holds a corporate-action jump."""
    o, c = p["open"], p["close"]
    jump = (c.pct_change().abs() > JUMP_LIMIT).astype(float)
    out = {}
    for h in horizons:
        r = c.shift(-h) / o.shift(-1) - 1
        bad = jump.rolling(h + 1, min_periods=1).max().shift(-h).fillna(0) > 0     # any jump in t..t+h
        out[h] = r.where(~bad)
    mdd = {}
    h = max(horizons)
    lows = p["low"]
    worst = pd.concat([lows.shift(-k) for k in range(1, h + 1)]).groupby(level=0).min()
    mdd = worst / o.shift(-1) - 1
    out["mdd20"] = mdd
    return out


# ── statistics ──────────────────────────────────────────────────────────────────────────────────
def _rank_ic(a: pd.Series, b: pd.Series) -> float:
    m = a.notna() & b.notna()
    if m.sum() < 30:
        return np.nan
    return float(a[m].rank().corr(b[m].rank()))


def daily_ic(scores: pd.DataFrame, fwd: pd.DataFrame, col: str) -> pd.Series:
    w = scores.pivot(index="date", columns="symbol", values=col)
    out = {}
    for d in w.index.intersection(fwd.index):
        out[d] = _rank_ic(w.loc[d], fwd.loc[d])
    return pd.Series(out).dropna()


def ic_summary(ic: pd.Series, h: int) -> dict:
    """Mean, std and t-stat of the daily ICs. Daily ICs at horizon h overlap (a session's window shares h-1
    days with the next), so the t-stat treats n/h of them as independent: t = mean / (std / sqrt(n / h)).
    The plain t (t_overlap) would overstate significance by about sqrt(h)."""
    n = len(ic)
    if n < 3:
        return {"n": n, "mean": None, "std": None, "t": None, "t_overlap": None}
    m, sd = float(ic.mean()), float(ic.std(ddof=1))
    t = lambda k: round(m / (sd / np.sqrt(k)), 2) if sd > 0 else None
    return {"n": n, "n_indep": round(n / max(h, 1), 1), "mean": round(m, 4), "std": round(sd, 4),
            "t": t(n / max(h, 1)), "t_overlap": t(n)}


def bucket_spread(scores: pd.DataFrame, fwd: pd.DataFrame, col: str, q: int = 10) -> dict:
    """Top bucket minus bottom bucket mean forward return (%), per session then averaged. Bucket 1 = lowest score."""
    w = scores.pivot(index="date", columns="symbol", values=col)
    spreads, tops, bots = [], [], []
    mid = {k: [] for k in range(q)}
    for d in w.index.intersection(fwd.index):
        s, r = w.loc[d], fwd.loc[d]
        m = s.notna() & r.notna()
        if m.sum() < q * 5:
            continue
        b = pd.qcut(s[m].rank(method="first"), q, labels=False)
        g = r[m].groupby(b).mean()
        if len(g) == q:
            spreads.append(g.iloc[-1] - g.iloc[0]); tops.append(g.iloc[-1]); bots.append(g.iloc[0])
            for k in range(q):
                mid[k].append(g.iloc[k])
    if not spreads:
        return {"spread_pct": None}
    sp = pd.Series(spreads)
    return {"spread_pct": round(float(sp.mean() * 100), 3), "top_pct": round(float(np.mean(tops) * 100), 3),
            "bottom_pct": round(float(np.mean(bots) * 100), 3), "sessions": len(sp),
            "monotonic": round(float(pd.Series([np.mean(mid[k]) for k in range(q)]).rank().corr(pd.Series(range(q)))), 2)}


def hit_rates(scores: pd.DataFrame, fwd: pd.DataFrame, col: str, top: float = 0.1, cost_pct: float = 0.25) -> dict:
    """BUY = top decile of the score that session, SELL = bottom decile (for CRI the sides swap: low risk is the BUY).
    Hit rate = share of BUYs with a positive forward return net of round-trip cost; SELLs hit when the return
    is negative (gross). Average win / loss in %."""
    w = scores.pivot(index="date", columns="symbol", values=col)
    buy, sell = [], []
    for d in w.index.intersection(fwd.index):
        s, r = w.loc[d], fwd.loc[d]
        m = s.notna() & r.notna()
        if m.sum() < 50:
            continue
        s, r = s[m], r[m]
        k = max(int(len(s) * top), 1)
        hi, lo = r[s.nlargest(k).index], r[s.nsmallest(k).index]
        if EXPECTED_SIGN[col] < 0:
            hi, lo = lo, hi
        buy += list(hi.values); sell += list(lo.values)
    def stat(x, sign, cost):
        x = np.array(x) * 100 * sign - cost
        if len(x) == 0:
            return {"n": 0}
        win, loss = x[x > 0], x[x <= 0]
        return {"n": int(len(x)), "hit_pct": round(float((x > 0).mean() * 100), 1),
                "avg_win_pct": round(float(win.mean()), 2) if len(win) else None,
                "avg_loss_pct": round(float(loss.mean()), 2) if len(loss) else None,
                "expectancy_pct": round(float(x.mean()), 3)}
    return {"BUY": stat(buy, 1, cost_pct), "SELL": stat(sell, -1, 0.0)}


def stability(scores: pd.DataFrame, col: str, top: float = 0.1) -> dict:
    """Day-to-day rank autocorrelation, and the share of the top-decile names that change next session."""
    w = scores.pivot(index="date", columns="symbol", values=col)
    ac, turn = [], []
    prev_top = None
    prev = None
    for d in w.index:
        s = w.loc[d].dropna()
        if prev is not None:
            ac.append(_rank_ic(prev, w.loc[d]))
        k = max(int(len(s) * top), 1)
        cur = set((s.nsmallest(k) if EXPECTED_SIGN[col] < 0 else s.nlargest(k)).index)
        if prev_top is not None:
            turn.append(1 - len(cur & prev_top) / max(len(cur), 1))
        prev, prev_top = w.loc[d], cur
    return {"rank_autocorr": round(float(np.nanmean(ac)), 3) if ac else None,
            "top_decile_turnover_pct": round(float(np.mean(turn) * 100), 1) if turn else None}


def mean_rank_corr(scores: pd.DataFrame, cols=SCORES) -> pd.DataFrame:
    """Average over sessions of the cross-sectional Spearman matrix (raw direction: CRI is NOT flipped)."""
    mats = []
    for _, g in scores.groupby("date"):
        g = g[list(cols)].dropna(how="all")
        if len(g) >= 30:
            mats.append(g.rank().corr())
    return sum(mats) / len(mats) if mats else pd.DataFrame()


def split_dates(dates, frac=OOS_FRACTION):
    dates = sorted(dates)
    k = int(len(dates) * (1 - frac))
    return dates[:k], dates[k:]


def verdict(oos: dict, sign: int) -> str:
    """WORKING / WEAK / INVERTED / BROKEN from the out-of-sample signed IC (20-day, falling back to 10 / 5):
    WORKING  signed IC > 0 with t >= 2 at one or more horizons and a positive decile spread in the expected direction
    INVERTED signed IC < 0 with |t| >= 2 (the score ranks the wrong way round)
    WEAK     0 < signed IC with 1 <= t < 2
    BROKEN   no measurable relationship (|t| < 1 everywhere) or no data."""
    best = None
    for h in (20, 10, 5, 1):
        s = oos.get(h) or {}
        if s.get("mean") is None or s.get("t") is None:
            continue
        sic, st = s["mean"] * sign, s["t"] * sign
        if best is None or abs(st) > abs(best[1]):
            best = (sic, st, h)
    if best is None:
        return "BROKEN"
    sic, st, h = best
    if st >= WORKING_T:
        return "WORKING"
    if st <= -WORKING_T:
        return "INVERTED"
    if st >= WEAK_T:
        return "WEAK"
    return "BROKEN"


# ── report ──────────────────────────────────────────────────────────────────────────────────────
def evaluate(scores: pd.DataFrame, fwd: dict, label: str, cols=SCORES) -> dict:
    dates = sorted(scores["date"].unique())
    is_d, oos_d = split_dates(dates)
    out = {"label": label, "sessions": len(dates), "in_sample": [is_d[0], is_d[-1]] if is_d else None,
           "out_of_sample": [oos_d[0], oos_d[-1]] if oos_d else None, "scores": {}}
    for col in cols:
        if col not in scores or scores[col].notna().sum() == 0:
            out["scores"][col] = {"verdict": "BROKEN", "note": "no data"}
            continue
        sc = {"ic": {"is": {}, "oos": {}}, "spread": {}, "hit": {}}
        for h in HORIZONS:
            ic = daily_ic(scores, fwd[h], col)
            sc["ic"]["is"][h] = ic_summary(ic[ic.index.isin(is_d)], h)
            sc["ic"]["oos"][h] = ic_summary(ic[ic.index.isin(oos_d)], h)
        o = scores[scores["date"].isin(oos_d)]
        for h in HORIZONS:
            sc["spread"][h] = {"decile": bucket_spread(o, fwd[h], col, 10), "quintile": bucket_spread(o, fwd[h], col, 5)}
        sc["hit"] = {h: hit_rates(o, fwd[h], col) for h in (5, 20)}
        sc["stability"] = stability(o, col)
        if col == "cri":
            ic = daily_ic(o, fwd["mdd20"], col)
            sc["ic_vs_20d_worst_drawdown_oos"] = ic_summary(ic, 20)       # +ve = higher CRI -> shallower worst case (wrong)
        sc["verdict"] = verdict(sc["ic"]["oos"], EXPECTED_SIGN[col])
        out["scores"][col] = sc
    out["corr_oos"] = mean_rank_corr(scores[scores["date"].isin(oos_d)], [c for c in cols if c in scores]).round(2).to_dict()
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="atip_data/atip.db")
    ap.add_argument("--out", default="docs/reports/2026-11-03")
    ap.add_argument("--tag", default="before")
    a = ap.parse_args(argv)
    p = load_panel(a.db)
    fwd = forward_returns(p)
    pit = score_panel(a.db, p)
    res = {"pit": evaluate(pit, fwd, "point-in-time recompute, %d symbols" % p["close"].shape[1])}
    st = stored_scores(a.db)
    stc = [c for c in SCORES]
    res["stored"] = evaluate(st, fwd, "stored ai_scores (as deployed)", stc)
    Path(a.out).mkdir(parents=True, exist_ok=True)
    f = Path(a.out) / f"validation_{a.tag}.json"
    f.write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    pit.to_parquet(Path(a.out) / f"pit_scores_{a.tag}.parquet") if False else None
    print(f"wrote {f}")
    for k in ("pit", "stored"):
        print(k, res[k]["label"], res[k]["sessions"], "sessions; OOS", res[k]["out_of_sample"])
        for c, v in res[k]["scores"].items():
            o = v.get("ic", {}).get("oos", {})
            print(f"  {c:5} {v['verdict']:9}", {h: (o[h]["mean"], o[h]["t"]) for h in o})


if __name__ == "__main__":
    main()


# ── markdown ────────────────────────────────────────────────────────────────────────────────────
def _f(x, nd=4, sign=True):
    if x is None or (isinstance(x, float) and x != x):
        return "—"
    return f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"


def markdown(before: dict, after: dict) -> str:
    """The validation report: IC (in / out of sample), decile and quintile spreads, hit rates, turnover,
    redundancy, verdicts -- before and after the SV-02..04 fixes."""
    L = []
    for view in ("pit", "stored"):
        b, a = before[view], after[view]
        L += [f"\n## {'Point-in-time recompute' if view == 'pit' else 'Stored scores (as deployed)'}",
              f"{a['label']}; {a['sessions']} sessions; in-sample {a['in_sample'][0]} → {a['in_sample'][1]}, "
              f"out-of-sample {a['out_of_sample'][0]} → {a['out_of_sample'][1]}.\n",
              "### Rank IC (Spearman), out of sample — mean (t, effective-n)\n",
              "| Score | Expected sign | Verdict before | Verdict after | " + " | ".join(
                  f"{h}d before | {h}d after" for h in HORIZONS) + " |",
              "|---|---|---|---|" + "---|---|" * len(HORIZONS)]
        for c in SCORES:
            sb, sa = b["scores"].get(c, {}), a["scores"].get(c, {})
            cells = []
            for h in HORIZONS:
                for s in (sb, sa):
                    o = (s.get("ic") or {}).get("oos", {}).get(str(h)) or (s.get("ic") or {}).get("oos", {}).get(h) or {}
                    cells.append(f"{_f(o.get('mean'))} ({_f(o.get('t'), 2)})")
            L.append(f"| {c.upper()}{'↓' if c == 'cri' else ''} | {'−' if EXPECTED_SIGN[c] < 0 else '+'} | "
                     f"{sb.get('verdict', '—')} | {sa.get('verdict', '—')} | " + " | ".join(cells) + " |")
        L += ["", "### Rank IC, in sample (after) — mean (t)\n", "| Score | " + " | ".join(f"{h}d" for h in HORIZONS) + " |",
              "|---|" + "---|" * len(HORIZONS)]
        for c in SCORES:
            s = a["scores"].get(c, {})
            ii = (s.get("ic") or {}).get("is", {})
            L.append(f"| {c.upper()} | " + " | ".join(
                f"{_f((ii.get(str(h)) or ii.get(h) or {}).get('mean'))} ({_f((ii.get(str(h)) or ii.get(h) or {}).get('t'), 2)})"
                for h in HORIZONS) + " |")
        L += ["", "### Decile / quintile spread, out of sample (top minus bottom bucket, % forward return; after)\n",
              "| Score | " + " | ".join(f"{h}d decile | {h}d quintile" for h in HORIZONS) + " | 20d monotonic |",
              "|---|" + "---|---|" * len(HORIZONS) + "---|"]
        for c in SCORES:
            sp = a["scores"].get(c, {}).get("spread", {})
            g = lambda h, k: (sp.get(str(h)) or sp.get(h) or {}).get(k, {})
            L.append(f"| {c.upper()} | " + " | ".join(
                f"{_f(g(h, 'decile').get('spread_pct'), 3)} | {_f(g(h, 'quintile').get('spread_pct'), 3)}" for h in HORIZONS) +
                f" | {_f(g(20, 'decile').get('monotonic'), 2)} |")
        L += ["", "### BUY / SELL hit rates, out of sample (after). BUY = top decile (bottom for CRI↓), net of 0.25 % round trip\n",
              "| Score | 5d BUY n | hit % | avg win % | avg loss % | expectancy % | 5d SELL hit % | 20d BUY hit % | 20d expectancy % | 20d SELL hit % |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for c in SCORES:
            ht = a["scores"].get(c, {}).get("hit", {})
            h5, h20 = ht.get("5") or ht.get(5) or {}, ht.get("20") or ht.get(20) or {}
            B5, S5, B20, S20 = h5.get("BUY", {}), h5.get("SELL", {}), h20.get("BUY", {}), h20.get("SELL", {})
            L.append(f"| {c.upper()} | {B5.get('n', '—')} | {B5.get('hit_pct', '—')} | {B5.get('avg_win_pct', '—')} | "
                     f"{B5.get('avg_loss_pct', '—')} | {B5.get('expectancy_pct', '—')} | {S5.get('hit_pct', '—')} | "
                     f"{B20.get('hit_pct', '—')} | {B20.get('expectancy_pct', '—')} | {S20.get('hit_pct', '—')} |")
        L += ["", "### Stability (out of sample, after)\n", "| Score | Day-to-day rank autocorrelation | Top-decile turnover / day |",
              "|---|---|---|"]
        for c in SCORES:
            st = a["scores"].get(c, {}).get("stability", {})
            L.append(f"| {c.upper()} | {st.get('rank_autocorr', '—')} | {st.get('top_decile_turnover_pct', '—')} % |")
        cm = a.get("corr_oos") or {}
        if cm:
            cols = list(cm.keys())
            L += ["", "### Correlation between the scores (mean daily Spearman, out of sample, after). |ρ| > 0.8 = redundant\n",
                  "| | " + " | ".join(c.upper() for c in cols) + " |", "|---|" + "---|" * len(cols)]
            flags = []
            for r in cols:
                row = []
                for c in cols:
                    v = cm[c].get(r)
                    row.append("—" if v is None else (f"**{v:.2f}**" if abs(v) > 0.8 and r != c else f"{v:.2f}"))
                    if v is not None and r < c and abs(v) > 0.8:
                        flags.append(f"{r.upper()}–{c.upper()} {v:.2f}")
                L.append(f"| {r.upper()} | " + " | ".join(row) + " |")
            L.append("\nRedundant pairs (|ρ| > 0.8): " + (", ".join(flags) if flags else "none") + ".")
    return "\n".join(L)
