#!/usr/bin/env python3
"""Test gate: every code change goes through the project's testing agent.

Claude Code runs this from hooks (see .claude/settings.json):

  SessionStart  test_gate.py session-start   note what the code looks like now
  Stop          test_gate.py stop            refuse to finish while a change is untested
  SubagentStop  test_gate.py subagent-stop   record the testing agent's verdict

Which paths belong to which project, and which agent tests each one, is in
.claude/test-gate.json next to this file. State is kept in .claude/state/,
which is git-ignored.

A change is "untested" when a project's files differ both from how they were
when the session started and from the last state a testing agent signed off.
So a session that only reads code is never stopped, while one that edits a
file -- with Edit, Write, a shell command, a commit -- is held until the
testing agent has run on the result.

The agent signs off by ending its last message with a line
"VERDICT: PASS", "VERDICT: FAIL" or "VERDICT: BLOCKED". Any verdict counts as
"tested": a FAIL means the agent already ran its own fix-and-retest loop and
what is left needs a person, so asking it again would only loop. The verdict is
surfaced to the user instead.

Sessions started in a folder that holds several repositories (as cloud
sessions do) have no project settings of their own. For those, the same
commands with --workspace DIR gate every repository directly under DIR that
has a .claude/test-gate.json; see the README section this file is linked from.

By hand:

  python3 .claude/hooks/test_gate.py status   what is tested and what is not
  python3 .claude/hooks/test_gate.py accept   sign off the current code without a run
  python3 .claude/hooks/test_gate.py install-user-hooks
                                               gate sessions started above the repositories

Only the standard library is used, so it runs on the python3 macOS ships.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time

GATE_VERSION = 1
MAX_BLOCKS = 2            # times Stop asks for the same untested code before giving up
SESSION_TTL = 14 * 86400  # forget sessions older than this
WORKSPACE = "--workspace" in sys.argv
VERDICT_RE = re.compile(r"VERDICT:\s*\**\s*(PASS|FAIL|BLOCKED)\b", re.IGNORECASE)
TESTER_RE = re.compile(r"TESTER:\s*\**\s*([A-Za-z0-9_-]+)")


# ---------------------------------------------------------------- git helpers

def git(root, *args, data=None):
    out = subprocess.run(
        ["git", "-C", root, *args],
        input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    )
    return out.stdout


def is_repo(root):
    return git(root, "rev-parse", "--is-inside-work-tree").strip() == b"true"


def head(root):
    return git(root, "rev-parse", "--verify", "-q", "HEAD").decode().strip()


def pathspec(project, cfg):
    spec = list(project.get("paths") or ["."])
    for pattern in cfg.get("ignore", []):
        spec.append(":(exclude,glob)" + pattern)
    return spec


def fingerprint(root, spec):
    """A hash of the project's files as they are on disk right now.

    Covers the index, unstaged edits and untracked (non-ignored) files, so it
    changes for an edit, a new file, a deletion or a commit alike, and is the
    same again if a change is undone.
    """
    h = hashlib.sha256()
    h.update(git(root, "ls-files", "-s", "--", *spec))
    h.update(git(root, "diff", "--no-ext-diff", "--binary", "--", *spec))
    untracked = sorted(n for n in git(root, "ls-files", "-o", "--exclude-standard", "-z", "--", *spec).split(b"\0") if n)
    if untracked:
        h.update(b"\0".join(untracked))
        h.update(git(root, "hash-object", "--stdin-paths", data=b"\n".join(untracked) + b"\n"))
    return h.hexdigest()


def changed_files(root, spec, since_head, limit=12):
    names = set()
    if since_head:
        names.update(git(root, "diff", "--name-only", since_head, "--", *spec).decode().splitlines())
    names.update(git(root, "diff", "--name-only", "HEAD", "--", *spec).decode().splitlines())
    names.update(git(root, "ls-files", "-o", "--exclude-standard", "--", *spec).decode().splitlines())
    names = sorted(n for n in names if n)
    shown = names[:limit]
    if len(names) > limit:
        shown.append("... and %d more" % (len(names) - limit))
    return shown


# ---------------------------------------------------------------- state

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(value, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


class Repo:
    def __init__(self, root):
        self.root = root
        self.cfg = load_json(os.path.join(root, ".claude", "test-gate.json"), {})
        self.state_path = os.path.join(root, ".claude", "state", "test-gate.json")
        self.state = load_json(self.state_path, {})
        self.state.setdefault("projects", {})
        self.state.setdefault("sessions", {})
        self.state["version"] = GATE_VERSION
        self._fp = {}

    @property
    def name(self):
        return os.path.basename(os.path.abspath(self.root))

    def projects(self):
        return self.cfg.get("projects", [])

    def fp(self, project):
        key = project["name"]
        if key not in self._fp:
            self._fp[key] = fingerprint(self.root, pathspec(project, self.cfg))
        return self._fp[key]

    def start_session(self, sid):
        """SessionStart: the code as it is now is this session's starting point."""
        self.state["sessions"][sid] = {"started": int(time.time()), "head": head(self.root),
                                       "fps": {p["name"]: self.fp(p) for p in self.projects()}, "blocks": {}}

    def session(self, sid, create=True):
        sessions = self.state["sessions"]
        if sid not in sessions and create:
            # Hooks were not running when this session started (settings added
            # mid-session, or SessionStart skipped). Take the last sign-off as
            # the starting point where there is one, so changes made before
            # this point are still caught; otherwise start from now.
            fps = {}
            for p in self.projects():
                tested = self.state["projects"].get(p["name"], {}).get("tested_fp")
                fps[p["name"]] = tested or self.fp(p)
            sessions[sid] = {"started": int(time.time()), "head": head(self.root), "fps": fps, "blocks": {}}
        return sessions.get(sid)

    def prune(self):
        cutoff = time.time() - SESSION_TTL
        for sid in [s for s, v in self.state["sessions"].items() if v.get("started", 0) < cutoff]:
            del self.state["sessions"][sid]

    def save(self):
        self.prune()
        save_json(self.state_path, self.state)

    def pending(self, sid):
        """Projects whose code changed this session and has not been tested since."""
        sess = self.session(sid)
        out = []
        for p in self.projects():
            fp = self.fp(p)
            start = sess["fps"].get(p["name"])
            tested = self.state["projects"].get(p["name"], {}).get("tested_fp")
            if start is None:
                sess["fps"][p["name"]] = start = tested or fp
            if fp != start and fp != tested:
                out.append(p)
        return out


