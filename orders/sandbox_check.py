"""
End-to-end SANDBOX verification (BR-08), W29.

    python -m orders.sandbox_check                      read-only checks
    python -m orders.sandbox_check --place-test-order   + one round trip on the sandbox:
                                                        a 1-share LIMIT BUY far below the
                                                        market, then its status, then cancel

Steps (each PASS / FAIL / SKIP, stops at the first FAIL):
    1 credentials   dhan_sandbox_client_id / dhan_sandbox_access_token present (never the
                    production pair -- the sandbox rejects those with DH-906)
    2 redirect      the client's base_url IS the sandbox host. This is the safety property:
                    every later step re-checks it, and nothing runs if it fails
    3 reachable     the sandbox host answers HTTP at all
    4 auth          get_fund_limits() on the sandbox client is accepted
    5 order         (--place-test-order only) place -> status -> cancel on the sandbox

Results are printed and returned; run() is also exposed at GET /api/execution/sandbox-check
(read-only steps only). It never uses the LIVE client, whatever broker_env says.
"""

from __future__ import annotations

import argparse
import json


def _ok(dhan):
    from orders.environment import SANDBOX_BASE_URL
    return str(getattr(getattr(dhan, "dhan_http", None), "base_url", "")).startswith(SANDBOX_BASE_URL)


def run(place_test_order: bool = False, security_id: str = "1333") -> dict:
    from orders.environment import SANDBOX_BASE_URL, broker_env, sandbox_credentials, sandbox_client
    steps = []

    def step(name, status, detail):
        steps.append({"step": name, "status": status, "detail": detail})
        return status != "FAIL"
    res = {"broker_env": broker_env(), "sandbox_url": SANDBOX_BASE_URL, "steps": steps}
    cid, tok = sandbox_credentials()
    if not step("credentials", "PASS" if (cid and tok) else "FAIL",
                "sandbox client id + token present" if (cid and tok) else
                "set dhan_sandbox_client_id and dhan_sandbox_access_token in atip_data/config.json "
                "(ask Dhan for a sandbox token)"):
        return {**res, "result": "NOT_CONFIGURED"}
    try:
        dhan = sandbox_client()
    except Exception as e:
        step("redirect", "FAIL", str(e))
        return {**res, "result": "FAIL"}
    if not step("redirect", "PASS" if _ok(dhan) else "FAIL", f"client base_url = {dhan.dhan_http.base_url}"):
        return {**res, "result": "FAIL"}
    try:
        import requests
        r = requests.get(SANDBOX_BASE_URL.rsplit("/v2", 1)[0] + "/v2/fundlimit", timeout=15,
                         headers={"access-token": "probe", "client-id": "probe"})
        step("reachable", "PASS", f"HTTP {r.status_code} from the sandbox host")
    except Exception as e:
        step("reachable", "FAIL", f"{type(e).__name__}: {e}")
        return {**res, "result": "FAIL"}
    try:
        f = dhan.get_fund_limits()
        txt = json.dumps(f, default=str)[:300]
        if not step("auth", "PASS" if f and f.get("status") != "failure" else "FAIL", txt):
            return {**res, "result": "FAIL"}
    except Exception as e:
        step("auth", "FAIL", f"{type(e).__name__}: {e}")
        return {**res, "result": "FAIL"}
    if not place_test_order:
        step("order", "SKIP", "pass --place-test-order for a place / status / cancel round trip on the sandbox")
        return {**res, "result": "PASS"}
    if not _ok(dhan):                       # re-check right before anything is sent
        step("order", "FAIL", "client is no longer pointed at the sandbox -- refused")
        return {**res, "result": "FAIL"}
    try:
        q = dhan.place_order(security_id=security_id, exchange_segment=dhan.NSE, transaction_type=dhan.BUY,
                             quantity=1, order_type=dhan.LIMIT, product_type=dhan.CNC, price=1.0)
        oid = ((q or {}).get("data") or {}).get("orderId")
        st = dhan.get_order_by_id(oid) if oid else None
        cx = dhan.cancel_order(oid) if oid else None
        ok = bool(oid) and (cx or {}).get("status") == "success"
        step("order", "PASS" if ok else "FAIL", json.dumps({"place": q, "status": st, "cancel": cx}, default=str)[:600])
        return {**res, "result": "PASS" if ok else "FAIL"}
    except Exception as e:
        step("order", "FAIL", f"{type(e).__name__}: {e}")
        return {**res, "result": "FAIL"}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="python -m orders.sandbox_check")
    ap.add_argument("--place-test-order", action="store_true")
    ap.add_argument("--security-id", default="1333", help="sandbox instrument (default 1333 = HDFCBANK on Dhan)")
    a = ap.parse_args()
    out = run(a.place_test_order, a.security_id)
    for s in out["steps"]:
        print(f"  {s['status']:<5} {s['step']:<12} {s['detail']}")
    print(f"  RESULT: {out['result']}")
