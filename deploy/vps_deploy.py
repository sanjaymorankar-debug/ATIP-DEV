#!/usr/bin/env python3
"""
ATIP on the VPS: deploy a GitHub master commit, restart, check, and roll back -- one command (19 Nov 2026).
How-to for the owner and the VPS session: docs/VPS_DEPLOY.md.

Live ATIP runs on the bkesari VPS (bkesari-atip.service, /var/www/bkesari/atip, user bkesari). Until now every
deploy was a hand-run runbook (deliverables/2026-11-18/vps_deploy_4d793ea/VPS_DEPLOY_STEPS.md): pick the files,
git archive them over ssh, sha256sum -c, stop, copy, start, look at the journal. This tool does those steps,
in that order, and adds the checks a hand run can forget:

  * only a commit on GitHub master is deployed, and the server must be on the commit the tool last deployed
    (its files are checked against that commit before anything changes -- "drift" stops the deploy);
  * a DRY RUN by default: without --authorize it prints the plan and changes nothing;
  * the restart only happens in a safe moment (see WINDOWS below) and never while an ATIP job holds its
    lock (GET /health/scheduler on the server); --wait waits for the next safe moment instead of refusing;
  * the new files are staged and compiled on the server BEFORE ATIP is stopped, so ATIP is down ~30 s;
  * the replaced files are backed up, and so is the database (bkesari-atip-pgdump.service);
  * after the start it waits for a new process, /health/live and /health/ready, and watches 20 s for a crash
    loop. If that fails it puts the old files back, starts ATIP again and says ROLLED BACK.

It runs on the Mac (or wherever the VPS ssh works), never on the VPS: the server needs no GitHub access.
Nothing here touches broker settings, config.json, the database contents or orders.

    python3 deploy/vps_deploy.py --ssh "ssh vps" status                 # what runs, health, locks, windows
    python3 deploy/vps_deploy.py --ssh "ssh vps" deploy                 # dry run: GitHub master
    python3 deploy/vps_deploy.py --ssh "ssh vps" deploy --authorize --wait
    python3 deploy/vps_deploy.py --ssh "ssh vps" rollback --authorize   # back to the previous deploy
    python3 deploy/vps_deploy.py --ssh "ssh vps" restart --authorize --wait
    python3 deploy/vps_deploy.py --ssh "ssh vps" adopt c03fc29 --authorize   # first use: record what runs
    python3 deploy/vps_deploy.py windows                                # the safe-restart rules, no ssh

--ssh (or ATIP_VPS_SSH) is the ssh command for the server; the remote user needs passwordless sudo, as the
runbook's commands did. --ctl (or ATIP_VPS_CTL) /usr/local/sbin/bk-ctl: log in as the bkesari-only deploy user
(deploy/vps-separation/, 23 Nov 2026), whose sudo allows only bk-ctl as root and anything as the app user; then the
tool starts / stops / restarts units and reads the journal through bk-ctl and cannot touch GoKesari or agtci. The tool works from any checkout of the repository (--repo, default: the checkout
the current directory is in); from a checkout without it:
    git -C /Users/agtci/ATIP show origin/master:deploy/vps_deploy.py > "$TMPDIR/vps_deploy.py"
    python3 "$TMPDIR/vps_deploy.py" --repo /Users/agtci/ATIP --ssh "ssh vps" status

WINDOWS (IST, every day; `windows` prints them). A restart is refused
  * from 05:45 to 15:35 -- the 06:00 resume, the Dhan token renewals (06:30/08:00/09:00), pre-market and the
    market -- and around the evening's heavy jobs (NO_GO below);
  * except in a "quiet minute": every ATIP job time is on a 5-minute mark, so the stop/start happens only in
    minute 1 or 2 after one (hh:01-hh:02:59, hh:06-hh:07:59, ...); tests/test_vps_deploy.py fails if a job
    is ever scheduled on a quiet minute outside the no-go periods;
  * while a job holds its lock (ops_job_lock, shown by /health/scheduler), whatever the time.
--outside-window REASON skips the first two (an urgent fix in market hours; the reason goes in the history),
never the lock check (--skip-lock-check REASON, for a lock you know to be stale).

STATE on the server (--state-dir, default /var/www/bkesari/atip-deploys, owned by the app user):
    DEPLOYED.json   the commit that runs, the one before it, when, by which command
    history.log     one line per deploy / rollback / restart / adopt
    backups/        <time>_<from>_<to>.tar.gz (the replaced files) + .added (files the deploy created)
Exit codes: 0 done or dry run, 2 refused (precondition), 3 not a safe moment, 4 failed and ROLLED BACK,
5 failed and the rollback did not come back healthy (look now).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:                                       # pragma: no cover - Python < 3.9
    from datetime import timezone
    IST = timezone(timedelta(hours=5, minutes=30))

# ── safe-restart rules ───────────────────────────────────────────────────────────────────────────────
# (from, to, why): no restart from `from` up to (not including) `to`, IST, every day.
NO_GO = [
    ("05:45", "15:35", "06:00 resume, Dhan token renewals 06:30/08:00/09:00, pre-market and market hours"),
    ("15:45", "16:15", "15:50 tick close-out and the 16:00 post-market RCA"),
    ("16:17", "16:25", "16:20 options settlement and the news-impact check"),
    ("16:42", "17:20", "16:45 post-market pipeline, its 16:48 data-quality check and the 17:00/17:10 slots"),
    ("18:27", "18:40", "18:30 post-market catch-up"),
    ("19:12", "19:35", "19:15 database backup, 19:30 late EOD and audit export"),
    ("19:42", "20:05", "19:45 alternative data and the 19:50 NSE Bhavcopy archive"),
]
QUIET_MINUTES = (1, 2)          # minute % 5 at which a stop/start may begin (jobs fire on minute % 5 == 0)
LEADER_LOCK = "scheduler_leader"  # the running scheduler's own lease (ops/jobs.py LEADER): always held
LOCK_TTL = timedelta(hours=6)   # ops/jobs.py DEFAULT_TTL: an older lock row has expired
POLL_S = 20

# ── what a deploy may change ─────────────────────────────────────────────────────────────────────────
NEVER = ("atip_data/", ".venv/", "deploy/atip.env", "deploy/pg_password.txt")
PACKAGES = ("requirements.txt", "requirements.lock.txt", "setup.py", "pyproject.toml")
NOT_RUNTIME = ("docs/", "tests/", "mobile/", ".github/", "deploy/")

DEFAULTS = {"app_dir": "/var/www/bkesari/atip", "state_dir": "/var/www/bkesari/atip-deploys",
            "service": "bkesari-atip.service", "app_user": "bkesari", "port": 8100,
            "db_backup_service": "bkesari-atip-pgdump.service", "root_paths": ("", "/atip")}


class Refused(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


# ═════════════════════════════════════ time rules ═══════════════════════════════════════════════════════
def now_ist() -> datetime:
    return datetime.now(IST)


def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def window_check(t: datetime) -> tuple[bool, str]:
    """(may a stop/start begin at t?, why not)."""
    m = t.hour * 60 + t.minute
    for a, b, why in NO_GO:
        if _hm(a) <= m < _hm(b):
            return False, f"{a}-{b} IST: {why}"
    if t.minute % 5 not in QUIET_MINUTES:
        return False, "not a quiet minute (ATIP jobs fire on 5-minute marks; restarts begin 1-2 minutes after one)"
    return True, ""


def next_safe(t: datetime) -> datetime:
    """t itself when it is safe, else the first safe minute after it."""
    x = t.replace(second=0, microsecond=0)
    if window_check(x)[0]:
        return t
    for _ in range(2 * 24 * 60):
        x += timedelta(minutes=1)
        if window_check(x)[0]:
            return x
    return x                                            # pragma: no cover


# ═════════════════════════════════════ local git ════════════════════════════════════════════════════════
class Repo:
    def __init__(self, path):
        self.path = str(path)

    def git(self, *args, input=None) -> bytes:
        p = subprocess.run(["git", "-C", self.path, *args], input=input, capture_output=True)
        if p.returncode:
            raise Refused(2, f"git {' '.join(args[:3])}: {p.stderr.decode(errors='replace').strip()}")
        return p.stdout

    def fetch(self):
        self.git("fetch", "--quiet", "origin")

    def commit(self, ref) -> str:
        return self.git("rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()

    def on_master(self, sha) -> bool:
        return subprocess.run(["git", "-C", self.path, "merge-base", "--is-ancestor", sha, "origin/master"],
                              capture_output=True).returncode == 0

    def log(self, base, target) -> list[str]:
        return self.git("log", "--oneline", "--no-decorate", f"{base}..{target}").decode().splitlines()

    def delta(self, base, target) -> dict:
        """{'A': [...], 'M': [...], 'D': [...]} -- renames as delete + add."""
        out = self.git("diff", "--no-renames", "--name-status", "-z", base, target).split(b"\0")
        d = {"A": [], "M": [], "D": []}
        it = iter(x for x in out if x)
        for st in it:
            path = next(it).decode()
            d["D" if st[:1] == b"D" else "A" if st[:1] == b"A" else "M"].append(path)
        return d

    def files(self, sha) -> list[str]:
        return [x.decode() for x in self.git("ls-tree", "-r", "-z", "--name-only", sha).split(b"\0") if x]

    def sha256s(self, sha, paths) -> dict:
        """{path: sha256 of the file at commit sha} (git cat-file --batch, one process)."""
        if not paths:
            return {}
        req = "".join(f"{sha}:{p}\n" for p in paths).encode()
        raw = self.git("cat-file", "--batch", input=req)
        out, i = {}, 0
        for p in paths:
            j = raw.index(b"\n", i)
            head = raw[i:j].split()
            if head[-1] == b"missing":
                out[p] = None
                i = j + 1
                continue
            size = int(head[2])
            out[p] = hashlib.sha256(raw[j + 1:j + 1 + size]).hexdigest()
            i = j + 1 + size + 1
        return out

    def archive(self, sha, paths) -> bytes:
        return self.git("archive", "--format=tar", sha, "--", *paths)


# ═════════════════════════════════════ the server ═══════════════════════════════════════════════════════
_HASH = r"""
import hashlib, sys
for p in sys.stdin.buffer.read().split(b"\0"):
    if not p:
        continue
    try:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        r = h.hexdigest()
    except FileNotFoundError:
        r = "-"
    except OSError as e:
        r = "!" + type(e).__name__
    sys.stdout.buffer.write(r.encode() + b"\t" + p + b"\0")
