"""
    python -m ml status
    python -m ml sync                                   # feature registry + built-in feature sets
    python -m ml create-model MODEL_ID --type logistic_regression --label direction [--feature-set atip_core@1] [--purpose signal]
    python -m ml train MODEL_ID dataset.json [--params '{"l2": 1}']
    python -m ml lifecycle MODEL_ID VERSION TO_STATE --reason "..."
    python -m ml predict MODEL_ID [--as-of 2026-09-24] [--version v1] [--no-store]
    python -m ml explain SYMBOL [--q "why this score"]

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
        elif a.cmd == "explain":
            out = assistant.answer(conn, a.q, a.symbol, a.as_of) if a.q else assistant.explain_symbol(conn, a.symbol,
                                                                                                        a.as_of)
    finally:
        conn.close()
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
