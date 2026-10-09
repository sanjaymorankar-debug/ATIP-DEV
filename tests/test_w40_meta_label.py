"""
W40: meta-labelling of the technical signals (ml/meta_label.py; AFML ch. 3-4, 7, 10).

Triple-barrier labels by hand (a gap through the stop, vertical expiry, the short side) and their identity
with the engine's own outcome rule; average uniqueness and return attribution by hand; purged k-fold with
embargo; features that are the same on a database truncated at the signal's close; a planted rule the
meta-model recovers (ADOPTABLE, kept signals beat all signals out of fold) and pure noise (NO_EDGE); the
registry gate; the disabled switch; the API and the /signals column on a seeded database.
"""
import datetime as dt
import json
import sqlite3

import numpy as np
import pandas as pd
import pytest

from ml import meta_label as M


@pytest.fixture
def db(temp_db):
    from db.schema import get_connection, init_db
    init_db()
    conn = get_connection()
    M.ensure_tables(conn)
    yield conn
    conn.close()


@pytest.fixture
def config(tmp_path, monkeypatch):
    """Point ml.config (and so ml.meta_label.settings) at a throwaway config.json; returns a writer."""
    from ml import config as MC
    path = tmp_path / "config.json"
    monkeypatch.setattr(MC, "CONFIG_PATH", path)

    def write(**meta):
        path.write_text(json.dumps({"ml": {"model_path": str(tmp_path / "ml")}, "meta_label": meta}), encoding="utf-8")
    write()
    return write


# ── 1. triple-barrier labels ──────────────────────────────────────────────

D = [str(d.date()) for d in pd.bdate_range("2026-09-01", periods=30)]


def _bar(i, o, h, l, c):
    return (D[i], o, h, l, c)


def test_triple_barrier_by_hand_long_short_gap_and_expiry():
    # long 100, stop 96 (-1 R = 4), target 108 (+2 R): the target on the second session
    lab = M.triple_barrier("BULL", 100, 96, 108, 5, [_bar(1, 100, 103, 99, 102), _bar(2, 102, 109, 101, 108)])
    assert (lab["label"], lab["barrier"], lab["t1"], lab["sessions"], lab["r_multiple"], lab["gap"]) == \
        (1, "PT", D[2], 2, 2.0, 0)
    # a gap down through the stop: label 0 on that session; the engine's R is -1 at the level, the fill -1.5 at the open
    lab = M.triple_barrier("BULL", 100, 96, 108, 5, [_bar(1, 94, 95, 93, 94), _bar(2, 94, 120, 94, 119)])
    assert (lab["label"], lab["barrier"], lab["t1"], lab["r_multiple"], lab["gap"], lab["r_fill"]) == \
        (0, "SL", D[1], -1.0, 1, -1.5)
    # a gap up through the target is label 1, filled better than the level
    lab = M.triple_barrier("BULL", 100, 96, 108, 5, [_bar(1, 110, 111, 109, 110)])
    assert (lab["label"], lab["barrier"], lab["gap"], lab["r_multiple"], lab["r_fill"]) == (1, "PT", 1, 2.0, 2.5)
    # vertical barrier: nothing touched in 3 sessions -> label 0 at the third close (+0.25 R)
    flat = [_bar(i, 100, 101, 99, 100) for i in (1, 2)] + [_bar(3, 100, 102, 99, 101)]
    lab = M.triple_barrier("BULL", 100, 96, 108, 3, flat + [_bar(4, 101, 130, 100, 129)])
    assert (lab["label"], lab["barrier"], lab["t1"], lab["sessions"], lab["r_multiple"]) == (0, "VB", D[3], 3, 0.25)
    # still open: fewer sessions than the horizon and nothing touched
    assert M.triple_barrier("BULL", 100, 96, 108, 3, flat[:2]) is None
    # short 100, stop 104, target 92: the low reaches 91 first -> label 1; a high of 105 first -> label 0
    lab = M.triple_barrier("BEAR", 100, 104, 92, 5, [_bar(1, 100, 101, 97, 98), _bar(2, 98, 99, 91, 92)])
    assert (lab["label"], lab["barrier"], lab["t1"], lab["r_multiple"], lab["return_pct"]) == (1, "PT", D[2], 2.0, 8.0)
    lab = M.triple_barrier("BEAR", 100, 104, 92, 5, [_bar(1, 101, 105, 100, 104)])
    assert (lab["label"], lab["barrier"], lab["r_multiple"], lab["gap"]) == (0, "SL", -1.0, 0)
    lab = M.triple_barrier("BEAR", 100, 104, 92, 5, [_bar(1, 106, 107, 105, 106)])          # gap up through it
    assert (lab["label"], lab["gap"], lab["r_fill"]) == (0, 1, -1.5)
    # a bar touching both levels counts as the stop (the engine's conservative rule)
    lab = M.triple_barrier("BULL", 100, 96, 108, 5, [_bar(1, 100, 110, 95, 100)])
    assert (lab["label"], lab["barrier"]) == (0, "SL")


