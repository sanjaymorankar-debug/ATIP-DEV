"""
The training pipeline.

    dataset spec -> build (point in time) -> snapshot (.npz + hash)
      -> feature selection (drop columns missing in > max_missing of TRAIN rows
         or constant on TRAIN rows; recorded)
      -> label / data validation (enough rows, >= 2 classes, finite targets)
      -> time split (chronological, purged + embargo; dataset.time_split)
      -> fit on TRAIN rows only (preprocessing fitted on TRAIN rows only)
      -> descriptive metrics on train and validation rows
      -> artifact (immutable, hashed) -> model registry version TRAINED
    ml_training_run records every step's outcome, including failures.

train() is a framework entry point. W5 did not run it to produce or evaluate
a model; metrics it records are descriptive and are not a validation claim.
"""

from __future__ import annotations

import json
import logging
import traceback
import uuid
from datetime import datetime

import numpy as np

from ml import artifacts, dataset as DS, registry as REG
from ml.config import model_root, settings
from ml.feature_registry import get_feature_set
from ml.labels import LabelSpec
from ml.models import make_model, metrics

log = logging.getLogger("atip.ml")


def _run(conn, run_id, **f):
    sets = ", ".join(f"{k}=?" for k in f)
    conn.execute(f"UPDATE ml_training_run SET {sets} WHERE run_id=?", list(f.values()) + [run_id])
    conn.commit()


