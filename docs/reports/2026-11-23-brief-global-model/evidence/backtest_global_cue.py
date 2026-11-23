"""
Backtest of ATIP's global-cue model (research/market_pulse.global_cue_model) on 10 years of history.

Scratch only: ATIP_DB_PATH points at ./atip_data/global_backtest.db next to this script (never the live database).
Run from this folder with PYTHONPATH=<ATIP worktree>:
    python backtest_global_cue.py fetch     # yfinance: global series + Nifty closes into the scratch DB, Nifty OHLC csv
    python backtest_global_cue.py test      # walk-forward evaluation -> results.json + RESULTS.md

The model: ridge regression of Nifty's close-to-close log return on each global factor's latest move dated strictly
before the Nifty session (S&P, Nasdaq, Nikkei, Hang Seng, Brent, dollar index, USD/INR, gold, US 10y), refit every
day on the 250 sessions before it (WINDOW). Same design() and _ridge() as the live code; no look-ahead.
"""

import json
import os
import sys

WORK = os.path.dirname(os.path.abspath(__file__))
os.makedirs(os.path.join(WORK, "atip_data"), exist_ok=True)
os.environ["ATIP_DB_PATH"] = os.path.join(WORK, "atip_data", "global_backtest.db")
os.chdir(WORK)                                   # relative atip_data/ paths land here, never in the live folder

import numpy as np                               # noqa: E402
import pandas as pd                              # noqa: E402

from db.schema import get_connection, init_db    # noqa: E402

PERIOD = "10y"
OHLC_CSV = os.path.join(WORK, "nifty_ohlc.csv")


