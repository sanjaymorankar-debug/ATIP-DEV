"""
W39 Phase 3 (item 2): Dhan's 20-level depth -- the binary frame layout of dhanhq's fulldepth.py, the
depth-weighted imbalance against a hand calculation, snapshot sampling and storage, Dhan's refusal codes,
the persistent flag, and a validation that finds a planted effect and not a random one. Nothing connects
to Dhan.
"""
import asyncio
import datetime as dt
import math
import struct

import numpy as np
import pytest

from data import depth20 as D


def _msg(code, sid, levels, seg=1, extra=0):
    body = b"".join(struct.pack("<dII", p, q, o) for p, q, o in levels)
    body += b"\0" * (16 * (20 - len(levels)))
    return struct.pack("<hBBiI", 12 + len(body), code, seg, sid, extra) + body


BIDS = [(100.00, 500, 5), (99.95, 400, 4), (99.90, 300, 3), (99.20, 9000, 9)]       # the last is 76 bp away
ASKS = [(100.05, 200, 2), (100.10, 300, 3), (100.15, 100, 1)]


# ── frames and measures ───────────────────────────────────────────────────

def test_a_frame_with_bid_and_ask_messages_and_a_truncated_tail():
    frame = _msg(41, 1333, BIDS) + _msg(51, 1333, ASKS) + b"\x01\x02\x03"
    out = D.parse_frame(frame)
    assert [(m["security_id"], m["side"], len(m["levels"])) for m in out] == [(1333, "bid", 4), (1333, "ask", 3)]
    assert out[0]["levels"][0] == (100.0, 500, 5), "empty levels (price 0) dropped"
    assert D.parse_frame(_msg(41, 1333, BIDS)[:200]) == [], "a message cut short is ignored"
    assert D.parse_frame(struct.pack("<hBBiI", 12, 50, 1, 0, 806)) == [{"disconnect": 806}]


def test_dwi_weights_levels_near_the_mid_and_ignores_far_ones():
    x = D.measures(BIDS, ASKS, band_bp=50, decay=0.5)
    w = [math.exp(-0.5 * k) for k in range(3)]
    wb = 500 * w[0] + 400 * w[1] + 300 * w[2]                  # 99.20 is beyond 50 bp of 100.025
    wa = 200 * w[0] + 300 * w[1] + 100 * w[2]
    assert x["dwi"] == pytest.approx((wb - wa) / (wb + wa), abs=1e-4)
    assert x["imb_l1"] == pytest.approx((500 - 200) / 700, abs=1e-4)
    assert x["imb_20"] == pytest.approx((10200 - 600) / 10800, abs=1e-4), "all levels, the far one included"
    assert x["mid"] == pytest.approx(100.025) and x["spread_bp"] == pytest.approx(5.0, abs=0.01)
    assert D.measures(BIDS, []) is None and D.measures([(101, 5, 1)], [(100.5, 5, 1)]) is None, "crossed book"


# ── the feed's consumer ───────────────────────────────────────────────────

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


def test_snapshots_are_sampled_and_stored(db):
    f = _feed(sample_seconds=15)
    t0 = dt.datetime(2026, 10, 6, 10, 0, 0)
    assert f.consume(_msg(41, 1333, BIDS), t0) == 0, "needs both sides"
    assert f.consume(_msg(51, 1333, ASKS), t0) == 1
    assert f.consume(_msg(41, 1333, BIDS) + _msg(51, 1333, ASKS), t0 + dt.timedelta(seconds=5)) == 0, "within 15 s"
    assert f.consume(_msg(41, 1333, BIDS), t0 + dt.timedelta(seconds=16)) == 1
    assert f.consume(_msg(41, 9999, BIDS), t0 + dt.timedelta(seconds=40)) == 0, "not subscribed"
    assert f.flush(db) == 2 and f.flush(db) == 0
    rows = db.execute("SELECT symbol, ts, dwi, levels FROM depth20_snapshot ORDER BY ts").fetchall()
    assert [r[0] for r in rows] == ["ACME", "ACME"] and str(rows[0][1])[:19] == "2026-10-06 10:00:00"
    assert rows[0][3] == 3
    sub = __import__("json").loads(f.subscriptions()[0])
    assert sub == {"RequestCode": 23, "InstrumentCount": 1, "InstrumentList": [{"ExchangeSegment": "NSE_EQ",
                                                                                 "SecurityId": "1333"}]}


