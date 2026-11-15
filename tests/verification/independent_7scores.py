"""
Independent recomputation of the seven ATIP scores (5 Nov 2026 QA pass).

Written from the spec (docs/reports/2026-11-03-fullreview/SCORE_AUDIT_REPORT.md §1) and the raw tables only. It does
not import scores/, data/, orders/ or utils/, and it does not reuse tests/verification/recompute_scores.py (the 4 Nov
pass's script). Two layers:

  A. indicators  RSI-14, EMA-9/21, SMA-200, ATR-14 %, ADX-14, volume ratio and MACD-histogram % recomputed from
                 prices_daily OHLCV (Wilder smoothing, EMA with adjust=False) and compared with technical_indicators.
  B. scores      VPI, MRI, RRI, ZPI, CRI, ACS and the ATIP composite (plus MSI, INS and the BUY/HOLD/SELL/WAIT
                 signal they feed) recomputed from technical_indicators + the raw input tables + weight_config, and
                 compared with ai_scores (tolerance 0.01) and score_components.

Inputs taken as given (not recomputed): technical_indicators.tech_score (TS), market_health (MH score, regime, VIX),
and the stock news score when news_symbol_score has no row for the day (those stocks are listed).

    python tests/verification/independent_7scores.py --db <copy.db> --date 2026-11-04 \
        --nifty500 atip_data/raw/ind_nifty500list.csv [--symbols TCS,ICICIBANK] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

TOL = 0.01


# ── helpers written from the spec ────────────────────────────────────────────
def scale(v, lo, hi, invert=False):
    """Fixed-range min-max to 0-100, clipped, 2 dp; no reading -> 50."""
    if v is None or (isinstance(v, float) and np.isnan(v)) or lo == hi:
        return 50.0
    s = (v - lo) / (hi - lo) * 100
    s = min(100.0, max(0.0, s))
    return round(100 - s if invert else s, 2)


def wmean(parts: dict, weights: dict) -> float:
    """Sum w*c / sum w over the components present (a missing one is left out, never 0)."""
    num = den = 0.0
    for k, w in weights.items():
        if parts.get(k) is not None:
            num += parts[k] * w
            den += w
    return round(num / den, 2) if den > 0 else 50.0


def wilder(x: pd.Series, n: int) -> pd.Series:
    return x.ewm(alpha=1 / n, adjust=False).mean()


def indicators(df: pd.DataFrame) -> dict:
    """df oldest first: open/high/low/close/volume. Standard definitions."""
    c, h, l, v = df["close"].astype(float), df["high"].astype(float), df["low"].astype(float), df["volume"].astype(float)
    out = {}
    d = c.diff()
    up, dn = d.clip(lower=0), -d.clip(upper=0)
    ag = up.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    al = dn.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rsi = 100 - 100 / (1 + ag / al)
    out["rsi_14"] = float(rsi.iloc[-1]) if len(c) > 14 else None
    out["ema_9"] = float(c.ewm(span=9, adjust=False).mean().iloc[-1])
    out["ema_21"] = float(c.ewm(span=21, adjust=False).mean().iloc[-1])
    out["sma_200"] = float(c.rolling(200).mean().iloc[-1]) if len(c) >= 200 else None
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    # ATR: first value the mean of the first 14 true ranges, then Wilder
    atr = pd.Series(np.nan, index=tr.index)
    if len(tr) > 14:
        atr.iloc[14] = tr.iloc[1:15].mean()
        for i in range(15, len(tr)):
            atr.iloc[i] = (atr.iloc[i - 1] * 13 + tr.iloc[i]) / 14
        out["atr_pct"] = float(atr.iloc[-1] / c.iloc[-1] * 100)
    # ADX (Wilder)
    upm, dnm = h.diff(), -l.diff()
    pdm = pd.Series(np.where((upm > dnm) & (upm > 0), upm, 0.0), index=c.index)
    ndm = pd.Series(np.where((dnm > upm) & (dnm > 0), dnm, 0.0), index=c.index)
    atr_w = wilder(tr, 14)
    pdi, ndi = 100 * wilder(pdm, 14) / atr_w, 100 * wilder(ndm, 14) / atr_w
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi)
    out["adx_14"] = float(wilder(dx.fillna(0), 14).iloc[-1]) if len(c) > 28 else None
    vs = v.rolling(20).mean().iloc[-1]
    out["volume_ratio"] = float(v.iloc[-1] / vs) if vs and vs > 0 else None
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    hist = macd - macd.ewm(span=9, adjust=False).mean()
    out["macd_hist_pct"] = float(hist.iloc[-1] / c.iloc[-1] * 100)
    return out


IND_TOL = {"rsi_14": ("abs", 0.5), "ema_9": ("rel", 0.002), "ema_21": ("rel", 0.002), "sma_200": ("rel", 0.0005),
           "atr_pct": ("rel", 0.03), "adx_14": ("abs", 2.0), "volume_ratio": ("rel", 0.01), "macd_hist_pct": ("abs", 0.02)}


def ind_ok(name, mine, stored):
    if mine is None or stored is None:
        return mine is None and stored is None
    kind, t = IND_TOL[name]
    return abs(mine - stored) <= (t if kind == "abs" else t * max(abs(stored), 1e-9))


# ── the run ──────────────────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--nifty500", required=True)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--json")
    ap.add_argument("--spec", choices=("2026-11-05", "2026-11-03"), default="2026-11-05",
                    help="2026-11-03: VPI V = ATR%% x 12 and VPI gates 70 / 60 (rows scored before the VF-01 change)")
    a = ap.parse_args(argv)
    if a.db.endswith("atip_data/atip.db"):
        raise SystemExit("point it at a copy")
    td = a.date
    gs, gl = (70, 60) if a.spec == "2026-11-03" else (76.5, 66.5)     # VPI BUY gates (strict, loose)
    D = datetime.strptime(td, "%Y-%m-%d")
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    q = lambda sql, *p: con.execute(sql, p).fetchall()
    one = lambda sql, *p: con.execute(sql, p).fetchone()

    W = {}
    for r in q("SELECT index_name, variable, weight FROM weight_config WHERE active = 1"):
        W.setdefault(r["index_name"], {})[r["variable"]] = r["weight"]

    stored = {r["symbol"]: dict(r) for r in q("SELECT * FROM ai_scores WHERE date = ?", td)}
    comps = {}
    for r in q("SELECT symbol, index_name, component, value FROM score_components WHERE date = ?", td):
        comps.setdefault(r["symbol"], {}).setdefault(r["index_name"], {})[r["component"]] = r["value"]
    want = [s for s in a.symbols.split(",") if s] or sorted(stored)

    # trading days = sessions with a full EQ bhavcopy (independent of utils.trading_calendar)
    tdays = [r[0] for r in q("SELECT date FROM prices_daily WHERE series = 'EQ' GROUP BY date HAVING COUNT(*) > 1000 ORDER BY date")]

    # market health (taken as given) and its VIX
    mh = dict(one("SELECT mh_score, regime, vix_level FROM market_health WHERE date = ?", td) or {})

    # ── MSI (market-wide) ──
    fii = dict(one("SELECT * FROM fii_dii_market WHERE date <= ? ORDER BY date DESC LIMIT 1", td) or {})
    idx = dict(one("SELECT * FROM index_levels WHERE date = ? ORDER BY time DESC LIMIT 1", td) or {})
    glb = dict(one("SELECT * FROM global_markets WHERE date <= ? ORDER BY date DESC LIMIT 1", td) or {})
    nxt = (D + timedelta(days=1)).strftime("%Y-%m-%d")
    r = one("SELECT AVG(news_score) FROM news_articles WHERE fetched_at >= ? AND fetched_at < ?",
            (D - timedelta(days=1)).strftime("%Y-%m-%d"), nxt)
    msi_c = {"News": float(r[0]) if r and r[0] else 50.0,
             "FII": scale(fii.get("fii_5d_avg") or 0, -2000, 2000),
             "DII": scale(fii.get("dii_5d_avg") or 0, -500, 1500)}
    chg = [idx.get(k) or 0 for k in ("nifty50_chg", "banknifty_chg", "midcap150_chg", "smallcap250_chg")]
    msi_c["Sector"] = sum(1 for x in chg if x > 0) / 4 * 100
    pr = one("SELECT pcr_oi FROM fo_underlying_daily WHERE date = ? AND symbol = 'NIFTY'", td)
    pcr = pr[0] if pr and pr[0] is not None else None
    if pcr is None:
        pr = one("SELECT pcr FROM fii_dii_market WHERE date = ? AND pcr IS NOT NULL", td)
        pcr = pr[0] if pr else None
    if pcr is not None:
        msi_c["Options"] = scale(pcr, 0.5, 1.8, invert=True)
    msi_c["Global"] = glb.get("global_score") or 50
    vr_ = one("SELECT india_vix FROM index_levels WHERE date = ? AND india_vix > 0 ORDER BY time DESC LIMIT 1", td)
    vix = vr_[0] if vr_ else (lambda x: x[0] if x else None)(one("SELECT close FROM prices_daily WHERE symbol='INDIAVIX' AND date=? AND close>0", td))
    if vix is not None:
        msi_c["VIX"] = scale(vix, 8, 35, invert=True)
    msi = wmean(msi_c, W["MSI"])

    # ── sector ranks: industries ranked by the day's average 1-day change of the scored universe ──
    n500 = pd.read_csv(a.nifty500)
    ind_map = dict(zip(n500["Symbol"], n500["Industry"]))
    held = {x[0] for x in q("SELECT DISTINCT symbol FROM portfolio_holdings")} if one(
        "SELECT 1 FROM sqlite_master WHERE name = 'portfolio_holdings'") else set()
    universe = (set(n500["Symbol"]) | held) & {x[0] for x in q("SELECT DISTINCT symbol FROM prices_daily WHERE date = ?", td)}
    day_chg = {}
    for s in universe:
        rr = q("SELECT close FROM prices_daily WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT 2", s, td)
        if len(rr) == 2 and rr[1][0]:
            day_chg[s] = (rr[0][0] / rr[1][0] - 1) * 100
    by_ind = {}
    for s, x in day_chg.items():
        if ind_map.get(s):
            by_ind.setdefault(ind_map[s], []).append(x)
    ranked = sorted(((k, sum(v) / len(v)) for k, v in by_ind.items()), key=lambda kv: kv[1], reverse=True)
    ind_rank = {k: i + 1 for i, (k, _) in enumerate(ranked)}
    n_ind = len(ranked)

    def sector_val(sym):
        rk = ind_rank.get(ind_map.get(sym))
        if rk is None or not n_ind:
            return 50.0
        p = rk / n_ind
        return 90.0 if p <= 0.25 else 60.0 if p <= 0.5 else 35.0

    bench = pd.read_sql("SELECT date, close FROM prices_daily WHERE symbol = 'NIFTY50' AND date <= ? ORDER BY date DESC LIMIT 30",
                        con, params=(td,))
    k5 = [d for d in tdays if d < td][-5] if len([d for d in tdays if d < td]) >= 5 else None

    rows, ind_rows, ns_given = [], [], []
    for sym in want:
        t = one("SELECT * FROM technical_indicators WHERE symbol = ? AND date = ?", sym, td)
        t = dict(t) if t else {}
        px = pd.read_sql("SELECT * FROM prices_daily WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT 260",
                         con, params=(sym, td))
        if not t:
            rows.append({"symbol": sym, "no_indicators": True, "stored": sym in stored})
            continue
        # layer A
        hist = pd.read_sql("SELECT date, open, high, low, close, volume FROM prices_daily WHERE symbol = ? AND date <= ? "
                           "ORDER BY date DESC LIMIT 520", con, params=(sym, td)).iloc[::-1].reset_index(drop=True)
        mine = indicators(hist) if len(hist) > 30 else {}
        ind_rows.append({"symbol": sym, **{k: ind_ok(k, mine.get(k), t.get(k)) for k in IND_TOL if k in mine}})

        # news score: stored per stock and day; else taken from the stored components (listed)
        nr = one("SELECT score FROM news_symbol_score WHERE symbol = ? AND date = ?", sym, td)
        if nr and nr[0] is not None:
            ns = float(nr[0])
        else:
            ns = comps.get(sym, {}).get("MRI", {}).get("News", 50.0)
            ns_given.append(sym)

        rsi, adx, atr, vr = t.get("rsi_14"), t.get("adx_14"), t.get("atr_pct"), t.get("volume_ratio")
        a200 = None if t.get("sma_200") is None else bool(t.get("above_200dma"))
        inst = q("SELECT fii_net_cr, dii_net_cr FROM institutional_data WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT 30", sym, td)

        # liquidity: 20-session average close x volume, Rs 100 Cr = 100
        w20 = px.iloc[:20]
        to = (w20["close"] * w20["volume"]).mean() if len(w20) else None
        lq = None if to is None or to != to else round(min(float(to) / 1e9 * 100, 100), 2)

        # relative strength vs Nifty 50, matched on date, 20 sessions
        rs = None
        m = px[["date", "close"]].merge(bench, on="date", suffixes=("", "_b")).sort_values("date", ascending=False)
        if len(px) >= 21 and len(bench) >= 21 and len(m) >= 21 and m["close"].iloc[20] and m["close_b"].iloc[20]:
            rs = scale((m["close"].iloc[0] / m["close"].iloc[20] - 1) * 100 - (m["close_b"].iloc[0] / m["close_b"].iloc[20] - 1) * 100, -15, 15)

        vpi_c = {}
        if atr: vpi_c["V"] = min(atr * 12, 100) if a.spec == "2026-11-03" else round(100 - min(atr * 12, 100), 2)
        if adx: vpi_c["TS"] = scale(adx, 0, 60)
        if rs is not None: vpi_c["RS"] = rs
        if lq is not None: vpi_c["LQ"] = lq
        if vr: vpi_c["VOL"] = min(vr / 3 * 100, 100)
        if rsi is not None: vpi_c["MR"] = 80 + (50 - rsi) if 30 <= rsi <= 50 else 70 if rsi < 30 else max(0, 60 - (rsi - 50) * 2)
        if inst: vpi_c["IS"] = scale(sum((x[0] or 0) + (x[1] or 0) for x in inst), -500, 500)
        vpi_c["NS"] = ns
        vpi = wmean(vpi_c, W["VPI"])

        mri_c = {}
        hp = t.get("macd_hist_pct")
        if hp is not None:
            mri_c["MACD"] = round(100 - round(50 + max(-50.0, min(50.0, float(hp) / 1.667 * 50)), 2), 2)
        if rsi is not None: mri_c["RSI"] = 85 if 40 <= rsi <= 55 else 60 if rsi < 40 else 20 if rsi > 70 else 50
        if adx: mri_c["ADX"] = scale(adx, 0, 60)
        if vr: mri_c["Volume"] = min(vr / 2 * 100, 100)
        if t.get("ema_9") and t.get("ema_21"): mri_c["EMA"] = 75 if t["ema_9"] > t["ema_21"] else 30
        mri_c["News"] = ns
        mri = wmean(mri_c, W["MRI"])

        rri_c = {}
        if len(px):
            lo = px["close"].min()
            if lo > 0: rri_c["Recovery"] = min((px["close"].iloc[0] - lo) / lo * 200, 100)
        if rsi is not None: rri_c["RSIRecovery"] = min(max(0, (rsi - 30) * 2), 100)
        if vr: rri_c["Volume"] = min(vr / 2 * 100, 100)
        if inst: rri_c["Institutional"] = scale(float(sum(x[1] or 0 for x in inst)), -200, 500)
        if a200 is not None: rri_c["Support"] = 80 if a200 else 30
        rri_c["News"] = ns
        rri = wmean(rri_c, W["RRI"])

        cri_c = {}
        if atr: cri_c["Volatility"] = min(atr * 15, 100)
        cri_c["Distribution"] = min(vr * 30, 100) if (vr and vr > 1.5) else 20
        if a200 is not None:
            cri_c["WeakTrend"] = min((0 if a200 else 40) + (40 if t.get("death_cross") else 0), 100)
        cri_c["NegativeNews"] = max(0, 100 - ns)
        mw = (50 if (mh.get("mh_score") or 50) < 40 else 0)
        mw += 30 if (mh.get("vix_level") is not None and mh["vix_level"] > 18) else 0
        mw += 20 if (mh.get("vix_level") is not None and mh["vix_level"] > 25) else 0
        cri_c["MarketWeakness"] = min(mw, 100)
        cri = wmean(cri_c, W["CRI"])

        zpi_c = {}
        if a200 is not None: zpi_c["Support"] = 80 if a200 else 30
        if adx: zpi_c["Resistance"] = min(100, adx * 2)
        if rsi is not None: zpi_c["RSI"] = 90 if 30 <= rsi <= 50 else 70 if rsi < 30 else 60 if rsi <= 60 else 20
        if atr: zpi_c["ATR"] = round(100 - min(atr * 12, 100), 2)
        if vr: zpi_c["Volume"] = 80 if vr < 0.7 else 65 if vr < 1.0 else 45 if vr < 1.5 else 20
        if a200 is not None: zpi_c["Trend"] = 85 if a200 else 15
        if "delivery_pct" in px.columns:
            dp = pd.to_numeric(px["delivery_pct"], errors="coerce")
            sh, bs = dp.iloc[:10], dp.iloc[:60]
            if sh.notna().sum() >= 8 and bs.notna().sum() >= 40 and bs.mean() > 0:
                zpi_c["Institutional"] = scale(sh.mean() / bs.mean(), 0.76, 1.24)
        zpi_c["News"] = ns
        zpi_c["Sector"] = sector_val(sym)
        zpi = wmean(zpi_c, W["ZPI"])

        ins_c = {}
        if fii.get("fii_5d_avg") is not None: ins_c["FII"] = scale(fii["fii_5d_avg"], -2000, 2000)
        if fii.get("dii_5d_avg") is not None: ins_c["DII"] = scale(fii["dii_5d_avg"], -500, 1500)
        b = one("SELECT SUM(net_value_cr) FROM bulk_deals WHERE symbol = ? AND date >= ? AND date <= ?",
                sym, (D - timedelta(days=10)).strftime("%Y-%m-%d"), td)
        if b and b[0] is not None: ins_c["BulkDeals"] = scale(float(b[0]), -50, 50)
        ins = round(wmean(ins_c, W["INS"]), 2) if ins_c else None

        ts = t.get("tech_score")
        acs_c = {}
        acc = one("SELECT AVG(correct_5d) * 100, COUNT(correct_5d) FROM accuracy_tracker WHERE symbol = ? AND pred_date >= ? "
                  "AND pred_date <= ? AND correct_5d IS NOT NULL", sym, (D - timedelta(days=90)).strftime("%Y-%m-%d"), k5)
        if acc and acc[1] >= 5: acs_c["HistoricalAccuracy"] = round(float(acc[0]), 2)
        agree = [x for x in (vpi, mri, rri, zpi, msi) if x]
        if agree: acs_c["Agreement"] = sum(1 for x in agree if x > 60) / len(agree) * 100
        if mh.get("mh_score") is not None: acs_c["MarketRegime"] = scale(mh["mh_score"], 0, 100)
        acs_c["DataQuality"] = sum(1 for x in (vpi, None, rri, mri, cri, msi, zpi, ts) if x is not None) / 8 * 100
        nc = one("SELECT AVG(confidence) FROM news_articles WHERE classifier = 'claude' AND symbols_mentioned LIKE ? "
                 "AND fetched_at >= ? AND fetched_at < ?", f'%"{sym}"%', (D - timedelta(days=7)).strftime("%Y-%m-%d"), nxt)
        if nc and nc[0] is not None:
            acs_c["NewsConfidence"] = round(float(nc[0]) * 100, 2)
        else:
            nc = one("SELECT AVG(confidence) FROM news_articles WHERE classifier = 'claude' AND fetched_at >= ? AND fetched_at < ?",
                     (D - timedelta(days=1)).strftime("%Y-%m-%d"), nxt)
            if nc and nc[0] is not None: acs_c["NewsConfidence"] = round(float(nc[0]) * 100, 2)
        if lq is not None: acs_c["Liquidity"] = lq
        acs = wmean(acs_c, W["ACS"])

        atip_c = {"VPI": vpi, "RRI": rri, "MRI": mri, "MSI": msi, "ZPI": zpi, "TS": ts, "INS": ins,
                  "SAFETY": round(100 - cri, 2)}            # SPI / FS: the W27 fundamentals switch is off
        atip = wmean({k: v for k, v in atip_c.items() if v is not None}, W["ATIP"])

        # signal, from the spec's decision table
        mh_s = 50 if mh.get("mh_score") is None else mh["mh_score"]
        reg = mh.get("regime") or "NEUTRAL"
        if cri > 75: sig = "SELL" if atip < 40 else "HOLD"
        elif reg == "HIGH_RISK": sig = "WAIT"
        elif reg == "BEAR" and atip < 70: sig = "WAIT"
        elif cri < 60 and ((atip >= 75 and vpi >= gs and zpi >= 65 and acs >= 60 and mh_s >= 50)
                           or (atip >= 65 and vpi >= gl and zpi >= 55 and mh_s >= 40)): sig = "BUY"
        elif atip < 30 or (cri > 60 and mri < 40): sig = "SELL"
        else: sig = "HOLD" if atip >= 50 else "WAIT"

        s = stored.get(sym, {})
        mine_s = {"vpi": vpi, "mri": mri, "rri": rri, "zpi": zpi, "cri": cri, "acs": acs, "atip_score": atip, "msi": msi}
        diff = {k: (None if s.get(k) is None else round(v - s[k], 4)) for k, v in mine_s.items()}
        cdiff = {}
        for name, mc in (("VPI", vpi_c), ("MRI", mri_c), ("RRI", rri_c), ("CRI", cri_c), ("ZPI", zpi_c), ("ACS", acs_c)):
            sc = comps.get(sym, {}).get(name, {})
            for k in set(mc) | set(sc):
                if k not in mc or k not in sc or abs(float(mc[k]) - float(sc[k])) > TOL:
                    cdiff[f"{name}.{k}"] = (None if k not in mc else round(float(mc[k]), 4), sc.get(k))
        rows.append({"symbol": sym, "mine": mine_s, "stored": {k: s.get(k) for k in mine_s}, "diff": diff,
                     "signal_mine": sig, "signal_stored": s.get("signal"), "component_diffs": cdiff,
                     "ok": all(x is not None and abs(x) <= TOL for x in diff.values()) and sig == s.get("signal")})
    con.close()

    scored = [r for r in rows if "mine" in r]
    bad = [r for r in scored if not r["ok"]]
    ind_df = pd.DataFrame(ind_rows).set_index("symbol") if ind_rows else pd.DataFrame()
    summary = {
        "date": td, "symbols": len(want), "scored_compared": len(scored), "within_0.01_and_same_signal": len(scored) - len(bad),
        "mismatches": [{"symbol": r["symbol"], "diff": {k: v for k, v in r["diff"].items() if v is None or abs(v) > TOL},
                        "signal": (r["signal_mine"], r["signal_stored"]), "components": r["component_diffs"]} for r in bad][:40],
        "no_indicator_rows": [r["symbol"] for r in rows if r.get("no_indicators")],
        "no_indicator_but_scored": [r["symbol"] for r in rows if r.get("no_indicators") and r.get("stored")],
        "news_score_taken_from_components": ns_given,
        "msi": {"mine": msi, "components": msi_c},
        "indicator_match_share": {k: round(float(ind_df[k].mean()), 4) for k in ind_df.columns} if len(ind_df) else {},
        "indicator_mismatch_examples": {k: list(ind_df.index[~ind_df[k].astype(bool)][:5]) for k in ind_df.columns} if len(ind_df) else {},
        "max_abs_diff": {k: max((abs(r["diff"][k]) for r in scored if r["diff"][k] is not None), default=None)
                         for k in ("vpi", "mri", "rri", "zpi", "cri", "acs", "atip_score", "msi")},
    }
    print(json.dumps(summary, indent=1, default=str))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "rows": rows}, f, indent=1, default=str)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
