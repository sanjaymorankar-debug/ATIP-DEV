"""
Resilience primitives for EXTERNAL dependencies (broker data APIs, news feeds,
webhook endpoints, Telegram).

    call_with_timeout(fn, seconds, *a, **k)   runs fn in a worker thread; raises
                                              DependencyTimeout when it overruns (the
                                              thread cannot be killed -- only use for
                                              calls that are safe to abandon)
    retry(attempts=4, base=0.5, cap=30, retry_on=(...))
                                              decorator: exponential backoff with full
                                              jitter; only retryable errors
    CircuitBreaker(name, failure_threshold=5, reset_seconds=60)
                                              CLOSED -> OPEN after N consecutive failures;
                                              OPEN rejects fast (DependencyUnavailable);
                                              after reset_seconds one HALF_OPEN trial
    breaker(name) / breakers()                shared registry (states exported as metrics
                                              and in /health/broker)

ORDERS ARE NEVER RETRIED. retry() refuses to wrap a function marked with
@non_idempotent (the W4 order manager's submit / cancel paths are marked), and
TradingSafetyError is never retryable. Duplicate-order protection is structural
(one order per intent, Idempotency-Key on the API), not retry-based.
"""

from __future__ import annotations

import concurrent.futures
import functools
import random
import threading
import time

from ops.errors import AtipError, DependencyTimeout, DependencyUnavailable, TradingSafetyError

_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="atip-timeout")
RETRYABLE = (DependencyUnavailable, DependencyTimeout, ConnectionError, TimeoutError, OSError)


def non_idempotent(fn):
    fn.__atip_non_idempotent__ = True
    return fn


def call_with_timeout(fn, seconds, *a, **k):
    fut = _POOL.submit(fn, *a, **k)
    try:
        return fut.result(timeout=seconds)
    except concurrent.futures.TimeoutError:
        raise DependencyTimeout(f"{getattr(fn, '__name__', 'call')} exceeded {seconds}s")


def retry(attempts=4, base=0.5, cap=30.0, retry_on=RETRYABLE):
    def deco(fn):
        if getattr(fn, "__atip_non_idempotent__", False):
            raise TypeError(f"{fn.__name__} is non-idempotent (orders): it must not be retried")

        @functools.wraps(fn)
        def wrapper(*a, **k):
            for i in range(attempts):
                try:
                    return fn(*a, **k)
                except TradingSafetyError:
                    raise
                except AtipError as e:
                    if not e.retryable or i == attempts - 1:
                        raise
                except retry_on:
                    if i == attempts - 1:
                        raise
                time.sleep(random.uniform(0, min(cap, base * 2 ** i)))
        return wrapper
    return deco


class CircuitBreaker:
    def __init__(self, name, failure_threshold=5, reset_seconds=60):
        self.name, self.threshold, self.reset = name, failure_threshold, reset_seconds
        self.state, self.failures, self.opened_at = "CLOSED", 0, None
        self._lock = threading.Lock()

    def call(self, fn, *a, **k):
        with self._lock:
            if self.state == "OPEN":
                if time.monotonic() - self.opened_at >= self.reset:
                    self.state = "HALF_OPEN"
                else:
                    raise DependencyUnavailable(f"{self.name} circuit open")
        try:
            out = fn(*a, **k)
        except Exception:
            with self._lock:
                self.failures += 1
                if self.state == "HALF_OPEN" or self.failures >= self.threshold:
                    self.state, self.opened_at = "OPEN", time.monotonic()
            raise
        with self._lock:
            self.state, self.failures = "CLOSED", 0
        return out

    def as_dict(self):
        return {"state": self.state, "failures": self.failures}


_BREAKERS = {}


def breaker(name, **kw) -> CircuitBreaker:
    if name not in _BREAKERS:
        _BREAKERS[name] = CircuitBreaker(name, **kw)
    return _BREAKERS[name]


def breakers() -> dict:
    return dict(_BREAKERS)
