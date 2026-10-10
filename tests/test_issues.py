import datetime as dt
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry, async_fire_time_changed)

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


def _issue(hass, entry, key="missing_entities"):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{key}_{entry.entry_id}")


async def _setup(hass, data=DATA):
    # Migrated already: migrating from 1.1 would replace the API key.
    entry = MockConfigEntry(domain=DOMAIN, data=data, minor_version=4)
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


# --- unavailable for a day ----------------------------------------------------
async def test_entity_unavailable_for_a_day_raises_an_issue(
        hass, freezer, enable_pyscript_and_rachio):
    for eid in BOUND:
        hass.states.async_set(eid, "1")
    hass.states.async_set("sensor.front_moisture", "unavailable")
    entry = await _setup(hass)
    # A restart, an integration reloading or a cloud blip: not reported.
    freezer.tick(dt.timedelta(hours=23))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _issue(hass, entry, "unavailable_entities") is None

    freezer.tick(dt.timedelta(hours=1, minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    issue = _issue(hass, entry, "unavailable_entities")
    assert issue is not None and issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_placeholders["entities"] == "`sensor.front_moisture`"
    # Not missing: it still has a state.
    assert _issue(hass, entry) is None

    # Clears the moment it reports again, not at the next hourly check.
    hass.states.async_set("sensor.front_moisture", "42")
    await hass.async_block_till_done()
    assert _issue(hass, entry, "unavailable_entities") is None


async def test_unavailable_own_entities_are_not_reported(
        hass, freezer, enable_pyscript_and_rachio):
    """The integration's own entities are unavailable only while it is."""
    for eid in BOUND:
        hass.states.async_set(eid, "1")
    hass.states.async_set("select.geodrops_rachio_drought_level", "unavailable")
    entry = await _setup(hass, {**DATA, "bindings": {
        **DATA["bindings"],
        "drought_level_select": "select.geodrops_rachio_drought_level"}})
    freezer.tick(dt.timedelta(hours=25))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _issue(hass, entry, "unavailable_entities") is None


# --- Rachio integration ---------------------------------------------------------
async def test_rachio_not_loaded_raises_an_issue_until_it_loads(
        hass, enable_pyscript_and_rachio):
    for eid in BOUND:
        hass.states.async_set(eid, "1")
    entry = await _setup(hass)
    issue = _issue(hass, entry, "rachio_not_loaded")
    assert issue is not None and issue.severity is ir.IssueSeverity.ERROR

    rachio = MockConfigEntry(domain="rachio")
    rachio.add_to_hass(hass)
    rachio.mock_state(hass, ConfigEntryState.LOADED)
    await hass.async_block_till_done()
    assert _issue(hass, entry, "rachio_not_loaded") is None

    rachio.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    await hass.async_block_till_done()
    assert _issue(hass, entry, "rachio_not_loaded") is not None


def _account(*names):
    return AsyncMock(return_value=(
        "acct", [{"id": str(i), "name": n} for i, n in enumerate(names)]))


@pytest.mark.parametrize(("bound", "raised"), [
    ("Front Yard", False),
    ("Front Yard Controller", False),  # rachio.stop_watering matches by `in`
    ("Back Yard", True),
    ("", True),
])
async def test_unknown_rachio_controller_raises_an_issue(
        hass, enable_pyscript_and_rachio, bound, raised):
    for eid in BOUND:
        hass.states.async_set(eid, "1")
    data = {**DATA, "api_key": "k",
            "bindings": {**DATA["bindings"], "rachio_device_name": bound}}
    with patch("custom_components.geodrops_rachio.rachio_client.async_fetch_account",
               _account("Front Yard", "Pool")):
        entry = await _setup(hass, data)
        # The key check is a background task: setup does not wait for it.
        await hass.async_block_till_done(wait_background_tasks=True)
    issue = _issue(hass, entry, "rachio_device_unknown")
    assert (issue is not None) is raised
    if raised:
        assert issue.translation_placeholders["controllers"] == "Front Yard, Pool"


async def test_unreachable_rachio_leaves_the_controller_unchecked(
        hass, enable_pyscript_and_rachio):
    from custom_components.geodrops_rachio.rachio_client import RachioConnectionError
    data = {**DATA, "api_key": "k",
            "bindings": {**DATA["bindings"], "rachio_device_name": "Gone"}}
    with patch("custom_components.geodrops_rachio.rachio_client.async_fetch_account",
               AsyncMock(side_effect=RachioConnectionError("down"))):
        entry = await _setup(hass, data)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert _issue(hass, entry, "rachio_device_unknown") is None


# --- notify service -------------------------------------------------------------
async def test_missing_notify_service_raises_an_issue_until_it_exists(
        hass, enable_pyscript_and_rachio):
    for eid in BOUND:
        hass.states.async_set(eid, "1")
    entry = await _setup(hass, {**DATA, "bindings": {
        **DATA["bindings"], "notify_service": "notify.mobile_app_phone"}})
    issue = _issue(hass, entry, "notify_missing")
    assert issue is not None and issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_placeholders["service"] == "`notify.mobile_app_phone`"

    hass.services.async_register("notify", "mobile_app_phone", AsyncMock())
    await hass.async_block_till_done()
    assert _issue(hass, entry, "notify_missing") is None

    hass.services.async_remove("notify", "mobile_app_phone")
    await hass.async_block_till_done()
    assert _issue(hass, entry, "notify_missing") is not None


async def test_entry_issues_wait_for_start_and_clear_on_unload(
        hass, enable_pyscript_and_rachio):
    hass.set_state(CoreState.starting)
    entry = await _setup(hass, {**DATA, "bindings": {
        **DATA["bindings"], "notify_service": "notify.later"}})
    # Services and other integrations' entries load after ours.
    assert _issue(hass, entry, "notify_missing") is None
    assert _issue(hass, entry, "rachio_not_loaded") is None
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert _issue(hass, entry, "notify_missing") is not None
    assert _issue(hass, entry, "rachio_not_loaded") is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert _issue(hass, entry, "notify_missing") is None
    assert _issue(hass, entry, "rachio_not_loaded") is None
