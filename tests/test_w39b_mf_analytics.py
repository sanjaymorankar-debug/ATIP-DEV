"""
W39b: mutual fund analytics on the stored AMFI NAVs (data/mf_analytics.py) and its read-only routes.
Seeded NAVs with hand-computed answers: CAGR on a constant-growth fund, rolling windows on a
calendar-daily series, drawdown dates, SIP units and XIRR against a direct cash-flow XIRR, holidays
and gaps, too-short histories, category rank coverage, routes and permissions. Nothing reaches AMFI.
"""
import math
import statistics
from datetime import date, timedelta

import pytest

LARGE = "Open Ended Schemes(Equity Scheme - Large Cap Fund)"
MID = "Open Ended Schemes(Equity Scheme - Mid Cap Fund)"
END = date(2026, 1, 30)                     # a Friday


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    """No config.json on the test machine decides a number."""
    from data import mf_analytics as MA
    from wealth import config as WC
    monkeypatch.setattr(MA, "settings", lambda: dict(MA.DEFAULTS))
    monkeypatch.setattr(WC, "settings", lambda: dict(WC.DEFAULTS))


@pytest.fixture
def db(temp_db):
    from db.schema import get_connection, init_db
    init_db()
    conn = get_connection()
    yield conn
    conn.close()


def _put(conn, code, rows, name, category=LARGE, amc="Test Mutual Fund"):
    conn.executemany("INSERT OR REPLACE INTO mf_nav (scheme_code,date,nav,scheme_name,amc,category) "
                     "VALUES (?,?,?,?,?,?)", [(code, str(d), v, name, amc, category) for d, v in rows])
    conn.commit()


def _weekdays(start, end, skip=()):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5 and d not in skip:
            out.append(d)
        d += timedelta(days=1)
    return out


def _growth(dates, rate, base=10.0, amp=0.0):
    d0 = dates[0]
    return [(d, base * (1 + rate) ** ((d - d0).days / 365) * (1 + amp * (-1) ** i)) for i, d in enumerate(dates)]


def _before(rows, target):
    """The last seeded (date, nav) on or before target -- the look-back rule, written out again."""
    return [r for r in rows if r[0] <= target][-1]


# ── calendar helpers ──────────────────────────────────────────────────────

def test_month_arithmetic_clamps_to_month_end():
    from data.mf_analytics import add_months
    assert add_months(date(2026, 3, 31), -1) == date(2026, 2, 28)
    assert add_months(date(2024, 3, 31), -1) == date(2024, 2, 29)
    assert add_months(date(2024, 2, 29), -12) == date(2023, 2, 28)
    assert add_months(date(2026, 1, 15), -36) == date(2023, 1, 15)
    assert add_months(date(2025, 12, 10), 1) == date(2026, 1, 10)


def test_plan_option_and_category_parsing():
    from data.mf_analytics import parse_category, plan_option
    assert plan_option("HDFC Dividend Yield Fund - Growth Option - Direct Plan") == ("Direct", "Growth")
    assert plan_option("Axis Growth Opportunities Fund - Regular Plan - IDCW") == ("Regular", "IDCW")
    assert plan_option("Some Fund - Direct Plan - Bonus Option") == ("Direct", "Bonus")
    assert plan_option("Old Scheme") == (None, None)
    c = parse_category(LARGE)
    assert c == {"structure": "Open Ended Schemes", "label": "Equity Scheme - Large Cap Fund",
                 "asset_class": "Equity Scheme", "sub_category": "Large Cap Fund"}
    assert parse_category(None) is None


# ── point to point, CAGR ──────────────────────────────────────────────────

def test_point_to_point_and_cagr_on_a_constant_12pct_fund(db):
    from data import mf_analytics as MA
    rows = _growth(_weekdays(date(2019, 1, 1), END), 0.12)
    _put(db, "100001", rows, "Alpha Large Cap Fund - Direct Plan - Growth")
    a = MA.analytics(db, "100001", rf_pct=6.5, peers=False)
    R = a["returns"]
    # NAV = 10 x 1.12 ** (days / 365): every CAGR (Actual/365) is exactly 12 %
    assert R["3Y"]["cagr_pct"] == pytest.approx(12.0, abs=1e-3) and R["3Y"]["annualised"]
    assert R["5Y"]["cagr_pct"] == pytest.approx(12.0, abs=1e-3)
    assert a["since_first_nav"]["cagr_pct"] == pytest.approx(12.0, abs=1e-3)
    # 1M: 30 Jan 2026 -> 30 Dec 2025 (a Tuesday, has a NAV): 31 days, absolute, not annualised
    m1 = R["1M"]
    assert m1["start_date"] == date(2025, 12, 30) and m1["days"] == 31
    assert m1["absolute_pct"] == pytest.approx((1.12 ** (31 / 365) - 1) * 100, abs=1e-3)
    assert m1["cagr_pct"] is None and not m1["annualised"]
    # 1Y: absolute only (the rule: up to a year absolute)
    assert R["1Y"]["absolute_pct"] == pytest.approx(12.0, abs=1e-3) and R["1Y"]["cagr_pct"] is None
    # 5Y: the target 30 Jan 2021 is a Saturday -> the Friday before, 1827 days
    m5 = R["5Y"]
    assert m5["start_target"] == date(2021, 1, 30) and m5["start_date"] == date(2021, 1, 29)
    assert m5["days"] == 1827
    assert m5["absolute_pct"] == pytest.approx((1.12 ** (1827 / 365) - 1) * 100, abs=1e-3)
    assert a["history"]["navs"] == len(rows) and a["history"]["gap_count"] == 0
    assert a["scheme"]["plan"] == "Direct" and a["scheme"]["option"] == "Growth"
    assert "Not SEBI-registered investment advice" in a["note"]


# ── rolling returns ───────────────────────────────────────────────────────

def _rolling_seed(db):
    """Every calendar day 1 Mar 2022 .. 10 Mar 2023 (no 29 Feb inside, so every 1Y window is 365 days).
    NAV 100 throughout the first year; the ten window ends 1..10 Mar 2023 carry chosen NAVs."""
    ends = [95, 100, 103, 105, 107.5, 110, 112, 115, 120, 130]
    rows, d = [], date(2022, 3, 1)
    while d <= date(2023, 3, 10):
        rows.append((d, float(ends[(d - date(2023, 3, 1)).days]) if d >= date(2023, 3, 1) else 100.0))
        d += timedelta(days=1)
    _put(db, "100002", rows, "Roll Liquid Fund - Direct Plan - Growth", category=None)
    return ends


