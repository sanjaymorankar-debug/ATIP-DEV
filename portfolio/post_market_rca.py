"""
Daily post-market RCA (owner prompt 6 Nov 2026 17:28 IST): what each session did today, the intraday path of every
stock a session held, why each losing trade lost and what alternative rules would have done. Every time is IST.

    build(day)          the report as a dict
    run(day)            build + store it in post_market_rca (shown at /rca and, through the shell, in the apps)
    run_scheduled()     the 16:00 IST job (pipeline/w39_jobs.py): market days only; a no-op on the view-only server
    python -m portfolio.post_market_rca [--date YYYY-MM-DD] [--no-fetch] [--json]

Data, all read-only (it writes only its own table):
  * the sessions' own trades (orders/sessions.py _trades, so every rupee agrees with the Sessions page) and the paper
    fills' times (paper_order.created_at, IST);
  * the signal behind each entry (its rule note: "ATIP HOLD 2026-11-05, score 63.5, RANK-FILL"), that day's ai_scores
    and technical_indicators (a NULL indicator is flagged, never assumed), prices_daily for the previous close;
  * 1-minute candles from Dhan (data/dhan.py fetch_historical_intraday) for every symbol and the Nifty 50 (IDX_I);
    with fetch=False, or on the viewer, the stored 15-minute intraday_bars instead (the report says which).

Rules the report uses (also printed in it):
  recovery start   on 5-minute candles: the first candle after the one holding the low after entry that CLOSES above
                   that candle's high, with the next candle closing above it too (held 2 candles); the time shown is
                   the start of the first of them.
  back to entry    the first 1-minute candle after the low that closes at or above the entry price.
  stopped at low   a stop-loss exit within STOP_NEAR_LOW_PCT of the low after entry.
  what-if          each alternative rule replayed on the same 1-minute candles: a stop fills at the stop, or at the
                   candle's open when the candle opens through it; the session's trail (from +target % the stop
                   trails trail % under the high); a trade still held at the close is marked at the close; charges
                   from backtest/costs.py. A skipped trade counts Rs 0 (the session would have picked another stock or
                   waited). A position carried in from an earlier day is replayed over today's candles only.
None of this trades. The rules it tests are in orders/entry_guards.py, every one OFF until the owner turns it on.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta

log = logging.getLogger("atip.rca")

OPEN_T, CLOSE_T = "09:15", "15:30"
STOP_NEAR_LOW_PCT = 0.5        # a stop-loss exit this close to the low after entry was "stopped at the low"
TIGHT_STOP_ATR = 1.0           # a stop nearer than this many 14-day ATRs sits inside one day's normal range
OPENING_MIN = 30               # entries / stops in the first 30 minutes are "opening volatility"
BORDERLINE_SCORE = 63.0        # rank-fill / HOLD picks under this ATIP score (HOLD >= 62 gives ~6 stocks a day)
HIGH_BETA = 1.5
SLIPPAGE_PCT = 0.25            # a stop filled more than this below its trigger
FILL_DELAY_S = 90              # stop first touched (1-minute low) this long before the fill
NIFTY_WEAK_PCT = -0.5          # "Nifty down" for the market filter
CAUSES = {
    "a": "Market-wide move", "b": "Signal quality", "c": "Entry timing", "d": "Stop-loss design",
    "e": "Target / exit design", "f": "Execution / system", "g": "News / event", "h": "Position sizing / correlation"}
NOTE_RE = re.compile(r"ATIP (\w+) (\d{4}-\d{2}-\d{2}), score ([\d.]+)(?:, ([\w-]+))?")


def _row(r) -> dict:
    return dict(r) if r is not None and not isinstance(r, dict) else (r or {})


def ensure_table(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS post_market_rca (day TEXT PRIMARY KEY, built_at TEXT, source TEXT, "
                 "headline TEXT, payload TEXT)")


# -- candles ------------------------------------------------------------------------------------------------------

def _bars_from_df(df) -> list:
    out = []
    for r in df.itertuples(index=False):
        ts = r.datetime if isinstance(r.datetime, datetime) else datetime.fromisoformat(str(r.datetime)[:19])
        out.append({"t": ts.replace(tzinfo=None), "o": float(r.open), "h": float(r.high), "l": float(r.low),
                    "c": float(r.close), "v": float(getattr(r, "volume", 0) or 0)})
    return out


def _stored_bars(conn, symbol: str, day: str) -> list:
    rows = conn.execute("SELECT ts, open, high, low, close, volume, interval_min FROM intraday_bars WHERE symbol = ? "
                        "AND ts >= ? AND ts < ? ORDER BY interval_min, ts", (symbol, day, _next(day))).fetchall()
    rows = [_row(r) for r in rows]
    if not rows:
        return []
    iv = min(r["interval_min"] for r in rows)
    return [{"t": datetime.fromisoformat(str(r["ts"])[:19]), "o": r["open"], "h": r["high"], "l": r["low"],
             "c": r["close"], "v": r["volume"] or 0} for r in rows if r["interval_min"] == iv]


def _next(day: str) -> str:
    return str(date.fromisoformat(day) + timedelta(days=1))


NIFTY_SID = "13"


def load_candles(conn, symbols, day: str, fetch: bool = True) -> tuple[dict, str]:
    """({symbol: [bar]} with "NIFTY50" for the index, source): Dhan 1-minute candles when fetch is on and this is the
    trading Mac, else the stored 15-minute bars. A symbol Dhan returns nothing for falls back to its stored bars."""
    out, src = {}, "stored 15-minute bars (intraday_bars)"
    client = None
    if fetch:
        try:
            from ops.role import is_viewer
            if not is_viewer():
                from data import dhan as D
                client, _ = D.get_dhan_client()
        except Exception as e:
            log.warning(f"  RCA: Dhan unavailable ({e}); using stored 15-minute bars")
            client = None
    d = date.fromisoformat(day)
    for s in symbols:
        bars = []
        if client is not None:
            try:
                from data import dhan as D
                df = D.fetch_historical_intraday(s, d, d, interval=1, dhan=client)
                bars = _bars_from_df(df) if len(df) else []
            except Exception as e:
                log.warning(f"  RCA: {s} 1-minute candles: {e}")
        out[s] = bars or _stored_bars(conn, s, day)
    if client is not None:
        src = "Dhan 1-minute candles (data/dhan.py)"
        try:
            from data import dhan as D
            r = D._guarded(client.intraday_minute_data, security_id=NIFTY_SID, exchange_segment="IDX_I",
                           instrument_type="INDEX", from_date=day, to_date=day, interval=1)
            data = (r or {}).get("data") or {}
            import pandas as pd
            df = pd.DataFrame({"datetime": D._ist_datetimes(data.get("timestamp", [])), "open": data.get("open", []),
                               "high": data.get("high", []), "low": data.get("low", []), "close": data.get("close", [])})
            out["NIFTY50"] = _bars_from_df(df) if len(df) else []
        except Exception as e:
            log.warning(f"  RCA: Nifty 1-minute candles: {e}")
    out.setdefault("NIFTY50", [])
    return out, src


def resample(bars: list, minutes: int = 5) -> list:
    """1-minute bars -> `minutes` bars aligned on 09:15 (a bar's time is its start, IST)."""
    out, cur = [], None
    for b in bars:
        m = (b["t"].hour * 60 + b["t"].minute - (9 * 60 + 15)) // minutes * minutes + 9 * 60 + 15
        t = b["t"].replace(hour=m // 60, minute=m % 60, second=0)
        if cur and cur["t"] == t:
            cur.update(h=max(cur["h"], b["h"]), l=min(cur["l"], b["l"]), c=b["c"], v=cur["v"] + b["v"])
        else:
            cur = {"t": t, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]}
            out.append(cur)
    return out


def hm(t) -> str | None:
    return t.strftime("%H:%M") if isinstance(t, datetime) else None


# -- the trades -----------------------------------------------------------------------------------------------------

def _fill_time(conn, order_id, fallback) -> datetime | None:
    s = None
    if order_id:
        for sql in ("SELECT created_at AS t FROM paper_order WHERE order_id = ?",
                    "SELECT timestamp AS t FROM order_log WHERE dhan_order_id = ? ORDER BY id LIMIT 1"):
            try:
                r = conn.execute(sql, (order_id,)).fetchone()
            except Exception:
                r = None
            if r and _row(r).get("t"):
                s = _row(r)["t"]
                break
    s = s or fallback
    try:
        return datetime.fromisoformat(str(s).replace("T", " ")[:19]) if s else None
    except ValueError:
        return None


def session_trades(conn, day: str) -> list:
    """Every session trade live on `day`: bought that day, sold that day, or held through it."""
    from orders import sessions as S
    out = []
    for s in S.list_sessions(conn)["sessions"]:
        sid = s["id"]
        rules = S._linked_rules(conn, sid)
        by_id = {r["id"]: r for r in rules}
        legs = {}
        for r in rules:
            if r.get("parent_rule_id"):
                legs.setdefault(r["parent_rule_id"], []).append(r)
        for t in S._trades(rules, S._charges(conn, rules), since=s.get("pool_started_at")):
            if t["date"] > day or (t["closed"] and (t["exit_date"] or "") < day):
                continue
            e = by_id[t["rule_id"]]
            done = [l for l in legs.get(e["id"], []) if l.get("status") == "EXECUTED" and l.get("execution_price")]
            stop_leg = next((l for l in legs.get(e["id"], []) if l.get("role") == "STOP"), {})
            m = NOTE_RE.search(e.get("notes") or "")
            exit_leg = done[-1] if done else None
            reason = None
            if exit_leg:
                if exit_leg.get("role") == "STOP":
                    act = exit_leg.get("trail_activate_price")
                    hw = exit_leg.get("trail_high_water")
                    reason = "Trail stop" if act and hw and float(hw) >= float(act) else "Stop-loss"
                else:
                    reason = "Target" if exit_leg.get("role") == "TARGET" else (exit_leg.get("role") or "Manual")
            stop_pct = float(e.get("bracket_stop_pct") or s.get("stop_pct") or 0) or None
            out.append({
                "session": sid, "session_name": s.get("name"), "rule_id": e["id"], "symbol": t["symbol"],
                "qty": t["qty"], "entry": t["entry"],
                "entry_at": _fill_time(conn, e.get("dhan_order_id"), e.get("updated_at")),
                "picked_at": _fill_time(conn, None, e.get("created_at")),
                # the stop leg is placed at the FILL less stop_pct; the entry's resolved_stoploss_price was worked out
                # from its trigger before the fill, so it is not the stop that ran
                "stop": round(t["entry"] * (1 - stop_pct / 100), 2) if stop_pct else
                (float(e["resolved_stoploss_price"]) if e.get("resolved_stoploss_price") else None),
                "stop_pct": stop_pct,
                "target_pct": float(e.get("trail_after_pct") or e.get("bracket_target_pct") or s.get("target_pct") or 0) or None,
                "trail_pct": float(e.get("trail_value") or 0) or None,
                "trigger": float(e.get("resolved_trigger_price") or 0) or None,
                "signal": m.group(1) if m else None, "signal_date": m.group(2) if m else None,
                "score": float(m.group(3)) if m else None, "source": (m.group(4) if m else None) or "MANUAL",
                "closed_today": bool(t["closed"] and t["exit_date"] == day),
                "exit_price": t["exit_price"] if t["closed"] else None,
                "exit_at": _fill_time(conn, exit_leg.get("dhan_order_id"), exit_leg.get("updated_at")) if exit_leg else None,
                "exit_reason": reason, "pnl_net": t["pnl"] if t["closed"] else None, "charges": t["charges"],
                "stop_trigger": float(stop_leg["resolved_trigger_price"]) if stop_leg.get("resolved_trigger_price") else None,
                "stop_leg_hw": float(stop_leg["trail_high_water"]) if stop_leg.get("trail_high_water") else None,
                "env": "VIRTUAL" if str(e.get("dhan_order_id") or "").startswith("PAPER") else "ACTUAL"})
    return out


_IND: dict = {}


def _industries() -> dict:
    """Symbol -> NSE industry (data/index_constituents.py), read once a day."""
    day = str(date.today())
    if _IND.get("day") != day:
        try:
            from data.index_constituents import get_symbol_industry_map
            _IND.update(day=day, map=get_symbol_industry_map() or {})
        except Exception:
            _IND.update(day=day, map={})
    return _IND["map"]


def _context(conn, t: dict, day: str) -> dict:
    """The signal day's scores and indicators, the previous close and the newest score before `day`."""
    q = lambda sql, a: _row(conn.execute(sql, a).fetchone())
    sd = t["signal_date"] or day
    sc = q("SELECT atip_score, `signal` AS sig, vpi, spi, rri, mri, cri, msi, zpi, acs, beta_1y FROM ai_scores "
           "WHERE symbol = ? AND date = ?", (t["symbol"], sd))
    ti = q("SELECT rsi_14, ema_9, ema_21, ema_50, sma_200, atr_14, atr_pct, adx_14 FROM technical_indicators "
           "WHERE symbol = ? AND date = ?", (t["symbol"], sd))
    pc = q("SELECT date, close FROM prices_daily WHERE symbol = ? AND date < ? AND (series = 'EQ' OR series IS NULL) "
           "ORDER BY date DESC LIMIT 1", (t["symbol"], day))
    last = q("SELECT date, atip_score, `signal` AS sig FROM ai_scores WHERE symbol = ? AND date < ? ORDER BY date DESC "
             "LIMIT 1", (t["symbol"], day))
    for r in (pc, last):                        # a DATE column can come back as a date object
        if r.get("date") is not None:
            r["date"] = str(r["date"])[:10]
    industry = _industries().get(t["symbol"])
    nulls = [k for k in ("rsi_14", "ema_9", "ema_21", "ema_50", "sma_200", "atr_14") if ti.get(k) is None]
    nulls += [k.upper() for k in ("vpi", "spi", "rri", "mri", "cri", "msi", "zpi") if sc and sc.get(k) is None]
    # a position carried in: its best price on the days after its entry day (the entry day's own high may be from
    # before the fill), so the replay starts with the trail where it already was
    prior_high = None
    if t["entry_at"] and str(t["entry_at"])[:10] < day:
        prior_high = q("SELECT MAX(high) AS h FROM prices_daily WHERE symbol = ? AND date > ? AND date < ? "
                       "AND (series = 'EQ' OR series IS NULL)", (t["symbol"], str(t["entry_at"])[:10], day)).get("h")
    prev_day = pc.get("date") or day
    news = q("SELECT AVG(mean_sentiment) AS s, MIN(score) AS lo, SUM(n_articles) AS n FROM news_symbol_score "
             "WHERE symbol = ? AND date BETWEEN ? AND ?", (t["symbol"], prev_day, day)) if _has(conn, "news_symbol_score") else {}
    ann = [_row(r) for r in conn.execute(
        "SELECT substr(broadcast_at, 1, 16) AS at, subject, tone FROM corporate_announcement WHERE symbol = ? "
        "AND broadcast_at >= ? AND broadcast_at < ? ORDER BY broadcast_at", (t["symbol"], prev_day, _next(day))).fetchall()] \
        if _has(conn, "corporate_announcement") else []
    if prior_high and t.get("stop_leg_hw"):           # the monitor's own high-water never saw more than this
        prior_high = min(prior_high, t["stop_leg_hw"])
    return {"scores": sc, "ind": ti, "prev_close": pc.get("close"), "prev_date": pc.get("date"),
            "score_before_day": last, "industry": industry, "null_fields": nulls, "prior_high": prior_high,
            "news": news, "announcements": ann}


def _has(conn, table: str) -> bool:
    try:
        conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchall()
        return True
    except Exception:
        return False


# -- the intraday path ----------------------------------------------------------------------------------------------

def path(t: dict, bars: list, day: str) -> dict:
    """Low / high after entry, the recovery and whether the stop fired before it (rules in the module docstring)."""
    if not bars:
        return {"bars": 0}
    start = t["entry_at"] if t["entry_at"] and t["entry_at"].strftime("%Y-%m-%d") == day else None
    after = [b for b in bars if start is None or b["t"] >= start.replace(second=0)]
    if not after:
        return {"bars": len(bars)}
    lo = min(after, key=lambda b: b["l"])
    hi = max(after, key=lambda b: b["h"])
    entry = t["entry"]
    five = resample(after, 5)
    lo5 = next((i for i, b in enumerate(five) if b["t"] <= lo["t"] < b["t"] + timedelta(minutes=5)), None)
    rec = None
    if lo5 is not None:
        ref = five[lo5]["h"]
        for i in range(lo5 + 1, len(five) - 1):
            if five[i]["c"] > ref and five[i + 1]["c"] > ref:
                rec = five[i]["t"]
                break
    back = next((b["t"] for b in after if b["t"] > lo["t"] and b["c"] >= entry), None)
    close = bars[-1]["c"]
    out = {"bars": len(bars), "day_open": bars[0]["o"], "close": close,
           "low": lo["l"], "low_at": lo["t"], "low_dd_pct": round((lo["l"] - entry) / entry * 100, 2),
           "high": hi["h"], "high_at": hi["t"], "high_pct": round((hi["h"] - entry) / entry * 100, 2),
           "recovery_at": rec, "back_to_entry_at": back,
           "back_to_entry_min": round((back - lo["t"]).total_seconds() / 60) if back else None,
           "close_vs_entry_pct": round((close - entry) / entry * 100, 2)}
    if t.get("exit_at") and t["closed_today"]:
        ex = t["exit_at"]
        out["stop_at_low"] = bool(t["exit_reason"] == "Stop-loss"
                                  and (t["exit_price"] - lo["l"]) / lo["l"] * 100 <= STOP_NEAR_LOW_PCT)
        out["stopped_before_recovery"] = bool(t["exit_reason"] == "Stop-loss" and (
            (rec and ex <= rec + timedelta(minutes=5)) or (close > t["exit_price"] and not rec)))
        touch = next((b["t"] for b in after if t.get("stop_trigger") and b["l"] <= t["stop_trigger"]), None) \
            if t["exit_reason"] == "Stop-loss" else None
        out["stop_first_touch_at"] = touch
        out["fill_delay_s"] = round((ex - touch).total_seconds()) if touch and ex >= touch else None
    return out


def nifty_change(nifty: list, prev_close: float | None, at: datetime | None) -> float | None:
    """Nifty 50 % change from the previous close to the 1-minute candle at `at` (IST)."""
    if not nifty or not prev_close or at is None:
        return None
    b = [x for x in nifty if x["t"] <= at]
    return round((b[-1]["c"] - prev_close) / prev_close * 100, 2) if b else None


# -- root causes ----------------------------------------------------------------------------------------------------

def classify(t: dict, ctx: dict, p: dict, nifty: list, nifty_prev: float | None, book: list) -> list:
    """[(code, cause, evidence)] for a losing trade; codes a-h as in the owner's prompt (CAUSES)."""
    out = []
    sc, ind = ctx["scores"] or {}, ctx["ind"] or {}
    entry, day_open, pc = t["entry"], p.get("day_open"), ctx.get("prev_close")
    beta = sc.get("beta_1y")
    # a) market: the beta-adjusted Nifty move from the previous close to the stock's low / the exit
    at = t.get("exit_at") if t["closed_today"] else p.get("low_at")
    nchg = nifty_change(nifty, nifty_prev, at)
    if nchg is not None and pc:
        stock = ((t["exit_price"] if t["closed_today"] else p["low"]) - pc) / pc * 100
        expected = (beta or 1.0) * nchg
        if nchg <= NIFTY_WEAK_PCT or (stock < 0 and expected < 0 and expected / stock >= 0.5):
            out.append(("a", CAUSES["a"], f"Nifty {nchg:+.2f}% at {hm(at)} IST; beta {beta or 1:.2f} explains "
                                           f"{expected:+.2f}% of the stock's {stock:+.2f}%"))
        else:
            t["market_note"] = (f"Not market-wide: Nifty {nchg:+.2f}% at {hm(at)} IST (beta {beta or 1:.2f} -> "
                                f"{expected:+.2f}%) while the stock was {stock:+.2f}% on its previous close")
    # b) signal quality
    why = []
    if t["signal"] and t["signal"] != "BUY":
        why.append(f"not a BUY: ATIP {t['signal']} {t['score']:g} picked by {t['source']}")
    if t["score"] is not None and t["score"] < BORDERLINE_SCORE:
        why.append(f"score {t['score']:g} under {BORDERLINE_SCORE:g}")
    last = ctx.get("score_before_day") or {}
    if t["signal_date"] and last.get("date") and last["date"] > t["signal_date"] and last.get("atip_score") is not None \
            and t["score"] is not None and last["atip_score"] < t["score"]:
        why.append(f"stale: by {last['date']} the score was {last['atip_score']:.2f} ({last['sig']})")
    if all(ind.get(k) for k in ("ema_9", "ema_21", "ema_50")) and ind["ema_9"] < ind["ema_21"] < ind["ema_50"]:
        why.append(f"conflicting trend: EMA9 {ind['ema_9']:.1f} < EMA21 {ind['ema_21']:.1f} < EMA50 {ind['ema_50']:.1f}")
    if ctx["null_fields"]:
        why.append("NULL inputs: " + ", ".join(ctx["null_fields"]))
    if why:
        out.append(("b", CAUSES["b"], "; ".join(why)))
    # c) entry timing
    why = []
    if t["entry_at"] and t["entry_at"].strftime("%H:%M") < _plus(OPEN_T, OPENING_MIN):
        on = "" if t["entry_at"].strftime("%Y-%m-%d") == p.get("_day") else t["entry_at"].strftime(" on %d %b")
        why.append(f"entered {hm(t['entry_at'])} IST{on}, inside the first {OPENING_MIN} min of the session")
    if pc and day_open and t["entry_at"] and t["entry_at"].strftime("%Y-%m-%d") == p.get("_day") and day_open < pc * 0.99:
        why.append(f"bought into a {(day_open - pc) / pc * 100:+.2f}% gap-down open")
    if why:
        out.append(("c", CAUSES["c"], "; ".join(why)))
    # d) stop-loss design
    atr = ind.get("atr_14")
    if t.get("stop") and atr:
        k = (entry - t["stop"]) / atr
        if k < TIGHT_STOP_ATR and (p.get("stopped_before_recovery") or p.get("stop_at_low")):
            where = (f"stopped at the low ({p['low']:.2f}, {hm(p['low_at'])} IST)" if p.get("stop_at_low") else
                     f"stopped {hm(t['exit_at'])} IST; the price fell on to {p['low']:.2f} ({hm(p['low_at'])} IST)")
            out.append(("d", CAUSES["d"], f"stop {entry - t['stop']:.2f} below entry = {k:.2f}x ATR14 ({atr:.2f}, "
                                           f"{ind.get('atr_pct') or 0:.2f}% of price): inside one day's normal range; "
                                           f"{where}, then closed at {p['close']:.2f} "
                                           f"({p['close_vs_entry_pct']:+.2f}% vs entry)"))
    # e) target / exit design: a trade that was +1R and still lost
    if t.get("stop_pct") and p.get("high_pct") is not None and p["high_pct"] >= t["stop_pct"]:
        out.append(("e", CAUSES["e"], f"was {p['high_pct']:+.2f}% (>= +1R) at {hm(p['high_at'])} IST and gave it back: "
                                       f"no break-even move below the +{t.get('target_pct') or 3:g}% trail start"))
    # f) execution
    why, slip_rs = [], 0.0
    if t["closed_today"] and t.get("stop_trigger") and t["exit_reason"] == "Stop-loss":
        slip = (t["stop_trigger"] - t["exit_price"]) / t["stop_trigger"] * 100
        slip_rs = max(0.0, (t["stop_trigger"] - t["exit_price"]) * t["qty"])
        if slip > SLIPPAGE_PCT:
            why.append(f"filled {t['exit_price']:.2f}, {slip:.2f}% under the {t['stop_trigger']:.2f} stop "
                       f"(Rs {(t['stop_trigger'] - t['exit_price']) * t['qty']:,.0f} slippage)")
        if (p.get("fill_delay_s") or 0) > FILL_DELAY_S:
            why.append(f"a 1-minute candle reached the stop at {hm(p['stop_first_touch_at'])} IST; the 30-second "
                       f"price poll first saw it at {hm(t['exit_at'])} IST ({p['fill_delay_s'] // 60} min later)")
    if why:            # what execution cost is the fill under the stop, not the whole loss
        out.append(("f", CAUSES["f"], "; ".join(why), -round(slip_rs, 2)))
    # g) news / events: negative news or a negative-toned filing on the day or the day before
    news, ann = ctx.get("news") or {}, ctx.get("announcements") or []
    bad = [a for a in ann if (a.get("tone") or 0) < 0]
    if (news.get("s") is not None and news["s"] < 0) or bad:
        out.append(("g", CAUSES["g"], "; ".join(
            ([f"news sentiment {news['s']:+.2f} over {int(news.get('n') or 0)} article(s)"] if news.get("s") is not None
             and news["s"] < 0 else []) + [f"{a['at']} IST: {a['subject']} (tone {a['tone']:+.2f})" for a in bad])))
    else:
        t["news_note"] = (f"No negative news: sentiment {news['s']:+.2f} over {int(news.get('n') or 0)} article(s)"
                          if news.get("s") is not None else "No scored news") + (
            f"; filings: {'; '.join(a['subject'] for a in ann)}" if ann else "; no filings")
    # h) sizing / correlation
    why = []
    if beta and beta >= HIGH_BETA:
        why.append(f"beta {beta:.2f}: the whole session's cash in one high-beta stock")
    same = [b["symbol"] for b in book if b["symbol"] != t["symbol"] and ctx.get("industry")
            and b.get("industry") == ctx["industry"]]
    if same:
        why.append(f"{ctx['industry']}: also held {', '.join(sorted(set(same)))}")
    if why:
        out.append(("h", CAUSES["h"], "; ".join(why)))
    return out


def _plus(hhmm: str, minutes: int) -> str:
    h, m = map(int, hhmm.split(":"))
    m += minutes
    return f"{h + m // 60:02d}:{m % 60:02d}"


# -- what-if ----------------------------------------------------------------------------------------------------------

def simulate(bars: list, entry: float, qty: float, stop: float | None, target_pct: float | None,
             trail_pct: float | None, start: datetime | None = None, stop_from: str | None = None,
             breakeven_r: float | None = None, high_water: float | None = None) -> dict:
    """Replay a long position over 1-minute bars: {exit, at, reason}. The stop fills at the stop, or at the bar's open
    when the bar opens through it; from +target_pct the stop trails trail_pct under the high (high_water: the best
    price before the first bar, for a position carried in); with breakeven_r the stop moves to the entry once the
    price is that many % up; stop_from ("09:45") ignores the stop before then. A position still held at the end is
    marked at the last close (reason "held")."""
    hw = max(entry, high_water or 0)
    act = entry * (1 + target_pct / 100) if target_pct else None

    def lift(stop):
        if trail_pct and act and hw >= act:
            stop = max(stop or 0, hw * (1 - trail_pct / 100))
        if breakeven_r and hw >= entry * (1 + breakeven_r / 100):
            stop = max(stop or 0, entry)
        return stop

    stop = lift(stop)
    for b in bars:
        if start and b["t"] < start.replace(second=0):
            continue
        armed = not stop_from or b["t"].strftime("%H:%M") >= stop_from
        if stop and armed and b["l"] <= stop:
            return {"exit": min(stop, b["o"]), "at": b["t"], "reason": "stop"}
        hw = max(hw, b["h"])
        stop = lift(stop)
    return {"exit": bars[-1]["c"] if bars else entry, "at": bars[-1]["t"] if bars else None, "reason": "held"}


ALT_RULES = [
    ("actual", "Today's rules (the real fills; positions still held marked at the close)"),
    ("atr_1.0", "Stop 1.0 x ATR14 under entry (same shares)"),
    ("atr_1.5", "Stop 1.5 x ATR14 under entry (same shares)"),
    ("atr_2.0", "Stop 2.0 x ATR14 under entry (same shares)"),
    ("atr_1.5_risk", "Stop 1.5 x ATR14, shares cut so the rupee risk equals the 2 % stop's"),
    ("stop_after_0945", "No stop-loss action before 09:45 IST (the stop is armed from 09:45)"),
    ("entry_after_0945", "No new buy before 09:45 IST (same price band)"),
    ("orb_30", "Buy only on a break above the 09:15-09:44 high, after 09:45"),
    ("revalidate_15m", "Buy only if the first 15-min candle is green and the price is above the previous close"),
    ("skip_gap_1", "Skip a buy when the stock opened more than 1 % below its previous close"),
    ("skip_nifty_weak", "Skip a buy while Nifty is below its open or 0.5 %+ down on the day"),
    ("min_score_63", "Pick only stocks with ATIP score 63+ (rank-fill / HOLD cut)"),
    ("buy_only", "Pick BUY signals only (rank-fill off)"),
    ("breakeven_1r", "Move the stop to the entry once the trade is +1R (+2 %)"),
    ("sector_cap_1", "At most one session per industry"),
]
# tools/rca_validate_rules.py, run 6 Nov 2026 18:40 IST: auto-pick's first 10 picks of every scored day 27 Jul - 5 Nov
# 2026 (420 trades on 42 sessions, Dhan 5-minute candles, Rs 25,000 a trade). Today's rules: 31.7 % won, -0.64 % a trade.
# (avg net % a trade, days better, days worse, verdict). Re-run the tool before acting on any of these.
STUDY = {"when": "6 Nov 2026, 42 sessions / 420 trades (27 Jul - 5 Nov 2026 signals)", "base_avg_pct": -0.64, "rules": {
    "atr_1.0": (-1.02, 14, 27, "REJECT: bigger losses than the 2 % stop"),
    "atr_1.5": (-1.54, 11, 30, "REJECT: 2.4x the loss of the 2 % stop"),
    "atr_2.0": (-1.84, 12, 29, "REJECT: bigger losses than the 2 % stop"),
    "atr_1.5_risk": (-0.48, 23, 19, "WEAK: fewer rupees at work, mixed days"),
    "stop_after_0945": (-0.73, 14, 27, "REJECT"),
    "entry_after_0945": (-0.43, 26, 16, "MIXED: the second half was no better"),
    "orb_30": (-0.31, 30, 12, "BEST SINGLE RULE: both halves; guard_orb_minutes"),
    "revalidate_15m": (-0.48, 30, 12, "PROMISING: 155 of 420 trades"),
    "skip_gap_1": (-0.66, 17, 7, "NO EFFECT"),
    "skip_nifty_weak": (-0.44, 19, 5, "PROMISING: guard_nifty_weak"),
    "min_score_63": (-0.45, 21, 7, "PROMISING: guard_min_score; with ORB about +0.05 %"),
    "buy_only": (-0.39, None, None, "FEW TRADES: 36 BUY picks in 42 sessions"),
    "breakeven_1r": (-0.56, 19, 6, "SMALL"),
    "sector_cap_1": (None, None, None, "NOT BACK-TESTED: a risk cap")}}
ENTRY_RULES = ("entry_after_0945", "orb_30", "revalidate_15m", "skip_gap_1", "skip_nifty_weak", "sector_cap_1")


def what_if(trades: list, bars: dict, ctxs: dict, day: str) -> dict:
    """{rule: {"label", "pnl", "delta", "trades": [{symbol, session, pnl, exit, at, how}]}}: each rule replayed over the
    day. pnl is TODAY's P&L of the position, as the sessions' day MTM counts it: from the previous close (a position
    carried in) or the fill (bought today) to the exit or today's close, less today's charges. A rule that would not
    have bought the stock counts Rs 0 (the session would have picked another stock or waited)."""
    from backtest.costs import cost_model_from_config
    model = cost_model_from_config()
    nifty = bars.get("NIFTY50") or []
    n_open = nifty[0]["o"] if nifty else None
    nprev = ctxs.get("_nifty_prev")
    held_ind = {}
    res = {k: {"label": lbl, "pnl": 0.0, "trades": []} for k, lbl in ALT_RULES}
    for t in sorted(trades, key=lambda t: t["entry_at"] or datetime.min):
        b = bars.get(t["symbol"]) or []
        ctx = ctxs[t["rule_id"]]
        ind, entry, qty = ctx["ind"] or {}, t["entry"], t["qty"]
        today = bool(t["entry_at"] and t["entry_at"].strftime("%Y-%m-%d") == day)
        atr, pc, sp = ind.get("atr_14"), ctx.get("prev_close"), (t["stop_pct"] or 2.0)
        ref0 = entry if today else (pc or entry)

        def day_pnl(en, ex, q, sold, bought_today=today):
            ref = en if bought_today else ref0
            v = (ex - ref) * q
            if bought_today:
                v -= model.total("BUY", en * q)
            if sold:
                v -= model.total("SELL", ex * q)
            return round(v, 2)

        def run(stop=t["stop"], st=None, en=entry, q=qty, **kw):
            st = st or (t["entry_at"] if today else None)
            r = simulate(b, en, q, stop, t["target_pct"], t["trail_pct"], st,
                         high_water=None if today else ctx.get("prior_high"), **kw)
            return day_pnl(en, r["exit"], q, r["reason"] == "stop"), r

        if t["closed_today"]:
            out = {"actual": (day_pnl(entry, t["exit_price"], qty, True),
                              {"exit": t["exit_price"], "at": t["exit_at"], "reason": "stop"})}
        else:
            out = {"actual": (day_pnl(entry, b[-1]["c"] if b else entry, qty, False),
                              {"exit": b[-1]["c"] if b else entry, "at": b[-1]["t"] if b else None, "reason": "held"})}
        for k in (1.0, 1.5, 2.0):
            out[f"atr_{k}"] = run(stop=entry - k * atr) if atr else out["actual"]
        if atr and t["stop"]:
            out["atr_1.5_risk"] = run(stop=entry - 1.5 * atr, q=max(1, int(qty * (entry - t["stop"]) / (1.5 * atr))))
        else:
            out["atr_1.5_risk"] = out["actual"]
        out["stop_after_0945"] = run(stop_from="09:45")
        out["breakeven_1r"] = run(breakeven_r=sp)
        skip = (0.0, None)
        if today and b:
            picked = max(t.get("picked_at") or t["entry_at"], t["entry_at"].replace(hour=9, minute=15, second=0))
            pick_hm = picked.strftime("%H:%M")
            if t["entry_at"].strftime("%H:%M") >= "09:45":
                out["entry_after_0945"] = out["actual"]
            else:
                late = [x for x in b if x["t"].strftime("%H:%M") >= max("09:45", pick_hm)
                        and (not t["trigger"] or x["o"] <= t["trigger"])]
                out["entry_after_0945"] = run(st=late[0]["t"], en=late[0]["o"]) if late else skip
            rng = [x for x in b if "09:15" <= x["t"].strftime("%H:%M") < "09:45"]
            orh = max(x["h"] for x in rng) if rng else None
            brk = next((x for x in b if x["t"].strftime("%H:%M") >= max("09:45", pick_hm) and orh and x["h"] > orh), None)
            if brk:
                px = max(orh, brk["o"])
                out["orb_30"] = run(st=brk["t"], en=px, stop=px * (1 - sp / 100), q=max(1, int(entry * qty / px)))
            else:
                out["orb_30"] = skip
            first = [x for x in b if "09:15" <= x["t"].strftime("%H:%M") < "09:30"]
            green = bool(first and first[-1]["c"] >= first[0]["o"])
            if t["entry_at"].strftime("%H:%M") < "09:30":
                at930 = next((x for x in b if x["t"].strftime("%H:%M") >= "09:30"), None)
                ok = green and at930 and pc and at930["o"] >= pc
                out["revalidate_15m"] = run(st=at930["t"], en=at930["o"], stop=at930["o"] * (1 - sp / 100),
                                            q=max(1, int(entry * qty / at930["o"]))) if ok else skip
            else:
                out["revalidate_15m"] = out["actual"] if green and pc and entry >= pc else skip
            gap = (b[0]["o"] - pc) / pc * 100 if pc else 0.0
            out["skip_gap_1"] = skip if gap < -1.0 else out["actual"]
            nchg = nifty_change(nifty, nprev, t["entry_at"])
            n_now = [x for x in nifty if x["t"] <= t["entry_at"]]
            weak = (nchg is not None and nchg <= NIFTY_WEAK_PCT) or bool(n_now and n_open and n_now[-1]["c"] < n_open)
            out["skip_nifty_weak"] = skip if weak else out["actual"]
            clash = ctx.get("industry") and any(
                v["industry"] == ctx["industry"] and v["start"] <= t["entry_at"] and (v["end"] is None or v["end"] > t["entry_at"])
                for v in held_ind.values())
            out["sector_cap_1"] = skip if clash else out["actual"]
        else:                                    # entry rules do not touch a position bought on an earlier day
            for k in ENTRY_RULES:
                out[k] = out["actual"]
        out["min_score_63"] = out["actual"] if t["signal"] is None or (t["score"] or 0) >= BORDERLINE_SCORE else skip
        out["buy_only"] = out["actual"] if t["signal"] in ("BUY", None) else skip
        held_ind[t["rule_id"]] = {"industry": ctx.get("industry"), "start": t["entry_at"] or datetime.min,
                                  "end": t["exit_at"] if t["closed_today"] else None}
        for k, _ in ALT_RULES:
            pnl, r = out[k]
            res[k]["pnl"] = round(res[k]["pnl"] + pnl, 2)
            res[k]["trades"].append({"symbol": t["symbol"], "session": t["session"], "pnl": pnl,
                                     "exit": round(r["exit"], 2) if r else None, "at": hm(r["at"]) if r else None,
                                     "how": (r or {}).get("reason") or "not bought"})
    base = res["actual"]["pnl"]
    for k in res:
        res[k]["delta"] = round(res[k]["pnl"] - base, 2)
    return res


# -- sessions ------------------------------------------------------------------------------------------------------

def session_summary(trades: list, bars: dict, ctxs: dict, day: str, conn) -> list:
    """Per session: trades bought today, closed today, wins / losses, gross / charges / net realised today, the day's
    mark-to-market P&L and the deepest fall of the session's day P&L from its high (max drawdown), all in rupees."""
    from backtest.costs import cost_model_from_config
    from orders import sessions as S
    model = cost_model_from_config()
    out = []
    grid = sorted({b["t"] for s, bs in bars.items() if s != "NIFTY50" for b in bs})
    for s in S.list_sessions(conn)["sessions"]:
        sid = s["id"]
        mine = [t for t in trades if t["session"] == sid]
        bought = [t for t in mine if t["entry_at"] and t["entry_at"].strftime("%Y-%m-%d") == day]
        closed = [t for t in mine if t["closed_today"]]
        gross = sum((t["exit_price"] - t["entry"]) * t["qty"] for t in closed)
        net = sum(t["pnl_net"] for t in closed)
        # the day's P&L path: each position from its reference (previous close if carried in, else the fill), the
        # charges of today's fills when they happen
        path_ = []
        for g in grid:
            v = 0.0
            for t in mine:
                ctx = ctxs[t["rule_id"]]
                ref = t["entry"] if t in bought else (ctx.get("prev_close") or t["entry"])
                if t in bought and g < t["entry_at"].replace(second=0):
                    continue
                if t["closed_today"] and t["exit_at"] and g >= t["exit_at"].replace(second=0):
                    px = t["exit_price"]
                    v -= model.total("SELL", px * t["qty"])
                else:
                    bs = [b for b in bars.get(t["symbol"]) or [] if b["t"] <= g]
                    px = bs[-1]["c"] if bs else ref
                if t in bought:
                    v -= model.total("BUY", t["entry"] * t["qty"])
                v += (px - ref) * t["qty"]
            path_.append((g, v))
        peak, dd, dd_at, low, low_at = 0.0, 0.0, None, 0.0, None
        for g, v in path_:
            peak = max(peak, v)
            if peak - v > dd:
                dd, dd_at = peak - v, g
            if v < low:
                low, low_at = v, g
        mtm = path_[-1][1] if path_ else 0.0
        try:
            L = S.ledger(conn, sid, s) or {}
        except Exception:
            L = {}
        start_eq = (L.get("equity") or float(s.get("start_capital") or 0)) - mtm
        wins = sum(1 for t in closed if t["pnl_net"] > 0)
        out.append({"session": sid, "name": s.get("name"), "bought": len(bought), "closed": len(closed),
                    "wins": wins, "losses": len(closed) - wins,
                    "win_rate_pct": round(wins / len(closed) * 100, 1) if closed else None,
                    "gross": round(gross, 2), "charges": round(gross - net, 2), "net": round(net, 2),
                    "net_pct": round(net / start_eq * 100, 2) if start_eq else None,
                    "day_mtm": round(mtm, 2), "day_mtm_pct": round(mtm / start_eq * 100, 2) if start_eq else None,
                    "max_dd": round(dd, 2), "max_dd_pct": round(dd / start_eq * 100, 2) if start_eq else None,
                    "max_dd_at": hm(dd_at), "worst": round(low, 2), "worst_at": hm(low_at),
                    "start_equity": round(start_eq, 2), "held": [t["symbol"] for t in mine if not t["closed_today"]],
                    "env": "VIRTUAL"})
    return out


# -- the report -----------------------------------------------------------------------------------------------------

def build(day: str | None = None, conn=None, fetch: bool = True, bars: dict | None = None) -> dict:
    """The day's RCA as a JSON-able dict. `bars` ({symbol: [bar]} with "NIFTY50") replaces the candle fetch (tests)."""
    from db.schema import get_connection
    own = conn is None
    conn = conn or get_connection()
    try:
        day = day or str(date.today())
        trades = session_trades(conn, day)
        syms = sorted({t["symbol"] for t in trades})
        src = "given"
        if bars is None:
            bars, src = load_candles(conn, syms, day, fetch)
        r = _row(conn.execute("SELECT close FROM prices_daily WHERE symbol = 'NIFTY50' AND date < ? ORDER BY date DESC "
                              "LIMIT 1", (day,)).fetchone())
        nprev = r.get("close")
        ctxs = {t["rule_id"]: _context(conn, t, day) for t in trades}
        ctxs["_nifty_prev"] = nprev
        book = [{"symbol": t["symbol"], "industry": ctxs[t["rule_id"]]["industry"]} for t in trades]
        rows = []
        for t in trades:
            ctx = ctxs[t["rule_id"]]
            p = path(t, bars.get(t["symbol"]) or [], day)
            p["_day"] = day
            pc = ctx.get("prev_close")
            losing = (t["closed_today"] and (t["pnl_net"] or 0) < 0) or \
                     (not t["closed_today"] and p.get("close") is not None and p["close"] < t["entry"])
            causes = classify(t, ctx, p, bars.get("NIFTY50") or [], nprev, book) if losing and p.get("bars") else []
            sc = ctx["scores"] or {}
            rows.append({
                "session": t["session"], "symbol": t["symbol"], "qty": t["qty"], "industry": ctx["industry"],
                "entry": t["entry"], "entry_at": t["entry_at"].strftime("%Y-%m-%d %H:%M:%S") if t["entry_at"] else None,
                "carried": bool(t["entry_at"] and t["entry_at"].strftime("%Y-%m-%d") < day),
                "signal": t["signal"], "signal_date": t["signal_date"], "score": t["score"], "source": t["source"],
                "scores": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in sc.items()},
                "atr": (ctx["ind"] or {}).get("atr_14"), "atr_pct": (ctx["ind"] or {}).get("atr_pct"),
                "null_fields": ctx["null_fields"], "prev_close": pc,
                "day_open": p.get("day_open"), "gap_pct": round((p["day_open"] - pc) / pc * 100, 2)
                if pc and p.get("day_open") else None,
                "low": p.get("low"), "low_at": hm(p.get("low_at")), "low_dd_pct": p.get("low_dd_pct"),
                "high": p.get("high"), "high_at": hm(p.get("high_at")), "high_pct": p.get("high_pct"),
                "recovery_at": hm(p.get("recovery_at")), "back_to_entry_at": hm(p.get("back_to_entry_at")),
                "back_to_entry_min": p.get("back_to_entry_min"), "close": p.get("close"),
                "close_vs_entry_pct": p.get("close_vs_entry_pct"),
                "stop": t["stop"], "stop_trigger": t["stop_trigger"],
                "target_at": round(t["entry"] * (1 + t["target_pct"] / 100), 2) if t["target_pct"] else None,
                "exit_price": t["exit_price"], "exit_at": hm(t["exit_at"]) if t["closed_today"] else None,
                "exit_reason": t["exit_reason"] if t["closed_today"] else "Held at close",
                "pnl_net": t["pnl_net"] if t["closed_today"] else None,
                "mtm_close": round((p["close"] - t["entry"]) * t["qty"], 2) if p.get("close") and not t["closed_today"] else None,
                "stop_at_low": p.get("stop_at_low"), "stopped_before_recovery": p.get("stopped_before_recovery"),
                "stop_first_touch_at": hm(p.get("stop_first_touch_at")), "fill_delay_s": p.get("fill_delay_s"),
                "losing": losing, "market_note": t.get("market_note"), "news_note": t.get("news_note"),
                "chart": [[hm(b["t"]), round(b["l"], 2), round(b["h"], 2), round(b["c"], 2)]
                          for b in resample(bars.get(t["symbol"]) or [], 5)] if losing else [],
                "causes": [{"code": c[0], "cause": c[1], "evidence": c[2], "rupees": c[3] if len(c) > 3 else None}
                           for c in causes], "env": t["env"],
                "bars": p.get("bars", 0)})
        sessions = session_summary(trades, bars, ctxs, day, conn)
        wi = what_if(trades, bars, ctxs, day) if trades else {}
        loss_rows = [r for r in rows if r["losing"] and r["pnl_net"] is not None]
        by_cause = {}
        for r in loss_rows:
            for c in r["causes"]:
                k = by_cause.setdefault(c["code"], {"code": c["code"], "cause": c["cause"], "rupees": 0.0, "trades": []})
                # Rs of the losing trades this cause was found in (a trade can carry several), except where a cause
                # has its own amount (execution: the fill under the stop)
                k["rupees"] = round(k["rupees"] + (c["rupees"] if c.get("rupees") is not None else r["pnl_net"]), 2)
                k["trades"].append(r["symbol"])
        nifty = bars.get("NIFTY50") or []
        tot = {k: round(sum(s[k] for s in sessions), 2) for k in ("gross", "charges", "net", "day_mtm")}
        tot.update(bought=sum(s["bought"] for s in sessions), closed=sum(s["closed"] for s in sessions),
                   wins=sum(s["wins"] for s in sessions), losses=sum(s["losses"] for s in sessions))
        ranked = []
        for k, v in wi.items():
            if k == "actual":
                continue
            st = STUDY["rules"].get(k) or (None, None, None, "")
            ranked.append(dict(v, rule=k, study_avg_pct=st[0], study_better=st[1], study_worse=st[2], verdict=st[3]))
        ranked.sort(key=lambda v: -v["delta"])
        return {
            "day": day, "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S IST"), "candles": src, "tz": "IST",
            "env_note": "All session trades today were PAPER (VIRTUAL)." if all(r["env"] == "VIRTUAL" for r in rows)
            else "Includes LIVE (ACTUAL) trades: money is shown per env, never added.",
            "market": {"nifty_prev_close": nprev, "nifty_open": nifty[0]["o"] if nifty else None,
                       "nifty_close": nifty[-1]["c"] if nifty else None,
                       "nifty_low": min((b["l"] for b in nifty), default=None),
                       "nifty_low_at": hm(min(nifty, key=lambda b: b["l"])["t"]) if nifty else None,
                       "nifty_chg_pct": round((nifty[-1]["c"] - nprev) / nprev * 100, 2) if nifty and nprev else None},
            "sessions": sessions, "totals": tot, "trades": rows, "causes_ranked": sorted(
                by_cause.values(), key=lambda v: v["rupees"]), "what_if": wi, "rules_ranked": ranked,
            "rules_used": {"recovery": "first 5-min candle after the low's candle closing above that candle's high, "
                                       "the next candle closing above it too; time = its start (IST)",
                           "back_to_entry": "first 1-min candle after the low closing at/above the entry",
                           "stop_at_low": f"stop-loss exit within {STOP_NEAR_LOW_PCT}% of the low after entry",
                           "tight_stop": f"stop nearer than {TIGHT_STOP_ATR}x ATR14",
                           "what_if": "replayed on the same candles; skipped trade = Rs 0; carried positions over "
                                      "today's candles only; one day is not evidence: validate over 20+ sessions"},
            "study": {"when": STUDY["when"], "base_avg_pct": STUDY["base_avg_pct"]},
            "headline": _headline(tot, loss_rows, by_cause, ranked)}
    finally:
        if own:
            conn.close()


