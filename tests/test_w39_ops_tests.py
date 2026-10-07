"""W39: first automated tests for operations features the tracker listed with no test reaching
their key files -- DBS-03 / OPS-06 backups (ops/backup.py), MON-09 operational alerting
(ops/monitor.py), REL-001 wealth-track health (wealth/health.py), SEC-03 audit export
(enterprise/audit_export.py), API-05 generated API reference (ops/api_docs.py) and DBS-02
retention (db/purge.py). Expected values are worked out by hand in each test; nothing here
touches the network."""

import base64
import gzip
import hashlib
import json
import os
import sqlite3
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest


# ── shared set-up ─────────────────────────────────────────────────────────────

@pytest.fixture
def ops_env(tmp_path, monkeypatch):
    """cwd = tmp_path, so atip_data/config.json, backups, restores, secrets and exports all land
    there; no encryption key from any source; the schema created (versioned migrations NOT applied)."""
    monkeypatch.chdir(tmp_path)
    for k in list(os.environ):
        if k.startswith("ATIP__") or k in ("ATIP_ENV", "ATIP_ENCRYPTION_KEY"):
            monkeypatch.delenv(k)
    import ops.secrets as S
    monkeypatch.setattr(S, "_ENV_LOADED", {"done": True, "values": {}})       # no .env either
    from db.schema import init_db
    init_db()
    return tmp_path


@pytest.fixture
def clock(monkeypatch):
    """ops.backup's datetime.now() returns clock.at (a plain datetime), so backups get distinct
    ids and finish times without sleeping. Everything else keeps the real clock."""
    from ops import backup as B

    class Clock(datetime):
        at = datetime(2026, 9, 1, 10, 0)

        @classmethod
        def now(cls, tz=None):
            return Clock.at

    monkeypatch.setattr(B, "datetime", Clock)
    return Clock


def _config(**sections):
    p = Path("atip_data") / "config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sections), encoding="utf-8")


def _key(monkeypatch) -> bytes:
    k = os.urandom(32)
    monkeypatch.setenv("ATIP_ENCRYPTION_KEY", base64.b64encode(k).decode())
    return k


def _conn():
    from db.schema import get_connection
    return get_connection()


PRICES = [("ACME", "2026-09-01", 100.0), ("ACME", "2026-09-02", 101.5), ("BETA", "2026-09-01", 50.25)]


def _seed(conn):
    conn.executemany("INSERT INTO prices_daily (symbol, date, close) VALUES (?,?,?)", PRICES)
    conn.executemany("INSERT INTO ai_scores (symbol, date, atip_score) VALUES (?,?,?)",
                     [("ACME", "2026-09-02", 71.0), ("BETA", "2026-09-02", 44.0)])
    conn.commit()


def _ro(path):
    return sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)


def _run(conn, job, status, run_date, kind="run"):
    now = datetime.now()
    conn.execute("INSERT INTO pipeline_log (run_date, job_name, start_time, end_time, status, kind) "
                 "VALUES (?,?,?,?,?,?)", (str(run_date), job, now, now, status, kind))
    conn.commit()


# ── DBS-03 / OPS-06: backups ───────────────────────────────────────────────────

def test_backup_is_an_online_copy_verified_by_integrity_counts_and_hash(ops_env):
    from ops import backup as B
    c = _conn()
    _seed(c)
    c.close()
    res = B.backup("manual")
    p = Path(res["path"])
    assert res["status"] == "VERIFIED" and res["integrity"] == "ok" and "error" not in res
    assert p.parent.resolve() == (ops_env / "atip_data" / "backups").resolve()
    assert p.name == res["backup_id"] + ".db" and res["backup_id"].startswith("atip-manual-")
    assert res["tables"]["prices_daily"] == 3 and res["tables"]["ai_scores"] == 2
    assert res["sha256"] == hashlib.sha256(p.read_bytes()).hexdigest() and res["size_bytes"] == p.stat().st_size
    c = _conn()
    row = c.execute("SELECT status, sha256, integrity, tables_json, finished_at FROM ops_backup WHERE backup_id=?",
                    (res["backup_id"],)).fetchone()
    c.close()
    assert (row["status"], row["sha256"], row["integrity"]) == ("VERIFIED", res["sha256"], "ok")
    assert json.loads(row["tables_json"]) == res["tables"] and row["finished_at"] is not None
    b = _ro(p)
    try:
        assert b.execute("SELECT symbol, date, close FROM prices_daily ORDER BY id").fetchall() == PRICES
    finally:
        b.close()


def test_a_copy_that_fails_verification_is_kept_as_failed_and_alerts_critical(tmp_path, monkeypatch):
    """No init_db: the W8 tables exist (additive layer) but prices_daily does not, so the copy
    cannot pass verification. It is renamed *.failed, recorded FAILED and never pruned after."""
    monkeypatch.chdir(tmp_path)
    from ops import backup as B
    from ops.monitor import r_backup
    v = B.verify(tmp_path / "nope.db")
    assert (v["ok"], v["error"]) == (False, "file missing")
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database " * 300)
    v = B.verify(junk)
    assert v["ok"] is False and "DatabaseError" in v["error"]

    res = B.backup("manual")
    assert (res["status"], res["error"]) == ("FAILED", "prices_daily missing")
    assert res["path"].endswith(".db.failed") and Path(res["path"]).exists()
    assert not Path(res["path"][:-len(".failed")]).exists()
    c = _conn()
    try:
        assert tuple(c.execute("SELECT status, error FROM ops_backup").fetchone()) == ("FAILED", "prices_daily missing")
        assert r_backup(c) == (True, "critical", "latest backup FAILED: prices_daily missing")
    finally:
        c.close()
    assert B.run_scheduled_backup() == {"status": "FAILED", "error": "prices_daily missing", "rows": 0}


