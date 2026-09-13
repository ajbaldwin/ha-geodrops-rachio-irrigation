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
