"""
ATIP -- intraday scans on stored bars (W28: SG-08).

The intraday scans used to be "fetch live quotes again" (run_preclose_scan) and the
12:30 ZPI alert. This scans the session's STORED 15-minute bars (intraday_bars,
written every 30 min by the scheduler's dhan_15min_bars job) for every tracked
symbol and records each setup it finds in intraday_scan_hit:

    ORB_UP / ORB_DOWN       close beyond the opening range (first OR_BARS bars) on a bar
                            whose volume is >= ORB_VOL_MULT x the session's average bar
    VWAP_RECLAIM / VWAP_LOSS the last bar closed on the other side of session VWAP from
                            the bar before
    VOLUME_SURGE            last bar volume >= SURGE_MULT x the average of the SAME time
                            slot over the previous SURGE_SESSIONS sessions (needs history;
                            skipped without it rather than compared with something else)
    POWER_HOUR_UP / _DOWN   from 14:00: >= POWER_MOVE_PCT from the session open, on the
                            same side of VWAP, and at the session extreme within 2 bars
    GAP_HOLD_UP / _DOWN     opened >= GAP_PCT away from the previous daily close and has
                            not filled it

One row per (date, symbol, scan): a setup seen again later in the session updates
bar_ts / price / strength and keeps first_seen_at. Each hit carries the stock's last
ATIP score and signal for context. Hits are observations, not signals: nothing here
creates a strategy decision or an order. With intraday_scan.alerts true in
config.json, a hit agreeing with a BUY (or SELL) signal is sent once through
alerts.telegram.notify.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, datetime, time as _time

log = logging.getLogger(__name__)

INTERVAL = 15
OR_BARS = 2
ORB_VOL_MULT = 1.5
SURGE_MULT = 3.0
SURGE_SESSIONS = 5
POWER_FROM = _time(14, 0)
POWER_MOVE_PCT = 1.5
GAP_PCT = 2.0


def _ts(v):
    if isinstance(v, datetime):
        return v
    return datetime.strptime(str(v)[:19], "%Y-%m-%d %H:%M:%S")


def _load_bars(conn, day: date, symbols=None) -> dict:
    sql = ("SELECT symbol, ts, open, high, low, close, volume FROM intraday_bars WHERE interval_min=? "
           "AND ts>=? AND ts<?")
    args = [INTERVAL, f"{day} 00:00:00", f"{day} 23:59:59"]
    out = defaultdict(list)
    for s, ts, o, h, lo, c, v in conn.execute(sql, args):
        if symbols and s not in symbols:
            continue
        if c is None:
            continue
        out[s].append({"ts": _ts(ts), "open": o, "high": h, "low": lo, "close": c, "volume": v or 0})
    for s in out:
        out[s].sort(key=lambda b: b["ts"])
    return out


def _slot_history(conn, day: date, slot: str) -> dict:
    """symbol -> mean volume of the bar at time `slot` over the previous SURGE_SESSIONS sessions."""
    rows = conn.execute(
        "SELECT symbol, AVG(volume), COUNT(*) FROM intraday_bars WHERE interval_min=? AND ts<? AND "
        "substr(ts,12,5)=? AND ts>=date(?, '-14 day') GROUP BY symbol",
        (INTERVAL, f"{day} 00:00:00", slot, str(day))).fetchall()
    return {s: (avg, n) for s, avg, n in rows if n >= min(3, SURGE_SESSIONS)}


def _vwap(bars):
    pv = vol = 0.0
    out = []
    for b in bars:
        tp = (b["high"] + b["low"] + b["close"]) / 3 if b["high"] and b["low"] else b["close"]
        pv += tp * b["volume"]
        vol += b["volume"]
        out.append(pv / vol if vol else b["close"])
    return out


def scan_symbol(bars: list, prev_close=None, slot_avg=None) -> list:
    """[(scan, direction, price, strength, detail)] for one symbol's session bars."""
    hits = []
    if len(bars) < OR_BARS + 1:
        return hits
    last, prev = bars[-1], bars[-2]
    vw = _vwap(bars)
    avg_vol = sum(b["volume"] for b in bars[:-1]) / max(1, len(bars) - 1)
    or_hi = max(b["high"] for b in bars[:OR_BARS])
    or_lo = min(b["low"] for b in bars[:OR_BARS])
    vol_ratio = last["volume"] / avg_vol if avg_vol else None
    if vol_ratio and vol_ratio >= ORB_VOL_MULT:
        if last["close"] > or_hi:
            hits.append(("ORB_UP", "UP", last["close"], round((last["close"] / or_hi - 1) * 100, 2),
                         {"or_high": or_hi, "vol_ratio": round(vol_ratio, 2)}))
        elif last["close"] < or_lo:
            hits.append(("ORB_DOWN", "DOWN", last["close"], round((1 - last["close"] / or_lo) * 100, 2),
                         {"or_low": or_lo, "vol_ratio": round(vol_ratio, 2)}))
    if prev["close"] < vw[-2] and last["close"] > vw[-1]:
        hits.append(("VWAP_RECLAIM", "UP", last["close"], round((last["close"] / vw[-1] - 1) * 100, 2),
                     {"vwap": round(vw[-1], 2)}))
    elif prev["close"] > vw[-2] and last["close"] < vw[-1]:
        hits.append(("VWAP_LOSS", "DOWN", last["close"], round((1 - last["close"] / vw[-1]) * 100, 2),
                     {"vwap": round(vw[-1], 2)}))
    if slot_avg and slot_avg[0]:
        r = last["volume"] / slot_avg[0]
        if r >= SURGE_MULT:
            d = "UP" if last["close"] >= last["open"] else "DOWN"
            hits.append(("VOLUME_SURGE", d, last["close"], round(r, 2),
                         {"slot_avg_volume": round(slot_avg[0]), "sessions": slot_avg[1]}))
    sess_open = bars[0]["open"] or bars[0]["close"]
    move = (last["close"] / sess_open - 1) * 100 if sess_open else 0.0
    if last["ts"].time() >= POWER_FROM:
        hi = max(b["high"] for b in bars)
        lo = min(b["low"] for b in bars)
        recent = bars[-2:]
        if move >= POWER_MOVE_PCT and last["close"] > vw[-1] and any(b["high"] >= hi for b in recent):
            hits.append(("POWER_HOUR_UP", "UP", last["close"], round(move, 2), {"from_open_pct": round(move, 2)}))
        elif move <= -POWER_MOVE_PCT and last["close"] < vw[-1] and any(b["low"] <= lo for b in recent):
            hits.append(("POWER_HOUR_DOWN", "DOWN", last["close"], round(-move, 2), {"from_open_pct": round(move, 2)}))
    if prev_close:
        gap = (sess_open / prev_close - 1) * 100
        if gap >= GAP_PCT and min(b["low"] for b in bars) > prev_close:
            hits.append(("GAP_HOLD_UP", "UP", last["close"], round(gap, 2), {"gap_pct": round(gap, 2)}))
        elif gap <= -GAP_PCT and max(b["high"] for b in bars) < prev_close:
            hits.append(("GAP_HOLD_DOWN", "DOWN", last["close"], round(-gap, 2), {"gap_pct": round(gap, 2)}))
    return hits