def test_prune_keeps_the_newest_daily_plus_one_per_week_and_only_its_own_files(ops_env, clock):
    from ops import backup as B
    _config(ops={"backup_keep_daily": 3, "backup_keep_weekly": 4})
    when = {"B1": datetime(2026, 9, 14, 10), "B2": datetime(2026, 9, 21, 10),       # Mondays
            "B3": datetime(2026, 9, 28, 10), "B4": datetime(2026, 9, 30, 10),       # Mon, Wed
            "B5": datetime(2026, 10, 2, 10),                                        # Fri, same week as B3/B4
            "B6": datetime(2026, 10, 5, 10), "B7": datetime(2026, 10, 6, 10)}       # Mon, Tue
    ids = {}
    for name, at in when.items():
        clock.at = at
        r = B.backup("scheduled")
        assert r["status"] == "VERIFIED"
        ids[name] = r["backup_id"]
    assert ids["B4"] == "atip-scheduled-20260930-100000"
    # an older VERIFIED backup recorded OUTSIDE the backup directory, a FAILED one inside it and a
    # hand-made snapshot next to the database: none of them may be deleted
    outside = ops_env / "elsewhere" / "atip-manual-20260901-100000.db"
    outside.parent.mkdir()
    outside.write_bytes(b"x")
    failed = Path("atip_data/backups/atip-manual-20260801-100000.db")
    failed.write_bytes(b"x")
    snap = Path("atip_data/atip.db.bak-20260801")
    snap.write_bytes(b"x")
    c = _conn()
    c.executemany("INSERT INTO ops_backup (backup_id, kind, path, started_at, finished_at, status) VALUES "
                  "(?,?,?,?,?,?)",
                  [("atip-manual-20260901-100000", "manual", str(outside), datetime(2026, 9, 1, 10),
                    datetime(2026, 9, 1, 10), "VERIFIED"),
                   ("atip-manual-20260801-100000", "manual", str(failed), datetime(2026, 8, 1, 10),
                    datetime(2026, 8, 1, 10), "FAILED")])
    c.commit()
    # daily: the newest 3 = B7 B6 B5. weekly (newest per ISO week, 4 weeks): B7 (week of 5 Oct),
    # B5 (week of 28 Sep), B2 (21 Sep), B1 (14 Sep). B4 and B3 share B5's week -> pruned, newest
    # first. The 1 Sep backup falls outside both windows but is not in the backup directory.
    assert B.prune() == [ids["B4"], ids["B3"]]
    for n in ("B1", "B2", "B5", "B6", "B7"):
        assert Path(f"atip_data/backups/{ids[n]}.db").exists()
    for n in ("B3", "B4"):                                   # gone, with nothing left beside it
        assert list(Path("atip_data/backups").glob(ids[n] + "*")) == []
    assert outside.exists() and failed.exists() and snap.exists()
    assert {r[0] for r in c.execute("SELECT backup_id FROM ops_backup WHERE pruned_at IS NOT NULL")} == \
        {ids["B3"], ids["B4"]}
    c.close()
    assert B.prune() == []                                   # idempotent


def test_restore_copies_a_verified_backup_to_a_new_file_never_over_the_live_db(ops_env):
    from db.schema import DB_PATH
    from ops import backup as B
    c = _conn()
    _seed(c)
    c.close()
    res = B.backup("manual")
    c = _conn()
    c.execute("INSERT INTO prices_daily (symbol, date, close) VALUES ('GAMMA', '2026-09-03', 9.0)")
    c.commit()
    c.close()
    target = ops_env / "restore-test" / "copy.db"
    out = B.restore(res["backup_id"], str(target))
    assert out == {"restored_to": str(target), "source": res["path"], "verified": True, "sha256_match": True,
                   "tables": res["tables"]}
    r = _ro(target)
    try:                                                     # the backup's point in time: no GAMMA
        assert r.execute("SELECT symbol, date, close FROM prices_daily ORDER BY id").fetchall() == PRICES
    finally:
        r.close()
    with pytest.raises(FileExistsError):
        B.restore(res["backup_id"], str(target))
    with pytest.raises(RuntimeError, match="never overwrites the live database"):
        B.restore(res["backup_id"], str(DB_PATH))
    with pytest.raises(FileNotFoundError):
        B.restore("atip-manual-19990101-000000")
    by_path = B.restore(res["path"])                         # default target: atip_data/restore/
    assert Path(by_path["restored_to"]).parent.resolve() == (ops_env / "atip_data" / "restore").resolve()
    assert by_path["sha256_match"] is True
    c = _conn()
    try:
        assert c.execute("SELECT COUNT(*) FROM prices_daily").fetchone()[0] == 4      # live DB untouched
    finally:
        c.close()


