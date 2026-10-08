"""
W39 Phase 4 (item 1): English -> screener query. The rule translator on everyday phrasings; the Claude path
with a fake client (structured output, cached system prompt, one retry with the parser's error, usage in
ai_usage_log, its own daily cap); every failure falling back to the rules with the reason; nothing is ever
run, and nothing the parser rejects is passed off as valid.
"""
import json
import types

import pytest

from research import screener as SC
from research import screener_nl as NL

INDS = ["Capital Goods", "Information Technology", "Financial Services"]


@pytest.mark.parametrize("text,query,sort", [
    ("debt-free capital goods companies with ROCE above 20% near their 52-week high",
     'debt_equity < 0.1 AND from_52w_high_pct > -5 AND industry IN ("Capital Goods") AND roce_pct > 20', None),
    ("stocks with PE below 15 and dividend yield over 3%, sorted by dividend yield",
     "dividend_yield_pct > 3 AND pe < 15", "dividend_yield_pct"),
    ("golden cross today in information technology with RSI under 70",
     'scan_golden_cross = 1 AND industry IN ("Information Technology") AND rsi_14 < 70', None),
    ("market cap above 1,000 crore and ROE between 15 and 25",
     "market_cap_cr > 1000 AND roe_pct >= 15 AND roe_pct <= 25", None),
    ("high roce small caps with no pledge, cheapest first",
     'roce_pct > 20 AND pledged_pct < 1 AND cap_bucket = "SMALL" AND pe > 0', "pe"),
])
def test_rule_translator(text, query, sort):
    r = NL.translate_rules(text, INDS)
    assert r["query"] == query and r["sort"] == sort and r["unmatched"] == []
    SC.parse(r["query"])


def test_presets_by_name_and_what_was_not_understood():
    r = NL.translate_rules("show me the quality compounders", INDS)
    presets = {p["key"]: p for p in SC.PRESETS}
    assert r["query"] == presets["quality_compounders"]["query"] and "preset" in r["explanation"]
    g = NL.translate_rules("golden cross", INDS)
    assert g["query"] == presets["t_golden_cross"]["query"], "the request IS the preset's name"
    m = NL.translate_rules("roe above 20 that the moon likes", INDS)
    assert m["query"] == "roe_pct > 20" and m["unmatched"] == ["moon", "likes"]


# ── the Claude path, with a fake client ───────────────────────────────────

@pytest.fixture
def db(temp_db):
    from db.schema import get_connection, init_db
    init_db()
    SC.clear_cache()
    conn = get_connection()
    yield conn
    conn.close()
    SC.clear_cache()


class FakeClient:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return types.SimpleNamespace(
            stop_reason="end_turn", usage=types.SimpleNamespace(input_tokens=3000, output_tokens=80),
            content=[types.SimpleNamespace(type="text", text=json.dumps(a))])


def _ans(query, sort=None, desc=True):
    return {"query": query, "sort": sort, "desc": desc, "explanation": "x", "unmatched": []}


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(NL, "settings", lambda: dict(NL.DEFAULTS, enabled=True))


def test_claude_writes_the_query_with_a_cached_catalogue_and_logs_usage(db, on):
    fake = FakeClient([_ans("roce_pct > 20 AND debt_equity < 0.5", "roce_pct")])
    r = NL.translate(db, "quality businesses with little debt", industries=INDS, client=fake)
    assert r["mode"] == "claude" and r["valid"] and r["query"] == "roce_pct > 20 AND debt_equity < 0.5"
    kw = fake.calls[0]
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"} and "roce_pct" in kw["system"][0]["text"]
    assert "Capital Goods" in kw["system"][0]["text"] and kw["output_config"]["format"]["type"] == "json_schema"
    row = db.execute("SELECT purpose, model, input_tokens, cost_usd FROM ai_usage_log").fetchone()
    assert row[0] == "screener_nl" and row[1] == NL.DEFAULTS["model"] and row[2] == 3000 and row[3] > 0


def test_a_rejected_query_is_sent_back_once_with_the_parsers_error(db, on):
    fake = FakeClient([_ans("roce_pct >> 20"), _ans("roce_pct > 20", "nonsense_field")])
    r = NL.translate(db, "high roce", industries=INDS, client=fake)
    assert r["mode"] == "claude" and r["query"] == "roce_pct > 20" and r["sort"] is None, "unknown sort dropped"
    retry = fake.calls[1]["messages"]
    assert len(retry) == 3 and "rejected" in retry[2]["content"]
    fake2 = FakeClient([_ans("roce_pct >> 20"), _ans("nosuch > 1")])
    r2 = NL.translate(db, "high roce", industries=INDS, client=fake2)
    assert r2["mode"] == "rules" and r2["query"] == "roce_pct > 20" and "unknown field" in r2["fallback_reason"]


def test_failures_fall_back_to_the_rules_with_the_reason(db, on, monkeypatch):
    err = type("AuthenticationError", (Exception,), {"status_code": 401})("invalid x-api-key")
    r = NL.translate(db, "roe above 15", industries=INDS, client=FakeClient([err]))
    assert r["mode"] == "rules" and r["fallback_reason"] == "Anthropic API key rejected (401)" and r["valid"]
    assert db.execute("SELECT ok FROM ai_usage_log").fetchone()[0] in (0, False)
    db.execute("INSERT INTO ai_usage_log (day, created_at, purpose, model, cost_usd, ok) VALUES (date('now', 'localtime'), "
               "datetime('now'), 'screener_nl', 'm', 1.0, 1)")
    db.commit()
    r = NL.translate(db, "roe above 15", industries=INDS, client=FakeClient([]))
    assert r["mode"] == "rules" and "cost cap" in r["fallback_reason"]
    monkeypatch.setattr(NL, "settings", lambda: dict(NL.DEFAULTS))
    r = NL.translate(db, "roe above 15", industries=INDS)
    assert r["fallback_reason"] == "screener_ai.enabled is false"


def test_bad_input_and_nothing_understood(db):
    with pytest.raises(ValueError):
        NL.translate(db, "  ")
    with pytest.raises(ValueError):
        NL.translate(db, "x" * 501)
    r = NL.translate(db, "stocks the moon likes", industries=INDS)
    assert not r["valid"] and "nothing in the text" in r["error"] and r["query"] == ""


def test_api_and_page(tmp_path, monkeypatch, db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    from enterprise.authz import permission_for
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    client = TestClient(server.app)
    h = {security.TOKEN_HEADER: security.token()}
    assert client.post("/api/screener/ask", json={"text": "roe above 20"}).status_code == 401
    r = client.post("/api/screener/ask", headers=h, json={"text": "roe above 20 sorted by roe"}).json()
    assert r["query"] == "roe_pct > 20" and r["sort"] == "roe_pct" and r["mode"] == "rules" and r["valid"]
    assert client.post("/api/screener/ask", headers=h, json={"text": ""}).status_code == 400
    assert permission_for("POST", "/api/screener/ask") == "workspace:write"
    assert "Ask in English" in client.get("/screener").text