def test_rolling_windows_min_median_max_and_hurdle(db):
    from data import mf_analytics as MA
    _rolling_seed(db)
    a = MA.analytics(db, "100002", hurdle_pct=7, rf_pct=6.5, peers=False)
    r = a["rolling"]["1Y"]
    st = r["stats"]
    # windows end 1..10 Mar 2023; returns -5, 0, 3, 5, 7.5, 10, 12, 15, 20, 30 %
    assert r["windows"] == 10 and r["skipped_for_gaps"] == 0
    assert st["min_pct"] == pytest.approx(-5.0) and st["min_end_date"] == date(2023, 3, 1)
    assert st["max_pct"] == pytest.approx(30.0) and st["max_end_date"] == date(2023, 3, 10)
    assert st["median_pct"] == pytest.approx(8.75)              # (7.5 + 10) / 2
    assert st["mean_pct"] == pytest.approx(9.75)
    assert st["pct_positive"] == 80.0                           # 0 % is not above 0
    assert st["pct_above_hurdle"] == 60.0                       # 7.5, 10, 12, 15, 20, 30 > 7
    assert st["latest_pct"] == pytest.approx(30.0)
    assert [p["return_pct"] for p in r["series"]] == pytest.approx([-5, 0, 3, 5, 7.5, 10, 12, 15, 20, 30])
    assert MA.analytics(db, "100002", hurdle_pct=10, peers=False)["rolling"]["1Y"]["stats"]["pct_above_hurdle"] == 40.0
    # 3Y: a year of history has no 3Y window -- None with the reason, never a guess
    r3 = a["rolling"]["3Y"]
    assert r3["stats"] is None and r3["windows"] == 0 and "no complete 3Y window" in r3["reason"]
    # one window short of MIN_WINDOWS (as of 9 Mar): withheld
    r9 = MA.analytics(db, "100002", as_of="2023-03-09", peers=False)["rolling"]["1Y"]
    assert r9["windows"] == 9 and r9["stats"] is None and "at least 10" in r9["reason"]
    assert "category" not in a


# ── drawdown ──────────────────────────────────────────────────────────────

def test_drawdown_peak_trough_and_recovery_dates():
    from data.mf_analytics import drawdown
    ds = [date(2025, 1, 1) + timedelta(days=i) for i in range(9)]
    d = drawdown(ds, [10, 11, 12, 11, 9, 10, 12, 12.5, 11])
    assert d["max_drawdown_pct"] == pytest.approx(-25.0)        # 9 / 12 - 1
    assert (d["peak_date"], d["trough_date"], d["recovery_date"]) == (ds[2], ds[4], ds[6])
    assert d["recovered"] and d["days_peak_to_trough"] == 2 and d["days_trough_to_recovery"] == 2
    assert d["current_drawdown_pct"] == pytest.approx((11 / 12.5 - 1) * 100, abs=1e-3)
    n = drawdown(ds[:4], [10, 12, 8, 9])
    assert n["max_drawdown_pct"] == pytest.approx(-100 / 3, abs=1e-3) and n["recovery_date"] is None
    assert not n["recovered"] and n["days_trough_to_recovery"] is None
    assert drawdown(ds[:3], [1, 2, 3])["max_drawdown_pct"] == 0.0
    assert drawdown(ds[:1], [1])["max_drawdown_pct"] is None


def test_drawdown_dates_through_the_analytics(db):
    from data import mf_analytics as MA
    ds = _weekdays(date(2025, 1, 1), date(2025, 12, 31))[:170]
    navs = [10 + 0.05 * i for i in range(80)]                       # peak at index 79: 13.95
    navs += [13.95 * (1 - 0.2 * (k + 1) / 30) for k in range(30)]   # trough at 109: 11.16 (-20 %)
    navs += [11.16 + 0.1 * (k + 1) for k in range(60)]              # back to >= 13.95 at 137
    _put(db, "100003", list(zip(ds, navs)), "Dip Fund - Direct Plan - Growth")
    k = MA.analytics(db, "100003", rf_pct=6.5, peers=False)["risk"]
    for dd in (k["max_drawdown"], k["max_drawdown_full_history"]):
        assert dd["max_drawdown_pct"] == pytest.approx(-20.0, abs=1e-3)
        assert (dd["peak_date"], dd["trough_date"], dd["recovery_date"]) == (ds[79], ds[109], ds[137])
    assert k["window"]["note"] and "shorter than 3Y" in k["window"]["note"]


# ── risk and the benchmark ────────────────────────────────────────────────

