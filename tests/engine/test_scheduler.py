import asyncio
import contextlib
import gc
import logging
import random

import pytest

from custom_components.geodrops_rachio.engine.store import (RUN_ACTIVE, RUN_PROGRESS,
                                                             WAITING_MARKER)
from tests.engine import golden
from tests.engine.helpers import ENGINE_LOGGER_PREFIX, log_trail_native
from tests.engine.scenario import entry_data, native_scheduler, populate
from tests.engine.world import FakeWorld

_Random = random.Random


@pytest.fixture(autouse=True)
def seeded_rng(monkeypatch):
    monkeypatch.setattr(random, "Random", lambda *a: _Random(1234))


WAITING = {WAITING_MARKER: {
    "window_end": "2026-07-02T04:59:00+00:00", "stamp": "2026-07-01T23:00:00",
    "trigger": "nightly"}}
MISSED = {WAITING_MARKER: {
    "window_end": "2026-07-02T01:00:00+00:00", "stamp": "2026-07-01T23:00:00",
    "trigger": "nightly"}}
# Real markers are AWARE (window_end comes from the sun sensor, +00:00). Through
# v0.9.15, _on_startup compared them with a NAIVE now() and recovery.startup_action
# raised TypeError, losing the night. Fixed in v1.0.0 inside brain.recovery, so
# aware and naive markers both reach RE_ARM / MISSED.
WAITING_NAIVE = {WAITING_MARKER: {
    "window_end": "2026-07-02T04:59:00", "stamp": "2026-07-01T23:00:00",
    "trigger": "nightly"}}
MISSED_NAIVE = {WAITING_MARKER: {
    "window_end": "2026-07-02T01:00:00", "stamp": "2026-07-01T23:00:00",
    "trigger": "nightly"}}

# name -> (start time, seed docs, world tweak, exception _on_startup raises)
STARTUP = {
    "daytime_noop": ("2026-07-02 14:00:00", {}, lambda w: None, None),
    "collapsed_marker": ("2026-07-02 02:00:00", {RUN_ACTIVE: True},
                         lambda w: None, None),
    "orphan_valve": ("2026-07-02 02:00:00", {},
                     lambda w: w.rachio.start_direct([("switch.front_zone", 30)]), None),
    "waiting_rearm": ("2026-07-02 00:30:00", WAITING_NAIVE, lambda w: None, None),
    "waiting_missed": ("2026-07-02 02:00:00", MISSED_NAIVE, lambda w: None, None),
    "waiting_rearm_aware": ("2026-07-02 00:30:00", WAITING, lambda w: None, None),
    "waiting_missed_aware": ("2026-07-02 02:00:00", MISSED, lambda w: None, None),
}

STOP = ("rachio", "stop_watering", {"devices": "Main House"})


def _logbook(world) -> list[str]:
    return [d["message"] for dom, svc, d in world.calls if (dom, svc) == ("logbook", "log")]


def _status_trail(eng) -> list:
    return [(v, a.get("detail")) for n, v, a in eng.record_history if n == "status"]


