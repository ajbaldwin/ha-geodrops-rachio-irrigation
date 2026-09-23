import copy

import pytest

from custom_components.geodrops_rachio.engine import store as es
from custom_components.geodrops_rachio.engine.io import IOMixin
from tests.engine.scenario import T_PLAN, entry_data, native_engine, populate
from tests.engine.world import FakeWorld


def _eng(freezer, **kw):
    freezer.move_to(T_PLAN)
    world = FakeWorld(freezer)
    data = entry_data()
    populate(world, data)
    eng = native_engine(world, data, IOMixin, **kw)
    eng._current_cfg = eng._load_cfg()
    eng._current_bindings = eng._current_cfg.bindings
    eng._current_tun = eng._current_cfg.tunables
    return world, eng


async def test_start_block_hands_rachio_one_schedule(freezer):
    from custom_components.geodrops_rachio.brain.blocks import ZoneRun
    world, eng = _eng(freezer)
    runs = [ZoneRun("front", 12), ZoneRun("back", 10), ZoneRun("front", 5)]
    await eng.start_block(runs, {"front": "switch.front_zone", "back": "switch.back_zone"})
    assert world.calls[-1] == ("rachio", "start_multiple_zone_schedule", {
        "entity_id": ["switch.front_zone", "switch.back_zone", "switch.front_zone"],
        "duration": "12,10,5"})
    assert eng.api_calls == 1


async def test_pause_clamps_and_stop_device_swallows(freezer):
    world, eng = _eng(freezer)
    await eng.pause_device(500)
    assert world.calls[-1] == ("rachio", "pause_watering",
                               {"devices": "Main House", "duration": 60})
    await eng.pause_device(0)
    assert world.calls[-1][2]["duration"] == 1
    world.failing.add(("rachio", "stop_watering"))
    await eng.stop_device()          # must not raise
    assert eng.api_calls == 3


async def test_poll_counts_state_polls_not_api_calls(freezer):
    world, eng = _eng(freezer)
    assert eng.any_zone_running(["switch.front_zone", "switch.back_zone"]) is False
    assert eng.state_polls == 2 and eng.api_calls == 0


async def test_runtime_cache_ttl(freezer):
    world, eng = _eng(freezer)
    world.rachio_api = ({"id-front": 33.0}, {"id-front": 8.0}, {"id-front": 20.0})
    calls = []
    orig = eng._fetch_zone_data_fn

    async def counting(key):
        calls.append(key)
        return await orig(key)

    eng._fetch_zone_data_fn = counting
    assert await eng.get_runtimes() == {"id-front": 33.0}
    assert await eng.get_refill_depths() == {"id-front": 8.0}
    assert calls == ["rachio_api_key"]            # cached
    freezer.tick(6 * 3600 + 1)
    await eng.get_refill_spans()
    assert len(calls) == 2                        # TTL expired
    world.rachio_api = None
    assert await eng.get_runtimes(force=True) == {}   # fetch failure -> {}


async def test_efficacy_and_pending_obs_roundtrip(freezer):
    world, eng = _eng(freezer)
    assert eng._read_efficacy_store() == {}
    await eng._write_efficacy_store({"front": {"state": "calibrating"}})
    assert eng.store.read(es.EFFICACY) == {"front": {"state": "calibrating"}}
    await eng._append_pending_obs([{"zone": "front"}])
    await eng._append_pending_obs([{"zone": "back"}])
    assert [r["zone"] for r in eng.store.read(es.PENDING_OBS)] == ["front", "back"]


async def test_status_activity_notify(freezer):
    world, eng = _eng(freezer)
    seen = []
    eng.add_listener(lambda: seen.append(1))
    eng._set_status("waiting", detail="watering starts 03:10")
    assert eng.records["status"]["value"] == "waiting"
    assert eng.records["status"]["attributes"]["detail"] == "watering starts 03:10"
    assert seen == [1]
    await eng._activity("hello")
    assert world.calls[-1] == ("logbook", "log", {
        "name": "Irrigation", "message": "hello",
        "entity_id": "sensor.geodrops_rachio_status"})
    world.failing.add(("notify", "phone"))
    await eng._notify("msg", "Title")            # must not raise
    assert world.calls[-1] == ("notify", "phone", {"message": "msg", "title": "Title"})


async def test_publish_record_persists_and_restores(freezer):
    world, eng = _eng(freezer)
    await eng._publish_record("targets", 2, {"target_floors": {"front": 65.0}})
    assert eng.store.read(es.record_key("targets")) == {
        "value": 2, "attributes": {"target_floors": {"front": 65.0}}}
    world2, eng2 = _eng(freezer, docs=copy.deepcopy(eng.store._docs))
    assert eng2.records["targets"]["value"] == 2      # preloaded at construction


async def test_failing_record_listener_does_not_break_publish(freezer, caplog):
    world, eng = _eng(freezer)
    seen = []

    def boom():
        raise RuntimeError("async_write_ha_state blew up")
    eng.add_listener(boom)
    eng.add_listener(lambda: seen.append(eng.records["status"]["value"]))
    eng._set_status("planning")                   # must not raise
    await eng._publish_record("preview", 1, {"planned_minutes": {}})
    await eng.store.write(es.EFFICACY, {})        # store on_write path too
    assert eng.records["status"]["value"] == "planning"
    assert eng.store.read(es.record_key("preview"))["value"] == 1
    assert len(seen) == 4                         # later listeners still ran
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 4
    assert all("record listener" in r.getMessage() for r in errors)
    caplog.clear()                                # expected errors; keep output clean
