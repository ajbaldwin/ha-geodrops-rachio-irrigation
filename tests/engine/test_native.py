"""Rachio-native runs: watering started from the Rachio app or a Rachio schedule,
seen only through the zone switches, recorded as a `rachio` Last run."""
import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest

from custom_components.geodrops_rachio.brain import report_format
from custom_components.geodrops_rachio.engine.native import NATIVE_GAP_S
from custom_components.geodrops_rachio.engine.store import PENDING_OBS, ZONE_WATERED
from tests.engine.scenario import entry_data, native_scheduler, populate
from tests.engine.world import FakeWorld

FRONT, BACK = "switch.front_zone", "switch.back_zone"


def _scheduler(freezer, docs=None):
    data = entry_data()
    freezer.move_to("2026-07-02 13:00:00")
    w = FakeWorld(freezer)
    populate(w, data)
    eng = native_scheduler(w, data, docs=docs)
    eng._init_native_tracking()
    return w, eng


def _flip(eng, switch, old, new):
    eng._on_zone_switch(switch, old, new)


async def _settle(eng):
    """Let the all-off gap elapse and the session publish."""
    task = eng._native_close_task
    assert task is not None
    await task


def _logbook(world):
    return [d["message"] for dom, svc, d in world.calls if (dom, svc) == ("logbook", "log")]


async def test_one_zone_run_is_recorded_as_rachio(freezer):
    w, eng = _scheduler(freezer)
    _flip(eng, FRONT, "off", "on")
    w.advance(6 * 60)
    _flip(eng, FRONT, "on", "off")
    await _settle(eng)
    rec = eng.records["last_run"]
    assert rec["value"] == 1
    a = rec["attributes"]
    assert a["trigger"] == "rachio"
    assert a["watered"] == ["front"]
    assert a["delivered_minutes"] == {"front": 6.0}
    assert a["start"] == "13:00" and a["end"] == "13:06"
    assert a["end_iso"] == "2026-07-02T13:06:00+00:00"
    assert "last_nightly" not in eng.records
    assert any("Rachio run" in m and "front 6.0 min" in m for m in _logbook(w))


async def test_zones_back_to_back_are_one_session(freezer):
    w, eng = _scheduler(freezer)
    _flip(eng, FRONT, "off", "on")
    w.advance(5 * 60)
    _flip(eng, FRONT, "on", "off")
    w.advance(NATIVE_GAP_S - 30)                   # Rachio's inter-zone gap
    _flip(eng, BACK, "off", "on")
    w.advance(7.5 * 60)
    _flip(eng, BACK, "on", "off")
    await _settle(eng)
    a = eng.records["last_run"]["attributes"]
    assert a["watered"] == ["front", "back"]
    assert a["delivered_minutes"] == {"front": 5.0, "back": 7.5}
    assert a["start"] == "13:00"
    assert a["end_iso"] == "2026-07-02T13:14:00+00:00"   # last valve close, not + gap


async def test_a_long_gap_splits_sessions(freezer):
    w, eng = _scheduler(freezer)
    _flip(eng, FRONT, "off", "on")
    w.advance(5 * 60)
    _flip(eng, FRONT, "on", "off")
    await _settle(eng)
    assert eng.records["last_run"]["attributes"]["watered"] == ["front"]
    w.advance(3 * 3600)
    _flip(eng, BACK, "off", "on")
    w.advance(8 * 60)
    _flip(eng, BACK, "on", "off")
    await _settle(eng)
    a = eng.records["last_run"]["attributes"]
    assert a["watered"] == ["back"] and a["delivered_minutes"] == {"back": 8.0}


async def test_zone_watered_doc_keeps_every_zones_latest(freezer):
    w, eng = _scheduler(freezer)
    _flip(eng, FRONT, "off", "on")
    w.advance(5 * 60)
    _flip(eng, FRONT, "on", "off")
    await _settle(eng)
    w.advance(3600)
    _flip(eng, BACK, "off", "on")
    w.advance(2 * 60)
    _flip(eng, BACK, "on", "off")
    await _settle(eng)
    assert eng.store.read(ZONE_WATERED) == {
        "front": {"end_iso": "2026-07-02T13:05:00+00:00", "minutes": 5.0,
                  "trigger": "rachio"},
        "back": {"end_iso": "2026-07-02T14:09:00+00:00", "minutes": 2.0,
                 "trigger": "rachio"},
    }


