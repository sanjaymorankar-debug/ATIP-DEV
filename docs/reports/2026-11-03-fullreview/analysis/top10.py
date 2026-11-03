import sys, numpy as np, pandas as pd
sys.path.insert(0, "/Users/agtci/ATIP/.claude/worktrees/full-review")
from scores import validation as V
DB = sys.argv[1]
p = V.load_panel(DB); fwd = V.forward_returns(p); ind = V.indicators(p)
to = ind["turnover20"].stack().rename("to").reset_index(); to.columns = ["date", "symbol", "to"]
cl = p["close"].stack().rename("cl").reset_index(); cl.columns = ["date", "symbol", "cl"]
F = {}
for h in (1, 5):
    f = fwd[h].stack().rename(f"r{h}").reset_index(); f.columns = ["date", "symbol", f"r{h}"]; F[h] = f
for name in ("now", "inv"):
    pit = pd.read_pickle(f"pit_{name}.pkl").merge(to, on=["date", "symbol"]).merge(cl, on=["date", "symbol"])
    pit = pit[(pit.cri < 75) & (pit.cl >= 20) & (pit.to >= 2e7)]
    d = sorted(pit.date.unique()); is_d, oos_d = V.split_dates(d)
    top = pit.sort_values(["date", "atip", "cri"], ascending=[True, False, True]).groupby("date").head(10)
    for h in (1, 5):
        top = top.merge(F[h], on=["date", "symbol"], how="left")
    for lab, dd in (("IS", is_d), ("OOS", oos_d)):
        s = top[top.date.isin(dd)]
        daily1 = s.groupby("date").r1.mean() * 100 - 0.25; daily5 = s.groupby("date").r5.mean() * 100 - 0.25
        uni5 = pit[pit.date.isin(dd)].merge(F[5], on=["date", "symbol"]).groupby("date").r5.mean() * 100
        print(f"{name} {lab:3} top-10 ATIP/day: 1d net {daily1.mean():+.3f}%  5d net {daily5.mean():+.3f}%  (universe 5d gross {uni5.mean():+.3f}%)  5d excess vs universe {(daily5 + 0.25 - uni5).mean():+.3f}%  hit5 {(s.r5*100-0.25>0).mean()*100:.0f}%")
    # signal frequency under the BUY gate per regime-neutral
