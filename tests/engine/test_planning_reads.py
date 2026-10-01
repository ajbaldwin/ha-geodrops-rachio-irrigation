"""PlanningMixin's reads degrade instead of crashing: a missing or unusable
entity falls back to a safe default, with a warning where it is a
misconfiguration."""
import datetime as dt
import logging
from types import SimpleNamespace

from custom_components.geodrops_rachio.engine.io import IOMixin
from custom_components.geodrops_rachio.engine.planning import PlanningMixin
from tests.engine.scenario import T_PLAN, entry_data, native_engine, populate
from tests.engine.world import FakeWorld


def _eng(freezer, data=None):
    freezer.move_to(T_PLAN)
    world = FakeWorld(freezer)
    data = data or entry_data()
    populate(world, data)
    eng = native_engine(world, data, PlanningMixin, IOMixin)
    eng._current_cfg = eng._load_cfg()
    eng._current_bindings = eng._current_cfg.bindings
    eng._current_tun = eng._current_cfg.tunables
    return world, eng


async def test_zone_without_an_exclude_binding_is_not_excluded(freezer):
    _world, eng = _eng(freezer)
    assert eng._zone_excluded(SimpleNamespace(exclude_boolean="")) is False


async def test_no_rain_condition_before_a_config_is_loaded(freezer):
    _world, eng = _eng(freezer)
    eng._current_tun = None
    assert eng._rain_condition_now() == (False, False)


async def test_spray_zone_rain_needs_the_gauge(freezer):
    """While a spray-flagged zone waters, "rain" with a dry gauge is its own
    overspray; a wet gauge makes it rain."""
    data = entry_data()
    data["zones"][0]["spray"] = True
    world, eng = _eng(freezer, data)
    world.rachio.running_switch = lambda: "switch.front_zone"
    world.set("sensor.tempest_sensor_precipitation_type", "rain")
    assert eng._rain_condition_now() == (False, False)
    world.set("sensor.tempest_rain_last_hour", "0.5")
    assert eng._rain_condition_now() == (True, False)


async def test_missing_or_bad_weather_reads_as_defaults(freezer, caplog):
    world, eng = _eng(freezer)
    world.remove("sensor.tempest_sensor_temperature")
    world.set("sensor.tempest_sensor_humidity", "unavailable")
    world.remove("sensor.tempest_sensor_precipitation_type")
    with caplog.at_level(logging.WARNING):
        wx = eng._read_weather(eng._current_tun)
    assert (wx.temp_f, wx.rh_pct, wx.precip_type) == (0.0, 0.0, "none")
    warned = " ".join(r.getMessage() for r in caplog.records)
    assert "sensor.tempest_sensor_temperature" in warned
    assert "sensor.tempest_sensor_precipitation_type" in warned


async def test_unusable_forecast_reads_as_none(freezer, caplog):
    world, eng = _eng(freezer)
    world.set("sensor.forecast_overnight_temp", "unknown")
    with caplog.at_level(logging.WARNING):
        assert eng._read_forecast_weather() is None
    assert "unusable" in caplog.text


async def test_last_updated_of_a_missing_entity_is_none(freezer):
    _world, eng = _eng(freezer)
    assert eng._sensor_last_updated("sensor.nope") is None


async def test_missing_sunrise_anchor_falls_back_to_dawn(freezer, caplog):
    world, eng = _eng(freezer)
    world.remove("sensor.sun_next_rising")
    with caplog.at_level(logging.WARNING):
        end = eng._end_anchor_time("sunrise")
    assert end == dt.datetime.fromisoformat("2026-07-02T04:30:00+00:00")
    assert "using dawn" in caplog.text


async def test_unknown_drought_level_defaults_to_critical(freezer, caplog):
    world, eng = _eng(freezer)
    world.set("select.geodrops_rachio_drought_level", "Level 9")
    with caplog.at_level(logging.WARNING):
        level, profile = eng._resolve_profile(eng._current_cfg)
    assert level == "Level 3 - Critical"
    assert profile is eng._current_cfg.drought_profiles["Level 3 - Critical"]
    assert "Level 9" in caplog.text
