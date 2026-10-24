"""Serving the dashboard under a URL prefix (ATIP_ROOT_PATH=/ATIP, FastAPI's root_path).

Behind a proxy that serves ATIP at https://<host>/ATIP/ and strips the prefix
(docs/HOSTED_DEPLOYMENT.md section 4), the server is told the prefix and puts it
in every request's ``scope["root_path"]``. The pages, though, are written with
root-relative URLs: ``fetch('/api/orders')``, ``href="/trading"``,
``location='/login'``. A browser resolves those against the host's root, the
portal's, not ATIP's.

``BasePathMiddleware`` fixes all of them in one place. When a request carries a
root_path, every HTML or JavaScript response is rewritten on the way out:

* a quoted string that starts with ``/<segment>``, where ``<segment>`` is the
  first path segment of one of the app's own routes (``api``, ``trading``,
  ``login``, ...), gets the prefix: ``'/api/x'`` -> ``'/ATIP/api/x'``. That covers
  every fetch, link, redirect and ``window.open`` the pages make, including
  template literals and concatenations, because they all start with such a string;
* ``href="/"`` / ``src="/"`` / ``action="/"`` (the "back to the dashboard" links)
  become ``href="/ATIP/"``;
* ``window.ATIP_BASE`` is defined (``"/ATIP"``) at the top of every HTML page, for
  the few scripts that build a URL from ``'/'`` alone (the sign-in redirect).

Without a root_path (a plain local run at http://127.0.0.1:8000/) nothing is
touched: the responses are byte-for-byte what the routes produced. The snapshot
that publish_snapshot.py sends to bkesari.com is built by calling build_html()
directly, not through this middleware, so it is unchanged as well (its token is
stripped and the hub's CSP blocks its requests).

``route_path(scope)`` gives the path the routes are matched on, without the
prefix, for middleware that decides by path (ops/http.py, enterprise/authz.py).
"""

from __future__ import annotations

import json
import re

_SAFE_ROOT = re.compile(r"(/[A-Za-z0-9._~-]+)+")
_REWRITE_TYPES = (b"text/html", b"javascript")
_HOME = re.compile(r"""(?P<a>\b(?:href|src|action)\s*=\s*)(?P<q>['"])/(?P=q)""")
_HEAD = re.compile(r"<head\b[^>]*>", re.I)
_patterns: dict[int, tuple] = {}     # id(app) -> (app, pattern); holding the app keeps its id from being reused


def route_path(scope) -> str:
    """The request path without root_path. uvicorn (0.26+) puts the prefix in front of
    ``scope["path"]``; Starlette's TestClient does not. Either way this is the path the
    routes are matched on: "/api/orders", never "/ATIP/api/orders"."""
    path, root = scope.get("path", "") or "", scope.get("root_path", "") or ""
    if root and (path == root or path.startswith(root + "/")):
        return path[len(root):] or "/"
    return path


def segments(app) -> list[str]:
    """First path segments of the app's own routes: "api", "trading", "sw.js", ..."""
    out = set()
    for r in getattr(app, "routes", []):
        p = getattr(r, "path", "") or ""
        seg = p.split("/")[1] if p.startswith("/") and len(p) > 1 else ""
        if seg and "{" not in seg:
            out.add(seg)
    return sorted(out, key=lambda s: (-len(s), s))


def _url_pattern(app) -> re.Pattern | None:
    """A quote, then "/" and one of the app's route segments, then "/", "?", "#" or the closing quote."""
    hit = _patterns.get(id(app))
    if hit is None or hit[0] is not app:
        segs = segments(app)
        pat = re.compile(r"""(?P<q>['"`])/(?=(?:%s)(?:[/?#'"`]))""" % "|".join(map(re.escape, segs))) \
            if segs else None
        hit = _patterns[id(app)] = (app, pat)
    return hit[1]


def prefix_urls(text: str, root: str, app, html: bool = True) -> str:
    """Put ``root`` in front of every root-relative URL to one of ``app``'s routes in
    ``text``, and (for HTML) define window.ATIP_BASE. ``root`` must be normalised
    ("/ATIP"); an empty one returns the text unchanged."""
    if not root:
        return text
    pat = _url_pattern(app)
    if pat is not None:
        text = pat.sub(lambda m: m.group("q") + root + "/", text)
    text = _HOME.sub(lambda m: m.group("a") + m.group("q") + root + "/" + m.group("q"), text)
    if html:
        tag = "<script>window.ATIP_BASE=" + json.dumps(root).replace("<", "\\u003c") + ";</script>"
        m = _HEAD.search(text)
        text = text[:m.end()] + tag + text[m.end():] if m else tag + text
    return text


class BasePathMiddleware:
    """Rewrites HTML and JavaScript responses for the request's root_path (see the module
    docstring). A pass-through when there is no root_path."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        root = (scope.get("root_path") or "") if scope["type"] == "http" else ""
        if not root or not _SAFE_ROOT.fullmatch(root) or scope.get("method") == "HEAD":
            return await self.app(scope, receive, send)
        owner = scope.get("app")
        held, chunks = {}, []

        async def send_wrapper(msg):
            if msg["type"] == "http.response.start":
                ct = next((v for k, v in msg.get("headers", []) if k.lower() == b"content-type"), b"").lower()
                if any(t in ct for t in _REWRITE_TYPES):
                    held["start"], held["html"] = msg, b"text/html" in ct
                    m = re.search(rb"charset=([\w-]+)", ct)
                    held["charset"] = m.group(1).decode() if m else "utf-8"
                    return
                return await send(msg)
            if msg["type"] == "http.response.body" and "start" in held:
                chunks.append(msg.get("body", b""))
                if msg.get("more_body"):
                    return
                body = b"".join(chunks)
                if body:
                    try:
                        body = prefix_urls(body.decode(held["charset"]), root, owner, held["html"]) \
                            .encode(held["charset"])
                    except (UnicodeError, LookupError):
                        pass                                      # not text after all: send it as it is
                start = held.pop("start")
                headers = [(k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"]
                await send({**start, "headers": headers + [(b"content-length", str(len(body)).encode())]})
                return await send({"type": "http.response.body", "body": body})
            return await send(msg)

        await self.app(scope, receive, send_wrapper)