def _headline(tot, losses, by_cause, ranked) -> str:
    top = sorted(by_cause.values(), key=lambda v: v["rupees"])[:3]
    # a fix is one that helped today AND held up in the study (verdict BEST / PROMISING), best study result first
    fixes = sorted((r for r in ranked if r["delta"] > 0 and str(r.get("verdict")).startswith(("BEST", "PROMISING"))),
                   key=lambda r: -(r["study_avg_pct"] or -99))[:3]
    # SR-01 (session review 20 Nov 2026): a losing trade is counted under every cause found in it, so the cause totals
    # overlap -- 16 Nov read "Signal quality (Rs -1,203), Stop-loss design (Rs -1,203), Position sizing (Rs -1,203)"
    # for Rs 1,203 lost in all. Say what was lost and that the causes overlap.
    lost = -sum(float(r.get("pnl_net") or 0) for r in losses)
    overlap = (f" (Rs {lost:,.0f} lost in {len(losses)} trade(s) in all; a trade is counted under each of its causes, "
               f"so these overlap)") if any(len(r.get("causes") or []) > 1 for r in losses) else ""
    return (f"Realised net Rs {tot['net']:,.2f} ({tot['closed']} closed, {len(losses)} loss(es)); day MTM "
            f"Rs {tot['day_mtm']:,.2f}. Top causes: " + (", ".join(f"{c['cause']} (Rs {c['rupees']:,.0f})" for c in top)
                                                     or "none") + overlap +
            ". Fixes that helped today and held up over " + STUDY["when"].split(", ")[1].split(" (")[0] + ": " +
            (", ".join(f"{r['label']} ({r['delta']:+,.0f} today)" for r in fixes) or "none") + ".")


