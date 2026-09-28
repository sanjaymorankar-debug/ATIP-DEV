"""
First models and the regime model (W24: ML-01, ML-05, ML-11).

    bootstrap(conn, start, end, families=("logistic_regression", "native_gbm", "ensemble"),
              label=None, feature_set="atip_core@1", sampling=None, n_windows=5, train=True)

For each family: create the model (id dir5_<family>) if it does not exist, run the
walk-forward validation report (validation.py) on one shared, point-in-time dataset,
and -- when train=True -- train a version on the whole period (status TRAINED).
Nothing is activated: DRAFT/TRAINED -> ACTIVE stays an explicit owner decision through
the W5 lifecycle (and a strategy only sees ml_* features from an ACTIVE model).

    regime(conn, start, end, family="native_gbm", horizon=5)

The ML regime model: market-level rows (NIFTY50 bars + market context), label
market_regime (the Market Health regime `horizon` sessions later), validated and
trained the same way (id regime_<family>, purpose regime). ml.regime_provider decides
whether it is ever used (W5 config); default deterministic.
"""

from __future__ import annotations

DEFAULT_LABEL = {"kind": "direction", "horizon": 5, "threshold_pct": 1.0}
FAMILIES = ("logistic_regression", "native_gbm", "native_random_forest", "ensemble")


def _ensure_model(conn, model_id, family, label_kind, feature_set, purpose):
    from ml import registry as REG
    m = REG.get_model(conn, model_id)
    if m:
        if m["model_type"] != family or m["feature_set"] != feature_set:
            raise ValueError(f"model {model_id} exists with a different type / feature set")
        return m
    return REG.create_model(conn, model_id, f"{family} {label_kind}", family, label_kind, feature_set,
                            description="W24 preset", purpose=purpose)


def bootstrap(conn, start, end, families=("logistic_regression", "native_gbm", "ensemble"), label=None,
              feature_set="atip_core@1", sampling=None, n_windows=5, train=True, prefix="dir5") -> dict:
    from ml import dataset as DS
    from ml.feature_registry import ensure_builtin_sets, sync_features
    from ml.training import train as train_model
    from ml.validation import walk_forward
    for f in families:
        if f not in FAMILIES:
            raise ValueError(f"family must be among {FAMILIES}")
    sync_features(conn)
    ensure_builtin_sets(conn)
    lab = label or DEFAULT_LABEL
    spec = {"name": f"{prefix}_{feature_set.split('@')[0]}", "version": f"{start}_{end}", "feature_set": feature_set,
            "label": lab, "start": str(start), "end": str(end),
            "sampling": sampling or {"every_n_sessions": 3, "max_symbols": 150}}
    ds = DS.build(conn, DS.DatasetSpec.from_dict(spec))
    out = {"dataset": {"rows": len(ds.y), "dates": len(set(ds.dates)), "columns": len(ds.columns)}, "models": []}
    for f in families:
        mid = f"{prefix}_{f.replace('_regression', '').replace('native_', '')}"
        _ensure_model(conn, mid, f, lab["kind"], feature_set, "signal")
        rep = walk_forward(conn, f, spec, None, n_windows, ds=ds)
        row = {"model_id": mid, "family": f, "validation_report": rep.get("report_id"), "verdict": rep["verdict"],
               "pooled": rep["pooled"]}
        if train:
            try:
                tr = train_model(conn, mid, spec)
                row.update({"version": tr["version"], "status": tr["status"]})
            except Exception as e:                       # the report stands even if training fails
                row["train_error"] = f"{type(e).__name__}: {e}"
        out["models"].append(row)
    return out


def regime(conn, start, end, family="native_gbm", horizon=5, feature_set="atip_technical@1", n_windows=5,
           train=True) -> dict:
    from ml import dataset as DS
    from ml.feature_registry import ensure_builtin_sets, sync_features
    from ml.training import train as train_model
    from ml.validation import walk_forward
    sync_features(conn)
    ensure_builtin_sets(conn)
    lab = {"kind": "market_regime", "horizon": int(horizon)}
    spec = {"name": "regime_market", "version": f"{start}_{end}_h{horizon}", "feature_set": feature_set,
            "label": lab, "start": str(start), "end": str(end), "universe": "market",
            "sampling": {"every_n_sessions": 1}}
    ds = DS.build(conn, DS.DatasetSpec.from_dict(spec))
    mid = f"regime_{family.replace('native_', '')}"
    _ensure_model(conn, mid, family, "market_regime", feature_set, "regime")
    out = {"model_id": mid, "dataset_rows": len(ds.y)}
    try:
        rep = walk_forward(conn, family, spec, None, n_windows, min_train_dates=60, ds=ds, perm_importance=False)
        out.update({"validation_report": rep.get("report_id"), "verdict": rep["verdict"], "pooled": rep["pooled"]})
    except ValueError as e:
        out["validation_error"] = str(e)
    if train:
        try:
            tr = train_model(conn, mid, spec)
            out.update({"version": tr["version"], "status": tr["status"]})
        except Exception as e:
            out["train_error"] = f"{type(e).__name__}: {e}"
    return out
