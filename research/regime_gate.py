"""
W39 (RG-01..RG-04) — market regime gate: should new long (or short) signals be taken today?

The single filter nearly every successful retail method applies before acting on a stock signal
is the state of the market itself (O'Neil / IBD "market direction"). This module rebuilds that read
for the Nifty 50 from ATIP's own data, one session at a time, using only what was known on that day:

    distribution day   the Nifty closes down >= 0.2 % on HIGHER market volume than the session before.
                       Active for 25 sessions, or until the Nifty closes 5 % above that day's close.
    status             CONFIRMED_UPTREND        fewer than 4 active distribution days
                       UPTREND_UNDER_PRESSURE   4 or 5 active distribution days
                       CORRECTION               6+ active distribution days, the Nifty 10 % below its
                                                highest close since the uptrend began, or (after a
                                                follow-through) a close below the correction's low
                       RALLY_ATTEMPT            in a correction, the first up close after the lowest close
                                                starts day 1; a lower close than that low resets it
                       follow-through day       day 4 or later of a rally attempt, the Nifty up >= 1.25 % on
                                                higher volume: back to CONFIRMED_UPTREND, count cleared
    gate               OPEN     confirmed uptrend, Nifty above its 200-DMA
                       CAUTION  uptrend under pressure, or a confirmed uptrend still below the 200-DMA
                       CLOSED   correction or rally attempt (no follow-through yet)
    alignment          a BULL signal is WITH the market when the gate is OPEN, MIXED on CAUTION and
                       AGAINST it when CLOSED; a BEAR signal the other way round

Market volume: IBD uses exchange volume. ATIP sums the day's volume over every stock in
prices_daily that traded on both days (so a stock joining or leaving the universe does not move
the ratio); NIFTYBEES volume when fewer than 20 stocks are stored. A day without a volume
comparison counts as neither a distribution day nor a follow-through day.

Nifty closes: global_market_history 'nifty50' (5 years from Yahoo via
`python -m research.market_pulse nifty-history`), extended with market_health.nifty_close for
days after its last row; else market_health alone; else NIFTYBEES.

Thresholds are config.json "regime_gate" (DEFAULTS below). They are ATIP's choices in the IBD
tradition, not IBD's published rules; the signal track record split by alignment
(research/tech_signals.py) is what shows whether the gate helps on Indian stocks.

Table market_regime_gate: one row per session, recomputed nightly before the technical signals
(the series is causal, so each row is what the gate said that day). Signals keep the gate they
were born under (technical_signal.market_gate / alignment), so later data fixes cannot rewrite it.

CLI: python -m research.regime_gate [now | history --days 120 | run]
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

DEFAULTS = {
    "dd_drop_pct": 0.2,            # a distribution day: Nifty down at least this much ...
    "dd_window": 25,               # ... stays on the count for this many sessions ...
    "dd_expire_gain_pct": 5.0,     # ... or until the Nifty closes this much above that day's close
    "pressure_dd": 4,              # active distribution days for UPTREND_UNDER_PRESSURE
    "correction_dd": 6,            # ... and for CORRECTION
    "correction_drawdown_pct": 10.0,   # or the Nifty this far below the uptrend's highest close
    "ftd_min_day": 4,              # a follow-through day: day 4 or later of a rally attempt ...
    "ftd_pct": 1.25,               # ... with the Nifty up at least this much on higher volume
    "lookback_days": 1100,         # calendar days of history replayed (warm-up included)
    "warmup_sessions": 60,         # rows before this many sessions carry gate UNKNOWN
}
STATUSES = ("CONFIRMED_UPTREND", "UPTREND_UNDER_PRESSURE", "RALLY_ATTEMPT", "CORRECTION")
GATES = ("OPEN", "CAUTION", "CLOSED")

DDL = (
    """CREATE TABLE IF NOT EXISTS market_regime_gate (
        date DATE PRIMARY KEY, nifty_close REAL, change_pct REAL, volume_ratio REAL,
        distribution_day INTEGER, dd_count INTEGER, dd_dates TEXT, sma50 REAL, sma200 REAL,
        above_200dma INTEGER, drawdown_pct REAL, status TEXT, gate TEXT, rally_day INTEGER,
        follow_through INTEGER, ftd_date DATE, reason TEXT, volume_source TEXT, computed_at TIMESTAMP)""",
)


def settings() -> dict:
    try:
        from ops.config import load
        raw = load().get("regime_gate") or {}
    except Exception:
        raw = {}
    out = dict(DEFAULTS)
    for k, v in raw.items():
        if k in DEFAULTS:
            try:
                out[k] = type(DEFAULTS[k])(v)
            except (TypeError, ValueError):
                pass
    return out


def ensure_tables(conn):
    for d in DDL:
        conn.execute(d)


def _d(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def _series(rows) -> pd.Series:
    s = pd.Series({pd.Timestamp(str(r[0])[:10]): float(r[1]) for r in rows if r[1] is not None}, dtype=float)
    return s.sort_index()


# ── inputs ───────────────────────────────────────────────────────────────────

def nifty_closes(conn, as_of=None, lookback_days=None) -> tuple:
    """(Series of Nifty 50 daily closes up to as_of, source label)."""
    as_of = _d(as_of or date.today())
    start = str(as_of - timedelta(days=int(lookback_days or DEFAULTS["lookback_days"])))
    end = str(as_of)

    def q(sql):
        try:
            return _series(conn.execute(sql, (start, end)).fetchall())
        except Exception:
            return pd.Series(dtype=float)
    yahoo = q("SELECT date, close FROM global_market_history WHERE series='nifty50' AND close>0 AND date>? AND date<=?")
    own = q("SELECT date, nifty_close FROM market_health WHERE nifty_close>0 AND date>? AND date<=?")
    if len(yahoo) >= 200:
        tail = own[own.index > yahoo.index[-1]] if len(own) else own
        return pd.concat([yahoo, tail]).sort_index(), "global_market_history nifty50" + (
            " + market_health" if len(tail) else "")
    if len(own) >= 60:
        return own, "market_health"
    etf = q("SELECT date, close FROM prices_daily WHERE symbol='NIFTYBEES' AND close>0 AND date>? AND date<=?")
    if len(etf) >= 60:
        return etf, "NIFTYBEES"
    best = max((yahoo, "global_market_history nifty50"), (own, "market_health"), (etf, "NIFTYBEES"),
               key=lambda x: len(x[0]))
    return best


def market_volume_ratio(conn, as_of=None, lookback_days=None) -> tuple:
    """(Series: today's market volume / the previous session's, over stocks that traded on both days; source)."""
    as_of = _d(as_of or date.today())
    start = str(as_of - timedelta(days=int(lookback_days or DEFAULTS["lookback_days"])))
    try:
        rows = conn.execute("SELECT date, symbol, volume FROM prices_daily WHERE date>? AND date<=? AND volume>0 "
                            "AND symbol<>'NIFTYBEES'", (start, str(as_of))).fetchall()
    except Exception:
        rows = []
    if rows:
        df = pd.DataFrame(rows, columns=["date", "symbol", "volume"])
        df["date"] = pd.to_datetime(df["date"].astype(str).str[:10])
        wide = df.pivot_table(index="date", columns="symbol", values="volume", aggfunc="last").sort_index()
        if wide.shape[1] >= 20:
            prev = wide.shift(1)
            both = wide.notna() & prev.notna()
            num = wide.where(both).sum(axis=1)
            den = prev.where(both).sum(axis=1)
            ratio = (num / den.replace(0, np.nan)).where(both.sum(axis=1) >= 10)
            return ratio, f"prices_daily ({wide.shape[1]} stocks)"
    try:
        etf = _series(conn.execute("SELECT date, volume FROM prices_daily WHERE symbol='NIFTYBEES' AND volume>0 "
                                   "AND date>? AND date<=?", (start, str(as_of))).fetchall())
    except Exception:
        etf = pd.Series(dtype=float)
    if len(etf) > 1:
        return etf / etf.shift(1), "NIFTYBEES"
    return pd.Series(dtype=float), None


# ── the state machine ────────────────────────────────────────────────────────

def gate_for(status, close, sma200) -> str | None:
    if status is None:
        return None
    if status in ("CORRECTION", "RALLY_ATTEMPT"):
        return "CLOSED"
    if status == "UPTREND_UNDER_PRESSURE":
        return "CAUTION"
    if sma200 is not None and not np.isnan(sma200) and close <= sma200:
        return "CAUTION"
    return "OPEN"


def alignment(direction, gate) -> str | None:
    """WITH / MIXED / AGAINST the market for a BULL or BEAR signal under `gate`."""
    if gate not in GATES or direction not in ("BULL", "BEAR"):
        return None
    if gate == "CAUTION":
        return "MIXED"
    with_market = (gate == "OPEN") == (direction == "BULL")
    return "WITH" if with_market else "AGAINST"


def _num(v, nd=None):
    if v is None or pd.isna(v):
        return None
    return round(float(v), nd) if nd is not None else float(v)


def replay(close: pd.Series, vol_ratio: pd.Series | None = None, cfg: dict | None = None) -> list:
    """Walk the sessions oldest first and return one row per session (causal: row t uses data <= t)."""
    cfg = {**DEFAULTS, **(cfg or {})}
    close = close.dropna().astype(float).sort_index()
    if close.empty:
        return []
    vr = (vol_ratio if vol_ratio is not None else pd.Series(dtype=float)).reindex(close.index)
    sma50 = close.rolling(50).mean()
    sma200 = close.rolling(200).mean()
    chg = close.pct_change() * 100
    state, peak, low, rally_day, ftd_date = "CONFIRMED_UPTREND", float(close.iloc[0]), None, 0, None
    base_low = None                           # the correction low a follow-through day launched from
    dds = []                                  # [(position, date, close)]
    out = []
    for i, (ts, c) in enumerate(close.items()):
        p = chg.iloc[i]
        v = vr.iloc[i]
        vol_up = v is not None and not pd.isna(v) and v > 1.0
        dds = [x for x in dds if i - x[0] < cfg["dd_window"] and c < x[2] * (1 + cfg["dd_expire_gain_pct"] / 100)]
        is_dd = bool(i > 0 and not pd.isna(p) and p <= -cfg["dd_drop_pct"] and vol_up)
        if is_dd:
            dds.append((i, ts, c))
        ftd = False
        reason = ""
        if state in ("CONFIRMED_UPTREND", "UPTREND_UNDER_PRESSURE"):
            peak = max(peak, c)
            draw = (1 - c / peak) * 100
            failed = base_low is not None and c < base_low
            if len(dds) >= cfg["correction_dd"] or draw >= cfg["correction_drawdown_pct"] or failed:
                reason = (f"{len(dds)} distribution days in {cfg['dd_window']} sessions" if len(dds) >= cfg["correction_dd"]
                          else "follow-through failed: Nifty closed below the correction low" if failed
                          else f"Nifty {draw:.1f}% below the uptrend's high")
                state, low, rally_day, base_low = "CORRECTION", c, 0, None
            elif len(dds) >= cfg["pressure_dd"]:
                state = "UPTREND_UNDER_PRESSURE"
                reason = f"{len(dds)} distribution days in {cfg['dd_window']} sessions"
            else:
                state = "CONFIRMED_UPTREND"
                reason = f"{len(dds)} distribution day{'s' if len(dds) != 1 else ''} in {cfg['dd_window']} sessions"
        else:
            if c < low:
                state, low, rally_day = "CORRECTION", c, 0
                reason = "new low close: the rally attempt, if any, failed"
            elif rally_day == 0:
                if not pd.isna(p) and p > 0:
                    state, rally_day = "RALLY_ATTEMPT", 1
                    reason = "day 1 of a rally attempt"
                else:
                    reason = "in a correction, no rally attempt yet"
            else:
                rally_day += 1
                if rally_day >= cfg["ftd_min_day"] and not pd.isna(p) and p >= cfg["ftd_pct"] and vol_up:
                    state, peak, ftd_date, ftd, dds, base_low = "CONFIRMED_UPTREND", c, ts, True, [], low
                    reason = f"follow-through day: day {rally_day} of the rally, Nifty {p:+.2f}% on higher volume"
                    rally_day = 0
                else:
                    reason = f"day {rally_day} of a rally attempt, waiting for a follow-through day"
        if state in ("CONFIRMED_UPTREND", "UPTREND_UNDER_PRESSURE"):
            draw_now = (1 - c / peak) * 100
        else:
            draw_now = None
        s200 = sma200.iloc[i]
        warm = i < cfg["warmup_sessions"]
        g = None if warm else gate_for(state, c, s200)
        if g == "CAUTION" and state == "CONFIRMED_UPTREND":
            reason += "; Nifty below its 200-DMA"
        out.append({"date": ts.date(), "nifty_close": round(c, 2), "change_pct": _num(p, 3),
                    "volume_ratio": _num(v, 3), "distribution_day": int(is_dd),
                    "dd_count": len(dds), "dd_dates": [str(x[1].date()) for x in dds],
                    "sma50": _num(sma50.iloc[i], 2), "sma200": _num(s200, 2),
                    "above_200dma": None if pd.isna(s200) else int(c > s200),
                    "drawdown_pct": _num(draw_now, 2),
                    "status": None if warm else state, "gate": g,
                    "rally_day": rally_day if state == "RALLY_ATTEMPT" else None, "follow_through": int(ftd),
                    "ftd_date": str(ftd_date.date()) if ftd_date is not None else None,
                    "reason": "warming up: not enough history yet" if warm else reason})
    return out


# ── store / read ─────────────────────────────────────────────────────────────

COLS = ("nifty_close", "change_pct", "volume_ratio", "distribution_day", "dd_count", "dd_dates", "sma50", "sma200",
        "above_200dma", "drawdown_pct", "status", "gate", "rally_day", "follow_through", "ftd_date", "reason",
        "volume_source")


def update(conn=None, as_of=None) -> dict:
    """Replay the gate up to as_of and store every session; returns the as_of row."""
    from db.schema import get_connection
    own = conn is None
    conn = conn or get_connection()
    try:
        ensure_tables(conn)
        cfg = settings()
        as_of = _d(as_of or date.today())
        close, csrc = nifty_closes(conn, as_of, cfg["lookback_days"])
        if len(close) < 30:
            return {"status": "SKIPPED", "rows": 0, "reason": f"only {len(close)} Nifty closes stored "
                    "(python -m research.market_pulse nifty-history loads 5 years)"}
        ratio, vsrc = market_volume_ratio(conn, as_of, cfg["lookback_days"])
        rows = replay(close, ratio, cfg)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for r in rows:
            vals = [r[c] for c in COLS[:-1]]
            vals[COLS.index("dd_dates")] = json.dumps(r["dd_dates"])
            conn.execute(f"INSERT INTO market_regime_gate (date, {', '.join(COLS)}, computed_at) VALUES "
                         f"({','.join('?' * (len(COLS) + 2))}) ON CONFLICT(date) DO UPDATE SET " +
                         ", ".join(f"{c}=excluded.{c}" for c in COLS) + ", computed_at=excluded.computed_at",
                         [str(r["date"])] + vals + [vsrc, now])
        conn.commit()
        last = rows[-1]
        covered = sum(1 for r in rows if r["volume_ratio"] is not None)
        return {"status": "SUCCESS", "rows": len(rows), "as_of": str(last["date"]), "gate": last["gate"],
                "market_status": last["status"], "dd_count": last["dd_count"], "close_source": csrc,
                "volume_source": vsrc, "volume_coverage_pct": round(covered / len(rows) * 100, 1)}
    finally:
        if own:
            conn.close()


def _row(conn, sql, args=()) -> dict | None:
    cur = conn.execute(sql, args)
    r = cur.fetchone()
    if not r:
        return None
    d = dict(zip([c[0] for c in cur.description], r))
    d["dd_dates"] = json.loads(d.get("dd_dates") or "[]")
    d["date"] = str(d["date"])[:10]
    return d


def gate_on(conn, day) -> dict | None:
    """The stored gate row for `day` (or the last session before it)."""
    try:
        ensure_tables(conn)
        return _row(conn, "SELECT * FROM market_regime_gate WHERE date<=? ORDER BY date DESC LIMIT 1", (str(_d(day)),))
    except Exception:
        return None


def current(conn) -> dict:
    """Today's gate with the context a page needs: the change log and the recent distribution days."""
    ensure_tables(conn)
    r = _row(conn, "SELECT * FROM market_regime_gate ORDER BY date DESC LIMIT 1")
    if not r:
        return {"status": "EMPTY", "reason": "not computed yet (runs nightly with the technical signals; "
                "POST /api/market-regime/run or python -m research.regime_gate run)"}
    cur = conn.execute("SELECT date, status, gate, reason FROM market_regime_gate WHERE status IS NOT NULL "
                       "ORDER BY date DESC LIMIT 400")
    changes, prev = [], None
    for d, st, g, why in reversed(cur.fetchall()):
        if st != prev:
            changes.append({"date": str(d)[:10], "status": st, "gate": g, "reason": why})
            prev = st
    since = changes[-1]["date"] if changes else None
    cfg = settings()
    return {**r, "status_since": since, "changes": changes[-8:][::-1],
            "rules": {k: cfg[k] for k in ("dd_drop_pct", "dd_window", "pressure_dd", "correction_dd",
                                           "correction_drawdown_pct", "ftd_min_day", "ftd_pct")},
            "note": "ATIP's IBD-style market read; the /signals track record shows whether it helps."}


def history(conn, days: int = 250) -> list:
    ensure_tables(conn)
    cur = conn.execute("SELECT date, nifty_close, change_pct, volume_ratio, distribution_day, dd_count, sma50, sma200, "
                       "status, gate, follow_through FROM market_regime_gate ORDER BY date DESC LIMIT ?",
                       (max(1, min(int(days), 2000)),))
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()][::-1]
    for r in rows:
        r["date"] = str(r["date"])[:10]
    return rows


def run_job() -> dict:
    return update()


def main(argv=None):
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(prog="python -m research.regime_gate")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    r.add_argument("--date")
    h = sub.add_parser("history")
    h.add_argument("--days", type=int, default=60)
    sub.add_parser("now")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        out = update(as_of=a.date)
    else:
        from db.schema import get_connection
        conn = get_connection()
        try:
            out = history(conn, a.days) if a.cmd == "history" else current(conn)
        finally:
            conn.close()
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
