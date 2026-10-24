"""A FAKE kiteconnect for the canary run (TEST_SPEC.md S6.4): every client is recorded and refused."""
from _canary_log import record


class KiteConnect:
    def __init__(self, *a, **k):
        record("kiteconnect.init", type(self).__name__)
        raise RuntimeError("CANARY: KiteConnect")


class KiteTicker(KiteConnect):
    pass
