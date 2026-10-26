"""
setup_atip.sh (the macOS setup / migration / deployment script) and the
`main.py --status` report it relies on.

The script tests run it with bash against a throwaway project in tmp_path, with
HOME pointed there too, so no real LaunchAgent, atip_data/ or .env is touched.
They cover what must never go wrong when moving ATIP to a new folder: copying
from a copy that is still running, losing a folder's own data, and a copy that
differs from its source.
"""

import fcntl
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "setup_atip.sh"

needs_bash = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash"),
                                reason="setup_atip.sh is a bash script for macOS")


# ── main.py --status ──────────────────────────────────────────────────────

def test_status_report_survives_null_rows_and_status(temp_db, capsys):
    from db.schema import init_db, get_connection
    from pipeline.scheduler import print_status
    init_db()
    c = get_connection()
    # close_indexes logs rows_processed as NULL; status is nullable too
    c.execute("INSERT INTO pipeline_log (run_date, job_name, start_time, status, rows_processed) "
              "VALUES ('2026-10-26', 'close_indexes', '2026-10-26 15:34:17', 'SUCCESS', NULL)")
    c.execute("INSERT INTO pipeline_log (run_date, job_name, start_time, status) "
              "VALUES ('2026-10-26', 'half_logged', '2026-10-26 15:35:00', NULL)")
    c.commit()
    c.close()
    print_status()
    out = capsys.readouterr().out
    assert "close_indexes" in out and "half_logged" in out
    assert "Database Row Counts" in out            # the report went on past the job list


# ── setup_atip.sh ─────────────────────────────────────────────────────────

def _run(project, *args, home):
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "ATIP_PYTHON": sys.executable,
           "LANG": "C.UTF-8"}
    return subprocess.run(["bash", str(project / "setup_atip.sh"), *args], cwd=project, env=env,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)


def _db(path, rows=(("ACME", "2026-10-23", 100.0),)):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE prices_daily (symbol TEXT, date DATE, close REAL)")
    c.executemany("INSERT INTO prices_daily VALUES (?,?,?)", rows)
    c.commit()
    c.close()


@pytest.fixture
def folders(tmp_path):
    """old/ = an ATIP folder with data; new/ = a fresh clone holding the script."""
    old, new, home = tmp_path / "old", tmp_path / "new", tmp_path / "home"
    for d in (old / "atip_data" / "raw", old / "pipeline", new, home):
        d.mkdir(parents=True)
    (old / "main.py").write_text("")
    _db(old / "atip_data" / "atip.db", [("ACME", "2026-10-23", 100.0), ("BETA", "2026-10-23", 50.0)])
    (old / "atip_data" / "config.json").write_text('{"dhan_client_id": "1"}')
    (old / "atip_data" / "raw" / "security_id_list.csv").write_text("x\n")
    (old / ".env").write_text("ANTHROPIC_API_KEY=test\n")
    shutil.copy(SCRIPT, new / "setup_atip.sh")
    (new / "main.py").write_text("")
    return old, new, home


@needs_bash
def test_script_parses_and_lists_every_command(tmp_path):
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    r = _run(ROOT, "help", home=tmp_path)
    assert r.returncode == 0
    for cmd in ("check", "install", "migrate", "data", "agents", "start", "stop", "verify", "all"):
        assert f"  {cmd}" in r.stdout
    assert _run(ROOT, "no-such-command", home=tmp_path).returncode == 2


@needs_bash
def test_migrate_copies_data_and_env_and_leaves_the_old_folder_alone(folders):
    old, new, home = folders
    before = {p: p.read_bytes() for p in old.rglob("*") if p.is_file()}
    r = _run(new, "migrate", "--from", str(old), home=home)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (new / "atip_data" / "atip.db").read_bytes() == (old / "atip_data" / "atip.db").read_bytes()
    assert (new / "atip_data" / "config.json").read_text() == '{"dhan_client_id": "1"}'
    assert (new / ".env").read_text() == "ANTHROPIC_API_KEY=test\n"
    assert (new / "atip_data" / ".migrated_from").read_text().split("\t")[0] == str(old)
    assert "2 daily prices, 2 symbols" in r.stdout
    assert {p: p.read_bytes() for p in old.rglob("*") if p.is_file()} == before


@needs_bash
def test_migrate_from_a_package_subfolder_uses_the_project(folders):
    old, new, home = folders
    r = _run(new, "migrate", "--from", str(old / "pipeline"), home=home)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (new / "atip_data" / "atip.db").exists()


@needs_bash
def test_migrate_refuses_while_the_old_copy_holds_its_lock(folders):
    old, new, home = folders
    lock = open(old / "atip_data" / "atip_single_instance.lock", "w")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)      # what a running main.py holds
    try:
        r = _run(new, "migrate", "--from", str(old), home=home)
    finally:
        lock.close()
    assert r.returncode == 1
    assert "still running" in r.stderr
    assert not (new / "atip_data").exists()


@needs_bash
def test_migrate_never_replaces_a_folders_own_data_without_a_yes(folders):
    old, new, home = folders
    (new / "atip_data").mkdir()
    _db(new / "atip_data" / "atip.db", [("MINE", "2026-10-24", 1.0)])
    r = _run(new, "migrate", "--from", str(old), home=home)            # no terminal, no --yes
    assert r.returncode == 1
    assert "nothing copied" in r.stderr
    assert sqlite3.connect(new / "atip_data" / "atip.db").execute(
        "SELECT symbol FROM prices_daily").fetchall() == [("MINE",)]

    r = _run(new, "migrate", "--from", str(old), "--yes", home=home)
    assert r.returncode == 0, r.stdout + r.stderr
    kept = list(new.glob("atip_data.before-migrate-*"))
    assert len(kept) == 1                                              # moved aside, not deleted
    assert sqlite3.connect(kept[0] / "atip.db").execute(
        "SELECT symbol FROM prices_daily").fetchall() == [("MINE",)]
    assert sqlite3.connect(new / "atip_data" / "atip.db").execute(
        "SELECT COUNT(*) FROM prices_daily").fetchone()[0] == 2


@needs_bash
def test_migrate_keeps_a_different_env_in_the_new_folder(folders):
    old, new, home = folders
    (new / ".env").write_text("ANTHROPIC_API_KEY=newer\n")
    r = _run(new, "migrate", "--from", str(old), home=home)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (new / ".env").read_text() == "ANTHROPIC_API_KEY=newer\n"
    assert "kept it" in r.stdout


@needs_bash
def test_migrate_rejects_its_own_folder_and_a_folder_without_data(folders, tmp_path):
    old, new, home = folders
    assert "give the OLD folder" in _run(new, "migrate", "--from", str(new), home=home).stderr
    (tmp_path / "empty").mkdir()
    assert "nothing to migrate" in _run(new, "migrate", "--from", str(tmp_path / "empty"), home=home).stderr
    assert "usage" in _run(new, "migrate", home=home).stderr