def _price(conn, sym, i, o, h, l, c):
    conn.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) VALUES (?,?,?,?,?,?,1,'t')",
                 (sym, D[i], o, h, l, c))


def _sig(conn, sid, sym, i, direction, entry, stop, target, horizon=3, scan="golden_cross"):
    conn.execute("INSERT INTO technical_signal (signal_id, symbol, date, scan, name, direction, entry, stop, target, atr, "
                 "horizon, confluence, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,1,'OPEN')",
                 (sid, sym, D[i], scan, scan, direction, entry, stop, target, abs(entry - stop) / 2, horizon))


def test_labels_are_the_engines_own_outcomes(db):
    """build_labels and research/tech_signals.evaluate_signals share first_touch: same barrier, same date."""
    from research import tech_signals as TS
    for i, (o, h, l, c) in enumerate([(100, 103, 99, 102), (102, 109, 101, 108), (108, 110, 100, 105)], start=1):
        _price(db, "UP", i, o, h, l, c)
        _price(db, "SIDE", i, 100, 102, 99, 101)
    for i, (o, h, l, c) in enumerate([(94, 95, 93, 94), (94, 96, 90, 95), (95, 96, 94, 95)], start=1):
        _price(db, "GAP", i, o, h, l, c)
    _sig(db, "a", "UP", 0, "BULL", 100, 96, 108)
    _sig(db, "b", "GAP", 0, "BULL", 100, 96, 108)
    _sig(db, "c", "SIDE", 0, "BULL", 100, 90, 120)
    _sig(db, "d", "UP", 2, "BEAR", 108, 112, 100, horizon=5)          # a short: its target on the next session
    _sig(db, "e", "UP", 3, "BULL", 105, 101, 113, horizon=5)          # still open: no session after it yet
    db.commit()
    lab = M.build_labels(db)
    TS.evaluate_signals(db)
    eng = {r[0]: (r[1], str(r[2])[:10], r[3]) for r in db.execute(
        "SELECT signal_id, status, outcome_date, r_multiple FROM technical_signal WHERE status<>'OPEN'")}
    assert set(lab["labels"]) == set(eng) == {"a", "b", "c", "d"} and lab["open"] == 1
    to = {"PT": "TARGET", "SL": "STOPPED", "VB": "EXPIRED"}
    for sid, x in lab["labels"].items():
        assert (to[x["barrier"]], x["t1"], round(x["r_multiple"], 2)) == eng[sid]
    stored = {r[0]: r[1:] for r in db.execute("SELECT signal_id, label, barrier, gap FROM ml_meta_label")}
    assert stored == {"a": (1, "PT", 0), "b": (0, "SL", 1), "c": (0, "VB", 0), "d": (1, "PT", 0)}
    assert M.build_labels(db)["engine_mismatch"] == 0


# ── 3. sample weights and purged k-fold ───────────────────────────────────

def test_average_uniqueness_by_hand():
    # A lives on bars 0-3, B on 2-5, C on bar 3: c = 1, 1, 2, 3, 1, 1
    u = M.average_uniqueness([(0, 3), (2, 5), (3, 3)])
    assert u == pytest.approx([(1 + 1 + 1 / 2 + 1 / 3) / 4, (1 / 2 + 1 / 3 + 1 + 1) / 4, 1 / 3])
    assert M.average_uniqueness([(0, 4)]) == pytest.approx([1.0])
    # concurrency is counted per stock: the same spans on two stocks do not overlap
    assert M.uniqueness_weights(["X", "Y"], [(0, 3), (2, 5)]) == pytest.approx([1.0, 1.0])
    assert M.uniqueness_weights(["X", "X"], [(0, 3), (2, 5)]) == pytest.approx([0.75, 0.75])
    # AFML 4.5 return attribution: |sum r_t / c_t| over each life, rescaled to sum to N
    r = [0.01, 0.02, -0.03, 0.04, 0.0, 0.01]                    # c = 1, 1, 2, 2, 1, 1 for A (0-3), B (2-5)
    w = M.return_attribution_weights(["X", "X"], [(0, 3), (2, 5)], {"X": r})
    raw = [abs(0.01 + 0.02 - 0.03 / 2 + 0.04 / 2), abs(-0.03 / 2 + 0.04 / 2 + 0.0 + 0.01)]
    assert w == pytest.approx([2 * raw[0] / sum(raw), 2 * raw[1] / sum(raw)])


