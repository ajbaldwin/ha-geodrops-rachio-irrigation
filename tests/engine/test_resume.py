"""Resuming an interrupted night from its persisted progress (crash or graceful
restart), asserted directly on the engine's effects."""
import asyncio
import collections

import pytest

from custom_components.geodrops_rachio.engine.store import (
    PENDING_OBS, RUN_ACTIVE, RUN_PROGRESS,
)
from tests.engine.scenario import entry_data, native_scheduler, populate
from tests.engine.world import FakeWorld

START = ("rachio", "start_multiple_zone_schedule")
WINDOW_END = "2026-07-02T04:15:00+00:00"


def _world(freezer, when, data):
    freezer.move_to(when)
    w = FakeWorld(freezer)
    populate(w, data)
    return w


def _sent_minutes(world) -> dict:
    """Minutes per zone handed to Rachio across every schedule start."""
    by_zone = collections.Counter()
    switch_to_zone = {"switch.front_zone": "front", "switch.back_zone": "back"}
    for dom, svc, data in world.calls:
        if (dom, svc) == START:
            for ent, mins in zip(data["entity_id"], str(data["duration"]).split(",")):
                by_zone[switch_to_zone[ent]] += int(mins)
    return dict(by_zone)


def _progress_writes(eng) -> list:
    """Every write of the progress doc, in order (a copy of each)."""
    log = []
    real = eng.store.write

    async def write(key, value):
        if key == RUN_PROGRESS:
            log.append(None if value is None else dict(value))
        await real(key, value)
    eng.store.write = write
    return log


async def _park_in_first_pause(eng, world):
    """A run_now parked inside its first device pause (valves 'watering')."""
    paused = asyncio.Event()
    real_sleep = eng.port.sleep

    async def sleep_until_paused(seconds):
        if world.rachio.paused_until is not None:
            paused.set()
            await asyncio.Event().wait()
        await real_sleep(seconds)
    eng.port.sleep = sleep_until_paused
    await eng.async_run_now()
    waiter = asyncio.ensure_future(paused.wait())
    await asyncio.wait({waiter, eng.run_task}, return_when=asyncio.FIRST_COMPLETED)
    assert paused.is_set(), "the run never reached a device pause"


async def test_progress_credits_each_step_as_it_starts_and_clears_at_the_end(freezer):
    data = entry_data()
    world = _world(freezer, "2026-07-02 02:00:00", data)
    eng = native_scheduler(world, data)
    writes = _progress_writes(eng)
    await eng.async_run_now()
    await eng.run_task

    opened, begun, last = writes[0], writes[1], writes[-1]
    # The orchestrator opens the doc; the runner then records the plan.
    assert opened["trigger"] == "run_now" and opened["window_end"]
    assert "planned" not in opened
    planned = begun["planned"]
    assert planned == _sent_minutes(world)           # the whole night's plan
    assert begun["delivered"] == {}
    started = [w for w in writes[2:-1] if w and w.get("delivered")]
    # The first water step is credited in full the moment it starts (errs dry).
    first_call = next(d for dom, svc, d in world.calls if (dom, svc) == START)
    zone = first_call["entity_id"][0].removeprefix("switch.").removesuffix("_zone")
    first_minutes = int(str(first_call["duration"]).split(",")[0])
    assert started[0]["delivered"] == {zone: first_minutes}
    assert started[-1]["delivered"] == planned       # by the last step, all of it
    assert last is None                              # cleared once the run ended
    assert eng.store.read(RUN_PROGRESS) is None


async def test_graceful_restart_keeps_progress_and_resumes_only_whats_owed(freezer):
    data = entry_data()
    world = _world(freezer, "2026-07-02 02:00:00", data)
    eng = native_scheduler(world, data)
    await _park_in_first_pause(eng, world)
    await eng.async_shutdown()
    left = eng.store.read(RUN_PROGRESS)
    assert left is not None, "a graceful stop must not clear the night's progress"
    owed = {z: left["planned"][z] - left["delivered"].get(z, 0) for z in left["planned"]}
    owed = {z: m for z, m in owed.items() if m > 0}
    assert owed, "the scenario must leave water owed"

    world2 = _world(freezer, "2026-07-02 02:40:00", data)
    eng2 = native_scheduler(world2, data, docs={RUN_PROGRESS: left})
    await eng2._on_startup()
    await eng2.run_task
    assert _sent_minutes(world2) == owed             # never re-planned from moisture
    assert eng2.records["last_run"]["attributes"]["trigger"] == "startup-resume"
    assert eng2.store.read(RUN_PROGRESS) is None


async def test_crash_mid_step_counts_that_step_as_delivered(freezer):
    data = entry_data(self_cal=True)
    world = _world(freezer, "2026-07-02 02:20:00", data)
    world.rachio.start_direct([("switch.back_zone", 12)])   # still watering
    crashed = {"stamp": "2026-07-01T23:00:00", "trigger": "nightly",
               "window_end": WINDOW_END,
               "planned": {"front": 24, "back": 24},
               "delivered": {"front": 12, "back": 12}}   # back's step was in flight
    # RUN_ACTIVE still set: the run never cleared it.
    eng = native_scheduler(world, data, docs={RUN_PROGRESS: crashed, RUN_ACTIVE: True})
    await eng._on_startup()
    await eng.run_task
    assert ("rachio", "stop_watering", {"devices": "Main House"}) in world.calls
    assert _sent_minutes(world) == {"front": 12, "back": 12}
    assert eng.records["last_run"]["attributes"]["trigger"] == "startup-resume"
    # An interrupted night is a confounded calibration observation: none recorded.
    assert not eng.store.read(PENDING_OBS)