def test_volatility_sharpe_sortino_and_beta_alpha_te_on_matched_dates(db):
    np = pytest.importorskip("numpy")
    from data import mf_analytics as MA
    ds = _weekdays(date(2024, 1, 1), date(2025, 6, 30))[:300]
    b = list(np.random.default_rng(3).normal(0.0004, 0.01, len(ds) - 1))
    a_daily = 0.0002
    r = [a_daily + 0.5 * x for x in b]                       # fund = 0.02 % a day + half the index
    nav, close = [10.0], [1000.0]
    for x, y in zip(r, b):
        nav.append(nav[-1] * (1 + x))
        close.append(close[-1] * (1 + y))
    _put(db, "100004", list(zip(ds, nav)), "Half Beta Fund - Direct Plan - Growth")
    missing = {20, 21, 75, 130, 131, 132, 200, 250, 251, 280}  # index holidays the fund does not have
    db.executemany("INSERT INTO prices_daily (symbol, date, close) VALUES ('NIFTY50', ?, ?)",
                   [(str(d), c) for i, (d, c) in enumerate(zip(ds, close)) if i not in missing])
    db.commit()
    k = MA.analytics(db, "100004", rf_pct=6.5, peers=False)["risk"]
    # NAVs on every weekday for 60 weeks (Mon 1 Jan 2024 .. Fri 22 Mar 2025): 299 returns over 417 days, so
    # the window's NAV observations a year are 299 x 365 / 417 = 261.715..., not 252
    assert (ds[-1] - ds[0]).days == 417
    P = 299 * 365 / 417
    assert k["periods_per_year"] == pytest.approx(P, abs=1e-4) and round(P, 2) == 261.71
    rf_d = 1.065 ** (1 / P) - 1
    ex = [x - rf_d for x in r]
    assert k["window"]["returns"] == 299 and k["window"]["excluded_gap_returns"] == 0
    assert k["volatility_pct"] == pytest.approx(statistics.stdev(r) * math.sqrt(P) * 100, abs=1e-3)
    assert k["sharpe"] == pytest.approx(statistics.mean(ex) / statistics.stdev(r) * math.sqrt(P), abs=1e-3)
    dd = math.sqrt(sum(min(0.0, e) ** 2 for e in ex) / len(ex))
    assert k["sortino"] == pytest.approx(statistics.mean(ex) / dd * math.sqrt(P), abs=1e-3)
    B = k["benchmark"]
    matched = [i for i in range(1, len(ds)) if i not in missing and i - 1 not in missing]
    assert B["status"] == "OK" and B["symbol"] == "NIFTY50" and B["matched_returns"] == len(matched)
    assert B["beta"] == pytest.approx(0.5, abs=1e-3)
    # Jensen: (a + b/2 - rf) - 0.5 (b - rf) = a - rf / 2 a NAV interval
    assert B["alpha_annual_pct"] == pytest.approx((a_daily - 0.5 * rf_d) * P * 100, abs=2e-3)
    te = statistics.stdev([0.5 * b[i - 1] for i in matched]) * math.sqrt(P)     # r - b = a - b / 2
    assert B["tracking_error_pct"] == pytest.approx(te * 100, abs=1e-3)
    # another index ATIP does not store: said so
    nb = MA.analytics(db, "100004", benchmark="NIFTYBANK", peers=False)["risk"]["benchmark"]
    assert nb["status"] == "UNAVAILABLE" and "NIFTYBANK" in nb["reason"]


def test_risk_free_rate_comes_from_the_wealth_config(db, monkeypatch):
    from data import mf_analytics as MA
    from wealth import config as WC
    _put(db, "100005", _growth(_weekdays(date(2025, 1, 1), END), 0.10, amp=0.002), "RF Fund - Direct Plan - Growth")
    monkeypatch.setattr(WC, "settings", lambda: {**WC.DEFAULTS, "risk_free_pct": 5.0})
    a = MA.analytics(db, "100005", peers=False)
    assert a["risk"]["risk_free_pct"] == 5.0 and a["conventions"]["risk_free_pct"] == 5.0
    assert MA.analytics(db, "100005", rf_pct=7.25, peers=False)["risk"]["risk_free_pct"] == 7.25


# ── gaps, holidays, short histories ───────────────────────────────────────

def test_a_gap_in_the_stored_navs_gives_none_not_a_guess(db):
    from data import mf_analytics as MA
    dec = {date(2025, 12, 15) + timedelta(days=i) for i in range(21)}          # 15 Dec .. 4 Jan missing
    jun = {date(2024, 6, 10) + timedelta(days=i) for i in range(21)}           # 10 Jun .. 30 Jun 2024 missing
    rows = _growth(_weekdays(date(2023, 1, 2), END, skip=dec | jun), 0.10, amp=0.001)
    _put(db, "100006", rows, "Gappy Fund - Direct Plan - Growth")
    a = MA.analytics(db, "100006", rf_pct=6.5, peers=False)
    m1 = a["returns"]["1M"]                     # target 30 Dec 2025 falls in the hole; last NAV 12 Dec
    assert m1["absolute_pct"] is None and "no NAV within 7 days on or before 2025-12-30" in m1["reason"]
    assert a["returns"]["3M"]["absolute_pct"] is not None
    assert a["history"]["gap_count"] == 2 and [g["days"] for g in a["history"]["gaps"]] == [24, 24]
    assert a["risk"]["window"]["excluded_gap_returns"] == 2
    assert any("gaps of more than 7 days" in w for w in a["warnings"])
    # 1Y windows ending 16..30 Jun 2025 start 16..30 Jun 2024: more than 7 days after the last NAV (7 Jun)
    r = a["rolling"]["1Y"]
    assert r["skipped_for_gaps"] == len([d for d, _ in rows if date(2025, 6, 15) <= d <= date(2025, 6, 30)]) == 11
    assert r["stats"]["min_pct"] is not None


def test_too_short_history_and_unusable_rows(db):
    from data import mf_analytics as MA
    ds = _weekdays(date(2025, 11, 3), END)[:40]
    _put(db, "100007", _growth(ds, 0.10), "New Fund - Direct Plan - Growth")
    db.execute("INSERT INTO mf_nav (scheme_code, date, nav, scheme_name) VALUES ('100007', ?, NULL, 'New Fund')",
               (str(ds[-1] + timedelta(days=3)),))
    db.execute("INSERT INTO mf_nav (scheme_code, date, nav, scheme_name) VALUES ('100007', ?, 0, 'New Fund')",
               (str(ds[-1] + timedelta(days=4)),))
    db.commit()
    a = MA.analytics(db, "100007", rf_pct=6.5, peers=False)
    assert a["history"]["unusable_rows"] == 2 and a["history"]["navs"] == 40 and a["as_of"] == ds[-1]
    assert a["returns"]["1M"]["absolute_pct"] is not None
    for p in ("3M", "6M", "1Y", "3Y", "5Y"):
        assert a["returns"][p]["absolute_pct"] is None and "history starts" in a["returns"][p]["reason"]
    assert a["since_first_nav"]["cagr_pct"] is None and "a year or more" in a["since_first_nav"]["reason"]
    assert a["rolling"]["1Y"]["stats"] is None and a["rolling"]["1Y"]["reason"]
    assert a["risk"]["volatility_pct"] is None and "39 daily returns" in a["risk"]["reason"]
    assert a["risk"]["sharpe"] is None and a["risk"]["benchmark"]["status"] == "INSUFFICIENT"
    s = MA.sip(db, "100007", 1000, 5, "2025-01-01", str(ds[-1]))
    assert s["status"] == "INSUFFICIENT" and "starts 2025-11-03" in s["reason"]
    with pytest.raises(LookupError):
        MA.analytics(db, "999999")
    with pytest.raises(ValueError):
        MA.analytics(db, "abc")


