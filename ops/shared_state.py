"""
Shared state for multi-process deployments (W9, ENT-16 foundation).

    store()                 the configured store: ops.shared_state_url
                            "memory" (default: in-process, what a single ATIP process needs)
                            "redis://host:6379/0" (needs the `redis` package -- NOT installed;
                            configuring it without the package fails at startup validation)
    Store.bucket(key, per_minute) -> (allowed, retry_after_s)    token bucket (rate limits)
    Store.incr(key, ttl_s) -> int                                 counters with expiry
Used by the W8 rate limiter (ops/http.py) and the W9 per-API-key limits
(enterprise/authz.py). Idempotency keys, job locks and the scheduler leader lease are
database-backed already, so they work across processes without this store.
"""

from __future__ import annotations

import threading
import time


class MemoryStore:
    name = "memory"

    def __init__(self):
        self._b, self._c, self._lock = {}, {}, threading.Lock()

    def bucket(self, key, per_minute) -> tuple:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._b.get(key, (float(per_minute), now))
            tokens = min(per_minute, tokens + (now - last) * per_minute / 60.0)
            if tokens < 1:
                self._b[key] = (tokens, now)
                return False, int((1 - tokens) * 60 / per_minute) + 1
            self._b[key] = (tokens - 1, now)
            if len(self._b) > 100000:
                self._b.clear()
            return True, 0

    def incr(self, key, ttl_s=86400) -> int:
        now = time.time()
        with self._lock:
            v, exp = self._c.get(key, (0, now + ttl_s))
            if exp < now:
                v, exp = 0, now + ttl_s
            self._c[key] = (v + 1, exp)
            return v + 1


class RedisStore:
    name = "redis"

    def __init__(self, url):
        import redis                          # not installed in W9; see module docstring
        self.r = redis.Redis.from_url(url)

    def bucket(self, key, per_minute) -> tuple:
        k = f"atip:rl:{key}:{int(time.time() // 60)}"
        n = self.r.incr(k)
        if n == 1:
            self.r.expire(k, 70)
        return (True, 0) if n <= per_minute else (False, 60 - int(time.time()) % 60)

    def incr(self, key, ttl_s=86400) -> int:
        k = f"atip:c:{key}"
        n = self.r.incr(k)
        if n == 1:
            self.r.expire(k, ttl_s)
        return int(n)


_STORE = {"s": None}


def store():
    if _STORE["s"] is None:
        url = "memory"
        try:
            from ops.config import ops
            url = str(ops().get("shared_state_url") or "memory")
        except Exception:
            pass
        _STORE["s"] = RedisStore(url) if url.startswith("redis://") else MemoryStore()
    return _STORE["s"]
