"""
W39 Phase 3 (item 2, OF-01..03): order-flow imbalance from quote changes on Dhan's 20-level depth -- e_n of Cont,
Kukanov & Stoikov against hand calculations (bid and ask price up, down, unchanged; quantity changes), the
multi-level version of Xu, Gould & Howison and its depth normalisation, the sums between snapshots, the frame
path the feed uses, storage and the columns added to an old table, and the validation: a planted OFI -> price
relation is recovered, noise is not called predictive, and a simulated book shows OFI's strong contemporaneous
relation. Nothing connects to Dhan.
"""
import asyncio
import datetime as dt
import math
import sqlite3
import struct

import numpy as np
import pytest

from data import depth20 as D


def _msg(code, sid, levels, seg=1, extra=0):
    body = b"".join(struct.pack("<dII", p, q, o) for p, q, o in levels)
    body += b"\0" * (16 * (20 - len(levels)))
    return struct.pack("<hBBiI", 12 + len(body), code, seg, sid, extra) + body


def _e(prev_b, prev_a, b, a, k=1):
    return D.ofi_events((prev_b, prev_a), (b, a), k)


BID, ASK = [(100.00, 500, 5)], [(100.10, 300, 3)]


# ── e_n by hand ───────────────────────────────────────────────────────────

def test_e_n_when_the_bid_moves_up_down_or_stays():
    assert _e(BID, ASK, [(100.05, 200, 2)], ASK) == [200], "bid raised: + the new bid quantity"
    assert _e(BID, ASK, [(99.95, 700, 7)], ASK) == [-500], "bid lowered: - the old bid quantity"
    assert _e(BID, ASK, [(100.00, 650, 6)], ASK) == [150], "same bid, more bought: + the change"
    assert _e(BID, ASK, [(100.00, 420, 4)], ASK) == [-80], "same bid, cancelled or sold into: - the change"
    assert _e(BID, ASK, BID, ASK) == [0], "nothing changed"


def test_e_n_when_the_ask_moves_down_up_or_stays():
    assert _e(BID, ASK, BID, [(100.05, 250, 2)]) == [-250], "ask lowered: - the new ask quantity"
    assert _e(BID, ASK, BID, [(100.15, 900, 9)]) == [300], "ask raised: + the old ask quantity"
    assert _e(BID, ASK, BID, [(100.10, 450, 4)]) == [-150], "same ask, more offered: - the change"
    assert _e(BID, ASK, BID, [(100.10, 100, 1)]) == [200], "same ask, lifted or cancelled: + the change"


def test_both_sides_at_once_equal_the_two_side_messages_one_after_the_other():
    b1, a1 = [(100.05, 200, 2)], [(100.15, 900, 9)]
    both = _e(BID, ASK, b1, a1)
    assert both == [200 + 300]
    assert _e(BID, ASK, b1, ASK)[0] + _e(b1, ASK, b1, a1)[0] == both[0], "bids and asks arrive as separate messages"


def test_e_n_at_deeper_levels_and_levels_that_appear_or_empty():
    b0 = [(100.00, 500, 5), (99.95, 400, 4), (99.90, 300, 3)]
    a0 = [(100.10, 300, 3), (100.15, 200, 2), (100.20, 100, 1)]
    b1 = [(100.00, 500, 5), (99.95, 650, 6), (99.85, 100, 1)]
    assert _e(b0, a0, b1, a0, k=3) == [0, 650 - 400, -300], "level 3's bid fell: - its old quantity"
    a1 = a0[:2]                                                  # the third ask level emptied
    assert _e(b0, a0, b0, a1, k=3) == [0, 0, 100], "an ask level that empties: + its quantity (selling left)"
    assert _e(b0, a1, b0, a0, k=3) == [0, 0, -100], "an ask level that appears: - its quantity"
    assert _e(b0[:2], a0, b0, a0, k=3) == [0, 0, 300], "a bid level that appears: + its quantity"
    assert _e(b0, a0, b0[:2], a0, k=4) == [0, 0, -300, 0], "a level neither book has counts 0"


# ── sums between snapshots and the multi-level normalisation ──────────────

