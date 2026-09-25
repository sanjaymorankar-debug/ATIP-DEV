"""
Structured logging + secret masking.

install(log_dir):
  * adds SecretMaskingFilter to EVERY root handler (the existing console handler and
    atip.log keep their format; configured secret values, JWTs, Telegram / Anthropic /
    ATIP tokens are replaced by ****)
  * adds a rotating JSON-lines handler, atip_data/atip.jsonl (ops.json_logs), one
    object per record:
      ts level service module logger message request_id correlation_id tenant_id
      user_id job event_type error_code exc
    tenant_id / user_id come from the request context (ops/context.py), which the HTTP
    middleware sets only from the authenticated principal.
Existing log text (atip.log) is unchanged apart from masking.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import datetime
from pathlib import Path

from ops import context
from ops.secrets import known_values, mask

SERVICE = "atip"


class SecretMaskingFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self.refresh()

    def refresh(self):
        try:
            self.values = known_values()
        except Exception:
            self.values = []

    def filter(self, record):
        try:
            msg = record.getMessage()
            m = mask(msg, self.values)
            if m != msg:
                record.msg, record.args = m, ()
        except Exception:
            pass
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record):
        ctx = context.snapshot()
        d = {"ts": datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
             "level": record.levelname, "service": SERVICE, "module": record.module, "logger": record.name,
             "message": record.getMessage(), **{k: v for k, v in ctx.items() if v},
             "event_type": getattr(record, "event_type", None), "error_code": getattr(record, "error_code", None)}
        if record.exc_info:
            d["exc"] = mask(self.formatException(record.exc_info))
        return json.dumps({k: v for k, v in d.items() if v is not None}, default=str)


_INSTALLED = {"done": False}


class _SafeRotating(logging.handlers.RotatingFileHandler):
    """Windows refuses to rename a log another process holds open; keep appending
    and retry the rollover later instead of raising on every record (as main.py)."""
    _retry_after = 0.0

    def shouldRollover(self, record):
        import time as _t
        return False if _t.time() < self._retry_after else super().shouldRollover(record)

    def doRollover(self):
        import time as _t
        try:
            super().doRollover()
        except OSError:
            self._retry_after = _t.time() + 300
            if self.stream is None:
                self.stream = self._open()


def install(log_dir: Path | str = "atip_data", json_logs: bool = True) -> dict:
    if _INSTALLED["done"]:
        return {"installed": False}
    root = logging.getLogger()
    f = SecretMaskingFilter()
    for h in root.handlers:
        h.addFilter(f)
    out = {"masking": True, "json": False}
    if json_logs:
        p = Path(log_dir) / "atip.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            jh = _SafeRotating(str(p), maxBytes=20 * 2**20, backupCount=5, encoding="utf-8", delay=True)
            jh.setFormatter(JsonFormatter())
            jh.addFilter(f)
            root.addHandler(jh)
            out["json"] = str(p)
        except Exception as e:
            logging.getLogger("atip.ops").warning(f"  JSON log handler not installed: {e}")
    _INSTALLED["done"] = True
    return out
