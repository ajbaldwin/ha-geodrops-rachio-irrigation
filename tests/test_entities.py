import pytest
from unittest.mock import AsyncMock, patch

from freezegun import freeze_time
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


async def test_action_buttons_created(hass, enable_pyscript_and_rachio):
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for key in ("run_now", "preview", "reset", "refresh_runtimes"):
        assert hass.states.get(f"button.geodrops_rachio_{key}") is not None


async def test_action_button_calls_pyscript_service(hass, enable_pyscript_and_rachio):
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    called = []
    hass.services.async_register(
        "pyscript", "geodrops_rachio_run_now", lambda call: called.append(call))
    await hass.services.async_call(
        "button", "press",
        {"entity_id": "button.geodrops_rachio_run_now"}, blocking=True)
    await hass.async_block_till_done()
    assert len(called) == 1


async def test_action_button_no_raise_when_service_absent(hass, enable_pyscript_and_rachio):
    """Pressing before the pyscript action is registered (pyscript not ready or
    the script not delivered yet) must not raise, only warn."""
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # No pyscript.geodrops_rachio_preview service registered -> must be a no-op.
    await hass.services.async_call(
        "button", "press",
        {"entity_id": "button.geodrops_rachio_preview"}, blocking=True)
    await hass.async_block_till_done()


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


async def test_zone_exclude_switch_restores_state(hass, enable_pyscript_and_rachio):
    """The exclude toggle is a RestoreEntity — its on/off survives a restart."""
    from homeassistant.core import State
    from pytest_homeassistant_custom_component.common import mock_restore_cache
    mock_restore_cache(
        hass, [State("switch.geodrops_rachio_front_slope_exclude", "on")])
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
    assert hass.states.get(
        "switch.geodrops_rachio_front_slope_exclude").state == "on"


async def test_observed_sensor_buffers_and_averages(hass, enable_pyscript_and_rachio):
    data = {**ENTRY_DATA, "bindings": {
        "weather": {"temperature": "sensor.station_temp"}}}
    # The sensor stamps and windows samples against local time, so pin both the
    # timezone and the clock: 23:00 in UTC is inside the 20:00->06:00 window.
    await hass.config.async_set_time_zone("UTC")
    with freeze_time("2026-06-15 23:00:00"):
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


async def test_observed_window_is_local_not_utc(hass, enable_pyscript_and_rachio):
    """The overnight window follows the home's timezone, not UTC. Pinned at a
    moment that is inside local overnight (05:00 US-Eastern) but OUTSIDE the
    UTC overnight window (10:00 UTC) — a UTC-based window would drop the samples
    and read 'unknown'."""
    data = {**ENTRY_DATA, "bindings": {
        "weather": {"temperature": "sensor.station_temp"}}}
    await hass.config.async_set_time_zone("America/New_York")
    # 2026-01-15 10:00 UTC == 05:00 EST (winter, UTC-5): inside 20:00->06:00
    # local, outside 20:00->06:00 UTC.
    with freeze_time("2026-01-15 10:00:00"):
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
         "end": "06:00", "updated": "2026-09-13T06:00:00+00:00",
         "calibration": {"front": {"state": "calibrating", "efficacy": 0.35}}})
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

    moisture = hass.states.get("sensor.geodrops_rachio_front_soil_moisture")
    assert moisture.state == "71.5"
    assert moisture.attributes["unit_of_measurement"] == "%"
    assert moisture.attributes["device_class"] == "moisture"
    assert hass.states.get("sensor.geodrops_rachio_front_last_delivered_runtime").state == "42.0"
    efficacy = hass.states.get("sensor.geodrops_rachio_front_efficacy")
    assert efficacy.attributes["unit_of_measurement"] == "%/min"
    assert efficacy.state == "0.35"  # from the live nightly calibration
    # Last watered comes from the record's full ISO `updated`, not time-only `end`.
    lw = hass.states.get("sensor.geodrops_rachio_front_last_watered")
    assert lw.state not in ("unknown", "unavailable")
    # Calibration state comes from the nightly `calibration` attr, not the file,
    # and is title-cased for display ("calibrating" -> "Calibrating").
    assert hass.states.get("sensor.geodrops_rachio_front_calibration_state").state == "Calibrating"
    # Refill depth: no rachio_zone_id and no live runtimes entity here, so the
    # sensor shows the static config value (mm) captured at wizard time.
    refill = hass.states.get("sensor.geodrops_rachio_front_refill_depth")
    assert float(refill.state) == 10.0
    assert refill.attributes["unit_of_measurement"] == "mm"
    # No device_class on purpose: DISTANCE would let HA convert mm to the
    # install's length unit (imperial -> inches, rounding small values to 0).
    assert "device_class" not in refill.attributes