# ---------------------------------------------------------------- events

def read_input():
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        return json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {}


def repos_for(args, data):
    """The repositories this invocation gates.

    Project mode (no --workspace): the project directory itself.
    Workspace mode: every repository directly under DIR with a test-gate.json,
    except one whose own .claude/settings.json already runs this gate -- its
    project hooks cover it and gating it twice would ask twice.
    """
    if WORKSPACE:
        base = args[args.index("--workspace") + 1]
        roots = []
        candidates = [base] + [os.path.join(base, d) for d in sorted(os.listdir(base))]
        for root in candidates:
            if not os.path.isfile(os.path.join(root, ".claude", "test-gate.json")) or not is_repo(root):
                continue
            own = os.path.join(root, ".claude", "settings.json")
            if os.path.abspath(root) == os.path.abspath(base) and _runs_gate(own):
                continue
            roots.append(root)
        return [Repo(r) for r in roots]
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return [Repo(root)] if is_repo(root) else []


def _runs_gate(settings_path):
    try:
        with open(settings_path, encoding="utf-8") as f:
            return "test_gate.py" in f.read()
    except OSError:
        return False


def emit(obj):
    if obj:
        sys.stdout.write(json.dumps(obj))
    sys.exit(0)


def on_session_start(repos, data):
    sid = data.get("session_id", "manual")
    lines = []
    for repo in repos:
        if sid not in repo.state["sessions"]:  # a resumed session keeps its start
            repo.start_session(sid)
        repo.save()
        for p in repo.projects():
            lines.append("- %s/%s: agent `%s`, spec `%s`" % (repo.name, p["name"], p["agent"], p.get("spec", "TEST_SPEC.md")))
    if lines:
        emit({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext":
            "Test gate is on. After changing code, run the project's testing agent before finishing "
            "(Agent tool with that subagent_type; if it is not offered, a general-purpose agent told to "
            "read and follow .claude/agents/<agent>.md in that repository):\n" + "\n".join(lines)}})
    emit(None)


def on_stop(repos, data):
    sid = data.get("session_id", "manual")
    asks, notes = [], []
    for repo in repos:
        sess = repo.session(sid)
        for p in repo.pending(sid):
            key = "%s:%s" % (p["name"], repo.fp(p))
            n = sess["blocks"].get(key, 0)
            if n >= MAX_BLOCKS:
                notes.append("%s/%s has untested changes: its testing agent `%s` was asked for %d times and not run."
                             % (repo.name, p["name"], p["agent"], n))
                continue
            sess["blocks"][key] = n + 1
            files = changed_files(repo.root, pathspec(p, repo.cfg), sess.get("head"))
            asks.append((repo, p, files))
        for p in repo.projects():
            last = repo.state["projects"].get(p["name"], {})
            if last.get("session") == sid and last.get("tested_fp") == repo.fp(p) and last.get("verdict") != "PASS":
                notes.append("%s/%s: the last test run ended %s -- see %s"
                             % (repo.name, p["name"], last.get("verdict", "UNKNOWN"), last.get("report") or ".claude/test-runs/"))
        repo.save()

    if asks:
        parts = []
        for repo, p, files in asks:
            where = repo.root if WORKSPACE else "this repository"
            parts.append(
                "- %s (%s): launch the `%s` subagent (Agent tool, subagent_type \"%s\"; if that type is not "
                "offered, a general-purpose agent told to read and follow %s/.claude/agents/%s.md). It runs the "
                "standard and full regression suites from %s, fixes the defects it finds, re-runs everything "
                "and reports. Changed: %s"
                % (p["name"], where, p["agent"], p["agent"], repo.root, p["agent"],
                   p.get("spec", "TEST_SPEC.md"), ", ".join(files) or "(see git status)"))
        emit({"decision": "block", "reason":
              "Code changed and has not been through its testing agent yet. Before finishing:\n" + "\n".join(parts)
              + "\nRun independent projects' agents in parallel. Then tell the user each agent's verdict."})
    if notes:
        emit({"systemMessage": "Test gate: " + " | ".join(notes)})
    emit(None)


