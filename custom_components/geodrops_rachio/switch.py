from __future__ import annotations
from typing import Any
from homeassistant.components.switch import SwitchEntity, ENTITY_ID_FORMAT
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from .coordinator import GeodropsRachioConfigEntry
from .entity import GeodropsRachioEntity, GeodropsRachioZoneEntity

# Local settings; nothing to fetch.
PARALLEL_UPDATES = 0

_FLAGS = ["standby"]


# RestoreEntity ahead of SwitchEntity: its async_added_to_hass sets up the
# restore-state machinery that async_get_last_state reads, so it must resolve
# first in the MRO and be chained via super() below.
class _RestoredSwitch(RestoreEntity, SwitchEntity):
    """An on/off setting that survives a restart."""

    _attr_is_on = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            self._attr_is_on = last.state == "on"

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()


class FlagSwitch(GeodropsRachioEntity, _RestoredSwitch):
    def __init__(self, entry: GeodropsRachioConfigEntry, key: str) -> None:
        super().__init__(entry, ENTITY_ID_FORMAT, key)


class ZoneExcludeSwitch(GeodropsRachioZoneEntity, _RestoredSwitch):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: GeodropsRachioConfigEntry, key: str,
                 hub_device_id: str) -> None:
        super().__init__(entry, ENTITY_ID_FORMAT, key, hub_device_id, "exclude")


async def async_setup_entry(hass: HomeAssistant, entry: GeodropsRachioConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    hub_device_id = entry.runtime_data.hub_device_id
    entities: list[SwitchEntity] = [FlagSwitch(entry, k) for k in _FLAGS]
    entities.extend(
        ZoneExcludeSwitch(entry, z["key"], hub_device_id)
        for z in entry.data.get("zones", []))
    async_add_entities(entities)
