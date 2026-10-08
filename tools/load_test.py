#!/usr/bin/env python
"""
ATIP load / latency test (W39; the development side of Wave 18 "Full QA / Security /
Performance").

Fires read-only GET requests at the dashboard from N concurrent workers and reports, per
route: request count, error rate, p50 / p95 / p99 / max latency, checked against the
targets below. Exit code 0 = every target met, 1 = a target missed, 2 = the run could not
start (bad arguments, server unreachable).

Two modes
  server        a running dashboard (`python main.py --dashboard`), over HTTP:
                    python tools/load_test.py --base-url http://127.0.0.1:8000 --requests 400 --concurrency 8
                    python tools/load_test.py --duration 60 --concurrency 4 --json load.json
  in-process    no server: the FastAPI app through starlette's TestClient, on a seeded
                throwaway database in a temp folder, with outbound network refused -- what
                CI runs (tests/test_w39_perf_budget.py):
                    python tools/load_test.py --in-process --requests 300 --concurrency 4

Only GET routes that read are requested -- never POST / PUT / DELETE. Routes whose latency
belongs to another system are left out of the default set on purpose: /api/live-quotes
(Dhan, in market hours), /api/zerodha/*, /api/schemas/check-all (calls this server back over
HTTP) and /api/platform/postgres (connects to a database server).

Dependencies: the standard library, plus httpx when installed (server mode falls back to
urllib). In-process mode needs fastapi (which brings httpx) and the repository itself.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import errno
import json
import logging
import math
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, namedtuple
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ── Route set ───────────────────────────────────────────────────────────────
# The main pages and the read APIs they (or the mobile page) poll. "{sym}" is filled from
# --symbol, or from the first row of /api/scores.
PAGES = ("/", "/wealth", "/trading", "/baskets", "/market", "/strategies", "/backtests", "/ml", "/quant",
         "/data-platform", "/compliance", "/m")
APIS = ("/health", "/health/ready", "/api/scores", "/api/mh", "/api/tod", "/api/alerts", "/api/portfolio",
        "/api/news", "/api/orders", "/api/orders/pending", "/api/stock/{sym}/history", "/api/wealth/overview",
        "/api/wealth/summary", "/api/wealth/goals", "/api/risk/portfolio", "/api/risk/limits",
        "/api/risk/exposure", "/api/execution/status", "/api/oms/orders", "/api/pnl/live",
        "/api/mobile/summary", "/api/ops/status", "/api/backtests", "/api/strategies")
DEFAULT_ROUTES = PAGES + APIS

# ── Targets ─────────────────────────────────────────────────────────────────
# Where the numbers come from:
#   * docs/W33_QA_UAT_HANDOFF.md §2 ("Performance: what to measure") states no per-route HTTP
#     latency target. Its numbers are for background work: enterprise/w32.capital_usage
#     "< 200 ms at today's fill count" and alerts_intraday "< 2 s for 1,000 rules". The second
#     is measured here in in-process mode (FUNCTION_TARGETS); capital_usage needs the
#     production fill count, so it stays on the W33 production-copy checklist.
#   * docs/W18_QA_SECURITY_PERFORMANCE_REPORT.md §3 measured, on the 70 MB production copy,
#     "Overview (whole chain) 0.3 s" and "Wealth summary < 0.3 s"; those are the p50 budgets of
#     /api/wealth/overview and /api/wealth/summary below.
#   * W18 §1 / W33 §1: a read must never be a server error, so the error-rate target is 0.
#   * Every other route: development's own budget (p95 <= 1 s), because no document sets one.
DEFAULT_TARGETS = {"max_error_rate": 0.0, "p95_ms": 1000.0}
ROUTE_TARGETS = {
    "/api/wealth/overview": {"p50_ms": 300.0},      # W18 §3: overview 0.3 s
    "/api/wealth/summary": {"p50_ms": 300.0},       # W18 §3: wealth summary < 0.3 s
}
FUNCTION_TARGETS = {
    "alerts_intraday": {"max_ms": 2000.0, "rules": 1000},    # W33 §2: < 2 s for 1,000 rules
}
TARGET_SOURCES = {
    "max_error_rate": "W18 §1 / W33 §1: reads never return 5xx",
    "p95_ms": "development budget (no document sets a per-route HTTP target)",
    "p50_ms": "W18 §3 measured times on the production copy",
    "alerts_intraday": "W33 §2: alerts_intraday < 2 s for 1,000 rules",
}

Sample = namedtuple("Sample", "route status seconds error")


# ── Statistics ──────────────────────────────────────────────────────────────

def percentile(values, p):
    """Nearest-rank percentile: the smallest value with at least p% of the sample at or below
    it. [10, 20, ..., 100] gives p50 = 50, p95 = 100, p99 = 100; p = 0 gives the minimum.
    None for an empty sample."""
    if not 0 <= p <= 100:
        raise ValueError("p must be within 0..100")
    if not values:
        return None
    s = sorted(values)
    if p == 0:
        return s[0]
    k = max(1, math.ceil(p * len(s) / 100))
    return s[min(k, len(s)) - 1]


def is_error(sample) -> bool:
    """A failed request: no response at all, or a server error (5xx)."""
    return sample.error is not None or sample.status is None or sample.status >= 500


def route_stats(samples) -> dict:
    ms = [s.seconds * 1000.0 for s in samples]
    n = len(samples)
    errors = sum(1 for s in samples if is_error(s))
    r2 = lambda v: None if v is None else round(v, 2)                       # noqa: E731
    return {"count": n, "errors": errors, "error_rate": round(errors / n, 4) if n else 0.0,
            "non_2xx": sum(1 for s in samples if s.status is None or not 200 <= s.status < 300),
            "statuses": dict(sorted(Counter(str(s.status) for s in samples).items())),
            "p50_ms": r2(percentile(ms, 50)), "p95_ms": r2(percentile(ms, 95)), "p99_ms": r2(percentile(ms, 99)),
            "max_ms": r2(max(ms) if ms else None), "mean_ms": r2(sum(ms) / n if n else None)}


def summarize(samples, wall_seconds=None) -> dict:
    """{"routes": {route: stats}, "overall": stats (+ rps and wall_s when wall_seconds is given)}."""
    by = {}
    for s in samples:
        by.setdefault(s.route, []).append(s)
    overall = route_stats(samples)
    if wall_seconds:
        overall["wall_s"] = round(wall_seconds, 3)
        overall["rps"] = round(len(samples) / wall_seconds, 2)
    return {"routes": {r: route_stats(v) for r, v in by.items()}, "overall": overall}


def targets_for(route, defaults=None, per_route=None) -> dict:
    defaults = DEFAULT_TARGETS if defaults is None else defaults
    per_route = ROUTE_TARGETS if per_route is None else per_route
    return {**defaults, **per_route.get(route, {})}


def check_targets(report, defaults=None, per_route=None) -> list:
    """Every missed target as {"route", "metric", "value", "target"}. A route with no samples
    misses nothing; error rates compare with >, latencies with > (so equal passes)."""
    misses = []
    for route, st in sorted(report["routes"].items()):
        for metric, target in targets_for(route, defaults, per_route).items():
            if target is None:
                continue
            value = st["error_rate"] if metric == "max_error_rate" else st.get(metric)
            if value is not None and value > target:
                misses.append({"route": route, "metric": metric, "value": value, "target": target})
    for name, st in (report.get("functions") or {}).items():
        if st.get("target_ms") is not None and st.get("ms") is not None and st["ms"] > st["target_ms"]:
            misses.append({"route": f"function:{name}", "metric": "max_ms", "value": st["ms"],
                           "target": st["target_ms"]})
    return misses


# ── Driving the load ────────────────────────────────────────────────────────

def expand(routes, params=None) -> list:
    """[(route template, request path)]: "{sym}" and friends filled from params."""
    params = params or {}
    return [(r, r.format(**params) if "{" in r else r) for r in routes]


def _one(fetch, route, path) -> Sample:
    t = time.perf_counter()
    try:
        status, error = int(fetch(path)), None
    except Exception as e:                       # a refused / reset / timed-out request is a sample too
        status, error = None, f"{type(e).__name__}: {e}"[:200]
    return Sample(route, status, time.perf_counter() - t, error)


def run_load(fetch, routes, requests=None, duration=None, concurrency=4, warmup=True, params=None):
    """Round-robin over `routes` from `concurrency` threads until `requests` have been sent or
    `duration` seconds have passed (whichever comes first). `fetch(path) -> status code` must
    be safe to call from several threads. A warm-up pass (one request per route, not recorded)
    keeps first-call imports and caches out of the numbers. Returns (samples, wall_seconds)."""
    if requests is None and duration is None:
        raise ValueError("give requests or duration")
    if requests is not None and requests < 1:
        raise ValueError("requests must be >= 1")
    if duration is not None and duration <= 0:
        raise ValueError("duration must be > 0")
    paths = expand(routes, params)
    if not paths:
        raise ValueError("no routes")
    if warmup:
        for route, path in paths:
            _one(fetch, route, path)
    samples, lock, nxt = [], threading.Lock(), [0]
    deadline = time.monotonic() + duration if duration is not None else None

    def worker():
        while True:
            with lock:
                i = nxt[0]
                nxt[0] += 1
            if (requests is not None and i >= requests) or (deadline is not None and time.monotonic() >= deadline):
                return
            route, path = paths[i % len(paths)]
            s = _one(fetch, route, path)
            with lock:
                samples.append(s)

    t0 = time.monotonic()
    threads = [threading.Thread(target=worker, name=f"load-{k}", daemon=True) for k in range(max(1, int(concurrency)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return samples, time.monotonic() - t0


def http_fetcher(base_url, timeout=30.0, headers=None):
    """fetch(path) -> status for a running server: one httpx client per thread when httpx is
    installed, else urllib. Proxy environment variables are ignored (the target is local)."""
    base = base_url.rstrip("/")
    headers = dict(headers or {})
    try:
        import httpx
    except ImportError:
        httpx = None
    if httpx is not None:
        local, clients = threading.local(), []

        def fetch(path):
            c = getattr(local, "client", None)
            if c is None:
                c = local.client = httpx.Client(base_url=base, timeout=timeout, follow_redirects=True,
                                                trust_env=False, headers=headers)
                clients.append(c)
            return c.get(path).status_code

        fetch.close = lambda: [c.close() for c in clients]
        return fetch
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def fetch(path):
        req = urllib.request.Request(base + path, headers=headers)
        try:
            with opener.open(req, timeout=timeout) as r:
                r.read()
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    fetch.close = lambda: None
    return fetch


# ── In-process mode ─────────────────────────────────────────────────────────

def _is_local(host) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode(errors="ignore")
    host = str(host).strip("[]").lower()
    return host in ("localhost", "::1", "0.0.0.0", "") or host.startswith("127.")


@contextlib.contextmanager
def block_network():
    """Refuse every connection that does not stay on this machine (and fail its DNS lookup at
    once), for the duration. Some read paths try NSE when a cache is missing; offline they
    fail fast instead of waiting out a 12-20 s timeout -- so the numbers measure ATIP, not
    the network, and a CI runner never reaches a real market-data service."""
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_getaddrinfo, real_create = socket.getaddrinfo, socket.create_connection

    def _addr_host(address):
        return address[0] if isinstance(address, tuple) else None     # AF_UNIX paths stay allowed

    def connect(self, address):
        if not _is_local(_addr_host(address)):
            raise ConnectionRefusedError(errno.ECONNREFUSED, f"network blocked by load_test: {address!r}")
        return real_connect(self, address)

    def connect_ex(self, address):
        if not _is_local(_addr_host(address)):
            return errno.ECONNREFUSED
        return real_connect_ex(self, address)

    def getaddrinfo(host, *a, **k):
        if not _is_local(host):
            raise socket.gaierror(socket.EAI_NONAME, f"network blocked by load_test: {host}")
        return real_getaddrinfo(host, *a, **k)

    def create_connection(address, *a, **k):
        if not _is_local(_addr_host(address)):
            raise ConnectionRefusedError(errno.ECONNREFUSED, f"network blocked by load_test: {address!r}")
        return real_create(address, *a, **k)

    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
    socket.getaddrinfo, socket.create_connection = getaddrinfo, create_connection
    try:
        yield
    finally:
        socket.socket.connect, socket.socket.connect_ex = real_connect, real_connect_ex
        socket.getaddrinfo, socket.create_connection = real_getaddrinfo, real_create


def _ensure_repo_on_path():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


EXTRA_PREFIX = "LOAD"


def seed(workdir, extra_symbols=50) -> dict:
    """Fill the database db.schema.DB_PATH points at (a throwaway file -- never call this with
    the live one) and `workdir`, which must be the current directory:

      * the deterministic W18 synthetic market and investor data (tests/_wealth_seed.py:
        NIFTY50, ACME, GOLDBEES, LIQUIDBEES, midcap index; 260 sessions, market health,
        global markets, an ACME score);
      * `extra_symbols` more stocks (LOAD000...) with the same 260 sessions and a score row each,
        so the score tables and pages carry a realistic number of rows;
      * the order-rules table, which the dashboard otherwise creates on import;
      * atip_data/raw/ind_nifty500list.csv, the Nifty 500 cache a production install keeps
        (without it every wealth read retries the NSE download).
    Returns {"sessions", "last_session", "symbols"}."""
    _ensure_repo_on_path()
    from tests._wealth_seed import fresh
    workdir = Path(workdir)
    conn, sessions = fresh(None)
    try:
        last = str(sessions[-1])
        syms = [f"{EXTRA_PREFIX}{i:03d}" for i in range(int(extra_symbols))]
        rows, scores = [], []
        for k, sym in enumerate(syms):
            p0, g = 50.0 + 7.0 * k, 0.0002 * ((k % 7) - 3)
            for i, d in enumerate(sessions):
                c = p0 * (1 + g) ** i * (1 + 0.02 * math.sin((i + k) / 7))
                rows.append((sym, str(d), round(c * 0.998, 2), round(c * 1.012, 2), round(c * 0.988, 2), round(c, 2),
                             500_000 + 1_000 * k))
            sig = ("BUY", "HOLD", "SELL", "WAIT")[k % 4]
            scores.append((sym, last, 40.0 + (k * 37) % 50, 30.0 + (k * 13) % 60, 20.0 + (k * 7) % 60,
                           25.0 + (k * 11) % 60, sig, "BULL", k + 2))
        conn.executemany("INSERT OR REPLACE INTO prices_daily (symbol,date,open,high,low,close,volume) "
                         "VALUES (?,?,?,?,?,?,?)", rows)
        conn.executemany("INSERT INTO ai_scores (symbol,date,atip_score,vpi,cri,zpi,signal,regime,atip_rank) "
                         "VALUES (?,?,?,?,?,?,?,?,?)", scores)
        conn.commit()
    finally:
        conn.close()
    from orders import rules
    rules.init_orders_table()
    raw = workdir / "atip_data" / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    with open(raw / "ind_nifty500list.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Company Name", "Industry", "Symbol", "Series", "ISIN Code"])
        for i, sym in enumerate(["ACME"] + syms):
            w.writerow([f"{sym} Ltd", ("Capital Goods", "Financial Services", "Information Technology")[i % 3], sym,
                        "EQ", f"INE{i:06d}01"])
    return {"sessions": len(sessions), "last_session": last, "symbols": ["ACME"] + syms}


def testclient_fetcher(headers=None):
    """fetch(path) -> status through starlette's TestClient (one client per thread). Server
    errors come back as 500 rather than raising. No lifespan: the dashboard's startup hook
    (the order-rules monitor thread) is not started."""
    _ensure_repo_on_path()
    from fastapi.testclient import TestClient
    from dashboard import server
    local, clients = threading.local(), []

    def fetch(path):
        c = getattr(local, "client", None)
        if c is None:
            c = local.client = TestClient(server.app, raise_server_exceptions=False, headers=dict(headers or {}))
            clients.append(c)
        return c.get(path).status_code

    fetch.close = lambda: [c.close() for c in clients]
    return fetch


def bench_alerts_intraday(rules=1000, symbols=100, quotes_per_symbol=375) -> dict:
    """W33 §2: enterprise/w32.evaluate_alerts_intraday on `rules` ACTIVE rules over `symbols`
    symbols, each with a full session of 1-minute live_quotes rows. No rule fires (the
    threshold is out of reach), so the time is the per-rule quote lookup W33 asks about.
    Runs on the database db.schema.DB_PATH points at -- a seeded throwaway one."""
    _ensure_repo_on_path()
    from db.schema import get_connection
    from enterprise.w32 import evaluate_alerts_intraday
    now = datetime.now().replace(microsecond=0)
    conn = get_connection()
    try:
        conn.executemany("INSERT OR IGNORE INTO live_quotes (symbol, ltp, volume, chg_pct, timestamp, source) "
                         "VALUES (?,?,?,?,?,?)",
                         [(f"Q{s:03d}", 100.0 + s, 1000 * k, 0.1, (now - timedelta(minutes=k)).isoformat(), "load_test")
                          for s in range(symbols) for k in range(quotes_per_symbol)])
        conn.executemany("INSERT OR REPLACE INTO enterprise_alert_rule (rule_id, tenant_id, user_id, name, symbol, "
                         "feature, op, value, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         [(f"LT{i:05d}", "default", "u_load", f"load {i}", f"Q{i % symbols:03d}", "ltp", ">", 1e12,
                           "ACTIVE", now) for i in range(rules)])
        conn.commit()
        t = time.perf_counter()
        out = evaluate_alerts_intraday(conn, now=now)
        ms = (time.perf_counter() - t) * 1000.0
    finally:
        conn.close()
    return {"ms": round(ms, 2), "rules": out["rules"], "triggered": out["triggered"], "symbols": symbols,
            "quotes": symbols * quotes_per_symbol}


def free_port(host="127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def serve(app, host="127.0.0.1", port=None, timeout=30.0):
    """Run `app` with uvicorn in a background thread on a free local port; yields the base URL.
    lifespan is off, so the dashboard's startup hook (the order-rules monitor thread) is not
    started. Stops the server on exit."""
    import uvicorn
    port = port or free_port(host)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, lifespan="off", log_level="error",
                                           access_log=False))
    t = threading.Thread(target=server.run, name="load-test-uvicorn", daemon=True)
    t.start()
    deadline = time.monotonic() + timeout
    while not server.started:
        if not t.is_alive():
            raise RuntimeError(f"uvicorn exited before serving on {host}:{port}")
        if time.monotonic() > deadline:
            server.should_exit = True
            raise TimeoutError(f"uvicorn did not start on {host}:{port} within {timeout} s")
        time.sleep(0.05)
    try:
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        t.join(timeout=15)


@contextlib.contextmanager
def in_process(workdir=None, extra_symbols=50, headers=None, http=False, keep=False):
    """The whole in-process set-up for the command line: a temp folder as the current
    directory (so atip_data/config.json, the token and every cache resolve there, never in the
    real install), db.schema.DB_PATH and dashboard.security's token / config paths inside it,
    the seed, and the network blocked. http=True serves the app with uvicorn on a free local
    port and loads it over real HTTP instead of through the TestClient. Yields (fetch,
    seed_info); everything is restored on exit, and the temp folder is removed unless `keep`
    or a `workdir` was given. dashboard.server is imported only after DB_PATH has moved: it
    creates the order-rules table on import."""
    _ensure_repo_on_path()
    import shutil
    import db.schema as schema
    from dashboard import security
    own = workdir is None
    tmp = Path(workdir or tempfile.mkdtemp(prefix="atip-load-")).resolve()
    (tmp / "atip_data").mkdir(parents=True, exist_ok=True)
    saved = (os.getcwd(), schema.DB_PATH, security.TOKEN_PATH, security.CONFIG_PATH)
    os.chdir(tmp)
    schema.DB_PATH = tmp / "atip_data" / "load_test.db"
    security.TOKEN_PATH = tmp / "atip_data" / "dashboard_token.txt"
    security.CONFIG_PATH = tmp / "atip_data" / "config.json"
    fetch = None
    try:
        with block_network():
            info = seed(tmp, extra_symbols)
            info["workdir"] = str(tmp)
            if http:
                from dashboard import server as _server
                with serve(_server.app) as base:
                    info["base_url"] = base
                    fetch = http_fetcher(base, headers=headers)
                    yield fetch, info
            else:
                fetch = testclient_fetcher(headers)
                yield fetch, info
    finally:
        if fetch is not None:
            with contextlib.suppress(Exception):
                fetch.close()
        os.chdir(saved[0])
        schema.DB_PATH, security.TOKEN_PATH, security.CONFIG_PATH = saved[1:]
        if own and not keep:
            shutil.rmtree(tmp, ignore_errors=True)


# ── Reporting ───────────────────────────────────────────────────────────────

def _fmt(v, unit=""):
    return "-" if v is None else f"{v:.1f}{unit}"


def format_report(report, misses, title="") -> str:
    missed = {}
    for m in misses:
        missed.setdefault(m["route"], []).append(m["metric"])
    lines = [title] if title else []
    w = max([len(r) for r in report["routes"]] + [len("overall")]) + 2
    lines.append(f"{'route':<{w}}{'n':>6}{'err%':>8}{'p50 ms':>10}{'p95 ms':>10}{'p99 ms':>10}{'max ms':>10}  target")
    for route in sorted(report["routes"], key=lambda r: -(report["routes"][r]["p95_ms"] or 0)):
        st = report["routes"][route]
        tag = "MISS " + ",".join(missed[route]) if route in missed else "ok"
        lines.append(f"{route:<{w}}{st['count']:>6}{st['error_rate'] * 100:>7.1f}%{_fmt(st['p50_ms']):>10}"
                     f"{_fmt(st['p95_ms']):>10}{_fmt(st['p99_ms']):>10}{_fmt(st['max_ms']):>10}  {tag}")
    o = report["overall"]
    lines.append(f"{'overall':<{w}}{o['count']:>6}{o['error_rate'] * 100:>7.1f}%{_fmt(o['p50_ms']):>10}"
                 f"{_fmt(o['p95_ms']):>10}{_fmt(o['p99_ms']):>10}{_fmt(o['max_ms']):>10}"
                 + (f"  {o['rps']} req/s over {o['wall_s']} s" if o.get("rps") else ""))
    for name, st in (report.get("functions") or {}).items():
        tag = "MISS" if f"function:{name}" in missed else "ok"
        lines.append(f"function {name}: {st['ms']:.1f} ms for {st.get('rules')} rules "
                     f"(target < {st['target_ms']:.0f} ms)  {tag}")
    bad = {k: v for k, v in o["statuses"].items() if not k.startswith("2")}
    if bad:
        lines.append(f"non-2xx responses: {bad}")
    lines.append(f"RESULT: {'FAIL' if misses else 'PASS'} ({len(misses)} target(s) missed)")
    for m in misses:
        lines.append(f"  missed: {m['route']} {m['metric']} = {m['value']} (target {m['target']})")
    return "\n".join(lines)


def _resolve_symbol(fetch_json, fallback="NIFTY50"):
    try:
        rows = fetch_json("/api/scores")
        if rows and isinstance(rows, list) and rows[0].get("symbol"):
            return rows[0]["symbol"]
    except Exception:
        pass
    return fallback


def _http_json(base_url, timeout, headers):
    def get(path):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(urllib.request.Request(base_url.rstrip("/") + path, headers=headers), timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    return get


def _headers(values) -> dict:
    out = {}
    for h in values or []:
        k, sep, v = h.partition(":")
        if not sep or not k.strip():
            raise ValueError(f"--header must look like 'Name: value', got {h!r}")
        out[k.strip()] = v.strip()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ATIP dashboard load / latency test (read-only GETs).")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000", help="running dashboard (server mode)")
    ap.add_argument("--in-process", action="store_true",
                    help="no server: TestClient on a seeded temp database, network blocked (CI)")
    ap.add_argument("--requests", type=int, help="total requests (default 200 when --duration is not given)")
    ap.add_argument("--duration", type=float, help="seconds to run (stops at whichever of --requests / --duration "
                                                   "comes first)")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--route", action="append", help="request this path instead of the default set (repeatable)")
    ap.add_argument("--symbol", help="symbol for {sym} routes (default: ACME in-process, else the first /api/scores row)")
    ap.add_argument("--header", action="append", help="extra request header 'Name: value' (repeatable), e.g. a "
                                                      "session cookie when enterprise mode is on")
    ap.add_argument("--timeout", type=float, default=30.0, help="per-request timeout, seconds (server mode)")
    ap.add_argument("--p95-ms", type=float, help=f"default per-route p95 target (default {DEFAULT_TARGETS['p95_ms']:.0f})")
    ap.add_argument("--max-error-rate", type=float, help="default per-route error-rate target (default 0)")
    ap.add_argument("--no-targets", action="store_true", help="report only; never exit 1")
    ap.add_argument("--no-warmup", action="store_true", help="record the first request of every route too")
    ap.add_argument("--extra-symbols", type=int, default=50, help="in-process: synthetic stocks seeded besides "
                                                                "the W18 market")
    ap.add_argument("--skip-functions", action="store_true",
                    help="in-process: skip the W33 alerts_intraday benchmark")
    ap.add_argument("--serve", action="store_true",
                    help="in-process: serve the seeded app with uvicorn on a free local port and load it over "
                         "real HTTP (concurrency then goes through uvicorn's thread pool)")
    ap.add_argument("--keep", action="store_true", help="in-process: keep the temp folder (database, report)")
    ap.add_argument("--json", help="also write the full report to this file")
    ap.add_argument("--verbose", action="store_true", help="in-process: keep the application's log output")
    a = ap.parse_args(argv)

    try:
        headers = _headers(a.header)
    except ValueError as e:
        ap.error(str(e))
    if a.concurrency < 1:
        ap.error("--concurrency must be >= 1")
    requests = a.requests if (a.requests is not None or a.duration is not None) else 200
    routes = tuple(a.route) if a.route else DEFAULT_ROUTES
    defaults = dict(DEFAULT_TARGETS)
    if a.p95_ms is not None:
        defaults["p95_ms"] = a.p95_ms
    if a.max_error_rate is not None:
        defaults["max_error_rate"] = a.max_error_rate

    if a.serve and not a.in_process:
        ap.error("--serve needs --in-process")
    report, mode = None, a.base_url
    if a.in_process:
        mode = (f"in-process {'over HTTP (uvicorn)' if a.serve else '(TestClient)'}, seeded temp database, "
                f"network blocked")
        if not a.verbose:
            logging.disable(logging.WARNING)
        try:
            with in_process(extra_symbols=a.extra_symbols, headers=headers, http=a.serve,
                            keep=a.keep) as (fetch, info):
                sym = a.symbol or "ACME"
                samples, wall = run_load(fetch, routes, requests, a.duration, a.concurrency, not a.no_warmup,
                                         {"sym": sym})
                report = summarize(samples, wall)
                if not a.skip_functions:
                    b = bench_alerts_intraday(FUNCTION_TARGETS["alerts_intraday"]["rules"])
                    b["target_ms"] = FUNCTION_TARGETS["alerts_intraday"]["max_ms"]
                    report["functions"] = {"alerts_intraday": b}
                report["seed"] = info
        except ImportError as e:
            print(f"in-process mode needs the repository's dependencies (fastapi, pandas, ...): {e}", file=sys.stderr)
            return 2
        finally:
            logging.disable(logging.NOTSET)
    else:
        fetch = http_fetcher(a.base_url, a.timeout, headers)
        try:
            status = fetch("/health/live")
        except Exception as e:
            print(f"cannot reach {a.base_url}: {e}", file=sys.stderr)
            return 2
        if status >= 500:
            print(f"{a.base_url}/health/live answered {status}", file=sys.stderr)
        sym = a.symbol or _resolve_symbol(_http_json(a.base_url, a.timeout, headers))
        try:
            samples, wall = run_load(fetch, routes, requests, a.duration, a.concurrency, not a.no_warmup,
                                     {"sym": sym})
        finally:
            fetch.close()
        report = summarize(samples, wall)

    misses = [] if a.no_targets else check_targets(report, defaults)
    report.update(mode=mode, symbol=sym, concurrency=a.concurrency, requested=requests, duration=a.duration,
                  targets={"default": defaults, "routes": ROUTE_TARGETS, "functions": FUNCTION_TARGETS,
                           "sources": TARGET_SOURCES}, misses=misses,
                  generated_at=datetime.now().isoformat(timespec="seconds"))
    title = (f"ATIP load test -- {mode}; {report['overall']['count']} requests over {len(routes)} routes, "
             f"concurrency {a.concurrency}")
    print(format_report(report, misses, title))
    if a.json:
        Path(a.json).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return 1 if misses else 0


if __name__ == "__main__":
    sys.exit(main())
