"""
W39 backtest validation tooling:

  BT-18  purge + embargo between the strategy walk-forward's windows
         (backtest/walkforward.py: build_windows / run_walk_forward)
  BT-19  transaction-cost stress sweep with a break-even multiplier
         (backtest/robustness.py: cost_sweep / break_even)

Prices are synthetic and deterministic: three sine-wave symbols the dip strategy
trades often (AAA / BBB / CCC) and three straight lines for buy-and-hold cases
whose net return is linear in the cost multiplier (UPP +1%, DWN -2%, RUN +20%).
Assertions are about the mechanics, never about a strategy being good.
"""

import json
import math

import pytest

START, END = "2025-01-01", "2025-09-30"
WAVES = ("AAA", "BBB", "CCC")


@pytest.fixture
def sessions(temp_db, tmp_path, monkeypatch):
    import backtest.config as bc
    from backtest.walkforward import trading_sessions
    from db.schema import get_connection, init_db
    monkeypatch.setattr(bc, "CONFIG_PATH", tmp_path / "no_config.json")     # DEFAULTS, whatever config.json says
    init_db()
    days = trading_sessions(START, END)
    rows = []
    for k, sym in enumerate(WAVES):
        prev = None
        for t, d in enumerate(days):
            c = round(100 * (1 + 0.0008 * t) * (1 + 0.07 * math.sin(2 * math.pi * (t + 5 * k) / 17)), 2)
            o = prev or c
            rows.append((sym, str(d), o, round(max(o, c) * 1.004, 2), round(min(o, c) * 0.996, 2), c, 2_000_000))
            prev = c
    for sym, a, b in (("UPP", 100, 101), ("DWN", 100, 98), ("RUN", 100, 120)):
        for t, d in enumerate(days):
            c = round(a + (b - a) * t / (len(days) - 1), 4)
            rows.append((sym, str(d), c, c, c, c, 2_000_000))
    conn = get_connection()
    conn.executemany("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return days


def _dip(**kw):
    return {"strategy_id": "dip", "universe": list(WAVES), "start": START, "end": END, **kw}


def _hold(sym):
    return {"strategy_id": "buy_and_hold", "universe": [sym], "start": START, "end": END, "cost_model": "flat",
            "slippage": {"kind": "none", "value": 0}}


def _old_windows(sessions, train, validation, test, step):
    """build_windows as it was before BT-18 (back-to-back windows)."""
    span, out, i = train + validation + test, [], 0
    while i + span <= len(sessions):
        out.append((
            (sessions[i], sessions[i + train - 1]),
            (sessions[i + train], sessions[i + train + validation - 1]) if validation else None,
            (sessions[i + train + validation], sessions[i + span - 1])))
        i += step
    return out


def _edges(wins):
    return [(w["train"], w["validation"], w["test"]) for w in wins]


# ── BT-18: purge + embargo ─────────────────────────────────────────────────

def test_default_windows_match_the_back_to_back_layout(sessions):
    from backtest.walkforward import build_windows
    for layout in ((40, 20, 20, 20), (40, 0, 20, 25), (60, 30, 30, 30)):
        wins = build_windows(sessions, *layout)
        assert _edges(wins) == _old_windows(sessions, *layout)
        assert _edges(wins) == _edges(build_windows(sessions, *layout, purge=0, embargo=0))
        assert all(w["purged"] == [] and w["embargoed"] == [] and w["purged_sessions"] == 0
                   and w["embargoed_sessions"] == 0 for w in wins)


def test_purge_ends_in_sample_early_and_embargo_starts_out_of_sample_late(sessions):
    from backtest.walkforward import build_windows
    ix = {d: i for i, d in enumerate(sessions)}
    base = build_windows(sessions, 40, 20, 20, 20)
    wins = build_windows(sessions, 40, 20, 20, 20, purge=3, embargo=2)
    assert len(wins) == len(base) >= 3
    for w, b in zip(wins, base):
        assert w["train"] == (b["train"][0], sessions[ix[b["train"][1]] - 3])
        assert w["validation"] == (sessions[ix[b["validation"][0]] + 2], sessions[ix[b["validation"][1]] - 3])
        assert w["test"] == (sessions[ix[b["test"][0]] + 2], b["test"][1])
        # the gap is exactly the purged sessions then the embargoed ones, at each boundary
        assert w["purged"] == [(sessions[ix[b["validation"][0]] - 3], b["train"][1]),
                               (sessions[ix[b["test"][0]] - 3], b["validation"][1])]
        assert w["embargoed"] == [(b["validation"][0], sessions[ix[b["validation"][0]] + 1]),
                                  (b["test"][0], sessions[ix[b["test"][0]] + 1])]
        assert (w["purged_sessions"], w["embargoed_sessions"]) == (6, 4)
    # no validation window: one boundary, train -> test
    one = build_windows(sessions, 40, 0, 20, 20, purge=5, embargo=1)
    for w, (tr, _va, te) in zip(one, _old_windows(sessions, 40, 0, 20, 20)):
        assert w["train"] == (tr[0], sessions[ix[tr[1]] - 5]) and w["validation"] is None
        assert w["test"] == (sessions[ix[te[0]] + 1], te[1])
        assert (w["purged_sessions"], w["embargoed_sessions"]) == (5, 1)


def test_purge_and_embargo_must_leave_every_window_a_session(sessions):
    from backtest.walkforward import build_windows
    for kw in ({"purge": -1}, {"embargo": -1}, {"purge": 40}, {"embargo": 20}, {"purge": 10, "embargo": 10}):
        with pytest.raises(ValueError):
            build_windows(sessions, 40, 20, 20, 20, **kw)
    assert build_windows(sessions, 40, 20, 20, 20, purge=10, embargo=9)[0]["validation"] is not None


def test_walk_forward_defaults_keep_todays_windows_and_results(sessions):
    from backtest import service, store
    from backtest.walkforward import run_walk_forward
    from db.schema import get_connection
    out = run_walk_forward(_dip(), 40, 20, 20, 20)
    old = _old_windows(sessions, 40, 20, 20, 20)
    assert out["status"] == "COMPLETED" and _edges(out["windows"]) == old
    assert (out["purge_sessions"], out["embargo_sessions"], out["sessions_purged"], out["sessions_embargoed"]) == \
        (0, 0, 0, 0)
    assert out["recommended_purge_sessions"] == 10                  # dip's max_hold_sessions
    conn = get_connection()
    try:
        kids = [c for c in store.child_runs(conn, out["run_id"]) if c["kind"] == "wf_test"]
        parent = store.get_run(conn, out["run_id"])
    finally:
        conn.close()
    assert [(str(c["start_date"]), str(c["end_date"])) for c in kids] == [(str(te[0]), str(te[1])) for _, _, te in old]
    assert "recommended purge: 10" in parent["bias_report"]["purge_embargo"]
    # the first test window, run on its own as before BT-18, gives the same numbers
    tr, va, te = old[0]
    alone = service.create_and_run({"strategy_id": "dip", "universe": list(WAVES), "period_label": "test",
                                    "allow_test": True, "params": out["windows"][0]["chosen_params"],
                                    "periods": {"research": [str(tr[0]), str(tr[1])],
                                                "validation": [str(va[0]), str(va[1])],
                                                "test": [str(te[0]), str(te[1])]}})
    assert alone["metrics"]["total_return"] == out["windows"][0]["test_metrics"]["total_return"]


def test_no_out_of_sample_trade_enters_inside_the_embargo(sessions):
    from backtest import store
    from backtest.walkforward import run_walk_forward
    from db.schema import get_connection

    def entries(out):
        conn = get_connection()
        try:
            return {w["index"]: [str(t["entry_date"]) for t in store.get_rows(conn, "backtest_trade", w["test_run_id"])]
                    for w in out["windows"]}
        finally:
            conn.close()
    cands = [{"stop_pct": 3}, {"stop_pct": 5}]
    out = run_walk_forward(_dip(), 40, 20, 20, 20, cands, "sharpe", purge_sessions=3, embargo_sessions=2)
    n = len(out["windows"])
    assert out["status"] == "COMPLETED" and (out["sessions_purged"], out["sessions_embargoed"]) == (6 * n, 4 * n)
    conn = get_connection()
    try:
        kids = store.child_runs(conn, out["run_id"])
        snap = store.get_run(conn, out["run_id"])["config"]["walk_forward"]
    finally:
        conn.close()
    assert (snap["purge_sessions"], snap["embargo_sessions"]) == (3, 2)
    embargoed = {w["index"]: {str(d) for d in sessions if w["embargoed"][-1][0] <= d <= w["embargoed"][-1][1]}
                 for w in out["windows"]}
    got = entries(out)
    for w in out["windows"]:
        mine = {c["kind"]: c for c in kids if c["window_index"] == w["index"]}
        assert (str(mine["wf_test"]["start_date"]), str(mine["wf_test"]["end_date"])) == tuple(map(str, w["test"]))
        assert (str(mine["wf_validation"]["start_date"]), str(mine["wf_validation"]["end_date"])) == \
            tuple(map(str, w["validation"]))
        assert all(d not in embargoed[w["index"]] and d >= str(w["test"][0]) for d in got[w["index"]])
    assert sum(map(len, got.values())) > 0
    # without the embargo the same strategy does enter on those sessions -- the check above bites
    plain = entries(run_walk_forward(_dip(), 40, 20, 20, 20, cands, "sharpe"))
    assert any(d in embargoed[k] for k, ds in plain.items() for d in ds)


# ── BT-19: transaction-cost stress ─────────────────────────────────────────

def test_cost_sweep_at_one_is_the_plain_backtest(sessions):
    from backtest import service
    from backtest.robustness import cost_sweep
    plain = service.create_and_run(_dip())["metrics"]
    out = cost_sweep(_dip())
    assert out["status"] == "COMPLETED" and out["multipliers"] == [0, 0.5, 1, 1.5, 2, 3, 5]
    one = next(p for p in out["points"] if p["multiplier"] == 1)
    assert one == out["baseline"]
    assert (one["total_return"], one["trades"], one["costs_paid"]) == \
        (plain["total_return"], plain["trades"], plain["costs_paid"])
    assert {"sharpe", "profit_factor", "max_drawdown", "slippage_paid"} <= set(one)


def test_net_return_never_rises_with_the_multiplier(sessions):
    from backtest.robustness import cost_sweep
    for scale, paid in (("costs", "costs_paid"), ("slippage", "slippage_paid"), ("both", "costs_paid")):
        out = cost_sweep(_dip(), scale=scale)
        rets = [p["total_return"] for p in out["points"]]
        assert all(b <= a for a, b in zip(rets, rets[1:])) and out["monotone"] is True, scale
        spent = [p[paid] for p in out["points"]]
        assert spent[0] == 0 and all(b > a for a, b in zip(spent, spent[1:])), scale
        assert out["break_even"] is None and out["break_even_note"] == "positive across the grid"


def test_scaled_costs_multiply_every_itemised_charge():
    from backtest.costs import cost_model
    from backtest.robustness import PCT_FIELDS, scaled_cost_overrides
    snap = {"cost_model": "nse_intraday", "cost_overrides": {"dp_charge_per_sell": 15.93, "brokerage_min": 5.0}}
    base = cost_model(snap["cost_model"], snap["cost_overrides"])
    for x in (0, 0.5, 2, 3):
        m = cost_model(snap["cost_model"], scaled_cost_overrides(snap, x))
        for side in ("BUY", "SELL"):
            for value in (1_000.0, 50_000.0, 5_000_000.0):            # min, proportional, capped brokerage
                got, want = m.charges(side, value), base.charges(side, value)
                for k in want:
                    assert got[k] == pytest.approx(x * want[k], abs=2e-4), (x, side, value, k)
    # robustness's costs_x2 still doubles the rates only, as before
    over = scaled_cost_overrides(snap, 2, PCT_FIELDS)
    assert over["brokerage_pct"] == 0.06 and over["dp_charge_per_sell"] == 15.93 and over["brokerage_min"] == 5.0


def test_break_even_interpolates_between_grid_points():
    from backtest.robustness import break_even
    assert break_even([0, 1, 2, 3], [0.03, 0.01, -0.01, -0.03])[0] == 1.5
    assert break_even([3, 0, 2, 1], [-0.03, 0.03, -0.01, 0.01])[0] == 1.5        # order does not matter
    assert break_even([0, 1, 2], [0.02, 0.01, 0.0])[0] == 2.0
    assert break_even([0, 1, 5], [0.04, 0.03, -0.01]) == (4.0, "net return crosses zero at 4.00x the configured cost")
    assert break_even([0, 1, 2], [0.03, None, -0.03])[0] == 1.0                  # a failed point is skipped
    assert break_even([0, 1, 2], [0.03, 0.02, 0.01]) == (None, "positive across the grid")
    assert break_even([0, 1, 2], [-0.01, -0.02, -0.03]) == (None, "negative even at zero cost")
    assert break_even([0.5, 1], [-0.01, -0.02]) == (None, "negative even at the lowest multiplier (0.5x)")
    assert break_even([], []) == (None, "no completed runs")


def test_break_even_on_a_constructed_backtest(sessions):
    from backtest.robustness import cost_sweep
    # one buy-and-hold trade, flat costs, no slippage: net return is linear in the multiplier
    out = cost_sweep(_hold("UPP"))
    r = {p["multiplier"]: p["total_return"] for p in out["points"]}
    assert all(p["trades"] == 1 for p in out["points"]) and r[3] > 0 > r[5]
    assert 3 < out["break_even"] < 5
    assert out["break_even"] == pytest.approx(r[0] / (r[0] - r[1]), abs=0.01)  # gross / cost at x1
    assert out["break_even_note"].startswith("net return crosses zero at")
    down = cost_sweep(_hold("DWN"))
    assert (down["break_even"], down["break_even_note"]) == (None, "negative even at zero cost")
    up = cost_sweep(_hold("RUN"), [0, 1, 2])
    assert (up["break_even"], up["break_even_note"]) == (None, "positive across the grid")


def test_cost_sweep_refuses_the_test_window_and_bad_grids(sessions):
    from backtest.robustness import cost_sweep, prepare_cost_sweep
    assert prepare_cost_sweep(_dip(), [2, 0, 1, 1]) == [0, 1, 2]
    for args in ((_dip(), None, "impact"), (_dip(), [1]), (_dip(), [-1, 1]), (_dip(), [0, 500])):
        with pytest.raises(ValueError):
            cost_sweep(*args)
    periods = {"research": ["2025-01-01", "2025-05-30"], "test": ["2025-06-02", END]}
    with pytest.raises(ValueError, match="test window"):
        cost_sweep({"strategy_id": "dip", "universe": list(WAVES), "periods": periods, "period_label": "test",
                    "allow_test": True})


def test_cli_and_routes_expose_the_options(sessions, tmp_path, monkeypatch, capsys):
    from backtest.__main__ import main
    base = ["--strategy", "dip", "--universe", ",".join(WAVES), "--start", START, "--end", END]
    main(["costsweep", *base, "--multipliers", "0,1,2"])
    out = json.loads(capsys.readouterr().out)
    assert [p["multiplier"] for p in out["points"]] == [0, 1, 2] and out["scale"] == "costs"
    main(["walkforward", *base, "--train", "40", "--validation", "0", "--test", "20", "--step", "20",
          "--purge", "4", "--embargo", "1"])
    out = json.loads(capsys.readouterr().out)
    assert (out["purge_sessions"], out["embargo_sessions"]) == (4, 1) and out["sessions_embargoed"] > 0

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    c = TestClient(server.app)
    h = {security.TOKEN_HEADER: security.token()}
    # validation runs synchronously, so a bad option is a 400 (nothing is started in the background)
    wf = {"request": _dip(), "train": 40, "validation": 20, "test": 20, "step": 20}
    r = c.post("/api/backtests/walkforward", headers=h, json={**wf, "embargo": 20})
    assert r.status_code == 400 and "embargo" in r.json()["error"]
    r = c.post("/api/backtests/walkforward", headers=h, json={**wf, "purge": 40})
    assert r.status_code == 400 and "purge" in r.json()["error"]
    r = c.post("/api/backtests/costsweep", headers=h, json={"request": _dip(), "scale": "impact"})
    assert r.status_code == 400 and "scale" in r.json()["error"]
    r = c.post("/api/backtests/costsweep", headers=h, json={"request": _dip(), "multipliers": [1]})
    assert r.status_code == 400
