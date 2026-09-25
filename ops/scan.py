"""
Security / configuration scan (a development and release check; no new tooling).

    python -m ops scan

1. secrets in git-tracked files: known token shapes (JWT, Telegram bot token,
   Anthropic key, ATIP session / refresh / API tokens, private keys, AWS keys) and
   `"<secret-like key>": "<non-empty literal>"` assignments. Findings show the file,
   line and pattern -- never the matched value.
2. files that must never be tracked: atip_data/, *.db, .env, secrets/, *.pem, *.key
3. configuration: ops.config.validate() + ops.secrets.validate()
4. dependency audit: pip-audit is used when installed; otherwise reported as NOT RUN
   (it is not installed by this tool).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

PATTERNS = {
    "jwt": re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "telegram_bot_token": re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}\b"),
    "anthropic_key": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "atip_token": re.compile(r"\b(ats|atr|atf)_[A-Za-z0-9_-]{30,}|\batk_[0-9a-f]{8}_[A-Za-z0-9_-]{20,}"),
    "private_key": re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "aws_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "secret_assignment": re.compile(
        r"""["']?(access_token|api_secret|api_key|client_secret|password|bot_token|secret_key)["']?\s*[:=]\s*["']"""
        r"""([^"'\s]{12,})["']""", re.I),
}
PLACEHOLDER = re.compile(r"(your|xxx|example|changeme|placeholder|<|\$\{|dummy|test|fake|\*\*\*|\.\.\.|…)", re.I)
FORBIDDEN = (re.compile(r"^atip_data/"), re.compile(r"\.db$"), re.compile(r"(^|/)\.env$"),
             re.compile(r"(^|/)secrets/"), re.compile(r"\.(pem|key|pfx)$"))
SKIP_EXT = {".png", ".jpg", ".ico", ".xlsx", ".pdf", ".gif", ".woff", ".woff2", ".ttf", ".zip", ".pkl", ".npz"}


def tracked_files(root=".") -> list:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True).stdout
        return [x for x in out.splitlines() if x]
    except Exception:
        return []


def scan_secrets(root=".") -> list:
    findings = []
    for f in tracked_files(root):
        for rx in FORBIDDEN:
            if rx.search(f):
                findings.append({"file": f, "line": None, "pattern": "forbidden_tracked_file"})
        p = Path(root) / f
        if p.suffix.lower() in SKIP_EXT or not p.is_file() or p.stat().st_size > 2_000_000:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for name, rx in PATTERNS.items():
                m = rx.search(line)
                if not m:
                    continue
                if name == "secret_assignment" and PLACEHOLDER.search(m.group(2)):
                    continue
                if f.startswith("ops/scan.py"):
                    continue
                findings.append({"file": f, "line": i, "pattern": name})
    return findings


def dependency_audit() -> dict:
    if not shutil.which("pip-audit"):
        return {"status": "NOT RUN", "reason": "pip-audit is not installed (not installed by this scan)"}
    try:
        r = subprocess.run(["pip-audit", "-f", "json"], capture_output=True, text=True, timeout=300)
        return {"status": "RUN", "exit_code": r.returncode, "output": r.stdout[-4000:]}
    except Exception as e:
        return {"status": "ERROR", "reason": str(e)}


def run(root=".") -> dict:
    from ops.config import validate as cfg_validate
    from ops.secrets import validate as sec_validate
    secrets = scan_secrets(root)
    config = cfg_validate() + sec_validate()
    return {"secret_findings": secrets, "config_findings": config, "dependency_audit": dependency_audit(),
            "ok": not secrets and not any(x["level"] == "error" for x in config)}
