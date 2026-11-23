"""
Backtest of the 9:00 AM Pre-Open Checklist (strategy/preopen_checklist.py) on the history that exists -- owner prompt
22 Nov 2026, PO-CL-12. Same rules and default thresholds as the live checklist; nothing is known that 09:00 IST would
not know.

What can be replayed, and from where
    US cues      Nasdaq Composite / Dow Jones: the last US daily close strictly before the Indian date vs the one before
                 it. NQ=F / YM=F: the close of the last hourly bar that ENDED by 09:00 IST (in practice 08:30 IST)
                 against the previous settlement (the last futures daily bar dated before the Indian date). Yahoo.
    India VIX    the previous session's close (^INDIAVIX daily, Yahoo) > 15 Volatile.
    NSE A/D      PRIOR-DAY only, as at 09:00 before NSE's book opens: ATIP's universe breadth (market_health advances /
                 declines, ~500 stocks, since Mar 2025), cross-checked with NSE's whole-market Bhavcopy where the lake
                 has it. Today's pre-open A/D exists only for the mornings data/preopen.py stored (Nifty + F&O set).
    GIFT Nifty   NOT replayable: no GIFT history at 09:00 exists (Dhan's IDX_I 5024 quote, the only one stored, is
                 frozen). Reported from the stored mornings only, flagged.
    Outcomes     Nifty 50 open / close (^NSEI daily, Yahoo; ATIP's prices_daily NIFTY50 is close-only), checked
                 against ATIP's stored closes. gap = open / previous close - 1 (class with the +-0.15 % band); day =
                 close / previous close - 1; open-to-close = close / open - 1 (what is left after the gap).

Each check is scored on days it voted (+1 / -1) and the outcome moved (not 0): hit rate, its standard error, and the
same days' majority-direction base rate ("always say the commoner direction"), so an edge means beating the base rate,
not 50 %.

    python -m tools.preopen_backtest --data DIR --db atip_data/atip.db --out docs/reports/2026-11-22-preopen-checklist
    (DIR: daily_NSEI.csv, daily_IXIC.csv, daily_DJI.csv, daily_INDIAVIX.csv, daily_NQ_F.csv, daily_YM_F.csv,
     hourly_NQ_F.csv, hourly_YM_F.csv as yfinance writes them; --fetch downloads them first)
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")
FILES = {"nifty": "daily_NSEI.csv", "nasdaq": "daily_IXIC.csv", "dow": "daily_DJI.csv", "vix": "daily_INDIAVIX.csv",
         "nq_d": "daily_NQ_F.csv", "ym_d": "daily_YM_F.csv", "nq_h": "hourly_NQ_F.csv", "ym_h": "hourly_YM_F.csv"}
TICKERS = {"nifty": "^NSEI", "nasdaq": "^IXIC", "dow": "^DJI", "vix": "^INDIAVIX", "nq_d": "NQ=F", "ym_d": "YM=F"}
US_BAND, VIX_THR, GIFT_BAND, AD_BULL, AD_BEAR = 0.10, 15.0, 0.15, 1.0, 1.0


def fetch(dirpath: Path, start="2024-06-01"):
    import yfinance as yf
    dirpath.mkdir(parents=True, exist_ok=True)
    d = yf.download(list(TICKERS.values()), start=start, interval="1d", group_by="ticker", progress=False,
                    auto_adjust=False)
    for k, t in TICKERS.items():
        d[t].dropna(how="all").to_csv(dirpath / FILES[k])
    h = yf.download(["NQ=F", "YM=F"], period="730d", interval="60m", group_by="ticker", progress=False, auto_adjust=False)
    h["NQ=F"].dropna(how="all").to_csv(dirpath / FILES["nq_h"])
    h["YM=F"].dropna(how="all").to_csv(dirpath / FILES["ym_h"])


def _read(dirpath: Path, key: str) -> pd.DataFrame:
    df = pd.read_csv(dirpath / FILES[key])
    col = df.columns[0]
    if key.endswith("_h"):
        df["ts"] = pd.to_datetime(df[col], utc=True)
    else:
        df["d"] = pd.to_datetime(df[col].astype(str).str[:10]).dt.date
    for c in ("Open", "High", "Low", "Close"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["Close"])


def sign_band(pct, band):
    if pct is None or (isinstance(pct, float) and math.isnan(pct)):
        return None
    return 1 if pct > band else -1 if pct < -band else 0


def load_breadth(db: str | None) -> dict:
    """{date: (advances, declines)} from market_health (ATIP universe), and the Bhavcopy whole market where stored."""
    out, nse = {}, {}
    if not db:
        return {"atip": out, "nse": nse}
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    for d, a, dn in c.execute("SELECT date, advances, declines FROM market_health WHERE advances IS NOT NULL AND "
                              "declines IS NOT NULL"):
        out[date.fromisoformat(str(d)[:10])] = (int(a), int(dn))
    base = Path(db).resolve().parent.parent
    for d, path in c.execute("SELECT partition_date, path FROM lake_partition WHERE dataset='nse_cm_bhavcopy'"):
        p = Path(path) if Path(path).is_absolute() else base / path
        try:
            df = pd.read_csv(p)
        except Exception:
            continue
        df = df[df["series"].astype(str).str.strip().str.upper() == "EQ"]
        df = df[(df["close"] > 0) & (df["prev_close"] > 0)]
        nse[date.fromisoformat(str(d)[:10])] = (int((df["close"] > df["prev_close"]).sum()),
                                               int((df["close"] < df["prev_close"]).sum()))
    c.close()
    return {"atip": out, "nse": nse}


def build_days(dirpath: Path, breadth: dict, start: date | None = None, end: date | None = None) -> pd.DataFrame:
    """One row per Nifty session with every input as known at 09:00 IST and the outcomes."""
    nifty = _read(dirpath, "nifty").sort_values("d").reset_index(drop=True)
    us = {k: _read(dirpath, k).sort_values("d") for k in ("nasdaq", "dow", "nq_d", "ym_d", "vix")}
    hourly = {k: _read(dirpath, k).sort_values("ts") for k in ("nq_h", "ym_h")}
    rows = []
    for i in range(1, len(nifty)):
        d, prev = nifty.loc[i, "d"], nifty.loc[i - 1, "d"]
        if (start and d < start) or (end and d > end):
            continue
        pc, o, c = nifty.loc[i - 1, "Close"], nifty.loc[i, "Open"], nifty.loc[i, "Close"]
        r = {"date": d, "prev_session": prev, "prev_close": pc, "open": o, "close": c,
             "gap_pct": (o / pc - 1) * 100, "day_pct": (c / pc - 1) * 100, "oc_pct": (c / o - 1) * 100}
        for k in ("nasdaq", "dow"):                         # the last US close strictly before the Indian date
            s = us[k][us[k]["d"] < d]
            r[f"{k}_chg"] = (s["Close"].iloc[-1] / s["Close"].iloc[-2] - 1) * 100 if len(s) >= 2 else None
            r[f"{k}_date"] = s["d"].iloc[-1] if len(s) else None
        cutoff = datetime(d.year, d.month, d.day, 9, 0, tzinfo=IST)
        for k in ("nq", "ym"):
            h = hourly[f"{k}_h"]
            done = h[h["ts"] + pd.Timedelta(hours=1) <= pd.Timestamp(cutoff)]
            settle = us[f"{k}_d"][us[f"{k}_d"]["d"] < d]
            if len(done) and len(settle) and (pd.Timestamp(cutoff) - done["ts"].iloc[-1]) <= pd.Timedelta(hours=3):
                r[f"{k}_chg"] = (done["Close"].iloc[-1] / settle["Close"].iloc[-1] - 1) * 100
            else:
                r[f"{k}_chg"] = None
        v = us["vix"][us["vix"]["d"] == prev]
        r["vix_prev"] = float(v["Close"].iloc[-1]) if len(v) else None
        ab = breadth["atip"].get(prev)
        r["adv"], r["dec"] = ab if ab else (None, None)
        nb = breadth["nse"].get(prev)
        r["nse_adv"], r["nse_dec"] = nb if nb else (None, None)
        rows.append(r)
    return pd.DataFrame(rows)


def votes(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    legs = [df[f"{k}_chg"].map(lambda x: sign_band(x, US_BAND)) for k in ("nasdaq", "dow", "nq", "ym")]
    green = sum((l == 1).astype(int) for l in legs)
    red = sum((l == -1).astype(int) for l in legs)
    have = sum(l.notna().astype(int) for l in legs)
    df["us_vote"] = [None if h == 0 else (g > rr) - (g < rr) for g, rr, h in zip(green, red, have)]
    df["us_close_only_vote"] = [None if pd.isna(a) and pd.isna(b) else
                                (lambda s: (s > 0) - (s < 0))(sum(x for x in (sign_band(a, US_BAND), sign_band(b, US_BAND))
                                                                  if x is not None))
                                for a, b in zip(df["nasdaq_chg"], df["dow_chg"])]
    df["fut_vote"] = [None if pd.isna(a) and pd.isna(b) else
                      (lambda s: (s > 0) - (s < 0))(sum(x for x in (sign_band(a, US_BAND), sign_band(b, US_BAND))
                                                        if x is not None))
                      for a, b in zip(df["nq_chg"], df["ym_chg"])]

    def ad(a, dn):
        if a is None or dn is None or pd.isna(a) or pd.isna(dn) or not dn:
            return None
        rt = a / dn
        return 1 if rt > AD_BULL else -1 if rt < AD_BEAR else 0
    df["ad_vote"] = [ad(a, dn) for a, dn in zip(df["adv"], df["dec"])]
    df["ad_nse_vote"] = [ad(a, dn) for a, dn in zip(df["nse_adv"], df["nse_dec"])]
    df["vix_regime"] = df["vix_prev"].map(lambda v: None if v is None or pd.isna(v) else
                                          "Volatile" if v > VIX_THR else "Stable")
    s = df[["us_vote", "ad_vote"]].apply(lambda r: [x for x in r if x is not None and not pd.isna(x)], axis=1)
    df["vote_sum"] = s.map(lambda xs: sum(xs) if xs else None)
    df["bias"] = df["vote_sum"].map(lambda x: None if x is None or pd.isna(x) else
                                    "Bullish" if x >= 1 else "Bearish" if x <= -1 else "Neutral")
    df["gap_class"] = df["gap_pct"].map(lambda g: {1: "Gap-Up", 0: "Flat", -1: "Gap-Down"}[sign_band(g, GIFT_BAND)])
    return df


def score(df: pd.DataFrame, vote_col: str, target: str) -> dict:
    """Hit rate of a +1/-1 vote against the sign of target, on days both are non-zero; the base rate is the share of
    the commoner direction on the same days (what 'always say it' would score)."""
    sub = df[[vote_col, target]].dropna()
    sub = sub[(sub[vote_col] != 0) & (sub[target] != 0)]
    n = len(sub)
    if not n:
        return {"n": 0, "hit_rate_pct": None, "se_pct": None, "base_rate_pct": None, "edge_pp": None}
    hits = int(((sub[vote_col] > 0) == (sub[target] > 0)).sum())
    p = hits / n
    up = float((sub[target] > 0).mean())
    base = max(up, 1 - up)
    return {"n": n, "hits": hits, "hit_rate_pct": round(100 * p, 1), "se_pct": round(100 * math.sqrt(p * (1 - p) / n), 1),
            "base_rate_pct": round(100 * base, 1), "edge_pp": round(100 * (p - base), 1),
            "significant": abs(p - base) > 2 * math.sqrt(p * (1 - p) / n) if 0 < p < 1 else False}


def bias_score(df, target):
    b = df["bias"].map({"Bullish": 1, "Bearish": -1, "Neutral": 0})
    return score(df.assign(_b=b), "_b", target)


def summarise(df: pd.DataFrame) -> dict:
    out = {"days": len(df), "from": str(df["date"].min()) if len(df) else None, "to": str(df["date"].max()) if len(df) else None}
    for name, col in (("us_combined", "us_vote"), ("us_closes_only", "us_close_only_vote"), ("us_futures_only", "fut_vote"),
                      ("ad_prior_day_atip_universe", "ad_vote"), ("ad_prior_day_nse_bhavcopy", "ad_nse_vote")):
        out[name] = {"vs_gap": score(df, col, "gap_pct"), "vs_day": score(df, col, "day_pct"),
                     "vs_open_to_close": score(df, col, "oc_pct"),
                     "coverage_pct": round(100 * df[col].notna().mean(), 1) if len(df) else None}
    out["bias_us_plus_ad"] = {"vs_gap": bias_score(df, "gap_pct"), "vs_day": bias_score(df, "day_pct"),
                              "vs_open_to_close": bias_score(df, "oc_pct"),
                              "neutral_days": int((df["bias"] == "Neutral").sum())}
    reg = {}
    for k in ("Volatile", "Stable"):
        sub = df[df["vix_regime"] == k]
        rng = ((sub["close"] - sub["open"]).abs() / sub["prev_close"] * 100) if len(sub) else pd.Series(dtype=float)
        reg[k] = {"days": len(sub), "mean_abs_gap_pct": round(float(sub["gap_pct"].abs().mean()), 3) if len(sub) else None,
                  "mean_abs_day_pct": round(float(sub["day_pct"].abs().mean()), 3) if len(sub) else None,
                  "mean_abs_open_to_close_pct": round(float(rng.mean()), 3) if len(sub) else None}
    out["vix_regime"] = reg
    out["gap_classes"] = df["gap_class"].value_counts().to_dict()
    return out


def stored_mornings(db: str | None) -> dict:
    """What the stored mornings say: pre-open A/D (data/preopen.py, Nifty + F&O set) and Dhan-GIFT market_cue rows."""
    if not db:
        return {}
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    po = c.execute("SELECT date, SUM(iep_chg_pct>0), SUM(iep_chg_pct<0) FROM preopen_snapshot GROUP BY date").fetchall()
    cue = c.execute("SELECT date, gift_move_pct, actual_gap_pct FROM market_cue WHERE actual_gap_pct IS NOT NULL AND "
                    "gift_move_pct IS NOT NULL ORDER BY date, captured_at").fetchall()
    c.close()
    return {"preopen_ad_days": [{"date": str(d)[:10], "advances": a, "declines": dn} for d, a, dn in po],
            "gift_mornings": len({str(r[0])[:10] for r in cue}),
            "gift_note": "Dhan's GIFT value is frozen (one value all day), so these mornings are not a valid test"}


def preopen_ad_score(df: pd.DataFrame, po_days: list) -> dict:
    m = {date.fromisoformat(x["date"]): x for x in po_days}
    sub = df[df["date"].isin(m.keys())].copy()
    if sub.empty:
        return {"n_days": 0}
    sub["po_vote"] = sub["date"].map(lambda d: (lambda a, dn: 1 if a > dn else -1 if a < dn else 0)(
        m[d]["advances"] or 0, m[d]["declines"] or 0))
    return {"n_days": len(sub), "vs_gap": score(sub, "po_vote", "gap_pct"), "vs_day": score(sub, "po_vote", "day_pct"),
            "vs_open_to_close": score(sub, "po_vote", "oc_pct"),
            "note": "today's pre-open book (Nifty + F&O set) -- known only at 09:07-09:08; tiny sample"}


def validate_closes(df: pd.DataFrame, db: str | None) -> dict:
    if not db:
        return {}
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    st = {date.fromisoformat(str(d)[:10]): float(v) for d, v in
          c.execute("SELECT date, close FROM prices_daily WHERE symbol='NIFTY50' AND close>0")}
    c.close()
    diffs = [abs(r.close / st[r.date] - 1) * 100 for r in df.itertuples() if r.date in st]
    return {"days_compared": len(diffs), "mean_abs_diff_pct": round(sum(diffs) / len(diffs), 4) if diffs else None,
            "max_abs_diff_pct": round(max(diffs), 4) if diffs else None}


def markdown(res: dict) -> str:
    def cell(s):
        if not s or not s.get("n"):
            return "—"
        sig = " ✓" if s.get("significant") else ""
        return f'{s["hit_rate_pct"]}% ±{s["se_pct"]} (n {s["n"]}; base {s["base_rate_pct"]}%, edge {s["edge_pp"]:+} pp){sig}'
    lines = []
    for w in ("full", "last_250", "last_60", "last_20"):
        r = res[w]
        lines += [f"### {w.replace('_', ' ')} — {r['days']} sessions, {r['from']} to {r['to']}", "",
                  "| Check | vs opening gap | vs day (close vs prev close) | vs open→close | coverage |",
                  "|---|---|---|---|---|"]
        for name, label in (("us_combined", "1. US cues (Nasdaq, Dow, NQ, YM)"), ("us_closes_only", "  US closes only"),
                            ("us_futures_only", "  US futures only (≈08:30 IST)"),
                            ("ad_prior_day_atip_universe", "3. A/D prior-day (ATIP universe)"),
                            ("ad_prior_day_nse_bhavcopy", "  A/D prior-day (NSE Bhavcopy, where stored)")):
            x = r[name]
            lines.append(f"| {label} | {cell(x['vs_gap'])} | {cell(x['vs_day'])} | {cell(x['vs_open_to_close'])} | "
                         f"{x['coverage_pct']}% |")
        b = r["bias_us_plus_ad"]
        lines.append(f"| **Bias (US + A/D; GIFT not replayable)** | {cell(b['vs_gap'])} | {cell(b['vs_day'])} | "
                     f"{cell(b['vs_open_to_close'])} | {b['neutral_days']} neutral days |")
        v = r["vix_regime"]
        lines += ["", f"VIX regime (previous close vs 15): Volatile {v['Volatile']['days']} days — mean |gap| "
                      f"{v['Volatile']['mean_abs_gap_pct']}%, mean |day| {v['Volatile']['mean_abs_day_pct']}%; Stable "
                      f"{v['Stable']['days']} days — mean |gap| {v['Stable']['mean_abs_gap_pct']}%, mean |day| "
                      f"{v['Stable']['mean_abs_day_pct']}%.", ""]
    return "\n".join(lines)


def run(dirpath: Path, db: str | None, out: Path | None = None) -> dict:
    breadth = load_breadth(db)
    df = votes(build_days(dirpath, breadth))
    df = df.sort_values("date").reset_index(drop=True)
    res = {"generated_at_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
           "rules": {"us_band_pct": US_BAND, "vix_threshold": VIX_THR, "gift_band_pct": GIFT_BAND, "ad_ratio": AD_BULL,
                     "bias_min_votes": 1},
           "full": summarise(df), "last_250": summarise(df.tail(250)), "last_60": summarise(df.tail(60)),
           "last_20": summarise(df.tail(20)), "by_year": {str(y): summarise(g) for y, g in
                                                          df.groupby(df["date"].map(lambda d: d.year))},
           "stored": stored_mornings(db), "close_check_vs_atip": validate_closes(df, db),
           "nse_vs_atip_ad_sign_agreement": None}
    both = df.dropna(subset=["ad_vote", "ad_nse_vote"])
    if len(both):
        res["nse_vs_atip_ad_sign_agreement"] = {"days": len(both),
                                                "agree_pct": round(100 * float((both["ad_vote"] == both["ad_nse_vote"]).mean()), 1)}
    res["preopen_ad"] = preopen_ad_score(df, res["stored"].get("preopen_ad_days", []))
    if out:
        out.mkdir(parents=True, exist_ok=True)
        (out / "backtest.json").write_text(json.dumps(res, indent=2, default=str))
        df.to_csv(out / "backtest_days.csv", index=False)
        (out / "backtest_tables.md").write_text(markdown(res))
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m tools.preopen_backtest")
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--db", default=None)
    ap.add_argument("--out", default=None, type=Path)
    ap.add_argument("--fetch", action="store_true")
    a = ap.parse_args(argv)
    if a.fetch:
        fetch(a.data)
    res = run(a.data, a.db, a.out)
    print(markdown(res))


if __name__ == "__main__":
    main()
