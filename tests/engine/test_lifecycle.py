"""Scheduler lifecycle: background jobs and teardown."""
import asyncio
import logging

import pytest

from tests.engine.scenario import entry_data, native_scheduler, populate
from tests.engine.world import FakeWorld

ENGINE = "custom_components.geodrops_rachio.engine"


def _scheduler(freezer):
    data = entry_data()
    freezer.move_to("2026-07-02 12:00:00")
    w = FakeWorld(freezer)
    populate(w, data)
    return native_scheduler(w, data)


@pytest.mark.parametrize("trigger, job", [
    ("_on_calibrate_time", "irrigation_calibrate"),
    ("_on_settle_time", "_settle_and_learn"),
])
async def test_shutdown_cancels_an_in_flight_timed_job(freezer, trigger, job):
    eng = _scheduler(freezer)
    events = []

    async def blocked():
        events.append("started")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("cancelled")
            raise
    setattr(eng, job, blocked)
    fired = asyncio.get_running_loop().create_task(getattr(eng, trigger)(None))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert events == ["started"]
    await asyncio.wait_for(eng.async_shutdown(), 1)
    assert events == ["started", "cancelled"]
    await asyncio.wait_for(fired, 1)               # the trigger itself returned
    assert eng._jobs == set()


async def test_finished_timed_job_is_forgotten(freezer):
    eng = _scheduler(freezer)

    async def quick():
        return None
    eng.irrigation_calibrate = quick
    await eng._on_calibrate_time(None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert eng._jobs == set()


async def test_task_failing_while_cancelled_is_logged_once(freezer, caplog):
    eng = _scheduler(freezer)
    caplog.set_level(logging.DEBUG, logger=ENGINE)

    async def bad_teardown(*_a):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("teardown boom") from None
    eng._plan_and_run = bad_teardown
    await eng.async_run_now()
    await asyncio.sleep(0)
    await eng._cancel_run()
    await asyncio.sleep(0)                         # done-callbacks run on the next tick
    hits = [r for r in caplog.records
            if r.name.startswith(ENGINE) and "teardown boom" in r.getMessage()
            and r.levelno >= logging.WARNING]
    assert [r.levelname for r in hits] == ["ERROR"]
