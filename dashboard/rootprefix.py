"""Keep every ATIP link inside its sub-path (bkesari VPS: https://bkesari.com/atip/).

ATIP's pages were written for a dashboard at the site root, so most of them say
href="/command", fetch("/api/...") or location.href = "/sessions". Behind a proxy that
serves ATIP under a prefix those URLs leave ATIP and land on whatever owns the root
(on bkesari.com, the hub). This layer fixes that without touching any page:

1. HTML responses: root-relative URLs in href/src/action/formaction/data-href/data-url/
   data-src/poster get the prefix, and a small script goes first in <head>.
2. That script prefixes URLs the page builds at run time: fetch, XMLHttpRequest,
   EventSource, window.open, history.pushState/replaceState, service-worker registration,
   links/forms added later (MutationObserver plus a click/submit check), and
   location changes through the Navigation API where the browser has it.
3. Redirects: a root-relative Location header gets the prefix.
4. The web-app manifest: root-relative strings get the prefix.

A no-op when the app is served at the root (no root_path), so a local run is unchanged.
VPS-only patch, 18-Nov-2026; installed from dashboard/server.py.
"""
from __future__ import annotations

import json
import re

_ATTR = re.compile(rb"""(\b(?:href|src|action|formaction|data-href|data-url|data-src|poster)\s*=\s*)(["'])/(?!/)""", re.I)
_HEAD = re.compile(rb"<head\b[^>]*>", re.I)

_SHIM = r"""<script>(function(){var R=%s;if(!R)return;var O=location.origin;
function fx(u){if(typeof u!=='string')return u;var s=u;if(s.indexOf(O+'/')===0)s=s.slice(O.length);
if(s.charAt(0)!=='/'||s.charAt(1)==='/')return u;if(s===R||s.indexOf(R+'/')===0||s.indexOf(R+'?')===0||s.indexOf(R+'#')===0)return u;
return R+s;}
window.__atipFix=fx;
var F=window.fetch;if(F)window.fetch=function(i,o){try{if(typeof i==='string')i=fx(i);else if(i&&i.url&&typeof Request!=='undefined'&&i instanceof Request){var n=fx(i.url);if(n!==i.url)i=new Request(n,i);}else if(i&&i.href)i=fx(i.href);}catch(e){}return F.call(this,i,o);};
var XO=XMLHttpRequest.prototype.open;XMLHttpRequest.prototype.open=function(m,u){arguments[1]=fx(String(u));return XO.apply(this,arguments);};
if(window.EventSource){var ES=window.EventSource;window.EventSource=function(u,c){return new ES(fx(String(u)),c);};window.EventSource.prototype=ES.prototype;}
var WO=window.open;window.open=function(u){if(u)arguments[0]=fx(String(u));return WO.apply(window,arguments);};
['pushState','replaceState'].forEach(function(k){var h=history[k];history[k]=function(s,t,u){if(u!=null)arguments[2]=fx(String(u));return h.apply(history,arguments);};});
try{if(navigator.serviceWorker){var SW=navigator.serviceWorker.register.bind(navigator.serviceWorker);navigator.serviceWorker.register=function(u,o){return SW(fx(String(u)),o);};}}catch(e){}
var A=['href','src','action','formaction','data-href','data-url','data-src','poster'];
function fe(el){if(!el||el.nodeType!==1)return;for(var i=0;i<A.length;i++){var v=el.getAttribute&&el.getAttribute(A[i]);if(v){var n=fx(v);if(n!==v)el.setAttribute(A[i],n);}}}
function ft(root){fe(root);if(root.querySelectorAll){var l=root.querySelectorAll('[href],[src],[action],[formaction],[data-href],[data-url],[data-src],[poster]');for(var i=0;i<l.length;i++)fe(l[i]);}}
try{new MutationObserver(function(ms){for(var i=0;i<ms.length;i++){var m=ms[i];if(m.type==='attributes')fe(m.target);else for(var j=0;j<m.addedNodes.length;j++)ft(m.addedNodes[j]);}}).observe(document.documentElement,{subtree:true,childList:true,attributes:true,attributeFilter:A});}catch(e){}
document.addEventListener('click',function(e){var a=e.target&&e.target.closest&&e.target.closest('a[href]');if(a)fe(a);},true);
document.addEventListener('submit',function(e){var f=e.target;if(f&&f.getAttribute){fe(f);if(e.submitter)fe(e.submitter);}},true);
try{if(window.navigation&&navigation.addEventListener)navigation.addEventListener('navigate',function(e){try{if(!e.cancelable||e.hashChange||e.downloadRequest)return;var d=new URL(e.destination.url);if(d.origin!==O)return;var p=d.pathname+d.search+d.hash;var n=fx(p);if(n!==p){e.preventDefault();location.assign(n);}}catch(x){}});}catch(e){}
})();</script>"""


