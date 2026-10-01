"""The engine reads weather in the scheduler's units (°F, mph, mm) whatever
unit each sensor reports — a metric weather station must not read °C as °F."""
import pytest

from custom_components.geodrops_rachio.engine.io import IOMixin
from custom_components.geodrops_rachio.engine.planning import PlanningMixin
from tests.engine.scenario import T_PLAN, entry_data, native_engine, populate
from tests.engine.world import FakeWorld


def _eng(freezer, weather=None):
    freezer.move_to(T_PLAN)
    world = FakeWorld(freezer)
    data = entry_data()
    if weather is not None:
        data["bindings"]["weather"] = weather
    populate(world, data)
    eng = native_engine(world, data, PlanningMixin, IOMixin)
    eng._current_cfg = eng._load_cfg()
    eng._current_bindings = eng._current_cfg.bindings
    eng._current_tun = eng._current_cfg.tunables
    return world, eng


def _metric_station(world):
    world.set("sensor.tempest_sensor_temperature", "21",
              {"unit_of_measurement": "°C"})
    world.set("sensor.tempest_sensor_humidity", "93",
              {"unit_of_measurement": "%"})
    world.set("sensor.tempest_sensor_wind_speed_average", "1.6",
              {"unit_of_measurement": "km/h"})
    world.set("sensor.tempest_rain_last_hour", "0.02",
              {"unit_of_measurement": "in"})


async def test_station_readings_are_converted(freezer):
    world, eng = _eng(freezer)
    _metric_station(world)
    wx = eng._read_weather(eng._current_tun)
    assert wx.temp_f == pytest.approx(69.8)
    assert wx.rh_pct == 93.0
    assert wx.wind_mph == pytest.approx(0.994, abs=1e-3)
    assert wx.rain_last_hour_mm == pytest.approx(0.508)


async def test_readings_without_a_unit_are_taken_as_scheduler_units(freezer):
    """The populated world carries no units: read as before."""
    _world, eng = _eng(freezer)
    wx = eng._read_weather(eng._current_tun)
    assert (wx.temp_f, wx.rh_pct, wx.wind_mph, wx.rain_last_hour_mm) == (
        60.0, 70.0, 5.0, 0.0)


async def test_overnight_and_precip_forecasts_are_converted(freezer):
    world, eng = _eng(freezer)
    world.set("sensor.forecast_overnight_temp", "15", {"unit_of_measurement": "°C"})
    world.set("sensor.forecast_overnight_wind", "2", {"unit_of_measurement": "m/s"})
    world.set("sensor.observed_overnight_temp", "59", {"unit_of_measurement": "°F"})
    world.set("sensor.precipitation_amount_12_hour", "0.5",
              {"unit_of_measurement": "in"})
    fc = eng._read_forecast_weather()
    assert fc.temp_f == pytest.approx(59.0)
    assert fc.wind_mph == pytest.approx(4.474, abs=1e-3)
    obs = eng._read_observed_overnight(eng._current_bindings)
    assert obs.temp_f == 59.0
    prob, amount = eng._read_forecast_precip(12)
    assert (prob, amount) == (10.0, pytest.approx(12.7))


async def test_rain_confounder_reads_the_gauge_in_mm(freezer):
    """0.05 in is 1.27 mm, over the 0.5 mm confounder; read raw it is not."""
    world, eng = _eng(freezer)
    world.set("sensor.tempest_precipitation_today", "0.05",
              {"unit_of_measurement": "in"})
    assert eng._rained_since_run(eng._current_bindings, eng._current_tun) is True
    world.set("sensor.tempest_precipitation_today", "0.05",
              {"unit_of_measurement": "mm"})
    assert eng._rained_since_run(eng._current_bindings, eng._current_tun) is False


async def test_rain_confounder_reads_the_bound_gauge(freezer):
    """A non-Tempest gauge picked in the wizard is the one read."""
    world, eng = _eng(freezer, weather={"rain_today": "sensor.backyard_rain_today"})
    world.set("sensor.backyard_rain_today", "3.0", {"unit_of_measurement": "mm"})
    assert eng._current_bindings.weather.rain_today == "sensor.backyard_rain_today"
    assert eng._rained_since_run(eng._current_bindings, eng._current_tun) is True


async def test_rain_confounder_fails_safe_on_a_missing_gauge(freezer):
    """A gauge entity that does not exist reads as no rain, not an error that
    would abort the settle pass."""
    world, eng = _eng(freezer)
    world.remove("sensor.tempest_precipitation_today")
    assert eng._rained_since_run(eng._current_bindings, eng._current_tun) is False
