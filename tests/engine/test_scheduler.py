import asyncio
import contextlib
import logging
import random

import pytest

from custom_components.geodrops_rachio.config_writer import build_config
from custom_components.geodrops_rachio.engine.store import LEGACY_FILE_KEYS
from tests.engine.diff import (
    ENGINE_LOGGER_PREFIX, assert_same_effects, log_trail_legacy, log_trail_native,
    status_trail_legacy,
)
from tests.engine.legacy_harness import LegacyFiles, load_legacy
from tests.engine.scenario import entry_data, native_scheduler, populate
from tests.engine.world import FakeWorld

_Random = random.Random


@pytest.fixture(autouse=True)
def seeded_rng(monkeypatch):
    monkeypatch.setattr(random, "Random", lambda *a: _Random(1234))


WAITING = {"irrigation_waiting.json": {
    "window_end": "2026-07-02T04:59:00+00:00", "stamp": "2026-07-01T23:00:00",
    "trigger": "nightly"}}
MISSED = {"irrigation_waiting.json": {
    "window_end": "2026-07-02T01:00:00+00:00", "stamp": "2026-07-01T23:00:00",
    "trigger": "nightly"}}
# LEGACY BUG, ported faithfully: _plan_and_run writes window_end from the AWARE
# ctx["end"] (sun sensor ISO carries +00:00), but _on_startup compares it with the
# NAIVE dt.datetime.now().isoformat(), so recovery.startup_action raises
# "TypeError: can't compare offset-naive and offset-aware datetimes" for every
# real marker. The aware scenarios pin that crash on both sides; the naive twins
# below are the only way to drive the RE_ARM / MISSED branches at all.
WAITING_NAIVE = {"irrigation_waiting.json": {
    "window_end": "2026-07-02T04:59:00", "stamp": "2026-07-01T23:00:00",
    "trigger": "nightly"}}
MISSED_NAIVE = {"irrigation_waiting.json": {
    "window_end": "2026-07-02T01:00:00", "stamp": "2026-07-01T23:00:00",
    "trigger": "nightly"}}

# name -> (start time, seed files, world tweak, exception _on_startup raises)
STARTUP = {
    "daytime_noop": ("2026-07-02 14:00:00", {}, lambda w: None, None),
    "collapsed_marker": ("2026-07-02 02:00:00", {},
                         lambda w: w.set("switch.geodrops_rachio_run_active", "on"), None),
    "orphan_valve": ("2026-07-02 02:00:00", {},
                     lambda w: w.rachio.start_direct([("switch.front_zone", 30)]), None),
    "waiting_rearm": ("2026-07-02 00:30:00", WAITING_NAIVE, lambda w: None, None),
    "waiting_missed": ("2026-07-02 02:00:00", MISSED_NAIVE, lambda w: None, None),
    "waiting_aware_marker_crashes": ("2026-07-02 00:30:00", WAITING, lambda w: None,
                                     TypeError),
    "missed_aware_marker_crashes": ("2026-07-02 02:00:00", MISSED, lambda w: None,
                                    TypeError),
}

STOP = ("rachio", "stop_watering", {"devices": "Main House"})
MARKER_OFF = ("switch", "turn_off", {"entity_id": "switch.geodrops_rachio_run_active"})


def _logbook(world) -> list[str]:
    return [d["message"] for dom, svc, d in world.calls if (dom, svc) == ("logbook", "log")]


