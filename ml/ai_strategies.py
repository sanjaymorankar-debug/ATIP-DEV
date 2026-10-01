"""
AI-driven strategies: readiness and activation (W28: SE-05).

The two ML strategies (strategy_engine/library/50_ml_direction, 51_atip_ml_blend) and
the W4 ml_model_active risk gate were built in W5/W24, but they produce nothing until
a model is ACTIVE and ml.default_model names it -- and nothing showed the owner how
far from that they were. This module answers it and performs the switch-on once the
owner asks.

readiness(conn) -> {"ready": bool, "checks": [{name, ok, detail}], "strategies": [...]}
    default_model     ml.default_model is set and the model exists
    active_version    that model has an ACTIVE version
    out_of_sample     the ACTIVE version's training dataset ends before the latest
                      prediction (else every prediction is in-sample and is never served)
    fresh_predictions predictions exist for the last scored session
    scheduled         ml.enabled is true, so predictions keep being made
    health            ml/decay.model_health is not ALERT for that version
    strategies        each AI strategy's lifecycle state (decisions only in PAPER/READY/ACTIVE)

activate(conn, model_id, reason, actor) -- owner action, behind the dashboard token:
    1. requires the model to have an ACTIVE version (never promotes a model itself)
    2. writes ml.default_model = model_id and ml.enabled = true to config.json
       (a timestamped copy of the previous file is kept beside it)
    3. moves each AI strategy to PAPER when its lifecycle allows that from where it is;
       otherwise reports the state it is in and the step the owner still has to take
       (e.g. DRAFT -> BACKTEST -> VALIDATION -> APPROVED needs evidence first)
It never moves anything to READY / ACTIVE and never enables live trading: PAPER
decisions still go through the W4 risk engine to the paper book only.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime

AI_STRATEGIES = ("ml_direction", "atip_ml_blend")


def _check(name, ok, detail):
    return {"name": name, "ok": bool(ok), "detail": detail}


def readiness(conn) -> dict:
    from ml.config import settings
    from ml import registry
    s = settings()
    checks = []
    mid = s.get("default_model")
    model = registry.get_model(conn, mid) if mid else None
    checks.append(_check("default_model", model is not None,
                         f"ml.default_model = {mid}" if model else
                         ("ml.default_model is not set" if not mid else f"no model {mid}")))
    act = registry.active_version(conn, mid) if model else None
    checks.append(_check("active_version", act is not None,
                         f"{mid} {act['version']} is ACTIVE" if act else "no ACTIVE version"))
    last_pred = None
    if act:
        end = conn.execute("SELECT d.end_date FROM ml_model_version v JOIN ml_dataset d ON d.dataset_id=v.dataset_id "
                           "WHERE v.model_id=? AND v.version=?", (mid, act["version"])).fetchone()
        last_pred = conn.execute("SELECT MAX(as_of) FROM ml_prediction WHERE model_id=? AND model_version=?",
                                 (mid, act["version"])).fetchone()[0]
        oos = bool(end and end[0] and last_pred and str(last_pred)[:10] > str(end[0])[:10])
        checks.append(_check("out_of_sample", oos,
                             f"training ends {end[0] if end else '?'}, latest prediction {last_pred or 'none'}"))
    else:
        checks.append(_check("out_of_sample", False, "no ACTIVE version"))
    last_session = conn.execute("SELECT MAX(date) FROM ai_scores").fetchone()[0]
    fresh = bool(last_pred and last_session and str(last_pred)[:10] >= str(last_session)[:10])
    checks.append(_check("fresh_predictions", fresh,
                         f"latest prediction {last_pred or 'none'}, last scored session {last_session or 'none'}"))
    checks.append(_check("scheduled", s.get("enabled"), "ml.enabled is " + ("true" if s.get("enabled") else "false")))
    level = None
    if act:
        try:
            from ml.decay import model_health
            h = next((x for x in model_health(conn) if x["model_id"] == mid and x["version"] == act["version"]), None)
            level = h["level"] if h else "OK"
            detail = "; ".join(h["issues"]) if h and h["issues"] else f"health {level}"
        except Exception as e:
            detail = f"health check unavailable: {e}"
    else:
        detail = "no ACTIVE version"
    checks.append(_check("health", level in ("OK", "WARNING"), detail))
    strategies = []
    for sid in AI_STRATEGIES:
        r = conn.execute("SELECT status, current_version FROM strategy WHERE strategy_id=?", (sid,)).fetchone()
        strategies.append({"strategy_id": sid, "status": r[0] if r else "NOT_LOADED",
                           "version": r[1] if r else None,
                           "deciding": bool(r and r[0] in ("PAPER", "READY", "ACTIVE"))})
    ready = all(c["ok"] for c in checks)
    return {"ready": ready, "checks": checks, "strategies": strategies,
            "note": "Ready means the ML features will be populated; each strategy still needs PAPER/READY/ACTIVE "
                    "to make decisions, and every decision still passes the W4 risk engine."}


def _write_ml_config(updates: dict) -> str:
    from ml.config import CONFIG_PATH
    CONFIG_PATH.parent.mkdir(exist_ok=True)
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        cfg = {}
    backup = ""
    if CONFIG_PATH.exists():
        backup = str(CONFIG_PATH.with_name(f"config.json.bak-{datetime.now():%Y%m%d-%H%M%S}"))
        shutil.copy2(CONFIG_PATH, backup)
    ml = dict(cfg.get("ml") or {})
    ml.update(updates)
    cfg["ml"] = ml
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)
    return backup


def activate(conn, model_id: str, reason: str = "", actor: str = "owner") -> dict:
    from ml import registry
    from strategy_engine import lifecycle
    if not registry.get_model(conn, model_id):
        raise LookupError(f"no model {model_id}")
    act = registry.active_version(conn, model_id)
    if not act:
        raise ValueError(f"{model_id} has no ACTIVE version -- move one to ACTIVE on /ml first")
    backup = _write_ml_config({"default_model": model_id, "enabled": True})
    results = []
    for sid in AI_STRATEGIES:
        r = conn.execute("SELECT status FROM strategy WHERE strategy_id=?", (sid,)).fetchone()
        if not r:
            results.append({"strategy_id": sid, "result": "NOT_LOADED"})
            continue
        st = r[0]
        if st in ("PAPER", "READY", "ACTIVE"):
            results.append({"strategy_id": sid, "result": "ALREADY_DECIDING", "status": st})
            continue
        if "PAPER" in lifecycle.TRANSITIONS.get(st, set()):
            try:
                t = lifecycle.transition(conn, sid, "PAPER", reason or f"SE-05: ML model {model_id} {act['version']} "
                                         f"is ACTIVE", {"model_id": model_id, "version": act["version"]}, actor)
                results.append({"strategy_id": sid, "result": "PAPER", "from": t["from"]})
            except lifecycle.LifecycleError as e:
                results.append({"strategy_id": sid, "result": "NEEDS_EVIDENCE", "status": st, "detail": str(e)})
        else:
            results.append({"strategy_id": sid, "result": "NEEDS_STEPS", "status": st,
                            "detail": f"{st} -> PAPER is not a lifecycle step; allowed from {st}: "
                                      f"{sorted(lifecycle.TRANSITIONS.get(st, set()))}"})
    return {"model_id": model_id, "version": act["version"], "config_backup": backup, "strategies": results,
            "readiness": readiness(conn)}
