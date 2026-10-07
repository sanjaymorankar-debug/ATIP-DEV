"""
W39 (TA-05..TA-08) — technical signal engine: runs research/technicals.py over the tracked
universe every evening, stores each stock's technical snapshot, turns scan hits into signals
with levels, and keeps a track record of every signal so the page can show which scans
actually work on Indian stocks.

    technical_snapshot   one row per symbol per day: technical rating, RS rating (IBD-style 1-99:
                         0.4 ROC63 + 0.2 ROC126 + 0.2 ROC189 + 0.2 ROC252, ranked across the universe), key
                         indicators, today's candlestick patterns and scan hits (the screener's technical half)
    technical_signal     one row per scan hit: direction, entry (the close), stop and target from
                         ATR (stop 2 x ATR, target 4 x ATR: 2 R), a 20-session horizon, and a
                         CONFLUENCE count of independent agreeing evidence:
                             technical rating on the same side
                             volume above 1.5 x its average
                             relative strength vs the Nifty on the same side
                             market regime (market_health) not against it
                             a candlestick pattern on the same side
                             research rating (BUY/ADD for a long, REDUCE/SELL for a short)
                         -- a high-confluence signal is the "confirmed" one the literature recommends
                         over raw single-indicator triggers.
    evaluate_signals     OPEN signals are marked TARGET / STOPPED (a bar touching both counts as
                         STOPPED: conservative) or EXPIRED at the horizon with the return and R
    scan_stats           per scan: closed signals, win rate, average R, expectancy

Benchmark for relative strength: the Nifty 50 daily close from market_health, else index_levels,
else NIFTYBEES from prices_daily, else none.
EOD only: signals are computed after the close, for the next session. Nothing here orders.
Scheduled at 20:30 on market days (before the 20:40 research reports and 20:50 saved screens).
CLI: python -m research.tech_signals run | evaluate | stats | today
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timedelta

import pandas as pd

from research import technicals as T

log = logging.getLogger(__name__)

LOOKBACK_DAYS = 600            # calendar days of bars read per symbol (~400 sessions: enough for SMA 200 + 52w)
STOP_ATR, TARGET_ATR, HORIZON = 2.0, 4.0, 20

SNAP_COLS = ["tech_rating", "tech_rating_label", "rs_rating", "rsi_14", "macd_hist", "adx_14", "supertrend_dir", "atr_pct",
             "pct_from_sma50", "pct_from_sma200", "above_200dma", "bb_width_pct", "vol_ratio", "rs_63_pct",
             "return_1m_pct", "return_3m_pct", "patterns", "signals", "bull_signals", "bear_signals"]

DDL = (
    """CREATE TABLE IF NOT EXISTS technical_snapshot (
        symbol TEXT NOT NULL, date DATE NOT NULL, close REAL, tech_rating REAL, tech_rating_label TEXT,
        rs_rating INTEGER, rsi_14 REAL, macd_hist REAL, adx_14 REAL, supertrend_dir INTEGER, atr_pct REAL, pct_from_sma50 REAL,
        pct_from_sma200 REAL, above_200dma INTEGER, bb_width_pct REAL, vol_ratio REAL, rs_63_pct REAL,
        return_1m_pct REAL, return_3m_pct REAL, patterns TEXT, signals TEXT, bull_signals INTEGER,
        bear_signals INTEGER, scans_json TEXT, created_at TIMESTAMP, PRIMARY KEY (symbol, date))""",
    "CREATE INDEX IF NOT EXISTS idx_technical_snapshot_date ON technical_snapshot(date)",
    """CREATE TABLE IF NOT EXISTS technical_signal (
        signal_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, date DATE NOT NULL, scan TEXT NOT NULL, name TEXT,
        direction TEXT NOT NULL, reason TEXT, entry REAL, stop REAL, target REAL, atr REAL, horizon INTEGER,
        confluence INTEGER, evidence_json TEXT, status TEXT NOT NULL DEFAULT 'OPEN', outcome_date DATE,
        outcome_price REAL, return_pct REAL, r_multiple REAL, created_at TIMESTAMP, UNIQUE (symbol, date, scan))""",
    "CREATE INDEX IF NOT EXISTS idx_technical_signal_date ON technical_signal(date)",
    "CREATE INDEX IF NOT EXISTS idx_technical_signal_status ON technical_signal(status)",
)


def ensure_tables(conn):
    for d in DDL:
        conn.execute(d)


def _d(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


# ── data ─────────────────────────────────────────────────────────────────────

def load_bars(conn, symbols, as_of, lookback_days=LOOKBACK_DAYS) -> dict:
    """{symbol: DataFrame(open, high, low, close, volume) indexed by date}, oldest first."""
    out = {}
    start = str(_d(as_of) - timedelta(days=lookback_days))
    syms = sorted(set(symbols))
    for i in range(0, len(syms), 400):
        part = syms[i:i + 400]
        q = (f"SELECT symbol, date, open, high, low, close, volume FROM prices_daily WHERE symbol IN "
             f"({','.join('?' * len(part))}) AND date>? AND date<=? AND close>0 ORDER BY symbol, date")
        rows = conn.execute(q, part + [start, str(_d(as_of))]).fetchall()
        cur, buf = None, []
        for r in rows + [(None,)]:
            if r[0] != cur and buf:
                df = pd.DataFrame(buf, columns=["date", "open", "high", "low", "close", "volume"])
                df["date"] = pd.to_datetime(df["date"].astype(str).str[:10])
                df = df.set_index("date")
                for c in ("open", "high", "low"):
                    df[c] = df[c].fillna(df["close"])
                df["volume"] = df["volume"].fillna(0)
                out[cur] = df
                buf = []
            if r[0] is None:
                break
            cur = r[0]
            buf.append(r[1:])
    return out


def benchmark(conn, as_of) -> pd.Series | None:
    """Nifty 50 daily close: market_health.nifty_close (kept 600 days), else index_levels (last reading per
    day; kept only 90 days), else NIFTYBEES from prices_daily."""
    start = str(_d(as_of) - timedelta(days=LOOKBACK_DAYS))
    try:
        rows = conn.execute("SELECT date, nifty_close FROM market_health WHERE nifty_close>0 AND date>? AND date<=? "
                            "ORDER BY date", (start, str(_d(as_of)))).fetchall()
        if len(rows) > 70:
            return pd.Series({pd.Timestamp(str(r[0])[:10]): float(r[1]) for r in rows}).sort_index()
    except Exception:
        pass
    try:
        rows = conn.execute("SELECT date, nifty50 FROM index_levels WHERE nifty50>0 AND date>? AND date<=? "
                            "ORDER BY date, time", (start, str(_d(as_of)))).fetchall()
        if len(rows) > 60:
            s = pd.Series({pd.Timestamp(str(r[0])[:10]): float(r[1]) for r in rows})
            return s.sort_index()
    except Exception:
        pass
    rows = conn.execute("SELECT date, close FROM prices_daily WHERE symbol='NIFTYBEES' AND date>? AND date<=? "
                        "AND close>0 ORDER BY date", (start, str(_d(as_of)))).fetchall()
    if len(rows) > 60:
        return pd.Series({pd.Timestamp(str(r[0])[:10]): float(r[1]) for r in rows}).sort_index()
    return None


def _context(conn, as_of) -> dict:
    ctx = {"regime": None, "research": {}}
    try:
        r = conn.execute("SELECT regime FROM market_health WHERE date<=? ORDER BY date DESC LIMIT 1",
                         (str(as_of),)).fetchone()
        ctx["regime"] = r[0] if r else None
    except Exception:
        pass
    try:
        ctx["research"] = {r[0]: r[1] for r in conn.execute(
            "SELECT r.symbol, r.rating FROM research_report r JOIN (SELECT symbol, MAX(as_of) m FROM research_report "
            "WHERE as_of<=? GROUP BY symbol) x ON r.symbol=x.symbol AND r.as_of=x.m", (str(as_of),))}
    except Exception:
        pass
    return ctx


# ── signals ──────────────────────────────────────────────────────────────────

def levels(direction, close, atr) -> tuple:
    if not atr or not close:
        return None, None
    if direction == "BULL":
        return round(close - STOP_ATR * atr, 2), round(close + TARGET_ATR * atr, 2)
    return round(close + STOP_ATR * atr, 2), round(close - TARGET_ATR * atr, 2)


def confluence(direction, snap, regime, research_rating) -> tuple:
    """(count, evidence) of independent agreeing evidence for a BULL / BEAR signal."""
    sign = 1 if direction == "BULL" else -1
    ev = {}
    tr = snap.get("tech_rating")
    ev["technical rating"] = tr is not None and tr * sign > 0.1
    ev["volume > 1.5x"] = (snap.get("vol_ratio") or 0) > 1.5
    rs = snap.get("rs_63_pct")
    ev["relative strength"] = rs is not None and rs * sign > 0
    reg = (regime or "").upper()          # market_health: STRONG_BULL / BULL / NEUTRAL / BEAR / HIGH_RISK
    against = ("BEAR", "HIGH_RISK") if sign > 0 else ("BULL", "STRONG_BULL")
    ev["market regime"] = bool(reg) and reg not in against
    ev["candle pattern"] = any(p[1] == direction for p in snap.get("_patterns") or [])
    rr = (research_rating or "").upper()
    ev["research rating"] = rr in (("BUY", "ADD") if sign > 0 else ("REDUCE", "SELL"))
    return sum(ev.values()), ev


def rs_rank(snaps: dict):
    """IBD-style RS rating 1-99: percentile of rs_raw across today's universe (99 = strongest)."""
    raw = sorted((s["_rs_raw"], sym) for sym, s in snaps.items() if s.get("_rs_raw") is not None)
    n = len(raw)
    for i, (_, sym) in enumerate(raw):
        snaps[sym]["rs_rating"] = max(1, min(99, int(round((i + 1) / n * 99)))) if n > 1 else 50
    for s in snaps.values():
        s.setdefault("rs_rating", None)


