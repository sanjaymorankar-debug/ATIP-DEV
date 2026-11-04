"""
V1 verification harness (4 Nov 2026 QA pass): run the real ten-session allocator point-in-time on a COPY of the
database and check its picks against an independent ranking.

For a score day D the copy is cut back to what was known at D's close (ai_scores and prices_daily after D deleted,
live quotes dropped), every session's pool is restarted empty (no holdings, no queued buys, full cash), and
orders.autopick.run() is called for the next session, exactly as the dashboard's order monitor calls it. Then:

  * picks      : how many PICKED, how many distinct symbols
  * SQL        : ENTRY rules linked to a session, created by this run, and their distinct symbols
  * board      : orders.session_board.board() -> filled / message / 10 rows
  * independent: the rank-fill rule recomputed here from ai_scores + prices_daily with pandas (no orders/ import):
                 BUY first by ATIP; then HOLD with CRI < 75 (CRI lower is better), close >= Rs 20, 20-day average
                 close x volume >= Rs 2 crore; ATIP desc, CRI asc (NULL last), symbol; a stock one session's cash
                 cannot buy is skipped. The first ten must equal the allocator's picks, in session order.
  * momentum   : the top ten by plain 20-day price momentum, for comparison (the allocator ranks by ATIP).

Shortfall mode (--min-turnover / --min-price) tightens the rank-fill filters on the copy only.

Never point it at the live database: it writes. It refuses a path that ends in atip_data/atip.db.

    PYTHONPATH=<repo> python tests/verification/v1_sessions_harness.py --src <snapshot.db> --date 2026-11-04 \
        --work <scratch dir> [--min-turnover 5e9] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd


def next_weekday(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def prepare_copy(src: Path, dst: Path, d: str, pool_start: str) -> None:
    if str(src).endswith("atip_data/atip.db") or str(dst).endswith("atip_data/atip.db"):
        raise SystemExit("refusing to touch a live atip_data/atip.db")
    shutil.copyfile(src, dst)
    c = sqlite3.connect(dst)
    c.execute("DELETE FROM ai_scores WHERE date > ?", (d,))
    c.execute("DELETE FROM prices_daily WHERE date > ?", (d,))
    c.execute("DELETE FROM order_rules WHERE substr(created_at, 1, 10) > ?", (d,))     # nothing traded after D yet
    for t in ("live_quotes", "session_rules", "paper_position"):
        try:
            c.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass
    # every pool starts afresh: its books count only rules linked after pool_started_at, and none are linked
    c.execute("UPDATE trading_sessions SET pool_started_at = ?, status = 'RUNNING', mode = 'AUTO', auto_pick = 1 "
              "WHERE start_capital IS NOT NULL", (pool_start,))
    c.commit()
    c.close()


def independent_ranking(db: Path, d: str, min_price=20.0, min_turnover=2e7, cri_danger=75.0, cash=None) -> pd.DataFrame:
    c = sqlite3.connect(db)
    s = pd.read_sql("SELECT symbol, atip_score, cri, `signal` AS sig FROM ai_scores WHERE date = ?", c, params=(d,))
    p = pd.read_sql("SELECT symbol, date, close, volume FROM prices_daily WHERE series = 'EQ' AND date <= ? "
                    "AND date >= date(?, '-60 days')", c, params=(d, d))
    c.close()
    p = p.sort_values(["symbol", "date"])
    last20 = p.groupby("symbol").tail(20)
    turnover = (last20["close"] * last20["volume"]).groupby(last20["symbol"]).mean().rename("turnover")
    close_d = p[p["date"] == d].set_index("symbol")["close"].rename("close")
    c = sqlite3.connect(db)                     # a BUY is priced off any series' close (the allocator's latest_close)
    any_close = pd.read_sql("SELECT symbol, close AS any_close FROM prices_daily WHERE date = ?", c, params=(d,))
    c.close()
    s = s.join(close_d, on="symbol").join(turnover, on="symbol").merge(any_close, on="symbol", how="left")
    s = s[s["atip_score"].notna()]
    buy = s[s["sig"] == "BUY"].sort_values("atip_score", ascending=False).assign(source="BUY")
    buy = buy.assign(close=buy["any_close"])
    hold = s[(s["sig"] == "HOLD") & ~(s["cri"] >= cri_danger) & (s["close"] >= min_price) & (s["turnover"] >= min_turnover)]
    hold = hold.assign(cri_null=hold["cri"].isna()).sort_values(["atip_score", "cri_null", "cri", "symbol"],
                                                                  ascending=[False, True, True, True])
    out = pd.concat([buy, hold.drop(columns="cri_null").assign(source="RANK-FILL")])
    if cash is not None:
        out = out[out["close"] * 1.001 < cash]          # one share plus a little slippage must fit the session's cash
    return out.reset_index(drop=True)


def momentum_top(db: Path, d: str, n=10) -> list:
    c = sqlite3.connect(db)
    p = pd.read_sql("SELECT symbol, date, close FROM prices_daily WHERE series='EQ' AND date <= ? AND date >= date(?, '-45 days')",
                    c, params=(d, d))
    tracked = set(pd.read_sql("SELECT symbol FROM ai_scores WHERE date = ?", c, params=(d,))["symbol"])
    c.close()
    w = p.pivot(index="date", columns="symbol", values="close").sort_index()
    if len(w) < 21:
        return []
    m = (w.iloc[-1] / w.iloc[-21] - 1).dropna()
    m = m[m.index.isin(tracked)]
    return list(m.sort_values(ascending=False).head(n).index)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--date", required=True, help="score day D (the allocator picks for the next session)")
    ap.add_argument("--work", required=True)
    ap.add_argument("--min-turnover", type=float, default=None)
    ap.add_argument("--min-price", type=float, default=None)
    ap.add_argument("--rank-fill-off", action="store_true")
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    work = Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    tag = a.date + ("_short" if (a.min_turnover or a.min_price or a.rank_fill_off) else "")
    db = work / f"v1_{tag}.db"
    run_day = next_weekday(date.fromisoformat(a.date))
    prepare_copy(Path(a.src), db, a.date, f"{a.date}T23:00:00")
    os.environ["ATIP_DB_PATH"] = str(db)
    os.environ["ATIP_OPERATING_SCHEDULE"] = "off"
    os.chdir(work)                                  # no atip_data/config.json here: PAPER, no halt flag, no keys

    from db.schema import get_connection
    from orders import autopick, session_board, sessions
    if a.min_turnover is not None:
        autopick.FILL_MIN_TURNOVER = a.min_turnover
    if a.min_price is not None:
        autopick.FILL_MIN_PRICE = a.min_price
    conn = get_connection()
    if a.rank_fill_off:
        conn.execute("INSERT OR REPLACE INTO session_settings (name, value, updated_at) VALUES (?, 'OFF', ?)",
                     (autopick.FILL_SETTING, f"{a.date}T23:00:00"))
        conn.commit()
    cash = {s["id"]: (s.get("pool") or {}).get("cash") for s in sessions.list_sessions(conn)["sessions"]}
    before = {r[0] for r in conn.execute("SELECT id FROM order_rules")}
    events = autopick.run(conn, dry_run=False, today=run_day)
    picked = [e for e in events if e["type"] == "PICKED"]
    new = [dict(r) for r in conn.execute(
        "SELECT r.id, r.symbol, r.role, r.status, s.session_id FROM order_rules r JOIN session_rules s ON s.rule_id = r.id "
        "WHERE (r.role IS NULL OR r.role = 'ENTRY')").fetchall() if r["id"] not in before]
    b = session_board.board(conn, today=run_day)
    conn.close()

    ind = independent_ranking(db, a.date, min_price=a.min_price or 20.0, min_turnover=a.min_turnover or 2e7,
                              cash=min(v for v in cash.values() if v) if any(cash.values()) else None)
    if a.rank_fill_off:
        ind = ind[ind["source"] == "BUY"]
    expect = list(ind["symbol"].head(10))
    got = [e["symbol"] for e in sorted(picked, key=lambda e: int(e["session"].lstrip("S")))]
    mom = momentum_top(db, a.date)
    res = {"score_day": a.date, "run_day": str(run_day), "filters": {"min_turnover": a.min_turnover, "min_price": a.min_price,
                                                                      "rank_fill_off": a.rank_fill_off},
           "picked": len(picked), "distinct_picked": len(set(got)), "picks_in_session_order": got,
           "waiting": [{"session": e["session"], "reason": e.get("reason")} for e in events if e["type"] == "WAITING"],
           "sql_entry_rules": len(new), "sql_distinct_symbols": len({r["symbol"] for r in new}),
           "board_filled": b["filled"], "board_rows": len(b["rows"]), "board_message": b["message"],
           "board_rows_detail": [{k: r.get(k) for k in ("session", "symbol", "status", "rank", "rank_of", "rationale", "entry",
                                                        "target", "stoploss", "pnl", "closed_trades", "successful_trades")}
                                 | {"scores": r.get("score_snapshot")} for r in b["rows"]],
           "independent_top10": expect, "independent_eligible": int(len(ind)),
           "order_matches_independent": got == expect[:len(got)] and len(got) == min(10, len(expect)),
           "momentum20_top10": mom, "overlap_with_momentum20": sorted(set(got) & set(mom))}
    print(json.dumps({k: v for k, v in res.items() if k != "board_rows_detail"}, indent=1, default=str))
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    Path(db).unlink(missing_ok=True)
    for ext in ("-wal", "-shm"):
        Path(str(db) + ext).unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
