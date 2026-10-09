"""
Stock detail view + table filtering for the main dashboard (W26).

    GET /api/stock/{symbol}/history?sessions=400
        prices (OHLCV), ATIP score history, signal log with outcomes, latest
        technicals (W21 ext included), key stats (52-week range, returns, average
        volume), the LIVE holding and ATIP's orders / order rules for the symbol,
        and chart_marks (chart_marks() below): the stored technical signals and
        candle patterns (technical_signal / technical_snapshot, research/tech_signals.py)
        and the chart patterns in place now with their lines (research/patterns.py).
        Read-only; authz falls under the default dashboard:read rule.

    GET /api/stock/{symbol}/intraday?date=YYYY-MM-DD&days=1..5&interval=15
        W40, stock_intraday() below: the `interval`-minute bars of intraday_bars (that interval_min only:
        tick capture's 1-minute bars share the table) of the last `days` sessions with bars up to `date`
        (default: the latest), 09:15-15:30 IST only, each with the session VWAP (cumulative typical price x
        volume / volume, restarting every session); each session's previous daily close (prices_daily); the
        intraday scan hits of those days as marks (research/intraday_signals.py's intraday_signal and
        strategy/intraday_scan.py's intraday_scan_hit); `message` says why when there are no bars. Read-only;
        dashboard:read like /history (enterprise/authz.py: GET ^/api/).

    ASSETS (CSS + HTML + JS, injected before </body> by dashboard/server.py)
        * clicking any row / card that carries data-sym opens the stock panel:
          price chart (line or candles, 1M / 3M / 6M / 1Y / All, volume, BUY / SELL
          signal markers, crosshair), ATIP score chart, and tabs for scores,
          W39 (DB-20, "history like any trading software"): 3Y / 5Y ranges (up to
          2500 sessions, ~10 years, are loaded), daily / weekly / monthly bars, SMA
          20 / 50 / 200, EMA 21 and Bollinger (20, 2) overlays computed on the whole
          series so the left edge is right, a log scale, and 3Y / 5Y / since-first-bar
          returns;
          signals, technicals, position & orders; Buy / Sell open the existing
          order modal. Esc or the backdrop closes it; the page auto-refresh is
          held while it is open.
        * the Patterns toggle (on by default, remembered for the session) draws on
          the price chart: a diamond on the day of a chart-pattern breakout, a dot
          on the day of any other technical signal (below the bar for BULL, above for
          BEAR), a small dot at the bar for a bullish / bearish candle pattern, and
          the lines of each pattern in place (box top / bottom, neckline,
          resistance / support, channel or wedge lines) dashed from where the pattern
          starts to the last bar. On weekly / monthly bars the marks of a week / month
          gather on its bar. The crosshair line names them. Plain SVG, as before.
        * W40: RSI 14 and MACD 12-26-9 panels under the price chart (RSI 14 / MACD buttons, off by default,
          remembered for the session like Patterns), with research/technicals.py's definitions -- RSI: Wilder's
          averages = pandas ewm(alpha=1/14, adjust=False, min_periods=14) of the gains / losses, 100 with no
          loss; MACD: ewm(span, adjust=False) EMAs 12 and 26, signal = EMA 9 of the line, histogram = line -
          signal, drawn from bar 26 / 34 -- computed on the whole daily / weekly / monthly series like the
          overlays, then cut to the range; they share the x-axis, the crosshair and the tooltip. The math
          sits between the "indicator math" markers (tests/test_w40_stock_chart.py runs it in node).
        * W40: 1D 15m / 5D 15m switch to the intraday view (/intraday, 5 sessions loaded; 1D shows the last,
          the overlays and RSI / MACD run over all of them): candles, volume, the session VWAP, the previous
          close dashed, the intraday scan marks under the Patterns toggle (▲ / ▼ intraday_signal, ● intraday
          scan), each session on a fixed 09:15-15:30 axis and the sessions side by side (no overnight gap).
          Without bars (no Dhan Data API) one line says why. Range / Daily / Weekly / Monthly go back.
        * ATIP Scores table: search matches symbol / company only, the signal
          filter matches the signal value exactly (the old filter matched the
          row text, so every row matched "sell" / "buy" through its order
          buttons), a Show 50 / 100 / 250 / All limit and a row count. Signal
          History gets the same exact filters; every other tab table gets a
          filter box. Sort arrows on headers; sort + filters survive the
          5-minute auto-refresh (sessionStorage, per tab).

Kept out of the server.py f-string on purpose: no doubled braces to get wrong.
"""

# No `from __future__ import annotations` (FastAPI must see the real types).
import bisect
import re
from datetime import date, datetime, timedelta

from fastapi.responses import JSONResponse

SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9&\-_.]{0,29}$")


def _rows(conn, sql, args=()):
    try:
        cur = conn.execute(sql, args)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:
        return []


def _one(conn, sql, args=()):
    r = _rows(conn, sql, args)
    return r[0] if r else None


def stock_history(conn, symbol: str, sessions: int = 400) -> dict:
    sym = (symbol or "").strip().upper()
    if not SYMBOL_RE.match(sym):
        raise ValueError("invalid symbol")
    sessions = max(20, min(int(sessions), 2500))
    px = _rows(conn, "SELECT date, open, high, low, close, volume, delivery_pct FROM prices_daily "
                     "WHERE symbol=? AND close>0 ORDER BY date DESC LIMIT ?", (sym, sessions))[::-1]
    if not px:
        raise LookupError(f"no price history for {sym}")
    for p in px:
        p["date"] = str(p["date"])[:10]
    closes = [float(p["close"]) for p in px]
    last = closes[-1]

    def ret(n):
        return round((last / closes[-1 - n] - 1) * 100, 2) if len(closes) > n and closes[-1 - n] else None
    yr = px[-250:]
    vols = [float(p["volume"] or 0) for p in px[-20:]]
    stats = {"last_close": last, "last_date": px[-1]["date"],
             "prev_close": closes[-2] if len(closes) > 1 else None,
             "change_pct": round((last / closes[-2] - 1) * 100, 2) if len(closes) > 1 and closes[-2] else None,
             "high_52w": max(float(p["high"] or p["close"]) for p in yr),
             "low_52w": min(float(p["low"] or p["close"]) for p in yr),
             "ret_1w": ret(5), "ret_1m": ret(21), "ret_3m": ret(63), "ret_6m": ret(126), "ret_1y": ret(250),
             "ret_3y": ret(750), "ret_5y": ret(1250),
             "ret_all": round((last / closes[0] - 1) * 100, 2) if closes[0] else None, "first_date": px[0]["date"],
             "high_all": max(float(p["high"] or p["close"]) for p in px),
             "low_all": min(float(p["low"] or p["close"]) for p in px),
             "avg_volume_20d": round(sum(vols) / len(vols)) if vols else None, "sessions": len(px)}
    scores = _rows(conn, "SELECT date, atip_score, atip_rank, vpi, mri, rri, zpi, cri, acs, spi, `signal`, confidence, "
                         "beta_1y, regime, top_factor_1, top_factor_2 FROM ai_scores WHERE symbol=? ORDER BY date",
                   (sym,))
    for s in scores:
        s["date"] = str(s["date"])[:10]
    sigs = _rows(conn, "SELECT id, signal_date, `signal`, entry_price, atip_score, zpi, cri, model_version "
                       "FROM signal_log WHERE symbol=? AND duplicate_of IS NULL ORDER BY signal_date DESC LIMIT 200",
                 (sym,))
    for s in sigs:
        s["signal_date"] = str(s["signal_date"])[:10]
        outs = _rows(conn, "SELECT threshold_pct, hit, hit_date, max_favourable_pct, max_adverse_pct, sessions_tracked "
                           "FROM signal_outcome WHERE signal_id=? ORDER BY threshold_pct", (s["id"],))
        hits = [o for o in outs if o.get("hit")]
        s["first_hit"] = (min(hits, key=lambda o: str(o["hit_date"])) if hits else None)
        s["best_pct"] = next((o["max_favourable_pct"] for o in outs if o.get("max_favourable_pct") is not None), None)
        s["worst_pct"] = next((o["max_adverse_pct"] for o in outs if o.get("max_adverse_pct") is not None), None)
        s["sessions_tracked"] = max((o.get("sessions_tracked") or 0 for o in outs), default=None)
    tech = _one(conn, "SELECT * FROM technical_indicators WHERE symbol=? ORDER BY date DESC LIMIT 1", (sym,)) or {}
    ext = _one(conn, "SELECT * FROM technical_ext WHERE symbol=? ORDER BY date DESC LIMIT 1", (sym,)) or {}
    for d in (tech, ext):
        d.pop("id", None)
        d.pop("created_at", None)
    holding = _one(conn, "SELECT date, qty, avg_price, cmp, current_val, pnl, pnl_pct, weight_pct FROM portfolio_holdings "
                         "WHERE symbol=? AND date=(SELECT MAX(date) FROM portfolio_holdings) AND qty>0", (sym,))
    paper = _one(conn, "SELECT quantity, avg_price, realized_pnl FROM paper_position WHERE symbol=? AND quantity>0", (sym,))
    rules = _rows(conn, "SELECT id, side, trigger_type, trigger_value, resolved_trigger_price, quantity_type, "
                        "quantity_value, status, created_at, triggered_at FROM order_rules WHERE symbol=? "
                        "ORDER BY created_at DESC LIMIT 20", (sym,))
    orders = _rows(conn, "SELECT timestamp, transaction_type, quantity, order_type, price, mode, status, error "
                         "FROM order_log WHERE symbol=? ORDER BY timestamp DESC LIMIT 20", (sym,))
    oms = _rows(conn, "SELECT created_at, side, quantity, filled_quantity, avg_fill_price, status, strategy_id "
                      "FROM oms_order WHERE symbol=? ORDER BY created_at DESC LIMIT 20", (sym,))
    name = None
    try:
        from data.companies import load_company_names
        name = load_company_names().get(sym)
    except Exception:
        pass
    return {"symbol": sym, "name": name, "stats": stats, "prices": px, "scores": scores, "signals": sigs,
            "technicals": tech, "technical_ext": ext, "holding": holding, "paper_position": paper,
            "order_rules": rules, "orders": orders, "oms_orders": oms, "chart_marks": chart_marks(conn, sym, px)}