def run_technical(symbols: list | None = None, as_of=None, conn=None) -> dict:
    """Compute today's technical snapshot and signals for the universe; evaluate open signals."""
    from db.schema import get_connection
    own = conn is None
    conn = conn or get_connection()
    as_of = _d(as_of) if as_of else None
    try:
        ensure_tables(conn)
        if as_of is None:
            r = conn.execute("SELECT MAX(date) FROM prices_daily").fetchone()
            if not r or not r[0]:
                return {"status": "SKIPPED", "reason": "no prices", "rows": 0}
            as_of = _d(r[0])
        if not symbols:
            try:
                from data.dhan import get_tracked_symbols
                symbols = get_tracked_symbols(conn)
            except Exception:
                symbols = [r[0] for r in conn.execute("SELECT DISTINCT symbol FROM fundamental_data")]
        bars = load_bars(conn, symbols, as_of)
        bench = benchmark(conn, as_of)
        ctx = _context(conn, as_of)
        n = sigs = 0
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        snaps = {}
        for sym, df in bars.items():
            if df.index[-1].date() != as_of:          # no bar today: stale, skip rather than repeat yesterday
                continue
            snap = T.snapshot(df, bench)
            if snap:
                snaps[sym] = snap
        rs_rank(snaps)
        for sym, snap in snaps.items():
            conn.execute(f"""INSERT INTO technical_snapshot (symbol, date, close, {', '.join(SNAP_COLS)}, scans_json,
                             created_at) VALUES ({','.join('?' * (len(SNAP_COLS) + 5))})
                             ON CONFLICT(symbol, date) DO UPDATE SET close=excluded.close, """ +
                         ", ".join(f"{c}=excluded.{c}" for c in SNAP_COLS) + ", scans_json=excluded.scans_json",
                         [sym, str(as_of), snap["_close"]] + [snap.get(c) for c in SNAP_COLS] +
                         [json.dumps([h[0] for h in snap["_hits"]]), now])
            n += 1
            for key, name, direction, reason in snap["_hits"]:
                if direction not in ("BULL", "BEAR"):
                    continue
                stop, target = levels(direction, snap["_close"], snap["_atr"])
                cnt, ev = confluence(direction, snap, ctx["regime"], ctx["research"].get(sym))
                conn.execute("""INSERT INTO technical_signal (signal_id, symbol, date, scan, name, direction, reason,
                                    entry, stop, target, atr, horizon, confluence, evidence_json, status, created_at)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN',?)
                                ON CONFLICT(symbol, date, scan) DO UPDATE SET entry=excluded.entry,
                                    stop=excluded.stop, target=excluded.target, confluence=excluded.confluence,
                                    evidence_json=excluded.evidence_json""",
                             (uuid.uuid4().hex[:16], sym, str(as_of), key, name, direction, reason, snap["_close"],
                              stop, target, snap["_atr"], HORIZON, cnt, json.dumps(ev), now))
                sigs += 1
        conn.commit()
        ev = evaluate_signals(conn)
    finally:
        if own:
            conn.close()
    return {"status": "SUCCESS" if n else "EMPTY", "rows": n, "signals": sigs, "as_of": str(as_of),
            "benchmark": bench is not None, "evaluated": ev}


