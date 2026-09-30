"""The scheduler still finds its entities after a user renames them (Settings
-> Entities -> Entity ID): the integration's own (owned_entities) and the ones
bound in the wizard (entity_renames). Read by the old id an entity would look
missing, which the engine treats as off: Standby and Exclude would stop
applying and the scheduler would water."""
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.geodrops_rachio.const import DOMAIN
from custom_components.geodrops_rachio.owned_entities import owned_ids
from tests.engine.scenario import entry_data


@pytest.fixture(autouse=True)
def _config_dir(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)


@pytest.fixture(autouse=True)
def _no_startup_sleep():
    with patch("custom_components.geodrops_rachio.engine.scheduler.Scheduler._on_startup",
               AsyncMock()):
        yield


async def _setup(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data())
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    scheduler = hass.data[DOMAIN][entry.entry_id]["scheduler"]
    scheduler._current_cfg = scheduler._load_cfg()
    scheduler._current_bindings = scheduler._current_cfg.bindings
    return entry, scheduler


def _rename(hass, old, new):
    er.async_get(hass).async_update_entity(old, new_entity_id=new)


async def _turn_on(hass, entity_id):
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": entity_id}, blocking=True)


async def test_owned_ids_match_the_entities_setup_creates(hass, enable_pyscript_and_rachio):
    """Guards owned_ids against drifting from the platforms: each listed id is
    the id its entity is actually created with, under the listed unique_id."""
    entry, _ = await _setup(hass)
    registry = er.async_get(hass)
    for entity_id, (domain, unique_id) in owned_ids(entry).items():
        assert registry.async_get_entity_id(domain, DOMAIN, unique_id) == entity_id


async def test_renamed_standby_still_applies(hass, enable_pyscript_and_rachio):
    _, scheduler = await _setup(hass)
    _rename(hass, "switch.geodrops_rachio_standby", "switch.lawn_standby")
    await hass.async_block_till_done()
    assert not scheduler._is_standby()
    await _turn_on(hass, "switch.lawn_standby")
    assert scheduler._is_standby()


async def test_renamed_zone_exclude_still_applies(hass, enable_pyscript_and_rachio):
    _, scheduler = await _setup(hass)
    _rename(hass, "switch.geodrops_rachio_front_exclude", "switch.front_lawn_off")
    await hass.async_block_till_done()
    front = scheduler._current_cfg.zones["front"]
    assert not scheduler._zone_excluded(front)
    await _turn_on(hass, "switch.front_lawn_off")
    assert scheduler._zone_excluded(front)
    assert not scheduler._zone_excluded(scheduler._current_cfg.zones["back"])


async def test_renamed_drought_level_still_read(hass, enable_pyscript_and_rachio):
    _, scheduler = await _setup(hass)
    _rename(hass, "select.geodrops_rachio_drought_level", "select.drought")
    await hass.async_block_till_done()
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": "select.drought", "option": "Level 0 - Normal"}, blocking=True)
    assert scheduler.port.state(
        scheduler._current_bindings.drought_level_select) == "Level 0 - Normal"


async def test_logbook_entries_follow_a_renamed_status_sensor(
        hass, enable_pyscript_and_rachio):
    from pytest_homeassistant_custom_component.common import async_mock_service
    _, scheduler = await _setup(hass)
    _rename(hass, "sensor.geodrops_rachio_status", "sensor.lawn_status")
    await hass.async_block_till_done()
    logs = async_mock_service(hass, "logbook", "log")
    await scheduler._activity("hello")
    assert [c.data["entity_id"] for c in logs] == ["sensor.lawn_status"]


# --- Entities the user bound in the wizard (entity_renames) ------------------

def _external(hass, entity_id, state="off"):
    """Register an entity another integration owns, with a state."""
    domain, object_id = entity_id.split(".", 1)
    er.async_get(hass).async_get_or_create(
        domain, "other", object_id, suggested_object_id=object_id)
    hass.states.async_set(entity_id, state)


def _running(hass, entry):
    return hass.data[DOMAIN][entry.entry_id]["scheduler"]


async def test_renamed_rachio_standby_is_rebound_and_applies(
        hass, enable_pyscript_and_rachio):
    _external(hass, "switch.rachio_standby")
    entry, before = await _setup(hass)
    _rename(hass, "switch.rachio_standby", "switch.rachio_pause")
    await hass.async_block_till_done()

    assert entry.data["bindings"]["standby_switch"] == "switch.rachio_pause"
    scheduler = _running(hass, entry)
    assert scheduler is not before                    # idle: reloaded at once
    scheduler._current_cfg = scheduler._load_cfg()
    scheduler._current_bindings = scheduler._current_cfg.bindings
    assert scheduler._current_bindings.standby_switch == "switch.rachio_pause"
    hass.states.async_set("switch.rachio_pause", "on")
    assert scheduler._is_standby()


