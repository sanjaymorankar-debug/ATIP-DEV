"""
E-mail verification and self-service password reset (W9, W7-R5) -- through the mail
adapter (enterprise/channels.py EmailAdapter): SANDBOX by default, so the message
lands in atip_data/outbox/<delivery>.eml on the owner's machine, never a real inbox.

    send_verification(conn, user_id)   one-time token (digest stored, 24 h) mailed to the
                                       user's address; verify(conn, token) sets
                                       enterprise_user.email_verified_at
    forgot_password(conn, identifier)  username or e-mail -> a W7 reset token (30 min) mailed
                                       to a VERIFIED address. Always returns the same generic
                                       answer (no account enumeration); nothing is mailed to
                                       an unverified address
Only verified addresses receive e-mail notifications (channels._destination).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from enterprise import audit
from enterprise import security as S

GENERIC = {"status": "if the account exists and has a verified e-mail, a reset link was sent"}


def _mail(conn, user_id, to, subject, body):
    from enterprise.channels import ADAPTERS, mask
    did = "nd_" + uuid.uuid4().hex[:16]
    st, err = ADAPTERS["email"].send(to, subject, body, did)
    conn.execute("INSERT INTO enterprise_notification_delivery (delivery_id,user_id,channel,destination_masked,subject,"
                 "status,mode,error,created_at,sent_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (did, user_id, "email", mask(to), subject, st, "transactional", err, datetime.now(),
                  datetime.now() if st in ("SENT", "SANDBOX") else None))
    return st


def send_verification(conn, user_id) -> dict:
    from enterprise.channels import settings
    r = conn.execute("SELECT email, email_verified_at FROM enterprise_user WHERE user_id=?", (user_id,)).fetchone()
    if not r or not r[0]:
        raise ValueError("no e-mail address on the profile")
    if r[1]:
        return {"status": "already verified"}
    tok = S.new_secret("atv_")
    conn.execute("INSERT INTO enterprise_email_token (token_hash,user_id,purpose,email,expires_at,created_at) VALUES "
                 "(?,?,?,?,?,?)", (S.digest(tok), user_id, "verify", r[0], datetime.now() + timedelta(hours=24),
                                   datetime.now()))
    st = _mail(conn, user_id, r[0], "Verify your ATIP e-mail address",
               f"Confirm this address for ATIP notifications:\n{settings()['base_url']}/app?verify={tok}\n\n"
               f"The link expires in 24 hours. If you did not ask for this, ignore it.")
    audit.record(conn, "user.email_verification_sent", user_id=user_id, actor=user_id, commit=False)
    conn.commit()
    return {"status": st}


def verify(conn, token) -> dict:
    r = conn.execute("SELECT user_id, email, expires_at, used_at FROM enterprise_email_token WHERE token_hash=? AND "
                     "purpose='verify'", (S.digest(token or ""),)).fetchone()
    if not r or r[3] or str(r[2]) < str(datetime.now()):
        raise ValueError("verification link invalid or expired")
    cur = conn.execute("SELECT email FROM enterprise_user WHERE user_id=?", (r[0],)).fetchone()
    if not cur or cur[0] != r[1]:
        raise ValueError("the address changed since the link was sent")
    conn.execute("UPDATE enterprise_user SET email_verified_at=? WHERE user_id=?", (datetime.now(), r[0]))
    conn.execute("UPDATE enterprise_email_token SET used_at=? WHERE token_hash=?", (datetime.now(), S.digest(token)))
    audit.record(conn, "user.email_verified", user_id=r[0], actor=r[0], commit=False)
    conn.commit()
    return {"status": "e-mail verified"}


def forgot_password(conn, identifier, ip=None) -> dict:
    from enterprise.channels import settings
    from enterprise.users import issue_reset
    ident = (identifier or "").strip().lower()
    if not ident:
        return GENERIC
    r = conn.execute("SELECT user_id, email, email_verified_at, status FROM enterprise_user WHERE LOWER(username)=? OR "
                     "LOWER(email)=?", (ident, ident)).fetchone()
    audit.record(conn, "auth.forgot_password", user_id=r[0] if r else None, actor=ident[:40], ip=ip,
                 details={"matched": bool(r)})
    if not r or not r[1] or not r[2] or r[3] in ("DISABLED",):
        return GENERIC
    out = issue_reset(conn, r[0], actor="self-service")
    _mail(conn, r[0], r[1], "Reset your ATIP password",
          f"Use this one-time code on the sign-in page (Reset) within 30 minutes:\n{out['reset_token']}\n\n"
          f"{settings()['base_url']}/login\nIf you did not ask for this, ignore it; your password is unchanged.")
    conn.commit()
    return GENERIC