MARK_LIMIT = 2000           # technical signals sent to the chart (newest first), enough for years of one stock
PATTERN_BARS = 400          # bars the live pattern read uses (the 20:30 job reads ~600 calendar days)


def chart_marks(conn, sym: str, px: list) -> dict:
    """What the price chart draws from research/tech_signals.py's tables and research/patterns.py, read-only:

        signals    technical_signal rows in the chart's date range: date, scan, name, direction, whether
                   the scan is a chart-pattern breakout (research/technicals.py PATTERN_SCANS), its status
                   and, for a pattern, the reason (which names the pattern's levels)
        candles    candle patterns from technical_snapshot.patterns, one per name per date, with their side
                   (technicals.CANDLE_SIDES); in_place: technical_snapshot.chart_patterns, the chart
                   patterns the 20:30 run listed that day
        patterns   the patterns in place on the stored bars right now (patterns.active on the last
                   PATTERN_BARS bars, the same rules the 20:30 run applies), each with its lines (box top /
                   bottom, neckline, resistance or support, channel or wedge lines) from where it starts
                   to the last bar, and the scan the last bar fired, if any
    """
    out = {"signals": [], "candles": [], "in_place": [], "patterns": []}
    if not px:
        return out
    first = px[0]["date"]
    try:
        from research.technicals import CANDLE_SIDES, PATTERN_SCANS
    except Exception:                                   # no pandas / numpy: the chart simply has no marks
        return out
    rows = _rows(conn, "SELECT date, scan, name, direction, reason, status, r_multiple FROM technical_signal "
                       "WHERE symbol=? AND date>=? ORDER BY date DESC LIMIT ?", (sym, first, MARK_LIMIT))
    for r in rows[::-1]:
        pat = r["scan"] in PATTERN_SCANS
        out["signals"].append({"date": str(r["date"])[:10], "scan": r["scan"], "name": r["name"] or r["scan"],
                               "direction": r["direction"], "pattern": pat, "status": r["status"],
                               "r_multiple": r["r_multiple"], "reason": r["reason"] if pat else None})
    for r in _rows(conn, "SELECT date, patterns, chart_patterns FROM technical_snapshot WHERE symbol=? AND date>=? "
                         "AND (patterns IS NOT NULL OR chart_patterns IS NOT NULL) ORDER BY date", (sym, first)):
        d = str(r["date"])[:10]
        for nm in dict.fromkeys(x.strip() for x in str(r["patterns"] or "").split(",") if x.strip()):
            out["candles"].append({"date": d, "name": nm, "side": CANDLE_SIDES.get(nm, "NEUTRAL")})
        if r["chart_patterns"]:
            out["in_place"].append({"date": d, "text": r["chart_patterns"]})
    out["patterns"] = _active_patterns(px[-PATTERN_BARS:])
    return out


def _active_patterns(px: list) -> list:
    if len(px) < 60:
        return []
    try:
        import pandas as pd
        from research import patterns as P
        from research import technicals as T
        df = pd.DataFrame(px, columns=["date", "open", "high", "low", "close", "volume"])
        df.index = pd.to_datetime(df.pop("date"))
        df = df.apply(pd.to_numeric, errors="coerce")
        for c in ("open", "high", "low"):
            df[c] = df[c].fillna(df["close"])
        df["volume"] = df["volume"].fillna(0)
        return P.active(T.indicators(df))
    except Exception:
        return []


# ── W40: intraday chart ─────────────────────────────────────────────────────

SESSION_OPEN_MIN, SESSION_CLOSE_MIN = 9 * 60 + 15, 15 * 60 + 30    # NSE regular session; bars are stored in IST
INTRADAY_MAX_DAYS = 5
# strategy/intraday_scan.py (SG-08) writes intraday_scan_hit without a side; these are its scans
SG08_SCANS = {"zpi_pullback": ("ZPI pullback", "BULL"), "breakout": ("20-session high breakout", "BULL"),
              "vwap_reclaim": ("VWAP reclaim", "BULL"), "momentum": ("Momentum on volume", "BULL"),
              "breakdown_risk": ("Breakdown risk", "BEAR")}


def _dt(v):
    s = str(v or "")[:19].replace("T", " ")
    for fmt, n in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16)):
        try:
            return datetime.strptime(s[:n], fmt)
        except ValueError:
            pass
    return None


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def stock_intraday(conn, symbol: str, day=None, days: int = 1, interval: int = 15) -> dict:
    """The intraday chart's data, read-only (intraday_bars, prices_daily, intraday_signal, intraday_scan_hit):

        bars       the `interval`-minute bars (interval_min = interval: 1-minute bars from tick capture sit in
                   the same table) of the last `days` (1..5) sessions that have bars, up to `day` (default:
                   the latest), regular session only (a bar starting 09:15..15:29 IST); each with `vwap`, the
                   session VWAP = cumulative typical price (H + L + C) / 3 x volume / cumulative volume,
                   restarting every session (None until the session has volume)
        sessions   per session: date, first / last bar index, OHLCV, closing VWAP and prev_close, the last
                   prices_daily close before that date (prev_close_date says which)
        prev_close the previous daily close of the last session shown
        marks      the intraday scan hits of those days: research/intraday_signals.py's intraday_signal
                   ("signal", on the bar holding the last minute of its trigger 15-minute bar, `time` = when that
                   bar closed) and strategy/intraday_scan.py's intraday_scan_hit ("scan", on the bar holding
                   run_at - interval: the last one complete when the scan ran, `time` = run_at); `bar` is the
                   index into bars, None when that bar is not stored
        message    why there is nothing to draw, or None
    """
    sym = (symbol or "").strip().upper()
    if not SYMBOL_RE.match(sym):
        raise ValueError("invalid symbol")
    interval, days = int(interval), int(days)
    if not 1 <= interval <= 75:
        raise ValueError("interval must be 1..75 minutes")
    if not 1 <= days <= INTRADAY_MAX_DAYS:
        raise ValueError(f"days must be 1..{INTRADAY_MAX_DAYS}")
    want = date.fromisoformat(str(day)[:10]) if day else None
    out = {"symbol": sym, "interval": interval, "days": days, "date": None,
           "requested_date": str(want) if want else None, "session_open": "09:15", "session_close": "15:30",
           "timezone": "Asia/Kolkata", "bars": [], "sessions": [], "prev_close": None, "prev_close_date": None,
           "marks": [], "message": None}
    lim = " AND ts<?" if want else ""
    args = (sym, interval) + ((str(want + timedelta(days=1)),) if want else ())
    cand = [str(r["d"])[:10] for r in _rows(conn, "SELECT DISTINCT DATE(ts) AS d FROM intraday_bars WHERE symbol=? AND "
                                                  "interval_min=?" + lim + " ORDER BY d DESC LIMIT ?",
                                            args + (days + 5,)) if r["d"]]
    by_day = {}
    if cand:
        rows = _rows(conn, "SELECT ts, open, high, low, close, volume FROM intraday_bars WHERE symbol=? "
                           "AND interval_min=? AND ts>=? AND ts<? ORDER BY ts",
                     (sym, interval, min(cand), str(date.fromisoformat(max(cand)) + timedelta(days=1))))
        for r in rows:
            t, c = _dt(r["ts"]), _num(r["close"])
            if t is None or not c or not SESSION_OPEN_MIN <= t.hour * 60 + t.minute < SESSION_CLOSE_MIN:
                continue
            by_day.setdefault(str(t.date()), {})[t] = r            # one bar per start (a duplicate ts keeps the last)
    sess = sorted(by_day)[-days:]
    if want and (not sess or sess[-1] != str(want)):
        sess = []
    if not sess:
        out["message"] = _no_intraday_reason(conn, sym, interval, want, sorted(by_day))
        return out
    bars = []
    for d in sess:
        cpv = cv = 0.0
        first = len(bars)
        for t in sorted(by_day[d]):
            r = by_day[d][t]
            c = _num(r["close"])
            o, h, lo = _num(r["open"]) or c, _num(r["high"]) or c, _num(r["low"]) or c
            v = max(_num(r["volume"]) or 0.0, 0.0)
            v = int(v) if v.is_integer() else v
            cpv += (h + lo + c) / 3 * v
            cv += v
            bars.append({"ts": t.strftime("%Y-%m-%d %H:%M:%S"), "date": d, "time": t.strftime("%H:%M"), "open": o,
                         "high": h, "low": lo, "close": c, "volume": v,
                         "vwap": cpv / cv if cv > 0 else None})
        seg = bars[first:]
        pc = _one(conn, "SELECT date, close FROM prices_daily WHERE symbol=? AND date<? AND close>0 ORDER BY date DESC "
                        "LIMIT 1", (sym, d))
        out["sessions"].append({"date": d, "first": first, "last": len(bars) - 1, "open": seg[0]["open"],
                                "high": max(b["high"] for b in seg), "low": min(b["low"] for b in seg),
                                "close": seg[-1]["close"], "volume": sum(b["volume"] for b in seg),
                                "vwap": seg[-1]["vwap"], "prev_close": _num(pc["close"]) if pc else None,
                                "prev_close_date": str(pc["date"])[:10] if pc else None})
    out.update(bars=bars, date=sess[-1], prev_close=out["sessions"][-1]["prev_close"],
               prev_close_date=out["sessions"][-1]["prev_close_date"],
               marks=_intraday_marks(conn, sym, sess, bars, interval))
    return out


def _no_intraday_reason(conn, sym, interval, want, other_days) -> str:
    what = f"{interval}-minute bars"
    if want:
        if other_days:
            return (f"No {what} for {sym} on {want} in the 09:15-15:30 session; the latest session before it is "
                    f"{other_days[-1]}.")
        prior = _one(conn, "SELECT MAX(ts) AS ts FROM intraday_bars WHERE symbol=? AND interval_min=? AND ts<?",
                     (sym, interval, str(want)))
        if prior and prior["ts"]:
            return f"No {what} for {sym} on {want}; the latest stored before it is from {str(prior['ts'])[:10]}."
    if other_days:
        return f"No {what} for {sym} inside the 09:15-15:30 session."
    if not _one(conn, "SELECT 1 AS x FROM intraday_bars LIMIT 1"):
        return ("No intraday bars are stored: they come from the Dhan Data API (15-minute bars every 30 minutes in "
                "market hours), which is not connected or has not run yet.")
    others = [r["interval_min"] for r in _rows(conn, "SELECT DISTINCT interval_min FROM intraday_bars WHERE symbol=? "
                                                     "ORDER BY interval_min", (sym,))]
    if others:
        return (f"No {what} for {sym}; it has " + ", ".join(f"{m}-minute" for m in others) +
                " bars (interval=" + str(others[0]) + ").")
    return (f"No {what} are stored for {sym}: the Dhan intraday fetch (every 30 minutes) covers the tracked "
            "symbols only, and this stock has none yet.")