async def test_renamed_zone_sensors_are_rebound(hass, enable_pyscript_and_rachio):
    _external(hass, "sensor.front_dominant", "55")
    _external(hass, "sensor.front_q2", "Good")
    entry, _ = await _setup(hass)
    _rename(hass, "sensor.front_dominant", "sensor.front_moisture")
    _rename(hass, "sensor.front_q2", "sensor.front_quality_2")
    await hass.async_block_till_done()

    front = next(z for z in entry.data["zones"] if z["key"] == "front")
    assert front["dominant_sensor"] == "sensor.front_moisture"
    assert front["quality_sensors"] == [
        "sensor.front_q1", "sensor.front_quality_2", "sensor.front_q3"]
    back = next(z for z in entry.data["zones"] if z["key"] == "back")
    assert back["dominant_sensor"] == "sensor.back_dominant"


async def test_rename_during_a_run_applies_now_and_rebinds_after_it(
        hass, enable_pyscript_and_rachio):
    """A reload would cancel the waiting or watering run, so the engine reads
    the new id straight away and the entry reloads once the run ends."""
    import asyncio
    _external(hass, "switch.rachio_standby")
    entry, scheduler = await _setup(hass)
    release = asyncio.Event()
    scheduler.run_task = asyncio.ensure_future(release.wait())  # untracked by hass

    _rename(hass, "switch.rachio_standby", "switch.rachio_pause")
    _rename(hass, "switch.rachio_pause", "switch.rachio_hold")    # twice
    await hass.async_block_till_done()
    assert entry.data["bindings"]["standby_switch"] == "switch.rachio_hold"
    assert _running(hass, entry) is scheduler                # not reloaded mid-run
    hass.states.async_set("switch.rachio_hold", "on")
    assert scheduler._is_standby()                           # old id still resolves

    run = scheduler.run_task
    release.set()
    await run
    await hass.async_block_till_done()
    assert _running(hass, entry) is not scheduler            # reloaded after it


async def test_renames_of_owned_entities_leave_the_entry_alone(
        hass, enable_pyscript_and_rachio):
    entry, scheduler = await _setup(hass)
    data = dict(entry.data)
    _rename(hass, "switch.geodrops_rachio_standby", "switch.lawn_standby")
    await hass.async_block_till_done()
    assert dict(entry.data) == data
    assert _running(hass, entry) is scheduler


def test_bound_entity_ids_skip_services_prefixes_and_text():
    from custom_components.geodrops_rachio.entity_renames import (
        bound_entity_ids, replace_entity_id)
    data = {"bindings": {
        "notify_service": "notify.phone",
        "standby_switch": "switch.rachio_standby",
        "derived": {"precipitation_chance_prefix": "sensor.precipitation_chance_"},
        "rachio_device_name": "Main House"},
        "zones": [{"key": "front", "quality_sensors": ["sensor.q1", "sensor.q2"]}],
        "advanced_overrides": "x: sensor.q1"}
    assert bound_entity_ids(data) == {
        "switch.rachio_standby", "sensor.q1", "sensor.q2"}
    out = replace_entity_id(data, "sensor.q1", "sensor.quality")
    assert out["zones"][0]["quality_sensors"] == ["sensor.quality", "sensor.q2"]
    assert out["advanced_overrides"] == "x: sensor.q1"
    assert data["zones"][0]["quality_sensors"][0] == "sensor.q1"   # not mutated


async def test_rename_while_options_open_survives_done(hass, enable_pyscript_and_rachio):
    """The options flow edits its own copy of the entry; pressing Done must not
    write the old id back over a rename made while the dialog was open."""
    _external(hass, "switch.rachio_standby")
    entry, _ = await _setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    _rename(hass, "switch.rachio_standby", "switch.rachio_pause")
    await hass.async_block_till_done()
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "finish"})
    await hass.async_block_till_done()
    assert entry.data["bindings"]["standby_switch"] == "switch.rachio_pause"
    scheduler = _running(hass, entry)
    cfg = scheduler._load_cfg()
    assert cfg.bindings.standby_switch == "switch.rachio_pause"