@pytest.mark.parametrize("size,chunks", [(10240, 3), (8192, 3)])
def test_backup_encryption_round_trips_in_authenticated_chunks(ops_env, monkeypatch, size, chunks):
    """10240 B = 4096 + 4096 + 2048. 8192 B = 4096 + 4096 + an EMPTY final chunk: the last chunk is
    always flagged, so a file cut at a chunk boundary is still detectable."""
    from ops import backup as B
    from ops.crypto import key_id
    k = _key(monkeypatch)
    monkeypatch.setattr(B, "CHUNK", 4096)
    data = (bytes(range(256)) * 64)[:size]
    src, enc_p, out = ops_env / "plain.bin", ops_env / "plain.bin.enc", ops_env / "back.bin"
    src.write_bytes(data)
    enc = B.encrypt_file(src, enc_p)
    raw = enc_p.read_bytes()
    assert enc["chunks"] == chunks and enc["key_id"] == key_id(k)
    assert raw[:20] == B.MAGIC + key_id(k).encode() + (4096).to_bytes(4, "big")         # 8 + 8 + 4 header
    assert len(raw) == 20 + chunks * (12 + 4 + 16) + size        # nonce + length + GCM tag per chunk
    assert enc["sha256"] == hashlib.sha256(raw).hexdigest() and data[:256] not in raw
    assert B.decrypt_file(enc_p, out) == {"path": str(out), "chunks": chunks} and out.read_bytes() == data


def test_encrypted_backup_rejects_truncation_reordering_tampering_and_an_unknown_key(ops_env, monkeypatch):
    from cryptography.exceptions import InvalidTag
    from ops import backup as B
    k_old = _key(monkeypatch)
    monkeypatch.setattr(B, "CHUNK", 4096)
    data = (bytes(range(256)) * 40)                             # 10240 B -> 3 chunks
    (ops_env / "p.bin").write_bytes(data)
    enc_p, out = ops_env / "p.enc", ops_env / "out.bin"
    B.encrypt_file(ops_env / "p.bin", enc_p)
    raw = enc_p.read_bytes()
    hdr, rec = raw[:20], 12 + 4 + 4096 + 16
    c0, c1, tail = raw[20:20 + rec], raw[20 + rec:20 + 2 * rec], raw[20 + 2 * rec:]
    bad = ops_env / "bad.enc"
    bad.write_bytes(hdr + c0 + c1)                               # final chunk dropped
    with pytest.raises(RuntimeError, match="truncated"):
        B.decrypt_file(bad, out)
    bad.write_bytes(hdr + c1 + c0 + tail)                        # chunks swapped
    with pytest.raises(InvalidTag):
        B.decrypt_file(bad, out)
    flipped = bytearray(raw)
    flipped[100] ^= 1                                            # inside chunk 0's ciphertext
    bad.write_bytes(bytes(flipped))
    with pytest.raises(InvalidTag):
        B.decrypt_file(bad, out)
    _key(monkeypatch)                                            # the key was rotated ...
    with pytest.raises(RuntimeError, match="that key is not available"):
        B.decrypt_file(enc_p, out)
    prev = Path("atip_data/secrets/ATIP_ENCRYPTION_KEY.previous")  # ... and the old one kept readable
    prev.parent.mkdir(parents=True)
    prev.write_text(base64.b64encode(k_old).decode(), encoding="utf-8")
    B.decrypt_file(enc_p, out)
    assert out.read_bytes() == data


def test_offsite_copy_is_encrypted_and_the_restore_drill_restores_from_it(ops_env, monkeypatch):
    from ops import backup as B
    _key(monkeypatch)
    off = ops_env / "nas"
    _config(ops={"backup_offsite_dir": str(off)})
    c = _conn()
    _seed(c)
    c.close()
    res = B.backup("scheduled")
    out = B.offsite_copy(res["backup_id"])
    name = Path(res["path"]).name + ".enc"
    enc = off / name
    assert out == {"status": "COPIED_ENCRYPTED", "path": str(enc), "sha256": out["sha256"]}
    assert (off / (name + ".sha256")).read_text(encoding="utf-8").split() == [out["sha256"], name]
    assert hashlib.sha256(enc.read_bytes()).hexdigest() == out["sha256"]
    assert enc.read_bytes().startswith(B.MAGIC) and b"SQLite format 3" not in enc.read_bytes()
    assert not Path(res["path"] + ".enc").exists()               # the local temporary is removed
    c = _conn()
    try:
        assert tuple(c.execute("SELECT offsite_status, offsite_path, encrypted_sha256 FROM ops_backup WHERE "
                               "backup_id=?", (res["backup_id"],)).fetchone()) == \
            ("COPIED_ENCRYPTED", str(enc), out["sha256"])
        drill = B.restore_drill(notify=False)
        assert (drill["status"], drill["source"], drill["backup_id"]) == \
            ("SUCCESS", "offsite_encrypted", res["backup_id"])
        assert drill["mismatches"] == {} and drill["tables_checked"] == len(res["tables"]) and drill["error"] is None
        assert list(Path("atip_data/restore").glob("drill-*")) == []                      # drill copy deleted
        assert [tuple(r) for r in c.execute("SELECT status, source FROM ops_restore_drill")] == \
            [("PASSED", "offsite_encrypted")]
        damaged = bytearray(enc.read_bytes())                   # bit rot on the NAS
        damaged[-1] ^= 1
        enc.write_bytes(bytes(damaged))
        failed = B.restore_drill()
        assert failed["status"] == "FAILED" and "does not match its sidecar hash" in failed["error"]
        (sev, msg), = c.execute("SELECT severity, message FROM alert_log WHERE category='ops'").fetchall()
        assert sev == "critical" and "Restore drill FAILED" in msg
    finally:
        c.close()


def test_restore_drill_compares_row_counts_with_those_recorded_at_backup_time(ops_env):
    from ops import backup as B
    c = _conn()
    _seed(c)
    c.close()
    res = B.backup("manual")
    ok = B.restore_drill(notify=False)
    assert (ok["status"], ok["source"], ok["mismatches"]) == ("SUCCESS", "local", {})
    claimed = dict(res["tables"], prices_daily=99)
    c = _conn()
    try:
        c.execute("UPDATE ops_backup SET tables_json=? WHERE backup_id=?", (json.dumps(claimed), res["backup_id"]))
        c.commit()
        bad = B.restore_drill(notify=False)
        assert bad["status"] == "FAILED" and bad["error"] == "row counts differ"
        assert bad["mismatches"] == {"prices_daily": (99, 3)}
        assert [r[0] for r in c.execute("SELECT status FROM ops_restore_drill ORDER BY started_at")] == \
            ["PASSED", "FAILED"]
    finally:
        c.close()