async def test_our_own_watering_is_ignored(freezer):
    w, eng = _scheduler(freezer)
    eng._watering_active = True
    _flip(eng, FRONT, "off", "on")
    w.advance(10 * 60)
    _flip(eng, FRONT, "on", "off")
    assert eng._native_session is None and eng._native_close_task is None
    assert "last_run" not in eng.records


async def test_our_run_starting_closes_an_open_session(freezer):
    w, eng = _scheduler(freezer)
    _flip(eng, FRONT, "off", "on")
    w.advance(4 * 60)
    eng._watering_active = True                    # our run takes over
    _flip(eng, FRONT, "on", "off")                 # the runner stops it first
    await _settle(eng)
    a = eng.records["last_run"]["attributes"]
    assert a["trigger"] == "rachio" and a["delivered_minutes"] == {"front": 4.0}
    assert eng._native_session is None


async def test_a_switch_appearing_on_does_not_open_a_session(freezer):
    # HA starting (or the Rachio integration reloading) mid-run: the switch
    # arrives already on. Its start is unknown, so the run is not guessed at.
    w, eng = _scheduler(freezer)
    _flip(eng, FRONT, None, "on")
    _flip(eng, BACK, "unavailable", "on")
    assert eng._native_session is None
    w.advance(5 * 60)
    _flip(eng, FRONT, "on", "off")
    assert eng._native_session is None and eng._native_close_task is None


async def test_unmanaged_switch_and_attribute_only_changes_are_ignored(freezer):
    w, eng = _scheduler(freezer)
    _flip(eng, "switch.patio_lights", "off", "on")
    _flip(eng, FRONT, "off", "off")
    assert eng._native_session is None


async def test_pending_calibration_sample_is_dropped_for_watered_zones(freezer):
    pending = [
        {"zone": "front", "pre_dominant": 50.0, "minutes": 20,
         "run_end_iso": "2026-07-02T05:00:00+00:00",
         "peak": None, "retained": None, "last_seen_updated": None},
        {"zone": "back", "pre_dominant": 52.0, "minutes": 18,
         "run_end_iso": "2026-07-02T05:00:00+00:00",
         "peak": None, "retained": None, "last_seen_updated": None},
    ]
    w, eng = _scheduler(freezer, docs={PENDING_OBS: pending})
    _flip(eng, FRONT, "off", "on")
    w.advance(5 * 60)
    _flip(eng, FRONT, "on", "off")
    await _settle(eng)
    assert [r["zone"] for r in eng.store.read(PENDING_OBS)] == ["back"]
    assert any("calibration sample dropped" in m and "front" in m for m in _logbook(w))


async def test_no_pending_sample_logs_no_drop(freezer):
    w, eng = _scheduler(freezer)
    _flip(eng, FRONT, "off", "on")
    w.advance(5 * 60)
    _flip(eng, FRONT, "on", "off")
    await _settle(eng)
    assert not any("calibration sample dropped" in m for m in _logbook(w))


async def test_engine_run_updates_zone_watered(freezer):
    w, eng = _scheduler(freezer)
    eng._current_cfg = eng._load_cfg()
    eng._current_bindings = eng._current_cfg.bindings
    result = report_format.RunResult(
        watered=["front"], uncompleted={}, start="05:00", end="05:40",
        end_iso="2026-07-02T05:40:00+00:00", per_zone_minutes={"front": 40},
        standby=False)
    await eng._publish_last_run("2026-07-01T23:00:00", "run_now", result=result)
    assert eng.store.read(ZONE_WATERED) == {
        "front": {"end_iso": "2026-07-02T05:40:00+00:00", "minutes": 40,
                  "trigger": "run_now"}}


async def test_engine_night_with_nothing_watered_leaves_zone_watered_alone(freezer):
    w, eng = _scheduler(freezer, docs={ZONE_WATERED: {"front": {
        "end_iso": "2026-07-01T05:00:00+00:00", "minutes": 30, "trigger": "nightly"}}})
    eng._current_cfg = eng._load_cfg()
    eng._current_bindings = eng._current_cfg.bindings
    result = report_format.RunResult(
        watered=[], uncompleted={}, start="", end="", end_iso="",
        per_zone_minutes={}, standby=False)
    await eng._publish_last_run("2026-07-01T23:00:00", "nightly", result=result)
    assert eng.store.read(ZONE_WATERED)["front"]["minutes"] == 30


async def test_shutdown_cancels_a_pending_close(freezer):
    w, eng = _scheduler(freezer)
    _flip(eng, FRONT, "off", "on")
    w.advance(60)
    _flip(eng, FRONT, "on", "off")
    task = eng._native_close_task
    await asyncio.wait_for(eng.async_shutdown(), 1)
    assert task.cancelled()
    assert "last_run" not in eng.records


