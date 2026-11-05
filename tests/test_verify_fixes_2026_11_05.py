"""Phase 3 fixes of the 5 Nov 2026 verification pass (VF5-01 .. VF5-06). The defect log and evidence are in
docs/reports/2026-11-05-verification/VERIFICATION_REPORT.md."""
import sqlite3

import pandas as pd

from tests.test_w39_qa_suite import _paper_orders, _rule, paper  # noqa: F401  (paper is a fixture)

W_VPI = {"V": 0.18, "TS": 0.15, "RS": 0.12, "LQ": 0.1, "VOL": 0.1, "MR": 0.1, "FG": 0.1, "IS": 0.08, "NS": 0.07}


# ── VF5-01 (VF-01, owner decision): VPI reads volatility as calm = high, and the VPI BUY gates are re-set ──────────

def test_vpi_scores_the_calmer_stock_higher():
    from scores import engine as E
    base = {"adx_14": 30, "volume_ratio": 1.0, "rsi_14": 50}
    calm = E.compute_vpi({**base, "atr_pct": 1.0}, pd.DataFrame(), {}, pd.DataFrame(), 50.0, {"V": 1.0})
    wild = E.compute_vpi({**base, "atr_pct": 6.0}, pd.DataFrame(), {}, pd.DataFrame(), 50.0, {"V": 1.0})
    assert (calm, wild) == (88.0, 28.0)                     # V = 100 - ATR% x 12
    assert E.compute_vpi({**base, "atr_pct": 9.0}, pd.DataFrame(), {}, pd.DataFrame(), 50.0, {"V": 1.0}) == 0.0


def test_the_vpi_buy_gates_are_the_re_set_ones():
    from scores import engine as E
    assert (E.BUY_STRICT["vpi"], E.BUY_LOOSE["vpi"]) == (76.5, 66.5)
    ok = {"atip_score": 70, "zpi": 60, "cri": 40, "acs": 50, "mri": 55}
    mh = {"mh_score": 55, "regime": "NEUTRAL"}
    assert E.determine_signal({**ok, "vpi": 66.4}, mh) == "HOLD"     # the old loose gate (60) would have said BUY
    assert E.determine_signal({**ok, "vpi": 66.5}, mh) == "BUY"


def test_the_buy_legend_shows_the_engine_gates():
    from dashboard.score_glossary import _rules
    assert "VPI ≥ 66.5" in _rules()[0] and "VPI ≥ 76.5" in _rules()[0]


def test_the_vpi_glossary_says_calm():
    from dashboard.score_glossary import FORMULAS, describe
    assert FORMULAS["VPI"].startswith("100 − ATR % ×12 (calm)") and "calm" in describe("VPI")


# ── VF5-03 (VF-08, owner decision): MRI is labelled as the momentum-REVERSAL index it is ──────────────────────────

def test_mri_is_labelled_momentum_reversal():
    from dashboard.command_more import FACTORS
    assert dict(FACTORS)["mri"] == "Momentum-reversal (MRI)"


# ── VF5-05: a PCR-only fii_dii_market row is not "the session's flows are in" ─────────────────────────────────────

def test_a_pcr_only_row_does_not_count_as_flows(temp_db):
    from db.schema import get_connection, init_db
    from pipeline import scheduler as S
    init_db()
    c = get_connection()
    c.execute("INSERT INTO fii_dii_market (date, pcr) VALUES ('2026-11-05', 1.2)")
    c.commit()
    assert S._evening_data("2026-11-05")[0] is False
    c.execute("UPDATE fii_dii_market SET fii_net_cr = 512.0, dii_net_cr = -80.0 WHERE date = '2026-11-05'")
    c.commit()
    c.close()
    assert S._evening_data("2026-11-05")[0] is True


# ── VF5-04: the evening re-score fetches the session's own F&O file (MSI Options) before scoring ──────────────────

def test_the_evening_rescore_fetches_the_sessions_pcr_first(monkeypatch):
    from pipeline import scheduler as S
    import data.bhavcopy as B
    import data.derivatives as DV
    import scores.engine as E
    calls = []
    monkeypatch.setattr(S, "_evening_data", lambda td: (True, True))
    monkeypatch.setattr(S, "_signal_log_state", lambda td: "held")
    monkeypatch.setattr(S, "postmarket_target_date", lambda: "2026-11-04")
    monkeypatch.setattr(B, "sync_nse_index_closes", lambda *a, **k: None)
    monkeypatch.setattr(DV, "run_fo_pipeline", lambda td, lookback=5, recompute=False: calls.append(("fo", str(td), lookback)) or {"status": "SUCCESS", "rows": 1})
    monkeypatch.setattr(E, "run_scoring_pipeline", lambda td=None: calls.append(("score", str(td))) or {"status": "SUCCESS", "rows": 1})
    monkeypatch.setattr(S, "_log_session_signals", lambda td: calls.append(("log", str(td))))
    monkeypatch.setattr(S, "log_job", lambda *a, **k: None, raising=False)
    out = S.settle_session_flows("2026-11-05", final=False)
    assert out["rescored"] is True
    assert calls[:3] == [("fo", "2026-11-05", 1), ("score", "2026-11-05"), ("log", "2026-11-05")]


# ── VF5-06: a stop fill also cancels a sibling target that is waiting for its confirmation tap ────────────────────

def test_a_stop_fill_cancels_a_target_pending_confirmation(paper):
    from orders import rules as OR
    group = "G-VF5-06"
    tgt = _rule(side="SELL", trigger_value=110, role="TARGET", oco_group_id=group, require_confirmation=True)
    stp = _rule(side="SELL", trigger_value=90, trigger_direction="BELOW", role="STOP", oco_group_id=group,
                require_confirmation=False)
    from db.schema import get_connection
    c = get_connection()
    c.execute("UPDATE order_rules SET status = 'PENDING_CONFIRMATION' WHERE id = ?", (tgt["id"],))  # its trigger was hit
    c.commit()
    c.close()
    out = OR.settle_oco_group(OR.get_rule(stp["id"]), 10)
    assert out == {"cancelled": [tgt["id"]], "stop_reduced_to": 0}
    assert OR.get_rule(tgt["id"])["status"] == "CANCELLED"
    late = OR.confirm_rule(tgt["id"], force=True)              # the tap that used to send a second SELL
    assert late["status"] == "FAILED" and "not pending confirmation" in late["error"]
    assert _paper_orders() == []
