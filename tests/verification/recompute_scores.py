"""
Independent recomputation of ATIP's seven scores (VPI, MRI, RRI, ZPI, CRI↓, ACS, ATIP) for given symbols on one day,
from the raw tables, WITHOUT importing scores.engine or data.technical. QA pass of 4 Nov 2026 (V2).

Three layers, each compared with what production stored:

  1. indicators  RSI-14 (Wilder), ATR-14 % (Wilder), ADX-14 (Wilder), EMA-9/21 (span, adjust=False), SMA-200,
                 20-day volume ratio and the MACD(12,26,9) histogram as % of price, recomputed from prices_daily's
                 last 260 bars  ->  vs technical_indicators
  2. components  every component of every score, re-derived from the spec (docs/reports/2026-11-03-fullreview/
                 SCORE_AUDIT_REPORT.md section 1) using the stored indicators, prices_daily, news_symbol_score,
                 market_health, fii_dii_market, bulk_deals, accuracy_tracker and the Nifty 500 industry list
                 ->  vs score_components (the components production captured while scoring)
  3. scores      sum(w * c) / sum(w) over the components present, weights from weight_config (active = 1), 2 dp
                 ->  vs ai_scores.   PASS when |difference| <= TOL (0.01).

Inputs this script takes as given, and says so: MSI (one market-wide number, checked equal across symbols),
the technical score TS (technical_indicators.tech_score), and the per-symbol news score (news_symbol_score.score,
50 when there is none -- the engine's documented neutral). SPI / FS / FG / Debt / Promoter are absent while the
fundamentals switch is off (atip_data/config.json), as in production.

    python tests/verification/recompute_scores.py --db <copy of atip.db> --date 2026-11-04 \
        --symbols RELIANCE,HDFCBANK,LAURUSLABS,REDINGTON,VEDPOWER,ICICILIFE [--industry atip_data/raw/ind_nifty500list.csv]
        [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

TOL = 0.01
SCORES = ("vpi", "mri", "rri", "zpi", "cri", "acs", "atip")


# ── small helpers (written from the spec, not copied from the engine) ───────────────────────────
def scale(x, lo, hi, invert=False):
    """Fixed-range min-max to 0..100, clipped; a missing reading is neutral 50; 2 dp."""
    if x is None or (isinstance(x, float) and math.isnan(x)) or lo == hi:
        return 50.0
    v = min(100.0, max(0.0, (x - lo) / (hi - lo) * 100.0))
    return round(100.0 - v if invert else v, 2)


def combine(parts: dict, weights: dict) -> float:
    num = den = 0.0
    for k, w in weights.items():
        if parts.get(k) is not None:
            num += parts[k] * w
            den += w
    return round(num / den, 2) if den > 0 else 50.0


def wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


# ── layer 1: indicators from prices_daily ───────────────────────────────────────────────────────
def indicators_from_bars(bars: pd.DataFrame) -> dict:
    """bars oldest -> newest (the last 260). Returns the indicators the scores read."""
    if len(bars) < 14:
        return {}
    c, h, l, v = (pd.to_numeric(bars[k], errors="coerce") for k in ("close", "high", "low", "volume"))
    d = c.diff()
    up, dn = wilder(d.clip(lower=0), 14), wilder((-d).clip(lower=0), 14)
    rsi = 100 - 100 / (1 + up / dn)
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = wilder(tr, 14)
    hd, ld = h.diff(), -l.diff()
    pdm = hd.where((hd > ld) & (hd > 0), 0.0)
    mdm = ld.where((ld > hd) & (ld > 0), 0.0)
    pdi, mdi = 100 * wilder(pdm, 14) / atr, 100 * wilder(mdm, 14) / atr
    adx = wilder(100 * (pdi - mdi).abs() / (pdi + mdi), 14)
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    hist = macd - macd.ewm(span=9, adjust=False).mean()
    vs20 = v.rolling(20).mean()
    last = float(c.iloc[-1])
    out = {"rsi_14": float(rsi.iloc[-1]), "atr_pct": float(atr.iloc[-1] / last * 100), "adx_14": float(adx.iloc[-1]),
           "ema_9": float(c.ewm(span=9, adjust=False).mean().iloc[-1]),
           "ema_21": float(c.ewm(span=21, adjust=False).mean().iloc[-1]),
           "macd_hist_pct": float(hist.iloc[-1] / last * 100),
           "volume_ratio": float(v.iloc[-1] / vs20.iloc[-1]) if vs20.iloc[-1] else None,
           "sma_200": float(c.rolling(200).mean().iloc[-1]) if len(c) >= 200 else None}
    out["above_200dma"] = None if out["sma_200"] is None else int(last > out["sma_200"])
    return out


# ── layer 2: components ─────────────────────────────────────────────────────────────────────────
def rsi_room(r):             # VPI MR
    return 80 + (50 - r) if 30 <= r <= 50 else 70 if r < 30 else max(0, 60 - (r - 50) * 2)


def macd_reversal(hist_pct):  # MRI MACD: 100 - the histogram's 0..100 reading (full scale 1.667 % of price)
    if hist_pct is None:
        return None
    reading = round(50.0 + max(-50.0, min(50.0, float(hist_pct) / 1.667 * 50.0)), 2)
    return round(100.0 - reading, 2)


def known(x):
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def components(t: dict, ctx: dict) -> dict:
    """Every score's components for one symbol. t = stored technical_indicators row ({} when there is none)."""
    atr, adx, rsi, vr = t.get("atr_pct"), t.get("adx_14"), t.get("rsi_14"), t.get("volume_ratio")
    e9, e21 = t.get("ema_9"), t.get("ema_21")
    a200 = None if t.get("sma_200") is None else bool(t.get("above_200dma"))
    ns = ctx["ns"]
    vpi = {"V": min(atr * 12, 100) if atr else None, "TS": scale(adx, 0, 60) if adx else None, "RS": ctx["rs"],
           "LQ": ctx["lq"], "VOL": min(vr / 3 * 100, 100) if vr else None,
           "MR": rsi_room(rsi) if known(rsi) else None, "NS": ns}
    mri = {"MACD": macd_reversal(t.get("macd_hist_pct")),
           "RSI": (85 if 40 <= rsi <= 55 else 60 if rsi < 40 else 20 if rsi > 70 else 50) if known(rsi) else None,
           "ADX": scale(adx, 0, 60) if adx else None, "Volume": min(vr / 2 * 100, 100) if vr else None,
           "EMA": (75 if e9 > e21 else 30) if (e9 and e21) else None, "News": ns}
    rri = {"Recovery": ctx["recovery"], "RSIRecovery": min(max(0, (rsi - 30) * 2), 100) if known(rsi) else None,
           "Volume": min(vr / 2 * 100, 100) if vr else None,
           "Support": None if a200 is None else (80 if a200 else 30), "News": ns}
    weak = None
    if a200 is not None:
        weak = min((0 if a200 else 40) + (40 if t.get("death_cross") else 0), 100)
    mh, vix = ctx["mh"], ctx["vix"]
    mh_eff = mh if mh else 50                     # the engine reads a missing (or 0) market health as 50 here
    mw = (50 if mh_eff < 40 else 0) + (30 if vix is not None and vix > 18 else 0) + (20 if vix is not None and vix > 25 else 0)
    cri = {"Volatility": min(atr * 15, 100) if atr else None,
           "Distribution": min(vr * 30, 100) if (vr and vr > 1.5) else 20,
           "WeakTrend": weak, "NegativeNews": max(0, 100 - ns), "MarketWeakness": min(mw, 100)}
    zpi = {"Support": None if a200 is None else (80 if a200 else 30),
           "Resistance": min(100, adx * 2) if adx else None,
           "RSI": (90 if 30 <= rsi <= 50 else 70 if rsi < 30 else 60 if rsi <= 60 else 20) if known(rsi) else None,
           "ATR": round(100 - min(atr * 12, 100), 2) if atr else None,
           "Volume": (80 if vr < 0.7 else 65 if vr < 1.0 else 45 if vr < 1.5 else 20) if vr else None,
           "Trend": None if a200 is None else (85 if a200 else 15),
           "Institutional": ctx["accumulation"], "News": ns, "Sector": ctx["sector"]}
    return {"VPI": vpi, "MRI": mri, "RRI": rri, "CRI": cri, "ZPI": zpi}


