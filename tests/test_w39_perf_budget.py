"""W39 performance budget -- the development side of Wave 18 (Full QA / Security / Performance).

tools/load_test.py drives read-only GET load at the dashboard and reports per-route error rate
and p50 / p95 / p99 / max latency. Here:

  * its statistics are checked on hand-worked samples (nearest-rank percentiles, error
    classification, target comparison, the round-robin scheduler);
  * the in-process mode runs briefly on the seeded W18 market (tests/_wealth_seed.py) and must
    produce no server error on any default route and stay inside a deliberately generous
    ceiling (p95 < 3 s), so a slow CI runner does not flake -- the real targets are the tool's
    own and are reported in docs/W39_QA_PERFORMANCE.md;
  * the W33 §2 alerts_intraday target (< 2 s for 1,000 rules) is checked as the doc states it;
  * server mode is exercised over real HTTP against uvicorn, and the CLI's exit codes.

Outbound network is refused for the duration (load_test.block_network), so nothing here can
reach NSE, yfinance or a broker.
"""

import json
import os
import random
import socket
import sys
import threading
import time

import pytest

from tools import load_test as LT

CEILING_MS = 3000.0          # generous on purpose: this guards against a pathological regression, not jitter


# ── statistics, by hand ──────────────────────────────────────────────────────

def test_percentile_is_nearest_rank():
    tens = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    assert [LT.percentile(tens, p) for p in (0, 10, 50, 90, 95, 99, 100)] == [10, 10, 50, 90, 100, 100, 100]
    hundred = list(range(1, 101))
    random.Random(7).shuffle(hundred)                         # order of arrival does not matter
    assert [LT.percentile(hundred, p) for p in (50, 95, 99)] == [50, 95, 99]
    assert LT.percentile([4, 1, 3, 2], 50) == 2               # ceil(0.5 * 4) = 2nd smallest
    assert LT.percentile([4, 1, 3, 2], 75) == 3 and LT.percentile([4, 1, 3, 2], 76) == 4
    assert LT.percentile([7.5], 99) == 7.5
    assert LT.percentile([], 50) is None
    for bad in (-1, 100.1):
        with pytest.raises(ValueError):
            LT.percentile(tens, bad)


def _s(route, status, ms, error=None):
    return LT.Sample(route, status, ms / 1000.0, error)


def test_route_stats_count_5xx_and_failures_as_errors_but_not_4xx():
    samples = [_s("/a", 200, 10), _s("/a", 404, 20), _s("/a", 500, 30), _s("/a", None, 40, "ConnectError: refused")]
    st = LT.route_stats(samples)
    assert (st["count"], st["errors"], st["error_rate"], st["non_2xx"]) == (4, 2, 0.5, 3)
    assert st["statuses"] == {"200": 1, "404": 1, "500": 1, "None": 1}
    assert (st["p50_ms"], st["p95_ms"], st["p99_ms"], st["max_ms"], st["mean_ms"]) == (20.0, 40.0, 40.0, 40.0, 25.0)
    assert [LT.is_error(s) for s in samples] == [False, False, True, True]


def test_summarize_groups_by_route_and_reports_throughput():
    samples = [_s("/a", 200, ms) for ms in (5, 15, 25, 35)] + [_s("/b", 503, ms) for ms in (100, 300)]
    rep = LT.summarize(samples, wall_seconds=2.0)
    assert sorted(rep["routes"]) == ["/a", "/b"]
    a, b, o = rep["routes"]["/a"], rep["routes"]["/b"], rep["overall"]
    assert (a["count"], a["errors"], a["p50_ms"], a["p95_ms"], a["mean_ms"]) == (4, 0, 15.0, 35.0, 20.0)
    assert (b["count"], b["errors"], b["error_rate"], b["p50_ms"], b["max_ms"]) == (2, 2, 1.0, 100.0, 300.0)
    # overall over [5, 15, 25, 35, 100, 300]: p50 = 3rd = 25, p95 = ceil(5.7) = 6th = 300; 6 requests / 2 s
    assert (o["count"], o["errors"], o["p50_ms"], o["p95_ms"], o["rps"], o["wall_s"]) == (6, 2, 25.0, 300.0, 3.0, 2.0)
    assert LT.summarize([])["overall"]["count"] == 0


