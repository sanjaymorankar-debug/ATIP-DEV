"""
W28 insights API + the /insights page. JSON; 400 invalid; 404 unknown; token on state changes.

    GET  /insights                            the page (dashboard/insights_page.py)
    GET  /api/insights/news-digest            ?date  latest AI news summary (DB-06 / NS-04)
    POST /api/insights/news-digest            (token) {session: premarket|midday|close, extractive?}
    GET  /api/insights/news                   ?symbol&event_type&hours=48&limit=100  classified articles (NS-02/03/05)
    GET  /api/insights/news/scores            ?date&limit  per-symbol weighted news scores (NS-05)
    GET  /api/insights/news/usage             ?days=14  LLM calls / tokens / estimated cost (NS-02 cost cap)
    GET  /api/insights/news/sources           measured source quality + feed status
    GET  /api/insights/announcements          ?symbol&event_type&days=7&limit  NSE announcements (NS-06)
    GET  /api/insights/lists                  ?date  crash-risk list, top-25 SPI, MSI gauge + history (DB-11)
    GET  /api/insights/strategies             ?window=60  per-strategy performance (DB-16)
    GET  /api/insights/strategies/{sid}/equity ?version&days=250  paper realised-P&L curve
    POST /api/insights/strategies/refresh     (token) recompute strategy_performance now
    GET  /api/insights/models                 ?days=90  model monitoring series + factor decay (DB-18)
    GET  /api/insights/ai-strategies          SE-05 readiness
    POST /api/insights/ai-strategies/activate (token) {model_id, reason}
    GET  /api/insights/scans                  ?date&scan  intraday scan hits (SG-08)
    POST /api/insights/scans/run              (token) run the scan on today's stored bars
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
import json
from datetime import date, datetime, timedelta

from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool


def _rows(conn, sql, *args):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _day(v):
    if not v:
        return None
    return date.fromisoformat(str(v)[:10])


def lists(conn, td=None) -> dict:
    """DB-11. MSI is one market-wide value per session (scores/engine.compute_msi), so it is
    shown as a gauge with its history, not as a ranked per-stock list."""
    td = td or conn.execute("SELECT MAX(date) FROM ai_scores").fetchone()[0]
    if not td:
        return {"date": None, "crash_risk": [], "top_spi": [], "msi": None, "msi_history": []}
    td = str(td)[:10]
    prev = conn.execute("SELECT MAX(date) FROM ai_scores WHERE date<?", (td,)).fetchone()[0]
    crash = _rows(conn,
                  "SELECT s.symbol, s.cri, s.atip_score, s.signal, s.beta_1y, p.close, "
                  "(SELECT cri FROM ai_scores x WHERE x.symbol=s.symbol AND x.date=?) AS prev_cri "
                  "FROM ai_scores s LEFT JOIN prices_daily p ON p.symbol=s.symbol AND p.date=s.date "
                  "WHERE s.date=? AND s.cri IS NOT NULL ORDER BY s.cri DESC LIMIT 25", prev, td)
    for r in crash:
        comps = _rows(conn, "SELECT component, value, weight FROM score_components WHERE symbol=? AND date=? AND "
                            "index_name='CRI'", r["symbol"], td)
        comps = [dict(c, contribution=round(c["value"] * c["weight"], 2)) for c in comps if c["weight"] is not None]
        r["drivers"] = sorted(comps, key=lambda c: -c["contribution"])[:3]
        r["cri_change"] = None if r["prev_cri"] is None else round(r["cri"] - r["prev_cri"], 2)
        r["band"] = "HIGH" if r["cri"] >= 70 else ("ELEVATED" if r["cri"] >= 60 else "NORMAL")
    spi = _rows(conn, "SELECT s.symbol, s.spi, s.fund_score, s.atip_score, s.signal, p.close FROM ai_scores s "
                      "LEFT JOIN prices_daily p ON p.symbol=s.symbol AND p.date=s.date WHERE s.date=? AND s.spi IS NOT "
                      "NULL ORDER BY s.spi DESC LIMIT 25", td)
    hist = _rows(conn, "SELECT date, MAX(msi) AS msi FROM ai_scores WHERE date<=? AND date>=date(?, '-120 day') "
                       "AND msi IS NOT NULL GROUP BY date ORDER BY date", td, td)
    msi = hist[-1]["msi"] if hist and hist[-1]["date"] == td else None
    comps = _rows(conn, "SELECT component, value, weight FROM score_components WHERE date=? AND index_name='MSI' "
                        "GROUP BY component", td)
    return {"date": td, "crash_risk": crash, "top_spi": spi,
            "spi_note": None if spi else "No SPI values for this session: fundamentals are not populated yet "
                                         "(W27 DP-15 / SC-02).",
            "msi": msi, "msi_components": comps, "msi_history": hist}


def models(conn, days=90) -> dict:
    """DB-18: per model version, the monitoring series (drift and prediction distribution),
    realised-vs-validation metrics, daily prediction counts / mean ML score, and factor decay."""
    since = str(date.today() - timedelta(days=int(days)))
    out = []
    for mid, ver, st in conn.execute("SELECT model_id, version, status FROM ml_model_version WHERE status NOT IN "
                                     "('FAILED','ARCHIVED') ORDER BY model_id, created_at").fetchall():
        mon = []
        for a, dj, fj, ns in conn.execute("SELECT as_of, prediction_dist_json, feature_drift_json, n_shifted FROM "
                                          "ml_model_monitoring WHERE model_id=? AND version=? AND as_of>=? ORDER BY "
                                          "as_of", (mid, ver, since)):
            drift = json.loads(fj or "{}")
            psis = [d.get("psi") for d in drift.values() if d.get("psi") is not None]
            mon.append({"as_of": str(a)[:10], "n_shifted": ns, "columns": len(drift),
                        "max_psi": round(max(psis), 4) if psis else None,
                        "mean_psi": round(sum(psis) / len(psis), 4) if psis else None,
                        "dist": json.loads(dj or "{}")})
        preds = _rows(conn, "SELECT as_of, COUNT(*) AS n, ROUND(AVG(ml_score),2) AS mean_score, "
                            "ROUND(AVG(confidence),4) AS mean_conf FROM ml_prediction WHERE model_id=? AND "
                            "model_version=? AND as_of>=? GROUP BY as_of ORDER BY as_of", mid, ver, since)
        real = _rows(conn, "SELECT kind, period_start, period_end, metrics_json, created_at FROM ml_model_metrics "
                           "WHERE model_id=? AND version=? ORDER BY created_at DESC LIMIT 10", mid, ver)
        for r in real:
            r["metrics"] = json.loads(r.pop("metrics_json") or "{}")
        if mon or preds or st == "ACTIVE":
            out.append({"model_id": mid, "version": ver, "status": st, "monitoring": mon, "predictions": preds,
                        "metrics": real})
    try:
        from ml.decay import latest
        health = latest(conn)
    except Exception:
        health = None
    return {"models": out, "health": health}


def register(app, guard, Req, get_connection, json_safe):
    BAD = (ValueError, KeyError, TypeError)

    def err(e, code=400):
        return JSONResponse({"error": str(e)}, status_code=code)

    async def body(request):
        try:
            b = await request.json()
        except Exception:
            return {}
        return b if isinstance(b, dict) else {}

    def _call(fn):
        conn = get_connection()
        try:
            return JSONResponse(json_safe(fn(conn)))
        except LookupError as e:
            return err(e, 404)
        except BAD as e:
            return err(e)
        finally:
            conn.close()

    async def run(fn):
        return await run_in_threadpool(_call, fn)

    @app.get("/insights", response_class=HTMLResponse)
    async def insights_page():
        from dashboard.insights_page import render
        from dashboard.security import token
        return HTMLResponse(render(token()))

    # ── news (DB-06, NS-02..06) ──────────────────────────────────────────
    @app.get("/api/insights/news-digest")
    async def news_digest(date: str = None):
        from data.news_digest import latest
        return await run(lambda c: latest(c, _day(date)) or {})

    @app.post("/api/insights/news-digest", dependencies=guard)
    async def news_digest_build(request: Req):
        b = await body(request)
        from data.news_digest import build_digest, SESSIONS
        sess = str(b.get("session") or "close")
        if sess not in SESSIONS:
            return err(f"session must be one of {SESSIONS}")
        return await run(lambda c: build_digest(sess, c, force_extractive=bool(b.get("extractive"))))

    @app.get("/api/insights/news")
    async def news(symbol: str = None, event_type: str = None, hours: int = 48, limit: int = 100):
        def q(c):
            sql = ("SELECT id, fetched_at, headline, source, url, category, event_type, symbols_mentioned, sentiment, "
                   "confidence, importance, novelty, dup_of, source_weight, half_life_h, sentiment_lex, lex_confidence, "
                   "classifier, ai_summary FROM news_articles WHERE fetched_at>=?")
            args = [(datetime.now() - timedelta(hours=max(1, min(int(hours), 24 * 30)))).strftime("%Y-%m-%d %H:%M:%S")]
            if symbol:
                sql += " AND symbols_mentioned LIKE ?"
                args.append(f'%"{symbol.upper()}"%')
            if event_type:
                sql += " AND event_type=?"
                args.append(event_type.upper())
            sql += " ORDER BY fetched_at DESC LIMIT ?"
            args.append(max(1, min(int(limit), 500)))
            rows = _rows(c, sql, *args)
            for r in rows:
                r["symbols"] = json.loads(r.pop("symbols_mentioned") or "[]")
            return rows
        return await run(q)

    @app.get("/api/insights/news/scores")
    async def news_scores(date: str = None, limit: int = 100):
        def q(c):
            d = _day(date) or c.execute("SELECT MAX(date) FROM news_symbol_score").fetchone()[0]
            return {"date": str(d)[:10] if d else None,
                    "rows": _rows(c, "SELECT symbol, score, n_articles, effective_weight, mean_sentiment FROM "
                                     "news_symbol_score WHERE date=? ORDER BY ABS(score-50) DESC LIMIT ?",
                                  str(d)[:10] if d else None, max(1, min(int(limit), 500)))}
        return await run(q)

    @app.get("/api/insights/news/usage")
    async def news_usage(days: int = 14):
        def q(c):
            from data.news_ai import settings
            s = settings()
            since = str(date.today() - timedelta(days=max(1, min(int(days), 365))))
            return {"settings": {k: s[k] for k in ("enabled", "model", "effort", "daily_budget_usd", "batch_size")},
                    "rows": _rows(c, "SELECT * FROM news_ai_usage WHERE day>=? ORDER BY day DESC, purpose", since)}
        return await run(q)

    @app.get("/api/insights/news/sources")
    async def news_sources():
        return await run(lambda c: {
            "quality": _rows(c, "SELECT * FROM news_source_quality ORDER BY weight DESC"),
            "status": _rows(c, "SELECT * FROM news_source_status ORDER BY source")})

    @app.get("/api/insights/announcements")
    async def announcements(symbol: str = None, event_type: str = None, days: int = 7, limit: int = 200):
        def q(c):
            sql = "SELECT * FROM corporate_announcement WHERE broadcast_at>=?"
            args = [str(date.today() - timedelta(days=max(1, min(int(days), 365))))]
            if symbol:
                sql += " AND symbol=?"
                args.append(symbol.upper())
            if event_type:
                sql += " AND event_type=?"
                args.append(event_type.upper())
            sql += " ORDER BY broadcast_at DESC LIMIT ?"
            args.append(max(1, min(int(limit), 1000)))
            rows = _rows(c, sql, *args)
            for r in rows:
                r["nlp"] = json.loads(r.pop("nlp_json") or "null")
                r.pop("detail", None)
            return rows
        return await run(q)

    # ── lists (DB-11) ────────────────────────────────────────────────────
    @app.get("/api/insights/lists")
    async def insight_lists(date: str = None):
        return await run(lambda c: lists(c, _day(date)))

    # ── strategy performance (DB-16) ─────────────────────────────────────
    @app.get("/api/insights/strategies")
    async def strategy_perf(window: int = 60):
        from strategy_engine.performance import latest, WINDOWS
        if int(window) not in WINDOWS:
            return err(f"window must be one of {WINDOWS}")
        return await run(lambda c: latest(c, int(window)))

    @app.get("/api/insights/strategies/{sid}/equity")
    async def strategy_equity(sid: str, version: str = None, days: int = 250):
        from strategy_engine.performance import equity_curve
        return await run(lambda c: equity_curve(c, sid, version, max(5, min(int(days), 2000))))

    @app.post("/api/insights/strategies/refresh", dependencies=guard)
    async def strategy_perf_refresh():
        from strategy_engine.performance import run_all
        return await run(lambda c: run_all(None, c))

    # ── models (DB-18) / AI strategies (SE-05) ───────────────────────────
    @app.get("/api/insights/models")
    async def model_panel(days: int = 90):
        return await run(lambda c: models(c, max(7, min(int(days), 730))))

    @app.get("/api/insights/ai-strategies")
    async def ai_strategies():
        from ml.ai_strategies import readiness
        return await run(readiness)

    @app.post("/api/insights/ai-strategies/activate", dependencies=guard)
    async def ai_strategies_activate(request: Req):
        b = await body(request)
        from ml.ai_strategies import activate
        if not b.get("model_id"):
            return err("model_id is required")
        return await run(lambda c: activate(c, str(b["model_id"]), str(b.get("reason") or ""), "owner"))

    # ── intraday scans (SG-08) ───────────────────────────────────────────
    @app.get("/api/insights/scans")
    async def scans(date: str = None, scan: str = None):
        from scores.intraday_scan import hits
        def q(c):
            d = _day(date) or c.execute("SELECT MAX(scan_date) FROM intraday_scan_hit").fetchone()[0]
            return {"date": str(d)[:10] if d else None,
                    "hits": hits(c, str(d)[:10], scan.upper() if scan else None) if d else []}
        return await run(q)

    @app.post("/api/insights/scans/run", dependencies=guard)
    async def scans_run():
        from scores.intraday_scan import run_scan
        return await run(lambda c: run_scan(None, c))