def test_offsite_copy_never_ships_plaintext_without_explicit_consent(ops_env):
    from ops import backup as B
    res = B.backup("manual")
    assert B.offsite_copy(res["backup_id"])["status"] == "NOT_CONFIGURED"
    off = ops_env / "nas"
    _config(ops={"backup_offsite_dir": str(off)})
    assert B.offsite_copy(res["backup_id"])["status"] == "SKIPPED_NO_KEY"          # no ATIP_ENCRYPTION_KEY
    assert list(off.iterdir()) == []
    _config(ops={"backup_offsite_dir": str(off), "backup_offsite_plaintext": True})
    out = B.offsite_copy(res["backup_id"])
    assert out["status"] == "COPIED_PLAINTEXT" and Path(out["path"]) == off / Path(res["path"]).name
    assert hashlib.sha256(Path(out["path"]).read_bytes()).hexdigest() == res["sha256"]
    assert B.offsite_copy("atip-manual-19990101-000000")["status"] == "SKIPPED"     # not a VERIFIED backup


def test_offsite_retention_keeps_the_newest_copies(ops_env, clock, monkeypatch):
    from ops import backup as B
    _key(monkeypatch)
    off = ops_env / "nas"
    _config(ops={"backup_offsite_dir": str(off), "backup_offsite_keep": 2})
    names = []
    for i, day in enumerate((1, 2, 3)):
        clock.at = datetime(2026, 10, day, 19, 15)
        r = B.backup("scheduled")
        assert B.offsite_copy(r["backup_id"])["status"] == "COPIED_ENCRYPTED"
        names.append(Path(r["path"]).name + ".enc")
        t = time.time() - (3 - i) * 3600                         # the copies were made a day apart
        os.utime(off / names[-1], (t, t))
    # the third copy pruned the oldest .enc and its sidecar
    assert sorted(p.name for p in off.iterdir()) == sorted([names[1], names[1] + ".sha256",
                                                            names[2], names[2] + ".sha256"])


# ── MON-09: operational alerting ───────────────────────────────────────────────

def test_monitor_rules_count_only_todays_failures(ops_env):
    from enterprise.audit import record
    from ops import monitor as M
    c = _conn()
    today, yday, now = date.today(), date.today() - timedelta(days=1), datetime.now()
    try:
        _run(c, "execution_cycle", "FAILED", yday)                          # history
        _run(c, "execution_cycle", "SUCCESS", today)
        _run(c, "execution_cycle", "FAILED", today, kind="step")            # a step row is not a run
        assert M.r_risk_failures(c) == (False, "warning", "execution / risk cycle failed 0 time(s) today")
        _run(c, "execution_cycle", "FAILED", today)
        assert M.r_risk_failures(c) == (True, "warning", "execution / risk cycle failed 1 time(s) today")

        _run(c, "ml_predictions", "FAILED", today)
        c.executemany("INSERT INTO ml_training_run (run_id, model_id, status, started_at) VALUES (?,?,?,?)",
                      [("R0", "m", "FAILED", now - timedelta(days=1)), ("R1", "m", "FAILED", now),
                       ("R2", "m", "SUCCESS", now)])
        c.commit()
        assert M.r_ml_failures(c) == (True, "warning", "2 failed ML training / prediction run(s) today")

        ins = ("INSERT INTO oms_order (order_id, intent_id, risk_decision_id, symbol, side, quantity, order_type, "
               "mode, status, created_at) VALUES (?,?,?,'ACME','BUY',1,'MARKET','PAPER',?,?)")
        c.executemany(ins, [("O1", "I1", "D1", "FILLED", now), ("O2", "I2", "D2", "REJECTED", now - timedelta(days=1))])
        c.commit()
        assert M.r_failed_orders(c) == (False, "warning", "0 rejected / failed order(s) today")
        c.execute(ins, ("O3", "I3", "D3", "REJECTED", now))
        c.commit()
        assert M.r_failed_orders(c) == (True, "warning", "1 rejected / failed order(s) today")

        for _ in range(19):
            record(c, "auth.login_failed", status_code=401, commit=False)
        record(c, "auth.login", status_code=200)                            # a success does not count
        assert M.r_auth_failures(c) == (False, "warning", "19 failed / denied authentication events in the last hour")
        record(c, "authz.denied", status_code=403)
        assert M.r_auth_failures(c) == (True, "warning", "20 failed / denied authentication events in the last hour")
    finally:
        c.close()


