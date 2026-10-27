"""
NI-01..NI-05 (2026-10-27): the News Impact Estimate -- which headlines are assessed, the Claude and keyword-rule
paths, combining into a session call with sectors and drivers, the tracking check against the Nifty close, the
job end to end from the feeds, and where it shows (API, pages, pre-market report, morning brief, scheduler).
No test reaches the network or the Anthropic API: news_ai._call and the feed pipeline are replaced.
"""

import json
from datetime import datetime, timedelta

import pytest

NOW = datetime(2026, 10, 27, 7, 40)          # a Tuesday, before the open


@pytest.fixture
def NI(temp_db, tmp_path, monkeypatch):
    from db.schema import init_db
    from data import news_impact
    init_db()
    monkeypatch.chdir(tmp_path)               # no atip_data/config.json: defaults, news AI off
    return news_impact


def _conn():
    from db.schema import get_connection
    return get_connection()


def art(c, headline, at=NOW - timedelta(hours=2), importance="HIGH", category="GLOBAL", sentiment=-0.6,
        confidence=0.7, source="Economic Times", dup_of=None):
    cur = c.execute("INSERT INTO news_articles (fetched_at, headline, source, url, category, symbols_mentioned, "
                    "sentiment, importance, confidence, classifier, dup_of) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (at.strftime("%Y-%m-%d %H:%M:%S"), headline, source, "", category, "[]", sentiment, importance,
                     confidence, "rule", dup_of))
    c.commit()
    return cur.lastrowid


def close_row(c, day, nifty, **sectors):
    cols = ["date", "time", "nifty50_chg"] + list(sectors)
    c.execute(f"INSERT INTO index_levels ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
              [day, "15:30:00", nifty] + list(sectors.values()))
    c.commit()


def claude(monkeypatch, items_for, brief=None):
    """Replace the Anthropic call: items_for(headlines) -> items; the brief call returns `brief`."""
    from data import news_ai
    calls = []

    def fake(conn, purpose, system, user, schema, max_tokens):
        calls.append(purpose)
        if purpose == "news_impact_brief":
            if brief is None:
                raise news_ai._Unavailable("no brief")
            return brief
        heads = [ln.split("] ", 1)[1] for ln in user.splitlines() if ln[:1].isdigit()]
        return {"items": items_for(heads)}
    monkeypatch.setattr(news_ai, "_call", fake)
    return calls


def item(i, d="DOWN", m="MEDIUM", sectors=(), conf=0.8, rel=0.9, tr="CRUDE_COMMODITIES"):
    return {"i": i, "india_relevance": rel, "nifty_direction": d, "magnitude": m, "horizon": "NEXT_SESSION",
            "transmission": tr, "sectors": [{"sector": s, "direction": sd} for s, sd in sectors],
            "confidence": conf, "rationale": "test"}


# ── sessions ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("now,expect", [
    (datetime(2026, 10, 27, 7, 40), "2026-10-27"),     # before the open: today
    (datetime(2026, 10, 27, 12, 0), "2026-10-27"),     # in session: today
    (datetime(2026, 10, 27, 15, 50), "2026-10-28"),    # after the close: the next session
    (datetime(2026, 10, 30, 20, 15), "2026-11-02"),    # Friday evening: Monday
    (datetime(2026, 10, 31, 9, 0), "2026-11-02"),      # Saturday
])
def test_target_session(NI, now, expect):
    assert NI.target_session(now) == expect


# ── which headlines ────────────────────────────────────────────────────────
@pytest.mark.parametrize("h", ["Sensex jumps 483 points, Nifty rises 153 points amid value buying",
                               "Stock Market Highlights: Sensex today", "Top 3 stocks to buy tomorrow: Laurus Labs",
                               "US Stock Market Live Updates: futures steady", "Market Wrap: Nifty Climbs 153 Points"])
def test_market_recaps_are_recognised(NI, h):
    assert NI.is_recap(h)