B0 = [(100.00, 500, 5), (99.95, 400, 4), (99.90, 300, 3)]
A0 = [(100.10, 300, 3), (100.15, 200, 2), (100.20, 100, 1)]
B1 = [(100.05, 200, 2), (100.00, 500, 5), (99.95, 400, 4)]          # bid raised a tick
A1 = [(100.10, 450, 4), (100.15, 200, 2), (100.20, 100, 1)]          # more offered at the ask
A2 = [(100.05, 100, 1), (100.10, 450, 4), (100.15, 200, 2)]          # ask lowered a tick
# the updates after the first book, e at levels 1..3, and each book's best-level and per-level depth
STEPS = [((B1, A0), [200, 500, 400], (200 + 300) / 2, (1100 + 600) / 6),
         ((B1, A1), [-150, 0, 0], (200 + 450) / 2, (1100 + 750) / 6),
         ((B0, A1), [-200, -500, -400], (500 + 450) / 2, (1200 + 750) / 6),
         ((B0, A2), [-100, -450, -200], (500 + 100) / 2, (1200 + 750) / 6)]
L1, LM = sum(s[1][0] for s in STEPS), [sum(s[1][m] for s in STEPS) for m in range(3)]
DEPTH1, DEPTHK = sum(s[2] for s in STEPS) / 4, sum(s[3] for s in STEPS) / 4


def test_each_update_against_a_hand_calculation():
    prev = (B0, A0)
    for book, e, _d1, _dk in STEPS:
        assert D.ofi_events(prev, book, 3) == e
        prev = book


def test_sums_between_snapshots_restart_after_each_one():
    o = D.OFI(levels=3)
    assert o.update(B0, A0)
    assert o.take() == {"ofi_l1": None, "ofi_ml": None, "ofi_depth": None, "ofi_updates": None}, \
        "the first snapshot has no interval behind it"
    for (b, a), *_ in STEPS:
        o.update(b, a)
    x = o.take()
    assert x["ofi_l1"] == L1 == -250 and x["ofi_updates"] == 4
    assert x["ofi_depth"] == pytest.approx(DEPTH1) and DEPTH1 == 337.5
    assert x["ofi_ml"] == pytest.approx(sum(LM) / 3 / DEPTHK, abs=1e-4)
    o.update([(100.00, 800, 6)] + B0[1:], A2)
    y = o.take()
    assert y["ofi_l1"] == 300 and y["ofi_updates"] == 1, "only the updates since the last snapshot"
    z = o.take()
    assert z["ofi_l1"] == 0 and z["ofi_updates"] == 0 and z["ofi_ml"] == 0, "no update: no order flow"
    assert z["ofi_depth"] == (800 + 100) / 2, "the depth of the book standing"


def test_a_crossed_or_one_sided_book_is_skipped_not_counted():
    o = D.OFI(levels=1)
    o.update(B0, A0)
    o.take()
    assert not o.update([(100.20, 50, 1)], A0) and not o.update(B0, [])
    o.update(B1, A0)
    x = o.take()
    assert x["ofi_l1"] == 200 and x["ofi_updates"] == 1, "compared with the last good book"


def test_multi_level_ofi_is_scaled_by_the_average_depth_so_stocks_compare():
    def run(scale, levels=3, decay=0.0):
        o = D.OFI(levels=levels, decay=decay)
        sc = [[(p, q * scale, n) for p, q, n in side] for side in (B0, A0)]
        o.update(*sc)
        o.take()
        for (b, a), *_ in STEPS:
            o.update([(p, q * scale, n) for p, q, n in b], [(p, q * scale, n) for p, q, n in a])
        return o.take()
    small, big = run(1), run(400)
    assert big["ofi_l1"] == 400 * small["ofi_l1"] and big["ofi_depth"] == pytest.approx(400 * small["ofi_depth"])
    assert big["ofi_ml"] == pytest.approx(small["ofi_ml"]), "a 400x deeper stock with the same flow: same OFI"
    assert big["ofi_l1"] / big["ofi_depth"] == pytest.approx(small["ofi_l1"] / small["ofi_depth"])
    w = [1, 0.5, 0.25]                                           # decay ln 2
    halved = run(1, decay=math.log(2))
    assert halved["ofi_ml"] == pytest.approx(sum(wi * x for wi, x in zip(w, LM)) / sum(w) / DEPTHK, abs=1e-4)
    deep = run(1, levels=20)
    q20 = sum((sum(q for _p, q, _o in b) + sum(q for _p, q, _o in a)) / 40 for (b, a), *_ in STEPS) / 4
    assert deep["ofi_ml"] == pytest.approx(sum(LM) / 20 / q20, abs=1e-4), "levels a book lacks count as 0 depth"


# ── the frame path the feed uses ──────────────────────────────────────────