def test_backup_rule_alerts_on_a_failed_or_stale_backup(ops_env):
    from ops.monitor import r_backup
    c = _conn()
    now = datetime.now()
    try:
        c.execute("INSERT INTO ops_heartbeat (component, beat_at) VALUES ('scheduler', ?)", (now - timedelta(hours=1),))
        c.commit()
        assert r_backup(c) == (False, "warning", "no verified backup yet")     # a new install gets 26 h
        c.execute("UPDATE ops_heartbeat SET beat_at=?", (now - timedelta(hours=27),))
        c.commit()
        assert r_backup(c) == (True, "warning", "no verified backup yet")
        ins = "INSERT INTO ops_backup (backup_id, kind, started_at, finished_at, status, error) VALUES (?,?,?,?,?,?)"
        c.execute(ins, ("b-old", "scheduled", now - timedelta(hours=30), now - timedelta(hours=30), "VERIFIED", None))
        c.commit()
        assert r_backup(c) == (True, "warning", "latest verified backup is 30 h old")
        c.execute(ins, ("b-new", "scheduled", now - timedelta(hours=2), now - timedelta(hours=2), "VERIFIED", None))
        c.commit()
        assert r_backup(c) == (False, "warning", "latest verified backup is 2 h old")
        c.execute(ins, ("b-bad", "manual", now - timedelta(minutes=5), now - timedelta(minutes=5), "FAILED",
                        "disk full"))
        c.commit()
        assert r_backup(c) == (True, "critical", "latest backup FAILED: disk full")
        _config(ops={"backup_enabled": False})
        assert r_backup(c) == (False, "info", "backups disabled")
    finally:
        c.close()


def test_monitor_notifies_when_a_rule_starts_firing_throttles_then_resolves_once(ops_env, monkeypatch):
    from ops import monitor as M

    def boom(conn):
        raise ZeroDivisionError

    monkeypatch.setattr(M, "RULES", {"risk_failures": M.r_risk_failures, "broken": boom})
    sent = []
    monkeypatch.setattr(M, "_notify", lambda rule, sev, msg: sent.append((rule, sev, msg)))
    c = _conn()
    state = lambda: c.execute("SELECT status, count, first_at, notified_at, resolved_at, message FROM ops_alert "
                              "WHERE rule='risk_failures'").fetchone()
    try:
        _run(c, "execution_cycle", "FAILED", date.today())
        out = M.evaluate(c, notify=False)                     # recorded, but nobody told yet
        assert out[0] == {"rule": "risk_failures", "firing": True, "severity": "warning",
                          "message": "execution / risk cycle failed 1 time(s) today"}
        assert out[1] == {"rule": "broken", "firing": False, "severity": "info",
                          "message": "rule error: ZeroDivisionError"}           # a broken rule never stops the rest
        assert sent == [] and state()["notified_at"] is None
        M.evaluate(c)                                          # the first notifying run tells
        assert sent == [("risk_failures", "warning", "execution / risk cycle failed 1 time(s) today")]
        s1 = state()
        assert (s1["status"], s1["count"]) == ("FIRING", 2) and s1["notified_at"] is not None

        _run(c, "execution_cycle", "FAILED", date.today())
        M.evaluate(c)                                          # still firing 15 min later: no repeat
        s2 = state()
        assert len(sent) == 1 and s2["count"] == 3 and s2["message"].endswith("failed 2 time(s) today")
        assert (s2["first_at"], s2["notified_at"]) == (s1["first_at"], s1["notified_at"])

        c.execute("UPDATE ops_alert SET notified_at=? WHERE rule='risk_failures'",
                  (datetime.now() - timedelta(hours=6, minutes=1),))
        c.commit()
        M.evaluate(c)                                          # still firing after 6 h: one reminder
        assert len(sent) == 2 and sent[-1][0] == "risk_failures"

        c.execute("DELETE FROM pipeline_log WHERE job_name='execution_cycle'")
        c.commit()
        M.evaluate(c)                                          # fixed: one "resolved" message ...
        assert sent[-1] == ("risk_failures", "info", "resolved: risk_failures") and len(sent) == 3
        s3 = state()
        assert s3["status"] == "RESOLVED" and s3["resolved_at"] is not None
        M.evaluate(c)                                          # ... and then silence
        assert len(sent) == 3

        _run(c, "execution_cycle", "FAILED", date.today())
        M.evaluate(c)                                          # a new episode notifies and restarts first_at
        s4 = state()
        assert len(sent) == 4 and s4["status"] == "FIRING" and s4["resolved_at"] is None
        assert s4["first_at"] > s1["first_at"]
    finally:
        c.close()


def test_run_monitor_records_its_alerts_in_the_alert_log(ops_env, monkeypatch):
    """The monitor's alerts go through alerts.telegram.notify -> alert_log (the dashboard panel).
    A regression shows up here as 'database is locked' (and a stall of busy_timeout per alert)."""
    import db.schema as S
    from ops import monitor as M
    monkeypatch.setattr(S, "BUSY_TIMEOUT_MS", 2000)
    c = _conn()
    _run(c, "execution_cycle", "FAILED", date.today())
    c.close()
    out = M.run_monitor()
    assert out["status"] == "SUCCESS" and out["rows"] == len(M.RULES) and "risk_failures" in out["firing"]
    q = ("SELECT severity, message, dedupe_key FROM alert_log WHERE category='ops' AND message LIKE "
         "'%execution / risk cycle%'")
    c = _conn()
    try:
        logged = c.execute(q).fetchall()
        assert [(r["severity"], r["message"]) for r in logged] == \
            [("warning", "[ATIP ops] execution / risk cycle failed 1 time(s) today")]
        assert logged[0]["dedupe_key"].startswith("ops:risk_failures:warning:")
        st = c.execute("SELECT status, count, notified_at FROM ops_alert WHERE rule='risk_failures'").fetchone()
        assert (st["status"], st["count"]) == ("FIRING", 1) and st["notified_at"] is not None
    finally:
        c.close()
    M.run_monitor()                                            # the next run does not repeat it
    c = _conn()
    try:
        assert len(c.execute(q).fetchall()) == 1
        assert c.execute("SELECT count FROM ops_alert WHERE rule='risk_failures'").fetchone()[0] == 2
    finally:
        c.close()


