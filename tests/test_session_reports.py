"""Session reports (enterprise/session_reports.py, RPT-01..07): every kind builds and renders in every format, figures
carry their env and the disclaimer, schedules fall due once per period in IST, cron works, the archive keeps files,
and a viewer never generates anything."""

import io
import json
from datetime import date, datetime

import pytest

from accounting import cycles as C
from accounting import tax as TX
from enterprise import session_reports as R
from tests.test_session_pools import _do, _entry, _pool, _tick
from tests.test_w39_qa_suite import paper  # noqa: F401  (pytest fixture)


@pytest.fixture(autouse=True)
def _configs(tmp_path, monkeypatch):
    monkeypatch.setattr(TX, "CONFIG_PATH", tmp_path / "tax-config.json")
    rc = tmp_path / "reports-config.json"
    rc.write_text(json.dumps({"reports": {"pdf_engine": "builtin"}}))
    monkeypatch.setattr(R, "CONFIG_PATH", rc)


@pytest.fixture
def traded(paper, tmp_path):
    """S1 with one closed cycle (charges on) and S2 with an open one."""
    cfg = tmp_path / "config.json"
    c = json.loads(cfg.read_text())
    c["paper_cost_model"] = "nse_delivery"
    cfg.write_text(json.dumps(c))
    _pool("S1")
    _pool("S2")
    _entry("ACME", 100.0, "S1")
    _entry("BETA", 50.0, "S2")
    _tick(paper, ACME=100.0)
    _tick(paper, ACME=103.0)
    _tick(paper, BETA=50.0)
    _do(C.sync)
    return paper


def test_indian_grouping_and_signs():
    assert R.inr(125000) == "₹1,25,000.00"
    assert R.inr(12345678.9) == "₹1,23,45,678.90"
    assert R.inr(750, sign=True) == "+₹750.00" and R.inr(-72.27, sign=True) == "−₹72.27"
    assert R.inr(999) == "₹999.00" and R.inr(None) == "—"


@pytest.mark.parametrize("kind", list(R.KINDS))
def test_every_kind_renders_in_every_format(traded, kind):
    today = date.today()
    params = {"sections": list(R.CUSTOM_SECTIONS)} if kind == "custom" else {}
    out = _do(R.generate, kind, params, list(R.FORMATS))
    files = {f["format"]: f for f in out["files"]}
    assert set(files) == set(R.FORMATS)
    for fmt, f in files.items():
        data, mime, name = _do(R.archive_file, f["id"])
        assert data and name.endswith("." + fmt) and mime == R.MIME[fmt]
        if fmt == "pdf":
            assert data.startswith(b"%PDF") and data.rstrip().endswith(b"%%EOF") and f["engine"] == "builtin"
        if fmt == "xlsx":
            from openpyxl import load_workbook
            wb = load_workbook(io.BytesIO(data))
            assert wb.sheetnames[0] == "Report" and len(wb.sheetnames) >= 2
        if fmt in ("html", "csv"):
            text = data.decode("utf-8")
            assert "Not financial advice" in text and "not tax advice" in text
        if fmt == "json":
            doc = json.loads(data)
            assert doc["kind"] == kind and doc["disclaimer"].startswith("For information only")
    assert out["period"]["to"] >= str(today)[:10] or kind in ("fy_tax_statement", "quarterly_review", "monthly_pnl",
                                                              "weekly_performance")


def test_figures_carry_env_and_gross_and_actual_stay_apart(traded):
    doc = _do(R.build, "monthly_pnl")
    st = doc["sections"][0]
    labels = [c["label"] for c in st["columns"]]
    assert {"Gross P&L", "Charges", "Tax (est.)", "Actual P&L", "Env"} <= set(labels)
    row = st["rows"][0]
    c = _do(C.stored, "S1")[0]
    assert (row["gross_pnl"], row["charges"], row["tax_est"], row["actual_pnl"], row["env"]) == \
        (c["gross_pnl"], c["charges"], c["tax_est"], c["actual_pnl"], "VIRTUAL")
    assert st["totals"][0]["session"] == "Total VIRTUAL"
    html = R.to_html(doc)
    assert "+₹" in html and "Waterfall" in html


