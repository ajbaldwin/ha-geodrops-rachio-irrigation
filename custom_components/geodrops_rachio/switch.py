from __future__ import annotations
from homeassistant.components.switch import SwitchEntity, ENTITY_ID_FORMAT
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from .entity_base import device_info, zone_device_info
from .util import slug

_FLAGS = [("standby", "Standby")]


class FlagSwitch(RestoreEntity, SwitchEntity):
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, key: str, name: str) -> None:
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self.entity_id = ENTITY_ID_FORMAT.format(f"geodrops_rachio_{key}")
        self._attr_is_on = False
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            self._attr_is_on = last.state == "on"

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()


# RestoreEntity first, matching FlagSwitch: its async_added_to_hass sets up the
# restore-state machinery that async_get_last_state reads, so it must resolve
# ahead of SwitchEntity in the MRO and be chained via super() below.
class ZoneExcludeSwitch(RestoreEntity, SwitchEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Exclude from watering"

    def __init__(self, entry: ConfigEntry, key: str) -> None:
        s = slug(key)
        self._attr_unique_id = f"{entry.entry_id}_zone_{s}_exclude"
        self.entity_id = ENTITY_ID_FORMAT.format(f"geodrops_rachio_{s}_exclude")
        self._attr_device_info = zone_device_info(entry, key)
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            self._attr_is_on = last.state == "on"

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    entities = [FlagSwitch(entry, k, n) for k, n in _FLAGS]
    entities.extend(
        ZoneExcludeSwitch(entry, z["key"]) for z in entry.data.get("zones", []))
    async_add_entities(entities)
