"""
tools/dhan_token_refresh.py and the macOS agent that runs it.

The move to macOS ported the platform and snapshot launchers but not the Windows task
"ATIP_DhanTokenRefresh", so nothing renewed the 24-hour token; and RenewToken sent the
client id under a header Dhan does not read. No test here reaches Dhan: http() is stubbed.
"""
import importlib.util
import json
import os
import plistlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("dtr", REPO / "tools" / "dhan_token_refresh.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sandbox(tmp_path, monkeypatch, cfg):
    dtr = _load()
    (tmp_path / "atip_data").mkdir()
    path = tmp_path / "atip_data" / "config.json"
    path.write_text(json.dumps(cfg))
    monkeypatch.setattr(dtr, "ROOT", tmp_path)
    monkeypatch.setattr(dtr, "CFG", path)
    monkeypatch.setattr(dtr, "LOG", tmp_path / "atip_data" / "dhan_token_refresh.log")
    monkeypatch.setattr(sys, "argv", ["dhan_token_refresh.py"])
    monkeypatch.chdir(tmp_path)
    return dtr, path


def test_renew_sends_the_documented_client_id_header(monkeypatch, tmp_path):
    dtr, _ = _sandbox(tmp_path, monkeypatch, {})
    seen = {}

    def fake_http(method, url, headers=None):
        seen.update(method=method, url=url, headers=headers)
        return 200, {"accessToken": "renewed", "expiryTime": "2026-10-08T06:30:00"}

    monkeypatch.setattr(dtr, "http", fake_http)
    assert dtr.renew("1000000001", "old") == "renewed"
    assert seen["method"] == "GET" and seen["url"] == "https://api.dhan.co/v2/RenewToken"
    assert seen["headers"] == {"access-token": "old", "dhanClientId": "1000000001"}


def test_renew_failure_is_logged_not_silent(monkeypatch, tmp_path):
    dtr, _ = _sandbox(tmp_path, monkeypatch, {})
    monkeypatch.setattr(dtr, "http", lambda *a, **k: (401, {"errorMessage": "Invalid Token"}))
    assert dtr.renew("1000000001", "old") is None
    assert "RenewToken failed: HTTP 401 Invalid Token" in dtr.LOG.read_text()


def test_expired_token_falls_back_to_pin_and_totp_and_is_saved(monkeypatch, tmp_path):
    dtr, path = _sandbox(tmp_path, monkeypatch, {
        "dhan_client_id": "1000000001", "dhan_access_token": "expired", "other": 1,
        "dhan_pin": "123456", "dhan_totp_secret": "JBSWY3DPEHPK3PXP"})
    calls = []

    def fake_http(method, url, headers=None):
        calls.append(url.split("?")[0])
        if "RenewToken" in url:
            return 401, {"errorMessage": "Token expired"}
        return 200, {"accessToken": "generated"}

    monkeypatch.setattr(dtr, "http", fake_http)
    monkeypatch.setattr(dtr, "token_valid", lambda cid, tok: tok == "generated")
    assert dtr.main() == 0
    assert calls == ["https://api.dhan.co/v2/RenewToken", "https://auth.dhan.co/app/generateAccessToken"]
    saved = json.loads(path.read_text())
    assert saved["dhan_access_token"] == "generated" and saved["other"] == 1


def test_without_pin_and_totp_an_expired_token_fails_loudly(monkeypatch, tmp_path):
    dtr, path = _sandbox(tmp_path, monkeypatch, {"dhan_client_id": "1000000001", "dhan_access_token": "expired"})
    monkeypatch.setattr(dtr, "http", lambda *a, **k: (401, {}))
    monkeypatch.setattr(dtr, "secret", lambda cfg, key, name: cfg.get(key, ""))   # no secret store
    assert dtr.main() == 1
    assert "dhan_pin / dhan_totp_secret are not set" in dtr.LOG.read_text()
    assert json.loads(path.read_text())["dhan_access_token"] == "expired", "config left alone"


def test_macos_agent_runs_the_refresh_before_the_premarket_pipeline():
    label = "com.atip.dhan-token-refresh"
    with open(REPO / "deploy" / "launchd" / f"{label}.plist", "rb") as f:
        agent = plistlib.load(f)
    assert agent["Label"] == label
    when = agent["StartCalendarInterval"]
    assert (when["Hour"], when["Minute"]) < (7, 0), "the 07:00 pre-market run makes the first Dhan call"
    assert agent["RunAtLoad"] is True, "a Mac that was off at the scheduled time must still refresh"

    wrapper = agent["ProgramArguments"][0].replace("__ATIP_DIR__/", "")
    assert (REPO / wrapper).is_file() and os.access(REPO / wrapper, os.X_OK)
    assert "tools/dhan_token_refresh.py" in (REPO / wrapper).read_text()

    install = (REPO / "deploy" / "launchd" / "install.sh").read_text()
    assert f'TOKEN_LABEL="{label}"' in install and 'install_one "$TOKEN_LABEL"' in install
