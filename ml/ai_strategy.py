"""
AI-driven strategy readiness and guarded activation (SE-05), W28.

The ML strategies (strategy_engine/library 50_ml_direction, 51_atip_ml_blend, and any
other strategy whose rules read an ml_* feature) produce nothing until a model is
ACTIVE -- and nothing said how far each one was from that. readiness() walks the whole
chain for every ML strategy and reports the FIRST blocking gate, so the path to a live
AI strategy is explicit:

    1  ml_enabled        config ml.enabled (scheduled predictions run)
    2  default_model     config ml.default_model names a model (strategies read the feature,
                         not a model id)
    3  model_exists      that model is registered
    4  active_version    it has an ACTIVE version (ml/registry lifecycle)
    5  validation        the latest walk-forward validation report for its model type is not
                         NO_EDGE (W24 ml_validation_report.verdict)
    6  predictions       predictions from the ACTIVE version exist for the latest session
    7  backtest          the strategy's current version has a COMPLETED backtest
                         (what strategy_engine.lifecycle demands for ACTIVE)
    8  risk_gate         W4 risk check ml_model_active blocks non-ACTIVE models (always on)

activate(conn, strategy_id, to_state='PAPER', actor) moves an ML strategy forward only
when gates 1-7 pass -- PAPER (the default) generates decisions against the PAPER book;
ACTIVE is refused here unless the strategy has already run in PAPER. Everything goes
through strategy_engine.lifecycle.transition, so its evidence checks and event log apply.
"""

from __future__ import annotations

import json


def ml_strategies(conn) -> list:
    out = []
    for sid, status, ver in conn.execute("SELECT strategy_id, status, current_version FROM strategy"):
        r = conn.execute("SELECT definition_json FROM strategy_version WHERE strategy_id=? AND version=?",
                         (sid, ver)).fetchone()
        text = r[0] if r else ""
        feats = sorted(set(f for f in _features(text) if f.startswith("ml_")))
        if feats:
            out.append({"strategy_id": sid, "status": status, "version": ver, "ml_features": feats})
    return out


def _features(definition_json: str) -> list:
    try:
        d = json.loads(definition_json or "{}")
    except ValueError:
        return []
    found = []

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in ("feature", "left", "right") and isinstance(v, str):
                    found.append(v)
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(d)
    return found


def _gates(conn, strategy: dict) -> list:
    from ml.config import settings as ml_settings
    s = ml_settings()
    g = []

    def add(name, ok, msg):
        g.append({"gate": name, "ok": bool(ok), "detail": msg})
    add("ml_enabled", s.get("enabled"), "ml.enabled is true" if s.get("enabled") else
        "set config.json ml.enabled = true (scheduled predictions + monitoring)")
    mid = s.get("default_model")
    add("default_model", mid, f"ml.default_model = {mid}" if mid else "set config.json ml.default_model to a model_id")
    m = conn.execute("SELECT model_id, model_type, status, active_version FROM ml_model WHERE model_id=?",
                     (mid,)).fetchone() if mid else None
    add("model_exists", m, f"model {mid} registered ({m[1]})" if m else "no such model registered -- train one "
        "(python -m ml train ... / POST /api/ml/models)")
    av = m[3] if m else None
    add("active_version", av, f"ACTIVE version {av}" if av else "no ACTIVE version -- validate and activate one "
        "(the W24 models showed NO_EDGE; none is active)")
    rep = conn.execute("SELECT verdict, created_at FROM ml_validation_report WHERE model_type=? ORDER BY created_at "
                       "DESC LIMIT 1", (m[1],)).fetchone() if m else None
    add("validation", rep and rep[0] != "NO_EDGE",
        f"latest walk-forward verdict {rep[0]} ({str(rep[1])[:10]})" if rep else
        "no walk-forward validation report for this model type (POST /api/ml/validation)")
    pred = conn.execute("SELECT MAX(as_of), COUNT(*) FROM ml_prediction WHERE model_id=? AND model_version=? AND "
                        "version_status='ACTIVE'", (mid, av)).fetchone() if av else None
    last_scored = conn.execute("SELECT MAX(date) FROM ai_scores").fetchone()[0]
    fresh = bool(pred and pred[0] and str(pred[0])[:10] >= str(last_scored)[:10])
    add("predictions", fresh, f"{pred[1]} ACTIVE predictions, latest {str(pred[0])[:10]}" if pred and pred[0] else
        "no predictions from the ACTIVE version yet (they run post-market once ml.enabled)")
    bt = conn.execute("SELECT run_id FROM backtest_run WHERE strategy_id=? AND strategy_version=? AND "
                      "status='COMPLETED' ORDER BY finished_at DESC LIMIT 1",
                      (strategy["strategy_id"], strategy["version"])).fetchone()
    add("backtest", bt, f"backtest {bt[0]}" if bt else f"no completed backtest of {strategy['strategy_id']} "
        f"{strategy['version']} (needs the model's history of ACTIVE predictions)")
    add("risk_gate", True, "W4 ml_model_active blocks any decision from a non-ACTIVE model (always on)")
    return g


def readiness(conn) -> dict:
    rows = []
    for st in ml_strategies(conn):
        gates = _gates(conn, st)
        blocking = next((x for x in gates if not x["ok"]), None)
        rows.append({**st, "ready": blocking is None, "blocked_at": blocking["gate"] if blocking else None,
                     "next_step": blocking["detail"] if blocking else
                     ("eligible for PAPER" if st["status"] not in ("PAPER", "READY", "ACTIVE") else "running"),
                     "gates": gates})
    return {"strategies": rows, "ready": sum(1 for r in rows if r["ready"]), "total": len(rows)}


def activate(conn, strategy_id: str, to_state: str = "PAPER", actor: str = "owner", reason: str = "") -> dict:
    from strategy_engine.lifecycle import transition
    to_state = (to_state or "PAPER").upper()
    if to_state not in ("PAPER", "ACTIVE"):
        raise ValueError("to_state must be PAPER or ACTIVE")
    st = next((s for s in ml_strategies(conn) if s["strategy_id"] == strategy_id), None)
    if not st:
        raise LookupError(f"{strategy_id} is not an ML strategy (no ml_* feature in its rules)")
    gates = _gates(conn, st)
    failed = [x for x in gates if not x["ok"]]
    if failed:
        raise ValueError(f"not ready: {failed[0]['gate']} -- {failed[0]['detail']}")
    if to_state == "ACTIVE" and st["status"] != "PAPER":
        raise ValueError("an ML strategy goes to ACTIVE only after running in PAPER")
    return transition(conn, strategy_id, to_state, reason or "SE-05 guarded activation (all ML gates passed)",
                      {"ml_gates": [x["gate"] for x in gates]}, actor)