def test_dhans_refusal_parks_the_feed_with_the_reason():
    f = _feed()
    with pytest.raises(D.FeedRefused) as e:
        f.consume(struct.pack("<hBBiI", 12, 50, 1, 0, 806))
    assert e.value.code == 806 and "Data APIs" in str(e.value)


def test_a_session_subscribes_then_reads_frames(db, monkeypatch):
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
    f = _feed()
    f._running = True
    ws = FakeWS([_msg(41, 1333, BIDS), _msg(51, 1333, ASKS), "a text frame"])
    asyncio.run(f.session(ws))
    assert len(ws.sent) == 1 and f.snapshots == 1 and f.mode == "LIVE"
    assert f.flush(db) == 1


def test_batches_of_50_per_subscription():
    f = _feed()
    f._sid_to_sym = {i: f"S{i}" for i in range(1, 121)}
    assert [__import__("json").loads(m)["InstrumentCount"] for m in f.subscriptions()] == [50, 50, 20]


# ── flags and validation ──────────────────────────────────────────────────

def _snap(conn, sym, ts, mid, dwi, l1=0.0, l20=0.0):
    conn.execute("INSERT INTO depth20_snapshot (symbol, ts, mid, dwi, imb_l1, imb_20) VALUES (?,?,?,?,?,?)",
                 (sym, ts.strftime("%Y-%m-%d %H:%M:%S"), mid, dwi, l1, l20))


def test_persistent_needs_three_snapshots_beyond_the_threshold(db):
    t = dt.datetime.combine(dt.date.today(), dt.time(10, 0))
    for i, (a, b) in enumerate([(0.5, 0.2), (0.4, -0.5), (0.35, -0.6), (0.6, -0.7)]):
        _snap(db, "UP", t + dt.timedelta(minutes=i), 100, a)
        _snap(db, "MIX", t + dt.timedelta(minutes=i), 100, b)
    _snap(db, "DOWN", t, 100, -0.4)
    db.commit()
    assert D.persistent(db) == {"UP": "BUYERS", "MIX": "SELLERS"}, "MIX: its last three are below -0.3"
    rows = {r["symbol"]: r for r in D.latest(db)}
    assert rows["UP"]["persistent"] == "BUYERS" and rows["DOWN"]["persistent"] is None


def test_validation_finds_a_planted_effect_and_not_noise(db):
    rng = np.random.default_rng(3)
    t0 = dt.datetime.now().replace(second=0, microsecond=0) - dt.timedelta(days=1)
    for s in range(4):
        mid, t = 100.0, t0
        for i in range(260):
            dwi = float(rng.uniform(-1, 1))
            noise = float(rng.uniform(-1, 1))
            _snap(db, f"S{s}", t, mid, dwi, l20=noise)
            p_up = 1 / (1 + math.exp(-2.5 * dwi))                   # the next minute rises more often with DWI
            mid = round(mid * (1.0005 if rng.random() < p_up else 0.9995), 4)
            t += dt.timedelta(minutes=1)
    db.commit()
    out = D.validate(db, horizon_minutes=1)
    assert out["n"] == 4 * 259
    assert out["measures"]["dwi"]["predictive"] and out["measures"]["dwi"]["z"] > 5
    assert out["measures"]["dwi"]["slope"] == pytest.approx(2.5, abs=0.6), "recovers the planted slope"
    assert not out["measures"]["imb_20"]["predictive"] and abs(out["measures"]["imb_20"]["z"]) < 3
    with pytest.raises(ValueError):
        D.validate(db, horizon_minutes=3)


# ── API and page ──────────────────────────────────────────────────────────

def test_depth_api_and_permissions(tmp_path, monkeypatch, db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from dashboard import security, server
    from enterprise.authz import permission_for
    from enterprise.privacy import classify
    from enterprise.scoping import TABLES
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    client = TestClient(server.app)
    _snap(db, "ACME", dt.datetime.combine(dt.date.today(), dt.time(10, 0)), 100, 0.45)
    db.commit()
    o = client.get("/api/orderbook/depth20").json()
    assert o["rows"][0]["symbol"] == "ACME" and o["feed"]["running"] is False and "off" in o["feed"]["detail"]
    v = client.get("/api/orderbook/depth20/validation", params={"horizon": 5})
    assert v.status_code == 200 and v.json()["horizon_minutes"] == 5
    assert client.get("/api/orderbook/depth20/validation", params={"horizon": 2}).status_code == 400
    assert permission_for("GET", "/api/orderbook/depth20") == "dashboard:read"
    assert classify("depth20_snapshot") is not None and TABLES["depth20_snapshot"] == "GLOBAL"
    assert "20-level depth" in client.get("/market-pulse").text
