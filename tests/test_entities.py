import pytest
from unittest.mock import AsyncMock, patch

from homeassistant.core import State
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry, async_fire_time_changed, mock_restore_cache_with_extra_data)

from custom_components.geodrops_rachio.const import DOMAIN

ENTRY_DATA = {"bindings": {}, "zones": [], "self_calibration_enabled": False,
              "advanced_overrides": ""}


@pytest.fixture(autouse=True)
def _quiet_scheduler(hass, tmp_path):
    """Real entry setup, but no 30 s startup task and an isolated config dir."""
    hass.config.config_dir = str(tmp_path)
    with patch(
        "custom_components.geodrops_rachio.engine.scheduler.Scheduler._on_startup",
        AsyncMock(),
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


async def test_action_button_calls_scheduler(hass, enable_pyscript_and_rachio):
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    with patch("custom_components.geodrops_rachio.engine.scheduler.Scheduler.async_run_now",
               AsyncMock()) as run_now:
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.geodrops_rachio_run_now"},
            blocking=True)
    run_now.assert_awaited_once()


async def test_stop_button_raises_manual_stop(hass, enable_pyscript_and_rachio):
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # request_stop also writes a Logbook entry; the test hass has no logbook.
    hass.services.async_register("logbook", "log", lambda call: None)
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.geodrops_rachio_stop"}, blocking=True)
    assert hass.data[DOMAIN][entry.entry_id]["scheduler"]._manual_stop is True


async def test_record_sensors_mirror_scheduler(hass, enable_pyscript_and_rachio):
    from tests.conftest import publish_record
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    publish_record(hass, entry, "last_nightly", 2,
                   {"friendly_name": "x", "watered": ["front"], "aborted_reason": None})
    await hass.async_block_till_done()
    st = hass.states.get("sensor.geodrops_rachio_last_nightly")
    assert st.state == "2"
    assert st.attributes["watered"] == ["front"]
    assert st.attributes["friendly_name"] != "x"
    assert hass.states.get("sensor.geodrops_rachio_last_run") is not None
    assert hass.states.get("sensor.geodrops_rachio_plan") is not None


async def test_record_sensors_count_zones(hass, enable_pyscript_and_rachio):
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for suffix in ("last_nightly", "last_run", "plan"):
        st = hass.states.get(f"sensor.geodrops_rachio_{suffix}")
        assert st.attributes["unit_of_measurement"] == "zones"


async def test_flag_switches_created_and_toggle(hass, enable_pyscript_and_rachio):
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("switch.geodrops_rachio_standby").state == "off"
    await hass.services.async_call(
        "switch", "turn_on",
        {"entity_id": "switch.geodrops_rachio_standby"}, blocking=True)
    assert hass.states.get("switch.geodrops_rachio_standby").state == "on"


async def test_run_active_is_a_read_only_indicator(hass, enable_pyscript_and_rachio):
    # Run active is informational: a binary_sensor mirroring the engine's
    # persisted run marker, with no switch to toggle.
    from custom_components.geodrops_rachio.engine.store import RUN_ACTIVE
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("switch.geodrops_rachio_run_active") is None
    eid = "binary_sensor.geodrops_rachio_run_active"
    assert hass.states.get(eid).state == "off"
    scheduler = hass.data[DOMAIN][entry.entry_id]["scheduler"]
    await scheduler.set_run_active(True)
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == "on"
    assert scheduler.store.read(RUN_ACTIVE) is True
    await scheduler.set_run_active(False)
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == "off"


