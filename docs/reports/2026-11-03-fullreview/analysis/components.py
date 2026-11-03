"""Per-component rank IC of VPI and RRI (and the other scores' parts), point-in-time, IS vs OOS."""
import json, sys, time
import numpy as np, pandas as pd
sys.path.insert(0, "/Users/agtci/ATIP/.claude/worktrees/full-review")
from scores import validation as V
DB = sys.argv[1]
t0 = time.time()
p = V.load_panel(DB); ind = V.indicators(p); fwd = V.forward_returns(p)
c = p["close"]; bench = V._nifty_ret20(DB, c.index)
clip = lambda x: x.clip(0, 100)
comp = {
 "VPI.V(atr%x12)": clip(ind["atr_pct"] * 12),
 "VPI.TS(adx)": clip(ind["adx"] / 60 * 100),
 "VPI.RS(ret20-nifty)": clip(((ind["ret20"].sub(bench, axis=0)) * 100 + 15) / 30 * 100),
 "VPI.LQ(turnover)": clip(ind["turnover20"] / 1e9 * 100),
 "VPI.VOL(vr/3)": clip(ind["volume_ratio"] / 3 * 100),
 "VPI.MR(rsi bucket)": ind["rsi"].apply(lambda col: col.map(lambda r: np.nan if r != r else (80 + (50 - r) if 30 <= r <= 50 else 70 if r < 30 else max(0, 60 - (r - 50) * 2)))),
 "RRI.Recovery(off 52w low)": clip((c - ind["low252"]) / ind["low252"] * 200),
 "RRI.RSIRecovery": clip((ind["rsi"] - 30) * 2),
 "RRI.Volume(vr/2)": clip(ind["volume_ratio"] / 2 * 100),
 "RRI.Support(>200dma)": (c > ind["sma200"]).astype(float).where(ind["sma200"].notna()) * 50 + 30,
 "raw.ret20": ind["ret20"], "raw.atr_pct": ind["atr_pct"],
}
dates = list(c.index[V.WARMUP:])
is_d, oos_d = V.split_dates(dates)
out = {}
for k, w in comp.items():
    w = w.loc[dates]
    long = w.stack().rename("v").reset_index(); long.columns = ["date", "symbol", "v"]
    row = {}
    for h in (1, 5, 10, 20):
        ic = V.daily_ic(long, fwd[h], "v")
        a, b = V.ic_summary(ic[ic.index.isin(is_d)], h), V.ic_summary(ic[ic.index.isin(oos_d)], h)
        row[h] = {"is": (a["mean"], a["t"]), "oos": (b["mean"], b["t"])}
    out[k] = row
    print(f"{k:28}", "  ".join(f"h{h}: IS {row[h]['is'][0]:+.3f}({row[h]['is'][1]:+.1f}) OOS {row[h]['oos'][0]:+.3f}({row[h]['oos'][1]:+.1f})" for h in (1, 5, 20)), flush=True)
json.dump({"is": [is_d[0], is_d[-1]], "oos": [oos_d[0], oos_d[-1]], "components": out}, open(sys.argv[2], "w"), indent=1, default=str)
print("done", round(time.time() - t0), "s", is_d[0], is_d[-1], oos_d[0], oos_d[-1])
