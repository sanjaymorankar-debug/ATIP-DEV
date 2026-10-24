"""A FAKE dhanhq for the canary run (TEST_SPEC.md S6.4) -- never a broker.

CI does not install dhanhq, so code behind `import dhanhq` never runs there. The owner's
Mac has the real SDK and real credentials. This stand-in takes the real one's place so
those code paths do run, and records every client built and every method called; each
call then fails. tests/canary/report.py turns the record into a verdict."""
from _canary_log import record


class _Refuse:
    def __init__(self, *a, **k):
        record("dhanhq.init", type(self).__name__)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def _call(*a, **k):
            record("dhanhq.call", name)
            raise RuntimeError(f"CANARY: dhanhq.{name} called")
        return _call


class dhanhq(_Refuse):          # noqa: N801 -- the SDK's own name
    pass


class DhanContext(_Refuse):
    pass


class DhanHQ(_Refuse):
    pass


from . import marketfeed  # noqa: E402,F401