def test_check_targets_merges_route_overrides_and_passes_on_equality():
    def st(p50=10.0, p95=10.0, err=0.0):
        return {"error_rate": err, "p50_ms": p50, "p95_ms": p95, "p99_ms": p95, "max_ms": p95}
    report = {"routes": {"/equal": st(p95=1000.0), "/over": st(p95=1000.01), "/errs": st(err=0.25),
                         "/api/wealth/overview": st(p50=301.0, p95=400.0),        # W18 §3: p50 <= 300 ms
                         "/api/wealth/summary": st(p50=300.0, p95=400.0)},
              "functions": {"alerts_intraday": {"ms": 2500.0, "target_ms": 2000.0},
                            "fast": {"ms": 5.0, "target_ms": 2000.0}}}
    misses = {(m["route"], m["metric"]) for m in LT.check_targets(report)}
    assert misses == {("/over", "p95_ms"), ("/errs", "max_error_rate"), ("/api/wealth/overview", "p50_ms"),
                      ("function:alerts_intraday", "max_ms")}
    assert LT.targets_for("/api/wealth/overview") == {"max_error_rate": 0.0, "p95_ms": 1000.0, "p50_ms": 300.0}
    # a looser default from the command line, and None switching a target off
    assert LT.check_targets({"routes": {"/over": st(p95=1500.0)}}, {"p95_ms": 2000.0}) == []
    assert LT.check_targets({"routes": {"/over": st(p95=1500.0)}}, {"p95_ms": None}) == []


def test_targets_cite_their_documents():
    assert LT.FUNCTION_TARGETS["alerts_intraday"] == {"max_ms": 2000.0, "rules": 1000}
    assert set(LT.TARGET_SOURCES) >= {"max_error_rate", "p95_ms", "p50_ms", "alerts_intraday"}
    assert "W33" in LT.TARGET_SOURCES["alerts_intraday"] and "W18" in LT.TARGET_SOURCES["p50_ms"]
    # the default route set is read-only and leaves out what measures another system
    assert all(r.startswith("/") for r in LT.DEFAULT_ROUTES) and len(set(LT.DEFAULT_ROUTES)) == len(LT.DEFAULT_ROUTES)
    for excluded in ("/api/live-quotes", "/api/schemas/check-all", "/api/platform/postgres", "/api/zerodha/status"):
        assert excluded not in LT.DEFAULT_ROUTES


# ── the scheduler ────────────────────────────────────────────────────────────

def test_run_load_round_robins_routes_and_records_failures():
    calls, threads, lock = [], set(), threading.Lock()

    def fetch(path):
        with lock:
            calls.append(path)
            threads.add(threading.current_thread().name)
        if path == "/boom":
            raise ConnectionError("refused")
        return 404 if path == "/missing" else 200

    routes = ["/ok", "/api/stock/{sym}/history", "/boom", "/missing"]
    samples, wall = LT.run_load(fetch, routes, requests=40, concurrency=3, warmup=True, params={"sym": "ACME"})
    assert len(calls) == 44, "4 warm-up calls are made but not recorded"
    assert len(samples) == 40 and wall > 0
    per = {r: [s for s in samples if s.route == r] for r in routes}
    assert {r: len(v) for r, v in per.items()} == {r: 10 for r in routes}
    assert "/api/stock/ACME/history" in calls and "/api/stock/{sym}/history" not in calls
    assert all(s.status is None and s.error == "ConnectionError: refused" for s in per["/boom"])
    assert all(s.status == 404 and not LT.is_error(s) for s in per["/missing"])
    assert 1 <= len(threads - {threading.current_thread().name}) <= 3
    rep = LT.summarize(samples, wall)
    assert rep["routes"]["/boom"]["error_rate"] == 1.0 and rep["overall"]["errors"] == 10


