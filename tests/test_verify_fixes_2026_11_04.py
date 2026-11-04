"""Phase 3 fixes of the 4 Nov 2026 verification pass (VF-02, VF-03, VF-04, VF-05, VF-07). The defect log and evidence
are in docs/reports/2026-11-04-verification/VERIFICATION_REPORT.md."""
import sqlite3
from datetime import datetime

from tests.test_autopick import _conn, _pick, _sessions, _signals, _tick
from tests.test_w39_qa_suite import TD, _run_postmarket, market, paper  # noqa: F401  (pytest fixtures)


# ── VF-02: a re-score replaces the day's components, it does not keep the ones it no longer used ──────────────

def _components_db():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE score_components (symbol TEXT NOT NULL, date DATE NOT NULL, index_name TEXT NOT NULL, "
              "component TEXT NOT NULL, value REAL, weight REAL, PRIMARY KEY (symbol, date, index_name, component))")
    return c


def test_a_rescore_drops_components_it_no_longer_uses():
    from scores.formulas import store_components
    c = _components_db()
    store_components(c, "NEWCO", "2026-11-03", {"RRI": {"components": {"Recovery": 40.0, "Support": 30.0},
                                                        "weights": {"Recovery": 0.25, "Support": 0.2}},
                                                "INS": {"components": {"FII": 60.0}, "weights": {"FII": 0.4}}})
    # the evening re-score: no 200-day average, so no Support; and no INS at all
    store_components(c, "NEWCO", "2026-11-03", {"RRI": {"components": {"Recovery": 41.0}, "weights": {"Recovery": 0.25}}})
    assert c.execute("SELECT index_name, component, value FROM score_components ORDER BY 1, 2").fetchall() == \
        [("RRI", "Recovery", 41.0)]


def test_the_market_row_replaces_only_its_own_indices():
    from scores.formulas import store_components
    c = _components_db()
    c.execute("INSERT INTO score_components VALUES ('__MARKET__', '2026-11-03', 'MH', 'VIX', 70, 0.1)")
    store_components(c, "__MARKET__", "2026-11-03", {"MSI": {"components": {"News": 55.0}, "weights": {"News": 0.3}}})
    store_components(c, "__MARKET__", "2026-11-03", {"MSI": {"components": {"FII": 50.0}, "weights": {"FII": 0.2}}})
    assert c.execute("SELECT index_name, component FROM score_components ORDER BY 1, 2").fetchall() == \
        [("MH", "VIX"), ("MSI", "FII")]


# ── VF-03: no indicators for the day, no score built from stand-ins ─────────────────────────────────────────────

def test_a_stock_without_indicators_is_not_scored_and_loses_its_earlier_row(market, monkeypatch):
    _run_postmarket(monkeypatch)
    from db.schema import get_connection
    from scores.engine import run_scoring_pipeline
    c = get_connection()
    assert c.execute("SELECT COUNT(*) FROM ai_scores WHERE symbol='AAA' AND date=?", (str(TD),)).fetchone()[0] == 1
    c.execute("DELETE FROM technical_indicators WHERE symbol='AAA' AND date=?", (str(TD),))
    c.commit(); c.close()
    out = run_scoring_pipeline(TD)
    assert out["status"] == "SUCCESS" and out["no_indicators"] == ["AAA"] and out["rows"] == 2
    c = get_connection()
    try:
        assert {r[0] for r in c.execute("SELECT symbol FROM ai_scores WHERE date=?", (str(TD),))} == {"BBB", "CCC"}
        assert c.execute("SELECT COUNT(*) FROM score_components WHERE symbol='AAA' AND date=?", (str(TD),)).fetchone()[0] == 0
    finally:
        c.close()


# ── VF-04: once the day's buys are used, auto-pick waits instead of queuing a buy the risk check refuses ────────

def test_autopick_waits_when_the_days_buy_count_is_used(paper, monkeypatch):
    import orders.risk as risk
    _signals(ACME=(80, "BUY", 100.0), BETA=(70, "BUY", 50.0))
    _sessions("S1", "S2")
    monkeypatch.setattr(risk, "limits", lambda: {"max_orders_per_day": 1})
    from orders.broker import _ensure_order_log_table
    c = _conn()
    _ensure_order_log_table(c)
    c.execute("INSERT INTO order_log (timestamp, symbol, transaction_type, quantity, status) VALUES (?, 'OTHER', 'BUY', 1, "
              "'PLACED')", (datetime.now().isoformat(timespec="seconds"),))
    c.commit(); c.close()
    p = _pick()
    for sid in ("S1", "S2"):
        assert p[sid]["type"] == "WAITING" and "today's 1 buys are used" in p[sid]["reason"], p[sid]
    monkeypatch.setattr(risk, "limits", lambda: {"max_orders_per_day": 2})       # one buy left: one session picks
    p = _pick()
    assert p["S1"]["type"] == "PICKED" and p["S1"]["symbol"] == "ACME"


