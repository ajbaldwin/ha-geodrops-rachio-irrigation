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


def _utc(s):
    return dt.datetime.fromisoformat(s)


async def test_window_end_without_finish_controls_is_the_levels_own(freezer):
    """No Finish anchor/offset entities (an entry set up before them): Level 1
    ends 5 min before sunrise, as before."""
    _world, eng = _eng(freezer)
    profile = eng._current_cfg.drought_profiles["Level 1 - Mild"]
    assert eng._window_end(eng._current_cfg, profile) == (
        "sunrise", 5, _utc("2026-07-02T04:55:00+00:00"))


async def test_finish_anchor_replaces_the_levels_anchor(freezer):
    world, eng = _eng(freezer)
    world.set("select.geodrops_rachio_finish_anchor", "dawn")
    profile = eng._current_cfg.drought_profiles["Level 1 - Mild"]
    assert eng._window_end(eng._current_cfg, profile) == (
        "dawn", 5, _utc("2026-07-02T04:25:00+00:00"))
    # Auto keeps the level's own, at Level 3 too.
    world.set("select.geodrops_rachio_finish_anchor", "auto")
    profile = eng._current_cfg.drought_profiles["Level 3 - Critical"]
    assert eng._window_end(eng._current_cfg, profile)[0] == "dawn"


async def test_finish_anchor_sunrise_applies_at_every_level(freezer):
    world, eng = _eng(freezer)
    world.set("select.geodrops_rachio_finish_anchor", "sunrise")
    profile = eng._current_cfg.drought_profiles["Level 3 - Critical"]
    assert eng._window_end(eng._current_cfg, profile)[0] == "sunrise"


async def test_finish_offset_shifts_the_end_later_when_positive(freezer):
    world, eng = _eng(freezer)
    world.set("number.geodrops_rachio_finish_offset", "30.0")
    profile = eng._current_cfg.drought_profiles["Level 1 - Mild"]
    assert eng._window_end(eng._current_cfg, profile) == (
        "sunrise", -25, _utc("2026-07-02T05:25:00+00:00"))
    world.set("number.geodrops_rachio_finish_offset", "-30")
    assert eng._window_end(eng._current_cfg, profile)[2] == _utc(
        "2026-07-02T04:25:00+00:00")


async def test_finish_offset_shifts_a_per_level_override(freezer):
    """The slider moves the level's configured end, so a per-level
    end_offset_minutes keeps applying underneath it."""
    data = entry_data(overrides=(
        'drought_profiles:\n  "Level 1 - Mild": {end_offset_minutes: -30}\n'))
    world, eng = _eng(freezer, data)
    world.set("number.geodrops_rachio_finish_offset", "15")
    profile = eng._current_cfg.drought_profiles["Level 1 - Mild"]
    assert eng._window_end(eng._current_cfg, profile)[2] == _utc(
        "2026-07-02T05:45:00+00:00")


async def test_plan_context_reports_the_finish_controls(freezer):
    world, eng = _eng(freezer)
    world.set("select.geodrops_rachio_finish_anchor", "dawn")
    world.set("number.geodrops_rachio_finish_offset", "-10")
    ctx = await eng._plan_context(eng._current_cfg)
    assert ctx["end_anchor"] == "dawn"
    assert ctx["end_offset_minutes"] == 15
    assert ctx["end"] == _utc("2026-07-02T04:15:00+00:00")


async def test_project_window_from_the_forecast(freezer):
    world, eng = _eng(freezer)
    world.set("select.geodrops_rachio_finish_anchor", "auto")
    world.set("number.geodrops_rachio_finish_offset", "10")
    w = eng.project_window()
    assert w is not None
    assert (w["start"], w["end"]) == (
        _utc("2026-07-01T23:05:00+00:00"), _utc("2026-07-02T05:05:00+00:00"))
    assert w["anchor"] == "sunrise" and w["anchor_choice"] == "auto"
    assert (w["shift_minutes"], w["end_offset_minutes"]) == (10, -5)
    assert (w["cap_hours"], w["cap_source"], w["pressure"]) == (
        6.0, "forecast", [])
    assert w["drought_level"] == "Level 1 - Mild"


async def test_project_window_shrinks_under_disease_pressure(freezer):
    world, eng = _eng(freezer)
    world.set("sensor.forecast_overnight_temp", "75")
    world.set("sensor.forecast_overnight_humidity", "95")
    w = eng.project_window()
    assert w is not None
    assert w["pressure"] == ["warm", "humid"]
    assert w["cap_hours"] == 4.0


async def test_project_window_falls_back_quietly(freezer, caplog):
    """No forecast: sized from current conditions; nothing at all: the longest
    window. Neither logs, since it re-reads on every input change."""
    world, eng = _eng(freezer)
    world.set("sensor.forecast_overnight_temp", "unknown")
    with caplog.at_level(logging.WARNING):
        assert eng.project_window()["cap_source"] == "instant"
        world.remove("sensor.tempest_sensor_temperature")
        w = eng.project_window()
    assert (w["cap_source"], w["cap_hours"]) == ("default", 6.0)
    assert caplog.text == ""


async def test_project_window_is_none_without_a_level_or_sun(freezer):
    world, eng = _eng(freezer)
    world.set("select.geodrops_rachio_drought_level", "unknown")
    assert eng.project_window() is None
    world.set("select.geodrops_rachio_drought_level", "Level 1 - Mild")
    world.remove("sensor.sun_next_rising")
    world.remove("sensor.sun_next_dawn")
    assert eng.project_window() is None


async def test_project_window_installs_no_config(freezer):
    """Safe beside a run: it never replaces the run's loaded config."""
    _world, eng = _eng(freezer)
    cfg, bindings = eng._current_cfg, eng._current_bindings
    eng.project_window()
    assert eng._current_cfg is cfg and eng._current_bindings is bindings


async def test_window_inputs_list_what_the_projection_reads(freezer):
    _world, eng = _eng(freezer)
    inputs = eng.window_inputs()
    for e in ("select.geodrops_rachio_drought_level",
              "select.geodrops_rachio_finish_anchor",
              "number.geodrops_rachio_finish_offset",
              "sensor.sun_next_dawn", "sensor.sun_next_rising",
              "sensor.forecast_overnight_temp"):
        assert e in inputs