# ── context from the raw tables ─────────────────────────────────────────────────────────────────
def load_ctx(c, sym, d, bench, sector_map, weights, msi, calendar):
    p = pd.read_sql("SELECT * FROM prices_daily WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 260", c, params=(sym, d))
    ctx = {"ns": 50.0, "rs": None, "lq": None, "recovery": None, "accumulation": None}
    r = c.execute("SELECT score FROM news_symbol_score WHERE symbol=? AND date=?", (sym, d)).fetchone()
    if r and r[0] is not None:
        ctx["ns"] = float(r[0])
    if not p.empty:
        w = p.iloc[:20]
        t = float((w["close"] * w["volume"]).mean())
        ctx["lq"] = None if t != t else round(min(t / 1e9 * 100, 100), 2)
        lo, cmp_ = float(p["close"].min()), float(p["close"].iloc[0])
        ctx["recovery"] = min((cmp_ - lo) / lo * 200, 100) if lo > 0 else None
        m = p[["date", "close"]].merge(bench, on="date", suffixes=("", "_b")).sort_values("date", ascending=False)
        if len(p) >= 21 and len(m) >= 21 and m["close"].iloc[20] and m["close_b"].iloc[20]:
            sr = (m["close"].iloc[0] / m["close"].iloc[20] - 1) * 100
            br = (m["close_b"].iloc[0] / m["close_b"].iloc[20] - 1) * 100
            ctx["rs"] = scale(sr - br, -15, 15)
        if "delivery_pct" in p:
            dp = pd.to_numeric(p["delivery_pct"], errors="coerce")
            s10, b60 = dp.iloc[:10], dp.iloc[:60]
            if s10.notna().sum() >= 8 and b60.notna().sum() >= 40 and b60.mean() > 0:
                ctx["accumulation"] = scale(s10.mean() / b60.mean(), 0.76, 1.24)
    ctx["sector"] = sector_map.get(sym, 50.0)
    mh = c.execute("SELECT mh_score, vix_level FROM market_health WHERE date=?", (d,)).fetchone()
    ctx["mh"], ctx["vix"] = (mh[0], mh[1]) if mh else (None, None)
    # ACS inputs
    i = calendar.index(d) if d in calendar else len([x for x in calendar if x < d])
    known_by = calendar[i - 5] if i >= 5 else None
    since = str(date.fromisoformat(d) - timedelta(days=90))
    acc = None
    if known_by:
        a = c.execute("SELECT AVG(correct_5d)*100, COUNT(correct_5d) FROM accuracy_tracker WHERE symbol=? AND pred_date>=? "
                      "AND pred_date<=? AND correct_5d IS NOT NULL", (sym, since, known_by)).fetchone()
        acc = round(float(a[0]), 2) if a and a[1] >= 5 else None
    ctx["accuracy"], ctx["msi"] = acc, msi
    return ctx


