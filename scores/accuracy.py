"""
ATIP — Accuracy Tracker: prediction vs actual 5/10/20-day outcomes

Reads predictions (written by scores/predictions.py) and measures what
actually happened, so ACS.HistoricalAccuracy and any strategy review rest on
recorded outcomes rather than assumption.

FIXES vs the original version of this module (all were silently wrong):
  1. hit_target_1 / hit_stop_loss only compared the CLOSE on the single day
     N trading days after entry. A stock that touched +3% on day 2 and fell
     back was recorded as "never hit target" — precisely backwards for a
     take-profit-at-3% strategy, where an intraday touch is the exit. Now the
     whole holding window is scanned on intraday high/low.
  2. It only looked for predictions dated exactly N trading days before the
     run date, so a missed run left that horizon permanently unfilled with
     nothing to backfill it. Now it finds every prediction whose horizon has
     matured and is still unmeasured, and fills it whenever it next runs.
  3. Each horizon's INSERT overwrote hit_target_1 / hit_stop_loss from the
     other horizons, so the stored value reflected whichever ran last. Those
     are now resolved per-horizon and written to their own columns.
  4. _trading_days_back skipped weekends but not NSE holidays, drifting the
     target date onto non-trading days. It now walks the real calendar via
     utils/trading_calendar.py.
  5. generate_report interpolated `symbol` straight into SQL. Parameterised.

Same-day ambiguity: when one daily bar's range covers both the stop and the
target, daily data cannot say which came first, so the stop is recorded
(pessimistic) — matching scores/backtest.py so the two agree.
"""
import argparse
import logging
from datetime import date, datetime, timedelta

from db.schema import get_connection, log_job

log = logging.getLogger(__name__)

HORIZONS = (5, 10, 20)


def _trading_days_forward(from_date, n):
    """The date n NSE trading days after from_date (holiday-aware)."""
    try:
        from utils.trading_calendar import is_trading_day
    except Exception:
        def is_trading_day(d):  # noqa: E306 — degrade to weekday-only if unavailable
            return d.weekday() < 5
    d, count = from_date, 0
    while count < n:
        d += timedelta(days=1)
        if is_trading_day(d):
            count += 1
    return d


def _resolve_window(conn, symbol, pred_date, horizon, signal, target_1, stop_loss):
    """
    Walk the bars from pred_date (exclusive) forward `horizon` trading bars and
    report what happened: closing price/return at the end of the window, plus
    whether target/stop were touched intraday at any point inside it.

    Returns None when the window hasn't fully elapsed yet, so an immature
    prediction is left alone rather than measured against partial data.
    """
    bars = conn.execute(
        "SELECT date,high,low,close FROM prices_daily "
        "WHERE symbol=? AND date>? ORDER BY date LIMIT ?",
        (symbol, pred_date, horizon),
    ).fetchall()
    if len(bars) < horizon:
        return None

    hit_target = hit_stop = 0
    for b in bars:
        if signal == "SELL":
            t_touch = bool(target_1 and b["low"] <= target_1)
            s_touch = bool(stop_loss and b["high"] >= stop_loss)
        else:
            t_touch = bool(target_1 and b["high"] >= target_1)
            s_touch = bool(stop_loss and b["low"] <= stop_loss)
        if t_touch and s_touch:
            hit_stop = 1          # pessimistic on an ambiguous bar
            break
        if s_touch:
            hit_stop = 1
            break
        if t_touch:
            hit_target = 1
            break
    return {"close": bars[-1]["close"], "hit_target": hit_target, "hit_stop": hit_stop}


