"""Session cycles, tax and the session ledger (accounting/, SES-02..06, ACC-01..07, 2 Nov 2026).

The arithmetic is checked against the owner's worked example (prompt 4.6) to the paisa; the integration tests run real
paper orders through orders.rules -> orders.broker -> orders.paper (the `paper` fixture of test_w39_qa_suite) and
check that the stored cycles, the engine's ledger(), gate(), the double-entry ledger and the reconciliation agree."""

import json

import pytest

from accounting import cycles as C
from accounting import tax as TX
from backtest.costs import cost_model
from tests.test_session_pools import _conn, _do, _entry, _ledger, _pool, _tick
from tests.test_w39_qa_suite import paper  # noqa: F401  (pytest fixture)


@pytest.fixture(autouse=True)
def _tax_config(tmp_path, monkeypatch):
    """Never the project's atip_data/config.json: the defaults unless a test writes its own."""
    monkeypatch.setattr(TX, "CONFIG_PATH", tmp_path / "tax-config.json")


DELIVERY = cost_model("nse_delivery")
# The brief's worked example (4.6) deducts STT from the gain and leaves out the cess; the defaults follow the law.
BRIEF = TX.settings({"tax": {"stt_deductible": True, "addons_enabled": False}})


# ── the worked example (4.6) ───────────────────────────────────────────────────────────────────

def test_worked_example_to_the_paisa():
    ch = C.itemise(DELIVERY, [("BUY", 25000.0), ("SELL", 25750.0)])
    assert ch == {"brokerage": 0.0, "stt": 50.75, "exchange": 1.51, "sebi": 0.05, "stamp": 3.75, "gst": 0.28,
                  "dp": 15.93}
    f = C.figures(25000.0, 25000.0, 25750.0, ch, "STCG", C.CASH_AFTER_CHARGES, BRIEF)
    assert f["gross_pnl"] == 750.00
    assert f["charges"] == 72.27
    assert f["net_pnl"] == 677.73
    assert f["tax_est"] == 135.55
    assert f["actual_pnl"] == 542.18
    assert f["end_amount"] == 25677.73                                       # cycle 2 under CASH_AFTER_CHARGES
    assert C.figures(25000.0, 25000.0, 25750.0, ch, "STCG", C.AFTER_TAX_RESERVE, BRIEF)["end_amount"] == 25542.18
    assert (f["gross_pct"], f["actual_pct"]) == (3.0, 2.17)                  # 750 / 25,000; 542.18 / 25,000


def test_worked_example_under_the_law():
    """The same cycle at the default settings: STT is not deductible from a capital gain (s.72(3) of the
    Income-tax Act 2025, was s.48) and the 4% cess is always added."""
    ch = C.itemise(DELIVERY, [("BUY", 25000.0), ("SELL", 25750.0)])
    f = C.figures(25000.0, 25000.0, 25750.0, ch, "STCG", C.CASH_AFTER_CHARGES)
    assert f["net_pnl"] == 677.73
    assert f["tax_est"] == 151.52                                            # (677.73 + STT 50.75) x 20 % x 1.04
    assert f["actual_pnl"] == 526.21
    assert f["end_amount"] == 25677.73                                       # charges only: tax is not held back
    assert C.figures(25000.0, 25000.0, 25750.0, ch, "STCG", C.AFTER_TAX_RESERVE)["end_amount"] == 25526.21