"""


class Server:
    def __init__(self, a):
        self.ssh = shlex.split(a.ssh)
        self.app, self.state, self.svc, self.user = a.app_dir, a.state_dir, a.service, a.app_user
        self.py = a.remote_python or f"{a.app_dir}/.venv/bin/python"
        self.port, self.roots, self.dbsvc = a.port, a.root_paths, a.db_backup_service
        self.ctl = getattr(a, "ctl", None)       # bk-ctl on the server: the bkesari-only root commands
        self._root = None

    # -- plumbing ---------------------------------------------------------------------------------------
    def sh(self, script, input=None, check=True, timeout=900) -> subprocess.CompletedProcess:
        p = subprocess.run(self.ssh + ["bash -c " + shlex.quote("set -euo pipefail\n" + script)],
                           input=input, capture_output=True, timeout=timeout)
        if check and p.returncode:
            err = (p.stderr or b"").decode(errors="replace").strip()[-600:]
            raise Refused(2, f"server command failed ({p.returncode}): {script.strip().splitlines()[0][:90]}"
                             f"\n    {err}")
        return p

    def as_user(self, cmd) -> str:
        return f"sudo -n -u {shlex.quote(self.user)} {cmd}"

    def in_dir(self, d, cmd) -> str:
        """cmd in directory d, as the app user (the ssh user may not be able to enter d)."""
        return self.as_user("bash -c " + shlex.quote(f"set -euo pipefail; cd {shlex.quote(d)}; {cmd}"))

    def q(self, s) -> str:
        return shlex.quote(s)

    # -- state ------------------------------------------------------------------------------------------
    def ensure_state(self):
        if self.ctl:
            self.sh(f"sudo -n {self.q(self.ctl)} init-state {self.q(self.state)}")
            return
        u = self.q(self.user)
        self.sh(f"sudo -n install -d -o {u} -g \"$(id -gn {u})\" -m 750 {self.q(self.state)} "
                f"{self.q(self.state + '/backups')} {self.q(self.state + '/staging')}")

    def marker(self) -> dict | None:
        p = self.sh(self.as_user(f"cat {self.q(self.state + '/DEPLOYED.json')} 2>/dev/null || true"))
        txt = p.stdout.decode().strip()
        return json.loads(txt) if txt else None

    def write(self, path, data: bytes, append=False):
        self.sh(self.as_user(f"tee {'-a ' if append else ''}{self.q(path)} >/dev/null"), input=data)

    def write_marker(self, m: dict):
        self.write(self.state + "/DEPLOYED.json", (json.dumps(m, indent=1) + "\n").encode())

    def history(self, line: str):
        self.write(self.state + "/history.log", (line.rstrip() + "\n").encode(), append=True)

    def lock(self, what):
        p = self.sh(self.as_user(f"mkdir {self.q(self.state + '/run.lock')}"), check=False)
        if p.returncode:
            raise Refused(2, f"another deploy/restart holds {self.state}/run.lock -- if none is running, "
                             f"remove that directory on the server")
        self.write(self.state + "/run.lock/what", f"{what} {now_ist():%Y-%m-%d %H:%M:%S}\n".encode())

    def unlock(self):
        self.sh(self.as_user(f"rm -rf {self.q(self.state + '/run.lock')}"), check=False)

    # -- files ------------------------------------------------------------------------------------------
    def hashes(self, base_dir, paths) -> dict:
        if not paths:
            return {}
        p = self.sh(self.in_dir(base_dir, f"exec {self.q(self.py)} -I -c {self.q(_HASH)}"),
                    input=b"\0".join(x.encode() for x in paths) + b"\0")
        out = {}
        for rec in p.stdout.split(b"\0"):
            if rec:
                h, _, path = rec.partition(b"\t")
                out[path.decode()] = h.decode()
        return out

    def stage(self, tar: bytes, sha: str) -> str:
        d = f"{self.state}/staging/{sha[:12]}"
        self.sh(self.as_user(f"rm -rf {self.q(d)}") + " && " + self.as_user(f"mkdir -p {self.q(d)}"))
        self.sh(self.as_user(f"tar -x -C {self.q(d)}"), input=tar)
        return d

    def compile(self, d, pyfiles):
        if pyfiles:
            self.sh(self.in_dir(d, f"PYTHONPYCACHEPREFIX={self.q(d + '/.pycache')} {self.q(self.py)} "
                                   f"-m py_compile " + " ".join(self.q(f) for f in pyfiles)))

    def list_file(self, path, paths):
        self.write(path, b"".join(x.encode() + b"\0" for x in paths))

    def backup(self, name, existing, added) -> str:
        bk = f"{self.state}/backups/{name}"
        self.list_file(bk + ".files", existing)
        self.list_file(bk + ".added", added)
        if existing:
            self.sh(self.in_dir(self.app, f"tar -czf {self.q(bk + '.tar.gz')} --null -T {self.q(bk + '.files')}"))
        return bk

    def install(self, staged, files, deleted, work):
        if files:
            lst = work + ".install"
            self.list_file(lst, files)
            self.sh(self.in_dir(staged, f"tar -cf - --null -T {self.q(lst)} | tar -x -C {self.q(self.app)} -f -"))
        if deleted:
            lst = work + ".delete"
            self.list_file(lst, deleted)
            self.sh(self.in_dir(self.app, f"xargs -0 rm -f -- < {self.q(lst)}"))

    def restore(self, bk):
        t, ad = self.q(bk + ".tar.gz"), self.q(bk + ".added")
        self.sh(self.in_dir(self.app, f"if [ -s {t} ]; then tar -xzf {t}; fi; "
                                      f"if [ -s {ad} ]; then xargs -0 rm -f -- < {ad}; fi"))

    def drop_staging(self, d):
        self.sh(self.as_user(f"rm -rf {self.q(d)}"), check=False)

    # -- service ----------------------------------------------------------------------------------------
    def systemctl(self, verb, unit=None):
        if self.ctl:
            self.sh(f"sudo -n {self.q(self.ctl)} unit {verb} {self.q(unit or self.svc)}", timeout=1800)
            return
        self.sh(f"sudo -n systemctl {verb} {self.q(unit or self.svc)}", timeout=1800)

    def service(self) -> dict:
        p = self.sh(f"systemctl show -p ActiveState -p MainPID -p NRestarts {self.q(self.svc)}", check=False)
        out = {}
        for line in p.stdout.decode().splitlines():
            k, _, v = line.partition("=")
            out[k] = v
        return {"active": out.get("ActiveState", "unknown"), "pid": int(out.get("MainPID") or 0),
                "restarts": int(out.get("NRestarts") or 0)}

    def db_backup(self):
        self.systemctl("start", self.dbsvc)
        r = self.sh(f"systemctl show -p Result --value {self.q(self.dbsvc)}").stdout.decode().strip()
        if r != "success":
            raise Refused(2, f"database backup {self.dbsvc} ended with result {r!r}: nothing was changed")

    def tracebacks_since(self, epoch: int) -> int:
        read = (f"sudo -n {self.q(self.ctl)} journal {self.q(self.svc)} {int(epoch)}" if self.ctl else
                f"sudo -n journalctl -u {self.q(self.svc)} --since @{epoch} --no-pager -q")
        p = self.sh(f"{read} 2>/dev/null | grep -c Traceback || true", check=False)
        try:
            return int(p.stdout.decode().strip() or 0)
        except ValueError:
            return 0

    def get(self, path) -> tuple[int, dict | None]:
        """(HTTP code, JSON) for an ATIP path on 127.0.0.1, with or without the /atip prefix."""
        roots = [self._root] if self._root is not None else list(self.roots)
        code, body = 0, None
        for r in roots:
            url = f"http://127.0.0.1:{self.port}{r}{path}"
            p = self.sh(f"u={self.q(url)}; if command -v curl >/dev/null 2>&1; then "
                        f"curl -s -m 5 -w '\\n%{{http_code}}' \"$u\" || true; else "
                        f"{self.q(self.py)} -I -c {self.q(_GET)} \"$u\"; fi", check=False)
            txt = p.stdout.decode(errors="replace")
            head, _, last = txt.rpartition("\n")
            try:
                code = int(last.strip() or 0)
            except ValueError:
                code = 0
            try:
                body = json.loads(head) if head.strip() else None
            except ValueError:
                body = None
            if code and code != 404:
                if self._root is None and code == 200:
                    self._root = r
                return code, body
        return code, body

    def running_jobs(self, now: datetime) -> tuple[list[str] | None, str]:
        """(jobs holding a lock now, note). None when the scheduler cannot be asked."""
        code, body = self.get("/health/scheduler")
        if code not in (200, 503) or not isinstance(body, dict) or "locks" not in body:
            return None, f"/health/scheduler did not answer (HTTP {code})"
        jobs = []
        for lk in body.get("locks") or []:
            if lk.get("job") == LEADER_LOCK:
                continue
            try:
                since = datetime.fromisoformat(str(lk.get("since"))[:19]).replace(tzinfo=IST)
                if now - since > LOCK_TTL:
                    continue                            # expired by ops/jobs.py's TTL
            except ValueError:
                pass
            jobs.append(f"{lk.get('job')} (since {str(lk.get('since'))[11:19]})")
        hb = body.get("heartbeat_age_s")
        return jobs, f"scheduler heartbeat {hb}s ago" if hb is not None else "no scheduler heartbeat"


_GET = r"""
import sys, urllib.request, urllib.error
try:
    r = urllib.request.urlopen(sys.argv[1], timeout=5); b, c = r.read(), r.status
