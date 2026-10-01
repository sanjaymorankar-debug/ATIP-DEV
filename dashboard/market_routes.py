"""
W27 market-data API and the /market page (DB-04 pre-open view; DP-01 / DP-08 / DP-12 /
DP-15 / DP-16 / AD-03 read surfaces). JSON; 400 invalid; 404 unknown.
Authz: GET under the default dashboard:read rule; POST (refresh jobs) needs
system:operate (the catch-all rule) and the dashboard token.

    GET  /api/market/preopen                    GIFT Nifty now + implied gap vs Nifty's last close,
                                                GIFT daily series, overnight global incl. US yields,
                                                last NIFTY / BANKNIFTY PCR and max pain, last FII/DII
    GET  /api/market/global-history             ?series=us_10y&days=365
    GET  /api/market/derivatives                ?date=&kind=INDEX|STOCK&limit=50  (by total OI)
    GET  /api/market/fundamentals/{symbol}      quarters (newest first) + filings stored
    GET  /api/market/ownership/{symbol}         shareholding history, insider trades, SAST, features
    GET  /api/market/deal-signal                latest AD-03 event study
    GET  /api/market/feed-status                DP-01 stock feed (and index feed) status
    POST /api/market/refresh/{job}              (token) job = fundamentals | institutional | fo |
                                                global_history | deal_signal; body {symbols?, period?}
                                                runs in the background; see pipeline_log
    GET  /market                                the page
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
import json
import re
import threading

from fastapi.responses import HTMLResponse, JSONResponse

SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9&\-_.]{0,29}$")
JOBS = ("fundamentals", "institutional", "fo", "global_history", "deal_signal")


def _rows(conn, sql, args=()):
    cur = conn.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def preopen(conn) -> dict:
    gift = conn.execute("SELECT date, time, gift_nifty, gift_nifty_chg, nifty50, nifty50_chg FROM index_levels "
                        "WHERE gift_nifty>0 ORDER BY date DESC, time DESC LIMIT 1").fetchone()
    nifty = conn.execute("SELECT date, close FROM prices_daily WHERE symbol='NIFTY50' AND close>0 "
                         "ORDER BY date DESC LIMIT 1").fetchone()
    out = {"gift": dict(gift) if gift else None, "nifty_last_close": dict(nifty) if nifty else None,
           "implied_gap_pct": None}
    if gift and nifty and gift["gift_nifty"] and nifty["close"]:
        out["implied_gap_pct"] = round((gift["gift_nifty"] / nifty["close"] - 1) * 100, 2)
        out["gap_note"] = ("GIFT Nifty trades at a futures basis to spot; the implied gap is indicative, "
                           "not a forecast of the open.")
    out["gift_series"] = _rows(conn, "SELECT date, close FROM prices_daily WHERE symbol='GIFTNIFTY' "
                                     "ORDER BY date DESC LIMIT 30")[::-1]
    g = conn.execute("SELECT * FROM global_markets ORDER BY date DESC, CASE time WHEN 'premarket' THEN 0 "
                     "ELSE 1 END LIMIT 1").fetchone()
    out["global"] = dict(g) if g else None
    out["derivatives"] = _rows(conn, "SELECT date, symbol, underlying_price, fut_close, pcr_oi, pcr_volume, "
                                     "max_pain, near_expiry, call_oi, put_oi FROM fo_underlying_daily WHERE "
                                     "symbol IN ('NIFTY','BANKNIFTY') AND date=(SELECT MAX(date) FROM "
                                     "fo_underlying_daily)")
    f = conn.execute("SELECT date, fii_net_cr, dii_net_cr, fii_5d_avg, dii_5d_avg FROM fii_dii_market "
                     "WHERE fii_net_cr IS NOT NULL ORDER BY date DESC LIMIT 1").fetchone()
    out["fii_dii"] = dict(f) if f else None
    return out


def _run_job(job, b):
    syms = b.get("symbols")
    if syms is not None:
        if not isinstance(syms, list) or not all(isinstance(s, str) and SYMBOL_RE.match(s.upper()) for s in syms):
            raise ValueError("symbols must be a list of NSE symbols")
        syms = [s.upper() for s in syms][:600]
    if job == "fundamentals":
        from data.nse_filings import run_fundamentals_pipeline
        return lambda: run_fundamentals_pipeline(syms)
    if job == "institutional":
        from data.institutional import run_institutional_pipeline
        return lambda: run_institutional_pipeline(syms)
    if job == "fo":
        from data.derivatives import run_fo_pipeline
        n = int(b.get("sessions") or 5)
        if not 1 <= n <= 250:
            raise ValueError("sessions must be 1..250")
        return lambda: run_fo_pipeline(None, n)
    if job == "global_history":
        from data.markets import backfill_global_history
        period = str(b.get("period") or "5y")
        if period not in ("1y", "2y", "5y", "10y", "max"):
            raise ValueError("period must be 1y / 2y / 5y / 10y / max")
        return lambda: backfill_global_history(period)
    if job == "deal_signal":
        from quant.deal_signal import run_scheduled
        return run_scheduled
    raise LookupError(f"unknown job {job!r}; one of {', '.join(JOBS)}")


def register(app, guard, Req, get_connection, json_safe):
    def ok(fn):
        conn = get_connection()
        try:
            return JSONResponse(json_safe(fn(conn)))
        except LookupError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except (ValueError, TypeError, KeyError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        finally:
            conn.close()

    def sym_ok(symbol):
        s = str(symbol or "").upper()
        if not SYMBOL_RE.match(s):
            raise ValueError("invalid symbol")
        return s

    @app.get("/api/market/preopen")
    async def api_preopen():
        return ok(preopen)

    @app.get("/api/market/global-history")
    async def api_global_history(series: str = "us_10y", days: int = 365):
        from data.markets import GLOBAL_TICKERS, global_history

        def f(conn):
            if series not in GLOBAL_TICKERS:
                raise LookupError(f"unknown series; one of {', '.join(GLOBAL_TICKERS)}")
            return {"series": series, "points": global_history(conn, series, max(1, min(int(days), 5000)))}
        return ok(f)

    @app.get("/api/market/derivatives")
    async def api_derivatives(date: str = None, kind: str = None, limit: int = 50):
        def f(conn):
            d = date or conn.execute("SELECT MAX(date) FROM fo_underlying_daily").fetchone()[0]
            if d is None:
                return {"date": None, "rows": [], "note": "no F&O data yet (POST /api/market/refresh/fo)"}
            sql, args = "SELECT * FROM fo_underlying_daily WHERE date=?", [str(d)[:10]]
            if kind:
                if kind.upper() not in ("INDEX", "STOCK"):
                    raise ValueError("kind must be INDEX or STOCK")
                sql += " AND kind=?"
                args.append(kind.upper())
            sql += " ORDER BY COALESCE(call_oi,0)+COALESCE(put_oi,0) DESC LIMIT ?"
            args.append(max(1, min(int(limit), 500)))
            return {"date": str(d)[:10], "rows": _rows(conn, sql, args)}
        return ok(f)

    @app.get("/api/market/fundamentals/{symbol}")
    async def api_fundamentals(symbol: str):
        def f(conn):
            s = sym_ok(symbol)
            rows = _rows(conn, "SELECT * FROM fundamental_data WHERE symbol=? ORDER BY COALESCE(period_end, "
                               "report_date) DESC LIMIT 12", (s,))
            fil = conn.execute("SELECT status, COUNT(*) FROM fundamental_filing WHERE symbol=? GROUP BY status",
                               (s,)).fetchall()
            for r in rows:
                if r.get("score_inputs"):
                    try:
                        r["score_inputs"] = json.loads(r["score_inputs"])
                    except ValueError:
                        pass
            return {"symbol": s, "quarters": rows, "filings": {r[0]: r[1] for r in fil}}
        return ok(f)

    @app.get("/api/market/ownership/{symbol}")
    async def api_ownership(symbol: str):
        def f(conn):
            from data.institutional import features
            s = sym_ok(symbol)
            return {"symbol": s, "features": features(conn, s),
                    "shareholding": _rows(conn, "SELECT * FROM shareholding_pattern WHERE symbol=? ORDER BY as_of "
                                                "DESC LIMIT 12", (s,)),
                    "insider": _rows(conn, "SELECT * FROM insider_trade WHERE symbol=? ORDER BY disclosed_at DESC "
                                           "LIMIT 50", (s,)),
                    "sast": _rows(conn, "SELECT * FROM sast_disclosure WHERE symbol=? ORDER BY disclosed_at DESC "
                                        "LIMIT 50", (s,))}
        return ok(f)

    @app.get("/api/market/deal-signal")
    async def api_deal_signal():
        def f(conn):
            from quant.deal_signal import latest
            r = latest(conn)
            return r or {"note": "no study yet (POST /api/market/refresh/deal_signal)"}
        return ok(f)

    @app.get("/api/market/feed-status")
    async def api_feed_status():
        def f(conn):
            from data.stock_feed import feed_status
            idx = None
            try:
                from data.dhan_ws import get_index_feed_manager
                m = get_index_feed_manager()
                idx = {"running": m is not None, "indexes_ticking": len(m.snapshot()) if m else 0}
            except Exception as e:
                idx = {"error": str(e)}
            return {"stocks": feed_status(conn), "indexes": idx}
        return ok(f)

    @app.post("/api/market/refresh/{job}", dependencies=guard)
    async def api_refresh(job: str, request: Req):
        try:
            b = await request.json()
        except Exception:
            b = {}
        b = b if isinstance(b, dict) else {}
        try:
            fn = _run_job(job, b)
        except LookupError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except (ValueError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        threading.Thread(target=fn, daemon=True, name=f"market-refresh-{job}").start()
        return JSONResponse({"started": job, "note": "running in the background; see pipeline_log / logs"},
                            status_code=202)

    @app.get("/market", response_class=HTMLResponse)
    async def market_page():
        from dashboard.market_page import render
        from dashboard.security import token as _tok
        return HTMLResponse(render(_tok()))