# ── SIP and lump sum ──────────────────────────────────────────────────────

def _xirr_newton(flows):
    t0 = flows[0][0]
    r = 0.1
    for _ in range(100):
        f = sum(a / (1 + r) ** ((d - t0).days / 365) for d, a in flows)
        fp = sum(-((d - t0).days / 365) * a / (1 + r) ** ((d - t0).days / 365 + 1) for d, a in flows)
        step = f / fp
        r -= step
        if abs(step) < 1e-12:
            break
    return r


def test_sip_units_value_and_xirr_match_a_direct_cash_flow_xirr(db):
    from data import mf_analytics as MA
    holidays = {date(2024, 5, 10), date(2023, 8, 15)}
    ds = _weekdays(date(2023, 1, 2), date(2025, 6, 30), skip=holidays)
    rows = [(d, 10 * (1 + 0.1 * math.sin(i / 15)) * 1.10 ** ((d - ds[0]).days / 365)) for i, d in enumerate(ds)]
    _put(db, "100008", rows, "Wavy Fund - Direct Plan - Growth")
    nav = dict(rows)
    # by hand: the 10th of each month, or the next NAV date; with the defaults each instalment buys
    # floor(5000 x (1 - 0.00005) / NAV, 3 decimals) units (stamp duty, RTA rounding); without them 5000 / NAV
    exp, units, raw = [], 0.0, 0.0
    y, m = 2023, 1
    while (y, m) <= (2025, 6):
        sched = date(y, m, 10)
        alloc = min(d for d in ds if d >= sched)
        exp.append((sched, alloc))
        units += math.floor(4999.75 / nav[alloc] * 1000) / 1000
        raw += 5000 / nav[alloc]
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    val_d = date(2025, 6, 30)
    for opts, u in (({}, units), ({"stamp_duty": False, "round_units": False}, raw)):
        s = MA.sip(db, "100008", 5000, 10, "2023-01-01", "2025-06-30", **opts)
        assert s["status"] == "OK"
        assert [(x["scheduled"], x["nav_date"]) for x in s["schedule"]] == exp
        may = next(x for x in s["schedule"] if x["scheduled"] == date(2024, 5, 10))
        assert may["nav_date"] == date(2024, 5, 13) and may["days_late"] == 3    # Friday holiday -> Monday
        value = u * nav[val_d]
        S = s["sip"]
        assert S["instalments"] == 30 and S["invested"] == 150000.0
        assert S["units"] == pytest.approx(u, abs=1e-5) and S["value"] == pytest.approx(value, abs=0.01)
        assert S["stamp_duty"] == (7.5 if not opts else 0.0)                       # 30 x 5000 x 0.005 %
        flows = [(alloc, -5000.0) for _, alloc in exp] + [(val_d, value)]
        assert S["xirr_pct"] == pytest.approx(_xirr_newton(flows) * 100, abs=2e-3)
        # the lump sum: the same 1,50,000 at the first NAV on or after the start (2 Jan 2023)
        L = s["lump_sum"]
        n0 = nav[date(2023, 1, 2)]
        lu = math.floor(149992.5 / n0 * 1000) / 1000 if not opts else 150000 / n0
        assert L["date"] == date(2023, 1, 2) and L["units"] == pytest.approx(lu, abs=1e-5)
        assert L["value"] == pytest.approx(lu * nav[val_d], abs=0.01)
        lx = (lu * nav[val_d] / 150000) ** (365 / (val_d - date(2023, 1, 2)).days) - 1
        assert L["xirr_pct"] == pytest.approx(lx * 100, abs=2e-3) and L["cagr_pct"] == pytest.approx(lx * 100, abs=2e-3)
        assert s["valuation"] == {"date": val_d, "nav": nav[val_d]}
    assert units < raw and S["xirr_pct"] > _xirr_newton(flows[:-1] + [(val_d, units * nav[val_d])]) * 100
    # a lump sum of its own size
    assert MA.sip(db, "100008", 5000, 10, "2023-01-01", "2025-06-30", lump_sum=1000)["lump_sum"]["invested"] == 1000


def test_sip_xirr_on_a_constant_growth_fund_is_that_growth(db):
    """Every rupee on a NAV growing 12 % a year (Actual/365) earns exactly 12 %: SIP and lump-sum XIRR = 12
    (without the stamp duty and unit rounding, which cost a little: on by default)."""
    from data import mf_analytics as MA
    _put(db, "100009", _growth(_weekdays(date(2021, 1, 1), END), 0.12), "Steady Fund - Direct Plan - Growth")
    s = MA.sip(db, "100009", 2500, 31, "2022-01-01", "2025-12-31", stamp_duty=False, round_units=False)
    assert s["sip"]["xirr_pct"] == pytest.approx(12.0, abs=1e-3)
    assert s["lump_sum"]["xirr_pct"] == pytest.approx(12.0, abs=1e-3)
    # the step-up changes how much goes in, not what each rupee earns
    up = MA.sip(db, "100009", 2500, 31, "2022-01-01", "2025-12-31", step_up_pct=10, stamp_duty="0", round_units="0")
    assert up["sip"]["xirr_pct"] == pytest.approx(12.0, abs=1e-3) and up["sip"]["last_instalment"] == 3327.5
    d = MA.sip(db, "100009", 2500, 31, "2022-01-01", "2025-12-31")["sip"]["xirr_pct"]
    assert 11.99 < d < 12.0, "0.005 % stamp duty and units rounded down cost a little"
    # day 31 is clamped to each month's end (29 Feb 2024 a Thursday; 30 Nov 2024 a Saturday -> 2 Dec)
    sch = {x["scheduled"]: x["nav_date"] for x in s["schedule"]}
    assert sch[date(2024, 2, 29)] == date(2024, 2, 29) and sch[date(2024, 11, 30)] == date(2024, 12, 2)
    assert s["sip"]["instalments"] == 48