def _assert_startup_branch(name, world, eng):
    """Confirm the run reached the branch the scenario is named for, so no
    scenario can silently collapse into the daytime no-op and still match (or
    regenerate) its fixture."""
    statuses = [v for v, _d in _status_trail(eng)]
    book = _logbook(world)
    rec = eng.records.get("last_run")
    last_run = None if rec is None else (rec["value"], rec["attributes"])
    if name == "daytime_noop":
        # Only the startup "idle" publish; the time gate returned before any check.
        assert _status_trail(eng) == [("idle", None)]
        assert [c for c in world.calls if c[0] != "logbook"] == []
        assert book == []
        assert last_run is None
    elif name == "collapsed_marker":
        assert world.calls[0] == STOP
        assert any("interrupted collapsed run was detected" in m for m in book)
        assert "planning" in statuses and "watering" in statuses
        assert last_run[1]["trigger"] == "startup-heal"
    elif name == "orphan_valve":
        assert world.calls[0] == ("switch", "turn_off", {"entity_id": "switch.front_zone"})
        assert any("closed 1 zone(s) left open" in m and "switch.front_zone" in m
                   for m in book)
        assert any("re-planning the interrupted run" in m for m in book)
        assert "planning" in statuses and "watering" in statuses
        assert last_run[1]["trigger"] == "startup-heal"
    elif name in ("waiting_rearm", "waiting_rearm_aware"):
        assert any("was waiting for its pre-dawn window when HA restarted" in m
                   for m in book)
        assert "planning" in statuses and "watering" in statuses
        assert last_run[1]["trigger"] == "startup-heal"
        assert last_run[1]["aborted_reason"] is None
        assert eng.store.read(WAITING_MARKER) is None       # consumed
    elif name in ("waiting_missed", "waiting_missed_aware"):
        assert _status_trail(eng) == [
            ("idle", None), ("skipped", "missed — restart after window closed")]
        assert any("recorded as missed" in m for m in book)
        assert last_run[1]["skipped"] == "missed-restart"
        assert last_run[1]["updated"] == "2026-07-01T23:00:00"
        assert last_run[1]["trigger"] == "nightly"
        assert eng.store.read(WAITING_MARKER) is None       # consumed
    else:  # pragma: no cover - every scenario must be pinned
        raise AssertionError(f"no branch evidence for {name}")


def _maybe_raises(exc):
    return pytest.raises(exc) if exc is not None else contextlib.nullcontext()


@pytest.mark.parametrize("name", list(STARTUP))
async def test_startup_scenario(freezer, name, caplog):
    caplog.set_level(logging.INFO, logger=ENGINE_LOGGER_PREFIX)
    start, docs, tweak, raises = STARTUP[name]
    data = entry_data()

    freezer.move_to(start)
    world = FakeWorld(freezer)
    populate(world, data)
    tweak(world)
    eng = native_scheduler(world, data, docs=docs)
    with _maybe_raises(raises):
        await eng._on_startup()
    if eng.run_task is not None:
        await eng.run_task

    golden.check(f"startup/{name}",
                 golden.engine_effects(world, eng, log_trail_native(caplog)))
    _assert_startup_branch(name, world, eng)


async def test_manual_stop_mid_run(freezer, caplog):
    caplog.set_level(logging.INFO, logger=ENGINE_LOGGER_PREFIX)
    data = entry_data()
    freezer.move_to("2026-07-01 23:00:00")
    world = FakeWorld(freezer)
    populate(world, data)
    eng = native_scheduler(world, data)
    world.at("2026-07-02 04:20:00", eng.request_stop)
    await eng.irrigation_nightly()
    await eng.run_task

    golden.check("scheduler/manual_stop_mid_run",
                 golden.engine_effects(world, eng, log_trail_native(caplog)))
    assert eng.records["last_run"]["attributes"]["aborted_reason"] == "manual-abort"
    assert "Stop button pressed" in _logbook(world)


async def test_reset_cancels_waiting_run(freezer):
    data = entry_data()
    freezer.move_to("2026-07-01 23:00:00")
    w = FakeWorld(freezer)
    populate(w, data)
    eng = native_scheduler(w, data)
    # Park the run in its pre-dawn wait: make sleep block until cancelled.
    gate = asyncio.Event()

    async def blocking_sleep(_s):
        await gate.wait()
    eng.port.sleep = blocking_sleep
    await eng.irrigation_nightly()
    for _ in range(5):                 # let the run task reach its wait
        await asyncio.sleep(0)
    assert eng.records["status"]["value"] == "waiting"
    await eng.async_reset()
    assert eng.run_task is None
    assert eng.records["status"]["value"] == "idle"
    assert ("rachio", "stop_watering", {"devices": "Main House"}) in w.calls
    assert eng.store.read("waiting_marker") is None


def _record_blocking(eng):
    """Wrap port.call to record each call's `blocking` flag (FakePort drops it)."""
    log = []
    real_call = eng.port.call

    async def call(domain, service, data, *, blocking=False):
        log.append((domain, service, dict(data), blocking))
        await real_call(domain, service, data, blocking=blocking)
    eng.port.call = call
    return log