def fetch():
    init_db()
    from data import markets as M
    from research import market_pulse as MP
    conn = get_connection()
    MP.ensure_tables(conn)
    conn.close()
    print("global:", M.backfill_global_history(period=PERIOD))
    print("nifty closes:", MP.nifty_history(period=PERIOD))
    df = M._yf_download("^NSEI", period=PERIOD, interval="1d", progress=False, auto_adjust=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close"]].dropna()
    df.index = pd.to_datetime(df.index).date
    df.to_csv(OHLC_CSV, index_label="date")
    print("nifty ohlc rows:", len(df), df.index.min(), df.index.max())
    vix = M._yf_download("^INDIAVIX", period=PERIOD, interval="1d", progress=False, auto_adjust=False)
    if isinstance(vix.columns, pd.MultiIndex):
        vix.columns = vix.columns.get_level_values(0)
    vix = vix[["Close"]].dropna()
    vix.index = pd.to_datetime(vix.index).date
    vix.to_csv(os.path.join(WORK, "india_vix.csv"), index_label="date")


def walk_forward(conn):
    from research import market_pulse as MP
    X, y = MP.design(conn)
    cols = list(X.columns)
    Xv, yv = X.to_numpy(float), y.to_numpy(float)
    w = MP.WINDOW
    rows = []
    for t in range(w, len(Xv)):
        b, mu, sd, ym = MP._ridge(Xv[t - w:t], yv[t - w:t])
        contrib = ((Xv[t] - mu) / sd) * b
        pred = float(ym + contrib.sum())
        sigma = float(np.std(yv[t - w:t]))
        rows.append({"date": X.index[t].date(), "pred": pred, "actual": float(yv[t]), "sigma": sigma,
                     "score": float(np.clip(pred / sigma, -3, 3)) if sigma else 0.0,
                     **{f"x_{c}": float(Xv[t][i]) for i, c in enumerate(cols)},
                     **{f"c_{c}": float(contrib[i]) for i, c in enumerate(cols)}})
    return pd.DataFrame(rows).set_index("date"), cols, X.attrs.get("stale", [])


def pct(x):
    return round(float(x) * 100, 3)


def stats(df, mask, label):
    d = df[mask]
    if not len(d):
        return {"bucket": label, "n": 0}
    return {"bucket": label, "n": int(len(d)),
            "mean_cc_pct": pct(d["cc"].mean()), "mean_gap_pct": pct(d["gap"].mean()),
            "mean_oc_pct": pct(d["oc"].mean()), "oc_down_share": round(float((d["oc"] < 0).mean()), 3),
            "cc_down_share": round(float((d["cc"] < 0).mean()), 3),
            "mean_o_c3_pct": pct(d["o_c3"].mean()), "o_c3_down_share": round(float((d["o_c3"] < 0).mean()), 3)}


def test():
    from research import market_pulse as MP
    conn = get_connection()
    wf, cols, stale = walk_forward(conn)
    model_now = MP.global_cue_model(conn)
    conn.close()

    o = pd.read_csv(OHLC_CSV, parse_dates=["date"]).set_index("date")
    o.index = o.index.date
    o["prev_close"] = o["Close"].shift(1)
    o["gap"] = o["Open"] / o["prev_close"] - 1
    o["oc"] = o["Close"] / o["Open"] - 1                       # bought at the open, sold at the close
    o["cc"] = o["Close"] / o["prev_close"] - 1
    o["o_c3"] = o["Close"].shift(-2) / o["Open"] - 1           # bought at the open, held to the 3rd close
    o["bad_open"] = (o["Open"] - o["prev_close"]).abs() < 1e-6  # Yahoo's old rows sometimes copy the close
    df = wf.join(o[["gap", "oc", "cc", "o_c3", "bad_open"]], how="inner").dropna(subset=["gap", "oc", "cc"])
    bad_open_share = float(df["bad_open"].mean())
    dfo = df[~df["bad_open"]]                                 # open-based stats only where the open is real

    big = df["actual"].abs() > 0.002
    sign_hit = lambda m: round(float((np.sign(df.loc[m, "pred"]) == np.sign(df.loc[m, "actual"])).mean()), 3)
    base_up = round(float((df["actual"] > 0).mean()), 3)
    rmse = float(np.sqrt(((df["pred"] - df["actual"]) ** 2).mean()))
    rmse0 = float(np.sqrt((df["actual"] ** 2).mean()))
    corr_cc = float(np.corrcoef(df["pred"], df["cc"])[0, 1])
    corr_gap = float(np.corrcoef(dfo["pred"], dfo["gap"])[0, 1])
    corr_oc = float(np.corrcoef(dfo["pred"], dfo["oc"])[0, 1])

    by_year = []
    for yr, d in df.groupby(pd.to_datetime(df.index).year):
        bg = d["actual"].abs() > 0.002
        by_year.append({"year": int(yr), "n": int(len(d)),
                        "hit_big": round(float((np.sign(d.loc[bg, "pred"]) == np.sign(d.loc[bg, "actual"])).mean()), 3),
                        "corr_cc": round(float(np.corrcoef(d["pred"], d["cc"])[0, 1]), 3)})

    edges = [-9, -0.0075, -0.004, -0.0015, 0.0015, 0.004, 0.0075, 9]
    names = ["<= -0.75%", "-0.75..-0.40%", "-0.40..-0.15%", "-0.15..+0.15%", "+0.15..+0.40%", "+0.40..+0.75%", ">= +0.75%"]
    buckets = [stats(dfo, (dfo["pred"] > lo) & (dfo["pred"] <= hi), n) for lo, hi, n in zip(edges[:-1], edges[1:], names)]
    buckets.insert(0, stats(dfo, dfo["pred"].notna(), "ALL DAYS"))

    spx = df.get("x_sp500")
    rules = [stats(dfo, dfo["pred"] <= -0.005, "model <= -0.50%"),
             stats(dfo, dfo["pred"] <= -0.0075, "model <= -0.75%"),
             stats(dfo, dfo["score"] <= -1.0, "model score <= -1 (NEGATIVE label)")]
    if spx is not None:
        rules += [stats(dfo, dfo["x_sp500"] <= np.log(1 - 0.015), "S&P prior session <= -1.5%"),
                  stats(dfo, dfo["x_sp500"] <= np.log(1 - 0.02), "S&P prior session <= -2%"),
                  stats(dfo, dfo["x_nasdaq"] <= np.log(1 - 0.02), "Nasdaq prior session <= -2%"),
                  stats(dfo, (dfo["x_sp500"] <= np.log(1 - 0.015)) & (dfo["gap"] > 0),
                        "S&P <= -1.5% but Nifty opened UP (like 23 Nov)")]

    # the 23 Nov 2026 call, out of sample (fitted on the 250 sessions before it)
    day = pd.Timestamp("2026-11-23").date()
    nov23 = None
    if day in wf.index:
        r = wf.loc[day]
        nov23 = {"pred_pct": pct(r["pred"]), "score": round(float(r["score"]), 2), "actual_cc_pct": pct(r["actual"]),
                 "label": MP._label(float(r["score"])),
                 "contrib_pct": {c: pct(r[f"c_{c}"]) for c in cols},
                 "inputs_pct": {c: pct(r[f"x_{c}"]) for c in cols}}
    last20 = [{"date": str(d), "pred_pct": pct(r["pred"]), "actual_pct": pct(r["actual"])}
              for d, r in wf.tail(20).iterrows()]

    res = {"period": PERIOD, "factors": cols, "stale_factors": stale, "window": 250, "sessions_tested": int(len(df)),
           "from": str(df.index.min()), "to": str(df.index.max()),
           "hit_all": sign_hit(df["actual"].notna()), "hit_big_moves": sign_hit(big), "base_up_share": base_up,
           "hit_when_pred_beyond_0.4": sign_hit(df["pred"].abs() >= 0.004),
           "rmse_pct": pct(rmse), "rmse_zero_pct": pct(rmse0), "beats_zero": rmse < rmse0,
           "corr_pred_vs_close_to_close": round(corr_cc, 3), "corr_pred_vs_gap": round(corr_gap, 3),
           "corr_pred_vs_open_to_close": round(corr_oc, 3), "bad_open_share": round(bad_open_share, 3),
           "by_year": by_year, "buckets": buckets, "rules": rules, "nov23": nov23, "last20": last20,
           "model_now": {k: model_now.get(k) for k in ("status", "observations", "window", "r2", "expected_move_pct",
                                                        "cue_score", "cue_label", "contributions_pct", "hit_rate",
                                                        "rmse_pct", "rmse_zero_forecast_pct", "beats_zero")}}
    with open(os.path.join(WORK, "results.json"), "w") as f:
        json.dump(res, f, indent=1, default=str)
    print(json.dumps({k: res[k] for k in ("sessions_tested", "from", "to", "hit_all", "hit_big_moves", "base_up_share",
                                           "hit_when_pred_beyond_0.4", "rmse_pct", "rmse_zero_pct", "beats_zero",
                                           "corr_pred_vs_close_to_close", "corr_pred_vs_gap",
                                           "corr_pred_vs_open_to_close", "bad_open_share", "stale_factors")}, indent=1))
    print("by_year", json.dumps(by_year))
    for b in buckets + rules:
        print(json.dumps(b))
    print("nov23", json.dumps(nov23, indent=1))
    print("model_now", json.dumps(res["model_now"], default=str)[:900])


if __name__ == "__main__":
    {"fetch": fetch, "test": test}[sys.argv[1]]()