def test_sip_input_validation_and_defaults(db):
    from data import mf_analytics as MA
    _put(db, "100010", _growth(_weekdays(date(2024, 6, 3), END), 0.10), "Short Fund - Direct Plan - Growth")
    for bad in ({"amount": 0}, {"amount": "lots"}, {"amount": None}, {"amount": 100, "day": 0},
                {"amount": 100, "day": 32}, {"amount": 100, "day": 2.5},
                {"amount": 100, "start": "2025-06-01", "end": "2025-01-01"}, {"amount": 100, "start": "June"}):
        with pytest.raises(ValueError):
            MA.sip(db, "100010", **bad)
    s = MA.sip(db, "100010", 1000)               # no start: the last 3 years, or all of the stored history
    assert s["status"] == "OK" and s["inputs"]["start_defaulted"] and s["inputs"]["start"] == date(2024, 6, 3)
    # an end after the last NAV: valued at the last stored NAV, said so
    late = MA.sip(db, "100010", 1000, 1, "2025-01-01", "2026-03-31")
    assert late["valuation"]["date"] == END and any("valued at the last stored NAV" in w for w in late["warnings"])
    assert all(x["scheduled"] <= END for x in late["schedule"]) and late["skipped"]
    short = MA.sip(db, "100010", 1000, 5, "2025-10-01", "2026-01-30")
    assert any("under a year" in w for w in short["warnings"])


# ── category and compare ──────────────────────────────────────────────────

def _category_seed(db):
    ds = _weekdays(date(2022, 1, 3), END)
    young = [d for d in ds if d >= date(2025, 8, 1)]                  # six months: no 1Y figure
    stopped = [d for d in ds if d <= date(2025, 12, 31)]              # no NAV in the last 30 days
    seed = {
        "200001": ("Alpha Large Cap Fund - Direct Plan - Growth", LARGE, 0.12, 0.004, ds),
        "200002": ("Beta Large Cap Fund - Direct Plan - Growth", LARGE, 0.15, 0.008, ds),
        "200003": ("Gamma Large Cap Fund - Direct Plan - Growth", LARGE, 0.08, 0.002, ds),
        "200004": ("Delta Large Cap Fund - Regular Plan - Growth", LARGE, 0.20, 0.003, ds),
        "200005": ("Epsilon Large Cap Fund - Direct Plan - IDCW", LARGE, 0.10, 0.003, ds),
        "200006": ("Zeta Mid Cap Fund - Direct Plan - Growth", MID, 0.30, 0.003, ds),
        "200007": ("Eta Large Cap Fund - Direct Plan - Growth", LARGE, 0.10, 0.003, young),
        "200008": ("Theta Large Cap Fund - Direct Plan - Growth", LARGE, 0.10, 0.003, stopped),
    }
    out = {}
    for code, (name, cat, g, amp, dts) in seed.items():
        rows = _growth(dts, g, amp=amp)
        _put(db, code, rows, name, category=cat)
        out[code] = rows
    return out


def test_category_rank_with_stated_coverage(db):
    from data import mf_analytics as MA
    rows = _category_seed(db)
    c = MA.category_rank(db, "200001")
    cv = c["coverage"]
    # in the category with a NAV near 30 Jan 2026: A B C D E Eta (Zeta is Mid Cap; Theta stopped 31 Dec)
    assert c["status"] == "OK" and cv["in_category_stored"] == 6
    assert {p["scheme_code"] for p in c["peers"]} == {"200001", "200002", "200003", "200007"}   # Direct Growth
    assert (cv["compared"], cv["with_1y"], cv["with_3y"], cv["with_volatility"]) == (4, 3, 3, 3)
    assert "Only schemes whose NAVs ATIP stores" in cv["note"]
    rk = c["rank"]
    assert (rk["return_1y"]["rank"], rk["return_1y"]["of"]) == (2, 3)
    assert (rk["cagr_3y"]["rank"], rk["cagr_3y"]["of"]) == (2, 3)
    assert (rk["volatility_1y"]["rank"], rk["volatility_1y"]["of"]) == (2, 3)     # Gamma calmer, Beta wilder
    a = next(p for p in c["peers"] if p["scheme_code"] == "200001")
    end = rows["200001"][-1]
    exp1 = end[1] / _before(rows["200001"], date(2025, 1, 30))[1] - 1
    assert a["return_1y_pct"] == pytest.approx(exp1 * 100, abs=1e-3) and a["is_target"]
    eta = next(p for p in c["peers"] if p["scheme_code"] == "200007")
    assert eta["return_1y_pct"] is None and "history starts" in eta["return_1y_reason"]
    assert rk["return_1y"]["category_median"] == pytest.approx(a["return_1y_pct"], abs=1e-3)
    # every plan and option: Delta (Regular, 20 %) leads, IDCW Epsilon joins
    wide = MA.category_rank(db, "200001", same_plan=False)
    assert wide["coverage"]["compared"] == 6 and wide["rank"]["return_1y"] ["rank"] == 3
    assert wide["rank"]["return_1y"]["of"] == 5
    # the scheme detail carries the same block
    assert MA.analytics(db, "200001", rf_pct=6.5)["category"]["rank"]["return_1y"]["rank"] == 2
    # a scheme backfilled by history_mf only has no category: said so
    db.execute("INSERT INTO mf_nav (scheme_code, date, nav, scheme_name) VALUES ('200009', ?, 10, 'Bare Fund')",
               (str(END),))
    db.commit()
    assert MA.category_rank(db, "200009")["status"] == "UNAVAILABLE"
    assert MA.analytics(db, "200009")["category"]["status"] == "UNAVAILABLE"


def test_compare_side_by_side_on_a_common_date(db):
    from data import mf_analytics as MA
    _category_seed(db)
    c = MA.compare(db, ["200001", "200002", "200008", "200001"])     # duplicates dropped
    assert [s["scheme_code"] for s in c["schemes"]] == ["200001", "200002", "200008"]
    assert c["as_of"] == date(2025, 12, 31)                           # Theta's last NAV: the common date
    assert all(s["nav_date"] == date(2025, 12, 31) for s in c["schemes"])
    assert c["rank"]["return_1y"] == {"200002": 1, "200001": 2, "200008": 3}
    assert c["rank"]["volatility_1y"]["200002"] == 3
    b = next(s for s in c["schemes"] if s["scheme_code"] == "200002")
    assert b["returns"]["3Y"]["cagr_pct"] is not None and b["volatility_pct"] is not None
    for bad in (["200001"], [str(300000 + i) for i in range(11)], ["200001", "x"]):
        with pytest.raises(ValueError):
            MA.compare(db, bad)