async def _run_to_pause(freezer):
    """A run_now parked inside its first device pause (valves 'watering')."""
    data = entry_data()
    freezer.move_to("2026-07-02 02:00:00")
    w = FakeWorld(freezer)
    populate(w, data)
    eng = native_scheduler(w, data)
    paused = asyncio.Event()
    real_sleep = eng.port.sleep

    async def sleep_until_paused(seconds):
        if w.rachio.paused_until is not None:
            paused.set()
            await asyncio.Event().wait()       # park here until cancelled
        await real_sleep(seconds)
    eng.port.sleep = sleep_until_paused
    await eng.async_run_now()
    # Not asyncio.wait_for(..., 5): freezegun also freezes loop.time(), and every
    # FakePort.sleep moves that clock (24 simulated minutes pass before the first
    # pause), so a 5 s deadline expires on simulated time. Instead fail if the run
    # finishes without ever pausing.
    waiter = asyncio.ensure_future(paused.wait())
    await asyncio.wait({waiter, eng.run_task}, return_when=asyncio.FIRST_COMPLETED)
    assert paused.is_set(), "the run never reached a device pause"
    assert eng._watering_active
    return w, eng


async def test_unload_mid_pause_stops_the_device(freezer):
    w, eng = await _run_to_pause(freezer)
    calls = _record_blocking(eng)
    await eng.async_shutdown()
    assert eng.run_task is None
    tail = w.calls[-4:]
    assert ("rachio", "stop_watering", {"devices": "Main House"}) in tail
    assert ("switch", "turn_off", {"entity_id": "switch.front_zone"}) in tail
    assert eng.store.read(RUN_ACTIVE) is None
    # The run's teardown clears its marker in the store, not via a service
    # call, so the only calls are the safety stop's, all blocking.
    assert calls == [
        ("rachio", "stop_watering", {"devices": "Main House"}, True),
        ("switch", "turn_off", {"entity_id": "switch.front_zone"}, True),
        ("switch", "turn_off", {"entity_id": "switch.back_zone"}, True),
    ]


async def test_nightly_superseding_a_watering_run_stops_rachio(freezer):
    """A Run Now still watering at 23:00 is replaced by the nightly. Cancelling
    it skips the runner's teardown, so without a stop Rachio auto-resumes the
    paused schedule with nobody watching — and its leftover progress would make
    a restart during the nightly's wait water those minutes again."""
    w, eng = await _run_to_pause(freezer)
    assert eng.store.read(RUN_PROGRESS) is not None
    n = len(w.calls)
    await eng.irrigation_nightly()
    after = w.calls[n:]
    assert STOP in after
    assert ("switch", "turn_off", {"entity_id": "switch.front_zone"}) in after
    assert eng.store.read(RUN_PROGRESS) is None
    await eng._cancel_run()


async def test_superseding_still_starts_the_new_run_if_progress_cannot_be_cleared(
        freezer, caplog):
    """Dropping the replaced run's progress is bookkeeping; a store failure
    there must not cost the night its nightly run."""
    w, eng = await _run_to_pause(freezer)
    real_write = eng.store.write

    async def write(key, value):
        if key == RUN_PROGRESS and value is None:
            raise OSError("disk full")
        await real_write(key, value)
    eng.store.write = write
    await eng.irrigation_nightly()
    assert eng.run_task is not None and not eng.run_task.done()
    assert STOP in w.calls
    assert any("could not clear the replaced run's progress (disk full)"
               in r.getMessage() for r in caplog.records)
    await eng._cancel_run()


async def test_superseding_a_waiting_run_issues_no_stop(freezer):
    """Replacing a run that has not started watering must not touch Rachio: a
    stop there could cut off someone's manual run from the app."""
    data = entry_data()
    freezer.move_to("2026-07-01 23:00:00")
    w = FakeWorld(freezer)
    populate(w, data)
    eng = native_scheduler(w, data)
    gate = asyncio.Event()

    async def blocking_sleep(_s):
        await gate.wait()
    eng.port.sleep = blocking_sleep
    await eng.irrigation_nightly()
    for _ in range(5):
        await asyncio.sleep(0)
    assert eng.records["status"]["value"] == "waiting"
    n = len(w.calls)
    await eng.async_run_now()
    assert STOP not in w.calls[n:]
    await eng._cancel_run()