def _context(conn, day: date) -> dict:
    d = conn.execute("SELECT MAX(date) FROM ai_scores WHERE date<?", (str(day),)).fetchone()[0]
    if not d:
        return {}
    return {s: (a, sig) for s, a, sig in conn.execute(
        "SELECT symbol, atip_score, signal FROM ai_scores WHERE date=?", (d,))}


def _prev_closes(conn, day: date) -> dict:
    return {s: c for s, c in conn.execute(
        "SELECT p.symbol, p.close FROM prices_daily p JOIN (SELECT symbol, MAX(date) d FROM prices_daily WHERE date<? "
        "GROUP BY symbol) m ON m.symbol=p.symbol AND m.d=p.date", (str(day),))}


def _alerts_on() -> bool:
    try:
        from pathlib import Path
        cfg = json.loads((Path("atip_data") / "config.json").read_text(encoding="utf-8"))
        return bool((cfg.get("intraday_scan") or {}).get("alerts"))
    except Exception:
        return False


def run_scan(day=None, conn=None, symbols=None) -> dict:
    from db.schema import get_connection
    own = conn is None
    conn = conn or get_connection()
    try:
        day = day or date.today()
        bars = _load_bars(conn, day, set(symbols) if symbols else None)
        if not bars:
            return {"status": "EMPTY", "hits": 0, "reason": f"no {INTERVAL}-min bars stored for {day}"}
        slot = max(b[-1]["ts"] for b in bars.values()).strftime("%H:%M")
        slots = _slot_history(conn, day, slot)
        ctx = _context(conn, day)
        prev = _prev_closes(conn, day)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        n = 0
        to_alert = []
        for sym, bs in bars.items():
            sa = slots.get(sym) if bs[-1]["ts"].strftime("%H:%M") == slot else None
            for scan, direction, price, strength, detail in scan_symbol(bs, prev.get(sym), sa):
                a, sig = ctx.get(sym, (None, None))
                conn.execute(
                    "INSERT INTO intraday_scan_hit (scan_date,symbol,scan,bar_ts,direction,price,strength,detail_json,"
                    "atip_score,signal,first_seen_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT"
                    "(scan_date,symbol,scan) DO UPDATE SET bar_ts=excluded.bar_ts, price=excluded.price,"
                    "strength=excluded.strength, detail_json=excluded.detail_json, updated_at=excluded.updated_at",
                    (str(day), sym, scan, bs[-1]["ts"].strftime("%Y-%m-%d %H:%M:%S"), direction, price, strength,
                     json.dumps(detail), a, sig, now, now))
                n += 1
                if (direction == "UP" and sig == "BUY") or (direction == "DOWN" and sig == "SELL"):
                    to_alert.append((sym, scan, direction, price, a, sig))
        conn.commit()
        sent = 0
        if to_alert and _alerts_on():
            from alerts.telegram import notify
            for sym, scan, direction, price, a, sig in to_alert:
                done = conn.execute("SELECT alerted FROM intraday_scan_hit WHERE scan_date=? AND symbol=? AND scan=?",
                                    (str(day), sym, scan)).fetchone()
                if done and done[0]:
                    continue
                notify(f"📡 <b>{sym}</b> {scan.replace('_', ' ')} @ {price:.2f} — agrees with {sig} "
                       f"(ATIP {a if a is not None else '—'})", category="intraday_scan", severity="info",
                       key=f"scan:{day}:{sym}:{scan}")
                conn.execute("UPDATE intraday_scan_hit SET alerted=1 WHERE scan_date=? AND symbol=? AND scan=?",
                             (str(day), sym, scan))
                sent += 1
            conn.commit()
        return {"status": "SUCCESS", "symbols": len(bars), "hits": n, "alerts": sent, "slot": slot}
    finally:
        if own:
            conn.close()


def hits(conn, day=None, scan=None, limit=300) -> list:
    sql = "SELECT * FROM intraday_scan_hit WHERE scan_date=?"
    args = [str(day or date.today())]
    if scan:
        sql += " AND scan=?"
        args.append(scan)
    sql += " ORDER BY updated_at DESC, strength DESC LIMIT ?"
    args.append(int(limit))
    out = []
    for r in conn.execute(sql, args):
        d = dict(r)
        d["detail"] = json.loads(d.pop("detail_json") or "{}")
        out.append(d)
    return out


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(description="Intraday scans on stored bars (SG-08)")
    ap.add_argument("--date")
    a = ap.parse_args()
    print(run_scan(date.fromisoformat(a.date) if a.date else None))