@pytest.fixture
def db(temp_db):
    from db.schema import get_connection, init_db
    init_db()
    conn = get_connection()
    D.ensure_tables(conn)
    yield conn
    conn.close()


def _feed(**cfg):
    f = D.Depth20Feed(symbols=["ACME"], cfg=dict(D.DEFAULTS, **cfg))
    f._sid_to_sym = {1333: "ACME"}
    return f


def test_every_frame_counts_toward_the_next_snapshot_and_is_stored(db):
    f = _feed(sample_seconds=15, ofi_levels=3)
    t0 = dt.datetime(2026, 10, 6, 10, 0, 0)
    s = dt.timedelta(seconds=1)
    assert f.consume(_msg(41, 1333, B0) + _msg(51, 1333, A0), t0) == 1
    assert f.consume(_msg(41, 1333, B1), t0 + 3 * s) == 0, "between snapshots, still counted"
    assert f.consume(_msg(51, 1333, A1), t0 + 6 * s) == 0
    assert f.consume(_msg(41, 1333, B0) + _msg(41, 9999, B1), t0 + 9 * s) == 0, "another stock's message ignored"
    assert f.consume(_msg(51, 1333, A2), t0 + 16 * s) == 1
    assert f.consume(_msg(41, 1333, B0) + _msg(51, 1333, A2), t0 + 20 * s) == 0, "unchanged: e = 0"
    assert f.consume(_msg(41, 1333, [(100.00, 800, 6)] + B0[1:]), t0 + 31 * s) == 1
    assert f.flush(db) == 3
    rows = db.execute("SELECT ts, ofi_l1, ofi_ml, ofi_depth, ofi_updates, best_bid, best_bid_qty, best_ask, "
                      "best_ask_qty, dwi FROM depth20_snapshot ORDER BY ts").fetchall()
    assert rows[0][1:5] == (None, None, None, None), "the first snapshot has no interval"
    assert rows[1][1] == L1 and rows[1][2] == pytest.approx(sum(LM) / 3 / DEPTHK, abs=1e-4)
    assert rows[1][3] == pytest.approx(DEPTH1) and rows[1][4] == 4
    assert rows[1][5:9] == (100.00, 500, 100.05, 100), "the best quotes at the snapshot"
    assert rows[1][9] == D.measures(B0, A2)["dwi"], "DWI unchanged beside it"
    assert rows[2][1] == 300 and rows[2][4] == 3, "two unchanged messages and the bid's 300 more"
    r = {x["symbol"]: x for x in D.latest(db, day="2026-10-06")}["ACME"]
    assert r["ofi_l1_norm"] == pytest.approx(300 / ((300 + 300 + 450) / 3), abs=1e-4), "over that interval's depth"


def test_each_message_is_an_update_however_frames_pack_them():
    s = dt.timedelta(seconds=1)
    t0 = dt.datetime(2026, 10, 6, 10, 0, 0)
    a, b, c = (_feed(sample_seconds=15, ofi_levels=3) for _ in range(3))
    for f in (a, b, c):
        f.consume(_msg(41, 1333, B0) + _msg(51, 1333, A0), t0)
    a.consume(_msg(41, 1333, B1) + _msg(51, 1333, A1), t0 + 5 * s)          # one frame, two messages
    b.consume(_msg(41, 1333, B1), t0 + 5 * s)                                # the same, split
    b.consume(_msg(51, 1333, A1), t0 + 6 * s)
    for f in (a, b):
        f.consume(_msg(41, 1333, B1), t0 + 20 * s)
    assert len(a._buffer) == 2 and a._buffer[-1][2] == b._buffer[-1][2]
    assert a._buffer[-1][2]["ofi_l1"] == 200 - 150 and a._buffer[-1][2]["ofi_updates"] == 3
    # a snapshot falls between a frame's messages: the rest of the frame counts toward the next one
    c.consume(_msg(41, 1333, B1) + _msg(51, 1333, A1), t0 + 20 * s)
    c.consume(_msg(41, 1333, B1), t0 + 40 * s)
    assert [x["ofi_l1"] for _s, _t, x in c._buffer] == [None, 200, -150]
    assert c._buffer[1][2]["best_ask_qty"] == A0[0][1], "the snapshot is the book its OFI ran up to"