async def test_unload_safety_stop_is_bounded(freezer, caplog):
    w, eng = await _run_to_pause(freezer)
    calls = _record_blocking(eng)
    inner = eng.port.call

    async def hanging_when_blocking(domain, service, data, *, blocking=False):
        await inner(domain, service, data, blocking=blocking)
        if blocking:
            # asyncio.timeout runs on loop.time(), which freezegun freezes: step
            # the frozen clock past the 10 s bound, then hang forever.
            freezer.tick(11)
            await asyncio.Event().wait()
    eng.port.call = hanging_when_blocking
    await eng.async_shutdown()
    assert eng.run_task is None
    assert [c[:2] for c in calls if c[3]] == [("rachio", "stop_watering")]  # cut off
    assert ("warning", "irrigation: unload safety stop failed ()") in log_trail_native(caplog)


async def test_unload_during_predawn_wait_leaves_valves_and_marker(freezer):
    data = entry_data()
    freezer.move_to("2026-07-01 23:00:00")
    w = FakeWorld(freezer)
    populate(w, data)
    eng = native_scheduler(w, data)
    gate = asyncio.Event()

    async def blocking_sleep(_s):
        await gate.wait()
    eng.port.sleep = blocking_sleep
    await eng.irrigation_nightly()
    for _ in range(5):
        await asyncio.sleep(0)
    assert eng.records["status"]["value"] == "waiting"
    assert eng.run_task is not None and not eng._watering_active
    before = list(w.calls)
    await eng.async_shutdown()
    assert eng.run_task is None
    assert w.calls == before                       # no safety stop, nothing else
    assert eng.store.read("waiting_marker") is not None   # startup can re-arm


async def test_overlapping_starts_leave_exactly_one_run_last_caller_wins(freezer):
    data = entry_data()
    freezer.move_to("2026-07-02 12:00:00")
    w = FakeWorld(freezer)
    populate(w, data)
    eng = native_scheduler(w, data)
    events = []

    async def fake_plan_and_run(wait, trigger):
        events.append(("start", trigger))
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)             # a teardown that yields
            events.append(("unwound", trigger))
    eng._plan_and_run = fake_plan_and_run

    def live():
        return [t for t in asyncio.all_tasks()
                if t.get_name() == "geodrops_rachio_run" and not t.done()]

    await eng._start_run(True, "old")
    await asyncio.sleep(0)
    await asyncio.gather(eng._start_run(False, "A"), eng._start_run(False, "B"))
    for _ in range(3):
        await asyncio.sleep(0)
    assert live() == [eng.run_task]
    assert events == [("start", "old"), ("unwound", "old"), ("start", "B")]

    # A cancel racing a start supersedes it: nothing survives.
    await asyncio.gather(eng._start_run(False, "C"), eng._cancel_run())
    for _ in range(3):
        await asyncio.sleep(0)
    assert live() == [] and eng.run_task is None
    assert events[-1] == ("unwound", "B")


async def test_crashed_run_and_startup_are_logged_at_once(freezer, caplog):
    data = entry_data()
    freezer.move_to("2026-07-02 12:00:00")
    w = FakeWorld(freezer)
    populate(w, data)
    eng = native_scheduler(w, data)

    async def boom(*_a):
        raise RuntimeError("boom")
    eng._plan_and_run = boom
    eng._on_startup = boom
    await eng.async_run_now()
    await eng._on_ha_started(None)
    tasks = [eng.run_task, eng.startup_task]
    await asyncio.wait(tasks)
    await asyncio.sleep(0)                     # done-callbacks run on the next tick
    errors = [r for r in caplog.records
              if r.name.startswith(ENGINE_LOGGER_PREFIX) and r.levelname == "ERROR"]
    assert [r.getMessage() for r in errors] == [
        "irrigation: geodrops_rachio_run failed (RuntimeError('boom'))",
        "irrigation: geodrops_rachio_startup failed (RuntimeError('boom'))",
    ]
    assert all(r.exc_info and r.exc_info[1].args == ("boom",) for r in errors)
    # Retrieved by the callback: no "never retrieved" when the tasks are collected.
    eng.run_task = eng.startup_task = None
    del tasks
    gc.collect()
    await asyncio.sleep(0)
    assert "never retrieved" not in caplog.text


