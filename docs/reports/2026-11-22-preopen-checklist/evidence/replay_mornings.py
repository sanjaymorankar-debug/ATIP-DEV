"""
Replay the Pre-Open Checklist (strategy/preopen_checklist.py, the real code) for past mornings on whatever database
ATIP_DB_PATH / ATIP_DATABASE_URL points at -- the Test server's scratch DB or the Prod snapshot (the VPS dump of
20 Nov 2026 19:15 IST restored on a throwaway PostgreSQL). Nothing reaches Dhan or NSE:

    US cues   from the Yahoo history the backtest downloaded (tools/preopen_backtest.py rules: the last US close before
              the date; NQ / YM at the last hourly bar that ended by 09:00 IST against the previous settlement)
    VIX       Dhan is not called -> the checklist's own fallback: the stored prior close (index_levels / prices_daily)
    A/D       NSE is not called -> the checklist's own fallbacks: the stored 09:09 pre-open capture (preopen_snapshot,
              Nifty + F&O set), else the prior day (Bhavcopy lake / market_health)
    GIFT      no GIFT history exists (Dhan's is frozen) -> UNAVAILABLE, as the live checklist reports it

    python replay_mornings.py --data DIR 2026-11-18 2026-11-19 2026-11-20 [--labels 0900 0909] [--evaluate-on 2026-11-21]
"""
import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def us_source(data: Path, d: date):
    import pandas as pd
    from tools.preopen_backtest import _read
    cutoff = pd.Timestamp(datetime(d.year, d.month, d.day, 9, 0, tzinfo=IST))
    out = {}
    for k, f in (("nasdaq", "nasdaq"), ("dow", "dow")):
        s = _read(data, f)
        s = s[s["d"] < d]
        if len(s) >= 2:
            out[k] = {"last": float(s["Close"].iloc[-1]), "prev": float(s["Close"].iloc[-2]), "as_of": str(s["d"].iloc[-1])}
    for k in ("nq", "ym"):
        h, dd = _read(data, f"{k}_h"), _read(data, f"{k}_d")
        done = h[h["ts"] + pd.Timedelta(hours=1) <= cutoff]
        st = dd[dd["d"] < d]
        if len(done) and len(st):
            out[k] = {"last": float(done["Close"].iloc[-1]), "prev": float(st["Close"].iloc[-1]),
                      "as_of": (done["ts"].iloc[-1] + pd.Timedelta(hours=1)).isoformat()}
    return lambda: out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("dates", nargs="+")
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--labels", nargs="+", default=["0900", "0909"])
    ap.add_argument("--evaluate-on", default=None)
    a = ap.parse_args(argv)
    from db.schema import get_connection
    from strategy import preopen_checklist as PC
    conn = get_connection()
    out = []
    try:
        for ds in a.dates:
            d = date.fromisoformat(ds)
            for lab in a.labels:
                now = datetime(d.year, d.month, d.day, 9, 0 if lab == "0900" else 9, 20 if lab == "0900" else 10, tzinfo=IST)
                if lab == "0909":
                    now = now.replace(minute=9)
                src = PC.Sources(us=us_source(a.data, d), dhan=lambda: {}, preopen=lambda: None, gift=lambda: None)
                res = PC.run(lab, now=now, conn=conn, sources=src, notify=False)
                row = PC.latest_run(conn, ds) if res.get("status") != "SKIPPED" else None
                out.append({"date": ds, "label": lab, "result": res,
                            "row": {k: (row or {}).get(k) for k in (
                                "run_label", "fetched_at_ist", "bias", "vote_sum", "votes_counted", "us_vote",
                                "nasdaq_chg_pct", "dow_chg_pct", "nq_chg_pct", "ym_chg_pct", "vix", "vix_regime",
                                "vix_status", "advances", "declines", "ad_basis", "ad_vote", "ad_status", "ad_source",
                                "gift_status", "expected_open", "caveats")}})
        if a.evaluate_on:
            out.append({"evaluate": PC.evaluate(conn, today=date.fromisoformat(a.evaluate_on))})
        out.append({"record": PC.record(conn)})
        ev = conn.execute("SELECT date, run_label, actual_open, actual_gap_pct, actual_open_class, nifty_close, day_chg_pct, "
                          "day_direction, evaluated_at_ist, eval_note FROM preopen_signals ORDER BY date, run_label").fetchall()
        out.append({"evaluated_rows": [list(r) for r in ev]})
    finally:
        conn.close()
    json.dump(out, sys.stdout, indent=2, default=str)


if __name__ == "__main__":
    main()
