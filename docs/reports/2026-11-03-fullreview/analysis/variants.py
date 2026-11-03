"""Composite variants for VPI / RRI / ATIP, point-in-time, IS vs OOS. Decision rule (set before looking):
adopt a variant only if its signed IC is higher than the current formula in BOTH the in-sample and the
out-of-sample period at h=1 and h=5, and not lower at h=20."""
import json, sys
import numpy as np, pandas as pd
sys.path.insert(0, "/Users/agtci/ATIP/.claude/worktrees/full-review")
from scores import validation as V
DB = sys.argv[1]
p = V.load_panel(DB); ind = V.indicators(p); fwd = V.forward_returns(p)
c = p["close"]; bench = V._nifty_ret20(DB, c.index)
clip = lambda x: x.clip(0, 100)
rsi = ind["rsi"]
def mr(r):
    a = 80 + (50 - r); b = (60 - (r - 50) * 2).clip(lower=0)
    return a.where((r >= 30) & (r <= 50), b.where(r > 50, 70)).where(r.notna())
C = {
 "V": clip(ind["atr_pct"] * 12), "TS": clip(ind["adx"] / 60 * 100),
 "RS": clip(((ind["ret20"].sub(bench, axis=0)) * 100 + 15) / 30 * 100),
 "LQ": clip(ind["turnover20"] / 1e9 * 100), "VOL": clip(ind["volume_ratio"] / 3 * 100), "MR": mr(rsi),
 "Recovery": clip((c - ind["low252"]) / ind["low252"] * 200), "RSIRecovery": clip((rsi - 30) * 2),
 "Volume": clip(ind["volume_ratio"] / 2 * 100),
 "Support": ((c > ind["sma200"]).astype(float) * 50 + 30).where(ind["sma200"].notna()),
 "CRIvol": clip(ind["atr_pct"] * 15),
}
C["Vinv"] = 100 - C["V"]
def comp(ws, xs=False):
    num = 0; den = 0
    for k, w in ws.items():
        x = C[k]
        if xs:
            x = x.rank(axis=1, pct=True) * 100
        num = num + (x * w).fillna(0); den = den + x.notna() * w
    return (num / den).where(den > 0)
VPI_W = {"V": .18, "TS": .15, "RS": .12, "LQ": .10, "VOL": .10, "MR": .10}
RRI_W = {"Recovery": .25, "Support": .20, "RSIRecovery": .15, "Volume": .15}
var = {
 "VPI now": comp(VPI_W),
 "VPI V dropped": comp({k: v for k, v in VPI_W.items() if k != "V"}),
 "VPI V inverted": comp({**{k: v for k, v in VPI_W.items() if k != "V"}, "Vinv": .18}),
 "VPI xs-pct": comp(VPI_W, xs=True),
 "RRI now": comp(RRI_W),
 "RRI xs-pct": comp(RRI_W, xs=True),
}
dates = list(c.index[V.WARMUP:]); is_d, oos_d = V.split_dates(dates)
res = {}
for k, w in var.items():
    w = w.loc[dates]; long = w.stack().rename("v").reset_index(); long.columns = ["date", "symbol", "v"]
    row = {}
    for h in (1, 5, 10, 20):
        ic = V.daily_ic(long, fwd[h], "v")
        a, b = V.ic_summary(ic[ic.index.isin(is_d)], h), V.ic_summary(ic[ic.index.isin(oos_d)], h)
        row[h] = {"is": [a["mean"], a["t"]], "oos": [b["mean"], b["t"]]}
    o = long[long.date.isin(oos_d)]
    row["oos_decile_5d"] = V.bucket_spread(o, fwd[5], "v", 10).get("spread_pct")
    res[k] = row
    print(f"{k:16}", "  ".join(f"h{h}: IS {row[h]['is'][0]:+.3f}({row[h]['is'][1]:+.1f}) OOS {row[h]['oos'][0]:+.3f}({row[h]['oos'][1]:+.1f})" for h in (1, 5, 10, 20)), " D10-D1 5d OOS", row["oos_decile_5d"], flush=True)
json.dump(res, open(sys.argv[2], "w"), indent=1)