# ── routes and permissions ────────────────────────────────────────────────

@pytest.fixture
def api(tmp_path, monkeypatch, db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    return TestClient(server.app), security


def test_mf_routes(api, db):
    client, _ = api
    _category_seed(db)
    r = client.get("/api/data/mf/200001/analytics", params={"hurdle": "8", "rf": "6.5"})
    assert r.status_code == 200
    j = r.json()
    assert j["returns"]["3Y"]["cagr_pct"] is not None and j["rolling"]["1Y"]["hurdle_pct"] == 8
    assert j["category"]["status"] == "OK" and j["as_of"] == str(END)
    assert "category" not in client.get("/api/data/mf/200001/analytics", params={"peers": "0"}).json()
    assert client.get("/api/data/mf/999999/analytics").status_code == 404
    assert client.get("/api/data/mf/abc/analytics").status_code == 400
    assert client.get("/api/data/mf/200001/analytics", params={"hurdle": "lots"}).status_code == 400
    assert client.get("/api/data/mf/200001/analytics", params={"risk_years": "40"}).status_code == 400
    s = client.get("/api/data/mf/200001/sip", params={"amount": "5000", "day": "10", "start": "2023-01-01",
                                                      "end": "2025-12-31"})
    assert s.status_code == 200 and s.json()["sip"]["instalments"] == 36
    assert client.get("/api/data/mf/200001/sip").status_code == 400                       # no amount
    assert client.get("/api/data/mf/200001/sip", params={"amount": "100", "day": "0"}).status_code == 400
    assert client.get("/api/data/mf/200001/sip", params={"amount": "100", "start": "2025-02-01",
                                                         "end": "2025-01-01"}).status_code == 400
    c = client.get("/api/data/mf/compare", params={"schemes": "200001,200002"})
    assert c.status_code == 200 and len(c.json()["schemes"]) == 2
    k = client.get("/api/data/mf/compare", params={"category_of": "200001", "same_plan": "0"})
    assert k.status_code == 200 and k.json()["coverage"]["compared"] == 6
    assert client.get("/api/data/mf/compare").status_code == 400
    assert client.get("/api/data/mf/compare", params={"schemes": "200001,200002",
                                                      "category_of": "200001"}).status_code == 400
    assert client.get("/api/data/mf", params={"q": "Alpha"}).json()[0]["scheme_code"] == "200001"
    for u in ("/api/data/mf/200001/analytics", "/api/data/mf/200001/sip", "/api/data/mf/compare"):
        assert client.post(u).status_code == 405                                         # read-only
    page = client.get("/data-platform")
    assert page.status_code == 200 and "mfOpen" in page.text and "SIP calculator" in page.text


def test_mf_routes_are_read_only_dashboard_reads():
    pytest.importorskip("fastapi")
    from dashboard import server
    from enterprise.authz import permission_for
    mf = [(m, r.path) for r in server.app.routes if getattr(r, "path", "").startswith("/api/data/mf")
          for m in (getattr(r, "methods", None) or ())]
    assert {p for _, p in mf} >= {"/api/data/mf", "/api/data/mf/compare", "/api/data/mf/{scheme}/analytics",
                                  "/api/data/mf/{scheme}/sip"}
    assert {m for m, _ in mf} <= {"GET", "HEAD"}
    for p in ("/api/data/mf/120503/analytics", "/api/data/mf/120503/sip", "/api/data/mf/compare"):
        assert permission_for("GET", p) == "dashboard:read"
    # the wealth ledger still refuses mutual funds (Scope Exclusions): analytics only
    from wealth import assets
    assert "MUTUAL_FUND" in assets.EXCLUDED_CLASSES


def test_enterprise_mode_needs_dashboard_read(api, db, monkeypatch):
    client, _ = api
    from enterprise import apikeys, authz, config as EC, rbac, tenants, users
    _put(db, "200001", _growth(_weekdays(date(2025, 1, 1), END), 0.1, amp=0.002), "Alpha - Direct Plan - Growth")
    rbac.seed(db)
    tenants.ensure_default(db)
    u = users.create_user(db, "mfviewer", "Mf-analytics-2026", "default", ["VIEWER"])
    good = apikeys.create(db, u["user_id"], "default", "reader", ["dashboard:read"])["api_key"]
    bad = apikeys.create(db, u["user_id"], "default", "notices", ["notifications:read"])["api_key"]
    monkeypatch.setattr(authz, "settings", lambda: {**EC.DEFAULTS, "enabled": True})
    url = "/api/data/mf/200001/analytics"
    assert client.get(url).status_code == 401
    r = client.get(url, headers={"Authorization": "ApiKey " + bad})
    assert r.status_code == 403 and "dashboard:read" in r.json()["error"]
    assert client.get(url, headers={"Authorization": "ApiKey " + good}).status_code == 200
    assert client.get("/api/data/mf/200001/sip", params={"amount": "1000"},
                      headers={"Authorization": "ApiKey " + good}).status_code == 200


def test_page_script_parses_with_node(tmp_path):
    import os
    import re
    import shutil
    import subprocess
    node = shutil.which("node") or next((p for p in ("/opt/node22/bin/node",) if os.path.exists(p)), None)
    if not node:
        pytest.skip("node not installed")
    from dashboard.w35_page import render
    f = tmp_path / "page.js"
    f.write_text("\n".join(re.findall(r"<script>(.*?)</script>", render("t"), re.S)), encoding="utf-8")
    r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


# ── W39B-MFA polish: step-up, stamp duty, unit rounding, exit load, annualisation ──
# Every expected number below is worked out by hand in the comment next to it.

def _calendar(start, end, nav=10.0):
    """A NAV on every calendar day (a liquid fund's pattern), flat at `nav`."""
    out, d = [], start
    while d <= end:
        out.append((d, nav))
        d += timedelta(days=1)
    return out


def test_one_purchase_stamp_duty_and_units_rounded_down_by_hand():
    from data.mf_analytics import STAMP_DUTY_RATE, purchase
    assert STAMP_DUTY_RATE == 0.00005                                  # 0.005 %
    # 5000 x (1 - 0.00005) = 4999.75; 4999.75 / 37.1234 = 134.67920... -> 134.679 (37.1234 x 134.679 = 4999.7424
    # <= 4999.75 < 37.1234 x 134.680 = 4999.7795); the duty is 5000 x 0.00005 = 0.25
    assert purchase(5000, 37.1234) == (134.679, 0.25)
    # no stamp duty: 5000 / 37.1234 = 134.68594... -> 134.685 (x 37.1234 = 4999.9651; 134.686 would be 5000.0022)
    assert purchase(5000, 37.1234, stamp_duty=False) == (134.685, 0.0)
    u, duty = purchase(5000, 37.1234, round_units=False)               # unrounded: 4999.75 / 37.1234
    assert u == pytest.approx(4999.75 / 37.1234, rel=1e-12) and round(u, 5) == 134.67921 and duty == 0.25
    # exact 3-decimal results stay exact (decimal arithmetic): 1000 x 0.99995 / 10 = 99.995; 1100 -> 109.9945 ->
    # 109.994; 1210 -> 120.99395 -> 120.993
    assert [purchase(a, 10)[0] for a in (1000, 1100, 1210)] == [99.995, 109.994, 120.993]
    # 1000 x 0.99995 / 12.3456 = 80.99646... -> 80.996
    assert purchase(1000, 12.3456)[0] == 80.996


def test_step_up_on_each_anniversary_of_the_first_instalment(db):
    from data import mf_analytics as MA
    _put(db, "100011", _calendar(date(2025, 3, 1), date(2027, 4, 30)), "Flat Liquid Fund - Direct Plan - Growth")
    # start 15 Mar 2025, day 1: the first instalment is 1 Apr 2025, so the step-ups fall on 1 Apr 2026 and
    # 1 Apr 2027 (not on 1 Jan, not on 15 Mar): 12 x 1000 + 12 x 1100 + 1 x 1210 = 26,410 in 25 instalments
    s = MA.sip(db, "100011", 1000, 1, "2025-03-15", "2027-04-01", step_up_pct=10, stamp_duty=False, round_units=False)
    sch = s["schedule"]
    assert [x["amount"] for x in sch] == [1000.0] * 12 + [1100.0] * 12 + [1210.0]
    assert [x["step_ups"] for x in sch] == [0] * 12 + [1] * 12 + [2]
    assert (sch[0]["scheduled"], sch[11]["scheduled"], sch[12]["scheduled"]) == (
        date(2025, 4, 1), date(2026, 3, 1), date(2026, 4, 1))
    S = s["sip"]
    assert (S["instalments"], S["invested"], S["units"], S["value"]) == (25, 26410.0, 2641.0, 26410.0)
    assert (S["first_instalment"], S["last_instalment"]) == (1000.0, 1210.0)
    assert S["xirr_pct"] == pytest.approx(0.0, abs=1e-3)               # a flat NAV earns nothing
    assert s["lump_sum"]["invested"] == 26410.0 and s["lump_sum"]["units"] == 2641.0   # the same total
    assert s["inputs"]["step_up_pct"] == 10.0 and "step-up 10 % a year" in s["conventions"]
    # the defaults on the same plan: units 12 x 99.995 + 12 x 109.994 + 120.993 = 2640.861; value at NAV 10
    # 26,408.61; stamp duty 12 x 0.05 + 12 x 0.055 + 0.0605 = 1.3205 -> 1.32; gain 26408.61 - 26410 = -1.39
    d = MA.sip(db, "100011", 1000, 1, "2025-03-15", "2027-04-01", step_up_pct=10)["sip"]
    assert (d["units"], d["value"], d["stamp_duty"], d["invested"], d["gain"]) == (
        2640.861, 26408.61, 1.32, 26410.0, -1.39)
    # no step-up: every instalment 1000
    assert {x["amount"] for x in MA.sip(db, "100011", 1000, 1, "2025-03-15", "2027-04-01")["schedule"]} == {1000.0}


def test_exit_load_per_instalment_on_units_held_under_n_days(db):
    from data import mf_analytics as MA
    _put(db, "100012", _calendar(date(2025, 1, 1), date(2025, 6, 30)), "Load Fund - Direct Plan - Growth")
    args = ("100012", 1000, 1, "2025-01-01", "2025-06-30")
    off = {"stamp_duty": False, "round_units": False}
    # held to 30 Jun: 1 Jan 180 days, 1 Feb 149, 1 Mar 121, 1 Apr 90, 1 May 60, 1 Jun 29
    s = MA.sip(db, *args, exit_load_pct=1, exit_load_days=90, **off)
    assert [x["days_held"] for x in s["schedule"]] == [180, 149, 121, 90, 60, 29]
    # under 90 days: May and Jun, 1 % of 100 units x NAV 10 = 10 each; 1 Apr (exactly 90 days) is free
    assert [x["exit_load"] for x in s["schedule"]] == [0, 0, 0, 0, 10.0, 10.0]
    S = s["sip"]
    assert (S["value"], S["exit_load"], S["value_after_exit_load"], S["gain"]) == (6000.0, 20.0, 5980.0, -20.0)
    assert S["absolute_return_pct"] == pytest.approx(-20 / 6000 * 100, abs=1e-3) and S["xirr_pct"] < 0
    L = s["lump_sum"]                                                  # 6000 on 1 Jan, held 180 days: free
    assert (L["exit_load"], L["value_after_exit_load"]) == (0.0, 6000.0)
    # a 181-day window catches everything: 6 x 10 on the SIP, 1 % of 6000 on the lump sum
    w = MA.sip(db, *args, exit_load_pct=1, exit_load_days=181, **off)
    assert (w["sip"]["exit_load"], w["lump_sum"]["exit_load"], w["lump_sum"]["value_after_exit_load"]) == (
        60.0, 60.0, 5940.0)
    # exit_load_days defaults to 365 once a load is given; no load at all by default
    assert MA.sip(db, *args, exit_load_pct=1, **off)["sip"]["exit_load"] == 60.0
    plain = MA.sip(db, *args, **off)
    assert plain["sip"]["exit_load"] is None and plain["sip"]["value_after_exit_load"] == plain["sip"]["value"]
    assert plain["inputs"]["exit_load_pct"] is None and "no exit load" in plain["conventions"]
    # with the defaults: 99.995 units an instalment; the load 2 x 99.995 x 10 x 1 % = 19.999 -> 20.00; value
    # 6 x 99.995 x 10 = 5999.70; after the load 5979.701 -> 5979.70
    dflt = MA.sip(db, *args, exit_load_pct=1, exit_load_days=90)["sip"]
    assert (dflt["units"], dflt["value"], dflt["exit_load"], dflt["value_after_exit_load"]) == (
        599.97, 5999.7, 20.0, 5979.7)
    for bad in ({"exit_load_days": 90}, {"exit_load_pct": 11}, {"exit_load_pct": 1, "exit_load_days": 2.5},
                {"exit_load_pct": 1, "exit_load_days": 0}, {"step_up_pct": -1}, {"step_up_pct": 101},
                {"stamp_duty": "maybe"}, {"round_units": "2"}):
        with pytest.raises(ValueError):
            MA.sip(db, *args, **bad)


def test_volatility_is_annualised_with_the_navs_actually_observed_a_year(db):
    from data import mf_analytics as MA
    assert MA.periods_per_year([]) is None
    assert MA.periods_per_year([1, 1, 1]) == 365.0                     # a NAV every calendar day
    assert MA.periods_per_year([1, 1, 1, 1, 3]) == pytest.approx(5 * 365 / 7)   # every weekday: 260.71
    assert MA.periods_per_year([7, 7]) == pytest.approx(365 / 7)       # weekly: 52.14
    # a liquid fund: 401 calendar-daily NAVs, returns alternating 0.03 % / 0.01 % (mean 0.02 %, each 0.01 %
    # from it): sample stdev = 0.0001 x sqrt(400 / 399), with 365 observations a year
    rows, nav, d0 = [], 1000.0, date(2024, 1, 1)
    for i in range(401):
        if i:
            nav *= 1 + (0.0003 if i % 2 else 0.0001)
        rows.append((d0 + timedelta(days=i), nav))
    _put(db, "100013", rows, "Liquid Fund - Direct Plan - Growth")
    k = MA.analytics(db, "100013", rf_pct=6.5, peers=False)["risk"]
    assert k["periods_per_year"] == 365.0 and k["window"]["days_spanned"] == 400
    assert k["volatility_pct"] == pytest.approx(0.0001 * math.sqrt(400 / 399) * math.sqrt(365) * 100, abs=5e-4)
    # (a fixed 252 would have read sqrt(252 / 365) = 0.83x of that)
    # the category rank's trailing-1Y volatility on the same basis: the last NAV is 4 Feb 2025, a year back is
    # 4 Feb 2024, so the 366 returns after it, x sqrt(365)
    q = MA._quick(MA.load(db, "100013"), 400)
    rs = [rows[i][1] / rows[i - 1][1] - 1 for i in range(1, 401) if rows[i][0] > date(2024, 2, 4)]
    assert len(rs) == 366
    assert q["volatility_1y_pct"] == pytest.approx(statistics.stdev(rs) * math.sqrt(365) * 100, abs=5e-4)
    # weekly NAVs (a scheme ATIP caught once a week): 61 NAVs 7 days apart, returns +1 % / -1 %: stdev
    # 0.01 x sqrt(60 / 59), annualised with 365 / 7 = 52.14 observations a year, not 252
    rows, nav = [], 100.0
    for i in range(61):
        if i:
            nav *= 1.01 if i % 2 else 0.99
        rows.append((d0 + timedelta(days=7 * i), nav))
    _put(db, "100014", rows, "Weekly Fund - Direct Plan - Growth")
    k = MA.analytics(db, "100014", rf_pct=6.5, peers=False)["risk"]
    assert k["periods_per_year"] == pytest.approx(365 / 7, abs=1e-4)
    assert k["volatility_pct"] == pytest.approx(0.01 * math.sqrt(60 / 59) * math.sqrt(365 / 7) * 100, abs=1e-3)
    # a gap: the return across it and its days both leave, so the frequency stays 365 a year
    hole = [r for r in _calendar(date(2024, 1, 1), date(2024, 6, 30)) if not date(2024, 3, 1) <= r[0] <= date(2024, 3, 10)]
    hole = [(dd, 10 + 0.01 * (i % 3)) for i, (dd, _) in enumerate(hole)]
    _put(db, "100015", hole, "Holey Fund - Direct Plan - Growth")
    k = MA.analytics(db, "100015", rf_pct=6.5, peers=False)["risk"]
    assert k["window"]["excluded_gap_returns"] == 1 and k["periods_per_year"] == 365.0


def test_sip_route_takes_the_new_options(api, db):
    client, _ = api
    _put(db, "100012", _calendar(date(2025, 1, 1), date(2025, 6, 30)), "Load Fund - Direct Plan - Growth")
    q = {"amount": "1000", "day": "1", "start": "2025-01-01", "end": "2025-06-30"}
    r = client.get("/api/data/mf/100012/sip", params={**q, "stamp_duty": "0", "round_units": "0",
                                                       "exit_load_pct": "1", "exit_load_days": "90",
                                                       "step_up_pct": "5"})
    assert r.status_code == 200
    j = r.json()
    assert j["inputs"]["stamp_duty"] is False and j["sip"]["exit_load"] == 20.0 and j["sip"]["stamp_duty"] == 0
    d = client.get("/api/data/mf/100012/sip", params=q).json()
    assert d["inputs"]["stamp_duty"] is True and d["inputs"]["round_units"] is True and d["sip"]["units"] == 599.97
    assert client.get("/api/data/mf/100012/sip", params={**q, "stamp_duty": "perhaps"}).status_code == 400
    assert client.get("/api/data/mf/100012/sip", params={**q, "exit_load_days": "30"}).status_code == 400
    page = client.get("/data-platform").text
    assert all(x in page for x in ("sipsu", "sipsd", "sipru", "sipel", "stamp duty 0.005 %"))
