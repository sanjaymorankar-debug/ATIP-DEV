"""
ATIP W7 -- the enterprise layer: users, tenants, RBAC, profiles, API keys,
notifications, workspace, billing, audit.

It wraps the existing platform (W1-W6) without changing it:

    request
      -> enterprise.authz middleware   (one place: authentication, RBAC, tenant
                                        isolation, tenant limits, audit)
      -> existing routes               (unchanged business logic; the W1
                                        X-ATIP-Token guard still applies to them)

Safe by default -- config.json "enterprise": {"enabled": false}:
  * disabled: the middleware passes every request through untouched; ATIP
    behaves exactly as before (single user, dashboard token, 127.0.0.1 bind).
  * enabled: every page and API needs a signed-in principal (session cookie,
    Bearer session token or API key); each route maps to one permission.
Nothing here changes the network binding (dashboard/security.py still binds to
127.0.0.1 unless the owner sets dashboard_host) and nothing enables live
trading: W4's gates remain authoritative, and a tenant / user trading profile
can only tighten them.
"""
