"""deploy/vps_deploy.py (19 Nov 2026): the one-command VPS deploy, against a fake server.

The fake server is a directory tree plus stubs on PATH for sudo, systemctl, journalctl and curl, reached
through a fake `ssh` that runs the remote command locally. "GitHub" is a bare repository on disk. The health
endpoints answer from what is deployed: an a.py containing BROKEN makes /health/ready fail, which is how the
automatic rollback is exercised. No network, no real service, no sudo.
"""
from __future__ import annotations

import getpass
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("vps_deploy", ROOT / "deploy" / "vps_deploy.py")
vd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vd)

IST = vd.IST

STUBS = {
    "fake_ssh": """#!/bin/bash
shift                                   # the host
export PATH="$FAKE_BIN:$PATH"
exec bash -c "$*"
""",
    "sudo": """#!/bin/bash
while [ $# -gt 0 ]; do
  case "$1" in -n) shift ;; -u) shift 2 ;; *) break ;; esac
done
exec "$@"
""",
    "systemctl": """#!/bin/bash
F="$FAKE"
echo "systemctl $*" >> "$F/calls.log"
unit="${@: -1}"
case "$1" in
  show)
    if [[ "$*" == *Result* ]]; then cat "$F/pgdump_result"; exit 0; fi
    st=$(cat "$F/state")
    echo "ActiveState=$st"
    if [ "$st" = active ]; then echo "MainPID=$(cat "$F/pid")"; else echo "MainPID=0"; fi
    echo "NRestarts=$(cat "$F/restarts")"
    ;;
  stop) echo inactive > "$F/state" ;;
  start|restart)
    if [[ "$unit" == *pgdump* ]]; then exit 0; fi
    echo active > "$F/state"; echo $(( $(cat "$F/pid") + 1 )) > "$F/pid"
    ;;
esac
""",
    "journalctl": """#!/bin/bash
cat "$FAKE/journal" 2>/dev/null
""",
    "curl": """#!/bin/bash
url="${@: -1}"
F="$FAKE"
if [ "$(cat "$F/state")" != active ]; then printf '\\n000'; exit 7; fi
case "$url" in
  */atip/health/live) printf '{"status":"LIVE","pid":%s,"uptime_s":3,"started_at":"x"}\\n200' "$(cat "$F/pid")" ;;
  */atip/health/ready)
    if grep -q BROKEN "$FAKE_APP/a.py" 2>/dev/null; then printf '{"status":"FAILED","database":"FAILED"}\\n503'
    else printf '{"status":"READY","database":"READY"}\\n200'; fi ;;
  */atip/health/scheduler) printf '{"status":"READY","heartbeat_age_s":12,"failed_jobs_24h":[],"locks":%s}\\n200' "$(cat "$F/locks.json")" ;;
  *) printf '{"detail":"Not Found"}\\n404' ;;
esac
""",
}


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


class Clock:
    def __init__(self, hhmm, day=datetime(2026, 11, 19)):
        h, m = map(int, hhmm.split(":"))
        self.t = day.replace(hour=h, minute=m, second=10, tzinfo=IST)

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += timedelta(seconds=s)


