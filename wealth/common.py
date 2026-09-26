"""
Shared helpers for the wealth package: ownership, ids, JSON columns.

OWNERSHIP. Every wealth row belongs to (tenant_id, owner_id).
  * enterprise off (the default, single user): ('default', 'owner').
  * enterprise on: the signed-in principal's tenant and user_id
    (request.state.principal, set by enterprise/authz.py).
A caller can only ever read or write its own rows: every query in the package
filters on both columns, and no route takes an owner from the request body.

Only the default tenant's owner sees the broker account and the paper book
(W7 rule: they belong to the default tenant); other tenants' wealth views are
built from their own manual holdings.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime

DEFAULT_OWNER = {"tenant_id": "default", "owner_id": "owner"}


def owner_from_principal(principal: dict | None) -> dict:
    p = principal or {}
    if p.get("tenant_id") and p.get("user_id"):
        return {"tenant_id": str(p["tenant_id"]), "owner_id": str(p["user_id"])}
    return dict(DEFAULT_OWNER)


def owns_house_book(owner: dict) -> bool:
    """True for the owner who may see the broker holdings and the paper book."""
    return owner.get("tenant_id") == "default"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


def dumps(obj) -> str:
    return json.dumps(obj, default=_default, sort_keys=True)


def loads(s, fallback=None):
    if s is None:
        return fallback
    try:
        return json.loads(s)
    except Exception:
        return fallback


def digest(obj) -> str:
    return hashlib.sha256(dumps(obj).encode()).hexdigest()


def _default(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


def table_exists(conn, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def num(v, name: str, lo: float | None = None, hi: float | None = None, required: bool = True):
    """Validate a numeric input; raises ValueError with the field name."""
    if v is None or v == "":
        if required:
            raise ValueError(f"{name} is required")
        return None
    if isinstance(v, bool):
        raise ValueError(f"{name} must be a number")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if f != f or f in (float("inf"), float("-inf")):
        raise ValueError(f"{name} must be finite")
    if lo is not None and f < lo:
        raise ValueError(f"{name} must be >= {lo}")
    if hi is not None and f > hi:
        raise ValueError(f"{name} must be <= {hi}")
    return f


def text(v, name: str, max_len: int = 200, required: bool = True):
    if v is None or str(v).strip() == "":
        if required:
            raise ValueError(f"{name} is required")
        return None
    s = str(v).strip()
    if len(s) > max_len:
        raise ValueError(f"{name} is longer than {max_len} characters")
    return s


def parse_date(v, name: str, required: bool = True):
    if v is None or v == "":
        if required:
            raise ValueError(f"{name} is required")
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        raise ValueError(f"{name} must be a date YYYY-MM-DD") from None


DISCLAIMER = ("ATIP is for personal informational use only and is not SEBI-registered investment advice. "
              "Every figure is a model output from the data and assumptions shown; verify independently and "
              "consult a SEBI-registered investment adviser before acting.")