def test_whole_share_rounding_leaves_idle_cash():
    """S1's Rs 25,000 at Rs 1,091.95 (PNBHOUSING, 30 Oct): 22 shares, the buy's charges paid, the rest idle."""
    b = C.whole_shares(25000.0, 1091.95, DELIVERY)
    assert b["quantity"] == 22                                               # 23 x 1,091.95 = 25,114.85 > 25,000
    assert b["buy_value"] == 24022.90                                        # 22 x 1,091.95
    # STT 24.0229 + exchange 0.7135 + SEBI 0.0240 + stamp 3.6034 + GST 18 % x 0.7375 = 28.4966
    assert b["buy_charges"] == 28.50
    assert b["idle_cash"] == 948.60                                          # 25,000 - 24,022.90 - 28.50
    sell = round(22 * 1124.71, 2)                                            # +3 % at the tick: 24,743.62
    ch = C.itemise(DELIVERY, [("BUY", 24022.90), ("SELL", sell)])
    # STT 48.77, exchange 1.45, SEBI 0.05, stamp 3.60, GST 0.27, DP 15.93
    assert sum(ch.values()) == pytest.approx(70.07)
    f = C.figures(25000.0, 24022.90, sell, ch, "STCG", cfg=BRIEF, buy_charges=b["buy_charges"])
    assert (f["gross_pnl"], f["charges"], f["net_pnl"], f["tax_est"], f["actual_pnl"]) == \
        (720.72, 70.07, 650.65, 130.13, 520.52)
    law = C.figures(25000.0, 24022.90, sell, ch, "STCG", buy_charges=b["buy_charges"])
    assert (law["tax_est"], law["actual_pnl"]) == (145.48, 505.17)          # (650.65 + 48.77) x 20 % x 1.04
    assert f["idle_cash"] == 948.60
    assert f["end_amount"] == 25650.65                                       # the idle cash stays in the pool
    assert f["gross_pct"] == 2.88                                            # on the cycle's start, not the buy


def test_a_losing_cycle_carries_no_tax_and_shrinks_the_next():
    ch = C.itemise(DELIVERY, [("BUY", 25000.0), ("SELL", 24500.0)])         # stopped at -2 %
    f = C.figures(25000.0, 25000.0, 24500.0, ch, "STCG", C.AFTER_TAX_RESERVE)
    assert f["gross_pnl"] == -500.0
    assert f["net_pnl"] == round(-500.0 - f["charges"], 2) and f["net_pnl"] < -500
    assert f["tax_est"] == 0.0 and f["actual_pnl"] == f["net_pnl"]
    assert f["end_amount"] == round(25000 + f["net_pnl"], 2)


@pytest.mark.parametrize("rule,value,every,starts,profitable,expected", [
    ("FIXED", None, None, [25000, 30000], 5, 12000.0),
    ("RATCHET_PCT", 50, None, [25000, 30000, 28000], 0, 15000.0),           # 50 % of the highest start so far
    ("RATCHET_PCT", 40, None, [25000], 0, 12000.0),                         # never under the base floor
    ("STEP", 500, 3, [25000], 7, 13000.0),                                  # +500 for every 3 profitable cycles
    ("STEP", 500, 3, [25000], 2, 12000.0),
])
def test_floor_rules(rule, value, every, starts, profitable, expected):
    s = {"min_buy_value": 12000, "floor_rule": rule, "floor_value": value, "floor_step_every": every}
    assert C.floor_for(s, starts, profitable) == expected


def test_split_prefills_to_the_paisa():
    assert C.split(100000, ["S1", "S2", "S3"]) == {"total": 100000.0,
                                                   "amounts": {"S1": 33333.34, "S2": 33333.33, "S3": 33333.33}}
    with pytest.raises(ValueError, match="more than"):
        C.split(0, ["S1"])


# ── tax ────────────────────────────────────────────────────────────────────────────────────────

def test_tax_heads_and_rates():
    assert TX.head("CNC", "2026-04-10", "2027-04-10") == "STCG"              # exactly 12 months: short term
    assert TX.head("CNC", "2026-04-10", "2027-04-11") == "LTCG"
    assert TX.head("MIS", "2026-05-01", "2026-05-01") == "SPECULATIVE"
    assert TX.head("NRML", "2026-05-01", "2026-05-20") == "BUSINESS"
    assert TX.cycle_tax(1000, "LTCG") == 130.0 and TX.cycle_tax(1000, "SPECULATIVE") == 312.0      # with 4 % cess
    assert TX.fy_of("2026-03-31") == "FY 2025-26" and TX.fy_of("2026-04-01") == "FY 2026-27"


def test_the_defaults_are_the_law_for_tax_year_2026_27():
    cfg = TX.settings({})
    assert (cfg["regime"], cfg["stcg_pct"], cfg["ltcg_pct"], cfg["ltcg_exemption"], cfg["cess_pct"]) == \
        ("new", 20.0, 12.5, 125000.0, 4.0)
    assert cfg["addons_enabled"] is True and cfg["stt_deductible"] is False and cfg["other_income"] is None


