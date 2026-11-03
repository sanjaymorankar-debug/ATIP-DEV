"""
W6 (options builder upgrade, 3 Nov 2026; benchmark Sensibull / Kite option chain): the option chain behind the
Option chain card on /options-builder (dashboard/static/atip/options.js + options.css). Read-only: nothing here
places an order; the card prices its legs with the existing POST /api/options/analyse (dashboard/w39_routes.py).

    GET /api/v2/options/chain/{symbol}?expiry=YYYY-MM-DD&strikes=10        no token (a GET)
        symbol            the F&O underlying (NIFTY50 -> NIFTY)
        status            OK | NO_DATA; NO_DATA carries `message` and empty rows
        source            "live" (the intraday option-chain feed's latest snapshot) | "bhavcopy" (NSE F&O, end of day)
        source_label      the same in words
        as_of             "YYYY-MM-DD HH:MM", IST: the snapshot's time, or 15:30 of the bhavcopy's session
        as_of_date        its date
        last_trading_day  the session the data should have reached by now (live: today from 09:15 on a trading day;
                          bhavcopy: today from 18:30 on a trading day; else the previous trading day)
        stale             as_of_date < last_trading_day; stale_reason says so in words
        spot, spot_source the underlying the chain was quoted against (else the latest stored price)
        lot_size, strike_step, risk_free
        expiries          every stored expiry not before the data's date, ascending
        expiry            the one shown: ?expiry=, else the nearest whose 15:30 IST close has not passed
        days_to_expiry    calendar days from the data's date
        atm_strike        the strike nearest the spot
        strikes           N each side of the ATM strike (?strikes=, 1..40, default 10)
        rows              [{strike, atm, ce: SIDE | null, pe: SIDE | null}] ascending by strike
            SIDE          {ltp, change, change_pct, oi, oi_change, volume, bid, ask, iv (% annualised),
                           iv_source "stored" | "implied" | null, delta, gamma, theta (per unit per calendar day),
                           vega (per unit per 1 vol point), itm}
        pcr               put-call ratio: total PE OI / total CE OI over every strike of the expiry
        max_pain          the strike at which option holders' expiry value (by OI) is least
        totals            {ce_oi, pe_oi, ce_oi_change, pe_oi_change, ce_volume, pe_volume} over the expiry
        max_oi            {ce, pe}: the largest OI among the rows shown (the OI bars' scale)
    A value ATIP does not have is null -- never 0, never NaN. 400: a bad symbol, an expiry that is not YYYY-MM-DD
    or not stored, a strikes count that is not a whole number.

Data: the latest option_chain_snapshot when it is from a later session than the latest F&O bhavcopy rows in
fo_contract_daily (the feed is off by default), else the bhavcopy. IV is the stored one (NSE's, or W35's
Black-Scholes IV of the bhavcopy close); with none stored it is implied from the price -- mid of bid / ask, else LTP --
with quant.derivatives.implied_vol. Greeks are quant.derivatives.greeks at that IV (r = RISK_FREE, q = 0) with T the
time from the data's own timestamp to 15:30 IST on expiry day. No new pricing maths lives here.

    register(app, guard, run=None)   `run` is w39_routes' `async def run(f)` (or a synchronous `call(f)`, which is
                                     run in a thread); with none, one is built on db.schema.get_connection.
"""

# No `from __future__ import annotations`: FastAPI reads the route's annotations at registration.
import inspect
import math
import re
from datetime import date, datetime, time, timedelta
from typing import Optional

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:                                   # pragma: no cover -- tzdata missing
    IST = None