# ── REL-001: wealth-track health ───────────────────────────────────────────────

MIG_0005 = "migration 0005 (append-only wealth audit tables) not applied"


def test_wealth_health_degraded_until_migration_0005_is_applied(ops_env):
    from ops import health as H
    from ops.migrations import apply, status
    from wealth.health import check
    c = _conn()
    try:
        d = check(c)
        assert (d["status"], d["enabled"], d["missing_tables"], d["append_only_migration"]) == \
            ("DEGRADED", False, [], "PENDING")
        assert d["problems"] == [MIG_0005]
        p = H.component("wealth")
        assert p["status"] == "DEGRADED" and H.http_status(p) == 200
        res = apply(c)
        assert "0005" in [r["version"] for r in res] and {r["status"] for r in res} == {"APPLIED"}
        assert status(c)["pending"] == []
        d = check(c)
        assert (d["status"], d["append_only_migration"], d["problems"]) == ("READY", "APPLIED", [])
        assert c.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='trg_perf_ledger_no_update'"
                         ).fetchone()
        c.execute("UPDATE schema_migrations SET status='FAILED' WHERE version='0005'")
        c.commit()
        assert check(c)["problems"] == [MIG_0005]                       # a FAILED 0005 is not applied either
    finally:
        c.close()


def test_health_wealth_endpoint_is_public_and_503_only_when_a_table_is_missing(ops_env):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard.server import app
    from ops.migrations import apply
    client = TestClient(app)
    r = client.get("/health/wealth")
    assert r.status_code == 200 and r.json()["status"] == "DEGRADED" and r.json()["problems"] == [MIG_0005]
    c = _conn()
    try:
        apply(c)
        r = client.get("/health/wealth")
        assert r.status_code == 200 and (r.json()["status"], r.json()["problems"]) == ("READY", [])
        c.execute("DROP TABLE wealth_cycle_run")
        c.commit()
    finally:
        c.close()
    r = client.get("/health/wealth")
    assert r.status_code == 503 and r.json()["status"] == "FAILED"
    assert r.json()["missing_tables"] == ["wealth_cycle_run"]


def test_wealth_cycle_rule_fires_for_a_stale_or_partial_owner_cycle(ops_env):
    from ops.monitor import r_wealth_cycle
    from wealth.health import check, recent_cycle_problem
    c = _conn()
    now = datetime.now()
    ins = "INSERT INTO wealth_cycle_run (tenant_id, owner_id, run_date, status, created_at) VALUES (?,?,?,?,?)"
    try:
        assert recent_cycle_problem(c) == (False, "wealth cycle disabled")
        _config(wealth={"enabled": True})
        c.executemany("INSERT INTO investor_profile (tenant_id, owner_id, profile_id) VALUES (?,?,?)",
                      [("default", "owner", "P1"), ("uat", "persona_a", "P2")])     # UAT personas never alert
        c.commit()
        assert recent_cycle_problem(c) == (True, "default:owner no investor cycle in 3 days")
        c.execute(ins, ("default", "owner", str(date.today() - timedelta(days=4)), "SUCCESS", now - timedelta(days=4)))
        c.commit()
        assert recent_cycle_problem(c) == (True, "default:owner no investor cycle in 3 days")
        c.execute(ins, ("default", "owner", str(date.today()), "PARTIAL", now - timedelta(hours=1)))
        c.commit()
        assert recent_cycle_problem(c) == (True, "default:owner latest cycle PARTIAL")
        assert r_wealth_cycle(c) == (True, "warning", "default:owner latest cycle PARTIAL")
        c.execute(ins, ("default", "owner", str(date.today()), "SUCCESS", now))
        c.commit()
        assert r_wealth_cycle(c) == (False, "warning", "wealth cycle healthy")
        d = check(c)                       # with the track on, stale benchmark prices degrade health too
        assert d["status"] == "DEGRADED" and d["benchmark_prices_as_of"] is None
        assert "NIFTY50 prices older than 5 days (performance and allocation inputs stale)" in d["problems"]
    finally:
        c.close()


# ── SEC-03: audit export ───────────────────────────────────────────────────────

def test_audit_export_writes_a_verified_segment_and_an_offbox_copy(ops_env):
    from enterprise import audit_export as AX
    from enterprise.audit import record
    exp, box = ops_env / "exports", ops_env / "offbox"
    _config(audit={"export_dir": str(exp), "offbox_dir": str(box)})
    c = _conn()
    try:
        for i, act in enumerate(("auth.login", "user.create", "role.grant")):
            record(c, act, tenant_id="default", actor="owner", details={"n": i, "password": "hunter2"})
        out = AX.export(c)
        seg = exp / "enterprise_audit" / "0000000001-0000000003.jsonl.gz"
        assert (out["rows"], out["first_id"], out["last_id"], out["path"]) == (3, 1, 3, str(seg))
        assert out["chain"] == {"ok": True, "checked": 3, "first_break": None, "unhashed": 0}
        with gzip.open(seg, "rt", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f]
        head = lines[0]["_segment"]
        assert (head["source"], head["first_id"], head["last_id"], head["rows"], head["chain"]["ok"]) == \
            ("enterprise_audit", 1, 3, 3, True)
        assert [r["action"] for r in lines[1:]] == ["auth.login", "user.create", "role.grant"]
        assert json.loads(lines[1]["details_json"]) == {"n": 0, "password": "***"}     # secrets never written
        assert lines[2]["prev_hash"] == lines[1]["row_hash"]                               # chain travels along
        assert (exp / "enterprise_audit" / (seg.name + ".sha256")).read_text(encoding="utf-8").split() == \
            [hashlib.sha256(seg.read_bytes()).hexdigest(), seg.name] == [out["sha256"], seg.name]
        copy = box / "enterprise_audit" / seg.name
        assert out["offbox_path"] == str(copy) and copy.read_bytes() == seg.read_bytes()
        assert AX.verify_segment(copy)["ok"] is True
        assert tuple(c.execute("SELECT source, first_id, last_id, rows, chain_ok, offbox_path FROM audit_export"
                               ).fetchone()) == ("enterprise_audit", 1, 3, 3, 1, str(copy))

        assert AX.export(c) == {"source": "enterprise_audit", "rows": 0, "note": "nothing new to export"}
        record(c, "a4")
        record(c, "a5")
        nxt = AX.export(c)                                     # the next segment starts after the last one
        assert (nxt["first_id"], nxt["last_id"], Path(nxt["path"]).name) == (4, 5, "0000000004-0000000005.jsonl.gz")
        st = AX.status(c)
        assert st["sources"]["enterprise_audit"]["pending_rows"] == 0 and "warning" not in st
        assert st["sources"]["enterprise_audit"]["last_export"]["last_id"] == 5

        copy.write_bytes(copy.read_bytes() + b"\0")            # altered after export
        assert AX.verify_segment(copy)["ok"] is False
    finally:
        c.close()


