"""
Audit export, retention and the off-box copy (SEC-03), W29.

The audit tables are append-only by trigger (ops/migrations 0004): enterprise_audit (hash
chained) and oms_order_event. Nothing deletes from them -- that is the point -- so
"retention" here means: every row is exported once, in order, to a segment file that is
itself tamper-evident, and a copy leaves the machine.

    export(conn, source="enterprise_audit"|"oms_order_event")
        rows after the last exported id -> one gzip JSONL segment in audit.export_dir
        (default atip_data/audit_exports/<source>/<first>-<last>.jsonl.gz) with a .sha256
        sidecar; for enterprise_audit the chain is verified first and its result recorded
        (a broken chain is exported anyway, flagged, and alerted -- the evidence matters
        more than a clean file). audit.offbox_dir (any path: a second drive, a NAS share,
        a synced OneDrive folder) gets a copy of both files. Registered in audit_export.
    run_scheduled()    both sources, daily (scheduler 19:30, after the ops backup)
    verify_segment(path)  recompute a segment's sha256 against its sidecar
    status(conn)       last segment per source, rows not yet exported, retention policy

config.json "audit": {"export_dir": "atip_data/audit_exports", "offbox_dir": null,
                      "retention_policy_days": 2555}
retention_policy_days (7 years default) is documented policy: rows older than it MAY be
pruned only by an operator, after confirming the segment holding them exists off-box --
ATIP itself never prunes.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path

SOURCES = {"enterprise_audit": "id", "oms_order_event": "id"}
DEFAULTS = {"export_dir": "atip_data/audit_exports", "offbox_dir": None, "retention_policy_days": 2555}


def settings() -> dict:
    try:
        cfg = json.loads((Path("atip_data") / "config.json").read_text(encoding="utf-8"))
        return {**DEFAULTS, **(cfg.get("audit") or {})}
    except Exception:
        return dict(DEFAULTS)


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def export(conn, source: str = "enterprise_audit", max_rows: int = 200_000) -> dict:
    if source not in SOURCES:
        raise ValueError(f"source must be one of {sorted(SOURCES)}")
    s = settings()
    last = conn.execute("SELECT MAX(last_id) FROM audit_export WHERE source=?", (source,)).fetchone()[0] or 0
    cur = conn.execute(f"SELECT * FROM {source} WHERE id>? ORDER BY id LIMIT ?", (int(last), int(max_rows)))
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    if not rows:
        return {"source": source, "rows": 0, "note": "nothing new to export"}
    chain = None
    if source == "enterprise_audit":
        from enterprise.audit import verify_chain
        chain = verify_chain(conn)
    first, lastid = rows[0]["id"], rows[-1]["id"]
    d = Path(s["export_dir"]) / source
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{first:010d}-{lastid:010d}.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(json.dumps({"_segment": {"source": source, "first_id": first, "last_id": lastid, "rows": len(rows),
                                         "exported_at": datetime.now().isoformat(timespec="seconds"),
                                         "chain": chain}}, default=str) + "\n")
        for r in rows:
            f.write(json.dumps(r, default=str, sort_keys=True) + "\n")
    sha = _sha(path)
    Path(str(path) + ".sha256").write_text(f"{sha}  {path.name}\n", encoding="utf-8")
    off = None
    if s.get("offbox_dir"):
        od = Path(s["offbox_dir"]) / source
        od.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, od / path.name)
        shutil.copy2(str(path) + ".sha256", od / (path.name + ".sha256"))
        if _sha(od / path.name) != sha:
            raise IOError(f"off-box copy of {path.name} does not match its hash")
        off = str(od / path.name)
    eid = "AEX" + uuid.uuid4().hex[:12].upper()
    conn.execute("INSERT INTO audit_export (export_id,source,first_id,last_id,`rows`,path,offbox_path,sha256,chain_ok,"
                 "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (eid, source, first, lastid, len(rows), str(path), off, sha,
                  None if chain is None else (1 if chain.get("ok") else 0), datetime.now()))
    conn.commit()
    if chain is not None and not chain.get("ok"):
        try:
            from alerts.telegram import notify
            notify(f"🛑 <b>Audit chain broken</b> at id {chain.get('first_break')}: {chain.get('reason')} "
                   f"(exported anyway as {path.name})", category="security", severity="critical",
                   key=f"audit-chain-{chain.get('first_break')}")
        except Exception:
            pass
    return {"source": source, "export_id": eid, "rows": len(rows), "first_id": first, "last_id": lastid,
            "path": str(path), "offbox_path": off, "sha256": sha, "chain": chain}


def verify_segment(path) -> dict:
    p = Path(path)
    side = Path(str(p) + ".sha256")
    if not p.exists() or not side.exists():
        raise FileNotFoundError("segment or its .sha256 sidecar is missing")
    want = side.read_text(encoding="utf-8").split()[0]
    got = _sha(p)
    return {"path": str(p), "ok": want == got, "sha256": got, "expected": want}


def status(conn) -> dict:
    s = settings()
    out = {"export_dir": s["export_dir"], "offbox_dir": s["offbox_dir"],
           "retention_policy_days": s["retention_policy_days"], "sources": {}}
    for src in SOURCES:
        last = conn.execute("SELECT export_id, last_id, `rows`, offbox_path, chain_ok, created_at FROM audit_export "
                            "WHERE source=? ORDER BY last_id DESC LIMIT 1", (src,)).fetchone()
        maxid = conn.execute(f"SELECT MAX(id) FROM {src}").fetchone()[0] or 0
        out["sources"][src] = {"last_export": dict(zip(("export_id", "last_id", "rows", "offbox_path", "chain_ok",
                                                        "created_at"), last)) if last else None,
                               "pending_rows": int(maxid) - int(last[1] if last else 0)}
    if not s["offbox_dir"]:
        out["warning"] = "audit.offbox_dir is not set: exports stay on this machine only"
    return out


def run_scheduled() -> dict:
    from db.schema import get_connection
    conn = get_connection()
    try:
        res = [export(conn, src) for src in SOURCES]
        return {"status": "SUCCESS", "rows": sum(r.get("rows", 0) for r in res), "exports": res}
    finally:
        conn.close()
