"""Does a rank-fill pick bought later in the day do worse? (20 Nov 2026 session review, read-only)

ATIP's own replay engine (research/session_ab_replay.py: 15-minute bars, stop before target, gapped opens fill at the
open, slippage and NSE delivery charges). Every rank-fill candidate (top 10 BUY/HOLD per scored day, the live picker's
order) is bought on its buy day at the open of the first 15-minute bar at or after T, if the price there is inside the
live picker's window (no more than 0.5 % over the signal close, no more than 1.5 % under it: max_intraday_drop_pct),
then sold by each session arm's exit. T = 09:15 is today's open entry; later T is a pick re-queued when a session
freed up during the day (KGF-03 allows that until 14:45).

python entry_time_replay.py SQLITE OUTDIR
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import research.session_ab_replay as R

db, out = sys.argv[1], Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)
data = R.load(db)
start = pd.Timestamp("2026-09-29")                      # first day with 15-minute bars (28 Sep) + 1
end = data.last_day
bars = R.Bars(R.hybrid_bars(data.daily, data.bars, start, end, True))
dd = R.atr_pct(data.daily)
C = R.rank_fill_candidates(data, start, end, 10, dd)
have = set(zip(data.bars["symbol"], data.bars["ts"].dt.normalize()))
C = C[[(s, pd.Timestamp(d)) in have for s, d in zip(C["sym"], C["nday"])]]   # buy days with real 15-minute bars
EXITS = {"S5 control (2% / trail 1% from +3%)": {"stop": {"type": "pct", "value": 2.0},
                                                 "trail": {"after": {"type": "pct", "value": 3.0}, "by": {"type": "pct", "value": 1.0}},
                                                 "hold_days": 15},
         "S7 (4% / trail 2% from +4%)": {"stop": {"type": "pct", "value": 4.0},
                                         "trail": {"after": {"type": "pct", "value": 4.0}, "by": {"type": "pct", "value": 2.0}},
                                         "hold_days": 15},
         "S1 (ATR 1.5x, 3-6%)": {"stop": {"type": "atr", "mult": 1.5, "min": 3.0, "max": 6.0},
                                 "trail": {"after": {"type": "pct", "value": 4.0}, "by": {"type": "pct", "value": 2.0}},
                                 "hold_days": 15},
         "S9 bracket 3/6": {"stop": {"type": "pct", "value": 3.0}, "target_pct": 6.0}}
TIMES = ["09:15", "09:30", "09:45", "10:00", "10:30", "11:00", "12:00", "13:00", "14:00"]
up = 1 + R.DEFAULT_FILLS.entry_slip_bps / 1e4
rows = []
for r in C.itertuples(index=False):
    ts, day, o, h, l, c = bars.d[r.sym]
    idx = np.flatnonzero(day == np.datetime64(pd.Timestamp(r.nday), "D"))
    if not len(idx):
        continue
    for T in TIMES:
        t = pd.Timestamp(r.nday) + pd.Timedelta(hours=int(T[:2]), minutes=int(T[3:]))
        j = [i for i in idx if pd.Timestamp(ts[i]) >= t]
        if not j:
            continue
        i = j[0]
        px = float(o[i])
        if px > r.c * 1.005 or px < r.c * (1 - 0.015):
            continue
        for name, spec in EXITS.items():
            s = R.simulate(bars, r.sym, pd.Timestamp(ts[i]), px * up, spec, atr_pct=r.atrp)
            if s:
                rows.append(dict(T=T, exit=name, sym=r.sym, nday=str(pd.Timestamp(r.nday).date()), epx=px * up,
                                 run_up_pct=round((px / float(o[idx[0]]) - 1) * 100, 2), **{k: s[k] for k in ("reason", "net", "netpct")}))
X = pd.DataFrame(rows)
X.to_csv(out / "entry_time_replay_trades.csv", index=False)


def summ(g):
    w, lo = g[g.net > 0], g[g.net <= 0]
    pf = w.net.sum() / -lo.net.sum() if len(lo) and lo.net.sum() < 0 else np.nan
    return pd.Series(dict(n=len(g), win_pct=round(100 * len(w) / len(g), 1), avg_netpct=round(g.netpct.mean(), 3),
                          total_net=round(g.net.sum(), 0), pf=round(pf, 2), stopped_pct=round(100 * (g.reason == "Stop").mean(), 1)))


S = X.groupby(["exit", "T"]).apply(summ).reset_index()
S.to_csv(out / "entry_time_replay_summary.csv", index=False)
# early (before 10:00) vs late (10:00 and after), each exit
X["when"] = np.where(X["T"] < "10:00", "before 10:00", "10:00 or later")
E = X.groupby(["exit", "when"]).apply(summ).reset_index()
E.to_csv(out / "entry_time_replay_early_late.csv", index=False)
# the same, split by calendar half (robustness)
X["half"] = np.where(X["nday"] < "2026-10-27", "29 Sep-26 Oct", "27 Oct-19 Nov")
H = X.groupby(["exit", "half", "when"]).apply(summ).reset_index()
H.to_csv(out / "entry_time_replay_halves.csv", index=False)
json.dump({"candidates": int(len(C)), "buy_days": int(C["nday"].nunique()), "rows": int(len(X)),
           "first": str(C["nday"].min()), "last": str(C["nday"].max())}, open(out / "entry_time_replay_meta.json", "w"), indent=1, default=str)
pd.set_option("display.width", 250)
print(S.to_string(index=False)); print(); print(E.to_string(index=False)); print(); print(H.to_string(index=False))