@pytest.mark.parametrize("h", ["Brent crude jumps 4% after OPEC+ cuts output", "RBI cuts repo rate by 25 bps",
                               "FIIs sell Rs 8,000 crore of Indian shares in October"])
def test_market_moving_news_is_not_a_recap(NI, h):
    assert not NI.is_recap(h)


def test_pending_takes_fresh_high_and_medium_headlines_once_and_skips_repeats(NI):
    c = _conn()
    try:
        NI.ensure_tables(c)
        a = art(c, "Brent crude jumps 4% after OPEC+ cuts output")
        art(c, "Brent crude jumps 4% after OPEC cut, says report", dup_of=a)            # the same story again
        art(c, "Company X opens a store", importance="LOW")                            # not market-relevant
        art(c, "Fed holds rates", at=NOW - timedelta(hours=30))                        # outside 18 h
        r = art(c, "Sensex jumps 483 points as banks rally")                            # recap
        todo, recaps = NI.pending(c, NOW)
        assert [x["id"] for x in todo] == [a] and [x["id"] for x in recaps] == [r]
        NI.assess_pending(c, NOW)
        assert NI.pending(c, NOW) == ([], []), "assessed headlines and recaps are not offered again"
        assert c.execute("SELECT method FROM news_impact WHERE article_id=?", (r,)).fetchone()[0] == "recap"
    finally:
        c.close()


# ── the keyword-rule path ──────────────────────────────────────────────────
def test_rule_path_reads_dearer_crude_as_bad_for_india(NI):
    x = NI.assess_rule({"headline": "Brent crude surges as Gulf supply fears grow", "category": "COMMODITIES",
                        "importance": "HIGH", "sentiment": 0.7, "confidence": 0.8})
    assert x["nifty_direction"] == "DOWN" and x["magnitude"] == "MEDIUM" and x["transmission"] == "CRUDE_COMMODITIES"
    assert {"sector": "ENERGY", "direction": "DOWN"} in x["sectors"] and x["confidence"] <= 0.5


@pytest.mark.parametrize("h,cat,sent,expect", [
    ("KOSPI Surges 3.46% on U.S. AI Rebound, Oil Decline", "GLOBAL", 0.6, "UP"),
    ("Rupee slips below to 95.18 against dollar on elevated oil prices", "GENERAL", -0.3, "DOWN"),
    ("Gold gains as oil slide cools some inflation concerns", "COMMODITIES", 0.3, "UP"),
    ("Wall Street slumps as Treasury yields jump", "GLOBAL", -0.6, "DOWN"),
    ("US inflation cools more than expected", "MACRO", 0.0, "UP"),
    ("Tamilnad Mercantile Bank Q2 Results: Net profit rises 42%", "REGULATORY_LEGAL", 0.6, "NONE"),
    ("Netflix Stock Falls on Report That Viewers Watch Less", "GLOBAL", -0.5, "NONE"),
])
def test_rule_path_reads_the_move_next_to_crude_yields_and_the_dollar(NI, h, cat, sent, expect):
    """Found on the live feeds 2026-10-27: the whole headline's tone, inverted, read "KOSPI surges on ... oil
    decline" as bad for India and one bank's results as a Nifty call."""
    x = NI.assess_rule({"headline": h, "category": cat, "importance": "HIGH", "sentiment": sent, "confidence": 0.7})
    assert x["nifty_direction"] == expect


def test_rule_path_keeps_company_news_off_the_nifty_and_never_says_large(NI):
    e = NI.assess_rule({"headline": "Tata Consumer Q2 profit rises 16%", "category": "EARNINGS",
                        "importance": "HIGH", "sentiment": 0.8, "confidence": 0.9})
    g = NI.assess_rule({"headline": "PTC India plans mineral exchange", "category": "GENERAL",
                        "importance": "MEDIUM", "sentiment": 0.5, "confidence": 0.6})
    assert e["nifty_direction"] == "NONE" and e["transmission"] == "STOCK_SPECIFIC"
    assert g["nifty_direction"] == "NONE", "a GENERAL headline naming no market-wide channel is company news"
    assert "LARGE" not in (e["magnitude"], g["magnitude"]) and e["method"] == "rule"