def test_a_new_connection_restarts_the_sums(db):
    class FakeWS:
        def __init__(self, frames):
            self.sent, self.frames = [], frames

        async def send(self, m):
            self.sent.append(m)

        def __aiter__(self):
            async def gen():
                for fr in self.frames:
                    yield fr
            return gen()
    f = _feed(sample_seconds=0)
    f._running = True
    asyncio.run(f.session(FakeWS([_msg(41, 1333, B0), _msg(51, 1333, A0), _msg(41, 1333, B1)])))
    assert [x["ofi_l1"] for _s, _t, x in f._buffer] == [None, 200]
    f._buffer = []
    asyncio.run(f.session(FakeWS([_msg(41, 1333, B0), _msg(51, 1333, A0)])))
    assert [x["ofi_l1"] for _s, _t, x in f._buffer] == [None], \
        "no quote change counted across the reconnect, and no stale side kept"


# ── storage on a table from before OFI ────────────────────────────────────

OLD_DDL = """CREATE TABLE depth20_snapshot (
    symbol TEXT NOT NULL, ts TIMESTAMP NOT NULL, mid REAL, spread_bp REAL, dwi REAL, imb_l1 REAL, imb_20 REAL,
    bid_qty REAL, ask_qty REAL, levels INTEGER, PRIMARY KEY (symbol, ts))"""


def test_an_old_table_gets_the_new_columns_and_keeps_its_rows(temp_db):
    import db.schema as S
    raw = sqlite3.connect(S.DB_PATH)
    raw.execute(OLD_DDL)
    raw.execute("INSERT INTO depth20_snapshot (symbol, ts, mid, dwi) VALUES ('OLD', '2026-10-05 10:00:00', 50, 0.2)")
    raw.commit()
    raw.close()
    S.init_db()                                                   # db/schema_w39.W39_COLUMNS
    conn = S.get_connection()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(depth20_snapshot)")}
    assert set(D.ADDED_COLUMNS["depth20_snapshot"]) <= cols
    assert [tuple(r) for r in conn.execute("SELECT symbol, mid, dwi, ofi_l1 FROM depth20_snapshot")] \
        == [("OLD", 50, 0.2, None)], "the old row kept"
    f = _feed(ofi_levels=3)
    t0 = dt.datetime(2026, 10, 6, 10, 0, 0)
    f.consume(_msg(41, 1333, B0) + _msg(51, 1333, A0), t0)
    f.consume(_msg(41, 1333, B1), t0 + dt.timedelta(seconds=20))
    assert f.flush(conn) == 2
    assert [tuple(r) for r in conn.execute("SELECT ofi_l1, ofi_updates FROM depth20_snapshot WHERE symbol='ACME' "
                                           "ORDER BY ts")] == [(None, None), (200, 1)]
    conn.close()
    raw = sqlite3.connect(S.DB_PATH)                              # and the module's own ensure_tables, alone
    raw.execute("DROP TABLE depth20_snapshot")
    raw.execute(OLD_DDL)
    D.ensure_tables(raw)
    assert set(D.ADDED_COLUMNS["depth20_snapshot"]) <= {r[1] for r in raw.execute("PRAGMA table_info(depth20_snapshot)")}
    raw.close()


def test_off_by_default_nothing_starts(monkeypatch):
    monkeypatch.setattr(D, "settings", lambda: dict(D.DEFAULTS))
    monkeypatch.setattr(D, "_feed", None)
    assert D.DEFAULTS["enabled"] is False
    assert D.start_depth20() is None and D._feed is None and D.feed_status()["running"] is False


# ── validation ────────────────────────────────────────────────────────────

def _row(conn, sym, ts, mid, ofi_l1=None, ofi_depth=None, ofi_ml=None, dwi=0.0):
    conn.execute("INSERT INTO depth20_snapshot (symbol, ts, mid, dwi, imb_l1, imb_20, ofi_l1, ofi_depth, ofi_ml) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (sym, ts.strftime("%Y-%m-%d %H:%M:%S"), mid, dwi, 0.0, 0.0, ofi_l1,
                                                ofi_depth, ofi_ml))


def _plant(conn, rng, stocks, snaps, b_now=3.0, b_next=2.5, depths=(800, 5000, 40000, 250000)):
    """Per stock, one snapshot a minute. The mid's move into snapshot i rises more often with the best-level OFI over
    that same minute (b_now) and with the previous snapshot's multi-level OFI (b_next). Depths differ 300x."""
    t0 = dt.datetime.now().replace(second=0, microsecond=0) - dt.timedelta(days=1)
    for s in range(stocks):
        depth = depths[s % len(depths)]
        l1, ml = rng.uniform(-1, 1, snaps), rng.uniform(-1, 1, snaps)
        mid = 100.0
        for i in range(snaps):
            if i:
                p_up = 1 / (1 + math.exp(-(b_now * l1[i] + b_next * ml[i - 1])))
                mid = round(mid * (1.0005 if rng.random() < p_up else 0.9995), 4)
            _row(conn, f"S{s}", t0 + dt.timedelta(minutes=i), mid, float(l1[i] * depth), depth, float(ml[i]))
    conn.commit()


