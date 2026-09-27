"""
Factor approval gate (W22, AF-08): a factor becomes APPROVED only on recorded
evidence, and (optionally) a strategy using an unapproved factor cannot be
promoted to APPROVED / ACTIVE.

evaluate(conn, factor_key) reads the latest stored research (quant/research.py,
quant_factor_research) and applies the CRITERIA:
    ic            |mean IC| >= 0.02 at the 5-session horizon over >= 60 dated ICs
    t_stat        |t| >= 2.0
    hit_rate      share of dates with IC in the factor's direction >= 0.52
    decay         IC keeps its sign at 10 sessions (ic_decay run present)
    redundancy    Spearman correlation with every APPROVED factor on the latest date
                  below 0.80 (a near-duplicate adds nothing)
The result is PASS / FAIL per criterion with the numbers. decide() records the
owner's decision (APPROVED / REJECTED / RETIRED) with that evidence in
quant_factor_approval (append-only history); the latest decision is the factor's
approval state. A decision of APPROVED against failing evidence needs an explicit
override reason, which is stored.

ENFORCEMENT: config quant.require_approved_factors (default false). When true,
strategy_engine/lifecycle.py refuses DRAFT/...->APPROVED and ->ACTIVE for a strategy
version whose features include a qf_<factor> that is not APPROVED.
"""

from __future__ import annotations

import json
from datetime import datetime

CRITERIA = {"min_abs_ic": 0.02, "min_dates": 60, "min_abs_t": 2.0, "min_hit_rate": 0.52, "max_corr": 0.80}
DECISIONS = ("APPROVED", "REJECTED", "RETIRED")


def _latest(conn, key, kind):
    r = conn.execute("SELECT result_json, created_at, start_date, end_date FROM quant_factor_research WHERE "
                     "factor_key=? AND kind=? ORDER BY created_at DESC LIMIT 1", (key, kind)).fetchone()
    if not r:
        return None
    d = json.loads(r[0] or "{}")
    d.update({"_at": str(r[1])[:19], "_period": f"{r[2]}..{r[3]}"})
    return d


def state(conn, factor_key) -> dict:
    r = conn.execute("SELECT decision, decided_at, decided_by, reason FROM quant_factor_approval WHERE factor_key=? "
                     "ORDER BY id DESC LIMIT 1", (factor_key,)).fetchone()
    return {"factor_key": factor_key, "state": r[0] if r else "UNREVIEWED",
            "decided_at": r[1] if r else None, "decided_by": r[2] if r else None, "reason": r[3] if r else None}


def approved_keys(conn) -> set:
    out = set()
    for (k,) in conn.execute("SELECT DISTINCT factor_key FROM quant_factor_approval"):
        if state(conn, k)["state"] == "APPROVED":
            out.add(k)
    return out


def _direction(conn, key):
    fid, _, ver = key.partition("@")
    r = conn.execute("SELECT direction FROM quant_factor WHERE factor_id=? AND version=?", (fid, ver or "1")).fetchone()
    return int(r[0]) if r and r[0] else 1