def test_stt_is_added_back_on_capital_gains_but_not_on_business_income():
    assert TX.cycle_tax(1000, "STCG", stt=25) == 213.2                      # (1,000 + 25) x 20 % x 1.04
    assert TX.cycle_tax(1000, "LTCG", stt=25) == 133.25                     # (1,000 + 25) x 12.5 % x 1.04
    assert TX.cycle_tax(1000, "SPECULATIVE", stt=25) == 312.0               # STT is a business expense
    assert TX.cycle_tax(1000, "BUSINESS", stt=25) == 312.0
    assert TX.cycle_tax(1000, "STCG", stt=25, cfg=BRIEF) == 200.0           # the brief's way: net, no cess
    assert TX.cycle_tax(-10, "STCG", stt=25) == 3.12                        # STT can turn a small loss taxable
    assert TX.cycle_tax(-30, "STCG", stt=25) == 0.0


def test_slabs_tax_year_2026_27():
    # new regime: 0-4 lakh nil, 4-8 5 %, 8-12 10 %, 12-16 15 %, 16-20 20 %, 20-24 25 %, above 30 %
    assert TX.slab_tax(400000) == 0 and TX.slab_tax(800000) == 20000.0
    assert TX.slab_tax(1300000) == 75000.0                                   # 20,000 + 40,000 + 15,000
    assert TX.slab_tax(3000000) == 480000.0                                  # 3,00,000 to 24 lakh + 30 % of 6 lakh
    # old regime: 0-2.5 lakh nil, 2.5-5 5 %, 5-10 20 %, above 30 %
    assert TX.slab_tax(750000, "old") == 62500.0 and TX.slab_tax(1200000, "old") == 172500.0
    assert [TX.marginal_rate(i) for i in (0, 399999, 400000, 1199999, 1200000, 2400000)] == [0, 0, 5, 10, 15, 30]
    assert TX.basic_exemption("new") == 400000 and TX.basic_exemption("old") == 250000
    cfg = TX.settings({"tax": {"other_income": 900000}})
    assert TX.rate("BUSINESS", cfg) == 10.0 and TX.rate("SPECULATIVE", cfg) == 10.0 and TX.rate("STCG", cfg) == 20.0
    assert TX.rate("BUSINESS", TX.settings({})) == 30.0                      # income not given: slab_pct
    assert TX.rate("BUSINESS", TX.settings({"tax": {"other_income": 900000, "regime": "old"}})) == 20.0


def test_surcharge_is_capped_at_15_on_capital_gains():
    cfg = TX.settings({"tax": {"surcharge_pct": 25}})
    assert TX.cycle_tax(1000, "STCG", cfg=cfg) == 239.2                     # 200 + 15 % + 4 % cess
    assert TX.cycle_tax(1000, "BUSINESS", cfg=cfg) == 390.0                 # 300 + 25 % + 4 % cess


def test_fy_statement_uses_the_slabs_and_the_unused_nil_slab():
    rows = [_cyc("S1", 100000, "BUSINESS"), _cyc("S1", 50000, "STCG")]
    st = TX.fy_statement(rows, "FY 2026-27", TX.settings({"tax": {"other_income": 900000}}))
    heads = {h["head"]: h for h in st["heads"]}
    assert heads["BUSINESS"]["tax"] == 10000.0 and heads["BUSINESS"]["rate_pct"] == 10.0   # 9 -> 10 lakh at 10 %
    assert heads["STCG"]["tax"] == 10000.0 and st["basic_exemption_used"] == 0.0
    assert st["liability"] == 20800.0                                        # 20,000 + 4 % cess
    st = TX.fy_statement(rows, "FY 2026-27", TX.settings({"tax": {"other_income": 100000}}))
    heads = {h["head"]: h for h in st["heads"]}
    # 1 lakh other + 1 lakh F&O leaves 2 lakh of the 4 lakh nil slab: it covers the 50,000 STCG (s.196(2))
    assert heads["BUSINESS"]["tax"] == 0.0 and heads["STCG"]["taxable"] == 0.0
    assert st["basic_exemption_used"] == 50000.0 and st["liability"] == 0.0