DEFAULT_STRIKES, MAX_STRIKES = 10, 40
SESSION_OPEN, SESSION_CLOSE, BHAVCOPY_READY = time(9, 15), time(15, 30), time(18, 30)
NO_DATA = "No option chain stored for {s}: the F&O bhavcopy job fills it after 18:30 IST"
SOURCE_LABEL = {"live": "Live option-chain feed", "bhavcopy": "NSE F&O bhavcopy (end of day)"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_YEAR_S = 365.0 * 86400


def now_ist() -> datetime:
    """Naive IST wall-clock time (ATIP stores naive IST timestamps). Tests replace it."""
    if IST is None:
        return datetime.now()
    return datetime.now(IST).replace(tzinfo=None)


# ── small helpers ──────────────────────────────────────────────────────────────────────────────

def _num(v):
    """A finite float, else None (NULL, '', NaN, inf, text)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _price(v):
    f = _num(v)
    return f if f is not None and f > 0 else None


def _iv_pct(v):
    """Stored IVs are percentages (NSE's chain, W35's bhavcopy IV); a value <= 3 is a decimal."""
    f = _num(v)
    if f is None or f <= 0:
        return None
    return f * 100.0 if f <= 3 else f


def _r(v, dp):
    f = _num(v)
    return None if f is None else round(f, dp)


def _int(v):
    f = _num(v)
    return None if f is None else int(round(f))


def _day(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _stamp(v) -> datetime:
    if isinstance(v, datetime):
        return v.replace(tzinfo=None)
    if isinstance(v, date):
        return datetime.combine(v, SESSION_CLOSE)
    s = str(v).strip().replace("T", " ")
    try:
        return datetime.fromisoformat(s[:19])
    except ValueError:
        return datetime.combine(date.fromisoformat(s[:10]), time(0, 0))


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    return xs[len(xs) // 2] if xs else None


def _sym(symbol) -> str:
    s = str(symbol or "").strip().upper()
    if not s or len(s) > 32 or not all(c.isalnum() or c in "-&_" for c in s):
        raise ValueError("invalid symbol")
    try:
        from data.derivatives_store import fo_symbol
        return fo_symbol(s)
    except Exception:
        return s


def _rows(conn, sql, args=()):
    try:
        return conn.execute(sql, args).fetchall()
    except Exception:
        return []


def _one(conn, sql, args=()):
    r = _rows(conn, sql, args)
    return r[0][0] if r else None


# ── the stored chain ───────────────────────────────────────────────────────────────────────────

def _live(conn, sym):
    """(ts, {(expiry, strike, kind): quote}, [underlying]) of the latest intraday snapshot."""
    ts = _one(conn, "SELECT MAX(ts) FROM option_chain_snapshot WHERE symbol=?", (sym,))
    if not ts:
        return None, {}, []
    quotes, und = {}, []
    for r in _rows(conn, "SELECT expiry, strike, option_type, ltp, `change`, iv, oi, oi_chg, volume, bid, ask, "
                         "underlying FROM option_chain_snapshot WHERE symbol=? AND option_type IN ('CE','PE') AND "
                         "ts=(SELECT MAX(ts) FROM option_chain_snapshot WHERE symbol=?)", (sym, sym)):
        und.append(_price(r[11]))
        quotes[(str(r[0])[:10], float(r[1]), r[2])] = {
            "ltp": _price(r[3]), "change": _num(r[4]), "iv": _iv_pct(r[5]), "oi": _num(r[6]), "oi_change": _num(r[7]),
            "volume": _num(r[8]), "bid": _price(r[9]), "ask": _price(r[10])}
    return (_stamp(ts) if quotes else None), quotes, und


def _eod(conn, sym):
    """(session date, quotes, [underlying], lot size) of the latest F&O bhavcopy session."""
    d = _one(conn, "SELECT MAX(date) FROM fo_contract_daily WHERE symbol=? AND option_type IN ('CE','PE')", (sym,))
    if not d:
        return None, {}, [], None
    quotes, und, lots = {}, [], {}
    for r in _rows(conn, "SELECT expiry, strike, option_type, close, settle, prev_close, oi, oi_chg, volume, iv, "
                         "underlying, lot_size FROM fo_contract_daily WHERE symbol=? AND date=? AND "
                         "option_type IN ('CE','PE')", (sym, str(d)[:10])):
        ltp = _price(r[3]) or _price(r[4])
        prev = _price(r[5])
        und.append(_price(r[10]))
        if _int(r[11]):
            lots[_int(r[11])] = lots.get(_int(r[11]), 0) + 1
        quotes[(str(r[0])[:10], float(r[1]), r[2])] = {
            "ltp": ltp, "change": (ltp - prev) if ltp is not None and prev is not None else None,
            "iv": _iv_pct(r[9]), "oi": _num(r[6]), "oi_change": _num(r[7]), "volume": _num(r[8]),
            "bid": None, "ask": None}
    lot = max(lots.items(), key=lambda kv: kv[1])[0] if lots else None
    return _day(d), quotes, und, lot


def _lot_size(conn, sym):
    v = _one(conn, "SELECT lot_size FROM fo_contract_daily WHERE symbol=? AND lot_size>0 ORDER BY date DESC LIMIT 1",
             (sym,))
    if not _int(v):
        v = _one(conn, "SELECT lot_size FROM fo_underlying_daily WHERE symbol=? AND lot_size>0 ORDER BY date DESC "
                       "LIMIT 1", (sym,))
    return _int(v) or None


def _fallback_spot(conn, sym, on: date):
    v = _price(_one(conn, "SELECT underlying_price FROM fo_underlying_daily WHERE symbol=? AND date<=? AND "
                          "underlying_price>0 ORDER BY date DESC LIMIT 1", (sym, str(on))))
    if v:
        return v, "F&O summary"
    names = [sym]
    try:
        from data.derivatives_store import atip_symbol
        names.append(atip_symbol(sym))
    except Exception:
        pass
    for s in dict.fromkeys(names):
        v = _price(_one(conn, "SELECT close FROM prices_daily WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 1",
                        (s, str(on))))
        if v:
            return v, "last close"
    return None, None


def expected_session(source: str, now: datetime) -> date:
    """The session the data should have reached by `now` (IST)."""
    from utils.trading_calendar import is_trading_day, last_trading_day
    today = now.date()
    ready = SESSION_OPEN if source == "live" else BHAVCOPY_READY
    if is_trading_day(today) and now.time() >= ready:
        return today
    return last_trading_day(today - timedelta(days=1))


def _max_pain(oi_by_strike: dict):
    """The strike where Σ CE OI × max(0, S − K) + PE OI × max(0, K − S) -- holders' expiry value -- is least."""
    if not oi_by_strike or not sum(c + p for c, p in oi_by_strike.values()):
        return None
    ks = sorted(oi_by_strike)
    return min(ks, key=lambda x: (sum(c * max(0.0, x - k) + p * max(0.0, k - x) for k, (c, p) in oi_by_strike.items()),
                                  x))


def _side(q, kind, K, spot, T, r):
    if q is None:
        return None
    from quant.derivatives import greeks, implied_vol
    cp = "call" if kind == "CE" else "put"
    ltp, bid, ask = q["ltp"], q["bid"], q["ask"]
    px = (bid + ask) / 2 if bid and ask and ask >= bid else ltp
    iv, src = q["iv"], ("stored" if q["iv"] else None)
    if iv is None and px and spot and T > 0:
        v = implied_vol(px, spot, K, T, r, cp)
        if v:
            iv, src = v * 100.0, "implied"
    g = greeks(spot, K, T, r, iv / 100.0, cp) if iv and spot and T > 0 else {}
    ch = q["change"]
    base = (ltp - ch) if ltp is not None and ch is not None else None
    return {"ltp": _r(ltp, 2), "change": _r(ch, 2), "change_pct": _r(ch / base * 100, 2) if base and base > 0 else None,
            "oi": _int(q["oi"]), "oi_change": _int(q["oi_change"]), "volume": _int(q["volume"]),
            "bid": _r(bid, 2), "ask": _r(ask, 2), "iv": _r(iv, 2), "iv_source": src,
            "delta": _r(g.get("delta"), 4), "gamma": _r(g.get("gamma"), 6), "theta": _r(g.get("theta"), 2),
            "vega": _r(g.get("vega"), 2),
            "itm": None if not spot else (K < spot if kind == "CE" else K > spot)}


def _strikes_arg(strikes) -> int:
    if strikes in (None, ""):
        return DEFAULT_STRIKES
    try:
        n = int(str(strikes).strip())
    except ValueError:
        raise ValueError(f"strikes must be a whole number from 1 to {MAX_STRIKES}")
    return max(1, min(MAX_STRIKES, n))


def option_chain(conn, symbol, expiry=None, strikes=None, now=None) -> dict:
    """The /api/v2/options/chain payload (see the module docstring)."""
    from quant.options_strategy import RISK_FREE, strike_step
    sym = _sym(symbol)
    if expiry not in (None, "") and not _DATE_RE.match(str(expiry).strip()):
        raise ValueError("expiry must be a date as YYYY-MM-DD")
    want = None
    if expiry not in (None, ""):
        try:
            want = date.fromisoformat(str(expiry).strip())
        except ValueError:
            raise ValueError("expiry must be a date as YYYY-MM-DD")
    n = _strikes_arg(strikes)
    now = now or now_ist()

    live_ts, live, live_und = _live(conn, sym)
    eod_d, eod, eod_und, eod_lot = _eod(conn, sym)
    use_live = bool(live) and (not eod or live_ts.date() > eod_d)
    if use_live:
        source, quotes, und, as_of = "live", live, live_und, live_ts
        lot = _lot_size(conn, sym)
    elif eod:
        source, quotes, und, as_of = "bhavcopy", eod, eod_und, datetime.combine(eod_d, SESSION_CLOSE)
        lot = eod_lot or _lot_size(conn, sym)
    else:
        source, quotes = None, {}
    as_of_date = as_of.date() if quotes else None
    expiries = sorted({k[0] for k in quotes if k[0] >= str(as_of_date)}) if quotes else []
    if not expiries:
        return {"symbol": sym, "status": "NO_DATA", "message": NO_DATA.format(s=sym), "source": None,
                "source_label": None, "as_of": None, "as_of_date": None, "stale": None, "spot": None, "lot_size": None,
                "expiries": [], "expiry": None, "atm_strike": None, "strikes": n, "rows": [], "pcr": None,
                "max_pain": None, "totals": None, "max_oi": None}

    if want is not None:
        if str(want) not in expiries:
            raise ValueError(f"no {sym} options stored for expiry {want}; stored: {', '.join(expiries)}")
        exp = str(want)
    else:
        exp = next((e for e in expiries if datetime.combine(date.fromisoformat(e), SESSION_CLOSE) > now), expiries[0])

    spot, spot_source = _median(und), ("option chain" if source == "live" else "F&O bhavcopy")
    if spot is None:
        spot, spot_source = _fallback_spot(conn, sym, as_of_date)
    exp_d = date.fromisoformat(exp)
    T = max(0.0, (datetime.combine(exp_d, SESSION_CLOSE) - as_of).total_seconds()) / _YEAR_S

    ks = sorted({k[1] for k in quotes if k[0] == exp})
    atm = min(ks, key=lambda k: (abs(k - spot), k)) if spot else ks[len(ks) // 2]
    i = ks.index(atm)
    shown = ks[max(0, i - n): i + n + 1]
    rows = [{"strike": K, "atm": K == atm,
             "ce": _side(quotes.get((exp, K, "CE")), "CE", K, spot, T, RISK_FREE),
             "pe": _side(quotes.get((exp, K, "PE")), "PE", K, spot, T, RISK_FREE)} for K in shown]

    def total(kind, field):
        vals = [q[field] for (e, _, k), q in quotes.items() if e == exp and k == kind and q[field] is not None]
        return _int(sum(vals)) if vals else None

    totals = {"ce_oi": total("CE", "oi"), "pe_oi": total("PE", "oi"), "ce_oi_change": total("CE", "oi_change"),
              "pe_oi_change": total("PE", "oi_change"), "ce_volume": total("CE", "volume"),
              "pe_volume": total("PE", "volume")}
    pcr = round(totals["pe_oi"] / totals["ce_oi"], 2) if totals["ce_oi"] and totals["pe_oi"] is not None else None
    oi_by = {}
    for (e, K, k), q in quotes.items():
        if e == exp and q["oi"] is not None:
            c, p = oi_by.get(K, (0.0, 0.0))
            oi_by[K] = (c + q["oi"], p) if k == "CE" else (c, p + q["oi"])

    def top(side):
        vals = [r[side]["oi"] for r in rows if r[side] and r[side]["oi"] is not None]
        return max(vals) if vals else None

    expected = expected_session(source, now)
    stale = as_of_date < expected
    return {"symbol": sym, "status": "OK", "source": source, "source_label": SOURCE_LABEL[source],
            "as_of": as_of.strftime("%Y-%m-%d %H:%M"), "as_of_date": str(as_of_date), "timezone": "IST",
            "last_trading_day": str(expected), "stale": stale,
            "stale_reason": (f"The data is from {as_of_date}; the latest session it should have reached is {expected}."
                             if stale else None),
            "spot": _r(spot, 2), "spot_source": spot_source, "lot_size": lot,
            "strike_step": _r(strike_step(sym, spot or atm, ks), 2), "risk_free": RISK_FREE,
            "expiries": expiries, "expiry": exp, "days_to_expiry": (exp_d - as_of_date).days,
            "atm_strike": atm, "strikes": n, "rows": rows, "pcr": pcr, "max_pain": _max_pain(oi_by),
            "totals": totals, "max_oi": {"ce": top("ce"), "pe": top("pe")},
            "model": "Black-Scholes (European, q = 0) at the stored IV, else the IV implied from the price"}


# ── routes ─────────────────────────────────────────────────────────────────────────────────────

def _default_run():
    from fastapi.responses import JSONResponse
    from starlette.concurrency import run_in_threadpool

    def finite(o):
        if isinstance(o, dict):
            return {k: finite(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [finite(v) for v in o]
        if isinstance(o, float) and not math.isfinite(o):
            return None
        if isinstance(o, (date, datetime)):
            return o.isoformat()
        return o

    def call(fn):
        from db.schema import get_connection
        conn = get_connection()
        try:
            return JSONResponse(finite(fn(conn)))
        except LookupError as e:
            return JSONResponse({"error": str(e).strip("'\"")}, status_code=404)
        except (ValueError, TypeError, KeyError) as e:
            return JSONResponse({"error": str(e).strip("'\"")}, status_code=400)
        finally:
            conn.close()

    async def run(fn):
        return await run_in_threadpool(call, fn)
    return run


def _as_async(run):
    if run is None:
        return _default_run()
    if inspect.iscoroutinefunction(run):
        return run
    from starlette.concurrency import run_in_threadpool

    async def arun(fn):
        return await run_in_threadpool(run, fn)
    return arun


def register(app, guard, run=None) -> None:
    """GET /api/v2/options/chain/{symbol}. `guard` (the POST token dependency) is unused: the route only reads."""
    run = _as_async(run)

    @app.get("/api/v2/options/chain/{symbol}")
    async def api_v2_options_chain(symbol: str, expiry: Optional[str] = None, strikes: Optional[str] = None):
        return await run(lambda conn: option_chain(conn, symbol, expiry=expiry, strikes=strikes))
