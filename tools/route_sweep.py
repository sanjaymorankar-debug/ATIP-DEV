"""
Route sweep for a running dashboard (TEST_SPEC.md S5 / S6 / S7).

    python tools/route_sweep.py http://127.0.0.1:8100            # both checks
    python tools/route_sweep.py http://127.0.0.1:8100 --get      # GET routes only
    python tools/route_sweep.py http://127.0.0.1:8100 --mutating # mutating routes only

Reads the server's own /openapi.json, then:

  --get       calls every GET route that has no path parameter. None may answer 5xx,
              except 503, which is what the enterprise / SaaS routes answer while
              enterprise.enabled is false.
  --mutating  calls every POST / PUT / PATCH / DELETE route WITHOUT the dashboard token
              and without a session, path parameters filled with "x". Every one must
              refuse: 401 (the X-ATIP-Token guard), 503 (enterprise off) or another 4xx.
              A 2xx means anyone who can reach the port can change something.

Only for a throwaway instance: a route that wrongly accepts the call has run. Prints a
JSON summary; exit 1 when either check finds something.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
import urllib.error
import urllib.request

STREAMING = ("/api/stream", "/api/live", "/events")          # long-lived responses, never finish


def _call(base, method, path, timeout):
    req = urllib.request.Request(base + path, method=method, data=b"{}" if method != "GET" else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:                                       # timeouts, resets
        return f"{type(e).__name__}"


def sweep(base, do_get=True, do_mut=True, timeout=90) -> dict:
    base = base.rstrip("/")
    spec = json.load(urllib.request.urlopen(base + "/openapi.json", timeout=timeout))
    out = {"get_routes": 0, "get_status": collections.Counter(), "get_5xx": [],
           "mutating_routes": 0, "mutating_status": collections.Counter(), "mutating_not_refused": []}
    for path, item in sorted(spec["paths"].items()):
        for method in sorted(item):
            m = method.upper()
            if m == "GET" and do_get:
                if "{" in path or path.startswith(("/docs", "/redoc")) or any(s in path for s in STREAMING):
                    continue
                s = _call(base, "GET", path, timeout)
                out["get_routes"] += 1
                out["get_status"][str(s)] += 1
                if not isinstance(s, int) or (s >= 500 and s != 503):
                    out["get_5xx"].append([path, s])
            elif m in ("POST", "PUT", "PATCH", "DELETE") and do_mut:
                s = _call(base, m, path.replace("{", "x").replace("}", ""), timeout)
                out["mutating_routes"] += 1
                out["mutating_status"][str(s)] += 1
                if not isinstance(s, int) or s < 400 or (s >= 500 and s != 503):
                    out["mutating_not_refused"].append([m, path, s])
    out["ok"] = not out["get_5xx"] and not out["mutating_not_refused"]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="sweep a running ATIP dashboard's routes")
    ap.add_argument("base")
    ap.add_argument("--get", action="store_true")
    ap.add_argument("--mutating", action="store_true")
    ap.add_argument("--timeout", type=float, default=90)
    a = ap.parse_args(argv)
    both = not (a.get or a.mutating)
    r = sweep(a.base, do_get=both or a.get, do_mut=both or a.mutating, timeout=a.timeout)
    print(json.dumps(r, indent=1, default=dict))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