def evaluate(conn, factor_key: str) -> dict:
    c = CRITERIA
    ic = _latest(conn, factor_key, "ic")
    decay = _latest(conn, factor_key, "ic_decay")
    checks = []
    sign = _direction(conn, factor_key)

    def chk(name, ok, detail):
        checks.append({"criterion": name, "result": "PASS" if ok else ("NO_DATA" if ok is None else "FAIL"),
                       "detail": detail})
    if not ic or not ic.get("dates"):
        chk("ic", None, "no IC research stored: run python -m quant research KEY --kind ic")
    else:
        m, n = ic.get("mean_ic"), ic.get("dates", 0)
        chk("ic", n >= c["min_dates"] and m is not None and abs(m) >= c["min_abs_ic"],
            f"mean IC {m} over {n} dates (need |IC| >= {c['min_abs_ic']} over >= {c['min_dates']})")
        t = ic.get("t_stat")
        chk("t_stat", t is not None and abs(t) >= c["min_abs_t"], f"t = {t} (need |t| >= {c['min_abs_t']})")
        hr = ic.get("hit_rate")
        hr_dir = (hr if (m or 0) >= 0 else (1 - hr)) if hr is not None else None
        chk("hit_rate", hr_dir is not None and hr_dir >= c["min_hit_rate"],
            f"IC in the factor's sign on {hr_dir} of dates (need >= {c['min_hit_rate']})")
        if m is not None and (m > 0) != (sign > 0):
            checks.append({"criterion": "direction", "result": "FAIL",
                           "detail": f"mean IC {m} has the opposite sign to the factor's declared direction {sign}"})
    if not decay:
        chk("decay", None, "no IC-decay research stored (python -m quant research KEY --kind ic_decay)")
    else:
        by = decay.get("mean_ic_by_horizon") or {k: v for k, v in decay.items() if not k.startswith("_")}
        i5, i10 = by.get("5") if "5" in by else by.get(5), by.get("10") if "10" in by else by.get(10)
        chk("decay", i5 is not None and i10 is not None and (i5 > 0) == (i10 > 0),
            f"IC at 5 sessions {i5}, at 10 sessions {i10} (need the same sign)")
    ap = approved_keys(conn) - {factor_key}
    last = conn.execute("SELECT MAX(as_of) FROM quant_factor_score WHERE factor_key=?", (factor_key,)).fetchone()[0]
    if ap and last:
        from quant.research import factor_correlation
        corr = factor_correlation(conn, [factor_key] + sorted(ap), last)[factor_key]
        worst = max(((k, abs(v)) for k, v in corr.items() if k != factor_key and v is not None), key=lambda x: x[1],
                    default=(None, 0))
        chk("redundancy", worst[1] < c["max_corr"], f"highest |correlation| with an approved factor {worst[1]:.2f} "
                                                   f"({worst[0]}) on {str(last)[:10]} (need < {c['max_corr']})")
    else:
        chk("redundancy", True, "no other approved factor to compare with")
    verdict = "PASS" if all(x["result"] == "PASS" for x in checks) else \
        "INCOMPLETE" if any(x["result"] == "NO_DATA" for x in checks) else "FAIL"
    return {"factor_key": factor_key, "verdict": verdict, "checks": checks, "criteria": c,
            "evidence": {"ic": ic, "ic_decay": decay}, "current": state(conn, factor_key)}


def decide(conn, factor_key: str, decision: str, reason: str = "", actor: str = "owner") -> dict:
    d = str(decision or "").upper()
    if d not in DECISIONS:
        raise ValueError(f"decision must be one of {list(DECISIONS)}")
    fid, _, ver = factor_key.partition("@")
    if not conn.execute("SELECT 1 FROM quant_factor WHERE factor_id=? AND version=?", (fid, ver or "1")).fetchone():
        raise LookupError(f"unknown factor {factor_key}")
    ev = evaluate(conn, factor_key)
    if d == "APPROVED" and ev["verdict"] != "PASS" and not (reason or "").strip():
        raise ValueError(f"evidence verdict is {ev['verdict']}: approving needs an override reason")
    conn.execute("INSERT INTO quant_factor_approval (factor_key,decision,verdict,evidence_json,reason,decided_at,"
                 "decided_by) VALUES (?,?,?,?,?,?,?)", (factor_key, d, ev["verdict"], json.dumps(ev, default=str),
                                                        (reason or "")[:500], datetime.now(), actor))
    conn.commit()
    return {**state(conn, factor_key), "verdict": ev["verdict"]}


def history(conn, factor_key: str) -> list:
    return [dict(zip(("decision", "verdict", "reason", "decided_at", "decided_by"), r)) for r in conn.execute(
        "SELECT decision, verdict, reason, decided_at, decided_by FROM quant_factor_approval WHERE factor_key=? "
        "ORDER BY id DESC", (factor_key,))]


def required() -> bool:
    try:
        from quant.config import settings
        return settings().get("require_approved_factors") is True
    except Exception:
        return False


def strategy_gate(conn, strategy_id: str, version) -> tuple[bool, str | None]:
    """(ok, note) for lifecycle promotion: every qf_<factor> the version uses must be APPROVED."""
    if not required():
        return True, None
    r = conn.execute("SELECT definition_json FROM strategy_version WHERE strategy_id=? AND version=?",
                     (strategy_id, version)).fetchone()
    if not r:
        return True, None
    try:
        feats = json.loads(r[0] or "{}").get("features_used") or []
    except Exception:
        feats = []
    import re as _re
    feats = set(feats) | set(_re.findall(r"qf_[a-z][a-z0-9_]*", r[0] or ""))
    from quant.factors import REGISTRY
    ap = approved_keys(conn)
    missing = []
    for f in feats:
        if f.startswith("qf_") and f[3:] in REGISTRY:
            key = REGISTRY[f[3:]].key
            if key not in ap:
                missing.append(key)
    if missing:
        return False, f"approved factors (quant.require_approved_factors): not approved {missing}"
    return True, None
