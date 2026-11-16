"""Formula validation for one scored day, on a DB snapshot (read-only). Independent of the app's code: indicators are
recomputed here with plain pandas from prices_daily; composite scores from their stored components and weights.

    python formula_check.py SNAPSHOT.db 2026-11-16 OUT.json
"""
import json
import sqlite3
import sys

import numpy as np
import pandas as pd

DB, DAY, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
SAMPLE = ["RELIANCE", "HDFCBANK", "INFY", "DLF", "GRASIM", "KALYANKJIL", "CDSL", "IGIL", "ATHERENERG", "SAILIFE"]
TOL = 0.5                                           # % mismatch flagged
c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row
out = {"day": DAY}

# ---------------------------------------------------------------- 1. coverage of the stored values
universe = [r[0] for r in c.execute("SELECT symbol FROM ai_scores WHERE date=?", (DAY,))]
out["universe"] = len(universe)
caps = {r[0]: (r[1] or "unknown") for r in c.execute("SELECT symbol, cap_bucket FROM technical_snapshot WHERE date=?", (DAY,))}
def coverage(table, cols, key="date"):
    rows = {r["symbol"]: dict(r) for r in c.execute(f"SELECT * FROM {table} WHERE {key}=?", (DAY,))}
    res = {}
    for col in cols:
        miss = [s for s in universe if rows.get(s, {}).get(col) is None]
        by_cap = {}
        for s in miss:
            by_cap[caps.get(s, "unknown")] = by_cap.get(caps.get(s, "unknown"), 0) + 1
        res[col] = {"have": len(universe) - len(miss), "pct": round((len(universe) - len(miss)) / len(universe) * 100, 1),
                    "missing_by_cap": by_cap, "missing_sample": miss[:6]}
    return res
score_cols = ["vpi", "spi", "rri", "mri", "cri", "msi", "zpi", "acs", "tech_score", "fund_score", "inst_score",
              "news_score", "atip_score", "signal", "confidence", "tod_score", "mh_score", "beta_1y"]
tech_cols = ["rsi_14", "stoch_k", "stoch_d", "williams_r", "cci_20", "macd_line", "macd_signal", "macd_hist", "macd_hist_pct",
             "adx_14", "ema_9", "ema_21", "ema_50", "sma_200", "atr_14", "atr_pct", "bb_upper", "bb_lower", "bb_mid",
             "bb_width", "obv", "volume_sma20", "volume_ratio", "rel_volume", "pivot", "r1", "r2", "s1", "s2", "fib_236",
             "fib_382", "fib_500", "fib_618", "golden_cross", "death_cross", "above_200dma", "gap_pct", "tech_score"]
ext_cols = ["vwap_20d", "supertrend", "ichimoku_tenkan", "keltner_upper", "donchian_upper", "mfi_14", "cmf_20", "roc_10",
            "aroon_up", "psar", "beta_60", "weekly_rsi", "rs_sector_pctile", "sr_support", "sr_resistance"]
pred_cols = ["entry_price", "stop_loss", "target_1", "target_2", "risk_reward", "position_size_pct"]
out["coverage"] = {"ai_scores": coverage("ai_scores", score_cols), "technical_indicators": coverage("technical_indicators", tech_cols),
                   "technical_ext": coverage("technical_ext", ext_cols),
                   "predictions": coverage("predictions", pred_cols, key="pred_date")}
bars = {r[0]: r[1] for r in c.execute("SELECT symbol, COUNT(*) FROM prices_daily WHERE date<=? GROUP BY symbol", (DAY,))}
out["short_history"] = {s: bars.get(s, 0) for s in universe if bars.get(s, 0) < 200}

# ---------------------------------------------------------------- 2. composite scores from their components
weights = {}
for r in c.execute("SELECT index_name, component, value, weight, symbol FROM score_components WHERE date=?", (DAY,)):
    weights.setdefault((r["symbol"], r["index_name"]), []).append((r["component"], r["value"], r["weight"]))
col_of = {"VPI": "vpi", "ZPI": "zpi", "MRI": "mri", "RRI": "rri", "CRI": "cri", "MSI": "msi", "ACS": "acs", "SPI": "spi",
          "TOD": "tod_score", "ATIP": "atip_score", "INS": "inst_score", "FS": "fund_score", "TS": "tech_score"}