def rewrite_html(body: bytes, root: str) -> bytes:
    r = root.encode()

    def attr(m):
        url_start = m.end() - 1                                 # the URL's leading "/"
        if body[url_start:url_start + len(r)] == r and \
                body[url_start + len(r):url_start + len(r) + 1] in (b"/", b"?", b"#", b'"', b"'"):
            return m.group(0)                                   # already under the prefix
        return m.group(1) + m.group(2) + r + b"/"

    out = _ATTR.sub(attr, body)
    shim = (_SHIM % json.dumps(root)).encode()
    h = _HEAD.search(out)
    if h:
        out = out[:h.end()] + shim + out[h.end():]
    else:
        out = shim + out
    return out


def _prefix_path(v: str, root: str) -> str:
    if v.startswith("/") and not v.startswith("//") and not (
            v == root or v.startswith(root + "/") or v.startswith(root + "?") or v.startswith(root + "#")):
        return root + v
    return v


def rewrite_manifest(body: bytes, root: str) -> bytes:
    try:
        data = json.loads(body)
    except Exception:
        return body

    def walk(x):
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        if isinstance(x, str):
            return _prefix_path(x, root)
        return x
    return json.dumps(walk(data)).encode()


class RootPrefixMiddleware:
    """Pure ASGI, outermost. Buffers HTML / manifest bodies (uncompressed only) and fixes redirects."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        root = (scope.get("root_path") or "").rstrip("/") if scope.get("type") == "http" else ""
        if not root:
            return await self.app(scope, receive, send)
        held = {"start": None, "kind": None, "chunks": []}

        async def capture(msg):
            if msg["type"] == "http.response.start":
                headers = list(msg.get("headers") or [])
                h = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in headers}
                if "location" in h:
                    loc = _prefix_path(h["location"], root)
                    if loc != h["location"]:
                        headers = [(k, v) for k, v in headers if k.lower() != b"location"] + \
                                  [(b"location", loc.encode("latin-1"))]
                        msg = {**msg, "headers": headers}
                ct = h.get("content-type", "")
                kind = None
                if not h.get("content-encoding"):
                    if "text/html" in ct:
                        kind = "html"
                    elif "manifest" in ct:
                        kind = "manifest"
                if kind is None:
                    await send(msg)
                    return
                held["start"], held["kind"] = msg, kind
                return
            if msg["type"] != "http.response.body" or held["kind"] is None:
                await send(msg)
                return
            held["chunks"].append(msg.get("body", b""))
            if msg.get("more_body"):
                return
            body = b"".join(held["chunks"])
            try:
                body = rewrite_html(body, root) if held["kind"] == "html" else rewrite_manifest(body, root)
            except Exception:
                pass                                            # never break a page over its links
            start = held["start"]
            hdrs = [(k, v) for k, v in start.get("headers") or [] if k.lower() != b"content-length"]
            hdrs.append((b"content-length", str(len(body)).encode()))
            await send({**start, "headers": hdrs})
            await send({"type": "http.response.body", "body": body, "more_body": False})

        await self.app(scope, receive, capture)


def install(app) -> None:
    app.add_middleware(RootPrefixMiddleware)
