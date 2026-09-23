from custom_components.geodrops_rachio.config_writer import build_config
from tests.engine.legacy_harness import load_legacy
from tests.engine.scenario import T_PLAN, entry_data, populate
from tests.engine.world import FakeWorld


def _legacy(freezer, data=None):
    freezer.move_to(T_PLAN)
    data = data or entry_data()
    world = FakeWorld(freezer)
    populate(world, data)
    return world, load_legacy(world, build_config(data))


def test_legacy_nightly_waters_both_zones(freezer):
    world, ns = _legacy(freezer)
    ns["irrigation_nightly"]()
    starts = [c for c in world.calls if c[:2] == ("rachio", "start_multiple_zone_schedule")]
    assert len(starts) == 1
    value, attrs = world.published["pyscript.geodrops_rachio_last_nightly"]
    assert attrs["aborted_reason"] is None
    assert sorted(attrs["watered"]) == ["back", "front"]
    assert world.published["pyscript.geodrops_rachio_status"][0] == "idle"
    assert world.get("switch.geodrops_rachio_run_active") == "off"


def test_legacy_preview_publishes_plan_without_watering(freezer):
    world, ns = _legacy(freezer)
    ns["_preview"]()
    assert not [c for c in world.calls if c[0] == "rachio"]
    value, attrs = world.published["pyscript.geodrops_rachio_preview"]
    assert value == 2 and set(attrs["planned_minutes"]) == {"front", "back"}


def test_legacy_standby_skips(freezer):
    world, ns = _legacy(freezer)
    world.set("switch.geodrops_rachio_standby", "on")
    ns["irrigation_nightly"]()
    assert world.published["pyscript.geodrops_rachio_status"][0] == "standby"
    assert not [c for c in world.calls if c[0] == "rachio"]