def test_validation_recovers_a_planted_ofi_relation_and_calls_noise_not_predictive(db):
    _plant(db, np.random.default_rng(7), stocks=4, snaps=260)
    t = dt.datetime.now().replace(second=0, microsecond=0) - dt.timedelta(days=1)
    mid = 100.0
    for i in range(60):                                          # a stock stored before OFI: DWI only
        mid = round(mid * (1.0005 if i % 3 else 0.9995), 4)
        _row(db, "OLD", t + dt.timedelta(minutes=i), mid, dwi=0.1 * (i % 5))
    db.commit()
    out = D.validate(db, horizon_minutes=1)
    m, c = out["measures"], out["contemporaneous"]["measures"]
    assert out["n"] == 4 * 259 + 59 and m["ofi_l1"]["n"] == m["ofi_ml"]["n"] == 4 * 259 and m["dwi"]["n"] == out["n"]
    assert m["ofi_ml"]["predictive"] and m["ofi_ml"]["z"] > 5, "the planted next-minute effect"
    assert not m["ofi_l1"]["predictive"] and abs(m["ofi_l1"]["z"]) < 3, "this minute's flow says nothing of the next"
    assert c["ofi_l1"]["related"] and c["ofi_l1"]["z"] > 8 and c["ofi_l1"]["n"] == 4 * 259
    assert 1.5 < c["ofi_l1"]["slope"] < 4, "scale-free: the same slope for stocks 300x apart in depth"
    assert c["ofi_l1"]["bp_per_unit"] > 0 and c["ofi_l1"]["r2"] > 0.1
    assert not c["ofi_ml"]["related"] and abs(c["ofi_ml"]["z"]) < 3 and c["ofi_ml"]["r2"] < 0.02, "noise"
    assert out["contemporaneous"]["pairs"] == 4 * 259, "the pre-OFI stock adds no pair"


def test_a_strong_relation_on_too_few_pairs_is_not_called(db):
    _plant(db, np.random.default_rng(11), stocks=2, snaps=150, b_now=4.0, b_next=4.0)
    out = D.validate(db, horizon_minutes=1)
    c, m = out["contemporaneous"]["measures"]["ofi_l1"], out["measures"]["ofi_ml"]
    assert c["n"] < D.MIN_VALIDATE and c["z"] >= D.Z_MIN and not c["related"]
    assert m["n"] < D.MIN_VALIDATE and m["z"] >= D.Z_MIN and not m["predictive"]


def test_pure_noise_is_not_predictive_or_related(db):
    _plant(db, np.random.default_rng(5), stocks=4, snaps=260, b_now=0.0, b_next=0.0)
    out = D.validate(db, horizon_minutes=1)
    for k in D.OFI_MEASURES:
        assert not out["measures"][k]["predictive"] and abs(out["measures"][k]["z"]) < 2.5
        assert not out["contemporaneous"]["measures"][k]["related"]
        assert abs(out["contemporaneous"]["measures"][k]["z"]) < 2.5


def test_a_measure_that_separates_the_moves_gets_the_score_z_not_a_collapsed_one():
    x = np.linspace(-1, 1, 600)
    up = (x > 0).astype(float)
    f = D._fit(x, up)
    r = float(np.corrcoef(x, up)[0, 1])
    assert f["separated"] and f["slope"] is None and f["predictive"], "the Wald z would be ~0 here"
    assert f["z"] == pytest.approx(math.sqrt(600) * r, abs=0.01) and f["sign_right_pct"] == 100.0
    g = D._fit(x, 1 - up)
    assert g["separated"] and g["z"] < -D.Z_MIN and not g["predictive"], "the wrong way round"
    few = x[:200]
    h = D._fit(few, (few > few.mean()).astype(float))
    assert h["separated"] and h["z"] > D.Z_MIN and not h["predictive"], "still needs 500 pairs"
    noisy = D._fit(x, ((x + np.random.default_rng(2).normal(0, 0.5, 600)) > 0).astype(float))
    assert not noisy["separated"] and noisy["slope"] > 0 and noisy["predictive"], "an ordinary fit as before"


