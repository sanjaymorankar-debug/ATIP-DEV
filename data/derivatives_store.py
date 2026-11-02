"""
Derivatives data store (W35: DP-08 -- completes the W27 partial).

W27 (data/derivatives.py) keeps one SUMMARY row per underlying per session (OI, PCR, max pain,
ATM IV). The contracts themselves were thrown away. Now:

    store_contracts(conn, df, d)   from the same downloaded NSE F&O bhavcopy (UDiFF):
        lake "fo_bhavcopy"         every contract, every strike, every expiry (DP-22; research)
        fo_contract_daily          futures of all expiries + options of the NEAREST_EXPIRIES
                                   expiries per underlying, every strike, with OHLC / settle / OI /
                                   OI change / volume / value / underlying / lot size and an implied
                                   vol (Black-Scholes, same method and rate as W30's ATM IV; None
                                   when the price is outside no-arbitrage bounds -- never made up)
        called by run_fo_pipeline after it stores the summary (one download, two uses)

    snapshot_option_chain(symbol)  NSE option-chain API (indices: NIFTY / BANKNIFTY / FINNIFTY /
                                   MIDCPNIFTY; stocks: any F&O symbol) -> option_chain_snapshot,
                                   every strike of every listed expiry: LTP, change, IV (NSE's),
                                   OI, OI change, volume, best bid / ask and quantities, underlying.
                                   Intraday, every derivatives.chain_minutes during the session
                                   when derivatives.option_chain_enabled (off by default);
                                   also written to lake "option_chain"

    chain(conn, symbol, ts=None)   the latest (or a given) snapshot as rows
    contracts(conn, symbol, d)     the session's stored contracts
    chain_on(conn, symbol, as_of)  (W40) the chain as stored for one session, point in time: the
                                   EOD contracts of the latest session <= as_of, overlaid by that
                                   day's latest intraday snapshot -- what option-overlay strategies
                                   select strikes from and the paper options book fills / marks at
    fo_symbol(symbol)              ATIP symbol -> NSE F&O underlying (NIFTY50 -> NIFTY ...)

Config (config.json "derivatives"): {"option_chain_enabled": false, "chain_symbols": ["NIFTY",
"BANKNIFTY"], "chain_minutes": 15, "nearest_expiries": 2, "chain_source": "dhan"}

2026-10-31 (DP-08), the chain from Dhan:
    chain_source "dhan"   Dhan's option chain API (Data API plan) for the index underlyings it knows
                          (DHAN_UNDERLYINGS: NIFTY 13, BANKNIFTY 25, FINNIFTY 27, MIDCPNIFTY 442,
                          NIFTYNXT50 38 -- Dhan's security list): the nearest `nearest_expiries` expiries,
                          LTP, change and OI change against Dhan's previous close / OI, IV, OI, volume,
                          best bid / ask and quantities, the underlying's LTP. Dhan allows one call every
                          3 seconds (data/dhan.py paces them); the expiry list is fetched once a day, so a
                          tick for NIFTY and BANKNIFTY is 4 calls, about 12 seconds. A Dhan failure, or an
                          underlying Dhan is not mapped for, falls back to the NSE chain for that symbol.
    chain_source "nse"    the NSE option-chain API only (as before)
    schedule              checked every minute in the session (pipeline/scheduler.py); a snapshot is taken
                          when the newest one is chain_minutes old (chain_due)
    status                live_feed_status row 'option_chain': OFF when switched off, IDLE outside the
                          window, LIVE / PARTIAL / ERROR with the rows and source of the last tick
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger(__name__)

NEAREST_EXPIRIES = 2
INDEX_UNDERLYINGS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
CHAIN_URL_INDEX = "https://www.nseindia.com/api/option-chain-indices?symbol={s}"
CHAIN_URL_EQUITY = "https://www.nseindia.com/api/option-chain-equities?symbol={s}"
DEFAULTS = {"option_chain_enabled": False, "chain_symbols": ["NIFTY", "BANKNIFTY"], "chain_minutes": 15,
            "nearest_expiries": NEAREST_EXPIRIES, "chain_source": "dhan"}
DHAN_UNDERLYINGS = {"NIFTY": 13, "BANKNIFTY": 25, "FINNIFTY": 27, "MIDCPNIFTY": 442, "NIFTYNXT50": 38}
DHAN_INDEX_SEGMENT = "IDX_I"
CHAIN_COLUMNS = ["ts", "symbol", "expiry", "strike", "option_type", "ltp", "change", "iv", "oi", "oi_chg", "volume",
                 "bid", "ask", "bid_qty", "ask_qty", "underlying"]
_expiries = {}          # (underlying, date) -> Dhan's expiry list, fetched once a day


def settings() -> dict:
    try:
        cfg = json.loads((Path("atip_data") / "config.json").read_text(encoding="utf-8"))
        return {**DEFAULTS, **(cfg.get("derivatives") or {})}
    except Exception:
        return dict(DEFAULTS)


def _f(v):
    try:
        x = float(v)
        return None if x != x else x
    except (TypeError, ValueError):
        return None


def store_contracts(conn, df, d) -> dict:
    """Both stores from one bhavcopy DataFrame. Returns counts."""
    from data import lake
    from data.derivatives import RISK_FREE, _opt_price
    from quant.derivatives import implied_vol
    if df is None or df.empty:
        return {"lake": 0, "contracts": 0}
    td = d if isinstance(d, date) else date.fromisoformat(str(d)[:10])
    lw = lake.write("fo_bhavcopy", td, df, knowledge_time=f"{td} 18:00:00", source="nse_fo_udiff", replace=True,
                    conn=conn)
    df = df.copy()
    df["XpryDt"] = df["XpryDt"].astype(str).str[:10]
    keep_n = int(settings().get("nearest_expiries") or NEAREST_EXPIRIES)
    n = 0
    for sym, g in df.groupby("TckrSymb"):
        opt_exp = sorted(e for e in g[g["FinInstrmTp"].isin(["IDO", "STO"])]["XpryDt"].unique() if e and e != "nan")
        keep = set(opt_exp[:keep_n])
        for _, r in g.iterrows():
            it = r.get("FinInstrmTp")
            is_opt = it in ("IDO", "STO")
            if is_opt and r["XpryDt"] not in keep:
                continue
            spot, strike = _f(r.get("UndrlygPric")), _f(r.get("StrkPric")) or 0.0
            iv = None
            if is_opt and spot and strike:
                dte = (date.fromisoformat(r["XpryDt"]) - td).days
                px = _opt_price(r)
                if px and dte > 0:
                    v = implied_vol(px, spot, strike, dte / 365.0, RISK_FREE,
                                    "call" if r.get("OptnTp") == "CE" else "put")
                    iv = round(v * 100, 3) if v else None
            conn.execute(
                "INSERT OR REPLACE INTO fo_contract_daily (date,symbol,instrument,expiry,strike,option_type,open,high,low,"
                "close,settle,prev_close,oi,oi_chg,volume,value,underlying,lot_size,iv) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(td), sym, it, r["XpryDt"], strike if is_opt else 0.0, r.get("OptnTp") if is_opt else "XX",
                 _f(r.get("OpnPric")), _f(r.get("HghPric")), _f(r.get("LwPric")), _f(r.get("ClsPric")),
                 _f(r.get("SttlmPric")), _f(r.get("PrvsClsgPric")), _f(r.get("OpnIntrst")), _f(r.get("ChngInOpnIntrst")),
                 _f(r.get("TtlTradgVol")), _f(r.get("TtlTrfVal")), spot,
                 int(_f(r.get("NewBrdLotQty")) or 0) or None, iv))
            n += 1
    conn.commit()
    return {"lake": lw.get("rows", 0), "contracts": n}


def _nse_rows(sym: str, nse, ts: str) -> list:
    url = (CHAIN_URL_INDEX if sym in INDEX_UNDERLYINGS else CHAIN_URL_EQUITY).format(s=sym)
    data = nse.json(url)
    rows = []
    for rec in ((data or {}).get("records") or {}).get("data") or []:
        for ot in ("CE", "PE"):
            o = rec.get(ot)
            if not isinstance(o, dict):
                continue
            try:
                exp = datetime.strptime(str(o.get("expiryDate") or rec.get("expiryDate")), "%d-%b-%Y").date()
            except ValueError:
                continue
            row = (ts, sym, str(exp), _f(o.get("strikePrice") or rec.get("strikePrice")), ot, _f(o.get("lastPrice")),
                   _f(o.get("change")), _f(o.get("impliedVolatility")), _f(o.get("openInterest")),
                   _f(o.get("changeinOpenInterest")), _f(o.get("totalTradedVolume")), _f(o.get("bidprice")),
                   _f(o.get("askPrice")), _f(o.get("bidQty")), _f(o.get("askQty")), _f(o.get("underlyingValue")))
            if row[3] is not None:
                rows.append(row)
    return rows


def _dhan_body(resp, what: str):
    """The payload of a dhanhq reply ({'status', 'remarks', 'data': Dhan's JSON body}); raises on a refusal."""
    if not isinstance(resp, dict) or resp.get("status") != "success":
        from data.dhan_ws import safe
        raise RuntimeError(f"Dhan {what} refused: {safe(json.dumps((resp or {}).get('remarks'), default=str))[:160]}")
    body = resp.get("data")
    if isinstance(body, dict) and "data" in body:
        body = body["data"]
    return body


def dhan_expiries(sym: str, dhan, day: date | None = None) -> list:
    """Dhan's expiry list for an index underlying, from `day` on; asked once a day."""
    day = day or date.today()
    key = (sym, str(day))
    if key not in _expiries:
        from data.dhan import _guarded
        body = _dhan_body(_guarded(dhan.expiry_list, DHAN_UNDERLYINGS[sym], DHAN_INDEX_SEGMENT), "expiry list")
        _expiries[key] = sorted(str(e)[:10] for e in (body or []) if str(e)[:10] >= str(day))
    return _expiries[key]


def _dhan_rows(sym: str, dhan, ts: str, n_expiries: int) -> list:
    """Dhan's option chain for the nearest n expiries, as option_chain_snapshot rows."""
    from data.dhan import _guarded
    rows = []
    for exp in dhan_expiries(sym, dhan)[: max(1, int(n_expiries))]:
        body = _dhan_body(_guarded(dhan.option_chain, DHAN_UNDERLYINGS[sym], DHAN_INDEX_SEGMENT, exp), "option chain")
        spot = _f((body or {}).get("last_price"))
        for strike, legs in ((body or {}).get("oc") or {}).items():
            k = _f(strike)
            if k is None or not isinstance(legs, dict):
                continue
            for ot in ("CE", "PE"):
                o = legs.get(ot.lower())
                if not isinstance(o, dict):
                    continue
                ltp, pc = _f(o.get("last_price")), _f(o.get("previous_close_price"))
                oi, poi = _f(o.get("oi")), _f(o.get("previous_oi"))
                rows.append((ts, sym, exp, k, ot, ltp, round(ltp - pc, 2) if ltp is not None and pc else None,
                             _f(o.get("implied_volatility")), oi, oi - poi if oi is not None and poi is not None else None,
                             _f(o.get("volume")), _f(o.get("top_bid_price")), _f(o.get("top_ask_price")),
                             _f(o.get("top_bid_quantity")), _f(o.get("top_ask_quantity")), spot))
    return rows


def _store_chain(conn, rows: list, ts: str, source: str = "nse"):
    for row in rows:
        conn.execute("INSERT OR REPLACE INTO option_chain_snapshot (ts,symbol,expiry,strike,option_type,ltp,`change`,"
                     "iv,oi,oi_chg,volume,bid,ask,bid_qty,ask_qty,underlying) VALUES "
                     "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
    conn.commit()
    if rows:
        import pandas as pd
        from data import lake
        lake.write("option_chain", ts[:10], pd.DataFrame(rows, columns=CHAIN_COLUMNS), knowledge_time=ts,
                   source=f"{source}_option_chain", conn=conn)


def snapshot_option_chain(symbol: str, conn=None, nse=None, source=None, dhan=None) -> dict:
    """One snapshot of one underlying's chain. source "dhan" (config's chain_source, unless an NSE client is
    passed) tries Dhan first and falls back to NSE when Dhan fails or returns nothing; "nse" asks NSE only."""
    from db.schema import get_connection
    sym = symbol.upper()
    src = (source or ("nse" if nse is not None else settings().get("chain_source") or "dhan")).lower()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows, used, note = [], "nse", None
    if src == "dhan" and sym in DHAN_UNDERLYINGS:
        try:
            if dhan is None:
                from data.dhan import get_dhan_client
                dhan, _ = get_dhan_client()
            rows = _dhan_rows(sym, dhan, ts, int(settings().get("nearest_expiries") or NEAREST_EXPIRIES))
            used = "dhan"
        except Exception as e:
            from data.dhan_ws import safe
            note = f"Dhan chain failed ({safe(e)[:120]}); NSE used"
            log.warning(f"  {sym} option chain: {note}")
    if not rows:
        from data.nse_api import client
        rows, used = _nse_rows(sym, nse or client(), ts), "nse"
    if not rows:
        return {"symbol": sym, "status": "EMPTY", "rows": 0, "source": used, "note": note}
    own = conn is None
    conn = conn or get_connection()
    try:
        _store_chain(conn, rows, ts, used)
        return {"symbol": sym, "status": "SUCCESS", "rows": len(rows), "ts": ts, "source": used, "note": note}
    finally:
        if own:
            conn.close()


def chain_due(conn=None, now=None, cfg=None) -> bool:
    """A snapshot is due when today's newest one is at least chain_minutes old (30 s of slack for the scheduler)."""
    from db.schema import get_connection
    cfg = cfg or settings()
    now = now or datetime.now()
    own = conn is None
    conn = conn or get_connection()
    try:
        last = conn.execute("SELECT MAX(ts) FROM option_chain_snapshot WHERE ts>=?",
                            (now.strftime("%Y-%m-%d 00:00:00"),)).fetchone()[0]
    except Exception:
        last = None
    finally:
        if own:
            conn.close()
    if not last:
        return True
    age = (now - datetime.fromisoformat(str(last)[:19])).total_seconds()
    return age >= float(cfg.get("chain_minutes") or 15) * 60 - 30


def report_state():
    """At start / the 06:00 resume: OFF when switched off, IDLE when on and outside the window, so the health checks
    show what the feed is doing and never 'not started' for a feed that is simply waiting for the session."""
    from data.dhan_ws import feed_window_open, record_feed_status
    s = settings()
    if not s.get("option_chain_enabled"):
        record_feed_status("option_chain", "OFF", detail="off: set config.json derivatives.option_chain_enabled")
    elif not feed_window_open():
        record_feed_status("option_chain", "IDLE", len(s["chain_symbols"]),
                           detail=f"every {s['chain_minutes']} min in the session ({s.get('chain_source')})")


def run_chain_tick() -> dict:
    from data.dhan_ws import feed_window_open, record_feed_status, safe
    s = settings()
    if not s["option_chain_enabled"]:
        report_state()
        return {"status": "SKIPPED", "rows": 0}
    if not feed_window_open():
        record_feed_status("option_chain", "IDLE", len(s["chain_symbols"]),
                           detail="outside 09:00-15:45 on a trading day")
        return {"status": "SKIPPED", "rows": 0}
    out = []
    for x in s["chain_symbols"]:
        try:
            out.append(snapshot_option_chain(x, source=s.get("chain_source")))
        except Exception as e:
            out.append({"symbol": str(x).upper(), "status": "FAILED", "rows": 0, "error": safe(e)[:160]})
    good = [o for o in out if o.get("rows")]
    mode = "LIVE" if len(good) == len(out) else "PARTIAL" if good else "ERROR"
    record_feed_status("option_chain", mode, len(out), sum(o["rows"] for o in out),
                       max((o["ts"] for o in good), default=None), datetime.now(),
                       "; ".join(f"{o['symbol']} {o['rows']} rows" + (f" ({o['source']})" if o.get("source") else "")
                                 + (f": {o.get('error') or o.get('note')}" if o.get("error") or o.get("note") else "")
                                 for o in out))
    return {"status": "SUCCESS" if good else "FAILED", "rows": sum(o["rows"] for o in out), "symbols": out}


def chain(conn, symbol: str, ts=None, expiry=None) -> dict:
    sym = symbol.upper()
    ts = ts or conn.execute("SELECT MAX(ts) FROM option_chain_snapshot WHERE symbol=?", (sym,)).fetchone()[0]
    if not ts:
        return {"symbol": sym, "ts": None, "rows": []}
    sql = "SELECT * FROM option_chain_snapshot WHERE symbol=? AND ts=?"
    args = [sym, ts]
    if expiry:
        sql += " AND expiry=?"
        args.append(expiry)
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY expiry, strike, option_type", args)]
    return {"symbol": sym, "ts": ts, "expiries": sorted({r["expiry"] for r in rows}), "rows": rows}


def contracts(conn, symbol: str, d=None, expiry=None) -> list:
    sym = symbol.upper()
    d = d or conn.execute("SELECT MAX(date) FROM fo_contract_daily WHERE symbol=?", (sym,)).fetchone()[0]
    sql = "SELECT * FROM fo_contract_daily WHERE symbol=? AND date=?"
    args = [sym, str(d)[:10] if d else None]
    if expiry:
        sql += " AND expiry=?"
        args.append(expiry)
    return [dict(r) for r in conn.execute(sql + " ORDER BY instrument, expiry, strike, option_type", args)]


# ── W40 (ENT-15): the chain on a date, for option-overlay strategies and their paper fills ─────────
FO_ALIAS = {"NIFTY50": "NIFTY", "NIFTYBANK": "BANKNIFTY"}      # ATIP index symbol -> NSE F&O underlying
ATIP_ALIAS = {v: k for k, v in FO_ALIAS.items()}


def fo_symbol(symbol: str) -> str:
    s = str(symbol or "").upper()
    return FO_ALIAS.get(s, s)


def atip_symbol(underlying: str) -> str:
    u = str(underlying or "").upper()
    return ATIP_ALIAS.get(u, u)


def _iv(v):
    """Stored IVs are percentages (NSE's chain; W35's bhavcopy IV): as a decimal."""
    v = _f(v)
    if v is None or v <= 0:
        return None
    return v / 100.0 if v > 3 else v


def chain_on(conn, symbol: str, as_of=None, fresh_minutes: int | None = None) -> dict:
    """
    The option chain ATIP stored for one session, point in time (W40).

    EOD      fo_contract_daily rows of the latest session <= as_of (default today): close, else
             settle; IV (decimal), OI, volume, lot size, the underlying's close that day.
    INTRADAY the latest option_chain_snapshot taken ON the as_of date overrides each contract's
             quote: mid of bid / ask when both are quoted (ask >= bid > 0), else LTP. With
             `fresh_minutes` (fills) the snapshot is used only when it is at most that old.
    Returns {symbol, session, snapshot_ts, source, spot, lot_size, expiries,
             quotes {(expiry, strike, CE|PE): {price, bid, ask, ltp, close, iv, oi, volume, lot_size, source}}}.
    Nothing is invented: a contract ATIP has no price for is absent.
    """
    from datetime import timedelta
    u = fo_symbol(symbol)
    d = (as_of if isinstance(as_of, date) else date.fromisoformat(str(as_of)[:10])) if as_of else date.today()
    out = {"symbol": u, "session": None, "snapshot_ts": None, "source": None, "spot": None, "lot_size": None,
           "expiries": [], "quotes": {}}
    try:
        sess = conn.execute("SELECT MAX(date) FROM fo_contract_daily WHERE symbol=? AND date<=? AND "
                            "option_type IN ('CE','PE')", (u, str(d))).fetchone()[0]
    except Exception:
        sess = None
    lots = {}
    if sess:
        out["session"] = str(sess)[:10]
        out["source"] = f"F&O bhavcopy {out['session']}"
        for r in conn.execute("SELECT expiry, strike, option_type, close, settle, iv, oi, volume, lot_size, underlying "
                              "FROM fo_contract_daily WHERE symbol=? AND date=? AND option_type IN ('CE','PE') "
                              "AND expiry>=?", (u, sess, str(d))):
            px = _f(r[3]) or _f(r[4])
            if r[9] and not out["spot"]:
                out["spot"] = float(r[9])
            if r[8]:
                lots[int(r[8])] = lots.get(int(r[8]), 0) + 1
            if not px or px <= 0:
                continue
            out["quotes"][(str(r[0])[:10], float(r[1]), r[2])] = {
                "price": round(px, 2), "bid": None, "ask": None, "ltp": None, "close": px, "iv": _iv(r[5]),
                "oi": _f(r[6]) or 0.0, "volume": _f(r[7]) or 0.0, "lot_size": int(r[8]) if r[8] else None,
                "source": out["source"]}
    try:
        ts = conn.execute("SELECT MAX(ts) FROM option_chain_snapshot WHERE symbol=? AND ts>=? AND ts<=?",
                          (u, f"{d} 00:00:00", f"{d} 23:59:59")).fetchone()[0]
    except Exception:
        ts = None
    if ts and fresh_minutes is not None:
        cutoff = (datetime.now() - timedelta(minutes=int(fresh_minutes))).strftime("%Y-%m-%d %H:%M:%S")
        if str(ts)[:19] < cutoff:
            ts = None
    if ts:
        out["snapshot_ts"] = str(ts)[:19]
        src = f"option chain {str(ts)[:16]}"
        for r in conn.execute("SELECT expiry, strike, option_type, ltp, bid, ask, iv, oi, volume, underlying FROM "
                              "option_chain_snapshot WHERE symbol=? AND ts=? AND expiry>=?", (u, ts, str(d))):
            ltp, bid, ask = _f(r[3]), _f(r[4]), _f(r[5])
            px = (bid + ask) / 2 if bid and ask and ask >= bid > 0 else ltp
            if r[9]:
                out["spot"] = float(r[9])
            if not px or px <= 0:
                continue
            key = (str(r[0])[:10], float(r[1]), r[2])
            prev = out["quotes"].get(key) or {}
            out["quotes"][key] = {"price": round(px, 2), "bid": bid, "ask": ask, "ltp": ltp, "close": prev.get("close"),
                                  "iv": _iv(r[6]) or prev.get("iv"), "oi": _f(r[7]) or prev.get("oi") or 0.0,
                                  "volume": _f(r[8]) or prev.get("volume") or 0.0, "lot_size": prev.get("lot_size"),
                                  "source": src}
        out["source"] = src + (f" over {out['source']}" if out["source"] else "")
    if lots:
        out["lot_size"] = max(lots.items(), key=lambda kv: kv[1])[0]
    else:
        try:
            r = conn.execute("SELECT lot_size FROM fo_underlying_daily WHERE symbol=? AND date<=? AND lot_size>0 "
                             "ORDER BY date DESC LIMIT 1", (u, str(d))).fetchone()
            out["lot_size"] = int(r[0]) if r and r[0] else None
        except Exception:
            pass
    if not out["spot"]:
        try:
            r = conn.execute("SELECT underlying_price FROM fo_underlying_daily WHERE symbol=? AND date<=? AND "
                             "underlying_price>0 ORDER BY date DESC LIMIT 1", (u, str(d))).fetchone()
            out["spot"] = float(r[0]) if r and r[0] else None
        except Exception:
            pass
    out["expiries"] = sorted({k[0] for k in out["quotes"]})
    return out
