"""
The core daily schedule: what start_scheduler() actually registers.

ATIP's day hangs on three IST times -- the 07:00 pre-market run, the 15-minute
intraday refresh whose last slot is 15:30 (the close), and the 16:45 post-market
run (after NSE publishes the Bhavcopy, ~16:33). tests/test_signal_pipeline.py
pins the POSTMARKET_RUN_TIME constant and tests/test_w39_history.py the W39 jobs,
but nothing checked that start_scheduler() registers these jobs at these times.

CI leaves the `schedule` package out, so a recorder stands in for it, and the
scheduler's start-up catch-ups (network jobs) are stubbed. The scheduler reads
the machine's local clock: the IST part is the TZ=Asia/Kolkata every launcher
sets (the Mac's LaunchAgent here; the container image in
tests/test_w38_platform_compliance.py).
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


class _Stop(Exception):
    """Raised where start_scheduler() would enter its endless run loop."""


class _Job:
    def __init__(self, jobs, interval):
        self.jobs, self.interval, self.unit, self.time = jobs, interval, None, None

    def __getattr__(self, unit):                 # .day, .saturday, .minutes, ...
        if unit.startswith("_"):
            raise AttributeError(unit)
        self.unit = unit
        return self

    def at(self, t):
        self.time = t
        return self

    def do(self, fn, *args, **kwargs):
        self.jobs.append((self.unit, self.time, fn, args))
        return self


class _Schedule:
    def __init__(self):
        self.jobs = []

    def every(self, interval=1):
        return _Job(self.jobs, interval)


@pytest.fixture
def registered(monkeypatch):
    from pipeline import scheduler as S
    import ops.jobs
    fake = _Schedule()
    monkeypatch.setattr(S, "schedule", fake, raising=False)
    monkeypatch.setattr(S, "HAS_SCHEDULE", True)
    for name in ("run_startup_market_catchup", "run_postmarket_if_missing", "run_fii_dii_watch",
                 "run_morning_catchup", "run_health_check"):
        monkeypatch.setattr(S, name, lambda *a, **k: None)
    monkeypatch.setattr(S, "is_market_day", lambda *a, **k: False)

    def stop(*a, **k):
        raise _Stop()
    monkeypatch.setattr(ops.jobs, "leader_lease", stop)
    with pytest.raises(_Stop):
        S.start_scheduler()
    return S, fake.jobs


def _times(jobs, fn, unit="day"):
    return [t for u, t, f, _ in jobs if f is fn and u == unit]


def test_premarket_runs_at_0700(registered):
    S, jobs = registered
    assert _times(jobs, S.run_premarket) == ["07:00"]


def test_the_intraday_refresh_runs_every_15_minutes_from_0915_to_the_1530_close(registered):
    S, jobs = registered
    slots = _times(jobs, S.run_intraday_15min)
    want = [f"{h:02d}:{m:02d}" for h in range(9, 16) for m in (0, 15, 30, 45)
            if (h, m) >= (9, 15) and (h, m) <= (15, 30)]
    assert slots == want
    assert slots[0] == "09:15" and slots[-1] == "15:30"


def test_postmarket_runs_at_1645_with_its_catch_up_after_it(registered):
    S, jobs = registered
    assert S.POSTMARKET_RUN_TIME == "16:45"
    assert _times(jobs, S.run_postmarket) == ["16:45"]
    assert _times(jobs, S.run_postmarket_if_missing) == [S.POSTMARKET_CATCHUP_TIME]
    assert S.POSTMARKET_CATCHUP_TIME > S.POSTMARKET_RUN_TIME


def test_overnight_and_weekly_runs(registered):
    S, jobs = registered
    assert _times(jobs, S.run_overnight) == ["23:00"]
    assert _times(jobs, S.run_weekly, unit="saturday") == ["08:00"]


def test_the_mac_launchagent_runs_the_scheduler_on_ist():
    plist = (ROOT / "deploy" / "launchd" / "com.atip.platform.plist").read_text(encoding="utf-8")
    assert "<key>TZ</key>" in plist
    after = plist.split("<key>TZ</key>", 1)[1]
    assert after.lstrip().startswith("<string>Asia/Kolkata</string>")