class World:
    def __init__(self, tmp: Path, monkeypatch):
        self.tmp = tmp
        self.origin = tmp / "origin.git"
        self.repo = tmp / "repo"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(self.origin)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "master", str(self.repo)], check=True)
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "remote", "add", "origin", str(self.origin))
        self.c = {}
        self.commit("c1", {"a.py": "X = 1\n", "pkg/b.py": "Y = 2\n", "old.py": "OLD = 1\n",
                           "docs/readme.md": "docs\n", "requirements.txt": "fastapi\n"})
        self.commit("c2", {"a.py": "X = 2\n", "pkg/new.py": "N = 1\n"}, delete=["old.py"])
        self.push()
        # the server: c1's tree, ATIP running
        self.app = tmp / "srv" / "app"
        self.state = tmp / "srv" / "state"
        self.fake = tmp / "srv" / "fake"
        self.bin = tmp / "bin"
        for d in (self.app, self.fake, self.bin):
            d.mkdir(parents=True)
        self.extract("c1")
        for name, body in STUBS.items():
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)
        (self.fake / "state").write_text("active\n")
        (self.fake / "pid").write_text("1000\n")
        (self.fake / "restarts").write_text("0\n")
        (self.fake / "pgdump_result").write_text("success\n")
        (self.fake / "locks.json").write_text(json.dumps([{"job": "scheduler_leader", "since": "2026-11-19 06:00:00"}]))
        (self.fake / "calls.log").write_text("")
        monkeypatch.setenv("FAKE", str(self.fake))
        monkeypatch.setenv("FAKE_APP", str(self.app))
        monkeypatch.setenv("FAKE_BIN", str(self.bin))
        monkeypatch.delenv("ATIP_VPS_SSH", raising=False)

    def commit(self, name, files, delete=()):
        for p, body in files.items():
            f = self.repo / p
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(body)
        for p in delete:
            (self.repo / p).unlink()
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", name)
        self.c[name] = git(self.repo, "rev-parse", "HEAD")
        return self.c[name]

    def push(self):
        git(self.repo, "push", "-q", "origin", "master")

    def extract(self, name):
        tar = subprocess.run(["git", "-C", str(self.repo), "archive", self.c[name]], check=True,
                             capture_output=True).stdout
        subprocess.run(["tar", "-x", "-C", str(self.app)], input=tar, check=True)

    def run(self, *args, clock=None, capsys=None):
        clock = clock or Clock("22:31")
        argv = ["--ssh", f"{self.bin}/fake_ssh vps", "--repo", str(self.repo), "--app-dir", str(self.app),
                "--state-dir", str(self.state), "--app-user", getpass.getuser(),
                "--remote-python", sys.executable, *args]
        code = vd.main(argv, now_fn=clock.now, sleep=clock.sleep)
        out = capsys.readouterr().out if capsys else ""
        return code, out

    def calls(self):
        return (self.fake / "calls.log").read_text()

    def marker(self):
        p = self.state / "DEPLOYED.json"
        return json.loads(p.read_text()) if p.exists() else None

    def history(self):
        p = self.state / "history.log"
        return p.read_text() if p.exists() else ""


@pytest.fixture
def w(tmp_path, monkeypatch):
    if not shutil.which("git"):
        pytest.skip("git not installed")
    return World(tmp_path, monkeypatch)


# ── the time rules ──────────────────────────────────────────────────────────────────────────────────────
def at(hhmm, sec=10):
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 11, 19, h, m, sec, tzinfo=IST)


@pytest.mark.parametrize("hhmm,ok", [
    ("10:01", False), ("15:31", False), ("06:31", False), ("05:46", False),   # market, morning
    ("16:01", False), ("16:46", False), ("19:16", False), ("19:51", False),   # heavy evening jobs
    ("20:00", False), ("20:03", False), ("20:04", False),                     # not a quiet minute
    ("20:06", True), ("20:07", True), ("21:31", True), ("23:21", True), ("02:12", True), ("05:41", True),
    ("15:36", True), ("17:21", True), ("18:41", True)])
def test_window_rules(hhmm, ok):
    assert vd.window_check(at(hhmm))[0] is ok


def test_next_safe_moment():
    assert vd.next_safe(at("10:00")).strftime("%H:%M") == "15:36"
    assert vd.next_safe(at("19:20")).strftime("%H:%M") == "19:36"
    assert vd.next_safe(at("20:06", 40)) == at("20:06", 40)          # already safe: now


def _scheduled_times():
    """Every wall-clock time an ATIP job is scheduled at (literal times, defaults of configured ones,
    and the generated loops), read from the code."""
    pat = re.compile(r'"(\d{1,2}):(\d{2})"')
    keys = (".at(", "_hhmm(", "_TIME", "time_ist", "times_ist", "_START", "_END", "backup_time", "drill_time")
    out = set()
    for p in list((ROOT / "pipeline").rglob("*.py")) + [ROOT / "ml" / "meta_label.py", ROOT / "data" / "news_impact.py",
                                                      ROOT / "data" / "asia_markets.py"]:
        for line in p.read_text(errors="ignore").splitlines():
            if any(k in line for k in keys):
                out |= {(int(h), int(m)) for h, m in pat.findall(line)}
    from pipeline import scheduler as s
    t = datetime.strptime(s.FII_DII_WATCH_START, "%H:%M")
    while t <= datetime.strptime(s.FII_DII_WATCH_END, "%H:%M"):
        out.add((t.hour, t.minute))
        t += timedelta(minutes=s.FII_DII_WATCH_EVERY)
    out |= {(h, m) for h in range(7, 24) for m in (10, 40)}            # job health checks
    return out


def test_no_job_is_scheduled_when_a_restart_may_begin():
    times = _scheduled_times()
    assert (16, 45) in times and (19, 50) in times and (17, 0) in times    # the scan sees the real schedule
    clash = sorted(f"{h:02d}:{m:02d}" for h, m in times
                   if vd.window_check(at(f"{h:02d}:{m:02d}", 0))[0])
    assert clash == [], f"jobs scheduled on a restart minute: {clash} -- update NO_GO / QUIET_MINUTES"


