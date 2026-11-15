"""
V3-5 (5 Nov 2026 QA pass): the 3 Nov formula changes -- MRI (MACD read as reversal, ADX straight), ZPI (ATR read as
calm, Resistance = 2 x ADX) and ATIP's SAFETY = 100 - CRI -- measured against the formulas they replaced, point in
time, in-sample AND out-of-sample, so an improvement that exists in-sample only shows up.

    PYTHONPATH=<repo> python tests/verification/v3_reweights_is_oos.py --db <copy.db> --json out.json
"""
from __future__ import annotations

import argparse
import json
import sys

from scores import engine as E
from scores import validation as V


def old_mri(tech, ns, weights):
    """compute_mri before SV-03: MACD histogram read as momentum, ADX inverted."""
    from data.technical import macd_component
    c = {}
    m = macd_component(tech.get("macd_hist_pct"))
    if m is not None: c["MACD"] = m
    rsi = tech.get("rsi_14")
    if rsi is not None: c["RSI"] = 85 if 40 <= rsi <= 55 else 60 if rsi < 40 else 20 if rsi > 70 else 50
    if tech.get("adx_14"): c["ADX"] = E.minmax(tech["adx_14"], 0, 60, invert=True)
    vr = tech.get("volume_ratio")
    if vr: c["Volume"] = min(vr / 2 * 100, 100)
    e9, e21 = tech.get("ema_9"), tech.get("ema_21")
    if e9 and e21: c["EMA"] = 75 if e9 > e21 else 30
    c["News"] = ns
    return round(E.weighted_score(c, weights), 2)


def old_zpi(tech, inst, ns, sector_val, weights, accumulation=None):
    """compute_zpi before SV-03: Resistance = 100 - 2 x ADX, ATR = ATR% x 12 (more range = better)."""
    t = dict(tech)
    c = {}
    a200 = E.above_200(t)
    if a200 is not None: c["Support"] = 80 if a200 else 30
    if t.get("adx_14"): c["Resistance"] = max(0, 100 - t["adx_14"] * 2)
    rsi = t.get("rsi_14")
    if rsi is not None: c["RSI"] = 90 if 30 <= rsi <= 50 else 70 if rsi < 30 else 60 if rsi <= 60 else 20
    if t.get("atr_pct"): c["ATR"] = min(t["atr_pct"] * 12, 100)
    vr = t.get("volume_ratio")
    if vr: c["Volume"] = 80 if vr < 0.7 else 65 if vr < 1.0 else 45 if vr < 1.5 else 20
    if a200 is not None: c["Trend"] = 85 if a200 else 15
    if accumulation is not None: c["Institutional"] = accumulation
    c["News"] = ns
    c["Sector"] = sector_val
    return round(E.weighted_score(c, weights), 2)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--json", required=True)
    a = ap.parse_args(argv)
    p = V.load_panel(a.db)
    fwd = V.forward_returns(p)
    import sqlite3
    c = sqlite3.connect(a.db); c.row_factory = sqlite3.Row
    w_atip = E.load_weights(c, "ATIP"); c.close()
    no_safety = {k: v for k, v in w_atip.items() if k != "SAFETY"}
    runs = {"now": ({}, None), "old_mri": ({"mri": old_mri}, None), "old_zpi": ({"zpi": old_zpi}, None),
            "atip_without_safety": ({}, {"ATIP": no_safety})}
    res = {}
    for name, (fns, w) in runs.items():
        pit = V.score_panel(a.db, p, fns=fns, weights=w)
        ev = V.evaluate(pit, fwd, name, cols=("atip", "mri", "zpi"))
        res[name] = {col: {"verdict": ev["scores"][col]["verdict"],
                           **{f"h{h}": {"is": ev["scores"][col]["ic"]["is"][h]["mean"], "oos": ev["scores"][col]["ic"]["oos"][h]["mean"],
                                        "is_t": ev["scores"][col]["ic"]["is"][h]["t"], "oos_t": ev["scores"][col]["ic"]["oos"][h]["t"]}
                              for h in (1, 5, 20)}} for col in ("atip", "mri", "zpi")}
        print(name, {k: v["verdict"] for k, v in res[name].items()}, flush=True)
    verdict = {}
    for old, col in (("old_mri", "mri"), ("old_zpi", "zpi"), ("atip_without_safety", "atip")):
        rows = {}
        for h in ("h1", "h5", "h20"):
            n, o = res["now"][col][h], res[old][col][h]
            rows[h] = {"is_now": n["is"], "is_old": o["is"], "oos_now": n["oos"], "oos_old": o["oos"],
                       "is_better": (n["is"] or -9) > (o["is"] or -9), "oos_better": (n["oos"] or -9) > (o["oos"] or -9)}
        verdict[col] = {"by_horizon": rows, "oos_better_at_all": all(r["oos_better"] for r in rows.values()),
                        "is_better_at_all": all(r["is_better"] for r in rows.values())}
        print(col, json.dumps({h: (round(r["is_old"], 4), round(r["is_now"], 4), round(r["oos_old"], 4), round(r["oos_now"], 4))
                               for h, r in rows.items()}), "OOS better at 1/5/20:", verdict[col]["oos_better_at_all"], flush=True)
    res["verdict"] = verdict
    with open(a.json, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1, default=str)


if __name__ == "__main__":
    sys.exit(main())
