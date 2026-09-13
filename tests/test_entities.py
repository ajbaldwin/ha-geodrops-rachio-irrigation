import pytest
from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.geodrops_rachio.const import DOMAIN

ENTRY_DATA = {"bindings": {}, "zones": [], "self_calibration_enabled": False,
              "advanced_overrides": ""}


@pytest.fixture(autouse=True)
def _mock_delivery():
    """Entity-platform tests exercise real config entry setup, but delivery
    of the bundled pyscript app is out of scope here (bundled_app/ doesn't
    exist until the vendoring/release task) — stub it out."""
    with patch(
        "custom_components.geodrops_rachio.delivery.async_deliver",
        AsyncMock(return_value=False),
    ):
        yield


async def test_control_entities_created(hass, enable_pyscript_and_rachio):
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    sel = hass.states.get("select.geodrops_rachio_drought_level")
    assert sel is not None and sel.state == "Level 1 - Mild"
    assert hass.states.get("button.geodrops_rachio_stop") is not None


async def test_flag_switches_created_and_toggle(hass, enable_pyscript_and_rachio):
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for key in ("run_active", "standby", "dew_formed"):
        eid = f"switch.geodrops_rachio_{key}"
        assert hass.states.get(eid).state == "off"
    await hass.services.async_call(
        "switch", "turn_on",
        {"entity_id": "switch.geodrops_rachio_run_active"}, blocking=True)
    assert hass.states.get("switch.geodrops_rachio_run_active").state == "on"


async def test_zone_exclude_switch_created_per_zone(hass, enable_pyscript_and_rachio):
    entry = MockConfigEntry(domain=DOMAIN, data={
        "bindings": {"weather": {}, "forecast_entity": "weather.home"},
        "zones": [{"key": "Front Slope", "rachio_switch": "switch.x",
                   "dominant_sensor": "sensor.d", "state_sensor": "sensor.s",
                   "quality_sensors": [], "target_range": "moist",
                   "runtime_minutes": 20, "refill_depth_mm": 10}],
        "self_calibration_enabled": False, "advanced_overrides": ""})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("switch.geodrops_rachio_front_slope_exclude")
    assert state is not None
    assert state.state == "off"


async def test_observed_sensor_buffers_and_averages(hass, enable_pyscript_and_rachio):
    data = {**ENTRY_DATA, "bindings": {
        "weather": {"temperature": "sensor.station_temp"}}}
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set("sensor.station_temp", "10.0")
    await hass.async_block_till_done()
    hass.states.async_set("sensor.station_temp", "30.0")
    await hass.async_block_till_done()
    s = hass.states.get("sensor.geodrops_rachio_observed_overnight_temp")
    assert s is not None and float(s.state) == 20.0


async def test_zone_status_sensors(hass, enable_pyscript_and_rachio):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.geodrops_rachio.const import DOMAIN
    hass.states.async_set("sensor.d", "71.5")  # the zone's dominant_sensor
    hass.states.async_set(
        "pyscript.geodrops_rachio_last_nightly", "1",
        {"delivered_minutes": {"front": 42.0}, "watered": ["front"],
         "end": "2026-09-13T06:00:00+00:00"})
    entry = MockConfigEntry(domain=DOMAIN, data={
        "bindings": {"weather": {}, "forecast_entity": "weather.home"},
        "zones": [{"key": "front", "rachio_switch": "switch.x",
                   "dominant_sensor": "sensor.d", "state_sensor": "sensor.s",
                   "quality_sensors": [], "target_range": "moist",
                   "runtime_minutes": 20, "refill_depth_mm": 10}],
        "self_calibration_enabled": False, "advanced_overrides": ""})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.geodrops_rachio_front_soil_moisture").state == "71.5"
    assert hass.states.get("sensor.geodrops_rachio_front_last_delivered_runtime").state == "42.0"
