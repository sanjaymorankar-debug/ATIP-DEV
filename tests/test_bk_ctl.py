"""deploy/vps-separation (23 Nov 2026): the bkesari-only deploy login on the shared VPS.

gk-db-1 runs GoKesari, bkesari (with ATIP) and agtci. bk-ctl is the only thing the bkesari deploy login may run as
root: start / stop / restart a unit on /etc/bk-ctl/units, read its journal, create a deploys folder. These tests run
the real script in its test mode (BK_CTL_TEST=1, never honoured as root) with systemctl / journalctl / flock stubs.
"""
from __future__ import annotations

import getpass
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SEP = ROOT / "deploy" / "vps-separation"
CTL = SEP / "bk-ctl"

STUB = """#!/bin/bash
echo "$(basename "$0") $*" >> "$CALLS"
"""


@pytest.fixture
def env(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("bk-ctl ignores its test mode as root")
    b = tmp_path / "bin"
    b.mkdir()
    for name in ("systemctl", "journalctl", "flock"):
        p = b / name
        p.write_text(STUB)
        p.chmod(0o755)
    units = tmp_path / "units"
    units.write_text("# bkesari units\nbkesari-atip.service\nbkesari-atip-pgdump.service\n\nbkesari-platform.service\n")
    (tmp_path / "www").mkdir()
    e = dict(os.environ, BK_CTL_TEST="1", BK_CTL_SYSTEMCTL=str(b / "systemctl"),
             BK_CTL_JOURNALCTL=str(b / "journalctl"), BK_CTL_FLOCK=str(b / "flock"),
             BK_CTL_UNITS=str(units), BK_CTL_LOG=str(tmp_path / "bk-ctl.log"), BK_CTL_LOCK=str(tmp_path / "lock"),
             BK_CTL_STATE_ROOT=str(tmp_path / "www"), BK_CTL_APP_USER=getpass.getuser(),
             CALLS=str(tmp_path / "calls.log"))
    e.pop("SUDO_USER", None)
    return tmp_path, e


def run(env, *args):
    tmp, e = env
    return subprocess.run(["bash", str(CTL), *args], env=e, capture_output=True, text=True)


def calls(env):
    p = env[0] / "calls.log"
    return p.read_text() if p.exists() else ""


def log(env):
    p = env[0] / "bk-ctl.log"
    return p.read_text() if p.exists() else ""


@pytest.mark.parametrize("verb", ["start", "stop", "restart"])
def test_a_bkesari_unit_can_be_started_stopped_and_restarted(env, verb):
    r = run(env, "unit", verb, "bkesari-atip.service")
    assert r.returncode == 0, r.stderr
    assert f"systemctl {verb} -- bkesari-atip.service" in calls(env)
    assert "flock -w 120 9" in calls(env)                        # one bkesari restart at a time
    assert f"{getpass.getuser()} unit {verb} bkesari-atip.service" in log(env) and " IST " in log(env)


@pytest.mark.parametrize("unit", [
    "site@gokesari-blue.service", "gk-deploy.service", "gk-deploy.timer",          # GoKesari
    "agtci-api@prod.service",                                                      # agtci
    "caddy.service", "postgresql@18-main.service", "postgresql@18-bkesari.service",  # shared
    "ssh.service", "wg-quick@wg0.service", "php8.3-fpm.service",
])
def test_no_other_apps_unit_and_no_shared_unit(env, unit):
    r = run(env, "unit", "restart", unit)
    assert r.returncode == 2 and "not a bkesari unit" in r.stderr
    assert "systemctl" not in calls(env) and "REFUSED" in log(env)


@pytest.mark.parametrize("unit", ["bkesari-atip.service;reboot", "bkesari-atip.service gk-deploy.service",
                                  "../bkesari-atip.service", "bkesari-atip", "", "-h"])
def test_odd_unit_names_are_refused(env, unit):
    r = run(env, "unit", "restart", unit)
    assert r.returncode == 2 and "systemctl" not in calls(env)


@pytest.mark.parametrize("verb", ["enable", "disable", "mask", "kill", "daemon-reload", "edit", "set-property"])
def test_only_start_stop_and_restart(env, verb):
    r = run(env, "unit", verb, "bkesari-atip.service")
    assert r.returncode == 2 and "start, stop or restart" in r.stderr and "systemctl" not in calls(env)


def test_the_journal_of_a_bkesari_unit_without_a_pager(env):
    r = run(env, "journal", "bkesari-atip.service", "1763870000")
    assert r.returncode == 0, r.stderr
    assert "journalctl -u bkesari-atip.service --since @1763870000 --no-pager -q" in calls(env)


@pytest.mark.parametrize("args", [("gk-deploy.service", "1763870000"), ("bkesari-atip.service", "yesterday"),
                                  ("bkesari-atip.service", "1;id"), ("bkesari-atip.service", "17"),
                                  ("bkesari-atip.service",)])
def test_the_journal_refuses_other_units_and_odd_times(env, args):
    r = run(env, "journal", *args)
    assert r.returncode == 2 and "journalctl" not in calls(env)


def test_init_state_creates_a_deploys_folder_for_the_app_user(env):
    d = env[0] / "www" / "atip-deploys"
    r = run(env, "init-state", str(d))
    assert r.returncode == 0, r.stderr
    assert (d / "backups").is_dir() and (d / "staging").is_dir()
    assert oct(d.stat().st_mode & 0o777) == "0o750"


@pytest.mark.parametrize("rel", ["atip", "Atip-deploys", "-deploys", "a/b-deploys", "../etc-deploys"])
def test_init_state_refuses_any_other_folder(env, rel):
    r = run(env, "init-state", str(env[0] / "www" / rel))
    assert r.returncode == 2
    r = run(env, "init-state", "/etc/atip-deploys")
    assert r.returncode == 2


def test_without_the_list_only_the_three_bkesari_units(env):
    tmp, e = env
    os.remove(e["BK_CTL_UNITS"])
    r = run(env, "units")
    assert r.stdout.split() == ["bkesari-atip.service", "bkesari-atip-pgdump.service", "bkesari-platform.service"]


def test_the_installed_list_holds_only_bkesari_units():
    lines = [x.strip() for x in (SEP / "units.bkesari").read_text().splitlines()
             if x.strip() and not x.strip().startswith("#")]
    assert lines and all(x.startswith("bkesari-") and x.endswith((".service", ".timer")) for x in lines)


def test_the_sudo_rule_allows_only_bk_ctl_as_root():
    rules = [x for x in (SEP / "sudoers-bkdeploy").read_text().splitlines() if x and not x.startswith("#")]
    assert "bkdeploy ALL=(root) NOPASSWD: /usr/local/sbin/bk-ctl" in rules
    assert "bkdeploy ALL=(bkesari) NOPASSWD: ALL" in rules
    assert not any("(ALL" in x or "(root) NOPASSWD: ALL" in x for x in rules)


def test_the_scripts_parse():
    for f in ("bk-ctl", "setup-bkdeploy.sh"):
        r = subprocess.run(["bash", "-n", str(SEP / f)], capture_output=True, text=True)
        assert r.returncode == 0, (f, r.stderr)


SETUP_STUBS = {
    # root for the script's own check, the app user bkesari exists, bkdeploy does not yet
    "id": '#!/bin/bash\ncase "$1" in -u) echo 0 ;; bkesari) exit 0 ;; *) exit 1 ;; esac\n',
    "sshd": '#!/bin/bash\n[ "$1" = -T ] && printf "%b" "$SSHD_T"\n',
    # the first command that would change anything: stop here, so the test never touches the machine
    "useradd": '#!/bin/bash\necho "REACHED useradd $*"\nexit 1\n',
}


@pytest.fixture
def setup_env(tmp_path):
    b = tmp_path / "bin"
    b.mkdir()
    for name, body in SETUP_STUBS.items():
        (b / name).write_text(body)
        (b / name).chmod(0o755)
    pub = tmp_path / "bkesari_deploy.pub"
    pub.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGEYT9aNCTRyutBset0fQ5BYJ9WEvC0WI3DNkXE0+gPi test\n")
    e = dict(os.environ, PATH=f"{b}:/usr/bin:/bin")
    return pub, e


def run_setup(setup_env, sshd_t, *args):
    pub, e = setup_env
    e = dict(e, SSHD_T=sshd_t)
    return subprocess.run(["bash", str(SEP / "setup-bkdeploy.sh"), *(args or (str(pub),))],
                          env=e, capture_output=True, text=True)


@pytest.mark.parametrize("sshd_t", [
    "",                                                                             # no login list at all
    "allowusers bkdeploy\\n",
    # 23 Nov server: GoKesari's lockdown list plus the owner-approved bkdeploy entry
    "allowusers gkadmin@10.66.0.10\\nallowusers gksnap@10.66.0.21\\nallowusers gksnap@10.66.0.23\\n"
    "allowusers gksnap@10.66.0.24\\nallowusers bkdeploy@10.66.0.10\\n",
])
def test_setup_goes_on_when_sshd_lets_bkdeploy_in(setup_env, sshd_t):
    r = run_setup(setup_env, sshd_t)
    assert "STOP" not in r.stdout, r.stdout
    assert "1/5 user bkdeploy" in r.stdout and "REACHED useradd" in r.stdout


@pytest.mark.parametrize("sshd_t", [
    "allowusers gkadmin@10.66.0.10\\nallowusers gksnap@10.66.0.21\\n",                # bkdeploy not on the list
    "allowusers gkadmin@10.66.0.10\\nallowusers bkdeployx@10.66.0.10\\n",             # a different user
    "allowusers *@10.66.0.10\\n",                                                    # wildcards stay the owner's call
    "allowusers bkdeploy@10.66.0.10\\nallowgroups ssh-users\\n",                      # AllowGroups too
])
def test_setup_stops_before_any_change_when_sshd_would_refuse_bkdeploy(setup_env, sshd_t):
    r = run_setup(setup_env, sshd_t)
    assert "STOP: sshd" in r.stdout and "REACHED useradd" not in r.stdout and "1/5" not in r.stdout


def test_setup_check_is_quiet_when_nothing_is_installed(setup_env):
    if Path("/etc/bk-ctl/units").exists():
        pytest.skip("this machine has /etc/bk-ctl/units")
    r = run_setup(setup_env, "", "--check")
    assert r.returncode == 0 and "No such file" not in r.stderr, r.stderr
    assert "units:     none" in r.stdout


def test_the_sudo_rule_passes_visudo():
    v = shutil.which("visudo") or "/usr/sbin/visudo"
    if not Path(v).exists():
        pytest.skip("no visudo")
    r = subprocess.run([v, "-cf", str(SEP / "sudoers-bkdeploy")], capture_output=True, text=True)
    if r.returncode and "permission" in (r.stderr + r.stdout).lower():
        pytest.skip("visudo needs root here")
    assert r.returncode == 0, r.stdout + r.stderr