async def test_shutdown_is_idempotent_and_skips_stop_when_idle(freezer):
    data = entry_data()
    freezer.move_to("2026-07-02 12:00:00")
    w = FakeWorld(freezer)
    populate(w, data)
    eng = native_scheduler(w, data)
    await eng.async_shutdown()
    await eng.async_shutdown()
    assert eng.run_task is None and eng.startup_task is None
    assert w.calls == []               # nothing was watering: no safety stop


async def test_cancel_run_does_not_swallow_the_callers_cancellation(freezer):
    data = entry_data()
    freezer.move_to("2026-07-02 12:00:00")
    w = FakeWorld(freezer)
    populate(w, data)
    eng = native_scheduler(w, data)
    release = asyncio.Event()

    async def stubborn():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()       # slow teardown
            raise
    eng.run_task = asyncio.get_running_loop().create_task(stubborn())
    await asyncio.sleep(0)
    canceller = asyncio.get_running_loop().create_task(eng._cancel_run())
    await asyncio.sleep(0)
    assert eng.run_task is None
    canceller.cancel()                 # the caller itself is cancelled mid-wait
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await canceller


async def test_refresh_runtimes_publishes_record(freezer):
    data = entry_data()
    freezer.move_to("2026-07-02 12:00:00")
    w = FakeWorld(freezer)
    populate(w, data)
    w.rachio_api = ({"id-front": 33.0}, {"id-front": 8.0}, {})
    eng = native_scheduler(w, data)
    await eng.async_refresh_runtimes()
    rec = eng.records["runtimes"]
    assert rec["value"] == 1 and rec["attributes"]["refill_depths_mm"] == {"id-front": 8.0}


async def test_safety_stop_fires_while_startup_task_still_running(freezer):
    """HA cancels background tasks (the run included) around shutdown. With the
    startup task still live, `async_shutdown` yields while cancelling it, the
    cancelled run unwinds and its `finally` clears `_watering_active` — the
    safety stop must still fire because the flag is read before any await."""
    w, eng = await _run_to_pause(freezer)

    async def slow_startup():
        try:
            await asyncio.Event().wait()           # the real one sleeps 30 s
        finally:
            await asyncio.sleep(0)                 # a teardown that yields
    eng.startup_task = asyncio.get_running_loop().create_task(slow_startup())
    await asyncio.sleep(0)
    calls = _record_blocking(eng)
    eng.run_task.cancel()                          # what HA does to background tasks
    await eng.async_shutdown()
    assert eng.run_task is None and eng.startup_task is None
    assert not eng._watering_active
    assert [c for c in calls if c[3]] == [
        ("rachio", "stop_watering", {"devices": "Main House"}, True),
        ("switch", "turn_off", {"entity_id": "switch.front_zone"}, True),
        ("switch", "turn_off", {"entity_id": "switch.back_zone"}, True),
    ]


def _night_scheduler(freezer, when="2026-07-02 02:00:00"):
    data = entry_data()
    freezer.move_to(when)
    w = FakeWorld(freezer)
    populate(w, data)
    return w, native_scheduler(w, data)


async def test_startup_survives_a_config_that_will_not_load(freezer, caplog):
    """A restart with broken overrides must still publish a status and must not
    crash the startup task (nothing else would report it)."""
    w, eng = _night_scheduler(freezer)

    def broken():
        raise ValueError("bad overrides")
    eng._load_raw_config = broken
    await eng._on_startup()
    assert eng.records["status"]["value"] == "idle" and eng.run_task is None
    msgs = [r.getMessage() for r in caplog.records]
    assert any("startup target-floor publish skipped (bad overrides)" in m for m in msgs)
    assert any("startup safety check skipped; config load failed" in m for m in msgs)


