"""
V2 invariance checks on COPIES of the database (QA pass, 4 Nov 2026). Each scoring run is the production
scores.engine.run_scoring_pipeline(D) in its own process, pointed at a copy by ATIP_DB_PATH.

  look-ahead      copy B has every row dated after D replaced by garbage (prices x random 0.3-3, indicators, news,
                  market health, FII/DII, accuracy outcomes, bulk deals). Scores and signals for D must equal copy A's.
                  The indicator engine is checked the same way (run_technical_pipeline(D) on a sample).
  normalisation   copy C has every row after D AND every price older than D - 450 days deleted (beyond the
                  260-bar / 90-day windows). Scores for D must equal copy A's: nothing is normalised over history.
  NULL EMA        on copy A, one symbol's ema_9 / ema_21 are set NULL for D and D is re-scored: MRI must leave the
                  EMA component out (not score it 0 or 30), and equal the weighted mean of what is left.
  composite       ATIP / ACS / component weight sums; every stored component of D is on the 0-100 scale.

    python tests/verification/v2_invariance.py --src <snapshot.db> --date 2026-10-29 --work <dir> \
        --raw /Users/agtci/ATIP/atip_data/raw --code <repo> --json out.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

SC = ("vpi", "mri", "rri", "zpi", "cri", "acs", "atip_score", "signal")


def run_in(db: Path, work: Path, code: str, py: str) -> str:
    env = dict(os.environ, ATIP_DB_PATH=str(db), ATIP_OPERATING_SCHEDULE="off", PYTHONPATH=code)
    r = subprocess.run([sys.executable, "-c", py], cwd=work, env=env, capture_output=True, text=True, timeout=900)
    if r.returncode:
        raise RuntimeError(r.stderr[-2000:])
    return r.stdout


def score(db, d, work, code):
    return run_in(db, work, code, f"from scores.engine import run_scoring_pipeline as r; from datetime import date; "
                                  f"print(r(date.fromisoformat('{d}')))")


def technical(db, d, syms, work, code):
    return run_in(db, work, code, f"from data.technical import run_technical_pipeline as r; from datetime import date; "
                                  f"print(r(date.fromisoformat('{d}'), symbols={syms!r}))")


def rows(db, d):
    c = sqlite3.connect(db)
    out = {r[0]: r[1:] for r in c.execute(f"SELECT symbol, {', '.join('`signal`' if k == 'signal' else k for k in SC)} "
                                          "FROM ai_scores WHERE date=?", (d,))}
    c.close()
    return out


def diff(a: dict, b: dict) -> dict:
    keys = set(a) | set(b)
    bad = []
    for k in sorted(keys):
        if a.get(k) != b.get(k):
            bad.append({"symbol": k, "a": a.get(k), "b": b.get(k)})
    return {"symbols": len(keys), "differ": len(bad), "examples": bad[:5]}


def corrupt_after(db, d, seed=7):
    rnd = random.Random(seed)
    c = sqlite3.connect(db)
    c.create_function("junk", 0, lambda: rnd.uniform(0.3, 3.0))
    c.execute("UPDATE prices_daily SET open=open*junk(), high=high*junk(), low=low*junk(), close=close*junk(), "
              "volume=volume*junk(), delivery_pct=delivery_pct*junk() WHERE date > ?", (d,))
    c.execute("UPDATE technical_indicators SET rsi_14=rsi_14*junk(), adx_14=adx_14*junk(), atr_pct=atr_pct*junk(), "
              "ema_9=ema_9*junk(), ema_21=ema_21*junk(), sma_200=sma_200*junk(), volume_ratio=volume_ratio*junk(), "
              "macd_hist_pct=macd_hist_pct*junk(), tech_score=tech_score*junk() WHERE date > ?", (d,))
    for sql in ("UPDATE news_symbol_score SET score = score * junk() WHERE date > ?",
                "UPDATE market_health SET mh_score = mh_score * junk(), vix_level = vix_level * junk() WHERE date > ?",
                "UPDATE fii_dii_market SET fii_5d_avg = fii_5d_avg * junk() * 9, dii_5d_avg = dii_5d_avg * junk() * 9 WHERE date > ?",
                "UPDATE accuracy_tracker SET correct_5d = 1 - COALESCE(correct_5d, 0) WHERE pred_date > ?",
                "UPDATE bulk_deals SET net_value_cr = net_value_cr * junk() * 50 WHERE date > ?",
                "UPDATE index_levels SET india_vix = india_vix * junk() WHERE date > ?",
                "UPDATE news_articles SET news_score = news_score * junk() WHERE fetched_at >= date(?, '+1 day')",
                "UPDATE global_markets SET global_score = global_score * junk() WHERE date > ?"):
        try:
            c.execute(sql, (d,))
        except sqlite3.OperationalError as e:
            print("  skip:", e)
    c.commit()
    c.close()


def trim(db, d, keep_days=450):
    floor = str(date.fromisoformat(d) - timedelta(days=keep_days))
    c = sqlite3.connect(db)
    for t, col in (("prices_daily", "date"), ("technical_indicators", "date"), ("news_symbol_score", "date"),
                   ("market_health", "date"), ("fii_dii_market", "date"), ("accuracy_tracker", "pred_date"),
                   ("bulk_deals", "date"), ("index_levels", "date"), ("ai_scores", "date"), ("score_components", "date")):
        try:
            c.execute(f"DELETE FROM {t} WHERE {col} > ?", (d,))
        except sqlite3.OperationalError as e:
            print("  skip:", e)
    c.execute("DELETE FROM prices_daily WHERE date < ?", (floor,))
    c.commit()
    c.close()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--raw", required=True, help="a folder holding ind_nifty500list.csv and dhan/security_id_list.csv")
    ap.add_argument("--code", required=True)
    ap.add_argument("--null-ema-symbol", default="RELIANCE")
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    d, work = a.date, Path(a.work)
    (work / "atip_data" / "raw" / "dhan").mkdir(parents=True, exist_ok=True)
    for f in ("ind_nifty500list.csv", "dhan/security_id_list.csv"):         # read-only copies: no network fetch
        if (Path(a.raw) / f).exists():
            shutil.copyfile(Path(a.raw) / f, work / "atip_data" / "raw" / f)
    A, B, C = work / "inv_A.db", work / "inv_B.db", work / "inv_C.db"
    for x in (A, B, C):
        shutil.copyfile(a.src, x)
    out = {"date": d}
    corrupt_after(B, d)
    trim(C, d)
    sample = ["RELIANCE", "HDFCBANK", "LAURUSLABS", "REDINGTON", "VEDPOWER", "TDPOWERSYS", "INFY", "TATASTEEL"]
    technical(A, d, sample, work, a.code)
    technical(B, d, sample, work, a.code)
    technical(C, d, sample, work, a.code)          # every copy's sample indicators are recomputed alike
    ca, cb = sqlite3.connect(A), sqlite3.connect(B)
    q = "SELECT symbol, rsi_14, adx_14, atr_pct, ema_9, ema_21, sma_200, volume_ratio, macd_hist_pct, tech_score FROM " \
        "technical_indicators WHERE date=? AND symbol IN (%s) ORDER BY symbol" % ",".join("?" * len(sample))
    ta_, tb_ = ca.execute(q, (d, *sample)).fetchall(), cb.execute(q, (d, *sample)).fetchall()
    ca.close(); cb.close()
    out["lookahead_indicators"] = {"rows": len(ta_), "identical": ta_ == tb_}
    for x in (A, B, C):
        score(x, d, work, a.code)
    ra, rb, rc = rows(A, d), rows(B, d), rows(C, d)
    out["lookahead_scores"] = diff(ra, rb)
    out["normalisation_history_removed"] = diff(ra, rc)
    # NULL EMA
    s = a.null_ema_symbol
    c = sqlite3.connect(A)
    c.execute("UPDATE technical_indicators SET ema_9=NULL, ema_21=NULL WHERE symbol=? AND date=?", (s, d))
    c.execute("DELETE FROM score_components WHERE symbol=? AND date=?", (s, d))
    c.commit(); c.close()
    score(A, d, work, a.code)
    c = sqlite3.connect(A)
    comp = {r[0]: (r[1], r[2]) for r in c.execute("SELECT component, value, weight FROM score_components WHERE symbol=? AND date=? "
                                                  "AND index_name='MRI'", (s, d))}
    mri = c.execute("SELECT mri FROM ai_scores WHERE symbol=? AND date=?", (s, d)).fetchone()[0]
    W = {r[0]: r[1] for r in c.execute("SELECT variable, weight FROM weight_config WHERE index_name='MRI' AND active=1")}
    wsum = sum(W[k] for k in comp)
    rebuilt = round(sum(v * W[k] for k, (v, _) in comp.items()) / wsum, 2)
    as_zero = round((sum(v * W[k] for k, (v, _) in comp.items()) + 0 * W["EMA"]) / (wsum + W["EMA"]), 2)
    out["null_ema"] = {"symbol": s, "mri_components": sorted(comp), "ema_left_out": "EMA" not in comp, "mri": mri,
                       "mri_rebuilt_from_present": rebuilt, "matches": abs(mri - rebuilt) <= 0.01,
                       "mri_if_ema_were_0": as_zero, "mri_before_null": ra.get(s, [None, None])[1]}
    # composite
    ws = {}
    for r in c.execute("SELECT index_name, SUM(weight) FROM weight_config WHERE active=1 GROUP BY index_name"):
        ws[r[0]] = round(r[1], 4)
    rng = c.execute("SELECT MIN(value), MAX(value), COUNT(*) FROM score_components WHERE date=? AND symbol != '__MARKET__'",
                    (d,)).fetchone()
    present = c.execute("SELECT AVG(s) FROM (SELECT symbol, SUM(weight) s FROM score_components WHERE date=? AND index_name='ATIP' "
                        "GROUP BY symbol)", (d,)).fetchone()[0]
    c.close()
    out["composite"] = {"weight_sums": ws, "component_min": rng[0], "component_max": rng[1], "components": rng[2],
                        "on_0_100_scale": rng[0] >= 0 and rng[1] <= 100, "atip_weight_present_avg": round(present or 0, 3)}
    print(json.dumps(out, indent=1, default=str))
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    for x in (A, B, C):
        for ext in ("", "-wal", "-shm"):
            Path(str(x) + ext).unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