stored = {r["symbol"]: dict(r) for r in c.execute("SELECT * FROM ai_scores WHERE date=?", (DAY,))}
comp = {}
for (sym, idx), parts in weights.items():
    col = col_of.get(idx)
    if not col or sym not in stored or stored[sym].get(col) is None:
        continue
    ws = sum(p[2] for p in parts if p[1] is not None)
    if not ws:
        continue
    val = sum(p[1] * p[2] for p in parts if p[1] is not None) / ws
    st = float(stored[sym][col])
    d = comp.setdefault(idx, {"n": 0, "match": 0, "max_abs_diff": 0.0, "worst": None})
    d["n"] += 1
    diff = abs(val - st)
    if diff <= max(0.05, abs(st) * TOL / 100):
        d["match"] += 1
    if diff > d["max_abs_diff"]:
        d["max_abs_diff"], d["worst"] = round(diff, 4), {"symbol": sym, "stored": st, "recomputed": round(val, 4)}
out["composites"] = comp

# ---------------------------------------------------------------- 3. indicators recomputed from raw prices
def series(sym):
    df = pd.read_sql("SELECT date,open,high,low,close,adj_close,volume FROM prices_daily WHERE symbol=? AND date<=? "
                     "ORDER BY date DESC LIMIT 520", c, params=(sym, DAY)).sort_values("date").tail(260).reset_index(drop=True)
    # data/technical.py run_technical_pipeline: the classic indicators keep a 260-bar window (OBV's level depends on it)
    for k in ("open", "high", "low", "close", "adj_close", "volume"):
        df[k] = pd.to_numeric(df[k], errors="coerce")
    df["c"] = df["adj_close"].fillna(df["close"])
    return df

def wilder(x, n):                                   # Wilder's smoothing seeded with the first n-value mean
    x = x.astype(float).values
    out_ = np.full(len(x), np.nan)
    first = np.where(~np.isnan(x))[0]
    if len(first) == 0 or len(x) < first[0] + n:
        return pd.Series(out_)
    s = first[0]
    out_[s + n - 1] = np.nanmean(x[s:s + n])
    for i in range(s + n, len(x)):
        out_[i] = (out_[i - 1] * (n - 1) + x[i]) / n
    return pd.Series(out_)