def test_daily_summary_shows_floor_cash_and_the_open_trade(traded):
    doc = _do(R.build, "daily_session_summary")
    rows = {r["session"].split()[0]: r for r in doc["sections"][0]["rows"]}
    assert rows["S1"]["closed"] == 1 and rows["S1"]["floor"] == 12000.0
    assert rows["S2"]["open_trade"].startswith("BETA ×") and rows["S2"]["opened"] == 1


def test_fy_statement_report_matches_the_tax_module(traded):
    doc = _do(R.build, "fy_tax_statement", {"fy": TX.fy_of(date.today())})
    st = TX.fy_statement(_do(C.stored), TX.fy_of(date.today()))
    kv = dict(doc["sections"][1]["kv"])
    assert kv["Estimated liability"] == R.inr(st["liability"])
    assert doc["sections"][2]["rows"][0]["symbol"] == "ACME"


def test_periods_for_scheduled_runs_cover_the_last_complete_period():
    mon = date(2026, 11, 2)
    assert R.period("weekly_performance", on=mon, scheduled=True) == \
        {"from": "2026-10-26", "to": "2026-11-01", "label": "Week 26 Oct – 01 Nov 2026"}
    assert R.period("monthly_pnl", on=date(2026, 11, 1), scheduled=True)["from"] == "2026-10-01"
    assert R.period("monthly_pnl", on=date(2026, 11, 1), scheduled=True)["to"] == "2026-10-31"
    assert R.period("quarterly_review", on=date(2026, 10, 1), scheduled=True)["label"] == "FY 2026-27 Q2"
    assert R.period("quarterly_review", {"quarter": "FY 2026-27 Q4"})["from"] == "2027-01-01"
    assert R.period("fy_tax_statement", on=date(2027, 4, 1), scheduled=True)["label"] == "FY 2026-27"


def test_cron():
    assert R.cron_matches("30 17 * * 1-5", datetime(2026, 11, 2, 17, 30))         # a Monday
    assert not R.cron_matches("30 17 * * 1-5", datetime(2026, 11, 1, 17, 30))     # a Sunday
    assert R.cron_matches("*/15 9-15 * * *", datetime(2026, 11, 2, 9, 45))
    with pytest.raises(ValueError, match="five fields"):
        R.cron_matches("30 17 *", datetime(2026, 11, 2))


def test_schedules_run_once_per_period_and_catch_up(traded):
    rows = _do(R.schedules)
    assert {r["kind"] for r in rows} == {"daily_session_summary", "weekly_performance", "monthly_pnl",
                                         "quarterly_review", "fy_tax_statement"}
    assert next(r for r in rows if r["kind"] == "weekly_performance")["when"] == "Every Monday at 08:00 IST"
    mon = datetime(2026, 11, 2, 7, 59)
    assert _do(R.run_due, mon)["ran"] == []                                        # not 08:00 yet
    ran = _do(R.run_due, datetime(2026, 11, 2, 18, 5))["ran"]                      # the Mac woke at 18:05
    assert sorted(r["kind"] for r in ran) == ["daily_session_summary", "weekly_performance"]
    assert all(r["files"] == 4 for r in ran)
    assert _do(R.run_due, datetime(2026, 11, 2, 18, 10))["ran"] == []              # once per period
    assert len(_do(R.archive)) == 8


def test_schedule_validation_is_plain_english(temp_db):
    with pytest.raises(ValueError, match="HH:MM"):
        _do(R.save_schedule, {"kind": "monthly_pnl", "freq": "monthly", "time_ist": "25:00"})
    with pytest.raises(ValueError, match="1 to 28"):
        _do(R.save_schedule, {"kind": "monthly_pnl", "freq": "monthly", "day": 31})
    s = _do(R.save_schedule, {"kind": "custom", "freq": "cron", "cron": "0 18 * * 5", "formats": ["pdf"],
                              "params": {"sections": ["pnl"]}})
    assert s["when"] == "Cron “0 18 * * 5” (IST)" and s["formats"] == ["pdf"]


def test_a_viewer_never_generates(temp_db, monkeypatch):
    monkeypatch.setenv("ATIP_ROLE", "viewer")
    import ops.role as role
    monkeypatch.setattr(role, "is_viewer", lambda: True)
    assert "skipped" in _do(R.run_due, datetime(2026, 11, 2, 18, 0))
