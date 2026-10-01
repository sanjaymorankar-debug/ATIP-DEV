"""
W28b API: news weighting (NS-05) and corporate announcements (NS-06).

    GET  /api/news/symbol-scores           ?date=&n=50  weighted news score per stock (50 = neutral)
    GET  /api/news/sources                 measured source weights + feed status
    GET  /api/news/announcements           ?symbol=&event_type=&days=7&n=100
    POST /api/news/announcements/run       (token) fetch today's announcements now; body {days?, deep?}
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
import json
from datetime import date, timedelta

from fastapi.responses import JSONResponse
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

    @app.get("/api/news/symbol-scores")
    async def news_symbol_scores(date: str = None, n: int = 50):
        def q(c):
            lim = _n(n, 1, 500, "n")
            d = date or c.execute("SELECT MAX(date) FROM news_symbol_score").fetchone()[0]
            if not d:
                return {"date": None, "rows": [], "note": "no weighted news scores yet (post-market job news_weights)"}
            rows = [dict(r) for r in c.execute(
                "SELECT symbol, score, n_articles, effective_weight, mean_sentiment FROM news_symbol_score WHERE date=? "
                "ORDER BY ABS(score-50) DESC LIMIT ?", (str(d)[:10], lim))]
            return {"date": str(d)[:10], "rows": rows}
        return await run(q)

    @app.get("/api/news/sources")
    async def news_sources():
        return await run(lambda c: {
            "quality": [dict(r) for r in c.execute("SELECT * FROM news_source_quality ORDER BY weight DESC")],
            "status": [dict(r) for r in c.execute("SELECT * FROM news_source_status ORDER BY source")]})

    @app.get("/api/news/announcements")
    async def news_announcements(symbol: str = None, event_type: str = None, days: int = 7, n: int = 100):
        def q(c):
            sql = ("SELECT ann_id, symbol, company, broadcast_at, subject, attachment_url, event_type, tone, importance, "
                   "confidence, summary, classifier, nlp_json FROM corporate_announcement WHERE broadcast_at>=?")
            args = [str(date.today() - timedelta(days=_n(days, 1, 365, "days")))]
            if symbol:
                sql += " AND symbol=?"
                args.append(symbol.upper())
            if event_type:
                sql += " AND event_type=?"
                args.append(event_type.upper())
            sql += " ORDER BY broadcast_at DESC LIMIT ?"
            args.append(_n(n, 1, 1000, "n"))
            rows = [dict(r) for r in c.execute(sql, args)]
            for r in rows:
                r["nlp"] = json.loads(r.pop("nlp_json") or "null")
            return rows
        return await run(q)

    @app.post("/api/news/announcements/run", dependencies=guard)
    async def news_announcements_run(request: Req):
        b = await body(request)
        from data.announcements import run_announcements
        days = _n(b.get("days", 1), 1, 30, "days")
        return await run(lambda c: run_announcements(days, c, deep=bool(b.get("deep", True))))
