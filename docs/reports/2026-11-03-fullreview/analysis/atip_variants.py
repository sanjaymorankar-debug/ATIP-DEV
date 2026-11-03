import json, sys, time
sys.path.insert(0, "/Users/agtci/ATIP/.claude/worktrees/full-review")
import pandas as pd
from scores import validation as V, engine as E
DB = sys.argv[1]; which = sys.argv[2]
p = V.load_panel(DB); fwd = V.forward_returns(p)
def vpi_variant(mode):
    def f(tech, prices, fund, inst, ns, weights, rs=None, liquidity=None):
        t = dict(tech)
        atr = t.pop("atr_pct", None)
        v = E.compute_vpi(t, prices, fund, inst, ns, weights, rs=rs, liquidity=liquidity)
        if mode == "drop" or not atr:
            return v
        # put V back as the inverted component with its own weight
        w = dict(weights); wv = w.get("V", 0)
        rest = sum(x for k, x in w.items() if k != "V")  # approximate: present-set renormalisation handled below
        c = {"V": max(0.0, 100 - min(atr * 12, 100))}
        return v  # placeholder (replaced below)
    return f
t0 = time.time()
fns = {}
if which == "drop":
    fns["vpi"] = vpi_variant("drop")
elif which == "inv":
    orig = E.compute_vpi
    def inv(tech, prices, fund, inst, ns, weights, rs=None, liquidity=None):
        t = dict(tech)
        if t.get("atr_pct"):
            t["atr_pct"] = max(0.0, 100 - min(t["atr_pct"] * 12, 100)) / 12   # V = 100 - ATR%x12
        return orig(t, prices, fund, inst, ns, weights, rs=rs, liquidity=liquidity)
    fns["vpi"] = inv
pit = V.score_panel(DB, p, fns=fns)
res = V.evaluate(pit, fwd, which, cols=("atip", "vpi", "acs"))
print(which, round(time.time() - t0), "s")
for col in ("atip", "vpi", "acs"):
    s = res["scores"][col]
    print(f"  {col:5} {s['verdict']:8}", " ".join(f"h{h}: IS {s['ic']['is'][h]['mean']:+.3f}({s['ic']['is'][h]['t']:+.1f}) OOS {s['ic']['oos'][h]['mean']:+.3f}({s['ic']['oos'][h]['t']:+.1f})" for h in (1, 5, 10, 20)),
          " D10-D1 5d", s["spread"][5]["decile"].get("spread_pct"), " top-decile 5d hit", s["hit"][5]["BUY"].get("hit_pct"), "exp", s["hit"][5]["BUY"].get("expectancy_pct"))
print("  corr", {k: v for k, v in res["corr_oos"].get("vpi", {}).items()})
json.dump(res, open(f"atip_{which}.json", "w"), indent=1, default=str)
