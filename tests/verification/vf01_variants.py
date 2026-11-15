"""
VF-01 (owner decision 5 Nov 2026: "fix if out of sample proves it"): the VPI volatility flip and an RRI redesign,
point in time, in-sample vs out-of-sample, with the BUY gate re-set on the in-sample period only.

The adoption rule is fixed before any result is looked at:
  a change is adopted only if (1) its own score's signed OOS rank IC is higher than today's formula at 1, 5 AND 20 days,
  (2) its in-sample 5-day IC is not lower (so the OOS gain is not one lucky window), and (3) ATIP's OOS IC -- what the
  ten sessions rank on -- is not lower at 1, 5 or 20 days.
The RRI redesign is chosen from the in-sample component ICs alone: a component whose in-sample t-stats (1, 5, 20 days)
sum below -2 is read the other way round, one whose |sum| is under 2 is left out, the rest stay; weights unchanged.
The BUY gate: VPI's two thresholds are re-set so the in-sample BUY-gate rows a day equal today's in-sample count.

    PYTHONPATH=<repo> python tests/verification/vf01_variants.py --db <copy.db> --json out.json
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np
import pandas as pd

from scores import engine as E
from scores import validation as V

H = (1, 5, 10, 20)


def flip_vpi(tech, prices, fund, inst, ns, weights, rs=None, liquidity=None):
    """VPI with V read as calm: V = 100 - min(ATR% x 12, 100); every other component as in compute_vpi."""
    t = dict(tech)
    if t.get("atr_pct"):
        t["atr_pct"] = max(1e-9, 100 - min(t["atr_pct"] * 12, 100)) / 12
    return E.compute_vpi(t, prices, fund, inst, ns, weights, rs=rs, liquidity=liquidity)


def rri_factory(plan: dict):
    """compute_rri with each component kept ('keep'), read the other way round ('flip') or left out ('drop')."""
    def f(tech, prices, inst, ns, weights):
        c = {}
        if not prices.empty:
            cmp, lo = prices["close"].iloc[0], prices["close"].min()
            if lo > 0: c["Recovery"] = min((cmp - lo) / lo * 200, 100)
        rsi = tech.get("rsi_14")
        if rsi is not None: c["RSIRecovery"] = min(max(0, (rsi - 30) * 2), 100)
        vr = tech.get("volume_ratio")
        if vr: c["Volume"] = min(vr / 2 * 100, 100)
        if not inst.empty:
            c["Institutional"] = E.minmax(float(inst["dii_net_cr"].sum() if "dii_net_cr" in inst.columns else 0), -200, 500)
        a200 = E.above_200(tech)
        if a200 is not None: c["Support"] = 80 if a200 else 30
        c["News"] = ns
        out = {}
        for k, v in c.items():
            how = plan.get(k, "keep")
            if how == "drop":
                continue
            out[k] = 100 - v if how == "flip" else v
        return round(E.weighted_score(out, weights), 2)
    return f


def long_of(wide: pd.DataFrame, dates) -> pd.DataFrame:
    w = wide.loc[dates]
    x = w.stack().rename("v").reset_index()
    x.columns = ["date", "symbol", "v"]
    return x


def ic_table(long: pd.DataFrame, fwd: dict, col: str, is_d, oos_d) -> dict:
    out = {}
    for h in H:
        ic = V.daily_ic(long, fwd[h], col)
        a, b = V.ic_summary(ic[ic.index.isin(is_d)], h), V.ic_summary(ic[ic.index.isin(oos_d)], h)
        out[h] = {"is": [a["mean"], a["t"]], "oos": [b["mean"], b["t"]]}
    return out


def buy_rows(pit, vs, vl, mh=50.0):
    """The BUY gates of determine_signal on the PIT panel (MH neutral 50 as in validation.score_panel)."""
    strict = (pit.atip >= 75) & (pit.vpi >= vs) & (pit.zpi >= 65) & (pit.acs >= 60) & (mh >= 50)
    loose = (pit.atip >= 65) & (pit.vpi >= vl) & (pit.zpi >= 55) & (mh >= 40)
    return pit[(pit.cri < 60) & (strict | loose)]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--json", required=True)
    a = ap.parse_args(argv)
    p = V.load_panel(a.db)
    ind = V.indicators(p)
    fwd = V.forward_returns(p)
    c = p["close"]
    dates = list(c.index[V.WARMUP:])
    is_d, oos_d = V.split_dates(dates)
    res = {"in_sample": [is_d[0], is_d[-1], len(is_d)], "out_of_sample": [oos_d[0], oos_d[-1], len(oos_d)]}
    print("IS", is_d[0], is_d[-1], len(is_d), "OOS", oos_d[0], oos_d[-1], len(oos_d), flush=True)

    # 1. RRI component ICs, in-sample only decides the plan
    clip = lambda x: x.clip(0, 100)
    comp = {"Recovery": clip((c - ind["low252"]) / ind["low252"] * 200), "RSIRecovery": clip((ind["rsi"] - 30) * 2),
            "Volume": clip(ind["volume_ratio"] / 2 * 100),
            "Support": ((c > ind["sma200"]).astype(float) * 50 + 30).where(ind["sma200"].notna())}
    plan, cics = {}, {}
    for k, w in comp.items():
        t = ic_table(long_of(w, dates), fwd, "v", is_d, oos_d)
        cics[k] = t
        s = sum((t[h]["is"][1] or 0) for h in (1, 5, 20))
        plan[k] = "flip" if s <= -2 else "drop" if abs(s) < 2 else "keep"
        print(f"  RRI.{k:12} IS t-sum {s:+.1f} -> {plan[k]:4}  " + "  ".join(
            f"h{h}: IS {t[h]['is'][0]:+.3f}({t[h]['is'][1]:+.1f}) OOS {t[h]['oos'][0]:+.3f}({t[h]['oos'][1]:+.1f})" for h in (1, 5, 20)), flush=True)
    res["rri_component_ic"], res["rri_plan_from_in_sample"] = cics, plan

    # 2. PIT panels
    variants = {"now": {}, "vpi_flip": {"vpi": flip_vpi}, "rri_new": {"rri": rri_factory(plan)},
                "both": {"vpi": flip_vpi, "rri": rri_factory(plan)}}
    pits = {}
    for name, fns in variants.items():
        pit = V.score_panel(a.db, p, fns=fns)
        pits[name] = pit
        ev = V.evaluate(pit, fwd, name, cols=("atip", "vpi", "rri", "acs"))
        res[name] = {col: {"verdict": ev["scores"][col]["verdict"],
                           "ic": {h: {"is": [ev["scores"][col]["ic"]["is"][h]["mean"], ev["scores"][col]["ic"]["is"][h]["t"]],
                                      "oos": [ev["scores"][col]["ic"]["oos"][h]["mean"], ev["scores"][col]["ic"]["oos"][h]["t"]]}
                                  for h in H},
                           "oos_decile_spread_5d": ev["scores"][col]["spread"][5]["decile"].get("spread_pct"),
                           "oos_top_decile_5d": ev["scores"][col]["hit"][5]}
                     for col in ("atip", "vpi", "rri", "acs")}
        print(name, {col: res[name][col]["verdict"] for col in ("atip", "vpi", "rri", "acs")}, flush=True)
        for col in ("atip", "vpi", "rri"):
            r = res[name][col]["ic"]
            print(f"   {col:4} " + "  ".join(f"h{h}: IS {r[h]['is'][0]:+.4f}({r[h]['is'][1]:+.1f}) OOS {r[h]['oos'][0]:+.4f}({r[h]['oos'][1]:+.1f})"
                                           for h in (1, 5, 20)), flush=True)

    # 3. the adoption rule
    def better(var, col):
        n, v = res["now"][col]["ic"], res[var][col]["ic"]
        oos = all((v[h]["oos"][0] or -9) > (n[h]["oos"][0] or -9) for h in (1, 5, 20))
        ins = (v[5]["is"][0] or -9) >= (n[5]["is"][0] or -9)
        atip_ok = all((res[var]["atip"]["ic"][h]["oos"][0] or -9) >= (res["now"]["atip"]["ic"][h]["oos"][0] or -9) - 1e-4
                      for h in (1, 5, 20))
        return {"own_oos_better_1_5_20": oos, "own_is_5d_not_lower": ins, "atip_oos_not_lower": atip_ok,
                "adopt": oos and ins and atip_ok}
    res["decision"] = {"vpi_flip": better("vpi_flip", "vpi"), "rri_new": better("rri_new", "rri"),
                       "both (ATIP)": better("both", "atip")}
    print("decision", json.dumps(res["decision"]), flush=True)

    # 4. BUY gate: today's gate on today's VPI vs a re-set VPI gate for the flip, calibrated in-sample
    f5 = fwd[5].stack().rename("r5").reset_index(); f5.columns = ["date", "symbol", "r5"]
    gate = {}
    base = pits["now"]
    nb = buy_rows(base[base.date.isin(is_d)], 70, 60)
    target = len(nb) / len(is_d)
    for name in ("vpi_flip", "both"):
        pit = pits[name]
        pis = pit[pit.date.isin(is_d)]
        best = None
        for vl in np.arange(55, 95.01, 0.5):
            r = len(buy_rows(pis, vl + 10, vl)) / len(is_d)
            if best is None or abs(r - target) < abs(best[1] - target):
                best = (float(vl), r)
        gate[name] = {"vpi_loose": best[0], "vpi_strict": best[0] + 10, "is_rows_per_day": round(best[1], 2)}
    out = {"target_is_rows_per_day": round(target, 2)}
    for name, (pit, vs, vl) in {"now (70/60)": (pits["now"], 70, 60),
                                "vpi_flip, old gate (70/60)": (pits["vpi_flip"], 70, 60),
                                f"vpi_flip, re-set gate": (pits["vpi_flip"], gate["vpi_flip"]["vpi_strict"], gate["vpi_flip"]["vpi_loose"]),
                                f"both, re-set gate": (pits["both"], gate["both"]["vpi_strict"], gate["both"]["vpi_loose"])}.items():
        row = {}
        for lab, dd in (("IS", is_d), ("OOS", oos_d)):
            g = buy_rows(pit[pit.date.isin(dd)], vs, vl).merge(f5, on=["date", "symbol"], how="left")
            r = g.r5.dropna() * 100 - 0.25
            row[lab] = {"rows_per_day": round(len(g) / len(dd), 2), "net_5d_mean_pct": round(float(r.mean()), 3) if len(r) else None,
                        "hit_pct": round(float((r > 0).mean() * 100), 1) if len(r) else None, "n": int(len(r))}
        out[name] = row
        print(f"  BUY {name:28} IS {row['IS']}  OOS {row['OOS']}", flush=True)
    res["buy_gate"] = {"calibrated": gate, "results": out}

    # 5. top-10 by ATIP per day (what the sessions buy): 5-day net, IS vs OOS
    top = {}
    for name in ("now", "vpi_flip", "rri_new", "both"):
        pit = pits[name].merge(f5, on=["date", "symbol"], how="left")
        t10 = pit.sort_values(["date", "atip"], ascending=[True, False]).groupby("date").head(10)
        top[name] = {lab: round(float((t10[t10.date.isin(dd)].r5.dropna() * 100 - 0.25).mean()), 3) for lab, dd in (("IS", is_d), ("OOS", oos_d))}
        print(f"  top-10 ATIP {name:9} 5d net IS {top[name]['IS']:+.3f}%  OOS {top[name]['OOS']:+.3f}%", flush=True)
    res["top10_atip_5d_net"] = top
    with open(a.json, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1, default=str)


if __name__ == "__main__":
    sys.exit(main())
