"""
Is ATIP's Dhan access token good for today? The banner every dashboard page carries
(dashboard/global_asia.py) puts the answer up in red when it is "no" -- owner, 30 Oct 2026: the
token had been dead since 29 Oct with nothing on the dashboard saying so.

status() reads local state only, so every open page can ask it each minute without a call to Dhan:
  * the token's own expiry: Dhan's tokens are JWTs whose payload carries "exp" and "iat";
  * Dhan's last word on it: a DH-901 in portfolio_sync after the token was issued means Dhan
    refused it before its expiry;
  * the log tools/dhan_token_refresh.py writes: the last attempt, the codes Dhan rejected today
    and the next automatic try.

    state   OK        works and lasts past 18:00 today (the trading day and the 16:45 post-market
                      run) -- or it is before the 06:30 refresh, which replaces yesterday's token
            EXPIRING  works now, but runs out before 18:00 today and the morning refresh did not
                      replace it
            EXPIRED   past its expiry, or refused by Dhan (DH-901)
            MISSING   no token saved
            UNKNOWN   no readable expiry and no DH-901: nothing to say
"""
import base64
import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROBLEM = {"EXPIRED": "error", "MISSING": "error", "EXPIRING": "warn"}
_tool = None


def _refresh_tool():
    """tools/dhan_token_refresh.py (a script, not a package): its log, schedule and limits."""
    global _tool
    if _tool is None:
        spec = importlib.util.spec_from_file_location("dhan_token_refresh", ROOT / "tools" / "dhan_token_refresh.py")
        _tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_tool)
    return _tool


def _token():
    """The token ATIP's Dhan calls use: config.json, else the environment / vault (data/dhan.py)."""
    from data.dhan import load_dhan_config
    return load_dhan_config().get("dhan_access_token") or ""


def _claims(tok) -> dict:
    try:
        part = tok.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        return {}


def _when(epoch):
    try:
        return datetime.fromtimestamp(float(epoch))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _refused_at(issued):
    """When Dhan last answered DH-901 -- if that is its latest word and came after the token was issued."""
    from db.schema import get_connection
    conn = get_connection()
    try:
        r = conn.execute("SELECT status, error, synced_at FROM portfolio_sync WHERE source = 'dhan' "
                         "ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    if not r or r[0] == "SUCCESS" or "DH-901" not in str(r[1] or ""):
        return None
    try:
        at = datetime.fromisoformat(str(r[2]).replace("T", " "))
    except ValueError:
        return None
    return at if issued is None or at > issued else None


def _log():
    """(last attempt as {"at", "message"}, last successful refresh time) from the refresh log."""
    try:
        lines = _refresh_tool().LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None, None
    last = next((l for l in reversed(lines) if l[21:] and not l[21:].startswith(("alert", "token check"))), None)
    ok = next((l for l in reversed(lines) if "  OK: " in l), None)
    return ({"at": last[:16], "message": last[21:]} if last else None), (ok[:16] if ok else None)


def _fix(cfg_vaulted, tries_left):
    out = []
    if tries_left > 0:
        out.append({"text": "Try the automatic login again now, in the ATIP folder:", "cmd": "tools/dhan_token_refresh.sh"})
    if cfg_vaulted:
        out.append({"text": ("Or generate" if out else "Generate") + " a token at web.dhan.co, then run this in "
                    "the ATIP folder and paste it:", "cmd": ".venv/bin/python -m ops vault-set DHAN_ACCESS_TOKEN"})
    else:
        out.append({"text": ("Or generate" if out else "Generate") + " a token at web.dhan.co and paste it as "
                    "dhan_access_token in atip_data/config.json.", "cmd": None})
    return out


def status(now=None) -> dict:
    now = now or datetime.now()
    t = _refresh_tool()
    try:
        tok = _token()
    except Exception:
        tok = ""
    claims = _claims(tok)
    exp, iat = _when(claims.get("exp")), _when(claims.get("iat"))
    try:
        refused = _refused_at(iat) if tok else None
    except Exception:
        refused = None
    first = now.replace(hour=t.SLOTS[0][0], minute=t.SLOTS[0][1], second=0, microsecond=0) + timedelta(minutes=5)
    day_end = now.replace(hour=t.DAY_END[0], minute=t.DAY_END[1], second=0, microsecond=0)
    if not tok:
        state = "MISSING"
    elif refused or (exp and exp <= now):
        state = "EXPIRED"
    elif exp and exp <= day_end and now >= first:
        state = "EXPIRING"
    else:
        state = "OK" if exp else "UNKNOWN"

    last, last_ok = _log()
    rejected = t.rejected_today()
    left = t.MAX_REJECTED_A_DAY - rejected
    nxt = t.next_slot(now) if left > 0 else None
    out = {"state": state, "severity": PROBLEM.get(state),
           "expires_at": exp.isoformat(timespec="minutes") if exp else None,
           "refused_at": refused.isoformat(timespec="minutes") if refused else None,
           "last_attempt": last, "last_ok": last_ok, "rejected_today": rejected, "next_try": nxt,
           "message": None, "fix": []}
    if state not in PROBLEM:
        return out

    if state == "MISSING":
        head = "No Dhan token saved: ATIP has no live prices and cannot trade."
    elif exp and exp <= now:
        head = f"Dhan token expired {exp:%d %b %H:%M}: ATIP has no live prices and cannot trade."
    elif refused:
        head = f"Dhan refused ATIP's token (DH-901) at {refused:%d %b %H:%M}: ATIP has no live prices and cannot trade."
    else:
        head = f"Dhan token runs out at {exp:%H:%M} today and the morning refresh did not replace it."
    if last:
        at = datetime.strptime(last["at"], "%Y-%m-%d %H:%M")
        head += f" Last automatic refresh {at:%d %b %H:%M}: {last['message'].rstrip('.')}."
    if left <= 0:
        head += (f" Dhan has rejected {rejected} login codes today, so ATIP will not try again today "
                 "(Dhan locks the login after 5).")
    else:
        head += f" Next automatic try at {nxt}." if nxt else " No more automatic tries today."
    try:
        vaulted = t._vaulted(json.loads(t.CFG.read_text(encoding="utf-8")))
    except Exception:
        vaulted = True
    out["message"], out["fix"] = head, _fix(vaulted, left)
    return out