class _Book:
    """A Cont-de Larrard style book, one tick wide, 20 levels a side: market orders and cancels take from the best
    queues, limit orders add to them; a queue that empties moves the price a tick and a fresh queue takes its place."""
    T = 0.05

    def __init__(self, rng, pb):
        self.rng, self.pb = rng, pb
        self.bq = [self._fresh() for _ in range(20)]
        self.aq = [self._fresh() for _ in range(20)]

    def _fresh(self):
        return int(self.rng.integers(100, 400))

    def levels(self):
        return ([(round(self.pb - m * self.T, 2), q, 1) for m, q in enumerate(self.bq)],
                [(round(self.pb + (m + 1) * self.T, 2), q, 1) for m, q in enumerate(self.aq)])

    def step(self) -> str:
        e, size = int(self.rng.integers(0, 8)), int(self.rng.integers(50, 250))
        if e in (0, 5):                                              # a buy takes, or the offer is cancelled
            self.aq[0] -= size
            if self.aq[0] <= 0:
                self.pb = round(self.pb + self.T, 2)
                self.aq, self.bq = self.aq[1:] + [self._fresh()], [self._fresh()] + self.bq[:-1]
                return "both"
            return "ask"
        if e in (1, 4):
            self.bq[0] -= size
            if self.bq[0] <= 0:
                self.pb = round(self.pb - self.T, 2)
                self.bq, self.aq = self.bq[1:] + [self._fresh()], [self._fresh()] + self.aq[:-1]
                return "both"
            return "bid"
        if e == 2:
            self.bq[0] += size
            return "bid"
        if e == 3:
            self.aq[0] += size
            return "ask"
        side = self.bq if self.rng.random() < 0.5 else self.aq       # deeper levels: added or cancelled
        m = int(self.rng.integers(1, 20))
        side[m] = side[m] + size if e == 6 else max(1, side[m] - size)
        return "bid" if side is self.bq else "ask"


def test_a_simulated_book_shows_ofis_strong_contemporaneous_relation(db):
    """Fed frame by frame through the feed's own parse / sample path: the mid's move over each 15 s is explained
    by that interval's OFI (Cont, Kukanov & Stoikov's relation), and the sanity check says so."""
    rng = np.random.default_rng(1)
    f = D.Depth20Feed(symbols=["AAA", "BBB"], cfg=dict(D.DEFAULTS))
    f._sid_to_sym = {1: "AAA", 2: "BBB"}
    books = {1: _Book(rng, 100.0), 2: _Book(rng, 2500.0)}
    t0 = dt.datetime.now().replace(second=0, microsecond=0) - dt.timedelta(days=1)
    for sid, b in books.items():
        bids, asks = b.levels()
        f.consume(_msg(41, sid, bids) + _msg(51, sid, asks), t0)
    for i in range(1, 9000):
        for sid, b in books.items():
            side = b.step()
            bids, asks = b.levels()
            frame = (_msg(41, sid, bids) if side in ("bid", "both") else b"") + \
                    (_msg(51, sid, asks) if side in ("ask", "both") else b"")
            f.consume(frame, t0 + dt.timedelta(seconds=i))
    assert f.frames == 2 + 2 * 8999 and f.flush(db) == f.snapshots
    c = D.validate(db, horizon_minutes=1)["contemporaneous"]["measures"]
    assert c["ofi_l1"]["related"] and c["ofi_l1"]["z"] > 10 and c["ofi_l1"]["r2"] > 0.3, c["ofi_l1"]
    assert c["ofi_ml"]["related"], c["ofi_ml"]


# ── API and page ──────────────────────────────────────────────────────────

def test_ofi_in_the_api_and_on_the_page(tmp_path, monkeypatch, db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    client = TestClient(server.app)
    _row(db, "ACME", dt.datetime.combine(dt.date.today(), dt.time(10, 0)), 100, ofi_l1=-250, ofi_depth=500, ofi_ml=-0.4)
    db.commit()
    r = client.get("/api/orderbook/depth20").json()["rows"][0]
    assert r["ofi_l1"] == -250 and r["ofi_l1_norm"] == -0.5 and r["ofi_ml"] == -0.4
    v = client.get("/api/orderbook/depth20/validation").json()
    assert set(D.OFI_MEASURES) <= set(v["measures"]) and set(v["contemporaneous"]["measures"]) == set(D.OFI_MEASURES)
    page = client.get("/market-pulse").text
    assert "order-flow imbalance" in page and "OFI 20" in page and "contemporaneous" in page
