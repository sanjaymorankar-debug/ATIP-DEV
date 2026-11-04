"""
QA pass 4 Nov 2026 (V2 / V5): the production scoring pipeline checked end to end against the independent
recomputation in tests/verification/recompute_scores.py, which does not import scores.engine.

The seeded market is test_w39_qa_suite's (260 sessions, NIFTY50 and three stocks with delivery and FII/DII). The
post-market run scores it; then every one of the seven scores of every stock must equal the recomputation within
0.01, and ATIP's SAFETY must be 100 - CRI, so lower crash risk raises ATIP. A mutation that flipped SAFETY to CRI
survived every earlier test (verification report V5-M2); these fail on it.
"""
import json
import sqlite3

from tests.test_w39_qa_suite import TD, _run_postmarket, market  # noqa: F401  (market is a fixture)
from tests.verification import recompute_scores as R


def test_every_score_equals_the_independent_recomputation(market, monkeypatch, tmp_path):
    out, _, _ = _run_postmarket(monkeypatch)
    assert out["status"] == "SCORED"
    from db import schema
    report = tmp_path / "recompute.json"
    rc = R.main(["--db", str(schema.DB_PATH), "--date", str(TD), "--symbols", "AAA,BBB,CCC",
                 "--industry", str(tmp_path / "no_industry_list.csv"), "--json", str(report)])
    res = json.loads(report.read_text())
    for sym, r in res["symbols"].items():
        for k, v in r["scores"].items():
            assert v["pass"], (sym, k, v)
    assert rc == 0


def test_atip_safety_is_100_minus_cri_and_lower_risk_raises_atip(market, monkeypatch):
    _run_postmarket(monkeypatch)
    from db import schema
    c = sqlite3.connect(schema.DB_PATH)
    try:
        rows = c.execute("SELECT s.symbol, s.cri, s.atip_score, k.value FROM ai_scores s JOIN score_components k "
                         "ON k.symbol = s.symbol AND k.date = s.date AND k.index_name = 'ATIP' AND k.component = 'SAFETY' "
                         "WHERE s.date = ?", (str(TD),)).fetchall()
        assert len(rows) == 3
        for sym, cri, atip, safety in rows:
            assert abs(safety - (100 - cri)) <= 0.01, (sym, cri, safety)
        # the same stock with a higher CRI must score a lower ATIP: rebuild ATIP from its stored components
        sym = rows[0][0]
        comp = dict(c.execute("SELECT component, value FROM score_components WHERE symbol=? AND date=? AND index_name='ATIP'",
                              (sym, str(TD))).fetchall())
        w = dict(c.execute("SELECT variable, weight FROM weight_config WHERE index_name='ATIP' AND active=1").fetchall())
        atip = R.combine(comp, w)
        riskier = R.combine({**comp, "SAFETY": comp["SAFETY"] - 20}, w)
        assert abs(atip - rows[0][2]) <= 0.01 and riskier < atip
    finally:
        c.close()
