"""
The pages under /ATIP/ (ATIP_ROOT_PATH=/ATIP; TEST_SPEC.md D10, docs/HOSTED_DEPLOYMENT.md section 4).

Behind the proxy, ATIP is reached at https://<host>/ATIP/... and the proxy strips the
prefix; anything outside /ATIP/ is the portal's, not ATIP's. The pages were written with
root-relative URLs (fetch('/api/orders'), href="/trading", location='/login'), so under the
prefix every one of them went to the portal and 404ed. dashboard/base_path.py rewrites the
pages and scripts for the request's root_path.

These tests serve the seeded dashboard over real HTTP (uvicorn, a free local port) behind a
stand-in for the proxy: a request under /ATIP/ reaches the app with root_path "/ATIP" and the
path uvicorn --root-path gives it; any other path is answered 404, as the portal would.

  * every root-relative URL in every served page and script carries the prefix;
  * the pages' own links and calls, swept under /ATIP and at the root, find no 404;
  * the middleware that decides by path (ops/http.py, enterprise/authz.py) sees the path
    without the prefix, and the sign-in redirect keeps it;
  * at the root the pages are served exactly as before;
  * in Chromium (when Playwright is installed), no page under /ATIP/ requests anything
    outside the prefix, every same-origin link points inside it, and no script fails.
"""

import glob
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tools import load_test as LT

ROOT = "/ATIP"
STREAMING = ("/api/stream", "/api/live", "/events")
QUOTED = re.compile(r"""(?P<pre>.)(?P<q>['"`])(?P<u>/[A-Za-z][^'"`\s<>]*)""")