def test_run_load_stops_at_the_duration_and_validates_arguments():
    def slow(path):
        time.sleep(0.01)
        return 200
    t0 = time.monotonic()
    samples, wall = LT.run_load(slow, ["/x"], duration=0.3, concurrency=2, warmup=False)
    assert 0.25 < wall < 2.0 and time.monotonic() - t0 < 2.5
    assert 5 <= len(samples) <= 70                          # 2 workers x ~30 requests of 10 ms
    one, _ = LT.run_load(slow, ["/x"], requests=3, duration=60, concurrency=8, warmup=False)
    assert len(one) == 3, "the request count stops a long duration"
    for kw in ({}, {"requests": 0}, {"duration": 0}):
        with pytest.raises(ValueError):
            LT.run_load(slow, ["/x"], **kw)
    with pytest.raises(ValueError):
        LT.run_load(slow, [], requests=1)


def test_block_network_refuses_outside_hosts_and_keeps_loopback():
    real = socket.socket.connect
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        with LT.block_network():
            with pytest.raises(OSError):
                socket.create_connection(("192.0.2.1", 80), timeout=1)        # TEST-NET-1, never routable
            with pytest.raises(socket.gaierror):
                socket.getaddrinfo("archives.nseindia.com", 443)
            with socket.create_connection(srv.getsockname(), timeout=2):
                pass                                                         # loopback still works
            requests = pytest.importorskip("requests")
            t = time.monotonic()
            with pytest.raises(requests.exceptions.RequestException):
                requests.get("https://archives.nseindia.com/content/indices/ind_nifty500list.csv", timeout=20)
            assert time.monotonic() - t < 5, "a blocked request fails at once, not after its timeout"
    assert socket.socket.connect is real


# ── in-process run on the seeded database ────────────────────────────────────

@pytest.fixture
def seeded(temp_db, tmp_path, monkeypatch):
    """The W18 seed plus 10 synthetic stocks in temp_db; cwd = tmp_path so atip_data/* (state
    cache, Nifty 500 list, token, config) lands there; network blocked."""
    pytest.importorskip("fastapi")
    from dashboard import security
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(security, "TOKEN_PATH", tmp_path / "dashboard_token.txt")
    monkeypatch.setattr(security, "CONFIG_PATH", tmp_path / "config.json")
    with LT.block_network():
        info = LT.seed(tmp_path, extra_symbols=10)
        yield info


def test_seed_is_the_w18_market_plus_the_extra_stocks(seeded):
    from db.schema import get_connection
    assert seeded["sessions"] == 260 and seeded["symbols"] == ["ACME"] + [f"LOAD{i:03d}" for i in range(10)]
    c = get_connection()
    try:
        assert c.execute("SELECT COUNT(*) FROM ai_scores WHERE date=?", (seeded["last_session"],)).fetchone()[0] == 11
        assert c.execute("SELECT COUNT(*) FROM prices_daily WHERE symbol LIKE 'LOAD%'").fetchone()[0] == 2600
    finally:
        c.close()
    assert (os.path.exists("atip_data/raw/ind_nifty500list.csv"))


def test_in_process_load_has_no_server_error_and_stays_under_the_ceiling(seeded):
    fetch = LT.testclient_fetcher()
    try:
        n = 2 * len(LT.DEFAULT_ROUTES)
        samples, wall = LT.run_load(fetch, LT.DEFAULT_ROUTES, requests=n, concurrency=2, params={"sym": "ACME"})
    finally:
        fetch.close()
    rep = LT.summarize(samples, wall)
    print("\n" + LT.format_report(rep, LT.check_targets(rep), "in-process budget run (targets informational)"))
    assert rep["overall"]["count"] == n
    assert {r: st["count"] for r, st in rep["routes"].items()} == {r: 2 for r in LT.DEFAULT_ROUTES}
    assert [(s.route, s.status, s.error) for s in samples if LT.is_error(s)] == [], "a read returned 5xx"
    assert [(s.route, s.status) for s in samples if s.status != 200] == [], "every default route answers 200"
    assert rep["overall"]["p95_ms"] < CEILING_MS
    slow = {r: st["max_ms"] for r, st in rep["routes"].items() if st["max_ms"] >= CEILING_MS}
    assert slow == {}, f"routes over {CEILING_MS:.0f} ms: {slow}"


