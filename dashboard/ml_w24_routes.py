"""
W24 ML API: validation reports, feature selection, clusters, PCA, anomalies, model /
factor health. JSON; 400 invalid; 404 unknown; token on state changes. Nothing here
activates a model or creates an order.

    GET  /api/ml/validation                  reports ; GET /api/ml/validation/{id}
    POST /api/ml/validation                  (token) {model_type, dataset, params?, windows?} -- background
    GET  /api/ml/validation/{id}/selection   ?min_stability ; POST (token) {name, version, min_stability}
    POST /api/ml/bootstrap                   (token) {start, end, families?} -- background
    GET  /api/ml/clusters                    latest run ; POST (token) {as_of?, k?}
    GET  /api/ml/pca                         ?as_of&lookback
    GET  /api/ml/anomalies                   ?as_of ; POST (token) {as_of?, threshold?}
    GET  /api/ml/health                      latest model / factor health ; POST (token) run now
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
import json
import threading

from fastapi.responses import JSONResponse


def register(app, guard, Req, get_connection, json_safe):
    from ml import decay as DC
    from ml import selection as SEL
    from ml import unsupervised as UN
    from ml import validation as VAL

    BAD = (ValueError, KeyError, TypeError)

    def err(e, code=400):
        return JSONResponse({"error": str(e)}, status_code=code)

    async def body(request):
        try:
            b = await request.json()
        except Exception:
            return {}
        return b if isinstance(b, dict) else {}

    def run(fn):
        conn = get_connection()
        try:
            return JSONResponse(json_safe(fn(conn)))
        except LookupError as e:
            return err(e, 404)
        except BAD as e:
            return err(e)
        finally:
            conn.close()

    def bg(fn):
        def go():
            c = get_connection()
            try:
                fn(c)
            except Exception:
                pass
            finally:
                c.close()
        threading.Thread(target=go, daemon=True).start()

    @app.get("/api/ml/validation")
    async def api_ml_validation_list():
        return run(lambda conn: VAL.list_reports(conn))

    @app.post("/api/ml/validation", dependencies=guard)
    async def api_ml_validation_run(request: Req):
        b = await body(request)
        from ml.dataset import DatasetSpec
        from ml.models import MODEL_TYPES
        try:
            if b.get("model_type") not in MODEL_TYPES:
                raise ValueError(f"model_type must be one of {sorted(MODEL_TYPES)}")
            DatasetSpec.from_dict(b["dataset"])
            w = int(b.get("windows", 5))
            if not 2 <= w <= 12:
                raise ValueError("windows must be 2..12")
        except BAD as e:
            return err(e)
        bg(lambda c: VAL.walk_forward(c, b["model_type"], b["dataset"], b.get("params"), w))
        return JSONResponse({"status": "RUNNING", "note": "the report appears in /api/ml/validation"})

    @app.get("/api/ml/validation/{rid}")
    async def api_ml_validation_get(rid: str):
        return run(lambda conn: VAL.get_report(conn, rid))

    @app.get("/api/ml/validation/{rid}/selection")
    async def api_ml_selection(rid: str, min_stability: float = 0.6):
        return run(lambda conn: SEL.recommend(conn, rid, min_stability))

    @app.post("/api/ml/validation/{rid}/selection", dependencies=guard)
    async def api_ml_selection_apply(rid: str, request: Req):
        b = await body(request)
        return run(lambda conn: SEL.apply(conn, rid, b.get("name"), str(b.get("version") or "1"),
                                          float(b.get("min_stability", 0.6))))

    @app.post("/api/ml/bootstrap", dependencies=guard)
    async def api_ml_bootstrap(request: Req):
        b = await body(request)
        from ml.presets import FAMILIES, bootstrap
        fam = tuple(b.get("families") or ("logistic_regression", "native_gbm", "ensemble"))
        if not b.get("start") or not b.get("end") or any(f not in FAMILIES for f in fam):
            return err(f"start, end required; families among {FAMILIES}")
        bg(lambda c: bootstrap(c, b["start"], b["end"], fam, train=b.get("train", True) is not False))
        return JSONResponse({"status": "RUNNING", "note": "reports in /api/ml/validation, models in /api/ml/models"})

    @app.get("/api/ml/clusters")
    async def api_ml_clusters():
        def f(conn):
            r = conn.execute("SELECT summary_json FROM ml_cluster_run ORDER BY created_at DESC LIMIT 1").fetchone()
            if not r:
                raise LookupError("no cluster run yet")
            return json.loads(r[0])
        return run(f)

    @app.post("/api/ml/clusters", dependencies=guard)
    async def api_ml_clusters_run(request: Req):
        b = await body(request)
        return run(lambda conn: UN.cluster_stocks(conn, b.get("as_of"), int(b.get("k", 6))))

    @app.get("/api/ml/pca")
    async def api_ml_pca(as_of: str = None, lookback: int = 120):
        return run(lambda conn: UN.return_pca(conn, as_of, max(20, min(lookback, 400))))

    @app.get("/api/ml/anomalies")
    async def api_ml_anomalies(as_of: str = None):
        def f(conn):
            d = as_of or conn.execute("SELECT MAX(as_of) FROM ml_anomaly").fetchone()[0]
            return {"as_of": d, "anomalies": [json.loads(r[0]) for r in conn.execute(
                "SELECT detail_json FROM ml_anomaly WHERE as_of=? ORDER BY score DESC", (str(d)[:10] if d else None,))]}
        return run(f)

    @app.post("/api/ml/anomalies", dependencies=guard)
    async def api_ml_anomalies_run(request: Req):
        b = await body(request)
        return run(lambda conn: UN.detect_anomalies(conn, b.get("as_of"), threshold=float(b.get("threshold", 4.0))))

    @app.get("/api/ml/health")
    async def api_ml_health():
        return run(lambda conn: DC.latest(conn) or {"status": "NEVER_RUN"})

    @app.post("/api/ml/health", dependencies=guard)
    async def api_ml_health_run():
        return run(lambda conn: DC.run(conn, notify=False))
