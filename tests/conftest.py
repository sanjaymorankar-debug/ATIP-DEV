"""
Shared test fixtures.

The single most important thing here is that no test ever touches the real
atip_data/atip.db. `db.schema.DB_PATH` is a module-level Path, so redirecting it
to a tmp_path for the duration of a test is enough to isolate every module that
calls get_connection() — which is all of them.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db.schema as schema  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Every test, even one that does not ask for temp_db: its own database,
    and no Telegram call. Alerts are now recorded in alert_log on every send,
    so a test that halts trading or fails a job would otherwise write to the
    real atip.db -- or message a configured bot."""
    monkeypatch.setattr(schema, "DB_PATH", tmp_path / "isolated.db")
    # The 21:00-06:00 IST stop window (pipeline/operating_schedule.py) closes the kill switch and the
    # job runner; a suite run after 21:00 must not see orders refused for the time of day. The
    # operating-schedule tests switch it back on with a fixed clock.
    monkeypatch.setenv("ATIP_OPERATING_SCHEDULE", "off")
    # The dashboard's state file is relative to the working directory: a test that rebuilds the
    # dashboard, run from the project folder on the Mac, replaced the live atip_data/dashboard_state.json
    # with its fixture rows (2026-10-26).
    monkeypatch.setattr("dashboard.server.STATE_PATH", tmp_path / "dashboard_state.json")
    import alerts.telegram as tg
    monkeypatch.setattr(tg, "_real_deliver_telegram", tg._deliver_telegram, raising=False)
    monkeypatch.setattr(tg, "_deliver_telegram", lambda message, parse_mode="HTML": (False, "test"))


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """A throwaway SQLite database for one test."""
    monkeypatch.setattr(schema, "DB_PATH", tmp_path / "test.db")
    yield tmp_path / "test.db"


@pytest.fixture
def cfg():
    """Strategy defaults with the feature switched on, isolated from config.json."""
    from strategy.aggressive import DEFAULTS, validate_config
    c = dict(DEFAULTS)
    c["aggressive_enabled"] = True
    return validate_config(c)


@pytest.fixture
def position(temp_db, cfg):
    """A filled 100-share long in ACME at 100.00, before target 1."""
    from strategy.positions import open_position
    return open_position("ACME", entry_price=100.0, quantity=100,
                         atr_pct=1.5, cfg=cfg)
