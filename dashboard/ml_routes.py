"""
W5 AI/ML API and the /ml page. Same conventions as the other route modules:
JSON, 400 invalid, 404 unknown, X-ATIP-Token on anything that changes state.
Nothing here can create an intent, a risk decision or an order.

    GET  /api/ml/status                   config, model families available, counts
    GET  /api/ml/features                 feature registry (metadata)
    GET  /api/ml/feature-sets             POST (token) {name, version, features, description}
    GET  /api/ml/labels                   label kinds
    GET  /api/ml/datasets                 POST (token) {spec..., build?: false}
    GET  /api/ml/datasets/{id}
    GET  /api/ml/models                   POST (token) {model_id, name, model_type, label_kind, feature_set, ...}
    GET  /api/ml/models/{id}              versions, events, metrics, explanations
    POST /api/ml/models/{id}/train        (token) {dataset: spec, params?} -- background
    POST /api/ml/models/{id}/lifecycle    (token) {version, to_state, reason}
    POST /api/ml/models/{id}/activate     (token) {version, reason}   (APPROVED -> ACTIVE only)
    POST /api/ml/models/{id}/pause        (token) {version?, reason}
    GET  /api/ml/training-runs
    GET  /api/ml/predictions              filters; GET /api/ml/predictions/{id}
    POST /api/ml/predict                  (token) {model_id, version?, as_of?, store?}
    GET  /api/ml/monitoring               ?model_id
    GET  /api/ml/regime                   deterministic vs configured provider for a date
    GET  /api/ml/explain/{symbol}         ?as_of&q= -- read-only "why" facts
    GET  /ml                              page
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
import json
import threading

from fastapi.responses import HTMLResponse, JSONResponse


def register(app, guard, Req, get_connection, json_safe):
    from ml import assistant as AS, dataset as DS, feature_registry as FR, labels as LB, models as MD
    from ml import predict as PR, registry as REG, training as TR
    from ml.config import settings
    from ml.registry import ModelRegistryError

    BAD = (ValueError, KeyError, TypeError, ModelRegistryError, MD.ModelDependencyError)
    _ready = {"done": False}

    def conn_ready():
        conn = get_connection()
        if not _ready["done"]:
            FR.sync_features(conn)
            FR.ensure_builtin_sets(conn)
            _ready["done"] = True
        return conn

    def err(e, code=400):
        return JSONResponse({"error": str(e)}, status_code=code)

    def err_for(e):
        return err(e, 404 if str(e).startswith("no ") else 400)

    async def body(request):
        try:
            b = await request.json()
        except Exception:
            return {}
        return b if isinstance(b, dict) else {}

    def rows(conn, q, args=()):
        return [dict(r) for r in conn.execute(q, args)]

    def loads(d, *keys):
        for k in keys:
            if k in d:
                d[k.replace("_json", "")] = json.loads(d.pop(k) or "null")
        return d

    @app.get("/api/ml/status")
    async def api_ml_status():
        conn = conn_ready()
        try:
            counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in (
                "ml_feature", "ml_feature_set", "ml_dataset", "ml_model", "ml_model_version", "ml_prediction",
                "ml_training_run")}
            active = rows(conn, "SELECT model_id, version, feature_set, activated_at FROM ml_model_version "
                                "WHERE status='ACTIVE'")
            return JSONResponse(json_safe({"settings": settings(), "model_families": MD.availability(),
                                           "counts": counts, "active_versions": active,
                                           "execution_link": "none: ML output reaches trading only as a strategy "
                                                             "feature, then W3 -> W4 risk -> OMS"}))
        finally:
            conn.close()

    @app.get("/api/ml/features")
    async def api_ml_features():
        conn = conn_ready()
        try:
            return JSONResponse(json_safe([loads(d, "dependencies_json") for d in
                                           rows(conn, "SELECT * FROM ml_feature ORDER BY category, name")]))
        finally:
            conn.close()

    @app.get("/api/ml/feature-sets")
    async def api_ml_feature_sets():
        conn = conn_ready()
        try:
            return JSONResponse(json_safe([loads(d, "features_json", "feature_versions_json") for d in
                                           rows(conn, "SELECT * FROM ml_feature_set ORDER BY name, version")]))
        finally:
            conn.close()

    @app.post("/api/ml/feature-sets", dependencies=guard)
    async def api_ml_feature_set_create(request: Req):
        b = await body(request)
        conn = conn_ready()
        try:
            fs = FR.FeatureSet(b.get("name"), str(b.get("version") or "1"), b.get("features") or [],
                               b.get("description", ""))
            return JSONResponse(json_safe(FR.save_feature_set(conn, fs)))
        except BAD as e:
            return err(e)
        finally:
            conn.close()

    @app.get("/api/ml/labels")
    async def api_ml_labels():
        return JSONResponse({"kinds": LB.KINDS, "classes": LB.CLASSES,
                             "defaults": LB.LabelSpec().as_dict()})

    @app.get("/api/ml/datasets")
    async def api_ml_datasets():
        conn = conn_ready()
        try:
            return JSONResponse(json_safe([DS.get(conn, r[0]) for r in conn.execute(
                "SELECT dataset_id FROM ml_dataset ORDER BY created_at DESC")]))
        finally:
            conn.close()

    @app.get("/api/ml/datasets/{did}")
    async def api_ml_dataset(did: str):
        conn = conn_ready()
        try:
            d = DS.get(conn, did)
            return JSONResponse(json_safe(d)) if d else err(f"no dataset {did}", 404)
        finally:
            conn.close()

    @app.post("/api/ml/datasets", dependencies=guard)
    async def api_ml_dataset_create(request: Req):
        """Body = a DatasetSpec (+ build: true to build and snapshot it in the background)."""
        b = await body(request)
        build = bool(b.pop("build", False))
        conn = conn_ready()
        try:
            spec = DS.DatasetSpec.from_dict(b)
            out = DS.register(conn, spec)
        except BAD as e:
            return err(e)
        finally:
            conn.close()
        if build:
            def job():
                from ml.config import model_root
                c = get_connection()
                try:
                    ds = DS.build(c, spec)
                    DS.register(c, spec, ds, DS.save_snapshot(ds, model_root()))
                except Exception as e:
                    c.execute("UPDATE ml_dataset SET status=?, summary_json=? WHERE dataset_id=?",
                              ("FAILED", json.dumps({"error": str(e)}), spec.dataset_id)); c.commit()
                finally:
                    c.close()
            threading.Thread(target=job, daemon=True).start()
            out["status"] = "BUILDING"
        return JSONResponse(json_safe(out))

    @app.get("/api/ml/models")
    async def api_ml_models():
        conn = conn_ready()
        try:
            return JSONResponse(json_safe(REG.list_models(conn)))
        finally:
            conn.close()

    @app.post("/api/ml/models", dependencies=guard)
    async def api_ml_model_create(request: Req):
        b = await body(request)
        conn = conn_ready()
        try:
            return JSONResponse(json_safe(REG.create_model(
                conn, b.get("model_id"), b.get("name") or b.get("model_id"), b.get("model_type"),
                b.get("label_kind"), b.get("feature_set") or f"{settings()['feature_set']}@1",
                b.get("description", ""), purpose=b.get("purpose", "signal"))))
        except BAD as e:
            return err(e)
        finally:
            conn.close()

    @app.get("/api/ml/models/{mid}")
    async def api_ml_model(mid: str):
        conn = conn_ready()
        try:
            m = REG.get_model(conn, mid)
            if not m:
                return err(f"no model {mid}", 404)
            m["versions"] = REG.list_versions(conn, mid)
            m["events"] = REG.events(conn, mid)
            m["metrics"] = [loads(d, "metrics_json") for d in rows(
                conn, "SELECT * FROM ml_model_metrics WHERE model_id=? ORDER BY created_at DESC LIMIT 50", (mid,))]
            m["explanations"] = [loads(d, "payload_json") for d in rows(
                conn, "SELECT * FROM ml_model_explanation WHERE model_id=? ORDER BY created_at DESC", (mid,))]
            m["allowed_transitions"] = {k: sorted(v) for k, v in REG.TRANSITIONS.items()}
            return JSONResponse(json_safe(m))
        finally:
            conn.close()

    @app.post("/api/ml/models/{mid}/train", dependencies=guard)
    async def api_ml_train(mid: str, request: Req):
        """{dataset: DatasetSpec, params?, validation_fraction?, embargo?} -- runs in the background;
        follow it in /api/ml/training-runs."""
        b = await body(request)
        conn = conn_ready()
        try:
            if not REG.get_model(conn, mid):
                return err(f"no model {mid}", 404)
            DS.DatasetSpec.from_dict(b.get("dataset") or {})        # validate before starting
        except BAD as e:
            return err(e)
        finally:
            conn.close()

        def job():
            c = get_connection()
            try:
                TR.train(c, mid, b["dataset"], b.get("params"), b.get("validation_fraction"), b.get("embargo"))
            except Exception:
                pass                                                 # recorded in ml_training_run
            finally:
                c.close()
        threading.Thread(target=job, daemon=True).start()
        return JSONResponse({"model_id": mid, "status": "STARTED"})

    async def _move(mid, request, to=None):
        b = await body(request)
        conn = conn_ready()
        try:
            ver = b.get("version")
            if to == "PAUSED" and not ver:
                av = REG.active_version(conn, mid)
                ver = av["version"] if av else None
            return JSONResponse(json_safe(REG.transition(conn, mid, ver, to or b.get("to_state"),
                                                         b.get("reason", ""))))
        except BAD as e:
            return err_for(e)
        finally:
            conn.close()

    @app.post("/api/ml/models/{mid}/lifecycle", dependencies=guard)
    async def api_ml_lifecycle(mid: str, request: Req):
        return await _move(mid, request)

    @app.post("/api/ml/models/{mid}/activate", dependencies=guard)
    async def api_ml_activate(mid: str, request: Req):
        return await _move(mid, request, "ACTIVE")

    @app.post("/api/ml/models/{mid}/pause", dependencies=guard)
    async def api_ml_pause(mid: str, request: Req):
        return await _move(mid, request, "PAUSED")

    @app.get("/api/ml/training-runs")
    async def api_ml_runs(model_id: str = None, limit: int = 100):
        conn = conn_ready()
        try:
            q = "SELECT * FROM ml_training_run" + (" WHERE model_id=?" if model_id else "") + \
                " ORDER BY started_at DESC LIMIT ?"
            return JSONResponse(json_safe([loads(d, "config_json", "metrics_json") for d in
                                           rows(conn, q, ([model_id] if model_id else []) + [int(limit)])]))
        finally:
            conn.close()

    @app.get("/api/ml/predictions")
    async def api_ml_predictions(model_id: str = None, symbol: str = None, as_of: str = None, limit: int = 200):
        conn = conn_ready()
        try:
            parts, args = [], []
            for col, v in (("model_id", model_id), ("symbol", symbol and symbol.upper()), ("as_of", as_of)):
                if v:
                    parts.append(f"{col}=?"); args.append(v)
            q = "SELECT * FROM ml_prediction" + (" WHERE " + " AND ".join(parts) if parts else "") + \
                " ORDER BY as_of DESC, ml_score DESC LIMIT ?"
            out = [loads(d, "probabilities_json", "explanation_json") for d in rows(conn, q, args + [int(limit)])]
            for d in out:
                d.pop("features_json", None)
            return JSONResponse(json_safe(out))
        finally:
            conn.close()

    @app.get("/api/ml/predictions/{pid}")
    async def api_ml_prediction(pid: str):
        conn = conn_ready()
        try:
            r = conn.execute("SELECT * FROM ml_prediction WHERE prediction_id=?", (pid,)).fetchone()
            if not r:
                return err(f"no prediction {pid}", 404)
            d = loads(dict(r), "probabilities_json", "explanation_json", "features_json")
            d["model_version_detail"] = REG.get_version(conn, d["model_id"], d["model_version"])
            return JSONResponse(json_safe(d))
        finally:
            conn.close()

    @app.post("/api/ml/predict", dependencies=guard)
    async def api_ml_predict(request: Req):
        b = await body(request)
        conn = conn_ready()
        try:
            out = PR.predict(conn, b.get("model_id"), b.get("as_of"), b.get("version"),
                             store=bool(b.get("store", True)))
            out["predictions"] = out["predictions"][:50]
            return JSONResponse(json_safe(out))
        except BAD as e:
            return err_for(e)
        finally:
            conn.close()

    @app.get("/api/ml/monitoring")
    async def api_ml_monitoring(model_id: str = None, limit: int = 60):
        conn = conn_ready()
        try:
            q = "SELECT * FROM ml_model_monitoring" + (" WHERE model_id=?" if model_id else "") + \
                " ORDER BY as_of DESC LIMIT ?"
            return JSONResponse(json_safe([loads(d, "prediction_dist_json", "feature_drift_json", "data_quality_json")
                                           for d in rows(conn, q, ([model_id] if model_id else []) + [int(limit)])]))
        finally:
            conn.close()

    @app.get("/api/ml/regime")
    async def api_ml_regime(as_of: str = None):
        from datetime import date
        from strategy_engine.regime import MarketHealthRegime, get_provider
        conn = conn_ready()
        try:
            d = date.fromisoformat(as_of) if as_of else (conn.execute("SELECT MAX(date) FROM market_health")
                                                         .fetchone()[0])
            d = d if isinstance(d, date) else date.fromisoformat(str(d)[:10])
            prov = get_provider(conn, d, d)
            return JSONResponse(json_safe({"as_of": str(d), "configured_source": settings()["regime_source"],
                                           "provider": prov.name, "configured": prov.on(d),
                                           "deterministic": MarketHealthRegime(conn, d, d).on(d)}))
        finally:
            conn.close()

    @app.get("/api/ml/explain/{symbol}")
    async def api_ml_explain(symbol: str, as_of: str = None, q: str = None):
        conn = conn_ready()
        try:
            return JSONResponse(json_safe(AS.answer(conn, q, symbol, as_of) if q else AS.explain_symbol(conn, symbol,
                                                                                                        as_of)))
        finally:
            conn.close()

    @app.get("/ml", response_class=HTMLResponse)
    async def ml_page():
        from dashboard.ml_page import render
        from dashboard.security import token as _tok
        return HTMLResponse(render(_tok()))
