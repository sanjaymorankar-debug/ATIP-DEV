"""
W39 Phase 3 (item 1, IN-01..03) — intraday scans on the 15-minute bars ATIP already stores
(intraday_bars, interval_min 15, written every 30 minutes from 10:00 by run_intraday_30min from Dhan's
intraday API for the tracked universe).

    orb_up / orb_down          opening-range breakout: the first bar after the opening range (09:15-09:30;
                               "or_minutes": 30 makes it 09:15-09:45) to CLOSE above its high / below its low,
                               counted only if that first close comes before "orb_until" (11:30) on 1.5x+ the
                               slot's usual volume ("orb_min_rvol")
    open_low / open_high       open = low / open = high (Chartink's favourite), judged once on the first hour
                               (09:15-10:15): no trade more than "open_tol_pct" (0.1 %) below (above) the open,
                               and the 10:15 close "open_min_move_pct" (0.5 %) or more above (below) it
    squeeze_up / squeeze_down  the TTM squeeze of research/technicals.py on 15-minute bars: Bollinger bands
                               inside the Keltner channel for "squeeze_bars" (6) bars, released on a bar that
                               closes beyond SMA 20 in the bar's direction on 1.2x+ the slot's usual volume

Each fires at most once per stock, day and scan. A hit stores the trigger bar and its close, and the price
when ATIP SAW it (the latest completed bar at the run): bars arrive every 30 minutes, so the record is
measured from the price an alert could actually have been acted on, not the trigger bar's close.

Record (IN-02): once the session is over, each hit gets its return to the session's last close, signed for
direction, that minus the Nifty's move over the same minutes (index_levels), and the best and worst move
in its favour / against it before the close. stats() gives per scan n, how often it made money and beat
the Nifty, and the mean / median. A scan only alerts once 30 hits are closed with a positive mean excess
(the plan's rule for every new signal); until then it is shown as "held".

Volume: rvol_slot = the trigger bar's volume over the same 15-minute slot's average in the last 5 sessions
(3 needed), so the breakout and squeeze scans start once 3 sessions of bars are stored. Calibration on
random-walk prices with independent volumes (500 stocks): without the volume and time filters the
opening-range breakout fired on ~78 % of stocks a day per side; with them it is rare.

Config (config.json "intraday_signals"): {"enabled": true, "or_minutes": 15, "orb_until": "11:30",
"orb_min_rvol": 1.5, "open_tol_pct": 0.1, "open_min_move_pct": 0.5, "squeeze_bars": 6, "squeeze_min_rvol": 1.2,
"history_sessions": 6, "alerts": true}. The job runs every 15 minutes from 09:45 to
15:50 on trading days (pipeline/scheduler.py); without stored bars it does nothing.
CLI: python -m research.intraday_signals [run|stats|today]
"""

from __future__ import annotations

import json
import logging
import statistics
from datetime import date, datetime, time, timedelta

import pandas as pd

log = logging.getLogger(__name__)

SCANS = {
    "orb_up": ("Opening-range breakout", "BULL"), "orb_down": ("Opening-range breakdown", "BEAR"),
    "open_low": ("Open = low", "BULL"), "open_high": ("Open = high", "BEAR"),
    "squeeze_up": ("Intraday squeeze fired up", "BULL"), "squeeze_down": ("Intraday squeeze fired down", "BEAR"),
}
DEFAULTS = {"enabled": True, "or_minutes": 15, "orb_until": "11:30", "orb_min_rvol": 1.5, "open_tol_pct": 0.1,
            "open_min_move_pct": 0.5, "squeeze_bars": 6, "squeeze_min_rvol": 1.2, "history_sessions": 6,
            "alerts": True}
INTERVAL = 15
SESSION_OPEN, SESSION_CLOSE = time(9, 15), time(15, 30)
PROVE_CLOSED = 30
MIN_RECORD = 10

