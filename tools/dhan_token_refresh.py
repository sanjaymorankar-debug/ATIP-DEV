"""
Refresh the Dhan access token and write it into atip_data/config.json -- or, once the credentials
were moved to the encrypted vault (python -m ops vault-migrate --apply), back into the vault.

Scheduled by:
  macOS    the LaunchAgent com.atip.dhan-token-refresh (deploy/launchd/install.sh): daily at
           06:30, before the 07:00 pre-market pipeline makes the day's first Dhan call, again at
           08:00 and 09:00 in case that failed, and at login so a Mac that was off at 06:30 still
           gets a token. A run whose token works and lasts past 18:00 today does nothing, so the
           08:00 / 09:00 / login runs only act when the 06:30 one did not.
  Windows  the scheduled task "ATIP_DhanTokenRefresh", daily at 08:00.
Stdlib only (the failure alert uses ATIP's alerts/telegram when it can be imported).

Order of attempts:
  1. GET  https://api.dhan.co/v2/RenewToken   (works while the current token is
     still valid; gives a fresh 24h token)
  2. POST https://auth.dhan.co/app/generateAccessToken  (works even when the
     token has expired; needs dhan_pin + dhan_totp_secret in config.json, or
     DHAN_PIN + DHAN_TOTP_SECRET in ATIP's secret store)
     Dhan sometimes answers a correct code with "Invalid TOTP" (8 of 16 mornings in Oct 2026,
     with the Mac's clock and the secret right), so a rejected code is retried with the next one:
     TRIES_PER_RUN codes a run, and at most MAX_REJECTED_A_DAY rejected codes a day across all runs,
     as Dhan locks the TOTP login after 5 wrong codes in a row.
A run that leaves ATIP without a fresh token tells the owner (Telegram + the dashboard's alert
list), with the next automatic try and how to save a token by hand.

One-time setup -- add to atip_data/config.json:
    "dhan_pin": "<your 6-digit Dhan trading PIN>",
    "dhan_totp_secret": "<base32 secret shown when you enable TOTP at web.dhan.co>"
(Dhan: web.dhan.co > profile > DhanHQ Trading APIs > enable TOTP.)

Usage:  python tools/dhan_token_refresh.py [--check | --force]
        --force refreshes even a token that lasts the day (the daily cap on rejected codes still holds)
Exit 0 = token is valid/updated, 1 = failed.  Log: atip_data/dhan_token_refresh.log
"""
import base64, hashlib, hmac, json, os, struct, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "atip_data" / "config.json"
LOG = ROOT / "atip_data" / "dhan_token_refresh.log"

SLOTS = ((6, 30), (8, 0), (9, 0))   # the LaunchAgent's runs (deploy/launchd/com.atip.dhan-token-refresh.plist)
DAY_END = (18, 0)                   # a token that lasts past this needs no refresh today
TRIES_PER_RUN = 2                   # login codes one run may send
MAX_REJECTED_A_DAY = 4              # Dhan locks the TOTP login after 5 wrong codes in a row
REJECTED = "Invalid TOTP"


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


def expires_at(tok):
    """When the token runs out: Dhan's tokens are JWTs whose payload carries "exp". None if unreadable."""
    try:
        part = tok.split(".")[1]
        return datetime.fromtimestamp(json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))["exp"])
    except Exception:
        return None


def lasts_the_day(cid, tok, now=None):
    """The token works and runs past today's DAY_END -- the trading day and the 16:45 post-market run --
    so there is nothing to do. At 06:30 yesterday's 06:30 token is seconds from expiring."""
    now = now or datetime.now()
    exp = expires_at(tok) if tok else None
    if not exp or exp <= max(now, now.replace(hour=DAY_END[0], minute=DAY_END[1], second=0, microsecond=0)):
        return False
    return token_valid(cid, tok)


def _since_ok_today():
    """Today's log lines after the last successful refresh."""
    today, out = f"{datetime.now():%Y-%m-%d} ", []
    try:
        lines = LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        if line.startswith(today):
            out = [] if "  OK: " in line else out + [line]
    return out


def rejected_today():
    """Login codes Dhan rejected today since the last successful refresh. Read back from the log, as
    the runs share Dhan's lockout of 5 wrong codes in a row."""
    return sum(REJECTED in line for line in _since_ok_today())


def next_slot(now=None):
    """The next scheduled run today as "08:00", or None after the last one."""
    now = now or datetime.now()
    later = [f"{h:02d}:{m:02d}" for h, m in SLOTS if (h, m) > (now.hour, now.minute)]
    return later[0] if later else None


def renew(cid, tok):
    # dhanClientId is the client-id header Dhan documents for RenewToken (was dataAccess-clientId).
    s, b = http("GET", "https://api.dhan.co/v2/RenewToken", {"access-token": tok, "dhanClientId": cid})
    new = (b.get("accessToken") or b.get("access_token")) if s == 200 else None
    if not new:
        log(f"RenewToken failed: HTTP {s} {b.get('errorMessage') or b.get('message') or b.get('status') or b}")
    return new


def generate(cid, pin, secret, tries=1):
    """A token from PIN + TOTP, sending up to `tries` codes: a code Dhan rejects as "Invalid TOTP" is
    followed by the next one, 30 seconds later. Any other answer ends the run's attempts."""
    for n in range(1, tries + 1):
        q = urllib.parse.urlencode({"dhanClientId": cid, "pin": pin, "totp": totp(secret)})
        s, b = http("POST", "https://auth.dhan.co/app/generateAccessToken?" + q)
        tok = b.get("accessToken")
        if tok:
            return tok
        why = b.get('message') or b.get('status') or b
        log(f"generateAccessToken failed: HTTP {s} {why}" + (f" (code {n} of {tries})" if tries > 1 else ""))
        if REJECTED not in str(why) or n == tries:
            return None
        time.sleep(30 - time.time() % 30 + 2)          # into the next 30-second window: a fresh code
    return None