def test_without_claude_every_headline_is_assessed_by_rules_and_says_why(NI):
    c = _conn()
    try:
        art(c, "Brent crude surges 5% as Gulf supply fears grow", category="COMMODITIES", sentiment=0.7)
        out = NI.assess_pending(c, NOW)
    finally:
        c.close()
    assert out["rule"] == 1 and out["claude"] == 0 and "ai_enabled is false" in out["fallback_reason"]


# ── the Claude path ────────────────────────────────────────────────────────
def test_claude_assessments_are_stored_and_bad_values_cleaned(NI, monkeypatch):
    calls = claude(monkeypatch, lambda hs: [
        {**item(0, sectors=[("ENERGY", "DOWN"), ("NOT_A_SECTOR", "UP"), ("ENERGY", "UP")]), "confidence": 3},
        item(1, d="NONE", m="LARGE")])
    c = _conn()
    try:
        a = art(c, "Brent crude surges 5% as Gulf supply fears grow")
        b = art(c, "Fed holds rates as expected", importance="MEDIUM")
        out = NI.assess_pending(c, NOW)
        ra = dict(c.execute("SELECT * FROM news_impact WHERE article_id=?", (a,)).fetchone())
        rb = dict(c.execute("SELECT * FROM news_impact WHERE article_id=?", (b,)).fetchone())
    finally:
        c.close()
    assert calls == ["news_impact"] and out["claude"] == 2 and out["fallback_reason"] is None
    assert ra["method"] == "claude" and ra["confidence"] == 1.0 and ra["for_session"] == "2026-10-27"
    assert json.loads(ra["sectors_json"]) == [{"sector": "ENERGY", "direction": "DOWN"}]
    assert rb["magnitude"] == "NONE", "no direction and no sector call means no size"


def test_a_headline_the_model_skipped_falls_back_to_rules(NI, monkeypatch):
    claude(monkeypatch, lambda hs: [item(0)])
    c = _conn()
    try:
        art(c, "Brent crude surges 5% as Gulf supply fears grow")
        art(c, "RBI cuts repo rate by 25 bps", category="RBI_POLICY", sentiment=-0.4, importance="MEDIUM")
        out = NI.assess_pending(c, NOW)
    finally:
        c.close()
    assert out["claude"] == 1 and out["rule"] == 1


# ── combining ──────────────────────────────────────────────────────────────
def test_negative_news_gives_a_negative_session_call_with_sectors_and_drivers(NI, monkeypatch):
    claude(monkeypatch, lambda hs: [item(i, sectors=[("ENERGY", "DOWN")]) for i in range(len(hs))],
           brief={"summary": "Crude is the story.", "bullets": [{"text": "crude", "refs": [0, 9]}], "watch": ["OMCs"]})
    c = _conn()
    try:
        for k in range(4):
            art(c, f"Brent crude surges {k + 3}% as Gulf supply fears grow", at=NOW - timedelta(hours=1 + k))
        NI.assess_pending(c, NOW)
        g = NI.aggregate(c, NOW, "premarket")
        stored = c.execute("SELECT bias, n_claude FROM news_impact_daily WHERE for_session='2026-10-27' AND "
                           "run_label='premarket'").fetchone()
    finally:
        c.close()
    assert g["bias"] == "NEGATIVE" and g["score"] <= 42 and g["strength"] in ("MODERATE", "STRONG")
    assert g["sectors"][0]["sector"] == "ENERGY" and g["sectors"][0]["bias"] == "NEGATIVE"
    assert len(g["drivers"]) == 4 and all(d["points"] < 0 for d in g["drivers"])
    assert g["brief_method"] == "claude" and g["brief"]["bullets"][0]["article_ids"] == [g["drivers"][0]["article_id"]]
    assert tuple(stored) == ("NEGATIVE", 4) and g["informational_only"]