def _jsonable(o):
    if isinstance(o, datetime):
        return o.strftime("%Y-%m-%d %H:%M:%S")
    raise TypeError(type(o).__name__)


def run(day: str | None = None, fetch: bool = True, conn=None) -> dict:
    """Build the day's report and store it (post_market_rca, one row a day, rebuilt on a re-run)."""
    from db.schema import get_connection
    own = conn is None
    conn = conn or get_connection()
    try:
        rep = build(day, conn, fetch)
        ensure_table(conn)
        conn.execute("INSERT OR REPLACE INTO post_market_rca (day, built_at, source, headline, payload) VALUES (?,?,?,?,?)",
                     (rep["day"], rep["built_at"], rep["candles"], rep["headline"], json.dumps(rep, default=_jsonable)))
        conn.commit()
        return {"status": "SUCCESS", "day": rep["day"], "trades": len(rep["trades"]), "headline": rep["headline"]}
    finally:
        if own:
            conn.close()


def latest(conn, day: str | None = None) -> dict | None:
    """The stored report of `day` (default: the newest one)."""
    try:
        ensure_table(conn)
        r = conn.execute("SELECT payload FROM post_market_rca WHERE day = ?" if day else
                         "SELECT payload FROM post_market_rca ORDER BY day DESC LIMIT 1", (day,) if day else ()).fetchone()
    except Exception:
        return None
    return json.loads(_row(r)["payload"]) if r else None


def days(conn, limit: int = 30) -> list:
    try:
        ensure_table(conn)
        return [_row(r)["day"] for r in conn.execute("SELECT day FROM post_market_rca ORDER BY day DESC LIMIT ?",
                                                     (limit,)).fetchall()]
    except Exception:
        return []


def run_scheduled(now: datetime | None = None) -> dict:
    """The 16:00 IST job: market days only; the view-only server never builds (it shows the Mac's stored reports)."""
    from ops.role import is_viewer
    from utils.trading_calendar import is_trading_day
    now = now or datetime.now()
    if is_viewer():
        return {"status": "SKIPPED", "reason": "view-only server"}
    if not is_trading_day(now.date()):
        return {"status": "SKIPPED", "reason": "not a market day"}
    return run(str(now.date()))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Daily post-market RCA (IST)")
    ap.add_argument("--date")
    ap.add_argument("--no-fetch", action="store_true", help="use the stored 15-minute bars, no Dhan call")
    ap.add_argument("--json", action="store_true", help="print the report instead of storing it")
    a = ap.parse_args()
    if a.json:
        print(json.dumps(build(a.date, fetch=not a.no_fetch), default=_jsonable, indent=1))
    else:
        print(run(a.date, fetch=not a.no_fetch))