def update_accuracy(target_date=None) -> int:
    """
    Measure every prediction whose horizon has matured and isn't recorded yet.
    Idempotent and self-backfilling — safe to run daily or weekly.
    """
    if target_date is None:
        target_date = date.today()
    conn = get_connection()
    updated = 0
    try:
        for horizon in HORIZONS:
            col_price, col_ret, col_ok = f"price_{horizon}d", f"return_{horizon}d", f"correct_{horizon}d"
            # Predictions old enough for this horizon to have elapsed, that
            # nobody has measured for this horizon yet.
            cutoff = str(target_date - timedelta(days=int(horizon * 1.6) + 3))
            preds = conn.execute(f"""
                SELECT p.pred_date, p.symbol, p.signal, p.entry_price, p.stop_loss, p.target_1
                FROM predictions p
                LEFT JOIN accuracy_tracker a
                       ON a.pred_date=p.pred_date AND a.symbol=p.symbol
                WHERE p.entry_price IS NOT NULL
                  AND p.pred_date <= ?
                  AND (a.{col_price} IS NULL)
                ORDER BY p.pred_date
            """, (cutoff,)).fetchall()

            for p in preds:
                res = _resolve_window(conn, p["symbol"], p["pred_date"], horizon,
                                      p["signal"], p["target_1"], p["stop_loss"])
                if not res:
                    continue
                entry = p["entry_price"]
                ret = round((res["close"] - entry) / entry * 100, 3)
                correct = int(
                    (ret > 0 and p["signal"] == "BUY")
                    or (ret < 0 and p["signal"] == "SELL")
                    or (abs(ret) < 2 and p["signal"] in ("HOLD", "WAIT"))
                )
                conn.execute(f"""
                    INSERT INTO accuracy_tracker
                        (pred_date, symbol, signal, entry_price,
                         {col_price}, {col_ret}, {col_ok}, hit_target_1, hit_stop_loss)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(pred_date,symbol) DO UPDATE SET
                        {col_price}=excluded.{col_price},
                        {col_ret}=excluded.{col_ret},
                        {col_ok}=excluded.{col_ok},
                        -- sticky: a target/stop touched in ANY measured window
                        -- stays recorded, instead of being reset by a later
                        -- horizon that resolved differently.
                        hit_target_1=MAX(COALESCE(accuracy_tracker.hit_target_1,0),excluded.hit_target_1),
                        hit_stop_loss=MAX(COALESCE(accuracy_tracker.hit_stop_loss,0),excluded.hit_stop_loss)
                """, (p["pred_date"], p["symbol"], p["signal"], entry,
                      res["close"], ret, correct, res["hit_target"], res["hit_stop"]))
                updated += 1

        conn.commit()
        log.info(f"  ✓ Accuracy: {updated} outcomes measured")
        log_job("accuracy", "SUCCESS", updated)
    except Exception as e:
        conn.rollback()
        log.error(f"  ✗ {e}")
        log_job("accuracy", "FAILED", 0, error=e)
    finally:
        conn.close()
    return updated


def generate_report(symbol=None, days_back=90) -> dict:
    conn = get_connection()
    since = str(date.today() - timedelta(days=days_back))
    sql = """SELECT a.symbol,a.signal,a.return_5d,a.correct_5d,a.return_10d,a.correct_10d,
                    a.return_20d,a.correct_20d,a.hit_target_1,a.hit_stop_loss
             FROM accuracy_tracker a WHERE a.pred_date>=?"""
    params = [since]
    if symbol:
        sql += " AND a.symbol=?"
        params.append(symbol.upper())
    try:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()
    if not rows:
        return {"error": "No accuracy data yet — run predictions then `--update`.",
                "symbol": symbol or "ALL", "period_days": days_back, "total": 0}

    rep = {"total": len(rows), "period_days": days_back, "symbol": symbol or "ALL"}
    for h in HORIZONS:
        vals = [r for r in rows if r.get(f"correct_{h}d") is not None]
        if not vals:
            continue
        rets = [r[f"return_{h}d"] for r in vals if r.get(f"return_{h}d") is not None]
        rep[f"n_{h}d"] = len(vals)
        rep[f"accuracy_{h}d"] = round(sum(r[f"correct_{h}d"] for r in vals) / len(vals) * 100, 1)
        if rets:
            rep[f"avg_return_{h}d"] = round(sum(rets) / len(rets), 2)
            rep[f"win_rate_{h}d"] = round(sum(1 for x in rets if x > 0) / len(rets) * 100, 1)
    tgt = [r for r in rows if r.get("hit_target_1") is not None]
    if tgt:
        rep["target_hit_rate"] = round(sum(r["hit_target_1"] for r in tgt) / len(tgt) * 100, 1)
        rep["stop_hit_rate"] = round(sum(r["hit_stop_loss"] or 0 for r in tgt) / len(tgt) * 100, 1)
    return rep


def print_report(symbol=None):
    rep = generate_report(symbol)
    print(f"\n{'='*56}\n  ATIP Accuracy — {rep.get('symbol')} (last {rep.get('period_days')}d)"
          f"\n  {rep.get('total',0)} measured predictions\n{'='*56}")
    if rep.get("error"):
        print(f"  {rep['error']}\n")
        return
    for h in HORIZONS:
        acc = rep.get(f"accuracy_{h}d")
        if acc is None:
            continue
        mark = "✅" if acc >= 55 else "⚠️" if acc >= 45 else "❌"
        print(f"  {h:2d}d: {mark} Direction {acc:.1f}%   Avg {rep.get(f'avg_return_{h}d',0):+.2f}%   "
              f"WinRate {rep.get(f'win_rate_{h}d',0):.1f}%   (n={rep.get(f'n_{h}d',0)})")
    if "target_hit_rate" in rep:
        print(f"\n  Target_1 touched: {rep['target_hit_rate']:.1f}%   "
              f"Stop touched: {rep['stop_hit_rate']:.1f}%")
    print()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true", help="measure matured predictions")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--symbol")
    ap.add_argument("--date", help="treat this date as 'today' (YYYY-MM-DD)")
    args = ap.parse_args()
    td = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else None
    if args.update:
        print(f"Measured {update_accuracy(td)} outcomes")
    else:
        print_report(args.symbol)
