from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.geodrops_rachio.const import DOMAIN

ENTRY_DATA = {"bindings": {}, "zones": [], "self_calibration_enabled": False,
              "advanced_overrides": ""}


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