def test_older_news_counts_less(NI):
    cfg = NI.settings()
    r = {"nifty_direction": "DOWN", "magnitude": "MEDIUM", "confidence": 1.0, "india_relevance": 1.0}
    fresh = NI._points({**r, "published_at": str(NOW)}, "DOWN", NOW, cfg)
    old = NI._points({**r, "published_at": str(NOW - timedelta(hours=12))}, "DOWN", NOW, cfg)
    assert fresh == -2.0 and old == pytest.approx(-1.0)


def test_no_news_is_a_neutral_call_with_a_plain_brief(NI):
    c = _conn()
    try:
        g = NI.aggregate(c, NOW, "premarket")
    finally:
        c.close()
    assert g["bias"] == "NEUTRAL" and g["score"] == 50 and g["n_items"] == 0 and "No market-moving" in g["brief"]["summary"]


def test_brief_falls_back_to_rules_when_claude_is_unavailable(NI):
    c = _conn()
    try:
        art(c, "Brent crude surges 5% as Gulf supply fears grow", category="COMMODITIES", sentiment=0.7)
        NI.assess_pending(c, NOW)
        g = NI.aggregate(c, NOW, "premarket")
    finally:
        c.close()
    assert g["brief_method"] == "rule" and "keyword rules, not AI" in g["brief"]["summary"] and g["fallback_reason"]


# ── tracking ───────────────────────────────────────────────────────────────
def _call_for(NI, monkeypatch, direction, when=NOW, label="premarket"):
    claude(monkeypatch, lambda hs: [item(i, d=direction, sectors=[("IT", "UP")]) for i in range(len(hs))])
    c = _conn()
    try:
        art(c, f"Big news {direction} {when}", at=when - timedelta(hours=1))
        NI.assess_pending(c, when)
        return NI.aggregate(c, when, label)
    finally:
        c.close()


def test_a_call_is_scored_against_the_nifty_close_of_its_session(NI, monkeypatch):
    _call_for(NI, monkeypatch, "DOWN")
    c = _conn()
    try:
        assert NI.evaluate(c, NOW)["evaluated"] == 0, "the session has not closed yet"
        close_row(c, "2026-10-27", -0.85, nifty_it_chg=1.2)
        out = NI.evaluate(c, datetime(2026, 10, 27, 16, 20))
        e = dict(c.execute("SELECT * FROM news_impact_eval").fetchone())
        h = c.execute("SELECT actual_chg, hit FROM news_impact").fetchone()
    finally:
        c.close()
    assert out["evaluated"] == 1 and out["headlines_evaluated"] == 1
    assert e["outcome"] == "NEGATIVE" and e["hit"] == 1 and e["pre_open"] == 1 and e["method"] == "claude"
    assert json.loads(e["sectors_json"]) == [{"sector": "IT", "bias": "POSITIVE", "actual_chg": 1.2, "hit": 1}]
    assert tuple(h) == (-0.85, 1)


def test_a_neutral_call_is_right_inside_the_band(NI):
    assert NI.outcome(0.2, 0.3) == "NEUTRAL" and NI.outcome(0.31, 0.3) == "POSITIVE" and NI.outcome(-0.5, 0.3) == "NEGATIVE"


def test_a_session_with_no_close_data_is_given_up_after_some_days(NI, monkeypatch):
    _call_for(NI, monkeypatch, "UP")
    c = _conn()
    try:
        assert NI.evaluate(c, datetime(2026, 10, 29, 17, 0))["no_data"] == 0
        assert NI.evaluate(c, datetime(2026, 11, 5, 17, 0))["no_data"] == 1
        assert c.execute("SELECT outcome FROM news_impact_eval").fetchone()[0] == "NO_DATA"
    finally:
        c.close()