def train(conn, model_id: str, dataset_spec: dict, params: dict | None = None, validation_fraction=None,
          embargo=None, max_missing: float = 0.5, actor: str = "owner") -> dict:
    m = REG.get_model(conn, model_id)
    if not m:
        raise REG.ModelRegistryError(f"no model {model_id}")
    cfg = settings()["training"]
    spec = DS.DatasetSpec.from_dict(dataset_spec)
    if spec.feature_set != m["feature_set"]:
        raise ValueError(f"dataset feature set {spec.feature_set} != model feature set {m['feature_set']}")
    lab = LabelSpec.from_dict(spec.label)
    if lab.kind != m["label_kind"]:
        raise ValueError(f"dataset label {lab.kind} != model label {m['label_kind']}")
    fs_name, fs_ver = spec.feature_set.split("@", 1)
    fs = get_feature_set(conn, fs_name, fs_ver)
    vf = float(validation_fraction if validation_fraction is not None else cfg["validation_fraction"])
    emb = embargo if embargo is not None else cfg["embargo_sessions"]
    tcfg = {"model_type": m["model_type"], "params": params or {}, "validation_fraction": vf, "embargo": emb,
            "max_missing": max_missing, "label": lab.as_dict(), "dataset_spec_hash": spec.spec_hash,
            "feature_set_hash": fs["content_hash"]}
    run_id = "TR" + uuid.uuid4().hex[:14].upper()
    conn.execute("INSERT INTO ml_training_run (run_id,model_id,dataset_id,status,config_json,started_at,actor) "
                 "VALUES (?,?,?,?,?,?,?)", (run_id, model_id, spec.dataset_id, "RUNNING", json.dumps(tcfg, default=str),
                                            datetime.now(), actor))
    conn.commit()
    version = None
    try:
        ds = DS.build(conn, spec)
        snap = DS.save_snapshot(ds, model_root())
        dsrow = DS.register(conn, spec, ds, snap)
        if len(ds.y) < int(cfg["min_rows"]):
            raise ValueError(f"dataset has {len(ds.y)} rows < min_rows {cfg['min_rows']}")
        tr, va, split = DS.time_split(ds, vf, emb)
        if len(tr) == 0:
            raise ValueError("no training rows after the time split")
        Xtr = ds.X[tr]
        miss = np.isnan(Xtr).mean(axis=0)
        const = np.nanstd(np.where(np.isnan(Xtr), np.nanmean(Xtr, axis=0), Xtr), axis=0) < 1e-12
        keep = [i for i in range(ds.X.shape[1]) if miss[i] <= max_missing and not const[i]]
        dropped = [ds.columns[i] for i in range(ds.X.shape[1]) if i not in keep]
        if not keep:
            raise ValueError("feature selection left no columns")
        cols = [ds.columns[i] for i in keep]
        y = np.array(ds.y, dtype=object)
        if lab.task == "classification" and len(set(map(str, y[tr]))) < 2:
            raise ValueError("training labels have a single class")
        if lab.task == "regression" and not np.all(np.isfinite(np.asarray(y[tr], dtype=float))):
            raise ValueError("non-finite regression targets")
        version = REG.start_version(conn, model_id, dsrow, fs, tcfg, actor)
        _run(conn, run_id, version=version)
        model = make_model(m["model_type"], lab.task, params, cols, lab.classes)
        model.fit(ds.X[tr][:, keep], list(y[tr]))
        out = {}
        for part, idx in (("train", tr), ("validation", va)):
            if len(idx):
                Xp = ds.X[idx][:, keep]
                proba = model.predict_proba(Xp) if lab.task == "classification" else None
                out[part] = metrics(lab.task, list(y[idx]), model.predict(Xp),
                                    None if proba is None else proba.tolist(), model.classes)
        Xk = ds.X[tr][:, keep]
        ref = {"columns": cols, "mean": np.nanmean(Xk, axis=0).tolist(), "std": np.nanstd(Xk, axis=0).tolist(),
               "missing": np.isnan(Xk).mean(axis=0).tolist(),
               "quantiles": np.nanpercentile(Xk, [10, 25, 50, 75, 90], axis=0).tolist()}
        if lab.task == "classification":
            p = model.predict_proba(Xk)
            ref["prediction_mean"] = p.mean(axis=0).tolist()
        else:
            ref["prediction_mean"] = float(np.mean(model.predict(Xk)))
        period = {"start": str(min(ds.dates[i] for i in tr)), "end": split["train_end"]}
        art = artifacts.save(model, model_id, version, {
            "feature_set": fs, "feature_names": ds.feature_names, "dataset": {k: dsrow[k] for k in (
                "dataset_id", "spec_hash", "snapshot_path", "snapshot_hash")}, "training_config": tcfg,
            "split": split, "selected_columns": cols, "dropped_columns": dropped, "all_columns": ds.columns,
            "reference_stats": ref, "label": lab.as_dict(), "training_period": period})
        allm = {**out, "split": split, "dropped_columns": dropped, "note": "descriptive only; not a validation"}
        REG.finish_version(conn, model_id, version, True, art, allm, period)
        imp = model.importances()
        if imp:
            conn.execute("INSERT INTO ml_model_explanation (model_id,version,kind,explanation_version,payload_json,"
                         "created_at) VALUES (?,?,?,?,?,?)", (model_id, version, "global_importance", "1",
                                                              json.dumps(imp), datetime.now()))
        for part, mm in out.items():
            conn.execute("INSERT INTO ml_model_metrics (model_id,version,kind,period_start,period_end,metrics_json,"
                         "created_at) VALUES (?,?,?,?,?,?,?)", (model_id, version, f"training_{part}",
                                                                period["start"] if part == "train" else split["validation_start"],
                                                                period["end"] if part == "train" else spec.end,
                                                                json.dumps(mm), datetime.now()))
        _run(conn, run_id, status="COMPLETED", finished_at=datetime.now(), metrics_json=json.dumps(allm, default=str),
             rows=len(ds.y))
        return {"run_id": run_id, "model_id": model_id, "version": version, "status": "TRAINED", "metrics": allm,
                "artifact": art[0]}
    except Exception as e:
        log.error(f"  training {model_id}: {e}")
        if version:
            REG.finish_version(conn, model_id, version, False, error=str(e)[:2000])
        _run(conn, run_id, status="FAILED", finished_at=datetime.now(), error=f"{type(e).__name__}: {e}",
             traceback=traceback.format_exc()[-4000:])
        raise
