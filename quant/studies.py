"""
Research registry (W22, DBS-07 + QR-12): a study is the record of one research
question -- hypothesis, method, what was run, what it found -- linking the factor
research, experiments and backtests that answered it.

    research_study     study_id, title, hypothesis, method, status (PROPOSED / RUNNING /
                       CONCLUDED / ABANDONED), conclusion, outcome (SUPPORTED / REJECTED /
                       INCONCLUSIVE), tags, created / updated, created_by
    research_link      study_id + kind (factor_research | experiment | backtest | factor |
                       strategy) + ref, with a note; a link is checked to exist when added

A concluded study is frozen: its fields and links can no longer change (a new study
supersedes it via `supersedes`).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

STATUSES = ("PROPOSED", "RUNNING", "CONCLUDED", "ABANDONED")
OUTCOMES = ("SUPPORTED", "REJECTED", "INCONCLUSIVE")
LINK_KINDS = {"factor_research": ("quant_factor_research", "id"), "experiment": ("quant_experiment", "experiment_id"),
              "backtest": ("backtest_run", "run_id"), "factor": ("quant_factor", "factor_id"),
              "strategy": ("strategy", "strategy_id")}


def _txt(v, name, n, required=True):
    if v is None or str(v).strip() == "":
        if required:
            raise ValueError(f"{name} is required")
        return None
    s = str(v).strip()
    if len(s) > n:
        raise ValueError(f"{name} longer than {n} characters")
    return s


def create(conn, b: dict, actor="owner") -> dict:
    unknown = set(b) - {"title", "hypothesis", "method", "tags", "supersedes"}
    if unknown:
        raise ValueError(f"unknown fields {sorted(unknown)}")
    sid = f"study_{uuid.uuid4().hex[:12]}"
    tags = b.get("tags") or []
    if not isinstance(tags, list) or any(not isinstance(t, str) or len(t) > 40 for t in tags):
        raise ValueError("tags must be a list of short strings")
    sup = b.get("supersedes")
    if sup and not conn.execute("SELECT 1 FROM research_study WHERE study_id=?", (sup,)).fetchone():
        raise LookupError(f"study {sup} not found")
    now = datetime.now()
    conn.execute("INSERT INTO research_study (study_id,title,hypothesis,method,status,tags_json,supersedes,created_at,"
                 "updated_at,created_by) VALUES (?,?,?,?,'PROPOSED',?,?,?,?,?)",
                 (sid, _txt(b.get("title"), "title", 200), _txt(b.get("hypothesis"), "hypothesis", 2000),
                  _txt(b.get("method"), "method", 4000, False), json.dumps(tags), sup, now, now, actor))
    conn.commit()
    return get(conn, sid)


def get(conn, sid) -> dict:
    r = conn.execute("SELECT * FROM research_study WHERE study_id=?", (sid,)).fetchone()
    if not r:
        raise LookupError("study not found")
    d = dict(r)
    d["tags"] = json.loads(d.pop("tags_json") or "[]")
    d["links"] = [dict(x) for x in conn.execute("SELECT kind, ref, note, added_at, added_by FROM research_link WHERE "
                                                "study_id=? ORDER BY added_at", (sid,))]
    return d


def list_studies(conn, status=None, limit=200) -> list:
    q = "SELECT study_id, title, status, outcome, created_at, updated_at FROM research_study"
    args = []
    if status:
        q += " WHERE status=?"
        args.append(status)
    return [dict(r) for r in conn.execute(q + " ORDER BY updated_at DESC LIMIT ?", (*args, int(limit)))]


def _open(conn, sid):
    s = get(conn, sid)
    if s["status"] in ("CONCLUDED", "ABANDONED"):
        raise ValueError(f"study is {s['status']} and frozen")
    return s


def link(conn, sid, kind, ref, note=None, actor="owner") -> dict:
    _open(conn, sid)
    if kind not in LINK_KINDS:
        raise ValueError(f"kind must be one of {sorted(LINK_KINDS)}")
    table, col = LINK_KINDS[kind]
    if not conn.execute(f"SELECT 1 FROM {table} WHERE {col}=?", (ref,)).fetchone():          # fixed mapping
        raise LookupError(f"{kind} {ref} not found")
    conn.execute("INSERT OR IGNORE INTO research_link (study_id,kind,ref,note,added_at,added_by) VALUES (?,?,?,?,?,?)",
                 (sid, kind, str(ref), _txt(note, "note", 500, False), datetime.now(), actor))
    if get(conn, sid)["status"] == "PROPOSED":
        conn.execute("UPDATE research_study SET status='RUNNING', updated_at=? WHERE study_id=?", (datetime.now(), sid))
    conn.commit()
    return get(conn, sid)


def conclude(conn, sid, outcome, conclusion, actor="owner") -> dict:
    s = _open(conn, sid)
    o = str(outcome or "").upper()
    if o not in OUTCOMES:
        raise ValueError(f"outcome must be one of {list(OUTCOMES)}")
    if not s["links"]:
        raise ValueError("a study needs at least one linked result before it can be concluded")
    conn.execute("UPDATE research_study SET status='CONCLUDED', outcome=?, conclusion=?, updated_at=?, concluded_by=? "
                 "WHERE study_id=?", (o, _txt(conclusion, "conclusion", 4000), datetime.now(), actor, sid))
    conn.commit()
    return get(conn, sid)


def abandon(conn, sid, reason, actor="owner") -> dict:
    _open(conn, sid)
    conn.execute("UPDATE research_study SET status='ABANDONED', conclusion=?, updated_at=?, concluded_by=? WHERE "
                 "study_id=?", (_txt(reason, "reason", 1000), datetime.now(), actor, sid))
    conn.commit()
    return get(conn, sid)
