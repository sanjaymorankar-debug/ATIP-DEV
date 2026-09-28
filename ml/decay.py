"""
Drift alerts (W24, ML-09) and model / factor decay monitoring (MON-05).

model_health(conn)  for every model version that is PAPER / READY / ACTIVE (or has
    stored predictions):
        drift       latest ml_model_monitoring batch: columns with PSI > 0.25 (the W5
                    "shift" rule); WARNING at 1+, ALERT when >= 25% of columns shift
        decay       realised metrics of matured predictions (ml_model_metrics kind
                    "realised") vs the version's training-validation metrics:
                    accuracy (classification) or correlation (regression) falling by more
                    than DECAY_DROP -> ALERT
factor_decay(conn)  for every factor with a stored IC run (quant_factor_research kind
    "ic", by_date): the mean IC of the most recent `recent` dated ICs vs the mean of the
    ones before. A sign flip, or a fall below half of the earlier IC -> DECAYING.
run(conn, notify=True)   both, stored in ml_health_check; ALERT items go to the alert log
    (alerts.telegram.notify, de-duplicated per day).
run_scheduled(td)        post-market; SKIPPED unless ml.enabled or quant.enabled.
The W8 monitor gains the rule `ml_model_health` reading the latest check.
"""

from __future__ import annotations

import json
from datetime import datetime

DECAY_DROP = 0.10          # 10 percentage points of accuracy / 0.10 of correlation
SHIFT_ALERT_SHARE = 0.25


def model_health(conn) -> list:
    out = []
    vers = conn.execute("SELECT model_id, version, status, metrics_json FROM ml_model_version WHERE status IN "
                        "('PAPER','READY','ACTIVE','VALIDATED','APPROVED') OR EXISTS (SELECT 1 FROM ml_prediction p "
                        "WHERE p.model_id=ml_model_version.model_id AND p.model_version=ml_model_version.version)"
                        ).fetchall()
    for mid, ver, st, mj in vers:
        item = {"model_id": mid, "version": ver, "status": st, "level": "OK", "issues": []}
        mon = conn.execute("SELECT as_of, feature_drift_json, n_shifted FROM ml_model_monitoring WHERE model_id=? AND "
                           "version=? ORDER BY as_of DESC LIMIT 1", (mid, ver)).fetchone()
        if mon:
            drift = json.loads(mon[1] or "{}")
            shifted = [c for c, d in drift.items() if d.get("shift")]
            item["drift"] = {"as_of": str(mon[0])[:10], "shifted": shifted, "columns": len(drift)}
            if shifted:
                share = len(shifted) / max(1, len(drift))
                item["level"] = "ALERT" if share >= SHIFT_ALERT_SHARE else "WARNING"
                item["issues"].append(f"{len(shifted)}/{len(drift)} input columns shifted (PSI > 0.25): "
                                      f"{', '.join(shifted[:5])}")
        real = conn.execute("SELECT metrics_json, created_at FROM ml_model_metrics WHERE model_id=? AND version=? AND "
                            "kind='realised' ORDER BY created_at DESC LIMIT 1", (mid, ver)).fetchone()
        train = json.loads(mj or "{}").get("validation") or {}
        if real:
            r = json.loads(real[0] or "{}")
            for key in ("accuracy", "correlation"):
                if r.get(key) is not None and train.get(key) is not None:
                    drop = train[key] - r[key]
                    item.setdefault("decay", {})[key] = {"training_validation": train[key], "realised": r[key],
                                                         "drop": round(drop, 4)}
                    if drop > DECAY_DROP:
                        item["level"] = "ALERT"
                        item["issues"].append(f"realised {key} {r[key]} vs {train[key]} at validation")
        out.append(item)
    return out


def factor_decay(conn, recent: int = 20) -> list:
    out = []
    keys = [r[0] for r in conn.execute("SELECT DISTINCT factor_key FROM quant_factor_research WHERE kind='ic' AND "
                                       "factor_key NOT LIKE '\\_\\_%' ESCAPE '\\'")]
    for k in keys:
        r = conn.execute("SELECT result_json FROM quant_factor_research WHERE factor_key=? AND kind='ic' ORDER BY "
                         "created_at DESC LIMIT 1", (k,)).fetchone()
        by = sorted((json.loads(r[0] or "{}").get("by_date") or {}).items())
        vals = [v for _, v in by if v is not None]
        if len(vals) < recent + 10:
            out.append({"factor_key": k, "status": "INSUFFICIENT_HISTORY", "dates": len(vals)})
            continue
        old, new = vals[:-recent], vals[-recent:]
        mo, mn = sum(old) / len(old), sum(new) / len(new)
        flip = (mo > 0) != (mn > 0) and abs(mo) > 0.005
        fade = abs(mn) < 0.5 * abs(mo)
        out.append({"factor_key": k, "earlier_mean_ic": round(mo, 4), "recent_mean_ic": round(mn, 4),
                    "status": "DECAYING" if (flip or fade) else "STABLE",
                    "reason": "sign flip" if flip else ("recent IC below half of earlier" if fade else None)})
    return out


def run(conn, notify: bool = True) -> dict:
    models = model_health(conn)
    factors = factor_decay(conn)
    alerts = [m for m in models if m["level"] == "ALERT"] + [f for f in factors if f.get("status") == "DECAYING"]
    res = {"checked_at": str(datetime.now())[:19], "models": models, "factors": factors,
           "alerts": len(alerts), "status": "ALERT" if alerts else "OK"}
    conn.execute("INSERT INTO ml_health_check (checked_at,status,result_json) VALUES (?,?,?)",
                 (datetime.now(), res["status"], json.dumps(res, default=str)))
    conn.commit()
    if notify and alerts:
        try:
            from alerts.telegram import notify as _n
            lines = [f"{a.get('model_id') or a.get('factor_key')}: "
                     f"{'; '.join(a.get('issues') or []) or a.get('reason')}" for a in alerts][:10]
            _n("<b>ATIP model / factor health</b>\n" + "\n".join(lines), category="ml", severity="warning",
               key="ml_health")
        except Exception:
            pass
    return res


def latest(conn) -> dict | None:
    r = conn.execute("SELECT result_json FROM ml_health_check ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(r[0]) if r else None


def run_scheduled(trade_date=None) -> dict:
    from db.schema import get_connection
    from ml.config import settings as ml_settings
    try:
        from quant.config import settings as q_settings
        q_on = q_settings()["enabled"]
    except Exception:
        q_on = False
    if not (ml_settings()["enabled"] or q_on):
        return {"status": "SKIPPED", "rows": 0, "reason": "ml.enabled and quant.enabled are false"}
    conn = get_connection()
    try:
        r = run(conn)
    finally:
        conn.close()
    return {"status": "SUCCESS", "rows": len(r["models"]) + len(r["factors"]), "alerts": r["alerts"]}
