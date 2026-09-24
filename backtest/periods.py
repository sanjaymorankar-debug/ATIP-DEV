"""
Research / validation / test separation (BT-03).

    periods = ResearchPeriods(research=("2025-02-01", "2025-12-31"),
                              validation=("2026-01-01", "2026-04-30"),
                              test=("2026-05-01", "2026-09-23"))

Rules this enforces:
  * the three windows are ordered and do not overlap -- evaluation data is
    never inside the research window;
  * a run evaluates exactly one labelled window (research | validation | test |
    full) and the run record carries the label, so a result always says which
    period it describes;
  * the engine only loads data up to the window's end, so nothing after it can
    reach the strategy; bars before the window are available only as warm-up
    history (the past is legitimately known) and no trade opens before it;
  * the test window is out-of-sample: evaluating on it needs allow_test=True,
    and a test run whose parameters differ from an earlier test run of the same
    strategy version on the same window is flagged in its bias report
    (parameters chosen after seeing test results are no longer out-of-sample).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

LABELS = ("research", "validation", "test", "full")


def _d(x) -> date:
    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


@dataclass(frozen=True)
class ResearchPeriods:
    research: tuple
    validation: tuple | None = None
    test: tuple | None = None

    def __post_init__(self):
        wins = [(n, w) for n, w in (("research", self.research), ("validation", self.validation),
                                    ("test", self.test)) if w]
        prev_end = None
        for name, (s, e) in wins:
            s, e = _d(s), _d(e)
            if s > e:
                raise ValueError(f"{name} window starts after it ends ({s} > {e})")
            if prev_end is not None and s <= prev_end:
                raise ValueError(f"{name} window starts on or before the previous window ends "
                                 f"({s} <= {prev_end}): windows must be ordered and not overlap")
            prev_end = e

    def window(self, label: str) -> tuple:
        if label == "full":
            last = self.test or self.validation or self.research
            return _d(self.research[0]), _d(last[1])
        w = getattr(self, label, None) if label in LABELS else None
        if not w:
            raise ValueError(f"no {label!r} window defined")
        return _d(w[0]), _d(w[1])

    def label_of(self, d) -> str | None:
        d = _d(d)
        for name in ("research", "validation", "test"):
            w = getattr(self, name)
            if w and _d(w[0]) <= d <= _d(w[1]):
                return name
        return None

    def as_dict(self) -> dict:
        f = lambda w: [str(_d(w[0])), str(_d(w[1]))] if w else None
        return {"research": f(self.research), "validation": f(self.validation), "test": f(self.test)}

    @classmethod
    def from_dict(cls, d: dict) -> "ResearchPeriods":
        return cls(research=tuple(d["research"]), validation=tuple(d["validation"]) if d.get("validation") else None,
                   test=tuple(d["test"]) if d.get("test") else None)