async def test_calibration_state_shows_progress_and_reason(hass, enable_pyscript_and_rachio):
    """The Calibration State label folds in probe progress and, when stuck, why."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.geodrops_rachio.const import DOMAIN
    hass.states.async_set(
        "pyscript.geodrops_rachio_last_nightly", "1",
        {"calibration": {
            "front": {"state": "calibrating", "n_obs": 2},
            "side": {"state": "calibrating", "n_obs": 1,
                     "last_reject_reason": "saturated"}}})
    entry = MockConfigEntry(domain=DOMAIN, data={
        "bindings": {"weather": {}, "forecast_entity": "weather.home"},
        "zones": [
            {"key": "front", "rachio_switch": "switch.x", "dominant_sensor": "s.d",
             "state_sensor": "s.s", "quality_sensors": [], "target_range": "moist",
             "runtime_minutes": 20, "refill_depth_mm": 10},
            {"key": "side", "rachio_switch": "switch.y", "dominant_sensor": "s.e",
             "state_sensor": "s.f", "quality_sensors": [], "target_range": "moist",
             "runtime_minutes": 20, "refill_depth_mm": 10}],
        "self_calibration_enabled": False, "advanced_overrides": ""})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # Progress toward convergence.
    assert hass.states.get(
        "sensor.geodrops_rachio_front_calibration_state").state == "Calibrating (2/3)"
    # A reject reason wins over the count.
    assert hass.states.get(
        "sensor.geodrops_rachio_side_calibration_state").state == "Calibrating — soil too wet"


async def test_zone_deficit_sensor(hass, enable_pyscript_and_rachio):
    """Deficit = max(0, target_floor - current moisture), live and unit-safe."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.geodrops_rachio.const import DOMAIN
    hass.states.async_set("sensor.d", "30.0")  # current dominant moisture
    hass.states.async_set(
        "pyscript.geodrops_rachio_targets", "1",
        {"target_floors": {"front": 45.0}})
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
    d = hass.states.get("sensor.geodrops_rachio_front_deficit")
    assert float(d.state) == 15.0  # 45 - 30
    assert d.attributes["unit_of_measurement"] == "%"
    # A delta, not an absolute moisture — must not be unit-converted.
    assert "device_class" not in d.attributes
    # Live: rising moisture at/above the floor drives the deficit to 0.
    hass.states.async_set("sensor.d", "50.0")
    await hass.async_block_till_done()
    assert float(hass.states.get("sensor.geodrops_rachio_front_deficit").state) == 0.0


async def test_zone_deficit_unknown_without_target(hass, enable_pyscript_and_rachio):
    """No published target floor -> deficit reads unknown, not a bogus number."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.geodrops_rachio.const import DOMAIN
    hass.states.async_set("sensor.d", "30.0")
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
    assert hass.states.get(
        "sensor.geodrops_rachio_front_deficit").state == "unknown"


async def test_refill_depth_prefers_live_rachio_value(hass, enable_pyscript_and_rachio):
    """A zone with a rachio_zone_id shows the live refill depth published on the
    runtimes entity, overriding the static config value."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.geodrops_rachio.const import DOMAIN
    hass.states.async_set(
        "pyscript.geodrops_rachio_runtimes", "1",
        {"refill_depths_mm": {"zone-abc": 17.5}})
    entry = MockConfigEntry(domain=DOMAIN, data={
        "bindings": {"weather": {}, "forecast_entity": "weather.home"},
        "zones": [{"key": "front", "rachio_switch": "switch.x",
                   "dominant_sensor": "sensor.d", "state_sensor": "sensor.s",
                   "quality_sensors": [], "target_range": "moist",
                   "runtime_minutes": 20, "refill_depth_mm": 10,
                   "rachio_zone_id": "zone-abc"}],
        "self_calibration_enabled": False, "advanced_overrides": ""})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    refill = hass.states.get("sensor.geodrops_rachio_front_refill_depth")
    assert float(refill.state) == 17.5  # live Rachio value, not the static 10


async def test_scheduler_status_sensor_mirrors_pyscript(hass, enable_pyscript_and_rachio):
    """The main device gets a Status sensor mirroring the scheduler's status."""
    hass.states.async_set(
        "pyscript.geodrops_rachio_status", "waiting",
        {"detail": "watering starts 01:44"})
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    s = hass.states.get("sensor.geodrops_rachio_status")
    assert s is not None and s.state == "Waiting"  # title-cased for display
    assert s.attributes["detail"] == "watering starts 01:44"


def test_pretty_status_titlecases_tokens_and_preserves_sentinels():
    from custom_components.geodrops_rachio.sensor import _pretty_status
    assert _pretty_status("calibrating") == "Calibrating"
    assert _pretty_status("watering") == "Watering"
    assert _pretty_status("no_rise") == "No Rise"          # underscore -> space
    assert _pretty_status("recalibrating") == "Recalibrating"
    # HA sentinels and non-strings pass through untouched.
    assert _pretty_status("unknown") == "unknown"
    assert _pretty_status("unavailable") == "unavailable"
    assert _pretty_status(None) is None
    assert _pretty_status("") == ""


async def test_zone_soil_moisture_non_numeric_source_reads_none(hass, enable_pyscript_and_rachio):
    """A moisture device_class must be numeric — an unavailable dominant sensor
    must read as no value, not push HA a non-numeric state."""
    hass.states.async_set("sensor.d", "unavailable")
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
    assert hass.states.get("sensor.geodrops_rachio_front_soil_moisture").state == "unknown"


async def test_zone_device_uses_friendly_name(hass, enable_pyscript_and_rachio):
    """Zone devices are named from a title-cased key, not the raw slug."""
    from homeassistant.helpers import device_registry as dr
    entry = MockConfigEntry(domain=DOMAIN, data={
        "bindings": {"weather": {}, "forecast_entity": "weather.home"},
        "zones": [{"key": "front_slope", "rachio_switch": "switch.x",
                   "dominant_sensor": "sensor.d", "state_sensor": "sensor.s",
                   "quality_sensors": [], "target_range": "moist",
                   "runtime_minutes": 20, "refill_depth_mm": 10}],
        "self_calibration_enabled": False, "advanced_overrides": ""})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, f"{entry.entry_id}:zone:front_slope")})
    assert device is not None and device.name == "Front Slope"
