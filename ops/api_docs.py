"""
Generated API reference (API-05), W31.

    python -m ops api-docs          writes docs/openapi.json and docs/API_REFERENCE.md

Built from the running FastAPI application object (dashboard.server.app), so it lists
exactly the routes that exist -- nothing hand-maintained to drift. For every route:
method, path, the permission the authz middleware requires (enterprise/authz.permission_for
-- the same function the middleware calls), whether it needs the dashboard token
(the X-ATIP-Token guard on state-changing routes), query / path parameters with their
types and defaults, and the first paragraph of the handler's docstring. Routes are grouped
by their first path segment. The /api/v1/* alias (ops/http.py) serves the same handlers
and is described once, in the preamble.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime
from pathlib import Path


def _guarded(route) -> bool:
    deps = getattr(route, "dependant", None)
    for d in (deps.dependencies if deps else []):
        name = getattr(d.call, "__name__", "")
        if name in ("_authorised", "authorised") or "token" in name.lower():
            return True
    return False


_ROUTE_LINE = __import__("re").compile(r"^\s*((?:GET|POST|PUT|DELETE|PATCH)(?:\s*\|\s*(?:GET|POST|PUT|DELETE|PATCH))*)"
                                       r"\s+(/[^\s]+)\s*(.*)$")


def _module_descriptions(modules) -> dict:
    """{(METHOD, path): description} from route modules' docstrings, which list their
    endpoints as 'METHOD  /path   description' lines."""
    import importlib
    out = {}
    for m in modules:
        try:
            doc = importlib.import_module(m).__doc__ or ""
        except Exception:
            continue
        for line in doc.splitlines():
            mm = _ROUTE_LINE.match(line)
            if not mm:
                continue
            path = mm.group(2).split("?")[0]
            for meth in mm.group(1).replace(" ", "").split("|"):
                out.setdefault((meth, path), mm.group(3).strip())
    return out


def collect(app=None) -> list:
    if app is None:
        from dashboard.server import app as _app
        app = _app
    from enterprise.authz import permission_for
    out = []
    for r in app.routes:
        methods = sorted(getattr(r, "methods", None) or [])
        path = getattr(r, "path", None)
        if not path or not methods or path.startswith(("/openapi", "/docs", "/redoc")):
            continue
        fn = getattr(r, "endpoint", None)
        doc = inspect.getdoc(fn) or ""
        params = []
        dep = getattr(r, "dependant", None)
        if dep:
            for p in list(dep.path_params) + list(dep.query_params):
                fi = getattr(p, "field_info", None)
                default = getattr(fi, "default", None)
                params.append({"name": p.name, "in": "path" if p in dep.path_params else "query",
                               "type": getattr(getattr(p, "type_", None), "__name__", None) or str(
                                   getattr(p, "annotation", ""))[:30],
                               "default": None if str(default).startswith("PydanticUndefined") else default})
        for m in methods:
            if m == "HEAD":
                continue
            out.append({"method": m, "path": path, "permission": _perm(m, path, permission_for), "token": _guarded(r),
                        "params": params, "summary": doc.split("\n\n")[0].replace("\n", " ")[:300],
                        "handler": getattr(fn, "__module__", "") + "." + getattr(fn, "__name__", "")})
    descs = _module_descriptions({r["handler"].rsplit(".", 1)[0] for r in out})
    for r in out:
        if not r["summary"]:
            r["summary"] = descs.get((r["method"], r["path"]), "")[:300]
    return sorted(out, key=lambda x: (x["path"], x["method"]))


def _perm(method, path, permission_for):
    """What authz really requires: public routes and self-service auth routes need no permission."""
    import re
    from enterprise.authz import PUBLIC, SELF, _public
    if _public(method, re.sub(r"\{[^}]+\}", "x", path)):
        return "public"
    if re.match(SELF, path):
        return "signed-in"
    perm = permission_for(method, path)
    if "{" in path:      # a path parameter PUBLIC spells out by value (GET /health/{component}: /health/wealth ...)
        words = {w for m, rx in PUBLIC if m == method for w in re.findall(r"[a-z0-9_]+", rx)}
        hits = sorted(w for w in words if _public(method, re.sub(r"\{[^}]+\}", w, path)))
        if hits:
            return f"public for {', '.join(hits)}; otherwise {perm}"
    return perm


def write(app=None, out_dir: str = "docs") -> dict:
    if app is None:
        from dashboard.server import app as _app
        app = _app
    routes = collect(app)
    od = Path(out_dir)
    od.mkdir(parents=True, exist_ok=True)
    try:
        spec = app.openapi()
        (od / "openapi.json").write_text(json.dumps(spec, indent=1, default=str), encoding="utf-8")
    except Exception as e:
        spec = {"error": str(e)}
    groups = {}
    for r in routes:
        seg = r["path"].strip("/").split("/")
        key = "/" + "/".join(seg[:2]) if seg and seg[0] == "api" and len(seg) > 1 else "/" + (seg[0] if seg else "")
        groups.setdefault(key, []).append(r)
    lines = ["# ATIP API reference", "",
             f"Generated {datetime.now():%Y-%m-%d %H:%M} by `python -m ops api-docs` from the running application "
             f"({len(routes)} method + path pairs). Do not edit by hand -- regenerate.", "",
             "- **Base URL:** `http://127.0.0.1:8000` (local only until ENT-07). `/api/v1/...` is an alias of every "
             "`/api/...` route (ops/http.py) and adds the `API-Version` header, pagination, sort and filter on list "
             "endpoints, and the standard error envelope `{\"error\": {\"code\", \"message\", \"request_id\"}}`.",
             "- **Auth:** with `enterprise.enabled`, a session cookie or `Authorization: Bearer <api key>`; the "
             "*Permission* column is what the authz middleware requires (enterprise/authz.py). Without enterprise, "
             "the dashboard is single-owner and local.",
             "- **Token:** routes marked *token* change state and need the dashboard token header "
             "`X-ATIP-Token` (dashboard/security.py). Mutating order routes also accept `Idempotency-Key`.",
             "- Full machine-readable schema: [`openapi.json`](openapi.json).", ""]
    for g in sorted(groups):
        lines += [f"## `{g}`", "", "| Method | Path | Permission | Token | Parameters | Summary |",
                  "|---|---|---|---|---|---|"]
        for r in groups[g]:
            ps = ", ".join(f"`{p['name']}`" + (f"={p['default']}" if p.get("default") not in (None, "") else "")
                           for p in r["params"]) or ""
            summ = r["summary"].replace("|", "\\|")
            lines.append(f"| {r['method']} | `{r['path']}` | {r['permission']} | {'token' if r['token'] else ''} | "
                         f"{ps} | {summ} |")
        lines.append("")
    (od / "API_REFERENCE.md").write_text("\n".join(lines), encoding="utf-8")
    return {"routes": len(routes), "groups": len(groups), "files": [str(od / "API_REFERENCE.md"),
                                                                    str(od / "openapi.json")]}