def test_windows_command_needs_no_ssh(capsys):
    assert vd.main(["windows"], now_fn=lambda: at("10:00")) == 0
    assert "next 15:36 IST" in capsys.readouterr().out


# ── deploy ──────────────────────────────────────────────────────────────────────────────────────────────
def test_dry_run_changes_nothing(w, capsys):
    code, out = w.run("deploy", "--from", w.c["c1"], capsys=capsys)
    assert code == 0, out
    assert "DRY RUN" in out and "server files match" in out and "restart now (22:31 IST): allowed" in out
    assert (w.app / "a.py").read_text() == "X = 1\n" and (w.app / "old.py").exists()
    assert "systemctl stop" not in w.calls() and "systemctl start" not in w.calls()
    assert not w.state.exists()


def test_deploy_installs_checks_and_restarts(w, capsys):
    code, out = w.run("deploy", "--from", w.c["c1"], "--authorize", capsys=capsys)
    assert code == 0, out
    assert (w.app / "a.py").read_text() == "X = 2\n"
    assert (w.app / "pkg" / "new.py").read_text() == "N = 1\n"
    assert not (w.app / "old.py").exists()
    m = w.marker()
    assert m["commit"] == w.c["c2"] and m["previous"] == w.c["c1"] and m["by"] == "deploy"
    calls = w.calls()
    assert calls.index("start bkesari-atip-pgdump.service") < calls.index("stop bkesari-atip.service") \
        < calls.rindex("start bkesari-atip.service")
    assert "DEPLOY " + w.c["c1"][:7] + " -> " + w.c["c2"][:7] + "  OK" in w.history()
    assert list((w.state / "backups").glob("*.tar.gz"))
    assert not list((w.state / "staging").iterdir())               # staging cleaned
    assert not (w.state / "run.lock").exists()
    assert "DONE" in out and "pid 1001" in out


def test_rollback_returns_to_the_previous_commit(w, capsys):
    assert w.run("deploy", "--from", w.c["c1"], "--authorize")[0] == 0
    code, out = w.run("rollback", "--authorize", capsys=capsys)
    assert code == 0, out
    assert (w.app / "a.py").read_text() == "X = 1\n" and (w.app / "old.py").read_text() == "OLD = 1\n"
    assert not (w.app / "pkg" / "new.py").exists()
    assert w.marker()["commit"] == w.c["c1"] and w.marker()["previous"] == w.c["c2"]
    assert "ROLLBACK" in w.history()


def test_a_server_that_drifted_is_not_touched(w, capsys):
    (w.app / "a.py").write_text("X = 99  # hand edit\n")
    code, out = w.run("deploy", "--from", w.c["c1"], "--authorize", capsys=capsys)
    assert code == 2 and "not on" in out and "a.py: differs" in out
    assert (w.app / "a.py").read_text() == "X = 99  # hand edit\n"
    assert "systemctl stop" not in w.calls()


def test_market_hours_are_refused_unless_a_reason_is_given(w, capsys):
    code, out = w.run("deploy", "--from", w.c["c1"], "--authorize", clock=Clock("10:01"), capsys=capsys)
    assert code == 3 and "09:15" not in out and "05:45-15:35" in out and "15:36" in out
    assert (w.app / "a.py").read_text() == "X = 1\n" and "systemctl stop" not in w.calls()
    code, out = w.run("deploy", "--from", w.c["c1"], "--authorize", "--outside-window", "urgent fix",
                      clock=Clock("10:01"), capsys=capsys)
    assert code == 0, out
    assert "outside window: urgent fix" in w.history()


def test_wait_holds_the_restart_until_the_window_opens(w, capsys):
    clock = Clock("19:20")
    code, out = w.run("deploy", "--from", w.c["c1"], "--authorize", "--wait", clock=clock, capsys=capsys)
    assert code == 0, out
    assert "waiting for a safe moment" in out
    stop_at = re.search(r"stopping bkesari-atip.service \(pid \d+\) at (\d\d:\d\d):\d\d", out).group(1)
    assert stop_at in ("19:36", "19:37")


