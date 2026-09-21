from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.geodrops_rachio.const import DOMAIN


async def test_setup_and_unload_entry(hass, enable_pyscript_and_rachio):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    with patch("custom_components.geodrops_rachio.delivery.async_deliver",
               AsyncMock(return_value=True)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.LOADED
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_delivers_and_registers_listener(hass, enable_pyscript_and_rachio):
    entry = MockConfigEntry(domain=DOMAIN, data={
        "bindings": {}, "zones": [], "self_calibration_enabled": False,
        "advanced_overrides": "",
    })
    entry.add_to_hass(hass)
    with patch("custom_components.geodrops_rachio.delivery.async_deliver",
               AsyncMock(return_value=True)) as deliver:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        deliver.assert_awaited()


async def test_reload_listener_honors_suppress_flag(hass, enable_pyscript_and_rachio):
    from custom_components.geodrops_rachio.const import DOMAIN
    entry = MockConfigEntry(domain=DOMAIN, data={
        "bindings": {}, "zones": [], "self_calibration_enabled": False,
        "advanced_overrides": ""})
    entry.add_to_hass(hass)
    with patch("custom_components.geodrops_rachio.delivery.async_deliver",
               AsyncMock(return_value=True)):
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
    with patch("custom_components.geodrops_rachio.delivery.async_deliver",
               AsyncMock(return_value=False)):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        reg = dr.async_get(hass)
        assert reg.async_get_device({(DOMAIN, f"{entry.entry_id}:zone:back")})

        hass.config_entries.async_update_entry(entry, data={**entry.data, "zones": [_zone("front")]})
        await hass.async_block_till_done()  # triggers reload via existing options listener
        assert reg.async_get_device({(DOMAIN, f"{entry.entry_id}:zone:back")}) is None
        assert reg.async_get_device({(DOMAIN, f"{entry.entry_id}:zone:front")})
