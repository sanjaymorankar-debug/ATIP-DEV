"""
Model artifacts: one immutable JSON file per model version.

    <ml.model_path>/models/<model_id>/<version>/model.json
      {"metadata": {...model.metadata()...}, "state": {...model.to_state()...},
       "feature_set": {...}, "dataset": {...}, "training_config": {...},
       "reference_stats": {...}}  (feature means/stds on the training rows,
                                   used by drift monitoring)

save() refuses to overwrite an existing file: a version that may be tied to
historical predictions is never replaced. load() verifies the stored sha256
before deserialising (the adapters' state is a pickle -- only ATIP's own,
hash-checked artifacts are ever loaded). Loaded models are cached by
(path, hash), so predictions never retrain or re-read a model needlessly.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ml.config import model_root
from ml.models import make_model

_CACHE = {}


def artifact_path(model_id: str, version: str) -> Path:
    return model_root() / "models" / model_id / version / "model.json"


def save(model, model_id: str, version: str, extra: dict) -> tuple:
    p = artifact_path(model_id, version)
    if p.exists():
        raise FileExistsError(f"artifact {p} already exists; model versions are immutable")
    p.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps({"metadata": model.metadata(), "state": model.to_state(), **extra}, sort_keys=True,
                      default=str).encode()
    p.write_bytes(blob)
    return str(p), hashlib.sha256(blob).hexdigest()


def load(path: str, expected_hash: str):
    key = (path, expected_hash)
    if key in _CACHE:
        return _CACHE[key]
    blob = Path(path).read_bytes()
    h = hashlib.sha256(blob).hexdigest()
    if h != expected_hash:
        raise ValueError(f"artifact {path} hash {h[:12]} does not match the registry ({expected_hash[:12]}) — "
                         f"refusing to load")
    doc = json.loads(blob)
    md = doc["metadata"]
    m = make_model(md["model_type"], md["task"], md["params"], md["columns"], md["classes"]).load_state(doc["state"])
    _CACHE[key] = (m, doc)
    return _CACHE[key]