def test_rates_come_from_config_and_validate(tmp_path):
    TX.CONFIG_PATH.write_text(json.dumps({"other": 1, "tax": {"stcg_pct": 15, "addons_enabled": True}}))
    cfg = TX.settings()
    assert cfg["stcg_pct"] == 15.0 and cfg["cess_pct"] == 4.0
    assert TX.cycle_tax(677.73, "STCG", cfg=cfg) == round(677.73 * 0.15 * 1.04, 2)
    with pytest.raises(ValueError, match="between 0 and 100"):
        TX.save({"stcg_pct": 150})
    TX.save({"slab_pct": 20, "effective_from": "2027-04-01"})
    saved = json.loads(TX.CONFIG_PATH.read_text())
    assert saved["other"] == 1 and saved["tax"]["slab_pct"] == 20.0 and saved["tax"]["stcg_pct"] == 15


def _cyc(sid, net, head="STCG", closed="2026-06-01", tax=None):
    return {"session_id": sid, "net_pnl": net, "tax_head": head, "closed_at": closed,
            "tax_est": TX.cycle_tax(net, head) if tax is None else tax}


def test_fy_statement_sets_off_losses_and_applies_the_exemption_once():
    rows = [_cyc("S1", 10000), _cyc("S2", -4000), _cyc("S1", 150000, "LTCG"), _cyc("S3", 20000, "LTCG"),
            _cyc("S1", 99999, closed="2027-04-01"),                          # next FY: left out
            _cyc("S4", -1000, "SPECULATIVE")]
    st = TX.fy_statement(rows, "FY 2026-27")
    heads = {h["head"]: h for h in st["heads"]}
    assert st["cycles"] == 5
    assert heads["STCG"]["net"] == 6000.0 and heads["STCG"]["tax"] == 1200.0
    assert heads["LTCG"]["taxable"] == 170000 - 125000 and heads["LTCG"]["tax"] == 5625.0
    assert st["ltcg_exemption_used"] == 125000.0
    assert st["carried_forward"] == {"SPECULATIVE": 1000.0}
    assert st["tax_before_addons"] == 6825.0 and st["cess"] == 273.0 and st["liability"] == 7098.0
    assert st["provisional_set_aside"] == round((2000 + 18750 + 2500) * 1.04, 2)   # per cycle: no set-off, no exemption


def test_fy_short_term_loss_is_set_off_against_long_term_gain():
    st = TX.fy_statement([_cyc("S1", -30000), _cyc("S2", 200000, "LTCG")], "FY 2026-27")
    heads = {h["head"]: h for h in st["heads"]}
    assert st["set_offs"][0]["amount"] == 30000.0
    assert heads["STCG"]["after_set_off"] == 0.0 and heads["LTCG"]["after_set_off"] == 170000.0
    assert heads["LTCG"]["taxable"] == 45000.0 and st["liability"] == 5850.0             # 5,625 + 4 % cess


# ── through real paper orders ──────────────────────────────────────────────────────────────────

def _charges_on(tmp_path):
    cfg = tmp_path / "config.json"
    c = json.loads(cfg.read_text())
    c["paper_cost_model"] = "nse_delivery"
    cfg.write_text(json.dumps(c))


