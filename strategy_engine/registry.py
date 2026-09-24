"""
Strategy registry (SE-01): strategies and their immutable versions.

  create_strategy(defn)   a new strategy with its first version, status DRAFT
  add_version(defn)       a new version of an existing strategy; its content is
                          frozen by definition_hash -- storing different content
                          under an existing version is refused ("create a new
                          version"), storing the same content again is a no-op
  set_current_version     which version decisions and new backtests use by default
  update_metadata         name / description only; anything that changes what the
                          strategy DOES is a new version
  sync_library            load strategy_engine/library/*.json: adds what is
                          missing, never overwrites a stored version

Versions are what backtests and decisions record, so a result can always be
traced to the exact rules, parameters, factors and weights that produced it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from strategy_engine import lifecycle
from strategy_engine.definition import DefinitionError, canonical, definition_hash, validate

log = logging.getLogger("atip.strategy_engine")
LIBRARY = Path(__file__).resolve().parent / "library"


class RegistryError(ValueError):
    pass


def _vkey(v: str) -> tuple:
    return tuple(int(x) for x in v.split("."))


def create_strategy(conn, defn: dict, actor: str = "owner", source: str = "user", notes: str = "") -> dict:
    d = validate(defn)
    if conn.execute("SELECT 1 FROM strategy WHERE strategy_id=?", (d["strategy_id"],)).fetchone():
        raise RegistryError(f"strategy {d['strategy_id']} exists — add a version instead")
    now = datetime.now()
    conn.execute("INSERT INTO strategy (strategy_id,name,description,kind,category,status,current_version,source,"
                 "owner,created_at,updated_at) VALUES (?,?,?,?,?,'DRAFT',?,?,?,?,?)",
                 (d["strategy_id"], d["name"], d["description"], d["kind"], d["category"], d["version"], source,
                  actor, now, now))
    _insert_version(conn, d, notes, actor)
    lifecycle.log_event(conn, d["strategy_id"], "LIFECYCLE", "created", d["version"], {"source": source},
                        None, "DRAFT", actor, commit=False)
    conn.commit()
    return get_strategy(conn, d["strategy_id"])


def _index_features(conn, d):
    """strategy_feature rows for a version: the features its definition uses and
    the data each needs. Idempotent (also back-fills versions stored before W4)."""
    from strategy_engine.features import inputs_of
    for f in d.get("features_used") or []:
        try:
            ins = ",".join(inputs_of(f))
        except Exception:
            ins = None
        conn.execute("INSERT OR IGNORE INTO strategy_feature (strategy_id,version,feature,inputs) VALUES (?,?,?,?)",
                     (d["strategy_id"], d["version"], f, ins))


def _insert_version(conn, d, notes, actor="owner"):
    """Store an immutable version, index its parameters, and log it."""
    h = definition_hash(d)
    conn.execute("INSERT INTO strategy_version (strategy_id,version,definition_json,definition_hash,notes,created_at) "
                 "VALUES (?,?,?,?,?,?)", (d["strategy_id"], d["version"], canonical(d), h, notes, datetime.now()))
    for p in d.get("parameters") or []:
        conn.execute("INSERT INTO strategy_parameter (strategy_id,version,name,type,default_json,min,max,allowed_json,"
                     "required,description) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (d["strategy_id"], d["version"], p["name"], p["type"], json.dumps(p.get("default")),
                      p.get("min"), p.get("max"), json.dumps(p.get("allowed")) if p.get("allowed") is not None else None,
                      int(bool(p.get("required"))), p.get("description", "")))
    _index_features(conn, d)
    lifecycle.log_event(conn, d["strategy_id"], "VERSION", notes or "version stored", d["version"],
                        {"definition_hash": h}, actor=actor, commit=False)


def add_version(conn, defn: dict, notes: str = "", make_current: bool = True) -> dict:
    d = validate(defn)
    s = conn.execute("SELECT kind, status FROM strategy WHERE strategy_id=?", (d["strategy_id"],)).fetchone()
    if not s:
        raise RegistryError(f"no strategy {d['strategy_id']} — create it first")
    if s[1] in ("RETIRED", "ARCHIVED"):
        raise RegistryError(f"{d['strategy_id']} is {s[1]}; it takes no new versions")
    if d["kind"] != s[0]:
        raise RegistryError(f"kind cannot change between versions ({s[0]} -> {d['kind']}); create a new strategy")
    ex = conn.execute("SELECT definition_hash FROM strategy_version WHERE strategy_id=? AND version=?",
                      (d["strategy_id"], d["version"])).fetchone()
    if ex:
        if ex[0] == definition_hash(d):
            return get_version(conn, d["strategy_id"], d["version"])
        raise RegistryError(f"{d['strategy_id']} {d['version']} already exists with different content — "
                            f"versions are immutable; create a new version number")
    latest = [r[0] for r in conn.execute("SELECT version FROM strategy_version WHERE strategy_id=?", (d["strategy_id"],))]
    if latest and _vkey(d["version"]) <= max(_vkey(v) for v in latest):
        raise RegistryError(f"version {d['version']} must be higher than {max(latest, key=_vkey)}")
    _insert_version(conn, d, notes)
    if make_current:
        conn.execute("UPDATE strategy SET current_version=?, name=?, description=?, updated_at=? WHERE strategy_id=?",
                     (d["version"], d["name"], d["description"], datetime.now(), d["strategy_id"]))
    conn.commit()
    return get_version(conn, d["strategy_id"], d["version"])


def set_current_version(conn, strategy_id: str, version: str) -> dict:
    if not conn.execute("SELECT 1 FROM strategy_version WHERE strategy_id=? AND version=?",
                        (strategy_id, version)).fetchone():
        raise RegistryError(f"no version {version} of {strategy_id}")
    conn.execute("UPDATE strategy SET current_version=?, updated_at=? WHERE strategy_id=?",
                 (version, datetime.now(), strategy_id))
    conn.commit()
    return get_strategy(conn, strategy_id)


METADATA_FIELDS = {"name": str, "description": str, "category": str, "owner": str, "priority": int, "weight": float}


def update_metadata(conn, strategy_id: str, changes: dict | None = None, **kw) -> dict:
    """Metadata only -- name, description, category, owner, and the selection
    settings priority (lower runs first) and weight (combination weight).
    Anything that changes what the strategy DOES is a new version."""
    changes = {**(changes or {}), **{k: v for k, v in kw.items() if v is not None}}
    if not get_strategy(conn, strategy_id):
        raise RegistryError(f"no strategy {strategy_id}")
    bad = set(changes) - set(METADATA_FIELDS)
    if bad:
        raise RegistryError(f"{sorted(bad)} cannot be updated in place; only {sorted(METADATA_FIELDS)} can — "
                            f"anything else is a new version")
    for k, v in changes.items():
        try:
            v = METADATA_FIELDS[k](v)
        except (TypeError, ValueError):
            raise RegistryError(f"{k} must be {METADATA_FIELDS[k].__name__}")
        if k == "weight" and v < 0:
            raise RegistryError("weight cannot be negative")
        conn.execute(f"UPDATE strategy SET {k}=?, updated_at=? WHERE strategy_id=?", (v, datetime.now(), strategy_id))
    if changes:
        lifecycle.log_event(conn, strategy_id, "METADATA", "metadata updated", details=changes, actor="owner",
                            commit=False)
    conn.commit()
    return get_strategy(conn, strategy_id)


def get_strategy(conn, strategy_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM strategy WHERE strategy_id=?", (strategy_id,)).fetchone()
    return dict(r) if r else None


def list_strategies(conn) -> list:
    return [dict(r) for r in conn.execute("SELECT * FROM strategy ORDER BY strategy_id")]


def list_versions(conn, strategy_id: str) -> list:
    rows = [dict(r) for r in conn.execute(
        "SELECT strategy_id, version, definition_hash, notes, created_at FROM strategy_version WHERE strategy_id=?",
        (strategy_id,))]
    return sorted(rows, key=lambda r: _vkey(r["version"]), reverse=True)


def get_version(conn, strategy_id: str, version: str | None = None) -> dict | None:
    """The stored version (default: current) with its parsed definition."""
    if version is None:
        s = get_strategy(conn, strategy_id)
        if not s:
            return None
        version = s["current_version"]
    r = conn.execute("SELECT * FROM strategy_version WHERE strategy_id=? AND version=?", (strategy_id, version)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["definition"] = json.loads(d.pop("definition_json"))
    return d


def loader(conn):
    """fn(strategy_id, version) -> definition, for composites."""
    def load(sid, ver):
        v = get_version(conn, sid, ver)
        if not v:
            raise RegistryError(f"composite member {sid} {ver} is not registered")
        return v["definition"]
    return load


def sync_library(conn) -> dict:
    """Register the shipped definitions that are missing; never overwrite."""
    added, skipped, conflicts = [], [], []
    for path in sorted(LIBRARY.glob("*.json")):
        if path.name == "regime_mapping.json":
            continue
        try:
            defn = json.loads(path.read_text(encoding="utf-8"))
            d = validate(defn)
        except (DefinitionError, ValueError) as e:
            conflicts.append(f"{path.name}: invalid ({e})"); continue
        sid, ver = d["strategy_id"], d["version"]
        ex = conn.execute("SELECT definition_hash FROM strategy_version WHERE strategy_id=? AND version=?",
                          (sid, ver)).fetchone()
        if ex:
            (skipped if ex[0] == definition_hash(d) else conflicts).append(f"{sid} {ver}")
            continue
        try:
            if get_strategy(conn, sid):
                add_version(conn, d, notes=f"library {path.name}")
            else:
                create_strategy(conn, d, actor="library", source="library", notes=f"library {path.name}")
            added.append(f"{sid} {ver}")
        except (RegistryError, DefinitionError) as e:
            conflicts.append(f"{sid} {ver}: {e}")
    if conflicts:
        log.warning(f"  strategy library: {conflicts}")
    for (dj,) in conn.execute("SELECT definition_json FROM strategy_version").fetchall():
        try:
            _index_features(conn, json.loads(dj))
        except Exception as e:
            log.warning(f"  strategy_feature index: {e}")
    conn.commit()
    from strategy_engine.selection import seed_default_mapping
    seeded = seed_default_mapping(conn)
    return {"added": added, "unchanged": skipped, "conflicts": conflicts, "regime_mapping_seeded": seeded}