except urllib.error.HTTPError as e:
    b, c = e.read(), e.code
except Exception:
    b, c = b"", 0
sys.stdout.write(b.decode("utf-8", "replace") + "\n" + str(c))
"""


# ═════════════════════════════════════ steps ════════════════════════════════════════════════════════════
def say(*lines):
    for x in lines:
        print(x, flush=True)


def gate(srv: Server, a, now_fn) -> tuple[bool, list[str]]:
    """(may ATIP be stopped right now?, reasons it may not)."""
    t = now_fn()
    why = []
    if not a.outside_window:
        ok, w = window_check(t)
        if not ok:
            why.append(w)
    if not a.skip_lock_check:
        st = srv.service()
        if st["active"] == "active":
            jobs, note = srv.running_jobs(t)
            if jobs is None:
                why.append(f"cannot tell whether a job is running: {note}")
            elif jobs:
                why.append("a job is running: " + ", ".join(jobs))
    return not why, why


def wait_gate(srv, a, now_fn, sleep, max_min) -> None:
    deadline = now_fn() + timedelta(minutes=max_min)
    first = True
    while True:
        ok, why = gate(srv, a, now_fn)
        if ok:
            return
        t = now_fn()
        if t >= deadline:
            nxt = next_safe(t)
            raise Refused(3, "not a safe moment to stop ATIP: " + "; ".join(why)
                          + f". Next window opens {nxt:%H:%M} IST" + ("" if a.outside_window else
                          " (--wait waits for it)"))
        if first:
            say(f"  waiting for a safe moment (up to {deadline:%H:%M} IST): " + "; ".join(why))
            first = False
        sleep(POLL_S)


def check_health(srv: Server, old_pid: int, started: int, now_fn, sleep, timeout_s=180, settle_s=20):
    """(ok, notes): a new process, /health/live + /health/ready, and no restart within settle_s."""
    notes = []
    st = srv.service()
    live = ready = 0
    rb = None
    for _ in range(max(1, timeout_s // 3)):
        st = srv.service()
        if st["active"] == "active" and st["pid"] and st["pid"] != old_pid:
            live, _lb = srv.get("/health/live")
            ready, rb = srv.get("/health/ready")
            if live == 200 and ready == 200 and (rb or {}).get("status") != "FAILED":
                break
        sleep(3)
    else:
        return False, [f"not healthy after {timeout_s}s: service {st['active']}, pid {st['pid']} "
                       f"(before {old_pid}), /health/live HTTP {live}, /health/ready HTTP {ready}"
                       + (f" {rb.get('status')}" if isinstance(rb, dict) and rb.get("status") else "")]
    pid, restarts = st["pid"], st["restarts"]
    sleep(settle_s)
    st2 = srv.service()
    if st2["active"] != "active" or st2["pid"] != pid or st2["restarts"] != restarts:
        return False, [f"restarted or stopped within {settle_s}s of coming up (pid {pid} -> {st2['pid']}, "
                       f"{st2['active']})"]
    n = srv.tracebacks_since(started)
    if n:
        notes.append(f"{n} Traceback(s) in the journal since the stop -- a job may have failed; read them")
    notes.append(f"pid {pid}, /health/live and /health/ready answer")
    return True, notes


def stamp(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S IST")


def refuse_paths(delta, a):
    every = delta["A"] + delta["M"] + delta["D"]
    bad = [p for p in every if any(p == n or p.startswith(n) for n in NEVER)]
    if bad:
        raise Refused(2, "this commit changes files a deploy never touches (secrets, data, the venv): "
                         + ", ".join(bad))
    pk = [p for p in every if p in PACKAGES]
    if pk and not a.packages_installed:
        raise Refused(2, "this commit changes " + ", ".join(pk) + ": install the packages on the server by hand "
                         "first (as the app user, in its .venv), then pass --packages-installed")


def drift(repo: Repo, srv: Server, base, target, delta) -> list[str]:
    """Files on the server that are not what `base` (the deployed commit) says."""
    want = repo.sha256s(base, delta["M"] + delta["D"])
    new = repo.sha256s(target, delta["A"])
    have = srv.hashes(srv.app, delta["M"] + delta["D"] + delta["A"])
    out = []
    for p, h in want.items():
        if have.get(p) != h:
            out.append(f"{p}: {'missing' if have.get(p) == '-' else 'differs from ' + base[:7]}")
    for p, h in new.items():
        if have.get(p) not in ("-", h):
            out.append(f"{p}: exists on the server although {base[:7]} does not have it")
    return out


def code_change(repo, srv, a, base, target, label, now_fn, sleep, marker_before) -> int:
    """Move the server from `base` to `target` (a deploy, or a rollback when target is older)."""
    delta = repo.delta(base, target)
    refuse_paths(delta, a)
    fwd, back = repo.log(base, target), repo.log(target, base)
    say(f"{label.upper()}: {base[:7]} -> {target[:7]} on {srv.svc}",
        f"  {len(fwd)} commit(s) added" + (f", {len(back)} taken out" if back else "") + ":")
    for c in (fwd + [f"(out) {x}" for x in back])[:15]:
        say(f"    {c}")
    if len(fwd) + len(back) > 15:
        say("    ...")
    say(f"  files: {len(delta['M'])} changed, {len(delta['A'])} new, {len(delta['D'])} deleted")
    for k, name in (("M", "changed"), ("A", "new"), ("D", "deleted")):
        for p in delta[k][:40]:
            say(f"    {name:8} {p}")
        if len(delta[k]) > 40:
            say(f"    ... and {len(delta[k]) - 40} more {name}")
    d = drift(repo, srv, base, target, delta)
    if d:
        raise Refused(2, f"the server is not on {base[:7]} -- nothing changed:\n    " + "\n    ".join(d[:30])
                         + ("\n    ..." if len(d) > 30 else "")
                         + "\n  (status --verify shows the whole tree; adopt records what really runs)")
    say(f"  server files match {base[:7]}: yes")
    ok, why = gate(srv, a, now_fn)
    t = now_fn()
    say(f"  restart now ({t:%H:%M} IST): " + ("allowed" if ok else "not yet -- " + "; ".join(why)))
    if not ok and not a.outside_window:
        say(f"  next window opens {next_safe(t):%H:%M} IST")
    say(f"  database backup first: {'no (--skip-db-backup)' if a.skip_db_backup else srv.dbsvc}")
    if not a.authorize:
        say("DRY RUN: nothing was changed. Add --authorize to do it"
            + ("" if ok else " (and --wait to wait for the window)") + ".")
        return 0

    files = delta["A"] + delta["M"]
    srv.ensure_state()
    srv.lock(f"{label} {base[:7]}->{target[:7]}")
    stopped = installed = done = False
    bk = staged = None
    restore_marker = marker_before or {"commit": base, "previous": None, "at": stamp(now_fn()),
                                       "by": f"{label} (restored after a failure)", "files": 0, "backup": None}
    try:
        wait_gate(srv, a, now_fn, sleep, a.wait or 0)
        # 1. stage, check and compile the new files -- ATIP keeps running
        if files:
            staged = srv.stage(repo.archive(target, files), target)
            want = repo.sha256s(target, files)
            got = srv.hashes(staged, files)
            bad = [p for p in files if got.get(p) != want[p]]
            if bad:
                raise Refused(2, "staged files do not match GitHub: " + ", ".join(bad[:10]))
            srv.compile(staged, [p for p in files if p.endswith(".py")])
            say(f"  staged and compiled {len(files)} file(s) on the server")
        # 2. backups
        name = f"{now_fn():%Y%m%d-%H%M%S}_{base[:7]}_{target[:7]}"
        bk = srv.backup(name, delta["M"] + delta["D"], delta["A"])
        say(f"  replaced files backed up: {bk}.tar.gz")
        if not a.skip_db_backup:
            say(f"  database backup: {srv.dbsvc} ...")
            srv.db_backup()
            say("  database backup: done")
        # 3. the backup may have run past the safe moment: wait (up to 10 minutes) for the next one
        wait_gate(srv, a, now_fn, sleep, 10)
        before = srv.service()
        started = int(time.time())
        say(f"  stopping {srv.svc} (pid {before['pid']}) at {now_fn():%H:%M:%S} IST")
        srv.systemctl("stop")
        stopped = True
        srv.install(staged, files, delta["D"], f"{srv.state}/backups/{name}")
        installed = True
        if files:
            got = srv.hashes(srv.app, files)
            want = repo.sha256s(target, files)
            bad = [p for p in files if got.get(p) != want[p]]
            if bad:
                raise Refused(2, "installed files do not match GitHub: " + ", ".join(bad[:10]))
        srv.write_marker({"commit": target, "previous": base, "at": stamp(now_fn()), "by": label,
                          "files": len(files) + len(delta["D"]), "backup": bk + ".tar.gz"})
        srv.systemctl("start")
        stopped = False
        ok, notes = check_health(srv, before["pid"], started, now_fn, sleep)
        if not ok:
            say("FAILED after the start: " + "; ".join(notes), "  rolling back ...")
            return roll_back(srv, bk, base, target, label, before["pid"], restore_marker, now_fn, sleep, notes)
        srv.history(f"{stamp(now_fn())}  {label.upper()} {base[:7]} -> {target[:7]}  OK  {notes[-1]}"
                    + (f"  (outside window: {a.outside_window})" if a.outside_window else "")
                    + (f"  (lock check skipped: {a.skip_lock_check})" if a.skip_lock_check else ""))
        done = True
        say(f"DONE: {srv.svc} runs {target[:7]} since {now_fn():%H:%M:%S} IST", *[f"  {n}" for n in notes],
            f"  undo: rollback --authorize (back to {base[:7]})")
        return 0
    except Refused as e:
        if stopped or installed:
            say(f"FAILED while ATIP was stopped: {e}", "  rolling back ...")
            return roll_back(srv, bk, base, target, label, 0, restore_marker, now_fn, sleep, [str(e)])
        raise
    except BaseException as e:                         # Ctrl-C, ssh dropped: never leave ATIP stopped
        if stopped or installed:
            say(f"INTERRUPTED while ATIP was stopped ({type(e).__name__}): restoring and starting it")
            roll_back(srv, bk, base, target, label, 0, restore_marker, now_fn, sleep, [type(e).__name__])
        raise
    finally:
        if staged:
            srv.drop_staging(staged)
        srv.unlock()


def roll_back(srv, bk, base, target, label, old_pid, restore_marker, now_fn, sleep, why) -> int:
    try:
        srv.systemctl("stop")
        if bk:
            srv.restore(bk)
        srv.write_marker(restore_marker)
        srv.systemctl("start")
        ok, notes = check_health(srv, old_pid, int(time.time()), now_fn, sleep)
    except Refused as e:
        ok, notes = False, [str(e)]
    srv.history(f"{stamp(now_fn())}  {label.upper()} {base[:7]} -> {target[:7]}  FAILED ({'; '.join(why)[:200]})"
                f"  ROLLED BACK to {base[:7]}: {'OK' if ok else 'NOT HEALTHY'}")
    if ok:
        say(f"ROLLED BACK: {srv.svc} runs {base[:7]} again; {notes[-1]}")
        return 4
    say("ROLLBACK DID NOT COME BACK HEALTHY: " + "; ".join(notes),
        f"  look now: journalctl -u {srv.svc} -n 200; backup {bk}.tar.gz")
    return 5


# ═════════════════════════════════════ commands ═════════════════════════════════════════════════════════
def cmd_windows(a, now_fn) -> int:
    t = now_fn()
    ok, why = window_check(t)
    say("ATIP restarts (IST, every day) -- refused in:")
    for x, y, w in NO_GO:
        say(f"  {x}-{y}  {w}")
    say(f"  and outside quiet minutes: a stop/start begins only at minute 1 or 2 after a 5-minute mark",
        f"  and while an ATIP job holds its lock (checked on the server at the time)",
        f"now {t:%a %H:%M} IST: " + ("a safe moment" if ok else f"not safe ({why}); next {next_safe(t):%H:%M} IST"))
    return 0


def cmd_status(a, repo, srv, now_fn) -> int:
    t = now_fn()
    m = srv.marker()
    st = srv.service()
    say(f"{srv.svc}: {st['active']}, pid {st['pid']}, restarts {st['restarts']}")
    if m:
        say(f"deployed: {m['commit'][:7]} ({m.get('by')}, {m.get('at')}); before it: "
            f"{(m.get('previous') or '-')[:7]}")
    else:
        say("deployed: unknown -- no DEPLOYED.json yet (adopt <commit> records what runs)")
    live, lb = srv.get("/health/live")
    ready, rb = srv.get("/health/ready")
    say(f"/health/live: HTTP {live}" + (f", up {lb.get('uptime_s')}s, started {lb.get('started_at')}" if lb else ""),
        f"/health/ready: HTTP {ready}" + (f", {rb.get('status')}" if rb else ""))
    jobs, note = srv.running_jobs(t)
    say(f"running jobs: {'unknown' if jobs is None else ', '.join(jobs) or 'none'} ({note})")
    ok, why = window_check(t)
    say(f"restart window now ({t:%H:%M} IST): " + ("open" if ok else f"closed ({why}); next {next_safe(t):%H:%M}"))
    try:
        repo.fetch()
        head = repo.commit("origin/master")
        say(f"GitHub master: {head[:7]}" + ("" if not m else " (= deployed)" if head == m["commit"] else
            f" ({len(repo.log(m['commit'], head))} commit(s) not deployed)"))
    except Refused as e:
        say(f"GitHub master: unknown ({e})")
    if a.verify:
        sha = repo.commit(a.commit) if a.commit else (m or {}).get("commit")
        if not sha:
            raise Refused(2, "--verify needs a deployed commit or --commit")
        return 0 if tree_report(repo, srv, sha)[0] else 2
    return 0


def tree_report(repo, srv, sha) -> tuple[bool, list[str]]:
    files = repo.files(sha)
    want = repo.sha256s(sha, files)
    have = srv.hashes(srv.app, files)
    runtime, other = [], []
    for p in files:
        if have.get(p) != want[p]:
            what = "missing" if have.get(p) == "-" else "differs"
            (other if p.startswith(NOT_RUNTIME) or p.endswith(".md") else runtime).append(f"{p} ({what})")
    say(f"server tree vs {sha[:7]}: {len(files) - len(runtime) - len(other)}/{len(files)} files match; "
        f"{len(runtime)} runtime and {len(other)} non-runtime (docs/tests/deploy) differences")
    for x in runtime[:40]:
        say(f"  RUNTIME  {x}")
    for x in other[:10]:
        say(f"  other    {x}")
    if len(other) > 10:
        say(f"  ... and {len(other) - 10} more non-runtime")
    return not runtime, runtime


def cmd_deploy(a, repo, srv, now_fn, sleep) -> int:
    repo.fetch()
    target = repo.commit(a.commit or "origin/master")
    if not repo.on_master(target):
        raise Refused(2, f"{target[:7]} is not on GitHub master: only master commits are deployed")
    m = srv.marker()
    if a.from_commit:
        base = repo.commit(a.from_commit)
        if m and m.get("commit") != base:
            raise Refused(2, f"DEPLOYED.json says {m['commit'][:7]}, --from says {base[:7]}: adopt the right one first")
    elif m:
        base = m["commit"]
    else:
        raise Refused(2, "the server has no DEPLOYED.json yet: run `adopt <commit it runs> --authorize` first "
                         "(status --verify --commit <sha> checks a guess)")
    if base == target:
        say(f"{srv.svc} already runs {target[:7]}: nothing to do")
        return 0
    return code_change(repo, srv, a, base, target, "deploy", now_fn, sleep, m)


def cmd_rollback(a, repo, srv, now_fn, sleep) -> int:
    repo.fetch()
    m = srv.marker()
    if not m or not m.get("previous"):
        raise Refused(2, "no previous deploy recorded in DEPLOYED.json: deploy <older commit> instead")
    base, target = m["commit"], m["previous"]
    if not repo.on_master(target):
        raise Refused(2, f"{target[:7]} is not on GitHub master")
    return code_change(repo, srv, a, base, target, "rollback", now_fn, sleep, m)


def cmd_restart(a, repo, srv, now_fn, sleep) -> int:
    before = srv.service()
    ok, why = gate(srv, a, now_fn)
    say(f"RESTART {srv.svc} (pid {before['pid']}, {before['active']})",
        f"  now ({now_fn():%H:%M} IST): " + ("allowed" if ok else "not yet -- " + "; ".join(why)))
    if not a.authorize:
        say("DRY RUN: nothing was changed. Add --authorize to restart" + ("" if ok else " (and --wait)") + ".")
        return 0
    srv.ensure_state()
    srv.lock("restart")
    try:
        wait_gate(srv, a, now_fn, sleep, a.wait or 0)
        before = srv.service()
        started = int(time.time())
        srv.systemctl("restart")
        ok, notes = check_health(srv, before["pid"], started, now_fn, sleep)
        srv.history(f"{stamp(now_fn())}  RESTART  {'OK' if ok else 'NOT HEALTHY'}  {'; '.join(notes)[:200]}"
                    + (f"  (outside window: {a.outside_window})" if a.outside_window else ""))
        say(("DONE: " if ok else "RESTARTED BUT NOT HEALTHY: ") + "; ".join(notes))
        return 0 if ok else 5
    finally:
        srv.unlock()


def cmd_adopt(a, repo, srv, now_fn) -> int:
    repo.fetch()
    sha = repo.commit(a.commit)
    if not repo.on_master(sha):
        raise Refused(2, f"{sha[:7]} is not on GitHub master")
    ok, runtime = tree_report(repo, srv, sha)
    if not ok:
        raise Refused(2, f"the server's runtime files are not {sha[:7]}: not recorded")
    m = srv.marker()
    if not a.authorize:
        say(f"DRY RUN: would record {sha[:7]} as deployed" + (f" (now {m['commit'][:7]})" if m else "")
            + ". Add --authorize.")
        return 0
    srv.ensure_state()
    srv.write_marker({"commit": sha, "previous": (m or {}).get("commit"), "at": stamp(now_fn()), "by": "adopt",
                      "files": 0, "backup": None})
    srv.history(f"{stamp(now_fn())}  ADOPT {sha[:7]}  (runtime files verified; nothing restarted)")
    say(f"recorded: {srv.svc} runs {sha[:7]}")
    return 0


# ═════════════════════════════════════ main ═════════════════════════════════════════════════════════════
def parse(argv):
    ap = argparse.ArgumentParser(prog="vps_deploy.py", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ssh", default=os.environ.get("ATIP_VPS_SSH"), help="ssh command for the VPS (or ATIP_VPS_SSH)")
    ap.add_argument("--repo", help="ATIP checkout to deploy from (default: the one the current directory is in)")
    ap.add_argument("--app-dir", default=DEFAULTS["app_dir"])
    ap.add_argument("--state-dir", default=DEFAULTS["state_dir"])
    ap.add_argument("--service", default=DEFAULTS["service"])
    ap.add_argument("--app-user", default=DEFAULTS["app_user"])
    ap.add_argument("--port", type=int, default=DEFAULTS["port"])
    ap.add_argument("--root-path", dest="root_paths", action="append", help="URL prefix of ATIP (default '' and /atip)")
    ap.add_argument("--remote-python", help="python on the server (default <app-dir>/.venv/bin/python)")
    ap.add_argument("--db-backup-service", default=DEFAULTS["db_backup_service"])
    ap.add_argument("--ctl", default=os.environ.get("ATIP_VPS_CTL") or None,
                    help="bk-ctl path on the server: start/stop/restart and journal through it (bkesari-only login)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def changes(p):
        p.add_argument("--authorize", action="store_true", help="really do it (default: dry run)")
        p.add_argument("--wait", type=int, nargs="?", const=180, metavar="MIN",
                       help="wait up to MIN minutes (default 180) for a safe moment instead of refusing")
        p.add_argument("--outside-window", metavar="REASON", help="skip the time rules (not the job-lock check)")
        p.add_argument("--skip-lock-check", metavar="REASON", help="skip the running-job check (a stale lock)")

    p = sub.add_parser("deploy", help="deploy a GitHub master commit (default: the tip)")
    p.add_argument("commit", nargs="?")
    p.add_argument("--from", dest="from_commit", help="the commit the server runs (only before the first adopt)")
    p.add_argument("--skip-db-backup", action="store_true")
    p.add_argument("--packages-installed", action="store_true", help="requirements changed and are installed")
    changes(p)
    p = sub.add_parser("rollback", help="go back to the commit before the last deploy")
    p.add_argument("--skip-db-backup", action="store_true")
    p.add_argument("--packages-installed", action="store_true")
    changes(p)
    p = sub.add_parser("restart", help="restart ATIP without a code change")
    changes(p)
    p = sub.add_parser("adopt", help="check the server against a commit and record it as deployed")
    p.add_argument("commit")
    p.add_argument("--authorize", action="store_true")
    p = sub.add_parser("status", help="what runs, health, running jobs, the restart window (read-only)")
    p.add_argument("--verify", action="store_true", help="check every tracked file on the server")
    p.add_argument("--commit", help="with --verify: compare with this commit instead of the deployed one")
    sub.add_parser("windows", help="print the safe-restart rules (no ssh)")
    a = ap.parse_args(argv)
    a.root_paths = tuple(a.root_paths) if a.root_paths else DEFAULTS["root_paths"]
    for k in ("authorize", "wait", "outside_window", "skip_lock_check", "skip_db_backup", "packages_installed",
              "from_commit", "commit", "verify"):
        if not hasattr(a, k):
            setattr(a, k, None)
    return a


def main(argv=None, now_fn=now_ist, sleep=time.sleep) -> int:
    a = parse(argv if argv is not None else sys.argv[1:])
    try:
        if a.cmd == "windows":
            return cmd_windows(a, now_fn)
        if not a.ssh:
            raise Refused(2, "--ssh (or ATIP_VPS_SSH) is needed: the ssh command for the VPS")
        top = a.repo or subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                                       text=True).stdout.strip()
        if not top:
            here = Path(__file__).resolve().parents[1]
            if not (here / ".git").exists():
                raise Refused(2, "not inside an ATIP checkout: pass --repo /path/to/ATIP")
            top = str(here)
        repo, srv = Repo(top), Server(a)
        if a.cmd == "status":
            return cmd_status(a, repo, srv, now_fn)
        if a.cmd == "adopt":
            return cmd_adopt(a, repo, srv, now_fn)
        if a.cmd == "deploy":
            return cmd_deploy(a, repo, srv, now_fn, sleep)
        if a.cmd == "rollback":
            return cmd_rollback(a, repo, srv, now_fn, sleep)
        if a.cmd == "restart":
            return cmd_restart(a, repo, srv, now_fn, sleep)
    except Refused as e:
        say(f"REFUSED: {e}")
        return e.code
    return 2                                            # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