def _intraday_marks(conn, sym, sess, bars, interval) -> list:
    starts = [datetime.strptime(b["ts"], "%Y-%m-%d %H:%M:%S") for b in bars]
    step = timedelta(minutes=interval)

    def bar_at(d, t):
        """index of the stored bar of session d whose [start, start + interval) holds t, None if not stored"""
        i = bisect.bisect_right(starts, t) - 1
        return i if i >= 0 and bars[i]["date"] == d and t < starts[i] + step else None
    sec = timedelta(seconds=1)
    marks = []
    try:
        from research.intraday_signals import INTERVAL as SIG_IV, SCANS
    except Exception:                                   # no pandas: names fall back to the scan id
        SIG_IV, SCANS = 15, {}
    for r in _rows(conn, "SELECT signal_id, date, scan, direction, bar_ts, price, level, reason, seen_at, seen_price, "
                         "ret_close_pct, excess_close_pct FROM intraday_signal WHERE symbol=? AND date>=? AND date<=? "
                         "ORDER BY bar_ts", (sym, sess[0], sess[-1])):
        t, d = _dt(r["bar_ts"]), str(r["date"])[:10]
        if d not in sess:
            continue
        done = t + timedelta(minutes=SIG_IV) if t else None           # the trigger 15-minute bar closes here
        marks.append({"source": "signal", "date": d, "time": done.strftime("%H:%M") if done else None,
                      "bar": bar_at(d, done - sec) if done else None, "scan": r["scan"],
                      "name": (SCANS.get(r["scan"]) or (r["scan"],))[0], "direction": r["direction"],
                      "price": _num(r["price"]), "level": _num(r["level"]), "reason": r["reason"],
                      "seen_at": str(r["seen_at"])[11:16] if r["seen_at"] else None,
                      "seen_price": _num(r["seen_price"]),
                      "ret_close_pct": _num(r["ret_close_pct"]), "excess_close_pct": _num(r["excess_close_pct"])})
    for r in _rows(conn, "SELECT run_at, session, scan, price, score FROM intraday_scan_hit WHERE symbol=? "
                         "AND session>=? AND session<=? ORDER BY run_at", (sym, sess[0], sess[-1])):
        t, d = _dt(r["run_at"]), str(r["session"])[:10]
        if d not in sess:
            continue
        name, side = SG08_SCANS.get(r["scan"], (r["scan"], "BOTH"))
        marks.append({"source": "scan", "date": d, "time": t.strftime("%H:%M") if t else None,
                      "bar": bar_at(d, t - step) if t else None, "scan": r["scan"],
                      "name": name, "direction": side, "price": _num(r["price"]), "level": None,
                      "reason": f"score {float(r['score']):.1f}" if _num(r["score"]) is not None else None})
    marks.sort(key=lambda m: (m["date"], m["time"] or ""))
    return marks


def register(app, get_connection, json_safe):
    @app.get("/api/stock/{symbol}/history")
    async def api_stock_history(symbol: str, sessions: int = 400):
        from starlette.concurrency import run_in_threadpool

        def go():
            conn = get_connection()
            try:
                return JSONResponse(json_safe(stock_history(conn, symbol, sessions)))
            except LookupError as e:
                return JSONResponse({"error": str(e)}, status_code=404)
            except (ValueError, TypeError) as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            finally:
                conn.close()
        return await run_in_threadpool(go)

    @app.get("/api/stock/{symbol}/intraday")
    async def api_stock_intraday(symbol: str, date: str = None, days: int = 1, interval: int = 15):
        """W40: intraday bars, session VWAP, previous close and scan marks (stock_intraday above)."""
        from starlette.concurrency import run_in_threadpool

        def go():
            conn = get_connection()
            try:
                return JSONResponse(json_safe(stock_intraday(conn, symbol, date, days, interval)))
            except (ValueError, TypeError) as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            finally:
                conn.close()
        return await run_in_threadpool(go)


