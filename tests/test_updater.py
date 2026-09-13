from custom_components.geodrops_rachio import updater


def test_decide_action():
    assert updater.decide_action(wrapper_changed=True, brain_changed=False) == "restart"
    assert updater.decide_action(wrapper_changed=False, brain_changed=True) == "reload"
    assert updater.decide_action(wrapper_changed=False, brain_changed=False) == "noop"
    # wrapper change dominates (Python can't hot-swap)
    assert updater.decide_action(wrapper_changed=True, brain_changed=True) == "restart"


from unittest.mock import AsyncMock, patch
from pytest_homeassistant_custom_component.common import MockConfigEntry
from custom_components.geodrops_rachio.const import DOMAIN

async def test_listener_reloads_on_version_bump(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    updater.async_register_update_listener(hass, entry)
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
        hass.states.async_set(updater.HACS_UPDATE_ENTITY, "on", {"installed_version": "1.0"})
        await hass.async_block_till_done()
        hass.states.async_set(updater.HACS_UPDATE_ENTITY, "off", {"installed_version": "1.1"})
        await hass.async_block_till_done()
        reload.assert_awaited_once_with(entry.entry_id)