def evaluate_signals(conn) -> dict:
    """Close OPEN signals that reached their target or stop, or ran out of horizon."""
    ensure_tables(conn)
    done = {"TARGET": 0, "STOPPED": 0, "EXPIRED": 0}
    rows = conn.execute("SELECT signal_id, symbol, date, direction, entry, stop, target, horizon FROM technical_signal "
                        "WHERE status='OPEN'").fetchall()
    for sid, sym, d0, direction, entry, stop, target, horizon in rows:
        bars = conn.execute("SELECT date, high, low, close FROM prices_daily WHERE symbol=? AND date>? ORDER BY date "
                            "LIMIT ?", (sym, str(d0)[:10], int(horizon or HORIZON))).fetchall()
        status = when = price = None
        for bd, hi, lo, cl in bars:
            hi, lo = hi or cl, lo or cl
            if direction == "BULL":
                if stop is not None and lo <= stop:
                    status, when, price = "STOPPED", bd, stop
                elif target is not None and hi >= target:
                    status, when, price = "TARGET", bd, target
            else:
                if stop is not None and hi >= stop:
                    status, when, price = "STOPPED", bd, stop
                elif target is not None and lo <= target:
                    status, when, price = "TARGET", bd, target
            if status:
                break
        if status is None and len(bars) >= int(horizon or HORIZON):
            status, when, price = "EXPIRED", bars[-1][0], bars[-1][3]
        if status and entry:
            sign = 1 if direction == "BULL" else -1
            ret = round(sign * (price / entry - 1) * 100, 2)
            risk = abs(entry - stop) if stop else None
            r_mult = round(sign * (price - entry) / risk, 2) if risk else None
            conn.execute("UPDATE technical_signal SET status=?, outcome_date=?, outcome_price=?, return_pct=?, "
                         "r_multiple=? WHERE signal_id=?", (status, str(when)[:10], price, ret, r_mult, sid))
            done[status] += 1
    conn.commit()
    return done