def mine(df):
    C, H, L, O, V = df["c"], df["high"], df["low"], df["open"], df["volume"].fillna(0)
    r = {}
    d = C.diff()
    up, dn = d.clip(lower=0), (-d).clip(lower=0)
    au, ad = up.ewm(alpha=1 / 14, adjust=False).mean(), dn.ewm(alpha=1 / 14, adjust=False).mean()
    r["rsi_14"] = float(100 - 100 / (1 + au.iloc[-1] / ad.iloc[-1])) if ad.iloc[-1] else 100.0
    ema = lambda s, n: s.ewm(span=n, adjust=False).mean()
    r["ema_9"], r["ema_21"] = float(ema(C, 9).iloc[-1]), float(ema(C, 21).iloc[-1])
    if len(df) >= 50:
        r["ema_50"] = float(ema(C, 50).iloc[-1])
    if len(df) >= 200:
        r["sma_200"] = float(C.rolling(200).mean().iloc[-1])
    m = ema(C, 12) - ema(C, 26)
    sig = ema(m, 9)
    r["macd_line"], r["macd_signal"], r["macd_hist"] = float(m.iloc[-1]), float(sig.iloc[-1]), float((m - sig).iloc[-1])
    pc = C.shift(1)
    tr = pd.concat([H - L, (H - pc).abs(), (L - pc).abs()], axis=1).max(axis=1)
    tr.iloc[0] = np.nan
    atr = wilder(tr, 14)
    r["atr_14"] = float(atr.iloc[-1])
    r["atr_pct"] = r["atr_14"] / float(C.iloc[-1]) * 100
    mid = C.rolling(20).mean()
    sd = C.rolling(20).std(ddof=0)
    r["bb_mid"], r["bb_upper"], r["bb_lower"] = float(mid.iloc[-1]), float((mid + 2 * sd).iloc[-1]), float((mid - 2 * sd).iloc[-1])
    r["bb_width"] = (r["bb_upper"] - r["bb_lower"]) / r["bb_mid"] * 100
    hh, ll = H.rolling(14).max(), L.rolling(14).min()
    k = 100 * (C - ll) / (hh - ll)
    r["stoch_k"], r["stoch_d"] = float(k.iloc[-1]), float(k.rolling(3).mean().iloc[-1])
    r["williams_r"] = float(-100 * (hh.iloc[-1] - C.iloc[-1]) / (hh.iloc[-1] - ll.iloc[-1]))
    tp = (H + L + C) / 3
    mad = tp.rolling(20).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    r["cci_20"] = float(((tp - tp.rolling(20).mean()) / (0.015 * mad)).iloc[-1])
    upm, dnm = H.diff(), -L.diff()
    pdm = pd.Series(np.where((upm > dnm) & (upm > 0), upm, 0.0))
    ndm = pd.Series(np.where((dnm > upm) & (dnm > 0), dnm, 0.0))
    pdm.iloc[0] = ndm.iloc[0] = np.nan
    atr_s = wilder(tr, 14)
    pdi, ndi = 100 * wilder(pdm, 14) / atr_s, 100 * wilder(ndm, 14) / atr_s
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi)
    r["adx_14"] = float(wilder(dx, 14).iloc[-1])
    obv = (np.sign(C.diff()).fillna(0) * V).cumsum()
    r["obv"] = float(obv.iloc[-1])
    vs = V.rolling(20).mean()
    r["volume_sma20"] = float(vs.iloc[-1])
    r["volume_ratio"] = float(V.iloc[-1] / vs.iloc[-1]) if vs.iloc[-1] else None
    ph, pl, pcl = float(H.iloc[-2]), float(L.iloc[-2]), float(C.iloc[-2])
    pv = (ph + pl + pcl) / 3
    r.update(pivot=pv, r1=2 * pv - pl, r2=pv + (ph - pl), s1=2 * pv - ph, s2=pv - (ph - pl))
    hi, lo = float(C.tail(252).max()), float(C.tail(252).min())
    r.update(fib_236=hi - .236 * (hi - lo), fib_382=hi - .382 * (hi - lo), fib_500=hi - .5 * (hi - lo), fib_618=hi - .618 * (hi - lo))
    r["gap_pct"] = (float(O.iloc[-1]) - pcl) / pcl * 100
    return r

checks, flagged = [], []
for sym in SAMPLE:
    st = c.execute("SELECT * FROM technical_indicators WHERE symbol=? AND date=?", (sym, DAY)).fetchone()
    df = series(sym)
    if st is None or len(df) < 30:
        checks.append({"symbol": sym, "error": "no stored row or too few bars", "bars": len(df)})
        continue
    st = dict(st)
    me = mine(df)
    row = {"symbol": sym, "bars": len(df), "cap": caps.get(sym), "fields": {}}
    for k, v in me.items():
        s = st.get(k)
        if v is None or s is None:
            row["fields"][k] = {"stored": s, "mine": None if v is None else round(v, 4), "diff_pct": None}
            continue
        base = abs(float(s)) if abs(float(s)) > 1e-9 else 1.0
        dp = abs(float(v) - float(s)) / base * 100
        # oscillators near 0 (MACD, CCI, Williams) compared in points against their scale, not as a % of a tiny value
        if k in ("macd_line", "macd_signal", "macd_hist"):
            dp = abs(float(v) - float(s)) / float(df["c"].iloc[-1]) * 100 * 100     # basis points of price, as %
        if k in ("cci_20", "williams_r", "gap_pct") and abs(float(s)) < 10:
            dp = abs(float(v) - float(s))                                         # points
        row["fields"][k] = {"stored": float(s), "mine": round(float(v), 4), "diff_pct": round(dp, 4)}
        if dp > TOL:
            flagged.append({"symbol": sym, "field": k, "stored": float(s), "mine": round(float(v), 4), "diff_pct": round(dp, 3)})
    checks.append(row)
out["indicator_sample"] = checks
out["indicator_flags"] = flagged
out["indicator_fields_checked"] = sum(len(r.get("fields", {})) for r in checks)