# ── VF-05: Command Center's Confirm & send dialog gets the shares and the order value ───────────────────────────

def test_command_center_pending_rows_carry_shares_and_value(paper):
    from dashboard.command_routes import summary
    from orders import rules
    _signals(ACME=(80, "BUY", 100.0))
    _sessions("S1", auto_send=False)
    e = _pick()["S1"]
    _tick(paper, ACME=100.0)
    assert rules.get_rule(e["rule_id"])["status"] == rules.PENDING_CONFIRMATION
    c = _conn()
    try:
        row = next(r for r in summary(c)["pending"] if r["id"] == e["rule_id"])
    finally:
        c.close()
    assert (row["est_qty"], row["est_value"]) == (250, 25000.0)          # Rs 25,000 of session cash at Rs 100, not "100"


# ── VF-07: the shortfall reason names rank-fill when its stocks were all taken ──────────────────────────────────

def test_the_wait_reason_says_rank_fill_when_its_stocks_are_taken(paper, monkeypatch):
    import orders.autopick as ap
    monkeypatch.setattr(ap, "FILL_MIN_TURNOVER", 0)
    _signals(ACME=(80, "BUY", 100.0), GAMMA=(70, "HOLD", 100.0))
    _sessions("S1", "S2", "S3")
    p = _pick()
    assert (p["S1"]["symbol"], p["S2"]["symbol"]) == ("ACME", "GAMMA")
    assert p["S3"]["reason"].startswith("no free BUY signal or rank-fill stock (ACME is taken; GAMMA is taken")


# ── VF-20: market news in MSI is bounded by the day being scored (a re-score never reads later news) ─────────────

def test_market_news_reads_nothing_published_after_the_day(temp_db):
    from datetime import date
    from db.schema import get_connection, init_db
    from scores import engine as E
    init_db()
    c = get_connection()
    try:
        for when, score, sym in (("2026-11-03 12:00:00", 80.0, "ACME"), ("2026-11-05 12:00:00", 0.0, "ACME")):
            c.execute("INSERT INTO news_articles (fetched_at, headline, symbols_mentioned, news_score) VALUES (?, 'x', ?, ?)",
                      (when, f'["{sym}"]', score))
        c.commit()
        E.compute_msi(date(2026, 11, 3), c, {"News": 1.0})
        assert E.LAST_MSI["components"]["News"] == 80.0          # the 5 Nov article (score 0) is not averaged in
    finally:
        c.close()


# ── VF-22 / VF-32: text on a red fill reads in both themes; atip.css rules use tokens only ────────────────────────

def _css():
    from pathlib import Path
    return (Path(__file__).resolve().parents[1] / "dashboard" / "static" / "atip" / "atip.css").read_text()


def test_text_on_the_red_fill_meets_aa_in_both_themes():
    import re
    from tests.test_ux_shell import _ratio
    css = _css()
    light = dict(re.findall(r"--ax-([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})", css.split("@media (prefers-color-scheme: dark)")[0]))
    dark = dict(re.findall(r"--ax-([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})", css.split(':root[data-theme="dark"]')[1].split("}")[0]))
    for theme in (light, {**light, **dark}):
        assert _ratio(theme["neg-ink"], theme["neg"]) >= 4.5, theme["neg"]       # the badge was white on #FCA5A5: 1.89
    assert "color: var(--ax-neg-ink)" in css.split(".ax-count {")[1].split("}")[0]


def test_atip_css_rules_write_no_raw_colours():
    import re
    body = re.sub(r"/\*.*?\*/", "", _css(), flags=re.S)
    body = re.sub(r"--[\w-]+\s*:[^;]*;", "", body)                  # the token blocks
    body = re.sub(r'\[style\*="[^"]*"\]', "", body)                  # the legacy bridge's matchers (selectors, not colours)
    assert re.findall(r"#[0-9A-Fa-f]{3,8}\b|rgba?\([^)]*\)", body) == []