ASSETS = r"""
<style>
th[data-sorted="asc"]::after{content:" \25B2";color:#38bdf8;font-size:9px}
th[data-sorted="desc"]::after{content:" \25BC";color:#38bdf8;font-size:9px}
tr[data-sym]{cursor:pointer}.tod-card[data-sym]{cursor:pointer}
.tfilter{display:flex;gap:6px;align-items:center;margin:0 0 8px;flex-wrap:wrap}.tfilter .cnt{font-size:11px;color:#64748b}
#sv-back{position:fixed;inset:0;background:rgba(2,6,23,.72);display:none;z-index:900;overflow:auto;padding:24px 12px}
#sv-back.on{display:block}
#sv{max-width:1040px;margin:0 auto;background:#1e293b;border:1px solid #334155;border-radius:12px;padding:14px 16px;color:#e2e8f0}
#sv .hd{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;flex-wrap:wrap}
#sv .sym{font-size:20px;font-weight:700}#sv .nm{font-size:12px;color:#94a3b8}
#sv .px{font-size:22px;font-weight:700}#sv .up{color:#059669}#sv .dn{color:#dc2626}#sv .mut{color:#64748b}
#sv .btns button{border:none;border-radius:6px;padding:6px 14px;margin-left:4px;cursor:pointer;font-weight:600;color:#fff}
#sv .b-buy{background:#059669}#sv .b-sell{background:#dc2626}#sv .b-x{background:#334155}
#sv .bar{display:flex;gap:4px;flex-wrap:wrap;margin:10px 0 6px;align-items:center}
#sv .bar button{background:#0f172a;border:1px solid #334155;color:#94a3b8;border-radius:6px;padding:3px 10px;cursor:pointer;font-size:11.5px}
#sv .bar button.on{background:#2563eb;color:#fff;border-color:#2563eb}
#sv .sp{flex:1}
#sv svg{width:100%;display:block;background:#0f172a;border-radius:8px}
#sv .tip{font-size:11px;color:#cbd5e1;min-height:16px;margin:4px 2px}
#sv #svnoid{background:#0f172a;border-radius:8px;padding:14px 12px;margin:0;font-size:12px}
#sv .kv{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:6px;margin:10px 0}
#sv .kv div{background:#0f172a;border:1px solid #334155;border-radius:8px;padding:6px 9px}
#sv .kv span{display:block;font-size:10px;color:#64748b;text-transform:uppercase}
#sv .kv b{font-size:13px}
#sv .tabs2{display:flex;gap:4px;margin:12px 0 8px;flex-wrap:wrap}
#sv .tabs2 div{padding:4px 11px;border-radius:6px;font-size:12px;cursor:pointer;border:1px solid #334155;color:#94a3b8}
#sv .tabs2 div.on{background:#2563eb;color:#fff;border-color:#2563eb}
#sv .pane{display:none;max-height:340px;overflow:auto}#sv .pane.on{display:block}
#sv table{font-size:11.5px}
</style>
<div id="sv-back" onclick="if(event.target===this)svClose()"><div id="sv" role="dialog" aria-modal="true"></div></div>
<script>
(function(){
var S={data:null,range:'1Y',kind:'line',sym:null,iv:'D',ov:{},log:false,pat:st().svpat!==false,
  ind:{rsi:st().svrsi===true,macd:st().svmacd===true},ix:0,ikind:'candle',intra:null,intraFor:null,ierr:null};
function st(){try{return JSON.parse(sessionStorage.getItem('atip.dash')||'{}')}catch(e){return {}}}
function save(k,v){try{var o=st();o[k]=v;sessionStorage.setItem('atip.dash',JSON.stringify(o))}catch(e){}}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function n(v,d){if(v==null||v==='')return '—';return Number(v).toLocaleString('en-IN',{maximumFractionDigits:d==null?2:d})}
function pc(v){if(v==null)return '<span class="mut">—</span>';return '<span class="'+(v>=0?'up':'dn')+'">'+(v>=0?'+':'')+Number(v).toFixed(2)+'%</span>'}

/* ---------- ATIP Scores / Signal History / other tables: filters ---------- */
function visibleLimit(){var e=document.getElementById('slim');return e?parseInt(e.value,10)||0:0}
window.ft=function(){
  var q=((document.getElementById('srch')||{}).value||'').trim().toLowerCase();
  var s=((document.getElementById('sf')||{}).value||'');
  var lim=visibleLimit(), shown=0, match=0, rows=document.querySelectorAll('#st tbody tr');
  for(var i=0;i<rows.length;i++){var r=rows[i];
    var ok=(!s||r.dataset.signal===s)&&(!q||(r.dataset.search||r.innerText.toLowerCase()).indexOf(q)>=0);
    if(ok){match++;}
    var show=ok&&(!lim||shown<lim); if(show)shown++;
    r.style.display=show?'':'none';}
  var c=document.getElementById('scnt'); if(c)c.textContent='Showing '+shown+' of '+match+(match!==rows.length?' matching ('+rows.length+' total)':'');
  save('srch',q);save('sf',s);save('slim',lim);
};
window.hft=function(){
  var q=((document.getElementById('hsrch')||{}).value||'').trim().toLowerCase();
  var s=((document.getElementById('hsf')||{}).value||'');
  var rows=document.querySelectorAll('#ht tbody tr'), m=0;
  for(var i=0;i<rows.length;i++){var r=rows[i];
    var ok=(!s||r.dataset.signal===s)&&(!q||(r.dataset.sym||'').toLowerCase().indexOf(q)>=0);
    if(ok)m++; r.style.display=ok?'':'none';}
  var c=document.getElementById('hcnt'); if(c)c.textContent=m+' of '+rows.length;
  save('hsrch',q);save('hsf',s);
};
function rowText(r){var t='';for(var i=0;i<r.cells.length;i++){if(!r.cells[i].classList.contains('acts'))t+=' '+r.cells[i].innerText}return t.toLowerCase()}
function addFilters(){
  document.querySelectorAll('.tc table').forEach(function(tb){
    if(tb.id==='st'||tb.id==='ht'||tb.dataset.filt)return;
    var body=tb.tBodies[0]; if(!body||body.rows.length<4)return;
    tb.dataset.filt='1';
    var w=document.createElement('div');w.className='tfilter';
    var inp=document.createElement('input');inp.placeholder='Filter this table…';
    var cnt=document.createElement('span');cnt.className='cnt';
    w.appendChild(inp);w.appendChild(cnt);tb.parentNode.insertBefore(w,tb);
    var key='f_'+(tb.closest('.tc')||{}).id+'_'+Array.prototype.indexOf.call(tb.parentNode.querySelectorAll('table'),tb);
    function run(){var q=inp.value.trim().toLowerCase(),m=0;
      for(var i=0;i<body.rows.length;i++){var r=body.rows[i],ok=!q||rowText(r).indexOf(q)>=0;if(ok)m++;r.style.display=ok?'':'none'}
      cnt.textContent=q?(m+' of '+body.rows.length):'';save(key,inp.value);}
    inp.addEventListener('input',run);
    var saved=st()[key]; if(saved){inp.value=saved;run();}
  });
}
/* keep the Show-limit and filters applied after a sort; remember the sort */
if(typeof srt==='function'){
  var _srt=srt;
  window.srt=function(tid,col){_srt(tid,col);
    var th=document.querySelectorAll('#'+tid+' thead th')[col];
    var o=st().sort||{};o[tid]={col:col,asc:th&&th.dataset.sorted==='asc'};save('sort',o);
    if(tid==='st')ft(); if(tid==='ht')hft();};
}
function restore(){
  var o=st();
  if(o.srch!=null&&document.getElementById('srch'))document.getElementById('srch').value=o.srch;
  if(o.sf!=null&&document.getElementById('sf'))document.getElementById('sf').value=o.sf;
  if(o.slim!=null&&document.getElementById('slim'))document.getElementById('slim').value=String(o.slim);
  if(o.hsrch!=null&&document.getElementById('hsrch'))document.getElementById('hsrch').value=o.hsrch;
  if(o.hsf!=null&&document.getElementById('hsf'))document.getElementById('hsf').value=o.hsf;
  var so=o.sort||{};
  Object.keys(so).forEach(function(tid){if(!document.getElementById(tid)||typeof ss==='undefined')return;
    ss[tid+so[tid].col]=!so[tid].asc; window.srt(tid,so[tid].col);});
  ft();hft();
}

/* ---------- stock panel ---------- */
window.openStock=function(sym){
  S.sym=sym;S.data=null;S.intra=null;S.intraFor=null;S.ierr=null;
  var b=document.getElementById('sv-back'),p=document.getElementById('sv');
  p.innerHTML='<div class="hd"><div><div class="sym">'+esc(sym)+'</div><div class="nm">loading history…</div></div><div class="btns"><button class="b-x" onclick="svClose()">✕</button></div></div>';
  b.classList.add('on');document.body.style.overflow='hidden';
  fetch('/api/stock/'+encodeURIComponent(sym)+'/history?sessions=2500').then(function(r){return r.json().then(function(j){if(!r.ok)throw new Error(j.error||r.status);return j})})
   .then(function(d){if(S.sym!==sym)return;S.data=d;render()})
   .catch(function(e){p.querySelector('.nm').textContent='No history: '+e.message});
};
window.svClose=function(){document.getElementById('sv-back').classList.remove('on');document.body.style.overflow='';S.sym=null};
document.addEventListener('keydown',function(e){if(e.key==='Escape'&&S.sym)svClose()});
setInterval(function(){if(S.sym&&typeof _secsLeft!=='undefined'&&_secsLeft<60)_secsLeft=60},1000);
document.addEventListener('click',function(e){
  var t=e.target; if(!t.closest)return;
  if(t.closest('#sv-back')||t.closest('button,a,input,select,textarea,label,.acts,.modal'))return;
  var el=t.closest('[data-sym]'); if(!el)return;
  var sym=el.dataset.sym; if(!sym||sym==='—'||sym==='None')return;
  openStock(sym);
});

var RANGES={'1M':21,'3M':63,'6M':126,'1Y':250,'3Y':750,'5Y':1250,'All':0};
var OV={sma20:['SMA 20','#f59e0b'],sma50:['SMA 50','#a78bfa'],sma200:['SMA 200','#f472b6'],ema21:['EMA 21','#22d3ee'],bb:['Bollinger 20,2','#94a3b8']};
var INTRA=[[1,'1D 15m'],[5,'5D 15m']],VWAPC='#e879f9',RSIC='#c084fc',MACDC='#38bdf8',SIGC='#f59e0b';
/* ---- indicator math: no DOM in this block (tests/test_w40_stock_chart.py runs it in node) ---- */
function bucketKey(ds,iv){if(iv==='D')return ds;if(iv==='M')return ds.slice(0,7);var d=new Date(ds+'T00:00:00Z'),wd=(d.getUTCDay()+6)%7;d.setUTCDate(d.getUTCDate()-wd);return d.toISOString().slice(0,10)}
function agg(P,iv){var out=[],cur=null;P.forEach(function(p){var k=bucketKey(p.date,iv),hi=+p.high||+p.close,lw=+p.low||+p.close;
  if(!cur||cur.k!==k){cur={k:k,date:p.date,open:+p.open||+p.close,high:hi,low:lw,close:+p.close,volume:+p.volume||0};out.push(cur)}
  else{cur.date=p.date;cur.high=Math.max(cur.high,hi);cur.low=Math.min(cur.low,lw);cur.close=+p.close;cur.volume+=(+p.volume||0)}});return out}
function sma(c,n){var o=[],s=0;for(var i=0;i<c.length;i++){s+=c[i];if(i>=n)s-=c[i-n];o.push(i>=n-1?s/n:null)}return o}
function ema(c,n){var o=[],k=2/(n+1),e=null;for(var i=0;i<c.length;i++){if(i===n-1){var s=0;for(var j=0;j<n;j++)s+=c[j];e=s/n}else if(i>=n)e=c[i]*k+e*(1-k);o.push(i>=n-1?e:null)}return o}
function boll(c,n,m){var mid=sma(c,n),up=[],dn=[];for(var i=0;i<c.length;i++){if(mid[i]==null){up.push(null);dn.push(null);continue}var v=0;for(var j=i-n+1;j<=i;j++)v+=Math.pow(c[j]-mid[i],2);var sd=Math.sqrt(v/n);up.push(mid[i]+m*sd);dn.push(mid[i]-m*sd)}return {mid:mid,up:up,dn:dn}}
/* pandas' Series.ewm(com=com, adjust=False).mean(), step for step as pandas computes it: seeded by the first value,
   then e = ((1-a)e + a v) / ((1-a) + a) with a = 1 / (1 + com); null before the first value */
function ewm(c,com){var a=1/(1+com),f=1-a,o=[],e=null;for(var i=0;i<c.length;i++){var v=c[i];
  if(v!=null&&v===v){if(e==null)e=v;else if(e!==v)e=(f*e+a*v)/(f+a)}o.push(e)}return o}
/* RSI n with Wilder smoothing exactly as research/technicals.py rsi(): gains / losses of close.diff() smoothed with
   ewm(alpha=1/n, adjust=False, min_periods=n) (pandas turns alpha into com = (1 - alpha) / alpha); 100 when the
   average loss is 0; null for the first n bars */
function rsi(c,n){var g=[null],l=[null],i;for(i=1;i<c.length;i++){var d=c[i]-c[i-1];g.push(d>0?d:0);l.push(d<0?-d:0)}
  var al=1/n,up=ewm(g,(1-al)/al),dn=ewm(l,(1-al)/al),o=[];
  for(i=0;i<c.length;i++)o.push(i<n?null:(dn[i]===0?100:100-100/(1+up[i]/dn[i])));return o}
/* MACD (fast, slow, sig) as research/technicals.py indicators(): EMA fast - EMA slow with ewm(span, adjust=False)
   (com = (span - 1) / 2) on the whole series, signal = the same EMA of the MACD line, histogram = MACD - signal.
   The values are the server's; the line is drawn from bar slow, signal and histogram from bar slow + sig - 1. */
function macd(c,fa,sl,sg){var a=ewm(c,(fa-1)/2),b=ewm(c,(sl-1)/2),ln=[],i;for(i=0;i<c.length;i++)ln.push(a[i]-b[i]);
  var s=ewm(ln,(sg-1)/2),o={line:[],signal:[],hist:[]};
  for(i=0;i<c.length;i++){var ok=i>=sl+sg-2;o.line.push(i>=sl-1?ln[i]:null);o.signal.push(ok?s[i]:null);o.hist.push(ok?ln[i]-s[i]:null)}return o}
/* ---- end of indicator math ---- */
function overlays(A){var c=A.map(function(p){return +p.close}),o={};if(S.ov.sma20)o.sma20=sma(c,20);if(S.ov.sma50)o.sma50=sma(c,50);if(S.ov.sma200)o.sma200=sma(c,200);if(S.ov.ema21)o.ema21=ema(c,21);if(S.ov.bb){var b=boll(c,20,2);o.bb=b.mid;o.bb_up=b.up;o.bb_dn=b.dn}return o}
/* RSI / MACD on the whole (aggregated) series, like the overlays, so the left edge of a range is right */
function indic(A){var c=A.map(function(p){return +p.close}),o={};if(S.ind.rsi)o.rsi=rsi(c,14);if(S.ind.macd)o.macd=macd(c,12,26,9);return o}
function cutInd(N,i0){var o={};if(N.rsi)o.rsi=N.rsi.slice(i0);if(N.macd)o.macd={line:N.macd.line.slice(i0),signal:N.macd.signal.slice(i0),hist:N.macd.hist.slice(i0)};return o}
function series(){var d=S.data,A=agg(d.prices,S.iv),O=overlays(A),N=indic(A),m=RANGES[S.range],i0=0;
  if(m&&d.prices.length>m){var sd=d.prices[d.prices.length-m].date;while(i0<A.length&&A[i0].date<sd)i0++}
  var Os={};Object.keys(O).forEach(function(k){Os[k]=O[k].slice(i0)});return {P:A.slice(i0),O:Os,N:cutInd(N,i0)}}
/* intraday: up to 5 sessions are loaded; 1D shows the last, the overlays and indicators run over all of them.
   x is the bar's slot in a fixed 09:15-15:30 session (pos), sessions side by side: no overnight gap is drawn */
function iseries(){var I=S.intra,A=I.bars,ss=I.sessions,k0=Math.max(0,ss.length-S.ix),i0=ss[k0].first,iv=+I.interval||15,
  spp=Math.ceil(375/iv),sess=[],pos=[],at=[],O=overlays(A),N=indic(A),Os={};
  ss.slice(k0).forEach(function(s,k){sess.push({date:s.date,first:s.first-i0,last:s.last-i0,base:k*spp,prev_close:s.prev_close,prev_close_date:s.prev_close_date});
    for(var j=s.first;j<=s.last;j++){var t=String(A[j].time||'09:15').split(':'),mn=(+t[0])*60+(+t[1])-555;pos.push(k*spp+Math.max(0,Math.min(spp-1,Math.floor(mn/iv))))}});
  var P=A.slice(i0),j=0;for(var q=0;q<sess.length*spp;q++){while(j+1<pos.length&&pos[j+1]<=q)j++;at.push(j)}
  Object.keys(O).forEach(function(k){Os[k]=O[k].slice(i0)});
  var days={},marks=[];sess.forEach(function(s){days[s.date]=1});
  (I.marks||[]).forEach(function(m){if(!days[m.date])return;marks.push({m:m,i:(m.bar!=null&&m.bar>=i0)?m.bar-i0:null})});
  return {P:P,O:Os,N:cutInd(N,i0),pos:pos,at:at,slots:sess.length*spp,spp:spp,sess:sess,marks:marks,iv:iv}}
function loadIntra(){var sym=S.sym;S.intraFor=sym;S.intra=null;S.ierr=null;
  fetch('/api/stock/'+encodeURIComponent(sym)+'/intraday?days=5&interval=15').then(function(r){return r.json().then(function(j){if(!r.ok)throw new Error(j.error||r.status);return j})})
   .then(function(j){if(S.intraFor!==sym||S.sym!==sym)return;S.intra=j;if(S.ix&&S.data)chart()})
   .catch(function(e){if(S.intraFor!==sym||S.sym!==sym)return;S.ierr=e.message||String(e);if(S.ix&&S.data)chart()})}
function dayLabel(ds){try{return new Date(ds+'T00:00:00Z').toLocaleDateString('en-IN',{weekday:'short',day:'2-digit',month:'short',timeZone:'UTC'})}catch(e){return ds}}
function nm(v){var a=Math.abs(v);return n(v,a>=100?1:a>=1?2:4)}
function indTip(N,i){var t=[];if(N.rsi&&N.rsi[i]!=null)t.push('<span style="color:'+RSIC+'">RSI 14</span> <b>'+n(N.rsi[i])+'</b>');
  if(N.macd&&N.macd.line[i]!=null)t.push('<span style="color:'+MACDC+'">MACD</span> <b>'+nm(N.macd.line[i])+'</b>'+(N.macd.signal[i]!=null?' <span style="color:'+SIGC+'">signal</span> '+nm(N.macd.signal[i])+' hist '+nm(N.macd.hist[i]):''));
  return t.length?' &nbsp;| '+t.join(' · '):''}
function pth(a,x,f){var s='',on=false;a.forEach(function(v,i){if(v==null){on=false;return}s+=(on?'L':'M')+x(i).toFixed(1)+' '+f(v).toFixed(1)+' ';on=true});return s}
function render(){
  var d=S.data,s=d.stats,p=document.getElementById('sv'),kind=S.ix?S.ikind:S.kind;
  var live=(typeof liveCmpFor==='function')?liveCmpFor(d.symbol):null;
  var cmp=(live&&live>0)?live:s.last_close, chg=s.prev_close?((cmp-s.prev_close)/s.prev_close*100):null;
  var h='<div class="hd"><div><div class="sym">'+esc(d.symbol)+'</div><div class="nm">'+esc(d.name||'')+'</div></div>'+
   '<div><div class="px">₹'+n(cmp)+' '+pc(chg)+'</div><div class="nm">'+(live&&live>0?'live':'close '+esc(s.last_date))+'</div></div>'+
   '<div class="btns"><button class="b-buy" onclick="svOrder(\'BUY\')">Buy</button><button class="b-sell" onclick="svOrder(\'SELL\')">Sell</button><button class="b-x" onclick="svClose()">✕</button></div></div>';
  h+='<div class="kv">'+[['52W high',n(s.high_52w)],['52W low',n(s.low_52w)],['1W',pc(s.ret_1w)],['1M',pc(s.ret_1m)],['3M',pc(s.ret_3m)],['6M',pc(s.ret_6m)],['1Y',pc(s.ret_1y)],['3Y',pc(s.ret_3y)],['5Y',pc(s.ret_5y)],['Since '+esc(s.first_date||''),pc(s.ret_all)],['High / low (loaded)',n(s.high_all)+' / '+n(s.low_all)],['Avg vol 20D',n(s.avg_volume_20d,0)]]
    .map(function(x){return '<div><span>'+x[0]+'</span><b>'+x[1]+'</b></div>'}).join('')+'</div>';
  h+='<div class="bar">'+Object.keys(RANGES).map(function(r){return '<button class="'+(!S.ix&&S.range===r?'on':'')+'" onclick="svRange(\''+r+'\')">'+r+'</button>'}).join('')+
     '<span class="sp"></span>'+[['D','Daily'],['W','Weekly'],['M','Monthly']].map(function(v){return '<button class="'+(!S.ix&&S.iv===v[0]?'on':'')+'" onclick="svIv(\''+v[0]+'\')">'+v[1]+'</button>'}).join('')+
     INTRA.map(function(v){return '<button class="svib'+(S.ix===v[0]?' on':'')+'" onclick="svIntra('+v[0]+')" title="15-minute bars of the last '+(v[0]>1?v[0]+' sessions':'session')+' (09:15-15:30), session VWAP, previous close, intraday scans">'+v[1]+'</button>'}).join('')+
     '<span class="sp"></span><button class="'+(kind==='line'?'on':'')+'" onclick="svKind(\'line\')">Line</button><button class="'+(kind==='candle'?'on':'')+'" onclick="svKind(\'candle\')">Candles</button><button class="'+(S.log?'on':'')+'" onclick="svLog()">Log</button></div>';
  h+='<div class="bar">'+Object.keys(OV).map(function(k){return '<button class="'+(S.ov[k]?'on':'')+'" style="'+(S.ov[k]?'background:'+OV[k][1]+';border-color:'+OV[k][1]+';color:#0f172a':'')+'" onclick="svOv(\''+k+'\')">'+OV[k][0]+'</button>'}).join('')+
     '<span class="sp"></span><button id="svrsibtn" class="'+(S.ind.rsi?'on':'')+'" onclick="svInd(\'rsi\')" title="RSI 14 (Wilder) panel under the chart">RSI 14</button><button id="svmacdbtn" class="'+(S.ind.macd?'on':'')+'" onclick="svInd(\'macd\')" title="MACD 12, 26, 9 panel under the chart">MACD</button>'+
     '<button id="svpatbtn" class="'+(S.pat?'on':'')+'" onclick="svPat()" title="Chart patterns, technical signals and candle patterns">Patterns</button></div>';
  h+='<div id="svc"></div><div class="tip" id="svtip">'+(S.ix?'':'Hover the chart for prices. ▲ BUY / ▼ SELL signals from the signal log.')+'</div><div class="tip" id="svpl"></div>';
  var tabs=[['sc','Score history ('+d.scores.length+')'],['sg','Signals ('+d.signals.length+')'],['te','Technicals'],['po','Position & orders']];
  h+='<div class="tabs2">'+tabs.map(function(t,i){return '<div class="'+(i===0?'on':'')+'" onclick="svTab(\''+t[0]+'\',this)">'+t[1]+'</div>'}).join('')+'</div>';
  h+='<div class="pane on" id="sv-sc">'+scoresTable(d)+'</div><div class="pane" id="sv-sg">'+signalsTable(d)+'</div><div class="pane" id="sv-te">'+techTable(d)+'</div><div class="pane" id="sv-po">'+posTable(d)+'</div>';
  p.innerHTML=h; chart();
}
window.svRange=function(r){S.range=r;S.ix=0;render()};
window.svKind=function(k){if(S.ix)S.ikind=k;else S.kind=k;render()};
window.svIv=function(v){S.iv=v;S.ix=0;render()};
window.svIntra=function(k){S.ix=k;if(S.ierr||S.intraFor!==S.sym)loadIntra();render()};
window.svLog=function(){S.log=!S.log;render()};
window.svOv=function(k){S.ov[k]=!S.ov[k];render()};
window.svInd=function(k){S.ind[k]=!S.ind[k];save('sv'+k,S.ind[k]);render()};
window.svPat=function(){S.pat=!S.pat;save('svpat',S.pat);render()};
window.svTab=function(id,el){document.querySelectorAll('#sv .pane').forEach(function(x){x.classList.remove('on')});document.querySelectorAll('#sv .tabs2 div').forEach(function(x){x.classList.remove('on')});document.getElementById('sv-'+id).classList.add('on');el.classList.add('on')};
window.svOrder=function(side){var d=S.data;if(typeof openOrderModal!=='function'){alert('Order entry is not available on this page');return}
  var live=(typeof liveCmpFor==='function')?liveCmpFor(d.symbol):null;svClose();openOrderModal(d.symbol,(live&&live>0)?live:d.stats.last_close,side)};

function chart(){
  var d=S.data,ix=S.ix>0,box=document.getElementById('svc'),pl=document.getElementById('svpl'),tip=document.getElementById('svtip');
  if(ix){if(S.intraFor!==S.sym)loadIntra();
    var I=S.intra,why=S.ierr?'Intraday bars could not be loaded: '+S.ierr:!I?'Loading intraday bars…':!(I.bars||[]).length?(I.message||'No intraday bars are stored for this stock.'):null;
    if(why){box.innerHTML='<div class="tip" id="svnoid">'+esc(why)+'</div>';if(pl)pl.innerHTML='';if(tip)tip.innerHTML='';return}}
  var SR=ix?iseries():series(),P=SR.P,O=SR.O,N=SR.N,W=1000,H=300,HV=60,HS=110,L=8,R=62,T=10,G=18,HR=78,HM=96,XA=ix?16:0;
  if(!P.length){box.innerHTML='';return}
  var NS=ix?SR.slots:P.length,kind=ix?S.ikind:S.kind;
  var hi=-1e18,lo=1e18,vmax=0;P.forEach(function(p){hi=Math.max(hi,+p.high||+p.close);lo=Math.min(lo,+p.low||+p.close);vmax=Math.max(vmax,+p.volume||0)});
  Object.keys(O).forEach(function(k){O[k].forEach(function(v){if(v!=null){hi=Math.max(hi,v);lo=Math.min(lo,v)}})});
  if(ix){P.forEach(function(p){if(p.vwap!=null){hi=Math.max(hi,p.vwap);lo=Math.min(lo,p.vwap)}});
    SR.sess.forEach(function(s){if(s.prev_close!=null){hi=Math.max(hi,s.prev_close);lo=Math.min(lo,s.prev_close)}})}
  /* the lines of the patterns in place, from the first visible bar they reach to the last bar; interpolated in
     daily sessions (gi), so a weekly / monthly bar takes the line's value on its last session */
  var M=ix?{}:(d.chart_marks||{}),gi={},segs=[],mc={BULL:'#22c55e',BEAR:'#ef4444',BOTH:'#f59e0b'};
  if(S.pat&&!ix){d.prices.forEach(function(p,i){gi[p.date]=i});
    (M.patterns||[]).forEach(function(pt){(pt.lines||[]).forEach(function(ln){var g0=gi[ln.from],g1=gi[ln.to],j0=-1;if(g0==null||g1==null)return;
      for(var j=0;j<P.length;j++){if(gi[P[j].date]>=g0){j0=j;break}}if(j0<0)return;
      var at=function(g){return g1>g0?ln.from_value+(ln.to_value-ln.from_value)*(Math.min(g,g1)-g0)/(g1-g0):ln.to_value},
          v0=at(Math.max(g0,gi[P[j0].date])),v1=at(gi[P[P.length-1].date]);
      segs.push({pt:pt,ln:ln,i0:j0,v0:v0,i1:P.length-1,v1:v1});hi=Math.max(hi,v0,v1);lo=Math.min(lo,v0,v1)})})}
  var lg=S.log&&lo>0,f=function(v){return lg?Math.log(v):v},fh,fl;
  if(lg){fh=Math.log(hi);fl=Math.log(lo);var lp=(fh-fl)*0.06||0.01;fh+=lp;fl-=lp}else{var pad=(hi-lo)*0.06||1;hi+=pad;lo-=pad;fh=hi;fl=lo}
  var cw=(W-L-R)/NS,x=ix?function(i){return L+cw*(SR.pos[i]+0.5)}:function(i){return L+cw*(i+0.5)},y=function(v){return T+(fh-f(v))/(fh-fl)*(H-T-HV-6)};
  /* under the price block (and, intraday, its time axis): the RSI and MACD panels, then the daily ATIP score panel;
     they share the x-axis, the crosshair and the tooltip */
  var top=H+XA,rT=0,mT=0;if(N.rsi){rT=top+G;top=rT+HR}if(N.macd){mT=top+G;top=mT+HM}
  var SB=top,HT=ix?top+4:SB+G+HS;
  var o='<svg viewBox="0 0 '+W+' '+HT+'" id="svsvg">';
  for(var k=0;k<=4;k++){var gv=lg?Math.exp(fl+(fh-fl)*k/4):lo+(hi-lo)*k/4,gy=y(gv);o+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+gy+'" y2="'+gy+'" stroke="#1e293b"/><text x="'+(W-R+4)+'" y="'+(gy+4)+'" fill="#64748b" font-size="11">'+n(gv)+'</text>'}
  if(ix){var one=SR.sess.length===1;
    SR.sess.forEach(function(s,k){var x0=L+cw*s.base;
      if(k)o+='<line class="svi-sep" x1="'+x0+'" x2="'+x0+'" y1="'+T+'" y2="'+H+'" stroke="#475569" stroke-dasharray="2 4"/>';
      o+='<text class="svi-day" x="'+(x0+(k?4:0))+'" y="'+(H+13)+'" fill="#94a3b8" font-size="10.5">'+esc(dayLabel(s.date))+'</text>'});
    if(one)for(var hh=10;hh<=15;hh++){var sl=(hh*60-555)/SR.iv;if(sl===Math.floor(sl)&&sl<SR.spp)o+='<text class="svi-hr" x="'+(L+cw*sl)+'" y="'+(H+13)+'" fill="#64748b" font-size="10.5" text-anchor="middle">'+hh+':00</text>'}}
  P.forEach(function(p,i){var vh=vmax?(+p.volume||0)/vmax*HV:0,up=i===0||+p.close>=+P[i-1].close;
    o+='<rect x="'+(x(i)-cw*0.4)+'" y="'+(H-vh)+'" width="'+Math.max(cw*0.8,0.6)+'" height="'+vh+'" fill="'+(up?'#05966955':'#dc262655')+'"/>'});
  if(kind==='candle'){P.forEach(function(p,i){var op=+p.open||+p.close,cl=+p.close,up=cl>=op,c=up?'#059669':'#dc2626';
      o+='<line x1="'+x(i)+'" x2="'+x(i)+'" y1="'+y(+p.high||cl)+'" y2="'+y(+p.low||cl)+'" stroke="'+c+'"/>'+
         '<rect x="'+(x(i)-Math.max(cw*0.35,0.5))+'" y="'+y(Math.max(op,cl))+'" width="'+Math.max(cw*0.7,1)+'" height="'+Math.max(Math.abs(y(op)-y(cl)),1)+'" fill="'+c+'"/>'})}
  else{var up=+P[P.length-1].close>=+P[0].close,col=up?'#059669':'#dc2626',path=P.map(function(p,i){return(i?'L':'M')+x(i).toFixed(1)+' '+y(+p.close).toFixed(1)}).join(' ');
    o+='<path d="'+path+' L'+x(P.length-1)+' '+(H-HV-6)+' L'+x(0)+' '+(H-HV-6)+' Z" fill="'+col+'18"/><path d="'+path+'" fill="none" stroke="'+col+'" stroke-width="1.8"/>'}
  Object.keys(O).forEach(function(k){var col=(OV[k]||OV[k.replace(/_(up|dn)$/,'')]||OV.bb)[1],seg='',on=false;
    O[k].forEach(function(v,i){if(v==null){on=false;return}seg+=(on?'L':'M')+x(i).toFixed(1)+' '+y(v).toFixed(1)+' ';on=true});
    if(seg)o+='<path d="'+seg+'" fill="none" stroke="'+col+'" stroke-width="'+(k.indexOf('bb')===0?1:1.4)+'"'+(k==='bb_up'||k==='bb_dn'?' stroke-dasharray="4 3"':'')+'/>'});
  if(ix){/* session VWAP (restarts each session) and each session's previous daily close */
    var vp='';SR.sess.forEach(function(s){var on=false;for(var i=s.first;i<=s.last;i++){var v=P[i].vwap;if(v==null){on=false;continue}vp+=(on?'L':'M')+x(i).toFixed(1)+' '+y(v).toFixed(1)+' ';on=true}});
    if(vp)o+='<path id="svvwap" d="'+vp+'" fill="none" stroke="'+VWAPC+'" stroke-width="1.5"/>';
    SR.sess.forEach(function(s,k){if(s.prev_close==null)return;var yy=y(s.prev_close),x0=L+cw*s.base,x1=L+cw*(s.base+SR.spp);
      o+='<line class="svi-pc" x1="'+x0+'" x2="'+x1+'" y1="'+yy+'" y2="'+yy+'" stroke="#94a3b8" stroke-dasharray="6 4"><title>'+esc('previous close '+n(s.prev_close)+' ('+(s.prev_close_date||'')+')')+'</title></line>';
      if(k===SR.sess.length-1)o+='<text class="svi-pct" x="'+(x1-4)+'" y="'+(yy-4)+'" text-anchor="end" font-size="10.5" fill="#94a3b8">prev close '+n(s.prev_close)+'</text>'})}
  var idx={};if(!ix){P.forEach(function(p,i){idx[p.k]=i});
  d.signals.forEach(function(sg){var i=idx[bucketKey(sg.signal_date,S.iv)];if(i==null)return;var p=P[i],buy=sg.signal==='BUY';
    var yy=buy?y(+p.low||+p.close)+14:y(+p.high||+p.close)-6;
    o+='<text x="'+x(i)+'" y="'+yy+'" text-anchor="middle" font-size="13" fill="'+(buy?'#22c55e':'#ef4444')+'">'+(buy?'▲':'▼')+'</text>'})}
  /* technical signals (◆ chart-pattern breakout, ● any other scan) and candle patterns (•), per visible bar */
  var bk=function(ds){return bucketKey(ds,S.iv)},sigBy={},cdlBy={},inBy={};
  (M.signals||[]).forEach(function(z){(sigBy[bk(z.date)]=sigBy[bk(z.date)]||[]).push(z)});
  (M.candles||[]).forEach(function(z){(cdlBy[bk(z.date)]=cdlBy[bk(z.date)]||[]).push(z)});
  (M.in_place||[]).forEach(function(z){inBy[bk(z.date)]=z.text});
  var cl=function(v){return Math.max(T+4,Math.min(H-HV-4,v))};
  if(S.pat&&!ix){o+='<g id="svmarks">';
    segs.forEach(function(g){var c=mc[g.pt.side]||mc.BOTH,y0=y(g.v0),y1=y(g.v1),t=g.pt.name+': '+g.ln.label.toLowerCase()+' '+n(g.v1);
      o+='<line class="svm-line" x1="'+x(g.i0)+'" y1="'+y0+'" x2="'+x(g.i1)+'" y2="'+y1+'" stroke="'+c+'" stroke-width="1.4" stroke-dasharray="6 4"><title>'+esc(t)+'</title></line>'+
         '<text x="'+(x(g.i1)-4)+'" y="'+(y1-4)+'" text-anchor="end" font-size="10.5" fill="'+c+'">'+esc(g.ln.label+' '+n(g.v1))+'</text>'});
    P.forEach(function(p,i){var hy=y(+p.high||+p.close),ly=y(+p.low||+p.close),sd={};
      (cdlBy[p.k]||[]).forEach(function(z){if((z.side!=='BULL'&&z.side!=='BEAR')||sd[z.side])return;sd[z.side]=1;
        o+='<circle class="svm-cdl" cx="'+x(i)+'" cy="'+cl(z.side==='BULL'?ly+5:hy-5)+'" r="2.2" fill="'+mc[z.side]+'"/>'});
      var ss=sigBy[p.k];if(!ss)return;
      ['BULL','BEAR'].forEach(function(dir){var g=ss.filter(function(z){return z.direction===dir});if(!g.length)return;var b=dir==='BULL';
        if(g.some(function(z){return z.pattern}))o+='<text class="svm-pat" x="'+x(i)+'" y="'+cl(b?ly+27:hy-18)+'" text-anchor="middle" font-size="13" fill="'+mc[dir]+'">◆</text>';
        else o+='<circle class="svm-sig" cx="'+x(i)+'" cy="'+cl(b?ly+22:hy-22)+'" r="3" fill="'+mc[dir]+'" fill-opacity=".85"/>'})});
    o+='</g>'}
  /* intraday scans: ▲ / ▼ research/intraday_signals.py (on the bar its trigger closed), ● strategy/intraday_scan.py
     (on the last bar done when it ran); below the bar for BULL, above for BEAR, stacked when several */
  if(S.pat&&ix){var nb={};o+='<g id="svimarks">';
    SR.marks.forEach(function(z){if(z.i==null)return;var m=z.m,p=P[z.i],b=m.direction==='BULL',c=mc[m.direction]||mc.BOTH,kk=z.i+(b?'b':'s'),st=nb[kk]=(nb[kk]||0)+1;
      var yy=b?cl(y(+p.low||+p.close)+4+st*11):cl(y(+p.high||+p.close)-st*11),t=esc((m.time||'')+' '+m.name+(m.price!=null?' @ '+n(m.price):'')+(m.reason?': '+m.reason:''));
      o+=m.source==='signal'?'<text class="svm-ihit" x="'+x(z.i)+'" y="'+(yy+4)+'" text-anchor="middle" font-size="12" fill="'+c+'">'+(b?'▲':'▼')+'<title>'+t+'</title></text>'
        :'<circle class="svm-iscan" cx="'+x(z.i)+'" cy="'+yy+'" r="3.2" fill="'+c+'" fill-opacity=".85"><title>'+t+'</title></circle>'});
    o+='</g>'}
  if(N.rsi){var ry=function(v){return rT+(100-v)/100*(HR-8)+4},rp=pth(N.rsi,x,ry);
    o+='<g id="svrsi"><text x="'+L+'" y="'+(rT-4)+'" fill="#64748b" font-size="11">RSI 14</text>';
    [30,70].forEach(function(b){o+='<line class="svi-guide" x1="'+L+'" x2="'+(W-R)+'" y1="'+ry(b)+'" y2="'+ry(b)+'" stroke="#334155" stroke-dasharray="3 4"/><text x="'+(W-R+4)+'" y="'+(ry(b)+4)+'" fill="#64748b" font-size="11">'+b+'</text>'});
    o+=(rp?'<path class="svi-rsi" d="'+rp+'" fill="none" stroke="'+RSIC+'" stroke-width="1.4"/>':'<text x="'+(W/2)+'" y="'+(rT+HR/2+4)+'" fill="#475569" font-size="12" text-anchor="middle">RSI 14 needs 15 bars</text>')+'</g>'}
  if(N.macd){var mm=N.macd,mx=0,mn=0;[mm.line,mm.signal,mm.hist].forEach(function(a){a.forEach(function(v){if(v!=null){mx=Math.max(mx,v);mn=Math.min(mn,v)}})});
    var mp=(mx-mn)*0.08||1;mx+=mp;mn-=mp;
    var my=function(v){return mT+4+(mx-v)/(mx-mn)*(HM-8)},z0=my(0),lp2=pth(mm.line,x,my),sp2=pth(mm.signal,x,my);
    o+='<g id="svmacd"><text x="'+L+'" y="'+(mT-4)+'" fill="#64748b" font-size="11">MACD 12, 26, 9 <tspan fill="'+MACDC+'">— MACD</tspan> <tspan fill="'+SIGC+'">— signal</tspan> histogram</text>';
    o+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+z0+'" y2="'+z0+'" stroke="#334155"/><text x="'+(W-R+4)+'" y="'+(z0+4)+'" fill="#64748b" font-size="11">0</text>';
    mm.hist.forEach(function(v,i){if(v==null)return;var yv=my(v);o+='<rect class="svi-hist" x="'+(x(i)-cw*0.35)+'" y="'+Math.min(yv,z0)+'" width="'+Math.max(cw*0.7,0.6)+'" height="'+Math.max(Math.abs(yv-z0),0.5)+'" fill="'+(v>=0?'#05966999':'#dc262699')+'"/>'});
    o+=(lp2?'<path class="svi-macd" d="'+lp2+'" fill="none" stroke="'+MACDC+'" stroke-width="1.4"/>':'<text x="'+(W/2)+'" y="'+(mT+HM/2+4)+'" fill="#475569" font-size="12" text-anchor="middle">MACD needs 26 bars</text>')+
       (sp2?'<path class="svi-signal" d="'+sp2+'" fill="none" stroke="'+SIGC+'" stroke-width="1.2"/>':'')+'</g>'}
  var sm={};if(!ix){d.scores.forEach(function(s){sm[bucketKey(s.date,S.iv)]=s});
  var sy=function(v){return SB+G+(100-v)/100*(HS-8)+4};
  o+='<text x="'+L+'" y="'+(SB+G-4)+'" fill="#64748b" font-size="11">ATIP score</text>';
  [45,70].forEach(function(b){o+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+sy(b)+'" y2="'+sy(b)+'" stroke="#334155" stroke-dasharray="3 4"/><text x="'+(W-R+4)+'" y="'+(sy(b)+4)+'" fill="#64748b" font-size="11">'+b+'</text>'});
  var seg='',started=false;P.forEach(function(p,i){var s=sm[p.k];if(!s||s.atip_score==null){started=false;return}seg+=(started?'L':'M')+x(i).toFixed(1)+' '+sy(+s.atip_score).toFixed(1)+' ';started=true});
  o+=seg?'<path d="'+seg+'" fill="none" stroke="#38bdf8" stroke-width="1.8"/>':'<text x="'+(W/2)+'" y="'+(SB+G+HS/2)+'" fill="#475569" font-size="12" text-anchor="middle">no ATIP scores in this range</text>';
  P.forEach(function(p,i){var s=sm[p.k];if(s&&s.atip_score!=null)o+='<circle cx="'+x(i)+'" cy="'+sy(+s.atip_score)+'" r="1.8" fill="#38bdf8"/>'})}
  o+='<line id="svx" x1="0" x2="0" y1="'+T+'" y2="'+HT+'" stroke="#94a3b8" stroke-dasharray="2 3" visibility="hidden"/>';
  o+='<rect x="'+L+'" y="0" width="'+(W-L-R)+'" height="'+HT+'" fill="transparent" id="svhit"/></svg>';
  box.innerHTML=o;
  var svg=document.getElementById('svsvg'),hit=document.getElementById('svhit'),vx=document.getElementById('svx');
  hit.addEventListener('mousemove',function(ev){var r=svg.getBoundingClientRect(),px=(ev.clientX-r.left)/r.width*W,q=Math.max(0,Math.min(NS-1,Math.floor((px-L)/cw))),i=ix?SR.at[q]:q;
    var p=P[i],ovt=Object.keys(O).filter(function(k){return OV[k]&&O[k][i]!=null}).map(function(k){return OV[k][0]+' '+n(O[k][i])}).join(' · ');
    vx.setAttribute('x1',x(i));vx.setAttribute('x2',x(i));vx.setAttribute('visibility','visible');
    if(ix){var ses=null;SR.sess.forEach(function(z){if(i>=z.first&&i<=z.last)ses=z});var pcl=ses&&ses.prev_close,ms=S.pat?SR.marks.filter(function(z){return z.i===i}):[];
      tip.innerHTML='<b>'+esc(dayLabel(p.date)+' '+p.time)+'</b> &nbsp;O '+n(p.open)+' H '+n(p.high)+' L '+n(p.low)+' C <b>'+n(p.close)+'</b> &nbsp;Vol '+n(p.volume,0)+
        (p.vwap!=null?' &nbsp;| <span style="color:'+VWAPC+'">VWAP</span> <b>'+n(p.vwap)+'</b>':'')+(pcl?' &nbsp;| prev close '+n(pcl)+' '+pc((p.close/pcl-1)*100):'')+(ovt?' &nbsp;| '+ovt:'')+indTip(N,i)+
        (ms.length?' &nbsp;| '+ms.map(function(z){return '<b style="color:'+(mc[z.m.direction]||mc.BOTH)+'">'+esc(z.m.name)+'</b>'+(z.m.reason?' <span class="mut">'+esc(z.m.reason)+'</span>':'')}).join(', '):'');
      return}
    var s=sm[p.k],sg=d.signals.filter(function(z){return bucketKey(z.signal_date,S.iv)===p.k})[0];
    var ts=S.pat?(sigBy[p.k]||[]):[],cd=S.pat?(cdlBy[p.k]||[]):[],ip=S.pat?inBy[p.k]:null;
    tip.innerHTML='<b>'+(S.iv==='D'?'':(S.iv==='W'?'week to ':'month to '))+esc(p.date)+'</b> &nbsp;O '+n(p.open)+' H '+n(p.high)+' L '+n(p.low)+' C <b>'+n(p.close)+'</b> &nbsp;Vol '+n(p.volume,0)+
      (s?' &nbsp;| ATIP <b>'+n(s.atip_score,0)+'</b> '+esc(s.signal||''):'')+(sg?' &nbsp;| <b style="color:'+(sg.signal==='BUY'?'#22c55e':'#ef4444')+'">'+esc(sg.signal)+' signal</b> @ '+n(sg.entry_price):'')+(ovt?' &nbsp;| '+ovt:'')+indTip(N,i)+
      (ts.length?' &nbsp;| '+ts.map(function(z){return '<b style="color:'+(mc[z.direction]||mc.BOTH)+'">'+(z.pattern?'◆ ':'')+esc(z.name)+'</b>'+(S.iv==='D'?'':' '+esc(z.date.slice(5)))+(z.reason?' <span class="mut">'+esc(z.reason)+'</span>':'')}).join(', '):'')+
      (cd.length?' &nbsp;| candles: '+cd.map(function(z){return '<span style="color:'+(mc[z.side]||'#94a3b8')+'">'+esc(z.name)+'</span>'}).join(', '):'')+
      (ip?' &nbsp;| in place: <span class="mut">'+esc(ip)+'</span>':'')});
  hit.addEventListener('mouseleave',function(){vx.setAttribute('visibility','hidden')});
  if(ix){var I2=S.intra,ds=SR.sess.map(function(z){return z.date});
    tip.innerHTML='Hover the chart for prices. '+esc(SR.iv)+'-minute bars, '+esc(ds.length>1?ds[0]+' to '+ds[ds.length-1]+' ('+ds.length+' sessions, overnight gaps left out)':ds[0])+
      ', regular session 09:15-15:30 IST. <span style="color:'+VWAPC+'">VWAP</span> restarts each session; dashed: previous close'+(I2.prev_close!=null?' '+n(I2.prev_close):'')+'.';
    if(pl){var all=SR.marks;pl.innerHTML=!S.pat?'<span class="mut">Intraday scan marks hidden (Patterns).</span>':
      (all.length?'Intraday scans: '+all.map(function(z){var m=z.m;return '<b style="color:'+(mc[m.direction]||mc.BOTH)+'">'+(m.source==='signal'?(m.direction==='BEAR'?'▼ ':'▲ '):'● ')+esc(m.name)+'</b> '+esc((ds.length>1?m.date.slice(5)+' ':'')+(m.time||''))+(m.price!=null?' @ '+n(m.price):'')}).join(' · ')
        :'<span class="mut">No intraday scan hit '+(ds.length>1?'in these sessions':'in this session')+'.</span>')+
      ' <span class="mut">&nbsp;▲▼ intraday signal (opening range, open = low / high, squeeze) · ● intraday scan (ZPI pullback, breakout, VWAP reclaim, momentum, breakdown risk)</span>'}
    return}
  var pts=M.patterns||[];
  if(pl)pl.innerHTML=!S.pat?'<span class="mut">Chart patterns, technical signals and candle patterns hidden (Patterns).</span>':
    (pts.length?'In place on the last bar: '+pts.map(function(pt){return '<b style="color:'+(mc[pt.side]||mc.BOTH)+'">'+esc(pt.name)+'</b> '+
      (pt.lines||[]).map(function(l){return esc(l.label.toLowerCase())+' '+n(l.to_value)}).join(' / ')+
      (pt.triggered?' <b>· '+(/breakdown$/.test(pt.triggered)?'broke down':'broke out')+' today</b>':'')}).join(' &nbsp;·&nbsp; ')
      :'<span class="mut">No chart pattern in place on the last bar.</span>')+
    ' <span class="mut">&nbsp;◆ chart-pattern breakout · ● other technical signal · • candle pattern (the 20:30 signal run)</span>';
}
function tbl(h,rows,empty){return '<table><thead><tr>'+h.map(function(x){return '<th>'+x+'</th>'}).join('')+'</tr></thead><tbody>'+(rows.join('')||'<tr><td colspan="'+h.length+'" class="mut" style="text-align:center;padding:14px">'+empty+'</td></tr>')+'</tbody></table>'}
function scoresTable(d){return tbl(['Date','ATIP','Rank','VPI','MRI','RRI','ZPI','CRI','ACS','Signal','Regime','Top factor'],d.scores.slice().reverse().map(function(s){
  return '<tr><td>'+esc(s.date)+'</td><td><b>'+n(s.atip_score,0)+'</b></td><td>'+n(s.atip_rank,0)+'</td><td>'+n(s.vpi,0)+'</td><td>'+n(s.mri,0)+'</td><td>'+n(s.rri,0)+'</td><td>'+n(s.zpi,0)+'</td><td>'+n(s.cri,0)+'</td><td>'+n(s.acs,0)+'</td><td>'+esc(s.signal||'')+'</td><td class="mut">'+esc(s.regime||'')+'</td><td class="mut">'+esc(s.top_factor_1||'')+'</td></tr>'}),'No ATIP scores stored for this stock.')}
function signalsTable(d){return tbl(['Issued','Signal','Entry','ATIP','First target hit','Best','Worst','Sessions','Model'],d.signals.map(function(s){var f=s.first_hit;
  return '<tr><td>'+esc(s.signal_date)+'</td><td style="font-weight:600;color:'+(s.signal==='BUY'?'#059669':'#dc2626')+'">'+esc(s.signal)+'</td><td>₹'+n(s.entry_price)+'</td><td>'+n(s.atip_score,0)+'</td><td>'+(f?esc(String(f.hit_date).slice(0,10))+' @'+f.threshold_pct+'%':'<span class="mut">—</span>')+'</td><td>'+pc(s.best_pct)+'</td><td>'+pc(s.worst_pct)+'</td><td>'+n(s.sessions_tracked,0)+'</td><td class="mut">'+esc(s.model_version||'')+'</td></tr>'}),'No BUY / SELL signals logged for this stock.')}
function techTable(d){var t=d.technicals||{},e=d.technical_ext||{},sp=function(v){return v==null?'—':(v>=0?'+':'')+n(v)+'%'},fl={fib_382:'38.2%',fib_500:'50%',fib_618:'61.8%'};
  var rows=[['As of',t.date||e.date],['RSI 14',t.rsi_14],['MACD hist',t.macd_hist],['ADX 14',t.adx_14],['EMA 21',t.ema_21],['EMA 50',t.ema_50],['SMA 200',t.sma_200],['Above 200 DMA',t.above_200dma==null?null:(t.above_200dma?'yes':'no')],['ATR %',t.atr_pct],['Bollinger upper / lower',t.bb_upper!=null?n(t.bb_upper)+' / '+n(t.bb_lower):null],['Pivot / R1 / S1',t.pivot!=null?n(t.pivot)+' / '+n(t.r1)+' / '+n(t.s1):null],['Volume ratio',t.volume_ratio],
    ['Support (touches)',e.sr_support!=null?n(e.sr_support)+' ('+n(e.sr_support_touches,0)+')':null],['Resistance (touches)',e.sr_resistance!=null?n(e.sr_resistance)+' ('+n(e.sr_resistance_touches,0)+')':null],['VWAP 20D',e.vwap_20d],['Supertrend',e.supertrend!=null?n(e.supertrend)+' ('+esc(e.supertrend_dir)+')':null],['Weekly / monthly trend',e.weekly_trend!=null?esc(e.weekly_trend)+' / '+esc(e.monthly_trend):null],['MTF alignment',e.mtf_alignment],['Beta 60D',e.beta_60],['MFI 14',e.mfi_14],
    ['RS vs sector 126D / 63D',e.rs_sector_126!=null||e.rs_sector_63!=null?sp(e.rs_sector_126)+' / '+sp(e.rs_sector_63)+' vs '+(e.rs_sector_index||'')+(e.rs_sector_index==='NIFTY50'?' (no sector index)':''):null],['RS percentile in sector',e.rs_sector_pctile],
    ['Fibonacci swing',e.fib_swing_dir!=null?(e.fib_swing_dir==='UP'?'UP '+n(e.fib_swing_low)+' → '+n(e.fib_swing_high):'DOWN '+n(e.fib_swing_high)+' → '+n(e.fib_swing_low)):null],['Swing Fib 38.2 / 50 / 61.8',e.fib_382!=null?n(e.fib_382)+' / '+n(e.fib_500)+' / '+n(e.fib_618):null],
    ['Nearest swing Fib',e.fib_nearest!=null?(fl[e.fib_nearest]||e.fib_nearest)+(e.fib_nearest_dist_pct!=null?' (close '+sp(e.fib_nearest_dist_pct)+')':''):null]]
    .filter(function(r){return r[1]!=null&&r[1]!==''});
  return tbl(['Indicator','Value'],rows.map(function(r){return '<tr><td>'+r[0]+'</td><td><b>'+(typeof r[1]==='number'?n(r[1]):esc(r[1]))+'</b></td></tr>'}),'No technical indicators stored for this stock.')}
function posTable(d){var h=d.holding,pp=d.paper_position,o='';
  o+='<div class="kv">'+(h?[['LIVE qty',n(h.qty,0)],['Avg price',n(h.avg_price)],['Value',n(h.current_val)],['P&L',n(h.pnl)+' ('+pc(h.pnl_pct)+')'],['Weight',h.weight_pct==null?'—':n(h.weight_pct)+'%'],['Synced',esc(String(h.date).slice(0,10))]]:[['LIVE holding','none']]).map(function(x){return '<div><span>'+x[0]+'</span><b>'+x[1]+'</b></div>'}).join('')+
     (pp?'<div><span>PAPER qty</span><b>'+n(pp.quantity,0)+' @ '+n(pp.avg_price)+'</b></div>':'')+'</div>';
  o+='<div class="nm" style="margin:6px 0 4px">Order rules</div>'+tbl(['Created','Side','Trigger','Price','Qty','Status'],d.order_rules.map(function(r){return '<tr><td>'+esc(String(r.created_at||'').slice(0,16))+'</td><td>'+esc(r.side)+'</td><td>'+esc(r.trigger_type)+' '+n(r.trigger_value)+'</td><td>'+n(r.resolved_trigger_price)+'</td><td>'+esc(r.quantity_type)+' '+n(r.quantity_value)+'</td><td>'+esc(r.status)+'</td></tr>'}),'No order rules.');
  o+='<div class="nm" style="margin:10px 0 4px">Orders placed</div>'+tbl(['When','Side','Qty','Type','Price','Mode','Status'],d.orders.map(function(r){return '<tr><td>'+esc(String(r.timestamp||'').slice(0,16))+'</td><td>'+esc(r.transaction_type)+'</td><td>'+n(r.quantity,0)+'</td><td>'+esc(r.order_type)+'</td><td>'+n(r.price)+'</td><td>'+esc(r.mode)+'</td><td>'+esc(r.status)+(r.error?' <span class="mut">'+esc(r.error)+'</span>':'')+'</td></tr>'}).concat(d.oms_orders.map(function(r){return '<tr><td>'+esc(String(r.created_at||'').slice(0,16))+'</td><td>'+esc(r.side)+'</td><td>'+n(r.quantity,0)+'</td><td>strategy '+esc(r.strategy_id)+'</td><td>'+n(r.avg_fill_price)+'</td><td>W4</td><td>'+esc(r.status)+'</td></tr>'})),'No orders for this stock.');
  return o}

addFilters();restore();
})();
</script>
"""