def test_purged_kfold_removes_overlapping_and_embargoed_training_labels():
    from ml.validation import purge_report, purged_kfold
    cal = [str(d.date()) for d in pd.bdate_range("2026-01-05", periods=30)]
    t0 = [cal[0]] + [cal[i] for i in range(20)]               # row 0: a long label D0 -> D12; rows 1..20: D_i -> D_i+3
    t1 = [cal[12]] + [cal[i + 3] for i in range(20)]
    folds = purged_kfold(t0, t1, n_splits=4, embargo=2, calendar=cal)
    assert len(folds) == 4
    assert sorted(np.concatenate([te for _, te in folds]).tolist()) == list(range(21)), "each row tested once"
    a, b = np.array(t0, dtype="datetime64[D]"), np.array(t1, dtype="datetime64[D]")
    pos = {d: i for i, d in enumerate(cal)}
    for tr, te in folds:
        assert not set(tr) & set(te)
        assert len({t0[i] for i in te} & {t0[i] for i in range(21) if i not in te}) == 0, "a date in one fold only"
        start, end = a[te].min(), b[te].max()
        emb_end = np.datetime64(cal[min(len(cal) - 1, pos[str(end)] + 2)])
        for i in range(21):
            allowed = b[i] < start or a[i] > emb_end
            assert (i in tr) == (allowed and i not in te), (i, str(start), str(end))
    # fold 1 by hand: tests D5..D9 (labels to D12); trains on D0->D3, D1->D4 and D15.. only
    tr1, te1 = folds[1]
    assert te1.tolist() == [6, 7, 8, 9, 10]
    assert tr1.tolist() == [1, 2, 16, 17, 18, 19, 20]
    rep = purge_report(t0, t1, folds)[1]
    assert (rep["purged"], rep["embargoed"]) == (7, 2)       # D2..D4 and the long label overlap; D10..D12 too
    with pytest.raises(ValueError):
        purged_kfold([cal[0]] * 3, [cal[1]] * 3, n_splits=2)
    with pytest.raises(ValueError):
        purged_kfold([cal[5]], [cal[4]], n_splits=2)


@pytest.mark.parametrize("family", ["logistic_regression", "native_gbm", "native_random_forest"])
def test_sample_weights_are_honoured_and_equal_weights_change_nothing(family):
    from ml.models import make_model
    rng = np.random.default_rng(1)
    X = rng.normal(size=(300, 3))
    y = [str(int(v)) for v in (X[:, 0] > 0)]
    y_flip = [("0" if v == "1" else "1") if i >= 150 else v for i, v in enumerate(y)]   # second half: the opposite rule
    params = M.MODEL_PARAMS[family]
    fit = lambda w: make_model(family, "classification", params, ["a", "b", "c"], M.CLASSES).fit(
        X, y_flip, **({} if w is None else {"sample_weight": w}))
    P0, P1 = fit(None).predict_proba(X), fit(np.full(300, 2.5)).predict_proba(X)
    assert np.allclose(P0, P1) or family == "native_random_forest"     # the forest's weighted bootstrap draws anew
    first = fit(np.r_[np.ones(150), np.zeros(150)])                    # only the first half counts
    p1 = lambda m: m.predict_proba(np.array([[2.0, 0, 0]]))[0, m.classes.index("1")]
    assert p1(first) > max(0.55, p1(fit(None)) + 0.05)                # the unweighted rules cancel out
    with pytest.raises(ValueError):
        fit(np.zeros(300))


