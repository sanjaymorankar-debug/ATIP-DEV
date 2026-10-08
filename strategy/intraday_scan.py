"""
Intraday scans (SG-08), W28.

The 12:30 "ZPI scan" used to read ai_scores for TODAY -- rows that only exist after the
16:05 post-market run -- so it could never find anything; the 14:45 "power hour" scan only
refreshed quotes. These scans run on what is actually known intraday:

    live      latest live_quotes row per symbol today (REST poll or the W27 stock feed)
    bars      today's stored intraday_bars (DP-03, 15-min) -> session VWAP, last two bars
    context   the PREVIOUS session's ai_scores and technical_indicators (pivot / S1 / R1,
              ATR, 20-day volume average) and the 20 prior daily highs / lows

    SCAN            fires when
    zpi_pullback    prev ZPI >= 60, prev CRI < 50, price pulled back into [S1, pivot] and down 0-3% on the day
    breakout        price above the prior 20-session high with relative volume >= 1.5
    vwap_reclaim    last 15-min bar closed above session VWAP, the bar before below it, prev ATIP >= 50
    momentum        up >= 2% with relative volume >= 1.5 and prev MRI >= 55
    breakdown_risk  prev CRI >= 60 and price below the prior day's low (risk watch, incl. holdings)

relative volume = today's cumulative volume / (20-day average volume x fraction of the
session elapsed). Hits go to intraday_scan_hit (one run_id per run) and, when the
Telegram bot is configured, one digest per run (top 5 per scan). Scans inform; they
place no order and create no W3 decision.

    run_intraday_scans(now=None, notify=True)
    config.json "intraday_scans": {"enabled": true, "times": ["10:30","12:30","14:45"], "min_rel_volume": 1.5}
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, time as _time
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULTS = {"enabled": True, "times": ["10:30", "12:30", "14:45"], "min_rel_volume": 1.5}
SESSION_OPEN, SESSION_CLOSE = _time(9, 15), _time(15, 30)
SCANS = ("zpi_pullback", "breakout", "vwap_reclaim", "momentum", "breakdown_risk")


def settings() -> dict:
    try:
        cfg = json.loads((Path("atip_data") / "config.json").read_text(encoding="utf-8"))
        return {**DEFAULTS, **(cfg.get("intraday_scans") or {})}
    except Exception:
        return dict(DEFAULTS)


def _session_fraction(now: datetime) -> float:
    mins = (datetime.combine(now.date(), min(max(now.time(), SESSION_OPEN), SESSION_CLOSE)) -
            datetime.combine(now.date(), SESSION_OPEN)).total_seconds() / 60
    return max(0.05, min(1.0, mins / 375.0))


def _prev_session(conn, today: date):
    r = conn.execute("SELECT MAX(date) FROM ai_scores WHERE date<?", (str(today),)).fetchone()
    return str(r[0])[:10] if r and r[0] else None


def load_context(conn, today: date) -> dict:
    prev = _prev_session(conn, today)
    if not prev:
        return {}
    ctx = {}
    for r in conn.execute("SELECT s.symbol, s.atip_score, s.zpi, s.cri, s.mri, s.`signal`, t.pivot, t.s1, t.r1, "
                          "t.atr_14, t.volume_sma20 FROM ai_scores s LEFT JOIN technical_indicators t ON "
                          "t.symbol=s.symbol AND t.date=s.date WHERE s.date=?", (prev,)):
        ctx[r[0]] = dict(zip(("symbol", "atip", "zpi", "cri", "mri", "signal", "pivot", "s1", "r1", "atr",
                              "vol20"), r))
    syms = list(ctx)
    for i in range(0, len(syms), 400):
        chunk = syms[i:i + 400]
        q = ",".join("?" * len(chunk))
        for sym, hi20, lo1 in conn.execute(
                f"SELECT symbol, MAX(high), (SELECT low FROM prices_daily p2 WHERE p2.symbol=p.symbol AND p2.date=?) "
                f"FROM prices_daily p WHERE symbol IN ({q}) AND date<=? AND date>DATE(?, '-30 days') GROUP BY symbol",
                (prev, *chunk, prev, prev)):
            ctx[sym]["high20"], ctx[sym]["prev_low"] = hi20, lo1
    return {"prev_session": prev, "stocks": ctx}


def load_live(conn, today: date, now: datetime | None = None) -> dict:
    """Latest quote per symbol today, stamped no later than `now` (no lookahead on a re-run)."""
    upto = (now or datetime.combine(today, SESSION_CLOSE)).strftime("%Y-%m-%d %H:%M:%S")
    out = {}
    for r in conn.execute("SELECT q.symbol, q.ltp, q.volume, q.chg_pct, q.timestamp FROM live_quotes q JOIN "
                          "(SELECT symbol, MAX(timestamp) ts FROM live_quotes WHERE timestamp>=? AND "
                          "REPLACE(SUBSTR(timestamp,1,19),'T',' ')<=? GROUP BY symbol) m "
                          "ON m.symbol=q.symbol AND m.ts=q.timestamp", (str(today), upto)):
        if r[1]:
            out[r[0]] = {"ltp": float(r[1]), "volume": r[2], "chg_pct": r[3], "ts": r[4]}
    return out


def load_bars(conn, today: date, now: datetime | None = None) -> dict:
    """Today's bars that had CLOSED by `now`: a bar stamped at its start (ts) is complete
    at ts + interval_min."""
    upto = (now or datetime.combine(today, SESSION_CLOSE)).strftime("%Y-%m-%d %H:%M:%S")
    bars = {}
    for sym, ts, h, l, c, v in conn.execute(
            "SELECT symbol, ts, high, low, close, volume FROM intraday_bars WHERE ts>=? AND "
            "DATETIME(REPLACE(SUBSTR(ts,1,19),'T',' '), '+' || COALESCE(interval_min,15) || ' minutes')<=? "
            "ORDER BY symbol, ts", (str(today), upto)):
        bars.setdefault(sym, []).append((ts, h, l, c, v or 0))
    return bars


def _vwap(b):
    pv = sum((h + l + c) / 3 * v for _, h, l, c, v in b if v)
    vol = sum(v for *_, v in b)
    return pv / vol if vol else None


def evaluate(ctx: dict, live: dict, bars: dict, now: datetime, min_rv=1.5) -> list:
    frac = _session_fraction(now)
    hits = []
    for sym, k in ctx.get("stocks", {}).items():
        q = live.get(sym)
        b = bars.get(sym, [])
        px = q["ltp"] if q else (b[-1][3] if b else None)
        if not px:
            continue
        cum_vol = (q or {}).get("volume") or (sum(x[4] for x in b) if b else None)
        rv = (cum_vol / (k["vol20"] * frac)) if (cum_vol and k.get("vol20")) else None
        chg = (q or {}).get("chg_pct")

        def hit(scan, score, **d):
            hits.append({"scan": scan, "symbol": sym, "price": px, "score": round(score, 2),
                         "details": {**d, "rel_volume": round(rv, 2) if rv else None, "chg_pct": chg,
                                     "prev_atip": k.get("atip")}})
        if (k.get("zpi") or 0) >= 60 and (k.get("cri") or 100) < 50 and k.get("s1") and k.get("pivot") \
                and k["s1"] <= px <= k["pivot"] and chg is not None and -3 <= chg <= 0:
            hit("zpi_pullback", k["zpi"], s1=k["s1"], pivot=k["pivot"])
        if k.get("high20") and px > k["high20"] and rv and rv >= min_rv:
            hit("breakout", 50 + min(50, (px / k["high20"] - 1) * 1000 + rv * 5), high20=k["high20"])
        if len(b) >= 3 and (k.get("atip") or 0) >= 50:
            vw_prev, vw_now = _vwap(b[:-1]), _vwap(b)
            if vw_prev and vw_now and b[-2][3] < vw_prev and b[-1][3] > vw_now and px > vw_now:
                hit("vwap_reclaim", k["atip"], vwap=round(vw_now, 2), bar=str(b[-1][0]))
        if chg is not None and chg >= 2 and rv and rv >= min_rv and (k.get("mri") or 0) >= 55:
            hit("momentum", min(100, chg * 10 + rv * 5), mri=k.get("mri"))
        if (k.get("cri") or 0) >= 60 and k.get("prev_low") and px < k["prev_low"]:
            hit("breakdown_risk", k["cri"], prev_low=k["prev_low"])
    return hits


def run_intraday_scans(now: datetime | None = None, notify: bool = True) -> dict:
    from db.schema import get_connection, log_job
    now = now or datetime.now()
    s = settings()
    if not s.get("enabled", True):
        return {"status": "SKIPPED", "rows": 0, "reason": "intraday_scans.enabled is false"}
    conn = get_connection()
    try:
        today = now.date()
        ctx = load_context(conn, today)
        if not ctx:
            return {"status": "SKIPPED", "rows": 0, "reason": "no previous scored session"}
        live, bars = load_live(conn, today, now), load_bars(conn, today, now)
        if not live and not bars:
            return {"status": "SKIPPED", "rows": 0, "reason": f"no live quotes or intraday bars for {today}"}
        hits = evaluate(ctx, live, bars, now, float(s.get("min_rel_volume") or 1.5))
        run_id = uuid.uuid4().hex[:12]
        conn.executemany("INSERT INTO intraday_scan_hit (run_id,run_at,session,scan,symbol,price,score,details_json) "
                         "VALUES (?,?,?,?,?,?,?,?)",
                         [(run_id, now, str(today), h["scan"], h["symbol"], h["price"], h["score"],
                           json.dumps(h["details"], default=str)) for h in hits])
        conn.commit()
        counts = {sc: sum(1 for h in hits if h["scan"] == sc) for sc in SCANS}
        if notify and hits:
            _notify(hits, now)
        log.info(f"  ✓ Intraday scans {now:%H:%M}: {counts} (context {ctx['prev_session']}, "
                 f"{len(live)} live quotes, {len(bars)} symbols with bars)")
        log_job("intraday_scans", "SUCCESS", len(hits))
        return {"status": "SUCCESS", "rows": len(hits), "run_id": run_id, "counts": counts,
                "context_session": ctx["prev_session"], "live_quotes": len(live), "bar_symbols": len(bars)}
    finally:
        conn.close()


def _notify(hits, now):
    try:
        from alerts.telegram import send_telegram, fmt
    except Exception:
        return
    lines = []
    for sc in SCANS:
        top = sorted((h for h in hits if h["scan"] == sc), key=lambda h: -h["score"])[:5]
        if top:
            lines.append(f"\n<b>{sc}</b>: " + ", ".join(f"{h['symbol']} {h['price']:.1f}" for h in top))
    try:
        send_telegram(fmt("🔎", f"Intraday scans {now:%H:%M}", "".join(lines)))
    except Exception as e:
        log.debug(f"  scan notify: {e}")


def latest_hits(conn, session=None, scan=None, limit=200) -> dict:
    session = session or (conn.execute("SELECT MAX(session) FROM intraday_scan_hit").fetchone() or [None])[0]
    if not session:
        return {"session": None, "runs": [], "hits": []}
    run = conn.execute("SELECT run_id, MAX(run_at) FROM intraday_scan_hit WHERE session=? GROUP BY run_id ORDER BY 2 "
                       "DESC LIMIT 1", (str(session)[:10],)).fetchone()
    sql, args = "SELECT * FROM intraday_scan_hit WHERE run_id=?", [run[0]]
    if scan:
        if scan not in SCANS:
            raise ValueError(f"scan must be one of {', '.join(SCANS)}")
        sql += " AND scan=?"
        args.append(scan)
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY scan, score DESC LIMIT ?", (*args, int(limit)))]
    for r in rows:
        r["details"] = json.loads(r.pop("details_json") or "{}")
    runs = [{"run_id": a, "run_at": b, "hits": c} for a, b, c in conn.execute(
        "SELECT run_id, MAX(run_at), COUNT(*) FROM intraday_scan_hit WHERE session=? GROUP BY run_id ORDER BY 2 DESC",
        (str(session)[:10],))]
    return {"session": str(session)[:10], "run_id": run[0], "run_at": run[1], "runs": runs, "hits": rows}