def test_accuracy_uses_the_last_pre_open_call_and_shows_baselines(NI, monkeypatch):
    days = [("2026-10-14", "DOWN", -0.9), ("2026-10-15", "UP", 0.8), ("2026-10-16", "UP", -0.6)]   # (20 Oct: Dussehra)
    for d, direction, chg in days:
        when = datetime.fromisoformat(d + " 07:40")
        _call_for(NI, monkeypatch, direction, when)
        _call_for(NI, monkeypatch, "UP" if direction == "DOWN" else "DOWN", when + timedelta(hours=5), "midday")
        c = _conn()
        try:
            close_row(c, d, chg)
        finally:
            c.close()
    c = _conn()
    try:
        NI.evaluate(c, datetime(2026, 10, 19, 9, 0))
        monkeypatch.setattr(NI, "_now", lambda now=None: datetime(2026, 10, 19, 9, 0))
        acc = NI.accuracy(c, 30)
    finally:
        c.close()
    assert acc["sessions"] == 3, "the midday call is not the one a trader read before the open"
    assert acc["hit_rate_pct"] == pytest.approx(66.7) and acc["by_method"]["claude"]["sessions"] == 3
    assert "yesterday_again" in acc["baselines"] and acc["note"] == "too few sessions to judge yet"


# ── the job, end to end from the feeds ─────────────────────────────────────
def test_run_fetches_indian_and_world_feeds_then_assesses_and_combines(NI, monkeypatch):
    import data.news as news
    seen = []

    def fake_pipeline(hours_back=12, feeds=None):
        seen.append("world" if feeds else "india")
        c = _conn()
        try:
            art(c, "Wall Street slumps as Treasury yields jump" if feeds else "RBI surprises with a repo rate cut",
                at=datetime.now() - timedelta(hours=1), category="GLOBAL" if feeds else "RBI_POLICY")
        finally:
            c.close()
        return {"status": "SUCCESS", "rows": 1, "fetched": 1}
    monkeypatch.setattr(news, "run_news_pipeline", fake_pipeline)
    claude(monkeypatch, lambda hs: [item(i, d="DOWN" if "Wall Street" in h else "UP", tr="GLOBAL_RISK")
                                    for i, h in enumerate(hs)])
    out = NI.run("premarket")
    assert seen == ["india", "world"] and out["status"] == "SUCCESS" and out["assessed"]["claude"] == 2
    assert out["feeds"]["world"]["stored"] == 1 and out["bias"] in ("POSITIVE", "NEUTRAL", "NEGATIVE")


def test_run_can_be_switched_off(NI, tmp_path):
    (tmp_path / "atip_data").mkdir()
    (tmp_path / "atip_data" / "config.json").write_text(json.dumps({"news_impact": {"enabled": False}}))
    assert NI.run("premarket")["status"] == "SKIPPED"


# ── where it shows ─────────────────────────────────────────────────────────
@pytest.fixture
def client(NI):
    from fastapi.testclient import TestClient
    import dashboard.server as srv
    return TestClient(srv.app)


def test_api_and_every_page_carry_the_panel(NI, client, monkeypatch):
    claude(monkeypatch, lambda hs: [item(i) for i in range(len(hs))])
    c = _conn()
    try:
        art(c, "Brent crude surges 5% as Gulf supply fears grow", at=datetime.now() - timedelta(hours=1))
        NI.assess_pending(c)
        NI.aggregate(c, label="manual")
    finally:
        c.close()
    d = client.get("/api/news-impact").json()
    assert d["latest"]["bias"] in ("NEGATIVE", "NEUTRAL") and d["recent"][0]["method"] == "claude"
    assert d["latest"]["informational_only"] and "hit_rate_pct" in d["accuracy"]
    assert client.get("/api/news-impact/accuracy?days=30").json()["days"] == 30
    for page, box in (("/", "niNewsBox"), ("/", "niAsiaBox"), ("/global-asia", "niBox"), ("/m", "niBox")):
        html = client.get(page).text
        assert f'id="{box}"' in html and "atipLoadNewsImpact" in html, (page, box)


