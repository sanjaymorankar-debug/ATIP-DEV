"""
Validate the post-market RCA's alternative rules over many sessions, not one day (owner prompt 6 Nov 2026: "do NOT
overfit to one day -- validate over 20+ sessions").

For every scored day D in the range, the stocks auto-pick would have queued for D+1 (orders/autopick.py: BUY signals by
ATIP score, then rank-fill; the first --picks of them) are bought at the next session's first candle and replayed on
Dhan 5-minute candles with the sessions' rules (stop -2 %, from +3 % the stop trails 1 % under the high, held up to 15
sessions) and with each alternative rule of portfolio/post_market_rca.py. Every trade is Rs 25,000; charges from
backtest/costs.py. Read-only: Dhan candles are cached as CSV under --cache; nothing is written to the database.

    python tools/rca_validate_rules.py --from 2026-07-27 --to 2026-11-05 [--picks 10] [--db PATH] [--cache DIR]

Output: one line per rule (trades, win %, average net % a trade, total Rs, worst trade, profit factor, how many of the
days it beat today's rules) and --out JSON with every trade. Scores of past days are the stored ones (re-scored with
the 3 Nov formulas), so the picks are what auto-pick WOULD pick today, not necessarily what it did.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from portfolio.post_market_rca import simulate, BORDERLINE_SCORE, NIFTY_WEAK_PCT  # noqa: E402

CAPITAL = 25000.0
STOP, TARGET, TRAIL, HOLD = 2.0, 3.0, 1.0, 15
RULES = ["actual", "atr_1.0", "atr_1.5", "atr_2.0", "atr_1.5_risk", "stop_after_0945", "entry_after_0945", "orb_30",
         "revalidate_15m", "skip_gap_1", "skip_nifty_weak", "min_score_63", "breakeven_1r"]


def picks(conn, d: str, n: int) -> list:
    from orders.autopick import _fill_rows
    rows = [dict(r) for r in conn.execute(
        "SELECT a.symbol, a.atip_score AS score, a.`signal` AS `signal` FROM ai_scores a WHERE a.date = ? "
        "AND a.`signal` = 'BUY' ORDER BY a.atip_score DESC", (d,)).fetchall()]
    rows += _fill_rows(conn, d, {r["symbol"] for r in rows})
    return rows[:n]


def _bars_csv(path: Path) -> list:
    out = []
    for line in path.read_text().splitlines()[1:]:
        t, o, h, l, c = line.split(",")[:5]
        out.append({"t": datetime.fromisoformat(t[:19]), "o": float(o), "h": float(h), "l": float(l), "c": float(c)})
    return out


def candles(symbol: str, start: date, end: date, cache: Path, client, index_sid: str | None = None) -> list:
    """5-minute candles (IST) from Dhan, in <=85-day requests, cached as CSV."""
    f = cache / f"{symbol}_{start}_{end}_5m.csv"
    if f.exists():
        return _bars_csv(f)
    from data import dhan as D
    import pandas as pd
    parts, a = [], start
    while a <= end:
        b = min(end, a + timedelta(days=85))
        if index_sid:
            r = D._guarded(client.intraday_minute_data, security_id=index_sid, exchange_segment="IDX_I",
                           instrument_type="INDEX", from_date=str(a), to_date=str(b), interval=5)
            data = (r or {}).get("data") or {}
            df = pd.DataFrame({"datetime": D._ist_datetimes(data.get("timestamp", [])), "open": data.get("open", []),
                               "high": data.get("high", []), "low": data.get("low", []), "close": data.get("close", [])})
        else:
            df = D.fetch_historical_intraday(symbol, a, b, interval=5, dhan=client)
        if len(df):
            parts.append(df[["datetime", "open", "high", "low", "close"]])
        a = b + timedelta(days=1)
    if not parts:
        return []
    df = pd.concat(parts).drop_duplicates("datetime").sort_values("datetime")
    df = df[df["datetime"].astype(str).str[11:16].between("09:15", "15:29")]
    df.to_csv(f, index=False)
    return _bars_csv(f)


def _net_pct(entry, exit_, model) -> float:
    q = int(CAPITAL // entry) or 1
    gross = (exit_ - entry) * q
    return (gross - model.total("BUY", entry * q) - model.total("SELL", exit_ * q)) / (entry * q) * 100


def replay(p: dict, bars: list, day_bars: list, prev_close: float, atr: float | None, nifty_day: list,
           nifty_prev: float | None, model) -> dict:
    """{rule: net % of the trade or None when the rule would not buy}."""
    out = {}
    if not day_bars:
        return out
    entry, t0 = day_bars[0]["o"], day_bars[0]["t"]
    after = [b for b in bars if b["t"] >= t0]

    def go(en=entry, st=t0, stop_pct=STOP, stop=None, **kw):
        r = simulate(after, en, 1, stop if stop is not None else en * (1 - stop_pct / 100), TARGET, TRAIL, st, **kw)
        return _net_pct(en, r["exit"], model)

    out["actual"] = go()
    for k in (1.0, 1.5, 2.0):
        out[f"atr_{k}"] = go(stop=entry - k * atr) if atr else out["actual"]
    out["atr_1.5_risk"] = (go(stop=entry - 1.5 * atr) * (entry * STOP / 100) / (1.5 * atr)) if atr else out["actual"]
    out["stop_after_0945"] = go(stop_from="09:45")
    out["breakeven_1r"] = go(breakeven_r=STOP)
    late = next((b for b in day_bars if b["t"].strftime("%H:%M") >= "09:45" and b["o"] <= entry * 1.005), None)
    out["entry_after_0945"] = go(en=late["o"], st=late["t"]) if late else None
    rng = [b for b in day_bars if b["t"].strftime("%H:%M") < "09:45"]
    orh = max((b["h"] for b in rng), default=None)
    brk = next((b for b in day_bars if b["t"].strftime("%H:%M") >= "09:45" and orh and b["h"] > orh), None)
    out["orb_30"] = go(en=max(orh, brk["o"]), st=brk["t"]) if brk else None
    first = [b for b in day_bars if b["t"].strftime("%H:%M") < "09:30"]
    at930 = next((b for b in day_bars if b["t"].strftime("%H:%M") >= "09:30"), None)
    ok = first and at930 and first[-1]["c"] >= first[0]["o"] and prev_close and at930["o"] >= prev_close
    out["revalidate_15m"] = go(en=at930["o"], st=at930["t"]) if ok else None
    out["skip_gap_1"] = None if prev_close and (entry - prev_close) / prev_close * 100 < -1.0 else out["actual"]
    nb = [b for b in nifty_day if b["t"] <= t0]
    weak = bool(nifty_day and nb and (nb[-1]["c"] < nifty_day[0]["o"] or
                                      (nifty_prev and (nb[-1]["c"] - nifty_prev) / nifty_prev * 100 <= NIFTY_WEAK_PCT)))
    out["skip_nifty_weak"] = None if weak else out["actual"]
    out["min_score_63"] = out["actual"] if (p.get("score") or 0) >= BORDERLINE_SCORE else None
    return out


def summarise(trades: list) -> list:
    rows = []
    days = sorted({t["day"] for t in trades})
    for r in RULES:
        xs = [t["rules"].get(r) for t in trades if t["rules"].get(r) is not None]
        base_by_day, rule_by_day = {}, {}
        for t in trades:
            base_by_day[t["day"]] = base_by_day.get(t["day"], 0.0) + t["rules"]["actual"]
            rule_by_day[t["day"]] = rule_by_day.get(t["day"], 0.0) + (t["rules"].get(r) or 0.0)
        wins = [x for x in xs if x > 0]
        loss = [x for x in xs if x <= 0]
        rows.append({"rule": r, "trades": len(xs), "win_pct": round(len(wins) / len(xs) * 100, 1) if xs else None,
                     "avg_net_pct": round(sum(xs) / len(xs), 3) if xs else None,
                     "total_rs": round(sum(xs) * CAPITAL / 100, 0), "worst_pct": round(min(xs), 2) if xs else None,
                     "profit_factor": round(sum(wins) / -sum(loss), 2) if loss and sum(loss) else None,
                     "days_better": sum(1 for d in days if rule_by_day[d] > base_by_day[d] + 1e-9),
                     "days_worse": sum(1 for d in days if rule_by_day[d] < base_by_day[d] - 1e-9), "days": len(days)})
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--picks", type=int, default=10)
    ap.add_argument("--db", default=str(ROOT / "atip_data" / "atip.db"))
    ap.add_argument("--cache", default=str(ROOT / "atip_data" / "rca_cache"))
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    cache = Path(a.cache)
    cache.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    from backtest.costs import cost_model_from_config
    from data import dhan as D
    model = cost_model_from_config()
    client, _ = D.get_dhan_client()
    score_days = [r[0] for r in conn.execute("SELECT DISTINCT date FROM ai_scores WHERE date BETWEEN ? AND ? ORDER BY date",
                                             (a.start, a.end))]
    trade_days = [r[0] for r in conn.execute("SELECT DISTINCT date FROM prices_daily WHERE symbol = 'NIFTY50' AND date > ? "
                                             "ORDER BY date", (a.start,))]
    nxt = lambda d: next((x for x in trade_days if x > d), None)
    plan = [(d, nxt(d), p) for d in score_days if nxt(d) for p in picks(conn, d, a.picks)]
    end = date.fromisoformat(max(x[1] for x in plan)) + timedelta(days=30)
    end = min(end, date.today())
    start = date.fromisoformat(min(x[1] for x in plan))
    nifty = candles("NIFTY50", start, end, cache, client, index_sid="13")
    trades = []
    for d, nd, p in plan:
        bars = candles(p["symbol"], start, end, cache, client)
        day_bars = [b for b in bars if str(b["t"])[:10] == nd]
        if not day_bars:
            continue
        horizon = (trade_days + [str(end)])[min(len(trade_days), trade_days.index(nd) + HOLD) if nd in trade_days else -1]
        window = [b for b in bars if nd <= str(b["t"])[:10] <= horizon]
        pc = conn.execute("SELECT close FROM prices_daily WHERE symbol = ? AND date < ? AND series = 'EQ' ORDER BY date DESC "
                          "LIMIT 1", (p["symbol"], nd)).fetchone()
        atr = conn.execute("SELECT atr_14 FROM technical_indicators WHERE symbol = ? AND date = ?", (p["symbol"], d)).fetchone()
        npc = conn.execute("SELECT close FROM prices_daily WHERE symbol = 'NIFTY50' AND date < ? ORDER BY date DESC LIMIT 1",
                           (nd,)).fetchone()
        r = replay(p, window, day_bars, pc[0] if pc else None, atr[0] if atr else None,
                   [b for b in nifty if str(b["t"])[:10] == nd], npc[0] if npc else None, model)
        if r:
            trades.append({"day": nd, "signal_day": d, "symbol": p["symbol"], "score": p["score"],
                           "source": p.get("source", "BUY"), "rules": {k: (round(v, 3) if v is not None else None)
                                                                         for k, v in r.items()}})
    rows = summarise(trades)
    print(f"{len(trades)} trades on {len({t['day'] for t in trades})} sessions ({a.start} .. {a.end} signals), "
          f"Rs {CAPITAL:,.0f} each, 5-minute Dhan candles, IST")
    print(f"{'rule':18s} {'trades':>6s} {'win%':>6s} {'avg%':>7s} {'total Rs':>10s} {'worst%':>7s} {'PF':>5s} {'better/worse days':>18s}")
    for r in rows:
        print(f"{r['rule']:18s} {r['trades']:>6d} {r['win_pct'] or 0:>6.1f} {r['avg_net_pct'] or 0:>7.3f} "
              f"{r['total_rs']:>10,.0f} {r['worst_pct'] or 0:>7.2f} {r['profit_factor'] or 0:>5.2f} "
              f"{r['days_better']:>8d}/{r['days_worse']:<3d} of {r['days']}")
    if a.out:
        Path(a.out).write_text(json.dumps({"rows": rows, "trades": trades}, indent=1))


if __name__ == "__main__":
    main()