def test_w33_alerts_intraday_meets_its_documented_target(seeded):
    b = LT.bench_alerts_intraday(rules=1000, symbols=100, quotes_per_symbol=60)
    assert (b["rules"], b["triggered"], b["quotes"]) == (1000, 0, 6000)
    assert b["ms"] < LT.FUNCTION_TARGETS["alerts_intraday"]["max_ms"]          # W33 §2: < 2 s for 1,000 rules
    from db.schema import get_connection
    c = get_connection()
    try:                                                    # every rule was evaluated: its last value recorded
        assert c.execute("SELECT COUNT(*) FROM enterprise_alert_rule WHERE last_value IS NOT NULL").fetchone()[0] == 1000
    finally:
        c.close()


def test_server_mode_over_real_http(seeded, monkeypatch, tmp_path):
    pytest.importorskip("uvicorn")
    from dashboard import server
    with LT.serve(server.app) as base:
        fetch = LT.http_fetcher(base)
        try:
            routes = ["/", "/wealth", "/api/scores", "/api/stock/{sym}/history", "/api/wealth/overview", "/health"]
            samples, wall = LT.run_load(fetch, routes, requests=12, concurrency=3, params={"sym": "ACME"})
        finally:
            fetch.close()
        assert sorted({s.status for s in samples}) == [200] and len(samples) == 12
        monkeypatch.setitem(sys.modules, "httpx", None)              # the urllib fallback
        plain = LT.http_fetcher(base)
        assert (plain("/health"), plain("/api/no-such-route")) == (200, 404)
        # the CLI against the running server: {sym} resolved from /api/scores (highest score first:
        # LOAD004 at 88 beats ACME at 72 -- see load_test.seed), report written, targets checked
        out = tmp_path / "server.json"
        code = LT.main(["--base-url", base, "--route", "/health", "--route", "/api/stock/{sym}/history",
                        "--requests", "6", "--concurrency", "2", "--p95-ms", str(CEILING_MS), "--json", str(out)])
        rep = json.loads(out.read_text(encoding="utf-8"))
        assert code == 0 and rep["misses"] == [] and rep["symbol"] == "LOAD004"
        assert rep["routes"]["/api/stock/{sym}/history"]["statuses"] == {"200": 3}


def test_cli_exit_codes(tmp_path, capsys):
    port = LT.free_port()                                       # nothing listens there
    assert LT.main(["--base-url", f"http://127.0.0.1:{port}", "--requests", "1"]) == 2
    assert "cannot reach" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        LT.main(["--header", "no-colon-here"])
    with pytest.raises(SystemExit):
        LT.main(["--serve"])                                    # --serve needs --in-process


def test_cli_in_process_run_fails_an_impossible_target_and_restores_everything(temp_db, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import db.schema as schema
    from dashboard import security
    monkeypatch.chdir(tmp_path)
    before = (os.getcwd(), schema.DB_PATH, security.TOKEN_PATH, security.CONFIG_PATH, socket.socket.connect)
    out = tmp_path / "report.json"
    routes = ["/health", "/api/scores", "/api/wealth/summary"]
    argv = ["--in-process", "--requests", "6", "--concurrency", "2", "--extra-symbols", "3", "--p95-ms", "0.0001",
            "--json", str(out)]
    for r in routes:
        argv += ["--route", r]
    assert LT.main(argv) == 1                                   # nothing answers in 0.1 microseconds
    assert (os.getcwd(), schema.DB_PATH, security.TOKEN_PATH, security.CONFIG_PATH, socket.socket.connect) == before
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert {m["route"] for m in rep["misses"] if m["metric"] == "p95_ms"} == set(routes)
    assert rep["overall"]["errors"] == 0 and rep["overall"]["count"] == 6
    assert rep["functions"]["alerts_intraday"]["rules"] == 1000
    assert rep["seed"]["symbols"] == ["ACME", "LOAD000", "LOAD001", "LOAD002"]
    assert not os.path.exists(rep["seed"]["workdir"]), "the temp folder is removed afterwards"
    assert not temp_db.exists(), "the run used its own database, never the one DB_PATH pointed at before"
