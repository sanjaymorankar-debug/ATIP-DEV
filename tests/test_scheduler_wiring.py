"""
start_scheduler() registers the aggressive exit manager (PR #17, merged 2026-10-26).

strategy/live.py's schedule_jobs is tested on its own; this checks the wiring in pipeline/scheduler.py
that calls it, by running start_scheduler up to its leader-lease loop with the start-up catch-ups stubbed.
"""

import pytest


class _Stop(Exception):
    pass


def test_start_scheduler_registers_the_aggressive_exit_manager(temp_db, monkeypatch):
    import schedule
    import ops.jobs as J
    from pipeline import scheduler as S
    from strategy import aggressive as A
    from strategy import live as L
    for name in ("run_startup_market_catchup", "run_postmarket_if_missing", "run_fii_dii_watch",
                 "run_morning_catchup", "run_health_check"):
        monkeypatch.setattr(S, name, lambda *a, **k: None)
    monkeypatch.setattr(A, "config", lambda refresh=False: A.validate_config(dict(A.DEFAULTS)))

    def stop(*a, **k):
        raise _Stop
    monkeypatch.setattr(J, "leader_lease", stop)
    schedule.clear()
    try:
        with pytest.raises(_Stop):
            S.start_scheduler()
        jobs = [j for j in schedule.get_jobs() if getattr(j.job_func, "func", None) is L.scheduled_pass]
        assert len(jobs) == 1
        assert jobs[0].interval == A.DEFAULTS["manage_interval_minutes"] and jobs[0].unit == "minutes"
        assert jobs[0].job_func.args == (S.run_job,)
    finally:
        schedule.clear()