# ---------------------------------------------------------------- 4. Dhan vs NSE for the same symbols and session
def dhan_vs_nse(day):
    rows = []
    q = ("SELECT symbol, MIN(ts) a, MAX(ts) b, MAX(high) h, MIN(low) l, COUNT(*) n FROM intraday_bars "
         "WHERE interval_min=15 AND source='dhan' AND substr(ts,1,10)=? GROUP BY symbol")
    for r in c.execute(q, (day,)):
        o = c.execute("SELECT open FROM intraday_bars WHERE interval_min=15 AND source='dhan' AND symbol=? AND ts=?",
                      (r["symbol"], r["a"])).fetchone()
        cl = c.execute("SELECT close FROM intraday_bars WHERE interval_min=15 AND source='dhan' AND symbol=? AND ts=?",
                       (r["symbol"], r["b"])).fetchone()
        n = c.execute("SELECT open, high, low, close FROM prices_daily WHERE symbol=? AND date=? AND source='bhavcopy'",
                      (r["symbol"], day)).fetchone()
        if not n:
            continue
        rows.append({"symbol": r["symbol"], "bars": r["n"], "last": r["b"], "d_open": o[0], "d_high": r["h"], "d_low": r["l"],
                     "d_close": cl[0], "n_open": n[0], "n_high": n[1], "n_low": n[2], "n_close": n[3]})
    full = [x for x in rows if x["bars"] >= 25]
    def pct(a, b):
        return abs(float(a) - float(b)) / float(b) * 100 if a and b else None
    res = {"symbols": len(rows), "full_day_symbols": len(full)}
    for k in ("open", "high", "low", "close"):
        diffs = [pct(x[f"d_{k}"], x[f"n_{k}"]) for x in (full if k != "open" else rows)]
        diffs = [d for d in diffs if d is not None]
        if k == "close" and not full:
            continue
        res[k] = {"n": len(diffs), "over_0_5pct": sum(1 for d in diffs if d > TOL), "max_pct": round(max(diffs), 3) if diffs else None,
                  "median_pct": round(float(np.median(diffs)), 4) if diffs else None}
        res[k]["worst"] = sorted(((round(pct(x[f"d_{k}"], x[f"n_{k}"]) or 0, 3), x["symbol"]) for x in (full if k != "open" else rows)),
                                 reverse=True)[:5]
    # partial day: the Dhan range so far must lie inside NSE's day range
    res["partial_outside_range"] = [x["symbol"] for x in rows if x["bars"] < 25 and
                                    (x["d_high"] > x["n_high"] * 1.0005 or x["d_low"] < x["n_low"] * 0.9995)][:10]
    return res
prev = c.execute("SELECT MAX(date) FROM prices_daily WHERE date<? AND source='bhavcopy'", (DAY,)).fetchone()[0]
out["dhan_vs_nse"] = {DAY: dhan_vs_nse(DAY), prev: dhan_vs_nse(prev)}

# ---------------------------------------------------------------- 5. market-level formulas
out["market_health"] = [dict(r) for r in c.execute("SELECT date, mh_score, regime, nifty_close, vix_level, breadth, "
                        "pct_advancing, mh_coverage, substr(mh_inputs,1,300) mh_inputs, created_at FROM market_health "
                        "WHERE date>=? ORDER BY date DESC LIMIT 3", (prev,))]
out["breadth"] = [dict(r) for r in c.execute("SELECT * FROM market_breadth ORDER BY date DESC LIMIT 4")]
out["signals"] = {r[0]: r[1] for r in c.execute("SELECT `signal`, COUNT(*) FROM ai_scores WHERE date=? GROUP BY 1", (DAY,))}
out["kite_scan_runs"] = [dict(r) for r in c.execute("SELECT id, scanner, as_of, scored_date, run_at, universe, scanned, results, "
                         "skipped, source FROM algo_scan_runs ORDER BY run_at DESC LIMIT 4")]
json.dump(out, open(OUT, "w"), indent=1, default=str)
print("universe", out["universe"], "flags", len(flagged), "fields", out["indicator_fields_checked"])
for idx, d in comp.items():
    print(f"  {idx:5} n={d['n']} match={d['match']} max_diff={d['max_abs_diff']} worst={d['worst']}")
for f in flagged[:40]:
    print("  FLAG", f)
