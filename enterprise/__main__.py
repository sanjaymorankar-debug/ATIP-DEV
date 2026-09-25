"""
    python -m enterprise status
    python -m enterprise bootstrap --username owner [--email x@y]   # prompts for the password
    python -m enterprise add-user USERNAME --tenant default --roles TRADER,VIEWER   # prompts for the password
    python -m enterprise add-tenant TENANT_ID --name "Name" [--plan PRO]
    python -m enterprise tenant-status TENANT_ID SUSPENDED
    python -m enterprise roles USERNAME --tenant T --set RISK_MANAGER
    python -m enterprise reset USERNAME              # issues a one-time reset token
    python -m enterprise audit [--tenant T] [--limit 50]

Passwords are read with getpass (never echoed, never on the command line).
"""

import argparse
import getpass
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _pw():
    a = getpass.getpass("password: ")
    if a != getpass.getpass("repeat:   "):
        raise SystemExit("passwords differ")
    return a


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m enterprise")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    b = sub.add_parser("bootstrap"); b.add_argument("--username", required=True); b.add_argument("--email")
    u = sub.add_parser("add-user"); u.add_argument("username"); u.add_argument("--tenant", default="default")
    u.add_argument("--roles", required=True); u.add_argument("--email")
    t = sub.add_parser("add-tenant"); t.add_argument("tenant_id"); t.add_argument("--name"); t.add_argument("--plan")
    ts = sub.add_parser("tenant-status"); ts.add_argument("tenant_id"); ts.add_argument("status")
    r = sub.add_parser("roles"); r.add_argument("username"); r.add_argument("--tenant", default="default")
    r.add_argument("--set", required=True)
    rs = sub.add_parser("reset"); rs.add_argument("username")
    au = sub.add_parser("audit"); au.add_argument("--tenant"); au.add_argument("--limit", type=int, default=50)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from db.schema import get_connection
    from enterprise import audit, billing, service, tenants, users
    from enterprise.config import settings
    conn = get_connection()
    try:
        service.ensure_seeded(conn)
        if a.cmd == "status":
            out = {"settings": settings(), "tenants": tenants.list_all(conn),
                   "users": [{k: x[k] for k in ("username", "status", "memberships")} for x in users.list_users(conn)]}
        elif a.cmd == "bootstrap":
            out = service.bootstrap(conn, a.username, _pw(), a.email)
        elif a.cmd == "add-user":
            out = users.create_user(conn, a.username, _pw(), a.tenant, a.roles.split(","), email=a.email, actor="cli")
        elif a.cmd == "add-tenant":
            out = tenants.create(conn, a.tenant_id, a.name or a.tenant_id, actor="cli")
            if a.plan:
                billing.subscribe(conn, a.tenant_id, a.plan, actor="cli")
        elif a.cmd == "tenant-status":
            out = tenants.set_status(conn, a.tenant_id, a.status, actor="cli")
        elif a.cmd == "roles":
            usr = users.by_username(conn, a.username) or {}
            out = users.set_roles(conn, usr.get("user_id"), a.tenant, a.set.split(","), actor="cli")
        elif a.cmd == "reset":
            usr = users.by_username(conn, a.username) or {}
            out = users.issue_reset(conn, usr.get("user_id"), actor="cli")
        elif a.cmd == "audit":
            out = audit.query(conn, a.tenant, limit=a.limit)
    finally:
        conn.close()
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
