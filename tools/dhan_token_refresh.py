"""
Refresh the Dhan access token and write it into atip_data/config.json.

Run daily at 08:00 by the Windows scheduled task "ATIP_DhanTokenRefresh".
Stdlib only.

Order of attempts:
  1. GET  https://api.dhan.co/v2/RenewToken   (works while the current token is
     still valid; gives a fresh 24h token)
  2. POST https://auth.dhan.co/app/generateAccessToken  (works even when the
     token has expired; needs dhan_pin + dhan_totp_secret in config.json)

One-time setup -- add to atip_data/config.json:
    "dhan_pin": "<your 6-digit Dhan trading PIN>",
    "dhan_totp_secret": "<base32 secret shown when you enable TOTP at web.dhan.co>"
(Dhan: web.dhan.co > profile > DhanHQ Trading APIs > enable TOTP.)

Usage:  python tools/dhan_token_refresh.py [--check]
Exit 0 = token is valid/updated, 1 = failed.  Log: atip_data/dhan_token_refresh.log
"""
import base64, hashlib, hmac, json, os, struct, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "atip_data" / "config.json"
LOG = ROOT / "atip_data" / "dhan_token_refresh.log"


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def totp(secret, step=30, digits=6):
    key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret.replace(" ", "")) % 8))
    msg = struct.pack(">Q", int(time.time()) // step)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** digits).zfill(digits)


def http(method, url, headers=None):
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode() or "{}")
        except Exception:
            body = {}
        return e.code, body


def token_valid(cid, tok):
    s, _ = http("GET", "https://api.dhan.co/v2/profile", {"access-token": tok, "client-id": cid})
    return s == 200


def renew(cid, tok):
    s, b = http("GET", "https://api.dhan.co/v2/RenewToken", {"access-token": tok, "dataAccess-clientId": cid})
    return b.get("accessToken") or b.get("access_token") if s == 200 else None


def generate(cid, pin, secret):
    q = urllib.parse.urlencode({"dhanClientId": cid, "pin": pin, "totp": totp(secret)})
    s, b = http("POST", "https://auth.dhan.co/app/generateAccessToken?" + q)
    tok = b.get("accessToken")
    if not tok:
        log(f"generateAccessToken failed: HTTP {s} {b.get('message') or b.get('status') or b}")
    return tok


def save(cfg, tok):
    bak = CFG.with_suffix(".json.bak-tokenrefresh")
    bak.write_text(CFG.read_text(encoding="utf-8"), encoding="utf-8")
    cfg["dhan_access_token"] = tok
    tmp = CFG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, CFG)


def main():
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    cid, tok = cfg.get("dhan_client_id", ""), cfg.get("dhan_access_token", "")
    if not cid:
        log("dhan_client_id missing in config.json"); return 1
    if "--check" in sys.argv:
        ok = bool(tok) and token_valid(cid, tok)
        log(f"token check: {'VALID' if ok else 'INVALID/EXPIRED'}"); return 0 if ok else 1

    new = renew(cid, tok) if tok else None
    how = "RenewToken"
    if not new:
        pin, sec = cfg.get("dhan_pin", ""), cfg.get("dhan_totp_secret", "")
        if not (pin and sec):
            log("Token expired and dhan_pin / dhan_totp_secret are not in config.json -- cannot auto-generate"); return 1
        new, how = generate(cid, pin, sec), "generateAccessToken"
    if not new:
        return 1
    if not token_valid(cid, new):
        log(f"{how} returned a token that failed the profile check; config NOT changed"); return 1
    save(cfg, new)
    log(f"OK: new Dhan token saved via {how}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}"); sys.exit(1)