async def test_window_closed_records_the_night_as_interrupted(freezer):
    data = entry_data()
    world = _world(freezer, "2026-07-02 05:00:00", data)
    left = {"stamp": "2026-07-01T23:00:00", "trigger": "nightly",
            "window_end": WINDOW_END, "planned": {"front": 24, "back": 24},
            "delivered": {"front": 12}}
    eng = native_scheduler(world, data, docs={RUN_PROGRESS: left})
    await eng._on_startup()
    assert eng.run_task is None
    assert [c for c in world.calls if c[:2] == START] == []
    last_run = eng.records["last_run"]["attributes"]
    assert last_run["skipped"] == "interrupted-restart"
    assert last_run["trigger"] == "nightly"
    assert eng.store.read(RUN_PROGRESS) is None


async def test_a_previous_nights_leftover_progress_is_discarded(freezer):
    data = entry_data()
    world = _world(freezer, "2026-07-02 23:30:00", data)
    stale = {"stamp": "2026-07-01T23:00:00", "trigger": "nightly",
             "window_end": WINDOW_END, "planned": {"front": 24}, "delivered": {}}
    eng = native_scheduler(world, data, docs={RUN_PROGRESS: stale})
    await eng._on_startup()
    assert eng.run_task is None and world.calls == []
    assert "last_run" not in eng.records
    assert eng.store.read(RUN_PROGRESS) is None


@pytest.mark.parametrize("trigger", ["nightly"])
async def test_nightly_record_counts_a_resumed_night(freezer, trigger):
    data = entry_data()
    world = _world(freezer, "2026-07-02 02:40:00", data)
    left = {"stamp": "2026-07-01T23:00:00", "trigger": trigger,
            "window_end": WINDOW_END, "planned": {"front": 12}, "delivered": {}}
    eng = native_scheduler(world, data, docs={RUN_PROGRESS: left})
    await eng._on_startup()
    await eng.run_task
    assert eng.records["last_nightly"]["attributes"]["trigger"] == "startup-resume"


async def test_reset_discards_the_nights_progress(freezer):
    # Reset cancels the run (and a cancelled run keeps its progress, for a
    # restart to resume) -- but a reset night is over; nothing must resume it.
    data = entry_data()
    world = _world(freezer, "2026-07-02 02:00:00", data)
    eng = native_scheduler(world, data)
    await _park_in_first_pause(eng, world)
    assert eng.store.read(RUN_PROGRESS) is not None
    await eng.async_reset()
    assert eng.store.read(RUN_PROGRESS) is None


async def test_crash_during_a_pause_stops_the_schedule_then_resumes(freezer):
    # Marker still on, but a pause reads all valves off: stop the device (it would
    # auto-resume unattended) without any per-zone stop, then resume what's owed.
    data = entry_data()
    world = _world(freezer, "2026-07-02 02:20:00", data)
    left = {"stamp": "2026-07-01T23:00:00", "trigger": "nightly",
            "window_end": WINDOW_END, "planned": {"front": 24, "back": 24},
            "delivered": {"front": 12, "back": 12}}
    eng = native_scheduler(world, data, docs={RUN_PROGRESS: left, RUN_ACTIVE: True})
    await eng._on_startup()
    await eng.run_task
    stop_at = world.calls.index(("rachio", "stop_watering", {"devices": "Main House"}))
    # No per-zone stop between the device stop and the resume's first schedule.
    after = world.calls[stop_at + 1:]
    first_start = next(i for i, c in enumerate(after) if c[:2] == START)
    assert ("switch", "turn_off") not in [c[:2] for c in after[:first_start]]
    assert eng.store.read(RUN_ACTIVE) is None
    assert _sent_minutes(world) == {"front": 12, "back": 12}


async def test_a_zone_excluded_since_the_restart_is_not_resumed(freezer):
    data = entry_data()
    world = _world(freezer, "2026-07-02 02:40:00", data)
    world.set("switch.geodrops_rachio_back_exclude", "on")
    left = {"stamp": "2026-07-01T23:00:00", "trigger": "nightly",
            "window_end": WINDOW_END, "planned": {"front": 24, "back": 24},
            "delivered": {"front": 12}}
    eng = native_scheduler(world, data, docs={RUN_PROGRESS: left})
    await eng._on_startup()
    await eng.run_task
    assert _sent_minutes(world) == {"front": 12}
    assert eng.records["last_run"]["attributes"]["uncompleted"].get("back") == "excluded"


async def test_owed_water_that_no_longer_fits_the_window_is_dropped(freezer):
    data = entry_data()
    world = _world(freezer, "2026-07-02 04:05:00", data)   # little window left
    left = {"stamp": "2026-07-01T23:00:00", "trigger": "nightly",
            "window_end": WINDOW_END, "planned": {"front": 60, "back": 60},
            "delivered": {}}
    eng = native_scheduler(world, data, docs={RUN_PROGRESS: left})
    await eng._on_startup()
    await eng.run_task
    uncompleted = eng.records["last_run"]["attributes"]["uncompleted"]
    assert "insufficient window" in uncompleted.values()
    sent = _sent_minutes(world)
    assert sum(sent.values()) < 120                  # not everything owed was sent


async def test_a_failed_progress_clear_is_logged_not_fatal(freezer, caplog):
    data = entry_data()
    world = _world(freezer, "2026-07-02 02:00:00", data)
    eng = native_scheduler(world, data)
    real = eng.store.write

    async def write(key, value):
        if key == RUN_PROGRESS and value is None:
            raise OSError("disk full")
        await real(key, value)
    eng.store.write = write
    await eng.async_run_now()
    await eng.run_task
    assert eng.records["status"]["value"] == "idle"
    assert any("could not clear run progress (disk full)" in r.getMessage()
               for r in caplog.records)
