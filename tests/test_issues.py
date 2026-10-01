from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.geodrops_rachio.const import DOMAIN

ZONE = {"key": "front", "rachio_switch": "switch.front_valve",
        "dominant_sensor": "sensor.front_moisture", "state_sensor": "sensor.front_state",
        "quality_sensors": [], "target_range": "moist",
        "runtime_minutes": 20, "refill_depth_mm": 10}
DATA = {"bindings": {"weather": {"precipitation_chance_prefix": "sensor.precip_chance"},
                     "forecast_entity": "weather.home"},
        "zones": [ZONE], "self_calibration_enabled": False, "advanced_overrides": ""}
BOUND = ("switch.front_valve", "sensor.front_moisture", "sensor.front_state",
         "weather.home")


@pytest.fixture(autouse=True)
def _quiet_scheduler(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    with patch("custom_components.geodrops_rachio.engine.scheduler.Scheduler._on_startup",
               AsyncMock()), \
         patch("custom_components.geodrops_rachio.engine.scheduler.Scheduler.async_start"):
        yield


def _issue(hass, entry):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"missing_entities_{entry.entry_id}")


async def _setup(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_missing_bound_entities_raise_an_issue(hass, enable_pyscript_and_rachio):
    for eid in BOUND[1:]:
        hass.states.async_set(eid, "1")
    entry = await _setup(hass)
    issue = _issue(hass, entry)
    assert issue is not None and issue.severity is ir.IssueSeverity.ERROR
    # Only the missing one; the prefix and the integration's own ids are not ids.
    assert issue.translation_placeholders["entities"] == "`switch.front_valve`"

    # Restored, and re-checked by the reload that Configure triggers.
    hass.states.async_set("switch.front_valve", "off")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass, entry) is None


async def test_registered_but_unavailable_is_not_missing(hass, enable_pyscript_and_rachio):
    """An entity whose integration is down keeps its registry entry."""
    reg = er.async_get(hass)
    reg.async_get_or_create("switch", "rachio", "valve", suggested_object_id="front_valve")
    for eid in BOUND[1:]:
        hass.states.async_set(eid, "1")
    entry = await _setup(hass)
    assert _issue(hass, entry) is None


async def test_deleting_a_bound_entity_raises_the_issue(hass, enable_pyscript_and_rachio):
    reg = er.async_get(hass)
    reg.async_get_or_create("sensor", "geodrops", "m", suggested_object_id="front_moisture")
    for eid in BOUND:
        hass.states.async_set(eid, "1")
    entry = await _setup(hass)
    assert _issue(hass, entry) is None

    reg.async_remove("sensor.front_moisture")
    await hass.async_block_till_done()
    issue = _issue(hass, entry)
    assert issue is not None
    assert issue.translation_placeholders["entities"] == "`sensor.front_moisture`"


async def test_check_waits_for_home_assistant_to_start(hass, enable_pyscript_and_rachio):
    """Other integrations' entities load after ours: no issue until started."""
    hass.set_state(CoreState.starting)
    entry = await _setup(hass)
    assert _issue(hass, entry) is None
    for eid in BOUND[1:]:
        hass.states.async_set(eid, "1")
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert _issue(hass, entry).translation_placeholders["entities"] == "`switch.front_valve`"


async def test_unloading_clears_the_issue(hass, enable_pyscript_and_rachio):
    entry = await _setup(hass)
    assert _issue(hass, entry) is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert _issue(hass, entry) is None


async def test_retired_switch_bindings_are_not_reported(hass, enable_pyscript_and_rachio):
    """An entry from before 1.1 still binds the retired Dew formed and Run
    active switches; setup deletes the switches, and the migration the
    bindings, so neither is reported missing."""
    for eid in BOUND:
        hass.states.async_set(eid, "1")
    entry = MockConfigEntry(domain=DOMAIN, minor_version=3, data={
        **DATA, "api_key": "k",
        "bindings": {**DATA["bindings"],
                     "dew_formed_boolean": "switch.geodrops_rachio_dew_formed",
                     "run_active_boolean": "switch.geodrops_rachio_run_active"}})
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    for key in ("dew_formed", "run_active"):
        reg.async_get_or_create(
            "switch", DOMAIN, f"{entry.entry_id}_{key}",
            suggested_object_id=f"geodrops_rachio_{key}", config_entry=entry)
    with patch("custom_components.geodrops_rachio._async_check_api_key", AsyncMock()):
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.minor_version == 4
    assert entry.data["bindings"] == DATA["bindings"]
    assert _issue(hass, entry) is None
