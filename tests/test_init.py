from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.geodrops_rachio.const import DOMAIN

DATA = {"bindings": {}, "zones": [], "self_calibration_enabled": False,
        "advanced_overrides": ""}


@pytest.fixture(autouse=True)
def _config_dir(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)


@pytest.fixture(autouse=True)
def _no_startup_sleep():
    with patch("custom_components.geodrops_rachio.engine.scheduler.Scheduler._on_startup",
               AsyncMock()):
        yield


async def test_setup_and_unload_entry(hass, enable_pyscript_and_rachio):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    assert "scheduler" in hass.data[DOMAIN][entry.entry_id]
    with patch("custom_components.geodrops_rachio.engine.scheduler.Scheduler.async_shutdown",
               AsyncMock()) as shutdown:
        assert await hass.config_entries.async_unload(entry.entry_id)
    shutdown.assert_awaited_once()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_retires_delivered_pyscript(hass, tmp_path, enable_pyscript_and_rachio):
    ps = tmp_path / "pyscript"
    ps.mkdir()
    (ps / "geodrops_rachio.py").write_text("# legacy")
    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert not (ps / "geodrops_rachio.py").exists()


async def test_setup_does_not_need_pyscript(hass):
    hass.config.components.add("rachio")
    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED


async def test_bad_overrides_not_ready(hass, enable_pyscript_and_rachio):
    entry = MockConfigEntry(domain=DOMAIN, data={**DATA, "advanced_overrides": "- a"})
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_nightly_trigger_starts_run(hass, enable_pyscript_and_rachio):
    from datetime import timedelta
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed
    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)
    with patch("custom_components.geodrops_rachio.engine.scheduler.Scheduler._start_run",
               AsyncMock()) as start:
        assert await hass.config_entries.async_setup(entry.entry_id)
        now = dt_util.now()
        async_fire_time_changed(hass, now.replace(hour=23, minute=0, second=0, microsecond=0)
                                + timedelta(days=1))
        await hass.async_block_till_done()
    start.assert_awaited_with(True, "nightly")


async def test_reload_listener_honors_suppress_flag(hass, enable_pyscript_and_rachio):
    from custom_components.geodrops_rachio.const import DOMAIN
    entry = MockConfigEntry(domain=DOMAIN, data={
        "bindings": {}, "zones": [], "self_calibration_enabled": False,
        "advanced_overrides": ""})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    reloads = []

    async def _fake_reload(entry_id):
        reloads.append(entry_id)

    with patch.object(hass.config_entries, "async_reload", _fake_reload):
        # Flag set -> update fires the listener but it must NOT reload.
        hass.data[DOMAIN][entry.entry_id]["suppress_reload"] = True
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "advanced_overrides": "a: 1"})
        await hass.async_block_till_done()
        assert reloads == []

        # Flag cleared -> the next data change reloads exactly once.
        hass.data[DOMAIN][entry.entry_id]["suppress_reload"] = False
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "advanced_overrides": "a: 2"})
        await hass.async_block_till_done()
        assert reloads == [entry.entry_id]


async def test_options_flow_defers_reload_until_finish(hass, enable_pyscript_and_rachio):
    from unittest.mock import patch as _patch
    from custom_components.geodrops_rachio.const import DOMAIN
    zone = {"key": "front", "rachio_switch": "switch.front",
            "dominant_sensor": "sensor.d", "state_sensor": "sensor.s",
            "quality_sensors": [], "target_range": "moist",
            "runtime_minutes": 45, "refill_depth_mm": 7.11}
    entry = MockConfigEntry(domain=DOMAIN, data={
        "bindings": {"notify_service": "notify.phone", "rachio_device_name": "Main House"},
        "zones": [dict(zone)], "self_calibration_enabled": False,
        "advanced_overrides": ""})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    reloads = []

    async def _fake_reload(entry_id):
        reloads.append(entry_id)

    async def _resolve(h, name):
        return None

    base = "custom_components.geodrops_rachio.config_flow."
    with _patch.object(hass.config_entries, "async_reload", _fake_reload), \
            _patch(base + "resolve_secret", _resolve):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "add_zone"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"key": "back", "rachio_switch": "switch.back",
             "dominant_sensor": "sensor.d2", "state_sensor": "sensor.s2",
             "quality_sensors": [], "target_range": "moist",
             "runtime_minutes": 20, "refill_depth_mm": 10})
        await hass.async_block_till_done()
        # Persisted, but the guard held off the reload.
        assert [z["key"] for z in entry.data["zones"]] == ["front", "back"]
        assert reloads == []
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "finish"})
        await hass.async_block_till_done()
    assert reloads == [entry.entry_id]           # exactly one, at finish


async def test_removing_zone_purges_its_device(hass, enable_pyscript_and_rachio):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from homeassistant.helpers import device_registry as dr
    from custom_components.geodrops_rachio.const import DOMAIN
    def _zone(k):
        return {"key": k, "rachio_switch": "switch.x", "dominant_sensor": "sensor.d",
                "state_sensor": "sensor.s", "quality_sensors": [], "target_range": "moist",
                "runtime_minutes": 20, "refill_depth_mm": 10}
    entry = MockConfigEntry(domain=DOMAIN, data={
        "bindings": {"weather": {}, "forecast_entity": "weather.home"},
        "zones": [_zone("front"), _zone("back")],
        "self_calibration_enabled": False, "advanced_overrides": ""})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    reg = dr.async_get(hass)
    assert reg.async_get_device({(DOMAIN, f"{entry.entry_id}:zone:back")})

    hass.config_entries.async_update_entry(entry, data={**entry.data, "zones": [_zone("front")]})
    await hass.async_block_till_done()  # triggers reload via existing options listener
    assert reg.async_get_device({(DOMAIN, f"{entry.entry_id}:zone:back")}) is None
    assert reg.async_get_device({(DOMAIN, f"{entry.entry_id}:zone:front")})