def test_a_closed_cycle_is_stored_with_gross_charges_tax_and_actual(paper, tmp_path):
    from orders import sessions
    _charges_on(tmp_path)
    _pool("S1")
    _entry("ACME", 100.0)
    _tick(paper, ACME=100.0)
    _tick(paper, ACME=103.0)
    out = _do(C.sync)
    assert out["written"] == 1 and _do(C.sync)["written"] == 0               # written once
    c = _do(C.stored, "S1")[0]
    L = _ledger()
    q = c["qty"]
    assert c["buy_value"] == q * 100 and c["sell_value"] == q * 103
    assert c["gross_pnl"] == q * 3.0
    assert c["net_pnl"] == pytest.approx(L["booked"], abs=0.01)              # what the engine booked
    assert c["charges"] == round(sum(c[f"ch_{k}"] for k in (*C.CHARGE_TYPES, "other")), 2)
    assert c["ch_stt"] == round(q * 100 * 0.001 + q * 103 * 0.001, 2) and c["ch_dp"] == 15.93
    assert c["tax_head"] == "STCG" and c["tax_est"] == round((c["net_pnl"] + c["ch_stt"]) * 0.2 * 1.04, 2)
    assert c["actual_pnl"] == round(c["net_pnl"] - c["tax_est"], 2)
    assert c["end_amount"] == round(25000 + c["net_pnl"], 2) == pytest.approx(L["cash"], abs=0.01)
    assert c["env"] == "VIRTUAL" and c["exit_reason"] == "Target" and c["fy"].startswith("FY ")
    b = _do(C.book, "S1")
    assert b["tiles"]["gross_pnl"] == c["gross_pnl"] and b["tiles"]["actual_pnl"] == c["actual_pnl"]
    assert [w["label"] for w in b["charts"]["waterfall"]][0] == "Gross P&L"
    assert b["charts"]["waterfall"][-1] == {"label": "Actual P&L", "value": c["actual_pnl"], "kind": "total"}
    j = _do(sessions.journal)                                                # backward compatible shapes
    assert {"trades", "pnl", "pool"} <= set(j["sessions"][0])


def test_after_tax_reserve_keeps_the_tax_out_of_the_next_buy(paper, tmp_path):
    from orders import rules, sessions
    _charges_on(tmp_path)
    _pool("S1")
    _do(sessions.update_session, "S1", {"compound_basis": "AFTER_TAX_RESERVE"})
    _entry("ACME", 100.0)
    b = _entry("BETA", 50.0)
    _tick(paper, ACME=100.0)
    _tick(paper, ACME=103.0)
    L = _ledger()
    _do(C.sync)
    c = _do(C.stored, "S1")
    assert L["tax_reserve"] == c[0]["tax_est"] > 0
    assert L["investable_cash"] == round(L["cash"] - L["tax_reserve"], 2)
    assert L["investable_cash"] == pytest.approx(c[0]["end_amount"], abs=0.01)
    q = _do(sessions.pool_quantity, "S1", 50.0)
    assert q["budget"] == L["investable_cash"]
    _tick(paper, BETA=50.0)
    got = rules.get_rule(b["id"])
    assert got["execution_quantity"] * 50 <= L["investable_cash"]           # the reserve was not spent


def test_a_ratcheting_floor_holds_the_next_cycle_and_says_why(paper, tmp_path):
    from orders import rules, sessions
    _pool("S1")
    _do(sessions.update_session, "S1", {"floor_rule": "RATCHET_PCT", "floor_value": 100})
    _entry("ACME", 100.0)
    b = _entry("BETA", 50.0)
    _tick(paper, ACME=100.0)
    _tick(paper, ACME=98.0)                                                   # stopped: 24,500 < the 25,000 floor
    L = _ledger()
    assert L["floor"] == 25000.0 and L["floor_rule"] == "RATCHET_PCT"
    assert L["waiting"] == "Waiting: below minimum (₹24,500.00 of ₹25,000.00)"
    ev = _tick(paper, BETA=50.0)
    held = [e for e in ev if e["type"] == "HELD_BY_SESSION"]
    assert held and "under its Rs 25,000 minimum buy" in held[0]["reason"]
    assert rules.get_rule(b["id"])["status"] == rules.ACTIVE


def test_defaults_leave_the_gate_and_sizing_as_they_were(paper):
    from orders import sessions
    _pool("S1")
    L = _ledger()
    assert (L["floor"], L["floor_rule"], L["compound_basis"], L["tax_reserve"]) == \
        (12000.0, "FIXED", "CASH_AFTER_CHARGES", 0.0)
    assert L["investable_cash"] == L["cash"] == 25000 and L["waiting"] is None
    assert _do(sessions.pool_quantity, "S1", 100.0)["quantity"] == 250


def test_ledger_balances_and_reconciles_with_the_engine(paper, tmp_path):
    from accounting import ledger as LG
    from accounting import reconcile as R
    _charges_on(tmp_path)
    _pool("S1")
    _pool("S2")
    _entry("ACME", 100.0)
    _tick(paper, ACME=100.0)
    _tick(paper, ACME=103.0)
    _do(C.sync)
    tb = _do(LG.trial_balance)
    assert tb["balanced"] and tb["debit"] > 0
    assert _do(LG.session_ledger, "S1")["balance"] == pytest.approx(_ledger()["cash"], abs=0.01)
    reg = _do(LG.charges_register, "type")
    assert reg["total"] == _do(C.stored, "S1")[0]["charges"]
    cash = _do(R.check_cash)
    assert {i["session_id"]: i["status"] for i in cash["sessions"]} == {"S1": "MATCHED", "S2": "MATCHED"}