def scan_stats(conn, min_confluence: int = 0) -> list:
    """Per scan: closed signals, win rate (TARGET share), average R, average return, expectancy in R."""
    ensure_tables(conn)
    out = {}
    for scan, name, direction, status, r, ret in conn.execute(
            "SELECT scan, name, direction, status, r_multiple, return_pct FROM technical_signal WHERE confluence>=?",
            (int(min_confluence),)):
        o = out.setdefault(scan, {"scan": scan, "name": name, "direction": direction, "open": 0, "closed": 0,
                                  "target": 0, "stopped": 0, "expired": 0, "_r": [], "_ret": []})
        if status == "OPEN":
            o["open"] += 1
            continue
        o["closed"] += 1
        o[status.lower()] += 1
        if r is not None:
            o["_r"].append(r)
        if ret is not None:
            o["_ret"].append(ret)
    rows = []
    for o in out.values():
        rs, rets = o.pop("_r"), o.pop("_ret")
        o["win_rate_pct"] = round(o["target"] / o["closed"] * 100, 1) if o["closed"] else None
        o["avg_r"] = round(sum(rs) / len(rs), 2) if rs else None
        o["avg_return_pct"] = round(sum(rets) / len(rets), 2) if rets else None
        rows.append(o)
    return sorted(rows, key=lambda o: (-(o["avg_r"] if o["avg_r"] is not None else -99), -o["closed"]))