def behind_proxy(app, root=ROOT):
    """What nginx's `location /ATIP/ { proxy_pass http://127.0.0.1:8000/; }` plus uvicorn
    --root-path /ATIP hand the app: root_path "/ATIP", path "/ATIP/<rest>". Outside the
    prefix the portal answers, and ATIP has nothing there: 404."""
    async def proxied(scope, receive, send):
        if scope["type"] == "http":
            p = scope["path"]
            if not (p == root or p.startswith(root + "/")):
                body = f"portal: {p} is not under {root}/".encode()
                await send({"type": "http.response.start", "status": 404,
                            "headers": [(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
            scope = dict(scope, root_path=root)
        return await app(scope, receive, send)
    return proxied


@pytest.fixture
def served(temp_db, tmp_path, monkeypatch):
    """The seeded dashboard twice over: at the root and behind the /ATIP proxy stand-in. cwd =
    tmp_path, so the state cache, token and config live there (as in test_w39_ui_smoke.py)."""
    pytest.importorskip("fastapi")
    pytest.importorskip("uvicorn")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ATIP_ROOT_PATH", raising=False)
    with LT.block_network():
        LT.seed(tmp_path, extra_symbols=3)
        from dashboard import server
        with LT.serve(server.app) as plain, LT.serve(behind_proxy(server.app)) as prefixed:
            yield {"app": server.app, "root": plain, "proxy": prefixed}


def _get(url, timeout=90):
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
            return r.status, r.headers.get("content-type", ""), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("content-type", ""), e.read().decode("utf-8", "replace")


def _page_paths(app):
    """Every parameterless GET route outside /api/ and the API docs: the pages, sw.js, the PWA files."""
    out = []
    for r in app.routes:
        p = getattr(r, "path", "")
        if "GET" in (getattr(r, "methods", None) or ()) and "{" not in p and not p.startswith(
                ("/api/", "/docs", "/redoc", "/openapi", "/health")) and not any(s in p for s in STREAMING):
            out.append(p)
    return sorted(set(out))


@pytest.fixture
def pages(served):
    """Every HTML / JavaScript page, served under /ATIP and at the root: {"under": {path: body},
    "plain": {path: body}, "html": {paths served as text/html}}."""
    under, plain, html = {}, {}, set()
    for p in _page_paths(served["app"]):
        s, ct, body = _get(served["proxy"] + ROOT + p)
        if "html" in ct or "javascript" in ct:
            under[p] = body
            plain[p] = _get(served["root"] + p)[2]
            if "text/html" in ct:
                html.add(p)
    assert len(under) >= 25, f"only {len(under)} pages found: {sorted(under)}"
    return {"under": under, "plain": plain, "html": html}


def _literals(body):
    """(char before the quote, URL) for every quoted string that starts with /<letter>."""
    return [(m.group("pre"), m.group("u")) for m in QUOTED.finditer(body)]


def test_every_root_relative_url_in_the_served_pages_and_scripts_gets_the_prefix(pages):
    missed = {}
    for path, body in pages["under"].items():
        bad = [u for pre, u in _literals(body)
               if not (u == ROOT or u.startswith(ROOT + "/"))
               and pre not in "+/\\"]                       # a path fragment ('+id+'/status') or a regex
        bad += re.findall(r"""\b(?:href|src|action)\s*=\s*['"]/['"]""", body)
        if bad:
            missed[path] = sorted(set(bad))
        if path in pages["html"]:
            assert f'window.ATIP_BASE="{ROOT}"' in body, path
    assert missed == {}, f"root-relative URLs left without {ROOT}: {missed}"
    # and the rewrite is real: the dashboard, the wealth page and sw.js carry many prefixed URLs
    for p in ("/", "/wealth", "/trading", "/sw.js"):
        assert pages["under"][p].count(ROOT + "/") >= 3, p


def test_at_the_root_the_pages_are_served_unchanged(served, pages):
    for path, body in pages["plain"].items():
        assert "window.ATIP_BASE=" not in body and ROOT + "/" not in body, path
    assert "fetch('/api/" in pages["plain"]["/"] or "afetch('/api/" in pages["plain"]["/"]
    # the snapshot path (publish_snapshot.build_snapshot) calls build_html() directly: never rewritten
    from dashboard import server
    from dashboard.base_path import prefix_urls
    assert prefix_urls("fetch('/api/x')", "", server.app) == "fetch('/api/x')"


def _targets(body):
    """Each URL a page links to or calls, as the browser requests it (host-relative), split
    into ones complete in the source (GET-able as they stand) and ones that continue at run
    time (a template or a concatenation: only their literal start is known)."""
    complete, partial = set(), set()
    for m in QUOTED.finditer(body):
        u = m.group("u")
        if m.group("pre") in "+/\\" or u == ROOT:          # a fragment, a regex; window.ATIP_BASE
            continue
        tail = body[m.end():m.end() + 3]
        if tail[:1] != m.group("q") or "${" in u or tail[1:2] == "+" or tail[1:3] == " +":
            partial.add(u.split("${")[0].split("?")[0])
        else:
            complete.add(u)
    return complete, partial


def _route_shapes(app):
    return {re.sub(r"\{[^}]*\}", "{}", getattr(r, "path", "")) for r in app.routes if getattr(r, "path", "")}


@pytest.mark.parametrize("where", ["proxy", "root"])
def test_route_sweep_the_pages_links_and_calls_find_no_404(served, pages, where):
    """Every link and call in every page, requested the way the browser would: under /ATIP
    through the proxy stand-in, and at the root."""
    root = ROOT if where == "proxy" else ""
    host = served[where]
    complete, partial = set(), set()
    for body in pages["under" if root else "plain"].values():
        c, p = _targets(body)
        complete |= c
        partial |= p
    assert len(complete) >= 40 and len(partial) >= 40, (len(complete), len(partial))
    outside = sorted(u for u in complete | partial if root and not u.startswith(root + "/"))
    not_found = []
    for u in sorted(complete):
        if u in outside or any(s in u for s in STREAMING):
            continue
        status, _, body = _get(host + u)
        # a 404 from the route itself ("no allocation run yet") is an answer; no route there
        # (FastAPI's {"detail":"Not Found"}) or the portal's 404 is a broken link
        if status == 404 and ('"detail":"Not Found"' in body or body.startswith("portal:")):
            not_found.append(u)
    shapes = _route_shapes(served["app"])
    no_route = sorted(u for u in partial if u not in outside
                      and not any(s.startswith(u[len(root):]) for s in shapes))
    assert outside == [], f"{where}: links / calls that leave {root}/ for the portal: {outside}"
    assert not_found == [], f"{where}: links / calls that answer 404: {not_found}"
    assert no_route == [], f"{where}: URL starts no route begins with: {no_route}"


def test_a_page_outside_the_prefix_is_the_portals():
    """The stand-in itself: a root-relative call from a page at /ATIP/ reaches the portal, not ATIP."""
    import asyncio
    sent = []

    async def app(scope, receive, send):                     # never reached
        raise AssertionError(scope["path"])

    async def send(msg):
        sent.append(msg)
    asyncio.run(behind_proxy(app)({"type": "http", "path": "/api/orders", "headers": []}, None, send))
    assert sent[0]["status"] == 404


def test_middleware_sees_the_path_without_the_prefix_and_the_sign_in_redirect_keeps_it():
    from dashboard.base_path import route_path
    assert route_path({"path": "/ATIP/api/orders", "root_path": "/ATIP"}) == "/api/orders"     # uvicorn
    assert route_path({"path": "/api/orders", "root_path": "/ATIP"}) == "/api/orders"          # TestClient
    assert route_path({"path": "/ATIP", "root_path": "/ATIP"}) == "/"
    assert route_path({"path": "/ATIPX/api", "root_path": "/ATIP"}) == "/ATIPX/api"
    assert route_path({"path": "/api/orders", "root_path": ""}) == "/api/orders"


def _asgi_get(app, path, root):
    """One GET through the whole ASGI stack, scope as uvicorn --root-path builds it."""
    import asyncio
    out = {"status": None, "headers": {}, "body": b""}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
            out["headers"] = {k.decode().lower(): v.decode("latin-1") for k, v in msg.get("headers", [])}
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")
    p, q = (path.split("?", 1) + [""])[:2]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": root + p, "raw_path": (root + p).encode(), "root_path": root, "query_string": q.encode(),
             "headers": [(b"host", b"127.0.0.1")], "client": ("127.0.0.1", 5000), "server": ("127.0.0.1", 80)}
    asyncio.run(app(scope, receive, send))
    return out


def test_api_v1_and_the_security_headers_work_under_the_prefix(temp_db, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    monkeypatch.chdir(tmp_path)
    from dashboard import server
    for root in ("", ROOT):
        r = _asgi_get(server.app, "/api/v1/enterprise/status", root)
        assert r["status"] == 200, (root, r["status"], r["body"][:200])
        assert r["headers"].get("api-version") == "v1", root           # ops/http.py saw /api/v1/...
        r = _asgi_get(server.app, "/api/auth/me", root)
        assert r["headers"].get("cache-control") == "no-store", root   # matched on the route path


def test_enterprise_sign_in_redirect_keeps_the_prefix(temp_db, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    monkeypatch.chdir(tmp_path)
    from enterprise import authz
    monkeypatch.setattr(authz, "settings", lambda: {"enabled": True, "default_tenant": "default"})
    monkeypatch.setattr(authz, "resolve", lambda conn, request: None)              # nobody signed in
    from dashboard import server
    r = _asgi_get(server.app, "/trading?x=1", ROOT)
    assert r["status"] == 302 and r["headers"]["location"] == "/ATIP/login?next=/ATIP/trading%3Fx%3D1", r
    r = _asgi_get(server.app, "/login", ROOT)                    # public under the prefix too: no redirect loop
    assert r["status"] == 200, r["status"]
    assert _asgi_get(server.app, "/health/live", ROOT)["status"] == 200
    r = _asgi_get(server.app, "/trading", "")
    assert r["status"] == 302 and r["headers"]["location"] == "/login?next=/trading", r


def test_the_pwa_manifest_points_inside_the_prefix(served):
    s, ct, body = _get(served["proxy"] + ROOT + "/manifest.webmanifest")
    m = json.loads(body)
    assert (m["start_url"], m["scope"], m["icons"][0]["src"]) == ("/ATIP/m", "/ATIP/", "/ATIP/icon.svg")
    m = json.loads(_get(served["root"] + "/manifest.webmanifest")[2])
    assert (m["start_url"], m["scope"], m["icons"][0]["src"]) == ("/m", "/", "/icon.svg")


# ── in a browser ────────────────────────────────────────────────────────────────────────────

def _chromium_candidates():
    return [c for c in [os.environ.get("ATIP_CHROMIUM")]
            + sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"), reverse=True)
            if c and Path(c).exists()] + [None]


def test_in_chromium_no_page_under_the_prefix_reaches_outside_it(served, pages):
    sync_api = pytest.importorskip("playwright.sync_api")
    pw = sync_api.sync_playwright().start()
    try:
        b, why = None, []
        for exe in _chromium_candidates():
            try:
                b = pw.chromium.launch(executable_path=exe) if exe else pw.chromium.launch()
                break
            except Exception as e:
                why.append(f"{exe or 'playwright default'}: {str(e).splitlines()[0][:120]}")
        if b is None:
            pytest.skip("no launchable Chromium for Playwright (" + "; ".join(why) + ")")
        origin = served["proxy"]
        ctx = b.new_context(viewport={"width": 1280, "height": 900})
        ctx.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin) else route.abort())
        problems = {}
        for path in sorted(p for p in pages["under"] if p != "/sw.js"):
            page, bad = ctx.new_page(), []
            page.on("pageerror", lambda e: bad.append(("pageerror", str(e)[:200])))
            page.on("request", lambda r: bad.append(("outside", r.url))
                    if r.url.startswith(origin) and not r.url[len(origin):].startswith(ROOT + "/") else None)
            page.on("response", lambda r: bad.append((r.status, r.url))
                    if r.status == 404 and r.url.startswith(origin) else None)
            page.goto(origin + ROOT + path, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(300)
            links = page.eval_on_selector_all("a[href]", "els => els.map(a => a.href)")
            bad += [("link", u) for u in links
                    if u.startswith(origin) and not u[len(origin):].startswith(ROOT + "/")]
            if page.evaluate("window.ATIP_BASE") != ROOT:
                bad.append(("ATIP_BASE", page.evaluate("String(window.ATIP_BASE)")))
            if bad:
                problems[path] = sorted(set(map(str, bad)))
            page.close()
        ctx.close()
        b.close()
        assert problems == {}, problems
    finally:
        pw.stop()


def test_the_account_and_admin_pages_define_their_helpers_before_using_them(temp_db, tmp_path, monkeypatch):
    """/account, /admin, /app and /admin/console call j() and esc() from their body script at
    once; the helpers came after it, so the first load failed with "esc is not defined"."""
    pytest.importorskip("fastapi")
    from dashboard.enterprise_page import render_account, render_admin
    from dashboard.saas_page import render_app, render_console
    for html in (render_account(), render_admin(), render_app(), render_console()):
        body = html.index("<div class='wrap'>")
        first_use = html.index("<script>", body)
        assert html.index("async function j(") < first_use and html.index("const esc=") < first_use