def test_new_tax_settings_re_estimate_stored_cycles_and_the_ledger(paper, tmp_path):
    from accounting import ledger as LG
    from accounting import reconcile as R
    from orders import sessions
    _charges_on(tmp_path)
    _pool("S1")
    _do(sessions.update_session, "S1", {"compound_basis": "AFTER_TAX_RESERVE"})
    _entry("ACME", 100.0)
    _tick(paper, ACME=100.0)
    _tick(paper, ACME=103.0)
    _do(C.sync)
    before = _do(C.stored, "S1")[0]
    TX.save({"stcg_pct": 15})
    out = _do(C.sync)
    after = _do(C.stored, "S1")[0]
    assert out["retaxed"] == 1 and out["written"] == 0 and _do(C.sync)["retaxed"] == 0
    assert after["tax_est"] == round((after["net_pnl"] + after["ch_stt"]) * 0.15 * 1.04, 2) < before["tax_est"]
    assert after["end_amount"] == round(25000 + after["actual_pnl"], 2)
    tb = _do(LG.trial_balance)
    bal = {r["account"]: r["balance"] for r in tb["rows"]}
    assert tb["balanced"]
    assert bal["Tax expense (est.)"] == after["tax_est"] == -bal["Tax payable (est.)"]    # the difference posted
    assert bal["Tax reserve:S1"] == after["tax_est"] == _ledger()["tax_reserve"]
    cash = _do(R.check_cash)
    assert {i["session_id"]: i["status"] for i in cash["sessions"]} == {"S1": "MATCHED"}


def test_a_cost_model_edit_never_rewrites_a_stored_cycle(paper, tmp_path):
    from accounting import ledger as LG
    _charges_on(tmp_path)
    _pool("S1")
    _entry("ACME", 100.0)
    _tick(paper, ACME=100.0)
    _tick(paper, ACME=103.0)
    _do(C.sync)
    before = _do(C.stored, "S1")[0]
    cfg = tmp_path / "config.json"
    c = json.loads(cfg.read_text())
    c["paper_cost_model"] = "nse_intraday"                     # other charges than the cycle was written with
    cfg.write_text(json.dumps(c))
    assert _do(C.sync)["retaxed"] == 0
    after = _do(C.stored, "S1")[0]
    assert {k: after[k] for k in ("charges", "net_pnl", "ch_stt", "tax_est")} == \
        {k: before[k] for k in ("charges", "net_pnl", "ch_stt", "tax_est")}
    TX.save({"stcg_pct": 15})                                  # a tax change re-estimates from the stored figures
    assert _do(C.sync)["retaxed"] == 1
    after = _do(C.stored, "S1")[0]
    assert after["charges"] == before["charges"] and after["net_pnl"] == before["net_pnl"]
    assert after["tax_est"] == round((before["net_pnl"] + before["ch_stt"]) * 0.15 * 1.04, 2)
    bal = {r["account"]: r["balance"] for r in _do(LG.trial_balance)["rows"]}
    assert bal["Tax expense (est.)"] == after["tax_est"]


def test_two_syncs_at_once_post_the_tax_correction_once(paper, tmp_path):
    from accounting import ledger as LG
    _charges_on(tmp_path)
    _pool("S1")
    _entry("ACME", 100.0)
    _tick(paper, ACME=100.0)
    _tick(paper, ACME=103.0)
    _do(C.sync)
    TX.save({"stcg_pct": 15})
    seen, real = [], LG._post
    LG._post = lambda conn, *a, **k: seen.append((a, k)) or 0  # what each sync would post, before either writes
    try:
        _do(C.sync)
        _do(LG.true_up_tax)
    finally:
        LG._post = real
    tax = [x for x in seen if ":TAX:" in x[0][0]]
    assert len(tax) == 2 and tax[0] == tax[1] and tax[0][0][0].endswith(":TAX:1")   # both computed the same id
    for a, k in tax:                                           # and both post it: the second is a no-op
        _do(lambda c: (LG._post(c, *a, **k), c.commit()))
    rows = _do(lambda c: c.execute("SELECT * FROM ledger_entries WHERE txn_id LIKE 'CYC:S1:%:TAX:%'").fetchall())
    assert len(rows) == 2 and _do(LG.trial_balance)["balanced"]