def test_bet_size_follows_afml_and_is_discretised():
    import math
    p = np.array([0.2, 0.5, 0.7, 0.9])
    z = (p - 0.5) / np.sqrt(p * (1 - p))
    m = np.array([math.erf(x / math.sqrt(2)) for x in z])
    assert m[2] == pytest.approx(0.3374, abs=1e-4) and m[3] == pytest.approx(0.8176, abs=1e-4)
    assert M.bet_size(p).tolist() == [0.0, 0.0, 0.3, 0.8]
    assert M.bet_size([0.7], step=0.25).tolist() == [0.25]


# ── a seeded market: signals whose outcome follows a planted rule (or none) ──

N_BARS = 190


def _seed(conn, n_sym=100, per=4, mode="rule", seed=0, today=12):
    """n_sym stocks with `per` signals each, 30 sessions apart (one label never overlaps the next on its stock); a
    fifth of them carry a second scan on the same day (overlapping labels: uniqueness 1/2). Outcomes by `mode`:
    rule -- ADX > 25 wins 75 % (stop 20 %), else 15 % (stop 80 %); noise -- 40 % whatever the features.
    `today` open signals on the last session (half ADX 35, half ADX 15) for scoring."""
    from research import regime_gate as RG
    rng = np.random.default_rng(seed)
    cal = [str(d.date()) for d in pd.bdate_range("2025-01-01", periods=N_BARS)]
    prices, sigs, snaps = [], [], []
    for k in range(n_sym):
        sym = f"S{k:03d}"
        off = k % 30
        plan = {}
        for m in range(per):
            j = 5 + off + 30 * m
            side = 1 if (k + m) % 2 == 0 else -1
            adx = float(rng.uniform(10, 40))
            pw = (0.75 if adx > 25 else 0.15) if mode == "rule" else 0.40
            pl = (0.20 if adx > 25 else 0.80) if mode == "rule" else 0.55
            u = rng.random()
            plan[j] = (side, "win" if u < pw else "loss" if u < pw + pl else "flat", adx)
        c = 100.0
        steps = {}
        for j, (side, out, adx) in plan.items():
            if out == "win":
                steps.update({j + q: 1 + side * 0.015 for q in range(1, 7)})
            elif out == "loss":
                steps.update({j + q: 1 - side * 0.02 for q in range(1, 3)})
        for i, d in enumerate(cal):
            o = c
            c = round(c * steps.get(i, 1.0), 4)
            prices.append((sym, d, o, round(max(o, c) * 1.002, 4), round(min(o, c) * 0.998, 4), c))
            if i in plan:
                side, out, adx = plan[i]
                direction = "BULL" if side > 0 else "BEAR"
                atr = c * 0.02
                stop, target = (round(c - 2 * atr, 2), round(c + 4 * atr, 2)) if side > 0 else \
                    (round(c + 2 * atr, 2), round(c - 4 * atr, 2))
                gate = ["OPEN", "CAUTION", "CLOSED"][int(rng.integers(0, 3))]
                base = (sym, d, direction, c, stop, target, atr, int(rng.integers(0, 7)), gate,
                        RG.alignment(direction, gate), int(rng.integers(0, 2)))
                scans = ["golden_cross" if side > 0 else "death_cross"]
                if rng.random() < 0.2:
                    scans.append("ema_9_21_bull" if side > 0 else "ema_9_21_bear")
                for sc in scans:
                    sigs.append((f"{sym}-{d}-{sc}", sc) + base)
                snaps.append(_snap(rng, sym, d, adx))
    for q in range(today):
        sym, d = f"S{q:03d}", cal[-1]
        c = [p for p in prices if p[0] == sym][-1][5]
        adx = 35.0 if q % 2 == 0 else 15.0
        sigs.append((f"{sym}-{d}-golden_cross", "golden_cross", sym, d, "BULL", c, round(c * 0.96, 2),
                     round(c * 1.08, 2), c * 0.02, 3, "OPEN", "WITH", 1))
        snaps.append(_snap(rng, sym, d, adx))
    conn.executemany("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume,source) VALUES "
                     "(?,?,?,?,?,?,100000,'t')", prices)
    conn.executemany("INSERT INTO technical_signal (signal_id, scan, symbol, date, direction, entry, stop, target, atr, "
                     "confluence, market_gate, alignment, weekly_agrees, name, horizon, status) VALUES "
                     "(?,?,?,?,?,?,?,?,?,?,?,?,?,'seeded',20,'OPEN')", sigs)
    conn.executemany("INSERT INTO technical_snapshot (symbol, date, close, tech_rating, rsi_14, adx_14, vol_ratio, "
                     "rs_rating, rs_63_pct, pct_from_sma50, pct_from_sma200, bb_width_pct, return_1m_pct, "
                     "supertrend_dir, tech_rating_w, atr_pct) VALUES (?,?,100,?,?,?,?,?,?,?,?,?,?,?,?,2.0)", snaps)
    for i, d in enumerate(cal):
        mh = float(rng.uniform(20, 80))
        conn.execute("INSERT INTO market_health (date, mh_score, regime, nifty_trend, vix_level) VALUES (?,?,?,?,?)",
                     (d, mh, "BULL" if mh >= 60 else "NEUTRAL" if mh >= 40 else "BEAR", float(rng.uniform(0, 100)),
                      float(rng.uniform(10, 25))))
        conn.execute("INSERT INTO market_regime_gate (date, nifty_close, sma50, sma200, above_200dma, drawdown_pct, "
                     "dd_count, gate, status) VALUES (?,?,?,?,?,?,?,?,?)",
                     (d, 20000 + i, 19900 + i * 0.8, 19000.0, 1, float(rng.uniform(0, 8)), int(rng.integers(0, 6)),
                      "OPEN", "CONFIRMED_UPTREND"))
    conn.commit()
    return cal