def todays_signals(conn, as_of=None, direction=None, min_confluence=0, limit=300) -> list:
    ensure_tables(conn)
    d = as_of or (conn.execute("SELECT MAX(date) FROM technical_signal").fetchone() or [None])[0]
    if not d:
        return []
    sql = ("SELECT s.symbol, s.date, s.scan, s.name, s.direction, s.reason, s.entry, s.stop, s.target, s.confluence, "
           "s.evidence_json, s.status, t.tech_rating_label, t.patterns FROM technical_signal s LEFT JOIN "
           "technical_snapshot t ON t.symbol=s.symbol AND t.date=s.date WHERE s.date=? AND s.confluence>=?")
    args = [str(d)[:10], int(min_confluence)]
    if direction:
        sql += " AND s.direction=?"
        args.append(direction.upper())
    sql += " ORDER BY s.confluence DESC, s.symbol LIMIT ?"
    args.append(int(limit))
    cur = conn.execute(sql, args)
    cols = [c[0] for c in cur.description]
    out = []
    for r in cur.fetchall():
        x = dict(zip(cols, r))
        x["evidence"] = json.loads(x.pop("evidence_json") or "{}")
        out.append(x)
    return out


def latest_snapshot(conn, symbols=None, as_of=None) -> dict:
    """{symbol: row} from the newest technical_snapshot at or before as_of (the screener's technical half)."""
    try:
        ensure_tables(conn)
        sql = ("SELECT t.* FROM technical_snapshot t JOIN (SELECT symbol, MAX(date) m FROM technical_snapshot "
               "WHERE date<=? GROUP BY symbol) x ON t.symbol=x.symbol AND t.date=x.m")
        cur = conn.execute(sql, (str(as_of or date.today()),))
    except Exception:
        return {}
    cols = [c[0] for c in cur.description]
    rows = {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}
    if symbols is not None:
        keep = set(symbols)
        rows = {k: v for k, v in rows.items() if k in keep}
    for v in rows.values():
        for key in json.loads(v.get("scans_json") or "[]"):
            v[f"scan_{key}"] = 1
    return rows


def alert_top(conn, as_of=None, min_confluence=4, limit=10) -> dict:
    sig = [s for s in todays_signals(conn, as_of, min_confluence=min_confluence) if s["direction"] in ("BULL", "BEAR")]
    if not sig:
        return {"alerted": 0}
    from alerts.telegram import notify
    lines = [f"{'▲' if s['direction'] == 'BULL' else '▼'} {s['symbol']}: {s['name']} (confluence {s['confluence']}/6, "
             f"entry {s['entry']:.2f}, stop {s['stop']:.2f}, target {s['target']:.2f})" for s in sig[:limit]]
    notify("<b>Technical signals</b> (EOD, for the next session; not advice)\n" + "\n".join(lines),
           category="signals", severity="info", key=f"tech_signals:{sig[0]['date']}")
    return {"alerted": len(lines)}


def run_job() -> dict:
    out = run_technical()
    try:
        from db.schema import get_connection
        conn = get_connection()
        try:
            out["alert"] = alert_top(conn)
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"  technical signal alert: {e}")
    return out


def main(argv=None):
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(prog="python -m research.tech_signals")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--symbols", nargs="+")
    r.add_argument("--date")
    sub.add_parser("evaluate")
    sub.add_parser("stats")
    t = sub.add_parser("today")
    t.add_argument("--min-confluence", type=int, default=0)
    a = ap.parse_args(argv)
    from db.schema import get_connection
    if a.cmd == "run":
        out = run_technical(a.symbols, a.date)
    else:
        conn = get_connection()
        try:
            out = {"evaluate": lambda: evaluate_signals(conn), "stats": lambda: scan_stats(conn),
                   "today": lambda: todays_signals(conn, min_confluence=a.min_confluence)}[a.cmd]()
        finally:
            conn.close()
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