def sector_scores(c, d, symbols, industry_csv) -> dict:
    """The day's industry rank (average 1-day change of the scored symbols in each Nifty 500 industry), mapped
    90 / 60 / 35 by the top quarter / half / rest; no industry -> 50."""
    if not industry_csv or not Path(industry_csv).exists():
        return {}
    ind = pd.read_csv(industry_csv)
    ind = dict(zip(ind["Symbol"].astype(str).str.strip(), ind["Industry"].astype(str).str.strip()))
    q = ",".join("?" * len(symbols))
    px = pd.read_sql(f"SELECT symbol, date, close FROM prices_daily WHERE symbol IN ({q}) AND date<=? AND date>=date(?, '-20 days')",
                     c, params=(*symbols, d, d)).sort_values(["symbol", "date"])
    chg = {}
    for s, g in px.groupby("symbol"):
        if len(g) >= 2 and g["close"].iloc[-2]:
            chg[s] = (g["close"].iloc[-1] / g["close"].iloc[-2] - 1) * 100
    groups = {}
    for s, v in chg.items():
        if ind.get(s):
            groups.setdefault(ind[s], []).append(v)
    ranked = sorted(((k, sum(v) / len(v)) for k, v in groups.items()), key=lambda kv: kv[1], reverse=True)
    rank = {k: i + 1 for i, (k, _) in enumerate(ranked)}
    n = len(ranked)
    out = {}
    for s in symbols:
        r = rank.get(ind.get(s))
        out[s] = 50.0 if not r or not n else 90.0 if r / n <= 0.25 else 60.0 if r / n <= 0.5 else 35.0
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--industry", default="atip_data/raw/ind_nifty500list.csv")
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    d, syms = a.date, [s.strip() for s in a.symbols.split(",") if s.strip()]
    c = sqlite3.connect(a.db)
    c.row_factory = sqlite3.Row
    W = {}
    for r in c.execute("SELECT index_name, variable, weight FROM weight_config WHERE active=1"):
        W.setdefault(r["index_name"], {})[r["variable"]] = r["weight"]
    bench = pd.read_sql("SELECT date, close FROM prices_daily WHERE symbol='NIFTY50' AND date<=? ORDER BY date DESC LIMIT 30",
                        c, params=(d,))
    calendar = [r[0] for r in c.execute("SELECT DISTINCT date FROM prices_daily WHERE symbol='NIFTY50' AND date<=? "
                                        "ORDER BY date", (d,))]
    msis = {r[0] for r in c.execute("SELECT DISTINCT msi FROM ai_scores WHERE date=?", (d,))}
    msi = next(iter(msis)) if len(msis) == 1 else None
    scored = [r[0] for r in c.execute("SELECT symbol FROM ai_scores WHERE date=?", (d,))]
    sec = sector_scores(c, d, scored, a.industry)
    fii = c.execute("SELECT fii_5d_avg, dii_5d_avg FROM fii_dii_market WHERE date<=? ORDER BY date DESC LIMIT 1", (d,)).fetchone()
    bulk_since = str(date.fromisoformat(d) - timedelta(days=10))
    report = {"date": d, "tolerance": TOL, "msi_single_value": len(msis) == 1, "msi": msi, "symbols": {}}
    for sym in syms:
        stored = c.execute("SELECT * FROM ai_scores WHERE symbol=? AND date=?", (sym, d)).fetchone()
        if not stored:
            report["symbols"][sym] = {"error": "no ai_scores row"}
            continue
        stored = dict(stored)
        trow = c.execute("SELECT * FROM technical_indicators WHERE symbol=? AND date=?", (sym, d)).fetchone()
        t = {k: v for k, v in dict(trow).items() if v is not None} if trow else {}
        bars = pd.read_sql("SELECT date, open, high, low, close, volume FROM prices_daily WHERE symbol=? AND date<=? "
                           "ORDER BY date DESC LIMIT 260", c, params=(sym, d)).iloc[::-1].reset_index(drop=True)
        mine_ind = indicators_from_bars(bars)
        ind_cmp = {k: {"recomputed": None if mine_ind.get(k) is None else round(mine_ind[k], 4),
                       "stored": t.get(k), "abs_diff": None if (mine_ind.get(k) is None or t.get(k) is None)
                       else round(abs(mine_ind[k] - t[k]), 4)}
                   for k in ("rsi_14", "atr_pct", "adx_14", "ema_9", "ema_21", "sma_200", "volume_ratio", "macd_hist_pct",
                             "above_200dma")}
        ctx = load_ctx(c, sym, d, bench, sec, W, msi, calendar)
        comp = components(t, ctx)
        out = {}
        for name in ("VPI", "MRI", "RRI", "CRI", "ZPI"):
            out[name.lower()] = combine(comp[name], W[name])
        # INS (input to ATIP), from the market FII/DII 5-day averages and the stock's 10-day bulk deals
        ins_c = {}
        if fii and fii[0] is not None:
            ins_c["FII"] = scale(fii[0], -2000, 2000)
        if fii and fii[1] is not None:
            ins_c["DII"] = scale(fii[1], -500, 1500)
        b = c.execute("SELECT SUM(net_value_cr) FROM bulk_deals WHERE symbol=? AND date>=? AND date<=?", (sym, bulk_since, d)).fetchone()
        if b and b[0] is not None:
            ins_c["BulkDeals"] = scale(float(b[0]), -50, 50)
        ins = round(combine(ins_c, W["INS"]), 2) if ins_c else None
        ts = t.get("tech_score")
        present = {"vpi": out["vpi"], "spi": None, "rri": out["rri"], "mri": out["mri"], "cri": out["cri"], "msi": msi,
                   "zpi": out["zpi"], "tech_score": ts}
        agree = [v for k, v in present.items() if k in ("vpi", "mri", "rri", "zpi", "msi") and v]
        acs_c = {"HistoricalAccuracy": ctx["accuracy"],
                 "Agreement": sum(1 for v in agree if v > 60) / len(agree) * 100 if agree else None,
                 "MarketRegime": scale(ctx["mh"], 0, 100) if ctx["mh"] is not None else None,
                 "DataQuality": sum(1 for v in present.values() if v is not None) / len(present) * 100,
                 "Liquidity": ctx["lq"]}
        out["acs"] = combine(acs_c, W["ACS"])
        atip_c = {"VPI": out["vpi"], "RRI": out["rri"], "MRI": out["mri"], "MSI": msi, "ZPI": out["zpi"], "TS": ts, "INS": ins,
                  "SAFETY": round(100 - out["cri"], 2)}
        out["atip"] = combine(atip_c, W["ATIP"])
        comp.update({"ACS": acs_c, "ATIP": atip_c, "INS": ins_c})
        # compare components with what production captured
        sc = {}
        for r in c.execute("SELECT index_name, component, value FROM score_components WHERE symbol=? AND date=?", (sym, d)):
            sc.setdefault(r[0], {})[r[1]] = r[2]
        comp_cmp, worst_comp = {}, 0.0
        for idx in ("VPI", "MRI", "RRI", "CRI", "ZPI", "ACS", "ATIP", "INS"):
            mine = {k: v for k, v in comp[idx].items() if v is not None}
            theirs = sc.get(idx, {})
            rows = {}
            for k in sorted(set(mine) | set(theirs)):
                m, s = mine.get(k), theirs.get(k)
                diff = None if (m is None or s is None) else round(abs(m - s), 4)
                if diff is not None:
                    worst_comp = max(worst_comp, diff)
                rows[k] = {"recomputed": None if m is None else round(m, 4), "stored": s, "abs_diff": diff,
                           "match": (m is None and s is None) or (diff is not None and diff <= TOL)}
            comp_cmp[idx] = rows
        stored_map = {"vpi": "vpi", "mri": "mri", "rri": "rri", "zpi": "zpi", "cri": "cri", "acs": "acs", "atip": "atip_score"}
        sc_cmp = {k: {"recomputed": out[k], "stored": stored[stored_map[k]],
                      "abs_diff": None if stored[stored_map[k]] is None else round(abs(out[k] - stored[stored_map[k]]), 4)}
                  for k in SCORES}
        for k in sc_cmp:
            sc_cmp[k]["pass"] = sc_cmp[k]["abs_diff"] is not None and sc_cmp[k]["abs_diff"] <= TOL
        report["symbols"][sym] = {"has_technical_row": bool(trow), "bars": len(bars), "null_ema": t.get("ema_9") is None,
                                  "null_sma200": t.get("sma_200") is None, "indicators": ind_cmp, "components": comp_cmp,
                                  "scores": sc_cmp, "all_scores_pass": all(v["pass"] for v in sc_cmp.values()),
                                  "components_all_match": all(r["match"] for rows in comp_cmp.values() for r in rows.values()),
                                  "worst_component_diff": round(worst_comp, 4)}
    c.close()
    for sym, r in report["symbols"].items():
        if "error" in r:
            print(f"{sym:12} {r['error']}")
            continue
        line = "  ".join(f"{k.upper()} {v['recomputed']:.2f}/{v['stored']} {'ok' if v['pass'] else 'FAIL'}" for k, v in r["scores"].items())
        print(f"{sym:12} tech_row={int(r['has_technical_row'])} bars={r['bars']:<3} {line}  components_match={r['components_all_match']}")
    if a.json:
        Path(a.json).write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    return 0 if all(r.get("all_scores_pass") for r in report["symbols"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