def _snap(rng, sym, d, adx):
    return (sym, d, float(rng.uniform(-1, 1)), float(rng.uniform(30, 70)), adx, float(rng.uniform(0.5, 3)),
            int(rng.integers(1, 100)), float(rng.uniform(-20, 20)), float(rng.uniform(-10, 10)),
            float(rng.uniform(-20, 20)), float(rng.uniform(2, 15)), float(rng.uniform(-10, 10)),
            int(rng.choice([-1, 1])), float(rng.uniform(-1, 1)))


# ── 2. no feature uses data after t0 ──────────────────────────────────────

def test_every_feature_is_known_at_the_signals_close(db, tmp_path):
    """Features of the signals of one day are identical on the full database and on a copy from which every row
    dated after that day was deleted (prices, snapshots, signals, market health, the gate, the labels)."""
    assert set(M.FEATURE_SOURCES) == set(M.FEATURES) and all(M.FEATURE_SOURCES.values())
    _seed(db, n_sym=30, per=3, today=0)
    M.build_labels(db)
    dates = sorted({str(r[0])[:10] for r in db.execute("SELECT date FROM technical_signal")})
    t0 = dates[int(len(dates) * 0.75)]                  # late enough for earlier labels to have ended
    full_sigs = M._signals(db, " AND date=?", (t0,))
    X_full = M.features(db, full_sigs)
    # a copy truncated at t0
    cut = sqlite3.connect(str(tmp_path / "cut.db"))
    db.backup(cut)
    for t, c in (("prices_daily", "date"), ("technical_snapshot", "date"), ("technical_signal", "date"),
                 ("market_health", "date"), ("market_regime_gate", "date")):
        cut.execute(f"DELETE FROM {t} WHERE {c}>?", (t0,))
    cut.execute("DELETE FROM ml_meta_label")
    cut.commit()
    M.build_labels(cut)
    cut_sigs = M._signals(cut, " AND date=?", (t0,))
    assert [s["signal_id"] for s in cut_sigs] == [s["signal_id"] for s in full_sigs] and full_sigs
    X_cut = M.features(cut, cut_sigs)
    cut.close()
    assert np.array_equal(X_full, X_cut, equal_nan=True)
    col = {f: j for j, f in enumerate(M.FEATURES)}
    # the test bites: the market, snapshot and scan-record columns are filled, and the record has past labels
    for f in ("adx_14", "mh_score", "nifty_vs_sma50_pct", "scan_prior_win_rate", "gate", "same_day_hits"):
        assert not np.isnan(X_full[:, col[f]]).any(), f
    # and labels decided after t0 are not in it: the record counts only labels with t1 <= t0
    assert db.execute("SELECT COUNT(*) FROM ml_meta_label WHERE t1>?", (t0,)).fetchone()[0] > 0
    for i, s in enumerate(full_sigs):
        n_before = db.execute("SELECT COUNT(*) FROM ml_meta_label WHERE t1<=? AND scan=?",
                              (t0, s["scan"])).fetchone()[0]
        assert X_full[i, col["scan_prior_n"]] == n_before
    assert (X_full[:, col["scan_prior_n"]] > 0).any()


