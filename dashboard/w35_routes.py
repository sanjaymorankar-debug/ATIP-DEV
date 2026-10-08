"""
W35 data-platform API + the /data-platform page. JSON; 400 invalid; 404 unknown; token on state changes.
Authz: GET /api/data -> dashboard:read; POST /api/data -> research:run (enterprise/authz.py).

    GET  /data-platform                              the page (dashboard/w35_page.py)
    GET  /api/data/lake                              datasets, partitions, rows, bytes, write format   DP-22
    POST /api/data/lake/verify                       re-hash every file against the manifest
    POST /api/data/lake/archive                      {table, date_col, start?, end?, knowledge_col?}
    GET  /api/data/ticks/status                      ?days=10  tick capture per day                    DP-04
    GET  /api/data/ticks                             ?day&symbol&limit=2000
    POST /api/data/ticks/minute-bars                 {day?}  rebuild 1-min bars from ticks
    GET  /api/data/depth                             ?symbol&limit=200  latest order-book snapshots     DP-05
    GET  /api/data/depth/features                    ?symbol&day
    POST /api/data/depth/snapshot                    {symbols?}
    GET  /api/data/fo/contracts                      ?symbol&date&expiry                               DP-08
    GET  /api/data/fo/chain                          ?symbol&expiry&ts
    POST /api/data/fo/chain/snapshot                 {symbol}
    GET  /api/data/fo/surface                        ?symbol&date  W39 (AF-10): IV surface, term structure, skew,
                                                     per-strike Greeks (quant.derivatives.vol_surface); 404 no data
    GET  /api/data/macro                             latest point-in-time snapshot + calendar           DP-14
    GET  /api/data/macro/{series_id}                 ?as_of
    POST /api/data/macro/refresh
    POST /api/data/macro/calendar                    {event_date, event, importance?, series_id?, note?}
    GET  /api/data/assets                            catalogue (assets + MF)                           DP-21
    GET  /api/data/assets/{asset_class}/{symbol}     ?start&end
    GET  /api/data/mf                                ?q=&limit=50  latest NAVs, name search
    POST /api/data/assets/refresh
    GET  /api/data/alt                               sources, health                                   AD-01/02
    GET  /api/data/alt/{source_id}/{metric}          ?entity&start&end&as_of
    POST /api/data/alt/{source_id}/run               {as_of?}
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
from datetime import date, timedelta

from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool


def register(app, guard, Req, get_connection, json_safe):
    def call(fn):
        conn = get_connection()
        try:
            return JSONResponse(json_safe(fn(conn)))
        except LookupError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except (ValueError, TypeError, KeyError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except RuntimeError as e:
            return JSONResponse({"error": str(e)}, status_code=422)
        finally:
            conn.close()

    async def run(fn):
        return await run_in_threadpool(call, fn)

    async def body(request):
        try:
            b = await request.json()
        except Exception:
            return {}
        return b if isinstance(b, dict) else {}

    def _n(v, lo, hi, name):
        v = int(v)
        if not lo <= v <= hi:
            raise ValueError(f"{name} must be {lo}..{hi}")
        return v

    def rows(c, sql, *a):
        return [dict(r) for r in c.execute(sql, a)]

    @app.get("/data-platform", response_class=HTMLResponse)
    async def data_platform():
        from dashboard.w35_page import render
        from dashboard.security import token
        return HTMLResponse(render(token()))

    # ── DP-22 lake ──
    @app.get("/api/data/lake")
    async def lake_stats():
        from data.lake import stats
        return await run(lambda c: stats(c))

    @app.post("/api/data/lake/verify", dependencies=guard)
    async def lake_verify():
        from data.lake import verify
        return await run(lambda c: verify(conn=c))

    @app.post("/api/data/lake/archive", dependencies=guard)
    async def lake_archive(request: Req):
        b = await body(request)
        from data.lake import archive_table
        if not b.get("table") or not b.get("date_col"):
            return JSONResponse({"error": "table and date_col are required"}, status_code=400)
        return await run(lambda c: archive_table(str(b["table"]), str(b["date_col"]), b.get("dataset"), b.get("start"),
                                                 b.get("end"), c, b.get("knowledge_col")))

    # ── DP-04 ticks ──
    @app.get("/api/data/ticks/status")
    async def ticks_status(days: int = 10):
        return await run(lambda c: rows(c, "SELECT * FROM tick_capture_status WHERE day>=? ORDER BY day DESC",
                                        str(date.today() - timedelta(days=_n(days, 1, 365, "days")))))

    @app.get("/api/data/ticks")
    async def ticks_read(day: str = None, symbol: str = None, limit: int = 2000):
        from data.ticks import ticks
        def q(c):
            df = ticks(day or str(date.today()), symbol, c)
            lim = _n(limit, 1, 100000, "limit")
            return {"rows": len(df), "data": df.tail(lim).to_dict("records") if not df.empty else []}
        return await run(q)

    @app.post("/api/data/ticks/minute-bars", dependencies=guard)
    async def ticks_minute(request: Req):
        b = await body(request)
        from data.ticks import build_minute_bars
        return await run(lambda c: build_minute_bars(b.get("day"), c))

    # ── DP-05 depth ──
    @app.get("/api/data/depth")
    async def depth(symbol: str = None, limit: int = 200):
        def q(c):
            lim = _n(limit, 1, 5000, "limit")
            if symbol:
                return rows(c, "SELECT * FROM order_book_snapshot WHERE symbol=? ORDER BY ts DESC LIMIT ?", symbol.upper(), lim)
            return rows(c, "SELECT o.* FROM order_book_snapshot o JOIN (SELECT symbol, MAX(ts) m FROM order_book_snapshot "
                           "GROUP BY symbol) x ON x.symbol=o.symbol AND x.m=o.ts ORDER BY o.spread_bps LIMIT ?", lim)
        return await run(q)

    @app.get("/api/data/depth/features")
    async def depth_features(symbol: str, day: str = None):
        from data.depth import features
        return await run(lambda c: features(c, symbol, day) or {"note": "no snapshots for that day"})

    @app.post("/api/data/depth/snapshot", dependencies=guard)
    async def depth_snapshot(request: Req):
        b = await body(request)
        from data.depth import snapshot
        return await run(lambda c: snapshot(b.get("symbols") or None, c))

    # ── DP-08 derivatives ──
    @app.get("/api/data/fo/contracts")
    async def fo_contracts(symbol: str, date: str = None, expiry: str = None):
        from data.derivatives_store import contracts
        return await run(lambda c: contracts(c, symbol, date, expiry))

    @app.get("/api/data/fo/chain")
    async def fo_chain(symbol: str, expiry: str = None, ts: str = None):
        from data.derivatives_store import chain
        return await run(lambda c: chain(c, symbol, ts, expiry))

    @app.post("/api/data/fo/chain/snapshot", dependencies=guard)
    async def fo_chain_snapshot(request: Req):
        b = await body(request)
        from data.derivatives_store import snapshot_option_chain
        if not b.get("symbol"):
            return JSONResponse({"error": "symbol is required"}, status_code=400)
        return await run(lambda c: snapshot_option_chain(str(b["symbol"]), c))

    @app.get("/api/data/fo/surface")
    async def fo_surface(symbol: str, date: str = None):
        from quant.derivatives import vol_surface
        def q(c):
            s = vol_surface(c, symbol, date)
            if s["status"] != "OK":
                raise LookupError(f"{s['status']}: {s['reason']}")
            return s
        return await run(q)

    # ── DP-14 macro ──
    @app.get("/api/data/macro")
    async def macro():
        from data.macro import latest_snapshot
        return await run(lambda c: {"series": latest_snapshot(c), "calendar": rows(
            c, "SELECT * FROM macro_calendar WHERE event_date>=? ORDER BY event_date LIMIT 50",
            str(date.today() - timedelta(days=7)))})

    @app.get("/api/data/macro/{series_id}")
    async def macro_series(series_id: str, as_of: str = None):
        from data.macro import point_in_time
        return await run(lambda c: point_in_time(c, series_id.upper(), as_of))

    @app.post("/api/data/macro/refresh", dependencies=guard)
    async def macro_refresh():
        from data.macro import run_macro
        return await run(lambda c: run_macro(c))

    @app.post("/api/data/macro/calendar", dependencies=guard)
    async def macro_calendar_add(request: Req):
        b = await body(request)
        from data.macro import add_calendar
        return await run(lambda c: add_calendar(c, b.get("event_date"), b.get("event"), b.get("importance") or "HIGH",
                                                b.get("country") or "IN", b.get("series_id"), b.get("note") or ""))

    # ── DP-21 multi-asset ──
    @app.get("/api/data/assets")
    async def assets_catalogue():
        from data.multi_asset import catalogue
        return await run(catalogue)

    @app.get("/api/data/assets/{asset_class}/{symbol}")
    async def asset_series(asset_class: str, symbol: str, start: str = None, end: str = None):
        from data.multi_asset import series
        return await run(lambda c: series(c, asset_class, symbol, start, end))

    @app.get("/api/data/mf")
    async def mf(q: str = "", limit: int = 50):
        def f(c):
            lim = _n(limit, 1, 1000, "limit")
            d = c.execute("SELECT MAX(date) FROM mf_nav").fetchone()[0]
            return rows(c, "SELECT scheme_code, scheme_name, nav, date, amc, category FROM mf_nav WHERE date=? AND "
                           "scheme_name LIKE ? ORDER BY scheme_name LIMIT ?", d, f"%{q}%", lim)
        return await run(f)

    @app.post("/api/data/assets/refresh", dependencies=guard)
    async def assets_refresh():
        from data.multi_asset import run_multi_asset
        return await run(lambda c: run_multi_asset(10, c))

    # ── AD-01/02 alt data ──
    @app.get("/api/data/alt")
    async def alt_catalogue():
        from altdata.framework import catalogue
        return await run(catalogue)

    @app.get("/api/data/alt/{source_id}/{metric}")
    async def alt_read(source_id: str, metric: str, entity: str = None, start: str = None, end: str = None,
                       as_of: str = None):
        from altdata.framework import read
        return await run(lambda c: read(c, source_id, metric, entity, start, end, as_of))

    @app.post("/api/data/alt/{source_id}/run", dependencies=guard)
    async def alt_run(source_id: str, request: Req):
        b = await body(request)
        from altdata.framework import run_source
        return await run(lambda c: run_source(c, source_id, b.get("as_of")))

