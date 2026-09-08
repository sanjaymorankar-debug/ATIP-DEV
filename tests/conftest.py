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
