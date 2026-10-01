"""
Polite NSE JSON / archive client shared by the W27 corporate-data fetchers
(data/nse_filings.py, data/institutional.py, data/derivatives.py).

NSE's www host needs the cookies its home page sets (data/bhavcopy.get_nse_session
does that), answers 401/403 once they expire, and throttles bursts. This wraps one
session with:
  * a minimum gap between requests (MIN_GAP_S) so a 500-symbol sweep stays polite;
  * one cookie refresh + retry on 401/403, and a backoff retry on 429/5xx;
  * JSON decoding that returns None (never raises) when NSE sends an HTML page.
Nothing here writes to the database.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

MIN_GAP_S = 0.35


class _Failed(Exception):
    """All attempts failed -- counted by the breaker, returned to callers as None."""
TIMEOUT_S = 25
RETRIES = 3


class NseClient:
    def __init__(self, min_gap: float = MIN_GAP_S):
        self.min_gap = float(min_gap)
        self._session = None
        self._last = 0.0
        self._lock = threading.Lock()

    def _fresh(self):
        from data.bhavcopy import get_nse_session
        self._session = get_nse_session()
        return self._session

    def session(self):
        return self._session or self._fresh()

    def _wait(self):
        with self._lock:
            gap = time.monotonic() - self._last
            if gap < self.min_gap:
                time.sleep(self.min_gap - gap)
            self._last = time.monotonic()

    def get(self, url: str, accept_json: bool = True):
        """The response (status 200) or None after RETRIES attempts. W31 (OPS-10): behind the
        "nse" circuit breaker -- after 8 consecutive failed fetches it fails fast (None) for
        180 s instead of retrying every symbol of a 500-symbol sweep against a dead host."""
        from ops.errors import DependencyUnavailable
        from ops.resilience import breaker
        try:
            return breaker("nse", failure_threshold=8, reset_seconds=180).call(self._get, url, accept_json)
        except DependencyUnavailable:
            return None
        except _Failed:
            return None

    def _get(self, url: str, accept_json: bool = True):
        from data.bhavcopy import HEADERS
        headers = {**HEADERS, "Accept": "application/json"} if accept_json else HEADERS
        refreshed = False
        for attempt in range(RETRIES):
            self._wait()
            try:
                r = self.session().get(url, timeout=TIMEOUT_S, headers=headers)
            except Exception as e:
                log.debug(f"  NSE {url[:80]}: {e}")
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 200:
                return r
            if r.status_code in (401, 403) and not refreshed:
                refreshed = True
                self._fresh()
                continue
            if r.status_code == 404:
                return None                      # a missing file is an answer, not a failure
            time.sleep(2.0 * (attempt + 1))
        raise _Failed(url)

    def json(self, url: str):
        r = self.get(url)
        if r is None:
            return None
        try:
            return r.json()
        except ValueError:
            return None

    def text(self, url: str):
        r = self.get(url, accept_json=False)
        return r.text if r is not None else None

    def content(self, url: str):
        r = self.get(url, accept_json=False)
        return r.content if r is not None else None


_client = None


def client() -> NseClient:
    global _client
    if _client is None:
        _client = NseClient()
    return _client
