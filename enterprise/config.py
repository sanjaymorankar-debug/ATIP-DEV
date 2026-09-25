"""
Enterprise configuration: atip_data/config.json section "enterprise" (optional).

    "enterprise": {
        "enabled": false,                 master switch (off: ATIP is single-user as before)
        "session_hours": 12,              session lifetime
        "allow_self_registration": false, open sign-up (accounts start PENDING)
        "accept_legacy_token": false,     treat a bare X-ATIP-Token request (no session /
                                          API key) as the platform SUPER_ADMIN -- for
                                          scripts; off because every dashboard page embeds
                                          that token
        "password_min_length": 12,
        "max_failed_logins": 5,
        "lockout_minutes": 15,
        "default_tenant": "default",      the owner's tenant; the only one that may read
                                          or trade the (single) paper book
        "refresh_days": 14                W8: refresh-token lifetime (rotated on every use)
    }
"""

from __future__ import annotations

import json
from pathlib import Path

CONFIG_PATH = Path("atip_data") / "config.json"
DEFAULTS = {"enabled": False, "session_hours": 12, "allow_self_registration": False, "accept_legacy_token": False,
            "password_min_length": 12, "max_failed_logins": 5, "lockout_minutes": 15, "default_tenant": "default",
            "refresh_days": 14}
COOKIE = "atip_session"
REFRESH_COOKIE = "atip_refresh"


def settings() -> dict:
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("enterprise") or {}
    except Exception:
        raw = {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in raw.items() if k in DEFAULTS})
    for k in ("enabled", "allow_self_registration", "accept_legacy_token"):
        out[k] = out.get(k) is True               # only a real JSON true switches these on
    return out


def enabled() -> bool:
    return settings()["enabled"]
