"""
The model registry: models, immutable versions, lifecycle.

ml_model           model_id, name, description, model_type, task, label kind,
                   feature_set, status (the ACTIVE version's state, or DRAFT),
                   active_version, owner, created/updated
ml_model_version   (model_id, version): feature_set name@version + content hash,
                   dataset_id + spec hash + snapshot hash, training config +
                   its hash, training period, artifact path + sha256, metrics,
                   status, trained_at, activated_at

Version states and allowed moves (TRANSITIONS):

    DRAFT -> TRAINING -> TRAINED -> VALIDATION -> APPROVED -> ACTIVE
    ACTIVE -> PAUSED -> ACTIVE ;  most states -> RETIRED -> ARCHIVED
    TRAINING -> FAILED (a training run that errored) -> RETIRED

A version does not become ACTIVE because it was trained: it must pass
VALIDATION and APPROVED first, each a recorded owner action (actor + reason in
ml_model_event). Only one version of a model is ACTIVE at a time; activating
a new one moves the previous ACTIVE version to PAUSED. RETIRED/ARCHIVED
versions keep their artifact (predictions still reference it).

Version ids are "v1", "v2", ... assigned in order; a version's identity is
(model_id, version) + the artifact hash.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

STATES = ("DRAFT", "TRAINING", "TRAINED", "FAILED", "VALIDATION", "APPROVED", "ACTIVE", "PAUSED", "RETIRED",
          "ARCHIVED")
_END = {"RETIRED"}
TRANSITIONS = {
    "DRAFT": {"TRAINING"} | _END,
    "TRAINING": {"TRAINED", "FAILED"},
    "TRAINED": {"VALIDATION"} | _END,
    "FAILED": _END,
    "VALIDATION": {"APPROVED", "TRAINED"} | _END,
    "APPROVED": {"ACTIVE", "VALIDATION"} | _END,
    "ACTIVE": {"PAUSED"} | _END,
    "PAUSED": {"ACTIVE"} | _END,
    "RETIRED": {"ARCHIVED"},
    "ARCHIVED": set(),
}
_ID = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


class ModelRegistryError(ValueError):
    pass


def _event(conn, model_id, version, event_type, frm=None, to=None, message="", details=None, actor="system"):
    conn.execute("INSERT INTO ml_model_event (model_id,version,event_type,from_state,to_state,message,details_json,"
                 "actor,at) VALUES (?,?,?,?,?,?,?,?,?)", (model_id, version, event_type, frm, to, message[:2000],
                                                          json.dumps(details or {}, default=str), actor, datetime.now()))


def create_model(conn, model_id, name, model_type, label_kind, feature_set, description="", owner="owner",
                 purpose="signal") -> dict:
    from ml.labels import KINDS
    from ml.models import MODEL_TYPES
    if not _ID.match(model_id or ""):
        raise ModelRegistryError("model_id: lowercase letters, digits, underscore (2-64)")
    if model_type not in MODEL_TYPES:
        raise ModelRegistryError(f"model_type must be one of {sorted(MODEL_TYPES)}")
    if label_kind not in KINDS:
        raise ModelRegistryError(f"label kind must be one of {sorted(KINDS)}")
    if purpose not in ("signal", "regime"):
        raise ModelRegistryError("purpose must be signal or regime")
    if conn.execute("SELECT 1 FROM ml_model WHERE model_id=?", (model_id,)).fetchone():
        raise ModelRegistryError(f"model {model_id} already exists")
    now = datetime.now()
    conn.execute("INSERT INTO ml_model (model_id,name,description,model_type,task,label_kind,feature_set,purpose,"
                 "status,owner,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (model_id, name, description, model_type, KINDS[label_kind], label_kind, feature_set, purpose,
                  "DRAFT", owner, now, now))
    _event(conn, model_id, None, "CREATED", None, "DRAFT", f"created {model_type}", actor=owner)
    conn.commit()
    return get_model(conn, model_id)


def next_version(conn, model_id) -> str:
    n = conn.execute("SELECT COUNT(*) FROM ml_model_version WHERE model_id=?", (model_id,)).fetchone()[0]
    return f"v{n + 1}"


def config_hash(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest()


def start_version(conn, model_id, dataset: dict, feature_set: dict, training_config: dict, actor="owner") -> str:
    m = get_model(conn, model_id)
    if not m:
        raise ModelRegistryError(f"no model {model_id}")
    v = next_version(conn, model_id)
    now = datetime.now()
    conn.execute("INSERT INTO ml_model_version (model_id,version,status,feature_set,feature_set_hash,dataset_id,"
                 "dataset_spec_hash,dataset_snapshot_hash,training_config_json,training_config_hash,created_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (model_id, v, "TRAINING", f"{feature_set['name']}@{feature_set['version']}",
                  feature_set["content_hash"], dataset["dataset_id"], dataset["spec_hash"],
                  dataset.get("snapshot_hash"), json.dumps(training_config, default=str),
                  config_hash(training_config), now))
    _event(conn, model_id, v, "LIFECYCLE", "DRAFT", "TRAINING", "training started", actor=actor)
    conn.commit()
    return v


def finish_version(conn, model_id, version, ok: bool, artifact=None, metrics=None, period=None, error=None):
    to = "TRAINED" if ok else "FAILED"
    conn.execute("UPDATE ml_model_version SET status=?, artifact_path=?, artifact_hash=?, metrics_json=?, "
                 "train_start=?, train_end=?, trained_at=?, error=? WHERE model_id=? AND version=?",
                 (to, artifact[0] if artifact else None, artifact[1] if artifact else None,
                  json.dumps(metrics, default=str) if metrics else None, (period or {}).get("start"),
                  (period or {}).get("end"), datetime.now(), error, model_id, version))
    _event(conn, model_id, version, "LIFECYCLE", "TRAINING", to, error or "training finished")
    conn.commit()


def transition(conn, model_id, version, to_state, reason="", actor="owner") -> dict:
    to_state = (to_state or "").upper()
    if to_state not in STATES:
        raise ModelRegistryError(f"state must be one of {STATES}")
    r = conn.execute("SELECT status, artifact_hash FROM ml_model_version WHERE model_id=? AND version=?",
                     (model_id, version)).fetchone()
    if not r:
        raise ModelRegistryError(f"no version {version} of model {model_id}")
    frm = r[0]
    if to_state not in TRANSITIONS[frm]:
        raise ModelRegistryError(f"{frm} -> {to_state} is not allowed; from {frm}: {sorted(TRANSITIONS[frm]) or 'none'}")
    if to_state in ("VALIDATION", "APPROVED", "ACTIVE") and not r[1]:
        raise ModelRegistryError(f"version {version} has no trained artifact")
    if to_state in ("APPROVED", "ACTIVE"):                  # W36 (ML-02): deep learning only with validated benefit
        mt = conn.execute("SELECT m.model_type, v.dataset_id FROM ml_model m JOIN ml_model_version v ON "
                          "v.model_id=m.model_id WHERE m.model_id=? AND v.version=?", (model_id, version)).fetchone()
        if mt and mt[0] == "neural_network":
            from ml.deep import allowed_to_activate
            ok, why = allowed_to_activate(conn, mt[1])
            if not ok:
                raise ModelRegistryError(f"neural_network {version} cannot be {to_state}: {why}")
    now = datetime.now()
    if to_state == "ACTIVE":
        prev = conn.execute("SELECT version FROM ml_model_version WHERE model_id=? AND status='ACTIVE'",
                            (model_id,)).fetchone()
        if prev and prev[0] != version:
            conn.execute("UPDATE ml_model_version SET status='PAUSED' WHERE model_id=? AND version=?",
                         (model_id, prev[0]))
            _event(conn, model_id, prev[0], "LIFECYCLE", "ACTIVE", "PAUSED", f"superseded by {version}", actor=actor)
        conn.execute("UPDATE ml_model_version SET activated_at=COALESCE(activated_at, ?) WHERE model_id=? AND version=?",
                     (now, model_id, version))
    conn.execute("UPDATE ml_model_version SET status=? WHERE model_id=? AND version=?", (to_state, model_id, version))
    active = conn.execute("SELECT version FROM ml_model_version WHERE model_id=? AND status='ACTIVE'",
                          (model_id,)).fetchone()
    conn.execute("UPDATE ml_model SET active_version=?, status=?, updated_at=? WHERE model_id=?",
                 (active[0] if active else None, "ACTIVE" if active else to_state, now, model_id))
    _event(conn, model_id, version, "LIFECYCLE", frm, to_state, reason, actor=actor)
    conn.commit()
    return get_version(conn, model_id, version)


def get_model(conn, model_id) -> dict | None:
    r = conn.execute("SELECT * FROM ml_model WHERE model_id=?", (model_id,)).fetchone()
    return dict(r) if r else None


def list_models(conn) -> list:
    return [dict(r) for r in conn.execute("SELECT * FROM ml_model ORDER BY model_id")]


def get_version(conn, model_id, version=None) -> dict | None:
    if version is None:
        r = conn.execute("SELECT * FROM ml_model_version WHERE model_id=? AND status='ACTIVE'", (model_id,)).fetchone()
    else:
        r = conn.execute("SELECT * FROM ml_model_version WHERE model_id=? AND version=?", (model_id, version)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["training_config"] = json.loads(d.pop("training_config_json") or "{}")
    d["metrics"] = json.loads(d.pop("metrics_json") or "null")
    return d


def list_versions(conn, model_id) -> list:
    return [get_version(conn, model_id, r[0]) for r in conn.execute(
        "SELECT version FROM ml_model_version WHERE model_id=? ORDER BY created_at", (model_id,))]


def active_version(conn, model_id) -> dict | None:
    return get_version(conn, model_id, None)


def events(conn, model_id, limit=100) -> list:
    return [dict(r) for r in conn.execute("SELECT * FROM ml_model_event WHERE model_id=? ORDER BY id DESC LIMIT ?",
                                          (model_id, int(limit)))]