async def test_state_event_adapter(freezer):
    w, eng = _scheduler(freezer)

    def event(entity_id, old, new):
        st = lambda s: None if s is None else SimpleNamespace(state=s)
        return SimpleNamespace(data={"entity_id": entity_id,
                                     "old_state": st(old), "new_state": st(new)})
    eng._on_switch_event(event(FRONT, "off", "on"))
    assert set(eng._native_session["zones"]) == {"front"}
    w.advance(3 * 60)
    eng._on_switch_event(event(FRONT, "on", None))    # entity removed = off
    await _settle(eng)
    assert eng.records["last_run"]["attributes"]["delivered_minutes"] == {"front": 3.0}


async def test_config_load_failure_disables_tracking(freezer, caplog):
    w, eng = _scheduler(freezer)
    eng._load_raw_config = lambda: (_ for _ in ()).throw(ValueError("bad yaml"))
    eng._init_native_tracking()
    assert eng._native_switches == {}
    _flip(eng, FRONT, "off", "on")
    assert eng._native_session is None
    assert any("Rachio-native run tracking disabled" in r.getMessage()
               for r in caplog.records)


async def test_our_run_starting_inside_the_gap_closes_at_the_last_valve(freezer):
    w, eng = _scheduler(freezer)
    _flip(eng, FRONT, "off", "on")
    w.advance(5 * 60)
    _flip(eng, FRONT, "on", "off")
    gap = eng._native_close_task
    w.advance(30)
    eng._watering_active = True
    _flip(eng, BACK, "off", "on")                  # our first block opens a valve
    await _settle(eng)
    assert gap.cancelled()
    a = eng.records["last_run"]["attributes"]
    assert a["delivered_minutes"] == {"front": 5.0}
    assert a["end_iso"] == "2026-07-02T13:05:00+00:00"


async def test_a_zero_length_flicker_records_nothing(freezer):
    w, eng = _scheduler(freezer)
    _flip(eng, FRONT, "off", "on")
    _flip(eng, FRONT, "on", "off")
    await _settle(eng)
    assert "last_run" not in eng.records
    assert eng.store.read(ZONE_WATERED) is None


async def test_other_zones_pending_sample_is_kept(freezer):
    pending = [{"zone": "back", "pre_dominant": 52.0, "minutes": 18,
                "run_end_iso": "2026-07-02T05:00:00+00:00",
                "peak": None, "retained": None, "last_seen_updated": None}]
    w, eng = _scheduler(freezer, docs={PENDING_OBS: pending})
    _flip(eng, FRONT, "off", "on")
    w.advance(5 * 60)
    _flip(eng, FRONT, "on", "off")
    await _settle(eng)
    assert eng.store.read(PENDING_OBS) == pending
    assert not any("calibration sample dropped" in m for m in _logbook(w))


async def test_zone_watered_write_failure_only_warns(freezer, caplog):
    w, eng = _scheduler(freezer)
    real_write = eng.store.write

    async def failing(key, value):
        if key == ZONE_WATERED:
            raise OSError("disk full")
        await real_write(key, value)
    eng.store.write = failing
    _flip(eng, FRONT, "off", "on")
    w.advance(5 * 60)
    _flip(eng, FRONT, "on", "off")
    await _settle(eng)
    assert eng.records["last_run"]["attributes"]["trigger"] == "rachio"
    assert any("could not record zone watering" in r.getMessage() for r in caplog.records)


async def test_each_zone_of_a_rachio_session_keeps_its_own_close_time(freezer):
    w, eng = _scheduler(freezer)
    _flip(eng, FRONT, "off", "on")
    w.advance(5 * 60)
    _flip(eng, FRONT, "on", "off")                 # front closes 13:05
    w.advance(60)
    _flip(eng, BACK, "off", "on")
    w.advance(8 * 60)
    _flip(eng, BACK, "on", "off")                  # back closes 13:14
    await _settle(eng)
    zw = eng.store.read(ZONE_WATERED)
    assert zw["front"]["end_iso"] == "2026-07-02T13:05:00+00:00"
    assert zw["back"]["end_iso"] == "2026-07-02T13:14:00+00:00"
    assert eng.records["last_run"]["attributes"]["zone_end_iso"] == {
        "front": "2026-07-02T13:05:00+00:00", "back": "2026-07-02T13:14:00+00:00"}