DDL = (
    """CREATE TABLE IF NOT EXISTS intraday_signal (
        signal_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, date DATE NOT NULL, scan TEXT NOT NULL,
        direction TEXT NOT NULL, bar_ts TIMESTAMP, price REAL, level REAL, rvol_slot REAL, reason TEXT,
        seen_at TIMESTAMP, seen_price REAL, close_price REAL, ret_close_pct REAL, excess_close_pct REAL,
        mfe_pct REAL, mae_pct REAL, evaluated_at TIMESTAMP, created_at TIMESTAMP)""",
    "CREATE INDEX IF NOT EXISTS idx_intraday_signal_date ON intraday_signal(date)",
)


def settings() -> dict:
    try:
        from ops.config import load
        raw = load().get("intraday_signals") or {}
    except Exception:
        raw = {}
    return {k: type(v)(raw[k]) if k in raw else v for k, v in DEFAULTS.items()}


def ensure_tables(conn):
    for d in DDL:
        conn.execute(d)


def _ts(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.strptime(str(v)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")


# ── bars ─────────────────────────────────────────────────────────────────────

def load_bars(conn, day: date, now: datetime | None = None, symbols=None, sessions: int = 4) -> dict:
    """{symbol: DataFrame(open, high, low, close, volume) indexed by bar start} for the 15-minute bars of
    the last `sessions` trading days up to `day`, completed by `now` (a bar stamped ts is done at ts + 15)."""
    from utils.trading_calendar import is_trading_day
    start, k = day, 1
    while k < sessions:
        start -= timedelta(days=1)
        k += is_trading_day(start)
    upto = now or datetime.combine(day, SESSION_CLOSE)
    sql = ("SELECT symbol, ts, open, high, low, close, volume FROM intraday_bars WHERE interval_min=? AND ts>=? "
           "AND ts<? ")
    args = [INTERVAL, str(start), str(day + timedelta(days=1))]
    if symbols:
        sql += f"AND symbol IN ({','.join('?' * len(symbols))}) "
        args += list(symbols)
    rows = {}
    for sym, ts, o, h, lo, c, v in conn.execute(sql + "ORDER BY symbol, ts", args):
        t = _ts(ts)
        if t + timedelta(minutes=INTERVAL) <= upto and c:
            rows.setdefault(sym, []).append((t, o, h, lo, c, v or 0))
    return {s: pd.DataFrame([r[1:] for r in rs], index=pd.DatetimeIndex([r[0] for r in rs]),
                            columns=["open", "high", "low", "close", "volume"]).astype(float)
            for s, rs in rows.items()}


def _rvol(df: pd.DataFrame, t: pd.Timestamp, n: int = 5):
    """The bar's volume over the same slot's average in the previous n sessions (3 needed)."""
    prior = df[(df.index.time == t.time()) & (df.index.normalize() < t.normalize())]["volume"].iloc[-n:]
    if len(prior) < 3 or prior.mean() <= 0:
        return None
    return round(float(df.at[t, "volume"] / prior.mean()), 2)


# ── the scans (pure) ─────────────────────────────────────────────────────────

def scan_symbol(df: pd.DataFrame, day: date, cfg: dict | None = None) -> list:
    """Hits on `day`'s completed bars: [{scan, direction, bar_ts, price, level, reason, rvol_slot}]."""
    cfg = cfg or DEFAULTS
    if df is None or df.empty:
        return []
    today = df[df.index.normalize() == pd.Timestamp(day)]
    if today.empty:
        return []
    out = []

    def hit(scan, t, level, reason):
        out.append({"scan": scan, "direction": SCANS[scan][1], "bar_ts": t.to_pydatetime(),
                    "price": round(float(today.at[t, "close"]), 2), "level": round(float(level), 2),
                    "reason": reason, "rvol_slot": _rvol(df, t)})

    # opening range
    or_end = datetime.combine(day, SESSION_OPEN) + timedelta(minutes=int(cfg["or_minutes"]))
    rng = today[today.index < pd.Timestamp(or_end)]
    after = today[today.index >= pd.Timestamp(or_end)]
    until = pd.Timestamp(datetime.combine(day, datetime.strptime(str(cfg["orb_until"]), "%H:%M").time()))
    if len(rng) == int(cfg["or_minutes"]) // INTERVAL and not after.empty:
        hi, lo = float(rng["high"].max()), float(rng["low"].min())
        for scan, beyond, level, word in (("orb_up", after["close"] > hi, hi, "above"),
                                          ("orb_down", after["close"] < lo, lo, "below")):
            first = after[beyond]
            if first.empty:
                continue
            t = first.index[0]                     # the FIRST close beyond the range decides
            rv = _rvol(df, t)
            if t < until and rv is not None and rv >= float(cfg["orb_min_rvol"]):
                hit(scan, t, level, f"closed {today.at[t, 'close']:.2f} {word} the {cfg['or_minutes']}-minute opening "
                                    f"range {'high' if word == 'above' else 'low'} {level:.2f} at {t:%H:%M} on "
                                    f"{rv:.1f}x the slot's usual volume")
    # open = low / open = high, judged once on the first hour (09:15-10:15), whenever the job runs
    hour = today.iloc[:4]
    if len(hour) == 4 and hour.index[0].time() == SESSION_OPEN:
        op, tol = float(hour["open"].iloc[0]), float(cfg["open_tol_pct"]) / 100
        last, t = float(hour["close"].iloc[-1]), hour.index[-1]
        mv = float(cfg["open_min_move_pct"]) / 100
        if float(hour["low"].min()) >= op * (1 - tol) and last >= op * (1 + mv):
            hit("open_low", t, op, f"opened at {op:.2f} and did not trade below it in the first hour (low "
                                   f"{hour['low'].min():.2f}); {last:.2f} at 10:15")
        if float(hour["high"].max()) <= op * (1 + tol) and last <= op * (1 - mv):
            hit("open_high", t, op, f"opened at {op:.2f} and did not trade above it in the first hour (high "
                                    f"{hour['high'].max():.2f}); {last:.2f} at 10:15")
    # squeeze, with research/technicals.py's Bollinger / Keltner on the 15-minute frame
    n = int(cfg["squeeze_bars"])
    if len(df) >= 20 + n + 1:
        from research.technicals import indicators
        d = indicators(df[["open", "high", "low", "close", "volume"]])
        on = (d["kc_up"] > d["bb_up"]) & (d["kc_lo"] < d["bb_lo"])
        for i in range(len(d)):
            t = d.index[i]
            if t.normalize() != pd.Timestamp(day) or i < n + 1:
                continue
            if not (on.iloc[i - n:i].all() and not on.iloc[i]):
                continue
            c, p, mid = d["close"].iloc[i], d["close"].iloc[i - 1], d["sma20"].iloc[i]
            rv = _rvol(df, t)
            if rv is None or rv < float(cfg["squeeze_min_rvol"]):
                break
            if c > mid and c > p:
                hit("squeeze_up", t, mid, f"Bollinger bands left the Keltner channel after {n}+ bars of squeeze, "
                                          f"close {c:.2f} above SMA 20 {mid:.2f} at {t:%H:%M}")
            elif c < mid and c < p:
                hit("squeeze_down", t, mid, f"Bollinger bands left the Keltner channel after {n}+ bars of squeeze, "
                                            f"close {c:.2f} below SMA 20 {mid:.2f} at {t:%H:%M}")
            break                                  # the first release of the day decides
    return out


# ── running, evaluating, the record ──────────────────────────────────────────

def _nifty_at(conn, day, hhmm: str, last=False):
    """The Nifty at or before hh:mm on day (index_levels), or the day's last reading up to 15:30."""
    try:
        r = conn.execute("SELECT nifty50 FROM index_levels WHERE date=? AND time<=? AND nifty50>0 "
                         "ORDER BY time DESC LIMIT 1", (str(day), "15:30" if last else hhmm)).fetchone()
    except Exception:
        return None
    return float(r[0]) if r else None


def run(conn, now: datetime | None = None, symbols=None) -> dict:
    """Scan today's completed bars, store new hits, evaluate finished sessions, alert proven scans."""
    ensure_tables(conn)
    cfg = settings()
    now = now or datetime.now()
    day = now.date()
    bars = load_bars(conn, day, now, symbols, int(cfg["history_sessions"]))
    have = {r[0] for r in conn.execute("SELECT signal_id FROM intraday_signal WHERE date=?", (str(day),))}
    new = []
    for sym, df in bars.items():
        seen = df[df.index.normalize() == pd.Timestamp(day)]
        if seen.empty:
            continue
        for h in scan_symbol(df, day, cfg):
            sid = f"{sym}:{day}:{h['scan']}"
            if sid in have:
                continue
            conn.execute("""INSERT INTO intraday_signal (signal_id, symbol, date, scan, direction, bar_ts, price, level,
                                rvol_slot, reason, seen_at, seen_price, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (sid, sym, str(day), h["scan"], h["direction"], h["bar_ts"].strftime("%Y-%m-%d %H:%M:%S"),
                          h["price"], h["level"], h["rvol_slot"], h["reason"],
                          (seen.index[-1] + timedelta(minutes=INTERVAL)).strftime("%Y-%m-%d %H:%M:%S"),
                          round(float(seen["close"].iloc[-1]), 2), datetime.now()))
            new.append(dict(h, symbol=sym, signal_id=sid))
    conn.commit()
    ev = evaluate(conn, now)
    alerted = alert(conn, new) if cfg["alerts"] and new else 0
    return {"status": "SUCCESS" if bars else "SKIPPED", "symbols": len(bars), "new": len(new), "evaluated": ev,
            "alerted": alerted, "reason": None if bars else "no 15-minute bars stored for today"}


def evaluate(conn, now: datetime | None = None) -> int:
    """Fill the record of every hit whose session is over: return to the last close (signed), minus the
    Nifty's over the same minutes, and the best / worst move after it was seen."""
    ensure_tables(conn)
    now = now or datetime.now()
    pend = conn.execute("SELECT signal_id, symbol, date, direction, seen_at, seen_price FROM intraday_signal "
                        "WHERE ret_close_pct IS NULL AND seen_price>0").fetchall()
    n = 0
    for sid, sym, d, direction, seen_at, px in pend:
        day = date.fromisoformat(str(d)[:10])
        if now < datetime.combine(day, SESSION_CLOSE) + timedelta(minutes=INTERVAL):
            continue
        rows = conn.execute("SELECT ts, high, low, close FROM intraday_bars WHERE symbol=? AND interval_min=? AND ts>=? "
                            "AND ts<? ORDER BY ts", (sym, INTERVAL, str(day), str(day + timedelta(days=1)))).fetchall()
        later = [r for r in rows if _ts(r[0]) >= _ts(seen_at)]
        if not rows:
            continue
        sign = 1 if direction == "BULL" else -1
        last = float(rows[-1][3])
        ret = (last / px - 1) * 100
        hi = max([float(r[1]) for r in later] or [px])
        lo = min([float(r[2]) for r in later] or [px])
        mfe = ((hi / px - 1) if sign > 0 else (1 - lo / px)) * 100
        mae = ((lo / px - 1) if sign > 0 else (1 - hi / px)) * 100
        n0, n1 = _nifty_at(conn, day, _ts(seen_at).strftime("%H:%M")), _nifty_at(conn, day, "", last=True)
        exc = sign * (ret - (n1 / n0 - 1) * 100) if n0 and n1 else None
        conn.execute("UPDATE intraday_signal SET close_price=?, ret_close_pct=?, excess_close_pct=?, mfe_pct=?, "
                     "mae_pct=?, evaluated_at=? WHERE signal_id=?",
                     (last, round(sign * ret, 3), round(exc, 3) if exc is not None else None, round(mfe, 3),
                      round(mae, 3), datetime.now(), sid))
        n += 1
    conn.commit()
    return n


def _cell(xs):
    if not xs:
        return None
    return {"n": len(xs), "positive_pct": round(sum(1 for x in xs if x > 0) / len(xs) * 100, 1),
            "median_pct": round(statistics.median(xs), 3), "mean_pct": round(sum(xs) / len(xs), 3)}


def stats(conn) -> list:
    """Per scan: hits, closed, return to the close and excess over the Nifty, alert status."""
    ensure_tables(conn)
    rows = conn.execute("SELECT scan, ret_close_pct, excess_close_pct, mfe_pct, mae_pct FROM intraday_signal").fetchall()
    out = []
    for scan, (name, direction) in SCANS.items():
        rs = [r for r in rows if r[0] == scan]
        closed = [r for r in rs if r[1] is not None]
        exc = [r[2] for r in closed if r[2] is not None]
        out.append({"scan": scan, "name": name, "direction": direction, "hits": len(rs), "closed": len(closed),
                    "to_close": _cell([r[1] for r in closed]), "vs_nifty": _cell(exc),
                    "avg_best_pct": _mean([r[3] for r in closed]), "avg_worst_pct": _mean([r[4] for r in closed]),
                    "enough": len(closed) >= MIN_RECORD,
                    "alerts": "on" if _proven(exc) else "held"})
    return out


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 3) if xs else None


def _proven(excess: list) -> bool:
    return len(excess) >= PROVE_CLOSED and sum(excess) / len(excess) > 0


def proven_scans(conn) -> set:
    rows = conn.execute("SELECT scan, excess_close_pct FROM intraday_signal WHERE excess_close_pct IS NOT NULL").fetchall()
    by = {}
    for s, x in rows:
        by.setdefault(s, []).append(x)
    return {s for s, xs in by.items() if _proven(xs)}


def alert(conn, new: list) -> int:
    proven = proven_scans(conn)
    hits = [h for h in new if h["scan"] in proven]
    if not hits:
        return 0
    from alerts.telegram import notify
    import html
    lines = [f"{html.escape(h['symbol'])}: {SCANS[h['scan']][0]} ({h['direction'].lower()}), {html.escape(h['reason'])}"
             for h in hits[:15]]
    notify("<b>Intraday signals</b>\n" + "\n".join(lines), category="intraday_signals", severity="info",
           key=f"intraday:{date.today()}:{','.join(sorted(h['signal_id'] for h in hits))[:200]}")
    return len(hits)


def todays(conn, day=None) -> list:
    ensure_tables(conn)
    cur = conn.execute("SELECT * FROM intraday_signal WHERE date=? ORDER BY bar_ts, symbol", (str(day or date.today()),))
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    proven = proven_scans(conn)
    for r in rows:
        r["name"] = SCANS.get(r["scan"], (r["scan"],))[0]
        r["alerts"] = "on" if r["scan"] in proven else "held"
    return rows


def run_job() -> dict:
    """Scheduler entry (every 15 minutes, 09:45-15:50 on trading days)."""
    from db.schema import get_connection
    if not settings()["enabled"]:
        return {"status": "SKIPPED", "rows": 0, "reason": "intraday_signals.enabled is false"}
    conn = get_connection()
    try:
        out = run(conn)
    finally:
        conn.close()
    out["rows"] = out.get("new", 0)
    return out


def main(argv=None):
    import argparse
    from db.schema import get_connection
    p = argparse.ArgumentParser(description="Intraday scans on 15-minute bars")
    p.add_argument("cmd", choices=("run", "stats", "today"), nargs="?", default="today")
    a = p.parse_args(argv)
    conn = get_connection()
    try:
        out = run(conn) if a.cmd == "run" else stats(conn) if a.cmd == "stats" else todays(conn)
        print(json.dumps(out, indent=2, default=str))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