# ── 4-5. the planted rule, pure noise, the registry gate ───────────────────

def test_planted_rule_is_recovered_and_adoptable(db, config):
    from ml import registry as REG
    _seed(db, mode="rule")
    rep = M.train(db, M.settings())
    assert rep["status"] == "TRAINED" and rep["verdict"] == "ADOPTABLE", rep["reason"]
    e, c = rep["expectancy"], rep["classification"]
    assert rep["oos_signals"] >= 200 and e["kept"]["n"] >= 30 and e["rejected"]["n"] >= 30
    assert e["kept"]["mean_r"] > e["all"]["mean_r"] + 0.3 and e["t_stat"] >= 2
    assert c["auc"] > 0.7 and c["log_loss"] < c["baseline_log_loss"] and c["precision"] > c["base_rate"]
    assert max(rep["features"]["importances"], key=rep["features"]["importances"].get) == "adx_14"
    assert rep["weights"]["min_uniqueness"] == pytest.approx(0.5), "same-day duplicate scans share their label"
    assert all(f["purged"] + f["embargoed"] > 0 for f in rep["folds"][1:-1])
    assert set(rep["sizing"]["distribution"]) <= {f"{v / 10:.1f}" for v in range(11)}
    # stored as its own kind, TRAINED, never activated by itself
    m = REG.get_model(db, rep["model_id"])
    assert (m["purpose"], m["label_kind"], m["model_type"]) == ("meta_label", "meta_label", "native_gbm")
    assert REG.get_version(db, rep["model_id"], rep["version"])["status"] == "TRAINED"
    assert M.adopted_model(db) is None
    # unchanged data and config: the stored report, no new version
    again = M.train(db, M.settings())
    assert again["status"] == "UNCHANGED" and again["run_id"] == rep["run_id"]
    assert len(REG.list_versions(db, rep["model_id"])) == 1
    # the owner adopts it: the gate lets an ADOPTABLE version through
    for to in ("VALIDATION", "APPROVED", "ACTIVE"):
        REG.transition(db, rep["model_id"], rep["version"], to, "reviewed the out-of-fold report")
    assert M.adopted_model(db)["version"] == rep["version"]


def test_pure_noise_is_no_edge_and_cannot_be_activated(db, config):
    from ml import registry as REG
    _seed(db, mode="noise", seed=3)
    rep = M.train(db, {**M.settings(), "threshold": 0.4})
    assert rep["verdict"] == "NO_EDGE" and rep["oos_signals"] >= 200, rep["reason"]
    assert rep["expectancy"]["t_stat"] is None or rep["expectancy"]["t_stat"] < 2
    REG.transition(db, rep["model_id"], rep["version"], "VALIDATION", "look")
    with pytest.raises(REG.ModelRegistryError, match="NO_EDGE"):
        REG.transition(db, rep["model_id"], rep["version"], "APPROVED", "try anyway")
    assert M.allowed_to_activate(db, rep["model_id"], rep["version"])[0] is False


def test_too_few_signals_trains_nothing(db, config):
    _seed(db, n_sym=5, per=2, today=0)
    rep = M.train(db, M.settings())
    assert rep["status"] == "TOO_FEW" and rep["verdict"] == "NO_EDGE" and rep.get("model_id") is None
    assert db.execute("SELECT COUNT(*) FROM ml_model WHERE purpose='meta_label'").fetchone()[0] == 0


# ── 6. the switch, the jobs, the API and the /signals column ───────────────

def _adopt(db):
    from ml import registry as REG
    rep = M.train(db, M.settings())
    assert rep["verdict"] == "ADOPTABLE", rep["reason"]
    for to in ("VALIDATION", "APPROVED", "ACTIVE"):
        REG.transition(db, rep["model_id"], rep["version"], to, "adopted")
    return rep