def test_a_broken_audit_chain_is_exported_anyway_flagged_and_alerted(ops_env, monkeypatch):
    import alerts.telegram as tg
    from enterprise import audit_export as AX
    from enterprise.audit import record
    from ops.migrations import apply
    _config(audit={"export_dir": str(ops_env / "exports")})
    sent = []
    monkeypatch.setattr(tg, "notify", lambda msg, **kw: sent.append((msg, kw)))
    c = _conn()
    try:
        for a in ("a1", "a2", "a3"):
            record(c, a)
        # migration 0004's append-only trigger is not installed yet, so a row can still be edited
        c.execute("UPDATE enterprise_audit SET action='forged' WHERE id=2")
        c.commit()
        out = AX.export(c)
        assert out["rows"] == 3 and Path(out["path"]).exists()
        assert (out["chain"]["ok"], out["chain"]["first_break"], out["chain"]["reason"]) == \
            (False, 2, "row content changed")
        assert c.execute("SELECT chain_ok FROM audit_export").fetchone()[0] == 0
        assert len(sent) == 1 and sent[0][1]["severity"] == "critical" and "Audit chain broken" in sent[0][0]
        assert AX.status(c)["warning"] == "audit.offbox_dir is not set: exports stay on this machine only"

        c.executemany("INSERT INTO oms_order_event (order_id, to_status, at) VALUES (?,?,?)",
                      [("O1", "NEW", datetime.now()), ("O1", "FILLED", datetime.now())])
        c.commit()
        ev = AX.export(c, "oms_order_event")                   # the order trail has no hash chain
        assert (ev["rows"], ev["chain"], Path(ev["path"]).name) == (2, None, "0000000001-0000000002.jsonl.gz")
        with pytest.raises(ValueError):
            AX.export(c, "enterprise_user")

        apply(c)                                               # 0004: the tables refuse edits from now on
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            c.execute("UPDATE enterprise_audit SET action='forged again' WHERE id=3")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            c.execute("DELETE FROM oms_order_event WHERE id=1")
    finally:
        c.close()


# ── API-05: generated API reference ────────────────────────────────────────────

def _app():
    pytest.importorskip("fastapi")
    from dashboard.server import app
    return app


def test_api_reference_lists_every_route_of_the_app():
    from ops.api_docs import collect
    app = _app()
    want = Counter((m, r.path) for r in app.routes for m in (getattr(r, "methods", None) or [])
                   if m != "HEAD" and not r.path.startswith(("/openapi", "/docs", "/redoc")))
    got = collect(app)
    assert Counter((r["method"], r["path"]) for r in got) == want and len(got) > 100
    assert got == sorted(got, key=lambda r: (r["path"], r["method"]))
    assert all(r["permission"] for r in got)


def test_api_reference_labels_each_route_with_what_authz_requires():
    import re
    from enterprise.authz import SELF, _public, permission_for
    from ops.api_docs import collect
    from ops.health import COMPONENTS
    rows = {(r["method"], r["path"]): r for r in collect(_app())}
    # hand-derived from enterprise/authz.py PUBLIC / SELF / ROUTE_RULES
    for (m, p), perm, token in [(("POST", "/api/auth/login"), "public", False),
                                (("GET", "/api/auth/me"), "signed-in", False),
                                (("GET", "/health"), "public", False),
                                (("POST", "/api/webhooks/{source}"), "public", False),
                                (("GET", "/api/admin/audit"), "audit:read", False),
                                (("GET", "/api/ops/backups"), "system:operate", False),
                                (("POST", "/api/ops/backups"), "system:operate", True),
                                (("GET", "/api/orders"), "execution:read", False),
                                (("POST", "/api/orders"), "orders:manage", True),
                                (("GET", "/api/wealth/goals"), "wealth:read", False),
                                (("DELETE", "/api/wealth/goals/{gid}"), "wealth:write", True)]:
        assert (rows[(m, p)]["permission"], rows[(m, p)]["token"]) == (perm, token), (m, p)
    # every route without a path parameter: exactly the middleware's order of checks
    for (m, p), r in rows.items():
        if "{" not in p:
            want = "public" if _public(m, p) else "signed-in" if re.match(SELF, p) else permission_for(m, p)
            assert r["permission"] == want, (m, p)
    # GET /health/{component}: the middleware lets every component through unauthenticated
    assert all(_public("GET", f"/health/{comp}") for comp in COMPONENTS)
    label = rows[("GET", "/health/{component}")]["permission"]
    assert label.startswith("public"), label
    listed = set(label[len("public for "):label.index(";")].split(", "))
    assert set(COMPONENTS) <= listed and label.endswith("otherwise dashboard:read")