@pytest.mark.parametrize("key", ["dew_formed", "run_active"])
async def test_retired_switch_is_removed(hass, enable_pyscript_and_rachio, key):
    # An install from before its removal still has the switch in the entity
    # registry; setup must drop it rather than leave it unavailable.
    from homeassistant.helpers import entity_registry as er
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    reg.async_get_or_create(
        "switch", DOMAIN, f"{entry.entry_id}_{key}",
        suggested_object_id=f"geodrops_rachio_{key}", config_entry=entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert reg.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_{key}") is None
    assert hass.states.get(f"switch.geodrops_rachio_{key}") is None


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
    # It gates calibration probing too, so the name says both. The entity id is
    # pinned, so renaming it breaks no automation.
    assert state.attributes["friendly_name"] == (
        "Front Slope Exclude from Watering/Calibration")


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


TEMP_DATA = {**ENTRY_DATA, "bindings": {"weather": {"temperature": "sensor.station_temp"}}}
OBSERVED_TEMP = "sensor.geodrops_rachio_observed_overnight_temp"


async def _setup_observed(hass, data=TEMP_DATA):
    """Set the entry up without the scheduler's 23:00 / 06:00 triggers: these
    tests move the clock across both."""
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    with patch("custom_components.geodrops_rachio.engine.scheduler.Scheduler.async_start"):
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _tick(hass, freezer, when):
    """Move the clock and fire the sensor's periodic recompute."""
    freezer.move_to(when)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_observed_sensor_is_time_weighted(hass, enable_pyscript_and_rachio, freezer):
    """10 held from before the window until 05:00, then 30 for the last hour:
    the 06:00 value is weighted by time (~12.9), not the event average (20)."""
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to("2026-06-15 22:00:00")
    await _setup_observed(hass)
    hass.states.async_set("sensor.station_temp", "10.0")
    await hass.async_block_till_done()
    freezer.move_to("2026-06-16 05:00:00")
    hass.states.async_set("sensor.station_temp", "30.0")
    await hass.async_block_till_done()
    await _tick(hass, freezer, "2026-06-16 06:00:00")
    assert float(hass.states.get(OBSERVED_TEMP).state) == pytest.approx(90.0 / 7)


async def test_observed_window_is_local_not_utc(hass, enable_pyscript_and_rachio, freezer):
    """The overnight window follows the home's timezone, not UTC. 10:00 UTC is
    05:00 EST: inside the local 23:00->06:00 window, outside the UTC one — a
    UTC-based window would read 'unknown'."""
    await hass.config.async_set_time_zone("America/New_York")
    freezer.move_to("2026-01-15 09:00:00")          # 04:00 EST
    await _setup_observed(hass)
    hass.states.async_set("sensor.station_temp", "10.0")
    await hass.async_block_till_done()
    await _tick(hass, freezer, "2026-01-15 10:00:00")
    assert float(hass.states.get(OBSERVED_TEMP).state) == 10.0


async def test_observed_sensor_seeds_from_the_source_at_startup(
        hass, enable_pyscript_and_rachio, freezer):
    """A steady source may not change all night; its current reading at startup
    still counts from then on."""
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to("2026-06-15 23:30:00")
    hass.states.async_set("sensor.station_temp", "15.0")
    await _setup_observed(hass)
    await _tick(hass, freezer, "2026-06-16 00:30:00")
    assert float(hass.states.get(OBSERVED_TEMP).state) == 15.0


async def test_observed_sensor_keeps_its_samples_across_a_restart(
        hass, enable_pyscript_and_rachio, freezer):
    """A restart mid-night must not reduce the window to the hours after it."""
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to("2026-06-16 01:00:00")
    mock_restore_cache_with_extra_data(hass, [(
        State(OBSERVED_TEMP, "10.0"),
        {"samples": [["2026-06-15T23:00:00+00:00", 10.0],
                     ["2026-06-16T00:00:00+00:00", 40.0]]})])
    await _setup_observed(hass)       # source sensor absent: nothing to seed
    await _tick(hass, freezer, "2026-06-16 02:00:00")
    # 10 for 1h, then 40 carried from 00:00 to 02:00.
    assert float(hass.states.get(OBSERVED_TEMP).state) == pytest.approx(30.0)


async def test_zone_status_sensors(hass, enable_pyscript_and_rachio):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.geodrops_rachio.const import DOMAIN
    from tests.conftest import publish_record
    hass.states.async_set("sensor.d", "71.5")  # the zone's dominant_sensor
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
    publish_record(
        hass, entry, "last_nightly", 1,
        {"delivered_minutes": {"front": 42.0}, "watered": ["front"],
         "end": "06:00", "updated": "2026-09-13T06:00:00+00:00",
         "calibration": {"front": {"state": "calibrating", "efficacy": 0.35}}})
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
    from tests.conftest import publish_record
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
    publish_record(
        hass, entry, "last_nightly", 1,
        {"calibration": {
            "front": {"state": "calibrating", "n_obs": 2},
            "side": {"state": "calibrating", "n_obs": 1,
                     "last_reject_reason": "saturated"}}})
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
    from tests.conftest import publish_record
    hass.states.async_set("sensor.d", "30.0")  # current dominant moisture
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
    publish_record(hass, entry, "targets", 1, {"target_floors": {"front": 45.0}})
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
    from tests.conftest import publish_record
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
    publish_record(hass, entry, "runtimes", 1, {"refill_depths_mm": {"zone-abc": 17.5}})
    await hass.async_block_till_done()
    refill = hass.states.get("sensor.geodrops_rachio_front_refill_depth")
    assert float(refill.state) == 17.5  # live Rachio value, not the static 10


async def test_scheduler_status_sensor_mirrors_scheduler(hass, enable_pyscript_and_rachio):
    """The main device gets a Status sensor mirroring the scheduler's status."""
    from tests.conftest import publish_record
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    publish_record(hass, entry, "status", "waiting", {"detail": "watering starts 01:44"})
    await hass.async_block_till_done()
    s = hass.states.get("sensor.geodrops_rachio_status")
    assert s is not None and s.state == "Waiting"  # title-cased for display
    assert s.attributes["detail"] == "watering starts 01:44"
    assert s.attributes["status"] == "waiting"


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
    from tests.conftest import zone_device
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
    device = zone_device(hass, entry, "front_slope")
    assert device is not None and device.name == "Front Slope"


@pytest.mark.parametrize("key, method", [
    ("preview", "async_preview"), ("refresh_runtimes", "async_refresh_runtimes")])
async def test_long_action_buttons_run_in_the_background(
        hass, enable_pyscript_and_rachio, key, method):
    import asyncio
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    release, finished = asyncio.Event(), asyncio.Event()

    async def slow():
        await release.wait()
        finished.set()
    scheduler = hass.data[DOMAIN][entry.entry_id]["scheduler"]
    with patch.object(scheduler, method, slow):
        # A blocking press returns while the action is still running (were it
        # awaited inline, the press would hang until the timeout fails it).
        await asyncio.wait_for(hass.services.async_call(
            "button", "press", {"entity_id": f"button.geodrops_rachio_{key}"},
            blocking=True), 5)
        assert not finished.is_set()
        release.set()
        await hass.async_block_till_done()
    assert finished.is_set()


@pytest.mark.parametrize("entity_id", [
    "sensor.geodrops_rachio_last_nightly", "sensor.geodrops_rachio_last_run",
    "sensor.geodrops_rachio_plan"])
async def test_record_sensor_attributes_stay_out_of_the_recorder(
        hass, enable_pyscript_and_rachio, entity_id):
    from homeassistant.const import MATCH_ALL
    from homeassistant.helpers.entity_component import DATA_INSTANCES
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity = hass.data[DATA_INSTANCES]["sensor"].get_entity(entity_id)
    assert MATCH_ALL in entity._state_info["unrecorded_attributes"]


async def test_standby_switch_turns_back_off(hass, enable_pyscript_and_rachio):
    """Standby is the operator's kill switch for tonight's run; turning it back
    off must take, or the system stays silently parked."""
    await _setup_observed(hass, ENTRY_DATA)
    for service, state in (("turn_on", "on"), ("turn_off", "off")):
        await hass.services.async_call(
            "switch", service, {"entity_id": "switch.geodrops_rachio_standby"},
            blocking=True)
        assert hass.states.get("switch.geodrops_rachio_standby").state == state


async def test_zone_exclude_switch_toggles(hass, enable_pyscript_and_rachio):
    await _setup_observed(hass, {**ENTRY_DATA, "zones": [{
        "key": "front", "rachio_switch": "switch.x", "dominant_sensor": "sensor.d",
        "state_sensor": "sensor.s", "quality_sensors": [], "target_range": "moist",
        "runtime_minutes": 20, "refill_depth_mm": 10}]})
    ent = "switch.geodrops_rachio_front_exclude"
    for service, state in (("turn_on", "on"), ("turn_off", "off")):
        await hass.services.async_call("switch", service, {"entity_id": ent},
                                       blocking=True)
        assert hass.states.get(ent).state == state


async def test_drought_level_select_changes_and_restores(hass, enable_pyscript_and_rachio):
    from pytest_homeassistant_custom_component.common import mock_restore_cache
    ent = "select.geodrops_rachio_drought_level"
    mock_restore_cache(hass, [State(ent, "not a level")])   # ignored: not an option
    await _setup_observed(hass, ENTRY_DATA)
    assert hass.states.get(ent).state == "Level 1 - Mild"
    options = hass.states.get(ent).attributes["options"]
    await hass.services.async_call("select", "select_option",
                                   {"entity_id": ent, "option": options[-1]},
                                   blocking=True)
    assert hass.states.get(ent).state == options[-1]


async def test_observed_sensor_ignores_non_numeric_readings(
        hass, enable_pyscript_and_rachio, freezer):
    """An 'unavailable' station must not end the night's mean: the last good
    reading carries until a number returns."""
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to("2026-06-15 23:00:00")
    await _setup_observed(hass)
    hass.states.async_set("sensor.station_temp", "10.0")
    await hass.async_block_till_done()
    freezer.move_to("2026-06-16 01:00:00")
    hass.states.async_set("sensor.station_temp", "unavailable")
    await hass.async_block_till_done()
    await _tick(hass, freezer, "2026-06-16 02:00:00")
    assert float(hass.states.get(OBSERVED_TEMP).state) == 10.0


async def test_observed_sensor_skips_corrupt_restored_samples(
        hass, enable_pyscript_and_rachio, freezer):
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to("2026-06-16 01:00:00")
    mock_restore_cache_with_extra_data(hass, [(
        State(OBSERVED_TEMP, "10.0"),
        {"samples": [["not a time", 1.0], ["2026-06-15T23:00:00", 2.0],
                     ["2026-06-15T23:00:00+00:00", "x"], ["short"],
                     ["2026-06-15T23:30:00+00:00", 20.0]]})])
    await _setup_observed(hass)
    await _tick(hass, freezer, "2026-06-16 01:30:00")
    # Only the one well-formed, tz-aware sample survives.
    assert float(hass.states.get(OBSERVED_TEMP).state) == 20.0