def on_subagent_stop(repos, data):
    """Record a testing agent's verdict against the code it left behind.

    The agent is recognised by its type, or -- where the project's agents are
    not registered, as in a session started above the repositories and the
    tester ran as a general-purpose agent -- by a "TESTER: <name>" line in
    its final message.
    """
    agent = (data.get("agent_type") or "").split(":")[-1]
    message = data.get("last_assistant_message") or ""
    verdicts = VERDICT_RE.findall(message)
    named = set(TESTER_RE.findall(message))
    for repo in repos:
        for p in repo.projects():
            if p["agent"] != agent and p["agent"] not in named:
                continue
            if not verdicts and p["agent"] == agent:
                tries = repo.state.setdefault("verdict_asks", {})
                aid = data.get("agent_id", "?")
                if tries.get(aid, 0) < 1:
                    tries[aid] = tries.get(aid, 0) + 1
                    repo.save()
                    emit({"decision": "block", "reason":
                          "Finish with the report the spec asks for, written to .claude/test-runs/, and end your "
                          "final message with two lines: TESTER: %s and VERDICT: PASS, VERDICT: FAIL or "
                          "VERDICT: BLOCKED." % p["agent"]})
            repo.state["projects"][p["name"]] = {
                "tested_fp": repo.fp(p),
                "verdict": verdicts[-1].upper() if verdicts else "UNKNOWN",
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "session": data.get("session_id", "manual"),
                "agent": p["agent"],
                "report": ".claude/test-runs/latest-%s.md" % p["agent"],
            }
            repo.state.get("verdict_asks", {}).pop(data.get("agent_id", "?"), None)
            repo.save()
    emit(None)


def on_status(repos, _data):
    for repo in repos:
        for p in repo.projects():
            last = repo.state["projects"].get(p["name"], {})
            ok = last.get("tested_fp") == repo.fp(p)
            print("%-18s %-14s %-9s %s" % (
                repo.name, p["name"],
                (last.get("verdict", "-") if ok else "UNTESTED"),
                ("last run %s by %s" % (last.get("at"), last.get("agent"))) if last else "never run"))
    sys.exit(0)


def on_accept(repos, _data):
    for repo in repos:
        for p in repo.projects():
            repo.state["projects"][p["name"]] = {
                "tested_fp": repo.fp(p), "verdict": "ACCEPTED", "agent": "manual",
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "session": "manual"}
        repo.save()
    on_status(repos, _data)


# Resolved when the hook fires, not when it is installed, so it works whether
# or not the repositories have been cloned yet (a cloud setup script may run
# first) and picks up whichever repository's copy of this file it finds.
USER_HOOK = ('f=$(ls "$CLAUDE_PROJECT_DIR"/.claude/hooks/test_gate.py "$CLAUDE_PROJECT_DIR"/*/.claude/hooks/test_gate.py '
             '2>/dev/null | head -n 1); [ -z "$f" ] || python3 "$f" %s --workspace "$CLAUDE_PROJECT_DIR"')


def on_install_user_hooks(_repos, _data):
    """Add the workspace-mode hooks to ~/.claude/settings.json, keeping what is there."""
    path = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    settings = load_json(path, None)
    if settings is None:
        if os.path.exists(path):
            sys.stderr.write("test_gate: %s is not valid JSON; left untouched\n" % path)
            sys.exit(1)
        settings = {}
    hooks = settings.setdefault("hooks", {})
    added = []
    for event, arg, timeout in (("SessionStart", "session-start", 30), ("Stop", "stop", 60), ("SubagentStop", "subagent-stop", 60)):
        groups = hooks.setdefault(event, [])
        if any("test_gate.py" in h.get("command", "") for g in groups for h in g.get("hooks", [])):
            continue
        groups.append({"hooks": [{"type": "command", "command": USER_HOOK % arg, "timeout": timeout}]})
        added.append(event)
    save_json(path, settings)
    print("%s: %s" % (path, ("added " + ", ".join(added)) if added else "already installed"))
    sys.exit(0)


EVENTS = {
    "session-start": on_session_start,
    "stop": on_stop,
    "subagent-stop": on_subagent_stop,
    "status": on_status,
    "accept": on_accept,
    "install-user-hooks": on_install_user_hooks,
}


def main(argv):
    if len(argv) < 2 or argv[1] not in EVENTS:
        sys.stderr.write("usage: test_gate.py {%s} [--workspace DIR]\n" % "|".join(EVENTS))
        return 2
    data = read_input() if argv[1] not in ("status", "accept", "install-user-hooks") else {}
    repos = repos_for(argv, data)
    EVENTS[argv[1]](repos, data)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except SystemExit:
        raise
    except Exception as exc:  # a broken gate must never wedge a session
        sys.stderr.write("test_gate: %s\n" % exc)
        sys.exit(0)