def test_a_running_job_blocks_the_restart(w, capsys):
    (w.fake / "locks.json").write_text(json.dumps([
        {"job": "scheduler_leader", "since": "2026-11-19 06:00:00"},
        {"job": "fii_dii_watch", "since": "2026-11-19 22:30:30"}]))
    code, out = w.run("deploy", "--from", w.c["c1"], "--authorize", capsys=capsys)
    assert code == 3 and "a job is running: fii_dii_watch" in out and "systemctl stop" not in w.calls()


def test_the_leader_lease_and_an_expired_lock_do_not_block(w, capsys):
    (w.fake / "locks.json").write_text(json.dumps([
        {"job": "scheduler_leader", "since": "2026-11-19 06:00:00"},
        {"job": "dhan_15min_bars", "since": "2026-11-19 10:01:00"}]))     # > 6 h: expired
    code, out = w.run("deploy", "--from", w.c["c1"], "--authorize", capsys=capsys)
    assert code == 0, out


def test_a_build_that_does_not_come_up_is_rolled_back(w, capsys):
    w.commit("c3", {"a.py": "X = 3  # BROKEN\n"})
    w.push()
    w.extract("c2")
    (w.app / "old.py").unlink()
    code, out = w.run("deploy", "--from", w.c["c2"], "--authorize", capsys=capsys)
    assert code == 4, out
    assert "ROLLED BACK" in out
    assert (w.app / "a.py").read_text() == "X = 2\n"
    assert w.marker()["commit"] == w.c["c2"]
    assert "FAILED" in w.history() and "ROLLED BACK to " + w.c["c2"][:7] + ": OK" in w.history()
    assert open(w.fake / "state").read().strip() == "active"


def test_package_changes_need_a_hand_install(w, capsys):
    w.commit("c3", {"requirements.txt": "fastapi\nnewlib\n"})
    w.push()
    w.extract("c2")
    (w.app / "old.py").unlink()
    code, out = w.run("deploy", "--from", w.c["c2"], capsys=capsys)
    assert code == 2 and "requirements.txt" in out and "--packages-installed" in out
    assert w.run("deploy", "--from", w.c["c2"], "--packages-installed")[0] == 0


def test_only_github_master_is_deployed(w, capsys):
    w.commit("local", {"a.py": "X = 5\n"})                                   # not pushed
    code, out = w.run("deploy", w.c["local"], "--from", w.c["c1"], capsys=capsys)
    assert code == 2 and "not on GitHub master" in out


def test_the_first_deploy_asks_for_adopt(w, capsys):
    code, out = w.run("deploy", capsys=capsys)
    assert code == 2 and "adopt" in out


def test_adopt_records_what_runs_after_checking_every_file(w, capsys):
    code, out = w.run("adopt", w.c["c2"], "--authorize", capsys=capsys)
    assert code == 2 and "RUNTIME  a.py (differs)" in out and w.marker() is None
    code, out = w.run("adopt", w.c["c1"], "--authorize", capsys=capsys)
    assert code == 0, out
    assert w.marker()["commit"] == w.c["c1"]
    code, out = w.run("deploy", capsys=capsys)                               # no --from needed now
    assert code == 0 and "DRY RUN" in out and w.c["c2"][:7] in out


def test_restart_only(w, capsys):
    code, out = w.run("restart", "--authorize", capsys=capsys)
    assert code == 0, out
    assert (w.fake / "pid").read_text().strip() == "1001" and "RESTART  OK" in w.history()
    code, out = w.run("restart", "--authorize", clock=Clock("12:01"), capsys=capsys)
    assert code == 3


def test_status_is_read_only(w, capsys):
    code, out = w.run("status", capsys=capsys)
    assert code == 0, out
    assert "active, pid 1000" in out and "running jobs: none" in out and "GitHub master: " + w.c["c2"][:7] in out
    assert not w.state.exists() and "systemctl stop" not in w.calls() and "systemctl restart" not in w.calls()


def test_a_failed_database_backup_stops_before_anything_changes(w, capsys):
    (w.fake / "pgdump_result").write_text("exit-code\n")
    code, out = w.run("deploy", "--from", w.c["c1"], "--authorize", capsys=capsys)
    assert code == 2 and "database backup" in out
    assert (w.app / "a.py").read_text() == "X = 1\n" and "stop bkesari-atip.service" not in w.calls()
    assert not (w.state / "run.lock").exists()


def test_the_tool_runs_on_the_macs_python_39():
    py = Path("/usr/bin/python3")
    if not py.exists():
        pytest.skip("no /usr/bin/python3")
    r = subprocess.run([str(py), "-c", "import ast,sys; ast.parse(open(sys.argv[1]).read())",
                        str(ROOT / "deploy" / "vps_deploy.py")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
