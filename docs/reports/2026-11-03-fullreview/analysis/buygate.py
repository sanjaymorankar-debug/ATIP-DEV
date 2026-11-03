import sys, numpy as np, pandas as pd
sys.path.insert(0, "/Users/agtci/ATIP/.claude/worktrees/full-review")
from scores import validation as V, engine as E
DB = sys.argv[1]
p = V.load_panel(DB); fwd = V.forward_returns(p)
orig = E.compute_vpi
def inv(tech, prices, fund, inst, ns, weights, rs=None, liquidity=None):
    t = dict(tech)
    if t.get("atr_pct"):
        t["atr_pct"] = max(0.001, 100 - min(t["atr_pct"] * 12, 100)) / 12
    return orig(t, prices, fund, inst, ns, weights, rs=rs, liquidity=liquidity)
dates = None
for name, fns in (("now", {}), ("inv", {"vpi": inv})):
    pit = V.score_panel(DB, p, fns=fns)
    pit.to_pickle(f"pit_{name}.pkl")
    d = sorted(pit.date.unique()); is_d, oos_d = V.split_dates(d)
    g = pit[(pit.cri < 60) & (pit.atip >= 65) & (pit.vpi >= 60) & (pit.zpi >= 55)]
    f5 = fwd[5].stack().rename("r5").reset_index(); f5.columns = ["date", "symbol", "r5"]
    f1 = fwd[1].stack().rename("r1").reset_index(); f1.columns = ["date", "symbol", "r1"]
    g = g.merge(f5, on=["date", "symbol"], how="left").merge(f1, on=["date", "symbol"], how="left")
    for lab, sub in (("IS", g[g.date.isin(is_d)]), ("OOS", g[g.date.isin(oos_d)])):
        nd = len(is_d if lab == "IS" else oos_d)
        r = sub.r5.dropna() * 100 - 0.25
        print(f"{name:4} {lab:3} BUY-gate rows {len(sub):5} ({len(sub)/nd:.1f}/day)  5d net mean {r.mean():+.2f}%  hit {(r>0).mean()*100:.0f}%  1d net {(sub.r1.dropna()*100-0.25).mean():+.2f}%")
    print(f"{name} VPI mean {pit.vpi.mean():.1f} p90 {pit.vpi.quantile(.9):.1f}; ATIP mean {pit.atip.mean():.1f} p99 {pit.atip.quantile(.99):.1f}")
