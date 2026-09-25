"""
Quant experiments (QR-12): a recorded hypothesis tied to runs of the EXISTING
research tools -- no second backtester.

quant_experiment: experiment_id, name, hypothesis, universe, factor_set / composite,
strategy (W3 strategy_id@version), parameters, period (start, end), frequency,
benchmark, version, status (DRAFT / RUNNING / COMPLETED / FAILED), config hash,
linked backtest run ids (W2 backtest_run) and research result ids.

run(conn, experiment_id) submits a W2 backtest of the named W3 strategy version
over the period (backtest.service.create + execute -- the same path as
POST /api/strategies/{id}/backtest), records the run id, and marks the
experiment COMPLETED / FAILED from the run's status. It reports the run's stored
metrics; it does not judge them.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime

KEYS = ("name", "hypothesis", "universe", "factor_set", "composite", "strategy_id", "strategy_version",
        "parameters", "start", "end", "frequency", "benchmark", "version", "notes")


def create(conn, spec: dict) -> dict:
    unknown = set(spec) - set(KEYS)
    if unknown:
        raise ValueError(f"unknown experiment keys: {sorted(unknown)}")
    if not re.match(r"^[a-z][a-z0-9_]{2,63}$", spec.get("name") or ""):
        raise ValueError("experiment name: lowercase letters, digits, underscore")
    for k in ("hypothesis", "start", "end"):
        if not spec.get(k):
            raise ValueError(f"{k} is required")
    cfg = {k: spec.get(k) for k in KEYS if k not in ("notes",)}
    cfg.setdefault("frequency", "daily")
    cfg["benchmark"] = cfg.get("benchmark") or "NIFTY50"
    cfg["version"] = str(cfg.get("version") or "1")
    h = hashlib.sha256(json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest()
    eid = "QX" + uuid.uuid4().hex[:14].upper()
    conn.execute("INSERT INTO quant_experiment (experiment_id,name,version,hypothesis,config_json,config_hash,status,"
                 "backtest_run_ids_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (eid, cfg["name"], cfg["version"], cfg["hypothesis"], json.dumps(cfg, default=str), h, "DRAFT", "[]",
                  datetime.now(), datetime.now()))
    conn.commit()
    return get(conn, eid)


def get(conn, eid) -> dict | None:
    r = conn.execute("SELECT * FROM quant_experiment WHERE experiment_id=?", (eid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["config"] = json.loads(d.pop("config_json"))
    d["backtest_run_ids"] = json.loads(d.pop("backtest_run_ids_json") or "[]")
    return d


def run(conn, eid) -> dict:
    """Submit and execute the W2 backtest synchronously (call from a background thread)."""
    from backtest import service
    e = get(conn, eid)
    if not e:
        raise ValueError(f"no experiment {eid}")
    c = e["config"]
    if not c.get("strategy_id"):
        raise ValueError("experiment has no strategy_id to backtest")
    req = {"strategy_id": c["strategy_id"], "start": c["start"], "end": c["end"],
           "notes": f"quant experiment {eid}: {c['hypothesis'][:200]}"}
    if c.get("strategy_version"):
        req["strategy_version"] = c["strategy_version"]
    if c.get("parameters"):
        req["params"] = c["parameters"]
    if isinstance(c.get("universe"), list):
        req["universe"] = c["universe"]
    conn.execute("UPDATE quant_experiment SET status='RUNNING', updated_at=? WHERE experiment_id=?",
                 (datetime.now(), eid)); conn.commit()
    try:
        run_id = service.create(req)
        runs = e["backtest_run_ids"] + [run_id]
        conn.execute("UPDATE quant_experiment SET backtest_run_ids_json=? WHERE experiment_id=?",
                     (json.dumps(runs), eid)); conn.commit()
        service.execute(run_id)
        st = conn.execute("SELECT status FROM backtest_run WHERE run_id=?", (run_id,)).fetchone()
        status = "COMPLETED" if st and st[0] == "COMPLETED" else "FAILED"
    except Exception as ex:
        status = "FAILED"
        conn.execute("UPDATE quant_experiment SET error=? WHERE experiment_id=?", (str(ex)[:2000], eid))
    conn.execute("UPDATE quant_experiment SET status=?, updated_at=? WHERE experiment_id=?",
                 (status, datetime.now(), eid)); conn.commit()
    return get(conn, eid)