def test_disabled_config_scores_nothing(db, config, monkeypatch):
    import utils.trading_calendar as TC
    from research import tech_signals as TS
    _seed(db, mode="rule")
    _adopt(db)                                      # an adopted model exists ...
    assert M.settings()["enabled"] is False         # ... but the switch is off (the default)
    out = M.score(db)
    assert out["status"] == "SKIPPED" and "enabled" in out["reason"]
    monkeypatch.setattr(TC, "is_trading_day", lambda d: True)
    assert M.run_nightly()["status"] == "SKIPPED" and M.run_weekly()["status"] == "SKIPPED"
    assert db.execute("SELECT COUNT(*) FROM ml_meta_label_score").fetchone()[0] == 0
    rows = M.attach_scores(db, TS.todays_signals(db))
    assert rows and not any("meta_prob" in r for r in rows)
    # enabled without an adopted model: still nothing
    config(enabled=True, model_id="some_other_model")
    assert M.score(db)["status"] == "SKIPPED" and M.adopted_model(db) is None


def test_scheduled_jobs_register_and_follow_the_switch(db, config, monkeypatch):
    import utils.trading_calendar as TC
    calls = []

    class _Job:
        def __init__(self):
            self.at_time = None

        def __getattr__(self, name):                  # .day / .saturday
            return self

        def at(self, t):
            self.at_time = t
            return self

        def do(self, fn, *args):
            calls.append((self.at_time, args))

    class _Schedule:
        def every(self):
            return _Job()

    lines = M.schedule_jobs(_Schedule(), lambda *a: None)
    assert [(t, a[0]) for t, a in calls] == [("20:45", "meta_label_score"), ("09:30", "meta_label_train")]
    assert calls[0][1][1] is M.run_nightly and calls[1][1][1] is M.run_weekly and len(lines) == 2
    monkeypatch.setattr(TC, "is_trading_day", lambda d: False)
    config(enabled=True)
    assert M.run_nightly()["reason"] == "not a market day"


@pytest.fixture
def api(tmp_path, monkeypatch, db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "dashboard_config.json")
    return TestClient(server.app)


def test_api_report_and_the_signals_column(db, config, api, monkeypatch):
    import utils.trading_calendar as TC
    cal = _seed(db, mode="rule")
    rep = _adopt(db)
    r = api.get("/api/research/meta-label/report").json()
    assert r["latest"]["verdict"] == "ADOPTABLE" and r["adopted"]["version"] == rep["version"]
    assert r["scoring"].startswith("off") and r["labels"]["signals"] == rep["signals"]
    assert not any("meta_prob" in x for x in api.get("/api/signals/technical").json())
    config(enabled=True)
    monkeypatch.setattr(TC, "is_trading_day", lambda d: True)
    out = M.run_nightly()
    assert out["status"] == "SUCCESS" and out["rows"] == 12 and out["date"] == cal[-1]
    rows = api.get("/api/signals/technical", params={"min_confluence": 0}).json()
    assert len(rows) == 12 and all(x["meta_prob"] is not None and 0 <= x["meta_size"] <= 1 for x in rows)
    hi = [x["meta_prob"] for x in rows if int(x["symbol"][1:]) % 2 == 0]       # ADX 35 today
    lo = [x["meta_prob"] for x in rows if int(x["symbol"][1:]) % 2 == 1]       # ADX 15
    assert min(hi) > max(lo) and all(x["meta_kept"] == (x["meta_prob"] >= 0.5) for x in rows)
    assert all(x["meta_model"] == f"{rep['model_id']}@{rep['version']}" for x in rows)
    r = api.get("/api/research/meta-label/report").json()
    assert r["scoring"] == "on" and r["latest_scores"][0]["scored"] == 12
    page = api.get("/signals").text
    assert "Meta-label P · size" in page and "metac(x)" in page and "meta_prob" in page


def test_cli_permissions_and_classification(db, config, capsys):
    from enterprise.authz import permission_for
    from enterprise.privacy import classify
    from enterprise.scoping import TABLES
    assert permission_for("GET", "/api/research/meta-label/report") == "research:read"
    for t in M.DDL:
        assert classify(t) is not None and t in TABLES
    assert M.main(["score"])["status"] == "SKIPPED"
    out = M.main(["report"])
    assert out["latest"] is None and out["scoring"].startswith("off")
    assert '"latest": null' in capsys.readouterr().out
    with pytest.raises(ValueError):
        M.check_config({"model_type": "xgboost"})
    with pytest.raises(ValueError, match="meta_label"):
        from ml.labels import LabelSpec, label_for
        label_for([], 0, LabelSpec(kind="meta_label"))