def test_api_reference_markdown_has_one_table_row_per_route(tmp_path):
    import re
    from ops.api_docs import collect, write
    app = _app()
    out = write(app, out_dir=str(tmp_path / "docs"))
    n = len(collect(app))
    md = (tmp_path / "docs" / "API_REFERENCE.md").read_text(encoding="utf-8")
    assert out["routes"] == n and f"({n} method + path pairs)" in md
    rows = [ln for ln in md.splitlines() if ln.startswith("| ") and not ln.startswith("| Method")]
    assert len(rows) == n
    assert all(len(re.findall(r"(?<!\\)\|", ln)) == 7 for ln in rows)        # six cells, none broken
    assert "| GET | `/health/{component}` | public for " in md
    spec = json.loads((tmp_path / "docs" / "openapi.json").read_text(encoding="utf-8"))
    assert "/api/ops/backups" in spec["paths"]


# ── DBS-02: retention purge ────────────────────────────────────────────────────

def _ago(n):
    return str(date.today() - timedelta(days=n))


def test_purge_dry_run_reports_and_apply_deletes_per_retention_tier(ops_env):
    from db import purge as P
    c = _conn()
    c.executemany("INSERT INTO prices_daily (symbol, date, close) VALUES (?,?,?)",
                  [("OLD", _ago(1100), 1.0), ("MID", _ago(800), 2.0), ("NEW", _ago(10), 3.0)])
    c.executemany("INSERT INTO ai_scores (symbol, date) VALUES (?,?)",
                  [("OLD", _ago(700)), ("EDGE", _ago(600)), ("NEW", _ago(100))])
    c.executemany("INSERT INTO pipeline_log (run_date, job_name, status) VALUES (?,?,?)",
                  [(_ago(120), "old_job", "SUCCESS"), (_ago(30), "new_job", "SUCCESS")])
    c.executemany("INSERT INTO live_quotes (symbol, ltp, timestamp) VALUES (?,?,?)",
                  [("OLD", 1.0, _ago(200) + " 10:00:00"), ("NEW", 2.0, _ago(0) + " 10:00:00")])
    c.commit()
    c.close()
    # cut-offs: price history 1000 d (beyond the 600 d long tier), long 600 d, short 90 d.
    # prices: OLD (1100 d) goes, MID (800 d) is kept by the history tier. ai_scores: OLD (700 d)
    # goes, EDGE sits ON the cut-off and stays. pipeline_log / live_quotes: the 120 / 200 d rows go.
    want = {"prices_daily": 1, "ai_scores": 1, "pipeline_log": 1, "live_quotes": 1}

    def rest(conn):
        return {t: sorted(r[0] for r in conn.execute(f"SELECT {col} FROM {t}"))
                for t, col in (("prices_daily", "symbol"), ("ai_scores", "symbol"), ("pipeline_log", "job_name"),
                               ("live_quotes", "symbol"))}

    res = P.purge_old_data(long_days=600, short_days=90, dry_run=True, history_days_=1000)
    assert {t: res[t] for t in want} == want
    assert all(v == 0 or str(v).startswith("error:") for t, v in res.items() if t not in want)
    c = _conn()
    try:
        assert rest(c) == {"prices_daily": ["MID", "NEW", "OLD"], "ai_scores": ["EDGE", "NEW", "OLD"],
                           "pipeline_log": ["db_purge", "new_job", "old_job"], "live_quotes": ["NEW", "OLD"]}
        assert [tuple(r) for r in c.execute("SELECT status, rows_processed FROM pipeline_log WHERE "
                                            "job_name='db_purge'")] == [("DRY_RUN", 4)]
    finally:
        c.close()
    res = P.purge_old_data(long_days=600, short_days=90, dry_run=False, history_days_=1000)
    assert {t: res[t] for t in want} == want
    c = _conn()
    try:
        assert rest(c) == {"prices_daily": ["MID", "NEW"], "ai_scores": ["EDGE", "NEW"],
                           "pipeline_log": ["db_purge", "db_purge", "new_job"], "live_quotes": ["NEW"]}
        assert [tuple(r) for r in c.execute("SELECT status, rows_processed FROM pipeline_log WHERE "
                                            "job_name='db_purge' ORDER BY id")] == [("DRY_RUN", 4), ("SUCCESS", 4)]
    finally:
        c.close()


def test_price_history_follows_history_years_and_is_never_shorter_than_the_long_tier(ops_env):
    from db import purge as P
    assert P.history_days() == round(7 * 365.25) + 7 == 2564          # default history_years 7
    _config(history_years=3)
    assert P.history_days() == round(3 * 365.25) + 7 == 1103
    c = _conn()
    c.executemany("INSERT INTO prices_daily (symbol, date, close) VALUES (?,?,?)",
                  [("A", _ago(700), 1.0), ("B", _ago(500), 1.0)])
    c.commit()
    c.close()
    # an explicit 100-day history is raised to the 600-day long tier: only A (700 d) would go
    assert P.purge_old_data(long_days=600, dry_run=True, history_days_=100)["prices_daily"] == 1
    # the configured 3 years (1103 d) keep both
    assert P.purge_old_data(long_days=600, dry_run=True)["prices_daily"] == 0
