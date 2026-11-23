"""
Test server only: a scripted morning for today through the real checklist code, every source a clearly labelled
SYNTHETIC test feed (source names say so), so the screens can be checked with all four checks populated. Stores a GIFT
15:31 reference for the previous session first, so the basis-adjusted GIFT move is exercised. Never run on production.
"""
import json
import sys
from datetime import timedelta

from strategy import preopen_checklist as PC
from db.schema import get_connection


def main():
    now0 = PC.now_ist().replace(hour=9, minute=0, second=21, microsecond=0)
    prev = PC.prev_session(now0.date())
    conn = get_connection()
    try:
        from research.global_sync import ensure_tables
        ensure_tables(conn)
        conn.execute("INSERT OR REPLACE INTO global_snapshot (date, label, series, price, quote_ts, captured_at) VALUES "
                     "(?, 'close', 'gift_nifty', 26010.0, ?, ?)", (str(prev), f"{prev} 15:30:41", f"{prev} 15:31:03"))
        conn.commit()
        out = []
        for label, now, adv, dec, when, gift in (
                ("0900", now0, 980, 1130, now0.strftime("%d-%b-%Y") + " 09:00:58", 25921.5),
                ("0909", now0.replace(minute=9, second=12), 1043, 1520, now0.strftime("%d-%b-%Y") + " 09:07:44", 25904.0)):
            items = [{"metadata": {"symbol": f"T{i}", "previousClose": 100},
                      "detail": {"preOpenMarket": {"IEP": 101 if i < 3 else 99, "prevClose": 100, "totalBuyQuantity": 5,
                                                   "totalSellQuantity": 7, "lastUpdateTime": when}}} for i in range(6)]
            src = PC.Sources(
                us=lambda: {"nasdaq": {"last": 18450.2, "prev": 19478.9, "as_of": "2026-11-20"},
                            "dow": {"last": 43810.0, "prev": 44673.0, "as_of": "2026-11-20"},
                            "nq": {"last": 20110.0, "prev": 20050.0, "as_of": (now - timedelta(minutes=6)).isoformat()},
                            "ym": {"last": 43795.0, "prev": 43880.0, "as_of": (now - timedelta(minutes=6)).isoformat()}},
                dhan=lambda: {"india_vix": {"ltp": 17.85}, "nifty50": {"prev_close": 25984.3}},
                preopen=lambda: {"data": items, "advances": adv, "declines": dec, "unchanged": 61},
                gift=lambda: {"price": gift, "chg_pct": -0.31, "expiry": "30-Dec-2026",
                              "as_of": now - timedelta(seconds=40), "source": "SYNTHETIC test feed (not NSE)"})
            r = PC.compute(conn, label, now, src)
            r["us_source"] = "SYNTHETIC test feed (not Yahoo)"
            r["ad_source"] = "SYNTHETIC test feed (not NSE)"
            r["vix_source"] = "SYNTHETIC test feed (not Dhan)"
            PC.store(conn, r)
            out.append({k: r.get(k) for k in ("run_label", "fetched_at_ist", "bias", "vote_sum", "vix_regime",
                                              "expected_open", "gift_move_pct", "gift_move_basis", "ad_basis")})
            out.append({"telegram_text": PC.telegram_text(PC.latest_run(conn, str(now.date())))})
        out.append({"notify_payload": PC.notify_payload(conn, now0.replace(minute=10))})
    finally:
        conn.close()
    json.dump(out, sys.stdout, indent=2, default=str)


if __name__ == "__main__":
    main()