@pytest.mark.parametrize("field,value", [("stcg_pct", "nan"), ("slab_pct", "inf"), ("ltcg_exemption", "1e400"),
                                         ("other_income", "1e400"), ("other_income", "nan")])
def test_tax_settings_refuse_nan_and_infinity(field, value):
    with pytest.raises(ValueError):
        TX.validate({field: value})
    TX.CONFIG_PATH.write_text(json.dumps({"tax": {"stcg_pct": float("nan")}}))
    assert TX.settings()["stcg_pct"] == 20.0                   # a stored NaN falls back to the default


def test_intraday_and_fno_cycles_are_taxed_at_the_slab_their_profit_reaches():
    cfg = TX.settings({"tax": {"other_income": 0}})            # a full-time trader: other income in the nil slab
    assert TX.rate("BUSINESS", cfg) == 0.0                     # the next rupee of other income alone
    assert TX.cycle_tax(500000, "SPECULATIVE", cfg=cfg) == round(500000 * 0.05 * 1.04, 2)
    assert TX.cycle_tax(2000000, "BUSINESS", cfg=cfg) == round(2000000 * 0.20 * 1.04, 2)
    assert TX.cycle_tax(100000, "BUSINESS", cfg=TX.settings({"tax": {"other_income": 1500000}})) == \
        round(100000 * 0.15 * 1.04, 2)


def test_transfers_move_cash_but_never_out_of_an_open_trade(paper):
    from accounting import ledger as LG
    from orders import sessions
    _pool("S1")
    _pool("S2")
    _entry("ACME", 100.0)
    _tick(paper, ACME=100.0)                                                   # S1 holds ACME
    with pytest.raises(ValueError, match="holds an open trade"):
        _do(LG.transfer, "S1", "S2", 1000, "rebalance")
    with pytest.raises(ValueError, match="Say why"):
        _do(LG.transfer, "S2", "S1", 1000, "")
    out = _do(LG.transfer, "S2", "S1", 5000, "S1 is doing well")
    assert out["amount"] == 5000.0
    assert _ledger("S2")["cash"] == 20000.0 and _ledger("S2")["transfers"] == -5000.0
    assert _ledger("S1")["transfers"] == 5000.0
    assert _do(LG.trial_balance)["balanced"]
    ev = _do(sessions.events, "S2")
    assert ev[0]["event"] == "TRANSFER" and "S1 is doing well" in ev[0]["detail"]


def test_contract_note_reconciliation_matches_and_flags(paper, tmp_path):
    from accounting import reconcile as R
    from orders import rules
    from orders.sessions import _charges
    _charges_on(tmp_path)
    _pool("S1")
    r = _entry("ACME", 100.0)
    _tick(paper, ACME=100.0)
    e = rules.get_rule(r["id"])
    d = str(e["updated_at"])[:10]
    fee = _do(_charges, [e])[e["id"]]
    note = ("Trade Date,Symbol,Buy/Sell,Quantity,Total Charges\n"
            f"{d},ACME,B,{int(e['execution_quantity'])},{fee}\n"
            f"{d},ZETA,S,10,20.00\n")
    out = _do(R.import_contract_note, note)
    assert (out["rows"], out["matched"], out["unmatched"]) == (2, 1, 1)
    items = _do(R.items, None, "CHARGES")
    zeta = next(i for i in items if i["symbol"] == "ZETA")
    assert zeta["status"] == "OPEN" and "no executed ATIP order" in zeta["detail"]
    assert _do(R.resolve, zeta["id"], "INVESTIGATE", "not ours")["status"] == "INVESTIGATE"
    with pytest.raises(ValueError, match="needs columns"):
        R.parse_contract_note("a,b\n1,2\n")