async def test_startup_closes_an_open_valve_when_another_zone_is_unreadable(freezer):
    """One renamed/missing zone switch must not stop the orphan-valve check for
    the others."""
    w, eng = _night_scheduler(freezer)
    w.rachio.start_direct([("switch.back_zone", 30)])
    w.remove("switch.front_zone")
    started = []

    async def fake_start(wait, trigger, resume=None):
        started.append(trigger)
    eng._start_run = fake_start
    await eng._on_startup()
    assert ("switch", "turn_off", {"entity_id": "switch.back_zone"}) in w.calls
    assert started == ["startup-heal"]


async def test_reset_before_any_config_is_loaded_still_goes_idle(freezer, caplog):
    """Reset pressed in the 30 s before startup has loaded a config."""
    w, eng = _night_scheduler(freezer)
    assert eng._current_cfg is None
    await eng.async_reset()
    assert eng.records["status"] == {"value": "idle", "attributes": {
        "friendly_name": "Irrigation Status", "updated": "2026-07-02T02:00:00",
        "detail": "reset"}}
    assert any("stop_device (rachio.stop_watering) failed" in r.getMessage()
               for r in caplog.records)


async def test_reset_marker_cleanup_failure_is_logged_not_raised(freezer, caplog):
    w, eng = _night_scheduler(freezer)
    eng._current_cfg = eng._load_cfg()
    eng._current_bindings = eng._current_cfg.bindings

    async def failing_write(_key, _value):
        raise OSError("disk full")
    eng.store.write = failing_write
    await eng.async_reset()
    assert STOP in w.calls
    assert eng.records["status"]["value"] == "idle"
    assert any("reset — marker cleanup skipped: disk full" in r.getMessage()
               for r in caplog.records)


def _to_translation_keys(world, data):
    """Rewrite the GeoDrops states the way ha-geodrops-hacs PR #12 reports
    them: translation keys instead of UI labels."""
    for z in data["zones"]:
        world.set(z["state_sensor"], world.get(z["state_sensor"]).lower()
                  .replace("+", "_plus"))
        for q in z["quality_sensors"]:
            world.set(q, world.get(q).lower())


@pytest.mark.parametrize("keys", [False, True], ids=["labels", "translation_keys"])
async def test_a_night_waters_the_same_with_either_geodrops_state_format(
        freezer, keys):
    data = entry_data()
    freezer.move_to("2026-07-01 23:00:00")
    w = FakeWorld(freezer)
    populate(w, data)
    if keys:
        _to_translation_keys(w, data)
    eng = native_scheduler(w, data)
    await eng.irrigation_nightly()
    await eng.run_task
    a = eng.records["last_run"]["attributes"]
    assert sorted(a["watered"]) == ["back", "front"]
    assert a["delivered_minutes"] == {"front": 36, "back": 36}


async def test_each_zone_records_its_own_last_valve_close(freezer):
    """Two zones alternate 12-min cycles (…back 04:31-04:43, front 04:43-04:55).
    Per-zone Last watered is when THAT zone's valve last closed, not when the
    whole run ended."""
    from custom_components.geodrops_rachio.engine.store import ZONE_WATERED
    data = entry_data()
    freezer.move_to("2026-07-01 23:00:00")
    w = FakeWorld(freezer)
    populate(w, data)
    eng = native_scheduler(w, data)
    await eng.irrigation_nightly()
    await eng.run_task
    zw = eng.store.read(ZONE_WATERED)
    assert zw["back"]["end_iso"] == "2026-07-02T04:43:00+00:00"
    assert zw["front"]["end_iso"] == "2026-07-02T04:55:00+00:00"
    a = eng.records["last_run"]["attributes"]
    assert a["end_iso"] == "2026-07-02T04:55:00+00:00"       # the run's own end
    assert a["zone_end_iso"] == {"back": "2026-07-02T04:43:00+00:00",
                                 "front": "2026-07-02T04:55:00+00:00"}