def _assert_startup_branch(name, lw, lf):
    """Confirm the LEGACY side reached the branch the scenario is named for, so
    no scenario can silently collapse into the daytime no-op and still 'match'."""
    statuses = [v for v, _d in status_trail_legacy(lw)]
    book = _logbook(lw)
    last_run = lw.published.get("pyscript.geodrops_rachio_last_run")
    if name == "daytime_noop":
        # Only the startup "idle" publish; the time gate returned before any check.
        assert status_trail_legacy(lw) == [("idle", None)]
        assert [c for c in lw.calls if c[0] != "logbook"] == []
        assert book == []
        assert last_run is None
    elif name == "collapsed_marker":
        assert lw.calls[0] == STOP and lw.calls[1] == MARKER_OFF
        assert any("interrupted collapsed run was detected" in m for m in book)
        assert "planning" in statuses and "watering" in statuses
        assert last_run[1]["trigger"] == "startup-heal"
    elif name == "orphan_valve":
        assert lw.calls[0] == ("switch", "turn_off", {"entity_id": "switch.front_zone"})
        assert any("closed 1 zone(s) left open" in m and "switch.front_zone" in m
                   for m in book)
        assert any("re-planning the interrupted run" in m for m in book)
        assert "planning" in statuses and "watering" in statuses
        assert last_run[1]["trigger"] == "startup-heal"
    elif name == "waiting_rearm":
        assert any("was waiting for its pre-dawn window when HA restarted" in m
                   for m in book)
        assert "planning" in statuses and "watering" in statuses
        assert last_run[1]["trigger"] == "startup-heal"
        assert last_run[1]["aborted_reason"] is None
        assert "irrigation_waiting.json" not in lf.files      # consumed
    elif name == "waiting_missed":
        assert status_trail_legacy(lw) == [
            ("idle", None), ("skipped", "missed — restart after window closed")]
        assert any("recorded as missed" in m for m in book)
        assert last_run[1]["skipped"] == "missed-restart"
        assert last_run[1]["updated"] == "2026-07-01T23:00:00"
        assert last_run[1]["trigger"] == "nightly"
        assert "irrigation_waiting.json" not in lf.files      # consumed
    elif name in ("waiting_aware_marker_crashes", "missed_aware_marker_crashes"):
        # Crashed inside recovery.startup_action, AFTER consuming the marker and
        # before any branch: the night is lost with no record (legacy bug).
        assert status_trail_legacy(lw) == [("idle", None)]
        assert [c for c in lw.calls if c[0] != "logbook"] == []
        assert book == []
        assert last_run is None
        assert "irrigation_waiting.json" not in lf.files
    else:  # pragma: no cover - every scenario must be pinned
        raise AssertionError(f"no branch evidence for {name}")


def _maybe_raises(exc):
    return pytest.raises(exc) if exc is not None else contextlib.nullcontext()


@pytest.mark.parametrize("name", list(STARTUP))
async def test_startup_matches_legacy(freezer, name, caplog):
    caplog.set_level(logging.INFO, logger=ENGINE_LOGGER_PREFIX)
    start, files, tweak, raises = STARTUP[name]
    data = entry_data()

    freezer.move_to(start)
    lw = FakeWorld(freezer)
    populate(lw, data)
    tweak(lw)
    lf = LegacyFiles()
    lf.files.update(files)
    ns = load_legacy(lw, build_config(data), lf)
    with _maybe_raises(raises):
        ns["_on_startup"]()
    _assert_startup_branch(name, lw, lf)

    freezer.move_to(start)
    nw = FakeWorld(freezer)
    populate(nw, data)
    tweak(nw)
    eng = native_scheduler(nw, data, docs={LEGACY_FILE_KEYS[f]: v for f, v in files.items()})
    with _maybe_raises(raises):
        await eng._on_startup()
    if eng.run_task is not None:
        await eng.run_task

    assert_same_effects(lw, lf, nw, eng)
    assert log_trail_native(caplog) == log_trail_legacy(lw)


async def test_manual_stop_mid_run_matches_legacy(freezer, caplog):
    caplog.set_level(logging.INFO, logger=ENGINE_LOGGER_PREFIX)
    data = entry_data()
    freezer.move_to("2026-07-01 23:00:00")
    lw = FakeWorld(freezer)
    populate(lw, data)
    lf = LegacyFiles()
    ns = load_legacy(lw, build_config(data), lf)
    lw.at("2026-07-02 04:20:00", lambda: ns["_on_stop_button"]("2026-07-02T04:20:00"))
    ns["irrigation_nightly"]()

    freezer.move_to("2026-07-01 23:00:00")
    nw = FakeWorld(freezer)
    populate(nw, data)
    eng = native_scheduler(nw, data)
    nw.at("2026-07-02 04:20:00", eng.request_stop)
    await eng.irrigation_nightly()
    await eng.run_task

    assert_same_effects(lw, lf, nw, eng)
    assert log_trail_native(caplog) == log_trail_legacy(lw)
    assert lw.published["pyscript.geodrops_rachio_last_run"][1]["aborted_reason"] == "manual-abort"
    assert "Stop button pressed" in _logbook(lw)


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


async def test_unload_mid_pause_stops_the_device(freezer):
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
    await eng.async_shutdown()
    assert eng.run_task is None
    tail = w.calls[-4:]
    assert ("rachio", "stop_watering", {"devices": "Main House"}) in tail
    assert ("switch", "turn_off", {"entity_id": "switch.front_zone"}) in tail
    assert w.get("switch.geodrops_rachio_run_active") == "off"


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
