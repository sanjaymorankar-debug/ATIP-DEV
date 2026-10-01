"""
W30 scheduled glue (advanced-quant data that strategies read, independent of quant.enabled).

    run_postmarket(td)    bar microstructure for the session (AF-07) + event sources (QR-09:
                          corporate actions, bulk deals, earnings, insider, SAST, the last 60
                          days of dividends)
    run_weekly_studies()  event studies for EARNINGS, DIVIDEND, INSIDER_BUY, INSIDER_SELL, SAST
                          over the last two years (quant/event_study.py)
"""

from __future__ import annotations


def run_postmarket(trade_date=None) -> dict:
    from db.schema import get_connection
    from quant.microstructure import compute_bars_day
    from quant.events import sync_events
    from quant.event_sources import sync_all
    conn = get_connection()
    try:
        ms = compute_bars_day(conn, trade_date)
        base = sync_events(conn)
        ev = sync_all(conn, dividends_days=60)
        return {"status": "SUCCESS", "rows": ms.get("symbols", 0), "microstructure": ms,
                "events": {**ev, "base_inserted": base.get("inserted")}}
    finally:
        conn.close()


def run_weekly_studies() -> dict:
    from db.schema import get_connection
    from quant.event_study import study
    conn = get_connection()
    try:
        out = {}
        for et in ("EARNINGS", "DIVIDEND", "INSIDER_BUY", "INSIDER_SELL", "SAST"):
            r = study(conn, et)
            out[et] = {"events": r.get("events"), "verdict": r.get("verdict")}
        return {"status": "SUCCESS", "rows": len(out), "studies": out}
    finally:
        conn.close()