def _vaulted(cfg) -> bool:
    """W38: the credentials were moved to the encrypted vault (python -m ops vault-migrate): config.json
    holds the keys blank and the token must be saved back to the vault, never to config.json."""
    return "dhan_access_token" in cfg and not cfg.get("dhan_access_token")


def secret(cfg, key, name):
    """config.json value when present (legacy), else ATIP's secret store (env / secrets / vault)."""
    if cfg.get(key):
        return cfg[key]
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from ops.secrets import get
        return get(name, log_access=False) or ""
    except Exception as e:
        log(f"secret store unavailable for {name}: {type(e).__name__}")
        return ""


def save(cfg, tok):
    if _vaulted(cfg):
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from ops.vault import get, set_secret
        set_secret("DHAN_ACCESS_TOKEN", tok)
        if get("DHAN_ACCESS_TOKEN") != tok:
            raise RuntimeError("vault verification failed; the old token is still in place")
        return "vault"
    bak = CFG.with_suffix(".json.bak-tokenrefresh")
    bak.write_text(CFG.read_text(encoding="utf-8"), encoding="utf-8")
    cfg["dhan_access_token"] = tok
    tmp = CFG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, CFG)
    return "config.json"


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def alert(title, body, ok=False):
    """Tell the owner: Telegram and the dashboard's alert list (alerts/telegram.notify). Never fails the run."""
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from alerts.telegram import fmt, notify
        notify(fmt("✅" if ok else "❌", title, body), category="job", severity="info" if ok else "error")
        log(f"alert: {title}")
    except Exception as e:
        log(f"alert not sent ({title}): {type(e).__name__}")


def failed():
    """After a run that leaves ATIP without a fresh token: why, what happens next, and how to save one by hand."""
    try:
        why = LOG.read_text(encoding="utf-8").splitlines()[-1][21:]       # the last message, after its timestamp
    except (OSError, IndexError):
        why = "unknown"
    nxt, left = next_slot(), MAX_REJECTED_A_DAY - rejected_today()
    if left <= 0:
        then = (f"No more automatic tries today: Dhan has rejected {MAX_REJECTED_A_DAY} codes and locks "
                "the login after 5.")
    else:
        then = f"Next automatic try at {nxt}." if nxt else "No more automatic tries today."
    try:
        cfg = json.loads(CFG.read_text(encoding="utf-8"))
        exp = expires_at(secret(cfg, "dhan_access_token", "DHAN_ACCESS_TOKEN"))
        where = ("then run <code>.venv/bin/python -m ops vault-set DHAN_ACCESS_TOKEN</code> in the ATIP "
                 "folder and paste it" if _vaulted(cfg) else "then paste it as dhan_access_token in atip_data/config.json")
    except Exception:
        exp, where = None, "then save it in ATIP"
    state = (f"The current token runs out {exp:%d %b %H:%M}." if exp and exp > datetime.now()
             else "ATIP has no live prices and cannot trade until a new token is saved.")
    alert("Dhan token not refreshed", f"{_esc(why)}\n\n{state} {then}\n\n"
                                      f"By hand: generate a token at web.dhan.co, {where}.")


def main():
    os.chdir(ROOT)                      # ops/* resolve atip_data/ relative to the repository
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    cid = secret(cfg, "dhan_client_id", "DHAN_CLIENT_ID")
    tok = secret(cfg, "dhan_access_token", "DHAN_ACCESS_TOKEN")
    if not cid:
        log("dhan_client_id missing in config.json"); return 1
    if "--check" in sys.argv:
        ok = bool(tok) and token_valid(cid, tok)
        log(f"token check: {'VALID' if ok else 'INVALID/EXPIRED'}"); return 0 if ok else 1
    if "--force" not in sys.argv and lasts_the_day(cid, tok):
        log(f"token valid until {expires_at(tok):%d %b %H:%M}; nothing to do"); return 0

    new = renew(cid, tok) if tok else None
    how = "RenewToken"
    if not new:
        pin, sec = secret(cfg, "dhan_pin", "DHAN_PIN"), secret(cfg, "dhan_totp_secret", "DHAN_TOTP_SECRET")
        if not (pin and sec):
            log("RenewToken did not give a token and dhan_pin / dhan_totp_secret are not set "
                "(config.json or DHAN_PIN / DHAN_TOTP_SECRET in the secret store) -- cannot auto-generate")
            failed(); return 1
        rejected = rejected_today()
        tries = min(TRIES_PER_RUN, MAX_REJECTED_A_DAY - rejected)
        if tries <= 0:
            log(f"no login code sent: Dhan has rejected {rejected} today and locks the login after 5 in a row")
            failed(); return 1
        new, how = generate(cid, pin, sec, tries), "generateAccessToken"
    if not new:
        failed(); return 1
    if not token_valid(cid, new):
        log(f"{how} returned a token that failed the profile check; config NOT changed")
        failed(); return 1
    where = save(cfg, new)
    told = any("alert: Dhan token not refreshed" in line for line in _since_ok_today())
    log(f"OK: new Dhan token saved via {how} (to {where})")
    if told:
        exp = expires_at(new)
        alert("Dhan token refreshed", "A new token is saved" + (f", valid until {exp:%d %b %H:%M}" if exp else "")
              + ". ATIP's live prices and trading resume.", ok=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}"); failed(); sys.exit(1)
