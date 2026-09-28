"""
    python -m ml status
    python -m ml sync                                   # feature registry + built-in feature sets
    python -m ml create-model MODEL_ID --type logistic_regression --label direction [--feature-set atip_core@1] [--purpose signal]
    python -m ml train MODEL_ID dataset.json [--params '{"l2": 1}']
    python -m ml lifecycle MODEL_ID VERSION TO_STATE --reason "..."
    python -m ml predict MODEL_ID [--as-of 2026-09-24] [--version v1] [--no-store]
    python -m ml explain SYMBOL [--q "why this score"]
    W24:
    python -m ml bootstrap --start D --end D [--families logistic_regression,native_gbm,ensemble] [--no-train]
    python -m ml regime-model --start D --end D [--family native_gbm]
    python -m ml validate MODEL_TYPE dataset.json [--windows 5]     walk-forward report
    python -m ml select REPORT_ID [--save-as name@version]           importance-stability selection
    python -m ml clusters [--as-of D] [--k 6] | pca [--as-of D] | anomalies [--as-of D] | health

dataset.json is a DatasetSpec, e.g.
  {"name": "dir5_tech", "version": "1", "feature_set": "atip_technical@1",
   "label": {"kind": "direction", "horizon": 5, "threshold_pct": 1.0},
   "start": "2025-06-01", "end": "2026-08-31", "sampling": {"every_n_sessions": 5, "max_symbols": 100}}
"""

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m ml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status"); sub.add_parser("sync")
    c = sub.add_parser("create-model"); c.add_argument("model_id"); c.add_argument("--type", required=True)
    c.add_argument("--label", required=True); c.add_argument("--feature-set", default="atip_core@1")
    c.add_argument("--purpose", default="signal"); c.add_argument("--name")
    t = sub.add_parser("train"); t.add_argument("model_id"); t.add_argument("dataset")
    t.add_argument("--params", type=json.loads)
    l = sub.add_parser("lifecycle"); l.add_argument("model_id"); l.add_argument("version"); l.add_argument("to_state")
    l.add_argument("--reason", default="")
    p = sub.add_parser("predict"); p.add_argument("model_id"); p.add_argument("--as-of"); p.add_argument("--version")
    p.add_argument("--no-store", action="store_true")
    b = sub.add_parser("bootstrap"); b.add_argument("--start", required=True); b.add_argument("--end", required=True)
    b.add_argument("--families", default="logistic_regression,native_gbm,ensemble"); b.add_argument("--no-train",
                                                                                                    action="store_true")
    b.add_argument("--max-symbols", type=int, default=150); b.add_argument("--every", type=int, default=3)
    rg = sub.add_parser("regime-model"); rg.add_argument("--start", required=True); rg.add_argument("--end", required=True)
    rg.add_argument("--family", default="native_gbm")
    v = sub.add_parser("validate"); v.add_argument("model_type"); v.add_argument("dataset")
    v.add_argument("--windows", type=int, default=5); v.add_argument("--params", type=json.loads)
    fsel = sub.add_parser("select"); fsel.add_argument("report_id"); fsel.add_argument("--save-as")
    fsel.add_argument("--min-stability", type=float, default=0.6)
    cl = sub.add_parser("clusters"); cl.add_argument("--as-of"); cl.add_argument("--k", type=int, default=6)
    pc = sub.add_parser("pca"); pc.add_argument("--as-of"); pc.add_argument("--lookback", type=int, default=120)
    an = sub.add_parser("anomalies"); an.add_argument("--as-of"); an.add_argument("--threshold", type=float, default=4.0)
    sub.add_parser("health")
    e = sub.add_parser("explain"); e.add_argument("symbol"); e.add_argument("--q"); e.add_argument("--as-of")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from db.schema import get_connection
    from ml import assistant, feature_registry as FR, models, predict, registry, training
    from ml.config import settings
    conn = get_connection()
    try:
        if a.cmd == "status":
            out = {"settings": settings(), "families": models.availability(), "models": registry.list_models(conn)}
        elif a.cmd == "sync":
            from ml import labels
            out = {"features": FR.sync_features(conn), "feature_sets": FR.ensure_builtin_sets(conn),
                   "labels": labels.ensure_builtin_labels(conn)}
        elif a.cmd == "create-model":
            out = registry.create_model(conn, a.model_id, a.name or a.model_id, a.type, a.label, a.feature_set,
                                        purpose=a.purpose)
        elif a.cmd == "train":
            out = training.train(conn, a.model_id, json.loads(Path(a.dataset).read_text(encoding="utf-8")), a.params)
        elif a.cmd == "lifecycle":
            out = registry.transition(conn, a.model_id, a.version, a.to_state, a.reason)
        elif a.cmd == "predict":
            out = predict.predict(conn, a.model_id, a.as_of, a.version, store=not a.no_store)
            out["predictions"] = out["predictions"][:20]
        elif a.cmd == "bootstrap":
            from ml.presets import bootstrap
            out = bootstrap(conn, a.start, a.end, tuple(a.families.split(",")), train=not a.no_train,
                            sampling={"every_n_sessions": a.every, "max_symbols": a.max_symbols})
        elif a.cmd == "regime-model":
            from ml.presets import regime
            out = regime(conn, a.start, a.end, a.family)
        elif a.cmd == "validate":
            from ml.validation import walk_forward
            out = walk_forward(conn, a.model_type, json.loads(Path(a.dataset).read_text(encoding="utf-8")), a.params,
                               a.windows)
        elif a.cmd == "select":
            from ml import selection
            if a.save_as:
                name, _, ver = a.save_as.partition("@")
                out = selection.apply(conn, a.report_id, name, ver or "1", a.min_stability)
            else:
                out = selection.recommend(conn, a.report_id, a.min_stability)
        elif a.cmd == "clusters":
            from ml.unsupervised import cluster_stocks
            out = cluster_stocks(conn, a.as_of, a.k)
        elif a.cmd == "pca":
            from ml.unsupervised import return_pca
            out = return_pca(conn, a.as_of, a.lookback)
        elif a.cmd == "anomalies":
            from ml.unsupervised import detect_anomalies
            out = detect_anomalies(conn, a.as_of, threshold=a.threshold)
        elif a.cmd == "health":
            from ml.decay import run
            out = run(conn, notify=False)
        elif a.cmd == "explain":
            out = assistant.answer(conn, a.q, a.symbol, a.as_of) if a.q else assistant.explain_symbol(conn, a.symbol,
                                                                                                        a.as_of)
    finally:
        conn.close()
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
