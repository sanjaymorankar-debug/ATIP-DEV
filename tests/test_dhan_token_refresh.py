"""
tools/dhan_token_refresh.py and the macOS agent that runs it.

The move to macOS ported the platform and snapshot launchers but not the Windows task
"ATIP_DhanTokenRefresh", so nothing renewed the 24-hour token; and RenewToken sent the
client id under a header Dhan does not read. No test here reaches Dhan: http() is stubbed.
"""
import base64
import importlib.util
import json
import os
import plistlib
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

from tests.test_session_pools import api  # noqa: F401  (pytest fixture)
from tests.test_w39_qa_suite import paper  # noqa: F401  (pytest fixture)

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
    dtr.alerts = []                                        # never a real Telegram message from a test
    monkeypatch.setattr(dtr, "alert", lambda title, body, ok=False: dtr.alerts.append((title, body, ok)))
    return dtr, path


def _jwt(exp, iat=None):
    """A token shaped like Dhan's: a JWT whose payload carries exp / iat."""
    def part(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    claims = {"dhanClientId": "1000000001", "exp": int(exp.timestamp())}
    if iat:
        claims["iat"] = int(iat.timestamp())
    return f"{part({'typ': 'JWT'})}.{part(claims)}.sig"


PIN_TOTP = {"dhan_client_id": "1000000001", "dhan_access_token": "expired",
            "dhan_pin": "123456", "dhan_totp_secret": "JBSWY3DPEHPK3PXP"}


def _dhan(dtr, monkeypatch, answers):
    """Stub Dhan: RenewToken refuses; generateAccessToken gives `answers` in turn. Returns the codes sent."""
    sent, answers = [], list(answers)

    def fake_http(method, url, headers=None):
        if "RenewToken" in url:
            return 500, {"errorMessage": "Renewal of token not allowed for this token type"}
        sent.append(url)
        return 200, answers.pop(0)

    monkeypatch.setattr(dtr, "http", fake_http)
    monkeypatch.setattr(dtr, "token_valid", lambda cid, tok: tok == "generated")
    monkeypatch.setattr(dtr.time, "sleep", lambda s: sent.append(("slept", s)))
    return sent


def _log_today(dtr, *messages):
    now = datetime.now()
    dtr.LOG.write_text("".join(f"{now:%Y-%m-%d} 06:30:0{i}  {m}\n" for i, m in enumerate(messages)))


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
    when = [(w["Hour"], w["Minute"]) for w in agent["StartCalendarInterval"]]
    assert when[0] < (7, 0), "the 07:00 pre-market run makes the first Dhan call"
    assert when == sorted(when) and when[-1] < (9, 15), "the re-runs come before the 09:15 open"
    assert tuple(when) == _load().SLOTS, "the script's next-try times are the agent's"
    assert agent["RunAtLoad"] is True, "a Mac that was off at the scheduled time must still refresh"

    wrapper = agent["ProgramArguments"][0].replace("__ATIP_DIR__/", "")
    assert (REPO / wrapper).is_file() and os.access(REPO / wrapper, os.X_OK)
    assert "tools/dhan_token_refresh.py" in (REPO / wrapper).read_text()

    install = (REPO / "deploy" / "launchd" / "install.sh").read_text()
    assert f'TOKEN_LABEL="{label}"' in install and 'install_one "$TOKEN_LABEL"' in install


# ── 30 Oct 2026: Dhan rejected a correct code ("Invalid TOTP") on 8 of 16 October mornings, and the
#    one attempt a day left ATIP without prices all day with nothing on the dashboard saying so.

def test_a_rejected_code_is_followed_by_the_next_one(monkeypatch, tmp_path):
    dtr, path = _sandbox(tmp_path, monkeypatch, dict(PIN_TOTP))
    sent = _dhan(dtr, monkeypatch, [{"message": "Invalid TOTP"}, {"accessToken": "generated"}])
    assert dtr.main() == 0
    assert len([s for s in sent if isinstance(s, str)]) == 2
    wait = [s[1] for s in sent if isinstance(s, tuple)]
    assert len(wait) == 1 and 2 <= wait[0] <= 32, "into the next 30-second window: a fresh code"
    assert json.loads(path.read_text())["dhan_access_token"] == "generated"
    assert "Invalid TOTP (code 1 of 2)" in dtr.LOG.read_text() and dtr.alerts == []


def test_a_run_sends_two_codes_then_tells_the_owner(monkeypatch, tmp_path):
    dtr, path = _sandbox(tmp_path, monkeypatch, dict(PIN_TOTP))
    sent = _dhan(dtr, monkeypatch, [{"message": "Invalid TOTP"}] * 3)
    assert dtr.main() == 1
    assert len([s for s in sent if isinstance(s, str)]) == dtr.TRIES_PER_RUN == 2
    assert dtr.rejected_today() == 2
    (title, body, ok), = dtr.alerts
    assert title == "Dhan token not refreshed" and not ok
    assert "Invalid TOTP (code 2 of 2)" in body and "cannot trade" in body and "web.dhan.co" in body
    assert json.loads(path.read_text())["dhan_access_token"] == "expired", "config left alone"


def test_another_answer_is_not_retried(monkeypatch, tmp_path):
    dtr, _ = _sandbox(tmp_path, monkeypatch, dict(PIN_TOTP))
    sent = _dhan(dtr, monkeypatch, [{"message": "Token can be generated once every 2 minutes."}])
    assert dtr.main() == 1 and len(sent) == 1 and dtr.rejected_today() == 0


def test_the_days_runs_stop_at_four_rejected_codes(monkeypatch, tmp_path):
    """Dhan locks the TOTP login after 5 wrong codes in a row: the runs share a day's count of 4."""
    dtr, _ = _sandbox(tmp_path, monkeypatch, dict(PIN_TOTP))
    _log_today(dtr, "generateAccessToken failed: HTTP 200 Invalid TOTP (code 1 of 2)",
               "generateAccessToken failed: HTTP 200 Invalid TOTP (code 2 of 2)",
               "generateAccessToken failed: HTTP 200 Invalid TOTP (code 1 of 2)")
    sent = _dhan(dtr, monkeypatch, [{"message": "Invalid TOTP"}] * 2)
    assert dtr.main() == 1 and len(sent) == 1, "only one code left today"
    assert dtr.rejected_today() == 4
    sent.clear()
    assert dtr.main() == 1 and sent == [], "no code at all once 4 were rejected"
    assert "no login code sent: Dhan has rejected 4 today" in dtr.LOG.read_text()
    assert "No more automatic tries today: Dhan has rejected 4 codes" in dtr.alerts[-1][1]


def test_a_success_resets_the_count_and_yesterday_does_not_count(monkeypatch, tmp_path):
    dtr, _ = _sandbox(tmp_path, monkeypatch, {})
    _log_today(dtr, "generateAccessToken failed: HTTP 200 Invalid TOTP", "OK: new Dhan token saved via x",
               "generateAccessToken failed: HTTP 200 Invalid TOTP")
    assert dtr.rejected_today() == 1
    dtr.LOG.write_text("2026-01-01 06:30:00  generateAccessToken failed: HTTP 200 Invalid TOTP\n")
    assert dtr.rejected_today() == 0


def test_a_token_that_lasts_the_day_is_left_alone(monkeypatch, tmp_path):
    tok = _jwt(datetime.now() + timedelta(days=2))
    dtr, path = _sandbox(tmp_path, monkeypatch, {**PIN_TOTP, "dhan_access_token": tok})
    sent = _dhan(dtr, monkeypatch, [{"accessToken": "generated"}])
    monkeypatch.setattr(dtr, "token_valid", lambda cid, t: t in (tok, "generated"))
    assert dtr.main() == 0 and sent == []
    assert "nothing to do" in dtr.LOG.read_text()
    monkeypatch.setattr(sys, "argv", ["dhan_token_refresh.py", "--force"])
    assert dtr.main() == 0 and len(sent) == 1 and json.loads(path.read_text())["dhan_access_token"] == "generated"


def test_lasts_the_day_means_past_18_00_today(monkeypatch, tmp_path):
    dtr, _ = _sandbox(tmp_path, monkeypatch, {})
    monkeypatch.setattr(dtr, "token_valid", lambda cid, tok: True)
    at_0630 = datetime(2026, 10, 30, 6, 30)
    assert not dtr.lasts_the_day("c", _jwt(datetime(2026, 10, 30, 6, 30, 5)), now=at_0630), "yesterday's token"
    assert not dtr.lasts_the_day("c", _jwt(datetime(2026, 10, 30, 15, 0)), now=datetime(2026, 10, 30, 8, 0))
    assert dtr.lasts_the_day("c", _jwt(datetime(2026, 10, 31, 6, 30)), now=datetime(2026, 10, 30, 8, 0))
    assert dtr.lasts_the_day("c", _jwt(datetime(2026, 10, 31, 6, 30)), now=datetime(2026, 10, 30, 20, 0))
    assert not dtr.lasts_the_day("c", "not-a-jwt", now=at_0630), "no readable expiry: refresh as before"
    monkeypatch.setattr(dtr, "token_valid", lambda cid, tok: False)
    assert not dtr.lasts_the_day("c", _jwt(datetime(2026, 10, 31, 6, 30)), now=at_0630), "refused by Dhan"


def test_next_slot():
    dtr = _load()
    assert dtr.next_slot(datetime(2026, 10, 30, 6, 30)) == "08:00"
    assert dtr.next_slot(datetime(2026, 10, 30, 8, 30)) == "09:00"
    assert dtr.next_slot(datetime(2026, 10, 30, 9, 0)) is None


def test_a_success_after_a_failure_alert_says_so(monkeypatch, tmp_path):
    dtr, _ = _sandbox(tmp_path, monkeypatch, dict(PIN_TOTP))
    _log_today(dtr, "generateAccessToken failed: HTTP 200 Invalid TOTP", "alert: Dhan token not refreshed")
    _dhan(dtr, monkeypatch, [{"accessToken": "generated"}])
    assert dtr.main() == 0
    assert [(t, ok) for t, _, ok in dtr.alerts] == [("Dhan token refreshed", True)]


def test_the_alert_is_recorded_and_sent_through_alerts_telegram(monkeypatch, tmp_path):
    dtr, _ = _sandbox(tmp_path, monkeypatch, {})
    dtr = _load()                                          # the real alert(), with a stand-in alerts.telegram
    monkeypatch.setattr(dtr, "LOG", tmp_path / "atip_data" / "dhan_token_refresh.log")
    seen = []
    fake = types.SimpleNamespace(fmt=lambda emoji, title, body: f"{emoji} {title}: {body}",
                                 notify=lambda msg, **kw: seen.append((msg, kw)))
    monkeypatch.setitem(sys.modules, "alerts.telegram", fake)
    dtr.alert("Dhan token not refreshed", "why")
    assert seen == [("❌ Dhan token not refreshed: why", {"category": "job", "severity": "error"})]
    assert "alert: Dhan token not refreshed" in dtr.LOG.read_text()
    fake.notify = lambda msg, **kw: 1 / 0
    dtr.alert("Dhan token not refreshed", "why")           # never fails the run
    assert "alert not sent (Dhan token not refreshed): ZeroDivisionError" in dtr.LOG.read_text()


# ── the red banner on every dashboard page (ops/dhan_token.py, dashboard/global_asia.py) ──

def _token_status(monkeypatch, tmp_path, tok, now, log=(), refused=None, cfg=None):
    from ops import dhan_token
    tool = dhan_token._refresh_tool()
    (tmp_path / "atip_data").mkdir(exist_ok=True)
    monkeypatch.setattr(tool, "LOG", tmp_path / "atip_data" / "dhan_token_refresh.log")
    monkeypatch.setattr(tool, "CFG", tmp_path / "atip_data" / "config.json")
    tool.CFG.write_text(json.dumps(cfg if cfg is not None else {"dhan_access_token": ""}))
    tool.LOG.write_text("".join(f"{datetime.now():%Y-%m-%d} 06:30:0{i}  {m}\n" for i, m in enumerate(log)))
    monkeypatch.setattr(dhan_token, "_token", lambda: tok)
    monkeypatch.setattr(dhan_token, "_refused_at", lambda issued: refused)
    return dhan_token.status(now)


def test_banner_an_expired_token(monkeypatch, tmp_path):
    s = _token_status(monkeypatch, tmp_path, _jwt(datetime(2026, 10, 29, 6, 30)), datetime(2026, 10, 30, 7, 0),
                      log=["RenewToken failed: HTTP 401 x", "generateAccessToken failed: HTTP 200 Invalid TOTP",
                           "alert: Dhan token not refreshed"])
    assert (s["state"], s["severity"], s["next_try"]) == ("EXPIRED", "error", "08:00")
    assert s["message"].startswith("Dhan token expired 29 Oct 06:30: ATIP has no live prices and cannot trade.")
    assert "Invalid TOTP" in s["message"] and "Next automatic try at 08:00." in s["message"]
    assert [f["cmd"] for f in s["fix"]] == ["tools/dhan_token_refresh.sh",
                                            ".venv/bin/python -m ops vault-set DHAN_ACCESS_TOKEN"]


def test_banner_a_missing_refused_or_capped_token(monkeypatch, tmp_path):
    now = datetime(2026, 10, 30, 10, 0)
    assert _token_status(monkeypatch, tmp_path, "", now)["state"] == "MISSING"
    s = _token_status(monkeypatch, tmp_path, _jwt(datetime(2026, 10, 31, 6, 30)), now,
                      refused=datetime(2026, 10, 30, 8, 20))
    assert s["state"] == "EXPIRED" and "refused ATIP's token (DH-901) at 30 Oct 08:20" in s["message"]
    s = _token_status(monkeypatch, tmp_path, _jwt(datetime(2026, 10, 29, 6, 30)), now,
                      log=["generateAccessToken failed: HTTP 200 Invalid TOTP"] * 4,
                      cfg={"dhan_access_token": "legacy"})
    assert "will not try again today" in s["message"] and s["next_try"] is None
    assert [f["cmd"] for f in s["fix"]] == [None] and "atip_data/config.json" in s["fix"][0]["text"]


def test_banner_quiet_while_the_token_is_good(monkeypatch, tmp_path):
    good = _token_status(monkeypatch, tmp_path, _jwt(datetime(2026, 10, 31, 6, 30)), datetime(2026, 10, 30, 10, 0))
    assert (good["state"], good["message"]) == ("OK", None)
    early = _token_status(monkeypatch, tmp_path, _jwt(datetime(2026, 10, 30, 6, 30, 5)), datetime(2026, 10, 30, 6, 0))
    assert early["state"] == "OK", "before 06:35 the morning refresh has yet to replace yesterday's token"
    late = _token_status(monkeypatch, tmp_path, _jwt(datetime(2026, 10, 30, 15, 0)), datetime(2026, 10, 30, 10, 0))
    assert (late["state"], late["severity"]) == ("EXPIRING", "warn") and "runs out at 15:00 today" in late["message"]


def test_every_page_carries_the_token_banner(api, monkeypatch):
    from dashboard import global_asia
    from ops import dhan_token
    client, _ = api
    bad = {"state": "EXPIRED", "severity": "error", "message": "Dhan token expired 29 Oct 06:30: <b>x</b>",
           "fix": [{"text": "Run:", "cmd": "tools/dhan_token_refresh.sh"}]}
    monkeypatch.setattr(dhan_token, "status", lambda now=None: bad)
    assert client.get("/api/status").json()["dhan_token"] == bad
    for page in ("/command", "/command/pnl"):
        html = client.get(page).text
        assert 'id="atipTokenBanner"' in html and '"state": "EXPIRED"' in html
    html = global_asia.status_banner_html()
    assert "<\\/b>" in html and "<b>x</b>" not in html, "embedded so it cannot close the script"