def test_refresh_needs_the_owner_token(NI, client, monkeypatch):
    import data.news_impact as mod
    monkeypatch.setattr(mod, "run", lambda label="manual", **k: {"status": "SUCCESS", "label": label})
    assert client.post("/api/news-impact/refresh").status_code == 401
    from dashboard.security import token
    r = client.post("/api/news-impact/refresh", headers={"X-ATIP-Token": token()})
    assert r.status_code == 200 and r.json() == {"status": "SUCCESS", "label": "manual"}


def test_premarket_report_and_morning_brief_carry_the_estimate(NI, monkeypatch):
    from pipeline.premarket_report import build
    from alerts.telegram import _market_lines
    c = _conn()
    try:
        assert "no news impact estimate yet" in build(c, NOW)["caveats"]
        art(c, "Brent crude surges 5% as Gulf supply fears grow", category="COMMODITIES", sentiment=0.7)
        NI.assess_pending(c, NOW)
        NI.aggregate(c, NOW, "resume")
        rep = build(c, NOW)
        lines = _market_lines(c, "2026-10-26", now=NOW)
    finally:
        c.close()
    assert rep["news_impact"]["for_session"] == "2026-10-27" and rep["news_impact"]["bias"]
    assert not [x for x in rep["caveats"] if "news impact" in x]
    ni = [x for x in lines if x.startswith("<b>News impact:</b>")]
    assert ni and "keyword rules, not AI" in ni[0]


def test_scheduler_runs_it_at_the_configured_times_and_scores_after_the_close(NI, monkeypatch):
    import schedule
    import pipeline.scheduler as S
    monkeypatch.setattr(schedule, "default_scheduler", schedule.Scheduler())
    monkeypatch.setattr(schedule, "jobs", schedule.default_scheduler.jobs)
    S._schedule_operating_jobs()
    jobs = {(str(j.at_time)[:5], getattr(j.job_func.func, "__name__", "")) for j in schedule.default_scheduler.jobs}
    for t in ("07:40", "12:20", "15:50", "20:15"):
        assert (t, "_news_impact_refresh") in jobs, t
    assert ("16:20", "_news_impact_evaluate") in jobs


def test_the_0600_resume_estimates_after_the_news_summary(NI, monkeypatch):
    import pipeline.operating_schedule as ops
    import data.news_ai as news_ai
    import data.news_impact as mod
    ran = []
    monkeypatch.setattr(news_ai, "run_summary_job", lambda: ran.append("summary") or {"status": "SUCCESS"})
    monkeypatch.setattr(mod, "run", lambda label, fetch_feeds=None: ran.append((label, fetch_feeds)) or {"status": "SUCCESS"})
    out = ops.step_sentiment({"started": NOW, "run_date": "2026-10-27"})
    assert ran == ["summary", ("resume", False)] and out["parts"]["news_impact_resume"] == "SUCCESS"


def test_tables_are_kept_by_the_retention_policy_and_classified(NI):
    from db import purge
    from enterprise.privacy import classify
    assert purge.LONG_RETENTION_TABLES["news_impact_daily"] == "for_session"
    assert purge.LONG_RETENTION_TABLES["news_impact_eval"] == "for_session"
    assert purge.SHORT_RETENTION_TABLES["news_impact"] == "published_at"
    for t in ("news_impact", "news_impact_daily", "news_impact_eval"):
        assert classify(t)["class"] == "research", t


def test_no_mysql_reserved_column_names(NI):
    from db import mysql as my
    assert not any(my.reserved_columns([d for d in NI.DDL if d.lstrip().startswith("CREATE TABLE")]).values())
